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
     '        if self.path.startswith("/api/fire/state"):\n            try:\n'
     '                out = auto_fire.state()',
     '        if False:\n            try:\n                out = auto_fire.state()'),
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
    ("Ⓠ9b 那顆鈕永遠畫出來（關著的時候整頁應該 0 顆按鈕）",
     "setEl('aloff', D.flag_exists", "setEl('aloff', true||D.flag_exists"),
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


TMP = pathlib.Path(tempfile.mkdtemp(prefix="fire-mut-"))
bad = 0


def sweep(title, muts, src, fname, test, var):
    """一組突變。回傳打不紅的個數。⛔ 先做尺的自證（沒有突變時要全綠）。"""
    global bad
    print(f"\n=== 尺的自證：{title} 原封不動要全綠 ===")
    (TMP / fname).write_text(src, encoding="utf-8")
    code, out = run(TMP, test, var)
    base_ok = (code == 0 and "全部通過" in out)
    print(("  OK   " if base_ok else "  FAIL ") + f"原封不動的 {fname}：全部通過"
          + ("" if base_ok else "  ⇒ " + out[-800:]))
    if not base_ok:
        bad += 1
        return
    print(f"\n=== 突變（{title}）：每一道守衛拿掉都要有東西紅 ===")
    for name, old, new in muts:
        if old not in src:
            print(f"  FAIL {name}  ⇒ ⛔ 突變目標不在原始碼裡（守衛可能已經被改掉了）")
            bad += 1
            continue
        mutated = src.replace(old, new, 1)
        if mutated == src:
            print(f"  FAIL {name}  ⇒ ⛔ 換完之後內容沒變")
            bad += 1
            continue
        (TMP / fname).write_text(mutated, encoding="utf-8")
        code, out = run(TMP, test, var)
        nfail = sum(1 for ln in out.splitlines() if ln.startswith("  FAIL"))
        summed = ("全部通過" in out) or ("項沒過" in out)
        killed = code != 0
        tag = "OK   " if killed else "FAIL "
        bad += not killed
        print(f"  {tag}{name}  ⇒ {'紅了' if killed else '⛔ 打不紅'}"
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

shutil.rmtree(TMP, ignore_errors=True)
_tot = len(MUT) + len(LP_MUT) + len(LP2_MUT)
print(f"\n{_tot} 個突變（auto_fire {len(MUT)} ＋ live_panel 路由 {len(LP_MUT)}"
      f" ＋ live_panel 主迴圈／畫面 {len(LP2_MUT)}），打紅 {_tot - bad} 個"
      + ("　全部通過 ✅" if not bad else f"　⛔ {bad} 個打不紅"))
sys.exit(1 if bad else 0)
