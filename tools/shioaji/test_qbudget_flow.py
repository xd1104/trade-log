# -*- coding: utf-8 -*-
"""
⭐ 2026-11-27 查詢預算：用假券商（test_qa_mix.py 那套）實際跑「送單前檢查 → 進場 → 持倉（背景對帳照新節奏）
→ 停利成交或停損 → 結束」，把每一次叫券商都經過 qbudget.Counted 記下來，算出**一趟**要問幾次。
⛔ 不連永豐、不碰 8770、開關檔全在暫存資料夾。假券商成交是立刻的（真的券商回報慢時次數會多一些）。
跑法：.venv\\Scripts\\python.exe -X utf8 tools\\shioaji\\test_qbudget_flow.py
"""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
_src = (HERE / "test_qa_mix.py").read_text(encoding="utf-8")
_harness = _src.split("# ===================== QA attack scenarios")[0]
exec(compile(_harness, "test_qa_mix_harness", "exec"))   # noqa: S102

import qbudget as Q      # noqa: E402

Q.DIR = TMP / "usage"
Q.SAVE_EVERY = 1e9
broker.clock_skew = lambda: 0.0
broker.entries_today = lambda: 0
RES = {}


def reset_count():
    Q._S.update({"month": None, "days": {}, "by": {}, "local": 0, "warned": [], "dirty": False, "saved": 0.0})


def run(name, mini, qty, ending, hold_s):
    F = fresh(mini=mini)
    api = Q.Counted(F)
    broker.configure(api, C["TMF"], {"MXF": C["MXF"]})
    broker._LAST_RECONCILE.update(at=CLK.t, unknown=False, ok_once=True)
    broker._RECON_KICK.update(due=[], why=None, cp=set())
    reset_count()
    ok, why = broker.can_enter(38000.0, True)
    assert ok, why
    broker.enter("long", 38000, 60, sl_points=200, qty=qty)
    t_end = CLK.t + hold_s
    filled = False
    while CLK.t < t_end:
        if ending == "tp" and not filled and CLK.t >= t_end - 600:
            for p in ("MXF", "TMF"):
                h = F.hold[p]
                if h:
                    F.fill_target(p, abs(h))
            broker.recon_kick("券商通知 成交")           # 正式環境是 on_order_event 叫的
            filled = True
        broker.reconcile_tick()
        CLK.t += 0.5
    if ending == "sl":
        r = broker.close("sl")
        for _ in range(4):                           # 停損後背景照常再問幾次（提醒）
            CLK.t += 31
            broker.reconcile_tick()
    v = Q.view()
    RES[name] = v
    left = {p: F.hold[p] for p in F.hold if F.hold[p]}
    print("  %-26s 共 %3d 次  %s  剩下部位=%s" % (name, v["total"], dict(sorted(v["by_method"].items())), left or "空手"))
    return v["total"], left


print("每一趟要問券商幾次（假券商、成交立刻回報）：")
DAY = 4 * 3600 + 40 * 60
NIGHT = 6 * 3600 + 18 * 60
out = [
    run("微台 1 口 日盤停損", False, 1, "sl", DAY),
    run("微台 1 口 日盤停利", False, 1, "tp", DAY),
    run("微台 4 口 夜盤停損", False, 4, "sl", NIGHT),
    run("混搭 11 口 日盤停損", True, 11, "sl", DAY),
    run("混搭 11 口 日盤停利", True, 11, "tp", DAY),
    run("小台 10 口 日盤停損", True, 10, "sl", DAY),
]
FAIL = [n for n, (tot, left) in zip(RES, out) if left]
print()
print("全部通過（每一趟最後都空手）" if not FAIL else "沒平乾淨：%s" % "、".join(FAIL))
sys.exit(1 if FAIL else 0)
