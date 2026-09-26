# -*- coding: utf-8 -*-
"""
【資料還原】換新電腦（或資料被刪）時，把私人 repo `xd1104/trade-data` 還原回原位（2026-09-26）。
配 `backup_data.py`（備份）。完整步驟看 trade-data 根目錄的 RESTORE.md。

  py -3.11 tools/shioaji/restore_data.py --dry     ⇒ 只列出會還原什麼、會不會蓋掉現有檔
  py -3.11 tools/shioaji/restore_data.py           ⇒ 真的還原（⛔ 只補「本機沒有」的檔；已經有的一律不蓋）
  py -3.11 tools/shioaji/restore_data.py --force   ⇒ 本機已有的也用備份蓋過去（⛔ 先確定本機那份是壞的再用）

每月切開的大 csv（`*.csv.parts/`）會照月份接回原本那一個檔。
⛔ 不還原金鑰與開關檔（備份裡本來就沒有）：.env、Sinopac.pfx、MONITOR_KEY.json、VAPID_PRIVATE.txt 要另外放回，
   AUTO_ORDERS_ON／NIGHT_ORDERS_ON／REAL_ORDERS_ON 由他自己在面板上重新打開。
"""
import csv
import json
import shutil
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent.parent
SRC = ROOT / "trade-data"
MAP = [(SRC / "panel", HERE), (SRC / "research", ROOT / "tick-research")]


def join_parts(parts_dir, out, dry, force):
    meta = json.loads((parts_dir / "_SPLIT.json").read_text(encoding="utf-8"))
    if out.exists() and not force:
        return "已存在，不蓋：%s" % out
    if dry:
        return "會接回：%s（%d 個月）" % (out, len(meta["months"]))
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="") as w:
        wr = csv.writer(w, lineterminator="\n")
        wr.writerow(meta["header"])
        for m in meta["months"]:
            with open(parts_dir / (m + ".csv"), encoding="utf-8", newline="") as f:
                rd = csv.reader(f)
                next(rd, None)
                wr.writerows(rd)
    return "接回：%s" % out


def main():
    dry, force = "--dry" in sys.argv, "--force" in sys.argv
    if not SRC.exists():
        print("找不到 %s —— 先 git clone https://github.com/xd1104/trade-data.git 到那裡" % SRC)
        return 2
    n_new = n_skip = 0
    for src_root, dst_root in MAP:
        if not src_root.exists():
            continue
        for p in sorted(src_root.rglob("*")):
            rel = p.relative_to(src_root)
            if p.is_dir() and p.name.endswith(".parts") and (p / "_SPLIT.json").exists():
                print(join_parts(p, dst_root / rel.parent / p.name[:-len(".parts")], dry, force))
                continue
            if not p.is_file() or any(x.endswith(".parts") for x in rel.parts[:-1]):
                continue
            dst = dst_root / rel
            if dst.exists() and not force:
                n_skip += 1
                continue
            n_new += 1
            if not dry:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dst)
    print("%s %d 個檔；本機已經有、沒動 %d 個" % ("會還原" if dry else "還原了", n_new, n_skip))
    return 0


if __name__ == "__main__":
    sys.exit(main())
