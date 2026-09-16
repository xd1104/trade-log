# -*- coding: utf-8 -*-
"""
【策略實驗室】的後端：歷史逐筆回測（唯讀）＋收盤後補抓當天逐筆。

⛔⛔ 這個模組**只算歷史**，不下單、不碰部位、不寫任何交易紀錄：
   ・一行都不 import broker／auto_fire，不打 /api/enter、/api/real/*、/api/fire/*。
   ・只寫 tick_hist/（歷史行情、每日摘要、快取；已 gitignore）。
   ・「有沒有部位」由面板注入一個唯讀的 callable（has_position），這裡不知道它怎麼問的。
⛔ 回測**不准跑在主迴圈或 shioaji 回呼執行緒**：只由 HTTP handler 執行緒呼叫 run_exclusive()，
   同時只准一個查詢在算（第二個丟 Busy ⇒ 面板回 429）。
⛔ 每日抓取跑在自己的背景執行緒（fetch_loop），任何例外都在這裡吞掉並記錄，不往外丟。
⛔ 用面板**現有的** api 連線（SESSION_REF["api"]），⛔ 絕不自己 login（會把面板踢下線）。

算法口徑＝ tick-research/scripts/hypotheses.py 的 run_bracket（實際口徑）：
  進場：做多用當時賣價、做空用當時買價（0 就用成交價）；cost=0 時用成交價
  停利：剛好 +TP；停損：碰到那一筆的成交價（會跳價多賠）
  沒碰到：13:43:30（含）前最後一筆的買價（多）／賣價（空）出場；結算日 13:30
  cost=1：每筆扣手續費 5 點
方向：bar5＝跟進場時刻所在 5 分 K 的第一筆比（filters.py 的 A）；open＝跟 08:45 第一筆比；
      long／short＝一律做多／做空。

資料：
  tick_hist/ticks/YYYY-MM-DD.csv.gz   永豐 api.ticks 原始欄位（日盤 08:45~13:45）
  tick_hist/cache/YYYY-MM-DD.npz      逐筆精簡快取（t 毫秒、p、bid、ask；帶來源檔的 mtime/size）
  tick_hist/days.jsonl                一天一列（昨日日盤收盤、08:45 開盤、夜盤高低、結算日、星期…）
"""
import json
import math
import os
import re
import threading
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
LAB_DIR = HERE / "tick_hist"          # ⚠️ 測試會整個導到暫存區；函式一律在呼叫時才讀這個常數
MIN1_CSV = HERE / "tmf_1min.csv"      # 夜盤高低與「真正的前一個交易日」從這裡推（唯讀）

VALID_FROM = "2026-01-01"             # 驗證期起點（之前＝設計期）
FEE = 5.0
Z = 2.80
RATE_MIN_N = 30                       # 少於這麼多天不給勝率
TOP_K = 5                             # 「拿掉最賺的 5 天」

T_MIN_SEC = 8 * 3600 + 46 * 60        # 進場時間白名單 08:46:00 ~ 13:00:00
T_MAX_SEC = 13 * 3600
PTS_MIN, PTS_MAX = 1, 2000
DIRS = ("bar5", "open", "long", "short")
PARAM_KEYS = ("t", "dir", "tp", "sl", "gap", "nr", "skip", "noexp", "cost")


def ms(hh, mm, ss=0):
    return (hh * 3600 + mm * 60 + ss) * 1000


T0845, T1330, T1343_30, T1345 = ms(8, 45), ms(13, 30), ms(13, 43, 30), ms(13, 45)


def _ticks_dir():
    return LAB_DIR / "ticks"


def _cache_dir():
    return LAB_DIR / "cache"


def _days_file():
    return LAB_DIR / "days.jsonl"


def _days_side():
    return LAB_DIR / "days_meta.json"


def log(msg):
    try:
        print("[策略實驗室] " + str(msg), flush=True)
    except Exception:
        pass


# ══ 日曆 ══════════════════════════════════════════════════════════════

# ⭐⭐ 結算日順延最多幾個日曆天（2026-09-16 加）。
#    農曆年會把結算日往後推：**實例 2023-01-30**（第三個週三 01-18 落在年假裡，
#    台股 01-17 封關、01-30 才開紅盤 ⇒ 順延 12 天）。
#    ⇒ 上限取 14（12 天 ＋ 2 天餘裕）。超過就是**行事曆有洞**，不是連假 ⇒ 退回「就是第三個週三」。
EXPIRY_MAX_POSTPONE = 14


def _wed3(d):
    """那個月的第三個週三（永遠落在 15~21 號）。"""
    first = date(d.year, d.month, 1)
    return first + timedelta(days=(2 - first.weekday()) % 7 + 14)


def is_expiry(d, cal=None):
    """
    結算日（**13:30 收盤**，不是 13:45）。

    ⛔ 規則：**每月第三個週三；那天休市就順延到下一個有交易的日子。**
       （期交所的規則；`hypotheses.is_expiry` 只寫了前半段。）

    `cal`＝交易日的集合（字串 `YYYY-MM-DD`）。
      ・**不給** ⇒ 只用「第三個週三」那半（＝ 2026-09-16 以前的行為，⛔ 一個字都沒變）
      ・**給了** ⇒ `d` 是結算日 ⟺ `d >= 第三個週三` 而且 **[第三個週三, d) 之間沒有任何交易日**
        ⚠️ `d` 本身不必在 `cal` 裡（今天還沒進行事曆，但今天開著盤就是交易日）。

    ⭐ 這條規則是**量出來的**（2026-09-16，lab-dev 用 `tick_hist` 520 天
       〔2024-07-29~2026-09-15〕逐日看「日盤最後一筆是幾點」）：
         ・27 天的最後一筆 < 13:40，其中 **25 天是真的結算日**（最後一筆 13:29），
           另外 2 天（2025-11-24 停在 10:00／2025-12-08 停在 10:20）是資料有洞、不是結算日
         ・舊的「第三個週三」抓到 24 天、**漏掉 2026-02-23**（第三個週三 02-18 落在農曆年假）
         ・這條新規則對那 25 天 **全中、0 漏、0 誤抓**
       ⛔ 要改這條規則的人先把上面那組數字重量一次。

    ⛔ 行事曆有洞時**不猜**：`d` 跟第三個週三差超過 `EXPIRY_MAX_POSTPONE` 天 ⇒
       退回「就是第三個週三」（＝舊行為），⛔ 不把一個離很遠的日子標成結算日。
    """
    w = _wed3(d)
    if cal is None:
        return d == w
    if d < w:
        return False
    if (d - w).days > EXPIRY_MAX_POSTPONE:
        return d == w
    k = w
    while k < d:
        if str(k) in cal:
            return False          # 第三個週三到 d 之間還有交易日 ⇒ 結算日是那一天，不是 d
        k += timedelta(days=1)
    if d != w and (not cal or min(cal) > str(w)):
        # ⛔⛔ **行事曆的起點比第三個週三還晚** ⇒ 中間有沒有交易日我們根本不知道
        #    （⛔ 「查不到」不是「沒有」）。實例：`tick_hist` 的第一天 2024-07-29，
        #    那個月的第三個週三是 07-17 —— 不擋的話那一天會被標成結算日（實測抓到）。
        #    ⇒ 退回「就是第三個週三」，⛔ 不猜。
        # ⚠️ **只擋左邊界**：行事曆過期（max 停在幾天前）時仍然會判成結算日 ——
        #    那個方向是安全的（早 15 分鐘平掉），而且順延上限已經把它框住了。
        return d == w
    return True


_CAL = {"key": None, "set": frozenset()}


def trading_days():
    """
    交易日的集合（`days.jsonl` 裡每一天＝真的有日盤成交的日子）。
    ⚠️ 只到**昨天**（今天那一份要收盤後才抓得到）—— `is_expiry(d, cal)` 問的是
       「第三個週三到 d **之前**有沒有交易日」，所以缺今天不影響。
    ⛔ 讀不到就回空集合 ⇒ 呼叫端退回「第三個週三」那半（安全的那一邊）。
    """
    f = _days_file()
    try:
        s = f.stat()
        key = (str(f), s.st_mtime_ns, s.st_size)
    except OSError:
        _CAL.update(key=None, set=frozenset())
        return _CAL["set"]
    if _CAL["key"] == key:
        return _CAL["set"]
    _CAL.update(key=key, set=frozenset(r["date"] for r in days() if r.get("date")))
    return _CAL["set"]


_EXP = {"key": None, "map": {}, "at": 0.0, "cal": frozenset()}
EXP_RECHECK_S = 5.0        # 多久回頭確認一次行事曆有沒有被重建


def is_expiry_cal(d):
    """
    帶行事曆的結算日判斷（回測與面板都走這一支）。
    ⛔ 行事曆讀不到（空集合）⇒ 退回「第三個週三」，⛔ 不猜。

    ⚠️⚠️ **一次回測查詢會叫 520 次**（一天一次，在 `_day_pts` 的熱路徑上）⇒
       結果記在 `_EXP["map"]` 裡，而且**命中時連 `stat()` 都不做**
       （每次都 stat 一下實測會讓一次查詢多 0.15 秒；那條線的上限是 3 秒）。
       行事曆有沒有換，每 `EXP_RECHECK_S` 秒回頭確認一次就夠了 ——
       `days.jsonl` 一天只重建一次（收盤後補抓那一段）。
    """
    now = time.time()
    if _EXP["at"] == 0.0 or now - _EXP["at"] > EXP_RECHECK_S:
        _EXP["at"] = now
        cal = trading_days()                      # ⚠️ 這一行就是那個 stat（每 5 秒才一次）
        if _EXP["key"] != _CAL["key"]:
            _EXP.update(key=_CAL["key"], map={}, cal=cal)
        else:
            _EXP["cal"] = cal
    m = _EXP["map"]
    if d not in m:
        m[d] = is_expiry(d, _EXP["cal"] or None)
    return m[d]


def expiry_week(d):
    """
    結算週＝第三個週三所在那一整週（週一～週五），跟 lab-ux 定案 demo 的 expiryWeek() 一致。
    ⚠️ 刻意跟 tick-research/scripts/filters.py 的 expiry_week（只算週一～週三）**不同**：
       畫面上寫的是「每月第三個週三那一週」，一週就是週一到週五；filters.py 是研究時的舊口徑。
    第三個週三永遠落在 15～21 號 ⇒ 那一週的週一～週五一定在同一個月（週一最早 13 號、週五最晚 23 號）。
    """
    if d.weekday() > 4:
        return False
    first = date(d.year, d.month, 1)
    wed3 = first + timedelta(days=(2 - first.weekday()) % 7 + 14)
    mon = wed3 - timedelta(days=2)
    return mon <= d <= mon + timedelta(days=4)


# ══ 逐筆讀取與快取 ═══════════════════════════════════════════════════════

def load_csv_day(f):
    """照 hypotheses.load_day：排序、只留 08:45 <= t < 13:45"""
    df = pd.read_csv(f, compression="gzip")
    if df.empty:
        return None
    ts = pd.to_datetime(df["ts"])
    t = ((ts.dt.hour * 3600 + ts.dt.minute * 60 + ts.dt.second) * 1000
         + ts.dt.microsecond // 1000).to_numpy(np.int64)
    order = np.argsort(t, kind="stable")
    t = t[order]
    p = df["close"].to_numpy(float)[order]
    b = df["bid_price"].to_numpy(float)[order]
    a = df["ask_price"].to_numpy(float)[order]
    keep = (t >= T0845) & (t < T1345)
    if not keep.any():
        return None
    return {"t": t[keep], "p": p[keep], "bid": b[keep], "ask": a[keep]}


def _stamp(f):
    s = f.stat()
    return np.array([s.st_mtime_ns, s.st_size], dtype=np.int64)


_NOQ = -32768     # 買價／賣價是 0（沒有報價）時的記號


def _encode(D, stamp):
    """
    精簡格式（查詢時的時間幾乎全花在解壓與 npz 成員的表頭解析 ⇒ 成員越少、位元組越少越快）：
      h：int64 [格式, 來源 mtime_ns, 來源 size, 第一筆成交價]
      t：int32 毫秒
      q：int16 (3, n) ＝ [成交價跟前一筆的差, 成交價−買價, 賣價−成交價]（買／賣價 0 記成 -32768）
    ⚠️ 價格不是整數、或差值超出 int16 ⇒ 格式 0 存原值，⛔ 不准四捨五入硬塞。
    """
    t = D["t"].astype(np.int32)
    p, b, a = D["p"], D["bid"], D["ask"]
    ints = all(np.all(np.round(x) == x) for x in (p, b, a))
    if ints and len(p):
        pi, bi, ai = p.astype(np.int64), b.astype(np.int64), a.astype(np.int64)
        dp = np.diff(pi, prepend=pi[0])
        db = np.where(bi == 0, _NOQ, pi - bi)
        da = np.where(ai == 0, _NOQ, ai - pi)

        def fits(x, sentinel):
            return bool(np.all(((x > -32768) & (x < 32768)) | sentinel))

        if fits(dp, False) and fits(db, bi == 0) and fits(da, ai == 0):
            return {"h": np.array([1, stamp[0], stamp[1], pi[0]], np.int64), "t": t,
                    "q": np.stack([dp, db, da]).astype(np.int16)}
    return {"h": np.array([0, stamp[0], stamp[1], 0], np.int64), "t": t,
            "r": np.stack([p, b, a]).astype(np.float64)}


def _decode(z, h):
    t = z["t"]
    if h[0] == 1:
        q = z["q"].astype(np.int32)
        p = np.cumsum(q[0], dtype=np.int64)
        p += h[3]
        b = np.where(q[1] == _NOQ, 0, p - q[1])
        a = np.where(q[2] == _NOQ, 0, p + q[2])
        return {"t": t, "p": p, "bid": b, "ask": a}
    r = z["r"]
    return {"t": t, "p": r[0], "bid": r[1], "ask": r[2]}


def build_cache(d):
    """從 csv.gz 建一天的快取（原子寫入）。回傳 dict 或 None（那天沒資料）"""
    src = _ticks_dir() / f"{d}.csv.gz"
    stamp = _stamp(src)
    D = load_csv_day(src)
    _cache_dir().mkdir(parents=True, exist_ok=True)
    out = _cache_dir() / f"{d}.npz"
    tmp = out.with_name(out.name + ".tmp.npz")
    if D is None:
        np.savez_compressed(tmp, h=np.array([-1, stamp[0], stamp[1], 0], np.int64))
    else:
        np.savez_compressed(tmp, **_encode(D, stamp))
    os.replace(tmp, out)
    return D


def load_day(d):
    """快取優先；快取不存在或跟來源檔對不上（mtime/size）就從 csv.gz 重建"""
    src = _ticks_dir() / f"{d}.csv.gz"
    try:
        stamp = _stamp(src)
    except OSError:
        return None
    try:
        with np.load(_cache_dir() / f"{d}.npz") as z:
            h = z["h"]
            if h[1] == stamp[0] and h[2] == stamp[1]:
                return None if h[0] < 0 else _decode(z, h)
    except Exception:
        pass
    return build_cache(d)


# ══ 每日摘要 days.jsonl ═══════════════════════════════════════════════════

_DAYS = {"key": None, "rows": []}
_DAYS_LOCK = threading.Lock()        # ⛔ 只保護摘要的讀寫，絕對不碰面板的 state_lock


def _tick_dates():
    rx = re.compile(r"^(\d{4}-\d{2}-\d{2})\.csv\.gz$")
    out = []
    d = _ticks_dir()
    if d.exists():
        for f in d.iterdir():
            m = rx.match(f.name)
            if m:
                out.append(m.group(1))
    return sorted(out)


def _read_min1():
    """tmf_1min.csv → (日盤收盤 {date: close}, 夜盤 bars DataFrame)。檔案不在就回 ({}, None)"""
    if not MIN1_CSV.exists():
        return {}, None
    px = pd.read_csv(MIN1_CSV, usecols=["ts", "High", "Low", "Close"])
    px["ts"] = pd.to_datetime(px["ts"])
    tm = px["ts"].dt.hour * 60 + px["ts"].dt.minute
    day = px[(tm >= 8 * 60 + 46) & (tm <= 13 * 60 + 45)]
    closes = day.groupby(day["ts"].dt.date)["Close"].last().to_dict()
    night = px[(tm >= 15 * 60) | (tm <= 5 * 60 + 1)].copy()
    return {str(k): float(v) for k, v in closes.items()}, night


def _nights(calendar, night):
    """照 frameworks.night_sessions 的歸屬：>=15:00 的 K 棒歸到「隔天起第一個交易日」、
    <=05:01 的歸到「當天起第一個交易日」。日曆＝逐筆日 ∪ 1 分 K 的日盤日（不會把缺資料那幾天的夜盤疊在一起）。
    回傳 {date: (hi, lo)}"""
    if night is None or night.empty or not calendar:
        return {}
    tds = np.array([np.datetime64(d) for d in calendar])
    dd = night["ts"].dt.normalize().to_numpy().astype("datetime64[D]")
    late = (night["ts"].dt.hour >= 15).to_numpy()
    key = np.where(late, dd + np.timedelta64(1, "D"), dd)
    idx = np.searchsorted(tds, key, side="left")
    ok = idx < len(tds)
    g = night[ok].assign(tday=tds[idx[ok]])
    out = {}
    for tday, x in g.groupby("tday"):
        out[str(pd.Timestamp(tday).date())] = (float(x["High"].max()), float(x["Low"].min()))
    return out


def day_close(D, dt):
    """日盤收盤＝13:45 前最後一筆成交（結算日 13:30 前）。⛔ 只從逐筆取，不靠 tmf_1min.csv"""
    # ⭐ 2026-09-16：結算日改走帶行事曆的那一支（農曆年會把結算日往後移）
    cut = T1330 if is_expiry_cal(dt) else T1345
    k = int(np.searchsorted(D["t"], cut, side="left")) - 1
    return float(D["p"][max(k, 0)])


PREV_MAX_GAP = 4          # 前一個有逐筆的交易日跟今天差超過這麼多日曆天 ⇒ 要能證明中間是連假才算數


def prev_close_date(d, prev, csv_dates):
    """
    「昨收」要用哪一天的日盤收盤。回傳日期字串或 None（＝沒有昨收 ⇒ 開跳空條件時那天被過濾掉）。
      ・prev＝tick_hist 裡前一個有資料的日子；⛔ 昨收一律從 tick_hist 取，不拿 tmf_1min.csv 的收盤頂
        （那個檔可能停更好幾天，拿它會算出錯的跳空而且看不出來）。
      ・1 分 K 顯示 prev 跟今天之間**有別的交易日** ⇒ 缺了一天 ⇒ None
      ・差 ≤ 4 個日曆天（週末、單日假期）⇒ 算數
      ・差 > 4 天：只有 1 分 K 檔涵蓋到今天的前一天、而且中間一天交易日都沒有（＝證明是連假）才算數；
        檔案沒涵蓋（停更）⇒ 無法確定 ⇒ None
    """
    if prev is None:
        return None
    between = [x for x in csv_dates if prev < x < d]
    if between:
        return None
    gap = (date.fromisoformat(d) - date.fromisoformat(prev)).days
    if gap <= PREV_MAX_GAP:
        return prev
    if csv_dates and csv_dates[-1] >= str(date.fromisoformat(d) - timedelta(days=1)):
        return prev
    return None


def _write_days(rows, side):
    LAB_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _days_file().with_name("days.jsonl.tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    os.replace(tmp, _days_file())
    tmp2 = _days_side().with_name("days_meta.json.tmp")
    tmp2.write_text(json.dumps(side, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp2, _days_side())


def refresh_days(force=False):
    """
    補齊 days.jsonl：新的逐筆日只補那一天（建快取、取開盤／收盤）；
    日曆相關欄位（昨收、夜盤）只有在「有新的一天、1 分 K 檔變了、或 force」才重算。
    ⛔ 由背景執行緒或回測查詢（拿著 run 鎖）呼叫，不在主迴圈上。
    """
    with _DAYS_LOCK:
        old = {}
        if _days_file().exists() and not force:
            for ln in _days_file().read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(ln)
                    old[r["date"]] = r
                except Exception:
                    pass
        side = {}
        if _days_side().exists():
            try:
                side = json.loads(_days_side().read_text(encoding="utf-8"))
            except Exception:
                side = {}
        dates = _tick_dates()
        csv_key = None
        if MIN1_CSV.exists():
            s = MIN1_CSV.stat()
            csv_key = [s.st_mtime_ns, s.st_size]
        new = [d for d in dates if d not in old]
        gone = [d for d in old if d not in set(dates)]
        stale = [d for d in dates if d in old and old[d].get("src") != _stamp(_ticks_dir() / f"{d}.csv.gz").tolist()]
        if not new and not gone and not stale and not force and side.get("csv") == csv_key:
            return False
        rows = {d: old[d] for d in dates if d in old and d not in stale}
        for d in new + stale:
            D = load_day(d)
            if D is None:
                continue          # 那天沒有日盤成交（檔案是空的）：不列入
            dt = date.fromisoformat(d)
            # ⛔ 這裡的行事曆用「這一批逐筆日」本身（`dates`）——
            #    ⛔ 不可以回頭呼叫 trading_days()：那讀的是 days.jsonl，正是這支要寫的檔。
            rows[d] = {"date": d, "weekday": dt.weekday(),
                       "is_expiry": is_expiry(dt, set(dates)),
                       "expiry_week": expiry_week(dt), "n": int(len(D["t"])),
                       "open": float(D["p"][0]), "close": day_close(D, dt),
                       "src": _stamp(_ticks_dir() / f"{d}.csv.gz").tolist()}
        closes, night = _read_min1()
        tick_days = sorted(rows)
        calendar = sorted(set(tick_days) | set(closes))
        nights = _nights(calendar, night)
        csv_dates = sorted(closes)
        for i, d in enumerate(tick_days):
            r = rows[d]
            prev = tick_days[i - 1] if i > 0 else None
            pc_date = prev_close_date(d, prev, csv_dates)
            r["prev_close"] = rows[pc_date]["close"] if pc_date else None
            r["prev_date"] = pc_date
            ng = nights.get(d)
            r["night_hi"], r["night_lo"] = (ng if ng else (None, None))
            r["night_range"] = None if not ng else round(ng[0] - ng[1], 1)
        out = [rows[d] for d in tick_days]
        _write_days(out, {"csv": csv_key, "built": datetime.now().isoformat(timespec="seconds")})
        _DAYS["key"] = None
        return True


def days():
    """讀 days.jsonl（以 mtime/size 快取）。沒有就回空清單（不在這裡建）"""
    f = _days_file()
    if not f.exists():
        return []
    s = f.stat()
    key = (str(f), s.st_mtime_ns, s.st_size)
    if _DAYS["key"] == key:
        return _DAYS["rows"]
    rows = []
    for ln in f.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(ln))
        except Exception:
            pass
    rows.sort(key=lambda r: r["date"])
    _DAYS.update(key=key, rows=rows)
    return rows


# ══ 參數白名單 ═══════════════════════════════════════════════════════════

class BadParam(ValueError):
    pass


class Busy(RuntimeError):
    pass


def _int_pts(v, name, optional=False):
    if v is None or v == "":
        if optional:
            return None
        raise BadParam(f"{name} 必填")
    if not re.fullmatch(r"\d{1,4}", v):
        raise BadParam(f"{name} 要是 {PTS_MIN}~{PTS_MAX} 的整數")
    n = int(v)
    if not (PTS_MIN <= n <= PTS_MAX):
        raise BadParam(f"{name} 要是 {PTS_MIN}~{PTS_MAX} 的整數")
    return n


def parse_params(qs):
    """qs：{key: [values]}（urllib.parse.parse_qs 的形狀，要 keep_blank_values=True）。不合法丟 BadParam"""
    for k, v in qs.items():
        if k not in PARAM_KEYS:
            raise BadParam(f"不認得的參數：{k[:20]}")
        if len(v) != 1:
            raise BadParam(f"參數重複：{k}")
    g = {k: v[0] for k, v in qs.items()}
    m = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2})", g.get("t") or "")
    if not m or int(m.group(2)) > 59 or int(m.group(3)) > 59:
        raise BadParam("t 要是 HH:MM:SS")
    tsec = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    if not (T_MIN_SEC <= tsec <= T_MAX_SEC):
        raise BadParam("t 要在 08:46:00 ~ 13:00:00")
    dr = g.get("dir")
    if dr not in DIRS:
        raise BadParam("dir 要是 bar5／open／long／short")
    skip = []
    sv = g.get("skip") or ""
    if sv:
        for x in sv.split(","):
            if not re.fullmatch(r"[1-5]", x) or int(x) in skip:
                raise BadParam("skip 只收 1~5（週一～週五），逗號分隔、不重複")
            skip.append(int(x))
    flags = {}
    for k, dflt in (("noexp", 0), ("cost", 1)):
        v = g.get(k)
        if v is None or v == "":
            flags[k] = dflt
        elif v in ("0", "1"):
            flags[k] = int(v)
        else:
            raise BadParam(f"{k} 只收 0 或 1")
    return {"t": g["t"], "tsec": tsec, "dir": dr,
            "tp": _int_pts(g.get("tp"), "tp"), "sl": _int_pts(g.get("sl"), "sl"),
            "gap": _int_pts(g.get("gap"), "gap", True), "nr": _int_pts(g.get("nr"), "nr", True),
            "skip": sorted(skip), "noexp": flags["noexp"], "cost": flags["cost"]}


# ══ 回測 ═════════════════════════════════════════════════════════════════

def run_bracket(D, i_entry, fill, d, tp, sl, cutoff, cost=True):
    """＝ hypotheses.run_bracket（cost=False 時收盤出場用成交價）"""
    j0 = i_entry + 1
    j1 = int(np.searchsorted(D["t"], cutoff, side="right"))
    if j0 >= j1:
        return 0.0, "eod"
    p = D["p"][j0:j1]
    tgt, stp = fill + d * tp, fill - d * sl
    hit_tp = (p >= tgt) if d > 0 else (p <= tgt)
    hit_sl = (p <= stp) if d > 0 else (p >= stp)
    i_tp = int(np.argmax(hit_tp)) if hit_tp.any() else None
    i_sl = int(np.argmax(hit_sl)) if hit_sl.any() else None
    if i_tp is None and i_sl is None:
        k = j1 - 1
        if cost:
            out = D["bid"][k] if d > 0 else D["ask"][k]
            out = out if out else D["p"][k]
        else:
            out = D["p"][k]
        return round(float(d * (out - fill)), 1), "eod"
    if i_sl is None or (i_tp is not None and i_tp < i_sl):
        return float(tp), "tp"
    return round(float(d * (p[i_sl] - fill)), 1), "sl"


def sim_day(row, D, P):
    """一天。回傳 (pts, why) 或 (None, 跳過原因)"""
    if D is None:
        return None, "nodata"
    T = P["tsec"] * 1000
    ipx = int(np.searchsorted(D["t"], T, side="right")) - 1
    if ipx < 0:
        return None, "nodir"
    px = D["p"][ipx]
    if P["dir"] == "bar5":
        bar0 = (T // 300000) * 300000
        i0 = int(np.searchsorted(D["t"], bar0, side="left"))
        if i0 >= len(D["t"]) or D["t"][i0] > T:
            return None, "nodir"
        d = 1 if px >= D["p"][i0] else -1
    elif P["dir"] == "open":
        d = 1 if px >= D["p"][0] else -1
    else:
        d = 1 if P["dir"] == "long" else -1
    cost = bool(P["cost"])
    if cost:
        fill = D["ask"][ipx] if d > 0 else D["bid"][ipx]
        fill = fill if fill else px
    else:
        fill = px
    dt = date.fromisoformat(row["date"])
    cutoff = T1330 if is_expiry_cal(dt) else T1343_30
    pts, why = run_bracket(D, ipx, float(fill), d, P["tp"], P["sl"], cutoff, cost)
    return pts - (FEE if cost else 0.0), why


def _skip_reason(row, P):
    if P["gap"] is not None:
        pc = row.get("prev_close")
        if pc is None or abs(row["open"] - pc) < P["gap"]:
            return "gap"
    if P["nr"] is not None:
        nr = row.get("night_range")
        if nr is None or nr < P["nr"]:
            return "night"
    if (row["weekday"] + 1) in P["skip"]:
        return "wd"
    if P["noexp"] and row.get("expiry_week"):
        return "expw"
    return None


def period_stats(rows):
    """rows：[(date, pts|None, why|skip)]"""
    t = [(d, p, w) for d, p, w in rows if p is not None]
    pts = [p for _, p, _ in t]
    n = len(pts)
    skip_by = {}
    for _, p, w in rows:
        if p is None:
            skip_by[w] = skip_by.get(w, 0) + 1
    s = {"days": len(rows), "n": n, "skipped": len(rows) - n, "skip_by": skip_by,
         "from": rows[0][0] if rows else None, "to": rows[-1][0] if rows else None,
         "tp": sum(1 for x in t if x[2] == "tp"), "sl": sum(1 for x in t if x[2] == "sl"),
         "eod": sum(1 for x in t if x[2] == "eod"),
         "rate": None, "avg": None, "total": None, "sd": None, "noise": None, "no5": None,
         "top5": [], "series": [[d, p, w] for d, p, w in rows]}
    if not n:
        return s
    total = sum(pts)
    avg = total / n
    sd = math.sqrt(sum((p - avg) ** 2 for p in pts) / (n - 1)) if n > 1 else None
    order = sorted(range(n), key=lambda i: -pts[i])
    rest = [pts[i] for i in order[TOP_K:]]
    s.update(total=round(total, 2), avg=round(avg, 4),
             rate=round(100.0 * sum(1 for p in pts if p > 0) / n, 2) if n >= RATE_MIN_N else None,
             sd=None if sd is None else round(sd, 3),
             noise=None if sd is None else round(Z * sd / math.sqrt(n), 3),
             no5=round(sum(rest) / len(rest), 4) if rest else None,
             top5=[t[i][0] for i in order[:TOP_K]])
    return s


def run(P):
    """整段回測。⚠️ 呼叫端要自己決定執行緒（面板：HTTP handler 執行緒＋run_exclusive）"""
    t0 = time.perf_counter()
    rows = days()
    if not rows and _tick_dates():
        refresh_days()
        rows = days()
    design, valid = [], []
    for r in rows:
        why = _skip_reason(r, P)
        if why:
            res = (r["date"], None, why)
        else:
            pts, w = sim_day(r, load_day(r["date"]), P)
            res = (r["date"], None if pts is None else round(pts, 1), w)
        (valid if r["date"] >= VALID_FROM else design).append(res)
    return {"ok": True, "valid_from": VALID_FROM,
            "params": {k: P[k] for k in ("t", "dir", "tp", "sl", "gap", "nr", "skip", "noexp", "cost")},
            "design": period_stats(design), "valid": period_stats(valid),
            "data_to": rows[-1]["date"] if rows else None, "n_days": len(rows),
            "elapsed_ms": round((time.perf_counter() - t0) * 1000)}


_RUN_LOCK = threading.Lock()


def run_exclusive(P):
    """同時只准一個查詢在算；第二個丟 Busy（面板回 429「還在算上一組」）"""
    if not _RUN_LOCK.acquire(blocking=False):
        raise Busy("還在算上一組")
    try:
        return run(P)
    finally:
        _RUN_LOCK.release()


def meta():
    rows = days()
    dsg = [r for r in rows if r["date"] < VALID_FROM]
    val = [r for r in rows if r["date"] >= VALID_FROM]
    return {"first": rows[0]["date"] if rows else None, "last": rows[-1]["date"] if rows else None,
            "n_days": len(rows), "valid_from": VALID_FROM, "n_design": len(dsg), "n_valid": len(val),
            "design_to": dsg[-1]["date"] if dsg else None,
            "fetch": {k: FETCH.get(k) for k in ("status", "msg", "at")}}


# ══ 每日補抓（收盤後，背景執行緒） ═════════════════════════════════════════

FETCH_FROM = dtime(13, 50)
FETCH_UNTIL = dtime(15, 0)
FETCH_RETRY_S = 600
USAGE_MAX = 0.85
CONTRACT_CODE = "TMFR1"
USAGE_TIMEOUT_MS = 10000      # ⛔ 明寫逾時：永豐卡住時這條背景執行緒最多等這麼久（毫秒）
TICKS_TIMEOUT_MS = 60000      # 一整天的逐筆約 5 萬筆，給 60 秒
FETCH = {"status": "idle", "msg": "", "at": None, "last_try": None, "done_day": None}


def _set(status, msg, now):
    FETCH.update(status=status, msg=msg, at=now.strftime("%Y-%m-%d %H:%M:%S"))
    return status


def _complete(df, d):
    """抓回來的是不是完整的一天（⛔ 半天的資料不准當成完整的存起來）"""
    ts = pd.to_datetime(df["ts"])
    if ts.empty or ts.max().date() != d or ts.min().date() != d:
        return False
    last = ts.max()
    need = dtime(13, 29) if is_expiry_cal(d) else dtime(13, 44)
    return last.time() >= need and ts.min().time() <= dtime(8, 50)


def fetch_today(api, has_position, now=None, qt=None):
    """
    交易日 13:50~15:00、今天的檔還不存在、沒有部位、流量 ≤ 85% ⇒ 抓今天日盤逐筆存起來。
    回傳狀態字串。⛔ 任何例外都在這裡吞掉（記進 FETCH），絕不往外丟。
    """
    try:
        now = now or datetime.now()
        d = now.date()
        if d.weekday() > 4:
            return _set("not_trading_day", "週末不抓", now)
        if now.time() < FETCH_FROM:
            return _set("too_early", "13:50 之後才抓今天的成交", now)
        if now.time() > FETCH_UNTIL:
            return _set("window_closed", "超過 15:00，今天不再抓", now)
        if (_ticks_dir() / f"{d}.csv.gz").exists():
            return _set("exists", "今天的成交已經存好", now)
        if FETCH.get("done_day") == str(d):
            return FETCH["status"]
        try:
            pos = has_position()
        except Exception:
            pos = True            # 問不到 ⇒ 當成有部位（寧可今天不抓）
        if pos:
            return _set("position", "有部位，先不抓", now)
        if api is None:
            return _set("no_api", "面板還沒連上永豐", now)
        lt = FETCH.get("last_try")
        if lt is not None and (now - lt).total_seconds() < FETCH_RETRY_S:
            return FETCH["status"]
        FETCH["last_try"] = now
        u = api.usage(timeout=USAGE_TIMEOUT_MS)
        used, lim = float(u.bytes), float(u.limit_bytes)
        if lim <= 0 or used / lim > USAGE_MAX:
            return _set("usage_high", f"永豐流量已用 {used / lim * 100:.0f}%（超過 85% 不抓）" if lim > 0
                        else "讀不到流量上限，不抓", now)
        if qt is None:
            import shioaji as sj
            qt = (getattr(sj, "TicksQueryType", None) or sj.constant.TicksQueryType).RangeTime
        contract = api.Contracts.Futures.TMF[CONTRACT_CODE]
        tk = api.ticks(contract=contract, date=str(d), query_type=qt,
                       time_start="08:45:00", time_end="13:45:00", timeout=TICKS_TIMEOUT_MS)
        df = pd.DataFrame({**tk})
        if df.empty:
            FETCH["done_day"] = str(d)
            return _set("empty", "今天沒有日盤成交（休市？）", now)
        if not _complete(df, d):
            return _set("incomplete", "成交資料還不完整，10 分鐘後再試", now)
        _ticks_dir().mkdir(parents=True, exist_ok=True)
        out = _ticks_dir() / f"{d}.csv.gz"
        tmp = out.with_name(out.name + ".tmp")
        df.to_csv(tmp, index=False, compression="gzip")
        os.replace(tmp, out)
        build_cache(str(d))
        refresh_days()
        FETCH["done_day"] = str(d)
        return _set("saved", f"已存 {d}（{len(df):,} 筆）", now)
    except Exception as e:
        try:
            log(f"抓今天的成交失敗：{str(e)[:160]}（10 分鐘後再試）")
            return _set("error", "抓取失敗：" + str(e)[:120], now or datetime.now())
        except Exception:
            return "error"


def fetch_loop(get_api, has_position, every=60.0):
    """面板 main() 起的背景執行緒。⛔ 永遠不丟例外、不碰主迴圈"""
    try:
        refresh_days()            # 開機先把快取／摘要補齊（第一次會花幾十秒，在背景）
    except Exception as e:
        log(f"建每日摘要失敗：{str(e)[:160]}")
    while True:
        try:
            st = fetch_today(get_api(), has_position)
            if st in ("too_early", "window_closed", "not_trading_day", "exists"):
                refresh_days()    # 1 分 K 檔 14:10 才併進來 ⇒ 夜盤欄位晚一點補
        except Exception as e:
            log(f"背景執行緒例外（已吞掉）：{str(e)[:160]}")
        time.sleep(every)


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    t0 = time.time()
    for d in _tick_dates():
        load_day(d)
    refresh_days(force="--force" in sys.argv)
    print(f"快取＋摘要完成：{len(days())} 天，{time.time() - t0:.1f} 秒")
