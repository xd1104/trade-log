# -*- coding: utf-8 -*-
"""
【程式下單】分頁的合成資料產生器（治具與探針共用）。

⛔ **價格一律 12000 附近。** 46xxx/47xxx 會撞到 Benson 真實的成交價，
   而這個 repo 是公開的（`tools/probe/leak-scan.py` 在守）。
⛔ 這裡一個位元組都不來自 `real_trades/`／`practice_trades/`／`autotest/`。

產出兩種東西：
  ① 一天的 1 分 K（08:45~13:45，300 根）—— 治具拿它頂替 `one_min_bars()`
  ② `autotest/YYYY-MM.jsonl` 的列 —— **用 live_panel 自己的函式算出來的**
     （`auto_dirs()` 決定方向、`_auto_run()` 決定出場），所以資料一定自洽：
     治具給的成績跟產品程式算的是同一套，不會出現「治具對、產品錯卻全綠」。
"""
import json
import math
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shioaji"))
import live_panel as LP          # noqa: E402

BASE_PX = 12000.0                # ⛔ 一律 12000 附近
OPEN_HM = "08:45"


def _rnd(seed):
    """可重現的偽亂數（不用 random，免得別人先 seed 過）。"""
    x = (seed * 1103515245 + 12345) & 0x7FFFFFFF
    while True:
        x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        yield (x / 0x7FFFFFFF) * 2 - 1


def day_bars(d, seed=None, drift=0.0, vol=9.0):
    """
    一天的 1 分 K（08:45~13:45，300 根，時間標籤是**起始時間**，跟 to_timeframe 一致）。

    drift 決定這天大致往哪走 —— 要造得出「+100 摸到」「−100 摸到」「都沒摸到」三種日子。
    """
    if seed is None:
        seed = sum(ord(c) for c in str(d))
    g = _rnd(seed)
    px = BASE_PX + (seed % 37) - 18
    out = []
    t0 = LP.SESSION_OPEN.hour * 60 + LP.SESSION_OPEN.minute
    for i in range(300):
        o = px
        px = px + drift + next(g) * vol
        h = max(o, px) + abs(next(g)) * vol * 0.4
        lo = min(o, px) - abs(next(g)) * vol * 0.4
        mm = t0 + i
        out.append({"t": f"{mm // 60:02d}:{mm % 60:02d}", "d": str(d),
                    "o": round(o, 1), "h": round(h, 1), "l": round(lo, 1),
                    "c": round(px, 1), "v": 100.0})
    return out


def sig_row(d, bars, src="live"):
    """
    那一天 09:03:30 的那一列。**進場價用標籤 09:04 那根的收盤當代理**
    （合成資料本來就沒有半分鐘的解析度，跟回測那條路同一個口徑）。
    """
    by = {b["t"]: b for b in bars}
    ent = by.get("09:03")
    o845 = by.get(OPEN_HM)
    p900 = by.get("09:00")
    if ent is None or o845 is None or p900 is None:
        return None
    px = round(ent["c"], 1)
    sa = round(px - p900["o"], 1)
    sb = round(px - o845["o"], 1)
    hi = max(b["h"] for b in bars[:19])
    lo = min(b["l"] for b in bars[:19])
    return {"rec": "sig", "date": str(d), "src": src,
            "at": "09:03:30.120", "at_lag_ms": 120,
            "px": px, "bid": round(px - 1, 1), "ask": px,
            "ref": {"open0845": round(o845["o"], 1), "p0900": round(p900["o"], 1),
                    "p0900_src": "bar_open", "prev_close": round(px - 12.0, 1),
                    "hi0845_0903": round(hi, 1), "lo0845_0903": round(lo, 1),
                    "rng20": 78.4},
            "sig": {"A": sa, "B": sb},
            "dirs": LP.auto_dirs(sa, sb),
            "thresh": LP.C_THRESH,
            "quote_gaps": 0.0,
            "wrote_at": f"{d}T09:03:30"}


def settle_row(d, bars, sig, src="live"):
    """⛔ 出場一律用產品程式的 `_auto_run()` 算，不要在治具裡另寫一份。"""
    after = [b for b in bars if b["t"] > LP.AUTO_SETTLE_FROM]
    runs = {}
    for k in ("A", "B", "C", "D"):
        dr = sig["dirs"].get(k)
        if dr is None:
            runs[k] = {"dir": None, "skip": "no_ref"}
        elif dr == 0:
            runs[k] = {"dir": 0, "skip": "below_threshold", "thresh": LP.C_THRESH}
        else:
            runs[k] = LP._auto_run(after, sig["px"], dr)
    return {"rec": "settle", "date": str(d), "src": src, "runs": runs,
            "settle_src": "1min", "settle_from": LP.AUTO_SETTLE_FROM,
            "bars": len(after), "wrote_at": f"{d}T13:47:00"}


def weekdays_back(n, end=None):
    """從 end（含）往回數 n 個平日，由早到晚。"""
    end = end or date.today()
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))


def build(days, end=None, src="live", settle=True, drift_of=None):
    """
    產 n 天的完整資料。回 `(rows, bars_by_date)`。

    drift_of(i) 決定第 i 天的走勢方向 —— 預設會刻意造出三種日子：
    摸到 +100、摸到 −100、以及**都沒摸到、13:45 收盤平**（那批不是 ±100 規則的結果，
    畫面上一定要單獨數出來）。
    """
    if drift_of is None:
        def drift_of(i):
            return (0.9, -0.9, 0.02)[i % 3]
    rows, bars = [], {}
    for i, d in enumerate(weekdays_back(days, end)):
        b = day_bars(d, seed=1000 + i * 7, drift=drift_of(i))
        bars[str(d)] = b
        s = sig_row(d, b, src=src)
        if s is None:
            continue
        rows.append(s)
        if settle:
            rows.append(settle_row(d, b, s, src=src))
    return rows, bars


def write(dirpath, rows):
    """照月份分檔寫出去（跟產品同一套檔名規則）。"""
    p = Path(dirpath)
    p.mkdir(parents=True, exist_ok=True)
    buckets = {}
    for r in rows:
        buckets.setdefault(str(r["date"])[:7], []).append(r)
    for month, rs in buckets.items():
        with (p / f"{month}.jsonl").open("a", encoding="utf-8") as f:
            for r in rs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return sorted(buckets)


def mine_rows(d, n=2, first_why="tp"):
    """
    他自己那幾筆的**合成**版本（⛔ 價格 12000 附近、時間與點數全是編的）。

    刻意造出 §17-4 那四種口徑差異：一天多筆、時刻不固定、有手動平倉、
    以及一筆 `points` 是 null（問不到成交價）。
    """
    base = BASE_PX + 30
    out = [{"date": str(d), "dir": "long", "qty": 1, "entry_time": "08:58:12",
            "entry": round(base, 1), "exit_time": "10:31:05",
            "exit": round(base + 100, 1) if first_why == "tp" else round(base - 100, 1),
            "reason": first_why, "points": 100.0 if first_why == "tp" else -100.0}]
    if n > 1:
        out.append({"date": str(d), "dir": "short", "qty": 1, "entry_time": "09:18:40",
                    "entry": round(base + 8, 1), "exit_time": "09:44:02",
                    "exit": round(base - 6, 1), "reason": "manual", "points": 14.0})
    if n > 2:
        out.append({"date": str(d), "dir": "long", "qty": 1, "entry_time": "09:26:55",
                    "entry": round(base + 3, 1), "exit_time": None, "exit": None,
                    "reason": "closed_elsewhere", "points": None})
    return out[:n]


if __name__ == "__main__":
    rs, bs = build(int(sys.argv[1]) if len(sys.argv) > 1 else 20)
    print(f"{len(rs)} 列 / {len(bs)} 天")
    for r in rs[:2]:
        print(json.dumps(r, ensure_ascii=False)[:200])
