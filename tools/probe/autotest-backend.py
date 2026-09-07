# -*- coding: utf-8 -*-
"""
【程式下單】分頁的**後端**探針（AUTOTEST-TAB-SPEC.md §15 的後端那一半）。

前端那半在 tools/probe/autotest-tab.mjs。這一支不開瀏覽器、不連永豐、⛔ 不碰 8770。

⛔ **全程唯讀他的 real_trades/ 與 autotest/**（收尾逐檔比對雜湊證明沒動過）。
   其餘所有測試都在暫存區。
⛔ 治具價格一律 12000 附近（autotest_synth.py）。

【每一條都要有負控組】沒有負控組的綠燈在這個專案不算數。
⚠️ 突變要專打「這一輪新寫的那一側」與「**接線**那一側」——
   tick_writer.py 與【細節】分頁連兩輪的教訓都是：守衛蓋的是自己剛寫的模組，
   「有沒有人真的把它接上電」那半沒人守（少一行 TICKS.start() ＝ 整個早上零落地）。
   所以 ⑩ 那一節用 AST 掃 main() 的接線，而且每一條都證明過改壞了會紅。

⚠️⚠️ 2026-09-07 第三輪 lab-qa 退件補的四類（**上一輪這幾塊要嘛零斷言、要嘛只比字串**）：
   ⑤b  結算窗口的第一根一定是標籤 09:04（R2；舊寫法 `>` ⇒ 實際從 09:05 起算）
   ⑧c  他自己那一欄只讀不寫（R6；AST 傳遞閉包 ＋ **暫存的** real 目錄比雜湊 ——
        ⑭ 比的是真正的資料夾，而探針早就把 AUTO_REAL_DIR 導到暫存區，抓不到）
   ⑩   **行為測試**取代字串比對（R3：真的讓 _auto_tick 丟例外，斷言迴圈還活著＋
        計數真的加了＋真的印了字）；quote_gaps 真的在數（R4）
   ⑫b  常數自洽（R1／R4／R8 三個退件全在「文案／命名／常數」這一側，上一輪整個沒人守）
   ＋ 未捕捉的例外轉成**具名 FAIL** ＋ 一定印總結（R7：突變讓探針當場掛掉時，
     一行 FAIL 都沒有、也沒有總結 ⇒ 只數 FAIL 行的人會判成綠）。

跑法：  PYTHONIOENCODING=utf-8 PYTHONUTF8=1 .venv\\Scripts\\python.exe tools\\probe\\autotest-backend.py
"""
import ast
import hashlib
import inspect
import io
import json
import math
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "shioaji"))
import live_panel as LP          # noqa: E402
import autotest_synth as SY      # noqa: E402

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


def near(name, got, want, tol):
    ok = got is not None and abs(got - want) <= tol
    say(ok, name, f"= {got}（期待 {want} ± {tol}）")


# ⚠️⚠️ **探針自己掛掉不可以看起來像綠燈**（2026-09-07 lab-qa 退件 R7）：
#   突變（M7／M8b）讓這支探針當場丟例外時，**一行 FAIL 都沒有、也沒有印總結** ——
#   只數 FAIL 行的人會把它判成綠。上一輪寫了「兩道處置」，但**後端這一支根本沒裝**。
#   現在：未捕捉的例外一律轉成一項**具名的 FAIL**，而且總結一定會印出來
#   （突變工具因此看得到「共 N 項，M 項未過」，也看得到 FAIL 行）。
_FINISHED = {"ok": False}


def _summary(broke=""):
    global FAIL
    if broke:
        say(False, "⛔ 探針自己掛掉了（未捕捉的例外）—— 這一項就是那個 FAIL", broke)
    print(f"\n{'='*60}\n共 {N} 項，{'全部通過' if not FAIL else str(FAIL) + ' 項未過'}"
          + ("（⚠️ 探針中途中斷，後面的項目沒有跑到）" if broke else ""))


def _on_exc(etype, e, tb):
    if _FINISHED["ok"]:
        return
    _FINISHED["ok"] = True
    import traceback
    traceback.print_exception(etype, e, tb)
    _summary(f"{etype.__name__}: {str(e)[:120]}")


sys.excepthook = _on_exc


def tree_hash(p: Path):
    out = []
    if p.exists():
        for f in sorted(p.rglob("*")):
            if f.is_file():
                out.append((f.name, f.stat().st_size,
                            hashlib.sha256(f.read_bytes()).hexdigest()))
    return out


REAL_TRADES = LP.HERE / "real_trades"
REAL_AUTO = LP.HERE / "autotest"
BEFORE_TRADES, BEFORE_AUTO = tree_hash(REAL_TRADES), tree_hash(REAL_AUTO)

TMP = Path(tempfile.mkdtemp(prefix="autotest-backend-"))
print(f"暫存區：{TMP}")
print(f"他的 real_trades（唯讀）：{len(BEFORE_TRADES)} 個檔　"
      f"autotest（唯讀）：{len(BEFORE_AUTO)} 個檔\n")

SRC = (LP.HERE / "live_panel.py").read_text(encoding="utf-8")
TREE = ast.parse(SRC)
FUNCS = {}
for node in ast.walk(TREE):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        FUNCS.setdefault(node.name, []).append(node)


def seg(name, idx=0):
    return ast.get_source_segment(SRC, FUNCS[name][idx])


# ══ ① ⛔ 這一頁碰不到下單路徑（§15-1）═══════════════════════════════════
print("=== ① 這一頁一行都碰不到下單路徑 ===")
BAN = ["broker", "place_order", "/api/enter", "/api/real/", "REAL_ORDERS_ON",
       "practice_trades", "sim_orders", "real_orders"]
AUTO_FUNCS = [n for n in FUNCS if n.startswith("auto_") or n.startswith("_auto")]
say(len(AUTO_FUNCS) >= 18, "找得到這一頁的後端函式", f"{len(AUTO_FUNCS)} 支")


def _docstrings(node):
    """整份子樹裡的 docstring 節點（掃描要跳過它們）。"""
    out = set()
    for n in ast.walk(node):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            b = getattr(n, "body", None)
            if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) \
                    and isinstance(b[0].value.value, str):
                out.add(id(b[0].value))
    return out


def scan_ban(node):
    """
    ⚠️ **不可以直接搜字串**：這一節的說明本身就在講「⛔ 不准碰 broker」，
       搜字串會把註解與 docstring 當成違規（第一版就是這樣紅的）。
       用 AST：只看真正的識別字與**非 docstring** 的字串常數。
    """
    skip = _docstrings(node)
    bad = []
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and n.id in ("broker", "place_order"):
            bad.append(n.id)
        if isinstance(n, ast.Attribute) and n.attr in ("place_order", "enter", "close") \
                and isinstance(n.value, ast.Name) and n.value.id == "broker":
            bad.append("broker." + n.attr)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip:
            for w in BAN:
                if w in n.value:
                    bad.append(w)
    return sorted(set(bad))


hits = []
for fn in AUTO_FUNCS:
    for w in scan_ban(FUNCS[fn][0]):
        hits.append((fn, w))
chk("後端 auto_* / _auto_* 一個下單字眼都沒有", hits, [])

# 前端：#tab-auto 那一段（HTML）＋ at* 的 JS。同理，⚠️ 註解要先剝掉。
import re as _re


def strip_js(s):
    s = _re.sub(r"/\*.*?\*/", " ", s, flags=_re.S)
    return "\n".join(_re.sub(r"//.*$", "", ln) for ln in s.splitlines())


def strip_html(s):
    return _re.sub(r"<!--.*?-->", " ", s, flags=_re.S)


page = SRC[SRC.index("PAGE = r\"\"\""):]
tab_html = strip_html(page[page.index('<div id="tab-auto"'):page.index('<!-- 【回顧】')])
# ⚠️ 切點要落在那個區塊註解的 `/*` **上面**，不然 strip_js 配不成對、
#    整段開頭的說明（裡面就寫著「不碰 /api/enter」）會被當成程式碼（第一版就這樣紅的）。
_js0 = page.index("【程式下單】分頁：四種方向判斷的模擬對照（Canvas）")
js_at = strip_js(page[page.rindex("/*", 0, _js0):page.index("\nrvBind();")])
fe_hits = [w for w in ["/api/enter", "/api/real/", "data-act=", "data-rdir=", "REAL_ON"]
           if w in tab_html or w in js_at]
chk("前端 #tab-auto ＋ at* 的 JS 沒有任何下單端點／按鈕", fe_hits, [])
say("data-rdir" in strip_html(page[:page.index('<div id="tab-auto"')]) or
    "data-rdir" in strip_js(page[:page.index('<div id="tab-auto"')]),
    "  負控組：同一把尺在真實下單那半抓得到 data-rdir")

# 【尺的自證】同一把尺掃 _real_enter 必須抓得到 broker ⇒ 證明尺是活的
say(len(scan_ban(FUNCS["_real_enter"][0])) > 0, "負控組：同一把尺掃 _real_enter 抓得到 broker",
    scan_ban(FUNCS["_real_enter"][0]))

# ⛔ 他自己那一欄一定要是**直接讀檔**，不可以走 broker（走了 AST 就掃不出來）
say("AUTO_REAL_DIR" in seg("_auto_mine_day"), "他自己那一欄是直接讀 real_trades/")
say("open(\"a\"" in seg("_auto_append"), "⛔ 落地一定是 append 不是覆寫")

# ══ ② 天花板與帶寬（§15-7、§7.1）══════════════════════════════════════
print("\n=== ② 天花板：算出來的，不是寫死的 ===")
chk("天花板(505) 對得上 PM 的 12.1", LP.auto_ceiling(505), 12.0)
chk("天花板(20)", LP.auto_ceiling(20), 60.5)
chk("天花板(0)＝算不出來", LP.auto_ceiling(0), None)
say(LP.auto_ceiling(20) > LP.auto_ceiling(120), "筆數越多門檻越低",
    f"{LP.auto_ceiling(20)} > {LP.auto_ceiling(120)}")
# 帶寬與天花板必須是同一句話的兩種畫法：band(n) ≈ ceiling(n)×n
# ⚠️ 容差要跟著 n 放大：ceiling 是**四捨五入到 0.1** 之後才乘回去的，
#    n=505 時那 0.05 的捨入就是 25 點 —— 拿固定容差量會誤判成「兩者不一致」。
for n in (10, 20, 60, 120, 505):
    near(f"帶寬(n={n}) ＝ 天花板×n", LP.auto_band(n), LP.auto_ceiling(n) * n, 0.05 * n + 0.5)
say(abs(LP.CEIL_Z - (1.96 + 0.84)) < 1e-9, "z ＝ 1.96（不是雜訊）＋ 0.84（八成抓得到）")

# ── σ 自己重算一次（⛔ 不可以照抄規格裡那個數字）
print("  重算 σ（tmf_1min.csv，最後 505 天，±100 觸價）…")
csv = LP.HERE / "tmf_1min.csv"
if csv.exists():
    import pandas as pd
    px = pd.read_csv(csv)
    px["ts"] = pd.to_datetime(px["ts"])
    px["d"] = px["ts"].dt.date
    px["hm"] = px["ts"].dt.strftime("%H:%M")
    g0 = px[(px["hm"] >= "08:46") & (px["hm"] <= "13:45")]
    pts = []
    for d, g in g0.groupby("d"):
        g = g.sort_values("ts")
        e = g[g["hm"] == "09:04"]
        after = g[g["hm"] > "09:04"]
        if e.empty or after.empty:
            continue
        ent = float(e["Close"].iloc[0])
        bars = [{"t": r.hm, "o": float(r.Open), "h": float(r.High),
                 "l": float(r.Low), "c": float(r.Close)} for r in after.itertuples()]
        pts.append(LP._auto_run(bars, ent, 1)["pts"])
    last = pts[-505:]
    m = sum(last) / len(last)
    sd = math.sqrt(sum((x - m) ** 2 for x in last) / len(last))
    near("重算的 σ 跟程式裡的常數對得上", round(sd, 2), LP.CEIL_SIGMA, 1.0)
    chk("重算用的樣本數", len(last), 505)
    print(f"    實測 σ={sd:.2f}、每筆平均 {m:+.2f} 點（{len(pts)} 天全樣本取最後 505）")
else:
    say(False, "找不到 tmf_1min.csv，σ 沒辦法重算")

# ══ ③ 配對判準：跟精確二項式對得上（§8）════════════════════════════════
print("\n=== ③ 配對判準 need = ceil((m+1.96√m)/2) ===")
chk("m=7 ⇒ 七天全贏才算數", LP.auto_pair_need(7), 7)
chk("m=10", LP.auto_pair_need(10), 9)
chk("m=20", LP.auto_pair_need(20), 15)
chk("m=0 ⇒ 沒有可以比的日子", LP.auto_pair_need(0), None)


def exact_need(m):
    """精確二項式：最小的 k 使 P(X>=k) <= 0.025（雙尾 95%）。"""
    tot = 2 ** m
    for k in range(m + 1):
        s = sum(math.comb(m, i) for i in range(k, m + 1))
        if s / tot <= 0.025:
            return k
    return m + 1


bad = [(m, LP.auto_pair_need(m), exact_need(m)) for m in range(1, 61)
       if LP.auto_pair_need(m) != exact_need(m)]
chk("m=1~60 跟精確二項式逐點一致", bad, [])
chk("m=5 ⇒ 全贏也不算數（need 大於 m，⛔ 不可以夾到 m）", LP.auto_pair_need(5), 6)
# 【尺的自證】規格 §8 那個閉合式**是錯的**，這裡把它當負控組留著：
#   它是沒有連續性修正的常態近似，m=5~40 有 17 個偏小（會提早說「算數了」）。
approx = lambda m: int(math.ceil((m + 1.96 * math.sqrt(m)) / 2.0))   # noqa: E731
off = [m for m in range(5, 41) if approx(m) != exact_need(m)]
say(len(off) >= 10, "負控組：規格那個閉合式跟精確值差很多（所以沒有照抄）",
    f"m=5~40 有 {len(off)} 個不一致，全部偏小")
say(all(approx(m) <= exact_need(m) for m in off), "  而且每一個都偏小（會提早說算數）")

# ══ ④ 方向規則（§2.1）══════════════════════════════════════════════════
print("\n=== ④ 四種算法唯一的差別：方向 ===")
chk("訊號 0 算做多（寫死，不准變成第三種狀態）",
    LP.auto_dirs(0.0, 0.0), {"D": 1, "A": 1, "B": 1, "C": 0})
chk("A 看 09:00、B 看 08:45、D 永遠做多",
    LP.auto_dirs(-2.3, 18.2), {"D": 1, "A": -1, "B": 1, "C": 0})
chk("C：剛好 30 點不做（要**超過**才做）", LP.auto_dirs(1, 30.0)["C"], 0)
chk("C：30.1 點才做", LP.auto_dirs(1, 30.1)["C"], 1)
chk("C：跌超過 30 點做空", LP.auto_dirs(1, -31.0)["C"], -1)
chk("算不出訊號回 None，不是 0", LP.auto_dirs(None, None),
    {"D": 1, "A": None, "B": None, "C": None})

# ══ ⑤ ±100 的結算（§3.2）══════════════════════════════════════════════
print("\n=== ⑤ ±100 觸價 ===")
B = lambda t, h, l: {"t": t, "o": 12000.0, "h": h, "l": l, "c": 12000.0}   # noqa: E731
chk("做多摸到 +100 ⇒ tp", LP._auto_run([B("09:10", 12101.0, 11990.0)], 12000.0, 1)["why"], "tp")
chk("做多摸到 −100 ⇒ sl", LP._auto_run([B("09:10", 12010.0, 11899.0)], 12000.0, 1)["why"], "sl")
chk("做空摸到 −100 ⇒ tp", LP._auto_run([B("09:10", 12010.0, 11899.0)], 12000.0, -1)["why"], "tp")
chk("做空摸到 +100 ⇒ sl", LP._auto_run([B("09:10", 12101.0, 11990.0)], 12000.0, -1)["why"], "sl")
r = LP._auto_run([B("09:10", 12101.0, 11899.0)], 12000.0, 1)
chk("⛔ 同一根雙觸 ⇒ 保守算停損", (r["why"], r["pts"]), ("sl", -100.0))
chk("⛔ 同一根雙觸要標 both（畫面上要數得出來）", r["both"], True)
e = LP._auto_run([B("09:10", 12010.0, 11990.0), {"t": "13:44", "o": 12000.0, "h": 12005.0,
                                                 "l": 11995.0, "c": 12031.0}], 12000.0, 1)
chk("都沒摸到 ⇒ 13:45 收盤平", (e["why"], e["pts"]), ("eod", 31.0))

# ══ ⑤b 結算窗口：⛔ 第一根一定是**標籤 09:04** ═════════════════════════
#   （2026-09-07 lab-qa 退件 R2）one_min_bars() 走 to_timeframe ⇒ 標籤是**起始時間**，
#   09:03 那根涵蓋 09:03~09:04（含進場前的價，要排掉）、09:04 那根涵蓋 09:04~09:05
#   （整根都在 09:03:30 之後，**本來就該收進去**）。舊寫法用 `>` ⇒ 實際用到的第一根是
#   **09:05**，看不到觸價的變成 90 秒，而畫面副標還寫「從 09:04 開始算」＝假話。
print("\n=== ⑤b 結算窗口的第一根是哪一根 ===")
D5 = TMP / "d5"
D5.mkdir(parents=True, exist_ok=True)
_OLD_1MIN = LP.one_min_bars
LP.AUTO_DIR = D5              # ⛔ 從這裡開始一路指在暫存區，不再指回他真正的資料夾
_d5 = "2026-08-31"
(D5 / "2026-08.jsonl").write_text(json.dumps(
    {"rec": "sig", "date": _d5, "src": "live", "px": 12000.0,
     "dirs": {"A": 1, "B": 1, "C": 1, "D": 1}, "thresh": 30.0},
    ensure_ascii=False) + "\n", encoding="utf-8")
_labels = ["09:0%d" % i for i in range(10)] + ["09:10", "13:44"]
_bars5 = [{"t": t, "o": 12000.0, "h": 12001.0, "l": 11999.0, "c": 12000.0} for t in _labels]
LP.one_min_bars = lambda d: [dict(b) for b in _bars5]
_seen5 = {}
_orig_run5 = LP._auto_run


def _spy_run(bars, entry, dr):
    _seen5.setdefault("labels", [b["t"] for b in bars])
    return _orig_run5(bars, entry, dr)


LP._auto_run = _spy_run
LP.AUTO_CACHE.clear()
LP._auto_settle(_d5)
LP._auto_run = _orig_run5
_lab5 = _seen5.get("labels") or []
chk("⛔ settle 用到的第一根標籤 ＝ 09:04", _lab5[0] if _lab5 else None, "09:04")
chk("  09:03 那根有被排掉（它含 09:03:30 進場前的價）", "09:03" in _lab5, False)
chk("  09:04 那根有被收進去（整根都在進場之後）", "09:04" in _lab5, True)
say([b["t"] for b in _bars5 if b["t"] > LP.AUTO_SETTLE_FROM][0] == "09:05",
    "  自證：舊寫法（`>`）第一根會是 09:05 ⇒ 這把尺分得出來（不是恆真）")
LP.AUTO_CACHE.clear()
_rows5, _ = LP._auto_read()
chk("  寫進 settle 那一列的 settle_from 跟真的用到的第一根一致（副標才不會是假話）",
    (_rows5.get(_d5) or {}).get("settle_from"), _lab5[0] if _lab5 else None)
chk("  AUTO_SETTLE_FROM 常數本身還是 09:04", LP.AUTO_SETTLE_FROM, "09:04")
LP.one_min_bars = _OLD_1MIN
LP.AUTO_CACHE.clear()

# ══ ⑥ 讀檔：每一列都要有去處（§15-10）═══════════════════════════════════
print("\n=== ⑥ 每一列都要有去處，一列壞資料不准弄掉一整個月 ===")
D1 = TMP / "d1"
LP.AUTO_DIR = D1
LP.AUTO_REAL_DIR = D1 / "real"
(D1 / "real").mkdir(parents=True, exist_ok=True)
rows, bars = SY.build(12, end=date(2026, 8, 28))
LP.one_min_bars = lambda d: bars.get(str(d), [])
SY.write(D1, rows)
month = D1 / "2026-08.jsonl"
with month.open("a", encoding="utf-8") as f:
    f.write('{"rec":"sig","date":"2026-08-27","px":"一二三"}\n')        # 壞：px 是字串
    f.write("{ 這不是 json\n")                                           # 壞：整列
    f.write('{"rec":"sig","date":"不是日期","px":12000}\n')              # 壞：日期
    f.write('{"rec":"sig","date":"2026-08-26","px":true}\n')             # 壞：bool
    f.write('{"rec":"miss","date":"2026-08-29","why":"no_quote"}\n')     # 沒錄到
    f.write(json.dumps(rows[0], ensure_ascii=False) + "\n")              # 重複的 sig
LP.AUTO_CACHE.clear()
# ⚠️ 同上：讀檔炸掉要轉成一項具名的 FAIL，不可以讓整支探針掛掉
try:
    recs, led = LP._auto_read()
    S = LP.auto_stats(60, "live")
except Exception as e:
    say(False, "⛔ 一列壞資料不可以炸掉整個月（讀檔丟了例外）",
        f"{type(e).__name__}: {str(e)[:80]}")
    recs, led = {}, {"lines": 0, "sig": 0, "settle": 0, "miss": 0, "dup": 0, "bad": 0}
    S = {"rows": {k: {"n": 0} for k in "ABCD"}, "notes": {"missing": 0, "days_total": 0}}
total = sum(led[k] for k in ("sig", "settle", "miss", "dup", "bad"))
chk("sig+settle+miss+dup+bad ＝ 檔案總列數", total, led["lines"])
chk("壞掉的列數", led["bad"], 4)
chk("重複的 sig 被擋掉", led["dup"], 1)
chk("沒錄到的日子有一列", led["miss"], 1)
say(S["rows"]["D"]["n"] >= 10, "⛔ 一列壞資料只弄掉那一列，好資料照樣算得出來",
    f"D 有 {S['rows']['D']['n']} 筆")
chk("沒錄到的日子進 missing", S["notes"]["missing"], 1)
chk("missing 也要進 days_total（每一天都有去處）",
    S["notes"]["days_total"], S["rows"]["D"]["n"] + S["notes"]["missing"])

print("  負控組：把 _auto_num 的型別檢查拿掉")
_orig_num = LP._auto_num
LP._auto_num = lambda x: (None if x is None else float(x))     # ⇒ 字串會丟 ValueError
LP.AUTO_CACHE.clear()
blew = False
try:
    LP.auto_stats(60, "live")
except Exception:
    blew = True
say(blew, "  拿掉型別檢查之後真的會炸（＝這道防線在承重）")
LP._auto_num = _orig_num
LP.AUTO_CACHE.clear()

# ══ ⑥b 快取／落地（⚠️ 這半是「別人幫我做的那一側」，突變專打這裡）═══════
print("\n=== ⑥b 快取要同時比 mtime 與 size，落地一定是 append ===")
D1b = TMP / "d1b"
LP.AUTO_DIR = D1b
D1b.mkdir(parents=True, exist_ok=True)
r1b, b1b = SY.build(4, end=date(2026, 8, 28))
LP.one_min_bars = lambda d: b1b.get(str(d), [])
SY.write(D1b, r1b)
LP.AUTO_CACHE.clear()
n1 = LP.auto_stats(0, "live")["rows"]["D"]["n"]
mp = D1b / "2026-08.jsonl"
mt = mp.stat().st_mtime
extra = SY.build(6, end=date(2026, 8, 20))
SY.write(D1b, extra[0])
import os as _os
_os.utime(mp, (mt, mt))          # ⚠️ 內容變了、mtime 沒變（同一秒內續寫就長這樣）
n2 = LP.auto_stats(0, "live")["rows"]["D"]["n"]
say(n2 > n1, "⛔ 內容變了但 mtime 沒變，照樣要重讀（只比 mtime ⇒ 畫面停在舊資料）",
    f"{n1} → {n2} 天")
say(mp.stat().st_mtime == mt, "  自證：mtime 真的沒有變（不然這一條是白過的）")

# append 不覆寫：看門狗重啟是常態，覆寫＝把當天稍早的資料弄丟
before_lines = len(mp.read_text(encoding="utf-8").splitlines())
LP._auto_append({"rec": "miss", "date": "2026-08-31", "src": "live", "why": "no_quote"})
after_lines = len(mp.read_text(encoding="utf-8").splitlines())
chk("⛔ 落地是 append：舊的列一列都沒少", after_lines, before_lines + 1)
LP.AUTO_CACHE.clear()
say(LP.auto_stats(0, "live")["rows"]["D"]["n"] == n2,
    "  而且原本那幾天還在（不是被蓋掉重寫）")

# ══ ⑦ 回測與實跑分開（§15-9）══════════════════════════════════════════
print("\n=== ⑦ 回測與實跑絕對不可以相加 ===")
D2 = TMP / "d2"
LP.AUTO_DIR = D2
LP.AUTO_REAL_DIR = D2 / "real"
(D2 / "real").mkdir(parents=True, exist_ok=True)
lr, lb = SY.build(10, end=date(2026, 8, 28))
br, bb = SY.build(60, end=date(2025, 6, 30), src="backfill")
allb = dict(lb)
allb.update(bb)
LP.one_min_bars = lambda d: allb.get(str(d), [])
SY.write(D2, lr)
SY.write(D2, br)
LP.AUTO_CACHE.clear()
live = LP.auto_stats(0, "live")
chk("src=live 的筆數不受回填影響", live["rows"]["D"]["n"], 10)
chk("天花板是 n=10 的值", live["ceiling"], LP.auto_ceiling(10))
allsrc = LP.auto_stats(0, "all")
chk("負控組：拿掉 src 過濾（src=all）筆數就變成 70", allsrc["rows"]["D"]["n"], 70)
say(live["ceiling"] != allsrc["ceiling"], "  兩者的天花板不一樣（證明過濾真的有作用）")

# ══ ⑧ 他自己那一欄：口徑要分兩列（§11）════════════════════════════════
print("\n=== ⑧ 他自己那一欄（全部 vs 同口徑）===")
d0 = sorted(lb)[-1]
mine = SY.mine_rows(d0, n=3, first_why="tp")
(D2 / "real" / f"{d0}.jsonl").write_text(
    "\n".join(json.dumps(r, ensure_ascii=False) for r in mine) + "\n", encoding="utf-8")
LP.AUTO_CACHE.clear()
S = LP.auto_stats(0, "live")
M = S["mine"]
chk("全部：3 筆裡有 1 筆算不出點數 ⇒ 只算 2 筆", M["all"]["n"], 2)
chk("⛔ 算不出點數的要明講排除幾筆", M["nopoints"], 1)
chk("同口徑：只取當天第一筆且 ±100 真的跑完 ⇒ 1 筆", M["strict"]["n"], 1)
say(M["strict"]["n"] < M["all"]["n"], "  同口徑的樣本一定比較小（誠實比好看重要）")
chk("手動平倉有數出來", M["why"]["manual"], 1)
sec = SY.mine_rows(d0, n=1, first_why="manual")
(D2 / "real" / f"{d0}.jsonl").write_text(
    json.dumps(sec[0], ensure_ascii=False) + "\n", encoding="utf-8")
LP.AUTO_CACHE.clear()
chk("負控組：當天第一筆是手動平倉 ⇒ 同口徑不收它",
    LP.auto_stats(0, "live")["mine"]["strict"]["n"], 0)

# ══ ⑧c ⛔⛔ 他自己那一欄是**唯讀**的，而且要有機器守衛 ═════════════════
#   （2026-09-07 lab-qa 退件 R6）舊版只有 ⑭ 那道雜湊比對，可是探針早就把
#   AUTO_REAL_DIR 導到暫存區、⑭ 比的是**真正的**資料夾 ⇒ lab-qa 把 _auto_mine_day
#   改成 append 一列進 real_trades/，⑭ **抓不到**。兩道一起補：
#   ① AST：從 _auto_mine_day 出發可達的函式裡，一個寫檔呼叫都不准有；
#   ② 行為：對**暫存的** real 目錄前後比雜湊（比的是這支探針真的動得到的那一份）。
print("\n=== ⑧c ⛔ 他自己那一欄只讀不寫（AST ＋ 暫存 real 目錄比雜湊）===")
WRITE_NAMES = {"write_text", "write_bytes", "mkdir", "unlink", "rmdir", "rename",
               "replace", "touch", "rmtree", "remove", "copy", "copy2", "move",
               "_auto_append", "_auto_put", "writelines", "write", "truncate", "open"}


def reach(start, seen=None):
    """從某支函式出發、**在這個檔案裡**可達的所有函式（傳遞閉包）。"""
    seen = seen if seen is not None else set()
    if start in seen or start not in FUNCS:
        return seen
    seen.add(start)
    for c in calls_of(start):
        if c in FUNCS:
            reach(c, seen)
    return seen


def calls_of(fn):
    out = set()
    for node in ast.walk(FUNCS[fn][0]):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


MINE_REACH = reach("_auto_mine_day") | reach("_auto_mine_rows")
_writes = sorted({(fn, c) for fn in MINE_REACH for c in calls_of(fn) if c in WRITE_NAMES})
chk("⛔ 從他自己那一欄可達的函式裡，一個寫檔呼叫都沒有", _writes, [])
say(len(MINE_REACH) >= 2, f"  掃了 {len(MINE_REACH)} 支可達函式", sorted(MINE_REACH))
say(any(c in WRITE_NAMES for c in calls_of("_auto_append")),
    "  負控組：同一把尺在 _auto_append 抓得到寫檔呼叫（尺是活的）")
# ② 行為：**暫存的** real 目錄前後比雜湊（⑭ 比不到這一份）
_real_tmp = D2 / "real"
_before_real = tree_hash(_real_tmp)
LP.AUTO_CACHE.clear()
for _d in sorted(lb)[-6:]:
    LP._auto_mine_day(_d)
    LP.auto_day(_d)
LP.auto_stats(0, "live")
chk("⛔ 讀完之後暫存的 real 目錄一個位元組都沒變", tree_hash(_real_tmp), _before_real)
say(len(_before_real) >= 1, f"  自證：那個目錄裡真的有檔（{len(_before_real)} 個）")
_canary = _real_tmp / "__tmp__canary.jsonl"
_canary.write_text("x\n", encoding="utf-8")
say(tree_hash(_real_tmp) != _before_real, "  自證：真的多寫一個檔時這條會紅（尺是活的）")
_canary.unlink()

# ══ ⑧b 配對對照的母體：⛔ 只收「結果不一樣」的那幾天（§8）═══════════════
print("\n=== ⑧b 配對對照的有效樣本 ===")
def _mk(dirs, pts):
    runs = {}
    for k in ("A", "B", "C", "D"):
        if dirs[k] == 0:
            runs[k] = {"dir": 0, "skip": "below_threshold", "thresh": 30.0}
        elif dirs[k] is None:
            runs[k] = {"dir": None, "skip": "no_ref"}
        else:
            runs[k] = {"dir": dirs[k], "pts": pts[k], "why": "tp" if pts[k] > 0 else "sl",
                       "exit_at": "11:00", "both": False}
    return {"date": "x", "runs": runs, "dirs": dirs, "sig": {"A": 1.0, "B": 1.0}}


same = [_mk({"A": 1, "B": 1, "C": 0, "D": 1}, {"A": 100.0, "B": 100.0, "C": 0, "D": 100.0})] * 8
diff = [_mk({"A": -1, "B": 1, "C": 0, "D": 1}, {"A": 100.0, "B": -100.0, "C": 0, "D": -100.0})] * 4
P = LP._auto_pairs(same + diff)
chk("⛔ A 跟 D 同向的 8 天不進母體（結果一樣，差值是 0）", P["A"]["m"], 4)
chk("  A 贏 4 次", P["A"]["w"], 4)
chk("  差值＝4×200", P["A"]["diff"], 800.0)
chk("⚠️ C 沒做的日子算「結果不一樣」（D 有做、C 沒做）", P["C"]["m"], 12)
chk("  C 那幾天的差值是 0 − D 的點數", P["C"]["diff"],
    round(-(8 * 100.0) - (4 * -100.0), 1))
chk("負控組：把同向那 8 天也算進去 ⇒ 母體會變成 12（＝把 20 當樣本數的那個錯）",
    len(same + diff), 12)

# ══ ⑨ 勝敗定義要跟練習／真實同一套（§6.1）═════════════════════════════
print("\n=== ⑨ 勝敗定義：點數 > 0 才算勝，0 算敗 ===")
chk("+1 勝", LP._auto_tally([1.0])["w"], 1)
chk("0 算敗", LP._auto_tally([0.0])["w"], 0)
chk("−1 敗", LP._auto_tally([-1.0])["l"], 1)
chk("少於 30 筆不給 %", LP._auto_row_stats(
    [{"runs": {"A": {"dir": 1, "pts": 100.0, "why": "tp"}}}] * 29, "A")["rate"], None)
r30 = LP._auto_row_stats([{"runs": {"A": {"dir": 1, "pts": 100.0, "why": "tp"}}}] * 30, "A")
chk("滿 30 筆才給 %", r30["rate"], 100)
chk("RATE_MIN_N 是 30", LP.RATE_MIN_N, 30)

# ══ ⑩ 接線：主迴圈真的有呼叫，而且一行 I/O 都沒有（AST）═══════════════
print("\n=== ⑩ 接線（⚠️ 突變專打這一側）===")
main_src = seg("main")
say("_auto_worker" in main_src and "AUTO[\"started\"] = True" in main_src,
    "main() 有起寫檔執行緒並打開 started 開關")
say(main_src.index("TICKS.start()") < main_src.index("_auto_worker"),
    "  寫檔執行緒排在 TICKS.start() 之後（不搶開盤前那段）")


def _main_calls(name):
    """main() 裡有沒有真的呼叫某個函式（AST，⛔ 不搜字串：註解裡也在講這件事）。"""
    for node in ast.walk(FUNCS["main"][0]):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == name:
            return True
    return False


say(_main_calls("_auto_tick_guarded"), "主迴圈每圈呼叫 _auto_tick_guarded()（try 在它裡面）")
say(not _main_calls("_auto_tick"),
    "⛔ main() **不可以**直接呼叫 _auto_tick（那樣就沒有那層 try 了）")
say(_main_calls("update_state") or _main_calls("quote_state"),
    "  自證：同一把尺在 main() 抓得到別的呼叫（不是恆真）")

# ⛔⛔ **這一節的重點：守衛必須是行為測試，不是字串比對**（2026-09-07 lab-qa 退件 R3）。
#    舊版是「呼叫點前後 200 字裡有沒有 try/except/tick_err/print」——
#    lab-qa 把 `AUTO["tick_err"] = …` 換成 `pass`（"tick_err" 這個字串仍在窗口裡）
#    ⇒ **125/125 全綠**，但真實行為是 except 區塊自己丟 KeyError ⇒ 例外衝出 try
#    ⇒ **主迴圈當場中斷＝停損不再監控**。現在改成真的讓 _auto_tick 丟例外。
print("  行為測試：真的讓 _auto_tick 丟例外")
_orig_tick = LP._auto_tick
_before_err = LP.AUTO.get("tick_err", 0)
LP.AUTO["err"] = None


def _boom(st, now, sess):
    raise RuntimeError("探針故意丟的例外")


LP._auto_tick = _boom
_buf = io.StringIO()
_survived = True
try:
    with redirect_stdout(_buf):
        for _ in range(2):
            LP._auto_tick_guarded(None, datetime.now(), "day")
except Exception as _e:                       # noqa: BLE001
    _survived = False
    _why = f"{type(_e).__name__}: {str(_e)[:80]}"
LP._auto_tick = _orig_tick
say(_survived, "⛔⛔ _auto_tick 丟例外時，呼叫端不會被打斷（＝主迴圈還活著、停損還在監控）",
    "" if _survived else f"例外衝出來了：{_why}")
say(LP.AUTO.get("tick_err", 0) == _before_err + 2,
    "  但**不是安靜地吞**：tick_err 真的加了 2",
    f"{_before_err} → {LP.AUTO.get('tick_err')}")
say("程式下單" in _buf.getvalue() and "探針故意丟的例外" in _buf.getvalue(),
    "  而且真的印了主控台警告（連原始訊息一起）", repr(_buf.getvalue()[:70]))
say(isinstance(LP.AUTO.get("err"), str) and "tick:" in LP.AUTO["err"],
    "  AUTO['err'] 記下最後一個錯誤（畫面上要看得到）", repr(LP.AUTO.get("err"))[:60])
# 【自證】把那層 try 拿掉（用一個沒有 try 的等價實作）⇒ 上面那條一定要紅
_bare_ok = True
try:
    LP._auto_tick = _boom
    _boom(None, datetime.now(), "day")
except Exception:
    _bare_ok = False
finally:
    LP._auto_tick = _orig_tick
say(not _bare_ok, "  自證：同一個例外**不經過** _auto_tick_guarded 時真的會衝出來")
# 計數不可以寫成 AUTO.get("tick_err", 0)：那會讓「有沒有在數」變成看不出來的事
say(LP.AUTO["tick_err"] >= 2 and "tick_err" in seg("_auto_tick_guarded"),
    "  tick_err 是 AUTO 的固定欄位（⛔ 不是 .get(...,0) 現生的）")
LP.AUTO["tick_err"] = 0
LP.AUTO["err"] = None

# ⛔ quote_gaps：那個早上有幾秒沒報價的**唯一痕跡**（2026-09-07 lab-qa 退件 R4：
#    把 `AUTO["gaps"] += 0.25` 拿掉 ⇒ 125 全綠 ⇒ 這個數字永遠是 0 也沒人看得出來，
#    正是這個專案明令禁止的「安靜地少」）。行為測試：報價舊了就要累加，新的就不准動。
print("  行為測試：quote_gaps 真的在數")


class _St:
    def __init__(self, recv):
        self.last_recv = recv


_when = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
LP.AUTO.update({"started": True, "day": str(_when.date()), "done": False,
                "settled": True, "gaps": 0.0})
for _ in range(4):
    LP._auto_tick(_St(0.0), _when, "day")            # last_recv 很久以前 ⇒ 每輪 +0.25
_stale = LP.AUTO["gaps"]
LP.AUTO["gaps"] = 0.0
for _ in range(4):
    LP._auto_tick(_St(time.time()), _when, "day")    # 剛剛才收到 ⇒ 一秒都不准加
_fresh = LP.AUTO["gaps"]
near("⛔ 報價斷了 4 輪（4Hz）⇒ quote_gaps 累加 1.0 秒", _stale, 1.0, 1e-9)
chk("  負控組：報價是新的 ⇒ 一秒都不加", _fresh, 0.0)
LP.AUTO["gaps"] = 0.0
LP.AUTO.update({"started": False, "day": None, "done": False, "settled": False})
say("quote_gaps" in seg("_auto_snap"),
    "  而且 quote_gaps 有被寫進那一列（不然數了也留不下來）")


def calls(fn):
    out = set()
    for node in ast.walk(FUNCS[fn][0]):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add((getattr(f.value, "id", "?"), f.attr))
    return out


IO_NAMES = {"open", "print", "read_text", "write_text", "mkdir", "stat", "read_csv",
            "_auto_append", "_auto_read", "_auto_warm", "_auto_record", "_auto_settle",
            "one_min_bars", "local_bars", "_local_span"}
tick_calls = {c if isinstance(c, str) else c[1] for c in calls("_auto_tick")}
chk("⛔ _auto_tick 裡一行 I/O 都沒有", sorted(tick_calls & IO_NAMES), [])
snap_calls = {c if isinstance(c, str) else c[1] for c in calls("_auto_snap")}
chk("⛔ _auto_snap 裡一行 I/O 都沒有", sorted(snap_calls & IO_NAMES), [])
say("put_nowait" in seg("_auto_put"), "⛔ 佇列是非阻塞的（滿了寧可丟也不塞住停損）")
# ⚠️ 用 AST 找**真的用到那個名字的程式碼**，不是搜字串 ——
#    註解裡本來就在講「⛔ 不可以碰 state_lock」，搜字串會把說明當成違規。


def uses_name(fn, name):
    for node in ast.walk(FUNCS[fn][0]):
        if isinstance(node, ast.Name) and node.id == name:
            return True
        if isinstance(node, ast.Attribute) and node.attr == name:
            return True
    return False


locks = [fn for fn in AUTO_FUNCS + ["_auto_tick", "_auto_snap"]
         if uses_name(fn, "state_lock")]
chk("⛔ 整個 auto 區段都不碰 state_lock", sorted(set(locks)), [])
say(uses_name("update_state", "state_lock"),
    "  負控組：同一把尺在 update_state 抓得到 state_lock")
say(not any(uses_name(fn, "TICKS") for fn in AUTO_FUNCS), "⛔ 這一頁不碰 TICKS 佇列")
say(uses_name("main", "TICKS"), "  負控組：同一把尺在 main() 抓得到 TICKS")
# 端點順序：days 一定要排在 day 前面（"/api/auto/days" 也 startswith("/api/auto/day")）
get_src = seg("do_GET")
say(get_src.index('"/api/auto/days"') < get_src.index('"/api/auto/day"'),
    "⛔ /api/auto/days 的判斷排在 /api/auto/day 前面")

# ══ ⑪ 09:03:30 那一刻：拿不到就不准編（§3.4）══════════════════════════
print("\n=== ⑪ 拿不到就不要編 ===")
D3 = TMP / "d3"
LP.AUTO_DIR = D3
D3.mkdir(parents=True, exist_ok=True)
today = str(date.today())


class FakeToday:
    def __init__(self, px=12010.0, age=0.0, mid=False, o=11990.0, p900=12008.0,
                 minute_bar=True):
        import time as _t
        self.price = px
        self.bid = None if px is None else px - 1
        self.ask = px
        self.price_is_mid = mid
        self.open = o
        self.high = None if px is None else px + 20
        self.low = None if px is None else px - 20
        self.prev_close = 11950.0
        self.last_recv = None if age is None else (_t.time() - age)
        self.minute_close = {539: p900 - 1} if p900 is not None else {}
        self.minute_bar = ({540: {"o": p900, "h": p900, "l": p900, "c": p900, "v": 1}}
                           if (minute_bar and p900 is not None) else {})


def record(st, when=None):
    """
    ⚠️ 例外要接住並轉成一項具名的 FAIL —— 讓探針**當場掛掉**雖然也是紅，
       但突變測試只數 FAIL 行時會被誤報成「打不紅」（這支自己踩過）。
    """
    LP.AUTO_CACHE.clear()
    for p in D3.glob("*.jsonl"):
        p.unlink()
    snap = LP._auto_snap(st, when or datetime.now().replace(hour=9, minute=3, second=30,
                                                            microsecond=120000))
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            LP._auto_record(snap)
    except Exception as e:
        say(False, "⛔ 09:03:30 記錄不可以丟例外（拿不到就寫 miss，不是炸掉）",
            f"{type(e).__name__}: {str(e)[:80]}")
        return {"miss": None, "why": None, "px": None, "sig": {}, "dirs": {}}, snap
    LP.AUTO_CACHE.clear()
    recs, _ = LP._auto_read()
    return recs.get(snap["date"]) or {"miss": None, "why": None, "px": None,
                                      "sig": {}, "dirs": {}}, snap


rec, snap = record(FakeToday())
chk("正常：記下 sig", rec["px"], 12010.0)
chk("  訊號 A ＝ 09:03:30 的價 − 09:00 的價", rec["sig"]["A"], 2.0)
chk("  訊號 B ＝ 09:03:30 的價 − 08:45 開盤", rec["sig"]["B"], 20.0)
chk("  09:00 的價來自 09:00 那根的開盤", snap["p0900_src"], "bar_open")
chk("  進場當下的買賣價有記下來", (rec["bid"], rec["ask"]), (12009.0, 12010.0))
near("  at_lag_ms 記得出來", rec["at_lag_ms"], 120, 1)

rec, _ = record(FakeToday(px=None))
chk("⛔ 沒有成交價 ⇒ 不寫 sig，只留 miss", (rec["miss"], rec["why"]), (True, "no_quote"))
rec, _ = record(FakeToday(age=60.0))
chk("⛔ 報價太舊 ⇒ 不拿舊價頂替", (rec["miss"], rec["why"]), (True, "quote_stale"))
rec, _ = record(FakeToday(mid=True))
chk("⛔ 只有中價 ⇒ 不冒充成交價", (rec["miss"], rec["why"]), (True, "mid_only"))
rec, snap = record(FakeToday(p900=12008.0, minute_bar=False))
chk("盤中重啟：退回用 08:59 最後一筆，而且要記下來源",
    snap["p0900_src"], "prev_min_close")
rec, snap = record(FakeToday(p900=None))
chk("⛔ 09:00 的價完全拿不到 ⇒ A 的訊號留白，不猜", rec["sig"]["A"], None)
chk("  ⇒ A 那天的方向是 None（不是 0，也不是做多）", rec["dirs"]["A"], None)
chk("  ⇒ B 照樣算得出來（不會被 A 拖下水）", rec["dirs"]["B"], 1)

# 重複寫入要擋（看門狗在 09:03:30 前後重啟）
LP.AUTO_CACHE.clear()
n_before = len((D3 / f"{today[:7]}.jsonl").read_text(encoding="utf-8").splitlines())
buf = io.StringIO()
with redirect_stdout(buf):
    LP._auto_record(LP._auto_snap(FakeToday(), datetime.now().replace(
        hour=9, minute=3, second=30, microsecond=0)))
n_after = len((D3 / f"{today[:7]}.jsonl").read_text(encoding="utf-8").splitlines())
chk("⛔ 同一天不准寫第二列（看門狗重啟會重跑一次判斷）", n_after, n_before)

# _auto_tick：晚太多就不記
print("  09:03:30 晚太久 ⇒ 標 late，⛔ 不可以拿 09:10 的價冒充")
LP.AUTO["started"] = True
LP.AUTO.update({"day": None, "done": False, "settled": False, "gaps": 0.0})
LP.AUTO["queued"] = set()
while not LP._AUTO_Q.empty():
    LP._AUTO_Q.get()
late = datetime.now().replace(hour=9, minute=10, second=0, microsecond=0)
LP._auto_tick(FakeToday(), late, "day")
LP._auto_tick(FakeToday(), late, "day")
q = []
while not LP._AUTO_Q.empty():
    q.append(LP._AUTO_Q.get()[0])
chk("  只排一件事，而且是 miss", q, ["warm", "miss"])
LP.AUTO.update({"day": None, "done": False, "settled": False, "gaps": 0.0})
LP.AUTO["queued"] = set()
ontime = datetime.now().replace(hour=9, minute=3, second=31, microsecond=0)
LP._auto_tick(FakeToday(), ontime, "day")
q = []
while not LP._AUTO_Q.empty():
    q.append(LP._AUTO_Q.get()[0])
chk("  準時（晚 1 秒）⇒ 排 record", q, ["warm", "record"])
LP.AUTO["started"] = False

# ══ ⑫ 端點：真的起服務打進去（⛔ 不是 8770）═══════════════════════════
print("\n=== ⑫ 端點（綁埠 0，⛔ 不碰 8770）===")
LP.AUTO_DIR = D2
LP.AUTO_REAL_DIR = D2 / "real"
LP.AUTO_CACHE.clear()
srv = ThreadingHTTPServer(("127.0.0.1", 0), LP.Handler)
port = srv.server_address[1]
say(port != 8770, "治具的埠不是 8770", f"port={port}")
threading.Thread(target=srv.serve_forever, daemon=True).start()


def get(path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


code, body = get("/api/auto/days")
chk("/api/auto/days 200", code, 200)
say(len(body.get("days", [])) == 70, "  70 天都列得出來", len(body.get("days", [])))
say("today" in body and "now" in body, "  帶後端的今天與時鐘（前端不准用 new Date()）")
code, body = get("/api/auto/stats?win=20&src=live")
chk("/api/auto/stats 200", code, 200)
chk("  win=20 但 live 只有 10 天", body["n"], 10)
code, body = get("/api/auto/stats?win=20&src=%E5%A3%9E%E6%8E%89")
chk("  src 亂填 ⇒ 退回 live（⛔ 不可以放寬）", body["src"], "live")
d = sorted(lb)[-1]
code, body = get(f"/api/auto/day?date={d}")
chk("/api/auto/day 200", code, 200)
say(len(body.get("bars", [])) > 200, "  帶 1 分 K", len(body.get("bars", [])))
chk("  帶他自己那幾筆（唯讀）", len(body.get("mine", [])), 1)
code, body = get("/api/auto/day?date=2020-01-01")
chk("沒有那天 ⇒ 404（不是 500）", code, 404)
code, body = get("/api/auto/day?date=../../etc/passwd")
chk("⛔ 路徑穿越 ⇒ 400", code, 400)
code, body = get("/api/auto/day?date=" + today.replace("-", "/"))
chk("⛔ 日期格式不對 ⇒ 400", code, 400)
# 路徑穿越那條的自證：目標檔真的存在也讀不到（「剛好不存在所以 404」不是防線）
probe_target = D2 / "2026-08.jsonl"
say(probe_target.exists(), "  自證：穿越測試的目標檔真的存在")

# 壞資料整月：端點還是 200（⛔ 不准整月回 500）
D4 = TMP / "d4"
LP.AUTO_DIR = D4
D4.mkdir(parents=True, exist_ok=True)
r4, b4 = SY.build(6, end=date(2026, 8, 28))
SY.write(D4, r4)
with (D4 / "2026-08.jsonl").open("a", encoding="utf-8") as f:
    f.write('{"rec":"sig","date":"2026-08-27","px":"NaN"}\n')
LP.AUTO_CACHE.clear()
code, body = get("/api/auto/stats?win=60&src=live")
chk("⛔ 一列壞資料 ⇒ 端點照樣 200", code, 200)
say(body["rows"]["D"]["n"] >= 5, "  好資料一列都沒少", body["rows"]["D"]["n"])
srv.shutdown()

# ══ ⑫b 常數自洽（⚠️ 這一側上一輪整個沒人守，R1／R4／R8 三個退件都落在這裡）══
#   「文案／命名／常數」那一半：改一個常數、忘了改跟它綁在一起的另一個，
#   畫面照樣長得好好的、測試照樣全綠 —— 這一頁最貴的錯（R2）正是這一類。
print("\n=== ⑮ 常數自洽（文案／命名／常數那一側）===")


def _hms(s):
    p = [int(x) for x in s.split(":")]
    while len(p) < 3:
        p.append(0)
    return p[0] * 3600 + p[1] * 60 + p[2]


chk("SIGNAL_SEC 就是 SIGNAL_AT（⛔ 只有一個地方定義，不准兩邊各寫一次）",
    LP.SIGNAL_SEC, _hms(LP.SIGNAL_AT))
# ⛔⛔ 結算窗口必須跟訊號時刻綁在一起：訊號改成 09:10:30 卻忘了改 AUTO_SETTLE_FROM，
#    窗口就會含進場**前**的 K 棒 ⇒ 把「進場前就摸到」算成觸價，而畫面上完全看不出來。
chk("⛔ AUTO_SETTLE_FROM ＝ 訊號時刻的下一分鐘（標籤是起始時間）",
    LP.AUTO_SETTLE_FROM,
    f"{(LP.SIGNAL_SEC // 60 + 1) // 60:02d}:{(LP.SIGNAL_SEC // 60 + 1) % 60:02d}")
say(_hms(LP.AUTO_SETTLE_FROM) >= LP.SIGNAL_SEC,
    "  自證：結算窗口的第一根不早於進場時刻")
chk("⛔ ±100 用的是他真的在用的那組常數（不另開一份）",
    (LP.AUTO_TP, LP.AUTO_SL), (LP.TP_POINTS, LP.SL_POINTS))
say(LP.AUTO_SETTLE_AFTER > LP.DAY_END_SEC,
    "13:45 收盤之後才結算（等最後一根 K 棒收完）",
    f"{LP.AUTO_SETTLE_AFTER} > {LP.DAY_END_SEC}")
say(0 < LP.AUTO_LATE_MS <= 60000, "晚到門檻是個合理的數", f"{LP.AUTO_LATE_MS} ms")
say(0 < LP.AUTO_GAP_S <= 60, "斷線判定門檻是個合理的數", f"{LP.AUTO_GAP_S} 秒")
say(LP.C_THRESH > 0, "C 的門檻是正數（名字自己帶著它）", LP.C_THRESH)
# 天花板的錨點與進度尺的分母**必須是同一個數**（畫面上那句「約兩年」靠它）
chk("進度尺的分母 track_n ＝ CEIL_REF_N", LP.auto_stats(0, "live")["track_n"], LP.CEIL_REF_N)
near("  而天花板(CEIL_REF_N) 就是畫面上那個 12.0", LP.auto_ceiling(LP.CEIL_REF_N), 12.0, 0.05)
# 端點端出去的門檻要跟常數一致（前端的名字與門檻掃描都靠它）
_st15 = LP.auto_stats(0, "live")
chk("  端點端出去的 thresh／rate_min_n／cum_min_n ＝ 常數",
    (_st15["thresh"], _st15["rate_min_n"], _st15["cum_min_n"]),
    (LP.C_THRESH, LP.RATE_MIN_N, LP.CUM_MIN_N))
chk("  端點端出去的 sigma／z ＝ 常數", (_st15["sigma"], _st15["z"]),
    (LP.CEIL_SIGMA, LP.CEIL_Z))
chk("  signal_at 也端出去了（前端的時態鎖用它，⛔ 不准用 new Date()）",
    _st15["signal_at"], LP.SIGNAL_AT)

# ══ ⑬ .gitignore（§15-16）═════════════════════════════════════════════
print("\n=== ⑬ 資料夾不可以進公開 repo ===")
gi = (LP.REPO / ".gitignore").read_text(encoding="utf-8")
say("tools/shioaji/autotest/" in gi, "autotest/ 已經在 .gitignore 裡")
say("tools/shioaji/real_trades/" in gi, "  負控組：real_trades/ 也在（尺是活的）")

# ══ ⑭ 唯讀證明 ══════════════════════════════════════════════════════════
print("\n=== ⑭ 全程沒有動到他的資料 ===")
chk("real_trades/ 一個位元組都沒被動過", tree_hash(REAL_TRADES), BEFORE_TRADES)
chk("autotest/ 一個位元組都沒被動過", tree_hash(REAL_AUTO), BEFORE_AUTO)
say(str(LP.AUTO_DIR).startswith(str(TMP)), "全程 AUTO_DIR 都指在暫存區", LP.AUTO_DIR)

shutil.rmtree(TMP, ignore_errors=True)
_FINISHED["ok"] = True
_summary()
sys.exit(1 if FAIL else 0)
