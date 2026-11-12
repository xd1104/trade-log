# -*- coding: utf-8 -*-
"""
網頁畫面搬出主程式（2026-11-12 整理第 3 批）的守門。純離線。
  ① live_panel.PAGE 從 panel_page.html 讀進來、規則數字已經替換（沒有 __RULE_ 佔位）
  ② 主程式裡不再有整頁網頁字串（⛔ 有人又把畫面貼回來就紅）
  ③ panel_page.html 是 LF、UTF-8、沒有 BOM（跟搬之前一樣，送出去的位元組才會一樣）
  ④ 只改 panel_page.html ⇒ code_stamp 也會說「有新版」（不然自動重啟不會換上新畫面）
"""
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.stdout.reconfigure(encoding="utf-8")
import testkit  # noqa: E402
testkit.sandbox()
import live_panel as LP  # noqa: E402

FAIL = []


def chk(name, got, want):
    ok = got == want
    print(("  OK   " if ok else "  FAIL ") + name + ("" if ok else "  (得到 %r，期待 %r)" % (got, want)))
    if not ok:
        FAIL.append(name)


raw = (HERE / "panel_page.html").read_bytes()
chk("① PAGE 開頭結尾是整頁網頁", (LP.PAGE.startswith("<!doctype html>"), LP.PAGE.rstrip().endswith("</html>")), (True, True))
chk("① 規則數字已替換", "__RULE_" in LP.PAGE, False)
chk("① PAGE＝檔案內容（只差規則數字替換）", len(LP.PAGE) > 300000 and LP.PAGE.count("\n") == raw.count(b"\n"), True)
src = (HERE / "live_panel.py").read_text(encoding="utf-8")
chk("② 主程式裡沒有整頁網頁字串", "<!doctype html>" in src or 'PAGE = r"""' in src, False)
chk("③ LF、沒有 CR、沒有 BOM", (b"\r" in raw, raw[:3] == b"\xef\xbb\xbf"), (False, False))
f = HERE / "panel_page.html"
st = f.stat()
try:
    os.utime(f, (st.st_atime, time.time() + 60))
    chk("④ 只改 panel_page.html ⇒ code_stamp 說有新版", LP.code_stamp()["stale"], True)
finally:
    os.utime(f, (st.st_atime, st.st_mtime))
print("\n總結：" + ("全部通過 ✅" if not FAIL else "%d 項沒過 ❌" % len(FAIL)))
sys.exit(1 if FAIL else 0)
