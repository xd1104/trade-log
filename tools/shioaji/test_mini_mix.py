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
        self.status = type("S", (), {"status": "Submitted", "deals": []})()
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
        st = "Filled" if k == o.quantity else "Cancelled"
        if self.hide and self.rng.random() < self.hide[0]:
            t.hidden = (deals, st, self.hide[1])
        else:
            t.status.deals, t.status.status = deals, st
        return t

    def fill_target(self, p, k, px=None):
        for (tp, t, act, price) in self.targets:
            if tp == p and t.status.status == "Submitted":
                px = float(price) if px is None else px
                self._apply(p, (k if act == "Buy" else -k), px)
                t.status.deals.append(Deal(px, k))
                if sum(d.quantity for d in t.status.deals) >= t.order.quantity:
                    t.status.status = "Filled"
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
        if t.status.status == "Filled":
            raise RuntimeError("已經成交，不能撤")
        t.status.status = "Cancelled"
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


# ════════════════════════════════════════════════════════════════════════════
print("=== a. 混搭進場：兩腿全成交（11＝2 小台＋1 微台，停利 60）===")
F = fresh()
ok, err, p = broker.enter("long", 38000, 60, sl_points=200, qty=11)
chk("進場成功", (ok, err), (True, None))
chk("送單：先小台 2 口新倉、再微台 1 口新倉（MKP），再兩張停利（LMT 平倉）", S(F),
    [("MXF", "Buy", 2, "New", "MKP"), ("TMF", "Buy", 1, "New", "MKP"),
     ("MXF", "Sell", 2, "Cover", "LMT"), ("TMF", "Sell", 1, "Cover", "LMT")])
chk("兩張進場單都是 IOC、停利都是 ROD", [r["ot"] for r in F.sent], ["IOC", "IOC", "ROD", "ROD"])
chk("停利價各用自己那一腿的成交價 +60", [r["price"] for r in F.sent[2:]], [38060, 38061])
chk("部位：MIX、等值 11、兩腿 10＋1", (p["product"], p["qty"], p["mix"], p["mix0"]),
    ("MIX", 11, {"MXF": 10, "TMF": 1}, {"MXF": 10, "TMF": 1}))
chk("進場價＝依等值加權", p["entry"], round((38000 * 10 + 38001) / 11, 2))
chk("成交價區間涵蓋兩腿", (p["fill_lo"], p["fill_hi"]), (38000.0, 38001.0))
chk("停利兩張都記著（依合約）", sorted(p["target_trades"]), ["MXF", "TMF"])
snap = broker.snapshot()
say(json.dumps(snap, ensure_ascii=False, default=None) is not None and "target_trades" not in snap["position"]
    and "entry_trades" not in snap["position"] and snap["position"]["has_target"],
    "snapshot 濾掉兩腿的永豐物件、has_target＝True、序列化得了")
logs = [json.loads(x) for x in next(broker.ORDER_DIR.glob("2*.jsonl")).read_text(encoding="utf-8").splitlines()]
chk("每天進場次數只算一次（微台那張記 entry_mix_tmf）", broker.entries_today(), 1)
meta = json.loads((broker.ORDER_DIR / broker.META_NAME).read_text(encoding="utf-8"))
chk("落地檔記得兩腿", (meta.get("product"), meta.get("mix"), meta.get("mix0")),
    ("MIX", {"MXF": 10, "TMF": 1}, {"MXF": 10, "TMF": 1}))

print("\n=== b. 小台成交、微台沒成交 ⇒ 單一小台部位 ===")
F = fresh()
F.policy = lambda pr, r: (0, F.mkt[pr]) if pr == "TMF" else (r["q"], F.mkt[pr])
ok, err, p = broker.enter("long", 38000, 60, sl_points=200, qty=11)
chk("進場成功", ok, True)
chk("送單：小台 2、微台 1，停利只掛小台 2 口", S(F),
    [("MXF", "Buy", 2, "New", "MKP"), ("TMF", "Buy", 1, "New", "MKP"), ("MXF", "Sell", 2, "Cover", "LMT")])
chk("部位：MXF 等值 10、沒有 mix", (p["product"], p["qty"], "mix" in p, p["qty_sent"]), ("MXF", 10, False, 11))
say(p.get("target_trade") is not None and "target_trades" not in p, "停利走單一合約那條（target_trade）")

print("\n=== c. 微台成交、小台沒成交 ⇒ 單一微台部位 ===")
F = fresh()
F.policy = lambda pr, r: (0, F.mkt[pr]) if pr == "MXF" else (r["q"], F.mkt[pr])
ok, err, p = broker.enter("short", 38000, 60, sl_points=200, qty=11)
chk("送單：小台賣 2、微台賣 1，停利只掛微台買回 1", S(F),
    [("MXF", "Sell", 2, "New", "MKP"), ("TMF", "Sell", 1, "New", "MKP"), ("TMF", "Buy", 1, "Cover", "LMT")])
chk("部位：TMF 1 口、做空", (p["product"], p["qty"], p["dir"], "mix" in p), ("TMF", 1, "short", False))
chk("停利價＝微台成交價 −60", F.sent[-1]["price"], 38001 - 60)

print("\n=== d. 小台部分成交（2 口只成交 1 口）＋微台全成交（13＝2 小台＋3 微台）===")
F = fresh()
F.policy = lambda pr, r: (1, F.mkt[pr]) if pr == "MXF" else (r["q"], F.mkt[pr])
ok, err, p = broker.enter("long", 38000, 60, sl_points=200, qty=13)
chk("送單：小台 2、微台 3，停利 小台 1＋微台 3（照實際成交）", S(F),
    [("MXF", "Buy", 2, "New", "MKP"), ("TMF", "Buy", 3, "New", "MKP"),
     ("MXF", "Sell", 1, "Cover", "LMT"), ("TMF", "Sell", 3, "Cover", "LMT")])
chk("部位：MIX 等值 8（5＋3）、送了 13", (p["product"], p["qty"], p["mix"], p["entry_qty"], p["qty_sent"]),
    ("MIX", 8, {"MXF": 5, "TMF": 3}, 8, 13))
logs = [json.loads(x) for x in next(broker.ORDER_DIR.glob("2*.jsonl")).read_text(encoding="utf-8").splitlines()]
say(any(l.get("kind") == "entry_partial" for l in logs), "記了 entry_partial（⛔ 不追單補）")
chk("⛔ 沒有追單（New 只有兩張）", sum(1 for r in F.sent if r["oc"] == "New"), 2)

print("\n=== e. 兩腿都沒成交 ⇒ 沒部位、不掛停利 ===")
F = fresh()
F.policy = lambda pr, r: (0, F.mkt[pr])
ok, err, p = broker.enter("long", 38000, 60, sl_points=200, qty=11)
chk("回報沒成交", (ok, "沒有成交" in (err or "")), (False, True))
chk("只送了兩張進場單，沒有停利", S(F), [("MXF", "Buy", 2, "New", "MKP"), ("TMF", "Buy", 1, "New", "MKP")])
chk("沒部位", pos(), None)

print("\n=== f. 停損：兩腿都平掉 ===")
F = fresh()
broker.enter("long", 38000, 60, sl_points=200, qty=11)
n0 = len(F.sent)
F.mkt = {"TMF": 37801.0, "MXF": 37800.0}
ok, err = broker.close("sl")
chk("平倉成功", (ok, err), (True, None))
chk("兩張停利都撤了", sorted(F.cancels), ["MXF", "TMF"])
chk("送單：小台賣 2 口平倉、微台賣 1 口平倉（MKP）", S(F, n0), [("MXF", "Sell", 2, "Cover", "MKP"), ("TMF", "Sell", 1, "Cover", "MKP")])
chk("平倉單都是 IOC", [r["ot"] for r in F.sent[n0:]], ["IOC", "IOC"])
chk("沒部位、券商也空手", (pos(), F.eq()), (None, {}))
tr = trades()
chk("成績單一筆：MIX、等值 11", [(t["product"], t["qty"], t["mix"], t["reason"]) for t in tr],
    [("MIX", 11, {"MXF": 10, "TMF": 1}, "sl")])
xp = round((37800 * 10 + 37801) / 11, 2)
chk("出場價依等值加權、點數＝出場−進場", (tr[0]["exit"], tr[0]["points"]),
    (xp, round(xp - round((38000 * 10 + 38001) / 11, 2), 1)))
chk("⛔ 沒有平過頭", F.bad, [])

print("\n=== g. 一腿連續平不掉 ⇒ 留下那一腿、回報失敗；下一輪只平那一腿 ===")
F = fresh()
broker.enter("long", 38000, 60, sl_points=200, qty=11)
n0 = len(F.sent)
F.policy = lambda pr, r: (0, F.mkt[pr]) if (pr == "TMF" and r["oc"] == "Cover") else (r["q"], F.mkt[pr])
F.mkt = {"TMF": 37801.0, "MXF": 37800.0}
ok, err = broker.close("sl")
chk("回報失敗", ok, False)
say("微台那一腿還剩 1 口等值" in (err or "") and "小台那一腿已平掉" in (err or ""), "原因講得出哪一腿平掉、哪一腿還在", err)
chk("送單：小台 2 一次；微台 1 送 CLOSE_TRIES 次", S(F, n0),
    [("MXF", "Sell", 2, "Cover", "MKP")] + [("TMF", "Sell", 1, "Cover", "MKP")] * broker.CLOSE_TRIES)
p = pos()
chk("部位留下微台 1 口（降成單一合約、mix0 還在）", (p["product"], p["qty"], "mix" in p, p["mix0"]),
    ("TMF", 1, False, {"MXF": 10, "TMF": 1}))
chk("平掉的小台那一段記成 leg", [(l["qty"], l["px"], l["src"]) for l in p["legs"]], [(10, 37800.0, "sl")])
ok2, err2 = broker.close("sl")
chk("冷卻中 ⇒ 不送", (ok2, len(F.sent)), (False, n0 + 1 + broker.CLOSE_TRIES))
cool()
F.policy = None
n1 = len(F.sent)
ok, err = broker.close("sl")
chk("下一輪只平微台 1 口（⛔ 沒有再送小台）", (ok, S(F, n1)), (True, [("TMF", "Sell", 1, "Cover", "MKP")]))
chk("沒有重撤已經撤掉的停利單", sorted(F.cancels), ["MXF", "TMF"])
tr = trades()
chk("成績單一筆、等值 11、出場價兩段加權", [(t["product"], t["qty"], t["exit"]) for t in tr],
    [("MIX", 11, round((37800 * 10 + 37801) / 11, 2))])
chk("⛔ 沒有平過頭、券商空手", (F.bad, F.eq(), pos()), ([], {}, None))

print("\n=== h. 平倉前券商已經少了一腿（他自己平掉小台）⇒ 只平剩下那腿 ===")
F = fresh()
broker.enter("long", 38000, 60, sl_points=200, qty=11)
F._apply("MXF", -2, 37900.0)                      # 他在大戶投平掉小台 2 口
n0 = len(F.sent)
ok, err = broker.close("manual")
chk("平倉成功", ok, True)
chk("只送微台 1 口平倉", S(F, n0), [("TMF", "Sell", 1, "Cover", "MKP")])
chk("兩張停利都撤（小台那張還掛著，不撤會變反向新倉）", sorted(F.cancels), ["MXF", "TMF"])
tr = trades()
chk("成績單一筆 11 口；小台那段不是我們平的 ⇒ 出場價留白（⛔ 不猜）",
    [(t["qty"], t["exit"], [l["src"] for l in t["legs"]]) for t in tr], [(11, None, ["elsewhere", "manual"])])
chk("⛔ 沒有平過頭", (F.bad, F.eq()), ([], {}))

print("\n=== i. 重啟撿回與對帳 ===")
F = fresh()
F.hold, F.px = {"MXF": 2, "TMF": 1}, {"MXF": 38000.0, "TMF": 38001.0}
got = broker.reconcile()
chk("券商同方向混搭 ⇒ 撿回成 MIX", (got["product"], got["qty"], got["mix"], got["recovered"]),
    ("MIX", 11, {"MXF": 10, "TMF": 1}, True))
chk("撿回的進場價＝加權", got["entry"], round((38000 * 10 + 38001) / 11, 2))
F = fresh()
F.hold, F.px = {"MXF": 2, "TMF": -1}, {"MXF": 38000.0, "TMF": 38001.0}
chk("⛔ 反方向 ⇒ unknown、本機沒部位", (broker.reconcile(), pos()), ("unknown", None))
n0 = len(F.sent)
ok, err = broker.close("sl")
chk("⛔ 反方向時平倉一張都不送", (ok, len(F.sent) - n0), (False, 0))
F = fresh()
broker.enter("long", 38000, None, sl_points=200, qty=11)
F._apply("MXF", -2, 37900.0)
got = broker.reconcile()
chk("本機 MIX、券商只剩微台 ⇒ 縮成 TMF 1 口", (got["product"], got["qty"], "mix" in got), ("TMF", 1, False))
chk("少掉的那腿記成 elsewhere（不是我們平的）", [(l["qty"], l["src"]) for l in got["legs"]], [(10, "elsewhere")])
F = fresh()
broker.enter("long", 38000, None, sl_points=200, qty=3)
chk("（3 口 ⇒ 微台）", pos()["product"], "TMF")
F._apply("MXF", 1, 38000.0)
chk("⛔ 本機 TMF、券商變 MIX ⇒ unknown", broker.reconcile(), "unknown")
chk("  本機部位沒被清掉", (pos()["product"], pos()["qty"]), ("TMF", 3))
say("分不出來" in (broker._state["last_error"] or ""), "  示警寫在 last_error", broker._state["last_error"])
F = fresh()
broker.enter("long", 38000, None, sl_points=200, qty=11)
F._apply("MXF", 1, 38000.0)
F._apply("TMF", -1, 38000.0)
chk("⛔ 本機 MIX、券商一腿變多一腿變少 ⇒ unknown", broker.reconcile(), "unknown")
# 重啟：落地檔對得上 ⇒ 補回混搭狀態
F = fresh()
broker.enter("long", 38000, 60, sl_points=200, qty=11)
connect(F)                                        # 模擬重啟：記憶體清空
got = broker.reconcile()
chk("重啟撿回混搭、落地檔補回（entry_qty、mix0、進場價）",
    (got["product"], got["mix"], got.get("entry_qty"), got.get("mix0"), got.get("sl_src")),
    ("MIX", {"MXF": 10, "TMF": 1}, 11, {"MXF": 10, "TMF": 1}, "meta"))
connect(F)
F._apply("MXF", -2, 38060.0)                      # 停機期間小台那一腿停利成交
got = broker.reconcile()
chk("重啟時只剩 1 口 ⇒ 照 QA r4 G7 不套落地檔（走帳本認人那條），撿回單純的 TMF 1 口",
    (got["product"], got["qty"], "mix" in got, "mix0" in got), ("TMF", 1, False, False))
F = fresh()
broker.enter("long", 38000, 60, sl_points=200, qty=13)
connect(F)
F._apply("MXF", -2, 38060.0)                      # 停機期間小台那一腿停利成交，剩微台 3 口
got = broker.reconcile()
chk("13 口混搭、停機期間小台那腿不見 ⇒ 撿回 TMF 3、標「等對帳」（shrink）",
    (got["product"], got["qty"], got.get("unknown_src"), got.get("mix0")), ("TMF", 3, "shrink", {"MXF": 10, "TMF": 3}))
chk("  ⛔ 合約以券商為準（不被落地的 MIX 蓋掉）", "mix" in got, False)
F.pl.clear()
F._pid = 0
for _i in range(2):                               # 已實現：小台 2 列（＝我們停利那 10 口等值）
    F.pl.append(type("R", (), {"code": "MXFK6", "quantity": 1, "direction": "Action.Buy", "entry_price": 38000.0,
                               "cover_price": 38060.0, "pnl": 0, "id": 900 + _i})())
chk("  跟已實現對帳：平掉 10 口等值 ≤ 帳上 10 ⇒ 放行（ok）", broker.resolve_shrink(), "ok")
n0 = len(F.sent)
ok, err = broker.close("sl")
chk("  放行後停損只平微台 3 口（走混搭平倉、不碰小台）", (ok, S(F, n0)), (True, [("TMF", "Sell", 3, "Cover", "MKP")]))
chk("  ⛔ 沒有平過頭", F.bad, [])

print("\n=== j. 停利：兩張各自正確；一腿停利成交 ⇒ 縮量正確；平倉時兩張都撤 ===")
F = fresh()
broker.enter("long", 38000, 60, sl_points=200, qty=11)
F.fill_target("MXF", 2)
got = broker.reconcile()
chk("小台停利成交 ⇒ 剩微台 1 口", (got["product"], got["qty"]), ("TMF", 1))
chk("那一段記成 tp、價錢＝停利價", [(l["qty"], l["px"], l["src"]) for l in got["legs"]], [(10, 38060.0, "tp")])
F.fill_target("TMF", 1)
got = broker.reconcile()
chk("微台停利也成交 ⇒ 沒部位", got, None)
tr = trades()
ent = round((38000 * 10 + 38001) / 11, 2)
xp = round((38060 * 10 + 38061) / 11, 2)
chk("成績單一筆：tp、11 口、出場兩腿加權、點數", [(t["reason"], t["qty"], t["exit"], t["points"]) for t in tr],
    [("tp", 11, xp, round(xp - ent, 1))])
F = fresh()
broker.enter("long", 38000, 60, sl_points=200, qty=11)
F.fill_target("MXF", 2)
n0 = len(F.sent)
ok, err = broker.close("sl")
chk("小台停利已成交後停損 ⇒ 只撤微台那張、只平微台 1 口", (ok, F.cancels, S(F, n0)),
    (True, ["TMF"], [("TMF", "Sell", 1, "Cover", "MKP")]))
chk("撤不到成交完的那張不算殘單（沒有「請自己刪」）", err, None)
F = fresh()
broker.enter("long", 38000, 60, sl_points=200, qty=11)
F.cancel_err = {"TMF"}
ok, err = broker.close("sl")
say(ok and "停利單沒撤掉" in (err or ""), "撤不掉 ⇒ 照樣平倉、但講出來", err)

print("\n=== p. 進場一腿晚回報 ⇒ 用那張進場單的明細認回來併進部位（⛔ 不是 unknown）===")
W11 = round((38000 * 10 + 38001) / 11, 2)


def late_case(prod, secs, qty=11, d="long", tp=60):
    F = fresh()
    F.late = {prod: secs}
    ok, err, p = broker.enter(d, 38000, tp, sl_points=200, qty=qty)
    return F, ok, p


for secs in (6.0, 20.0):
    F, ok, p = late_case("MXF", secs)
    chk("小台腿晚 %g 秒：進場當下只看到微台 1 口（照實際）、fill_warn 講出來" % secs,
        (ok, p["product"], p["qty"], "晚到的話" in (p.get("fill_warn") or "")), (True, "TMF", 1, True))
    chk("  記下送出去的兩腿", (p["mix_sent"], sorted(p["mix_sent_ids"])), ({"MXF": 10, "TMF": 1}, ["MXF", "TMF"]))
    until = F._late["MXF"][0]
    if until - CLK.t > 1:                         # 進場等成交已經用掉 FILL_WAIT 秒；還沒到回報時刻才驗這一段
        CLK.t += (until - CLK.t) / 2
        got = broker.reconcile()
        chk("  還沒回報那段時間：照舊 TMF 1（不動、不是 unknown）", (got["product"], got["qty"]), ("TMF", 1))
    CLK.t = until + 1
    got = broker.reconcile()
    chk("  回報到了 ⇒ 併成 MIX 11（⛔ 不是 unknown）", (got if got == "unknown" else (got["product"], got["qty"], got["mix"])),
        ("MIX", 11, {"MXF": 10, "TMF": 1}))
    chk("  進場價重新加權、帳本那個價留在 entry0、entry_qty／mix0 跟著加", (got["entry"], got["entry0"], got["entry_qty"], got["mix0"],
                                                         got.get("late_leg")),
        (W11, 38001.0, 11, {"MXF": 10, "TMF": 1}, True))
    chk("  原本那張微台停利單搬進 target_trades（平倉時一起撤）", (sorted(got["target_trades"]), got["target_trade"]), (["TMF"], None))
    say("沒有停利單" in (got.get("fill_warn") or ""), "  講出「晚到那幾口沒有停利單」", got.get("fill_warn"))
    meta = json.loads((broker.ORDER_DIR / broker.META_NAME).read_text(encoding="utf-8"))
    chk("  落地檔跟著更新", (meta.get("product"), meta.get("qty"), meta.get("late_leg"), meta.get("mix_sent")),
        ("MIX", 11, True, {"MXF": 10, "TMF": 1}))
n0 = len(F.sent)
F.mkt = {"TMF": 37801.0, "MXF": 37800.0}
ok, err = broker.close("sl")
chk("晚到併進來之後停損：兩腿都平、撤掉微台那張停利", (ok, S(F, n0), F.cancels),
    (True, [("MXF", "Sell", 2, "Cover", "MKP"), ("TMF", "Sell", 1, "Cover", "MKP")], ["TMF"]))
chk("  ⛔ 沒有平過頭、空手", (F.bad, F.eq(), pos()), ([], {}, None))
t = trades()[-1]
xp = round((37800 * 10 + 37801) / 11, 2)
chk("  成績單：entry＝帳本那個價（認人用）、entry_w＝加權、points 用加權算", (t["qty"], t["entry"], t["entry_w"], t["points"]),
    (11, 38001.0, W11, round(xp - W11, 1)))

F, ok, p = late_case("TMF", 6.0)
chk("微台腿晚到：進場當下只看到小台 10", (p["product"], p["qty"]), ("MXF", 10))
CLK.t += 7
got = broker.reconcile()
chk("  回報到了 ⇒ 併成 MIX 11", (got if got == "unknown" else (got["product"], got["qty"], got["mix"])),
    ("MIX", 11, {"MXF": 10, "TMF": 1}))
n0 = len(F.sent)
ok, err = broker.close("sl")
chk("  停損兩腿都平、⛔ 不平過頭", (ok, S(F, n0), F.bad), (True, [("MXF", "Sell", 2, "Cover", "MKP"), ("TMF", "Sell", 1, "Cover", "MKP")], []))

F = fresh()
F.policy = lambda pr, r: (0, F.mkt[pr]) if (pr == "MXF" and r["oc"] == "New") else (r["q"], F.mkt[pr])
broker.enter("long", 38000, 60, sl_points=200, qty=11)
F.policy = None
F._apply("MXF", 3, 38000.0)                       # 券商上冒出小台 3 口（15 等值 > 送出的 10）
chk("⛔ 晚到量超過送出量 ⇒ unknown", broker.reconcile(), "unknown")
chk("  本機部位不動", (pos()["product"], pos()["qty"]), ("TMF", 1))
F._apply("MXF", -1, 38000.0)                      # 剩 2 口（＝送出量），但那張進場單明細是 0（IOC 沒撮到）
chk("⛔ 量沒超過、但進場單明細證明不是它成交的 ⇒ unknown", broker.reconcile(), "unknown")
F = fresh()
F.late = {"MXF": 6.0}
broker.enter("short", 38000, 60, sl_points=200, qty=11)
CLK.t += 7
F._apply("MXF", 4, 38000.0)                      # 反方向：小台變成淨多（他自己買的比我們空的多）
chk("⛔ 晚到那一腿方向相反 ⇒ unknown", broker.reconcile(), "unknown")
F, ok, p = late_case("MXF", 6.0, d="short")
F._apply("TMF", 1, 38001.0)                       # 他把微台那口空單平掉
F._apply("MXF", 4, 38000.0)                       # 小台：我們晚到的空 2 口＋他買 4 口 ⇒ 券商回報時是多 2 口（等值 10 ≤ 送出量）
CLK.t = F._late["MXF"][0] + 1
chk("⛔ 本機空單、券商只剩反方向的小台（量、明細都沒超過）⇒ 不併（unknown）", broker.reconcile(), "unknown")
chk("  本機部位方向沒被改", (pos()["dir"], pos()["product"]), ("short", "TMF"))
F, ok, p = late_case("MXF", 6.0)
F._apply("MXF", 1, 38000.0)                       # 晚到的 2 口＋他多買 1 口 ⇒ 回報時 3 口（15 > 送出的 10）
CLK.t = F._late["MXF"][0] + 1
chk("⛔ 真的晚到、但券商那一腿比送出去的多 ⇒ unknown", broker.reconcile(), "unknown")
F, ok, p = late_case("MXF", 6.0)
CLK.t += broker.LATE_LEG_S + 1
chk("⛔ 超過 LATE_LEG_S 才出現 ⇒ 不當成晚到（unknown）", broker.reconcile(), "unknown")

F, ok, p = late_case("TMF", 40.0)
chk("微台腿晚 40 秒、回報前就觸發停損：先照看得到的小台 10 開著", (p["product"], p["qty"]), ("MXF", 10))
n0 = len(F.sent)
ok, err = broker.close("sl")
chk("  只平看得到的小台 2 口（⛔ 看不到的那腿不送）", (ok, S(F, n0)), (True, [("MXF", "Sell", 2, "Cover", "MKP")]))
say("微台 1 口等值的成交券商還沒回報" in (err or ""), "  講出「還有一腿成交了但券商還沒回報」", err)
CLK.t = F._late["TMF"][0] + 1
got = broker.reconcile()
chk("  回報到了 ⇒ 撿回成一筆部位（不是 unknown；照撿回部位的規矩有停損）",
    (got if got == "unknown" else (got["product"], got["qty"], got["recovered"])), ("TMF", 1, True))
n0 = len(F.sent)
ok, err = broker.close("sl")
chk("  再平掉它、⛔ 不平過頭", (ok, S(F, n0), F.bad, F.eq()), (True, [("TMF", "Sell", 1, "Cover", "MKP")], [], {}))

print("\n  -- 晚到期間重啟（落地檔補回 mix_sent，用委託單編號問回明細）--")
F, ok, p = late_case("MXF", 20.0)
connect(F)                                        # 重啟：記憶體清空（兩張進場單物件也沒了）
CLK.t += 21
got = broker.reconcile()
chk("重啟後撿回 MIX 11、落地檔補回並併好（late_leg、mix_sent、entry_qty 11、sl_src meta）",
    (got.get("product"), got.get("qty"), got.get("late_leg"), got.get("mix_sent"), got.get("entry_qty"),
     got.get("sl_src"), got.get("multi_unknown")),
    ("MIX", 11, True, {"MXF": 10, "TMF": 1}, 11, "meta", None))
chk("  進場價＝加權、entry0＝帳本那個價", (got["entry"], got["entry0"]), (W11, 38001.0))
n0 = len(F.sent)
ok, err = broker.close("sl")
chk("  停損兩腿都平、⛔ 不平過頭", (ok, S(F, n0), F.bad), (True, [("MXF", "Sell", 2, "Cover", "MKP"), ("TMF", "Sell", 1, "Cover", "MKP")], []))
F, ok, p = late_case("MXF", 20.0)
connect(F)
F.no_list_trades = True                           # 問不回委託單明細 ⇒ 沒有證據
CLK.t += 21
got = broker.reconcile()
chk("⛔ 重啟後問不回進場單明細 ⇒ 不認（分不出、不平）", (got.get("product"), got.get("multi_unknown")), ("MIX", True))
n0 = len(F.sent)
ok, err = broker.close("sl")
chk("  分不出 ⇒ 一張都不送", (ok, len(F.sent) - n0), (False, 0))
F = fresh()
F.policy = lambda pr, r: (0, F.mkt[pr]) if (pr == "MXF" and r["oc"] == "New") else (r["q"], F.mkt[pr])
broker.enter("long", 38000, 60, sl_points=200, qty=11)     # 小台那張 IOC 真的沒撮到（明細 0）
F.policy = None
connect(F)                                        # 重啟
F._apply("MXF", 2, 38000.0)                       # 停機期間他自己買了小台 2 口
got = broker.reconcile()
chk("⛔ 重啟後問回來的進場單明細是 0（不是它成交的）⇒ 不認（分不出）", (got.get("product"), got.get("multi_unknown"),
                                                              got.get("late_leg")), ("MIX", True, None))

print("\n=== n. 不平過頭的性質測試（固定種子，隨機成交／不成交／部分成交／回報慢／查不到）===")
rng = random.Random(20261106)
viol, incons, rounds_left = [], [], 0
N_PROP = 300
for it in range(N_PROP):
    F = fresh()
    F.rng = rng
    q = rng.choice([q for q in range(6, 50) if q % 5])
    d = rng.choice(["long", "short"])
    if rng.random() < 0.35:                       # 進場腿晚回報（幾秒後才出現在券商部位）
        F.late = {rng.choice(["MXF", "TMF"]): rng.choice([2.0, 6.0, 12.0, 20.0])}   # 第一次平倉前會回報（見 p 段 40 秒那一項）
    broker.enter(d, 38000, rng.choice([None, 60]), sl_points=200, qty=q)
    if pos() is None:
        incons.append((it, "沒進場"))
        continue

    def pol(pr, r, F=F):
        if r["oc"] == "New":
            return r["q"], F.mkt[pr]
        x = rng.random()
        if x < 0.35:
            return r["q"], F.mkt[pr]
        if x < 0.7:
            return 0, F.mkt[pr]
        return rng.randint(0, r["q"]), F.mkt[pr]
    F.policy = pol
    F.lag = rng.choice([0, 0, 1.0, 3.0, 6.0, 10.0])      # 回報慢幾秒（實測 09-23／10-15 約 5 秒；＞等成交的 5 秒、＜冷卻 15 秒）
    F.hide = (rng.choice([0.0, 0.3]), rng.choice([1, 2]))
    if rng.random() < 0.3 and pos().get("target_trades"):
        F.fill_target(rng.choice(sorted(pos()["target_trades"])), 1)
    for _k in range(10):
        if rng.random() < 0.15:
            F.pos_fail = rng.choice([1, 3, 8])
        cool()
        r = broker.close("sl")
        if pos() is None and not F.eq():
            break
    F.lag, F.pos_fail, F.hide = 0, 0, None
    F.update_status()
    F.update_status()
    F._hist = []
    for _k in range(2):
        broker.reconcile()
    viol += [(it,) + tuple(b) for b in F.bad]
    p, eq = pos(), F.eq()
    if p is None:
        if eq:
            incons.append((it, "本機空手、券商還有", eq))
    elif p == "unknown" or not isinstance(p, dict):
        incons.append((it, "部位不是 dict", p))
    else:
        rounds_left += 1
        mine = {k: v for k, v in (p.get("mix") or {p["product"]: p["qty"]}).items() if v}
        if mine != eq or p["qty"] != sum(eq.values()):
            incons.append((it, "本機跟券商不一致", mine, eq))
chk("⛔ %d 輪裡沒有任何一張平倉單超過那一腿的持有、沒有平過頭變反向" % N_PROP, viol[:3], [])
chk("最後要嘛空手、要嘛留下的部位跟券商一致", incons[:3], [])
print("    （其中 %d 輪最後還留著部位＝那幾腿一直平不掉，本機跟券商一致）" % rounds_left)

print("\n=== o. 開關沒開 ⇒ 全部微台、行為跟以前一樣 ===")
F = fresh(mini=False)
chk("1～10 口一律 TMF", {broker.product_for(q) for q in range(1, 11)}, {"TMF"})
chk("11 口以上不合理", [broker.qty_ok(q) for q in (10, 11, 13, 50)], [True, False, False, False])
ok, err, p = broker.enter("long", 38000, 60, sl_points=200, qty=7)
chk("7 口 ⇒ 一張微台 7 口＋一張停利 7 口", S(F), [("TMF", "Buy", 7, "New", "MKP"), ("TMF", "Sell", 7, "Cover", "LMT")])
chk("部位 TMF、沒有 mix", (p["product"], "mix" in p, "mix0" in p), ("TMF", False, False))
n0 = len(F.sent)
ok, err = broker.close("sl")
chk("平倉：微台 7 口一張", (ok, S(F, n0)), (True, [("TMF", "Sell", 7, "Cover", "MKP")]))
chk("成績單不寫 product（＝微台，跟以前逐欄位相同）", ["product" in t for t in trades()], [False])
F = fresh(mini=False)
F.hold, F.px = {"MXF": 2, "TMF": 1}, {"MXF": 38000.0, "TMF": 38001.0}
got = broker.reconcile()
chk("開關沒開也看得到手上的混搭（重啟撿回，不然沒有停損）", (got or {}).get("product"), "MIX")
n0 = len(F.sent)
ok, err = broker.close("manual")
chk("開關沒開時手上的混搭照樣平得掉（小台 2＋微台 1）", (ok, S(F, n0)),
    (True, [("MXF", "Sell", 2, "Cover", "MKP"), ("TMF", "Sell", 1, "Cover", "MKP")]))

# ════════════════════════════════════════════════════════════════════════════
print("\n=== k. 日盤 13:43:30 收盤平倉面對混搭（auto_fire）===")
import auto_fire as AF      # noqa: E402
import night_fire as NF     # noqa: E402
import risk_cap             # noqa: E402
import cashflow             # noqa: E402
AF.ARM_FLAG = TMP / "AUTO_ORDERS_ON"
AF.FIRE_DIR = TMP / "autofire"
AF.FAST_HIST = TMP / "fh.jsonl"
AF.ORB_HIST = TMP / "orb.jsonl"
AF.TICK_DIR = TMP / "tick_logs"
NF.ARM_FLAG = TMP / "NIGHT_ORDERS_ON"
NF.NF_DIR = TMP / "nightfire"
NF.TSM_CTX = TMP / "tsm.json"
NF.TREND_CTX = TMP / "trend.json"
risk_cap.OVERRIDE_FLAG = TMP / "RISK_OVERRIDE"
_LOTS = {"q": 11}
AF.lots.read_state = lambda *a, **k: ({}, None)
AF.lots.day_lots = lambda st, d: _LOTS["q"]
AF.lots.cap_lots = lambda st, d: (10, 1)
AF.lots.effective_level = lambda st, d: 9
AF.configure(signal_at="09:03:30", signal_sec=32610, late_ms=5000, gap_s=3.0, sig_fn=lambda *a: None,
             dirs_fn=lambda *a: None, eod_at="13:43:30", pctl=80, rev_at="09:15:00", rev_sec=33300)
dd = str(date.today())


def af_open(qty=11, late=None):
    _LOTS["q"] = qty
    F = fresh()
    if late:
        F.late = dict(late)
    for f in list(AF.FIRE_DIR.glob("*")) if AF.FIRE_DIR.exists() else []:
        f.unlink()
    base = {"method": "U", "cand": "fast", "dir": "long", "leg": "fast", "sl_points": 200, "tp_points": 60}
    AF._send(dd, base, {"method": "U"}, "long", 38000.0, 200, 0, _rt.time(), {"quote_age_ms": 100}, "t",
             tp_points=60)
    return F


F = af_open()
ent, why = AF._auto_entry(dd)
chk("帳本那一列：等值 11、成交區間兩腿、有停利", (ent.get("qty"), ent.get("fill_lo"), ent.get("fill_hi"), ent.get("has_target")),
    (11, 38000.0, 38001.0, True))
chk("認得是自己的（_looks_ours）", AF._looks_ours(broker.reconcile(), ent), (True, None))
chk("還沒平（_already_closed＝open）", AF._already_closed(ent)[0], "open")
n0 = len(F.sent)
AF._eod(dd, 0, _rt.time(), "13:43:30")
last = AF._ST["last"]
chk("收盤平倉成功", last.get("why"), "eod_closed")
chk("送單：小台 2＋微台 1 平倉", S(F, n0), [("MXF", "Sell", 2, "Cover", "MKP"), ("TMF", "Sell", 1, "Cover", "MKP")])
tr = trades()
chk("成績單一筆 eod、11 口、MIX", [(t["reason"], t["qty"], t["product"]) for t in tr], [("eod", 11, "MIX")])
chk("券商已實現是兩種合約（小台 2 列＋微台 1 列）", sorted(r["product"] for r in broker._realized_day(dd)),
    ["MXF", "MXF", "TMF"])
chk("平完之後 _already_closed＝closed（兩種合約的已實現加起來剛好 11）", AF._already_closed(ent)[0], "closed")
# 小台停利先成交、剩微台 ⇒ 已實現只有小台那兩列（10 口）⇒ ⛔ 不可以被當成「已經平過」
F = af_open()
F.fill_target("MXF", 2)
broker.reconcile()
ent, why = AF._auto_entry(dd)
chk("小台停利成交後：已實現 10 口 < 11 ⇒ 還是 open（⛔ 不誤判已經平過）", AF._already_closed(ent)[0], "open")
chk("剩下的微台 1 口認得是自己的", AF._looks_ours(broker.reconcile(), ent)[0], True)
n0 = len(F.sent)
AF._eod(dd, 0, _rt.time(), "13:43:30")
chk("收盤只平微台 1 口", (AF._ST["last"].get("why"), S(F, n0)), ("eod_closed", [("TMF", "Sell", 1, "Cover", "MKP")]))
tr = trades()
chk("成績單一筆 11 口（10 口 tp＋1 口 eod）", [(t["qty"], [(l["qty"], l["src"]) for l in t["legs"]]) for t in tr],
    [(11, [(10, "tp"), (1, "eod")])])
# 重啟後（記憶體沒了）撿回的混搭也認得
F = af_open()
connect(F)
AF._MEM.clear() if hasattr(AF, "_MEM") else None
got = broker.reconcile()
ent, why = AF._auto_entry(dd)
chk("重啟撿回的混搭也認得是自己的", AF._looks_ours(got, ent)[0], True)
# 進場時小台腿晚 6 秒回報 ⇒ 帳本只記到微台 1 口（fill_unsure）；收盤前已經併成混搭 ⇒ 13:43:30 照樣認得、兩腿都平
F = af_open(late={"MXF": 6.0})
ent, why = AF._auto_entry(dd)
chk("小台腿晚到：帳本記 1 口、fill_unsure、送了 11", (ent.get("qty"), ent.get("fill_unsure"), ent.get("qty_sent")), (1, True, 11))
CLK.t = F._late["MXF"][0] + 1
got = broker.reconcile()
chk("  併成 MIX 11", (got["product"], got["qty"]), ("MIX", 11))
chk("  認得是自己的（比帳本那個價 entry0）", AF._looks_ours(got, ent), (True, None))
n0 = len(F.sent)
AF._eod(dd, 0, _rt.time(), "13:43:30")
chk("  收盤平倉兩腿都平", (AF._ST["last"].get("why"), S(F, n0)),
    ("eod_closed", [("MXF", "Sell", 2, "Cover", "MKP"), ("TMF", "Sell", 1, "Cover", "MKP")]))
chk("  平完 _already_closed＝closed（成績單 entry＝帳本的價）", AF._already_closed(ent)[0], "closed")
st = risk_cap.state(month=dd[:7], fire_dir=AF.FIRE_DIR, nf_dir=TMP / "__none_nf", trade_dir=broker.TRADE_DIR, lots=(10, 1))
chk("  風控對得到這一趟（11 口）", [x["q"] for x in st["trades"]], [11])
chk("  ⛔ 沒有平過頭", (F.bad, F.eq()), ([], {}))
src = (HERE / "auto_fire.py").read_text(encoding="utf-8")
say("place_order" not in src and "FuturesOrder" not in src, "⛔ auto_fire 裡沒有 place_order／FuturesOrder")

print("\n=== l. 夜盤 04:58 平倉面對混搭（night_fire）===")
E = date(2026, 6, 10)
NF.ARM_FLAG.parent.mkdir(parents=True, exist_ok=True)
NF.ARM_FLAG.write_text("R", encoding="ascii")
NF.TREND_CTX.write_text(json.dumps({"sig": {str(E - timedelta(days=i + 1)): (100.0 if i % 2 else -50.0)
                                            for i in range(40)}}), encoding="utf-8")
MC = {21 * 60 + 9: 37800.0, 21 * 60 + 39: 38000.0}
NF.configure(quote_fn=lambda: (38000.0, 1.0), session_fn=lambda now: "night", minute_close_fn=lambda m: MC.get(m))
NF.lots.read_state = lambda *a, **k: ({}, None)
NF.lots.cap_lots = lambda st, E: (10, 3)
NF.lots.night_plan = lambda E, st: {"qty": 13, "b": 3, "vol": None, "thr": None, "high": None, "last": None,
                                    "level": 9, "rule": None, "why": "test"}
_rb = risk_cap.blocked
risk_cap.blocked = lambda *a, **k: (False, "", {})
F = fresh()
NF._ST["tried"] = None
NF._decide(E, NF.trend_at(E) + timedelta(seconds=1))
p = pos()
chk("夜盤 13 口 ⇒ 混搭 2 小台＋3 微台、不設停利", (p and p["product"], p and p["mix"], p and p.get("no_tp")),
    ("MIX", {"MXF": 10, "TMF": 3}, True))
chk("送單：小台買 2、微台買 3（沒有停利單）", S(F), [("MXF", "Buy", 2, "New", "MKP"), ("TMF", "Buy", 3, "New", "MKP")])
ent = NF._entry_of(NF.rows_of(E))
chk("夜盤帳本那一列 13 口、成交區間", (ent.get("qty"), ent.get("fill_lo"), ent.get("fill_hi")), (13, 38000.0, 38001.0))
chk("04:58 認得是自己的", NF.looks_ours(broker.reconcile(), ent), True)
n0 = len(F.sent)
NF._ST["eod_try_at"] = 0.0
done = NF._eod(E, datetime(2026, 6, 11, 4, 58, 1))
chk("04:58 平倉完成", (done, [o.get("why") for o in NF.rows_of(E) if o.get("rec") == "eod"]), (True, ["closed"]))
chk("送單：小台賣 2＋微台賣 3 平倉", S(F, n0), [("MXF", "Sell", 2, "Cover", "MKP"), ("TMF", "Sell", 3, "Cover", "MKP")])
chk("成績單 night_eod、13 口", [(t["reason"], t["qty"], t.get("product")) for t in trades()],
    [("night_eod", 13, "MIX")])
# 成績單檔名是「真的今天」；夜盤那一晚 E 是假的日子 ⇒ 把那一列放到 E＋1 的檔（04:58 出場那天）再查
(broker.TRADE_DIR / ("%s.jsonl" % (E + timedelta(days=1)))).write_text(
    (broker.TRADE_DIR / ("%s.jsonl" % date.today())).read_text(encoding="utf-8"), encoding="utf-8")
chk("_trip_recorded 找得到今晚這一趟（加權進場價對得上帳本）", bool(NF._trip_recorded(E, ent)), True)
risk_cap.blocked = _rb

print("\n=== m. 成績單與風控：點數 × 等值口數 × 10 元 ===")
F = af_open()
F.mkt = {"TMF": 37901.0, "MXF": 37900.0}
AF._eod(dd, 0, _rt.time(), "13:43:30")
t = trades()[-1]
ent_px = round((38000 * 10 + 38001) / 11, 2)
xp = round((37900 * 10 + 37901) / 11, 2)
chk("成績單：每口點數＝加權出場−加權進場", t["points"], round(xp - ent_px, 1))
st = risk_cap.state(month=dd[:7], fire_dir=AF.FIRE_DIR, nf_dir=TMP / "__none_nf", trade_dir=broker.TRADE_DIR,
                    lots=(10, 1))
chk("風控：那一趟總點數＝每口點數 × 11", [(x["q"], x["pts"]) for x in st["trades"]], [(11, round(t["points"] * 11, 1))])
chk("交易金額＝點數 × 等值口數 × 10 元", round(t["points"] * t["qty"] * cashflow.PT_VALUE),
    round(round(xp - ent_px, 1) * 11 * 10))
tax = sum(round(t[k] * 10 * cashflow.TAX_RATE) for k in ("entry", "exit"))
chk("手續費估算：小台 2 口×25×2＋微台 1 口×18×2＋稅（等值 11 口）", cashflow.est_fee(t), 2 * 25 * 2 + 1 * 18 * 2 + 11 * tax)
chk("舊紀錄（沒有 product）照舊：每等值口 18×2＋稅", cashflow.est_fee({"qty": 3, "entry": 38000, "exit": 38100}),
    3 * (36 + round(38000 * 10 * cashflow.TAX_RATE) + round(38100 * 10 * cashflow.TAX_RATE)))
chk("整筆小台 10 口等值＝2 口合約", cashflow.est_fee({"qty": 10, "product": "MXF", "entry": 38000, "exit": 38100}),
    2 * 25 * 2 + 10 * (round(38000 * 10 * cashflow.TAX_RATE) + round(38100 * 10 * cashflow.TAX_RATE)))

# ── 收尾：⛔ 真的開關檔一個都沒被建出來／刪掉
chk("⛔ tools/shioaji 底下的真開關檔跟開跑前一樣（沒建也沒刪）",
    {f: (HERE / f).exists() for f in _PRE}, _PRE)
print("\n%d 項通過" % N_OK[0])
print("全部通過 ✅" if not FAIL else "%d 項沒過 ❌：%s" % (len(FAIL), FAIL))
sys.exit(1 if FAIL else 0)
