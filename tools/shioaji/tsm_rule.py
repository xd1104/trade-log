# -*- coding: utf-8 -*-
"""
【模擬】「台積電快攻」的**門檻與框寬正本**（2026-09-22 晚）。

⛔ 放在自己的檔：`sim_lanes.py` 有一條守衛（test_sim_lanes ⑫）不准它自己呼叫 percentile ——
   日盤那幾條的門檻一律走 auto_fire 注入的那一份，⛔ 不准有第二把尺。這一條是夜盤的新規則，
   跟 auto_fire 無關 ⇒ 它的尺住這裡，sim_lanes 只呼叫。
研究：tick-research/scripts/night_controls.py（`np.percentile(|過去 40 晚|, 80)`、框寬 ＝ 進場價 × 過去 20 晚振幅平均）。
"""
import numpy as np

WIN, MIN_N, PCTL, RNG_N = 40, 20, 80, 20


def threshold(past_mv):
    """過去（⛔ 不含今晚）最多 WIN 晚的走幅% ⇒ 門檻%（|走幅| 的第 PCTL 百分位）。"""
    v = np.abs(np.asarray(list(past_mv)[-WIN:], float))
    return float(np.percentile(v, PCTL))


def width(entry, past_rng):
    """框寬（點）＝ 進場價 × 過去（⛔ 不含今晚）最多 RNG_N 晚「進場到 04:58」振幅% 的平均。"""
    return float(entry) * float(np.mean(list(past_rng)[-RNG_N:])) / 100


def is_fast(mv, thr):
    return mv != 0 and abs(mv) >= thr
