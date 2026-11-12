# -*- coding: utf-8 -*-
"""
交易時刻正本（market_clock.py，2026-11-12 整理第 2 批）的一致性檢查。純離線、不連永豐。

  ① 正本的數字跟 Benson 定過的一樣（改任何一個都會在這裡紅 ⇒ 改交易行為要他同意）
  ② 已經改讀正本的模組（live_panel、night_fire、monitor_push）讀到的跟正本一樣
  ③ 還自己寫數字的模組（auto_fire 開箱、sim_lanes、decision_log、tick_writer）跟正本一致 —— 改一處漏一處就在這裡抓
  ④ 每一個會送單／平倉的時刻：重啟鈕擋得住、問帳戶餘額會避開、五檔重連會安靜
跑法：.venv/Scripts/python.exe -X utf8 tools/shioaji/test_market_clock.py
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.stdout.reconfigure(encoding="utf-8")

import testkit  # noqa: E402
testkit.sandbox()
import market_clock as MC  # noqa: E402
import auto_fire  # noqa: E402
import decision_log  # noqa: E402
import live_panel as LP  # noqa: E402
import monitor_push  # noqa: E402
import night_fire  # noqa: E402
import sim_lanes  # noqa: E402
import tick_writer  # noqa: E402

FAIL = []


def chk(name, got, want):
    ok = got == want
    print(("  OK   " if ok else "  FAIL ") + name + ("" if ok else "  (得到 %r，期待 %r)" % (got, want)))
    if not ok:
        FAIL.append(name)


print("=== ① 正本的數字（Benson 定過的）===")
chk("日盤：開盤 08:45、現貨 09:00、訊號 09:03:30、回馬 09:15、判斷結束 09:30",
    [MC.hms(t) for t in (MC.DAY_OPEN, MC.CASH_OPEN, MC.SIGNAL, MC.REV, MC.WATCH_END)],
    ["08:45:00", "09:00:00", "09:03:30", "09:15:00", "09:30:00"])
chk("日盤：收盤平倉 13:43:30、結算日 13:28:30、收盤 13:45",
    [MC.hms(t) for t in (MC.DAY_EOD, MC.DAY_EOD_EXPIRY, MC.DAY_CLOSE)], ["13:43:30", "13:28:30", "13:45:00"])
chk("夜盤：開盤 15:00、平倉 04:58、≥5 口 04:50、重試到 04:59:50、收盤 05:00",
    [MC.hms(t) for t in (MC.NIGHT_OPEN, MC.NIGHT_EOD, MC.NIGHT_EOD_EARLY, MC.NIGHT_EOD_UNTIL, MC.NIGHT_CLOSE)]
    + [MC.NIGHT_EOD_EARLY_QTY],
    ["15:00:00", "04:58:00", "04:50:00", "04:59:50", "05:00:00", 5])

print("\n=== ② 改讀正本的模組 ===")
chk("live_panel 日盤時刻", [LP.SIGNAL_AT, LP.REV_AT, LP.EOD_CLOSE_AT, LP.EOD_CLOSE_AT_EXPIRY],
    ["09:03:30", "09:15:00", "13:43:30", "13:28:30"])
chk("live_panel 秒數", [LP.SIGNAL_SEC, LP.REV_SEC, LP.EOD_CLOSE_SEC, LP.EOD_CLOSE_SEC_EXPIRY],
    [MC.sec(MC.SIGNAL), MC.sec(MC.REV), MC.sec(MC.DAY_EOD), MC.sec(MC.DAY_EOD_EXPIRY)])
chk("live_panel 盤別邊界",
    [str(x) for x in (LP.SESSION_OPEN, LP.WATCH_END, LP.DAY_END, LP.NIGHT_OPEN, LP.NIGHT_CLOSE, LP.CASH_OPEN)],
    ["08:45:00", "09:30:00", "13:45:00", "15:00:00", "05:00:00", "09:00:00"])
chk("night_fire 平倉時刻", [night_fire.EOD_FROM, night_fire.EOD_EARLY_FROM, night_fire.EOD_UNTIL, night_fire.EOD_EARLY_QTY],
    [MC.NIGHT_EOD, MC.NIGHT_EOD_EARLY, MC.NIGHT_EOD_UNTIL, MC.NIGHT_EOD_EARLY_QTY])
chk("monitor_push 幾點自動平（4 口／5 口）", [monitor_push._night_eod_hm(4), monitor_push._night_eod_hm(5)],
    ["04:58", "04:50"])

print("\n=== ③ 還自己寫數字的模組，要跟正本一致 ===")
chk("auto_fire 開箱 09:00～09:05、突破截止＝判斷結束",
    [auto_fire.ORB_BOX_FROM_AT, auto_fire.ORB_BREAK_BY], [MC.hms(MC.CASH_OPEN), MC.hms(MC.WATCH_END)])
chk("sim_lanes 快攻 ref／訊號（毫秒）", [sim_lanes.FAST_REF_MS, sim_lanes.FAST_PX_MS],
    [MC.sec(MC.CASH_OPEN) * 1000, MC.sec(MC.SIGNAL) * 1000])
chk("sim_lanes 夜盤模擬抱到 04:58（⚠ 模擬是 1 口，不套 ≥5 口提早）", sim_lanes.NIGHT_EXIT_MIN,
    MC.NIGHT_EOD[0] * 60 + MC.NIGHT_EOD[1] + 1440)
chk("decision_log 逐筆窗口＝日盤開盤～判斷結束", decision_log.TICK_WIN, (MC.hms(MC.DAY_OPEN), MC.hms(MC.WATCH_END)))
chk("tick_writer 預設錄製窗口", [str(tick_writer.DEFAULT_START), str(tick_writer.DEFAULT_END)],
    [MC.hms(MC.DAY_OPEN), MC.hms(MC.WATCH_END)])

print("\n=== ④ 每一個送單／平倉時刻：重啟擋、問帳戶避開、五檔安靜 ===")
day = datetime(2026, 6, 10)          # 週三
for t in MC.ORDER_MOMENTS:
    at = day.replace(hour=t[0], minute=t[1], second=t[2])
    hmv = MC.hm(t)
    blocked = any(a <= hmv < b for a, b, _why in LP.RESTART_BLOCK)
    chk("%s：重啟鈕擋得住" % MC.hms(t), blocked, True)
    chk("%s：問帳戶餘額會避開" % MC.hms(t), LP._eq_quiet(at), True)
    chk("%s：在五檔重連的安靜時刻裡" % MC.hms(t), MC.hms(t) in LP.DEPTH_QUIET_ZONES, True)
for m in LP.night_moments(datetime(2026, 1, 14, 22, 0)) + LP.night_moments(datetime(2026, 6, 10, 21, 0)):
    chk("夜盤送單 %s：問帳戶餘額會避開" % m.strftime("%H:%M:%S"), LP._eq_quiet(m), True)
chk("問帳戶：平常的 11:00 照問", LP._eq_quiet(day.replace(hour=11)), False)

print("\n總結：" + ("全部通過 ✅" if not FAIL else "%d 項沒過 ❌：%s" % (len(FAIL), "；".join(FAIL))))
sys.exit(1 if FAIL else 0)
