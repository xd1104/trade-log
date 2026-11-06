# -*- coding: utf-8 -*-
"""
第三輪獨立驗收攻擊測試（lab-qa，2026-11-06，⛔ 不 commit）。
前提（老闆）：自動交易開下去之後他完全不會手動加碼 ⇒ 券商上的每一口都是程式的；
券商口數比程式多只會是資料延遲／晚到成交／異常。
沿用 test_qa_mix.py 的假券商／假時鐘（不連永豐、不碰 8770、開關檔全在暫存資料夾）。
"""
import importlib.util
import json
import pathlib
import random
import sys

HERE = pathlib.Path(__file__).resolve().parent
_src = (HERE / "test_qa_mix.py").read_text(encoding="utf-8")
_harness = _src.split("# ===================== QA attack scenarios")[0]
exec(compile(_harness, "test_qa_mix_harness", "exec"))   # noqa: S102

MAIN = HERE.parents[2] / "trade-log" / "tools" / "shioaji" / "broker.py"
_spec = importlib.util.spec_from_file_location("broker_main3", str(MAIN))
bm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bm)
bm.ORDER_DIR = TMP / "main_orders"
bm.TRADE_DIR = TMP / "main_trades"
bm.REAL_FLAG = TMP / "REAL_ORDERS_ON"
bm.MINI_FLAG = TMP / "MINI_ON"
bm.FILL_WAIT = 5.0
bm.MAX_ENTRIES = 99
bm.CA_OK["ok"] = True
bm.time = CLK
N_RAND = int(sys.argv[1]) if len(sys.argv) > 1 else 600


def tag(n):
    print("\n=== QA3-%s ===" % n)


class Fake3(Fake):
    """
    多幾種壞法：
      hide_t＝(機率, 秒數)：委託單「狀態＋明細」一起晚 N 秒（假時鐘）才回報（實測兩者同一次 update 回來）。
      revert＝機率：list_positions 偶爾退回一份之前的非空部位快照（舊資料）。
      tp_on_cancel＝機率：撤停利單那一瞬間它先成交 1 口。
    """
    def __init__(self, rng=None):
        super().__init__()
        self.rng = rng or random.Random(0)
        self.hide_t = None
        self.revert = 0.0
        self.snaps = []
        self.tp_on_cancel = 0.0
        self.new_fill = {"TMF": 0, "MXF": 0}       # 進場（New）實際成交的合約口數

    def place_order(self, c, o):
        t = super().place_order(c, o)
        rec = self.sent[-1]
        if rec["pt"] != "LMT" and rec["oc"] == "New":
            k = sum(d.quantity for d in (t.status.deals if t.hidden is None else t.hidden[0]))
            self.new_fill[rec["p"]] += k
        if rec["pt"] != "LMT" and self.hide_t and self.rng.random() < self.hide_t[0] and t.hidden is None:
            t.hidden = (t.status.deals, t.status.status, ("T", CLK.t + self.hide_t[1]))
            t.status.deals, t.status.status = [], "OrderStatus.Submitted"
        return t

    def update_status(self, acc=None):
        for t in list(Trade._live):
            if t.hidden is not None and isinstance(t.hidden[2], tuple):
                d, st, (_, at) = t.hidden
                if CLK.t >= at:
                    t.status.deals, t.status.status, t.hidden = d, st, None
        if any(t.hidden is not None and not isinstance(t.hidden[2], tuple) for t in Trade._live):
            super().update_status(acc)

    def release_all(self):
        for t in list(Trade._live):
            if t.hidden is not None:
                t.status.deals, t.status.status, t.hidden = t.hidden[0], t.hidden[1], None

    def _snap(self):
        super()._snap()
        r = self._rows()
        if r:
            self.snaps.append(r)

    def list_positions(self, acc=None):
        if self.revert and self.snaps and self.rng.random() < self.revert:
            return self.rng.choice(self.snaps)
        return super().list_positions(acc)

    def cancel_order(self, t):
        if self.tp_on_cancel and self.rng.random() < self.tp_on_cancel and t.status.status in (
                "OrderStatus.Submitted", "OrderStatus.PartFilled"):
            p = CODE2P[t.contract.code]
            left = t.order.quantity - sum(d.quantity for d in t.status.deals)
            if abs(self.hold[p]) >= 1 and left >= 1:
                self.fill_target(p, 1)
        return super().cancel_order(t)

    def realized_eqpts(self, sgn):
        return sum(sgn * (r.cover_price - r.entry_price) * broker.RATIO[CODE2P[r.code]] for r in self.pl)


def fresh3(mini=True, rng=None, mod=None):
    mod = mod or broker
    for d in (mod.ORDER_DIR, mod.TRADE_DIR):
        if d.exists():
            for f in d.glob("*"):
                f.unlink()
    F = Fake3(rng)
    mod.configure(F, C["TMF"], {"MXF": C["MXF"]})
    mod._state["position"] = None
    mod._state["last_error"] = None
    mod._close_fail["at"] = 0.0
    mod._CLOSE_INFLIGHT["on"] = False
    mod._REALIZED["at"] = 0.0
    mod._META.update(last=None, swept=False)
    if hasattr(mod, "_LAST_CLOSE_SEND"):
        mod._LAST_CLOSE_SEND["at"] = 0.0
    if mini:
        mod.MINI_FLAG.write_text("on", encoding="utf-8")
    elif mod.MINI_FLAG.exists():
        mod.MINI_FLAG.unlink()
    mod.REAL_FLAG.write_text("on", encoding="utf-8")
    return F


def trades_of(mod=broker):
    f = mod.TRADE_DIR / ("%s.jsonl" % date.today())
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []


def P(mod=broker):
    p = mod._state["position"]
    return None if p is None else {k: p.get(k) for k in ("product", "qty", "mix", "handover", "touched",
                                                         "multi_unknown", "late_pending", "recovered", "sl_src")}


# ---- 1. 停利剛成交 1 口微台＋部位回報慢 6 秒 ⇒ 停損
tag("1  MIX11：微台停利剛成交 1 口、部位回報慢 6 秒，立刻停損（他沒加碼）")
F = fresh3()
broker.enter("long", 38000, 60, sl_points=200, qty=11)
CLK.t += 30
F.lag = 6.0
F.fill_target("TMF", 1)
n0 = len(F.sent)
ok, err = broker.close("sl")
print("  close:", ok, (err or "")[:100], S(F, n0), "hold:", F.hold, "bad:", F.bad)
print("  close 之後部位：", P())
fake_hand_1 = bool((broker._state["position"] or {}).get("handover"))
CLK.t += 10
F.lag = 0
F._hist = []
broker.reconcile()
tr = trades_of()
print("  成績單：", [{k: t.get(k) for k in ("qty", "points", "reason", "product", "mix", "legs")} for t in tr])
chk("QA3-1 沒有平過頭", F.bad, [])
chk("QA3-1 券商最後空手", F.eq(), {})
chk("QA3-1 平完不會變出一筆假的「他的手動部位」（他沒加碼）", fake_hand_1, False)
chk("QA3-1 成績單只有一列（整趟）", len(tr), 1)
chk("QA3-1 成績單口數＝11（含停利那 1 口）", sum(int(t.get("qty") or 0) for t in tr), 11)
real = F.realized_eqpts(1)
got = sum((t.get("points") or 0) * int(t.get("qty") or 0) for t in tr)
say(all(t.get("points") is not None for t in tr) and abs(real - got) < 0.06 * 11 + 0.01,
    "QA3-1 成績單點數×口數＝券商實際損益點數（%.2f）" % real, got)

# ---- 1b. 小台停利成交 1 口（5 等值），回報慢 60 秒 ⇒ 假手動部位再被手動停損
tag("1b MIX11：小台停利剛成交 1 口、部位回報慢 60 秒；停損後若生出假手動部位 ⇒ 手動停損會不會平過頭")
F = fresh3()
broker.enter("long", 38000, 60, sl_points=200, qty=11)
CLK.t += 30
F.lag = 60.0
F.fill_target("MXF", 1)
n0 = len(F.sent)
ok, err = broker.close("sl")
print("  close:", ok, (err or "")[:100], S(F, n0), "hold:", F.hold)
print("  close 之後部位：", P())
CLK.t += 3
broker._LAST_RECONCILE["at"] = 0
broker.reconcile_tick()
print("  對帳後部位：", P())
n1 = len(F.sent)
for i in range(3):
    if broker._state["position"] is None:
        break
    ok2, err2 = broker.close("sl")
    print("  手動停損 #%d:" % (i + 1), ok2, (err2 or "")[:90], S(F, n1))
    CLK.t += broker.CLOSE_COOLDOWN + 1
chk("QA3-1b 沒有平過頭（含假手動部位被停損）", F.bad, [])
CLK.t += 60
F.lag = 0
F._hist = []
broker.reconcile()
tr = trades_of()
print("  成績單：", [{k: t.get(k) for k in ("qty", "points", "reason")} for t in tr])
chk("QA3-1b 成績單只有一列", len(tr), 1)

# ---- 1c. 小台停利成交、回報慢 6 秒
tag("1c MIX11：小台停利剛成交、回報慢 6 秒；停損後的部位與成績單")
F = fresh3()
broker.enter("long", 38000, 60, sl_points=200, qty=11)
CLK.t += 30
F.lag = 6.0
F.fill_target("MXF", 1)
ok, err = broker.close("sl")
print("  close:", ok, (err or "")[:100], "hold:", F.hold, "pos:", P())
fake_hand_1c = bool((broker._state["position"] or {}).get("handover"))
CLK.t += 10
F.lag = 0
F._hist = []
broker.reconcile()
tr = trades_of()
print("  成績單：", [{k: t.get(k) for k in ("qty", "points", "reason", "legs")} for t in tr])
chk("QA3-1c 沒有假手動部位", fake_hand_1c, False)
chk("QA3-1c 成績單一列 11 口", (len(tr), sum(int(t.get("qty") or 0) for t in tr)), (1, 11))

# ---- 2. 第一輪平倉小台部分成交、狀態＋明細一起晚 40 秒；微台沒撮到 ⇒ 第二輪
tag("2  MIX11 停損：小台 2 口只成交 1 口且回報（狀態＋明細）晚 40 秒、微台 IOC 沒撮到 ⇒ 冷卻後第二輪")
F = fresh3()
broker.enter("long", 38000, 60, sl_points=200, qty=11)
CLK.t += 30


def _pol2(pr, r):
    if r["oc"] != "Cover":
        return r["q"], F.mkt[pr]
    return (1 if pr == "MXF" else 0), F.mkt[pr]


F.policy = _pol2
F.hide_t = (1.0, 40.0)
_place = F.place_order


def _po(c, o, F=F):
    t = _place(c, o)
    if CODE2P[c.code] == "TMF" and t.hidden is not None:
        t.status.deals, t.status.status, t.hidden = t.hidden[0], t.hidden[1], None
    return t


F.place_order = _po
n0 = len(F.sent)
ok, err = broker.close("sl")
print("  round1:", ok, (err or "")[:120], S(F, n0), "hold:", F.hold, "pos:", P())
F.policy = None
CLK.t += broker.CLOSE_COOLDOWN + 1
n1 = len(F.sent)
ok, err = broker.close("sl")
print("  round2:", ok, (err or "")[:120], S(F, n1), "hold:", F.hold, "pos:", P())
ho = bool((broker._state["position"] or {}).get("handover"))
chk("QA3-2 沒有平過頭", F.bad, [])
chk("QA3-2 程式自己的小台 1 口沒被改判成他的手動部位（他沒加碼）", ho, False)
for i in range(4):
    if broker._state["position"] is None or (broker._state["position"] or {}).get("handover"):
        break
    CLK.t += broker.CLOSE_COOLDOWN + 1
    ok, err = broker.close("sl")
    print("  round%d:" % (i + 3), ok, (err or "")[:110], "hold:", F.hold, "pos:", P())
chk("QA3-2 最後程式的口數都平掉（券商空手）", F.eq(), {})

# ---- 3. 重啟時正在等晚到腿（QA2-6 延伸）⇒ 回報後停損 ⇒ 成績單
tag("3  微台腿晚到 20 秒、停損平掉小台（late_pending）⇒ 重啟 ⇒ 回報 ⇒ 再停損：成績單是否整趟")
F = fresh3()
F.late = {"TMF": 20.0}
broker.enter("long", 38000, 60, sl_points=200, qty=11)
ok, err = broker.close("sl")
broker._state["position"] = None
broker._META.update(last=None, swept=False)
broker.reconcile()
CLK.t = F._late["TMF"][0] + 1
broker.reconcile()
print("  回報後：", P())
CLK.t += broker.CLOSE_COOLDOWN + 1
ok, err = broker.close("sl")
print("  停損：", ok, (err or "")[:80], "hold:", F.hold)
tr = trades_of()
print("  成績單：", [{k: t.get(k) for k in ("qty", "points", "reason", "legs")} for t in tr])
chk("QA3-3 券商空手", F.eq(), {})
chk("QA3-3 成績單合計 11 口", sum(int(t.get("qty") or 0) for t in tr), 11)
say(all(t.get("points") is not None for t in tr), "QA3-3 成績單點數有值", [t.get("points") for t in tr])

# ---- 4. 開關關：舊資料退回 ⇒ branch vs main
tag("4  開關關（單一微台 3 口）：部位回報退回舊資料 ⇒ branch 與 main 行為比對")


def scen4(mod, kind):
    F = fresh3(mini=False, mod=mod)
    mod.enter("long", 38000, 60, sl_points=200, qty=3)
    CLK.t += 10
    F.fill_target("TMF", 1)
    mod.reconcile()
    if kind == "stale_high_once":
        F.revert, F.snaps = 1.0, [F.snaps[0]]
        CLK.t += 4
        mod.reconcile()
        F.revert = 0
        CLK.t += 4
        mod.reconcile()
    elif kind == "stale_high_20s":
        F.revert, F.snaps = 1.0, [F.snaps[0]]
        for _ in range(3):
            CLK.t += 9
            mod.reconcile()
        F.revert = 0
        CLK.t += 4
        mod.reconcile()
    elif kind == "stale_low_once":
        row = type("P", (), {"code": "TMFK6", "quantity": 1, "direction": "Action.Buy", "price": 38001.0})()
        F.revert, F.snaps = 1.0, [[row]]
        CLK.t += 4
        mod.reconcile()
        F.revert = 0
        CLK.t += 4
        mod.reconcile()
    p = mod._state["position"]
    pk = None if p is None else {k: p.get(k) for k in ("qty", "touched", "handover")}
    if p is not None:
        pk["legs"] = [(l.get("qty"), l.get("src")) for l in p.get("legs") or []]
        pk["susp"] = bool(p.get("touch_susp"))
    n0 = len(F.sent)
    mod.close("eod")
    return (pk, S(F, n0), dict(F.hold), F.bad,
            [{k: t.get(k) for k in ("qty", "points", "reason")} for t in trades_of(mod)],
            (mod._state["position"] or {}).get("handover"))


for kind in ("stale_high_once", "stale_high_20s", "stale_low_once"):
    a, b = scen4(broker, kind), scen4(bm, kind)
    print("  %s %s\n    branch=%s\n    main  =%s" % (kind, "相同" if a == b else "不同", a, b))
    chk("QA3-4 %s branch 沒有平過頭" % kind, a[3], [])

# ---- 4b. 撤停利瞬間成交＋部位回報慢 6 秒（他沒加碼）
tag("4b 撤停利單那一瞬間停利成交 1 口、部位回報慢 6 秒 ⇒ 停損：branch 混搭／整筆小台 vs main 微台多口")
for name, mod, mini, qq in (("branch MIX11", broker, True, 11), ("branch MXF10", broker, True, 10),
                            ("branch MIX7", broker, True, 7), ("main TMF3", bm, False, 3), ("branch TMF3-off", broker, False, 3)):
    F = fresh3(mini=mini, mod=mod)
    mod.enter("long", 38000, 60, sl_points=200, qty=qq)
    CLK.t += 30
    F.lag = 6.0
    F.tp_on_cancel = 1.0
    n0 = len(F.sent)
    r = mod.close("sl")
    print("  %-16s close=%s sent=%s hold=%s bad=%s" % (name, r[0], S(F, n0), F.hold, F.bad))
    if mod is broker and mini:
        chk("QA3-4b %s 撤停利瞬間成交不平過頭" % name, F.bad, [])

# ---- 4c. 平倉成功（靠明細）後部位回報還停在舊資料 ⇒ 對帳撿回假部位 ⇒ 停損再平
tag("4c 平倉成功後部位回報慢 8 秒：對帳撿回假部位再停損（main 也有，比較平過頭的量）")
for name, mod, mini, qq in (("main TMF3", bm, False, 3), ("main MXF10", bm, True, 10), ("branch TMF3-off", broker, False, 3),
                            ("branch MIX7", broker, True, 7), ("branch MIX11", broker, True, 11), ("branch MXF10", broker, True, 10)):
    F = fresh3(mini=mini, mod=mod)
    mod.enter("long", 38000, 60, sl_points=200, qty=qq)
    CLK.t += 30
    F.lag = 8.0
    r = [mod.close("sl")[0]]
    for i in range(3):
        CLK.t += 3.1
        mod._LAST_RECONCILE["at"] = 0
        mod.reconcile_tick()
        if mod._state["position"] is not None:
            r.append(("recovered", mod._state["position"].get("qty")))
            r.append(mod.close("sl")[0])
    over = sum(abs(min(0, F.hold[p])) * broker.RATIO[p] for p in F.hold)
    print("  %-16s over_eq=%s hold=%s %s" % (name, over, F.hold, r))

# ---- 5. 隨機性質測試（他不加碼）
tag("5  隨機性質測試（他不加碼）：兩種強度 x branch 混搭與小台、branch 微台 開關關、main 微台")


def pol_new(rng, F):
    def f(pr, r):
        if r["oc"] == "New":
            return rng.randint(0, r["q"]), F.mkt[pr]
        return r["q"], F.mkt[pr]
    return f


def pol_cover(rng, F):
    def f(pr, r):
        if r["oc"] == "Cover":
            return rng.randint(0, r["q"]), F.mkt[pr]
        return r["q"], F.mkt[pr]
    return f


PROFILES = {
    "realistic": dict(lags=[0.0, 2.0, 6.0], p_lag=0.5, reverts=[], p_rev=0.0, hides=[2.0, 8.0], p_hide=0.4, p_toc=0.3),
    "harsh": dict(lags=[2.0, 6.0, 12.0, 30.0], p_lag=0.4, reverts=[0.1, 0.3], p_rev=0.3, hides=[2.0, 8.0, 20.0, 40.0],
                  p_hide=0.4, p_toc=0.3),
}


def run_seed(mod, mini, qtys, prof, seed):
    rng = random.Random(5000 + seed)
    F = fresh3(mini=mini, rng=rng, mod=mod)
    qq = rng.choice(qtys)
    d = rng.choice(["long", "short"])
    sgn = 1 if d == "long" else -1
    ev = []
    if rng.random() < 0.3:
        F.policy = pol_new(rng, F)
        ev.append("entry_partial")
    if rng.random() < 0.25:
        F.late = {rng.choice(["TMF", "MXF"]): rng.choice([3.0, 8.0, 30.0])}
        ev.append(("late", dict(F.late)))
    mod.enter(d, 38000, 60, sl_points=200, qty=qq)
    F.policy = None
    if sum(F.new_fill.values()) == 0:
        return None
    for _ in range(rng.randint(0, 3)):
        CLK.t += rng.choice([1, 4, 16, 40])
        x = rng.random()
        legs = [p for p in ("MXF", "TMF") if F.hold[p] != 0]
        if x < 0.4 and legs:
            p = rng.choice(legs)
            try:
                F.fill_target(p, 1)
                ev.append(("tp", p))
            except AssertionError:
                pass
        elif x < 0.5:
            mod._state["position"] = None
            mod._META.update(last=None, swept=False)
            ev.append("restart")
        if rng.random() < 0.6:
            mod.reconcile()
            ev.append("rec")
    if rng.random() < prof["p_lag"]:
        F.lag = rng.choice(prof["lags"])
        ev.append(("lag", F.lag))
    if prof["reverts"] and rng.random() < prof["p_rev"]:
        F.revert = rng.choice(prof["reverts"])
        ev.append(("revert", F.revert))
    if rng.random() < prof["p_hide"]:
        F.hide_t = (rng.choice([0.5, 1.0]), rng.choice(prof["hides"]))
        ev.append(("hide", F.hide_t))
    if rng.random() < prof["p_toc"]:
        F.tp_on_cancel = 0.7
        ev.append("tp_on_cancel")
    if rng.random() < 0.3:
        F.policy = pol_cover(rng, F)
        ev.append("close_partial")
    F.mkt = {"TMF": 38001.0 - sgn * 150, "MXF": 38000.0 - sgn * 150}
    for attempt in range(8):
        if mod._state["position"] is None and F.eq() == {}:
            break
        mod.close("sl")
        ev.append(("close", {k: v for k, v in F.hold.items() if v}, (P(mod) or {}).get("handover")))
        CLK.t += mod.CLOSE_COOLDOWN + 1
        mod._LAST_RECONCILE["at"] = 0
        mod.reconcile_tick()
        if attempt == 3:
            F.lag, F._hist, F.revert, F.hide_t = 0, [], 0, None
            F.policy = None
            F.release_all()
            F.tp_on_cancel = 0
    out = {"bad": bool(F.bad), "left": None, "card": None, "hand": False, "ev": ev, "qq": qq}
    p = mod._state["position"]
    if F.eq():
        if p is None:
            out["left"] = "pos=None"
        elif p.get("handover"):
            out["left"] = "handover"
        elif p.get("multi_unknown"):
            out["left"] = "multi_unknown"
        else:
            out["left"] = "other"
    elif p is not None:
        out["hand"] = True
    tr = trades_of(mod)
    nq = F.new_fill["TMF"] + F.new_fill["MXF"] * 5
    rq = sum(int(t.get("qty") or 0) for t in tr)
    real = F.realized_eqpts(sgn)
    got = sum((t.get("points") or 0) * int(t.get("qty") or 0) for t in tr)
    priced = all(t.get("points") is not None for t in tr)
    if not F.eq() and not F.bad:
        if rq != nq or len(tr) != 1:
            out["card"] = "rows/qty"
        elif priced and abs(real - got) > 0.06 * nq + 0.02:
            out["card"] = "points"
        elif not priced:
            out["card"] = "blank"
    return out


MIXQ = [6, 7, 8, 9, 11, 12, 13, 14, 16, 19, 10, 15]
TMFQ = [2, 3, 4, 5, 6, 7, 8, 9, 10]
RES = {}
EX = {}
for prof in ("realistic", "harsh"):
    for name, mod, mini, qtys in (("branch-mix", broker, True, MIXQ), ("branch-tmf-off", broker, False, TMFQ),
                                  ("main-tmf", bm, False, TMFQ)):
        st = {"n": 0, "bad": 0, "left": {}, "hand": 0, "card": {}}
        ex = {"bad": [], "left": [], "card": []}
        for seed in range(N_RAND):
            o = run_seed(mod, mini, qtys, PROFILES[prof], seed)
            if o is None:
                continue
            st["n"] += 1
            if o["bad"]:
                st["bad"] += 1
                ex["bad"].append((seed, o["qq"], o["ev"]))
            if o["left"]:
                st["left"][o["left"]] = st["left"].get(o["left"], 0) + 1
                ex["left"].append((seed, o["qq"], o["left"], o["ev"]))
            st["hand"] += o["hand"]
            if o["card"]:
                st["card"][o["card"]] = st["card"].get(o["card"], 0) + 1
                if o["card"] != "blank":
                    ex["card"].append((seed, o["qq"], o["card"], o["ev"]))
        RES[(prof, name)] = st
        EX[(prof, name)] = ex
        print("  [%s] %-15s n=%d over=%d not_closed=%s local_left=%d card=%s" % (
            prof, name, st["n"], st["bad"], st["left"], st["hand"], st["card"]))
for k in ("bad", "left", "card"):
    for x in EX[("realistic", "branch-mix")][k][:5]:
        print("      realistic branch-mix %s seed=%s %s" % (k, x[0], x[1:]))
r = RES[("realistic", "branch-mix")]
chk("QA3-5 realistic 混搭與小台：沒有平過頭", r["bad"], 0)
chk("QA3-5 realistic 混搭與小台：程式的口數最後都平掉", sum(r["left"].values()), 0)
chk("QA3-5 realistic 混搭與小台：成績單口數與點數正確（不含留白）",
    sum(v for k, v in r["card"].items() if k != "blank"), 0)

print("\n%d passed" % N_OK[0])
print("ALL PASS" if not FAIL else "%d FAIL: %s" % (len(FAIL), FAIL))
