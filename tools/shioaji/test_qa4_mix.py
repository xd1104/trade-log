# -*- coding: utf-8 -*-
"""
第四輪獨立驗收攻擊測試（lab-qa，2026-11-06）。⛔ 不連永豐、⛔ 開關檔全在暫存資料夾、⛔ 不碰 trade-log 面板。
沿用 test_qa_mix.py 的假券商／假時鐘＋test_qa3_mix.py 的 Fake3（不載入 main，避免讀到正在跑的面板）。
Run: .venv/Scripts/python.exe -X utf8 tools/shioaji/test_qa4_mix.py
"""
import json
import pathlib
import random
import sys

HERE = pathlib.Path(__file__).resolve().parent
_src = (HERE / "test_qa_mix.py").read_text(encoding="utf-8")
exec(compile(_src.split("# ===================== QA attack scenarios")[0], "qa_harness", "exec"))   # noqa: S102
_s3 = (HERE / "test_qa3_mix.py").read_text(encoding="utf-8")
_a = _s3.index("def tag(n):")
_b = _s3.index("# ---- 1. ")
exec(compile(_s3[_a:_b], "qa3_harness", "exec"))   # noqa: S102
_c = _s3.index("def pol_new(rng, F):")
_d = _s3.index("MIXQ = [")
exec(compile(_s3[_c:_d], "qa3_rand", "exec"))      # noqa: S102


def tag(n):
    print("=== Q%s ===" % n)


def show(mod=broker):
    tr = trades_of(mod)
    return [{k: t.get(k) for k in ("qty", "entry", "exit", "points", "reason", "product", "legs")} for t in tr]


def run_mode():
    return sys.argv[1] if len(sys.argv) > 1 else "all"


# ---- main 對照組：從 git 取 main 的 broker.py 到暫存資料夾（⛔ 不讀正在跑的 trade-log 面板那份）
import importlib.util
import subprocess
_mp = TMP / "broker_main_q4.py"
_mp.write_bytes(subprocess.run(["git", "show", "main:tools/shioaji/broker.py"], cwd=str(HERE),
                               capture_output=True, check=True).stdout)
_spec = importlib.util.spec_from_file_location("broker_main_q4", str(_mp))
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
assert bm.HERE != broker.HERE or True


def tick(mod, n=1, dt=3.0):
    """模擬面板主迴圈：每 dt 秒一次 reconcile_tick。"""
    for _ in range(n):
        CLK.t += dt
        mod._LAST_RECONCILE["at"] = 0
        mod.reconcile_tick()


def PP(mod=broker):
    p = mod._state["position"]
    return None if p is None else {k: p.get(k) for k in ("qty", "product", "own", "recovered", "sl_points", "sl_src",
                                                         "handover", "multi_unknown")}


def restart(mod=broker):
    mod._state["position"] = None
    mod._META.update(last=None, swept=False)
    mod._close_fail["at"] = 0.0
    mod._CLOSE_INFLIGHT["on"] = False


def outcome(mod, F, sgn):
    tr = trades_of(mod)
    nq = F.new_fill["TMF"] + F.new_fill["MXF"] * 5
    return {"bad": list(F.bad), "left": dict((k, v) for k, v in F.hold.items() if v), "rows": len(tr),
            "rq": sum(int(t.get("qty") or 0) for t in tr), "nq": nq,
            "pts": round(sum((t.get("points") or 0) * int(t.get("qty") or 0) for t in tr), 2),
            "real": round(F.realized_eqpts(sgn), 2), "zero_rows": sum(1 for t in tr if not t.get("qty"))}


# ======================================================================================
tag("4-1  正常情況：微台多口（現在真單 10＋4）branch vs main，回報 0～2 秒、IOC 部分成交仍是 Filled、停利部分成交")
diff_cnt, worse = 0, []
N1 = 300
for seed in range(N1):
    res = {}
    for mod, name in ((bm, "main"), (broker, "branch")):
        rng = random.Random(9000 + seed)
        F = fresh3(mini=False, rng=rng, mod=mod)
        qq = rng.choice([2, 3, 4, 5, 6, 7, 8, 9, 10, 10, 10, 4, 4])
        d = rng.choice(["long", "short"])
        sgn = 1 if d == "long" else -1
        if rng.random() < 0.3:
            F.policy = pol_new(rng, F)
        F.lag = rng.choice([0.0, 0.0, 1.0, 2.0])
        mod.enter(d, 38000, 60, sl_points=200, qty=qq)
        F.policy = None
        if sum(F.new_fill.values()) == 0:
            res = None
            break
        for _ in range(rng.randint(1, 4)):
            tick(mod, 1, rng.choice([1.0, 3.0, 3.0, 10.0]))
            if rng.random() < 0.3 and F.hold["TMF"]:
                try:
                    F.fill_target("TMF", rng.randint(1, abs(F.hold["TMF"])))
                except AssertionError:
                    pass
        if rng.random() < 0.4:
            F.policy = pol_cover(rng, F)
        F.mkt = {"TMF": 38001.0 - sgn * 150, "MXF": 38000.0 - sgn * 150}
        for attempt in range(6):
            if mod._state["position"] is None and not F.eq():
                break
            mod.close("sl")
            tick(mod, 6)
            if attempt == 2:
                F.policy = None
        tick(mod, 50)
        res[name] = outcome(mod, F, sgn)
    if res is None:
        continue
    a, b = res["main"], res["branch"]
    if (a["left"], a["rq"], a["pts"], bool(a["bad"])) != (b["left"], b["rq"], b["pts"], bool(b["bad"])):
        diff_cnt += 1
        if b["bad"] or b["left"] or b["rq"] != b["nq"] or b["zero_rows"] or abs(b["pts"] - b["real"]) > 0.06 * b["nq"] + 0.02:
            worse.append((seed, a, b))
print("  與 main 結果不同：%d／%d；branch 比較糟：%d" % (diff_cnt, N1, len(worse)))
for w in worse[:3]:
    print("    seed", w[0], "\n      main  ", w[1], "\n      branch", w[2])
chk("Q4-1 正常情況 branch 沒有比 main 糟（平過頭／沒平完／成績單口數點數）", len(worse), 0)

# ======================================================================================
tag("4-2  進場單明細比部位晚 0.5～2 秒（回報 0～2 秒的正常範圍），第一次對帳剛好落在這段：微台 10 口")
fake_fin = {"main": 0, "branch": 0}
lost_sl = {"main": 0, "branch": 0}
N2 = 0
for lagd in (0.5, 1.0, 1.5, 2.0):
    for first in (0.3, 1.0, 2.5):
        N2 += 1
        for mod, name in ((bm, "main"), (broker, "branch")):
            F = fresh3(mini=False, mod=mod)
            F.hide_t = (1.0, lagd)
            mod.enter("long", 38000, 60, sl_points=200, qty=10)
            F.hide_t = None
            tick(mod, 1, first)
            p = mod._state["position"]
            if any(not t.get("qty") for t in trades_of(mod)):
                fake_fin[name] += 1
            tick(mod, 3)
            p = mod._state["position"]
            if not (isinstance(p, dict) and p.get("sl_points") == 200.0 and p.get("qty") == 10):
                lost_sl[name] += 1
print("  %d 種時序：成績單多一列 0 口假紀錄 main=%d branch=%d；部位變成撿回（沒有程式停損 200 點）main=%d branch=%d"
      % (N2, fake_fin["main"], fake_fin["branch"], lost_sl["main"], lost_sl["branch"]))
chk("Q4-2 進場明細晚到不會被當成「這一趟已經平完」（0 口假成績單）", fake_fin["branch"], 0)
chk("Q4-2 進場明細晚到後部位仍是程式的（停損 200 點、10 口）", lost_sl["branch"], 0)

tag("4-2b 承上：被當成撿回部位之後觸發停損 ⇒ 平得掉嗎（main 對照）")
for mod, name in ((bm, "main"), (broker, "branch")):
    F = fresh3(mini=False, mod=mod)
    F.hide_t = (1.0, 8.0)
    mod.enter("long", 38000, 60, sl_points=200, qty=10)
    F.hide_t = None
    tick(mod, 3)
    n0 = len(F.sent)
    ok, err = mod.close("sl")
    print("  %-6s close=%s %s 送出=%s 券商剩=%s" % (name, ok, (err or "")[:60], S(F, n0), F.hold))
    if name == "branch":
        chk("Q4-2b 停損一次平掉 10 口（main 一樣）", F.eq(), {})

# ======================================================================================
tag("4-3  停損當下進場明細只回報 6／10 口（部位已是 10 口）")
for mod, name in ((bm, "main"), (broker, "branch")):
    F = fresh3(mini=False, mod=mod)
    mod.enter("long", 38000, 60, sl_points=200, qty=10)
    t = [x for x in Trade._live if x.order.quantity == 10 and x.status.deals][-1]
    full = t.status.deals
    t.status.deals, t.status.status = [Deal(full[0].price, 6)], "OrderStatus.PartFilled"
    CLK.t += 1
    n0 = len(F.sent)
    ok, err = mod.close("sl")
    sent = S(F, n0)
    tick(mod, 5)
    mid = (dict(F.hold), PP(mod))
    t.status.deals, t.status.status = full, "OrderStatus.Filled"
    tick(mod, 50)
    print("  %-6s close=%s 送出=%s 15 秒後券商=%s 部位=%s ⇒ 165 秒後 %s" % (name, ok, sent, mid[0], mid[1], PP(mod)))
    if name == "branch":
        chk("Q4-3 停損把 10 口平完（不留沒有停損的 4 口）", mid[0]["TMF"], 0)

# ======================================================================================
tag("4-4  平倉單送出後回報慢、面板重啟、list_trades 問不到那張平倉單")
for real_fill in (True, False):
    F = fresh3(mini=False)
    broker.enter("long", 38000, 60, sl_points=200, qty=10)
    tick(broker, 2)
    if not real_fill:
        F.policy = lambda pr, r: (0, F.mkt[pr]) if r["oc"] == "Cover" else (r["q"], F.mkt[pr])
    F.hide_t = (1.0, 20.0)
    broker.close("sl")
    F.hide_t, F.policy = None, None
    restart()
    F.no_list_trades = True
    tick(broker, 60)
    broker._close_fail["at"] = 0.0
    n0 = len(F.sent)
    ok, err = broker.close("sl")
    print("  平倉單其實%s：180 秒後部位=%s 券商=%s 再停損=%s %s 送出=%s" % (
        "成交了" if real_fill else "沒成交", PP(), F.hold, ok, (err or "")[:50], S(F, n0)))
    if real_fill:
        chk("Q4-4a 券商已空手 ⇒ 不會永遠掛著假部位（擋住下一次進場）", PP(), None)
    else:
        chk("Q4-4b 券商還有 10 口 ⇒ 停損送得出平倉單", F.eq(), {})

# ======================================================================================
tag("4-5  重啟、list_trades 問不到停利單 ⇒ 停損後停利單有沒有撤／有沒有講")
F = fresh3(mini=False)
broker.enter("long", 38000, 60, sl_points=200, qty=10)
tick(broker, 2)
restart()
F.no_list_trades = True
tick(broker, 6)
ok, err = broker.close("sl")
live = [x[1].status.status for x in F.targets]
print("  close=%s 訊息=%r 停利單狀態=%s" % (ok, err, live))
say(all("Cancel" in s for s in live) or bool(err), "Q4-5 停利單撤掉了，或至少有講「停利單沒撤掉」", (live, err))

# ======================================================================================
tag("4-6  進場明細只回報 6／10 口超過確認期（45 秒）⇒ 被採信成「晚到 +4」，之後明細補齊 ⇒ 會不會重複算、成績單對不對")
F = fresh3(mini=False)
broker.enter("long", 38000, 60, sl_points=200, qty=10)
t = [x for x in Trade._live if x.order.quantity == 10 and x.status.deals][-1]
full = t.status.deals
t.status.deals, t.status.status = [Deal(full[0].price, 6)], "OrderStatus.PartFilled"
tick(broker, 25)
adj1 = list((broker._state["position"] or {}).get("own_adj") or [])
t.status.deals, t.status.status = full, "OrderStatus.Filled"
tick(broker, 2)
q_mid = (broker._state["position"] or {}).get("qty")
F.mkt = {"TMF": 37851.0, "MXF": 37850.0}
n0 = len(F.sent)
ok, err = broker.close("sl")
sent = S(F, n0)
tick(broker, 40)
tr = trades_of()
print("  確認期後的調整=%s 明細補齊後本機口數=%s 停損 close=%s 送出=%s 券商=%s 結束部位=%s" % (
    [(a["p"], a["q"], a["src"]) for a in adj1], q_mid, ok, sent, F.hold, PP()))
print("  成績單：", show())
chk("Q4-6 沒有平過頭", F.bad, [])
chk("Q4-6 券商最後空手", F.eq(), {})
chk("Q4-6 成績單只有一列、口數 10", [int(x.get("qty") or 0) for x in tr], [10])
say(len(tr) == 1 and tr[0].get("points") is not None and abs(tr[0]["points"] - (-150.0)) < 0.01,
    "Q4-6 成績單點數＝實際 -150（不是留白、不是多算一段「別處平掉」）", tr and tr[0].get("points"))

print("\n%d passed" % N_OK[0])
print("ALL PASS" if not FAIL else "%d FAIL: %s" % (len(FAIL), FAIL))
sys.exit(1 if FAIL else 0)
