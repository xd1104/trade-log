# -*- coding: utf-8 -*-
"""
【自動下單】「開盤快才做」的開盤走幅歷史 —— **種子**（2026-09-15 加）。

面板每天 09:03:30 之後會自己把當天那一列寫進 `fast_hist.jsonl`（auto_fire._hist_step），
但**第一天上線時檔案是空的** ⇒ 過去 40 天不夠 20 天 ⇒ 一律 `no_hist` 不送。
這支從研究用的逐筆資料（tick-research 的 `*.csv.gz`）把最近 60 個交易日補進去。

    python build_fast_hist.py DIR [DIR...] --out 路徑 [--days 60] [--before YYYY-MM-DD]

  DIR       放 `YYYY-MM-DD.csv.gz` 的資料夾（或它的上一層，底下有 `ticks/`）
  --out     輸出的 jsonl（⛔ 只 append；檔案裡已經有的日子不重寫）
  --days    最多寫幾個最近的交易日（預設 60）
  --before  只收這一天**之前**的日子（預設今天 ⇒ 不會把今天那一份半天的資料寫進去；
            今天那一列留給面板 09:03:30 自己寫）

一天一列：`{"date","ref","px","move_pct","ref_src":"seed"}` —— 跟面板寫的同一種格式。
  ref ＝ 08:45:00 起、**09:00:00.000（含）以前最後一筆**成交
  px  ＝ **09:03:30.000（含）以前最後一筆**成交
  （研究 `hypotheses.last_before()` 的定義；研究的 `load_day` 只留 08:45~13:45 的逐筆）
  ⛔ 兩個任一拿不到 ⇒ 那天跳過並印出來（⛔ 不猜、不補）。

⛔ 這支**不連永豐**、不碰任何下單路徑；只讀 csv.gz、只寫 --out。
⛔ 部署時由 PM 拿研究資料跑；測試一律寫到暫存路徑。
"""
import argparse
import json
import pathlib
import sys
from datetime import date

import numpy as np
import pandas as pd

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import auto_fire as AF       # noqa: E402  ⛔ move_pct 的正本在這裡（不另寫一份）

T0845 = (8 * 3600 + 45 * 60) * 1000
T0900 = 9 * 3600 * 1000
T0903_30 = (9 * 3600 + 3 * 60 + 30) * 1000
T1345 = (13 * 3600 + 45 * 60) * 1000


def load_ms_close(f):
    """
    讀一天的逐筆 ⇒ (當天毫秒時間陣列（排序過）, 成交價陣列)；空的回 None。
    ts 有兩種：奈秒整數（ticks2y）與字串（ticks2026）—— 跟研究的 `search3.load_day` 同一套讀法。
    """
    df = pd.read_csv(f, compression="gzip", usecols=["ts", "close"])
    if df.empty:
        return None
    if pd.api.types.is_numeric_dtype(df["ts"]):
        ts = pd.to_datetime(df["ts"])
    else:
        ts = pd.to_datetime(df["ts"], format="ISO8601")
    t = ((ts.dt.hour * 3600 + ts.dt.minute * 60 + ts.dt.second) * 1000
         + ts.dt.microsecond // 1000).to_numpy(np.int64)
    o = np.argsort(t, kind="stable")
    t, p = t[o], df["close"].to_numpy(float)[o]
    keep = (t >= T0845) & (t < T1345)
    if not keep.any():
        return None
    return t[keep], p[keep]


def last_before(t, tms):
    """tms（含）以前最後一筆的索引；沒有回 -1（研究的 last_before）。"""
    return int(np.searchsorted(t, tms, side="right")) - 1


def day_row(f):
    """一天 ⇒ (row 或 None, 跳過的原因)。"""
    got = load_ms_close(f)
    if got is None:
        return None, "沒有 08:45~13:45 的逐筆"
    t, p = got
    i0, i1 = last_before(t, T0900), last_before(t, T0903_30)
    if i0 < 0:
        return None, "09:00 以前沒有成交"
    # ⛔ px 那一筆必須落在 09:00:00.000（含）之後：09:00~09:03:30 之間一筆成交都沒有的話，
    #    last_before(09:03:30) 會退回 09:00 以前那一筆 ⇒ px == ref ⇒ 走幅 0% —— 那是一筆假的
    #    「完全沒動」，寫進去會把以後 40 天的門檻拉低。研究（benson_rule.main）也是把
    #    `first_at(D, 09:00) < 0` 的日子整天丟掉，這裡是同一個意思。⛔ 不猜、不補。
    if i1 < 0 or t[i1] < T0900:
        return None, "09:00~09:03:30 之間沒有成交"
    ref, px = float(p[i0]), float(p[i1])
    mv = AF.move_pct(px, ref)
    if mv is None:
        return None, "參考價不合理（%r）" % ref
    return {"date": pathlib.Path(f).name[:10], "ref": ref, "px": px,
            "move_pct": round(mv, 6), "ref_src": "seed"}, None


def tick_files(dirs):
    """每個 DIR 底下的 `YYYY-MM-DD.csv.gz`（DIR 本身或 DIR/ticks）。同一天只認第一個 DIR 的。"""
    out, dup = {}, 0
    for d in dirs:
        root = pathlib.Path(d)
        if (root / "ticks").is_dir():
            root = root / "ticks"
        for f in sorted(root.glob("*.csv.gz")):
            s = f.name[:10]
            if not AF._DATE_RE.match(s):
                continue
            if s in out:
                dup += 1
                continue
            out[s] = f
    return out, dup


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("dirs", nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--before", default=str(date.today()))
    a = ap.parse_args(argv)
    if not AF._DATE_RE.match(a.before):
        print("--before 要是 YYYY-MM-DD")
        return 2
    files, dup = tick_files(a.dirs)
    out = pathlib.Path(a.out)
    have = set()
    if out.exists():
        rows, bad, hdup = AF.hist_read(out)
        have = {r["date"] for r in rows}
        if bad or hdup:
            print("⚠️ %s 原本就有 %d 列讀不出來、%d 列重複（不動它們）" % (out, bad, hdup))
    wrote, skipped, kept = [], [], 0
    for s in sorted((s for s in files if s < a.before), reverse=True):
        if len(wrote) + kept >= a.days:
            break
        if s in have:
            kept += 1                     # ⛔ 已經有的日子不重寫（只 append 的檔）
            continue
        row, why = day_row(files[s])
        if row is None:
            skipped.append((s, why))
            continue
        wrote.append(row)
    wrote.sort(key=lambda r: r["date"])
    if wrote:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8") as fh:        # ⛔ 一定是 append
            for r in wrote:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("寫進 %d 天（%s ~ %s）、檔案裡原本就有 %d 天、跳過 %d 天、重複的來源檔 %d 個 ⇒ %s"
          % (len(wrote), wrote[0]["date"] if wrote else "—", wrote[-1]["date"] if wrote else "—",
             kept, len(skipped), dup, out))
    for s, why in skipped:
        print("  跳過 %s：%s" % (s, why))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
