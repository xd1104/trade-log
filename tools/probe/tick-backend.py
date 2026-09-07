# -*- coding: utf-8 -*-
"""
【細節】分頁的**後端**探針（TICK-TAB-SPEC.md §6.2 / §7.3 / §7.4 / §12-6、§12-10 的後端那一半）。

前端那半在 tools/probe/tick-tab.mjs。這一支不開瀏覽器、不起服務、不連永豐。

⛔ **全程唯讀他的 tick_logs**：第 ① 節會真的去掃 `tools/shioaji/tick_logs/`
   （那是驗「磁碟上現有的輪詢檔不可以被當成逐筆檔」唯一有意義的做法），
   收尾有一節斷言「那個資料夾一個位元組都沒被動過」。其餘所有測試都在暫存區。

跑法：  PYTHONIOENCODING=utf-8 PYTHONUTF8=1 .venv\\Scripts\\python.exe tools\\probe\\tick-backend.py
"""
import ast
import hashlib
import inspect
import io
import json
import os
import shutil
import sys
import tempfile
import textwrap
import threading
import time
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from datetime import date, datetime, time as dtime
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "shioaji"))
import live_panel as LP          # noqa: E402
import tick_synth                # noqa: E402

FAIL = 0
N = 0


def say(ok, name, extra=""):
    global FAIL, N
    N += 1
    if not ok:
        FAIL += 1
    print(("  OK   " if ok else "  FAIL ") + name + (("  " + str(extra)) if extra else ""))


def chk(name, got, want):
    say(got == want, name, "" if got == want else f"(得到 {got!r}，期待 {want!r})")


def tree_hash(p: Path):
    """資料夾的內容指紋（檔名＋大小＋內容雜湊），用來證明我們沒有動過它。"""
    out = []
    if p.exists():
        for f in sorted(p.iterdir()):
            if f.is_file():
                out.append((f.name, f.stat().st_size,
                            hashlib.sha256(f.read_bytes()).hexdigest()))
    return out


REAL_TICKS = LP.HERE / "tick_logs"
BEFORE = tree_hash(REAL_TICKS)

TMP = Path(tempfile.mkdtemp(prefix="tick-backend-"))
print(f"暫存區：{TMP}")
print(f"他的逐筆資料夾（唯讀）：{REAL_TICKS}　現有 {len(BEFORE)} 個檔\n")

# ══ ① 磁碟上現有的輪詢檔不可以被當成逐筆檔（§12-6，唯讀）═══════════════
print("=== ① 磁碟上現有的檔（唯讀）===")
LP.TICK_DIR = REAL_TICKS
LP._TICK_SNIFFED.clear()
LP._TICK_SAID.clear()
buf = io.StringIO()
with redirect_stdout(buf):
    real_days = LP.tick_days()
console = buf.getvalue()
names = [f.name for f in REAL_TICKS.iterdir()] if REAL_TICKS.exists() else []
polled_like = [n for n in names if LP._TICK_NAME.match(n)]
print(f"  （現況）符合 YYYY-MM-DD.jsonl 的檔：{polled_like or '無'}")
print(f"  （現況）days={[d['d'] for d in real_days['days']]}　skipped={real_days['skipped']}")
for n in polled_like:
    d = n[:-6]
    with (REAL_TICKS / n).open("rb") as f:
        head = f.read(8192)
    has_k = any(('"k"' in ln.decode("utf-8", "ignore"))
                for ln in head.split(b"\n")[:-1] if ln.strip())
    if not has_k:
        say(d not in [x["d"] for x in real_days["days"]],
            f"{n}（內容沒有 k 欄位＝輪詢 schema）不在 days 裡")
        say(d in real_days["skipped"], f"{n} 出現在 skipped 裡")
        say(("跳過 " + n) in console, "server console 有印出跳過的原因",
            console.strip().splitlines()[:1])
if not polled_like:
    print("  （這台機器上目前沒有符合逐筆檔名的檔，這一節改由 ② 的合成負控組覆蓋）")

# ══ ② 嗅探的負控組：合法的逐筆檔必須進得去 ════════════════════════════
print("\n=== ② 嗅探：負控組 ===")
SB = TMP / "sniff"
SB.mkdir()
LP.TICK_DIR = SB
LP._TICK_SNIFFED.clear()
LP._TICK_SAID.clear()
today = date.today()
DTS = tick_synth.fixture_dates(today)
tick_synth.synth_day(SB / f"{DTS['tiny']}.jsonl", ticks=2, bidask=0, first=31505, last=31506)
tick_synth.synth_polled(SB / f"{DTS['decoy']}.jsonl", 50)
(SB / f"{DTS['blank']}.jsonl").write_bytes(
    (json.dumps({"k": "h", "v": 1}) + "\r\n").encode())
tick_synth.synth_polled(SB / f"{DTS['tiny']}-polled.jsonl", 10)
buf = io.StringIO()
with redirect_stdout(buf):
    dd = LP.tick_days()
got = [x["d"] for x in dd["days"]]
say(DTS["tiny"] in got, "只有 3 列的合法逐筆檔出現在 days 裡（證明尺不是什麼都跳過）")
say(DTS["decoy"] not in got, "輪詢 schema 的檔不在 days 裡")
say(DTS["decoy"] in dd["skipped"], "輪詢 schema 的檔出現在 skipped 裡")
say(f"{DTS['tiny']}-polled" not in got and DTS["tiny"] + "-polled" not in dd["skipped"],
    "帶 -polled 後綴的檔連嗅探都不走（檔名就擋掉）")
chk("只有檔頭的那天 empty=True",
    [x["empty"] for x in dd["days"] if x["d"] == DTS["blank"]], [True])
chk("since ＝最早那一天", dd["since"], min(got))
chk("today 由後端給（前端不准用 new Date()）", dd["today"], str(today))
say("跳過" in buf.getvalue(), "跳過的檔有印到 console（⛔ 不可以靜靜跳過）")

# ══ ③ 解析：一整天（真實量級）══════════════════════════════════════
print("\n=== ③ 解析一整天（真實量級）===")
PB = TMP / "parse"
PB.mkdir()
DAY = DTS["gappy"]
lines = tick_synth.synth_day(PB / f"{DAY}.jsonl", ticks=90000, bidask=45000,
                             first=31502, last=34199, seed=20260908)
size = (PB / f"{DAY}.jsonl").stat().st_size
LP.TICK_DIR = PB
LP.TICK_CACHE.clear()
t0 = time.perf_counter()
day1 = LP.tick_day(DAY)
cold = time.perf_counter() - t0
t0 = time.perf_counter()
day2 = LP.tick_day(DAY)
warm = time.perf_counter() - t0
payload = len(json.dumps(day1, ensure_ascii=False).encode())
print(f"  {lines:,} 列 / {size/1048576:.2f} MB　冷解析 {cold:.3f} 秒、"
      f"走快取 {warm*1000:.1f} ms、payload {payload/1024:.1f} KB")
say(cold < 2.0, "冷解析 < 2 秒", f"= {cold:.3f} 秒")
say(warm < 0.1, "第二次走快取（沒有重新解析）", f"= {warm*1000:.1f} ms")
say(payload < 400 * 1024, "payload 遠小於原始 jsonl",
    f"{payload/1024:.1f} KB vs {size/1024:.0f} KB（小 {size/payload:.0f} 倍）")
chk("桶數不超過一天的上限 2,700", len(day1["s"]) <= 2700, True)
chk("s 嚴格遞增", all(day1["s"][i] > day1["s"][i - 1] for i in range(1, len(day1["s"]))), True)
chk("成交筆數", day1["n"], 90000)
chk("壞列數", day1["bad"], 0)
say(all(day1["h"][i] >= day1["l"][i] for i in range(len(day1["s"]))),
    "每個桶的高 ≥ 低")
say(all(day1["h"][i] >= max(day1["o"][i], day1["c"][i]) for i in range(len(day1["s"]))),
    "每個桶的高 ≥ max(開,收)")
say(sum(day1["vq"]) > 0, "成交量有加總（不是筆數也不是平均）", f"總量 {sum(day1['vq']):,}")
say(day1["first"] < day1["last"], "first/last 涵蓋時段",
    f"{day1['first']}~{day1['last']}")

# ── 買賣價帶（bl/ah）必須真的有東西（lab-qa 2026-09-07 退件 R1）───────────
# 「折線＋買賣價帶」是**出貨的三種圖種之一**。後端整個不記錄 k=="b" 的話，
# 圖上就是一條沒有價帶的折線 —— 而前端 108 項探針裡沒有任何一條驗過「價帶真的有資料」，
# QA 把 `elif k == "b":` 改成永不匹配，前後端 166 項全綠。**這一條就是補那個洞。**
band = sum(1 for i in range(len(day1["s"]))
           if day1["bl"][i] is not None and day1["ah"][i] is not None)
say(band >= len(day1["s"]) * 0.9,
    "買賣價帶：有 bl 與 ah 的桶數佔比（k==\"b\" 真的有被記錄）",
    f"{band}/{len(day1['s'])} 個桶")
say(all(day1["ah"][i] >= day1["bl"][i] for i in range(len(day1["s"]))
        if day1["bl"][i] is not None and day1["ah"][i] is not None),
    "每個桶的最高賣價 ≥ 最低買價")
say(all(day1["ah"][i] >= day1["h"][i] * 0.9 for i in range(len(day1["s"]))
        if day1["ah"][i] is not None),
    "價帶跟成交價在同一個數量級（不是把別的欄位塞進來）")
# 負控組：把 `elif k == "b":` 那個分支拔掉 ⇒ 價帶必須整個消失
_src_load = inspect.getsource(LP._tick_load)
BNEEDLE = '        elif k == "b":\n'
say(BNEEDLE in _src_load, "負控組的目標字串還在原始碼裡（尺的自證）")
if BNEEDLE in _src_load:
    ns = {}
    exec(compile(_src_load.replace(BNEEDLE, '        elif k == "__never__":\n'),
                 "<mutated>", "exec"), LP.__dict__, ns)
    _orig_load = LP._tick_load
    LP._tick_load = ns["_tick_load"]
    LP.TICK_CACHE.clear()
    dayN = LP.tick_day(DAY)
    nband = sum(1 for i in range(len(dayN["s"]))
                if dayN["bl"][i] is not None or dayN["ah"][i] is not None)
    say(nband == 0, "負控組：拿掉 k==\"b\" 的分支之後，價帶必須一個桶都不剩",
        f"還剩 {nband} 個桶")
    say(len(dayN["s"]) == len(day1["s"]),
        "負控組的自證：成交那半沒有被弄壞（只有價帶不見了）")
    LP._tick_load = _orig_load
    LP.TICK_CACHE.clear()
    day1 = LP.tick_day(DAY)
    say(sum(1 for i in range(len(day1["s"])) if day1["bl"][i] is not None) == band,
        "裝回去之後價帶回來了")

# ══ ④ 增量：同 s 覆蓋，不是 concat（§7.3）═══════════════════════════
print("\n=== ④ 增量合併：同 s 覆蓋，不是 concat ===")
IB = TMP / "incr"
IB.mkdir()
IDAY = DTS["full"]
tick_synth.synth_day(IB / f"{IDAY}.jsonl", ticks=6000, bidask=3000,
                     first=31502, last=33000, seed=11)
LP.TICK_DIR = IB
LP.TICK_CACHE.clear()
first = LP.tick_day(IDAY)
lastS = first["s"][-1]
# 續寫：故意讓新的那一批**跨過**目前最後一個桶（那個桶上次拿到時還沒收完）
more = []
px = 12000
for i in range(1200):
    sec = 31500 + lastS + i // 8
    px += (i % 5) - 2
    more.append(json.dumps({"k": "t", "t": tick_synth._hms(sec, (i * 31) % 1000),
                            "p": float(px), "v": 2}, separators=(",", ":")))
tick_synth.append_lines(IB / f"{IDAY}.jsonl", more)
inc = LP.tick_day(IDAY, lastS)
chk("增量回的第一個桶就是 from 本身（那個桶可能還沒收完）", inc["s"][0], lastS)
overlap = set(first["s"]) & set(inc["s"])
say(len(overlap) >= 1, "增量與既有資料真的有重疊（尺的自證：沒有重疊就測不到 concat 的差別）",
    f"重疊 {len(overlap)} 個桶")
# 負控組（算出來的）：無腦 concat 會產生重複的秒
concat = first["s"] + inc["s"]
say(len(concat) != len(set(concat)),
    "負控組：無腦 concat 會出現重複的秒（圖上看不出來、只有量會變兩倍）",
    f"{len(concat)} 個 vs 去重後 {len(set(concat))} 個")
# 產品的做法：整份重讀一次，結果必須跟「舊的 ＋ 增量」合併後完全一樣
LP.TICK_CACHE.clear()
whole = LP.tick_day(IDAY)
merged = {s: [first["o"][i], first["h"][i], first["l"][i], first["c"][i], first["vq"][i]]
          for i, s in enumerate(first["s"])}
for i, s in enumerate(inc["s"]):
    merged[s] = [inc["o"][i], inc["h"][i], inc["l"][i], inc["c"][i], inc["vq"][i]]
same = ([s for s in sorted(merged)] == whole["s"]
        and [merged[s][0] for s in sorted(merged)] == whole["o"]
        and [merged[s][1] for s in sorted(merged)] == whole["h"]
        and [merged[s][2] for s in sorted(merged)] == whole["l"]
        and [merged[s][3] for s in sorted(merged)] == whole["c"]
        and [merged[s][4] for s in sorted(merged)] == whole["vq"])
say(same, "「同 s 覆蓋」合併出來的結果＝整份重讀的結果")

# ══ ⑤ 半列：append 不是原子的（§7.4）════════════════════════════════
print("\n=== ⑤ 讀到半列的時候 ===")
LP.TICK_CACHE.clear()
base = LP.tick_day(IDAY)
tick_synth.append_half_line(IB / f"{IDAY}.jsonl")
half = LP.tick_day(IDAY)
chk("半列不算壞列（丟掉、位移退回上一個換行處）", half["bad"], base["bad"])
chk("半列不會生出新的桶", len(half["s"]), len(base["s"]))
# 把那一列補完，它必須被完整讀進來
tick_synth.append_lines(IB / f"{IDAY}.jsonl",
                        ['0:00.000","p":12345.0,"v":9}'])
done = LP.tick_day(IDAY)
say(done["n"] == base["n"] + 1, "補完之後那一列被完整讀進來",
    f"{base['n']} → {done['n']}")
chk("補完之後仍然沒有壞列", done["bad"], base["bad"])
# 負控組：把「退回上一個換行處」那一段拿掉，半列必須變成壞列
src = inspect.getsource(LP._tick_load)
NEEDLE = ' cut = chunk.rfind(b"\\n")\n    chunk = b"" if cut < 0 else chunk[:cut + 1]'
say(NEEDLE in src, "負控組的目標字串還在原始碼裡（尺的自證）")
if NEEDLE in src:
    ns = {}
    exec(compile(src.replace(NEEDLE, " pass"), "<mutated>", "exec"), LP.__dict__, ns)
    orig = LP._tick_load
    LP._tick_load = ns["_tick_load"]
    LP.TICK_CACHE.clear()
    b2 = LP.tick_day(IDAY)
    tick_synth.append_half_line(IB / f"{IDAY}.jsonl")
    h2 = LP.tick_day(IDAY)
    say(h2["bad"] > b2["bad"], "負控組：拿掉半列處理之後，半列會被算成壞列",
        f"bad {b2['bad']} → {h2['bad']}")
    LP._tick_load = orig
    LP.TICK_CACHE.clear()
    tick_synth.append_lines(IB / f"{IDAY}.jsonl", ['0:01.000","p":12345.0,"v":1}'])
    h3 = LP.tick_day(IDAY)
    say(h3["bad"] == base["bad"], "裝回去之後又不算壞列", f"bad={h3['bad']}")

# ══ ⑤b 快取：上限 ＋ 有效性（lab-qa 退件 M1／M5）═══════════════════════
# M1 拿掉 TICK_CACHE 的上限（記憶體無上限）、M5 只比 mtime 不比 size —— 兩個突變
# 原本都打不紅。兩個都是「安靜地壞」：M1 要一年後才看得出來，M5 是**畫面停在舊資料**。
print("\n=== ⑤b 快取的上限與有效性 ===")
KB = TMP / "keep"
KB.mkdir()
KDAYS = ["2019-01-%02d" % (i + 1) for i in range(LP.TICK_KEEP + 3)]
for i, kd in enumerate(KDAYS):
    # ⚠️ 種子固定（不要用 hash()，那個每個行程都不一樣，探針必須可重跑）
    tick_synth.synth_day(KB / f"{kd}.jsonl", ticks=20, bidask=10,
                         first=31502, last=31600, seed=100 + i)
LP.TICK_DIR = KB
LP.TICK_CACHE.clear()
for kd in KDAYS:
    LP.tick_day(kd)
say(len(LP.TICK_CACHE) <= LP.TICK_KEEP,
    f"連續讀 {len(KDAYS)} 天之後，TICK_CACHE 不超過上限 {LP.TICK_KEEP}",
    f"= {len(LP.TICK_CACHE)} 天")
say(KDAYS[-1] in LP.TICK_CACHE, "留下來的是最近讀的那幾天（不是把剛讀的丟掉）")
KNEEDLE = "    if len(TICK_CACHE) > TICK_KEEP:\n"
say(KNEEDLE in _src_load, "M1 負控組的目標字串還在原始碼裡（尺的自證）")
if KNEEDLE in _src_load:
    ns = {}
    exec(compile(_src_load.replace(KNEEDLE, "    if False:\n"), "<mutated>", "exec"),
         LP.__dict__, ns)
    _orig_load = LP._tick_load
    LP._tick_load = ns["_tick_load"]
    LP.TICK_CACHE.clear()
    for kd in KDAYS:
        LP.tick_day(kd)
    say(len(LP.TICK_CACHE) > LP.TICK_KEEP,
        "M1 負控組：拿掉上限之後快取真的會無限長",
        f"= {len(LP.TICK_CACHE)} 天（上限應該是 {LP.TICK_KEEP}）")
    LP._tick_load = _orig_load
    LP.TICK_CACHE.clear()

# M5：mtime 沒變、但檔案長大了 ⇒ 快取一定要失效。
# 【為什麼會發生】看門狗重啟後同一秒內續寫、或檔案系統的 mtime 解析度只到秒（FAT/網路碟），
# 都會出現「內容變了、mtime 沒變」。只比 mtime 的話畫面就停在舊資料 —— 而**畫面看不出來**。
CB = TMP / "csize"
CB.mkdir()
CD = "2019-02-01"
cpath = CB / f"{CD}.jsonl"
tick_synth.synth_day(cpath, ticks=40, bidask=20, first=31502, last=31600, seed=77)
LP.TICK_DIR = CB
LP.TICK_CACHE.clear()
c1 = LP.tick_day(CD)
cst = cpath.stat()
tick_synth.append_lines(cpath, [json.dumps(
    {"k": "t", "t": tick_synth._hms(31700 + i, 0), "p": 12100.0 + i, "v": 1},
    separators=(",", ":")) for i in range(40)])
os.utime(cpath, (cst.st_atime, cst.st_mtime))        # 把 mtime 改回去：只有 size 變了
say(cpath.stat().st_mtime == cst.st_mtime and cpath.stat().st_size != cst.st_size,
    "造出「mtime 一樣、size 不一樣」的檔（尺的自證）",
    f"size {cst.st_size} → {cpath.stat().st_size}")
c2 = LP.tick_day(CD)
say(c2["n"] > c1["n"], "同一個 mtime、size 變了 ⇒ 快取失效、續讀新資料",
    f"{c1['n']} → {c2['n']} 筆")
SNEEDLE = 'ent["mtime"] == st.st_mtime and ent["size"] == st.st_size'
say(SNEEDLE in _src_load, "M5 負控組的目標字串還在原始碼裡（尺的自證）")
if SNEEDLE in _src_load:
    ns = {}
    exec(compile(_src_load.replace(SNEEDLE, 'ent["mtime"] == st.st_mtime'),
                 "<mutated>", "exec"), LP.__dict__, ns)
    _orig_load = LP._tick_load
    LP._tick_load = ns["_tick_load"]
    LP.TICK_CACHE.clear()
    c3 = LP.tick_day(CD)
    st3 = cpath.stat()
    tick_synth.append_lines(cpath, [json.dumps(
        {"k": "t", "t": tick_synth._hms(31800 + i, 0), "p": 12200.0 + i, "v": 1},
        separators=(",", ":")) for i in range(40)])
    os.utime(cpath, (st3.st_atime, st3.st_mtime))
    c4 = LP.tick_day(CD)
    say(c4["n"] == c3["n"], "M5 負控組：只比 mtime 的話，檔案長大了畫面還是舊的",
        f"{c3['n']} → {c4['n']} 筆（應該要變多才對）")
    LP._tick_load = _orig_load
    LP.TICK_CACHE.clear()
    c5 = LP.tick_day(CD)
    say(c5["n"] > c4["n"], "裝回去之後又讀得到新增那段", f"{c4['n']} → {c5['n']} 筆")

# ══ ⑥ 痕跡列與壞列不可以吞掉 ══════════════════════════════════════
print("\n=== ⑥ 缺口與壞列 ===")
GB = TMP / "gap"
GB.mkdir()
GD = DTS["tiny"]
tick_synth.write_gappy(GB, GD, clean=False, big=True)
LP.TICK_DIR = GB
LP.TICK_CACHE.clear()
g = LP.tick_day(GD)
chk("痕跡列（queue_full）有回報", [x["why"] for x in g["gaps"]], ["queue_full"])
chk("丟棄筆數", g["gaps"][0]["n"], 128)
say(g["gaps"][0]["sec"] is not None, "痕跡列的 sec 由 after（前一列的交易所時間）推出來",
    f"sec={g['gaps'][0]['sec']}")
say(g["bad"] == 2, "壞列有計數不吞掉", f"bad={g['bad']}")
say(g["heads"] == 2, "多列檔頭有數出來（面板當天重啟過幾次）", f"heads={g['heads']}")
say(g["first"].startswith("09:03") and g["s"][0] == 1080,
    # ⚠️ 原本是「少 17 分鐘、first 落在 09:02」，而 09:02 撞到他一筆真實交易的分鐘
    #    （lab-qa 2026-09-07 抓到；leak-scan 對 HH:MM 是死角）。命中一律改掉、不加豁免。
    "開頭少 18 分鐘（first 落在 09:03，相對 08:45 的第 1080 秒）",
    f"first={g['first']} s[0]={g['s'][0]}")
holes = [(g["s"][i - 1], g["s"][i]) for i in range(1, len(g["s"])) if g["s"][i] - g["s"][i - 1] > 30]
say(len(holes) >= 1, "中間那一段完全沒有資料（前端會畫斜線）", f"{holes[:2]}")
# 負控組：補滿之後這些全部要消失
tick_synth.write_gappy(GB, GD, clean=True, big=True)
LP.TICK_CACHE.clear()
g2 = LP.tick_day(GD)
say(not g2["gaps"] and g2["bad"] == 0 and g2["s"][0] <= 30,
    "負控組：補滿之後缺口／壞列／開頭缺一段全部消失",
    f"gaps={len(g2['gaps'])} bad={g2['bad']} first={g2['first']}")

# ══ ⑥b 壞掉的時間戳不可以用 sec=0 頂替（lab-qa 退件 M8）════════════════
# 這是這個專案明令禁止的「**安靜地少**」的另一種形狀：時間看不懂就當成 0，
# 那一筆會被塞進「當日第 0 秒」（相對 08:45 是 **-31500**）—— 圖上根本畫不到，
# 而 `bad` 是 0 ⇒ 畫面理直氣壯地說「沒有壞列」。**看不懂就要算進 bad，不准猜。**
print("\n=== ⑥b 壞掉的時間戳：計入 bad，不可以用 sec=0 頂替 ===")
XB = TMP / "badts"
XB.mkdir()
XD = "2019-03-01"
good = tick_synth.synth_lines(ticks=60, bidask=30, first=31502, last=31600, seed=9)
BADTS = 5
for i in range(BADTS):
    good.insert(3 + i * 7, json.dumps({"k": "t", "t": "??", "p": 12345.0, "v": 1},
                                      separators=(",", ":")))
(XB / f"{XD}.jsonl").write_bytes(("\r\n".join(good) + "\r\n").encode("utf-8"))
LP.TICK_DIR = XB
LP.TICK_CACHE.clear()
xd = LP.tick_day(XD)
chk("看不懂的時間戳整整 5 列都算進 bad", xd["bad"], BADTS)
say(min(xd["s"]) >= 0, "沒有任何桶落在時段之外（sec=0 會變成 -31500）",
    f"最小的桶 s={min(xd['s'])}")
chk("那 5 列不算成交筆數", xd["n"], 60)
TNEEDLE = ('            sec = _tick_sec(o.get("t"))\n'
           '            if sec is None:\n'
           '                ent["bad"] += 1\n'
           '                continue\n')
say(TNEEDLE in _src_load, "M8 負控組的目標字串還在原始碼裡（尺的自證）")
if TNEEDLE in _src_load:
    ns = {}
    exec(compile(_src_load.replace(
        TNEEDLE, '            sec = _tick_sec(o.get("t")) or 0\n'),
        "<mutated>", "exec"), LP.__dict__, ns)
    _orig_load = LP._tick_load
    LP._tick_load = ns["_tick_load"]
    LP.TICK_CACHE.clear()
    xn = LP.tick_day(XD)
    say(xn["bad"] == 0 and min(xn["s"]) < 0,
        "M8 負控組：改成 sec=0 頂替之後，bad 歸零、資料落到時段之外（安靜地少）",
        f"bad={xn['bad']} 最小的桶 s={min(xn['s'])}")
    LP._tick_load = _orig_load
    LP.TICK_CACHE.clear()
    xb = LP.tick_day(XD)
    chk("裝回去之後 bad 又數得出來", xb["bad"], BADTS)

# ══ ⑦ complete 由後端算（不可以讓前端用 new Date()）══════════════════
print("\n=== ⑦ complete 由後端算 ===")
t = str(today)
chk("過去的日子＝已完成", LP._tick_complete("2020-01-02"), True)
chk("今天、09:00 ⇒ 還沒完成",
    LP._tick_complete(t, datetime.combine(today, dtime(9, 0))), False)
chk("今天、09:31:30 ⇒ 已完成",
    LP._tick_complete(t, datetime.combine(today, dtime(9, 31, 30))), True)
chk("今天、09:30:59 ⇒ 還沒完成",
    LP._tick_complete(t, datetime.combine(today, dtime(9, 30, 59))), False)

# ══ ⑧ ⛔ 絕對不可以碰 state_lock（§7.4）═══════════════════════════
print("\n=== ⑧ 解析路徑不可以碰 state_lock ===")
tree = ast.parse((LP.HERE / "live_panel.py").read_text(encoding="utf-8"))
funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}


def uses(name, needle):
    fn = funcs.get(name)
    if fn is None:
        return None
    return needle in ast.dump(fn)


for f in ("tick_days", "tick_day", "_tick_load", "_tick_sniff", "tick_trades",
          "_tick_complete"):
    say(uses(f, "state_lock") is False, f"{f}() 沒有碰 state_lock")
# 負控組：尺本身要抓得到「真的有用 state_lock 的函式」
say(uses("update_state", "state_lock") is True,
    "負控組：同一把尺在 update_state() 裡抓得到 state_lock（證明尺沒壞）")
say(uses("tick_day", "TICK_LOCK") is True, "tick_day() 拿的是 TICK_LOCK")

# ══ ⑨ 端點：把面板自己的 Handler 架起來，真的打進去 ═══════════════════
#
# ⛔ 這一節**不可以**再用「探針自己重跑一次正則」或「字面字串有沒有出現在原始碼裡」。
#    2026-09-07 lab-qa 退件 R2／R3：舊版第 ⑨ 節四條「date='../../secrets' 會被擋掉」
#    是探針自己呼叫 `LP.re.match(...)` —— **恆真，跟 handler 一點關係都沒有**；
#    另一條 `src_h.index('/api/tick/days')` 命中的是**上面那行註解**裡的字串。
#    QA 把 handler 的守衛與 days 路由分別改成永不匹配，58/58 照樣全綠。
#    現在改成起一個 `ThreadingHTTPServer`（空閒埠）真的送 HTTP 請求。
# ⛔ 埠一律 0（讓 OS 給空閒埠），**絕對不碰 8770**（Benson 的面板正開著）。
print("\n=== ⑨ 端點（真的起服務打進去，不是重跑一次正則）===")
EPROOT = TMP / "ep"
EP = EPROOT / "tick_logs"
EP.mkdir(parents=True)
EDAY = "2019-04-01"
tick_synth.synth_day(EP / f"{EDAY}.jsonl", ticks=200, bidask=100,
                     first=31502, last=31900, seed=3)
# 穿越的目標：`TICK_DIR/../../secrets.jsonl`。這裡故意放一個**合法的逐筆檔**，
# 這樣守衛失效時端點會真的回 200 ＋ 資料（而不是剛好因為檔案不存在而 404）——
# 「檔案不存在所以沒事」不是防線。
tick_synth.synth_day(TMP / "secrets.jsonl", ticks=50, bidask=0,
                     first=31502, last=31600, seed=4)
LP.TICK_DIR = EP
LP.TICK_CACHE.clear()
LP._TICK_SNIFFED.clear()
LP._TICK_SAID.clear()
srv = ThreadingHTTPServer(("127.0.0.1", 0), LP.Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
say(PORT != 8770, f"服務架在空閒埠 {PORT}（⛔ 不是 8770）")


def hit(path):
    """真的送一個 GET，回 (status, body)。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:200]}


def tree_list(p):
    return sorted(str(q.relative_to(p)) for q in p.rglob("*"))


SRC_GET = textwrap.dedent(inspect.getsource(LP.Handler.do_GET))
ORIG_GET = LP.Handler.do_GET


def mutate_get(frm, to, tag):
    """把 do_GET 的原始碼改一個字串再裝回去（負控組唯一的工具，跟產品程式不分岔）。"""
    if frm not in SRC_GET:
        say(False, f"{tag} 負控組的目標字串還在 do_GET 裡（尺的自證）")
        return False
    ns = {}
    exec(compile(SRC_GET.replace(frm, to), "<mutated do_GET>", "exec"), LP.__dict__, ns)
    LP.Handler.do_GET = ns["do_GET"]
    say(True, f"{tag} 負控組已套用（do_GET 換成突變版）")
    return True


# ── ⑨-1 /api/tick/days 回的是清單，不是 day 的 400/404（R3）──────────────
code, body = hit("/api/tick/days")
chk("/api/tick/days 的狀態碼", code, 200)
say(isinstance(body.get("days"), list), "回的是 days 清單（不是 day 那條路由的錯誤）",
    f"keys={sorted(body.keys())}")
chk("清單裡就是那一天", [d["d"] for d in body.get("days", [])], [EDAY])
chk("today 由後端給", body.get("today"), str(date.today()))

# ── ⑨-2 /api/tick/day 正常路徑 ─────────────────────────────────────────
code, body = hit(f"/api/tick/day?date={EDAY}")
chk(f"/api/tick/day?date={EDAY} 的狀態碼", code, 200)
say(body.get("date") == EDAY and len(body.get("s") or []) > 0,
    "拿得到那一天的桶", f"{len(body.get('s') or [])} 個桶")
code, body = hit("/api/tick/day?date=2019-04-09")
chk("合法格式但沒有那個檔 ⇒ 404（跟 400 分得出來）", code, 404)

# ── ⑨-3 路徑穿越必須被端點擋掉（R2）────────────────────────────────────
BEFORE_EP = tree_list(TMP)
for bad_date, tag in (("../../secrets", "相對路徑"),
                      ("..%2F..%2Fsecrets", "URL 編碼過的相對路徑"),
                      ("2026-9-8", "少補零"),
                      ("2026-09-08x", "後面多接東西"),
                      ("", "空字串")):
    code, body = hit(f"/api/tick/day?date={bad_date}")
    say(code == 400, f"date={bad_date!r}（{tag}）⇒ 端點回 400", f"得到 {code}")
    say("s" not in body, f"date={bad_date!r} 沒有回出任何資料")
chk("打了這些請求之後暫存區沒有多出檔案", tree_list(TMP), BEFORE_EP)

# 負控組（就是 QA 那個突變）：把格式檢查短路掉，**字面字串原封不動**
if mutate_get('if not want or not re.match(r"^\\d{4}-\\d{2}-\\d{2}$", want):',
              'if not want or (False and not re.match(r"^\\d{4}-\\d{2}-\\d{2}$", want)):',
              "R2"):
    code, body = hit("/api/tick/day?date=../../secrets")
    say(code == 200 and len(body.get("s") or []) > 0,
        "R2 負控組：守衛短路之後，`../../secrets` 真的讀得到 TICK_DIR 外面的檔",
        f"狀態 {code}、{len(body.get('s') or [])} 個桶")
    LP.Handler.do_GET = ORIG_GET
    code, _ = hit("/api/tick/day?date=../../secrets")
    chk("裝回去之後又擋得住", code, 400)

# 負控組（QA 那個突變）：把 days 的路由改成永不匹配 ⇒ 會掉進 day 那條路由
if mutate_get('if self.path.startswith("/api/tick/days"):',
              'if self.path.startswith("/api/tick/days__never__"):',
              "R3"):
    code, body = hit("/api/tick/days")
    say(code != 200 or not isinstance(body.get("days"), list),
        "R3 負控組：days 路由壞掉之後，/api/tick/days 不再回清單",
        f"狀態 {code}、keys={sorted(body.keys())}")
    LP.Handler.do_GET = ORIG_GET
    code, body = hit("/api/tick/days")
    say(code == 200 and isinstance(body.get("days"), list), "裝回去之後又回清單")

srv.shutdown()
srv.server_close()
say(LP.Handler.do_GET is ORIG_GET, "收尾時 do_GET 是原版（突變都拆乾淨了）")

# ══ ⑩ 收尾：他的 tick_logs 一個位元組都沒被動過 ═════════════════════
print("\n=== ⑩ 收尾 ===")
AFTER = tree_hash(REAL_TICKS)
chk("全程沒有動到他的 tick_logs（檔名／大小／內容雜湊逐項比對）", AFTER, BEFORE)
say(str(LP.TICK_DIR).startswith(str(TMP)) or LP.TICK_DIR == REAL_TICKS,
    "收尾時 TICK_DIR 沒有指到別的地方", str(LP.TICK_DIR))
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n總結：{N - FAIL}/{N} 通過")
sys.exit(1 if FAIL else 0)
