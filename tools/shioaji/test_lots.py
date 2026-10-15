# -*- coding: utf-8 -*-
"""
口數（多口，`lots.py`＋broker／auto_fire／night_fire／risk_cap 的多口那一半）的離線探針（2026-10-14）。

**不連永豐、不送任何單、不碰他的資料。** 全程假券商；每一個會寫檔的出口都導到暫存區，
收尾一節斷言「全程沒有指回真的資料夾」（2026-09-01 真的寫進過他的成績單）。

在守的事：
  ①  沒有狀態檔 ⇒ 1＋1；狀態檔壞掉 ⇒ 1＋1（⛔ 不覆蓋那個檔）
  ②  ⛔⛔ 沒有狀態檔時送出的每一張單，跟改版前（git HEAD 的 broker／auto_fire／night_fire）**逐位元相同**
  ③  各級口數（goal.LADDER 是唯一正本）、單條降級 cap、已確認升級的生效日
  ④  多口送單：N 口全成交／部分成交（接受 k 口、⛔ 不追單）／口數不合理不送／平倉平到 0
      ／停利部分成交（剩下的照樣停損、平倉、記帳）／撤停利之前又成交（⛔ 不平過頭）
  ⑤  auto_fire 多口：送單口數、帳本口數、收盤平倉認人（部分成交後還是我們的；比帳本多 ⇒ 不碰）
  ⑥  風控 B：400 ×（本月用過最大 a＋b），月中減級不掉、換月重算
  ⑦  減級（自動）：權益 < 門檻 × 0.85、單條降級、停損失靈事故暫停升級
  ⑧  升級只建議不寫入；apply 只收「這個月建議的那一級」、當下重驗、下一個交易日生效、生效前可撤回；
      條件掉了收回建議；7 萬那一級不算升級；冷卻從生效日算；不滿 30 筆用現有的
  ⑨  夜盤波動預測（跟研究 sizing.py 的 forecast 同一條式子）、規則 B（1＋1 時每晚 1 口）
  ⑩  live_panel.equity_view：保證金看 max(a, b) 口；1＋1 時那句話一個字都沒變
用法：python test_lots.py（PowerShell：先設 $env:PYTHONIOENCODING="utf-8"）
⚠️ 環境變數 `MUT_DIR`：突變測試用（指到放在暫存區的突變版模組，⛔ 絕不把壞版本寫進 tools/shioaji）。
"""
import json
import math
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_MUT = os.environ.get("MUT_DIR")
if _MUT:
    sys.path.insert(0, _MUT)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import broker            # noqa: E402
import lots              # noqa: E402
import risk_cap          # noqa: E402
import auto_fire as AF   # noqa: E402
import night_fire as NF  # noqa: E402
import goal              # noqa: E402
import shioaji as sj     # noqa: E402
# ⚠️ live_panel 一定要在這裡就載（⛔ 不可以等到 ⑩）：analyst_facts 被 import 時會把 tools/shioaji 插到 sys.path 最前面，
#    之後再 import 就會繞過 MUT_DIR ⇒ 前端那幾條突變結構上打不紅（2026-10-14 突變 P6 實測踩到）。
import live_panel as LP  # noqa: E402

FAIL = 0


def chk(name, got, want):
    global FAIL
    ok = got == want
    FAIL += not ok
    print(("  OK   " if ok else "  FAIL ") + name + ("" if ok else "  (得到 %r，期待 %r)" % (got, want)))


def say(cond, name, extra=""):
    global FAIL
    FAIL += not cond
    print(("  OK   " if cond else "  FAIL ") + name + (("  " + str(extra)) if extra else ""))


def _boom(kind, err, tb):
    """測試自己掛掉也要印一項 FAIL ＋ 總結行（突變測試只數 FAIL 行，掛掉不可以被記成打不紅）。"""
    import traceback
    traceback.print_exception(kind, err, tb)
    print("  FAIL ⛔ 測試自己掛掉了（未捕捉的例外）：" + str(err)[:120])
    print("⛔ 有 ? 項沒過（測試中斷）")
    sys.stdout.flush()


sys.excepthook = _boom

# ══ 寫檔出口：一次導完（先記下原路徑，收尾斷言沒有指回去）════════════════
TMP = pathlib.Path(tempfile.mkdtemp(prefix="lots-test-"))
REAL = {"broker.ORDER_DIR": broker.ORDER_DIR, "broker.TRADE_DIR": broker.TRADE_DIR,
        "broker.REAL_FLAG": broker.REAL_FLAG,
        "lots.LEVEL_FILE": lots.LEVEL_FILE, "lots.LOG_DIR": lots.LOG_DIR,
        "AF.ARM_FLAG": AF.ARM_FLAG, "AF.FIRE_DIR": AF.FIRE_DIR, "AF.FAST_HIST": AF.FAST_HIST,
        "AF.ORB_HIST": AF.ORB_HIST, "AF.TICK_DIR": AF.TICK_DIR,
        "NF.ARM_FLAG": NF.ARM_FLAG, "NF.NF_DIR": NF.NF_DIR, "NF.TSM_CTX": NF.TSM_CTX,
        "NF.TREND_CTX": NF.TREND_CTX, "risk_cap.OVERRIDE_FLAG": risk_cap.OVERRIDE_FLAG}
REAL_LEVEL_EXISTED = REAL["lots.LEVEL_FILE"].exists()
REAL_LOG_EXISTED = REAL["lots.LOG_DIR"].exists()
broker.ORDER_DIR = TMP / "real_orders"
broker.TRADE_DIR = TMP / "real_trades"
broker.REAL_FLAG = TMP / "REAL_ORDERS_ON"
lots.LEVEL_FILE = TMP / "LOTS_LEVEL.json"
lots.LOG_DIR = TMP / "lots"
lots.VOL_CSV = TMP / "__no_such_tmf_1min.csv"
AF.ARM_FLAG = TMP / "AUTO_ORDERS_ON"
AF.FIRE_DIR = TMP / "autofire"
AF.FAST_HIST = TMP / "fast_hist.jsonl"
AF.ORB_HIST = TMP / "orb_hist.jsonl"
AF.TICK_DIR = TMP / "tick_logs"
NF.ARM_FLAG = TMP / "NIGHT_ORDERS_ON"
NF.NF_DIR = TMP / "nightfire"
NF.TSM_CTX = TMP / "tsm_ctx.json"
NF.TREND_CTX = TMP / "trend_ctx.json"
risk_cap.OVERRIDE_FLAG = TMP / "RISK_OVERRIDE"
broker.FILL_WAIT = 1.2
broker.CA_OK["ok"] = True

FAKE_ACC = sj.Account(account_type=sj.AccountType.Future, person_id="TESTPID",
                      broker_id="F000000", account_id="0000000", signed=True, username="test")


def write_state(**kw):
    st = json.loads(json.dumps(lots.DEFAULT))
    st.update(kw)
    lots.LEVEL_FILE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    return st


def clear_state():
    for p in (lots.LEVEL_FILE, lots.LEVEL_FILE.with_name(lots.LEVEL_FILE.name + ".tmp")):
        if p.exists():
            p.unlink()
    shutil.rmtree(lots.LOG_DIR, ignore_errors=True)


# ══ 假券商：多口、部分成交、停利單部分成交 ══════════════════════════════

class D:
    def __init__(self, price, quantity):
        self.price, self.quantity = price, quantity


class Trade:
    _n = 0

    def __init__(self):
        Trade._n += 1
        self.order = type("O", (), {"id": "T%07d" % Trade._n})()
        self.status = type("S", (), {"status": "Submitted", "deals": []})()


class MultiAPI:
    """
    ⚠️ 假券商一定要模擬「部位真的會出現／消失、口數真的會變」（2026-09-01 連兩次栽在假物件上）。
      new_fill：每一張新倉單成交幾口（None ⇒ 全部）；cover_fill：每一張平倉市價單成交幾口（None ⇒ 全部剩下的）。
      before_cover：撤停利之後、第一張平倉單之前，停利單又成交幾口（重現「撤單前一瞬間」）。
      平倉單口數 > 剩下的 ⇒ 記 overclosed（真的券商會反向開新倉）。
    """

    def __init__(self, side="Buy", px=12010.0, new_fill=None, cover_fill=None, exit_px=12000.0, spread=0.0):
        self.side, self.px, self.exit_px, self.spread = side, px, exit_px, spread
        self.new_fill = list(new_fill or [])
        self.cover_fill = list(cover_fill or [])
        self.orders, self.cancels = [], 0
        self.lots = []            # 還開著的各口成交價
        self.target = None
        self.overclosed = 0
        self.pnl = []

    def place_order(self, contract, order):
        self.orders.append(order)
        t = Trade()
        oc, pt = str(order.octype), str(order.price_type)
        if oc.endswith("New"):
            k = order.quantity if not self.new_fill else self.new_fill.pop(0)
            for i in range(k):
                p = self.px + self.spread * i
                self.lots.append(p)
                t.status.deals.append(D(p, 1))
        elif oc.endswith("Cover") and pt.endswith("MKP"):
            if order.quantity > len(self.lots):
                self.overclosed += order.quantity - len(self.lots)
            k = min(order.quantity, len(self.lots)) if not self.cover_fill else self.cover_fill.pop(0)
            for _ in range(k):
                self.lots.pop(0)
                t.status.deals.append(D(self.exit_px, 1))
        elif oc.endswith("Cover"):
            self.target = t
        return t

    def fill_target(self, n, px):
        for _ in range(n):
            self.lots.pop(0)
        self.target.status.deals.append(D(px, n))

    def list_positions(self, acc=None):
        if not self.lots:
            return []
        return [type("P", (), {"code": "TMFI6", "quantity": len(self.lots), "direction": self.side,
                               "price": round(sum(self.lots) / len(self.lots), 2)})()]

    def update_status(self, acc=None):
        pass

    def cancel_order(self, t):
        self.cancels += 1

    def list_profit_loss(self, acc=None, d1=None, d2=None):
        return list(self.pnl)


def connect(api):
    broker._state.update({"api": api, "account": FAKE_ACC, "contract": type("C", (), {"code": "TMFI6"})(),
                          "position": None})
    broker._close_fail["at"] = 0.0
    broker._LAST_RECONCILE["at"] = 0.0
    broker._CLOSE_INFLIGHT["on"] = False


def live(on=True):
    if on:
        broker.REAL_FLAG.parent.mkdir(parents=True, exist_ok=True)
        broker.REAL_FLAG.write_text("on", encoding="utf-8")
    elif broker.REAL_FLAG.exists():
        broker.REAL_FLAG.unlink()


def qtys(api, kind=None):
    out = []
    for o in api.orders:
        oc, pt = str(o.octype), str(o.price_type)
        k = "new" if oc.endswith("New") else ("close" if pt.endswith("MKP") else "target")
        if kind is None or k == kind:
            out.append(o.quantity)
    return out


def trades_file():
    p = broker.TRADE_DIR / ("%s.jsonl" % date.today())
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []


def order_log():
    p = broker.ORDER_DIR / ("%s.jsonl" % date.today())
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []


def reset_dirs():
    for p in (broker.ORDER_DIR, broker.TRADE_DIR, AF.FIRE_DIR, NF.NF_DIR):
        shutil.rmtree(p, ignore_errors=True)
    broker._REALIZED.update({"at": 0.0, "rows": []})
    broker._HIST["at"] = 0.0


# ══ ① 沒有狀態檔 ⇒ 1＋1 ═══════════════════════════════════════════════
print("=== ① 沒有狀態檔 ⇒ 1＋1（跟 10-14 以前一樣）===")
clear_state()
st, err = lots.read_state()
chk("  檔案不存在 ⇒ 第 0 級、沒有錯誤", (st["level"], err), (0, None))
chk("  日盤 1 口", lots.day_lots(), 1)
chk("  風控上限用 (1, 1) ⇒ 800", (lots.cap_lots(), risk_cap.cap_of(lots=lots.cap_lots())), ((1, 1), 800.0))
chk("  保證金算 1 口", lots.margin_lots(), 1)
for hi in (True, False, None):
    _rows = [("2026-05-%02d" % (i + 1), (2.0 if hi else 0.5)) for i in range(25)] if hi is not None else []
    _p = lots.night_plan(date(2026, 5, 26), rows=_rows)
    # ⭐ QA 必修 1：1＋1 ⇒ ⛔ 連振幅都不算（high 一律 None）
    chk("  夜盤（振幅指標%s）⇒ 1 口、⛔ 1 口不算振幅" % {True: "偏高", False: "不偏高", None: "算不出來"}[hi],
        (_p["qty"], _p["high"], _p["vol"]), (1, None, None))
for bad, why in (('{"level": 3', "JSON 壞掉"), ('{"level": 99}', "級數超過階梯"), ('{"level": true}', "bool"),
                 ('{"level": "2"}', "字串"), ('{"level": 2, "cap": {"union": 5}}', "cap 不合理"),
                 ('[2]', "不是物件"), ('{"level": 2, "pending": {"to": 2, "eff": "2026-11-03"}}', "pending 沒往上"),
                 ('{"level": 2, "pending": {"to": 11, "eff": "2026-11-03"}}', "pending 超過階梯"),
                 ('{"level": 2, "month_max": {"month": "2026-10", "ab": [99, 99, 1]}}', "month_max 壞")):
    lots._LAST_GOOD.clear()
    lots.LEVEL_FILE.write_text(bad, encoding="utf-8")
    st, err = lots.read_state()
    say(st["level"] == 0 and err and lots.day_lots() == 1 and lots.cap_lots() == (1, 1),
        "  狀態檔%s ⇒ 一律 1＋1、而且講得出原因" % why, err)
# ⭐ QA 必修 5：Windows 換檔競態 ⇒ 讀不到（PermissionError）時 ⛔ 不退回 1＋1，用上一次讀成功的那份
write_state(level=4)
chk("  前置：讀到 4＋2", lots.base_lots(lots.read_state()[0]), (4, 2))
_orig_rt = pathlib.Path.read_text
_RT = {"n": 0}


def _perm_read(self, *a, **k):
    if self == lots.LEVEL_FILE:
        _RT["n"] += 1
        raise PermissionError(13, "存取被拒（測試）")
    return _orig_rt(self, *a, **k)


pathlib.Path.read_text = _perm_read
try:
    _st_race, _err_race = lots.read_state()
finally:
    pathlib.Path.read_text = _orig_rt
chk("  ⛔ 一直 PermissionError ⇒ 用上一次讀成功的 4＋2（⛔ 不退回 1＋1）、不報錯",
    (lots.base_lots(_st_race), _err_race), ((4, 2), None))
say(_RT["n"] == lots.IO_TRIES, "  而且真的有重試（%d 次）" % lots.IO_TRIES, _RT["n"])
_RT["n"] = 0


def _perm_twice(self, *a, **k):
    if self == lots.LEVEL_FILE and _RT["n"] < 2:
        _RT["n"] += 1
        raise PermissionError(13, "存取被拒（測試）")
    return _orig_rt(self, *a, **k)


lots._LAST_GOOD.clear()
pathlib.Path.read_text = _perm_twice
try:
    _st_race, _err_race = lots.read_state()
finally:
    pathlib.Path.read_text = _orig_rt
chk("  前兩次被拒、第三次讀到 ⇒ 4＋2（沒有上一份可用也讀得到）", (lots.base_lots(_st_race), _err_race), ((4, 2), None))
lots._LAST_GOOD.clear()
pathlib.Path.read_text = _perm_read
try:
    _st_race, _err_race = lots.read_state()
finally:
    pathlib.Path.read_text = _orig_rt
say(lots.base_lots(_st_race) == (1, 1) and _err_race, "  從來沒讀成功過、又一直被拒 ⇒ 才退回 1＋1、講得出原因", _err_race)
_orig_rep = pathlib.Path.replace
_RP = {"n": 0}


def _perm_replace(self, target):
    if _RP["n"] < 3:
        _RP["n"] += 1
        raise PermissionError(13, "存取被拒（測試）")
    return _orig_rep(self, target)


pathlib.Path.replace = _perm_replace
try:
    _we = None
    _sw = json.loads(json.dumps(lots.DEFAULT))
    _sw["level"] = 3
    lots._write_state(_sw)
except Exception as e:      # noqa: BLE001
    _we = e
finally:
    pathlib.Path.replace = _orig_rep
chk("  寫：前三次 replace 被拒 ⇒ 重試後寫進去（3＋1）", (_we, lots.base_lots(lots.read_state()[0])), (None, (3, 1)))
clear_state()
lots._LAST_GOOD.clear()
lots.LEVEL_FILE.write_text('{"level": 3', encoding="utf-8")
_r = lots.daily_check([{"date": "2026-10-13", "equity": 500000.0}], now=datetime(2026, 10, 14, 15, 0),
                      ctx={"retire": {"union": "ok", "trend": "ok"}, "quality": {"ok": True}, "incidents": []})
chk("  ⛔ 狀態檔壞掉時每日檢查不覆蓋它（留著給人看）", lots.LEVEL_FILE.read_text(encoding="utf-8"), '{"level": 3')
say(_r["err"] and not _r["ran"], "  而且回報錯誤", _r["err"])
clear_state()

# ══ ② ⛔⛔ 逐位元相同：改版前（git HEAD）vs 現在，同一套情境送出去的每一張單 ════════
print("\n=== ② ⛔⛔ 沒有狀態檔時送出的單，跟改版前逐位元相同（git HEAD 對照）===")
SCEN = r'''
import json, sys, time, pathlib
from datetime import date, datetime, timedelta
out = pathlib.Path(sys.argv[1]); paths = json.loads(sys.argv[2])
for p in reversed(paths):
    sys.path.insert(0, p)
import broker, auto_fire as AF, night_fire as NF, risk_cap, us_feed
import shioaji as sj
if "lots" in sys.modules:
    L = sys.modules["lots"]
    L.LEVEL_FILE = out / "LOTS_LEVEL.json"; L.LOG_DIR = out / "lots"; L.VOL_CSV = out / "__none.csv"
broker.ORDER_DIR = out / "real_orders"; broker.TRADE_DIR = out / "real_trades"
broker.REAL_FLAG = out / "REAL_ORDERS_ON"; out.mkdir(parents=True, exist_ok=True)
broker.REAL_FLAG.write_text("on", encoding="utf-8")
AF.ARM_FLAG = out / "AUTO_ORDERS_ON"; AF.FIRE_DIR = out / "autofire"; AF.FAST_HIST = out / "fh.jsonl"
AF.ORB_HIST = out / "orb.jsonl"; AF.TICK_DIR = out / "tick_logs"
NF.ARM_FLAG = out / "NIGHT_ORDERS_ON"; NF.NF_DIR = out / "nightfire"; NF.TSM_CTX = out / "tsm.json"
NF.TREND_CTX = out / "trend.json"; risk_cap.OVERRIDE_FLAG = out / "RISK_OVERRIDE"
broker.FILL_WAIT = 1.2; broker.CA_OK["ok"] = True; broker.MAX_ENTRIES = 99
ACC = sj.Account(account_type=sj.AccountType.Future, person_id="TESTPID", broker_id="F000000",
                 account_id="0000000", signed=True, username="test")
class D:
    def __init__(s, p, q): s.price, s.quantity = p, q
class T:
    def __init__(s):
        s.order = type("O", (), {"id": "T1"})(); s.status = type("S", (), {"status": "Submitted", "deals": []})()
SENT = []
class API:
    def __init__(s, side, px, ex): s.side, s.px, s.ex, s.q = side, px, ex, 0
    def place_order(s, c, o):
        SENT.append(repr(o)); t = T(); oc, pt = str(o.octype), str(o.price_type)
        if oc.endswith("New"):
            s.q += o.quantity; t.status.deals.append(D(s.px, o.quantity))
        elif oc.endswith("Cover") and pt.endswith("MKP"):
            k = min(o.quantity, s.q); s.q -= k; t.status.deals.append(D(s.ex, k))
        return t
    def list_positions(s, acc=None):
        return [] if s.q <= 0 else [type("P", (), {"code": "TMFI6", "quantity": s.q, "direction": s.side, "price": s.px})()]
    def update_status(s, acc=None): pass
    def cancel_order(s, t): SENT.append("cancel")
    def list_profit_loss(s, acc=None, a=None, b=None): return []
def connect(api):
    broker._state.update({"api": api, "account": ACC, "contract": type("C", (), {"code": "TMFI6"})(), "position": None})
    broker._close_fail["at"] = 0.0; broker._CLOSE_INFLIGHT["on"] = False
AF.configure(signal_at="09:03:30", signal_sec=32610, late_ms=5000, gap_s=3.0, sig_fn=lambda *a: None,
             dirs_fn=lambda *a: None, eod_at="13:43:30", pctl=80, rev_at="09:15:00", rev_sec=33300)
# S1 手動真單（做多、停利 100）→ 手動平倉
connect(API("Buy", 12113.0, 12120.0)); broker.enter("long", 12110.0, 100); broker.close("manual")
# S2 日盤自動（多方聯軍・快攻，有停利）→ 收盤平倉（走 _eod 的三道認人）
d = str(date.today())
connect(API("Buy", 12013.0, 12030.0))
base = {"method": "U", "cand": "fast", "dir": "long", "leg": "fast", "sl_points": 60, "tp_points": 60}
AF._send(d, base, {"method": "U"}, "long", 12010.0, 60, 0, time.time(), {"quote_age_ms": 100}, "t", tp_points=60)
AF._eod(d, 0, time.time(), "13:43:30")
# S3 日盤自動（開箱，不設停利）做空 → 停損
connect(API("Sell", 11990.0, 12050.0))
d3 = str(date.today() - timedelta(days=1))
AF._send(d3, {"method": "U", "cand": "orb", "dir": "short", "sl_points": 57}, {"method": "U"}, "short", 11992.0, 57, 0,
         time.time(), {"quote_age_ms": 100}, "t", tp_points=None)
broker.close("sl")
# S4 夜盤跟勢（R）→ 04:58 平倉
E = date(2026, 6, 10)
NF.ARM_FLAG.write_text("R", encoding="ascii")
NF.TREND_CTX.write_text(json.dumps({"sig": {str(E - timedelta(days=i + 1)): (100.0 if i % 2 else -50.0) for i in range(40)}}), encoding="utf-8")
MC = {21 * 60 + 9: 11800.0}
NF.configure(quote_fn=lambda: (12000.0, 1.0), session_fn=lambda now: "night", minute_close_fn=lambda m: MC.get(m))
connect(API("Buy", 12001.0, 11990.0))
NF._decide(E, NF.trend_at(E) + timedelta(seconds=1))
NF._eod(E, datetime(2026, 6, 11, 4, 58, 1))
# S5 台積電快攻（T，有停利）做空 → 04:58 平倉
E5 = date(2026, 6, 17)
NF.ARM_FLAG.write_text("T", encoding="ascii")
NF.TSM_CTX.write_text(json.dumps({"rng": {str(date(2026, 5, 1) + timedelta(days=i)): 0.5 for i in range(30)}}), encoding="utf-8")
NF.past_mvs = lambda E, n=40, back_days=90: [0.3 if i % 2 else -0.2 for i in range(40)]
us_feed.first5_live = lambda sym, E, feed="iex", now=None: {"open": 100.0, "close": 98.0, "mv_pct": -2.0}
connect(API("Sell", 11998.0, 11980.0))
NF._ST["tried"] = None
NF._decide(E5, NF.done_at(E5) + timedelta(seconds=3))
NF._ST["eod_try_at"] = 0.0
NF._eod(E5, datetime(2026, 6, 18, 4, 58, 1))
rec = []
for p in sorted((out / "real_trades").glob("*.jsonl")):
    for ln in p.read_text(encoding="utf-8").splitlines():
        o = json.loads(ln); [o.pop(k, None) for k in ("entry_time", "exit_time", "date")]; rec.append(o)
print("@@" + json.dumps({"orders": SENT, "trades": rec}, ensure_ascii=False))
'''
_scen = TMP / "__tmp__scenario.py"
_scen.write_text(SCEN, encoding="utf-8")
_old = TMP / "__tmp__head"
_old.mkdir()
_repo = HERE.parent.parent
# ⭐ 2026-10-15：多口已經併進 main（72291049）⇒ 「改版前」＝那一筆的上一筆，⛔ 不再是 HEAD。
#    QA 的突變複本（自己的 git、只有一筆 base）沒有那個 commit ⇒ 退回 HEAD（那邊的 HEAD 就是改版前）。
_BASE_REV = "72291049^" if subprocess.run(["git", "-C", str(_repo), "rev-parse", "--verify", "-q", "72291049^"],
                                           capture_output=True).returncode == 0 else "HEAD"
_gitok = True
for _f in ("broker.py", "auto_fire.py", "night_fire.py", "risk_cap.py"):
    _r = subprocess.run(["git", "-C", str(_repo), "show", _BASE_REV + ":tools/shioaji/" + _f], capture_output=True)
    if _r.returncode != 0:
        _gitok = False
    (_old / _f).write_bytes(_r.stdout)
say(_gitok and b"QTY = 1" in (_old / "broker.py").read_bytes() and b"lots" not in (_old / "broker.py").read_bytes()
    and b"import lots" not in (_old / "auto_fire.py").read_bytes(),
    "  尺的自證：拿到的是改版前的 broker.py／auto_fire.py（沒有 lots）")


def _run_scen(tag, paths):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env.pop("MUT_DIR", None)
    r = subprocess.run([sys.executable, str(_scen), str(TMP / ("__tmp__scen_" + tag)), json.dumps(paths)],
                       capture_output=True, text=True, encoding="utf-8", env=env, timeout=300)
    line = next((x for x in r.stdout.splitlines() if x.startswith("@@")), None)
    if line is None:
        print(r.stdout[-1500:], r.stderr[-1500:])
        return None
    return json.loads(line[2:])


_new_paths = ([_MUT] if _MUT else []) + [str(HERE)]
G_OLD = _run_scen("old", [str(_old), str(HERE)])
G_NEW = _run_scen("new", _new_paths)
say(G_OLD is not None and G_NEW is not None, "  兩邊的情境都跑完了")
if G_OLD and G_NEW:
    say(len(G_OLD["orders"]) >= 12, "  尺的自證：情境真的送出了單（%d 張：進場／停利／撤單／平倉）" % len(G_OLD["orders"]))
    say(any("quantity=1" in o for o in G_OLD["orders"]), "  尺的自證：比的是完整的委託單內容（含 quantity）",
        (G_OLD["orders"] or [""])[0][:90])
    chk("  ⛔⛔ 每一張單（含順序、口數、價格、型態、帳號）跟改版前完全一樣", G_NEW["orders"], G_OLD["orders"])
    chk("  成績單（扣掉時間欄位）跟改版前完全一樣", G_NEW["trades"], G_OLD["trades"])
    say(len(G_OLD["trades"]) == 5, "  尺的自證：五趟來回都有成績（手動／日盤收盤／日盤停損／夜盤跟勢／台積電快攻）",
        len(G_OLD["trades"]))

# ══ ③ 各級口數 ═══════════════════════════════════════════════════════
print("\n=== ③ 各級口數（goal.LADDER 是唯一正本）===")
chk("  lots 讀的是 goal.LADDER 那一份（同一個物件）", lots._ladder(), tuple(goal.LADDER))
_bad = []
for k in range(0, len(goal.LADDER) + 1):
    write_state(level=k)
    a, b = goal.lots_of(k)
    got = (lots.day_lots(), lots.night_plan(date(2026, 5, 26), rows=[("2026-05-%02d" % (i + 1), 2.0) for i in range(25)])["qty"],
           lots.night_plan(date(2026, 5, 26), rows=[("2026-05-%02d" % (i + 1), 0.2) for i in range(25)])["qty"],
           lots.cap_lots(), lots.margin_lots())
    want = (a, b, max(1, b - 1), (a, b), max(a, b))
    if got != want:
        _bad.append((k, got, want))
chk("  第 0～10 級：日盤 a、夜盤偏高 b／不偏高 max(1,b−1)、風控 (a,b)、保證金 max(a,b)", _bad, [])
write_state(level=4)
chk("  第 4 級（20 萬）＝ 4＋2", lots.base_lots(), (4, 2))
write_state(level=4, cap={"union": None, "trend": 1})
chk("  夜盤跟勢降級 ⇒ 夜盤最多 1 口、日盤照舊 4 口", lots.base_lots(), (4, 1))
chk("  ⛔ 風控上限不套單條降級（照階梯 4＋2）", lots.cap_lots(), (4, 2))
write_state(level=4, cap={"union": 0, "trend": None})
chk("  退休歸零還沒拍板（RETIRE_ZERO=False）⇒ 退休也只壓到 1 口", lots.base_lots(), (1, 2))
_t = date.today()
_eff = lots.next_weekday(_t)
write_state(level=1, pending={"to": 2, "from": 1, "eff": str(_eff), "at": "x"})
chk("  已確認的升級：生效日以前照舊 1 口", lots.day_lots(d=_t), 1)
chk("  生效日（含）起 2 口", (lots.day_lots(d=_eff), lots.base_lots(d=_eff + timedelta(days=7))), (2, (2, 1)))
clear_state()

# ══ ④ 多口送單（broker）══════════════════════════════════════════════
print("\n=== ④ 多口送單：全成交／部分成交／口數不合理／平倉平到 0 ===")
reset_dirs()
live(True)
api = MultiAPI()
connect(api)
ok, err, pos = broker.enter("long", 12010.0, 60, sl_points=60, qty=3)
chk("  3 口全成交：進場單 3 口、停利單 3 口", (ok, qtys(api, "new"), qtys(api, "target")), (True, [3], [3]))
chk("  部位記 3 口（entry_qty／qty_sent 也是 3）", (pos["qty"], pos["entry_qty"], pos["qty_sent"]), (3, 3, 3))
ok, err = broker.close("sl")
chk("  停損平倉：一張 3 口的平倉單、平到 0", (ok, qtys(api, "close"), api.lots, api.overclosed), (True, [3], [], 0))
_tr = trades_file()
chk("  成績單：qty 3、每口點數 −10（12010→12000）", (_tr[-1]["qty"], _tr[-1]["points"]), (3, -10.0))
chk("  risk_cap 的算法（points × qty）＝ −30", _tr[-1]["points"] * _tr[-1]["qty"], -30.0)

print("  -- 部分成交：送 3 只成交 2 ⇒ 接受 2 口，⛔ 不追單 --")
reset_dirs()
api = MultiAPI(new_fill=[2])
connect(api)
_t0 = time.time()
ok, err, pos = broker.enter("long", 12010.0, 60, sl_points=60, qty=3)
chk("  進場成功、部位 2 口（entry_qty 2、qty_sent 3）",
    (ok, pos["qty"], pos["entry_qty"], pos["qty_sent"]), (True, 2, 2, 3))
chk("  ⛔ 只送了一張新倉單（不追單補到 3）", qtys(api, "new"), [3])
chk("  停利單照實際口數 2 口", qtys(api, "target"), [2])
say(any(r.get("kind") == "entry_partial" and r.get("got") == 2 for r in order_log()), "  委託 log 記下 entry_partial（送 3 成交 2）")
say(time.time() - _t0 < broker.FILL_WAIT + 1.5, "  部分成交不會卡到逾時以上", "%.2f 秒" % (time.time() - _t0))
ok, err = broker.close("eod")
chk("  收盤平倉：平倉單 2 口、平到 0、⛔ 沒平過頭", (ok, qtys(api, "close"), api.lots, api.overclosed), (True, [2], [], 0))
chk("  成績單記 2 口", trades_file()[-1]["qty"], 2)

print("  -- 平倉單只成交一部分 ⇒ 下一張只送剩下的，最後平到 0 --")
reset_dirs()
api = MultiAPI(side="Sell", cover_fill=[1], exit_px=12000.0)
connect(api)
broker.enter("short", 12010.0, None, sl_points=60, qty=3)
ok, err = broker.close("sl")
chk("  兩張平倉單：3 口（成交 1）→ 2 口（成交 2）⇒ 平到 0", (ok, qtys(api, "close"), api.lots, api.overclosed),
    (True, [3, 2], [], 0))
_tr = trades_file()[-1]
chk("  成績單：qty 3、出場價是兩張單的加權平均、做空每口 +10", (_tr["qty"], _tr["exit"], _tr["points"]), (3, 12000.0, 10.0))

print("  -- 口數不合理 ⇒ 一張都不送 --")
for bad in (0, 11, True, 2.0, "2", -1):
    reset_dirs()
    api = MultiAPI()
    connect(api)
    ok, err, pos = broker.enter("long", 12010.0, 60, qty=bad)
    say(not ok and api.orders == [] and broker._state["position"] is None, "  qty=%r ⇒ 不送、沒有部位" % (bad,), err)
try:
    broker._order(sj.Action.Buy, 0, sj.FuturesPriceType.MKP, sj.OrderType.IOC, sj.FuturesOCType.New, qty=11)
    say(False, "  _order(qty=11) 要組不出單")
except ValueError:
    say(True, "  ⛔ _order 本身也擋：qty=11 組不出單（QTY_MAX=%d）" % broker.QTY_MAX)

print("  -- 停利單部分成交：剩下的照樣看停損、平剩下的、成績單記整趟 --")
reset_dirs()
api = MultiAPI()
connect(api)
ok, err, pos = broker.enter("long", 12010.0, 60, sl_points=60, qty=3)
api.fill_target(1, 12070.0)                       # 停利單先成交 1 口
broker.reconcile()
p = broker._state["position"]
chk("  對帳：部位剩 2 口、記一段 leg（1 口 @12070 停利）",
    (p["qty"], [(l["qty"], l["px"], l["src"]) for l in p.get("legs", [])]), (2, [(1, 12070.0, "tp")]))
chk("  ⛔ 進場價（停損基準）維持成交那一刻的均價", p["entry"], 12010.0)
ok, err = broker.close("sl")
chk("  停損：撤停利 → 平倉單只送剩下的 2 口、平到 0", (ok, api.cancels, qtys(api, "close"), api.lots, api.overclosed),
    (True, 1, [2], [], 0))
_tr = trades_file()[-1]
chk("  成績單：qty 3、出場加權 (12070 + 2×12000)/3、每口點數",
    (_tr["qty"], _tr["exit"], _tr["points"]), (3, round((12070 + 24000) / 3, 2), round((12070 + 24000) / 3 - 12010, 1)))
chk("  總點數（points × qty）＝ +60 −10 −10 ＝ +40", round(_tr["points"] * _tr["qty"]), 40)
chk("  legs 附在成績單上給人對帳", [(l["qty"], l["src"]) for l in _tr.get("legs", [])], [(1, "tp"), (2, "sl")])

print("  -- 停利單分兩次吃完：第二段只算剩下那幾口的價 --")
reset_dirs()
api = MultiAPI()
connect(api)
broker.enter("long", 12010.0, 60, sl_points=60, qty=3)
api.fill_target(1, 12070.0)
broker.reconcile()
api.fill_target(2, 12072.0)
broker.reconcile()
_tr = trades_file()[-1]
chk("  整趟一列：qty 3、出場 (12070+2×12072)/3、理由 tp", (_tr["qty"], _tr["exit"], _tr["reason"]),
    (3, round((12070 + 2 * 12072) / 3, 2), "tp"))

print("  -- 撤停利之前那一瞬間停利又成交 ⇒ ⛔ 平倉單不可以比剩下的多 --")
reset_dirs()
api = MultiAPI()
connect(api)
broker.enter("long", 12010.0, 60, sl_points=60, qty=3)
_real_cancel = api.cancel_order


def _cancel_race(t):
    _real_cancel(t)
    if len(api.lots) == 3:
        api.fill_target(2, 12070.0)               # 撤單送出去前又成交 2 口
api.cancel_order = _cancel_race
ok, err = broker.close("sl")
chk("  只送剩下的 1 口、⛔ 沒有平過頭（反向開新倉）", (ok, qtys(api, "close"), api.overclosed, api.lots), (True, [1], 0, []))

print("  -- 一口的部位（含撿回來對不上的手動多口）平倉照舊：每張 1 口 --")
reset_dirs()
api = MultiAPI()
connect(api)
api.lots = [12010.0, 12010.0]                      # 他手動的 2 口（不是我們開的）
broker._state["position"] = {"dir": "long", "entry": 12010.0, "qty": 2, "entry_time": None,
                             "target_trade": None, "recovered": True}
ok, err = broker.close("sl")
chk("  手動 2 口（沒有 entry_qty）：平倉單 1 口、1 口（跟改版前一樣）", (ok, qtys(api, "close")), (True, [1, 1]))
live(False)

# ══ ⑤ auto_fire 多口 ═════════════════════════════════════════════════
print("\n=== ⑤ auto_fire 多口：送單口數、帳本、收盤平倉認人 ===")
AF.configure(signal_at="09:03:30", signal_sec=32610, late_ms=5000, gap_s=3.0, sig_fn=lambda *a: None,
             dirs_fn=lambda *a: None, eod_at="13:43:30", pctl=80, rev_at="09:15:00", rev_sec=33300)
DAY = str(date.today())


def af_send(api, lvl, qty_fill=None):
    reset_dirs()
    clear_state()
    if lvl is not None:
        write_state(level=lvl)
    live(True)
    connect(api)
    AF._MEM.update({"date": None, "entry": None, "state": None})
    AF._send(DAY, {"method": "U", "cand": "fast", "dir": "long", "leg": "fast", "sl_points": 60, "tp_points": 60},
             {"method": "U"}, "long", 12010.0, 60, 0, time.time(), {"quote_age_ms": 100}, "t", tp_points=60)
    return AF._day_rows(DAY)[0].get("fast") or {}


api = MultiAPI()
r = af_send(api, 2)
chk("  第 2 級（10 萬 2＋1）⇒ 日盤送 2 口、停利 2 口", (qtys(api, "new"), qtys(api, "target")), ([2], [2]))
chk("  帳本：qty 2（實際成交）、qty_sent 2、lots_level 2", (r.get("qty"), r.get("qty_sent"), r.get("lots_level")), (2, 2, 2))
api = MultiAPI(new_fill=[1])
r = af_send(api, 2)
chk("  部分成交：帳本 qty 1、qty_sent 2", (r.get("qty"), r.get("qty_sent")), (1, 2))
api = MultiAPI(spread=2.0)
r = af_send(api, 3)
chk("  3 口成交價 12010／12012／12014 ⇒ 帳本記均價與高低", (r.get("entry"), r.get("fill_lo"), r.get("fill_hi")),
    (12012.0, 12010.0, 12014.0))
ent = r
_pos_after = {"dir": "long", "entry": 12013.0, "qty": 2, "entry_time": ent.get("entry_time")}
chk("  停利部分成交後剩 2 口、均價 12013（在 12010～12014 之間）⇒ 還是我們的", AF._looks_ours(_pos_after, ent)[0], True)
chk("  ⛔ 比帳本多（4 口）⇒ 他自己加過碼 ⇒ 不是我們的", AF._looks_ours(dict(_pos_after, qty=4), ent)[0], False)
chk("  ⛔ 均價跑出成交範圍 ⇒ 不是我們的", AF._looks_ours(dict(_pos_after, entry=12020.0), ent)[0], False)
_ent1 = {"dir": "long", "entry": 12010.0, "entry_time": "09:03:31", "qty": 1}
chk("  一口的帳本：剛好 1 口才是我們的（跟以前一樣）",
    (AF._looks_ours({"dir": "long", "entry": 12010.0, "qty": 1}, _ent1)[0],
     AF._looks_ours({"dir": "long", "entry": 12010.0, "qty": 2}, _ent1)[0]), (True, False))
chk("  舊帳本沒有 qty ⇒ 當 1 口", AF._looks_ours({"dir": "long", "entry": 12010.0, "qty": 2},
                                              {"dir": "long", "entry": 12010.0})[0], False)
api.pnl = [{"entry": 12010.0, "exit": 12070.0, "pnl": 600.0, "qty": 1, "dir": "long"}]
_REAL_RT = broker.realized_today
broker.realized_today = lambda fresh=False: list(api.pnl)
chk("  券商已實現只有其中 1 口（停利部分成交）⇒ ⛔ 不是「整趟平掉了」", AF._already_closed(ent)[0], "open")
api.pnl += [{"entry": 12012.0, "exit": 12000.0, "pnl": -120.0, "qty": 1, "dir": "long"},
            {"entry": 12014.0, "exit": 12000.0, "pnl": -140.0, "qty": 1, "dir": "long"}]
chk("  三口都在已實現裡 ⇒ closed", AF._already_closed(ent)[0], "closed")
chk("  一口的帳本：對到一列就 closed（跟以前一樣）",
    AF._already_closed(dict(_ent1, entry=12012.0))[0], "closed")
broker.realized_today = _REAL_RT                 # 換回正本
# 收盤平倉整條路：停利部分成交後 13:43:30 平剩下的
api = MultiAPI(spread=2.0)
r = af_send(api, 3)
api.fill_target(1, 12072.0)
broker.reconcile()
AF._eod(DAY, 0, time.time(), "13:43:30")
_e = [o for o in AF._rows_of(DAY) if o.get("rec") == "eod"]
chk("  收盤平倉：停利吃掉 1 口之後，平剩下的 2 口（eod_closed）、平到 0",
    ([o.get("why") for o in _e], qtys(api, "close"), api.lots, api.overclosed), (["eod_closed"], [2], [], 0))
meta = AF._recover_decide({"dir": "long", "entry": 12013.0, "qty": 2, "recovered": True}, r, None)
chk("  重啟撿回（多口）：補回 entry_qty 3、entry_fill、停損點數", (meta.get("entry_qty"), meta.get("entry_fill"),
                                                    meta.get("sl_points")), (3, 12012.0, 60.0))
meta1 = AF._recover_decide({"dir": "long", "entry": 12010.0, "qty": 1, "recovered": True},
                           dict(_ent1, sl_points=60.0), None)
chk("  重啟撿回（一口）：⛔ 不多補欄位（跟以前一樣）", "entry_qty" in meta1, False)
nm = NF._multi_meta({"qty": 2, "entry": 12000.0})
chk("  夜盤撿回（多口）補 entry_qty；一口不補", (nm, NF._multi_meta({"qty": 1, "entry": 1.0})),
    ({"entry_qty": 2, "entry_fill": 12000.0}, {}))
live(False)
clear_state()

# ══ ⑥ 風控 B：本月用過最大 ═══════════════════════════════════════════
print("\n=== ⑥ 風控 B：400 ×（本月用過最大 a＋b），月中減級不掉、換月重算 ===")
EQ_ROWS = []


def ctx_ok(**kw):
    c = {"retire": {"union": "ok", "trend": "ok"}, "retire_err": None,
         "quality": {"ok": True, "n": 30, "need": 30, "slip_avg": 3.2, "errors": []}, "incidents": []}
    c.update(kw)
    return c


clear_state()
write_state(level=2, checked="2026-11-16", up_month="2026-11")
lots.daily_check([{"date": "2026-11-17", "equity": 83100.0}], now=datetime(2026, 11, 17, 14, 5), ctx=ctx_ok())
st, _ = lots.read_state()
chk("  11/17 權益 8.31 萬 < 8.5 萬 ⇒ 退回第 1 級（1＋1）", (st["level"], lots.base_lots(st, date(2026, 11, 18))), (1, (1, 1)))
chk("  ⛔ 11 月風控上限維持 2＋1 ⇒ 1,200（不跟著掉到 800）",
    (lots.cap_lots(st, date(2026, 11, 18)), risk_cap.cap_of(lots=lots.cap_lots(st, date(2026, 11, 18)))), ((2, 1), 1200.0))
chk("  12 月起照 1＋1 ⇒ 800", lots.cap_lots(st, date(2026, 12, 1)), (1, 1))
chk("  夜盤（E 在 11 月）一樣用 11 月的最大", lots.cap_lots(st, date(2026, 11, 30)), (2, 1))

# ══ ⑦ 減級 ═══════════════════════════════════════════════════════════
print("\n=== ⑦ 減級（自動）===")
_s0 = json.loads(json.dumps(lots.DEFAULT))
_s0["level"] = 4                                    # 20 萬級 4＋2
for eq, want in ((170001.0, 4), (169999.0, 3), (140000.0, 2), (84000.0, 1), (50000.0, 0)):
    s2, ev = lots.downgrade(_s0, eq, today=date(2026, 11, 3))
    chk("  20 萬級、權益 %s ⇒ 第 %d 級" % (format(int(eq), ","), want), s2["level"], want)
_s1 = dict(_s0, level=1)
s2, ev = lots.downgrade(_s1, 50000.0, today=date(2026, 11, 3))
chk("  7 萬級掉到 5 萬 ⇒ 第 0 級，但兩級都是 1＋1 ⇒ 不算一次減級（kind=free）", (s2["level"], [e["kind"] for e in ev]),
    (0, ["free"]))
s2, ev = lots.downgrade(_s0, 300000.0, retire={"union": "ok", "trend": "down"}, today=date(2026, 11, 3))
chk("  夜盤跟勢降級 ⇒ cap 1（夜盤 1 口）", (s2["cap"]["trend"], lots.base_lots(s2)), (1, (4, 1)))
s3, ev3 = lots.downgrade(dict(s2), 300000.0, retire={"union": "ok", "trend": "ok"}, today=date(2026, 11, 4))
chk("  ⛔ 恢復正常不會自己解除 cap（解除＝加口數，要等確認升級）", s3["cap"]["trend"], 1)
s2, ev = lots.downgrade(_s0, 300000.0, incs=[{"d": "2026-11-12", "sess": "day", "pts": -400.0, "sl": 230.0}],
                        today=date(2026, 11, 13))
chk("  停損失靈事故 11/12 ⇒ 暫停升級到 2027-05-12", s2["pause_until"], "2027-05-12")
_ps = dict(_s0, pending={"to": 5, "from": 4, "eff": "2026-11-05", "at": "x"})
s2, ev = lots.downgrade(_ps, 160000.0, today=date(2026, 11, 4))
chk("  減級時已確認還沒生效的升級作廢", (s2["level"], s2["pending"]), (3, None))
print("  -- daily_check 整條路（寫狀態檔＋紀錄）--")
clear_state()
write_state(level=2, checked="2026-11-16", up_month="2026-11")
_r = lots.daily_check([{"date": "2026-11-17", "equity": 83100.0}], now=datetime(2026, 11, 17, 14, 5), ctx=ctx_ok())
chk("  事件：down_equity", [e["kind"] for e in _r["events"]], ["down_equity"])
_lg = lots.read_log(5)
say(bool(_lg) and _lg[0]["kind"] == "down_equity" and _lg[0].get("by") == "auto"
    and _lg[0].get("from_ab") == [2, 1] and _lg[0].get("to_ab") == [1, 1],
    "  紀錄檔有一列 down_equity（by auto、2＋1 → 1＋1）", _lg[:1])
_mt = lots.LEVEL_FILE.stat().st_mtime_ns
_r2 = lots.daily_check([{"date": "2026-11-17", "equity": 83100.0}], now=datetime(2026, 11, 17, 14, 15), ctx=ctx_ok())
chk("  同一列權益再跑一次 ⇒ 不做事、不重寫檔", (_r2["ran"], lots.LEVEL_FILE.stat().st_mtime_ns == _mt), (False, True))
v = lots.view(now=datetime(2026, 11, 18, 9, 0))
chk("  view：剛減級的提示（kind down）、紀錄最近一筆 2＋1 → 1＋1",
    ((v["notice"] or {}).get("kind"), (v["hist"] or [{}])[0].get("from"), (v["hist"] or [{}])[0].get("to")),
    ("down", "2＋1", "1＋1"))
chk("  view：風控上限 1,200 而且講得出為什麼（本月用過 2＋1）", (v["cap"]["total"], bool(v["cap"]["note"])), (1200, True))

# ══ ⑧ 升級（10-14 老闆新規則：試車 → 跳級）══════════════════════════════
print("\n=== ⑧ 升級：每天檢查；先試車 2＋1、過了跳級、之後隔一個月；apply 只收現在建議的那一級 ===")


def led_day(rows):
    """日盤帳本（autofire/）寫幾列。"""
    AF.FIRE_DIR.mkdir(parents=True, exist_ok=True)
    by = {}
    for o in rows:
        by.setdefault(o["date"][:7], []).append(o)
    for m, rr in by.items():
        with (AF.FIRE_DIR / ("%s.jsonl" % m)).open("a", encoding="utf-8") as f:
            for o in rr:
                f.write(json.dumps(o, ensure_ascii=False) + "\n")


def day_res(d, qty=2, slip=2.0, live_=True, ok=True, why=None):
    o = {"rec": "result", "ok": ok, "date": d, "entry_time": "09:03:31", "slip": slip, "qty": qty, "live": live_}
    if why:
        o.update(why=why, why_msg="送出去了但沒有成交")
    return o


shutil.rmtree(AF.FIRE_DIR, ignore_errors=True)
shutil.rmtree(NF.NF_DIR, ignore_errors=True)
clear_state()
write_state(level=1, checked="2026-10-30")
N0 = datetime(2026, 11, 2, 14, 5)
_r = lots.daily_check([{"date": "2026-11-02", "equity": 105000.0}], now=N0, ctx=ctx_ok())
st, _ = lots.read_state()
chk("  11/2 在 1＋1、權益 10.5 萬、條件都過 ⇒ 建議「2＋1（試車）」",
    (st["suggest"]["ok"], st["suggest"]["to"], st["suggest"]["kind"], st["suggest"]["id"]),
    (True, 2, "trial", "2026-11-02:1>2"))
chk("  ⛔ 只建議：級數沒變、今天明天都還是 1 口", (st["level"], lots.day_lots(st, date(2026, 11, 2)),
                                     lots.day_lots(st, date(2026, 11, 3))), (1, 1, 1))
v = lots.view(now=datetime(2026, 11, 2, 15, 0), eq=105000.0)
chk("  view 端得出 offer（id、試車、生效日 11/3、升級後風控 1,200）",
    ((v["offer"] or {}).get("id"), (v["offer"] or {}).get("kind"), (v["offer"] or {}).get("eff"),
     (v["offer"] or {}).get("cap_after")), ("2026-11-02:1>2", "trial", "2026-11-03", 1200))
for bad_id, why in (("2026-11-02:1>3", "跳級"), ("2026-11-02:2>3", "建議以外的級"), ("2026-10-30:1>2", "舊的建議"),
                    ("", "空的"), (2, "數字")):
    ok, msg = lots.apply_upgrade(bad_id, now=datetime(2026, 11, 2, 15, 0), ctx=ctx_ok())
    say(not ok and lots.read_state()[0]["pending"] is None, "  apply(%r)（%s）⇒ 拒絕" % (bad_id, why), msg)
ok, msg = lots.apply_upgrade("2026-11-02:1>2", now=datetime(2026, 11, 2, 15, 0),
                             ctx=ctx_ok(retire={"union": "ok", "trend": "watch"}))
say(not ok, "  ⛔ apply 當下重驗：夜盤跟勢變成「觀察」⇒ 拒絕", msg)
ok, msg = lots.apply_upgrade("2026-11-02:1>2", now=datetime(2026, 11, 2, 15, 0),
                             rows=[{"date": "2026-11-02", "equity": 99000.0}], ctx=ctx_ok())
say(not ok, "  ⛔ apply 當下重驗：最新權益 9.9 萬不到 10 萬 ⇒ 拒絕", msg)
# ⭐ QA L2：建議之後級數變過 ⇒ 拒絕（⛔ 不可以照舊的建議升）
_sx = lots.read_state()[0]
lots.LEVEL_FILE.write_text(json.dumps(dict(_sx, level=0), ensure_ascii=False), encoding="utf-8")
ok, msg = lots.apply_upgrade("2026-11-02:1>2", now=datetime(2026, 11, 2, 15, 0),
                             rows=[{"date": "2026-11-02", "equity": 105000.0}], ctx=ctx_ok())
say(not ok and "級數變過" in str(msg) and lots.read_state()[0]["pending"] is None,
    "  ⛔ 建議之後級數變過（1 → 0）⇒ 拒絕", msg)
lots.LEVEL_FILE.write_text(json.dumps(_sx, ensure_ascii=False), encoding="utf-8")
# ⭐ QA 建議（4Hz 變慢）：apply 不帶 ctx ⇒ 用每日檢查存好的那一份，⛔ 不在 HTTP 執行緒上重算退休標準
_rs_calls = {"n": 0}
_orig_rs = lots.retire_status
_orig_eq = lots.exec_quality


def _rs_count():
    _rs_calls["n"] += 1
    return {"union": "ok", "trend": "ok"}, None


lots.retire_status = _rs_count
lots.exec_quality = lambda n=30: (_rs_calls.__setitem__("n", _rs_calls["n"] + 1), {"ok": True})[1]
try:
    _t0 = time.perf_counter()
    ok, msg = lots.apply_upgrade("2026-11-02:1>2", now=datetime(2026, 11, 2, 15, 0),
                                 rows=[{"date": "2026-11-02", "equity": 105000.0}])
    _apply_ms = (time.perf_counter() - _t0) * 1000
finally:
    lots.retire_status = _orig_rs
    lots.exec_quality = _orig_eq
st, _ = lots.read_state()
chk("  正確的 id ＋ 條件都過 ⇒ 確認成功、pending 下一個交易日 11/3 生效（試車）",
    (ok, (st["pending"] or {}).get("eff"), (st["pending"] or {}).get("kind")), (True, "2026-11-03", "trial"))
chk("  ⭐ apply 重驗用存好的那一份：退休標準／執行品質 0 次重算", _rs_calls["n"], 0)
print("        （apply 本身花 %.1f ms）" % _apply_ms)
chk("  確認當天（11/2）日盤／夜盤照舊 1 口；11/3 起 2＋1",
    (lots.day_lots(st, date(2026, 11, 2)), lots.base_lots(st, date(2026, 11, 2))[1],
     lots.base_lots(st, date(2026, 11, 3))), (1, 1, (2, 1)))
ok2, msg2 = lots.apply_upgrade("2026-11-02:1>2", now=datetime(2026, 11, 2, 15, 1), ctx=ctx_ok())
say(not ok2 and (lots.read_state()[0]["pending"] or {}).get("to") == 2, "  ⛔ 連按兩下 ⇒ 第二次拒絕（不會升兩次）", msg2)
ok, msg = lots.revoke_pending(now=datetime(2026, 11, 2, 20, 0))
chk("  生效前撤回 ⇒ 成功、回到 1＋1", (ok, lots.read_state()[0]["pending"], lots.day_lots(d=date(2026, 11, 3))),
    (True, None, 1))
ok, msg = lots.apply_upgrade("2026-11-02:1>2", now=datetime(2026, 11, 2, 21, 0),
                             rows=[{"date": "2026-11-02", "equity": 105000.0}], ctx=ctx_ok())
say(ok, "  撤回後可以再確認一次", msg)
ok, msg = lots.revoke_pending(now=datetime(2026, 11, 3, 8, 0))
say(not ok and lots.read_state()[0]["pending"], "  ⛔ 生效日當天起撤不回來", msg)
# 11/3 試車生效：日盤成交 2 口（真單）
led_day([day_res("2026-11-03")])
_r = lots.daily_check([{"date": "2026-11-03", "equity": 106000.0}], now=datetime(2026, 11, 3, 14, 5), ctx=ctx_ok())
st, _ = lots.read_state()
chk("  11/3 每日檢查：轉正（level 2、up_at 11/3、pending 清掉）、試車開始（從 11/3 算）",
    (st["level"], st["up_at"], st["pending"], [e["kind"] for e in _r["events"]], st["trial"]["status"],
     st["trial"]["start"], st["trial"]["n"]),
    (2, "2026-11-03", None, ["upgrade_effective"], "on", "2026-11-03", 1))
print("  -- 試車進度（帳本：實際成交 ≥ 2 口的自動真單；演練、1 口都不算；下單錯誤 ⇒ 從 0 重算）--")
led_day([day_res("2026-11-04"), day_res("2026-11-04", qty=1, slip=0.0), day_res("2026-11-04", live_=False, slip=0.0)])
lots.daily_check([{"date": "2026-11-04", "equity": 106000.0}], now=datetime(2026, 11, 4, 14, 5), ctx=ctx_ok())
st, _ = lots.read_state()
chk("  11/4：2 筆（1 口那筆、演練那筆不算）", (st["trial"]["n"], st["trial"]["status"]), (2, "on"))
PT = lots.panel(now=datetime(2026, 11, 4, 15, 0), eq=106000.0, lot1=35050.0)
say("試車 <b>2／5 筆</b>" in (PT["trial"]["line"] or "") and PT["trial"]["pips"] == [True, True, False, False, False]
    and PT["verdict"]["kind"] == "trial" and PT["verdict"]["title"] == "試車中：2／5 筆",
    "  ⭐ 口數卡：「試車 2／5 筆」＋五格（前兩格亮）、結論「試車中：2／5 筆」", (PT["trial"], PT["verdict"]["title"]))
say(PT["offer"] is None and not PT["alarm"], "  試車中 ⇒ 不出升級建議、不上金條")
led_day([day_res("2026-11-05", ok=False, why="order_failed")])
lots.daily_check([{"date": "2026-11-05", "equity": 106000.0}], now=datetime(2026, 11, 5, 14, 5), ctx=ctx_ok())
st, _ = lots.read_state()
chk("  11/5 一次下單錯誤 ⇒ 筆數從 0 重算、記下哪天", (st["trial"]["n"], st["trial"]["reset"]), (0, "2026-11-05"))
led_day([day_res("2026-11-%02d" % d, slip=3.0) for d in (6, 9, 10, 11)])
lots.daily_check([{"date": "2026-11-11", "equity": 106000.0}], now=datetime(2026, 11, 11, 14, 5), ctx=ctx_ok())
chk("  11/11：重算後 4 筆 ⇒ 還沒過", (lots.read_state()[0]["trial"]["n"], lots.read_state()[0]["trial"]["status"]),
    (4, "on"))
led_day([day_res("2026-11-12", slip=3.0)])
_r = lots.daily_check([{"date": "2026-11-12", "equity": 106000.0}], now=datetime(2026, 11, 12, 14, 5), ctx=ctx_ok())
st, _ = lots.read_state()
chk("  11/12：第 5 筆、平均滑價 3 ≤ 4、沒有錯誤 ⇒ 試車通過（記在狀態檔）",
    (st["trial"]["status"], st["trial"]["passed_at"], "trial_passed" in [e["kind"] for e in _r["events"]]),
    ("passed", "2026-11-12", True))
say(not (st["suggest"] or {}).get("ok") or (st["suggest"] or {}).get("withdrawn") or (st["suggest"] or {}).get("confirmed"),
    "  權益 10.6 萬只撐得起 2＋1 ⇒ 通過了也不出跳級建議")
print("  -- 試車通過 ⇒ 權益撐得到哪一級就直接跳（第一次跳不用等一個月）--")
_r = lots.daily_check([{"date": "2026-11-13", "equity": 212000.0}], now=datetime(2026, 11, 13, 14, 5), ctx=ctx_ok())
st, _ = lots.read_state()
chk("  11/13 權益 21.2 萬 ⇒ 建議直接跳到 4＋2（跳 2 級；11/3 才升過也不用等）",
    (st["suggest"]["ok"], st["suggest"]["kind"], st["suggest"]["to"], st["suggest"]["id"]),
    (True, "jump", 4, "2026-11-13:2>4"))
PJ = lots.panel(now=datetime(2026, 11, 13, 15, 0), eq=212000.0, lot1=35050.0)
say("直接跳到 <b>4＋2</b>（跳 2 級）" in PJ["offer"]["q"] and PJ["offer"]["power"] is None,
    "  確認條問句「直接跳到 4＋2（跳 2 級）」；目標日盤 4 口 ⇒ 沒有停電那一行", PJ["offer"]["q"])
ok, msg = lots.apply_upgrade("2026-11-13:2>4", now=datetime(2026, 11, 13, 15, 0),
                             rows=[{"date": "2026-11-13", "equity": 212000.0}], ctx=ctx_ok())
st, _ = lots.read_state()
chk("  確認 ⇒ pending 4（下一個交易日 11/16 一）", (ok, (st["pending"] or {}).get("to"), (st["pending"] or {}).get("eff")),
    (True, 4, "2026-11-16"))
print("  -- ⭐ 已確認還沒生效：策略掉到觀察 ⇒ 作廢（隔天開盤前那次就看）--")
_r = lots.daily_check([{"date": "2026-11-13", "equity": 212000.0}], now=datetime(2026, 11, 14, 7, 0),
                      ctx=ctx_ok(retire={"union": "watch", "trend": "ok"}))
st, _ = lots.read_state()
chk("  11/14 07:00（沒有新權益列）⇒ 作廢：pending 清掉、記下原因、建議收回",
    (st["pending"], (st["void"] or {}).get("to"), "多方聯軍" in str((st["void"] or {}).get("why")),
     bool((st["suggest"] or {}).get("withdrawn")), [e["kind"] for e in _r["events"]]),
    (None, 4, True, True, ["pending_void"]))
chk("  作廢之後 11/16 照舊 2＋1", lots.base_lots(st, date(2026, 11, 16)), (2, 1))
PV = lots.panel(now=datetime(2026, 11, 14, 8, 0), eq=212000.0, lot1=35050.0)
say(PV["verdict"]["kind"] == "void" and "作廢" in PV["verdict"]["sub"] and "多方聯軍" in PV["verdict"]["sub"]
    and PV["offer"] is None and PV["alarm"],
    "  畫面講為什麼作廢（結論 void、金條、⛔ 不再端出確認鈕）", PV["verdict"])
_r = lots.daily_check([{"date": "2026-11-13", "equity": 212000.0}], now=datetime(2026, 11, 14, 8, 0), ctx=ctx_ok())
chk("  同一天再跑一次（沒有 pending）⇒ 不做事", (_r["ran"], _r["events"]), (False, []))
_r = lots.daily_check([{"date": "2026-11-16", "equity": 212000.0}], now=datetime(2026, 11, 16, 14, 5), ctx=ctx_ok())
st, _ = lots.read_state()
chk("  11/16 策略回到正常 ⇒ 每天檢查 ⇒ 重新出跳級建議（新 id）", (st["suggest"]["ok"], st["suggest"]["id"]),
    (True, "2026-11-16:2>4"))
ok, msg = lots.apply_upgrade("2026-11-13:2>4", now=datetime(2026, 11, 16, 15, 0), ctx=ctx_ok())
say(not ok, "  ⛔ 舊的 id（11/13 那一份）⇒ 拒絕", msg)
ok, msg = lots.apply_upgrade("2026-11-16:2>4", now=datetime(2026, 11, 16, 15, 0),
                             rows=[{"date": "2026-11-16", "equity": 212000.0}], ctx=ctx_ok())
_g = lots.daily_check([{"date": "2026-11-16", "equity": 212000.0}], now=datetime(2026, 11, 17, 7, 0), ctx=ctx_ok())
st, _ = lots.read_state()
chk("  生效日開盤前看一次：策略都正常 ⇒ 不作廢（記下看過）", ((st["pending"] or {}).get("vchk"), _g["events"]),
    ("2026-11-17", []))
_r = lots.daily_check([{"date": "2026-11-17", "equity": 213000.0}], now=datetime(2026, 11, 17, 14, 5), ctx=ctx_ok())
st, _ = lots.read_state()
chk("  11/17 轉正：4＋2、up_at 11/17、記下「試車後已經跳過一次」",
    (st["level"], lots.base_lots(st, date(2026, 11, 18)), st["up_at"], st["trial"]["jumped"]),
    (4, (4, 2), "2026-11-17", True))
print("  -- 之後再升：離上一次升級生效至少 1 個日曆月（11/17 ⇒ 12/17）--")
_r = lots.daily_check([{"date": "2026-12-01", "equity": 262000.0}], now=datetime(2026, 12, 1, 14, 5), ctx=ctx_ok())
st, _ = lots.read_state()
say(not st["conds"]["ok"] and any("12-17" in w for w in st["conds"]["why"]),
    "  12/1 權益夠 25 萬級（5＋2），但 11/17 才生效 ⇒ 12/17 以前不升", st["conds"]["why"])
_r = lots.daily_check([{"date": "2026-12-16", "equity": 262000.0}], now=datetime(2026, 12, 16, 14, 5), ctx=ctx_ok())
say(not lots.read_state()[0]["conds"]["ok"], "  12/16 還差一天 ⇒ 不升")
_r = lots.daily_check([{"date": "2026-12-17", "equity": 262000.0}], now=datetime(2026, 12, 17, 14, 5), ctx=ctx_ok())
st, _ = lots.read_state()
chk("  12/17 滿一個月 ⇒ 建議升到 5＋2", (st["suggest"]["ok"], st["suggest"]["to"], st["suggest"]["kind"]),
    (True, 5, "jump"))
P5 = lots.panel(now=datetime(2026, 12, 17, 15, 0), eq=262000.0, lot1=35050.0)
_pw = P5["offer"]["power"] or {}
say("停電沒人處理時最壞一次約 <b>11.9 萬</b>" == _pw.get("line") and _pw.get("remind") == "升到這一級之前要再確認停電備援",
    "  ⭐ 目標日盤 5 口 ⇒ 確認條多一行「停電沒人處理時最壞一次約 11.9 萬」＋「升到這一級之前要再確認停電備援」", _pw)
print("  -- 試車通過之後被減級 ⇒ 不必再試車 --")
_r = lots.daily_check([{"date": "2026-12-18", "equity": 150000.0}], now=datetime(2026, 12, 18, 14, 5), ctx=ctx_ok())
st, _ = lots.read_state()
chk("  12/18 權益 15 萬 < 17 萬 ⇒ 退到 3＋1；試車照樣是通過", (st["level"], st["trial"]["status"]), (3, "passed"))
_r = lots.daily_check([{"date": "2026-12-21", "equity": 262000.0}], now=datetime(2026, 12, 21, 14, 5), ctx=ctx_ok())
st, _ = lots.read_state()
chk("  12/21 權益回到 26.2 萬 ⇒ 直接建議跳回 5＋2（不試車）", (st["suggest"]["ok"], st["suggest"]["kind"], st["suggest"]["to"]),
    (True, "jump", 5))
print("  -- 試車還沒過就被退回 1＋1 ⇒ 這次試車作廢、之後重來 --")
clear_state()
shutil.rmtree(AF.FIRE_DIR, ignore_errors=True)
write_state(level=2, checked="2026-11-16", since="2026-11-10",
            trial={"status": "on", "start": "2026-11-10", "n": 3, "slip": 2.0})
_r = lots.daily_check([{"date": "2026-11-17", "equity": 83100.0}], now=datetime(2026, 11, 17, 14, 5), ctx=ctx_ok())
st, _ = lots.read_state()
chk("  退回 1＋1 ⇒ 試車回到「還沒試」、記一列 trial_void", (st["level"], st["trial"]["status"],
                                                   [e["kind"] for e in _r["events"]]),
    (1, "none", ["down_equity", "trial_void"]))
_r = lots.daily_check([{"date": "2026-11-18", "equity": 105000.0}], now=datetime(2026, 11, 18, 14, 5), ctx=ctx_ok())
st, _ = lots.read_state()
chk("  權益回到 10.5 萬 ⇒ 重新建議試車", (st["suggest"]["ok"], st["suggest"]["kind"]), (True, "trial"))
print("  -- 試車那把尺（純函式）--")
_E = [(("2026-11-0%d" % i, "a"), "trade", 3.0) for i in range(1, 6)]
chk("  5 筆、平均 3 ⇒ 通過", lots.trial_eval(_E)["passed"], True)
chk("  4 筆 ⇒ 不過（還差 1 筆）", (lots.trial_eval(_E[:4])["passed"], lots.trial_eval(_E[:4])["why"]), (False, "還差 1 筆"))
chk("  中間一次錯誤 ⇒ 從 0 重算", lots.trial_eval(_E[:3] + [(("2026-11-04", "b"), "err", "x")] + _E[3:])["n"], 2)
chk("  平均滑價 4.4 ⇒ 不過", lots.trial_eval([(k, "trade", 4.4) for k, _t, _v in _E])["passed"], False)
chk("  剛好 4.0 ⇒ 通過（≤ 4）", lots.trial_eval([(k, "trade", 4.0) for k, _t, _v in _E])["passed"], True)
chk("  有一筆算不出滑價 ⇒ 不過（⛔ 不拿 4 筆冒充 5 筆）",
    lots.trial_eval(_E[:4] + [(("2026-11-06", "a"), "trade", None)])["passed"], False)
print("  -- 條件掉了 ⇒ 收回；條件回來 ⇒ 每天檢查，會再出新的 --")
clear_state()
write_state(level=1, checked="2026-10-30")
lots.daily_check([{"date": "2026-11-02", "equity": 105000.0}], now=N0, ctx=ctx_ok())
lots.daily_check([{"date": "2026-11-04", "equity": 105000.0}], now=datetime(2026, 11, 4, 14, 5),
                 ctx=ctx_ok(quality={"ok": False, "n": 12, "why": "目前 12 筆平均滑價 5.1 點，超過 4 點",
                                     "slip_avg": 5.1, "errors": []}))
st, _ = lots.read_state()
chk("  11/4 滑價變差 ⇒ 建議收回（withdrawn）、view 不再端出 offer",
    (st["suggest"].get("withdrawn"), lots.view(now=datetime(2026, 11, 4, 15, 0))["offer"]), ("2026-11-04", None))
ok, msg = lots.apply_upgrade("2026-11-02:1>2", now=datetime(2026, 11, 4, 15, 0), ctx=ctx_ok())
say(not ok, "  收回後 apply ⇒ 拒絕", msg)
lots.daily_check([{"date": "2026-11-05", "equity": 105000.0}], now=datetime(2026, 11, 5, 14, 5), ctx=ctx_ok())
_sg = lots.read_state()[0]["suggest"]
chk("  11/5 條件又好了 ⇒ 出一份新的（id 換成 11/5）", (_sg.get("ok"), _sg.get("withdrawn"), _sg.get("id")),
    (True, None, "2026-11-05:1>2"))
lots.daily_check([{"date": "2026-11-06", "equity": 105000.0}], now=datetime(2026, 11, 6, 14, 5), ctx=ctx_ok())
chk("  11/6 還是同一級 ⇒ 沿用 11/5 那一份（id 不變，按鈕不會一直換）", lots.read_state()[0]["suggest"]["id"], "2026-11-05:1>2")
print("  -- 7 萬那一級不算升級 --")
clear_state()
write_state(level=0, checked="2026-11-16")
_r = lots.daily_check([{"date": "2026-11-17", "equity": 75000.0}], now=datetime(2026, 11, 17, 14, 5), ctx=ctx_ok())
st, _ = lots.read_state()
chk("  權益到 7.5 萬 ⇒ 直接標第 1 級（不用確認）、口數還是 1＋1、沒有建議",
    (st["level"], st["up_at"], st["pending"], lots.base_lots(st), (st["suggest"] or {}).get("ok")), (1, None, None, (1, 1), None))
clear_state()
write_state(level=0, checked="2026-11-30")
lots.daily_check([{"date": "2026-12-01", "equity": 104000.0}], now=datetime(2026, 12, 1, 14, 5), ctx=ctx_ok())
st, _ = lots.read_state()
chk("  第 0 級、權益 10.4 萬 ⇒ 先標第 1 級，建議直接是試車 1>2", (st["level"], st["suggest"]["id"], st["suggest"]["ok"]),
    (1, "2026-12-01:1>2", True))
print("  -- 剛上線（權益 5.1 萬）⇒ 沒有建議 --")
clear_state()
lots.daily_check([{"date": "2026-10-14", "equity": 51486.0}], now=datetime(2026, 10, 14, 14, 5), ctx=ctx_ok())
st, _ = lots.read_state()
chk("  10/14 第一次跑：level 0、沒有建議、口數 1＋1", (st["level"], st["suggest"], lots.base_lots(st)), (0, None, (1, 1)))
shutil.rmtree(AF.FIRE_DIR, ignore_errors=True)
print("  -- 最近 30 筆：不滿 30 筆用現有的（日盤＋夜盤合計）--")
shutil.rmtree(AF.FIRE_DIR, ignore_errors=True)
shutil.rmtree(NF.NF_DIR, ignore_errors=True)
AF.FIRE_DIR.mkdir(parents=True)
NF.NF_DIR.mkdir(parents=True)
with (AF.FIRE_DIR / "2026-10.jsonl").open("w", encoding="utf-8") as f:
    for i, s in enumerate((3.0, 2.0, 5.0)):
        f.write(json.dumps({"rec": "result", "ok": True, "date": "2026-10-%02d" % (i + 5), "entry_time": "09:03:31",
                            "slip": s}) + "\n")
with (NF.NF_DIR / "2026-10.jsonl").open("w", encoding="utf-8") as f:
    f.write(json.dumps({"rec": "result", "ok": True, "E": "2026-10-06", "at": "x", "dir": "long",
                        "entry": 12002.0, "px": 12000.0}) + "\n")
q = lots.exec_quality()
chk("  4 筆（日盤 3＋夜盤 1）⇒ n 4、平均滑價 (3+2+5+2)/4 ＝ 3、過", (q["n"], q["slip_avg"], q["ok"]), (4, 3.0, True))
with (AF.FIRE_DIR / "2026-10.jsonl").open("a", encoding="utf-8") as f:
    f.write(json.dumps({"rec": "result", "ok": False, "date": "2026-10-09", "why": "order_failed",
                        "why_msg": "單送出去了，但沒有成交"}) + "\n")
q = lots.exec_quality()
chk("  窗口裡有一筆下單錯誤 ⇒ 不過", (q["ok"], len(q["errors"])), (False, 1))
shutil.rmtree(AF.FIRE_DIR, ignore_errors=True)
shutil.rmtree(NF.NF_DIR, ignore_errors=True)
chk("  一筆都沒有 ⇒ 算不出滑價 ⇒ 不過", lots.exec_quality()["ok"], False)
clear_state()

# ══ ⑨ 夜盤波動預測與規則 B ═════════════════════════════════════════════
print("\n=== ⑨ 夜盤波動預測（跟研究 sizing.py 同一條式子）與規則 B ===")
import numpy as np   # noqa: E402
import pandas as pd  # noqa: E402


def _research_forecast(v):
    """tick-research/scripts/sizing.py forecast() 照抄（對照組，⛔ 不是產品程式）。"""
    v = np.asarray(v, dtype=float)
    f = np.full(len(v), np.nan)
    for i in range(22, len(v)):
        f[i] = (v[i - 1] + v[i - 5:i].mean() + v[i - 22:i].mean()) / 3
    return f


rng = np.random.default_rng(7)
ser = list(rng.uniform(0.4, 2.0, 80))
ref = _research_forecast(ser)
_bad = [i for i in range(22, 80) if abs(lots.night_vol_forecast(ser[:i]) - ref[i]) > 1e-12]
chk("  80 晚隨機振幅：第 22～79 晚的預測值跟研究算法完全一樣", _bad, [])
chk("  不到 22 晚 ⇒ None", lots.night_vol_forecast(ser[:21]), None)
chk("  最近 22 晚有 NaN ⇒ None", lots.night_vol_forecast(ser[:30] + [float("nan")]), None)
chk("  門檻 1.03 固定", lots.NIGHT_VOL_THR, 1.03)
_flat = [("2026-05-%02d" % (i + 1), 1.03) for i in range(25)]
chk("  剛好 1.03 ⇒ 偏高（≥）", lots.night_vol_of("2026-05-26", _flat)["high"], True)
chk("  1.0299 ⇒ 不偏高", lots.night_vol_of("2026-05-26", [(k, 1.0299) for k, _v in _flat])["high"], False)
_fut = _flat + [("2026-05-26", 50.0), ("2026-05-27", 50.0)]
chk("  ⛔ 今晚與之後的振幅不准用（偷看未來）", lots.night_vol_of("2026-05-26", _fut)["vol"], 1.03)
chk("  資料太舊（最新一晚 5/25、今晚 6/5）⇒ 不算偏高、講得出原因",
    (lots.night_vol_of("2026-06-05", _flat)["high"], bool(lots.night_vol_of("2026-06-05", _flat)["why"])), (None, True))
chk("  規則 B：b＝1 偏高／不偏高／算不出來 ⇒ 1／1／1", [lots.night_qty(1, h) for h in (True, False, None)], [1, 1, 1])
chk("  規則 B：b＝2 ⇒ 2／1／1；b＝4 ⇒ 4／3／3", [lots.night_qty(2, h) for h in (True, False, None)] +
    [lots.night_qty(4, h) for h in (True, False, None)], [2, 1, 1, 4, 3, 3])
chk("  b＝0（退休歸零）⇒ 0", lots.night_qty(0, True), 0)
print("  -- 振幅沿用【健檢】health._sessions（假的 1 分 K 檔）--")
_rows = []
for k in range(25):
    E = date(2026, 4, 1) + timedelta(days=k)
    t0 = datetime(E.year, E.month, E.day, 15, 1)
    for m in range(300):
        p = 20000.0 + (k % 3) * 10 + (m % 7)
        _rows.append({"ts": t0 + timedelta(minutes=m), "Open": 20000.0 if m == 0 else p,
                      "High": p + (40 if m == 100 else 0), "Low": p - (60 if m == 200 else 0), "Close": p,
                      "Volume": 1, "Amount": 1})
_csv = TMP / "__tmp__tmf.csv"
pd.DataFrame(_rows).to_csv(_csv, index=False)
amps, aerr = lots.night_amps(_csv)
_df = pd.DataFrame(_rows)
_df["E"] = (_df["ts"] - pd.Timedelta(hours=6)).dt.date.astype(str)
_g = _df.groupby("E").agg(o=("Open", "first"), h=("High", "max"), l=("Low", "min"))
_want = [(k, round(float((r.h - r.l) / r.o * 100), 6)) for k, r in _g.iterrows()]
chk("  25 晚的振幅 ＝（最高 − 最低）÷ 開盤（獨立用 pandas 算一次對照）",
    [(k, round(v, 6)) for k, v in amps], _want)
say(len(amps) == 25 and aerr is None, "  尺的自證：真的讀到 25 晚、沒有錯誤", aerr)
_pl = lots.night_plan(date(2026, 4, 26), rows=amps)
chk("  night_plan：1＋1 ⇒ 1 口、⛔ 不算振幅", (_pl["qty"], _pl["vol"]), (1, None))
write_state(level=4)
_pl = lots.night_plan(date(2026, 4, 26), rows=amps)
chk("  night_plan：4＋2 ⇒ 用這份算出預測值", (_pl["b"], _pl["vol"] is not None), (2, True))
clear_state()

# ══ ⑩ 保證金：max(a, b) 口 ═════════════════════════════════════════════
print("\n=== ⑩ live_panel.equity_view：保證金看 max(a, b) 口 ===")
import live_panel as LP  # noqa: E402
REAL["LP.EQUITY_DIR"] = LP.EQUITY_DIR
LP.EQUITY_DIR = TMP / "equity"
LP.EQUITY.update({"m": {"equity_amount": 90000.0, "available_margin": 90000.0, "initial_margin": 0.0},
                  "err": None, "tfx": 35050.0, "tfx_at": "2026/10/01"})
clear_state()
e1 = LP.equity_view()["enough"]
chk("  1＋1：要 35,050、那句話跟以前一樣（一口要／夠下一口）",
    (e1["need"], e1["msg"]), (35050.0, "可動用 90,000／一口要 35,050（期交所公告（10/01 起））—— 夠下一口"))
write_state(level=4)
e4 = LP.equity_view()["enough"]
chk("  4＋2：要 4 × 35,050 ＝ 140,200 ⇒ 不夠", (e4["need"], e4["ok"], e4.get("lots")), (140200.0, False, 4))
say("4 口要 140,200" in e4["msg"], "  那句話講口數", e4["msg"])
LP.EQUITY.update({"m": {"equity_amount": 90000.0, "available_margin": 20000.0, "initial_margin": 70100.0}})
write_state(level=2)
e2 = LP.equity_view()["enough"]
chk("  2＋1 有部位：平倉後約可動用 90,100 ≥ 2 × 35,050 ⇒ 夠（保留「看平倉後」的行為）",
    (e2["need"], e2["ok"], e2.get("why")), (70100.0, True, "holding"))
clear_state()
chk("  risk_view 用 lots（1＋1 ⇒ 800）", LP.risk_view(force=True).get("cap"), 800.0)

# ══ ⑪ 第二階段（PM 10-14 拍板）═══════════════════════════════════════
print("\n=== ⑪ 「下單錯誤」只算送出去之後失敗（週報那把尺不動）===")
import analyst_facts as AFX  # noqa: E402
shutil.rmtree(AF.FIRE_DIR, ignore_errors=True)
shutil.rmtree(NF.NF_DIR, ignore_errors=True)
AF.FIRE_DIR.mkdir(parents=True)
NF.NF_DIR.mkdir(parents=True)
_day_rows = [
    {"rec": "result", "ok": True, "date": "2026-10-05", "entry_time": "09:03:31", "slip": 3.0},
    {"rec": "skip", "date": "2026-10-06", "why": "no_quote", "why_msg": "09:03:30 收不到成交價"},
    {"rec": "skip", "date": "2026-10-07", "why": "late", "why_msg": "太晚"},
    {"rec": "result", "ok": False, "date": "2026-10-08", "why": "cant_enter", "why_msg": "券商那一關擋下來了"},
]
with (AF.FIRE_DIR / "2026-10.jsonl").open("w", encoding="utf-8") as f:
    for o in _day_rows:
        f.write(json.dumps(o, ensure_ascii=False) + "\n")
with (NF.NF_DIR / "2026-10.jsonl").open("w", encoding="utf-8") as f:
    f.write(json.dumps({"rec": "result", "ok": True, "E": "2026-10-05", "at": "a", "dir": "long",
                        "entry": 12001.0, "px": 12000.0}) + "\n")
    f.write(json.dumps({"rec": "skip", "E": "2026-10-06", "why": "cannot", "msg": "現在不能進場"}) + "\n")
q = lots.exec_quality()
chk("  沒送的（收不到成交價／太晚／券商那一關擋下／夜盤不能進場）⛔ 不算下單錯誤", (q["errors"], q["ok"]), ([], True))
say(any("收不到成交價" in str(e) for e in [AFX.day_err(o) for o in _day_rows]),
    "  對照：週報那把尺（analyst_facts.day_err）照舊算它們（⛔ 沒改週報）")
with (AF.FIRE_DIR / "2026-10.jsonl").open("a", encoding="utf-8") as f:
    f.write(json.dumps({"rec": "result", "ok": False, "date": "2026-10-09", "why": "order_failed",
                        "why_msg": "沒有成交"}, ensure_ascii=False) + "\n")
chk("  日盤 order_failed（送出去之後沒成交／被拒）⇒ 算", len(lots.exec_quality()["errors"]), 1)
with (NF.NF_DIR / "2026-10.jsonl").open("a", encoding="utf-8") as f:
    f.write(json.dumps({"rec": "result", "ok": False, "E": "2026-10-09", "at": "b", "err": "券商拒絕"},
                       ensure_ascii=False) + "\n")
    f.write(json.dumps({"rec": "eod", "E": "2026-10-09", "why": "failed", "msg": "平不掉"}, ensure_ascii=False) + "\n")
with (AF.FIRE_DIR / "2026-10.jsonl").open("a", encoding="utf-8") as f:
    f.write(json.dumps({"rec": "eod", "date": "2026-10-09", "why": "eod_failed", "alarm": True,
                        "why_msg": "收盤平倉沒有成功"}, ensure_ascii=False) + "\n")
chk("  夜盤送單失敗、夜盤 04:58 平不掉、日盤收盤平倉平不掉 ⇒ 都算（共 4 次）", len(lots.exec_quality()["errors"]), 4)
shutil.rmtree(AF.FIRE_DIR, ignore_errors=True)
shutil.rmtree(NF.NF_DIR, ignore_errors=True)

print("\n=== ⑫ 休市平日：不出新建議；夜盤振幅「太舊」用交易日算（QA 10-14）===")
clear_state()
write_state(level=1, checked="2026-11-30")
lots.daily_check([{"date": "2026-12-01", "equity": 105000.0}], now=datetime(2026, 12, 1, 14, 5), ctx=ctx_ok(),
                 closed={"2026-12-01"})
chk("  12/1 認得出休市 ⇒ 那天不出建議", lots.read_state()[0]["suggest"], None)
lots.daily_check([{"date": "2026-12-02", "equity": 105000.0}], now=datetime(2026, 12, 2, 14, 5), ctx=ctx_ok(),
                 closed={"2026-12-01"})
chk("  ⇒ 下一個交易日（12/2）出", (lots.read_state()[0]["suggest"] or {}).get("made"), "2026-12-02")
chk("  first_open_weekday 跳過休市（畫面／舊呼叫還在用）", str(lots.first_open_weekday(2026, 12, {"2026-12-01"})), "2026-12-02")
clear_state()
# 春節：最新一晚 2/13（五），今晚 2/23（一）；中間 2/16~2/20 休市
_cny = [("2026-01-%02d" % (i + 1), 1.2) for i in range(22)] + [("2026-02-13", 1.2)]
_old_kc = lots.KNOWN_CLOSED
lots.KNOWN_CLOSED = {"2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20"}
_nv = lots.night_vol_of(date(2026, 2, 23), _cny)
chk("  春節後第一晚（認得出休市）⇒ 只隔 1 個交易日 ⇒ 不算太舊、照常算", (_nv["why"], _nv["high"]), (None, True))
lots.KNOWN_CLOSED = set()
_nv = lots.night_vol_of(date(2026, 2, 23), _cny)
say(_nv["high"] is None and "交易日" in str(_nv["why"]), "  認不出休市 ⇒ 隔 6 個交易日 ⇒ 太舊、照實講", _nv["why"])
lots.KNOWN_CLOSED = _old_kc
chk("  trading_gap：週五 → 下週一 ＝ 1", lots.trading_gap("2026-11-13", "2026-11-16", set()), 1)
chk("  trading_gap：週五 → 下週三 ＝ 3（> 2 ⇒ 太舊）", lots.trading_gap("2026-11-13", "2026-11-18", set()), 3)

print("\n=== ⑬ 給畫面的整份（LOTS-UI-SPEC §5）：句子後端組好 ===")
clear_state()
P = lots.panel(now=datetime(2026, 10, 14, 20, 40), eq=51230.0, lot1=35050.0)
chk("  沒有狀態檔：大字 1＋1、起始級、夜盤一行「今晚 1 口」", (P["run"]["label"], P["run"]["tier"], P["tonight"]["line"]),
    ("1＋1", "起始級", "今晚 <b>1 口</b>"))
chk("  下一級跳過 7 萬那一級（同樣 1＋1）⇒ 10 萬・2＋1", (P["next"]["lvl"], P["next"]["label"]), (2, "2＋1"))
chk("  還差 4.9 萬 ⇒「離下一級還遠」（4 萬以內才寫還差幾萬）", P["verdict"]["title"], "離下一級還遠")
chk("  沒事 ⇒ 沒有金條、沒有小金點、手機不進「要注意的事」", (P["alarm"], P["dot"], P["phone"]["warn"]), ([], False, None))
chk("  藥丸「1 口」「1 口」", P["pills"], {"day": "1 口", "night": "1 口"})
say("1＋1" in P["lots_line"]["text"] and not P["lots_line"]["att"], "  帳戶那一行：現在做 1＋1（灰底）", P["lots_line"]["text"])
P0 = lots.panel(now=datetime(2026, 10, 14, 20, 40), eq=None, lot1=None)
say(P0["ok"] and P0["run"]["label"] == "1＋1", "  權益／保證金都不知道也畫得出來（不丟例外）")
write_state(level=4)
lots._AMP.update(key=None, rows=None, err=None)     # 快取清掉（⑨ 讀過的那份留著會蓋過「還沒算」這個情境）
P4 = lots.panel(now=datetime(2026, 10, 14, 20, 40), eq=213400.0, lot1=35050.0)
say("2 口" in P4["tonight"]["line"] or "1 口" in P4["tonight"]["line"], "  4＋2：今晚那一行講口數", P4["tonight"]["line"])
say(P4["tonight"]["line"].startswith("今晚 <b>1 口</b>：振幅指標還沒算好") and "照沒偏高算" in P4["tonight"]["line"]
    and P4["pills"]["night"] == "1 口",
    "  ⛔ 振幅還沒算 ⇒ 照實講「還沒算好…，照沒偏高算」1 口，跟送單路徑同一個數（藥丸也是 1 口）", P4["tonight"]["line"])
lots._AMP.update(key="x", rows=[("2026-09-%02d" % (i + 1), 1.5) for i in range(25)], err=None)
P4s = lots.panel(now=datetime(2026, 10, 14, 20, 40), eq=213400.0, lot1=35050.0)
say("太舊" in P4s["tonight"]["line"] and "照沒偏高算" in P4s["tonight"]["line"] and P4s["pills"]["night"] == "1 口",
    "  振幅資料太舊 ⇒「…太舊…，照沒偏高算」、1 口", P4s["tonight"]["line"])
lots._AMP.update(key="x", rows=[("2026-09-%02d" % (i + 1), 1.5) for i in range(25)] +
                 [("2026-10-%02d" % (i + 1), 1.5) for i in range(13)], err=None)
P4b = lots.panel(now=datetime(2026, 10, 14, 20, 40), eq=213400.0, lot1=35050.0)
say("2 口" in P4b["tonight"]["line"] and "1.50%" in P4b["tonight"]["line"] and "1.03%" in P4b["tonight"]["line"],
    "  振幅指標 1.50% ≥ 1.03% ⇒「今晚 2 口：近幾晚夜盤振幅指標 1.50%（≥ 1.03% 算偏高）」", P4b["tonight"]["line"])
lots._AMP.update(key=None, rows=None, err=None)
chk("  4＋2 的徽章與退級線", (P4["run"]["tier"], P4["bar"]["lo_txt"]), ("第 4 級・20 萬", "退級線 17 萬"))
clear_state()
write_state(level=1, checked="2026-10-30", up_month="2026-10")
lots.daily_check([{"date": "2026-11-02", "equity": 106480.0}], now=datetime(2026, 11, 2, 14, 5), ctx=ctx_ok())
PO = lots.panel(now=datetime(2026, 11, 2, 15, 0), eq=106480.0, lot1=35050.0)
chk("  建議：金條一則、小金點、手機進「要注意的事」", (len(PO["alarm"]), PO["dot"], bool(PO["phone"]["warn"])), (1, True, True))
_cost = {c["k"]: (c["now"], c["after"]) for c in PO["offer"]["costs"]}
chk("  代價表：保證金 ＝ 面板學到的一口 × max(a,b)（⛔ 不寫死）", _cost["要壓的保證金"], ("35,050", "70,100"))
chk("  代價表：回落中位數照研究（1＋1 2 萬 → 2＋1 3.1 萬）", _cost["從高點回落：一般（中位數）"], ("約 2 萬", "約 3.1 萬"))
chk("  代價表：風控上限 −800 → −1,200", _cost["本月風控上限"], ("−800 點", "−1,200 點"))
PO2 = lots.panel(now=datetime(2026, 11, 2, 15, 0), eq=106480.0, lot1=None)
chk("  學不到一口保證金 ⇒ 照實寫「不知道」", {c["k"]: c["now"] for c in PO2["offer"]["costs"]}["要壓的保證金"],
    "不知道（還沒學到一口保證金）")
say(all(k in lots.LADDER_COST for k in [tuple(L[1:3]) for L in goal.LADDER]),
    "  積極版每一級（含 7＋3、9＋4、10＋4）都有研究算出來的代價（⛔ 不內插）")
import goal as _G  # noqa: E402
_GV = _G.view([{"date": "2026-10-01", "equity": 90000.0}], 106480.0, now=datetime(2026, 11, 2, 15, 0))
_GO = lots.goal_overlay(_GV, PO)
chk("  目標卡：7 萬那一列「現在在跑」、10 萬那一列「權益到了・等你確認」",
    [(s["lvl"], s["state"]) for s in _GO["steps"]][:3], [(1, "run"), (2, "wait"), (3, "")])
say("在跑起始級" in _GO["pos"] and "等你到【自動下單】確認" in _GO["pos"],
    "  ⛔ 權益夠了 ≠ 已經升級：那句話不寫「在第 2 級」", _GO["pos"])
chk("  ⛔ 目標卡的計畫線沒被動到（overlay 不改 line）", _GO["line"], _GV["line"])
lots.apply_upgrade(PO["offer"]["id"], now=datetime(2026, 11, 2, 15, 1),
                   rows=[{"date": "2026-11-02", "equity": 106480.0}], ctx=ctx_ok())
PP = lots.panel(now=datetime(2026, 11, 2, 15, 2), eq=106480.0, lot1=35050.0)
chk("  確認後：藥丸「1 口 → 明天 2 口」、手機標籤、沒有金條", (PP["pills"]["day"], PP["phone"]["label"], PP["alarm"]),
    ("1 口 → 明天 2 口", "1＋1（11/3 起試車 2＋1）", []))
chk("  確認後：目標卡 10 萬那一列「已確認・11/3 起」",
    [s["state_txt"] for s in lots.goal_overlay(_GV, PP)["steps"]][1], "已確認・11/3 起")
clear_state()
write_state(level=2, checked="2026-11-16", up_month="2026-11")
lots.daily_check([{"date": "2026-11-17", "equity": 83100.0}], now=datetime(2026, 11, 17, 14, 5), ctx=ctx_ok())
PD = lots.panel(now=datetime(2026, 11, 17, 15, 0), eq=83100.0, lot1=35050.0)
chk("  剛減級：結論 down、金條、手機警示", (PD["verdict"]["kind"], len(PD["alarm"]), bool(PD["phone"]["warn"])), ("down", 1, True))
clear_state()
write_state(level=4, cap={"union": None, "trend": 0})
PR = lots.panel(now=datetime(2026, 11, 17, 15, 0), eq=213000.0, lot1=35050.0)
say(any("已退休：自動壓到 1 口，要不要關掉由你按" in (a.get("text") or "") for a in PR["alarm"]),
    "  ⭐ 退休：金條寫「已退休：自動壓到 1 口，要不要關掉由你按」", PR["alarm"])
chk("  退休那條實際送 1 口（RETIRE_ZERO=False）", lots.base_lots(), (4, 1))
clear_state()

print("\n=== ⑭ ⛔ LOTS_LEVEL.json 的寫入者只有 lots.py（同 AUTO_ORDERS_ON 那套精神）===")
import ast  # noqa: E402
_writers = []
for _p in sorted(HERE.glob("*.py")):
    if _p.name.startswith("test_"):
        continue
    _s = _p.read_text(encoding="utf-8")
    _tree = ast.parse(_s)
    for _n in ast.walk(_tree):
        if isinstance(_n, ast.Call) and isinstance(_n.func, (ast.Attribute, ast.Name)):
            _nm = _n.func.attr if isinstance(_n.func, ast.Attribute) else _n.func.id
            if _nm == "_write_state":
                _writers.append(_p.name)
        # ⚠️ backup_data.py 的白名單寫了 "LOTS_LEVEL.json"：那是**從這裡複製出去**備份（唯讀），不是寫入者
        if isinstance(_n, ast.Constant) and isinstance(_n.value, str) and "LOTS_LEVEL" in _n.value \
                and _p.name != "lots.py" and not (_p.name == "backup_data.py" and _n.value == "LOTS_LEVEL.json"):
            _writers.append(_p.name + "（字面 LOTS_LEVEL）")
        if isinstance(_n, ast.Attribute) and _n.attr == "LEVEL_FILE" and _p.name != "lots.py":
            _writers.append(_p.name + "（碰 LEVEL_FILE）")
chk("  ⛔ 整個 tools/shioaji（測試以外）呼叫 _write_state／碰 LEVEL_FILE 的只有 lots.py", sorted(set(_writers)), ["lots.py"])
_lt = ast.parse(pathlib.Path(lots.__file__).read_text(encoding="utf-8"))
_callers = set()
for _f in ast.walk(_lt):
    if isinstance(_f, ast.FunctionDef):
        for _n in ast.walk(_f):
            if isinstance(_n, ast.Call) and getattr(_n.func, "id", None) == "_write_state":
                _callers.add(_f.name)
chk("  ⛔ lots.py 裡會寫狀態檔的只有 apply_upgrade／revoke_pending／daily_check（＋每日的 _pending_guard）",
    sorted(_callers), ["_pending_guard", "apply_upgrade", "daily_check", "revoke_pending"])
_lsrc = pathlib.Path(lots.__file__).read_text(encoding="utf-8")
say(_lsrc.count("tmp.replace(p)") == 1 and "open(" not in _lsrc.split("def _write_state")[1].split("def _log")[0],
    "  寫法是原子換檔（tmp → replace），⛔ 沒有就地覆寫")

print("\n=== ⑮ 前端（原始碼量尺）===")
_LPS = pathlib.Path(LP.__file__).read_text(encoding="utf-8")   # ⚠️ 突變時讀的是 MUT_DIR 那一份
_page = _LPS[_LPS.index('PAGE = r"""'):]
_fire_html = _page[_page.index('<div id="tab-fire" hidden>'):_page.index("【自動下單】到此")]
say('<div id="allots"></div>' in _fire_html and '<div id="allotsbar"></div>' in _fire_html,
    "  【自動下單】有口數卡與頂部金條的空容器")
chk("  ⛔ 這一頁靜態 HTML 仍然 0 顆 button", _fire_html.count("<button"), 0)
_js = _page[_page.index("<script>"):]
_first = _js[_js.index("const uo=e.target.closest('[data-upon]')"):_js.index("const un=e.target.closest('[data-upno]')")]
say("pfetch" not in _first and "fetch(" not in _first, "  ⛔ 升級第一段（data-upon）零請求", _first[:80])
_arm = _js[_js.index("function ltArm(){"):_js.index("function ltRevoke(){")]
say("pfetch('/api/lots/upgrade',JSON.stringify({offer_id:O.id}))" in _arm,
    "  第二段才 POST，而且 offer_id 是後端給的 O.id（⛔ 前端不組 id）")
chk("  ⛔ /api/lots/upgrade 整份前端只有一個呼叫點（在 ltArm 裡）", _js.count("'/api/lots/upgrade'"), 1)
_yes = _js[_js.index("const uy=e.target.closest('[data-upyes]')"):_js.index("const ur=e.target.closest('[data-ltrev]')")]
say("ltArm()" in _yes, "  ltArm 只從確認條「確定」（data-upyes）那一顆叫")
chk("  ltArm 整份前端只有那一個呼叫點", _js.count("ltArm();"), 1)
_conf = _js[_js.index("function ltActHTML(L){"):_js.index("function ltBarHTML(L){")]
say("al-conf real" in _conf and "data-upyes" in _conf and "data-upno" in _conf,
    "  確認條是紅底（al-conf real），有「確定」與「取消」")
say("21:00" not in _js[_js.index("var LTUP="):_js.index("function ltRevoke(){")],
    "  ⛔ 口數那段沒有寫死 21:00（夜盤判定時刻一律用後端 look_at）")
say("'　1 口</div>'" not in _js, "  ⛔ 夜盤「已出手」那句不再寫死「1 口」")
_pos = _js[_js.index("function alPosHTML(D,row){"):_js.index("function alPts(")]
say("multi?'點／口':'點'" in _pos and "const mq=multi?" in _pos,
    "  部位卡：多口才改「點／口」與「N 口合計」（1 口時 mq 是空字串）")

print("\n=== ⑯ QA 10-14 退件那幾條（每一條都要有一個會紅的測試）===")
AF.EOD_WINDOW_S = 3.0
AF.EOD_RETRY_S = 0.2
_save_cd = broker.CLOSE_COOLDOWN
broker.CLOSE_COOLDOWN = 0.0
_save_max = broker.MAX_ENTRIES
broker.MAX_ENTRIES = 99


class LagAPI(MultiAPI):
    """
    部位回報慢一拍：新倉單成交後，前 `lag` 次 list_positions 只回 `part` 口。
    `deal_lag`＞0 ⇒ 委託單的成交明細也慢：前 deal_lag 次 update_status 只看得到 part 口（模擬明細一筆一筆進來）。
    """

    def __init__(self, *a, lag=0, part=1, deal_lag=0, **k):
        MultiAPI.__init__(self, *a, **k)
        self.lag, self.part, self.deal_lag = lag, part, deal_lag
        self.pending, self.dpend, self.ent = 0, 0, None

    def place_order(self, contract, order):
        t = MultiAPI.place_order(self, contract, order)
        if str(order.octype).endswith("New"):
            self.pending, self.dpend, self.ent = self.lag, self.deal_lag, t
            if self.deal_lag:
                self._all = list(t.status.deals)
                t.status.deals = self._all[:self.part]
        return t

    def update_status(self, acc=None):
        if self.dpend > 0:
            self.dpend -= 1
            if self.dpend == 0:
                self.ent.status.deals = list(self._all)

    def list_positions(self, acc=None):
        if self.pending > 0:
            self.pending -= 1
            return [type("P", (), {"code": "TMFI6", "quantity": self.part, "direction": self.side,
                                   "price": self.px})()]
        return MultiAPI.list_positions(self, acc)


print("  -- 必修 2：部分成交不能只看「同一個口數看到幾次」，要對委託單自己的明細 --")
# B1：明細一筆一筆進來（前 2 次只看得到 1 口）、部位也慢（前 2 次 1 口）⇒ 一開始「部位＝明細＝1」是假的一致
api = LagAPI(lag=2, part=1, deal_lag=2)
r = af_send(api, 3)
chk("  ⛔ 回報慢一拍、一開始部位與明細都只有 1 口 ⇒ 等到 3 口：帳本 3 口、停利 3 口",
    (r.get("qty"), qtys(api, "target")), (3, [3]))
live(False)
# M1：成交 3 口、明細馬上 3 口，但部位一直只回 1 口直到期限 ⇒ 先照 1 口記＋警示；之後部位變 3 ⇒ 認得出是我們的
api = LagAPI(lag=4, part=1)
r = af_send(api, 3)
p = broker._state["position"]
say(r.get("qty") == 1 and p.get("fill_warn") and any(o.get("kind") == "entry_fill_unsure" for o in order_log()),
    "  明細 3 口、部位到期限還是 1 口 ⇒ 先照券商的 1 口記，而且講出來（委託 log entry_fill_unsure）", p.get("fill_warn"))
api.pending = 0                                    # 部位回報趕上了
broker._LAST_RECONCILE["at"] = 0.0
broker.reconcile()
p = broker._state["position"]
chk("  之後部位變 3 口：明細也是 3 ⇒ 是我們的（entry_qty 3、late_fill、⛔ 不是「他動過」）",
    (p.get("qty"), p.get("entry_qty"), p.get("late_fill"), p.get("touched")), (3, 3, True, None))
chk("  收盤平倉認得出來（帳本 1 口、送出 3 口、部位 3 口 ⇒ 我們的）", AF._looks_ours(dict(p), r)[0], True)
AF._eod(DAY, 0, time.time(), "13:43:30")
_eod = [o for o in AF._rows_of(DAY) if o.get("rec") == "eod"]
chk("  ⭐ 13:43:30 平掉 3 口、⛔ 不留倉過夜（QA M1：舊版留了 3 口）",
    ([o.get("why") for o in _eod], qtys(api, "close"), len(api.lots)), (["eod_closed"], [3], 0))
live(False)
# 明細與部位「一起慢」、一致地停在 1 口 ⇒ 當下定案 1 口（沒有警示）；之後兩邊都變 3 口 ⇒ 認成我們的（late_fill）、收盤照平
api = LagAPI(lag=99, part=1, deal_lag=99)
r = af_send(api, 3)
chk("  明細與部位一致停在 1 口 ⇒ 定案 1 口、⛔ 沒有 fill_unsure", (r.get("qty"), r.get("fill_unsure")), (1, None))
api.pending, api.dpend = 0, 0
api.ent.status.deals = list(api._all)              # 剩下 2 口的明細也進來了
broker._LAST_RECONCILE["at"] = 0.0
broker.reconcile()
p = broker._state["position"]
chk("  之後兩邊都是 3 口 ⇒ late_fill、entry_qty 3", (p.get("qty"), p.get("entry_qty"), p.get("late_fill")), (3, 3, True))
AF._eod(DAY, 0, time.time(), "13:43:30")
_eod = [o for o in AF._rows_of(DAY) if o.get("rec") == "eod"]
chk("  ⭐ 收盤認得出（late_fill ⇒ 最多認到 3 口）⇒ 平掉 3 口",
    ([o.get("why") for o in _eod], qtys(api, "close"), len(api.lots)), (["eod_closed"], [3], 0))
live(False)
# M1＋面板中途重啟（記憶體裡的 late_fill 沒了）⇒ 帳本 fill_unsure 讓收盤照樣認得出 3 口
api = LagAPI(lag=4, part=1)
r = af_send(api, 3)
chk("  帳本記 fill_unsure、qty 1、qty_sent 3", (r.get("fill_unsure"), r.get("qty"), r.get("qty_sent")), (True, 1, 3))
api.pending = 0
broker._state["position"] = None                   # 模擬看門狗重啟：記憶體清空
_hook = broker.RECOVER_HOOK
broker.RECOVER_HOOK = AF.recover_meta
try:
    broker._LAST_RECONCILE["at"] = 0.0
    broker.reconcile()
finally:
    broker.RECOVER_HOOK = _hook
p = broker._state["position"]
chk("  重啟後撿回 3 口、補回 entry_qty 3（帳本 fill_unsure ⇒ 最多認到送出去的 3 口）",
    (p.get("qty"), p.get("entry_qty"), p.get("recovered")), (3, 3, True))
AF._eod(DAY, 0, time.time(), "13:43:30")
_eod = [o for o in AF._rows_of(DAY) if o.get("rec") == "eod"]
chk("  ⭐ 重啟過也照樣平掉 3 口（一張 3 口）、⛔ 不留倉過夜",
    ([o.get("why") for o in _eod], qtys(api, "close"), len(api.lots)), (["eod_closed"], [3], 0))
live(False)

print("  -- 必修 2：平倉中部位一直問不到 ⇒ ⛔ 不盲目再送（QA M3：舊版平過頭 3 口）--")


class UnkAPI(MultiAPI):
    def __init__(self, *a, **k):
        MultiAPI.__init__(self, *a, **k)
        self.unk = 0

    def place_order(self, contract, order):
        t = MultiAPI.place_order(self, contract, order)
        if str(order.octype).endswith("Cover") and str(order.price_type).endswith("MKP"):
            self.unk = int(broker.FILL_WAIT / 0.4) + 2
        return t

    def list_positions(self, acc=None):
        if self.unk > 0:
            self.unk -= 1
            raise RuntimeError("網路斷一下")
        return MultiAPI.list_positions(self, acc)


api = UnkAPI()
r = af_send(api, 3)
ok, err = broker.close("sl")
chk("  整段問不到部位 ⇒ 看平倉單自己的明細（3 口都成交）⇒ 平完、只送一張、⛔ 沒平過頭",
    (ok, qtys(api, "close"), api.overclosed, broker._state["position"]), (True, [3], 0, None))
chk("  成績單記 3 口", trades_file()[-1]["qty"], 3)
api = UnkAPI(cover_fill=[0])
r = af_send(api, 3)
ok, err = broker.close("sl")
chk("  問不到部位、但明細說第一張 0 口 ⇒ 才補送下一張 ⇒ 平完、⛔ 沒平過頭",
    (ok, qtys(api, "close"), api.overclosed, broker._state["position"]), (True, [3, 3], 0, None))
live(False)

print("  -- 必修 3：停利部分成交後他自己加碼 ⇒ 收盤⛔ 不平、停損只平我們剩下的、成績單不多算 --")
for tag, add_px in (("不同價", 12040.0), ("同價", 12010.0)):
    api = MultiAPI()
    r = af_send(api, 3)
    api.fill_target(2, 12070.0)                     # 停利先吃 2 口 ⇒ 我們剩 1 口
    broker._LAST_RECONCILE["at"] = 0.0
    broker.reconcile()
    api.lots.append(add_px)                         # 他在大戶投自己加 1 口
    broker._LAST_RECONCILE["at"] = 0.0
    broker.reconcile()
    p = broker._state["position"]
    chk("  [%s] 對帳：口數變多、明細對不上 ⇒ 標「他動過」、⛔ 不蓋進場價、我們剩 1 口" % tag,
        (p.get("touched"), p.get("entry"), broker._ours_left(p), p.get("qty")), (True, 12010.0, 1, 2))
    AF._eod(DAY, 0, time.time(), "13:43:30")
    _eod = [o for o in AF._rows_of(DAY) if o.get("rec") == "eod"]
    chk("  [%s] ⭐ 收盤：混在一起 ⇒ ⛔ 不平（eod_not_ours、0 張平倉單、券商還是 2 口）" % tag,
        ([o.get("why") for o in _eod], qtys(api, "close"), len(api.lots)), (["eod_not_ours"], [], 2))
    ok, err = broker.close("sl")
    p = broker._state["position"]
    chk("  [%s] 停損：⛔ 只平我們的 1 口、他的 1 口留著（改成他的手動部位）" % tag,
        (ok, qtys(api, "close"), len(api.lots), (p or {}).get("qty"), (p or {}).get("sl_src")),
        (True, [1], 1, 1, "manual"))
    _tr = trades_file()[-1]
    chk("  [%s] ⭐ 成績單記 3 口（停利 2＋停損 1）、⛔ 不是 4 口" % tag, (_tr["qty"], [l["qty"] for l in _tr["legs"]]),
        (3, [2, 1]))
    live(False)
# 他動過之後，他自己在別處平掉 1 口（⛔ 不是停利單吃的）⇒ 先算成他那口，我們剩下的口數不變
api = MultiAPI()
r = af_send(api, 3)
api.fill_target(2, 12070.0)
broker._LAST_RECONCILE["at"] = 0.0
broker.reconcile()
api.lots.append(12040.0)
broker._LAST_RECONCILE["at"] = 0.0
broker.reconcile()
api.lots.pop()                                     # 他把自己加的那口平掉
broker._LAST_RECONCILE["at"] = 0.0
broker.reconcile()
p = broker._state["position"]
chk("  他動過後自己減 1 口（不是停利）⇒ 算他的：我們還剩 1 口、legs 只有停利那一段、照樣標「他動過」",
    (broker._ours_left(p), [l["qty"] for l in p.get("legs") or []], p.get("touched"), p.get("sl_src")),
    (1, [2], True, None))
live(False)
chk("  夜盤 04:58 那一把尺也一樣：他動過 ⇒ ⛔ 不碰（沒動過照舊認得出來）",
    (NF.looks_ours({"dir": "long", "entry": 12010.0, "qty": 2, "touched": True}, {"dir": "long", "entry": 12010.0}),
     NF.looks_ours({"dir": "long", "entry": 12010.0, "qty": 2}, {"dir": "long", "entry": 12010.0})), (False, True))
# 他加碼之後，我們剩下那口又被停利吃掉 ⇒ 我們的全出場 ⇒ 交接成他的手動部位
api = MultiAPI()
r = af_send(api, 3)
api.fill_target(2, 12070.0)
broker._LAST_RECONCILE["at"] = 0.0
broker.reconcile()
api.lots.append(12040.0)
broker._LAST_RECONCILE["at"] = 0.0
broker.reconcile()
api.fill_target(1, 12071.0)
broker._LAST_RECONCILE["at"] = 0.0
broker.reconcile()
p = broker._state["position"]
_tr = trades_file()[-1]
chk("  我們的口數都出場了 ⇒ 成績單整趟 3 口、剩下的改成他的手動部位（1 口）",
    (_tr["qty"], _tr["reason"], (p or {}).get("qty"), (p or {}).get("sl_src")), (3, "legs", 1, "manual"))
live(False)

print("  -- B3：平倉後券商變成反向部位 ⇒ 停手 --")


class FlipAPI(MultiAPI):
    def place_order(self, contract, order):
        t = MultiAPI.place_order(self, contract, order)
        if str(order.octype).endswith("Cover") and str(order.price_type).endswith("MKP"):
            self.side = "Sell"                      # 停利單殘單同時成交 ⇒ 反向 1 口
            self.lots = [12070.0]
        return t


api = FlipAPI()
r = af_send(api, 3)
ok, err = broker.close("sl")
say(not ok and "反向" in str(err) and qtys(api, "close") == [3]
    and any(o.get("kind") == "close_flipped" for o in order_log()),
    "  ⛔ 反向 ⇒ 回失敗、只送過一張、委託 log 記 close_flipped", (ok, err, qtys(api, "close")))
live(False)

print("  -- B4：我們自己的平倉單在路上時，對帳看到少了幾口 ⇒ ⛔ 不記成 leg（那是我們在平）--")
api = MultiAPI()
r = af_send(api, 3)
api.lots.pop(0)                                    # 平倉單剛吃掉 1 口
broker._CLOSE_INFLIGHT["on"] = True
broker._LAST_RECONCILE["at"] = 0.0
broker.reconcile()
chk("  平倉中：部位 2 口、legs 沒有多一段", (broker._state["position"]["qty"], broker._state["position"].get("legs")),
    (2, None))
broker._CLOSE_INFLIGHT["on"] = False
live(False)

print("  -- 必修 7：_partial_leg 拿著 broker._lock 時 ⛔ 不問券商 --")


class CountAPI(MultiAPI):
    def __init__(self, *a, **k):
        MultiAPI.__init__(self, *a, **k)
        self.us_locked = 0

    def update_status(self, acc=None):
        if broker._lock.locked():
            self.us_locked += 1


api = CountAPI()
r = af_send(api, 3)
api.fill_target(1, 12070.0)
broker._LAST_RECONCILE["at"] = 0.0
broker.reconcile()
p = broker._state["position"]
chk("  停利部分成交那次對帳：leg 價錢照樣算得出來（tp），update_status 在鎖裡被叫 0 次",
    ([(l["qty"], l["px"], l["src"]) for l in p.get("legs") or []], api.us_locked), ([(1, 12070.0, "tp")], 0))
live(False)

print("  -- A1：口數模組壞掉 ⇒ 一律 1 口（日盤、夜盤）--")
_orig_dl = lots.day_lots
lots.day_lots = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("壞了"))
try:
    api = MultiAPI()
    r = af_send(api, 4)
finally:
    lots.day_lots = _orig_dl
chk("  日盤：lots.day_lots 丟例外（第 4 級）⇒ 送 1 口、帳本 1 口", (qtys(api, "new"), r.get("qty")), ([1], 1))
live(False)

print("  -- 必修 1：夜盤 1＋1 ⛔ 不讀振幅檔、送單不變慢；b ≥ 2 只用快取，沒有就照 b 口、不等 --")
E_N = date(2026, 6, 10)
NF.ARM_FLAG.write_text("R", encoding="ascii")
NF.TREND_CTX.write_text(json.dumps({"sig": {str(E_N - timedelta(days=i + 1)): (100.0 if i % 2 else -50.0)
                                            for i in range(40)}}), encoding="utf-8")
NF.configure(quote_fn=lambda: (12000.0, 1.0), session_fn=lambda now: "night",
             minute_close_fn=lambda m: {21 * 60 + 9: 11800.0}.get(m))
_NA = {"n": 0}
_orig_na = lots.night_amps


def _slow_amps(path=None):
    _NA["n"] += 1
    time.sleep(1.5)                                 # 冷快取讀 40 MB 的樣子（QA 實測 1.6 秒）
    return _orig_na(path)


_CALL = {}
_orig_enter, _orig_ce = broker.enter, broker.can_enter


def _fake_enter(d, px, tp, sl_points=None, qty=None):
    _CALL.update(t=time.perf_counter(), qty=qty)
    return False, "測試假的", None


def nf_decide(lvl, amps=None):
    shutil.rmtree(NF.NF_DIR, ignore_errors=True)
    clear_state()
    if lvl is not None:
        write_state(level=lvl)
    lots._AMP.update(key=("x" if amps is not None else None), rows=amps, err=None)
    NF._ST["tried"] = None
    _CALL.clear()
    _NA["n"] = 0
    t0 = time.perf_counter()
    NF._decide(E_N, NF.trend_at(E_N) + timedelta(seconds=1))
    return (_CALL.get("t", t0) - t0), _CALL.get("qty")


lots.night_amps = _slow_amps
broker.enter, broker.can_enter = _fake_enter, (lambda *a, **k: (True, None))
try:
    dt1, q1 = nf_decide(None)
    chk("  1＋1：送 1 口、⛔ 振幅檔讀了 0 次", (q1, _NA["n"]), (1, 0))
    say(dt1 < 0.3, "  1＋1：判斷到送單 %.3f 秒（跟改版前同一個量級；QA 量到舊版 1.6 秒）" % dt1)
    _row = [o for o in NF.rows_of(E_N) if o.get("rec") == "result"][-1]
    chk("  1＋1 帳本：qty 1、vol 不算（None）", (_row.get("qty"), _row.get("vol")), (1, None))
    dt2, q2 = nf_decide(4)
    chk("  ⭐ 4＋2、快取還沒有（不知道）⇒ ⛔ 不等、規則 B 照「不偏高」送 1 口、振幅檔讀了 0 次", (q2, _NA["n"]), (1, 0))
    say(dt2 < 0.3, "  4＋2 冷快取：判斷到送單 %.3f 秒" % dt2)
    _lo = [("2026-05-%02d" % (i + 1), 0.5) for i in range(25)] + [("2026-06-09", 0.5)]
    _hi = [("2026-05-%02d" % (i + 1), 2.0) for i in range(25)] + [("2026-06-09", 2.0)]
    dt3, q3 = nf_decide(4, _lo)
    chk("  ⭐ N1：4＋2、快取說不偏高 ⇒ 規則 B 送 1 口（⛔ 不是 b＝2）", (q3, _NA["n"]), (1, 0))
    dt4, q4 = nf_decide(4, _hi)
    chk("  4＋2、快取說偏高 ⇒ 2 口", q4, 2)
    _orig_rs2 = lots.read_state
    lots.read_state = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("壞了"))
    try:
        dt5, q5 = nf_decide(4, _hi)
    finally:
        lots.read_state = _orig_rs2
    chk("  ⭐ A1：夜盤口數模組壞掉 ⇒ 1 口", q5, 1)
finally:
    lots.night_amps = _orig_na
    broker.enter, broker.can_enter = _orig_enter, _orig_ce
    lots._AMP.update(key=None, rows=None, err=None)
print("  -- 必修 1：預熱 —— 開關晚開也會補暖；在背景讀、⛔ 不擋夜盤那條執行緒；1＋1 不暖 --")
_W = {"n": 0}
_orig_wn = lots.warm_night
lots.warm_night = lambda E=None: _W.__setitem__("n", _W["n"] + 1)
try:
    _wt = datetime.combine(E_N, datetime.min.time()) + timedelta(hours=21, minutes=25)
    clear_state()
    NF._DONE.update(warm=None, lwarm=None, decide=E_N, eod=None)
    NF.ARM_FLAG.unlink()
    NF.step(_wt)
    chk("  21:25 開關關著 ⇒ 不暖", _W["n"], 0)
    write_state(level=4)
    NF.ARM_FLAG.write_text("R", encoding="ascii")
    NF.step(_wt + timedelta(minutes=10))
    chk("  ⭐ 21:35 才打開（4＋2）⇒ 補暖一次", _W["n"], 1)
    NF.step(_wt + timedelta(minutes=11))
    chk("  同一晚不再暖第二次", _W["n"], 1)
    clear_state()
    NF._DONE.update(warm=None, lwarm=None)
    NF.step(_wt)
    chk("  1＋1 ⇒ ⛔ 不暖（1 口不看振幅）", _W["n"], 1)
finally:
    lots.warm_night = _orig_wn
    NF._DONE.update(warm=None, lwarm=None, decide=None, eod=None)
lots._AMP.update(key=None, rows=None, err=None)
_slow_path = TMP / "__slow_amps.csv"
_orig_na2 = lots.night_amps
lots.night_amps = lambda path=None: (time.sleep(1.0), ([], None))[1]
try:
    _t0 = time.perf_counter()
    _started = lots.warm_night(E_N)
    _dt = time.perf_counter() - _t0
    say(_started and _dt < 0.2, "  warm_night 在背景執行緒讀（呼叫端 %.3f 秒就回）" % _dt)
    say(not lots.warm_night(E_N), "  還在讀 ⇒ 不再開第二條")
    lots._WARM["th"].join(3)
finally:
    lots.night_amps = _orig_na2
NF.ARM_FLAG.unlink()

print("  -- F1／F2：前端兩段式與一口的部位卡（原始碼量尺）--")
_idle = _conf[_conf.index("return '<div class=\"lt-act\"><div class=\"row\">"):]
say('data-upon="1"' in _idle and "data-upyes" not in _idle and "pfetch" not in _idle,
    "  ⭐ F1：還沒按之前只有「data-upon」那一顆（⛔ 沒有 data-upyes、⛔ 沒有請求）", _idle[:90])
_cfm = _conf[_conf.index("if(LTUP.step==='confirm'){"):_conf.index("return '<div class=\"lt-act\"><div class=\"row\">")]
say("data-upyes" in _cfm and "data-upon" not in _cfm, "  確認條那一段才有「data-upyes」")
chk("  ⭐ F2：一口的部位卡不是多口版面（multi 只在 qty > 1）", _pos.count("multi=(nq!=null&&nq>1);"), 1)
say("O.power" in _conf and "lt-pw" in _conf, "  確認條會畫停電那一行（目標 ≥ 5 口時後端才給）")
say("TR.line" in _js and "lt-trial" in _js, "  口數卡會畫試車進度（句子＝後端 L.trial.line）")

print("  -- P2：【自動下單】本月卡的風控上限跟著口數 --")
write_state(level=2)
chk("  第 2 級（2＋1）⇒ risk_view 上限 1,200（⛔ 不是永遠 800）", LP.risk_view(force=True).get("cap"), 1200.0)
clear_state()
chk("  沒有狀態檔 ⇒ 800", LP.risk_view(force=True).get("cap"), 800.0)

print("  -- 建議：equity_cushion 問 lots（1＋1 一個字都不差）--")
_c1 = LP.equity_cushion(90000.0, 35050.0, px=23000.0, day_on=True, night_on=True)
write_state(level=4)
_c4 = LP.equity_cushion(250000.0, 35050.0, px=23000.0, day_on=True, night_on=True)
clear_state()
chk("  1＋1：跟改版前同一句", _c1["msg"], "權益比一口保證金多 54,950 元（約 %d 點），撐得住一次停損（約 460 點）"
    % int(54950 // risk_cap.PT_NTD))
say(_c4.get("lots") == 4 and "4 口保證金" in _c4["msg"] and _c4["stop_pts"] == 920,
    "  4＋2：保證金 × 4、一次停損 ＝ max(夜盤 460×2, 日盤 115×4) ＝ 920 點", _c4["msg"])

print("  -- 建議：最近 30 筆 ⛔ 不算演練的單 --")
shutil.rmtree(AF.FIRE_DIR, ignore_errors=True)
shutil.rmtree(NF.NF_DIR, ignore_errors=True)
led_day([day_res("2026-10-05", slip=3.0), day_res("2026-10-06", slip=9.0, live_=False),
         day_res("2026-10-07", ok=False, why="order_failed", live_=False)])
q = lots.exec_quality()
chk("  演練的那筆（滑價 9）與演練的下單錯誤都不算 ⇒ 1 筆、滑價 3、沒有錯誤", (q["n"], q["slip_avg"], q["errors"]), (1, 3.0, []))
shutil.rmtree(AF.FIRE_DIR, ignore_errors=True)

print("  -- 建議：減級那一句白話、照規格 --")
_sd, _evd = lots.downgrade(dict(json.loads(json.dumps(lots.DEFAULT)), level=2), 83100.0, today=date(2026, 11, 17))
chk("  「權益 8.31 萬，低於這一級的保護線 8.5 萬 ⇒ 明天起退回 1＋1。」", _evd[0]["msg"],
    "權益 8.31 萬，低於這一級的保護線 8.5 萬 ⇒ 明天起退回 1＋1。")
broker.CLOSE_COOLDOWN = _save_cd
broker.MAX_ENTRIES = _save_max

print("\n=== ⑰ QA r2 退件（他自己加碼過：夜盤交接、提醒、重啟補回）===")
broker.CLOSE_COOLDOWN = 0.0
broker.MAX_ENTRIES = 99
EN = date.today()                                  # ⚠️ 成績單寫在「今天」那個檔 ⇒ 夜盤那一晚就用今天
NF.ARM_FLAG.write_text("R", encoding="ascii")
NF.TREND_CTX.write_text(json.dumps({"sig": {str(EN - timedelta(days=i + 1)): (100.0 if i % 2 else -50.0)
                                            for i in range(40)}}), encoding="utf-8")
NF.configure(quote_fn=lambda: (12000.0, 1.0), session_fn=lambda now: "night",
             minute_close_fn=lambda m: {21 * 60 + 9: 11800.0}.get(m))
_EOD_AT = datetime.combine(EN + timedelta(days=1), datetime.min.time()).replace(hour=4, minute=58, second=1)


def nf_open(api, lvl=6):
    """夜盤真的走 broker.enter（假券商）開出多口。"""
    reset_dirs()
    clear_state()
    write_state(level=lvl)
    lots._AMP.update(key=None, rows=None, err=None)
    live(True)
    connect(api)
    NF._ST["tried"] = None
    NF._ST["eod_try_at"] = 0.0
    NF._MEM.update(E=None, entry=None)
    NF._decide(EN, NF.trend_at(EN) + timedelta(seconds=1))
    return [o for o in NF.rows_of(EN) if o.get("rec") == "result"][-1]


def nf_eod_rows():
    return [(o.get("why")) for o in NF.rows_of(EN) if o.get("rec") == "eod"]


print("  -- 必修 1：夜盤他加碼 → 停損平我們的 → 剩他的 ⇒ 04:58 ⛔ 不平（QA N1：舊版送了 [1]）--")
api = MultiAPI(px=12001.0, exit_px=11700.0)
ent = nf_open(api)
chk("  前置：6＋3、振幅不知道 ⇒ 規則 B 送 2 口", (qtys(api, "new"), ent.get("qty")), ([2], 2))
api.lots.append(12003.0)                            # 他自己加 1 口（價錢只差 2 點）
broker._LAST_RECONCILE["at"] = 0.0
broker.reconcile()
ok, err = broker.close("sl")
p = broker._state["position"]
chk("  停損只平我們的 2 口；剩他 1 口交接成手動部位（handover）",
    (qtys(api, "close"), list(api.lots), p.get("handover"), p.get("sl_src")), ([2], [12003.0], True, "manual"))
chk("  ⭐ 04:58 那一把尺：交接出去的 ⇒ ⛔ 不是我們的（價錢只差 2 點也一樣）", NF.looks_ours(p, ent), False)
chk("  日盤收盤那一把尺也一樣", AF._looks_ours(dict(p), dict(ent, entry_time=None))[0], False)
NF._eod(EN, _EOD_AT)
chk("  ⭐ 04:58：0 張平倉單、他那口還在", (qtys(api, "close"), list(api.lots), nf_eod_rows()[-1:]),
    ([2], [12003.0], ["done_elsewhere"]))
live(False)
print("  -- 必修 1：夜盤也補「成績單已經有這一趟 ⇒ 不平」（一口那條也受保護）--")
api = MultiAPI(px=12001.0, exit_px=11700.0)
ent = nf_open(api, lvl=None)                        # 1＋1：夜盤 1 口
ok, err = broker.close("sl")                        # 停損出場（成績單記一趟）
api.lots.append(12002.0)                            # 他自己又開 1 口、價錢跟我們差不多
broker._LAST_RECONCILE["at"] = 0.0
broker.reconcile()
NF._eod(EN, _EOD_AT)
chk("  ⭐ 今晚那一筆已經出場 ⇒ 04:58 ⛔ 不碰他後來開的那口（done_elsewhere、0 張新平倉單）",
    (nf_eod_rows()[-1:], qtys(api, "close"), len(api.lots)), (["done_elsewhere"], [1], 1))
live(False)
print("  -- 必修 4：他動過之後面板重啟 ⇒ 從落地的狀態補回、停損只平我們的（QA D3：舊版 [1,1,1]）--")
api = MultiAPI()
r = af_send(api, 3)
say((broker.ORDER_DIR / broker.META_NAME).exists(), "  多口部位一成立就落地（%s）" % broker.META_NAME)
api.lots.append(12040.0)
broker._LAST_RECONCILE["at"] = 0.0
broker.reconcile()
broker._state["position"] = None                    # 看門狗重啟：記憶體清空
AF._MEM.update({"date": None, "entry": None, "state": None})
broker._LAST_RECONCILE["at"] = 0.0
broker.reconcile()
AF._recover_poll()
p = broker._state["position"]
chk("  撿回來：他動過、我們剩 3 口、進場價是我們的成交價、停損點數補回",
    (p.get("touched"), broker._ours_left(p), p.get("entry"), p.get("sl_points"), p.get("sl_src")),
    (True, 3, 12010.0, 60.0, "meta"))
AF._eod(DAY, 0, time.time(), "13:43:30")
chk("  收盤照樣不平（他動過）", ([o.get("why") for o in AF._rows_of(DAY) if o.get("rec") == "eod"], qtys(api, "close")),
    (["eod_not_ours"], []))
ok, err = broker.close("sl")
chk("  ⭐ 停損：只平我們的 3 口（一張）、他那口留著", (ok, qtys(api, "close"), len(api.lots)), (True, [3], 1))
p = broker._state["position"]
chk("  剩下的交接成他的手動部位（落地狀態也跟著改成交接）",
    (p.get("handover"), json.loads((broker.ORDER_DIR / broker.META_NAME).read_text(encoding="utf-8")).get("handover")),
    (True, True))
broker._state["position"] = None                    # 再重啟一次：交接狀態也要補得回來
broker._LAST_RECONCILE["at"] = 0.0
broker.reconcile()
p = broker._state["position"]
chk("  交接之後再重啟 ⇒ 撿回來還是「他的、交接過的」（收盤／04:58 不碰）", (p.get("handover"), p.get("sl_src")), (True, "manual"))
api.lots = []
broker._LAST_RECONCILE["at"] = 0.0
broker.reconcile()
say(not (broker.ORDER_DIR / broker.META_NAME).exists(), "  部位沒了 ⇒ 落地的狀態清掉")
live(False)
chk("  日盤那一把尺（同價、口數也對得上）：交接出去的 ⇒ ⛔ 不是我們的；沒交接的對照組 ⇒ 是",
    (AF._looks_ours({"dir": "long", "entry": 12010.0, "qty": 1, "handover": True},
                    {"dir": "long", "entry": 12010.0, "qty": 3, "qty_sent": 3})[0],
     AF._looks_ours({"dir": "long", "entry": 12010.0, "qty": 1},
                    {"dir": "long", "entry": 12010.0, "qty": 3, "qty_sent": 3})[0]), (False, True))
# 落地的檔太舊（三天前、上次當掉沒清）⇒ ⛔ 不拿來補
api = MultiAPI()
r = af_send(api, 3)
api.lots.append(12040.0)
broker._LAST_RECONCILE["at"] = 0.0
broker.reconcile()
_mp = broker.ORDER_DIR / broker.META_NAME
_mo = json.loads(_mp.read_text(encoding="utf-8"))
_mp.write_text(json.dumps(dict(_mo, day=str(date.today() - timedelta(days=3)))), encoding="utf-8")
broker._state["position"] = None
broker._LAST_RECONCILE["at"] = 0.0
broker.reconcile()
p = broker._state["position"]
chk("  落地的檔是三天前的 ⇒ ⛔ 不補（不會被標成「他動過」、也不會用舊的口數）", (p.get("touched"), p.get("sl_src")), (None, None))
api.lots = []
broker._LAST_RECONCILE["at"] = 0.0
broker.reconcile()
live(False)
print("  -- 必修 4：補不回來（落地的檔不見）⇒ ⛔ 不准退到一口的迴圈去平 ⇒ 不平、示警 --")
api = MultiAPI()
r = af_send(api, 3)
api.lots.append(12040.0)
broker._LAST_RECONCILE["at"] = 0.0
broker.reconcile()
broker._state["position"] = None
(broker.ORDER_DIR / broker.META_NAME).unlink()
broker._META.update(last=None)
AF._MEM.update({"date": None, "entry": None, "state": None})
broker._LAST_RECONCILE["at"] = 0.0
broker.reconcile()
AF._recover_poll()
p = broker._state["position"]
say(p.get("multi_unknown") and "分不出" in str(p.get("sl_warn")), "  撿回來對不上今天的多口 ⇒ 標「分不出是誰的」、講出來", p.get("sl_warn"))
ok, err = broker.close("sl")
chk("  ⭐ 停損 ⇒ ⛔ 一張都不送（舊版 [1,1,1]）、回失敗＋原因", (ok, qtys(api, "close"), len(api.lots)), (False, [], 4))
say("分不出" in str(err), "  回的那句話", err)
AF._eod(DAY, 0, time.time(), "13:43:30")
chk("  收盤也不平", ([o.get("why") for o in AF._rows_of(DAY) if o.get("rec") == "eod"][-1:], qtys(api, "close")),
    (["eod_not_ours"], []))
chk("  04:58 那一把尺也不認", NF.looks_ours(p, {"dir": "long", "entry": p.get("entry")}), False)
live(False)
print("  -- 一口的部位：⛔ 不落地、snapshot 沒有新欄位（畫面跟以前一樣）--")
api = MultiAPI()
r = af_send(api, None)
_sn = broker.snapshot()["position"]
say(not (broker.ORDER_DIR / broker.META_NAME).exists() and not any(k in _sn for k in ("lot_note", "no_auto_exit", "own_left")),
    "  1＋1 的部位：沒有落地檔、沒有 lot_note／no_auto_exit／own_left", sorted(_sn))
live(False)
print("  -- 必修 2：他動過那一刻起就有提醒（部位卡、頂部金條、分頁小金點、手機）--")
api = MultiAPI()
r = af_send(api, 3)
api.lots.append(12040.0)
broker._LAST_RECONCILE["at"] = 0.0
broker.reconcile()
_sn = broker.snapshot()["position"]
say(_sn.get("no_auto_exit") and _sn.get("own_left") == 3 and "自己加碼過" in _sn.get("lot_note", "")
    and "停損只會平程式開的 3 口" in _sn["lot_note"], "  部位卡資料：不會自動平、我們剩 3 口、一句實話", _sn.get("lot_note"))
_PN = LP.lots_panel_now()
say(any(a.get("kind") == "pos_touched" and "自己加碼過" in a.get("text", "") for a in _PN.get("alarm") or [])
    and _PN.get("dot") and "自己加碼過" in str((_PN.get("phone") or {}).get("warn")) and (_PN.get("phone") or {}).get("att"),
    "  ⭐ 【自動下單】頂部金條＋小金點＋手機「要注意的事」都有那一句", (_PN.get("phone") or {}).get("warn"))
ok, err = broker.close("sl")
_sn = broker.snapshot()["position"]
say(_sn.get("lot_note_kind") == "handover" and "130 點停損" in _sn.get("lot_note", "") and _sn.get("no_auto_exit"),
    "  ⭐ 交接之後：「剩下 1 口是你的，面板照手動單的規矩用 130 點停損」", _sn.get("lot_note"))
api.lots = []
broker._LAST_RECONCILE["at"] = 0.0
broker.reconcile()
live(False)
_cardjs = _js[_js.index("function alPosHTML(D,row){"):_js.index("function alPts(")]
say("P.no_auto_exit?'<span><b>收盤／04:58 不會自動平</b></span>'" in _cardjs and "P.lot_note?" in _cardjs
    and "multi&&!P.lot_note_kind?" in _cardjs and "停損只平程式開的" in _cardjs,
    "  部位卡：他動過／交接時不寫「沒平掉就 XX 自動平」「N 口一起」，改寫實話（原始碼量尺）")
print("  -- 順手：成交口數一度對不上 ⇒ 畫面／手機也看得到 --")
api = LagAPI(lag=4, part=1)
r = af_send(api, 3)
_PN = LP.lots_panel_now()
say("成交口數一度對不上" in str((_PN.get("phone") or {}).get("warn")) and "成交口數一度對不上" in broker.snapshot()["position"].get("lot_note", ""),
    "  M1 那種情況：部位卡與手機都有「成交口數一度對不上，已照券商口數處理」", (_PN.get("phone") or {}).get("warn"))
api.lots = []
api.pending = 0
broker._LAST_RECONCILE["at"] = 0.0
broker.reconcile()
live(False)
print("  -- 順手：手上有部位 ⇒ 06:00 那道守衛先不跑（不跟停損迴圈搶 CPU）--")
clear_state()
write_state(level=1, checked="2026-11-13",
            pending={"to": 2, "from": 1, "eff": "2026-11-16", "kind": "trial", "at": "x", "id": "2026-11-13:1>2"})
_r = lots.daily_check([{"date": "2026-11-13", "equity": 105000.0}], now=datetime(2026, 11, 14, 7, 0),
                      ctx=ctx_ok(retire={"union": "watch", "trend": "ok"}), busy=True)
chk("  有部位 ⇒ 跳過（pending 還在）", (_r["events"], (lots.read_state()[0]["pending"] or {}).get("to")), ([], 2))
_r = lots.daily_check([{"date": "2026-11-13", "equity": 105000.0}], now=datetime(2026, 11, 14, 7, 10),
                      ctx=ctx_ok(retire={"union": "watch", "trend": "ok"}), busy=False)
chk("  沒部位了 ⇒ 照跑（策略掉了 ⇒ 作廢）", [e["kind"] for e in _r["events"]], ["pending_void"])
_lpsrc = pathlib.Path(LP.__file__).read_text(encoding="utf-8")
say('busy=broker._state.get("position") is not None' in _lpsrc, "  面板呼叫時帶 busy（有部位）")
print("  -- QA r2 T9：試車沒過就想跳級 ⇒ apply 拒絕、講得出原因 --")
write_state(level=2, checked="2026-11-13", trial={"status": "on", "start": "2026-11-10", "n": 2},
            suggest={"ok": True, "id": "2026-11-13:2>4", "kind": "jump", "from": 2, "to": 4, "lots": "4＋2", "why": [],
                     "made": "2026-11-13"})
ok, msg = lots.apply_upgrade("2026-11-13:2>4", now=datetime(2026, 11, 13, 15, 0),
                             rows=[{"date": "2026-11-13", "equity": 212000.0}], ctx=ctx_ok())
say(not ok and "試車還沒通過" in str(msg) and lots.read_state()[0]["pending"] is None, "  拒絕：試車還沒通過", msg)
print("  -- 順手：手機在試車期間標籤寫「2＋1（試車）」--")
write_state(level=2, checked="2026-11-13", trial={"status": "on", "start": "2026-11-10", "n": 2})
chk("  手機標籤", lots.panel(now=datetime(2026, 11, 14, 9, 0), eq=106000.0)["phone"]["label"], "2＋1（試車）")
clear_state()

print("\n=== ⑱ QA r3 退件（重啟補回：寧可認不出來，也不可以認錯人）===")


def restart_all(keep_meta=True):
    """模擬看門狗重啟：記憶體全清，再走一次「撿回部位」那條路（主迴圈掛勾＋兩支的工作執行緒補點數）。"""
    if not keep_meta:
        (broker.ORDER_DIR / broker.META_NAME).unlink(missing_ok=True)
    broker._state["position"] = None
    broker._META.update(last=None)
    AF._MEM.update({"date": None, "entry": None, "state": None})
    NF._MEM.update(E=None, entry=None)
    broker._LAST_RECONCILE["at"] = 0.0
    broker.reconcile()
    AF._recover_poll()


print("  -- 必修 1：停機期間有變化 ⇒ 有落地檔也⛔ 不補、標分不出、不平（QA R1／R3／R5：舊版送 [2]／[2]／[3]）--")
for keep in (True, False):
    tag = "有落地檔" if keep else "沒有落地檔"
    # R1：停機期間停利成交 2 口、他又加 1 口
    api = MultiAPI()
    r = af_send(api, 3)
    api.fill_target(2, 12070.0)
    api.lots.append(12040.0)
    restart_all(keep)
    p = broker._state["position"]
    AF._eod(DAY, 0, time.time(), "13:43:30")
    chk("  [%s] R1 停機時停利 2 口＋他加 1 口 ⇒ 分不出、收盤 0 張單、券商還是 2 口" % tag,
        (p.get("multi_unknown"), qtys(api, "close"), len(api.lots)), (True, [], 2))
    live(False)
    # R3：先他動過（落地記成 touched），停機期間停利成交 2 口
    api = MultiAPI()
    r = af_send(api, 3)
    api.lots.append(12040.0)
    broker._LAST_RECONCILE["at"] = 0.0
    broker.reconcile()
    api.fill_target(2, 12070.0)
    restart_all(keep)
    ok, err = broker.close("sl")
    chk("  [%s] R3 他動過後停機、停利成交 2 口 ⇒ 分不出、停損 0 張單" % tag,
        (broker._state["position"].get("multi_unknown"), ok, qtys(api, "close"), len(api.lots)), (True, False, [], 2))
    live(False)
    # R5：停機期間他把我們 3 口平掉、自己另開 3 口（均價不同）
    api = MultiAPI()
    r = af_send(api, 3)
    api.lots = [12030.0] * 3
    restart_all(keep)
    AF._eod(DAY, 0, time.time(), "13:43:30")
    ok, err = broker.close("sl")
    chk("  [%s] R5 他平掉我們的、另開 3 口 ⇒ 分不出、收盤與停損都 0 張單" % tag,
        (broker._state["position"].get("multi_unknown"), qtys(api, "close"), len(api.lots)), (True, [], 3))
    live(False)
# 停機期間「均價」沒變、「口數」變了 ／ 口數沒變、均價變了 —— 各一個（落地檔要真的有在比）
api = MultiAPI()
r = af_send(api, 3)
api.lots = [12010.0, 12010.0]                      # 停機期間他平掉一口（均價一樣）
restart_all(True)
chk("  口數 3 → 2（均價一樣、在區間內）⇒ 第六輪 G3：跟已實現對帳（沒有多出來的）⇒ 認回 2 口",
    (broker._state["position"].get("multi_unknown"), broker._ours_left(broker._state["position"])), (None, 2))
live(False)
api = MultiAPI()
r = af_send(api, 3)
api.lots = [12010.0, 12010.0, 12010.6]             # 口數一樣、均價動了 0.2（在帳本 ±1 的尺內，但落地比對要抓得到）
restart_all(True)
chk("  口數一樣、均價 12010 → 12010.2 ⇒ 分不出（落地比對看均價，⛔ 不是只看帳本 ±1）",
    broker._state["position"].get("multi_unknown"), True)
live(False)
# 對照組：停機期間什麼都沒變 ⇒ 照樣補回、照樣是我們的
api = MultiAPI(spread=2.0)
r = af_send(api, 3)
restart_all(True)
p = broker._state["position"]
chk("  對照組：停機期間沒變 ⇒ 補回（sl_src meta、我們 3 口、不是分不出）",
    (p.get("sl_src"), broker._ours_left(p), p.get("multi_unknown")), ("meta", 3, None))
AF._eod(DAY, 0, time.time(), "13:43:30")
chk("  對照組：收盤照平 3 口", (qtys(api, "close"), len(api.lots)), ([3], 0))
live(False)
# 後補成交：落地時成交口數沒定案（1 口），重啟時券商補到 3 口、委託單明細也說 3 口 ⇒ 是我們的
api = LagAPI(lag=4, part=1)
r = af_send(api, 3)
api.pending = 0
restart_all(True)
p = broker._state["position"]
chk("  後補成交：落地 1 口（沒定案）→ 重啟 3 口、在成交價區間內 ⇒ 補回、late_fill、我們 3 口",
    (p.get("sl_src"), p.get("late_fill"), broker._ours_left(p), p.get("multi_unknown")), ("meta", True, 3, None))
live(False)

print("  -- 必修 1：夜盤（QA R4：舊版 04:58 送 [2]）--")
api = MultiAPI(px=12001.0, exit_px=11700.0)
ent = nf_open(api)
api.lots = [12003.0] * len(api.lots)               # 停機期間他把我們 2 口平掉、自己開 2 口（只差 2 點，在夜盤 ±3 的尺內）
restart_all(True)
NF._MEM.update(E=EN, entry=NF._entry_of(NF.rows_of(EN)))
NF._recover_poll()
p = broker._state["position"]
NF._eod(EN, _EOD_AT)
chk("  ⭐ 有落地檔：均價 12001 → 12003 ⇒ 分不出、夜盤帳本⛔ 蓋不回來、04:58 0 張單",
    (p.get("multi_unknown"), p.get("unknown_src"), qtys(api, "close"), len(api.lots)), (True, "meta", [], 2))
live(False)

print("  -- 必修 2：真的是夜盤的 2 口 ⇒ 不管兩支誰先補，都要認得出來（QA r3_night）--")
for order in ("af_first", "nf_first"):
    reset_dirs()
    api = MultiAPI()
    af_send(api, 4)                                  # 今天日盤開了 4 口（帳本多口），收盤平掉
    AF._eod(DAY, 0, time.time(), "13:43:30")
    shutil.rmtree(TMP / "__keep_af", ignore_errors=True)
    shutil.copytree(AF.FIRE_DIR, TMP / "__keep_af")          # nf_open 會清資料夾 ⇒ 日盤帳本先收起來
    api = MultiAPI(px=12001.0, exit_px=11700.0)
    ent = nf_open(api)
    shutil.copytree(TMP / "__keep_af", AF.FIRE_DIR, dirs_exist_ok=True)
    say(AF._auto_entry(DAY)[0] is not None and (AF._auto_entry(DAY)[0] or {}).get("qty") == 4,
        "  [%s] 前置：今天日盤帳本是多口（4 口）那一筆" % order)
    (broker.ORDER_DIR / broker.META_NAME).unlink(missing_ok=True)   # 落地檔不見
    broker._state["position"] = None
    broker._META.update(last=None)
    AF._MEM.update({"date": None, "entry": None, "state": None})
    NF._MEM.update(E=None, entry=None)
    broker._LAST_RECONCILE["at"] = 0.0
    broker.reconcile()
    NF._MEM.update(E=EN, entry=NF._entry_of(NF.rows_of(EN)))
    if order == "af_first":
        AF._recover_poll()
        chk("  [af_first] 日盤那支先跑完的當下：⛔ 沒有把今晚夜盤這筆標成分不出（看到夜盤帳本開著那一筆）",
            (broker._state["position"].get("multi_unknown"), broker._state["position"].get("sl_src")), (None, None))
        NF._recover_poll()
    else:
        NF._recover_poll()
        AF._recover_poll()
    p = broker._state["position"]
    ok, err = broker.close("sl")
    chk("  [%s] 夜盤 2 口 ⇒ 認成夜盤那一筆（nightfire、不是分不出）、停損照平 2 口" % order,
        (p.get("sl_src"), p.get("multi_unknown"), qtys(api, "close"), len(api.lots)), ("nightfire", None, [2], 0))
    live(False)
# 兩道各自要成立（⛔ 不靠另一道兜）：日盤那支看得出「這是今晚夜盤那一筆」；夜盤那支蓋得回「帳本對不上」的分不出
_pn = {"dir": "long", "entry": 12001.0, "qty": 2, "recovered": True}
NF._MEM.update(E=EN, entry={"dir": "long", "entry": 12001.0, "qty": 2, "qty_sent": 2, "sl_points": 240.0,
                            "tp_points": None})
chk("  日盤那支：今晚夜盤帳本開著、對得上 ⇒ _night_claims 是 True（⇒ 不標分不出）；對不上（他的 3 口）⇒ False",
    (AF._night_claims(_pn), AF._night_claims(dict(_pn, qty=3))), (True, False))
_pl = dict(_pn, multi_unknown=True, unknown_src="ledger", sl_src="unmatched")
broker._state["position"] = _pl
NF._recover_poll()
chk("  夜盤那支：日盤標的「帳本對不上」分不出 ⇒ 對得上今晚帳本就蓋回（nightfire）",
    (_pl.get("multi_unknown"), _pl.get("sl_src")), (None, "nightfire"))
broker._state["position"] = None
# 落地檔被改過／別筆的：券商均價等於記下的均價，但不在我們的成交價區間 ⇒ ⛔ 不補
api = MultiAPI(spread=2.0)
r = af_send(api, 3)
_mp = broker.ORDER_DIR / broker.META_NAME
_mo = json.loads(_mp.read_text(encoding="utf-8"))
api.lots = [12030.0] * 3
_mp.write_text(json.dumps(dict(_mo, bpx=12030.0)), encoding="utf-8")
restart_all(True)
chk("  落地記的均價跟現在一樣（12030），但不在我們的成交價區間 12010～12014 ⇒ 分不出（第 ④ 道）",
    (broker._state["position"].get("multi_unknown"), broker._state["position"].get("unknown_src")), (True, "meta"))
live(False)
# 夜盤蓋回來只限「帳本對不上」那一種：落地比對出變化的（meta）⛔ 不蓋
_pu = {"dir": "long", "entry": 12001.0, "qty": 2, "recovered": True, "multi_unknown": True, "unknown_src": "meta",
       "sl_src": "unmatched"}
broker._state["position"] = _pu
NF._MEM.update(E=EN, entry={"dir": "long", "entry": 12001.0, "qty": 2, "qty_sent": 2, "sl_points": 240.0,
                            "tp_points": None})
NF._recover_poll()
chk("  落地比對出變化的「分不出」⇒ 夜盤帳本對得上也⛔ 不蓋回", (_pu.get("multi_unknown"), _pu.get("sl_src")), (True, "unmatched"))
broker._state["position"] = None

print("  -- 必修 3：分不出時畫面不畫停損、拒平只記一次（⛔ 每 15 秒刷紀錄；第六輪起放在 broker 拒平那條路）--")
api = MultiAPI()
r = af_send(api, 3)
api.lots = [12030.0] * 3
restart_all(True)
_sn = broker.snapshot()["position"]
chk("  snapshot：stop_off、一句實話", (_sn.get("stop_off"), "分不出" in str(_sn.get("lot_note"))), (True, True))
_LPS2 = pathlib.Path(LP.__file__).read_text(encoding="utf-8")
say('if pos.get("stop_off"):' in _LPS2 and 'snap["sl"] = snap["tp"] = snap["tp_pts"] = snap["sl_pts"] = None' in _LPS2,
    "  /api/state：分不出 ⇒ 不端停損價／停利價（畫面才不會畫刻度、寫離停損 N 點）")
say("P.stop_off?'<span><b style=\"color:var(--gold)\">停損不會執行，請自己處理</b></span>'" in _LPS2
    and "P.stop_off?'<div class=\"r\"><span>停損</span><b style=\"color:var(--gold)\">不會執行，請自己處理</b></div>'" in _LPS2,
    "  部位卡與【即時】卡：寫「停損不會執行，請自己處理」（原始碼量尺）")
_n0 = len([o for o in order_log() if o.get("kind") == "close_refused"])
_said = []
_orig_print = print


def _cap_print(*a, **k):
    _said.append(" ".join(str(x) for x in a))


import builtins  # noqa: E402
builtins.print = _cap_print
try:
    for _ in range(5):
        LP.check_real_position(11000.0, 0.1, "live")   # 價格遠遠穿過任何停損
finally:
    builtins.print = _orig_print
_n1 = len([o for o in order_log() if o.get("kind") == "close_refused"])
chk("  ⭐ 停損迴圈跑 5 圈（價格早就穿過停損）⇒ 0 張平倉單、close_refused 只記 1 列、「停損不會執行」只講 1 次",
    (qtys(api, "close"), _n1 - _n0, len([x for x in _said if "停損不會執行" in x])), ([], 1, 1))
live(False)

print("\n=== ⑲ QA r4 退件（停機時停利部分成交、1 口不套落地檔、主迴圈一字不動）===")
print("  -- 必修 1（G3）：停機期間停利成交 1 口 ⇒ 剩下 2 口確定是我們的 ⇒ 認回來、收盤照平 --")
api = MultiAPI()
r = af_send(api, 3)
api.fill_target(1, 12070.0)                        # 面板關著時停利單吃掉 1 口
broker._state["position"] = None
broker._META.update(last=None)
AF._MEM.update({"date": None, "entry": None, "state": None})
NF._MEM.update(E=None, entry=None)
broker._LAST_RECONCILE["at"] = 0.0
broker.reconcile()
p = broker._state["position"]
chk("  撿回那一刻：先標「分不出（等對帳）」⇒ 對完之前停損、收盤都不平", (p.get("multi_unknown"), p.get("unknown_src")),
    (True, "shrink"))
chk("  等對帳期間 ⛔ 不覆蓋落地檔（再重啟一次還是照原本 3 口那份比）",
    json.loads((broker.ORDER_DIR / broker.META_NAME).read_text(encoding="utf-8")).get("qty"), 3)
_RQ = {"n": 0}
_orig_rt2 = broker.realized_today


def _rt_empty(fresh=False):
    _RQ["n"] += 1
    return []


broker.realized_today = _rt_empty
try:
    AF._recover_poll()                              # 工作執行緒：跟已實現損益對帳
finally:
    broker.realized_today = _orig_rt2
p = broker._state["position"]
chk("  ⭐ 已實現沒有多出來的 ⇒ 認回：我們剩 2 口、停機那段記成價錢留白的 leg、停損點數還在",
    (p.get("multi_unknown"), p.get("sl_src"), broker._ours_left(p), [(l["qty"], l["px"]) for l in p.get("legs") or []],
     p.get("sl_points"), _RQ["n"]), (None, "meta", 2, [(1, None)], 60.0, 1))
AF._eod(DAY, 0, time.time(), "13:43:30")
chk("  ⭐ 收盤平剩下的 2 口（一張）、券商 0 口", ([o.get("why") for o in AF._rows_of(DAY) if o.get("rec") == "eod"],
                                       qtys(api, "close"), len(api.lots)), (["eod_closed"], [2], 0))
chk("  成績單整趟 3 口（停機那一口價錢留白 ⇒ 整趟出場價留白，⛔ 不猜）", (trades_file()[-1]["qty"], trades_file()[-1]["exit"]),
    (3, None))
live(False)
print("  -- G3 反例：停機期間停利 2 口、他同價加 1 口（口數、均價都像我們的）⇒ 已實現說我們平了 2 口 ⇒ ⛔ 不認 --")
api = MultiAPI()
r = af_send(api, 3)
api.fill_target(2, 12070.0)
api.lots.append(12010.0)
broker._state["position"] = None
broker._META.update(last=None)
AF._MEM.update({"date": None, "entry": None, "state": None})
broker._LAST_RECONCILE["at"] = 0.0
broker.reconcile()
broker.realized_today = lambda fresh=False: [{"entry": 12010.0, "exit": 12070.0, "qty": 1, "dir": "long", "pnl": 600.0}] * 2
try:
    AF._recover_poll()
finally:
    broker.realized_today = _orig_rt2
p = broker._state["position"]
AF._eod(DAY, 0, time.time(), "13:43:30")
ok, err = broker.close("sl")
chk("  ⭐ 已實現 2 口 > 帳上應該只少 1 口 ⇒ 改成分不出（meta）、收盤與停損都 0 張單",
    (p.get("multi_unknown"), p.get("unknown_src"), qtys(api, "close"), len(api.lots)), (True, "meta", [], 2))
live(False)
print("  -- G3：已實現問不到 ⇒ 維持等對帳（⛔ 不放行）--")
api = MultiAPI()
r = af_send(api, 3)
api.fill_target(1, 12070.0)
broker._state["position"] = None
broker._META.update(last=None)
broker._LAST_RECONCILE["at"] = 0.0
broker.reconcile()
broker.realized_today = lambda fresh=False: None
try:
    _ro = broker.resolve_shrink()
finally:
    broker.realized_today = _orig_rt2
chk("  問不到 ⇒ 還是分不出（shrink）", (_ro, broker._state["position"].get("unknown_src")), (None, "shrink"))
broker._state["position"] = None
live(False)
print("  -- G3：均價跑出我們的成交價區間 ⇒ 直接分不出（⛔ 不必等對帳）--")
api = MultiAPI()
r = af_send(api, 3)
api.lots = [12030.0, 12030.0]
broker._state["position"] = None
broker._META.update(last=None)
broker._LAST_RECONCILE["at"] = 0.0
broker.reconcile()
chk("  口數變少但均價 12030 不在 12010±1 ⇒ 分不出（meta）", broker._state["position"].get("unknown_src"), "meta")
broker._state["position"] = None
live(False)
print("  -- 補強（G7）：撿回的是 1 口、落地檔不是交接 ⇒ 不套落地檔，走帳本認人 --")
api = MultiAPI()
r = af_send(api, None)                              # 1＋1：1 口
broker.ORDER_DIR.mkdir(exist_ok=True)
(broker.ORDER_DIR / broker.META_NAME).write_text(json.dumps(
    {"dir": "long", "entry_fill": 12013.0, "entry_qty": 3, "qty_sent": 3, "qty": 3, "bpx": 12013.0,
     "day": str(date.today())}), encoding="utf-8")   # 更早那一趟多口留下的舊檔（當掉沒清）
broker._state["position"] = None
broker._META.update(last=None)
AF._MEM.update({"date": None, "entry": None, "state": None})
broker._LAST_RECONCILE["at"] = 0.0
broker.reconcile()
AF._recover_poll()
p = broker._state["position"]
chk("  ⭐ 1 口的部位：⛔ 不被舊落地檔標成分不出，照帳本認成自動下單那一口（autofire）",
    (p.get("multi_unknown"), p.get("sl_src")), (None, "autofire"))
ok, err = broker.close("sl")
chk("  停損照平 1 口", (ok, qtys(api, "close")), (True, [1]))
live(False)
print("  -- QA r4 G5：成交口數沒定案時就重啟（落地 1 口、券商還在補）⇒ 帳本補回 3 口，券商補到 3 口 ⇒ ⛔ 不標他動過、收盤照平 --")
api = LagAPI(lag=99, part=1)
r = af_send(api, 3)
broker._state["position"] = None
broker._META.update(last=None)
AF._MEM.update({"date": None, "entry": None, "state": None})
broker._LAST_RECONCILE["at"] = 0.0
broker.reconcile()
AF._recover_poll()
p = broker._state["position"]
chk("  重啟時券商還只顯示 1 口 ⇒ 帳本（fill_unsure）補回我們 3 口", (p.get("qty"), p.get("entry_qty"), p.get("sl_src")),
    (1, 3, "autofire"))
api.pending = 0
AF._eod(DAY, 0, time.time(), "13:43:30")
chk("  ⭐ 券商補到 3 口 ⇒ 是我們的（⛔ 不是他動過）、收盤平 3 口、不留倉",
    ([o.get("why") for o in AF._rows_of(DAY) if o.get("rec") == "eod"], qtys(api, "close"), len(api.lots),
     broker._state["position"]), (["eod_closed"], [3], 0, None))
live(False)
print("  -- 必修 2：主迴圈那幾支跟改版前（git HEAD）AST 一模一樣（⛔ 動主迴圈要在回報第一段講）--")
_head_lp = subprocess.run(["git", "-C", str(HERE), "show", _BASE_REV + ":tools/shioaji/live_panel.py"], capture_output=True).stdout
if _head_lp:
    import ast as _ast


    def _fn_dump(src, name):
        for _n in _ast.walk(_ast.parse(src)):
            if isinstance(_n, _ast.FunctionDef) and _n.name == name:
                return _ast.dump(_n)
        return None
    _cur_src = pathlib.Path(LP.__file__).read_text(encoding="utf-8")
    _head_src = _head_lp.decode("utf-8")
    _diff = [f for f in ("_auto_tick", "_auto_tick_guarded", "check_real_position", "on_tick", "_auto_snap", "_auto_put")
             if _fn_dump(_cur_src, f) != _fn_dump(_head_src, f)]
    chk("  ⭐ _auto_tick／_auto_tick_guarded／check_real_position／on_tick／_auto_snap／_auto_put 跟 HEAD 相同", _diff, [])
else:
    say(False, "  ⛔ 拿不到 git HEAD 的 live_panel.py（這一段沒驗到 ⛔ 不當成通過）")
print("  -- 拒平只記一次（放在 broker 拒平那條路）--")
api = MultiAPI()
r = af_send(api, 3)
api.lots = [12030.0] * 3
restart_all(True)
_n0 = len([o for o in order_log() if o.get("kind") == "close_refused"])
for _ in range(4):
    broker._close_fail["at"] = 0.0
    broker.close("sl")
chk("  分不出的部位被按 4 次平倉 ⇒ 0 張單、close_refused 只記 1 列",
    (qtys(api, "close"), len([o for o in order_log() if o.get("kind") == "close_refused"]) - _n0), ([], 1))
live(False)

print("\n=== ⑳ QA r5 非阻擋三條（對帳退避、只算進場之後的列、夜盤跨午夜）===")
_RT0 = broker.realized_today
_RD0 = broker._realized_day
_RC = {"n": 0}


def real_stub(rows):
    def f(fresh=False):
        _RC["n"] += 1
        return None if rows is None else [dict(x) for x in rows]
    broker.realized_today = f


def shrink_restart():
    broker._state["position"] = None
    broker._META.update(last=None)
    AF._MEM.update({"date": None, "entry": None, "state": None})
    NF._MEM.update(E=None, entry=None)
    broker._LAST_RECONCILE["at"] = 0.0
    broker.reconcile()


try:
    print("  -- 1：等對帳時問不到 ⇒ 每 10 秒才再問；等超過 1 分鐘 ⇒ 改講「問不到 N 分鐘，停損不會執行」--")
    real_stub([])
    api = MultiAPI()
    r = af_send(api, 3)
    api.fill_target(1, 12070.0)
    shrink_restart()
    real_stub(None)
    _RC["n"] = 0
    _t = time.time()
    for _ in range(20):
        broker.resolve_shrink(now=_t + 0.5)
    chk("  ⭐ 問不到之後連叫 20 次（同一個 10 秒內）⇒ 只打券商 1 次", _RC["n"], 1)
    broker.resolve_shrink(now=_t + 11.0)
    chk("  過了 10 秒 ⇒ 再問 1 次", _RC["n"], 2)
    p = broker._state["position"]
    say("正在跟券商的已實現損益對帳" in str(broker.pos_note(p)[0]), "  1 分鐘以內：照舊「正在對帳」那一句", broker.pos_note(p)[0])
    p["shrink_pend"]["since"] = time.time() - 125
    _note = broker.pos_note(p)[0]
    chk("  ⭐ 等了 2 分多鐘 ⇒「券商已實現損益問不到 2 分鐘，停損不會執行，請自己處理」",
        str(_note).startswith("券商已實現損益問不到 2 分鐘，停損不會執行，請自己處理"), True)
    _sn = broker.snapshot()["position"]
    _PN = LP.lots_panel_now()
    say("問不到 2 分鐘" in str(_sn.get("lot_note")) and _sn.get("stop_off")
        and any("問不到 2 分鐘" in str(a.get("text")) for a in _PN.get("alarm") or [])
        and "問不到 2 分鐘" in str((_PN.get("phone") or {}).get("warn")),
        "  部位卡（不畫停損）、【自動下單】金條、手機都換成那一句", _sn.get("lot_note"))
    real_stub([{"dir": "long", "entry": 12010.0, "exit": 12070.0, "qty": 1, "id": 31}])
    broker.resolve_shrink(now=_t + 25.0)
    p = broker._state["position"]
    chk("  之後問到了 ⇒ 認回（我們剩 2 口）", (p.get("multi_unknown"), broker._ours_left(p)), (None, 2))
    live(False)

    print("  -- 2：只算我們進場之後的列（他當天稍早同方向、同價位的手動單不算）--")
    _manual = {"dir": "long", "entry": 12010.0, "exit": 12030.0, "qty": 1, "id": 7}
    real_stub([_manual])                                # 進場那一刻：已實現裡已經有他稍早的那一筆
    api = MultiAPI()
    r = af_send(api, 3)
    chk("  進場時記下當下已有的列（real_base）、也落地", (broker._state["position"].get("real_base"),
                                              json.loads((broker.ORDER_DIR / broker.META_NAME).read_text(encoding="utf-8")).get("real_base")),
        (["7"], ["7"]))
    api.fill_target(1, 12070.0)
    shrink_restart()
    real_stub([_manual, {"dir": "long", "entry": 12010.0, "exit": 12070.0, "qty": 1, "id": 9}])
    AF._recover_poll()
    p = broker._state["position"]
    chk("  ⭐ 他那筆（id 7，同價 12010）排除 ⇒ 我們平掉 1 口 ＝ 帳上的 1 口 ⇒ 認回、收盤照平",
        (p.get("multi_unknown"), broker._ours_left(p)), (None, 2))
    AF._eod(DAY, 0, time.time(), "13:43:30")
    chk("  收盤平 2 口", (qtys(api, "close"), len(api.lots)), ([2], 0))
    live(False)
    # 對照：進場那一刻沒問到（real_base 沒有）⇒ 不排除 ⇒ 2 口 > 1 口 ⇒ 分不出（保守）
    real_stub(None)
    api = MultiAPI()
    r = af_send(api, 3)
    api.fill_target(1, 12070.0)
    shrink_restart()
    real_stub([_manual, {"dir": "long", "entry": 12010.0, "exit": 12070.0, "qty": 1, "id": 9}])
    AF._recover_poll()
    chk("  對照：進場時沒問到 ⇒ 不排除任何列 ⇒ 分不出（寧可認不出來）", broker._state["position"].get("unknown_src"), "meta")
    broker._state["position"] = None
    live(False)
    # 價錢不一樣的手動單（沒有 id 也一樣）⇒ 不在我們成交價區間 ⇒ 不算（有記到各口成交價區間時 ⛔ 不再外擴 1 點）
    real_stub([])
    api = MultiAPI()
    r = af_send(api, 3)
    chk("  前置：這一筆進場時有記到各口成交價區間（fill_lo／fill_hi）",
        (broker._state["position"].get("fill_lo"), broker._state["position"].get("fill_hi")), (12010.0, 12010.0))
    api.fill_target(1, 12070.0)
    shrink_restart()
    real_stub([{"dir": "long", "entry": 12009.5, "exit": 12020.0, "qty": 1},
               {"dir": "long", "entry": 12010.0, "exit": 12070.0, "qty": 1}])
    AF._recover_poll()
    chk("  ⭐ QA S4：他稍早 12009.5 那筆（我們成交 12010）⇒ 不算我們的 ⇒ 認回", broker._state["position"].get("multi_unknown"), None)
    broker._state["position"] = None
    live(False)

    print("  -- QA r6 S5：進場時成交明細沒回報（沒有成交價區間）⇒ 對帳區間退回 ±1（⛔ 不用 ±0.01）--")


    class SpreadNoDeals(MultiAPI):
        """3 口成交在 12009／12010／12011，但委託單的成交明細讀不到（只有均價 12010）。"""

        def place_order(self, contract, order):
            t = MultiAPI.place_order(self, contract, order)
            if str(order.octype).endswith("New"):
                self.lots = [12009.0, 12010.0, 12011.0]
                t.status.deals = []
            return t


    real_stub([])
    broker.FILL_WAIT, _fw = 0.9, broker.FILL_WAIT
    try:
        api = SpreadNoDeals()
        r = af_send(api, 3)
    finally:
        broker.FILL_WAIT = _fw
    p = broker._state["position"]
    chk("  前置：沒有成交價區間、均價 12010", (p.get("fill_lo"), p.get("fill_hi"), p.get("entry_fill"), p.get("qty")),
        (None, None, 12010.0, 3))
    api.lots = [12011.0, 12011.0]                      # 停機時停利吃掉 12009、12010 兩口；他又在 12011 加 1 口
    shrink_restart()
    real_stub([{"dir": "long", "entry": 12009.0, "exit": 12070.0, "qty": 1, "id": 61},
               {"dir": "long", "entry": 12010.0, "exit": 12070.0, "qty": 1, "id": 62}])
    AF._recover_poll()
    p = broker._state["position"]
    AF._eod(DAY, 0, time.time(), "13:43:30")
    ok, err = broker.close("sl")
    chk("  ⭐ 我們平掉的 2 口（12009、12010）都算到 ⇒ 2 口 > 帳上 1 口 ⇒ 分不出、收盤與停損 0 張單、他那口不動",
        (p.get("unknown_src"), qtys(api, "close"), len(api.lots)), ("meta", [], 2))
    broker._state["position"] = None
    live(False)

    print("  -- 3：夜盤跨午夜（停機在午夜前、重啟在午夜後）⇒ 連進場那天的已實現一起看 --")
    _YD = str(date.today() - timedelta(days=1))
    for tag, prev_rows, want in (
            ("前一天平了 2 口（他又同價加 1 口）", [{"dir": "long", "entry": 12001.0, "exit": 11950.0, "qty": 1, "id": 51}] * 2, "meta"),
            ("前一天平了 1 口", [{"dir": "long", "entry": 12001.0, "exit": 11950.0, "qty": 1, "id": 51}], None)):
        real_stub([])
        api = MultiAPI(px=12001.0, exit_px=11700.0)
        ent = nf_open(api, lvl=9)                         # 9＋4，振幅不知道 ⇒ 3 口
        with broker._lock:
            broker._state["position"]["entry_day"] = _YD   # 進場在昨天晚上
        broker._meta_sync()
        api.lots = [12001.0, 12001.0]                     # 停機期間（午夜前）少了 1 口
        shrink_restart()
        real_stub([])                                     # 今天（午夜後）的已實現：空的
        _asked = []
        broker._realized_day = (lambda rows: (lambda d: (_asked.append(str(d)), [dict(x) for x in rows])[1]))(prev_rows)
        broker.resolve_shrink()
        p = broker._state["position"]
        chk("  ⭐ [%s] ⇒ 有去問 %s 的已實現、結果 %s" % (tag, _YD, "分不出" if want else "認回"),
            (_asked, p.get("unknown_src")), ([_YD], want))
        broker._realized_day = _RD0
        broker._state["position"] = None
        live(False)
    # 日盤（進場是今天）⇒ ⛔ 不問前一天
    real_stub([])
    api = MultiAPI()
    r = af_send(api, 3)
    api.fill_target(1, 12070.0)
    shrink_restart()
    _asked = []
    broker._realized_day = lambda d: (_asked.append(str(d)), [])[1]
    real_stub([{"dir": "long", "entry": 12010.0, "exit": 12070.0, "qty": 1, "id": 3}])
    broker.resolve_shrink()
    chk("  日盤（今天進場）⇒ 不問前一天", _asked, [])
    broker._realized_day = _RD0
    broker._state["position"] = None
    live(False)
finally:
    broker.realized_today = _RT0
    broker._realized_day = _RD0

# ══ 收尾：全程沒有指回真的資料夾 ═════════════════════════════════════════
print("\n=== 收尾：全程沒有寫到他真的資料夾 ===")
NOW = {"broker.ORDER_DIR": broker.ORDER_DIR, "broker.TRADE_DIR": broker.TRADE_DIR,
       "broker.REAL_FLAG": broker.REAL_FLAG, "lots.LEVEL_FILE": lots.LEVEL_FILE, "lots.LOG_DIR": lots.LOG_DIR,
       "AF.ARM_FLAG": AF.ARM_FLAG, "AF.FIRE_DIR": AF.FIRE_DIR, "AF.FAST_HIST": AF.FAST_HIST,
       "AF.ORB_HIST": AF.ORB_HIST, "AF.TICK_DIR": AF.TICK_DIR, "NF.ARM_FLAG": NF.ARM_FLAG,
       "NF.NF_DIR": NF.NF_DIR, "NF.TSM_CTX": NF.TSM_CTX, "NF.TREND_CTX": NF.TREND_CTX,
       "risk_cap.OVERRIDE_FLAG": risk_cap.OVERRIDE_FLAG, "LP.EQUITY_DIR": LP.EQUITY_DIR}
for k, real in REAL.items():
    say(NOW[k] != real and str(TMP) in str(NOW[k]), "  %s 全程在暫存區" % k, NOW[k])
chk("  ⛔ 真的 LOTS_LEVEL.json 存在與否跟開跑前一樣", REAL["lots.LEVEL_FILE"].exists(), REAL_LEVEL_EXISTED)
chk("  ⛔ 真的 lots/ 紀錄資料夾沒被建出來（開跑前不存在的話）",
    REAL["lots.LOG_DIR"].exists(), REAL_LOG_EXISTED)
shutil.rmtree(TMP, ignore_errors=True)
print("\n" + ("全部通過 ✅" if FAIL == 0 else "⛔ 有 %d 項沒過" % FAIL))
sys.exit(1 if FAIL else 0)
