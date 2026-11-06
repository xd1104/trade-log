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
HERE = pathlib.Path(os.environ["BROKER_DIR"])
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


