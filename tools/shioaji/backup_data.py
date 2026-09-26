# -*- coding: utf-8 -*-
"""
【資料備份】研究資料＋面板本機資料 ⇒ 私人 GitHub repo `xd1104/trade-data`（2026-09-26 Benson 交辦）。

為什麼：程式碼在 GitHub，但策略要用的歷史（快攻 40 天、開箱 20 天、夜盤 40 晚）、真單紀錄、模擬紀錄、
逐筆行情、整個 tick-research 都只在這台電腦 —— 電腦壞了，策略會因為「歷史不夠」停擺、真單紀錄也沒了。

怎麼跑：面板每天 05:30（夜盤 04:58 已平、日盤 08:45 才開 ⇒ 一定沒有部位、網路空著）**另開這支**、最低優先權、
⛔ 面板自己不做上傳（停損每秒看 4 次報價，不可以被幾百 MB 的上傳拖住）。也可以手動跑：
    .venv\\Scripts\\python.exe tools\\shioaji\\backup_data.py
結果寫在 `backup/status.json`（面板【帳戶】與手機監控讀它）。

⛔⛔ **白名單**：只備份下面列出來的東西。金鑰（.env、*.pfx、MONITOR_KEY.json、VAPID_PRIVATE.txt）、
   開關檔（AUTO_ORDERS_ON、NIGHT_ORDERS_ON、REAL_ORDERS_ON、RISK_OVERRIDE）、記錄檔一律不碰；
   上傳前再掃一次檔名與內容（`_secret_hits()`），有一個可疑就整批不推。
⭐ 大 csv（> SPLIT_MB）切成「每月一檔」再放（GitHub 單檔上限 100 MB；整份每天改寫會讓 repo 越來越肥）。
   還原：`restore_data.py` 會把每月的檔接回原本那一個。
⛔ 只新增／覆蓋，**不刪**（本機刪掉的檔，備份裡照留 —— 備份就是要防手滑）。
"""
import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).resolve().parent                       # trade-log/tools/shioaji
ROOT = HERE.parent.parent.parent                             # Desktop/claude
DEST = ROOT / "trade-data"                                   # 私人 repo 的本機工作目錄
REMOTE = "https://github.com/xd1104/trade-data.git"
STATUS = HERE / "backup" / "status.json"                     # ⛔ gitignore
LOCK = HERE / "backup" / "running.lock"
SPLIT_MB = 20
PUSH_BATCH_MB = 700                                          # 一次 push 別太大（GitHub 單次上限 2 GB）

# ── 白名單 ────────────────────────────────────────────────────────────
PANEL_ITEMS = [   # trade-log/tools/shioaji 底下、只在本機的資料（程式碼本身在 trade-log repo）
    "sim_lanes", "fast_hist.jsonl", "orb_hist.jsonl", "trend_ctx.json", "tsm_ctx.json", "usml_ctx.json",
    "real_trades", "real_orders", "autofire", "nightfire", "equity", "analyst", "riskcap", "usage",
    "tick_hist", "tick_logs", "tmf_1min.csv", "txf_1min.csv", "index_1min.csv", "intraday.csv",
    "us_bars", "us_spy", "stats.json", "calibration.json", "my_trades.json", "practice_trades",
    "replay_log", "daily_reports", "morning_logs", "autotest", "sim_orders", "review_cache.json",
]
PANEL_SKIP_DIRS = {"cache", "__pycache__"}
RESEARCH = ROOT / "tick-research"
RESEARCH_SKIP_DIRS = {"__pycache__", ".git"}
RESEARCH_SKIP_EXT = {".log", ".pyc"}
# ⛔ 不管在哪裡、長什麼名字，這些一律不備份
SECRET_NAME = re.compile(r"(^\.env|\.pfx$|\.p12$|\.pem$|token|secret|MONITOR_KEY|VAPID_PRIVATE|password|"
                         r"^AUTO_ORDERS_ON|^NIGHT_ORDERS_ON|^REAL_ORDERS_ON|^RISK_OVERRIDE)", re.I)
SECRET_TEXT = re.compile(rb"(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
                         rb"SHIOAJI_SECRET_KEY\s*=\s*\S{8,}|SHIOAJI_API_KEY\s*=\s*\S{8,})")


def log(msg):
    print("[備份] " + msg, flush=True)


def _sha(b):
    return hashlib.sha1(b).hexdigest()


def _same(src, dst):
    try:
        s, d = src.stat(), dst.stat()
    except FileNotFoundError:
        return False
    if s.st_size != d.st_size:
        return False
    if abs(s.st_mtime - d.st_mtime) < 2:
        return True
    return _sha(src.read_bytes()) == _sha(dst.read_bytes())


def _month_key(first):
    first = first.strip()
    if re.fullmatch(r"\d{12,}", first):                       # epoch 毫秒／奈秒
        v = int(first)
        v = v / 1e9 if v > 1e15 else v / 1e3
        return datetime.fromtimestamp(v, tz=timezone.utc).strftime("%Y-%m")
    m = re.match(r"(\d{4})-(\d{2})", first)
    return "%s-%s" % (m.group(1), m.group(2)) if m else "other"


def split_csv(src, dst_dir):
    """大 csv ⇒ dst_dir/YYYY-MM.csv（每檔都帶表頭）。⇒ 這次改了幾個月份檔。"""
    parts = {}
    with open(src, encoding="utf-8", newline="") as f:
        rd = csv.reader(f)
        head = next(rd, None)
        if head is None:
            return 0
        for row in rd:
            if not row:
                continue
            parts.setdefault(_month_key(row[0]), []).append(row)
    dst_dir.mkdir(parents=True, exist_ok=True)
    changed = 0
    for k, rows in parts.items():
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(head)
        w.writerows(rows)
        data = buf.getvalue().encode("utf-8")
        p = dst_dir / (k + ".csv")
        if p.exists() and p.read_bytes() == data:
            continue
        p.write_bytes(data)
        changed += 1
    (dst_dir / "_SPLIT.json").write_text(json.dumps({"source": src.name, "header": head, "months": sorted(parts)},
                                                    ensure_ascii=False), encoding="utf-8")
    return changed


def mirror_file(src, dst):
    """⇒ 這次動了幾個檔。大 csv 走 split_csv。"""
    if SECRET_NAME.search(src.name):
        return 0
    if src.suffix.lower() == ".csv" and src.stat().st_size > SPLIT_MB * 1e6:
        return split_csv(src, dst.with_name(dst.name + ".parts"))
    if src.stat().st_size > 95e6:
        log("⚠️ 跳過超過 95 MB 的非 csv 檔：%s" % src)
        return 0
    if _same(src, dst):
        return 0
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return 1


def mirror_tree(src_dir, dst_dir, skip_dirs, skip_ext=()):
    n = 0
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
        for fn in files:
            p = Path(root) / fn
            if p.suffix.lower() in skip_ext:
                continue
            n += mirror_file(p, dst_dir / p.relative_to(src_dir))
    return n


def collect():
    n = 0
    for item in PANEL_ITEMS:
        p = HERE / item
        if not p.exists():
            continue
        if p.is_dir():
            n += mirror_tree(p, DEST / "panel" / item, PANEL_SKIP_DIRS)
        else:
            n += mirror_file(p, DEST / "panel" / item)
    if RESEARCH.exists():
        n += mirror_tree(RESEARCH, DEST / "research", RESEARCH_SKIP_DIRS, RESEARCH_SKIP_EXT)
    return n


def git(*args, check=True, timeout=3600):
    r = subprocess.run(["git"] + list(args), cwd=str(DEST), capture_output=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError("git %s：%s" % (args[0], (r.stderr or b"").decode("utf-8", "replace")[-300:]))
    return r.stdout.decode("utf-8", "replace")


def ensure_repo():
    if not (DEST / ".git").exists():
        DEST.mkdir(parents=True, exist_ok=True)
        git("init", "-q", "-b", "main")
        git("remote", "add", "origin", REMOTE)
        (DEST / ".gitattributes").write_text("* -text\n", encoding="utf-8")   # ⛔ 不轉換換行（資料要一模一樣）


def _secret_hits(paths):
    hits = []
    for rel in paths:
        p = DEST / rel
        if SECRET_NAME.search(p.name):
            hits.append(rel + "（檔名）")
            continue
        try:
            if p.stat().st_size < 5e6 and p.suffix.lower() in (".json", ".jsonl", ".txt", ".md", ".py", ".csv", ".env", ""):
                if SECRET_TEXT.search(p.read_bytes()):
                    hits.append(rel + "（內容）")
        except OSError:
            pass
    return hits


def commit_and_push():
    """分批 add／commit／push（每批 ≤ PUSH_BATCH_MB）。⇒ (推了幾批, 幾個檔)。"""
    out = git("status", "--porcelain", "-uall")
    todo = [ln[3:].strip().strip('"') for ln in out.splitlines() if ln.strip()]
    if not todo:
        return 0, 0
    hits = _secret_hits(todo)
    if hits:
        raise RuntimeError("⛔ 疑似機密，整批不推：" + "、".join(hits[:5]))
    batches, cur, size = [], [], 0.0
    for rel in sorted(todo):
        s = (DEST / rel).stat().st_size if (DEST / rel).exists() else 0
        if cur and size + s > PUSH_BATCH_MB * 1e6:
            batches.append(cur); cur, size = [], 0.0
        cur.append(rel); size += s
    if cur:
        batches.append(cur)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    for i, b in enumerate(batches, 1):
        for j in range(0, len(b), 200):
            git("add", "--", *b[j:j + 200])
        git("commit", "-q", "-m", "備份 %s（%d/%d，%d 個檔）" % (stamp, i, len(batches), len(b)))
        git("push", "-q", "origin", "main")
        log("推上第 %d/%d 批（%d 個檔）" % (i, len(batches), len(b)))
    return len(batches), len(todo)


def write_status(**kw):
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    old = {}
    try:
        old = json.loads(STATUS.read_text(encoding="utf-8"))
    except Exception:
        pass
    st = dict(old, **kw)
    STATUS.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    if LOCK.exists() and time.time() - LOCK.stat().st_mtime < 6 * 3600:
        log("上一次還在跑（或 6 小時內中斷過），這次不跑")
        return 0
    LOCK.write_text(str(os.getpid()))
    t0 = time.time()
    write_status(started=datetime.now().isoformat(timespec="seconds"), running=True)
    try:
        ensure_repo()
        n = collect()
        b, files = commit_and_push()
        write_status(running=False, ok=True, at=datetime.now().isoformat(timespec="seconds"), err=None,
                     changed=n, pushed_files=files, batches=b, secs=round(time.time() - t0))
        log("完成：本機有變動 %d 個、推上 %d 個檔（%d 批），花 %d 秒" % (n, files, b, time.time() - t0))
        return 0
    except Exception as e:
        write_status(running=False, ok=False, at=datetime.now().isoformat(timespec="seconds"), err=str(e)[:300])
        log("⛔ 失敗：%s" % str(e)[:300])
        return 1
    finally:
        try:
            LOCK.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
