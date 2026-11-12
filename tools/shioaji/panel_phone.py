# -*- coding: utf-8 -*-
"""
練習紀錄的手機同步：心得從手機抓回來（pull_from_phone／poll_phone）、練習紀錄推上雲端（sync_to_cloud）。
2026-11-12 健檢整理第 5 批，從 live_panel.py 原樣搬出來。

⛔ 這支**不下單、不碰真實交易**：只碰練習紀錄（practice_trades/）的 note／note_at，和 data/practice.json。
   真實交易不上傳（repo 是公開的，Benson 定的）。
⛔ 不准 import live_panel（面板是用 `python live_panel.py` 跑的 ⇒ 再 import 一次會變成兩份狀態）。
   練習紀錄放在面板那邊 ⇒ 由 live_panel 在載入時接上：trade_dir、load_today_trades、all_practice_trades。
"""
import json
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
REPO = HERE.parent.parent                       # trade-log/ 根目錄
SYNC_FILE = REPO / "data" / "practice.json"     # 練習紀錄的雲端同步檔


def trade_dir():
    """live_panel 載入時換掉（⇒ 它的 TRADE_DIR，測試導走的話跟著走）。⛔ 沒接上 ⇒ 丟錯、什麼都不碰。"""
    raise RuntimeError("panel_phone.trade_dir 還沒接上")


def load_today_trades():
    raise RuntimeError("panel_phone.load_today_trades 還沒接上")


def all_practice_trades():
    raise RuntimeError("panel_phone.all_practice_trades 還沒接上")


PHONE_URL = ("https://raw.githubusercontent.com/xd1104/trade-log/"
             "main/data/phone.json")
PHONE_EVERY = 180          # 秒。GitHub raw 本來就有 CDN 快取，抓太密沒有意義


def _note_wins(inc_note, inc_at, cur_note, cur_at):
    """
    手機那筆的心得該不該蓋掉面板這筆。跟手機端 noteWins() 是同一套規則，
    兩邊不一致就會來回互蓋。

    有時間戳的贏；兩邊都有就比時間；兩邊都沒有時**只補空的、不覆蓋**。
    """
    a = (inc_note or "").strip()
    b = (cur_note or "").strip()
    if a == b:
        return False
    if inc_at and not cur_at:
        return True
    if cur_at and not inc_at:
        return False
    if inc_at and cur_at:
        return inc_at > cur_at
    return (not b) and bool(a)


def pull_from_phone():
    """
    把手機寫的心得抓回來。手機沒辦法直接連到這台電腦（常常不同網路），
    所以走跟 sync_to_cloud() 對稱的路：手機用鑰匙圈的金鑰把 data/phone.json
    寫進 repo，這裡讀那個檔。

    【只碰 note / note_at】不會新增、刪除或改動任何一筆交易 ——
    練習紀錄的真相在這台電腦（面板才知道成交價與 _mfe 那些欄位）。
    """
    import urllib.request
    url = PHONE_URL + "?t=" + str(int(time.time()))
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return 0, f"讀不到手機的檔案：{str(e)[:80]}"

    # 依日期分組。【不能把 time 放進配對條件】——手機上編輯過的紀錄 time 是空的
    # （手機表單以前會把它弄丟），而「編輯過」跟「有心得」剛好是同一批，
    # 一旦把 time 算進去，真正要同步的那幾筆 100% 對不上（2026-08-25 踩到）。
    by_day = {}
    for t in payload.get("trades") or []:
        try:
            by_day.setdefault(str(t["date"]), []).append(
                (round(float(t["entry"])), t.get("note", ""), t.get("note_at", "")))
        except Exception:
            continue
    if not by_day:
        return 0, "手機那邊還沒有紀錄"

    def match(day, entry, same_day_count):
        """先比進場價；對不上而那天兩邊都只有一筆，就用日期認（他在手機上改過價）。"""
        cands = by_day.get(day) or []
        for e, note, at in cands:
            if e == entry:
                return note, at
        if len(cands) == 1 and same_day_count == 1:
            return cands[0][1], cands[0][2]
        return None

    changed = 0
    tdir = trade_dir()
    if tdir.exists():
        for f in sorted(tdir.glob("*.json")):
            try:
                recs = json.loads(f.read_text(encoding="utf-8")) or []
            except Exception:
                continue
            touched = False
            for r in recs:
                try:
                    day, entry = str(r.get("date")), round(float(r.get("entry")))
                except Exception:
                    continue
                hit = match(day, entry, sum(1 for x in recs if x.get("date") == day))
                if hit is None:
                    continue
                note, at = hit
                if _note_wins(note, at, r.get("note"), r.get("note_at")):
                    r["note"] = note
                    r["note_at"] = at or datetime.now().isoformat(timespec="seconds")
                    touched = True
                    changed += 1
            if touched:
                f.write_text(json.dumps(recs, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    if changed:
        # 今天那份也在記憶體裡，不重讀的話面板畫面還是舊的
        load_today_trades()
        sync_to_cloud()
    return changed, (f"從手機補回 {changed} 筆心得" if changed else "沒有比較新的心得")


def poll_phone():
    """背景執行緒：定期把手機那邊的心得抓回來。抓不到就下次再說，不吵人。"""
    while True:
        try:
            n, msg = pull_from_phone()
            if n:
                print(f"[同步] {msg}")
        except Exception as e:
            print(f"[同步] 讀手機紀錄失敗：{str(e)[:100]}")
        time.sleep(PHONE_EVERY)


def sync_to_cloud():
    """
    把練習紀錄寫進 data/practice.json 並推上 GitHub。

    手機常不在同一個網路，所以拿 GitHub Pages 當中間人：
    面板推上去 → 手機開 App 時自動抓下來合併。

    【只同步練習（sim）】真實交易不上傳 —— repo 是公開的，
    那是 Benson 明確的決定。

    推送失敗（沒網路、git 沒設定）不會影響面板運作，只留訊息。
    """
    trades = all_practice_trades()
    if not trades:
        return False, "沒有練習紀錄可同步"
    SYNC_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated": datetime.now().isoformat(timespec="seconds"),
               "count": len(trades), "trades": trades}
    SYNC_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    import subprocess
    def git(*args):
        return subprocess.run(["git"] + list(args), cwd=str(REPO), capture_output=True,
                              text=True, timeout=60)
    try:
        git("add", str(SYNC_FILE.relative_to(REPO)).replace("\\", "/"))
        st = git("status", "--porcelain", "--", "data/practice.json")
        if not st.stdout.strip():
            return True, f"已是最新（{len(trades)} 筆）"
        git("commit", "-m", f"chore: 同步練習紀錄（{len(trades)} 筆）")
        r = git("push", "origin", "HEAD")
        if r.returncode != 0:
            # 手機也會往同一個 repo 寫（data/phone.json），被搶先就會是 non-fast-forward。
            # --autostash：工作目錄不乾淨時照樣能 rebase，事後原樣放回去。
            git("pull", "--rebase", "--autostash", "origin", "main")
            r = git("push", "origin", "HEAD")
            if r.returncode != 0:
                return False, "推送失敗：" + (r.stderr or "")[-120:]
        return True, f"已同步 {len(trades)} 筆到雲端"
    except Exception as e:
        return False, f"同步失敗：{str(e)[:120]}"
