# -*- coding: utf-8 -*-
r"""
真實交易外洩掃描器 —— commit 前的守衛。

【為什麼要有這支】這個 repo 是**公開的**，而 CLAUDE.md 兩處明寫
「真實交易的紀錄與心得絕不上傳（Benson 的決定）」。
2026-09-03 一天之內同一條規則被撞開六次（PM 兩次、dev 兩次、QA 抓到兩次），
每一次的想法都是「我只要把價格換掉就安全了」——
**錯：進出場時間、賺賠點數、心得原文，任何一項單獨拿出來都是他的真實紀錄。**
靠人盯已經證明會漏，所以做成會擋的檢查。

  跑法： py tools/probe/leak-scan.py      （或 repo 內的 .venv 那支 python）
  命中就以非零離開，並印出「檔案:行號 值」。

────────────────────────────────────────────────────────────────────────
【掃描範圍】`git ls-files` **∪ 未追蹤但沒被 gitignore 的檔**。

  ⛔ **這支一定要掃到它自己。** 第一版只掃 `git ls-files`，而它自己還沒 git add ⇒
     它的註解裡舉例用了兩個**他真實的成交價**，自己卻報「乾淨」（QA 2026-09-03 抓到）。
     用 `--others --exclude-standard` 之後，還沒 add 的新檔（包含這一支）也在範圍內；
     `main()` 另外斷言「掃描清單裡有這支自己」，沒有就當成尺壞了、非零離開。

【判準】欄位的「熵」差很多，一律同等對待的話不是漏抓就是全是雜訊。實測定出兩級：

  ● 單獨命中就算（高熵，實測全 repo 雜訊 0）
      - entry / exit 價格。**實測全 repo 只命中 4 處**（2 處是這支自己的洩漏、
        2 處是合成 K 棒示範資料剛好撞號）⇒ 值得單獨報。
        ⚠️ 第一版要求「旁證」（同一筆的另一個欄位要在 ±2 行內），於是
           **「註解裡單獨引一個價格」整類抓不到** —— 那正是這支自己犯的形狀。
           現在單獨出現的價格會被抓到。
      - entry_time / exit_time（完整 HH:MM:SS）
      - note 整串
      - note 的**連續 8 字子字串**：人引述心得多半是節錄，那是最可能的外洩形狀。
        實測 8 字 → 74 種子字串、命中 0 處；6 字會誤咬 `data/practice.json` 裡
        他自己在**練習**紀錄寫的相似句子（同一個人講話會像，那不是外洩）。

  ● 要旁證才算（低熵）
      - points：只有 4 個值，而 `100` / `-100` 就是他固定的 ±100 規則值。
        **實測單獨比對會命中 318 處**，全是雜訊 ⇒ 必須同一筆的**另一個欄位**
        出現在 ±2 行之內才報。
      - **值剛好等於程式自己的產品常數的時間欄**（2026-09-10 PM 裁示，見下面那一段）。

  ● **沒有作證資格**（2026-09-10 PM 第二次裁示，`can_corroborate()`）
      - **值等於產品常數的欄位**（時間或數值，⛔ note 除外）。
        旁證的意義是「**這一行附近有他個人獨有的資訊**」；一個值如果等於產品常數，
        它**本來就公開**（讀程式的人都知道）⇒ **既不能自己成立、也不能替別人作證**。
        這是同一條原則的兩面，⛔ 不是新開一個後門。

────────────────────────────────────────────────────────────────────────
【為什麼「時間 ＝ 產品常數」要降級】（2026-09-10 PM 裁示；⛔ 這不是豁免、不是白名單）

  把「完整 `HH:MM:SS`」放進「單獨命中就算」那一層，靠的是一個**前提**：
  **「一個精確到秒的時間，是他個人交易獨有的資訊」**。

  **2026-09-10 這個前提破了**：他開始用【自動下單】，而那支程式的進場時間
  **永遠等於 `SIGNAL_AT` 這個產品常數**。於是他真實紀錄的 `entry_time`
  跟原始碼裡的常數變成同一個字串 ⇒ 這支從那天起**恆紅 224 處**
  （203 處 `entry_time` ＋ 21 處被它當旁證撐起來的 `points`），
  **全部是 2026-09-07 就在的既有內容**（拿 `5a5395a` 逐檔數過）。

  破在哪裡講清楚：`SIGNAL_AT` 那個時刻出現在 repo 裡**沒有洩漏任何東西** ——
  任何人讀了 `auto_fire.py` 都知道這支程式在那一刻送單，那是**程式自己公開的規格**，
  不是他的秘密。相對地，`09:11:07` 這種**不等於任何常數**的時間仍然是他獨有的，
  ⇒ **那一層一點都沒有放寬。**

  ⛔ 所以規則寫成「**去程式裡把常數讀出來再比對**」（`product_consts()` 用 AST
     讀 `CONST_SOURCES` 的模組層級常數），**⛔ 這支裡面一個時間字串都沒有寫死**：
     常數改成別的時間，這支自動跟著改；有人把常數刪掉，它自動變回嚴格。
  ⛔ **只降這一層**：降級後它走的是 `points` 那一層 ——
     同一筆的**另一個欄位**（進出場價／點數／心得）出現在 ±2 行之內就**照樣命中**。
     他真實那一趟來回的其他欄位一個都沒有放過。
  ⚠️ 那一版的代價：**「常數 ＋ 他真實的點數（±100）」這種組合仍然會報** ⇒
     **降級前 224 處 → 降級後 39 處**，那 39 處（18 `entry_time` ＋ 21 `points`）
     **全部是同一種形狀**：`SIGNAL_AT` 與 ±100 這**兩個產品常數**被寫在同一段話裡。
     ⇒ 見下面〈常數 × 常數 ＝ 零資訊量〉，那是 2026-09-10 下午收掉的。

────────────────────────────────────────────────────────────────────────
【常數 × 常數 ＝ 零資訊量】（2026-09-10 PM 第二次裁示，`can_corroborate()`）

  上面那 39 處撐起來的「旁證」是**另一個產品常數**（`points=100` ＝ `TP_POINTS`）。
  **旁證的意義是「這一行附近有他個人獨有的資訊」** —— 兩個公開的常數寫在一起，
  資訊量是零：讀程式的人本來就知道這支程式在 `SIGNAL_AT` 送單、停利停損是 ±100。
  ⇒ **值等於產品常數的欄位，一併排除在「旁證資格」之外。**
  ⛔ 這是同一條原則的兩面（不能自己成立／不能替別人作證），**不是新開一個後門**。

  ⛔ 數值常數同樣**從程式裡讀出來**（`_fmt_const()`，`TP_POINTS`／`SL_POINTS`／
     `EOD_WINDOW_S`… 現況 26 個），⛔ 這支裡面**一個數字都沒有寫死**。
     格式化跟 `load_records()` **同一套**（`100.0` → `"100"`）——
     兩邊不一致的話這條規則會安靜地失效（自證有一條在守，突變 ⑧ 證明會紅）。
     只認**字面值**：`8 * 3600 + 45 * 60` 這種算式不算（這支不替產品算數），
     `True`／`False` 不算（`isinstance(True, int)` 是 True）。

  ⚠️⚠️ **界線在哪（下一個人要看得出來）**：這個降級唯一會變成洞的形狀是
     **「他真實紀錄的值剛好等於某個產品常數，而同一段話裡其他欄位也全是常數」**。
     那時我們放掉的是「常數的**組合**」所攜帶的資訊（例：某天他的損益剛好是 30 點，
     而 `C_THRESH = 30`，那麼「`SIGNAL_AT` ＋ 30 點」這段話就抓不到了）。
     現況量過：他 12 筆真實紀錄的 28 個數值裡**只有 `100` 落在常數清單**（＝ ±100 規則值），
     時間欄則只有自動下單那一筆等於 `SIGNAL_AT` ⇒ 目前被放掉的**只有裁示要放行的那一種**。
     兩個要盯的變化：
       ① **有人在 `CONST_SOURCES` 的模組層級新增常數 ＝ 幫那個值開後門**，
          尤其是**接近他紀錄尺度的數字**（哪天多一個 `SOME_PX = 46980`，
          那個價格就同時失去「自己成立」與「作證」兩種資格）。
          ⇒ 每次跑都把「讀到哪些常數」印出來（時間 4 個／數值 26 個），
          **清單變長是要看的一件事**。
       ② 數值常數裡有一批小數字（1／2／3／4／5／8／10／20／24／30…），
          他哪天的 `points` 剛好是其中一個就會踩到上面那個形狀。
          **但只要同一段話裡還有他的價格或心得，就照樣命中**
          （負面對照 ①②④ 在守這件事）。
     ⛔ 這條規則**不對 `note` 生效**（心得是自由文字，不可能是產品常數）——
        明寫出來是免得有人哪天把 `consts` 的來源放寬到一般字串常數，
        一句被抄進原始碼的心得就順便失去了作證資格。

  【這兩條規則的突變測試】（2026-09-10 lab-dev，9 個**全紅**；
   ⛔ 沒有負控組的綠燈在這個專案不算數。跑手不進 repo，明細如下）
     ① 整層豁免（常數 ⇒ 直接跳過、旁證也不管）        → 紅（負面對照①④）
     ② 所有時間都降級（`val in consts` 拿掉）          → 紅（兩條）
     ③ `CONST_SOURCES` 清空（常數讀不到）              → 紅（尺壞了）
     ④ `tree.body` 換成 `ast.walk`（函式裡的字面值也算）→ 紅（時間與數值各一條）
     ⑤ `TIME_FIELDS` 多塞 `entry`／`exit`              → 紅
        ⚠️ **上一輪這個突變打不紅**（那時常數清單全是 `HH:MM(:SS)`，價格永遠比不中，
           是空包彈）；**數值常數進來之後它就有牙齒了** ⇒ 補了一條負控組
           「價格剛好等於某個產品常數 ⇒ 照樣單獨命中（降級只降時間欄）」。
           **通則：上一輪判定「空包彈」的突變，規則變動之後要重新判一次。**
     ⑥ ⭐ 把「常數不能當旁證」整條拿掉                 → 紅（負面對照③：39 處全部回來）
     ⑦ ⭐ 所有數值都當成產品常數（全部失去作證資格）   → 紅（負面對照①②）
     ⑧ 數值常數不照紀錄的寫法格式化（停在 `"100.0"`）  → 紅（自證的格式那條）
     ⑨ `bool` 的擋拿掉（`True` 變成常數 `"1"`）        → 紅

【這支自承會漏掉什麼】（誠實講限制，不要假裝滴水不漏）
  1. **截斷成 HH:MM 的時間完全不比對**（例如 `09:11`）。而那正是畫面顯示的形式、
     也是 `/api/note` 送出去的欄位 ⇒ **這是一個真的洞**。
     為什麼不加：實測把 HH:MM 納入會命中 **6 萬多筆**（`index_1min.csv`、
     `data/practice.json`、`CLAUDE.md` 裡滿滿的時間字串），雜訊多到沒人會看，
     那跟沒有守衛一樣。**引用時間到分要靠人自己警覺。**
  2. **只洩漏點數欄、旁邊什麼都沒有**抓不到（理由見上面，318 處雜訊）。
  3. note 的節錄**短於 8 字**抓不到（8 字是實測的「零雜訊」下界）。
  4. 價格換了寫法就比對不到：底線分隔、補小數位、或寫成兩個數字相加算出來的。
  5. **`SKIP_SUFFIX` 整類跳過**：`.csv` / 圖檔 / `.pyc` / `.zip` 不掃。
     跳過 `tmf_1min.csv`（54 萬列行情）是為了速度，而且 `*.csv` 本來就在
     `.gitignore` 裡；**但如果有人把紀錄匯出成 csv 再強制加進版控，這支不會擋。**
  6. 只比對「一模一樣的值」。有人手動改成差 1 點之類的變形抓不到。
  7. 比對來源只有 `real_trades/*.jsonl`。`real_orders/`（每張委託單的原始 log）
     **不在範圍內** —— 要納入把 SOURCES 加一行就好。
  8. **命中不一定是抄的，也可能是巧合**（合成的 K 棒示範資料裡剛好出現同一個五位數）。
     處置一律是**把那個數字改掉**，不是加豁免名單 ——
     因為看的人**分不出巧合與外洩**，那就不該讓它留在公開 repo 裡。
  9. **「常數 × 常數」的組合抓不到**（2026-09-10 PM 裁示，界線見上面那一段）：
     同一段話裡他每一個欄位的值都剛好等於某個產品常數時，這支不報。
     現況落在這裡的只有「`SIGNAL_AT` ＋ ±100」這一種形狀 ——
     ⚠️ **這是刻意放行的，不是漏抓；但它是一個會隨常數清單變長而擴大的洞。**

⚠️ 這支**自己會先驗尺**：拿真實值合成幾行測試字串，抓不到就直接失敗並非零離開
   （掃描器壞掉時最危險的症狀不是報錯，是它安靜地開始全綠）。
"""
import ast
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCES = ["tools/shioaji/real_trades/*.jsonl"]
SKIP_DIRS = ("tools/shioaji/real_trades/",)          # 比對來源自己不掃
SKIP_SUFFIX = (".png", ".jpg", ".jpeg", ".ico", ".gif", ".pyc", ".zip", ".csv")
NOTE_SUB = 8                                          # note 子字串長度（實測的零雜訊下界）

SOLO = ("entry", "exit", "entry_time", "exit_time", "note")   # 單獨命中就算
CORROB = ("points",)                                          # 要旁證
TEXT = ("entry_time", "exit_time", "note")                    # 字串比對（其餘走數值邊界）
TIME_FIELDS = ("entry_time", "exit_time")                     # 會被「等於產品常數」降級的欄位

# 產品常數的來源檔。⛔ 這裡放的是**檔案路徑**，不是常數值 ——
# 規則是「去程式裡把常數讀出來再比對」，常數改了這支自動跟著改。
CONST_SOURCES = ("tools/shioaji/live_panel.py", "tools/shioaji/auto_fire.py")
_TIME_LIKE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")


def product_consts():
    """用 AST 讀出 `CONST_SOURCES` 裡「模組層級、全大寫」的產品常數。

    回傳 {值的字串形式: "檔名.常數名"}，**時間常數與數值常數住在同一個字典裡**
    （`SIGNAL_AT="09:03:30"`、`TP_POINTS=100.0` → `"100"`）。
    ⛔ 這不是白名單：
      - 沒有任何一個時間／數字寫死在這支裡；`SIGNAL_AT`／`TP_POINTS` 改成別的值，
        這裡自動跟著改；常數被刪掉，這支自動變回嚴格版。
      - 讀不到（檔案搬走／語法壞掉）就回空的 ⇒ **這支自動變回原本的嚴格版**，
        方向是安全的那一邊（`main()` 另外會斷言至少讀得到一個，讀不到當尺壞了）。
    只收**模組層級**的賦值：函式／類別裡的區域值不算，免得隨便一個字面值就變成後門。
    """
    out = {}
    for rel in CONST_SOURCES:
        p = ROOT / rel
        if not p.exists():
            continue
        try:
            src = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for val, name in _consts_from_source(src).items():
            out.setdefault(val, "%s.%s" % (p.name, name))
    return out


def _fmt_const(node):
    """把 AST 節點轉成「跟 `load_records()` 同一種字串形式」，不是常數就回 None。

    ⚠️ 兩邊的格式化**必須是同一套**（`100.0` → `"100"`），否則比不中 ——
    比不中的方向是「這支變嚴格」（安全），但那條規則就等於沒生效、沒人看得出來。
    ⛔ `bool` 要另外擋：`isinstance(True, int)` 是 True，不然 `FLAG = True` 會變成 `"1"`。
    ⛔ 只認**字面值**（含開頭的負號）：`8 * 3600 + 45 * 60` 這種算式一律不算 ——
       這支不去執行產品的程式碼，也不替它算數。
    """
    sign = 1
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        sign, node = -1, node.operand
    if not isinstance(node, ast.Constant):
        return None
    v = node.value
    if isinstance(v, str):
        return v if (sign == 1 and _TIME_LIKE.match(v)) else None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v) * sign
    return str(int(v)) if v.is_integer() else str(v)


def _consts_from_source(src):
    """`product_consts()` 的純函式那一半（吃字串、不碰檔案）⇒ 自證測得到。

    ⛔ **只收模組層級的賦值**（`tree.body`，⛔ 不是 `ast.walk`）：
       函式／類別裡的區域值不算，免得隨手一個字面值就變成一個時間／數字的後門。
    """
    out = {}
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    for node in tree.body:                           # ⛔ 只走最外層，不用 ast.walk
        if isinstance(node, ast.Assign):
            targets, val = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, val = [node.target], node.value
        else:
            continue
        val = _fmt_const(val)
        if val is None:
            continue
        for t in targets:
            if isinstance(t, ast.Name) and t.id.isupper():
                out.setdefault(val, t.id)
    return out


def is_product_const(field, val, consts):
    """這個值是不是「程式自己就印在原始碼裡」的產品常數 ⇒ 讀了程式的人本來就知道。

    只對時間欄成立。⚠️ 這**不是**「不算命中」，是「降到要旁證那一層」——
    見檔頭〈為什麼「時間 ＝ 產品常數」要降級〉。
    """
    return field in TIME_FIELDS and val in consts


def can_corroborate(field, val, consts):
    """這個欄位有沒有「替別人作證」的資格（2026-09-10 PM 裁示）。

    旁證的意義是「**這一行附近有他個人獨有的資訊**」。
    一個值如果等於產品常數，它**本來就公開**（讀程式的人都知道），
    ⇒ **它既不能自己成立、也不能替別人作證**。這是同一條原則的兩面，
    ⛔ 不是新開一個後門 —— 見檔頭〈常數 × 常數 ＝ 零資訊量〉。

    ⛔ `note` 永遠有資格：心得是自由文字，不可能是產品常數
       （這裡明寫出來，是免得有人哪天把 `consts` 的來源放寬到字串常數，
        一句被抄進原始碼的心得就順便失去了作證資格）。
    """
    return field == "note" or val not in consts


def load_records():
    """把真實交易讀成一筆一筆的 {欄位: 字串}。只讀，絕不寫。"""
    recs = []
    for pat in SOURCES:
        for f in sorted(ROOT.glob(pat)):
            for ln in f.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                d = json.loads(ln)
                r = {}
                for k in ("entry", "exit"):
                    v = d.get(k)
                    if v is not None:
                        # 整數與帶小數兩種寫法都要抓得到
                        r[k] = str(int(v)) if float(v).is_integer() else str(v)
                for k in ("entry_time", "exit_time"):
                    if d.get(k):
                        r[k] = str(d[k])
                if d.get("points") is not None:
                    v = float(d["points"])
                    r["points"] = str(int(v)) if v.is_integer() else str(v)
                if d.get("note"):
                    r["note"] = str(d["note"])
                if r:
                    recs.append((f.name, r))
    return recs


def note_subs(recs):
    """note 的連續 N 字子字串 → 來源檔名。節錄是最可能的外洩形狀。"""
    out = {}
    for src, r in recs:
        n = r.get("note") or ""
        for i in range(len(n) - NOTE_SUB + 1):
            out.setdefault(n[i:i + NOTE_SUB], src)
    return out


def _has(field, val, text):
    if field in TEXT:
        return val in text
    # 數值要用邊界比對，否則 47010 會命中 470100、147010
    return re.search(r"(?<![\d.])" + re.escape(val) + r"(?![\d])", text) is not None


def scan_text(lines, recs, subs, consts=None):
    """回傳 [(行號, 欄位, 值, 說明)]。

    `consts`＝`product_consts()` 的結果；傳空的就是**原本的嚴格版**
    （時間單獨命中就算、任何欄位都能當旁證），
    所以自證可以拿同一支程式跑「降級前／降級後」兩種世界。
    """
    consts = consts or {}
    hits = []
    for i, line in enumerate(lines):
        for src, r in recs:
            for f in SOLO + CORROB:
                if f not in r or not _has(f, r[f], line):
                    continue
                demoted = f in SOLO and is_product_const(f, r[f], consts)
                if f in SOLO and not demoted:
                    hits.append((i + 1, f, r[f][:24], src))
                    continue
                # 要旁證那一層（低熵的 points、以及被降級的「時間＝產品常數」）：
                # 同一筆的**另一個**欄位要出現在 ±2 行之內（一筆測資常跨兩三行）
                # ⛔ **值等於產品常數的欄位沒有作證資格**（`can_corroborate()`）：
                #    常數 × 常數 撐起來的旁證，資訊量是零。
                window = "\n".join(lines[max(0, i - 2):i + 3])
                other = [g for g in r if g != f and can_corroborate(g, r[g], consts)
                         and _has(g, r[g], window)]
                if other:
                    why = "%s（旁證 %s）" % (src, "/".join(other))
                    if demoted:
                        why += "｜值＝產品常數 %s，靠旁證成立" % consts[r[f]]
                    hits.append((i + 1, f, r[f][:24], why))
        for s, src in subs.items():
            if s in line:
                hits.append((i + 1, "note(節錄)", s, src))
    return hits


def self_test(recs, subs, consts):
    """尺的自證：合成幾行一定該被抓到的字串，抓不到就是掃描器壞了。
    配負控組確認雜訊規則真的有在收斂（不然它會變成永遠紅，跟永遠綠一樣沒用）。

    ⚠️ 這裡合成的字串**只活在記憶體**，一個位元組都不落地。
    ⚠️ 「時間＝產品常數」那三條刻意用**自己編的假紀錄**（價格 12000 附近），
       ⛔ 不拿他真的數字去湊 —— 那三條驗的是**規則**，不需要真值。
    """
    rec = next((r for _, r in recs
                if "entry_time" in r and "entry" in r and "points" in r
                and r["entry_time"] not in consts), None)
    note = next((r["note"] for _, r in recs if r.get("note")), None)
    if rec is None or note is None:
        print("  尺壞了：real_trades 裡找不到欄位齊全（且時間不等於產品常數）的紀錄，無法自證")
        return False
    cases = [
        ("價格單獨出現（第一版的死角）", ["# 舉例：%s" % rec["entry"]], True),
        ("完整時間單獨一行（不等於任何產品常數）", ['x = "%s"' % rec["entry_time"]], True),
        ("心得原文", ['s = "%s"' % note], True),
        ("心得節錄 %d 字" % NOTE_SUB, ["# 他寫「%s」" % note[:NOTE_SUB]], True),
        ("點數＋旁證跨兩行", ["entry = %s" % rec["entry"],
                              "pts = %s" % rec["points"]], True),
        ("（負控組）只有點數、沒有旁證", ["FEE = %s" % rec["points"]], False),
        ("（負控組）長得像但不是那個數", ["x = 1%s0" % rec["entry"]], False),
        ("（負控組）心得節錄只有 4 字", ["# %s" % note[:4]], False),
    ]
    cases = [(n, ls, w, recs) for n, ls, w in cases]

    # ── 「值＝產品常數」那兩條規則自己的自證（用自己編的假紀錄，⛔ 不碰他的數字）──
    if not consts:
        print("  尺壞了：一個產品常數都讀不到（CONST_SOURCES 搬走了？）")
        return False
    ct = next((v for v in sorted(consts) if _TIME_LIKE.match(v) and len(v) == 8),
              None)
    cn = _pick_num_const(consts)
    if ct is None or cn is None:
        print("  尺壞了：讀不到完整的時間常數（HH:MM:SS）或數值常數，無法自證")
        return False
    faked = _synth_time(ct, consts, recs)
    px = _synth_px(consts, recs, 5)          # 5 個「絕不撞到他資料」的假價格
    if faked is None or px is None:
        print("  尺壞了：湊不出假時間／假價格（⛔ 不可以拿他的真數字頂替）")
        return False
    fnote = "__自證用的假心得，這句不是他寫的__"
    # ⚠️ 假紀錄的每一個欄位都是編的；`_synth_px()` 已排除他真實的值與所有產品常數，
    #    所以「假價格能不能當旁證」這件事不會被巧合影響。
    fake = [("__自證用的假紀錄__",
             {"entry_time": ct, "entry": px[0], "points": cn})]
    fake_free = [("__自證用的假紀錄__",
                  {"entry_time": faked, "entry": px[0]})]
    fake_exit = [("__自證用的假紀錄__",
                  {"points": cn, "exit": px[1]})]
    fake_full = [("__自證用的假紀錄__",
                  {"entry": px[0], "exit": px[1], "entry_time": ct,
                   "points": cn, "note": fnote})]
    cases += [
        ("【降級】時間＝產品常數（%s）、單獨一行 ⇒ 不再自己成立"
         % consts[ct], ['AT = "%s"' % ct], False, fake),
        ("⭐【負面對照①】時間＝常數 ＋ ±2 行內有他的真實進場價 ⇒ **仍然命中**",
         ["entry = %s" % px[0], "# 進場 %s" % ct], "entry_time", fake),
        ("⭐【負面對照②】點數＝常數（%s=%s）＋ ±2 行內有他的真實出場價 ⇒ **仍然命中**"
         % (consts[cn], cn), ["exit = %s" % px[1], "pts = %s" % cn],
         "points", fake_exit),
        ("⭐【負面對照③】時間＝常數 ＋ 點數＝常數、其他什麼都沒有 ⇒ 不命中"
         "（常數×常數＝零資訊）", ['AT = "%s"' % ct, "PTS = %s" % cn], False, fake),
        ("⭐【負面對照④】一整筆真實紀錄（進出場價／時間／點數／心得）⇒ **五個欄位一個都不放過**",
         ["進場 %s @ %s" % (px[0], ct), "出場 %s，%s 點" % (px[1], cn),
          "心得：%s" % fnote],
         frozenset({"entry", "exit", "entry_time", "points", "note"}), fake_full),
        ("（負控組）時間不在常數清單裡（%s）⇒ 單獨照樣命中" % faked,
         ['t = "%s"' % faked], "entry_time", fake_free),
        ("（負控組）**價格**剛好等於某個產品常數 ⇒ 照樣單獨命中（⛔ 降級只降時間欄）",
         ["x = %s" % cn], "entry", [("__自證用的假紀錄__", {"entry": cn})]),
    ]

    ok = True
    # ── 常數是**怎麼讀出來的**本身也要驗：那是這條規則唯一的攻擊面 ──
    #    ⛔ 用一段合成的原始碼，⛔ 不是真的那兩支（不然改了產品常數這裡就跟著壞）。
    _t1, _t2 = faked, _synth_time(faked, consts, recs)
    _t3 = _synth_time(_t2, consts, recs) if _t2 else None
    if len({_t1, _t2, _t3}) != 3:
        print("  尺壞了：湊不出三個相異的假時間")
        return False
    _n1, _n2, _n3, _n4, _n5 = px          # 五個相異的假數字
    _got = _consts_from_source(
        'X_AT = "%s"\n'
        'lower_at = "%s"\n'
        "X_PTS = %s.0\n"                  # 浮點數要格式化成跟紀錄同一種寫法
        "x_pts = %s\n"
        "X_NEG = -%s\n"
        "X_CALC = %s * 1\n"               # 算式不算（這支不替產品算數）
        "X_FLAG = True\n"                 # ⛔ bool 不可以變成 "1"
        "def f():\n"
        '    INSIDE_AT = "%s"\n'
        "    INSIDE_PTS = %s\n"
        "    return INSIDE_AT, INSIDE_PTS\n"
        % (_t1, _t2, _n1, _n2, _n3, _n4, _t3, _n5))
    for _name, _cond in (
            ("模組層級、全大寫的時間常數讀得到", _got.get(_t1) == "X_AT"),
            ("⛔ 函式**裡面**的時間字串不算常數（⛔ 不准用 ast.walk）", _t3 not in _got),
            ("⛔ 小寫的模組層級變數不算常數", _t2 not in _got),
            ("模組層級、全大寫的**數值**常數讀得到（%s.0 → \"%s\"）" % (_n1, _n1),
             _got.get(_n1) == "X_PTS"),
            ("負數常數讀得到（-%s）" % _n3, _got.get("-" + _n3) == "X_NEG"),
            ("⛔ 函式**裡面**的數字不算常數", _n5 not in _got),
            ("⛔ 小寫的模組層級數字不算常數", _n2 not in _got),
            ("⛔ 算式不算常數（%s * 1）" % _n4, str(int(_n4) * 1) not in _got),
            ("⛔ True／False 不算數值常數（bool 是 int 的子類）", "1" not in _got)):
        ok = ok and bool(_cond)
        print("  %s %s" % ("OK  " if _cond else "壞了", _name))

    for name, lines, want, use in cases:
        hits = scan_text(lines, use, subs if use is recs else {}, consts)
        fields = {h[1] for h in hits}
        if isinstance(want, frozenset):               # 指定「這些欄位都要命中」
            missing = sorted(want - fields)
            passed = not missing
            desc = "抓到 %s" % "/".join(sorted(fields)) if fields else "沒抓到"
            expect = "%d 個欄位全抓到%s" % (
                len(want), "" if passed else "（少了 %s）" % "/".join(missing))
        elif isinstance(want, str):                   # 指定「哪一個欄位」要命中
            passed = want in fields
            desc, expect = ("抓到" if passed else "沒抓到"), "抓到 %s" % want
        else:
            got = bool(hits)
            passed, expect = got == want, ("抓到" if want else "不該抓到")
            desc = "抓到" if got else "沒抓到"
        ok = ok and passed
        print("  %s %s → %s（期待 %s）"
              % ("OK  " if passed else "壞了", name, desc, expect))
    return ok


def _pick_num_const(consts):
    """挑一個「當得成點數」的數值常數當自證素材（`TP_POINTS`／`SL_POINTS` 優先）。

    ⛔ 不寫死 100：常數改成別的值，自證跟著改。
    """
    nums = [(n, v) for v, n in consts.items() if not _TIME_LIKE.match(v)]
    if not nums:
        return None
    for want in ("TP_POINTS", "SL_POINTS", "POINTS"):
        for n, v in sorted(nums):
            if n.endswith(want):
                return v
    return sorted(nums)[0][1]


def _synth_px(consts, recs, n):
    """編 n 個假價格（12000 附近）給自證用。

    ⛔ 逐一排除**他真實紀錄裡的每一個數值**與**所有產品常數** ⇒ 結構上撞不到，
       不是「看起來不像」而已。（12000 附近也刻意離他的 4 萬多點很遠。）
    """
    real = {r[f] for _, r in recs for f in ("entry", "exit", "points") if f in r}
    out = []
    for v in range(12000, 13000):
        s = str(v)
        if s in real or s in consts or "-" + s in consts:
            continue
        out.append(s)
        if len(out) == n:
            return out
    return None


def _synth_time(ct, consts, recs):
    """從產品常數推一個「一定不是常數、也不是他任何一筆真實時間」的假時間。

    ⛔ 這裡不寫死任何時間字串（那會變成把值抄進公開 repo），
       而且會逐一排除他真實紀錄裡的時間 ⇒ 不可能撞到他的資料。
    """
    parts = ct.split(":")
    real = {r.get(f) for _, r in recs for f in TIME_FIELDS if r.get(f)}
    for k in range(1, 24):
        cand = ":".join(["%02d" % ((int(parts[0]) + k) % 24)] + parts[1:])
        if cand not in consts and cand not in real:
            return cand
    return None


def targets():
    """git ls-files ∪ 未追蹤但沒被 gitignore 的檔（**包含這支自己**）。"""
    seen, out = set(), []
    for args in (["git", "ls-files"],
                 ["git", "ls-files", "--others", "--exclude-standard"]):
        res = subprocess.run(args, cwd=ROOT, capture_output=True, text=True,
                             encoding="utf-8")
        for rel in res.stdout.splitlines():
            if rel and rel not in seen:
                seen.add(rel)
                out.append(rel)
    return out


def main():
    recs = load_records()
    if not recs:
        print("real_trades/ 是空的 —— 沒有東西可以比對，掃描沒有意義")
        return 2
    subs = note_subs(recs)
    consts = product_consts()
    print("比對來源：%d 筆真實交易、%d 個欄位值、%d 種心得節錄"
          % (len(recs), sum(len(r) for _, r in recs), len(subs)))
    # ⚠️ 把「讀到哪些產品常數」印出來：這些值在時間欄會被降到「要旁證」那一層，
    #    而且**任何欄位只要值等於它們就不能當旁證**，
    #    清單變長 ＝ 有人在產品程式裡多開了一個常數，**那是要看的一件事**。
    times = {v: n for v, n in consts.items() if _TIME_LIKE.match(v)}
    nums = {v: n for v, n in consts.items() if v not in times}
    print("產品常數（從 %s 的模組層級讀出來）：時間 %d 個、數值 %d 個"
          % ("／".join(CONST_SOURCES), len(times), len(nums)))
    print("  時間：%s" % "、".join("%s=%s" % (n, v) for v, n in
                                   sorted(times.items(), key=lambda kv: kv[1])))
    print("  數值：%s" % "、".join("%s=%s" % (n, v) for v, n in
                                   sorted(nums.items(), key=lambda kv: kv[1])))
    print("\n=== 尺的自證（掃描器自己抓不抓得到）===")
    if not self_test(recs, subs, consts):
        print("\n⛔ 掃描器壞了，這次的結果不可信")
        return 3

    files = targets()
    me = str(pathlib.Path(__file__).resolve().relative_to(ROOT)).replace("\\", "/")
    print("\n=== 掃描 %d 個檔（git ls-files ∪ 未追蹤）===" % len(files))
    # 尺的自證之二：它自己一定要在被掃的清單裡（第一版就是漏了自己）
    if me not in files:
        print("  ⛔ 尺壞了：掃描清單裡沒有這支自己（%s）" % me)
        return 3
    print("  （已確認清單含這支自己：%s）" % me)
    total, scanned = 0, 0
    for rel in files:
        if rel.startswith(SKIP_DIRS) or rel.endswith(SKIP_SUFFIX):
            continue
        try:
            lines = (ROOT / rel).read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue                      # 二進位／讀不到的跳過
        scanned += 1
        for ln, f, v, why in scan_text(lines, recs, subs, consts):
            print("  ⛔ %s:%d  %s=%s  ← %s" % (rel, ln, f, v, why))
            total += 1
    print("\n實際掃了 %d 個文字檔，命中 %d 處" % (scanned, total))
    if total:
        print("\n⛔ 上面那些值跟 Benson 的真實交易一模一樣，而這個 repo 是公開的。")
        print("   ⚠️ 換的時候不是只換價格：**時間、點數、心得原文任何一項都算**。")
        print("   ⚠️ 就算是巧合（合成資料剛好撞號）也請把數字改掉 ——")
        print("      看的人分不出巧合與外洩，那就不該留在公開 repo 裡。")
        return 1
    print("乾淨。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
