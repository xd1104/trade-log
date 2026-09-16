# -*- coding: utf-8 -*-
"""
【自動下單】「開箱」的箱子寬度歷史 —— **種子**（2026-09-16 加）。

面板每天 09:05~09:30 之間會自己把當天那一列寫進 `orb_hist.jsonl`（`auto_fire._orb_hist_step`），
但**第一天上線時檔案是空的** ⇒ 過去不足 20 天 ⇒ 開箱這個候選一律不可用。
這支從【策略實驗室】已經抓好的歷史逐筆（`tick_hist/ticks/YYYY-MM-DD.csv.gz`）把最近 N 個
交易日補進去。

    py build_orb_hist.py [--out 路徑] [--days 40] [--before YYYY-MM-DD] [--dry]

  --out     輸出的 jsonl（預設 `orb_hist.jsonl`；⛔ 只 append，檔案裡已經有的日子不重寫）
  --days    最多寫幾個最近的交易日（預設 40 ＝ 中位數要的 20 天再加一倍餘裕）
  --before  只收這一天**之前**的日子（預設今天 ⇒ 不會把今天那半天寫進去；
            今天那一列留給面板自己寫）
  --dry     只印不寫

一天一列：`{"date","hi","lo","w","fill","box_pct","src":"seed"}` —— 跟面板寫的同一種格式。
  箱子 ＝ 09:00:00.000 ~ 09:05:00.000（兩端都含）的最高／最低
  box_pct ＝ 箱寬 ÷ **進場價** × 100；有突破 ⇒ 進場價＝突破那一筆的賣價（多）／買價（空），
             沒突破 ⇒ 退回箱子最後一筆成交價（跟 `sim_lanes.orb_calc` 同一個實作決定）
  ⛔ 那段沒有成交 ⇒ 那天跳過並印出來（⛔ 不猜、不補）。

⛔ 這支**不連永豐**、不碰任何下單路徑；只讀 `tick_hist/`、只寫 `--out`。
⛔ 規則一律呼叫 `auto_fire` 的正本（`orb_box_of` / `orb_break_of` / `orb_fill` / `orb_box_pct`），
   ⛔ 不在這裡另寫一份 —— 種子跟面板算出來的值要是兩把尺，門檻就整個歪掉。
"""
import argparse
import json
import pathlib
import sys
from datetime import date

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import auto_fire as AF        # noqa: E402  ⛔ 規則的正本在這裡
import strategy_lab as SL     # noqa: E402  ⛔ 逐筆讀取的正本在這裡（含快取）


def day_rows(d):
    """一天 ⇒ orb_hist 的那一列（dict），算不出來 ⇒ (None, 為什麼)。"""
    D = SL.load_day(d)
    if D is None or not len(D["t"]):
        return None, "沒有逐筆"
    trades = list(zip(D["t"].tolist(), D["p"].tolist()))
    box = AF.orb_box_of(trades)
    if box is None:
        return None, "%s~%s 沒有成交" % (AF.ORB_BOX_FROM_AT[:5], AF.ORB_BOX_TO_AT[:5])
    hit = AF.orb_break_of(trades, box["hi"], box["lo"])
    if hit is None:
        fill = box["last"]
    else:
        i = int((D["t"] == hit["t_ms"]).argmax())
        fill = AF.orb_fill(hit["d"], hit["p"], float(D["bid"][i]), float(D["ask"][i]))
    bp = AF.orb_box_pct(box["w"], fill)
    if bp is None:
        return None, "算不出箱子寬度%"
    return {"date": str(d), "hi": box["hi"], "lo": box["lo"], "w": round(box["w"], 1),
            "fill": fill, "box_pct": round(bp, 6), "src": "seed"}, None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(AF.ORB_HIST))
    ap.add_argument("--days", type=int, default=40)
    ap.add_argument("--before", default=None)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args(argv)
    out = pathlib.Path(a.out)
    before = date.fromisoformat(a.before) if a.before else date.today()

    have = set()
    if out.exists():
        rows, bad, dup = AF.orb_hist_read(out)
        have = {r["date"] for r in rows}
        print("已經有 %d 天（壞列 %d、重複 %d）" % (len(have), bad, dup))

    days = [r["date"] for r in SL.days() if r.get("date") and r["date"] < str(before)]
    days = sorted(days)[-a.days:]
    if not days:
        print("⛔ tick_hist/days.jsonl 裡沒有可用的日子（先跑 strategy_lab.py 建）")
        return 2
    print("要補的範圍：%s ~ %s（%d 天）" % (days[0], days[-1], len(days)))

    new, skip = [], 0
    for ds in days:
        if ds in have:
            continue
        row, why = day_rows(date.fromisoformat(ds))
        if row is None:
            skip += 1
            print("  ⛔ 跳過 %s：%s" % (ds, why))
            continue
        new.append(row)
    print("算得出來 %d 天、跳過 %d 天" % (len(new), skip))
    if not new:
        return 0
    print("  範例：%s" % json.dumps(new[-1], ensure_ascii=False))
    if a.dry:
        print("（--dry：沒有寫檔）")
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    # ⛔ 一定是 append：這個檔面板也在寫（只 append），覆寫就是把它寫的那幾天弄丟。
    with out.open("a", encoding="utf-8") as f:
        for r in sorted(new, key=lambda x: x["date"]):
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        f.flush()
    print("寫進 %s（+%d 列）" % (out, len(new)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
