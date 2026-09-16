# -*- coding: utf-8 -*-
"""
【模擬】分頁 —— 拿研究資料實跑 sim_lanes 的計算函式，逐日印出來給 PM 抽查
（2026-09-15 lab-dev；2026-09-16 補上逐筆那六條：快攻／早收／回馬槍／純回馬／開箱／多方聯軍）。

  ・逐筆那六條：tick-research 的 `ticks2026/ticks/YYYY-MM-DD.csv.gz`（strategy_lab.load_csv_day 讀，⛔ 不建快取）
             ＋ 門檻歷史＝`--hist` 指的 fast_hist.jsonl（真單在用的那一份，唯讀）
             ⇒ sim_lanes.fast_eval()（規則函式注入 auto_fire 的正本＋live_panel.FAST_PCTL，跟面板 main() 同一組）
  ・夜盤順勢：tick-research 的 `nights/ticks/*.csv.gz`（夜盤逐筆，21:25~05:00）先合成 1 分 K
             （**標籤＝結束時間**：那一分鐘裡的成交歸到下一個整分），再丟 sim_lanes.night_eval()。
             ⚠️ 這是「逐筆合成的 1 分 K」，面板用的是永豐的 kbars／tmf_1min.csv —— 口徑一樣、來源不同。

⛔ 不連永豐、不寫任何檔（只印）。

⛔⛔ **這支印出來的開箱成績，是在「窗口跨度閘門關閉」的情況下算的**（lab-qa 2026-09-16 S5）：
   這裡把箱子歷史當成一串**沒有日期的 list** 傳給 `orb_eval`，而 `sim_lanes._box_win()` 收到 list
   就把 `span` 設成 `None` ⇒ `orb_span_bad()` 直接放行。面板那條路傳的是 `box_window()` 的 dict、
   跨度 > `ORB_SPAN_MAX_DAYS` 會記「資料缺」不做定論 ⇒ **面板的結果可能比這裡少幾天**。
   ⇒ ⛔ 不要把這支印出來的數字當成「閘門後的成績」拿去跟面板對帳。

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
    # ⛔ configure() 是**全有全無**：2026-09-16 加了 reversal_fn／rev_sec，少傳就整個回 False
    #    （這支當場掛在這一行 —— lab-qa 退件 R2）。⛔ 以後 configure 加參數要一起改這裡。
    assert S.configure(AF.fast_verdict, AF.move_pct, AF.tpsl_points, AF.hist_read, LP.FAST_PCTL, AF.FAST_RULE,
                       reversal_fn=AF.reversal_dir, rev_sec=LP.REV_SEC), "configure 接不上（參數少傳？）"
    hist, bad, dup = AF.hist_read(pathlib.Path(a.hist))
    print("fast_hist：%d 列（%s ~ %s），壞列 %d、重複 %d" % (len(hist), hist[0]["date"], hist[-1]["date"], bad, dup))

    # ⛔ 逐筆那六條**共用同一天的 day_pack**（跟面板 _step_ticks 同一條路），⛔ 不要一條算一次。
    #    ⚠️ 開箱／多方聯軍要「這一天以前 20 天的箱子寬度%」—— 這裡邊跑邊累積（研究資料是連續的），
    #       跟面板去掃 tick_hist 的來源不同，但口徑（orb_box_pct）是同一支。
    _hdr = ("日期", "判定", "走幅%", "門檻%", "天數", "進場", "出場", "出場", "點數", "原因")
    _fmt = "%-10s %-4s %8s %8s %6s %9s %9s %-4s %7s  %s"
    _all = sorted(pathlib.Path(a.ticks).glob("*.csv.gz"))
    # ⚠️ 只多讀「要印的第一天」往前 ORB_HIST_N×2 個檔（湊得出 20 天箱子歷史就好）——
    #    整個資料夾全讀很慢，而且更早的檔時間欄格式不一樣（load_csv_day 會炸）。
    _i0 = next((i for i, f in enumerate(_all) if f.name[:10] >= a.d0), len(_all))
    _files = _all[max(0, _i0 - S.ORB_HIST_N * 2):]
    _box, _skip = {}, []                        # 日期 ⇒ 箱子寬度%（照日期順序邊跑邊累積）
    _rows = {ln: [] for ln in S.TICK_LANES}
    for f in _files:
        d = f.name[:10]
        try:
            D = SL.load_csv_day(f)
        except Exception as e:                  # ⛔ 讀不動就跳過並**講出來**（不可以安靜地少一天歷史）
            _skip.append("%s（%s）" % (d, str(e)[:40]))
            continue
        past = [v for _d, v in sorted(_box.items()) if _d < d][-S.ORB_HIST_N:]
        pk = S.day_pack(d, D, hist, past)
        if a.d0 <= d <= a.d1:
            for ln in S.TICK_LANES:
                _rows[ln].append((d, S.TICK_EVAL[ln](d, D, hist, past, pack=pk)))
        v = S.orb_box_pct(d, D)                 # ⚠️ 先算結果再把今天加進歷史（⛔ 不可以偷看今天）
        if v is not None:
            _box[d] = v
    print("箱子寬度歷史：%d 天可用（%s ~ %s）%s"
          % (len(_box), min(_box) if _box else "—", max(_box) if _box else "—",
             ("；讀不動 %d 天：%s" % (len(_skip), "、".join(_skip[:3]))) if _skip else ""))
    for ln in S.TICK_LANES:
        print("\n■ %s（sim_lanes.%s）" % (S.LANE_NAME[ln], ln))
        if ln in ("orb", "union"):
            # ⛔ 講在輸出裡（看報告的人不會去讀 docstring）：這裡的箱子歷史是沒有日期的 list
            #    ⇒ `_box_win()` 把 span 設成 None ⇒ 跨度閘門整個跳過。
            print("  ⚠️ 這一段是**窗口跨度閘門關閉**下算的（歷史用 list 傳、沒有日期）"
                  "⇒ ⛔ 不等於面板的成績（面板會把跨度 > %d 天的日子記成資料缺）" % S.ORB_SPAN_MAX_DAYS)
        print(_fmt % _hdr)
        tot, n = 0.0, 0
        for d, r in _rows[ln]:
            if r.get("pending"):
                print("%-10s 資料缺：%s" % (d, r["msg"]))
                continue
            if r.get("points") is not None:
                tot, n = tot + r["points"], n + 1
            print(_fmt % (
                d, r["decision"], r.get("move_pct"), r.get("thr_pct"), r.get("n_hist"),
                r["entry"] if r["entry"] is not None else "—", r["exit"] if r["exit"] is not None else "—",
                r["exit_reason"] or "—", r["points"] if r["points"] is not None else "—", r["reason"]))
        print("  合計 %+.1f 點（%d 筆）" % (tot, n))

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
        print("\n■ 夜盤順勢（%s 的 1 分 K ⇒ sim_lanes.night_eval）" % pathlib.Path(a.min1).name)
    else:
        src = []
        for f in sorted(pathlib.Path(a.nights).glob("*.csv.gz")):
            E, bars = night_bars(f)
            if E is None or not ((a.n0 or a.d0) <= str(E) <= a.n1):
                continue
            src.append((E, bars))
        print("\n■ 夜盤順勢（逐筆合成 1 分 K ⇒ sim_lanes.night_eval）")
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
