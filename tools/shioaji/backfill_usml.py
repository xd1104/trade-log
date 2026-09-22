# -*- coding: utf-8 -*-
"""
把【模擬】「美股開盤模型」那一條**往回補**（2026-09-22 加）。

    python backfill_usml.py [--from 2025-01-01] [--dry]

⛔ **不連永豐**：台指 1 分 K 只讀本機 `tmf_1min.csv`；SPY 走 `spy_feed`（Alpaca，唯讀行情）。
⛔ 規則走 `sim_lanes.usml_eval` 正本（⛔ 這支不自己算一份）。
⚠️ SPY 的 **IEX** 歷史大約從 2025-01 才有 ⇒ 預設從那時開始補。
⚠️ 已經有定論的日子不重算（`append_row` 自己會擋重複，這裡也先跳過）。
"""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sim_lanes as S  # noqa: E402
import spy_feed  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="d0", default="2025-01-01")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    d0 = date.fromisoformat(a.d0)

    px = pd.read_csv(S.MIN1_CSV)
    px["ts"] = pd.to_datetime(px["ts"])
    last = px["ts"].dt.date.max()
    print("本機 1 分 K：%d 根，%s ~ %s" % (len(px), px["ts"].dt.date.min(), last))

    rows, _st = S.read_rows()
    have = {k[1] for k in rows if k[0] == "usml"}
    ctx = S._usml_ctx_read()

    done = skip = pend = 0
    E = d0
    while E <= last:
        es = str(E)
        g = px[(px["ts"].dt.date == E) | (px["ts"].dt.date == E + timedelta(days=1))]
        if len(g) >= S.NIGHT_MIN_BARS:
            dc = S._usml_day_close(g, E)
            if dc:
                ctx["day_close"][es] = round(dc, 1)
            mm, H, L, C = S.night_frame(g, E)
            if len(mm) >= S.NIGHT_MIN_BARS:
                ctx["night_range"][es] = round(float((C.max() - C.min()) / C.min() * 100), 4)
            if es in have:
                skip += 1
            else:
                spy = spy_feed.evening(E)
                res = S.usml_eval(E, g, spy, ctx)
                if res.get("pending"):
                    pend += 1
                elif a.dry:
                    done += 1
                elif S.append_row(dict(res, calc="backfill")):
                    done += 1
        E += timedelta(days=1)
    if not a.dry:
        S._usml_ctx_write(ctx)
    print("落地 %d 列、已有跳過 %d、算不出來 %d%s" % (done, skip, pend,
                                           "（--dry：沒有真的寫檔）" if a.dry else ""))
    if not a.dry and done:
        rows, _ = S.read_rows()
        v = [r["points"] for (ln, _d), r in rows.items()
             if ln == "usml" and isinstance(r.get("points"), (int, float))]
        if v:
            mths = len({d[:7] for (ln, d) in rows if ln == "usml"})
            print("現在共 %d 筆、合計 %+.0f 點、每月 %+.0f、勝率 %.0f%%"
                  % (len(v), sum(v), sum(v) / max(mths, 1),
                     100 * sum(1 for x in v if x > 0) / len(v)))


if __name__ == "__main__":
    main()
