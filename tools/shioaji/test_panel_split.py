# -*- coding: utf-8 -*-
"""
主程式拆檔（2026-11-12 健檢整理第 5 批）的接線檢查。純離線、不連永豐、不碰真的資料。

拆出去的四支：panel_depth（五檔）、panel_phone（練習紀錄手機同步）、panel_account（帳戶底層）、panel_kline（K 線＋逐筆圖）。
  ① 四支都不准 import live_panel（面板是 `python live_panel.py` 跑的 ⇒ 再 import 一次＝兩份狀態）
  ② live_panel 載入時有把該接的東西接上（漏接 ⇒ 那支會用預設值，畫面靜靜地錯）
  ③ 跟 live_panel 各寫一份的常數要一樣
  ④ live_panel 轉出去的名字跟那支是同一個東西（不是複製一份）
  ⑤ 沒接上時的預設是安全方向（五檔不重連、不問券商流量、不碰練習紀錄）
跑法：.venv/Scripts/python.exe -X utf8 tools/shioaji/test_panel_split.py
"""
import ast
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.stdout.reconfigure(encoding="utf-8")

import testkit  # noqa: E402
testkit.sandbox()
import live_panel as LP  # noqa: E402
import panel_account as PA  # noqa: E402
import panel_depth as PD  # noqa: E402
import panel_kline as PK  # noqa: E402
import panel_phone as PH  # noqa: E402

FAIL = []
MODS = {"panel_depth": PD, "panel_phone": PH, "panel_account": PA, "panel_kline": PK}


def chk(name, got, want):
    ok = got == want
    print(("  OK   " if ok else "  FAIL ") + name + ("" if ok else "  (得到 %r，期待 %r)" % (got, want)))
    if not ok:
        FAIL.append(name)


print("=== ① 不准 import live_panel ===")
for name in MODS:
    tree = ast.parse((HERE / (name + ".py")).read_text(encoding="utf-8"))
    imps = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imps |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            imps.add(n.module or "")
    chk("%s 沒有 import live_panel" % name, "live_panel" in imps, False)

print("\n=== ② 接線 ===")
chk("五檔：重連跟重啟鈕吃同一份關鍵時段", PD.restart_blocks is LP.restart_blocks, True)
chk("四支的 PRODUCT 都是面板那個", [PD.PRODUCT, PA.PRODUCT, PK.PRODUCT], [LP.PRODUCT] * 3)
chk("手機同步：練習紀錄資料夾＝面板的（測試導走也跟著走）", PH.trade_dir(), LP.TRADE_DIR)
chk("手機同步：讀今天、讀全部練習紀錄是面板那兩支",
    (PH.load_today_trades is LP.load_today_trades, PH.all_practice_trades is LP.all_practice_trades), (True, True))
old = LP.CURRENT_STATE.get("today")
LP.CURRENT_STATE["today"] = type("T", (), {"price": 23456.0})()
chk("帳戶：現價讀 Today", PA.price_now(), 23456.0)
LP.CURRENT_STATE["today"] = old
old = LP.SESSION_REF.get("api")
LP.SESSION_REF["api"] = "假連線"
chk("帳戶：永豐連線讀面板那條", PA.api_now(), "假連線")
LP.SESSION_REF["api"] = old
chk("K 線：CURRENT_STATE／SESSION_REF 是同一個 dict",
    (PK.CURRENT_STATE is LP.CURRENT_STATE, PK.SESSION_REF is LP.SESSION_REF), (True, True))
chk("K 線：今天的練習紀錄、練習／逐筆資料夾跟著面板",
    (PK.today_trades() is LP.TODAY_TRADES, PK.trade_dir(), PK.tick_dir()), (True, LP.TRADE_DIR, LP.TICK_DIR))
chk("K 線：sim_lanes／day_features／prev_day_close 是面板那個",
    (PK.sim_lanes_mod() is LP.sim_lanes, PK.day_features is LP.day_features, PK.prev_day_close is LP.prev_day_close),
    (True, True, True))

print("\n=== ③ 各寫一份的常數要一樣 ===")
chk("K 線：5 分 K、日盤開收、夜盤開、夜盤尾巴",
    [PK.CHART_TF, PK.SESSION_OPEN, PK.DAY_END, PK.NIGHT_OPEN, PK.NIGHT_TAIL],
    [LP.CHART_TF, LP.SESSION_OPEN, LP.DAY_END, LP.NIGHT_OPEN, LP.NIGHT_TAIL])
chk("流量資料夾（帳戶寫、面板放自動重啟紀錄）同一個名字", PA.USAGE_DIR.name, LP.USAGE_DIR.name)

print("\n=== ④ 轉出去的名字是同一個東西 ===")
src = (HERE / "live_panel.py").read_text(encoding="utf-8")
n_same = 0
for node in ast.parse(src).body:
    if isinstance(node, ast.ImportFrom) and node.module in MODS:
        for a in node.names:
            nm = a.asname or a.name
            if getattr(LP, nm) is not getattr(MODS[node.module], a.name):
                FAIL.append("LP.%s 不是 %s.%s" % (nm, node.module, a.name))
                print("  FAIL LP.%s 不是 %s.%s" % (nm, node.module, a.name))
            else:
                n_same += 1
chk("轉出去的名字有被檢查到（量尺自證）", n_same > 60, True)

print("\n=== ⑤ 沒接上時的預設（另開一個行程，只 import 那四支）===")
code = r"""
import sys; sys.path.insert(0, %r)
from datetime import datetime
import panel_depth as PD, panel_account as PA, panel_kline as PK, panel_phone as PH
T = 100000.0
q, go = PD.depth_check(T, T - PD.DEPTH_STALE_S - 1, 2, True, True, False, 0, datetime(2026, 6, 10, 11, 0))
print(go, PA.api_now(), PA.price_now(), PK.sim_lanes_mod(), PK.today_trades())
for f in (PH.trade_dir, PK.trade_dir, PK.tick_dir):
    try:
        f(); print("沒丟錯")
    except RuntimeError:
        print("丟錯")
""" % str(HERE)
r = subprocess.run([sys.executable, "-X", "utf8", "-c", code], capture_output=True, text=True, encoding="utf-8",
                   cwd=str(HERE), timeout=120)
lines = (r.stdout or "").strip().splitlines()
chk("五檔沒接上關鍵時段 ⇒ 不重連；帳戶沒接上 ⇒ 不問流量、現價不知道；K 線 ⇒ 沒有模擬、沒有今天的練習紀錄",
    lines[:1], ["False None None None []"])
chk("練習／逐筆資料夾沒接上 ⇒ 丟錯（⛔ 不會去讀寫真的資料夾）", lines[1:], ["丟錯", "丟錯", "丟錯"])
if r.returncode:
    print(r.stderr[-800:])

print("\n總結：" + ("全部通過 ✅" if not FAIL else "%d 項沒過 ❌：%s" % (len(FAIL), "；".join(FAIL))))
sys.exit(1 if FAIL else 0)
