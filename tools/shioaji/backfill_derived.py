# -*- coding: utf-8 -*-
"""
把【模擬】推導出來的三條（夜盤跟勢只做多／夜盤聯軍／聯軍留倉）**往回補**（2026-09-23 深夜加）。

    python backfill_derived.py [--from 2024-08-01] [--dry]

⛔ 不連永豐、不連任何外部行情：只讀本機 `tmf_1min.csv` 與 `sim_lanes/` 裡本尊（夜盤跟勢、台積電快攻、多方聯軍）的定論。
⛔ 規則走 `sim_lanes.DERIVED_EVAL` 正本（⛔ 這支不自己算一份）。
⚠️ 本尊那一晚／那一天沒有定論 ⇒ 這三條也算不出來（照實計數，⛔ 不猜）。
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
    cnt = {k: {"done": 0, "skip": 0, "pend": 0} for k in S.DERIVED_LANES}
    why = {k: {} for k in S.DERIVED_LANES}
    E = d0
    while E < last:
        es = str(E)
        if E.weekday() > 4:
            E += timedelta(days=1)
            continue
        g = px[(px["d"] == E) | (px["d"] == E + timedelta(days=1))]
        for lane in S.DERIVED_LANES:
            if (lane, es) in rows:
                cnt[lane]["skip"] += 1
                continue
            res = S.DERIVED_EVAL[lane](E, g, rows)
            w = res.get("why")
            why[lane][w] = why[lane].get(w, 0) + 1
            if res.get("pending"):
                cnt[lane]["pend"] += 1
                continue
            if a.dry or S.append_row(dict(res, calc="backfill")):
                cnt[lane]["done"] += 1
                rows[(lane, es)] = res
        E += timedelta(days=1)
    for lane in S.DERIVED_LANES:
        c = cnt[lane]
        print("\n%s：落地 %d 列、已有跳過 %d、算不出來 %d%s" % (
            S.LANE_NAME[lane], c["done"], c["skip"], c["pend"], "（--dry）" if a.dry else ""))
        print("  理由分布：", why[lane])
    # 並排：每月點數（同一段日子）
    print("\n【同一段日子的每月點數】")
    months = sorted({d[:7] for (ln, d) in rows if ln == "trend"})
    for lane in ("union", "trend", "tsm") + S.DERIVED_LANES:
        v = [r["points"] for (ln, d), r in rows.items()
             if ln == lane and d[:7] in months and isinstance(r.get("points"), (int, float))]
        print("  %-10s 做了 %3d 筆、每月 %+6.0f" % (S.LANE_NAME[lane], len(v), sum(v) / max(len(months), 1)))
    lost = [r["trend_lost"] for (ln, d), r in rows.items()
            if ln == "hold" and isinstance(r.get("trend_lost"), (int, float))]
    print("  聯軍留倉擋掉的夜盤跟勢：%d 晚、每月 %+.0f（比較時要從『聯軍＋夜盤跟勢』裡扣掉）"
          % (len(lost), sum(lost) / max(len(months), 1)))


if __name__ == "__main__":
    main()
