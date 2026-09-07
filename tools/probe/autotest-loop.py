# -*- coding: utf-8 -*-
"""
【程式下單】分頁會不會拖慢**停損**（AUTOTEST-TAB-SPEC.md §15-15）。

⚠️ 這件事**一定要用獨立治具量**：`fe_harness.py` 只有 HTTP 服務、**沒有 4Hz 主迴圈**，
   結構上量不到「會不會拖慢停損」——【細節】分頁那一輪就把探針 ⑫ 誤命名成「不影響即時」，
   後來改名了。這支自己起一條 4Hz 迴圈當被害人。

【為什麼會互相影響】這支面板是**單行程**：`ThreadingHTTPServer` 的工作執行緒跟主迴圈
搶的是同一個 GIL。「不碰 state_lock」≠ 零成本。永豐沒有停損單，**停損就活在那條迴圈裡**，
所以迴圈被拖到幾秒＝停損慢幾秒。

量的是「相鄰兩次停損檢查相差多久」的中位與**最大值**（⛔ 只報中位會把尾巴蓋掉 ——
他回報「面板卡了一下」時要查的是 max）。

對照組是**既有功能** `/api/bars` 換日（CLAUDE.md 實測 1732 ms）—— 有對照組才知道
這一頁是不是新洞；沒有對照組的絕對值在不同機器上沒有意義。

⛔ 不碰 8770。跑法：
   PYTHONIOENCODING=utf-8 PYTHONUTF8=1 .venv\\Scripts\\python.exe tools\\probe\\autotest-loop.py
"""
import json
import statistics
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "shioaji"))
import live_panel as LP          # noqa: E402
import autotest_synth as SY      # noqa: E402

SECS = int(sys.argv[1]) if len(sys.argv) > 1 else 20

TMP = Path(tempfile.mkdtemp(prefix="autotest-loop-"))
LP.AUTO_DIR = TMP
LP.AUTO_REAL_DIR = TMP / "real"
(TMP / "real").mkdir(parents=True, exist_ok=True)
rows, bars = SY.build(240, end=date.today())        # 一年份，比他一年後的量還多
SY.write(TMP, rows)
LP.one_min_bars = lambda d: bars.get(str(d), [])
DATES = sorted(bars)
print(f"治具資料：{TMP}　{len(DATES)} 天（比一年還多）")

srv = ThreadingHTTPServer(("127.0.0.1", 0), LP.Handler)
PORT = srv.server_address[1]
assert PORT != 8770
threading.Thread(target=srv.serve_forever, daemon=True).start()
print(f"治具服務：127.0.0.1:{PORT}（⛔ 不是 8770）\n")

STOP = {"v": False}
GAPS = []


def loop():
    """4Hz 主迴圈的替身。⚠️ 每圈做的事要跟真的停損檢查同一個量級（只有幾個比較）。"""
    last = time.perf_counter()
    px, entry = 12000.0, 12000.0
    while not STOP["v"]:
        now = time.perf_counter()
        GAPS.append((now - last) * 1000.0)
        last = now
        # check_real_position() 的核心就是這幾個比較（±100 到價才平）
        _ = (px - entry >= 100.0) or (entry - px >= 100.0)
        time.sleep(0.25)


def hammer(paths, secs, label, cold=False):
    GAPS.clear()
    t = threading.Thread(target=loop, daemon=True)
    STOP["v"] = False
    t.start()
    time.sleep(1.0)
    GAPS.clear()
    end = time.time() + secs
    n = 0
    while time.time() < end:
        if cold:
            # 每一發都當成「面板剛重啟、第一次切進這一頁」：整年份重新解析
            with LP.AUTO_LOCK:
                LP.AUTO_CACHE.clear()
        for p in paths(n):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{p}", timeout=30) as r:
                    r.read()
            except Exception as e:
                print(f"  請求失敗 {p}：{str(e)[:60]}")
            n += 1
    STOP["v"] = True
    time.sleep(0.4)
    g = sorted(GAPS)[:-1] or [0]
    return {"label": label, "n_req": n, "n": len(g),
            "median": round(statistics.median(g), 1),
            "p95": round(g[int(len(g) * 0.95)], 1),
            "max": round(max(g), 1)}


def show(r):
    print(f"  {r['label']:<28} 中位 {r['median']:>7.1f} ms　p95 {r['p95']:>7.1f} ms　"
          f"**最大 {r['max']:>7.1f} ms**　（{r['n_req']} 個請求／{r['n']} 圈）")


print(f"每組量 {SECS} 秒 —— 4Hz ⇒ 平常應該是 250 ms 上下\n")
base = hammer(lambda n: [], SECS, "① 什麼都不打（基準）")
show(base)

auto = hammer(lambda n: [f"/api/auto/days",
                         f"/api/auto/stats?win=20&src=live",
                         f"/api/auto/day?date={DATES[n % len(DATES)]}"],
              SECS, "② 一直打【程式下單】")
show(auto)

# 對照組：既有功能。⚠️ /api/bars 會去讀 tmf_1min.csv（54 萬列），那是他每按一次 ◀ 就發生的事。
ctrl = hammer(lambda n: [f"/api/bars?date={DATES[-1 - (n % 5)]}&tf=5&full=1"],
              SECS, "③ 對照組：/api/bars 換日")
show(ctrl)

cold = hammer(lambda n: [f"/api/auto/stats?win=0&src=live",
                         f"/api/auto/day?date={DATES[n % len(DATES)]}"],
              SECS, "④ 冷解析（每發都清快取）", cold=True)
show(cold)

print()
ok = max(auto["max"], cold["max"]) <= max(ctrl["max"], 400.0)
print(f"  判準：【程式下單】的**最大**停損檢查週期要 ≤ 既有功能（/api/bars 換日）——"
      f"「最慢會停在一秒級」是這支面板既有的架構性質，這一頁不可以是新洞。")
print(f"  結果：熱 {auto['max']} ms／冷 {cold['max']} ms  vs  對照組 {ctrl['max']} ms"
      f"　⇒ {'通過' if ok else '未過'}")
srv.shutdown()
sys.exit(0 if ok else 1)
