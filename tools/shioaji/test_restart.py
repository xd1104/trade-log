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
real_ms = LP.auto_fire.morning_settled
SETTLED = {"v": False}
LP.auto_fire.morning_settled = lambda d: SETTLED["v"]   # ⛔ 不讓真帳本影響 ①～⑥（10-21 那天真的有成交）
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
    for hm in ("08:57", "09:31", "10:30", "13:46", "15:00", "21:05", "21:43", "04:55", "05:01", "23:00"):
        ok, why = LP.restart_check(at(hm))
        say(ok, "%s 沒部位 ⇒ 可以重啟" % hm, why)

    # ④ 關鍵時刻 ⇒ 擋（含頭不含尾）
    for hm in ("08:58", "09:00", "09:03", "09:15", "09:30", "13:27", "13:43", "13:45",
               "21:07", "21:10", "21:33", "21:35", "21:40", "21:42", "04:56", "04:58", "05:00"):
        ok, why = LP.restart_check(at(hm))
        say(not ok and "先不要重啟" in (why or ""), "%s 關鍵時刻 ⇒ 擋" % hm, why)

    # ④-b 冬令（2026-11-05 修：原本寫死夏令 21:33～21:43，冬令 22:35～22:40 送單那段沒擋）
    def wat(hm):
        return datetime(2026, 11, 4, int(hm[:2]), int(hm[3:5]), 0)
    for hm in ("22:07", "22:10", "22:33", "22:35", "22:40", "22:42"):
        ok, why = LP.restart_check(wat(hm))
        say(not ok and "先不要重啟" in (why or ""), "冬令 %s 夜盤判斷中 ⇒ 擋" % hm, why)
    for hm in ("21:35", "21:40", "22:05", "22:43"):
        ok, why = LP.restart_check(wat(hm))
        say(ok, "冬令 %s 不是送單時刻 ⇒ 可以重啟" % hm, why)

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

    # ⑩ 自動重啟（2026-11-05）：程式有新版＋沒部位＋安全時段才會自己按
    def dt(y, mo, d, hm):
        return datetime(y, mo, d, int(hm[:2]), int(hm[3:5]), 0)
    W = LP.auto_restart_window
    say(W(dt(2026, 11, 5, "05:05")) and W(dt(2026, 11, 5, "08:29")), "平日 05:05～08:29 是安全時段")
    say(not W(dt(2026, 11, 5, "04:59")) and not W(dt(2026, 11, 5, "08:30")) and not W(dt(2026, 11, 5, "10:30"))
        and not W(dt(2026, 11, 5, "22:00")), "平日 04:59、08:30、盤中、夜盤都不是")
    say(not W(dt(2026, 11, 7, "03:00")) and W(dt(2026, 11, 7, "05:05")), "週六 05:00 前還有夜盤 ⇒ 05:05 起才可以")
    say(W(dt(2026, 11, 8, "14:00")), "週日整天可以")
    calls = []
    real_req, real_file = LP.request_restart, LP.AUTO_RESTART_FILE
    LP.request_restart = lambda: (calls.append(1) or (200, {"ok": True}))
    import tempfile as _tf
    LP.AUTO_RESTART_FILE = Path(_tf.mkdtemp()) / "auto_restart.json"
    try:
        LP.SUPERVISOR.update(done=True, who="app")
        broker._state["position"] = None

        def tick(when, stale=True):
            LP.AUTO_RESTART.update(day=None, checked=0.0)
            return LP.auto_restart_tick(when, stamp={"stale": stale, "started": "11-05 00:07"})
        say(tick(dt(2026, 11, 5, "05:10")) and calls == [1], "有新版、沒部位、05:10 ⇒ 自己重啟", calls)
        say(LP.AUTO_RESTART_FILE.exists(), "留下紀錄（新面板起來後通知手機用）")
        calls.clear()
        say(not tick(dt(2026, 11, 5, "05:10"), stale=False) and calls == [], "沒有新版 ⇒ 不重啟", calls)
        say(not tick(dt(2026, 11, 5, "10:30")) and calls == [], "盤中 ⇒ 不重啟", calls)
        broker._state["position"] = {"dir": "long", "entry": 43000}
        say(not tick(dt(2026, 11, 5, "05:10")) and calls == [], "⛔ 有部位 ⇒ 不重啟", calls)
        broker._state["position"] = None
        LP.AUTO_RESTART.update(day=dt(2026, 11, 5, "05:10").date(), checked=0.0)
        say(not LP.auto_restart_tick(dt(2026, 11, 5, "06:00"), stamp={"stale": True}) and calls == [],
            "一天最多一次", calls)
        LP.SUPERVISOR.update(done=True, who=None)
        say(not tick(dt(2026, 11, 5, "05:10")) and calls == [], "⛔ 不是捷徑開的（關掉不會自己開回來）⇒ 不重啟", calls)
    finally:
        LP.request_restart, LP.AUTO_RESTART_FILE = real_req, real_file
        LP.AUTO_RESTART.update(day=None, checked=0.0)
        LP.SUPERVISOR.update(done=True, who="app")
        broker._state["position"] = None

    # ⑪ 2026-11-05：開盤那一口已經有結果＋沒部位 ⇒ 08:58～09:31 不再擋（其他時段照擋）
    SETTLED["v"] = True
    for hm in ("09:10", "09:15", "09:30"):
        ok, why = LP.restart_check(at(hm))
        say(ok, "今天單已有結果、沒部位、%s ⇒ 可以重啟" % hm, why)
    for hm in ("13:43", "04:58", "21:35"):
        ok, why = LP.restart_check(at(hm))
        say(not ok and "先不要重啟" in (why or ""), "單有結果也不放其他時段：%s ⇒ 擋" % hm, why)
    broker._state["position"] = {"dir": "long", "entry": 12000}
    ok, why = LP.restart_check(at("09:15"))
    say(not ok and "有部位" in why, "單有結果但還有部位 ⇒ 擋", why)
    broker._state["position"] = None
    LP.auto_fire.morning_settled = lambda d: 1 / 0
    ok, why = LP.restart_check(at("09:15"))
    say(not ok and "先不要重啟" in (why or ""), "看不出有沒有定論（出錯）⇒ 照擋", why)
    LP.auto_fire.morning_settled = lambda d: SETTLED["v"]
    SETTLED["v"] = False

    # ⑫ morning_settled 本身：用假帳本逐種情況驗
    import json as _json
    import tempfile as _tf
    AF = LP.auto_fire
    real_dir = AF.FIRE_DIR
    AF.FIRE_DIR = Path(_tf.mkdtemp())
    try:
        def case(rows):
            d = "2026-11-05"
            (AF.FIRE_DIR / "2026-11.jsonl").write_text(
                "".join(_json.dumps(dict(r, date=d), ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
            AF._MS_CACHE["key"] = None
            return real_ms(d)
        say(case([]) is False, "沒有任何列 ⇒ 還沒定論")
        say(case([{"rec": "fire", "cand": "fast", "stage": "sending"}]) is False, "送出去還不知道下場 ⇒ 還沒定論")
        say(case([{"rec": "fire", "cand": "fast", "stage": "sending"},
                  {"rec": "result", "cand": "fast", "ok": True, "dir": "long", "stage": "done"},
                  {"rec": "skip", "cand": "orb", "why": "union_done"}]) is True, "快攻成交 ⇒ 有定論")
        say(case([{"rec": "result", "cand": "fast", "ok": False, "why": "order_failed"}]) is False,
            "送單失敗（可能成交、要撿回）⇒ 還沒定論")
        say(case([{"rec": "skip", "cand": "fast", "why": "not_fast"},
                  {"rec": "skip", "cand": "orb", "why": "orb_short"}]) is False, "回馬槍還沒判 ⇒ 還沒定論")
        say(case([{"rec": "skip", "cand": "fast", "why": "not_fast"},
                  {"rec": "skip", "cand": "orb", "why": "orb_short"},
                  {"rec": "skip", "cand": "rev", "why": "no_reversal"}]) is True, "三個候選都收工 ⇒ 有定論")
    finally:
        AF.FIRE_DIR = real_dir
        AF._MS_CACHE["key"] = None

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
    LP.auto_fire.morning_settled = real_ms
    LP.SUPERVISOR.clear(); LP.SUPERVISOR.update(saved[0])
    LP.RESTART.clear(); LP.RESTART.update(saved[1])
    broker._state["position"] = saved[2]

print("\n全部通過 ✅" if not FAILS else "\n%d 項沒過 ❌" % len(FAILS))
sys.exit(1 if FAILS else 0)
