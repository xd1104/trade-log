# -*- coding: utf-8 -*-
r"""
永豐 Shioaji：匯出台指期歷史 1 分 K → CSV（給回測用）
=============================================================
用途：拉一段歷史的 1 分 K 線，存成 txf_1min.csv。

【為什麼一週一週抓】
實測（2026-08-10）：kbars 的區間端點只要碰到非交易日（週末、假日），
整段會直接回 `404 Data not found` —— 不是沒資料，是端點無效。
例：2025-08-31（週日）單獨抓 → 404；2025-08-04~08-29 → 正常 22224 根。
所以切成「週一～週五」為單位，並對整週失敗的情況退回逐日抓、跳過休市日。

每週存一份到 cache/，中斷了可以重跑接續，已抓過的週會跳過、不重複消耗流量。

【安全第一】
- API Key / Secret 只填在同資料夾的 .env（已被 .gitignore 擋掉）。
- 不要把 Key/Secret 貼給 Claude、也不要上傳到 GitHub。
- 跑出來的 txf_1min.csv 只是行情數字，不敏感。

【執行】
  ..\..\.venv\Scripts\python.exe shioaji_export.py
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

# 想要的歷史區間
# 樣本瓶頸是「獨立的交易日數」，不是筆數 —— 每天只算 1 個獨立樣本。
# 等一年只多 250 天，但永豐的歷史至少有到 2020，直接往回抓比等有效率得多。
START = date(2020, 8, 1)
END = date.today()

HERE = Path(__file__).parent
CACHE = HERE / "cache"
OUT = HERE / "txf_1min.csv"


def week_ranges(start: date, end: date):
    """切成 [(週一, 週五), ...]，頭尾裁到 start/end，跳過整段落在週末的區塊。"""
    cur = start - timedelta(days=start.weekday())      # 該週週一
    while cur <= end:
        w_start = max(cur, start)
        w_end = min(cur + timedelta(days=4), end)      # 該週週五
        if w_start <= w_end:
            # 端點碰到週末會 404，往內縮到最近的平日
            while w_start.weekday() > 4:
                w_start += timedelta(days=1)
            while w_end.weekday() > 4:
                w_end -= timedelta(days=1)
            if w_start <= w_end:
                yield w_start, w_end
        cur += timedelta(days=7)


def _fetch(api, contract, a: date, b: date) -> pd.DataFrame:
    df = pd.DataFrame({**api.kbars(contract, start=str(a), end=str(b))})
    if not df.empty:
        df["ts"] = pd.to_datetime(df["ts"])
    return df


def fetch_week(api, contract, w_start: date, w_end: date) -> pd.DataFrame:
    """抓一週 1 分 K；有 cache 就直接讀。整週失敗就退回逐日抓、跳過休市日。"""
    cache_file = CACHE / f"txf_1min_{w_start:%Y-%m-%d}.csv"
    if cache_file.exists():
        return pd.read_csv(cache_file)

    try:
        df = _fetch(api, contract, w_start, w_end)
    except Exception as e:
        # 整週失敗多半是區間內某個端點是假日 → 逐日抓，休市日直接跳過
        print(f"  {w_start:%Y-%m-%d} 整週失敗（{str(e)[-24:]}），改逐日抓…")
        days = []
        d = w_start
        while d <= w_end:
            try:
                one = _fetch(api, contract, d, d)
                if not one.empty:
                    days.append(one)
            except Exception:
                pass  # 休市日
            d += timedelta(days=1)
            time.sleep(0.5)
        df = pd.concat(days, ignore_index=True) if days else pd.DataFrame()

    if df.empty:
        print(f"  {w_start:%Y-%m-%d}  整週無資料（可能整週休市）")
        return df

    df = df.sort_values("ts")
    cache_file.parent.mkdir(exist_ok=True)
    df.to_csv(cache_file, index=False)
    print(f"  {w_start:%Y-%m-%d}  {len(df):>6} 根 K（{df['ts'].dt.date.nunique()} 個交易日）")
    return df


def main():
    API_KEY, API_SECRET = get_credentials()
    api = sj.Shioaji()  # 正式環境（只讀資料、免憑證）
    api.login(api_key=API_KEY, secret_key=API_SECRET)
    print(f"登入成功。抓取 {START} ~ {END} 的台指期 1 分 K…\n")

    # 台指期近月連續（大台，最活躍、資料最乾淨；開盤首根K方向與微台一致）
    contract = api.Contracts.Futures.TXF.TXFR1

    frames = []
    for w_start, w_end in week_ranges(START, END):
        try:
            df = fetch_week(api, contract, w_start, w_end)
            if not df.empty:
                frames.append(df)
        except Exception as e:
            print(f"  {w_start:%Y-%m-%d}  失敗：{e}")
            print("  （若是流量上限，明天再跑一次，已抓到的週會自動跳過。）")
            break
        time.sleep(0.5)  # 對伺服器客氣一點

    api.logout()

    if not frames:
        print("\n什麼都沒抓到，看上面的錯誤訊息。")
        return

    all_df = pd.concat(frames, ignore_index=True)
    all_df["ts"] = pd.to_datetime(all_df["ts"])
    all_df = all_df.drop_duplicates(subset="ts").sort_values("ts")
    all_df.to_csv(OUT, index=False)

    print(f"\n完成！{len(all_df)} 根 K 線 → {OUT}")
    print(f"實際涵蓋：{all_df['ts'].min()} ~ {all_df['ts'].max()}")
    print(f"交易日數：{all_df['ts'].dt.date.nunique()} 天")


if __name__ == "__main__":
    main()
