# -*- coding: utf-8 -*-
"""
auto_fire.py 的離線測試 —— **自動下單**的安全網。

**不連永豐、不送任何單。** 全程用假券商，而且把每一個會寫檔的地方都導到暫存區
（收尾有一節斷言「全程沒有指回真的資料夾」）。

⛔⛔ 這支要證明的是**兩件事**，只證明第一件不算數：
  1. **關著的時候一張單都不會出去** —— 用「一被呼叫就 raise 的假券商」，
     ⛔ 不是只看回傳值。
  2. **打開之後送出去的是對的** —— 方向、1 口、MKP+IOC、New、
     停利價 = **實際成交價** ±100。**做多與做空各驗一輪**
     （CLAUDE.md：只測一個方向等於沒測；2026-09-01 就是只跑做多，
      漏掉「方向永遠回報做多」那個會賠錢的 bug）。

怎麼跑（PowerShell）：
    $env:PYTHONIOENCODING="utf-8"; $env:PYTHONUTF8="1"
    & "…\\trade-log\\.venv\\Scripts\\python.exe" "…\\tools\\shioaji\\test_auto_fire.py"

⚠️ 改過 auto_fire.py／broker.py／live_panel.py 的接線就要重跑。
⚠️ 環境變數 `AF_SRC_DIR` 是給突變測試用的（`tools/probe/fire-mutate.py`）：
   指到一份**放在暫存區**的 auto_fire.py，⛔ 絕對不會把壞版本寫進 tools/shioaji
   （看門狗是活的，磁碟上有壞版本就會被載進他正在跑的面板）。
"""
import ast
import datetime
import json
import os
import pathlib
import queue

import shutil
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_MUT = os.environ.get("AF_SRC_DIR")
if _MUT:
    sys.path.insert(0, _MUT)          # 突變版的 auto_fire.py（在暫存區）
# ⚠️ 2026-09-09 加：`live_panel.py` 那一半也要能被突變（`AUTO["eod"] = True`、
#    13:43:30 的時鐘防護、那顆「關閉」鈕的顯示條件 —— 這幾條都住在 live_panel）。
#    lab-qa 的 Q11／Q9 就是打在這裡而**整組打不紅**：舊版這支只吃 AF_SRC_DIR。
#    ⛔ 同樣只讀暫存區的那一份，絕不把壞版本寫進 tools/shioaji。
_MUT_LP = os.environ.get("LP_SRC_DIR")
if _MUT_LP:
    sys.path.insert(0, _MUT_LP)       # 突變版的 live_panel.py（在暫存區）

import broker                       # noqa: E402
import auto_fire as AF              # noqa: E402
import live_panel as LP             # noqa: E402
import shioaji as sj                # noqa: E402

TMP = pathlib.Path(tempfile.mkdtemp(prefix="autofire-test-"))
# ⛔ 每一個會寫檔／讀開關的地方都要導到暫存區。**漏掉一個就是污染他的真實紀錄**
#    （2026-09-01 踩過：test_broker 漏了 TRADE_DIR，他的成績單多了 6 筆假交易）。
REAL_PATHS = {"broker.ORDER_DIR": broker.ORDER_DIR, "broker.TRADE_DIR": broker.TRADE_DIR,
              "broker.REAL_FLAG": broker.REAL_FLAG, "AF.ARM_FLAG": AF.ARM_FLAG,
              "AF.FIRE_DIR": AF.FIRE_DIR, "LP.AUTO_DIR": LP.AUTO_DIR,
              "LP.AUTO_REAL_DIR": LP.AUTO_REAL_DIR}
broker.ORDER_DIR = TMP / "real_orders"
broker.TRADE_DIR = TMP / "real_trades"
broker.REAL_FLAG = TMP / "REAL_ORDERS_ON"          # 不存在 → dry run
AF.ARM_FLAG = TMP / "AUTO_ORDERS_ON"               # 不存在 → 自動下單關著
AF.FIRE_DIR = TMP / "autofire"
LP.AUTO_DIR = TMP / "autotest"                     # ⛔ 不准碰他真的模擬紀錄
LP.AUTO_REAL_DIR = TMP / "real_trades"

TODAY = datetime.date.today()
DAY = str(TODAY)
FAIL = 0
# ⛔ 價格一律 12000 附近（沿用 autotest_synth 的規矩）——
#    46xxx/47xxx 會撞到他的真實紀錄，那是這個 repo 的洩漏紅線。
PX = 12010.0
OPEN845 = 12000.0
P0900 = 12005.0


def chk(name, got, want):
    global FAIL
    ok = got == want
    FAIL += not ok
    print(("  OK   " if ok else "  FAIL ") + name
          + ("" if ok else f"  (得到 {got!r}，期待 {want!r})"))


def say(cond, name, extra=""):
    global FAIL
    FAIL += not cond
    print(("  OK   " if cond else "  FAIL ") + name + (f"  {extra}" if extra else ""))


def _boom(kind, err, tb):
    """
    ⛔ 這支自己掛掉的時候要**印出一項具名的 FAIL ＋ 總結行**。
    突變測試只數 FAIL 行的話，「突變讓測試當場掛掉」會 0 行 FAIL ⇒ 被記成「打不紅」
    （CLAUDE.md 記過這個假綠燈，而且第一版只做在前端、後端那支根本沒裝）。
    """
    import traceback
    traceback.print_exception(kind, err, tb)
    print("  FAIL ⛔ 測試自己掛掉了（未捕捉的例外）：" + str(err)[:120])
    print("⛔ 有 ? 項沒過（測試中斷）")
    sys.stdout.flush()


sys.excepthook = _boom


# ---------------------------------------------------------------- 假券商

SENT = {"n": 0}


class FakeTrade:
    order = type("O", (), {"id": "T0000001"})()
    status = type("S", (), {"status": "Submitted", "deals": []})()


class ExplodeAPI:
    """
    ⛔ **一被呼叫 place_order 就爆炸。** 這個假券商是給「不該送單」的每一種情境用的：
       回傳值看起來對，但單其實送出去了 —— 那是最危險的假綠燈。
    """

    def place_order(self, contract, order):
        SENT["n"] += 1
        raise AssertionError("⛔ 不該送單的情境竟然真的呼叫了 place_order！")

    def list_positions(self, acc=None):
        return []

    def update_status(self, acc=None):
        pass

    def cancel_order(self, t):
        pass

    def list_profit_loss(self, acc=None, d1=None, d2=None):
        # ⛔ 真的永豐 API 有這個方法。假物件沒有 ⇒ realized_today() 會變成「問不到」，
        #    整節測試就會在**問不到**的狀態下跑（M1 當初就是被這件事蓋住的）。
        return []


class BadAPI(ExplodeAPI):
    """對帳（部位）問不到（網路抖）—— can_enter 要擋下來。

    ⚠️ **已實現損益那一支照樣答得出來**：這個假物件要測的是「部位對不到帳」，
       不是「什麼都問不到」。兩件事要分開量（前者 `eod_unknown`、
       後者 `eod_cant_tell`）—— 混在一起的話，收盤那一段永遠走不到第③道。
    """

    def list_positions(self, acc=None):
        raise RuntimeError("網路斷了")


class HasPosAPI(ExplodeAPI):
    """券商已經有部位 —— can_enter 要擋下來。"""

    def list_positions(self, acc=None):
        return [type("P", (), {"code": "TMFI6", "quantity": 1,
                               "direction": "Buy", "price": PX})()]


class SimAPI:
    """
    會成交的假券商（離線，⛔ 不連任何網路；模擬帳戶那條路的離線替身）。

    ⚠️ 假券商一定要模擬「部位真的會出現／消失」：永遠回報空手的話，
       `_wait_fill()` 會判定沒成交、整條停利那半就測不到（2026-09-01 連兩次栽在假物件上）。
    """

    def __init__(self, side, fill):
        self.side = side              # 券商回報的方向字串："Buy" / "Sell"
        self.fill = fill              # 實際成交價（⛔ 故意跟送單當下的參考價不一樣）
        self.orders = []
        self.flat = False
        self.opened = False        # ⛔ 送出新倉之前一定要回報「空手」——
        #    一開始就說有部位的話，can_enter 會擋在「已經有部位了」，
        #    整個進場那半根本測不到（假物件沒做好 ⇒ 測試紅的是假物件不是產品）。

    def place_order(self, contract, order):
        SENT["n"] += 1
        self.orders.append(order)
        if str(order.octype) == "FuturesOCType.New":
            self.opened = True
        if (str(order.octype) == "FuturesOCType.Cover"
                and str(order.price_type) == "FuturesPriceType.MKP"):
            self.flat = True
        return FakeTrade()

    def list_positions(self, acc=None):
        if self.flat or not self.opened:
            return []
        return [type("P", (), {"code": "TMFI6", "quantity": 1,
                               "direction": self.side, "price": self.fill})()]

    def update_status(self, acc=None):
        pass

    def cancel_order(self, t):
        pass

    def list_profit_loss(self, acc=None, d1=None, d2=None):
        """
        ⛔⛔ **這個方法一定要有**（2026-09-09 lab-qa 退件 M1 的假物件那一半）：
        真的永豐 API 有它，假券商沒有的話 `broker.realized_today()` 會吃到
        AttributeError ⇒ 「問不到」。舊版 broker 把例外吞掉、靜靜回空陣列，
        所以整節測試一直在**問不到**的狀態下綠燈 —— 假物件把 M1 蓋住了。
        回空陣列 ＝ 「券商說今天沒有已實現損益」，那是一個**確定的答案**。
        """
        return list(getattr(self, "pnl_rows", []))


FAKE_ACC = sj.Account(account_type=sj.AccountType.Future, person_id="TESTPID",
                      broker_id="F000000", account_id="0000000", signed=True,
                      username="test")


def connect(api):
    broker._state["api"] = api
    broker._state["account"] = FAKE_ACC
    broker._state["contract"] = type("C", (), {"code": "TMFI6"})()
    broker._state["position"] = None


class FakeToday:
    """`Today` 的替身：`_auto_snap()` 只讀這幾個屬性。"""

    def __init__(self, px=PX, open845=OPEN845, p900=P0900, age=0.2, is_mid=False):
        self.price = px
        self.bid = None if px is None else px - 1
        self.ask = None if px is None else px + 1
        self.price_is_mid = is_mid
        self.last_recv = None if age is None else time.time() - age
        self.open = open845
        self.prev_close = 11990.0
        self.high = 12020.0
        self.low = 11995.0
        self.minute_bar = {} if p900 is None else {540: {"o": p900}}
        self.minute_close = {}


# ---------------------------------------------------------------- 治具

def arm_write(text):
    AF.ARM_FLAG.write_text(text, encoding="utf-8")


def arm_clear():
    if AF.ARM_FLAG.exists():
        AF.ARM_FLAG.unlink()


def live_on():
    broker.REAL_FLAG.parent.mkdir(parents=True, exist_ok=True)
    broker.REAL_FLAG.write_text("on", encoding="utf-8")


def live_off():
    if broker.REAL_FLAG.exists():
        broker.REAL_FLAG.unlink()


def reset(keep_orders=False):
    """每一個情境之間把狀態歸零。⛔ 量之前把狀態歸零（CLAUDE.md 的通則）。"""
    if AF.FIRE_DIR.exists():
        shutil.rmtree(AF.FIRE_DIR)
    if not keep_orders and broker.ORDER_DIR.exists():
        shutil.rmtree(broker.ORDER_DIR)
    broker._state["position"] = None
    broker._close_fail["at"] = 0.0
    broker._LAST_RECONCILE["at"] = 0.0
    AF._ST["err"] = None
    AF._ST["err_n"] = 0
    AF._ST["last"] = None
    SENT["n"] = 0
    LP.AUTO["eod"] = False
    while not AF._Q.empty():
        AF._Q.get()
    while not AF._EQ.empty():
        AF._EQ.get()
    while not LP._AUTO_Q.empty():
        LP._AUTO_Q.get()


def reset_eod():
    """收盤平倉那一節額外要清的：成績單（`_already_closed` 會去讀它）。"""
    reset()
    if broker.TRADE_DIR.exists():
        shutil.rmtree(broker.TRADE_DIR)
    broker._REALIZED["at"] = 0.0
    broker._REALIZED["rows"] = []


def wire():
    """跟 live_panel.main() 一模一樣的接線（⛔ 常數與算式的正本都在 live_panel）。"""
    AF.configure(signal_at=LP.SIGNAL_AT, signal_sec=LP.SIGNAL_SEC,
                 late_ms=LP.AUTO_LATE_MS, gap_s=LP.AUTO_GAP_S, tp_points=LP.TP_POINTS,
                 sig_fn=LP.auto_sig, dirs_fn=LP.auto_dirs, eod_at=LP.EOD_CLOSE_AT)
    LP.AUTO_SIG_HOOK = AF.on_signal
    LP.AUTO_EOD_HOOK = AF.on_eod


def run_signal(st, hh=9, mm=3, ss=30, ms=100):
    """
    走**完整的一條路**：4Hz 主迴圈跨過 09:03:30 → `_auto_tick` → 掛勾 → 佇列 → 送單。

    ⚠️ 佇列在這裡是同步排掉的（測試要可重現）；`start()` 那條真的執行緒另有一節在驗。
    """
    LP.AUTO["started"] = True
    LP.AUTO.update({"day": DAY, "done": False, "settled": True, "gaps": 0.0})
    now = datetime.datetime.combine(TODAY, datetime.time(hh, mm, ss, ms * 1000))
    LP._auto_tick(st, now, "day")
    while not AF._Q.empty():
        AF._fire(*AF._Q.get())
    while not LP._AUTO_Q.empty():
        LP._AUTO_Q.get()


def run_eod(hh=13, mm=43, ss=30):
    """
    走**完整的一條路**：4Hz 主迴圈跨過 13:43:30 → `_auto_tick` → 掛勾 → 佇列 → 平倉。

    ⚠️ `done=True` 是為了不讓同一次 tick 順手觸發 09:03:30 那一段
       （這一節測的是收盤平倉，兩件事要分開量）。
    """
    LP.AUTO["started"] = True
    LP.AUTO.update({"day": DAY, "done": True, "settled": True, "eod": False,
                    "gaps": 0.0})
    now = datetime.datetime.combine(TODAY, datetime.time(hh, mm, ss, 100000))
    LP._auto_tick(None, now, "day")
    n = 0
    while not AF._EQ.empty():
        AF._eod(*AF._EQ.get())
        n += 1
    while not LP._AUTO_Q.empty():
        LP._AUTO_Q.get()
    return n


def eod_row():
    """今天那一列收盤平倉的紀錄（沒有就 None）。"""
    for r in reversed(rows()):
        if r.get("rec") == "eod":
            return r
    return None


def rows():
    p = AF.FIRE_DIR / (DAY[:7] + ".jsonl")
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def merged():
    d, _led = AF.read_all()
    return (d or [{}])[0] if d else {}


def sent_orders(api):
    return [{"action": str(o.action), "price": o.price, "qty": o.quantity,
             "price_type": str(o.price_type), "order_type": str(o.order_type),
             "octype": str(o.octype)} for o in api.orders]


# ══ ① 開關：檔案存在才做事，內容不對就拒絕並講出原因 ════════════════════
print("=== ① 開關（AUTO_ORDERS_ON）===")
arm_clear()
a = AF.arm()
chk("  檔案不存在 → 關著", (a["on"], a["why"]), (False, "off"))
for txt, want in (("A", "A"), ("B", "B"), ("a\n", "A"), ("  b  \r\n", "B")):
    arm_write(txt)
    a = AF.arm()
    chk(f"  內容 {txt!r} → 開，做法 {want}", (a["on"], a["method"]), (True, want))
for txt in ("", "   \n", "C", "D", "AB", "A B", "A,B", "5 分 K", "1", "on", "\x00\xff"):
    arm_write(txt)
    a = AF.arm()
    say(a["on"] is False and a["why"] == "bad_method" and len(a["msg"]) > 8,
        f"  內容 {txt!r} → 拒絕下單，而且講得出原因", a["msg"][:46])
arm_write("C")
say("C" in AF.arm()["msg"] and "A" in AF.arm()["msg"] and "B" in AF.arm()["msg"],
    "  C 的訊息要明說「只支援 A 與 B」，⛔ 不可以只說「看不懂」")
arm_clear()
chk("  ⛔ 只支援兩種做法（C／D 不做成可下單的選項）", list(AF.METHODS), ["A", "B"])
say(len(set(AF.WHY.values())) == len(AF.WHY),
    "  ⛔ 每一種原因都有自己的一句話（兩個不同的原因不准寫同一句）",
    f"{len(AF.WHY)} 種")

# ══ ② 常數：不准自己另開一份（正本在 live_panel.py）═══════════════════
print("\n=== ② 常數的正本只有一份 ===")
wire()
chk("  訊號時刻跟模擬那一頁同一個", AF._CFG["signal_at"], LP.SIGNAL_AT)
chk("  停利點數跟他真的在用的規則同一組", AF._CFG["tp"], LP.TP_POINTS)
chk("  報價新鮮度門檻同一個", AF._CFG["gap_s"], LP.AUTO_GAP_S)
chk("  訊號算式用的是 live_panel 的正本", AF._CFG["sig_fn"], LP.auto_sig)
chk("  方向算式用的是 live_panel 的正本", AF._CFG["dirs_fn"], LP.auto_dirs)
chk("  模擬與自動下單算出來的訊號逐字相同",
    LP.auto_sig(PX, OPEN845, P0900), (PX - P0900, PX - OPEN845))

# ══ ③ ⛔ 關著：走完整條路，place_order 一次都沒被呼叫 ══════════════════
print("\n=== ③ ⛔ 關著的時候一張單都不會出去（假券商一被呼叫就 raise）===")
reset()
arm_clear()
live_off()
connect(ExplodeAPI())
run_signal(FakeToday())
chk("  place_order 被呼叫 0 次", SENT["n"], 0)
chk("  ⛔ 委託單 log 一列都沒有", broker.ORDER_DIR.exists(), False)
chk("  broker 沒有留下任何部位", broker._state["position"], None)
r = merged()
chk("  但是有留下一列「沒送」", (r.get("rec"), r.get("why")), ("skip", "off"))
say(len(r.get("why_msg") or "") > 6, "  而且講得出原因（畫面看得到）", r.get("why_msg"))

print("\n  ── 開關開著、但真單開關關著（dry run）──")
reset()
arm_write("B")
live_off()
connect(ExplodeAPI())
run_signal(FakeToday())
chk("  place_order 被呼叫 0 次", SENT["n"], 0)
r = merged()
chk("  這一天有走完整條路（送出去了，只是 dry run）", (r.get("rec"), r.get("ok")),
    ("result", True))
chk("  ⛔ 紀錄上明講這不是真單", r.get("live"), False)
chk("  ⛔ 受 REAL_ORDERS_ON 管：broker 說不是真的", broker.is_live(), False)
recs = [json.loads(x) for x in
        (broker.ORDER_DIR / f"{TODAY}.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
say(all(x.get("dry_run") for x in recs if x.get("kind") in ("entry", "target")),
    "  委託單 log 上每一列都標著 dry_run")
say(any(x.get("kind") == "entry" for x in recs) and any(x.get("kind") == "target" for x in recs),
    "  整條路真的跑完了（進場單 ＋ 停利單都組出來了）")

# ══ ④ ⛔ 打開＋會成交的假券商：送出去的內容要對（做多／做空各一輪）══════
print("\n=== ④ 打開之後送出去的是對的 —— ⛔ 做多與做空各一輪 ===")
broker.CA_OK["ok"] = True


def one_round(method, st, side, fill, want_action, want_tp_action, want_tp):
    reset()
    arm_write(method)
    live_on()
    api = SimAPI(side, fill)
    connect(api)
    run_signal(st)
    return api


print("\n  ── 做多（開盤起：08:45 → 09:03:30 上漲）──")
api = one_round("B", FakeToday(px=12010.0, open845=12000.0, p900=12005.0),
                "Buy", 12013.0, None, None, None)
o = sent_orders(api)
chk("  送出 2 張（進場 ＋ 停利），⛔ 不多不少", len(o), 2)
chk("  進場方向 Buy", o[0]["action"], "Action.Buy")
chk("  ⛔ 口數寫死 1 口", o[0]["qty"], 1)
chk("  進場價格型態 MKP（範圍市價）", o[0]["price_type"], "FuturesPriceType.MKP")
chk("  進場委託條件 IOC", o[0]["order_type"], "OrderType.IOC")
chk("  進場是新倉 New", o[0]["octype"], "FuturesOCType.New")
chk("  停利方向 Sell（做多的反向）", o[1]["action"], "Action.Sell")
chk("  停利是限價 LMT", o[1]["price_type"], "FuturesPriceType.LMT")
chk("  停利是 ROD", o[1]["order_type"], "OrderType.ROD")
chk("  停利是平倉 Cover", o[1]["octype"], "FuturesOCType.Cover")
chk("  ⛔ 停利價 = **實際成交價** 12013 +100 = 12113（不是參考價 12010+100）",
    o[1]["price"], 12113.0)
chk("  停利口數 1", o[1]["qty"], 1)
r = merged()
chk("  紀錄：做法＝開盤起", r.get("method"), "B")
chk("  紀錄：方向＝做多", r.get("dir"), "long")
chk("  紀錄：進場價＝實際成交價", r.get("entry"), 12013.0)
chk("  紀錄：停利價", r.get("tp"), 12113.0)
chk("  紀錄：滑價＝成交 − 09:03:30 的價", r.get("slip"), 3.0)
chk("  紀錄：這是真單", r.get("live"), True)
chk("  紀錄：停利真的掛上去了", r.get("has_target"), True)
chk("  紀錄：口數", r.get("qty"), 1)
# ⛔ 落地一定是 append：**先寫「要送了」再送單**，送到一半當掉才不會重送。
#    寫成覆寫的話兩列會變一列，而畫面上完全看不出來。
raw = rows()
chk("  ⛔ 檔案裡是兩列（先 sending 再 result），不是被覆寫成一列", len(raw), 2)
chk("  ⛔ 而且 sending 那一列在前面（先落地再送單）",
    [x.get("stage") for x in raw], ["sending", "done"])

print("\n  ── 做空（開盤起：08:45 → 09:03:30 下跌）──")
api = one_round("B", FakeToday(px=11990.0, open845=12000.0, p900=11995.0),
                "Sell", 11987.0, None, None, None)
o = sent_orders(api)
chk("  送出 2 張（進場 ＋ 停利）", len(o), 2)
chk("  ⛔ 進場方向 Sell（做空）", o[0]["action"], "Action.Sell")
chk("  ⛔ 口數 1 口", o[0]["qty"], 1)
chk("  進場 MKP", o[0]["price_type"], "FuturesPriceType.MKP")
chk("  進場 IOC", o[0]["order_type"], "OrderType.IOC")
chk("  進場 New", o[0]["octype"], "FuturesOCType.New")
chk("  ⛔ 停利方向 Buy（做空的反向）—— 送 Sell 等於再加一口空單",
    o[1]["action"], "Action.Buy")
chk("  ⛔ 停利價 = 實際成交價 11987 −100 = 11887", o[1]["price"], 11887.0)
chk("  停利是 LMT / ROD / Cover",
    (o[1]["price_type"], o[1]["order_type"], o[1]["octype"]),
    ("FuturesPriceType.LMT", "OrderType.ROD", "FuturesOCType.Cover"))
r = merged()
chk("  紀錄：方向＝做空", r.get("dir"), "short")
chk("  紀錄：進場價＝實際成交價", r.get("entry"), 11987.0)
chk("  紀錄：停利價", r.get("tp"), 11887.0)
chk("  紀錄：滑價（做空，成交比參考價低 3 點＝賺 3 點）", r.get("slip"), 3.0)

print("\n  ── 做法 A（5 分 K）跟做法 B 判出不同方向時，送出去的要跟開關講的一致 ──")
# 09:00 之後上漲、但 08:45 到現在是跌 ⇒ A 做多、B 做空
ST_SPLIT = dict(px=11990.0, open845=12000.0, p900=11980.0)
chk("  這一組資料 A 與 B 真的不同向",
    (LP.auto_dirs(*LP.auto_sig(ST_SPLIT["px"], ST_SPLIT["open845"], ST_SPLIT["p900"]))["A"],
     LP.auto_dirs(*LP.auto_sig(ST_SPLIT["px"], ST_SPLIT["open845"], ST_SPLIT["p900"]))["B"]),
    (1, -1))
api = one_round("A", FakeToday(**ST_SPLIT), "Buy", 11991.0, None, None, None)
chk("  開關寫 A ⇒ 送出的是 Buy（做多）", sent_orders(api)[0]["action"], "Action.Buy")
chk("  ⛔ 只送一次進場（一天一口，不是兩個做法各一口）",
    len([x for x in sent_orders(api) if x["octype"] == "FuturesOCType.New"]), 1)
api = one_round("B", FakeToday(**ST_SPLIT), "Sell", 11989.0, None, None, None)
chk("  開關寫 B ⇒ 送出的是 Sell（做空）", sent_orders(api)[0]["action"], "Action.Sell")
chk("  ⛔ 只送一次進場", len([x for x in sent_orders(api)
                              if x["octype"] == "FuturesOCType.New"]), 1)

# ══ ⑤ 每一種「不送」都要真的沒送（假券商一被呼叫就 raise）═══════════════
print("\n=== ⑤ 這些情況一律不送，而且每一種都要有原因 ===")


def no_send(name, why, setup, st=None, api=None, live=True, no_api=False):
    reset()
    if live:
        live_on()
    else:
        live_off()
    setup()
    connect(api or ExplodeAPI())
    if no_api:
        broker._state["api"] = None          # 還沒連上永豐
    run_signal(st or FakeToday())
    r = merged()
    got = (SENT["n"], r.get("rec"), r.get("why"))
    ok = got == (0, "skip", why) or (got[0] == 0 and got[1] == "result"
                                     and r.get("ok") is False and got[2] == why)
    say(ok, f"  {name} → 不送（{why}）", f"place_order {SENT['n']} 次，紀錄 {got[1:]}")
    say(len(r.get("why_msg") or "") > 6, f"    …而且講得出原因", (r.get("why_msg") or "")[:52])
    return r


no_send("開關檔不存在", "off", arm_clear)
no_send("開關內容不合法（C）", "bad_method", lambda: arm_write("C"))
no_send("開關內容不合法（空的）", "bad_method", lambda: arm_write(""))
no_send("09:03:30 收不到成交價", "no_quote", lambda: arm_write("B"),
        st=FakeToday(px=None))
no_send("報價中斷（超過 5 秒沒更新）", "quote_stale", lambda: arm_write("B"),
        st=FakeToday(age=30.0))
no_send("從來沒收到過報價", "quote_stale", lambda: arm_write("B"),
        st=FakeToday(age=None))
no_send("只有中價、還沒有成交", "mid_only", lambda: arm_write("B"),
        st=FakeToday(is_mid=True))
no_send("算不出訊號（拿不到 08:45 開盤）", "no_signal", lambda: arm_write("B"),
        st=FakeToday(open845=None))
no_send("算不出訊號（拿不到 09:00 的價，做法 A）", "no_signal", lambda: arm_write("A"),
        st=FakeToday(p900=None))
no_send("還沒連上永豐", "cant_enter", lambda: arm_write("B"), no_api=True)
no_send("跟券商對帳失敗", "cant_enter", lambda: arm_write("B"), api=BadAPI())
no_send("券商已經有部位", "cant_enter", lambda: arm_write("B"), api=HasPosAPI())
no_send("憑證沒啟用", "cant_enter",
        lambda: (arm_write("B"), broker.CA_OK.update({"ok": False, "msg": "憑證沒啟用"}))[0])
broker.CA_OK["ok"] = True


def _fill_limit():
    arm_write("B")
    broker.ORDER_DIR.mkdir(parents=True, exist_ok=True)
    with (broker.ORDER_DIR / f"{TODAY}.jsonl").open("a", encoding="utf-8") as f:
        for _ in range(broker.MAX_ENTRIES):
            f.write(json.dumps({"kind": "entry", "live": True, "ok": True}) + "\n")


reset()
live_on()
_fill_limit()
connect(ExplodeAPI())
run_signal(FakeToday())
r = merged()
say(SENT["n"] == 0 and r.get("why") == "cant_enter" and "上限" in (r.get("why_msg") or ""),
    "  當天已達進場上限 → 不送（cant_enter）", (r.get("why_msg") or "")[:60])

print("\n  ── 面板 09:03:30 沒開著／剛啟動 ⇒ 那天跳過，⛔ 不補單 ──")
reset()
arm_write("B")
live_on()
connect(ExplodeAPI())
run_signal(FakeToday(), hh=9, mm=10, ss=0)      # 面板 09:10 才開起來
r = merged()
chk("  place_order 0 次", SENT["n"], 0)
chk("  紀錄是 late", (r.get("rec"), r.get("why")), ("skip", "late"))
say("不補單" in (r.get("why_msg") or ""), "  而且明講「不補單」", r.get("why_msg"))
say(rows() and all(x.get("rec") == "skip" for x in rows()),
    "  ⛔ 那天檔案裡只有一列 skip，沒有任何 fire")

print("\n  ── 排隊排太久（主迴圈準時、但送單那一段卡住）⇒ 不送 ──")
reset()
arm_write("B")
live_on()
connect(ExplodeAPI())
st_late = FakeToday()
LP.AUTO["started"] = True
LP.AUTO.update({"day": DAY, "done": False, "settled": True, "gaps": 0.0})
now = datetime.datetime.combine(TODAY, datetime.time(9, 3, 30, 100000))
LP._auto_tick(st_late, now, "day")
snap, dd, lag, _put_at = AF._Q.get()
AF._fire(snap, dd, lag, time.time() - 30)       # 排到我的時候已經晚了 30 秒
r = merged()
chk("  place_order 0 次", SENT["n"], 0)
chk("  紀錄是 late", (r.get("rec"), r.get("why")), ("skip", "late"))
say("30" in (r.get("why_msg") or ""), "  而且把實際晚了幾秒寫出來",
    r.get("why_msg"))
while not LP._AUTO_Q.empty():
    LP._AUTO_Q.get()

print("\n  ── 沒有接線（configure 沒跑）⇒ 不送 ──")
reset()
arm_write("B")
live_on()
connect(ExplodeAPI())
AF._ST["wired"] = False
run_signal(FakeToday())
chk("  place_order 0 次", SENT["n"], 0)
chk("  紀錄是 not_wired", merged().get("why"), "not_wired")
wire()

print("\n  ── 那天已經送過了（一天 1 次）──")
reset()
arm_write("B")
live_on()
api = SimAPI("Buy", 12013.0)
connect(api)
run_signal(FakeToday())
n1 = len(api.orders)
before = len(rows())
broker._state["position"] = None          # 假裝停利成交了，部位不見了
run_signal(FakeToday())                   # 同一天再跑一次（看門狗重啟就是這樣）
chk("  第二次一張單都沒送", len(api.orders), n1)
chk("  ⛔ 也沒有再寫任何一列", len(rows()), before)
chk("  第一次真的有送", n1, 2)

print("\n  ── 送到一半當掉：重啟後 ⛔ 不可以再送一次 ──")
reset()
arm_write("B")
live_on()
api = SimAPI("Buy", 12013.0)
connect(api)
# 只留下「決定送單」那一列（stage=sending），模擬 place_order 之後、寫結果之前當掉
AF.FIRE_DIR.mkdir(parents=True, exist_ok=True)
with (AF.FIRE_DIR / (DAY[:7] + ".jsonl")).open("a", encoding="utf-8") as f:
    f.write(json.dumps({"rec": "fire", "stage": "sending", "date": DAY,
                        "method": "B", "dir": "long"}, ensure_ascii=False) + "\n")
run_signal(FakeToday())
chk("  ⛔ 一張單都沒送", len(api.orders), 0)
d, _l = AF.read_all()
chk("  那一天標成「不知道下場」", d[0].get("why"), "crashed")
say("大戶投" in (d[0].get("why_msg") or ""), "  而且叫他自己去確認部位",
    d[0].get("why_msg"))

# ══ ⑥ 帳本：每一列都要有去處 ═══════════════════════════════════════════
print("\n=== ⑥ 帳本（每一列都要有去處）===")
reset()
arm_write("B")
live_on()
connect(SimAPI("Buy", 12013.0))
run_signal(FakeToday())
with (AF.FIRE_DIR / (DAY[:7] + ".jsonl")).open("a", encoding="utf-8") as f:
    f.write("{ 這一列不是 json\n")
    f.write(json.dumps({"rec": "sig", "date": DAY}) + "\n")       # 不認得的 rec
d, led = AF.read_all()
chk("  fire + result + skip + eod + bad ＝ 檔案總列數",
    led["fire"] + led["result"] + led["skip"] + led["eod"] + led["bad"], led["total"])
chk("  讀不出來的算進 bad，⛔ 不准弄掉一整天", led["bad"], 2)
chk("  ⛔ 一列壞資料不准弄掉那一天", len(d), 1)
chk("  那一天照樣是「送出去了」", (d[0].get("rec"), d[0].get("ok")), ("result", True))

# ══ ⑦ 主迴圈那一段：⛔ 只能 put_nowait、永遠不丟例外 ═══════════════════
print("\n=== ⑦ on_signal 跑在 4Hz 主迴圈上（那條迴圈就是他的停損）===")
src = pathlib.Path(AF.__file__).read_text(encoding="utf-8")
tree = ast.parse(src)
fn = {n.name: n for n in ast.walk(tree)
      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
calls = sorted({(n.func.attr if isinstance(n.func, ast.Attribute) else
                 getattr(n.func, "id", "?"))
                for n in ast.walk(fn["on_signal"]) if isinstance(n, ast.Call)})
say("put_nowait" in calls, "  on_signal 有用 put_nowait", calls)
say(not any(c in calls for c in ("open", "read_text", "write_text", "sleep",
                                 "put", "get", "enter", "can_enter", "acquire")),
    "  ⛔ on_signal 裡沒有 I/O／阻塞式 put／鎖／下單", calls)
AF._Q.queue.clear()
for _ in range(AF.QUEUE_MAX):
    AF._Q.put_nowait(("填滿", DAY, 0, 0.0))     # 直接塞爆（不經過產品程式）


def call_while_full(n):
    """
    ⛔ 佇列滿的時候呼叫 `on_signal` **一定要立刻回來**。
    ⚠️ 這裡刻意開一條 daemon 執行緒去呼叫：改成阻塞式 `put` 的話，
       直接呼叫會讓**整支測試永遠掛住**（一行 FAIL 都印不出來 ＝ 假綠燈，
       而且突變測試會等到天荒地老）。掛住就讓它掛在那條執行緒上，主線照樣走完。
    """
    done = []

    def go():
        for _ in range(n):
            AF.on_signal({"px": 1}, DAY, 0)
        done.append(True)

    th = __import__("threading").Thread(target=go, daemon=True)
    th.start()
    th.join(2.0)
    return bool(done)


say(call_while_full(3), "  佇列滿了也不會卡住主迴圈（⛔ 非阻塞，也沒有丟例外）")
say(AF._ST["err_n"] >= 1 and AF._ST["err"],
    "  ⛔ 但不可以安靜地吞：有計數 ＋ 有訊息（畫面上看得到）", AF._ST["err"])
t0 = time.time()
ok200 = call_while_full(200)
say(ok200 and (time.time() - t0) < 1.0, "  塞爆之後呼叫 200 次仍然很快回來",
    f"{(time.time() - t0) * 1000:.1f} ms")
say("佇列" in (AF._ST["err"] or ""), "  而且那句話講得出是什麼事（⛔ 不是一句空的）",
    AF._ST["err"])
AF._Q.queue.clear()
reset()
AF._ST["err_n"] = 0
prev_fire = AF._fire
AF._fire = lambda *a: (_ for _ in ()).throw(RuntimeError("測試故意炸的"))
AF.start()
AF.on_signal({"px": 1}, DAY, 0)
time.sleep(0.4)
say(AF._ST["err_n"] >= 1, "  送單執行緒出錯時：有計數（⛔ 不可以安靜地吞）",
    AF._ST["err"])
AF._fire = prev_fire
say(AF._ST["started"] is True, "  送單執行緒起得來")

# ══ ⑧ 接線（AST）：⛔ 只有 main() 會把掛勾接上去 ═══════════════════════
print("\n=== ⑧ 接線：⛔ 掛勾預設是 no-op，只有 main() 會接 ===")
LPSRC = pathlib.Path(LP.__file__).read_text(encoding="utf-8")


def wiring_fails(text):
    """
    對 live_panel 的**原始碼字串**做接線檢查。回傳失敗清單（空 ＝ 全過）。

    ⚠️ 吃字串不吃檔案是刻意的：突變測試要能拿壞掉的版本來驗這把尺是活的，
       而 ⛔ **絕對不可以把壞版本寫到磁碟上**（看門狗會把它載進他正在跑的面板）。
    """
    bad = []
    try:
        t = ast.parse(text)
    except SyntaxError as e:
        return ["原始碼解析不了：" + str(e)[:60]]
    fns = {n.name: n for n in ast.walk(t)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    # ① 模組層的預設值一定是 no-op
    top = [n for n in t.body if isinstance(n, ast.Assign)
           and any(getattr(x, "id", "") == "AUTO_SIG_HOOK" for x in n.targets)]
    if len(top) != 1 or getattr(top[0].value, "id", "") != "_auto_noop":
        bad.append("模組層的 AUTO_SIG_HOOK 預設值不是 _auto_noop")
    # ①b 收盤平倉的掛勾同一套規矩（它會送出平倉單）
    top_e = [n for n in t.body if isinstance(n, ast.Assign)
            and any(getattr(x, "id", "") == "AUTO_EOD_HOOK" for x in n.targets)]
    if len(top_e) != 1 or getattr(top_e[0].value, "id", "") != "_auto_eod_noop":
        bad.append("模組層的 AUTO_EOD_HOOK 預設值不是 _auto_eod_noop")
    # ② 只有 main() 會指派它，而且指的是 auto_fire.on_signal
    where = []
    for name, node in fns.items():
        for n in ast.walk(node):
            if isinstance(n, ast.Assign) and any(
                    getattr(x, "id", "") == "AUTO_SIG_HOOK" for x in n.targets):
                where.append((name, ast.unparse(n.value)))
    if where != [("main", "auto_fire.on_signal")]:
        bad.append(f"AUTO_SIG_HOOK 的指派點不對：{where}")
    where_e = []
    for name, node in fns.items():
        for n in ast.walk(node):
            if isinstance(n, ast.Assign) and any(
                    getattr(x, "id", "") == "AUTO_EOD_HOOK" for x in n.targets):
                where_e.append((name, ast.unparse(n.value)))
    if where_e != [("main", "auto_fire.on_eod")]:
        bad.append(f"AUTO_EOD_HOOK 的指派點不對：{where_e}")
    # ③ main() 要有 global 宣告，不然那一行只是個區域變數（接線靜靜地沒生效）
    _mainfn = fns.get("main", ast.Module(body=[], type_ignores=[]))
    _globals = {nm for n in ast.walk(_mainfn) if isinstance(n, ast.Global)
                for nm in n.names}
    if "AUTO_SIG_HOOK" not in _globals:
        bad.append("main() 沒有 global AUTO_SIG_HOOK")
    if "AUTO_EOD_HOOK" not in _globals:
        bad.append("main() 沒有 global AUTO_EOD_HOOK（收盤平倉接線靜靜地沒生效）")
    # ④ main() 要接常數與算式，也要把執行緒起起來
    m = ast.unparse(fns["main"]) if "main" in fns else ""
    for want in ("auto_fire.configure", "auto_fire.start()"):
        if want not in m:
            bad.append(f"main() 沒有 {want}")
    for want in ("sig_fn=auto_sig", "dirs_fn=auto_dirs", "signal_at=SIGNAL_AT",
                 "tp_points=TP_POINTS", "gap_s=AUTO_GAP_S", "eod_at=EOD_CLOSE_AT"):
        if want not in m:
            bad.append(f"main() 的接線少了 {want}（⛔ 比名稱不比數值）")
    # ⑤ _auto_tick 的**兩個**分支都要通知掛勾（晚到那一邊也要留下原因）
    tk = ast.unparse(fns["_auto_tick"]) if "_auto_tick" in fns else ""
    if tk.count("AUTO_SIG_HOOK(") != 2:
        bad.append("_auto_tick 呼叫掛勾的次數不是 2（晚到那一邊也要記）")
    if "AUTO_SIG_HOOK(None," not in tk.replace(" ", "").replace(",", ", "):
        bad.append("_auto_tick 的「晚到」那一邊沒有通知掛勾")
    # ⑤b 收盤平倉的掛勾也要真的被呼叫，而且那一天要能重置（跨日）
    if tk.count("AUTO_EOD_HOOK(") != 1:
        bad.append("_auto_tick 沒有呼叫收盤平倉的掛勾（13:43:30 什麼都不會發生）")
    # ⛔ 觸發條件要是一個**半開區間**：`EOD_CLOSE_SEC <= secs < DAY_END_SEC`。
    #    下界寫死數字 ⇒ 改了 13:43:30 不會跟著改（比名稱不比數值）。
    #    ⛔⛔ 沒有上界 ⇒ 本機時鐘（NTP 校時／他手動改時間）往前跳過 13:43:30，
    #    早上就會觸發一次平倉 —— 而那一刻部位是剛開的（lab-qa 提的時鐘防護）。
    #    ⚠️ 上界不可以靠 `sess != "day"` 代勞：那是 market_session() 那把尺算的，
    #    改了那邊這裡就靜靜地沒有上界（＝守衛蓋不到的那一半）。
    _tk1 = tk.replace(" ", "")
    if "EOD_CLOSE_SEC<=secs" not in _tk1:
        bad.append("_auto_tick 的收盤平倉觸發沒有用 EOD_CLOSE_SEC 當下界（⛔ 比名稱不比數值）")
    if "secs<DAY_END_SEC" not in _tk1:
        bad.append("_auto_tick 的收盤平倉觸發沒有 DAY_END_SEC 上界"
                   "（⛔ 時鐘往前跳就會在早上平掉他的單）")
    if "'eod': False" not in tk and '"eod": False' not in tk:
        bad.append("_auto_tick 跨日時沒有把 AUTO['eod'] 重置（隔天不會再平）")
    # ⑥ 訊號算式只能有一份正本
    if "auto_sig(" not in ast.unparse(fns.get("_auto_record",
                                              ast.Module(body=[], type_ignores=[]))):
        bad.append("_auto_record 沒有用 auto_sig()（訊號算式變成兩把尺）")
    return bad


chk("  出貨的這一份：接線全過", wiring_fails(LPSRC), [])
MUT = [
    ("接線整行拿掉（掛勾永遠是 no-op ⇒ 一輩子不下單）",
     "    AUTO_SIG_HOOK = auto_fire.on_signal", "    pass"),
    ("忘了寫 global（那一行變成區域變數，接線靜靜地沒生效）",
     "    global AUTO_SIG_HOOK", "    pass"),
    ("模組層的預設值直接接上 on_signal（治具與 --replay 會開始送單）",
     "AUTO_SIG_HOOK = _auto_noop", "AUTO_SIG_HOOK = auto_fire.on_signal"),
    ("晚到那一邊不通知掛勾（那天連「為什麼沒送」都沒有）",
     "            AUTO_SIG_HOOK(None, d, lag)", "            pass"),
    ("送單執行緒沒起來（佇列積著，永遠不送）",
     "    auto_fire.start()", "    pass"),
    ("接線比數值不比名稱（訊號時刻改了，自動下單不會跟著改）",
     "signal_at=SIGNAL_AT", 'signal_at="09:03:30"'),
    ("訊號算式各寫一份（模擬與真單可能算出相反的方向）",
     "    sig_a, sig_b = auto_sig(px, o845, p900)",
     "    sig_a = None if p900 is None else round(px - p900, 1)\n"
     "    sig_b = None if o845 is None else round(px - o845, 1)"),
    # ── 收盤平倉的接線（2026-09-09 加）
    ("收盤平倉的接線整行拿掉（13:43:30 什麼都不會發生 ⇒ 抱過夜盤）",
     "    AUTO_EOD_HOOK = auto_fire.on_eod", "    pass"),
    ("忘了把 AUTO_EOD_HOOK 也加進 global（收盤平倉靜靜地沒接上）",
     "    global AUTO_SIG_HOOK, AUTO_EOD_HOOK", "    global AUTO_SIG_HOOK"),
    ("收盤平倉的預設值直接接上（治具與 --replay 會開始送平倉單）",
     "AUTO_EOD_HOOK = _auto_eod_noop", "AUTO_EOD_HOOK = auto_fire.on_eod"),
    ("主迴圈不呼叫收盤平倉的掛勾",
     "        AUTO_EOD_HOOK(d, (secs - EOD_CLOSE_SEC) * 1000 + now.microsecond // 1000)",
     "        pass"),
    ("收盤平倉的時刻寫死（EOD_CLOSE_AT 改了不會跟著改）",
     "eod_at=EOD_CLOSE_AT", 'eod_at="13:43:30"'),
    ("收盤平倉的秒數寫死（⛔ 比名稱不比數值）",
     "    if not AUTO[\"eod\"] and EOD_CLOSE_SEC <= secs < DAY_END_SEC:",
     "    if not AUTO[\"eod\"] and 49410 <= secs < DAY_END_SEC:"),
    ("⛔ 時鐘防護：上界拿掉（NTP 往前跳過 13:43:30 ⇒ 早上就平掉他剛開的單）",
     "    if not AUTO[\"eod\"] and EOD_CLOSE_SEC <= secs < DAY_END_SEC:",
     "    if not AUTO[\"eod\"] and EOD_CLOSE_SEC <= secs:"),
    ("⛔ 時鐘防護：上界改用 sess 代勞（另一把尺，改了那邊這裡靜靜地沒有上界）",
     "    if not AUTO[\"eod\"] and EOD_CLOSE_SEC <= secs < DAY_END_SEC:",
     "    if not AUTO[\"eod\"] and EOD_CLOSE_SEC <= secs and sess == \"day\":"),
    ("跨日沒有重置 AUTO['eod']（隔天不會再平）",
     '        AUTO.update({"day": d, "done": False, "settled": False, "eod": False,',
     '        AUTO.update({"day": d, "done": False, "settled": False,'),
]
for name, old, new in MUT:
    say(old in LPSRC, f"  突變目標真的在原始碼裡：{name}")
    mutated = LPSRC.replace(old, new, 1)
    say(mutated != LPSRC, f"    …而且真的改到了")
    say(len(wiring_fails(mutated)) > 0, f"    ⇒ 守衛會紅：{name}",
        wiring_fails(mutated))

# ══ ⑨ ⛔ 這個檔不重寫下單邏輯（下單一律走 broker）═════════════════════
print("\n=== ⑨ ⛔ 下單一定要走 broker.py，不准自己組單 ===")
names = sorted({n.attr for n in ast.walk(tree)
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                and n.value.id == "broker"})
say("enter" in names and "can_enter" in names,
    "  auto_fire 是透過 broker.can_enter / broker.enter 下單的", names)
say("place_order" not in src and "FuturesOrder" not in src,
    "  ⛔ auto_fire 裡沒有 place_order／FuturesOrder（不自己組單、不自己送）")
say("REAL_ORDERS_ON" not in src.replace("`REAL_ORDERS_ON`", ""),
    "  ⛔ 沒有自己再判斷一次 REAL_ORDERS_ON（兩把尺遲早分岔）")
say(not any(isinstance(n, ast.Attribute) and n.attr == "exists"
            and isinstance(n.value, ast.Attribute) and n.value.attr == "REAL_FLAG"
            for n in ast.walk(tree)),
    "  ⛔ 也沒有偷讀 broker.REAL_FLAG")
say("import shioaji" not in src and "import sj" not in src,
    "  ⛔ 連 shioaji 都沒有 import（結構上組不出委託單）")

# ══ ⛔⛔ 行為面的量尺：**真的起一個服務打進去**（埠 0，⛔ 不是 8770）═════════
#    ⚠️⚠️ 2026-09-09 lab-qa 退件 R1：這一支原本用「某個字串出現在 live_panel.py 裡」
#       當「那個端點存在／回 405」的證據 —— 而 `do_GET` 的**中文註解本身**就含
#       `/api/fire/on` 與 `405`（那段註解正是在解釋這件事）⇒ 把真正那兩行整個刪掉，
#       斷言照樣綠（lab-qa 實測）。**這是 Ⓝ7 的同一個形狀，第二個。**
#    ⛔ 通則：端點的行為一律**送一個請求進去看回什麼**，⛔ 不准比原始碼字串。
#       （「哪一行程式在不在」那種只能比字串的，就要先把註解／docstring 剝掉，
#         見 `_nodoc()`。）
import threading as _th                                          # noqa: E402
import urllib.error as _ue                                       # noqa: E402
import urllib.request as _ur                                     # noqa: E402
from http.server import ThreadingHTTPServer as _THS              # noqa: E402

_SRV = _THS(("127.0.0.1", 0), LP.Handler)
_SRV_PORT = _SRV.server_address[1]
assert _SRV_PORT != 8770, "⛔ 不可以用 8770（他的面板正開著）"
_th.Thread(target=_SRV.serve_forever, daemon=True).start()


def hit(path, method="GET", body=None, headers=None):
    """真的打一個請求進去，回 `(狀態碼, Content-Type, 內文)`。⛔ 打的是暫存區那一套。"""
    rq = _ur.Request(f"http://127.0.0.1:{_SRV_PORT}{path}", data=body,
                     method=method, headers=headers or {})
    try:
        with _ur.urlopen(rq, timeout=15) as r:
            return r.status, r.headers.get("Content-Type", ""), \
                r.read().decode("utf-8", "replace")
    except _ue.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), \
            e.read().decode("utf-8", "replace")


# ══ ⑩ 端點是唯讀的（⛔ 畫面上沒有開關）═════════════════════════════════
print("\n=== ⑩ /api/fire/state 是唯讀的 ===")
before_rows = len(rows())
st = AF.state()
chk("  state() 不會寫任何一列", len(rows()), before_rows)
chk("  端出「開了沒」", "armed" in st, True)
chk("  端出「哪個做法」", "method" in st, True)
chk("  端出「真單開關」", "live" in st, True)
# ⛔⛔ 不可以只驗「欄位在不在」（lab-qa 2026-09-09 把它寫死成 True／False，181/181 全綠）。
#    這個欄位是「兩個開關都要開才會真的送出去」在畫面上**唯一**的來源 ——
#    寫死成 True ⇒ 演練時畫面寫「會真的送出去」；寫死成 False ⇒ 真的會送單卻寫「只會演練」。
live_on()
chk("  ⛔ 而且 live 要真的跟著 REAL_ORDERS_ON 走（檔案在 ⇒ True）",
    AF.state()["live"], True)
live_off()
chk("  ⛔ 而且 live 要真的跟著 REAL_ORDERS_ON 走（檔案不在 ⇒ False）",
    AF.state()["live"], False)
chk("    live_flag 講得出是哪個檔", AF.state()["live_flag"], "REAL_ORDERS_ON")
live_on()
chk("  端出每一天送了沒", isinstance(st.get("days"), list), True)
chk("  端出原因的正本（前後端不分岔）", st.get("why_texts"), dict(AF.WHY))
# ⛔ 2026-09-09 lab-qa R1 盤查：這一條原本是 `"/api/fire/state" in LPSRC` ——
#    那個字串在檔案裡出現好幾次（註解、前端 fetch），⇒ 後端路由刪掉照樣綠。
#    改成**真的送一個 GET 進去**，並附上「路由沒中的時候長什麼樣」當尺的自證。
_c, _ct, _b = hit("/api/fire/state")
_sj = json.loads(_b) if "json" in _ct.lower() else None
say(isinstance(_sj, dict) and "armed" in _sj,
    "  ⛔ 面板真的有這個 GET 端點（**真的打進去**，⛔ 不是比原始碼字串）",
    f"{_c} {_ct[:28]} {_b[:40]}")
_c2, _ct2, _b2 = hit("/api/fire/statXX")
say("html" in _ct2.lower(),
    "    尺的自證：路由沒中的時候回的是整張 HTML（⇒ 上面那條不是恆真）",
    f"{_c2} {_ct2[:28]}")
say('"/api/fire/state"' not in LPSRC.split("def do_POST")[1].split("def do_GET")[0],
    "  ⛔ do_POST 裡沒有這個端點（畫面上按不到開關）")
page = LPSRC[LPSRC.index('PAGE = r"""'):]
fire_html = page[page.index('<div id="tab-fire"'):page.index('<div id="tab-auto"')]
import re as _re
fire_html_nc = _re.sub(r"<!--.*?-->", " ", fire_html, flags=_re.S)
for tag in ("<button", "<form", "<input", "data-act", "data-rdir", "type=submit"):
    say(tag not in fire_html_nc, f"  ⛔ 【自動下單】那一頁沒有 {tag}")
say("AUTO_ORDERS_ON" in page, "  畫面上寫得出開關檔的名字")

# ══ ⑫ ⛔⛔ 收盤自動平倉：只平自己開的那一口 ═══════════════════════════
print("\n=== ⑫ ⛔⛔ 收盤自動平倉（13:43:30）===")
chk("  正本只有 live_panel 一份（秒數與字串對得上）",
    LP.EOD_CLOSE_SEC,
    int(LP.EOD_CLOSE_AT[:2]) * 3600 + int(LP.EOD_CLOSE_AT[3:5]) * 60
    + int(LP.EOD_CLOSE_AT[6:8]))
say(LP.EOD_CLOSE_SEC < LP.DAY_END_SEC,
    "  ⛔ 一定要在 13:45 收盤之前（收盤後送不出去，而且停損也停了）",
    f"{LP.EOD_CLOSE_SEC} < {LP.DAY_END_SEC}")
say(LP.DAY_END_SEC - LP.EOD_CLOSE_SEC >= 60,
    "  ⛔ 而且要留得下 broker 兩輪重試的餘裕（≥60 秒）",
    f"{LP.DAY_END_SEC - LP.EOD_CLOSE_SEC} 秒")
say(len(set(AF.WHY.values())) == len(AF.WHY),
    "  ⛔ 加了收盤那幾種之後，每一句話仍然互不相同")


def auto_entered(method="B", side="Buy", fill=12013.0):
    """讓自動下單真的開出一口部位（走完整條 09:03:30 的路），回傳那口部位。"""
    reset_eod()
    arm_write(method)
    live_on()
    api = SimAPI(side, fill)
    connect(api)
    run_signal(FakeToday())
    return api


print("\n  ── ① 自動開的那一口 ⇒ 收盤真的平掉 ──")
api = auto_entered()
say(broker._state["position"] is not None, "  前置：自動下單真的開出部位了",
    str(broker._state["position"]))
covers0 = [o for o in api.orders if str(o.octype) == "FuturesOCType.Cover"
           and str(o.price_type) == "FuturesPriceType.MKP"]
run_eod()
r = eod_row()
chk("  落地了一列收盤平倉", (r or {}).get("rec"), "eod")
chk("  結果是「平掉了」", (r or {}).get("why"), "eod_closed")
covers = [o for o in api.orders if str(o.octype) == "FuturesOCType.Cover"
          and str(o.price_type) == "FuturesPriceType.MKP"]
chk("  ⛔ 真的送出一張市價平倉單（Cover + MKP + IOC）", len(covers) - len(covers0), 1)
chk("    方向是反向的（做多 ⇒ 賣出）", str(covers[-1].action), "Action.Sell")
chk("    IOC", str(covers[-1].order_type), "OrderType.IOC")
chk("  平完之後本機沒有部位了", broker._state["position"], None)
say((r or {}).get("at"), "  落地寫得出幾點平的", (r or {}).get("at"))
say((r or {}).get("eod_at") == LP.EOD_CLOSE_AT, "  落地寫得出設定的時刻",
    (r or {}).get("eod_at"))
say((r or {}).get("exit") is not None or (r or {}).get("exit_time") is not None,
    "  落地帶得出出場那一筆（成績單上撈回來的）", str(r))
day0 = merged()
chk("  ⛔ 收盤那一列不准蓋掉「今天送了什麼」", (day0.get("rec"), day0.get("ok")),
    ("result", True))
chk("    而且收在自己的抽屜裡", (day0.get("eod") or {}).get("why"), "eod_closed")
_d12, _led12 = AF.read_all()
say(_led12["eod"] >= 1, "  帳本真的把 eod 那一種數進去了", str(_led12))
chk("  ⛔ fire + result + skip + eod + bad ＝ 總列數（收盤那一列也要有去處）",
    _led12["fire"] + _led12["result"] + _led12["skip"] + _led12["eod"] + _led12["bad"],
    _led12["total"])

print("\n  ── ② ⛔⛔ 他 09:20 自己進場 ⇒ 收盤那一下絕對不准平 ──")
reset_eod()
arm_clear()                    # 自動下單關著 ⇒ 今天不會有自動的部位
live_on()
api = SimAPI("Buy", 12008.0)
connect(api)
run_signal(FakeToday())        # 走完 09:03:30：關著 ⇒ 落一列 skip
chk("  前置：自動下單那一列是「沒送」", merged().get("rec"), "skip")
ok, err, pos = broker.enter("long", 12008.0, LP.TP_POINTS)   # ← 他自己按的那一單
broker._state["position"]["entry_time"] = "09:20:11"
say(ok and broker._state["position"] is not None, "  前置：他自己 09:20 進場了", str(err))
n_before = len(api.orders)
run_eod()
r = eod_row()
chk("  ⛔⛔ 一張平倉單都沒有送出去", len(api.orders) - n_before, 0)
say(broker._state["position"] is not None,
    "  ⛔⛔ 他的部位**還在**（沒有被平掉）", str(broker._state["position"]))
chk("  而且落地講得出為什麼不碰", (r or {}).get("why"), "eod_no_entry")
say("沒有開出部位" in ((r or {}).get("why_msg") or ""),
    "    那句話講得出「今天自動下單沒有開出部位」", (r or {}).get("why_msg"))

print("\n  ── ③ ⛔⛔ 自動開了 → 他自己平掉 → 又自己開一口新的 ⇒ 不准平 ──")
api = auto_entered(side="Buy", fill=12013.0)
say(broker._state["position"] is not None, "  前置：自動開了一口")
okc, errc = broker.close("manual")            # ← 他自己按手動平倉
say(okc, "  前置：他自己把它平掉了", str(errc))
say(len(broker.trades_today()) >= 1, "  前置：成績單上留下那一趟",
    str(len(broker.trades_today())))
api.flat = False                              # 他又自己開了一口新的（同方向、價差 5 點）
api.fill = 12018.0
ok, err, pos = broker.enter("long", 12018.0, LP.TP_POINTS)
broker._state["position"]["entry_time"] = "11:05:00"
say(ok, "  前置：他自己又開了一口新的", str(err))
n_before = len(api.orders)
run_eod()
r = eod_row()
chk("  ⛔⛔ 一張平倉單都沒有送出去", len(api.orders) - n_before, 0)
say(broker._state["position"] is not None, "  ⛔⛔ 他那口新的部位還在")
chk("  而且是「先前已經平掉了」擋下來的", (r or {}).get("why"), "eod_done_elsewhere")
say(len((r or {}).get("why_msg") or "") > 10, "    那句話講得出來", (r or {}).get("why_msg"))

print("\n  ── ③b ⛔⛔ 平掉之後又在**同一個價**開一口新的（只有「查過帳」擋得住）──")
api = auto_entered(side="Buy", fill=12013.0)
_ent_t = broker._state["position"]["entry_time"]
okc, errc = broker.close("manual")
say(okc, "  前置：自動那一口被他平掉了", str(errc))
api.flat = False
ok, err, pos = broker.enter("long", 12013.0, LP.TP_POINTS)   # ⚠️ 同方向、同一個價
broker._state["position"]["entry_time"] = None               # ⚠️ 面板重啟後撿回來的形狀
say(ok and broker._state["position"]["entry"] == 12013.0,
    "  前置：他自己又開了一口，方向與進場價**跟自動那一口一模一樣**")
n_before = len(api.orders)
run_eod()
r = eod_row()
chk("  ⛔⛔ 一張平倉單都沒有送出去（方向與價都對得上，靠的是「查過帳」那一道）",
    len(api.orders) - n_before, 0)
chk("  落地是「先前已經平掉了」", (r or {}).get("why"), "eod_done_elsewhere")

print("\n  ── ③c ⛔ 面板當掉那段他在別處平的：只有**券商的已實現損益**看得到 ──")


class PnlAPI(SimAPI):
    """有 list_profit_loss 的假券商（`broker.realized_today()` 的離線替身）。"""

    def __init__(self, side, fill, rows):
        super().__init__(side, fill)
        self.pnl_rows = rows

    def list_profit_loss(self, acc=None, d1=None, d2=None):
        return self.pnl_rows


api = auto_entered(side="Buy", fill=12013.0)
_auto_entry_px = broker._state["position"]["entry"]
# 面板當掉那段時間他在大戶投把自動那口平掉、又開了一口新的（本機**沒有任何紀錄**）
if broker.TRADE_DIR.exists():
    shutil.rmtree(broker.TRADE_DIR)
broker._REALIZED.update({"at": 0.0, "rows": []})
api2 = PnlAPI("Buy", 12013.0, [type("R", (), {
    "direction": "Buy", "entry_price": _auto_entry_px, "cover_price": 12040.0,
    "pnl": 1350.0, "quantity": 1})()])
api2.opened = True
broker._state["api"] = api2
broker._state["position"]["entry_time"] = None      # 重啟後撿回來的形狀
say(broker.trades_today() == [] or all(
        abs((t.get("entry") or 0) - _auto_entry_px) > 1.0 for t in broker.trades_today()),
    "  前置：本機成績單上**沒有**那一趟（面板當掉了）")
say(len(broker.realized_today()) == 1,
    "  前置：券商的已實現損益上有那一趟", str(broker.realized_today()))
n_before = len(api2.orders)
run_eod()
r = eod_row()
chk("  ⛔⛔ 一張平倉單都沒有送出去", len(api2.orders) - n_before, 0)
chk("  落地是「先前已經平掉了」", (r or {}).get("why"), "eod_done_elsewhere")
say("已實現損益" in ((r or {}).get("why_msg") or ""),
    "    而且講得出是從券商那份看出來的", (r or {}).get("why_msg"))
broker._REALIZED.update({"at": 0.0, "rows": []})

print("\n  ── ③d ⛔ 進場時間對不上 ⇒ 不准平（方向與價都一樣也不行）──")
api = auto_entered(side="Buy", fill=12013.0)
broker._state["position"]["entry_time"] = "10:30:00"   # ⚠️ 跟帳本那一列不同
n_before = len(api.orders)
run_eod()
r = eod_row()
chk("  ⛔ 一張平倉單都沒有送出去", len(api.orders) - n_before, 0)
chk("  落地是「不是自動下單開的」", (r or {}).get("why"), "eod_not_ours")
say("進場時間" in ((r or {}).get("why_msg") or ""),
    "    而且講得出差在哪", (r or {}).get("why_msg"))

print("\n  ── ③e ⛔ 價格差很多的部位（撿回來的／他在別處開的）⇒ 不准平 ──")
api = auto_entered(side="Buy", fill=12013.0)
# 直接把券商那邊換成「另一口」：方向一樣但進場價差很多
api.fill = 12090.0
broker._state["position"]["entry"] = 12090.0
broker._state["position"]["entry_time"] = None      # ← 重啟後撿回來的形狀
n_before = len(api.orders)
run_eod()
r = eod_row()
chk("  ⛔ 一張平倉單都沒有送出去", len(api.orders) - n_before, 0)
chk("  落地是「不是自動下單開的」", (r or {}).get("why"), "eod_not_ours")
say("進場價差" in ((r or {}).get("why_msg") or ""),
    "    而且講得出差在哪（⛔ 不是一句空話）", (r or {}).get("why_msg"))

print("\n  ── ③f ⛔ 方向不一樣 ⇒ 不准平（平錯邊＝部位加倍）──")
api = auto_entered(side="Buy", fill=12013.0)
broker._state["position"]["dir"] = "short"
api.side = "Sell"
n_before = len(api.orders)
run_eod()
chk("  ⛔ 一張平倉單都沒有送出去", len(api.orders) - n_before, 0)
chk("  落地是「不是自動下單開的」", (eod_row() or {}).get("why"), "eod_not_ours")

print("\n  ── ④ 已經沒有部位（停利在券商成交了）⇒ 什麼都不做 ──")
api = auto_entered(side="Buy", fill=12013.0)
api.flat = True                       # 券商那邊平掉了
broker._state["position"] = None
n_before = len(api.orders)
run_eod()
chk("  ⛔ 一張平倉單都沒有送出去", len(api.orders) - n_before, 0)
say((eod_row() or {}).get("why") in ("eod_flat", "eod_done_elsewhere"),
    "  落地講得出「已經沒有部位了」", (eod_row() or {}).get("why"))

print("\n  ── ⑤ 對帳失敗 ⇒ ⛔ 不敢動，也不准清掉部位 ──")
api = auto_entered(side="Buy", fill=12013.0)
pos_before = dict(broker._state["position"])
_win, AF.EOD_WINDOW_S = AF.EOD_WINDOW_S, 0.0      # 這一節不等重試（守的是「不動手」）
broker._state["api"] = BadAPI()                   # list_positions 直接丟例外
n_before = len(api.orders)
run_eod()
r = eod_row()
AF.EOD_WINDOW_S = _win
chk("  ⛔ 一張平倉單都沒有送出去", len(api.orders) - n_before, 0)
chk("  落地是「對帳失敗，不敢動」", (r or {}).get("why"), "eod_unknown")
say(broker._state["position"] is not None,
    "  ⛔ 本機部位還在（清掉就等於停損也不管了）")
say(bool((r or {}).get("alarm")), "  而且標成要他自己動手（alarm）")

print("\n  ── ⑥ 平不掉 ⇒ ⛔ 大聲講，叫他自己到大戶投平 ──")


class NoCoverAPI(SimAPI):
    """送得出去、就是撮不到（範圍市價 IOC 當下沒成交）—— broker 既有的失敗路徑。"""

    def place_order(self, contract, order):
        SENT["n"] += 1
        self.orders.append(order)
        if str(order.octype) == "FuturesOCType.New":
            self.opened = True
        return FakeTrade()            # ⛔ 刻意不設 flat ⇒ 部位一直都在


reset_eod()
arm_write("B")
live_on()
api = NoCoverAPI("Buy", 12013.0)
connect(api)
_fw, broker.FILL_WAIT = broker.FILL_WAIT, 0.5     # ⛔ 只縮測試等待，不改產品邏輯
_win, AF.EOD_WINDOW_S = AF.EOD_WINDOW_S, 0.0
_rt, AF.EOD_RETRY_S = AF.EOD_RETRY_S, 0.05
run_signal(FakeToday())
say(broker._state["position"] is not None, "  前置：開出部位了")
n_before = len(api.orders)
run_eod()
r = eod_row()
broker.FILL_WAIT, AF.EOD_WINDOW_S, AF.EOD_RETRY_S = _fw, _win, _rt
say(len(api.orders) - n_before >= broker.CLOSE_TRIES,
    f"  真的照 broker 的 CLOSE_TRIES 重送了 {broker.CLOSE_TRIES} 次",
    str(len(api.orders) - n_before))
chk("  落地是「沒平掉」", (r or {}).get("why"), "eod_failed")
say("大戶投" in ((r or {}).get("why_msg") or ""),
    "  ⛔ 而且叫他自己到大戶投平倉", (r or {}).get("why_msg"))
say(bool((r or {}).get("alarm")), "  標成要他自己動手（alarm）")
say(broker._state["position"] is not None,
    "  ⛔⛔ 沒平掉就**絕對不可以**清掉本機部位（清了停損就不管了）")

print("\n  ── ⑦ 走的是 broker 既有的那一把 _close_lock ──")
say("close" in sorted({n.attr for n in ast.walk(ast.parse(
        pathlib.Path(AF.__file__).read_text(encoding="utf-8")))
    if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
    and n.value.id == "broker"}),
    "  收盤平倉是呼叫 broker.close()（⛔ 沒有自己寫一套）")
_af_src = pathlib.Path(AF.__file__).read_text(encoding="utf-8")
say("place_order" not in _af_src and "FuturesOCType" not in _af_src,
    "  ⛔ 這個檔沒有自己組平倉單")
api = auto_entered(side="Buy", fill=12013.0)
broker._close_lock.acquire()          # 假裝停損那條執行緒正在平倉
n_before = len(api.orders)
_win, AF.EOD_WINDOW_S = AF.EOD_WINDOW_S, 0.0
run_eod()
AF.EOD_WINDOW_S = _win
broker._close_lock.release()
chk("  ⛔ 鎖被別人拿著的時候一張單都不送（不排隊）", len(api.orders) - n_before, 0)
say((eod_row() or {}).get("why") == "eod_failed"
    and "正在平倉中" in ((eod_row() or {}).get("err") or ""),
    "  而且把 broker 那句「正在平倉中」原樣帶出來", str((eod_row() or {}).get("err")))

print("\n  ── ⑧ on_eod 跑在 4Hz 主迴圈上（⛔ 只准 put_nowait）──")
_tree = ast.parse(_af_src)
_fn = {n.name: n for n in ast.walk(_tree)
       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
_calls = sorted({(n.func.attr if isinstance(n.func, ast.Attribute) else
                  getattr(n.func, "id", "?"))
                 for n in ast.walk(_fn["on_eod"]) if isinstance(n, ast.Call)})
say("put_nowait" in _calls, "  on_eod 有用 put_nowait", str(_calls))
say(not any(c in _calls for c in ("open", "read_text", "write_text", "sleep",
                                  "put", "get", "close", "reconcile", "acquire")),
    "  ⛔ 裡面沒有 I/O／網路／鎖／阻塞式 put", str(_calls))
_before = AF._ST["err_n"]
# ⚠️ ⑦ 已經把真的執行緒起起來了 —— 直接往 `_EQ` 塞會被它撈去真的跑一輪平倉，
#    然後下一節 rmtree 撞到「檔案正由另一個程序使用」。換一個乾淨的佇列來塞。
_saveQ, AF._EQ = AF._EQ, queue.Queue(maxsize=AF.QUEUE_MAX)
for _ in range(AF.QUEUE_MAX + 3):
    AF.on_eod(DAY, 0)
say(AF._ST["err_n"] > _before and "佇列" in (AF._ST["err"] or ""),
    "  佇列滿了也不丟例外，而且講得出是什麼事", AF._ST["err"])
AF._EQ = _saveQ
while not AF._EQ.empty():
    AF._EQ.get()

print("\n  ── ⑨ 一天一次（看門狗在 13:43~13:45 之間重啟會重觸發）──")
api = auto_entered(side="Buy", fill=12013.0)
run_eod()
chk("  第一次：平掉了", (eod_row() or {}).get("why"), "eod_closed")
n_rows = len(rows())
n_before = len(api.orders)
run_eod()                            # 看門狗重啟：同一天再觸發一次
chk("  ⛔ 第二次不會再送平倉單", len(api.orders) - n_before, 0)
chk("  ⛔ 也不會再多寫一列", len(rows()), n_rows)

print("\n  ── ⑩ 送到一半當掉那天（不知道下場）⇒ ⛔ 不敢動手 ──")
reset_eod()
arm_write("B")
live_on()
api = SimAPI("Buy", 12013.0)
connect(api)
AF._append({"rec": "fire", "stage": "sending", "date": DAY, "method": "B",
            "dir": "long", "px": 12010.0, "live": True})
broker._state["position"] = {"dir": "long", "entry": 12013.0, "qty": 1,
                             "entry_time": "09:03:31", "target_trade": None,
                             "recovered": False}
api.opened = True
n_before = len(api.orders)
run_eod()
chk("  ⛔ 一張平倉單都沒有送出去", len(api.orders) - n_before, 0)
chk("  落地是「不知道下場，不敢動手」", (eod_row() or {}).get("why"), "eod_unsure")
say(bool((eod_row() or {}).get("alarm")), "  標成要他自己確認（alarm）")

print("\n  ── ⑪ 演練模式（真單關著）也要照跑（⛔ 只差不送出去）──")
reset_eod()
arm_write("B")
live_off()
api = SimAPI("Buy", 12013.0)
connect(api)
run_signal(FakeToday())
say(broker._state["position"] is not None, "  前置：演練部位開出來了")
run_eod()
chk("  演練也會走完收盤平倉這條路", (eod_row() or {}).get("why"), "eod_closed")
chk("  演練部位被平掉了", broker._state["position"], None)
say((eod_row() or {}).get("live") is False, "  而且落地標著這是演練")
live_on()

print("\n  ── ⑫ ⛔⛔ M1：問不到券商的已實現損益 ⇒ 不敢動手（lab-qa X4）──")


class BlindPnlAPI(SimAPI):
    """
    已實現損益那一支**問不到**（網路抖一下）。⚠️ 部位那一支照樣答得出來 ——
    X4 的形狀就是「其他都正常，只有這一支抖了一下」。
    """

    def list_profit_loss(self, acc=None, d1=None, d2=None):
        raise RuntimeError("list_profit_loss 抖了一下")


# X4 的完整形狀：面板當掉的那段時間，他在大戶投把自動那口平掉、又自己開了一口
# **方向與價位都一樣**的新單 ⇒ 面板重啟（entry_time 遺失）⇒
# 第①道（成績單）沒有那一趟、第③道只剩方向＋價 ⇒ **全靠第②道**。
api = auto_entered(side="Buy", fill=12013.0)
_x4_px = broker._state["position"]["entry"]
if broker.TRADE_DIR.exists():
    shutil.rmtree(broker.TRADE_DIR)            # 面板當掉 ⇒ 本機成績單上沒有那一趟
broker._REALIZED.update({"at": 0.0, "rows": []})
_blind = BlindPnlAPI("Buy", _x4_px)
_blind.opened = True
broker._state["api"] = _blind
broker._state["position"]["entry_time"] = None   # 重啟後從券商撿回來的形狀
say(broker.realized_today() is None,
    "  前置：券商的已實現損益**問不到** ⇒ broker 回 None（⛔ 不是空陣列）",
    repr(broker.realized_today()))
n_before = len(_blind.orders)
run_eod()
r = eod_row()
chk("  ⛔⛔ 一張平倉單都沒有送出去（X4 舊版在這裡送出 1 張，平掉他的部位）",
    len(_blind.orders) - n_before, 0)
say(broker._state["position"] is not None, "  ⛔⛔ 他那口部位**還在**")
chk("  落地是「對不了帳，不知道平掉了沒」", (r or {}).get("why"), "eod_cant_tell")
say((r or {}).get("alarm") is True, "  ⛔ 而且掛金色警示（他要自己去確認）")
say("已經平掉" not in ((r or {}).get("why_msg") or ""),
    "  ⛔ 而且**不准**寫成「已經平掉了」（部位還開著 ⇒ 那是一句假話）",
    (r or {}).get("why_msg"))

print("    ── 負控組：同一個情境，只把「問不到」換成「券商答了：今天沒有」──")
# ⛔ 沒有這一組就證明不了「擋下來的是第②道」——可能只是別的地方順手擋掉了。
api = auto_entered(side="Buy", fill=12013.0)
if broker.TRADE_DIR.exists():
    shutil.rmtree(broker.TRADE_DIR)
broker._REALIZED.update({"at": 0.0, "rows": []})
broker._state["position"]["entry_time"] = None
chk("    前置：這一次券商答得出來（空的＝確定沒有）", broker.realized_today(fresh=True), [])
n_before = len(api.orders)
run_eod()
chk("    ⇒ 這一次**才**動手（證明前面擋下來的就是第②道那個「問不到」）",
    len(api.orders) - n_before, 1)
chk("      落地是「平掉了」", (eod_row() or {}).get("why"), "eod_closed")

print("\n  ── ⑫b ⛔⛔ M1／X5：快取沒過期而且是空的（同根因、無例外版）──")
api = auto_entered(side="Buy", fill=12013.0)
_x5_px = broker._state["position"]["entry"]
if broker.TRADE_DIR.exists():
    shutil.rmtree(broker.TRADE_DIR)
# 13:42:40 問過一次，那時他還沒平 ⇒ 快取是空的，而且 TTL(60 秒) **還沒過期**
broker._REALIZED.update({"at": time.time(), "rows": []})
# 13:43:10 他在大戶投把它平掉了 —— 這件事只有券商那一份看得到
_p5 = PnlAPI("Buy", _x5_px, [type("R", (), {
    "direction": "Buy", "entry_price": _x5_px, "cover_price": 12040.0,
    "pnl": 1350.0, "quantity": 1})()])
_p5.opened = True
broker._state["api"] = _p5
broker._state["position"]["entry_time"] = None
chk("  前置：吃快取那條路回的是**空的**（59 秒前的快照）", broker.realized_today(), [])
say(len(broker.realized_today(fresh=True) or []) == 1,
    "  前置：fresh=True 重問一次才看得到他剛剛平掉了",
    str(broker.realized_today(fresh=True)))
broker._REALIZED.update({"at": time.time(), "rows": []})   # 快取再壓回「空的、沒過期」
n_before = len(_p5.orders)
run_eod()
r = eod_row()
chk("  ⛔⛔ 一張平倉單都沒有送出去（X5 舊版在這裡送出 1 張）",
    len(_p5.orders) - n_before, 0)
chk("  落地是「先前已經平掉了」", (r or {}).get("why"), "eod_done_elsewhere")
say("已實現損益" in ((r or {}).get("why_msg") or ""),
    "    而且講得出是從券商那一份看出來的", (r or {}).get("why_msg"))

print("\n  ── ⑫c ⛔⛔ M2／X1：他在大戶投加碼 ⇒ 券商上是 2 口 ⇒ ⛔ 不准碰 ──")
api = auto_entered(side="Buy", fill=12013.0)
_x1_px = broker._state["position"]["entry"]


def _lots(a, n, px):
    # ⚠️ 加碼之後的均價落在 EOD_PX_TOL(1.0 點) 之內 ⇒ 方向、價、時間**全都對得上**，
    #    舊版因此判定「是我們的」；而 broker.close() 的成交判準是「部位整個不見」
    #    ⇒ 它會一直送 Cover 到券商剩 0 口 ⇒ 他自己那一口一起被平掉。
    # ⚠️ 平掉之後要真的回報空手（`a.flat`），不然假物件會讓 close() 永遠等不到成交 ——
    #    那樣負控組紅的是假物件不是產品（CLAUDE.md：2026-09-01 連兩次栽在假物件上）。
    def f(acc=None):
        if a.flat or not a.opened:
            return []
        return [type("P", (), {"code": "TMFI6", "quantity": n,
                               "direction": "Buy", "price": px})()]
    return f


api.list_positions = _lots(api, 2, _x1_px + 0.5)
n_before = len(api.orders)
run_eod()
r = eod_row()
chk("  ⛔⛔ 一張平倉單都沒有送出去（X1 舊版連送 2 張、把他加碼那口一起平掉）",
    len(api.orders) - n_before, 0)
chk("  落地是「不是自動下單開的」", (r or {}).get("why"), "eod_not_ours")
say("口數" in ((r or {}).get("why_msg") or ""),
    "    而且講得出差在哪（⛔ 不是一句空話）", (r or {}).get("why_msg"))
say((r or {}).get("alarm") is True, "  ⛔ 而且掛金色警示")
chk("    落地寫得出現在是幾口（他要看得出為什麼面板不幫他平）",
    (r or {}).get("now_qty"), 2.0)

print("    ── 負控組：一模一樣的情境，只把 2 口換成 1 口 ──")
api = auto_entered(side="Buy", fill=12013.0)
api.list_positions = _lots(api, 1, broker._state["position"]["entry"] + 0.5)
n_before = len(api.orders)
run_eod()
chk("    ⇒ 1 口才動手（證明擋下來的就是「口數」那一格）",
    len(api.orders) - n_before, 1)
chk("      落地是「平掉了」", (eod_row() or {}).get("why"), "eod_closed")

print("\n  ── ⑫d ⛔ M3／Y1：帳本那一列沒有進場價 ⇒ ⛔ 不准寫成「已經平掉了」──")
reset_eod()
arm_write("B")
live_on()
api = SimAPI("Buy", 12013.0)
api.opened = True
connect(api)
broker._state["position"] = {"dir": "long", "entry": 12013.0, "qty": 1,
                             "entry_time": "09:03:31", "target_trade": None,
                             "recovered": False}
AF.FIRE_DIR.mkdir(parents=True, exist_ok=True)
AF._append({"rec": "result", "date": DAY, "stage": "done", "ok": True,
            "dir": "long", "entry": None, "entry_time": "09:03:31",
            "method": "B", "why": None})
n_before = len(api.orders)
run_eod()
r = eod_row()
chk("  ⛔ 一張平倉單都沒有送出去", len(api.orders) - n_before, 0)
say(broker._state["position"] is not None, "  ⛔ 部位**還在**")
chk("  落地是 eod_cant_tell（⛔ 不是 eod_done_elsewhere）",
    (r or {}).get("why"), "eod_cant_tell")
say((r or {}).get("alarm") is True, "  ⛔ 而且掛金色警示（Y1 舊版靜悄悄）")
say("已經平掉" not in ((r or {}).get("why_msg") or ""),
    "  ⛔ 畫面上不准寫「先前已經平掉了」——部位其實還開著（Y1：那是一句假話）",
    (r or {}).get("why_msg"))

print("\n  ── ⑫e ⛔ M3／Y2：讀不到面板的成績單 ⇒ 同樣不准說「已經平掉了」──")
api = auto_entered(side="Buy", fill=12013.0)
_orig_tt = broker.trades_today


def _tt_boom():
    raise RuntimeError("成績單讀不到（檔案壞了）")


broker.trades_today = _tt_boom
n_before = len(api.orders)
run_eod()
r = eod_row()
broker.trades_today = _orig_tt
chk("  ⛔ 一張平倉單都沒有送出去", len(api.orders) - n_before, 0)
say(broker._state["position"] is not None, "  ⛔ 部位**還在**")
chk("  落地是 eod_cant_tell（⛔ 不是 eod_done_elsewhere）",
    (r or {}).get("why"), "eod_cant_tell")
say((r or {}).get("alarm") is True, "  ⛔ 而且掛金色警示（Y2 舊版靜悄悄）")

print("\n  ── ⑫f ⛔ 「查不到」跟「已經平掉了」是兩句不同的話，而且待遇不同 ──")
say(AF.WHY["eod_cant_tell"] != AF.WHY["eod_done_elsewhere"],
    "  ⛔ 兩個不同的原因不准寫同一句")
say("eod_cant_tell" in AF.EOD_ALARM,
    "  ⛔ 「查不到」要示警（他抱過夜盤、13:45 起停損也停了，畫面不能靜悄悄）")
say("eod_done_elsewhere" not in AF.EOD_ALARM,
    "  ⚠️ 「真的查到出場紀錄」不示警（那是正常的一天）")
say("eod_cant_tell" not in AF.EOD_SETTLED,
    "  ⛔ 「查不到」不算有定論 ⇒ 看門狗在 13:43:30~13:45 重啟時還能再試一次")
say("eod_done_elsewhere" in AF.EOD_SETTLED,
    "  ⚠️ 「真的平掉了」才算有定論")

print("\n  ── ⑫g ⛔ 三個常數自己也要有守衛（lab-qa 突變 Q2／Q3／Q4 都打不紅）──")
# ⛔ Q2：EOD_WINDOW_S 75→5 ⇒ 平不掉只試得了一輪，而那一輪就是 3 張沒撮到的 IOC。
#    窗口至少要放得下**兩輪真的送單**：一輪 close() 是 CLOSE_TRIES 張 IOC、
#    每張等 FILL_WAIT；失敗後還要等 CLOSE_COOLDOWN 才准再送一輪。
_one_round = broker.CLOSE_TRIES * broker.FILL_WAIT
_two_rounds = _one_round + broker.CLOSE_COOLDOWN + _one_round
say(AF.EOD_WINDOW_S >= _two_rounds,
    "  ⛔ 窗口放得下兩輪 close()（⛔ 比常數不比數字）",
    f"EOD_WINDOW_S={AF.EOD_WINDOW_S} ≥ {_one_round}+{broker.CLOSE_COOLDOWN}"
    f"+{_one_round}={_two_rounds}")
say(LP.EOD_CLOSE_SEC + AF.EOD_WINDOW_S <= LP.DAY_END_SEC,
    "  ⛔ 而且窗口整段收在 13:45 之前（收盤後送不出去，停損也停了）",
    f"{LP.EOD_CLOSE_SEC}+{AF.EOD_WINDOW_S} ≤ {LP.DAY_END_SEC}")
say(AF.EOD_RETRY_S <= broker.CLOSE_COOLDOWN,
    "  ⚠️ 重試間隔不比 broker 的冷卻長（不然每一輪都要多空等一段）",
    f"{AF.EOD_RETRY_S} ≤ {broker.CLOSE_COOLDOWN}")
# ⛔ Q3：把 eod_failed 塞進 EOD_SETTLED ⇒ 看門狗重啟後不再重試，而部位還在。
for _w in ("eod_failed", "eod_unknown", "eod_crashed", "eod_cant_tell"):
    say(_w not in AF.EOD_SETTLED,
        f"  ⛔ {_w} **不准**算有定論（部位可能還在，要能重試）")
# ⛔ 逐字斷言：名單被動過就要紅（Q3／Q4 都是「悄悄改一個名單」的形狀）。
chk("  ⛔ EOD_SETTLED 就是這幾種（逐字）", tuple(AF.EOD_SETTLED),
    ("eod_closed", "eod_flat", "eod_no_entry", "eod_not_ours",
     "eod_done_elsewhere", "eod_unsure"))
# ⛔ Q4：把 eod_not_ours 從 EOD_ALARM 拿掉 ⇒ 「這口不是我的、我不碰」變成一行小字，
#    他不會注意到，於是抱著一口沒人管的部位過夜。
chk("  ⛔ EOD_ALARM 就是這幾種（逐字）", tuple(AF.EOD_ALARM),
    ("eod_failed", "eod_unknown", "eod_unsure", "eod_crashed",
     "eod_not_ours", "eod_queue_full", "eod_cant_tell"))
say(set(AF.EOD_ALARM) & set(AF.EOD_SETTLED) == {"eod_unsure", "eod_not_ours"},
    "  ⚠️ 「有定論」與「要示警」只有這兩種可以重疊（不碰它但這一天不用再試）",
    str(sorted(set(AF.EOD_ALARM) & set(AF.EOD_SETTLED))))
say(set(AF.EOD_ALARM) | set(AF.EOD_SETTLED) | {"eod_closed"} <= set(AF.WHY),
    "  ⛔ 兩個名單裡的每一種都要有自己的一句話（畫面上看得到）")

print("\n  ── ⑫h ⛔ B4／Q11：13:43:30 一天只丟一件（4Hz ⇒ 每圈丟一次的話一秒 4 張）──")
reset_eod()
LP.AUTO["started"] = True
LP.AUTO.update({"day": DAY, "done": True, "settled": True, "eod": False, "gaps": 0.0})
LP.AUTO_EOD_HOOK = AF.on_eod
for _i in range(12):        # 4Hz 跑三秒
    LP._auto_tick(None, datetime.datetime.combine(
        TODAY, datetime.time(13, 43, 30 + _i // 4, (_i % 4) * 250000)), "day")
chk("  ⛔ 跑 12 圈只丟進佇列 1 件（⛔ 不是 12 件）", AF._EQ.qsize(), 1)
while not AF._EQ.empty():
    AF._EQ.get()
# ⛔ 時鐘往前跳：NTP 校時把早上跳到 13:43:30 之後才會觸發，跳到別的時間都不准。
for _hh, _mm, _ss, _want in ((9, 3, 31, 0), (13, 43, 29, 0), (13, 44, 59, 1),
                             (13, 45, 0, 0), (10, 0, 0, 0)):
    reset_eod()
    LP.AUTO["started"] = True
    LP.AUTO.update({"day": DAY, "done": True, "settled": True, "eod": False,
                    "gaps": 0.0})
    LP._auto_tick(None, datetime.datetime.combine(
        TODAY, datetime.time(_hh, _mm, _ss)), "day")
    chk(f"  {_hh:02d}:{_mm:02d}:{_ss:02d} ⇒ 丟進佇列 {_want} 件",
        AF._EQ.qsize(), _want)
    while not AF._EQ.empty():
        AF._EQ.get()

print("\n  ── ⑫i ⛔ B1：那 75 秒真的送得出幾張單（⛔ 實測，不准用算的）──")


class NeverFillAPI:
    """單送得出去但**永遠不成交**（範圍市價 IOC 沒撮到就是這個樣子）。"""

    def __init__(self, px):
        self.px, self.orders = px, []

    def place_order(self, contract, order):
        self.orders.append(_CLK["t"] - _CLK["t0"])
        return FakeTrade()

    def list_positions(self, acc=None):
        return [type("P", (), {"code": "TMFI6", "quantity": 1,
                               "direction": "Buy", "price": self.px})()]

    def update_status(self, acc=None):
        pass

    def cancel_order(self, t):
        pass

    def list_profit_loss(self, acc=None, d1=None, d2=None):
        return []


reset_eod()
live_on()
_nf = NeverFillAPI(12013.0)
connect(_nf)
broker._state["position"] = {"dir": "long", "entry": 12013.0, "qty": 1,
                             "entry_time": "09:03:31", "target_trade": None,
                             "recovered": False}
AF.FIRE_DIR.mkdir(parents=True, exist_ok=True)
AF._append({"rec": "result", "date": DAY, "stage": "done", "ok": True,
            "dir": "long", "entry": 12013.0, "entry_time": "09:03:31",
            "method": "B", "why": None})
_CLK = {"t": 1000000.0}
_CLK["t0"] = _CLK["t"]
_rt, _sl = time.time, time.sleep
_nclose = {"n": 0}
_realclose = broker.close


def _counted(reason):
    _nclose["n"] += 1
    return _realclose(reason)


try:
    time.time = lambda: _CLK["t"]
    time.sleep = lambda s: _CLK.__setitem__("t", _CLK["t"] + s)
    broker.close = _counted
    _r = AF._eod(DAY, 0, _CLK["t"])
    _dur = _CLK["t"] - _CLK["t0"]
finally:
    time.time, time.sleep, broker.close = _rt, _sl, _realclose
_last = _nf.orders[-1] if _nf.orders else None
chk("  平不掉 ⇒ 落地 eod_failed", (_r or {}).get("why"), "eod_failed")
chk("  ⛔ 實測 close() 被呼叫幾次", _nclose["n"], 8)
chk("  ⛔ 實測真的送出去幾張 IOC 平倉單", len(_nf.orders), 9)
say(abs(_dur - 78.2) < 1.0, "  ⛔ 實測整段耗時（秒）", "%.1f" % _dur)
say(LP.EOD_CLOSE_SEC + _last < LP.DAY_END_SEC,
    "  ⛔⛔ 最後一張單仍然在 13:45 收盤之前送出去（這個窗口的存在理由）",
    "13:43:30 + %.1f 秒 = %02d:%02d:%02d" % (
        _last, int((LP.EOD_CLOSE_SEC + _last) // 3600),
        int((LP.EOD_CLOSE_SEC + _last) % 3600 // 60),
        int((LP.EOD_CLOSE_SEC + _last) % 60)))
say(AF.EOD_WINDOW_S < _dur < LP.DAY_END_SEC - LP.EOD_CLOSE_SEC,
    "  ⚠️ 整段會**略微超出**窗口（最後一輪是在窗口內開始的），但仍在收盤之前",
    "窗口 %.0f 秒、實跑 %.1f 秒、離收盤 %d 秒" % (
        AF.EOD_WINDOW_S, _dur, LP.DAY_END_SEC - LP.EOD_CLOSE_SEC))
say("實測" in AF.__doc__ or "實測" in pathlib.Path(AF.__file__).read_text(
        encoding="utf-8")[:12000],
    "  ⛔ 檔頭那段設計備忘要寫實測值（⛔ 這個專案不准放沒量過的數字）")

# ══ ⑬ ⭐ 開難、關易：關得掉，而且**只能關不能開** ═════════════════════
print("\n=== ⑬ ⭐ 開難關易（面板上那顆「關閉」鈕）===")
reset_eod()
arm_write("A")
say(AF.arm()["on"] and AF.flag_exists(), "  前置：開關開著")
ok, msg = AF.disarm()
say(ok, "  按下去回報成功", msg)
say(not AF.ARM_FLAG.exists(), "  ⛔ 開關檔真的不在了")
say(AF.arm()["on"] is False and AF.arm()["why"] == "off",
    "  ⇒ 立刻變成「關閉中」", str(AF.arm()))
_kept = [p for p in AF.ARM_FLAG.parent.iterdir()
         if p.name.startswith(AF.ARM_FLAG.name + ".off-")]
chk("  是改名不是刪掉（他寫的內容留著）", len(_kept), 1)
chk("    內容真的留著", _kept[0].read_text(encoding="utf-8").strip(), "A")
ok2, msg2 = AF.disarm()
say(ok2 and not AF.ARM_FLAG.exists(),
    "  再按一次不會出錯，也不會把開關打開", msg2)
say(AF.state()["flag_exists"] is False,
    "  ⛔ 關著的時候 flag_exists 是 False（畫面據此把那顆鈕藏起來）")
say(AF.state()["off_msg"], "  關著時端得出「剛剛關掉了」那句", AF.state()["off_msg"])
arm_write("A")
say(AF.state()["off_msg"] is None and AF.state()["off_at"] is None,
    "  ⛔ 他又自己把檔案建回來之後那句就不見了（不然是一句假話）",
    str(AF.state()["off_msg"]))
AF.disarm()
arm_write("K線")
say(AF.arm()["on"] is False and AF.state()["flag_exists"] is True,
    "  ⚠️ 內容看不懂時：開不成（armed=False）但檔案還在 ⇒ 那顆鈕**還是要出現**",
    str(AF.state()["arm_why"]))
AF.disarm()
# ⛔⛔ 這一條是整個「開難」的結構性保證：**程式裡沒有任何一行會建立開關檔**
_arm_writes = []
for _n in ast.walk(_tree):
    if isinstance(_n, ast.Call) and isinstance(_n.func, ast.Attribute) \
            and _n.func.attr in ("write_text", "write_bytes", "touch", "open", "mkdir") \
            and isinstance(_n.func.value, ast.Name) and _n.func.value.id == "ARM_FLAG":
        _arm_writes.append(ast.unparse(_n)[:60])
chk("  ⛔⛔ auto_fire.py 裡沒有任何一行會建立／寫入 ARM_FLAG", _arm_writes, [])
_arm_attrs = sorted({_n.func.attr for _n in ast.walk(_tree)
                     if isinstance(_n, ast.Call) and isinstance(_n.func, ast.Attribute)
                     and isinstance(_n.func.value, ast.Name)
                     and _n.func.value.id == "ARM_FLAG"})
say(set(_arm_attrs) <= {"exists", "read_bytes", "replace", "with_name"},
    "  ⛔ 對 ARM_FLAG 只做「看在不在／讀／改名」三件事", str(_arm_attrs))
# ⛔ 這一條一定要用 AST 不可以搜字串 —— do_POST 的**註解本身**就在講
#    「沒有任何一行會建立 ARM_FLAG」，搜字串會把說明當成違規（【模擬】那一頁踩過）。
_dopost = next(n for n in ast.walk(ast.parse(LPSRC))
               if isinstance(n, ast.FunctionDef) and n.name == "do_POST")
say(not any(getattr(n, "attr", None) == "ARM_FLAG" or getattr(n, "id", None) == "ARM_FLAG"
            for n in ast.walk(_dopost)),
    "  ⛔ do_POST 裡不准直接碰 ARM_FLAG（一律走 disarm()）")
_dp = LPSRC.split("def do_POST")[1].split("def do_GET")[0]
say('"/api/fire/off"' in _dp, "  「關閉」那一顆的 POST 在")
say('"/api/fire/state"' not in _dp, "  ⛔ 唯讀那支仍然沒有 POST")
say("auto_fire.disarm()" in _dp and "auto_fire.arm(" in _dp,
    "  關閉那條路只呼叫 disarm()（⛔ 沒有任何「開啟」的函式可以呼叫）")
say('"/api/fire/arm"' not in LPSRC and '"/api/fire/method"' not in LPSRC,
    "  ⛔ 除了 on／off／state 之外沒有別的 fire 端點")

# ══ ⑬b ⭐⭐ 「打開」那一顆（2026-09-09 Benson 要求做在面板上）═══════════
#    ⛔⛔ 「開」現在打得開了，所以這一節守的是**開的那條路只有一個入口、
#       而且入口上每一道關卡都還在**。行為面（六道防護、mode 驗證、409、落地）
#       由 `test_fire_routes.py` ③b 真的起服務打進去驗；這裡守的是
#       **常數／文案／接線那半**（這個專案連五次的老形狀就是那半空白）。
print("\n=== ⑬b ⭐⭐ 打開那一顆（⛔ 唯一一個會建立開關檔的地方）===")
_lptree = ast.parse(LPSRC)
_arm_fn = next((n for n in ast.walk(_lptree)
                if isinstance(n, ast.FunctionDef) and n.name == "fire_arm_on"), None)
say(_arm_fn is not None, "  live_panel 有 fire_arm_on()（建開關檔的唯一入口）")
# ⛔⛔ 建檔這件事**只能在那一個函式裡**。同一把尺掃全檔：任何一個
#    `os.open(...)` / `open(..., "w")` 打在 ARM_FLAG 上的地方都要落在 fire_arm_on 裡。
_arm_lines = range(_arm_fn.lineno, (_arm_fn.end_lineno or _arm_fn.lineno) + 1) \
    if _arm_fn else range(0)
_creators = []
for _n in ast.walk(_lptree):
    if isinstance(_n, ast.Call) and "O_EXCL" in ast.unparse(_n):
        _creators.append((ast.unparse(_n)[:60],
                          getattr(_n, "lineno", -1) in _arm_lines))
say(len(_creators) == 1 and _creators[0][1],
    "  ⛔⛔ 整支 live_panel 只有一個地方在建立開關檔，而且它就在 fire_arm_on 裡",
    str(_creators))
say(_arm_fn is not None and "O_EXCL" in ast.unparse(_arm_fn),
    "  ⛔ 用的是 O_CREAT|O_EXCL ⇒ **結構上不可能蓋掉他已經有的那個檔**")
def _nodoc(fn):
    """⛔ 比程式碼要先把**說明**拿掉：這一節的說明本身就在講「用 O_EXCL」
       「不再問一次 broker.is_live()」—— 連說明一起比的話，那幾條斷言是恆真的
       （這個專案在【模擬】那一頁踩過同一個坑）。"""
    if fn is None:
        return ""
    body = [n for n in fn.body]
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    return "\n".join(ast.unparse(n) for n in body)


_armsrc = _nodoc(_arm_fn)
say("auto_fire.METHODS" in _armsrc,
    "  ⛔ mode 拿 auto_fire.METHODS 比（⛔ 不是自己寫死一份 A／B ＝兩把尺）")
say(("METHODS" in _armsrc and "os.O_EXCL" in _armsrc
     and _armsrc.index("METHODS") < _armsrc.index("os.O_EXCL")),
    "  ⛔⛔ 而且**先驗再寫**（驗完才碰檔案）")
# 行為面的兩條（⛔ 直接呼叫產品函式，不必起服務）
_probe_dir = TMP / "armprobe"
_probe_dir.mkdir(exist_ok=True)
_old_flag = AF.ARM_FLAG
AF.ARM_FLAG = _probe_dir / "AUTO_ORDERS_ON"
try:
    _c, _o = LP.fire_arm_on("C")
    say(_c == 400 and not AF.ARM_FLAG.exists(),
        "  ⛔ mode=C ⇒ 400，而且**一個檔都沒建**", f"{_c} {str(_o)[:60]}")
    _c, _o = LP.fire_arm_on("A")
    say(_c == 200 and AF.ARM_FLAG.read_bytes() == b"A",
        "  尺的自證：mode=A ⇒ 真的建出來，內容就是一個 ASCII 字母",
        f"{_c} {AF.ARM_FLAG.read_bytes()!r}")
    _c, _o = LP.fire_arm_on("B")
    say(_c == 409 and AF.ARM_FLAG.read_bytes() == b"A",
        "  ⛔⛔ 已經開著再按 ⇒ 409，⛔ 原本那個檔一個位元組都沒被動到",
        f"{_c} {AF.ARM_FLAG.read_bytes()!r}")
finally:
    AF.ARM_FLAG = _old_flag
    shutil.rmtree(_probe_dir, ignore_errors=True)
# ⛔ 確認條那句話：**兩種模式不可以寫同一句**（寫同一句就一定有一句是假的）
_cr = LP.fire_arm_confirm(True)
_cd = LP.fire_arm_confirm(False)
say(_cr["live"] is True and _cd["live"] is False and _cr["text"] != _cd["text"],
    "  ⛔ 真錢與演練兩句話不一樣", (_cr["text"][:24] + " ／ " + _cd["text"][:16]))
say(all(s in _cr["text"] for s in ("真實下單", "你的錢", LP.SIGNAL_AT))
    and ("%g" % LP.TP_POINTS) in _cr["text"],
    "  ⛔ 真錢那句要講「用你的錢」「幾點送」「幾點停利停損」", _cr["text"])
say("演練" in _cd["text"] and "不會真的送單" in _cd["text"]
    and "你的錢" not in _cd["text"],
    "  ⛔ 演練那句⛔ 不准出現「你的錢」（會嚇人，而且是假的）", _cd["text"])
say("broker.is_live()" not in _nodoc(next(
    (n for n in ast.walk(_lptree)
     if isinstance(n, ast.FunctionDef) and n.name == "fire_arm_confirm"), None)),
    "  ⛔ 那句話用的是傳進來的 live（⛔ 不再問一次 broker ＝ 兩把尺）")
# 路由：⛔ 精確比對（放寬＝多開一批沒人審過的入口，跟 Q12 同一個形狀）
say('if self.path == "/api/fire/on":' in _dp,
    "  ⛔ 打開那條路由是**精確比對**（⛔ 不是 startswith／in）")
# ⛔⛔ 【P0，2026-09-09 lab-qa】守衛套在 `do_POST` 的**入口**，不是逐條路由各自套。
#    ⚠️ 這一條**一定要用 AST**：`do_POST` 的註解本身就在講 `fire_post_guard`，
#       搜字串的話「把那次呼叫刪掉」照樣綠（R1 的同一個形狀）。
_gcalls = [n for n in ast.walk(_dopost)
           if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "fire_post_guard"]
chk("  ⛔ do_POST 真的呼叫 fire_post_guard()（AST，⛔ 不是搜字串）", len(_gcalls), 1)
_routes_in_post = [n.lineno for n in ast.walk(_dopost)
                   if isinstance(n, ast.Compare) and "self.path ==" in ast.unparse(n)]
say(bool(_gcalls) and bool(_routes_in_post)
    and _gcalls[0].lineno < min(_routes_in_post),
    "  ⛔⛔ 而且它在**第一條路由之前** ⇒ 每一個 POST 都會過（⛔ 不可能有漏掉的端點）",
    f"guard@{_gcalls[0].lineno if _gcalls else '?'} "
    f"vs 第一條路由@{min(_routes_in_post) if _routes_in_post else '?'}")
_guard = next((n for n in ast.walk(_lptree)
               if isinstance(n, ast.FunctionDef) and n.name == "fire_post_guard"), None)
# ⛔⛔ 2026-09-09 R1 盤查抓到的第三個恆真守衛：這裡本來是 `ast.unparse(_guard)`，
#    **而 `ast.unparse` 會把 docstring 一起吐出來** —— 那份 docstring 逐字列著
#    Content-Type／X-Panel／Origin／X-Panel-Token／Sec-Fetch-Site／Host／compare_digest
#    ⇒ **把六道檢查整組刪掉，下面這 7 條照樣全綠**。一律走 `_nodoc()`。
_gsrc = _nodoc(_guard) + "\n" + _nodoc(next(
    (n for n in ast.walk(_lptree)
     if isinstance(n, ast.FunctionDef) and n.name == "_fire_browser_guard"), None))
for _h in ("Content-Type", "X-Panel", "Origin", "X-Panel-Token",
           "Sec-Fetch-Site", "Host"):
    say(_h in _gsrc, f"  ⛔ 防護還在：{_h}")
say("FIRE_TOKEN" in _gsrc and "compare_digest" in _gsrc,
    "  ⛔ token 用 compare_digest 比（不是 ==）")
# ⛔⛔ 2026-09-09 lab-qa 退件 R1：這一條本來是比原始碼字串，而 `do_GET` 的註解裡
#    就有 `/api/fire/on` 與 `405` ⇒ 把真正那兩行整個刪掉照樣綠（lab-qa 實測）。
#    改成**真的送請求進去**。
for _m in ("GET", "HEAD"):
    _c, _ct, _b = hit("/api/fire/on", _m)
    say(_c == 405,
        f"  ⛔ {_m} /api/fire/on ⇒ 405（**真的送一個請求進去**，⛔ 不是比字串）"
        "　一個 <img src> 不可以幫他打開", str(_c))
    say(not AF.ARM_FLAG.exists(), f"    ⇒ ⛔ 而且開關檔沒有被建出來（{_m}）")
_c, _ct, _b = hit("/api/fire/on?mode=A", "GET")
say(_c == 405, "  ⛔ 帶查詢字串的 GET 也是 405", str(_c))
# 尺的自證：⛔ POST（帶齊標頭）**不是** 405 —— 不然「整個端點不見了」也會全綠
_c, _ct, _b = hit("/api/fire/on", "POST", b'{"mode":"C"}',
                  {"Content-Type": "application/json", "X-Panel": "1",
                   "X-Panel-Token": LP.FIRE_TOKEN})
say(_c == 400 and "做法" in (_b or ""),
    "  尺的自證：帶齊標頭的 POST 走進 mode 檢查（⇒ 上面那幾條 405 不是恆真）",
    f"{_c} {(_b or '')[:50]}")
say(not AF.ARM_FLAG.exists(), "    ⇒ ⛔ 開關檔仍然沒有被建出來")

# ⛔⛔ 前端那半（畫面／接線）—— 端到端由 `tools/probe/fire-tab.mjs` ⑪ 量，
#    這裡是純 Python 這一層，兩層都要有（Q9 那次就是這一層整片空白）。
_alon = page[page.index("function alOnHTML"):page.index("function alArm")]
_alarm = page[page.index("function alArm"):page.index("document.addEventListener('click'",
                                                      page.index("function alArm"))]
say("if(D.flag_exists) return ''" in _alon,
    "  ⛔ 已經開著的時候不畫「打開」（⛔ 開與關不可以同時在畫面上）")
# ⛔ 先剝註解再比：alOnHTML 與 alArm 之間的那段**說明文字**現在就含「pfetch()」
#    ⇒ 連註解一起比的話這一條會變成假紅（同一個坑的反面）。
_alon_nc = _re.sub(r"(?m)^\s*//.*$", " ",
                   _re.sub(r"/\*.*?\*/", " ", _alon, flags=_re.S))
say("fetch(" not in _alon_nc,
    "  ⛔⛔ 第一段（兩顆做法鈕）那一段**一個 fetch 都沒有**（沒確認就不准送）")
# ⚠️ ⛔ 比的是 `esc(C.text)`（**真的畫出去的那一段**）不是 `C.text` ——
#    `typeof C.text!=='string'` 那道防呆本身就含 "C.text"，比 "C.text" 的話
#    「把文案改成前端寫死」那個突變**打不紅**（2026-09-09 fire-mutate Ⓝ7 實測）。
say("esc(C.text)" in _alon and "arm_confirm" in _alon,
    "  ⛔ 確認條那句話是從後端拿的（⛔ 前端不准自己猜真錢／演練）")
say("al-conf real" in _alon or "' real'" in _alon,
    "  ⛔ 真錢那一條有自己的樣子（⛔ 兩種模式不可以長一樣）")
say("'/api/fire/on'" in _alarm and "pfetch(" in _alarm,
    "  ⛔ 第二段才送請求，而且走的是共用的 pfetch()（⛔ 不自己寫一份標頭）")
say("ALON.step='idle'" in page and "ALON.step='confirm'" in page,
    "  兩段式的狀態機在（idle ↔ confirm）")
_alent = page[page.index("function alEnter"):page.index("function alLoop")]
say("ALON.step='idle'" in _alent,
    "  ⛔ 切進這一頁一定回到「未確認」（⛔ 不可以留著上次展開到一半的確認條）")
# ⛔⛔ 那顆鈕的**顯示條件**（2026-09-09 lab-qa 的 Q9：純 Python 這一層完全空白）：
#    要看 `flag_exists`（檔案在不在），⛔ 不可以看 `armed`。
#    他把開關檔存成 UTF-16／打成 `K線` 的時候 armed 是 False 但檔案還在 ——
#    看 armed 的話那顆鈕會消失 ⇒ **壞掉的開關檔他關不掉**，而「關」是安全方向。
#    ⚠️ 這一條只能比前端原始碼（那是瀏覽器才跑得到的一行），
#      真正端到端量它的是 `tools/probe/fire-tab.mjs` ⑧c —— 兩層都要有。
_alp0 = LPSRC.rindex("setEl('aloff',")
_alpaint = LPSRC[_alp0:LPSRC.index("setEl('alcount'", _alp0)]
say("D.flag_exists" in _alpaint,
    "  ⛔ 「關閉」鈕的顯示條件用的是 flag_exists（檔案在不在）")
say("D.armed" not in _alpaint,
    "  ⛔⛔ 而且**沒有**用 armed（開關檔壞掉時 armed=False ⇒ 那顆鈕會消失、關不掉）",
    _alpaint[:80].replace("\n", " "))
# ⛔ 條件**只能是這一個**，不准加料（`true||D.flag_exists`、`D.armed||D.flag_exists`…）：
#    加料之後「關著的時候整頁 0 顆按鈕」那條鐵律就破了，而上面兩項照樣綠。
_alcond = _alpaint.split("setEl('aloff',", 1)[1].split("?", 1)[0].strip()
chk("  ⛔ 那個條件逐字就是 D.flag_exists（⛔ 不准 || 也不准 &&）",
    _alcond, "D.flag_exists")

# ══ ⑬c ⭐⭐ 確認條那句話：「今天」還是「下一個交易日」（2026-09-09 退件 R2）═══
#    ⚠️ 原本寫死「**下一個交易日** 09:03:30」，但 `auto_fire` **沒有「今天開的不算」
#       的閘門** ⇒ 他 08:50 按下去，13 分鐘後今天就送一口真單，而畫面說是明天。
#    ⭐ Benson 裁示：**改文案、不加閘門**（「按了就開始」才是他要的行為）。
#    ⛔⛔ 這一節守的是「兩邊同一把尺」：`fire_fires_today()` 的答案必須跟
#       **真的驅動 `_auto_tick()` 走一遍那一天**的結果逐一相同。
print("\n=== ⑬c ⭐⭐ 「今天 09:03:30」還是「下一個交易日 09:03:30」===")
_DT = datetime.datetime


def _fire_today_pair(t0):
    """回 (那句話說的, `_auto_tick` 真的會做的) —— ⛔ **兩邊在同一個世界裡問**。

    ⛔ 用**產品自己的 `_auto_tick()`** 走一遍那一天（面板 08:00 起一直開著），
       記下 09:03:30 那一刻的掛勾是在哪個時刻被呼叫的。

    ⚠️⚠️ **2026-09-10 PM 裁示，修的是測試的模型、⛔ 沒有動產品**（原本 2 項紅）：
      1. **兩邊共用同一份 `AUTO`**。舊版先在「AUTO 沒 done」的世界問
         `fire_fires_today()`，再拿「面板 08:00 一路開著（09:03:30 早就送過了）」
         的模擬當答案 —— **兩邊在講不同的世界**，09:03:31 那一格必紅。
         現在是：先把 t0 **之前**的每一秒餵給 `_auto_tick()`（＝他按下去的那一刻，
         面板已經走到哪就是哪），**在那個狀態下**問那句話，再繼續跑完剩下的時間。
         ⛔ t0 那一格**要留在後面跑**：他按下去的時候，那一秒的 tick 還沒輪到
         （HTTP 執行緒與 4Hz 主迴圈是併行的）。
      2. **比較方式跟 `_auto_tick()` 一致：`>=`，不是 `>`**。
         舊版寫「嚴格晚於 t0」＝ 一個保守約定，但**產品在那一秒真的會送**
         ⇒ 正確答案是「今天」。⛔ 測試要模擬的是產品真正在做的事，
         不是我們希望它做的事。
    """
    fired, cur = [], [None]
    _old_hook = LP.AUTO_SIG_HOOK
    _old_auto = dict(LP.AUTO)
    # ⛔ 「真的會送」＝ 掛勾拿到 **snap**。`_auto_tick` 在「太晚了」那條路也會呼叫掛勾
    #    （`AUTO_SIG_HOOK(None, d, lag)` ＝ 記一列 late、**不送**）——
    #    只數「掛勾被呼叫過」會把「跳過」算成「送了」。
    LP.AUTO_SIG_HOOK = lambda snap, d, lag: (fired.append(cur[0])
                                             if snap is not None else None)
    LP.AUTO.update({"started": True, "day": None, "done": False, "settled": False,
                    "eod": False, "gaps": 0.0, "queued": set()})
    try:
        t = _DT.combine(t0.date(), datetime.time(8, 0, 0))
        end = _DT.combine(t0.date(), datetime.time(0, 0)) + \
            datetime.timedelta(seconds=LP.SIGNAL_SEC + 5)
        while t <= end and t < t0:           # ── 他按下去**之前**的每一秒
            cur[0] = t
            LP._auto_tick(None, t, LP.market_session(t))
            t += datetime.timedelta(seconds=1)
        said = LP.fire_fires_today(t0)       # ⛔ 就在這個狀態下問那句話
        while t <= end:                      # ── 剩下的時間照跑
            cur[0] = t
            LP._auto_tick(None, t, LP.market_session(t))
            t += datetime.timedelta(seconds=1)
    finally:
        LP.AUTO_SIG_HOOK = _old_hook
        LP.AUTO.clear()
        LP.AUTO.update(_old_auto)
        while not LP._AUTO_Q.empty():        # ⛔ 別把模擬產生的東西留在佇列裡
            try:
                LP._AUTO_Q.get_nowait()
            except Exception:
                break
    return said, any(ts >= t0 for ts in fired)


# 週五 2026-09-11／週六 09-12／週日 09-13／週一 09-14（⛔ 寫死的日子，跟今天無關）
_R2 = [
    ("週一 08:00（開盤前）", _DT(2026, 9, 14, 8, 0, 0)),
    ("週一 08:50（他真的會按的時間）", _DT(2026, 9, 14, 8, 50, 0)),
    ("週一 09:03:29（差一秒）", _DT(2026, 9, 14, 9, 3, 29)),
    ("週一 09:03:30（剛好那一秒）", _DT(2026, 9, 14, 9, 3, 30)),
    ("週一 09:03:31（過了一秒）", _DT(2026, 9, 14, 9, 3, 31)),
    ("週一 10:30（盤中）", _DT(2026, 9, 14, 10, 30, 0)),
    ("週一 20:00（夜盤）", _DT(2026, 9, 14, 20, 0, 0)),
    ("週五 08:50", _DT(2026, 9, 11, 8, 50, 0)),
    ("週六 08:50（⛔ 不是交易日）", _DT(2026, 9, 12, 8, 50, 0)),
    ("週日 08:50（⛔ 不是交易日）", _DT(2026, 9, 13, 8, 50, 0)),
]
_r2_true = 0
for _name, _t in _R2:
    _said, _real = _fire_today_pair(_t)      # ⛔ 同一個 AUTO 世界問出來的兩個答案
    _r2_true += bool(_real)
    chk(f"  {_name}：那句話說的 ＝ _auto_tick 真的會做的", _said, _real)
# ⛔ 尺的自證：這一組裡**兩種答案都出現過**（不然「永遠回 False」也全綠）
say(0 < _r2_true < len(_R2),
    "  ⛔ 尺的自證：這一組時間點裡「今天會送」與「不會送」都出現過",
    f"{_r2_true}/{len(_R2)} 會送")


# ── ⛔⛔ 「看門狗剛重啟」那個世界（`AUTO` 還沒 done）─────────────────────
#    ⚠️⚠️ 上面那一組模擬的是「面板 08:00 一路開著」⇒ 09:03:30 一到 `AUTO["done"]`
#       就是 True、之後永遠由條件 ① 擋下來 ⇒ **那一組結構上驗不到條件 ② 的補送窗口**
#       （`>= SIGNAL_SEC + AUTO_LATE_MS/1000`，2026-09-10 上午加的那一段）。
#       ⛔ 別以為上面那組有守到它 —— 把 `+ AUTO_LATE_MS/1000` 拿掉，上面 10 項全綠。
#    這一段把世界換成「面板在 09:03:3x 才剛起來（`connect()` 卡了幾秒）」，
#    那正是那一段程式要修的情境：畫面說「下一個交易日」，但它今天就會送。
def _restart_pair(t0):
    """回 (那句話說的, `_auto_tick` 真的會送嗎) —— 面板剛起來、今天還沒走過那一刻。"""
    got = []
    _oh, _oa = LP.AUTO_SIG_HOOK, dict(LP.AUTO)
    LP.AUTO_SIG_HOOK = lambda snap, d, lag: got.append(snap is not None)
    LP.AUTO.update({"started": True, "day": None, "done": False, "settled": False,
                    "eod": False, "gaps": 0.0, "queued": set()})
    try:
        said = LP.fire_fires_today(t0)       # 他就在這一刻按下去
        LP._auto_tick(None, t0, LP.market_session(t0))
    finally:
        LP.AUTO_SIG_HOOK = _oh
        LP.AUTO.clear()
        LP.AUTO.update(_oa)
        while not LP._AUTO_Q.empty():
            try:
                LP._AUTO_Q.get_nowait()
            except Exception:
                break
    return said, any(got)


# ⚠️ 邊界（`fire_fires_today` 的說明裡也寫了）：`_auto_tick` 的判準是
#    `lag > AUTO_LATE_MS`（lag 剛好 3000ms 還是會送），而那句話只看得到「秒」⇒
#    **`09:03:33.000` 那一個瞬間**兩邊是不一致的。4Hz 的迴圈要剛好落在微秒 0
#    才踩得到，⛔ 不要為了它把整個 09:03:33 那一秒都說成「今天」
#    （那一秒其餘 999ms 其實是 late ⇒ 反過來又變成另一句假話）。
#    ⇒ 這一組**刻意跳過那一個瞬間**，並用 `.001` 與 `.999` 把兩邊都夾住。
_R3 = [
    ("剛重啟・09:03:30.000（那一刻才起來）", _DT(2026, 9, 14, 9, 3, 30)),
    ("剛重啟・09:03:31（補送窗口內 lag=1000ms）", _DT(2026, 9, 14, 9, 3, 31)),
    ("剛重啟・09:03:32.999（窗口的最後一刻 lag=2999ms）",
     _DT(2026, 9, 14, 9, 3, 32, 999000)),
    ("剛重啟・09:03:33.001（過了窗口 lag=3001ms ⇒ 記 late 不送）",
     _DT(2026, 9, 14, 9, 3, 33, 1000)),
    ("剛重啟・09:03:40（早就過了 ⇒ 記 late 不送）", _DT(2026, 9, 14, 9, 3, 40)),
]
_r3_true = 0
for _name, _t in _R3:
    _said, _real = _restart_pair(_t)
    _r3_true += bool(_real)
    chk(f"  {_name}：那句話說的 ＝ _auto_tick 真的會做的", _said, _real)
say(0 < _r3_true < len(_R3),
    "  ⛔ 尺的自證：這一組裡「今天真的會送」與「不會送」都出現過",
    f"{_r3_true}/{len(_R3)} 會送")

# ⛔ 第三個條件（`AUTO["done"]`）：上面那組模擬每次都把它重置了，所以單獨驗一次。
#    這是「面板 09:10 才被看門狗重開」那個真實情境。
_mon = _DT(2026, 9, 14, 8, 50, 0)
_old_auto = dict(LP.AUTO)
try:
    LP.AUTO.update({"day": None, "done": False})
    chk("  尺的自證：週一 08:50、今天還沒走過那一刻 ⇒ 今天",
        LP.fire_fires_today(_mon), True)
    LP.AUTO.update({"day": "2026-09-14", "done": True})
    chk("  ⛔ 今天已經走過 09:03:30 了（面板 09:10 才重開）⇒ 下一個交易日",
        LP.fire_fires_today(_mon), False)
    LP.AUTO.update({"day": "2026-09-11", "done": True})
    chk("  ⛔ 那個 done 是**別天**的 ⇒ 不算（⛔ 不可以只看 done）",
        LP.fire_fires_today(_mon), True)
finally:
    LP.AUTO.clear()
    LP.AUTO.update(_old_auto)

# ── 那句話本身
for _live in (True, False):
    _t1 = LP.fire_arm_confirm(_live, _DT(2026, 9, 14, 8, 50, 0))
    _t2 = LP.fire_arm_confirm(_live, _DT(2026, 9, 14, 10, 30, 0))
    _tag = "真錢" if _live else "演練"
    say("今天 " + LP.SIGNAL_AT in _t1["text"] and "下一個交易日" not in _t1["text"],
        f"  ⛔ {_tag}・盤前按 ⇒ 那句話說「今天 {LP.SIGNAL_AT}」", _t1["text"])
    say("下一個交易日 " + LP.SIGNAL_AT in _t2["text"] and "今天" not in _t2["text"],
        f"  ⛔ {_tag}・盤後按 ⇒ 那句話說「下一個交易日 {LP.SIGNAL_AT}」", _t2["text"])
# ⛔ 時刻的正本只有 SIGNAL_AT 一個（⛔ 那句話裡不准出現寫死的 09:03:30）
say("09:03:30" not in _nodoc(next(
    (n for n in ast.walk(_lptree)
     if isinstance(n, ast.FunctionDef) and n.name == "fire_arm_confirm"), None)),
    "  ⛔ 那句話裡沒有寫死的時刻（⛔ 一律走 SIGNAL_AT）")
say("SIGNAL_SEC" in _nodoc(next(
    (n for n in ast.walk(_lptree)
     if isinstance(n, ast.FunctionDef) and n.name == "fire_fires_today"), None))
    and "market_session" in _nodoc(next(
        (n for n in ast.walk(_lptree)
         if isinstance(n, ast.FunctionDef) and n.name == "fire_fires_today"), None)),
    "  ⛔⛔ 判斷用的是 SIGNAL_SEC ＋ market_session（＝_auto_tick 那把尺），"
    "⛔ 不是自己寫的 weekday()")

# ══ ⑬d ⭐⭐ 前端：會改變狀態的 POST **只有一個出口**（pfetch）═══════════
#    ⛔⛔ 【P0】後端現在每一個 POST 都要標頭與 token，前端漏一個呼叫點
#       ＝ 他的某一顆鈕從此按不動（畫面上只寫「送不出去」，看不出是自己人擋的）。
print("\n=== ⑬d ⭐⭐ 前端每一個 POST 都走同一個出口（pfetch）===")
# ⛔ 先把 JS 註解剝掉再比（`/* … */` 與 `// …`）—— 這一節講的就是這些字，
#    連註解一起比的話又是一條恆真守衛（R1 那個形狀）。
# ⚠️ `page` 是「從 PAGE 開始到檔尾」，⛔ 含 PAGE 之後的 **Python 程式碼**
#    （`do_POST` 的註解裡就寫著 `method:'POST'`）⇒ 一定要先切到 PAGE 這個字串為止，
#    不然「前端只有一個 POST 出口」那條會被後端的一句註解弄成假紅。
_page_nc = page[:page.index('</html>"""')]
_page_nc = _re.sub(r"/\*.*?\*/", " ", _page_nc, flags=_re.S)
_page_nc = _re.sub(r"(?m)^\s*//.*$", " ", _page_nc)
_posts_js = _re.findall(r"method:'POST'", _page_nc)
chk("  ⛔⛔ 整份前端只有**一個**地方寫 method:'POST'（那就是 pfetch）",
    len(_posts_js), 1)
_pf = _page_nc[_page_nc.index("function pfetch("):
               _page_nc.index("}", _page_nc.index("body:body||'{}'"))]
for _h in ("'Content-Type':'application/json'", "'X-Panel':'1'",
           "'X-Panel-Token':PTOK"):
    say(_h in _pf, f"  ⛔ pfetch 帶得出 {_h}")
# ⛔⛔ token 有**兩個**更新點，職責不同、⛔ 不可以用同一條斷言含混過去
#    （2026-09-09 fire-mutate Ⓝ5d 實測：只比「整份前端有沒有這個字串」的話，
#     把 `tick()` 那一個拿掉照樣綠 —— 因為 `ptok()` 裡有一模一樣的一行）。
_tick0 = _page_nc.index("async function tick(nf)")
_tickfn = _page_nc[_tick0:_page_nc.index("LASTS=s;", _tick0) + 9]
_ptokfn = _page_nc[_page_nc.index("function ptok("):_page_nc.index("function pfetch(")]
say("PTOK=s.token" in _tickfn.replace(" ", ""),
    "  ⛔⛔ tick()：token 跟著每 0.5 秒的 /api/state 換新"
    "（⛔ 少了這行，看門狗重啟後他的平倉鈕會 403 按不動）")
say("PTOK=s.token" in _ptokfn.replace(" ", ""),
    "  ⛔ ptok()：還沒拿到 token 時先去要一次（畫面剛開就按下去也按得動）")
chk("  ⛔ 整份前端**剛好兩個**地方會寫 PTOK（⛔ 不多不少）",
    _page_nc.count("PTOK=s.token"), 2)
# ⛔ 每一個會改變狀態的端點都要有人呼叫 pfetch（⛔ 少一個 ＝ 那顆鈕壞了）
for _ep in ("/api/real/enter", "/api/real/close", "/api/note", "/api/replay",
            "/api/fire/on", "/api/fire/off"):
    say(f"pfetch('{_ep}'" in _page_nc, f"  ⛔ 前端用 pfetch 打 {_ep}")
say("pfetch(url,body)" in _page_nc.replace(" ", ""),
    "  ⛔ 練習那幾顆（/api/enter・/api/close・/api/undo）也走 pfetch")

# ══ ⑭ ⛔ 他第一次建那個檔的每一種寫法（BOM／UTF-16）═════════════════
print("\n=== ⑭ 開關檔的編碼：他第一次一定會用的那幾種寫法 ===")
_cases = [
    ("純 A", b"A", True, "A"),
    ("A + LF", b"A\n", True, "A"),
    ("A + CRLF（Windows 記事本）", b"A\r\n", True, "A"),
    ("小寫 a", b"a", True, "A"),
    ("前後有空白", b"  B  \r\n", True, "B"),
    ("UTF-8 with BOM（記事本另存選錯）", b"\xef\xbb\xbfA", True, "A"),
    ("UTF-8 with BOM + CRLF", b"\xef\xbb\xbfB\r\n", True, "B"),
    ("UTF-16LE + BOM（PowerShell 的 \"A\" > 檔 / Out-File）",
     "A\r\n".encode("utf-16-le") and b"\xff\xfe" + "A\r\n".encode("utf-16-le"), True, "A"),
    ("UTF-16BE + BOM", b"\xfe\xff" + "B\r\n".encode("utf-16-be"), True, "B"),
    ("UTF-16LE 沒有 BOM", "A\r\n".encode("utf-16-le"), True, "A"),
]
for name, raw, want_on, want_m in _cases:
    AF.ARM_FLAG.write_bytes(raw)
    a = AF.arm()
    say(a["on"] is want_on and a["method"] == want_m,
        f"  {name} ⇒ 讀得懂（{want_m}）",
        f'on={a["on"]} method={a["method"]} raw={a["raw"]!r}')
_bad = [("空的", b""), ("C", b"C"), ("D", b"D"), ("AB", b"AB"),
        ("亂碼", b"\x81\x40\x81\x41"), ("BOM + C", b"\xef\xbb\xbfC"),
        ("UTF-16 的 C", b"\xff\xfe" + "C".encode("utf-16-le"))]
for name, raw in _bad:
    AF.ARM_FLAG.write_bytes(raw)
    a = AF.arm()
    say(a["on"] is False and len(a["msg"] or "") > 6,
        f"  ⛔ {name} ⇒ 拒絕下單並講得出讀到什麼", a["msg"])
AF.ARM_FLAG.write_bytes(b"\xff\xfe" + "C".encode("utf-16-le"))
say("C" in (AF.arm()["msg"] or "") and "只支援" in (AF.arm()["msg"] or ""),
    "  ⛔ UTF-16 的 C 也要認出「這是 C」，不可以只說看不懂", AF.arm()["msg"])
arm_clear()

# ══ ⑮ ⛔ fire_sim_pairs() 是唯讀的（呼叫前後雜湊比對）═══════════════
print("\n=== ⑮ ⛔ fire_sim_pairs() 唯讀 autotest/（前後雜湊比對）===")
import hashlib as _hl                                                # noqa: E402

LP.AUTO_DIR.mkdir(parents=True, exist_ok=True)
for _p in LP.AUTO_DIR.glob("*.jsonl"):
    _p.unlink()
LP.AUTO_CACHE.clear()
with (LP.AUTO_DIR / (DAY[:7] + ".jsonl")).open("a", encoding="utf-8") as _f:
    _f.write(json.dumps({"rec": "sig", "date": DAY, "src": "live", "at": "09:03:30.100",
                         "px": 12010.0, "sig": {"A": 5.0, "B": 10.0},
                         "dirs": {"A": 1, "B": 1, "C": 0, "D": 1},
                         "thresh": 30.0}, ensure_ascii=False) + "\n")


def _dir_hash(d):
    """那個資料夾現在的樣子（檔名＋內容）。"""
    h = _hl.sha256()
    for p in sorted(pathlib.Path(d).rglob("*")):
        h.update(str(p.relative_to(d)).encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


def _cache_hash():
    """⛔ 連**快取裡那批 dict** 也要比：`_auto_read()` 回的是同一批物件（淺拷貝），
    哪天有人在 fire_sim_pairs 裡加一行寫入，磁碟不會變、但模擬那一頁的快取會被污染。"""
    recs, led = LP._auto_read()
    return _hl.sha256(json.dumps([recs, led], sort_keys=True, default=str,
                                 ensure_ascii=False).encode()).hexdigest()


_h0, _c0 = _dir_hash(LP.AUTO_DIR), _cache_hash()
_pairs = LP.fire_sim_pairs([{"date": DAY}])
_h1, _c1 = _dir_hash(LP.AUTO_DIR), _cache_hash()
say(bool(_pairs.get(DAY)), "  尺的自證：真的讀到那一天了（不是回空的）", str(list(_pairs)))
chk("  ⛔ 磁碟上的 autotest/ 一個位元組都沒變", _h1, _h0)
chk("  ⛔ 記憶體快取裡那批 dict 也一個欄位都沒變", _c1, _c0)
# 尺的自證：真的動一下，那兩把尺要抓得到
_pairs2 = LP.fire_sim_pairs([{"date": DAY}])
LP._auto_read()[0][DAY]["px"] = 99999.0
say(_cache_hash() != _c0, "  尺的自證：真的改一個欄位，快取雜湊會變（⇒ 這把尺是活的）")
(LP.AUTO_DIR / "__tmp__canary.jsonl").write_text("x", encoding="utf-8")
say(_dir_hash(LP.AUTO_DIR) != _h0, "  尺的自證：資料夾多一個檔，磁碟雜湊會變")
(LP.AUTO_DIR / "__tmp__canary.jsonl").unlink()
LP.AUTO_CACHE.clear()
_srcs = sorted({n.attr for n in ast.walk(ast.parse(
    ast.unparse(next(n for n in ast.walk(ast.parse(LPSRC))
                     if isinstance(n, ast.FunctionDef) and n.name == "fire_sim_pairs"))))
    if isinstance(n, ast.Attribute)})
say(not any(a in _srcs for a in ("write_text", "write_bytes", "mkdir", "unlink")),
    "  ⛔ AST：fire_sim_pairs 裡沒有任何寫檔呼叫", str(_srcs))

# ══ ⑪ ⛔ 全程沒有指回真的資料夾 ════════════════════════════════════════
print("\n=== ⑪ ⛔ 全程沒有寫到他真的資料夾 ===")
now_paths = {"broker.ORDER_DIR": broker.ORDER_DIR, "broker.TRADE_DIR": broker.TRADE_DIR,
             "broker.REAL_FLAG": broker.REAL_FLAG, "AF.ARM_FLAG": AF.ARM_FLAG,
             "AF.FIRE_DIR": AF.FIRE_DIR, "LP.AUTO_DIR": LP.AUTO_DIR,
             "LP.AUTO_REAL_DIR": LP.AUTO_REAL_DIR}
for k, real in REAL_PATHS.items():
    say(now_paths[k] != real and str(TMP) in str(now_paths[k]),
        f"  {k} 全程都在暫存區", str(now_paths[k]))
say(not REAL_PATHS["AF.ARM_FLAG"].exists(),
    "  ⛔⛔ 真的 AUTO_ORDERS_ON **不存在**（測試絕對不可以把它建出來）")
say(not REAL_PATHS["AF.FIRE_DIR"].exists() or
    not any(REAL_PATHS["AF.FIRE_DIR"].iterdir()),
    "  真的 autofire/ 沒有被寫進東西")

live_off()
arm_clear()
_SRV.shutdown()          # ⛔ 行為面那把量尺的服務先收掉，再刪暫存區
shutil.rmtree(TMP, ignore_errors=True)
print("\n" + ("全部通過 ✅" if FAIL == 0 else f"⛔ 有 {FAIL} 項沒過"))
sys.exit(1 if FAIL else 0)
