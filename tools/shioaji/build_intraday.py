r"""
建立「盤中逐分鐘」歷史矩陣 → intraday.csv
=============================================================================
把 248 個交易日的 08:45~09:30 每一分鐘，都轉成一筆：

    「那一刻的盤面狀態」 →  「後來到 13:45 為止，做多／做空各是輸是贏」

盤中即時面板就是拿現在這一刻的狀態，去這張表裡找歷史上長得像的時刻，
看那些時刻後來怎麼走，算出做多勝率／做空勝率。

狀態特徵（都是當下看得到的資訊，沒有偷看未來）：
  mom5       最近 5 分鐘漲跌      ← 「當下的趨勢」，權重最高
  mom15      最近 15 分鐘漲跌     ← 「當下的趨勢」
  ret_open   現價 - 今天 08:45 開盤價
  gap        今天開盤價 - 上一個交易日日盤收盤（跳空）
  rng        今天到目前為止的高低幅
  pos        現價在今天到目前為止區間的位置（0=最低, 1=最高）
  vol_ratio  到目前為止的累計量 ÷ 歷史同一分鐘的中位數量

後續結果：從當下價格進場，停利停損 ±100 點，13:45 前沒觸及就收盤平倉，
         扣掉來回手續費 5 點，算做多與做空各自的淨點數。

【去趨勢】同時算一組「扣掉大盤漂移」的結果（net_long_dt / net_short_dt）。
樣本這一年台指期漲 81.7%，日盤 09:00→13:45 中位漂移約 +25 點；
在 ±100 點的框架下，這足以讓做多勝率虛胖。
去趨勢的做法：把每天的後續路徑減掉「同一分鐘、全樣本的中位數漂移」（依時間線性攤提），
這樣算出來的勝率反映的是「當下這個動能會不會延續」，而不是「那一年大盤在漲」。

執行：
  ..\..\.venv\Scripts\python.exe build_intraday.py
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

CSV = Path(__file__).with_name("txf_1min.csv")
OUT = Path(__file__).with_name("intraday.csv")

SESSION_OPEN = pd.Timestamp("08:45").time()
WATCH_END = pd.Timestamp("09:30").time()      # Benson 的下單時段（只有這段會記錄資料）
DAY_END = pd.Timestamp("13:45").time()
MATRIX_END = pd.Timestamp("13:30").time()     # 矩陣涵蓋整個日盤，讓非下單時段也看得到趨勢

TP = SL = 100.0
FEE = 5.0                       # 來回 NT$50 ÷ 每點 NT$10

FEATURES = ["mom5", "mom15", "ret_open", "gap", "rng", "pos", "vol_ratio"]


def simulate(highs, lows, closes, entry, direction, drift=None):
    """
    從 entry 進場，±100 先碰哪個算哪個，沒碰到就撐到 13:45 收盤平倉。

    回傳 (淨點數, 結果)，結果為：
      'tp'   吃到 +100 停利   ← Benson 定義的「贏」就是這個
      'sl'   吃到 -100 停損
      'none' 到收盤都沒碰到任何一邊

    drift 有給的話，會把「線性攤提的大盤漂移」從路徑上扣掉（去趨勢）。
    """
    tp_p = entry + direction * TP
    sl_p = entry - direction * SL
    n = len(highs)
    for i in range(n):
        adj = drift * (i + 1) / n if drift else 0.0
        h, l = highs[i] - adj, lows[i] - adj
        if direction == 1:
            hit_tp, hit_sl = h >= tp_p, l <= sl_p
        else:
            hit_tp, hit_sl = l <= tp_p, h >= sl_p
        if hit_tp and hit_sl:
            return -SL - FEE, "sl"      # 同棒模糊 → 保守算停損
        if hit_tp:
            return TP - FEE, "tp"
        if hit_sl:
            return -SL - FEE, "sl"
    final = closes[-1] - (drift if drift else 0.0)
    return direction * (final - entry) - FEE, "none"


def main():
    if not CSV.exists():
        print(f"找不到 {CSV}，請先跑 shioaji_export.py")
        return

    df = pd.read_csv(CSV)
    df["ts"] = pd.to_datetime(df["ts"])
    df["date"] = df["ts"].dt.date
    df["time"] = df["ts"].dt.time
    df = df[(df["time"] >= SESSION_OPEN) & (df["time"] < DAY_END)].sort_values("ts")

    days = {d: g.reset_index(drop=True) for d, g in df.groupby("date", sort=True)}
    dates = sorted(days)
    print(f"交易日 {len(dates)} 天：{dates[0]} ~ {dates[-1]}")

    # 涵蓋整個日盤的分鐘清單（08:45 ~ 13:30）
    minutes = []
    t = pd.Timestamp("2000-01-01 08:45")
    while t.time() <= MATRIX_END:
        minutes.append(t.time())
        t += pd.Timedelta(minutes=1)
    print(f"每天 {len(minutes)} 個時間點（{minutes[0]} ~ {minutes[-1]}）")
    print(f"其中 08:45~09:30 是 Benson 的下單時段\n")

    # ---- 第一輪：算出每個時間點的狀態特徵，以及「到收盤的漂移」
    snaps = []
    prev_close = None
    for d in dates:
        g = days[d]
        if g.empty or g["time"].iloc[0] > pd.Timestamp("08:50").time():
            prev_close = g["Close"].iloc[-1] if not g.empty else prev_close
            continue

        day_open = float(g["Open"].iloc[0])
        gap = (day_open - prev_close) if prev_close is not None else np.nan

        times = g["time"].to_numpy()
        highs = g["High"].to_numpy(dtype=float)
        lows = g["Low"].to_numpy(dtype=float)
        closes = g["Close"].to_numpy(dtype=float)
        vols = g["Volume"].to_numpy(dtype=float)

        for m in minutes:
            idx = np.nonzero(times <= m)[0]
            if len(idx) == 0 or idx[-1] + 1 >= len(closes):
                continue
            i = idx[-1]
            cur = closes[i]
            hi, lo = highs[: i + 1].max(), lows[: i + 1].min()
            snaps.append({
                "date": str(d), "minute": m.strftime("%H:%M"), "i": i,
                "day": d, "price": cur,
                "mom5": cur - closes[max(0, i - 5)],
                "mom15": cur - closes[max(0, i - 15)],
                "ret_open": cur - day_open,
                "gap": gap,
                "rng": hi - lo,
                "pos": (cur - lo) / (hi - lo) if hi > lo else 0.5,
                "vol_cum": vols[: i + 1].sum(),
                "fwd": closes[-1] - cur,          # 到 13:45 的漂移
                # 短天期後續走勢 → 趨勢指數用（接下來幾分鐘漲還是跌）
                "fwd5": closes[min(i + 5, len(closes) - 1)] - cur,
                "fwd10": closes[min(i + 10, len(closes) - 1)] - cur,
                "fwd15": closes[min(i + 15, len(closes) - 1)] - cur,
            })
        prev_close = closes[-1]

    snap_df = pd.DataFrame(snaps).dropna(subset=["gap"])

    # ---- 每分鐘的大盤漂移（用中位數，避免被少數暴跌日拉走）
    drift_by_min = snap_df.groupby("minute")["fwd"].median().to_dict()
    print("各時間點的大盤漂移中位數（去趨勢就是扣掉這個）：")
    for m in ["08:50", "09:00", "09:10", "09:20", "09:30"]:
        if m in drift_by_min:
            print(f"  {m}  {drift_by_min[m]:+.0f} 點")
    print()

    # ---- 第二輪：模擬（含原始與去趨勢兩組）
    rows = []
    for r in snap_df.to_dict("records"):
        g = days[r["day"]]
        highs = g["High"].to_numpy(dtype=float)
        lows = g["Low"].to_numpy(dtype=float)
        closes = g["Close"].to_numpy(dtype=float)
        i, cur = r["i"], r["price"]
        h, l, c = highs[i + 1:], lows[i + 1:], closes[i + 1:]
        dft = drift_by_min.get(r["minute"], 0.0)
        nl, ol = simulate(h, l, c, cur, 1)
        ns, os_ = simulate(h, l, c, cur, -1)
        nld, old = simulate(h, l, c, cur, 1, drift=dft)
        nsd, osd = simulate(h, l, c, cur, -1, drift=dft)
        rows.append({
            **{k: r[k] for k in ["date", "minute", "price", "mom5", "mom15",
                                 "ret_open", "gap", "rng", "pos", "vol_cum",
                                 "fwd5", "fwd10", "fwd15"]},
            "net_long": nl, "out_long": ol,
            "net_short": ns, "out_short": os_,
            "net_long_dt": nld, "out_long_dt": old,
            "net_short_dt": nsd, "out_short_dt": osd,
        })

    out = pd.DataFrame(rows)

    # 量能比：除以「同一分鐘、全樣本的累計量中位數」
    ref = out.groupby("minute")["vol_cum"].transform("median")
    out["vol_ratio"] = out["vol_cum"] / ref

    # 「贏」= 真的吃到 +100 停利（Benson 的定義），不是收盤結算有賺就算
    for side in ["long", "short", "long_dt", "short_dt"]:
        out[f"win_{side}"] = (out[f"out_{side}"] == "tp").astype(int)
        out[f"lose_{side}"] = (out[f"out_{side}"] == "sl").astype(int)

    out.to_csv(OUT, index=False)

    n_days = out["date"].nunique()
    print(f"產出 {len(out):,} 筆（{n_days} 天 × 每天約 {len(out) // max(n_days,1)} 個時間點）")
    print(f"→ {OUT.name}\n")

    # 摘要一律只看下單時段 —— 全天混在一起會失真
    # （13:00 進場只剩 45 分鐘，本來就很難摸到 ±100，跟早盤不能比）
    win = out[out["minute"] <= WATCH_END.strftime("%H:%M")]
    print(f"以下摘要只看下單時段 08:45~09:30（{len(win):,} 筆）：\n")

    print("結果分布（做多、去趨勢）：")
    vc = win["out_long_dt"].value_counts(normalize=True) * 100
    print(f"  吃到 +100 停利  {vc.get('tp', 0):5.1f}%")
    print(f"  吃到 -100 停損  {vc.get('sl', 0):5.1f}%")
    print(f"  收盤都沒碰到    {vc.get('none', 0):5.1f}%")

    print("\n整體基準（「贏」＝吃到 +100 停利）：")
    print(f"  【原始】做多 {win['win_long'].mean()*100:5.1f}%   做空 {win['win_short'].mean()*100:5.1f}%"
          f"   ← 含那一年大盤在漲")
    print(f"  【去趨勢】做多 {win['win_long_dt'].mean()*100:5.1f}%   做空 {win['win_short_dt'].mean()*100:5.1f}%"
          f"   ← 扣掉大盤漂移，這才是純動能")

    print("\n分時段（去趨勢、吃到 +100 的比例）：")
    for m in ["08:50", "09:00", "09:05", "09:10", "09:15", "09:20", "09:30"]:
        s = win[win["minute"] == m]
        if not s.empty:
            print(f"  {m}   n={len(s):>3}   做多 {s['win_long_dt'].mean()*100:5.1f}%   "
                  f"做空 {s['win_short_dt'].mean()*100:5.1f}%   "
                  f"（沒碰到 {(s['out_long_dt']=='none').mean()*100:4.1f}%）")

    print("\n動能延續性檢查（去趨勢、以天為單位算信賴區間）：")
    for lo, hi, name in [(-9e9, -40, "最近5分鐘跌超過40點"), (-40, -10, "小跌"),
                         (-10, 10, "幾乎沒動"), (10, 40, "小漲"), (40, 9e9, "最近5分鐘漲超過40點")]:
        s = win[(win["mom5"] > lo) & (win["mom5"] <= hi)]
        if len(s) < 30:
            continue
        byday = s.groupby("date")["win_long_dt"].mean()
        p = byday.mean(); se = byday.std(ddof=1) / np.sqrt(len(byday))
        mark = "★" if (p - 1.96*se > 0.5 or p + 1.96*se < 0.5) else "—"
        print(f"  {name:<16} n={len(byday):>3}天  做多勝率 {p*100:5.1f}%  "
              f"95%CI [{(p-1.96*se)*100:4.1f}%, {(p+1.96*se)*100:4.1f}%]  {mark}")


if __name__ == "__main__":
    main()
