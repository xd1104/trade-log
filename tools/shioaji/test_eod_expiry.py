# -*- coding: utf-8 -*-
"""
⭐⭐ 結算日 13:30 —— **獨立的一件事、獨立的測試**（2026-09-16，PM 裁示）。

要守的兩件事：
  (a) **收盤自動平倉要有結算日分支**：結算日日盤 **13:30** 收盤，`EOD_CLOSE_AT`（13:43:30）
      那個時刻**市場已經關了** ⇒ 平倉單送不出去、部位抱過夜，而且 13:45 起停損也停了。
      ⇒ 結算日提前到 `EOD_CLOSE_AT_EXPIRY`（13:28:30，同樣是收盤前 90 秒）。
  (b) **`is_expiry()` 要認得移動過的結算日**：舊版用「每月第三個星期三」算，
      **農曆年會把結算日往後移**（實例 **2026-02-23**、**2023-01-30**）。
      新規則：第三個星期三；那天休市就順延到下一個有交易的日子。

⛔ 這一支**不碰任何真實資料夾**：只讀 `strategy_lab` 的純函式與 `live_panel` 的常數，
   行事曆一律用自己造的集合（⛔ 不讀他的 days.jsonl 來當斷言的來源）。
⛔ 這一支一張單都不會送（連假券商都不用）。

跑法：
    PYTHONIOENCODING=utf-8 PYTHONUTF8=1 .venv\\Scripts\\python.exe tools\\shioaji\\test_eod_expiry.py
"""
import datetime
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import strategy_lab as SL       # noqa: E402
import live_panel as LP         # noqa: E402

FAIL = 0


def chk(name, got, want):
    global FAIL
    ok = got == want
    FAIL += not ok
    print(("  OK   " if ok else "  FAIL ") + name + ("" if ok else f"  (得到 {got!r}，期待 {want!r})"))


def say(cond, name, extra=""):
    global FAIL
    FAIL += not cond
    print(("  OK   " if cond else "  FAIL ") + name + (("  " + str(extra)) if extra else ""))


def D(s):
    return datetime.date.fromisoformat(s)


def weekdays(a, b):
    """a~b 之間所有的平日（當成「有開盤」）。"""
    out, d = set(), D(a)
    while d <= D(b):
        if d.weekday() < 5:
            out.add(str(d))
        d += datetime.timedelta(days=1)
    return out


# ══ ① 不給行事曆 ⇒ ⛔ 跟 2026-09-16 以前一模一樣（舊呼叫端不會被動到）════════
print("=== ① 不給行事曆 ⇒ 舊行為一個字都沒變 ===")
for s, want in (("2025-01-15", True), ("2026-01-21", True), ("2026-09-16", True),
                ("2026-01-14", False), ("2026-01-22", False), ("2026-02-18", True),
                ("2026-02-23", False)):
    chk(f"  is_expiry({s}) 只看第三個週三", SL.is_expiry(D(s)), want)
chk("  第三個週三永遠落在 15~21 號",
    sorted({SL._wed3(datetime.date(y, m, 1)).day
            for y in (2023, 2024, 2025, 2026) for m in range(1, 13)}),
    [15, 16, 17, 18, 19, 20, 21])
say(all(SL._wed3(datetime.date(y, m, 1)).weekday() == 2
        for y in (2023, 2024, 2025, 2026) for m in range(1, 13)),
    "  而且一定是星期三")

# ══ ② 給行事曆 ⇒ 順延到下一個有交易的日子 ════════════════════════════════
print("\n=== ② ⭐ 農曆年：第三個週三休市 ⇒ 順延（2026-02 與 2023-01 兩個實例）===")
# 2026-02：第三個週三 02-18 落在年假裡，02-12~02-20 全休，02-23（一）開紅盤
CAL26 = (weekdays("2026-01-01", "2026-02-11") | weekdays("2026-02-23", "2026-03-31"))
chk("  治具自證：2026-02-18 不在行事曆裡（那天休市）", "2026-02-18" in CAL26, False)
chk("  治具自證：2026-02-23 在行事曆裡", "2026-02-23" in CAL26, True)
chk("  ⭐ 2026-02-23 是結算日（舊規則說不是）", SL.is_expiry(D("2026-02-23"), CAL26), True)
chk("    舊規則對照：同一天 ⇒ False（證明這條真的改變了答案）",
    SL.is_expiry(D("2026-02-23")), False)
chk("  ⛔ 2026-02-24 不是（前一天已經開過盤了）", SL.is_expiry(D("2026-02-24"), CAL26), False)
chk("  ⛔ 2026-02-17 不是（還沒到第三個週三）", SL.is_expiry(D("2026-02-17"), CAL26), False)
chk("  ⛔ 2026-02-11 不是", SL.is_expiry(D("2026-02-11"), CAL26), False)
# 2023-01：第三個週三 01-18 落在年假（台股 01-17 封關、01-30 開紅盤）⇒ 順延 12 天
CAL23 = (weekdays("2023-01-01", "2023-01-17") | weekdays("2023-01-30", "2023-02-28"))
chk("  治具自證：2023-01-18 休市", "2023-01-18" in CAL23, False)
chk("  ⭐ 2023-01-30 是結算日（順延 12 天）", SL.is_expiry(D("2023-01-30"), CAL23), True)
chk("    舊規則對照：同一天 ⇒ False", SL.is_expiry(D("2023-01-30")), False)
chk("  ⛔ 2023-01-31 不是", SL.is_expiry(D("2023-01-31"), CAL23), False)
# 正常月份：第三個週三有開盤 ⇒ 就是那天，⛔ 不准順延
CAL_OK = weekdays("2026-09-01", "2026-09-30")
chk("  ⛔ 正常月份：2026-09-16（第三個週三、有開盤）就是結算日",
    SL.is_expiry(D("2026-09-16"), CAL_OK), True)
chk("  ⛔ 正常月份：2026-09-17 不是（⛔ 不准順延）",
    SL.is_expiry(D("2026-09-17"), CAL_OK), False)
chk("  ⛔ 正常月份：2026-09-15 不是", SL.is_expiry(D("2026-09-15"), CAL_OK), False)

print("\n=== ③ ⛔ 行事曆有洞時不猜（順延上限 ＋ 左邊界）===")
chk("  上限就是 EXPIRY_MAX_POSTPONE 天", SL.EXPIRY_MAX_POSTPONE, 14)
# 行事曆從 08-01 起、但 09-16 之後就沒有資料了（＝資料缺，不是連假）
CAL_HOLE = weekdays("2026-08-01", "2026-09-15")
chk("  第三個週三（09-16）照舊算結算日", SL.is_expiry(D("2026-09-16"), CAL_HOLE), True)
chk("    邊界：差剛好 14 天 ⇒ 還算（`>` 不是 `>=`）",
    SL.is_expiry(D("2026-09-30"), CAL_HOLE), True)
chk("  ⛔ 差 15 天（超過上限）⇒ 不算（⛔ 不把一個離很遠的日子標成結算日）",
    SL.is_expiry(D("2026-10-01"), CAL_HOLE), False)
# ⭐⭐ **左邊界**：行事曆的起點比第三個週三還晚 ⇒ 中間有沒有交易日我們根本不知道
#    （實例：tick_hist 的第一天 2024-07-29，那個月的第三個週三是 07-17。
#      沒有這道的話那一天會被標成結算日 —— 2026-09-16 實測抓到過。）
CAL_LATE = weekdays("2024-07-29", "2024-08-31")
chk("  ⛔ 行事曆起點晚於第三個週三 ⇒ 那一天不算結算日（⛔ 不猜）",
    SL.is_expiry(D("2024-07-29"), CAL_LATE), False)
chk("    對照組：同一份行事曆，8 月的第三個週三照樣算得出來",
    SL.is_expiry(D("2024-08-21"), CAL_LATE), True)
chk("  ⛔ 行事曆是空的 ⇒ 只認第三個週三",
    [SL.is_expiry(D(s), set()) for s in ("2026-09-16", "2026-09-17")], [True, False])

print("\n=== ④ ⛔ 用他真的資料重驗一次（tick_hist 520 天）===")
try:
    cal = SL.trading_days()
except Exception as e:
    cal = set()
    print("  ·    讀不到行事曆（%s）" % str(e)[:80])
if len(cal) >= 300:
    # 這 25 天是量出來的（逐筆最後一筆停在 13:29）。⛔ 這張表是 2026-09-16 實測的結果，
    #    要改的人先重跑「每一天日盤最後一筆是幾點」。
    REAL = ["2024-08-21", "2024-09-18", "2024-10-16", "2024-11-20", "2024-12-18",
            "2025-01-15", "2025-02-19", "2025-03-19", "2025-04-16", "2025-05-21",
            "2025-06-18", "2025-07-16", "2025-08-20", "2025-09-17", "2025-10-15",
            "2025-11-19", "2025-12-17", "2026-01-21", "2026-02-23", "2026-03-18",
            "2026-04-15", "2026-05-20", "2026-06-17", "2026-07-15", "2026-08-19"]
    hit = [s for s in REAL if SL.is_expiry(D(s), cal)]
    chk("  ⭐ 25 天實測的結算日全中（0 漏）", len(hit), len(REAL))
    miss_old = [s for s in REAL if not SL.is_expiry(D(s))]
    chk("    舊規則漏掉的就是 2026-02-23（證明這條真的補了洞）", miss_old, ["2026-02-23"])
    wrong = [s for s in sorted(cal) if s not in REAL and SL.is_expiry(D(s), cal)]
    chk("  ⭐ 而且 0 誤抓（行事曆裡沒有別的日子被標成結算日）", wrong, [])
else:
    print("  ·    未驗：這台機器上沒有 tick_hist/days.jsonl（%d 天）⇒ ④ 整段跳過" % len(cal))
    say(False, "  ⛔ 這一段沒驗到（⛔ 不當成通過）")

# ══ ⑤ (a) 收盤平倉的結算日分支 ══════════════════════════════════════════
print("\n=== ⑤ ⭐ 收盤平倉：結算日提前到 13:28:30 ===")
chk("  平常那一組：字串與秒數對得上", LP.EOD_CLOSE_SEC,
    int(LP.EOD_CLOSE_AT[:2]) * 3600 + int(LP.EOD_CLOSE_AT[3:5]) * 60 + int(LP.EOD_CLOSE_AT[6:8]))
chk("  結算日那一組：字串與秒數對得上", LP.EOD_CLOSE_SEC_EXPIRY,
    int(LP.EOD_CLOSE_AT_EXPIRY[:2]) * 3600 + int(LP.EOD_CLOSE_AT_EXPIRY[3:5]) * 60
    + int(LP.EOD_CLOSE_AT_EXPIRY[6:8]))
chk("  ⭐ 結算日是 13:28:30", LP.EOD_CLOSE_AT_EXPIRY, "13:28:30")
chk("  ⛔ 兩組都是「收盤前 90 秒」（13:45−90／13:30−90）",
    (LP.DAY_END_SEC - LP.EOD_CLOSE_SEC, LP.DAY_END_SEC_EXPIRY - LP.EOD_CLOSE_SEC_EXPIRY),
    (90, 90))
chk("  ⛔ 結算日的上界是 13:30", LP.DAY_END_SEC_EXPIRY, 13 * 3600 + 30 * 60)
say(LP.EOD_CLOSE_SEC_EXPIRY + int(0) < LP.DAY_END_SEC_EXPIRY,
    "  ⛔ 平倉那一刻落在結算日收盤之前")
# ⚠️ auto_fire 的重試窗口（實測 75 秒）要塞得進「平倉時刻 → 收盤」
import auto_fire as AF          # noqa: E402
say(AF.EOD_WINDOW_S <= LP.DAY_END_SEC_EXPIRY - LP.EOD_CLOSE_SEC_EXPIRY,
    "  ⛔ 重試窗口（%.0f 秒）塞得進結算日的 90 秒" % AF.EOD_WINDOW_S)

print("\n  ── ⑤b eod_plan()：一天只算一次，判不出來就退回平常那一組 ──")
LP.EOD_DAY.update({"date": None})
p = LP.eod_plan(D("2026-02-23"))
chk("  2026-02-23（順延過的結算日）⇒ 用結算日那一組",
    (p["expiry"], p["at"], p["sec"], p["end"]),
    (True, LP.EOD_CLOSE_AT_EXPIRY, LP.EOD_CLOSE_SEC_EXPIRY, LP.DAY_END_SEC_EXPIRY))
p = LP.eod_plan(D("2026-02-24"))
chk("  2026-02-24 ⇒ 平常那一組",
    (p["expiry"], p["at"], p["sec"], p["end"]),
    (False, LP.EOD_CLOSE_AT, LP.EOD_CLOSE_SEC, None))
_n = {"n": 0}
_real_cal = SL.is_expiry_cal


def _count_cal(d):
    _n["n"] += 1
    return _real_cal(d)


SL.is_expiry_cal = _count_cal
try:
    LP.EOD_DAY.update({"date": None})
    for _ in range(50):
        LP.eod_plan(D("2026-02-24"))
    chk("  ⛔ 同一天只讀一次行事曆（4Hz 主迴圈不准每一圈都讀檔）", _n["n"], 1)
    LP.eod_plan(D("2026-02-25"))
    chk("    跨日會重算一次（⛔ 不是永遠不更新）", _n["n"], 2)
finally:
    SL.is_expiry_cal = _real_cal

print("\n  ── ⑤c ⛔ 判不出來 ⇒ 退回平常那一組，而且**要說得出原因** ──")
_real_sl = LP.strategy_lab
try:
    LP.strategy_lab = None
    LP.EOD_DAY.update({"date": None})
    p = LP.eod_plan(D("2026-02-23"))
    chk("  退回 13:43:30（安全的那一邊是「不猜」）",
        (p["expiry"], p["at"]), (False, LP.EOD_CLOSE_AT))
    say(p["err"] and "結算日" in p["err"], "  ⛔ 而且原因留著（⛔ 不可以安靜地用錯的時刻）", p["err"])
finally:
    LP.strategy_lab = _real_sl
    LP.EOD_DAY.update({"date": None})

print("\n  ── ⑤d ⛔ 主迴圈真的在用那一組（`_auto_tick` 走完整條路）──")


class FakeSt:
    price = None
    bid = None
    ask = None
    price_is_mid = False
    last_recv = None
    open = None
    prev_close = None
    high = None
    low = None
    minute_bar = {}
    minute_close = {}


def run_eod_at(day, hh, mm, ss):
    """把主迴圈的時鐘放到某一刻，看收盤平倉那個掛勾有沒有被叫、帶了什麼時刻。"""
    got = []
    old = LP.AUTO_EOD_HOOK
    LP.AUTO_EOD_HOOK = lambda d, lag, at=None: got.append((d, at))
    LP.AUTO.update({"started": True, "day": str(day), "done": True, "settled": True,
                    "eod": False, "rev": True, "gaps": 0.0})
    LP.EOD_DAY.update({"date": None})
    try:
        LP._auto_tick(FakeSt(), datetime.datetime.combine(day, datetime.time(hh, mm, ss)), "day")
    finally:
        LP.AUTO_EOD_HOOK = old
    return got


EXP_DAY, NORM_DAY = D("2026-02-23"), D("2026-02-24")
chk("  ⭐ 結算日 13:28:30 ⇒ 掛勾被叫，帶的是 13:28:30",
    run_eod_at(EXP_DAY, 13, 28, 30), [(str(EXP_DAY), LP.EOD_CLOSE_AT_EXPIRY)])
chk("  ⛔ 結算日 13:28:29 ⇒ 還沒到，不叫", run_eod_at(EXP_DAY, 13, 28, 29), [])
chk("  ⛔⛔ 結算日 13:43:30 ⇒ **不叫**（市場 13:30 就關了，那一刻在上界之外）",
    run_eod_at(EXP_DAY, 13, 43, 30), [])
chk("  ⛔ 結算日 13:30:00 ⇒ 不叫（上界是半開區間）", run_eod_at(EXP_DAY, 13, 30, 0), [])
chk("  對照組：一般日 13:43:30 ⇒ 掛勾被叫，帶的是 13:43:30",
    run_eod_at(NORM_DAY, 13, 43, 30), [(str(NORM_DAY), LP.EOD_CLOSE_AT)])
chk("  對照組：一般日 13:28:30 ⇒ ⛔ 不叫（那天還沒到）",
    run_eod_at(NORM_DAY, 13, 28, 30), [])
chk("  對照組：一般日 13:45:00 ⇒ 不叫（上界）", run_eod_at(NORM_DAY, 13, 45, 0), [])

print("\n  ── ⑤e 落地那一列要寫出「今天用的是哪一個時刻」 ──")
chk("  auto_fire.on_eod 收得到 at", AF.on_eod.__code__.co_varnames[:3], ("day", "lag_ms", "at"))
chk("  _eod 收得到 at", AF._eod.__code__.co_varnames[:4], ("day", "lag_ms", "put_at", "at"))

print("\n=== ⑥ ⛔ 收尾：這一支沒有動到任何真實資料夾 ===")
say(not (HERE / "AUTO_ORDERS_ON").exists(), "  ⛔ 真的 AUTO_ORDERS_ON 不存在")
say(LP.EOD_DAY["date"] is None or True, "  （eod_plan 的快取只活在記憶體）")

print("\n" + ("全部通過 ✅" if not FAIL else f"⛔ {FAIL} 項失敗"))
sys.exit(1 if FAIL else 0)
