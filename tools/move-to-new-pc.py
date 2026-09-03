# -*- coding: utf-8 -*-
"""
把這台電腦上「repo 裡沒有、但新電腦需要」的東西打包成一個 zip。

背景：程式碼全都在公開 repo（git clone 就有），但有一批東西**刻意不進版控** ——
真實交易紀錄、練習紀錄、API Key、憑證、還有幾個很大的歷史行情 csv。
這支就是把那一批收集起來，讓換電腦不必一個一個檔案找。

怎麼跑：
    .venv\\Scripts\\python.exe tools\\move-to-new-pc.py
    .venv\\Scripts\\python.exe tools\\move-to-new-pc.py --out D:\\        （指定放哪）
    .venv\\Scripts\\python.exe tools\\move-to-new-pc.py --no-secrets      （不含機密檔）

================================================================
⛔ 這個 zip 裡面有機密
================================================================
預設會包含 `tools/shioaji/.env`（永豐 API Key 與 Secret）與 `Sinopac.pfx`（憑證）。
**拿到這個包 ＝ 可以用他的帳戶下單。**

  - 只走實體管道（USB 隨身碟），**不要丟雲端硬碟、不要寄信、不要傳通訊軟體**
  - 新電腦解開之後，**把隨身碟上那份刪掉**
  - 不要放進任何 git repo（這支會擋住寫進 repo 資料夾裡）

不想冒這個險就加 `--no-secrets`，那兩個檔自己用別的方式帶。

================================================================
⛔ 刻意不打包的東西
================================================================
`REAL_ORDERS_ON` —— **那個檔存在就代表「真的會送出委託單」**。
新電腦應該先在演練模式跑順、確認報價與對帳都正常，再由本人手動建那個檔。
自動幫他複製過去，等於讓一台還沒驗證過的機器直接能動真錢。

`.venv/`         —— 裡面是絕對路徑，換機器一定壞，用 requirements.txt 重建
`cache/`         —— 永豐合約快取，會自己長回來（100MB 以上，帶了純浪費）
`__pycache__/`   —— 同上
`*.log`          —— 執行記錄，新電腦不需要
"""
import argparse
import datetime
import pathlib
import sys
import zipfile

REPO = pathlib.Path(__file__).resolve().parent.parent
SJ = REPO / "tools" / "shioaji"

# ── 要打包的東西。分類只是為了印出來給人看，實際都照原本的相對路徑塞進 zip。
#    第三欄：True ＝ 缺了就是紅字警告（面板會跑不起來或紀錄會不見）
GROUPS = [
    ("你的紀錄（repo 裡沒有，這是唯一一份）", [
        (SJ / "real_trades", "真實交易成績單（含你補的心得）", True),
        (SJ / "practice_trades", "練習交易紀錄", True),
        (SJ / "real_orders", "真實委託單原始 log", False),
        (SJ / "sim_orders", "演練委託單 log", False),
        (SJ / "replay_log", "Bar Replay 的判斷紀錄", False),
        (SJ / "morning_logs", "早盤記錄", False),
        (SJ / "review_cache.json", "回顧分頁的快取（缺了會自己重算，只是慢）", False),
        (SJ / "my_trades.json", "手機 App 匯出的舊交易（回顧分頁會讀）", False),
        (SJ / "my_trades_reviewed.csv", "對回盤面後的版本", False),
    ]),
    ("歷史行情（面板啟動就要用，缺了跑不起來）", [
        (SJ / "tmf_1min.csv", "微台 1 分 K —— K 線圖的來源", True),
        (SJ / "intraday.csv", "盤中特徵矩陣 —— 缺了 main() 直接退出", True),
        (SJ / "txf_1min.csv", "大台 1 分 K —— 啟動時算波動度基準要用", True),
    ]),
    ("研究用資料（不影響面板，帶著比較省事）", [
        (SJ / "index_1min.csv", "加權指數 1 分 K", False),
        (SJ / "barrier_results.csv", "±100 規則的走查驗證結果", False),
        (SJ / "walkforward_results.csv", "走查驗證結果", False),
        (SJ / "backtest_trades.csv", "回測交易明細", False),
    ]),
]

SECRETS = [
    (SJ / ".env", "永豐 API Key 與 Secret", True),
    (SJ / "Sinopac.pfx", "下單憑證", True),
]

SKIP_NAMES = {"__pycache__"}
SKIP_SUFFIX = {".log", ".pyc"}

README = """\
早盤儀表板 —— 新電腦安裝步驟
=====================================

這個包裡是「GitHub 上沒有」的東西。程式碼要另外從 GitHub 拿。

-------------------------------------------------
0. 先做這件事，不然後面全白做
-------------------------------------------------
永豐的 API Key 是**綁 IP** 的。換一台電腦、換一個網路 = 不同 IP，
會直接登入被拒。

先去永豐的 API 管理後台，把新電腦所在網路的對外 IP 加進去。
（家用網路的 IP 可能會變動，如果常常斷就要問永豐怎麼處理。）

建議：先確認新電腦連得上，再開始搬其他東西。

-------------------------------------------------
1. 裝 Python 3.12 與 Git
-------------------------------------------------
Python 要 3.12（shioaji 對版本挑）。安裝時記得勾「Add to PATH」。

-------------------------------------------------
2. 把程式碼抓下來
-------------------------------------------------
    git clone https://github.com/xd1104/trade-log.git
    cd trade-log

-------------------------------------------------
3. 建 Python 環境
-------------------------------------------------
    py -3.12 -m venv .venv
    .venv\\Scripts\\python.exe -m pip install -r requirements.txt

如果是 Windows 10，還要另外裝 WebView2 執行階段（Win11 內建）。
桌面 App 的視窗是靠它畫的。

-------------------------------------------------
4. 把這個包裡的 payload 資料夾整個蓋回去
-------------------------------------------------
把 payload\\ 底下的東西，照原本的資料夾結構複製到 trade-log\\ 裡面。
（payload\\tools\\shioaji\\... 就對應 trade-log\\tools\\shioaji\\...）

-------------------------------------------------
5. 先用演練模式確認一切正常
-------------------------------------------------
雙擊 tools\\shioaji\\start-panel.bat，或直接跑：

    .venv\\Scripts\\pythonw.exe tools\\shioaji\\panel_app.pyw

要確認的四件事：
    - 能登入永豐（登不進去 = 第 0 步的 IP 還沒處理好）
    - 報價會跳
    - K 線圖畫得出來，而且日期是對的
    - 「真實」分頁看得到你過去的交易紀錄與心得

這個階段面板是**演練模式**（不會送出任何委託單），可以放心亂按。

-------------------------------------------------
6. 確認都對了，才打開真實下單
-------------------------------------------------
在 tools\\shioaji\\ 底下自己建一個空檔案，檔名：

    REAL_ORDERS_ON

**這個檔存在 = 面板真的會送單。** 打包時刻意沒有幫你複製過去，
就是不希望一台還沒驗證過的機器直接能動真錢。

-------------------------------------------------
7. 補上兩個週邊
-------------------------------------------------
桌面捷徑：指向 .venv\\Scripts\\pythonw.exe，
          參數是 tools\\shioaji\\panel_app.pyw 的完整路徑，
          工作目錄設 tools\\shioaji。

每日排程：原本那台是每個工作日 14:10 跑 tools\\shioaji\\append_today.py
          （抓當天 1 分 K、併進 tmf_1min.csv、重算 intraday.csv）。
          沒有它的話歷史資料會停在搬家那天。

-------------------------------------------------
⛔ 最後：這個包裡有你的 API Key 與憑證
-------------------------------------------------
裝完之後，把隨身碟上這個 zip 刪掉。
不要放進任何 git repo，也不要留在雲端硬碟。
"""


def collect(path):
    """回傳 [(實體檔, zip 內的相對路徑)]；資料夾就整個走一遍。"""
    if not path.exists():
        return []
    if path.is_file():
        return [(path, path.relative_to(REPO))]
    out = []
    for f in sorted(path.rglob("*")):
        if not f.is_file():
            continue
        if any(part in SKIP_NAMES for part in f.parts) or f.suffix in SKIP_SUFFIX:
            continue
        out.append((f, f.relative_to(REPO)))
    return out


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None, help="zip 要放哪個資料夾（預設：桌面）")
    ap.add_argument("--no-secrets", action="store_true",
                    help="不要把 .env 與 Sinopac.pfx 打包進去")
    args = ap.parse_args()

    out_dir = pathlib.Path(args.out).resolve() if args.out \
        else pathlib.Path.home() / "Desktop"
    # ⛔ 絕對不可以把含機密的 zip 寫進 repo 裡 —— 一個手滑的 `git add .` 就推上公開網路了
    try:
        out_dir.relative_to(REPO)
        print(f"⛔ 不可以把這個包放進 repo 裡（{out_dir}）")
        print("   裡面有 API Key 與憑證，一次 `git add .` 就推上公開網路了。")
        print("   換一個 repo 外面的位置，例如桌面或隨身碟。")
        sys.exit(2)
    except ValueError:
        pass                                    # 不在 repo 裡面，正常
    if not out_dir.exists():
        print(f"⛔ 找不到這個資料夾：{out_dir}")
        sys.exit(2)

    groups = list(GROUPS)
    if args.no_secrets:
        print("－ 不含機密檔（--no-secrets）：.env 與 Sinopac.pfx 要自己另外帶\n")
    else:
        groups = groups + [("⛔ 機密（拿到就能用你的帳戶下單）", SECRETS)]

    items, missing, total = [], [], 0
    print(f"來源：{REPO}\n")
    for title, entries in groups:
        print(f"【{title}】")
        for path, desc, required in entries:
            found = collect(path)
            if not found:
                mark = "⛔ 缺" if required else "－ 沒有"
                print(f"  {mark}  {path.relative_to(REPO)}  （{desc}）")
                if required:
                    missing.append((path, desc))
                continue
            size = sum(f.stat().st_size for f, _ in found)
            total += size
            items += found
            n = f"{len(found)} 個檔" if len(found) > 1 else ""
            print(f"  OK  {path.relative_to(REPO)}  {human(size):>9}  {n}")
        print()

    # REAL_ORDERS_ON 一定要講出來，不然他會以為打包 = 全部都在
    if (SJ / "REAL_ORDERS_ON").exists():
        print("【刻意沒有打包】")
        print("  跳過  tools/shioaji/REAL_ORDERS_ON")
        print("        那個檔存在 ＝ 面板真的會送出委託單。")
        print("        新電腦請先用演練模式確認一切正常，再自己手動建一個。\n")

    if missing:
        print("⚠️  下面這些是必要的，但這台機器上找不到 —— 新電腦會跑不起來或紀錄會不見：")
        for path, desc in missing:
            print(f"      {path.relative_to(REPO)}（{desc}）")
        print()

    stamp = datetime.date.today().isoformat()
    zip_path = out_dir / f"trade-log-搬家包-{stamp}.zip"
    print(f"打包中（原始 {human(total)}，csv 壓得很好，實際會小很多）…")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f, rel in items:
            z.write(f, str(pathlib.Path("payload") / rel))
        z.writestr("新電腦安裝步驟.txt", README)

    print(f"\n好了：{zip_path}")
    print(f"      {len(items)} 個檔，壓縮後 {human(zip_path.stat().st_size)}")
    print("\n裡面附了一份「新電腦安裝步驟.txt」，照著做就行。")
    if not args.no_secrets:
        print("\n⛔ 這個包裡有你的 API Key 與憑證：")
        print("   只走隨身碟，不要丟雲端、不要寄信、不要傳通訊軟體。")
        print("   新電腦裝完之後，把隨身碟上那份刪掉。")


if __name__ == "__main__":
    main()
