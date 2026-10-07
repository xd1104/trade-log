# -*- coding: utf-8 -*-
"""
【帳戶】目標卡的守衛（2026-09-30 加）。⛔ 離線、⛔ 不連永豐、⛔ 金額全是捏的整數。

守的是：
  ① 階梯跟 CLAUDE.md 09-30 積極版一字不差（門檻、口數、要補錢機率）。
  ② 兩條計畫線：保守線＝只存錢（純算術）；目標線照升級規則（一次一級、升完 3 個月不升）。
  ③ 「現在在哪」「超前／落後」「每月實際（扣掉存入）」的算式。
  ④ ⛔ 用字：不准出現預測／期望值／建議這類字（CLAUDE.md 開頭那條鐵律）。
  ⑤ 前端真的把它畫出來、而且走的是 /api/account/hist（10 分鐘一次），⛔ 沒有掛進 0.5 秒的 /api/state。

怎麼跑（PowerShell）：
    $env:PYTHONIOENCODING="utf-8"; $env:PYTHONUTF8="1"
    & "…\\trade-log\\.venv\\Scripts\\python.exe" "…\\tools\\shioaji\\test_goal.py"
"""
import json
import pathlib
import sys
from datetime import date, datetime

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import goal as G                    # noqa: E402

FAIL = 0


def say(cond, name, extra=""):
    global FAIL
    FAIL += not cond
    print(("  OK   " if cond else "  FAIL ") + name + (f"  {extra}" if extra else ""))


def chk(name, got, want):
    global FAIL
    ok = got == want
    FAIL += not ok
    print(("  OK   " if ok else "  FAIL ") + name + ("" if ok else f"  (得到 {got!r}，期待 {want!r})"))


print("=== ① 階梯 ＝ CLAUDE.md 09-30 積極版 ===")
chk("  門檻", [L[0] for L in G.LADDER],
    [70000, 100000, 150000, 200000, 250000, 300000, 350000, 400000, 450000, 500000])
chk("  口數", ["%d＋%d" % (L[1], L[2]) for L in G.LADDER],
    ["1＋1", "2＋1", "3＋1", "4＋2", "5＋2", "6＋3", "7＋3", "8＋3", "9＋4", "10＋4"])
chk("  要補錢機率", [L[3] for L in G.LADDER], [4, 15, 12, 15, 12, 15, 13, 12, 13, 12])
chk("  1＋1 每月（對 capital_ladder_out 的 2,641）", G.monthly_of(1, 1), 2660)
chk("  level_of 邊界", [G.level_of(69999), G.level_of(70000), G.level_of(99999), G.level_of(500000)],
    [0, 1, 1, 10])

print("\n=== ② 兩條計畫線 ===")
P = G.plan(40000, date(2026, 9, 30), months=12)
chk("  保守線＝只存錢", [p["safe"] for p in P[:4]], [40000, 53000, 66000, 79000])
chk("  第一個月目標線＝存錢＋1＋1 的獲利", P[1]["target"], 40000 + 13000 + 2660)
say(all(P[i]["target"] >= P[i]["safe"] for i in range(len(P))), "  目標線永遠不低於保守線")
chk("  月底日期會夾住（01-31 ＋1 月 ⇒ 02-28）", G._add_months(date(2027, 1, 31), 1), date(2027, 2, 28))
# 升級冷卻：從 10 萬出發 ⇒ 馬上就夠 15 萬也要等 3 個月
P2 = G.plan(160000, date(2026, 9, 30), months=8)
lv = [p["lvl"] for p in P2]
ups = [i for i in range(1, len(lv)) if lv[i] > lv[i - 1]]
say(all(b - a >= G.UPGRADE_GAP for a, b in zip(ups, ups[1:])), "  升完一級至少 3 個月不再升", str(lv))
say(all(lv[i] - lv[i - 1] <= 1 for i in range(1, len(lv))), "  一次只升一級")

print("\n=== ③ 現在在哪／超前落後／每月實際 ===")
rows = [{"date": "2026-09-21", "equity": 40000, "deposit": 0},
        {"date": "2026-09-30", "equity": 38000, "deposit": 1000},
        {"date": "2026-10-15", "equity": 52000, "deposit": 13000},
        {"date": "2026-10-31", "equity": 53000, "deposit": 0}]
V = G.view(rows, 54000.0, 0, datetime(2026, 11, 3, 15))
say(V["ok"], "  算得出來")
chk("  起點 ＝ 09-30 那一列（⛔ 不是寫死的金額）", (V["start"], V["start_eq"]), ("2026-09-30", 38000))
chk("  現在在第 0 級、差 16,000 到 7 萬", (V["lvl"], V["gap"], V["next"]), (0, 16000, 70000))
say("落後目標線" in V["vs"] or "超前目標線" in V["vs"], "  有講超前還是落後", V["vs"])
say("計畫 13,000" in (V["since"] or ""), "  計畫存入按整月算（11-03 ⇒ 過了 1 個月）", V["since"])
mon = {m["ym"]: m for m in V["months"]}
chk("  九月：40,000 → 38,000、存入 1,000 ⇒ 賺賠 −3,000", mon["2026-09"]["net"], -3000)
chk("  十月：月初＝九月底 38,000、存 13,000、月底 53,000 ⇒ +2,000", mon["2026-10"]["net"], 2000)
chk("  十一月：沒紀錄也用現在的權益 ⇒ +1,000", (mon["2026-11"]["net"], mon["2026-11"]["live"]), (1000, True))
chk("  起算日當天：兩條線同一點", G.view(rows, 38000.0, 0, datetime(2026, 9, 30, 15))["vs"],
    "今天是起算日：兩條計畫線都從 38,000 元出發。")
chk("  問不到券商 ⇒ 退回最後一列", G.view(rows, None, 0, datetime(2026, 11, 3, 15))["eq"], 53000)
chk("  什麼都沒有 ⇒ 照實說", G.view([], None)["ok"], False)
V2 = G.view(rows, 123000.0, 0, datetime(2026, 11, 3, 15))
chk("  12.3 萬 ⇒ 第 2 級、下一級 15 萬", (V2["lvl"], V2["lots"], V2["next"]), (2, "2＋1", 150000))
chk("  已到的級數有打勾日期", [s["done"] is not None for s in V2["steps"][:3]], [True, True, False])
V3 = G.view(rows, 520000.0, 0, datetime(2026, 11, 3, 15))
say(V3["next"] is None and "最後一級" in V3["pos"], "  超過 50 萬 ⇒ 最後一級", V3["pos"])
say(all(p["t"] is None or p["t"] < G.GOAL * 1.2 for p in V["line"]), "  目標線到 50 萬就收（圖的刻度不會被撐爆）")

print("\n=== ③b ⭐ 2026-10-07 券商漏寫的入金（09-30 傍晚存 1.3 萬，兩列的出入金都是 0）===")
# 照他真的那幾天的形狀（金額縮整）：09-30 14:00 記 37,560；當晚入金 13,000；10-01 日盤賺 2,420、成本 56
_R = [{"date": "2026-09-29", "equity": 37560, "deposit": 3006, "settle_pl": -2540, "fee": 36, "tax": 20, "float_pl": 0},
      {"date": "2026-09-30", "equity": 37560, "deposit": 0, "settle_pl": 0, "fee": 0, "tax": 0, "float_pl": 0},
      {"date": "2026-10-01", "equity": 52924, "deposit": 0, "settle_pl": 2420, "fee": 36, "tax": 20, "float_pl": 0},
      {"date": "2026-10-02", "equity": 53138, "deposit": 0, "settle_pl": 270, "fee": 36, "tax": 20, "float_pl": 0}]
chk("  推得出 10-01 那段的入金", G.infer_deposit(_R[1], _R[2]), 13000.0)
chk("  ⛔ 券商有寫就照券商（09-29 寫 3,006）", G.eff_deposit(_R[0], dict(_R[0], deposit=3006)), 3006.0)
chk("  ⛔ 一般交易日推出來 ≈ 0 ⇒ 不當成入金", G.eff_deposit(_R[2], _R[3]), 0.0)
_gap = dict(_R[2], date="2026-10-03")
chk("  負控組：⛔ 不相鄰（中間有沒開面板的日子）⇒ 不推、照券商寫的 0", G.eff_deposit(_R[0], _gap), 0.0)
_live = {"equity_amount": 51816.0, "deposit_withdrawal": 0.0, "future_settle_profitloss": -1680.0,
         "fee": 36.0, "tax": 20.0, "future_open_position": 0.0}
_m = {x["ym"]: x for x in G.months_actual(_R + [{"date": "2026-10-06", "equity": 53552, "deposit": 0,
                                                    "settle_pl": 0, "fee": 0, "tax": 0, "float_pl": 0}],
                                          51816.0, 0.0, date(2026, 10, 7), live=_live)}
chk("  十月：月初＝09-30 那列、扣掉推出來的 1.3 萬", (_m["2026-10"]["base"], _m["2026-10"]["dep"]), (37560, 13000))
_V = G.view(_R, 53138.0, 0, datetime(2026, 10, 2, 15))
say("交易賺賠 +2,578" in (_V["since"] or ""), "  ⭐ 目標卡：1.3 萬⛔ 不再算成交易賺的（2,364＋214）", _V["since"])

print("\n=== ④ ⛔ 用字 ===")
_txt = json.dumps([V, V2, V3], ensure_ascii=False)
for w in ("建議", "推薦", "會賺", "應該進場", "最佳", "預測", "期望值", "訊號強度", "勝率"):
    chk(f"  沒有「{w}」", w in _txt, False)

print("\n=== ⑤ 前端 ===")
import live_panel as LP             # noqa: E402
say("function goalHTML(" in LP.PAGE and 'id="acctgoal"' in LP.PAGE, "  頁面有目標卡與容器")
say("ACCT.goal=(x&&x.goal)" in LP.PAGE, "  目標資料走 /api/account/hist（10 分鐘一次）")
_st = LP.PAGE.split("async function tick(")[1].split("function ")[0]
say("goalHTML(ACCT.goal)" in _st and "/api/account/goal" not in LP.PAGE,
    "  ⛔ 0.5 秒的 tick 只重畫、不另外打端點")
_g = LP.goal_view(datetime(2026, 11, 3, 15))
say(isinstance(_g, dict) and "ok" in _g, "  live_panel.goal_view() 壞了也回一句話、不丟例外")

print("\n" + ("全部通過 ✅" if not FAIL else "❌ 有 %d 項沒過" % FAIL))
sys.exit(1 if FAIL else 0)
