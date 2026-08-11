r"""
抓加權指數（IX0001）1 分 K → index_1min.csv
=============================================================================
用途：測試「期貨與現貨的價差（基差）」對趨勢預測有沒有幫助。

【已知限制】永豐的指數歷史只有到 2026-01 左右（約 150 個交易日），
比微台的 506 天短很多。所以基差特徵只能用來看「有沒有明顯效果」，
細微的貢獻在這個樣本數下測不出來。

【踩過的坑】api.Contracts.Indexs 只能用屬性存取（.TSE），
用中括號 ['TSE'] 會 KeyError；而且合約清單是非同步下載的，要等。

執行：
  ..\..\.venv\Scripts\python.exe fetch_index.py
"""

import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import shioaji as sj

from _config import get_credentials

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).parent
CACHE = HERE / "cache" / "IDX"
OUT = HERE / "index_1min.csv"
START = date(2025, 12, 1)      # 往前多抓一點，沒有就是沒有
END = date.today()


def get_index(api):
    for _ in range(30):
        try:
            lst = list(api.Contracts.Indexs.TSE)      # 只能用屬性，不能用 ['TSE']
            if lst:
                return [c for c in lst if c.code == "IX0001"][0]
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("取不到指數合約")


def week_ranges(start, end):
    cur = start - timedelta(days=start.weekday())
    while cur <= end:
        a, b = max(cur, start), min(cur + timedelta(days=4), end)
        while a.weekday() > 4:
            a += timedelta(days=1)
        while b.weekday() > 4:
            b -= timedelta(days=1)
        if a <= b:
            yield a, b
        cur += timedelta(days=7)


def main():
    key, secret = get_credentials()
    api = sj.Shioaji()
    api.login(api_key=key, secret_key=secret)
    contract = get_index(api)
    print(f"商品：{contract.code} {contract.name}\n")

    CACHE.mkdir(parents=True, exist_ok=True)
    frames = []
    for a, b in week_ranges(START, END):
        f = CACHE / f"idx_{a:%Y-%m-%d}.csv"
        if f.exists():
            frames.append(pd.read_csv(f))
            continue
        try:
            df = pd.DataFrame({**api.kbars(contract, start=str(a), end=str(b))})
        except Exception:
            df = pd.DataFrame()
            for d in [a + timedelta(days=i) for i in range((b - a).days + 1)]:
                try:
                    one = pd.DataFrame({**api.kbars(contract, start=str(d), end=str(d))})
                    if len(one):
                        df = pd.concat([df, one], ignore_index=True)
                except Exception:
                    pass
                time.sleep(0.3)
        if df.empty:
            print(f"  {a:%Y-%m-%d}  無資料")
            continue
        df["ts"] = pd.to_datetime(df["ts"])
        df = df.sort_values("ts")
        df.to_csv(f, index=False)
        print(f"  {a:%Y-%m-%d}  {len(df):>5} 根（{df['ts'].dt.date.nunique()} 天）")
        frames.append(df)
        time.sleep(0.4)

    api.logout()
    if not frames:
        print("什麼都沒抓到。")
        return
    all_df = pd.concat(frames, ignore_index=True)
    all_df["ts"] = pd.to_datetime(all_df["ts"])
    all_df = all_df.drop_duplicates(subset="ts").sort_values("ts")
    all_df.to_csv(OUT, index=False)
    print(f"\n完成！{len(all_df):,} 根 → {OUT.name}")
    print(f"涵蓋 {all_df['ts'].min().date()} ~ {all_df['ts'].max().date()}，"
          f"{all_df['ts'].dt.date.nunique()} 個交易日")


if __name__ == "__main__":
    main()
