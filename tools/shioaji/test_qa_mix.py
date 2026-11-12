# -*- coding: utf-8 -*-
"""
混搭（同一筆部位同時有小台 MXF＋微台 TMF）的離線測試（2026-11-06）。
⛔ 不連永豐、⛔ 不碰 8770、⛔ 不建／不刪真的 REAL_ORDERS_ON／MINI_ON／AUTO_ORDERS_ON／NIGHT_ORDERS_ON：
   所有開關檔都導到暫存資料夾；「真單模式」是把 broker.REAL_FLAG 指到暫存資料夾裡的檔，
   place_order 是假券商（只記錄、照劇本成交，不連網）。broker 的時鐘換成假時鐘（等成交不用真的等）。
每一項都斷言「送了哪些單（合約、方向、口數、新倉／平倉）」與最後部位。
跑法：.venv\\Scripts\\python.exe -X utf8 tools\\shioaji\\test_mini_mix.py
"""
import json
import pathlib
import random
import sys
import tempfile
import time as _rt
from datetime import date, datetime, timedelta

import os
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_PRE = {f: (HERE / f).exists() for f in ("REAL_ORDERS_ON", "MINI_ON", "AUTO_ORDERS_ON", "NIGHT_ORDERS_ON")}

import broker            # noqa: E402
import shioaji as sj     # noqa: E402

TMP = pathlib.Path(tempfile.mkdtemp(prefix="__tmp__mix-test-"))
broker.ORDER_DIR = TMP / "real_orders"
broker.TRADE_DIR = TMP / "real_trades"
broker.REAL_FLAG = TMP / "REAL_ORDERS_ON"
broker.MINI_FLAG = TMP / "MINI_ON"
broker.FILL_WAIT = 5.0                   # 跟正式一樣（假時鐘，不會真的等）
broker.MAX_ENTRIES = 99
broker.CA_OK["ok"] = True
FAIL = []
N_OK = [0]


def chk(name, got, want):
    ok = got == want
    print(("  OK   " if ok else "  FAIL ") + name + ("" if ok else "  (得到 %r，期待 %r)" % (got, want)))
    if ok:
        N_OK[0] += 1
    else:
        FAIL.append(name)


def say(cond, name, extra=None):
    chk(name + ("" if cond or extra is None else "  [%r]" % (extra,)), bool(cond), True)


class Clock:
    """假時鐘：broker 等成交時的 sleep 只推時間、不真的睡。"""
    def __init__(self):
        self.t = 1_900_000_000.0

    def time(self):
        return self.t

    def sleep(self, d):
        self.t += float(d)


CLK = Clock()
ACC = sj.Account(account_type=sj.AccountType.Future, person_id="TESTPID", broker_id="F000000",
                 account_id="0000000", signed=True, username="test")
broker.time = CLK


class Contract:
    def __init__(self, code):
        self.code = code


C = {"TMF": Contract("TMFK6"), "MXF": Contract("MXFK6")}
CODE2P = {"TMFK6": "TMF", "MXFK6": "MXF"}


class Deal:
    def __init__(self, p, q):
        self.price, self.quantity = p, q


class Trade:
    _n = [0]

    def __init__(self, c, o):
        Trade._n[0] += 1
        self.contract = c
        self.order = type("O", (), {"id": "T%d" % Trade._n[0], "quantity": o.quantity})()
        self.status = type("S", (), {"status": "OrderStatus.Submitted", "deals": []})()
        self.hidden = None                 # (deals, status, 還要幾次 update_status 才回報)


class Fake:
    """
    假券商。hold＝每一種合約的部位（合約口數，正＝多、負＝空）；px＝均價。
    policy(合約, 這張單) ⇒ (成交幾口合約, 成交價)；預設全部成交在 mkt。
    lag＝部位回報慢幾秒（假時鐘）：list_positions 回的是「lag 秒前」那一刻的部位。
    hide＝(機率, 幾次)：成交明細晚幾次 update_status 才出現（委託單狀態先是 Submitted）。
    """
    def __init__(self):
        self.hold = {"TMF": 0, "MXF": 0}
        self.px = {"TMF": 0.0, "MXF": 0.0}
        self.mkt = {"TMF": 38001.0, "MXF": 38000.0}
        self.sent, self.cancels, self.targets, self.pl = [], [], [], []
        self.policy = None
        self.lag = 0
        self._hist = []                    # [(時刻, 那一刻之後的部位列)]（回報慢用）
        self.pos_fail = 0
        self.cancel_err = set()
        self.bad = []                      # 違規紀錄（平過頭、送出的平倉口數 > 那一腿當下持有）
        self.hide = None
        self.rng = None
        self._pid = 0
        self.late = {}                     # {合約: 秒}：那一種合約的新倉成交，部位要晚幾秒才回報（進場腿晚到）
        self._late = {}                    # {合約: (到幾點才回報, 回報之前看到的部位口數, 均價)}
        self.no_list_trades = False

    # ── 券商 API ──
    def place_order(self, c, o):
        p = CODE2P[c.code]
        act = "Buy" if str(o.action).endswith("Buy") else "Sell"
        oc = "New" if str(o.octype).endswith("New") else "Cover"
        pt = str(o.price_type).split(".")[-1]
        ot = str(o.order_type).split(".")[-1]
        rec = {"p": p, "act": act, "q": o.quantity, "oc": oc, "pt": pt, "ot": ot, "price": o.price}
        self.sent.append(rec)
        t = Trade(c, o)
        if pt == "LMT":
            self.targets.append((p, t, act, o.price))
            return t
        if oc == "Cover":
            h = self.hold[p]
            closing = (h > 0 and act == "Sell") or (h < 0 and act == "Buy")
            if not closing or o.quantity > abs(h):
                self.bad.append(("平倉單口數超過持有", p, act, o.quantity, h))
        k, px = self.policy(p, rec) if self.policy else (o.quantity, self.mkt[p])
        k = max(0, min(int(k), o.quantity))
        self._apply(p, (k if act == "Buy" else -k), px)
        deals = [Deal(px, k)] if k else []
        # 2026-11-06 模擬帳戶實測（probe_ioc_status.py）：IOC 有成交（含部分成交）⇒ OrderStatus.Filled；0 成交 ⇒ Cancelled
        st = "OrderStatus.Filled" if k > 0 else "OrderStatus.Cancelled"
        if self.hide and self.rng.random() < self.hide[0]:
            t.hidden = (deals, st, self.hide[1])
        else:
            t.status.deals, t.status.status = deals, st
        return t

    def fill_target(self, p, k, px=None):
        for (tp, t, act, price) in self.targets:
            if tp == p and t.status.status in ("OrderStatus.Submitted", "OrderStatus.PartFilled"):
                px = float(price) if px is None else px
                self._apply(p, (k if act == "Buy" else -k), px)
                t.status.deals.append(Deal(px, k))
                if sum(d.quantity for d in t.status.deals) >= t.order.quantity:
                    t.status.status = "OrderStatus.Filled"
                else:
                    t.status.status = "OrderStatus.PartFilled"   # 限價 ROD 部分成交（還掛著）
                return t
        raise AssertionError("找不到 %s 的停利單" % p)

    def update_status(self, acc=None):
        for t in list(Trade._live):
            if t.hidden is not None:
                d, st, n = t.hidden
                if n <= 1:
                    t.status.deals, t.status.status, t.hidden = d, st, None
                else:
                    t.hidden = (d, st, n - 1)

    def cancel_order(self, t):
        p = CODE2P[t.contract.code]
        if p in self.cancel_err:
            raise RuntimeError("cancel 失敗（假）")
        if t.status.status == "OrderStatus.Filled":
            raise RuntimeError("已經成交，不能撤")
        t.status.status = "OrderStatus.Cancelled"
        self.cancels.append(p)

    def list_positions(self, acc=None):
        if self.pos_fail > 0:
            self.pos_fail -= 1
            raise RuntimeError("查不到（假）")
        if self.lag and self._hist:
            seen = None
            for t, rows in self._hist:
                if t <= CLK.t - self.lag:
                    seen = rows
            if seen is not None:
                return seen
            return self._hist0
        return self._rows()

    def list_profit_loss(self, acc=None, a=None, b=None):
        return list(self.pl)

    def list_trades(self):
        return [] if self.no_list_trades else list(Trade._live)

    def list_accounts(self):
        return [ACC]

    # ── 內部 ──
    def _rows(self):
        out = []
        for p in ("TMF", "MXF"):
            h = self.hold[p]
            px = self.px[p]
            lt = self._late.get(p)
            if lt is not None and CLK.t < lt[0]:
                h, px = lt[1], lt[2]            # 這一腿的新倉成交還沒回報
            if h:
                out.append(type("P", (), {"code": C[p].code, "quantity": abs(h),
                                          "direction": "Action.Buy" if h > 0 else "Action.Sell",
                                          "price": px})())
        return out

    def _apply(self, p, delta, px):
        if delta == 0:
            return
        if p in self.late and p not in self._late:
            self._late[p] = (CLK.t + self.late[p], self.hold[p], self.px[p])
        if self.lag and not self._hist:
            self._hist0 = self._rows()
        h = self.hold[p]
        if h == 0 or (h > 0) == (delta > 0):
            nh = h + delta
            self.px[p] = (self.px[p] * abs(h) + px * abs(delta)) / abs(nh)
            self.hold[p] = nh
            self._snap()
            return
        k = min(abs(delta), abs(h))
        for _ in range(k):
            self._pid += 1
            self.pl.append(type("R", (), {"code": C[p].code, "quantity": 1,
                                          "direction": "Action.Buy" if h > 0 else "Action.Sell",
                                          "entry_price": self.px[p], "cover_price": px,
                                          "pnl": 0, "id": self._pid})())
        nh = h + delta
        if (h > 0 and nh < 0) or (h < 0 and nh > 0):
            self.bad.append(("平過頭變反向", p, h, delta))
            self.px[p] = px
        self.hold[p] = nh
        self._snap()

    def _snap(self):
        if self.lag:
            self._hist.append((CLK.t, self._rows()))

    def eq(self):
        return {p: abs(self.hold[p]) * broker.RATIO[p] for p in self.hold if self.hold[p]}


Trade._live = []
_orig_init = Trade.__init__


def _tinit(self, c, o):
    _orig_init(self, c, o)
    Trade._live.append(self)


Trade.__init__ = _tinit


def connect(fake, mini=True, live=True, keep_pos=False):
    broker.configure(fake, C["TMF"], {"MXF": C["MXF"]})
    if not keep_pos:
        broker._state["position"] = None
    broker._close_fail["at"] = 0.0
    broker._CLOSE_INFLIGHT["on"] = False
    broker._REALIZED["at"] = 0.0
    broker._META.update(last=None, swept=False)
    if mini:
        broker.MINI_FLAG.write_text("on", encoding="utf-8")
    elif broker.MINI_FLAG.exists():
        broker.MINI_FLAG.unlink()
    if live:
        broker.REAL_FLAG.write_text("on", encoding="utf-8")
    elif broker.REAL_FLAG.exists():
        broker.REAL_FLAG.unlink()
    return fake


def fresh(mini=True, live=True):
    """新的一輪：清掉落地檔與成績單（每一項各自獨立）。"""
    for d in (broker.ORDER_DIR, broker.TRADE_DIR):
        if d.exists():
            for f in d.glob("*"):
                f.unlink()
    return connect(Fake(), mini, live)


def S(fake, start=0):
    """送出去的單 ⇒ 精簡成 (合約, 方向, 口數, 新倉/平倉, 價格型態)。"""
    return [(r["p"], r["act"], r["q"], r["oc"], r["pt"]) for r in fake.sent[start:]]


def trades():
    f = broker.TRADE_DIR / ("%s.jsonl" % date.today())
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []


def cool():
    CLK.t += broker.CLOSE_COOLDOWN + 1


def pos():
    return broker._state["position"]




# ===================== QA attack scenarios (lab-qa, 2026-11-06) =====================
# Offline only: fake broker, fake clock, flags in temp dir. Failing items are real findings.

def tag(n):
    print("\n=== QA-%s ===" % n)


def live_targets(F):
    return [(p, act, px) for (p, t, act, px) in F.targets if t.status.status in ("OrderStatus.Submitted", "OrderStatus.PartFilled")]


tag("A  mix entry, TMF leg no fill -> single MXF position; stop-loss must cancel the MXF target order")
F = fresh()
F.policy = lambda pr, r: (0, F.mkt[pr]) if (pr == "TMF" and r["oc"] == "New") else (r["q"], F.mkt[pr])
ok, err, p = broker.enter("long", 38000, 60, sl_points=200, qty=11)
print("  pos:", p["product"], p["qty"], p.get("target_trade") is not None, p.get("target_trades"))
F.policy = None
F.mkt = {"TMF": 37801.0, "MXF": 37800.0}
n0 = len(F.sent)
ok, err = broker.close("sl")
print("  close:", ok, err, S(F, n0), "cancels:", F.cancels)
chk("QA-A target order cancelled before close", F.cancels, ["MXF"])
chk("QA-A no live target order left after close", live_targets(F), [])
if live_targets(F):
    F.fill_target("MXF", 2)
    print("  stray target filled later -> broker hold:", F.hold, "bad:", F.bad)

tag("A2 same, MXF leg no fill -> single TMF position")
F = fresh()
F.policy = lambda pr, r: (0, F.mkt[pr]) if (pr == "MXF" and r["oc"] == "New") else (r["q"], F.mkt[pr])
broker.enter("long", 38000, 60, sl_points=200, qty=11)
F.policy = None
n0 = len(F.sent)
ok, err = broker.close("eod")
print("  close:", ok, err, S(F, n0), "cancels:", F.cancels)
chk("QA-A2 target order cancelled", F.cancels, ["TMF"])
chk("QA-A2 no live target order left", live_targets(F), [])

tag("B  MIX close with position report lag (stale > FILL_WAIT) -> must not over-close")
for lag in (6.0, 10.0, 30.0):
    F = fresh()
    broker.enter("long", 38000, 60, sl_points=200, qty=11)
    F.lag = lag
    n0 = len(F.sent)
    ok, err = broker.close("sl")
    sent = S(F, n0)
    CLK.t += lag + 1
    F.lag = 0
    F._hist = []
    r = broker.reconcile()
    print("  lag=%s close=%s err=%s sent=%s hold=%s pos=%s" % (lag, ok, (err or "")[:60], sent, F.hold,
          r if not isinstance(r, dict) else (r.get("product"), r.get("qty"))))
    chk("QA-B lag=%s no over-close" % lag, F.bad, [])
    chk("QA-B lag=%s broker flat afterwards" % lag, F.eq(), {})

tag("C  TP leg partially filled just before stop-loss, position report stale (lag 3s)")
F = fresh()
broker.enter("long", 38000, 60, sl_points=200, qty=11)
F.lag = 3.0
F.fill_target("MXF", 1)
n0 = len(F.sent)
ok, err = broker.close("sl")
print("  close:", ok, (err or "")[:80], S(F, n0), "hold:", F.hold, "bad:", F.bad)
chk("QA-C no over-close when TP filled just before stop (mix)", F.bad, [])

tag("C2 same for single MXF 2 lots (qty=10)")
F = fresh()
broker.enter("long", 38000, 60, sl_points=200, qty=10)
F.lag = 3.0
F.fill_target("MXF", 1)
n0 = len(F.sent)
ok, err = broker.close("sl")
print("  close:", ok, (err or "")[:80], S(F, n0), "hold:", F.hold, "bad:", F.bad)
chk("QA-C2 no over-close when TP filled just before stop (MXF 2)", F.bad, [])

tag("D  _close_multi (single contract multi-lot) on branch: report lag 6s")
for qq, mini in ((10, False), (10, True), (7, True)):
    F = fresh(mini=mini)
    broker.enter("long", 38000, 60, sl_points=200, qty=qq)
    F.lag = 6.0
    n0 = len(F.sent)
    ok, err = broker.close("sl")
    print("  qty=%s mini=%s close=%s sent=%s hold=%s bad=%s" % (qq, mini, ok, S(F, n0), F.hold, F.bad))
    chk("QA-D qty=%s mini=%s no over-close in _close_multi" % (qq, mini), F.bad, [])

tag("E  flag OFF: our TMF 3 + his manual MXF 1 same direction (main -> unknown)")
F = fresh(mini=False)
broker.enter("long", 38000, 60, sl_points=200, qty=3)
F._apply("MXF", 1, 38000.0)
r = broker.reconcile()
print("  reconcile ->", r if not isinstance(r, dict) else (r.get("product"), r.get("qty")), "| last_error:", broker._state.get("last_error"))
chk("QA-E flag off, local TMF + broker TMF+MXF -> unknown", r, "unknown")

tag("E2 flag OFF, no local position, broker has manual TMF 1 + MXF 1 same direction")
F = fresh(mini=False)
F._apply("TMF", 1, 38000.0)
F._apply("MXF", 1, 38000.0)
r = broker.reconcile()
print("  reconcile ->", r if not isinstance(r, dict) else {k: r.get(k) for k in ("product", "qty", "mix", "recovered")})
chk("QA-E2 flag off, broker TMF+MXF -> unknown (same as main)", r, "unknown")
if isinstance(r, dict):
    n0 = len(F.sent)
    ok, err = broker.close("sl")
    print("  panel stop on adopted position ->", ok, (err or "")[:80], S(F, n0), F.hold)

tag("E3 flag OFF, our TMF 1 entering while he already holds MXF 1 same direction")
F = fresh(mini=False)
F._apply("MXF", 1, 38000.0)
try:
    ok, err, p = broker.enter("long", 38500, 60, sl_points=200, qty=1)
    print("  enter ->", ok, err, None if p is None else {k: p.get(k) for k in ("product", "qty", "entry")}, "sent:", S(F))
except Exception as e:
    print("  enter raised", e)

tag("F  restart with MIX open then stop-loss: TP orders left on book? (shared with main)")
F = fresh()
broker.enter("long", 38000, 60, sl_points=200, qty=11)
connect(F)
got = broker.reconcile()
print("  recovered:", {k: got.get(k) for k in ("product", "qty", "mix", "multi_unknown")})
n0 = len(F.sent)
ok, err = broker.close("sl")
print("  close:", ok, (err or "")[:60], S(F, n0), "cancels:", F.cancels, "live targets:", live_targets(F))
chk("QA-F (shared) no live TP order after close following restart", len(live_targets(F)), 0)

# ⭐ 2026-11-12 QA-F 一口微台那條（不走帳本）：重啟撿回後停損 ⇒ 用落地的送單紀錄認回自己的停利單、撤掉
for _q, _mini in ((1, False), (3, False), (5, True)):
    tag("F%d  restart with %d lot(s) open then stop-loss (mini=%s)" % (_q, _q, _mini))
    F = fresh(mini=_mini)
    broker.enter("long", 38000, 60, sl_points=200, qty=_q)
    connect(F, _mini)
    broker.reconcile()
    ok, err = broker.close("sl")
    chk("QA-F%d no live TP order after close following restart" % _q, (ok, live_targets(F)), (True, []))
tag("F1b  same, but the send log is gone -> ours cannot be identified -> nothing cancelled (his orders are never touched)")
F = fresh(mini=False)
broker.enter("long", 38000, 60, sl_points=200, qty=1)
for f in broker.ORDER_DIR.glob("*"):
    f.unlink()
connect(F, False)
broker.reconcile()
broker.close("sl")
chk("QA-F1b only orders in our own send log get cancelled", len(live_targets(F)), 1)

tag("G  IOC close: status Cancelled first, deals arrive 3 updates later, lag 6s")
F = fresh()
broker.enter("long", 38000, 60, sl_points=200, qty=11)
F.lag = 6.0
_orig_po = F.place_order


def po(c, o, F=F, _o=_orig_po):
    t = _o(c, o)
    if str(o.octype).endswith("Cover") and str(o.price_type).endswith("MKP"):
        d, st = t.status.deals, t.status.status
        t.status.deals, t.status.status = [], "OrderStatus.Cancelled"
        t.hidden = (d, st, 3)
    return t


F.place_order = po
n0 = len(F.sent)
ok, err = broker.close("sl")
print("  close:", ok, (err or "")[:80], S(F, n0), "hold:", F.hold, "bad:", F.bad)
chk("QA-G no over-close when IOC Cancelled status precedes deals", F.bad, [])

tag("H  late MXF leg (20s), stop-loss fires before it is reported")
F = fresh()
F.late = {"MXF": 20.0}
ok, err, p = broker.enter("long", 38000, 60, sl_points=200, qty=11)
print("  pos at entry:", p["product"], p["qty"])
n0 = len(F.sent)
ok, err = broker.close("sl")
print("  close:", ok, (err or "")[:140], S(F, n0), "hold:", F.hold)
CLK.t = F._late["MXF"][0] + 1
r = broker.reconcile()
print("  after late report:", r if not isinstance(r, dict) else {k: r.get(k) for k in ("product", "qty", "recovered", "sl_src")})
say(isinstance(r, dict) and r.get("qty") == 10, "QA-H late leg adopted after close", r)
chk("QA-H no live TP order left", live_targets(F), [])

tag("I  cashflow fee for MIX")
import cashflow  # noqa: E402
t = {"qty": 11, "product": "MIX", "mix": {"MXF": 10, "TMF": 1}, "entry": 38000, "exit": 38100}
print("  est_fee MIX 11:", cashflow.est_fee(t), " TMF 11:", cashflow.est_fee({"qty": 11, "entry": 38000, "exit": 38100}))

print("\n%d passed" % N_OK[0])
print("ALL PASS" if not FAIL else "%d FAIL: %s" % (len(FAIL), FAIL))
