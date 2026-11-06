# -*- coding: utf-8 -*-
"""
第七輪複驗攻擊測試（lab-qa，2026-11-06）。沿用 test_qa4／qa5 的治具。對照組 main＝git 的 main（5c8cc0aa4，第六輪放行版）。
不連永豐、開關檔全在暫存資料夾、不碰 trade-log 面板。
Run: .venv/Scripts/python.exe -X utf8 tools/shioaji/test_qa7_mix.py
"""
import pathlib
import random
import sys
from datetime import datetime, timedelta

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


def run_est(mod, qty, mini, hide, nofill=False, horizon=200):
    """
    ① 照明細確認那條：進場前那一次查部位失敗（_entry_base ⇒ "?"）＋進場明細晚 hide 秒（9999＝永遠不回報）。
    價格一直在停損外，面板每 1 秒對帳、每 5 秒叫一次停損。⇒ 程式的口數從進場到「券商空手」要幾秒、期間有沒有任何一口被平。
    """
    F = fresh3(mini=mini, mod=mod)
    if nofill:
        F.policy = lambda pr, r: (0, F.mkt[pr]) if r["oc"] == "New" else (r["q"], F.mkt[pr])
    F.hide_t = (1.0, hide)
    F.pos_fail = 1                        # 進場前第一次查部位失敗
    T0 = CLK.t
    ok, err, _p = mod.enter("long", 38000, 60, sl_points=200, qty=qty)
    F.hide_t, F.policy = None, None
    pe = PP(mod)
    F.mkt = {"TMF": 37801.0, "MXF": 37800.0}
    flat_at, first_cut = None, None
    for s in range(horizon):
        CLK.t += 1
        mod._LAST_RECONCILE["at"] = 0
        mod.reconcile_tick()
        if s % 5 == 0 and mod._state["position"] is not None:
            mod.close("sl")
        if first_cut is None and F.eq() and sum(F.eq().values()) < (qty if not nofill else 1):
            first_cut = round(CLK.t - T0)
        if flat_at is None and not F.eq() and F.new_fill["TMF"] + F.new_fill["MXF"] > 0:
            flat_at = round(CLK.t - T0)
    tr = trades_of(mod)
    return dict(ok=ok, err=(err or "")[:50], pos=pe, flat=flat_at, first=first_cut, bad=F.bad[:2], left=F.eq(),
                rows=[(t.get("qty"), t.get("reason"), t.get("points")) for t in tr],
                tp=[x[1].status.status for x in F.targets])


# ======================================================================================
tag("7-1  ①保守估計：進場前查部位失敗＋進場明細晚 8／20 秒／永遠不回報 ⇒ 多久才平掉（main＝第六輪版：丟例外變撿回部位）")
RES = {}
for hide in (8.0, 20.0, 9999.0):
    for qty, mini in ((10, False), (11, True)):
        for mod, mn in ((bm, "main"), (broker, "branch")):
            r = run_est(mod, qty, mini, hide)
            RES[(hide, qty, mn)] = r
            print("  明細晚 %-6g %s%2d 口 %-6s enter=%s 進場後部位 qty=%s recovered=%s ⇒ 第一口被平 %ss、全平 %ss、平過頭=%s 殘留=%s 成績單=%s" % (
                hide, "混搭" if mini else "微台", qty, mn, r["ok"], (r["pos"] or {}).get("qty"),
                (r["pos"] or {}).get("recovered"), r["first"], r["flat"], r["bad"], r["left"], r["rows"]))
        b = RES[(hide, qty, "branch")]
        chk("Q7-1 明細晚 %g 秒 %d 口：沒有平過頭、最後平完" % (hide, qty), (b["bad"], b["left"]), ([], {}))
        say(b["flat"] is not None and b["flat"] <= 75, "Q7-1 明細晚 %g 秒 %d 口：75 秒內平完" % (hide, qty), b["flat"])
print("  （對照：明細不回報時 main〔第六輪版〕走撿回部位、用手動停損）")
for hide in (8.0, 20.0, 9999.0):
    for qty in (10, 11):
        a, b = RES[(hide, qty, "main")], RES[(hide, qty, "branch")]
        say(b["flat"] is not None and (a["flat"] is None or b["flat"] <= a["flat"] + 5),
            "Q7-1 明細晚 %g 秒 %d 口：branch 平完的時間不比第六輪版晚（%s vs %s）" % (hide, qty, b["flat"], a["flat"]),
            (b["flat"], a["flat"]))

tag("7-1b ①保守估計但其實沒成交（IOC 沒撮到、狀態也晚到／永遠不到）：不會記假成績單、不會平到別的")
for hide in (8.0, 9999.0):
    r = run_est(broker, 10, False, hide, nofill=True)
    print("  明細晚 %g：enter=%s 部位=%s 平過頭=%s 殘留=%s 成績單=%s 停利單=%s" % (hide, r["ok"], r["pos"], r["bad"], r["left"], r["rows"], r["tp"]))
    chk("Q7-1b 沒成交（明細晚 %g）：沒有送任何平倉、沒有成績單" % hide, (r["bad"], r["left"], r["rows"]), ([], {}, []))

# ======================================================================================
tag("7-2  ②收盤再問券商：空殼逾時結算後，窗口內冒出 (i) 晚到那一腿 (ii) 別人的同方向部位（價差 30 點） (iii) 什麼都沒有")
import auto_fire  # noqa: E402
import night_fire  # noqa: E402
auto_fire.time = CLK
night_fire.time = CLK
ROWS = []
auto_fire._eod_settled = lambda d: False
auto_fire._already_closed = lambda ent: ("open", None)
auto_fire._eod_row = lambda d, why, extra=None, msg=None: ROWS.append((why, msg)) or why


def shell_setup(kind):
    """混搭 11 口，微台那一腿永遠沒有明細；第 7 秒停損平掉小台 ⇒ 空殼；kind 決定之後券商上冒出什麼（第 70 秒）。"""
    F = fresh3(mini=True)
    T0 = CLK.t
    if kind != "late":
        F.policy = lambda pr, r: (0, F.mkt[pr]) if (pr == "TMF" and r["oc"] == "New") else (r["q"], F.mkt[pr])
    hide_leg(F, "TMF", 9999.0, pos_too=False, status=(None if kind == "late" else "OrderStatus.Submitted"))
    if kind == "late":
        F.late = {"TMF": 70.0}
    elif kind == "nofill":
        pass
    broker.enter("long", 38000, 60, sl_points=200, qty=11)
    p = broker._state["position"]
    ent = {"rec": "result", "ok": True, "dir": "long", "entry": p.get("entry0"), "qty": p.get("qty"),
           "qty_sent": 11, "fill_unsure": True, "entry_time": p.get("entry_time")}
    CLK.t = T0 + 7
    broker.close("sl")
    return F, T0, ent


for side in ("day", "night"):
    for kind in ("late", "his", "nofill"):
        F, T0, ent = shell_setup(kind)
        his_at = T0 + 70.0
        orig_rec = broker.reconcile

        def rec_hook(F=F, kind=kind, his_at=his_at):
            if kind == "his" and CLK.t >= his_at and not getattr(F, "_his_done", False):
                F._his_done = True
                F._apply("TMF", 2, 38030.0)          # 別人在 38030 開的同方向 2 口（不是程式的）
            return orig_rec()
        broker.reconcile = rec_hook
        t_start = CLK.t
        if side == "day":
            del ROWS[:]
            auto_fire._auto_entry = (lambda e: (lambda d: (e, None)))(ent)
            auto_fire._eod(str(datetime.now().date()), 0, CLK.t)
            res = ROWS[-1][0] if ROWS else None
        else:
            E = "2026-11-05"
            RN = [{"E": E, "rec": "result", "ok": True, "dir": "long", "entry": ent["entry"], "qty": ent["qty"]}]
            night_fire.rows_of = lambda e, RN=RN: list(RN)
            night_fire._append = lambda row, RN=RN: RN.append(row)
            night_fire._trip_recorded = lambda e, ent: None
            night_fire._ST["eod_try_at"] = 0.0
            night_fire._ST.pop("eod_shell", None)
            base = datetime(2026, 11, 6, 4, 58, 0)
            for s in range(111):
                CLK.t += 1
                if night_fire._eod(E, base + timedelta(seconds=s)):
                    break
            res = [r.get("why") for r in RN if r.get("rec") == "eod"]
        broker.reconcile = orig_rec
        took = round(CLK.t - t_start)
        print("  [%s/%s] 結局=%s 花 %d 秒 券商=%s 部位=%s" % (side, kind, res, took, F.eq(), PP()))
        if kind == "late":
            chk("Q7-2 [%s] 晚到那一腿在窗口內冒出 ⇒ 平掉" % side, F.eq(), {})
        elif kind == "his":
            chk("Q7-2 [%s] 別人的同方向 2 口 ⇒ 不碰" % side, F.eq(), {"TMF": 2})
        else:
            chk("Q7-2 [%s] 什麼都沒有 ⇒ 空手" % side, F.eq(), {})
        say(took <= (80 if side == "day" else 111), "Q7-2 [%s/%s] 定案時間在窗口內（日盤 75 秒／夜盤 04:59:50）" % (side, kind), took)

# ======================================================================================
tag("7-3  ③ enter() 自己擋：剛平完 120 秒內，券商顯示舊部位／查詢失敗 ⇒ 一張都不送；120 秒後照常")
for mod, mn in ((bm, "main"), (broker, "branch")):
    for lag in (6.0, 200.0):
        F = fresh3(mini=False, mod=mod)
        mod.enter("long", 38000, 60, sl_points=200, qty=10)
        tick(mod, 2)
        F.lag = lag
        mod.close("sl")
        tick(mod, 1, 1.0)
        n0 = len(F.sent)
        F.policy = _pol_new_k(F, 3) if "_pol_new_k" in globals() else None
        r = mod.enter("long", 38000, 60, sl_points=200, qty=10)
        F.policy = None
        sent = S(F, n0)
        CLK.t += 1
        for _ in range(12):
            mod._close_fail["at"] = 0
            mod._LAST_RECONCILE["at"] = 0
            mod.reconcile_tick()
            if mod._state["position"] is not None:
                mod.close("sl")
            cool()
        F.lag, F._hist = 0, []
        tick(mod, 60)
        print("  %-6s 回報慢 %g 秒：直接 enter ⇒ %s %s 送出=%s 平過頭=%s 最後券商=%s" % (mn, lag, r[0], (r[1] or "")[:30], sent[:1], F.bad[:1], F.eq()))
        if mn == "branch":
            chk("Q7-3 回報慢 %g 秒直接 enter：沒有平過頭" % lag, F.bad, [])

# ======================================================================================
tag("7-4  回歸：Q5-4 600 輪對照 main（5c8cc0aa4）")
_s5b = _s5[_s5.index('tag("5-4'):_s5.index('# ======================================================================================', _s5.index('tag("5-4'))]
exec(compile(_s5b.replace('chk("Q5-4', 'chk("Q7-4'), "q54", "exec"))   # noqa: S102

print("\n%d passed" % N_OK[0])
print("ALL PASS" if not FAIL else "%d FAIL: %s" % (len(FAIL), FAIL))
sys.exit(1 if FAIL else 0)
