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

print("\n  ── pick_front：固定挑最近一個還沒到期的（不看成交量） ──")
cands = [oct_, nov, C(code="TMFL6", delivery_month="202612", delivery_date="2026/12/16")]
chk("結算日 10:00 ⇒ 還是 10 月（盤中重連不會換月）", LP.pick_front(cands, datetime(2026, 10, 21, 10, 0)).code, "TMFJ6")
chk("結算日 13:29:59 ⇒ 10 月", LP.pick_front(cands, datetime(2026, 10, 21, 13, 29, 59)).code, "TMFJ6")
chk("結算日 13:30 ⇒ 11 月", LP.pick_front(cands, datetime(2026, 10, 21, 13, 30)).code, "TMFK6")
chk("結算日前一晚 22:00 ⇒ 10 月", LP.pick_front(cands, datetime(2026, 10, 20, 22, 0)).code, "TMFJ6")
chk("有一個讀不出到期日 ⇒ None（退回舊挑法）", LP.pick_front([oct_, nodd], datetime(2026, 10, 21, 10)), None)
chk("空清單 ⇒ None", LP.pick_front([], datetime(2026, 10, 21, 10)), None)

print("\n  ── quiet_seconds：重啟後一筆都沒收到也要算斷線 ──")


def ts(*a):
    return datetime(*a).timestamp()


chk("有收過 ⇒ 照舊用 last_recv",
    LP.quiet_seconds(ts(2026, 10, 21, 17, 0), ts(2026, 10, 21, 16, 0), "night", ts(2026, 10, 21, 17, 2)), 120.0)
chk("10-21 17:14 重啟、17:20 還沒收到 ⇒ 360 秒（會重連）",
    LP.quiet_seconds(None, ts(2026, 10, 21, 17, 14), "night", ts(2026, 10, 21, 17, 20)), 360.0)
chk("08:30 連好、08:45:30 還沒收到 ⇒ 只算開盤後 30 秒（不會一開盤就重連）",
    LP.quiet_seconds(None, ts(2026, 10, 22, 8, 30), "day", ts(2026, 10, 22, 8, 45, 30)), 30.0)
chk("昨晚 23:00 連上、凌晨 01:00 還沒收到 ⇒ 從 23:00 算",
    LP.quiet_seconds(None, ts(2026, 10, 21, 23, 0), "night", ts(2026, 10, 22, 1, 0)), 7200.0)
chk("凌晨 03:00 才重啟、03:01 還沒收到 ⇒ 60 秒",
    LP.quiet_seconds(None, ts(2026, 10, 22, 3, 0), "night", ts(2026, 10, 22, 3, 1)), 60.0)
chk("休市 ⇒ None（不判斷）", LP.quiet_seconds(None, ts(2026, 10, 21, 14, 0), "closed", ts(2026, 10, 21, 14, 30)), None)
chk("還沒連上過 ⇒ None", LP.quiet_seconds(None, None, "day", ts(2026, 10, 21, 9, 0)), None)
chk("守門：看門狗改用 quiet_seconds", "quiet = quiet_seconds(" in src, True)
chk("守門：connect() 有記 conn_at", 'session["conn_at"] = time.time()' in src, True)
chk("守門：pick_live_contract 先試 pick_front", "front = pick_front(cands)" in body, True)

print("\n%s" % ("全部通過" if not bad else "❌ %d 項沒過" % bad))
sys.exit(1 if bad else 0)
