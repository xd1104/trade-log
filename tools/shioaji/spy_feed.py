# -*- coding: utf-8 -*-
"""
SPY 5 分 K（給【模擬】第八條線「美股開盤模型」用）。2026-09-22 加。

⛔⛔ **唯讀行情，這個檔不會下任何單。** 資料來源 Alpaca，金鑰在 `~/.alpaca-token`（兩行）。
⚠️ **一律用 IEX**：Alpaca 免費方案的即時只有 IEX（SIP 即時是 403）⇒ 模擬要跟實盤拿得到的
   東西一致，所以歷史也用 IEX（研究驗過：IEX vs SIP 的 5 分走幅相關 0.9902、績效幾乎沒差）。

落地：`us_spy/YYYY-MM.csv`（⛔ gitignore）。一個月一個檔，抓過就不再抓。
⚠️ 抓不到就回 None —— 呼叫端要照實說「缺 SPY」，⛔ 不准用舊資料或猜。
"""
import csv
import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
DIR = HERE / "us_spy"
TOKEN = Path.home() / ".alpaca-token"
URL = "https://data.alpaca.markets/v2/stocks/bars"
TPE = timezone(timedelta(hours=8))
FEED = "iex"
TIMEOUT = 45


def _keys():
    try:
        lines = [x.strip() for x in TOKEN.read_text(encoding="utf-8").splitlines() if x.strip()]
        return (lines[0], lines[1]) if len(lines) >= 2 else (None, None)
    except Exception:
        return None, None


def _fetch_month(ym):
    """跟 Alpaca 要一個月的 5 分 K ⇒ [(ts, close)]；失敗回 None（⛔ 不丟例外給呼叫端）。"""
    k, s = _keys()
    if not k:
        return None
    y, m = int(ym[:4]), int(ym[5:7])
    start = datetime(y, m, 1, tzinfo=timezone.utc) - timedelta(days=1)
    end = (datetime(y + (m == 12), (m % 12) + 1, 1, tzinfo=timezone.utc) + timedelta(days=1))
    rows, token = [], None
    try:
        while True:
            q = {"symbols": "SPY", "timeframe": "5Min", "limit": "10000",
                 "adjustment": "raw", "feed": FEED,
                 "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "end": end.strftime("%Y-%m-%dT%H:%M:%SZ")}
            if token:
                q["page_token"] = token
            req = urllib.request.Request(URL + "?" + urllib.parse.urlencode(q), headers={
                "APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                j = json.loads(r.read().decode())
            rows += [(b["t"], b["c"]) for b in ((j.get("bars") or {}).get("SPY") or [])]
            token = j.get("next_page_token")
            if not token:
                break
    except Exception:
        return None
    return rows


def _month_path(ym):
    return DIR / (ym + ".csv")


def ensure_month(ym, refetch_if_recent=True):
    """
    確保某個月的檔案存在 ⇒ True/False。
    ⚠️ **當月與上個月會重抓**（那兩個月還在長；舊月份抓過就不動）。
    """
    p = _month_path(ym)
    now = datetime.now(TPE)
    cur = "%04d-%02d" % (now.year, now.month)
    prev = "%04d-%02d" % ((now.year - 1) if now.month == 1 else now.year,
                          12 if now.month == 1 else now.month - 1)
    if p.exists() and not (refetch_if_recent and ym in (cur, prev)):
        return True
    rows = _fetch_month(ym)
    if rows is None:
        return p.exists()          # 抓失敗 ⇒ 有舊檔就先用舊的
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ts", "close"])
        w.writerows(rows)
    tmp.replace(p)                 # ⛔ 原子換檔（面板可能正在讀）
    return True


_CACHE = {"key": None, "map": None}


def _load(ym):
    p = _month_path(ym)
    try:
        st = p.stat()
    except OSError:
        return {}
    key = (ym, st.st_mtime_ns, st.st_size)
    if _CACHE["key"] == key:
        return _CACHE["map"]
    out = {}
    try:
        with p.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                dt = datetime.fromisoformat(row["ts"].replace("Z", "+00:00")).astimezone(TPE)
                mo = dt.hour * 60 + dt.minute
                if mo >= 15 * 60 + 1:
                    E, k = dt.date(), mo
                elif mo <= 5 * 60:
                    E, k = dt.date() - timedelta(days=1), mo + 1440
                else:
                    continue
                out.setdefault(E, {})[k] = float(row["close"])
    except Exception:
        return {}
    _CACHE.update(key=key, map=out)
    return out


def evening(E, fetch=True):
    """
    E 那一晚的 SPY ⇒ {夜盤分鐘: 收盤}；拿不到 ⇒ None（⛔ 呼叫端照實說缺資料）。
    ⚠️ 一晚會橫跨兩個月份檔（例如 21:30 在 A 月、隔天 04:00 也在 A 月），
       但月初那幾天要看前一個月的檔 ⇒ 兩個月都讀。
    """
    yms = {"%04d-%02d" % (E.year, E.month),
           "%04d-%02d" % ((E + timedelta(days=1)).year, (E + timedelta(days=1)).month)}
    if fetch:
        for ym in sorted(yms):
            ensure_month(ym)
    out = {}
    for ym in sorted(yms):
        got = _load(ym).get(E)
        if got:
            out.update(got)
    return out or None
