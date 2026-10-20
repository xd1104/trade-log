# -*- coding: utf-8 -*-
"""
重啟鈕（2026-10-20）的條件探針：什麼時候准按、什麼時候擋。
⛔ 不連永豐、⛔ 不碰 8770、⛔ 不會真的結束任何行程（request_restart 只在條件全過時才排程結束 ⇒ 這裡只驗擋下來的路）。
跑法：.venv\\Scripts\\python.exe tools\\shioaji\\test_restart.py
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_panel as LP      # noqa: E402
import broker                # noqa: E402

FAILS = []


def say(ok, name, got=""):
    print(("  OK     " if ok else "  FAIL   ") + name + ("" if ok else "  (得到 %r)" % (got,)))
    if not ok:
        FAILS.append(name)


def at(hm):
    return datetime(2026, 10, 21, int(hm[:2]), int(hm[3:5]), 0)


saved = (dict(LP.SUPERVISOR), dict(LP.RESTART), broker._state.get("position"))
try:
    LP.RESTART["at"] = None
    broker._state["position"] = None

    # ① 還沒查完是誰開的 ⇒ 擋
    LP.SUPERVISOR.update(done=False, who=None)
    ok, why = LP.restart_check(at("10:30"))
    say(not ok and "剛開" in why, "還在確認是誰開的 ⇒ 擋", why)

    # ② 不是捷徑開的（沒有看門狗）⇒ 擋（結束了不會回來）
    LP.SUPERVISOR.update(done=True, who=None)
    ok, why = LP.restart_check(at("10:30"))
    say(not ok and "不是用桌面捷徑開的" in why, "沒人顧 ⇒ 擋", why)

    # ③ 被顧著、平常時段、沒部位 ⇒ 准
    LP.SUPERVISOR.update(done=True, who="app")
    for hm in ("08:57", "09:31", "10:30", "13:46", "15:00", "21:32", "21:43", "04:55", "05:01", "23:00"):
        ok, why = LP.restart_check(at(hm))
        say(ok, "%s 沒部位 ⇒ 可以重啟" % hm, why)

    # ④ 關鍵時刻 ⇒ 擋（含頭不含尾）
    for hm in ("08:58", "09:00", "09:03", "09:15", "09:30", "13:27", "13:43", "13:45",
               "21:33", "21:35", "21:40", "21:42", "04:56", "04:58", "05:00"):
        ok, why = LP.restart_check(at(hm))
        say(not ok and "先不要重啟" in (why or ""), "%s 關鍵時刻 ⇒ 擋" % hm, why)

    # ⑤ 有部位 ⇒ 擋（不管幾點）
    broker._state["position"] = {"dir": "long", "entry": 12000}
    ok, why = LP.restart_check(at("10:30"))
    say(not ok and "有部位" in why, "有部位 ⇒ 擋", why)
    c, out = LP.request_restart()
    say(c == 409 and not LP.RESTART["at"], "有部位時直接打端點 ⇒ 409、沒有要求重啟", (c, out))
    broker._state["position"] = None

    # ⑥ 單子正在送 ⇒ 擋
    LP.ENTER_LOCK.acquire()
    try:
        ok, why = LP.restart_check(at("10:30"))
        say(not ok and "正在送" in why, "單子在送 ⇒ 擋", why)
    finally:
        LP.ENTER_LOCK.release()

    # ⑦ 已經要求過 ⇒ 不能再按第二次
    LP.RESTART["at"] = 1.0
    ok, why = LP.restart_check(at("10:30"))
    say(not ok and "重啟中" in why, "已經在重啟 ⇒ 不能再按", why)
    LP.RESTART["at"] = None

    # ⑧ 畫面要的那份資料
    info = LP.restart_info()
    say(set(info) >= {"ok", "why", "who", "pending"}, "restart_info 帶齊欄位", info)

    # ⑨ 離開碼：start-panel.bat 是 `if errorlevel 2 goto already`（≥2 就不重開）⇒ 一定要 < 2、且不是 0 以外的 2
    say(LP.RESTART_EXIT == 1, "離開碼是 1（bat 與 panel_app 都會重開）", LP.RESTART_EXIT)
    bat = (Path(__file__).resolve().parent / "start-panel.bat").read_text(encoding="utf-8", errors="replace")
    say("if errorlevel 2 goto already" in bat, "bat 的判斷還是 errorlevel 2（改了要回來看離開碼）")
finally:
    LP.SUPERVISOR.clear(); LP.SUPERVISOR.update(saved[0])
    LP.RESTART.clear(); LP.RESTART.update(saved[1])
    broker._state["position"] = saved[2]

print("\n全部通過 ✅" if not FAILS else "\n%d 項沒過 ❌" % len(FAILS))
sys.exit(1 if FAILS else 0)
