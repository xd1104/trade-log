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

shutil.rmtree(TMP, ignore_errors=True)
print("\n總結:", "全部通過" if not FAIL else f"{FAIL} 項失敗")
sys.exit(1 if FAIL else 0)
