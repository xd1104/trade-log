# -*- coding: utf-8 -*-
"""
K 線圖與逐筆圖的資料：某一天的 K 棒（本機 CSV ＋ 跟永豐要）、日盤＋夜盤接成一段、疊上面板記憶體裡的即時分鐘、
換時間框、交易日清單／休市日、逐筆檔（tick_logs/）的解析與快取
（2026-11-12 健檢整理第 5 批，從 live_panel.py 原樣搬出來）。

⛔ 這支**只給畫面看**：不下單、不碰停損、不寫任何交易紀錄；跟永豐只用面板現有的連線要 K 棒（⛔ 不另外登入）。
⛔ 自動下單讀的 Today.minute_bar／minute_close 不在這裡（只讀 Today.minute_ts 給圖用，見 overlay_live）。
⛔ 不准 import live_panel（面板是用 `python live_panel.py` 跑的 ⇒ 再 import 一次會變成兩份狀態）。
   面板的東西由 live_panel 在載入時接上：CURRENT_STATE、SESSION_REF（同一個 dict）、
   today_trades／trade_dir／tick_dir／sim_lanes_mod（lambda：測試導走或換掉時跟著走）、day_features、prev_day_close。
"""
import json
import re
import threading
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

import pandas as pd

import broker
import market_clock as MC

HERE = Path(__file__).parent
PRODUCT = "TMF"                                 # live_panel 載入時會用它自己的蓋過去（同一個值）
CHART_TF = 5                                    # K 線圖用 5 分 K（跟 live_panel 同一個數；test_panel_split 檢查）
SESSION_OPEN = MC.as_time(MC.DAY_OPEN)
DAY_END = MC.as_time(MC.DAY_CLOSE)
NIGHT_OPEN = MC.as_time(MC.NIGHT_OPEN)
NIGHT_TAIL = pd.Timestamp("05:01").time()

# ── live_panel 載入時接上（⛔ 沒接上 ⇒ 丟錯或當沒有，不會去碰任何真的東西）──
CURRENT_STATE = {"today": None}
SESSION_REF = {"api": None}


def today_trades():
    return []


def trade_dir():
    raise RuntimeError("panel_kline.trade_dir 還沒接上")


def tick_dir():
    raise RuntimeError("panel_kline.tick_dir 還沒接上")


def sim_lanes_mod():
    return None


def day_features(bars, d):
    raise RuntimeError("panel_kline.day_features 還沒接上")


def prev_day_close(d):
    return None


def _day_trades(d):
    """某一天的練習交易（今天的用記憶體，過去的讀檔）。"""
    if d == date.today():
        return list(today_trades())
    f = trade_dir() / f"{d}.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []


def day_bars(day=None, tf=CHART_TF, full=False):
    """
    取某一天的 K 棒，附上那天的練習交易（給 K 線圖標記用）。

    full=True  完整交易日：前一交易日 15:00 的夜盤 → 當天 13:45 收盤（即時分頁用）。
    full=False 只有日盤 08:45~13:45（回顧分頁用）—— 回顧與 Bar Replay 談的是
               他 08:45~09:30 的那一單，把夜盤幾百根塞進去只會讓重播沒法用。

    tf: 1 或 5（分鐘）。回顧分頁的 Bar Replay 用 1 分 K —— 08:45~09:30 只有 9 根
        5 分 K，逐根重播沒有練習密度。
    tf=1 也一樣走 to_timeframe()：永豐的 1 分 K 用「結束時間」標記，
    直接吃原始 ts 會整串偏一分鐘（見 to_timeframe 的說明）。

    盤中即時抓得到當天的 K 棒（實測 0 分鐘延遲），所以不必自己從 tick 拼；
    直接跟永豐要，資料跟大戶投同源，也就不會對不起來。
    """
    d = day or date.today()
    tf = 1 if int(tf) == 1 else CHART_TF

    if full:
        try:
            g, base, partial = session_frame(d)
            if g is None or g.empty:
                return {"date": str(d), "bars": [], "trades": [], "tf": tf, "full": True,
                        "partial": bool(partial), "partial_what": partial,
                        "error": None if SESSION_REF.get("api") else "尚未連線，且本機沒有這幾天的資料"}
            if d == date.today():
                g = overlay_live(g)          # 先在 1 分 K 這一層換掉還沒收完的那幾分鐘
                if partial == "today":
                    # ⭐ 2026-11-02：永豐 kbars 逾時，但面板自己收的即時 K 棒補上了 ⇒ 不是「圖上只有昨晚」
                    mine = g[(g["ts"].dt.date == d) & (g["ts"].dt.time >= SESSION_OPEN)]
                    if not mine.empty:
                        late = mine["ts"].min() > pd.Timestamp.combine(d, SESSION_OPEN) + pd.Timedelta(minutes=5)
                        partial = "today_part" if late else ""
            bars = to_timeframe(g, tf, base)
        except Exception as e:
            return {"error": str(e)[:120]}
        return {"date": str(d), "bars": bars, "trades": _day_trades(d), "tf": tf,
                # partial＝夜盤那一段還沒到齊（通常是剛啟動、還沒連上永豐）。
                # 前端據此顯示「夜盤載入中」，不要讓他以為圖已經完整。
                "full": True, "partial": bool(partial), "partial_what": partial,
                "night_open": base.strftime("%Y-%m-%d"),
                # 漲跌的基準是上一個交易日的日盤收盤（跟看盤軟體一致）。
                # 含夜盤之後不能再拿「圖上第一根的開盤」當基準 —— 那是昨晚 15:00。
                "ref": prev_day_close(d)}

    df = None

    # 過去的日子優先讀本機的 tmf_1min.csv（排程每天累積，永久留著）——
    # 回顧不該依賴永豐的 API 還活著，也快得多。
    if d != date.today():
        df = local_bars(d)

    if df is None:
        api = SESSION_REF.get("api")
        if api is None:
            return {"error": "尚未連線，且本機沒有這天的資料"}
        try:
            contract = getattr(api.Contracts.Futures, PRODUCT)[f"{PRODUCT}R1"]
            df = pd.DataFrame({**api.kbars(contract, start=str(d), end=str(d))})
        except Exception as e:
            return {"error": str(e)[:120]}

    feats = None
    try:
        if df is None or df.empty:
            return {"date": str(d), "bars": [], "trades": [], "tf": tf}
        df["ts"] = pd.to_datetime(df["ts"])
        g = df[(df["ts"].dt.time >= SESSION_OPEN)
               & (df["ts"].dt.time < DAY_END)].sort_values("ts")
        if d == date.today():
            g = overlay_live(g)
        bars = to_timeframe(g, tf)
        if tf == 1:
            # 逐根的客觀盤面（重播時「目前這一刻」要跟著更新）——只有 1 分 K 對得準
            feats = day_features(bars, d)
    except Exception as e:
        return {"error": str(e)[:120]}

    out = {"date": str(d), "bars": bars, "trades": _day_trades(d), "tf": tf}
    if feats is not None:
        out["feats"] = feats
    return out


_LOCAL_PX = {"df": None, "mtime": None}


def local_bars(d):
    """從本機的 tmf_1min.csv 取某一天的 1 分 K；沒有就回 None。"""
    f = HERE / "tmf_1min.csv"
    if not f.exists():
        return None
    m = f.stat().st_mtime
    if _LOCAL_PX["df"] is None or _LOCAL_PX["mtime"] != m:
        px = pd.read_csv(f)
        px["ts"] = pd.to_datetime(px["ts"])
        _LOCAL_PX.update({"df": px, "mtime": m})
    px = _LOCAL_PX["df"]
    g = px[px["ts"].dt.date == d]
    return g.copy() if len(g) else None


def _local_span():
    """本機 tmf_1min.csv 涵蓋的日期範圍 (最早, 最晚)；沒有檔案回 (None, None)。"""
    f = HERE / "tmf_1min.csv"
    if not f.exists():
        return None, None
    if _LOCAL_PX["df"] is None or _LOCAL_PX["mtime"] != f.stat().st_mtime:
        local_bars(date.today())            # 觸發載入
    px = _LOCAL_PX["df"]
    if px is None or px.empty:
        return None, None
    dd = px["ts"].dt.date
    return dd.min(), dd.max()


def _raw_days(days, report=None):
    """
    取這幾天的原始 1 分 K（含夜盤）。本機 tmf_1min.csv 優先，缺的才跟永豐要。

    本機檔涵蓋範圍「之內」卻沒資料的日子＝休市，不必再問永豐 ——
    否則每畫一次圖就要為週末白跑兩次 API。
    永豐的區間端點碰到非交易日會整段回 404，所以缺的日子一天一天抓、失敗就跳過。

    【本機檔的最後一天永遠是半天】排程 14:10 跑 append_today，那時當晚的夜盤
    （15:00~23:59）根本還沒發生 —— 所以 csv 的最後一天只有 00:00~13:45。
    若照「在範圍內就當作已完整」處理，最新那個交易日的夜盤永遠拿不到，
    session_frame() 也就永遠拼不出完整的交易日（2026-08-23 實測 08-21 只有半天）。
    因此 dd >= 本機最後一天時，**一律再跟永豐要一次**，再與本機資料合併
    （下面 drop_duplicates("ts") 會濾掉重複的分鐘），不是二選一。
    """
    _lo, hi = _local_span()
    frames, missing = [], []
    for dd in days:
        g = local_bars(dd)
        if g is not None:
            frames.append(g)
        # hi 是 None＝本機根本沒有檔案；dd >= hi＝本機那天可能還沒收完（見上面說明）。
        # 只有「dd 落在本機檔範圍內、而且比最後一天早」才敢斷定是休市日、不問永豐。
        if hi is None or dd >= hi:
            missing.append(dd)
    if missing:
        api = SESSION_REF.get("api")
        contract = None
        if api is not None:
            try:
                contract = getattr(api.Contracts.Futures, PRODUCT)[f"{PRODUCT}R1"]
            except Exception as e:
                print(f"[K線] 取不到合約：{str(e)[:80]}")
        if contract is None and report is not None:
            # 【要得到卻要不到】有日子需要跟永豐補（多半是「昨天的夜盤」與今天），
            # 但這一刻還沒連上。回傳的資料是「能拿到的部分」，不是完整的 ——
            # 呼叫端（_cached_raw）必須知道，否則會把半成品當成正解永久留著。
            report["incomplete"] = True
        if contract is not None:
            for dd in missing:
                try:
                    df = pd.DataFrame({**api.kbars(contract, start=str(dd), end=str(dd))})
                except Exception:
                    continue                # 多半就是休市日
                if not df.empty:
                    df = df.copy()
                    df["ts"] = pd.to_datetime(df["ts"])
                    frames.append(df)
    if not frames:
        return None
    out = pd.concat(frames, ignore_index=True)
    out["ts"] = pd.to_datetime(out["ts"])
    return out.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


_SESS_BACK = {}     # 交易日 → 它前五天的原始 K 棒（過去的資料不會變，永久留著）
_SESS_OWN = {}      # 日期 → 該日的原始 K 棒（只快取過去的日子，今天的還在長）
_TODAY_RAW = {}     # 今天的原始 K 棒：短期快取，見 TODAY_TTL
SESS_RETRY = 60     # 抓不到資料時隔多久再試一次
# 「拿到了但不完整」要更勤地重試：那段期間畫面上是缺夜盤的，撐 60 秒太久
SESS_RETRY_PARTIAL = 8
# 今天的 kbars 一分鐘才變一次，不必每次要圖都跟永豐重抓（實測整支 0.5~0.85 秒）。
# 還沒收完的那幾分鐘由 overlay_live() 用即時 tick 換掉，所以壓成 20 秒不會讓畫面變舊。
TODAY_TTL = 20
TODAY_RETRY_EMPTY = 120


def _csv_stamp():
    """tmf_1min.csv 的 mtime，當快取鍵用（檔案換了就代表資料可能補齊了）。"""
    f = HERE / "tmf_1min.csv"
    return f.stat().st_mtime if f.exists() else None


def _cached_raw(cache, key, days, flags=None):
    """
    _raw_days 的快取層。抓到就留著；抓不到隔 SESS_RETRY 秒再試。

    【不能把失敗也永久快取】面板啟動時可能還沒連上永豐，那一瞬間抓不到資料 ——
    若把 None 記起來，K 線圖就會整天空著，重開面板才會好。

    【也不能不管 csv 有沒有更新】某天在它還是「本機檔最後一天」的時候被快取起來，
    拿到的是半天（夜盤還沒發生）；排程 14:10 併檔補上夜盤之後，快取若不失效
    就會一直是半天。所以快取鍵要帶上 csv 的 mtime（day_index() 同樣的作法）。
    """
    stamp = _csv_stamp()
    hit = cache.get(key)
    if hit is not None and hit.get("mt") == stamp:
        # 完整拿到的才永久留著；「還沒連上永豐、只拿到本機那半份」要隔一陣子再試。
        # 【踩過】面板啟動的頭幾秒還沒連線，那份缺了昨晚夜盤的結果被當成成功存起來，
        # 而快取鍵只帶 csv 的 mtime、csv 要隔天 14:10 才會再變 ⇒ 圖整天都少一段夜盤，
        # 夜盤基準往回跳到更早的日子（2026-08-25：週二的圖接的是上週五的夜盤）。
        if hit["df"] is not None and not hit.get("partial"):
            return hit["df"]
        # 不完整的那份重試要勤一點：舊值 60 秒代表「畫面上的錯資料要撐一分鐘」
        wait = SESS_RETRY_PARTIAL if hit.get("partial") else SESS_RETRY
        if time.time() - hit["at"] < wait:
            if flags is not None and hit.get("partial"):
                flags["partial"] = True
            return hit["df"]
    rep = {}
    df = _raw_days(days, report=rep)
    part = bool(rep.get("incomplete"))
    cache[key] = {"df": df, "at": time.time(), "mt": stamp, "partial": part}
    if flags is not None and part:
        flags["partial"] = True
    return df


def _today_raw(d):
    """
    今天的原始 1 分 K，帶 TODAY_TTL 秒的短快取。

    【為什麼要快取】前端每 3 秒要一次圖，這裡不快取就是每 3 秒跟永豐打一次 kbars ——
    實測 /api/bars 整支要 0.5~0.85 秒，幾乎都花在這。而今天的 kbars 一分鐘才變一次。
    【為什麼快取 20 秒不會讓畫面變舊】還沒收完的那幾分鐘是 overlay_live() 用即時
    tick 現算的，不靠 kbars；kbars 只負責已經收完的那些分鐘。
    """
    hit = _TODAY_RAW.get(d)
    if hit is not None and time.time() - hit["at"] < hit["ttl"]:
        return hit["df"]
    df = _raw_days([d])
    _TODAY_RAW.clear()          # 只留今天那一筆，跨日自然就沒了
    # 拿不到（多半是 kbars 逾時，一次卡 30 秒）⇒ 隔久一點再試，別讓每次畫圖都卡住
    bad = df is None or df.empty
    _TODAY_RAW[d] = {"df": df, "at": time.time(), "ttl": TODAY_RETRY_EMPTY if bad else TODAY_TTL}
    return df


def session_frame(d):
    """
    交易日 d 的完整 1 分 K：前一個交易日 15:00 的夜盤 → d 的日盤 13:45 收盤。
    回傳 (DataFrame, base)；base 是夜盤開盤那一刻，給 to_timeframe 當分組原點。

    期貨的一個交易日是「前一晚夜盤 ＋ 當天日盤」。Benson 早上 08:45 下單前
    要看得到昨晚怎麼走，所以圖一定要含夜盤 —— 這是原本只畫 08:45~13:45 的缺口。

    【週一的「昨晚」是上週五】夜盤 15:00 開、延到隔天凌晨 05:00，週日沒有夜盤。
    所以夜盤開盤日不能用「d 減一天」，要往回找最近一個真的有 15:00 以後 K 棒的日子。

    【今天是滾動的】選過去的日期＝嚴格的交易日（到 13:45 收盤為止）；
    「今天（即時）」則會一路接到今晚的夜盤，看盤時線不會斷在半路。
    """
    today = date.today()
    key = str(d)

    flags = {}
    back = _cached_raw(_SESS_BACK, key, [d - timedelta(days=k) for k in range(5, 0, -1)],
                       flags)
    # 今天的 K 棒還在長，不能快取；過去的日子抓一次就夠
    own = _today_raw(d) if d == today else _cached_raw(_SESS_OWN, key, [d], flags)
    # 「不完整」有兩種，文案完全不同，所以要分得出來：
    #   night ＝ 夜盤那一段還沒到齊（剛啟動、還沒連上永豐）
    #   today ＝ **今天的 K 棒完全拿不到** ⇒ 圖上會整片都是昨晚，卻掛著今天的日期
    # 後者是 2026-09-02 他回報「加載完了但 K 圖還是不是最新的」的那個
    # （實測：109 根、最後一根停在 23:45，全部都是前一晚）。
    partial = "night" if flags.get("partial") else ""
    if (not partial and d == today
            and (own is None or getattr(own, "empty", True))
            and datetime.now().time() >= SESSION_OPEN):
        # 已經開盤了卻一根今天的 K 棒都沒有 ⇒ 拿不到，不是「還沒開始交易」
        partial = "today"

    pool = [x for x in (back, own) if x is not None and not x.empty]
    if not pool:
        return None, None, partial
    px = pd.concat(pool, ignore_index=True).drop_duplicates("ts").sort_values("ts")
    tt, dd = px["ts"].dt.time, px["ts"].dt.date

    rows, n = [], None
    # ⛔ 【資料不完整時，絕對不可以猜夜盤是哪一天】2026-09-02 他回報「K 線圖一直變來變去」，
    #    截圖是重啟後第 60 秒與第 93 秒：先畫出**前天**的夜盤，連上永豐後才換成昨晚的。
    #    根因就在下面這兩行 —— `n` 取的是「**手上這批資料裡**最近一個有夜盤的日子」。
    #    面板剛啟動還沒連上永豐時只讀得到本機 csv，而 csv 的最後一天永遠缺夜盤
    #    （排程 14:10 跑，那時夜盤還沒發生）⇒ 它就理直氣壯地挑到前天，畫出錯的一晚。
    #    程式其實知道這份不完整（`partial`），只是沒拿它擋畫面。
    #    現在：不完整就**只畫當天日盤**（那段是對的），夜盤等資料到齊再接上去。
    #    少一段可以看得出來，畫錯一天看不出來 —— 他早上是照這張圖決定要不要進場的。
    nights = px[(tt >= NIGHT_OPEN) & (dd < d)]
    # ⚠️ 只有「夜盤那份不完整」才不准猜；「今天拿不到」的時候**夜盤反而要畫出來**
    #    —— 那是手上唯一真的資料。（第一版沒分，把「只有夜盤」那個測項打紅了。）
    if partial == "night":
        nights = nights.iloc[0:0]
    if not nights.empty:
        n = nights["ts"].dt.date.max()                     # 夜盤開盤日
        rows.append(px[(dd == n) & (tt >= NIGHT_OPEN)])    # 當晚 15:00~23:59
        tail = n + timedelta(days=1)
        rows.append(px[(dd == tail) & (tt <= NIGHT_TAIL)])  # 隔天凌晨 ~05:01
    rows.append(px[(dd == d) & (tt >= SESSION_OPEN) & (tt < DAY_END)])   # 當天日盤
    if d == today:
        rows.append(px[(dd == d) & (tt >= NIGHT_OPEN)])    # 今晚的夜盤（只有即時才接）

    rows = [r for r in rows if not r.empty]
    if not rows:
        return None, None, partial
    g = pd.concat(rows, ignore_index=True).drop_duplicates("ts").sort_values("ts")
    base = (pd.Timestamp.combine(n, NIGHT_OPEN) if n is not None
            else pd.Timestamp.combine(d, SESSION_OPEN))
    return g, base, partial


def overlay_live(g):
    """
    用即時 tick 覆蓋掉「還沒收完」的那幾分鐘，回傳新的 1 分 K 表。

    【一定要在 1 分 K 這一層做】永豐的 kbars 會給一根還沒收完的當前分鐘，
    所以「kbars 涵蓋到哪一分鐘」永遠含當前這分鐘。如果等合成完 N 分 K 再補，
    那一分鐘會被當成「已經有了」而跳過 —— 畫面就一直吃永豐那份幾秒前的半成品，
    價格在跳、K 棒不動（Benson 2026-08-26 回報，實測差到 19 點）。
    同一層才能「換掉」而不是「加上去」，量也就不會重複計。

    只換 kbars 最後那一分鐘（含）之後的部分，前面已經收完的分鐘一律以永豐為準 ——
    面板自己累的量會因為斷線重連而少算，歷史的部分不要拿它去蓋。
    """
    st = CURRENT_STATE.get("today")
    if g is None or g.empty or st is None or not getattr(st, "minute_bar", None):
        return g
    now = datetime.now()
    today = now.date()
    rows = []
    for mi, b in st.minute_bar.items():
        hh, mm = divmod(int(mi), 60)
        t0 = dtime(hh, mm)
        if not (SESSION_OPEN <= t0 < DAY_END or t0 >= NIGHT_OPEN or t0 <= NIGHT_TAIL):
            continue                                   # 13:45~15:00 沒在交易
        # minute_bar 的鍵只有「分鐘」沒有日期 ⇒ ⭐ 11-12 改看 minute_ts（那一分鐘最後一筆成交的完整時間）；
        #    舊的 Today（還沒有 minute_ts）才退回原本的猜法：08:45 之前看到的「15:00 以後」是昨晚的
        w = (getattr(st, "minute_ts", None) or {}).get(mi)
        if hasattr(w, "date"):
            d0 = w.date()
        else:
            d0 = (today - timedelta(days=1)
                  if (now.time() < SESSION_OPEN and t0 >= NIGHT_OPEN) else today)
        rows.append({"ts": pd.Timestamp.combine(d0, t0) + pd.Timedelta(minutes=1),
                     "Open": float(b["o"]), "High": float(b["h"]),
                     "Low": float(b["l"]), "Close": float(b["c"]),
                     "Volume": float(b["v"])})
    if not rows:
        return g
    live = pd.DataFrame(rows)
    # ⛔ 2026-11-05 00:04 他回報「K 線圖只到下午 1 點 45」：過了午夜，minute_bar 裡
    #    **昨天日盤** 08:45~13:45 那幾分鐘被蓋上「今天」的日期 ⇒ 變成還沒發生的未來 K 棒，
    #    接在今晚夜盤後面（圖的最右邊是昨天的日盤、真正最新的夜盤反而擠在中間）。
    #    即時 K 棒不可能在未來 ⇒ 結束時間晚於「現在＋1 分」的一律丟掉。
    live = live[live["ts"] <= pd.Timestamp(now) + pd.Timedelta(minutes=1)]
    edge = g["ts"].max()                               # kbars 最後那一根（結束時間標記）
    live = live[live["ts"] >= edge]
    if live.empty:
        return g
    # 只丟掉「真的有即時版本可以取代」的那幾列，其餘原封不動
    keep = g[~g["ts"].isin(set(live["ts"]))]
    return pd.concat([keep, live], ignore_index=True).sort_values("ts")


def to_timeframe(g, minutes, base=None):
    """
    把 1 分 K 合成 N 分 K，並以每根的「起始時間」標示（跟看盤軟體一致）。

    base: 分組的原點。日盤單獨一張圖時是當天 08:45；
          含夜盤的完整交易日則是夜盤開盤那一刻（前一交易日 15:00），
          這樣夜盤與日盤才會落在同一套格線上（15:00 到隔天 08:45 剛好 1065 分鐘，
          是 5 的倍數，所以 5 分 K 對得起來）。

    【關鍵：永豐的 1 分 K 用結束時間標記】
    日盤 08:45 開盤，但第一根的標籤是 08:46 —— 它涵蓋的是 08:45~08:46。
    若直接照標籤切 5 分鐘，第一根只會包到 08:46~08:49（四分鐘），
    08:50 那根會被推到下一格，整串往前偏一格，看起來就比大戶投「快一根」。
    所以先把時間往回挪一分鐘還原成「起始時間」，再分組。
    """
    if g.empty:
        return []
    g = g.copy()
    start_ts = g["ts"] - pd.Timedelta(minutes=1)          # 還原成該根的起始時間
    if base is None:
        base = pd.Timestamp.combine(g["ts"].iloc[0].date(), SESSION_OPEN)
    base = pd.Timestamp(base)
    g["slot"] = ((start_ts - base).dt.total_seconds() // (minutes * 60)).astype(int)
    out = []
    for _, blk in g.groupby("slot", sort=True):
        start = base + pd.Timedelta(minutes=minutes * int(blk["slot"].iloc[0]))
        # 帶上日期：跨夜的圖上「22:00」與「10:00」會同時出現，
        # 前端要靠它畫日期分隔線、也要靠它分辨哪幾根是夜盤。
        out.append({"t": start.strftime("%H:%M"), "d": start.strftime("%Y-%m-%d"),
                    "o": float(blk["Open"].iloc[0]), "h": float(blk["High"].max()),
                    "l": float(blk["Low"].min()), "c": float(blk["Close"].iloc[-1]),
                    "v": float(blk["Volume"].sum())})
    return out


def traded_days():
    """
    回顧用的日期清單。有練習紀錄的排前面（那些才是他想回顧的），
    後面補上本機有資料的最近交易日，方便看沒下單的日子長什麼樣。

    另外一定補上「最近幾個平日」—— 排程每天 14:10 才把當天併進 tmf_1min.csv，
    只看本機檔的話，選單裡永遠選不到昨天（他早上最想翻的就是昨天）。
    休市日選下去會是空的，但那不會壞事。
    """
    traded = []
    if trade_dir().exists():
        traded = sorted([f.stem for f in trade_dir().glob("*.json")
                         if json.loads(f.read_text(encoding="utf-8") or "[]")], reverse=True)
    others = []
    f = HERE / "tmf_1min.csv"
    if f.exists():
        try:
            if _LOCAL_PX["df"] is None or _LOCAL_PX["mtime"] != f.stat().st_mtime:
                local_bars(date.today())        # 觸發載入
            px = _LOCAL_PX["df"]
            if px is not None:
                # 週六在檔案裡也有 K 棒（週五夜盤延到週六凌晨），但它不是交易日 ——
                # 選下去只會看到半截夜盤，所以不列進選單。
                all_days = sorted({str(x) for x in px["ts"].dt.date
                                   if x.weekday() < 5}, reverse=True)
                others = [x for x in all_days if x not in traded][:20]
        except Exception:
            pass
    recent = []
    for k in range(0, 12):
        dd = date.today() - timedelta(days=k)
        if dd.weekday() < 5:                     # 週六日沒有日盤
            recent.append(str(dd))
    others = [x for x in recent if x not in traded and x not in others] + others
    return {"traded": traded, "others": others[:24]}


_DAYIDX = {"key": None, "days": None}


def day_index(n=70):
    """
    日期選單（迷你月曆）用的清單：最近 n 個交易日，每天附上日盤漲跌、震幅、練習結果。

    月曆要能一眼看出「哪幾天在動、哪幾天有下單」，所以不能只給日期字串。
    三種日子要分得出來：
      有 stats     本機 csv 裡有那天的日盤 K 棒 → 紅綠、震幅都畫得出來
      closed=True  在 csv 涵蓋範圍內、卻沒有日盤 K 棒 → 休市，選單裡灰掉不能點
                   （原本可以選，點下去是一張空白圖）
      兩者皆非     csv 最後一天之後的平日。排程 14:10 才併檔，今天與昨天常常還沒進去 ——
                   這些仍然要能選，只是沒有紅綠可畫。
    """
    f = HERE / "tmf_1min.csv"
    mt = f.stat().st_mtime if f.exists() else None
    pt = 0.0
    if trade_dir().exists():
        pt = max([p.stat().st_mtime for p in trade_dir().glob("*.json")], default=0.0)
    key = (mt, pt, len(today_trades()), str(date.today()))
    if _DAYIDX["key"] == key:
        return _DAYIDX["days"]

    stats, lo, hi = {}, None, None
    if f.exists():
        if _LOCAL_PX["df"] is None or _LOCAL_PX["mtime"] != mt:
            local_bars(date.today())            # 觸發載入
        px = _LOCAL_PX["df"]
        if px is not None and not px.empty:
            t = px["ts"].dt.time
            dayp = px[(t >= SESSION_OPEN) & (t < DAY_END)]
            if not dayp.empty:
                g = dayp.groupby(dayp["ts"].dt.date).agg(
                    h=("High", "max"), l=("Low", "min"), c=("Close", "last")).tail(n + 1)
                prev = g["c"].shift(1)
                lo, hi = g.index.min(), g.index.max()
                for d0, row in g.iterrows():
                    p = prev.loc[d0]
                    stats[d0] = {
                        "c": int(row["c"]), "rng": int(row["h"] - row["l"]),
                        "chg": None if pd.isna(p) else int(row["c"] - p),
                        "pct": None if pd.isna(p) else round(float((row["c"] - p) / p * 100), 2),
                    }

    cand = set(stats)
    if trade_dir().exists():
        for p in trade_dir().glob("*.json"):
            try:
                cand.add(datetime.strptime(p.stem, "%Y-%m-%d").date())
            except ValueError:
                pass
    for k in range(0, 16):                      # 最近兩週的平日一定要在（含今天）
        d0 = date.today() - timedelta(days=k)
        if d0.weekday() < 5:
            cand.add(d0)

    days = []
    for d0 in sorted(cand)[-n:]:
        s = stats.get(d0)
        try:
            tr = _day_trades(d0)
        except Exception:
            tr = []
        days.append({
            "d": str(d0), "w": "一二三四五六日"[d0.weekday()],
            "closed": s is None and lo is not None and lo <= d0 <= hi,
            "n": len(tr),
            "net": int(round(sum(x.get("_net") or 0 for x in tr))) if tr else None,
            **(s or {}),
        })
    _DAYIDX.update({"key": key, "days": days})
    return days


def closed_days():
    """最近的休市平日（國定假日、補假）⇒ ['YYYY-MM-DD', …]。
    ⭐ 2026-10-14：10-09 國慶補假，【自動下單】寫「09:03:30 收不到成交價」、夜盤寫「報價不新鮮」，
       看起來像壞掉 ⇒ 畫面改說「休市」。⛔ 判斷沿用月曆那一把尺（`day_index()` 的 closed：
       本機 1 分 K 範圍內、那天卻沒有日盤）——⛔ 不另寫一份行事曆。
    ⚠️ 本機檔每天清晨才併進前一天 ⇒ **當天**的假日要到隔天才認得出來（當天照舊寫收不到報價）。"""
    out = {x["d"] for x in day_index() if x.get("closed")}
    # ⭐ 2026-11-02：連假剛結束那晚本機 1 分 K 還認不出來 ⇒ 10-27 夜盤誤判振幅資料太舊、少送 1 口；併入模擬判定的休市日
    try:
        if sim_lanes_mod() is not None:
            out.update(sim_lanes_mod().holiday_days())
    except Exception:                          # noqa: BLE001
        pass
    return sorted(out)


# ------------------------------------------------- 【細節】分頁：逐筆早盤圖的資料層
#
# 資料源是 tick_writer.py 落地的 tick_logs/YYYY-MM-DD.jsonl。
#
# ⛔ 這一段一行都不碰 on_tick、不碰佇列、不碰 state_lock。
#    「從記憶體佇列直接拿最新的 tick 就不用等 flush」的誘惑很大，但 on_tick 跑在永豐
#    SDK 的回呼執行緒上，**它更新的價就是停損看的那個價**（永豐沒有停損單，停損活在
#    Python 迴圈裡）。那條路上多一個讀鎖就是多一次可能塞住他的停損。
#    代價是今天的最後一筆有 ≤1.5 秒的落地延遲（flush_every=1.0），畫面上直接寫出來。
#
# ⛔ 解析（一天約 0.3 秒）跑在 ThreadingHTTPServer 的請求執行緒上，
#    只准拿 TICK_LOCK；拿到 state_lock 就是卡住整個面板的報價。

# 這一頁吃**兩種**檔，而且畫面上一定要分得出來（2026-09-07 加）：
#   ① 逐筆    tick_logs/YYYY-MM-DD.jsonl          面板自己的 tick_writer.py 寫的
#   ② 取樣    tick_logs/YYYY-MM-DD-polled.jsonl   tick_recorder.py 每 0.1 秒讀 /api/state
# ⛔ 「不要讓取樣冒充逐筆」的正解是**標示清楚**，不是整個不給看。
#    （原本只收 ①，於是 09-07 早上那份唯一的行情紀錄在畫面上完全不存在。）
#
# ⛔⛔ 但是**檔名放寬 ≠ schema 檢查放寬**。一個叫 `YYYY-MM-DD.jsonl`（逐筆檔名）
#      內容卻是取樣 schema 的檔，**必須繼續被擋掉並列進 skipped** —— 那是這整條
#      規則的原始目的（`tick_recorder.py` 加 -polled 後綴**之前**留下來的檔就長這樣：
#      幾千列、一個 k 欄位都沒有，光看檔名會把它當成逐筆，畫出一張空圖或直接爆掉）。
#      所以兩種檔名各有各的嗅探，`_tick_sniff(path, kind)` 的 kind 一定要跟檔名一致。
_TICK_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")
_TICK_POLLED_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})-polled\.jsonl$")
TICK_SEC0 = 8 * 3600 + 45 * 60      # 08:45:00 的當日秒數（前端算 x 的原點）
# ⛔ 這一頁的時間軸是**固定的 08:45:00~09:30:00**，所以「一天最多 2,700 個桶」是
#    前端效能與版面的**不變式**，不是巧合（見 TICK-TAB-SPEC.md §6.2）。
#    逐筆檔靠 tick_writer 的窗口自然落在裡面，但**取樣檔不是** ——
#    `tick_recorder.py --until 13:45` 是它自己說明裡就有的用法，那種檔一路錄到下午，
#    照收就會給出 8,000+ 個桶、打破不變式（關掉「時間軸固定」之後整個早上被壓成一小段）。
#    所以解析時一律切窗口，**而且被切掉幾列要講出來**（outwin）——「安靜地少」是禁止的。
TICK_SPAN = 45 * 60                 # 2,700 秒；桶的秒數必須落在 [SEC0, SEC0+SPAN)
TICK_SNIFF = 8192                   # 嗅探只讀開頭這麼多位元組
TICK_KEEP = 8                       # 解析結果最多留幾天（一天最多 2,700 個桶）

TICK_CACHE = {}                 # 日期字串 -> 解析結果（含 byte 位移，今天走增量）
TICK_LOCK = threading.Lock()    # ⛔ 只保護這份快取，絕對不可以碰 state_lock
_TICK_SNIFFED = {}              # (檔名, size, mtime) -> 嗅探結果（同一版不重讀）
_TICK_SAID = set()              # 已經在主控台喊過的檔（不要每次列目錄都洗版）


def _tick_sec(t):
    """
    把 "HH:MM:SS.mmm"（或 "HH:MM"）換成當日秒數。看不懂就回 None —— **不猜**。

    ⚠️ 逐筆檔裡的 t 是**永豐給的交易所時間**（檔頭 clock 那句），日期看檔名。
    """
    if not isinstance(t, str) or len(t) < 5 or t[2] != ":":
        return None
    if len(t) >= 6 and t[5] != ":":
        return None
    try:
        h, m = int(t[0:2]), int(t[3:5])
        s = int(t[6:8]) if len(t) >= 8 else 0
    except ValueError:
        return None
    return h * 3600 + m * 60 + s


def _tick_num(x):
    """
    數值欄位一律過這道。回 float 或 None（看不懂就 None，**不猜**）。

    ⛔ 為什麼要有這個：jsonl 裡只要**一列**的 price／p／v 是字串（或 null 之外的東西），
       舊寫法會在 `px > row[1]`（str vs float）或 `round(float(px), 1)` 當場炸掉，
       例外一路冒到 handler ⇒ **那一天整份回 500、1,091 列好資料一列都看不到**
       （lab-qa 2026-09-07 實測）。
       這個專案禁止「安靜地少」，**「大聲地全沒了」同樣不可接受** ——
       一列壞資料只准弄掉那一列（計入 bad），不准弄掉一整天。
    bool 要另外擋：`isinstance(True, int)` 是 True，會被當成價格 1.0。
    """
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    x = float(x)
    # NaN（x != x）與 inf 都會讓後面的 min/max 與 round 產生沒有意義的結果
    return x if x == x and -1e12 < x < 1e12 else None


def _tick_inwin(sec):
    """這個秒數落在 08:45:00~09:30:00 裡面嗎（見 TICK_SPAN 那條註解）。"""
    return sec is not None and TICK_SEC0 <= sec < TICK_SEC0 + TICK_SPAN


def _tick_iso_hms(t):
    """
    取樣檔的 `t` 是**完整 ISO 到毫秒**（"2026-09-07T09:04:51.706"），
    逐筆檔那邊是 "HH:MM:SS.mmm"。把兩者都收斂成 "HH:MM:SS.mmm"，看不懂就回 None。

    ⚠️ 日期一律看**檔名**，不看這一欄 —— 跟逐筆檔同一條規則（檔名是唯一的日期來源），
       這樣「今天還在長」的增量與快取才有同一把尺。
    """
    if not isinstance(t, str):
        return None
    if len(t) >= 11 and t[10] == "T":
        t = t[11:]
    return t if _tick_sec(t) is not None else None


def _tick_sniff(path, kind="tick"):
    """
    這個檔是不是它檔名宣稱的那種格式？**只讀開頭 8KB，不解析整個檔。**

    kind="tick"    ：至少要有一列 json 物件帶 `k` 欄位。
                     ⛔ 這道不准放寬 —— 取樣檔一個 k 都沒有，靠它才擋得住
                        「逐筆檔名 ＋ 取樣內容」那種檔（見上面的註解）。
    kind="polled"  ：至少要有一列 json 物件帶 `price` ＋ 讀得懂的 `t`，
                     而且**不准帶 `k`**（帶了就是逐筆檔被改錯名，一樣不收）。

    回 {"ok","v","saw_t","whole","reason"}；同一版的檔（name+size+mtime）不重讀。
    """
    try:
        st = path.stat()
    except OSError:
        return {"ok": False, "v": None, "saw_t": False, "whole": True,
                "reason": "讀不到這個檔"}
    key = (path.name, st.st_size, st.st_mtime, kind)
    hit = _TICK_SNIFFED.get(key)
    if hit is not None:
        return hit
    try:
        with path.open("rb") as f:
            raw = f.read(TICK_SNIFF)
    except OSError:
        raw = b""
    whole = st.st_size <= TICK_SNIFF
    lines = raw.split(b"\n")
    if not whole:
        lines = lines[:-1]      # 8KB 剛好切在一列中間，最後那一段丟掉
    ok = saw_t = False
    ver = None
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            o = json.loads(ln)
        except Exception:
            continue            # 壞列跳過（真正的計數在 _tick_load，這裡只是嗅探）
        if not isinstance(o, dict):
            continue
        if kind == "polled":
            # ⛔ 帶 k ＝ 那是逐筆列，不是取樣列（檔名跟內容對不起來，一樣不收）
            if "k" in o or o.get("price") is None or _tick_iso_hms(o.get("t")) is None:
                continue
            ok = saw_t = True
            continue
        if "k" not in o:
            continue
        ok = True
        if o.get("k") == "h" and isinstance(o.get("v"), int):
            ver = o["v"]
        elif o.get("k") == "t":
            saw_t = True
    out = {"ok": ok, "v": ver, "saw_t": saw_t, "whole": whole,
           "reason": ("" if ok else
                      ("沒有任何一列長得像取樣快照：要有 price ＋ 讀得懂的 t、而且不帶 k"
                       if kind == "polled" else
                       "沒有任何一列帶 k 欄位；可能是輪詢取樣檔被取成逐筆的檔名"))}
    if len(_TICK_SNIFFED) > 500:
        _TICK_SNIFFED.clear()
    _TICK_SNIFFED[key] = out
    return out


def tick_days():
    """
    有哪幾天有紀錄。**只准列目錄 ＋ 8KB 嗅探 ＋ stat。**

    ⛔ 不可以在這裡解析整個檔：一年後這裡有 240 個檔、每個 5MB，每次列目錄就是一分多鐘，
       而且發生在「他切到【細節】分頁」的當下。筆數／涵蓋時段／缺口一律等他真的切到
       那一天，由 /api/tick/day 給 —— 所以清單裡沒載入過的日子**不顯示估算筆數**
       （這個專案不放沒把握的數字）。

    每一天帶一個 `kind`：
      "tick"    逐筆（tick_writer.py）
      "polled"  取樣（tick_recorder.py，約 0.5 秒一筆、**沒有單筆成交量**）
    ⛔ 這個欄位不是裝飾：前端靠它在圖上、清單上、副標上明講「這天是取樣不是逐筆」。

    【同一天兩種檔都有的時候】**逐筆優先**，另一種標成 `alt: True`（清單會寫「另有取樣檔」）。
    理由：逐筆是取樣的上位集合（它有每一筆與成交量，取樣兩者都沒有），
    而且逐筆是面板自己寫的、不會因為外掛工具中途被關掉而少一段。
    現在不會同時發生，但 `tick_recorder.py` 還在，哪天他盤中又拿它錄一次就會。
    """
    found, skipped = {}, []
    if tick_dir().exists():
        for p in sorted(tick_dir().iterdir()):
            if not p.is_file():
                continue
            m = _TICK_NAME.match(p.name)
            kind = "tick"
            if not m:
                m = _TICK_POLLED_NAME.match(p.name)
                kind = "polled"
            if not m:
                continue
            d = m.group(1)
            s = _tick_sniff(p, kind)
            if not s["ok"]:
                # ⛔ 不要靜靜跳過（會變成「我明明有錄怎麼看不到」的無解客訴），
                #    也 ⛔ 不准自作主張改名或刪除 —— 那是 Benson 的資料。
                skipped.append(d)
                if p.name not in _TICK_SAID:
                    _TICK_SAID.add(p.name)
                    print(f"⚠️ [tick] 跳過 {p.name}："
                          f"內容不是{'取樣' if kind == 'polled' else '逐筆'}格式"
                          f"（{s['reason']}）", flush=True)
                continue
            try:
                dt = datetime.strptime(d, "%Y-%m-%d").date()
            except ValueError:
                continue
            row = {
                "d": d, "w": "一二三四五六日"[dt.weekday()], "kind": kind, "alt": False,
                # empty＝整個檔都在 8KB 之內、而且一列資料都沒有（只有檔頭）。
                # 檔案比 8KB 大時嗅探看不到全貌，一律當成有資料 —— 切進去就知道了。
                "empty": bool(s["whole"] and not s["saw_t"])}
            old = found.get(d)
            if old is None:
                found[d] = row
            else:
                # 兩種都有 ⇒ 留逐筆那份，另一份只留一個「還有別的檔」的痕跡
                keep = old if old["kind"] == "tick" else row
                keep["alt"] = True
                found[d] = keep
    days = sorted(found.values(), key=lambda x: x["d"], reverse=True)
    return {"days": days,
            "since": days[-1]["d"] if days else None,
            "skipped": sorted(set(skipped), reverse=True),
            # 前端切「今天／過去」一律用這個，⛔ 不可以用瀏覽器的 new Date()
            # —— 跨午夜那一刻兩邊會不同一天（面板已經為這件事踩過一次）。
            "today": str(date.today())}


def _tick_blank(kind):
    """一份空的快取條目。逐筆與取樣共用同一個形狀，前端才不必寫兩套。"""
    return {"off": 0, "b": {}, "bl": {}, "ah": {}, "gaps": [], "n": 0, "bad": 0,
            "heads": 0, "v": None, "vmix": False, "first": None, "last": None,
            "mtime": 0, "size": 0, "kind": kind,
            # outwin＝落在 08:45~09:30 之外、被切掉的列數（⛔ 少了東西一定要有一個數字）
            "outwin": 0,
            # 取樣專用：ms＝相鄰兩列的間隔（算中位數用）、nopx＝那一列根本沒有報價
            "ms": [], "nopx": 0}


def _tick_chunk(path, ent, kind):
    """
    共用的「續讀」：回 `(ent, chunk, st)`，`chunk is None` 代表快取還有效、不必重解析。
    **逐筆與取樣兩條路都走這裡**，所以下面這三道守衛只要壞一次，兩邊會一起紅。

    - 快取有效性要**同時**比 mtime 與 size。只比 mtime 的話，「內容變了、mtime 沒變」
      （看門狗同一秒內續寫、或檔案系統 mtime 只到秒）會讓**畫面停在舊資料**，
      而且畫面上完全看不出來。
    - 檔案變小（不該發生，但看門狗＋磁碟異常時可能）⇒ 位移歸零重讀整檔，不要硬接。
    - ⛔ 最後一列若不是以 `\\n` 結尾就丟掉、位移退回上一個換行處 ——
      append 寫入不是原子的，一定會讀到半列。
    """
    st = path.stat()
    if ent is not None and ent["mtime"] == st.st_mtime and ent["size"] == st.st_size:
        return ent, None, st
    if ent is not None and st.st_size < ent["off"]:
        ent = None
    if ent is None:
        ent = _tick_blank(kind)
    with path.open("rb") as f:
        f.seek(ent["off"])
        chunk = f.read()
    cut = chunk.rfind(b"\n")
    chunk = b"" if cut < 0 else chunk[:cut + 1]
    ent["off"] += len(chunk)
    return ent, chunk, st


def _tick_load_polled(d):
    """
    解析（或增量續讀）某一天的**取樣**檔（`tick_recorder.py` 的
    `YYYY-MM-DD-polled.jsonl`），轉成**跟逐筆完全同一種**的桶結構。
    **呼叫端必須持有 TICK_LOCK。**

    取樣列長這樣（欄位跟逐筆完全不同，**沒有 k**）：
        {"t":"2026-09-07T09:04:51.706","ms":464,"clock":"09:04:51","quote":"live",
         "price":47245.0,"bid":47242.0,"ask":47245.0,...}

    - `t` 是完整 ISO 到毫秒 ⇒ 用 `_tick_iso_hms()` 收斂成 "HH:MM:SS.mmm"。
    - `price` 當成那一秒的開/高/低/收（一列就是一個取樣點）。
    - `bid`/`ask` 進 bl/ah ⇒ **「折線＋價帶」那個圖種對取樣日照樣能用**。
    - ⛔⛔ **成交量一律 0**。取樣檔沒有單筆成交量，`vol_ratio` 是「累積量 ÷ 歷史中位」，
      **不是量**；拿它湊一個看起來像的量柱＝編資料。前端靠 `has_vol=False`
      把那顆疊圖做成 disabled ＋ 寫出原因。
    - `price` 是 null 的列（面板當下沒有報價）不算壞列，另外計 `nopx` ——
      「安靜地少」是這個專案明令禁止的失敗模式，看不到的東西要有一個數字。
    - `price`／`bid`／`ask` 一律過 `_tick_num()`：**一列是字串就讓整天回 500** 是
      「大聲地全沒了」，跟安靜地少一樣不可接受（lab-qa 2026-09-07 實測）。
    - ⛔ **08:45~09:30 之外的列切掉並計入 `outwin`**（`tick_recorder.py --until 13:45`
      是它自己說明裡的用法）—— 見 TICK_SPAN 那條註解。

    ⛔ **回 None 的條件是「沒有這個檔 **或** 嗅探不過」**，不是只有「檔案不存在」。
       嗅不過就等於沒有這種檔，呼叫端才不會拿到一份跟清單講的種類對不起來的東西。
    """
    path = tick_dir() / f"{d}-polled.jsonl"
    if not path.exists() or not _tick_sniff(path, "polled")["ok"]:
        return None
    key = d + "-polled"
    ent, chunk, st = _tick_chunk(path, TICK_CACHE.get(key), "polled")
    if chunk is None:
        return ent
    B, BL, AH = ent["b"], ent["bl"], ent["ah"]
    for ln in chunk.split(b"\n"):
        if not ln.strip():
            continue
        try:
            o = json.loads(ln)
        except Exception:
            ent["bad"] += 1
            continue
        if not isinstance(o, dict) or "k" in o:
            # ⛔ 帶 k ＝ 逐筆列跑進取樣檔裡。不猜、不吃，算進 bad。
            ent["bad"] += 1
            continue
        ts = _tick_iso_hms(o.get("t"))
        if ts is None:
            ent["bad"] += 1          # 時間看不懂就算壞列，⛔ 不准用 sec=0 頂替
            continue
        sec = _tick_sec(ts)
        if not _tick_inwin(sec):
            # 09:30 之後（`--until 13:45`）或 08:45 之前的列：不畫，但**要有數字**
            ent["outwin"] += 1
            continue
        ms = _tick_num(o.get("ms"))
        if ms is not None and ms >= 0:
            ent["ms"].append(int(ms))
        px = o.get("price")
        if px is None:
            ent["nopx"] += 1
            continue
        px = _tick_num(px)
        if px is None:
            ent["bad"] += 1          # 價格不是數字（字串／NaN）＝這一列讀不出來
            continue
        if ent["first"] is None or ts < ent["first"]:
            ent["first"] = ts
        if ent["last"] is None or ts > ent["last"]:
            ent["last"] = ts
        row = B.get(sec)
        if row is None:
            B[sec] = [px, px, px, px, 0]        # ⛔ 第五格（量）永遠是 0
        else:
            if px > row[1]:
                row[1] = px
            if px < row[2]:
                row[2] = px
            row[3] = px
        ent["n"] += 1
        # ⚠️ 讀不懂的 bid/ask 當成「這一列沒有買賣價」＝那個桶的價帶留白（前端本來就處理
        #    得了），**不計 bad** —— 價本身讀到了，把整列說成「讀不出來」反而是假話。
        bid, ask = _tick_num(o.get("bid")), _tick_num(o.get("ask"))
        if bid is not None and (BL.get(sec) is None or bid < BL[sec]):
            BL[sec] = bid
        if ask is not None and (AH.get(sec) is None or ask > AH[sec]):
            AH[sec] = ask
    ent["mtime"], ent["size"] = st.st_mtime, st.st_size
    TICK_CACHE[key] = ent
    if len(TICK_CACHE) > TICK_KEEP:
        for old in sorted(TICK_CACHE)[:len(TICK_CACHE) - TICK_KEEP]:
            if old != key:
                TICK_CACHE.pop(old, None)
    return ent


def _tick_load(d):
    """
    解析（或增量續讀）某一天的逐筆檔，回快取條目。**呼叫端必須持有 TICK_LOCK。**

    - 過去的日子：mtime/size 沒變就直接用快取，永久有效（檔案不會再長）。
    - 今天：走增量。記住上次讀到的 byte 位移，seek 過去只讀新增那段。
      快取有效性／檔案變小／讀到半列這三道在 `_tick_chunk()`（跟取樣那條路共用）。
    - 多列檔頭是正常的（看門狗重啟就多一列），數出來放進 heads。
    - 解析失敗的列**計數不丟棄地回報**（bad）。「安靜地少」是這個專案明令禁止的失敗模式。
    - 價量一律過 `_tick_num()`（理由見那個函式）；08:45~09:30 之外的列切掉並計入
      `outwin` —— 那個不變式（一天最多 2,700 個桶）是**整頁**的性質，不是某一種檔的，
      只在取樣那條路切等於在逐筆這條路埋同一顆地雷（有人動 `WATCH_END` 就會踩到）。

    ⛔ **回 None 的條件是「沒有這個檔 **或** 嗅探不過」**（2026-09-07 lab-qa 退件 M1）。
       舊寫法只看檔案存不存在 ⇒ 一個「逐筆檔名 ＋ 取樣內容」的檔被 `tick_days()` 的嗅探
       擋掉之後，`tick_day()` 照樣端得出來，而且會把同一天真正的取樣檔整份蓋掉：
       清單寫「取樣」、點進去圖上寫「逐筆」、沒有金籤、成交量那顆變回可以按、
       空狀態寫「這天只有檔頭，一筆成交都沒有錄到」（**一句假話**，那天有 1,091 列）。
       這個洞在 939dce2 就存在，只是那時取樣檔不進清單、那一天點不到而已。

    ⚠️ 檔案**不保證時間單調遞增**（檔頭的 order 欄位明講）。所以一個秒桶的「開」是
       **檔案裡第一次出現的那一列**，不是「那一秒最早的成交」。同一秒之內誰先誰後
       這張圖在任何縮放倍率下都畫不出來（一個像素 ≥ 0.06 秒），所以不去修它。
    """
    path = tick_dir() / f"{d}.jsonl"
    if not path.exists() or not _tick_sniff(path, "tick")["ok"]:
        return None
    ent, chunk, st = _tick_chunk(path, TICK_CACHE.get(d), "tick")
    if chunk is None:
        return ent
    B, BL, AH = ent["b"], ent["bl"], ent["ah"]
    for ln in chunk.split(b"\n"):
        if not ln.strip():
            continue
        try:
            o = json.loads(ln)
        except Exception:
            ent["bad"] += 1
            continue
        if not isinstance(o, dict):
            ent["bad"] += 1
            continue
        k = o.get("k")
        sec = None
        if k == "t" or k == "b":
            sec = _tick_sec(o.get("t"))
            if sec is None:
                ent["bad"] += 1
                continue
            if not _tick_inwin(sec):
                ent["outwin"] += 1      # 時段外：不畫，但要有數字
                continue
            ts = o["t"]
            if ent["first"] is None or ts < ent["first"]:
                ent["first"] = ts
            if ent["last"] is None or ts > ent["last"]:
                ent["last"] = ts
        if k == "t":
            p = _tick_num(o.get("p"))
            if p is None:
                ent["bad"] += 1         # 沒有價、或價不是數字（字串／NaN）＝讀不出來
                continue
            # ⚠️ 量維持 int：_tick_num 回 float，直接用會讓 payload 變成 "1.0" 這種寫法
            v = int(_tick_num(o.get("v")) or 0)
            row = B.get(sec)
            if row is None:
                B[sec] = [p, p, p, p, v]
            else:
                if p > row[1]:
                    row[1] = p
                if p < row[2]:
                    row[2] = p
                row[3] = p
                row[4] += v
            ent["n"] += 1
        elif k == "b":
            # 讀不懂的買賣價當成「這一列沒有買賣價」（同 _tick_load_polled 那條註解）
            bid, ask = _tick_num(o.get("b")), _tick_num(o.get("a"))
            if bid is not None and (BL.get(sec) is None or bid < BL[sec]):
                BL[sec] = bid
            if ask is not None and (AH.get(sec) is None or ask > AH[sec]):
                AH[sec] = ask
        elif k == "x":
            # 痕跡列刻意沒有 t（那一筆連進佇列都沒進來，沒有交易所時間可用）。
            # after＝同一批裡前一列的交易所時間＝缺口起點的下界。
            after = o.get("after")
            asec = _tick_sec(after) if after else None
            # ⚠️ n 也要過 _tick_num：`int("abc")` 會 ValueError ⇒ 整天回 500
            gn = _tick_num(o.get("n"))
            ent["gaps"].append({"sec": None if asec is None else asec - TICK_SEC0,
                                "n": int(gn or 0),
                                "why": str(o.get("why") or "?")[:40],
                                "after": after, "wt": o.get("wt")})
        elif k == "h":
            ent["heads"] += 1
            v = o.get("v")
            if isinstance(v, int):
                if ent["v"] is not None and ent["v"] != v:
                    ent["vmix"] = True
                ent["v"] = v
        # 不認得的 k 一律整列跳過（檔頭的 fmt 就是這樣約定的，之後加新種類不會弄壞舊程式）
    ent["mtime"], ent["size"] = st.st_mtime, st.st_size
    TICK_CACHE[d] = ent
    if len(TICK_CACHE) > TICK_KEEP:
        for old in sorted(TICK_CACHE)[:len(TICK_CACHE) - TICK_KEEP]:
            if old != d:
                TICK_CACHE.pop(old, None)
    return ent


def _tick_complete(d, now=None):
    """這一天的錄製結束了沒有。⛔ 用後端的日期，不可以讓前端拿 new Date() 判。"""
    now = now or datetime.now()
    today = str(now.date())
    if d != today:
        return True
    return now.time() > dtime(9, 31)


def tick_trades(d):
    """
    那一天的交易，給【細節】的「我的單」疊圖用。練習與真實都給，用 kind 分。

    ⚠️ 真實單的 exit／points 可能是 None（問不到成交價就留白，不拿現價冒充）——
       **照實傳 null**。前端把它餵進 `Math.min(lo, entry, exit)` 的話 null 會被當成 0、
       價格軸整個掉到 0（2026-09-02 踩過），所以前端進價格軸之前一定要先過濾。
    ⚠️ 練習紀錄只記到分（`time` 是 HH:MM），真實才有秒。
    """
    out = []
    try:
        dd = datetime.strptime(d, "%Y-%m-%d").date()
    except ValueError:
        return out
    try:
        for t in _day_trades(dd):
            out.append({"kind": "sim", "dir": t.get("dir"),
                        "entry": t.get("entry"), "exit": t.get("exit"),
                        "t_in": _tick_sec(t.get("time")),
                        "t_out": _tick_sec(t.get("_exit_time")),
                        "points": t.get("_points"), "why": t.get("_reason")})
    except Exception:
        pass
    try:
        f = broker.TRADE_DIR / f"{d}.jsonl"
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                out.append({"kind": "real", "dir": r.get("dir"),
                            "entry": r.get("entry"), "exit": r.get("exit"),
                            "t_in": _tick_sec(r.get("entry_time")),
                            "t_out": _tick_sec(r.get("exit_time")),
                            "points": r.get("points"), "why": r.get("reason")})
    except Exception:
        pass
    return out


def tick_day(d, frm=None):
    """
    一天份的 1 秒桶，columnar。⛔ 不要把 jsonl 整份丟給前端。

    實測（合成 135,001 列 / 7.70 MB 的一天）：原始 jsonl 7.70 MB vs 1 秒桶 columnar
    146 KB ＝ **小 53 倍**，而且**上限固定**（一天最多 2,700 個桶，不管他成交 8 萬還是
    20 萬筆）—— 逐筆的量會長，這一點比壓縮率更重要。

    1 秒是原子單位：繪圖區 976px、45 分鐘 ⇒ 一個像素 2.77 秒；就算放大到只看 1 分鐘，
    一根 1 秒 K 也已經佔 16px。1 秒桶保留開/高/低/收/量/該秒最低買價/最高賣價，
    「這一段摸到多高多低」完全沒有損失 —— 那正是 ±100 觸價規則唯一在意的東西。

    frm（`from=<秒>`，相對 sec0）：只回 s >= frm 的桶，**含 frm 本身**
    （那個桶上次拿到時可能還沒收完）。⛔ 前端合併規則是「同 s 覆蓋、其餘 append」，
    無腦 concat 會出現兩根同一秒的 K 棒 —— 圖上完全看不出來，只有量會變兩倍。

    【逐筆 vs 取樣】同一天兩種檔都在的話**逐筆優先**（理由見 tick_days()）。

    ⛔⛔ **「有沒有那種檔」問的是嗅探，不是 `path.exists()`**（2026-09-07 lab-qa 退件 M1）。
         `_tick_load()` / `_tick_load_polled()` 自己會先嗅探、嗅不過就回 None，所以
         下面這兩行是「先要逐筆，沒有**合格的**逐筆才退到取樣」。
         舊寫法按檔案存不存在挑 ⇒ 一個「逐筆檔名 ＋ 取樣內容」的檔（`tick_days()` 已經
         把它列進 skipped 了）照樣會被端出來，還把同一天真正的取樣檔整份蓋掉：
         **清單說取樣、點進去說逐筆、1,091 列真資料變成「一筆成交都沒有錄到」**。
         守衛：`tick-backend.py` ②c（每一天的 `tick_day().kind` 必須等於
         `tick_days()` 清單上那天的 kind），負控組就是「把這兩行對調」。
    回傳一定帶：
      kind    "tick" / "polled"   ⛔ 前端靠它在圖上明講「這天是取樣」
      has_vol 取樣日是 False      ⛔ 成交量疊圖要 disabled ＋ 寫出原因，
                                     絕不可以拿 vol_ratio 之類的東西湊一個假的量柱
      ms_med  取樣的中位間隔（毫秒），逐筆日是 None
      nopx    取樣時面板剛好沒有報價的列數
      outwin  08:45~09:30 之外、被切掉沒有畫的列數（⛔ 少了東西一定要有一個數字）
    """
    with TICK_LOCK:
        ent = _tick_load(d)
        if ent is None:
            ent = _tick_load_polled(d)
        if ent is None:
            return None
        secs = sorted(ent["b"])
        if frm is not None:
            secs = [s for s in secs if s - TICK_SEC0 >= frm]
        B, BL, AH = ent["b"], ent["bl"], ent["ah"]
        r1 = lambda x: None if x is None else round(float(x), 1)
        kind = ent.get("kind", "tick")
        msl = sorted(ent.get("ms") or [])
        out = {"date": d, "v": ent["v"], "vmix": ent["vmix"], "heads": ent["heads"],
               "sec0": TICK_SEC0,
               "kind": kind,
               # ⛔ 取樣檔沒有單筆成交量。這個 False 是量柱唯一的開關，不要另外找路。
               "has_vol": kind == "tick",
               "ms_med": (msl[len(msl) // 2] if msl else None),
               "nopx": ent.get("nopx", 0),
               "outwin": ent.get("outwin", 0),
               "s": [s - TICK_SEC0 for s in secs],
               "o": [r1(B[s][0]) for s in secs], "h": [r1(B[s][1]) for s in secs],
               "l": [r1(B[s][2]) for s in secs], "c": [r1(B[s][3]) for s in secs],
               "vq": [B[s][4] for s in secs],
               "bl": [r1(BL.get(s)) for s in secs], "ah": [r1(AH.get(s)) for s in secs],
               "gaps": [dict(g) for g in ent["gaps"]],
               "bad": ent["bad"], "n": ent["n"],
               "first": ent["first"], "last": ent["last"]}
    # 這兩個要讀別的檔／看時鐘，故意放在鎖外面
    out["complete"] = _tick_complete(d)
    out["trades"] = tick_trades(d)
    return out
