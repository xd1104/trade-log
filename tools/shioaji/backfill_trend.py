# -*- coding: utf-8 -*-
"""
把【模擬】「夜盤跟勢」那一條**往回補**（2026-09-22 晚加）。

    python backfill_trend.py [--from 2024-08-01] [--dry]

⛔ **不連永豐、也不連任何外部行情**：只讀本機 `tmf_1min.csv`（這一條只看台指自己）。
⛔ 規則走 `sim_lanes.trend_eval`／`trend_sig` 正本（⛔ 這支不自己算一份）。
⚠️ 要先有 20 晚歷史才會開始做 ⇒ 開頭那一個多月一律「歷史不夠」，那是正常的。
⚠️ 已經有定論的晚上不重算（`append_row` 自己會擋重複），但歷史照樣記進 `trend_ctx.json`。
"""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sim_lanes as S  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="d0", default="2024-08-01")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    d0 = date.fromisoformat(a.d0)

    px = pd.read_csv(S.MIN1_CSV)
    px["ts"] = pd.to_datetime(px["ts"])
    px["d"] = px["ts"].dt.date
    last = px["d"].max()
    print("本機 1 分 K：%d 根，%s ~ %s" % (len(px), px["d"].min(), last))

    rows, _st = S.read_rows()
    have = {k[1] for k in rows if k[0] == "trend"}
    ctx = {"sig": {}} if a.dry else S._trend_ctx_read()

    done = skip = pend = 0
    why = {}
    E = d0
    while E < last:
        es = str(E)
        if E.weekday() > 4:
            E += timedelta(days=1)
            continue
        g = px[(px["d"] == E) | (px["d"] == E + timedelta(days=1))]
        if es in have:
            skip += 1
        else:
            res = S.trend_eval(E, g, ctx)
            if res.get("pending"):
                pend += 1
                why[res["why"]] = why.get(res["why"], 0) + 1
            else:
                why[res.get("why")] = why.get(res.get("why"), 0) + 1
                if a.dry or S.append_row(dict(res, calc="backfill")):
                    done += 1
                    if a.dry and res["decision"] != "不做":
                        rows[("trend", es)] = res
        sg = S.trend_sig(E, g)
        if sg is not None:
            ctx["sig"][es] = sg
        E += timedelta(days=1)
    if not a.dry:
        S._trend_ctx_write(ctx)
    print("落地 %d 列、已有跳過 %d、算不出來 %d%s" % (done, skip, pend,
                                           "（--dry：沒有真的寫檔）" if a.dry else ""))
    print("理由分布：", why)
    if not a.dry:
        rows, _ = S.read_rows()
    v = [r["points"] for (ln, _d), r in rows.items()
         if ln == "trend" and isinstance(r.get("points"), (int, float))]
    if v:
        mths = len({d[:7] for (ln, d) in rows if ln == "trend"})
        print("做了 %d 筆、合計 %+.0f 點、每筆 %+.1f、每月 %+.0f、勝率 %.0f%%"
              % (len(v), sum(v), sum(v) / len(v), sum(v) / max(mths, 1),
                 100 * sum(1 for x in v if x > 0) / len(v)))


if __name__ == "__main__":
    main()
