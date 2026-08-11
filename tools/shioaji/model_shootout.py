r"""
換更強的模型，這些客觀數字就能預測嗎？
=============================================================================
Benson 問：趨勢指數不能用那些客觀數字預測出來嗎？

先釐清：趨勢指數本來就是用那些客觀數字算的（mom5/mom15/ret_open/gap/rng/pos/vol_ratio）。
但先前只用了 kNN（找相似的歷史時刻）這種土法，有可能是「方法太弱」而不是「資料沒用」。

這支程式排除那個可能：拿同一批特徵、同一個目標（±100 誰先到），
換三種完全不同的模型跑走查驗證，看有沒有任何一種能贏過「什麼都不做」。

  1. kNN                 —— 現行做法
  2. 邏輯迴歸             —— 線性、穩健、不易過擬合
  3. 梯度提升樹           —— 能抓非線性與交互作用，是這類表格資料最強的一類
  4. 對照組：每天固定做多  —— 完全不看任何資料

驗證方式：擴張窗走查（只用當天之前的資料訓練），每天只下一單。
如果連梯度提升樹都贏不了「固定做多」，那就不是方法的問題，是資訊本身不存在。

執行：
  ..\..\.venv\Scripts\python.exe model_shootout.py
"""

import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).parent))
from live_panel import FEATURES  # noqa: E402

from sklearn.ensemble import HistGradientBoostingClassifier  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).parent
RETRAIN_EVERY = 20        # 每 20 個交易日重訓一次（每天重訓太慢，效果差異可忽略）
WARMUP_DAYS = 200         # 前 200 天只當訓練資料


def load():
    """把特徵與『±100 誰先到』的結果合起來。"""
    r = pd.read_csv(HERE / "barrier_results.csv")      # 已含 net_long / net_short
    f = pd.read_csv(HERE / "intraday.csv")
    f = f[f["minute"] <= "09:30"].copy()
    f["min_idx"] = f["minute"].map(lambda s: int(s[:2]) * 60 + int(s[3:]))
    d = f.merge(r[["date", "min_idx", "net_long", "net_short", "long_wins"]],
                on=["date", "min_idx"], how="inner")
    return d.sort_values(["date", "min_idx"]).reset_index(drop=True)


def daily_pnl(df, prob, threshold):
    """每天取第一個達門檻的時刻進場一次，回傳每日淨點數。"""
    df = df.copy()
    df["p"] = prob
    out = []
    for _, g in df.groupby("date"):
        g = g.sort_values("min_idx")
        s = g[(g["p"] >= threshold) | (g["p"] <= 1 - threshold)]
        if s.empty:
            continue
        x = s.iloc[0]
        out.append(x["net_long"] if x["p"] >= threshold else x["net_short"])
    return np.array(out)


def report(name, a):
    if len(a) < 30:
        print(f"  {name:<26} 樣本太少")
        return
    se = a.std(ddof=1) / np.sqrt(len(a))
    mark = "★" if (a.mean() - 1.96 * se > 0 or a.mean() + 1.96 * se < 0) else "—"
    print(f"  {name:<26}{len(a):>5} 天　勝率 {(a > 0).mean() * 100:5.1f}%　"
          f"每筆 {a.mean():+6.2f} 點　95%[{a.mean() - 1.96 * se:+6.2f},"
          f"{a.mean() + 1.96 * se:+6.2f}]　總計 {a.sum():+7.0f}　{mark}")


def main():
    d = load()
    days = sorted(d["date"].unique())
    print(f"資料：{len(d):,} 筆 / {len(days)} 天　目標：做多的 ±100 結果是否優於做空")
    print(f"訓練方式：擴張窗、每 {RETRAIN_EVERY} 天重訓，前 {WARMUP_DAYS} 天只當訓練資料\n")

    X = d[FEATURES].to_numpy(dtype=float)
    y = d["long_wins"].to_numpy()
    dates = d["date"].to_numpy()

    models = {
        "邏輯迴歸": lambda: LogisticRegression(max_iter=1000, C=1.0),
        "梯度提升樹": lambda: HistGradientBoostingClassifier(
            max_depth=3, max_iter=200, learning_rate=0.05,
            min_samples_leaf=40, l2_regularization=1.0, random_state=0),
    }
    preds = {k: np.full(len(d), np.nan) for k in models}

    test_days = days[WARMUP_DAYS:]
    for blk in range(0, len(test_days), RETRAIN_EVERY):
        chunk = test_days[blk:blk + RETRAIN_EVERY]
        train = np.isin(dates, days[:days.index(chunk[0])])
        test = np.isin(dates, chunk)
        if train.sum() < 500 or test.sum() == 0:
            continue
        sc = StandardScaler().fit(X[train])
        for name, make in models.items():
            m = make()
            m.fit(sc.transform(X[train]), y[train])
            preds[name][test] = m.predict_proba(sc.transform(X[test]))[:, 1]
        if (blk // RETRAIN_EVERY) % 5 == 0:
            print(f"  訓練到 {chunk[0]}…")

    mask = ~np.isnan(preds["邏輯迴歸"])
    sub = d[mask].reset_index(drop=True)
    print(f"\n實測期間：{sub['date'].min()} ~ {sub['date'].max()}（{sub['date'].nunique()} 天）\n")

    print("=" * 78)
    print("【一】各模型判斷方向的能力（AUC：0.5 = 等於丟銅板）")
    print("=" * 78)
    from sklearn.metrics import roc_auc_score
    for name in models:
        p = preds[name][mask]
        auc = roc_auc_score(sub["long_wins"], p)
        # 以天為單位 bootstrap，避免同一天的相鄰時刻被當成獨立樣本
        ds = sub["date"].to_numpy()
        uniq = np.unique(ds)
        rng = np.random.default_rng(0)
        boot = []
        for _ in range(300):
            pick = rng.choice(uniq, len(uniq), replace=True)
            idx = np.concatenate([np.nonzero(ds == u)[0] for u in pick])
            if len(np.unique(sub["long_wins"].to_numpy()[idx])) < 2:
                continue
            boot.append(roc_auc_score(sub["long_wins"].to_numpy()[idx], p[idx]))
        lo, hi = np.percentile(boot, [2.5, 97.5])
        mark = "★" if lo > 0.5 or hi < 0.5 else "—"
        print(f"  {name:<12} AUC {auc:.4f}　95%[{lo:.4f}, {hi:.4f}]　{mark}")

    print("\n" + "=" * 78)
    print("【二】實際每天下一單的損益（±100 出場、已扣手續費 5 點）")
    print("=" * 78)
    for name in models:
        for th in [0.52, 0.55, 0.60]:
            report(f"{name}（門檻 {th}）", daily_pnl(sub, preds[name][mask], th))
    print()
    first = sub.groupby("date").first()
    report("對照：每天固定做多", first["net_long"].to_numpy())
    report("對照：每天固定做空", first["net_short"].to_numpy())


if __name__ == "__main__":
    main()
