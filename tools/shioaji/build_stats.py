r"""
建立「歷史基準表」：各種開盤情境下，順勢做的後續統計 → stats.json
=====================================================================
給 morning_live.py 在盤中即時對照用。

每一項都會附上：樣本數、點估計、95% 信賴區間、以及「是否統計顯著」。
顯著性判定：期望值的 95% CI 若跨過 0，就標記為「不顯著」——
代表這個數字無法證明有優勢，只能當描述、不能當勝率用。

執行：
  ..\..\.venv\Scripts\python.exe build_stats.py
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

CSV = Path(__file__).with_name("txf_1min.csv")
OUT = Path(__file__).with_name("stats.json")

POINT_VALUE = 10
FEE_POINTS = 50 / POINT_VALUE      # 來回手續費 = 5 點
TP = SL = 100.0                    # 對應 Benson 慣用的 ±100

SESSION_START = pd.Timestamp("08:45").time()
SESSION_END = pd.Timestamp("13:45").time()
OPEN_TIME = pd.Timestamp("09:00").time()
ENTRY_TIME = pd.Timestamp("09:05").time()


def build_days() -> pd.DataFrame:
    df = pd.read_csv(CSV)
    df["ts"] = pd.to_datetime(df["ts"])
    df["date"] = df["ts"].dt.date
    df["time"] = df["ts"].dt.time
    df = df[(df["time"] >= SESSION_START) & (df["time"] < SESSION_END)].sort_values("ts")

    rows = []
    prev_close = None
    for d, g in df.groupby("date", sort=True):
        first5 = g[(g["time"] >= OPEN_TIME) & (g["time"] < ENTRY_TIME)]
        after = g[g["time"] >= ENTRY_TIME]
        if len(first5) < 5 or after.empty:
            prev_close = g["Close"].iloc[-1] if not g.empty else prev_close
            continue

        k_open = first5["Open"].iloc[0]
        k_close = first5["Close"].iloc[-1]
        k_high = first5["High"].max()
        k_low = first5["Low"].min()
        direction = 1 if k_close > k_open else -1 if k_close < k_open else 0
        entry = after["Open"].iloc[0]

        gross = None
        if direction != 0:
            tp_p, sl_p = entry + direction * TP, entry - direction * SL
            for h, l, c in after[["High", "Low", "Close"]].to_numpy():
                hit_tp = h >= tp_p if direction == 1 else l <= tp_p
                hit_sl = l <= sl_p if direction == 1 else h >= sl_p
                if hit_tp and hit_sl:
                    gross = -SL          # 保守：同棒模糊算停損
                    break
                if hit_tp:
                    gross = TP
                    break
                if hit_sl:
                    gross = -SL
                    break
            if gross is None:
                gross = direction * (after["Close"].iloc[-1] - entry)

        rows.append({
            "date": str(d),
            "dir": direction,
            "gap": (k_open - prev_close) if prev_close is not None else np.nan,
            "body": abs(k_close - k_open),
            "range": k_high - k_low,
            "vol5": first5["Volume"].sum(),
            "net": (gross - FEE_POINTS) if gross is not None else np.nan,
            # 後續走勢（純描述用）
            "day_high_after": after["High"].max() - entry,
            "day_low_after": after["Low"].min() - entry,
        })
        prev_close = g["Close"].iloc[-1]

    out = pd.DataFrame(rows)
    # 量能基準：前 20 日首根 5 分 K 量的移動中位數
    out["vol5_ref"] = out["vol5"].rolling(20).median().shift(1)
    out["vol_ratio"] = out["vol5"] / out["vol5_ref"]
    return out


def stat(t: pd.DataFrame, name: str) -> dict:
    t = t.dropna(subset=["net"])
    n = len(t)
    if n < 5:
        return {"name": name, "n": n, "enough": False}
    mean = t["net"].mean()
    se = t["net"].std(ddof=1) / np.sqrt(n)
    lo, hi = mean - 1.96 * se, mean + 1.96 * se
    win = (t["net"] > 0).mean()
    wse = np.sqrt(win * (1 - win) / n)
    return {
        "name": name,
        "n": n,
        "enough": True,
        "win_rate": round(win * 100, 1),
        "win_ci": [round(100 * (win - 1.96 * wse), 1), round(100 * (win + 1.96 * wse), 1)],
        "exp": round(mean, 1),
        "exp_ci": [round(lo, 1), round(hi, 1)],
        # CI 不跨 0 才算顯著
        "significant": bool(lo > 0 or hi < 0),
        "avg_max_fav": round(t["day_high_after"].abs().mean(), 0),
    }


def main():
    if not CSV.exists():
        print(f"找不到 {CSV}，請先跑 shioaji_export.py")
        return

    d = build_days()
    traded = d[d["dir"] != 0]

    buckets = [stat(traded, "全部（首根K有方向就順勢做）")]

    buckets.append(stat(traded[traded["dir"] == 1], "首根K收紅 → 做多"))
    buckets.append(stat(traded[traded["dir"] == -1], "首根K收黑 → 做空"))

    g = traded.dropna(subset=["gap"])
    buckets.append(stat(g[g["gap"] > 20], "跳空開高 > 20 點"))
    buckets.append(stat(g[g["gap"].abs() <= 20], "幾乎平盤開（±20 點內）"))
    buckets.append(stat(g[g["gap"] < -20], "跳空開低 > 20 點"))

    buckets.append(stat(traded[traded["body"] < 20], "首根K實體 < 20 點（小K）"))
    buckets.append(stat(traded[(traded["body"] >= 20) & (traded["body"] < 50)], "首根K實體 20~50 點"))
    buckets.append(stat(traded[traded["body"] >= 50], "首根K實體 ≥ 50 點（大K）"))

    v = traded.dropna(subset=["vol_ratio"])
    buckets.append(stat(v[v["vol_ratio"] >= 1.2], "首根K放量（≥ 近20日中位數 1.2 倍）"))
    buckets.append(stat(v[(v["vol_ratio"] > 0.8) & (v["vol_ratio"] < 1.2)], "首根K量能持平"))
    buckets.append(stat(v[v["vol_ratio"] <= 0.8], "首根K縮量（≤ 0.8 倍）"))

    sig = [b for b in buckets if b.get("significant")]

    payload = {
        "period": f"{d['date'].min()} ~ {d['date'].max()}",
        "n_days": int(len(d)),
        "tp": TP, "sl": SL, "fee_points": FEE_POINTS,
        "buckets": buckets,
        "any_significant": len(sig) > 0,
        "significant_names": [b["name"] for b in sig],
        # 描述性基準（沒有統計宣稱，純粹讓盤中知道「今天算不算異常」）
        "descriptive": {
            "gap_abs_median": round(float(d["gap"].abs().median()), 0),
            "gap_abs_p80": round(float(d["gap"].abs().quantile(0.8)), 0),
            "body_median": round(float(traded["body"].median()), 0),
            "body_p80": round(float(traded["body"].quantile(0.8)), 0),
            "range_median": round(float(traded["range"].median()), 0),
            "vol5_median": float(traded["vol5"].median()),
        },
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"樣本期間 {payload['period']}，{payload['n_days']} 個交易日\n")
    hdr = f"{'情境':<32}{'樣本':>5}{'勝率':>8}{'期望值':>9}{'95% 信賴區間':>18}   統計顯著"
    print(hdr)
    print("-" * len(hdr))
    for b in buckets:
        if not b.get("enough"):
            print(f"{b['name']:<32}{b['n']:>5}   樣本太少")
            continue
        mark = "★ 是" if b["significant"] else "— 否"
        ci = f"[{b['exp_ci'][0]:+.1f}, {b['exp_ci'][1]:+.1f}]"
        print(f"{b['name']:<32}{b['n']:>5}{b['win_rate']:>7.1f}%{b['exp']:>+8.1f}"
              f"{ci:>18}   {mark}")

    print()
    if sig:
        print(f"★ 有 {len(sig)} 個情境達統計顯著：{'、'.join(payload['significant_names'])}")
    else:
        print("★ 沒有任何情境達到統計顯著 —— 這些勝率數字都不能當作優勢使用。")
    print(f"\n→ {OUT.name}")


if __name__ == "__main__":
    main()
