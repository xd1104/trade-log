# -*- coding: utf-8 -*-
"""
美股 5 分 K（Alpaca，唯讀行情）。2026-09-22 晚加，取代 `spy_feed.py`。

⛔⛔ **Alpaca 的 K 棒時間標的是「開始」時間**（例：14:30Z 那一根＝美東 9:30~9:35，收盤是 9:35 的價）。
   永豐的台指 1 分 K 標的是**結束**時間。舊的 `spy_feed` 把兩者當同一條時間軸 ⇒ 「美股開盤模型」
   偷看了進場後 5 分鐘（`tick-research/night_ml_CORRECTION_2026-09-22.md`）。
   ⇒ 這個模組**只回傳「哪一根」與它的開／收盤**，並且明講那一根**什麼時候才收完**（`done_at`），
      呼叫端拿 `done_at` 去對台指，⛔ 不准自己拿 ts 當時刻用。

資料來源：預設 **SIP**（全市場，研究用的就是 SIP；免費方案 15 分鐘前的都拿得到，
模擬是隔天早上才算 ⇒ 夠用）。另存 IEX 那一份，給之後比較「即時只有 IEX 時會差多少」。
落地：`us_bars/{SYM}-{feed}-{YYYY-MM}.csv`（⛔ gitignore）。抓不到回 None，⛔ 不猜、不用別的來源頂。
"""
import csv
import json
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).parent
DIR = HERE / "us_bars"
TOKEN = Path.home() / ".alpaca-token"
URL = "https://data.alpaca.markets/v2/stocks/bars"
TPE = timezone(timedelta(hours=8))
NY = ZoneInfo("America/New_York")
TIMEOUT = 45
BAR_MIN = 5
RECENT_TTL = 1800.0      # 當月／上個月的檔最多每 30 分鐘重抓一次（面板每分鐘跑一輪，⛔ 不要每分鐘打 Alpaca）
_LAST_FETCH = {}


def _keys():
    try:
        lines = [x.strip() for x in TOKEN.read_text(encoding="utf-8").splitlines() if x.strip()]
        return (lines[0], lines[1]) if len(lines) >= 2 else (None, None)
    except Exception:
        return None, None


def _fetch_month(sym, feed, ym):
    """⇒ [(開始時間 UTC 字串, open, close)]；失敗回 None（⛔ 不丟例外）。"""
    k, s = _keys()
    if not k:
        return None
    y, m = int(ym[:4]), int(ym[5:7])
    start = datetime(y, m, 1, tzinfo=timezone.utc)
    end = datetime(y + (m == 12), (m % 12) + 1, 1, tzinfo=timezone.utc)
    # ⛔ 免費方案不准查最近 15 分鐘的 SIP（整個請求會 403）⇒ 當月的 end 夾到 20 分鐘前
    end = min(end, datetime.now(timezone.utc) - timedelta(minutes=20))
    if end <= start:
        return []
    rows, token = [], None
    try:
        while True:
            q = {"symbols": sym, "timeframe": "5Min", "limit": "10000", "adjustment": "raw",
                 "feed": feed, "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "end": end.strftime("%Y-%m-%dT%H:%M:%SZ")}
            if token:
                q["page_token"] = token
            req = urllib.request.Request(URL + "?" + urllib.parse.urlencode(q), headers={
                "APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                j = json.loads(r.read().decode())
            rows += [(b["t"], b["o"], b["c"]) for b in ((j.get("bars") or {}).get(sym) or [])]
            token = j.get("next_page_token")
            if not token:
                break
    except Exception:
        return None
    return rows


def _path(sym, feed, ym):
    return DIR / ("%s-%s-%s.csv" % (sym.upper(), feed, ym))


def ensure_month(sym, feed, ym, now=None):
    """確保那個月的檔在 ⇒ True/False。⚠️ 當月與上個月還在長 ⇒ 每 RECENT_TTL 秒最多重抓一次。"""
    p = _path(sym, feed, ym)
    now = now or datetime.now(TPE)
    cur = "%04d-%02d" % (now.year, now.month)
    prv = "%04d-%02d" % ((now.year - 1) if now.month == 1 else now.year,
                         12 if now.month == 1 else now.month - 1)
    recent = ym in (cur, prv)
    if p.exists() and not recent:
        return True
    key = (sym, feed, ym)
    if p.exists() and time.time() - _LAST_FETCH.get(key, 0) < RECENT_TTL:
        return True
    _LAST_FETCH[key] = time.time()
    rows = _fetch_month(sym.upper(), feed, ym)
    if rows is None:
        return p.exists()
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["start_utc", "open", "close"])
        w.writerows(rows)
    tmp.replace(p)                 # ⛔ 原子換檔
    return True


_CACHE = {}


def _load(sym, feed, ym):
    """⇒ {美東日期: (open, close)}，只收**美東 9:30 開始**的那一根（開盤第一根 5 分 K）。"""
    p = _path(sym, feed, ym)
    try:
        st = p.stat()
    except OSError:
        return None
    key = (sym, feed, ym, st.st_mtime_ns, st.st_size)
    if key in _CACHE:
        return _CACHE[key]
    out = {}
    try:
        with p.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                t0 = datetime.fromisoformat(row["start_utc"].replace("Z", "+00:00")).astimezone(NY)
                if (t0.hour, t0.minute) == (9, 30):
                    out[t0.date()] = (float(row["open"]), float(row["close"]))
    except Exception:
        return None
    _CACHE[key] = out
    return out


def first5(sym, E, feed="sip", fetch=True, now=None):
    """
    E（美東日期；台北同一個日曆日的晚上開盤）那天**開盤第一根 5 分 K**。
    ⇒ {"open", "close", "mv_pct", "done_at"}，或 `"closed"`（那個月的資料在、就是沒有 E ＝ 美股休市），
      或 None（資料抓不到／還沒到）。
    ⭐ `done_at`＝那一根**收完**的台北時刻（美東 9:35）；呼叫端只准在台指 `≤ done_at` 之後才用它。
    """
    ym = "%04d-%02d" % (E.year, E.month)
    if fetch:
        ensure_month(sym, feed, ym, now=now)
    m = _load(sym, feed, ym)
    if m is None:
        return None
    got = m.get(E)
    if got is None:
        later = [d for d in m if d > E]
        return "closed" if later else None
    o, c = got
    if o <= 0:
        return None
    done = datetime(E.year, E.month, E.day, 9, 30, tzinfo=NY) + timedelta(minutes=BAR_MIN)
    return {"open": o, "close": c, "mv_pct": 100.0 * (c - o) / o,
            "done_at": done.astimezone(TPE).replace(tzinfo=None)}


LIVE_TIMEOUT = 8


def first5_live(sym, E, feed="iex", now=None):
    """
    ⭐ **即時版**（2026-09-22 夜盤真單用）：直接跟 Alpaca 要 E 那天美東 9:30 開始的那一根 5 分 K。
    ⇒ 同 `first5()` 的 dict，或 None（還沒收完／還沒出來／抓不到）。⛔ 不寫檔、不丟例外。
    ⛔⛔ 那一根 9:35 才收完 ⇒ 台北時間還沒到 `done_at` 一律回 None（⛔ 不准拿半根當整根）。
    ⚠️ 免費方案即時只有 **IEX**（SIP 即時是 403）；研究驗過用 IEX 判快不快，回測沒有變差。
    """
    done = datetime(E.year, E.month, E.day, 9, 30, tzinfo=NY) + timedelta(minutes=BAR_MIN)
    now = now or datetime.now(TPE)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TPE)
    if now < done:
        return None
    k, s = _keys()
    if not k:
        return None
    start = (done - timedelta(minutes=BAR_MIN)).astimezone(timezone.utc)
    q = {"symbols": sym.upper(), "timeframe": "5Min", "limit": "5", "adjustment": "raw", "feed": feed,
         "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": done.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    try:
        req = urllib.request.Request(URL + "?" + urllib.parse.urlencode(q), headers={
            "APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s})
        with urllib.request.urlopen(req, timeout=LIVE_TIMEOUT) as r:
            j = json.loads(r.read().decode())
        bars = (j.get("bars") or {}).get(sym.upper()) or []
    except Exception:
        return None
    for b in bars:
        t0 = datetime.fromisoformat(str(b["t"]).replace("Z", "+00:00")).astimezone(NY)
        if (t0.hour, t0.minute) == (9, 30) and t0.date() == E and float(b["o"]) > 0:
            o, c = float(b["o"]), float(b["c"])
            return {"open": o, "close": c, "mv_pct": 100.0 * (c - o) / o,
                    "done_at": done.astimezone(TPE).replace(tzinfo=None)}
    return None
