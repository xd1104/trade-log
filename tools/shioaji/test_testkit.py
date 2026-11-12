# -*- coding: utf-8 -*-
"""
守「測試寫不到真的資料檔」那層（testkit.sandbox，2026-11-12 整理第 1 批）。純離線。
  ① sandbox() 之後，REDIRECT 裡每一個常數都不在 tools/shioaji 底下、開關檔全部不存在（＝全關）
  ② 這幾個模組裡看起來是資料檔／開關檔的路徑常數，每一個都要嘛在 REDIRECT、要嘛在 READONLY（⛔ 新加的忘了登記 ⇒ 這裡抓）
  ③ 下單模組沒有 import testkit
跑法：.venv/Scripts/python.exe -X utf8 tools/shioaji/test_testkit.py
"""
import importlib
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.stdout.reconfigure(encoding="utf-8")
import testkit  # noqa: E402

FAIL = []


def chk(name, got, want):
    ok = got == want
    print(("  OK   " if ok else "  FAIL ") + name + ("" if ok else "  (得到 %r，期待 %r)" % (got, want)))
    if not ok:
        FAIL.append(name)


root = testkit.sandbox()
chk("① sandbox 之後沒有常數還指著真的資料夾", testkit.still_real(), [])
chk("① sandbox 重叫一次回同一個暫存區", testkit.sandbox(), root)
import broker, auto_fire, night_fire  # noqa: E402,E401
chk("① 開關檔全部不存在（＝全關）",
    [p.exists() for p in (broker.REAL_FLAG, broker.MINI_FLAG, auto_fire.ARM_FLAG, night_fire.ARM_FLAG)],
    [False, False, False, False])

miss = []
for m in testkit.REDIRECT:
    mod = importlib.import_module(m)
    for k, v in vars(mod).items():
        if not (k.isupper() and isinstance(v, Path) and re.search(r"(DIR|FILE|FLAG|HIST|CSV|CTX|LOG)$", k)):
            continue
        if k in testkit.REDIRECT[m] or k in testkit.READONLY.get(m, []):
            continue
        miss.append("%s.%s" % (m, k))
chk("② 每一個資料檔／開關檔常數都登記過（REDIRECT 或 READONLY）", miss, [])

bad = [f.name for f in HERE.glob("*.py")
       if not f.name.startswith("test_") and f.name not in ("testkit.py", "run_tests.py")
       and re.search(r"^\s*(import|from)\s+testkit", f.read_text(encoding="utf-8"), re.M)]
chk("③ 下單／面板模組沒有 import testkit", bad, [])
print("\n總結：" +("全部通過 ✅" if not FAIL else "%d 項沒過 ❌" % len(FAIL)))
sys.exit(1 if FAIL else 0)
