# -*- coding: utf-8 -*-
"""
【自動下單】「開盤快才做」—— 離線逐日對照（2026-09-15，lab-dev）。

拿研究的逐筆資料（tick-research 的 `*.csv.gz`）當「歷史＋當天」，
呼叫**面板實作的那一套**判斷函式（⛔ 不自己另寫一份規則）：
  ・ref／px          build_fast_hist.day_row()（種子腳本同一支）
  ・門檻、快不快      auto_fire.fast_verdict()（送單那一刻與畫面用的同一支）
                     百分位 ＝ live_panel.FAST_PCTL（面板 configure 傳的同一個正本；80）
  ・停利停損點數      auto_fire.tpsl_points()
  ・方向（做法 A）    live_panel.auto_sig()／auto_dirs()；09:00 的價＝09:00:00 起第一筆
                     （面板的 minute_bar[540]["o"]）
逐日印出 PM 的對照表要的欄位，對不上就是實作跟研究不一樣。

    python fast-rule-replay.py DIR [DIR...] [--from 2026-09-01] [--to 2026-09-15]
    例：… tick-research\\ticks2026 tick-research   （後者底下只有 2026-09-15.csv.gz，今天到 10:02）

⚠️ 跟研究（benson_rule.py）刻意的差別只有一個：研究的 `LOCKED`（2025-04-07／04-10）
   與 BLOCKS 以外的日子不進歷史 —— 對 2026-09 的 40 天窗口沒有影響（全在 26Q3 區段內）。

⛔ 不連永豐、不寫任何檔（只印）。
"""
import argparse
import pathlib
import sys

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "shioaji"))
import auto_fire as AF            # noqa: E402
import build_fast_hist as B       # noqa: E402
import live_panel as LP           # noqa: E402


def p0900_of(f):
    """09:00:00.000 起第一筆成交（面板方向用的 minute_bar[540]["o"]）。"""
    t, p = B.load_ms_close(f)
    i = int(np.searchsorted(t, B.T0900, side="left"))
    return float(p[i]) if i < len(t) else None


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="+")
    ap.add_argument("--from", dest="d0", default="2026-09-01")
    ap.add_argument("--to", dest="d1", default="2026-09-15")
    a = ap.parse_args()
    files, _dup = B.tick_files(a.dirs)
    rows = []
    for s in sorted(files):
        if s > a.d1:
            break
        r, _why = B.day_row(files[s])
        if r is not None:
            rows.append(r)
    print("歷史：%d 天（%s ~ %s）" % (len(rows), rows[0]["date"], rows[-1]["date"]))
    print("%-10s %-6s %8s %8s %7s %7s %7s %5s %5s" % (
        "日期", "判定", "走幅%", "門檻%", "走點", "門檻點", "方向", "pts", "天數"))
    for r in rows:
        d = r["date"]
        if not (a.d0 <= d <= a.d1):
            continue
        v = AF.fast_verdict(d, r["move_pct"], rows, pctl=LP.FAST_PCTL)
        p900 = p0900_of(files[d])
        sig_a, sig_b = LP.auto_sig(r["px"], None, p900)
        dv = LP.auto_dirs(sig_a, sig_b)["A"]
        signed = r["px"] - r["ref"]
        thr_pts = AF.approx_points(v["thr_pct"], r["ref"])
        print("%-10s %-6s %8.3f %8s %+7.0f %7s %7s %5s %5d" % (
            d, {"fast": "快", "slow": "慢", "no_hist": "歷史不夠"}.get(v["verdict"], "?"),
            r["move_pct"], "—" if v["thr_pct"] is None else "%.3f" % v["thr_pct"],
            signed, "—" if thr_pts is None else thr_pts,
            "做多" if dv == 1 else ("做空" if dv == -1 else "—"),
            AF.tpsl_points(r["px"]), v["n"]))


if __name__ == "__main__":
    main()
