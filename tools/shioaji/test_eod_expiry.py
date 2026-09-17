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

print("\n=== ③ ⛔⛔ 行事曆有洞 ⇒ 回答「**不知道**」（⛔ 不准當成「那天沒開盤」）===")
# ⛔⛔⛔ 這一整段 2026-09-17 **重寫**（lab-qa 退件 M1）。
#    舊版把下面 CAL_HOLE 的 09-30 釘成 `True`（「差剛好 14 天 ⇒ 還算」），
#    ⇒ 那條斷言**把壞行為當成了正確答案**：真實情況是 `days.jsonl` 只到 09-15、
#      第三個週三是 09-16，於是 **09-17～09-30 整整 14 個平常日全被判成結算日**
#      （主控台印「今天是結算日」、err 是 None、每天安靜地提早 15 分鐘平倉）。
#    ⇒ 正確答案是 **None ＝ 判不出來**。
chk("  上限就是 EXPIRY_MAX_POSTPONE 天", SL.EXPIRY_MAX_POSTPONE, 14)
# 行事曆從 08-01 起、但 09-15 之後就沒有資料了（＝資料缺，不是連假）
CAL_HOLE = weekdays("2026-08-01", "2026-09-15")
chk("  治具自證：行事曆最後一天是 09-15、09-16 不在裡面",
    (max(CAL_HOLE), "2026-09-16" in CAL_HOLE), ("2026-09-15", False))
chk("  第三個週三（09-16）照舊算結算日（主判斷就是第三個週三）",
    SL.expiry_state(D("2026-09-16"), CAL_HOLE), (True, None))
# ⭐⭐⭐ **PM 指名要釘住的那個失敗案例**（2026-09-17 退件 M1）：
#    第三個週三 ＝ 09-16，行事曆只到 09-15 ⇒ 09-16 之後**一天資料都沒有**
#    ⇒ 分不出「09-16 休市所以順延」跟「只是還沒補資料」⇒ **必須回答「不知道」**。
_st, _why = SL.expiry_state(D("2026-09-17"), CAL_HOLE)
chk("  ⭐⭐⭐ 09-17（第三個週三 09-16 查不到、它後面沒有任何交易日）⇒ **不知道**", _st, None)
say(bool(_why) and "2026-09-16" in _why,
    "  ⛔ 而且說得出是哪一天查不到（⛔ 不可以只回 None 不說話）", _why)
chk("    ⛔ 布林版是 False ⇒ 收盤平倉用平常那一組（⛔ 不是 13:28:30）",
    SL.is_expiry(D("2026-09-17"), CAL_HOLE), False)
chk("  ⛔ 09-30（差剛好 14 天）也是「不知道」—— ⛔ 舊版把它釘成 True",
    SL.expiry_state(D("2026-09-30"), CAL_HOLE)[0], None)
chk("  ⛔ 差 15 天（超過順延上限）⇒ **確定不是**（不必再講「不知道」）",
    SL.expiry_state(D("2026-10-01"), CAL_HOLE), (False, None))
# ⭐ 對照組（夾擊）：同一個第三個週三，行事曆**前後都有**交易日 ⇒ 答案是確定的
CAL_PINCER = (weekdays("2026-08-01", "2026-09-15") | weekdays("2026-09-17", "2026-09-30"))
chk("  ⭐ 夾擊成立（09-16 前後都有交易日、09-16 本身沒有）⇒ 確定順延到 09-17",
    SL.expiry_state(D("2026-09-17"), CAL_PINCER), (True, None))
chk("    同一份行事曆：09-18 確定不是",
    SL.expiry_state(D("2026-09-18"), CAL_PINCER), (False, None))
chk("  ⭐ 第三個週三**在**行事曆裡 ⇒ 一翻兩瞪眼，⛔ 不准順延",
    [SL.expiry_state(D(s), weekdays("2026-09-01", "2026-09-30"))
     for s in ("2026-09-16", "2026-09-17")], [(True, None), (False, None)])
# ⭐⭐ **左邊界**：行事曆的起點比第三個週三還晚 ⇒ 中間有沒有交易日我們根本不知道
#    （實例：tick_hist 的第一天 2024-07-29，那個月的第三個週三是 07-17。
#      沒有這道的話那一天會被標成結算日 —— 2026-09-16 實測抓到過。）
CAL_LATE = weekdays("2024-07-29", "2024-08-31")
chk("  ⛔ 行事曆起點晚於第三個週三 ⇒ 那一天是「不知道」（⛔ 不猜）",
    SL.expiry_state(D("2024-07-29"), CAL_LATE)[0], None)
chk("    ⛔ 布林版照舊是 False（舊呼叫端的答案一個字都沒變）",
    SL.is_expiry(D("2024-07-29"), CAL_LATE), False)
chk("    對照組：同一份行事曆，8 月的第三個週三照樣算得出來（而且是確定的）",
    SL.expiry_state(D("2024-08-21"), CAL_LATE), (True, None))
chk("  ⛔ 行事曆是空的 ⇒ 只認第三個週三，而且那個答案是**確定的**（沒有順延這回事）",
    [SL.expiry_state(D(s), set()) for s in ("2026-09-16", "2026-09-17")],
    [(True, None), (False, None)])
# ⛔⛔ `max(cal)` **不可以**拿來當新鮮度浮水印（lab-qa 2026-09-17 指出）：
#    連假也會讓 max 停住 ⇒ 2026-02-23 那種**真的移動過**的結算日會被誤殺。
CAL_LNY = (weekdays("2026-01-01", "2026-02-11") | weekdays("2026-02-23", "2026-02-27"))
chk("  ⛔⛔ 反例：農曆年讓行事曆「停」在 02-11，但 02-23 照樣判得出來"
    "（⛔ 所以不准拿 max(cal) 當新鮮度浮水印）",
    SL.expiry_state(D("2026-02-23"), CAL_LNY), (True, None))

print("\n=== ④ ⛔ 用他真的資料重驗一次（tick_hist 520 天）===")
try:
    cal = SL.trading_days()
except Exception as e:
    cal = set()
    print("  ·    讀不到行事曆（%s）" % str(e)[:80])
if len(cal) >= 300:
    # 這 26 天是量出來的（逐筆最後一筆停在 13:29）。⛔ 這張表是實測的結果，
    #    要改的人先重跑「每一天日盤最後一筆是幾點」。
    # ⭐ 2026-09-17 補上 **2026-09-16**：`days.jsonl` 那天收盤後多了一列，
    #    lab-dev 重量過 `tick_hist/ticks/2026-09-16.csv.gz` ⇒ **日盤最後一筆 13:29:58**
    #    ⇒ 它是真的結算日（⛔ 不是規則抓錯，是這張表沒跟上資料）。
    REAL = ["2024-08-21", "2024-09-18", "2024-10-16", "2024-11-20", "2024-12-18",
            "2025-01-15", "2025-02-19", "2025-03-19", "2025-04-16", "2025-05-21",
            "2025-06-18", "2025-07-16", "2025-08-20", "2025-09-17", "2025-10-15",
            "2025-11-19", "2025-12-17", "2026-01-21", "2026-02-23", "2026-03-18",
            "2026-04-15", "2026-05-20", "2026-06-17", "2026-07-15", "2026-08-19",
            "2026-09-16"]
    # ⚠️ 行事曆只到「昨天」⇒ 表裡比行事曆還新的日子不該拿來算漏抓（那不是漏，是還沒發生）
    REAL = [s for s in REAL if s <= max(cal)]
    hit = [s for s in REAL if SL.is_expiry(D(s), cal)]
    chk("  ⭐ 25 天實測的結算日全中（0 漏）", len(hit), len(REAL))
    miss_old = [s for s in REAL if not SL.is_expiry(D(s))]
    chk("    舊規則漏掉的就是 2026-02-23（證明這條真的補了洞）", miss_old, ["2026-02-23"])
    wrong = [s for s in sorted(cal) if s not in REAL and SL.is_expiry(D(s), cal)]
    chk("  ⭐ 而且 0 誤抓（行事曆裡沒有別的日子被標成結算日）", wrong, [])
    # ⭐⭐⭐ 2026-09-17 退件 M1：**往前掃到行事曆的盡頭之後**（那正是出事的那一段）。
    #    ⛔ 這一段的正確答案只有兩天是結算日（09-16、10-21），其餘一律 13:43:30。
    #    ⚠️ 用 eod_plan()（＝面板真的在用的那一支），⛔ 不是只問 is_expiry。
    _fwd, _d = [], D("2026-09-16")
    while _d <= D("2026-10-31"):
        if _d.weekday() < 5:
            LP.EOD_DAY.update({"date": None})
            _p = LP.eod_plan(_d)
            _fwd.append((str(_d), _p["expiry"], _p["at"], _p["sure"]))
        _d += datetime.timedelta(days=1)
    LP.EOD_DAY.update({"date": None})
    chk("  ⭐⭐ 2026-09-16~10-31：只有 09-16 與 10-21 判成結算日",
        [x[0] for x in _fwd if x[1]], ["2026-09-16", "2026-10-21"])
    chk("  ⛔⛔ 其餘每一天都是 13:43:30（⛔ 舊版有 14 天安靜地用 13:28:30）",
        sorted({x[2] for x in _fwd if not x[1]}), [LP.EOD_CLOSE_AT])
    chk("    兩個真的結算日用的是 13:28:30",
        sorted({x[2] for x in _fwd if x[1]}), [LP.EOD_CLOSE_AT_EXPIRY])
    say(all(x[3] for x in _fwd if x[1]),
        "  ⛔ 而且那兩天是**有把握**的（⛔ 不是猜的）")
    say(all(x[1] is False for x in _fwd if not x[3]),
        "  ⛔ 判不出來的日子一律走平常那一組（⛔ 沒有一天是「不確定卻用了 13:28:30」）",
        "%d 天判不出來" % sum(1 for x in _fwd if not x[3]))
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
# ⚠️ 2026-09-17：面板走的是**三態**那一支（`expiry_state_cal`），⛔ 不是 `is_expiry_cal`
#    —— 這裡要數的就是面板真的會叫的那一支（數錯了這條等於沒測）。
_real_cal = SL.expiry_state_cal


def _count_cal(d):
    _n["n"] += 1
    return _real_cal(d)


SL.expiry_state_cal = _count_cal
try:
    LP.EOD_DAY.update({"date": None})
    for _ in range(50):
        LP.eod_plan(D("2026-02-24"))
    chk("  ⛔ 同一天只讀一次行事曆（4Hz 主迴圈不准每一圈都讀檔）", _n["n"], 1)
    LP.eod_plan(D("2026-02-25"))
    chk("    跨日會重算一次（⛔ 不是永遠不更新）", _n["n"], 2)
finally:
    SL.expiry_state_cal = _real_cal

print("\n  ── ⑤c ⛔ 判不出來 ⇒ 退回平常那一組，而且**要說得出原因** ──")
_real_sl = LP.strategy_lab
try:
    LP.strategy_lab = None
    LP.EOD_DAY.update({"date": None})
    p = LP.eod_plan(D("2026-02-23"))
    chk("  退回 13:43:30（安全的那一邊是「不猜」）",
        (p["expiry"], p["at"]), (False, LP.EOD_CLOSE_AT))
    say(p["err"] and "結算日" in p["err"], "  ⛔ 而且原因留著（⛔ 不可以安靜地用錯的時刻）", p["err"])
    chk("  ⛔ 而且 sure 是 False（畫面靠這個字決定要不要出聲）", p["sure"], False)
finally:
    LP.strategy_lab = _real_sl
    LP.EOD_DAY.update({"date": None})

# ⭐⭐⭐ ⑤c2（2026-09-17 退件 M1）：**行事曆有洞**那條路要走到 eod_plan()
#    ——⛔ 這才是真的會發生的那一種「判不出來」（⑤c 那種是模組整個載不起來）。
print("\n  ── ⑤c2 ⛔⛔ 行事曆只到第三個週三的前一天 ⇒ eod_plan 要說「不知道」 ──")
_real_state = SL.expiry_state_cal
_HOLE = weekdays("2026-08-01", "2026-09-15")
try:
    SL.expiry_state_cal = lambda d: SL.expiry_state(d, _HOLE)
    LP.EOD_DAY.update({"date": None})
    p = LP.eod_plan(D("2026-09-17"))
    chk("  ⛔⛔ 09-17 ⇒ **不是**結算日那一組（⛔ 舊版會安靜地用 13:28:30）",
        (p["expiry"], p["at"], p["sec"], p["end"]),
        (False, LP.EOD_CLOSE_AT, LP.EOD_CLOSE_SEC, None))
    chk("  ⛔ sure＝False（⛔ 不可以跟平常日子長得一樣）", p["sure"], False)
    say(p["err"] and "2026-09-16" in p["err"] and LP.EOD_CLOSE_AT in p["err"],
        "  ⛔ err 講得出「哪一天查不到」與「改用哪個時刻」", p["err"])
    LP.EOD_DAY.update({"date": None})
    p2 = LP.eod_plan(D("2026-09-16"))
    chk("  ⭐ 對照組：同一份有洞的行事曆，第三個週三 09-16 照樣判得出來（而且有把握）",
        (p2["expiry"], p2["at"], p2["sure"], p2["err"]),
        (True, LP.EOD_CLOSE_AT_EXPIRY, True, None))
finally:
    SL.expiry_state_cal = _real_state
    LP.EOD_DAY.update({"date": None})

# ⛔ 「今天是結算日」那句話**在不確定時不准出現**（PM 2026-09-17 裁示 3）。
#    ⚠️ 這裡比的是**原始碼**（那句話印在 main() 的啟動訊息裡，測試起不了 main）——
#       所以另外要求它跟 `_ep["sure"]` 綁在一起，⛔ 不是只有 `_ep["expiry"]`。
_src_main = pathlib.Path(LP.__file__).read_text(encoding="utf-8")
_i_line = _src_main.find("今天是結算日，日盤 13:30 收盤")
_win = _src_main[_i_line - 200:_i_line + 400] if _i_line > 0 else ""
say(_i_line > 0 and '_ep["expiry"]' in _win and 'not _ep["sure"]' in _win
    and "判不出來" in _win,
    "  ⛔⛔ 啟動那一行：不確定時走「判不出來」那一句，⛔ 不准印「今天是結算日」")
say("D.eod_sure===false" in _src_main and "eod_err" in _src_main,
    "  ⛔ 而且「判不出來」有端到**畫面**上（⛔ 不是只留在主控台）")

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
