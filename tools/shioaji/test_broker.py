# -*- coding: utf-8 -*-
"""
broker.py 的離線測試 —— 真實下單程式的安全網。

**不連永豐、不送任何單。** 全程 dry run，而且把紀錄寫到暫存資料夾，
不會碰到 real_orders/ 裡的真實紀錄。驗兩件事：
  1. 組出來的委託單內容對不對（方向、口數、價格型態、新倉/平倉、停利價）
  2. 每一道防呆會不會真的擋下來

怎麼跑（PowerShell）：
    & "C:\\Users\\USER\\Desktop\\Claude Work\\trade-log\\.venv\\Scripts\\python.exe" `
      "C:\\Users\\USER\\Desktop\\Claude Work\\trade-log\\tools\\shioaji\\test_broker.py"

⚠️ 改過 broker.py 就要重跑一次。那個檔是唯一會動到真錢的地方。
"""
import datetime
import json
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import broker
import shioaji as sj

TMP = pathlib.Path(tempfile.mkdtemp(prefix="broker-test-"))
broker.ORDER_DIR = TMP / "real_orders"
broker.REAL_FLAG = TMP / "REAL_ORDERS_ON"        # 不存在 → dry run
TODAY = datetime.date.today()
FAIL = 0


def chk(name, got, want):
    global FAIL
    ok = got == want
    FAIL += not ok
    print(("  OK   " if ok else "  FAIL ") + name + ("" if ok else f"  (得到 {got!r}，期待 {want!r})"))


def blocked(name, res, expect_reason_has):
    global FAIL
    ok = res[0] is False and expect_reason_has in (res[1] or "")
    FAIL += not ok
    print(("  OK   " if ok else "  FAIL ") + f"{name} → {res[1]}")


class FakeAPI:
    # place_order 故意用「一被呼叫就爆炸」的寫法：這個假券商是給演練模式用的，
    # 演練模式**永遠不該送出任何單**。哪天有人把 dry run 的判斷改壞了，
    # 這裡會當場炸開，而不是安靜地把單送出去。
    def place_order(self, contract, order):
        raise AssertionError("演練模式竟然真的送單了！這是重大錯誤")

    def list_positions(self, acc=None):
        return []


class BadAPI:
    def list_positions(self, acc=None):
        raise RuntimeError("網路斷了")


class HasPos:
    def list_positions(self, acc=None):
        return [type("P", (), {"code": "TMFI6", "quantity": 1,
                               "direction": "Buy", "price": 45000})()]


# SDK 對 account 參數會嚴格檢查型別（實測：隨便捏一個物件會噴
# TypeError: argument 'account': 'A' object is not an instance of 'Account'），
# 所以測試也要用真的 Account，否則測不到正式會走的那條路。
FAKE_ACC = sj.Account(account_type=sj.AccountType.Future, person_id="TESTPID",
                      broker_id="F000000", account_id="0000000", signed=True,
                      username="test")


class BrokerSim:
    """
    會成交的假券商。**送出「平倉的範圍市價單」之後就變成空手** ——
    停利那張也是 Cover 但屬於限價，不算平倉成交。

    ⚠️ 假券商一定要模擬「部位真的會消失」。之前那版永遠回報有部位，
    於是 close() 的確認迴圈永遠等不到空手 —— 測試紅了，但紅的是假物件，
    不是產品（2026-09-01 連續兩次栽在假物件上）。
    """

    def __init__(self, direction="Action.Sell", cancel_ok=True):
        self.flat = False
        self.cancel_ok = cancel_ok
        self.sent = 0
        self.direction = direction

    def place_order(self, contract, order):
        self.sent += 1
        if (str(order.octype) == "FuturesOCType.Cover"
                and str(order.price_type) == "FuturesPriceType.MKP"):
            self.flat = True                 # 平倉市價單 → 成交
        return FakeTrade()

    def update_status(self, acc=None):
        if not self.cancel_ok:
            raise RuntimeError("StatusCode: 400, Detail: Please run update_status")

    def cancel_order(self, t):
        if not self.cancel_ok:
            raise RuntimeError("cancel failed")

    def list_positions(self, acc=None):
        if self.flat:
            return []
        return [type("P", (), {"code": "TMFI6", "quantity": 1,
                               "direction": self.direction, "price": 46978})()]


def connect(api):
    broker._state["api"] = api
    broker._state["account"] = FAKE_ACC
    broker._state["contract"] = type("C", (), {"code": "TMFI6"})()
    broker._state["position"] = None


print("=== 預設狀態 ===")
chk("預設是 dry run（不會真的送單）", broker.is_live(), False)
chk("口數寫死 1 口", broker.QTY, 1)

print("\n=== 組出來的委託單 ===")
o = broker._order(sj.Action.Buy, 0, sj.FuturesPriceType.MKP,
                  sj.OrderType.IOC, sj.FuturesOCType.New)
print("  進場（做多）:", {k: str(getattr(o, k)) for k in
                        ("action", "price", "quantity", "price_type", "order_type", "octype")})
chk("  進場是新倉", o.octype.value, "New")
chk("  進場口數 1", o.quantity, 1)

t = broker._order(sj.Action.Sell, 45500, sj.FuturesPriceType.LMT,
                  sj.OrderType.ROD, sj.FuturesOCType.Cover)
print("  停利（做多的出場）:", {k: str(getattr(t, k)) for k in
                            ("action", "price", "quantity", "price_type", "order_type", "octype")})
chk("  停利是平倉", t.octype.value, "Cover")
chk("  停利是限價（才掛得住）", t.price_type.value, "LMT")
chk("  停利是 ROD（掛著等，不是立刻取消）", t.order_type.value, "ROD")

print("\n=== 防呆：這些情況一律不准送單 ===")
broker._state.update({"api": None, "account": None, "position": None})
blocked("沒有即時報價", broker.can_enter(None, True), "沒有即時報價")
blocked("報價不是即時的", broker.can_enter(45000, False), "沒有即時報價")
blocked("還沒連上永豐", broker.can_enter(45000, True), "還沒連上")

connect(FakeAPI())
ok, err = broker.can_enter(45000, True)
chk("一切正常時可以下單", ok, True)

broker.ORDER_DIR.mkdir(parents=True, exist_ok=True)
with (broker.ORDER_DIR / f"{TODAY}.jsonl").open("w", encoding="utf-8") as f:
    for _ in range(broker.MAX_ENTRIES):
        f.write(json.dumps({"kind": "entry", "live": True, "ok": True}) + "\n")
blocked(f"當天真實進場已達上限 {broker.MAX_ENTRIES} 次", broker.can_enter(45000, True), "上限")
(broker.ORDER_DIR / f"{TODAY}.jsonl").unlink()

connect(BadAPI())
blocked("跟券商對帳失敗（這種時候絕不送單）", broker.can_enter(45000, True), "對帳失敗")

connect(HasPos())
blocked("券商那邊已經有部位", broker.can_enter(45000, True), "已經有部位")

print("\n=== 認帳號：要挑到期貨帳號，不能挑到證券 ===")


class ManyAccounts:
    def list_accounts(self):
        return [sj.Account(account_type=sj.AccountType.Stock, account_id="STOCK1"),
                sj.Account(account_type=sj.AccountType.Future, account_id="FUT1")]

    def list_positions(self, acc=None):
        return []


picked = broker.configure(ManyAccounts(), type("C", (), {"code": "TMFI6"})())
chk("  挑到期貨帳號（不是證券）", getattr(picked, "account_id", None), "FUT1")

print("\n=== 重啟後要撿得回部位（看門狗會重啟，記憶體不可信）===")
connect(HasPos())
broker._state["position"] = None
pos = broker.reconcile()
chk("  對帳撿回方向", pos["dir"], "long")
chk("  標記為『重啟後撿回來的』", pos["recovered"], True)

print("\n=== 演練模式的部位不可以被對帳清掉 ===")
connect(FakeAPI())          # FakeAPI 回報「券商沒有部位」
broker._state["position"] = {"dir": "long", "entry": 45000, "qty": 1,
                             "entry_time": "09:05", "target_trade": None,
                             "recovered": False}
kept = broker.reconcile()
chk("  演練時本機部位留著（不然持倉畫面演練不到）", (kept or {}).get("entry"), 45000)
blocked("  演練時已有部位一樣擋住再進場", broker.can_enter(45000, True), "已經有部位")
broker._state["position"] = None

# ⛔ 但「從券商撿回來的部位」不是演練部位 —— 2026-09-01 12:43 在他機器上真的卡住：
#    面板在真實模式撿回一口多單，之後改回演練、他自己用別的工具平掉，
#    券商已經空手、面板卻永遠掛著那口鬼部位 ⇒ **從此不能再進場**，
#    浮動損益還一直拿現價去對一個不存在的部位算。
broker._state["position"] = {"dir": "long", "entry": 47091, "qty": 1,
                             "entry_time": None, "target_trade": None,
                             "recovered": True}       # ← 差別只在這一個欄位
gone = broker.reconcile()
chk("  券商說平掉了，撿回來的那種要跟著清掉", gone, None)
ok_again, why_again = broker.can_enter(45000, True)
chk("  清掉之後才進得了場（不然他從此下不了單）", ok_again, True)
broker._state["position"] = None

print("\n=== 停利要用實際成交價算，不是送單當下的參考價 ===")
# 模擬帳戶實測：參考價 46833、實際成交 46835 ⇒ 停利要 46935 不是 46933
FILLED = {"code": "TMFI6", "quantity": 1, "direction": "Buy", "price": 46835}


class FakeTrade:
    """place_order 回傳的東西。broker 只會拿它去 cancel_order，不會讀內容。"""


class FilledAPI:
    # ⚠️ 假券商一定要有 place_order。少了它 _send() 會丟 AttributeError，
    #    enter() 直接在第一關就失敗 —— 後面「用成交價算停利」那段根本走不到，
    #    測試看起來是紅的，但驗到的其實是「假物件沒做好」（2026-09-01 踩過）。
    def place_order(self, contract, order):
        return FakeTrade()

    def list_positions(self, acc=None):
        return [type("P", (), FILLED)()]


connect(FilledAPI())
broker.is_live = lambda: True          # 借用「已成交」的假券商，測正式模式的算法
broker._state["position"] = None
ok, err, pos = broker.enter("long", 46833, 100)      # 參考價故意跟成交價差 2 點
chk("  部位記的是實際成交價", (pos or {}).get("entry"), 46835.0)
recs = [json.loads(l) for l in
        (broker.ORDER_DIR / f"{TODAY}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
chk("  停利掛在 46935（成交價 +100），不是 46933", recs[-1]["price"], 46935.0)

print("\n=== 做空的部位不可以被讀成做多（2026-09-01 出過大事）===")


def pos_api(direction_value, qty=1):
    class A:
        def place_order(self, contract, order):
            return FakeTrade()

        def list_positions(self, acc=None):
            return [type("P", (), {"code": "TMFI6", "quantity": qty,
                                   "direction": direction_value, "price": 46978})()]
    return A()


for label, val, want in [("Action.Sell（永豐給的樣子）", "Action.Sell", "short"),
                         ("Action.Buy", "Action.Buy", "long"),
                         ("字串 Sell", "Sell", "short"),
                         ("字串 Buy", "Buy", "long")]:
    connect(pos_api(val))
    chk(f"  {label} → {want}", (broker.broker_position() or {}).get("dir"), want)

connect(pos_api("", qty=-1))
chk("  沒有 direction 欄位、數量是負的 → short", (broker.broker_position() or {}).get("dir"), "short")
connect(pos_api("", qty=0))
chk("  數量 0 → 當作沒有部位", broker.broker_position(), None)

print("\n=== 平倉要用券商確認過的方向，不是記憶體裡那份 ===")
connect(BrokerSim("Action.Sell"))
broker.is_live = lambda: True
broker.FILL_WAIT = 1.0
# 故意把本機記成相反方向，重現 2026-09-01 那個狀況
broker._state["position"] = {"dir": "long", "entry": 46978, "qty": 1,
                             "entry_time": "11:23", "target_trade": None, "recovered": True}
# 先單獨驗對帳：本機那份要被券商改正
fixed = broker.reconcile()
chk("  對帳之後本機那份被改正成 short", (fixed or {}).get("dir"), "short")

# 再驗平倉送出去的是哪一邊
# ⚠️ 平倉成功後 _state["position"] 會變成 None（本來就該這樣），
#    所以「本機被改正了」要在 close() **之前**驗，不能在之後（會 NoneType 爆掉）。
broker.close("test")
recs = [json.loads(l) for l in
        (broker.ORDER_DIR / f"{TODAY}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
# ⚠️ 不可以用 recs[-1] —— 平倉成功後最後一筆是 close_confirmed（沒有 action 欄位）。
#    要找的是「真的送出去的那張單」，也就是最後一筆帶 action 的紀錄。
sent = [r for r in recs if r.get("action")]
chk("  空單平倉送出的是 Buy（舊版會送 Sell，等於再加一口空單）",
    sent[-1]["action"], "Action.Buy")
chk("  有留下「本機方向被券商修正」的紀錄",
    any(r["kind"] == "position_fixed" for r in recs), True)
chk("  平倉成功後本機部位清空", broker._state["position"], None)
broker.is_live = lambda: broker.REAL_FLAG.exists()
(broker.ORDER_DIR / f"{TODAY}.jsonl").unlink()

print("\n=== 平倉沒撮到：不可以當成平掉了（09-01 真的發生）===")


class CloseNeverFills:
    """券商收得到單，但部位一直都在（IOC 沒撮到）。"""

    def __init__(self):
        self.sent = 0

    def place_order(self, contract, order):
        self.sent += 1
        return FakeTrade()

    def update_status(self, acc=None):
        pass

    def cancel_order(self, t):
        pass

    def list_positions(self, acc=None):
        return [type("P", (), {"code": "TMFI6", "quantity": 1,
                               "direction": "Action.Sell", "price": 46978})()]


nofill_api = CloseNeverFills()
connect(nofill_api)
broker.is_live = lambda: True
broker.FILL_WAIT = 0.8
broker._state["position"] = {"dir": "short", "entry": 46978, "qty": 1,
                             "entry_time": "11:44", "target_trade": None,
                             "recovered": False}
ok, err = broker.close("manual")
chk("  回報失敗（不可以說成功）", ok, False)
chk("  本機部位保留 —— 清掉的話停損就不跑了，再按平倉還會說「沒有部位」",
    (broker._state["position"] or {}).get("dir"), "short")
chk("  訊息叫他自己去平倉", "大戶投" in (err or ""), True)
chk(f"  有重試（送了 {nofill_api.sent} 次）", nofill_api.sent >= 2, True)
recs = [json.loads(l) for l in
        (broker.ORDER_DIR / f"{TODAY}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
chk("  有留下 close_failed 紀錄", any(r["kind"] == "close_failed" for r in recs), True)

# 【失敗後要冷卻】主迴圈每 0.25 秒就叫一次停損檢查，沒有冷卻的話只要價格還在
# 停損之外就一直重送 —— 每一輪最多 CLOSE_TRIES 張，一分鐘幾百張反向單出去。
before_sent = nofill_api.sent
ok2, err2 = broker.close("sl")
chk("  剛失敗完不准馬上再送（冷卻中）", nofill_api.sent, before_sent)
chk("  而且要說得出還要等多久", "秒後" in (err2 or ""), True)

# 【互斥】停損跑在主迴圈執行緒、他手按平倉跑在 HTTP 執行緒，兩邊會同時進來。
# 沒有鎖的話兩邊各送一輪，最壞情況反向開出好幾口。
broker._close_fail["at"] = 0.0
held = broker._close_lock.acquire(blocking=False)
ok3, err3 = broker.close("manual")
chk("  已經有人在平倉時，第二個要被擋下來", ok3, False)
chk("  被擋下來的那個一張單都不准送", nofill_api.sent, before_sent)
if held:
    broker._close_lock.release()
broker._close_fail["at"] = 0.0          # 冷卻只針對這一段，別影響後面的測試

broker.is_live = lambda: broker.REAL_FLAG.exists()
broker._state["position"] = None
(broker.ORDER_DIR / f"{TODAY}.jsonl").unlink()

print("\n=== 撤停利單失敗：要照樣平倉，但要講出來 ===")


connect(BrokerSim("Action.Buy", cancel_ok=False))
broker.is_live = lambda: True
broker._state["position"] = {"dir": "long", "entry": 46978, "qty": 1,
                             "entry_time": "11:44", "target_trade": FakeTrade(),
                             "recovered": False}
ok, err = broker.close("manual")
chk("  還是要平掉（裸部位比殘單危險）", ok, True)
chk("  訊息要提醒他去刪那張停利單", "停利單" in (err or ""), True)
recs = [json.loads(l) for l in
        (broker.ORDER_DIR / f"{TODAY}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
chk("  cancel_target 記成失敗",
    next(r for r in recs if r["kind"] == "cancel_target")["ok"], False)
broker.is_live = lambda: broker.REAL_FLAG.exists()
broker._state["position"] = None
(broker.ORDER_DIR / f"{TODAY}.jsonl").unlink()

print("\n=== 有部位但方向不符 → 停手，不可以當成「沒成交」 ===")
connect(pos_api("Action.Buy"))          # 券商說是多單
broker.is_live = lambda: True
broker.FILL_WAIT = 1.0
broker._state["position"] = None
ok, err, pos = broker.enter("short", 46978, 100)   # 但我們送的是空單
chk("  回報失敗", ok, False)
chk("  訊息講出方向不符", "不符" in (err or ""), True)
recs = [json.loads(l) for l in
        (broker.ORDER_DIR / f"{TODAY}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
chk("  記成 entry_mismatch 而不是 entry_nofill", recs[-1]["kind"], "entry_mismatch")
chk("  完全沒有掛停利", any(r["kind"] == "target" for r in recs), False)
broker.is_live = lambda: broker.REAL_FLAG.exists()
(broker.ORDER_DIR / f"{TODAY}.jsonl").unlink()

print("\n=== IOC 沒成交就不可以掛停利 ===")


class NoFillAPI:
    def place_order(self, contract, order):
        return FakeTrade()              # 送得出去

    def list_positions(self, acc=None):
        return []                       # 但沒撮到


connect(NoFillAPI())
broker.is_live = lambda: True
broker.FILL_WAIT = 1.0                  # 測試不要真的等 5 秒
broker._state["position"] = None
ok, err, pos = broker.enter("long", 46833, 100)
chk("  回報失敗", ok, False)
chk("  沒有留下假部位", broker._state["position"], None)
recs = [json.loads(l) for l in
        (broker.ORDER_DIR / f"{TODAY}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
chk("  最後一筆是「沒成交」而不是停利單", recs[-1]["kind"], "entry_nofill")
chk("  完全沒有掛出停利（那張會變成反向新部位）",
    any(r["kind"] == "target" for r in recs[-2:]), False)
broker.is_live = lambda: broker.REAL_FLAG.exists()   # 還原
(broker.ORDER_DIR / f"{TODAY}.jsonl").unlink()

print("\n=== dry run 真的沒送出去 ===")
connect(FakeAPI())
ok, err, pos = broker.enter("long", 45000, 100)
chk("  enter() 回報成功", ok, True)
recs = [json.loads(l) for l in
        (broker.ORDER_DIR / f"{TODAY}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
for r in recs:
    print(f"    紀錄 {r['kind']:8} live={r['live']} dry_run={r.get('dry_run')} "
          f"{r.get('action')} {r.get('price')} {r.get('octype')}")
chk("  有記到進場與停利兩筆", [r["kind"] for r in recs], ["entry", "target"])
chk("  兩筆都標記 dry_run", all(r.get("dry_run") for r in recs), True)
chk("  停利掛在 45100（做多 +100）", recs[1]["price"], 45100)
chk("  dry run 不計入當日真實進場次數", broker.entries_today(), 0)

print("\n=== 對帳查不到 ≠ 沒成交（送出去了但問不到，絕不可以說沒成交）===")


class BlindAPI:
    """單送得出去，但之後每一次查詢部位都爆炸（網路抖、券商忙）。"""

    def __init__(self):
        self.sent = 0

    def place_order(self, contract, order):
        self.sent += 1
        return FakeTrade()

    def update_status(self, acc=None):
        pass

    def cancel_order(self, t):
        pass

    def list_positions(self, acc=None):
        raise RuntimeError("timeout")


blind = BlindAPI()
connect(blind)
broker.is_live = lambda: True
broker.FILL_WAIT = 1.2
ok, err, pos = broker.enter("long", 45000, 100)
chk("  不可以回報成功", ok, False)
chk("  要明講「不知道有沒有成交」", "不知道有沒有成交" in (err or ""), True)
chk("  要叫他自己去看部位", "大戶投" in (err or ""), True)
chk("  只送了進場那一張，沒有補掛停利（方向與價格都還不確定）", blind.sent, 1)
broker.is_live = lambda: broker.REAL_FLAG.exists()
broker._state["position"] = None
(broker.ORDER_DIR / f"{TODAY}.jsonl").unlink(missing_ok=True)

print("\n=== 停利掛不上去：進場成功，但一定要講出來 ===")


class NoTargetAPI:
    """進場撮得到，但停利那一張券商不收。"""

    def __init__(self):
        self.sent = 0

    def place_order(self, contract, order):
        self.sent += 1
        if str(getattr(order, "octype", "")).endswith("Cover"):
            raise RuntimeError("停利單被拒絕")
        return FakeTrade()

    def update_status(self, acc=None):
        pass

    def cancel_order(self, t):
        pass

    def list_positions(self, acc=None):
        return [type("P", (), {"code": "TMFI6", "quantity": 1,
                               "direction": "Action.Buy", "price": 45010})()]


connect(NoTargetAPI())
broker.is_live = lambda: True
broker.FILL_WAIT = 1.2
ok, err, pos = broker.enter("long", 45000, 100)
chk("  進場本身算成功（部位真的在）", ok, True)
chk("  但要回報停利沒掛上", "停利單沒掛上去" in (err or ""), True)
chk("  部位保留著（面板的停損還要靠它）", (pos or {}).get("dir"), "long")
chk("  snapshot 要照實說沒有停利單", broker.snapshot()["position"]["has_target"], False)
broker.is_live = lambda: broker.REAL_FLAG.exists()
broker._state["position"] = None
(broker.ORDER_DIR / f"{TODAY}.jsonl").unlink(missing_ok=True)

print("\n=== 對帳要獨立於 can_enter：達上限、憑證沒過，照樣要對帳 ===")


class CountingAPI:
    """只數「被查了幾次部位」。"""

    def __init__(self):
        self.asked = 0

    def place_order(self, contract, order):
        return FakeTrade()

    def update_status(self, acc=None):
        pass

    def cancel_order(self, t):
        pass

    def list_positions(self, acc=None):
        self.asked += 1
        return [type("P", (), {"code": "TMFI6", "quantity": 1,
                               "direction": "Action.Sell", "price": 46978})()]


counting = CountingAPI()
connect(counting)
broker.is_live = lambda: True
broker.CA_OK["ok"] = False                    # 憑證沒過 → can_enter 會在第一關就擋下來
broker._state["position"] = None
broker._LAST_RECONCILE["at"] = 0.0
ok, why = broker.can_enter(46978, True)
chk("  can_enter 的確被前面的關卡擋住了", ok, False)
chk("  它一次都沒去問券商（所以不能只靠它對帳）", counting.asked, 0)
broker.reconcile_tick()
chk("  reconcile_tick 有去問券商", counting.asked >= 1, True)
chk("  重啟後的部位撿得回來（停損才會繼續跑）",
    (broker._state["position"] or {}).get("dir"), "short")
asked_after = counting.asked
broker.reconcile_tick()
chk(f"  但要節流，不可以每 0.25 秒打一次（{broker.RECONCILE_EVERY:.0f} 秒一次）",
    counting.asked, asked_after)
broker.CA_OK["ok"] = True
broker.is_live = lambda: broker.REAL_FLAG.exists()
broker._state["position"] = None
(broker.ORDER_DIR / f"{TODAY}.jsonl").unlink(missing_ok=True)

print("\n=== 成績單：一趟來回要記得起來，而且問不到成交價時要留白 ===")
broker.TRADE_DIR = TMP / "real_trades"


class DealAPI:
    """平倉會成交，而且回報得出成交明細。"""

    def __init__(self, deal=None):
        self.deal = deal
        self.flat = False

    def place_order(self, contract, order):
        self.flat = True                       # 一送平倉單就變空手
        t = FakeTrade()
        if self.deal is not None:
            t.status = type("S", (), {"deals": [
                type("D", (), {"price": self.deal, "quantity": 1})()]})()
        return t

    def update_status(self, acc=None):
        pass

    def cancel_order(self, t):
        pass

    def list_positions(self, acc=None):
        if self.flat:
            return []
        return [type("P", (), {"code": "TMFI6", "quantity": 1,
                               "direction": "Action.Buy", "price": 47144})()]


def fresh_pos(direction="long", entry=47144):
    broker._state["position"] = {"dir": direction, "entry": float(entry), "qty": 1,
                                 "entry_time": "13:39:21", "target_trade": None,
                                 "recovered": False}


connect(DealAPI(deal=47166))
broker.is_live = lambda: True
broker.FILL_WAIT = 1.2
broker.realized_today = lambda: []          # 這一段不要去問券商損益，測的是自己記的那條路
fresh_pos()
ok, err = broker.close("manual")
chk("  平倉成功", ok, True)
tr = broker.trades_today()
chk("  成績單有一筆", len(tr), 1)
chk("  記到實際出場價（不是送單時的 0）", tr[0]["exit"], 47166.0)
chk("  算得出點數：做多 47144→47166 ＝ +22", tr[0]["points"], 22.0)
chk("  記得住是為什麼平的", tr[0]["reason"], "manual")

# 做空要反過來算 —— 又是一個「只測一邊會過」的地方
(broker.TRADE_DIR / f"{TODAY}.jsonl").unlink()
api2 = DealAPI(deal=47100)
api2.list_positions = lambda acc=None: ([] if api2.flat else [
    type("P", (), {"code": "TMFI6", "quantity": -1,
                   "direction": "Action.Sell", "price": 47144})()])
connect(api2)
broker.is_live = lambda: True
fresh_pos("short")
broker.close("sl")
tr = broker.trades_today()
chk("  做空 47144→47100 ＝ +44（不是 −44）", tr[0]["points"], 44.0)

# 問不到成交價：**留白，不可以拿別的價冒充**
(broker.TRADE_DIR / f"{TODAY}.jsonl").unlink()
connect(DealAPI(deal=None))
broker.is_live = lambda: True
fresh_pos()
broker.close("manual")
tr = broker.trades_today()
chk("  問不到成交價 → 出場價留白", tr[0]["exit"], None)
chk("  點數也跟著留白，不編一個數字", tr[0]["points"], None)

print("\n=== 停利在券商成交：面板沒送過單，也要記得起來 ===")
# 面板**永遠不送停利單**（那張掛在券商），所以停利成交時 close() 根本不會被呼叫。
# 部位是在對帳時「自己不見的」—— 不在那裡記一筆，這趟來回就完全不會進成績單。
(broker.TRADE_DIR / f"{TODAY}.jsonl").unlink()
tp_trade = FakeTrade()
tp_trade.status = type("S", (), {"deals": [
    type("D", (), {"price": 47244, "quantity": 1})()]})()
connect(FakeAPI())                          # FakeAPI 回報「券商沒有部位」
broker.is_live = lambda: True
broker._state["position"] = {"dir": "long", "entry": 47144.0, "qty": 1,
                             "entry_time": "13:39:21", "target_trade": tp_trade,
                             "recovered": False}
broker.reconcile()
tr = broker.trades_today()
chk("  部位自己不見了也要留下紀錄", len(tr), 1)
chk("  出場價 = 停利單的成交價", tr[0]["exit"], 47244.0)
chk("  +100 點", tr[0]["points"], 100.0)
chk("  理由標成停利", tr[0]["reason"], "tp")
broker.is_live = lambda: broker.REAL_FLAG.exists()
broker._state["position"] = None

shutil.rmtree(TMP, ignore_errors=True)
print("\n總結:", "全部通過" if not FAIL else f"{FAIL} 項失敗")
sys.exit(1 if FAIL else 0)
