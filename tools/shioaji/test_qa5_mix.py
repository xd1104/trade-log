# -*- coding: utf-8 -*-
"""
第五輪複驗攻擊測試（lab-qa，2026-11-06）。沿用 test_qa4_mix.py 的治具（假券商／假時鐘／main 對照組）。
不連永豐、開關檔全在暫存資料夾、不碰 trade-log 面板。
Run: .venv/Scripts/python.exe -X utf8 tools/shioaji/test_qa5_mix.py
"""
import pathlib
import random
import sys

HERE = pathlib.Path(__file__).resolve().parent
_s4 = (HERE / "test_qa4_mix.py").read_text(encoding="utf-8")
_h = _s4[:_s4.index("# ======================================================================================")]
_h = _h.replace('pathlib.Path(__file__).resolve().parent', 'pathlib.Path(%r)' % str(HERE))
exec(compile(_h, "qa4_harness", "exec"))   # noqa: S102


def tag(n):
    print("=== Q%s ===" % n)


def hide_leg(F, leg, secs, pos_too=True, status=None):
    """那一種合約的進場單：明細（＋狀態）晚 secs 秒；pos_too ⇒ 部位也晚 secs 秒。status ⇒ 回報時改成這個狀態＋0 成交（沒撮到）。"""
    if pos_too:
        F.late = {leg: secs}
    po = F.place_order

    def place(c, o):
        t = po(c, o)
        r = F.sent[-1]
        if r["p"] == leg and r["oc"] == "New":
            d, st = t.status.deals, t.status.status
            if status is not None:
                d, st = [], status
            t.hidden = (d, st, ("T", CLK.t + secs))
            t.status.deals, t.status.status = [], "OrderStatus.Submitted"
        return t
    F.place_order = place


def stop_loop(mod, F, secs, every=5.0, dt=1.0, log=None, t0=0.0):
    """模擬面板：每 dt 秒對帳一次，價格一直在停損外 ⇒ 每 every 秒叫一次停損。"""
    t_end = CLK.t + secs
    nxt = CLK.t
    while CLK.t < t_end:
        CLK.t += dt
        mod._LAST_RECONCILE["at"] = 0
        mod.reconcile_tick()
        if CLK.t >= nxt:
            nxt = CLK.t + every
            if mod._state["position"] is not None:
                ok, err = mod.close("sl")
                if log is not None:
                    log.append((round(CLK.t - t0), ok, (err or "")[:40], dict(F.hold)))


# ======================================================================================
tag("5-1  風險①：混搭 11 口，微台那一腿部位＋明細晚 20 秒；第 7 秒停損（價格一直在停損外）")
cases = [("真的成交、20 秒後回報", dict(secs=20.0)),
         ("其實沒撮到、20 秒後回報 Cancelled", dict(secs=20.0, nofill=True, status="OrderStatus.Cancelled")),
         ("其實沒撮到、狀態永遠不回報", dict(secs=9999.0, nofill=True, status="OrderStatus.Cancelled")),
         ("真的成交、部位 20 秒後出現但明細永遠不回報", dict(secs=9999.0, pos_secs=20.0))]
for name, cfg in cases:
    F = fresh3(mini=True)
    T0 = CLK.t
    if cfg.get("nofill"):
        F.policy = lambda pr, r: (0, F.mkt[pr]) if (pr == "TMF" and r["oc"] == "New") else (r["q"], F.mkt[pr])
    if cfg.get("pos_secs"):
        F.late = {"TMF": cfg["pos_secs"]}
        hide_leg(F, "TMF", cfg["secs"], pos_too=False)
    else:
        hide_leg(F, "TMF", cfg["secs"], pos_too=not cfg.get("nofill"), status=cfg.get("status"))
    broker.enter("long", 38000, 60, sl_points=200, qty=11)
    F.policy = None
    F.mkt = {"TMF": 37801.0, "MXF": 37800.0}
    CLK.t = T0 + 7
    log = []
    ok, err = broker.close("sl")
    log.append((7, ok, (err or "")[:40], dict(F.hold)))
    naked, shell = 0, 0
    for s in range(200):
        stop_loop(broker, F, 1.0, every=5.0, dt=1.0, log=log, t0=T0)
        p = broker._state["position"]
        if F.eq() and (p is None or p.get("recovered") or p.get("sl_points") is None):
            naked += 1
        if isinstance(p, dict) and not p.get("qty"):
            shell += 1
    tr = trades_of()
    print("  [%s] 最後券商=%s 部位=%s 空殼秒數=%d 有部位卻沒有程式停損的秒數=%d 平過頭=%s" % (
        name, F.eq(), PP(), shell, naked, F.bad))
    print("     成績單：%s" % [(t.get("qty"), t.get("points"), t.get("reason")) for t in tr])
    print("     停損呼叫：%s" % log[:7])
    chk("Q5-1 [%s] 沒有平過頭" % name, F.bad, [])
    chk("Q5-1 [%s] 最後券商空手" % name, F.eq(), {})
    nq = F.new_fill["TMF"] + 5 * F.new_fill["MXF"]
    chk("Q5-1 [%s] 成績單一列、口數＝實際成交 %d" % (name, nq), [int(t.get("qty") or 0) for t in tr], [nq])
    say(naked <= 65, "Q5-1 [%s] 程式的口數沒有停損的時間 ≤ 65 秒" % name, naked)

# ======================================================================================
tag("5-2  空殼期間收盤認人：auto_fire._looks_ours 看到口數 0 的部位")
import auto_fire  # noqa: E402
F = fresh3(mini=True)
T0 = CLK.t
hide_leg(F, "TMF", 20.0)
broker.enter("long", 38000, 60, sl_points=200, qty=11)
CLK.t = T0 + 7
broker.close("sl")
p = broker._state["position"]
ent = {"dir": "long", "entry": (p or {}).get("entry0") or 38000.0, "qty": 10, "qty_sent": 11}
print("  空殼部位：%s" % PP())
print("  _looks_ours => %s" % (auto_fire._looks_ours(p, ent),))

# ======================================================================================
tag("5-3  風險②：上一筆剛平掉、券商部位停在舊的 10 口（回報慢 6 秒），1 秒後同方向再進場 10 口 ⇒ 停損")


def _pol_new_k(F, k):
    def f(pr, r):
        if r["oc"] == "New":
            return k, F.mkt[pr]
        return r["q"], F.mkt[pr]
    return f


res = {}
for mod, name in ((bm, "main"), (broker, "branch")):
    for fill in (4, 0, 10):
        F = fresh3(mini=False, mod=mod)
        mod.enter("long", 38000, 60, sl_points=200, qty=10)
        tick(mod, 2)
        F.lag = 6.0
        mod.close("sl")
        tick(mod, 1, 1.0)
        F.policy = _pol_new_k(F, fill)
        n0 = len(F.sent)
        mod.enter("long", 38000, 60, sl_points=200, qty=10)
        F.policy = None
        CLK.t += 1
        mod.close("sl")
        for _ in range(10):
            cool()
            mod._close_fail["at"] = 0
            mod._LAST_RECONCILE["at"] = 0
            mod.reconcile_tick()
            if mod._state["position"] is not None:
                mod.close("sl")
        F.lag, F._hist = 0, []
        tick(mod, 50)
        res[(name, fill)] = (S(F, n0), dict(F.hold), list(F.bad))
        print("  %-6s 新單成交 %2d 口：送出=%s 券商=%s 平過頭=%s 部位=%s" % (name, fill, S(F, n0), F.hold, F.bad, PP(mod)))
for fill in (4, 0, 10):
    b, a = res[("branch", fill)], res[("main", fill)]
    say(len(b[2]) <= len(a[2]) and abs(b[1]["TMF"]) <= abs(a[1]["TMF"]),
        "Q5-3 新單成交 %d 口：branch 平過頭／殘留不比 main 多" % fill, (b, a))

# ======================================================================================
tag("5-4  Q4-1 重跑加強：微台多口 branch vs main 600 輪（回報 0～2 秒，含進場／平倉明細晚 0～2 秒、對帳時間點隨機）")


def _score(o):
    return (bool(o["bad"]) + bool(o["left"]) + (o["rq"] != o["nq"]) + bool(o["zero_rows"])
            + (abs(o["pts"] - o["real"]) > 0.06 * o["nq"] + 0.02))


worse, diff_cnt, n_run = [], 0, 0
for seed in range(600):
    res = {}
    for mod, name in ((bm, "main"), (broker, "branch")):
        rng = random.Random(15000 + seed)
        F = fresh3(mini=False, rng=rng, mod=mod)
        qq = rng.choice([2, 3, 4, 5, 6, 7, 8, 9, 10, 10, 10, 10, 4, 4])
        d = rng.choice(["long", "short"])
        sgn = 1 if d == "long" else -1
        if rng.random() < 0.3:
            F.policy = pol_new(rng, F)
        F.lag = rng.choice([0.0, 0.0, 1.0, 2.0])
        if rng.random() < 0.5:
            F.hide_t = (1.0, rng.choice([0.5, 1.0, 2.0]))
        mod.enter(d, 38000, 60, sl_points=200, qty=qq)
        F.policy = None
        if sum(F.new_fill.values()) == 0:
            res = None
            break
        tick(mod, 1, rng.choice([0.2, 0.7, 1.5, 3.0]))
        F.hide_t = (1.0, rng.choice([0.5, 1.0, 2.0])) if rng.random() < 0.5 else None
        for _ in range(rng.randint(0, 4)):
            tick(mod, 1, rng.choice([1.0, 3.0, 10.0]))
            if rng.random() < 0.3 and F.hold["TMF"]:
                try:
                    F.fill_target("TMF", rng.randint(1, abs(F.hold["TMF"])))
                except AssertionError:
                    pass
        if rng.random() < 0.4:
            F.policy = pol_cover(rng, F)
        F.mkt = {"TMF": 38001.0 - sgn * 150, "MXF": 38000.0 - sgn * 150}
        for attempt in range(8):
            if mod._state["position"] is None and not F.eq():
                break
            mod.close("sl")
            tick(mod, 6)
            if attempt == 2:
                F.policy = None
        F.hide_t = None
        F.release_all()
        tick(mod, 60)
        res[name] = outcome(mod, F, sgn)
    if res is None:
        continue
    n_run += 1
    a, b = res["main"], res["branch"]
    if (a["left"], a["rq"], a["pts"], bool(a["bad"])) != (b["left"], b["rq"], b["pts"], bool(b["bad"])):
        diff_cnt += 1
        if _score(b) > _score(a) or (b["bad"] and not a["bad"]) or (b["left"] and not a["left"]):
            worse.append((seed, a, b))
better = None
print("  跑了 %d 輪，跟 main 不同 %d 輪，branch 比 main 糟 %d 輪" % (n_run, diff_cnt, len(worse)))
for w in worse[:4]:
    print("    seed", w[0], "\n      main  ", w[1], "\n      branch", w[2])
chk("Q5-4 微台多口 branch 沒有一輪比 main 糟", len(worse), 0)

# ======================================================================================
tag("5-5  承 5-3：上一筆剛平完、券商還停在舊部位的那幾秒，can_enter() 會不會放行（auto_fire／night_fire 送單前只靠它）")
for mod, name in ((bm, "main"), (broker, "branch")):
    F = fresh3(mini=False, mod=mod)
    mod.enter("long", 38000, 60, sl_points=200, qty=10)
    tick(mod, 2)
    F.lag = 6.0
    mod.close("sl")
    tick(mod, 1, 1.0)
    ce = mod.can_enter(38000, True)
    print("  %-6s 平完 1 秒後（券商還顯示舊的 10 口）can_enter => %s" % (name, ce))
    if name == "branch":
        say(not ce[0], "Q5-5 券商還顯示舊部位時 can_enter 不放行（main 會擋）", ce)

print("\n%d passed" % N_OK[0])
print("ALL PASS" if not FAIL else "%d FAIL: %s" % (len(FAIL), FAIL))
sys.exit(1 if FAIL else 0)
