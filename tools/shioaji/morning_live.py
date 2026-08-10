r"""
早盤即時面板：09:00~09:05 開盤首根 K 即時追蹤 + 歷史情境對照
=============================================================================
用途：每天早上開盤時跑，即時算出當下的客觀數字，並對照歷史同情境的統計。

【這個工具會做什麼】
- 08:45 起接即時 tick，本地合成 1 分 K
- 09:00~09:05 倒數，即時顯示這根 K 正在長什麼樣
- 09:05 定案，輸出「判斷依據卡」：
    A. 今天的客觀事實（描述性數字，附歷史分位讓你知道今天算大還算小）
    B. 歷史情境對照（每列都標樣本數、信賴區間、是否統計顯著）

【這個工具不會做什麼】
- 不給買賣訊號、不說該做多還做空、不預測。
- 沒達統計顯著的數字一律標「不顯著」，那種數字只能當描述，不能當勝率。

執行：
  ..\..\.venv\Scripts\python.exe morning_live.py           # 正式：等開盤
  ..\..\.venv\Scripts\python.exe morning_live.py --demo    # 測試：不等時間，馬上跑一輪
"""

import json
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

import pandas as pd
import shioaji as sj
from shioaji import Exchange, TickFOPv1

from _config import get_credentials

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from pathlib import Path

STATS = Path(__file__).with_name("stats.json")
LOG_DIR = Path(__file__).with_name("morning_logs")

SESSION_OPEN = pd.Timestamp("08:45").time()
OPEN_T = pd.Timestamp("09:00").time()
ENTRY_T = pd.Timestamp("09:05").time()
DAY_END = pd.Timestamp("13:45").time()

bars = defaultdict(lambda: {"o": None, "h": -1e9, "l": 1e9, "c": None, "v": 0})
last_price = {"p": None, "t": None}


def on_tick(exchange: Exchange, tick: TickFOPv1):
    ts = tick.datetime.replace(second=0, microsecond=0)
    b = bars[ts]
    price = float(tick.close)
    if b["o"] is None:
        b["o"] = price
    b["h"] = max(b["h"], price)
    b["l"] = min(b["l"], price)
    b["c"] = price
    b["v"] += int(tick.volume)
    last_price["p"] = price
    last_price["t"] = tick.datetime


def fetch_reference(api, contract, today: date):
    """
    抓「上一個交易日的日盤收盤」+「從那時到今天開盤前的夜盤區間」。

    注意：週六也會有 K 線（週五夜盤延到週六凌晨），不能把它當成交易日。
    判定交易日的依據是「有沒有 08:45~13:45 的日盤 K 棒」。
    """
    frames = []
    for back in range(0, 9):
        d = today - timedelta(days=back)
        try:
            df = pd.DataFrame({**api.kbars(contract, start=str(d), end=str(d))})
        except Exception:
            continue          # 休市日會 404
        if not df.empty:
            frames.append(df)
    if not frames:
        return None, None, None

    all_df = pd.concat(frames, ignore_index=True)
    all_df["ts"] = pd.to_datetime(all_df["ts"])
    all_df = all_df.sort_values("ts")
    t = all_df["ts"].dt.time

    # 日盤 K 棒 → 用來認出真正的交易日
    day_bars = all_df[(t >= SESSION_OPEN) & (t < DAY_END)]
    if day_bars.empty:
        return None, None, None

    trading_days = sorted(day_bars["ts"].dt.date.unique())
    prev_day = next((d for d in reversed(trading_days) if d < today), trading_days[-1])

    prev_session = day_bars[day_bars["ts"].dt.date == prev_day]
    prev_close = float(prev_session["Close"].iloc[-1])

    # 夜盤 = 上一個交易日日盤收盤之後 ~ 今天日盤開盤之前
    night = all_df[(all_df["ts"] > prev_session["ts"].iloc[-1])
                   & (all_df["ts"] < pd.Timestamp.combine(today, SESSION_OPEN))]
    if night.empty:
        return prev_close, None, None
    return prev_close, float(night["High"].max()), float(night["Low"].min())


def match_buckets(stats, gap, body, vol_ratio, direction):
    """挑出今天符合的歷史情境。"""
    want = ["全部（首根K有方向就順勢做）"]
    want.append("首根K收紅 → 做多" if direction > 0 else "首根K收黑 → 做空")
    if gap is not None:
        want.append("跳空開高 > 20 點" if gap > 20 else
                    "跳空開低 > 20 點" if gap < -20 else "幾乎平盤開（±20 點內）")
    want.append("首根K實體 < 20 點（小K）" if body < 20 else
                "首根K實體 20~50 點" if body < 50 else "首根K實體 ≥ 50 點（大K）")
    if vol_ratio is not None:
        want.append("首根K放量（≥ 近20日中位數 1.2 倍）" if vol_ratio >= 1.2 else
                    "首根K縮量（≤ 0.8 倍）" if vol_ratio <= 0.8 else "首根K量能持平")
    by_name = {b["name"]: b for b in stats["buckets"]}
    return [by_name[w] for w in want if w in by_name]


def render_card(stats, k, prev_close, night_high, night_low, vol_ref):
    o, h, l, c, v = k["o"], k["h"], k["l"], k["c"], k["v"]
    body = abs(c - o)
    direction = 1 if c > o else -1 if c < o else 0
    gap = (o - prev_close) if prev_close else None
    vol_ratio = (v / vol_ref) if vol_ref else None
    desc = stats["descriptive"]

    def scale(val, med, p80):
        if val is None:
            return ""
        if val >= p80:
            return "偏大（前 20%）"
        if val >= med:
            return "中間偏大"
        return "偏小"

    print("\n" + "=" * 74)
    print(f"  開盤首根 5 分 K 定案   {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 74)

    print("\n【A】今天的客觀事實  ← 描述現況，沒有統計宣稱")
    print("-" * 74)
    arrow = "收紅 ▲" if direction > 0 else "收黑 ▼" if direction < 0 else "平盤 ─"
    print(f"  首根K  開 {o:.0f}   高 {h:.0f}   低 {l:.0f}   收 {c:.0f}    {arrow}")
    print(f"  實體   {body:.0f} 點      （歷史中位數 {desc['body_median']:.0f}、前20%門檻 {desc['body_p80']:.0f} → {scale(body, desc['body_median'], desc['body_p80'])}）")
    print(f"  高低幅 {h - l:.0f} 點      （歷史中位數 {desc['range_median']:.0f}）")
    if gap is not None:
        print(f"  跳空   {gap:+.0f} 點     （昨日日盤收 {prev_close:.0f}；跳空幅度歷史中位數 {desc['gap_abs_median']:.0f}、前20%門檻 {desc['gap_abs_p80']:.0f} → {scale(abs(gap), desc['gap_abs_median'], desc['gap_abs_p80'])}）")
    if vol_ratio is not None:
        tag = "放量" if vol_ratio >= 1.2 else "縮量" if vol_ratio <= 0.8 else "持平"
        print(f"  量能   {v} 口      （近20日同時段中位數 {vol_ref:.0f} → {vol_ratio:.2f} 倍，{tag}）")
    if night_high and night_low:
        pos = (c - night_low) / (night_high - night_low) * 100 if night_high > night_low else 50
        print(f"  夜盤區間 {night_low:.0f} ~ {night_high:.0f}    現在位在區間 {pos:.0f}% 的位置")

    if direction == 0:
        print("\n  首根K收平盤，沒有方向可對照。")
        return

    print("\n【B】歷史同情境對照  ← 樣本期間 " + stats["period"] + f"（{stats['n_days']} 天）")
    print("  規則：首根K方向順勢進場、停利停損 ±100 點、已扣手續費 5 點/筆")
    print("-" * 74)
    print(f"  {'情境':<30}{'樣本':>4}{'勝率':>7}{'期望值':>8}{'95%信賴區間':>16}  顯著")
    print("  " + "-" * 70)
    matched = match_buckets(stats, gap, body, vol_ratio, direction)
    sig_hits = []
    for b in matched:
        if not b.get("enough"):
            print(f"  {b['name']:<30}{b['n']:>4}   樣本太少")
            continue
        mark = "★" if b["significant"] else "—"
        ci = f"[{b['exp_ci'][0]:+.0f}, {b['exp_ci'][1]:+.0f}]"
        print(f"  {b['name']:<30}{b['n']:>4}{b['win_rate']:>6.1f}%{b['exp']:>+8.1f}{ci:>16}   {mark}")
        if b["significant"]:
            sig_hits.append(b)

    print("\n【C】怎麼讀這張表")
    print("-" * 74)
    print("  「—」= 信賴區間跨過 0，統計上無法證明有優勢。")
    print("     這種列的勝率只是這一年剛好長這樣，不是你的勝率，不要拿來下注。")
    if sig_hits:
        for b in sig_hits:
            direction_word = "負的" if b["exp"] < 0 else "正的"
            print(f"\n  ★ 今天命中一個統計顯著的情境：{b['name']}")
            print(f"     期望值 {b['exp']:+.1f} 點/筆（{direction_word}），信賴區間 "
                  f"[{b['exp_ci'][0]:+.0f}, {b['exp_ci'][1]:+.0f}]，樣本 {b['n']} 天。")
            if b["exp"] < 0:
                print("     → 這是唯一有統計證據的一項，而它說的是：這種情境順勢做會賠。")
        print("\n     提醒：這是從 12 個情境中篩出來的 1 個，用多重檢定標準看只在邊緣，")
        print("     不能當成鐵律。它比較適合當『今天不做』的理由，而不是『反著做』的理由。")
    else:
        print("\n  今天命中的情境全部都不顯著 —— 這張表沒有給你任何統計上的方向依據。")

    print("\n" + "=" * 74)
    print("  歷史統計 ≠ 預測。以上為資訊呈現，不是投資建議，進場與否由你決定。")
    print("=" * 74 + "\n")


def save_log(k, prev_close, vol_ref):
    LOG_DIR.mkdir(exist_ok=True)
    rec = {
        "date": str(date.today()),
        "open": k["o"], "high": k["h"], "low": k["l"], "close": k["c"], "volume": k["v"],
        "prev_close": prev_close, "vol_ref": vol_ref,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
    }
    f = LOG_DIR / f"{date.today()}.json"
    f.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"當日開盤資料已存檔 → morning_logs/{f.name}\n")


def main():
    demo = "--demo" in sys.argv
    if not STATS.exists():
        print("找不到 stats.json，請先跑 build_stats.py")
        return
    stats = json.loads(STATS.read_text(encoding="utf-8"))

    api_key, secret = get_credentials()
    api = sj.Shioaji()
    api.login(api_key=api_key, secret_key=secret)
    contract = api.Contracts.Futures.TXF.TXFR1
    api.set_on_tick_fop_v1_callback(on_tick)

    today = date.today()
    prev_close, night_high, night_low = fetch_reference(api, contract, today)
    vol_ref = stats["descriptive"]["vol5_median"]

    print(f"\n台指期 TXFR1 早盤面板   {'[DEMO 模式]' if demo else ''}")
    print(f"昨日日盤收盤 {prev_close}   夜盤區間 {night_low} ~ {night_high}")

    api.quote.subscribe(contract, quote_type=sj.constant.QuoteType.Tick,
                        version=sj.constant.QuoteVersion.v1)

    if demo:
        print("\nDEMO：收集 60 秒即時 tick，然後用這段資料當作『首根K』跑完整流程…")
        for i in range(60, 0, -10):
            print(f"  還有 {i} 秒…")
            time.sleep(10)
        k = {"o": None, "h": -1e9, "l": 1e9, "c": None, "v": 0}
        for b in bars.values():
            if b["o"] is None:
                continue
            if k["o"] is None:
                k["o"] = b["o"]
            k["h"] = max(k["h"], b["h"])
            k["l"] = min(k["l"], b["l"])
            k["c"] = b["c"]
            k["v"] += b["v"]
        if k["o"] is None:
            print("這段時間沒有成交，抓不到 tick（可能已休市）。")
            api.logout(); return
        render_card(stats, k, prev_close, night_high, night_low, vol_ref)
        api.logout()
        return

    # 正式模式：等到 09:00，即時顯示，09:05 定案
    print("\n等待 09:00 開盤…（Ctrl+C 可中止）")
    while datetime.now().time() < OPEN_T:
        time.sleep(1)

    print("\n開盤！首根 5 分 K 進行中：")
    while datetime.now().time() < ENTRY_T:
        now = datetime.now()
        window = [b for ts, b in bars.items()
                  if OPEN_T <= ts.time() < ENTRY_T and b["o"] is not None]
        if window:
            o = window[0]["o"]
            hi = max(b["h"] for b in window)
            lo = min(b["l"] for b in window)
            cur = last_price["p"]
            left = (datetime.combine(now.date(), ENTRY_T) - now).seconds
            state = "紅" if cur > o else "黑" if cur < o else "平"
            print(f"\r  {now:%H:%M:%S}  現價 {cur}  開 {o:.0f} 高 {hi:.0f} 低 {lo:.0f} "
                  f"（目前{state}，實體 {abs(cur - o):.0f} 點）  距定案 {left} 秒   ",
                  end="", flush=True)
        time.sleep(1)
    print()

    window = [(ts, b) for ts, b in sorted(bars.items())
              if OPEN_T <= ts.time() < ENTRY_T and b["o"] is not None]
    if not window:
        print("09:00~09:05 沒收到任何 tick，無法定案。")
        api.logout(); return

    k = {
        "o": window[0][1]["o"],
        "h": max(b["h"] for _, b in window),
        "l": min(b["l"] for _, b in window),
        "c": window[-1][1]["c"],
        "v": sum(b["v"] for _, b in window),
    }
    render_card(stats, k, prev_close, night_high, night_low, vol_ref)
    save_log(k, prev_close, vol_ref)
    api.logout()


if __name__ == "__main__":
    main()
