# scripts/：一次性／手動用的腳本

面板（live_panel.py）**不會載入**這裡的任何一支。這裡放的是研究、回測、補資料、舊版工具。

- 資料檔與共用模組都還在上一層 `tools/shioaji/`；每支開頭有兩行讓它照舊以為自己在上一層，
  所以用法跟搬家前一樣，只是路徑多一層 `scripts\`：
  `.venv\Scripts\python.exe tools\shioaji\scripts\backfill_min1.py --dry`
- ⛔ 很多支會**寫資料檔**（回填、重建歷史），跑之前先看檔頭說明、能 `--dry` 就先 `--dry`。
- 2026-11-05 從 `tools/shioaji/` 搬過來（健檢：讓面板資料夾只剩面板在用的程式）。
- `build_fast_hist.py` 沒搬：測試與 probe 會 import 它。
