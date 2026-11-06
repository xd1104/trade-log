# -*- coding: utf-8 -*-
"""
第八輪複驗攻擊測試（lab-qa，2026-11-06）。沿用 test_qa4／qa5 的治具。對照組 main＝git 的 main（6d708a812，第七輪放行版）。
不連永豐、開關檔全在暫存資料夾、不碰 trade-log 面板。
Run: .venv/Scripts/python.exe -X utf8 tools/shioaji/test_qa8_mix.py
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


def recovered_multi(mod, F, qty, d="long"):
    """面板重啟、沒有落地檔 ⇒ 券商上的 qty 口微台被撿回成一般部位（不是帳本部位）。"""
    mod.enter(d, 38000, 60, sl_points=200, qty=qty)
    for t in list(F.targets):
        try:
            F.cancel_order(t[1])
        except Exception:
            pass
    mod._state["position"] = None
    (mod.ORDER_DIR / mod.META_NAME).unlink(missing_ok=True)
    mod._META.update(last=None, swept=False)
    if hasattr(mod, "_loop_reset"):
        mod._loop_reset()
    tick(mod, 2)
    return PP(mod)


def stop_for(mod, F, secs, every=5):
    """價格一直在停損外、固定跑 secs 秒（券商舊資料期間也照樣叫停損）。"""
    for s in range(secs):
        CLK.t += 1
        mod._LAST_RECONCILE["at"] = 0
        mod.reconcile_tick()
        if s % every == 0 and mod._state["position"] is not None:
            mod.close("sl")


def stop_until_flat(mod, F, T0, horizon=400, every=5):
    """價格一直在停損外：每 1 秒對帳、每 every 秒叫停損 ⇒ 第幾秒券商空手（None＝沒平完）。"""
    for s in range(horizon):
        CLK.t += 1
        mod._LAST_RECONCILE["at"] = 0
        mod.reconcile_tick()
        if s % every == 0 and mod._state["position"] is not None:
            mod.close("sl")
        if not F.eq():
            return round(CLK.t - T0)
    return None


# ======================================================================================
tag("8-1  風險 3：撿回的多口部位（微台 3／10 口）一口一口平，上一張平倉單沒撮到、狀態晚 H 秒／永遠不回報 ⇒ 多久平完（main＝6d708a812）")
for qty in (3, 10):
    for H in (0.0, 10.0, 30.0, 9999.0):
        out = {}
        for mod, mn in ((bm, "main"), (broker, "branch")):
            F = fresh3(mini=False, mod=mod)
            pr = recovered_multi(mod, F, qty)
            F.mkt = {"TMF": 37801.0, "MXF": 37800.0}
            nfirst = [True]

            def pol(p, r, F=F, nfirst=nfirst):
                if r["oc"] == "Cover" and r["pt"] != "LMT" and nfirst[0]:
                    nfirst[0] = False
                    return 0, F.mkt[p]              # 第一張平倉單沒撮到
                return r["q"], F.mkt[p]
            F.policy = pol
            if H > 0:
                F.hide_t = (1.0, H)
            T0 = CLK.t
            n0 = len(F.sent)
            fl = stop_until_flat(mod, F, T0, horizon=400)
            out[mn] = (fl, F.bad[:1], len([x for x in F.sent[n0:] if x["oc"] == "Cover"]))
            F.hide_t = None
        print("  %2d 口、第一張沒撮到＋狀態晚 %-6g：main 平完 %s 秒 平過頭=%s ｜ branch 平完 %s 秒 平過頭=%s（送了 %d 張）" % (
            qty, H, out["main"][0], out["main"][1], out["branch"][0], out["branch"][1], out["branch"][2]))
        chk("Q8-1 %d 口 狀態晚 %g：branch 沒有平過頭" % (qty, H), out["branch"][1], [])
        say(out["branch"][0] is not None, "Q8-1 %d 口 狀態晚 %g：branch 400 秒內平完" % (qty, H), out["branch"][0])

# ======================================================================================
tag("8-2  風險 1：剛乾淨平完（種子 300 秒）後，程式自己同方向再進場、同口數 ⇒ 停損照常")
for qty in (10, 1, 3):
    F = fresh3(mini=False)
    broker.enter("long", 38000, 60, sl_points=200, qty=10)
    tick(broker, 2)
    broker.close("sl")
    tick(broker, 45)                              # 平完 135 秒（RECENT_CLOSE_S 過了，種子還在）
    seed_before = broker._LOOP.get("seed")
    F.mkt = {"TMF": 38001.0, "MXF": 38000.0}      # 同價（同均價）
    ok, err, _p = broker.enter("long", 38000, 60, sl_points=200, qty=qty)
    seed_after = broker._LOOP.get("seed")
    F.mkt = {"TMF": 37801.0, "MXF": 37800.0}
    T0 = CLK.t
    fl = stop_until_flat(broker, F, T0, horizon=120)
    print("  再進場 %2d 口：enter=%s 種子 前=%s 後=%s ⇒ 停損平完 %s 秒 平過頭=%s" % (qty, ok, seed_before, seed_after, fl, F.bad[:1]))
    chk("Q8-2 再進場 %d 口：送新進場單時種子作廢" % qty, seed_after, False)
    say(fl is not None and fl <= 20, "Q8-2 再進場 %d 口：停損 20 秒內平完" % qty, fl)

tag("8-2b 種子＋重啟：剛乾淨平完、券商舊資料卡 200 秒、這時面板重啟 ⇒ 舊資料被撿回後平過頭幾口（main 對照）")
for mod, mn in ((bm, "main"), (broker, "branch")):
    F = fresh3(mini=False, mod=mod)
    mod.enter("long", 38000, 60, sl_points=200, qty=10)
    tick(mod, 2)
    F.lag = 200.0
    mod.close("sl")
    tick(mod, 1)
    for restart_ in (False, True):
        pass
    mod._state["position"] = None
    mod._META.update(last=None, swept=False)
    mod.configure(F, C["TMF"], {"MXF": C["MXF"]})   # 重新連線（重啟）
    T0 = CLK.t
    stop_for(mod, F, 400)
    print("  %-6s 重啟後：平過頭=%d 張 最後券商=%s" % (mn, len(F.bad), F.hold))

tag("8-2c 種子、不重啟：同上但面板沒重啟（開發說明：300 秒內擋、之後最多多平 1 口）")
for mod, mn in ((bm, "main"), (broker, "branch")):
    F = fresh3(mini=False, mod=mod)
    mod.enter("long", 38000, 60, sl_points=200, qty=10)
    tick(mod, 2)
    F.lag = 200.0
    mod.close("sl")
    T0 = CLK.t
    stop_for(mod, F, 400)
    print("  %-6s：平過頭=%d 張 最後券商=%s" % (mn, len(F.bad), F.hold))
    if mn == "branch":
        say(abs(F.hold["TMF"]) <= 1, "Q8-2c 券商舊資料卡 200 秒：最多多平 1 口", F.hold)

# ======================================================================================
tag("8-3  自動補掛停利：進場前券商已有別處同方向 2 口（保守估計那條）＋進場明細晚 8 秒")


def est_entry(deal_k, hide=8.0):
    F = fresh3(mini=False)
    broker._RECENT_CLOSE.update(at=0.0, unclean=False)   # 每一項各自獨立（上一項剛平完的記憶不帶過來）
    broker._loop_reset()
    F._apply("TMF", 2, 37990.0)                   # 進場前券商上已有同方向 2 口（不是這一趟的）
    F.policy = _pol_new_k(F, deal_k)
    F.hide_t = (1.0, hide)
    r = broker.enter("long", 38000, 60, sl_points=200, qty=10)
    F.hide_t, F.policy = None, None
    return F, r


for k in (10, 6, 0):
    F, r = est_entry(k)
    t_before = [(x[2], x[1].order.quantity, x[3]) for x in F.targets]
    tick(broker, 4)
    tgts = [(x[2], x[1].order.quantity, x[3], x[1].status.status) for x in F.targets]
    print("  實際成交 %2d 口：enter=%s 掛停利 前=%s 明細回報後=%s 部位=%s 成績單=%s" % (
        k, r[0], t_before, tgts, PP(), [(t.get("qty"), t.get("reason")) for t in trades_of()]))
    want = [] if k == 0 else [("Sell", k, 38061.0, "OrderStatus.Submitted")]
    chk("Q8-3 實際成交 %d 口：補掛的停利單方向／口數／價格正確" % k, tgts, want)

tag("8-3b 補掛停利的時間點撞上停損：停損撤完停利單、送平倉單之前，主迴圈那一輪對帳剛好補掛")
F, r = est_entry(10, hide=9999.0)
CLK.t += 3                                        # 明細還沒回報（停損這時觸發）
_orig = broker._own_cancel_targets


def _cancel_then_tick(pos, _o=_orig):
    out = _o(pos)
    F.release_all()                               # 明細剛好在停損撤完停利單之後回報
    broker.reconcile()                            # 另一條執行緒（主迴圈）剛好在這時對帳 ⇒ 補掛停利
    return out
broker._own_cancel_targets = _cancel_then_tick
F.mkt = {"TMF": 37801.0, "MXF": 37800.0}
ok, err = broker.close("sl")
broker._own_cancel_targets = _orig
tick(broker, 20)
live = [(x[2], x[1].order.quantity, x[1].status.status) for x in F.targets if "Submitted" in str(x[1].status.status)]
print("  close=%s %s 券商=%s 部位=%s 還掛著的停利單=%s 全部停利單=%s" % (ok, (err or "")[:40], F.hold, PP(), live, [(x[2], x[1].order.quantity, x[1].status.status) for x in F.targets]))
print("  送出的單：%s" % S(F))
chk("Q8-3b 平完之後沒有還掛著的停利單（會變反向新倉）", live, [])

# ======================================================================================
tag("8-4  回歸：Q5-4 600 輪對照 main（6d708a812）")
_s5b = _s5[_s5.index('tag("5-4'):_s5.index('# ======================================================================================', _s5.index('tag("5-4'))]
exec(compile(_s5b.replace('chk("Q5-4', 'chk("Q8-4'), "q54", "exec"))   # noqa: S102

print("\n%d passed" % N_OK[0])
print("ALL PASS" if not FAIL else "%d FAIL: %s" % (len(FAIL), FAIL))
sys.exit(1 if FAIL else 0)
