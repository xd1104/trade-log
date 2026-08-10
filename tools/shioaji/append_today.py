r"""
每日收盤後：把今天的 1 分 K 併進歷史資料並重算模型
=============================================================================
排程每個交易日 14:10 執行（日盤 13:45 收盤後，當天資料才完整）。

流程：
  1. 抓今天的 1 分 K
  2. 併進 txf_1min.csv（已經有就跳過，不會重複）
  3. 重新產生 intraday.csv（模型的歷史矩陣）
  4. 印出前後對照，讓人一眼看出樣本多了幾天、關鍵數字有沒有變

執行：
  ..\..\.venv\Scripts\python.exe append_today.py
"""

import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).parent
PX = HERE / "txf_1min.csv"
MATRIX = HERE / "intraday.csv"
REPORT = HERE / "daily_reports"

SESSION_OPEN = pd.Timestamp("08:45").time()
WATCH_END = pd.Timestamp("09:30").time()
DAY_END = pd.Timestamp("13:45").time()


def snapshot_stats():
    """抓幾個關鍵數字，用來對照併入新資料前後有沒有變化。"""
    if not MATRIX.exists():
        return None
    d = pd.read_csv(MATRIX)
    w = d[d["minute"] <= "09:30"]
    if w.empty:
        return None

    def up_rate(sub):
        if sub.empty:
            return None
        byday = sub.assign(u=(sub["fwd10_n"] > 0).astype(int)).groupby("date")["u"].mean()
        return round(float(byday.mean()) * 100, 2)

    # 門檻用「幾倍日常波動」，不用絕對點數 —— 這樣跨年份才可比
    return {
        "days": int(d["date"].nunique()),
        "up_after_drop": up_rate(w[w["mom5_n"] <= -0.15]),
        "up_after_rise": up_rate(w[w["mom5_n"] >= 0.15]),
        "up_flat": up_rate(w[w["mom5_n"].abs() < 0.05]),
    }


def main():
    today = date.today()
    print(f"=== 每日資料併入　{today} ===\n")

    before = snapshot_stats()

    px = pd.read_csv(PX)
    px["ts"] = pd.to_datetime(px["ts"])
    have = set(px["ts"].dt.date.astype(str))
    if str(today) in have:
        print(f"{today} 已經在資料裡了，不重複併入。")
        print("（若要重算模型，直接跑 build_intraday.py）")
        return

    import shioaji as sj
    from _config import get_credentials

    api_key, secret = get_credentials()
    api = sj.Shioaji()
    api.login(api_key=api_key, secret_key=secret)
    contract = api.Contracts.Futures.TXF.TXFR1
    try:
        new = pd.DataFrame({**api.kbars(contract, start=str(today), end=str(today))})
    except Exception as e:
        print(f"抓不到 {today} 的資料：{e}")
        print("（休市日就是這個結果，正常。）")
        api.logout()
        return
    finally:
        try:
            api.logout()
        except Exception:
            pass

    if new.empty:
        print(f"{today} 沒有資料（休市日）。")
        return

    new["ts"] = pd.to_datetime(new["ts"])
    day_bars = new[(new["ts"].dt.time >= SESSION_OPEN) & (new["ts"].dt.time < DAY_END)]
    if day_bars.empty:
        print(f"{today} 沒有日盤 K 棒（休市日）。")
        return

    merged = pd.concat([px, new], ignore_index=True)
    merged = merged.drop_duplicates(subset="ts").sort_values("ts")
    merged.to_csv(PX, index=False)
    print(f"併入 {len(new)} 根 K 線（其中日盤 {len(day_bars)} 根）")
    print(f"txf_1min.csv 現在共 {len(merged):,} 根\n")

    print("重算模型中…")
    r = subprocess.run([sys.executable, str(HERE / "build_intraday.py")],
                       capture_output=True, text=True, encoding="utf-8", cwd=HERE)
    if r.returncode != 0:
        print("重算失敗：")
        print(r.stderr[-1500:])
        return
    print(r.stdout[r.stdout.find("以下摘要"):] if "以下摘要" in r.stdout else r.stdout[-1200:])

    after = snapshot_stats()
    if before and after:
        print("\n=== 併入前後對照 ===")
        rows = [("樣本天數", before["days"], after["days"], "天"),
                ("急跌後10分鐘上漲", before["up_after_drop"], after["up_after_drop"], "%"),
                ("橫盤後10分鐘上漲", before["up_flat"], after["up_flat"], "%"),
                ("急漲後10分鐘上漲", before["up_after_rise"], after["up_after_rise"], "%")]
        for name, b, a, unit in rows:
            delta = a - b
            flag = "" if abs(delta) < (1 if unit == "%" else 0.5) else "  ← 有變動"
            print(f"  {name:<18} {b:>7} → {a:>7} {unit}   ({delta:+.2f}){flag}")

        REPORT.mkdir(exist_ok=True)
        (REPORT / f"{today}.json").write_text(
            json.dumps({"date": str(today), "before": before, "after": after,
                        "ran_at": datetime.now().isoformat(timespec="seconds")},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n報告已存 → daily_reports/{today}.json")

    print("\n提醒：一天只會多 1 天樣本，數字通常不會有肉眼可見的變化。")
    print("真正看得出差別大概要累積一兩個月以上。")


if __name__ == "__main__":
    main()
