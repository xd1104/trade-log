# -*- coding: utf-8 -*-
"""
探針：【帳戶】金流明細（cashflow.py）＋ goal 推出入金的週末修正（2026-10-21）。
⛔ repo 是公開的 ⇒ 這裡的金額全是捏造的（形狀照真實帳本：週末重複列、漏寫的入金、2 口單、夜盤跨日）。

跑法：python -X utf8 test_cashflow.py
"""
import sys
from pathlib import Path

import cashflow as C
import goal as G

bad = 0


def chk(name, got, want):
    global bad
    ok = got == want
    if not ok:
        bad += 1
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else "  got=%r want=%r" % (got, want)))


def row(d, eq, dep=0.0, pl=0.0, fee=0.0, tax=0.0):
    return {"date": d, "equity": eq, "deposit": dep, "settle_pl": pl, "float_pl": 0.0, "fee": fee, "tax": tax}


# 捏造的一段：09-30 起點 10,000
#  10-01（四）券商漏寫入金 5,000，日盤 +100 點 ×1 ⇒ +1,000，費稅 56
#  10-02（五）日盤 −50 點 ×2 ⇒ −1,000，費稅 112
#  10-03（六）夜盤（10-02 晚）+300 點 ×1 ⇒ +3,000，費稅 54
#  10-04（日）券商把週六的 settle／fee 原封不動再給一次，權益沒變
#  10-05（一）券商的 settle 含週六那筆（3,000）＋今天日盤 −20 點 ×1（−200）⇒ 2,800；費稅含週六的 ⇒ 108
rows = [
    row("2026-09-30", 10000),
    row("2026-10-01", 15944, 0, 1000, 36, 20),
    row("2026-10-02", 14832, 0, -1000, 72, 40),
    row("2026-10-03", 17778, 0, 3000, 36, 18),
    row("2026-10-04", 17778, 0, 3000, 36, 18),
    row("2026-10-05", 17524, 0, 2800, 72, 36),
]
trades = {
    "2026-10-01": [{"date": "2026-10-01", "dir": "long", "qty": 1, "entry": 40000.0, "entry_time": "09:15:01",
                    "exit": 40100.0, "exit_time": "10:00:00", "reason": "tp", "points": 100.0}],
    "2026-10-02": [{"date": "2026-10-02", "dir": "long", "qty": 2, "entry": 40000.0, "entry_time": "09:03:30",
                    "exit": 39950.0, "exit_time": "09:40:00", "reason": "sl", "points": -50.0}],
    "2026-10-03": [{"date": "2026-10-03", "dir": "short", "qty": 1, "entry": 40300.0, "entry_time": "21:40:01",
                    "exit": 40000.0, "exit_time": "04:58:00", "reason": "night_eod", "points": 300.0}],
    "2026-10-05": [{"date": "2026-10-05", "dir": "long", "qty": 1, "entry": 40000.0, "entry_time": "09:07:00",
                    "exit": 39980.0, "exit_time": "13:43:36", "reason": "eod", "points": -20.0}],
}
day_res = {"2026-10-01": [{"rec": "result", "ok": True, "method": "U", "cand": "rev", "entry": 40000.0}],
           "2026-10-02": [{"rec": "result", "ok": True, "method": "U", "cand": "fast", "entry": 40000.0}],
           "2026-10-05": [{"rec": "result", "ok": True, "method": "U", "cand": "orb", "entry": 40000.0}]}
night_res = [{"rec": "result", "ok": True, "method": "R", "entry": 40300.0, "E": "2026-10-02"}]

print("\n  ── goal 推出入金：週末重複列不可以變成出金 ──")
chk("10-01 漏寫的入金推得出來（5,000）", round(G.eff_deposit(rows[0], rows[1])), 5000)
chk("10-04（週日）重複週六那列 ⇒ 0", round(G.eff_deposit(rows[3], rows[4])), 0)
chk("10-05（週一）settle 含週六那筆 ⇒ 0", round(G.eff_deposit(rows[4], rows[5])), 0)
chk("10-03（週六）本身 ⇒ 0", round(G.eff_deposit(rows[2], rows[3])), 0)
m = [x for x in G.months_actual(rows, 17524.0, 0) if x["ym"] == "2026-10"][0]
chk("月合計：入金 5,000、實際賺賠 2,524", (m["dep"], m["net"]), (5000, 2524))

out = C.build(rows, trades, day_res, night_res, G.eff_deposit)
M = out["months"]["2026-10"]
print("\n  ── 金流明細：月總結 ──")
chk("入金", M["dep"], 5000)
chk("交易損益（1,000 −1,000 +3,000 −200）", M["trade"], 2800)
chk("手續費＋稅（56+112+54+54）", M["fee"], -276)
chk("沒有對不起來的錢", M["diff"], 0)
chk("本月實際賺賠 ＝ 跟 goal 同一個數", M["net"], m["net"])

D = {d["date"]: d for d in M["days"]}
print("\n  ── 金流明細：逐日 ──")
chk("新到舊排", [d["date"] for d in M["days"]], ["2026-10-05", "2026-10-03", "2026-10-02", "2026-10-01"])
chk("10-04 什麼都沒發生 ⇒ 不列", "2026-10-04" in D, False)
chk("結餘＝那天的權益", D["2026-10-05"]["bal"], 17524)
chk("10-01 入金排第一（券商漏寫 ⇒ 標推算）", D["2026-10-01"]["items"][0], {"k": "dep", "amt": 5000, "inferred": True})
t = D["2026-10-02"]["items"][0]
chk("2 口：金額＝點數×口數×10", (t["pts"], t["qty"], t["amt"]), (-50, 2, -1000))
chk("策略名：fast ⇒ 快攻、rev ⇒ 純回馬、orb ⇒ 開箱",
    (t["name"], D["2026-10-01"]["items"][1]["name"], D["2026-10-05"]["items"][0]["name"]), ("快攻", "純回馬", "開箱"))
n = D["2026-10-03"]["items"][0]
chk("夜盤：盤別、名字、方向、哪一晚", (n["sess"], n["name"], n["dir"], n["night"]), ("夜盤", "跟勢", "做空", "10-02 晚"))
chk("出場原因翻成中文", (n["why"], t["why"]), ("04:58 平", "停損"))
chk("對不到帳本 ⇒ 手動", C._trade_item(dict(trades["2026-10-01"][0], entry=1.0), day_res, night_res)["name"], "手動")

print("\n  ── 對不起來要照實列 ──")
rows2 = rows[:2] + [row("2026-10-02", 14832 + 77, 0, -1000, 72, 40)]
o2 = C.build(rows2, trades, day_res, night_res, G.eff_deposit)
d2 = {d["date"]: d for d in o2["months"]["2026-10"]["days"]}["2026-10-02"]
chk("多出 77（不到推入金的門檻）⇒ 費稅照費率估 104、剩下 +69 列成差額", (d2["diff"], d2["fee"]), (69, -104))
chk("月合計把差額算進去（實際賺賠還是等於權益變化）", o2["months"]["2026-10"]["net"], (15944 - 10000 - 5000) + (14832 + 77 - 15944))

rows3 = rows[:2] + [row("2026-10-02", 14832 + 3000, 0, -1000, 72, 40)]
d4 = {d["date"]: d for d in C.build(rows3, trades, day_res, night_res, G.eff_deposit)["months"]["2026-10"]["days"]}["2026-10-02"]
chk("券商漏寫、推出來的入金要標「推算」", d4["items"][0], {"k": "dep", "amt": 3000, "inferred": True})
rows4 = [rows[0], dict(rows[1], deposit=5000.0)]
d5 = C.build(rows4, trades, day_res, night_res, G.eff_deposit)["months"]["2026-10"]["days"][0]
chk("券商有寫的入金不標", d5["items"][0], {"k": "dep", "amt": 5000})

print("\n  ── 今天用券商現在那一份取代 14:00 那列 ──")
live = {"equity": 17000.0, "deposit": 0.0, "settle_pl": 0.0, "float_pl": 0.0, "fee": 0.0, "tax": 0.0}
o3 = C.build(rows, {}, {}, [], G.eff_deposit, live=live, today="2026-10-05")
d3 = {d["date"]: d for d in o3["months"]["2026-10"]["days"]}["2026-10-05"]
chk("結餘用現在的權益", d3["bal"], 17000)

print("\n  ── 守門：面板有接、前端只畫 ──")
src = (Path(__file__).parent / "live_panel.py").read_text(encoding="utf-8")
chk("/api/account/hist 端出 cash", '"cash": cashflow_view()' in src, True)
chk("前端有金流明細卡", "function cashHTML(" in src, True)

print("\n%s" % ("全部通過" if not bad else "❌ %d 項沒過" % bad))
sys.exit(1 if bad else 0)
