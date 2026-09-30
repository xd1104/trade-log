# -*- coding: utf-8 -*-
"""
【健檢】分頁的後端（2026-09-23 加）—— ⛔ 唯讀、⛔ 一個位元組都不寫。

這一支只做一件事：**把已經發生的數字算出來**。
⛔⛔ 這一頁不准出現預測、勝率、期望值、訊號強度、買賣建議（CLAUDE.md 開頭那條鐵律）。
   燈號**只描述已經發生的數字**，⛔ 不准附「要不要關掉／要不要加碼」。

兩區：
  ① 策略健檢：正在跑真單的那幾條，**主要資料來源是【模擬】的定論**（`sim_lanes/*.jsonl`）。
     ⛔ 為什麼不是真單帳本 —— 真單樣本太少（多方聯軍真單只有個位數筆），
        15 筆／30 筆的窗口結構上算不出來。模擬那一份是**同一條規則、每天事後照規則算一次**，
        而且回填到 2024-08 ⇒ 那是唯一有足夠筆數的尺。
     ⛔⛔ 畫面上一定要標清楚哪一個數字是「模擬」、哪一個是「真單」（混在一起就是騙自己）。
     ⛔ 模擬的點數**直接讀 sim_lanes 的定論**（`sim_lanes.read_rows()`），
        ⛔ 不在這裡重算規則 —— 那就是第二把尺。
  ② 市場狀態：三個數字 ＋ 各自在過去一年的百分位。
     ⛔ 一律只講「現在落在哪裡」。
     ⭐ 2026-09-30 Benson 要「一眼看出是甚麼狀況」⇒ 每張卡多一句白話（`_now_line`）：
        只把百分位／正負翻成「比平常安靜、漲的都在夜盤」這種描述；⛔ 仍然不准預測、不准建議。

⚠️⚠️ **效能**（CLAUDE.md「重活跑在 HTTP 執行緒上會拖慢主迴圈」那一條）：
   市場狀態那一區要讀 `tmf_1min.csv`（約 39 MB）與 `soxx_5m_alpaca.csv`（約 12 MB）⇒
   ⛔ **絕對不可以掛在 5 秒輪詢上**，也不可以在 HTTP 執行緒裡直接算。
   做法：
     ・`state()` 只回「算好的那一份」；沒算好就回 `market_ready=False`（前端隔幾秒再問一次）。
     ・真正的計算在**背景執行緒**（`_ensure_market()` 起的那一條），而且讀檔分批
       ＋ 每批之間 `time.sleep(0)` 讓出 GIL（停損活在 4Hz 主迴圈裡，⛔ 不可以被這一頁塞住）。
     ・快取鍵 ＝ 來源檔的 `(mtime_ns, size)`（跟 `_SESS_OWN`／`_FIRE_REAL_CACHE` 同一招）⇒
       檔案沒變就整天不會再算第二次。

⛔ 真單那一半（`REAL_FN`）由 `live_panel` 注入：日盤走既有的 `fire_real_pairs()`（唯讀比對
   `real_trades/`），⛔ 這裡不自己再寫一份對帳法。
"""
import json
import math
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import sim_lanes

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent                       # trade-log/ 根目錄
MIN1_CSV = HERE / "tmf_1min.csv"                # ⛔ 唯讀
# ⛔⛔ 這個檔在**另一個 repo**（tick-research）⇒ **唯讀，一個位元組都不准寫**。
SOXX_CSV = REPO.parent / "tick-research" / "us" / "soxx_5m_alpaca.csv"
# ⭐ 2026-09-30：上面那份是研究時一次下載的（停在 09-15）⇒ 面板每天早上用 us_feed 補抓 SOXX 月檔到 us_bars/，
#    這裡**只讀**、跟上面那份合併（同一根以面板那份為準）。⛔ 這支照舊一個位元組都不寫。
SOXX_PANEL_DIR = HERE / "us_bars"

# ── 策略健檢 ─────────────────────────────────────────────────────────
# ⛔ 名字一律從 sim_lanes.LANE_NAME（⛔ 不准在這裡寫第二份）。
NEED = 15               # 少於這麼多筆 ⇒ ⛔ 留白，不用比較少的筆數硬算
W15, W30 = 15, 30
STRATS = (
    {"key": "union", "sub": "日盤・每天 09:03:30・1 口", "real": "day"},
    # ⭐ 2026-09-30：日盤開關寫 A 時跑的是快攻回馬槍 ⇒ 開著才出現（真單那一半跟多方聯軍讀同一本日盤帳）
    {"key": "hmq", "sub": "日盤・09:03:30／09:15・1 口", "real": "day"},
    {"key": "tsm", "sub": "夜盤・美股開盤後・1 口", "real": "night"},
    # ⚠️ 2026-09-23 起夜盤跟勢可以在【自動下單】選來下真單（跟台積電快攻擇一）⇒ 真單欄照實接上。
    {"key": "trend", "sub": "夜盤・美股開盤後 10 分鐘・1 口（夜盤選它才下單）", "real": "night"},
)
# ⛔ 燈號的門檻（PM 2026-09-23 定）。⛔ 前端不准自己算一份。
LAMP_WORD = {"ok": "正常", "wn": "明顯變差", "bd": "連 15 筆為負", "na": "資料不足"}
LAMP_NOTE = {
    "ok": "近 %d 筆每筆平均沒有低到全部平均的一半" % W15,
    "wn": "近 %d 筆每筆平均 < 全部平均的一半" % W15,
    "bd": "近 %d 筆每筆平均 < 0" % W15,
    "na": "定論還不到 %d 筆，這裡留白" % NEED,
}

# ── 市場狀態 ─────────────────────────────────────────────────────────
VOL_WIN = 20            # 夜盤波動度：最近幾晚
VOL_REF = 250           # 拿來對照的中位數用過去幾晚
RET_WIN = 60            # 日夜盤漲幅：最近幾天
COR_WIN = 60            # 連動度：滾動幾晚
YEAR_N = 250            # 「過去一年」＝這麼多個值
COR_LOW = 0.05          # 連動度低於這個數
COR_LOW_RUN = 60        # 連續這麼多個交易日都低於 ⇒ 標「要注意」（PM 2026-09-23 定）
NIGHT_MIN_BARS = 200    # 一晚少於這麼多根 1 分 K ⇒ 資料有洞，⛔ 不算（跟 sim_lanes 同一個口徑）
DAY_MIN_BARS = 200
CSV_CHUNK = 200_000     # 讀 csv 每批幾列（每批之間讓出 GIL）

# ⛔ 真單那一半由 live_panel 注入（⛔ 這裡不自己對帳）。
REAL_FN = None
# ⭐ 2026-09-30（Benson：「健檢應該只要出現我現在有開策略的真單就好」）⇒ live_panel 注入「現在開著的真單是哪幾條」。
#    ⛔ 沒注入（週報、測試）⇒ 照舊三條（DEFAULT_KEYS）；讀開關失敗 ⇒ 也照舊三條並講出來（⛔ 不猜）。
ARMED_FN = None
DEFAULT_KEYS = ("union", "tsm", "trend")
_MKT = {"key": None, "data": None, "busy": False, "err": None, "at": None}
_LOCK = threading.Lock()


def configure(real_fn=None, armed_fn=None):
    """live_panel 啟動時接線。⛔ 只接一次、⛔ 不在這裡做任何 I/O。"""
    global REAL_FN, ARMED_FN
    REAL_FN = real_fn
    ARMED_FN = armed_fn


# ══ 小工具 ══════════════════════════════════════════════════════════

def _sig(p):
    """檔案指紋 (mtime_ns, size)；不存在 ⇒ None。⛔ 只 stat，不讀內容。"""
    try:
        s = p.stat()
        return (s.st_mtime_ns, s.st_size)
    except OSError:
        return None


def _avg(xs):
    return round(sum(xs) / len(xs), 1) if xs else None


def _pct_rank(cur, pool):
    """`cur` 落在 `pool` 的第幾百分位（0~100）。pool 空的 ⇒ None。"""
    if cur is None or not len(pool):
        return None
    le = int(np.sum(np.asarray(pool) <= cur))
    return int(round(le * 100.0 / len(pool)))


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))


# ══ ① 策略健檢 ══════════════════════════════════════════════════════

def _lane_points(rows, lane):
    """
    某一條的【模擬】定論，**舊到新**的每筆點數。
    ⛔ 「不做」那些天不算一筆（那天沒有部位、沒有點數）；⛔ 點數不是數字的也不算。
    """
    rs = sorted((r for (ln, _d), r in rows.items() if ln == lane), key=lambda r: r["date"])
    out = []
    for r in rs:
        if r.get("decision") == "不做":
            continue
        if _num(r.get("points")):
            out.append((r["date"], float(r["points"])))
    return out


def _lamp(pts):
    """⇒ (lamp, avg_all, avg30, avg15)。⛔ 筆數不足就是 'na'，⛔ 不用少的筆數硬算。"""
    n = len(pts)
    if n < NEED:
        return "na", (_avg(pts) if pts else None), None, None
    a_all = sum(pts) / n
    a30 = sum(pts[-W30:]) / min(n, W30)
    a15 = sum(pts[-W15:]) / W15
    if a15 < 0:
        lamp = "bd"
    elif a15 < a_all / 2.0:
        lamp = "wn"
    else:
        lamp = "ok"
    return lamp, round(a_all, 1), round(a30, 1), round(a15, 1)


def strategies(now=None):
    """策略健檢那一區。⛔ 唯讀（只讀 sim_lanes/ 與注入的真單函式）。"""
    now = now or datetime.now()
    rows, fst = sim_lanes.read_rows()
    real = {}
    if REAL_FN is not None:
        try:
            real = REAL_FN() or {}
        except Exception as e:                      # ⛔ 真單那半壞掉不可以把整頁帶掉
            real = {"_err": str(e)[:120]}
    keys, armed_note = list(DEFAULT_KEYS), None
    if ARMED_FN is not None:
        try:
            got = [k for k in (ARMED_FN() or []) if k]
            keys = [S["key"] for S in STRATS if S["key"] in got]
            names = "、".join(sim_lanes.LANE_NAME.get(k, k) for k in keys)
            armed_note = ("只顯示現在開著的自動真單：" + names) if keys else "目前沒有開著的自動真單"
        except Exception as e:                      # ⛔ 讀不到開關 ⇒ 照舊三條、講出來
            armed_note = "讀不到自動下單開關（%s），先照舊顯示三條" % str(e)[:60]
    out = []
    for S in STRATS:
        k = S["key"]
        if k not in keys:
            continue
        pts = _lane_points(rows, k)
        vals = [p for _d, p in pts]
        lamp, a_all, a30, a15 = _lamp(vals)
        R = real.get(k) if isinstance(real, dict) else None
        out.append({
            "key": k,
            "name": sim_lanes.LANE_NAME.get(k, k),
            "sub": S["sub"],
            "src": "模擬",                          # ⛔ 畫面一定要標出來
            "src_note": "【模擬】每天事後照規則算一次的定論（%s）" % sim_lanes.SRC_NAME.get(k, ""),
            "n": len(vals),
            "need": NEED,
            "ready": lamp != "na",
            "avg_all": a_all, "avg30": a30, "avg15": a15,
            "lamp": lamp, "lamp_word": LAMP_WORD[lamp], "lamp_note": LAMP_NOTE[lamp],
            "recent": [round(v, 1) for _d, v in pts[-W30:]],
            "d0": pts[0][0] if pts else None,
            "d1": pts[-1][0] if pts else None,
            "real": R,
        })
    return {"strategies": out, "armed_note": armed_note, "file": dict(fst),
            "real_err": real.get("_err") if isinstance(real, dict) else None,
            "as_of": now.strftime("%Y-%m-%d")}


# ══ ② 市場狀態 ══════════════════════════════════════════════════════

def _read_csv_chunked(path, **kw):
    """
    分批讀 csv，**每批之間 `time.sleep(0)` 讓出 GIL**。
    ⛔ 一次 `read_csv` 讀 39 MB ＝ 這條執行緒整段抱著 GIL ⇒ 4Hz 的停損迴圈跟著停。
    """
    parts = []
    for ch in pd.read_csv(path, chunksize=CSV_CHUNK, **kw):
        parts.append(ch)
        time.sleep(0)
    if not parts:
        return None
    return pd.concat(parts, ignore_index=True)


def _sessions(px):
    """
    把 1 分 K 切成「一晚」與「一個日盤」。
      夜盤：15:00 ~ 隔天 05:00（⛔ 跨午夜 ⇒ 用 `ts − 6 小時` 的日期當「那一晚」E）
      日盤：08:45 ~ 13:45
    ⛔ 一場少於 `*_MIN_BARS` 根就丟掉（資料有洞 ⇒ 不算，跟 sim_lanes 同一條規矩）。
    """
    t = px["ts"]
    mi = t.dt.hour * 60 + t.dt.minute
    nm = (mi >= 15 * 60) | (mi <= 5 * 60)
    dm = (mi >= 8 * 60 + 45) & (mi <= 13 * 60 + 45)

    n = px.loc[nm].copy()
    n["E"] = (t.loc[nm] - pd.Timedelta(hours=6)).dt.date
    g = n.groupby("E").agg(o=("Open", "first"), h=("High", "max"),
                           l=("Low", "min"), c=("Close", "last"), k=("ts", "count"))
    g = g[g["k"] >= NIGHT_MIN_BARS]
    g["amp"] = (g["h"] - g["l"]) / g["o"] * 100.0
    g["ret"] = (g["c"] - g["o"]) / g["o"] * 100.0

    d = px.loc[dm].copy()
    d["D"] = t.loc[dm].dt.date
    gd = d.groupby("D").agg(o=("Open", "first"), h=("High", "max"), l=("Low", "min"),
                            c=("Close", "last"), k=("ts", "count"))
    gd = gd[gd["k"] >= DAY_MIN_BARS]
    gd["ret"] = (gd["c"] - gd["o"]) / gd["o"] * 100.0
    gd["amp"] = (gd["h"] - gd["l"]) / gd["o"] * 100.0
    return g, gd


# ⭐ 2026-09-30（Benson：「這四個我看了也不知道代表甚麼」）⇒ 每張卡一句「跟我們兩條真單的關係」。
#    ⛔ 只寫研究量到的關係（附數字與出處），⛔ 不寫現在偏高偏低、⛔ 不給建議（這一頁的鐵律）。
# ⭐ 同日下午（Benson：「希望敘述再白話一點，一眼就看出是甚麼狀況」）⇒ 改短、改口語。
CARD_WHY = {
    "night_vol": "為什麼要看：夜盤跟勢靠「晃很大的晚上」賺錢（那種晚上每筆約 +121 點），安靜的晚上它多半小賠。",
    "day_vol": "為什麼要看：多方聯軍是日盤策略，日盤晃越大它越好賺。研究的分界：1.03% 以下算安靜、1.23% 以上算熱鬧。",
    "day_night": "為什麼要看：多方聯軍只在日盤做多，日盤一直跌它就難賺；漲幅都跑到夜盤時，對夜盤跟勢比較有利。",
    "us_sox": "為什麼要看：夜盤跟勢的前提是「美股開盤會帶著台指夜盤走」。這個數字 1＝完全同步、0＝各走各的；"
              "連續 60 個交易日都低於 0.05 會亮「要注意」。",
}


def _now_line(c):
    """
    每張卡最上面那一句「現在怎樣」—— ⛔ 只把卡上已經有的數字翻成白話（位置、正負、有沒有連動），
    ⛔ 不寫預測、⛔ 不給建議、⛔ 不下「盤整期」判斷（那個只在週報）。
    """
    k, v, p = c.get("key"), c.get("value"), c.get("pct")
    if v is None:
        return None
    if k in ("night_vol", "day_vol"):
        who = "夜盤" if k == "night_vol" else "日盤"
        unit = "晚上" if k == "night_vol" else "日子"
        if p is None:
            return None
        if p <= 20:
            head = "%s最近很安靜" % who
        elif p <= 40:
            head = "%s最近比平常安靜" % who
        elif p < 60:
            head = "%s最近晃動普通" % who
        elif p < 80:
            head = "%s最近比平常熱鬧" % who
        else:
            head = "%s最近很熱鬧" % who
        return "%s：過去一年有 %d%% 的%s晃得比現在大。" % (head, 100 - p, unit)
    if k == "day_night":
        n, d = c.get("n_sum"), c.get("d_sum")
        if n is None or d is None:
            return None
        if n >= 0 and d < 0:
            head = "最近 60 天漲的都在夜盤、日盤反而在跌"
        elif n < 0 and d >= 0:
            head = "最近 60 天漲的都在日盤、夜盤反而在跌"
        elif n >= 0 and d >= 0:
            head = "最近 60 天日盤、夜盤都在漲，%s漲比較多" % ("夜盤" if n >= d else "日盤")
        else:
            head = "最近 60 天日盤、夜盤都在跌，%s跌比較多" % ("日盤" if d <= n else "夜盤")
        return "%s（夜盤 %s、日盤 %s）。" % (head, _fmt_pct(n, 1), _fmt_pct(d, 1))
    if k == "us_sox":
        if v < 0.05:
            head = "美股開盤和台指夜盤最近幾乎沒在連動，各走各的"
        elif v < 0.2:
            head = "美股開盤和台指夜盤最近只有一點點連動"
        elif v < 0.4:
            head = "美股開盤和台指夜盤最近有一些連動"
        else:
            head = "美股開盤和台指夜盤最近連動明顯"
        run = c.get("low_days") or 0
        tail = ("；已經連續 %d 個交易日低於 0.05（滿 %d 會亮「要注意」）" % (run, COR_LOW_RUN)) if run else ""
        return head + tail + "。"
    return None


def _soxx_panel_files():
    try:
        return sorted(SOXX_PANEL_DIR.glob("SOXX-sip-*.csv"))
    except Exception:
        return []


def _soxx_frame(path, extra=()):
    """研究那份（ts_utc）＋面板月檔（start_utc，…Z）合併 ⇒ DataFrame(ts_utc, open, close)；都讀不到 ⇒ None。"""
    parts = []
    if path is not None and Path(path).exists():
        s = _read_csv_chunked(path, parse_dates=["ts_utc"])
        if s is not None and len(s):
            parts.append(s[["ts_utc", "open", "close"]])
    for f in extra:
        try:
            m = pd.read_csv(f)
            m["ts_utc"] = pd.to_datetime(m["start_utc"], utc=True).dt.tz_localize(None)
            parts.append(m[["ts_utc", "open", "close"]])
        except Exception:
            continue
    if not parts:
        return None
    s = pd.concat(parts, ignore_index=True).drop_duplicates("ts_utc", keep="last")
    return s.sort_values("ts_utc").reset_index(drop=True)


def _us_open_bars(path, extra=()):
    """
    SOXX **美東 9:30 那根 5 分 K** 的 open→close 走幅%（⇒ 台北時間 21:30 或 22:30）。
    ⚠️ 檔頭是 `ts_utc` ⇒ 9:30 ET ＝ UTC 13:30（夏令）或 14:30（冬令）。
       ⛔ 不自己算夏令時間表：同一天**兩根都看**，有 13:30 就是夏令（那天的 14:30 是盤中）。
    """
    s = _soxx_frame(path, extra)
    if s is None or not len(s):
        return None
    mi = s["ts_utc"].dt.hour * 60 + s["ts_utc"].dt.minute
    pick = {}
    for want, tag in ((14 * 60 + 30, "EST"), (13 * 60 + 30, "EDT")):   # 夏令後蓋、優先
        sub = s.loc[mi == want]
        for _i, r in sub.iterrows():
            if r["open"] and r["open"] > 0:
                pick[r["ts_utc"].date()] = (r["ts_utc"] + pd.Timedelta(hours=8),
                                            (r["close"] - r["open"]) / r["open"] * 100.0, tag)
        time.sleep(0)
    if not pick:
        return None
    rows = [{"tw": v[0], "sret": v[1], "tz": v[2]} for v in pick.values()]
    return pd.DataFrame(rows).sort_values("tw").reset_index(drop=True)


def _roll_mean(a, w):
    a = np.asarray(a, dtype=float)
    if len(a) < w:
        return np.array([])
    c = np.cumsum(np.insert(a, 0, 0.0))
    return (c[w:] - c[:-w]) / w


def _roll_sum(a, w):
    m = _roll_mean(a, w)
    return m * w if len(m) else m


def _roll_corr(x, y, w):
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    out = []
    for i in range(w, len(x) + 1):
        a, b = x[i - w:i], y[i - w:i]
        if a.std() == 0 or b.std() == 0:
            out.append(np.nan)
        else:
            out.append(float(np.corrcoef(a, b)[0, 1]))
    return np.asarray(out, dtype=float)


def _card(key, title, note, value, unit, series, dp=2, lines=(), as_of=None,
          flag=None, flag_word=None, flag_note=None):
    """一張市場狀態小卡。位置條的母體 ＝ `series` 的最後 `YEAR_N` 個值（＝過去一年）。"""
    pool = np.asarray(series[-YEAR_N:], dtype=float)
    pool = pool[np.isfinite(pool)]
    return {"key": key, "title": title, "note": note,
            "value": None if value is None else round(float(value), dp),
            "unit": unit, "dp": dp,
            "lo": round(float(pool.min()), dp) if len(pool) else None,
            "hi": round(float(pool.max()), dp) if len(pool) else None,
            "pct": _pct_rank(value, pool),
            "n_pool": int(len(pool)),
            "series": [round(float(v), dp) for v in pool[-60:]],
            "lines": list(lines), "as_of": as_of,
            "flag": flag, "flag_word": flag_word, "flag_note": flag_note}


def _fmt_pct(v, dp=2):
    return ("%+." + str(dp) + "f%%") % v


def market():
    """
    市場狀態三張卡。⛔ **這一支很重**（讀兩個大 csv）—— ⛔ 只准在背景執行緒裡呼叫。
    ⛔ 唯讀：兩個來源檔都只 read。
    """
    px = _read_csv_chunked(MIN1_CSV, parse_dates=["ts"])
    if px is None or not len(px):
        raise RuntimeError("讀不到 tmf_1min.csv")
    g, gd = _sessions(px)
    del px
    time.sleep(0)

    cards = []
    # ── ① 夜盤波動度：最近 20 晚的夜盤振幅% 平均
    amp = g["amp"].to_numpy(dtype=float)
    roll = _roll_mean(amp, VOL_WIN)
    cur = float(roll[-1]) if len(roll) else None
    ref = float(np.median(amp[-VOL_REF:])) if len(amp) else None
    cards.append(_card(
        "night_vol", "夜盤波動度",
        "每晚夜盤從最低到最高差幾 %%，取最近 %d 晚平均" % VOL_WIN,
        cur, "%", roll, dp=2,
        lines=(["平常（過去 %d 晚的中位數）是 %.2f%%" % (VOL_REF, ref)] if ref is not None else []),
        as_of=str(g.index[-1]) if len(g) else None))

    # ── ①b 日盤波動度：最近 20 天的日盤振幅% 平均（多方聯軍靠的是日盤的波動）
    damp = gd["amp"].to_numpy(dtype=float)
    droll = _roll_mean(damp, VOL_WIN)
    dcur = float(droll[-1]) if len(droll) else None
    dref = float(np.median(damp[-VOL_REF:])) if len(damp) else None
    cards.append(_card(
        "day_vol", "日盤波動度",
        "每天日盤從最低到最高差幾 %%，取最近 %d 天平均" % VOL_WIN,
        dcur, "%", droll, dp=2,
        lines=(["平常（過去 %d 天的中位數）是 %.2f%%" % (VOL_REF, dref)] if dref is not None else []),
        as_of=str(gd.index[-1]) if len(gd) else None))

    # ── ② 日盤／夜盤 漲幅：最近 60 天各自的合計漲幅%
    #    ⚠️ 兩條各自對齊自己的最後 N 個交易日（⛔ 不硬把夜盤跟日盤配成一天 ——
    #       那需要一張國定假日表，這裡沒有）。位置條看的是「夜盤合計 − 日盤合計」。
    nr = g["ret"].to_numpy(dtype=float)
    dr = gd["ret"].to_numpy(dtype=float)
    ns = _roll_sum(nr, RET_WIN)
    ds = _roll_sum(dr, RET_WIN)
    m = min(len(ns), len(ds))
    diff = (ns[-m:] - ds[-m:]) if m else np.array([])
    n_sum = float(ns[-1]) if len(ns) else None
    d_sum = float(ds[-1]) if len(ds) else None
    cards.append(_card(
        "day_night", "日盤／夜盤 漲幅",
        "最近 %d 天夜盤、日盤各自加起來漲跌多少；大數字＝夜盤減日盤" % RET_WIN,
        (n_sum - d_sum) if (n_sum is not None and d_sum is not None) else None,
        "%點", diff, dp=1,
        lines=([("夜盤合計 " + _fmt_pct(n_sum)) if n_sum is not None else "",
                ("日盤合計 " + _fmt_pct(d_sum)) if d_sum is not None else ""]),
        as_of=str(max(g.index[-1], gd.index[-1])) if (len(g) and len(gd)) else None))
    cards[-1]["n_sum"], cards[-1]["d_sum"] = n_sum, d_sum

    # ── ③ 美股半導體 vs 台指夜盤：滾動 60 晚相關係數
    #    SOXX 美東 9:30 那根 5 分 K 的走幅 vs 那一晚台指夜盤（開→收）的走幅。
    so = None
    try:
        so = _us_open_bars(SOXX_CSV, _soxx_panel_files())
    except Exception as e:
        so = None
        so_err = str(e)[:120]
    else:
        so_err = None
    if so is None or not len(so):
        cards.append({"key": "us_sox", "title": "美股半導體 vs 台指夜盤",
                      "note": "滾動 %d 晚相關係數" % COR_WIN,
                      "value": None, "unit": "", "dp": 2, "lo": None, "hi": None,
                      "pct": None, "n_pool": 0, "series": [],
                      "lines": ["讀不到 tick-research/us/soxx_5m_alpaca.csv" + (
                          "：" + so_err if so_err else "")],
                      "as_of": None, "flag": None, "flag_word": None, "flag_note": None})
    else:
        xs, ys, ds2 = [], [], []
        gmap = g["ret"].to_dict()
        for _i, r in so.iterrows():
            E = (r["tw"] - pd.Timedelta(hours=6)).date()
            if E in gmap:
                xs.append(float(r["sret"]))
                ys.append(float(gmap[E]))
                ds2.append(E)
        cor = _roll_corr(xs, ys, COR_WIN)
        cur = float(cor[-1]) if len(cor) and math.isfinite(cor[-1]) else None
        # ⭐ PM 2026-09-23 定的規則：連續 COR_LOW_RUN 個交易日都低於 COR_LOW ⇒ 標「要注意」。
        tail = cor[-COR_LOW_RUN:]
        low_run = bool(len(tail) == COR_LOW_RUN and np.all(np.isfinite(tail))
                       and np.all(tail < COR_LOW))
        cards.append(_card(
            "us_sox", "美股半導體 vs 台指夜盤",
            "美股半導體（SOXX）開盤頭 5 分鐘的漲跌，跟當晚台指夜盤漲跌有多同步；看最近 %d 晚" % COR_WIN,
            cur, "", cor, dp=2,
            lines=["配得起來的晚上 %d 場" % len(xs)],
            as_of=str(ds2[-1]) if ds2 else None,
            flag=("wn" if low_run else None),
            flag_word=("要注意" if low_run else None),
            flag_note=("連續 %d 個交易日的滾動相關係數都低於 %.2f" % (COR_LOW_RUN, COR_LOW)
                       if low_run else None)))
        run = 0
        for x in cor[::-1]:
            if not (math.isfinite(x) and x < COR_LOW):
                break
            run += 1
        cards[-1]["low_days"] = run
    for c in cards:
        c["why"] = CARD_WHY.get(c.get("key"))
        c["now"] = _now_line(c)
    return {"market": cards,
            "market_note": "數字來源是歷史資料，不代表明天會怎樣。這一頁不下任何判斷、不給任何建議。"}



# ══ 快取（⛔ 重活只在背景執行緒，HTTP 執行緒只拿算好的）══════════════

def _mkt_key():
    return (_sig(MIN1_CSV), _sig(SOXX_CSV), tuple(_sig(p) for p in _soxx_panel_files()))


def _mkt_worker(key):
    try:
        data = market()
        err = None
    except Exception as e:
        data, err = None, str(e)[:160]
        print("⚠️ [健檢] 市場狀態算不出來：%s" % err, flush=True)
    with _LOCK:
        _MKT.update({"key": key, "data": data, "err": err, "busy": False,
                     "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})


def _ensure_market():
    """
    ⇒ (算好的那一份 或 None, 還在算嗎, 錯誤)。
    ⛔ **不會在呼叫端的執行緒裡算** —— 沒有就開一條背景執行緒，這次先回「還在算」。
    """
    key = _mkt_key()
    with _LOCK:
        if _MKT["key"] == key and (_MKT["data"] is not None or _MKT["err"]):
            return _MKT["data"], False, _MKT["err"]
        if _MKT["busy"]:
            return None, True, None
        _MKT["busy"] = True
    threading.Thread(target=_mkt_worker, args=(key,), daemon=True,
                     name="health-market").start()
    return None, True, None


def state(now=None):
    """
    GET /api/health/state 的內容。⛔ 唯讀、⛔ 零寫入、⛔ 不碰 state_lock／部位。
    ⚠️ 市場狀態那一區第一次會回 `market_ready=False`（背景在算）—— 前端隔幾秒再問一次，
       ⛔ 不要把它掛進 5 秒輪詢。
    """
    now = now or datetime.now()
    out = {"ok": True}
    out.update(strategies(now))
    mk, busy, err = _ensure_market()
    out["market_ready"] = mk is not None
    out["market_busy"] = busy
    out["market_err"] = err
    out["market_at"] = _MKT.get("at")
    if mk:
        out.update(mk)
    else:
        out["market"] = []
        out["market_note"] = ("市場狀態還在算（第一次要讀兩個大檔，約幾秒）。"
                              if busy else (err or "市場狀態算不出來"))
    out["need"] = NEED
    out["lamp_words"] = dict(LAMP_WORD)
    out["lamp_notes"] = dict(LAMP_NOTE)
    return out
