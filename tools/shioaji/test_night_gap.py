# -*- coding: utf-8 -*-
"""
探針：夜盤跟勢 21:40 判定前，「走勢」即時數字（night_fire.trend_gap，2026-10-21）。

跑法：python -X utf8 test_night_gap.py
"""
import sys
from datetime import date, datetime

import night_fire as NF

bad = 0


def chk(name, got, want):
    global bad
    ok = got == want
    if not ok:
        bad += 1
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else "  got=%r want=%r" % (got, want)))


E = date(2026, 10, 21)                          # 冬令還沒開始 ⇒ 21:40 判定、21:10 起算
ON = {"on": True, "method": "R"}
NF.past_sigs = lambda e: ([100.0 + i for i in range(40)], "2026-10-20")
NF.ref_price = lambda e: (47000.0, "21:09")
NF._CFG["quote_fn"] = lambda: (47080.0, 1.0)
NF._GAP_THR.update(E=None, thr=None)
thr = round(NF.trend_rule.threshold([100.0 + i for i in range(40)]), 1)

print("\n  ── 什麼時候端 ──")
g = NF.trend_gap(E, datetime(2026, 10, 21, 21, 25), ON, [])
chk("21:25 ⇒ 現在 +80 點", (g or {}).get("now_pts"), 80.0)
chk("21:25 ⇒ 門檻跟送單同一個算法", (g or {}).get("need_pts"), thr)
chk("21:25 ⇒ 判定時刻 21:40", (g or {}).get("at"), "21:40")
chk("21:09 還沒開始算 ⇒ None", NF.trend_gap(E, datetime(2026, 10, 21, 21, 9), ON, []), None)
chk("21:40 到了（判定那一刻）⇒ None", NF.trend_gap(E, datetime(2026, 10, 21, 21, 40), ON, []), None)
chk("已經有定論 ⇒ None", NF.trend_gap(E, datetime(2026, 10, 21, 21, 25), ON, [{"E": "2026-10-21", "rec": "skip"}]), None)
chk("開的是台積電快攻 ⇒ None", NF.trend_gap(E, datetime(2026, 10, 21, 21, 25), {"on": True, "method": "T"}, []), None)
chk("關著 ⇒ None", NF.trend_gap(E, datetime(2026, 10, 21, 21, 25), {"on": False, "method": None}, []), None)

print("\n  ── 拿不到就不端（⛔ 不猜） ──")
NF._CFG["quote_fn"] = lambda: (47080.0, 60.0)
chk("報價 60 秒沒更新 ⇒ None", NF.trend_gap(E, datetime(2026, 10, 21, 21, 25), ON, []), None)
NF._CFG["quote_fn"] = lambda: (47080.0, 1.0)
NF.ref_price = lambda e: (None, None)
chk("拿不到 30 分鐘前的價 ⇒ None", NF.trend_gap(E, datetime(2026, 10, 21, 21, 25), ON, []), None)
NF.ref_price = lambda e: (47000.0, "21:09")
NF._GAP_THR.update(E=None, thr=None)
NF.past_sigs = lambda e: ([100.0] * 5, "2026-10-20")
chk("歷史不夠 20 晚 ⇒ None", NF.trend_gap(E, datetime(2026, 10, 21, 21, 25), ON, []), None)

print("\n  ── state() 帶得出、壞了不拖垮 ──")
NF.trend_gap = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
chk("trend_gap 丟例外 ⇒ _gap_safe 回 None", NF._gap_safe(E, datetime(2026, 10, 21, 21, 25), ON, []), None)
src = open(NF.__file__, encoding="utf-8").read()
chk("state() 有端 gap", '"gap": _gap_safe(E, now, a, recent)' in src, True)

print("\n%s" % ("全部通過" if not bad else "❌ %d 項沒過" % bad))
sys.exit(1 if bad else 0)
