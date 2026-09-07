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
# 磁碟上**真的**有取樣檔的話，它要看得到、而且標成取樣（2026-09-07 的整條需求）。
# ⛔ 這裡只斷言種類與有沒有列進去，**一個他的數字都不印**（筆數／時段／價格都是他的紀錄）。
real_polled = [n for n in names if LP._TICK_POLLED_NAME.match(n)]
if real_polled:
    rp = {x["d"]: x for x in real_days["days"]}
    for n in real_polled:
        d = LP._TICK_POLLED_NAME.match(n).group(1)
        has_tick = (REAL_TICKS / f"{d}.jsonl").exists()
        say(d in rp, f"{n} 出現在 days 裡（以前完全看不到）")
        chk(f"{n} 的種類標記", (rp.get(d) or {}).get("kind"),
            "tick" if has_tick else "polled")
else:
    print("  （這台機器上目前沒有 -polled 檔，取樣那半由 ②b 的合成資料覆蓋）")

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
    "-polled 不會被當成日期的一部分（它掛在同一天底下，不是新的一天）")
# 同一天兩種檔都有 ⇒ **逐筆優先**、另一份標成 alt（tick_days() 的取捨，見那裡的說明）
KINDS = {x["d"]: (x["kind"], x["alt"]) for x in dd["days"]}
chk("兩種檔都有的那天：kind=tick、alt=True", KINDS.get(DTS["tiny"]), ("tick", True))
chk("只有逐筆檔的那天：kind=tick、alt=False", KINDS.get(DTS["blank"]), ("tick", False))
chk("只有檔頭的那天 empty=True",
    [x["empty"] for x in dd["days"] if x["d"] == DTS["blank"]], [True])
chk("since ＝最早那一天", dd["since"], min(got))
chk("today 由後端給（前端不准用 new Date()）", dd["today"], str(today))
say("跳過" in buf.getvalue(), "跳過的檔有印到 console（⛔ 不可以靜靜跳過）")

# ══ ②b 取樣檔（YYYY-MM-DD-polled.jsonl）也要看得到 ════════════════════
#
# 2026-09-07：他早上的行情只有取樣檔（面板 12:16 才重啟、逐筆落地那時才生效），
# 而【細節】分頁**完全讀不到它** —— 因為檔名不符 YYYY-MM-DD.jsonl。
# 「不要讓取樣冒充逐筆」的正解是**標示清楚**，不是整個不給看。
#
# ⛔⛔ 這一節是「多接受一種**檔名**」，**不是放寬 schema 檢查**。
#      上面 ① / ② 那條「逐筆檔名 ＋ 取樣內容 ⇒ skipped」必須原封不動 ——
#      這一節底下就有一條負控組再驗一次它沒有被弄鬆。
print("\n=== ②b 取樣檔：讀得到，而且標得出來 ===")
PL = TMP / "polled"
PL.mkdir()
PDAY = DTS["polled"]
prows = tick_synth.synth_polled_day(PL / f"{PDAY}-polled.jsonl", PDAY,
                                    first=32700, last=34199, seed=8811, nopx=3)
# ⛔ 對照組：同一個資料夾裡放一個「逐筆檔名 ＋ 取樣內容」的檔，它**必須繼續被擋掉**
tick_synth.synth_polled(PL / f"{DTS['decoy']}.jsonl", 60)
LP.TICK_DIR = PL
LP.TICK_CACHE.clear()
LP._TICK_SNIFFED.clear()
LP._TICK_SAID.clear()
buf = io.StringIO()
with redirect_stdout(buf):
    pdd = LP.tick_days()
pgot = {x["d"]: x for x in pdd["days"]}
say(PDAY in pgot, "取樣檔出現在 days 裡（以前完全看不到）", f"days={sorted(pgot)}")
chk("而且帶正確的種類標記", (pgot.get(PDAY) or {}).get("kind"), "polled")
chk("取樣檔那天不是 empty", (pgot.get(PDAY) or {}).get("empty"), False)
# ⛔ 原始目的：假逐筆檔（逐筆檔名 ＋ 取樣內容）仍然要被擋掉並列進 skipped
say(DTS["decoy"] not in pgot, "⛔ 假逐筆檔（逐筆檔名＋取樣內容）仍然不在 days 裡")
say(DTS["decoy"] in pdd["skipped"], "⛔ 假逐筆檔仍然出現在 skipped 裡")
say("跳過" in buf.getvalue(), "⛔ 假逐筆檔被跳過的原因仍然印到 console")

pd_ = LP.tick_day(PDAY)
chk("/api/tick/day 回的 kind", pd_["kind"], "polled")
say(len(pd_["s"]) > 100, "轉成跟逐筆同一種 1 秒桶 columnar（前端不必寫兩套）",
    f"{len(pd_['s'])} 個桶")
chk("s 嚴格遞增", all(pd_["s"][i] > pd_["s"][i - 1] for i in range(1, len(pd_["s"]))), True)
say(all(pd_["h"][i] >= pd_["l"][i] for i in range(len(pd_["s"]))), "每個桶的高 ≥ 低")
say(all(pd_["h"][i] >= max(pd_["o"][i], pd_["c"][i]) for i in range(len(pd_["s"]))),
    "每個桶的高 ≥ max(開,收)")
chk("讀得懂完整 ISO 的 t（逐筆那邊是 HH:MM:SS.mmm）", len(pd_["first"] or ""), 12)
chk("壞列 0", pd_["bad"], 0)
chk("沒有報價的那幾列另外計數（⛔ 不可以安靜地少）", pd_["nopx"], 3)
chk("成交筆數＝取樣列數（扣掉沒有報價的）", pd_["n"], prows - 3)
say(pd_["ms_med"] and 200 < pd_["ms_med"] < 900, "中位取樣間隔算得出來",
    f"ms_med={pd_['ms_med']}")

# ── ⛔ 成交量：取樣檔沒有，一律 0 ＋ has_vol=False ───────────────────────
chk("has_vol=False（前端靠它把量柱做成 disabled）", pd_["has_vol"], False)
chk("每一個桶的量都是 0（⛔ 不准拿 vol_ratio 之類的東西湊假量柱）", sum(pd_["vq"]), 0)
# 負控組：如果哪天有人把量塞進來，這條要紅
_src_pol = inspect.getsource(LP._tick_load_polled)
VNEEDLE = "            B[sec] = [px, px, px, px, 0]"
say(VNEEDLE in _src_pol, "量柱負控組的目標字串還在原始碼裡（尺的自證）")
if VNEEDLE in _src_pol:
    ns = {}
    exec(compile(_src_pol.replace(VNEEDLE, "            B[sec] = [px, px, px, px, 1]"),
                 "<mutated>", "exec"), LP.__dict__, ns)
    _orig_pol = LP._tick_load_polled
    LP._tick_load_polled = ns["_tick_load_polled"]
    LP.TICK_CACHE.clear()
    pv = LP.tick_day(PDAY)
    say(sum(pv["vq"]) > 0, "負控組：有人偷塞量進來的話，這條會紅",
        f"總量 {sum(pv['vq'])}")
    LP._tick_load_polled = _orig_pol
    LP.TICK_CACHE.clear()
    chk("裝回去之後量又是 0", sum(LP.tick_day(PDAY)["vq"]), 0)

# ── 買賣價帶：取樣檔**有** bid/ask ⇒「折線＋價帶」那個圖種照樣能用 ────────
pband = sum(1 for i in range(len(pd_["s"]))
            if pd_["bl"][i] is not None and pd_["ah"][i] is not None)
say(pband >= len(pd_["s"]) * 0.9, "取樣日的價帶有資料（bid/ask 真的被讀進去）",
    f"{pband}/{len(pd_['s'])} 個桶")
say(all(pd_["ah"][i] >= pd_["bl"][i] for i in range(len(pd_["s"]))
        if pd_["bl"][i] is not None and pd_["ah"][i] is not None),
    "每個桶的最高賣價 ≥ 最低買價")
PBNEEDLE = '        bid, ask = _tick_num(o.get("bid")), _tick_num(o.get("ask"))\n'
say(PBNEEDLE in _src_pol, "價帶負控組的目標字串還在原始碼裡（尺的自證）")
if PBNEEDLE in _src_pol:
    ns = {}
    exec(compile(_src_pol.replace(PBNEEDLE, '        bid, ask = None, None\n'),
                 "<mutated>", "exec"), LP.__dict__, ns)
    _orig_pol = LP._tick_load_polled
    LP._tick_load_polled = ns["_tick_load_polled"]
    LP.TICK_CACHE.clear()
    pn = LP.tick_day(PDAY)
    say(sum(1 for i in range(len(pn["s"]))
            if pn["bl"][i] is not None or pn["ah"][i] is not None) == 0,
        "負控組：不讀 bid/ask 的話價帶整個不見")
    LP._tick_load_polled = _orig_pol
    LP.TICK_CACHE.clear()

# ── 種類判斷反過來必須紅（逐筆日不可以被標成取樣）────────────────────────
_src_days = inspect.getsource(LP.tick_days)
KNEEDLE2 = '            kind = "tick"\n'
say(KNEEDLE2 in _src_days, "種類負控組的目標字串還在原始碼裡（尺的自證）")
if KNEEDLE2 in _src_days:
    ns = {}
    exec(compile(_src_days.replace(KNEEDLE2, '            kind = "polled"\n'),
                 "<mutated>", "exec"), LP.__dict__, ns)
    _orig_days = LP.tick_days
    LP.tick_days = ns["tick_days"]
    LP.TICK_DIR = SB          # 這個資料夾裡是**逐筆**檔
    LP._TICK_SNIFFED.clear()
    LP._TICK_SAID.clear()
    with redirect_stdout(io.StringIO()):
        nd = LP.tick_days()
    say(all(x["kind"] == "tick" for x in nd["days"]) is False,
        "負控組：把種類判斷反過來之後，逐筆日被標成取樣（這條會紅）",
        f"kinds={sorted({x['kind'] for x in nd['days']})}")
    LP.tick_days = _orig_days
    LP._TICK_SNIFFED.clear()
    LP._TICK_SAID.clear()
    with redirect_stdout(io.StringIO()):
        nd2 = LP.tick_days()
    say(all(x["kind"] == "tick" for x in nd2["days"]),
        "裝回去之後逐筆日又標回逐筆（⛔ 逐筆日不可以被標成取樣）")

# ── 取樣檔的半列：跟逐筆走同一個 _tick_chunk()，所以同一道守衛蓋兩邊 ────────
LP.TICK_DIR = PL
LP.TICK_CACHE.clear()
pb = LP.tick_day(PDAY)
tick_synth.append_half_line(PL / f"{PDAY}-polled.jsonl")
ph = LP.tick_day(PDAY)
chk("取樣檔讀到半列時不算壞列（位移退回上一個換行處）", ph["bad"], pb["bad"])
chk("取樣檔讀到半列時不會生出新的桶", len(ph["s"]), len(pb["s"]))

# ══ ②b-2 取樣檔的壞列：看不懂的時間戳、字串價格（lab-qa 打不紅的 BM5 ＋ 退件）════
#
# BM5：把「時間看不懂 ⇒ 算 bad」改成「靜靜當成 08:45:00」，144/144 全綠 ——
#      那是**「安靜地少」的原型**，上一輪才在逐筆側（⑥b）補起來，
#      新長出來的取樣側原封不動又長了一次。
# 另一面：**一列 `price` 是字串 ⇒ 整天 500、1,091 列好資料全部看不到**（lab-qa 實測）。
#      「安靜地少」的反面是「大聲地全沒了」，同樣不該發生 —— 一列壞資料只准弄掉那一列。
print("\n=== ②b-2 取樣檔的壞列：時間戳看不懂／價格是字串 ===")
PB = TMP / "polbad"
PB.mkdir()
PBD = "2019-05-11"
BADTS_N, STRPX_N = 7, 5
pbrows = tick_synth.synth_polled_day(PB / f"{PBD}-polled.jsonl", PBD,
                                     first=32700, last=33200, every_ms=460, seed=5,
                                     badts=BADTS_N, strpx=STRPX_N)
LP.TICK_DIR = PB
LP.TICK_CACHE.clear()
LP._TICK_SNIFFED.clear()
LP._TICK_SAID.clear()
pbad = LP.tick_day(PBD)
chk("看不懂的時間戳 ＋ 字串價格整整都算進 bad", pbad["bad"], BADTS_N + STRPX_N)
chk("那幾列不算成交筆數", pbad["n"], pbrows - BADTS_N - STRPX_N)
say(0 not in pbad["s"],
    "⛔ 沒有任何桶落在第 0 秒（＝沒有人把看不懂的時間戳頂替成 08:45:00）",
    f"最小的桶 s={min(pbad['s'])}")
say(pbad["n"] > 100 and len(pbad["s"]) > 100,
    "⛔ 而且那一天**其餘的好資料照樣看得到**（不是整天不見）",
    f"{pbad['n']} 筆 / {len(pbad['s'])} 個桶")
# ⛔ 每一列都要有去處：好的進 n，其餘一定落在 bad / nopx / outwin 三個數字裡的一個。
chk("每一列都有去處（n + bad + nopx + outwin ＝ 總列數）",
    pbad["n"] + pbad["bad"] + pbad["nopx"] + pbad["outwin"], pbrows)

_src_pol2 = inspect.getsource(LP._tick_load_polled)
TSNEEDLE = '        ts = _tick_iso_hms(o.get("t"))\n        if ts is None:\n'
say(TSNEEDLE in _src_pol2, "BM5 負控組的目標字串還在原始碼裡（尺的自證）")
if TSNEEDLE in _src_pol2:
    ns = {}
    exec(compile(_src_pol2.replace(
        TSNEEDLE,
        '        ts = _tick_iso_hms(o.get("t")) or "08:45:00.000"\n        if ts is None:\n'),
        "<mutated>", "exec"), LP.__dict__, ns)
    _orig_pol = LP._tick_load_polled
    LP._tick_load_polled = ns["_tick_load_polled"]
    LP.TICK_CACHE.clear()
    pn = LP.tick_day(PBD)
    say(pn["bad"] < pbad["bad"] and 0 in pn["s"],
        "BM5 負控組：頂替成 08:45:00 之後 bad 少算、而且多出一個第 0 秒的假桶",
        f"bad {pbad['bad']} → {pn['bad']}、s 有沒有 0：{0 in pn['s']}")
    LP._tick_load_polled = _orig_pol
    LP.TICK_CACHE.clear()
    chk("裝回去之後 bad 又數得出來", LP.tick_day(PBD)["bad"], BADTS_N + STRPX_N)

# 「大聲地全沒了」的負控組：拿掉價格型別檢查 ⇒ 一列字串就讓整天炸掉
PXNEEDLE = ('        px = _tick_num(px)\n'
            '        if px is None:\n')
say(PXNEEDLE in _src_pol2, "字串價格負控組的目標字串還在原始碼裡（尺的自證）")
if PXNEEDLE in _src_pol2:
    ns = {}
    exec(compile(_src_pol2.replace(PXNEEDLE, '        if False:\n'),
                 "<mutated>", "exec"), LP.__dict__, ns)
    _orig_pol = LP._tick_load_polled
    LP._tick_load_polled = ns["_tick_load_polled"]
    LP.TICK_CACHE.clear()
    try:
        LP.tick_day(PBD)
        blew = ""
    except Exception as e:
        blew = type(e).__name__
    say(bool(blew),
        "負控組：拿掉價格型別檢查之後，一列字串 price 就讓**整天**炸掉（＝舊行為）",
        f"例外 {blew or '（沒炸，尺壞了）'}")
    LP._tick_load_polled = _orig_pol
    LP.TICK_CACHE.clear()
    say(LP.tick_day(PBD)["n"] > 100, "裝回去之後整天又讀得出來")

# ══ ②b-3 ms_med 是量出來的，不是寫死的（lab-qa 打不紅的 BM7）══════════════
# 「約 0.46 秒一筆」是**畫在他螢幕上的數字**（副標、金籤、auto 桶寬下限都靠它），
# 寫死 460 之後 144/144 全綠 ⇒ 那個數字等於沒有人在守。
print("\n=== ②b-3 ms_med 是量出來的 ===")
MB = TMP / "msmed"
MB.mkdir()
MS_CASES = [("2019-05-21", 250), ("2019-05-22", 900)]
for md, ev_ms in MS_CASES:
    tick_synth.synth_polled_day(MB / f"{md}-polled.jsonl", md, first=32700, last=33300,
                                every_ms=ev_ms, seed=31 + ev_ms)
LP.TICK_DIR = MB
LP.TICK_CACHE.clear()
LP._TICK_SNIFFED.clear()
LP._TICK_SAID.clear()
msgot = {}
for md, ev_ms in MS_CASES:
    msgot[md] = LP.tick_day(md)["ms_med"]
    say(abs(msgot[md] - ev_ms) <= ev_ms * 0.25,
        f"間隔 {ev_ms}ms 的取樣檔量出來的 ms_med 要接近 {ev_ms}",
        f"ms_med={msgot[md]}")
say(msgot[MS_CASES[0][0]] != msgot[MS_CASES[1][0]],
    "兩份密度不同的取樣檔，ms_med 必須不一樣（⛔ 不是寫死的常數）",
    f"{msgot}")
_src_day = inspect.getsource(LP.tick_day)
MNEEDLE = '               "ms_med": (msl[len(msl) // 2] if msl else None),\n'
say(MNEEDLE in _src_day, "BM7 負控組的目標字串還在原始碼裡（尺的自證）")
if MNEEDLE in _src_day:
    ns = {}
    exec(compile(_src_day.replace(MNEEDLE, '               "ms_med": 460,\n'),
                 "<mutated>", "exec"), LP.__dict__, ns)
    _orig_day = LP.tick_day
    LP.tick_day = ns["tick_day"]
    LP.TICK_CACHE.clear()
    mn = {md: LP.tick_day(md)["ms_med"] for md, _ in MS_CASES}
    say(len(set(mn.values())) == 1,
        "BM7 負控組：寫死之後兩份不同密度的檔量出同一個數字（這條會紅）", f"{mn}")
    LP.tick_day = _orig_day
    LP.TICK_CACHE.clear()

# ══ ②b-4 09:30 之後的取樣檔要切窗口，而且**要講出來**（lab-qa M5）═══════════
#
# `tick_recorder.py --until 13:45` 是它自己說明裡就有的用法 ⇒ 取樣檔可能一路錄到下午。
# 照收會打破「一天最多 2,700 個桶」那條不變式（前端固定時間軸與效能都靠它），
# 關掉「時間軸固定」之後整個早上被壓成一小段。
# ⛔ 切掉的部分**一定要有一個數字**（outwin）—— 安靜地少是這個專案明令禁止的。
print("\n=== ②b-4 09:30 之後的資料：切窗口 ＋ 講出來 ===")
WB = TMP / "until1345"
WB.mkdir()
WD = "2019-05-31"
wrows = tick_synth.synth_polled_day(WB / f"{WD}-polled.jsonl", WD,
                                    first=32700, last=49500,      # 09:05 ~ 13:45
                                    every_ms=2000, seed=99)
LP.TICK_DIR = WB
LP.TICK_CACHE.clear()
LP._TICK_SNIFFED.clear()
LP._TICK_SAID.clear()
wd_ = LP.tick_day(WD)
say(wrows > 5000, "治具真的錄到 13:45（尺的自證）", f"{wrows:,} 列")
le_ok = len(wd_["s"]) <= 2700
say(le_ok, "⛔ 不變式：一天最多 2,700 個桶", f"{len(wd_['s'])} 個桶")
say(max(wd_["s"]) < 2700 and min(wd_["s"]) >= 0,
    "每一個桶都落在 08:45~09:30 裡面", f"s 範圍 {min(wd_['s'])}~{max(wd_['s'])}")
say(wd_["outwin"] > 5000, "⛔ 被切掉的列有一個數字（outwin），不是安靜地少",
    f"outwin={wd_['outwin']:,}")
chk("每一列都有去處（n + bad + nopx + outwin ＝ 總列數）",
    wd_["n"] + wd_["bad"] + wd_["nopx"] + wd_["outwin"], wrows)
WNEEDLE = ('        sec = _tick_sec(ts)\n'
           '        if not _tick_inwin(sec):\n')
say(WNEEDLE in _src_pol2, "M5 負控組的目標字串還在原始碼裡（尺的自證）")
if WNEEDLE in _src_pol2:
    ns = {}
    exec(compile(_src_pol2.replace(WNEEDLE,
                                   '        sec = _tick_sec(ts)\n'
                                   '        if False:\n'),
                 "<mutated>", "exec"), LP.__dict__, ns)
    _orig_pol = LP._tick_load_polled
    LP._tick_load_polled = ns["_tick_load_polled"]
    LP.TICK_CACHE.clear()
    wn = LP.tick_day(WD)
    say(len(wn["s"]) > 2700 and wn["outwin"] == 0,
        "M5 負控組：不切窗口的話桶數爆掉、而且 outwin 是 0（看不出少了什麼）",
        f"{len(wn['s'])} 個桶、outwin={wn['outwin']}")
    LP._tick_load_polled = _orig_pol
    LP.TICK_CACHE.clear()
    say(len(LP.tick_day(WD)["s"]) <= 2700, "裝回去之後又守得住 2,700")
# 逐筆那條路也要切（不變式是**整頁**的性質，不是某一種檔的）
TW = TMP / "tickwin"
TW.mkdir()
TWD = "2019-06-01"
twlines = tick_synth.synth_lines(ticks=800, bidask=400, first=31502, last=49500, seed=12)
(TW / f"{TWD}.jsonl").write_bytes(("\r\n".join(twlines) + "\r\n").encode("utf-8"))
LP.TICK_DIR = TW
LP.TICK_CACHE.clear()
LP._TICK_SNIFFED.clear()
tw = LP.tick_day(TWD)
say(max(tw["s"]) < 2700 and tw["outwin"] > 0,
    "逐筆檔一路寫到 13:45 時同樣切窗口 ＋ 有數字（⛔ 不要只守取樣那一半）",
    f"最大的桶 s={max(tw['s'])}、outwin={tw['outwin']:,}")

# ══ ②c ⛔ tick_day() 按「驗證過的種類」挑，不是按檔案存不存在挑（退件 M1）══════
#
# lab-qa 實測：一個「逐筆檔名 ＋ 取樣內容」的檔被 tick_days() 的嗅探擋掉之後，
# tick_day() **照樣端得出來**（它只看 TICK_DIR/{d}.jsonl 在不在），而且把同一天真正的
# 取樣檔整份蓋掉 ⇒ 清單寫「取樣」、點進去圖上寫「逐筆」、成交量那顆變回可以按、
# 空狀態寫「這天只有檔頭，一筆成交都沒有錄到」——**一句假話**（那天 1,091 列）。
# ⛔ 真正的不變式：**兩把尺必須是同一把** —— tick_day(d).kind ＝ tick_days() 上那天的 kind。
print("\n=== ②c tick_day() 按驗證過的種類挑（兩把尺必須是同一把）===")
KB2 = TMP / "kindpick"
KB2.mkdir()
KD_TICK, KD_POLL, KD_BOTH, KD_FAKE = ("2019-07-01", "2019-07-02", "2019-07-03", "2019-07-04")
tick_synth.synth_day(KB2 / f"{KD_TICK}.jsonl", ticks=200, bidask=100,
                     first=31502, last=31900, seed=21)
tick_synth.synth_polled_day(KB2 / f"{KD_POLL}-polled.jsonl", KD_POLL,
                            first=32700, last=33400, seed=22)
tick_synth.synth_day(KB2 / f"{KD_BOTH}.jsonl", ticks=200, bidask=100,
                     first=31502, last=31900, seed=23)
tick_synth.synth_polled_day(KB2 / f"{KD_BOTH}-polled.jsonl", KD_BOTH,
                            first=32700, last=33400, seed=24)
# ⛔ M1 的形狀：假逐筆（逐筆檔名 ＋ 取樣內容）＋ 同一天真正的取樣檔
tick_synth.synth_polled(KB2 / f"{KD_FAKE}.jsonl", 400)
FAKE_ROWS = tick_synth.synth_polled_day(KB2 / f"{KD_FAKE}-polled.jsonl", KD_FAKE,
                                        first=32700, last=33400, seed=25)
LP.TICK_DIR = KB2
LP.TICK_CACHE.clear()
LP._TICK_SNIFFED.clear()
LP._TICK_SAID.clear()
with redirect_stdout(io.StringIO()):
    kdd = LP.tick_days()
klist = {x["d"]: x["kind"] for x in kdd["days"]}
chk("清單上的種類", klist, {KD_TICK: "tick", KD_POLL: "polled",
                            KD_BOTH: "tick", KD_FAKE: "polled"})
say(KD_FAKE in kdd["skipped"], "假逐筆檔照樣列進 skipped", f"skipped={kdd['skipped']}")
for kd in (KD_TICK, KD_POLL, KD_BOTH, KD_FAKE):
    LP.TICK_CACHE.clear()
    chk(f"{kd}：tick_day() 的 kind 要等於清單上的 kind",
        LP.tick_day(kd)["kind"], klist[kd])
LP.TICK_CACHE.clear()
kf = LP.tick_day(KD_FAKE)
chk("⛔ 假逐筆那天端出來的是**真取樣檔的每一列**（不是 0 筆的假話）", kf["n"], FAKE_ROWS)
chk("而且 has_vol=False（量柱要 disabled）", kf["has_vol"], False)
say(len(kf["s"]) > 100, "而且真的畫得出來", f"{len(kf['s'])} 個桶")

# 負控組 A（就是 lab-qa 那個突變）：把 tick_day() 的兩行對調 ⇒ 取樣優先
_src_day2 = inspect.getsource(LP.tick_day)
ORDER = ('        ent = _tick_load(d)\n'
         '        if ent is None:\n'
         '            ent = _tick_load_polled(d)\n')
SWAPPED = ('        ent = _tick_load_polled(d)\n'
           '        if ent is None:\n'
           '            ent = _tick_load(d)\n')
say(ORDER in _src_day2, "M1 負控組 A 的目標字串還在原始碼裡（尺的自證）")
if ORDER in _src_day2:
    ns = {}
    exec(compile(_src_day2.replace(ORDER, SWAPPED), "<mutated>", "exec"), LP.__dict__, ns)
    _orig_day = LP.tick_day
    LP.tick_day = ns["tick_day"]
    LP.TICK_CACHE.clear()
    say(LP.tick_day(KD_BOTH)["kind"] != klist[KD_BOTH],
        "M1 負控組 A：兩行對調（取樣優先）之後，兩種檔都有的那天跟清單對不起來",
        f"清單說 {klist[KD_BOTH]}、端點給 {LP.tick_day(KD_BOTH)['kind']}")
    LP.tick_day = _orig_day
    LP.TICK_CACHE.clear()
    chk("裝回去之後又對得上", LP.tick_day(KD_BOTH)["kind"], klist[KD_BOTH])

# 負控組 B：把 _tick_load() 的嗅探拿掉 ⇒ 回到「按檔案存不存在挑」（＝ 939dce2 的洞）
_src_load_a = inspect.getsource(LP._tick_load)
SNIFFPICK = '    if not path.exists() or not _tick_sniff(path, "tick")["ok"]:\n'
say(SNIFFPICK in _src_load_a, "M1 負控組 B 的目標字串還在原始碼裡（尺的自證）")
if SNIFFPICK in _src_load_a:
    ns = {}
    exec(compile(_src_load_a.replace(SNIFFPICK, '    if not path.exists():\n'),
                 "<mutated>", "exec"), LP.__dict__, ns)
    _orig_load = LP._tick_load
    LP._tick_load = ns["_tick_load"]
    LP.TICK_CACHE.clear()
    kfn = LP.tick_day(KD_FAKE)
    say(kfn["kind"] != klist[KD_FAKE] and kfn["n"] == 0,
        "M1 負控組 B：按檔案存不存在挑的話，假逐筆檔被端出來、真取樣的 1,000+ 列整份不見",
        f"kind={kfn['kind']}（清單說 {klist[KD_FAKE]}）、n={kfn['n']}（真取樣有 {FAKE_ROWS}）")
    LP._tick_load = _orig_load
    LP.TICK_CACHE.clear()
    chk("裝回去之後真取樣的每一列又回來了", LP.tick_day(KD_FAKE)["n"], FAKE_ROWS)

# ══ ②d 取樣檔的嗅探：種類要跟檔名一致（lab-qa 打不紅的 BM4）════════════════
# 「嗅探跟檔名一致」實作有寫、測試沒守：拿掉「不帶 k」＋「讀得懂的 t」兩個條件之後
# 144/144 全綠。這一節補上兩個要被擋掉的檔。
print("\n=== ②d 取樣檔的嗅探：不帶 k ＋ 讀得懂的 t ===")
SB2 = TMP / "sniff2"
SB2.mkdir()
SD_OK, SD_K, SD_T = "2019-08-01", "2019-08-02", "2019-08-03"
tick_synth.synth_polled_day(SB2 / f"{SD_OK}-polled.jsonl", SD_OK,
                            first=32700, last=33000, seed=41)
tick_synth.synth_polled_day(SB2 / f"{SD_K}-polled.jsonl", SD_K,
                            first=32700, last=33000, seed=42, with_k=True)
tick_synth.synth_polled_day(SB2 / f"{SD_T}-polled.jsonl", SD_T,
                            first=32700, last=33000, seed=43, all_bad_t=True)
LP.TICK_DIR = SB2
LP.TICK_CACHE.clear()
LP._TICK_SNIFFED.clear()
LP._TICK_SAID.clear()
buf = io.StringIO()
with redirect_stdout(buf):
    sdd = LP.tick_days()
sgot = [x["d"] for x in sdd["days"]]
say(SD_OK in sgot, "合格的取樣檔進得去（尺的自證：不是什麼都擋）")
say(SD_K not in sgot and SD_K in sdd["skipped"],
    "⛔ 取樣檔名 ＋ 每列都帶 k（逐筆內容）⇒ 擋掉並列進 skipped", f"days={sgot}")
say(SD_T not in sgot and SD_T in sdd["skipped"],
    "⛔ 取樣檔名 ＋ t 讀不懂 ⇒ 擋掉並列進 skipped")
say(buf.getvalue().count("跳過") >= 2, "兩個都印出了跳過的原因（⛔ 不可以靜靜跳過）")
_src_sniff = inspect.getsource(LP._tick_sniff)
BNEEDLE = ('            if "k" in o or o.get("price") is None '
           'or _tick_iso_hms(o.get("t")) is None:\n')
say(BNEEDLE in _src_sniff, "BM4 負控組的目標字串還在原始碼裡（尺的自證）")
if BNEEDLE in _src_sniff:
    ns = {}
    exec(compile(_src_sniff.replace(BNEEDLE,
                                    '            if o.get("price") is None:\n'),
                 "<mutated>", "exec"), LP.__dict__, ns)
    _orig_sniff = LP._tick_sniff
    LP._tick_sniff = ns["_tick_sniff"]
    LP._TICK_SNIFFED.clear()
    LP._TICK_SAID.clear()
    with redirect_stdout(io.StringIO()):
        sn = [x["d"] for x in LP.tick_days()["days"]]
    say(SD_K in sn and SD_T in sn,
        "BM4 負控組：拿掉「不帶 k」＋「讀得懂的 t」之後，兩個假取樣檔都混進來了",
        f"days={sn}")
    LP._tick_sniff = _orig_sniff
    LP._TICK_SNIFFED.clear()
    LP._TICK_SAID.clear()
    with redirect_stdout(io.StringIO()):
        sn2 = [x["d"] for x in LP.tick_days()["days"]]
    say(SD_K not in sn2 and SD_T not in sn2, "裝回去之後又擋得住", f"days={sn2}")

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
# 負控組：把「退回上一個換行處」那一段拿掉，半列必須變成壞列。
# ⚠️ 這道守衛住在 `_tick_chunk()`（2026-09-07 抽出來的共用續讀）——
#    **逐筆與取樣兩條路都走它**，所以這一個負控組同時蓋住兩邊。
src = inspect.getsource(LP._tick_chunk)
NEEDLE = '    cut = chunk.rfind(b"\\n")\n    chunk = b"" if cut < 0 else chunk[:cut + 1]\n'
say(NEEDLE in src, "負控組的目標字串還在原始碼裡（尺的自證）")
if NEEDLE in src:
    ns = {}
    exec(compile(src.replace(NEEDLE, ""), "<mutated>", "exec"), LP.__dict__, ns)
    orig = LP._tick_chunk
    LP._tick_chunk = ns["_tick_chunk"]
    LP.TICK_CACHE.clear()
    b2 = LP.tick_day(IDAY)
    tick_synth.append_half_line(IB / f"{IDAY}.jsonl")
    h2 = LP.tick_day(IDAY)
    say(h2["bad"] > b2["bad"], "負控組：拿掉半列處理之後，半列會被算成壞列",
        f"bad {b2['bad']} → {h2['bad']}")
    LP._tick_chunk = orig
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
# ⚠️ 快取有效性也住在共用的 `_tick_chunk()` ⇒ 這個負控組同樣同時蓋住逐筆與取樣兩條路。
_src_chunk = inspect.getsource(LP._tick_chunk)
SNEEDLE = 'ent["mtime"] == st.st_mtime and ent["size"] == st.st_size'
say(SNEEDLE in _src_chunk, "M5 負控組的目標字串還在原始碼裡（尺的自證）")
if SNEEDLE in _src_chunk:
    ns = {}
    exec(compile(_src_chunk.replace(SNEEDLE, 'ent["mtime"] == st.st_mtime'),
                 "<mutated>", "exec"), LP.__dict__, ns)
    _orig_load = LP._tick_chunk
    LP._tick_chunk = ns["_tick_chunk"]
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
    LP._tick_chunk = _orig_load
    LP.TICK_CACHE.clear()
    c5 = LP.tick_day(CD)
    say(c5["n"] > c4["n"], "裝回去之後又讀得到新增那段", f"{c4['n']} → {c5['n']} 筆")

# CM3：**檔案變小 ⇒ 位移歸零重讀整檔**（lab-qa 打不紅的第三個突變，舊有缺口）。
# 不該發生，但看門狗 ＋ 磁碟異常時會發生；硬接的話 `f.seek(off)` 直接落在檔尾之外、
# 讀回 b""，於是**畫面停在那個檔已經不存在的舊內容上**，而且 n 還是舊的數字。
SHB = TMP / "shrink"
SHB.mkdir()
SHD = "2019-02-11"
spath = SHB / f"{SHD}.jsonl"
tick_synth.synth_day(spath, ticks=400, bidask=200, first=31502, last=31900, seed=88)
LP.TICK_DIR = SHB
LP.TICK_CACHE.clear()
LP._TICK_SNIFFED.clear()
s1 = LP.tick_day(SHD)
big_off = LP.TICK_CACHE[SHD]["off"]
tick_synth.synth_day(spath, ticks=40, bidask=20, first=31502, last=31600, seed=88)
say(spath.stat().st_size < big_off, "造出「檔案比上次讀到的位移還小」的情況（尺的自證）",
    f"上次位移 {big_off} → 現在 {spath.stat().st_size} bytes")
s2 = LP.tick_day(SHD)
say(s2["n"] < s1["n"] and s2["n"] > 0,
    "檔案變小 ⇒ 位移歸零重讀整檔（拿到的是**現在**檔案裡的東西）",
    f"{s1['n']} → {s2['n']} 筆")
SHNEEDLE = '    if ent is not None and st.st_size < ent["off"]:\n        ent = None\n'
_src_chunk2 = inspect.getsource(LP._tick_chunk)
say(SHNEEDLE in _src_chunk2, "CM3 負控組的目標字串還在原始碼裡（尺的自證）")
if SHNEEDLE in _src_chunk2:
    ns = {}
    exec(compile(_src_chunk2.replace(SHNEEDLE, ""), "<mutated>", "exec"), LP.__dict__, ns)
    _orig_chunk = LP._tick_chunk
    LP._tick_chunk = ns["_tick_chunk"]
    LP.TICK_CACHE.clear()
    tick_synth.synth_day(spath, ticks=400, bidask=200, first=31502, last=31900, seed=88)
    s3 = LP.tick_day(SHD)
    tick_synth.synth_day(spath, ticks=40, bidask=20, first=31502, last=31600, seed=88)
    s4 = LP.tick_day(SHD)
    say(s4["n"] == s3["n"],
        "CM3 負控組：不處理「檔案變小」的話，畫面停在那個檔已經不存在的舊內容上",
        f"{s3['n']} → {s4['n']} 筆（應該要變少才對）")
    LP._tick_chunk = _orig_chunk
    LP.TICK_CACHE.clear()
    say(LP.tick_day(SHD)["n"] == s2["n"], "裝回去之後又拿到現在檔案裡的東西")

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
    # ⚠️ 2026-09-07 起窗口切分（_tick_inwin）會把 sec=0 那幾列攔進 outwin，
    #    所以現在的傷害形狀是「bad 歸零、那 5 列被改講成『時段外』」而不是落到負秒數。
    #    兩條都收：只要那 5 列不再被講成「讀不出來」，這個負控組就成立。
    say(xn["bad"] == 0 and (min(xn["s"]) < 0 or xn["outwin"] > xd["outwin"]),
        "M8 負控組：改成 sec=0 頂替之後，bad 歸零、那幾列被改講成別的東西（安靜地少）",
        f"bad={xn['bad']} 最小的桶 s={min(xn['s'])} outwin {xd['outwin']}→{xn['outwin']}")
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


for f in ("tick_days", "tick_day", "_tick_load", "_tick_load_polled", "_tick_chunk",
          "_tick_sniff", "_tick_iso_hms", "tick_trades", "_tick_complete"):
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

# ── ⑨-2b 只有取樣檔的日子，端點也要載得出來（這一輪的整條需求）────────────
EPDAY = "2019-04-02"
tick_synth.synth_polled_day(EP / f"{EPDAY}-polled.jsonl", EPDAY,
                            first=31510, last=31900, seed=61)
LP._TICK_SNIFFED.clear()
LP._TICK_SAID.clear()
code, body = hit("/api/tick/days")
kmap = {d["d"]: d["kind"] for d in body.get("days", [])}
chk("取樣檔那天也列在 /api/tick/days 裡", kmap.get(EPDAY), "polled")
chk("逐筆檔那天仍然標成 tick（⛔ 逐筆日不可以被標成取樣）", kmap.get(EDAY), "tick")
code, body = hit(f"/api/tick/day?date={EPDAY}")
chk(f"/api/tick/day?date={EPDAY}（取樣）的狀態碼", code, 200)
chk("端點回的 kind", body.get("kind"), "polled")
chk("端點回的 has_vol", body.get("has_vol"), False)
say(len(body.get("s") or []) > 0 and sum(body.get("vq") or []) == 0,
    "取樣日拿得到桶、而且量全部是 0", f"{len(body.get('s') or [])} 個桶")
say(sum(1 for x in (body.get("bl") or []) if x is not None) > 0,
    "取樣日的價帶有資料（bid/ask 真的被讀進去）")

# ── ⑨-2c 一列壞資料不可以讓整天回 500（lab-qa 實測：1,091 好列 ＋ 1 壞列 ⇒ ValueError
#    冒到 handler）。「安靜地少」的反面是「大聲地全沒了」，同樣不該發生。
EPBAD = "2019-04-03"
epbad_rows = tick_synth.synth_polled_day(EP / f"{EPBAD}-polled.jsonl", EPBAD,
                                         first=31510, last=31900, seed=62,
                                         strpx=1, badts=1)
LP._TICK_SNIFFED.clear()
LP.TICK_CACHE.clear()
code, body = hit(f"/api/tick/day?date={EPBAD}")
chk("一列 price 是字串 ＋ 一列時間戳壞掉 ⇒ 端點照樣 200（⛔ 不是整天 500）", code, 200)
chk("那兩列算進 bad", body.get("bad"), 2)
say(len(body.get("s") or []) > 50 and body.get("n") == epbad_rows - 2,
    "其餘每一列照樣看得到", f"{body.get('n')} 筆 / {len(body.get('s') or [])} 個桶")

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
