# -*- coding: utf-8 -*-
"""
主迴圈（停損那條）不可以被慢的東西卡住 —— 2026-11-05 健檢補的守衛。
⛔ 不連永豐、⛔ 不碰 8770、⛔ 不送單。

① 畫面那句「能不能進場」：每 CAN_ENTER_VIEW_S 秒才真的問一次券商（原本每一圈都打 list_positions）。
② 09:30 雲端同步（git push）不可以在主迴圈上同步跑。
跑法：.venv\\Scripts\\python.exe tools\\shioaji\\test_loop_blocking.py
"""
import ast
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import live_panel as LP      # noqa: E402
import broker                # noqa: E402

FAILS = []


def say(ok, name, got=""):
    print(("  OK     " if ok else "  FAIL   ") + name + ("" if ok else "  (得到 %r)" % (got,)))
    if not ok:
        FAILS.append(name)


print("=== ① 能不能進場：畫面用的那份要節流 ===")
calls = []
real = broker.can_enter
broker.can_enter = lambda px, live: (calls.append((px, live)) or (True, None))
try:
    LP._CAN_ENTER_VIEW.update(at=0.0, key=None, v=(False, ""))
    t0 = 1000.0
    for i in range(12):                                   # 一秒四圈 × 3 秒
        LP._can_enter_view(43000.0, True, False, now=t0 + i * 0.25)
    say(len(calls) == 1, "3 秒內 12 圈只問券商 1 次", len(calls))
    LP._can_enter_view(43000.0, True, False, now=t0 + LP.CAN_ENTER_VIEW_S + 0.01)
    say(len(calls) == 2, "超過 3 秒才再問一次", len(calls))
    LP._can_enter_view(43000.0, True, True, now=t0 + LP.CAN_ENTER_VIEW_S + 0.02)
    say(len(calls) == 3, "有沒有部位變了 ⇒ 馬上重問（不等 3 秒）", len(calls))
    LP._can_enter_view(43000.0, False, True, now=t0 + LP.CAN_ENTER_VIEW_S + 0.03)
    say(len(calls) == 4, "報價狀態變了 ⇒ 馬上重問", len(calls))
finally:
    broker.can_enter = real
    LP._CAN_ENTER_VIEW.update(at=0.0, key=None, v=(False, ""))

print("=== ② 09:30 雲端同步不在主迴圈上同步跑 ===")
src = __import__("testkit").panel_source(HERE / "live_panel.py")
tree = ast.parse(src)
main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
direct = []
for node in ast.walk(main):
    # 只看 main 本體（不含它裡面定義的內嵌函式 —— 背景執行緒的那支就是內嵌函式）
    pass


def calls_in(fn_node, skip_nested=True):
    out = []
    stack = list(fn_node.body)
    while stack:
        n = stack.pop()
        if skip_nested and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            out.append(n.func.id)
        stack.extend(ast.iter_child_nodes(n))
    return out


say("sync_to_cloud" not in calls_in(main), "main() 本體沒有直接呼叫 sync_to_cloud()",
    [c for c in calls_in(main) if c == "sync_to_cloud"])
say("sync_to_cloud" in calls_in(main, skip_nested=False), "尺的自證：背景那支確實還會呼叫 sync_to_cloud()")

print("=== ③ 主迴圈卡住要有人知道（手機示警＋停心跳）===")
import monitor_push as MP          # noqa: E402
say("STATE[\"loop_at\"] = time.time()" in src, "面板主迴圈有蓋心跳章 loop_at")
pos_snap = {"real": {"position": {"dir": "long", "qty": 3}}, "loop_age": 200}
t = MP._todo(pos_snap, [], [])
say(any("卡住" in x["text"] and x["lvl"] == "now" for x in t), "有部位、卡 200 秒 ⇒ 手機最前面一則「卡住」", t)
t = MP._todo(dict(pos_snap, loop_age=5), [], [])
say(not any("卡住" in x["text"] for x in t), "正常轉（5 秒）⇒ 不示警", t)
t = MP._todo({"real": {"position": None}, "loop_age": 200}, [], [])
say(not any("卡住" in x["text"] for x in t), "確定空手 ⇒ 不吵（停損沒東西可顧）", t)
t = MP._todo(dict(pos_snap, loop_age=None), [], [])
say(not any("卡住" in x["text"] for x in t), "舊版面板沒有這個欄位 ⇒ 不判", t)
hb = []
_saved = (MP.snapshot, MP.push, MP.heartbeat, MP.seal, MP._analyst_box, MP._log)
MP.push = lambda *a, **k: None
MP.heartbeat = lambda: hb.append(1)
MP.seal = lambda o, k: {}
MP._analyst_box = lambda k: None
MP._log = lambda m: None
try:
    MP.snapshot = lambda port: {"loop_age": MP.LOOP_DEAD_S + 10}
    MP.once(0, None)
    say(hb == [], "卡超過 %d 秒 ⇒ 刻意不心跳（讓外面的斷線通知響）" % MP.LOOP_DEAD_S, hb)
    MP.snapshot = lambda port: {"loop_age": MP.LOOP_DEAD_S + 10, "real": {"position": None}}
    MP.once(0, None)
    say(hb == [1], "確定空手、只是卡住（休市重連）⇒ 照常心跳，不叫醒人", hb)
    MP.snapshot = lambda port: {"loop_age": 2}
    MP.once(0, None)
    say(hb == [1, 1], "正常 ⇒ 照常心跳", hb)
finally:
    MP.snapshot, MP.push, MP.heartbeat, MP.seal, MP._analyst_box, MP._log = _saved

print("=== ④ 備份不可以安靜壞掉 ===")
from datetime import datetime as _dt   # noqa: E402
_rb = LP.PA._backup_read
try:
    now = _dt(2026, 11, 5, 12, 0, 0)
    LP.PA._backup_read = lambda: {"running": True, "started": "2026-11-05T05:30:00", "at": "2026-11-04T05:32:00"}
    v = LP.backup_view(now)
    say(v["warn"] and "中斷" in v["msg"], "05:30 開始、中午還在 running ⇒ 警告可能中斷了", v)
    LP.PA._backup_read = lambda: {"running": True, "started": "2026-11-05T05:30:00"}
    v = LP.backup_view(_dt(2026, 11, 5, 5, 31, 0))
    say(not v["warn"] and "正在跑" in v["msg"], "剛開始跑 ⇒ 照常寫正在跑", v)
    LP.PA._backup_read = lambda: {"ok": True, "at": "2026-11-05T05:32:00", "n_skipped": 1, "skipped": ["2026-11-04.jsonl.gz"]}
    v = LP.backup_view(now)
    say(v["warn"] and "太大" in v["msg"] and v["ok"] is False, "有檔太大被跳過 ⇒ 不准寫成功", v)
    LP.PA._backup_read = lambda: {"ok": True, "at": "2026-11-05T05:32:00", "pushed_files": 3}
    v = LP.backup_view(now)
    say(v["ok"] and not v["warn"], "對照組：正常成功 ⇒ 不警告", v)
finally:
    LP.PA._backup_read = _rb
import backup_data as BD           # noqa: E402
say(hasattr(BD, "SKIPPED"), "backup_data 會記下被跳過的檔")

print("\n全部通過 ✅" if not FAILS else "\n%d 項沒過 ❌" % len(FAILS))
sys.exit(1 if FAILS else 0)
