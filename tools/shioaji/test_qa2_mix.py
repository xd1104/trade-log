# -*- coding: utf-8 -*-
"""
第二輪品管攻擊測試（lab-qa，2026-11-06，不 commit）。
不連永豐、不碰 8770、開關檔全部導到暫存資料夾（沿用 test_qa_mix.py 的假券商／假時鐘）。
另外用 importlib 載入 main 的 broker.py（唯讀，只 import）做「開關沒開」的差異比對。
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
_spec = importlib.util.spec_from_file_location("broker_main", str(MAIN))
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


def tag(n):
    print("\n=== QA2-%s ===" % n)


def live_targets(F):
    return [(p, act, px) for (p, t, act, px) in F.targets
            if t.status.status in ("OrderStatus.Submitted", "OrderStatus.PartFilled")]


def connect_m(mod, fake, mini=False, live=True):
    mod.configure(fake, C["TMF"], {"MXF": C["MXF"]})
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
    if live:
        mod.REAL_FLAG.write_text("on", encoding="utf-8")
    return fake


def fresh_m(mod, mini=False):
    for d in (mod.ORDER_DIR, mod.TRADE_DIR):
        if d.exists():
            for f in d.glob("*"):
                f.unlink()
    return connect_m(mod, Fake(), mini)


def trades_m(mod):
    f = mod.TRADE_DIR / ("%s.jsonl" % date.today())
    rows = [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []
    for r in rows:
        r.pop("exit_time", None)
        for l in r.get("legs") or []:
            l.pop("at", None)
    return rows


# ======== 1. 混搭上他加碼一腿 => 停損 ========
tag("1  MIX 11 (10+1)，他自己加碼 1 口微台，持續 >15 秒 => 觸及停損：程式那 11 口應該平掉、他那 1 口不動")
F = fresh()
broker.enter("long", 38000, 60, sl_points=200, qty=11)
CLK.t += 40
F._apply("TMF", 1, 38001.0)
r1 = broker.reconcile()
CLK.t += 16
r2 = broker.reconcile()
print("  touched:", r2.get("touched"), "qty:", r2.get("qty"), "mix:", r2.get("mix"))
F.mkt = {"TMF": 37801.0, "MXF": 37800.0}
n0 = len(F.sent)
ok, err = broker.close("sl")
print("  close:", ok, (err or "")[:120], S(F, n0), "hold:", F.hold)
chk("QA2-1 MIX touched：停損有平掉程式的小台 2 口", F.hold.get("MXF"), 0)
chk("QA2-1 MIX touched：他加的微台 1 口沒被平（hold TMF=1）", F.hold.get("TMF"), 1)

tag("1b 對照：單一合約 TMF 3 口（開關關）他加碼 1 口 => 停損只平程式的 3 口")
for name, mod in (("branch", broker), ("main", bm)):
    F = fresh_m(mod, mini=False)
    mod.enter("long", 38000, 60, sl_points=200, qty=3)
    CLK.t += 40
    F._apply("TMF", 1, 38001.0)
    mod.reconcile()
    CLK.t += 16
    mod.reconcile()
    n0 = len(F.sent)
    ok, err = mod.close("sl")
    print("  %s close: %s %s sent=%s hold=%s" % (name, ok, (err or "")[:60], S(F, n0), F.hold))
    chk("QA2-1b %s 單一合約 touched 停損平掉程式 3 口、留他 1 口" % name, F.hold.get("TMF"), 1)

# ======== 2. 停利剛成交在撤單前 => 成績單點數 ========
for qq, label in ((11, "MIX 11"), (10, "MXF 10")):
    tag("2  %s：停損撤停利單那一瞬間小台停利成交 1 口（5 等值），部位已回報 => 成績單 points 要有值" % label)
    for name, mod in (("branch", broker), ("main", bm)):
        if name == "main" and qq == 11:
            continue
        F = fresh_m(mod, mini=True)
        mod.enter("long", 38000, 60, sl_points=200, qty=qq)
        CLK.t += 30
        _oc = F.cancel_order
        fired = [False]

        def co(t, F=F, _oc=_oc, fired=fired):
            if not fired[0] and CODE2P[t.contract.code] == "MXF":
                fired[0] = True
                F.fill_target("MXF", 1)
            return _oc(t)

        F.cancel_order = co
        F.mkt = {"TMF": 37801.0, "MXF": 37800.0}
        ok, err = mod.close("sl")
        tr = trades_m(mod)
        last = tr[-1] if tr else {}
        print("  %s close=%s hold=%s bad=%s rec: qty=%s exit=%s points=%s legs=%s" % (
            name, ok, F.hold, F.bad, last.get("qty"), last.get("exit"), last.get("points"),
            [(l.get("qty"), l.get("px"), l.get("src")) for l in last.get("legs") or []]))
        chk("QA2-2 %s %s 沒有平過頭" % (label, name), F.bad, [])
        say(last.get("points") is not None, "QA2-2 %s %s 成績單 points 有值（風控才算得到）" % (label, name), last.get("points"))

# ======== 3. 他在別處平掉一腿 => 停損 ========
tag("3  MIX 11：他自己平掉微台那 1 口 => 停損只平小台 2 口、不平過頭")
F = fresh()
broker.enter("long", 38000, 60, sl_points=200, qty=11)
CLK.t += 30
F._apply("TMF", -1, 38050.0)
r = broker.reconcile()
print("  after his close:", {k: r.get(k) for k in ("product", "qty", "mix", "legs")})
F.mkt = {"TMF": 37801.0, "MXF": 37800.0}
n0 = len(F.sent)
ok, err = broker.close("sl")
print("  close:", ok, (err or "")[:80], S(F, n0), F.hold, F.bad, "live tgts:", live_targets(F))
chk("QA2-3 平掉小台 2 口、不碰微台", [s for s in S(F, n0) if s[3] == "Cover" and s[4] == "MKP"],
    [("MXF", "Sell", 2, "Cover", "MKP")])
chk("QA2-3 沒有平過頭", F.bad, [])
chk("QA2-3 券商空手", F.eq(), {})
chk("QA2-3 沒有殘留停利單", live_targets(F), [])

tag("3b MIX 11：他自己平掉 1 口小台（5 等值）=> 停損平 1 小台+1 微台")
F = fresh()
broker.enter("long", 38000, 60, sl_points=200, qty=11)
CLK.t += 30
F._apply("MXF", -1, 38050.0)
r = broker.reconcile()
n0 = len(F.sent)
ok, err = broker.close("sl")
print("  close:", ok, (err or "")[:80], S(F, n0), F.hold, F.bad)
chk("QA2-3b 沒有平過頭", F.bad, [])
chk("QA2-3b 券商空手", F.eq(), {})
tr = trades()
print("  rec:", {k: tr[-1].get(k) for k in ("qty", "product", "mix", "points", "exit")} if tr else None)

# ======== 4. 開關沒開：branch vs main 差異比對 ========
tag("4  MINI_ON 關：同一套情境 branch 與 main 送單、部位、成績單逐項比")


def scen(mod, kind):
    F = fresh_m(mod, mini=False)
    rng = random.Random(7)
    F.rng = rng
    if kind == "q1_stop":
        mod.enter("long", 38000, 60, sl_points=200, qty=1)
        CLK.t += 10
        mod.close("sl")
    elif kind == "q3_tp_partial_stop":
        mod.enter("short", 38000, 60, sl_points=200, qty=3)
        CLK.t += 10
        F.fill_target("TMF", 1)
        mod.reconcile()
        mod.close("sl")
    elif kind == "q3_lag_close":
        mod.enter("long", 38000, 60, sl_points=200, qty=3)
        F.lag = 6.0
        mod.close("sl")
        CLK.t += 20
        F.lag = 0
        F._hist = []
        mod.reconcile()
    elif kind == "q3_partial_entry":
        F.policy = lambda pr, r: (2, F.mkt[pr]) if r["oc"] == "New" else (r["q"], F.mkt[pr])
        mod.enter("long", 38000, 60, sl_points=200, qty=3)
        F.policy = None
        mod.close("eod")
    elif kind == "q10_tp_full":
        mod.enter("long", 38000, 60, sl_points=200, qty=10)
        CLK.t += 10
        F.fill_target("TMF", 10)
        mod.reconcile()
    elif kind == "q3_restart_stop":
        mod.enter("long", 38000, 60, sl_points=200, qty=3)
        mod._state["position"] = None
        mod._META.update(last=None, swept=False)
        CLK.t += 10
        mod.reconcile()
        mod.close("sl")
    elif kind == "q3_his_add_then_stop_15s":
        mod.enter("long", 38000, 60, sl_points=200, qty=3)
        CLK.t += 40
        F._apply("TMF", 1, 38001.0)
        mod.reconcile()
        CLK.t += 16
        mod.reconcile()
        mod.close("sl")
    elif kind == "q3_his_add_then_stop_now":
        mod.enter("long", 38000, 60, sl_points=200, qty=3)
        CLK.t += 40
        F._apply("TMF", 1, 38001.0)
        mod.reconcile()
        mod.close("sl")
    elif kind == "q3_stale_up_after_tp":
        # 停利成交 1 口、對帳記好之後，券商回報退回舊資料（3 口）一次，再回到 2 口；之後停損
        mod.enter("long", 38000, 60, sl_points=200, qty=3)
        CLK.t += 10
        F.fill_target("TMF", 1)
        mod.reconcile()
        _rows = F._rows
        F._rows = lambda: [type("P", (), {"code": "TMFK6", "quantity": 3, "direction": "Action.Buy", "price": 38001.0})()]
        CLK.t += 4
        mod.reconcile()
        F._rows = _rows
        CLK.t += 4
        mod.reconcile()
        mod.close("sl")
    elif kind == "q3_hidden_deals_close":
        mod.enter("long", 38000, 60, sl_points=200, qty=3)
        F.hide = (1.0, 2)
        F.lag = 6.0
        mod.close("sl")
    p = mod._state["position"]
    pk = None if p is None else {k: p.get(k) for k in ("dir", "qty", "product", "touched", "handover",
                                                        "multi_unknown", "entry", "entry_qty")}
    return S(F), dict(F.hold), F.bad, trades_m(mod), pk


for kind in ("q1_stop", "q3_tp_partial_stop", "q3_lag_close", "q3_partial_entry", "q10_tp_full",
             "q3_restart_stop", "q3_hidden_deals_close", "q3_his_add_then_stop_15s", "q3_his_add_then_stop_now",
             "q3_stale_up_after_tp"):
    a = scen(broker, kind)
    b = scen(bm, kind)
    same = a == b
    print("  %-28s %s" % (kind, "相同" if same else "不同"))
    if not same:
        for i, nm in enumerate(("sent", "hold", "bad", "trades", "pos")):
            if a[i] != b[i]:
                print("     %s\n       branch=%s\n       main  =%s" % (nm, a[i], b[i]))
    if kind not in ("q3_his_add_then_stop_15s", "q3_his_add_then_stop_now", "q3_stale_up_after_tp"):
        chk("QA2-4 開關關 %s 跟 main 一樣" % kind, same, True)

# ======== 5. 持有混搭時關掉開關 => 停損 ========
tag("5  MIX 11 持有中刪掉 MINI_ON => 停損照樣平乾淨；重啟（開關關）也撿得回混搭")
F = fresh()
broker.enter("long", 38000, 60, sl_points=200, qty=11)
connect(F, mini=False, keep_pos=True)
n0 = len(F.sent)
ok, err = broker.close("sl")
print("  close:", ok, (err or "")[:80], S(F, n0), F.hold, F.bad)
chk("QA2-5 開關關掉後停損平乾淨", F.eq(), {})
F = fresh()
broker.enter("long", 38000, 60, sl_points=200, qty=11)
connect(F, mini=False)
broker._META.update(last=None, swept=False)
r = broker.reconcile()
print("  recovered:", r if not isinstance(r, dict) else {k: r.get(k) for k in ("product", "qty", "mix", "multi_unknown", "sl_src")})
n0 = len(F.sent)
ok, err = broker.close("sl")
print("  close:", ok, (err or "")[:80], S(F, n0), F.hold, F.bad)
chk("QA2-5 重啟+開關關，停損平乾淨", F.eq(), {})

# ======== 6. 晚到腿 + 重啟 ========
tag("6  微台腿晚到 20 秒，停損已平掉小台（late_pending）=> 這時面板重啟")
F = fresh()
F.late = {"TMF": 20.0}
broker.enter("long", 38000, 60, sl_points=200, qty=11)
n0 = len(F.sent)
ok, err = broker.close("sl")
print("  close:", ok, (err or "")[:100], S(F, n0), F.hold, "pos:", {k: pos().get(k) for k in ("qty", "late_pending", "product")} if pos() else None)
meta_before = (broker.ORDER_DIR / broker.META_NAME).exists()
connect(F)
broker._META.update(last=None, swept=False)
r = broker.reconcile()
print("  restart reconcile ->", r, " meta 檔還在？", (broker.ORDER_DIR / broker.META_NAME).exists(), "(重啟前 %s)" % meta_before)
CLK.t = F._late["TMF"][0] + 1
r = broker.reconcile()
print("  late leg reported ->", r if not isinstance(r, dict) else {k: r.get(k) for k in ("product", "qty", "recovered", "sl_src", "multi_unknown", "sl_points")})
tr = trades()
print("  成績單筆數:", len(tr), [{k: t.get(k) for k in ("qty", "points", "reason")} for t in tr])
say(isinstance(r, dict), "QA2-6 晚到腿回報後還是被當成部位（有東西可以停損）", r)
say(isinstance(r, dict) and r.get("sl_points") is not None, "QA2-6 重啟後那 1 口還帶著程式的停損點數（不是手動 130）",
    r if not isinstance(r, dict) else r.get("sl_src"))
say(len(tr) >= 1, "QA2-6 已經平掉的小台 10 口等值有進成績單", len(tr))

# ======== 7. 隨機性質測試 ========
tag("7  隨機 400 輪：混搭／整筆小台／微台多口 x 停利部分成交 x 他加減碼 x 回報慢 x 明細晚到")
viol, leftover, detail = [], {}, {}


def _ours_gone():
    P = pos()
    return P is None or P.get("handover") or P.get("recovered")


for seed in range(400):
    rng = random.Random(1000 + seed)
    F = fresh(mini=True)
    F.rng = rng
    qq = rng.choice([6, 7, 9, 11, 12, 14, 16, 10, 15, 3])
    d = rng.choice(["long", "short"])
    sg = 1 if d == "long" else -1
    broker.enter(d, 38000, 60, sl_points=200, qty=qq)
    if pos() is None:
        continue
    his = {"TMF": 0, "MXF": 0}
    evs = []
    CLK.t += 30
    for _ in range(rng.randint(0, 3)):
        ev = rng.random()
        legs = [p for p in ("MXF", "TMF") if F.hold[p] != 0]
        if not legs:
            break
        p = rng.choice(legs)
        if ev < 0.35:
            try:
                F.fill_target(p, 1)
                evs.append(("tp", p))
            except AssertionError:
                pass
        elif ev < 0.55:
            his[p] += 1
            F._apply(p, sg * 1, F.mkt[p])
            evs.append(("his_add", p))
        elif ev < 0.7 and abs(F.hold[p]) > his[p]:
            F._apply(p, -sg * 1, F.mkt[p])
            evs.append(("his_cut", p))
        if rng.random() < 0.5:
            broker.reconcile()
            evs.append("rec")
        dt = rng.choice([1, 5, 16])
        CLK.t += dt
        evs.append(dt)
    if rng.random() < 0.4:
        F.lag = rng.choice([2.0, 6.0, 12.0])
        evs.append(("lag", F.lag))
    if rng.random() < 0.4:
        F.hide = (0.5, rng.randint(1, 3))
        evs.append(("hide", F.hide))
    for attempt in range(6):
        if _ours_gone():
            break                                   # 程式的部位已經結束（剩的是他的／撿回來的）⇒ 不再按停損
        broker.close("sl")
        cool()
        if attempt == 2:
            F.lag = 0
            F._hist = []
    if F.bad:
        viol.append((seed, qq, F.bad[:2]))
        detail[seed] = evs
    for p in ("MXF", "TMF"):
        if abs(F.hold[p]) < his[p]:
            viol.append((seed, qq, "平到他加碼的 %s：hold=%s his=%s" % (p, F.hold[p], his[p])))
            detail[seed] = evs
    ours = {p: abs(F.hold[p]) - his[p] for p in ("MXF", "TMF")}
    if any(v > 0 for v in ours.values()):
        P = pos() or {}
        k = "touched" if P.get("touched") else ("unknown" if P.get("multi_unknown") else
                                                (str(broker._state.get("last_error"))[:24] if broker._state.get("last_error") else "other"))
        leftover.setdefault(k, []).append((seed, qq, ours, his))
print("  違規（平過頭／平到他的）:", len(viol), viol[:8])
for s in list(detail)[:4]:
    print("    seed %s events: %s" % (s, detail[s]))
print("  程式自己的口數停損 6 次後還留著（漏停損）:", {k: len(v) for k, v in leftover.items()})
for k, v in leftover.items():
    print("    %s 例：%s" % (k, v[:2]))
chk("QA2-7 隨機 400 輪：沒有平過頭、沒有平到他加碼的", viol, [])
chk("QA2-7 隨機 400 輪：程式的口數停損後都平掉", sum(len(v) for k, v in leftover.items()), 0)

# ======== 8. auto_fire / night_fire 認人（晚到腿）========
tag("8  收盤 13:43:30 / 04:58 認人：晚到腿併進來後")
import auto_fire  # noqa: E402
import night_fire  # noqa: E402
F = fresh()
F.late = {"TMF": 8.0}
ok, err, p = broker.enter("long", 38000, 60, sl_points=200, qty=11)
ent = {"dir": "long", "entry": p["entry"], "qty": p["qty"], "qty_sent": 11, "entry_time": p["entry_time"],
       "fill_lo": p.get("fill_lo"), "fill_hi": p.get("fill_hi"), "fill_unsure": bool(p.get("fill_warn"))}
CLK.t = F._late["TMF"][0] + 1
r = broker.reconcile()
print("  merged:", {k: r.get(k) for k in ("product", "qty", "entry", "entry0", "late_leg", "mix")})
print("  auto_fire._looks_ours:", auto_fire._looks_ours(r, ent), " night_fire.looks_ours:", night_fire.looks_ours(r, ent))
chk("QA2-8 auto_fire 收盤認得晚到腿併好的混搭", auto_fire._looks_ours(r, ent)[0], True)
chk("QA2-8 night_fire 04:58 認得", bool(night_fire.looks_ours(r, ent)), True)
connect(F, mini=False, keep_pos=True)
print("  開關關掉後 auto_fire._looks_ours:", auto_fire._looks_ours(broker._state["position"], ent))
chk("QA2-8 開關關掉後 auto_fire 收盤仍認得（不留倉過夜）", auto_fire._looks_ours(broker._state["position"], ent)[0], True)

# ======== 9. snapshot ========
tag("9  snapshot 各種混搭狀態都序列化得了")
bad_snap = []
for st in ("mix", "touched", "late_pending"):
    F = fresh()
    if st == "late_pending":
        F.late = {"TMF": 20.0}
    broker.enter("long", 38000, 60, sl_points=200, qty=11)
    if st == "touched":
        CLK.t += 40
        F._apply("TMF", 1, 38001.0)
        broker.reconcile()
        CLK.t += 16
        broker.reconcile()
    if st == "late_pending":
        broker.close("sl")
    try:
        s = broker.snapshot()
        json.dumps(s, ensure_ascii=False)
        print("  %s: ok note=%s stop_off=%s" % (st, (s["position"] or {}).get("lot_note_kind"), (s["position"] or {}).get("stop_off")))
    except Exception as e:
        bad_snap.append((st, repr(e)[:80]))
chk("QA2-9 snapshot 都序列化得了", bad_snap, [])

# ======== 10. 他剛加碼（15 秒內，還沒判 touched）＋ 平倉單明細晚到 ⇒ 會不會平到他的 ========
tag("10 他加碼 1 口後 5 秒內觸及停損，平倉單成交明細晚 3 次 update 才到（部位即時）")


def scen10(mod, mini, qq, leg, hide=(1.0, 3), lag=0):
    F = fresh_m(mod, mini=mini)
    F.rng = random.Random(3)
    mod.enter("long", 38000, 60, sl_points=200, qty=qq)
    CLK.t += 30
    F._apply(leg, 1, F.mkt[leg])
    mod.reconcile()
    CLK.t += 5
    F.hide = hide
    if lag:
        F.lag = lag
    outs = []
    for i in range(4):
        P = mod._state["position"]
        if P is None or P.get("handover") or P.get("recovered"):
            break
        outs.append(mod.close("sl")[0])
        CLK.t += mod.CLOSE_COOLDOWN + 1
    P = mod._state["position"]
    return dict(F.hold), F.bad, outs, None if P is None else {k: P.get(k) for k in ("qty", "handover", "product")}


for name, mod, mini, qq, leg in (("main  TMF3", bm, False, 3, "TMF"), ("branch TMF3", broker, False, 3, "TMF"),
                                 ("branch MIX11 加微台", broker, True, 11, "TMF"),
                                 ("branch MIX9 加微台", broker, True, 9, "TMF"),
                                 ("branch MIX11 加小台", broker, True, 11, "MXF")):
    h, bad, outs, P = scen10(mod, mini, qq, leg)
    print("  %-20s hold=%s bad=%s closes=%s pos=%s" % (name, h, bad, outs, P))
    chk("QA2-10 %s 他加的那 1 口還在" % name, h.get(leg), 1)

tag("10b 同上，換條件：只有部位回報慢（lag）、或什麼都不慢")
for hide, lag in ((None, 0), (None, 6.0), ((1.0, 1), 0)):
    for name, mod, mini, qq, leg in (("main  TMF3", bm, False, 3, "TMF"), ("branch TMF3", broker, False, 3, "TMF"),
                                     ("branch MIX11 加微台", broker, True, 11, "TMF")):
        h, bad, outs, P = scen10(mod, mini, qq, leg, hide=hide, lag=lag)
        print("  hide=%s lag=%s %-20s hold=%s bad=%s closes=%s pos=%s" % (hide, lag, name, h, bad, outs, P))
        chk("QA2-10b hide=%s lag=%s %s 他加的那 1 口還在" % (hide, lag, name), h.get(leg), 1)

print("\n%d passed" % N_OK[0])
print("ALL PASS" if not FAIL else "%d FAIL: %s" % (len(FAIL), FAIL))
