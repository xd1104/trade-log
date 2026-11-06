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
        self.ghost, self.ghost_until = [], 0.0   # 接下來幾次 list_positions 回這些「舊資料」（口數比實際多；只在 ghost_until 之前有效＝暫時的）

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
        if self.ghost and CLK.t <= self.ghost_until:
            return self.ghost.pop(0)
        self.ghost = []
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

    def ghost_more(self, k=1, until=10.0):
        """接下來 k 次 list_positions 回一份「某一腿多 1 口」的舊資料（模擬回報退回舊資料／一次暫時的錯資料）。"""
        rows = self._rows()
        if rows:
            r = rows[0]
            bumped = type("P", (), {"code": r.code, "quantity": r.quantity + 1, "direction": r.direction,
                                    "price": r.price})()
            self.ghost = [[bumped] + rows[1:] for _ in range(k)]
            self.ghost_until = CLK.t + until

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
    broker._RECENT_CLOSE["at"] = 0.0             # 每一項獨立：不讓上一項「剛平完」的記憶擋住這一項的撿回
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
# ⭐ 2026-11-06（PM 定）：「已取消＋明細 0」跟 main 的 _sent_dealt 一樣當成沒成交 ⇒ 同一輪補送（急跌時不能等 15 秒）
chk("送單：小台 2 一次；微台 1 同一輪送 CLOSE_TRIES 次", S(F, n0),
    [("MXF", "Sell", 2, "Cover", "MKP")] + [("TMF", "Sell", 1, "Cover", "MKP")] * broker.CLOSE_TRIES)
p = pos()
chk("部位留下微台 1 口（降成單一合約、mix0 還在）", (p["product"], p["qty"], "mix" in p, p["mix0"]),
    ("TMF", 1, False, {"MXF": 10, "TMF": 1}))
# 第三輪品管：帳本部位不再在部位上記 legs，平掉的那段在帳本（自家平倉單明細）裡
_bk = broker._own_book(p)
chk("平掉的小台那一段記在帳本（平倉單明細 10 口等值、37800）", (_bk["MXF"]["c"], _bk["MXF"]["cpq"] / 10), (10.0, 37800.0))
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

print("\n=== g2. 部分成交（狀態 OrderStatus.Filled，照模擬帳戶實測）＋部位回報慢 ⇒ 同一輪只補送剩下的、⛔ 不平過頭 ===")
for lag in (0.0, 6.0, 10.0):
    F = fresh()
    broker.enter("long", 38000, 60, sl_points=200, qty=13)          # 小台 2＋微台 3
    n0 = len(F.sent)
    _first = {"MXF": True, "TMF": True}

    def _pol(pr, r, F=F, _first=_first):
        if r["oc"] == "Cover" and _first.get(pr):
            _first[pr] = False
            return 1, F.mkt[pr]                                    # 第一張每一腿都只成交 1 口
        return r["q"], F.mkt[pr]
    F.policy = _pol
    F.lag, F._hist = lag, []
    ok, err = broker.close("sl")
    chk("lag=%g：平完（同一輪補送剩下的）" % lag, (ok, pos()), (True, None))
    chk("  送單：小台 2→補 1、微台 3→補 2（狀態 Filled 但明細只有 1 口 ⇒ 照明細量算）", S(F, n0),
        [("MXF", "Sell", 2, "Cover", "MKP"), ("TMF", "Sell", 3, "Cover", "MKP"),
         ("MXF", "Sell", 1, "Cover", "MKP"), ("TMF", "Sell", 2, "Cover", "MKP")])
    chk("  ⛔ 沒有平過頭、券商空手", (F.bad, F.eq()), ([], {}))
    chk("  成績單一筆 13 口", [t["qty"] for t in trades()], [13])


print("\n=== t. 帳本部位：券商部位只當確認用（第三輪品管：差異要持續才採信、程式的部位不轉手動）===")
F = fresh()
broker.enter("long", 38000, 60, sl_points=200, qty=11)
F.ghost_more(1)                                   # 一次舊資料：微台多 1 口
got = broker.reconcile()
chk("一次口數變多 ⇒ 口數照帳本（11）、記下差異但不採信", (got["qty"], bool(got.get("diff_susp"))), (11, True))
CLK.t += broker.CLOSE_COOLDOWN + 1
got = broker.reconcile()
chk("  下一次對帳回到正常 ⇒ 差異取消", (got["qty"], got.get("diff_susp")), (11, None))
n0 = len(F.sent)
ok, err = broker.close("sl")
chk("  停損照常兩腿都平", (ok, S(F, n0), F.bad), (True, [("MXF", "Sell", 2, "Cover", "MKP"), ("TMF", "Sell", 1, "Cover", "MKP")], []))

F = fresh()
broker.enter("long", 38000, 60, sl_points=200, qty=11)
F._apply("MXF", 1, 38020.0)                       # 券商上多出小台 1 口（程式明細沒有；老闆不會手動加碼 ⇒ 晚到／異常）
got = broker.reconcile()
chk("券商多 5 口等值：剛送過單（靜默期）⇒ 照帳本 11、不採信", (got["qty"], (got.get("diff_susp") or {}).get("wait")), (11, True))
CLK.t += broker.TOUCH_QUIET_S + 1
got = broker.reconcile()
chk("  過了靜默期：第一次看到 ⇒ 開始計時（還是 11）", (got["qty"], (got.get("diff_susp") or {}).get("since") is not None), (11, True))
CLK.t += 5
chk("  不到 CLOSE_COOLDOWN ⇒ 還是 11", broker.reconcile()["qty"], 11)
CLK.t += broker.CLOSE_COOLDOWN
got = broker.reconcile()
chk("  相隔 ≥ CLOSE_COOLDOWN 還一樣 ⇒ 當成程式晚到的成交併進來（16）、⛔ 不轉手動部位、大聲講",
    (got["qty"], got.get("handover"), "晚到的成交" in (broker._state["last_error"] or "")), (16, None, True))
n0 = len(F.sent)
ok, err = broker.close("sl")
chk("  停損照程式規則全部平掉（小台 3＋微台 1）", (ok, S(F, n0), F.eq(), F.bad),
    (True, [("MXF", "Sell", 3, "Cover", "MKP"), ("TMF", "Sell", 1, "Cover", "MKP")], {}, []))

F = fresh()
broker.enter("long", 38000, 60, sl_points=200, qty=11)
F.policy = lambda pr, r: (0, F.mkt[pr]) if (pr == "TMF" and r["oc"] == "Cover") else (r["q"], F.mkt[pr])
broker.close("sl")                                # 小台平掉、微台平不掉 ⇒ 部位留微台 1 口
F.policy = None
F.ghost_more(3)                                   # 剛平過倉：券商回報還停在舊的（比本機多）
got = broker.reconcile()
chk("剛送過平倉單 ⇒ 口數照帳本（1）、⛔ 不採信券商", (got["qty"], got.get("handover")), (1, None))
cool()
F.ghost = []
n0 = len(F.sent)
ok, err = broker.close("sl")
chk("  之後停損照常平剩下的微台", (ok, S(F, n0), F.bad), (True, [("TMF", "Sell", 1, "Cover", "MKP")], []))

print("  -- main 也有的路（單一合約微台多口，開關關著）--")
F = fresh(mini=False)
broker.enter("long", 38000, 60, sl_points=200, qty=3)
broker._LAST_CLOSE_SEND["at"] = 0.0
F.ghost_more(1)
got = broker.reconcile()
# ⭐ 第三輪品管：微台多口也是帳本部位 ⇒ 口數照帳本（3）、差異確認中、⛔ 不標 touched（main 原本會直接標）
chk("微台 3 口：一次舊資料比本機多 ⇒ 照帳本 3 口、⛔ 不標 touched", (got.get("touched"), got["qty"], bool(got.get("diff_susp"))),
    (None, 3, True))
CLK.t += broker.CLOSE_COOLDOWN + 1
got = broker.reconcile()
n0 = len(F.sent)
ok, err = broker.close("sl")
chk("  停損照常平 3 口", (ok, S(F, n0), F.bad), (True, [("TMF", "Sell", 3, "Cover", "MKP")], []))

print("  -- 補送前的確認：狀態先到（已取消＋明細 0）、但券商部位已經變少 ⇒ ⛔ 不補送 --")
F = fresh()
broker.enter("long", 38000, 60, sl_points=200, qty=11)
_o = F.place_order


def _po(c, o, F=F, _o=_o):
    t = _o(c, o)
    if str(o.octype).endswith("Cover") and str(o.price_type).endswith("MKP") and CODE2P[c.code] == "MXF" \
            and t.status.deals:
        t.hidden = (t.status.deals, t.status.status, 50)   # 明細很久以後才到
        t.status.deals, t.status.status = [], "OrderStatus.Cancelled"
    return t


F.place_order = _po
F.policy = lambda pr, r: (1, F.mkt[pr]) if (pr == "MXF" and r["oc"] == "Cover") else (r["q"], F.mkt[pr])
n0 = len(F.sent)
ok, err = broker.close("sl")
chk("小台送 2 口實際成交 1 口、明細沒到、狀態已取消；券商部位已經剩 1 口 ⇒ 同一輪 ⛔ 不補送",
    [r for r in S(F, n0) if r[0] == "MXF"], [("MXF", "Sell", 2, "Cover", "MKP")])
chk("  ⛔ 沒有平過頭", F.bad, [])


print("\n=== h. 平倉前券商已經少了一腿（小台不見了，程式明細沒有）⇒ 只平看得到的那腿；少的那腿要持續確認才記成別處平掉 ===")
F = fresh()
broker.enter("long", 38000, 60, sl_points=200, qty=11)
F._apply("MXF", -2, 37900.0)                      # 小台 2 口在券商上不見了（程式明細沒有這張平倉單）
n0 = len(F.sent)
ok, err = broker.close("manual")
# 第三輪品管規則 ②：券商比我們少 ⇒ 確認前照明細行事（⛔ 不記別處平掉）；送單 ＝ min(券商, 明細) ⇒ 小台送 0
chk("只送微台 1 口平倉；小台照明細還在（確認中）⇒ 回報沒平完", (ok, S(F, n0), pos()["qty"]),
    (False, [("TMF", "Sell", 1, "Cover", "MKP")], 10))
chk("兩張停利都撤（小台那張還掛著，不撤會變反向新倉）", sorted(F.cancels), ["MXF", "TMF"])
CLK.t += broker.LATE_LEG_S + 1                    # 那一腿券商從沒顯示過 ⇒ 等 LATE_LEG_S（可能是晚到的腿）
broker.reconcile()
CLK.t += 3                                        # 持續＝至少 DIFF_CONFIRM_N 次對帳都一樣＋相隔 ≥ CLOSE_COOLDOWN
broker.reconcile()
CLK.t += broker.CLOSE_COOLDOWN + 1
got = broker.reconcile()
tr = trades()
chk("持續確認之後 ⇒ 小台那段記成別處平掉、整趟一筆 11 口、出場價留白（⛔ 不猜）",
    (got, [(t["qty"], t["exit"], [l["src"] for l in t["legs"]]) for t in tr]), (None, [(11, None, ["elsewhere", "manual"])]))
chk("⛔ 沒有平過頭", (F.bad, F.eq()), ([], {}))

print("\n=== i. 重啟撿回與對帳 ===")
F = fresh()
F.hold, F.px = {"MXF": 2, "TMF": 1}, {"MXF": 38000.0, "TMF": 38001.0}
got = broker.reconcile()
chk("券商同方向混搭 ⇒ 撿回成 MIX", (got["product"], got["qty"], got["mix"], got["recovered"]),
    ("MIX", 11, {"MXF": 10, "TMF": 1}, True))
chk("撿回的進場價＝加權", got["entry"], round((38000 * 10 + 38001) / 11, 2))
n0 = len(F.sent)
ok, err = broker.close("sl")
chk("⭐ 第三輪品管 #3：撿回來（沒有進場明細）還沒持續確認 ⇒ ⛔ 不准一次送整筆小台／混搭", (ok, len(F.sent) - n0), (False, 0))
CLK.t += broker.CLOSE_COOLDOWN + 1
broker.reconcile()
cool()
n0 = len(F.sent)
ok, err = broker.close("sl")
chk("  相隔 ≥ CLOSE_COOLDOWN 兩次對帳都一樣 ⇒ 才平（小台 2＋微台 1）", (ok, S(F, n0)),
    (True, [("MXF", "Sell", 2, "Cover", "MKP"), ("TMF", "Sell", 1, "Cover", "MKP")]))
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
chk("本機 MIX、券商只剩微台 ⇒ 確認前照帳本（11）", got["qty"], 11)
CLK.t += broker.LATE_LEG_S + 1
broker.reconcile()
CLK.t += 3                                        # 持續＝至少 DIFF_CONFIRM_N 次對帳都一樣＋相隔 ≥ CLOSE_COOLDOWN
broker.reconcile()
CLK.t += broker.CLOSE_COOLDOWN + 1
got = broker.reconcile()
chk("  持續確認之後 ⇒ 小台那腿記成別處平掉、剩 TMF 1 口", (got["product"], got["qty"], "mix" in got,
                                                     [a["src"] for a in got.get("own_adj") or []]), ("TMF", 1, False, ["elsewhere"]))
F = fresh()
broker.enter("long", 38000, None, sl_points=200, qty=3)
chk("（3 口 ⇒ 微台）", pos()["product"], "TMF")
F._apply("MXF", 1, 38000.0)
# ⭐ 第二輪品管（PM 拍板）：分得出我們每一腿的上限（進場是微台 3 口）⇒ ⛔ 不再 unknown；多出來的小台先記懷疑，平倉只平我們的
got = broker.reconcile()
# ⭐ 第三輪品管：微台多口也是帳本部位 ⇒ 多出來的小台還沒確認 ⇒ 照帳本（微台 3）、記下差異
chk("本機 TMF 3、券商多了小台 1 口 ⇒ 照帳本微台 3 口、差異確認中",
    (got.get("product"), got["qty"], bool(got.get("diff_susp"))), ("TMF", 3, True))
n0 = len(F.sent)
ok, err = broker.close("sl")
chk("  停損只平帳本的微台 3 口、⛔ 不碰沒確認的小台；這一趟結束（⛔ 不轉手動部位）", (ok, S(F, n0), F.hold, pos()),
    (True, [("TMF", "Sell", 3, "Cover", "MKP")], {"TMF": 0, "MXF": 1}, None))
F = fresh()
broker.enter("long", 38000, None, sl_points=200, qty=11)
F._apply("MXF", 1, 38000.0)
F._apply("TMF", -1, 38000.0)
got = broker.reconcile()
chk("本機 MIX、券商一腿變多一腿變少 ⇒ 確認前照帳本 11", got["qty"], 11)
CLK.t += broker.LATE_LEG_S + 1
broker.reconcile()
CLK.t += 3                                        # 持續＝至少 DIFF_CONFIRM_N 次對帳都一樣＋相隔 ≥ CLOSE_COOLDOWN
broker.reconcile()
CLK.t += broker.CLOSE_COOLDOWN + 1
got = broker.reconcile()
chk("  持續確認之後 ⇒ 小台多的併進來（晚到）、微台少的記別處平掉 ⇒ 小台 15", (got.get("product"), got["qty"]), ("MXF", 15))
n0 = len(F.sent)
ok, err = broker.close("sl")
chk("  停損照程式規則全部平掉（第三輪品管 #3：程式的部位不轉手動）", (ok, S(F, n0), F.hold, F.bad),
    (True, [("MXF", "Sell", 3, "Cover", "MKP")], {"TMF": 0, "MXF": 0}, []))
# 重啟：落地的帳本 ⇒ 補回混搭狀態
F = fresh()
broker.enter("long", 38000, 60, sl_points=200, qty=11)
connect(F)                                        # 模擬重啟：記憶體清空
got = broker.reconcile()
chk("重啟後從落地的帳本補回混搭（entry_qty、mix0、進場價）",
    (got["product"], got["mix"], got.get("entry_qty"), got.get("mix0"), got.get("sl_src")),
    ("MIX", {"MXF": 10, "TMF": 1}, 11, {"MXF": 10, "TMF": 1}, "meta"))
connect(F)
F.fill_target("MXF", 2)                           # 停機期間小台那一腿停利成交
got = broker.reconcile()
chk("重啟時小台那腿已經停利 ⇒ 用委託單編號問回停利單明細 ⇒ 帳本剩 TMF 1（跟券商一致、不用等確認）",
    (got["product"], got["qty"], "mix" in got, got.get("diff_susp")), ("TMF", 1, False, None))
F = fresh()
broker.enter("long", 38000, 60, sl_points=200, qty=13)
connect(F)
F.fill_target("MXF", 2)                           # 停機期間小台那一腿停利成交，剩微台 3 口
got = broker.reconcile()
chk("13 口混搭、停機期間小台停利 ⇒ 補回 TMF 3（照明細，直接對得上）", (got["product"], got["qty"], got.get("sl_src")),
    ("TMF", 3, "meta"))
n0 = len(F.sent)
ok, err = broker.close("sl")
chk("  停損只平微台 3 口（不碰小台）", (ok, S(F, n0)), (True, [("TMF", "Sell", 3, "Cover", "MKP")]))
tr = trades()
chk("  成績單一筆 13 口（小台 10 口停利＋微台 3 口停損）", [(t["qty"], [(l["qty"], l["src"]) for l in t["legs"]]) for t in tr],
    [(13, [(10, "tp"), (3, "sl")])])
chk("  ⛔ 沒有平過頭", F.bad, [])

print("\n=== j. 停利：兩張各自正確；一腿停利成交 ⇒ 縮量正確；平倉時兩張都撤 ===")
F = fresh()
broker.enter("long", 38000, 60, sl_points=200, qty=11)
F.fill_target("MXF", 2)
got = broker.reconcile()
chk("小台停利成交 ⇒ 剩微台 1 口", (got["product"], got["qty"]), ("TMF", 1))
_bk = broker._own_book(got)   # 第三輪品管：帳本部位不在部位上記 legs，停利那段在帳本（停利單明細）
chk("那一段在帳本：停利單明細 10 口等值、價錢＝停利價", (_bk["MXF"]["t"], _bk["MXF"]["tpq"] / 10), (10.0, 38060.0))
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

print("\n=== p. 進場一腿晚回報（帳本部位：我們的口數以進場單明細為準；券商晚回報只是確認）===")
W11 = round((38000 * 10 + 38001) / 11, 2)


def late_case(prod, secs, qty=11, d="long", tp=60):
    F = fresh()
    F.late = {prod: secs}
    ok, err, p = broker.enter(d, 38000, tp, sl_points=200, qty=qty)
    return F, ok, p


for secs in (6.0, 20.0):
    F, ok, p = late_case("MXF", secs)
    chk("小台腿晚 %g 秒：進場成功、記下送出去的兩腿與兩張進場單" % secs,
        (ok, p.get("own"), p["mix_sent"], sorted(p["mix_sent_ids"])), (True, True, {"MXF": 10, "TMF": 1}, ["MXF", "TMF"]))
    got = broker.reconcile()
    chk("  第一次對帳：帳本照進場單明細 ＝ MIX 11（券商還沒回報小台 ⇒ 等確認、⛔ 不當成少了）",
        (got["product"], got["qty"], got["mix"], bool(got.get("diff_susp"))), ("MIX", 11, {"MXF": 10, "TMF": 1}, True))
    CLK.t = F._late["MXF"][0] + 1
    got = broker.reconcile()
    chk("  回報到了 ⇒ 跟帳本一致、差異取消", (got["qty"], got.get("diff_susp")), (11, None))
    chk("  進場價＝兩腿加權", got["entry"], W11)
n0 = len(F.sent)
F.mkt = {"TMF": 37801.0, "MXF": 37800.0}
ok, err = broker.close("sl")
chk("停損：兩腿都平、撤停利（晚到的小台當初沒掛停利，只有微台那張）", (ok, S(F, n0), sorted(F.cancels)),
    (True, [("MXF", "Sell", 2, "Cover", "MKP"), ("TMF", "Sell", 1, "Cover", "MKP")], ["TMF"]))
chk("  ⛔ 沒有平過頭、空手", (F.bad, F.eq(), pos()), ([], {}, None))
t = trades()[-1]
xp = round((37800 * 10 + 37801) / 11, 2)
chk("  成績單：11 口、entry＝帳本當初的價（認人用）、entry_w＝兩腿加權、點數用加權算", (t["qty"], t["entry"], t.get("entry_w"), t["points"]),
    (11, 38001.0, W11, round(xp - W11, 1)))

F, ok, p = late_case("TMF", 6.0)
got = broker.reconcile()
chk("微台腿晚到：帳本照明細就是 MIX 11", (got["product"], got["qty"]), ("MIX", 11))
n0 = len(F.sent)
ok, err = broker.close("sl")
chk("  還沒回報就停損 ⇒ 只送看得到的小台 2 口（送單＝min(券商, 帳本)）、微台留著", (ok, S(F, n0), pos()["qty"]),
    (False, [("MXF", "Sell", 2, "Cover", "MKP")], 1))
CLK.t = F._late["TMF"][0] + 1
cool()
n0 = len(F.sent)
ok, err = broker.close("sl")
chk("  回報之後 ⇒ 平微台 1 口、⛔ 不平過頭", (ok, S(F, n0), F.bad, F.eq()), (True, [("TMF", "Sell", 1, "Cover", "MKP")], [], {}))
chk("  整趟一筆成績單 11 口", [(t["qty"], t.get("product")) for t in trades()], [(11, "MIX")])

F = fresh()
F.policy = lambda pr, r: (0, F.mkt[pr]) if (pr == "MXF" and r["oc"] == "New") else (r["q"], F.mkt[pr])
broker.enter("long", 38000, 60, sl_points=200, qty=11)
F.policy = None
F._apply("MXF", 3, 38000.0)                       # 券商上冒出小台 3 口（進場單明細是 0）
got = broker.reconcile()
chk("小台 IOC 沒撮到、券商卻冒出小台 3 口 ⇒ 確認前照帳本（微台 1）", (got["product"], got["qty"]), ("TMF", 1))
n0 = len(F.sent)
ok, err = broker.close("sl")
chk("  這時停損 ⇒ 只平帳本的微台 1 口（⛔ 不碰沒確認的小台）", (S(F, n0), F.hold), ([("TMF", "Sell", 1, "Cover", "MKP")],
                                                                    {"TMF": 0, "MXF": 3}))
F = fresh()
F.late = {"MXF": 6.0}
broker.enter("short", 38000, 60, sl_points=200, qty=11)
F._apply("TMF", 1, 38001.0)                       # 微台空單在券商上不見了
CLK.t = F._late["MXF"][0] + 1
F._apply("MXF", 4, 38000.0)                       # 小台：晚到的空 2 口＋多 4 口 ⇒ 券商上是多 2 口（反方向）
broker.reconcile()
CLK.t += broker.LATE_LEG_S + 1
broker.reconcile()
CLK.t += 3                                        # 持續＝至少 DIFF_CONFIRM_N 次對帳都一樣＋相隔 ≥ CLOSE_COOLDOWN
broker.reconcile()
CLK.t += broker.CLOSE_COOLDOWN + 1
got = broker.reconcile()
chk("⭐ 第三輪品管 #4：確認過券商是反方向 ⇒ 這一趟收尾、改成反向殘單（照手動停損、收盤不碰）",
    (got.get("dir"), got.get("qty"), got.get("residual"), "反向殘單" in (got.get("sl_warn") or "")), ("long", 10, True, True))
chk("  原本那一趟有成績單", len(trades()), 1)
n0 = len(F.sent)
ok, err = broker.close("sl")
chk("  反向殘單停損照平（賣 2 口小台）", (ok, S(F, n0), F.eq()), (True, [("MXF", "Sell", 2, "Cover", "MKP")], {}))

print("\n  -- 晚到期間重啟（落地的帳本補回；問不回委託單也照落地時的明細）--")
for nolist in (False, True):
    F, ok, p = late_case("MXF", 20.0)
    connect(F)                                    # 重啟：記憶體清空（兩張進場單物件也沒了）
    F.no_list_trades = nolist
    got = broker.reconcile()
    chk("重啟（%s）⇒ 從落地的帳本補回 MIX 11（券商還沒回報小台也一樣）" % ("問不回委託單" if nolist else "問得回委託單"),
        (got.get("product"), got.get("qty"), got.get("sl_src"), got.get("own")), ("MIX", 11, "meta", True))
    CLK.t = F._late["MXF"][0] + 1
    broker.reconcile()
    cool()
    n0 = len(F.sent)
    ok, err = broker.close("sl")
    chk("  回報後停損兩腿都平、⛔ 不平過頭", (ok, S(F, n0), F.bad),
        (True, [("MXF", "Sell", 2, "Cover", "MKP"), ("TMF", "Sell", 1, "Cover", "MKP")], []))
    chk("  整趟一筆 11 口", [t["qty"] for t in trades()], [11])

print("\n  -- 平倉單明細晚 10～20 秒才回報（模擬帳戶實測 B：小台平倉單的狀態／明細慢過 FILL_WAIT）--")
for delay in (10.0, 20.0):
    F = fresh()
    broker.enter("short", 38000, 60, sl_points=200, qty=13)
    _o = F.place_order

    def _po(c, o, F=F, _o=_o, delay=delay):
        t = _o(c, o)
        if str(o.octype).endswith("Cover") and str(o.price_type).endswith("MKP") and CODE2P[c.code] == "MXF":
            t.hidden = (t.status.deals, t.status.status, None)
            t.status.deals, t.status.status = [], "OrderStatus.Submitted"
            _HID.append((t, CLK.t + delay))
        return t

    _HID = []
    _us = F.update_status

    def _upd(acc=None, _us=_us):
        for t, at in list(_HID):
            if CLK.t >= at and t.hidden is not None:
                t.status.deals, t.status.status, t.hidden = t.hidden[0], t.hidden[1], None
                _HID.remove((t, at))
        _us(acc)

    F.place_order, F.update_status = _po, _upd
    F.mkt = {"TMF": 38201.0, "MXF": 38200.0}
    ok, err = broker.close("sl")
    chk("明細晚 %g 秒：第一次停損 ⇒ 微台平掉、小台平倉單送出去了但還沒回報 ⇒ ⛔ 不再送、回報沒平完" % delay,
        (ok, [s[:3] for s in S(F)[-2:]], "還沒回報" in (err or "")), (False, [("MXF", "Buy", 2), ("TMF", "Buy", 3)], True))
    cool()
    ok, err = broker.close("sl")
    chk("  冷卻後第二次停損：明細到了 ⇒ 結算、回「平完」（⛔ 不是「沒有部位」）", (ok, pos()), (True, None))
    ok, err = broker.close("sl")
    chk("  再按一次 ⇒ 回「已經平完了」（不是「沒有部位」）", (ok, "已經平完" in (err or "")), (True, True))
    t = trades()[-1]
    chk("  成績單一筆 13 口、小台那段有出場價（⛔ 不是別處平掉／留白）", (t["qty"], t["exit"] is not None,
                                                       [l["src"] for l in t.get("legs") or []]), (13, True, []))
    chk("  ⛔ 沒有平過頭", (F.bad, F.eq()), ([], {}))

print("\n=== q. 第四輪品管（2026-11-06）：晚到的腿留著程式停損、採信過的晚到跟之後的明細互相抵銷 ===")


def _hide_new(F, prod, secs):
    """那一種合約的進場單（New／MKP）狀態＋明細一起晚 secs 秒才回報（假時鐘）。"""
    hid = []
    _o, _us = F.place_order, F.update_status

    def _po(c, o):
        t = _o(c, o)
        if str(o.octype).endswith("New") and str(o.price_type).endswith("MKP") and CODE2P[c.code] == prod:
            t.hidden = (t.status.deals, t.status.status, None)
            t.status.deals, t.status.status = [], "OrderStatus.Submitted"
            hid.append((t, CLK.t + secs))
        return t

    def _upd(acc=None):
        for t, at in list(hid):
            if CLK.t >= at and t.hidden is not None:
                t.status.deals, t.status.status, t.hidden = t.hidden[0], t.hidden[1], None
                hid.remove((t, at))
        _us(acc)

    F.place_order, F.update_status = _po, _upd


F = fresh()
F.late = {"MXF": 20.0}
_hide_new(F, "MXF", 20.0)
t0 = CLK.t
ok, err, p = broker.enter("long", 38000, 60, sl_points=200, qty=11)
chk("小台腿（部位＋明細）晚 20 秒：進場時只看得到微台 1 口", (ok, p["qty"], p["product"]), (True, 1, "TMF"))
CLK.t = t0 + 7
n0 = len(F.sent)
ok, err = broker.close("sl")
chk("  第 7 秒停損：先平看得到的微台 1 口、⛔ 不結算（小台那張進場單還沒結束）、講清楚",
    (ok, S(F, n0), pos() is not None, "還沒到" in (err or "")), (False, [("TMF", "Sell", 1, "Cover", "MKP")], True, True))
CLK.t = F._late["MXF"][0] + 1
got = broker.reconcile()
chk("  小台回報到了 ⇒ 還是我們的（有程式停損 200 點、不是撿回）",
    (got.get("product"), got.get("qty"), got.get("sl_points"), got.get("recovered"), got.get("own")),
    ("MXF", 10, 200.0, False, True))
cool()
n0 = len(F.sent)
ok, err = broker.close("sl")
chk("  再停損 ⇒ 平小台 2 口、空手、⛔ 沒平過頭", (ok, S(F, n0), F.eq(), F.bad, pos()),
    (True, [("MXF", "Sell", 2, "Cover", "MKP")], {}, [], None))
chk("  整趟一筆成績單 11 口（MIX）", [(t["qty"], t.get("product")) for t in trades()], [(11, "MIX")])

F = fresh()
F.policy = lambda pr, r: (0, F.mkt[pr]) if (pr == "MXF" and r["oc"] == "New") else (r["q"], F.mkt[pr])
broker.enter("long", 38000, 60, sl_points=200, qty=11)
F.policy = None
F._apply("MXF", 2, 38000.0)                       # 券商上出現小台 2 口（進場單明細說 0 成交、已取消）
CLK.t += broker.TOUCH_QUIET_S + 1
broker.reconcile()
CLK.t += 5
broker.reconcile()
CLK.t += broker.CLOSE_COOLDOWN
got = broker.reconcile()
chk("確認過券商多小台 10 口等值 ⇒ 採信成晚到（11）", (got["qty"], [a["q"] for a in got.get("own_adj") or []]), (11, [10]))
_mt = [x for x in Trade._live if x.contract.code == C["MXF"].code and str(x.status.status).endswith("Cancelled")][-1]
_mt.status.deals, _mt.status.status = [Deal(38000.0, 2)], "OrderStatus.Filled"   # 之後那張進場單的明細補到了
got = broker.reconcile()
chk("  ⭐ #4 之後明細補到 ⇒ 跟調整互相抵銷、⛔ 不重複算（還是 11，不是 21）", (got["qty"], got.get("mix")),
    (11, {"MXF": 10, "TMF": 1}))
n0 = len(F.sent)
F.mkt = {"TMF": 37901.0, "MXF": 37900.0}
ok, err = broker.close("sl")
chk("  停損平完：小台 2＋微台 1、空手、⛔ 沒平過頭", (ok, S(F, n0), F.eq(), F.bad),
    (True, [("MXF", "Sell", 2, "Cover", "MKP"), ("TMF", "Sell", 1, "Cover", "MKP")], {}, []))
t = trades()[-1]
chk("  成績單 11 口、點數＝實際（-100）", (len(trades()), t["qty"], t["points"]), (1, 11, -100.0))

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
        if rng.random() < 0.2:
            # ⭐ 2026-11-06（PM）：一兩次暫時的舊資料（口數比實際多）⇒ ⛔ 不可以變成 touched／平過頭
            F.ghost_more(rng.choice([1, 2]))
        if rng.random() < 0.25:
            # ⭐ 品管 M9：冷卻之後才開始回報慢 ⇒ 平倉一開始讀到的就是舊部位
            F.lag, F._hist = rng.choice([3.0, 6.0, 10.0]), []
        if rng.random() < 0.25 and isinstance(pos(), dict):
            # ⭐ 品管 M9：平倉前幾秒停利剛成交（部分或全部），部位還沒回報
            live = [(tp, t) for (tp, t, a, px) in F.targets if t.status.status in ("OrderStatus.Submitted", "OrderStatus.PartFilled")]
            if live:
                tp, t = rng.choice(live)
                left = t.order.quantity - sum(dd.quantity for dd in t.status.deals)
                if left > 0 and F.hold[tp] != 0:
                    F.fill_target(tp, rng.randint(1, min(left, abs(F.hold[tp]))))
            CLK.t += rng.choice([0.0, 1.0, 3.0])
        r = broker.close("sl")
        if pos() is None and not F.eq():
            break
        if isinstance(pos(), dict) and pos().get("handover"):
            # 我們的口數都平完了，剩的轉成他的手動部位（第二輪品管）⇒ 程式這一趟結束（手動部位的停損是 main 那條一口迴圈，不在這裡測）
            break
    F.lag, F.pos_fail, F.hide, F.ghost = 0, 0, None, []
    F.update_status()
    F.update_status()
    F._hist = []
    for _k in range(2):
        broker.reconcile()
    viol += [(it,) + tuple(b) for b in F.bad]
    p, eq = pos(), F.eq()
    if isinstance(p, dict) and p.get("touched"):
        incons.append((it, "⛔ 他沒有加碼卻被標成 touched（舊資料變成永久不停損）"))
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
# ⭐ 2026-11-06 品管 M3：開關關著、本機也沒有混搭開的部位 ⇒ 跟 main 一樣 unknown、一張都不送
chk("開關沒開、券商上兩種合約（不是程式開的）⇒ unknown（跟 main 一樣）", got, "unknown")
n0 = len(F.sent)
ok, err = broker.close("manual")
chk("  一張都不送", (ok, len(F.sent) - n0), (False, 0))
F = fresh()
broker.enter("long", 38000, 60, sl_points=200, qty=11)
broker.MINI_FLAG.unlink()                         # 開著時開了混搭，之後他把開關關掉
chk("開關被關掉，但本機部位是混搭開的 ⇒ 照樣認得", (broker.reconcile() or {}).get("product"), "MIX")
n0 = len(F.sent)
ok, err = broker.close("manual")
chk("  照樣平得掉（小台 2＋微台 1）", (ok, S(F, n0)),
    (True, [("MXF", "Sell", 2, "Cover", "MKP"), ("TMF", "Sell", 1, "Cover", "MKP")]))
F = fresh()
broker.enter("long", 38000, 60, sl_points=200, qty=11)
broker.MINI_FLAG.unlink()
connect(F, mini=False)                            # 開關關著＋重啟：落地檔是混搭開的 ⇒ 撿回成 MIX
got = broker.reconcile()
chk("開關關著＋重啟、落地檔是混搭開的 ⇒ 撿回 MIX、落地檔補回", (got.get("product"), got.get("sl_src")), ("MIX", "meta"))

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
# ⭐ 品管 M7：小台腿晚 40 秒、回報前就停損（只平掉微台）⇒ 部位留著晚到那一腿；回報之後 13:43:30 照樣認得、平掉
F = af_open(late={"MXF": 40.0})
ok, err = broker.close("sl")
# 第三輪品管：帳本照進場單明細 ⇒ 部位留著小台 10 口等值（等券商回報才送）
chk("M7 日盤：停損時只看得到微台 ⇒ 平掉微台、部位留著晚到的小台 10 口等值",
    (ok, (pos() or {}).get("product"), (pos() or {}).get("qty")), (False, "MXF", 10))
CLK.t = F._late["MXF"][0] + 1
broker.reconcile()
ent, why = AF._auto_entry(dd)
chk("  回報之後：還沒平過（已實現只有微台 1 口 < 11）", AF._already_closed(ent)[0], "open")
chk("  13:43:30 認得是自己的", AF._looks_ours(pos(), ent), (True, None))
n0 = len(F.sent)
AF._eod(dd, 0, _rt.time(), "13:43:30")
chk("  收盤平掉小台 2 口", (AF._ST["last"].get("why"), S(F, n0)), ("eod_closed", [("MXF", "Sell", 2, "Cover", "MKP")]))
# 帳本部位的成績單：兩次平倉（sl＋eod）都是自家平倉單 ⇒ 合成最後一段（出場價依明細加權），理由記最後那次
chk("  整趟一筆成績單 11 口（eod）、券商空手、⛔ 沒平過頭",
    ([(t["qty"], t["reason"], t["exit"] is not None) for t in trades()], F.eq(), F.bad),
    ([(11, "eod", True)], {}, []))
# ⭐ 第五輪品管 (c)：小台腿（部位＋明細）晚 20 秒、第 7 秒停損 ⇒ 空殼（口數 0）；剛好碰上 13:43:30 收盤
#    ⇒ ⛔ 不可以判成 eod_not_ours 定案；窗口內等那一腿進來再平
_LOTS["q"] = 11
F = fresh()
F.late = {"MXF": 20.0}
_hide_new(F, "MXF", 20.0)
for f in list(AF.FIRE_DIR.glob("*")) if AF.FIRE_DIR.exists() else []:
    f.unlink()
AF._send(dd, {"method": "U", "cand": "fast", "dir": "long", "leg": "fast", "sl_points": 200, "tp_points": 60},
         {"method": "U"}, "long", 38000.0, 200, 0, _rt.time(), {"quote_age_ms": 100}, "t", tp_points=60)
CLK.t += 7
ok, err = broker.close("sl")
chk("空殼：停損平掉微台、小台那一腿還沒回報 ⇒ 部位口數 0（broker.own_shell）",
    (ok, (pos() or {}).get("qty"), broker.own_shell(pos())), (False, 0, True))
ent, why = AF._auto_entry(dd)
n0 = len(F.sent)
_aft = AF.time
AF.time = CLK                                     # 收盤那一段的等待用假時鐘（每次重試 EOD_RETRY_S 秒）
try:
    AF._eod(dd, 0, CLK.t, "13:43:30")
finally:
    AF.time = _aft
chk("  13:43:30 碰上空殼 ⇒ 等到小台那一腿進來、照樣認得、平掉（⛔ 不是 eod_not_ours）",
    (AF._ST["last"].get("why"), S(F, n0)), ("eod_closed", [("MXF", "Sell", 2, "Cover", "MKP")]))
chk("  整趟一筆成績單 11 口（eod）、券商空手、⛔ 沒平過頭",
    ([(t["qty"], t["reason"]) for t in trades()], F.eq(), F.bad), ([(11, "eod")], {}, []))

print("\n  -- 第五輪品管 (a)(b)：剛平完、券商回報慢還顯示舊部位 --")
F = fresh(mini=False)
broker.enter("long", 38000, 60, sl_points=200, qty=10)
F.lag = 6.0
broker.close("sl")
CLK.t += 1
chk("(a) 平完 1 秒、券商還顯示舊的 10 口 ⇒ 不撿回（沒有部位），但 can_enter 擋下",
    (broker.reconcile(), broker.can_enter(38000, True)), (None, (False, "剛平倉、券商部位還沒更新，稍後再進")))
F.policy = lambda pr, r: (4, F.mkt[pr]) if r["oc"] == "New" else (r["q"], F.mkt[pr])
ok, err, p = broker.enter("long", 38000, 60, sl_points=200, qty=10)
F.policy = None
chk("(b) 這時還是進場（只成交 4 口）⇒ 確認口數照明細 4（⛔ 不被舊的 10 口騙）", (ok, p["qty"], p["own_trades"][0]["conf"]), (True, 4, 4))
CLK.t += 1
n0 = len(F.sent)
ok, err = broker.close("sl")
chk("  停損只平 4 口、⛔ 沒平過頭", ([s for s in S(F, n0) if s[4] == "MKP"], F.bad), ([("TMF", "Sell", 4, "Cover", "MKP")], []))
F.lag, F._hist = 0, []
CLK.t += 10
chk("  券商更新成空手之後 can_enter 放行", broker.can_enter(38000, True), (True, None))
F = fresh(mini=False)
broker.enter("long", 38000, 60, sl_points=200, qty=10)
F.lag = 6.0
broker.close("sl")
CLK.t += 1
F.policy = lambda pr, r: (0, F.mkt[pr]) if r["oc"] == "New" else (r["q"], F.mkt[pr])
ok, err, p = broker.enter("long", 38000, 60, sl_points=200, qty=10)
F.policy = None
chk("(b) 新單 0 成交 ⇒ 沒部位；舊部位照樣不撿回", (ok, pos(), broker.reconcile()), (False, None, None))
CLK.t += 10
F.lag, F._hist = 0, []
chk("  ⛔ 沒有送任何平倉單、券商空手", ([s for s in S(F) if s[3] == "Cover" and s[4] == "MKP"][1:], F.eq()), ([], {}))

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
# ⭐ 品管 M7：夜盤 13 口、小台腿晚 40 秒、回報前就停損 ⇒ 04:58 照樣認得晚到那一腿、平掉
E2 = date(2026, 6, 17)
NF.TREND_CTX.write_text(json.dumps({"sig": {str(E2 - timedelta(days=i + 1)): (100.0 if i % 2 else -50.0)
                                            for i in range(40)}}), encoding="utf-8")
F = fresh()
F.late = {"MXF": 40.0}
NF._ST["tried"] = None
NF._decide(E2, NF.trend_at(E2) + timedelta(seconds=1))
chk("M7 夜盤：進場時只看得到微台 3 口", ((pos() or {}).get("product"), (pos() or {}).get("qty")), ("TMF", 3))
ok, err = broker.close("sl")
chk("  停損只平微台 3 口、部位留著晚到的小台 10 口等值", (ok, (pos() or {}).get("product"), (pos() or {}).get("qty")),
    (False, "MXF", 10))
CLK.t = F._late["MXF"][0] + 1
broker.reconcile()
ent2 = NF._entry_of(NF.rows_of(E2))
chk("  04:58 認得是自己的", NF.looks_ours(pos(), ent2), True)
n0 = len(F.sent)
NF._ST["eod_try_at"] = 0.0
NF._eod(E2, datetime(2026, 6, 18, 4, 58, 1))
chk("  04:58 平掉小台 2 口", ([o.get("why") for o in NF.rows_of(E2) if o.get("rec") == "eod"], S(F, n0)),
    (["closed"], [("MXF", "Sell", 2, "Cover", "MKP")]))
chk("  整趟一筆 13 口、券商空手、⛔ 沒平過頭", ([t["qty"] for t in trades()], F.eq(), F.bad), ([13], {}, []))
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
