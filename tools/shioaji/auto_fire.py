# -*- coding: utf-8 -*-
"""
自動下單 —— **程式自己在 09:03:30 送出委託單**的那一段。

================================================================
⛔⛔ 先讀這一段：這個檔跟這個專案其他所有東西都不一樣
================================================================
到 2026-09-09 為止，這個專案裡「會動到真錢」的路只有一條：`broker.py`，
而且是**他自己按著按鈕送出去的**（長按 650ms）。這個檔是第二條路，
差別在於：**他按的時候人在螢幕前面，這個檔送的時候他可能不在。**

⚠️ **永豐沒有停損單。停損活在面板的 Python 迴圈裡**（見 broker.py 檔頭與
   CLAUDE.md，那是 Benson 2026-08-28 拍板承擔的風險）。程式死掉／斷線／
   電腦睡著就沒有停損 —— 而自動下單會在他不在的時候把單送出去。
   **這個檔沒有辦法消除那個風險，只能盡量不要在不該送的時候送。**

================================================================
開關：`AUTO_ORDERS_ON`（⛔ 只有 Benson 自己建得出來）
================================================================
`tools/shioaji/AUTO_ORDERS_ON` —— 沿用 `REAL_ORDERS_ON` 那套精神：
**檔案存在才會做事，而且開發／測試／AI 一律不准建它。**

⭐ **開難、關易**（Benson 2026-09-09：「我用工具那邊關掉之後，才會失效」）：
   - **開**：⚠️ 2026-09-09 下午 Benson 要求「做在面板上，按個鈕就可以開始」之後，
     這裡多了第二條路：面板上的**兩段式**按鈕 → `POST /api/fire/on` →
     `live_panel.fire_arm_on()`（六道防護 ＋ 確認條 ＋ `O_CREAT|O_EXCL`）。
     他自己在硬碟上建這個檔（內容 A 或 B）**仍然有效**，兩條路都認同一個檔。
     ⛔⛔ 但**這個模組**（`auto_fire.py`）裡仍然沒有任何一行會建立 `ARM_FLAG`
     （`test_auto_fire.py` ⑬ 用 AST 在守，一個字都沒放寬）——
     **會送單的那個模組打不開自己的開關**，這是刻意留著的結構性保證。
     建檔的地方整個 repo 只有 `live_panel.fire_arm_on()` 一個。
   - **關**：面板上一顆按鈕 → `POST /api/fire/off` → `disarm()`，把開關檔**改名**收起來。
     關掉永遠是安全的動作，所以做成一鍵、不跳確認（沿用「平倉不跳確認」同一個道理）。
   - **開關沒有有效期**：建了就一直有效，每個交易日都送，直到他自己關掉。

⚠️ 他第一次建這個檔**八成會失敗**（lab-qa 實測 10 種寫法）：Notepad 另存選到
   「UTF-8 with BOM」、PowerShell 5.1 的 `"A" > 檔` 或 `Out-File`（UTF-16LE）——
   兩種都會讓他看到「我明明打了 A，畫面說看不懂」。所以 `_decode_flag()` 把 BOM
   與 UTF-16 都當**主流程**處理（CLAUDE.md：第一次一定會失敗的路徑要當成主流程做）。

- **檔案不存在** ⇒ 完全不送，畫面上寫「關閉中」。這是出貨狀態。
- **內容是 `A`** ⇒ 用「開盤快才做」（見下面〈2026-09-15 規則〉）。
- ⛔ **`B` 已經不支援**（2026-09-15 Benson 決定自動下單只剩 A）：寫 B ⇒ 拒絕下單，
  而且那句話要講清楚「B 已經不支援」，⛔ 不可以只說「看不懂」。
- **其他任何內容**（空的、`C`、`D`、`A B`、亂碼…）⇒ **拒絕下單並把讀到什麼講出來**。
  ⛔ 不准猜、不准挑一個預設值 —— 猜錯就是送出一口方向相反的單。
- **一次只能一個做法**。檔案裡只放一個字母，結構上就送不出兩口。

================================================================
⭐⭐ 2026-09-15 規則：「開盤衝得快才做」（⛔ 只有自動下單換規則）
================================================================
研究在 tick-research/scripts/benson_rule.py、exam25h2.py（事先登記、跑完不改）。
⛔ **手動真單、練習下單、【自動下單（模擬）】維持 ±130 不動** —— Benson 選的是只改自動下單。
  1. 09:03:30 那一刻（快照是 live_panel 主迴圈給的同一份，⛔ 不自己再讀一次價）：
     方向＝做法 A（09:03:30 的價 − 09:00 的價，≥0 做多）。
  2. **快不快**：`move_pct = |px − ref| / ref × 100`
       px  ＝ 09:03:30 那一刻的成交價（就是停損看的那個價）
       ref ＝ **09:00 以前最後一筆成交**（`minute_close[539]`），拿不到才退到
             09:00 那一分鐘第一筆（`minute_bar[540]["o"]`），用了哪一種記在 `ref_src`。
     門檻 ＝ 過去最近 `FAST_RULE["window"]`（40）個交易日（⛔ 不含今天）的 move_pct，
             `numpy.percentile(…, pctl)`（**80**，預設線性內插，跟研究同一個算法）。
             ⚠️ 百分位的正本是 `live_panel.FAST_PCTL`，經 `configure(pctl=…)` 接過來（`_CFG["pctl"]`）。
             ⚠️ 2026-09-15 下午由 70 改 80（Benson 拍板）：研究 17 組測試裡唯一過多重檢定門檻的是 80。
     少於 `FAST_RULE["min_n"]`（20）天 ⇒ **不送**（`no_hist`）。
     `move_pct >= 門檻` ⇒ 送；否則不送（`not_fast`），畫面寫出走了多少、門檻多少。
  3. 停利停損 ＝ **±0.5% of 09:03:30 的 px**：`pts = round(px × 0.005)`。
     停利掛券商（用實際成交價 ± pts，broker 既有行為）；停損 ⇒ **這一口部位自己帶
     `sl_points`**（`broker.enter(..., sl_points=pts)`），面板的停損迴圈讀它
     （手動真單沒有這個欄位 ⇒ 照舊用 SL_POINTS）。
     ⛔⛔ 重啟後從券商撿回來的部位沒有 `sl_points` ⇒ 會掉回 130（被提早洗掉）。
        所以撿回部位時用今天帳本那一列（`rec:"result"` ok）把它補回去 —— 見 `recover_meta()`。
  4. 歷史檔 `fast_hist.jsonl`（⛔ gitignore）：一天一列，**不管開關開不開、送不送**都寫，
     由工作執行緒寫（⛔ 不是主迴圈）；同一天不重寫（看檔案）。
     種子由 `build_fast_hist.py` 從研究的逐筆資料建。

⛔ **同時受 `REAL_ORDERS_ON` 管**：這個檔**不自己判斷要不要真的送出去**，
   一律走 `broker.enter()` ⇒ `broker._send()` ⇒ `broker.is_live()`。
   `REAL_ORDERS_ON` 不在 ⇒ 那一層自己就是 dry run（單子照組、內容照記、就是不送）。
   **他關掉真單就等於連自動也關掉，不需要記得關兩個。**
   ⛔ 不要在這個檔裡另外加一道 `REAL_FLAG.exists()` 判斷 —— 兩把尺遲早會分岔。
   （這也是刻意的：dry run 時整條路仍然照跑，他才驗得到「打開之後會送出什麼」。
     CLAUDE.md 有一條「演練模式必須跟正式模式行為一致，只差不送出去」。）

================================================================
下單一定要走 `broker.py`（⛔ 這個檔不重寫任何下單邏輯）
================================================================
`broker.py` 的每一道防呆都是踩過事故換來的（方向誤判、IOC 沒成交卻掛停利、
平倉送錯邊…）。自動下單就是「**在 09:03:30 呼叫既有的 `enter()`**」，其餘照舊：

- 口數寫死 1 口（`broker.QTY`）
- 一天上限 `broker.MAX_ENTRIES`（送單前 `broker.can_enter()` 會擋）
- 送單前 `can_enter()` 會跟券商對帳（沒報價／報價不新鮮／還沒連上永豐／
  對帳失敗／券商已有部位／當天已達上限，任何一項都擋下來）
- 停利掛在券商端（`enter()` 用**實際成交價**算 ± pts，pts ＝ round(09:03:30 的價 × 0.5%)）
- 停損由面板主迴圈監控（`live_panel.check_real_position`，讀這一口自己的 `sl_points`）

================================================================
一天只送一次，而且「送過了」看檔案不看記憶體
================================================================
看門狗重啟是常態（永豐 SDK 斷線會把整個行程帶掉）。所以：
- **決定做完就先落地一列**（`stage:"sending"`）**再呼叫 broker** ——
  送到一半當掉的話，重啟後 `_has()` 讀得到那一列 ⇒ **那天不會再送第二次**。
  代價是「可能送出去了卻沒記到結果」，那一列會停在 `sending`，畫面上寫
  「送出去了但不知道結果 —— 請自己到大戶投確認」。⛔ 寧可漏記，不可以重送。
- 落地一律 `open("a")` 只 append，跟 `autotest/` 同一招。

================================================================
資料落地：`tools/shioaji/autofire/YYYY-MM.jsonl`（⛔ 已 gitignore）
================================================================
⚠️ **刻意不跟【自動下單（模擬）】的 `autotest/` 混在同一個檔**，兩個理由：
 ① `autotest/` 有一條硬不變式 `sig+settle+miss+dup+bad ＝ 總列數`，
    混進第四種 `rec` 會讓那條式子與既有守衛整組失效。
 ② 模擬與真的送單是本質不同的兩種資料。這個專案已經吃過「取樣冒充逐筆」的虧
    （【細節】分頁），**「看得出來是哪一種」比「少一個檔」重要**。
對帳靠 `date` 欄位：兩邊都是一天一列、同一個 `signal_at`、同一把訊號的尺
（`auto_sig()` / `auto_dirs()` 是 `live_panel.py` 的正本，這裡用傳進來的那一份）。

================================================================
⛔⛔ 收盤自動平倉（2026-09-09 Benson 拍板）
================================================================
505 天裡有 **40 天（≈8%）走完一整天都沒碰到 ±100**（CLAUDE.md 的 `eod`）——
⇒ **大約每 12 個交易日就有一天會抱過夜盤，而他可能不在。**
而且【自動下單（模擬）】那一頁的成績是**假設 13:45 收盤平倉**在算的，
不平的話真單跟模擬根本對不起來。所以 `EOD_CLOSE_AT` 一到就走 `broker.close("eod")`。

⛔⛔ **絕對不可以平掉他自己開的單。** `broker` 只記一口部位，他手動進場而自動開關
   剛好開著的話，收盤那一下會把**他的**部位平掉 —— 那是絕對不可以發生的事。
   所以平之前要三道都對得上（`_looks_ours()` / `_already_closed()`）：
     ① 今天的帳本裡真的有一列「自動下單開出來的部位」（`rec:"result"` 且 `ok`）；
     ② 那一口**還沒被平掉** —— 面板的成績單（`broker.trades_today()`）與
        **券商自己的已實現損益**（`broker.realized_today(fresh=True)`）兩邊都查
        （面板當掉那段時間他在別處平掉的，只有券商那份看得到）。
        ⛔⛔ 這一道有**三種**答案：平掉了／還開著／**查不到**。「查不到」既不是
        「還開著」也不是「已經平掉了」，它自己一句話（`eod_cant_tell`）＋示警；
     ③ 現在券商上那口部位的**方向 ＋ 進場價 ＋ 進場時間 ＋ 口數**跟帳本那一列對得上
        （認人方式跟 `broker.set_trade_note()` 同一套：日期＋進場時間＋進場價；
        口數一定要是 `broker.QTY` —— 他加碼之後是 2 口，而 `close()` 會平到 0 口）。
   ⛔ 任何一項對不上就**不平**，並把「為什麼不平」寫在畫面上。
   ⚠️ 這是**刻意不對稱**的：誤判成「不是我的」最多是他多抱一晚（＝現況），
      誤判成「是我的」就是把他的單平掉。**寧可不平，不可以平錯。**

⛔ **不重寫任何平倉邏輯** —— 一律走 `broker.close()`（平倉前重新跟券商確認方向、
   確認部位真的消失才算成功、失敗重試與冷卻、拿不到 `_close_lock` 不排隊，
   那一整套都是踩過事故換來的）。這裡只負責「幾點、該不該平、平不掉要大聲講」。
"""
import json
import pathlib
import queue
import re
import threading
import time
from datetime import date, datetime

import numpy as np       # 門檻用 numpy.percentile（跟研究同一個算法，⛔ 不自己寫內插）

import broker            # ⛔ 唯一的下單出口。這個檔不自己組單、不自己送單

HERE = pathlib.Path(__file__).resolve().parent
# ⛔ 只有 Benson 自己建、或他在面板上按那顆兩段式的鈕（live_panel.fire_arm_on）；
#    ⛔⛔ 開發與測試**一律不准**把這個檔建出來（測試全部導到暫存區）。
ARM_FLAG = HERE / "AUTO_ORDERS_ON"
FIRE_DIR = HERE / "autofire"            # ⛔ 一定要 gitignore（含進場價與時間）
# 開盤走幅的歷史（一天一列）。⛔ 一定要 gitignore：裡面是每天 09:00／09:03:30 的價。
FAST_HIST = HERE / "fast_hist.jsonl"

# ⛔ 2026-09-15 起**只支援 A**（Benson 決定自動下單只剩「開盤快才做」）。
#    B（開盤起）不再是可下單的選項；C（要 30 點）與 D（不判斷）本來就只是【模擬】那一頁的對照組。
#    ⚠️ `live_panel.fire_arm_on()` 拿這個 tuple 驗 mode（⛔ 不自己寫一份），
#       所以面板上那顆「用 B 開始」按鈕自然也打不開了。
METHODS = ("A",)
# 畫面上的名字。⛔ 代號不准上畫面（他退件過一次：「我要從哪裡知道現在我看的是哪個做法？」）。
#    ⚠️ 名字從「5 分 K」改成「開盤快才做」：方向的算法沒變（仍是 09:00 那根起算），
#       但「今天做不做」多了一道快不快 —— 名字只寫方向的話，他會以為每天都送。
METHOD_NAME = {"A": "開盤快才做"}
METHOD_SUB = {"A": "09:00 起算"}

# ⭐ 「開盤快才做」的規則數字（前端從 state()["rule"] 拿，⛔ 不准寫死）。
#   window    過去幾個交易日（⛔ 不含今天）
#   min_n     少於幾天就不送（no_hist）
#   tpsl_frac 停利停損 ＝ 09:03:30 的價 × 這個比例（0.005 ＝ 0.5%）
# ⛔⛔ **百分位不在這裡**：正本是 `live_panel.FAST_PCTL`（80），經 `configure(pctl=…)` 接進 `_CFG["pctl"]`。
#    （2026-09-15 下午 Benson 拍板由 70 改 80；⛔ 不准在這個檔再寫一份預設值 —— 兩把尺。）
# ⚠️ 刻意收成一個 dict、⛔ 不寫成全大寫的數值常數：`tools/probe/leak-scan.py` 會把
#    CONST_SOURCES 模組層級「全大寫＝字面數字」的常數當成公開值，而**值等於產品常數的欄位
#    沒有作證資格**。20／40 正好落在他真實交易「點數」的尺度 ⇒ 註冊成常數就是替
#    那幾個點數開後門（CLAUDE.md ⑦c：「要盯的是常數清單變長，尤其是接近他紀錄尺度的數字」）。
#    收成 dict ⇒ leak-scan 讀不到 ⇒ 掃描維持嚴格（安全的那一邊）。
FAST_RULE = {"window": 40, "min_n": 20, "tpsl_frac": 0.005}
# 開關檔寫 B（或面板上有人送 mode=B）時的那句話。⛔ 正本只有這一份（arm() 與 fire_arm_on 共用）。
MSG_ONLY_A = "B 已經不支援，自動下單現在只有 A（開盤快才做）"

ARM_MAX_BYTES = 64          # 開關檔只讀這麼多 —— 有人不小心指到大檔也不會卡住
ARM_SHOW = 24               # 內容看不懂時，畫面上顯示前幾個字
LATE_MS = 5000              # 真的要送的那一刻，離 09:03:30 超過這麼久就不送
QUEUE_MAX = 4               # 主迴圈只 put_nowait；滿了寧可不送也不阻塞（那是他的停損）

# ── 收盤平倉 ──────────────────────────────────────────────────────
# 進場價要差在這個範圍內才算「同一口」。⛔ 跟 broker.trades_today() 補洞用的容差
# 同一個數（1.0 點）—— 兩把不一樣的尺會讓「這口是誰的」在兩個地方得到不同答案。
EOD_PX_TOL = 1.0
EOD_RETRY_S = 5.0           # 平不掉時隔多久再試（broker 自己還有 15 秒冷卻，會擋掉太密的）
# 從觸發那一刻算起，最多在這個窗口裡重試。⛔ 一定要在 13:45 收盤之前收工。
#
# ⚠️ **這裡的數字是實測的，⛔ 不是推的**（2026-09-09 lab-qa 退件 B1；舊版寫
#    「最壞兩輪 ≈59 秒」，那是紙上算的，跟實際跑出來的不一樣）。
#    量法：假券商「單送得出去但永遠不成交」（＝範圍市價 IOC 沒撮到），虛擬時鐘。
#    守衛：`test_auto_fire.py` ⑫⑬ 會把同一件事再量一次，數字對不上就紅。
#      ・一輪 close()：CLOSE_TRIES(3) 張 IOC，每張等 FILL_WAIT(5 秒) ⇒ 實測 t+0／
#        +4.8／+9.6 秒送出，這一輪約 14.4 秒後回報失敗
#      ・失敗後 CLOSE_COOLDOWN(15 秒)：這段時間裡 _eod 每 EOD_RETRY_S(5 秒) 叫一次
#        close()，但那幾次**當場被冷卻擋掉、一張單都沒送**
#      ・⇒ 每 ≈29.4 秒才真的送得出一輪
#    **實測（75 秒窗口）：close() 被呼叫 8 次、真的送出 9 張 IOC 平倉單、
#      三輪送單（t+0／+29.4／+58.8），最後一張在 t+68.4 秒＝13:44:38，
#      整段在 t+78.2 秒＝13:44:48 結束。**
#    ⇒ 窗口是 75 秒，但**最後一輪是在 75 秒之前開始的**，所以整段會略微超出窗口；
#      13:43:30 起算仍然全部落在 13:45 收盤之前 ✔（這個決定是對的，只是數字要照實寫）
EOD_WINDOW_S = 75.0
# 這幾種結局＝這一天不用再試了（看門狗在 13:43:30~13:45 之間重啟會重新觸發一次）。
# ⛔ `eod_failed` / `eod_unknown` / `eod_crashed` **不在這裡** —— 那幾種要能重試。
EOD_SETTLED = ("eod_closed", "eod_flat", "eod_no_entry", "eod_not_ours",
               "eod_done_elsewhere", "eod_unsure")

_MONTH_RE = re.compile(r"^\d{4}-\d{2}\.jsonl$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# ⛔ 每一種「沒送」都要有自己的一句話。**兩個不同的原因不准寫同一句** ——
#    寫同一句一定有一句是假的（【細節】的「加權 title」踩過同一個病）。
#    測試會斷言這裡的每一句都不一樣。
WHY = {
    "off": "自動下單是關著的（找不到開關檔）",
    "bad_method": "開關檔的內容看不懂，不知道要用哪個做法",
    "unreadable": "開關檔讀不出來（檔案壞了或沒有權限）",
    "not_wired": "面板沒有把自動下單接起來（設定沒跑到）",
    "late": "面板在 09:03:30 沒開著、或剛啟動還沒收到報價 —— 那天跳過，不補單",
    "no_quote": "09:03:30 收不到成交價",
    "quote_stale": "09:03:30 的報價太舊（斷線中），不能用舊價下單",
    "mid_only": "只有中價、還沒有成交，不能拿它當進場價",
    "no_signal": "拿不到 09:00 的參考價，算不出方向與開盤走幅",
    "no_trade": "這個做法今天判定不下單",
    # ── 2026-09-15「開盤快才做」的兩種不送（⛔ 跟上面每一句都不一樣）───────
    "not_fast": "今天開盤不夠快 —— 照規則今天不做",
    "no_hist": "過去的開盤走幅紀錄不夠多天，算不出「快」的門檻 —— 照規則不做",
    "cant_enter": "券商那一關擋下來了",
    "order_failed": "單送出去了，但沒有成交或被拒絕",
    "queue_full": "主迴圈丟不進佇列（前一件事還沒做完），這一天沒有送",
    "crashed": "決定送單之後程式中斷了，不知道那一張單的下場",
    # ── 收盤平倉（每一種結局也都要有自己的一句話）───────────────────
    "eod_closed": "收盤前已經把自動下單那一口平掉了",
    "eod_flat": "收盤前檢查：已經沒有部位了（停利成交，或先前就平掉了）",
    "eod_no_entry": "收盤前檢查：今天自動下單沒有開出部位，沒有東西要平",
    "eod_done_elsewhere": "收盤前檢查：自動下單那一口先前已經平掉了，現在這口不是它",
    # ⛔⛔ 這一句跟上面那句**必須分開**（2026-09-09 lab-qa 退件 M3）：
    #    「查不到」被寫成「已經平掉了」時，部位其實還開著 ⇒ 那是一句假話，
    #    而且它落在 EOD_SETTLED 裡、不示警 ⇒ 他抱過夜盤、13:45 起停損也停了，
    #    畫面卻告訴他已經平掉。
    "eod_cant_tell": "⚠️ 收盤前對不了帳，不知道自動下單那一口平掉了沒 —— "
                     "不敢動手，請自己到大戶投確認部位",
    "eod_not_ours": "⛔ 現在這口部位不是自動下單開的 —— 不碰它，要不要平請你自己決定",
    "eod_unknown": "收盤前跟券商對帳失敗，分不出這口是誰的 —— 不敢動，請自己確認",
    "eod_unsure": "今天那一張單停在「不知道下場」，收盤平倉不敢動手 —— 請自己確認",
    "eod_failed": "⚠️ 收盤平倉沒有成功 —— 部位還在，請立刻自己到大戶投平倉",
    "eod_crashed": "收盤平倉那一段自己出錯了 —— 請自己到大戶投確認部位",
    "eod_queue_full": "收盤平倉丟不進佇列（前一件事還沒做完），這一天沒有自動平倉",
}
# 這幾種是「他要立刻自己動手」的 —— 畫面上要跳出來，⛔ 不可以混在一般紀錄裡。
EOD_ALARM = ("eod_failed", "eod_unknown", "eod_unsure", "eod_crashed",
             "eod_not_ours", "eod_queue_full", "eod_cant_tell")

# 這個模組自己的狀態。⛔ 任何「只存在記憶體」的東西都不可以是唯一真相 ——
# 看門狗會重啟（CLAUDE.md 踩過三次），所以「今天送了沒」一律回去讀檔（_has）。
_ST = {
    "wired": False,        # configure() 跑過了沒
    "started": False,      # 送單執行緒起來了沒
    "err": None,           # 最後一次出錯的訊息（⛔ 要上畫面，不可以只留在記憶體）
    "err_n": 0,            # 出錯次數（永遠 0 也看不出來 ＝ 安靜地少）
    "last": None,          # 最後一次的結果（畫面用；真相仍然在檔案裡）
    "off_at": None,        # 最後一次從畫面上按「關閉」是什麼時候（真相是那個被改名的檔）
    "off_msg": None,
    # ── 開盤走幅歷史（⛔ 讀到壞列不可以安靜地少：計數 ＋ 主控台 ＋ 端點端出去）
    "hist_bad": 0,         # 最近一次讀 fast_hist.jsonl 時跳過的壞列數
    "hist_dup": 0,         # 同一天出現第二列（只認第一列）
    "hist_msg": None,      # 最近一次「今天那一列沒有寫」的原因
    # ── 重啟撿回部位時補停損點數（recover_meta／_recover_poll）
    "rec_msg": None,       # 最近一次補（或補不回來）的結果，畫面看得到
}

# 這些常數的**正本在 live_panel.py**，由 configure() 接過來。
# ⛔ 不要在這裡自己寫一份預設值 —— 兩把尺分岔的話，訊號時刻改了這裡不會跟著改，
#    而畫面上完全看不出來（【程式下單】的 R2 就是這個形狀）。
# ⚠️ 2026-09-15 拿掉 `tp`：自動下單的停利停損改成 ±0.5%（FAST_RULE），
#    ⛔ 不再吃 live_panel 的 TP_POINTS（那是手動真單／練習／模擬的 ±130）。
#    留著一個沒人用的 `tp` 會讓人以為自動下單還是 ±130。
# ⭐ `pctl`（2026-09-15 加）：「開盤快才做」門檻的百分位，正本 live_panel.FAST_PCTL（80）。
#    沒接（None）⇒ wired=False ⇒ 不送；fast_threshold() 拿不到也**不猜一個預設值**（丟例外）。
_CFG = {"signal_at": None, "signal_sec": None, "late_ms": None, "gap_s": None,
        "sig_fn": None, "dirs_fn": None, "eod_at": None, "pctl": None}

# 今天帳本裡「自動下單開出來的那一口」的記憶體副本（重啟撿回部位時補 sl_points 用）。
#   date  這份是哪一天的（⛔ 不是今天就不准拿來用 ⇒ 交給工作執行緒去讀檔）
#   entry `rec:"result"` ok 那一列（沒有就 None）
#   state `_auto_entry()` 的第二個回傳值（None＝有那一口；eod_no_entry／eod_unsure）
# ⛔ 真相永遠在檔案裡；這份只是讓 `recover_meta()`（跑在主迴圈的 reconcile 裡）不必碰磁碟。
#    寫入的地方只有三個：start() 開機讀一次、_result() 送成那一刻、_recover_poll() 讀檔。
_MEM = {"date": None, "entry": None, "state": None}
POLL_S = 0.5            # 送單執行緒沒事做時多久看一次「有沒有撿回來、還沒補停損的部位」

_Q = queue.Queue(maxsize=QUEUE_MAX)
# ⚠️ 收盤平倉走**自己的**佇列與執行緒：09:03:30 那一件跟 13:43:30 那一件差四個半小時，
#    但共用一條佇列的話，送單那邊萬一卡住（等成交最多 5 秒 × 3 次），收盤平倉就要排在
#    後面等 —— 而它的窗口只有 75 秒。⛔ 平倉不可以排在別人後面。
_EQ = queue.Queue(maxsize=QUEUE_MAX)


# ---------------------------------------------------------------- 開關

def _num(x):
    """數值欄位一律過這道。回 float 或 None（看不懂就 None，**不猜**）。
    bool 要另外擋：isinstance(True, int) 是 True。"""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    x = float(x)
    return x if x == x and -1e12 < x < 1e12 else None


def _clean(s):
    """開關檔的內容要顯示在畫面上 —— 只留看得見的字，長度切掉。"""
    s = "".join(c if c.isprintable() else "�" for c in str(s))
    return s[:ARM_SHOW] + ("…" if len(s) > ARM_SHOW else "")


def _decode_flag(b):
    """
    把開關檔的位元組變成字串。**這是「他第一次建這個檔」的主流程，不是邊角料。**

    ⛔ lab-qa 2026-09-09 實測 10 種他會用的建檔方式，有兩種會失敗：
      ・**Notepad「另存新檔」選到 UTF-8 with BOM** ⇒ 開頭多 `EF BB BF`
        ⇒ 舊版讀到的是 `"\\ufeffA"` ⇒ 畫面寫「讀到『﻿A』，只認得 A 或 B」
      ・**PowerShell 5.1 的 `"A" > 檔` 或 `Out-File`** ⇒ 整個檔是 **UTF-16LE**
        （`FF FE 41 00`）⇒ 舊版讀到 `"\\xff\\xfeA\\x00"`
    失敗的方向是安全的（不送單），但他會看到「我明明打了 A，畫面說看不懂」，
    然後以為是程式壞了。CLAUDE.md：**第一次使用一定會失敗的路徑要當成主流程做。**

    ⚠️ 只吃 BOM 與「內容裡有 NUL」這兩個**明確的訊號**，⛔ 不做編碼猜測 ——
       猜錯就是把一個看不懂的檔解讀成 A 或 B，那是會送出一口單的錯誤。
    """
    if not isinstance(b, (bytes, bytearray)):
        return str(b)
    b = bytes(b)
    if b[:3] == b"\xef\xbb\xbf":                      # UTF-8 with BOM（Notepad）
        return b.decode("utf-8-sig", "replace")
    if b[:2] in (b"\xff\xfe", b"\xfe\xff"):           # UTF-16 LE／BE（PowerShell 5.1）
        enc = "utf-16-le" if b[:2] == b"\xff\xfe" else "utf-16-be"
        body = b[2:]
        return body[:len(body) // 2 * 2].decode(enc, "replace")
    if b"\x00" in b:
        # 沒有 BOM 但夾著 NUL ⇒ 幾乎一定是 UTF-16（有些工具不寫 BOM）。
        # 兩個 endian 都試，⛔ 解不出可讀內容就照原樣走 UTF-8（不猜）。
        for enc in ("utf-16-le", "utf-16-be"):
            try:
                s = b[:len(b) // 2 * 2].decode(enc)
            except UnicodeDecodeError:
                continue
            if s.strip():
                return s
    return b.decode("utf-8", "replace")


def arm():
    """
    現在的開關狀態。**這是「要不要送」唯一的來源。**

    回傳 {"on", "method", "why", "msg", "raw"}：
      on=True  ⇒ method 一定是 "A" 或 "B"
      on=False ⇒ why 一定講得出原因（off／bad_method／unreadable）

    ⛔ 讀不懂就是讀不懂，**不准挑一個預設做法**。
    """
    try:
        if not ARM_FLAG.exists():
            return {"on": False, "method": None, "why": "off",
                    "msg": WHY["off"], "raw": None}
        # ⛔ 一定要走 _decode_flag：Notepad 的 BOM 與 PowerShell 的 UTF-16
        #    是他第一次建這個檔最可能的兩種寫法。
        raw = _decode_flag(ARM_FLAG.read_bytes()[:ARM_MAX_BYTES])
    except Exception as e:
        return {"on": False, "method": None, "why": "unreadable",
                "msg": WHY["unreadable"] + "：" + _clean(str(e)), "raw": None}
    txt = raw.strip().upper()
    if txt in METHODS:
        return {"on": True, "method": txt, "why": None,
                "msg": "用「%s」（%s）" % (METHOD_NAME[txt], METHOD_SUB[txt]),
                "raw": _clean(raw.strip())}
    if not txt:
        msg = WHY["bad_method"] + "：檔案是空的。要用請寫一個 A 進去"
    elif txt == "B":
        # ⛔⛔ 2026-09-15 起 B 不支援。他 09-09 以前開過 B 的話，檔案裡還是 B ⇒
        #    這句話一定要講清楚「是規則換了」，⛔ 不可以只說「看不懂」（他會以為檔案壞了）。
        msg = WHY["bad_method"] + "：讀到「B」。" + MSG_ONLY_A
    elif txt in ("C", "D"):
        # ⛔ C（要 30 點）與 D（不判斷）刻意不支援 —— 講清楚，不要只說「看不懂」
        msg = (WHY["bad_method"] + "：讀到「%s」。自動下單現在只有 A（%s），"
               "C 與 D 只有【自動下單（模擬）】那一頁在跑" %
               (_clean(txt), METHOD_NAME["A"]))
    else:
        msg = WHY["bad_method"] + "：讀到「%s」，只認得 A" % _clean(raw.strip())
    return {"on": False, "method": None, "why": "bad_method",
            "msg": msg, "raw": _clean(raw.strip())}


def flag_exists():
    """開關檔在不在。⚠️ 跟 `arm()["on"]` **不一樣**：內容看不懂時檔案還在
    （`on` 是 False，但畫面上那顆「關閉」鈕仍然該出現，不然他關不掉那個壞檔）。"""
    try:
        return ARM_FLAG.exists()
    except Exception:
        return False


def disarm():
    """
    ⭐ **關掉自動下單。這個函式只會關，永遠不會開。**

    它做的唯一一件事是「把 `ARM_FLAG` 移走」——
    ⛔ **這個模組**沒有任何一行會建立 `ARM_FLAG`，所以這條路結構上就打不開開關
       （`test_auto_fire.py` ⑬ 用 AST 在守）。
       ⚠️ 2026-09-09 更正：舊註解寫「整個 repo 的產品程式都沒有」——那句**已經不成立**
       （面板上那顆鈕會走 `live_panel.fire_arm_on()` 建檔）。真正還成立、也真正重要的
       是**會送單的這個模組**打不開自己的開關；建檔的入口整個 repo 只有那一個。

    **改名不刪掉**：他寫的那個字母留著（`AUTO_ORDERS_ON.off-YYYYmmdd-HHMMSS`），
    要再開的時候自己把檔名改回去就好；而那個檔名本身就是「幾點關的」的紀錄。
    ⚠️ 那個新檔名也要在 `.gitignore` 裡（`AUTO_ORDERS_ON*`）。
    """
    if not ARM_FLAG.exists():
        # ⛔ 這不是錯誤 —— 兩個視窗各按一次、或他本來就沒開，都會走到這裡。
        return True, "本來就是關著的（沒有 %s 這個檔）" % ARM_FLAG.name
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = ARM_FLAG.with_name(ARM_FLAG.name + ".off-" + stamp)
    try:
        ARM_FLAG.replace(dest)
    except Exception as e:
        msg = "關不掉：" + _clean(str(e))
        _ST["off_msg"] = msg
        print("⚠️ [自動下單] " + msg, flush=True)
        return False, msg
    msg = "已經關掉自動下單（原本的開關檔改名成 %s，內容留著）" % dest.name
    _ST["off_at"] = datetime.now().isoformat(timespec="seconds")
    _ST["off_msg"] = msg
    print("[自動下單] " + msg, flush=True)
    return True, msg


# ---------------------------------------------------------------- 落地

def _month_path(d):
    return FIRE_DIR / (str(d)[:7] + ".jsonl")


def _append(row):
    """⛔ 一定是 open("a")。看門狗重啟是常態，覆寫＝把當天稍早的紀錄弄丟。"""
    FIRE_DIR.mkdir(exist_ok=True)
    with _month_path(row["date"]).open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()
    return row


def _rows_of(d):
    """某一天已經寫過的列（原始，舊到新）。"""
    p = _month_path(d)
    if not p.exists():
        return []
    out = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if isinstance(o, dict) and o.get("date") == d:
                out.append(o)
    except Exception:
        return []
    return out


def _has(d):
    """
    這一天已經處理過了嗎。**看檔案，不看記憶體。**

    看門狗在 09:03:30 前後重啟時會重跑一次判斷 —— 沒有這道就會送出第二張單。
    """
    return bool(_rows_of(d))


def _skip(d, why, extra=None, msg=None):
    """沒送。**每一種都要落地、都要有原因**（畫面上看得到）。"""
    row = {"rec": "skip", "date": d, "why": why,
           "why_msg": msg or WHY.get(why, why),
           "live": broker.is_live(),
           "wrote_at": datetime.now().isoformat(timespec="seconds")}
    row.update(extra or {})
    _ST["last"] = row
    print("[%s] 自動下單：沒有送單 —— %s" % (d, row["why_msg"]), flush=True)
    return _append(row)


# ---------------------------------------------------------------- 開盤快不快（純函式）
#
# ⛔ 這幾支是規則的**正本**：`_fire()`、`state()`、`tools/probe/fast-rule-replay.py`
#    （離線逐日對照研究）全部呼叫同一份。⛔ 不准在別處再寫一份「>= 門檻」或「× 0.005」
#    —— 兩把尺的話，畫面說「快」、送單那邊卻判「不快」，而且看不出來。

def move_pct(px, ref):
    """今天開盤走幅（%）＝ |px − ref| / ref × 100。拿不到或 ref 不合理 ⇒ None（⛔ 不猜）。"""
    px, ref = _num(px), _num(ref)
    if px is None or ref is None or ref <= 0:
        return None
    return abs(px - ref) / ref * 100.0


def fast_pctl():
    """門檻用第幾百分位（正本 live_panel.FAST_PCTL，configure 接過來）。沒接 ⇒ None。"""
    return _CFG["pctl"]


def fast_threshold(past_moves, pctl=None):
    """
    過去的 move_pct（舊到新，⛔ 呼叫端負責排掉今天）⇒ (門檻 %, 用了幾天)。
    天數不夠 `min_n` ⇒ (None, 天數)。
    ⚠️ 取**最近** `window` 天；`numpy.percentile` 預設線性內插 —— 研究
       （benson_rule.py 的 `np.percentile(hist, …)`，hist 是 deque(maxlen=40)）同一個算法。
    `pctl` 不給 ⇒ 用 configure 接過來的 `_CFG["pctl"]`（80）。
    ⛔ 兩個都沒有 ⇒ **丟例外**（⛔ 不猜一個預設百分位：猜錯就是門檻整個換一把尺）。
    """
    q = _CFG["pctl"] if pctl is None else pctl
    if isinstance(q, bool) or not isinstance(q, (int, float)) or not (0 < q <= 100):
        raise ValueError("開盤快才做的百分位沒有接起來（pctl=%r）" % (q,))
    last = [float(m) for m in past_moves][-FAST_RULE["window"]:]
    if len(last) < FAST_RULE["min_n"]:
        return None, len(last)
    return float(np.percentile(last, q)), len(last)


def approx_points(pct, ref):
    """% 換成「約略點數」給畫面看（％ × ref / 100 取整）。⚠️ 只是換算，判斷一律用 %。"""
    pct, ref = _num(pct), _num(ref)
    if pct is None or ref is None:
        return None
    return int(round(pct * ref / 100.0))


def tpsl_points(px):
    """停利停損點數 ＝ round(09:03:30 的價 × 0.5%)。例：px=46000 ⇒ 230。拿不到價 ⇒ None。"""
    px = _num(px)
    if px is None or px <= 0:
        return None
    return int(round(px * FAST_RULE["tpsl_frac"]))


def fast_verdict(day, mv, hist_rows, pctl=None):
    """
    ⭐ 「今天快不快」唯一的判斷。`hist_rows` 是 `hist_read()` 的 rows（舊到新）。
    `pctl` 只給離線對照（fast-rule-replay.py 傳 live_panel.FAST_PCTL）用；面板一律不傳（走 _CFG）。
    回 {"verdict": "fast"|"slow"|"no_hist"|None, "thr_pct", "n", "move_pct"}。
      - 過去（date < day）天數不夠 ⇒ "no_hist"
      - mv 是 None ⇒ verdict None（算不出今天的走幅，⛔ 不是「不快」）
      - ⛔ **`>=` 算快**（研究：`abs(move) >= np.percentile(...)`）
    """
    past = [r["move_pct"] for r in (hist_rows or []) if r.get("date", "") < day]
    thr, n = fast_threshold(past, pctl)
    out = {"thr_pct": thr, "n": n, "move_pct": _num(mv)}
    if thr is None:
        out["verdict"] = "no_hist"
    elif out["move_pct"] is None:
        out["verdict"] = None
    else:
        out["verdict"] = "fast" if out["move_pct"] >= thr else "slow"
    return out


def hist_read(path=None):
    """
    讀 `fast_hist.jsonl` ⇒ (rows 依日期舊到新, 壞列數, 重複天數)。
    ⚠️ **只准在工作執行緒／HTTP 執行緒叫**（⛔ 主迴圈不准，這是磁碟 I/O）。
    ⛔ 壞列**跳過並計數**（「安靜地少」是這個專案明令禁止的失敗模式）：
       呼叫端要把 bad 端到畫面、印到主控台。
    ⛔ 同一天第二列**只認第一列**、也計數（只 append 的檔，正常情況不會有）。
    讀不出整個檔（權限／編碼）⇒ 讓例外往外丟，呼叫端當成「歷史拿不到」處理。
    """
    p = path or FAST_HIST
    rows, bad, dup, seen = [], 0, 0, set()
    if not p.exists():
        return rows, 0, 0
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            o = json.loads(line)
        except Exception:
            bad += 1
            continue
        d = o.get("date") if isinstance(o, dict) else None
        mv = _num(o.get("move_pct")) if isinstance(o, dict) else None
        if not isinstance(d, str) or not _DATE_RE.match(d) or mv is None or mv < 0:
            bad += 1
            continue
        if d in seen:
            dup += 1
            continue
        seen.add(d)
        rows.append({"date": d, "move_pct": mv, "ref": _num(o.get("ref")),
                     "px": _num(o.get("px")), "ref_src": o.get("ref_src")})
    rows.sort(key=lambda r: r["date"])
    return rows, bad, dup


def _hist_note(bad, dup):
    """讀到壞列／重複 ⇒ 記下來 ＋ 主控台（⛔ 不可以安靜地少）。"""
    _ST["hist_bad"], _ST["hist_dup"] = bad, dup
    if bad or dup:
        print("⚠️ [自動下單] %s 有 %d 列讀不出來、%d 列是重複的日子 —— 已跳過（門檻少算那幾天）"
              % (FAST_HIST.name, bad, dup), flush=True)


def _hist_step(d, snap):
    """
    09:03:30 之後（⚠️ **工作執行緒**）：讀歷史 ＋ 把今天那一列寫進去。
    ⛔ 不管開關開不開、送不送都要跑（歷史是給以後的門檻用的，不能只記送單的日子）。
    ⛔ 永遠不往外丟例外（它出錯不可以讓送單那一段直接崩掉；而是變成「歷史拿不到 ⇒ 不送」）。
    回 {"rows", "bad", "dup", "wrote", "why", "err"}：
      wrote=False 的 why：already（今天已經有了，看門狗重啟）／no_quote／quote_stale／
      mid_only（報價不能用）／no_ref（拿不到 09:00 以前的價）／io（寫不進去）
    """
    out = {"rows": None, "bad": 0, "dup": 0, "wrote": False, "why": None, "err": None}
    try:
        rows, bad, dup = hist_read()
    except Exception as e:
        out.update(err="讀不出 %s：%s" % (FAST_HIST.name, str(e)[:100]), why="io")
        _ST["err"], _ST["err_n"] = "hist: " + out["err"], _ST["err_n"] + 1
        print("⚠️ [自動下單] " + out["err"], flush=True)
        return out
    out.update(rows=rows, bad=bad, dup=dup)
    _hist_note(bad, dup)
    if any(r["date"] == d for r in rows):
        out["why"] = "already"                   # ⛔ 同一天不重寫（看檔案，看門狗重啟是常態）
        return out
    q = _quote_why(snap)
    ref = _num(snap.get("ref0900"))
    px = _num(snap.get("px"))
    mv = move_pct(px, ref)
    if q or mv is None:
        # ⛔ 報價不能用／拿不到 09:00 以前的價 ⇒ **不寫**（寫進去就是一筆假的走幅，
        #    會污染以後 40 天的門檻），原因落地在帳本那一列（base["hist"]）＋ 主控台。
        out["why"] = q or "no_ref"
        _ST["hist_msg"] = "%s 今天那一列沒有寫進開盤走幅歷史（%s）" % (d, out["why"])
        print("[自動下單] " + _ST["hist_msg"], flush=True)
        return out
    row = {"date": d, "ref": ref, "px": px, "move_pct": round(mv, 6),
           "ref_src": snap.get("ref_src")}
    try:
        FAST_HIST.parent.mkdir(parents=True, exist_ok=True)
        # ⛔ 一定是 open("a")：看門狗重啟是常態，覆寫＝把過去的歷史弄丟。
        with FAST_HIST.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
    except Exception as e:
        out.update(why="io", err="寫不進 %s：%s" % (FAST_HIST.name, str(e)[:100]))
        _ST["err"], _ST["err_n"] = "hist: " + out["err"], _ST["err_n"] + 1
        print("⚠️ [自動下單] " + out["err"], flush=True)
        return out
    out["wrote"] = True
    _ST["hist_msg"] = None
    return out


# ---------------------------------------------------------------- 決定與送出

def _quote_why(snap):
    """
    報價這一關。回 None＝可以用，否則回不能用的原因。

    ⛔ 跟【模擬】那一頁 `_auto_record()` 同一套判準（沒有成交價／報價太舊／只有中價），
       ⛔ 但**這裡更嚴格的地方在於它會擋住真錢**：報價斷了就不送
       （Benson 明確同意「斷線時自動下單要跟著停」）。
    """
    if _num(snap.get("px")) is None:
        return "no_quote"
    if snap.get("is_mid"):
        return "mid_only"
    age = snap.get("quote_age_ms")
    if age is None or age > _CFG["gap_s"] * 1000:
        return "quote_stale"
    return None


def _fire(snap, day, lag_ms, put_at):
    """
    ⚠️ **跑在自己的 daemon 執行緒上**（⛔ 不是主迴圈）：這裡會呼叫券商 API、
       會等成交（最多 `broker.FILL_WAIT` 秒）、會寫檔 —— 任何一項放進 4Hz 主迴圈
       都等於**把他的停損塞住幾秒**。
    """
    d = day
    if _has(d):
        return None                      # 一天一次。⛔ 這道在最前面
    if not _ST["wired"]:
        return _skip(d, "not_wired")
    if snap is None:
        # 主迴圈說「跨過 09:03:30 了，但已經晚太多」（面板 09:10 才開起來／看門狗剛重啟）
        return _skip(d, "late", {"at_lag_ms": lag_ms})

    # ── 開盤走幅歷史：先把今天那一列寫進去。⛔ 排在「開關開不開」之前 ——
    #    歷史是以後 40 天門檻的材料，只記送單的日子就是一份有偏差的歷史。
    hist = _hist_step(d, snap)

    a = arm()
    base = {"method": a["method"], "arm_raw": a["raw"],
            "at": snap.get("at"), "at_lag_ms": snap.get("at_lag_ms"),
            "px": _num(snap.get("px")), "quote_age_ms": snap.get("quote_age_ms"),
            "quote_gaps": snap.get("quote_gaps"),
            # ⛔ 今天那一列有沒有寫進歷史、沒寫的話為什麼 —— 落地在每一種結局上
            "hist": {"wrote": hist["wrote"], "why": hist["why"],
                     "bad": hist["bad"], "dup": hist["dup"], "err": hist["err"]}}
    if not a["on"]:
        return _skip(d, a["why"], base, a["msg"])

    q = _quote_why(snap)
    if q:
        return _skip(d, q, base)

    px = _num(snap.get("px"))
    o845, p900 = _num(snap.get("open0845")), _num(snap.get("p0900"))
    ref = _num(snap.get("ref0900"))
    sig_a, sig_b = _CFG["sig_fn"](px, o845, p900)
    dirs = _CFG["dirs_fn"](sig_a, sig_b)
    base["sig"] = {"A": sig_a}
    base["ref"] = {"open0845": o845, "p0900": p900,
                   "p0900_src": snap.get("p0900_src"),
                   "ref0900": ref, "ref_src": snap.get("ref_src"),
                   "prev_close": _num(snap.get("prev_close"))}
    base["bid"], base["ask"] = _num(snap.get("bid")), _num(snap.get("ask"))
    dv = dirs.get(a["method"])
    mv = move_pct(px, ref)
    # ⛔ 方向（09:00 那一分鐘第一筆）或走幅（09:00 以前最後一筆）任一個算不出來 ⇒ 不送。
    #    兩個參考價在 _auto_snap 裡是同兩個來源、順序相反，所以實際上會一起有、一起沒有。
    if dv is None or mv is None:
        return _skip(d, "no_signal", base)
    if dv == 0:
        # A 不會回 0（那是 C 的門檻），留著是防呆：真的回 0 就是不做，⛔ 不猜方向
        return _skip(d, "no_trade", base)
    direction = "long" if dv > 0 else "short"
    base["dir"] = direction

    # ── ⭐ 快不快（2026-09-15）。⛔ 判斷只准走 fast_verdict()（畫面與離線對照用同一支）
    if hist["rows"] is None:
        base["fast"] = {"verdict": "no_hist", "move_pct": round(mv, 4)}
        return _skip(d, "no_hist", base,
                     WHY["no_hist"] + "（" + str(hist["err"] or "歷史檔讀不出來") + "）")
    v = fast_verdict(d, mv, hist["rows"])
    fast = {"verdict": v["verdict"], "move_pct": round(mv, 4),
            "move_pts": approx_points(mv, ref),
            "thr_pct": None if v["thr_pct"] is None else round(v["thr_pct"], 4),
            "thr_pts": approx_points(v["thr_pct"], ref),
            "n": v["n"], "window": FAST_RULE["window"], "pctl": fast_pctl(),
            "min_n": FAST_RULE["min_n"]}
    base["fast"] = fast
    if v["verdict"] == "no_hist":
        return _skip(d, "no_hist", base,
                     "過去的開盤走幅只有 %d 天（至少要 %d 天才算得出門檻）—— 照規則今天不做"
                     % (v["n"], FAST_RULE["min_n"]))
    if v["verdict"] != "fast":
        return _skip(d, "not_fast", base,
                     "今天開盤不夠快：走 %.2f%%（約 %s 點），門檻 %.2f%%（約 %s 點）—— 照規則今天不做"
                     % (mv, fast["move_pts"], v["thr_pct"], fast["thr_pts"]))
    # ⭐ 停利停損 ±0.5% of 09:03:30 的價（⛔ 不是成交價：送單之前就要定下來、落地）
    pts = tpsl_points(px)
    base["tp_points"] = pts
    base["sl_points"] = pts

    # 【第二道遲到檢查】上面那道是主迴圈跨過 09:03:30 的延遲；這一道是
    # 「排隊 ＋ 排到我開始做」的延遲。市價單晚幾秒送出去，成交價就不是那一刻的價了。
    late = (lag_ms or 0) + (time.time() - put_at) * 1000.0
    if late > LATE_MS:
        base["late_ms"] = int(late)
        return _skip(d, "late", base,
                     WHY["late"] + "（實際晚了 %.1f 秒）" % (late / 1000.0))

    # ⛔⛔ **先落地再送單**。送到一半當掉的話，重啟後 _has() 讀得到這一列
    #     ⇒ 那天不會再送第二張。⛔ 寧可漏記結果，不可以重送。
    fire = dict(base)
    fire.update({"rec": "fire", "stage": "sending", "date": d,
                 "live": broker.is_live(), "qty": broker.QTY,
                 "wrote_at": datetime.now().isoformat(timespec="seconds")})
    _append(fire)
    _ST["last"] = fire

    fresh = (snap.get("quote_age_ms") is not None
             and snap["quote_age_ms"] <= _CFG["gap_s"] * 1000)
    ok, why = broker.can_enter(px, fresh)
    if not ok:
        # 沒報價／報價不新鮮／還沒連上永豐／對帳失敗／券商已有部位／當天已達上限
        why = str(why or "沒有說原因")
        # ⚠️ 「還沒連上永豐」這一句最容易被讀成「程式壞了」，其實幾乎都是
        #    **面板 09:03:30 才剛啟動、SDK 還在登入**（lab-qa：09:03 才開機那天就長這樣）。
        #    ⛔ 症狀跟原因看起來毫無關係的時候一定要把原因寫出來。
        hint = ("　（面板是不是 %s 前後才開起來？永豐 SDK 登入要幾十秒，"
                "那段時間進不了場 —— 這是**開太晚**，不是程式壞了。"
                "要用自動下單就讓面板一直開著。）" % (_CFG["signal_at"] or "09:03:30")
                ) if "還沒連上永豐" in why else ""
        return _result(d, base, False, "cant_enter",
                       WHY["cant_enter"] + "：" + why + hint)

    print("[%s] 自動下單：用「%s」判定 %s（走 %.2f%% ≥ 門檻 %.2f%%），送出 1 口，停利停損各 %d 點（%s）" %
          (d, METHOD_NAME[a["method"]], "做多" if direction == "long" else "做空",
           mv, v["thr_pct"], pts,
           "真單" if broker.is_live() else "演練，不會真的送出去"), flush=True)
    # ⛔⛔ sl_points 一定要帶：停損活在面板迴圈（check_real_position），它讀的是
    #     **這一口部位自己的** sl_points；沒帶就掉回手動真單的 SL_POINTS（130）⇒ 提早被洗掉。
    ok, err, pos = broker.enter(direction, px, pts, sl_points=pts)
    if not ok:
        return _result(d, base, False, "order_failed",
                       WHY["order_failed"] + "：" + str(err or "券商沒有說原因"))
    entry = _num((pos or {}).get("entry"))
    tp = None if entry is None else round(entry + (pts if direction == "long" else -pts), 1)
    sl = None if entry is None else round(entry - (pts if direction == "long" else -pts), 1)
    extra = dict(base)
    extra.update({"entry": entry, "tp": tp, "sl": sl,
                  # ⛔⛔ **收盤平倉靠這個欄位認人**（日期＋進場時間＋進場價，
                  #    跟 broker.set_trade_note() 同一套）—— 少了它，13:43:30 那一下
                  #    只能比方向與價格，他自己開的單就有機會被誤判成「我的」。
                  "entry_time": (pos or {}).get("entry_time"),
                  "slip": None if (entry is None or px is None) else
                          round((entry - px) * (1 if direction == "long" else -1), 1),
                  "has_target": bool((pos or {}).get("target_trade") is not None),
                  # ⛔ 停利掛失敗不可以吞掉：broker 會回 ok=True ＋ 一句警告
                  "warn": err or None})
    return _result(d, extra, True, None, None)


def _result(d, extra, ok, why, msg):
    row = dict(extra)
    # ⛔ `stage` 一定要蓋成 done：合併是「後寫的蓋前面的」，不蓋的話那一天會一直
    #    停在 sending ⇒ 畫面上把已經有結果的一天寫成「不知道下場」（一句假話）。
    row.update({"rec": "result", "date": d, "stage": "done",
                "ok": bool(ok), "why": why,
                "why_msg": msg, "live": broker.is_live(),
                "wrote_at": datetime.now().isoformat(timespec="seconds")})
    _ST["last"] = row
    print("[%s] 自動下單：%s" % (d, msg or "已送出並記錄"), flush=True)
    out = _append(row)
    if ok:
        # 撿回部位時要用的「今天那一口」—— 剛寫進檔案的就是真相，直接放進記憶體
        # （⛔ 不必再讀一次檔；recover_meta() 在主迴圈上只准讀記憶體）。
        _MEM.update({"date": d, "entry": row, "state": None})
    return out


# ---------------------------------------------------------------- 收盤平倉

def _day_rows(d):
    """某一天合併後的樣子：(送單那一列, 收盤平倉那一列)。⛔ 看檔案不看記憶體。"""
    fire, eod = None, None
    for o in _rows_of(d):
        rec = o.get("rec")
        if rec in ("fire", "result", "skip"):
            fire = dict(fire or {})
            fire.update({k: v for k, v in o.items() if k != "rec"})
            fire["rec"] = rec
        elif rec == "eod":
            eod = dict(eod or {})
            eod.update({k: v for k, v in o.items() if k != "rec"})
    return fire, eod


def _eod_settled(d):
    """這一天的收盤平倉已經有定論了嗎（⛔ 平不掉／對不了帳的那幾種不算，要能重試）。"""
    _f, e = _day_rows(d)
    return bool(e) and e.get("why") in EOD_SETTLED


def _auto_entry(d):
    """
    今天**自動下單真的開出來**的那一口。回 (那一列, 不能動手的原因)。

    ⛔ 只有 `rec:"result"` 且 `ok` 才算 —— skip 那幾種根本沒送單，
       「送出去了但不知道結果」（stage 停在 sending）也**不算**：那種狀態下
       券商上那口部位到底是不是我們的，我們自己都不知道 ⇒ 不敢動。
    """
    f, _e = _day_rows(d)
    if not f:
        return None, "eod_no_entry"
    if f.get("rec") == "fire" and f.get("stage") == "sending":
        return None, "eod_unsure"
    if f.get("rec") != "result" or not f.get("ok"):
        return None, "eod_no_entry"
    if f.get("dir") not in ("long", "short"):
        return None, "eod_no_entry"
    return f, None


def _looks_ours(pos, ent):
    """
    券商上那口部位，是不是帳本上那一列開出來的。回 (是嗎, 不是的話差在哪)。

    ⛔ 任何一項對不上就回 False。**寧可不平，不可以平錯**（平錯＝平掉他自己的單）。
    """
    if not isinstance(pos, dict) or not isinstance(ent, dict):
        return False, "拿不到部位或紀錄"
    # ⛔⛔ **口數也要比**（2026-09-09 lab-qa 退件 M2，實測平掉他兩口）：
    #    自動下單永遠只開 1 口（`broker.QTY`）。他在大戶投加碼之後券商上是 2 口，
    #    而加碼的均價很容易落在 EOD_PX_TOL（1.0 點）之內 ⇒ 方向、價、時間全都對得上
    #    ⇒ 舊版判定「是我們的」。而 `broker.close()` 的成交判準是「部位**整個**不見」，
    #    於是它連送 Cover 直到券商剩 0 口 —— **他自己那一口一起被平掉**。
    #    這一項不是「多比一個欄位」，是「這口是不是我們的」那把尺原本缺的一格。
    pq, eq = _num(pos.get("qty")), float(broker.QTY)
    if pq is None or pq != eq:
        return False, "口數不一樣（現在 %s 口，自動下單只開 %g 口）—— 你自己加減碼過" % (
            "?" if pq is None else ("%g" % pq), eq)
    if pos.get("dir") != ent.get("dir"):
        return False, "方向不一樣（現在是 %s，自動下單開的是 %s）" % (
            pos.get("dir"), ent.get("dir"))
    pe, ee = _num(pos.get("entry")), _num(ent.get("entry"))
    if pe is None or ee is None:
        return False, "沒有進場價可以比對"
    if abs(pe - ee) > EOD_PX_TOL:
        return False, "進場價差了 %.1f 點（現在 %.1f，自動下單開在 %.1f）" % (
            abs(pe - ee), pe, ee)
    pt, et = pos.get("entry_time"), ent.get("entry_time")
    if isinstance(pt, str) and pt and isinstance(et, str) and et and pt != et:
        return False, "進場時間不一樣（現在這口是 %s 開的，自動下單開在 %s）" % (pt, et)
    # ⚠️ pt 是 None ＝ 面板重啟後從券商撿回來的（broker 只拿得到方向與均價，
    #    拿不到時間）。那種情況只能靠方向＋進場價 —— 這是這道守衛最弱的一環，
    #    所以 _already_closed() 那一道（含券商的已實現損益）一定要先跑過。
    return True, None


def _already_closed(ent):
    """
    帳本上那一口**先前是不是已經平掉了**。回 (狀態, 怎麼看出來的)。

    狀態有**三種**，⛔ 不是兩種（2026-09-09 lab-qa 退件 M1）：
      - `"closed"` ＝ 查到了，那一趟已經有出場紀錄
      - `"open"`   ＝ 查到了，兩份都沒有它 ⇒ 那一口還開著
      - `"unknown"`＝ **查不到**（帳本那列沒進場價／讀不到成績單／問不到券商）
    ⛔⛔ `"unknown"` 絕對不可以併進上面任何一種：
      - 併進 `"open"` ⇒ 往下走到第③道，而第③道在「面板重啟、`entry_time` 遺失」時
        只剩方向＋價 ⇒ **把他的部位平掉**（lab-qa 實測 X4／X5 各送出一張）。
      - 併進 `"closed"` ⇒ 畫面寫「自動下單那一口先前已經平掉了」，但部位其實還開著
        ⇒ **一句假話**，而且不示警（lab-qa 實測 Y1／Y2）。
      `"unknown"` 自己有一句話（`eod_cant_tell`）而且**掛金色警示**。

    ⛔ 這一道是「他中途自己平掉、又自己開了一口新的」唯一擋得住的地方 ——
       少了它，那口**新的、他自己的**部位會被當成我們的平掉。
    兩個來源都要查：
      ① `broker.trades_today()`：面板自己記的成績單（停利成交／他按平倉／
         reconcile 發現部位不見了，三種都會留一列）。
      ② `broker.realized_today(fresh=True)`：**券商自己**的已實現損益 —— 面板當掉的
         那段時間他在大戶投平掉的，只有這一份看得到。
         ⛔ 一定要 `fresh=True`：TTL 快取最舊可以是 59 秒前的快照，而這一刻要判的
         正是「這一分鐘之內他有沒有自己平掉」（X5 就是快取沒過期而且是空的）。
         ⛔ 而且**不准寫 `or []`** —— `None` 是「問不到」，不是「沒有」。
    """
    e, dv, t = _num(ent.get("entry")), ent.get("dir"), ent.get("entry_time")
    if e is None:
        return "unknown", "帳本那一列沒有進場價，對不了帳"
    try:
        mine = broker.trades_today()
    except Exception as ex:
        return "unknown", "讀不到面板的成績單（%s）" % str(ex)[:60]
    if mine is None:
        return "unknown", "讀不到面板的成績單"
    for r in mine:
        if not isinstance(r, dict) or r.get("dir") != dv:
            continue
        re_ = _num(r.get("entry"))
        if re_ is None or abs(re_ - e) > EOD_PX_TOL:
            continue
        if t and isinstance(r.get("entry_time"), str) and r["entry_time"] and \
                r["entry_time"] != t:
            continue
        return "closed", "面板的成績單裡已經有這一趟（%s 出場）" % (r.get("exit_time") or "?")
    try:
        real = broker.realized_today(fresh=True)
    except Exception as ex:
        return "unknown", "問不到券商的已實現損益（%s）" % str(ex)[:60]
    if real is None:
        # ⛔⛔ 這裡就是 M1 的落點。broker 那層以前把例外吞掉、靜靜回一個空陣列，
        #    所以外面這個 try/except **結構上永遠接不到** —— 現在它回 None。
        return "unknown", "問不到券商的已實現損益（對不了帳）"
    for r in real:
        if not isinstance(r, dict):
            continue
        if r.get("dir") and dv and r["dir"] != dv:
            continue
        re_ = _num(r.get("entry"))
        if re_ is None or abs(re_ - e) > EOD_PX_TOL:
            continue
        return "closed", "券商今天的已實現損益裡已經有這一趟（進場 %.1f）" % re_
    return "open", None


def _eod_row(d, why, extra=None, msg=None):
    """收盤平倉的落地。⛔ 每一種結局都要有一列、都要有原因（畫面上看得到）。"""
    row = {"rec": "eod", "date": d, "stage": "done",
           "why": why, "why_msg": msg or WHY.get(why, why),
           "ok": why == "eod_closed", "alarm": why in EOD_ALARM,
           "at": datetime.now().strftime("%H:%M:%S"),
           "eod_at": _CFG["eod_at"], "live": broker.is_live(),
           "wrote_at": datetime.now().isoformat(timespec="seconds")}
    row.update(extra or {})
    _ST["last"] = row
    head = "⚠️ " if row["alarm"] else ""
    print("%s[%s] 收盤平倉：%s" % (head, d, row["why_msg"]), flush=True)
    return _append(row)


def _eod_exit_of(ent):
    """平完之後回頭把出場價與點數撈出來（`broker.close()` 已經寫進成績單了）。"""
    e, dv = _num(ent.get("entry")), ent.get("dir")
    try:
        rows = broker.trades_today() or []
    except Exception:
        return {}
    for r in reversed(rows):
        if not isinstance(r, dict) or r.get("dir") != dv:
            continue
        re_ = _num(r.get("entry"))
        if re_ is None or e is None or abs(re_ - e) > EOD_PX_TOL:
            continue
        return {"exit": _num(r.get("exit")), "exit_time": r.get("exit_time"),
                "points": _num(r.get("points")), "close_reason": r.get("reason")}
    return {}


def _eod(day, lag_ms, put_at):
    """
    ⚠️ **跑在自己的 daemon 執行緒上**（⛔ 不是主迴圈）：這裡會跟券商對帳、
       會送平倉單並等成交（`broker.close()` 最壞十幾秒）、會寫檔。

    ⛔⛔ **只平自己開的那一口。** 三道守衛全過才動手，任何一道不過就落地一列
       「為什麼不平」然後收工。
    """
    d = day
    if _eod_settled(d):
        return None                      # 一天一次（看門狗在 13:43~13:45 重啟會重觸發）
    ent, why = _auto_entry(d)
    if ent is None:
        return _eod_row(d, why, {"at_lag_ms": lag_ms})
    base = {"dir": ent.get("dir"), "entry": _num(ent.get("entry")),
            "entry_time": ent.get("entry_time"), "method": ent.get("method"),
            "at_lag_ms": lag_ms}

    # ── ② 那一口是不是早就平掉了（他中途自己平掉、又自己開了一口新的）
    #    ⛔⛔ 三種狀態，⛔ 不是兩種。「查不到」有自己的一句話而且會示警 ——
    #    寫成「已經平掉了」是**一句假話**（部位還開著、13:45 起停損也停了，
    #    而畫面告訴他已經平掉；lab-qa 實測 Y1／Y2）。
    state, how = _already_closed(ent)
    if state == "closed":
        return _eod_row(d, "eod_done_elsewhere", dict(base, how=how),
                        WHY["eod_done_elsewhere"] + " —— " + str(how))
    if state != "open":
        # ⛔ 不動手（沿用「寧可不平，不可以平錯」的不對稱原則）。
        # ⚠️ 這裡**刻意不重試**：三條路裡有一條是「帳本那列沒有進場價」——
        #    那是確定性的，重試只會把 75 秒的窗口燒光，而重試也不會送單。
        #    `eod_cant_tell` 不在 EOD_SETTLED 裡 ⇒ 看門狗在 13:43:30~13:45
        #    之間重啟時還會再試一次。
        return _eod_row(d, "eod_cant_tell", dict(base, how=how),
                        WHY["eod_cant_tell"] + " —— " + str(how))

    deadline = put_at + EOD_WINDOW_S
    last_err, tries = None, 0
    while True:
        # ── ③ 現在券商上有什麼（⛔ 券商是真相；演練模式沒有券商，看記憶體那一口）
        pos = broker.reconcile() if broker.is_live() else broker._state.get("position")
        if pos == "unknown":
            if time.time() < deadline:
                time.sleep(EOD_RETRY_S)
                continue
            return _eod_row(d, "eod_unknown", dict(base, tries=tries))
        if pos is None:
            return _eod_row(d, "eod_flat", dict(base, tries=tries))
        ours, diff = _looks_ours(pos, ent)
        if not ours:
            # ⛔⛔ 這是最重要的一條：**不是我們開的就不碰**。
            return _eod_row(d, "eod_not_ours",
                            dict(base, now_dir=pos.get("dir"),
                                 now_entry=_num(pos.get("entry")),
                                 now_entry_time=pos.get("entry_time"),
                                 # ⛔ 口數也要落地：他要看得出「現在是 2 口」
                                 #    才知道為什麼面板不幫他平（M2）。
                                 now_qty=_num(pos.get("qty")),
                                 diff=diff, tries=tries),
                            WHY["eod_not_ours"] + "（" + str(diff) + "）")

        # ── ④ 平。⛔ 一律走 broker.close()（那裡有整套防呆＋同一把 _close_lock）
        tries += 1
        ok, err = broker.close("eod")
        if ok:
            return _eod_row(d, "eod_closed",
                            dict(base, tries=tries, **_eod_exit_of(ent)),
                            WHY["eod_closed"] + (("（%s）" % err) if err else ""))
        last_err = err
        if time.time() >= deadline:
            break
        time.sleep(EOD_RETRY_S)
    # 平不掉。⛔ **大聲講**，而且叫他自己去大戶投平 —— 沿用 broker 既有的那句話。
    return _eod_row(d, "eod_failed", dict(base, tries=tries, err=str(last_err or "")),
                    WHY["eod_failed"] + "（試了 %d 次；券商說：%s）" % (
                        tries, str(last_err or "沒有說原因")))


def on_eod(day, lag_ms):
    """
    ⚠️⚠️ **這個函式跑在 4Hz 主迴圈上，而那條迴圈就是他的停損。**
    ⛔ 裡面只准有 `put_nowait` —— 一行 I/O、一次網路、一個鎖都不准有，
       **而且永遠不可以往外丟例外**（跟 `on_signal` 同一套規矩）。
    """
    try:
        _EQ.put_nowait((day, lag_ms, time.time()))
    except Exception as e:
        _ST["err"] = WHY["eod_queue_full"] + ("（%s）" % str(e)[:80] if str(e) else "")
        _ST["err_n"] += 1
        print("⚠️ [自動下單] %s" % WHY["eod_queue_full"], flush=True)


def _eod_worker():
    while True:
        item = _EQ.get()
        try:
            _eod(*item)
        except Exception as e:
            # ⛔ 不可以安靜地吞：計數 ＋ 主控台 ＋ 落地（畫面上看得到）
            _ST["err"] = "eod: " + str(e)[:150]
            _ST["err_n"] += 1
            print("⚠️ [自動下單] 收盤平倉那一段出錯：%s" % str(e)[:200], flush=True)
            try:
                _eod_row(item[0], "eod_crashed",
                         {"err": str(e)[:150]},
                         WHY["eod_crashed"] + "：" + str(e)[:120])
            except Exception:
                pass


# ---------------------------------------------------------------- 重啟撿回部位：補停損點數
#
# ⛔⛔ 為什麼一定要有這一段：停損活在面板迴圈（`live_panel.check_real_position`），
#    它讀的是**這一口部位自己的** `sl_points`。看門狗重啟之後，`broker.reconcile()`
#    從券商撿回來的部位只有方向／口數／均價（`recovered=True`）⇒ 沒有 `sl_points`
#    ⇒ 掉回手動真單的 SL_POINTS（130）⇒ **自動下單那一口的停損從 ±0.5%（約 230 點）
#    縮成 130 點，提早被洗掉。**
# ⇒ 認人：今天帳本裡 `rec:"result"` ok 那一列，**沿用 `_looks_ours()`**
#    （方向 ＋ 進場價 ±EOD_PX_TOL ＋ 口數；撿回來的部位沒有 entry_time，那一格跳過）。
#    ⛔ 不另寫一把尺（收盤平倉認人用的就是這一把）。
# ⛔ 對不上 ⇒ **維持 SL_POINTS**，並把原因掛在部位上（`sl_warn`，/api/state 端得出去）
#    ＋ 主控台。⚠️ 方向是**寧可用手動那一套**：誤把他自己的單當成自動的，停損就被放寬了。

def _recover_decide(pos, ent, why):
    """純函式：撿回來的那口 ＋ 今天帳本那一口 ⇒ 要補到部位上的欄位。"""
    if ent is None:
        if why == "eod_unsure":
            # 今天有一張單停在「送出去了但不知道結果」⇒ 撿回來的這口**可能**就是它，
            # 但帳本裡沒有進場價可以比 ⇒ 認不出來 ⇒ 用手動那一套，而且要講。
            return {"sl_src": "unmatched",
                    "sl_warn": "今天自動下單那一張停在「不知道下場」，認不出撿回來的這口是不是它"
                               " —— 停損先用手動真單那一套，請自己到大戶投確認"}
        # 今天自動下單根本沒有開出部位 ⇒ 這口是他自己開的（或更早的），手動那一套就是對的
        return {"sl_src": "manual"}
    ours, diff = _looks_ours(pos, ent)
    slp, tpp = _num(ent.get("sl_points")), _num(ent.get("tp_points"))
    if ours and slp is not None and slp > 0:
        return {"sl_points": slp, "tp_points": tpp, "sl_src": "autofire"}
    if ours:
        return {"sl_src": "unmatched",
                "sl_warn": "撿回來的這口對得上今天自動下單那一口，但帳本那一列沒有停損點數"
                           " —— 停損先用手動真單那一套，請自己確認"}
    return {"sl_src": "unmatched",
            "sl_warn": "撿回來的這口對不上今天自動下單那一口（%s）—— 停損用手動真單那一套" % diff}


def recover_meta(pos):
    """
    ⚠️⚠️ **會在 4Hz 主迴圈上被呼叫**（`broker.reconcile()` 撿回部位那一刻，經由
       `broker.RECOVER_HOOK`；reconcile_tick 是主迴圈叫的）。
    ⛔ 只准讀記憶體（`_MEM`）—— 一行 I/O、一次網路、一個鎖都不准有；⛔ 永遠不往外丟例外。
    記憶體裡還沒有今天的帳本（開機還沒讀到／剛跨日）⇒ 回 None，交給 `_recover_poll()`
    在送單執行緒上讀檔補（最多晚 POLL_S 秒；那段時間停損用手動那一套）。
    """
    try:
        if _MEM["date"] != str(date.today()):
            return None
        return _recover_decide(pos, _MEM["entry"], _MEM["state"])
    except Exception:
        return None


def _mem_load(d):
    """⚠️ 送單執行緒／start()：讀今天帳本那一口放進記憶體。"""
    ent, why = _auto_entry(d)
    _MEM.update({"date": d, "entry": ent, "state": why})


_REC_SEEN = {"pos": None}      # 哪一口已經講過了（⛔ 主控台一口只講一次，不要每 0.5 秒刷一次）


def _recover_poll():
    """
    ⚠️ **送單執行緒**（⛔ 不是主迴圈）：看有沒有「撿回來、還沒補停損點數」的部位。
    `recover_meta()` 在主迴圈上補不起來（記憶體還沒有今天的帳本）時，由這裡讀檔補。
    """
    pos = broker._state.get("position")
    if not isinstance(pos, dict) or not pos.get("recovered"):
        return None
    if pos.get("sl_src") is None:
        d = str(date.today())
        if _MEM["date"] != d:
            _mem_load(d)
        meta = _recover_decide(pos, _MEM["entry"], _MEM["state"])
        with broker._lock:
            if broker._state.get("position") is pos and pos.get("sl_src") is None:
                pos.update(meta)
    if _REC_SEEN["pos"] is pos:
        return pos.get("sl_src")
    _REC_SEEN["pos"] = pos
    if pos.get("sl_src") == "autofire":
        _ST["rec_msg"] = ("重啟後撿回自動下單那一口：停損 %g 點（從今天的帳本補回來）"
                          % pos.get("sl_points"))
        print("[自動下單] " + _ST["rec_msg"], flush=True)
    elif pos.get("sl_warn"):
        _ST["rec_msg"] = str(pos["sl_warn"])
        print("⚠️ [自動下單] " + _ST["rec_msg"], flush=True)
    return pos.get("sl_src")


# ---------------------------------------------------------------- 接線

def configure(signal_at, signal_sec, late_ms, gap_s, sig_fn, dirs_fn, eod_at, pctl):
    """
    面板啟動時叫一次，把**常數與訊號算式的正本**接過來（正本在 live_panel.py）。

    ⛔ 這個檔不自己寫一份 09:03:30／訊號算式 —— 那會變成兩把尺，
       訊號時刻改了一邊、另一邊不會跟著改，而畫面上完全看不出來。
    ⚠️ 停利停損點數（2026-09-15 起 ±0.5%）的正本是這個檔的 FAST_RULE，⛔ 不吃 TP_POINTS。
    ⭐ `pctl`（2026-09-15）：「開盤快才做」門檻的百分位，正本 `live_panel.FAST_PCTL`（80）。
       看不懂（bool／非數字／不在 0~100）⇒ 存 None ⇒ wired=False ⇒ 不送（⛔ 不猜）。
    沒有接起來（`wired=False`）時**一律不送**，理由 `not_wired`。
    """
    ok_pctl = (not isinstance(pctl, bool) and isinstance(pctl, (int, float))
               and 0 < pctl <= 100)
    _CFG.update({"signal_at": signal_at, "signal_sec": signal_sec,
                 "late_ms": late_ms, "gap_s": float(gap_s),
                 "sig_fn": sig_fn, "dirs_fn": dirs_fn, "eod_at": eod_at,
                 "pctl": pctl if ok_pctl else None})
    _ST["wired"] = all(_CFG[k] is not None for k in _CFG)
    return _ST["wired"]


def on_signal(snap, day, lag_ms):
    """
    ⚠️⚠️ **這個函式跑在 4Hz 主迴圈上，而那條迴圈就是他的停損。**
    ⛔ 裡面只准有 `put_nowait` —— 一行 I/O、一次網路、一個鎖都不准有，
       **而且永遠不可以往外丟例外**（丟出去就是主迴圈那一段被打斷）。

    `snap is None` ＝ 主迴圈判定「已經晚太多」，那一天要記成 late（⛔ 不補單）。
    """
    try:
        _Q.put_nowait((snap, day, lag_ms, time.time()))
    except Exception as e:
        # 佇列滿了＝前一件事還沒做完。⛔ 不可以在主迴圈上等，也 ⛔ 不可以安靜地丟。
        # ⚠️ queue.Full 的訊息是**空字串** —— 直接串上去畫面上會出現「on_signal: 」，
        #    等於沒說。原因要自己寫出來。
        _ST["err"] = WHY["queue_full"] + ("（%s）" % str(e)[:80] if str(e) else "")
        _ST["err_n"] += 1
        if _ST["err_n"] <= 3:
            print("⚠️ [自動下單] %s（停損不受影響）" % WHY["queue_full"], flush=True)


def _worker():
    while True:
        # ⚠️ 帶 timeout：沒有單可送的時候也要醒來看一眼「撿回來的部位補停損了沒」
        #    （_recover_poll）。⛔ 那件事不准放進主迴圈（要讀檔）。
        try:
            item = _Q.get(timeout=POLL_S)
        except queue.Empty:
            item = None
        if item is not None:
            try:
                _fire(*item)
            except Exception as e:
                # ⛔ 不可以安靜地吞：計數 ＋ 主控台 ＋ 端點端出去（畫面上看得到）
                _ST["err"] = "fire: " + str(e)[:150]
                _ST["err_n"] += 1
                print("⚠️ [自動下單] 送單那一段出錯（停損不受影響）：%s" % str(e)[:200],
                      flush=True)
        try:
            _recover_poll()
        except Exception as e:
            _ST["err"] = "recover: " + str(e)[:150]
            _ST["err_n"] += 1
            if _ST["err_n"] <= 3:
                print("⚠️ [自動下單] 補撿回部位的停損點數出錯（停損照舊用手動那一套）：%s"
                      % str(e)[:200], flush=True)


def start():
    """
    把送單與收盤平倉兩條執行緒起起來。
    ⛔ 只有 live_panel.main() 會叫（治具與 --replay 不會）。
    ⚠️ **兩條要分開**：平倉的窗口只有 75 秒，不可以排在送單那件事後面。
    """
    if _ST["started"]:
        return False
    # 開機先把今天帳本那一口讀進記憶體：看門狗重啟後 reconcile 撿回部位的**第一刻**
    # recover_meta() 就補得起來（⛔ 不必等送單執行緒 0.5 秒後才補 —— 那段時間停損是 130）。
    # ⚠️ 這裡是 main() 啟動流程（還沒進主迴圈），讀一次小檔可以。
    try:
        _mem_load(str(date.today()))
    except Exception as e:
        print("⚠️ [自動下單] 開機讀不到今天的帳本（撿回部位時改由送單執行緒補）：%s"
              % str(e)[:120], flush=True)
    threading.Thread(target=_worker, daemon=True, name="auto-fire").start()
    threading.Thread(target=_eod_worker, daemon=True, name="auto-fire-eod").start()
    _ST["started"] = True
    return True


# ---------------------------------------------------------------- 讀檔給畫面

def read_all(limit_days=180):
    """
    每一天一列，新到舊。同一天以**後寫的為準**（sending → result）。

    ⛔ 每一列都要有去處：`fire + result + skip + eod + bad ＝ 檔案總列數`
       （這條等式是「有沒有東西被安靜吃掉」唯一的機器判準）。

    ⚠️ **`eod`（收盤平倉）那一列不可以用同一套合併規則往上蓋。**
       送單那件事與收盤平倉那件事是同一天的**兩件**事：直接 `update` 的話，
       `rec` 會被蓋成 eod、`why`／`why_msg` 會從「今天送了什麼」變成「收盤平了沒」——
       畫面上那一天就從「已送出委託單」變成別的東西。所以收在 `row["eod"]` 底下。
    """
    led = {"fire": 0, "result": 0, "skip": 0, "eod": 0, "bad": 0, "total": 0}
    days = {}
    if FIRE_DIR.exists():
        for p in sorted(FIRE_DIR.iterdir()):
            if not p.is_file() or not _MONTH_RE.match(p.name):
                continue
            try:
                lines = p.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue
            for line in lines:
                if not line.strip():
                    continue
                led["total"] += 1
                try:
                    o = json.loads(line)
                except Exception:
                    led["bad"] += 1
                    continue
                d = o.get("date") if isinstance(o, dict) else None
                rec = o.get("rec") if isinstance(o, dict) else None
                if not isinstance(d, str) or not _DATE_RE.match(d) \
                        or rec not in ("fire", "result", "skip", "eod"):
                    led["bad"] += 1
                    continue
                led[rec] += 1
                cur = days.setdefault(d, {"date": d})
                if rec == "eod":
                    # ⛔ 收在自己的抽屜裡，不准蓋掉「今天送了什麼」那幾個欄位
                    e = dict(cur.get("eod") or {})
                    e.update({k: v for k, v in o.items() if k != "rec"})
                    cur["eod"] = e
                    continue
                cur.update({k: v for k, v in o.items() if k != "rec"})
                cur["rec"] = rec
    out = []
    for d in sorted(days, reverse=True)[:limit_days]:
        r = days[d]
        # stage 停在 sending ＝ 決定送單之後程式中斷了。⛔ 不可以顯示成「沒送」
        if r.get("rec") == "fire" and r.get("stage") == "sending":
            r = dict(r)
            r["why"] = "crashed"
            r["why_msg"] = WHY["crashed"] + " —— 請自己到大戶投確認部位"
        if "rec" not in r:
            # 只有收盤平倉那一列、沒有送單那一列（理論上碰不到，但**空的 rec
            # 會讓畫面掉進「沒有送單」再讀不到 why_msg** ⇒ 一格空白）。
            e = r.get("eod") or {}
            r = dict(r, rec="skip", why=e.get("why"), why_msg=e.get("why_msg"))
        out.append(r)
    return out, led


_HIST_CACHE = {"key": None, "rows": [], "bad": 0, "dup": 0, "err": None}
_HIST_LOCK = threading.Lock()   # ⛔ 只保護這份快取（HTTP 執行緒會併發），跟停損那條路無關


def _hist_cached():
    """
    給 `state()`（HTTP 執行緒，每 5 秒一次）用的歷史。`(mtime_ns, size)` 快取 ——
    ⚠️ 穩態下一次 stat、零次讀檔（HTTP 執行緒跟 4Hz 主迴圈搶同一個 GIL）。
    ⛔ 快取鍵要帶 size（只比 mtime 會停在舊資料，`_auto_read_month` 踩過同一個坑）。
    """
    try:
        st = FAST_HIST.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = "missing"
    with _HIST_LOCK:
        if _HIST_CACHE["key"] == key:
            return dict(_HIST_CACHE)
    try:
        rows, bad, dup = hist_read() if key != "missing" else ([], 0, 0)
        val = {"key": key, "rows": rows, "bad": bad, "dup": dup, "err": None}
    except Exception as e:
        val = {"key": None, "rows": [], "bad": 0, "dup": 0,
               "err": "讀不出 %s：%s" % (FAST_HIST.name, str(e)[:100])}
    with _HIST_LOCK:
        _HIST_CACHE.update(val)
    return dict(val)


def fast_today(today, day_row=None):
    """
    畫面上「今天的門檻與判定」。⚠️ 唯讀。判斷一律走 `fast_verdict()`（跟送單同一支）。
    - 09:03:30 之前：只有門檻（%）與天數；verdict 是 None（⛔ 不預告）
    - 之後：歷史檔裡有今天那一列 ⇒ 用它判（開關關著的日子也看得到判定）
    - 帳本那一列若有 `fast`（送單那一刻判的）⇒ **以帳本為準**（那才是真的決定送不送的那一次）
    """
    h = _hist_cached()
    rows = h["rows"]
    if fast_pctl() is None:
        # 沒接起來（治具／設定沒跑到）⇒ 算不出門檻；⛔ 不猜百分位、⛔ 不讓 state() 整個崩掉
        return {"thr_pct": None, "n": None, "bad": h["bad"], "dup": h["dup"],
                "err": (h["err"] or "") + ("；" if h["err"] else "") + WHY["not_wired"],
                "window": FAST_RULE["window"], "pctl": None, "min_n": FAST_RULE["min_n"],
                "verdict": None, "move_pct": None, "move_pts": None, "thr_pts": None,
                "pts": None, "src": None}
    mine = next((r for r in rows if r["date"] == today), None)
    v = fast_verdict(today, mine["move_pct"] if mine else None, rows)
    ref = (mine or {}).get("ref")
    out = {"thr_pct": v["thr_pct"], "n": v["n"], "bad": h["bad"], "dup": h["dup"],
           "err": h["err"], "window": FAST_RULE["window"], "pctl": fast_pctl(),
           "min_n": FAST_RULE["min_n"],
           "verdict": v["verdict"] if mine else ("no_hist" if v["verdict"] == "no_hist" else None),
           "move_pct": None if not mine else mine["move_pct"],
           "move_pts": approx_points(mine["move_pct"], ref) if mine else None,
           "thr_pts": approx_points(v["thr_pct"], ref) if mine else None,
           "pts": tpsl_points((mine or {}).get("px")),
           "src": "hist" if mine else None}
    f = (day_row or {}).get("fast")
    if isinstance(f, dict) and f.get("verdict"):
        out.update({"verdict": f.get("verdict"), "move_pct": f.get("move_pct"),
                    "move_pts": f.get("move_pts"), "thr_pct": f.get("thr_pct"),
                    "thr_pts": f.get("thr_pts"), "n": f.get("n"), "src": "ledger"})
        if (day_row or {}).get("tp_points") is not None:
            out["pts"] = day_row.get("tp_points")
    return out


def state():
    """給 `/api/fire/state`。⚠️ **唯讀**，這個函式一張單都不會送。"""
    a = arm()
    rows, led = read_all()
    today = str(date.today())
    pos = broker._state.get("position")
    pos_sl = None
    if isinstance(pos, dict):
        pos_sl = {k: pos.get(k) for k in ("sl_points", "tp_points", "sl_src", "sl_warn",
                                          "recovered")}
    return {
        "armed": a["on"], "method": a["method"],
        "arm_why": a["why"], "arm_msg": a["msg"], "arm_raw": a["raw"],
        "flag": ARM_FLAG.name,
        # ⚠️ 「檔案在不在」跟「開得成不成」是兩件事：內容看不懂時 armed=False
        #    但檔案還在 ⇒ 那顆「關閉」鈕**還是要出現**，不然他關不掉那個壞檔。
        "flag_exists": flag_exists(),
        # ⚠️ 「剛剛關掉了」那句只在**開關真的不在**的時候才端出去 ——
        #    他關掉之後又自己把檔案建回來，畫面卻還掛著「已經關掉自動下單」
        #    就是一句假話（`_ST` 只活在記憶體裡，真相永遠是那個檔案）。
        "off_at": None if flag_exists() else _ST["off_at"],
        "off_msg": None if flag_exists() else _ST["off_msg"],
        "eod_at": _CFG["eod_at"],
        "eod_alarms": list(EOD_ALARM),
        # broker 那一層的開關。⛔ 兩個都要開才會真的送出去
        "live": broker.is_live(), "live_flag": broker.REAL_FLAG.name,
        "methods": [{"k": k, "name": METHOD_NAME[k], "sub": METHOD_SUB[k]}
                    for k in METHODS],
        "signal_at": _CFG["signal_at"],
        # ⭐ 規則數字（前端 ⛔ 不准寫死 40／80／20／0.5%）
        "rule": {"window": FAST_RULE["window"], "pctl": fast_pctl(),
                 "min_n": FAST_RULE["min_n"],
                 "tpsl_pct": round(FAST_RULE["tpsl_frac"] * 100, 6)},
        "fast": fast_today(today, next((r for r in rows if r.get("date") == today), None)),
        "hist_msg": _ST["hist_msg"],
        # 現在那一口部位用的停損點數從哪來（自動下單那一口／手動那一套／撿回來對不上）
        "pos_sl": pos_sl, "rec_msg": _ST["rec_msg"],
        "qty": broker.QTY, "max_entries": broker.MAX_ENTRIES,
        "entries_today": broker.entries_today(),
        "wired": _ST["wired"], "started": _ST["started"],
        "err": _ST["err"], "err_n": _ST["err_n"],
        "days": rows, "ledger": led,
        "today": today, "now": datetime.now().strftime("%H:%M:%S"),
        "why_texts": dict(WHY),
    }
