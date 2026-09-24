# 早盤儀表板・手機監控 📈

手機優先的 PWA：**遠端看早盤儀表板還活著沒、有沒有自動下單**（2026-09-24 起取代原本的「微台指交易日誌」）。
⛔ 只看不動 —— 這一頁沒有任何可以下單、開關自動下單的東西。

## 怎麼運作

1. 電腦上的面板每 2 分鐘把狀態拍一張快照（面板活著沒、日盤／夜盤開關、今天判斷、部位、帳戶），
   **加密**後推到這個 repo 的 `monitor` 分支（只留最新一份）。程式：`tools/shioaji/monitor_push.py`。
2. 手機打開 `https://xd1104.github.io/trade-log/`，輸入監控密碼解開來看。
3. ⚠️ repo 是**公開**的：推上去的只有密文，沒有密碼的人只看得到亂碼。

## 設定密碼（電腦上跑一次，Windows PowerShell）

```
& "C:\Users\Administrator\Desktop\claude\trade-log\.venv\Scripts\python.exe" "C:\Users\Administrator\Desktop\claude\trade-log\tools\shioaji\monitor_setup.py"
```

密碼不會存下來，只存「從密碼算出來的鑰匙」`tools/shioaji/MONITOR_KEY.json`（gitignore）。跑完重啟面板才會開始推。

## 燈號

- 灰點「面板正常運作中」：5 分鐘內有回報
- 金點「有點久沒回報／面板有狀況」：超過 5 分鐘，或這次有讀不到的東西
- 金色整張「面板沒有回報了」：超過 8 分鐘 —— 電腦睡著、斷網或面板關了

## 技術

純靜態：`index.html` + `css/monitor.css` + `js/monitor.js` + `sw.js`。解密用 WebCrypto
（PBKDF2-SHA256 → HMAC-SHA256 金鑰流 ＋ HMAC 驗證，跟 `monitor_push.py` 同一套，改要兩邊一起改）。
舊的交易日誌檔案（`js/app.js` 等）留在 repo 但已不再載入。
