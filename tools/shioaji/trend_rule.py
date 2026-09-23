# -*- coding: utf-8 -*-
"""
【模擬】「夜盤跟勢」的**門檻正本**（2026-09-23）。

規則（研究：`tick-research/usopen_scan.py`／`usopen_best_check.py`／`trend_audit.py`）：
  美股開盤後 10 分鐘（夏令 21:40／冬令 22:40）進場那一刻，
  看台指**前 30 分鐘**走了幾點；≥ 過去 40 晚同一個量的第 80 百分位 ⇒ 順勢做 1 口，抱到 04:58。
⛔ 沒有停利停損（研究就是這樣定的；夜盤窄框會被洗掉，見 tick-research 的停損那一輪）。

⛔ 放在自己的檔：`sim_lanes.py` 有守衛不准它自己呼叫 percentile（日盤那幾條的門檻一律走 auto_fire）。
"""
import numpy as np

ENTRY_OFFSET = 10      # 美股開盤後幾分鐘進場
LOOKBACK = 30          # 訊號：進場前幾分鐘的走幅
WIN, MIN_N, PCTL = 40, 20, 80


def threshold(past_moves):
    """過去（⛔ 不含今晚）最多 WIN 晚的走幅（點）⇒ 門檻（|走幅| 的第 PCTL 百分位）。"""
    v = np.abs(np.asarray(list(past_moves)[-WIN:], float))
    return float(np.percentile(v, PCTL))


def is_fast(mv, thr):
    return mv != 0 and abs(mv) >= thr
