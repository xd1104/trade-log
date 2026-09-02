# -*- coding: utf-8 -*-
"""
K 線圖的「交易日拼裝」離線測試 —— **不連永豐、不送任何單**。

================================================================
這支存在的理由
================================================================
2026-09-02 早上 Benson 回報「K 線圖一直變來變去」，截圖是面板重啟後的
第 60 秒與第 93 秒：先畫出**前天（08-31）的夜盤**，連上永豐之後才換成昨晚（09-01）的。

根因在 `session_frame()`：
    nights = px[(tt >= NIGHT_OPEN) & (dd < d)]
    n = nights["ts"].dt.date.max()        # 夜盤開盤日
`n` 取的是「**手上這批資料裡**最近一個有夜盤的日子」。面板剛啟動還沒連上永豐時
只讀得到本機 csv，而 csv 的最後一天永遠缺夜盤（排程 14:10 跑，那時夜盤還沒發生）
⇒ 它就理直氣壯地挑到前天，**畫出錯的一晚**，而且畫面上完全看不出來。

程式其實知道那份不完整（`_cached_raw` 的 `partial` 旗標），只是沒拿它擋畫面。

**他早上是照這張圖決定要不要進場的**，所以這條要有測試守著：
少一段夜盤看得出來，畫錯一天看不出來。

怎麼跑（PowerShell）：
    & "C:\\Users\\USER\\Desktop\\Claude Work\\trade-log\\.venv\\Scripts\\python.exe" `
      "C:\\Users\\USER\\Desktop\\Claude Work\\trade-log\\tools\\shioaji\\test_session_frame.py"
"""
import datetime as dt
import pathlib
import sys

import pandas as pd

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import live_panel as LP

FAIL = 0
D = dt.date(2026, 9, 2)          # 「今天」（週三）
PREV = dt.date(2026, 9, 1)       # 正確的夜盤開盤日（昨晚）
OLDER = dt.date(2026, 8, 31)     # 錯誤答案：前天


def chk(name, got, want):
    global FAIL
    ok = got == want
    FAIL += not ok
    print(("  OK   " if ok else "  FAIL ") + name +
          ("" if ok else f"  (得到 {got!r}，期待 {want!r})"))


def bars(day, times, px=46000.0):
    """造幾根 1 分 K。times 是 'HH:MM' 字串。"""
    rows = []
    for i, t in enumerate(times):
        ts = pd.Timestamp(f"{day} {t}")
        rows.append({"ts": ts, "Open": px + i, "High": px + i + 2,
                     "Low": px + i - 2, "Close": px + i, "Volume": 10})
    df = pd.DataFrame(rows)
    return df.rename(columns={"Open": "o", "High": "h", "Low": "l",
                              "Close": "c", "Volume": "v"})


NIGHT = ["15:00", "15:01", "22:30"]          # 夜盤（15:00 之後）
DAY = ["08:45", "08:46", "09:00", "09:15"]   # 日盤


def install(back_days, incomplete):
    """
    換掉 _raw_days，模擬「這批資料涵蓋哪些日子」。
    incomplete=True 代表『我想跟永豐要卻要不到』（就是還沒連上線的那個狀態）。
    """
    def fake(days, report=None):
        want = [d for d in days if d in back_days]
        frames = [back_days[d] for d in want]
        if report is not None and incomplete:
            report["incomplete"] = True
        if not frames:
            return None
        return pd.concat(frames, ignore_index=True)
    LP._raw_days = fake
    LP._SESS_BACK.clear()
    LP._SESS_OWN.clear()
    LP._TODAY_RAW.clear()


def night_dates(g):
    """圖上出現了哪幾天的夜盤（日期小於 D 的那些）。"""
    if g is None or g.empty:
        return []
    return sorted({str(x) for x in g["ts"].dt.date if x < D})


print("=== ① 還沒連上永豐：本機 csv 只到前天的夜盤，今天的日盤已經有了 ===")
# 這就是他截圖那一刻：today 的 K 棒拿得到（已連線），但 back 那份還是啟動時的殘缺快取
install({OLDER: bars(OLDER, NIGHT, 45800.0), D: bars(D, DAY, 46500.0)}, incomplete=True)
g, base, partial = LP.session_frame(D)
# partial 現在回的是「哪一種不完整」：'night'＝夜盤沒到、'today'＝今天的整個拿不到、''＝都齊
chk("  要回報「夜盤那份不完整」", partial, "night")
chk("  ⛔ 絕對不可以畫出前天的夜盤", night_dates(g), [])
chk("  今天的日盤照樣要畫出來（那段是對的）",
    len(g) if g is not None else 0, len(DAY))
chk("  base 落在當天日盤開盤，不是某個猜出來的夜晚",
    str(base), str(pd.Timestamp.combine(D, LP.SESSION_OPEN)))

print("\n=== ② 資料到齊：夜盤要接上昨晚，不是前天 ===")
install({OLDER: bars(OLDER, NIGHT, 45800.0),
         PREV: bars(PREV, NIGHT, 46700.0),
         D: bars(D, DAY, 46500.0)}, incomplete=False)
g, base, partial = LP.session_frame(D)
chk("  不再回報不完整", partial, "")
chk("  夜盤接的是昨晚（09-01），不是前天（08-31）", night_dates(g), [str(PREV)])
chk("  base 落在昨晚 15:00",
    str(base), str(pd.Timestamp.combine(PREV, LP.NIGHT_OPEN)))
chk("  夜盤＋日盤都在", len(g), len(NIGHT) + len(DAY))

print("\n=== ③ 完全沒有資料：不可以炸掉，要照實說不完整 ===")
install({}, incomplete=True)
g, base, partial = LP.session_frame(D)
chk("  沒有資料時回 None", g, None)
chk("  但仍然回報不完整（前端才知道是在載入、不是休市）", partial, "night")

print("\n=== ④ 只有夜盤、還沒開盤（早上 08:45 之前）===")
install({PREV: bars(PREV, NIGHT, 46700.0)}, incomplete=False)
g, base, partial = LP.session_frame(D)
chk("  夜盤畫得出來", night_dates(g), [str(PREV)])
chk("  不會因為當天還沒有日盤就整個回 None", g is not None and not g.empty, True)

print("\n=== ⑤ 今天的 K 棒完全拿不到：圖整片是昨晚，一定要講出來 ===")
# 2026-09-02 他回報「加載完了但 K 圖還是不是最新的」。實測面板：109 根、最後一根
# 停在 23:45、**全部都是前一晚** —— 圖上卻掛著今天的日期，什麼都沒說。
# 跟夜盤那次同一個道理：少一段看得出來，掛錯日期看不出來。
NIGHT_FULL = ["%02d:%02d" % (15 + (m // 60), m % 60) for m in range(0, 9 * 60, 5)]
install({PREV: bars(PREV, NIGHT_FULL, 46800.0)}, incomplete=False)
g, base, partial = LP.session_frame(D)
chk("  要回報是「今天的拿不到」，不是夜盤沒到", partial, "today")
chk("  昨晚的還是要畫（那是手上唯一真的資料）", night_dates(g), [str(PREV)])
chk("  但圖上一根今天的 K 棒都沒有",
    [str(x) for x in g["ts"].dt.date if x == D], [])

print("\n=== ⑥ 兩種不完整要分得開（畫面上的文案完全不同）===")
install({OLDER: bars(OLDER, NIGHT, 45800.0), D: bars(D, DAY, 46500.0)}, incomplete=True)
chk("  夜盤沒到 → night", LP.session_frame(D)[2], "night")
install({PREV: bars(PREV, NIGHT, 46700.0), D: bars(D, DAY, 46500.0)}, incomplete=False)
chk("  都到齊 → 不回報不完整", LP.session_frame(D)[2], "")

print("\n總結:", "全部通過" if not FAIL else f"{FAIL} 項失敗")
sys.exit(1 if FAIL else 0)
