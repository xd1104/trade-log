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
- **內容是 `A`** ⇒ 用「快攻回馬槍」（見下面〈2026-09-15 規則〉與〈2026-09-15 晚上〉）。
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

================================================================
⭐⭐ 2026-09-15 晚上：「快攻回馬槍」＝ 上面的快攻 ＋ 慢的日子等 09:15 反轉
================================================================
研究在 tick-research/scripts/defs_research.py（「09:15 方向相反就算」那一列）與
retest/FINAL_REPORT.md。Benson 決定隔天起自動下單換這一套（⛔ 開關檔內容仍然是 A）。
  1. 09:03:30：**快** ⇒ 跟上面一模一樣（順勢送、帳本記 `leg:"fast"`）。
     開關關著／報價不能用／算不出訊號（no_signal）／歷史不夠（no_hist）⇒ 跟以前一樣記原因、
     **當天結束，⛔ 不進回馬槍**。
  2. 09:03:30 判定**不快** ⇒ ⛔ 不再寫終局的 `not_fast`，改落地一列 `rec:"wait"`：
     **一定帶 09:03:30 的 px（`px`）與方向 `d`（+1／−1）**——看門狗在 09:03:30~09:15 之間
     重啟是常態，09:15 那一刻**只准從檔案讀回來判斷**（⛔ 不靠記憶體）。
  3. 09:15:00（`live_panel.REV_AT`／`REV_SEC`，經 configure 接進 `_CFG["rev_at"]`／`["rev_sec"]`）：
     主迴圈跨過那一刻時用**同一支 `_auto_snap`** 快照，`on_reversal()` 只 put_nowait；
     晚超過 `AUTO_LATE_MS` ⇒ 快照給 None ⇒ 這裡記 `late`、⛔ 不補單。
  4. 工作執行緒（`_rev()`）：今天帳本**沒有 wait** ⇒ 什麼都不做；**已經有 fire／result／skip**
     （快攻送過、或那天早就有定論）⇒ 什麼都不做（一天只准一筆）。
     `d2 = sign(p15 − px_0903)`；**`d2 != 0` 且 `d2 != d` ＝反轉** ⇒ 順 d2 送 1 口，
     `pts = round(p15 × 0.005)`、sl_points 同值，走同一條 can_enter／enter（先落地 sending 再送），
     帳本記 `leg:"reversal"`、`p15`、`px_0903`。
     沒反轉（同方向或一樣價）⇒ 終局 skip `no_reversal`，那句話寫出兩個價。
     ⛔ 09:15 會**重新讀 arm()**：09:03:30 之後被關掉 ⇒ 不送（off）。
  ⛔ 判斷只准走 `reversal_dir()`（送單與 `tools/probe/fast-rule-replay.py` 同一支）。

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

# ⛔⛔⛔ 2026-09-17 Benson 裁示：**真單繼續跑「快攻回馬槍」（A），⛔ 不要換成多方聯軍。**
#    原話：「快攻就做他自己的，你現在在修的，就繼續弄，我還是要讓他現在繼續用快攻回馬槍下真單」
#    ⇒ 多方聯軍是**多一個可以選的做法（U）**，⛔ 不是取代 A。
#    ⇒ ⛔⛔ **A 那條路的行為必須跟 `main`（45be573）一模一樣** ——
#       所有多方聯軍才有的閘門（只做多、開箱、一天三個候選）一律掛在 `_union_on()` 底下，
#       `test_auto_fire.py` ⑲ 有一整節在守（配突變：把預設值改成 U ⇒ 必須翻紅）。
#
# ⚠️ B（開盤起）2026-09-15 起不再是可下單的選項；
#    **C 與 D 刻意不可用** —— 那兩個是【自動下單（模擬）】那一頁的對照組代號
#    （C＝要 30 點、D＝不判斷），帳本那一列的 `method` 會被 `alSimTxt()` 拿去對
#    模擬那邊的 `runs[method]`，多方聯軍用 C 的話會**對到模擬的「要 30 點」那一條**
#    ⇒ 畫面上安靜地配錯一對數字。所以多方聯軍用 **U**（union），⛔ 不用 C。
#    ⚠️ `live_panel.fire_arm_on()` 拿這個 tuple 驗 mode（⛔ 不自己寫一份）。
METHODS = ("A", "U")
# ⭐ **預設做法**＝他不去動 `AUTO_ORDERS_ON` 的內容時用的那一個。
#    ⛔ 這個常數只給「面板要建檔時預填什麼」與測試用；真正在跑的一律以**檔案內容**為準。
#    ⚠️ 2026-09-23 v3 改成 U：Benson 09-17 起真單用多方聯軍，而 v3 的日盤畫面**只有一顆「打開」、
#       不給選**（「日盤就放多方聯軍就好」），那顆鈕帶的就是這個常數。停在 A 的話，
#       關著時畫面寫「快攻回馬槍」、按下去開的也是 A —— 他關掉再從面板打開就換了規則。
DEFAULT_METHOD = "U"
# 畫面上的名字。⛔ 代號不准上畫面（他退件過一次：「我要從哪裡知道現在我看的是哪個做法？」）。
#    ⚠️ 名字從「5 分 K」改成「開盤快才做」：方向的算法沒變（仍是 09:00 那根起算），
#       但「今天做不做」多了一道快不快 —— 名字只寫方向的話，他會以為每天都送。
#    ⚠️ 2026-09-15 晚上再改成「快攻回馬槍」：慢的日子也可能在 09:15 送（反轉才送），
#       名字還寫「開盤快才做」就是一句假話。
#    ⛔⛔ 2026-09-17：A 這個代號**改回**「快攻回馬槍」（2026-09-16 那一版把它改成
#       「多方聯軍」是錯的 —— 那等於一 merge 就把他的真單換了規則）。多方聯軍是 U。
#    ⛔⛔ **改這裡的字串會把舊紀錄一起改名** —— 正解是 `RULE_ID`／`RULE_NAME`
#       （送單時把規則代號寫進帳本那一列，畫面照那一列走），見下面那一段。
METHOD_NAME = {"A": "快攻回馬槍", "U": "多方聯軍"}
# 帳本那一列是哪一段送的（畫面要看得出來）。⛔ 代號不上畫面，一律走這張表。
# ⚠️ 這張表是 **A（快攻回馬槍）** 在用的（快攻／回馬槍兩段）。
#    多方聯軍（U）改用 `cand`／`CAND_NAME`（一天最多三個候選各一列），⛔ 這張表不准改名
#    ——改了就是把 09-16 那幾天的紀錄改成另一個名字。
LEG_NAME = {"fast": "快攻", "reversal": "回馬槍"}
METHOD_SUB = {"A": "09:00 起算",
              "U": "三個候選裡最早觸發的那個做多"}

# ⭐⭐⭐ **規則代號寫進帳本那一列**（PM 2026-09-17 裁示 M3 的「長遠那半」）。
#    問題：`METHOD_NAME["A"]` 一改，**舊紀錄會當場跟著改名**（09-15 晚上與 09-16
#    連續踩過兩次），只好靠前端一排日期閘門（`AL_HMQ_FROM`／`AL_UNION_FROM`）擋 ——
#    而那種閘門要「上線日」猜對才會對，猜錯就是安靜地把舊紀錄標成新規則。
#    ⇒ 根治：**送單那一刻就把規則代號寫進那一列**（`rule`），畫面照帳本那一列走。
#    ⛔⛔ `RULE_NAME` **只准新增、不准改既有的值** —— 規則改了就發一個**新代號**。
#    ⚠️ 舊帳本沒有 `rule` 這個欄位 ⇒ 前端 `alRecName()` 退回原本那套日期閘門
#       ⇒ 舊紀錄的名字一個字都不會變。
RULE_ID = {"A": "hmq", "U": "union"}
RULE_NAME = {"hmq": "快攻回馬槍", "union": "多方聯軍"}

# ⛔⛔ 做法代號 → **方向怎麼算**（`live_panel.auto_dirs()` 那張表的 key）。
#    A 與 U 的快攻那一段用的是**同一個方向算法**（09:03:30 的價 − 09:00 的價），
#    所以兩個都指到 `"A"`。⛔ 這張表是必要的：`auto_dirs()` 回的 key 是 A/B/C/D
#    （那是【模擬】那一頁四種算法的代號），拿 `"U"` 去查會回 None ⇒
#    多方聯軍的每一天都會變成「算不出訊號」（安靜地整條規則失效）。
DIR_KEY = {"A": "A", "U": "A"}

# ⭐⭐ 多方聯軍的三個候選。⛔ 順序就是「觸發時刻一樣時誰先」的定序
#    （跟 `sim_lanes.UNION_TIE` 同一組：fast 0 ／ orb 1 ／ rev 2），
#    `test_auto_fire.py` 有一條比對兩邊。
CANDS = ("fast", "orb", "rev")
# ⛔ 名字一律用候選自己的正式名字：**純回馬**（「回馬槍」是舊規則整條的名字，不是候選）。
CAND_NAME = {"fast": "快攻", "orb": "開箱", "rev": "純回馬"}
# 候選觸發的時刻（畫面用；開箱是「第一次突破那一筆」的時間，不是固定時刻）。
CAND_AT = {"fast": "09:03:30（＝訊號時刻）", "orb": "09:05 之後第一次突破箱子那一刻",
           "rev": "09:15（＝回馬槍時刻）"}
# 帳本裡「這一列不屬於任何一個候選，是整天的事」（開關關著、面板沒接起來…）。
# ⚠️ **舊帳本那幾列沒有 `cand` 欄位 ⇒ 一律當成 `day`** —— 那正是舊資料的語意
#    （那時候一天就只有一件事），所以舊紀錄的畫面一個字都不會變。
CAND_DAY = "day"

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
# 開關檔寫得出「現在有哪幾個做法可以選」那句話。⛔ 正本只有這一份（arm() 與 fire_arm_on 共用）。
# ⚠️ 2026-09-17 多了 U ⇒ 這句話不能再寫死「只有 A」（那會變成一句假話）。
MSG_METHODS = "現在可以選的是：" + "、".join(
    "%s（%s）" % (k, METHOD_NAME[k]) for k in METHODS)
# 開關檔寫 B（或面板上有人送 mode=B）時的那句話。
MSG_ONLY_A = "B 已經不支援。" + MSG_METHODS

# ══════════════════════════════════════════════════════════════════════
# ⭐⭐ 開箱（ORB）—— 多方聯軍的第三個候選（2026-09-16 加）
# ══════════════════════════════════════════════════════════════════════
# 箱子 ＝ 09:00:00.000 ~ 09:05:00.000（兩端都含）的最高／最低。
# 之後**第一次**穿出箱子 ⇒ 上緣用 `>` 做多、下緣用 `<` 做空（碰到邊不算），一天最多 1 次。
# **停損 ＝ 箱子的另一端**；⛔ **不設停利**（`broker.enter(..., tp_points=None)`）。
# 箱子太窄（箱子寬度% < 過去 20 個交易日的中位數）⇒ 今天這個候選不可用。
def _ms_of(hms):
    """「HH:MM:SS」／「HH:MM:SS.mmm」⇒ 當日毫秒數。看不懂 ⇒ None（⛔ 不猜）。
    ⚠️ tick_logs 的 `t` 是**永豐給的交易所時間**，不是本機時鐘（見 tick_writer 檔頭）。"""
    try:
        hh, mm, rest = str(hms).split(":")
        sec, _dot, frac = rest.partition(".")
        out = int(hh) * 3600000 + int(mm) * 60000 + int(sec) * 1000
        if frac:
            out += int((frac + "000")[:3])
        return out if 0 <= out < 86400000 else None
    except Exception:
        return None


ORB_BOX_FROM_AT = "09:00:00"
ORB_BOX_TO_AT = "09:05:00"

# ⛔⛔⛔ **突破的截止時刻**（PM 2026-09-16 裁示 2）：09:30 以後才第一次穿出箱子 ⇒
#    **當天開箱這個候選不可用**（其他兩個候選照跑）。
#
#    ⚠️⚠️ **09:30 是實作限制，⛔ 不是因為它數字最好**：真單判突破一律讀面板自己錄的
#       `tick_logs`（⛔ 不准用 4Hz 的 `st.price` —— 實測會晚 23 分鐘、差 121 點、
#       方向還錯 2 天，那是第二把尺），而 `tick_writer` 只錄到 09:30。
#    PM 已照「上線前 7 項檢查清單」重驗過各種截止時刻（scratchpad/orb_cutoff.py，
#    保守進場價、其餘一字不改），⛔ 這張表要跟這個常數放在一起，免得以後有人
#    以為 09:30 是調出來的參數：
#
#        突破截止      每月     t    筆數  其中開箱  丟掉的多單  最大連虧
#        不限（現行）   +256  2.85   517    194        0      2124
#        09:30        +262  2.94   512    189        7      2018
#        09:45        +258  2.88   516    193        2      2124
#        10:00~11:00  +256  2.85   517    194        0      2124
#
# ⛔⛔ **不變式**：這個時刻**必須 ≤ tick_writer 的錄製結束時刻**（`live_panel.WATCH_END`
#    餵給 `live_panel.TICKS.win_end`）。兩者不一致就是 bug ——「09:30 之後沒突破」
#    會變成「錄不到所以看不見」，而畫面上完全看不出差別。
#    `test_auto_fire.py` 有一條**直接讀 tick_writer 的設定**斷言這件事。
ORB_BREAK_BY = "09:30:00"

# ⛔⛔ 開箱那一段**最晚做到幾點**（2026-09-16 實測抓到的坑）：
#    送單執行緒是 **24 小時醒著的**（`_worker` 每 0.5 秒一圈），而開箱是「自己看時鐘」
#    的那一段 —— ⛔ 不像 09:03:30／09:15 有主迴圈的 `sess == "day"` 幫忙擋。
#    沒有這道上界 ⇒ **半夜、週末、國定假日都會跑一輪**，然後在帳本上寫一列
#    「今天沒有錄到逐筆」—— 那是一句假話（那天根本沒開盤），而且會永久留在只 append 的檔裡。
#    ⇒ 只在 `09:05 < 現在 ≤ ORB_LAST_AT` 之間做事，而且**今天的 tick_logs 檔要真的存在**
#      （面板那天有錄 ＝ 那天真的有開盤；⛔ 「查不到」不可以寫成「沒有突破」）。
#    ⚠️ 11:00 是「看門狗重啟後還來得及把今天的結論補寫進帳本」的餘裕（突破截止是 09:30）。
#      ⛔ 刻意不用 13:45／DAY_END —— 那是 live_panel 的尺，抄過來就是第二把尺。
ORB_LAST_AT = "11:00:00"

# ⭐ 開箱的規則數字。⚠️ 跟 FAST_RULE 同一個理由收成 dict（leak-scan：模組層級
#    「全大寫＝字面數字」的常數會被當成公開值，20／50 正好落在他真實紀錄的點數尺度）。
#   hist_n         箱子寬度%的中位數看過去幾個交易日（⛔ 不含今天）
#   span_max_days  那幾天的日曆跨度上限；超過 ⇒ 這個候選不可用（資料有洞，
#                  「跟最近的波動比」就不是那個意思了）。
#   ⚠️ 兩個數字的正本是 `sim_lanes.ORB_HIST_N` / `sim_lanes.ORB_SPAN_MAX_DAYS`
#      （50 是 lab-dev 用 520 天逐筆量出來的，見那邊的註解）。
#      ⛔ 這裡是**第二份**，所以 `test_auto_fire.py` 有一條直接比對兩邊、對不上就紅。
#      （auto_fire 不 import sim_lanes：sim_lanes 會把 pandas 與 strategy_lab 拖進
#        送單這條路，而那條路上任何一個 import 都是風險。）
ORB_RULE = {"hist_n": 20, "span_max_days": 50}

# 面板自己錄的逐筆（tick_writer 寫的）。⛔ 真單判突破只准讀這個，⛔ 不准用 4Hz 的 st.price。
TICK_DIR = HERE / "tick_logs"
# 箱子寬度%的歷史（一天一列）。⛔ 一定要 gitignore（裡面是每天 09:00~09:05 的高低價）。
# 種子由 `build_orb_hist.py` 從 tick_hist 的逐筆建；之後面板每天自己補一列。
ORB_HIST = HERE / "orb_hist.jsonl"

ORB_BOX_FROM_MS = _ms_of(ORB_BOX_FROM_AT)
ORB_BOX_TO_MS = _ms_of(ORB_BOX_TO_AT)
ORB_BREAK_BY_MS = _ms_of(ORB_BREAK_BY)
ORB_LAST_MS = _ms_of(ORB_LAST_AT)

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
    # ⚠️ 2026-09-15 晚上起 09:03:30 不快**不再寫 not_fast**（改寫 wait）；這一句留給舊帳本那幾天。
    "not_fast": "今天開盤不夠快 —— 照規則今天不做",
    # ── 2026-09-15 晚上「快攻回馬槍」（⛔ 跟上面每一句都不一樣）───────────
    "wait_rev": "今天開盤不夠快 —— 等回馬槍那一刻看有沒有反轉（還沒有定論）",
    "no_reversal": "回馬槍那一刻的價跟 09:03:30 比沒有反轉 —— 今天不做",
    "wait_bad": "「等反轉」那一列讀不出 09:03:30 的價或方向 —— 不猜，今天不做",
    "no_hist": "過去的開盤走幅紀錄不夠多天，算不出「快」的門檻 —— 照規則不做",
    # ── 2026-09-16「多方聯軍」：三個候選各自一句話（⛔ 跟上面每一句都不一樣）────────
    # ⛔ 「只做多」那三句一定要分開（將來看紀錄時「哪個候選說了做空」意義完全不同）。
    "fast_short": "快攻判定做空 —— 多方聯軍只做多，這個候選今天不用",
    "rev_short": "反轉後的方向是做空 —— 多方聯軍只做多，這個候選今天不用",
    "orb_short": "跌破箱子（做空）—— 多方聯軍只做多，這個候選今天不用",
    "no_long": "今天三個候選都沒有給出做多 —— 照規則今天不做",
    "no_cand_rev": "純回馬只有「09:03:30 判定不快」的日子才有 —— 今天沒有這個候選",
    "union_done": "今天已經照另一個候選送出去了 —— 一天最多一口，這個候選不再看",
    # ── 開箱（ORB）自己的每一種不可用 ──────────────────────────────────
    "orb_no_box": "09:00~09:05 沒有成交，畫不出箱子 —— 開箱這個候選今天不可用",
    "orb_narrow": "開箱：箱子太窄（比過去的中位數窄）—— 這個候選今天不可用",
    "orb_no_break": "開箱：%s 前沒有突破，這個候選今天不可用" % ORB_BREAK_BY[:5],
    "orb_no_hist": "開箱：過去的箱子寬度紀錄不夠多天，算不出中位數 —— 這個候選今天不可用",
    "orb_span": "開箱：箱子寬度歷史跨的日曆天數太多（資料有洞）—— 這個候選今天不可用",
    "orb_bad_sl": "開箱：箱子的另一端算不出停損 —— 這個候選今天不可用",
    "orb_wait": "開箱：箱子已經畫好，等突破（還沒有定論）",
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

# ⭐ 回馬槍那一刻（09:15）的「沒送」。⛔ 理由代號沿用上面那幾個（late／no_quote／quote_stale／
#    mid_only），但**那句話不可以沿用** —— WHY 裡寫的是「09:03:30 收不到成交價」，
#    拿去講 09:15 那一刻就是一句假話。`%s` 填 `_CFG["rev_at"]`（⛔ 不寫死 09:15）。
#    測試斷言這幾句互不相同、也不跟 WHY 任何一句相同。
REV_MSG = {
    "late": "面板在回馬槍那一刻（%s）沒開著、或剛啟動 —— 那一刻跳過，不補單",
    "no_quote": "回馬槍那一刻（%s）收不到成交價 —— 今天不做",
    "quote_stale": "回馬槍那一刻（%s）的報價太舊（斷線中），不能用舊價下單 —— 今天不做",
    "mid_only": "回馬槍那一刻（%s）只有中價、還沒有成交，不能拿它當進場價 —— 今天不做",
}

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
# ⭐ `rev_at`／`rev_sec`（2026-09-15 晚上）：回馬槍那一刻，正本 `live_panel.REV_AT`／`REV_SEC`。
#    沒接 ⇒ wired=False ⇒ 09:03:30 與 09:15 都不送（⛔ 不猜一個 09:15）。
_CFG = {"signal_at": None, "signal_sec": None, "late_ms": None, "gap_s": None,
        "sig_fn": None, "dirs_fn": None, "eod_at": None, "pctl": None,
        "rev_at": None, "rev_sec": None}

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
      on=True  ⇒ method 一定在 `METHODS` 裡（A＝快攻回馬槍／U＝多方聯軍）
      on=False ⇒ why 一定講得出原因（off／bad_method／unreadable）

    ⛔⛔ 讀不懂就是讀不懂，**不准挑一個預設做法** —— 尤其現在有兩個做法可以選，
       猜錯就是用另一條規則下他的真錢。（`DEFAULT_METHOD` 只是「面板要建檔時預填什麼」。）
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
        msg = (WHY["bad_method"] + "：檔案是空的。要用請寫一個 %s 進去（%s）"
               % (DEFAULT_METHOD, MSG_METHODS))
    elif txt == "B":
        # ⛔⛔ 2026-09-15 起 B 不支援。他 09-09 以前開過 B 的話，檔案裡還是 B ⇒
        #    這句話一定要講清楚「是規則換了」，⛔ 不可以只說「看不懂」（他會以為檔案壞了）。
        msg = WHY["bad_method"] + "：讀到「B」。" + MSG_ONLY_A
    elif txt in ("C", "D"):
        # ⛔ C（要 30 點）與 D（不判斷）刻意不支援 —— 那是【自動下單（模擬）】那一頁的
        #    對照組代號。講清楚，不要只說「看不懂」。
        msg = (WHY["bad_method"] + "：讀到「%s」——「C」與「D」只有"
               "【自動下單（模擬）】那一頁在跑，不是真單的做法。%s" %
               (_clean(txt), MSG_METHODS))
    else:
        msg = (WHY["bad_method"] + "：讀到「%s」。%s"
               % (_clean(raw.strip()), MSG_METHODS))
    return {"on": False, "method": None, "why": "bad_method",
            "msg": msg, "raw": _clean(raw.strip())}


def _union_on(a=None):
    """
    ⭐⭐⭐ **現在跑的是不是「多方聯軍」（U）。這是 A 與 U 唯一的分水嶺。**

    ⛔⛔ 多方聯軍才有的每一道（只做多、開箱這個候選、一天三個候選各一列）
       **一律掛在這一支底下** —— 這樣 `AUTO_ORDERS_ON` 寫 `A` 的時候，
       送單那條路跟 `main`（45be573）**一個判斷都不差**（那是 Benson 2026-09-17
       明確要求的：「我還是要讓他現在繼續用快攻回馬槍下真單」）。
    ⛔ 讀的是**檔案**（`arm()`），⛔ 不是記憶體裡的快取 —— 他可以在盤中把檔案改掉，
       而「現在該用哪條規則」永遠以那個檔案為準（跟 `_sent()` 看檔案同一條規矩）。
    """
    return (a or arm()).get("method") == "U"


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
    """
    ⛔ 一定是 open("a")。看門狗重啟是常態，覆寫＝把當天稍早的紀錄弄丟。

    ⭐⭐ 2026-09-17（PM 裁示 M3）：**落地時把規則代號蓋上去**（`rule`）。
    ⛔ 這裡是帳本唯一的寫入出口 ⇒ 蓋在這裡就不會有哪一種列漏掉。
    ⚠️ 只有「知道是哪個做法」的列才有（`method` 認得出來）——
       開關關著那幾列本來就沒有做法可言，沒有 `rule`，畫面退回舊的日期閘門。
    ⛔ 已經有 `rule` 的不覆蓋（以後若有別的地方先填好了，以那一份為準）。
    """
    if not row.get("rule"):
        _rid = RULE_ID.get(row.get("method"))
        if _rid:
            row["rule"] = _rid
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


def _cand_of(o):
    """
    這一列屬於哪個候選。⚠️ **舊帳本沒有 `cand` 欄位 ⇒ 一律當成 `day`** ——
    那正是舊資料的語意（那時候一天就只有一件事），所以舊紀錄合併後一個欄位都不會變。
    """
    c = o.get("cand") if isinstance(o, dict) else None
    return c if c in CANDS else CAND_DAY


def _sent(d, rows=None):
    """
    ⭐⭐ 今天**送過單了嗎** —— 這是防重送唯一的閘門。**看檔案，不看記憶體。**
    （看門狗在 09:03:30 前後重啟時會重跑一次判斷，沒有這道就會送出第二張單。）

    ⛔⛔ 判準是「帳本裡有 `fire` 或 `result`」，**不是「今天有任何一列」**
       （PM 2026-09-16 裁示 4）：一天最多三個候選各一列，光看「有沒有列」的話，
       第一個候選寫完 skip 之後，當天**其餘兩個候選就永遠送不出去了**。
    """
    rows = _rows_of(d) if rows is None else rows
    return any(o.get("rec") in ("fire", "result") for o in rows)


def _cand_done(d, cand, rows=None):
    """這個候選今天已經有自己的一列（＝已經有定論）了嗎。"""
    rows = _rows_of(d) if rows is None else rows
    return any(_cand_of(o) == cand for o in rows)


def _wait_row(rows):
    """
    09:03:30 判定「不快」那一列 —— 回馬槍要從它讀回 09:03:30 的價與方向。
    ⚠️ 兩種格式都要認得：**舊帳本**是 `rec:"wait"`；**新帳本**是快攻那個候選的
       `skip`（`why:"not_fast"`）。⛔ 只認一種就是把另一半的日子判成「沒有候選」。
    """
    out = None
    for o in rows:
        if o.get("rec") == "wait" or (_cand_of(o) == "fast" and o.get("why") == "not_fast"):
            out = o
    return out


def _skip(d, why, extra=None, msg=None, cand=CAND_DAY):
    """
    沒送。**每一種都要落地、都要有原因**（畫面上看得到）。

    `cand`＝這一列屬於哪個候選（`fast`／`orb`／`rev`），或 `day`＝整天的事
    （開關關著、面板沒接起來）。⛔ 一天最多三個候選各一列。
    """
    row = {"rec": "skip", "date": d, "why": why,
           "why_msg": msg or WHY.get(why, why),
           "live": broker.is_live(),
           "wrote_at": datetime.now().isoformat(timespec="seconds")}
    row.update(extra or {})
    # ⛔ extra 不准蓋掉 cand（那會讓兩個候選併成一列）。
    # ⛔⛔ 2026-09-17：**「整天的事」那幾列不寫 `cand`** —— `_cand_of()` 讀不到就當 `day`，
    #    所以語意一個字都沒差；但這樣 A（快攻回馬槍）落地的每一列**跟 main 的形狀一樣**
    #    （PM 要求「他不改設定就重啟面板，行為跟現在完全一樣」）。
    if cand == CAND_DAY:
        row.pop("cand", None)
    else:
        row["cand"] = cand
    _ST["last"] = row
    # ⚠️ 那句話自己已經以候選的名字開頭時就不要再加一次（會變成「開箱：開箱：…」）
    head = "" if cand == CAND_DAY or str(row["why_msg"]).startswith(CAND_NAME[cand]) \
        else (CAND_NAME[cand] + "：")
    print("[%s] 自動下單：沒有送單 —— %s%s" % (d, head, row["why_msg"]), flush=True)
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


def reversal_dir(px_0903, d, p15):
    """
    ⭐ 「09:15 有沒有反轉」唯一的判斷（送單 `_rev()` 與離線對照 fast-rule-replay.py 同一支）。
      px_0903  09:03:30 那一刻的價（wait 那一列落地的 `px`）
      d        09:03:30 那一刻的方向（+1／−1，wait 那一列的 `d`）
      p15      09:15 那一刻的價
    回 **d2（+1／−1）＝反轉了、要順 d2 做**；沒反轉（同方向／一樣價）或拿不到 ⇒ None。
    ⛔ `d2 = sign(p15 − px_0903)`；**`d2 != 0` 且 `d2 != d`** 才算（研究 defs_research.py：
       `opp = -d * (p2 - px)`，`opp <= 0` 不做 —— 一樣價就是 opp = 0 ⇒ 不做）。
    ⛔ d 不是 ±1 ⇒ None（⛔ 不猜方向）。
    """
    a, b = _num(px_0903), _num(p15)
    if a is None or b is None or isinstance(d, bool) or d not in (1, -1):
        return None
    d2 = (b > a) - (b < a)
    if d2 != 0 and d2 != d:
        return d2
    return None


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


# ═════════════════════════════════════════════════════ 開箱（ORB）：純函式
#
# ⛔ 這幾支是開箱規則的**正本**：`_orb_step()`（送單）、`state()`（畫面）、
#    `build_orb_hist.py`（種子）全部呼叫同一份。
# ⚠️ `sim_lanes.py` 有**第二份**（那邊吃 numpy 陣列、回測用 13:43:30 當截止）——
#    `test_auto_fire.py` 有一條餵同一天的資料給兩邊、比對箱子／突破／箱子寬度%，
#    對不上就紅。⛔ 不准只改一邊。

def orb_box_pct(w, fill):
    """箱子寬度% ＝ 箱寬 ÷ **進場價** × 100（濾網比的就是這個值）。算不出 ⇒ None。"""
    w, fill = _num(w), _num(fill)
    if w is None or fill is None or fill <= 0 or w < 0:
        return None
    return w / fill * 100.0


def orb_fill(d, px, bid, ask):
    """進場價：多用賣價、空用買價，拿不到（或 0）就用成交價（⛔ 跟快攻同一種口徑）。"""
    px = _num(px)
    q = _num(ask) if d > 0 else _num(bid)
    return q if q else px


def orb_sl_points(d, fill, hi, lo):
    """停損點數 ＝ 進場價到**箱子另一端**的距離。算不出或 ≤0 ⇒ None（⛔ 不猜）。"""
    fill, hi, lo = _num(fill), _num(hi), _num(lo)
    if fill is None or hi is None or lo is None:
        return None
    sl = round(fill - lo, 1) if d > 0 else round(hi - fill, 1)
    return sl if sl > 0 else None


def orb_med(vals):
    """箱子寬度%的中位數（取**最近** hist_n 天）。天數不夠 ⇒ None（呼叫端當「這個候選不可用」）。"""
    v = [float(x) for x in (vals or [])]
    if len(v) < ORB_RULE["hist_n"]:
        return None
    return float(np.median(np.asarray(v[-ORB_RULE["hist_n"]:], dtype=float)))


def orb_span(dates):
    """最近 hist_n 天的日曆跨度（天）。⚠️ 日期不足或看不懂 ⇒ None。"""
    ds = [d for d in (dates or []) if isinstance(d, str) and _DATE_RE.match(d)]
    ds = ds[-ORB_RULE["hist_n"]:]
    if len(ds) < ORB_RULE["hist_n"]:
        return None
    try:
        return (date.fromisoformat(ds[-1]) - date.fromisoformat(ds[0])).days
    except Exception:
        return None


def orb_span_bad(dates):
    """
    窗口跨度太寬 ⇒ 回那句原因（呼叫端要當成「這個候選今天不可用」）；沒問題 ⇒ None。
    ⛔ 跨度上限的理由與量測見 `sim_lanes.ORB_SPAN_MAX_DAYS` 的註解（他的逐筆有過 16 個月的洞，
       「過去 20 天」可能有 12 天是一年多以前的 —— 那就不是「跟最近的波動比」了）。
    """
    sp = orb_span(dates)
    if sp is None or sp <= ORB_RULE["span_max_days"]:
        return None
    return ("箱子寬度歷史跨了 %d 天（%s~%s），超過 %d 天 —— 資料有洞，這幾天不是「最近的波動」"
            % (sp, dates[-ORB_RULE["hist_n"]], dates[-1], ORB_RULE["span_max_days"]))


def orb_hist_read(path=None):
    """
    讀 `orb_hist.jsonl` ⇒ (rows 依日期舊到新, 壞列數, 重複天數)。
    ⛔ 規矩跟 `hist_read()` 一模一樣：壞列跳過並計數（「安靜地少」是明令禁止的失敗模式）、
       同一天第二列只認第一列並計數、整個檔讀不出來就讓例外往外丟。
    一列：{"date","hi","lo","w","fill","box_pct","src"}；**只有 `date` 與 `box_pct` 是必要的**。
    """
    p = path or ORB_HIST
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
        bp = _num(o.get("box_pct")) if isinstance(o, dict) else None
        if not isinstance(d, str) or not _DATE_RE.match(d) or bp is None or bp < 0:
            bad += 1
            continue
        if d in seen:
            dup += 1
            continue
        seen.add(d)
        rows.append({"date": d, "box_pct": bp, "hi": _num(o.get("hi")),
                     "lo": _num(o.get("lo")), "fill": _num(o.get("fill")),
                     "src": o.get("src")})
    rows.sort(key=lambda r: r["date"])
    return rows, bad, dup


def orb_threshold(hist_rows, day):
    """
    今天的箱子寬度門檻。`hist_rows`＝`orb_hist_read()` 的 rows。
    回 {"med", "n", "dates", "span", "why"}：`why` 不是 None ⇒ **這個候選今天不可用**。
    ⛔ 只用 `date < day` 的那幾天（⛔ 不含今天 —— 今天那一列是拿來給以後用的）。
    """
    past = [r for r in (hist_rows or []) if r.get("date", "") < day]
    dates = [r["date"] for r in past]
    vals = [r["box_pct"] for r in past]
    med = orb_med(vals)
    out = {"med": med, "n": len(vals), "dates": dates[-ORB_RULE["hist_n"]:],
           "span": orb_span(dates), "why": None, "msg": None}
    if med is None:
        out["why"] = "orb_no_hist"
        out["msg"] = ("開箱：過去的箱子寬度只有 %d 天（至少要 %d 天才算得出中位數）"
                      " —— 這個候選今天不可用" % (len(vals), ORB_RULE["hist_n"]))
        return out
    bad = orb_span_bad(dates)
    if bad:
        out["why"], out["msg"] = "orb_span", "開箱：" + bad
    return out


# ── 讀面板自己錄的逐筆（tick_logs）──────────────────────────────────────
#
# ⛔⛔ **真單判突破只准讀這裡**（PM 2026-09-16 裁示 2）。
#    ⛔ 不准用 4Hz 主迴圈的 `st.price`：實測那把尺會晚 23 分鐘、差 121 點、方向還錯 2 天。
#    ⛔ 也不准動 `on_tick` 熱路徑（那條路是他的停損）—— 這裡是**送單執行緒**在讀檔。

def _tick_path(d):
    return TICK_DIR / (str(d) + ".jsonl")


def tick_feed(path, pos=0):
    """
    從位元組 `pos` 起把 tick_logs 讀進來（**增量**：一天十萬列，⛔ 不可以每 0.5 秒整檔重讀）。

    回 {"pos", "trades": [(t_ms, price)], "quotes": [(t_ms, bid, ask)], "bad", "drops", "err"}
      ・`trades`／`quotes` 都**照交易所時間排序過**（檔案不保證單調遞增，見 tick_writer 檔頭）
      ・`drops` ＝ 這一段裡 tick_writer 自己記的丟棄痕跡列（k=x）筆數 —— ⛔ 要端到畫面
      ・讀不到／讀壞 ⇒ `err` 有字，`pos` 不動（下一輪再試；⛔ 不可以把 pos 推過去）

    ⚠️ **只讀到最後一個完整的換行為止**：寫檔執行緒可能剛好寫到一半，
       半列 JSON 解不開會被算成 bad ⇒ 那一列就永遠漏掉了。
    """
    out = {"pos": pos, "trades": [], "quotes": [], "bad": 0, "drops": 0, "err": None}
    try:
        if not path.exists():
            return out
        with path.open("rb") as f:
            f.seek(pos)
            buf = f.read()
    except Exception as e:
        out["err"] = "讀不出 %s：%s" % (path.name, str(e)[:100])
        return out
    cut = buf.rfind(b"\n")
    if cut < 0:
        return out                      # 還沒有一列是完整的
    out["pos"] = pos + cut + 1
    for line in buf[:cut].split(b"\n"):
        if not line.strip():
            continue
        try:
            o = json.loads(line.decode("utf-8"))
        except Exception:
            out["bad"] += 1
            continue
        if not isinstance(o, dict):
            out["bad"] += 1
            continue
        k = o.get("k")
        if k == "h":
            continue                    # 檔頭（面板每重啟一次就多一列）
        if k == "x":
            out["drops"] += 1           # ⛔ 丟棄痕跡：不可以安靜地少
            continue
        t = _ms_of(o.get("t"))
        if t is None:
            out["bad"] += 1
            continue
        if k == "t":
            p = _num(o.get("p"))
            if p is None or p <= 0:
                out["bad"] += 1
                continue
            out["trades"].append((t, p))
        elif k == "b":
            out["quotes"].append((t, _num(o.get("b")), _num(o.get("a"))))
        # ⛔ 不認得的 k 整列跳過（tick_writer 檔頭明寫「之後加新種類不會弄壞舊程式」），
        #    ⚠️ 但**不算 bad** —— 那是相容性，不是壞掉。
    out["trades"].sort(key=lambda x: x[0])
    out["quotes"].sort(key=lambda x: x[0])
    return out


def orb_box_of(trades):
    """
    箱子 ＝ 09:00:00.000 ~ 09:05:00.000（**兩端都含**，跟快攻同一種「≤」口徑）的最高／最低。
    回 {"hi","lo","w","last","n"}；那段沒有成交 ⇒ None。
    `last` ＝ 箱子裡最後一筆成交價（沒有突破的日子拿它當箱子寬度%的分母）。
    """
    seg = [p for t, p in trades if ORB_BOX_FROM_MS <= t <= ORB_BOX_TO_MS]
    if not seg:
        return None
    hi, lo = max(seg), min(seg)
    return {"hi": hi, "lo": lo, "w": hi - lo, "last": seg[-1], "n": len(seg)}


def orb_break_of(trades, hi, lo, by_ms=None):
    """
    箱子之後**第一次**穿出箱子的那一筆 ⇒ {"t_ms","p","d"}；沒有 ⇒ None。
    ⛔ 上緣用 `>`、下緣用 `<`（碰到邊不算突破）；⛔ 一天最多 1 次（只認第一筆）。
    ⛔ 只看 `ORB_BOX_TO_MS < t <= by_ms`（預設 `ORB_BREAK_BY_MS`）—— 見 ORB_BREAK_BY 的說明。
    """
    by = ORB_BREAK_BY_MS if by_ms is None else by_ms
    for t, p in trades:
        if t <= ORB_BOX_TO_MS or t > by:
            continue
        if p > hi:
            return {"t_ms": t, "p": p, "d": 1}
        if p < lo:
            return {"t_ms": t, "p": p, "d": -1}
    return None


def _hms(ms):
    """當日毫秒數 ⇒ HH:MM:SS（突破時刻是**那一筆成交的時間**，不是固定時刻）。"""
    ms = int(ms)
    return "%02d:%02d:%02d" % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60)


def _quote_at(quotes, t_ms, prev):
    """
    突破**那一刻**的買賣價 ＝ 時間 ≤ t_ms 的最後一筆（`quotes` 已排序）。
    這一批裡沒有 ⇒ 沿用 `prev`（上一批結束時的那一筆）。
    ⛔ 不可以直接拿整批最後一筆：一批可能含好幾秒，那是比突破**更晚**的價。
    """
    b, a = prev
    for t, bb, aa in quotes:
        if t > t_ms:
            break
        if bb or aa:
            b, a = bb, aa
    return b, a


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


def _fire(snap, day, lag_ms, put_at, leg="fast"):
    """
    ⚠️ **跑在自己的 daemon 執行緒上**（⛔ 不是主迴圈）：這裡會呼叫券商 API、
       會等成交（最多 `broker.FILL_WAIT` 秒）、會寫檔 —— 任何一項放進 4Hz 主迴圈
       都等於**把他的停損塞住幾秒**。
    ⭐ `leg="reversal"`（09:15 那一件，`on_reversal()` 丟進來的）⇒ 交給 `_rev()`。
       ⚠️ 刻意**共用同一條佇列與同一個入口**：09:03:30 那一件一定排在 09:15 前面處理完
       （FIFO），09:15 讀帳本時一定看得到 09:03:30 寫的那一列。
    """
    if leg == "reversal":
        return _rev(snap, day, lag_ms, put_at)
    d = day
    # ⛔⛔ **A 與 U 在這裡就分岔了**（2026-09-17 Benson 裁示：真單繼續跑 A）。
    #    ⚠️ 防重送的閘門**兩條規則的判準不一樣**，⛔ 不可以只留一份：
    #      ・A（快攻回馬槍）：一天只有一件事 ⇒ **今天有任何一列就收工**（＝ main 的 `_has`）。
    #        ⛔ 換成 `_sent` 的話，09:03:30 寫完 wait 之後看門狗重啟，這一段會再寫一列。
    #      ・U（多方聯軍）：一天最多三個候選各一列 ⇒ 判準是「有沒有 fire／result」，
    #        再加「這個候選自己有沒有定論」。
    _u = _union_on()
    rows = _rows_of(d)
    if _u:
        if _sent(d, rows):
            return None                  # 一天一口。⛔ 這道在最前面
        if _cand_done(d, "fast", rows):
            return None                  # 快攻這個候選今天已經有定論（看門狗重啟）
    elif rows:
        return None                      # A：一天一次。⛔ 這道在最前面
    # ⚠️ `_cf0` 是**還沒讀開關之前**那幾條早退路徑用的（只有「晚到」那一條）；
    #    讀完開關之後一律用下面那個 `_cf`。⛔ 兩個名字刻意不一樣 ——
    #    同名的話突變只會打到第一個、而第二個又把它蓋回來 ＝ **那個突變等於沒跑**
    #    （2026-09-17 突變 ⓤ9 實測踩到）。
    _cf0 = "fast" if _u else CAND_DAY    # A 的帳本沒有候選這個概念（⛔ 不寫 cand 欄位）
    if not _ST["wired"]:
        return _skip(d, "not_wired")     # ⛔ 整天的事（U：三個候選都送不出去）
    if snap is None:
        # 主迴圈說「跨過 09:03:30 了，但已經晚太多」（面板 09:10 才開起來／看門狗剛重啟）
        # ⚠️ U：只有**快攻與純回馬**這兩個候選沒了（它們吃 09:03:30 的快照）；
        #    **開箱照跑** —— 它讀的是 tick_logs，跟這份快照無關。
        return _skip(d, "late", {"at_lag_ms": lag_ms}, cand=_cf0)

    # ── 開盤走幅歷史：先把今天那一列寫進去。⛔ 排在「開關開不開」之前 ——
    #    歷史是以後 40 天門檻的材料，只記送單的日子就是一份有偏差的歷史。
    hist = _hist_step(d, snap)

    a = arm()
    # ⛔ 從這裡開始一律用 `a` 這一份（⛔ 不再去讀第二次檔案 —— 同一輪裡兩把尺）
    _u = _union_on(a)
    _cf = "fast" if _u else CAND_DAY
    base = {"method": a["method"], "arm_raw": a["raw"],
            "at": snap.get("at"), "at_lag_ms": snap.get("at_lag_ms"),
            "px": _num(snap.get("px")), "quote_age_ms": snap.get("quote_age_ms"),
            "quote_gaps": snap.get("quote_gaps"),
            # ⛔ 今天那一列有沒有寫進歷史、沒寫的話為什麼 —— 落地在每一種結局上
            "hist": {"wrote": hist["wrote"], "why": hist["why"],
                     "bad": hist["bad"], "dup": hist["dup"], "err": hist["err"]}}
    if not a["on"]:
        # ⛔ 開關關著／內容看不懂 ＝ **整天**都不送（⛔ 不是只有快攻這個候選）
        return _skip(d, a["why"], base, a["msg"])

    q = _quote_why(snap)
    if q:
        return _skip(d, q, base, cand=_cf)

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
    # ⛔ 走 `DIR_KEY`（⛔ 不是直接拿 method 當 key）—— 見那張表的說明。
    dv = dirs.get(DIR_KEY.get(a["method"]))
    mv = move_pct(px, ref)
    # ⛔ 方向（09:00 那一分鐘第一筆）或走幅（09:00 以前最後一筆）任一個算不出來 ⇒ 不送。
    #    兩個參考價在 _auto_snap 裡是同兩個來源、順序相反，所以實際上會一起有、一起沒有。
    if dv is None or mv is None:
        return _skip(d, "no_signal", base, cand=_cf)
    if dv == 0:
        # A 不會回 0（那是 C 的門檻），留著是防呆：真的回 0 就是不做，⛔ 不猜方向
        return _skip(d, "no_trade", base, cand=_cf)
    direction = "long" if dv > 0 else "short"
    base["dir"] = direction

    # ── ⭐ 快不快（2026-09-15）。⛔ 判斷只准走 fast_verdict()（畫面與離線對照用同一支）
    if hist["rows"] is None:
        base["fast"] = {"verdict": "no_hist", "move_pct": round(mv, 4)}
        return _skip(d, "no_hist", base,
                     WHY["no_hist"] + "（" + str(hist["err"] or "歷史檔讀不出來") + "）",
                     cand=_cf)
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
                     ("過去的開盤走幅只有 %d 天（至少要 %d 天才算得出門檻）"
                      "—— 快攻與純回馬這兩個候選今天不可用（開箱照跑）" if _u else
                      "過去的開盤走幅只有 %d 天（至少要 %d 天才算得出門檻）—— 照規則今天不做")
                     % (v["n"], FAST_RULE["min_n"]), cand=_cf)
    if v["verdict"] != "fast":
        # ⛔⛔ **px 與 d 一定要落地**：看門狗在 09:03:30~09:15 之間重啟是常態，
        #    09:15 那一刻只准從檔案讀回來判斷（`_rev()` 走 `_wait_row()`），⛔ 不靠記憶體。
        # ⚠️ 刻意不帶 `dir`：這一列**不是部位**（`_auto_entry` 只認 result ok），
        #    留一個 dir 會讓畫面上這一列看起來像有方向。方向記在 `d`／`dir_0903`。
        slow = dict(base)
        slow.pop("dir", None)
        # ⚠️ `px` 不必再寫一次：`base` 裡那一份就是同一個值（`_num(snap.get("px"))`）。
        #    寫兩份的話，拿掉任何一份都不會有東西紅 ＝ 兩邊互相遮住（2026-09-16 突變測試抓到）。
        slow.update({"d": dv, "dir_0903": direction, "rev_at": _CFG["rev_at"]})
        if not _u:
            # ── A（快攻回馬槍）：⛔ **這一段跟 main 一個字都不能差**。
            #    不快 ⇒ 還沒有定論，落地一列 `rec:"wait"`，等 09:15 看反轉。
            #    ⛔ 不可以改成 U 那種 `rec:"skip"`（why=not_fast）：`read_all()` 的
            #       硬不變式把 wait 算成 wait、skip 算成 skip，換一種就是把他既有的
            #       紀錄換一個形狀；而且「還沒有定論」正是 A 在這一刻的真實語意。
            wait = dict(slow)
            wait.update({"rec": "wait", "date": d, "why": "wait_rev",
                         "why_msg": "今天開盤不夠快（走 %.2f%%／門檻 %.2f%%）—— 等 %s 看有沒有反轉"
                                    % (mv, v["thr_pct"], _CFG["rev_at"]),
                         "px": px, "live": broker.is_live(),
                         "wrote_at": datetime.now().isoformat(timespec="seconds")})
            _ST["last"] = wait
            print("[%s] 自動下單：%s" % (d, wait["why_msg"]), flush=True)
            return _append(wait)
        # ── U（多方聯軍）：「不快」⇒ **快攻這個候選今天不用**，但那一天還沒完
        #    （純回馬與開箱照跑）⇒ 快攻這個候選在這一刻就有定論了 ⇒ 寫 `rec:"skip"`。
        return _skip(d, "not_fast", slow,
                     "快攻：今天開盤不夠快（走 %.2f%%／門檻 %.2f%%）—— 這個候選今天不用，"
                     "等 %s 看純回馬有沒有反轉" % (mv, v["thr_pct"], _CFG["rev_at"]),
                     cand="fast")
    if _u and dv < 0:
        # ⭐⭐ 多方聯軍**只做多**（⛔ 說做空的略過，但當天要繼續看下一個候選，不是收工）。
        #    ⚠️ 快攻判定「快且做空」的日子 ⇒ ⛔ **沒有純回馬這個候選**（純回馬只有
        #       「09:03:30 判定不快」的日子才有）—— 今天只剩開箱。
        #    ⛔⛔ 這一道**只有 U 有**：A（快攻回馬槍）夠快就照方向做，做空照送。
        return _skip(d, "fast_short", base,
                     "快攻：今天夠快（走 %.2f%% ≥ 門檻 %.2f%%）但方向是做空 —— "
                     "多方聯軍只做多，這個候選今天不用（只剩開箱）"
                     % (mv, v["thr_pct"]), cand="fast")
    base["leg"] = "fast"
    if _u:
        base["cand"] = "fast"
    # ⭐ 停利停損 ±0.5% of 09:03:30 的價（⛔ 不是成交價：送單之前就要定下來、落地）
    pts = tpsl_points(px)
    base["tp_points"] = pts
    base["sl_points"] = pts
    return _send(d, base, a, direction, px, pts, lag_ms, put_at, snap,
                 "走 %.2f%% ≥ 門檻 %.2f%%" % (mv, v["thr_pct"]), tp_points=pts)


def _send(d, base, a, direction, px, pts, lag_ms, put_at, snap, how, tp_points=None):
    """
    ⚠️ **工作執行緒**：遲到檢查 → 先落地 sending → can_enter → enter(sl_points) → result。
    三個候選（快攻 09:03:30／開箱 突破那一刻／純回馬 09:15）**共用這一段**
    （⛔ 不准各寫一份送單流程 —— 兩把尺）。
    `lag_ms` 是主迴圈跨過**那一刻**的延遲（開箱沒有「那一刻」⇒ 傳 0）。
    `pts` ＝ **停損**點數；`tp_points` ＝ 停利點數，⭐ **None ＝ 這一口不設停利**
    （開箱那個候選；`broker.enter` 會一次 `place_target` 都不呼叫）。
    """
    # 【第二道遲到檢查】上面那道是主迴圈跨過 09:03:30 的延遲；這一道是
    # 「排隊 ＋ 排到我開始做」的延遲。市價單晚幾秒送出去，成交價就不是那一刻的價了。
    late = (lag_ms or 0) + (time.time() - put_at) * 1000.0
    if late > LATE_MS:
        base["late_ms"] = int(late)
        # ⛔ 回馬槍那一刻晚到，那句話要講 09:15（WHY["late"] 寫的是 09:03:30）
        head = (REV_MSG["late"] % _CFG["rev_at"]) if base.get("leg") == "reversal" \
            else WHY["late"]
        return _skip(d, "late", base,
                     head + "（實際晚了 %.1f 秒）" % (late / 1000.0),
                     cand=_cand_of(base))

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
                "要用自動下單就讓面板一直開著。）" % (
                    (_CFG["rev_at"] if base.get("leg") == "reversal" else _CFG["signal_at"])
                    or "09:03:30")
                ) if "還沒連上永豐" in why else ""
        return _result(d, base, False, "cant_enter",
                       WHY["cant_enter"] + "：" + why + hint)

    # ⚠️ A 的帳本沒有 `cand` ⇒ 那一段用 `LEG_NAME`（跟 main 同一句話）；
    #    U 才有候選的名字。⛔ 兩者不要混成一句（他要看得出現在跑的是哪條規則）。
    _seg = (("的「%s」" % CAND_NAME[_cand_of(base)]) if _cand_of(base) in CANDS
            else ("的" + LEG_NAME.get(base.get("leg"), "")))
    print("[%s] 自動下單：用「%s」%s判定 %s（%s），送出 1 口，%s，停損 %g 點（%s）" %
          (d, METHOD_NAME[a["method"]], _seg,
           "做多" if direction == "long" else "做空", how,
           "不設停利" if tp_points is None else ("停利 %g 點" % tp_points), pts,
           "真單" if broker.is_live() else "演練，不會真的送出去"), flush=True)
    # ⛔⛔ sl_points 一定要帶：停損活在面板迴圈（check_real_position），它讀的是
    #     **這一口部位自己的** sl_points；沒帶就掉回手動真單的 SL_POINTS（130）⇒ 提早被洗掉。
    # ⭐ tp_points=None ⇒ broker 一次 place_target 都不呼叫（開箱那個候選不設停利）。
    ok, err, pos = broker.enter(direction, px, tp_points, sl_points=pts)
    if not ok:
        return _result(d, base, False, "order_failed",
                       WHY["order_failed"] + "：" + str(err or "券商沒有說原因"))
    entry = _num((pos or {}).get("entry"))
    tp = None if (entry is None or tp_points is None) else \
        round(entry + (tp_points if direction == "long" else -tp_points), 1)
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
                  # ⭐⭐ 這一口**券商端有沒有任何掛單**。開箱那個候選 no_tp=True ⇒
                  #    券商端一張單都沒有（永豐又沒有停損單）⇒ 停損與收盤平倉都靠面板。
                  #    ⛔ 這件事要寫進畫面那一列與 reason（PM 2026-09-16 指定）。
                  "no_tp": bool((pos or {}).get("no_tp")),
                  # ⛔ 停利掛失敗不可以吞掉：broker 會回 ok=True ＋ 一句警告
                  "warn": err or None})
    return _result(d, extra, True, None, None)


def _fmt_px(x):
    """價格給那句話用：整數價不帶小數（23456），不是整數才寫一位。"""
    x = _num(x)
    if x is None:
        return "—"
    return ("%.0f" % x) if x == int(x) else ("%.1f" % x)


def _rev(snap, day, lag_ms, put_at):
    """
    ⭐⭐ 09:15 那一件。A（快攻回馬槍）叫它「回馬槍」，U（多方聯軍）叫它「純回馬」。
    ⚠️ **工作執行緒**（⛔ 不是主迴圈）。
    ⛔⛔ 「今天要不要做」**只看檔案**：看門狗在 09:03:30~09:15 之間重啟是常態，
       記憶體裡什麼都沒有也要判得出來 ⇒ 09:03:30 的 px 與方向一律從帳本那一列讀
       （`_wait_row()`：A 是 `rec:"wait"`，U 是快攻那列的 not_fast）。
    """
    d = day
    _u = _union_on()
    if _u:
        # ⭐⭐ **先把開箱補掃一次再判**：開箱若在 09:15 之前就突破了，它比純回馬早觸發
        #    ⇒ 多方聯軍要挑開箱那一個。⛔ 這一步一定要在下面的 `_sent()` 之前。
        #    ⛔⛔ **只有 U 有這一段**：A 沒有開箱這個候選（⛔ 一次都不准叫）。
        #    ⚠️ 已知限制：tick_writer 每 1 秒才把 tick 寫進磁碟 ⇒ 09:14:59 之後那一秒內的
        #       突破這一刻還讀不到，那種日子會挑到純回馬。⛔ 不為此加等待（那會吃掉送單的
        #       遲到預算），也⛔ 不去讀記憶體裡的價（那是第二把尺）。
        #    ⚠️ now_ms 要給**回馬槍那一刻**（⛔ 不是本機此刻）：要問的是「到 09:15 為止
        #       開箱突破了沒」。給本機時鐘的話，看門狗 11:00 才重啟那天會用一個 11:00 的
        #       尺去判 09:15 的事。
        try:
            _orb_step(d, (_CFG["rev_sec"] or 0) * 1000 + int(lag_ms or 0))
        except Exception as e:
            _ST["err"] = "orb(rev): " + str(e)[:140]
            _ST["err_n"] += 1
    rows = _rows_of(d)
    if _u:
        if _sent(d, rows):
            return None     # ⛔ 一天一口：快攻或開箱已經送出去了
        if _cand_done(d, "rev", rows):
            return None     # 純回馬這個候選今天已經有定論（看門狗重啟）
    wait = _wait_row(rows)
    if wait is None:
        # 今天 09:03:30 沒有判成「不快」（快攻夠快／關著／沒訊號…）⇒ 09:15 沒有事。
        # ⛔ **不落地任何一列**：09:03:30 那一列已經把原因講完了，再寫一列只是把同一件事
        #    講第二次（而且會讓「09:15 有沒有多做事」變得看不出來）。
        return None
    if not _u and any(o.get("rec") in ("fire", "result", "skip") for o in rows):
        # ── A（快攻回馬槍）：⛔ 這一道跟 main 一模一樣 —— 一天只准一筆，
        #    已經送過、或 09:15 那一件已經有定論（看門狗重啟）⇒ 不做。
        return None
    rev_at = _CFG["rev_at"]
    px0, d0 = _num(wait.get("px")), wait.get("d")
    _cr = "rev" if _u else CAND_DAY
    base = {"leg": "reversal", "method": wait.get("method"),
            "px_0903": px0, "d_0903": d0, "rev_at": rev_at, "at_lag_ms": lag_ms,
            "fast": wait.get("fast")}
    if _u:
        base["cand"] = "rev"
    if not _ST["wired"]:
        return _skip(d, "not_wired", base, cand=_cr)
    if snap is None:
        # 主迴圈說「跨過 09:15 了，但已經晚太多」⇒ ⛔ 不補單
        return _skip(d, "late", base, REV_MSG["late"] % rev_at, cand=_cr)
    p15 = _num(snap.get("px"))
    base.update({"at": snap.get("at"), "at_lag_ms": snap.get("at_lag_ms"),
                 "p15": p15, "px": p15, "quote_age_ms": snap.get("quote_age_ms"),
                 "bid": _num(snap.get("bid")), "ask": _num(snap.get("ask"))})
    # ⛔ 09:15 **重新讀開關**：09:03:30 之後他按了「關閉」⇒ 不送
    a = arm()
    base["arm_raw"] = a["raw"]
    if not a["on"]:
        return _skip(d, a["why"], base, a["msg"], cand=_cr)
    base["method"] = a["method"]
    q = _quote_why(snap)
    if q:
        return _skip(d, q, base, REV_MSG[q] % rev_at, cand=_cr)
    if px0 is None or isinstance(d0, bool) or d0 not in (1, -1):
        return _skip(d, "wait_bad", base, cand=_cr)
    d2 = reversal_dir(px0, d0, p15)
    base["d2"] = d2
    if d2 is None:
        return _skip(d, "no_reversal", base,
                     ("純回馬：%s 價 %s、%s 價 %s，沒有反轉 —— 這個候選今天不用" if _u
                      else "%s 價 %s、%s 價 %s，沒有反轉 —— 今天不做") % (
                         _CFG["signal_at"], _fmt_px(px0), rev_at, _fmt_px(p15)),
                     cand=_cr)
    if _u and d2 < 0:
        # ⭐⭐ 多方聯軍**只做多**：反轉後的方向是做空 ⇒ 略過這個候選。
        #    ⚠️ 這一刻開箱可能還沒突破 ⇒ 那一天仍然可能由開箱送出去（⛔ 不是收工）。
        #    ⛔⛔ 這一道**只有 U 有**：A（快攻回馬槍）反轉成做空照樣送。
        return _skip(d, "rev_short", base,
                     "純回馬：%s 價 %s → %s 價 %s 反轉成做空 —— 多方聯軍只做多，這個候選今天不用"
                     % (_CFG["signal_at"], _fmt_px(px0), rev_at, _fmt_px(p15)),
                     cand="rev")
    direction = "long" if d2 > 0 else "short"
    base["dir"] = direction
    # ⭐ 停利停損 ±0.5% of **09:15 的價**（⛔ 不是 09:03:30 的價、不是成交價）
    pts = tpsl_points(p15)
    base["tp_points"] = pts
    base["sl_points"] = pts
    return _send(d, base, a, direction, p15, pts, lag_ms, put_at, snap,
                 "%s 價 %s → %s 價 %s，反轉" % (_CFG["signal_at"], _fmt_px(px0),
                                              rev_at, _fmt_px(p15)), tp_points=pts)


# ═══════════════════════════════════════════ 開箱（ORB）：每 0.5 秒看一次有沒有突破
#
# ⚠️⚠️ **跑在送單執行緒上**（`_worker` 的 POLL_S 迴圈）：這裡會讀檔、會呼叫券商 API。
#    ⛔ 一行都不准搬進 4Hz 主迴圈，⛔ 也不准動 `on_tick` 熱路徑（那條路是他的停損）。
#
# 一天的流程：
#   09:05 之後第一次進來 ⇒ 讀 tick_logs 畫箱子 ＋ 讀 orb_hist 算門檻
#     ・箱子畫不出來／歷史不夠／跨度太寬 ⇒ 落地一列「這個候選今天不可用」，收工
#   箱子畫好之後 ⇒ 每一輪把新寫進 tick_logs 的成交讀進來找**第一次**突破
#     ・突破了 ⇒ ⭐ **這一刻才判箱寬濾網**（分母＝進場價，PM 2026-09-16 裁示 3，
#       跟回測 `sim_lanes.orb_calc` 同一把尺）⇒ 夠寬且做多 ⇒ 送單；做空／太窄 ⇒ 落地不做
#     ・到了 ORB_BREAK_BY 還沒突破 ⇒ 落地一列「09:30 前沒有突破」，收工
#   ⛔ 不管送不送，今天那一列箱子寬度%都要寫進 orb_hist（歷史是以後 20 天的材料，
#      只記送單的日子就是一份有偏差的歷史 —— 跟 fast_hist 同一條規矩）。

_ORB = {"date": None, "pos": 0, "box": None, "hit": None, "bid": None, "ask": None,
        "thr": None, "done": False, "msg": None, "bad": 0, "drops": 0, "err": None,
        "hist_wrote": False, "seen_ms": None, "acc": None}


def _orb_reset(d):
    # `acc`＝箱子那一段的累加器（增量讀檔 ⇒ ⛔ 不可以只看「這一批」有沒有箱子，
    #        第一批讀完之後那幾列就再也不會出現了）。
    _ORB.update({"date": d, "pos": 0, "box": None, "hit": None, "bid": None, "ask": None,
                 "thr": None, "done": False, "msg": None, "bad": 0, "drops": 0,
                 "err": None, "hist_wrote": False, "seen_ms": None,
                 "acc": {"hi": None, "lo": None, "last": None, "n": 0}})


def _orb_acc(trades):
    """把這一批裡屬於箱子時段的成交累進 `_ORB["acc"]`（⛔ 增量，跨批累加）。"""
    a = _ORB["acc"]
    for t, p in trades:
        if not (ORB_BOX_FROM_MS <= t <= ORB_BOX_TO_MS):
            continue
        a["hi"] = p if a["hi"] is None else max(a["hi"], p)
        a["lo"] = p if a["lo"] is None else min(a["lo"], p)
        a["last"] = p
        a["n"] += 1
    return a


def _orb_box_from_acc():
    """累加器 ⇒ 箱子（形狀跟 `orb_box_of()` 一模一樣）。⛔ 一筆都沒有 ⇒ None。"""
    a = _ORB["acc"]
    if not a or not a["n"]:
        return None
    return {"hi": a["hi"], "lo": a["lo"], "w": a["hi"] - a["lo"],
            "last": a["last"], "n": a["n"]}


def _orb_hist_step(d, box, fill):
    """
    今天那一列箱子寬度% 寫進 `orb_hist.jsonl`。⛔ 同一天不重寫（看檔案）、⛔ 一定是 append。
    ⛔ 永遠不往外丟例外（它出錯不可以讓送單那一段崩掉）。
    """
    if _ORB["hist_wrote"]:
        return
    bp = orb_box_pct(box["w"], fill)
    if bp is None:
        return
    try:
        rows, _bad, _dup = orb_hist_read()
        if any(r["date"] == d for r in rows):
            _ORB["hist_wrote"] = True
            return
        ORB_HIST.parent.mkdir(parents=True, exist_ok=True)
        with ORB_HIST.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"date": d, "hi": box["hi"], "lo": box["lo"],
                                "w": round(box["w"], 1), "fill": fill,
                                "box_pct": round(bp, 6), "src": "panel"},
                               ensure_ascii=False) + "\n")
            f.flush()
        _ORB["hist_wrote"] = True
    except Exception as e:
        _ORB["err"] = "寫不進 %s：%s" % (ORB_HIST.name, str(e)[:100])
        _ST["err"], _ST["err_n"] = "orb_hist: " + _ORB["err"], _ST["err_n"] + 1
        print("⚠️ [自動下單] " + _ORB["err"], flush=True)


def _orb_end(d, why, extra=None, msg=None):
    """開箱這個候選今天收工（落地一列）。"""
    _ORB["done"] = True
    _ORB["msg"] = msg or WHY.get(why, why)
    return _skip(d, why, extra, msg, cand="orb")


def _orb_step(d=None, now_ms=None):
    """
    ⚠️ **送單執行緒**。回 True＝這一輪有落地什麼（測試用），否則 False。
    `now_ms`＝現在的當日毫秒數（測試會塞；正式走本機時鐘）。
    ⛔ 永遠不往外丟例外由呼叫端（`_worker`／`_rev`）負責；這裡只管規則。

    ⛔⛔⛔ **開箱只屬於「多方聯軍」（U）。** `AUTO_ORDERS_ON` 寫 `A` 的時候
       這一支從頭到尾什麼都不做：不讀 tick_logs、不落地任何一列、不送任何單
       （Benson 2026-09-17：真單繼續跑快攻回馬槍）。`test_auto_fire.py` ⑲ 在守。
    """
    now = datetime.now()
    d = str(date.today()) if d is None else str(d)
    if now_ms is None:
        now_ms = (now.hour * 3600 + now.minute * 60 + now.second) * 1000 \
            + now.microsecond // 1000
    if _ORB["date"] != d:
        _orb_reset(d)
    if _ORB["done"]:
        return False
    # ⛔⛔⛔ **這三道一定要在最前面**（在任何會落地、會讀檔的分支之前）：
    #    這一段是「自己看時鐘」的（送單執行緒 24 小時醒著，每 0.5 秒一圈），
    #    ⛔ 不像 09:03:30／09:15 有主迴圈的 `sess == "day"` 幫忙擋。
    if not (ORB_BOX_TO_MS < now_ms <= ORB_LAST_MS):
        # ⛔⛔ 箱子還沒畫完（09:05 以前）／已經過了今天的窗口（半夜、週末、假日）⇒ 什麼都不做。
        #    見 ORB_LAST_AT 的說明：這條執行緒 24 小時醒著，沒有這道就會在沒開盤的日子寫假紀錄。
        return False
    # ⛔⛔ **做法閘門**：不是多方聯軍就沒有開箱這個候選。
    #    ⚠️ 刻意排在時間窗口**後面** —— `arm()` 會讀檔，而這支每 0.5 秒被叫一次，
    #       09:05~11:00 之外一次都不該去碰磁碟。
    _a = arm()
    if not _union_on(_a):
        # ⛔ **不可以在這裡把 `done` 立起來**：他有可能 09:06 才把開關檔從 A 改成 U，
        #    立了就等於「今天再也不看開箱」＝安靜地少一個候選。每一輪重問一次就好
        #    （`arm()` 是 64 位元組的檔，這條執行緒本來每輪就在讀整個月的帳本）。
        _ORB["msg"] = ("開箱：這個候選只有「%s」才有 —— 現在用的是「%s」"
                       % (METHOD_NAME["U"],
                          METHOD_NAME.get(_a.get("method")) or "（自動下單沒開）"))
        return False
    if not _tick_path(d).exists():
        # ⛔⛔ 面板今天**一筆逐筆都沒錄到** ⇒ 那天要嘛沒開盤、要嘛面板整個早上沒開著。
        #    ⛔ 不可以寫成「今天沒有突破」（那是一句假話）——不落地，只在畫面上講。
        _ORB["msg"] = "開箱：今天沒有錄到逐筆（面板那個早上有開著嗎）—— 判不出箱子"
        return False
    rows = _rows_of(d)
    # ⛔⛔ **這一道要排在 `_sent()` 前面**：開箱自己送出去的那一天，`_sent()` 也是 True ⇒
    #    順序反過來就會在它自己的 result 後面再蓋一列 union_done ⇒ 合併後那一口不見了
    #    ⇒ 收盤平倉判成「今天沒有部位」（實測過）。
    if _cand_done(d, "orb", rows):
        _ORB["done"] = True             # 看門狗重啟：今天開箱已經有定論
        return False
    if _sent(d, rows):
        # 快攻或純回馬已經送出去了 ⇒ ⛔ 一天一口。仍然要落地一列（畫面上三個候選都要有交代）。
        _orb_end(d, "union_done", {"leg": "orb"})
        return True
    if not _ST["wired"]:
        return False                    # ⛔ 沒接起來就不送；理由那一列由 09:03:30 那段寫

    # ── 把新寫進 tick_logs 的資料讀進來（增量）
    feed = tick_feed(_tick_path(d), _ORB["pos"])
    if feed["err"]:
        _ORB["err"] = feed["err"]
    _ORB["pos"] = feed["pos"]
    _ORB["bad"] += feed["bad"]
    _ORB["drops"] += feed["drops"]
    # ⚠️ 先把「這一批之前最後一筆買賣價」留著：突破的進場價要用**突破那一刻**的買賣價，
    #    ⛔ 不可以用整批最後一筆（那是比突破更晚的價 —— 一批可能含好幾秒）。
    prev_q = (_ORB["bid"], _ORB["ask"])
    for _t, b, a_ in feed["quotes"]:
        if b or a_:
            _ORB["bid"], _ORB["ask"] = b, a_
    if feed["trades"]:
        # ⚠️ 取**最大值**（檔案不保證單調遞增，見 tick_writer 檔頭）
        _ORB["seen_ms"] = max(_ORB["seen_ms"] or 0, feed["trades"][-1][0],
                              max(t for t, _p in feed["trades"]))

    # ── 箱子：只畫一次
    _orb_acc(feed["trades"])
    if _ORB["box"] is None:
        # ⛔⛔ **箱子要等「讀到 09:05 之後的第一筆」才算畫完**：`tick_writer` 每 1 秒才
        #    flush 一次 ⇒ 09:05:00.5 這一輪讀到的檔案很可能還少了最後一秒的成交，
        #    那一秒剛好是新高／新低的話，箱子的上下緣就少算了（而箱子只畫一次、改不回來）。
        #    ⇒ 沒讀到 09:05 之後的成交就先不畫；真的一整天都沒有 ⇒ 到 ORB_BREAK_BY 才收。
        if (_ORB["seen_ms"] or 0) <= ORB_BOX_TO_MS and now_ms < ORB_BREAK_BY_MS:
            _ORB["msg"] = ("開箱：還在等 %s 之後的第一筆（箱子還沒畫完）" % ORB_BOX_TO_AT[:5])
            return False
        _ORB["box"] = _orb_box_from_acc()
        if _ORB["box"] is None:
            # 還沒有箱子（面板 09:05 之後才開、檔案還沒出現、那段真的沒成交）。
            # ⛔⛔ **不在這一刻下定論**：定論要留到 ORB_BREAK_BY —— 中間看門狗重啟、
            #    檔案晚一點才寫出來都是常態，提早寫死一列就再也改不回來了（只 append）。
            if now_ms < ORB_BREAK_BY_MS:
                _ORB["msg"] = ("開箱：還沒讀到 %s~%s 的逐筆（面板那段有開著嗎）"
                               % (ORB_BOX_FROM_AT[:5], ORB_BOX_TO_AT[:5]))
                return False
            # ⚠️ 走到這裡一定有 tick 檔（上面擋過了）⇒ 就是「那段沒有成交／面板開太晚」
            _orb_end(d, "orb_no_box", {"leg": "orb"})
            return True
        # 箱子畫好了 ⇒ 門檻也算一次（⛔ 這裡還**不判箱寬**，那一刀在突破那一刻）
        try:
            hrows, hbad, hdup = orb_hist_read()
        except Exception as e:
            _ORB["err"] = "讀不出 %s：%s" % (ORB_HIST.name, str(e)[:100])
            _orb_end(d, "orb_no_hist", {"leg": "orb", "orb_err": _ORB["err"]},
                     "開箱：箱子寬度歷史讀不出來（%s）—— 這個候選今天不可用" % _ORB["err"])
            return True
        _ORB["thr"] = orb_threshold(hrows, d)
        _ORB["thr"].update({"bad": hbad, "dup": hdup})

    box, thr = _ORB["box"], _ORB["thr"]
    base = {"leg": "orb", "box_hi": box["hi"], "box_lo": box["lo"],
            "box_w": round(box["w"], 1), "box_n": box["n"],
            "box_at": "%s~%s" % (ORB_BOX_FROM_AT[:5], ORB_BOX_TO_AT[:5]),
            "break_by": ORB_BREAK_BY,
            "orb_hist": {"n": thr["n"], "med_pct": None if thr["med"] is None
                         else round(thr["med"], 4), "span": thr["span"],
                         "bad": thr.get("bad", 0), "dup": thr.get("dup", 0)},
            "tick_bad": _ORB["bad"], "tick_drops": _ORB["drops"]}
    if thr["why"]:
        # 歷史不夠／跨度太寬 ⇒ 這個候選今天不可用。
        # ⛔ 今天那一列箱子寬度%**照樣要寫進歷史**（不然永遠補不滿 20 天）。
        _orb_hist_step(d, box, box["last"])
        _orb_end(d, thr["why"], base, thr["msg"])
        return True

    # ── 找第一次突破（⛔ 只認第一筆、⛔ 只看 09:05 之後到 ORB_BREAK_BY）
    if _ORB["hit"] is None:
        # ⭐ 2026-09-17（lab-qa 建議 3）：上界取 `min(ORB_BREAK_BY_MS, now_ms)` ——
        #    ⛔ 讓程式跟上面那句註解一致：「只看 09:05 之後到**現在**（最晚到 ORB_BREAK_BY）」。
        #    ⚠️ 用意是 `_rev()` 補掃那一次：它問的是「到 09:15 為止突破了沒」，
        #       檔案裡若已經有 09:20 的成交（增量讀一次讀進一大段），沒有這個上界就會
        #       拿一筆**比 09:15 還晚**的突破當答案 ⇒ 純回馬跟開箱的先後就錯了。
        _ORB["hit"] = orb_break_of(feed["trades"], box["hi"], box["lo"],
                                   by_ms=min(ORB_BREAK_BY_MS, now_ms))
    if _ORB["hit"] is None:
        if now_ms >= ORB_BREAK_BY_MS:
            # ⭐ 「09:30 前沒有突破」——今天這個候選不可用。
            #    ⚠️ 沒有突破 ⇒ 沒有進場價 ⇒ 箱子寬度%的分母用**箱子最後一筆成交價**
            #       （跟回測 `sim_lanes.orb_calc` 同一個實作決定，兩者差 < 1%）。
            _orb_hist_step(d, box, box["last"])
            _orb_end(d, "orb_no_break", base,
                     "開箱：%s 前沒有突破箱子（%s ~ %s）—— 這個候選今天不可用"
                     % (ORB_BREAK_BY[:5], _fmt_px(box["lo"]), _fmt_px(box["hi"])))
            return True
        # 還在等 ⇒ 畫面那一列寫「箱子已經畫好，等突破」（⛔ 不落地，這不是定論）
        _ORB["msg"] = ("開箱：箱子已畫好（上緣 %s／下緣 %s），等突破"
                       % (_fmt_px(box["hi"]), _fmt_px(box["lo"])))
        return False

    hit = _ORB["hit"]
    dv = hit["d"]
    bid, ask = _quote_at(feed["quotes"], hit["t_ms"], prev_q)
    fill = orb_fill(dv, hit["p"], bid, ask)
    at = _hms(hit["t_ms"])
    base.update({"at": at, "break_px": hit["p"], "px": fill, "d": dv,
                 "bid": bid, "ask": ask})
    # ⭐⭐ **箱寬濾網在突破那一刻才判，分母用進場價**（PM 2026-09-16 裁示 3，
    #    ＝回測 `orb_calc` 現在的做法）。⛔ 不准改成「09:05 用箱子最後一筆當分母」——那是第二把尺。
    bp = orb_box_pct(box["w"], fill)
    base["box_pct"] = None if bp is None else round(bp, 4)
    base["box_med_pct"] = round(thr["med"], 4)
    _orb_hist_step(d, box, fill)       # ⛔ 不管送不送，今天那一列都要寫進歷史
    if bp is None:
        _orb_end(d, "orb_no_box", base, "開箱：算不出箱子寬度% —— 這個候選今天不可用")
        return True
    if bp < thr["med"]:
        _orb_end(d, "orb_narrow", base,
                 "開箱：箱子太窄（%.3f%%，過去 %d 天中位數 %.3f%%）—— 這個候選今天不可用"
                 % (bp, thr["n"] if thr["n"] < ORB_RULE["hist_n"] else ORB_RULE["hist_n"],
                    thr["med"]))
        return True
    if dv < 0:
        _orb_end(d, "orb_short", base,
                 "開箱：%s 跌破箱子下緣 %s（做空）—— 多方聯軍只做多，這個候選今天不用"
                 % (at, _fmt_px(box["lo"])))
        return True
    sl = orb_sl_points(dv, fill, box["hi"], box["lo"])
    if sl is None:
        _orb_end(d, "orb_bad_sl", base)
        return True
    a = arm()                          # ⛔ 突破那一刻**重新讀開關**（09:03:30 之後他可能關掉了）
    base["arm_raw"], base["method"] = a["raw"], a["method"]
    if not a["on"]:
        _orb_end(d, a["why"], base, a["msg"])
        return True
    base["cand"] = "orb"
    # ⛔⛔ `dir` 一定要落地：收盤平倉與「重啟撿回那一口」認人都靠它
    #    （`_auto_entry` 只認 `dir` 是 long／short 的 result）。
    base["dir"] = "long"
    base["sl_points"] = sl
    base["tp_points"] = None           # ⛔ 開箱不設停利（落地也要寫出來）
    base["no_tp"] = True
    # ⚠️ 開箱沒有「主迴圈跨過那一刻」的快照 ⇒ 自己組一份：
    #    `quote_age_ms` 用「突破那一筆到現在」，那正是 can_enter 要問的「報價新不新鮮」。
    age = max(0, int(now_ms - hit["t_ms"]))
    snap = {"px": fill, "quote_age_ms": age, "at": at,
            "bid": _ORB["bid"], "ask": _ORB["ask"]}
    q = _quote_why(snap)
    if q:
        _orb_end(d, q, base,
                 "開箱：突破那一筆（%s）距離現在已經 %.1f 秒，不能拿舊價下單 —— 今天不做"
                 % (at, age / 1000.0))
        return True
    _ORB["done"] = True
    _send(d, base, a, "long", fill, sl, 0, time.time(), snap,
          "%s 突破箱子上緣 %s（寬 %.3f%% ≥ 中位數 %.3f%%），停損＝箱子下緣 %s"
          % (at, _fmt_px(box["hi"]), bp, thr["med"], _fmt_px(box["lo"])),
          tp_points=None)
    return True


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
    """
    某一天合併後的樣子：({候選 ⇒ 那一列}, 收盤平倉那一列)。⛔ 看檔案不看記憶體。

    ⛔⛔ **合併是「每個候選各自」後寫的蓋前面的**（PM 2026-09-16 裁示 4）——
       ⛔ 不可以再讓後寫的那一列整個蓋掉前面的：一天最多三個候選各一列，
       舊的合併會讓開箱那一列把快攻那一列吃掉（畫面上就只剩最後寫的那一個原因）。
    ⚠️ 舊帳本沒有 `cand` 欄位 ⇒ `_cand_of()` 一律回 `day` ⇒ 舊資料照舊併成同一格，
       **合併後的內容跟改版前一個欄位都不差**。
    ⚠️ `wait`（舊規則「等 09:15 反轉」）也算送單那一側：它不是部位
       （`_auto_entry` 只認 result ok）。
    """
    cands, eod = {}, None
    for o in _rows_of(d):
        rec = o.get("rec")
        if rec in ("fire", "result", "skip", "wait"):
            k = _cand_of(o)
            cur = dict(cands.get(k) or {})
            cur.update({kk: vv for kk, vv in o.items() if kk != "rec"})
            cur["rec"], cur["cand"] = rec, k
            cands[k] = cur
        elif rec == "eod":
            eod = dict(eod or {})
            eod.update({kk: vv for kk, vv in o.items() if kk != "rec"})
    return cands, eod


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
    ⚠️ 一天最多三個候選各一列，但**最多只有一列會開出部位**（送出去之後其餘候選
       一律落地 `union_done`）。所以這裡是「在三列裡面找那一列」，⛔ 不是看最後一列。
    """
    cands, _e = _day_rows(d)
    rows = list(cands.values())
    if not rows:
        return None, "eod_no_entry"
    done = [r for r in rows if r.get("rec") == "result" and r.get("ok")
            and r.get("dir") in ("long", "short")]
    if len(done) > 1:
        # ⛔⛔ 一天只准一口。真的出現兩列＝防重送的閘門破了 ⇒ **不敢動手**，大聲講。
        return None, "eod_unknown"
    if done:
        f = done[0]
    elif any(r.get("rec") == "fire" and r.get("stage") == "sending" for r in rows):
        return None, "eod_unsure"
    else:
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


def _eod(day, lag_ms, put_at, at=None):
    """
    ⚠️ **跑在自己的 daemon 執行緒上**（⛔ 不是主迴圈）：這裡會跟券商對帳、
       會送平倉單並等成交（`broker.close()` 最壞十幾秒）、會寫檔。

    ⛔⛔ **只平自己開的那一口。** 三道守衛全過才動手，任何一道不過就落地一列
       「為什麼不平」然後收工。
    """
    d = day
    # ⭐ 2026-09-16：**結算日 13:30 收盤** ⇒ 平倉時刻由呼叫端（live_panel）決定並傳進來。
    #    ⛔ 這個模組不自己判結算日（要讀行事曆，而呼叫這條路的是 4Hz 主迴圈）。
    at = at or _CFG["eod_at"]
    if _eod_settled(d):
        return None                      # 一天一次（看門狗在 13:43~13:45 重啟會重觸發）
    ent, why = _auto_entry(d)
    if ent is None:
        return _eod_row(d, why, {"at_lag_ms": lag_ms, "eod_at": at})
    base = {"dir": ent.get("dir"), "entry": _num(ent.get("entry")),
            "entry_time": ent.get("entry_time"), "method": ent.get("method"),
            "at_lag_ms": lag_ms, "eod_at": at}

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


def on_eod(day, lag_ms, at=None):
    """
    ⚠️⚠️ **這個函式跑在 4Hz 主迴圈上，而那條迴圈就是他的停損。**
    ⛔ 裡面只准有 `put_nowait` —— 一行 I/O、一次網路、一個鎖都不准有，
       **而且永遠不可以往外丟例外**（跟 `on_signal` 同一套規矩）。

    ⭐ `at`（2026-09-16）＝**今天實際用的收盤平倉時刻**（結算日是 13:28:30，
       其他日子 13:43:30）。⛔ 一定要由呼叫端傳進來：這個模組**不判斷結算日**
       （那要讀行事曆＝磁碟 I/O，而這裡是主迴圈）。沒傳 ⇒ 用 configure 接的那個。
    """
    try:
        _EQ.put_nowait((day, lag_ms, time.time(), at))
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
        out = {"sl_points": slp, "tp_points": tpp, "sl_src": "autofire"}
        # ⭐⭐ 2026-09-16「開箱」那一口**沒有停利**（帳本那一列 `no_tp`）——
        #    撿回來的時候一定要把這件事也補回去，⛔ 不然 `pos_tp_points()` 會掉回 130
        #    ⇒ 畫面畫出一條根本不存在的停利線（跟 sl_points 掉回 130 是同一類的坑）。
        #    ⚠️ 只有帳本那一列真的是開箱那一口才補 ⇒ ⛔ 不會動到手動真單的口徑
        #       （手動真單的帳本裡沒有 `no_tp`，也沒有 sl_points ⇒ 走不到這一段）。
        if ent.get("no_tp") or (tpp is None and ent.get("cand") == "orb"):
            out["no_tp"] = True
            out["tp_points"] = None
        return out
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

def configure(signal_at, signal_sec, late_ms, gap_s, sig_fn, dirs_fn, eod_at, pctl,
              rev_at=None, rev_sec=None):
    """
    面板啟動時叫一次，把**常數與訊號算式的正本**接過來（正本在 live_panel.py）。

    ⛔ 這個檔不自己寫一份 09:03:30／訊號算式 —— 那會變成兩把尺，
       訊號時刻改了一邊、另一邊不會跟著改，而畫面上完全看不出來。
    ⚠️ 停利停損點數（2026-09-15 起 ±0.5%）的正本是這個檔的 FAST_RULE，⛔ 不吃 TP_POINTS。
    ⭐ `pctl`（2026-09-15）：「開盤快才做」門檻的百分位，正本 `live_panel.FAST_PCTL`（80）。
       看不懂（bool／非數字／不在 0~100）⇒ 存 None ⇒ wired=False ⇒ 不送（⛔ 不猜）。
    ⭐ `rev_at`／`rev_sec`（2026-09-15 晚上）：回馬槍那一刻，正本 `live_panel.REV_AT`／`REV_SEC`。
       ⚠️ 預設 None 是刻意的：**沒傳 ⇒ wired=False ⇒ 一張都不送**（⛔ 不猜一個 09:15）。
       rev_sec 要是整數秒、而且晚於 signal_sec（⛔ 反過來就是「先等反轉、再判快不快」）。
    沒有接起來（`wired=False`）時**一律不送**，理由 `not_wired`。
    """
    ok_pctl = (not isinstance(pctl, bool) and isinstance(pctl, (int, float))
               and 0 < pctl <= 100)
    ok_rev = (isinstance(rev_at, str) and bool(rev_at)
              and not isinstance(rev_sec, bool) and isinstance(rev_sec, int)
              and isinstance(signal_sec, int) and rev_sec > signal_sec)
    _CFG.update({"signal_at": signal_at, "signal_sec": signal_sec,
                 "late_ms": late_ms, "gap_s": float(gap_s),
                 "sig_fn": sig_fn, "dirs_fn": dirs_fn, "eod_at": eod_at,
                 "pctl": pctl if ok_pctl else None,
                 "rev_at": rev_at if ok_rev else None,
                 "rev_sec": rev_sec if ok_rev else None})
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


def on_reversal(snap, day, lag_ms):
    """
    ⭐ 回馬槍那一刻（09:15）的掛勾。⚠️⚠️ **跑在 4Hz 主迴圈上，而那條迴圈就是他的停損。**
    ⛔ 規矩跟 `on_signal` 一模一樣：只准 `put_nowait`，一行 I/O、一次網路、一個鎖都不准有，
       **永遠不往外丟例外**。
    `snap is None` ＝ 主迴圈判定「跨過 09:15 但已經晚太多」⇒ 工作執行緒記 late（⛔ 不補單）。
    ⚠️ 丟進**同一條** `_Q`（第五格 "reversal"）：09:03:30 那一件一定先處理完，
       09:15 讀帳本時看得到 wait 那一列。
    """
    try:
        _Q.put_nowait((snap, day, lag_ms, time.time(), "reversal"))
    except Exception as e:
        _ST["err"] = WHY["queue_full"] + "（回馬槍）" + ("（%s）" % str(e)[:80] if str(e) else "")
        _ST["err_n"] += 1
        if _ST["err_n"] <= 3:
            print("⚠️ [自動下單] %s —— 回馬槍那一件沒有排進去（停損不受影響）"
                  % WHY["queue_full"], flush=True)


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
        # ⭐⭐ 開箱（ORB）：09:05~09:30 之間每 POLL_S 秒看一次 tick_logs 有沒有突破。
        #    ⛔ 一定在**這條執行緒**上（要讀檔、可能送單）；⛔ 不准搬進 4Hz 主迴圈，
        #    ⛔ 也不准動 on_tick 熱路徑。出錯不可以把這條執行緒帶掉（下一輪還要送單）。
        try:
            _orb_step()
        except Exception as e:
            _ST["err"] = "orb: " + str(e)[:150]
            _ST["err_n"] += 1
            if _ST["err_n"] <= 3:
                print("⚠️ [自動下單] 開箱那一段出錯（停損不受影響）：%s" % str(e)[:200],
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
    每一天一列，新到舊。同一天**每個候選各自**以後寫的為準（sending → result）。

    ⛔ 每一列都要有去處：`fire + result + skip + eod + wait + bad ＝ 檔案總列數`
       （這條等式是「有沒有東西被安靜吃掉」唯一的機器判準）。
       ⚠️ 2026-09-15 晚上加 `wait`（舊規則「等 09:15 反轉」那一列）—— 沒加進來會被算成 bad。
       ⚠️ 2026-09-16 一天變成**最多三個候選各一列** ⇒ 這條等式的右邊變大了，但左邊
          每一種的定義沒變 ⇒ **等式本身照樣成立**（三列 skip 就是 skip 加三）。

    ⭐⭐ 2026-09-16（PM 裁示 4）：合併改成**每個候選一格**（`row["cands"][cand]`），
       ⛔ 不可以再讓後寫的整個蓋掉前面的 —— 那會讓開箱那一列把快攻那一列吃掉。
       頂層仍然攤平一份「今天的結論」給既有畫面用，挑法（⛔ 固定，不是「最後一列」）：
         ① 有 `fire`／`result` 的那一列（＝真的送出去的那個候選）
         ② 沒有的話 ⇒ 整天那一列（`day`：開關關著、面板沒接起來）
         ③ 再沒有 ⇒ 檔案裡**最後寫的**那個候選（畫面至少有一句話）
       ⚠️ 舊帳本每一列都沒有 `cand` ⇒ 全部落在 `day` 那一格 ⇒ **舊紀錄的頂層一個欄位都不變**。

    ⚠️ **`eod`（收盤平倉）那一列不可以用同一套合併規則往上蓋。**
       送單那件事與收盤平倉那件事是同一天的**兩件**事：直接 `update` 的話，
       `rec` 會被蓋成 eod、`why`／`why_msg` 會從「今天送了什麼」變成「收盤平了沒」——
       畫面上那一天就從「已送出委託單」變成別的東西。所以收在 `row["eod"]` 底下。
    """
    led = {"fire": 0, "result": 0, "skip": 0, "eod": 0, "wait": 0, "bad": 0, "total": 0}
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
                        or rec not in ("fire", "result", "skip", "eod", "wait"):
                    led["bad"] += 1
                    continue
                led[rec] += 1
                cur = days.setdefault(d, {"date": d, "cands": {}, "_order": []})
                if rec == "eod":
                    # ⛔ 收在自己的抽屜裡，不准蓋掉「今天送了什麼」那幾個欄位
                    e = dict(cur.get("eod") or {})
                    e.update({k: v for k, v in o.items() if k != "rec"})
                    cur["eod"] = e
                    continue
                k = _cand_of(o)
                c = dict(cur["cands"].get(k) or {})
                c.update({kk: vv for kk, vv in o.items() if kk != "rec"})
                c["rec"], c["cand"] = rec, k
                cur["cands"][k] = c
                if k in cur["_order"]:
                    cur["_order"].remove(k)
                cur["_order"].append(k)
    out = []
    for d in sorted(days, reverse=True)[:limit_days]:
        r = dict(days[d])
        order = r.pop("_order", [])
        cands = r.get("cands") or {}
        # stage 停在 sending ＝ 決定送單之後程式中斷了。⛔ 不可以顯示成「沒送」
        for k, c in list(cands.items()):
            if c.get("rec") == "fire" and c.get("stage") == "sending":
                c = dict(c)
                c["why"] = "crashed"
                c["why_msg"] = WHY["crashed"] + " —— 請自己到大戶投確認部位"
                cands[k] = c
        # ── 頂層攤平「今天的結論」（⛔ 挑法固定，見 docstring）
        pick = next((cands[k] for k in order
                     if cands[k].get("rec") in ("fire", "result")), None)
        if pick is None:
            pick = cands.get(CAND_DAY)
        if pick is None and order:
            pick = cands[order[-1]]
        if pick is not None:
            r.update({kk: vv for kk, vv in pick.items() if kk != "cands"})
        else:
            # 只有收盤平倉那一列、沒有送單那一列（理論上碰不到，但**空的 rec
            # 會讓畫面掉進「沒有送單」再讀不到 why_msg** ⇒ 一格空白）。
            e = r.get("eod") or {}
            r.update({"rec": "skip", "why": e.get("why"), "why_msg": e.get("why_msg")})
        # ⭐ 三個候選照固定順序排好給畫面（⛔ 不按寫入順序：那會讓同一天每次看起來不一樣）
        r["cand_rows"] = [cands[k] for k in CANDS if k in cands]
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


_ORB_HIST_CACHE = {"key": None, "val": None}


def orb_hist_ready(today):
    """
    ⭐⭐⭐ **「箱子寬度歷史夠不夠用」—— 給畫面用的，⛔ 一天的任何時刻都答得出來。**

    ⛔⛔ 2026-09-17 lab-qa 退件 R1：`orb_hist.jsonl` **這台機器上根本還沒有**
       ⇒ `orb_threshold()` 算不出中位數 ⇒ **開箱這個候選開箱即不可用**，
       而畫面上（09:05 以前）只寫「等 09:05 畫完箱子」⇒ 他以為有三個候選，
       實際上只跑得出兩個。⛔ **不可以安靜地少一個候選。**
    ⇒ 這一支在**任何時刻**都講得出「歷史有幾天、夠不夠、不夠的話畫面要寫什麼」。

    回 {"ok", "why", "msg", "n", "med_pct", "missing"}；`ok=False` ⇒ 今天開箱不可用。
    ⚠️ 唯讀、HTTP 執行緒上（每 5 秒一次）⇒ 用 `(mtime, size)` 快取，穩態零次讀檔。
    """
    try:
        st = ORB_HIST.stat()
        key = (st.st_mtime_ns, st.st_size, today)
    except OSError:
        key = ("missing", today)
    if _ORB_HIST_CACHE["key"] == key:
        return dict(_ORB_HIST_CACHE["val"])
    missing = not ORB_HIST.exists()
    try:
        rows, _bad, _dup = orb_hist_read()
        thr = orb_threshold(rows, today)
        out = {"ok": thr["why"] is None, "why": thr["why"], "msg": thr["msg"],
               "n": thr["n"], "med_pct": None if thr["med"] is None else round(thr["med"], 4),
               "missing": missing}
        if missing:
            # ⛔ 「檔案還沒建」跟「天數不夠」是兩件事，話要講得不一樣 ——
            #    前者他要去跑 `build_orb_hist.py`，後者只要再等幾天。
            out["why"] = out["why"] or "orb_no_hist"
            out["msg"] = ("開箱：**還沒有箱子寬度歷史**（%s 還沒建）—— 這個候選不可用。"
                          "要用請先跑 build_orb_hist.py 建種子。" % ORB_HIST.name)
    except Exception as e:
        out = {"ok": False, "why": "orb_no_hist", "n": 0, "med_pct": None,
               "missing": missing,
               "msg": "開箱：箱子寬度歷史讀不出來（%s）—— 這個候選今天不可用" % str(e)[:80]}
    _ORB_HIST_CACHE.update(key=key, val=dict(out))
    return out


def orb_today(today, day_row=None):
    """
    畫面上「開箱今天到哪了」。⚠️ **唯讀**：只讀 `_ORB`（送單執行緒維護的那份）與帳本那一列，
    ⛔ 這裡不判斷、不讀 tick_logs、不送單（那些都在 `_orb_step()`）。

    `stage`：`before`（09:05 以前，還沒有箱子）／`wait`（箱子畫好了，等突破）／
             `done`（今天已經有定論，`why`／`msg` 說得出是哪一種）
    ⭐ `hist_ok`／`hist_msg`：**歷史夠不夠**（⛔ 不夠的話畫面一定要講，見 `orb_hist_ready`）。
    """
    row = None
    for c in ((day_row or {}).get("cand_rows") or []):
        if c.get("cand") == "orb":
            row = c
    mine = _ORB if _ORB.get("date") == today else None
    box = (mine or {}).get("box")
    out = {"break_by": ORB_BREAK_BY, "box_at": "%s~%s" % (ORB_BOX_FROM_AT[:5],
                                                          ORB_BOX_TO_AT[:5]),
           "hist_n": ORB_RULE["hist_n"],
           "hi": None, "lo": None, "w": None, "med_pct": None, "n": None,
           "stage": "before", "why": None, "msg": None,
           "bad": (mine or {}).get("bad", 0), "drops": (mine or {}).get("drops", 0),
           "err": (mine or {}).get("err")}
    # ⭐⭐ R1：歷史夠不夠**在任何時刻**都要答得出來（⛔ 不是等 09:05 畫完箱子才發現）
    hr = orb_hist_ready(today)
    out["hist_ok"] = hr["ok"]
    out["hist_why"] = hr["why"]
    out["hist_msg"] = None if hr["ok"] else hr["msg"]
    out["hist_have"] = hr["n"]
    out["hist_missing"] = hr["missing"]
    # ⭐ 2026-09-21：箱寬門檻（過去 N 天的中位數 %）**在 09:05 以前也要答得出來** ——
    #    「現在離箱寬門檻還差幾點」那一行要用（`live_panel.fire_gap()`）。
    #    ⛔ 跟下面那個 `med_pct` 是兩件事：那一個是**今天判定時用的那一個**（箱子畫好才有、
    #       有定論之後以帳本為準），這一個是**現在這份歷史算出來的**。
    #       ⛔ 不要把兩個併成一個欄位（判定用哪一個，事後一定要看得出來）。
    out["hist_med_pct"] = hr["med_pct"]
    if box:
        out.update({"hi": box["hi"], "lo": box["lo"], "w": round(box["w"], 1)})
        thr = (mine or {}).get("thr") or {}
        out.update({"med_pct": None if thr.get("med") is None else round(thr["med"], 4),
                    "n": thr.get("n")})
        out["stage"] = "wait"
        out["msg"] = (mine or {}).get("msg")
    if row is not None:
        # ⛔ **帳本那一列是定論**（重啟之後記憶體是空的，但那一天早就判完了）
        out.update({"stage": "done", "why": row.get("why"), "msg": row.get("why_msg"),
                    "hi": row.get("box_hi", out["hi"]), "lo": row.get("box_lo", out["lo"]),
                    "w": row.get("box_w", out["w"]),
                    "box_pct": row.get("box_pct"),
                    "med_pct": row.get("box_med_pct", out["med_pct"]),
                    "at": row.get("at")})
    elif (mine or {}).get("done"):
        out.update({"stage": "done", "msg": (mine or {}).get("msg")})
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
        # ⭐ 現在跑的是不是多方聯軍（前端靠這個決定要不要畫「三個候選」那一區）。
        #    ⛔ 前端不准自己比 method === 'U'（那是把代號寫死進畫面）。
        "union": _union_on(a),
        "default_method": DEFAULT_METHOD,
        "method_names": dict(METHOD_NAME),
        # ⭐⭐ 帳本那一列的規則代號 → 名字（PM 2026-09-17 裁示 M3 的「長遠那半」）。
        #    ⛔ 這張表只准新增不准改值：畫面照帳本那一列的 `rule` 取名字，
        #       名字就再也不會因為「現在跑哪條規則」而改動。
        "rule_names": dict(RULE_NAME),
        "signal_at": _CFG["signal_at"],
        # ⭐ 回馬槍那一刻（前端 ⛔ 不准寫死 09:15；正本 live_panel.REV_AT）
        "rev_at": _CFG["rev_at"],
        "leg_names": dict(LEG_NAME),
        # ⭐ 多方聯軍的三個候選（前端 ⛔ 不准寫死名字與時刻）
        "cands": [{"k": k, "name": CAND_NAME[k], "at": CAND_AT[k]} for k in CANDS],
        "cand_names": dict(CAND_NAME),
        # ⭐ 規則數字（前端 ⛔ 不准寫死 40／80／20／0.5%）
        "rule": {"window": FAST_RULE["window"], "pctl": fast_pctl(),
                 "min_n": FAST_RULE["min_n"],
                 "tpsl_pct": round(FAST_RULE["tpsl_frac"] * 100, 6),
                 "rev_at": _CFG["rev_at"],
                 # ⭐ 開箱的規則數字（前端 ⛔ 不准寫死 09:00~09:05／09:30／20 天）
                 "box_at": "%s~%s" % (ORB_BOX_FROM_AT[:5], ORB_BOX_TO_AT[:5]),
                 "break_by": ORB_BREAK_BY, "orb_hist_n": ORB_RULE["hist_n"]},
        "fast": fast_today(today, next((r for r in rows if r.get("date") == today), None)),
        "orb": orb_today(today, next((r for r in rows if r.get("date") == today), None)),
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
