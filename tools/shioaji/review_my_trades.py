r"""
檢驗 Benson 自己的判斷：他在什麼盤面下進場？準不準？
=============================================================================
今天測完一輪之後，唯一還沒被否定的可能性是「Benson 自己的直覺有沒有價值」。
演算法在他的設定下沒有優勢（已用多種模型、多種目標驗證），但他本人還沒被測過。

這支程式把兩份資料對起來：

  trade-log App 的交易紀錄   date / time / dir / entry / exit / note
  面板存的逐分鐘盤面         morning_logs/YYYY-MM-DD-live.json
                              （沒有當天的即時存檔時，退而用 tmf_1min.csv 重建）

用 (日期, 進場時間) 對帳 —— 所以 App 與面板不需要連動，Benson 什麼都不用多做。

【怎麼給我資料】
在 trade-log App 按「匯出」下載 JSON，放到這個資料夾命名 my_trades.json。

【樣本要求】
25 筆看不出東西（勝率 64% 有 17% 機率純屬運氣）。
單筆報酬標準差約 90 點，要驗證「每筆多賺 15 點」需要約 140 筆，
「每筆多賺 10 點」需要約 320 筆。所以先累積到 100 筆再看，會比較有意義。

執行：
  ..\..\.venv\Scripts\python.exe review_my_trades.py
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).parent
TRADES = HERE / "my_trades.json"
LOGS = HERE / "morning_logs"
PX = HERE / "tmf_1min.csv"

POINT_VALUE = 10          # 微台每點 NT$10


def load_trades():
    if not TRADES.exists():
        print(f"找不到 {TRADES.name}")
        print("請到 trade-log App 按「匯出」下載 JSON，改名成 my_trades.json 放進這個資料夾。")
        return None
    t = pd.DataFrame(json.loads(TRADES.read_text(encoding="utf-8")))
    t["dir"] = t["dir"].fillna("long")
    t["points"] = np.where(t["dir"] == "long", t["exit"] - t["entry"], t["entry"] - t["exit"])
    # 時間統一成 HH:MM
    t["time"] = t["time"].astype(str).str.strip().map(
        lambda s: f"{int(s.split(':')[0]):02d}:{int(s.split(':')[1]):02d}" if ":" in s else s)
    return t.sort_values("date").reset_index(drop=True)


def market_at(d, hhmm):
    """先找面板存檔；沒有就用歷史 1 分 K 重建。"""
    f = LOGS / f"{d}-live.json"
    if f.exists():
        rec = json.loads(f.read_text(encoding="utf-8"))
        for m in rec.get("minutes", []):
            if m["time"] == hhmm:
                return m
    if not PX.exists():
        return None
    px = getattr(market_at, "_px", None)
    if px is None:
        px = pd.read_csv(PX)
        px["ts"] = pd.to_datetime(px["ts"])
        market_at._px = px
    g = px[px["ts"].dt.date.astype(str) == d].sort_values("ts")
    g = g[(g["ts"].dt.time >= pd.Timestamp("08:45").time())
          & (g["ts"].dt.time < pd.Timestamp("13:45").time())]
    if g.empty:
        return None
    upto = g[g["ts"].dt.strftime("%H:%M") <= hhmm]
    if upto.empty:
        return None
    cur = float(upto["Close"].iloc[-1])
    day_open = float(g["Open"].iloc[0])
    hi, lo = float(upto["High"].max()), float(upto["Low"].min())
    c = upto["Close"].to_numpy(dtype=float)
    return {"time": hhmm, "price": cur,
            "mom5": round(cur - c[max(0, len(c) - 6)], 1),
            "mom15": round(cur - c[max(0, len(c) - 16)], 1),
            "ret_open": round(cur - day_open, 1),
            "rng": round(hi - lo, 1),
            "pos": round((cur - lo) / (hi - lo), 3) if hi > lo else 0.5,
            "vol_cum": float(upto["Volume"].sum()), "reconstructed": True}


def band(v, edges, names):
    for e, n in zip(edges, names):
        if v <= e:
            return n
    return names[-1]


def main():
    t = load_trades()
    if t is None:
        return
    print(f"交易紀錄 {len(t)} 筆（{t['date'].min()} ~ {t['date'].max()}）")
    fee = 5.0
    t["net"] = t["points"] - fee
    print(f"整體：勝率 {(t['net'] > 0).mean() * 100:.1f}%　每筆 {t['net'].mean():+.1f} 點"
          f"　合計 {t['net'].sum():+.0f} 點（約 NT${t['net'].sum() * POINT_VALUE:+,.0f}）\n")

    if len(t) < 60:
        se = t["net"].std(ddof=1) / np.sqrt(len(t))
        need15 = int((2 * t["net"].std(ddof=1) / 15) ** 2)
        print("⚠️ 樣本還太少，以下分析只能當作趨勢觀察，不能下結論。")
        print(f"   目前 {len(t)} 筆，每筆 95% 區間 [{t['net'].mean() - 1.96 * se:+.1f},"
              f" {t['net'].mean() + 1.96 * se:+.1f}] 點 —— 跨過 0 就代表跟丟銅板分不出來。")
        print(f"   要驗證「每筆多賺 15 點」大約需要 {need15} 筆。\n")

    rows = []
    for _, x in t.iterrows():
        m = market_at(str(x["date"]), str(x["time"]))
        if not m:
            continue
        rows.append({**{k: x[k] for k in ["date", "time", "dir", "entry", "exit", "net", "note"]},
                     "mom5": m.get("mom5"), "mom15": m.get("mom15"),
                     "ret_open": m.get("ret_open"), "rng": m.get("rng"), "pos": m.get("pos")})
    r = pd.DataFrame(rows)
    if r.empty:
        print("沒有任何一筆對得上盤面資料。")
        return
    print(f"成功對上盤面的有 {len(r)} 筆\n")

    def show(title, col, edges, names):
        print(f"── {title} ──")
        r["_g"] = r[col].map(lambda v: band(v, edges, names))
        for n in names:
            s = r[r["_g"] == n]
            if len(s) < 3:
                continue
            se = s["net"].std(ddof=1) / np.sqrt(len(s)) if len(s) > 1 else 0
            print(f"   {n:<14} {len(s):>3} 筆　勝率 {(s['net'] > 0).mean() * 100:5.1f}%"
                  f"　每筆 {s['net'].mean():+7.1f} 點"
                  + (f"　95%[{s['net'].mean() - 1.96 * se:+.0f},{s['net'].mean() + 1.96 * se:+.0f}]"
                     if len(s) > 2 else ""))
        print()

    show("你進場時，最近 5 分鐘的動能", "mom5",
         [-40, -10, 10, 40], ["急跌", "小跌", "幾乎沒動", "小漲", "急漲"])
    show("你進場時，價格在當日區間的位置", "pos",
         [0.25, 0.5, 0.75], ["低檔 0~25%", "偏低 25~50%", "偏高 50~75%", "高檔 75~100%"])
    show("你進場時，今天已經走了多少", "ret_open",
         [-60, -20, 20, 60], ["已大跌", "小跌", "接近平盤", "小漲", "已大漲"])
    show("進場時間", "time", ["09:00", "09:05", "09:10"],
         ["09:00 前", "09:00~09:05", "09:05~09:10", "09:10 後"])

    print("── 心得關鍵字 vs 實際損益 ──")
    kw = ["追高", "搶反彈", "太早", "手癢", "等", "冷靜", "紀律", "小心", "騙"]
    for k in kw:
        s = r[r["note"].fillna("").str.contains(k)]
        if len(s) < 3:
            continue
        print(f"   「{k}」　{len(s):>3} 筆　勝率 {(s['net'] > 0).mean() * 100:5.1f}%"
              f"　每筆 {s['net'].mean():+7.1f} 點")

    out = HERE / "my_trades_reviewed.csv"
    r.drop(columns=["_g"], errors="ignore").to_csv(out, index=False)
    print(f"\n逐筆明細（含進場當下盤面）→ {out.name}")


if __name__ == "__main__":
    main()
