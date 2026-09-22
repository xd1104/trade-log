# -*- coding: utf-8 -*-
"""
【模擬】第八條線「美股開盤模型」的**規則正本**（2026-09-22 加）。

⛔⛔ **特徵怎麼算只准寫在這一個檔。** 研究端（`tick-research/scripts/train_usml.py`）
   訓練時 import 這一支、面板算模擬時也 import 這一支 —— 兩邊同一把尺。
   各寫一份的話，模型看到的東西跟面板餵的東西會悄悄不一樣，而畫面上完全看不出來。

規則（研究報告 `tick-research/night_ml_results_2026-09-22.md`）：
  T    ＝美股開盤（台北；夏令 21:30／冬令 22:30，逐晚判斷）
  進場 ＝ T+5 的價，**抱 30 分鐘**平倉（⛔ 沒有停利停損）
  方向 ＝ 模型算出「接下來 30 分鐘上漲的機率」>0.5 做多、<0.5 做空
  成本 ＝ 7 點（跟其他模擬線同一個假設）

⚠️ **模型是離線訓練好的**（`usml_model.json`：標準化的平均/標準差＋係數）。
   面板只做「標準化 → 乘係數 → sigmoid」，⛔ 不在面板上訓練。
   重訓要跑 `train_usml.py`，並把新的 json 換進來（檔頭有 trained_through 可以核對）。
"""
import json
import math
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).parent
MODEL_PATH = HERE / "usml_model.json"

ENTRY_OFFSET = 5          # 美股開盤後幾分鐘進場
HOLD_MIN = 30             # 抱幾分鐘
COST = 7.0                # 一趟成本（點）

# ⛔ 順序就是模型係數的順序，⛔ 不准改動或重排
FEATURES = ["mv5", "mv5_abs", "since_open", "intraday_range", "gap1500",
            "prev_day_ret", "vol20", "rsi15", "vs_ma60", "spy5", "spread",
            "dow", "dom"]


def us_dst(d):
    """美國夏令：3 月第二個週日（含）~ 11 月第一個週日（不含）"""
    mar = date(d.year, 3, 8) + timedelta(days=(6 - date(d.year, 3, 8).weekday()) % 7)
    nov = date(d.year, 11, 1) + timedelta(days=(6 - date(d.year, 11, 1).weekday()) % 7)
    return mar <= d < nov


def us_open_min(d):
    """E 那晚美股開盤的台北分鐘數（夏令 21:30／冬令 22:30）"""
    return (21 * 60 + 30) if us_dst(d) else (22 * 60 + 30)


def near(bars, mm, tol=6):
    """≤mm 之內最近的一根（⛔ 只往回找，不用未來的價）。找不到 ⇒ None。"""
    for k in range(int(mm), int(mm) - tol - 1, -1):
        v = bars.get(k)
        if v is not None:
            return float(v)
    return None


def rsi(vals, n=15):
    v = [float(x) for x in vals][-n - 1:]
    if len(v) < 5:
        return 50.0
    d = [v[i + 1] - v[i] for i in range(len(v) - 1)]
    up = sum(x for x in d if x > 0) / len(d)
    dn = sum(-x for x in d if x < 0) / len(d)
    return 100 * up / (up + dn) if (up + dn) > 0 else 50.0


def features(E, tx, spy, day_close, prev_day_close, vol20):
    """
    ⭐ **特徵的唯一正本。** 全部只用 T+5 以前就知道的東西。
      E              夜盤所屬交易日
      tx             {夜盤分鐘: 台指收盤}（15:01 起，隔天 +1440）
      spy            {夜盤分鐘: SPY 收盤}（同一個時間軸；**用 IEX**，跟實盤拿得到的一致）
      day_close      E 當天日盤收盤（算 15:00 跳空、前一日漲跌用）
      prev_day_close 前一個交易日的日盤收盤
      vol20          過去 20 晚夜盤振幅（%）的平均
    ⇒ (特徵 dict, 進場價, 說明字串)；缺東西 ⇒ (None, None, 原因)
    """
    T = us_open_min(E)
    p0, p5 = near(tx, T), near(tx, T + ENTRY_OFFSET)
    o15 = tx.get(15 * 60 + 1)
    if p0 is None or p5 is None or o15 is None:
        return None, None, "缺台指的價（美股開盤前後）"
    s0, s5 = near(spy, T), near(spy, T + ENTRY_OFFSET)
    if s0 is None or s5 is None:
        return None, None, "缺 SPY 的價（美股開盤前後）"
    if day_close is None or prev_day_close is None or not vol20:
        return None, None, "缺日盤收盤或波動歷史"
    seg = [v for k, v in sorted(tx.items()) if k <= T + ENTRY_OFFSET]
    ma60 = sum(v for k, v in tx.items() if T + ENTRY_OFFSET - 60 <= k <= T + ENTRY_OFFSET)
    n60 = sum(1 for k in tx if T + ENTRY_OFFSET - 60 <= k <= T + ENTRY_OFFSET)
    ma60 = ma60 / n60 if n60 else p5
    f = {
        "mv5": (p5 - p0) / p0 * 100,
        "mv5_abs": abs(p5 - p0) / p0 * 100,
        "since_open": (p5 - o15) / o15 * 100,
        "intraday_range": (max(seg) - min(seg)) / min(seg) * 100 if seg else 0.0,
        "gap1500": (o15 - day_close) / day_close * 100,
        "prev_day_ret": (day_close - prev_day_close) / prev_day_close * 100,
        "vol20": float(vol20),
        "rsi15": rsi(seg),
        "vs_ma60": (p5 - ma60) / ma60 * 100 if ma60 else 0.0,
        "spy5": (s5 - s0) / s0 * 100,
        "spread": ((p5 - p0) / p0 - (s5 - s0) / s0) * 100,
        "dow": float(E.weekday()),
        "dom": float(E.day),
    }
    return f, p5, None


def load_model(path=None):
    """讀模型；沒有或讀不出來 ⇒ None（呼叫端要照實說「沒有模型」，⛔ 不猜）。"""
    p = Path(path) if path else MODEL_PATH
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if m.get("features") != FEATURES:
        return None                    # ⛔ 特徵對不上就不要用（順序錯了係數全錯）
    need = ("mean", "std", "coef", "intercept")
    if any(k not in m for k in need) or len(m["coef"]) != len(FEATURES):
        return None
    return m


def prob_up(model, f):
    """⇒ 上漲機率（0~1）。⛔ 只做標準化＋線性＋sigmoid，面板上不訓練任何東西。"""
    z = float(model["intercept"])
    for i, k in enumerate(FEATURES):
        sd = float(model["std"][i]) or 1.0
        z += float(model["coef"][i]) * ((float(f[k]) - float(model["mean"][i])) / sd)
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
