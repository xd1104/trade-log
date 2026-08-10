r"""
回測：開盤首根 5 分 K 方向 → 順勢進場
=====================================================
規則（對應 Benson 實際打法）：
  1. 看 09:00~09:05 這根 5 分 K（由 09:00~09:04 五根 1 分 K 合成）
  2. 收紅（收 > 開）→ 做多；收黑 → 做空
  3. 09:05 進場（用 09:05 那根 1 分 K 的開盤價）
  4. 停利／停損 ±N 點，先碰到哪個算哪個；當日 13:45 前沒碰到就收盤平倉

【誠實聲明】這是歷史統計，不是預測，也不是買賣建議。
樣本只有 1 年，且台指期單一商品、單一時段 —— 勝率會隨行情型態改變。

【保守假設】
- 同一根 1 分 K 內同時觸及停利與停損時，一律算「停損」（無法得知先後）。
  報告會列出這種模糊 K 棒佔比，佔比高代表結果要打折看。
- 成本：來回手續費 NT$50，微台每點 NT$10 → 每筆固定扣 5 點。

執行：
  ..\..\.venv\Scripts\python.exe backtest_first5k.py
"""

import sys
from pathlib import Path

import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

CSV = Path(__file__).with_name("txf_1min.csv")
POINT_VALUE = 10          # 微台每點 NT$10
FEE_NTD = 50              # 來回手續費
FEE_POINTS = FEE_NTD / POINT_VALUE   # = 5 點

SESSION_START = pd.Timestamp("08:45").time()
SESSION_END = pd.Timestamp("13:45").time()
OPEN_TIME = pd.Timestamp("09:00").time()
ENTRY_TIME = pd.Timestamp("09:05").time()


def load_day_session() -> pd.DataFrame:
    df = pd.read_csv(CSV)
    df["ts"] = pd.to_datetime(df["ts"])
    df["date"] = df["ts"].dt.date
    df["time"] = df["ts"].dt.time
    # 只留日盤（夜盤不參與這個打法）
    df = df[(df["time"] >= SESSION_START) & (df["time"] < SESSION_END)]
    return df.sort_values("ts").reset_index(drop=True)


def build_days(df: pd.DataFrame) -> list[dict]:
    """每個交易日整理出：首根5分K、前一日收盤、進場價、以及進場後的 1 分 K。"""
    days = []
    prev_close = None
    for d, g in df.groupby("date", sort=True):
        first5 = g[(g["time"] >= OPEN_TIME) & (g["time"] < ENTRY_TIME)]
        after = g[g["time"] >= ENTRY_TIME]
        if len(first5) < 5 or after.empty:
            prev_close = g["Close"].iloc[-1] if not g.empty else prev_close
            continue

        rec = {
            "date": d,
            "k_open": first5["Open"].iloc[0],
            "k_high": first5["High"].max(),
            "k_low": first5["Low"].min(),
            "k_close": first5["Close"].iloc[-1],
            "prev_close": prev_close,
            "entry": after["Open"].iloc[0],
            "bars": after[["High", "Low", "Close"]].to_numpy(),
        }
        days.append(rec)
        prev_close = g["Close"].iloc[-1]
    return days


def simulate(day: dict, direction: int, entry: float, tp: float, sl: float):
    """回傳 (毛點數, 出場原因, 是否為同棒模糊)。direction: 1=多, -1=空"""
    tp_price = entry + direction * tp
    sl_price = entry - direction * sl
    for high, low, _close in day["bars"]:
        if direction == 1:
            hit_tp, hit_sl = high >= tp_price, low <= sl_price
        else:
            hit_tp, hit_sl = low <= tp_price, high >= sl_price
        if hit_tp and hit_sl:
            return -sl, "停損(同棒模糊)", True          # 保守：算停損
        if hit_tp:
            return tp, "停利", False
        if hit_sl:
            return -sl, "停損", False
    last_close = day["bars"][-1][2]
    return direction * (last_close - entry), "收盤平倉", False


def run(days, tp=100.0, sl=100.0, mode="close", gap_filter=None, body_min=None):
    """
    mode: 'close'      = 09:05 直接進場
          'breakout'   = 等突破首根 K 高/低點才進（沒突破就不做）
    gap_filter: None / 'up' / 'down'   （依開盤跳空方向篩選）
    body_min:   首根 K 實體至少幾點才做
    """
    trades = []
    for day in days:
        direction = 1 if day["k_close"] > day["k_open"] else -1 if day["k_close"] < day["k_open"] else 0
        if direction == 0:
            continue

        body = abs(day["k_close"] - day["k_open"])
        if body_min is not None and body < body_min:
            continue

        if gap_filter is not None:
            if day["prev_close"] is None:
                continue
            gap = day["k_open"] - day["prev_close"]
            if gap_filter == "up" and gap <= 0:
                continue
            if gap_filter == "down" and gap >= 0:
                continue

        entry = day["entry"]
        if mode == "breakout":
            trigger = day["k_high"] if direction == 1 else day["k_low"]
            entered = False
            for high, low, _c in day["bars"]:
                if (direction == 1 and high >= trigger) or (direction == -1 and low <= trigger):
                    entry = trigger
                    entered = True
                    break
            if not entered:
                continue

        gross, reason, fuzzy = simulate(day, direction, entry, tp, sl)
        trades.append({
            "date": day["date"],
            "dir": "多" if direction == 1 else "空",
            "gross": gross,
            "net": gross - FEE_POINTS,
            "reason": reason,
            "fuzzy": fuzzy,
            "body": body,
        })
    return pd.DataFrame(trades)


def summarize(t: pd.DataFrame, label: str) -> dict:
    if t.empty:
        return {"策略": label, "樣本": 0}
    wins = (t["net"] > 0).sum()
    return {
        "策略": label,
        "樣本": len(t),
        "勝率": f"{wins / len(t) * 100:.1f}%",
        "淨點數": f"{t['net'].sum():+.0f}",
        "每筆期望值": f"{t['net'].mean():+.1f}",
        "最大單筆賺": f"{t['net'].max():+.0f}",
        "最大單筆賠": f"{t['net'].min():+.0f}",
        "模糊棒": f"{t['fuzzy'].sum()}",
    }


def main():
    if not CSV.exists():
        print(f"找不到 {CSV}，請先跑 shioaji_export.py")
        return

    df = load_day_session()
    days = build_days(df)
    print(f"資料：{df['date'].min()} ~ {df['date'].max()}，可用交易日 {len(days)} 天")
    print(f"成本假設：來回手續費 NT${FEE_NTD} = {FEE_POINTS:.0f} 點／筆\n")

    base = run(days, tp=100, sl=100, mode="close")

    print("=" * 78)
    print("【一】核心問題：首根 5 分 K 順勢做，到底有沒有效？（±100 點，09:05 進場）")
    print("=" * 78)
    rows = [summarize(base, "全部順勢（紅→多、黑→空）")]
    rows.append(summarize(base[base["dir"] == "多"], "  只做多（收紅）"))
    rows.append(summarize(base[base["dir"] == "空"], "  只做空（收黑）"))
    # 逆勢對照組
    rev = base.copy()
    rev["net"] = -rev["gross"] - FEE_POINTS
    rows.append(summarize(rev, "（對照）反著做"))
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n" + "=" * 78)
    print("【二】停利／停損組合比較（順勢、09:05 進場）")
    print("=" * 78)
    combos = [(50, 50), (100, 100), (150, 150), (100, 50), (50, 100), (200, 100)]
    print(pd.DataFrame([
        summarize(run(days, tp=tp, sl=sl, mode="close"), f"停利{tp} / 停損{sl}")
        for tp, sl in combos
    ]).to_string(index=False))

    print("\n" + "=" * 78)
    print("【三】進場方式：09:05 直接進 vs 等突破首根K高/低點")
    print("=" * 78)
    print(pd.DataFrame([
        summarize(run(days, mode="close"), "09:05 直接進"),
        summarize(run(days, mode="breakout"), "等突破首根K高/低點"),
    ]).to_string(index=False))

    print("\n" + "=" * 78)
    print("【四】加條件篩選（±100 點、09:05 進場）")
    print("=" * 78)
    print(pd.DataFrame([
        summarize(run(days, mode="close"), "不篩選"),
        summarize(run(days, mode="close", gap_filter="up"), "只做開高（跳空向上）"),
        summarize(run(days, mode="close", gap_filter="down"), "只做開低（跳空向下）"),
        summarize(run(days, mode="close", body_min=30), "首根K實體 ≥ 30 點"),
        summarize(run(days, mode="close", body_min=50), "首根K實體 ≥ 50 點"),
        summarize(run(days, mode="close", body_min=80), "首根K實體 ≥ 80 點"),
    ]).to_string(index=False))

    print("\n" + "=" * 78)
    print("【五】分月看：這套規律是穩定的還是在退化？")
    print("=" * 78)
    b = base.copy()
    b["月"] = pd.to_datetime(b["date"]).dt.to_period("M").astype(str)
    monthly = b.groupby("月").agg(
        樣本=("net", "size"),
        勝率=("net", lambda s: f"{(s > 0).mean() * 100:.0f}%"),
        淨點數=("net", lambda s: f"{s.sum():+.0f}"),
        期望值=("net", lambda s: f"{s.mean():+.1f}"),
    )
    print(monthly.to_string())

    print("\n" + "=" * 78)
    print("【六】出場原因分布（±100 點）")
    print("=" * 78)
    print(base["reason"].value_counts().to_string())
    fuzzy_pct = base["fuzzy"].sum() / len(base) * 100
    print(f"\n同棒模糊（停利停損同一根 1 分 K 內都碰到，一律算停損）："
          f"{base['fuzzy'].sum()} 筆 / {len(base)} 筆 = {fuzzy_pct:.1f}%")
    if fuzzy_pct > 10:
        print("⚠️ 模糊佔比偏高，實際結果可能比表上好一些（也可能不會）。")

    base.to_csv(Path(__file__).with_name("backtest_trades.csv"), index=False)
    print(f"\n逐筆明細 → backtest_trades.csv")


if __name__ == "__main__":
    main()
