# -*- coding: utf-8 -*-
"""
把【模擬】「台積電快攻」那一條**往回補**（2026-09-22 晚加）。

    python backfill_tsm.py [--from 2024-08-01] [--dry]

⛔ **不連永豐**：台指 1 分 K 只讀本機 `tmf_1min.csv`；台積電 ADR 走 `us_feed`（Alpaca，唯讀行情）。
⛔ 規則走 `sim_lanes.tsm_eval`／`tsm_facts` 正本（⛔ 這支不自己算一份）。
⚠️ 要先有 20 晚歷史才會開始做 ⇒ 開頭那一個多月一律「歷史不夠」，那是正常的。
⚠️ 已經有定論的晚上不重算（`append_row` 自己會擋重複），但歷史照樣記進 `tsm_ctx.json`。
"""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sim_lanes as S  # noqa: E402
import us_feed  # noqa: E402

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
    have = {k[1] for k in rows if k[0] == "tsm"}
    ctx = {"mv": {}, "rng": {}} if a.dry else S._tsm_ctx_read()

    done = skip = pend = 0
    why = {}
    E = d0
    while E < last:
        es = str(E)
        if E.weekday() > 4:
            E += timedelta(days=1)
            continue
        g = px[(px["d"] == E) | (px["d"] == E + timedelta(days=1))]
        first = us_feed.first5(S.TSM_SYM, E, feed=S.TSM_FEED)
        if es in have:
            skip += 1
        else:
            res = S.tsm_eval(E, g, first, ctx)
            if res.get("pending"):
                pend += 1
                why[res["why"]] = why.get(res["why"], 0) + 1
            else:
                why[res.get("why")] = why.get(res.get("why"), 0) + 1
                if a.dry or S.append_row(dict(res, calc="backfill")):
                    done += 1
                    if a.dry and res["decision"] != "不做":
                        rows[("tsm", es)] = res
        mv, rng = S.tsm_facts(E, g, first)
        if mv is not None:
            ctx["mv"][es] = mv
        if rng is not None:
            ctx["rng"][es] = rng
        E += timedelta(days=1)
    if not a.dry:
        S._tsm_ctx_write(ctx)
    print("落地 %d 列、已有跳過 %d、算不出來 %d%s" % (done, skip, pend,
                                           "（--dry：沒有真的寫檔）" if a.dry else ""))
    print("理由分布：", why)
    if not a.dry:
        rows, _ = S.read_rows()
    v = [r["points"] for (ln, _d), r in rows.items()
         if ln == "tsm" and isinstance(r.get("points"), (int, float))]
    if v:
        mths = len({d[:7] for (ln, d) in rows if ln == "tsm"})
        print("做了 %d 筆、合計 %+.0f 點、每筆 %+.1f、每月 %+.0f、勝率 %.0f%%"
              % (len(v), sum(v), sum(v) / len(v), sum(v) / max(mths, 1),
                 100 * sum(1 for x in v if x > 0) / len(v)))


if __name__ == "__main__":
    main()
