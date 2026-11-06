# -*- coding: utf-8 -*-
"""
第九輪複驗測試（lab-qa，2026-11-06）。沿用 test_qa4／qa5 的治具。對照組 main＝git 的 main（6d708a812，第七輪放行版）。
不連永豐、開關檔全在暫存資料夾、不碰 trade-log 面板。
Run: .venv/Scripts/python.exe -X utf8 tools/shioaji/test_qa9_mix.py
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
tag("9-1  結算時撤停利：上一趟撤不掉的停利單 ⇒ 示警；下一趟新掛的停利單不會被撤")
F = fresh3(mini=False)
broker._RECENT_CLOSE.update(at=0.0, unclean=False)
broker.enter("long", 38000, 60, sl_points=200, qty=10)
tA = [x[1] for x in F.targets][-1]
F.cancel_err = {"TMF"}                              # 撤單失敗（券商回錯）
ok, err = broker.close("sl")
print("  A 停損：close=%s %s ；A 的停利單=%s last_error=%s" % (ok, (err or "")[:50], tA.status.status, (broker._state.get("last_error") or "")[:40]))
say(ok and err and "停利" in err, "Q9-1 撤不掉的停利單有講", err)
F.cancel_err = set()
tick(broker, 45)                                    # 過了 RECENT_CLOSE_S
F.mkt = {"TMF": 38101.0, "MXF": 38100.0}
broker.enter("long", 38100, 60, sl_points=200, qty=10)
tB = [x[1] for x in F.targets][-1]
tick(broker, 10)
print("  B 進場後 30 秒：B 的停利單=%s（A 的=%s）" % (tB.status.status, tA.status.status))
chk("Q9-1 下一趟的停利單沒被撤", "Submitted" in str(tB.status.status), True)

tag("9-1b 停利已經全部成交（這一趟由停利結束）⇒ 結算時不去撤已成交的停利單、不誤報")
F = fresh3(mini=False)
broker._RECENT_CLOSE.update(at=0.0, unclean=False)
broker.enter("long", 38000, 60, sl_points=200, qty=10)
F.fill_target("TMF", 10)
n_c = len(F.cancels)
broker._state["last_error"] = None
tick(broker, 3)
print("  停利全成交後：部位=%s 撤單次數=%d last_error=%s 成績單=%s" % (PP(), len(F.cancels) - n_c, broker._state.get("last_error"), [(t.get("qty"), t.get("reason")) for t in trades_of()]))
chk("Q9-1b 沒有去撤已成交的停利單、沒有誤報", (len(F.cancels) - n_c, broker._state.get("last_error")), (0, None))

tag("9-1c 停利部分成交 4 口、其餘停損平掉 ⇒ 剩下那 6 口的停利單撤掉、成績單一列 10 口")
F = fresh3(mini=False)
broker._RECENT_CLOSE.update(at=0.0, unclean=False)
broker.enter("long", 38000, 60, sl_points=200, qty=10)
F.fill_target("TMF", 4)
tick(broker, 2)
ok, err = broker.close("sl")
t = [x[1] for x in F.targets][-1]
print("  close=%s %s 停利單=%s 券商=%s 成績單=%s" % (ok, err, t.status.status, F.hold, [(x.get("qty"), x.get("reason")) for x in trades_of()]))
chk("Q9-1c 停利單撤掉、券商空手、成績單 10 口", ("Cancel" in str(t.status.status), F.eq(), [int(x.get("qty") or 0) for x in trades_of()]), (True, {}, [10]))

# ======================================================================================
tag("9-2  平倉進行中標記：平倉失敗／例外之後有沒有清掉；下一趟的保守估計補掛停利照常")


def est_entry(deal_k, hide=8.0):
    F = fresh3(mini=False)
    broker._RECENT_CLOSE.update(at=0.0, unclean=False)
    broker._loop_reset()
    F._apply("TMF", 2, 37990.0)
    F.policy = _pol_new_k(F, deal_k)
    F.hide_t = (1.0, hide)
    r = broker.enter("long", 38000, 60, sl_points=200, qty=10)
    F.hide_t, F.policy = None, None
    return F, r


# (a) 平倉中途丟例外
F = fresh3(mini=False)
broker._RECENT_CLOSE.update(at=0.0, unclean=False)
broker.enter("long", 38000, 60, sl_points=200, qty=10)
_o = broker._own_cancel_targets


def boom(pos):
    raise RuntimeError("假的例外")
broker._own_cancel_targets = boom
try:
    broker.close("sl")
    ex = None
except RuntimeError as e:
    ex = str(e)
broker._own_cancel_targets = _o
print("  (a) 平倉丟例外=%s ⇒ 平倉進行中=%s、close 鎖可再拿=%s" % (ex, broker._CLOSE_INFLIGHT["on"], broker._close_lock.acquire(blocking=False)))
try:
    broker._close_lock.release()
except RuntimeError:
    pass
chk("Q9-2a 例外之後「平倉進行中」有清掉", broker._CLOSE_INFLIGHT["on"], False)
broker._close_fail["at"] = 0.0
ok, err = broker.close("sl")
chk("Q9-2a 例外之後再停損照樣平得掉", (ok, F.eq()), (True, {}))
# (b) 平倉失敗（券商問不到）⇒ 冷卻 ⇒ 下一趟保守估計補掛停利
F = fresh3(mini=False)
broker._RECENT_CLOSE.update(at=0.0, unclean=False)
broker.enter("long", 38000, 60, sl_points=200, qty=10)
F.pos_fail = 99
ok1, err1 = broker.close("sl")
F.pos_fail = 0
broker._close_fail["at"] = 0.0
ok2, err2 = broker.close("sl")
print("  (b) 第一次 close=%s（%s）平倉進行中=%s；第二次 close=%s 券商=%s" % (ok1, (err1 or "")[:30], broker._CLOSE_INFLIGHT["on"], ok2, F.hold))
chk("Q9-2b 失敗後「平倉進行中」有清掉、之後平得掉", (broker._CLOSE_INFLIGHT["on"], ok2, F.eq()), (False, True, {}))
tick(broker, 45)
F, r = est_entry(10)
tick(broker, 4)
tg = [(x[2], x[1].order.quantity, str(x[1].status.status)) for x in F.targets]
print("  (c) 下一趟保守估計：補掛停利=%s" % tg)
chk("Q9-2c 下一趟照常補掛停利", tg, [("Sell", 10, "OrderStatus.Submitted")])

# ======================================================================================
tag("9-3  第八輪 Q8-1 情境（一口一口平、第一張沒撮到＋狀態永遠不回報）：多久平完")
for qty in (3, 10):
    for mod, mn in ((bm, "main"), (broker, "branch")):
        F = fresh3(mini=False, mod=mod)
        recovered_multi(mod, F, qty)
        F.mkt = {"TMF": 37801.0, "MXF": 37800.0}
        nf = [True]

        def pol(p, r, F=F, nf=nf):
            if r["oc"] == "Cover" and r["pt"] != "LMT" and nf[0]:
                nf[0] = False
                return 0, F.mkt[p]
            return r["q"], F.mkt[p]
        F.policy = pol
        F.hide_t = (1.0, 9999.0)
        T0 = CLK.t
        fl = stop_until_flat(mod, F, T0, horizon=400)
        F.hide_t = None
        print("  %2d 口 %-6s 平完 %s 秒 平過頭=%s" % (qty, mn, fl, F.bad[:1]))
        if mn == "branch":
            say(fl is not None and fl <= 60 + 15 + 9 * qty + 30, "Q9-3 %d 口 branch 有上限地平完" % qty, fl)
            chk("Q9-3 %d 口 branch 沒有平過頭" % qty, F.bad, [])

# ======================================================================================
tag("9-4  回歸：Q5-4 600 輪對照 main（6d708a812）")
_s5b = _s5[_s5.index('tag("5-4'):_s5.index('# ======================================================================================', _s5.index('tag("5-4'))]
exec(compile(_s5b.replace('chk("Q5-4', 'chk("Q9-4'), "q54", "exec"))   # noqa: S102

print("\n%d passed" % N_OK[0])
print("ALL PASS" if not FAIL else "%d FAIL: %s" % (len(FAIL), FAIL))
sys.exit(1 if FAIL else 0)
