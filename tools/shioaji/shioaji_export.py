# -*- coding: utf-8 -*-
"""
永豐 Shioaji：匯出台指期歷史 1 分 K → CSV（給 Claude 回測用）
=============================================================
用途：拉一段歷史的 1 分 K 線，存成 txf_1min.csv，然後把「CSV 檔」傳給 Claude。

【安全第一】
- 你的 API Key / Secret 是機密，**只填在你自己電腦這個檔案裡**。
- 千萬不要把 Key/Secret 貼給 Claude、也不要上傳到 GitHub。
- 只需要把跑出來的「txf_1min.csv」傳給我就好（那只是行情數字，不敏感）。

【事前準備】
1. 到永豐開通 Shioaji API：官網「API 專區」→ 申請/簽署 → 取得 API Key 與 Secret Key
   （只拉行情、不下單，不需要憑證 CA）
2. 安裝 Python 3，然後在終端機執行：  pip install shioaji pandas
3. 把下面 KEY / SECRET 換成你的，存檔後執行：  python shioaji_export.py
"""

import shioaji as sj
import pandas as pd

# ↓↓↓ 換成你自己的（跑完可以刪掉，別留著）↓↓↓
API_KEY = "貼上你的_API_KEY"
API_SECRET = "貼上你的_SECRET_KEY"

# 想要的歷史區間（越長統計越可靠，建議至少半年～一年）
START = "2024-08-01"
END = "2025-07-31"

def main():
    api = sj.Shioaji()  # 正式環境
    api.login(api_key=API_KEY, secret_key=API_SECRET)
    print("登入成功，抓取中…（資料多的話要等一下）")

    # 台指期近月連續（大台，最活躍、資料最乾淨；開盤首根K方向與微台一致）
    contract = api.Contracts.Futures.TXF.TXFR1

    kbars = api.kbars(contract, start=START, end=END)
    df = pd.DataFrame({**kbars})
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.sort_values("ts")
    out = "txf_1min.csv"
    df.to_csv(out, index=False)
    print(f"完成！已存 {len(df)} 根 K 線 → {out}")
    print("把這個 txf_1min.csv 傳給 Claude 就可以了。")

    api.logout()

if __name__ == "__main__":
    main()
