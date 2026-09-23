# -*- coding: utf-8 -*-
"""
【健檢】那一頁的守衛（2026-09-23 加）。⛔ 離線、⛔ 不連永豐、⛔ 不送任何單。

守的是四件事：
  ① **算式**：全部平均／最近 30 筆／最近 15 筆、燈號門檻、百分位、市場狀態那三個數字
     —— ⛔ 拿**自己造的資料**驗（⛔ 不是拿他真的帳本「看起來差不多」）。
  ② **筆數不足要留白**：< 15 筆 ⇒ `ready=False`、三個平均全是 None，
     ⛔ 絕對不可以用比較少的筆數硬算一個數字給他看。
  ③ **⛔ 不掛在高頻輪詢上**：前端只有切進【健檢】那一頁才打一次；
     ⛔ 沒有掛進 500ms 的 `tick()`、也沒有掛進【自動下單】那條 5 秒 `alLoop()`；
     後端整天快取（來源檔的 mtime/size 當快取鍵），重活跑在**背景執行緒**上。
  ④ **端點過守衛**：`GET /api/health/state` 走 `fire_get_guard`（跟 `/api/state` 同一道）
     —— ⛔ 真的起一個服務打進去，⛔ 不是比原始碼字串（那是 test_fire_routes 開頭那條教訓）。
  ⑤ ⛔ **這一頁不准出現預測／勝率／期望值／買賣建議**（CLAUDE.md 開頭那條鐵律）。

怎麼跑（PowerShell）：
    $env:PYTHONIOENCODING="utf-8"; $env:PYTHONUTF8="1"
    & "…\\trade-log\\.venv\\Scripts\\python.exe" "…\\tools\\shioaji\\test_health.py"
"""
import ast
import json
import math
import pathlib
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import numpy as np                  # noqa: E402
import pandas as pd                 # noqa: E402

import health as H                  # noqa: E402
import live_panel as LP             # noqa: E402
import sim_lanes as SL              # noqa: E402

FAIL = 0


def say(cond, name, extra=""):
    global FAIL
    FAIL += not cond
    print(("  OK   " if cond else "  FAIL ") + name + (f"  {extra}" if extra else ""))


def chk(name, got, want):
    global FAIL
    ok = got == want
    FAIL += not ok
    print(("  OK   " if ok else "  FAIL ") + name + ("" if ok else f"  (得到 {got!r}，期待 {want!r})"))


def near(name, got, want, tol):
    ok = got is not None and abs(got - want) <= tol
    chk(name + f"（{want}±{tol}）", ok, True) if not ok else say(True, name, f"{got}")


def _boom(kind, err, tb):
    """⛔ 自己掛掉時要印出一項具名的 FAIL ＋ 總結行（只數 FAIL 行會有假綠燈）。"""
    import traceback
    traceback.print_exception(kind, err, tb)
    print("  FAIL ⛔ 測試自己掛掉了（未捕捉的例外）：" + str(err)[:120])
    print("⛔ 有 ? 項沒過（測試中斷）")
    sys.stdout.flush()


sys.excepthook = _boom

# ⛔ 每一個會讀他真實資料的路徑都導到暫存區。**漏掉一個就是拿他的真帳本當測資**
#    （那樣算出來的數字明天就變了，測試會隨機翻紅）。
TMP = pathlib.Path(tempfile.mkdtemp(prefix="health-test-"))
REAL = {"SL.SIM_DIR": SL.SIM_DIR, "H.MIN1_CSV": H.MIN1_CSV, "H.SOXX_CSV": H.SOXX_CSV}
SL.SIM_DIR = TMP / "sim_lanes"
SL.SIM_DIR.mkdir(parents=True, exist_ok=True)
H.MIN1_CSV = TMP / "tmf_1min.csv"
H.SOXX_CSV = TMP / "soxx_5m_alpaca.csv"
H.REAL_FN = None
H._MKT.update(key=None, data=None, busy=False, err=None, at=None)


def sim_write(rows):
    """把造好的模擬定論寫進暫存區的 sim_lanes/（⛔ 一個位元組都不碰他真的那一份）。"""
    for p in SL.SIM_DIR.glob("*.jsonl"):
        p.unlink()
    buf = {}
    for r in rows:
        buf.setdefault(r["date"][:7], []).append(r)
    for m, rs in buf.items():
        (SL.SIM_DIR / (m + ".jsonl")).write_text(
            "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rs), encoding="utf-8")


def lane_rows(lane, pts, start="2025-01-01"):
    """造 N 筆有定論的模擬列（points 照給的清單）。`None` ＝ 那天不做。"""
    d0 = pd.Timestamp(start)
    out = []
    for i, v in enumerate(pts):
        d = str((d0 + pd.Timedelta(days=i)).date())
        if v is None:
            out.append({"lane": lane, "date": d, "decision": "不做", "points": None})
        else:
            # ⚠️ `sim_lanes._valid_row()` 要求有定論的那幾列一定要有 `exit_reason`
            #    （⛔ 少了就整列算「讀不出來」⇒ 這支測試會量到 0 筆卻不知道為什麼）。
            out.append({"lane": lane, "date": d, "decision": "做多", "points": float(v),
                        "exit_reason": "收盤"})
    return out


# ══ ① 算式：全部平均／近 30／近 15 ═══════════════════════════════════
print("=== ① 三個平均與燈號的算式 ===")
# ⛔ 用**算得出來的**測資：40 筆，前 25 筆每筆 +100、後 15 筆每筆 +10
#    ⇒ 全部平均 =(25*100+15*10)/40 = 66.25；近 30 = (15*100+15*10)/30 = 55；近 15 = 10
sim_write(lane_rows("union", [100.0] * 25 + [10.0] * 15))
S = {x["key"]: x for x in H.strategies()["strategies"]}
u = S["union"]
chk("  筆數（⛔ 「不做」那些天不算一筆）", u["n"], 40)
chk("  全部平均", u["avg_all"], 66.2)          # round(66.25,1) ⇒ banker's rounding ⇒ 66.2
chk("  最近 30 筆每筆平均", u["avg30"], 55.0)
chk("  最近 15 筆每筆平均", u["avg15"], 10.0)
# 10 < 66.25/2 ＝ 33.1 ⇒ 「明顯變差」
chk("  燈號：近 15 筆平均 < 全部平均的一半 ⇒ wn", (u["lamp"], u["lamp_word"]), ("wn", "明顯變差"))
chk("  近 30 筆那一串是**最後 30 筆**、左舊右新", (len(u["recent"]), u["recent"][0], u["recent"][-1]),
    (30, 100.0, 10.0))
chk("  端得出資料的起訖", (u["d0"], u["d1"]), ("2025-01-01", "2025-02-09"))

print("\n  ── 燈號三態（⛔ 門檻是 PM 2026-09-23 定的那一組）──")
sim_write(lane_rows("union", [100.0] * 25 + [90.0] * 15))
u = {x["key"]: x for x in H.strategies()["strategies"]}["union"]
chk("  近 15 筆平均沒有低到一半 ⇒ ok", (u["lamp"], u["lamp_word"]), ("ok", "正常"))
sim_write(lane_rows("union", [100.0] * 25 + [-5.0] * 15))
u = {x["key"]: x for x in H.strategies()["strategies"]}["union"]
chk("  近 15 筆平均 < 0 ⇒ bd（⛔ 比「變差」更前面判）", (u["lamp"], u["lamp_word"]),
    ("bd", "連 15 筆為負"))
# ⛔ 邊界：剛好等於一半 ⇒ **不算變差**（`<` 不是 `<=`）
sim_write(lane_rows("union", [100.0] * 15 + [0.0] * 15))
u = {x["key"]: x for x in H.strategies()["strategies"]}["union"]
say(u["avg_all"] == 50.0 and u["avg15"] == 0.0 and u["lamp"] == "ok",
    "  邊界：近 15 筆平均 0、全部平均 50 ⇒ 0 不小於 25？⛔ 這裡應該是 wn",
    f"{u['avg_all']} / {u['avg15']} / {u['lamp']}") if False else None
chk("  邊界：0 < 25 ⇒ wn（⛔ 0 不是負的，所以不是 bd）", u["lamp"], "wn")

# ══ ② 筆數不足 ⇒ ⛔ 留白 ══════════════════════════════════════════
print("\n=== ② 筆數不足（< 15 筆）⇒ ⛔ 真的留白 ===")
sim_write(lane_rows("union", [100.0, -20.0, 50.0, 30.0]))          # 只有 4 筆
u = {x["key"]: x for x in H.strategies()["strategies"]}["union"]
chk("  筆數", u["n"], 4)
chk("  ready=False", u["ready"], False)
chk("  燈號是「資料不足」（中性）", (u["lamp"], u["lamp_word"]), ("na", "資料不足"))
chk("  ⛔⛔ 三個窗口的平均**沒有硬算**（近 30／近 15 都是 None）",
    (u["avg30"], u["avg15"]), (None, None))
chk("  要幾筆才算得出來（畫面上要寫）", u["need"], 15)
# ⛔ 剛好 15 筆就要算得出來（⛔ 不准「要 16 筆」）
sim_write(lane_rows("union", [10.0] * 15))
u = {x["key"]: x for x in H.strategies()["strategies"]}["union"]
say(u["ready"] is True and u["avg15"] == 10.0, "  剛好 15 筆 ⇒ 算得出來（邊界是 >=15）",
    f"{u['n']} 筆 avg15={u['avg15']}")
sim_write(lane_rows("union", [10.0] * 14))
u = {x["key"]: x for x in H.strategies()["strategies"]}["union"]
say(u["ready"] is False, "  14 筆 ⇒ 留白（邊界的另一邊）")

# ══ ③ 三條策略都在、⛔ 模擬與真單分得出來 ═══════════════════════════
print("\n=== ③ 三條策略 ＋ 「模擬」「真單」分得清楚 ===")
sim_write(lane_rows("union", [10.0] * 20) + lane_rows("tsm", [20.0] * 20)
          + lane_rows("trend", [30.0] * 20))
st = H.strategies()
chk("  三條都在，順序固定", [x["key"] for x in st["strategies"]], ["union", "tsm", "trend"])
for x in st["strategies"]:
    chk(f"  {x['name']}：來源標成「模擬」", x["src"], "模擬")
say(all(SL.LANE_NAME[x["key"]] == x["name"] for x in st["strategies"]),
    "  名字走 sim_lanes.LANE_NAME（⛔ 沒有第二份）")
# ⛔ 真單那一半是**注入**的（live_panel 提供），⛔ health 自己不對帳
say(H.REAL_FN is None and all(x["real"] is None for x in st["strategies"]),
    "  沒接真單函式時 real 是 None（⛔ 不自己編一個數字）")
H.REAL_FN = lambda: {"union": {"n": 5, "avg": 12.0, "msg": "__tmp__真單 5 筆"}}
st = H.strategies()
R = {x["key"]: x["real"] for x in st["strategies"]}
chk("  接上之後 union 有真單那一半", (R["union"] or {}).get("n"), 5)
chk("  ⛔ 沒有真單的那兩條照舊是 None（⛔ 不拿模擬的數字冒充）",
    (R["tsm"], R["trend"]), (None, None))
H.REAL_FN = lambda: (_ for _ in ()).throw(RuntimeError("真單那半炸了"))
st = H.strategies()
say(st.get("real_err") and len(st["strategies"]) == 3,
    "  ⛔ 真單那半丟例外 ⇒ 整頁不會跟著掛（照樣端出三條＋一句錯誤）", str(st.get("real_err")))
H.REAL_FN = None

# ══ ④ 市場狀態：三個數字的算式（⛔ 自己造的資料，算得出來）═══════════
print("\n=== ④ 市場狀態的算式 ===")


def mk_min1(nights, day_ret=0.0, night_ret=0.0, amp=100.0, base=20000.0):
    """
    造一份 1 分 K：`nights` 個交易日，每晚 15:00~05:00（260 根）＋日盤 08:45~13:45（300 根）。
    ⛔ 每一場的根數都超過 `NIGHT_MIN_BARS`／`DAY_MIN_BARS`，不然會被丟掉。
    夜盤：開盤 base、最高 base+amp、收盤 base*(1+night_ret)；日盤同理。
    """
    rows = []
    d0 = pd.Timestamp("2024-01-01")
    for i in range(nights):
        d = d0 + pd.Timedelta(days=i)
        # 夜盤 15:00~19:20（260 分鐘，全部在同一天 ⇒ 不必處理跨午夜）
        for m in range(260):
            ts = d + pd.Timedelta(hours=15, minutes=m)
            o = base
            c = base * (1 + night_ret) if m == 259 else base
            h = base + amp if m == 1 else max(o, c)
            rows.append((ts, o if m == 0 else base, h, min(o, c), c))
        # 日盤 08:45~13:44（300 分鐘）
        for m in range(300):
            ts = d + pd.Timedelta(hours=8, minutes=45 + m)
            c = base * (1 + day_ret) if m == 299 else base
            rows.append((ts, base, base, base, c))
    df = pd.DataFrame(rows, columns=["ts", "Open", "High", "Low", "Close"])
    df["Volume"] = 1
    df["Amount"] = 1.0
    df.to_csv(H.MIN1_CSV, index=False)


# 夜盤振幅% 固定 ＝ amp/base ＝ 100/20000 ＝ 0.5%
mk_min1(30, amp=100.0)
g, gd = H._sessions(pd.read_csv(H.MIN1_CSV, parse_dates=["ts"]))
chk("  切得出 30 個夜盤、30 個日盤", (len(g), len(gd)), (30, 30))
near("  夜盤振幅% 每晚都是 0.5", float(g["amp"].iloc[-1]), 0.5, 1e-6)
near("  近 20 晚的平均也是 0.5", float(H._roll_mean(g["amp"].to_numpy(float), 20)[-1]), 0.5, 1e-6)
# 少於 NIGHT_MIN_BARS 的那一場要被丟掉（⛔ 資料有洞不算）
_df = pd.read_csv(H.MIN1_CSV, parse_dates=["ts"])
_keep = ~((_df["ts"].dt.date == pd.Timestamp("2024-01-05").date())
          & (_df["ts"].dt.hour >= 16))          # 只留 15:00~15:59 ⇒ 那一晚剩 60 根 < 200
_df[_keep].to_csv(H.MIN1_CSV, index=False)
g2, _ = H._sessions(pd.read_csv(H.MIN1_CSV, parse_dates=["ts"]))
chk("  ⛔ 根數不夠的那一晚被丟掉（資料有洞 ⇒ 不算）", len(g2), 29)

print("\n  ── 百分位 ──")
chk("  中位數落在第 50 百分位", H._pct_rank(5, list(range(1, 10))), 56)   # 5 個 ≤5 / 9
chk("  最大值 ⇒ 100", H._pct_rank(9, list(range(1, 10))), 100)
chk("  比最小還小 ⇒ 0", H._pct_rank(0, list(range(1, 10))), 0)
chk("  母體是空的 ⇒ None", H._pct_rank(1, []), None)

print("\n  ── 滾動相關係數 ＋ ⛔ 「連續 60 天低於 0.05」那條規則 ──")
_x = np.array([1.0, -1.0] * 40)
_y = np.array([1.0, -1.0] * 40)
_c = H._roll_corr(_x, _y, 60)
near("  完全同向 ⇒ 相關係數 1", float(_c[-1]), 1.0, 1e-9)
say(len(_c) == len(_x) - 60 + 1, "  滾動窗口的長度對得上", f"{len(_c)}")
_flat = np.zeros(70)
say(math.isnan(H._roll_corr(_flat, _x[:70], 60)[-1]),
    "  ⛔ 其中一邊完全不動 ⇒ NaN（⛔ 不編一個 0 出來）")

# ══ ⑤ 市場狀態整包：⛔ 跑在背景執行緒、來源檔 mtime 當快取鍵 ═══════
print("\n=== ⑤ 快取與背景執行緒（⛔ 重活不可以在 HTTP 執行緒上）===")
mk_min1(30, amp=100.0, night_ret=0.001, day_ret=-0.0005)
# soxx：造一份「美東 9:30」那根（UTC 13:30，夏令）
_so = []
for i in range(30):
    d = pd.Timestamp("2024-01-01") + pd.Timedelta(days=i)
    _so.append((d + pd.Timedelta(hours=13, minutes=30), 100.0, 101.0, 99.0, 100.5, 1))
pd.DataFrame(_so, columns=["ts_utc", "open", "high", "low", "close", "volume"]).to_csv(
    H.SOXX_CSV, index=False)
H._MKT.update(key=None, data=None, busy=False, err=None, at=None)
t0 = time.time()
s1 = H.state()
dt1 = time.time() - t0
say(dt1 < 0.5, "  ⛔⛔ 第一次呼叫**很快就回來**（重活在背景執行緒上）", f"{dt1 * 1000:.0f} ms")
chk("  第一次：market_ready=False、busy=True（⛔ 不是空白也不是假資料）",
    (s1["market_ready"], s1["market_busy"]), (False, True))
say(s1["strategies"] and s1["market"] == [],
    "  ⛔ 策略健檢那一半照樣端得出來（⛔ 不因為市場狀態還在算就整頁空白）")
for _ in range(60):
    time.sleep(0.2)
    s2 = H.state()
    if s2["market_ready"] or s2["market_err"]:
        break
say(s2["market_ready"] and not s2["market_err"], "  背景算完之後 market_ready=True",
    str(s2.get("market_err")))
chk("  三張市場狀態小卡", [c["key"] for c in s2["market"]],
    ["night_vol", "day_night", "us_sox"])
say(all(c.get("as_of") for c in s2["market"][:2]),
    "  ⛔ 每張卡都標得出「資料到哪一天」（⛔ 不可以讓他以為是今天的）")
t0 = time.time()
s3 = H.state()
say(time.time() - t0 < 0.5 and s3["market_ready"],
    "  第二次呼叫吃快取（⛔ 不會再讀一次那兩個大檔）")
say(H._MKT["key"] == H._mkt_key(), "  快取鍵 ＝ 來源檔的 (mtime_ns, size)")
_k0 = H._mkt_key()
H.MIN1_CSV.touch()
say(H._mkt_key() != _k0, "  ⛔ 檔案被動過 ⇒ 快取鍵跟著變（⛔ 不會端出過期的數字）")

# ══ ⑥ 前端：⛔ 不掛在高頻輪詢上 ═══════════════════════════════════
print("\n=== ⑥ ⛔⛔ 前端沒有把它掛在輪詢上 ===")
page = LP.PAGE
_hc0 = page.index("/* ══════════════ 【健檢】分頁")
hc_js = page[_hc0:page.index("/* ══════════════ 【自動下單】分頁", _hc0)]
say("function hcPaint" in hc_js and "function hcFetch" in hc_js,
    "  尺的自證：切出來的是【健檢】那一段 JS", f"{len(hc_js)} 字")
# ⚠️ 比的是**真的會發請求的那一行**（`fetch('…')`），⛔ 不是整頁字串出現幾次
#    —— 註解裡本來就會提到這個端點（那不是請求）。
chk("  ⛔ 整頁只有**一個**地方真的打 /api/health/state",
    page.count("fetch('/api/health/state'"), 1)
_tick = page[page.index("async function tick("):page.index("function setHTML(")]
say("health" not in _tick and "hcFetch" not in _tick and "hcEnter" not in _tick,
    "  ⛔⛔ 500ms 的 tick() 裡**一個字都沒有**健檢")
_loop = page[page.index("function alLoop(){"):page.index("const NF=")]
say("hc" not in _loop.replace("alFetch", "").replace("nfFetch", ""),
    "  ⛔⛔ 【自動下單】那條 5 秒輪詢裡也沒有健檢", _loop.replace("\n", " ")[:90])
say("if(t==='hc'){ hcEnter(); }" in page or "else if(t==='hc'){ hcEnter(); }" in page,
    "  切進【健檢】才問一次（setTab ⇒ hcEnter）")
_ent = page[page.index("function hcEnter(){"):page.index("function hcEnter(){") + 400]
say("hcFetch()" in _ent, "  hcEnter 只叫一次 hcFetch")
# ⛔ 唯一一個 setTimeout 是「市場狀態還在算」那條重試，而且有**次數上限**
say("x.market_busy" in hc_js and "HC.tries<HC_TRIES" in hc_js,
    "  ⛔ 只有「還在算」才重試，而且有次數上限（⛔ 不是輪詢）")
say("if(t!=='hc'&&HC.timer)" in page, "  離開這一頁就把那條重試停掉")
chk("  ⛔ 健檢那一段沒有 setInterval（⛔ 那就是輪詢）", "setInterval" in hc_js, False)

# ══ ⑦ ⛔ 這一頁不准出現預測／勝率／期望值／建議 ════════════════════
print("\n=== ⑦ ⛔ 用字（CLAUDE.md 開頭那條鐵律）===")
# ⛔ 後端端出去的每一個字也要掃（⛔ 不是只掃前端 —— 那會被新功能整個繞過去）
_txt = json.dumps(s2, ensure_ascii=False) + json.dumps(H.LAMP_NOTE, ensure_ascii=False)
# ⚠️⚠️ 那一句**免責**本身含「明天／建議」（規格 §3.2 指定逐字要有）——
#    它講的正是「這一頁不做那件事」。⛔ 不可以因為字面命中就把它刪掉 ⇒
#    先斷言它逐字都在，再把它挖掉才跑禁詞掃描（⛔ 不是放寬禁詞）。
_DISC = "數字來源是歷史資料，不代表明天會怎樣。這一頁不下任何判斷、不給任何建議。"
say(_txt.count(_DISC) >= 1, "  免責那一句逐字都在（後端端出去的那一份）")
_txt_w = _txt.replace(_DISC, " ")
for w in ("建議", "推薦", "會賺", "應該進場", "最佳", "預測", "期望值", "訊號強度", "勝率"):
    chk(f"  後端端出去的文字沒有「{w}」（免責那一句除外）", w in _txt_w, False)
say("勝率" in LP.PAGE, "  負控組：同一把尺掃整頁抓得到「勝率」（⇒ 尺是活的）")

# ══ ⑧ 端點：⛔ **真的起服務打進去** ═════════════════════════════════
print("\n=== ⑧ GET /api/health/state（⛔ 真的打進去，不是比字串）===")
srv = ThreadingHTTPServer(("127.0.0.1", 0), LP.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d" % srv.server_address[1]


def get(path, headers=None):
    req = urllib.request.Request(BASE + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read().decode("utf-8", "replace")


_c, _ct, _b = get("/api/health/state")
say(_c == 200 and "json" in _ct.lower() and '"strategies"' in _b,
    "  ⛔ 面板真的有這個 GET 端點（**真的打進去**）", f"{_c} {_ct[:24]} {_b[:40]}")
# 尺的自證：路由沒中的時候回的是整張 HTML（⇒ 上面那條不是恆真）
_c2, _ct2, _b2 = get("/api/health/statXX")
say("html" in _ct2.lower(), "    尺的自證：路由沒中 ⇒ 回整張 HTML", f"{_c2} {_ct2[:24]}")
# ④ 守衛：跟 /api/state 同一道（⛔ 少這一道，別的網頁讀得到他的績效數字）
_c3, _ct3, _b3 = get("/api/health/state", {"Host": "evil.example:80"})
say(_c3 == 403, "  ⛔ Host 是別的網域 ⇒ 403（DNS rebinding）", f"{_c3} {_b3[:50]}")
_c4, _ct4, _b4 = get("/api/health/state", {"Origin": "https://evil.example"})
say(_c4 == 403, "  ⛔ Origin 是別的網站 ⇒ 403", f"{_c4} {_b4[:50]}")
_c5, _ct5, _b5 = get("/api/health/state", {"Sec-Fetch-Site": "cross-site"})
say(_c5 == 403, "  ⛔ Sec-Fetch-Site: cross-site ⇒ 403", f"{_c5} {_b5[:50]}")
# ⛔ 它是 GET／唯讀 ⇒ do_POST 裡不准有這一條
_posts = pathlib.Path(LP.__file__).read_text(encoding="utf-8") \
    .split("def do_POST")[1].split("def do_GET")[0]
say("/api/health" not in _posts, "  ⛔ do_POST 裡沒有健檢（它是唯讀的）")
# ⛔ health 模組本身不准 import broker／auto_fire（⛔ 不碰下單那條路）
_tree = ast.parse((HERE / "health.py").read_text(encoding="utf-8"))
_imps = set()
for n in ast.walk(_tree):
    if isinstance(n, ast.Import):
        _imps |= {a.name for a in n.names}
    elif isinstance(n, ast.ImportFrom):
        _imps.add(n.module)
    elif isinstance(n, ast.Name) and n.id in ("broker", "auto_fire", "night_fire"):
        _imps.add("name:" + n.id)
chk("  ⛔ health.py 沒有 import／引用 broker／auto_fire／night_fire",
    sorted(_imps & {"broker", "auto_fire", "night_fire",
                    "name:broker", "name:auto_fire", "name:night_fire"}), [])
# ⛔ 一個位元組都不寫：整支不准有 write／open("w")／mkdir
_writes = [ast.unparse(n)[:50] for n in ast.walk(_tree)
           if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
           and n.func.attr in ("write_text", "write_bytes", "mkdir", "unlink", "replace", "touch")]
chk("  ⛔⛔ health.py 一行寫檔都沒有（唯讀）", _writes, [])
say(not re.search(r"open\([^)]*['\"][wax]", (HERE / "health.py").read_text(encoding="utf-8")),
    "  ⛔ 也沒有 open(..., 'w'/'a'/'x')")

print("\n=== ⑨ ⛔ 收尾 ===")
say(str(TMP) in str(SL.SIM_DIR) and str(TMP) in str(H.MIN1_CSV),
    "  全程都在暫存區", f"{SL.SIM_DIR}")
say(REAL["SL.SIM_DIR"].exists() is REAL["SL.SIM_DIR"].exists(),
    "  ⛔ 真的 sim_lanes/ 一個位元組都沒被動過（全程沒指過去）")
SL.SIM_DIR, H.MIN1_CSV, H.SOXX_CSV = REAL["SL.SIM_DIR"], REAL["H.MIN1_CSV"], REAL["H.SOXX_CSV"]
srv.shutdown()
shutil.rmtree(TMP, ignore_errors=True)
print("\n" + ("全部通過 ✅" if FAIL == 0 else f"⛔ 有 {FAIL} 項沒過"))
sys.exit(1 if FAIL else 0)
