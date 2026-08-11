r"""
走查驗證（walk-forward）：面板的預測，真的準嗎？
=============================================================================
到目前為止所有數字都是「回頭看」算出來的 —— 用全部歷史去描述全部歷史。
那種數字必然好看，因為答案本來就在樣本裡。

這支程式做真正的測試：

    假裝回到某一天的某一分鐘，
    只用「那個時間點之前」的資料建模型，
    產生預測，再跟「後來實際發生的事」對答案。

模型完全不知道未來，跟盤中即時的處境一模一樣。

【看兩件事】
1. 鑑別力：模型說偏漲的時候，實際上漲的比例，有沒有高於它說偏跌的時候？
   沒有的話，這個面板就是裝飾品。
2. 校準度：模型說 60% 的那些時刻，實際上漲的是不是真的接近 60%？
   如果模型說 60% 但實際只有 50%，那它在說謊，數字不能照字面看。

執行：
  ..\..\.venv\Scripts\python.exe walkforward.py
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

MATRIX = Path(__file__).with_name("intraday.csv")
OUT = Path(__file__).with_name("walkforward_results.csv")

WARMUP_DAYS = 150          # 前 150 天只拿來當歷史，不評分
SAMPLE_EVERY = 3           # 每 3 分鐘取一個時間點（減少運算，不影響結論）
WATCH_ONLY = True          # 只驗證 Benson 實際下單的時段 08:45~09:30


def main():
    df = pd.read_csv(MATRIX)
    df["min_idx"] = df["minute"].map(lambda s: int(s[:2]) * 60 + int(s[3:]))
    if WATCH_ONLY:
        df = df[df["minute"] <= "09:30"]

    all_days = sorted(df["date"].unique())
    if len(all_days) <= WARMUP_DAYS + 20:
        print(f"樣本只有 {len(all_days)} 天，扣掉暖身期不夠驗證。")
        return
    test_days = all_days[WARMUP_DAYS:]
    print(f"歷史共 {len(all_days)} 天：前 {WARMUP_DAYS} 天當暖身，"
          f"驗證 {len(test_days)} 天（{test_days[0]} ~ {test_days[-1]}）")
    print(f"每 {SAMPLE_EVERY} 分鐘取樣一次，只看 08:45~09:30\n")

    X_all = df[FEATURES].to_numpy(dtype=float)
    dates = df["date"].to_numpy()
    mins = df["min_idx"].to_numpy()
    fwd10 = df["fwd10_n"].to_numpy(dtype=float)

    rows = []
    for n, d in enumerate(test_days):
        past = dates < d                        # 只用嚴格早於當天的資料
        today = dates == d
        for m in sorted(set(mins[today]))[::SAMPLE_EVERY]:
            i = np.nonzero(today & (mins == m))[0]
            if not len(i):
                continue
            i = i[0]

            pool = past & (mins >= m - MINUTE_WINDOW) & (mins <= m + MINUTE_WINDOW)
            if pool.sum() < 100:
                continue

            Xp = X_all[pool]
            sd = Xp.std(axis=0)
            sd[sd == 0] = 1.0
            dist = np.sqrt((((Xp - X_all[i]) / sd) ** 2 * FEATURE_WEIGHT).sum(axis=1))

            # 一天只取最相似的一刻（跟面板同樣的規則）
            pd_dates = dates[pool]
            order = np.argsort(dist)
            seen, keep = set(), []
            for j in order:
                if pd_dates[j] in seen:
                    continue
                seen.add(pd_dates[j])
                keep.append(j)
                if len(keep) >= K_NEIGHBOURS:
                    break
            if len(keep) < 30:
                continue

            pred = float((fwd10[pool][keep] > 0).mean())
            rows.append({"date": d, "min_idx": int(m), "pred": pred,
                         "actual_up": int(fwd10[i] > 0), "actual_move": fwd10[i],
                         "n_neighbours": len(keep)})
        if (n + 1) % 50 == 0:
            print(f"  已驗證 {n + 1}/{len(test_days)} 天…")

    r = pd.DataFrame(rows)
    r.to_csv(OUT, index=False)
    print(f"\n共 {len(r):,} 次預測（{r['date'].nunique()} 天）→ {OUT.name}\n")

    print("=" * 70)
    print("【一】鑑別力：模型看多的時候，真的比較會漲嗎？")
    print("=" * 70)
    bins = [(0, .40, "強烈偏跌 <40%"), (.40, .45, "偏跌 40~45%"),
            (.45, .55, "沒方向 45~55%"), (.55, .60, "偏漲 55~60%"),
            (.60, 1.01, "強烈偏漲 >60%")]
    print(f"  {'模型說':<16}{'次數':>7}{'實際上漲':>10}{'95% 信賴區間':>18}")
    print("  " + "-" * 52)
    for lo, hi, name in bins:
        s = r[(r["pred"] >= lo) & (r["pred"] < hi)]
        if len(s) < 30:
            continue
        # 以天為單位算誤差 —— 同一天的相鄰時刻不是獨立樣本
        byday = s.groupby("date")["actual_up"].mean()
        p = byday.mean()
        se = byday.std(ddof=1) / np.sqrt(len(byday))
        print(f"  {name:<16}{len(s):>7}{p * 100:>9.1f}%"
              f"{f'[{(p-1.96*se)*100:.1f}, {(p+1.96*se)*100:.1f}]':>18}")

    lo_g = r[r["pred"] < 0.45]
    hi_g = r[r["pred"] > 0.55]
    if len(lo_g) > 30 and len(hi_g) > 30:
        a = lo_g.groupby("date")["actual_up"].mean()
        b = hi_g.groupby("date")["actual_up"].mean()
        diff = b.mean() - a.mean()
        se = np.sqrt(a.std(ddof=1) ** 2 / len(a) + b.std(ddof=1) ** 2 / len(b))
        print(f"\n  看多組 減 看空組 = {diff * 100:+.1f} 個百分點"
              f"　95% 信賴區間 [{(diff - 1.96 * se) * 100:+.1f}, {(diff + 1.96 * se) * 100:+.1f}]")
        print(f"  → {'★ 有鑑別力，區間沒跨過 0' if diff - 1.96 * se > 0 else '— 沒有鑑別力，區間跨過 0'}")

    print("\n" + "=" * 70)
    print("【二】校準度：模型說幾 %，實際就是幾 % 嗎？")
    print("=" * 70)
    r["bucket"] = (r["pred"] * 20).round() / 20
    cal = r.groupby("bucket").agg(次數=("actual_up", "size"),
                                  實際=("actual_up", "mean")).reset_index()
    cal = cal[cal["次數"] >= 50]
    print(f"  {'模型預測':>10}{'實際上漲':>10}{'落差':>9}{'次數':>8}")
    print("  " + "-" * 37)
    for _, x in cal.iterrows():
        gap = x["實際"] - x["bucket"]
        print(f"  {x['bucket']*100:>9.0f}%{x['實際']*100:>9.1f}%{gap*100:>+8.1f}%{int(x['次數']):>8}")
    if len(cal) > 1:
        bias = (cal["實際"] - cal["bucket"]).mean() * 100
        print(f"\n  平均落差 {bias:+.1f} 個百分點"
              f"（正數代表模型太保守，負數代表模型太樂觀）")

    print("\n" + "=" * 70)
    print("【三】用它會賺錢嗎？（照模型方向做，持有 10 分鐘）")
    print("=" * 70)
    for th, name in [(0.55, "只在指數 >55 或 <45 時做"), (0.60, "只在指數 >60 或 <40 時做")]:
        s = r[(r["pred"] >= th) | (r["pred"] <= 1 - th)].copy()
        if len(s) < 50:
            continue
        s["dir"] = np.where(s["pred"] >= th, 1, -1)
        s["ret_n"] = s["dir"] * s["actual_move"]      # 單位：幾倍日常波動
        byday = s.groupby("date")["ret_n"].mean()
        m = byday.mean()
        se = byday.std(ddof=1) / np.sqrt(len(byday))
        print(f"  {name:<24} {len(s):>5} 次 / {len(byday):>3} 天"
              f"　每次 {m:+.4f} 倍日常波動"
              f"　95% [{m - 1.96 * se:+.4f}, {m + 1.96 * se:+.4f}]"
              f"　{'★' if (m - 1.96 * se > 0 or m + 1.96 * se < 0) else '—'}")
    print("\n  註：這裡沒扣手續費。日常波動約 400 點時，0.01 倍 ≈ 4 點；")
    print("      來回手續費是 5 點，所以每次至少要 +0.0125 倍才打平。")


if __name__ == "__main__":
    main()
