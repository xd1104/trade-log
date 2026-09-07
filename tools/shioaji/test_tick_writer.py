# -*- coding: utf-8 -*-
"""
逐筆落地（tick_writer.py）的離線測試 —— **不連永豐、不送任何單、不碰真的 tick_logs/**。

================================================================
這支存在的理由
================================================================
面板收到的每一筆報價以前**完全沒有留**：`on_tick` 丟給 `feed()`，而 `feed()` 從頭到尾
只動記憶體。`morning_logs/2026-08-11-live.json` 裡寫著 `"ticks": 42718` ——
那天收了四萬兩千多筆，留下來的只有那個數字本身。

現在要把它們寫進 `tick_logs/YYYY-MM-DD.jsonl`，而寫檔這件事有一個絕對不能碰的東西：
**`on_tick` 跑在永豐 SDK 的回呼執行緒上，它更新的價格就是停損判斷看的那個價。**
一筆卡 5ms，早上四萬筆就是 200 秒 —— 塞住的是他的停損。

所以這四件事一定要有測試守著（每一條都做過突變測試，改壞了會紅）：
  ① 時段外不寫（08:44:59 不寫、09:30:01 不寫）
  ② 重啟之後是「接著寫」不是覆蓋（看門狗重啟是常態，覆寫＝弄丟當天稍早的資料）
  ③ **on_tick 不會被磁碟拖住** ← 最重要的一條
  ④ 佇列滿了會丟資料，但留得下痕跡（計數＋檔案裡的痕跡列＋主控台警告），不是安靜地少

怎麼跑（PowerShell）：
    & "C:\\Users\\USER\\Desktop\\Claude Work\\trade-log\\.venv\\Scripts\\python.exe" `
      "C:\\Users\\USER\\Desktop\\Claude Work\\trade-log\\tools\\shioaji\\test_tick_writer.py"
"""
import datetime as dt
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import tick_writer as TW

FAIL = 0
DAY = dt.date(2026, 9, 4)        # 隨便挑一個平日；刻意不是「今天」——
                                 # 檔名要來自 tick 的日期，不是本機時鐘


def chk(name, got, want):
    global FAIL
    ok = got == want
    FAIL += not ok
    print(("  OK   " if ok else "  FAIL ") + name +
          ("" if ok else f"  (得到 {got!r}，期待 {want!r})"))


def chk_true(name, cond, extra=""):
    global FAIL
    ok = bool(cond)
    FAIL += not ok
    print(("  OK   " if ok else "  FAIL ") + name + ("" if ok else f"  ({extra})"))


def T(h, m, s, ms=0, day=DAY):
    return dt.datetime(day.year, day.month, day.day, h, m, s, ms * 1000)


def rows_of(path, kind=None):
    """讀回檔案；kind 給定就只留那一種。整份都必須是合法 JSON。"""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        o = json.loads(line)          # 壞掉的行會在這裡直接炸掉
        if kind is None or o.get("k") == kind:
            out.append(o)
    return out


class Sandbox:
    """每一節都用自己的暫存資料夾 —— 絕對不可以寫到真的 tools/shioaji/tick_logs/。"""

    def __enter__(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="__tmp__tickw_"))
        return self.dir

    def __exit__(self, *a):
        shutil.rmtree(self.dir, ignore_errors=True)


def file_of(d, day=DAY):
    return d / f"{day:%Y-%m-%d}.jsonl"


# ================================================================ ① 時段
print("=== ① 只錄 08:45:00 ~ 09:30:00 ===")
with Sandbox() as d:
    w = TW.TickWriter(d, flush_every=0.02).start()
    w.tick(T(8, 44, 59, 999), 12000.0, 1)     # 差 1 毫秒，不錄
    w.tick(T(8, 45, 0, 0), 12001.0, 1)        # 開盤第一筆，要錄
    w.tick(T(9, 0, 0, 0), 12002.0, 2)
    w.tick(T(9, 30, 0, 0), 12003.0, 3)        # 收工那一刻，要錄
    w.tick(T(9, 30, 0, 1), 12004.0, 1)        # 過了，不錄
    w.tick(T(9, 30, 1, 0), 12005.0, 1)        # 不錄
    w.tick(T(13, 44, 0, 0), 12006.0, 1)       # 日盤其他時間，不錄
    w.tick(T(23, 30, 0, 0), 12007.0, 1)       # 夜盤，不錄
    w.bidask(T(8, 44, 59, 0), 11999.0, 12000.0)   # 不錄
    w.bidask(T(9, 0, 0, 500), 12001.0, 12002.0)   # 要錄
    w.stop()
    ticks = rows_of(file_of(d), "t")
    chk("  成交只留下時段內的三筆", [r["p"] for r in ticks], [12001.0, 12002.0, 12003.0])
    chk("  買賣價同一把尺", [r["b"] for r in rows_of(file_of(d), "b")], [12001.0])
    chk("  時段外的沒有落地（含夜盤）",
        [r for r in ticks if r["p"] in (12000.0, 12004.0, 12005.0, 12006.0, 12007.0)], [])
    chk("  檔名跟著 tick 的日期，不是本機時鐘", file_of(d).exists(), True)
    chk("  沒有生出「今天」的檔案", file_of(d, dt.date.today()).exists(), False)

print("\n=== ①b ts 是 None 不可以把永豐的回呼執行緒打死 ===")
# `_in_window()` 那句 `if ts is None` 是**熱路徑上唯一的防呆**，而它擋的是例外：
# on_tick 沒有 try，`ts.time()` 丟出來的 AttributeError 會直接冒回永豐 SDK 的執行緒。
# （on_bidask 自己有 try，但那個 try 包不到 TICKS.bidask 那一行 —— 它在 try 外面。）
with Sandbox() as d:
    w = TW.TickWriter(d, flush_every=0.02).start()
    err = None
    try:
        w.tick(None, 12100.0, 1)
        w.bidask(None, 12099.0, 12100.0)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    w.tick(T(9, 5, 0), 12101.0, 1)          # 尺的自證：正常的那一筆一定要進得去
    w.stop()
    chk("  ts=None 不丟例外（丟出去就是丟在 SDK 的回呼執行緒上）", err, None)
    chk("  ts=None 不落地", [r["p"] for r in rows_of(file_of(d), "t")], [12101.0])
    chk("  （尺的自證）正常的那一筆有寫出去", w.written, 1)

# ================================================================ ② 重啟
print("\n=== ② 重啟之後接著寫，不是覆蓋 ===")
# 看門狗重啟是常態（永豐 SDK 斷線會把行程帶掉）。這個專案已經用 TODAY_TRADES
# 弄丟過一次真實資料 —— 重啟後覆寫就是把當天稍早的資料整份抹掉。
with Sandbox() as d:
    w1 = TW.TickWriter(d, flush_every=0.02).start()
    for i in range(3):
        w1.tick(T(8, 45, i), 12010.0 + i, 1)
    w1.stop()
    before = file_of(d).read_text(encoding="utf-8")

    w2 = TW.TickWriter(d, flush_every=0.02).start()     # ← 模擬看門狗重啟
    for i in range(2):
        w2.tick(T(9, 0, i), 12020.0 + i, 1)
    w2.stop()

    after = file_of(d).read_text(encoding="utf-8")
    chk("  早上那三筆一個字都沒被動到", after.startswith(before), True)
    chk("  五筆都在", [r["p"] for r in rows_of(file_of(d), "t")],
        [12010.0, 12011.0, 12012.0, 12020.0, 12021.0])
    chk("  重啟會多一列檔頭（看得出來重啟過幾次）", len(rows_of(file_of(d), "h")), 2)

# ================================================================ ②b flush
print("\n=== ②b 行程被砍（不呼叫 stop()）資料照樣在檔案裡 ===")
# ⛔ 這一節必須開子行程，**在同一個行程裡結構上驗不到**：
#    上面每一節收尾都呼叫 w.stop() ⇒ 關檔 ⇒ 緩衝一定落地 ⇒ `fh.flush()` 拿掉也照樣全綠。
#    而 flush() 要防的正好是 stop() 不會被呼叫的情境：永豐 SDK 斷線會把整個行程帶掉
#    （2026-08-12 19:17，連 traceback 都沒有），看門狗再把它重開。
#    所以：子行程寫幾筆 → 睡一下讓寫檔執行緒跑一輪 → os._exit(9)（跳過所有 Python 收尾，
#    緩衝跟著行程一起消失）→ 父行程去讀那個檔。
CHILD = '''# -*- coding: utf-8 -*-
import datetime as dt
import os
import sys
import time

sys.path.insert(0, r"{here}")
import tick_writer as TW

w = TW.TickWriter(r"{out}", flush_every=0.05).start()
for i in range(4):
    w.tick(dt.datetime({y}, {m}, {dd}, 9, 25, i), 12070.0 + i, 1)
time.sleep(0.6)      # 讓寫檔執行緒真的跑完至少一輪
os._exit(9)          # ⛔ 刻意不呼叫 stop()：模擬 SDK 把行程帶掉、看門狗重開
'''

with Sandbox() as d:
    script = d / "__tmp__killed_child.py"
    script.write_text(CHILD.format(here=str(HERE), out=str(d),
                                   y=DAY.year, m=DAY.month, dd=DAY.day), encoding="utf-8")
    p = subprocess.run([sys.executable, str(script)],
                       capture_output=True, text=True, timeout=90)
    # 尺的自證：離開碼 9 只有 os._exit(9) 那一行給得出來 ——
    # 子行程要是早就炸在別的地方，這條會先紅，不會誤判成「flush 沒作用」。
    chk("  （尺的自證）子行程真的活到被 os._exit(9) 幹掉那一刻", p.returncode, 9)
    chk_true("  （尺的自證）子行程沒有噴例外", not p.stderr.strip(), f"stderr={p.stderr[:300]}")
    got = ([r["p"] for r in rows_of(file_of(d), "t")]
           if file_of(d).exists() else "（檔案根本不存在）")
    chk("  沒 stop() 就被砍，寫檔執行緒寫過的那幾筆一列都沒掉", got,
        [12070.0, 12071.0, 12072.0, 12073.0])

# ================================================================ ③ 不可以拖慢 on_tick
print("=== ③ on_tick 不會被磁碟拖住（最重要的一條）===")


class SlowSink:
    """故意每次 write 慢 50ms 的假檔案。真的沒解耦的話，on_tick 會當場現形。"""

    DELAY = 0.05

    def __init__(self):
        self.writes = 0
        self.slept = 0.0
        self.buf = []

    def write(self, s):
        t0 = time.perf_counter()
        time.sleep(self.DELAY)
        self.slept += time.perf_counter() - t0
        self.writes += 1
        self.buf.append(s)

    def flush(self):
        pass

    def close(self):
        pass


with Sandbox() as d:
    sink = SlowSink()
    w = TW.TickWriter(d, flush_every=0.01, opener=lambda p: sink).start()
    N = 400
    worst = 0.0
    t_all = time.perf_counter()
    for i in range(N):
        t0 = time.perf_counter()
        w.tick(T(9, 0, i % 60, i % 1000), 12030.0 + (i % 7), 1)
        worst = max(worst, time.perf_counter() - t0)
    total = time.perf_counter() - t_all
    time.sleep(0.35)                       # 讓寫檔執行緒真的去慢慢寫
    w.stop(timeout=10)

    # 尺的自證：磁碟真的很慢，而且真的被寫到了。
    # 少了這兩條，「時段判斷壞掉 ⇒ 一筆都沒寫 ⇒ 當然很快」會變成假綠燈。
    chk_true("  （尺的自證）慢 sink 真的有被寫進去", sink.writes >= 1, f"writes={sink.writes}")
    chk_true("  （尺的自證）磁碟真的慢：累計 sleep ≥ 50ms",
             sink.slept >= SlowSink.DELAY, f"slept={sink.slept*1000:.0f}ms")
    chk_true(f"  單筆 on_tick 最慢 {worst*1000:.3f}ms，要 < 5ms",
             worst < 0.005, f"worst={worst*1000:.2f}ms")
    chk_true(f"  {N} 筆合計 {total*1000:.1f}ms，要 < 100ms（沒解耦的話 ≥ {N*50}ms）",
             total < 0.1, f"total={total*1000:.1f}ms")
    chk_true("  資料沒有因此被丟掉", w.dropped == 0, f"dropped={w.dropped}")
    # ⛔ 上面兩條自證（有寫到、磁碟真的慢）**只需要一列就成立** ——
    #    lab-qa 做過針對性突變：讓 tick() 只放第一筆進佇列、之後 399 筆靜靜丟掉，
    #    這一節照樣五項全綠（一筆都沒錄當然快）。所以要驗「全部」，不是「有」。
    chk("  （尺的自證）400 筆全部寫出去，不是只寫了一筆就說很快", w.written, N)

# ================================================================ ④ 佇列滿
print("\n=== ④ 佇列滿了會丟資料，但不可以安靜地少 ===")
with Sandbox() as d:
    logs = []
    # 刻意不 start() 寫檔執行緒 ＝ 模擬「寫檔完全卡死」的最壞情況
    w = TW.TickWriter(d, max_queue=5, log=logs.append)
    for i in range(20):
        w.tick(T(9, 10, i % 60, i), 12040.0 + i, 1)
    chk("  佇列只留得下 5 筆", w.stats()["queued"], 5)
    chk("  多出來的 15 筆有算到", w.dropped, 15)
    w.stop()          # 收尾：把佇列裡的寫出去，並落下痕跡

    kinds = [r["k"] for r in rows_of(file_of(d))]
    marks = rows_of(file_of(d), "x")
    chk("  檔案裡有一列丟棄痕跡", len(marks), 1)
    chk("  痕跡寫得出丟了幾筆", [m["n"] for m in marks], [15])
    # 痕跡列身上沒有交易所時間可用（那一筆連進佇列都沒進來），所以刻意不叫 `t`：
    # 檔頭的 clock 欄位寫著「t 不是本機時鐘」，拿本機時鐘冒充 t 就是把那句話變成謊話。
    chk("  痕跡列沒有 `t`（那是交易所時間，它沒有）", [("t" in m) for m in marks], [False])
    chk("  痕跡列的本機時鐘另外叫 `wt`", [("wt" in m) for m in marks], [True])
    chk("  痕跡列夾得出缺口的起點（前一列的交易所時間）",
        [m.get("after") for m in marks], ["09:10:04.004"])
    chk("  痕跡就在斷點上（排在那批資料後面）", kinds, ["h", "t", "t", "t", "t", "t", "x"])
    chk_true("  主控台也吼了一聲", any("佇列滿" in m for m in logs), f"logs={logs}")
    chk("  留下來的是最早那幾筆（丟新的不丟舊的，檔案不會出現看不見的洞）",
        [r["p"] for r in rows_of(file_of(d), "t")],
        [12040.0, 12041.0, 12042.0, 12043.0, 12044.0])

print("\n=== ④b 寫檔失敗也不可以安靜地少 ===")


class Boom:
    """寫什麼都失敗的假檔案（磁碟滿了／檔案被鎖住）。"""

    def write(self, s):
        raise OSError("disk full (測試用的假錯誤)")

    def flush(self):
        pass

    def close(self):
        pass


with Sandbox() as d:
    logs = []
    w = TW.TickWriter(d, flush_every=0.02, opener=lambda p: Boom(), log=logs.append).start()
    for i in range(5):
        w.tick(T(9, 20, i), 12060.0 + i, 1)
    time.sleep(0.3)
    alive = w._thread.is_alive()      # 一個例外不可以把寫檔執行緒打死
    w.stop()
    chk("  寫不出去的那幾列有算到（lost_io）", w.stats()["lost_io"], 5)
    chk_true("  主控台講了寫檔失敗", any("寫檔失敗" in m for m in logs), f"logs={logs}")
    chk("  寫檔執行緒沒有被例外打死", alive, True)

print("\n=== ④c 日期亂跳不可以把檔案灌成一半檔頭 ===")
# lab-qa 實測：200 筆交錯日期 → 一次 drain 從 1.8ms 變 97.1ms，
# 兩個檔各 200 列裡有 100 列是檔頭（每換一次日期就 close + open + 寫檔頭）。
# 全部發生在寫檔執行緒、on_tick 不受影響 —— 但那條執行緒卡住就是「這段沒錄到」。
with Sandbox() as d:
    D1, D2 = dt.date(2026, 9, 4), dt.date(2026, 9, 5)
    logs = []
    w = TW.TickWriter(d, log=logs.append)      # 不 start()：用 stop() 觸發單獨一次 drain
    for i in range(200):
        w.tick(T(9, 15, i % 60, i % 1000, day=(D1 if i % 2 == 0 else D2)),
               12080.0 + (i % 5), 1)
    w.stop()
    cap = TW.MAX_DAY_SWITCH + 1
    h1 = len(rows_of(file_of(d, D1), "h"))
    h2 = len(rows_of(file_of(d, D2), "h")) if file_of(d, D2).exists() else 0
    n1 = len(rows_of(file_of(d, D1), "t"))
    n2 = len(rows_of(file_of(d, D2), "t")) if file_of(d, D2).exists() else 0
    chk_true(f"  第一個檔只有 {h1} 列檔頭，要 ≤ {cap}（沒守衛的話是 100）", h1 <= cap, f"h={h1}")
    chk_true(f"  第二個檔只有 {h2} 列檔頭，要 ≤ {cap}", h2 <= cap, f"h={h2}")
    chk("  200 列一列都沒少（只是被換了個檔放）", n1 + n2, 200)
    chk("  留得下痕跡，不是安靜地混進去",
        [m["why"] for m in rows_of(file_of(d, D1), "x")], ["date_thrash"])
    chk_true("  主控台也吼了一聲", any("日期跳" in m for m in logs), f"logs={logs}")

# ================================================================ ⑤ 格式
print("\n=== ⑤ 格式：三個月後還看得懂 ===")
with Sandbox() as d:
    w = TW.TickWriter(d, flush_every=0.02).start()
    # ⚠️ 這些數字刻意離真實行情很遠（12xxx）：他的真實紀錄不准進公開 repo，
    #    而「示範用的價格剛好撞到他某一筆」是看的人分不出來的（leak-scan 檔頭有記）。
    w.tick(T(9, 12, 34, 567), 12245.0, 3)
    w.bidask(T(9, 12, 34, 567), 12242.0, 12245.0)
    w.tick(T(9, 12, 34, 567, day=dt.date(2026, 9, 5)), 12300.0, 1)   # 隔天 → 另一個檔
    w.stop()
    head = rows_of(file_of(d), "h")[0]
    chk("  檔頭寫得出每個欄位是什麼", sorted(head["fmt"]),
        ["a", "after", "b", "k", "n", "p", "t", "v", "why", "wt"])
    chk("  檔頭寫得出時間是誰給的", "永豐" in head["clock"], True)
    chk("  檔頭要自己招認「wt 是本機時鐘」這個例外", "wt" in head["clock"], True)
    # 檔案是照「收到的順序」寫的，不保證時間單調遞增（重連補送／SDK 亂序／重啟接檔）。
    # 分析腳本很容易假設有序，所以檔頭要先講。
    chk("  檔頭講明不保證時間單調遞增", "不保證時間單調遞增" in head.get("order", ""), True)
    chk("  檔頭寫得出錄的時段", head["win"], "08:45:00~09:30:00")
    t0 = rows_of(file_of(d), "t")[0]
    chk("  成交列的欄位", sorted(t0), ["k", "p", "t", "v"])
    chk("  時間到毫秒", t0["t"], "09:12:34.567")
    b0 = rows_of(file_of(d), "b")[0]
    chk("  買賣價列的欄位", sorted(b0), ["a", "b", "k", "t"])
    chk("  隔一天就換一個檔", file_of(d, dt.date(2026, 9, 5)).exists(), True)
    # ⚠️ 量要算**上磁碟的位元組**：Windows 文字模式寫出去是 CRLF（每列 +2），
    #    而且 bidask 那型比 tick 長 6 個字元。第一版寫 46 bytes/列 —— 那是「JSON 字串長度、
    #    不含行尾、而且只算 tick」，比實際少了 11%。這一條是**刻意的絆線**：
    #    有人加欄位它就會紅，紅了就順手把 CLAUDE.md 的量估算一起更新。
    #    量的來源是 tick_writer 自己產的那一列（不是測試裡另抄一份），
    #    這樣有人改欄位它才會紅 —— 抄一份的話這條永遠是常數、守不到任何東西。
    ts5 = T(9, 12, 34, 567)
    t_line = TW._line(("t", ts5, 12245.0, 3))       # 已含 "\n"
    b_line = TW._line(("b", ts5, 12242.0, 12245.0))
    t_disk, b_disk = len(t_line) + 1, len(b_line) + 1   # 文字模式寫出去 "\n" → CRLF
    lo = 42_700 * t_disk + 42_700 * b_disk            # tick 42.7k ＋ bidask 同量
    hi = 42_700 * t_disk + 42_700 * 2 * b_disk        # bidask 兩倍量
    chk(f"  上磁碟一列 tick {t_disk}／bidask {b_disk} 位元組（含 CRLF）"
        f"　⇒ 一早上 {lo/1e6:.2f}~{hi/1e6:.2f}MB、一年 {lo*240/1e9:.2f}~{hi*240/1e9:.2f}GB",
        [t_disk, b_disk], [48, 54])

# ================================================================ ⑥ 接線
print("\n=== ⑥ 面板真的有接上去，而且 on_tick 裡沒有半點 I/O ===")
# 這一節不執行 live_panel（那會連永豐），是**讀它的原始碼**做結構檢查。
# 理由：on_tick／on_bidask 是 main() 裡的巢狀函式，叫不到；但「有沒有被接上」與
# 「裡面有沒有偷偷寫檔／print」用 AST 驗得比讀程式碼可靠。
import ast

BANNED = {"write", "flush", "open", "print", "fsync", "dump", "writelines"}
src = (HERE / "live_panel.py").read_text(encoding="utf-8")
tree = ast.parse(src)
cbs = {n.name: n for n in ast.walk(tree)
       if isinstance(n, ast.FunctionDef) and n.name in ("on_tick", "on_bidask")}
chk("  兩個回呼都找得到", sorted(cbs), ["on_bidask", "on_tick"])


def calls(fn):
    out = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute):
                who = f.value.id if isinstance(f.value, ast.Name) else "?"
                out.append(f"{who}.{f.attr}")
            elif isinstance(f, ast.Name):
                out.append(f.id)
    return out


for name, want in (("on_tick", "TICKS.tick"), ("on_bidask", "TICKS.bidask")):
    fn = cbs.get(name)
    got = calls(fn) if fn else []
    chk_true(f"  {name} 有把資料交出去（{want}）", want in got, f"calls={got}")
    bad = sorted({c for c in got if c.split(".")[-1] in BANNED})
    chk(f"  {name} 裡沒有任何同步 I/O（{'/'.join(sorted(BANNED))}）", bad, [])

# ---------------------------------------------------------------- ⑥b 有沒有接上電
print("\n=== ⑥b 有沒有人真的把它接上電（接線本身，不是回呼內部）===")
# ⛔ 上面那幾條只驗兩個回呼**內部**長什麼樣。lab-qa 實測：把 main() 裡的 TICKS.start()
#    換成 pass、或把建構式的 win_start/win_end 拿掉、或改成 win_end=DAY_END（＝錄整個日盤、
#    檔案大 4 倍），這支照樣全綠 —— 而少那一行就是**整個早上零落地**，
#    要到 09:30 印成績單才看得出來，那時候那天的資料已經沒了。
#    看門狗會自動載入磁碟上的任何一版，所以「接線」跟「回呼內部」一樣要有守衛。


def call_lines(fn, want):
    """回傳 fn 裡呼叫 `want`（形如 a.b）的行號，用來比先後順序。"""
    out = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            who = n.func.value.id if isinstance(n.func.value, ast.Name) else "?"
            if f"{who}.{n.func.attr}" == want:
                out.append(n.lineno)
    return sorted(out)


mainfn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
chk_true("  （尺的自證）找得到 main()", mainfn is not None, "live_panel.py 裡沒有 main()")
chk_true("  (a) main() 裡真的有 TICKS.start()（沒有＝整個早上零落地）",
         bool(mainfn) and "TICKS.start" in calls(mainfn),
         f"main() 裡的 {len(calls(mainfn)) if mainfn else 0} 個呼叫裡沒有 TICKS.start")

# (b) 窗口要綁在面板自己的常數上。⚠️ **比名稱不比數值** ——
#     比數值的話，有人把 WATCH_END 從 09:30 改成 13:45 一樣過，而檔案會大 4 倍。
tw_kw = {}
for n in tree.body:
    if (isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "TICKS" for t in n.targets)
            and isinstance(n.value, ast.Call)):
        tw_kw = {k.arg: k.value for k in n.value.keywords}
        break


def kwname(k):
    v = tw_kw.get(k)
    return v.id if isinstance(v, ast.Name) else None


chk("  (b) 錄製窗口綁在面板的常數上（比名稱不比數值）",
    [kwname("win_start"), kwname("win_end")], ["SESSION_OPEN", "WATCH_END"])

# (c) 順序。整個設計的第一順位是「停損看的價先更新」——
#     `st.feed()` 更新的 self.price 就是停損在看的價，落地永遠排在它後面。
#     這件事程式碼註解寫得很大聲，但在這一條之前零守衛。
ot = cbs.get("on_tick")
feed_ln = call_lines(ot, "st.feed") if ot else []
tick_ln = call_lines(ot, "TICKS.tick") if ot else []
chk_true("  （尺的自證）on_tick 裡 st.feed() 與 TICKS.tick() 各找到一個",
         len(feed_ln) == 1 and len(tick_ln) == 1, f"feed={feed_ln} tick={tick_ln}")
chk_true("  (c) st.feed() 排在 TICKS.tick() 前面（停損看的價先更新）",
         bool(feed_ln) and bool(tick_ln) and feed_ln[0] < tick_ln[0],
         f"feed={feed_ln} tick={tick_ln}")

print("\n總結:", "全部通過" if not FAIL else f"{FAIL} 項失敗")
sys.exit(1 if FAIL else 0)
