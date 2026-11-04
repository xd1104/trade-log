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
src = (HERE / "live_panel.py").read_text(encoding="utf-8")
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

print("\n全部通過 ✅" if not FAILS else "\n%d 項沒過 ❌" % len(FAILS))
sys.exit(1 if FAILS else 0)
