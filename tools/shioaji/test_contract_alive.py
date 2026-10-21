# -*- coding: utf-8 -*-
"""
探針：結算日傍晚重啟面板不可以挑到已到期的合約（2026-10-21 踩到的坑）。

跑法：python -X utf8 test_contract_alive.py
"""
import sys
from datetime import date, datetime
from types import SimpleNamespace as C

import live_panel as LP

bad = 0


def chk(name, got, want):
    global bad
    ok = got == want
    if not ok:
        bad += 1
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else "  got=%r want=%r" % (got, want)))


oct_ = C(code="TMFJ6", delivery_month="202610", delivery_date="2026/10/21")
nov = C(code="TMFK6", delivery_month="202611", delivery_date="2026/11/18")
octd = C(code="TMFJ6", delivery_month="202610", delivery_date=date(2026, 10, 21))
nodd = C(code="X", delivery_month="202610")

print("\n  ── contract_alive ──")
chk("結算日 13:29 還活著", LP.contract_alive(oct_, datetime(2026, 10, 21, 13, 29, 59)), True)
chk("結算日 13:30 到期了", LP.contract_alive(oct_, datetime(2026, 10, 21, 13, 30, 0)), False)
chk("結算日 17:14（他重啟那一刻）到期了", LP.contract_alive(oct_, datetime(2026, 10, 21, 17, 14)), False)
chk("隔天凌晨到期了", LP.contract_alive(oct_, datetime(2026, 10, 22, 0, 30)), False)
chk("結算日前一天活著", LP.contract_alive(oct_, datetime(2026, 10, 20, 21, 0)), True)
chk("下個月合約活著", LP.contract_alive(nov, datetime(2026, 10, 21, 17, 14)), True)
chk("delivery_date 是 date 型別也判得出來", LP.contract_alive(octd, datetime(2026, 10, 21, 17, 14)), False)
chk("沒有 delivery_date ⇒ 當作活著（不亂丟）", LP.contract_alive(nodd, datetime(2026, 10, 21, 17, 14)), True)

print("\n  ── 守門：pick_live_contract 有先過 contract_alive ──")
src = open(LP.__file__, encoding="utf-8").read()
i = src.index("def pick_live_contract(api):")
body = src[i:src.index("def connect():", i)]
chk("pick_live_contract 裡有呼叫 contract_alive", "contract_alive(c)" in body, True)

print("\n%s" % ("全部通過" if not bad else "❌ %d 項沒過" % bad))
sys.exit(1 if bad else 0)
