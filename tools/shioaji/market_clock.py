# -*- coding: utf-8 -*-
"""
交易時刻的正本（2026-11-12，健檢報告整理第 2 批）。

為什麼有這支：同一個時刻（例如「夜盤幾點平倉」）以前寫在 6 個以上的地方，改一處漏一處。
11-12 把夜盤 ≥ 5 口改成 04:50 平時，重啟鈕的擋時段、問帳戶餘額的避開時刻都還停在 04:58，就是這種漏法。
⇒ 時刻只在這裡寫一次，其他模組讀這裡；`test_market_clock.py` 檢查各模組跟這裡一致（包含還沒改成讀這裡的）。

⛔ 只放「幾點」：不放規則、不放判斷。⛔ 改這裡的任何一個數字＝改交易行為，要 Benson 同意。
夜盤送單時刻跟美股冬夏令換（夏令 21:35／21:40、冬令 22:35／22:40），那個由 night_fire.done_at／trend_at 算，不寫死在這裡。
時刻一律 (時, 分, 秒)。
"""
from datetime import time as dtime


def hms(t):
    """(時, 分, 秒) ⇒ "HH:MM:SS"。"""
    return "%02d:%02d:%02d" % t


def hm(t):
    """(時, 分, 秒) ⇒ "HH:MM"。"""
    return "%02d:%02d" % t[:2]


def sec(t):
    """(時, 分, 秒) ⇒ 當天第幾秒。"""
    return t[0] * 3600 + t[1] * 60 + t[2]


def as_time(t):
    return dtime(*t)


# ── 日盤 ──
DAY_OPEN = (8, 45, 0)            # 期貨日盤開盤
CASH_OPEN = (9, 0, 0)            # 現貨開盤（快攻 ref、開箱起點）
SIGNAL = (9, 3, 30)              # 快攻／多方聯軍訊號
REV = (9, 15, 0)                 # 回馬槍（慢日反轉）
WATCH_END = (9, 30, 0)           # 開盤判斷時段結束（開箱突破截止）
DAY_EOD = (13, 43, 30)           # 日盤收盤平倉
DAY_EOD_EXPIRY = (13, 28, 30)    # 結算日收盤平倉（市場 13:30 收）
DAY_CLOSE = (13, 45, 0)          # 日盤收盤

# ── 夜盤 ──
NIGHT_OPEN = (15, 0, 0)
NIGHT_EOD = (4, 58, 0)           # 夜盤收盤平倉（4 口以下）
NIGHT_EOD_EARLY = (4, 50, 0)     # ⭐ 11-12 Benson 定：今晚那一筆 ≥ NIGHT_EOD_EARLY_QTY 口（微台等值）提早到這一刻
NIGHT_EOD_EARLY_QTY = 5
NIGHT_EOD_UNTIL = (4, 59, 50)    # 平倉重試到這一刻，之後叫他自己平
NIGHT_CLOSE = (5, 0, 0)

# 會送單（或平倉）的固定時刻 ⇒ 重啟鈕、五檔重連、問帳戶餘額都要避開（夜盤送單那兩刻另外跟 night_fire 問）
ORDER_MOMENTS = (SIGNAL, REV, DAY_EOD_EXPIRY, DAY_EOD, NIGHT_EOD_EARLY, NIGHT_EOD)
