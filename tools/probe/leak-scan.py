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

⚠️ 這支**自己會先驗尺**：拿真實值合成幾行測試字串，抓不到就直接失敗並非零離開
   （掃描器壞掉時最危險的症狀不是報錯，是它安靜地開始全綠）。
"""
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


def scan_text(lines, recs, subs):
    """回傳 [(行號, 欄位, 值, 說明)]。"""
    hits = []
    for i, line in enumerate(lines):
        for src, r in recs:
            for f in SOLO:
                if f in r and _has(f, r[f], line):
                    hits.append((i + 1, f, r[f][:24], src))
            for f in CORROB:
                if f not in r or not _has(f, r[f], line):
                    continue
                # 旁證：同一筆的**另一個**欄位要出現在 ±2 行之內（一筆測資常跨兩三行）
                window = "\n".join(lines[max(0, i - 2):i + 3])
                other = [g for g in r if g != f and _has(g, r[g], window)]
                if other:
                    hits.append((i + 1, f, r[f], "%s（旁證 %s）" % (src, "/".join(other))))
        for s, src in subs.items():
            if s in line:
                hits.append((i + 1, "note(節錄)", s, src))
    return hits


def self_test(recs, subs):
    """尺的自證：合成幾行一定該被抓到的字串，抓不到就是掃描器壞了。
    配負控組確認雜訊規則真的有在收斂（不然它會變成永遠紅，跟永遠綠一樣沒用）。"""
    rec = next((r for _, r in recs
                if "entry_time" in r and "entry" in r and "points" in r), None)
    note = next((r["note"] for _, r in recs if r.get("note")), None)
    if rec is None or note is None:
        print("  尺壞了：real_trades 裡找不到欄位齊全的紀錄，無法自證")
        return False
    cases = [
        ("價格單獨出現（第一版的死角）", ["# 舉例：%s" % rec["entry"]], True),
        ("完整時間單獨一行", ['x = "%s"' % rec["entry_time"]], True),
        ("心得原文", ['s = "%s"' % note], True),
        ("心得節錄 %d 字" % NOTE_SUB, ["# 他寫「%s」" % note[:NOTE_SUB]], True),
        ("點數＋旁證跨兩行", ["entry = %s" % rec["entry"],
                              "pts = %s" % rec["points"]], True),
        ("（負控組）只有點數、沒有旁證", ["FEE = %s" % rec["points"]], False),
        ("（負控組）長得像但不是那個數", ["x = 1%s0" % rec["entry"]], False),
        ("（負控組）心得節錄只有 4 字", ["# %s" % note[:4]], False),
    ]
    ok = True
    for name, lines, want in cases:
        got = bool(scan_text(lines, recs, subs))
        if got != want:
            ok = False
        print("  %s %s → %s（期待 %s）"
              % ("OK  " if got == want else "壞了", name,
                 "抓到" if got else "沒抓到", "抓到" if want else "不該抓到"))
    return ok


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
    print("比對來源：%d 筆真實交易、%d 個欄位值、%d 種心得節錄"
          % (len(recs), sum(len(r) for _, r in recs), len(subs)))
    print("\n=== 尺的自證（掃描器自己抓不抓得到）===")
    if not self_test(recs, subs):
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
        for ln, f, v, why in scan_text(lines, recs, subs):
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
