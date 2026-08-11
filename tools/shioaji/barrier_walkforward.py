r"""
用「Benson 真正的規則」重做驗證
=============================================================================
先前的驗證有一個實質錯誤：

    面板顯示的是「10 分鐘後會不會漲」
    我的回測是「持有 10 分鐘後平倉」
    但 Benson 實際做的是「±100 點先碰到哪一個」

三個定義互不相同，所以那個「驗證通過」驗證的不是他在做的事。
這支程式改成直接用他的規則當目標。

【前提】Benson 已確認「每天一定要下一單」是硬約束。
所以問題不是「要不要下」，而是「今天做多還是做空比較不爛」。

【做法】
1. 每天每分鐘（08:45~09:30），從當時價格進場，往後走到 13:45，
   看是 +100 先到、-100 先到、還是收盤前都沒碰到。
2. 走查驗證：只用當天之前的資料找相似時刻，預測「做多會贏」的機率。
3. 每天只取第一個達門檻的時刻進場一次 —— 這才對得上「每天一單」。

執行：
  ..\..\.venv\Scripts\python.exe barrier_walkforward.py
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from live_panel import FEATURES, FEATURE_WEIGHT, K_NEIGHBOURS, MINUTE_WINDOW  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).parent
PX = HERE / "tmf_1min.csv"
MATRIX = HERE / "intraday.csv"
OUT = HERE / "barrier_results.csv"

SESSION_OPEN = pd.Timestamp("08:45").time()
WATCH_END = pd.Timestamp("09:30").time()
DAY_END = pd.Timestamp("13:45").time()

TP = SL = 100.0
FEE = 5.0
WARMUP_DAYS = 150


def barrier_outcome(highs, lows, closes, entry, direction):
    """±100 先碰哪個。回傳 (淨點數, 結果)。"""
    tp_p, sl_p = entry + direction * TP, entry - direction * SL
    for h, l in zip(highs, lows):
        if direction == 1:
            hit_tp, hit_sl = h >= tp_p, l <= sl_p
        else:
            hit_tp, hit_sl = l <= tp_p, h >= sl_p
        if hit_tp and hit_sl:
            return -SL - FEE, "sl"          # 同棒模糊 → 保守算停損
        if hit_tp:
            return TP - FEE, "tp"
        if hit_sl:
            return -SL - FEE, "sl"
    return direction * (closes[-1] - entry) - FEE, "none"


def build_outcomes():
    """算出每天每分鐘、做多的 ±100 結果。"""
    px = pd.read_csv(PX)
    px["ts"] = pd.to_datetime(px["ts"])
    px["date"] = px["ts"].dt.date.astype(str)
    px["time"] = px["ts"].dt.time
    px = px[(px["time"] >= SESSION_OPEN) & (px["time"] < DAY_END)].sort_values("ts")

    rows = []
    for d, g in px.groupby("date", sort=True):
        g = g.reset_index(drop=True)
        t = g["time"].to_numpy()
        h = g["High"].to_numpy(dtype=float)
        l = g["Low"].to_numpy(dtype=float)
        c = g["Close"].to_numpy(dtype=float)
        idx = np.nonzero(t <= WATCH_END)[0]
        for i in idx:
            if i + 1 >= len(c):
                continue
            nl, ol = barrier_outcome(h[i + 1:], l[i + 1:], c[i + 1:], c[i], 1)
            ns, os_ = barrier_outcome(h[i + 1:], l[i + 1:], c[i + 1:], c[i], -1)
            rows.append({"date": d, "minute": t[i].strftime("%H:%M"),
                         "net_long": nl, "out_long": ol,
                         "net_short": ns, "out_short": os_})
    return pd.DataFrame(rows)


def main():
    print("步驟 1／3：用 ±100 規則算出每個時刻的實際結果…")
    out = build_outcomes()
    print(f"  {len(out):,} 筆（{out['date'].nunique()} 天）")
    vc = out["out_long"].value_counts(normalize=True) * 100
    print(f"  做多結果分布：+100 先到 {vc.get('tp', 0):.1f}%　"
          f"-100 先到 {vc.get('sl', 0):.1f}%　收盤未觸及 {vc.get('none', 0):.1f}%\n")

    print("步驟 2／3：合併特徵…")
    feat = pd.read_csv(MATRIX)
    feat = feat[feat["minute"] <= "09:30"]
    df = feat.merge(out, on=["date", "minute"], how="inner")
    df["min_idx"] = df["minute"].map(lambda s: int(s[:2]) * 60 + int(s[3:]))
    df["long_wins"] = (df["net_long"] > df["net_short"]).astype(int)
    print(f"  合併後 {len(df):,} 筆\n")

    print("步驟 3／3：走查驗證（只用當天之前的資料）…")
    all_days = sorted(df["date"].unique())
    test_days = all_days[WARMUP_DAYS:]
    X = df[FEATURES].to_numpy(dtype=float)
    dates = df["date"].to_numpy()
    mins = df["min_idx"].to_numpy()
    win = df["long_wins"].to_numpy()
    nl = df["net_long"].to_numpy()
    ns = df["net_short"].to_numpy()

    rows = []
    for n, d in enumerate(test_days):
        past, today = dates < d, dates == d
        for m in sorted(set(mins[today])):
            i = np.nonzero(today & (mins == m))[0]
            if not len(i):
                continue
            i = i[0]
            pool = past & (mins >= m - MINUTE_WINDOW) & (mins <= m + MINUTE_WINDOW)
            if pool.sum() < 100:
                continue
            Xp = X[pool]
            sd = Xp.std(axis=0)
            sd[sd == 0] = 1.0
            dist = np.sqrt((((Xp - X[i]) / sd) ** 2 * FEATURE_WEIGHT).sum(axis=1))
            pdates, order, seen, keep = dates[pool], np.argsort(dist), set(), []
            for j in order:
                if pdates[j] in seen:
                    continue
                seen.add(pdates[j])
                keep.append(j)
                if len(keep) >= K_NEIGHBOURS:
                    break
            if len(keep) < 30:
                continue
            rows.append({"date": d, "min_idx": int(m),
                         "p_long": float(win[pool][keep].mean()),
                         "long_wins": int(win[i]),
                         "net_long": nl[i], "net_short": ns[i]})
        if (n + 1) % 60 == 0:
            print(f"  {n + 1}/{len(test_days)} 天…")

    r = pd.DataFrame(rows)
    r.to_csv(OUT, index=False)
    print(f"\n共 {len(r):,} 次預測（{r['date'].nunique()} 天）→ {OUT.name}\n")

    print("=" * 72)
    print("【一】模型說做多會贏的時候，做多真的比較會贏嗎？")
    print("=" * 72)
    for lo, hi, name in [(0, .40, "看空 <40%"), (.40, .60, "沒方向 40~60%"), (.60, 1.01, "看多 >60%")]:
        s = r[(r["p_long"] >= lo) & (r["p_long"] < hi)]
        if len(s) < 50:
            continue
        b = s.groupby("date")["long_wins"].mean()
        p, se = b.mean(), b.std(ddof=1) / np.sqrt(len(b))
        mark = "★" if (p - 1.96 * se > 0.5 or p + 1.96 * se < 0.5) else "—"
        print(f"  {name:<14}{len(s):>6} 次 /{len(b):>4} 天　做多實際勝出 {p*100:5.1f}%"
              f"　95%[{(p-1.96*se)*100:5.1f},{(p+1.96*se)*100:5.1f}]　{mark}")

    print("\n" + "=" * 72)
    print("【二】每天一單（第一個達門檻就進場，±100 出場，已扣手續費）")
    print("=" * 72)
    print(f"  {'門檻':>6}{'有訊號':>8}{'勝率':>9}{'每筆':>10}{'95% 信賴區間':>22}{'總點數':>11}")
    print("  " + "-" * 64)
    for th in [0.52, 0.55, 0.58, 0.60, 0.65]:
        picks = []
        for d, g in r.groupby("date"):
            g = g.sort_values("min_idx")
            s = g[(g["p_long"] >= th) | (g["p_long"] <= 1 - th)]
            if s.empty:
                continue
            x = s.iloc[0]
            picks.append(x["net_long"] if x["p_long"] >= th else x["net_short"])
        a = np.array(picks)
        if len(a) < 30:
            continue
        se = a.std(ddof=1) / np.sqrt(len(a))
        mark = "★" if (a.mean() - 1.96 * se > 0 or a.mean() + 1.96 * se < 0) else "—"
        print(f"  {th:>6.2f}{len(a):>8}{(a>0).mean()*100:>8.1f}%{a.mean():>+9.2f}點"
              f"{f'[{a.mean()-1.96*se:+.2f}, {a.mean()+1.96*se:+.2f}]':>22}{a.sum():>+10.0f}{mark:>3}")

    print("\n  對照組（同樣每天一單，但完全不看模型）：")
    for name, pick in [("每天 09:05 固定做多", lambda g: ("long", g[g["min_idx"] == 545])),
                       ("每天 09:05 固定做空", lambda g: ("short", g[g["min_idx"] == 545]))]:
        vals = []
        for d, g in r.groupby("date"):
            side, s = pick(g)
            if s.empty:
                continue
            vals.append(s.iloc[0]["net_long"] if side == "long" else s.iloc[0]["net_short"])
        a = np.array(vals)
        if len(a) < 30:
            continue
        se = a.std(ddof=1) / np.sqrt(len(a))
        print(f"    {name}: {len(a)} 天　勝率 {(a>0).mean()*100:.1f}%　"
              f"每筆 {a.mean():+.2f} 點　95%[{a.mean()-1.96*se:+.2f}, {a.mean()+1.96*se:+.2f}]")


if __name__ == "__main__":
    main()
