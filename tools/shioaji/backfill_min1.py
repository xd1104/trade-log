# -*- coding: utf-8 -*-
"""
把 `tmf_1min.csv` 缺掉的日子跟永豐要回來（2026-09-17 加）。

    python backfill_min1.py [--from 2024-08-01] [--to YYYY-MM-DD] [--dry] [--yes]

**為什麼需要這一支**（2026-09-17 查出來的）：
- `tmf_1min.csv` 兩年只有 **3 個週六**有 K 棒（2026-08-15／08-22／08-29，各 301 根）。
  夜盤是「E 晚 → 隔天清晨 05:00」⇒ **週五晚上那一場的尾巴落在週六** ⇒
  週六沒資料 ＝【模擬】「夜盤順勢」**每一個週五晚都算不出來**（545 晚只落地 443）。
  ⚠️ 這跟 2026-09-16「台積電 ADR 訊號」被報了五次不同結論的根因是**同一個**
  （研究用的 1 分 K 一根週六都沒有 ⇒ 吃掉 18% 的夜盤訊號）。
- `append_today.py` 本來就會「回頭補抓前 4 天含週末」，那 3 個週六就是它抓的 ——
  但**那個 14:10 的排程現在沒有註冊**（`schtasks` 查不到），檔案從 **2026-09-02** 起就凍住了。
  ⇒ ⛔ 跑完這一支之後**還是要把排程接回去**，不然過幾天又會缺。

⛔ **一天一天抓**：區間端點碰到非交易日，永豐會整段回 404（不是沒資料，是端點無效）。
⛔ **抓之前的防護**（跟 `strategy_lab.fetch_today` 同一套，⛔ 不准放寬）：
   ・**08:30~13:50 不抓**（整個日盤是真單的時間，完全不跟它搶連線）
   ・**流量 > 85% 不抓**（`api.usage()`；讀不到上限也不抓）
   ・**有部位不抓** —— 這一支不 import broker，改成要你先自己確認（`--yes`），
     ⛔ 因為它是手動跑的一次性工具，不該長出一條碰得到下單模組的路。
⛔ **寫檔只在最後一次**：讀進來 → concat → `drop_duplicates("ts")` → 依 ts 排序 →
   先寫 `.tmp` 再 `os.replace`（原子換檔）。面板正開著也不會讀到寫到一半的檔。
   ⚠️ **排序是必要的**：`sim_lanes.night_frame()` 假設 K 棒是舊到新，直接 append 會讓它算錯。
⛔ 這一支**不重算 `intraday.csv`**（那是模型的歷史矩陣，另一件事）：要重算自己跑 `build_intraday.py`。
"""
import argparse
import os
import sys
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).parent
PX = HERE / "tmf_1min.csv"
QUIET_FROM, QUIET_TO = dtime(8, 30), dtime(13, 50)   # ⛔ 日盤不抓（跟 sim_lanes 同一個區間）
USAGE_MAX = 0.85                                     # ⛔ 流量停止線（跟 strategy_lab 同一個數字）
USAGE_TIMEOUT_MS = 10000
SAT_FULL = 250          # 一個完整的週六凌晨大約 301 根；少於這個數就當作那天缺
DAY_FULL = 600          # 一個完整的交易日（夜盤＋日盤）大約 1,140 根


def missing_days(px, d0, d1):
    """
    ⇒ 要跟永豐要的日子（舊到新）。兩種：
      ① **週六**：只要不足 `SAT_FULL` 根 —— 週五夜盤的尾巴，缺了夜盤那條就算不出週五
      ② **本機檔最後一天之後的每一天**（含週末）：排程停掉留下的洞
    ⛔ 不碰「今天」：今晚的夜盤還沒走完，抓回來是半截的（跟 csv 最後一天永遠是半天同一個坑）。
    """
    have = px.groupby(px["ts"].dt.date).size() if len(px) else pd.Series(dtype=int)
    last = max(have.index) if len(have) else d0
    out, d = [], d0
    while d <= d1:
        n = int(have.get(d, 0))
        if d.weekday() == 5:                 # 週六
            if n < SAT_FULL:
                out.append(("週六（週五夜盤的尾巴）", d, n))
        elif d > last:                       # 本機檔已經追不上的那一段
            out.append(("排程停掉之後缺的", d, n))
        elif d.weekday() == 6:               # 週日沒有夜盤，⛔ 不要白問
            pass
        elif n and n < DAY_FULL:
            out.append(("只有半天", d, n))
        d += timedelta(days=1)
    return out


def gate(api, now):
    """抓之前的共用防護。⇒ None＝可以抓，否則回一句不能抓的理由。"""
    if QUIET_FROM <= now.time() < QUIET_TO:
        return "現在是 %s~%s（日盤），⛔ 不跟真單搶連線" % (QUIET_FROM.strftime("%H:%M"), QUIET_TO.strftime("%H:%M"))
    try:
        u = api.usage(timeout=USAGE_TIMEOUT_MS)
        used, lim = float(getattr(u, "bytes", 0) or 0), float(getattr(u, "limit_bytes", 0) or 0)
    except Exception as e:
        return "讀不到流量用量（%s）⇒ ⛔ 當成不能抓" % str(e)[:80]
    if lim <= 0:
        return "讀不到流量上限 ⇒ ⛔ 當成不能抓"
    if used / lim > USAGE_MAX:
        return "永豐流量已用 %.0f%%（超過 %.0f%% 不抓）" % (used / lim * 100, USAGE_MAX * 100)
    print("  流量 %.0f MB／%.0f MB（%.1f%%）" % (used / 1e6, lim / 1e6, used / lim * 100), flush=True)
    return None


def main():
    ap = argparse.ArgumentParser(description="把 tmf_1min.csv 缺掉的日子跟永豐要回來")
    ap.add_argument("--from", dest="d0", default="2024-08-01")
    ap.add_argument("--to", dest="d1", default=None, help="預設＝昨天（⛔ 不抓今天，今晚夜盤還沒走完）")
    ap.add_argument("--dry", action="store_true", help="只列出缺哪幾天，不連永豐")
    ap.add_argument("--yes", action="store_true", help="我已經確認**手上沒有部位**（⛔ 有部位不准抓）")
    a = ap.parse_args()

    d0 = date.fromisoformat(a.d0)
    d1 = date.fromisoformat(a.d1) if a.d1 else date.today() - timedelta(days=1)
    px = pd.read_csv(PX)
    px["ts"] = pd.to_datetime(px["ts"])
    print("=" * 66)
    print("tmf_1min.csv：%d 根，%s ~ %s" % (len(px), px["ts"].min().date(), px["ts"].max().date()))
    want = missing_days(px, d0, d1)
    by = {}
    for why, d, n in want:
        by.setdefault(why, []).append(d)
    for why, ds in by.items():
        print("  缺 %-16s %3d 天　%s ~ %s" % (why, len(ds), ds[0], ds[-1]))
    print("合計要問 %d 天（%s ~ %s）" % (len(want), want[0][1] if want else "-", want[-1][1] if want else "-"))
    print("=" * 66)
    if not want:
        print("沒有缺的日子。")
        return
    if a.dry:
        print("（--dry：沒有連永豐）")
        return
    if not a.yes:
        sys.exit("⛔ 請先確認手上沒有部位，再加 --yes 重跑（有部位時抓資料會跟真單搶連線）")

    import shioaji as sj
    from _config import get_credentials

    api_key, secret = get_credentials()
    api = sj.Shioaji()
    api.login(api_key=api_key, secret_key=secret)
    frames, got, empty, failed = [], 0, [], []
    try:
        why_not = gate(api, datetime.now())
        if why_not:
            sys.exit("⛔ 不抓：" + why_not)
        contract = api.Contracts.Futures.TMF["TMFR1"]
        for i, (_why, d, _n) in enumerate(want):
            try:
                df = pd.DataFrame({**api.kbars(contract, start=str(d), end=str(d))})
            except Exception as e:
                failed.append(d)
                print("  %s 抓不到（休市日多半就是這樣）：%s" % (d, str(e)[:60]), flush=True)
                continue
            if df.empty:
                empty.append(d)
                continue
            df["ts"] = pd.to_datetime(df["ts"])
            frames.append(df)
            got += len(df)
            print("  %s 取得 %d 根" % (d, len(df)), flush=True)
            if (i + 1) % 25 == 0:               # ⛔ 中途再量一次流量：一口氣抓一百多天不能只看開頭
                why_not = gate(api, datetime.now())
                if why_not:
                    print("⛔ 中途停下：" + why_not)
                    break
    finally:
        try:
            api.logout()
        except Exception:
            pass

    if not frames:
        print("\n一根都沒拿到（這些日子多半是休市）。⛔ 沒有動 tmf_1min.csv。")
        return
    new = pd.concat(frames, ignore_index=True)
    merged = pd.concat([px, new], ignore_index=True).drop_duplicates(subset="ts").sort_values("ts")
    added = len(merged) - len(px)
    # ⛔ 先寫 .tmp 再原子換檔：面板正開著，⛔ 不可以讓它讀到寫到一半的檔
    tmp = PX.with_suffix(".csv.tmp")
    merged.to_csv(tmp, index=False)
    os.replace(tmp, PX)
    print("\n" + "=" * 66)
    print("拿到 %d 根、新增 %d 根（其餘是本來就有的）" % (got, added))
    print("休市／沒資料 %d 天、問不到 %d 天" % (len(empty), len(failed)))
    print("tmf_1min.csv 現在 %d 根，%s ~ %s"
          % (len(merged), merged["ts"].min().date(), merged["ts"].max().date()))
    sat = merged[merged["ts"].dt.dayofweek == 5]
    print("週六 K 棒：%d 根、%d 個週六（⛔ 跑之前只有 903 根／3 個週六）"
          % (len(sat), sat["ts"].dt.date.nunique()))
    print("⚠️ 接下來：跑 `backfill_sim.py --lanes night` 把夜盤那條重算，")
    print("⚠️ 然後把 14:10 那個排程接回去，不然過幾天又會缺。")


if __name__ == "__main__":
    main()
