r"""
建立「盤中逐分鐘」歷史矩陣 → intraday.csv
=============================================================================
把微台指每個交易日 08:45~13:30 的每一分鐘，都轉成一筆：

    「那一刻的盤面狀態」 →  「接下來 5 / 10 / 15 分鐘往哪走」

盤中即時面板就是拿現在這一刻的狀態，去這張表裡找歷史上長得像的時刻，
看那些時刻後來怎麼走，算出趨勢方向與強度。

【商品】全部使用微型臺指期貨 TMF —— Benson 實際交易的商品。
不與大台 TXF 混用：兩者成交量差 6 倍以上，混用會讓量能特徵完全失真。

狀態特徵（都是當下看得到的資訊，沒有偷看未來）：
  mom5       最近 5 分鐘漲跌      ← 「當下的趨勢」，權重最高
  mom15      最近 15 分鐘漲跌     ← 「當下的趨勢」
  ret_open   現價 - 今天 08:45 開盤價
  gap        今天開盤價 - 上一個交易日日盤收盤（跳空）
  rng        今天到目前為止的高低幅
  pos        現價在今天到目前為止區間的位置（0=最低, 1=最高）
  vol_ratio  到目前為止的累計量 ÷ 歷史同一分鐘的中位數量

【波動度正規化 —— 跨年份比較的必要條件】
指數水準會變（2024 年約 2 萬多、2026 年 4 萬 5），同樣的點數在不同時期意義不同。
直接用點數跨期比較，模型會把當年的大波動誤認成今天的小抖動。

所以所有點數欄位都除以 dayvol（= 前 20 個交易日「日盤高低幅」的中位數），
變成「幾倍的當時日常波動」。dayvol 只用過去的資料算，不會偷看當天。
帶 _n 結尾的就是正規化後的欄位，模型比對用這組；顯示給人看時再乘回 dayvol 換成點數。

後續結果：從當下價格進場，停利停損 ±100 點，13:45 前沒觸及就收盤平倉，
         扣掉來回手續費 5 點，算做多與做空各自的淨點數。

【去趨勢】另外算一組「扣掉大盤漂移」的結果（欄位尾巴 _dt，僅在 SIMULATE_TP_SL 開啟時產生）。
做法：把每天的後續路徑減掉「同一分鐘、全樣本的中位數漂移」（依時間線性攤提），
剩下的才是「當下這個動能會不會延續」，而不是「那段期間大盤在漲」。

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

CSV = Path(__file__).with_name("tmf_1min.csv")
OUT = Path(__file__).with_name("intraday.csv")

SESSION_OPEN = pd.Timestamp("08:45").time()
WATCH_END = pd.Timestamp("09:30").time()      # Benson 的下單時段（只有這段會記錄資料）
DAY_END = pd.Timestamp("13:45").time()
MATRIX_END = pd.Timestamp("13:30").time()     # 矩陣涵蓋整個日盤，讓非下單時段也看得到趨勢

TP = SL = 100.0
FEE = 5.0                       # 來回 NT$50 ÷ 每點 NT$10

# ±100 停利停損的逐筆模擬佔了 95% 以上的運算量，而面板已改成只顯示趨勢、不再用這組數字。
# 需要重新檢視「吃到 ±100 的勝率」時再打開（跑起來很久）。
SIMULATE_TP_SL = False

FEATURES = ["mom5_n", "mom15_n", "ret_open_n", "gap_n", "rng_n", "pos", "vol_ratio"]


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

    # 每天的波動度基準 = 前 20 個交易日「日盤高低幅」的中位數（只用過去，不偷看當天）
    day_range = pd.Series({d: float(g["High"].max() - g["Low"].min()) for d, g in days.items()})
    day_range = day_range.sort_index()
    dayvol = day_range.rolling(20, min_periods=10).median().shift(1)
    print(f"波動度基準 dayvol：最早 {dayvol.dropna().iloc[0]:.0f} 點"
          f" → 最近 {dayvol.dropna().iloc[-1]:.0f} 點"
          f"（{dayvol.dropna().iloc[-1] / dayvol.dropna().iloc[0]:.1f} 倍，這就是為什麼要正規化）")

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

    # 波動度正規化：所有點數欄位換算成「幾倍的當時日常波動」
    snap_df["dayvol"] = snap_df["day"].map(dayvol.to_dict())
    snap_df = snap_df.dropna(subset=["dayvol"])
    snap_df = snap_df[snap_df["dayvol"] > 0]
    for col in ["mom5", "mom15", "ret_open", "gap", "rng", "fwd5", "fwd10", "fwd15"]:
        snap_df[col + "_n"] = snap_df[col] / snap_df["dayvol"]

    # ---- 每分鐘的大盤漂移（用中位數，避免被少數暴跌日拉走）
    drift_by_min = snap_df.groupby("minute")["fwd"].median().to_dict()
    print("各時間點的大盤漂移中位數（去趨勢就是扣掉這個）：")
    for m in ["08:50", "09:00", "09:10", "09:20", "09:30"]:
        if m in drift_by_min:
            print(f"  {m}  {drift_by_min[m]:+.0f} 點")
    print()

    # ---- 第二輪：模擬 ±100 停利停損（預設關閉，見 SIMULATE_TP_SL）
    keep = ["date", "minute", "price", "mom5", "mom15", "ret_open", "gap", "rng",
            "pos", "vol_cum", "fwd5", "fwd10", "fwd15", "dayvol",
            "mom5_n", "mom15_n", "ret_open_n", "gap_n", "rng_n",
            "fwd5_n", "fwd10_n", "fwd15_n"]
    if SIMULATE_TP_SL:
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
            rows.append({**{k: r[k] for k in keep},
                         "net_long": nl, "out_long": ol,
                         "net_short": ns, "out_short": os_,
                         "net_long_dt": nld, "out_long_dt": old,
                         "net_short_dt": nsd, "out_short_dt": osd})
        out = pd.DataFrame(rows)
        for side in ["long", "short", "long_dt", "short_dt"]:
            out[f"win_{side}"] = (out[f"out_{side}"] == "tp").astype(int)
    else:
        out = snap_df[keep].copy()
        print("（已略過 ±100 停利停損模擬 —— 面板只用趨勢欄位。"
              "要重新分析勝率請把 SIMULATE_TP_SL 改成 True）\n")

    # 量能比：除以「同一分鐘、全樣本的累計量中位數」
    ref = out.groupby("minute")["vol_cum"].transform("median")
    out["vol_ratio"] = out["vol_cum"] / ref

    out.to_csv(OUT, index=False)

    n_days = out["date"].nunique()
    print(f"產出 {len(out):,} 筆（{n_days} 天 × 每天約 {len(out) // max(n_days,1)} 個時間點）")
    print(f"→ {OUT.name}\n")

    # 摘要一律只看下單時段 —— 全天混在一起會失真
    # （13:00 進場只剩 45 分鐘，本來就很難摸到 ±100，跟早盤不能比）
    win = out[out["minute"] <= WATCH_END.strftime("%H:%M")]
    print(f"以下摘要只看下單時段 08:45~09:30（{len(win):,} 筆）：\n")

    if SIMULATE_TP_SL:
        print("整體基準（「贏」＝吃到 +100 停利）：")
        print(f"  【原始】做多 {win['win_long'].mean()*100:5.1f}%"
              f"   做空 {win['win_short'].mean()*100:5.1f}%   ← 含當期大盤漲跌")
        print(f"  【去趨勢】做多 {win['win_long_dt'].mean()*100:5.1f}%"
              f"   做空 {win['win_short_dt'].mean()*100:5.1f}%   ← 扣掉大盤漂移\n")

    print("動能延續性（10 分鐘後上漲的比例，以天為單位算信賴區間）：")
    for lo, hi, name in [(-9e9, -0.15, "急跌（>0.15 倍日常波動）"), (-0.15, -0.05, "小跌"),
                         (-0.05, 0.05, "幾乎沒動"), (0.05, 0.15, "小漲"),
                         (0.15, 9e9, "急漲（>0.15 倍日常波動）")]:
        s = win[(win["mom5_n"] > lo) & (win["mom5_n"] <= hi)]
        if len(s) < 30:
            continue
        byday = s.assign(u=(s["fwd10_n"] > 0).astype(int)).groupby("date")["u"].mean()
        p = byday.mean(); se = byday.std(ddof=1) / np.sqrt(len(byday))
        mark = "★" if (p - 1.96*se > 0.5 or p + 1.96*se < 0.5) else "—"
        print(f"  {name:<22} n={len(byday):>4}天  上漲 {p*100:5.1f}%  "
              f"95%CI [{(p-1.96*se)*100:4.1f}%, {(p+1.96*se)*100:4.1f}%]  {mark}")

    print("\n分年度看（急跌後 10 分鐘上漲的比例）—— 這個現象在每一年都成立嗎：")
    yr = win[win["mom5_n"] <= -0.15].copy()
    yr["年"] = yr["date"].str[:4]
    for y, g in yr.groupby("年"):
        byday = g.assign(u=(g["fwd10_n"] > 0).astype(int)).groupby("date")["u"].mean()
        if len(byday) < 20:
            continue
        p = byday.mean(); se = byday.std(ddof=1) / np.sqrt(len(byday))
        mark = "★" if (p - 1.96*se > 0.5 or p + 1.96*se < 0.5) else "—"
        print(f"  {y}  n={len(byday):>3}天  上漲 {p*100:5.1f}%  "
              f"95%CI [{(p-1.96*se)*100:4.1f}%, {(p+1.96*se)*100:4.1f}%]  {mark}")


if __name__ == "__main__":
    main()
