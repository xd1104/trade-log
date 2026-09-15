# -*- coding: utf-8 -*-
"""
【策略實驗室】「模擬（不會下單）」—— 拿研究資料實跑 sim_lanes 的計算函式，逐日印出來給 PM 抽查（2026-09-15，lab-dev）。

  ・早盤快攻：tick-research 的 `ticks2026/ticks/YYYY-MM-DD.csv.gz`（strategy_lab.load_csv_day 讀，⛔ 不建快取）
             ＋ 門檻歷史＝`--hist` 指的 fast_hist.jsonl（真單在用的那一份，唯讀）
             ⇒ sim_lanes.fast_eval()（規則函式注入 auto_fire 的正本＋live_panel.FAST_PCTL，跟面板 main() 同一組）
  ・美股開盤順勢：tick-research 的 `nights/ticks/*.csv.gz`（夜盤逐筆，21:25~05:00）先合成 1 分 K
             （**標籤＝結束時間**：那一分鐘裡的成交歸到下一個整分），再丟 sim_lanes.night_eval()。
             ⚠️ 這是「逐筆合成的 1 分 K」，面板用的是永豐的 kbars／tmf_1min.csv —— 口徑一樣、來源不同。

⛔ 不連永豐、不寫任何檔（只印）。

    python sim-lanes-replay.py --ticks DIR --hist fast_hist.jsonl (--nights DIR | --min1 tmf_1min.csv)
                               [--from 2026-09-01] [--to 2026-09-15] [--night-from 2026-08-20] [--night-to 2026-09-01]
"""
import argparse
import pathlib
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "shioaji"))
import auto_fire as AF            # noqa: E402  （探針可以 import；sim_lanes 本身不行）
import live_panel as LP           # noqa: E402
import sim_lanes as S             # noqa: E402
import strategy_lab as SL         # noqa: E402


def night_bars(f):
    df = pd.read_csv(f, compression="gzip", usecols=["ts", "close"])
    ts = pd.to_datetime(df["ts"]) if pd.api.types.is_numeric_dtype(df["ts"]) else pd.to_datetime(df["ts"], format="ISO8601")
    lab = ts.dt.floor("min") + pd.Timedelta(minutes=1)
    g = pd.DataFrame({"ts": lab, "p": df["close"].astype(float)}).groupby("ts", sort=True)["p"]
    bars = pd.DataFrame({"High": g.max(), "Low": g.min(), "Close": g.last()}).reset_index()
    eve = ts[ts.dt.hour >= 15]
    return (eve.dt.date.min() if not eve.empty else None), bars


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", required=True)
    ap.add_argument("--nights", default=None, help="夜盤逐筆資料夾（逐筆合成 1 分 K）；跟 --min1 二選一")
    ap.add_argument("--min1", default=None, help="tmf_1min.csv（面板同一份 1 分 K，標籤＝結束時間）")
    ap.add_argument("--night-from", dest="n0", default=None)
    ap.add_argument("--hist", required=True)
    ap.add_argument("--from", dest="d0", default="2026-09-01")
    ap.add_argument("--to", dest="d1", default="2026-09-15")
    ap.add_argument("--night-to", dest="n1", default="2026-09-12")
    a = ap.parse_args()
    assert S.configure(AF.fast_verdict, AF.move_pct, AF.tpsl_points, AF.hist_read, LP.FAST_PCTL, AF.FAST_RULE)
    hist, bad, dup = AF.hist_read(pathlib.Path(a.hist))
    print("fast_hist：%d 列（%s ~ %s），壞列 %d、重複 %d" % (len(hist), hist[0]["date"], hist[-1]["date"], bad, dup))

    print("\n■ 早盤快攻（sim_lanes.fast_eval）")
    print("%-10s %-4s %8s %8s %6s %9s %9s %-4s %7s  %s" % ("日期", "判定", "走幅%", "門檻%", "天數", "進場", "出場", "出場", "點數", "原因"))
    for f in sorted(pathlib.Path(a.ticks).glob("*.csv.gz")):
        d = f.name[:10]
        if not (a.d0 <= d <= a.d1):
            continue
        r = S.fast_eval(d, SL.load_csv_day(f), hist)
        if r.get("pending"):
            print("%-10s 資料缺：%s" % (d, r["msg"]))
            continue
        print("%-10s %-4s %8s %8s %6s %9s %9s %-4s %7s  %s" % (
            d, r["decision"], r.get("move_pct"), r.get("thr_pct"), r.get("n_hist"),
            r["entry"] if r["entry"] is not None else "—", r["exit"] if r["exit"] is not None else "—",
            r["exit_reason"] or "—", r["points"] if r["points"] is not None else "—", r["reason"]))

    if a.min1:
        px = pd.read_csv(a.min1, usecols=["ts", "High", "Low", "Close"])
        px["ts"] = pd.to_datetime(px["ts"])
        d0 = date.fromisoformat(a.n0 or a.d0)
        d1 = date.fromisoformat(a.n1)
        src = []
        E = d0
        while E <= d1:
            if E.weekday() < 5:
                dd = px["ts"].dt.date
                src.append((E, px[(dd == E) | (dd == E + timedelta(days=1))].reset_index(drop=True)))
            E += timedelta(days=1)
        print("\n■ 美股開盤順勢（%s 的 1 分 K ⇒ sim_lanes.night_eval）" % pathlib.Path(a.min1).name)
    else:
        src = []
        for f in sorted(pathlib.Path(a.nights).glob("*.csv.gz")):
            E, bars = night_bars(f)
            if E is None or not ((a.n0 or a.d0) <= str(E) <= a.n1):
                continue
            src.append((E, bars))
        print("\n■ 美股開盤順勢（逐筆合成 1 分 K ⇒ sim_lanes.night_eval）")
    print("%-10s %-5s %-4s %9s %9s %-4s %9s %-5s %7s %9s  %s" % (
        "晚上E", "開盤", "判定", "ref", "c", "出場", "出場價", "時刻", "點數", "研究×460", "原因"))
    tot = 0.0
    for E, bars in src:
        r = S.night_eval(E, bars)
        if r.get("pending"):
            print("%-10s 資料缺：%s" % (E, r["msg"]))
            continue
        res = None
        if r["decision"] != "不做":
            d = 1 if r["decision"] == "做多" else -1
            res = 100 * (d * (r["exit"] - r["c"])) / r["c"] * 460 - 7
            tot += r["points"]
        print("%-10s %-5s %-4s %9s %9s %-4s %9s %-5s %7s %9s  %s" % (
            E, r.get("us_open"), r["decision"], r.get("ref"), r.get("c"), r["exit_reason"] or "—",
            r["exit"] if r["exit"] is not None else "—", r.get("exit_label", "—"),
            r["points"] if r["points"] is not None else "—", "—" if res is None else "%+.1f" % res, r["reason"]))
    print("夜盤合計（點，成本已扣）：%+.1f" % tot)


if __name__ == "__main__":
    main()
