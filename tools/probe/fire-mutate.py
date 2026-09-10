# -*- coding: utf-8 -*-
"""
【自動下單】的突變測試 —— 把 `auto_fire.py` 的每一道守衛拿掉，看有沒有東西紅。

⛔⛔ **絕對不會把壞版本寫進 `tools/shioaji/`。** 看門狗是活的（永豐 SDK 斷線會把
     整個行程帶掉、`start-panel.bat` 自動重開），磁碟上只要有一版壞掉的 `auto_fire.py`，
     下一次重啟就會被載進他**正在跑的面板**。所以突變版一律寫進**暫存資料夾**，
     再用環境變數 `AF_SRC_DIR` 讓 `test_auto_fire.py` 從那裡載入。

⚠️ 判定「打紅了沒」⛔ **不可以只數 FAIL 行**：突變讓測試當場掛掉時一行 FAIL 都沒有，
   會被記成「打不紅」（CLAUDE.md 記過這個假綠燈）。這裡兩件都看：
   ① 離開碼非 0；② 有印出總結行（`test_auto_fire.py` 自己裝了 excepthook，
   掛掉時也會印出一項具名的 FAIL ＋ 總結）。

跑法：
    PYTHONIOENCODING=utf-8 PYTHONUTF8=1 .venv\\Scripts\\python.exe tools\\probe\\fire-mutate.py
"""
import atexit
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
SHIO = HERE.parent / "shioaji"
SRC = (SHIO / "auto_fire.py").read_text(encoding="utf-8")
LPSRC = (SHIO / "live_panel.py").read_text(encoding="utf-8")
TEST = SHIO / "test_auto_fire.py"
ROUTE_TEST = SHIO / "test_fire_routes.py"
PY = sys.executable

# ⛔⛔⛔ **真的**那個開關檔。這一支從頭到尾只讀它、只印它，⛔ 一行程式都不准寫它、
#    改名它、刪除它 —— 那是他自己按下去的東西，動了就等於在他不知情的情況下
#    關掉（或打開）他的自動下單。
REAL_ARM = SHIO / "AUTO_ORDERS_ON"


def _real_arm_line(tag):
    """收尾（與啟動）都印一行，跟 `fire_harness.py`／`test_fire_routes.py` 對齊。"""
    return "%s真的 %s：%s" % (
        tag, REAL_ARM.name, "存在" if REAL_ARM.exists() else "不存在")


# ⛔⛔ **啟動時就拒絕跑**（2026-09-10 P0）。理由不是「測試會紅」，是這支曾經被中斷之後
#    在**真的** tools/shioaji/ 留下 `AUTO_ORDERS_ON` ＋ `autofire/arm-*.jsonl`，
#    而 `REAL_ORDERS_ON` 開著 ⇒ 那個殘骸留到隔天早上就是**一口沒有任何人授權的真單**。
#    ⚠️ 兩者都在 `.gitignore` 裡 ⇒ **`git status` 看不見它**。
#    ⛔ 正確的處置是「**不跑**」，⛔ 不是清掉它、不是備份它、不是繞過它 ——
#       他自己按開的那個檔，這支沒有任何權力去動。
#    ⇒ 他真的在用自動下單的日子，這支測試就跑不了。**那是設計，不是壞掉。**
if REAL_ARM.exists():
    print("⛔ " + _real_arm_line("") + "（他自己開著的）")
    print("⛔ 這支拒絕啟動：它會把每一支子測試都變成假紅燈，")
    print("   而那幾支（fire_harness.py／test_fire_routes.py）本來就會拒絕啟動。")
    print("⛔ ⛔ 不要為了跑測試把那個檔搬走／改名／刪掉 ——")
    print("   那等於在他不知情的情況下關掉他的自動下單。")
    print("   要跑這支，請等他自己在面板上按「關閉自動下單」之後。")
    sys.exit(2)

# （名字, 原文, 換成什麼）—— 每一個都是「把一道守衛拿掉」
MUT = [
    ("① 開關內容看不懂時當成 A（⛔ 猜方向）",
     '    txt = raw.strip().upper()', '    txt = "A"'),
    ("② 開關檔不存在也照做（＝出貨就是開著的）",
     '        if not ARM_FLAG.exists():', '        if False:'),
    ("③ 一天一次的守衛拿掉（看門狗重啟就送第二張）",
     '    if _has(d):\n        return None', '    if False:\n        return None'),
    ("④ 報價太舊照樣下單（斷線時自動下單沒有跟著停）",
     '    if age is None or age > _CFG["gap_s"] * 1000:', '    if False:'),
    ("⑤ 只有中價也照樣下單（拿中價當進場價）",
     '    if snap.get("is_mid"):', '    if False:'),
    ("⑥ 沒有成交價也照樣下單",
     '    if _num(snap.get("px")) is None:', '    if False:'),
    ("⑦ 算不出訊號也照樣下單（方向用猜的）",
     '    if dv is None:\n        return _skip(d, "no_signal", base)',
     '    if dv is None:\n        dv = 1'),
    ("⑧ 方向永遠回報做多（2026-09-01 那個會賠錢的 bug 的形狀）",
     '    direction = "long" if dv > 0 else "short"', '    direction = "long"'),
    ("⑨ 開關講 A／B 但送的是別的做法",
     '    dv = dirs.get(a["method"])', '    dv = dirs.get("D")'),
    ("⑩ 不走 broker.can_enter（券商那一整排防呆全失效）",
     '    ok, why = broker.can_enter(px, fresh)', '    ok, why = True, None'),
    ("⑪ 「沒送」不落地（畫面上永遠看不到為什麼沒送）",
     '    return _append(row)\n\n\n# ---------------------------------------------------------------- 決定與送出',
     '    return row\n\n\n# ---------------------------------------------------------------- 決定與送出'),
    ("⑫ 落地改成覆寫（送到一半當掉就會重送）",
     '    with _month_path(row["date"]).open("a", encoding="utf-8") as f:',
     '    with _month_path(row["date"]).open("w", encoding="utf-8") as f:'),
    ("⑬ 先送單再落地（當掉就重送）",
     '    _append(fire)\n    _ST["last"] = fire', '    _ST["last"] = fire'),
    ("⑭ 排隊排太久也照送（那張單的價不是 09:03:30 的價）",
     '    if late > LATE_MS:', '    if False:'),
    ("⑮ 沒接線也照送（常數與算式都是 None）",
     '    if not _ST["wired"]:\n        return _skip(d, "not_wired")',
     '    if False:\n        return _skip(d, "not_wired")'),
    ("⑯ 晚到那一天不記原因（安靜地少）",
     '    if snap is None:\n        # 主迴圈說', '    if False:\n        # 主迴圈說'),
    ("⑰ on_signal 改成阻塞式 put（主迴圈＝他的停損會被卡住）",
     '        _Q.put_nowait((snap, day, lag_ms, time.time()))',
     '        _Q.put((snap, day, lag_ms, time.time()))'),
    ("⑱ 佇列滿了安靜地丟（畫面上看不出那天沒送）",
     '        _ST["err"] = WHY["queue_full"]', '        _junk = WHY["queue_full"]'),
    ("⑲ 兩個不同的原因寫同一句",
     '    "no_quote": "09:03:30 收不到成交價",',
     '    "no_quote": "09:03:30 的報價太舊（斷線中），不能用舊價下單",'),
    ("⑳ result 那一列不蓋掉 stage（有結果的一天被寫成「不知道下場」）",
     '    row.update({"rec": "result", "date": d, "stage": "done",',
     '    row.update({"rec": "result", "date": d,'),
    # ── 收盤自動平倉（2026-09-09 加）。⛔ 每一個都是「把他的單平掉」或「該平沒平」
    ("㉑ 收盤平倉不比方向（做多做空對不上照樣平 ⇒ **平掉他的單**）",
     '    if pos.get("dir") != ent.get("dir"):', '    if False:'),
    ("㉒ 收盤平倉不比進場價（隨便一口部位都當成自己的 ⇒ **平掉他的單**）",
     '    if abs(pe - ee) > EOD_PX_TOL:', '    if False:'),
    ("㉓ 收盤平倉不比進場時間（他自己開的那口會被誤認）",
     '    if isinstance(pt, str) and pt and isinstance(et, str) and et and pt != et:',
     '    if False:'),
    ("㉔ 不查「那一口是不是早就平掉了」（他中途自己平掉、又自己開一口 ⇒ **平掉他的單**）",
     '    state, how = _already_closed(ent)', '    state, how = "open", None'),
    ("㉕ 不查券商的已實現損益（面板當掉那段他在別處平的看不到）",
     '        real = broker.realized_today(fresh=True)', '        real = []'),
    # ── M1（2026-09-09 lab-qa 退件）：**「問不到」不可以當成「沒有平過」**
    ("Ⓜ1a 問不到已實現損益 ⇒ 當成「還沒平」（＝ X4／X5 那張平掉他部位的單）",
     '    if real is None:\n        # ⛔⛔ 這裡就是 M1 的落點。',
     '    if real is None:\n        real = []\n    if False:\n        # ⛔⛔ 這裡就是 M1 的落點。'),
    ("Ⓜ1b 問不到已實現損益 ⇒ 當成「已經平掉了」（Y 型假話：部位其實還開著）",
     '        return "unknown", "問不到券商的已實現損益（對不了帳）"',
     '        return "closed", "問不到券商的已實現損益（對不了帳）"'),
    ("Ⓜ1c 讀不到成績單 ⇒ 當成「還沒平」（第①道靜音失敗）",
     '        return "unknown", "讀不到面板的成績單（%s）" % str(ex)[:60]',
     '        return "open", "讀不到面板的成績單（%s）" % str(ex)[:60]'),
    ("Ⓜ1d 帳本那列沒有進場價 ⇒ 當成「還沒平」（對不了帳卻照樣動手）",
     '        return "unknown", "帳本那一列沒有進場價，對不了帳"',
     '        return "open", "帳本那一列沒有進場價，對不了帳"'),
    ("Ⓜ1e 已實現損益回頭吃 TTL 快取（X5：59 秒前的空快照當證據）",
     '        real = broker.realized_today(fresh=True)',
     '        real = broker.realized_today()'),
    ("Ⓜ1f 「查不到」照樣往下走到第③道（而第③道擋不住 X4 那個形狀）",
     '    if state != "open":', '    if state != "open" and False:'),
    # ── M2：**口數也要比**（他加碼之後券商上是 2 口）
    ("Ⓜ2a 不比口數（他在大戶投加碼 ⇒ 2 口一起被平掉）",
     '    if pq is None or pq != eq:', '    if False:'),
    ("Ⓜ2b 口數比對用「至少 1 口」（2 口照樣過關）",
     '    if pq is None or pq != eq:', '    if pq is None or pq < eq:'),
    ("Ⓜ2c 口數拿別的數字比（不是 broker.QTY ⇒ 兩把尺）",
     '    pq, eq = _num(pos.get("qty")), float(broker.QTY)',
     '    pq, eq = _num(pos.get("qty")), 2.0'),
    # ── M3：「查不到」要有自己的一句話，而且要示警
    ("Ⓜ3a eod_cant_tell 不示警（他抱過夜盤而畫面靜悄悄）",
     '             "eod_not_ours", "eod_queue_full", "eod_cant_tell")',
     '             "eod_not_ours", "eod_queue_full")'),
    ("Ⓜ3b eod_cant_tell 被當成有定論（看門狗重啟後不再重試）",
     '               "eod_done_elsewhere", "eod_unsure")',
     '               "eod_done_elsewhere", "eod_unsure", "eod_cant_tell")'),
    # ── B3：三個常數本身零守衛（lab-qa 突變 Q2／Q3／Q4 都打不紅）
    ("Ⓠ2 EOD_WINDOW_S 75→5（平不掉只試得了一輪就放棄）",
     'EOD_WINDOW_S = 75.0', 'EOD_WINDOW_S = 5.0'),
    ("Ⓠ2b EOD_WINDOW_S 撐到收盤之後（13:45 之後送不出去，還占著窗口）",
     'EOD_WINDOW_S = 75.0', 'EOD_WINDOW_S = 300.0'),
    ("Ⓠ3 EOD_SETTLED 多收 eod_failed（看門狗重啟不再重試，而部位還在）",
     '               "eod_done_elsewhere", "eod_unsure")',
     '               "eod_done_elsewhere", "eod_unsure", "eod_failed")'),
    ("Ⓠ4 EOD_ALARM 拿掉 eod_not_ours（「這口不是我的」變成一行小字）",
     '             "eod_not_ours", "eod_queue_full", "eod_cant_tell")',
     '             "eod_queue_full", "eod_cant_tell")'),
    ("㉖ 「今天沒有自動下單開的部位」也照平（＝拿他的部位當自己的）",
     '    if ent is None:\n        return _eod_row(d, why, {"at_lag_ms": lag_ms})',
     '    if ent is None:\n        ent, why = {"dir": "long", "entry": None}, None'),
    ("㉗ 「送到一半當掉」那天也敢動手（不知道那口是不是我們的）",
     '        return None, "eod_unsure"', '        return None, "eod_no_entry"'),
    ("㉘ 對帳失敗（unknown）當成沒有部位（該平的沒平／狀態被清掉）",
     '        if pos == "unknown":', '        if False:'),
    ("㉙ 收盤平倉不落地（畫面上永遠看不到平了沒）",
     '    row.update(extra or {})\n    _ST["last"] = row\n    head = ',
     '    row.update(extra or {})\n    return row\n    _ST["last"] = row\n    head = '),
    ("㉚ 平不掉時不講話（他以為平掉了，實際抱過夜盤）",
     '    return _eod_row(d, "eod_failed", dict(base, tries=tries, err=str(last_err or "")),',
     '    return _eod_row(d, "eod_closed", dict(base, tries=tries, err=str(last_err or "")),'),
    ("㉛ 收盤那一列用一般規則合併（把「今天送了什麼」整個蓋掉）",
     '                if rec == "eod":', '                if False:'),
    ("㉜ 帳本少算 eod 那一種（有東西被安靜吃掉看不出來）",
     '                led[rec] += 1', '                led[rec] += (rec != "eod")'),
    ("㉝ 一天平一次的守衛拿掉（看門狗重啟就再送一張平倉單）",
     '    if _eod_settled(d):\n        return None', '    if False:\n        return None'),
    # ── 開關：⛔ 只能關不能開（2026-09-09 加）
    ("㉞ 關閉那條路變成「會把開關建出來」（⛔ 開難關易整個反過來）",
     '        ARM_FLAG.replace(dest)',
     '        ARM_FLAG.replace(dest)\n        ARM_FLAG.write_text("A", encoding="utf-8")'),
    ("㉟ 關閉改成刪掉（他寫的內容不見了，也看不出幾點關的）",
     '        ARM_FLAG.replace(dest)', '        ARM_FLAG.unlink()'),
    ("㊱ flag_exists 直接抄 armed（內容看不懂時那顆鈕會消失 ⇒ 壞檔關不掉）",
     '        "flag_exists": flag_exists(),', '        "flag_exists": a["on"],'),
    # ── 開關檔的編碼：他第一次一定會踩的兩種（2026-09-09 加）
    ("㊲ 不處理 BOM／UTF-16（他用記事本或 PowerShell 建的檔會被說「看不懂」）",
     '        raw = _decode_flag(ARM_FLAG.read_bytes()[:ARM_MAX_BYTES])',
     '        raw = ARM_FLAG.read_bytes()[:ARM_MAX_BYTES].decode("utf-8", "replace")'),
    # ── state() 的 live 寫死（lab-qa 2026-09-09 抓到的「打不紅」之二）
    ("㊴ state() 的 live 寫死成 True（演練時畫面卻寫「會真的送出去」）",
     '        "live": broker.is_live(), "live_flag": broker.REAL_FLAG.name,',
     '        "live": True, "live_flag": broker.REAL_FLAG.name,'),
    ("㊵ state() 的 live 寫死成 False（真的會送單，畫面卻寫「只會演練」）",
     '        "live": broker.is_live(), "live_flag": broker.REAL_FLAG.name,',
     '        "live": False, "live_flag": broker.REAL_FLAG.name,'),
    ("㊳ UTF-16 那條退路拿掉（PowerShell 的 \"A\" > 檔）",
     '    if b[:2] in (b"\\xff\\xfe", b"\\xfe\\xff"):           # UTF-16 LE／BE（PowerShell 5.1）',
     '    if False:'),
]

# ── 第二組：**產品的路由**（⛔ 只有 test_fire_routes.py 打得到）
#    這一組是 2026-09-09 lab-qa 退件 M1 的負控組：舊守衛用
#    `"/api/fire/state" in LPSRC` 當證據，而那個字串在檔案裡出現兩次（第二次是前端的
#    fetch）⇒ 路由改壞照樣全綠。現在真的起服務打進去，改掉路由字串就要紅。
LP_MUT = [
    ("Ⓐ GET 的路由改名（端點整個不見，但原始碼裡那個字串還在）",
     '        if self.path.startswith("/api/fire/state"):',
     '        if self.path.startswith("/api/fire/statXX"):'),
    ("Ⓑ GET 的路由整個刪掉",
     '        if self.path.startswith("/api/fire/state"):\n',
     '        if False:\n'),
    ("Ⓒ 關閉那顆的路由改名（畫面上那顆鈕按下去 404）",
     '        if self.path == "/api/fire/off":', '        if self.path == "/api/fire/ofXX":'),
    ("Ⓓ 關閉那條改成「什麼都不做」（按了說成功，開關其實還開著）",
     '                ok, msg = auto_fire.disarm()',
     '                ok, msg = True, "已關閉"'),
    # ── Q12（2026-09-09 lab-qa 打不紅）：路由比對放寬
    #    那顆鈕是這一頁**唯一**會改變狀態的動作，比對放寬＝多開一批沒人審過的入口。
    ("Ⓠ12 關閉那條改用 startswith（/api/fire/offXX、?arm=A 全都會中）",
     '        if self.path == "/api/fire/off":',
     '        if self.path.startswith("/api/fire/off"):'),
    ("Ⓠ12b 關閉那條改成「路徑裡有就算」（in 比 startswith 更寬）",
     '        if self.path == "/api/fire/off":',
     '        if "/api/fire/off" in self.path:'),
    # ── B6（既有的洞，2026-09-09 補）：/api/replay 收空 body 也回 200 並寫一列全 null
    ("Ⓑ6a /api/replay 不驗日期（空 body 也照寫，replay_log/ 多一列全 null）",
     '            if not (isinstance(_d, str) and _REPLAY_DATE_RE.match(_d)):',
     '            if False:'),
    ("Ⓑ6b /api/replay 不驗 judged／dir／entry（半套資料混進勝率統計）",
     '            if not isinstance(body.get("judged"), bool):',
     '            if False:'),
    ("Ⓑ6c 日期只看有沒有值（`../../boom` 會被當成檔名）",
     '            if not (isinstance(_d, str) and _REPLAY_DATE_RE.match(_d)):',
     '            if not _d:'),
    # ── Ⓣ1（2026-09-09 lab-qa 建議 1）：`/api/state` **序列化失敗那條退路**的 token。
    #    CLAUDE.md 給那一行標了 ⛔⛔（「少了它，面板一出狀況他就同時失去畫面與按鈕」），
    #    但在這之前**刪掉它兩支測試全綠**：Ⓟ7b 打的是正常那條路（token 在
    #    `dict(STATE, token=…)` 裡），跟這條退路是兩行不同的程式。
    #    ⇒ 守它的是 `test_fire_routes.py` ③f（真的塞一個序列化不了的東西進 STATE）。
    ("Ⓣ1 /api/state 的序列化退路不帶 token（面板一出狀況他同時失去畫面與按鈕）",
     '                    safe["token"] = FIRE_TOKEN\n', ''),
]

# ── 第三組：**live_panel 的主迴圈與畫面**（只有 test_auto_fire.py 打得到）
#    2026-09-09 lab-qa 的 Q11／Q9 就是打在這裡而**整組打不紅**：
#    那時 `test_auto_fire.py` 只吃 AF_SRC_DIR ⇒ live_panel 永遠是好的那一份。
#    ⚠️ 老形狀：守衛蓋住「邏輯」那半，「常數／文案／接線」那半空白。
LP2_MUT = [
    ("Ⓠ11 拿掉 AUTO['eod'] = True（4Hz 主迴圈每圈丟一次平倉，一秒 4 張）",
     '        AUTO["eod"] = True\n        AUTO_EOD_HOOK(',
     '        AUTO_EOD_HOOK('),
    ("Ⓠ11b 收盤平倉的旗標跟 09:03:30 那件共用（送過單就永遠不平倉）",
     '    if not AUTO["eod"] and EOD_CLOSE_SEC <= secs < DAY_END_SEC:',
     '    if not AUTO["done"] and EOD_CLOSE_SEC <= secs < DAY_END_SEC:'),
    ("⏰a 時鐘防護：上界拿掉（NTP 往前跳過 13:43:30 ⇒ 早上就平掉他剛開的單）",
     '    if not AUTO["eod"] and EOD_CLOSE_SEC <= secs < DAY_END_SEC:',
     '    if not AUTO["eod"] and EOD_CLOSE_SEC <= secs:'),
    ("⏰b 時鐘防護：下界拿掉（任何時刻都可以觸發）",
     '    if not AUTO["eod"] and EOD_CLOSE_SEC <= secs < DAY_END_SEC:',
     '    if not AUTO["eod"] and secs < DAY_END_SEC:'),
    ("⏰c 觸發時刻寫死數字（EOD_CLOSE_AT 改了不會跟著改）",
     '    if not AUTO["eod"] and EOD_CLOSE_SEC <= secs < DAY_END_SEC:',
     '    if not AUTO["eod"] and 49410 <= secs < DAY_END_SEC:'),
    ("Ⓠ9 那顆「關閉」鈕改看 armed（開關檔壞掉時 armed=False ⇒ 他關不掉）",
     "setEl('aloff', D.flag_exists", "setEl('aloff', D.armed"),
    ("Ⓠ9b 那顆鈕永遠畫出來（關著的時候應該只有那兩顆「開始」）",
     "setEl('aloff', D.flag_exists", "setEl('aloff', true||D.flag_exists"),
    # ── ⭐⭐ 「打開」那一顆（2026-09-09）：⛔ **文案／畫面／接線那半**
    #    （這個專案連五輪的固定失敗形狀就是「只守自己剛寫的邏輯那半」）
    ("Ⓝ1 確認條那句話兩種模式寫同一句（真錢那次會寫成「只是演練」）",
     '        return {"live": True,', '        return {"live": False,'),
    ("Ⓝ1b 真錢那句不提「你的錢」（他不會知道那一下代表什麼）",
     '"真的送單，一天一次，%g 點停利／%g 點停損。"',
     '"送出委託單。"'),
    ("Ⓝ1c 演練那句改成真錢那種語氣（狼來了）",
     '            "text": "現在是演練模式，%s 會照跑但不會真的送單。" % when}',
     '            "text": "現在會用你的錢真的送單。"}'),
    ("Ⓝ2 fire_arm_confirm 自己再問一次 broker（兩把尺）",
     "def fire_arm_confirm(live, now=None):",
     "def fire_arm_confirm(live, now=None):\n    live = broker.is_live()"),
    # ── ⭐⭐ R2（2026-09-09 退件）：確認條的「今天／下一個交易日」
    #    ⛔ 那句話必須跟 `_auto_tick()` 真正的觸發條件同一把尺。
    ("Ⓡ1 文案退回退件前（永遠寫「下一個交易日」，盤前按下去就是假話）",
     '    when = ("今天 " if fire_fires_today(now) else "下一個交易日 ") + SIGNAL_AT',
     '    when = "下一個交易日 " + SIGNAL_AT'),
    ("Ⓡ1b 文案反過來（永遠寫「今天」）",
     '    when = ("今天 " if fire_fires_today(now) else "下一個交易日 ") + SIGNAL_AT',
     '    when = "今天 " + SIGNAL_AT'),
    ("Ⓡ2 fire_fires_today 永遠回 True（週末也說「今天」）",
     '    return market_session(sig_at) == "day"', '    return True'),
    ("Ⓡ3 fire_fires_today 永遠回 False（＝退回退件前的行為）",
     '    # ① 今天那一刻已經過去了', '    return False\n    # ① 今天那一刻已經過去了'),
    ("Ⓡ4 不看 AUTO['done']（面板 09:10 才重開，卻還說「今天會送」）",
     '    if AUTO.get("day") == str(now.date()) and AUTO.get("done"):\n        return False',
     '    if False:\n        return False'),
    ("Ⓡ5 只看 done、不管是哪一天（昨天送過 ⇒ 今天永遠說「下一個交易日」）",
     '    if AUTO.get("day") == str(now.date()) and AUTO.get("done"):',
     '    if AUTO.get("done"):'),
    # ⚠️⚠️ 2026-09-10 修：這一條的**突變目標字串過期了**（那天上午條件 ② 加了
    #    `+ AUTO_LATE_MS / 1000`）⇒ 它**整條沒跑**，而舊版的總結把「沒跑」印成
    #    「打紅」。⛔ 目標字串是會隨產品一起腐爛的東西，總結一定要看得到「沒跑」。
    ("Ⓡ6 牆上時鐘那道拿掉（09:04 按下去還說「今天」）",
     '    if now.hour * 3600 + now.minute * 60 + now.second >= SIGNAL_SEC + AUTO_LATE_MS / 1000:\n        return False',
     '    if False:\n        return False'),
    # ⛔⛔ 補送窗口那一段（2026-09-10 上午加的）**本來零守衛**：拿掉它，
    #    ⑬c 上面那 10 項全綠（那一組模擬「面板一路開著」⇒ 永遠由條件 ① 擋下來）。
    #    紅的是 ⑬c 新的「看門狗剛重啟」那一組。
    ("Ⓡ6b 補送窗口拿掉（面板剛重啟的那 3 秒按下去，說「下一個交易日」卻今天就送）",
     '>= SIGNAL_SEC + AUTO_LATE_MS / 1000:', '>= SIGNAL_SEC:'),
    # ⚠️ Ⓡ7 現在**行為上是等價的**（`market_session` 那把尺在 09:03:30 只剩星期在起作用），
    #    但它是「兩把尺」的種子：哪天 market_session 長出假日表，這裡就會靜靜地分岔。
    #    ⇒ 由 ⑬c 那條「判斷用的是 SIGNAL_SEC ＋ market_session」的原始碼斷言擋。
    ("Ⓡ7 自己寫 weekday() 取代 market_session（⛔ 兩把尺的種子）",
     '    return market_session(sig_at) == "day"', '    return now.weekday() < 5'),
    ("Ⓝ3 開著的時候還是把「打開」畫出來（開與關同時在畫面上）",
     "if(D.flag_exists) return '';", "if(false) return '';"),
    ("Ⓝ4 第一段（選做法）就直接送出請求（⛔ 沒確認就武裝真錢）",
     " return '<div class=\"row\">'+", " fetch('/api/fire/on');\n return '<div class=\"row\">'+"),
    ("Ⓝ5 前端不帶自訂標頭（後端會擋 ⇒ **每一顆鈕**從此按不動）",
     "'X-Panel':'1',", ""),
    ("Ⓝ5b 前端不帶 token", "'X-Panel-Token':PTOK}", "}"),
    # ── ⭐⭐ P0（2026-09-09）：前端「只有一個 POST 出口」那條鐵律
    ("Ⓝ5c 平倉那顆自己寫一份 fetch（⛔ 第二把尺，漏標頭 ⇒ 他平不掉倉）",
     "  pfetch('/api/real/close')",
     "  fetch('/api/real/close',{method:'POST',"
     "headers:{'Content-Type':'application/json'},body:'{}'})"),
    # ⚠️ 這兩條要**分別**打得到（2026-09-09 第一版寫成 `" if(s&&s.token) PTOK=s.token;"`
    #    ⇒ `.replace(old,new,1)` 換掉的是 `ptok()` 裡那一份（它排在前面），
    #    而守衛只比「整份前端有沒有這個字串」⇒ **打不紅**。
    #    ⛔ 通則：突變字串要**唯一**，守衛要**指名是哪一個函式裡的那一行**。
    ("Ⓝ5d tick() 不更新 token（看門狗重啟後每一顆鈕都 403，而畫面看不出原因）",
     " if(s&&s.token) PTOK=s.token;\n LASTS=s;", " LASTS=s;"),
    ("Ⓝ5e ptok() 拿不到 token（畫面剛開、第一次 tick 還沒回來就按 ⇒ 403）",
     ".then(s=>{ if(s&&s.token) PTOK=s.token; }).catch(()=>{});",
     ".catch(()=>{});"),
    # ── ⭐ R1（2026-09-09 退件）：那條 405 的守衛原本是恆真的（`do_GET` 的中文註解
    #    本身就含 `/api/fire/on` 與 `405`）。改成行為斷言之後，這一條要在
    #    **test_auto_fire 這一層**也紅得起來（以前只有 test_fire_routes 抓得到）。
    ("Ⓡ8 GET /api/fire/on 不再回 405（⛔ 一個 <img src> 就幫他打開了）",
     '        if self.path.split("?", 1)[0] == "/api/fire/on":\n'
     '            return self._json(405, {"ok": False, "msg": "這個端點只收 POST"})\n'
     '        if self.path.startswith("/api/fire/state"):',
     '        if self.path.startswith("/api/fire/state"):'),
    ("Ⓡ9 GET /api/fire/state 的路由改名（端點整個不見，但字串還在檔案裡）",
     '        if self.path.startswith("/api/fire/state"):\n',
     '        if self.path.startswith("/api/fire/statXX"):\n'),
    ("Ⓝ6 切進分頁時不重置兩段式（回來時看到一條展開到一半的確認條）",
     " ALON.step='idle'; ALON.mode=null; ALON.busy=false; ALON.err='';\n alFetch();",
     " alFetch();"),
    ("Ⓝ7 確認條的文案改成前端自己寫死（後端說什麼都沒用）",
     "esc(C.text)", "'現在是演練模式，會照跑但不會真的送單。'"),
]

# ── 第四組：**「打開」那顆的端點與六道防護**（只有 test_fire_routes.py 打得到）
#    ⛔⛔ 這一組就是「別人幫我做的那一側」：每一道防護單獨拿掉都要有東西紅，
#    ⛔ 打不紅 ＝ 那一道等於沒有（他電腦上任何一個網頁都能替他武裝真錢）。
LP3_MUT = [
    ("Ⓖ1 拿掉 Content-Type 檢查（⛔ 一個 <form> 就送得出去）",
     '    if ct != "application/json":', '    if False:'),
    ("Ⓖ1b Content-Type 只要「含有 json」就算（application/json-x…）",
     '    if ct != "application/json":', '    if "json" not in ct:'),
    ("Ⓖ2 拿掉自訂標頭 X-Panel（簡單請求不必 preflight）",
     '    if (headers.get("X-Panel") or "").strip() != "1":', '    if False:'),
    ("Ⓖ2b X-Panel 只看「有沒有」不看值", '.strip() != "1":', '.strip() == "\\x00":'),
    ("Ⓖ3 拿掉 Origin 檢查（跨站 POST 直接成功）",
     '    if not _fire_origin_ok(headers.get("Origin")):', '    if False:'),
    # ⚠️ ⛔ 這一條原本寫成「把那個 if 改成 False」，但那是**等價突變**：
    #    掉下去 urlsplit("null") 的 scheme 是空的，照樣回 False ⇒ 結構上打不紅
    #    （2026-09-09 實測）。真正的弱化是「把 null 當成本機」，改成這個之後就紅了。
    #    ⛔ 通則：打不紅先問「這個突變真的弱化了什麼嗎」，不要急著加測試。
    ("Ⓖ3b Origin=null 當成合法（sandbox iframe／file://）",
     '    if o.lower() == "null":\n        return False',
     '    if o.lower() == "null":\n        return True'),
    ("Ⓖ3c Origin 只比「開頭是不是 http://127.0.0.1」（127.0.0.1.evil.com 會過）",
     '    return u.scheme in ("http", "https") and (u.hostname or "").lower() in FIRE_LOOPBACK',
     '    return o.startswith("http://127.0.0.1")'),
    ("Ⓖ4 拿掉 token（前三道都是「送不出來」，只有這道是「猜不到」）",
     '    if not tok.isascii() or not secrets.compare_digest(tok, FIRE_TOKEN):',
     '    if False:'),
    ("Ⓖ4b token 沒帶就放行（空字串當成不必檢查）",
     '    if not tok.isascii() or not secrets.compare_digest(tok, FIRE_TOKEN):',
     '    if tok and not secrets.compare_digest(tok, FIRE_TOKEN):'),
    ("Ⓖ4c 非 ASCII 的 token 讓 compare_digest 自己炸（⛔ 拿例外當防線）",
     '    if not tok.isascii() or not secrets.compare_digest(tok, FIRE_TOKEN):',
     '    if not secrets.compare_digest(tok, FIRE_TOKEN):'),
    ("Ⓖ5 拿掉 Sec-Fetch-Site（瀏覽器自己加的那一道）",
     '    if sfs and sfs not in ("same-origin", "none"):', '    if False:'),
    ("Ⓖ6 拿掉 Host 檢查（⛔ DNS rebinding）",
     '    if not _fire_host_ok(headers.get("Host")):', '    if False:'),
    ("Ⓖ7 GET 也收（⛔ 一個 <img src> 就幫他打開了）",
     '        if self.path.split("?", 1)[0] == "/api/fire/on":\n'
     '            return self._json(405, {"ok": False, "msg": "這個端點只收 POST"})\n'
     '        if self.path.startswith("/api/fire/state"):',
     '        if self.path.startswith("/api/fire/state"):'),
    ("Ⓖ8 路由改用 startswith（/api/fire/onXX 全都會中）",
     '        if self.path == "/api/fire/on":', '        if self.path.startswith("/api/fire/on"):'),
    ("Ⓖ9 mode 不驗（C／D／亂碼全部寫得進去）",
     '    if not isinstance(mode, str) or mode not in auto_fire.METHODS:', '    if False:'),
    ("Ⓖ9b mode 先寫再驗（驗失敗那一瞬間開關已經是開著的）",
     '    if not isinstance(mode, str) or mode not in auto_fire.METHODS:\n'
     '        return 400, {"ok": False, "msg": "只能用「%s」或「%s」這兩個做法" % (\n'
     '            auto_fire.METHOD_NAME["A"], auto_fire.METHOD_NAME["B"])}',
     '    pass'),
    ("Ⓖ10 拿掉 O_EXCL（⛔ 已經開著再按會把他的檔蓋掉）",
     '        fd = os.open(str(flag), os.O_CREAT | os.O_EXCL | os.O_WRONLY)',
     '        fd = os.open(str(flag), os.O_CREAT | os.O_TRUNC | os.O_WRONLY)'),
    ("Ⓖ11 「打開」不落地（誰在什麼時候開的永遠查不到）",
     '    warn = _fire_arm_log(row)', '    warn = None'),
    ("Ⓖ11b 落地寫進 YYYY-MM.jsonl（⛔ 污染 autofire 帳本的不變式）",
     '        p = auto_fire.FIRE_DIR / ("arm-" + str(row["date"])[:7] + ".jsonl")',
     '        p = auto_fire.FIRE_DIR / (str(row["date"])[:7] + ".jsonl")'),
    ("Ⓖ12 state 不帶 token（那顆鈕從此按不動，而且沒人看得出來）",
     '                out["token"] = FIRE_TOKEN', '                pass'),
    ("Ⓖ13 arm_confirm 不帶出去（前端只能自己猜真錢／演練）",
     '                out["arm_confirm"] = fire_arm_confirm(out.get("live"))', '                pass'),
    # ── ⛔⛔⛔ 【P0，2026-09-09 lab-qa】守衛套在 do_POST 入口這件事本身
    ("Ⓟ1 守衛退回「只掛在 /api/fire/on」（⛔ 一張純 HTML 表單就能用他的帳戶送單）",
     '        ok, code, msg = fire_post_guard(self.headers)\n'
     '        if not ok:\n'
     '            return self._json(code, {"ok": False, "msg": msg})\n'
     '        try:\n'
     '            body = json.loads(raw or b"{}")',
     '        try:\n'
     '            body = json.loads(raw or b"{}")'),
    ("Ⓟ1b 守衛只在「不是真錢那兩條」時才過（⛔ 剛好放掉最危險的兩條）",
     '        ok, code, msg = fire_post_guard(self.headers)',
     '        ok, code, msg = (True, 200, "") '
     'if self.path.startswith("/api/real/") else fire_post_guard(self.headers)'),
    # ── ⛔ 「放寬型」：QA 說她打不紅的那兩種（token 只比前綴、Host 用 endswith）
    ("Ⓟ2 token 只比前 8 碼（⛔ 舊的「token 猜錯」那條打不紅它）",
     '    if not tok.isascii() or not secrets.compare_digest(tok, FIRE_TOKEN):',
     '    if not tok.isascii() or tok[:8] != FIRE_TOKEN[:8]:'),
    ("Ⓟ2b token 用 startswith（帶對的前綴再接一截也過）",
     '    if not tok.isascii() or not secrets.compare_digest(tok, FIRE_TOKEN):',
     '    if not tok.isascii() or not tok.startswith(FIRE_TOKEN[:8]):'),
    ("Ⓟ3 Host 改用 endswith（⛔ evil-127.0.0.1 會過）",
     '    return h in FIRE_LOOPBACK',
     '    return any(h.endswith(x) for x in FIRE_LOOPBACK)'),
    ("Ⓟ3b Host 改用 startswith（⛔ 127.0.0.1.evil.com 會過）",
     '    return h in FIRE_LOOPBACK',
     '    return any(h.startswith(x) for x in FIRE_LOOPBACK)'),
    ("Ⓟ4 Origin 不驗形狀（⛔ http://evil@127.0.0.1 會過）",
     '    if not _FIRE_ORIGIN_RE.match(o):\n        return False',
     '    if False:\n        return False'),
    # ── ⛔ Content-Length（⛔ abc ⇒ traceback 斷線；-1 ⇒ 執行緒卡住）
    ("Ⓟ5 Content-Length 退回 int()（abc ⇒ 噴 traceback、-1 ⇒ 執行緒卡住）",
     '        try:\n'
     '            n = int(_cl) if (_cl or "").strip() else 0\n'
     '        except (TypeError, ValueError):\n'
     '            return self._json(400, {"ok": False, "msg": "Content-Length 看不懂"})\n'
     '        if n < 0 or n > MAX_POST_BYTES:\n'
     '            return self._json(400, {"ok": False, "msg": "body 太大或長度不合理"})',
     '        n = int(_cl or 0)'),
    ("Ⓟ5b 只擋看不懂的、不擋負數與超大（⛔ -1 那條路照樣卡住執行緒）",
     '        if n < 0 or n > MAX_POST_BYTES:\n'
     '            return self._json(400, {"ok": False, "msg": "body 太大或長度不合理"})',
     '        if False:\n'
     '            return self._json(400, {"ok": False, "msg": "body 太大或長度不合理"})'),
    # ── ⛔ 端出 token 的兩個 GET 的守衛（④ 那一道的真正強度）
    ("Ⓟ6 /api/fire/state 不設防（⛔ DNS rebinding 讀得到 token）",
     '            ok, code, msg = fire_get_guard(self.headers)\n'
     '            if not ok:\n'
     '                return self._json(code, {"ok": False, "msg": msg})\n'
     '            try:\n'
     '                out = auto_fire.state()',
     '            try:\n'
     '                out = auto_fire.state()'),
    ("Ⓟ7 /api/state 不設防（⛔ token 就在那份 JSON 裡）",
     '            ok, code, msg = fire_get_guard(self.headers)\n'
     '            if not ok:\n'
     '                return self._json(code, {"ok": False, "msg": msg})\n'
     '            LAST_CLIENT["at"] = time.time()',
     '            LAST_CLIENT["at"] = time.time()'),
    ("Ⓟ7b /api/state 不帶 token（他的每一顆鈕都要先繞一次才按得動）",
     '                    payload = json.dumps(dict(STATE, token=FIRE_TOKEN),\n'
     '                                         ensure_ascii=False).encode()',
     '                    payload = json.dumps(STATE, ensure_ascii=False).encode()'),
]
# ⚠️ `main()` 的接線（`eod_at=EOD_CLOSE_AT`、`AUTO_EOD_HOOK = auto_fire.on_eod`）
#    **不在這一組**：`test_fire_routes.py` 自己接線、走不到 `main()`。
#    守那一半的是 `test_auto_fire.py` ⑧ 的 `wiring_fails()`（它自己帶 10 個突變）。


def run(src_dir, test=None, var="AF_SRC_DIR"):
    test = test or TEST
    env = dict(os.environ)
    env.update({var: str(src_dir) if src_dir else "",
                "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
    env.pop("AF_SRC_DIR" if var != "AF_SRC_DIR" else "LP_SRC_DIR", None)
    if not src_dir:
        env.pop(var, None)
    # ⛔ 一定要有 timeout：某些突變（例如把 put_nowait 換成阻塞式 put）會讓
    #    測試永遠掛住，沒有 timeout 的話這支就在那裡等到天荒地老，
    #    還會留下一個殺不掉的殭屍行程。逾時＝那個突變被抓到了（照樣算紅）。
    try:
        p = subprocess.run([PY, str(test)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env,
                           cwd=str(SHIO), timeout=240)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + (e.stderr or "")
        return 124, (out if isinstance(out, str) else "") + "\n⛔ 測試逾時（掛住了）"
    out = (p.stdout or "") + (p.stderr or "")
    return p.returncode, out


TMP = pathlib.Path(tempfile.mkdtemp(prefix="fire-mut-")).resolve()


def _cleanup():
    """
    ⛔⛔ **任何結束路徑都要把暫存區收乾淨**（Ctrl-C、例外、逾時、正常結束）——
    這支被中斷過一次，留下的殘骸差點變成隔天早上一口沒人授權的真單。

    ⛔ 白名單語意（CLAUDE.md「擋破壞性操作要列舉准許的地方」）：
       只准刪「`mkdtemp` 開出來的那一個資料夾」本身，而且要先驗它真的長那個樣子。
       ⛔ 這支程式裡**沒有任何一行**會刪 `tools/shioaji/` 底下的東西。
    """
    p = TMP
    if p.name.startswith("fire-mut-") and p.parent != p and p.is_dir():
        shutil.rmtree(p, ignore_errors=True)
    # 收尾一定要講一句真的那個開關檔在不在（跟另外兩支對齊）——
    # ⛔ 只是「看一眼並印出來」，不做任何處置。
    print("　" + _real_arm_line(""), flush=True)


atexit.register(_cleanup)


RESULTS = []            # 每一組一筆，總結那句話**只准照這裡的實測數字寫**


def sweep(title, muts, src, fname, test, var):
    """一組突變。⛔ 先做尺的自證（沒有突變時要全綠）。

    ⚠️⚠️ **2026-09-10 修「總結會說謊」**（PM 裁示）：尺的自證沒過時整組不跑是對的
       （跑了也不能解讀 —— 分不出是突變打紅的還是本來就紅的），
       但舊版只記一句 `bad += 1`，總結照樣印「128 個突變…打紅 126」——
       **那一次 85 個突變一個都沒跑，卻被算成打紅了。**
       ⛔ **一支會謊報成績的測試工具比沒有還危險。**
       現在：跳過的一律進 `skipped`（⛔ 不併進「打紅」），總結逐組列出，
       而且只要有任何一個沒跑或沒打紅，**整支非零離開**。
    """
    r = {"title": title, "n": len(muts), "ran": 0, "killed": 0,
         "alive": [], "skipped": 0, "base_ok": False, "why": ""}
    RESULTS.append(r)
    print(f"\n=== 尺的自證：{title} 原封不動要全綠 ===")
    (TMP / fname).write_text(src, encoding="utf-8")
    code, out = run(TMP, test, var)
    base_ok = (code == 0 and "全部通過" in out)
    r["base_ok"] = base_ok
    print(("  OK   " if base_ok else "  FAIL ") + f"原封不動的 {fname}：全部通過"
          + ("" if base_ok else "  ⇒ " + out[-800:]))
    if not base_ok:
        r["skipped"] = len(muts)
        r["why"] = "尺的自證沒過（原封不動就不是綠的）"
        print(f"  ⛔ 這一組 {len(muts)} 個突變**整組沒跑** —— "
              f"⛔ 那不叫「打紅」，總結會分開算，離開碼非零。")
        return
    print(f"\n=== 突變（{title}）：每一道守衛拿掉都要有東西紅 ===")
    for name, old, new in muts:
        if old not in src:
            print(f"  FAIL {name}  ⇒ ⛔ 突變目標不在原始碼裡（守衛可能已經被改掉了）"
                  f"＝**這個突變沒跑**")
            r["skipped"] += 1
            continue
        mutated = src.replace(old, new, 1)
        if mutated == src:
            print(f"  FAIL {name}  ⇒ ⛔ 換完之後內容沒變＝**這個突變沒跑**")
            r["skipped"] += 1
            continue
        (TMP / fname).write_text(mutated, encoding="utf-8")
        code, out = run(TMP, test, var)
        nfail = sum(1 for ln in out.splitlines() if ln.startswith("  FAIL"))
        summed = ("全部通過" in out) or ("項沒過" in out)
        killed = code != 0
        r["ran"] += 1
        r["killed"] += bool(killed)
        if not killed:
            r["alive"].append(name)
        print(f"  {'OK   ' if killed else 'FAIL '}{name}  "
              f"⇒ {'紅了' if killed else '⛔ 打不紅'}"
              f"（FAIL {nfail} 項{'' if summed else '、⚠️ 沒有印出總結'}）")
        if not killed:
            print("        " + out[-400:].replace("\n", "\n        "))
    (TMP / fname).write_text(src, encoding="utf-8")


sweep("auto_fire.py", MUT, SRC, "auto_fire.py", TEST, "AF_SRC_DIR")
# ⛔⛔ 第二組打的是**產品的路由**，只有 test_fire_routes.py（真的起服務打進去）
#    才抓得到。舊守衛用字串比對 ⇒ 這一組會全部打不紅（那就是退件 M1 的形狀）。
sweep("live_panel.py 的路由", LP_MUT, LPSRC, "live_panel.py", ROUTE_TEST, "LP_SRC_DIR")
# ⛔⛔ 第三組打的是 live_panel 的**主迴圈與畫面**（4Hz 那一段、那顆關閉鈕的顯示條件）。
#    這一組只有 test_auto_fire.py 抓得到 —— 而它以前只吃 AF_SRC_DIR，
#    所以 lab-qa 打在這裡的 Q11／Q9 整組打不紅。
sweep("live_panel.py 的主迴圈與畫面", LP2_MUT, LPSRC, "live_panel.py", TEST,
      "LP_SRC_DIR")
# ⛔⛔ 第四組打的是「打開自動下單」那顆的**端點與六道防護**（2026-09-09 加）。
#    ⚠️ 這一組刻意全部打在「別人幫我做的那一側」——
#    前四輪的固定失敗形狀就是「守衛只蓋到新寫的那一側，端點／文案／接線沒守」。
sweep("live_panel.py 的「打開」端點與防護", LP3_MUT, LPSRC, "live_panel.py",
      ROUTE_TEST, "LP_SRC_DIR")

shutil.rmtree(TMP, ignore_errors=True)

# ══ 總結 ⛔ 只准照 RESULTS 的實測數字寫 ═══════════════════════════════════
#    ⛔⛔ 「沒跑」與「打紅」是兩件事，**不可以合併成一個數字**（2026-09-10 修）。
_tot = sum(r["n"] for r in RESULTS)
_ran = sum(r["ran"] for r in RESULTS)
_killed = sum(r["killed"] for r in RESULTS)
_alive = sum(len(r["alive"]) for r in RESULTS)
_skipped = sum(r["skipped"] for r in RESULTS)
print("\n=== 總結 ===")
for r in RESULTS:
    print(f"  {r['title']}：{r['n']} 個 ⇒ 實跑 {r['ran']}、打紅 {r['killed']}、"
          f"打不紅 {len(r['alive'])}、⛔ 沒跑 {r['skipped']}"
          + (f"（{r['why']}）" if r["why"] else ""))
    for _n in r["alive"]:
        print(f"      ⛔ 打不紅：{_n}")
print(f"\n{_tot} 個突變（auto_fire {len(MUT)} ＋ live_panel 路由 {len(LP_MUT)}"
      f" ＋ live_panel 主迴圈／畫面 {len(LP2_MUT)}"
      f" ＋ 「打開」端點與防護 {len(LP3_MUT)}）："
      f"實跑 {_ran}、打紅 {_killed}、打不紅 {_alive}、⛔ 沒跑 {_skipped}")
if _killed == _tot:
    print("全部通過 ✅")
else:
    # ⛔ 這句話要說得出「哪一種沒過」——「沒跑」被說成「打紅」就是上一版的病。
    print(f"⛔ {_tot - _killed} 個沒有打紅"
          + (f"（其中 {_skipped} 個**根本沒跑**）" if _skipped else ""))
sys.exit(0 if _killed == _tot else 1)
