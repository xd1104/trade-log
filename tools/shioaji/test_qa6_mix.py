# -*- coding: utf-8 -*-
"""
第六輪複驗攻擊測試（lab-qa，2026-11-06）。沿用 test_qa5_mix.py 的治具（前半段：假券商／假時鐘／main 對照組／hide_leg）。
不連永豐、開關檔全在暫存資料夾、不碰 trade-log 面板。
Run: .venv/Scripts/python.exe -X utf8 tools/shioaji/test_qa6_mix.py
"""
import pathlib
import random
import sys
from datetime import datetime

HERE = pathlib.Path(__file__).resolve().parent
_s4 = (HERE / "test_qa4_mix.py").read_text(encoding="utf-8")
_h = _s4[:_s4.index("# ======================================================================================")]
_h = _h.replace('pathlib.Path(__file__).resolve().parent', 'pathlib.Path(%r)' % str(HERE))
exec(compile(_h, "qa4_harness", "exec"))   # noqa: S102
_s5 = (HERE / "test_qa5_mix.py").read_text(encoding="utf-8")
_a = _s5.index("def hide_leg(")
_b = _s5.index("# ======================================================================================", _a)
exec(compile(_s5[_a:_b], "qa5_helpers", "exec"))   # noqa: S102


def tag(n):
    print("=== Q%s ===" % n)


def _pol_new_k(F, k):
    def f(pr, r):
        if r["oc"] == "New":
            return min(k, r["q"]), F.mkt[pr]
        return r["q"], F.mkt[pr]
    return f


def gate_enter(mod, F, d, qty, tries=60, dt=3.0):
    """照 auto_fire 的作法：can_enter 放行才 enter；擋下就等 dt 秒再問。⇒ (擋了幾秒, 最後一次的理由, enter 結果)"""
    waited, why = 0.0, None
    for _ in range(tries):
        mod._LAST_RECONCILE["at"] = 0
        ok, why = mod.can_enter(38000, True)
        if ok:
            return waited, why, mod.enter(d, 38000, 60, sl_points=200, qty=qty)
        CLK.t += dt
        waited += dt
    return waited, why, None


# ======================================================================================
tag("6-1  第五輪必修變化：剛平完＋券商回報慢 L 秒，馬上同方向再進場（走 can_enter／直接 enter 兩種）⇒ 停損")
viol, cnt, worse_main = [], 0, []
for gate in (True, False):
    for lag in (2.0, 6.0, 15.0, 30.0):
        for d in ("long", "short"):
            for mini, qty in ((False, 10), (False, 4), (True, 11), (True, 10), (True, 7)):
                for fill in (0, 1, 3, 99):
                    cnt += 1
                    F = fresh3(mini=mini)
                    broker.enter(d, 38000, 60, sl_points=200, qty=qty)
                    tick(broker, 2)
                    F.lag = lag
                    broker.close("sl")
                    tick(broker, 1, 1.0)
                    F.policy = _pol_new_k(F, fill)
                    if gate:
                        waited, why, r = gate_enter(broker, F, d, qty)
                    else:
                        broker.enter(d, 38000, 60, sl_points=200, qty=qty)
                    F.policy = None
                    CLK.t += 1
                    for _ in range(10):
                        broker._close_fail["at"] = 0
                        broker._LAST_RECONCILE["at"] = 0
                        broker.reconcile_tick()
                        if broker._state["position"] is not None:
                            broker.close("sl")
                        cool()
                    F.lag, F._hist = 0, []
                    tick(broker, 50)
                    for _ in range(4):
                        if broker._state["position"] is not None:
                            broker._close_fail["at"] = 0
                            broker.close("sl")
                        tick(broker, 6)
                    if F.bad or F.eq():
                        viol.append((gate, lag, d, mini, qty, fill, F.bad[:1], F.eq(), PP()))
print("  %d 種組合，平過頭或沒平完：%d" % (cnt, len(viol)))
for v in viol[:6]:
    print("   ", v)
chk("Q6-1 各種延遲／口數／多空／混搭／整筆小台：沒有平過頭、最後都平完", viol, [])

# ======================================================================================
tag("6-2  新擋單會不會擋太久：(a) 券商一直不更新（舊部位 10 分鐘） (b) 平完後券商真的出現一筆同方向、不是我們的部位")
for name, stale_s in (("券商舊資料卡 600 秒", 600.0), ("券商舊資料卡 20 秒", 20.0)):
    for mod, mn in ((bm, "main"), (broker, "branch")):
        F = fresh3(mini=False, mod=mod)
        mod.enter("long", 38000, 60, sl_points=200, qty=10)
        tick(mod, 2)
        F.lag = stale_s
        mod.close("sl")
        T1 = CLK.t
        seen = []
        for i in range(70):          # 210 秒，每 3 秒問一次 can_enter
            CLK.t += 3
            mod._LAST_RECONCILE["at"] = 0
            ok, why = mod.can_enter(38000, True)
            if not seen or seen[-1][1] != (ok, why):
                seen.append((round(CLK.t - T1), (ok, why), PP(mod)))
        print("  [%s] %-6s can_enter 變化：%s" % (name, mn, [(s[0], s[1][0], (s[1][1] or "")[:22], (s[2] or {}).get("recovered")) for s in seen]))
for mod, mn in ((bm, "main"), (broker, "branch")):
    F = fresh3(mini=False, mod=mod)
    mod.enter("long", 38000, 60, sl_points=200, qty=10)
    tick(mod, 2)
    mod.close("sl")
    tick(mod, 1, 1.0)
    F._apply("TMF", 3, 38010.0)              # 別處（不是程式）同方向 3 口
    T1 = CLK.t
    seen = []
    for i in range(60):
        CLK.t += 3
        mod._LAST_RECONCILE["at"] = 0
        ok, why = mod.can_enter(38000, True)
        if not seen or seen[-1][1] != (ok, why):
            seen.append((round(CLK.t - T1), (ok, why), PP(mod)))
    print("  [平完後出現別處同方向 3 口] %-6s can_enter 變化：%s" % (mn, [(s[0], s[1][0], (s[1][1] or "")[:22], (s[2] or {}).get("recovered"), (s[2] or {}).get("sl_points")) for s in seen]))

# ======================================================================================
tag("6-3  進場前已有同方向部位（照明細確認那條）＋進場明細晚到 2／8 秒：會不會少算、停損會不會漏")
for hide in (2.0, 8.0):
    for qty, mini in ((10, False), (11, True)):
        F = fresh3(mini=mini)
        broker.enter("long", 38000, 60, sl_points=200, qty=qty)
        tick(broker, 2)
        F.lag = 6.0
        broker.close("sl")
        tick(broker, 1, 1.0)
        F.hide_t = (1.0, hide)
        ok, err, _p = broker.enter("long", 38000, 60, sl_points=200, qty=qty)
        F.hide_t = None
        p0 = PP()
        F.mkt = {"TMF": 37801.0, "MXF": 37800.0}
        naked = 0
        for s in range(150):
            CLK.t += 1
            broker._LAST_RECONCILE["at"] = 0
            broker.reconcile_tick()
            p = broker._state["position"]
            if s % 5 == 0 and p is not None:
                broker.close("sl")
            if F.eq() and (p is None or p.get("recovered") or p.get("sl_points") is None):
                naked += 1
        F.lag, F._hist = 0, []
        tick(broker, 30)
        for _ in range(4):
            if broker._state["position"] is not None:
                broker._close_fail["at"] = 0
                broker.close("sl")
            tick(broker, 6)
        print("  明細晚 %g 秒 %s %d 口：enter=%s %s 進場後部位=%s 程式口數沒程式停損的秒數=%d 平過頭=%s 最後券商=%s 成績單=%s" % (
            hide, "混搭" if mini else "微台", qty, ok, (err or "")[:40], p0, naked, F.bad, F.eq(),
            [(t.get("qty"), t.get("reason")) for t in trades_of()]))
        chk("Q6-3 明細晚 %g 秒 %d 口：沒有平過頭" % (hide, qty), F.bad, [])
        chk("Q6-3 明細晚 %g 秒 %d 口：最後平完" % (hide, qty), F.eq(), {})
        say(naked <= 5, "Q6-3 明細晚 %g 秒 %d 口：程式的口數沒有程式停損的時間 ≤ 5 秒" % (hide, qty), naked)

# ======================================================================================
tag("6-4  日盤收盤 auto_fire._eod 遇到混搭空殼：等得到／等不到晚到那一腿")
import auto_fire  # noqa: E402
auto_fire.time = CLK
ROWS = []
auto_fire._eod_settled = lambda d: False
auto_fire._already_closed = lambda ent: ("open", None)
auto_fire._eod_row = lambda d, why, extra=None, msg=None: ROWS.append((why, msg)) or why
eod_cases = [("晚到腿真的成交、20 秒後回報", dict(secs=20.0)),
             ("晚到腿沒撮到、狀態永遠不回報", dict(secs=9999.0, nofill=True)),
             ("晚到腿成交、部位 20 秒後出現但明細永遠不回報", dict(secs=9999.0, pos_secs=20.0)),
             ("晚到腿真的成交、70 秒後才回報", dict(secs=70.0))]
for name, cfg in eod_cases:
    F = fresh3(mini=True)
    T0 = CLK.t
    if cfg.get("nofill"):
        F.policy = lambda pr, r: (0, F.mkt[pr]) if (pr == "TMF" and r["oc"] == "New") else (r["q"], F.mkt[pr])
        hide_leg(F, "TMF", cfg["secs"], pos_too=False, status="OrderStatus.Cancelled")
    elif cfg.get("pos_secs"):
        F.late = {"TMF": cfg["pos_secs"]}
        hide_leg(F, "TMF", cfg["secs"], pos_too=False)
    else:
        hide_leg(F, "TMF", cfg["secs"])
    broker.enter("long", 38000, 60, sl_points=200, qty=11)
    F.policy = None
    p = broker._state["position"]
    ent = {"rec": "result", "ok": True, "dir": "long", "entry": p.get("entry0"), "qty": p.get("qty"),
           "qty_sent": 11, "fill_unsure": True, "entry_time": p.get("entry_time")}
    auto_fire._auto_entry = (lambda e: (lambda d: (e, None)))(ent)
    CLK.t = T0 + 7
    broker.close("sl")                         # 停損先平掉小台 ⇒ 空殼
    shell0 = PP()
    del ROWS[:]
    r = auto_fire._eod(str(datetime.now().date()), 0, CLK.t)
    after = (round(CLK.t - T0), F.eq(), PP())
    tick(broker, 40)
    print("  [%s] 收盤前部位=%s ⇒ _eod=%s %s 秒、券商=%s 部位=%s；再 120 秒後券商=%s 部位=%s" % (
        name, shell0, ROWS[-1:], after[0], after[1], after[2], F.eq(), PP()))
    chk("Q6-4 [%s] 收盤後券商空手（等到了就平掉）" % name, F.eq(), {})
    say(ROWS and ROWS[-1][0] in ("eod_closed", "eod_flat"), "Q6-4 [%s] 收盤結局是 closed／flat（不是 not_ours／failed）" % name, ROWS[-1:])

# ======================================================================================
tag("6-5  夜盤 04:58 night_fire._eod 遇到混搭空殼（04:58:00～04:59:50 每秒一輪）")
import night_fire  # noqa: E402
from datetime import timedelta
night_fire.time = CLK
for name, cfg in eod_cases:
    F = fresh3(mini=True)
    T0 = CLK.t
    if cfg.get("nofill"):
        F.policy = lambda pr, r: (0, F.mkt[pr]) if (pr == "TMF" and r["oc"] == "New") else (r["q"], F.mkt[pr])
        hide_leg(F, "TMF", cfg["secs"], pos_too=False, status="OrderStatus.Cancelled")
    elif cfg.get("pos_secs"):
        F.late = {"TMF": cfg["pos_secs"]}
        hide_leg(F, "TMF", cfg["secs"], pos_too=False)
    else:
        hide_leg(F, "TMF", cfg["secs"])
    broker.enter("long", 38000, 60, sl_points=200, qty=11)
    F.policy = None
    p = broker._state["position"]
    E = "2026-11-05"
    RN = [{"E": E, "rec": "result", "ok": True, "dir": "long", "entry": p.get("entry0"), "qty": p.get("qty")}]
    night_fire.rows_of = lambda e, RN=RN: list(RN)
    night_fire._append = lambda row, RN=RN: RN.append(row)
    night_fire._trip_recorded = lambda e, ent: None
    night_fire._ST["eod_try_at"] = 0.0
    CLK.t = T0 + 7
    broker.close("sl")
    base = datetime(2026, 11, 6, 4, 58, 0)
    done_at = None
    for s in range(111):
        CLK.t += 1
        broker._LAST_RECONCILE["at"] = 0
        broker.reconcile_tick()
        if night_fire._eod(E, base + timedelta(seconds=s)):
            done_at = s
            break
    eod = [r for r in RN if r.get("rec") == "eod"]
    tick(broker, 40)
    print("  [%s] 定案於 04:58:%s 結局=%s 券商=%s 部位=%s" % (name, done_at, [(r.get("why"), (r.get("msg") or "")[:30]) for r in eod], F.eq(), PP()))
    chk("Q6-5 [%s] 04:59:50 前券商空手" % name, F.eq(), {})

# ======================================================================================
tag("6-6  承 6-1：略過 can_enter 直接 enter（券商回報慢 30 秒、新單部分成交）branch 與 main 對照")
for mod, mn in ((bm, "main"), (broker, "branch")):
    for qty, fill in ((10, 1), (10, 3), (4, 1)):
        F = fresh3(mini=False, mod=mod)
        mod.enter("long", 38000, 60, sl_points=200, qty=qty)
        tick(mod, 2)
        F.lag = 30.0
        mod.close("sl")
        tick(mod, 1, 1.0)
        F.policy = _pol_new_k(F, fill)
        mod.enter("long", 38000, 60, sl_points=200, qty=qty)
        F.policy = None
        CLK.t += 1
        for _ in range(10):
            mod._close_fail["at"] = 0
            mod._LAST_RECONCILE["at"] = 0
            mod.reconcile_tick()
            if mod._state["position"] is not None:
                mod.close("sl")
            cool()
        F.lag, F._hist = 0, []
        tick(mod, 50)
        print("  %-6s %d 口只成交 %d：平過頭=%s 最後券商=%s" % (mn, qty, fill, F.bad[:2], F.eq()))

print("\n%d passed" % N_OK[0])
print("ALL PASS" if not FAIL else "%d FAIL: %s" % (len(FAIL), FAIL))
sys.exit(1 if FAIL else 0)
