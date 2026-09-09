r"""
早盤趨勢面板（微台即時報價 + 大台歷史模型）— 本機網頁版
=============================================================================
即時接收台指期價格跳動，顯示「現在的趨勢往哪走、會走多強」。

做法：
  1. 即時算出當下盤面狀態（最近 5/15 分鐘動能、相對開盤、跳空、震幅、位階、量能）
  2. 到歷史裡找「同一時段、走勢長得最像」的日子（每天只取最像的一刻）
  3. 看那些日子接下來 5 / 10 / 15 分鐘怎麼走 → 方向機率 + 預期變動點數

【為什麼只給趨勢，不給勝率】
原本有「做多／做空吃到 ±100 的勝率」，已移除。實測結果：
  - 方向確實可預測：急跌後 10 分鐘上漲 63.7%、急漲後 43.9%，統計顯著，
    且 5/10/15 分鐘三個時間長度一致 —— 這部分是真的。
  - 但照這個方向下單賺不到錢：九種停損停利組合（100/100、50/100、25/75…）
    每筆都是負的，因為方向猜錯時賠得比猜對時賺得多。
所以面板只呈現「方向傾向」這個站得住腳的部分，不謊稱它能賺錢。

【兩個必要的統計修正，缺一數字就會虛高】
1. 一天只算一筆：每個歷史日只取「最像現在」的那一分鐘。
   若讓同一天貢獻多筆，等於假設你能在同一波行情裡反覆進場，
   實測會把數字灌水到 15 個百分點（60% vs 45%）。
2. 去趨勢：樣本期間台指期漲 81.7%，日盤中位漂移約 +25 點，已從結果扣除。
   剩下的才是「當下動能會不會延續」。

【為什麼沒有逐分鐘盤面存檔】
曾經每天存一份 08:45~09:30 的完整盤面，後來拿掉了：
那份資料可以用「日期＋時間」從 tmf_1min.csv 完整重建（排程每天自動累積），
存檔完全多餘，卻多一個會靜默失敗的零件 —— 2026-08-12 就真的存出了 0 分鐘還不報錯。

【這不是投資建議】只呈現歷史統計，不預測、不給買賣訊號。

執行：
  ..\..\.venv\Scripts\python.exe live_panel.py               # 正式
  ..\..\.venv\Scripts\python.exe live_panel.py --replay 2026-07-15   # 重播某天，先看效果
"""

import json
import math
import os
import queue
import re
import secrets
import sys
import threading
import time
import webbrowser
from datetime import date, datetime, timedelta
from datetime import time as dtime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np
import pandas as pd

import broker            # 真實下單。預設 dry run，見那個檔開頭的說明
import auto_fire         # 自動下單。預設**關著**（沒有 AUTO_ORDERS_ON 就完全不送）
import tick_writer       # 逐筆報價落地。⛔ 它的存在前提是「絕不在 on_tick 裡碰磁碟」

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).parent
REPO = HERE.parent.parent                       # trade-log/ 根目錄
SYNC_FILE = REPO / "data" / "practice.json"     # 練習紀錄的雲端同步檔
MATRIX = HERE / "intraday.csv"
CALIB = HERE / "calibration.json"     # 走查驗證產出的分段命中率
TRADE_DIR = HERE / "practice_trades"     # 模擬練習的交易紀錄
# 【回顧】分頁用的兩個檔案，刻意跟 practice_trades/ 分開放：
#   REPLAY_DIR    Bar Replay 的判斷紀錄。那是「事後重播」不是真的練習，
#                 混進 practice_trades/ 會污染他的勝率統計、也會被同步到 GitHub 給手機。
#   REVIEW_CACHE  進場當下盤面／MFE／MAE 的快取（歷史資料不會變，算過就留著）。
#                 同樣不能放進 practice_trades/ —— 那個資料夾是用 glob("*.json") 全讀的，
#                 多丟一個格式不同的檔進去，練習成績統計會壞掉。
REPLAY_DIR = HERE / "replay_log"
REVIEW_CACHE = HERE / "review_cache.json"

TP_POINTS = 100.0        # Benson 固定 ±100
SL_POINTS = 100.0
FEE_POINTS = 5.0         # 來回 NT$50 ÷ 每點 NT$10
PORT = 8770

SESSION_OPEN = pd.Timestamp("08:45").time()
WATCH_END = pd.Timestamp("09:30").time()
DAY_END = pd.Timestamp("13:45").time()
NIGHT_OPEN = pd.Timestamp("15:00").time()      # 夜盤 15:00 ~ 隔天 05:00
NIGHT_CLOSE = pd.Timestamp("05:00").time()
# 永豐的 K 棒用「結束時間」標記，夜盤最後一根標到 05:01 —— 收夜盤尾巴要含它，
# 否則每天都會少掉收盤前那一兩分鐘。
NIGHT_TAIL = pd.Timestamp("05:01").time()

# 逐筆報價落地：08:45~09:30（他的下單時段）的每一筆成交與買賣價寫進
# tick_logs/YYYY-MM-DD.jsonl。以前這些資料收完就丟，只留下 ticks 的計數，
# 一分鐘內發生什麼事永遠補不回來。
# ⛔ on_tick 只把資料 append 進記憶體佇列，寫檔在 tick_writer 自己的執行緒 ——
#    那條執行緒卡住最多是「這段沒錄到」，絕對不可以變成「停損慢了 200 秒」。
# 時段的單一來源在這裡（SESSION_OPEN / WATCH_END），tick_writer 自己的預設值只是備援。
TICK_DIR = HERE / "tick_logs"
TICKS = tick_writer.TickWriter(TICK_DIR,
                               win_start=SESSION_OPEN, win_end=WATCH_END)


def market_session(now=None):
    """
    現在是不是交易時段：'day'（日盤）／'night'（夜盤）／'closed'（休市）。

    只看時鐘與星期，**不含國定假日**（本機沒有假日表；假日會被判成 'day'）。
    用途是把「現在本來就沒有盤」跟「盤中卻收不到報價」分開 ——
    前者是正常的，後者才要示警。

    夜盤 15:00 開、延到隔天凌晨 05:00：
      週一~週五晚上有夜盤（週日晚上沒有），所以凌晨那段落在週二~週六。
    """
    now = now or datetime.now()
    t, wd = now.time(), now.weekday()          # 0=週一 … 6=週日
    if wd < 5 and SESSION_OPEN <= t < DAY_END:
        return "day"
    if wd < 5 and t >= NIGHT_OPEN:
        return "night"
    if 1 <= wd <= 5 and t < NIGHT_CLOSE:
        return "night"
    return "closed"


def quote_state(price, age, sess):
    """
    報價狀態，給前端判斷「能不能相信畫面上的數字、能不能下模擬單」。

      live    有新鮮的報價
      nodata  應該有盤卻收不到報價 —— 要示警（也可能是國定假日，本機無假日表）
      closed  休市中 —— 正常，不必示警；圖照畫，只是價格不是即時的

    這三態是 Bug A 的核心：以前兩種「沒報價」都叫 waiting，前端分不出來，
    就用同一道門把整個即時分頁（含 K 線圖與日期選單）擋掉。
    """
    if price is not None and age is not None and age <= STALE_SECONDS:
        return "live"
    return "nodata" if sess in ("day", "night") else "closed"


def boot_msg(now=None):
    """
    剛啟動、還沒收到第一筆報價時要說的話 —— **要看時鐘**。

    盤中重開面板時說「等待 08:45 開盤」是錯的：那時盤早就開了，
    真正的狀況是「連上了，還沒收到第一筆 tick」。
    """
    t = (now or datetime.now()).time()
    sess = market_session(now)
    if sess == "closed":
        return "已連線。現在不是交易時段，開盤後會自動進入即時模式。"
    if sess == "day" and t < SESSION_OPEN:
        return "已連線，等待 08:45 開盤…"
    return "已連線，正在等第一筆報價…（剛啟動時要幾秒）"


QUOTE_MSG = {
    "closed": "休市中 —— 現在不是交易時段，圖上顯示的是最後一根 K 棒的收盤價，不是即時價。",
    "nodata": "盤中卻收不到報價 —— 若今天是國定假日就是正常休市，否則是連線問題（程式每分鐘會自動重連）。",
}

CHART_TF = 5        # K 線圖用 5 分 K（Benson 看盤的習慣）
# 圖顯示整個日盤 08:45~13:45：5 分 K 共 60 根，剛好是一張看得舒服的圖；
# 他的下單時段 08:45~09:30 會在圖上以底色標出來。

# 全部用「幾倍的當時日常波動」比對，不用絕對點數 ——
# 台指期 2020 年 12,400 點、2026 年 45,000 點，同樣 40 點的意義差了三倍以上。
FEATURES = ["mom5_n", "mom15_n", "ret_open_n", "gap_n", "rng_n", "pos", "vol_ratio"]
FEATURE_WEIGHT = np.array([3.0, 2.0, 1.0, 0.8, 0.8, 1.5, 0.8])
# 「當下的趨勢」是 Benson 要的重點 → mom5 / mom15 權重最高
# 全部使用「微型臺指期貨」TMF —— Benson 實際交易、也是他在大戶投看的商品。
#
# 曾經考慮「歷史用大台 TXF、即時顯示用微台」來換取更長的樣本（1453 天 vs 約 450 天），
# 但混用兩個商品立刻出事：同一時刻 TXF 今日量 25,045 口、TMF 167,521 口，差 6 倍以上，
# 拿 TMF 的量去比對 TXF 的量能基準會讓模型一直誤判成「今天爆量」；跳空、動能同理。
#
# 兩者的走勢在統計上是同一個東西（同一天 1 分鐘變動中位數都是 22.0 點），
# 所以改成全部用微台：樣本少一些，但沒有任何混用風險，而且模型描述的就是他真正交易的商品。
PRODUCT = "TMF"

MINUTE_WINDOW = 3          # 只跟前後 3 分鐘的歷史時刻比
K_NEIGHBOURS = 150   # 樣本從 242 天增到 1453 天，取更多鄰居仍然比以前更像

# 走查驗證的分段命中率：面板不再拿未經檢驗的 kNN 百分比當機率用
try:
    CALIBRATION = json.loads(CALIB.read_text(encoding="utf-8"))
except Exception:
    CALIBRATION = {}

# 模擬練習的持倉與當日已平倉紀錄。
# 【鐵律】這裡只做紙上練習，不會送任何委託到永豐 —— 真實下單必須由 Benson 自己操作。
POSITION = None
TODAY_TRADES = []
BOOT_AT = datetime.now()             # 這個行程什麼時候起來的（給程式版本指紋用）
CURRENT_STATE = {"today": None}      # 讓 HTTP handler 拿得到當前的 Today 物件
# 最後一次有瀏覽器來要資料的時間。桌面 App 靠它判斷「視窗是不是被關掉了」——
# 用 Edge 的 --app-id 開視窗時，啟動的那個行程會立刻結束（Edge 交棒給既有的行程），
# 所以不能用「等那個行程結束」來判斷關窗，會一開就誤判（實測 1 秒就誤判）。
LAST_CLIENT = {"at": 0.0}
ENTER_LOCK = threading.Lock()   # 真實進場的互斥：同一時間只准有一張新倉單在路上
SESSION_REF = {"api": None}          # K 線圖要用它去跟永豐要 K 棒

# 加權指數（現貨）：Benson 下單時會看，所以面板一起顯示，並算出基差。
# 【只顯示，不做分析】永豐的指數歷史 1 分 K 只有 54 個破碎的交易日，
# 測不出東西 —— 這裡純粹是多一個客觀數字，不是訊號。
# 現貨 09:00 才開盤、13:30 收，比期貨晚開早收，所以會有「尚未開盤」的空窗。
INDEX = {"price": None, "chg": None, "pct": None, "at": None, "contract": None, "ms": None}
INDEX_EVERY = 2.0        # 秒。指數輪詢的目標週期（實際週期 = max(這個值, 單次耗時)）
CASH_OPEN = pd.Timestamp("09:00").time()
CASH_CLOSE = pd.Timestamp("13:35").time()

state_lock = threading.Lock()
STATE = {"status": "starting", "msg": "啟動中…"}

# 連線健康狀態（VPN 切換／網路中斷時會反映在這裡）
CONN = {"ok": True, "since": None, "retries": 0, "last_error": None}

STALE_SECONDS = 90          # 日盤超過這麼久沒收到 tick 就視為斷線
RECONNECT_EVERY = 60        # 斷線後每隔多久重試一次


# ---------------------------------------------------------------- 歷史矩陣

class History:
    def __init__(self):
        df = pd.read_csv(MATRIX)
        df["min_idx"] = df["minute"].map(lambda s: int(s[:2]) * 60 + int(s[3:]))
        self.df = df
        self.n_days = df["date"].nunique()
        self.period = f"{df['date'].min()} ~ {df['date'].max()}"

    def query(self, min_idx, feats, dayvol):
        """
        找同一時段、狀態最像的歷史時刻，回傳統計結果。

        【關鍵】每個歷史日只取「最像的那一分鐘」一筆。
        若允許同一天貢獻多筆，等於假設你在同一波行情裡可以反覆進場，
        會把勝率算得虛高（實測過，差距可達 15 個百分點）。
        一天一筆 = 一天一個獨立樣本，才對得上「你每天只下一單」的實況。
        """
        pool = self.df[(self.df["min_idx"] >= min_idx - MINUTE_WINDOW)
                       & (self.df["min_idx"] <= min_idx + MINUTE_WINDOW)]
        if len(pool) < 30:
            return None

        X = pool[FEATURES].to_numpy(dtype=float)
        sd = X.std(axis=0)
        sd[sd == 0] = 1.0
        q = np.array([feats[f] for f in FEATURES], dtype=float)
        dist = np.sqrt((((X - q) / sd) ** 2 * FEATURE_WEIGHT).sum(axis=1))

        pool = pool.assign(_d=dist)
        # 每天只留最相似的那一刻 → 再取最接近的 K 天
        per_day = pool.loc[pool.groupby("date")["_d"].idxmin()].nsmallest(
            min(K_NEIGHBOURS, pool["date"].nunique()), "_d")

        by_day = per_day.set_index("date")
        n_days = len(by_day)
        if n_days < 15:
            return None

        horizons = {h: self._horizon(by_day, pool, h, dayvol) for h in (5, 10, 15)}

        # 三個時間長度都指同一邊 = 一致；一致比單一數字漂亮更值得相信
        sides = [1 if hz["prob_up"] > 50 else -1 if hz["prob_up"] < 50 else 0
                 for hz in horizons.values()]
        consistent = abs(sum(sides)) == 3

        # 門檻來自走查驗證（walkforward.py）：只有指數 <40 或 >60 才真的有訊號，
        # 40~60 之間經檢驗是雜訊（斜率 -0.077，信賴區間 [-0.69, +0.53]，不顯著）。
        # 所以中間一律報「沒訊號」，不再分「弱偏漲/弱偏跌」誤導人。
        idx = horizons[10]["prob_up"]
        if idx > 60:
            direction, regime = "偏漲", "bull"
        elif idx < 40:
            direction, regime = "偏跌", "bear"
        else:
            direction, regime = "沒訊號", "flat"

        return {
            "n_days": n_days,
            "index": idx,
            "direction": direction,
            "regime": regime,
            "verified": CALIBRATION.get(regime),
            "consistent": consistent,
            "any_meaningful": any(hz["meaningful"] for hz in horizons.values()),
            "horizons": [{"h": h, **hz} for h, hz in horizons.items()],
        }

    @staticmethod
    def _horizon(by_day, pool, h, dayvol):
        """
        單一時間長度的方向與強度。

          prob_up   歷史上長得像現在的日子，h 分鐘後價格比現在高的比例（0~100）
          move      那些日子 h 分鐘後的變動中位數（點），中位數比平均耐得住極端值
          spread    變動的四分位距，讓人知道「會走多強」有多不確定
        """
        col = f"fwd{h}_n"          # 正規化後的後續變動（幾倍日常波動）
        up = (by_day[col] > 0).astype(float)
        n = len(up)
        p = float(up.mean())
        se = float(up.std(ddof=1) / np.sqrt(n)) if n > 1 else 0.5
        lo, hi = max(0.0, p - 1.96 * se), min(1.0, p + 1.96 * se)
        moves = by_day[col].astype(float)
        base = float((pool[col] > 0).mean())
        return {
            "prob_up": round(p * 100, 1),
            "ci": [round(lo * 100, 1), round(hi * 100, 1)],
            "base": round(base * 100, 1),
            # 顯示時乘回「今天的」波動度 → 換成今天有意義的點數
            "move": round(float(moves.median()) * dayvol, 1),
            "q1": round(float(moves.quantile(0.25)) * dayvol, 1),
            "q3": round(float(moves.quantile(0.75)) * dayvol, 1),
            "meaningful": bool(lo > 0.5 or hi < 0.5),
        }


# ---------------------------------------------------------------- 今日盤面

class Today:
    def __init__(self, prev_close, dayvol=1.0):
        self.prev_close = prev_close
        self.dayvol = dayvol if dayvol and dayvol > 0 else 1.0
        self.open = None
        self.high = None
        self.low = None
        self.price = None
        self.bid = None
        self.ask = None
        self.vol = 0
        self.ticks = 0
        self.quotes = 0
        self.price_is_mid = False
        self.updated = None
        self.last_recv = None      # 最後一次真的收到 tick 的本機時間（判斷斷線用）
        self.minute_close = {}     # 分鐘索引 → 該分鐘最後成交價（算 mom5 / mom15 用）
        self.minute_bar = {}       # 分鐘索引 → 該分鐘的 OHLCV（給最新那根 K 棒即時累加用）

    def feed_quote(self, bid, ask, when):
        """
        五檔買賣價變動。實測每分鐘 400+ 筆（成交才 25 筆），
        大戶投那種「一直在跳」的感覺就是來自這個。
        不進模型 —— 模型的歷史資料是成交價。

        但「還沒有任何成交」時（例如凌晨微台很冷清）要拿中價頂著，
        否則面板會一直卡在「等待第一筆成交…」，明明報價是活的。
        """
        self.bid, self.ask = bid, ask
        self.last_recv = time.time()
        self.quotes += 1
        if self.price is None and bid and ask:
            self.price = (bid + ask) / 2
            self.price_is_mid = True

    def feed(self, price, volume, when, in_session):
        """
        成交。任何時候都記價格（面板要一直顯示現價與動能）；
        只有日盤（08:45~13:45）才累積開高低與成交量 —— 那些欄位的定義是日盤專用的。
        """
        self.price = price
        self.price_is_mid = False
        self.ticks += 1
        self.updated = when
        self.last_recv = time.time()
        mi = when.hour * 60 + when.minute
        self.minute_close[mi] = price
        b = self.minute_bar.get(mi)
        if b is None:
            self.minute_bar[mi] = {"o": price, "h": price, "l": price, "c": price, "v": 0.0}
            b = self.minute_bar[mi]
        b["h"] = max(b["h"], price); b["l"] = min(b["l"], price)
        b["c"] = price; b["v"] += float(volume)
        if not in_session:
            return
        if self.open is None:
            self.open = price
            self.high = self.low = price
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.vol += volume

    def _price_ago(self, now_idx, minutes):
        """
        N 分鐘前的價格；那一分鐘沒成交就往更早找。
        還是找不到就退回「已知最早的價格」，再不然就用現價（動能 = 0）。
        夜盤時沒有日盤開盤價，所以不能拿 self.open 當 fallback。
        """
        for m in range(now_idx - minutes, now_idx - minutes - 10, -1):
            if m in self.minute_close:
                return self.minute_close[m]
        earlier = [m for m in self.minute_close if m <= now_idx - minutes]
        if earlier:
            return self.minute_close[max(earlier)]
        return self.open if self.open is not None else self.price

    def features(self, vol_ref, now_idx):
        if self.open is None or self.prev_close is None:
            return None
        rng = self.high - self.low
        return {
            "mom5": self.price - self._price_ago(now_idx, 5),
            "mom15": self.price - self._price_ago(now_idx, 15),
            "ret_open": self.price - self.open,
            "gap": self.open - self.prev_close,
            "rng": rng,
            "mom5_n": (self.price - self._price_ago(now_idx, 5)) / self.dayvol,
            "mom15_n": (self.price - self._price_ago(now_idx, 15)) / self.dayvol,
            "ret_open_n": (self.price - self.open) / self.dayvol,
            "gap_n": (self.open - self.prev_close) / self.dayvol,
            "rng_n": rng / self.dayvol,
            "pos": (self.price - self.low) / rng if rng > 0 else 0.5,
            "vol_ratio": (self.vol / vol_ref) if vol_ref else 1.0,
        }


def reset_for_new_day(st):
    """
    跨過午夜時把「日盤專用」的欄位清乾淨，其餘保留。

    【為什麼不整個重建 Today】00:00~05:00 夜盤還在跑，minute_bar / minute_close
    是圖上「最新那根 K 棒」與 5/15 分動能的來源（見 overlay_live），
    整個換掉的話夜盤的線會停住、動能會變成 0。

    【為什麼一定要清】open/high/low/vol 的定義是「今天日盤」。不清的話
    features() 會照樣回傳昨天的開高低量 ⇒ 00:00~08:30 這段（正好是他早上
    開面板的時間）畫面上的開高低、震幅、位階、量能、跳空全是昨天的，
    但時鐘是今天 —— 就是他說的「時間錯亂」。
    """
    st.open = st.high = st.low = None
    st.vol = 0
    st.ticks = 0


# ------------------------------------------------------- 模擬練習（不會真的下單）

def open_position(direction, price, note=""):
    """開一筆模擬單。direction: 'long' / 'short'。"""
    global POSITION
    if POSITION is not None:
        return False, "已經有持倉了，先平倉才能再進場"
    if price is None:
        return False, "還沒有報價，無法進場"
    d = 1 if direction == "long" else -1
    POSITION = {
        "dir": direction,
        "entry": float(price),
        "entry_time": datetime.now().strftime("%H:%M:%S"),
        "tp": float(price) + d * TP_POINTS,
        "sl": float(price) - d * SL_POINTS,
        "note": note,
    }
    print(f"[練習] 進場 {direction} @ {price}　停利 {POSITION['tp']:.0f}　停損 {POSITION['sl']:.0f}")
    return True, "已進場"


def close_position(price, reason):
    """平倉並記錄。reason: 'tp' / 'sl' / 'manual' / 'close'。"""
    global POSITION
    if POSITION is None:
        return None
    p = POSITION
    d = 1 if p["dir"] == "long" else -1
    points = d * (float(price) - p["entry"])
    rec = {
        "date": str(date.today()),
        "dir": p["dir"],
        "entry": round(p["entry"]),
        "exit": round(float(price)),
        "time": p["entry_time"][:5],
        "note": p.get("note", ""),
        "mode": "sim",
        # 以下是 trade-log App 沒有、但事後分析很有用的欄位
        "_exit_time": datetime.now().strftime("%H:%M:%S"),
        "_reason": reason,
        "_points": round(points, 1),
        "_net": round(points - FEE_POINTS, 1),
    }
    TODAY_TRADES.append(rec)
    POSITION = None
    save_trades()
    # 背景同步，不讓 git 的網路延遲卡住面板
    threading.Thread(target=sync_to_cloud, daemon=True).start()
    label = {"tp": "停利", "sl": "停損", "manual": "手動平倉", "close": "收盤平倉"}.get(reason, reason)
    print(f"[練習] {label} @ {price}　{points:+.0f} 點（扣費後 {points - FEE_POINTS:+.1f}）")
    return rec


def check_position(price):
    """每次報價更新時檢查有沒有觸及 ±100。"""
    if POSITION is None or price is None:
        return
    d = 1 if POSITION["dir"] == "long" else -1
    if d * (float(price) - POSITION["tp"]) >= 0:
        close_position(POSITION["tp"], "tp")
    elif d * (float(price) - POSITION["sl"]) <= 0:
        close_position(POSITION["sl"], "sl")


REAL_STALE = {"since": None}      # 有真實部位而報價中斷，從何時開始


def check_real_position(price, age, sess="day"):
    """
    真實部位的停損。**停利不在這裡** —— 那一張是限價單，進場後就掛在券商那邊，
    電腦關機也有效；這裡只顧永豐 API 給不了的那一半。

    Benson 2026-08-28 知情選擇「停損交給面板」。所以這幾行是他的停損，
    程式死掉／斷線／電腦睡著就沒有了 —— `REAL_STALE` 是為了讓他**知道**，
    不是為了消除風險。
    """
    pos = broker._state.get("position")
    if pos is None:
        REAL_STALE["since"] = None
        return
    # 【休市不算斷線】13:45~15:00、夜盤收盤後、週末抱單，本來就沒有報價。
    # 舊版一律當成「報價已中斷」跳紅色警報，一次響 75 分鐘以上 ——
    # 狼來了喊多了，真的斷線那次他就不會理了（lab-qa 退件第 6 條）。
    # CLAUDE.md 早就有「沒有報價」與「應該有卻收不到」要分兩態的鐵律。
    if sess == "closed":
        REAL_STALE["since"] = None
        return
    # 報價斷了就記下起點，前端據此示警。斷線時不可以拿舊價去判停損。
    if age is None or age > broker.STALE_ALARM:
        if REAL_STALE["since"] is None:
            REAL_STALE["since"] = time.time()
        return
    REAL_STALE["since"] = None
    if price is None:
        return
    d = 1 if pos["dir"] == "long" else -1
    sl = pos["entry"] - d * SL_POINTS
    if d * (float(price) - sl) <= 0:
        ok, err = broker.close("sl")
        print(f"[真實] 觸及停損 {sl:.0f} → 平倉：{'成功' if ok else err}")


def save_trades():
    TRADE_DIR.mkdir(exist_ok=True)
    (TRADE_DIR / f"{date.today()}.json").write_text(
        json.dumps(TODAY_TRADES, ensure_ascii=False, indent=2), encoding="utf-8")


NOTE_MAX = 500


def set_note(d, t, entry, text, on_open=False):
    """
    幫某一筆練習紀錄補寫心得 —— 跟手機 App 是同一個 note 欄位。

    【只動 note 一個欄位】整筆重寫會把 _mfe/_mae 這些事後算的欄位弄掉，
    也會被前端手上的舊快照把其他欄位蓋回舊值。
    【用（日期＋進場時間＋進場價）認人，不用陣列位序】撤銷最後一筆之後位序就位移了。
    """
    text = (text or "").strip()[:NOTE_MAX]
    if on_open:
        if POSITION is None:
            return False, "現在沒有持倉"
        POSITION["note"] = text
        POSITION["note_at"] = datetime.now().isoformat(timespec="seconds")
        return True, "已記下"

    try:
        datetime.strptime(str(d), "%Y-%m-%d")     # 同時擋掉 ../ 這種路徑
    except Exception:
        return False, "日期格式不對"

    today = str(d) == str(date.today())
    if today:
        recs = TODAY_TRADES
    else:
        f = TRADE_DIR / f"{d}.json"
        if not f.exists():
            return False, "那一天沒有練習紀錄"
        try:
            recs = json.loads(f.read_text(encoding="utf-8")) or []
        except Exception:
            return False, "紀錄檔讀不開"

    hit = None
    for r in recs:
        try:
            if (str(r.get("time", ""))[:5] == str(t)[:5]
                    and round(float(r.get("entry"))) == round(float(entry))):
                hit = r
                break
        except Exception:
            continue
    if hit is None:
        return False, "找不到那一筆紀錄"
    hit["note"] = text
    # 時間戳是「手機與面板誰的心得比較新」唯一的判準
    hit["note_at"] = datetime.now().isoformat(timespec="seconds")

    if today:
        save_trades()
    else:
        (TRADE_DIR / f"{d}.json").write_text(
            json.dumps(recs, ensure_ascii=False, indent=2), encoding="utf-8")
    # 心得也要跟著上雲，手機那邊才看得到
    threading.Thread(target=sync_to_cloud, daemon=True).start()
    return True, "已存"


def load_today_trades():
    """
    啟動與跨日時把當天已有的紀錄讀回記憶體。

    【沒有這段會弄丟資料】TODAY_TRADES 原本只存在記憶體，面板一重啟就變空；
    接著再下一單、save_trades() 一寫，當天稍早的紀錄就被整個覆蓋掉。
    早上下過單、中途重開面板、再下一單 —— 早上那些就沒了。
    """
    global TODAY_TRADES
    f = TRADE_DIR / f"{date.today()}.json"
    if not f.exists():
        TODAY_TRADES = []
        return
    try:
        TODAY_TRADES = json.loads(f.read_text(encoding="utf-8")) or []
        if TODAY_TRADES:
            print(f"  讀回今天已有的 {len(TODAY_TRADES)} 筆練習紀錄")
    except Exception:
        TODAY_TRADES = []


def practice_stats():
    """
    練習成績統計，比照 trade-log App 的呈現方式（近 N 筆的勝率／勝敗／淨點數）。
    若同資料夾放了 my_trades.json（App 匯出檔），會一併算進來，
    這樣面板就是「一個地方看完全部」。
    """
    recs = []
    if TRADE_DIR.exists():
        for f in sorted(TRADE_DIR.glob("*.json")):
            try:
                recs += json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                pass
    imported = HERE / "my_trades.json"
    if imported.exists():
        try:
            for r in json.loads(imported.read_text(encoding="utf-8")):
                d = 1 if r.get("dir") == "long" else -1
                pts = d * (float(r["exit"]) - float(r["entry"]))
                recs.append({**r, "_points": round(pts, 1),
                             "_net": round(pts - FEE_POINTS, 1), "_source": "app"})
        except Exception:
            pass

    for r in recs:
        r.setdefault("_source", "panel")
    recs.sort(key=lambda r: (r.get("date", ""), r.get("time", "")))

    def agg(sub):
        if not sub:
            return None
        net = [r["_net"] for r in sub]
        w = sum(1 for x in net if x > 0)
        return {"n": len(sub), "wins": w, "losses": len(sub) - w,
                "win_rate": round(w / len(sub) * 100, 1),
                "total": round(sum(net), 1),
                "avg": round(sum(net) / len(sub), 1),
                "ntd": round(sum(net) * 10)}          # 微台每點 NT$10

    return {
        "windows": [{"label": lab, **(agg(recs[-n:]) or {})}
                    for lab, n in [("近 7 筆", 7), ("近 10 筆", 10),
                                   ("近 30 筆", 30), ("全部", 10 ** 6)]
                    if agg(recs[-n:])],
        "recent": [{k: r.get(k) for k in
                    ("date", "time", "dir", "entry", "exit", "note",
                     "_net", "_reason", "_source")}
                   for r in recs[-12:]][::-1],
        "total": len(recs),
    }


PHONE_URL = ("https://raw.githubusercontent.com/xd1104/trade-log/"
             "main/data/phone.json")
PHONE_EVERY = 180          # 秒。GitHub raw 本來就有 CDN 快取，抓太密沒有意義


def _note_wins(inc_note, inc_at, cur_note, cur_at):
    """
    手機那筆的心得該不該蓋掉面板這筆。跟手機端 noteWins() 是同一套規則，
    兩邊不一致就會來回互蓋。

    有時間戳的贏；兩邊都有就比時間；兩邊都沒有時**只補空的、不覆蓋**。
    """
    a = (inc_note or "").strip()
    b = (cur_note or "").strip()
    if a == b:
        return False
    if inc_at and not cur_at:
        return True
    if cur_at and not inc_at:
        return False
    if inc_at and cur_at:
        return inc_at > cur_at
    return (not b) and bool(a)


def pull_from_phone():
    """
    把手機寫的心得抓回來。手機沒辦法直接連到這台電腦（常常不同網路），
    所以走跟 sync_to_cloud() 對稱的路：手機用鑰匙圈的金鑰把 data/phone.json
    寫進 repo，這裡讀那個檔。

    【只碰 note / note_at】不會新增、刪除或改動任何一筆交易 ——
    練習紀錄的真相在這台電腦（面板才知道成交價與 _mfe 那些欄位）。
    """
    import urllib.request
    url = PHONE_URL + "?t=" + str(int(time.time()))
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return 0, f"讀不到手機的檔案：{str(e)[:80]}"

    # 依日期分組。【不能把 time 放進配對條件】——手機上編輯過的紀錄 time 是空的
    # （手機表單以前會把它弄丟），而「編輯過」跟「有心得」剛好是同一批，
    # 一旦把 time 算進去，真正要同步的那幾筆 100% 對不上（2026-08-25 踩到）。
    by_day = {}
    for t in payload.get("trades") or []:
        try:
            by_day.setdefault(str(t["date"]), []).append(
                (round(float(t["entry"])), t.get("note", ""), t.get("note_at", "")))
        except Exception:
            continue
    if not by_day:
        return 0, "手機那邊還沒有紀錄"

    def match(day, entry, same_day_count):
        """先比進場價；對不上而那天兩邊都只有一筆，就用日期認（他在手機上改過價）。"""
        cands = by_day.get(day) or []
        for e, note, at in cands:
            if e == entry:
                return note, at
        if len(cands) == 1 and same_day_count == 1:
            return cands[0][1], cands[0][2]
        return None

    changed = 0
    if TRADE_DIR.exists():
        for f in sorted(TRADE_DIR.glob("*.json")):
            try:
                recs = json.loads(f.read_text(encoding="utf-8")) or []
            except Exception:
                continue
            touched = False
            for r in recs:
                try:
                    day, entry = str(r.get("date")), round(float(r.get("entry")))
                except Exception:
                    continue
                hit = match(day, entry, sum(1 for x in recs if x.get("date") == day))
                if hit is None:
                    continue
                note, at = hit
                if _note_wins(note, at, r.get("note"), r.get("note_at")):
                    r["note"] = note
                    r["note_at"] = at or datetime.now().isoformat(timespec="seconds")
                    touched = True
                    changed += 1
            if touched:
                f.write_text(json.dumps(recs, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    if changed:
        # 今天那份也在記憶體裡，不重讀的話面板畫面還是舊的
        load_today_trades()
        sync_to_cloud()
    return changed, (f"從手機補回 {changed} 筆心得" if changed else "沒有比較新的心得")


def poll_phone():
    """背景執行緒：定期把手機那邊的心得抓回來。抓不到就下次再說，不吵人。"""
    while True:
        try:
            n, msg = pull_from_phone()
            if n:
                print(f"[同步] {msg}")
        except Exception as e:
            print(f"[同步] 讀手機紀錄失敗：{str(e)[:100]}")
        time.sleep(PHONE_EVERY)


def sync_to_cloud():
    """
    把練習紀錄寫進 data/practice.json 並推上 GitHub。

    手機常不在同一個網路，所以拿 GitHub Pages 當中間人：
    面板推上去 → 手機開 App 時自動抓下來合併。

    【只同步練習（sim）】真實交易不上傳 —— repo 是公開的，
    那是 Benson 明確的決定。

    推送失敗（沒網路、git 沒設定）不會影響面板運作，只留訊息。
    """
    trades = all_practice_trades()
    if not trades:
        return False, "沒有練習紀錄可同步"
    SYNC_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated": datetime.now().isoformat(timespec="seconds"),
               "count": len(trades), "trades": trades}
    SYNC_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    import subprocess
    def git(*args):
        return subprocess.run(["git"] + list(args), cwd=str(REPO), capture_output=True,
                              text=True, timeout=60)
    try:
        git("add", str(SYNC_FILE.relative_to(REPO)).replace("\\", "/"))
        st = git("status", "--porcelain", "--", "data/practice.json")
        if not st.stdout.strip():
            return True, f"已是最新（{len(trades)} 筆）"
        git("commit", "-m", f"chore: 同步練習紀錄（{len(trades)} 筆）")
        r = git("push", "origin", "HEAD")
        if r.returncode != 0:
            # 手機也會往同一個 repo 寫（data/phone.json），被搶先就會是 non-fast-forward。
            # --autostash：工作目錄不乾淨時照樣能 rebase，事後原樣放回去。
            git("pull", "--rebase", "--autostash", "origin", "main")
            r = git("push", "origin", "HEAD")
            if r.returncode != 0:
                return False, "推送失敗：" + (r.stderr or "")[-120:]
        return True, f"已同步 {len(trades)} 筆到雲端"
    except Exception as e:
        return False, f"同步失敗：{str(e)[:120]}"


def _day_trades(d):
    """某一天的練習交易（今天的用記憶體，過去的讀檔）。"""
    if d == date.today():
        return list(TODAY_TRADES)
    f = TRADE_DIR / f"{d}.json"
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
    if hit is not None and time.time() - hit["at"] < TODAY_TTL:
        return hit["df"]
    df = _raw_days([d])
    _TODAY_RAW.clear()          # 只留今天那一筆，跨日自然就沒了
    _TODAY_RAW[d] = {"df": df, "at": time.time()}
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
        # minute_bar 的鍵只有「分鐘」沒有日期：08:45 之前看到的「15:00 以後」是昨晚的
        d0 = (today - timedelta(days=1)
              if (now.time() < SESSION_OPEN and t0 >= NIGHT_OPEN) else today)
        rows.append({"ts": pd.Timestamp.combine(d0, t0) + pd.Timedelta(minutes=1),
                     "Open": float(b["o"]), "High": float(b["h"]),
                     "Low": float(b["l"]), "Close": float(b["c"]),
                     "Volume": float(b["v"])})
    if not rows:
        return g
    live = pd.DataFrame(rows)
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
    if TRADE_DIR.exists():
        traded = sorted([f.stem for f in TRADE_DIR.glob("*.json")
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
    if TRADE_DIR.exists():
        pt = max([p.stat().st_mtime for p in TRADE_DIR.glob("*.json")], default=0.0)
    key = (mt, pt, len(TODAY_TRADES), str(date.today()))
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
    if TRADE_DIR.exists():
        for p in TRADE_DIR.glob("*.json"):
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
    if TICK_DIR.exists():
        for p in sorted(TICK_DIR.iterdir()):
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
    path = TICK_DIR / f"{d}-polled.jsonl"
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
    path = TICK_DIR / f"{d}.jsonl"
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


# ═════════════ 【程式下單】分頁：四種方向判斷的模擬對照（2026-09-07 加）═════════════
#
# 這一頁回答一個問題，而且只有這一個：**「判斷方向」到底有沒有加分？**
# 每個交易日 09:03:30 讓四種算法各記一筆**模擬**進出場，唯一的差別是方向怎麼決定，
# 其他（時刻、口數、±100、出場規則）全部一樣，然後把成績並排。
#
# ⛔⛔ 鐵律一：**永遠只是模擬，一張單都不會送出去。這不是自動下單，也不是它的前置作業。**
#    這一段（以及 PAGE 裡 #tab-auto 那一段）**一行都不准** import broker、呼叫 broker.*、
#    打 /api/enter、/api/real/*，也不准寫進 practice_trades/ real_trades/
#    sim_orders/ real_orders/。它有自己的資料夾 autotest/。
#    ⚠️ 他自己那一欄是**直接讀** real_trades/*.jsonl（唯讀，一個位元組都不寫），
#      刻意不走 broker.trades_history() —— 走 broker 就會讓「這一頁碰不到下單路徑」
#      這條紅線變成靠人自律，AST 掃不出來。
#    守衛：tools/probe/autotest-backend.py ⑫（AST 掃描；負控組是同一把尺掃
#    Handler._real_enter 那段，必須抓得到 broker ⇒ 證明尺是活的）。
#
# ⛔ 鐵律二：**不碰 on_tick、不碰佇列、不碰 state_lock。**
#    09:03:30 那一刻是主迴圈（4Hz）自己從 Today 物件讀價、**在記憶體裡組好一個 dict**，
#    丟進 _AUTO_Q 給獨立的寫檔執行緒落地（跟 tick_writer.py 同一招）。
#    on_tick 跑在永豐 SDK 的回呼執行緒上，**它更新的價就是他的停損**
#    （永豐沒有停損單，停損活在這支面板的 Python 迴圈裡）。
# ⛔ 鐵律三：**不開新的即時監控迴圈。** 四條的 ±100 是**事後**從 1 分 K 算出來的
#    （_auto_settle），不需要即時 —— 多一條迴圈就是多一個跟停損搶 GIL 的東西。
#
# 資料落地：autotest/YYYY-MM.jsonl（**已 gitignore**），一天一列、`open("a")` 只 append。
#   看門狗重啟是常態，覆寫＝弄丟當天的資料（TODAY_TRADES 已經這樣弄丟過真實資料）。
#   ⚠️ 因為只能 append，「結算」不是回頭改那一列，而是**再 append 一列 rec:"settle"**，
#      讀檔時以 date 為鍵合併。

AUTO_DIR = HERE / "autotest"            # ⛔ 一定要 gitignore（含進場價與時間）
AUTO_REAL_DIR = HERE / "real_trades"    # ⛔ **唯讀**：他自己那一欄的來源

SIGNAL_AT = "09:03:30"                  # ⛔ 只有這一個地方定義，不要散在程式各處
SIGNAL_SEC = 9 * 3600 + 3 * 60 + 30

# ⛔⛔ 【自動下單】的收盤平倉時刻。**正本只有這裡**，auto_fire 靠 configure() 接過去。
#   為什麼是 13:43:30 而不是 13:45：
#     ① 平不掉的時候要有時間重試。⚠️ **這裡不再放第二份數字**（2026-09-09 lab-qa
#        退件 B1）：舊版這一段寫「兩輪要 ~59 秒」，跟 auto_fire.py、CLAUDE.md 各一份，
#        **三份都是紙上算的、而且都跟實跑不一樣**（實測是三輪送單、9 張 IOC、78.2 秒，
#        最後一張 13:44:38）。實測值的正本在 `auto_fire.EOD_WINDOW_S` 的註解，
#        每次跑 `test_auto_fire.py` ⑫i 都會重量一次。
#        重點只有一句：**做得完，而且做完還沒到 13:45（收盤後就送不出去了）**。
#     ② 13:45 之後 `market_session()` 判成 closed ⇒ `check_real_position()` 直接 return
#        ⇒ **停損也停了**。⚠️ ⛔ 但不可以說「收盤後完全沒有保護」（lab-qa 退件 B2）——
#        **15:00 起 sess 變成 night，停損會恢復**；真正的空窗是 13:45~15:00 與
#        05:00~08:45（再加週末與國定假日）。
#     ③ 【模擬】那一頁的 `eod` 取的是標籤 13:43 那根（涵蓋 13:43~13:44）的收盤價
#        （見 AUTO_EOD_LAST 的說明）—— 在那根之內平，跟模擬的基準最接近。
#   ⚠️ 要改的話兩件事一起改：這裡的字串與秒數，⛔ 不可以只改一個。
EOD_CLOSE_AT = "13:43:30"
EOD_CLOSE_SEC = 13 * 3600 + 43 * 60 + 30
C_THRESH = 30.0                         # C：|訊號| 要**超過**這麼多點才做
RATE_MIN_N = 30                         # 少於這麼多筆就不給勝率 %（是選的，不是算的）
CUM_MIN_N = 10                          # 少於這麼多筆就不畫累計線
CEIL_REF_N = 505                        # 進度尺的分母＝天花板的錨點，**同一個數**

# 天花板 ＝ 「這批資料至少要差多少，才有八成機率被抓到」＝ (1.96+0.84)·σ/√n。
#   σ 是**這一頁自己的出場規則（±100 停利停損）**下單筆點數的標準差。
#   ⚠️ CLAUDE.md 開頭那個 σ=89.7 是**另一套出場口徑**的，⛔ 不可以混用在同一條算式裡。
#   ⚠️ 規格（AUTOTEST-TAB-SPEC §7.1）寫的 96.8 是 PM 給的，lab-dev 依 CLAUDE.md
#      「不可以放沒量過的數字」自己重算過一次：
#      量法（tools/probe/autotest-backend.py ⑦ 會用同一套算式重驗定點值）——
#        資料 tmf_1min.csv、最後 505 個交易日；進場價用標籤 09:04 那根（涵蓋
#        09:03~09:04）的 Close 當 09:03:30 的代理（分鐘資料切不出半分鐘，見 §2.3）；
#        一律做多、±100 觸價、都沒摸到就 13:45 收盤平；不扣費。
#      實測 σ = **96.61**（510 天全樣本是 96.65），tp 231 / sl 239 / eod 40、both 0。
#      ⇒ 天花板(505) = 2.80×96.6/√505 = **12.04 點**（PM 那個 12.1 對得起來）、
#         天花板(20) = 60.5 點（規格推的 60.8 也對得起來）。
CEIL_SIGMA = 96.6
CEIL_Z = 2.80                           # 1.96（不是雜訊）＋ 0.84（八成機率被抓到）

AUTO_TP = TP_POINTS                     # ⛔ 跟他真的在用的規則同一組常數，不另開一份
AUTO_SL = SL_POINTS
# 結算從**標籤 09:04 那根（含）**開始 —— 標籤是起始時間，那根涵蓋 09:04~09:05。
# ⛔ 比較一定要用 `>=`（見 _auto_settle 的說明；用 `>` 會從 09:05 才算起，盲區變 90 秒）。
AUTO_SETTLE_FROM = "09:04"
DAY_END_SEC = DAY_END.hour * 3600 + DAY_END.minute * 60
AUTO_LATE_MS = 3000                     # 晚超過這麼久就**不記**（⛔ 不可以拿晚到的價冒充）
AUTO_GAP_S = 5.0                        # 多久沒收到報價算「這一秒是斷的」
AUTO_SETTLE_AFTER = DAY_END_SEC + 120   # 13:47 之後才結算（等最後一根 K 棒收完）
# ⛔⛔ 「都沒摸到 ±100 ⇒ 用收盤價結算」的前提是**真的看完了整個日盤**。
#   2026-09-08 出事就是這樣：他 09:05 打開分頁 ⇒ 端點順手排了一次結算 ⇒ 手上只有
#   兩根 K 棒（09:04／09:05）⇒ _auto_run 沒摸到 ±100 ⇒ 回 eod ⇒ **拿 09:05 的收盤價
#   當「收盤價」寫死進檔案**，四條泳道全部變成「09:05 收盤平 ±67 點」——**假成績**。
#   所以 eod 這條路要同時滿足兩件事：① 那一天的日盤結束了（_auto_day_over）；
#   ② 手上這批 1 分 K 真的走到收盤附近（最後一根的標籤 >= 這裡）。
#   ⚠️ 13:43 不是筆誤：one_min_bars() 的最後一根就是標籤 13:43（涵蓋 13:43~13:44），
#      13:44~13:45 那一分鐘是**既有**的缺口（見 CLAUDE.md「1 分 K 用結束時間標記」）。
AUTO_EOD_LAST = "13:43"

_AUTO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_AUTO_MONTH = re.compile(r"^(\d{4}-\d{2})\.jsonl$")

AUTO_CACHE = {}                 # 檔名 -> {"key":(mtime,size), ...}
AUTO_LOCK = threading.Lock()    # ⛔ 只保護這份快取，絕對不可以碰 state_lock
_AUTO_Q = queue.Queue(maxsize=64)
AUTO = {"started": False, "day": None, "done": False, "settled": False,
        # eod ＝ 今天的收盤平倉觸發過了沒（⛔ 記憶體只是防重複觸發，
        #       「這一天到底平了沒」的真相在 autofire/ 那個檔裡）
        "eod": False,
        "gaps": 0.0, "warm": None, "warm_for": None, "queued": set(), "err": None,
        # ⛔ 一定要在這裡就有 0：`AUTO.get("tick_err", 0)` 那種寫法會讓「有沒有真的在數」
        #    變成看不出來的事，而這個計數是「出錯了但沒有安靜地吞」唯一的痕跡。
        "tick_err": 0}


def auto_ceiling(n):
    """n 筆時，「每筆差多少才分得出來」的門檻（點）。n<=0 回 None（什麼都測不出來）。"""
    if not n or n <= 0:
        return None
    return round(CEIL_Z * CEIL_SIGMA / math.sqrt(n), 1)


def auto_band(n):
    """累計點數的「證不出來帶」半寬 ＝ 天花板(n) × n ＝ CEIL_Z·σ·√n。

    ⛔ 刻意跟 auto_ceiling 用同一個 z 與同一個 σ —— 兩者必須是同一句話的兩種畫法
       （「線還在帶子裡」⇔「每筆沒超過天花板」）。用不同的 z 會讓圖跟表互相打臉。
    """
    if not n or n <= 0:
        return 0.0
    return round(CEIL_Z * CEIL_SIGMA * math.sqrt(n), 1)


def _auto_num(x):
    """
    數值欄位一律過這道。回 float 或 None（看不懂就 None，**不猜**）。

    ⛔ 跟 _tick_num 是**刻意分開的兩份**：這一頁的負控組（把型別檢查拿掉 ⇒ 整月回 500）
       要能獨立驗證，共用一份的話突變會同時打紅【細節】那邊、分不出是誰壞了。
    bool 要另外擋：isinstance(True, int) 是 True，會被當成價格 1.0。
    """
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    x = float(x)
    return x if x == x and -1e12 < x < 1e12 else None


def _auto_dir(v):
    """方向欄位：只認 1 / -1 / 0（不做）/ None（算不出訊號）。⛔ 其餘一律 None。"""
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = int(v)
    return v if v in (1, 0, -1) else None


def auto_sig(px, open0845, p0900):
    """
    09:03:30 那一刻的兩個訊號值（點）。**這是唯一的正本。**

      A  P(09:03:30) − P(09:00)
      B  P(09:03:30) − Open(08:45)

    ⛔ 拿不到參考價就回 None（**不是 0**）——「那天沒做」跟「那天算不出來」
       必須分得出來。
    ⚠️ 抽成一支函式是刻意的：【自動下單（模擬）】那一頁與【自動下單】（會真的送單）
       **一定要用同一把尺**。各寫一份的話，哪天改了算式只會改到一邊，
       而「模擬記的方向」跟「真的送出去的方向」就對不起來了 —— 那正是這兩頁存在的理由。
    """
    return (None if p0900 is None else round(px - p0900, 1),
            None if open0845 is None else round(px - open0845, 1))


def auto_dirs(sig_a, sig_b, thresh=C_THRESH):
    """
    四種算法**唯一的差別**：方向怎麼決定。

      A  09:00 那根 5 分 K 到現在是漲是跌     訊號 = P(09:03:30) − P(09:00)
      B  08:45 開盤到現在是漲是跌             訊號 = P(09:03:30) − Open(08:45)
      C  同 B，但**超過** 30 點才做           不夠就當天不下單（dir = 0）
      D  一律做多，完全不判斷（對照組）       ⛔ D 不是陪跑的，D 是目前的冠軍

    ⛔ 訊號值 == 0 算做多（跟 D 同向）。這是寫死的，不准讓它變成第三種狀態。
    ⛔ 訊號算不出來（拿不到 09:00 或 08:45 的價）回 None，**不是 0** ——
       「那天沒做」跟「那天算不出來」必須分得出來。
    """
    out = {"D": 1}
    out["A"] = None if sig_a is None else (1 if sig_a >= 0 else -1)
    out["B"] = None if sig_b is None else (1 if sig_b >= 0 else -1)
    if sig_b is None:
        out["C"] = None
    elif abs(sig_b) <= thresh:
        out["C"] = 0
    else:
        out["C"] = 1 if sig_b > 0 else -1
    return out


def auto_pair_need(m):
    """
    配對對照的判準：m 天結果不一樣時，**要贏幾次才算數**
    ＝ 讓「純擲銅板也贏這麼多次」的機率 ≤ 2.5%（二項式，雙尾 95%）的最小次數。

      m=7 ⇒ 7（七天全贏才算數）／m=10 ⇒ 9／m=20 ⇒ 15

    ⚠️⚠️ **規格 §8 那個閉合式 `ceil((m+1.96√m)/2)` 不能用**（lab-dev 2026-09-07 實測）：
       它是沒有連續性修正的常態近似，m=5~40 裡有 **17 個** 跟精確二項式對不上，
       而且**每一個都偏小**（m=8 給 7、精確是 8；m=11 給 9、精確是 10）——
       也就是它會**提早**說「算數了」。這一頁存在的理由正是防這件事，
       所以改用精確二項式（m 很小，成本可以忽略）。
       ⚠️ m ≤ 5 時**全贏也不算數**（P(X≥m)=0.5^m > 2.5%），回傳的 need 會大於 m，
          畫面上要照實說「這幾天全贏也還不算數」，⛔ 不可以夾到 m。
    """
    if not m or m <= 0:
        return None
    # P(X=m)=0.5^m 起算，往下累加 P(X≥k)；第一個超過 2.5% 的 k，答案就是 k+1
    term = 0.5 ** m
    cum, k = term, m
    while k > 0 and cum <= 0.025:
        k -= 1
        term = term * (k + 1) / (m - k)
        cum += term
    return k + 1


# ---------------------------------------------------- 09:03:30 那一刻：記下來

def _auto_snap(st, now):
    """
    ⚠️ **這個函式跑在 4Hz 主迴圈上**，所以裡面只准有記憶體讀取 —— 一行 I/O、
       一次 pandas、一個鎖都不准有。真正的組裝與落地在 _auto_worker()。

    【09:03:30 那一刻的價從哪來】
      px       Today.price —— **就是停損看的那個價**（on_tick 每筆成交更新的那一個）。
               ⛔ 不用 STATE["chips"]["price"]：那是 4Hz 渲染出來的快照，多繞一手、
                  而且要拿 state_lock。這裡直接讀屬性，零成本也零風險。
      bid/ask  Today.bid / Today.ask（五檔第一檔）—— 滑價與價差事後絕對補不回來。
      08:45 開盤   Today.open：日盤第一筆成交價；盤中重啟時 seed_from_bars() 會用
                   當天的 1 分 K 補回來。
      09:00 的價   Today.minute_bar[540]["o"] ＝ **09:00 那一分鐘第一筆成交**，
                   那正是「09:00 那根 5 分 K 的開盤」。
                   盤中重啟時 minute_bar 是空的（seed_from_bars 只補 minute_close）⇒
                   退而求其次用 minute_close[539]（08:59 最後一筆，≈ 09:00 開盤），
                   並把用了哪一種記進 p0900_src。
      ⛔⛔ 兩個都拿不到就是拿不到，**不准編**：sig 那一項寫 null，該算法那天
           dir=None、skip="no_ref"，畫面上照實說「那天算不出訊號」。
           面板已經有一條同性質的規矩：「沒有即時報價一律不准開模擬單 ——
           這是紀錄正確性，不是 UX 取捨」。
    """
    mi = st.minute_bar.get(540) if st is not None else None
    p0900, p0900_src = None, None
    if mi is not None and isinstance(mi.get("o"), (int, float)):
        p0900, p0900_src = float(mi["o"]), "bar_open"
    elif st is not None and st.minute_close.get(539) is not None:
        p0900, p0900_src = float(st.minute_close[539]), "prev_min_close"
    age = None if (st is None or st.last_recv is None) else (time.time() - st.last_recv)
    return {
        "date": str(now.date()),
        "at": now.strftime("%H:%M:%S.") + f"{now.microsecond // 1000:03d}",
        "at_lag_ms": int(round((now.hour * 3600 + now.minute * 60 + now.second
                                + now.microsecond / 1e6 - SIGNAL_SEC) * 1000)),
        "px": None if st is None else st.price,
        "bid": None if st is None else st.bid,
        "ask": None if st is None else st.ask,
        "is_mid": bool(st is not None and st.price_is_mid),
        "quote_age_ms": None if age is None else int(round(age * 1000)),
        "open0845": None if st is None else st.open,
        "p0900": p0900, "p0900_src": p0900_src,
        "prev_close": None if st is None else st.prev_close,
        "hi": None if st is None else st.high,
        "lo": None if st is None else st.low,
        "quote_gaps": round(AUTO["gaps"], 1),
    }


def _auto_month_path(d):
    return AUTO_DIR / (str(d)[:7] + ".jsonl")


def _auto_append(row):
    """⛔ 一定是 open("a")。看門狗重啟是常態，覆寫＝把當天稍早的資料弄丟。"""
    AUTO_DIR.mkdir(exist_ok=True)
    p = _auto_month_path(row["date"])
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()
    with AUTO_LOCK:
        AUTO_CACHE.pop(p.name, None)


def _auto_has(d, kinds=("sig", "miss")):
    """這一天已經寫過了嗎（**看檔案，不看記憶體**）。看門狗在 09:03:30 前後重啟時
    會重跑一次判斷 —— 沒有這道就會寫出第二列。"""
    rows, _led = _auto_read()
    r = rows.get(d)
    if r is None:
        return False
    if "sig" in kinds and r.get("px") is not None:
        return True
    return bool("miss" in kinds and r.get("miss"))


def _auto_warm(d):
    """
    當天要用到、但**跟 09:03:30 那一刻無關**的參考值先算好（在寫檔執行緒上算）。

    rng20 ＝ 近 20 個交易日「日盤高低幅」的平均，當波動度基準線。
    ⚠️ 這件事要 pandas 掃 csv，**絕對不可以在 09:03:30 當下才算** ——
       那一刻只准做記憶體讀取（見 _auto_snap 的註解）。算不出來就留 None，不猜。
    """
    try:
        lo, hi = _local_span()
        if hi is None:
            return {"rng20": None}
        px = _LOCAL_PX["df"]
        g = px[(px["ts"].dt.time >= SESSION_OPEN) & (px["ts"].dt.time <= DAY_END)].copy()
        g["dd"] = g["ts"].dt.date
        g = g[g["dd"] < datetime.strptime(d, "%Y-%m-%d").date()]
        rng = (g.groupby("dd")["High"].max() - g.groupby("dd")["Low"].min()).tail(20)
        return {"rng20": round(float(rng.mean()), 1) if len(rng) else None}
    except Exception as e:
        print(f"⚠️ [程式下單] 波動度基準算不出來：{str(e)[:100]}", flush=True)
        return {"rng20": None}


def _auto_record(snap):
    """把 09:03:30 的快照組成一列並落地。**跑在寫檔執行緒上。**"""
    d = snap["date"]
    if _auto_has(d):
        return
    px = _auto_num(snap.get("px"))
    stale = (snap.get("quote_age_ms") is None
             or snap["quote_age_ms"] > AUTO_GAP_S * 1000)
    if px is None or stale or snap.get("is_mid"):
        # ⛔ 09:03:30 收不到報價（斷線／休市／國定假日）⇒ **那天不寫 sig**，
        #    只留一列 miss 說明原因，畫面上列進「沒有記錄的日子」。
        #    ⛔ 不可以拿舊價或中價頂替 —— 那筆成績就是假的。
        why = ("no_quote" if px is None
               else ("mid_only" if snap.get("is_mid") else "quote_stale"))
        return _auto_append({"rec": "miss", "date": d, "src": "live", "why": why,
                             "at": snap.get("at"), "quote_age_ms": snap.get("quote_age_ms"),
                             "quote_gaps": snap.get("quote_gaps"),
                             "wrote_at": datetime.now().isoformat(timespec="seconds")})
    warm = AUTO["warm"] if AUTO.get("warm_for") == d else _auto_warm(d)
    o845 = _auto_num(snap.get("open0845"))
    p900 = _auto_num(snap.get("p0900"))
    sig_a, sig_b = auto_sig(px, o845, p900)      # ⛔ 訊號算式的正本只有一份
    row = {
        "rec": "sig", "date": d, "src": "live",
        "at": snap["at"], "at_lag_ms": snap["at_lag_ms"],
        "px": round(px, 1),
        "bid": _auto_num(snap.get("bid")), "ask": _auto_num(snap.get("ask")),
        "ref": {"open0845": o845, "p0900": p900, "p0900_src": snap.get("p0900_src"),
                "prev_close": _auto_num(snap.get("prev_close")),
                "hi0845_0903": _auto_num(snap.get("hi")),
                "lo0845_0903": _auto_num(snap.get("lo")),
                "rng20": (warm or {}).get("rng20")},
        # ⛔ 記**數值**不是只記方向：二十天後想試「要跌超過 50 點才做」，
        #    用同一批資料就算得出來，不必再等二十天。
        "sig": {"A": sig_a, "B": sig_b},
        "dirs": auto_dirs(sig_a, sig_b),
        "thresh": C_THRESH,
        "quote_age_ms": snap.get("quote_age_ms"),
        "quote_gaps": snap.get("quote_gaps"),
        "wrote_at": datetime.now().isoformat(timespec="seconds"),
    }
    _auto_append(row)
    print(f"[{d}] 程式下單：{SIGNAL_AT} 記下 {row['px']}"
          f"（A {sig_a} / B {sig_b}，晚 {row['at_lag_ms']}ms）—— 模擬，沒有送出任何單",
          flush=True)


# ---------------------------------------------------- 事後結算 ±100

def _auto_day_over(d, now=None):
    """
    **那一天**的日盤結束了沒（＝可不可以用「收盤價」結算）。

    ⛔⛔ 這道判斷是 2026-09-08 那個 bug 的正解：`_auto_run` 的 `eod` 分支語意是
       「**到收盤都沒碰到 ±100**」，這個前提在盤中根本不成立 —— 盤中拿它結算，
       等於把「還沒結束」寫成「收盤了」，而且畫面上看起來就是一筆正常的成績。
    ⚠️ 判斷要站在**那一天**的角度，不是「現在幾點」：
       過去的日子一律已經結束（照樣立刻結算）；只有 d ＝ 今天、而且時鐘還沒走到
       AUTO_SETTLE_AFTER（13:47，等最後一根 1 分 K 收完）才算「還沒結束」。
       未來的日期一律當成沒結束（那種列本來就不該存在，寧可不結算也不要編）。

    ⚠️⚠️ **用哪一把尺當「現在」＝本機時鐘（datetime.now()）**，理由三條：
      ① 這支跑在**寫檔執行緒**上。`STATE["clock"]` 要拿 `state_lock`，
         而這一頁的鐵律二明令不准碰 `state_lock`（那條鎖跟他的停損共用一個 GIL 與一把鎖）。
      ② 永豐的 tick 時間只活在 `on_tick` 那條回呼執行緒更新的 `Today` 物件裡 ——
         去讀它就是把手伸進停損那條路，而且盤前／斷線時它根本是 None。
      ③ 這一頁「今天是哪一天／時刻到了沒」**本來就全部用本機時鐘**
         （`_auto_tick` 的 `secs`、`auto_days()` 的 `date.today()`、`AUTO_SETTLE_AFTER`）。
         再引進第二把尺，就是這個檔案裡「兩把尺」那一類事故的溫床。
      ⇒ 代價是本機時鐘歪掉時這道會跟著歪，但門檻取在收盤後 2 分鐘、而且錯的方向是
        「晚一點才結算」（頂多多等一輪），不會回頭變成假成績。
    """
    now = now or datetime.now()
    today = str(now.date())
    if d < today:
        return True
    if d > today:
        return False
    return (now.hour * 3600 + now.minute * 60 + now.second) >= AUTO_SETTLE_AFTER


def _auto_runs_settled(runs):
    """
    這批 runs 是不是**不會再變**的最終結果（＝可以立刻落地）。

    ・摸到 ±100（`tp` / `sl`）⇒ 最終結果，不管幾點都不會再變。
    ・`dir` 是 None（算不出訊號）或 0（沒超過門檻，這天不做）⇒ 跟價格無關，也是最終的。
    ・`eod` ⇒ **不是**最終結果，它只有在日盤真的收了之後才成立。
    """
    for r in (runs or {}).values():
        if r.get("dir") in (None, 0):
            continue
        if r.get("why") not in ("tp", "sl"):
            return False
    return True


def _auto_run(bars, entry, d):
    """
    ±100 觸價的結果。bars 是 09:04 之後的 1 分 K。d：1 做多 / −1 做空。

    ⛔ **同一根同時摸到 ±100 ⇒ 分不出誰先到 ⇒ 保守算停損**，並標 both=True。
       這個筆數一定要顯示在畫面上 —— 回測最經典的陷阱就是把雙觸日算成停利。
    """
    tgt, stp = entry + d * AUTO_TP, entry - d * AUTO_SL
    for b in bars:
        hit_tp = (b["h"] >= tgt) if d > 0 else (b["l"] <= tgt)
        hit_sl = (b["l"] <= stp) if d > 0 else (b["h"] >= stp)
        if hit_tp and hit_sl:
            return {"dir": d, "exit_at": b["t"], "exit_px": round(stp, 1),
                    "pts": -AUTO_SL, "why": "sl", "both": True}
        if hit_tp:
            return {"dir": d, "exit_at": b["t"], "exit_px": round(tgt, 1),
                    "pts": AUTO_TP, "why": "tp", "both": False}
        if hit_sl:
            return {"dir": d, "exit_at": b["t"], "exit_px": round(stp, 1),
                    "pts": -AUTO_SL, "why": "sl", "both": False}
    last = bars[-1]
    return {"dir": d, "exit_at": last["t"], "exit_px": round(last["c"], 1),
            "pts": round(d * (last["c"] - entry), 1), "why": "eod", "both": False}


def _auto_settle(d):
    """
    補算某一天四條的出場。**跑在寫檔執行緒上**，結果再 append 一列 rec:"settle"。

    ⚠️⚠️ **one_min_bars() 的標籤是「起始時間」**（to_timeframe 已經往回挪一分鐘）——
       標籤 09:03 那根涵蓋 09:03~09:04、標籤 09:04 那根涵蓋 09:04~09:05。
       所以要排掉的是**標籤 09:03 那根**（它的高低含 09:03:30 進場前的價，
       用它會把「進場前就摸到」算成觸價），標籤 09:04 那根**整根都在進場之後、本來就該收進去**。
       ⇒ 條件一定是 `>= AUTO_SETTLE_FROM`（`>` 會連 09:04 那根一起丟掉）。
       ⛔⛔ 2026-09-07 lab-qa 用真 csv 實測退件過一次：舊寫法 `>` ⇒ 實際用到的第一根是
          **09:05**，看不到觸價的變成 **90 秒**，而畫面副標還寫著「從 09:04 開始算」（假話）。
          守衛：autotest-backend.py ⑤b 斷言「settle 用到的第一根標籤 == 09:04」。
       代價是 09:03:30~09:04:00 這 **30 秒**的觸價看不到（要在那半分鐘走 100 點）。
       ⛔ 這件事寫在這裡，不要用「反正很少見」帶過 —— settle_from 欄位有記，畫面上看得到。
    ⚠️ 強制平倉是**當天日盤 13:45 收盤**（PM 拍板，規格 §16-2）：±100 在 09:30 前
       常常摸不到，用 09:30 會製造一大批「不是 ±100 結果」的筆數污染統計。

    ⛔⛔ **盤中不准用「收盤價」結算**（2026-09-08 修，他早上真的踩到）：
       摸到 ±100 ⇒ 立刻寫、標 `final`；還沒摸到而日盤還沒收 ⇒ **一列都不寫**
       （畫面維持「持倉中」）；還沒摸到而收盤了 ⇒ 才可以寫 `eod`。
       舊格式（沒有 `final` 欄位）的 settle 一律當成不可信的早結，會被重算蓋掉。
    """
    rows, _led = _auto_read()
    r = rows.get(d)
    if r is None or r.get("px") is None:
        return
    # ⛔⛔ 只有 **final** 的結算才准擋住重算（2026-09-08 修）。舊版寫的是
    #    `or r.get("runs")` ⇒ 盤中算出來的那一列**把錯的結果鎖死**，一輩子不會再算。
    #    `_auto_read` 已經把「不是 final 的 runs」當成還沒結算（會是 None），
    #    這裡再擋一次是刻意的雙保險：有人拿掉上面那道時，這裡還擋得住假成績。
    if r.get("runs") and r.get("final"):
        return
    # ⛔ 一定是 >=：標籤是**起始時間**，09:04 那根涵蓋 09:04~09:05、整根都在進場之後。
    bars = [b for b in one_min_bars(d) if b["t"] >= AUTO_SETTLE_FROM]
    if not bars:
        return                      # K 棒還拿不到 ⇒ 留著下次再算（畫面寫「結算中」）
    entry, runs = r["px"], {}
    for k in ("A", "B", "C", "D"):
        dr = _auto_dir((r.get("dirs") or {}).get(k))
        if dr is None:
            runs[k] = {"dir": None, "skip": "no_ref"}
        elif dr == 0:
            # ⛔ C 沒做的日子**要有一列**（原因 ＋ 當時的門檻）：
            #    「那天沒做」跟「那天沒錄到」必須分得出來。
            runs[k] = {"dir": 0, "skip": "below_threshold",
                       "thresh": r.get("thresh", C_THRESH)}
        else:
            runs[k] = _auto_run(bars, entry, dr)
    # ⛔⛔ 這裡是 2026-09-08 那個 bug 的閘門（他早上真的踩到，四條全變成假的 ±67）。
    #   摸到 ±100 ⇒ 結果不會再變，**立刻寫、標 final**（不必等收盤）。
    #   還沒摸到 ⇒ 那只是「到目前為止」，`eod` 的語意（到收盤都沒碰到）還不成立 ⇒
    #   **一列都不要寫**，讓它維持「持倉中」，下一輪再算（AUTO_RETRY 冷卻在管節奏）。
    #   ⚠️ 兩道都要：日盤收了沒（_auto_day_over）＋ 手上這批 K 棒有沒有走到收盤附近
    #      （少了第二道，一個只給到 10:00 的殘缺日照樣會寫出「10:00 收盤平」）。
    if not _auto_runs_settled(runs):
        if not _auto_day_over(d) or bars[-1]["t"] < AUTO_EOD_LAST:
            return
    _auto_append({"rec": "settle", "date": d, "src": r.get("src", "live"),
                  "runs": runs, "settle_src": "1min", "settle_from": AUTO_SETTLE_FROM,
                  "bars": len(bars),
                  # ⛔ 寫出去的一定是最終結果 ⇒ final 永遠是 True。
                  #   這個欄位在守的是**舊格式**：2026-09-08 之前寫的 settle 沒有它，
                  #   一律當成「不可信的早結」重算（見 _auto_read_month 結尾）。
                  "final": True,
                  "wrote_at": datetime.now().isoformat(timespec="seconds")})
    print(f"[{d}] 程式下單：已結算 "
          + "／".join(f"{k} {runs[k].get('pts')}" for k in "ABCD"), flush=True)


def _auto_worker():
    """
    唯一會寫 autotest/ 的執行緒。⛔ 主迴圈只 put，不做任何 I/O。

    佇列滿了寧可丟也不阻塞（阻塞的話塞住的是他的停損），但**要留痕跡**：
    一天最多幾件事，滿了幾乎不可能，真的滿了就是有東西壞了，要看得到。
    """
    while True:
        try:
            kind, arg = _AUTO_Q.get()
        except Exception:
            return
        try:
            if kind == "warm":
                AUTO["warm"] = _auto_warm(arg)
                AUTO["warm_for"] = arg
            elif kind == "record":
                _auto_record(arg)
            elif kind == "settle":
                _auto_settle(arg)
            elif kind == "miss":
                if not _auto_has(arg[0]):
                    _auto_append({"rec": "miss", "date": arg[0], "src": "live",
                                  "why": arg[1],
                                  "wrote_at": datetime.now().isoformat(timespec="seconds")})
        except Exception as e:
            AUTO["err"] = f"{kind}: {str(e)[:150]}"
            print(f"⚠️ [程式下單] {kind} 失敗：{str(e)[:200]}", flush=True)
        finally:
            AUTO["queued"].discard((kind, str(arg)))


AUTO_RETRY = 300.0          # 秒。同一天的結算最多多久重試一次
_AUTO_TRIED = {}            # (日期, 日盤收了沒) -> 上次排結算的時間


def _auto_put(kind, arg):
    """⛔ 非阻塞。佇列滿了就丟掉並留痕跡 —— 絕不可以讓主迴圈在這裡等。"""
    key = (kind, str(arg))
    if key in AUTO["queued"]:
        return
    if kind == "settle":
        # ⚠️ 結算是由 HTTP 端點順手排的（每次切進這一頁都會掃一遍還沒結算的日子）。
        #    沒有這道冷卻的話，**永遠結算不了的那一天**（例如那天的 1 分 K 根本拿不到）
        #    會讓每一個請求都去跟永豐要一次 K 棒 —— one_min_bars() 對「空結果」是不快取的。
        # ⚠️⚠️ 冷卻要**分「日盤收了沒」兩段**（2026-09-08 加）：盤中排過一次之後
        #    （盤中那次會因為「還沒摸到」而不寫），13:47 主迴圈那一次會被同一把冷卻
        #    擋掉、要再等 5 分鐘才把早結那列蓋掉。把「收盤了沒」放進鍵裡 ⇒
        #    **收盤後的第一次一定排得進去**，13:47:00 就會重算。
        now = time.time()
        ck = (arg, _auto_day_over(arg))
        if now - _AUTO_TRIED.get(ck, 0.0) < AUTO_RETRY:
            return
        _AUTO_TRIED[ck] = now
    try:
        AUTO["queued"].add(key)
        _AUTO_Q.put_nowait((kind, arg))
    except queue.Full:
        AUTO["queued"].discard(key)
        AUTO["err"] = f"佇列滿了，{kind} 這件事沒做"
        print(f"⚠️ [程式下單] 佇列滿了，丟掉一件 {kind}", flush=True)


def _auto_noop(snap, day, lag_ms):
    """預設的 09:03:30 掛勾：**什麼都不做**。"""
    return None


# ⛔⛔ 09:03:30 那一刻唯一的對外出口。**預設是 no-op**，只有 main() 會把它接到
#    auto_fire.on_signal（＝會真的送單的那一段）。所以：
#      ・--replay、測試治具、任何 import 這個檔的人 ⇒ 掛勾是 no-op ⇒ 一張單都不會出去
#      ・【自動下單（模擬）】那一頁的資料路徑（_auto_record／_auto_settle／autotest/）
#        完全沒有改變，它仍然只是模擬
#    ⛔ 這一行以外不准有第二個地方指派 AUTO_SIG_HOOK；掛上去的函式**只准 put_nowait**
#      （auto_fire.on_signal 自己也有一層 try，永遠不往外丟例外）。
#    ⚠️ 為什麼要共用同一份快照，而不是讓自動下單自己再讀一次價：
#      再讀一次就是**兩把尺**（相差幾毫秒就可能算出相反的方向）⇒
#      「真的送出去的方向」跟「模擬那一頁記下來的方向」對不起來，
#      而那個對照正是這兩頁存在的理由。
AUTO_SIG_HOOK = _auto_noop


def _auto_eod_noop(day, lag_ms):
    """預設的收盤平倉掛勾：**什麼都不做**（跟 `_auto_noop` 同一套規矩）。"""
    return None


# ⛔⛔ 13:43:30 那一刻唯一的對外出口（收盤自動平倉）。**預設是 no-op**，
#    只有 main() 會把它接到 auto_fire.on_eod。
#    ⚠️ 這一條會**送出平倉單**，所以規矩跟 AUTO_SIG_HOOK 一模一樣：
#      ・這一行以外不准有第二個地方指派它
#      ・掛上去的函式**只准 put_nowait**（真正的平倉在 auto_fire 自己的執行緒上跑，
#        `broker.close()` 會等成交最多十幾秒 —— ⛔ 那絕對不可以卡在 4Hz 主迴圈上，
#        那條迴圈就是他的停損）
AUTO_EOD_HOOK = _auto_eod_noop


def _auto_tick(st, now, sess):
    """
    ⚠️⚠️ **這個函式跑在 4Hz 主迴圈上，而那條迴圈就是他的停損。**
    裡面只有整數比較、屬性讀取與 queue.put_nowait —— ⛔ 一行 I/O 都不准加。
    """
    if not AUTO["started"]:
        return
    d = str(now.date())
    if AUTO["day"] != d:
        AUTO.update({"day": d, "done": False, "settled": False, "eod": False,
                     "gaps": 0.0})
        _auto_put("warm", d)
    if sess != "day":
        return
    secs = now.hour * 3600 + now.minute * 60 + now.second
    # 08:45~09:03:30 之間報價斷了幾秒。⛔「安靜地少」是這個專案明令禁止的失敗模式：
    # 訊號算出來之後看不出「那個早上其實有一段沒有報價」就是安靜地少。
    if TICK_SEC0 <= secs < SIGNAL_SEC and st is not None:
        if st.last_recv is None or time.time() - st.last_recv > AUTO_GAP_S:
            AUTO["gaps"] += 0.25
    if not AUTO["done"] and secs >= SIGNAL_SEC:
        AUTO["done"] = True
        lag = (secs - SIGNAL_SEC) * 1000 + now.microsecond // 1000
        if lag > AUTO_LATE_MS:
            # ⛔⛔ 面板是 09:10 才開起來的（或看門狗剛重啟）⇒ **這一刻的價不是
            #     09:03:30 的價**。拿它記進去，那筆成績就是假的而且看不出來。
            _auto_put("miss", (d, "late"))
            # 掛勾也要收到「這一天跳過了」—— 不然那一天在【自動下單】那一頁
            # 連一列「為什麼沒送」都沒有（＝安靜地少）。⛔ 跳過就是跳過，不補單。
            AUTO_SIG_HOOK(None, d, lag)
        else:
            snap = _auto_snap(st, now)
            _auto_put("record", snap)
            # ⛔ 只准 put_nowait。掛勾預設是 no-op，接上去的是 auto_fire.on_signal。
            AUTO_SIG_HOOK(snap, d, lag)
    # ⛔⛔ 收盤自動平倉。505 天裡有 40 天（≈8%）走完一整天都沒碰到 ±100 ⇒
    #     大約每 12 個交易日就有一天會抱過夜盤，**而他可能不在**。
    #     ⚠️ 這裡只有整數比較與 put_nowait；真正的平倉在 auto_fire 自己的執行緒上。
    #     ⚠️ 上面已經 `if sess != "day": return`，所以這條只會在日盤（<13:45）觸發；
    #        13:45 之後重啟面板不會再送平倉單（也送不出去）。
    #     ⛔ **時鐘往前跳的防護**（2026-09-09 lab-qa 提）：這一整段唯一的時間來源是
    #        本機的 `datetime.now()`。NTP 校時往前跳過 13:43:30（或他手動改系統時間）
    #        會讓「早上」也落進這個條件 ⇒ 一口早上剛開的部位當場被平掉。
    #        所以觸發那一刻的 `secs` **必須真的落在 [EOD_CLOSE_SEC, DAY_END_SEC)**
    #        這個半開區間裡；跳出區間就不觸發（漏平最多是他多抱一晚＝現況，
    #        平錯是把他的單平掉 —— 沿用同一條不對稱原則）。
    #        ⚠️ 上界不能靠上面那句 `sess != "day"` 代勞：那把尺是**另一個**函式
    #        （market_session）算的，改了那邊這裡就靜靜地沒有上界了。
    if not AUTO["eod"] and EOD_CLOSE_SEC <= secs < DAY_END_SEC:
        AUTO["eod"] = True
        AUTO_EOD_HOOK(d, (secs - EOD_CLOSE_SEC) * 1000 + now.microsecond // 1000)
    if not AUTO["settled"] and secs >= AUTO_SETTLE_AFTER:
        AUTO["settled"] = True
        _auto_put("settle", d)


def _auto_tick_guarded(st, now, sess):
    """
    ⛔⛔ **主迴圈唯一該呼叫的入口**，這一層 try 不是裝飾。
    這一頁是研究功能，它出任何差錯都不可以讓 4Hz 主迴圈中斷 ——
    中斷了就是**停損不再監控，而且畫面上看不出來**（永豐沒有停損單）。
    ⚠️ 但**不可以安靜地吞**：計數（AUTO["tick_err"]）＋ 主控台警告（前三次）
       ＋ AUTO["err"] 會被 auto_days() 端出去、畫在成績卡的最後一行。

    ⚠️⚠️ **為什麼抽成獨立函式**（2026-09-07 lab-qa 退件 R3）：try 寫在 main() 裡面時，
       探針起不了主迴圈 ⇒ 守衛只能拿字串比對「窗口裡有沒有 try/except/tick_err/print」。
       lab-qa 把 `AUTO["tick_err"] = …` 換成 `pass`（字串仍在窗口裡）⇒ **125/125 全綠**，
       但真正的行為是 except 區塊自己丟 KeyError ⇒ **例外衝出 try ⇒ 主迴圈當場中斷**。
       抽出來之後守衛可以**真的讓 _auto_tick 丟例外**，斷言「迴圈還活著＋計數真的加了＋
       真的印了字」（autotest-backend.py ⑩）。⛔ 不要為了少一層呼叫把它搬回 main()。
    """
    try:
        _auto_tick(st, now, sess)
    except Exception as e:
        AUTO["err"] = f"tick: {str(e)[:150]}"
        AUTO["tick_err"] = AUTO["tick_err"] + 1
        if AUTO["tick_err"] <= 3:
            print(f"⚠️ [程式下單] 主迴圈那一段出錯（停損不受影響）："
                  f"{str(e)[:200]}", flush=True)


# ---------------------------------------------------- 讀檔與統計

def _auto_norm_runs(runs):
    """settle 那一列的 runs 欄位。讀不懂就回 None（整列算 bad，但不弄掉整個月）。"""
    if not isinstance(runs, dict):
        return None
    out = {}
    for k in ("A", "B", "C", "D"):
        r = runs.get(k)
        if not isinstance(r, dict):
            continue
        dr = _auto_dir(r.get("dir"))
        row = {"dir": dr}
        if dr in (None, 0):
            row["skip"] = r.get("skip") if isinstance(r.get("skip"), str) else "no_ref"
            row["thresh"] = _auto_num(r.get("thresh"))
        else:
            row.update({"exit_at": r.get("exit_at") if isinstance(r.get("exit_at"), str) else None,
                        "exit_px": _auto_num(r.get("exit_px")),
                        "pts": _auto_num(r.get("pts")),
                        "why": r.get("why") if r.get("why") in ("tp", "sl", "eod") else None,
                        "both": bool(r.get("both"))})
        out[k] = row
    return out or None


def _auto_norm_sig(o):
    """sig 那一列。px 讀不出來就整列作廢（沒有進場價，這一列什麼都算不了）。"""
    px = _auto_num(o.get("px"))
    if px is None:
        return None
    ref = o.get("ref") if isinstance(o.get("ref"), dict) else {}
    sig = o.get("sig") if isinstance(o.get("sig"), dict) else {}
    dirs = o.get("dirs") if isinstance(o.get("dirs"), dict) else {}
    return {"date": o["date"], "src": ("backfill" if o.get("src") == "backfill" else "live"),
            "px": px, "at": o.get("at") if isinstance(o.get("at"), str) else None,
            "at_lag_ms": _auto_num(o.get("at_lag_ms")),
            "bid": _auto_num(o.get("bid")), "ask": _auto_num(o.get("ask")),
            "ref": {k: _auto_num(ref.get(k)) for k in
                    ("open0845", "p0900", "prev_close",
                     "hi0845_0903", "lo0845_0903", "rng20")},
            "sig": {"A": _auto_num(sig.get("A")), "B": _auto_num(sig.get("B"))},
            "dirs": {k: _auto_dir(dirs.get(k)) for k in ("A", "B", "C", "D")},
            "thresh": _auto_num(o.get("thresh")) or C_THRESH,
            "quote_gaps": _auto_num(o.get("quote_gaps")),
            "runs": None, "final": False}


def _auto_read_month(path):
    """
    解析一個月檔。快取鍵是 (mtime, size) —— ⛔ 只比 mtime 不夠：
    「內容變了、mtime 沒變」（同一秒內續寫）會讓畫面停在舊資料，而且完全看不出來。

    ⛔ **每一列都要有去處**：sig + settle + miss + dup + bad ＝ 檔案裡的總列數。
       這條等式是「有沒有東西被安靜吃掉」唯一的機器判準。
    ⛔ **一列壞資料只准弄掉那一列**，不准弄掉一整個月（【細節】分頁 2026-09-07
       為了同一個病退件過：一列壞 price 讓整天回 500、1,091 列好資料一列都看不到）。
    """
    st = path.stat()
    key = (st.st_mtime, st.st_size)
    ent = AUTO_CACHE.get(path.name)
    if ent is not None and ent["key"] == key:
        return ent
    recs = {}
    led = {"lines": 0, "sig": 0, "settle": 0, "miss": 0, "dup": 0, "bad": 0}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    for line in text.splitlines():
        if not line.strip():
            continue
        led["lines"] += 1
        try:
            o = json.loads(line)
        except (ValueError, TypeError):
            led["bad"] += 1
            continue
        if not isinstance(o, dict):
            led["bad"] += 1
            continue
        d, k = o.get("date"), o.get("rec")
        if not isinstance(d, str) or not _AUTO_DATE.match(d) \
                or k not in ("sig", "settle", "miss"):
            led["bad"] += 1
            continue
        cur = recs.get(d)
        if k == "sig":
            row = _auto_norm_sig(o)
            if row is None:
                led["bad"] += 1
                continue
            if cur is not None and cur.get("px") is not None:
                led["dup"] += 1          # 看門狗在 09:03:30 前後重啟過，照規矩只留第一列
                continue
            led["sig"] += 1
            if cur is not None:          # 先讀到 settle 才讀到 sig（順序不保證）
                row["runs"] = cur.get("runs")
                row["settle_from"] = cur.get("settle_from")
                row["final"] = bool(cur.get("final"))
            recs[d] = row
        elif k == "settle":
            runs = _auto_norm_runs(o.get("runs"))
            if runs is None:
                led["bad"] += 1
                continue
            led["settle"] += 1
            if cur is None:
                cur = recs[d] = {"date": d, "px": None,
                                 "src": ("backfill" if o.get("src") == "backfill" else "live")}
            cur["runs"] = runs
            # ⛔ 沒有 final 欄位 ＝ **2026-09-08 之前的舊格式**，一律當成不可信的早結。
            #   後寫的蓋前面的（同一天多列 settle 時，最後一列說了算）。
            cur["final"] = bool(o.get("final"))
            cur["settle_from"] = o.get("settle_from") if isinstance(
                o.get("settle_from"), str) else None
        else:
            led["miss"] += 1
            if cur is None:
                recs[d] = {"date": d, "px": None, "miss": True, "runs": None,
                           "src": ("backfill" if o.get("src") == "backfill" else "live"),
                           "why": o.get("why") if isinstance(o.get("why"), str) else "unknown"}
    # ⛔⛔ **不是 final 的 settle 一律當成「還沒結算」**（2026-09-08 修）。
    #   盤中提早算出來的那一列（以及 2026-09-08 之前寫的舊格式）是假成績 ——
    #   它一個字都不准流到成績表／累計圖／配對卡／日期清單／今天卡。
    #   把它清成 None 之後，下游那一整排「沒有 runs ＝ 還沒結算」的既有邏輯就自動對了，
    #   而且 _auto_settle 會把它當成沒結算過、重算一列蓋上去。
    #   ⚠️ 帳本（led）**不動**：那一列還在檔案裡，sig+settle+miss+dup+bad 照樣等於總列數。
    for _r in recs.values():
        if _r.get("runs") and not _r.get("final"):
            _r["runs"] = None
    ent = {"key": key, "recs": recs, "led": led}
    AUTO_CACHE[path.name] = ent
    if len(AUTO_CACHE) > 26:            # 兩年份，超過就丟最舊的月份
        for name in sorted(AUTO_CACHE)[:len(AUTO_CACHE) - 26]:
            AUTO_CACHE.pop(name, None)
    return ent


def _auto_read():
    """所有月份合起來：(date -> row, ledger)。**日期由小到大用的時候自己 sort。**"""
    recs, led = {}, {"lines": 0, "sig": 0, "settle": 0, "miss": 0, "dup": 0, "bad": 0}
    with AUTO_LOCK:
        if AUTO_DIR.exists():
            for p in sorted(AUTO_DIR.iterdir()):
                if not p.is_file() or not _AUTO_MONTH.match(p.name):
                    continue
                ent = _auto_read_month(p)
                recs.update(ent["recs"])
                for k in led:
                    led[k] += ent["led"][k]
    return recs, led


def _auto_mine_day(d):
    """
    某一天他自己真的做的那幾筆。⛔ **唯讀** real_trades/，一個位元組都不寫，
    也不經過 broker（見本節開頭鐵律一）。
    """
    p = AUTO_REAL_DIR / f"{d}.jsonl"
    out = []
    if not p.exists():
        return out
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            o = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(o, dict):
            continue
        out.append({"date": d, "dir": o.get("dir") if o.get("dir") in ("long", "short") else None,
                    "entry_time": o.get("entry_time") if isinstance(o.get("entry_time"), str) else None,
                    "entry": _auto_num(o.get("entry")), "exit": _auto_num(o.get("exit")),
                    "exit_time": o.get("exit_time") if isinstance(o.get("exit_time"), str) else None,
                    "points": _auto_num(o.get("points")),
                    "why": o.get("reason") if isinstance(o.get("reason"), str) else None})
    out.sort(key=lambda t: t.get("entry_time") or "")
    return out


def _auto_tally(pts):
    """勝敗定義**必須跟練習／真實那邊同一套：點數 > 0 才算勝，0 算敗。**
    ⛔ 用不同定義的話，同一批交易會算出兩個勝率。"""
    w = sum(1 for p in pts if p > 0)
    return {"n": len(pts), "w": w, "l": len(pts) - w,
            "pts": round(sum(pts), 1),
            "avg": round(sum(pts) / len(pts), 1) if pts else None}


def _auto_row_stats(recs, k, open_days=()):
    """一條算法的成績。cum 是「第幾筆 → 累計點數」。

    ⚠️ `open_days` ＝「日盤還沒收的那幾天」。沒有結算的日子要分成兩堆：
       **持倉中**（正常，還沒摸到 ±100、盤還沒收）與 **還沒結算**（該算了卻還沒算出來，
       那是要示警的）。混成一個數字的話，每天早上都會看到一行紅字。
    """
    pts, cum, run = [], [], 0.0
    skip = pend = noref = both = eod = hold = 0
    for r in recs:
        rr = (r.get("runs") or {}).get(k)
        if rr is None:
            if r.get("date") in open_days:
                hold += 1
            else:
                pend += 1
            continue
        if rr.get("dir") is None:
            noref += 1
            continue
        if rr.get("dir") == 0:
            skip += 1
            continue
        p = rr.get("pts")
        if p is None:
            pend += 1
            continue
        pts.append(p)
        run += p
        cum.append(round(run, 1))
        if rr.get("both"):
            both += 1
        if rr.get("why") == "eod":
            eod += 1
    out = _auto_tally(pts)
    ceil_v = auto_ceiling(out["n"])
    out.update({"skip": skip, "pending": pend, "holding": hold, "noref": noref,
                "both": both, "eod": eod, "cum": cum,
                "ceiling": ceil_v,
                # ⛔ 樣本少的時候不給百分比：3 筆 2 勝顯示「67%」是三次擲銅板，
                #    而一個看起來可信卻無效的數字，比明顯沒用的更危險。
                "rate": (round(out["w"] / out["n"] * 100) if out["n"] >= RATE_MIN_N
                         and out["n"] else None),
                "over": bool(ceil_v is not None and out["avg"] is not None
                             and abs(out["avg"]) > ceil_v)})
    return out


def _auto_mine_rows(recs):
    """
    他自己那一欄。⚠️ **口徑跟四條算法對不上**（規格 §17-4）：
    他一天 0~3 筆、時刻不固定、有手動平倉、points 可能算不出來。
    ⇒ 一定要給**兩列**，只給「全部」那一列會做出錯誤的自我評價，
      而 CLAUDE.md 說「他自己的判斷有沒有價值」是唯一還沒被否定的假設 ——
      把它評價錯的代價比其他四條都高。
      all    ：所有真實交易
      strict ：只取**當天第一筆**、而且 why ∈ {tp, sl}（±100 真的跑完）⇒ 才跟上面可比
    """
    days = [r["date"] for r in recs]
    allp, strictp, cum_all, cum_str = [], [], [], []
    nopts = manual = 0
    ndays = 0
    tally_why = {"tp": 0, "sl": 0, "manual": 0, "other": 0}
    run_a = run_s = 0.0
    for d in days:
        L = _auto_mine_day(d)
        if L:
            ndays += 1
        for i, t in enumerate(L):
            w = t.get("why")
            tally_why[w if w in ("tp", "sl", "manual") else "other"] += 1
            if t.get("points") is None:
                nopts += 1
            else:
                allp.append(t["points"])
                run_a += t["points"]
                cum_all.append(round(run_a, 1))
            if i == 0 and w in ("tp", "sl"):
                if t.get("points") is None:
                    continue
                strictp.append(t["points"])
                run_s += t["points"]
                cum_str.append(round(run_s, 1))
            if w == "manual":
                manual += 1
    def pack(pts, cum):
        o = _auto_tally(pts)
        c = auto_ceiling(o["n"])
        o.update({"cum": cum, "ceiling": c,
                  "rate": (round(o["w"] / o["n"] * 100)
                           if o["n"] >= RATE_MIN_N and o["n"] else None),
                  "over": bool(c is not None and o["avg"] is not None
                               and abs(o["avg"]) > c)})
        return o
    return {"all": pack(allp, cum_all), "strict": pack(strictp, cum_str),
            "days": ndays, "of_days": len(days), "nopoints": nopts,
            "why": tally_why}


def _auto_pairs(recs):
    """
    跟 D 的配對對照。**這一區才是這一頁真正有價值的東西。**

    A、B、D 大多數日子方向一樣 ⇒ 方向一樣的日子兩邊結果完全相同、差值是 0。
    所以「A 比 D 多賺 234 點」的**有效樣本數不是 20，是「結果不一樣的那幾天」**。
    把 20 當成樣本數會嚴重高估這個比較的可信度。
    ⚠️ 「結果不一樣」不等於「方向不同」：C 沒做的日子 D 有做 ⇒ 結果當然不一樣。
    """
    out = {}
    for k in ("A", "B", "C"):
        pts, w, l = [], 0, 0
        for r in recs:
            runs = r.get("runs") or {}
            a, dd = runs.get(k), runs.get("D")
            if a is None or dd is None or dd.get("pts") is None:
                continue
            if a.get("dir") is None:
                continue
            mine = 0.0 if a.get("dir") == 0 else a.get("pts")
            if mine is None:
                continue
            if a.get("dir") == dd.get("dir"):
                continue                      # 同向 ⇒ 結果一樣，差值是 0，不進母體
            diff = round(mine - dd["pts"], 1)
            pts.append(diff)
            if diff > 0:
                w += 1
            elif diff < 0:
                l += 1
        m = len(pts)
        out[k] = {"m": m, "w": w, "l": l, "diff": round(sum(pts), 1),
                  "need": auto_pair_need(m),
                  "dots": [1 if p > 0 else (-1 if p < 0 else 0) for p in pts][:60]}
    return out


def _auto_bseries(recs):
    """
    門檻掃描的原料：每一天的 (B 的訊號值, B 那一筆的點數)。

    【為什麼算得出來】C 的定義是「同 B，但 |訊號| 要超過門檻才做」——
    做的時候方向、進場時刻、進場價、±100 通通跟 B 一樣 ⇒ **結果就是 B 那一筆的結果**。
    所以任何門檻的成績都能從 (sigB, ptsB) 這兩欄直接重算，不必再等一次。
    這就是「⛔ 記數值，不只記方向」那條規矩的兌現。
    ⛔ 這裡面沒有價格、沒有時刻 —— 只有訊號值與模擬點數。
    """
    out = []
    for r in recs:
        s = (r.get("sig") or {}).get("B")
        rr = (r.get("runs") or {}).get("B")
        if s is None or rr is None or rr.get("pts") is None:
            continue
        out.append([s, rr["pts"]])
    return out


def auto_scan(bs, thresholds=(0, 20, 30, 50, 80)):
    """
    ⛔ **不標「最好的那一列」、不畫門檻對報酬的曲線。** 那種圖會讓人一眼挑峰值，
       而峰值幾乎一定是雜訊（同一批資料被問越多次，挑到的越可能只是運氣）。
       只給固定幾檔的表，而且每一列照樣套自己的天花板 ——
       門檻越高 ⇒ 筆數越少 ⇒ 那條線越高，這件事要讓他自己看見。
    """
    rows = []
    for t in thresholds:
        pts = [p for s, p in bs if abs(s) > t]
        o = _auto_tally(pts)
        c = auto_ceiling(o["n"])
        o.update({"t": t, "skip": len(bs) - len(pts), "ceiling": c,
                  "rate": (round(o["w"] / o["n"] * 100)
                           if o["n"] >= RATE_MIN_N and o["n"] else None),
                  "over": bool(c is not None and o["avg"] is not None
                               and abs(o["avg"]) > c)})
        rows.append(o)
    return rows


def auto_days():
    """有哪幾天有紀錄。⛔ 只讀 autotest/，不碰別的東西。"""
    recs, led = _auto_read()
    days, missing = [], []
    for d in sorted(recs, reverse=True):
        r = recs[d]
        try:
            wd = "一二三四五六日"[datetime.strptime(d, "%Y-%m-%d").date().weekday()]
        except ValueError:
            continue
        if r.get("miss"):
            missing.append({"d": d, "w": wd, "why": r.get("why") or "unknown"})
            continue
        runs = r.get("runs") or {}
        days.append({"d": d, "w": wd, "src": r.get("src", "live"),
                     "done": bool(runs),
                     # ⛔ 「還沒摸到 ±100、日盤也還沒收」＝**持倉中**，不是「結算中」。
                     #    這兩件事在畫面上必須分得出來：前者是正常的等待，
                     #    後者是「該算了卻還沒算出來」。⛔ 不給浮動損益（規格 §16-3）。
                     "holding": bool(not runs and not _auto_day_over(d)),
                     "dirs": r.get("dirs") or {},
                     "net": (round(sum(v.get("pts") or 0 for v in runs.values()), 1)
                             if runs else None)})
        if not runs:
            _auto_put("settle", d)      # 還沒結算的日子順手排一件事（不阻塞）
    return {"days": days, "missing": missing,
            "since": days[-1]["d"] if days else None,
            "ledger": led, "err": AUTO["err"], "tick_err": AUTO["tick_err"],
            "signal_at": SIGNAL_AT, "thresh": C_THRESH,
            "track_n": CEIL_REF_N, "rate_min_n": RATE_MIN_N, "cum_min_n": CUM_MIN_N,
            # ⛔ 前端切「今天／到了沒」一律用這兩個 —— 不可以用瀏覽器的 new Date()
            #    （跨午夜那一刻兩邊會不同一天，面板已經為這件事踩過一次）。
            "today": str(date.today()),
            "now": datetime.now().strftime("%H:%M:%S")}


def auto_stats(win=20, src="live"):
    """
    成績。⛔ **回測（backfill）與實跑（live）絕對不可以相加。** 預設只算 live：
    兩批資料的進場價分布不同（分鐘資料切不出 09:03:30，差半分鐘），
    混在一起算出來的勝率是假的。
    """
    recs, led = _auto_read()
    rows = [recs[d] for d in sorted(recs)]
    got = [r for r in rows if not r.get("miss")]
    if src in ("live", "backfill"):
        got = [r for r in got if r.get("src") == src]
    miss = [r for r in rows if r.get("miss")
            and (src not in ("live", "backfill") or r.get("src") == src)]
    total_days = len(got) + len(miss)
    if win and win > 0:
        got = got[-win:]
    # ⚠️ total_n（累積到今天總共幾天）跟 n（這個窗口幾天）**是兩個數字**：
    #    進度尺問的是「這個測試跑到哪裡了」⇒ 一定要用 total_n，
    #    用 n 的話按「近10」進度尺就縮回去，看起來像資料不見了。
    total_n = len([r for r in rows if not r.get("miss")
                   and (src not in ("live", "backfill") or r.get("src") == src)])
    # 「日盤還沒收」的那幾天（幾乎永遠只有今天）。⛔ 用 _auto_day_over 這**同一把尺**，
    #    畫面上「持倉中」與後端「不准寫 eod」才會是同一句話。
    open_days = {r["date"] for r in got if not _auto_day_over(r["date"])}
    out = {"n": len(got), "total_n": total_n, "win": win, "src": src,
           "since": got[0]["date"] if got else None,
           "until": got[-1]["date"] if got else None,
           "rows": {k: _auto_row_stats(got, k, open_days) for k in ("A", "B", "C", "D")},
           "mine": _auto_mine_rows(got),
           "pairs": _auto_pairs(got),
           "bseries": _auto_bseries(got),
           "scan": auto_scan(_auto_bseries(got)),
           "ceiling": auto_ceiling(len(got)),
           "band_k": round(CEIL_Z * CEIL_SIGMA, 1),
           "sigma": CEIL_SIGMA, "z": CEIL_Z,
           "track_n": CEIL_REF_N, "rate_min_n": RATE_MIN_N, "cum_min_n": CUM_MIN_N,
           "signal_at": SIGNAL_AT, "thresh": C_THRESH,
           "tp": AUTO_TP, "sl": AUTO_SL,
           "ledger": led,
           # ⛔⛔ 主迴圈那道 try 攔下來的錯**一定要端出來**（2026-09-07 lab-qa 退件 R5）：
           #    「不可以安靜地吞」如果只吞在後端變數裡、畫面上一個字都沒有，
           #    那就還是吞掉了。err ＋ tick_err 畫在 ledger 那一行旁邊。
           "err": AUTO["err"], "tick_err": AUTO["tick_err"],
           # ⛔ 例外筆數一定要顯示，「安靜地少」是這個專案明令禁止的失敗模式
           "notes": {"missing": len(miss), "missing_days": miss[-12:],
                     "backfill": sum(1 for r in rows
                                     if not r.get("miss") and r.get("src") == "backfill"),
                     "days_total": total_days,
                     "pending": sum(1 for r in got if not r.get("runs")
                                    and r["date"] not in open_days),
                     "holding": sum(1 for r in got if not r.get("runs")
                                    and r["date"] in open_days)}}
    return out


def auto_day(d):
    """
    某一天：那一列 ＋ 08:45~13:45 的 1 分 K ＋ 他自己那幾筆。

    ⚠️ **今天還沒到 09:03:30 時也要回得出東西**（`px` 是 None）——
       他早上開面板看到的預設就是今天。回 404 的話畫面只能退到「最後一個有紀錄的日子」，
       而那一天有方向、有成績 ⇒ 09:03 之前站在這一頁看到的是**別天的方向**，
       最容易被讀成「今天的判斷」。這是紅線 §4.2（不預告）真正的破口。
    """
    recs, _led = _auto_read()
    r = recs.get(d)
    if r is None:
        if d != str(date.today()):
            return None
        r = {"date": d, "px": None, "src": "live", "runs": None, "pending": True}
    bars = one_min_bars(d)
    if r.get("px") is not None and not r.get("runs"):
        _auto_put("settle", d)
    return {"date": d, "src": r.get("src", "live"), "miss": bool(r.get("miss")),
            "pending": bool(r.get("pending")), "today": str(date.today()),
            "now": datetime.now().strftime("%H:%M:%S"),
            "why": r.get("why"), "px": r.get("px"), "at": r.get("at"),
            "ref": r.get("ref") or {}, "sig": r.get("sig") or {},
            "dirs": r.get("dirs") or {}, "runs": r.get("runs"),
            # ⛔ 沒摸到 ±100、日盤也還沒收 ⇒ 畫面寫「持倉中」＋進場價，
            #    ⛔ 不算「現在賺賠多少」（規格 §16-3 拍板：不顯示浮動損益）。
            "holding": bool(r.get("px") is not None and not r.get("runs")
                            and not _auto_day_over(d)),
            "final": bool(r.get("final")),
            "thresh": r.get("thresh", C_THRESH),
            "settle_from": r.get("settle_from"),
            "tp": AUTO_TP, "sl": AUTO_SL, "signal_at": SIGNAL_AT,
            "mine": _auto_mine_day(d),
            "bars": [[b["t"], b["o"], b["h"], b["l"], b["c"]] for b in bars]}


def fire_sim_pairs(days):
    """
    【自動下單】那一頁要的「同一天，模擬那邊記了什麼」。**唯讀。**

    他之後要比的是「真的送出去的」跟「模擬的」差多少：滑價、成交價差、
    以及沒送成的那幾天模擬有沒有記到。兩邊都是一天一列、同一個 09:03:30、
    同一把訊號的尺（`auto_sig()` / `auto_dirs()`），所以用 `date` 就對得起來。

    ⛔ 這裡**只讀** autotest/ 的那份模擬紀錄，一個位元組都不寫；
       也 ⛔ 不把兩邊的成績相加（一個是模擬、一個是真的送出去，相加沒有意義）。
    """
    want = {r.get("date") for r in (days or []) if isinstance(r, dict)}
    if not want:
        return {}
    recs, _led = _auto_read()
    out = {}
    for d in want:
        r = recs.get(d)
        if r is None:
            continue
        runs = r.get("runs") or {}
        out[d] = {"px": r.get("px"), "at": r.get("at"),
                  "miss": bool(r.get("miss")), "why": r.get("why"),
                  "sig": r.get("sig") or {}, "dirs": r.get("dirs") or {},
                  # 只給 A／B 的模擬結果（C／D 不是自動下單的選項）
                  "runs": {k: runs.get(k) for k in ("A", "B") if runs.get(k)}}
    return out


# ------------------------------------------------- 【自動下單】從面板打開（武裝真錢）

# ⭐ 2026-09-09 Benson：「我覺得他現在的開關有點麻煩，可以幫我把開關做在面板那邊嗎？
#    我按個鈕就可以開始了這樣。」⇒「開」從「自己在硬碟上建檔」改成面板上兩顆鈕。
#    ⛔⛔ 這顆鈕按下去就是**武裝真錢**（真單開關也開著的話，下一個交易日 09:03:30
#    會用他的錢送出委託單），所以規矩全部反過來寫：
#      ・**兩段式**：兩顆「用XX開始」→ 畫面上的確認條 →「確定，打開」才會有請求出去。
#        ⛔ 不用 window.confirm（pywebview／WebView2 裡不可靠）。
#      ・確認條那句話的**正本在後端**（`fire_arm_confirm()`）—— 現在是真錢還是演練
#        ⛔ 不准前端自己猜（猜錯就是把「會用你的錢」寫成「只是演練」）。
#      ・端點 `POST /api/fire/on` 有**六道**防護（見 `fire_post_guard()`），
#        ⛔ 缺一道就有繞法。
#      ・已經開著再按 ⇒ **409**，⛔ 不覆蓋、⛔ 不當成換做法
#        （換做法牽涉到「今天已經進場了怎麼辦」，這一輪不做）。
#    ⛔ 建開關檔這件事**整個 repo 只有 `fire_arm_on()` 一個地方做得到**：
#       `auto_fire.py` 對 `ARM_FLAG` 仍然只做 exists／read_bytes／replace／with_name
#       四件事（`test_auto_fire.py` ⑬ 的 AST 斷言一個字都沒放寬）——
#       **會送單的那個模組打不開自己的開關**，這是刻意留著的結構性保證。

# 這個行程開機時隨機產一次。⛔ 不落地、不寫進網址、不放 cookie ——
# 只從 `/api/fire/state` 的 JSON 拿得到，而那份回應**沒有 CORS 標頭**
# ⇒ 別的網站的 JS 送得出請求但**讀不到內容** ⇒ 拿不到這個字串。
FIRE_TOKEN = secrets.token_hex(16)
# ⛔ 只認字面上的本機位址。網域名稱（就算現在解析到 127.0.0.1）一律不算 ——
#    那正是 DNS rebinding 的形狀。
FIRE_LOOPBACK = ("127.0.0.1", "localhost", "::1", "[::1]")
# POST body 的上限。⛔ 有上限本身就是一道防線：沒有的話，一個 `Content-Length: 9e9`
#    就能讓那條執行緒一直讀。最長的 body 是心得（幾 KB），256 KB 綽綽有餘。
MAX_POST_BYTES = 256 * 1024


def _fire_host_ok(h):
    """`Host` 要是本機的字面位址（⛔ 擋 DNS rebinding：把 evil.com 指到 127.0.0.1）。"""
    h = (h or "").strip().lower()
    if not h:
        return False
    if h.startswith("["):                       # [::1]:8770
        h = h.split("]", 1)[0] + "]"
    elif ":" in h:
        h = h.rsplit(":", 1)[0]
    return h in FIRE_LOOPBACK


# ⛔ 真正的 `Origin` 只有 `scheme://host[:port]` 這一種形狀 —— 沒有帳號、沒有路徑、
#    沒有查詢字串、沒有 fragment。⚠️ 只靠 `urlsplit().hostname` 判斷會漏：
#    `http://evil@127.0.0.1` 的 hostname 是 `127.0.0.1` ⇒ 舊寫法會放行
#    （2026-09-09 lab-qa 提）。瀏覽器送不出這種東西，所以**先驗形狀再看主機**。
_FIRE_ORIGIN_RE = re.compile(
    r"^https?://(?:\[[0-9A-Fa-f:.]+\]|[0-9A-Za-z._\-]+)(?::[0-9]{1,5})?$")


def _fire_origin_ok(o):
    """
    有 `Origin` 的話必須是本機。⛔ **沒有** Origin 不算違規（非瀏覽器的呼叫沒有它，
    例如 `test_fire_routes.py`）—— 這一道擋的是「別的網站的分頁」，
    而瀏覽器對跨站的 POST **一定會**帶 Origin，網頁改不掉它。
    ⛔ `null` 要擋：sandbox iframe 與 file:// 送的就是 `null`，那不是他的面板。
    ⛔ 形狀不對的一律擋（`http://evil@127.0.0.1`、帶路徑、帶查詢字串）。
    """
    o = (o or "").strip()
    if not o:
        return True
    if o.lower() == "null":
        return False
    if not _FIRE_ORIGIN_RE.match(o):
        return False
    try:
        u = urlsplit(o)
    except Exception:
        return False
    return u.scheme in ("http", "https") and (u.hostname or "").lower() in FIRE_LOOPBACK


def _fire_browser_guard(headers):
    """
    ⭐ 「這個請求是不是**他自己那個分頁**發出來的」—— 三道**瀏覽器自己加、網頁改不掉**
    的標頭（原本六道防護裡的 ③⑤⑥）。⛔ 這是唯一一份，POST 與 GET 兩條路共用：
    兩邊各寫一份就一定有一份會忘記跟上（這個專案已經因為兩把尺被退件過）。

      ③ `Origin` 有的話必須是本機（`null` 也算跨站）
      ⑤ `Sec-Fetch-Site` 有的話必須是 `same-origin`／`none`（`fetch` 不准設 `Sec-` 開頭）
      ⑥ `Host` 必須是本機**字面位址**（擋 DNS rebinding：evil.com 指到 127.0.0.1）
    """
    if not _fire_origin_ok(headers.get("Origin")):
        return False
    sfs = (headers.get("Sec-Fetch-Site") or "").strip().lower()
    if sfs and sfs not in ("same-origin", "none"):
        return False
    if not _fire_host_ok(headers.get("Host")):
        return False
    return True


def fire_get_guard(headers):
    """
    ⛔⛔ **會端出 `X-Panel-Token` 的 GET 端點**（`/api/state`、`/api/fire/state`）的守衛。

    ⚠️⚠️ 2026-09-09 lab-qa 證偽了原本寫在這裡的推理：舊註解說「就算哪天有人加了 CORS
       標頭，①②③ 一起失效、④ token 還在」—— **不成立**。token 就放在當時**毫無防護**的
       `/api/fire/state` 裡：DNS rebinding 下那份 JSON 是同源的，跨站讀得到 ⇒
       ④ 跟著一起垮。**token 不是獨立的一道**，它的強度等於「拿得到它的那個端點」的強度。
       所以會端出 token 的 GET 也要過 ③⑤⑥。

    ⛔ 這裡**刻意不要** ①②④，理由各不相同（⛔ 不要把它們寫成同一個理由）：
      ・① GET 沒有 body，沒有 Content-Type 可言。
      ・④ 的來源就是這裡（要 token 才拿得到 token ＝ 死結）。
      ・② **不是做不到，是會弄壞既有的呼叫方**：`panel_app.pyw`（桌面殼判斷伺服器
        活著沒／版本舊了沒）與 `restart-panel.py` 都用 `urllib` 打 `/api/state`，
        它們送不出 `X-Panel`。加了 ② ⇒ 他點捷徑開面板時**殼會判定伺服器沒起來**。
      ⛔ 而 ③⑤⑥ 已經足以擋掉「別的網站的分頁」與 DNS rebinding（那兩樣是瀏覽器
        自己加、網頁改不掉的），非瀏覽器的本機呼叫則因為沒有 Origin／Sec-Fetch 而照樣通。
    """
    if not _fire_browser_guard(headers):
        return False, 403, "這個請求不是從面板發出來的"
    return True, 200, ""


def fire_post_guard(headers):
    """
    ⛔⛔ **這支面板每一個 POST 的防護**（`do_POST` 一進來就過，⛔ 不是逐條路由各自套）。

    ⚠️⚠️ 2026-09-09 lab-qa 抓到的 P0：這道防護原本只掛在 `/api/fire/on` 上，
       `/api/real/enter`／`/api/real/close` 一道都沒有 —— 他上網時**任何一個網頁**
       都可以用一張純 HTML 的 `<form enctype="text/plain">`（不必 JS、不必 CORS、
       不必 token）把 `{"dir":"long","x":"="}` 送進來，`broker.enter()` 真的被呼叫。
       實測回 `200 {"ok": true, "msg": "已送出"}`。
       ⇒ 所以現在改成**在 `do_POST` 的入口套一次**：結構上不可能再有「新加的端點忘了套」。

    面板是本機 HTTP 伺服器 ⇒ 他電腦上**任何一個開著的分頁**都送得出請求到
    `localhost:8770`，而他不會看到、也不會被問。所以這裡要擋的不是「壞人連進他的電腦」，
    是「他自己開著的某個網頁」。⛔ 下面每一道**單獨拿掉都有繞法**，缺一道等於沒有：

      ① **Content-Type 必須是 application/json**
         ⛔ 表單那三種（`application/x-www-form-urlencoded`／`multipart/form-data`／
            `text/plain`）是 CORS 眼中的「簡單請求」，**不需要 preflight**，
            一個 `<form>` 自動送出就打得到。
      ② **自訂標頭 `X-Panel: 1`**
         簡單表單送不出自訂標頭；帶自訂標頭的 `fetch` 會被瀏覽器**強制走 preflight**
         （OPTIONS），而我們**不回任何 CORS 標頭** ⇒ 那個 preflight 過不了、
         真正的 POST 根本不會發出來。
      ③ **`Origin` 有就必須是本機**（`null` 也算跨站）。
      ④ **`X-Panel-Token`**：這個行程開機時隨機產的字串，只從 `/api/state`／
         `/api/fire/state` 拿得到，而那兩份回應沒有 CORS 標頭、⛔ 而且**自己也過
         `fire_get_guard()`（③⑤⑥）** ⇒ 跨站的 JS 讀不到 ⇒ 猜不到。
         ⚠️⚠️ **舊註解在這裡寫錯過**（2026-09-09 lab-qa 證偽）：原本寫「①②③ 因為
         有人加 CORS 標頭而一起失效時，④ token 還在」—— 不成立。當時 token 就放在
         **毫無防護的** `/api/fire/state` 裡，DNS rebinding 下那份 JSON 是同源的、
         讀得到 ⇒ ④ 跟著垮。**④ 不是獨立的一道**，它的強度 ＝ 端出它的那個 GET 的強度。
         它真正補得起來的破口是「①②在某個新前端上被寫漏」那一種，⛔ 不是「CORS 被打開」。
      ⑤ **`Sec-Fetch-Site`**：有的話必須是 `same-origin`／`none`。
         那是瀏覽器自己加的、網頁**改不掉**（`fetch` 不准設 `Sec-` 開頭的標頭）。
      ⑥ **`Host` 必須是本機字面位址**（擋 DNS rebinding）。

    回 `(ok, code, msg)`。⛔ 訊息刻意不講「你少了哪一道」——
       這顆鈕沒有「讓外面的人除錯」的需求。
    """
    ct = (headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if ct != "application/json":
        return False, 415, "這個端點只收 application/json"
    if (headers.get("X-Panel") or "").strip() != "1":
        return False, 403, "這個請求不是從面板發出來的"
    tok = (headers.get("X-Panel-Token") or "").strip()
    # ⚠️ `compare_digest` 對**非 ASCII 的 str 會 raise TypeError**，而標頭是 latin-1
    #    解出來的 ⇒ 外面塞一個 >127 的位元組就會讓這裡炸掉、例外冒到 handler（500）。
    #    ⛔ 擋下來的方向是對的，但**不可以用例外當防線**（下一個人很容易把它包成 try/pass）。
    if not tok.isascii() or not secrets.compare_digest(tok, FIRE_TOKEN):
        return False, 403, "這個請求不是從面板發出來的"
    # ③⑤⑥ ⛔ 走共用的那一份（`fire_get_guard` 用的是同一個函式 ＝ 只有一把尺）
    if not _fire_browser_guard(headers):
        return False, 403, "這個請求不是從面板發出來的"
    return True, 200, ""


def fire_fires_today(now=None):
    """
    ⭐⭐ **「按下去之後，第一次真的送單是今天還是下一個交易日？」**

    ⚠️⚠️ 2026-09-09 lab-qa 退件 R2：確認條原本寫死「**下一個交易日** 09:03:30」，
       但 `auto_fire` **沒有「今天開的不算」的閘門** —— 他 08:50 按下去，13 分鐘後
       今天就送一口真單，而畫面告訴他是明天。
       ⭐ Benson 裁示：**改文案、不加閘門**（「按了就開始」才是他要的行為）。

    ⛔⛔ **這裡不准出現第二把尺。** 真正決定 09:03:30 會不會送的是 `_auto_tick()`，
       它只看三樣東西 —— 這個函式就只准看那三樣：
         ① `AUTO["done"]`  今天那一刻已經過去了（送了、或判定 late 跳過了）
         ② `SIGNAL_SEC`    今天的牆上時鐘還沒走到那一秒
         ③ `market_session(那一刻) == "day"`
            ⛔ 用的是 `market_session` 這把**產品自己的**尺（週六日／夜盤都是它說了算），
               ⛔ 不准自己寫 `weekday() < 5`。
       兩邊被 `test_auto_fire.py` ⑬c 用**同一組時間點**綁死（那一條會真的驅動
       `_auto_tick()` 跑一遍，比對兩邊的答案）。

    ⚠️ 已知限制（跟 `market_session` 同一個）：**本機沒有國定假日表** ⇒ 平日的國定假日
       這裡會說「今天」。那不是分岔 —— `_auto_tick()` 那天也一樣會走到 09:03:30
       （只是沒有報價 ⇒ 記一列「沒送」）。兩邊講的是同一件事。
    """
    now = now or datetime.now()
    # ① 今天那一刻已經過去了（`_auto_tick` 早就把 done 立起來了）⇒ 只能等下一個交易日
    if AUTO.get("day") == str(now.date()) and AUTO.get("done"):
        return False
    # ② 牆上時鐘。⛔ 用 SIGNAL_SEC（跟 `_auto_tick` 同一個常數），不是 SIGNAL_AT 那個字串
    if now.hour * 3600 + now.minute * 60 + now.second >= SIGNAL_SEC:
        return False
    # ③ 「今天的那一刻」是不是日盤 —— ⛔ 問 market_session，不要自己判斷星期
    sig_at = datetime.combine(now.date(), dtime(0, 0)) + timedelta(seconds=SIGNAL_SEC)
    return market_session(sig_at) == "day"


def fire_arm_confirm(live, now=None):
    """
    兩段式確認**第二段那句話的正本**。⛔ 前端不准自己算「現在是不是真錢」——
    畫面上那句話講錯的代價是「他以為只是演練，結果那天真的送了一口單」。
    ⚠️ `live` 由呼叫端傳進來（就是 `auto_fire.state()` 算好的那一個），
       ⛔ 這裡**不再問一次** `broker.is_live()` —— 同一件事兩把尺一定會有一把是錯的。
    ⚠️ 「今天／下一個交易日」同理走 `fire_fires_today()`（跟 `_auto_tick` 同一組條件），
       ⛔ 不准在這裡自己寫「明天」兩個字。
    """
    when = ("今天 " if fire_fires_today(now) else "下一個交易日 ") + SIGNAL_AT
    if live:
        return {"live": True, "when": when,
                "text": ("現在是真實下單模式。打開之後，%s 會用你的錢"
                         "真的送單，一天一次，%g 點停利／%g 點停損。"
                         % (when, TP_POINTS, TP_POINTS))}
    return {"live": False, "when": when,
            "text": "現在是演練模式，%s 會照跑但不會真的送單。" % when}


def _fire_arm_log(row):
    """
    ⭐ 「誰在什麼時候、用哪個做法、當下是不是真錢」落地一列。
    ⛔ **刻意不寫進 `autofire/YYYY-MM.jsonl`**：那個檔有硬不變式
       `fire + result + skip + eod + bad ＝ 總列數`，塞第五種 `rec` 進去會讓
       既有守衛整組失效（`read_all()` 會把它算成 `bad` ＝ 看起來像壞資料）。
       所以用同一個資料夾、同一種 jsonl 格式，但**檔名不同**（`arm-YYYY-MM.jsonl`，
       ⛔ 不符合 `auto_fire._MONTH_RE` ⇒ `read_all()` 結構上不會撿到它）。
    ⛔ 一定是 `open("a")`（看門狗重啟是常態）。寫不進去**不可以安靜**：
       開關檔已經建好了（那才是真相），所以回報成功但把警告帶出去 ＋ 主控台印一行。
    """
    try:
        auto_fire.FIRE_DIR.mkdir(parents=True, exist_ok=True)
        p = auto_fire.FIRE_DIR / ("arm-" + str(row["date"])[:7] + ".jsonl")
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
        return None
    except Exception as e:
        msg = "開關打開了，但這一筆沒有記錄下來：" + str(e)[:120]
        print("⚠️ [自動下單] " + msg, flush=True)
        return msg


def fire_arm_on(mode, who="panel"):
    """
    ⭐⭐ **整個 repo 唯一一個會建立 `AUTO_ORDERS_ON` 的地方。**
    回傳 `(http_code, payload)`。⛔ 呼叫端必須先過 `fire_post_guard()`。

    - `mode` 只准 `"A"` / `"B"`，⛔ **先驗再寫**（不准寫進檔案再回頭驗 ——
      驗失敗那一瞬間開關就是開著的）。
    - 寫檔用 `O_CREAT|O_EXCL` ⇒ **結構上不可能蓋掉他已經有的那個檔**
      （已經開著再按 ⇒ 409，⛔ 不是換做法）。而且這一道連「兩個視窗同時按」
      那種競態都擋得住（不是「先 exists() 再寫」那種查完再做）。
    - 內容就是一個 ASCII 大寫字母，⛔ 不加 BOM、不加換行 ——
      跟 `auto_fire._decode_flag()` 讀的那條路對齊（那邊 `strip().upper()`）。
    """
    if not isinstance(mode, str) or mode not in auto_fire.METHODS:
        return 400, {"ok": False, "msg": "只能用「%s」或「%s」這兩個做法" % (
            auto_fire.METHOD_NAME["A"], auto_fire.METHOD_NAME["B"])}
    flag = auto_fire.ARM_FLAG
    live = broker.is_live()
    try:
        flag.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(flag), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return 409, {"ok": False, "armed": auto_fire.arm()["on"],
                     "msg": "已經開著了，要換做法請先關掉"}
    except Exception as e:
        return 500, {"ok": False, "msg": "打不開：" + str(e)[:150]}
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(mode.encode("ascii"))
            f.flush()
    except Exception as e:
        # 檔案已經建出來但內容沒寫成 ⇒ 那是個「看不懂的開關檔」（auto_fire 會拒絕下單），
        # ⛔ 但不可以留著讓他以為開好了：走產品自己的 disarm() 收乾淨。
        try:
            auto_fire.disarm()
        except Exception:
            pass
        return 500, {"ok": False, "msg": "寫不進去：" + str(e)[:150]}
    a = auto_fire.arm()
    row = {"rec": "arm", "date": str(date.today()),
           "at": datetime.now().isoformat(timespec="seconds"),
           "method": mode, "live": live, "who": str(who)[:60],
           "src": "panel", "flag": flag.name, "armed": a["on"]}
    warn = _fire_arm_log(row)
    print("[自動下單] 從面板打開：%s（%s）" % (
        auto_fire.METHOD_NAME.get(mode, mode),
        "真實下單模式" if live else "演練模式"), flush=True)
    return 200, {"ok": True, "armed": a["on"], "method": a["method"],
                 "live": live, "warn": warn,
                 "msg": a["msg"] if a["on"] else (a["msg"] or "開關建好了")}


# ---------------------------------------------------------------- 回顧分頁

_VOLREF = {"map": None}


def vol_ref_by_min():
    """
    每一分鐘「正常」該有的累積量（歷史中位數），量能倍數就是拿現在的累積量去除它。
    跟即時面板 update_state() 用的是同一份 intraday.csv，兩邊的量能才是同一把尺。
    """
    if _VOLREF["map"] is None:
        try:
            df = pd.read_csv(MATRIX, usecols=["minute", "vol_cum"])
            idx = df["minute"].map(lambda s: int(s[:2]) * 60 + int(s[3:]))
            _VOLREF["map"] = df.assign(_i=idx).groupby("_i")["vol_cum"].median().to_dict()
        except Exception:
            _VOLREF["map"] = {}
    return _VOLREF["map"]


_PREVC = {}


def prev_day_close(d):
    """
    上一個交易日的日盤收盤（算跳空用）。

    【這裡踩過一次】直接拿「本機 csv 裡上一個有資料的日子」會算錯：
    排程每天 14:10 才把當天併進 tmf_1min.csv，所以最近一兩天不在檔案裡 ——
    2026-08-13 會去跟 08-11 比，跳空多算了一整天（1078 點 vs 實際 ~300）。
    所以本機資料沒涵蓋到 d 的前一天時，改跟永豐要，並且把結果記起來。
    """
    if d in _PREVC:
        return _PREVC[d]
    f = HERE / "tmf_1min.csv"
    val, local_max = None, None
    if f.exists():
        if _LOCAL_PX["df"] is None or _LOCAL_PX["mtime"] != f.stat().st_mtime:
            local_bars(d)          # 觸發載入
        px = _LOCAL_PX["df"]
        if px is not None:
            day = px[(px["ts"].dt.date < d) & (px["ts"].dt.time >= SESSION_OPEN)
                     & (px["ts"].dt.time < DAY_END)]
            if not day.empty:
                last = day["ts"].dt.date.max()
                val = float(day[day["ts"].dt.date == last]["Close"].iloc[-1])
            local_max = px["ts"].dt.date.max()

    if local_max is None or local_max < d - timedelta(days=1):
        api = SESSION_REF.get("api")
        if api is not None:
            try:
                contract = getattr(api.Contracts.Futures, PRODUCT)[f"{PRODUCT}R1"]
                for back in range(1, 9):
                    pd_ = d - timedelta(days=back)
                    df = pd.DataFrame({**api.kbars(contract, start=str(pd_), end=str(pd_))})
                    if df.empty:
                        continue
                    df["ts"] = pd.to_datetime(df["ts"])
                    g = df[(df["ts"].dt.time >= SESSION_OPEN)
                           & (df["ts"].dt.time < DAY_END)].sort_values("ts")
                    if not g.empty:
                        val = float(g["Close"].iloc[-1])
                        break
            except Exception as e:
                print(f"[回顧] 抓 {d} 的前一日收盤失敗：{str(e)[:80]}")
    _PREVC[d] = val
    return val


def day_features(bars, d):
    """
    用該日 1 分 K 逐根重算客觀盤面。
    欄位定義照 Today.features()（mom5/mom15/ret_open/gap/rng/pos/vol_ratio），
    這樣回顧看到的數字跟他當時在即時面板上看到的是同一組東西。
    """
    if not bars:
        return []
    prev = prev_day_close(d)
    vref = vol_ref_by_min()
    op = bars[0]["o"]
    out, closes = [], []
    hi, lo, cum = -1e18, 1e18, 0.0
    for b in bars:
        hi = max(hi, b["h"]); lo = min(lo, b["l"]); cum += b["v"]
        closes.append(b["c"])
        c, rng = b["c"], hi - lo
        # bars 的 t 是「起始時間」，量能基準的索引是收盤那一分鐘 → +1
        mi = int(b["t"][:2]) * 60 + int(b["t"][3:]) + 1
        ref = vref.get(mi) or vref.get(mi - 1)
        out.append({
            "t": b["t"], "price": c,
            "mom5": round(c - closes[max(0, len(closes) - 6)], 1),
            "mom15": round(c - closes[max(0, len(closes) - 16)], 1),
            "ret_open": round(c - op, 1),
            "gap": round(op - prev, 1) if prev else None,
            "rng": round(rng, 1),
            "pos": round((c - lo) / rng, 3) if rng > 0 else 0.5,
            "vol_ratio": round(cum / ref, 3) if ref else None,
        })
    return out


_DAY1 = {"mtime": None}     # 日期 → 該日 1 分 K（回顧一次會查很多筆，避免重複合成）


def one_min_bars(d):
    """某一天的 1 分 K（走 to_timeframe，時間標記已還原成起始時間）。沒有就回 []。"""
    key = str(d)
    f = HERE / "tmf_1min.csv"
    m = f.stat().st_mtime if f.exists() else None
    if _DAY1["mtime"] != m:              # csv 被排程更新過 → 整個快取作廢
        _DAY1.clear()
        _DAY1["mtime"] = m
    if key in _DAY1:
        return _DAY1[key]
    dd = d if isinstance(d, date) else datetime.strptime(key, "%Y-%m-%d").date()
    df = local_bars(dd)
    if df is None:
        # 排程每天 14:10 才把當天併進 tmf_1min.csv，所以「今天」與「還沒併進去的昨天」
        # 只能跟永豐要。沒連線就回空陣列，前端顯示「—」。
        api = SESSION_REF.get("api")
        if api is not None:
            try:
                contract = getattr(api.Contracts.Futures, PRODUCT)[f"{PRODUCT}R1"]
                df = pd.DataFrame({**api.kbars(contract, start=str(dd), end=str(dd))})
            except Exception as e:
                print(f"[回顧] 跟永豐要 {dd} 的 K 棒失敗：{str(e)[:80]}")
                df = None
    bars = []
    if df is not None and not df.empty:
        df = df.copy()
        df["ts"] = pd.to_datetime(df["ts"])
        g = df[(df["ts"].dt.time >= SESSION_OPEN)
               & (df["ts"].dt.time < DAY_END)].sort_values("ts")
        bars = to_timeframe(g, 1)
    # 今天的 K 棒還在長，不能快取（不然早上開過一次回顧，整天都停在那一刻）
    if dd != date.today() and bars:
        _DAY1[key] = bars
    return bars


def _bar_index(bars, hhmm):
    """時間 → K 棒索引（找最後一根 t <= hhmm）。找不到回 -1。"""
    r = -1
    for i, b in enumerate(bars):
        if b["t"] <= hhmm:
            r = i
        else:
            break
    return r


_REVIEW_CACHE = None


def _review_cache():
    global _REVIEW_CACHE
    if _REVIEW_CACHE is None:
        try:
            _REVIEW_CACHE = json.loads(REVIEW_CACHE.read_text(encoding="utf-8"))
        except Exception:
            _REVIEW_CACHE = {}
    return _REVIEW_CACHE


def enrich_trade(t):
    """
    幫一筆紀錄補上「進場當下的盤面」與 MFE / MAE。
    歷史 K 棒不會變，所以算過就快取（記憶體 + 落地一份），面板重啟也不用重算。
    """
    key = f"{t.get('date')}|{t.get('time')}|{t.get('entry')}|{t.get('exit')}"
    cache = _review_cache()
    if key in cache and t.get("date") != str(date.today()):
        return {**t, **cache[key]}

    bars = one_min_bars(t.get("date"))
    got = {"_snap": None, "_mfe": None, "_mae": None, "_mins": None}
    if bars:
        feats = day_features(bars, datetime.strptime(t["date"], "%Y-%m-%d").date())
        ei = _bar_index(bars, str(t.get("time", ""))[:5])
        if ei >= 0:
            got["_snap"] = feats[ei]
        xt = str(t.get("_exit_time") or "")[:5]
        xi = _bar_index(bars, xt) if xt else -1
        if ei >= 0 and xi >= ei:
            d = 1 if t.get("dir") == "long" else -1
            entry = float(t["entry"])
            mfe, mae = 0.0, 0.0
            # 從進場的「下一根」算起 —— 進場那一分鐘的低點多半發生在進場之前，算進去會誤導
            for b in bars[min(ei + 1, xi):xi + 1]:
                mfe = max(mfe, d * (b["h"] - entry), d * (b["l"] - entry))
                mae = min(mae, d * (b["l"] - entry), d * (b["h"] - entry))
            got.update({"_mfe": round(mfe), "_mae": round(mae), "_mins": xi - ei})
    # 只有真的算出東西才寫進快取（那天還沒有本機 K 棒時，明天可能就有了）；
    # 今天的先不快取 —— 盤中資料還會補，免得把不完整的 MFE/MAE 永久留下來
    if got["_snap"] is not None and t.get("date") != str(date.today()):
        cache[key] = got
        try:
            REVIEW_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return {**t, **got}


def review_payload():
    """回顧分頁要用的全部紀錄（一次給完，前端不用逐日抓）。"""
    recs = []
    if TRADE_DIR.exists():
        for f in sorted(TRADE_DIR.glob("*.json")):
            try:
                for r in json.loads(f.read_text(encoding="utf-8")):
                    recs.append({**r, "_source": "panel"})
            except Exception:
                pass
    imported = HERE / "my_trades.json"
    if imported.exists():
        try:
            for r in json.loads(imported.read_text(encoding="utf-8")):
                d = 1 if r.get("dir") == "long" else -1
                pts = d * (float(r["exit"]) - float(r["entry"]))
                recs.append({**r, "_points": round(pts, 1),
                             "_net": round(pts - FEE_POINTS, 1), "_source": "app"})
        except Exception:
            pass

    out = []
    for r in recs:
        # 尚未平倉／資料損壞的跳過，不要讓整頁壞掉
        if r.get("entry") is None or r.get("exit") is None:
            print(f"[回顧] 略過一筆沒有進出場價的紀錄：{r.get('date')} {r.get('time')}")
            continue
        t = str(r.get("time") or "")
        if ":" in t:                       # App 匯出的是 "9:02" —— 補成 "09:02"
            hh, mm = t.split(":")[:2]
            r = {**r, "time": f"{int(hh):02d}:{int(mm):02d}"}
        try:
            out.append(enrich_trade(r))
        except Exception as e:
            print(f"[回顧] 這筆補資料失敗（略過細節）：{r.get('date')} {str(e)[:80]}")
            out.append({**r, "_snap": None, "_mfe": None, "_mae": None, "_mins": None})

    out.sort(key=lambda r: (r.get("date", ""), r.get("time", "")))
    days = traded_days()
    return {"trades": out,
            "days": sorted(set(days["traded"]) | set(days["others"]), reverse=True),
            "traded": days["traded"], "others": days["others"],
            "tally": replay_tally()}


# ------------------------------------------------- Bar Replay 的判斷（獨立存檔）

# ⛔ 日期就是檔名（`replay_file`），所以進來的字串一定要先驗形狀 ——
#    `/api/replay` 那條路在收 body 之前就用它擋掉亂七八糟的 date。
_REPLAY_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def replay_file(d):
    return REPLAY_DIR / f"{d}.json"


def save_replay(rec):
    """
    把一次重播的判斷存起來。

    【鐵律】只寫 replay_log/，絕對不碰 practice_trades/ ——
    重播是事後演練，混進真實練習紀錄會污染勝率統計，也會被同步到 GitHub 給手機。
    """
    d = str(rec.get("date") or date.today())
    REPLAY_DIR.mkdir(exist_ok=True)
    f = replay_file(d)
    try:
        cur = json.loads(f.read_text(encoding="utf-8")) if f.exists() else []
    except Exception:
        cur = []
    cur.append(rec)
    f.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(cur)


def replay_tally():
    """
    重播成績。只算次數，不換算成百分比 ——
    那是「事後重播」的次數，讀成勝率會誤導。
    """
    n = tp = sl = same = 0
    if REPLAY_DIR.exists():
        for f in sorted(REPLAY_DIR.glob("*.json")):
            try:
                for r in json.loads(f.read_text(encoding="utf-8")):
                    n += 1
                    if r.get("reason") == "tp":
                        tp += 1
                    elif r.get("reason") == "sl":
                        sl += 1
                    if r.get("same_dir"):
                        same += 1
            except Exception:
                pass
    return {"n": n, "tp": tp, "sl": sl, "same": same}


def all_practice_trades():
    """把所有練習紀錄整理成 trade-log App 可以匯入的格式。"""
    out = []
    if TRADE_DIR.exists():
        for f in sorted(TRADE_DIR.glob("*.json")):
            try:
                for r in json.loads(f.read_text(encoding="utf-8")):
                    out.append({k: v for k, v in r.items() if not k.startswith("_")})
            except Exception:
                pass
    return out


# ---------------------------------------------------------------- 桌面 App

# 【為什麼要 manifest】只用 Edge 的 --app 開視窗，Windows 還是把它算成 Edge，
# 工作列顯示的是 Edge 的圖示。要變成自己的圖示、能釘選、出現在「已安裝的應用程式」裡，
# 就得讓瀏覽器「安裝」它 —— 而安裝的前提是有一份 manifest 與 192／512 的圖示。
MANIFEST = {
    "name": "早盤儀表板",
    "short_name": "早盤儀表板",
    "description": "微台指早盤看盤與模擬練習",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#0F1218",
    "theme_color": "#0F1218",
    "lang": "zh-Hant",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png",
         "purpose": "any"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png",
         "purpose": "any"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png",
         "purpose": "maskable"},
    ],
}


# ---------------------------------------------------------------- 網頁

PAGE = r"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>早盤儀表板</title>
<!-- 圖示直接內嵌（面板是單一檔、不另外供應靜態檔）。桌面 App 的視窗與
     工作列圖示就是靠這張 favicon；同一張圖也做成 panel.ico 給捷徑用。 -->
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#0F1218">
<link rel="icon" type="image/png" href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAAvklEQVR42mNgwANEJBT+UwMzkAKoZSlZjqG15XgdQS/LsTqCVM2PVwZixWQ7ghJLKXEM0Q4gxWBS1JJkObnRRNARtLCcFP0MxGp+FxUCxtR2BAOxmsh1ACFHMJDie0KOMNlcB8ZUcQA235PrAGxm4nQAvuDH5wCY5aSGwqgDRh0w+BxAbjYkxgE0LQnxOYDikpDuDiC3JiTVcqpXx+gOoKg6pluDZMCbZEOiUUrzZvmAd0wGRddsUHROB6J7DgCcaOnIVZuz+QAAAABJRU5ErkJggg==">
<style>
/* 配色與版型對齊 trade-log App（css/style.css）。
   含台股慣例：紅色=漲/贏、綠色=跌/輸 —— 跟 App 一致，避免看反方向。
   2026-08-25 視覺升級（方案 A 沉穩，規格見 UI-REDESIGN-SPEC.md）：
   舊版每張卡同一個底色＋同一條邊框 ⇒ K 線圖（主角）跟一排篩選鈕（配角）視覺重量一樣。
   現在分三層：L1 主卡（只有 K 線圖，全頁唯一有陰影）／L2 一般卡／L3 純容器。 */
:root{
  --bg:#0E1116; --surface:#151A22; --surface-2:#1C222C; --raise:#1A212B;
  --line:#242C38; --line-soft:#1E2530;
  --text:#E9ECF1; --dim:#8D95A3; --faint:#5C6472; --ghost:#39414F;
  --gold:#E3A951; --gold-soft:rgba(227,169,81,.14); --gold-line:rgba(227,169,81,.42);
  /* 紅漲綠跌是台股慣例，色碼沿用舊值不准動，只補 soft/line 兩個衍生色 */
  --up:#EE5A54; --up-soft:rgba(238,90,84,.15); --up-line:rgba(238,90,84,.38);
  --down:#34B37E; --down-soft:rgba(52,179,126,.15); --down-line:rgba(52,179,126,.38);
  --r-lg:16px; --r-md:12px; --r-sm:9px; --r-xs:6px;
  --radius:var(--r-lg); --radius-sm:var(--r-sm);   /* 舊名保留當別名，免得改上百處 */
  --shadow-1:0 18px 40px -22px rgba(0,0,0,.85);
  --shadow-2:0 24px 60px -20px rgba(0,0,0,.7);
  --ease:cubic-bezier(.22,.68,.36,1);
  --font-sans:-apple-system,BlinkMacSystemFont,"PingFang TC","Microsoft JhengHei","Noto Sans TC","Segoe UI",Roboto,sans-serif;
  --font-mono:ui-monospace,"SF Mono","JetBrains Mono","Roboto Mono",Menlo,Consolas,monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg); color:var(--text); font-family:var(--font-sans); line-height:1.5;
  background-image:radial-gradient(1100px 460px at 50% -10%, rgba(227,169,81,.055), transparent 72%);
  background-repeat:no-repeat; min-height:100vh; -webkit-font-smoothing:antialiased}
/* 電腦大螢幕：左邊大圖、右邊操作區。Benson 只在電腦上開這個面板。 */
.app{max-width:1500px; margin:0 auto; padding:0 24px 28px}
.cols{display:grid; grid-template-columns:minmax(0,1fr) 388px; gap:18px; align-items:start}
@media(max-width:1160px){ .cols{grid-template-columns:minmax(0,1fr)} }
.right{display:flex; flex-direction:column; gap:14px}
.topbar{display:flex; align-items:center; justify-content:space-between; padding:18px 2px 16px; gap:16px}
.brand{display:flex; align-items:center; gap:11px}
.brand .mark{width:34px;height:34px;border-radius:11px;display:grid;place-items:center;
  background:linear-gradient(150deg,rgba(227,169,81,.22),rgba(227,169,81,.06));
  color:var(--gold); font-size:15px; box-shadow:inset 0 0 0 1px rgba(227,169,81,.2)}
.brand .nm{font-size:15px; font-weight:680; letter-spacing:.3px}
.brand .sub{font-size:11px; color:var(--faint); margin-top:1px}
.clock{text-align:right; font-family:var(--font-mono); font-variant-numeric:tabular-nums}
.clock .d{font-size:14px; font-weight:600; letter-spacing:.4px}
.clock .w{font-size:11px; color:var(--faint)}
/* L2：一般卡（右欄、回顧的資料卡）。margin-bottom 保留 —— 右欄很多地方靠它疊卡片 */
.card{background:var(--surface); border:1px solid var(--line-soft); border-radius:var(--r-lg);
  padding:16px 18px; margin-bottom:12px}
/* L1：只有 K 線圖用。全頁唯一有陰影的東西 ⇒ 一眼看得出誰是主角。
   ⚠ 不可以加 overflow:hidden —— 迷你月曆 .calpop 是浮出卡片外的絕對定位元素。 */
.card.l1{background:linear-gradient(180deg,var(--raise),#161C24); border-color:var(--line);
  box-shadow:var(--shadow-1); padding:16px 18px 13px; position:relative}
.card.l1::before{content:''; position:absolute; left:16px; right:16px; top:0; height:1px;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.07),transparent)}
/* L3：純容器（篩選 chips、只放一行提示的區塊）—— 不要再包一張有邊框的卡 */
.sec-head{display:flex; align-items:center; justify-content:space-between; margin:0 2px 8px; gap:10px}
.sec-head h2{font-size:11.5px; margin:0; color:var(--dim); letter-spacing:1.6px; font-weight:650}
.sec-head .count{font-size:11px; color:var(--faint); font-family:var(--font-mono);
  font-variant-numeric:tabular-nums}
.grid{display:grid; grid-template-columns:1fr 1fr; gap:1px; background:var(--line-soft);
  border-radius:var(--r-md); overflow:hidden}
.cell{background:var(--surface); padding:11px 13px}
.cell .l{font-size:11px; color:var(--dim)}
.cell .v{font-size:19px; font-weight:650; font-family:var(--font-mono);
  font-variant-numeric:tabular-nums; margin-top:2px}
.up{color:var(--up)} .down{color:var(--down)} .flat{color:var(--text)}
.chart{position:relative}
/* 用瀏覽器原生的縮放把手：右下角可拖曳改變寬高，尺寸記在 localStorage。
   preserveAspectRatio="none" 讓 K 線跟著容器拉伸，跟看盤軟體一樣。 */
.cwrap{overflow:hidden; border-radius:10px; position:relative}
.cwrap svg{display:block; width:100%; height:auto; cursor:grab; touch-action:none;
  user-select:none}
.legend{display:flex; gap:14px; flex-wrap:wrap; font-size:12.5px; color:var(--dim);
  font-family:var(--font-mono); font-variant-numeric:tabular-nums; margin:2px 0 8px}
.legend b{color:var(--text); font-weight:600}
.legend .lt{color:var(--faint)}
.chint{font-size:11px; color:var(--faint); text-align:right; margin-top:5px}
.chint #cinfo{color:var(--dim)}
/* 報價區三層：52px 價格（主角）／有底色的漲跌膠囊／第三行灰字（即時燈・昨收・合約・更新時間）。
   標頭右邊是兩行的翻頁列，一定要底部對齊（flex-end）：baseline 會把右欄「第一行的基線」
   對到大字的基線，第二行整條就掛到大字底線以下，整塊白白多吃 22px（實測 45→67）。
   回顧分頁的 #rhead 也是同一套 qblock，所以這條可以掛在 .chead 上。 */
.chead{display:flex; align-items:flex-end; justify-content:space-between; margin-bottom:10px;
  gap:16px; flex-wrap:wrap}
.qblock{display:flex; flex-direction:column; gap:6px; min-width:0}
.qmain{display:flex; align-items:baseline; gap:11px; flex-wrap:wrap}
.cpx{font-size:52px; font-weight:700; font-family:var(--font-mono);
  font-variant-numeric:tabular-nums; line-height:.94; letter-spacing:-1.6px}
.cchg{display:inline-flex; align-items:baseline; gap:7px; font-family:var(--font-mono);
  font-variant-numeric:tabular-nums; font-size:15px; font-weight:700; padding:4px 10px 5px;
  border-radius:9px; line-height:1}
.cchg.up{background:var(--up-soft); color:var(--up)}
.cchg.down{background:var(--down-soft); color:var(--down)}
.cchg.flat{background:var(--surface-2); color:var(--dim)}
.cchg .pct{font-size:12.5px; font-weight:600; opacity:.85}
.qsub{display:flex; align-items:center; gap:9px; font-size:11.5px; color:var(--faint);
  font-family:var(--font-mono); flex-wrap:wrap}
.qsub .sep{color:var(--ghost)}
.qsub .live{color:var(--gold); display:inline-flex; align-items:center; gap:5px}
.qsub .live i{width:6px;height:6px;border-radius:50%;background:var(--gold);
  box-shadow:0 0 0 3px var(--gold-soft)}
/* 沒有即時報價時燈換成中性灰 —— 週末沒報價不是故障 */
.qsub .live.dead{color:var(--faint)}
.qsub .live.dead i{background:var(--faint); box-shadow:0 0 0 3px rgba(255,255,255,.05)}
/* 換日：圖上常駐一條翻頁列，右欄兩行 —— 上排「能按的」（◀ 日期 ▶ 今天／即時），
   下排 11px 灰字「只是說明的」（夜盤範圍、練習結果、鍵盤提示）。
   高度預算：r1 24px ＋ gap 2px ＋ r2 約 16px ＝ 42px，不可超過改版前的 45px；
   動任何一個字級／padding 都要重量一次 getBoundingClientRect().height，不能用字級推算。 */
.pager{display:flex; flex-direction:column; align-items:flex-end; gap:2px; position:relative}
.pager .r1{display:flex; align-items:center; gap:3px}
.pager .r2{display:flex; align-items:center; gap:7px; font-size:11px; color:var(--faint);
  line-height:1.25; padding-right:5px; flex-wrap:wrap; justify-content:flex-end}
.pager .r2 .sep{color:var(--ghost)}
.pager .r2 b{font-family:var(--font-mono); font-variant-numeric:tabular-nums;
  font-size:11.5px; font-weight:650}
.pager .r2 .kbdgrp{display:inline-flex; gap:3px; align-items:center}
.pager .r2 kbd{font-family:var(--font-mono); font-size:10.5px; background:var(--surface-2);
  border:1px solid var(--line-soft); border-radius:5px; padding:0 4px; color:var(--dim);
  line-height:1.35}
/* 箭頭去框變成幽靈圖示：有框的方塊視覺重量跟資訊一樣重，會跟日期互相搶。
   24×24 是上限，26×26 會讓整塊超過 45px。變灰用顏色不用 opacity（opacity 連背景一起淡，看起來髒）。 */
.pager .nav-icon{width:24px; height:24px; border-radius:8px; border:0; background:transparent;
  color:var(--faint); font-size:11px; line-height:1; display:grid; place-items:center;
  cursor:pointer; font-family:var(--font-sans); padding:0;
  transition:background .12s var(--ease), color .12s var(--ease)}
.pager .nav-icon:hover:not(:disabled){background:var(--surface-2); color:var(--text)}
.pager .nav-icon:active:not(:disabled){background:var(--line)}
.pager .nav-icon:disabled{color:var(--ghost); cursor:default}
.pager .nav-icon:focus-visible{outline:1px solid var(--gold); outline-offset:1px}
/* 日期本身不再是金色（金色只留給「即時／現在」一個意思），line-height 全部鎖 1，
   否則行高會把 r1 撐過 24px */
.pager .dstamp{display:inline-flex; align-items:center; gap:7px; border:0; background:transparent;
  cursor:pointer; padding:3px 7px; border-radius:9px; color:var(--text); line-height:1;
  font-family:var(--font-mono); font-variant-numeric:tabular-nums; white-space:nowrap;
  transition:background .12s var(--ease)}
.pager .dstamp .num{font-size:15px; font-weight:650; letter-spacing:.3px; line-height:1}
/* 星期那一格永遠佔一個字寬（今天是週末/休市時 dayInfo 找不到、星期是空的）——
   翻頁列整條靠右對齊，這一格一縮 ◀ 就往右跑 12px，連點時第 2 下會落到日期鈕上（誤開月曆）。 */
.pager .dstamp .wd{font-family:var(--font-sans); font-size:12px; color:var(--dim);
  font-weight:500; line-height:1; min-width:1em; text-align:center}
.pager .dstamp .cal-i{color:var(--faint); transition:color .12s var(--ease)}
.pager .dstamp .caret{font-size:8px; color:var(--faint);
  transition:transform .15s var(--ease), color .12s var(--ease)}
.pager .dstamp:hover{background:var(--surface-2)}
.pager .dstamp:hover .cal-i,.pager .dstamp:hover .caret{color:var(--gold)}
.pager .dstamp.open{background:var(--surface-2)}
.pager .dstamp.open .cal-i,.pager .dstamp.open .caret{color:var(--gold)}
.pager .dstamp.open .caret{transform:rotate(180deg)}
.pager .dstamp.loading .num{color:var(--dim)}
.pager .dstamp:focus-visible{outline:1px solid var(--gold); outline-offset:1px}
/* 金色只准有一個意思＝「即時／現在」：看歷史日時出現金色「今天」鈕，
   即時時改成金點，兩者永不同時出現（舊版金色膠囊＋金色今天互搶就是這樣消掉的） */
.pager .jump2{font-family:var(--font-sans); font-size:12px; font-weight:600; cursor:pointer;
  border:0; border-radius:8px; padding:4px 10px; line-height:1.15;
  background:var(--gold-soft); color:var(--gold); white-space:nowrap;
  display:inline-flex; align-items:center; justify-content:center; min-width:52px;
  transition:background .12s var(--ease)}
.pager .jump2:hover:not(:disabled){background:rgba(227,169,81,.24)}
/* 【細節】分頁會在「今天沒有逐筆紀錄」時把它停用（寬度要跟能按的時候一模一樣，
   消失或變窄都會讓整條靠右對齊的 r1 位移）。即時分頁不會走到這個狀態。 */
.pager .jump2:disabled{background:var(--surface-2); color:var(--ghost); cursor:default}
/* 「今天」鈕與「即時」燈固定同寬：這兩個是互斥的（看歷史日才有今天鈕），
   寬度不一樣的話一按 ◀ 整條靠右對齊的 r1 就會位移，連點時第 2 下會落到別的鈕上。 */
.pager .livelamp{display:inline-flex; align-items:center; justify-content:center; gap:6px;
  font-size:12px; color:var(--dim); min-width:52px;
  padding:4px 6px 4px 4px; line-height:1.15; white-space:nowrap}
.pager .livelamp i{width:6px; height:6px; border-radius:50%; background:var(--gold);
  box-shadow:0 0 0 3px var(--gold-soft)}

/* 月曆改成錨在翻頁列底下的浮層：以前掛在圖下面，展開會把整張 K 線圖往下推、
   而且離入口很遠。#cpick 仍然是獨立節點（不併進 #chead）—— #chead 每秒跟著報價重繪，
   月曆若在裡面會被整個重建，滑鼠停在哪一格都會被打斷。 */
.cheadwrap{position:relative}
.calpop{position:absolute; top:calc(100% + 8px); right:0; z-index:30}
.calpop .calbox{margin-top:0; box-shadow:var(--shadow-2)}
.calbox{margin-top:10px; border:1px solid var(--line); border-radius:14px;
  padding:12px 14px 13px; background:var(--surface-2); width:max-content; max-width:100%}
.calhead{display:flex; align-items:center; justify-content:space-between; gap:18px;
  margin-bottom:10px}
.calhead .mo{font-family:var(--font-mono); font-size:12.5px; font-weight:700}
.calhead .cnav{display:flex; gap:5px}
.calhead .cnav button{width:24px; height:24px; border-radius:6px; font-size:12px;
  display:grid; place-items:center; padding:0; cursor:pointer;
  background:var(--surface); color:var(--dim); border:1px solid var(--line-soft)}
.calhead .cnav button:hover:not(:disabled){border-color:var(--gold-line); color:var(--gold)}
.calhead .cnav button:disabled{opacity:.3; cursor:default}
.cal{display:grid; grid-template-columns:repeat(5,48px); gap:5px}
.cal .wd{font-size:10px; color:var(--faint); text-align:center; letter-spacing:1px}
.cal .cell{position:relative; height:44px; border-radius:10px; border:1px solid transparent;
  background:var(--bg); cursor:pointer; padding:5px 0 0; display:flex; flex-direction:column;
  align-items:center; gap:2px; font-family:var(--font-mono)}
.cal .cell .dd{font-size:13.5px; font-weight:700; line-height:1.1;
  font-variant-numeric:tabular-nums}
.cal .cell .pc{font-size:9px; line-height:1; opacity:.85}
.cal .cell .rngbar{position:absolute; left:7px; right:7px; bottom:5px; height:2px;
  border-radius:1px; opacity:.5}
.cal .cell:hover{border-color:var(--line)}
.cal .cell.up .dd,.cal .cell.up .pc{color:var(--up)} .cal .cell.up .rngbar{background:var(--up)}
.cal .cell.dn .dd,.cal .cell.dn .pc{color:var(--down)} .cal .cell.dn .rngbar{background:var(--down)}
.cal .cell.na .dd{color:var(--dim)}
/* 休市與空格：不能點，也不要看起來像能點 */
.cal .cell.off{background:transparent; cursor:default; border-color:transparent}
.cal .cell.off .dd{color:var(--ghost); font-weight:400}
.cal .cell.prac{border-color:var(--gold-line)}
.cal .cell.on{background:var(--gold-soft); border-color:var(--gold)}
.cal .cell.today::after{content:''; position:absolute; top:5px; right:6px; width:5px;
  height:5px; border-radius:50%; background:var(--gold)}
.callegend{display:flex; gap:14px; flex-wrap:wrap; margin-top:11px; font-size:10.5px;
  color:var(--faint)}
.callegend i{display:inline-block; width:8px; height:8px; border-radius:3px;
  vertical-align:-1px; margin-right:4px}
/* 窄視窗：只收掉「裝飾」（鍵盤提示，那裡通常也沒有實體鍵盤），
   夜盤範圍與練習結果是資訊、不可藏；月曆改靠左展開才不會超出右邊界。
   不要對 .pager 下 width:100% —— 那會逼它提早換行，實測反而多吃一整行。 */
@media(max-width:820px){
  .pager .r2 .kbdgrp,.pager .r2 .sep.k{display:none}
  .calpop{right:auto; left:0}
}
/* ── 資料軌：取代舊的 mini 一行純文字（即時）與 7 格方塊（回顧），兩邊共用同一個元件。
   舊版 9 個數字同字級同顏色、只用空白隔開 ⇒ 讀起來是一長串連續的字。
   新版：①標籤在上、數值在下 ②分組（動能／今天／盤口／現貨）中間有分隔線
        ③位階與量能各給一條 3px 量尺 —— 只是把已發生的數字畫成長度，
          不得加任何「強／弱／偏多」之類的評語（CLAUDE.md 第一段）。 */
.rail{display:flex; align-items:stretch; flex-wrap:wrap; margin-top:12px; padding-top:11px;
  border-top:1px solid var(--line-soft)}
.rail .grp{display:flex; gap:18px; padding:0 18px; border-right:1px solid var(--line-soft)}
.rail .grp:first-child{padding-left:0}
.rail .grp:last-child{border-right:0; padding-right:0}
.rail .it{min-width:44px}
.rail .k{font-size:10.5px; color:var(--faint); letter-spacing:.4px; white-space:nowrap; line-height:1.3}
.rail .v{font-size:15px; font-weight:650; font-family:var(--font-mono);
  font-variant-numeric:tabular-nums; line-height:1.25; margin-top:2px; white-space:nowrap}
.rail .v small{font-size:10.5px; font-weight:500; color:var(--faint); margin-left:2px}
.rail .v i{font-style:normal; color:var(--faint)}      /* 「未開盤」這種非數值 */
/* 加權後面的漲跌：比點數小一級，紅漲綠跌。用 em 不用 small ——
   small 已經被單位（「倍」）佔走，兩者字級與顏色都不一樣。 */
.rail .v em{font-style:normal; font-size:11.5px; font-weight:600; margin-left:6px;
  letter-spacing:0}
.rail .v em.up{color:var(--up)} .rail .v em.down{color:var(--down)}
.rail .v em.flat{color:var(--faint)}
.rail .track{height:3px; border-radius:2px; background:var(--surface-2); margin-top:5px;
  overflow:hidden; position:relative}
.rail .track i{position:absolute; top:0; bottom:0; left:0; border-radius:2px; background:var(--dim)}
.rail .track i.hot{background:var(--gold)}
.rail .muted .v{color:var(--dim)}
/* 回顧分頁的資料軌自己包在一張卡裡，卡片本身就是分隔，不用再畫上邊線 */
#rfstrip{margin-top:0; padding-top:0; border-top:0}
.btns{display:flex; gap:10px}
.btn{flex:1; padding:14px; border-radius:var(--r-md); cursor:pointer; border:1px solid transparent;
  font-family:var(--font-sans); font-size:15.5px; font-weight:700; letter-spacing:1px;
  transition:transform .1s var(--ease), background .15s var(--ease), border-color .15s var(--ease)}
.btn.long{background:var(--up-soft); color:var(--up); border-color:var(--up-line);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.05)}
.btn.short{background:var(--down-soft); color:var(--down); border-color:var(--down-line);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.05)}
.btn.long:hover:not(:disabled){background:rgba(238,90,84,.22)}
.btn.short:hover:not(:disabled){background:rgba(52,179,126,.22)}
.btn.flat2{background:var(--surface-2); color:var(--text); border-color:var(--line)}
.btn.ghost{flex:0 0 auto; background:transparent; color:var(--faint); font-size:13px;
  font-weight:500; border-color:var(--line)}
.btn.ghost:hover:not(:disabled){color:var(--text); border-color:var(--faint)}
.btn:active:not(:disabled){transform:scale(.985)}
/* 沒有即時報價時的下單按鈕：真的停用，並在底下寫清楚為什麼（不是純視覺的灰） */
.btn:disabled{opacity:.3; cursor:not-allowed}
.btn:disabled:active{transform:none}
.whyoff{margin-top:10px; font-size:11.5px; color:var(--faint); line-height:1.6}
/* ═══ 真實下單 ═══
   刻意跟練習長得不一樣：誤按是這件事最大的風險，兩組按鈕長得像就遲早會出事。
   金色外框＋明確寫「真實下單」，而且預設收起來、每次開面板都要重新打開。 */
.real{margin-top:14px; border-color:var(--gold-line);
  background:linear-gradient(180deg,var(--raise),#161C24)}
.real .sec-head h2{color:var(--gold)}
.rtop{display:flex; align-items:center; justify-content:space-between; gap:10px}
.rlabel{font-size:12.5px; color:var(--dim)}
.rlabel b{color:var(--text); font-weight:650}
.sw{width:46px; height:26px; border-radius:999px; background:var(--surface-2);
  border:1px solid var(--line); position:relative; cursor:pointer; flex:none;
  transition:background .18s var(--ease), border-color .18s var(--ease)}
.sw i{position:absolute; top:3px; left:3px; width:18px; height:18px; border-radius:50%;
  background:var(--ghost); transition:transform .18s var(--ease), background .18s var(--ease)}
.sw.on{background:var(--gold-soft); border-color:var(--gold-line)}
.sw.on i{transform:translateX(20px); background:var(--gold)}
.rbody{margin-top:14px; padding-top:14px; border-top:1px solid var(--line-soft)}
.rrow{display:flex; justify-content:space-between; font-size:12.5px; padding:4px 0}
.rrow .k{color:var(--faint)}
.rrow .v{font-family:var(--font-mono); font-variant-numeric:tabular-nums}
.rbtns{display:flex; gap:9px; margin-top:13px}
.rbtn{flex:1; padding:15px 10px; border-radius:var(--r-md); cursor:pointer; font-family:inherit;
  font-size:14.5px; font-weight:700; border:1px solid; letter-spacing:.5px;
  position:relative; overflow:hidden}
.rbtn.b{background:var(--up-soft); color:var(--up); border-color:var(--up-line)}
.rbtn.s{background:var(--down-soft); color:var(--down); border-color:var(--down-line)}
.rbtn:disabled{opacity:.28; cursor:not-allowed}
/* 長按送出：確認框要多一次移動＋點擊，下單當下那一兩秒很要命（Benson 2026-08-28）。
   長按只有一個動作、原地不動，而且誤觸點一下不會送。
   按住的過程中才把停利停損長出來 —— 把確認塞進等待裡，不另外花時間。 */
.rbtn .fill{position:absolute; left:0; top:0; bottom:0; width:0; background:currentColor;
  opacity:.22; pointer-events:none}
.rbtn.holding .fill{transition:width var(--hold,650ms) linear; width:100%}
.rbtn .hint{display:block; font-size:10.5px; font-weight:600; margin-top:3px;
  font-family:var(--font-mono); opacity:0; transition:opacity .12s var(--ease)}
.rbtn.holding .hint{opacity:.95}
.rbtn span{position:relative}
/* 載入中的三個點。⚠️ 這一塊**不是**每 0.5 秒重繪的節點（只有 partial 變了才換），
   所以可以掛動畫；報價那些每 tick 重建的地方一律不准掛（面板開發鐵律）。 */
.dots{display:inline-flex; gap:4px; margin-left:6px; vertical-align:middle}
.dots i{width:5px; height:5px; border-radius:50%; background:var(--gold);
  animation:dotpulse 1.1s var(--ease) infinite}
.dots i:nth-child(2){animation-delay:.18s}
.dots i:nth-child(3){animation-delay:.36s}
@keyframes dotpulse{0%,60%,100%{opacity:.22} 30%{opacity:1}}
@media (prefers-reduced-motion:reduce){.dots i{animation:none; opacity:.6}}
.quota{font-size:11px; color:var(--faint); font-family:var(--font-mono);
  text-align:right; margin-top:9px}
/* 今天的真實交易成績單。⚠️ 這一塊每 0.5 秒跟著卡片重繪，所以不掛任何動畫。 */
/* 筆數多了要能捲，不可以把整張卡撐長（lab-ux 實測 7 筆就把右欄撐到 1259.9px）。
   ⚠️ 有 max-height 的 flex 直欄，子元素一定要 flex:none，否則每一列會被壓扁。 */
.rtrades{margin-top:12px; padding-top:10px; border-top:1px solid var(--line)}
.rtlist{max-height:168px; overflow-y:auto; display:flex; flex-direction:column}
.rtlist>*{flex:none}
.rth{display:flex; align-items:baseline; gap:8px; font-size:12px; margin-bottom:6px}
.rnet{margin-left:auto; font-family:var(--font-mono); font-size:13px; font-weight:600}
.rtrow{display:flex; align-items:center; gap:8px; padding:4px 0;
  font-size:12px; font-family:var(--font-mono)}
.rtrow+.rtrow{border-top:1px solid var(--line)}
.rtt{color:var(--dim); min-width:132px}
.rtp{color:var(--text); min-width:118px}
.rtw{margin-left:auto; font-size:11px}
.rtn{min-width:52px; text-align:right; font-weight:600}
/* 有真實部位：整張卡換成部位的顏色，一眼看得出在玩真的 */
.real.holding{border-color:var(--up-line);
  background:linear-gradient(180deg,rgba(238,90,84,.10),#161C24)}
.real.holding.sh{border-color:var(--down-line);
  background:linear-gradient(180deg,rgba(52,179,126,.10),#161C24)}
.rpos{display:flex; align-items:baseline; gap:10px; margin-bottom:6px}
.rpos .big{font-size:34px; font-weight:700; font-family:var(--font-mono); line-height:1}
.rpos .tag{font-size:11px; font-weight:700; padding:3px 9px; border-radius:var(--r-xs)}
.rpos .tag.l{background:var(--up-soft); color:var(--up)}
.rpos .tag.s{background:var(--down-soft); color:var(--down)}
.ralarm{background:var(--gold-soft); border:1px solid var(--gold-line);
  border-radius:var(--r-md); padding:11px 13px; margin-top:12px; font-size:12.5px; line-height:1.6}
.ralarm.bad{background:var(--up-soft); border-color:var(--up-line)}
@keyframes kk-puls{0%,100%{opacity:1}50%{opacity:.55}}
.ralarm.bad{animation:kk-puls 1.1s ease-in-out infinite}
@media (prefers-reduced-motion: reduce){
  .ralarm.bad{animation:none}
  .rbtn.holding .fill{transition:none; width:100%}
}

/* ═══════════════════════════════════════════════════════════════════════
   【右欄兩個分頁：練習 ／ 真實】2026-09-01（lab-ux v2 提案，老闆已拍板）
   -----------------------------------------------------------------------
   一眼看得出站在哪一區＝四個訊號同時變，沒有一個需要讀字：
     ① 頁籤填色（練習深灰實心／真實金色實心）
     ② 整區外框（真實有金線＋陰影，全頁最重）
     ③ 左緣 4px 直帶貫穿整區（灰／金漸層）
     ④ 下單鈕形狀（練習描邊＋淡底／真實實心）—— 形狀在餘光裡最強
   結構本身就是防呆：站在練習分頁時，畫面上根本沒有真實下單鈕。

   ⛔ 修飾字一律加前綴（t- / z- / c- / n-）。裸寫 .real 會被既有的
      .real{margin-top:14px; background:linear-gradient(...)} 套中 ——
      lab-ux 實測第二顆頁籤整個往下掉 14px、未選取時還吃到別人的漸層底。
   ⛔ 也不可以拿 .warn 當修飾字：既有的 .warn 是金色圓角藥丸，
      套到 .n-g 上整列會變成藥丸。要標「該注意」一律用 .n-att。
   ⚠️ 這一整區每 0.5 秒會被重繪，所以一個 CSS animation 都不掛。
   ═══════════════════════════════════════════════════════════════════════ */
/* 【鐵律】有 max-height 的 flex 直欄，子元素一定要 flex:none，否則不是捲動
   而是把每一列壓扁 —— 筆數少的時候完全看不出來。 */
#tab-live .right>*{flex:none}
/* 沒有警報時完全不佔位（.right 有 gap:14px，空的節點照樣會多一段空隙） */
#tab-live #xal:empty{display:none}
#tab-live #xal{position:sticky; top:8px; z-index:20}

/* ── ① 跨分頁警報：站在練習分頁也看得到 ───────────────────────── */
.n-x{border-radius:var(--r-md); padding:12px 14px; font-size:13px; font-weight:700;
  line-height:1.5; display:flex; align-items:center; gap:12px}
.n-x.n-bad{background:var(--up); color:#14171C}
.n-x.n-att{background:var(--gold); color:#14171C}
.n-x .g{flex:1; min-width:0}
.n-x .s{font-weight:600; opacity:.85; font-size:11.5px; margin-top:2px}
.n-x .num{font-family:var(--font-mono); font-variant-numeric:tabular-nums}
.n-x .go{flex:none; border:0; background:rgba(0,0,0,.24); color:inherit; font-weight:750;
  font-family:inherit; font-size:12px; padding:8px 12px; border-radius:8px; cursor:pointer;
  white-space:nowrap}
.n-x .go:hover{background:rgba(0,0,0,.36)}

/* ── ② 頁籤 ───────────────────────────────────────────────────── */
.n-tabs{display:flex; gap:8px; align-items:flex-start}
.n-tab{position:relative; flex:1 1 0; min-width:0; display:flex; flex-direction:column;
  justify-content:center; border:1px solid var(--line); background:transparent; cursor:pointer;
  border-radius:13px; padding:11px 10px 10px; text-align:center; color:var(--dim);
  font-family:var(--font-sans);
  /* 高度寫死：兩顆頁籤必須分毫不差。改字級要重量一次，不准用字級推算。 */
  height:62px;
  transition:background .14s var(--ease), color .14s var(--ease), border-color .14s var(--ease)}
.n-tab .t{font-size:14.5px; font-weight:800; letter-spacing:2px; line-height:1.2}
.n-tab .s{font-size:10.5px; font-weight:600; letter-spacing:.4px; margin-top:2px; opacity:.85}
.n-tab:hover{color:var(--text); border-color:var(--ghost)}
.n-tab.on.t-sim{background:var(--surface-2); color:var(--text); border-color:var(--ghost)}
.n-tab.on.t-real{background:var(--gold); color:#14171C; border-color:var(--gold);
  box-shadow:0 6px 18px -10px rgba(227,169,81,.8)}
/* 沒站在真實分頁而真實有部位：頁籤右上角直接掛浮動點數（連賺還賠都不用點進去看）。
   ⚠️ 它是獨立節點，不跟著整條頁籤重建（點頁籤的那一瞬間按鈕不可以被換掉）。 */
.n-tab .badge{position:absolute; top:-8px; right:-6px; font-family:var(--font-mono);
  font-variant-numeric:tabular-nums; font-size:12px; font-weight:800; padding:3px 8px;
  border-radius:20px; border:2px solid var(--bg); line-height:1.15}
.n-tab .badge.up{background:var(--up); color:#14171C}
.n-tab .badge.down{background:var(--down); color:#0E1116}
.n-tab .badge.flat{background:var(--ghost); color:var(--text)}
.n-tab .badge.alert{background:var(--up); color:#14171C}

/* ── ③ 分區容器 ───────────────────────────────────────────────── */
.n-zone{position:relative; border-radius:var(--r-lg); border:1px solid; overflow:hidden}
.n-zone::before{content:''; position:absolute; left:0; top:0; bottom:0; width:4px}
.n-zone.z-sim{background:#12161C; border-color:var(--line-soft)}
.n-zone.z-sim::before{background:var(--ghost)}
.n-zone.z-real{background:linear-gradient(180deg,#1D242F,#151A22); border-color:var(--gold-line);
  box-shadow:0 0 0 1px rgba(227,169,81,.10), var(--shadow-1)}
.n-zone.z-real::before{background:linear-gradient(180deg,var(--gold),rgba(227,169,81,.25))}
.n-hd{display:flex; align-items:center; gap:12px; padding:14px 18px 13px 20px}
.n-hd .t{font-size:13px; font-weight:750; letter-spacing:1.4px; color:var(--text)}
.n-hd .s{font-size:11.5px; color:var(--dim); margin-top:2px; font-family:var(--font-mono)}
.n-hd .grow{flex:1; min-width:0}
.n-chip{font-size:10.5px; font-weight:700; letter-spacing:.6px; border-radius:5px;
  padding:3px 8px; white-space:nowrap}
.n-chip.c-real{background:var(--gold); color:#151A22}
.n-chip.c-sim{background:var(--surface-2); color:var(--dim); border:1px solid var(--line)}
.n-chip.c-off{background:transparent; color:var(--faint); border:1px solid var(--line)}
.n-sep{height:1px; background:var(--line-soft); margin:0 18px 0 20px}
.n-bd{padding:0 18px 16px 20px}
.n-bd.n-bd-t{padding-top:14px}

/* 現價（空手態）*/
.n-px{display:flex; align-items:baseline; justify-content:space-between; gap:10px; padding:12px 0 4px}
.n-px .n{font-size:30px; font-weight:700; font-family:var(--font-mono);
  font-variant-numeric:tabular-nums; letter-spacing:-1px; line-height:1}
.n-px .k{font-size:11px; color:var(--dim); letter-spacing:1.4px}
.n-px .qty{font-size:11.5px; color:var(--dim); font-family:var(--font-mono)}

/* 真實下單鈕：實心。跟練習區的「描邊＋淡底」在形狀上分家。 */
.n-fire{display:flex; gap:10px; margin-top:12px}
.n-fb{flex:1; position:relative; overflow:hidden; border:0; cursor:pointer;
  border-radius:var(--r-md); padding:16px 10px 14px; color:#12151B; font-weight:800;
  font-size:16px; letter-spacing:1px; text-align:center; line-height:1.15;
  font-family:var(--font-sans)}
.n-fb.b{background:var(--up)} .n-fb.s{background:var(--down)}
.n-fb:disabled{background:var(--surface-2); color:var(--faint); cursor:not-allowed}
.n-fb .txt{position:relative; z-index:2}
/* 【停利停損：還沒選方向就不顯示】兩個方向的數字互相干擾（Benson 2026-08-28 要求）。
   按住之後才浮出來 —— 那時方向已經選定了。 */
.n-fb .sub{display:block; font-size:11px; font-weight:700; font-family:var(--font-mono);
  letter-spacing:0; opacity:0; margin-top:5px; transition:opacity .12s var(--ease)}
.n-fb.holding .sub{opacity:.78}
.n-fb .lb2{display:none}
.n-fb.holding .lb{display:none}
.n-fb.holding .lb2{display:inline}
.n-fb.holding{box-shadow:inset 0 0 0 999px rgba(0,0,0,.20)}
.n-fb .bar{position:absolute; left:0; bottom:0; height:4px; width:0; background:#12151B;
  opacity:.55; z-index:3}
.n-fb.holding .bar{transition:width var(--hold,650ms) linear; width:100%}
.n-holdhint{font-size:11.5px; color:var(--dim); text-align:center; margin-top:9px;
  font-family:var(--font-mono)}

/* 會影響他決定的說明字：最低只能用 --dim（5.37:1）。--faint（2.72:1）只留給版本指紋。 */
.n-why{margin-top:11px; font-size:12.5px; color:var(--dim); line-height:1.6;
  background:var(--surface-2); border-radius:var(--r-sm); padding:9px 11px}
.n-why b{color:var(--text)}
.n-quota{display:flex; align-items:center; gap:9px; margin-top:12px; font-size:11.5px;
  color:var(--dim); font-family:var(--font-mono)}
.n-quota .pips{display:flex; gap:4px}
.n-quota .pips i{width:16px; height:5px; border-radius:3px; background:var(--ghost)}
.n-quota .pips i.used{background:var(--dim)}
.n-quota .pips i.full{background:var(--gold)}
.n-firing{margin-top:12px; background:var(--surface-2); border-radius:var(--r-md);
  padding:13px 14px; font-size:13.5px; color:var(--text); font-weight:650}
.n-firing .s{font-size:11.5px; color:var(--dim); font-weight:500; margin-top:4px; line-height:1.5}
.n-firing .bar{height:3px; border-radius:2px; background:var(--line); margin-top:10px; overflow:hidden}
.n-firing .bar i{display:block; height:100%; background:var(--gold); width:30%}

/* 持倉態：浮動點數 34px → 52px（全頁最大的損益數字不可以是模擬的） */
.n-pos{display:flex; align-items:flex-end; justify-content:space-between; gap:14px; padding:14px 0 2px}
.n-pos .big{font-size:52px; font-weight:700; font-family:var(--font-mono);
  font-variant-numeric:tabular-nums; line-height:.92; letter-spacing:-1.6px}
.n-pos .u{font-size:15px; font-weight:650; margin-left:5px; letter-spacing:0}
.n-pos .cash{font-size:12.5px; color:var(--dim); font-family:var(--font-mono); margin-top:6px}
.n-dir{display:inline-flex; align-items:center; gap:6px; font-size:12.5px; font-weight:750;
  padding:5px 10px; border-radius:var(--r-xs); white-space:nowrap}
.n-dir.l{background:var(--up-soft); color:var(--up)}
.n-dir.s{background:var(--down-soft); color:var(--down)}
.n-dir.u{background:var(--surface-2); color:var(--text); border:1px solid var(--gold-line)}
.n-meta{font-size:11.5px; color:var(--dim); font-family:var(--font-mono); text-align:right; margin-top:6px}

/* 保護狀態列：紅綠只描述錢（漲跌／賺賠），系統狀態一律中性灰階＋填滿程度。
   ● 實心白＝掛在券商（電腦關機也有效）／○ 空心＝這台電腦在看（正常，不是警告）／● 紅＝沒掛上或監控不到 */
.n-guard{margin-top:14px; border:1px solid var(--line-soft); border-radius:var(--r-md);
  overflow:hidden; background:rgba(0,0,0,.16)}
.n-g{display:flex; align-items:center; gap:11px; padding:11px 13px}
.n-g+.n-g{border-top:1px solid var(--line-soft)}
.n-g .ic{width:9px; height:9px; border-radius:50%; flex:none; box-sizing:content-box}
.n-g .ic.solid{background:var(--text); box-shadow:0 0 0 3px rgba(233,236,241,.10)}
.n-g .ic.hollow{background:transparent; border:2px solid var(--dim); width:5px; height:5px}
.n-g .ic.bad{background:var(--up); box-shadow:0 0 0 3px var(--up-soft)}
.n-g .lb{font-size:12.5px; color:var(--dim); min-width:74px}
.n-g .lb b{color:var(--text); font-weight:650; font-family:var(--font-mono)}
.n-g .st{margin-left:auto; font-size:12px; color:var(--dim); text-align:right; line-height:1.45}
.n-g .st b{color:var(--text); font-weight:650}
/* 要注意的那一列：底色用中性提亮，不用紅底 —— 紅底只留給「請你立刻動手」的橫幅 */
.n-g.n-att{background:rgba(255,255,255,.045)}
.n-g.n-att .st b{color:var(--up)}
.n-gfoot{font-size:11.5px; color:var(--dim); line-height:1.6; padding:0 13px 11px; margin-top:-3px}

/* 平倉：中性底。平倉不是方向動作，不該吃紅綠 —— 做空的平倉送出去的是「買進」，
   09-01 就出過「平倉送錯邊、變成再加一口空單」的事故。鈕上直接寫會送出什麼。 */
.n-close{width:100%; margin-top:14px; border:1px solid var(--line); background:var(--surface-2);
  color:var(--text); font-size:15px; font-weight:750; letter-spacing:1px; padding:15px 10px 13px;
  border-radius:var(--r-md); cursor:pointer; font-family:var(--font-sans);
  transition:background .14s var(--ease)}
.n-close:hover:not(:disabled){background:#232A36}
.n-close .sub{display:block; font-size:11.5px; font-weight:600; color:var(--dim); letter-spacing:0;
  margin-top:4px; font-family:var(--font-mono)}
.n-close:disabled{opacity:.45; cursor:not-allowed}

/* ── ④ 紀錄表：固定五欄，欄寬不隨內容跳；超過就捲，每一列 flex:none ── */
.n-trh{display:flex; align-items:baseline; gap:8px; font-size:11.5px; color:var(--dim);
  letter-spacing:1.2px; font-weight:650; padding:13px 18px 7px 20px;
  border-top:1px solid var(--line-soft); margin-top:2px}
.n-trh .c{letter-spacing:0; color:var(--faint); font-weight:500}
.n-trh .net{margin-left:auto; font-family:var(--font-mono); font-size:14px; font-weight:700;
  letter-spacing:0; font-variant-numeric:tabular-nums}
.n-trl{max-height:186px; overflow-y:auto; display:flex; flex-direction:column;
  padding:0 18px 4px 20px}
.n-trl>*{flex:none}
.n-trl::-webkit-scrollbar{width:6px}
.n-trl::-webkit-scrollbar-thumb{background:var(--line); border-radius:3px}
.n-item{padding:2px 0 6px; border-top:1px solid var(--line-soft)}
.n-item:first-child{border-top:0}
/* （2026-09-03 A 版拿掉了 .n-dayh —— 那是舊「過去的真實交易」分日標頭專用的，
    跨日紀錄改由成績區底下的 .trade 卡片清單承擔，見 realCard()。） */
.n-row{display:grid; grid-template-columns:18px 92px 1fr 68px 58px; align-items:center;
  gap:9px; padding:8px 0; font-size:12px; font-family:var(--font-mono);
  font-variant-numeric:tabular-nums; border-top:1px solid var(--line-soft)}
.n-trl>.n-row:first-child, .n-item .n-row{border-top:0}
.n-row .d{font-size:11px; text-align:center}
.n-row .d.l{color:var(--up)} .n-row .d.s{color:var(--down)}
.n-row .tm{color:var(--dim)}
/* ⚠️ 價格欄是 1fr ＝ 容器一變窄就是它先被吃掉。nowrap 是為了「不准折行」
   （折行那一列會比別列高，實測 52 vs 36.5px），ellipsis 只是最後的安全網。
   ⛔ 這條規則**擋不住**「容器變窄」——2026-09-03 就是這樣：成績單被包進 .n-bd
      多吃一層左右 38px，價格欄 76px → 38px，畫面上是「4701…」而不是完整成交價，
      而且筆數少的時候看起來完全正常，活了兩天沒人發現。
      看到成交價變成「…」時要去查**容器寬度**（.n-trl 有沒有被多包一層有 padding
      的祖先），不要來這裡加寬度覆寫。守衛：hold-to-fire.mjs ⑧c。 */
.n-row .px{color:var(--text); white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
.n-row .wy{color:var(--dim); font-size:11px; font-family:var(--font-sans); text-align:right;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
.n-row .pt{text-align:right; font-weight:700; font-size:13px}
.n-row .pt.na{color:var(--faint); font-weight:500}
/* 練習紀錄還是要能補心得（跟手機 App 同一個 note 欄位），壓成一行掛在那一列底下 */
.n-item .noteline{white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
  background:none; border:0; padding:0; margin-top:0; font-size:11.5px;
  color:var(--faint); line-height:1.5}
.n-item .noteline[data-nedit]:hover{color:var(--gold); border-color:transparent}
.n-trnote{font-size:11.5px; color:var(--dim); line-height:1.55; padding:8px 18px 0 20px;
  border-top:1px solid var(--line-soft); margin-top:6px}
.n-trnote b{color:var(--text)}
.n-empty{font-size:12.5px; color:var(--dim); line-height:1.6; padding:2px 18px 14px 20px}
/* 版本指紋：純除錯、不影響決定 ⇒ 這裡是唯一還能用 --faint 的地方。整區最底。 */
.n-foot{display:flex; align-items:center; gap:8px; flex-wrap:wrap; padding:9px 18px 11px 20px;
  font-size:10.5px; color:var(--faint); font-family:var(--font-mono);
  border-top:1px solid var(--line-soft)}
.n-foot .stale{color:var(--gold); font-weight:700; font-family:var(--font-sans)}
/* 練習成績的小標（分區裡不再包一張卡，所以不用 .sec-head） */
.n-sh{display:flex; align-items:baseline; justify-content:space-between; gap:10px;
  font-size:11.5px; color:var(--dim); letter-spacing:1.6px; font-weight:650; margin-bottom:9px}
.n-sh .c{font-size:11px; color:var(--faint); letter-spacing:0; font-weight:500;
  font-family:var(--font-mono)}

@media (prefers-reduced-motion: reduce){
  .n-fb.holding .bar{transition:none; width:100%}
}
@media(max-width:640px){
  .n-row{grid-template-columns:16px 78px 1fr 52px}
  .n-row .wy{display:none}
  .n-pos .big{font-size:44px}
}

.warn{font-size:10.5px; color:var(--gold); background:var(--gold-soft);
  border-radius:20px; padding:2px 9px; font-weight:600}
.pnl{text-align:center; padding:4px 0 8px}
.pnl .v{font-size:44px; font-weight:700; font-family:var(--font-mono);
  font-variant-numeric:tabular-nums; line-height:1; letter-spacing:-1px}
.pnl .l{font-size:12px; color:var(--dim); margin-top:7px}
.plimit{display:flex; justify-content:center; gap:14px; font-size:11.5px; color:var(--faint);
  font-family:var(--font-mono); margin-bottom:12px}
.seg{display:flex; gap:4px; background:var(--surface-2); border-radius:10px; padding:3px;
  border:1px solid var(--line-soft); margin-bottom:14px}
.seg button{flex:1; border:0; background:transparent; color:var(--dim); cursor:pointer;
  font-family:var(--font-sans); font-size:12.5px; font-weight:600; padding:7px 4px; border-radius:8px;
  transition:color .15s var(--ease), background .15s var(--ease)}
.seg button:hover{color:var(--text)}
.seg button.on{background:var(--gold-soft); color:var(--gold)}
/* ── 練習成績。舊版全頁最大最亮的數字是「金色的勝率」，而金色在翻頁列的定義是
   「即時／現在」⇒ 一個顏色兩種意思；把勝率捧到視覺頂端也等於暗示這個工具在追勝率。
   現在：勝率降成中性色，紅綠讓給真正的結果（合計點數），另加一條勝敗條看比例。
   （這是設計決定，不要改回金色） */
.score{display:flex; align-items:flex-start; justify-content:space-between; gap:14px}
.score .rate{line-height:1}
.score .rate .n{font-family:var(--font-mono); font-size:40px; font-weight:680; letter-spacing:-1.2px;
  font-variant-numeric:tabular-nums; color:var(--text)}
.score .rate .p{font-size:19px; color:var(--dim); font-family:var(--font-mono); margin-left:1px}
.score .rate .lab{font-size:11px; color:var(--faint); letter-spacing:2px; margin-top:7px}
.score .sum{text-align:right; font-family:var(--font-mono); font-variant-numeric:tabular-nums}
.score .sum .n{font-size:26px; font-weight:700; letter-spacing:-.5px; line-height:1.1}
.score .sum .u{font-size:11px; color:var(--dim); margin-left:3px; font-weight:500}
.score .sum .cash{font-size:11.5px; color:var(--faint); margin-top:5px}
.wlbar{display:flex; height:6px; border-radius:3px; overflow:hidden; margin-top:13px; gap:2px}
.wlbar i{display:block; height:100%; border-radius:3px}
.wlbar i.w{background:var(--up)} .wlbar i.l{background:var(--down)}
.wlfoot{display:flex; justify-content:space-between; font-size:11.5px; color:var(--faint);
  font-family:var(--font-mono); margin-top:6px}
.wlfoot b{font-weight:650}
.wlfoot .w b{color:var(--up)} .wlfoot .l b{color:var(--down)}
.cash{font-size:11px; color:var(--faint); font-family:var(--font-mono)}
/* 交易列表自己捲動，整個儀表板才能一眼看完、不用捲整頁 */
.list{display:flex; flex-direction:column; gap:7px; margin-top:14px;
  max-height:290px; overflow-y:auto; padding-right:4px}
/* 【一定要 flex:none】.list 是有 max-height 的 flex 直欄，子元素的預設 flex-shrink 是 1 ——
   內容一超過就不是捲動，而是把每一列**壓扁**（實測 107px 被壓成 21.6px，字全部切掉）。
   筆數少的時候看不出來（總高沒超過 max-height），紀錄一多就整片糊掉。 */
.list>*{flex:none}
/* 清單裡的心得壓成一行、去掉框與底色 —— 這是「掃結果」用的清單，
   心得在這裡是附註不是主角。帶框的完整樣子留給回顧分頁的「這一筆」。
   （每列 107px 的話 290px 只放得下 2.7 列，等於要一直捲。） */
.trade .noteline{white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
  background:none; border:0; padding:0; margin-top:5px; font-size:11.5px;
  color:var(--faint); line-height:1.5}
.trade .noteline[data-nedit]:hover{color:var(--gold); border-color:transparent}
.list::-webkit-scrollbar{width:6px}
.list::-webkit-scrollbar-thumb{background:var(--line); border-radius:3px}
/* 左緣 2px 的結果色：一眼掃得出賺賠，不必讀數字 */
.trade{background:var(--surface-2); border:1px solid var(--line-soft); border-radius:var(--r-md);
  padding:10px 12px 10px 13px; position:relative; overflow:hidden;
  transition:border-color .15s var(--ease), background .15s var(--ease)}
.trade::before{content:''; position:absolute; left:0; top:0; bottom:0; width:2px; background:var(--ghost)}
.trade.win::before{background:var(--up)} .trade.loss::before{background:var(--down)}
.tr-top{display:flex; align-items:center; gap:9px}
.tr-date{font-family:var(--font-mono); font-size:12px; color:var(--faint); width:42px; flex:none}
.dir{font-size:10.5px; font-weight:700; padding:2px 7px; border-radius:var(--r-xs); flex:none;
  letter-spacing:.3px}
.dir.l{background:var(--up-soft); color:var(--up)}
.dir.s{background:var(--down-soft); color:var(--down)}
.tr-px{flex:1; font-family:var(--font-mono); font-size:12.5px; font-variant-numeric:tabular-nums;
  color:var(--dim)}
.tr-px .arrow{color:var(--ghost); margin:0 3px}
.tr-res{font-family:var(--font-mono); font-size:15px; font-weight:700; text-align:right;
  min-width:52px; flex:none; font-variant-numeric:tabular-nums}
.r-win{color:var(--up)} .r-loss{color:var(--down)}
/* 真實交易問不到成交價時算不出點數 ⇒ 印「—」、灰、不加粗（跟 .n-row .pt.na 同一套）。
   ⛔ 不可以拿現價冒充，也不可以猜輸贏 —— 留白看得出來是缺，編的數字看不出來。 */
.tr-res.na{color:var(--faint); font-weight:500}
.tag{font-size:10px; color:var(--faint); border:1px solid var(--line); border-radius:5px; padding:1px 5px}
.alert{background:var(--up-soft); border:1px solid var(--up-line); border-radius:var(--r-md);
  padding:12px 14px; font-size:12.5px; margin-bottom:12px; line-height:1.6}
.note{background:var(--surface); border:1px solid var(--line-soft); border-radius:var(--r-md);
  padding:12px 14px; font-size:12px; color:var(--dim); margin-bottom:12px; line-height:1.65}
.note b{color:var(--text)}

/* ═══════════ 開啟與載入的動態 ═══════════
   量測過的實際延遲：整頁 17ms、/api/state 2ms、但 /api/bars 要 300~500ms。
   所以「打開面板」的體感就是那半秒 —— 舊版在那半秒塞一張小小的數字卡，
   資料到了再被大圖整個頂掉，版面跳一下。現在改成：
     ① 骨架直接寫在 HTML 裡（第 0 毫秒就在，尺寸跟真圖一模一樣，不會跳）
     ② 真圖淡入蓋過去
     ③ K 線由左往右展開一次（只在第一次與換日時，不是每次重繪）
   ⚠️ 每 0.5 秒重繪的東西一律不准掛動畫：#chead 每次報價變動就整個重建，
      掛上去會變成一直閃。動畫只能掛在「建一次就不動」的容器上。 */
@keyframes kk-rise{from{opacity:0;transform:translateY(9px)}to{opacity:1;transform:none}}
@keyframes kk-fade{from{opacity:0}to{opacity:1}}
@keyframes kk-wipe{from{clip-path:inset(0 100% 0 0)}to{clip-path:inset(0 0 0 0)}}
@keyframes kk-sheen{from{transform:translateX(-60%)}to{transform:translateX(260%)}}
@keyframes kk-bar{from{left:-38%}to{left:100%}}
@keyframes kk-breathe{0%,100%{opacity:.30}50%{opacity:.62}}

/* 進場：只在開站後的頭 1.1 秒有效（body.boot），之後的重繪一律不動 */
body.boot .topbar{animation:kk-rise .40s var(--ease) both}
body.boot #mkt>*{animation:kk-rise .46s var(--ease) both .04s}
body.boot .right>*>.card{animation:kk-rise .46s var(--ease) both}
body.boot .right>#ntabs{animation:kk-rise .46s var(--ease) both .08s}
body.boot .right>#zone{animation:kk-rise .46s var(--ease) both .14s}
/* 真圖接手骨架：淡入就好，不要再 rise 一次（同一個位置動兩次看起來很躁） */
.card.chart.kk-in{animation:kk-fade .34s ease both}
.cwrap.kk-draw svg{animation:kk-wipe .52s var(--ease) both}

/* 骨架：刻意沿用 .chead/.legend/.cwrap/.chint/.rail 這幾個真名字，
   高度才會跟真圖分毫不差。換成自訂 class 就得手動對高度，改一次錯一次。 */
.skel .sk{position:relative; overflow:hidden; border-radius:5px;
  background:var(--surface-2); animation:kk-breathe 1.7s ease-in-out infinite}
.skel .sk::after{content:''; position:absolute; inset:0;
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.045),transparent);
  animation:kk-sheen 1.6s ease-in-out infinite}
/* 【2026-08-25 視覺升級後重量】Chrome 1600×950 實測 getBoundingClientRect().height
   （骨架 → 真圖，同一次載入量兩段）：
     .chead   骨架 72.25 ／ 真圖 72.25   （52px 大字 ＋ qsub 一行）
     .legend  骨架 18.75 ／ 真圖 18.75
     .cwrap   骨架 455.53 ／ 真圖 455.53（同一條 aspect-ratio 1040/470）
     .chint   骨架 16.50 ／ 真圖 16.50
     .rail    骨架 54.38 ／ 真圖 54.39   （資料軌是兩行＋量尺，比舊的 mini 高一截）
     #mkt 總高 695.41 → 695.42，差 0.01px、CLS = 0；.pager 42.17px（上限 45）。
   ⚠ 改任何字級／padding 都要重量一次，不准用字級推算。 */
.skel .chead{align-items:center; min-height:72.25px}
.skel .legend{min-height:18.75px; align-items:center}
.skel .rail{min-height:54.39px; align-items:center}
.skel .sk-px{width:196px; height:44px}
.skel .sk-day{width:118px; height:20px}
.skel .legend .sk{height:11px}
.skel .chint .sk{width:150px; height:10px; display:inline-block}
.skel .rail .sk{width:60px; height:26px; margin-right:18px}
/* 高度＝寬度 × 470/1040，跟真圖的 viewBox 完全一致。
   ⚠️ 假 K 棒一定要絕對定位：當成一般 flex 子元素的話，它們的百分比高度會反過來
      把 .cwrap 撐高（實測 351 → 553），骨架就比真圖高一截，換過去時版面照樣跳。 */
.skel .cwrap{aspect-ratio:1040/470; position:relative}
/* 先把價格軸的格線畫出來，真圖進來時網格不會「突然出現」 */
.skel .cwrap::before{content:''; position:absolute; left:0; right:0; top:12px; bottom:26px;
  background:repeating-linear-gradient(to bottom,var(--line-soft) 0 1px,transparent 1px 20%)}
.skel .bars{position:absolute; left:0; right:0; top:12px; bottom:26px;
  display:flex; align-items:flex-end; gap:2px}
.skel .bars i{flex:1; background:var(--surface-2); border-radius:2px;
  animation:kk-breathe 1.7s ease-in-out infinite}

/* 換日：不要只換一行「載入中」的字。圖先淡下去、頂上跑一條細進度條，
   新資料回來再由左往右展開 —— 這樣看得出「它在做事」而不是卡住。 */
.card.chart{position:relative}
.chart .kk-prog{position:absolute; left:0; right:0; top:0; height:2px; overflow:hidden;
  opacity:0; transition:opacity .2s; pointer-events:none; border-radius:var(--r-lg) var(--r-lg) 0 0}
.chart.kk-load .kk-prog{opacity:1}
.chart .kk-prog i{position:absolute; top:0; height:2px; width:38%;
  background:linear-gradient(90deg,transparent,var(--gold),transparent);
  animation:kk-bar 1.05s linear infinite}
.chart.kk-load .cwrap,.chart.kk-load .legend,.chart.kk-load .rail{opacity:.4}
.chart .cwrap,.chart .legend,.chart .rail{transition:opacity .22s ease}

/* 系統設定「減少動態」就全部關掉 —— 這是看盤工具，不能跟使用者的設定作對。
   一條全域規則最保險：新增元件時不會忘了把它加進白名單。 */
@media (prefers-reduced-motion: reduce){
  *,*::before,*::after{animation:none !important; transition:none !important}
}
.foot{font-size:11px; color:var(--faint); text-align:center; margin-top:20px; line-height:1.7}
.dot{display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--down);margin-right:5px}
.dot.stale{background:var(--gold)} .dot.dead{background:var(--up)}
/* 休市中不是故障 —— 燈號用中性灰，不要每個週末都亮紅燈 */
.dot.off{background:var(--faint)}
.dl{display:inline-block; margin-top:12px; font-size:12px; color:var(--gold); text-decoration:none}

/* ================= 【回顧】分頁（沿用上面的顏色變數，不另立一套） ================= */
[hidden]{display:none !important}
.tabs{display:flex; gap:3px; background:var(--surface-2); border-radius:12px; padding:3px;
  border:1px solid var(--line-soft)}
.tabs button{border:0; background:transparent; color:var(--dim); cursor:pointer; min-width:100px;
  font-family:var(--font-sans); font-size:13.5px; font-weight:650; padding:8px 18px; border-radius:9px;
  transition:color .15s var(--ease), background .15s var(--ease)}
.tabs button:hover{color:var(--text)}
.tabs button.on{background:var(--gold-soft); color:var(--gold)}
#tab-review .card{padding:16px 18px; margin-bottom:10px}
#tab-review .card.chart{padding:16px 18px 13px}
#tab-review .chead{gap:14px}
/* 回顧的大字比即時小一階：這一頁的主角是「那一筆交易」，不是現在的價格 */
#tab-review .cpx{font-size:40px; letter-spacing:-1.2px} #tab-review .cchg{font-size:14px}
#tab-review .cwrap svg{cursor:crosshair}
.cday{font-size:13px; color:var(--dim); font-family:var(--font-mono)}
.ctag{font-size:11px; color:var(--faint); border:1px solid var(--line); border-radius:6px;
  padding:2px 8px; margin-left:6px}
.tfsw{display:flex; gap:3px; background:var(--surface-2); border-radius:var(--r-sm); padding:3px;
  border:1px solid var(--line-soft)}
.tfsw button{border:0; background:transparent; color:var(--dim); cursor:pointer;
  font-family:var(--font-sans); font-size:11.5px; font-weight:600; padding:5px 12px; border-radius:7px}
.tfsw button.on{background:var(--gold-soft); color:var(--gold)}
/* 重播控制列：放在圖的正下方，眼睛不用離開圖。
   舊版是一排長得都一樣的方框按鈕，看不出哪個是主要動作、也看不出「走到哪了」。
   新版兩行：上行運鏡（播放鍵是唯一的金色）、下行時間軸（可點著跳）。 */
.rpbar{margin-top:10px; padding:10px 12px 9px; background:var(--surface-2);
  border:1px solid var(--line-soft); border-radius:14px}
.rprow{display:flex; align-items:center; gap:8px}
.rpbtn{border:1px solid var(--line); background:var(--surface); color:var(--dim); cursor:pointer;
  font-family:var(--font-sans); font-size:13px; font-weight:600; padding:8px 12px;
  border-radius:var(--r-sm); min-width:40px;
  transition:color .15s var(--ease), border-color .15s var(--ease)}
.rpbtn:hover:not(:disabled){color:var(--text); border-color:var(--faint)}
.rpbtn.play{background:var(--gold-soft); color:var(--gold); border-color:transparent;
  min-width:104px; font-size:13.5px; padding:9px 14px}
.rpbtn.play:hover:not(:disabled){background:rgba(227,169,81,.24); color:var(--gold)}
.rpbtn:disabled{opacity:.35; cursor:default}
.rpsp{display:flex; gap:2px; background:var(--surface); border-radius:var(--r-sm); padding:3px;
  border:1px solid var(--line-soft)}
.rpsp button{border:0; background:transparent; color:var(--faint); cursor:pointer;
  font-family:var(--font-mono); font-size:11.5px; font-weight:600; padding:5px 9px;
  border-radius:var(--r-xs)}
.rpsp button:hover{color:var(--text)}
.rpsp button.on{background:var(--gold-soft); color:var(--gold)}
.rppos{flex:1; font-family:var(--font-mono); font-size:12.5px; color:var(--faint);
  font-variant-numeric:tabular-nums; text-align:right; white-space:nowrap}
.rppos b{color:var(--text); font-size:14px; font-weight:650}
/* 時間軸：.win＝08:45~09:30（他真正下單的時段）、.jm＝這次按下判斷的那一根 */
.rpscrub{position:relative; height:20px; margin-top:8px; cursor:pointer}
.rpscrub .trk{position:absolute; left:0; right:0; top:5px; height:4px; border-radius:2px;
  background:var(--surface)}
.rpscrub .win{position:absolute; top:5px; height:4px; background:var(--gold-soft)}
.rpscrub .fill{position:absolute; left:0; top:5px; height:4px; border-radius:2px;
  background:linear-gradient(90deg,rgba(227,169,81,.5),var(--gold))}
.rpscrub .knob{position:absolute; top:2px; width:10px; height:10px; border-radius:50%;
  background:var(--gold); box-shadow:0 0 0 3px rgba(227,169,81,.18); margin-left:-5px}
.rpscrub .jm{position:absolute; top:0; width:2px; height:14px; border-radius:1px; margin-left:-1px}
.rpscrub .tk{position:absolute; top:12px; font-size:9.5px; color:var(--ghost);
  font-family:var(--font-mono); transform:translateX(-50%)}
.rpscrub.locked{cursor:default}
.kbd{font-family:var(--font-mono); font-size:10.5px; color:var(--faint);
  border:1px solid var(--line); border-radius:5px; padding:1px 5px; background:var(--surface-2)}
#tab-review .seg{margin-bottom:0}
.chips{display:flex; gap:6px; flex-wrap:wrap; margin:0 0 12px}
.chips button{font-size:11.5px; padding:5px 11px; border-radius:20px; cursor:pointer;
  background:var(--surface-2); color:var(--dim); border:1px solid var(--line-soft);
  font-family:var(--font-sans); transition:color .15s var(--ease), background .15s var(--ease)}
.chips button:hover{color:var(--text)}
.chips button.on{background:var(--gold-soft); color:var(--gold); border-color:transparent}
/* 目標：1600×950 一頁看完、不捲整頁。右欄本身留一道安全閥（視窗更矮時右欄自己捲，
   整頁還是不捲），清單則維持自己的捲動區。 */
#tab-review .right{max-height:calc(100vh - 96px); overflow-y:auto; padding-right:2px}
#tab-review .right::-webkit-scrollbar{width:6px}
#tab-review .right::-webkit-scrollbar-thumb{background:var(--line); border-radius:3px}
#rpane .list{max-height:196px; gap:6px}
#rpane .card{padding:13px 15px}
#rpane .dt{gap:5px}
#rpane .dt-big{padding:0 0 4px}
#rpane .dt-big .v{font-size:32px}
#rpane .noteline{padding:7px 10px}
#rpane .btn{padding:10px}
#rpane .trade{padding:9px 12px}
#rpane .trade{cursor:pointer}
#rpane .trade:hover{border-color:var(--faint)}
.trade.sel{border-color:var(--gold-line); background:rgba(227,169,81,.08)}
.tr-note{font-size:11.5px; color:var(--faint); margin-top:6px; padding-left:51px;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
.dt{display:flex; flex-direction:column; gap:9px}
.dt-row{display:flex; justify-content:space-between; align-items:baseline; font-size:12.5px}
.dt-row .k{color:var(--dim)}
.dt-row .v{font-family:var(--font-mono); font-variant-numeric:tabular-nums; font-size:13.5px}
.dt-big{text-align:center; padding:0 0 6px}
.dt-big .v{font-size:38px; font-weight:700; font-family:var(--font-mono); line-height:1;
  font-variant-numeric:tabular-nums; letter-spacing:-1px}
.dt-big .l{font-size:12px; color:var(--dim); margin-top:6px}
.hr{height:1px; background:var(--line-soft); margin:2px 0}
.noteline{font-size:12.5px; color:var(--text); background:var(--surface-2);
  border:1px solid var(--line-soft); border-radius:var(--r-sm); padding:9px 11px; line-height:1.6}
.noteline.empty{color:var(--faint)}
.noteline[data-nedit]{cursor:pointer;
  transition:border-color .15s var(--ease), color .15s var(--ease)}
.noteline[data-nedit]:hover{border-color:var(--gold-line); color:var(--gold)}
/* 卡片內的心得再壓一層底色（同色系會糊在一起） */
.nedit{margin-top:8px}
.nedit textarea{width:100%; box-sizing:border-box; min-height:78px; resize:vertical;
  background:var(--surface-2); border:1px solid var(--gold-line); border-radius:var(--r-sm);
  color:var(--text); font-family:var(--font-sans); font-size:12.5px; line-height:1.6;
  padding:9px 11px}
.nedit textarea::placeholder{color:var(--faint)}
.nedit textarea:focus{outline:none; border-color:var(--gold)}
.nedit .nbtn{display:flex; gap:8px; margin-top:7px}
.nedit .nbtn .btn{flex:1; padding:7px 0; font-size:12.5px}
.empty{text-align:center; padding:32px 16px; color:var(--faint); font-size:12.5px; line-height:1.8}
.btn.gold{background:var(--gold-soft); color:var(--gold); border-color:transparent}
.btn.gold:hover:not(:disabled){background:rgba(227,169,81,.24)}
.btn.gw{flex:1}                     /* 回顧頁的次要按鈕要跟主按鈕一樣寬 */
.daysel{display:flex; gap:6px; flex-wrap:wrap; margin-top:10px}
.daysel button{font-size:12px; padding:6px 10px; border-radius:var(--r-sm); cursor:pointer;
  background:var(--surface-2); color:var(--dim); border:1px solid var(--line-soft);
  font-family:var(--font-mono)}
.daysel button:hover{color:var(--text)}
.daysel button.on{background:var(--gold-soft); color:var(--gold); border-color:transparent}
.daysel button .m{font-size:9.5px; color:var(--faint); margin-left:4px}
.jinput{width:100%; background:var(--surface-2); border:1px solid var(--line); border-radius:9px;
  color:var(--text); font-family:var(--font-sans); font-size:13px; padding:10px 11px; margin-top:9px}
.jinput::placeholder{color:var(--faint)}
.jinput:focus{outline:none; border-color:var(--gold-line)}
.hold{background:var(--surface-2); border:1px solid var(--line-soft); border-radius:var(--r-md);
  padding:12px 14px}
.hold .v{font-size:34px; font-weight:700; font-family:var(--font-mono); line-height:1;
  font-variant-numeric:tabular-nums; text-align:center; letter-spacing:-1px}
.hold .l{font-size:12px; color:var(--dim); text-align:center; margin-top:6px}
.cmp{display:flex; flex-direction:column; gap:8px}
.cmp .side{background:var(--surface-2); border:1px solid var(--line-soft); border-radius:var(--r-md);
  padding:10px 12px}
.cmp .side.mine{border-color:var(--gold-line)}
.cmp .side .h{font-size:11px; color:var(--dim); margin-bottom:5px; letter-spacing:.5px}
.cmp .side .b{display:flex; align-items:center; gap:8px; font-family:var(--font-mono); font-size:13px}
.cmp .side .b .res{margin-left:auto; font-size:16px; font-weight:700}
.verdict{border-radius:var(--r-md); padding:10px 12px; font-size:12.5px; line-height:1.6;
  text-align:center; font-weight:600}
.verdict.same{background:var(--gold-soft); color:var(--gold)}
.verdict.diff{background:var(--surface-2); color:var(--dim); border:1px solid var(--line-soft)}
.tally{display:flex; gap:14px; justify-content:center; font-family:var(--font-mono); font-size:12px;
  color:var(--faint); padding-top:8px; flex-wrap:wrap}
.tally b{color:var(--text); font-size:14px}

/* ══ 【細節】分頁：逐筆早盤圖 ═══════════════════════════════════════════
   ⛔ class 一律 `tk-` 前綴 —— **不可以用 `dt-`**，回顧分頁的「當天資料」
      （.dt / .dt-row / .dt-big）已經佔走了，撞了會互相污染。
   ⛔ 一個新顏色都不准加：全部用 :root 既有的 token。
   ⚠️ 有 max-height 的 flex 直欄，子元素一律 flex:none（面板鐵律，2026-08-25 踩過：
      預設 flex-shrink:1 會把每一列壓扁而不是捲動，筆數少的時候完全看不出來）。 */
.tk-head{display:flex; align-items:flex-end; justify-content:space-between; gap:16px;
  margin-bottom:10px; flex-wrap:wrap}
.tk-title{min-width:0}
.tk-title .h{font-size:21px; font-weight:700; letter-spacing:.3px; line-height:1.15}
.tk-title .s{font-size:11.5px; color:var(--faint); font-family:var(--font-mono);
  font-variant-numeric:tabular-nums; margin-top:5px; display:flex; gap:8px;
  align-items:center; flex-wrap:wrap; line-height:1.3}
.tk-title .s .sep{color:var(--ghost)}
.tk-title .s .warn{color:var(--gold)}
.tk-tools{display:flex; align-items:center; gap:9px; flex-wrap:wrap; margin-bottom:9px}
/* 這裡的 .seg 不吃滿寬（頂列那個分頁 seg 才要），按鈕也不要 flex:1 平分 */
.tk-tools .seg{flex:none; width:auto}
.tk-tools .seg button{flex:none; min-width:0; padding:5px 11px; font-size:12px;
  white-space:nowrap}
.tk-tools .lab{font-size:10.5px; color:var(--faint); letter-spacing:.6px}
.tk-tools .gap{width:1px; height:20px; background:var(--line-soft)}
.tk-chip{border:1px solid var(--line); background:var(--surface-2); color:var(--dim);
  border-radius:999px; padding:4px 11px 5px; font-size:11.5px; cursor:pointer;
  line-height:1.3; font-family:var(--font-sans); white-space:nowrap;
  transition:color .12s var(--ease), border-color .12s var(--ease)}
.tk-chip:hover:not(:disabled){color:var(--text); border-color:var(--gold-line)}
.tk-chip.on{background:var(--gold-soft); color:var(--gold); border-color:var(--gold-line)}
.tk-chip:disabled{color:var(--ghost); cursor:default; border-color:var(--line-soft);
  background:transparent}
.tk-chip:focus-visible{outline:1px solid var(--gold); outline-offset:1px}
/* 讀值列**固定高度**：沒有 hover 時整列消失的話，底下整張圖會跳一格 */
.tk-read{height:27px; min-height:27px; flex:none; display:flex; align-items:center;
  gap:9px; font-family:var(--font-mono); font-variant-numeric:tabular-nums;
  font-size:11.5px; color:var(--dim); overflow:hidden; white-space:nowrap;
  margin-bottom:6px}
.tk-read .lt{color:var(--faint)}
.tk-read b{color:var(--text); font-weight:650}
.tk-read .sep{color:var(--ghost)}
.tk-read .up{color:var(--up)} .tk-read .down{color:var(--down)}
.tk-read .warn{color:var(--gold)}
/* aspect-ratio 撐高度 ⇒ 骨架與真圖的高度**結構上就一樣**，不必靠量測對齊
   ⚠️ 2026-09-08 Benson：「早盤細節的 K 圖區也可以幫我縮小嗎？現在感覺有點大」
      ⇒ 1040/470 → **1040/380**（高度 −19%，跟【程式下單】那張同一個比例，
        他看過那張說剛好）。1500px 視窗實測 canvas 631 → **510px**。
   ⛔ 這張圖的下緣有兩樣東西，縮之前量過（探針 ⑪b 把新值斷言死了）：
      ① 成交量疊圖佔繪圖區 **22%**（TKVOLH）—— 是**比例**不是固定像素，
         所以它跟著等比縮：130.5 → **103.8px**（即時分頁那張同視窗下是 115px，同一個量級）。
      ② 時間軸 TKBOT=26px 是**固定像素**，不吃這一刀；而標籤密度 `maxLab=floor(PW/110)`
         只跟**寬度**有關 ⇒ 縮高度**不會**讓 HH:MM:SS 擠在一起（實測相鄰標籤邊緣間距
         縮前縮後都是 60.7px，一模一樣）。⛔ 別再拿「怕標籤重疊」當不敢縮的理由。
      ③ 取樣金籤掛在**價格區底緣**（TKTOP+priceH-22、tkChip 高 17）⇒ 跟著上移，
         餘裕不變（籤 462~479、價格區 12~484 ⇒ 底下還有 5px）。
   ⛔ 不可以改成縮量柱佔比（22%）或縮 TKBOT 來換高度 —— 要縮就縮價格區。 */
.tk-wrap{position:relative; aspect-ratio:1040/380; border-radius:10px; overflow:hidden;
  background:var(--bg)}
.tk-wrap canvas{position:absolute; inset:0; width:100%; height:100%; display:block;
  cursor:crosshair; touch-action:none; user-select:none}
/* 骨架：假 K 棒一律**絕對定位** —— 當成一般 flex 子元素的話，它們的百分比高度會
   反過來把容器撐高，骨架比真圖高一截，換過去照樣跳（面板踩過 351→553）。 */
.tk-skel{position:absolute; inset:0; display:flex; align-items:flex-end;
  gap:4px; padding:22px 66px 30px 6px; pointer-events:none}
.tk-skel i{flex:1; border-radius:2px;
  background:linear-gradient(180deg,var(--surface-2),var(--line-soft));
  animation:tkpulse 1.4s var(--ease) infinite}
@keyframes tkpulse{0%,100%{opacity:.45} 50%{opacity:.8}}
.tk-empty{position:absolute; inset:0; display:flex; flex-direction:column;
  align-items:center; justify-content:center; gap:9px; text-align:center; padding:0 32px}
.tk-empty .t{font-size:14px; color:var(--text); font-weight:600; line-height:1.6}
.tk-empty .d{font-size:11.5px; color:var(--faint); line-height:1.6}
.tk-empty .go{margin-top:4px}
/* 「回到最新」：使用者拖走之後才出現。⛔ 不可以直接把他拉回去 —— 他可能正在看某一段。 */
.tk-back{position:absolute; right:74px; top:10px; background:var(--surface-2);
  border:1px solid var(--gold-line); color:var(--gold); border-radius:999px;
  font-size:11px; padding:3px 10px 4px; cursor:pointer; font-family:var(--font-sans)}
.tk-back:hover{background:var(--gold-soft)}
.tk-foot{font-size:11px; color:var(--faint); margin-top:7px; display:flex; gap:8px;
  flex-wrap:wrap; font-family:var(--font-mono); font-variant-numeric:tabular-nums}
.tk-foot .sep{color:var(--ghost)}
/* 日期清單（不是迷你月曆）：這裡的母體是「有逐筆檔的日子」，上線第一週只有 0~3 天，
   一個 95% 格子都是灰的月曆傳達的是「壞了」而不是「沒有資料」。
   清單每一列寫得出這一頁真正在意的東西：筆數與缺口 —— 全站沒有第二個地方看得到。 */
/* ⚠️ 有 max-height 的 flex 直欄，子元素一律 flex:none（.row 與 .foot 兩個都要）——
   預設 flex-shrink:1 時，筆數超過高度不是捲動而是把每一列壓扁，
   而且**筆數少的時候完全看不出來**（面板鐵律，2026-08-25 實測 107px 被壓成 21.6px）。 */
.tk-list{border:1px solid var(--line); border-radius:14px; padding:8px;
  background:var(--surface-2); box-shadow:var(--shadow-2); width:max-content;
  max-width:min(520px,86vw); max-height:320px; overflow:auto;
  display:flex; flex-direction:column}
.tk-list .row{display:flex; align-items:center; gap:12px; width:100%; text-align:left;
  border:0; background:transparent; color:var(--text); cursor:pointer; padding:7px 9px;
  border-radius:9px; font-family:var(--font-mono); font-variant-numeric:tabular-nums;
  font-size:12px; line-height:1.3; flex:none}
.tk-list .row:hover:not(:disabled){background:var(--raise)}
.tk-list .row.on{background:var(--gold-soft); color:var(--gold)}
.tk-list .row:disabled{color:var(--ghost); cursor:default}
.tk-list .row .dd{min-width:64px; font-weight:650}
.tk-list .row .wd{font-family:var(--font-sans); color:var(--dim); min-width:1.2em}
/* ⚠️ nowrap：meta 換行的話那一列會比別列高一截（.row 是 flex，撐高看得很清楚）。
   清單本身是 width:max-content ＋ max-width，太長就整塊橫向捲，不會再影響列高。 */
.tk-list .row .meta{margin-left:auto; color:var(--faint); font-size:11px; white-space:nowrap}
.tk-list .row .meta .warn{color:var(--gold)}
/* 種類標記：⛔ 逐筆與取樣**兩種都標**（只標一種的話，沒有標記等於「不知道」）。
   ⛔ 一個新顏色都不准加 —— 逐筆走中性線框、取樣走既有的 gold token（＝要注意）。 */
.tk-list .row .kind{flex:none; min-width:34px; text-align:center; font-size:10px;
  font-family:var(--font-sans); line-height:1.5; padding:1px 6px 2px; border-radius:999px;
  border:1px solid var(--line); color:var(--dim); background:var(--surface-2)}
.tk-list .row .kind.polled{border-color:var(--gold-line); color:var(--gold);
  background:var(--gold-soft)}
.tk-list .row .meta .alt{color:var(--ghost); margin-left:6px}
.tk-list .foot{border-top:1px solid var(--line-soft); margin-top:6px; padding:8px 9px 3px;
  font-size:11px; color:var(--faint); line-height:1.5; flex:none}
@media(max-width:1024px){
  .tk-tools{gap:7px}
  .tk-title .h{font-size:19px}
}

/* ══ 【程式下單】分頁：四種方向判斷的模擬對照 ═══════════════════════════
   ⛔ class 一律 `at-` 前綴 —— `tk-`（細節）／`dt-`（回顧當天資料）／`n-`（真實區）
      都已經被佔走，撞了會互相污染。
   ⛔ 一個新顏色都不准加：全部用 :root 既有的 token。
   ⛔ **顏色只給損益**（比別頁嚴格）：方向、訊號值、筆數、門檻、天數、天花板一律中性色。
   ⚠️ 有 max-height 的 flex 直欄，子元素一律 flex:none（面板鐵律，2026-08-25 踩過）。 */

/* ⛔ 進度尺（.at-track）已於 2026-09-08 依 Benson 指示整條拿掉（原話：
   「程式下單那邊這個欄位不需要」）—— ⛔ 不要再加回來。
   守衛：autotest-tab.mjs ⑤ 斷言 #attrack 不存在、而且「這個測試跑到哪裡了」／
   「/ 505 筆」／「要到這裡才算數」在 DOM ＋ 兩張 canvas 上零命中。
   ⚠️ 後端 total_n／track_n 照端（天花板那一行還在用 track_n 算 title），只是不畫進度尺。 */

.at-head{display:flex; align-items:flex-end; justify-content:space-between; gap:16px;
  margin-bottom:10px; flex-wrap:wrap}
.at-title{min-width:0}
.at-title .h{font-size:21px; font-weight:700; letter-spacing:.3px; line-height:1.15;
  display:flex; align-items:center; gap:10px; flex-wrap:wrap}
.at-title .h .dt{font-size:14px; font-weight:600; color:var(--dim);
  font-family:var(--font-mono); font-variant-numeric:tabular-nums}
.at-title .s{font-size:11.5px; color:var(--faint); font-family:var(--font-mono);
  font-variant-numeric:tabular-nums; margin-top:5px; display:flex; gap:8px;
  align-items:center; flex-wrap:wrap; line-height:1.3}
.at-title .s .sep{color:var(--ghost)}
.at-title .s .warn{color:var(--gold)}
/* ⛔ 恰好 1 顆、不可關閉。探針會真的數畫面上有幾顆（不是掃原始碼）。 */
.simlock{flex:none; font-size:10.5px; font-weight:650; font-family:var(--font-sans);
  letter-spacing:.3px; color:var(--gold); background:var(--gold-soft);
  border:1px solid var(--gold-line); border-radius:999px; padding:2px 10px 3px;
  white-space:nowrap; line-height:1.5}
.at-tools{display:flex; align-items:center; gap:9px; flex-wrap:wrap; margin-bottom:9px}
.at-tools .lab{font-size:10.5px; color:var(--faint); letter-spacing:.6px}
.at-chip{border:1px solid var(--line); background:var(--surface-2); color:var(--dim);
  border-radius:999px; padding:4px 11px 5px; font-size:11.5px; cursor:pointer;
  line-height:1.3; font-family:var(--font-sans); white-space:nowrap;
  transition:color .12s var(--ease), border-color .12s var(--ease)}
.at-chip:hover:not(:disabled){color:var(--text); border-color:var(--gold-line)}
.at-chip.on{background:var(--gold-soft); color:var(--gold); border-color:var(--gold-line)}
.at-chip:focus-visible{outline:1px solid var(--gold); outline-offset:1px}
/* aspect-ratio 撐高度 ⇒ 骨架與真圖的高度**結構上就一樣**，不必靠量測對齊
   ⚠️ 2026-09-08 Benson：「這個線的面板可以改的小一點點嗎，我覺得他現在有一點點大」
      ⇒ 1040/470 → **1040/380**（高度 −19%）。
   ⛔ 只有**價格區**變矮：四條泳道是從下緣往上排的固定像素（4×22 ＋ ATBOT 26），
      H 變小不會壓到它們，名字欄寬 L 也是 measureText 量出來的 ——
      ⛔ 不可以改成縮泳道列高或縮名字字級來換高度（規格 §9.3，被退件過）。 */
.at-wrap{position:relative; aspect-ratio:1040/380; border-radius:10px; overflow:hidden;
  background:var(--bg)}
.at-wrap canvas{position:absolute; inset:0; width:100%; height:100%; display:block}
.at-skel{position:absolute; inset:0; display:flex; align-items:flex-end; gap:4px;
  padding:22px 66px 30px 6px; pointer-events:none}
.at-skel i{flex:1; border-radius:2px;
  background:linear-gradient(180deg,var(--surface-2),var(--line-soft));
  animation:tkpulse 1.4s var(--ease) infinite}
.at-blank{position:absolute; inset:0; display:flex; flex-direction:column;
  align-items:center; justify-content:center; gap:8px; text-align:center; padding:0 32px}
.at-blank .t{font-size:14px; color:var(--text); font-weight:600; line-height:1.6}
.at-blank .d{font-size:11.5px; color:var(--faint); line-height:1.6}

/* 四格讀數（＝泳道的圖例）。⛔ 方向用中性色，紅綠只留給點數。 */
.at-today{display:grid; grid-template-columns:repeat(4,1fr); gap:1px;
  background:var(--line-soft); border-radius:var(--r-md); overflow:hidden; margin-top:11px}
.at-today .c{background:var(--surface); padding:9px 12px 10px; min-width:0}
.at-today .k{font-size:10.5px; color:var(--faint); letter-spacing:.4px;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
.at-today .k b{color:var(--dim); font-weight:700; margin-right:5px}
.at-today .dir{font-size:17px; font-weight:700; color:var(--text); margin-top:2px;
  line-height:1.3}
.at-today .dir.off{color:var(--faint); font-size:14px}
.at-today .sg,.at-today .rs{font-size:11.5px; color:var(--dim);
  font-family:var(--font-mono); font-variant-numeric:tabular-nums; line-height:1.5;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
.at-today .rs{border-top:1px solid var(--line-soft); margin-top:6px; padding-top:6px}
.at-today .rs .up{color:var(--up)} .at-today .rs .down{color:var(--down)}
.at-today .wait{grid-column:1/-1; background:var(--surface); padding:16px 14px;
  text-align:center}
.at-today .wait .t{font-size:14px; color:var(--text); font-weight:650}
.at-today .wait .d{font-size:11.5px; color:var(--faint); margin-top:5px}

/* 成績表。⛔ 窄視窗要整張橫向捲，**不可以讓欄位被壓扁** ——
   面板為「容器變窄 ⇒ 價格欄被截掉」踩過兩天（2026-09-03，實測 12/12 全截）。 */
.at-score .at-seg{margin-bottom:10px}
/* ⚠️ 窗口去重後只剩一個時整條不畫（沿用真實區 realWindows() 的決定）——
   空的 .seg 會留下一條 32px 的灰帶，看起來像壞掉的按鈕列。 */
.at-seg:empty{display:none}
.at-tblwrap{overflow-x:auto}
.at-tbl{width:100%; min-width:640px; border-collapse:collapse;
  font-family:var(--font-mono); font-variant-numeric:tabular-nums}
.at-tbl th{font-size:10.5px; color:var(--faint); font-weight:600; text-align:right;
  padding:0 8px 7px; letter-spacing:.4px; white-space:nowrap;
  font-family:var(--font-sans); border-bottom:1px solid var(--line-soft)}
.at-tbl th:first-child,.at-tbl td:first-child{text-align:left; padding-left:2px}
.at-tbl th:last-child,.at-tbl td:last-child{text-align:left; padding-right:2px}
.at-tbl td{font-size:12.5px; color:var(--dim); text-align:right; padding:8px;
  white-space:nowrap; border-bottom:1px solid var(--line-soft)}
.at-tbl tr:last-child td{border-bottom:0}
.at-tbl .nm{color:var(--text); font-family:var(--font-sans); font-weight:650}
.at-tbl .nm i{display:block; font-style:normal; font-size:10.5px; color:var(--faint);
  font-weight:400; margin-top:1px}
/* 主角是累計點數（大字＋紅綠）；勝率是中性色小字。
   ⛔ 這是面板既有的設計決定（見 .score 的註解），不要改回把勝率捧到視覺頂端。 */
.at-tbl .pts{font-size:16px; font-weight:700; color:var(--text)}
.at-tbl .pts.up{color:var(--up)} .at-tbl .pts.down{color:var(--down)}
.at-tbl .avg.up{color:var(--up)} .at-tbl .avg.down{color:var(--down)}
.at-tbl .rate{color:var(--dim)}
.at-tbl .rate.na{color:var(--faint); font-size:11px; font-family:var(--font-sans)}
.at-tbl .sub{color:var(--faint); font-size:11px}
.at-tbl .tag{display:inline-block; font-family:var(--font-sans); font-size:10.5px;
  border-radius:999px; padding:2px 9px 3px; border:1px solid var(--line);
  color:var(--dim); background:var(--surface-2); white-space:nowrap}
.at-tbl .tag.over{border-color:var(--gold-line); color:var(--gold);
  background:var(--gold-soft)}
/* 他自己那兩列跟四條算法的口徑不同 ⇒ **一條虛線隔開**，不是同一批東西 */
/* ⛔ 「你自己」那兩列（tr.mine）與上面那條分隔（tr.sep）已於 2026-09-08 依 Benson
   指示從畫面上拿掉（原話：「然後我自己的這邊都拿掉」）—— ⛔ 不要再加回來。
   ⚠️ 後端 _auto_mine_day／_auto_mine_rows **刻意留著**（他之後可能會想加回來），
      連同「只讀不寫」的守衛（autotest-backend.py ⑧c）一起留 —— 只是前端不畫。 */
.at-ceil{margin-top:11px; font-size:11.5px; color:var(--dim); line-height:1.6;
  display:flex; align-items:baseline; gap:7px; flex-wrap:wrap}
.at-ceil .dot{color:var(--faint)}
.at-ceil b{color:var(--text); font-family:var(--font-mono); font-weight:650}
.at-ceil .hit{color:var(--gold)}
.at-notes{margin-top:7px; font-size:11px; color:var(--faint); line-height:1.7;
  display:flex; gap:6px; flex-wrap:wrap; font-family:var(--font-mono);
  font-variant-numeric:tabular-nums}
.at-notes .sep{color:var(--ghost)}
.at-notes .warn{color:var(--gold)}
/* ⚠️ 2026-09-08 起 .at-notes **只在有異常的時候才有東西**（帳本那一行拿掉了）⇒
   沒異常時整條要真的不見，不然會留一條 7px 的空隙看起來像壞掉。 */
.at-notes:empty{display:none}

.at-cwrap{position:relative; aspect-ratio:1040/300; border-radius:10px;
  background:var(--bg); overflow:hidden}
.at-cwrap canvas{position:absolute; inset:0; width:100%; height:100%; display:block}
.at-empty{position:absolute; inset:0; display:flex; flex-direction:column;
  align-items:center; justify-content:center; gap:6px; text-align:center; padding:0 32px}
.at-empty .t{font-size:13.5px; color:var(--text); font-weight:600}
.at-empty .d{font-size:11.5px; color:var(--faint)}

/* 摺疊區：⛔ 三個都預設關著。v1 把這些東西平鋪在畫面上，退件原因就是它們。 */
.at-fold{background:var(--surface); border:1px solid var(--line-soft);
  border-radius:var(--r-md); margin-bottom:10px}
.at-fold>summary{cursor:pointer; padding:11px 16px; font-size:12px; color:var(--dim);
  letter-spacing:.5px; list-style:none; user-select:none}
.at-fold>summary::-webkit-details-marker{display:none}
.at-fold>summary::before{content:'▸'; color:var(--faint); margin-right:8px;
  display:inline-block; transition:transform .15s var(--ease)}
.at-fold[open]>summary::before{transform:rotate(90deg)}
.at-fold>summary:hover{color:var(--text)}
.at-fold>div,.at-fold>ol{padding:2px 16px 14px}
.at-list{margin:0; padding:2px 16px 14px 34px}
.at-list li{font-size:11.5px; color:var(--dim); line-height:1.75; margin-bottom:3px}
.at-pairs{display:grid; grid-template-columns:repeat(3,1fr); gap:10px}
.at-pc{background:var(--surface-2); border:1px solid var(--line-soft);
  border-radius:var(--r-sm); padding:10px 12px 11px}
.at-pc .h{font-size:12px; color:var(--text); font-weight:650}
.at-pc .m{font-size:11px; color:var(--faint); margin-top:2px; font-family:var(--font-mono)}
.at-pc .v{font-size:12.5px; color:var(--dim); margin-top:5px; font-family:var(--font-mono);
  font-variant-numeric:tabular-nums}
.at-pc .v .up{color:var(--up)} .at-pc .v .down{color:var(--down)}
/* 圓點列是這一區的靈魂：十個點就是十個點，他一眼看得出可以拿來比的東西有多少 */
.at-dots{display:flex; gap:4px; flex-wrap:wrap; margin:8px 0 6px; min-height:9px}
.at-dots i{width:8px; height:8px; border-radius:50%; background:var(--ghost)}
.at-dots i.w{background:var(--up)} .at-dots i.l{background:var(--down)}
.at-pc .need{font-size:11px; color:var(--faint); font-family:var(--font-mono)}
.at-scan{width:100%; border-collapse:collapse; font-family:var(--font-mono);
  font-variant-numeric:tabular-nums}
.at-scan th{font-size:10.5px; color:var(--faint); font-weight:600; text-align:right;
  padding:0 8px 6px; font-family:var(--font-sans);
  border-bottom:1px solid var(--line-soft)}
.at-scan td{font-size:12px; color:var(--dim); text-align:right; padding:6px 8px;
  border-bottom:1px solid var(--line-soft)}
.at-scan th:first-child,.at-scan td:first-child{text-align:left}
.at-scan tr.cur td{color:var(--text)}
.at-scan .up{color:var(--up)} .at-scan .down{color:var(--down)}
.at-advrow{display:flex; align-items:center; gap:11px; margin:10px 0 4px;
  font-size:11.5px; color:var(--dim); flex-wrap:wrap}
.at-advrow input[type=range]{flex:1; min-width:180px; accent-color:var(--gold)}
.at-advrow b{font-family:var(--font-mono); color:var(--text); min-width:56px}
.at-fold .note{font-size:11px; color:var(--faint); margin-top:8px; line-height:1.6}

/* ══════════ 【自動下單】（會真的送單的那一頁）══════════
   版面沿用【自動下單（模擬）】那一套（.card / .sec-head / .at-title / .at-tbl），
   ⛔ 差別只有三件事，而且每一件都要**一眼看得到**：
     ① 現在是開還是關　② 選了哪個做法　③ 今天送了沒／為什麼沒送
   ⛔ 這一頁沒有任何 button／form／input —— 開關只有一條路：他自己建 AUTO_ORDERS_ON。 */
.al-state{display:flex; align-items:center; gap:12px; flex-wrap:wrap}
/* 關著＝中性灰（⛔ 不是紅色：關著是**正常**狀態，跟休市的連線燈同一個道理）；
   開著＝金色（這個面板既有的「注意，這是什麼種類」的意思）。
   ⛔ 絕對不准用紅綠 —— 紅綠在這個面板只給損益。 */
.al-badge{flex:none; font-size:12px; font-weight:700; font-family:var(--font-sans);
  letter-spacing:.3px; border-radius:999px; padding:4px 13px 5px; white-space:nowrap;
  line-height:1.5; color:var(--faint); background:var(--surface-2);
  border:1px solid var(--line)}
.al-badge.on{color:var(--gold); background:var(--gold-soft); border-color:var(--gold-line)}
.al-way{font-size:19px; font-weight:700; color:var(--text); line-height:1.2}
.al-way i{display:block; font-style:normal; font-size:11.5px; font-weight:500;
  color:var(--faint); font-family:var(--font-mono); margin-top:3px}
.al-way.off{font-size:15px; color:var(--dim); font-weight:600}
.al-how{margin-top:11px; font-size:11.5px; color:var(--dim); line-height:1.75}
.al-how code{font-family:var(--font-mono); color:var(--gold); font-size:11.5px}
/* 可以直接貼的指令：⛔ 一定要能整行看完（斷行不斷字），不然他會複製到半截。 */
.al-how code.cmd{display:inline-block; margin-top:2px; padding:3px 8px 4px;
  background:var(--surface-2); border:1px solid var(--line); border-radius:6px;
  color:var(--text); word-break:break-all; white-space:normal; line-height:1.6}
.al-how .warn{color:var(--gold)}
.al-how b{color:var(--text); font-weight:650}
.al-gates{display:grid; grid-template-columns:repeat(3,1fr); gap:1px;
  background:var(--line-soft); border-radius:var(--r-md); overflow:hidden; margin-top:11px}
.al-gates .c{background:var(--surface); padding:9px 12px 10px; min-width:0}
.al-gates .k{font-size:10.5px; color:var(--faint); letter-spacing:.4px;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
.al-gates .v{font-size:14px; font-weight:650; color:var(--text); margin-top:3px;
  line-height:1.3; white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
.al-gates .v.off{color:var(--faint); font-weight:600}
.al-today{margin-top:2px}
.al-today .t{font-size:17px; font-weight:700; color:var(--text); line-height:1.35}
.al-today .t.off{font-size:15px; color:var(--dim); font-weight:650}
.al-today .d{font-size:12px; color:var(--faint); line-height:1.7; margin-top:5px}
.al-today .d b{color:var(--dim); font-weight:650; font-family:var(--font-mono)}
/* 紀錄清單。⛔ 沿用 .at-tbl（同一個面板裡「一排結果」長得都一樣）。 */
.al-tbl td .why{display:block; font-size:11px; color:var(--faint);
  font-family:var(--font-sans); margin-top:2px; white-space:normal}
.al-tbl .ok{color:var(--gold)}
.al-tbl .no{color:var(--faint)}
.al-empty{font-size:12px; color:var(--faint); line-height:1.8; padding:10px 2px}
/* 關閉鈕。⛔ 這是這一頁唯一一顆按鈕，而且只有開關檔存在時才畫得出來。
   ⛔ 不用紅色（紅綠只給損益），用跟「手動平倉」同一顆 .btn.flat2 的語彙 ——
      兩者都是「安全方向的收手動作」。 */
.al-off{margin-top:13px; display:flex; align-items:center; gap:12px; flex-wrap:wrap}
.al-off .btn{flex:none}
.al-off .n{font-size:11.5px; color:var(--faint); line-height:1.6}
.al-off .n b{color:var(--gold); font-weight:650}
/* ⭐ 打開自動下單（2026-09-09 加）。⛔⛔ 這是這個面板上**唯一一顆會武裝真錢**的鈕，
   所以視覺規矩跟別處不一樣，⛔ 不要「順手統一」掉：
     ・第一段（兩顆做法鈕）＝金色框線的次要鈕：看得出來是「要動手了」，
       但**還不是**那個決定（真正的決定在第二段）。
     ・第二段（確認條）＝**真錢模式用紅底**。這個面板的鐵律是「紅綠只給損益」，
       這裡是**唯一的例外**，而且是 Benson 2026-09-09 指定的：
       那一刻要講的是「會用你的錢」，那不是狀態、是警告。
     ・演練模式用中性灰 —— 同一條，⛔ 兩種模式不可以長一樣（長一樣＝那句警告失效）。 */
.al-on{margin-top:13px}
.al-on .row{display:flex; align-items:center; gap:10px; flex-wrap:wrap}
.al-on .btn{flex:none; padding:11px 18px; font-size:14px;
  background:var(--gold-soft); color:var(--gold); border-color:var(--gold-line)}
.al-on .btn:hover:not(:disabled){background:rgba(227,169,81,.24)}
.al-on .n{font-size:11.5px; color:var(--faint); line-height:1.7; margin-top:8px}
.al-on .n b{color:var(--dim); font-weight:650}
.al-conf{padding:12px 14px 13px; border-radius:var(--r-md);
  border:1px solid var(--line); background:var(--surface-2)}
.al-conf.real{border-color:var(--up-line); background:var(--up-soft)}
.al-conf .q{font-size:13px; line-height:1.75; font-weight:650; color:var(--dim)}
.al-conf.real .q{color:var(--up)}
.al-conf .q b{font-weight:750}
.al-conf .btns2{display:flex; gap:10px; margin-top:11px; flex-wrap:wrap}
.al-conf .btn{flex:none; padding:11px 18px; font-size:14px}
.al-conf .btn.go{background:var(--surface-2); color:var(--text); border-color:var(--line)}
.al-conf.real .btn.go{background:var(--up-soft); color:var(--up);
  border-color:var(--up-line)}
.al-conf .btn.no{background:transparent; color:var(--faint); border-color:var(--line)}
.al-conf .btn.no:hover:not(:disabled){color:var(--text); border-color:var(--faint)}
.al-on .err{font-size:12px; color:var(--gold); line-height:1.7; margin-top:9px;
  font-weight:600}
/* 收盤平倉要他自己動手的那幾種：⛔ 不可以混在一般紀錄裡看不出來。 */
.al-alarm{margin-top:11px; padding:9px 12px 10px; border-radius:var(--r-md);
  background:var(--gold-soft); border:1px solid var(--gold-line);
  color:var(--gold); font-size:12px; line-height:1.7; font-weight:600}
@media(max-width:720px){
  .al-gates{grid-template-columns:1fr}
}
@media(max-width:1024px){
  .at-title .h{font-size:19px}
  .at-pairs{grid-template-columns:1fr}
}
@media(max-width:720px){
  .at-today{grid-template-columns:repeat(2,1fr)}
  /* 泳道切成堆疊排法（名字自己一列）之後多吃 32px，**圖要跟著變高** ——
     ⛔ 不可以改成壓縮泳道，也不可以縮寫名字（規格 §9.3）。 */
  .at-wrap{aspect-ratio:520/620}
}
</style></head><body><div class="app">
<div class="topbar">
  <div class="brand"><div class="mark">&#9702;</div>
    <div><div class="nm">早盤儀表板</div><div class="sub" id="sub">連線中…</div></div></div>
  <div class="tabs">
    <button data-tab="live" class="on">即時</button>
    <!-- 【細節】放中間：時間上它介於「現在」與「回頭看」之間，而且跟即時共用同一天。
         分頁名只有兩個字（跟左右等寬）—— 分頁列是導航不是說明，
         「早盤細節」四個字塞進來會讓這一格比左右寬約 30px，語意交給卡片標題補。 -->
    <button data-tab="tick">細節</button>
    <button data-tab="review">回顧</button>
    <!-- 【自動下單（模擬）】放右邊倒數第二：前三顆是「現在 → 更細 → 回頭看自己」的
         時間動線，這一頁是研究性質，接在後面。⛔ 不可以插在中間。
         ⚠️ 2026-09-09 從「程式下單」改名成「自動下單（模擬）」（Benson 指示）——
            隔壁多了一顆會真的送單的【自動下單】，兩顆的名字必須一眼分得出來，
            **括號裡那兩個字是唯一的差別，⛔ 不准拿掉**。 -->
    <button data-tab="auto">自動下單（模擬）</button>
    <!-- ⛔⛔ 【自動下單】＝**會真的送出委託單**的那一頁，所以放最右（動線的終點）。
         這一頁本身**沒有任何開關**：要用只能自己在硬碟上建 tools/shioaji/AUTO_ORDERS_ON，
         而且照樣受 REAL_ORDERS_ON 管。⛔ 不准在這裡加按鈕。 -->
    <button data-tab="fire">自動下單</button>
  </div>
  <div class="clock"><div class="d" id="clk">--:--</div><div class="w" id="ph"></div></div>
</div>

<div id="tab-live">
  <div id="warn"></div>
  <div class="cols"><div id="mkt">
    <!-- K 棒要 0.3~0.5 秒才回得來。骨架寫死在 HTML 裡，第 0 毫秒就佔好位置，
         尺寸與真圖完全相同 —— 真圖進來時只是淡入，版面一格都不會跳。 -->
    <div class="card chart l1 skel">
      <div class="chead"><span class="sk sk-px"></span><span class="sk sk-day"></span></div>
      <div class="legend"><span class="sk" style="width:88px"></span>
        <span class="sk" style="width:70px"></span><span class="sk" style="width:70px"></span>
        <span class="sk" style="width:70px"></span><span class="sk" style="width:56px"></span></div>
      <div class="cwrap"><div class="bars"><i style="height:38%"></i><i style="height:52%"></i>
        <i style="height:44%"></i><i style="height:61%"></i><i style="height:55%"></i>
        <i style="height:70%"></i><i style="height:64%"></i><i style="height:48%"></i>
        <i style="height:57%"></i><i style="height:72%"></i><i style="height:66%"></i>
        <i style="height:80%"></i><i style="height:74%"></i><i style="height:59%"></i>
        <i style="height:68%"></i><i style="height:52%"></i><i style="height:63%"></i>
        <i style="height:47%"></i></div></div>
      <div class="chint"><span class="sk"></span></div>
      <div class="rail"><span class="sk"></span><span class="sk"></span><span class="sk"></span>
        <span class="sk"></span><span class="sk"></span><span class="sk"></span></div>
    </div>
  </div><div class="right">
    <!-- 右欄拆成三個各自比對自己字串的節點：
         #xal  跨分頁警報（兩個分頁都看得到，沒警報時 :empty 完全不佔位）
         #ntabs 頁籤（浮動點數是裡面的獨立節點 #tabbadge）
         #zone  練習／真實其中一區（切分頁時才重建骨架） -->
    <div id="xal"></div><div id="ntabs"></div><div id="zone"></div>
  </div></div>
</div>

<!-- 【細節】：08:45~09:30 的逐秒圖。容器只建這一次，之後只換 canvas 內容與幾個文字節點
     （整塊 innerHTML 重繪會打斷使用者的縮放／拖曳並閃爍）。
     ⚠️ #tkpager 是**獨立節點**，不可以跟會重繪的東西寫在同一串 innerHTML ——
        即時分頁踩過 #cupd 那個坑：報價每秒跳，翻頁列跟著被重建，◀ 會在滑鼠底下被換掉。 -->
<div id="tab-tick" hidden>
 <div class="card chart l1">
  <div class="cheadwrap">
   <div class="tk-head">
    <div class="tk-title">
     <div class="h">早盤細節</div>
     <div class="s" id="tksub"></div>
    </div>
    <div id="tkpager"></div>
   </div>
   <div class="calpop" id="tkpick"></div>
  </div>
  <div class="tk-tools" id="tktools"></div>
  <div class="tk-read" id="tkread"></div>
  <div class="tk-wrap" id="tkwrap">
   <canvas id="tkcv"></canvas>
   <div id="tkover"></div>
  </div>
  <div class="tk-foot" id="tkfoot"></div>
 </div>
</div>

<!-- 【程式下單】：四種方向判斷的模擬對照。
     ⛔⛔ 這一頁**永遠只是模擬，一張單都不會送出去**，也不是自動下單的前置作業。
         裡面沒有任何 [data-act] / [data-rdir]（那兩個才是會送單的按鈕）。
     ⭐ 第一屏只有兩樣東西：**圖＋四條泳道 ／ 成績表**（PM 2026-09-07 裁示三樣，
        2026-09-08 Benson 把進度尺拿掉之後剩兩樣）。
        v1 那一牆解釋文字被 Benson 退件（原話：「聽不懂，然後我覺得面板一大堆多餘的文字」）
        ⇒ 2026-09-08 他連摺疊的〈這一頁在算什麼〉都不要了（原話：「然後這一頁在算甚麼
        也拿掉，我不需要看這個我也不會看」）⇒ **整個摺疊區已刪掉**。
        ⛔ 不准把那十條（或任何解釋牆）搬回這一頁 —— 那正是 v1 退件的原因。
     ⛔ 這一頁已經拿掉四樣東西，⛔ **一樣都不准加回來**（守衛：autotest-tab.mjs ⑤／㉔）：
        ① 進度尺（#attrack）② 成績表的「你自己」兩列 ③ 帳本那一行
        ④〈這一頁在算什麼〉摺疊區。 -->
<!-- ══════════ 【自動下單】：會真的送出委託單的那一頁 ══════════
     ⛔⛔ **開難、關易**（Benson 2026-09-09 拍板；同日下午他要求「開」也做到畫面上）：
       ・**開**＝ #alon 那兩顆做法鈕 → **第二段確認條** → 「確定，打開」
         → POST /api/fire/on（六道防護，見 live_panel.fire_post_guard）。
         ⛔ **兩段式不可以拿掉**：這是這個面板上唯一一顆會武裝真錢的鈕。
         ⛔ 確認條那句話（現在是真錢還是演練）**一律從後端拿**（arm_confirm），
            ⛔ 前端不准自己猜 —— 講錯的代價是「他以為只是演練，結果真的送了一口」。
         ⛔ 第一段沒按「確定」之前**一個請求都不准出去**（fire-tab.mjs ⑪ 在守）。
       ・**關**有一顆按鈕（#aloff → POST /api/fire/off）。關掉永遠是安全的動作，
         所以**不跳確認**（⛔ 開跳、關不跳，這個不對稱是刻意的）。
       ・⛔ 開著的時候**只有**「關閉」那一顆（⛔ 沒有「換做法」——
         換做法牽涉到「今天已經進場了怎麼辦」，這一輪不做；再按一次開的話後端回 409）。
         守衛：fire-tab.mjs ②／⑧c／⑩／⑪。
     ⛔ 這一段刻意放在 #tab-auto **之前**：autotest-backend.py ① 掃的是
        `<div id="tab-auto">` 到 `<!-- 【回顧】` 之間那一段（模擬那一頁的紅線
        「一行都不碰下單路徑」），放進去會讓那把尺量到不該量的東西。 -->
<div id="tab-fire" hidden>

 <div class="card l1">
  <div class="at-head">
   <div class="at-title">
    <div class="h al-state" id="alstate"></div>
    <div class="s" id="alsub"></div>
   </div>
  </div>
  <div class="al-gates" id="algates"></div>
  <div class="al-how" id="alhow"></div>
  <!-- ⭐ 打開（兩段式）。⛔ 只有開關**關著**時才會有東西畫進來（alPaint）：
       開著的時候這裡是空的（要換做法請先關掉），關著的時候 #aloff 是空的。
       ⛔ 兩個永遠不會同時有東西 —— 「開」與「關」同時在畫面上會讓他按錯。 -->
  <div class="al-on" id="alon"></div>
  <!-- 關閉鈕。⛔ 只有開關檔存在時才會有東西畫進來（alPaint），
       關著的時候這裡是空的 —— 沒東西可關就不該有按鈕。 -->
  <div class="al-off" id="aloff"></div>
 </div>

 <div class="card">
  <div class="sec-head"><h2>今天</h2><span class="count" id="alcount"></span></div>
  <div class="al-today" id="altoday"></div>
 </div>

 <div class="card">
  <div class="sec-head"><h2>紀錄</h2><span class="count" id="allogn"></span></div>
  <div class="at-tblwrap"><table class="at-tbl al-tbl" id="altbl"></table></div>
  <div class="al-empty" id="alempty"></div>
  <div class="at-notes" id="alnotes"></div>
 </div>
</div>

<div id="tab-auto" hidden>

 <div class="card chart l1">
  <div class="cheadwrap">
   <div class="at-head">
    <div class="at-title">
     <!-- 裝置一：「模擬 · 不送單」鎖印。**全頁恰好 1 顆**，掛在最容易被截圖的地方。
          ⛔ 不可以拿掉，也不可以變成兩顆（v1 三處是退件原因之一）。
          金色在這個面板既有的意思是「這是什麼種類的資料，注意」（＝【細節】的取樣金籤），
          沿用不是新增意思。⛔ 不可以用紅綠 —— 紅綠只給損益。 -->
     <div class="h">這一天 <span class="dt" id="atdt"></span>
       <span class="simlock" title="這一頁的四條算法全部是模擬，不會、也不能送出任何委託單">⚪ 模擬 · 不送單</span></div>
     <div class="s" id="atsub"></div>
    </div>
    <div id="atpager"></div>
   </div>
   <div class="calpop" id="atpick"></div>
  </div>
  <div class="at-tools" id="attools"></div>
  <div class="at-wrap" id="atwrap"><canvas id="atday"></canvas>
   <div id="atmsg"></div></div>
  <!-- 四格讀數＝泳道的圖例，也是「四種算法各記一筆，他要看結果和數字」那句話的落點。
       ⛔ 方向一律中性色（.dir）：這是全站唯一「程式說了什麼方向」的地方，
          染紅會讓它看起來像一個令人興奮的訊號。紅綠只給點數。 -->
  <div class="at-today" id="attoday"></div>
 </div>

 <div class="card at-score">
  <div class="sec-head"><h2>成績</h2><span class="count" id="atcount"></span></div>
  <div class="seg at-seg" id="atwin"></div>
  <div class="at-tblwrap"><table class="at-tbl" id="attbl"></table></div>
  <!-- ⛔ 同上：title 的數字是算出來寫進去的，⛔ 不要寫死。 -->
  <div class="at-ceil" id="atceil"></div>
  <div class="at-notes" id="atnotes"></div>
 </div>

 <div class="card at-cum">
  <div class="sec-head"><h2>累計點數</h2><span class="count" id="atcumn"></span></div>
  <div class="at-cwrap" id="atcwrap"><canvas id="atcum"></canvas>
   <div class="at-empty" id="atcumempty"></div></div>
 </div>

 <!-- 配對對照：PM 2026-09-07 裁示「收進摺疊區，不要刪掉、也不放第一屏」。 -->
 <details class="at-fold at-pair" id="atpair"><summary>結果不一樣的那幾天</summary>
  <div id="atpairbody"></div></details>

 <details class="at-fold at-adv" id="atadv"><summary>換一個門檻看看</summary>
  <div id="atadvbody"></div></details>

 <!-- ⛔⛔ 〈這一頁在算什麼〉那個摺疊區（十條）已於 2026-09-08 依 Benson 指示**整個刪掉**
      （原話：「然後這一頁在算甚麼也拿掉，我不需要看這個我也不會看」）。
      ⛔ 不要再加回來，也不要改成別的名字重開一個解釋牆。
      ⚠️ 那十條裡有兩條原本是別的守衛的落點，已經改掛在別處、**沒有跟著失效**：
        ・「09:03:30 沒有被證明比 09:04:30 好」是畫面上唯一的第二個時分秒 ⇒
          它走了之後，㉓⑤ 的規則從「只准出現在那一條裡」**收緊成**
          「畫面上除了後端的 signal_at，一個時分秒都不准有」。
        ・少樣本／不預告／不做多數決那幾條的**行為**本來就各自有守衛（⑥／⑪／⑧），
          刪的只是說明文字，紅線一條都沒有放寬。 -->
</div>

<!-- 【回顧】：容器只建這一次，之後只換裡面的內容（重繪不打斷縮放／拖曳、也不閃） -->
<div id="tab-review" hidden>
 <div class="cols">
  <div>
   <div class="card chart l1">
    <div class="chead" id="rhead"></div>
    <div class="legend" id="rlegend"></div>
    <div class="cwrap"><svg id="rsvg" preserveAspectRatio="none"></svg></div>
    <div id="rctrl"></div>
   </div>
   <div class="sec-head" style="margin-top:16px"><h2 id="rftitle">進場當下的客觀盤面</h2>
     <span class="count">只有已經發生的數字</span></div>
   <div class="card"><div class="rail" id="rfstrip"></div></div>
  </div>
  <div class="right">
   <div class="seg" id="rmode">
     <button data-mode="review" class="on">翻紀錄</button>
     <button data-mode="replay">重播練習</button>
   </div>
   <div id="rpane"></div>
  </div>
 </div>
</div>

<!-- ⚠️ 2026-09-09 補上「自動下單（模擬）」那五個字：這一句原本只講練習單，
     而現在面板上有兩顆長得很像的分頁，其中一顆**會真的送單**。
     ⛔ 不可以把這句話寫成「這個面板不會送單」—— 那是一句假話
     （即時分頁的真實下單卡、以及【自動下單】都會送）。 -->
<div class="foot">只顯示已經發生的客觀數字，不做預測、不給買賣訊號。<br>練習下單與【自動下單（模擬）】都是模擬，不會送單到永豐。</div>
</div>
<script>
var WIN=7;
// 真實成績的分段窗口。⚠️ 跟練習的 WIN 分開 —— 共用的話在一邊按會讓另一邊也跳，
// 而且兩邊的筆數差很多（真實只有個位數），可選的窗口本來就不一樣。
var RWIN=7;
const f=(n,d=0)=>n==null?'—':Number(n).toFixed(d);
const sgn=v=>v>0?'up':v<0?'down':'flat';
const pm=(v,d=0)=>(v>0?'+':'')+f(v,d);

var lastMkt='', lastTrade='', lastStats='', lastWarn='', lastReal='', statsCache=null, statsAt=0;

/* ══════════════════════════════════════════════════════════════════════
   ⭐⭐ pfetch —— **這一頁每一個會改變狀態的 POST 唯一的出口**
   ----------------------------------------------------------------------
   ⛔⛔ 【2026-09-09 lab-qa 的 P0】後端 `do_POST` 現在**每一個** POST 都要過
      `fire_post_guard()`（Content-Type／X-Panel／token／Origin／Sec-Fetch-Site／Host）
      —— 因為在那之前，他上網時任何一個網頁都可以用一張純 HTML 表單
      （不必 JS、不必 CORS、不必 token）打 `/api/real/enter`、用他的帳戶真的送單。
   ⛔ 所以前端**每一個**呼叫點都必須帶那三樣。⛔ 不准有第二個地方自己寫
      `fetch(url,{method:'POST',…})` —— 漏一個地方 ＝ 他的某一顆鈕從此按不動，
      而畫面上只會顯示「送不出去」，看不出是自己人擋的。
   ⚠️ token 來自 0.5 秒一次的 `/api/state`（見 `tick()`）：看門狗把面板重開之後
      那個字串會變，靠這條路最多 0.5 秒就換到新的 ⇒ **他的「平倉」不會因此按不動**。
      真的還沒拿到（畫面剛開、第一次 tick 還沒回來）就先去要一次再送 ——
      ⛔ 那是一個 GET，不會有「送兩張單」的風險。
   ══════════════════════════════════════════════════════════════════════ */
var PTOK='';
function ptok(){
  if(PTOK) return Promise.resolve();
  return fetch('/api/state').then(r=>r.json())
    .then(s=>{ if(s&&s.token) PTOK=s.token; }).catch(()=>{});
}
function pfetch(url,body){
  return ptok().then(()=>fetch(url,{method:'POST',
    headers:{'Content-Type':'application/json','X-Panel':'1','X-Panel-Token':PTOK},
    body:body||'{}'}));
}

/* ---------------- 真實下單 ----------------
   ⚠️ REAL_ON 刻意不記進 localStorage：每次開面板都要重新打開。
   記住狀態的話，某天心不在焉點到就是一個真實部位。 */
var REAL_ON=false, HOLD_MS=650, holdTimer=null, holdingNow=false;
/* ---------------- 右欄分頁：練習 ／ 真實 ----------------
   老闆 2026-09-01 拍板：**不自動切分頁**（lab-ux 提案 §2 的 A 案）。
   有真實部位時靠「頁籤上的浮動點數」＋「跨分頁警報橫幅」讓他知道，
   不搶他的畫面 —— 被搶走畫面本身也是一種傷害。
   ⚠️ RTAB 跟 REAL_ON 一樣只活在記憶體裡：重新整理一律回到練習分頁。 */
var RTAB='sim';
var LASTS=null;                    // 最後一次拿到的 /api/state，切分頁時直接拿它重畫
function setRTab(t){
  if(RTAB===t) return;
  RTAB=t;
  // 先用手上的狀態立刻換過去（不等 fetch 回來，也不會因為 /api/state 掛掉就卡住）
  if(LASTS) paintRight(LASTS,true);
  tick(true);
}
function realToggle(){ REAL_ON=!REAL_ON; lastReal=''; tick(true); }
function holdStart(el,dir){
  holdEnd(el);
  holdingNow=true;
  el.classList.add('holding');
  // 只綁在按鈕本身：mouseleave 綁在按鈕上時，游標在按鈕內部的子元素之間移動
  // **不會**觸發。之前是 document + capture，游標稍微一動、離開任何一個
  // 子元素就被判定成放開，所以按到一半就自己取消。
  el.addEventListener('mouseleave',onLeave);
  window.addEventListener('mouseup',onUp,true);
  holdTimer=setTimeout(function(){
    // 保險：按鈕已經不在畫面上就不要送單。理論上有 holdingNow 擋著不會發生，
    // 但這是真錢，寧可多一道。
    if(!document.body.contains(el)){ holdEnd(el); return; }
    holdEnd(el); realFire(dir);
  },HOLD_MS);
}
function onLeave(e){ holdEnd(e.currentTarget); }
// 認 data-rdir 不認 class：按鈕的樣式名稱會改，「它是不是下單鈕」不會改
function onUp(){ document.querySelectorAll('[data-rdir].holding').forEach(holdEnd); }
function holdEnd(el){
  clearTimeout(holdTimer); holdTimer=null; holdingNow=false;
  if(!el) return;
  el.classList.remove('holding');
  el.removeEventListener('mouseleave',onLeave);
  window.removeEventListener('mouseup',onUp,true);
}
var firing=false;
function realFire(dir){
  holdingNow=false;
  // 【擋連按】券商回報部位有 1~2 秒延遲，他以為沒送出去再按一次 ⇒ 兩張都出去、
  // 變成 2 口，而且兩張停利單都掛了、面板只記得後面那張，先掛的**永遠撤不掉**
  // （lab-qa 退件第 1 條）。平倉早就有 closing 擋著，進場一直沒有 —— 又是一邊做了一邊沒做。
  if(firing) return;
  firing=true; lastReal=''; tick(true);
  pfetch('/api/real/enter',JSON.stringify({dir:dir}))
   .then(r=>r.json()).then(r=>{
      // warn＝進場成功但停利沒掛上，那也要跳出來讓他知道（不是失敗，但不能沉默）
      if(!r.ok||r.warn) alert(r.msg||'送不出去');
    })
   .catch(()=>alert('送不出去，面板可能剛好在重啟'))
   .then(()=>{ firing=false; lastReal=''; tick(true); });
}
var closing=false;
function realClose(){
  // 【連按要擋】平倉是反向下單：送兩張的話第二張會開出一個反向的新部位。
  // 拿掉確認框之後連點的機率更高，這道就更不能少。
  if(closing) return;
  closing=true;
  lastReal=''; tick(true);                 // 立刻把按鈕變成「平倉中…」
  pfetch('/api/real/close')
   .then(r=>r.json()).then(r=>{ if(!r.ok) alert(r.msg||'平不掉'); })
   .catch(()=>alert('送不出去，請自己到大戶投平倉'))
   .then(()=>{ closing=false; lastReal=''; tick(true); });
}
/* ══════════════════════════════════════════════════════════════════════
   右欄：跨分頁警報 ＋ 頁籤 ＋ 練習／真實兩個分區
   ----------------------------------------------------------------------
   ⛔ 每一塊各自比對「上次自己設進去的字串」（快取在節點上，見 setEl）——
      整塊 innerHTML 每 0.5 秒重建的話，使用者按下去的那一瞬間按鈕會連同
      事件一起被換掉；長按送單的那顆更嚴重（時間到照樣送單，他以為取消了）。
   ══════════════════════════════════════════════════════════════════════ */

/* 沒有即時報價時，現價退回「最後一根 K 棒的收盤」，並在旁邊明寫「非即時」。
   ⚠️ 這只是顯示用；沒有即時報價時兩區的下單鈕都是真的 disabled。 */
function livePx(s){
  const p=(s.chips||{}).price;
  if(p!=null) return p;
  const B=(barsCache&&barsCache.bars)||[];
  return B.length?B[B.length-1].c:null;
}

/* ---------------- 跨分頁警報 ----------------
   停損活在這台電腦的 Python 迴圈裡。他站在練習分頁時，真錢那一邊照樣要看得到。
   ⚠️ 秒數每秒在變，所以骨架與秒數拆成兩個節點：寫在一起的話「去看部位」
      那顆鈕會跟著每秒被重建，滑鼠停在上面剛好碰到就按不動（跟 #cupd 同一個坑）。
   ⚠️ 這一塊不掛任何 CSS 動畫（舊版 .ralarm.bad 的 1.1 秒呼吸永遠演不完半個循環，
      因為每 0.5 秒就從頭開始 —— 看起來是在抖不是在呼吸）。 */
function xalHTML(R){
  const P=R.position;
  if(!P) return '';
  const go=(RTAB==='real')?'':'<button class="go" data-rgo="1">去看部位 &rarr;</button>';
  if(R.stale_sec!=null)
    return '<div class="n-x n-bad"><div class="g">&#9888; 報價已中斷 '+
      '<span class="num" id="xalsec"></span> 秒　停損現在沒人在看'+
      '<div class="s">停損活在這台電腦裡，收不到報價就判斷不了。'+
      '請立刻到大戶投確認部位。</div></div>'+go+'</div>';
  if(!P.has_target)
    return '<div class="n-x n-att"><div class="g">&#9888; 停利沒有掛上券商　賺的那一邊沒有保護'+
      '<div class="s">請到大戶投自己補掛一張 '+f(R.tp)+' 的平倉限價單，或直接平倉。</div></div>'+
      go+'</div>';
  return '';
}

/* ---------------- 頁籤 ---------------- */
function tabsHTML(R){
  return '<div class="n-tabs">'+
    '<button class="n-tab t-sim'+(RTAB==='sim'?' on':'')+'" data-rtab="sim">'+
      '<div class="t">練習</div><div class="s">模擬・不會送單</div></button>'+
    '<button class="n-tab t-real'+(RTAB==='real'?' on':'')+'" data-rtab="real">'+
      '<div class="t">真實</div><div class="s">'+(R.live?'真的會送單':'演練模式')+'</div>'+
      '<span class="badge" id="tabbadge" hidden></span></button></div>';
}
/* 浮動點數是獨立節點：整條頁籤不可以因為點數跳動就被重建。 */
function paintBadge(R){
  const e=document.getElementById('tabbadge');
  if(!e) return;
  const P=R.position;
  let cls='', txt='';
  if(P&&RTAB!=='real'){
    if(R.stale_sec!=null||!P.has_target){ cls='alert'; txt='!'; }
    else { cls=sgn(R.float_pts); txt=(R.float_pts==null?'—':pm(R.float_pts)); }
  }
  const k=cls+'|'+txt;
  if(e.__k===k) return;
  e.__k=k;
  e.className='badge'+(cls?' '+cls:'');
  e.textContent=txt;
  e.hidden=!txt;
}

/* ══════════════ 練習分頁 ══════════════════════════════════════════
   結構跟真實那一區對稱：下單區 → 今天的練習交易 → 練習成績。
   下單維持**單擊**（練習不需要長按防呆），按鈕維持描邊＋淡底 ——
   那是它跟真實區在形狀上的分界，不要改成實心。 */
function simZone(){
  return '<div class="n-zone z-sim">'+
    '<div class="n-hd"><div class="grow"><div class="t">練習下單</div>'+
    '<div class="s">微台 TMF・1 口・固定 &plusmn;100 點</div></div>'+
    '<span class="n-chip c-sim">模擬・不會送單</span></div>'+
    '<div class="n-sep"></div><div class="n-bd" id="simbody"></div>'+
    '<div id="simtr"></div><div id="simstats"></div></div>';
}
function simBody(s){
  const P=s.position;
  if(P)
    return '<div class="pnl"><div class="v '+sgn(P.float_pts)+'">'+pm(P.float_pts)+'</div>'+
      '<div class="l">'+(P.dir==='long'?'做多':'做空')+'　進場 '+f(P.entry)+'　'+P.entry_time+'</div></div>'+
      '<div class="plimit"><span>停利 '+f(P.tp)+'</span><span>停損 '+f(P.sl)+'</span></div>'+
      '<div class="btns"><button class="btn flat2" data-act="close">手動平倉</button>'+
      '<button class="btn ghost" data-act="undo">取消</button></div>'+
      noteBox('t|open',P.note,'data-nopen="1"','＋ 記下現在為什麼這樣做',
              '現在為什麼想這樣做？（平倉後會留在這筆紀錄裡）');
  // 【紀錄正確性】沒有即時報價就不能開單 —— 拿舊價／收盤價記進練習成績，那筆成績是假的。
  // 這不是 UX 取捨，所以按鈕真的停用（後端 /api/enter 也擋一次）。
  const q=quoteState(s), off=q!=='live', dis=off?' disabled':'';
  let h='<div class="n-px"><div><div class="k">現價</div><div class="n">'+f(livePx(s))+
    (off?'<span class="qty" style="margin-left:8px">非即時</span>':'')+'</div></div>'+
    '<div class="qty">1 口・固定</div></div>'+
    '<div class="btns" style="margin-top:12px">'+
    '<button class="btn long" data-act="long"'+dis+'>&#9650; 做多</button>'+
    '<button class="btn short" data-act="short"'+dis+'>&#9660; 做空</button></div>';
  if(off) h+='<div class="n-why">'+(q==='closed'
    ? '<b>休市中，沒有即時報價</b><br>練習下單要用當下的真實成交價才有意義，'+
      '不然記進成績的是假成績。開盤後才能按。'
    : '<b>目前收不到報價</b><br>無法確定進場價 —— 恢復報價後才能按。')+'</div>';
  return h;
}
/* 今天的練習交易：跟真實那一區同一套固定欄表格。
   ⚠️ 心得（note）要留著 —— 那是跟手機 App 同一個欄位，事後補寫的。
      所以每一筆包成 .n-item：上面一列固定欄、下面掛心得。 */
function simTrades(s){
  const T=s.today_trades||[];
  if(!T.length)
    return '<div class="n-trh">今天的練習交易</div>'+
      '<div class="n-empty">今天還沒有練習紀錄。進場之後這裡會一筆一筆長出來。</div>';
  let sum=0; T.forEach(t=>sum+=t._net);
  let h='<div class="n-trh">今天的練習交易　<span class="c">'+T.length+' 筆</span>'+
    '<span class="net '+sgn(sum)+'">'+pm(Math.round(sum*10)/10)+' 點</span></div>'+
    '<div class="n-trl">';
  T.slice().reverse().forEach(t=>{ h+=simRow(t); });      // 新的在上面
  return h+'</div>';
}
function simRow(t){
  const why={tp:'停利',sl:'停損',manual:'手動',close:'收盤'}[t._reason]||'其他';
  return '<div class="n-item"><div class="n-row">'+
    '<span class="d '+(t.dir==='long'?'l':'s')+'">'+(t.dir==='long'?'&#9650;':'&#9660;')+'</span>'+
    '<span class="tm">'+esc(String(t.time||'—').slice(0,5))+'&rarr;'+
      esc(String(t._exit_time||'—').slice(0,5))+'</span>'+
    '<span class="px">'+f(t.entry)+'&rarr;'+f(t.exit)+'</span>'+
    '<span class="wy">'+why+'</span>'+
    '<span class="pt '+sgn(t._net)+'">'+pm(t._net)+'</span></div>'+
    noteBox(nkey('t',t),t.note,nattr(t),'＋ 寫下今天的心得',
            '今天的盤感、進出場理由、紀律有沒有守…')+'</div>';
}

/* ══════════════ 真實分頁 ══════════════════════════════════════════
   ⚠️ REAL_ON 刻意不記進 localStorage：每次開面板都要重新打開。 */
function realZone(R){
  const chip = !REAL_ON ? '<span class="n-chip c-off">關閉中・按右邊打開</span>'
    : (R.live ? '<span class="n-chip c-real">真的會送單</span>'
              // 藥丸太長會把標頭那行「微台 TMF・1 口・固定 ±100 點」擠成兩行（實測）
              : '<span class="n-chip c-sim">演練模式・不送出</span>');
  return '<div class="n-zone z-real">'+
    '<div class="n-hd"><div class="grow"><div class="t">真實下單</div>'+
    '<div class="s">微台 TMF・1 口・固定 &plusmn;100 點</div></div>'+
    chip+'<div class="sw'+(REAL_ON?' on':'')+'" data-rt="1"><i></i></div></div>'+
    // ⛔ 【有部位就一定要看得到，開關關著也一樣】
    //    `REAL_ON` 只是「要不要露出下單按鈕」的保險蓋，不是「有沒有部位」。
    //    舊寫法在開關關著時整個 body 都不畫 ⇒ **站在寫著「真實」的那一頁，
    //    反而看不到自己的真實部位、也沒有平倉鈕**，唯一的提示（頁籤上的浮動點數）
    //    又剛好只在「不在真實頁」時才掛 ⇒ 站在真實頁＝知道得最少（lab-qa 退件第 2 條）。
    //    而且看門狗重啟是常態、`REAL_ON` 又刻意不記憶 ⇒ **重啟後一定是關的**，
    //    偏偏 reconcile 會把券商那口單撿回來 —— 這個組合遲早會遇到。
    ((REAL_ON || R.position) ?
      '<div class="n-sep"></div><div class="n-bd" id="realbody"></div>' : '')+
    // ⛔ 【成績單不受保險蓋管】Benson 2026-09-02：「不用打開真實下單也可以看得到」。
    //    `REAL_ON` 管的是「會不會送單」，看自己過去的成績跟送不送單無關 ——
    //    而且開關刻意不記憶（重啟後一定是關的），綁在一起等於每天早上都看不到昨天的成績。
    '<div id="realstats"></div>'+
    '<div class="n-foot" id="realfoot"></div></div>';
}
function realBody(s){
  const R=s.real||{}, P=R.position;
  if(R.error) return '<div class="n-why"><b>真實下單模組出錯，先不要用</b><br>'+esc(R.error)+'</div>';
  let h='';
  if(P){
    const fl=R.float_pts;
    // 浮動點數 52px：全頁最大的損益數字不可以是模擬的（舊版真單 34px、練習 44px）
    h+='<div class="n-pos"><div><div class="big '+sgn(fl)+'">'+(fl==null?'—':pm(fl))+
       '<span class="u">點</span></div><div class="cash">'+
       (fl==null?'—':(fl>0?'+':fl<0?'-':'')+'NT$'+Math.abs(Math.round(fl*10)).toLocaleString())+
       '　浮動</div></div><div>'+
       '<span class="n-dir '+(P.dir==='long'?'l':P.dir==='short'?'s':'u')+'">'+
       (P.dir==='long'?'&#9650; 做多':P.dir==='short'?'&#9660; 做空':'? 方向不明')+
       ' '+(P.qty==null?1:P.qty)+' 口</span>'+
       '<div class="n-meta">'+(P.entry_time?esc(P.entry_time)+' 進場 ':'進場 ')+f(P.entry)+
       '</div></div></div>';
    // 【顏色分工】紅綠只描述錢；系統狀態一律中性灰階＋填滿程度。
    // ⚠️「已掛在券商」不可以寫死，一定要讀 has_target（QC 退件第 4 條）。
    const noTp=!P.has_target, blind=R.stale_sec!=null;
    h+='<div class="n-guard">'+
       '<div class="n-g'+(noTp?' n-att':'')+'"><span class="ic '+(noTp?'bad':'solid')+'"></span>'+
       '<span class="lb">停利 <b>'+f(R.tp)+'</b></span><span class="st">'+
       (noTp?'<b>沒掛上</b><br>賺的那一邊沒有保護，請自己補掛或平倉'
            :'<b>已掛在券商</b><br>電腦關機也有效')+'</span></div>'+
       '<div class="n-g'+(blind?' n-att':'')+'"><span class="ic '+(blind?'bad':'hollow')+'"></span>'+
       '<span class="lb">停損 <b>'+f(R.sl)+'</b></span><span class="st">'+
       (blind?'<b>監控不到</b><br>收不到報價'
             :'<b>由這台電腦監控</b><br>券商端做不到停損')+'</span></div>'+
       '<div class="n-gfoot">停損靠這台電腦：面板關掉、電腦睡著、網路斷掉都會失效 —— '+
       '這是 2026-08-28 拍板承擔的風險。</div></div>';
    // 【平倉鈕中性底】做空的平倉送出去的是「買進」，09-01 出過送錯邊、變成再加一口空單的事故。
    // 【不跳確認視窗】要平倉的時候通常是急的，多一個對話框是在最糟的時機加摩擦。
    h+='<button class="n-close"'+(closing?' disabled':'')+' data-rclose="1">'+
       (closing?'平倉中…':'立刻平倉')+'<span class="sub">'+
       (closing?'等券商回報部位真的消失'
         :(P.dir==='long'?'會送出 賣出 &times;1（賣掉多單）'
          :P.dir==='short'?'會送出 買進 &times;1（回補空單）'
          :'方向不明 —— 送出前會再跟券商對帳'))+'</span></button>';
    if(P.recovered) h+='<div class="n-why">這筆是面板重啟後從券商對帳撿回來的，'+
      '停利單的下落請自己到大戶投確認。</div>';
  } else {
    const q=quoteState(s), px=livePx(s), ok=R.can_enter&&!firing;
    h+='<div class="n-px"><div><div class="k">現價</div><div class="n">'+f(px)+
       (q!=='live'?'<span class="qty" style="margin-left:8px">非即時</span>':'')+
       '</div></div><div class="qty">1 口・固定不能改</div></div>'+
       '<div class="n-fire">'+fireBtn('long',px,!ok)+fireBtn('short',px,!ok)+'</div>';
    h+= firing
      ? '<div class="n-firing">送出中…<div class="s">等券商回報成交（最多 5 秒）。'+
        '這段時間再按也不會變成第二張單。</div><div class="bar"><i></i></div></div>'
      : (R.can_enter?'<div class="n-holdhint">按住 0.65 秒送出　·　中途放開就取消</div>':'');
    // 擋單原因：會影響他決定的字，最低只能用 --dim（.n-why），不准再用 --faint
    if(!R.can_enter) h+='<div class="n-why">'+esc(R.why||'現在不能下單')+'</div>';
    const used=R.entries_today||0, max=R.max_entries||3;
    let pips='';
    for(let i=0;i<max;i++) pips+='<i class="'+(i<used?(used>=max?'full':'used'):'')+'"></i>';
    h+='<div class="n-quota"><div class="pips">'+pips+'</div><span>今天進場 '+used+' / '+max+
       '</span></div>';
  }
  return h;
}
/* 真實下單鈕：實心（練習是描邊＋淡底）——形狀在餘光裡最強，這是第四個訊號。
   【停利停損還沒選方向就不顯示】兩個方向的數字互相干擾（Benson 2026-08-28 要求）。
   .sub 平常 opacity:0，按住時才浮出來，同時把標籤換成「放開取消」。 */
function fireBtn(dir,px,dis){
  const long=dir==='long';
  const tp=px==null?null:(long?px+100:px-100), sl=px==null?null:(long?px-100:px+100);
  return '<button class="n-fb '+(long?'b':'s')+'"'+(dis?' disabled':'')+
    ' data-rdir="'+dir+'"><span class="txt">'+
    '<span class="lb">'+(long?'買進 做多':'賣出 做空')+'</span>'+
    '<span class="lb2">放開取消</span>'+
    '<span class="sub">'+(tp==null?'&nbsp;':'停利 '+f(tp)+'　停損 '+f(sl))+'</span>'+
    '</span><span class="bar"></span></button>';
}
/* 版本指紋降到整區最底：純除錯、不影響決定，所以是唯一還准用 --faint 的地方。
   ⚠️ stale＝硬碟上的程式比跑著的這個新 ⇒ 他以為重開過了，其實沒有。一定要講出來。 */
function realFoot(R){
  const c=R.code;
  if(!c) return '';
  return (c.stale
    ? '<span class="stale">面板跑的不是最新的程式　關視窗再開沒有用（會接回同一個舊的），'+
      '要跑 restart-panel.py 才會真的換掉</span>' : '')+
    '<span>程式 '+esc(c.broker)+'　啟動 '+esc(c.started)+'</span>';
}

// 今天的真實交易成績單。
// 【為什麼不放進「練習成績」那一區】那一區會同步到 data/practice.json、推上公開的 repo、
// 再拉到手機。真實交易不上傳是 Benson 的決定，混進去就等於上傳了。
// 所以真單只活在這張卡裡，也只活在這台電腦上。
/* 真實成績：勝率、勝敗、淨點數 —— 版面與「練習成績」一致，他一眼就認得。
   ⚠️ 勝敗的定義要跟練習那邊同一套（點數 > 0 才算勝，0 算敗），
      兩邊用不同定義的話，同一批交易會算出兩個勝率。

   【A 版：完全照練習抄】（Benson 2026-09-03 拍板）
   順序也跟練習一樣：**今天的清單在上、成績區在下**（舊版是反的）。
   過去的紀錄改由成績區底下那份 `.list` 卡片清單承擔，`realPast()` / `.t-past`
   / `.n-dayh` 因此整段移除 —— 同一批資料不要用兩種長相各列一次。
   ⛔ **刻意不放「下載紀錄」那顆鈕**：練習那顆寫著「可匯入 App」，而 App 會同步到
      公開 repo。真實區放一顆長一樣的鈕，他哪天順手匯進去就把真實紀錄推上公開網路，
      而且沒有任何一道防線會擋。這是 Benson 拍板的，不要「順手補上」。 */
function realStats(R){
  const all=(R&&R.trades_all)||[];
  const done=all.filter(t=>t.points!=null);
  // ⛔ 今天的清單一定要掛在 .n-bd 外面（見 realTrades 上面那段註解）——
  //    包進去會多吃左右 38px，全部從 .n-row 唯一的 1fr（價格欄）身上扣，成交價被截。
  let h=realTrades((R&&R.trades)||[])+
    '<div class="n-sep"></div><div class="n-bd n-bd-t">'+
    '<div class="n-sh">真實成績<span class="c">共 '+all.length+' 筆</span></div>'+
    // ⛔ 【分段那一段要能單獨重畫】按 .seg 的時候底下的 .list 裡可能正有人在打心得，
    //    整塊 #realstats 重畫會把 textarea 換掉 ⇒ 重繪守衛只好整塊擋下來 ⇒
    //    **按了畫面完全不動、關掉編輯器才突然跳**（比不動更像壞掉）。
    //    把「共用窗口的那幾塊」包成獨立節點，按鈕就能只換這一塊、不碰 .list。
    '<div id="realscore">'+realScore(all)+'</div>';
  // 卡片清單：跟練習成績底下那份同一種 .trade 卡（含事後補心得）。
  // ⛔ 【清單固定是「最近 12 筆」，不跟著分段窗口縮】練習那邊就是這樣
  //    （statsBox 畫的是 ST.recent，跟 WIN 無關）——分段只換上面的勝率／合計。
  //    跟著縮的話，預設「近 7 筆」剛好只涵蓋今天，**昨天的紀錄又會從畫面上消失**，
  //    等於把他 2026-09-03 早上問的那個問題原封不動放回來（只是這次藏在按鈕後面）。
  // ⚠️ .list 是有 max-height 的 flex 直欄，靠既有的 `.list>*{flex:none}` 才是捲動
  //    而不是把每張卡壓扁 —— 筆數少的時候看不出來，測試一定要用會捲的筆數。
  const cards=realSorted(all).slice(0,12);
  if(cards.length){
    h+='<div class="list">';
    cards.forEach(t=>{ h+=realCard(t); });
    h+='</div>';
  }
  // ⛔ 這裡**不放**「下載紀錄」——理由見函式開頭那段。
  return h+'</div>';
}

/* 只跟「選中哪個窗口」有關的那幾塊：.seg／.score／.wlbar／.wlfoot／.n-why。
   ⛔ 【為什麼要獨立成一個函式＋一個節點】按分段按鈕時，底下的 .list 裡可能正有人
      在打心得。整塊 #realstats 重畫會把那個 textarea 換掉（中文輸入法會掉字），
      所以重繪守衛會把整塊擋下來 ⇒ **按了畫面完全不動，關掉編輯器才突然跳**。
      「按了不動」正是我們當初要避免的那個毛病，延遲跳動又更像壞掉。
      切開之後，[data-rwin] 的處理只換 #realscore，.list 一個節點都不碰 ——
      正在打的字活著，數字也立刻變。
   ⚠️ 心得**不在**這一段裡（心得在 .list 的卡片與上面那份 .n-row 裡），
      所以這一塊可以隨時重畫。要往這裡加任何帶輸入框的東西之前先想一下這件事。
   守衛：hold-to-fire.mjs ⑧b7。 */
function realScore(all){
  const wins2=realWindows(all);
  const cur=wins2.find(x=>x.k===RWIN)||wins2[wins2.length-1];
  let h='';
  // ⚠️ 算完**去重**，只剩一個窗口就整條不畫 —— 真實筆數少，那四顆常常對到同一批資料，
  //    按了畫面完全不動比沒有這排按鈕更糟。
  if(wins2.length>1){
    h+='<div class="seg">';
    wins2.forEach(x=>{ h+='<button class="'+(x===cur?'on':'')+'" data-rwin="'+x.k+'">'+
      x.label+'</button>'; });
    h+='</div>';
  }
  // 勝率／勝敗條算的是**選中那個窗口**（跟練習的 statsBox 同一套），
  // 而上面「共 N 筆」講的是全部 —— 兩個數字的口徑不一樣，所以窗口的筆數印在勝敗條下面。
  const win=cur.rows, wdone=win.filter(t=>t.points!=null);
  if(!wdone.length)
    return h+'<div class="n-empty">還沒有算得出點數的真實交易。'+
      '進場、平倉之後這裡就會有勝率。</div>';
  const wins=wdone.filter(t=>t.points>0).length, losses=wdone.length-wins;
  const net=Math.round(wdone.reduce((a,t)=>a+t.points,0)*10)/10;
  const cls=net>0?'up':net<0?'down':'flat';
  const ntd=Math.round(net*10);          // 微台 1 點 = NT$10
  h+='<div class="score"><div class="rate"><span class="n">'+
     Math.round(wins/wdone.length*100)+'</span><span class="p">%</span>'+
     '<div class="lab">勝率</div></div>'+
     '<div class="sum"><div><span class="n '+cls+'">'+pm(net)+
     '</span><span class="u">點</span></div>'+
     '<div class="cash">'+(ntd<0?'-':'+')+'NT$'+Math.abs(ntd).toLocaleString()+'</div>'+
     '</div></div>'+
     '<div class="wlbar"><i class="w" style="flex:'+Math.max(wins,0.001)+'"></i>'+
     '<i class="l" style="flex:'+Math.max(losses,0.001)+'"></i></div>'+
     '<div class="wlfoot"><span class="w"><b>'+wins+'</b> 勝</span>'+
     '<span>'+wdone.length+' 筆</span><span class="l"><b>'+losses+'</b> 敗</span></div>';
  // 算不出點數的那幾筆不能默默不算進勝率 —— 要講出來（講的是這個窗口裡的）
  if(win.length>wdone.length)
    h+='<div class="n-why">另有 '+(win.length-wdone.length)+
       ' 筆問不到成交價，沒有算進勝率</div>';
  return h;
}

/* 分段窗口（近 7／近 10／近 30／全部）。
   ⛔ **算完要去重**：真實筆數少，四個窗口常常對到同一批資料 ——
      按了畫面完全不動比沒有這排按鈕更糟（練習那邊 11 筆時「近10」與「近30」就一模一樣）。
      去重之後只剩一個窗口的話，realStats() 整條 .seg 都不畫。
   ⚠️ 排序自己來（date + entry_time，新到舊），不要依賴 trades_all 的既有順序 ——
      那份是「今天那段反過來、再一個檔案一個檔案接上去」，剛好是新到舊但那是實作細節。 */
/* 平倉理由的中文標籤。**今天那份 .n-row 與成績區的卡片共用這一份**，
   不要各自帶一份 map —— 同一筆交易在同一個畫面上出現兩次，字不一樣看起來像兩件事。
   ⛔ 認不得的一律寫「其他」：內部代號（sl_test 之類）不該印到他眼前。
   ⛔ **每個標籤最多 4 個字，這是量出來的版面硬限制，不是文案偏好。**
      這個數字是怎麼來的（2026-09-03 實測，字級/字距沒改就一直成立）：
        · 卡片那一行是 `.tr-date`(42px 固定) ＋ `.dir` ＋ `.tr-px`(flex:1) ＋ `.tr-res`(52px 固定)，
          扣掉 gap 之後 **`.tr-px` 只剩 157.6px**，而它裡面要塞「價格→價格 ＋ 標籤」。
        · 量到的需求寬度：2 字標籤 **120.5px**、4 字 **140.5px**（都放得下，餘裕 17.1px）、
          **6 字 161px ⇒ 超出 157.6px**。
        · `.tr-px` 沒有 nowrap ⇒ 超出時是**折行**（不是截斷、不會報錯），
          **那張卡從 65px 變成 80px**，跟練習的卡片就不一樣高了 ——
          而「形式長的一樣」是老闆對這一版的主要要求。
      所以 closed_elsewhere 從「不是面板平的」（6 字）縮成「別處平的」（4 字）。
      ⚠️ 縮寫要保留原本的語氣（這支面板從頭到尾講人話），不要寫成公文腔。
      ⚠️ 不可以改用 text-overflow:ellipsis 把它藏起來 —— 那是把問題藏起來不是修好，
         而且筆數少的時候看起來完全正常（09-03 成交價那個 bug 就是這樣活了兩天）。
      守衛：`hold-to-fire.mjs` ⑧b6（每一種理由各造一張卡，斷言全部不折行）。 */
const RWHY={sl:'停損', tp:'停利', manual:'手動', closed_elsewhere:'別處平的'};
function rwhy(t){ return RWHY[t&&t.reason]||'其他'; }

/* 新到舊排序。⚠️ 明著照 date + entry_time 排，不要依賴 trades_all 的既有順序 ——
   那份是「今天那段反過來、再一個檔案一個檔案接上去」，它現在剛好是新到舊，
   但那是後端的實作細節不是保證。 */
function realSorted(all){
  return all.slice().sort((a,b)=>
    ((b.date||'')+' '+(b.entry_time||'')).localeCompare((a.date||'')+' '+(a.entry_time||'')));
}
function realWindows(all){
  const sorted=realSorted(all);
  const out=[], seen={};
  [[7,'近 7 筆'],[10,'近 10 筆'],[30,'近 30 筆'],[0,'全部']].forEach(function(p){
    const rows=p[0]?sorted.slice(0,p[0]):sorted;
    if(seen[rows.length]) return;          // 筆數一樣＝同一批資料，不重複給一顆按鈕
    seen[rows.length]=1;
    // 這個窗口其實已經涵蓋全部時，標籤就寫「全部」不要寫「近 30 筆」——
    // 練習那邊的窗口是後端給的（近 7／近 10／全部），從來不會出現「近 30 筆」
    // 卻剛好等於全部的情況。寫「近 30 筆」會讓人以為還有更早的沒被算進去。
    const all=rows.length===sorted.length;
    out.push({k:all?0:p[0], label:all?'全部':p[1], rows:rows});
  });
  return out;
}

/* 一筆真實交易的卡片（成績區底下那份清單）。版面完全照練習的 row(t,'s')。
   左緣 2px 色條照練習掛 .win / .loss（2026-09-03 Benson 拍板）——
   兩區並排時，練習是一排紅綠條紋、真實卻是一片灰，那是兩邊看起來最不一樣的地方。
   勝敗定義跟練習同一套：**點數 > 0 才算勝，0 算敗**（兩邊用不同定義，同一批交易會算出兩個勝率）。
   ⛔ 但**算不出點數的那一筆維持 --ghost 灰、不掛 .win/.loss** —— 那筆問不到成交價，
      猜輸贏就是編數字。留白看得出來是缺，猜出來的看不出來。
   ⛔ `t.exit` 在真實那邊**可能是 null**（問不到成交價時留白）。這裡只印字串所以安全，
      但凡是把真實資料餵進練習的算式（例如 Math.min(lo, t.entry, t.exit)）都要先問一次
      「這個欄位在真實那邊會不會是 null」—— null 會被當成 0，把價格軸整個拉到 0。
   ⚠️ 練習的 row() 對 App 匯入的那幾筆是唯讀的（_source==='app'），
      真實這邊**每一筆都要可編輯**，那段唯讀邏輯不要抄過來。 */
function realCard(t){
  const why=rwhy(t), na=t.points==null;
  // ⚠️ exit_time 要一起帶：entry 是 null 時 nkey() 拿它當識別，少帶的話
  //    兩張同日同分鐘的卡會生出同一個 key（見 nkey 的註解）。nattr() 不用它。
  const nt={date:t.date, time:String(t.entry_time||'').slice(0,5), entry:t.entry,
            exit_time:t.exit_time};
  // 算得出點數才有輸贏色；算不出的留空 ⇒ .trade::before 保持 --ghost 灰
  return '<div class="trade'+(na?'':(t.points>0?' win':' loss'))+'"><div class="tr-top">'+
    '<span class="tr-date">'+esc(t.date?t.date.slice(5):'')+'</span>'+
    '<span class="dir '+(t.dir==='long'?'l':'s')+'">'+(t.dir==='long'?'▲ 多':'▼ 空')+'</span>'+
    '<span class="tr-px">'+f(t.entry)+'<span class="arrow">&rarr;</span>'+
      (t.exit==null?'—':f(t.exit))+' <span class="tag">'+why+'</span></span>'+
    '<span class="tr-res '+(na?'na':(t.points>0?'r-win':'r-loss'))+'">'+
      (na?'—':pm(t.points))+'</span></div>'+
    // ⛔ 【分區字母是大寫 S，不是 R】同一筆今天的交易會**同時**出現在上面那份
    //    「今天的真實交易」（.n-row，用 R）與這份卡片清單裡。兩邊用同一個 nkey 的話，
    //    點一下會展開**兩個** id 都叫 tnote 的 textarea，
    //    `document.getElementById('tnote')` 只拿得到第一個 ⇒ 在第二個打的字存不進去。
    //    練習那邊本來就是這樣分的（今天的清單用 't'、成績區的卡片用 's'），
    //    真實這邊照抄成 R / S。小寫 r／s 已經被回顧與練習佔走，不可以共用。
    // ⚠️ 真實交易的心得只留在這台電腦，**不上傳**（走 /api/note 的 kind:"real"）。
    noteBox(nkey('S',nt), t.note, nattr(nt)+' data-nkind="real"',
            '＋ 補寫心得', '現在回頭看，這一筆做對了什麼、做錯了什麼？')+'</div>';
}

function realTrades(list){
  if(!list||!list.length)
    return '<div class="n-trh">今天的真實交易</div>'+
      '<div class="n-empty">今天還沒有真實交易。進場之後這裡會一筆一筆長出來。</div>';
  const done=list.filter(t=>t.points!=null);
  const miss=list.length-done.length;
  const net=Math.round(done.reduce((a,t)=>a+t.points,0)*10)/10;
  // 【合計要說清楚算的是哪幾筆】舊版標題寫「N 筆」、旁邊卻只加總算得出點數的那幾筆，
  // 看起來像 N 筆的總和 —— 有筆數對不起來時等於報了一個錯的成績。
  let h='<div class="n-trh">今天的真實交易　<span class="c">'+list.length+' 筆'+
    (miss?'（'+done.length+' 筆算得出點數）':'')+'</span>'+
    (done.length?'<span class="net '+sgn(net)+'">'+pm(net)+' 點</span>':'')+
    // t-today 純粹是「這是哪一份清單」的標記（沒有樣式）。A 版之後真實區只剩這一份
    // .n-trl（.t-past 已隨 realPast() 移除），但標記留著 —— 探針與日後的程式
    // 一律靠這個 class 認人，不可以靠「第幾個 .n-trl」。
    '</div><div class="n-trl t-today">';
  for(let i=list.length-1;i>=0;i--)              // 新的在上面
    h+=realRow(list[i], '＋ 寫下今天的心得', '今天的盤感、進出場理由、紀律有沒有守…');
  h+='</div>';
  // 出場價問不到就留白，不可以拿現價冒充 —— 留白看得出來是缺，編的數字看不出來
  if(miss)
    h+='<div class="n-trnote"><b>'+miss+' 筆</b>問不到成交價，算不出點數 —— '+
      '那幾筆的損益請到大戶投看。'+
      (done.length?'合計的 '+pm(net)+' 點只含算得出來的 '+done.length+' 筆。':'')+'</div>';
  return h;
}
/* 一筆真實交易那一列（今天與過去共用一份 —— 兩邊長得不一樣的話，
   同一筆交易過了午夜就換個樣子，看起來像不是同一筆東西）。
   【結構要跟練習一樣】每一筆是 `.n-item` 包住「一列 ＋ 底下的心得」，
   心得的樣式掛在 `.n-item .noteline` 上；裸的 .n-row 吃不到那組樣式
   （他 2026-09-02 要求對齊）。 */
function realRow(t, hint, ph){
  const why=rwhy(t);
  let h='<div class="n-item"><div class="n-row">'+
    '<span class="d '+(t.dir==='long'?'l':'s')+'">'+(t.dir==='long'?'&#9650;':'&#9660;')+'</span>'+
    '<span class="tm">'+esc(String(t.entry_time||'—').slice(0,5))+'&rarr;'+
      esc(String(t.exit_time||'—').slice(0,5))+'</span>'+
    '<span class="px">'+f(t.entry)+'&rarr;'+(t.exit==null?'—':f(t.exit))+'</span>'+
    '<span class="wy">'+why+'</span>'+
    '<span class="pt '+(t.points==null?'na':sgn(t.points))+'">'+
    (t.points==null?'—':pm(t.points))+'</span></div>';
  // 心得：跟練習同一套（點一下展開輸入框、事後補寫）。
  // 分區字母用大寫 R —— 小寫 r 已經被回顧分頁佔走，同一個字母會讓
  // nEditing() 分不出是誰在編輯，兩邊的輸入框會互相打架。
  // ⚠️ 真實交易的心得只留在這台電腦，**不上傳**（跟成績單一樣）。
  // ⚠️ exit_time 要一起帶：entry 是 null 時 nkey() 拿它當識別，少帶的話
  //    兩張同日同分鐘的卡會生出同一個 key（見 nkey 的註解）。nattr() 不用它。
  const nt={date:t.date, time:String(t.entry_time||'').slice(0,5), entry:t.entry,
            exit_time:t.exit_time};
  return h+noteBox(nkey('R',nt), t.note, nattr(nt)+' data-nkind="real"', hint, ph)+'</div>';
}

/* ⛔ 【realPast() / .t-past / .n-dayh 已於 2026-09-03 的 A 版整段移除】
   舊版真實區底下另有一份「過去的真實交易」，照日期分組、每天一個 .n-dayh 小計。
   A 版（Benson 拍板「完全照練習抄」）改成：跨日的紀錄由**成績區底下那份卡片清單**
   （realCard，取 trades_all 前 12 筆）承擔 —— 同一批資料不要用兩種長相各列一次。
   **「昨天的紀錄不見了」那個需求仍然被滿足**（他 2026-09-03 早上問的那件事）：
   卡片上有日期欄（.tr-date），過去幾天的每一筆都列得出來、也都能事後補心得。
   ⚠️ 跟著移除的還有「今天／過去」的切分，所以這一版**不再需要** R.today ——
      但 realTrades() 用的 R.trades 仍然是後端按日期切好的今天那份，
      前端一樣不可以用 new Date() 自己算今天（跨午夜兩邊會不同一天）。 */
function rrow(k,v){ return '<div class="rrow"><span class="k">'+k+'</span><span class="v">'+v+'</span></div>'; }

/* ---------------- 右欄總繪製 ----------------
   ⛔ 呼叫端一定要用 holdingNow 擋著：長按送單期間含那顆按鈕的節點不准重繪。 */
function paintRight(s,nf){
  const R=s.real||{};
  // ① 跨分頁警報（骨架與秒數分成兩個節點）
  setEl('xal', xalHTML(R));
  setEl('xalsec', R.stale_sec==null?'':String(R.stale_sec));
  // ② 頁籤（浮動點數是裡面的獨立節點）
  setEl('ntabs', tabsHTML(R));
  paintBadge(R);
  // ③ 整區：只有切分頁／開關真實下單／演練↔真實 才重建骨架
  const zone=document.getElementById('zone');
  if(!zone) return;
  // 骨架的識別鍵。⚠️ 一定要帶「有沒有部位」——開關關著時 body 的存在與否由部位決定，
  // 不帶的話，部位出現時骨架不會重建，那口真錢就永遠畫不出來（配合 realZone 那條註解看）。
  const zk=RTAB+'|'+(RTAB==='real'?(REAL_ON?'on':'off')+'|'+(R.live?'live':'dry')
                                  +'|'+(R.position?'pos':'flat'):'');
  if(zone.__zk!==zk){
    zone.__zk=zk;
    zone.innerHTML=(RTAB==='real'?realZone(R):simZone());
  }
  if(RTAB==='real'){
    setEl('realbody', realBody(s));
    // 編輯心得時不重畫這一區 —— 中文輸入法打到一半整個 textarea 被換掉，字會不見。
    // 「展開輸入框」本身也是一次重繪，所以刻意的重繪帶 nf 旗標繞過去（跟練習那邊同一招）。
    // ⚠️ #realstats 裡有**兩份**清單：今天那份 .n-row 用分區 R、成績區的卡片用 S。
    //    只擋 R 的話，在卡片上打字會被下一輪重繪洗掉（漏一個字母就等於沒有這道保護）。
    if(nf||(!nEditing('R')&&!nEditing('S'))) setEl('realstats', realStats(R));
    setEl('realfoot', realFoot(R));
  } else {
    // 編輯心得時不重畫那一區 —— 中文輸入法打到一半整個 textarea 被換掉，字會不見。
    // 「展開輸入框」本身也是一次重繪，所以刻意的重繪帶 nf 旗標繞過去。
    if(nf||!nEditing('t')){ setEl('simbody', simBody(s)); setEl('simtr', simTrades(s)); }
    if(nf||!nEditing('s')) setEl('simstats', statsBox(statsCache));
  }
}

/* ---------------- 心得：跟手機 App 同一個 note 欄位 ----------------
   面板是即時下單，成交當下沒空打字，所以心得一律「事後補寫」：
   點那一筆就展開輸入框，存檔後跟練習紀錄一起同步上雲，手機開 App 就看得到。
   NOTE.key 前面那個字母是分區（t=練習下單、s=練習成績、r=回顧），
   因為同一筆交易會同時出現在好幾個清單裡，不分區就會有兩個 id 相同的輸入框。 */
var NOTE={key:null,text:''};
function nkey(ns,t){
  // ⚠️ entry 認不出數字時不可以退成 0 —— `Math.round(null)` 是 0，兩筆這種資料就會
  //    生出一模一樣的 key（`S|||0`），點一下會展開兩個 id 都叫 tnote 的輸入框。
  //    真實那邊的欄位不保證都有值（見 CLAUDE.md「練習那邊一定有的欄位，真實那邊不一定有」）。
  // ⛔ **`null` 一定要單獨擋**：`Number(null)` 是 **0**、`Number.isFinite(0)` 為真 ⇒
  //    只寫 `Number.isFinite()` 的話 null 走的還是舊路徑，防呆等於沒做
  //    （QA 2026-09-03 實測 `nkey('S',{entry:null})` 還是回 `S|||0`）。
  //    只有 `undefined` / 非數字字串才會變 NaN，那是最不可能發生的那一種。
  // ⚠️ 退路要用**這一筆自己才有的東西**當識別：`exit_time` 由呼叫端一起帶進來
  //    （realRow / realCard 的 nt 物件），少帶的話兩張同日同分鐘的卡照樣會撞。
  const e=Number(t.entry);
  const bad=(t.entry==null||!Number.isFinite(e));
  return ns+'|'+(t.date||'')+'|'+String(t.time||'').slice(0,5)+'|'+
    (bad?'na'+String(t.exit_time||t.time||''):Math.round(e));
}
function noteBox(key,note,attrs,hint,ph){
  if(NOTE.key===key)
    return '<div class="nedit"><textarea id="tnote" maxlength="500" placeholder="'+ph+
      '">'+esc(NOTE.text)+'</textarea><div class="nbtn">'+
      '<button class="btn flat2" data-nsave="1">儲存</button>'+
      '<button class="btn ghost" data-ncancel="1">取消</button></div></div>';
  return '<div class="noteline'+(note?'':' empty')+'" data-nedit="'+esc(key)+'" '+attrs+
    ' data-note="'+esc(note||'')+'">'+(note?'「'+esc(note)+'」':hint)+'</div>';
}
function nattr(t){
  return 'data-nd="'+esc(t.date||'')+'" data-nt="'+esc(String(t.time||'').slice(0,5))+
    '" data-ne="'+Math.round(t.entry)+'"';
}
/* 編輯中就不重畫那一區 —— 中文輸入法打到一半被換掉整個 textarea，字會直接不見。
   但「展開輸入框」本身也是一次重繪，會被自己這道保護擋掉（第一次就踩到），
   所以刻意的重繪要帶 force 旗標繞過去。 */
function nEditing(ns){ return NOTE.key!=null && NOTE.key[0]===ns; }
function nrepaint(){
  lastTrade=''; lastStats=''; lastPane='';
  if(TAB==='review') rvRender(true); else tick(true);
  setTimeout(function(){ const el=document.getElementById('tnote');
    if(el){ el.focus(); el.setSelectionRange(el.value.length,el.value.length); } },0);
}

/* ---------------- 報價狀態（Bug A） ----------------
   後端把「沒有報價」分成兩種：closed＝休市中（正常）、nodata＝盤中卻收不到（示警）。
   以前兩種都叫 waiting，前端只好用一道門把整個即時分頁擋掉 ——
   結果假日、國定假日、13:45~15:00、05:00~08:45 全是一片空白，
   連本機明明就有的歷史 K 線與日期選單都用不了。現在「有沒有報價」跟「畫不畫圖」分開：
   圖照畫（沒有即時價就用最後一根 K 棒的收盤），只是把狀態誠實標出來。
   舊版後端沒有 quote 欄位，就退回看 chips.price 推 —— 測試治具也走這條。 */
function quoteState(s){
 if(s.quote) return s.quote;
 return (s.chips&&s.chips.price!=null)?'live':'closed';
}
const QLAB={closed:['休市中','var(--faint)'],nodata:['無報價','var(--gold)']};
const QMSG={
 closed:'<div class="note"><b>休市中</b>　現在不是交易時段，沒有即時報價。'+
        '圖上顯示的是最後一根 K 棒的收盤價，不是即時價 —— 歷史 K 線與換日照常可用。</div>',
 nodata:'<div class="alert"><b>盤中卻收不到報價</b>　'+
        '若今天是國定假日就是正常休市；否則是連線問題，程式每分鐘會自動重連。</div>'};

async function tick(nf){
 let s; try{ s=await (await fetch('/api/state')).json(); }catch(e){ return; }
 /* ⛔⛔ token 每 0.5 秒跟著換新（看門狗重啟後那個字串會變）——
    ⛔ 少了這一行，重啟之後他的「平倉」鈕會 403 按不動，而畫面上看不出原因。 */
 if(s&&s.token) PTOK=s.token;
 LASTS=s;
 // 成績每 5 秒抓一次就好 —— 它會讀所有紀錄檔，沒必要跟著報價跳
 if(Date.now()-statsAt>5000){
   statsAt=Date.now();
   fetch('/api/stats').then(r=>r.json()).then(x=>{statsCache=x;}).catch(()=>{});
 }
 const q=quoteState(s);
 const PH={recording:['記錄中','var(--up)'],live:['顯示中','var(--down)'],off:['夜盤','var(--faint)']};
 const ph=QLAB[q]||PH[s.phase]||PH.off, age=s.age_sec==null?99:s.age_sec;
 // 休市中不算「斷線」—— 沒有報價是正常的，燈號要中性，不然每個週末都在紅燈
 const dead=q==='live'?((s.conn&&s.conn.ok===false)||age>90):(q==='nodata');
 const dot=q==='closed'?'off':(dead?'dead':(q==='live'&&age>25?'stale':''));
 document.getElementById('clk').textContent=(s.clock||'').slice(0,5);
 document.getElementById('ph').innerHTML='<span style="color:'+ph[1]+'">'+ph[0]+'</span>';
 document.getElementById('sub').innerHTML='<span class="dot '+dot+'"></span>'+
   ((s.conn&&s.conn.contract_name)||'微台')+(s.replay?'・重播':'');

 // 【回顧】分頁時只更新頂列的時鐘／連線燈；即時分頁的 DOM 一律不動。
 // 後端的報價、持倉監控、±100 自動停利停損跑在 shioaji 回呼裡，完全不受影響。
 if(TAB!=='live') return;

 fetchBars(false);
 const warn = QMSG[q] || (dead ? '<div class="alert"><b>報價已中斷</b>　畫面上的數字是舊的（'+
   (s.age_sec==null?'尚未收到':age+' 秒前')+'）。程式每分鐘會自動重連。</div>' : '');
 setHTML('warn',warn);
 // 畫不出 K 線時退回 fallback 卡片（它一樣帶著翻頁列與月曆，見 pagerHTML）。
 // barsCache===null 這一關一定要擋在前面：那是開站的頭 0.3~0.5 秒，一根 K 棒都還沒回來，
 // 骨架留著就好 —— 不然那半秒會先塞一張小卡，圖回來時整個被頂掉，版面跳一下。
 if(!paintChart(s) && barsCache!==null) paintFallback(s,q);
 // 【長按期間不准重繪整個右欄】下單鈕那一區有現價，每次報價變動 innerHTML 就被換掉，
 // 按住的那顆按鈕當場被銷毀 —— 畫面看起來像「按到一半自己取消」。
 // 更糟的是計時器還握著那顆已經不在畫面上的按鈕，時間到照樣送單：
 // 使用者以為取消了，單卻出去了（2026-09-01 Benson 回報）。
 if(!holdingNow) paintRight(s,nf);
}

// 只有內容真的變了才動 DOM。否則每 0.5 秒重建一次，
// 使用者剛好在那一瞬間按下去，按鈕會連同事件一起被換掉 → 第一下沒反應。
function setHTML(id,html){
 const box={mkt:'lastMkt',trade:'lastTrade',stats:'lastStats',warn:'lastWarn',real:'lastReal'}[id];
 if(window[box]===html) return;
 window[box]=html;
 document.getElementById(id).innerHTML=html;
}

/* 單一節點版的 setHTML：比對的是「上次自己設進去的那個字串」（快取在節點上），
   絕對不可以讀回 e.innerHTML 來比 —— 瀏覽器解析後再序列化的結果跟原字串不一樣，
   裸屬性 disabled 讀回來是 disabled=""，守衛會整個失效（見 paintChart 的長註解）。 */
function setEl(id,html){
 const e=document.getElementById(id); if(!e) return;
 if(e.__html===html) return;
 e.__html=html;
 e.innerHTML=html;
}

/* 沒有 K 棒可畫時的卡片。
   ⚠ 它一定要含翻頁列與月曆：舊版在這個狀態下把整張 K 線卡（連同換日控制）換成
   一張只有數字的小卡，於是「換到有資料的那天」這條唯一的自救路徑也一起消失了。
   做法跟 paintChart 一樣：外框只建一次，之後只換裡面的節點 ——
   整塊 innerHTML 每 0.5 秒重建的話，使用者按下去的那一瞬間按鈕會連事件一起被換掉。
   翻頁列與報價分成兩個節點：成交價每秒在跳，寫在一起會讓 ◀ ▶ 跟著每秒被重建。 */
function paintFallback(s,q){
 if(!document.getElementById('mfall')){
   document.getElementById('mkt').innerHTML=
     '<div class="card l1" id="mfall">'+
     '<div class="cheadwrap"><div class="chead">'+
     '<div id="mfq"></div><div id="mfpager"></div></div>'+
     '<div class="calpop" id="cpick"></div></div>'+
     '<div id="mfbody"></div></div>';
 }
 const c=s.chips||{}, hasPx=(q==='live'&&c.price!=null);
 // 只寫「現在收到的成交價」，不換算漲跌 —— 漲跌的基準是上一個交易日的日盤收盤，
 // 那個值跟著 K 棒一起來（barsCache.ref），這個狀態下本來就沒有，硬算會是錯的。
 setEl('mfq','<div class="qblock"><div class="qmain">'+
   '<span class="cpx flat">'+(hasPx?f(c.price):'—')+'</span></div>'+
   '<div class="qsub">'+(hasPx
     ? '<span class="live"><i></i>即時</span><span class="sep">·</span><span>成交價</span>'
     : '<span class="live dead"><i></i>'+(q==='closed'?'休市中':'收不到報價')+'</span>'+
       '<span class="sep">·</span><span>沒有價格可顯示</span>')+
   '</div></div>');
 setEl('mfpager',pagerHTML(null));
 setEl('cpick',pickOpen?calHTML():'');
 // 【還在載入 ≠ 這天沒有資料】兩者的文案完全不同：前者叫他等，後者叫他換一天。
 // 剛啟動還沒跟永豐要到資料時說「這一天沒有 K 線」，等於叫他去做沒有用的事。
 const still=barsLoading()||barsPending;
 setEl('mfbody', still
   ? '<div class="note skel-note"><b>K 線資料載入中…</b>　'+
     '正在跟永豐要這個交易日的 K 棒。<span class="dots"><i></i><i></i><i></i></span>'+
     (s.msg?'<br><span style="color:var(--faint)">'+s.msg+'</span>':'')+'</div>'
   :
   '<div class="note"><b>這一天沒有 K 線可以畫</b>　'+
   '本機的歷史檔沒有這一天，也還沒跟永豐要到。'+
   '用上面的 ◀ ▶、月曆或鍵盤 ← → 換到別的交易日就看得到。'+
   (s.msg?'<br><span style="color:var(--faint)">'+s.msg+'</span>':'')+'</div>'+
   (hasPx?'<div class="grid">'+
     cell('最近 5 分鐘',pm(c.mom5)+' 點',sgn(c.mom5))+
     cell('最近 15 分鐘',pm(c.mom15)+' 點',sgn(c.mom15))+'</div>':''));
}


// ---------------- K 線圖（純 SVG，TradingView 式操作） ----------------
// 滾輪＝縮放疏密（以游標位置為中心）、按住拖曳＝左右移動時間、雙擊＝還原。
// 價格軸自動貼合「畫面上看得到的那幾根」，跟看盤軟體一樣。
var barsCache=null, barsAt=0, viewDate='', pickOpen=false, calMonth='';
var barsPending=false;        // 換日的資料還在路上（見 fetchBars）
// 夜盤資料還沒到齊（後端 partial）的起算時間。
// 他 2026-09-02：「如果 k 圖還沒有畫好，可以顯示 loading 動畫，不要直接讓我看到錯的 k 圖」。
// ⚠️ 要有上限：永豐真的連不上時 partial 會一直是 true，沒有上限的話他會對著
//    一張永遠在轉的圖乾等。超過 PARTIAL_WAIT 就把圖照常畫出來，靠標題那行說明。
var partialSince=null;
const PARTIAL_WAIT=30000;
function barsLoading(){
  const part=!!(barsCache&&barsCache.partial);
  if(!part){ partialSince=null; return false; }
  if(partialSince==null) partialSince=Date.now();
  return (Date.now()-partialSince)<PARTIAL_WAIT;
}
var barsSeq=0;                // 換日請求的流水號，只採用最後一次的回應（見 fetchBars）
// n＝看得到幾根；end＝最右邊那根的索引（null＝跟著最新）
// vz＝價格軸縮放倍率（>1 放大、<1 壓縮）；voff＝價格軸平移量（單位：點）
var VIEW={n:60, end:null, vz:1, voff:0};
var lastSpan=0;   // 目前畫面的價格跨度（直向平移換算用）
var HOVER={i:null};   // 游標對到的 K 棒（全域索引），null＝顯示最新那根
var DRAG=null;

function fetchBars(force){
 // 3 秒 → 1 秒。以前 /api/bars 整支要 0.5~0.85 秒（每次都跟永豐重抓今天的 K 棒），
 // 抓密一點只是白費力氣；現在今天的 kbars 有 20 秒短快取、最新那幾分鐘由 tick 現算，
 // 實測 0.04~0.07 秒，所以可以跟著報價一起跳。tick() 本身是 0.5 秒一次。
 if(!force && Date.now()-barsAt<1000) return;
 barsAt=Date.now();
 // force＝使用者換日（含按「今天」回到即時）。到新資料回來為止都算「載入中」。
 // 不能只靠「curDay() 跟 barsCache.date 不一樣」判斷 —— 按「今天」時 viewDate 變成空字串，
 // curDay() 會直接回舊的 barsCache.date，看起來沒在載入，那一秒就會把昨天的練習
 // 掛在金點「即時」底下（跟舊版把它掛在「今天」底下是同一個坑）。
 if(force) barsPending=true;
 // 【只認最後一次請求】連按換日時，先送出的請求可能後回來（伺服器每換一天就要重篩
 // 54 萬列，多執行緒之間不保證順序）。沒有這道守衛，barsCache 會被舊那天的資料蓋回去，
 // 而且 barsCache.date 跟 curDay() 又剛好對得上 ⇒ loading 判定不出來，
 // 畫面就在「即時」底下顯示別天的 K 線與練習筆數（實測按 Home 之後停在 08-18）。
 const my=++barsSeq;
 // full=1＝完整交易日（前一晚 15:00 夜盤 → 當天 13:45 收盤）。
 // 回顧分頁走的是同一支 API 但不帶 full，那邊只要日盤。
 fetch('/api/bars?full=1'+(viewDate?('&date='+viewDate):''))
  .then(r=>r.json()).then(x=>{ if(my!==barsSeq) return; barsCache=x; barsPending=false; })
  .catch(()=>{ if(my===barsSeq) barsPending=false; });
}

// 價格軸的「上次算出來的自動範圍」——給遲滯用，見 chartSVG() 裡的說明
var AXIS={key:null, hi:0, lo:0, step:0};

/* 把「一格大概多少」修成好看的刻度：1／2／2.5／5／10 × 10^n。
   貼齊之後刻度線會落在整數（46500、46600…），而且價格動個幾點不會再牽動整條軸。 */
function niceStep(raw){
  if(!(raw>0)) return 10;
  const p=Math.pow(10,Math.floor(Math.log10(raw))), n=raw/p;
  return (n<=1?1:n<=2?2:n<=2.5?2.5:n<=5?5:10)*p;
}

function chartGeom(){
 const all=(barsCache&&barsCache.bars)||[];
 if(!all.length) return null;
 // n 有「至少 8 根」的下限（避免縮太近），但總根數可能比 8 少 ——
 // 早盤 09:25 之前 5 分 K 不滿 8 根。此時 end-n 會是負數，
 // 而 Array.slice(負數) 會被當成「從尾端算起」，畫面只剩最後兩根（踩過）。
 // 所以 from 一定要夾在 0 以上。
 const n=Math.max(8,Math.min(VIEW.n,all.length));
 const end=VIEW.end==null?all.length:Math.max(n,Math.min(VIEW.end,all.length));
 return {all:all, from:Math.max(0,end-n), to:end, n:n, live:VIEW.end==null};
}

/* 日期按鈕左邊的小月曆圖示：舊版只有一個貼在字尾的「▾」，看不出來那裡可以點 */
const CAL_ICON='<svg class="cal-i" width="13" height="13" viewBox="0 0 14 14" fill="none" '+
  'stroke="currentColor" stroke-width="1.3" stroke-linecap="round">'+
  '<rect x="1.4" y="2.6" width="11.2" height="10" rx="2"/>'+
  '<path d="M4.4 1.2v2.6M9.6 1.2v2.6M1.4 6h11.2"/></svg>';

/* ---------------- 資料軌（即時分頁與回顧分頁共用同一個元件） ----------------
   舊版即時是一行純文字、回顧是 7 格方塊，兩套；而且 9 個數字同字級同顏色，
   讀起來是一長串連續的字。現在統一成：標籤在上、數值在下、依語意分組。
   groups＝[[{k,v,cls,u,track,hot,muted},…],…]，一個內層陣列就是一組。
   track 只是把已經發生的數字畫成長度（位階、量能），
   ⛔ 不得加任何「強／弱／偏多」之類的評語或預測（CLAUDE.md 第一段）。 */
function railHTML(groups){
 return groups.filter(g=>g&&g.length).map(g=>
   '<div class="grp">'+g.map(it=>{
     let h='<div class="it'+(it.muted?' muted':'')+'"><div class="k">'+it.k+'</div>'+
       '<div class="v '+(it.cls||'')+'">'+it.v+(it.u?'<small>'+it.u+'</small>':'')+'</div>';
     if(it.track!=null) h+='<div class="track"><i class="'+(it.hot?'hot':'')+'" style="width:'+
       Math.max(3,Math.min(100,it.track)).toFixed(1)+'%"></i></div>';
     return h+'</div>';
   }).join('')+'</div>').join('');
}
/* 沒有即時報價（休市／收不到）或在看歷史日時的資料軌：
   那一天日盤的開高低收＋震幅＋收在區間＋跳空＋總量。資料本來就有，
   只是不是即時的 —— 舊版整排數字直接消失，看起來像壞掉。 */
function dayRail(BC,q,live){
 const all=(BC&&BC.bars)||[], dd=(BC&&BC.date)||'';
 let D=all.filter(b=>(!b.d||b.d===dd)&&b.t>='08:45'&&b.t<'14:00');
 if(!D.length) D=all;
 if(!D.length) return '';
 const o=D[0].o, hi=Math.max.apply(null,D.map(b=>b.h)), lo=Math.min.apply(null,D.map(b=>b.l));
 const c=D[D.length-1].c, vol=D.reduce((a,b)=>a+b.v,0);
 const pos=(c-lo)/Math.max(1,hi-lo);
 const ref=(BC&&BC.ref!=null)?BC.ref:null;
 return railHTML([
   [{k:'開',v:f(o)},{k:'高',v:f(hi),cls:'up'},{k:'低',v:f(lo),cls:'down'},{k:'收',v:f(c)}],
   [{k:'震幅',v:f(hi-lo)},{k:'收在區間',v:f(pos*100)+'%',track:pos*100},
    {k:'跳空',v:ref==null?'—':pm(o-ref)}],
   [{k:'總量',v:(vol/1000).toFixed(1),u:'k'},
    {k:'報價',v:live?(q==='closed'?'休市中':'收不到'):'歷史日',muted:true}]
 ]);
}
function chartSVG(s){
 const G=chartGeom(); if(!G) return null;
 const B=G.all.slice(G.from,G.to), P=s.position;
 // 【真實交易也要標在圖上】本來只有練習單有標記 —— 他 2026-09-02 說「真實交易也可以
 // 像練習一樣在 K 圖上顯示我哪裡進哪裡出嗎」。真實那幾筆在 s.real.trades（只有今天），
 // 所以**只有看今天的即時圖時才畫**，切到別天不可以把今天的單畫上去。
 // 形狀沿用練習那一套（三角形／菱形／持有區間），另外加一圈金色光環區分真假。
 const T0=(barsCache.trades)||[];
 const RT=(!viewDate&&s.real&&s.real.trades)?s.real.trades:[];
 const T=T0.concat(RT.filter(t=>t.entry!=null&&t.entry_time).map(t=>({
     time:String(t.entry_time).slice(0,5), entry:t.entry, exit:t.exit,
     dir:t.dir, _exit_time:t.exit==null?null:String(t.exit_time||''),
     _net:t.points==null?0:t.points, _real:true})));
 const live=!viewDate;
 const cname=(s.conn&&s.conn.contract_name)||'微台';
 const first=G.all[0], last=G.all[G.all.length-1];
 // 沒有即時報價時退回「最後一根 K 棒的收盤」，別把舊的成交價當現價用（Bug A）。
 const q=quoteState(s);
 const px = (live && q==='live' && s.chips && s.chips.price!=null) ? s.chips.price : last.c;
 // 漲跌基準＝上一個交易日的日盤收盤（跟看盤軟體一致）。含夜盤之後圖上第一根是
 // 昨晚 15:00，拿它當基準會變成「相對昨晚開盤」，跟大戶投上的數字對不起來。
 const ref=(barsCache&&barsCache.ref!=null)?barsCache.ref:first.o;
 const chg=px-ref, pct=chg/ref*100;

 // 價格軸只貼合看得到的那幾根
 let hi=Math.max(...B.map(b=>b.h)), lo=Math.min(...B.map(b=>b.l));
 const inView=t=>{ const i=idxAll(t); return i>=G.from&&i<G.to; };
 // ⛔ 【出場價可能是 null】練習單一定有出場價，**真實單不一定**（問不到成交價時會留白）。
 //    舊寫法 Math.min(lo, entry, null) 會把 null 當成 0 ⇒ **價格軸整個掉到 0**，
 //    K 棒被壓成畫面頂端一條線（2026-09-02 加真實標記時當場踩到，截圖才看見）。
 //    又是同一個形狀的坑：練習那邊一定有的欄位，真實那邊可能沒有。
 T.forEach(t=>{ if(!inView(t.time)) return;
   [t.entry,t.exit].forEach(v=>{ if(typeof v==='number'&&isFinite(v)){
     hi=Math.max(hi,v); lo=Math.min(lo,v); } }); });
 if(P&&live&&G.live){ hi=Math.max(hi,P.tp); lo=Math.min(lo,P.sl); }
 // 【真實部位也要畫在圖上】舊版 chartSVG 只畫 s.position（練習部位），
 // 真實部位在 s.real.position ⇒ **假單有標記、真單一條線都沒有**（提案 §3.H）。
 // 圖是兩個分頁共用的，所以站在練習分頁也看得到這三條線。
 const RQ=(s.real&&s.real.position&&s.real.tp!=null&&s.real.sl!=null)?s.real:null;
 if(RQ&&live&&G.live){ hi=Math.max(hi,RQ.tp,RQ.sl); lo=Math.min(lo,RQ.tp,RQ.sl); }
 const pad=(hi-lo)*0.08||10;
 // ⛔ 【價格軸要黏住，不可以每根新 K 棒就重算一次】2026-09-02 他回報「開盤時 K 線圖
 //    一直變來變去」。實測回放今天的資料：**08:45~09:30 之間軸變了 7 次、09:30 之後 0 次**。
 //    原因不是價格創新高低，是**視窗固定只顯示最後 60 根**：每多一根新的，
 //    左邊就掉一根舊的 ⇒ 昨晚夜盤的最高點被一根一根擠出畫面 ⇒ hi 一路縮
 //    （實測上緣 46962→46934→46868→46836→46815）⇒ **整張圖所有 K 棒重新定位**。
 //    最後那根還是「沒收完」的即時棒，高低點每個 tick 都在動，中間還會再抖。
 //    修法兩層：① 貼齊整數刻度（差幾點根本不會動到軸，順便讓刻度變成好讀的整數）；
 //             ② 遲滯 —— 舊軸只要還包得住新資料、而且沒有空太多，就沿用。
 //    ⚠️ 縮放／平移／換日要能重新貼合，所以那些會進 key。
 const axHi=hi+pad, axLo=lo-pad;
 const astep=niceStep((axHi-axLo)/6);
 let sHi=Math.ceil(axHi/astep)*astep, sLo=Math.floor(axLo/astep)*astep;
 const axKey=(barsCache&&barsCache.date||'')+'|'+VIEW.n+'|'+(VIEW.end==null?'live':VIEW.end);
 if(AXIS.key===axKey && AXIS.hi>=axHi && AXIS.lo<=axLo
    && (axHi-axLo)>=(AXIS.hi-AXIS.lo)*0.55){ sHi=AXIS.hi; sLo=AXIS.lo; }
 AXIS={key:axKey, hi:sHi, lo:sLo, step:astep};
 hi=sHi; lo=sLo;
 // 直向縮放／平移：以自動範圍的中心為基準伸縮，再整體上下位移
 { const mid=(hi+lo)/2+VIEW.voff, half=((hi-lo)/2)/VIEW.vz;
   hi=mid+half; lo=mid-half; }

 lastSpan=hi-lo;
 const W=1040,H=470,R=64,TOP=12,BOT=26;
 const VOLH=86, GAP=14;                 // 量能區高度、與價格區的間距
 const PB=H-BOT-VOLH-GAP;               // 價格區底部
 // K 棒寬度由「縮放倍率」決定，不是由「現有幾根」決定 ——
 // 否則開盤沒多久只有 6 根時，會被拉開成 6 支橫跨整個畫面的粗棒子。
 // 跟看盤軟體一樣：棒寬固定、不夠的部分右邊留白。
 const slots=Math.max(B.length, Math.min(VIEW.n, G.all.length) || B.length);
 // 再加間距上限：早盤只有幾根時，若讓它們平均攤滿整個畫面，
 // 會變成幾支孤零零的粗棒子橫跨全圖。夾住之後就是「盤在進行、右邊慢慢填滿」的樣子。
 const cw=Math.min((W-R)/slots, 40), bw=Math.max(1.5,Math.min(18,cw*0.62));
 const y=v=>TOP+(hi-v)/(hi-lo)*(PB-TOP);
 const vmax=Math.max(1,...B.map(b=>b.v));
 const vy=v=>H-BOT-(v/vmax)*VOLH;       // 量柱由下往上長
 const x=i=>i*cw+cw/2;
 const isNight=b=>b.t>='15:00'||b.t<'08:45';

 let g='';
 // 夜盤底色：一眼分得出哪一段是昨晚。15:00 之後或 08:45 之前都算夜盤。
 { let a=-1;
   const band=(p,q)=>'<rect x="'+(p*cw).toFixed(1)+'" y="'+TOP+'" width="'+((q+1-p)*cw).toFixed(1)+
     '" height="'+(H-TOP-BOT)+'" fill="#7C8CA8" opacity=".07"/>';
   B.forEach((bar,i)=>{ if(isNight(bar)){ if(a<0)a=i; } else if(a>=0){ g+=band(a,i-1); a=-1; } });
   if(a>=0) g+=band(a,B.length-1);
 }
 // 下單時段底色
 { let a=-1,b=-1;
   B.forEach((bar,i)=>{ if(bar.t>='08:45'&&bar.t<'09:30'){ if(a<0)a=i; b=i; } });
   if(a>=0) g+='<rect x="'+(a*cw).toFixed(1)+'" y="'+TOP+'" width="'+((b+1-a)*cw).toFixed(1)+
     '" height="'+(H-TOP-BOT)+'" fill="#E3A951" opacity=".05"/>';
 }
 g+='<rect x="'+(W-R)+'" y="0" width="'+R+'" height="'+H+'" fill="#1C222C" opacity=".45"/>';
 // 刻度線畫在 step 的整數倍上（軸已貼齊 step，線就會落在整數價位）。
 // ⚠️ 縮放／平移之後 hi/lo 被 vz/voff 改過、不再是 step 的倍數，
 //    所以從 lo 往上取第一個整數倍開始，不能假設 lo 本身就是；
 //    倍率拉很大時線會太多，那就退回原本的「切成 5 等分」。
 const gs=(AXIS.step>0 && (hi-lo)/AXIS.step<=12) ? AXIS.step : (hi-lo)/5;
 for(let v=Math.ceil(lo/gs)*gs; v<=hi+1e-9; v+=gs){
   const yy=y(v);
   g+='<line x1="0" y1="'+yy.toFixed(1)+'" x2="'+(W-R)+'" y2="'+yy.toFixed(1)+
      '" stroke="#232A35" stroke-width="1"/>'+
      '<text x="'+(W-R+8)+'" y="'+(yy+4).toFixed(1)+'" fill="#5C6472" font-size="12" '+
      'font-family="ui-monospace,monospace">'+v.toFixed(0)+'</text>';
 }
 // 分段線：夜盤→日盤（08:45 開盤）與跨午夜的地方。
 // 沒有這條線的話，22:00 跟 10:00 擠在同一張圖上會看不出斷在哪。
 B.forEach((b,i)=>{
   if(!i) return;
   const p=B[i-1], dayOpen=isNight(p)&&!isNight(b);
   const midnight=isNight(b)&&isNight(p)&&b.d!==p.d;
   if(!dayOpen&&!midnight) return;
   const X=i*cw;
   g+='<line x1="'+X.toFixed(1)+'" y1="'+TOP+'" x2="'+X.toFixed(1)+'" y2="'+(H-BOT)+
      '" stroke="#49536A" stroke-width="1" stroke-dasharray="2 4"/>'+
      '<text x="'+(X+4).toFixed(1)+'" y="'+(TOP+12)+'" fill="#6B7385" font-size="10.5" '+
      'font-family="ui-monospace,monospace">'+(dayOpen?'日盤':(b.d||'').slice(5))+'</text>';
 });
 B.forEach((b,i)=>{
   const up=b.c>=b.o, col=up?'#EE5A54':'#34B37E', X=x(i);
   g+='<line x1="'+X.toFixed(1)+'" y1="'+y(b.h).toFixed(1)+'" x2="'+X.toFixed(1)+
      '" y2="'+y(b.l).toFixed(1)+'" stroke="'+col+'" stroke-width="1"/>';
   const yo=y(b.o), yc=y(b.c), top=Math.min(yo,yc), hh=Math.max(1.2,Math.abs(yc-yo));
   g+='<rect x="'+(X-bw/2).toFixed(1)+'" y="'+top.toFixed(1)+'" width="'+bw.toFixed(1)+
      '" height="'+hh.toFixed(1)+'" fill="'+col+'"/>';
 });
 // ---- 成交量（同樣紅漲綠跌，跟 K 棒對齊）----
 g+='<line x1="0" y1="'+(H-BOT-VOLH-GAP/2).toFixed(1)+'" x2="'+(W-R)+
    '" y2="'+(H-BOT-VOLH-GAP/2).toFixed(1)+'" stroke="#232A35" stroke-width="1"/>';
 B.forEach((b,i)=>{
   const col=b.c>=b.o?'#EE5A54':'#34B37E', X=x(i), yy=vy(b.v);
   g+='<rect x="'+(X-bw/2).toFixed(1)+'" y="'+yy.toFixed(1)+'" width="'+bw.toFixed(1)+
      '" height="'+Math.max(0.8,H-BOT-yy).toFixed(1)+'" fill="'+col+'" opacity=".55"/>';
 });
 g+='<text x="'+(W-R+8)+'" y="'+(H-BOT-VOLH+10)+'" fill="#5C6472" font-size="11" '+
    'font-family="ui-monospace,monospace">'+(vmax>=10000?(vmax/1000).toFixed(0)+'k':vmax.toFixed(0))+'</text>'+
    '<text x="'+(W-R+8)+'" y="'+(H-BOT-2)+'" fill="#5C6472" font-size="11">量</text>';

 if(P&&live&&G.live){
   [[P.tp,'#EE5A54','停利'],[P.sl,'#34B37E','停損']].forEach(z=>{
     const yy=y(z[0]); if(yy<TOP||yy>PB) return;
     g+='<line x1="0" y1="'+yy.toFixed(1)+'" x2="'+(W-R)+'" y2="'+yy.toFixed(1)+
        '" stroke="'+z[1]+'" stroke-width="1.2" stroke-dasharray="5 4" opacity=".8"/>'+
        '<text x="6" y="'+(yy-5).toFixed(1)+'" fill="'+z[1]+'" font-size="11.5">'+z[2]+' '+z[0].toFixed(0)+'</text>';
   });
 }
 // 真實部位：進場（白）／停利（紅）／停損（綠）。虛線點法跟練習不同（2 5 vs 5 4），
 // 標籤帶底色掛在線的**下方** —— 練習的標籤在上方，兩組同時存在也不會疊在一起。
 if(RQ&&live&&G.live){
   [[RQ.position.entry,'#E9ECF1','真實進場'],[RQ.tp,'#EE5A54','真實停利'],
    [RQ.sl,'#34B37E','真實停損']].forEach(z=>{
     if(z[0]==null) return;
     const yy=y(z[0]); if(yy<TOP||yy>PB-16) return;
     const txt=z[2]+' '+z[0].toFixed(0);
     let w=12; for(let i=0;i<txt.length;i++) w+=(txt.charCodeAt(i)>255?10.5:6.3);
     g+='<line x1="0" y1="'+yy.toFixed(1)+'" x2="'+(W-R)+'" y2="'+yy.toFixed(1)+
        '" stroke="'+z[1]+'" stroke-width="1.4" stroke-dasharray="2 5" opacity=".95"/>'+
        '<rect x="4" y="'+(yy+2).toFixed(1)+'" width="'+w.toFixed(1)+'" height="15" rx="4" '+
        'fill="#0E1116" fill-opacity=".92" stroke="'+z[1]+'" stroke-opacity=".55"/>'+
        '<text x="'+(4+w/2).toFixed(1)+'" y="'+(yy+13).toFixed(1)+'" text-anchor="middle" fill="'+
        z[1]+'" font-size="10.5" font-weight="700">'+txt+'</text>';
   });
 }
 // ---- 進出場標記：圖區只留形狀，文字全部搬到本來就空著的兩條軌 ----
 // 右側 64px 的價格軸掛價位、底部時間軸帶掛時間與損益。
 // 他的單 5~15 分鐘就結束（±100 點只要 1~3 根 5 分 K），進出場在 x 軸上非常靠近 ——
 // 舊版兩塊描邊文字必然互相推擠，而且一定壓在那幾根關鍵 K 棒上
 // （Benson 2026-08-17 回報「時間標示有點擋路」）。
 const AXB=[], LANE=[], laneX=[];
 const tw=(str,fs)=>{ let w=0; for(let i=0;i<str.length;i++) w+=(str.charCodeAt(i)>255?1.0:0.6)*fs; return w; };
 // 右側價格軸掛牌：跟看盤軟體一樣，價位貼在軸上，圖區完全不動
 const axisChip=(aY,txt,col)=>{
   const h=17; let py=aY-h/2;
   for(let k=0;k<6;k++){
     py=Math.max(TOP,Math.min(PB-h,aY-h/2+(k%2?1:-1)*Math.ceil(k/2)*(h+2)));
     if(!AXB.some(b=>py<b+h&&b<py+h)) break;
   }
   AXB.push(py);
   return '<rect x="'+(W-R+1)+'" y="'+py.toFixed(1)+'" width="'+(R-2)+'" height="'+h+
     '" rx="4" fill="'+col+'"/>'+
     '<text x="'+(W-R+7)+'" y="'+(py+h-5).toFixed(1)+'" fill="#0E1116" font-size="11.5"'+
     ' font-weight="700" font-family="ui-monospace,monospace">'+txt+'</text>';
 };
 // 時間軸帶上的膠囊：彼此水平避讓（碰到就往右推），並夾在圖區內
 const lanePill=(X,txt,col)=>{
   const fs=10.5, w=tw(txt,fs)+13, h=17;
   let px=Math.max(1,Math.min(W-R-w-1,X-w/2));
   for(let k=0;k<8;k++){
     if(!LANE.some(b=>px<b.x+b.w+3&&b.x<px+w+3)) break;
     px=Math.min(W-R-w-1,px+w+5);
   }
   LANE.push({x:px,w:w});
   return '<rect x="'+px.toFixed(1)+'" y="'+(H-BOT+3)+'" width="'+w.toFixed(1)+'" height="'+h+
     '" rx="5" fill="#0E1116" fill-opacity=".92" stroke="'+col+'" stroke-opacity=".55"/>'+
     '<text x="'+(px+w/2).toFixed(1)+'" y="'+(H-BOT+15)+'" text-anchor="middle" fill="'+col+
     '" font-size="'+fs+'" font-weight="700" font-family="ui-monospace,monospace">'+txt+'</text>';
 };
 T.forEach(t=>{
   const ia=idxAll(t.time); if(ia<G.from||ia>=G.to) return;
   const i=ia-G.from, X=x(i), Y=y(t.entry), long=t.dir==='long', col=long?'#EE5A54':'#34B37E';
   const je=t._exit_time?idxAll(t._exit_time.slice(0,5)):-1;
   const hasExit=je>=G.from&&je<G.to;
   const XE=hasExit?x(je-G.from):null, YE=hasExit?y(t.exit):null;
   const ec=t._net>0?'#EE5A54':'#34B37E';
   if(hasExit){
     // 持有區間：底色 ＋ 進出場價的短虛線（只畫在區間內，不再橫貫全圖）＋ 連線
     const yTop=Math.min(Y,YE), yBot=Math.max(Y,YE);
     g+='<rect x="'+(X-cw/2).toFixed(1)+'" y="'+yTop.toFixed(1)+'" width="'+
        Math.max(cw,(XE-X)+cw).toFixed(1)+'" height="'+Math.max(2,yBot-yTop).toFixed(1)+
        '" fill="'+ec+'" opacity=".10"/>'+
        '<line x1="'+(X-cw/2).toFixed(1)+'" y1="'+Y.toFixed(1)+'" x2="'+(XE+cw/2).toFixed(1)+
        '" y2="'+Y.toFixed(1)+'" stroke="'+col+'" stroke-width="1.1" stroke-dasharray="4 3" opacity=".7"/>'+
        '<line x1="'+(X-cw/2).toFixed(1)+'" y1="'+YE.toFixed(1)+'" x2="'+(XE+cw/2).toFixed(1)+
        '" y2="'+YE.toFixed(1)+'" stroke="'+ec+'" stroke-width="1.1" stroke-dasharray="4 3" opacity=".7"/>'+
        '<line x1="'+X.toFixed(1)+'" y1="'+Y.toFixed(1)+'" x2="'+XE.toFixed(1)+'" y2="'+
        YE.toFixed(1)+'" stroke="'+ec+'" stroke-width="1.8" opacity=".9" stroke-linecap="round"/>';
   }
   // 引導線：從標記垂直落到時間軸帶，眼睛才接得起來（不壓 K 棒）
   g+='<line x1="'+X.toFixed(1)+'" y1="'+Y.toFixed(1)+'" x2="'+X.toFixed(1)+'" y2="'+(H-BOT)+
      '" stroke="'+col+'" stroke-width="1" stroke-dasharray="2 4" opacity=".32"/>';
   // 真實單多一圈金色光環：形狀跟練習一樣（他認得），但一眼分得出是真錢
   if(t._real) g+='<circle cx="'+X.toFixed(1)+'" cy="'+Y.toFixed(1)+
     '" r="9.5" fill="none" stroke="#E3A951" stroke-width="1.6" opacity=".85"/>';
   const tri=long?('M'+(X-7.5)+' '+(Y+16)+' L'+X+' '+(Y+3.5)+' L'+(X+7.5)+' '+(Y+16)+' Z')
                 :('M'+(X-7.5)+' '+(Y-16)+' L'+X+' '+(Y-3.5)+' L'+(X+7.5)+' '+(Y-16)+' Z');
   g+='<path d="'+tri+'" fill="'+col+'" stroke="#0E1116" stroke-width="1.8" stroke-linejoin="round"/>'+
      '<circle cx="'+X.toFixed(1)+'" cy="'+Y.toFixed(1)+'" r="2.6" fill="'+col+
      '" stroke="#0E1116" stroke-width="1.2"/>';
   g+=axisChip(Y,String(Math.round(t.entry)),col);
   // 進出場很近就把兩枚膠囊合併成一枚，不要互相推擠（他的單多半 5~15 分鐘就結束）
   const near=hasExit&&(XE-X)<110;
   const tag=t._real?'真 ':'';
   if(!near) laneX.push([X,tag+(long?'▲ 進 ':'▼ 進 ')+t.time,col]);
   else laneX.push([(X+XE)/2,tag+(long?'▲ ':'▼ ')+t.time+'→'+t._exit_time.slice(0,5)+
     '　'+pm(t._net),ec]);
   if(hasExit){
     g+='<line x1="'+XE.toFixed(1)+'" y1="'+YE.toFixed(1)+'" x2="'+XE.toFixed(1)+'" y2="'+(H-BOT)+
        '" stroke="'+ec+'" stroke-width="1" stroke-dasharray="2 4" opacity=".32"/>'+
        '<rect x="'+(XE-5.6).toFixed(1)+'" y="'+(YE-5.6).toFixed(1)+'" width="11.2" height="11.2"'+
        ' rx="2.4" transform="rotate(45 '+XE.toFixed(1)+' '+YE.toFixed(1)+')" fill="'+ec+
        '" stroke="#0E1116" stroke-width="1.8"/>';
     if(t._real) g+='<circle cx="'+XE.toFixed(1)+'" cy="'+YE.toFixed(1)+
       '" r="9.5" fill="none" stroke="#E3A951" stroke-width="1.6" opacity=".85"/>';
     g+=axisChip(YE,String(Math.round(t.exit)),ec);
     if(!near) laneX.push([XE,(t._real?'真 ':'')+'出 '+t._exit_time.slice(0,5)+
       '　'+pm(t._net),ec]);
   }
 });
 // ---- 游標所在那根：畫垂直參考線 ----
 let legendBar=B[B.length-1], legendIdx=G.to-1, hovering=false;
 if(HOVER.i!=null && HOVER.i>=G.from && HOVER.i<G.to){
   legendIdx=HOVER.i; legendBar=G.all[HOVER.i]; hovering=true;
   const X=x(HOVER.i-G.from);
   g+='<line x1="'+X.toFixed(1)+'" y1="'+TOP+'" x2="'+X.toFixed(1)+'" y2="'+(H-BOT)+
      '" stroke="#8D95A3" stroke-width="1" stroke-dasharray="3 3" opacity=".6"/>';
 }

 // 膠囊先算（lanePill 會把實際落點記進 LANE，互相避讓後位置才確定），
 // 時間刻度再依 LANE 的實際位置閃避 —— 用「原本想放的中心」去比會漏掉被推開的那幾枚。
 let pills='';
 laneX.forEach(p=>{ pills+=lanePill(p[0],p[1],p[2]); });
 // 時間刻度：依疏密自動決定間隔；壓到膠囊的就整個跳過，不要疊字
 const step=Math.max(1,Math.ceil(B.length/8));
 B.forEach((b,i)=>{ if(i%step) return;
   const X=x(i);
   if(LANE.some(z=>X+24>z.x-4&&X-24<z.x+z.w+4)) return;
   g+='<text x="'+X.toFixed(1)+'" y="'+(H-9)+'" fill="#5C6472" font-size="11.5" '+
      'text-anchor="middle" font-family="ui-monospace,monospace">'+b.t+'</text>';
 });
 g+=pills;                                            // 膠囊畫最後，壓在刻度上面

 const c=s.chips||{};
 // ---- 資料軌 ----
 // 標籤在上、數值在下，並依語意分組（動能／今天／盤口／現貨），中間有分隔線。
 // 位階與量能各給一條量尺 —— 只是把已發生的數字畫成長度，不做任何強弱評語。
 let rail='';
 if(live && q==='live'){
   const grp=[];
   const mom=[];
   if(c.mom5!=null) mom.push({k:'5 分',v:pm(c.mom5),cls:sgn(c.mom5)});
   if(c.mom15!=null) mom.push({k:'15 分',v:pm(c.mom15),cls:sgn(c.mom15)});
   if(mom.length) grp.push(mom);
   if(c.chg!=null) grp.push([{k:'跳空',v:pm(c.gap)},{k:'今日震幅',v:f(c.rng)},
     {k:'位階',v:f(c.pos*100)+'%',track:c.pos*100,hot:c.pos>0.8||c.pos<0.2},
     {k:'量能',v:f(c.vol_ratio,2),u:'倍',track:c.vol_ratio/3*100,hot:c.vol_ratio>1.5}]);
   if(c.bid!=null) grp.push([{k:'買 / 賣',v:f(c.bid)+' / '+f(c.ask)}]);
   // 加權指數與基差：現貨 09:00 才開盤、13:30 收，空窗期明講「未開盤」而不是消失
   // 加權要看得到「今天漲跌多少」，只給點數等於少一半資訊（Benson 2026-08-28 提的，
   // 說要跟大戶投一樣）。漲跌與百分比接在點數後面，紅漲綠跌跟全站一致。
   let idxv='<i>未開盤</i>';
   if(c.idx!=null){
     idxv=f(c.idx);
     if(c.idx_chg!=null)
       idxv+='<em class="d '+sgn(c.idx_chg)+'">'+pm(c.idx_chg,1)+
             (c.idx_pct==null?'':' ('+pm(c.idx_pct,2)+'%)')+'</em>';
   }
   const spot=[{k:'加權',v:idxv}];
   if(c.basis!=null) spot.push({k:'基差',v:pm(c.basis),cls:sgn(c.basis)});
   grp.push(spot);
   rail=railHTML(grp);
 } else {
   // 沒有即時報價（休市／收不到）或在看歷史日：資料本來就有，只是不是即時的 ——
   // 舊版整排數字直接消失，看起來像壞掉。改成顯示那一天日盤的開高低收。
   rail=dayRail(barsCache,q,live);
 }
 const pick=pickOpen?calHTML():'';
 // 報價區第三行：把舊版擠在 mini 列開頭那句「報價 休市中（上面是收盤價，非即時）」
 // 搬上來，跟昨收、合約、更新時間放在一起。
 // ⚠ 更新時間放在獨立的 <span id="cupd">：它每秒都在變，寫進 #chead 的字串裡
 //   會讓整個標頭（含翻頁列按鈕）每秒被重建一次 —— paintChart 會另外單獨更新它。
 const qs=live
   ? (q==='live'
      ? '<span class="live"><i></i>即時</span><span class="sep">·</span>'+
        '<span>昨收 '+f(ref)+'</span><span class="sep">·</span><span>'+cname+'</span>'+
        '<span class="sep">·</span><span id="cupd"></span>'
      : '<span class="live dead"><i></i>'+(q==='closed'?'休市中':'收不到報價')+'</span>'+
        '<span class="sep">·</span><span>昨收 '+f(ref)+'</span><span class="sep">·</span>'+
        '<span>'+(q==='closed'?'上面是收盤價，非即時'
                              :'可能是國定假日，也可能是連線問題')+'</span>')
   : '<span>歷史日</span><span class="sep">·</span><span>昨收 '+f(ref)+'</span>'+
     '<span class="sep">·</span><span>13:45 收盤</span>';
 return {
   svg:g, vb:'0 0 '+W+' '+H,
   upd:(live&&q==='live')?((s.clock||'')+' 更新'):'',
   head:'<div class="qblock"><div class="qmain">'+
        '<span class="cpx '+sgn(chg)+'">'+f(px)+'</span>'+
        '<span class="cchg '+sgn(chg)+'">'+pm(chg)+
        '<span class="pct">'+pm(pct,2)+'%</span></span></div>'+
        '<div class="qsub">'+qs+'</div></div>'+pagerHTML(T),
   pick:pick, rail:rail,
   legend:(function(b,hv){
     const up=b.c>=b.o, col=up?'#EE5A54':'#34B37E';
     const vol=b.v>=10000?(b.v/1000).toFixed(1)+'k':b.v.toFixed(0);
     // 跨夜的圖上光看 22:15 分不出是哪一天，所以連日期一起顯示
     return '<span class="lt">'+(b.d?b.d.slice(5)+' ':'')+b.t+'</span>'+
       '<span>開 <b>'+b.o.toFixed(0)+'</b></span>'+
       '<span>高 <b>'+b.h.toFixed(0)+'</b></span>'+
       '<span>低 <b>'+b.l.toFixed(0)+'</b></span>'+
       '<span>收 <b style="color:'+col+'">'+b.c.toFixed(0)+'</b></span>'+
       '<span>量 <b>'+vol+'</b></span>'+
       (hv?'':'<span class="lt">（最新）</span>');
   })(legendBar,hovering),
   info:B.length+' / '+G.all.length+' 根'+
        (Math.abs(VIEW.vz-1)>0.02?'　直向 '+VIEW.vz.toFixed(1)+'x':'')+
        ((G.live&&Math.abs(VIEW.vz-1)<=0.02&&Math.abs(VIEW.voff)<1)?'':'　雙擊還原')
 };
}

/* ---------------- 換日：翻頁列 ＋ 迷你月曆 ----------------
   /api/bars 會附一份 days：最近 70 個交易日，每天帶漲跌、震幅、練習結果。
   休市日（closed）不能選 —— 以前選得到，點下去是一張空白圖。 */
function dayList(){ return ((barsCache&&barsCache.days)||[]).filter(x=>!x.closed); }
function curDay(){ return viewDate||(barsCache&&barsCache.date)||today10(); }
function dayInfo(d){ return dayList().find(x=>x.d===d)||null; }

/* 翻頁列本體（◀ 日期 ▶ ＋ 今天／即時，下排是說明）。
   ⚠ 這一列跟「畫不畫得出 K 線」是兩件事，兩邊都要有。
   以前它只由 chartSVG 產出、跟著 #chead 塞進 K 線卡裡，於是「一根 K 棒都抓不到」時
   （本機 csv 沒那天、又還沒連上永豐）整張卡連同翻頁列與月曆被換成一張小數字卡 ——
   而「換到有資料的那天」正是那個狀態下唯一的自救路徑，使用者反而被鎖死在那一天
   （2026-08-25 視覺升級驗收時再次確認，另開單處理）。
   T＝那一天的練習交易（有 K 棒時 /api/bars 會一起帶回來）；
   傳 null＝沒有 K 棒可看，練習筆數改用日期索引裡的 n / net（跟月曆同一份資料）。 */
function pagerHTML(T){
 const cur=curDay(), me=dayInfo(cur);
 // 換日之後、新的 K 棒還沒回來的那一秒，barsCache 還是上一天的 ——
 // 這時候標籤若照常顯示，會把上一天的練習筆數掛在新日期底下（看起來像那天有下單）。
 // bd 是空的只發生在 /api/bars 整個出錯（連 date 都沒回），那時不可以判成「載入中」，
 // 否則日期會永遠停在灰色、r2 永遠寫「載入中…」。
 const bd=(barsCache&&barsCache.date)||'';
 const loading=barsPending||(!!bd&&cur!==bd);
 const dayS=cur.slice(5), noS=((barsCache&&barsCache.night_open)||'').slice(5);
 const n=T?T.length:((me&&me.n)||0);
 const net=T?T.reduce((a,t)=>a+t._net,0):((me&&me.net)||0);
 // 下排：純說明。即時那天也照樣寫日期（金點已經在講「現在」了，再寫「今天」是重複）；
 // 沒練習寫「未練習」而不是整段消失 —— 消失會讓上下兩行的位置跳動。
 // partial＝夜盤那一段還沒到齊（剛啟動、還沒連上永豐）。
 // ⛔ 這種時候後端**不會**再猜夜盤是哪一天了（猜錯會畫出前天的夜盤，他早上就是照這張圖
 //    決定要不要進場的），所以圖上只有當天日盤 —— 一定要講出來，不然他會以為圖是完整的。
 const partial=!!(barsCache&&barsCache.partial);
 // night＝夜盤還沒到；today＝**今天的 K 棒整個拿不到**，圖上會是昨晚（最容易被誤認成最新）
 const pwhat=(barsCache&&barsCache.partial_what)||'night';
 const r2=loading
   ? '<span>載入中…</span>'
   : (partial?'<span class="warn">'+(pwhat==='today'
        ? '今天的 K 棒還沒拿到，圖上是昨晚的夜盤'
        : '夜盤資料載入中，圖上只有今天日盤')+'</span>'+
              '<span class="sep">·</span>':'')+
     ((!partial&&noS&&noS!==dayS)
       ?'<span>含 '+noS+' 夜盤 15:00 起</span><span class="sep">·</span>':'')+
     '<span>'+(n
       // 負號用 U+2212 不用 hyphen，等寬字型下跟 + 對得齊（只改這裡，不動全域的 pm()）
       ? '練習 '+n+' 筆 <b class="'+sgn(net)+'">'+pm(net).replace('-','−')+'</b> 點'
       : '未練習')+'</span>'+
     // 這個分隔點跟著鍵盤提示一起藏（窄視窗會把提示收掉，只留一個孤零零的「·」很醜）
     '<span class="sep k">·</span>'+
     '<span class="kbdgrp"><kbd>←</kbd><kbd>→</kbd> 換日</span>';
 const nav=stepTarget(-1), fwd=stepTarget(1);
 return '<div class="pager">'+
   '<div class="r1">'+
   '<button class="nav-icon" data-nav="-1" title="前一個交易日（←）"'+
     (nav?'':' disabled')+'>◀</button>'+
   '<button class="dstamp'+(pickOpen?' open':'')+(loading?' loading':'')+
     '" data-pick="1" title="選日期（Esc 收合）">'+CAL_ICON+
     '<span class="num">'+dayS+'</span>'+
     // 這一格固定放星期，載入中不換字 —— 換成「載入中…」會讓日期鈕瞬間變寬約 33px，
     // 整條 r1 是靠右對齊的，◀ 會被往左推出滑鼠底下：連點 ◀ 時第 2 下就落在日期鈕上
     // （實測 250ms 節奏 4/5、60ms 節奏 2/5 生效，還誤開了月曆）。
     // 載入中仍然看得出來：.dstamp.loading 會把日期轉灰，下排 r2 也照樣寫「載入中…」。
     '<span class="wd">'+(me?me.w:'')+'</span>'+
     '<span class="caret">▼</span></button>'+
   '<button class="nav-icon" data-nav="1" title="後一個交易日（→）"'+
     (fwd?'':' disabled')+'>▶</button>'+
   (viewDate
     ?'<button class="jump2" data-day="" title="回到即時（Home）">今天</button>'
     :'<span class="livelamp" title="即時（Home）"><i></i>即時</span>')+
   '</div>'+
   '<div class="r2">'+r2+'</div>'+
   '</div>';
}

/* 往前／往後一個交易日；到底了回 null（箭頭就會變灰） */
function stepTarget(dir){
 const L=dayList(); if(!L.length) return null;
 let i=L.findIndex(x=>x.d===curDay());
 // 目前看的日子不在交易日清單裡 —— 只會發生在「今天不是交易日」（週末／國定假日）。
 // 這時候往前一步應該是「最後一個交易日」本身，不是再退一天：
 // 週日實測按 ◀ 會從 08-23 直接跳到 08-20，整個跳過上週五（Benson 週末最想看的那天）。
 if(i<0) return dir<0 ? L[L.length-1].d : null;
 const j=i+dir;
 return (j>=0&&j<L.length)?L[j].d:null;
}
function goDay(d){
 if(d==null) return;
 // 最後一天就是今天 → 回到即時模式（viewDate 空字串），而不是把今天當歷史日看
 const L=dayList();
 viewDate=(L.length&&d===L[L.length-1].d&&d===today10())?'':d;
 calMonth=d.slice(0,7);
 pickOpen=false; fetchBars(true); setTimeout(tick,250); tick();
}

/* 迷你月曆：只排週一到週五（週末沒有日盤），紅漲綠跌，底下細線是震幅 */
function calHTML(){
 const L=dayList(), ALL=(barsCache&&barsCache.days)||[];
 if(!ALL.length) return '';
 const months=[...new Set(ALL.map(x=>x.d.slice(0,7)))];
 if(months.indexOf(calMonth)<0) calMonth=curDay().slice(0,7);
 const mi=months.indexOf(calMonth);
 const y=+calMonth.slice(0,4), m=+calMonth.slice(5);
 const maxRng=Math.max(1,...ALL.map(x=>x.rng||0));
 const cur=curDay(), td=today10();
 const first=new Date(y,m-1,1), start=new Date(first);
 start.setDate(1-((first.getDay()+6)%7));          // 回到該週的週一
 let cells='';
 for(let k=0;k<42;k++){
   const dt=new Date(start); dt.setDate(start.getDate()+k);
   if(dt.getDay()===0||dt.getDay()===6) continue;   // 週末不排
   const num=dt.getDate();
   if(dt.getMonth()!==m-1){ cells+='<span class="cell off"></span>'; continue; }
   const iso=dt.getFullYear()+'-'+String(dt.getMonth()+1).padStart(2,'0')+
             '-'+String(num).padStart(2,'0');
   const x=ALL.find(v=>v.d===iso);
   if(!x||x.closed){                                // 休市或超出範圍 → 不能點
     cells+='<span class="cell off"><span class="dd">'+num+'</span></span>'; continue;
   }
   const c=['cell', x.pct==null?'na':(x.pct>0?'up':x.pct<0?'dn':'na')];
   if(x.n) c.push('prac');
   if(iso===cur) c.push('on');
   if(iso===td) c.push('today');
   cells+='<button class="'+c.join(' ')+'" data-day="'+iso+'">'+
     '<span class="dd">'+num+'</span>'+
     '<span class="pc">'+(x.pct==null?'—':pm(x.pct,1))+'</span>'+
     (x.rng?'<span class="rngbar" style="transform:scaleX('+
       (0.25+0.75*x.rng/maxRng).toFixed(2)+')"></span>':'')+'</button>';
 }
 return '<div class="calbox"><div class="calhead">'+
   '<span class="mo">'+y+' 年 '+m+' 月</span><span class="cnav">'+
   '<button data-mo="-1"'+(mi<=0?' disabled':'')+'>‹</button>'+
   '<button data-mo="1"'+(mi>=months.length-1?' disabled':'')+'>›</button>'+
   '</span></div><div class="cal">'+
   ['一','二','三','四','五'].map(w=>'<span class="wd">'+w+'</span>').join('')+
   cells+'</div><div class="callegend">'+
   '<span><i style="background:var(--up)"></i>收紅</span>'+
   '<span><i style="background:var(--down)"></i>收綠</span>'+
   '<span><i style="background:transparent;border:1px solid rgba(227,169,81,.55)"></i>有練習</span>'+
   '<span><i style="background:var(--gold);border-radius:50%"></i>今天</span>'+
   '<span>底下細線＝震幅</span></div></div>';
}

/* 交易時間（HH:MM）→ K 棒索引。
   含夜盤之後，圖上的時間不再是遞增的字串（…23:55, 00:00…, 08:45…），
   整條掃會在午夜那裡就停住。練習交易一定落在當天日盤，所以只在那一段找。 */
function idxAll(t){
 const all=(barsCache&&barsCache.bars)||[];
 const dd=(barsCache&&barsCache.date)||'';
 let r=-1;
 for(let i=0;i<all.length;i++){
   const b=all[i];
   if(b.d&&b.d!==dd) continue;      // 前一晚的夜盤
   if(b.t<'08:45') continue;        // 當天凌晨那段仍屬夜盤
   if(b.t<=t) r=i; else break;
 }
 return r;
}

/* K 線由左往右展開一次。用 class + 計時器拿掉，不留 clip-path 在元素上 ——
   留著的話之後每次重繪都被裁，圖會缺一角。 */
var kkWasBusy=false, kkTimer=null;
function kkDraw(){
 const w=document.querySelector('#mkt .cwrap'); if(!w) return;
 w.classList.remove('kk-draw');
 void w.offsetWidth;                 // 強制回流，動畫才會重播
 w.classList.add('kk-draw');
 clearTimeout(kkTimer);
 kkTimer=setTimeout(function(){ w.classList.remove('kk-draw'); },600);
}

/* 外框只建一次，之後只換 svg 內容 —— 重繪不會打斷你的縮放與拖曳 */
function paintChart(s){
 const d=chartSVG(s); if(!d) return false;
 if(!document.getElementById('csvg')){
   // #cpick 包在 .cheadwrap 裡：月曆是絕對定位的浮層，要錨在翻頁列正下方，
   // 定位基準必須是「標頭這一塊」而不是整張卡片（卡片是 position:relative）。
   // kk-in＝淡入蓋過骨架（骨架已經佔好一樣的位置，所以不需要再 rise 一次）
   document.getElementById('mkt').innerHTML='<div class="card chart l1 kk-in" id="cchart">'+
     '<div class="kk-prog"><i></i></div>'+
     '<div class="cheadwrap"><div class="chead" id="chead"></div>'+
     '<div class="calpop" id="cpick"></div></div>'+
     '<div class="legend" id="clegend"></div>'+
     '<div class="cwrap"><svg id="csvg" preserveAspectRatio="none"></svg></div>'+
     '<div class="chint"><span id="cinfo"></span>　滾輪縮放・拖曳平移・雙擊還原</div>'+
     '<div class="rail" id="crail"></div></div>';
   bindChart();
   kkDraw();                       // 第一次出現：K 線由左往右展開一次
 }
 // 換日／回到即時的等待期間：圖淡下去 ＋ 頂上跑一條細進度條。
 // 只切 class，不動 innerHTML —— 動 innerHTML 會打斷他的縮放與拖曳。
 { const card=document.getElementById('cchart');
   if(card){
     const busy=barsPending||curDay()!==((barsCache&&barsCache.date)||'')||barsLoading();
     if(busy!==card.classList.contains('kk-load')) card.classList.toggle('kk-load',busy);
     if(!busy&&kkWasBusy) kkDraw();     // 新的一天到齊了 → 再展開一次
     kkWasBusy=busy;
   }
 }
 // 只有內容真的變了才動 DOM —— 跟 setHTML() 同一套道理：tick 每 0.5 秒跑一次，
 // 使用者剛好在那一瞬間按下去，按鈕會連同事件一起被換掉 → 第一下沒反應。
 // ⚠️ 比對的是「上次自己設進去的那個字串」（快取在節點上的 __html／__at_xxx），
 //    絕對不可以讀回 e.innerHTML 來比。瀏覽器解析後再序列化的結果跟原字串不一樣：
 //    我們產的裸屬性 disabled（翻頁列 ◀▶ 的 (nav?'':' disabled')、月曆 ‹› 的
 //    (mi<=0?' disabled':'')）讀回來是 disabled=""，兩邊永遠不相等 ⇒ 守衛整個失效，
 //    月曆與翻頁列每秒被重建兩次（滑鼠 hover 的格子一直被抽掉、點下去剛好碰到重建就沒反應）。
 //    2026-08-21 實測：3 秒內 #cpick、#chead 各被整個換掉 4 次。
 //    把 disabled 改成輸出 disabled="" 只是治標 —— 屬性引號、HTML 實體、空白、屬性順序
 //    任何一個序列化差異都會再犯一次，所以一律比快取字串，不比 DOM 讀回值。
 const set=(id,html,attr)=>{ const e=document.getElementById(id); if(!e) return;
   const k=attr?'__at_'+attr:'__html';
   if(e[k]===html) return;
   e[k]=html;
   if(attr) e.setAttribute(attr,html); else e.innerHTML=html; };
 set('chead',d.head); set('clegend',d.legend);
 set('csvg',d.vb,'viewBox'); set('csvg',d.svg);
 set('cpick',d.pick); set('crail',d.rail); set('cinfo',d.info);
 // 「HH:MM:SS 更新」每秒都在變，所以它自己一個節點：塞進 d.head 的話整個標頭
 // （含翻頁列的 ◀ ▶ 按鈕）每秒被重建一次，滑鼠停在按鈕上剛好碰到就按不動。
 set('cupd',d.upd);
 return true;
}

function bindChart(){
 const sv=document.getElementById('csvg');
 const total=()=>((barsCache&&barsCache.bars)||[]).length;

 // 價格軸在圖的最右邊（SVG 座標 W-R 之後），換算成畫面比例
 const AXIS=64/1040;
 const onAxis=e=>{ const r=sv.getBoundingClientRect();
                   return (e.clientX-r.left)/r.width > 1-AXIS; };

 sv.addEventListener('wheel',function(e){
   e.preventDefault();
   // Shift＋滾輪、或游標在價格軸上 → 直向縮放
   if(e.shiftKey||onAxis(e)){
     VIEW.vz=Math.min(12,Math.max(0.25,VIEW.vz*(e.deltaY>0?0.88:1.14)));
     tick(); return;
   }
   const G=chartGeom(); if(!G) return;
   const r=sv.getBoundingClientRect();
   const frac=Math.min(1,Math.max(0,(e.clientX-r.left)/r.width));   // 游標在圖上的相對位置
   const anchor=G.from+frac*G.n;                                    // 以游標處那根為中心縮放
   const n=Math.round(Math.min(total(),Math.max(8,G.n*(e.deltaY>0?1.18:0.85))));
   let end=Math.round(anchor+(1-frac)*n);
   end=Math.max(n,Math.min(total(),end));
   VIEW.n=n; VIEW.end=(end>=total())?null:end;
   tick();
 },{passive:false});

 sv.addEventListener('mousedown',function(e){
   const G=chartGeom(); if(!G) return;
   const r=sv.getBoundingClientRect();
   DRAG={x:e.clientX, y:e.clientY, end:G.to, n:G.n, w:r.width, h:r.height,
         vz:VIEW.vz, voff:VIEW.voff, span:lastSpan,
         axis:onAxis(e)};                       // 在價格軸上按下 → 拖曳＝直向縮放
   sv.style.cursor=DRAG.axis?'ns-resize':'grabbing';
   e.preventDefault();
 });
 window.addEventListener('mousemove',function(e){
   if(!DRAG) return;
   if(DRAG.axis){
     // 往下拉＝壓縮（看更大範圍），往上拉＝放大
     const k=Math.exp(-(e.clientY-DRAG.y)/220);
     VIEW.vz=Math.min(12,Math.max(0.25,DRAG.vz*k));
     tick(); return;
   }
   const perBar=DRAG.w/DRAG.n;
   const moved=Math.round((e.clientX-DRAG.x)/perBar);
   let end=DRAG.end-moved;
   end=Math.max(DRAG.n,Math.min(total(),end));
   VIEW.end=(end>=total())?null:end;
   // 上下拖曳＝價格軸平移（換算成點數）
   if(DRAG.span>0) VIEW.voff=DRAG.voff+(e.clientY-DRAG.y)/DRAG.h*DRAG.span;
   tick();
 });
 window.addEventListener('mouseup',function(){
   if(!DRAG) return; DRAG=null; sv.style.cursor='';
 });
 // 游標移動 → 對到最近的那根 K 棒
 sv.addEventListener('mousemove',function(e){
   if(DRAG) return;
   const G=chartGeom(); if(!G) return;
   const r=sv.getBoundingClientRect();
   const frac=(e.clientX-r.left)/r.width;
   if(frac<0||frac>1-64/1040){ if(HOVER.i!=null){HOVER.i=null; tick();} return; }
   const i=G.from+Math.floor(frac/(1-64/1040)*G.n);
   const ni=Math.max(G.from,Math.min(G.to-1,i));
   if(ni!==HOVER.i){ HOVER.i=ni; tick(); }
 });
 sv.addEventListener('mouseleave',function(){
   if(HOVER.i!=null){ HOVER.i=null; tick(); }
 });

 sv.addEventListener('dblclick',function(e){
   // 在價格軸上雙擊＝只還原直向；在圖上雙擊＝全部還原
   if(onAxis(e)){ VIEW.vz=1; VIEW.voff=0; }
   else VIEW={n:60,end:null,vz:1,voff:0};
   tick();
 });
}
function cell(l,v,cls){return '<div class="cell"><div class="l">'+l+'</div><div class="v '+cls+'">'+v+'</div></div>';}

function row(t,ns){
 const rs={tp:'停利',sl:'停損',manual:'手動',close:'收盤'}[t._reason]||'';
 // App 匯入的那幾筆在 my_trades.json，面板不去改它 —— 有心得就顯示，但不給編輯，
 // 不然按下去只會得到「找不到那一筆紀錄」。
 const ro=t._source==='app';
 const nb=!ns?''
   :ro?(t.note?'<div class="noteline">「'+esc(t.note)+'」</div>':'')
   :noteBox(nkey(ns,t),t.note,nattr(t),
       ns==='t'?'＋ 寫下今天的心得':'＋ 補寫心得',
       ns==='t'?'今天的盤感、進出場理由、紀律有沒有守…'
               :'現在回頭看，這一筆做對了什麼、做錯了什麼？');
 return '<div class="trade '+(t._net>0?'win':'loss')+'"><div class="tr-top">'+
  '<span class="tr-date">'+(t.date?t.date.slice(5):'')+'</span>'+
  '<span class="dir '+(t.dir==='long'?'l':'s')+'">'+(t.dir==='long'?'▲ 多':'▼ 空')+'</span>'+
  '<span class="tr-px">'+t.entry+'<span class="arrow">→</span>'+t.exit+
  (rs?' <span class="tag">'+rs+'</span>':'')+(t._source==='app'?' <span class="tag">App</span>':'')+'</span>'+
  '<span class="tr-res '+(t._net>0?'r-win':'r-loss')+'">'+pm(t._net)+'</span></div>'+nb+'</div>';
}
function statsBox(ST){
 if(!ST||!ST.windows||!ST.windows.length) return '';
 let w=ST.windows.find(x=>x.n===WIN)||ST.windows.find(x=>x.label.indexOf(String(WIN))>=0);
 if(!w) w=ST.windows[ST.windows.length-1];
 let seg='<div class="seg">';
 ST.windows.forEach((x,i)=>{
   const k=parseInt(x.label.replace(/[^0-9]/g,''))||0;
   seg+='<button class="'+(x===w?'on':'')+'" data-win="'+k+'">'+x.label+'</button>';
 });
 seg+='</div>';
 const cls=w.total>0?'up':w.total<0?'down':'flat';
 // 勝率是「已經發生的統計」，用中性色；金色只留給「即時／現在」一個意思，
 // 紅綠讓給真正的結果（合計點數）。勝敗條讓比例一眼看得出來，不必讀數字。
 let h='<div class="n-sep"></div><div class="n-bd n-bd-t">'+
  '<div class="n-sh">練習成績<span class="c">共 '+ST.total+' 筆</span></div>'+seg+
  '<div class="score"><div class="rate"><span class="n">'+w.win_rate.toFixed(0)+
  '</span><span class="p">%</span><div class="lab">勝率</div></div>'+
  '<div class="sum"><div><span class="n '+cls+'">'+pm(w.total)+
  '</span><span class="u">點</span></div>'+
  '<div class="cash">'+(w.ntd<0?'-':'+')+'NT$'+Math.abs(w.ntd).toLocaleString()+'</div>'+
  '</div></div>'+
  '<div class="wlbar"><i class="w" style="flex:'+Math.max(w.wins,0.001)+'"></i>'+
  '<i class="l" style="flex:'+Math.max(w.losses,0.001)+'"></i></div>'+
  '<div class="wlfoot"><span class="w"><b>'+w.wins+'</b> 勝</span>'+
  '<span>'+w.n+' 筆</span><span class="l"><b>'+w.losses+'</b> 敗</span></div>';
 if(ST.recent&&ST.recent.length){
   h+='<div class="list">';
   ST.recent.forEach(t=>h+=row(t,'s'));
   h+='</div><a class="dl" href="/api/export" download>下載練習紀錄（可匯入 App）</a>';
 }
 return h+'</div>';
}
// 事件委派：掛在 document 上，就算某一區重繪也不會掉事件
document.addEventListener('click', function(e){
 if(TAB!=='live') return;          // 回顧分頁有自己的一套 data-act，別互相搶
 const b=e.target.closest('[data-act]');
 if(!b||b.disabled) return;
 const a=b.getAttribute('data-act');
 const url=(a==='long'||a==='short')?'/api/enter':'/api/'+a;
 const body=(a==='long'||a==='short')?JSON.stringify({dir:a}):'{}';
 b.disabled=true;
 pfetch(url,body)
  .then(r=>r.json())
  .then(r=>{ if(!r.ok&&r.msg) alert(r.msg); statsAt=0; tick(); })
  .catch(()=>{})
  .then(()=>{ b.disabled=false; });
});
/* 心得的展開／儲存／取消：即時與回顧兩個分頁共用 */
document.addEventListener('input', function(e){
 if(e.target&&e.target.id==='tnote') NOTE.text=e.target.value;
});
document.addEventListener('click', function(e){
 const ed=e.target.closest('[data-nedit]');
 if(ed){
   NOTE={key:ed.getAttribute('data-nedit'), text:ed.getAttribute('data-note')||'',
         date:ed.getAttribute('data-nd')||'', time:ed.getAttribute('data-nt')||'',
         entry:ed.getAttribute('data-ne')||'', open:ed.hasAttribute('data-nopen'),
         kind:ed.getAttribute('data-nkind')||''};
   nrepaint(); return;
 }
 const sv=e.target.closest('[data-nsave]');
 if(sv){
   const el=document.getElementById('tnote'), txt=el?el.value:NOTE.text;
   const b=NOTE.open?{open:true,text:txt}
     :{date:NOTE.date,time:NOTE.time,entry:Number(NOTE.entry),text:txt,
       // 真實交易的心得存進 real_trades/，不走練習那條同步鏈
       kind:(NOTE.kind==='real'?'real':undefined)};
   sv.disabled=true;
   pfetch('/api/note',JSON.stringify(b))
    .then(r=>r.json())
    .then(r=>{
      if(!r.ok){ sv.disabled=false; alert(r.msg||'存不起來'); return; }
      // 回顧分頁的 RV 是進分頁時抓一次的快取，不順手更新的話畫面會停在舊的字
      if(RV&&RV.trades) RV.trades.forEach(function(x){
        if(x._source!=='app' && x.date===NOTE.date &&
           String(x.time||'').slice(0,5)===NOTE.time &&
           Math.round(x.entry)===Number(NOTE.entry)) x.note=txt;
      });
      NOTE={key:null,text:''}; statsAt=0; nrepaint();
    })
    .catch(()=>{ sv.disabled=false; alert('存不起來，面板可能剛好在重啟'); });
   return;
 }
 if(e.target.closest('[data-ncancel]')){ NOTE={key:null,text:''}; nrepaint(); return; }
});
/* 真實下單：開關與平倉用 click，送單用長按（mousedown/up） */
document.addEventListener('click', function(e){
 if(TAB!=='live') return;
 // 切分頁：不自動切是老闆拍板的（A 案），所以只有他自己點才會換
 const rtb=e.target.closest('[data-rtab]');
 if(rtb){ setRTab(rtb.getAttribute('data-rtab')); return; }
 // 跨分頁警報上的「去看部位 →」
 if(e.target.closest('[data-rgo]')){ setRTab('real'); return; }
 if(e.target.closest('[data-rt]')){ realToggle(); return; }
 if(e.target.closest('[data-rclose]')){
   // 【平倉不跳確認】要平倉的時候通常是急的，多一個對話框是在最糟的時機加摩擦。
   // 而且平倉是「安全方向」——誤按的代價是一點滑價，不是無上限的虧損（跟進場相反）。
   realClose();
   return; }
});
document.addEventListener('mousedown', function(e){
 // 【只認左鍵】Windows 的右鍵選單是放開才跳出來的，所以按住右鍵 650ms 也會送單
 //（lab-qa 退件第 7 條）。中鍵同理。
 if(e.button!==0) return;
 const b=e.target.closest('[data-rdir]');
 if(b&&!b.disabled) holdStart(b,b.getAttribute('data-rdir'));
});
// 切走視窗（Alt+Tab、鎖螢幕）也算放開 —— 手離開鍵鼠了就不該繼續倒數
window.addEventListener('blur',function(){
 document.querySelectorAll('[data-rdir].holding').forEach(holdEnd);
});

document.addEventListener('click', function(e){
 if(TAB!=='live') return;
 if(e.target.closest('[data-pick]')){
   pickOpen=!pickOpen; if(pickOpen) calMonth=curDay().slice(0,7); tick(); return; }
 // 順序有關係：翻頁箭頭與換月按鈕要先攔，它們跟日期按鈕都在同一塊裡
 const nv=e.target.closest('[data-nav]');
 if(nv){ goDay(stepTarget(+nv.getAttribute('data-nav'))); return; }
 const mo=e.target.closest('[data-mo]');
 if(mo){
   const ms=[...new Set(((barsCache&&barsCache.days)||[]).map(x=>x.d.slice(0,7)))];
   const i=ms.indexOf(calMonth)+(+mo.getAttribute('data-mo'));
   if(i>=0&&i<ms.length){ calMonth=ms[i]; tick(); }
   return; }
 const d=e.target.closest('[data-day]');
 if(d){
   const v=d.getAttribute('data-day');
   if(v===''){ viewDate=''; pickOpen=false; fetchBars(true); tick(); setTimeout(tick,250); }
   else goDay(v);
   return; }
 // 真實成績的分段窗口。⚠️ 用自己的 RWIN，不要跟練習的 WIN 共用（見宣告處註解）。
 // ⛔ 【按下去就要生效，正在打心得時也一樣】只換 #realscore 這一個節點 ——
 //    整塊 #realstats 重畫會把 .list 裡正在編輯的 textarea 換掉，所以重繪守衛會擋，
 //    於是變成「按了畫面完全不動、關掉編輯器才突然跳」（QA 2026-09-03 退件）。
 //    這裡直接就地更新，不必等 tick()；再叫一次 tick() 讓其他區塊跟上。
 const rw=e.target.closest('[data-rwin]');
 if(rw){
   RWIN=parseInt(rw.getAttribute('data-rwin'))||0; pickOpen=false;
   const R=(LASTS&&LASTS.real)||{};
   setEl('realscore', realScore(R.trades_all||[]));
   tick(); return;
 }
 const b=e.target.closest('[data-win]');
 if(!b){
   // 月曆現在浮在圖上面（以前掛在圖下面，蓋不到東西）——
   // 點到圖或其他地方就要收掉，不然它會一直擋著 K 線。點月曆自己（換月）不算。
   if(pickOpen&&!e.target.closest('.calbox')){ pickOpen=false; tick(); }
   return; }
 WIN=parseInt(b.getAttribute('data-win'))||0;
 lastStats=''; pickOpen=false; tick();
});
/* ============================================================================
   【回顧】分頁
   ----------------------------------------------------------------------------
   兩個目的：翻自己的紀錄（看當時的盤面與 MFE/MAE），以及 Bar Replay
   —— 把後面的 K 棒蓋住、在不知道結果的狀態下練習判斷，之後才揭曉並跟當天實際的決定對照。

   【紅線】這一頁只顯示已經發生的客觀數字：不預測、不算勝率、不給買賣建議。
   重播的成績只記次數（停利幾次／停損幾次／與當天同向幾次），不換算成百分比 ——
   那會被讀成「我這套有 X% 勝率」，但 08:45~09:30 已經被走查驗證證明沒有統計優勢。
   ============================================================================ */
var TAB='live', MODE='review', FILTER='all', SEL=null, TF=5;
var RV=null;                 // /api/review：全部紀錄 + 日期清單 + 重播累計
var RB={};                   // K 棒快取：'日期|週期' → {bars,feats}
var RVIEW={n:60,end:null,vz:1,voff:0};
var RHOVER={i:null}, RDRAG=null, FOCUSPEND=false, lastPane='';
var RP={date:null,state:'idle',rev:0,speed:1,timer:null,end:null,
        judge:null,note:'',result:null,axis:null,n:48};
var TALLY={n:0,tp:0,sl:0,same:0};
const SPEEDS=[[0.5,1200],[1,600],[2,300],[4,150]];
const FUT=10;                // 重播時右邊固定留幾格空白
const RFEE=5, RTP=100;       // 跟練習下單同一把尺：±100 點、來回 5 點成本
const REASON={tp:'停利',sl:'停損',manual:'手動',close:'收盤'};
/* 進場動畫只在開站後的頭 1.1 秒有效。過了就把 class 拿掉 ——
   不然之後每次卡片內容變動（下單、成績更新）都會整張再飛一次。 */
document.body.classList.add('boot');
setTimeout(function(){ document.body.classList.remove('boot'); }, 1100);

const esc=s=>String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;')
  .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const mmin=t=>parseInt(t.slice(0,2))*60+parseInt(t.slice(3,5));
const RW=1040,RH=430,RR=64,RTOP=10,RBOT=24,RVOLH=64,RGAP=12,RPB=RH-RBOT-RVOLH-RGAP;

function rset(id,html){ const e=document.getElementById(id);
  if(e&&e.innerHTML!==html) e.innerHTML=html; }
function idxAt(bars,t){ let r=-1;
  for(let i=0;i<bars.length;i++){ if(bars[i].t<=t) r=i; else break; } return r; }
const today10=()=>new Date(Date.now()-new Date().getTimezoneOffset()*60000)
  .toISOString().slice(0,10);

/* ---------------- 資料 ---------------- */
function rvFetch(){
 fetch('/api/review').then(r=>r.json()).then(x=>{
   RV=x; TALLY=x.tally||TALLY;
   if(SEL==null&&x.trades&&x.trades.length) SEL=x.trades.length-1;
   if(!RP.date) RP.date=(x.trades&&x.trades.length?x.trades[x.trades.length-1].date
                          :((x.days||[])[0]||null));
   focusTrade(); rvRender();
 }).catch(()=>{});
}
/* 沒抓過就去抓，抓回來再重畫。回傳 null＝還在載入 */
function rvBars(day,tf){
 if(!day) return null;
 const k=day+'|'+tf, c=RB[k];
 if(c&&!c.loading){
   // 今天的 K 棒還在長，隔一段時間補抓一次（過去的日子不會變）
   if(day===today10()&&Date.now()-c.at>20000&&!c.busy){ c.busy=true;
     fetch('/api/bars?date='+day+'&tf='+tf).then(r=>r.json()).then(x=>{
       RB[k]={loading:false,at:Date.now(),bars:x.bars||[],feats:x.feats||null,error:x.error||null};
       rvRender(); }).catch(()=>{ c.busy=false; }); }
   return c;
 }
 if(c) return null;
 RB[k]={loading:true};
 fetch('/api/bars?date='+day+'&tf='+tf).then(r=>r.json()).then(x=>{
   RB[k]={loading:false,at:Date.now(),bars:x.bars||[],feats:x.feats||null,error:x.error||null};
   if(FOCUSPEND) focusTrade();
   rvRender();
 }).catch(()=>{ RB[k]={loading:false,at:Date.now(),bars:[],feats:null,error:'讀取失敗'};
   rvRender(); });
 return null;
}
function rvList(){
 const T=(RV&&RV.trades)||[];
 return T.map((t,i)=>({t:t,i:i})).filter(x=>{
   if(FILTER==='win') return x.t._net>0;
   if(FILTER==='loss') return x.t._net<=0;
   if(FILTER==='long') return x.t.dir==='long';
   if(FILTER==='short') return x.t.dir==='short';
   return true;
 }).reverse();                                  // 新的排在上面
}
function selTrade(){ const L=rvList(); if(!L.length) return null;
  return L.find(x=>x.i===SEL)||L[0]; }
/* 當天實際那一筆（App 匯入的沒有出場時間，不列入重播對照） */
function dayTrade(day){ const T=(RV&&RV.trades)||[];
  return T.find(t=>t.date===day&&t._source!=='app')||null; }
function dayTrades(day){ const T=(RV&&RV.trades)||[];
  return T.map((t,i)=>({t:t,i:i})).filter(x=>x.t.date===day); }
function rvCtx(){
 if(MODE==='replay') return {day:RP.date,tf:1};
 const s=selTrade();
 return {day:s?s.t.date:((RV&&RV.days||[])[0]||null), tf:TF};
}
/* 把畫面對準目前選到的那一筆（不是顯示整天） */
function focusTrade(){
 const s=selTrade(); if(!s){ RVIEW={n:60,end:null,vz:1,voff:0}; return; }
 const D=RB[s.t.date+'|'+TF];
 if(!D||D.loading||!D.bars||!D.bars.length){ FOCUSPEND=true; return; }
 FOCUSPEND=false;
 const all=D.bars, ei=Math.max(0,idxAt(all,s.t.time));
 const xt=s.t._exit_time?String(s.t._exit_time).slice(0,5):null;
 const xi=xt?Math.max(ei,idxAt(all,xt)):ei;
 const span=Math.max(TF===1?45:12,(xi-ei)*2+(TF===1?30:8));
 RVIEW={n:Math.min(all.length,Math.round(span)),
        end:Math.min(all.length,xi+Math.round(span*0.35)),vz:1,voff:0};
}

/* ---------------- K 線圖 ---------------- */
function rvGeom(all){
 if(MODE==='replay'){
   const n=Math.max(16,Math.min(RP.n,all.length));
   // RP.end＝null 時視窗跟著揭曉進度走；揭曉後才固定視窗（對準判斷的那一段）
   const end=Math.min(all.length,RP.end!=null?RP.end:RP.rev+1+FUT);
   return {from:Math.max(0,end-n),to:end,n:n,rev:Math.min(RP.rev,all.length-1)};
 }
 const n=Math.max(8,Math.min(RVIEW.n,all.length));
 const end=RVIEW.end==null?all.length:Math.max(n,Math.min(RVIEW.end,all.length));
 return {from:Math.max(0,end-n),to:end,n:n,rev:all.length-1};
}
function rvBlank(msg,loading){
 const sv=document.getElementById('rsvg');
 sv.setAttribute('viewBox','0 0 '+RW+' '+RH);
 let g='<rect x="0" y="0" width="'+RW+'" height="'+RH+'" fill="#151A22"/>';
 if(loading){
   // 換一天要 0.4~1 秒（伺服器每次都要重篩 54 萬列）。單一行「載入中…」看起來像當掉，
   // 所以先畫出格線與呼吸中的假 K 棒 —— 跟即時分頁的骨架同一套語言。
   for(let k=1;k<5;k++){ const y=RTOP+(RPB-RTOP)*k/5;
     g+='<line x1="0" y1="'+y.toFixed(1)+'" x2="'+(RW-RR)+'" y2="'+y.toFixed(1)+
        '" stroke="#232A35" stroke-width="1"/>'; }
   const H=[38,52,44,61,55,70,64,48,57,72,66,80,74,59,68,52,63,47,58,66,51,71,60,45];
   const cw=(RW-RR)/H.length;
   H.forEach(function(h,i){
     const bh=(RPB-RTOP)*h/140, y=RTOP+(RPB-RTOP)*0.5-bh/2;
     g+='<rect x="'+(i*cw+cw*0.22).toFixed(1)+'" y="'+y.toFixed(1)+'" width="'+(cw*0.56).toFixed(1)+
        '" height="'+bh.toFixed(1)+'" rx="2" fill="#1C222C">'+
        '<animate attributeName="opacity" values="0.35;0.7;0.35" dur="1.6s" begin="'+
        (i*0.045).toFixed(2)+'s" repeatCount="indefinite"/></rect>';
   });
 }
 g+='<text x="'+(RW/2)+'" y="'+(loading?RPB+34:RH/2)+'" fill="#5C6472" font-size="'+
    (loading?13:16)+'" text-anchor="middle">'+msg+'</text>';
 sv.innerHTML=g;
 rset('rlegend','');
}
function rvDraw(C,D){
 if(!C.day){ rvBlank('還沒有任何練習紀錄'); rset('rhead',''); return; }
 if(!D){ rvBlank('載入中…',true); return; }
 const all=D.bars||[];
 if(!all.length){ rvBlank(D.error?('這天讀不到 K 棒：'+D.error):'這天沒有本機資料');
   rset('rhead','<div><span class="cday">'+C.day+'</span></div>'); return; }
 const G=rvGeom(all), B=all.slice(G.from,G.to);
 const vis=B.filter((b,i)=>G.from+i<=G.rev);        // 重播時只有揭曉過的才算數
 if(!vis.length){ rvBlank('還沒開始'); return; }
 const sel=MODE==='review'?selTrade():null;
 const T=sel?sel.t:null;

 /* ---- 價格軸 ---- */
 let hi,lo;
 if(MODE==='replay'){
   let h=Math.max.apply(null,vis.map(b=>b.h)), l=Math.min.apply(null,vis.map(b=>b.l));
   if(RP.judge){ h=Math.max(h,RP.judge.tp); l=Math.min(l,RP.judge.sl); }
   const p0=Math.max(40,(h-l)*0.28); h+=p0; l-=p0;
   // 【最容易洩漏答案的地方】價格軸只能用已揭曉的 K 棒算，而且只擴不縮：
   // 照整天高低點定軸的話，光看軸有多寬就知道等一下會走多大。
   if(RP.axis){ hi=Math.max(RP.axis.hi,h); lo=Math.min(RP.axis.lo,l); } else { hi=h; lo=l; }
   RP.axis={hi:hi,lo:lo};
 } else {
   hi=Math.max.apply(null,B.map(b=>b.h)); lo=Math.min.apply(null,B.map(b=>b.l));
   dayTrades(C.day).forEach(x=>{ const i=idxAt(all,x.t.time);
     if(i>=G.from&&i<G.to){ hi=Math.max(hi,x.t.entry); lo=Math.min(lo,x.t.entry); } });
   if(T){ const ei=idxAt(all,T.time);
     // 選到的那一筆在畫面內時，價格軸要容得下它的停利／停損線
     if(ei>=G.from&&ei<G.to){ hi=Math.max(hi,T.entry+110); lo=Math.min(lo,T.entry-110); } }
   const p=(hi-lo)*0.08||10; hi+=p; lo-=p;
   const mid=(hi+lo)/2+RVIEW.voff, half=((hi-lo)/2)/RVIEW.vz; hi=mid+half; lo=mid-half;
 }
 const y=v=>RTOP+(hi-v)/(hi-lo)*(RPB-RTOP);
 const cw=(RW-RR)/B.length, bw=Math.max(1.5,Math.min(16,cw*0.62));
 const x=i=>i*cw+cw/2, gi=i=>i-G.from;
 const vmax=Math.max.apply(null,[1].concat(vis.map(b=>b.v)));
 const vy=v=>RH-RBOT-(v/vmax)*RVOLH;

 let g='<defs><pattern id="hatch" width="9" height="9" patternUnits="userSpaceOnUse" '+
   'patternTransform="rotate(45)"><rect width="9" height="9" fill="#141922"/>'+
   '<line x1="0" y1="0" x2="0" y2="9" stroke="#1D2430" stroke-width="4"/></pattern></defs>';

 /* 下單時段 08:45~09:30 底色 */
 { let a=-1,b=-1; B.forEach((bar,i)=>{ if(bar.t>='08:45'&&bar.t<'09:30'){ if(a<0)a=i; b=i; } });
   if(a>=0) g+='<rect x="'+(a*cw).toFixed(1)+'" y="'+RTOP+'" width="'+((b+1-a)*cw).toFixed(1)+
     '" height="'+(RH-RTOP-RBOT)+'" fill="#E3A951" opacity=".05"/>'; }

 /* 持倉區間著色（賺紅賠綠）＋整條時間帶淡白底 —— 畫在 K 棒底下 */
 if(T&&T._exit_time){
   const a=gi(idxAt(all,T.time)), b=gi(idxAt(all,String(T._exit_time).slice(0,5)));
   if(b>=0&&a<B.length){
     const x0=Math.max(0,a*cw), x1=Math.min(RW-RR,(b+1)*cw);
     const col=T._net>0?'#EE5A54':'#34B37E', yA=y(T.entry), yB=y(T.exit);
     g+='<rect x="'+x0.toFixed(1)+'" y="'+Math.min(yA,yB).toFixed(1)+'" width="'+(x1-x0).toFixed(1)+
        '" height="'+Math.abs(yB-yA).toFixed(1)+'" fill="'+col+'" opacity=".16"/>'+
        '<rect x="'+x0.toFixed(1)+'" y="'+RTOP+'" width="'+(x1-x0).toFixed(1)+'" height="'+(RPB-RTOP)+
        '" fill="#E9ECF1" opacity=".025"/>';
   }
 }

 /* 價格格線 */
 g+='<rect x="'+(RW-RR)+'" y="0" width="'+RR+'" height="'+RH+'" fill="#1C222C" opacity=".45"/>';
 for(let k=0;k<=5;k++){
   const v=lo+(hi-lo)*k/5, yy=y(v);
   g+='<line x1="0" y1="'+yy.toFixed(1)+'" x2="'+(RW-RR)+'" y2="'+yy.toFixed(1)+
      '" stroke="#232A35" stroke-width="1"/><text x="'+(RW-RR+8)+'" y="'+(yy+4).toFixed(1)+
      '" fill="#5C6472" font-size="12" font-family="ui-monospace,monospace">'+v.toFixed(0)+'</text>';
 }

 /* 停利／停損線 */
 const lines=[];
 if(T){ const d=T.dir==='long'?1:-1;
   lines.push([T.entry+d*RTP,'#EE5A54','停利'],[T.entry-d*RTP,'#34B37E','停損']); }
 if(MODE==='replay'&&RP.judge) lines.push([RP.judge.tp,'#EE5A54','停利'],[RP.judge.sl,'#34B37E','停損']);
 lines.forEach(z=>{ const yy=y(z[0]); if(yy<RTOP||yy>RPB) return;
   g+='<line x1="0" y1="'+yy.toFixed(1)+'" x2="'+(RW-RR)+'" y2="'+yy.toFixed(1)+
      '" stroke="'+z[1]+'" stroke-width="1.2" stroke-dasharray="5 4" opacity=".75"/>'+
      '<text x="6" y="'+(yy-5).toFixed(1)+'" fill="'+z[1]+'" font-size="12">'+z[2]+' '+z[0].toFixed(0)+'</text>'; });

 /* K 棒（重播時只畫揭曉過的） */
 B.forEach((b,i)=>{
   if(G.from+i>G.rev) return;
   const up=b.c>=b.o, col=up?'#EE5A54':'#34B37E', X=x(i);
   g+='<line x1="'+X.toFixed(1)+'" y1="'+y(b.h).toFixed(1)+'" x2="'+X.toFixed(1)+'" y2="'+
      y(b.l).toFixed(1)+'" stroke="'+col+'" stroke-width="1"/>';
   const yo=y(b.o),yc=y(b.c),tp=Math.min(yo,yc),hh=Math.max(1.2,Math.abs(yc-yo));
   g+='<rect x="'+(X-bw/2).toFixed(1)+'" y="'+tp.toFixed(1)+'" width="'+bw.toFixed(1)+
      '" height="'+hh.toFixed(1)+'" fill="'+col+'"/>';
 });

 /* 未揭曉區 */
 if(MODE==='replay'&&G.rev<G.to-1){
   const x0=(gi(G.rev)+0.5)*cw+cw*0.2;
   g+='<rect x="'+x0.toFixed(1)+'" y="'+RTOP+'" width="'+(RW-RR-x0).toFixed(1)+'" height="'+
      (RH-RTOP-RBOT)+'" fill="url(#hatch)" opacity=".85"/>'+
      '<text x="'+(x0+(RW-RR-x0)/2).toFixed(1)+'" y="'+(RTOP+26)+'" fill="#5C6472" font-size="12.5" '+
      'text-anchor="middle">後面還沒揭曉</text>';
 }

 /* 成交量 */
 g+='<line x1="0" y1="'+(RH-RBOT-RVOLH-RGAP/2).toFixed(1)+'" x2="'+(RW-RR)+'" y2="'+
    (RH-RBOT-RVOLH-RGAP/2).toFixed(1)+'" stroke="#232A35" stroke-width="1"/>';
 B.forEach((b,i)=>{ if(G.from+i>G.rev) return;
   const col=b.c>=b.o?'#EE5A54':'#34B37E', X=x(i), yy=vy(b.v);
   g+='<rect x="'+(X-bw/2).toFixed(1)+'" y="'+yy.toFixed(1)+'" width="'+bw.toFixed(1)+
      '" height="'+Math.max(0.8,RH-RBOT-yy).toFixed(1)+'" fill="'+col+'" opacity=".5"/>'; });
 g+='<text x="'+(RW-RR+8)+'" y="'+(RH-RBOT-RVOLH+10)+'" fill="#5C6472" font-size="11" '+
    'font-family="ui-monospace,monospace">'+(vmax>=10000?(vmax/1000).toFixed(0)+'k':vmax.toFixed(0))+'</text>';

 /* 09:30 下單時段結束 */
 { const i=B.findIndex(b=>b.t>='09:30');
   if(i>0){ const X=(i*cw).toFixed(1);
     g+='<line x1="'+X+'" y1="'+RTOP+'" x2="'+X+'" y2="'+(RH-RBOT)+'" stroke="#E3A951" '+
        'stroke-width="1" stroke-dasharray="2 5" opacity=".5"/>'+
        '<text x="'+(+X+5)+'" y="'+(RH-RBOT-6)+'" fill="#5C6472" font-size="10.5">09:30</text>'; } }

 /* ---- 進出場標記（跟即時分頁同一套）--------------------------------------
    圖區只留形狀（三角形＝進場、菱形＝出場、中間一條連線與淡色持有區間），
    所有文字搬到本來就空著的兩條軌：右側價格軸掛價位、底部時間軸帶掛時間與損益。
    Benson 2026-08-17 回報「時間標示有點擋路」—— 他的單 5~15 分鐘就結束，
    進出場在 x 軸上非常近，舊版兩塊描邊文字必然互相推擠、還壓住那幾根關鍵 K 棒。 */
 const AXB=[], LANE=[], laneX=[];
 function txtW(s,fs){ let w=0;
   for(let i=0;i<s.length;i++) w+=(s.charCodeAt(i)>255?1.0:0.6)*fs;
   return w; }
 function axisChip(aY,txt,col,dim){
   const h=17; let py=aY-h/2;
   for(let k=0;k<6;k++){
     py=Math.max(RTOP,Math.min(RPB-h,aY-h/2+(k%2?1:-1)*Math.ceil(k/2)*(h+2)));
     if(!AXB.some(b=>py<b+h&&b<py+h)) break;
   }
   AXB.push(py);
   const o=dim?'.55':'1';
   return '<rect x="'+(RW-RR+1)+'" y="'+py.toFixed(1)+'" width="'+(RR-2)+'" height="'+h+
     '" rx="4" fill="'+col+'" opacity="'+o+'"/>'+
     '<text x="'+(RW-RR+7)+'" y="'+(py+h-5).toFixed(1)+'" fill="#0E1116" font-size="11.5"'+
     ' font-weight="700" font-family="ui-monospace,monospace" opacity="'+o+'">'+txt+'</text>';
 }
 function lanePill(X,txt,col,dim){
   const fs=10.5, w=txtW(txt,fs)+13, h=17;
   let px=Math.max(1,Math.min(RW-RR-w-1,X-w/2));
   for(let k=0;k<8;k++){
     if(!LANE.some(b=>px<b.x+b.w+3&&b.x<px+w+3)) break;
     px=Math.min(RW-RR-w-1,px+w+5);
   }
   LANE.push({x:px,w:w});
   const o=dim?'.55':'1';
   return '<g opacity="'+o+'"><rect x="'+px.toFixed(1)+'" y="'+(RH-RBOT+3)+'" width="'+w.toFixed(1)+
     '" height="'+h+'" rx="5" fill="#0E1116" fill-opacity=".92" stroke="'+col+
     '" stroke-opacity=".55"/>'+
     '<text x="'+(px+w/2).toFixed(1)+'" y="'+(RH-RBOT+15)+'" text-anchor="middle" fill="'+col+
     '" font-size="'+fs+'" font-weight="700" font-family="ui-monospace,monospace">'+txt+'</text></g>';
 }
 /* r＝{entry,time,exit,exit_time,dir,net}；pre＝膠囊前綴（揭曉後的「當天」那一筆） */
 function markTrade(r,dim,pre){
   const ia=idxAt(all,r.time), i=gi(ia);
   if(ia<0||i<0||i>=B.length) return '';
   const X=x(i), Y=y(r.entry), long=r.dir==='long', col=long?'#EE5A54':'#34B37E';
   const o=dim?' opacity=".55"':'';
   const je=r.exit_time?gi(idxAt(all,String(r.exit_time).slice(0,5))):-1;
   const hasExit=r.exit!=null&&je>=0&&je<B.length;
   const XE=hasExit?x(je):null, YE=hasExit?y(r.exit):null;
   const ec=(r.net!=null&&r.net>0)?'#EE5A54':'#34B37E';
   let s='';
   if(hasExit){
     const yTop=Math.min(Y,YE), yBot=Math.max(Y,YE);
     s+='<g'+o+'><rect x="'+(X-cw/2).toFixed(1)+'" y="'+yTop.toFixed(1)+'" width="'+
        Math.max(cw,(XE-X)+cw).toFixed(1)+'" height="'+Math.max(2,yBot-yTop).toFixed(1)+
        '" fill="'+ec+'" opacity=".10"/>'+
        '<line x1="'+(X-cw/2).toFixed(1)+'" y1="'+Y.toFixed(1)+'" x2="'+(XE+cw/2).toFixed(1)+
        '" y2="'+Y.toFixed(1)+'" stroke="'+col+'" stroke-width="1.1" stroke-dasharray="4 3" opacity=".7"/>'+
        '<line x1="'+(X-cw/2).toFixed(1)+'" y1="'+YE.toFixed(1)+'" x2="'+(XE+cw/2).toFixed(1)+
        '" y2="'+YE.toFixed(1)+'" stroke="'+ec+'" stroke-width="1.1" stroke-dasharray="4 3" opacity=".7"/>'+
        '<line x1="'+X.toFixed(1)+'" y1="'+Y.toFixed(1)+'" x2="'+XE.toFixed(1)+'" y2="'+
        YE.toFixed(1)+'" stroke="'+ec+'" stroke-width="1.8" opacity=".9" stroke-linecap="round"/></g>';
   }
   const tri=long?('M'+(X-7.5)+' '+(Y+16)+' L'+X+' '+(Y+3.5)+' L'+(X+7.5)+' '+(Y+16)+' Z')
                 :('M'+(X-7.5)+' '+(Y-16)+' L'+X+' '+(Y-3.5)+' L'+(X+7.5)+' '+(Y-16)+' Z');
   s+='<g'+o+'><line x1="'+X.toFixed(1)+'" y1="'+Y.toFixed(1)+'" x2="'+X.toFixed(1)+'" y2="'+
      (RH-RBOT)+'" stroke="'+col+'" stroke-width="1" stroke-dasharray="2 4" opacity=".32"/>'+
      '<path d="'+tri+'" fill="'+col+'" stroke="#0E1116" stroke-width="1.8" stroke-linejoin="round"/>'+
      '<circle cx="'+X.toFixed(1)+'" cy="'+Y.toFixed(1)+'" r="2.6" fill="'+col+
      '" stroke="#0E1116" stroke-width="1.2"/></g>';
   s+=axisChip(Y,String(Math.round(r.entry)),col,dim);
   const near=hasExit&&(XE-X)<110;
   const xt=hasExit?String(r.exit_time).slice(0,5):'';
   const money=(r.net==null?'':'　'+pm(r.net));
   if(!near) laneX.push([X,(pre||'')+(long?'▲ 進 ':'▼ 進 ')+r.time,col,dim]);
   else laneX.push([(X+XE)/2,(pre||'')+(long?'▲ ':'▼ ')+r.time+'→'+xt+money,ec,dim]);
   if(hasExit){
     s+='<g'+o+'><line x1="'+XE.toFixed(1)+'" y1="'+YE.toFixed(1)+'" x2="'+XE.toFixed(1)+
        '" y2="'+(RH-RBOT)+'" stroke="'+ec+'" stroke-width="1" stroke-dasharray="2 4" opacity=".32"/>'+
        '<rect x="'+(XE-5.6).toFixed(1)+'" y="'+(YE-5.6).toFixed(1)+'" width="11.2" height="11.2"'+
        ' rx="2.4" transform="rotate(45 '+XE.toFixed(1)+' '+YE.toFixed(1)+')" fill="'+ec+
        '" stroke="#0E1116" stroke-width="1.8"/></g>';
     s+=axisChip(YE,String(Math.round(r.exit)),ec,dim);
     if(!near) laneX.push([XE,(pre||'')+'出 '+xt+money,ec,dim]);
   }
   return s;
 }
 let mk='';
 if(MODE==='review'){
   // 一天多筆時全部畫出來：選中那筆實心，其餘半透明
   dayTrades(C.day).forEach(v=>{
     const t=v.t;
     mk+=markTrade({entry:t.entry,time:t.time,exit:t.exit,exit_time:t._exit_time,
                    dir:t.dir,net:t._net},!(T&&sel&&v.i===sel.i));
   });
 } else if(RP.judge){
   const J=RP.judge, Rr=RP.result;
   mk+=markTrade({entry:J.entry,time:J.time,exit:Rr?Rr.exit:null,
                  exit_time:Rr?Rr.time:null,dir:J.dir,net:Rr?Rr.net:null});
   if(RP.state==='revealed'){
     const rt=dayTrade(RP.date);            // 揭曉後把當天實際那筆疊上去對照
     if(rt) mk+=markTrade({entry:rt.entry,time:rt.time,exit:rt.exit,
                           exit_time:rt._exit_time,dir:rt.dir,net:rt._net},true,'當天 ');
   }
 }

 /* 游標十字線（重播時不能指到未揭曉的地方） */
 let lb=vis[vis.length-1], hovering=false;
 if(RHOVER.i!=null&&RHOVER.i>=G.from&&RHOVER.i<G.to&&RHOVER.i<=G.rev){
   lb=all[RHOVER.i]; hovering=true;
   const X=x(gi(RHOVER.i));
   g+='<line x1="'+X.toFixed(1)+'" y1="'+RTOP+'" x2="'+X.toFixed(1)+'" y2="'+(RH-RBOT)+
      '" stroke="#8D95A3" stroke-width="1" stroke-dasharray="3 3" opacity=".6"/>';
 }
 /* 膠囊先算（避讓後位置才確定），時間刻度再依 LANE 的實際落點閃避 */
 let pills='';
 laneX.forEach(p=>{ pills+=lanePill(p[0],p[1],p[2],p[3]); });
 const step=Math.max(1,Math.ceil(B.length/9));
 B.forEach((b,i)=>{ if(i%step) return;
   const X=x(i);
   if(LANE.some(z=>X+24>z.x-4&&X-24<z.x+z.w+4)) return;
   g+='<text x="'+X.toFixed(1)+'" y="'+(RH-9)+'" fill="#5C6472" font-size="11" '+
      'text-anchor="middle" font-family="ui-monospace,monospace">'+b.t+'</text>'; });
 g+=mk;                                   // 標記畫最後 → 壓在 K 棒上面，一眼看得到
 g+=pills;

 /* 容器只建一次，這裡只換 svg 內容 */
 const sv=document.getElementById('rsvg');
 if(sv.getAttribute('viewBox')!=='0 0 '+RW+' '+RH) sv.setAttribute('viewBox','0 0 '+RW+' '+RH);
 if(sv.innerHTML!==g) sv.innerHTML=g;

 /* 標題列與 OHLCV 圖例 */
 const op0=all[0].o, px=lb.c, chg=px-op0, pct=chg/op0*100;
 const wd=['日','一','二','三','四','五','六'][new Date(C.day+'T00:00:00').getDay()];
 const dts=dayTrades(C.day);
 // 跟即時分頁同一個 qblock：價格／漲跌膠囊／第三行灰字
 rset('rhead','<div class="qblock"><div class="qmain">'+
   '<span class="cpx '+sgn(chg)+'">'+f(px)+'</span>'+
   '<span class="cchg '+sgn(chg)+'">'+pm(chg)+'<span class="pct">'+pm(pct,2)+'%</span></span>'+
   '</div><div class="qsub"><span>'+C.day+'（'+wd+'）</span><span class="sep">·</span>'+
   '<span>日盤 08:45–13:45</span><span class="sep">·</span>'+
   '<span>'+(dts.length?('當天有 '+dts.length+' 筆紀錄'):'當天沒下單')+'</span>'+
   (MODE==='replay'?'<span class="sep">·</span><span style="color:var(--gold)">重播中（1 分 K）</span>':'')+
   '</div></div>'+
   (MODE==='review'
     ?'<div class="tfsw"><button data-tf="1" class="'+(TF===1?'on':'')+'">1 分</button>'+
      '<button data-tf="5" class="'+(TF===5?'on':'')+'">5 分</button></div>'
     :''));
 const vol=lb.v>=10000?(lb.v/1000).toFixed(1)+'k':lb.v.toFixed(0);
 rset('rlegend','<span class="lt">'+lb.t+'</span><span>開 <b>'+f(lb.o)+'</b></span>'+
   '<span>高 <b>'+f(lb.h)+'</b></span><span>低 <b>'+f(lb.l)+'</b></span>'+
   '<span>收 <b style="color:'+(lb.c>=lb.o?'#EE5A54':'#34B37E')+'">'+f(lb.c)+'</b></span>'+
   '<span>量 <b>'+vol+'</b></span>'+(hovering?'':'<span class="lt">（最新一根）</span>'));
}

/* ---------------- 進場當下的客觀盤面（跟即時分頁同一條資料軌） ---------------- */
function fstrip(D){
 let ft='進場當下的客觀盤面', F=null;
 if(MODE==='replay'){
   const fe=D&&D.feats;
   if(fe&&fe.length){
     // 已經判斷過就凍結在「按下去的那一刻」—— 那才是要檢討的盤面
     const i=RP.judge?idxAt(D.bars,RP.judge.time):RP.rev;
     F=fe[Math.max(0,Math.min(fe.length-1,i))];
     ft=RP.judge?('進場當下的客觀盤面（'+RP.judge.time+'）')
                :('目前這一刻的客觀盤面（'+(F?F.t:'')+'）');
   }
 } else {
   const s=selTrade();
   if(s){ F=s.t._snap; ft='進場當下的客觀盤面（'+s.t.date.slice(5)+' '+s.t.time+'）'; }
 }
 document.getElementById('rftitle').textContent=ft;
 if(!F){ rset('rfstrip','<div class="grp"><div class="it" style="min-width:0">'+
   '<div class="k">　</div><div class="v" style="color:var(--faint);font-size:12.5px">'+
   '這一刻沒有本機 K 棒可以重建盤面</div></div></div>'); return; }
 rset('rfstrip',railHTML([
   [{k:'最近 5 分',v:pm(F.mom5),cls:sgn(F.mom5)},{k:'最近 15 分',v:pm(F.mom15),cls:sgn(F.mom15)}],
   [{k:'對開盤',v:pm(F.ret_open),cls:sgn(F.ret_open)},{k:'跳空',v:pm(F.gap)},
    {k:'今日震幅',v:f(F.rng)},
    {k:'位階',v:F.pos==null?'—':f(F.pos*100)+'%',
     track:F.pos==null?null:F.pos*100,hot:F.pos>0.8||F.pos<0.2},
    {k:'量能',v:F.vol_ratio==null?'—':f(F.vol_ratio,2),u:F.vol_ratio==null?'':'倍',
     track:F.vol_ratio==null?null:F.vol_ratio/3*100,hot:F.vol_ratio>1.5}]
 ]));
}

/* ---------------- 右欄：翻紀錄 ---------------- */
function rowHTML(x){
 const t=x.t, rs=REASON[t._reason]||'';
 return '<div class="trade '+(t._net>0?'win':'loss')+(x.i===SEL?' sel':'')+
   '" data-rpick="'+x.i+'">'+
   '<div class="tr-top"><span class="tr-date">'+(t.date||'').slice(5)+'</span>'+
   '<span class="dir '+(t.dir==='long'?'l':'s')+'">'+(t.dir==='long'?'▲ 多':'▼ 空')+'</span>'+
   '<span class="tr-px">'+t.entry+'<span class="arrow">→</span>'+t.exit+
   (rs?' <span class="tag">'+rs+'</span>':'')+
   (t._source==='app'?' <span class="tag">App 匯入</span>':'')+'</span>'+
   '<span class="tr-res '+(t._net>0?'r-win':'r-loss')+'">'+pm(t._net)+'</span></div>'+
   (t.note?'<div class="tr-note">「'+esc(t.note)+'」</div>':'')+'</div>';
}
function paneReview(){
 const L=rvList(), s=selTrade();
 const FT=[['all','全部'],['win','只看賺的'],['loss','只看賠的'],['long','做多'],['short','做空']];
 let h='<div class="chips">'+FT.map(x=>'<button data-rfilter="'+x[0]+'" class="'+
       (FILTER===x[0]?'on':'')+'">'+x[1]+'</button>').join('')+'</div>';
 h+='<div class="sec-head" style="margin-top:8px"><h2>練習紀錄</h2><span class="count">'+
    L.length+' 筆　'+pm(L.reduce((a,x)=>a+(x.t._net||0),0))+' 點</span></div>';
 if(!L.length){ h+='<div class="card"><div class="empty">這個條件下沒有紀錄</div></div>'; }
 else{
   h+='<div class="list">'+L.map(rowHTML).join('')+'</div>'+
      '<div class="note" style="margin-top:10px;text-align:center">'+
      '<span class="kbd">←</span> <span class="kbd">→</span> 切換上一筆／下一筆　'+
      '<span class="kbd">R</span> 重播這一天</div>';
 }
 if(s){
   const t=s.t, win=t._net>0, ex=t._exit_time?String(t._exit_time).slice(0,5):null;
   h+='<div class="sec-head" style="margin-top:14px"><h2>這一筆</h2><span class="count">'+
      t.date+(t._source==='app'?'　App 匯入':'')+'</span></div>'+
      '<div class="card"><div class="dt">'+
      '<div class="dt-big"><div class="v '+(win?'up':'down')+'">'+pm(t._net)+'</div>'+
      '<div class="l">'+(t.dir==='long'?'做多':'做空')+'　'+(REASON[t._reason]||'')+
      '出場　NT$'+Math.round((t._net||0)*10).toLocaleString()+'</div></div>'+
      '<div class="hr"></div>'+
      '<div class="dt-row"><span class="k">進場</span><span class="v">'+t.time+'　'+t.entry+'</span></div>'+
      '<div class="dt-row"><span class="k">出場</span><span class="v">'+(ex?ex+'　'+t.exit:'—　'+t.exit)+'</span></div>'+
      '<div class="dt-row"><span class="k">抱了多久</span><span class="v">'+
        (t._mins==null?'—':t._mins+' 分鐘')+'</span></div>'+
      '<div class="dt-row"><span class="k">進場後最順</span><span class="v up">'+
        (t._mfe==null?'—':pm(t._mfe)+' 點')+'</span></div>'+
      '<div class="dt-row"><span class="k">進場後最逆</span><span class="v down">'+
        (t._mae==null?'—':pm(t._mae)+' 點')+'</span></div>'+
      '<div class="hr"></div>'+
      '<div class="dt-row"><span class="k" style="font-size:11.5px">心得</span></div>'+
      noteBox(nkey('r',t),t.note,nattr(t),'＋ 補寫這一筆的心得',
              '現在回頭看，這一筆做對了什麼、做錯了什麼？')+
      '<div class="btns" style="margin-top:4px">'+
      '<button class="btn gold" data-ract="replayday">重播這一天（蓋住結果）</button></div>'+
      '</div></div>';
 }
 return h;
}

/* ---------------- 右欄：Bar Replay ---------------- */
function rpBars(){ const D=RB[RP.date+'|1']; return (D&&!D.loading&&D.bars)||null; }
function rpStop(){ if(RP.timer){ clearInterval(RP.timer); RP.timer=null; } }
function rpReset(day){
 rpStop();
 RP.date=day||RP.date; RP.state='idle'; RP.rev=0; RP.judge=null; RP.result=null;
 RP.axis=null; RP.note=''; RP.end=null; RP.n=48; RHOVER.i=null; lastPane='';
}
function rpPlay(){
 if(RP.state==='revealed') return;
 rpStop(); if(!RP.judge) RP.state='running';
 const ms=(SPEEDS.find(s=>s[0]===RP.speed)||SPEEDS[1])[1];
 RP.timer=setInterval(function(){ rpStep(); },ms);
 rvRender();
}
function rpPause(){ rpStop(); if(RP.state==='running') RP.state='paused'; rvRender(); }
function rpStep(back){
 if(RP.state==='revealed') return;          // 揭曉後不再逐根走，要重玩請按按鈕
 const B=rpBars(); if(!B||!B.length) return;
 if(back){ RP.rev=Math.max(0,RP.rev-1); RP.axis=null; rvRender(); return; }
 if(RP.rev>=B.length-1){ rpReveal(); return; }
 RP.rev++;
 if(RP.judge&&!RP.result){
   const b=B[RP.rev], J=RP.judge, d=J.dir==='long'?1:-1;
   const hitSL=d>0?b.l<=J.sl:b.h>=J.sl, hitTP=d>0?b.h>=J.tp:b.l<=J.tp;
   // 【約定】同一根同時觸及停利與停損時算停損 —— 保守，不能從 1 分 K 知道誰先到
   if(hitSL||hitTP){
     const tp=!hitSL;
     RP.result={reason:tp?'tp':'sl',exit:tp?J.tp:J.sl,time:b.t,
                points:tp?RTP:-RTP,net:tp?RTP-RFEE:-RTP-RFEE};
     rpReveal(); return;
   }
   if(b.t>='11:00'){ const p=Math.round(d*(b.c-J.entry));
     RP.result={reason:'close',exit:b.c,time:b.t,points:p,net:p-RFEE}; rpReveal(); return; }
 }
 if(!RP.judge&&B[RP.rev].t>='10:00'){ rpReveal(); return; }
 rvRender();
}
function rpJudge(dir){
 if(RP.state==='revealed'||RP.judge) return;
 const B=rpBars(); if(!B||!B.length) return;
 const b=B[Math.min(RP.rev,B.length-1)], d=dir==='long'?1:-1;
 const el=document.getElementById('jnote');
 RP.judge={dir:dir,entry:b.c,time:b.t,tp:b.c+d*RTP,sl:b.c-d*RTP,
           note:(el?el.value:RP.note)||''};
 RP.state='holding'; rpPlay();
}
function rpReveal(){
 rpStop();
 const B=rpBars(); if(!B||!B.length) return;
 // 整天全部揭開，但畫面停在「判斷的那一段＋後續 40 分鐘」，不要跳到下午去
 const anchor=RP.result?idxAt(B,RP.result.time):RP.rev;
 RP.end=Math.min(B.length,Math.max(anchor+40,RP.rev+20));
 RP.n=Math.min(B.length,Math.max(60,RP.end));
 RP.rev=B.length-1; RP.axis=null; RP.state='revealed';
 const rt=dayTrade(RP.date), J=RP.judge, Rr=RP.result;
 // 落地存檔到 replay_log/：這是事後重播，絕不寫進 practice_trades/（會污染真實練習統計）
 pfetch('/api/replay',
   JSON.stringify({date:RP.date,judged:!!J,
     dir:J?J.dir:null,entry:J?J.entry:null,time:J?J.time:null,note:J?J.note:'',
     exit:Rr?Rr.exit:null,exit_time:Rr?Rr.time:null,reason:Rr?Rr.reason:null,
     points:Rr?Rr.points:null,net:Rr?Rr.net:null,
     same_dir:!!(rt&&J&&rt.dir===J.dir),day_dir:rt?rt.dir:null,day_time:rt?rt.time:null}))
  .then(r=>r.json()).then(x=>{ if(x&&x.tally){ TALLY=x.tally; rvRender(); } }).catch(()=>{});
 rvRender();
}
function paneReplay(D){
 const rt=dayTrade(RP.date);
 const days=((RV&&RV.days)||[]).slice(0,14);
 const traded=(RV&&RV.traded)||[];
 let h='<div class="sec-head" style="margin-top:6px"><h2>選一天重播</h2>'+
   '<span class="count">從 08:45 開始逐根走</span></div><div class="daysel">'+
   days.map(d=>'<button data-rday="'+d+'" class="'+(RP.date===d?'on':'')+'">'+d.slice(5)+
     (traded.indexOf(d)>=0?'<span class="m">有下單</span>':'<span class="m">沒下單</span>')+
     '</button>').join('')+'</div>';
 if(!D||!D.bars||!D.bars.length){
   h+='<div class="card" style="margin-top:12px"><div class="empty">'+
      (D?'這天沒有本機 K 棒可以重播<br>換一天試試':'載入中…')+'</div></div>';
   return h;
 }
 if(RP.state==='idle'){
   h+='<div class="card" style="margin-top:12px"><div class="note" style="border:0;padding:0">'+
      '<b>怎麼玩</b><br>後面的 K 棒會被蓋住，你只看得到「已經走完的部分」。<br>'+
      '按播放讓它一根一根走，覺得可以進場就按 ▲做多 或 ▼做空 —— 這時候你還<b>不知道結果</b>，'+
      '跟早上真的在看盤一樣。<br>判斷完會繼續走到碰停利或停損，然後才揭曉後續走勢，'+
      '並跟你當天實際的決定對照。</div>'+
      '<div class="btns" style="margin-top:12px">'+
      '<button class="btn gold" data-ract="rpplay">開始重播</button></div></div>';
 } else if(RP.state==='revealed'){
   const J=RP.judge, Rr=RP.result;
   h+='<div class="sec-head" style="margin-top:14px"><h2>揭曉・對照</h2><span class="count">'+
      RP.date+'</span></div><div class="card"><div class="cmp">'+
      '<div class="side mine"><div class="h">你這次的判斷</div><div class="b">';
   if(J){ h+='<span class="dir '+(J.dir==='long'?'l':'s')+'">'+(J.dir==='long'?'▲ 多':'▼ 空')+'</span>'+
     '<span>'+J.time+'　'+J.entry+' → '+(Rr?Rr.exit:'—')+'</span>'+
     '<span class="res '+(Rr&&Rr.net>0?'up':'down')+'">'+(Rr?pm(Rr.net):'—')+'</span>'; }
   else { h+='<span style="color:var(--dim)">這次沒有下判斷（觀望）</span>'; }
   h+='</div>'+(J&&J.note?'<div class="tr-note" style="padding-left:0;margin-top:6px">「'+
      esc(J.note)+'」</div>':'')+'</div>'+
      '<div class="side"><div class="h">當天你實際的決定</div><div class="b">';
   if(rt){ h+='<span class="dir '+(rt.dir==='long'?'l':'s')+'">'+(rt.dir==='long'?'▲ 多':'▼ 空')+'</span>'+
     '<span>'+rt.time+'　'+rt.entry+' → '+rt.exit+'</span>'+
     '<span class="res '+(rt._net>0?'up':'down')+'">'+pm(rt._net)+'</span>'; }
   else { h+='<span style="color:var(--dim)">當天你沒有下單</span>'; }
   h+='</div></div>';
   let v='',cls='diff';
   if(J&&rt){ const same=J.dir===rt.dir, dm=mmin(J.time)-mmin(rt.time);
     v=(same?'方向一致':'方向相反')+'　'+
       (dm===0?'時間也一樣':(dm>0?'你晚了 '+dm+' 分鐘':'你早了 '+(-dm)+' 分鐘'));
     cls=same?'same':'diff';
   } else if(J&&!rt){ v='當天你沒進場，這次你進了'; }
   else if(!J&&rt){ v='當天你有進場，這次你選擇觀望'; }
   else { v='兩次都沒進場'; }
   h+='<div class="verdict '+cls+'">'+v+'</div></div>'+
      '<div class="tally"><span>重播 <b>'+TALLY.n+'</b> 次</span>'+
      '<span>停利 <b class="up">'+TALLY.tp+'</b></span>'+
      '<span>停損 <b class="down">'+TALLY.sl+'</b></span>'+
      '<span>與當天同向 <b>'+TALLY.same+'</b></span></div>'+
      '<div style="text-align:center;font-size:10.5px;color:var(--faint);margin-top:6px">'+
      '累計次數（存在 replay_log/，跟你的練習紀錄分開）</div>'+
      '<div class="btns" style="margin-top:12px">'+
      '<button class="btn" data-ract="rpagain">再玩一次這天</button>'+
      '<button class="btn ghost gw" data-ract="rpnext">換下一天</button></div></div>';
 } else if(RP.judge){
   const J=RP.judge, B=D.bars, cur=B[Math.min(RP.rev,B.length-1)].c;
   const fl=(J.dir==='long'?1:-1)*(cur-J.entry);
   h+='<div class="sec-head" style="margin-top:14px"><h2>你已經進場了</h2>'+
      '<span class="count">等結果</span></div><div class="card">'+
      '<div class="hold"><div class="v '+sgn(fl)+'">'+pm(Math.round(fl))+'</div>'+
      '<div class="l">'+(J.dir==='long'?'做多':'做空')+'　'+J.time+' 進場 '+J.entry+'</div></div>'+
      '<div class="dt-row" style="margin-top:10px"><span class="k">停利</span>'+
      '<span class="v up">'+J.tp+'</span></div>'+
      '<div class="dt-row"><span class="k">停損</span><span class="v down">'+J.sl+'</span></div>'+
      (J.note?'<div class="noteline" style="margin-top:10px">「'+esc(J.note)+'」</div>':'')+
      '<div class="btns" style="margin-top:12px">'+
      '<button class="btn ghost gw" data-ract="rpreveal">直接看結果</button></div></div>';
 } else {
   const fe=D.feats, i=Math.min(RP.rev,(fe?fe.length:1)-1);
   const now=fe&&fe.length?fe[Math.max(0,i)]:{t:D.bars[RP.rev].t,price:D.bars[RP.rev].c};
   h+='<div class="sec-head" style="margin-top:14px"><h2>你的判斷</h2><span class="count">現在 '+
      now.t+'　'+f(now.price)+'</span></div><div class="card">'+
      '<div class="btns"><button class="btn long" data-ract="jlong">&#9650; 做多</button>'+
      '<button class="btn short" data-ract="jshort">&#9660; 做空</button></div>'+
      '<input class="jinput" id="jnote" placeholder="為什麼進？（可不寫，回顧時會顯示）" value="'+
      esc(RP.note)+'">'+
      '<div class="btns" style="margin-top:10px">'+
      '<button class="btn ghost gw" data-ract="rpreveal">今天不做，直接揭曉</button></div>'+
      '<div class="note" style="margin-top:10px;text-align:center;border:0;padding:0">'+
      '<span class="kbd">空白鍵</span> 播放／暫停　<span class="kbd">→</span> 下一根　'+
      '<span class="kbd">↑</span> 做多　<span class="kbd">↓</span> 做空</div></div>';
 }
 return h;
}
/* 播放控制列（在圖的正下方，眼睛不用離開圖）。
   兩行：上行運鏡（播放鍵是唯一的金色 ⇒ 一眼看得出主要動作），
   下行時間軸 —— 看得出「現在走到哪、還有多長」，也可以點著跳。 */
function ctrlHTML(D){
 if(MODE!=='replay'){
   return '<div class="chint"><span style="color:var(--dim)">'+
     (selTrade()?'進出場之間已著色：紅＝這一筆賺、綠＝賠':'選一筆紀錄看細節')+
     '</span>　滾輪縮放・拖曳平移・雙擊回到這一筆</div>';
 }
 const B=(D&&D.bars)||[];
 if(!B.length) return '<div class="chint">這天沒有 K 棒</div>';
 const playing=RP.state==='running'||(RP.timer!=null);
 const cur=B[Math.min(RP.rev,B.length-1)];
 const last=Math.max(1,B.length-1);
 const pctOf=t=>{ const i=idxAt(B,t); return (i<0?0:i)/last*100; };
 const fill=Math.min(RP.rev,last)/last*100;
 const wa=pctOf('08:45'), wb=pctOf('09:30');
 let ticks='';
 ['08:45','09:30','11:00','13:40'].forEach(t=>{
   const p=pctOf(t); if(p<=0&&t!=='08:45') return;
   ticks+='<span class="tk" style="left:'+p.toFixed(2)+'%">'+t+'</span>';
 });
 // 判斷點：這次按下做多／做空的那一根（多紅、空綠）
 const jm=RP.judge?'<span class="jm" style="left:'+pctOf(RP.judge.time).toFixed(2)+'%;background:'+
   (RP.judge.dir==='long'?'var(--up)':'var(--down)')+'"></span>':'';
 // 揭曉後不接受跳轉（那是對照用的定格）；已經進場、還在等結果時也不行 ——
 // 跳過去等於跳過中間那幾根的 ±100 觸價檢查，結果會算錯（紀錄正確性）。
 const locked=RP.state==='revealed'||!!RP.judge;
 return '<div class="rpbar"><div class="rprow">'+
   '<button class="rpbtn" data-ract="rphome" title="回到 08:45">⏮</button>'+
   '<button class="rpbtn" data-ract="rpback" title="退一根">◀</button>'+
   '<button class="rpbtn play" data-ract="'+(playing?'rppause':'rpplay')+'">'+
     (playing?'❚❚ 暫停':'▶ 播放')+'</button>'+
   '<button class="rpbtn" data-ract="rpstep" title="下一根">▶▶</button>'+
   '<div class="rpsp">'+SPEEDS.map(s=>'<button data-rspeed="'+s[0]+'" class="'+
     (RP.speed===s[0]?'on':'')+'">×'+s[0]+'</button>').join('')+'</div>'+
   // 「已揭曉」講的是狀態，不能拿 locked 來判 —— locked 還包含「已進場、等結果中」，
   // 那時候後面明明還蓋著，卻會寫成已揭曉（QA 退件：走 20 根按做多就重現）。
   '<div class="rppos"><b>'+cur.t+'</b>　'+(RP.rev+1)+' / '+B.length+' 根'+
     (RP.state==='revealed'?'　已揭曉':'')+'</div></div>'+
   '<div class="rpscrub'+(locked?' locked':'')+'"'+(locked?'':' data-rseek="1"')+
   ' title="點著跳到那一根"><div class="trk"></div>'+
   '<div class="win" style="left:'+wa.toFixed(2)+'%;width:'+Math.max(0,wb-wa).toFixed(2)+'%"></div>'+
   '<div class="fill" style="width:'+fill.toFixed(2)+'%"></div>'+jm+
   '<div class="knob" style="left:'+fill.toFixed(2)+'%"></div>'+ticks+'</div></div>';
}

/* ---------------- 繪製與事件 ---------------- */
function rvRender(nf){
 if(TAB!=='review') return;
 const C=rvCtx(), D=C.day?rvBars(C.day,C.tf):null;
 if(FOCUSPEND&&D) focusTrade();
 rvDraw(C,D);
 rset('rctrl',ctrlHTML(D));
 // 重播每走一根就重繪右欄 → 先把使用者打到一半的「為什麼進」收起來，重繪後再放回去
 const jn=document.getElementById('jnote'); if(jn) RP.note=jn.value;
 const foc=document.activeElement&&document.activeElement.id==='jnote';
 const html=MODE==='review'?paneReview():paneReplay(D);
 if(lastPane!==html && (nf||!nEditing('r'))){
   lastPane=html; document.getElementById('rpane').innerHTML=html;
   if(foc){ const el=document.getElementById('jnote');
     if(el){ el.focus(); el.setSelectionRange(el.value.length,el.value.length); } }
 }
 fstrip(D);
 const sel=document.querySelector('#rpane .trade.sel');
 if(sel&&sel.scrollIntoView) sel.scrollIntoView({block:'nearest'});
}
function setTab(t){
 if(t===TAB) return;
 TAB=t;
 if(t!=='review'){ rpStop(); if(RP.state==='running') RP.state='paused'; }
 // 離開【細節】就把 2 秒輪詢停掉（它只有在那一頁前景時才該跑）
 if(t!=='tick'&&TK.timer){ clearTimeout(TK.timer); TK.timer=null; }
 // 離開【自動下單】也把 5 秒輪詢停掉（它只在那一頁前景時才該跑）
 if(t!=='fire'&&AL.timer){ clearTimeout(AL.timer); AL.timer=null; }
 document.getElementById('tab-live').hidden=(t!=='live');
 document.getElementById('tab-tick').hidden=(t!=='tick');
 document.getElementById('tab-review').hidden=(t!=='review');
 document.getElementById('tab-auto').hidden=(t!=='auto');
 document.getElementById('tab-fire').hidden=(t!=='fire');
 document.querySelectorAll('.tabs button').forEach(b=>
   b.classList.toggle('on',b.getAttribute('data-tab')===t));
 if(t==='review'){ lastPane=''; if(!RV) rvFetch(); else rvRender(); }
 else if(t==='tick'){ tkEnter(); }
 // 【程式下單】的資料不掛在 500ms 的 tick 上，只在切進來／換天／按窗口時抓。
 // 後端的即時報價、持倉監控、±100 自動停利停損全程都在跑，切分頁完全不影響那一條路。
 else if(t==='auto'){ atEnter(); }
 // 【自動下單】同樣不掛在 500ms 的 tick 上：後端的送單、持倉監控、±100 停利停損
 // 全程都在跑，切不切進這一頁完全不影響。
 else if(t==='fire'){ alEnter(); }
 // 切回即時時立刻呼叫一次 tick()（後端的報價、持倉監控、±100 自動停利停損
 // 全程都在跑，切分頁完全不影響那一條路）
 else { lastMkt=''; lastTrade=''; lastStats=''; lastWarn=''; tick(); }
}
function setMode(m){
 if(m===MODE) return;
 MODE=m; rpStop(); RHOVER.i=null; lastPane='';
 document.querySelectorAll('#rmode button').forEach(b=>
   b.classList.toggle('on',b.getAttribute('data-mode')===m));
 if(m==='review') focusTrade();
 else if(!RP.date){ const s=selTrade(); if(s) RP.date=s.t.date; }
 rvRender();
}
function rvPick(i){ SEL=i; focusTrade(); rvRender(); }
function moveSel(step){
 const L=rvList(); if(!L.length) return;
 let k=L.findIndex(x=>x.i===SEL); if(k<0) k=0;
 k=Math.max(0,Math.min(L.length-1,k+step));
 rvPick(L[k].i);
}
function rvBind(){
 const sv=document.getElementById('rsvg');
 const AXIS=RR/RW;
 const onAxis=e=>{ const r=sv.getBoundingClientRect();
   return (e.clientX-r.left)/r.width>1-AXIS; };
 const bars=()=>{ const C=rvCtx(), D=RB[C.day+'|'+C.tf];
   return (D&&!D.loading&&D.bars)||[]; };
 sv.addEventListener('wheel',function(e){
   e.preventDefault();
   // 重播只准改「看幾根」——不能平移到未來，否則等於直接看答案
   if(MODE==='replay'){ RP.n=Math.round(Math.min(120,Math.max(20,RP.n*(e.deltaY>0?1.15:0.87))));
     rvRender(); return; }
   if(e.shiftKey||onAxis(e)){ RVIEW.vz=Math.min(12,Math.max(0.25,RVIEW.vz*(e.deltaY>0?0.88:1.14)));
     rvRender(); return; }
   const all=bars(); if(!all.length) return;
   const G=rvGeom(all), total=all.length, r=sv.getBoundingClientRect();
   const frac=Math.min(1,Math.max(0,(e.clientX-r.left)/r.width));
   const anchor=G.from+frac*G.n;
   const n=Math.round(Math.min(total,Math.max(8,G.n*(e.deltaY>0?1.18:0.85))));
   let end=Math.round(anchor+(1-frac)*n); end=Math.max(n,Math.min(total,end));
   RVIEW.n=n; RVIEW.end=(end>=total)?null:end; rvRender();
 },{passive:false});
 sv.addEventListener('mousedown',function(e){
   if(MODE==='replay') return;
   const all=bars(); if(!all.length) return;
   const G=rvGeom(all), r=sv.getBoundingClientRect();
   RDRAG={x:e.clientX,y:e.clientY,end:G.to,n:G.n,w:r.width,h:r.height,
          vz:RVIEW.vz,voff:RVIEW.voff,axis:onAxis(e)};
   sv.style.cursor=RDRAG.axis?'ns-resize':'grabbing'; e.preventDefault();
 });
 window.addEventListener('mousemove',function(e){
   if(!RDRAG) return;
   if(RDRAG.axis){ RVIEW.vz=Math.min(12,Math.max(0.25,RDRAG.vz*Math.exp(-(e.clientY-RDRAG.y)/220)));
     rvRender(); return; }
   const all=bars(), total=all.length; if(!total) return;
   const moved=Math.round((e.clientX-RDRAG.x)/(RDRAG.w/RDRAG.n));
   let end=RDRAG.end-moved; end=Math.max(RDRAG.n,Math.min(total,end));
   RVIEW.end=(end>=total)?null:end; rvRender();
 });
 window.addEventListener('mouseup',function(){
   if(RDRAG){ RDRAG=null; sv.style.cursor=''; } });
 sv.addEventListener('mousemove',function(e){
   if(RDRAG||TAB!=='review') return;
   const all=bars(); if(!all.length) return;
   const G=rvGeom(all), r=sv.getBoundingClientRect();
   const frac=(e.clientX-r.left)/r.width;
   if(frac<0||frac>1-AXIS){ if(RHOVER.i!=null){ RHOVER.i=null; rvRender(); } return; }
   const i=G.from+Math.floor(frac/(1-AXIS)*G.n);
   // 十字線也要 clamp 在已揭曉的範圍內
   const ni=Math.max(G.from,Math.min(Math.min(G.to-1,G.rev),i));
   if(ni!==RHOVER.i){ RHOVER.i=ni; rvRender(); }
 });
 sv.addEventListener('mouseleave',function(){
   if(RHOVER.i!=null){ RHOVER.i=null; rvRender(); } });
 sv.addEventListener('dblclick',function(e){
   if(MODE==='replay'){ RP.n=48; rvRender(); return; }
   if(onAxis(e)){ RVIEW.vz=1; RVIEW.voff=0; } else focusTrade();
   rvRender();
 });
}
document.addEventListener('click',function(e){
 const tb=e.target.closest('[data-tab]'); if(tb){ setTab(tb.getAttribute('data-tab')); return; }
 if(TAB!=='review') return;
 const md=e.target.closest('[data-mode]'); if(md){ setMode(md.getAttribute('data-mode')); return; }
 const tf=e.target.closest('[data-tf]');
 if(tf){ TF=parseInt(tf.getAttribute('data-tf')); focusTrade(); rvRender(); return; }
 const fl=e.target.closest('[data-rfilter]');
 if(fl){ FILTER=fl.getAttribute('data-rfilter');
   const L=rvList(); if(L.length&&!L.find(x=>x.i===SEL)) SEL=L[0].i;
   focusTrade(); rvRender(); return; }
 const pk=e.target.closest('[data-rpick]');
 if(pk){ rvPick(parseInt(pk.getAttribute('data-rpick'))); return; }
 const dy=e.target.closest('[data-rday]');
 if(dy){ rpReset(dy.getAttribute('data-rday')); rvRender(); return; }
 // 點時間軸跳到那一根：跳之前先停掉播放；已揭曉就不接受跳轉（那是對照用的定格）
 const sk=e.target.closest('[data-rseek]');
 if(sk){
   const B=rpBars();
   if(B&&B.length&&RP.state!=='revealed'&&!RP.judge){
     const r=sk.getBoundingClientRect();
     const p=Math.max(0,Math.min(1,(e.clientX-r.left)/r.width));
     rpStop();
     if(RP.state==='idle') RP.state='paused';
     RP.rev=Math.max(1,Math.round(p*(B.length-1)));
     RP.axis=null;                       // 價格軸只擴不縮，跳轉後要重算
     rvRender();
   }
   return;
 }
 const sp=e.target.closest('[data-rspeed]');
 if(sp){ RP.speed=parseFloat(sp.getAttribute('data-rspeed'));
   if(RP.timer) rpPlay(); else rvRender(); return; }
 const a=e.target.closest('[data-ract]'); if(!a) return;
 const act=a.getAttribute('data-ract');
 if(act==='replayday'){ const s=selTrade(); if(s){ rpReset(s.t.date); setMode('replay'); } return; }
 if(act==='rpplay'){ if(RP.state==='idle') RP.state='paused'; rpPlay(); return; }
 if(act==='rppause'){ rpPause(); return; }
 if(act==='rpstep'){ rpStop(); if(RP.state==='idle') RP.state='paused'; rpStep(); return; }
 if(act==='rpback'){ rpStop(); rpStep(true); return; }
 if(act==='rphome'){ rpReset(); RP.state='paused'; rvRender(); return; }
 if(act==='jlong'||act==='jshort'){
   const el=document.getElementById('jnote'); if(el) RP.note=el.value;
   rpJudge(act==='jlong'?'long':'short'); return; }
 if(act==='rpreveal'){ rpReveal(); return; }
 if(act==='rpagain'){ rpReset(); rvRender(); return; }
 if(act==='rpnext'){ const D=(RV&&RV.days)||[]; const i=D.indexOf(RP.date);
   if(D.length){ rpReset(D[(i+1)%D.length]); rvRender(); } return; }
});
/* 即時分頁：← → 換日、Home 回到即時。看圖時手不用離開鍵盤。 */
document.addEventListener('keydown',function(e){
 if(TAB!=='live') return;
 if(e.target.tagName==='INPUT'||e.target.tagName==='TEXTAREA') return;
 if(e.key==='ArrowLeft'){ e.preventDefault(); goDay(stepTarget(-1)); }
 else if(e.key==='ArrowRight'){ e.preventDefault(); goDay(stepTarget(1)); }
 else if(e.key==='Home'){ e.preventDefault();
   viewDate=''; pickOpen=false; fetchBars(true); tick(); setTimeout(tick,250); }
 else if(e.key==='Escape'&&pickOpen){ pickOpen=false; tick(); }
});
document.addEventListener('keydown',function(e){
 if(TAB!=='review') return;
 if(e.target.tagName==='INPUT'){ if(e.key==='Escape') e.target.blur(); return; }
 if(MODE==='review'){
   if(e.key==='ArrowLeft'||e.key==='ArrowUp'){ e.preventDefault(); moveSel(-1); }
   else if(e.key==='ArrowRight'||e.key==='ArrowDown'){ e.preventDefault(); moveSel(1); }
   else if(e.key==='r'||e.key==='R'){ const s=selTrade();
     if(s){ rpReset(s.t.date); setMode('replay'); } }
   return;
 }
 if(e.key===' '){ e.preventDefault(); if(RP.timer) rpPause(); else rpPlay(); }
 else if(e.key==='ArrowRight'){ e.preventDefault(); rpStop();
   if(RP.state==='idle') RP.state='paused'; rpStep(); }
 else if(e.key==='ArrowLeft'){ e.preventDefault(); rpStop(); rpStep(true); }
 else if(e.key==='ArrowUp'){ e.preventDefault(); if(!RP.judge) rpJudge('long'); }
 else if(e.key==='ArrowDown'){ e.preventDefault(); if(!RP.judge) rpJudge('short'); }
 else if(e.key==='Enter'){ e.preventDefault(); rpReveal(); }
});
/* ══════════════════ 【細節】分頁：逐筆早盤圖（Canvas） ══════════════════

   即時分頁回答「今天走到哪」，這一頁回答「**那 45 分鐘裡，每一秒發生什麼**」。
   區隔不是「今天 vs 那 45 分鐘」（即時分頁本來就看得到那 45 分鐘），是**多細**：
   他那一單 290 秒，在即時的 5 分 K 上是 1 根、30 秒桶約 10 根、1 秒桶 290 根。
   K 棒比折線多給的是「這一段摸到多高多低」，那正是 ±100 觸價規則在意的
   （觸價看有沒有摸到，不是看收在哪）—— 所以預設圖種是秒級 K 棒。

   ⛔ **Canvas，不是 SVG。即時分頁的 chartSVG() 那套不能沿用。**
      2026-09-07 實測（headless、真滑鼠拖 60 步、量每一步）：40,000 點的折線+價帶
      SVG path 每步 155.1ms(6.4fps)／Canvas 全部點 152.8ms(6.5fps)／
      **Canvas 像素分桶 15.4ms(65fps)**；秒K 6.95ms(144fps)。
      SVG 版的 d 字串長 973,543 字元，每平移一格重建一次。
      **像素分桶是必須不是優化** —— 另外兩條路的手感都是「黏住」。
   ⛔ 這一頁一行都不碰下單路徑，也不碰 on_tick（理由見後端 tick_day 的註解）。
   ⛔ 不得出現任何預測／勝率預估／期望值／買賣建議／訊號強度，
      只畫已經發生的客觀事實：成交價、買賣價、成交量、他自己那一單、缺口。
   ⛔ 效能數字、筆數、桶寬、缺口筆數一律不准用紅綠（那兩個顏色只代表漲跌／賺賠）。
*/
const TKSEC0=8*3600+45*60;      // 08:45:00 的當日秒數（＝後端的 sec0）
const TKSPAN=45*60;             // 08:45:00 ~ 09:30:00 ＝ 2,700 秒
const TKR=64, TKTOP=12, TKBOT=26;      // 右側價格軸寬／繪圖區上下留白（沿用面板）
const TKBARSEC=[1,2,5,10,15,30,60];
const TKHOLE=30;                // 秒。連續這麼久一筆成交都沒有 ＝ 沒錄到，不是行情靜止
const TKVOLH=0.22;              // 成交量佔繪圖區高度的比例（疊在下緣，不開獨立副圖）
/* TK.date（想看哪天）與 TK.data.date（手上這份是哪天）**必須是兩個欄位** ——
   換日到新資料回來之間那一秒，只有一個欄位的話會把昨天的資料掛在今天的日期底下。 */
var TK={date:'', data:null, days:null, since:null, skipped:[], today:'',
        view:null, seq:0, pending:false, err:'', v:'C', bar:'auto',
        ov:{trade:true, stop:true, vol:true, idx:false, fixed:true},
        follow:true, hover:null, sel:0, pick:false, cache:{}, timer:null,
        drawn:0, ms:0, times:[], booted:false};
var TKC={W:0,H:0,DPR:0};                    // canvas 目前的尺寸（見 tkFit）
var TKAXIS={key:null,hi:0,lo:0,step:0};     // 價格軸的遲滯（見 tkAxis）
var TKBARC={key:'',bars:null};              // 秒K 聚合快取
var TKDRAG=null;

const tkN=x=>(typeof x==='number'&&isFinite(x))?x:null;
/* ⚠️ 分鐘/秒的換算一律 Math.floor 不可以 round（demo 踩過：290 秒被印成「5 分 50 秒」） */
function tkHMS(s){
 let t=Math.floor(s)+TKSEC0; if(!isFinite(t)) t=TKSEC0;
 const h=Math.floor(t/3600), m=Math.floor(t/60)%60, q=((t%60)+60)%60;
 return String(h).padStart(2,'0')+':'+String(m).padStart(2,'0')+':'+String(q).padStart(2,'0');
}
const tkNum=n=>n==null?'—':Number(n).toLocaleString();

/* ---------------- 逐筆 vs 取樣 ----------------
   這一頁吃兩種檔：逐筆（tick_writer.py 的 YYYY-MM-DD.jsonl）與**取樣**
   （tick_recorder.py 的 YYYY-MM-DD-polled.jsonl，每 0.1 秒讀一次 /api/state、
   只在價格或買賣價有變時記一列）。
   ⛔ 取樣日**一定要在畫面上講出來**（圖上的籤、日期清單的標記、副標三處都要），
      不是只放在資料裡 —— 兩者的密度差 40 倍，看不出差別就會把取樣當成逐筆讀。
   ⛔ 取樣檔**沒有單筆成交量**（vol_ratio 是「累積量 ÷ 歷史中位」，不是量）⇒
      量柱那顆疊圖一律 disabled ＋ 寫出原因，跟「加權」那顆同一套處理。 */
function tkKindOf(d){ const x=tkDayInfo(d); return (x&&x.kind)||'tick'; }
function tkKind(){ const D=TK.data;
 return (D&&D.date===TK.date&&D.kind)?D.kind:tkKindOf(TK.date); }
const tkIsPolled=()=>tkKind()==='polled';
/* 量柱唯一的開關。⛔ 沒有 has_vol 的舊回應（或還沒載入）一律看種類，不猜。 */
function tkHasVol(){ const D=TK.data;
 return (D&&D.date===TK.date&&D.has_vol!==undefined)?!!D.has_vol:(tkKind()==='tick'); }
/* 取樣密度講的是實測的中位間隔，不是寫死的「0.5 秒」。
   ⚠️ 去尾零要一路去到底：舊版 `.replace(/0$/,'')` 只去掉**一個** 0 ⇒ ms_med=1000
      會印成「約 1.0 秒一筆」（lab-qa 2026-09-07 抓到）。整數毫秒（例如 5ms）會被
      去成空字串，所以留一條「小於 10 毫秒就直接講毫秒」的退路。 */
function tkRate(){ const D=TK.data, m=(D&&D.date===TK.date)?D.ms_med:null;
 if(!(typeof m==='number'&&m>0)) return '約 0.5 秒一筆';
 const s=(m/1000).toFixed(2).replace(/\.?0+$/,'');
 return s?('約 '+s+' 秒一筆'):('約 '+Math.round(m)+' 毫秒一筆'); }
/* ⛔ 圖上那張金籤要把「高低不可信」也講出來（2026-09-07 加）：取樣列只有**一個**
   `price`，開高低收四個值全部來自同一個取樣點 —— 那四個數字沒有無中生有（它們真的是
   那一秒**取樣到的**極值），但秒 K 之所以被選成預設，理由正是「K 棒多給的是這一段
   摸到多高多低」，而取樣日**恰好就是那個東西不成立**：他那份真檔實測 1 秒桶有
   **26.7% 高＝低**（401/1504）。他早上盯的是圖不是副標，所以這句話要在圖上。 */
const TKPOLLBADGE=()=>'取樣 · '+tkRate()+' · 不是逐筆 · 高低＝取樣點的極值';

/* ---------------- 取資料 ---------------- */
function tkFetchDays(){
 return fetch('/api/tick/days').then(r=>r.json()).then(x=>{
   TK.days=(x&&x.days)||[]; TK.since=(x&&x.since)||null;
   TK.skipped=(x&&x.skipped)||[]; TK.today=(x&&x.today)||'';
   if(!TK.date){
     const first=TK.days.find(d=>!d.empty)||TK.days[0];
     if(first) TK.date=first.d;
   }
 }).catch(()=>{ if(!TK.days) TK.days=[]; });
}

/* columnar → TypedArray。2,700 個桶 × 8 欄 ≈ 86KB，整天留在記憶體完全無壓力
   （demo 實測載入 40,000 點，JS heap 差 0.00 MB）。 */
function tkPack(x){
 const cap=Math.max(2760,(x.s||[]).length+64);
 const D={date:x.date, len:0, cap:cap, idx:Object.create(null),
          s:new Int32Array(cap), o:new Float32Array(cap), h:new Float32Array(cap),
          l:new Float32Array(cap), c:new Float32Array(cap), vq:new Int32Array(cap),
          bl:new Float32Array(cap), ah:new Float32Array(cap),
          gaps:x.gaps||[], bad:x.bad||0, n:x.n||0, heads:x.heads||0, v:x.v,
          vmix:!!x.vmix, first:x.first, last:x.last, complete:!!x.complete,
          trades:x.trades||[], sec0:x.sec0,
          kind:x.kind||'tick', has_vol:(x.has_vol!==undefined?!!x.has_vol:true),
          ms_med:(x.ms_med==null?null:x.ms_med), nopx:x.nopx||0, outwin:x.outwin||0};
 tkMerge(D,x);
 return D;
}
/* ⛔ 合併規則是「同 s 覆蓋、其餘 append」，**不可以無腦 concat** ——
   `from=<秒>` 回的桶含 from 本身（那個桶上次拿到時還沒收完），
   concat 會出現兩根同一秒的 K 棒：**圖上完全看不出來，只有量會變兩倍**。 */
function tkMerge(D,x){
 const S=x.s||[]; let dirty=false;
 for(let i=0;i<S.length;i++){
   const s=S[i]; let j=D.idx[s];
   if(j===undefined){
     if(D.len>=D.cap) break;
     if(D.len&&s<D.s[D.len-1]) dirty=true;      // 不該發生，但檔案不保證單調遞增
     j=D.len++; D.idx[s]=j; D.s[j]=s;
   }
   D.o[j]=x.o[i]; D.h[j]=x.h[i]; D.l[j]=x.l[i]; D.c[j]=x.c[i];
   D.vq[j]=x.vq[i]||0;
   D.bl[j]=(x.bl[i]==null?NaN:x.bl[i]); D.ah[j]=(x.ah[i]==null?NaN:x.ah[i]);
 }
 if(dirty) tkResort(D);
 // meta 每次都換成最新的（筆數、缺口、first/last 都會長）
 for(const k of ['gaps','bad','n','heads','first','last','complete','trades','v','vmix',
                 'kind','has_vol','ms_med','nopx','outwin'])
   if(x[k]!==undefined) D[k]=x[k];
 TKBARC.key='';
 return D;
}
function tkResort(D){
 const ord=Array.from({length:D.len},(_,i)=>i).sort((a,b)=>D.s[a]-D.s[b]);
 const cp=a=>{const t=a.slice(0,D.len); for(let i=0;i<D.len;i++) a[i]=t[ord[i]];};
 cp(D.s);cp(D.o);cp(D.h);cp(D.l);cp(D.c);cp(D.vq);cp(D.bl);cp(D.ah);
 D.idx=Object.create(null);
 for(let i=0;i<D.len;i++) D.idx[D.s[i]]=i;
}

function tkFetchDay(d,incr){
 // ⛔ 換日請求要帶流水號，只認最後一次的回應。連按 ◀ 時先送的請求可能後回來
 //    （每換一天後端就要重解析一個 5MB 的檔），舊那天的資料會蓋回快取；
 //    而且快取的日期跟當前選擇又剛好對得上 ⇒「載入中」那道守衛判定不出來。
 const my=++TK.seq;
 const old=TK.cache[d];
 const from=(incr&&old&&old.len)?old.s[old.len-1]:null;
 if(!incr) TK.pending=true;
 const url='/api/tick/day?date='+encodeURIComponent(d)+(from!=null?('&from='+from):'');
 return fetch(url).then(r=>r.json().then(j=>({ok:r.ok,j:j}))).then(res=>{
   if(my!==TK.seq) return;
   if(!res.ok){
     if(d===TK.date){ TK.err=(res.j&&res.j.error)||'讀不出來'; TK.pending=false; TK.data=null; }
     tkPaint(); return;
   }
   const x=res.j;
   const D=(incr&&old)?tkMerge(old,x):tkPack(x);
   TK.cache[d]=D;
   // 每個日期一份，最多留 3 份（今天 ＋ 前後翻的兩天），超過就丟最舊的
   const ks=Object.keys(TK.cache);
   if(ks.length>3){ ks.sort(); for(const k of ks.slice(0,ks.length-3)) if(k!==d) delete TK.cache[k]; }
   if(d===TK.date){
     TK.err=''; TK.pending=false;
     const fresh=(!TK.data||TK.data.date!==d);
     TK.data=D;
     if(fresh){ TK.view=null; TK.follow=true; TK.hover=null; TKAXIS.key=null;
                TK.sel=Math.max(0,D.trades.findIndex(t=>t.kind==='real')); }
     tkFollow();
   }
   tkPaint();
 }).catch(()=>{
   if(my!==TK.seq) return;
   if(d===TK.date&&!incr){ TK.pending=false; TK.err='連不上面板'; }
   tkPaint();
 });
}

/* 今天還在長 ⇒ 每 2 秒抓增量。
   ⚠️ 節奏是 2 秒不是 1 秒：tick_writer 每 1.0 秒批次 flush，磁碟上的資料本來就有
      ≤1.5 秒延遲，用 1 秒去輪詢只會常常拿到 0 個新桶。 */
function tkPoll(){
 clearTimeout(TK.timer); TK.timer=null;
 if(TAB!=='tick'||document.hidden) return;
 const D=TK.data;
 if(!D||D.date!==TK.date||D.complete||TK.date!==TK.today) return;
 TK.timer=setTimeout(()=>{ if(!TK.pending) tkFetchDay(TK.date,true); tkPoll(); },2000);
}
/* 貼齊資料右緣時自動跟著往前；使用者拖走過就停止跟隨。
   ⛔ 不可以直接把他拉回去 —— 他可能正在看某一段。 */
function tkFollow(){
 const D=TK.data; if(!D||!D.len||!TK.follow||!TK.view) return;
 const right=D.s[D.len-1], span=TK.view.t1-TK.view.t0;
 if(TK.ov.fixed) return;                       // 固定框本來就涵蓋整段
 TK.view={t0:right-span,t1:right};
}

/* ---------------- 幾何 ---------------- */
function tkFull(){
 const D=TK.data;
 if(TK.ov.fixed) return {t0:0,t1:TKSPAN};
 if(D&&D.len) return {t0:D.s[0],t1:Math.max(D.s[0]+10,D.s[D.len-1])};
 return {t0:0,t1:TKSPAN};
}
function tkView(){ if(!TK.view) TK.view=tkFull(); return TK.view; }
/* 自動桶寬：視窗裡大約 150 根。
   ⚠️ **取樣日的下限是 2 秒不是 1 秒**。用他 2026-09-07 那份真取樣檔（2,972 列）實測：
      1 秒桶 1,504 個、平均 **1.98 個點**，其中 **26.7% 高＝低**（401 個，秒 K 退化成
      一根橫線，看起來像「這一秒沒動」）；2 秒桶 755 個、平均 3.94 個點，
      高＝低掉到 **0.9%**（7 個）。「這一段摸到多高多低」到 2 秒桶才畫得出來
      —— 那正是 ±100 觸價唯一在意的東西。
      （舊版這裡寫「一半只有 1 個點」，那個數字沒有量過：只有 1 個點的是 **13.3%**
        ＝ 200/1504。真正撐住這個決定的是高＝低那 26.7%。）
      ⛔ 只動 `auto`：他手動按「1 秒」還是要給他 1 秒（那是他自己選的，不是我們冒充的）。 */
function tkAutoSec(){ const v=tkView(), want=(v.t1-v.t0)/150;
 const floor=tkIsPolled()?2:1;
 return TKBARSEC.find(s=>s>=want&&s>=floor)||60; }
function tkBarSec(){ return TK.bar==='auto'?tkAutoSec():TK.bar; }

/* 秒K 聚合。桶邊界一律 floor(秒/桶寬)*桶寬（對齊整秒的絕對格線）——
   ⛔ 不可以用「從第一筆開始每 N 秒」，那樣換視窗時每根 K 棒都會重新切一次、
      形狀跟著滑動，看起來像資料在變。sec0=31500 是 60 的倍數，所以相對秒與絕對秒同格線。
   快取 key＝日期|桶寬|桶數，資料或桶寬沒變就不重算。 */
function tkBars(){
 const D=TK.data; if(!D||!D.len) return [];
 const w=tkBarSec(), key=D.date+'|'+w+'|'+D.len;
 if(TKBARC.key===key&&TKBARC.bars) return TKBARC.bars;
 const out=[]; let cur=null;
 for(let i=0;i<D.len;i++){
   const k=Math.floor(D.s[i]/w)*w;
   if(!cur||cur.t!==k){ cur={t:k,o:D.o[i],h:D.h[i],l:D.l[i],c:D.c[i],v:D.vq[i]}; out.push(cur); }
   else{ if(D.h[i]>cur.h)cur.h=D.h[i]; if(D.l[i]<cur.l)cur.l=D.l[i];
         cur.c=D.c[i]; cur.v+=D.vq[i]; }
 }
 TKBARC={key:key,bars:out}; return out;
}
/* 視窗內索引用二分搜（資料本身時間遞增），不要每次 filter 整條陣列 */
function tkBisect(D,v){
 let lo=0,hi=D.len-1;
 while(lo<hi){ const m=(lo+hi)>>1; if(D.s[m]<v) lo=m+1; else hi=m; }
 return lo;
}
function tkRange(){
 const D=TK.data, v=tkView();
 if(!D||!D.len) return [0,-1];
 return [Math.max(0,tkBisect(D,v.t0)-1), Math.min(D.len-1,tkBisect(D,v.t1)+1)];
}
/* 「這一段完全沒有成交」＝沒錄到。08:45~09:30 的微台不可能連 30 秒一筆都沒有。
   ⚠️ **取樣日照樣用 30 秒這個門檻**（2026-09-07 量過他那份真的取樣檔：相鄰兩列
      中位 457ms、最大 1,628ms，**秒與秒之間最大只跳 2 秒**）⇒ 30 秒離取樣本身的
      抖動還有一個數量級，不會誤報。
   ⛔ 但**那句話要換掉**：取樣工具「只在價格或買賣價有變時才記一列」，所以取樣檔裡
      一段空白有兩種可能 —— 沒錄到，**或**那段時間報價真的一動也沒動。
      逐筆檔可以斬釘截鐵說「不是沒行情」，取樣檔不行，寫成一樣的就是講了一句不確定的話。 */
function tkHoles(){
 const D=TK.data, out=[]; if(!D||!D.len) return out;
 for(let i=1;i<D.len;i++) if(D.s[i]-D.s[i-1]>TKHOLE) out.push([D.s[i-1]+1,D.s[i]]);
 return out;
}

/* ---------------- 價格軸：niceStep ＋ 遲滯（必抄，不是優化） ----------------
   即時分頁 2026-09-02 修過一次「軸一直重算、整張圖跳」（開盤 8 次 → 1 次）。
   這張圖比 5 分 K 嚴重得多：資料密 40 倍、拖曳縮放是主要操作、最後一根還在長。
   ⚠️ 遲滯 key **必須**帶「視窗起｜視窗迄｜桶寬」——
      漏掉後兩項會出現「軸黏在上一個視窗」的鬼影（縮放／換圖種對不上）。 */
function tkNiceStep(raw){
 if(!(raw>0)) return 1;
 const e=Math.pow(10,Math.floor(Math.log10(raw))), r=raw/e;
 return (r<=1?1:r<=2?2:r<=2.5?2.5:r<=5?5:10)*e;
}
function tkAxis(){
 const D=TK.data, v=tkView(), w=tkBarSec();
 let hi=-1e18, lo=1e18;
 if(TK.v==='C'){
   for(const b of tkBars()) if(b.t+w>=v.t0&&b.t<=v.t1){
     if(b.h>hi)hi=b.h; if(b.l<lo)lo=b.l; }
 }else if(D&&D.len){
   const [i0,i1]=tkRange();
   for(let i=i0;i<=i1;i++){
     if(D.h[i]>hi)hi=D.h[i]; if(D.l[i]<lo)lo=D.l[i];
     if(TK.v==='B'){ const a=D.ah[i], b=D.bl[i];
       if(isFinite(a)&&a>hi)hi=a; if(isFinite(b)&&b<lo)lo=b; }
   }
 }
 /* ⚠️ 進出場價只有在「那一單真的落在目前視窗的時間範圍裡」時才納入價格軸。
    ⚠️ 真實單的 exit 可能是 null，`Math.min(lo,entry,exit)` 會把 null 當 0、
       價格軸整個掉到 0（2026-09-02 踩過）—— 所以一律先 tkN() 過濾。
    ⛔ ±100 停利停損**一律不納入**：那 45 分鐘的真實振幅常常只有 30~60 點，
       硬把 ±200 的範圍塞進來會把唯一要看的波動壓成一條直線。 */
 if(TK.ov.trade) for(const t of tkVisTrades()){
   const a=tkN(t.entry), b=tkN(t.exit);
   if(a!=null){ if(a>hi)hi=a; if(a<lo)lo=a; }
   if(b!=null){ if(b>hi)hi=b; if(b<lo)lo=b; }
 }
 if(!(hi>lo)){ const m=(hi>-1e17?hi:12000); hi=m+5; lo=m-5; }
 const pad=Math.max(1,(hi-lo)*0.06);
 let aHi=hi+pad, aLo=lo-pad;
 const step=tkNiceStep((aHi-aLo)/6);
 aHi=Math.ceil(aHi/step)*step; aLo=Math.floor(aLo/step)*step;
 const key=[TK.date,TK.v,v.t0.toFixed(2),v.t1.toFixed(2),w].join('|');
 /* key 沒變時：舊軸還包得住新資料、而且新資料的高度沒有縮到舊軸的 55% 以下，就沿用。 */
 if(TKAXIS.key===key&&TKAXIS.hi>=aHi&&TKAXIS.lo<=aLo
    &&(aHi-aLo)>=(TKAXIS.hi-TKAXIS.lo)*0.55){
   return TKAXIS;
 }
 TKAXIS={key:key,hi:aHi,lo:aLo,step:step};
 return TKAXIS;
}

/* ---------------- 我的單 ---------------- */
function tkAllTrades(){ const D=TK.data; return (D&&D.trades)||[]; }
function tkTradeSpan(t){
 const a=tkN(t.t_in), b=tkN(t.t_out);
 return {a:a==null?null:a-TKSEC0, b:b==null?null:b-TKSEC0};
}
function tkVisTrades(){
 const v=tkView(), out=[];
 for(const t of tkAllTrades()){
   const sp=tkTradeSpan(t); if(sp.a==null) continue;
   const end=sp.b==null?v.t1:sp.b;
   if(sp.a<v.t1&&end>v.t0) out.push(t);
 }
 return out;
}
function tkSel(){ const L=tkAllTrades(); if(!L.length) return null;
 return L[Math.min(Math.max(0,TK.sel),L.length-1)]; }

/* ---------------- canvas ----------------
   ⛔ 這道 early-return 不准拿掉。寫 canvas.width（**就算寫同一個值**）會重新配置
      整張後備緩衝區並清空 —— demo 實測 2,212 點時一次 draw 從 2ms 變 129ms（64 倍）。
      tkFit() 每次 draw 都會呼叫，所以它在熱路徑上。 */
function tkFit(){
 const cv=document.getElementById('tkcv'); if(!cv) return null;
 const box=cv.getBoundingClientRect();
 const w=Math.max(320,Math.round(box.width)), h=Math.max(140,Math.round(box.height));
 const dpr=Math.min(2,window.devicePixelRatio||1);
 if(w===TKC.W&&h===TKC.H&&dpr===TKC.DPR&&cv.width) return cv;
 TKC={W:w,H:h,DPR:dpr};
 cv.width=Math.round(w*dpr); cv.height=Math.round(h*dpr);
 cv.getContext('2d').setTransform(dpr,0,0,dpr,0,0);
 return cv;
}

/* 像素分桶：一個像素欄只留 min/max/first/last（買賣價另留 bidMin/askMax）。
   ⛔ 不可以用「每 N 筆取一筆」的等距抽樣 —— 那會漏掉尖峰，
      而尖峰正是 ±100 觸價唯一在意的東西。
   一個像素欄本來就塞不下兩個以上的值，所以這不叫抽稀：畫面上看到的東西一模一樣
   （每欄的 min/max 逐欄比對必須 100% 相等，探針第 2 條在守）。
   真正丟掉的只有「同一像素欄之內誰先誰後」。 */
function tkCols(D,i0,i1,xOf,cols){
 const mn=new Float64Array(cols).fill(Infinity), mx=new Float64Array(cols).fill(-Infinity);
 const bmn=new Float64Array(cols).fill(Infinity), amx=new Float64Array(cols).fill(-Infinity);
 const fst=new Float64Array(cols).fill(NaN), lst=new Float64Array(cols).fill(NaN);
 let used=0;
 for(let i=i0;i<=i1;i++){
   let c=Math.floor(xOf(D.s[i])); if(c<0) c=0; if(c>=cols) c=cols-1;
   if(D.l[i]<mn[c]) mn[c]=D.l[i];
   if(D.h[i]>mx[c]) mx[c]=D.h[i];
   if(isNaN(fst[c])){ fst[c]=D.o[i]; used++; }
   lst[c]=D.c[i];
   const b=D.bl[i], a=D.ah[i];
   if(isFinite(b)&&b<bmn[c]) bmn[c]=b;
   if(isFinite(a)&&a>amx[c]) amx[c]=a;
 }
 return {mn:mn,mx:mx,bmn:bmn,amx:amx,fst:fst,lst:lst,used:used};
}

/* 折線／折線+價帶。**抽出來當獨立函式**是刻意的：探針的負控組要能整支換掉，
   拿「一個點都不省」的畫法跑同一份資料，證明分桶那條綠燈不是量錯的
   （沒有負控組的綠燈在這個專案不算數）。 */
function tkLine(ctx,D,i0,i1,xOf,yOf,PW){
 const cols=Math.max(1,Math.ceil(PW));
 const C=tkCols(D,i0,i1,xOf,cols);
 if(TK.v==='B'){
   ctx.fillStyle='rgba(124,140,168,.20)'; ctx.beginPath();
   let st=false;
   for(let c=0;c<cols;c++) if(isFinite(C.amx[c])){ const y=yOf(C.amx[c]);
     st?ctx.lineTo(c+.5,y):(ctx.moveTo(c+.5,y),st=true); }
   for(let c=cols-1;c>=0;c--) if(isFinite(C.bmn[c])) ctx.lineTo(c+.5,yOf(C.bmn[c]));
   ctx.closePath(); ctx.fill();
 }
 // 每欄的高低（極短的垂直線）＝那一欄真正的振幅，不會被抽掉
 ctx.strokeStyle='rgba(233,236,241,.38)'; ctx.lineWidth=1; ctx.beginPath();
 for(let c=0;c<cols;c++) if(isFinite(C.mn[c])&&C.mx[c]-C.mn[c]>0){
   ctx.moveTo(c+.5,yOf(C.mx[c])); ctx.lineTo(c+.5,yOf(C.mn[c])); }
 ctx.stroke();
 ctx.strokeStyle='#E9ECF1'; ctx.lineWidth=1.4; ctx.lineJoin='round'; ctx.beginPath();
 let st=false;
 for(let c=0;c<cols;c++) if(!isNaN(C.lst[c])){ const y=yOf(C.lst[c]);
   st?ctx.lineTo(c+.5,y):(ctx.moveTo(c+.5,y),st=true); }
 ctx.stroke();
 return C.used;
}

function tkDraw(){
 const t0=performance.now();
 const cv=tkFit(); if(!cv) return 0;
 const ctx=cv.getContext('2d');
 const W=TKC.W, H=TKC.H, PW=W-TKR;
 const D=TK.data, v=tkView();
 const xOf=s=>(s-v.t0)/(v.t1-v.t0)*PW;
 const A=tkAxis();
 const PH=H-TKTOP-TKBOT;
 // ⛔ 取樣日沒有量（tkHasVol()===false）⇒ 量區高度一定是 0。
 //    ⛔⛔ 絕不可以拿 vol_ratio 之類的東西湊一根「看起來像」的量柱。
 const VH=(TK.ov.vol&&tkHasVol()&&D&&D.len)?PH*TKVOLH:0;
 const priceH=PH-VH;
 const yOf=p=>TKTOP+(A.hi-p)/(A.hi-A.lo)*priceH;
 ctx.clearRect(0,0,W,H);
 ctx.font='12px ui-monospace, monospace'; ctx.textBaseline='alphabetic'; ctx.textAlign='left';

 // 下單時段底色（沿用面板「08:45~09:30」的表達）
 const bx0=Math.max(0,xOf(0)), bx1=Math.min(PW,xOf(TKSPAN));
 if(bx1>bx0){ ctx.fillStyle='rgba(227,169,81,.05)'; ctx.fillRect(bx0,TKTOP,bx1-bx0,PH); }

 /* 缺的那段畫斜線 ＋ **一句人話**。
    ⛔ 那句話一定要說出「不是沒行情」：空白的那一段長得跟「行情沒動」一模一樣，
       他早上是照圖判斷的，看不出差別會直接誤讀。
    ⚠️「我們漏了」與「時間還沒走到」的文案必須不同。 */
 const hatch=(x0,x1,label,soft)=>{
   x0=Math.max(0,x0); x1=Math.min(PW,x1);
   if(!(x1>x0+1)) return;
   ctx.save(); ctx.beginPath(); ctx.rect(x0,TKTOP,x1-x0,PH); ctx.clip();
   ctx.fillStyle='#121721'; ctx.fillRect(x0,TKTOP,x1-x0,PH);
   ctx.strokeStyle=soft?'rgba(141,149,163,.10)':'rgba(141,149,163,.18)'; ctx.lineWidth=1;
   for(let x=x0-H;x<x1+H;x+=7){ ctx.beginPath(); ctx.moveTo(x,H-TKBOT);
     ctx.lineTo(x+PH,TKTOP); ctx.stroke(); }
   ctx.restore();
   if(x1-x0>130&&label){
     ctx.save(); ctx.font='12.5px "Microsoft JhengHei","PingFang TC",sans-serif';
     const tw=ctx.measureText(label).width+16;
     ctx.fillStyle='#0E1116'; ctx.globalAlpha=.82;
     ctx.fillRect((x0+x1)/2-tw/2,TKTOP+priceH/2-18,tw,22); ctx.globalAlpha=1;
     ctx.fillStyle='#8D95A3'; ctx.textAlign='center';
     ctx.fillText(label,(x0+x1)/2,TKTOP+priceH/2-2);
     ctx.textAlign='left'; ctx.restore();
   }
 };
 /* ⚠️ 取樣日不可以說「不是沒行情」—— 取樣工具只在有變動時才記一列，
       空白的那一段有兩種可能，講死就是講了一句我們證不了的話。 */
 const MISS=tkIsPolled()?'這段沒有取樣到（沒錄到，或報價一直沒變）'
                       :'這段沒有錄到（不是沒行情）', SOON='還沒到';
 if(D&&D.len){
   hatch(xOf(v.t0),xOf(D.s[0]),MISS);
   // 今天而且還沒錄完 ⇒ 尾巴是「還沒到」；過去的日子沒有「還沒到」這件事
   const growing=(!D.complete&&D.date===TK.today);
   hatch(xOf(D.s[D.len-1]),xOf(v.t1),growing?SOON:MISS,growing);
   for(const g of tkHoles()) hatch(xOf(g[0]),xOf(g[1]),MISS);
 }

 // 價格軸底 ＋ 格線（刻度線從 lo 往上取第一個 step 整數倍開始畫）
 ctx.fillStyle='rgba(28,34,44,.45)'; ctx.fillRect(PW,0,TKR,H);
 ctx.strokeStyle='#232A35'; ctx.lineWidth=1;
 for(let p=A.lo;p<=A.hi+1e-9;p+=A.step){
   const y=Math.round(yOf(p))+.5;
   if(y<TKTOP-1||y>TKTOP+priceH+1) continue;
   ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(PW,y); ctx.stroke();
   ctx.fillStyle='#5C6472'; ctx.fillText(p.toFixed(A.step<1?1:0),PW+8,y+4);
 }

 /* 時間軸。⛔ 秒級一律印完整 HH:MM:SS —— 只印 MM:SS 的話，放大到 12 秒時
    「14:05」會被讀成下午兩點零五（demo 實測踩到）。 */
 const span=v.t1-v.t0, maxLab=Math.max(3,Math.floor(PW/110));
 const cand=[1,2,5,10,15,30,60,120,300,600,900];
 const gt=cand.find(c=>span/c<=maxLab)||900;
 ctx.strokeStyle='rgba(35,42,53,.7)';
 for(let t=Math.ceil(v.t0/gt)*gt;t<=v.t1;t+=gt){
   const x=Math.round(xOf(t))+.5;
   ctx.beginPath(); ctx.moveTo(x,TKTOP); ctx.lineTo(x,H-TKBOT); ctx.stroke();
   ctx.fillStyle='#5C6472'; ctx.textAlign='center';
   ctx.fillText(tkHMS(t),Math.max(34,Math.min(x,PW-34)),H-8);
   ctx.textAlign='left';
 }
 // 09:30 收手線。標籤放**下緣**不放上緣（上緣要留給畫面外的邊緣籤，兩張會互相壓）
 if(TKSPAN>=v.t0&&TKSPAN<=v.t1){
   const x=Math.round(xOf(TKSPAN))+.5;
   ctx.save(); ctx.setLineDash([4,4]); ctx.strokeStyle='rgba(227,169,81,.55)';
   ctx.beginPath(); ctx.moveTo(x,TKTOP); ctx.lineTo(x,H-TKBOT); ctx.stroke(); ctx.restore();
   ctx.save(); ctx.fillStyle='#E3A951';
   ctx.font='11.5px ui-monospace,"Microsoft JhengHei",monospace';
   const lab='09:30 收手', lw=ctx.measureText(lab).width;
   ctx.fillText(lab,Math.min(x+5,PW-lw-4),H-TKBOT-8); ctx.restore();
 }

 let drawn=0;
 ctx.save(); ctx.beginPath(); ctx.rect(0,0,PW,H); ctx.clip();
 if(D&&D.len){
   const [i0,i1]=tkRange();
   if(TK.v==='C'){
     const w=tkBarSec(), B=tkBars();
     const bw=Math.max(1,Math.min(14,(xOf(v.t0+w)-xOf(v.t0))*.66));
     for(const b of B){
       if(b.t+w<v.t0||b.t>v.t1) continue;
       drawn++;
       const x=xOf(b.t+w/2), up=b.c>=b.o;
       ctx.strokeStyle=ctx.fillStyle=up?'#EE5A54':'#34B37E'; ctx.lineWidth=1;
       ctx.beginPath(); ctx.moveTo(Math.round(x)+.5,yOf(b.h));
       ctx.lineTo(Math.round(x)+.5,yOf(b.l)); ctx.stroke();
       const yo=yOf(b.o), yc=yOf(b.c);
       ctx.fillRect(x-bw/2,Math.min(yo,yc),bw,Math.max(1.2,Math.abs(yc-yo)));
     }
   }else{
     drawn=tkLine(ctx,D,i0,i1,xOf,yOf,PW);
   }

   /* 成交量：疊在下緣、佔繪圖區 22%（即時分頁的 K 線圖就是這樣畫的，沿用同一套語言）。
      量＝**桶內加總**；紅綠＝該桶的漲跌（收 ≥ 開為紅）。
      ⛔ 絕對不可以標成「買量／賣量」或「內外盤」—— 逐筆紀錄裡沒有這個旗標，那會是編的。
      量軸不畫刻度數字（秒級成交量的絕對值沒有解讀價值），只在右上角標「最大 N 口」。 */
   if(VH>0){
     const vy0=TKTOP+priceH, vy1=TKTOP+PH;
     ctx.strokeStyle='#1E2530'; ctx.lineWidth=1; ctx.beginPath();
     ctx.moveTo(0,Math.round(vy0)+.5); ctx.lineTo(PW,Math.round(vy0)+.5); ctx.stroke();
     let vmax=0, list;
     if(TK.v==='C'){ const w=tkBarSec();
       list=tkBars().filter(b=>b.t+w>=v.t0&&b.t<=v.t1)
                    .map(b=>({x0:xOf(b.t),x1:xOf(b.t+w),v:b.v,up:b.c>=b.o}));
     }else{
       const cols=Math.max(1,Math.ceil(PW)), acc=new Float64Array(cols);
       const upc=new Int8Array(cols);
       for(let i=i0;i<=i1;i++){ let c=Math.floor(xOf(D.s[i]));
         if(c<0)c=0; if(c>=cols)c=cols-1; acc[c]+=D.vq[i]; upc[c]=(D.c[i]>=D.o[i])?1:0; }
       list=[]; for(let c=0;c<cols;c++) if(acc[c]>0)
         list.push({x0:c,x1:c+1,v:acc[c],up:!!upc[c]});
     }
     for(const b of list) if(b.v>vmax) vmax=b.v;
     if(vmax>0){
       for(const b of list){
         const h2=Math.max(1,(b.v/vmax)*(vy1-vy0-3));
         ctx.fillStyle=b.up?'rgba(238,90,84,.55)':'rgba(52,179,126,.55)';
         const w2=Math.max(1,Math.min(14,(b.x1-b.x0)*.66));
         ctx.fillRect((b.x0+b.x1)/2-w2/2,vy1-h2,w2,h2);
       }
       // 量軸不畫刻度數字（秒級成交量的絕對值沒有解讀價值，要看的是相對高低），
       // 只在右上角標一個「最大 N 口」；量柱會蓋到它，所以先墊一塊底
       ctx.save(); ctx.font='10.5px ui-monospace,monospace'; ctx.textAlign='right';
       const vt='最大 '+vmax.toLocaleString()+' 口', vw=ctx.measureText(vt).width;
       ctx.fillStyle='#0E1116'; ctx.globalAlpha=.85;
       ctx.fillRect(PW-vw-11,vy0+2,vw+8,13); ctx.globalAlpha=1;
       ctx.fillStyle='#5C6472'; ctx.fillText(vt,PW-6,vy0+12);
       ctx.textAlign='left'; ctx.restore();
     }
   }

   /* 佇列滿丟過資料的地方：一條金色虛線 ＋ 上緣小三角。
      這是「這裡有 N 筆沒錄到」的痕跡，不是行情。 */
   for(const g of (D.gaps||[])){
     if(g.sec==null||g.sec<v.t0||g.sec>v.t1) continue;
     const x=Math.round(xOf(g.sec))+.5;
     ctx.save(); ctx.setLineDash([3,5]); ctx.strokeStyle='rgba(227,169,81,.6)';
     ctx.lineWidth=1; ctx.beginPath(); ctx.moveTo(x,TKTOP); ctx.lineTo(x,TKTOP+priceH);
     ctx.stroke(); ctx.restore();
     ctx.fillStyle='#E3A951'; ctx.beginPath(); ctx.moveTo(x,TKTOP+7);
     ctx.lineTo(x-5,TKTOP); ctx.lineTo(x+5,TKTOP); ctx.closePath(); ctx.fill();
   }
 }
 ctx.restore();

 // 我的單
 if(TK.ov.trade&&D) tkDrawTrades(ctx,xOf,yOf,v,PW,H,A,priceH);
 // 游標十字線
 if(TK.hover!=null&&D&&D.len) tkDrawHover(ctx,xOf,yOf,PW,H,priceH);

 /* ⛔⛔ 取樣日的標籤**畫在圖上**，不是只放在資料裡。
    這張圖跟另外兩張的差別就是「多細」，取樣把密度打掉 40 倍 ——
    看不出差別的話他會把取樣當逐筆讀，而畫面上一模一樣。
    位置在繪圖區**左下角**：左上角是「我那一單在左/右邊」的邊緣籤、
    右側是「±100 在畫面外」與「09:30 收手」，三張擠在一起會互相壓掉字。 */
 if(tkIsPolled()) tkChip(ctx,6,TKTOP+priceH-22,TKPOLLBADGE(),'#E3A951');

 const ms=performance.now()-t0;
 TK.ms=ms; TK.drawn=drawn;
 TK.times.push(ms); if(TK.times.length>240) TK.times.shift();
 return ms;
}

function tkChip(ctx,x,y,txt,col){
 ctx.save(); ctx.font='11px ui-monospace,"Microsoft JhengHei",monospace';
 const w=ctx.measureText(txt).width+14;
 ctx.fillStyle='#0E1116'; ctx.globalAlpha=.9;
 ctx.beginPath(); ctx.roundRect(x,y,w,17,4); ctx.fill(); ctx.globalAlpha=1;
 ctx.strokeStyle=col; ctx.globalAlpha=.5; ctx.lineWidth=1; ctx.stroke(); ctx.globalAlpha=1;
 ctx.fillStyle=col; ctx.fillText(txt,x+7,y+12); ctx.restore();
}
function tkDrawTrades(ctx,xOf,yOf,v,PW,H,A,priceH){
 const vis=tkVisTrades(), all=tkAllTrades();
 ctx.save(); ctx.beginPath(); ctx.rect(0,0,PW,H); ctx.clip();
 for(const t of vis){
   const sp=tkTradeSpan(t);
   const x0=Math.max(-2,xOf(sp.a)), x1=Math.min(PW,xOf(sp.b==null?v.t1:sp.b));
   ctx.fillStyle='rgba(233,236,241,.028)';
   ctx.fillRect(x0,TKTOP,Math.max(1,x1-x0),priceH);
   ctx.strokeStyle='rgba(233,236,241,.16)'; ctx.lineWidth=1;
   [x0,x1].forEach(x=>{ ctx.beginPath(); ctx.moveTo(x+.5,TKTOP);
     ctx.lineTo(x+.5,TKTOP+priceH); ctx.stroke(); });
   const short=(t.dir==='short'), pin=tkN(t.entry), pout=tkN(t.exit);
   const tag=(t.kind==='real'?'真':'練');
   if(pin!=null){
     const y=yOf(pin);
     ctx.save(); ctx.fillStyle=short?'#34B37E':'#EE5A54'; ctx.beginPath();
     if(short){ ctx.moveTo(xOf(sp.a),y+9); ctx.lineTo(xOf(sp.a)-7,y-3); ctx.lineTo(xOf(sp.a)+7,y-3); }
     else{ ctx.moveTo(xOf(sp.a),y-9); ctx.lineTo(xOf(sp.a)-7,y+3); ctx.lineTo(xOf(sp.a)+7,y+3); }
     ctx.closePath(); ctx.fill(); ctx.restore();
     tkChip(ctx,Math.max(2,Math.min(xOf(sp.a)+10,PW-160)),y-30,
       tag+(short?' ▼ 空 ':' ▲ 多 ')+tkHMS(sp.a)+' '+pin.toFixed(0),
       short?'#34B37E':'#EE5A54');
   }
   if(pout!=null&&sp.b!=null){
     const y=yOf(pout), win=(tkN(t.points)||0)>0, col=win?'#EE5A54':'#34B37E';
     ctx.save(); ctx.strokeStyle=col; ctx.lineWidth=2.2; ctx.beginPath();
     const x=xOf(sp.b);
     ctx.moveTo(x-6,y-6); ctx.lineTo(x+6,y+6); ctx.moveTo(x+6,y-6); ctx.lineTo(x-6,y+6);
     ctx.stroke(); ctx.restore();
     tkChip(ctx,Math.max(2,Math.min(x+10,PW-180)),y+12,
       '✕ '+tkHMS(sp.b)+' '+pout.toFixed(0)+
       (tkN(t.points)==null?'  —':'  '+(t.points>0?'+':'')+t.points),col);
   }
 }
 ctx.restore();
 /* 不在畫面裡就整組不畫，改用邊緣籤講清楚它在哪一邊。
    ⛔ 不要把標記黏在畫面邊緣假裝畫得出來 —— 看的人會以為那一單就發生在畫面邊上。 */
 for(const t of all){
   if(vis.indexOf(t)>=0) continue;
   const sp=tkTradeSpan(t); if(sp.a==null) continue;
   const left=(sp.b==null?sp.a:sp.b)<=v.t0;
   const txt=(left?'← ':'')+(t.kind==='real'?'真':'練')+'那一單在'+(left?'左':'右')+
             '邊（'+tkHMS(sp.a)+'）'+(left?'':' →');
   ctx.save(); ctx.font='11px ui-monospace,"Microsoft JhengHei",monospace';
   const w=ctx.measureText(txt).width+14; ctx.restore();
   tkChip(ctx,left?6:PW-w-6,TKTOP+4,txt,'#E3A951');
   break;
 }
 /* ±100：只畫「目前選中的那一筆」。⛔ 沒有交易的日子不准畫「如果現在進場 ±100 會在哪」
    —— 那是建議。畫不到就在對應那一側掛一張籤，**不撐大價格軸**。 */
 const s=tkSel();
 if(TK.ov.stop&&s&&tkN(s.entry)!=null){
   const d=(s.dir==='short')?-1:1, e=tkN(s.entry);
   [[e+d*100,'#EE5A54','停利 +100'],[e-d*100,'#34B37E','停損 −100']].forEach(z=>{
     const y=yOf(z[0]);
     if(y>=TKTOP+8&&y<=TKTOP+priceH-4){
       ctx.save(); ctx.setLineDash([5,4]); ctx.strokeStyle=z[1]; ctx.globalAlpha=.75;
       ctx.lineWidth=1.2; ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(PW,y); ctx.stroke();
       ctx.restore();
       tkChip(ctx,PW-124,y-8,z[2]+' '+z[0].toFixed(0),z[1]);
     }else{
       /* 畫面外：不去撐大價格軸（會把真正的波動壓扁），改成貼一張籤講清楚差多少。
          ⚠️ y 要讓開兩個已經被佔走的位置：上緣 TKTOP+4 是「我那一單在左/右邊」的邊緣籤、
             下緣 H-BOT-8 是「09:30 收手」—— 三張擠在同一行時會互相蓋掉一半的字
             （實測過：停利那張把邊緣籤的時間壓成「:00 ）→」）。 */
       const above=z[0]>A.hi, dd=Math.round(above?z[0]-A.hi:A.lo-z[0]);
       tkChip(ctx,PW-236,above?TKTOP+25:TKTOP+priceH-42,
         (above?'↑ ':'↓ ')+z[2]+' 在畫面'+(above?'上':'下')+'方 '+dd+' 點處',z[1]);
     }
   });
 }
}
function tkDrawHover(ctx,xOf,yOf,PW,H,priceH){
 const D=TK.data, v=tkView();
 const t=v.t0+(TK.hover/PW)*(v.t1-v.t0);
 let i=tkBisect(D,t);
 if(i>0&&Math.abs(D.s[i-1]-t)<Math.abs(D.s[i]-t)) i--;
 const x=xOf(D.s[i]), y=yOf(D.c[i]);
 if(x<0||x>PW){ TK.hi=null; return; }
 ctx.save(); ctx.setLineDash([3,3]); ctx.strokeStyle='rgba(141,149,163,.55)'; ctx.lineWidth=1;
 ctx.beginPath(); ctx.moveTo(x+.5,TKTOP); ctx.lineTo(x+.5,H-TKBOT);
 ctx.moveTo(0,y+.5); ctx.lineTo(PW,y+.5); ctx.stroke(); ctx.restore();
 ctx.fillStyle='#E3A951'; ctx.beginPath(); ctx.arc(x,y,3,0,7); ctx.fill();
 ctx.fillStyle='#E3A951'; ctx.fillRect(PW,y-8,TKR,16);
 ctx.fillStyle='#0E1116'; ctx.save(); ctx.font='11.5px ui-monospace,monospace';
 ctx.fillText(D.c[i].toFixed(0),PW+8,y+4); ctx.restore();
 TK.hi=i;
}

/* ---------------- 周邊文字 ---------------- */
function tkLoading(){ return TK.pending||!TK.data||TK.data.date!==TK.date; }
function tkDayInfo(d){ return (TK.days||[]).find(x=>x.d===d)||null; }
/* 今天到底有沒有錄到。休市日、或面板今天還沒開過，today 根本不在清單裡 ——
   那時候按「今天」只會換來一張錯誤畫面。 */
function tkTodayOk(){ const x=tkDayInfo(TK.today); return !!(x&&!x.empty); }
function tkStep(dir){
 // ◀▶ 走的是**清單的索引**，不是「日期減一天」—— 沒有錄到的日子要選不到
 const L=(TK.days||[]).filter(x=>!x.empty);
 if(!L.length) return null;
 const i=L.findIndex(x=>x.d===TK.date);
 if(i<0) return dir<0?L[0].d:null;
 const j=i-dir;                         // days 是新到舊，往前一天＝索引 +1
 return (j>=0&&j<L.length)?L[j].d:null;
}
function tkPagerHTML(){
 const cur=TK.date, me=tkDayInfo(cur), loading=tkLoading();
 const D=(!loading&&TK.data)?TK.data:null;
 const back=tkStep(-1), fwd=tkStep(1);
 let r2;
 if(!cur) r2='<span>沒有逐筆紀錄</span>';
 else if(loading) r2='<span>載入中…</span>';
 else if(TK.err) r2='<span class="warn">讀不出來</span>';
 else r2='<span>'+(tkIsPolled()?'取樣':'逐筆')+' '+tkNum(D?D.n:null)+
   ' 筆</span><span class="sep">·</span>'+
   '<span>'+((D&&D.first)?(D.first.slice(0,8)+'~'+(D.last||'').slice(0,8)):'—')+'</span>'+
   ((D&&D.bad)?'<span class="sep">·</span><span class="warn">'+D.bad+' 列讀不出來</span>':'')+
   '<span class="sep k">·</span><span class="kbdgrp"><kbd>←</kbd><kbd>→</kbd> 換日</span>';
 return '<div class="pager">'+
  '<div class="r1">'+
  '<button class="nav-icon" data-tknav="-1" title="前一個有錄到的日子（←）"'+
    (back?'':' disabled')+'>◀</button>'+
  // ⚠️ 日期鈕的寬度不准隨狀態變：載入中只把日期轉灰（.loading），不可以換成「載入中…」
  //    —— 那會讓按鈕瞬間變寬約 33px，靠右對齊的 ◀ 被推出滑鼠底下。
  '<button class="dstamp'+(TK.pick?' open':'')+(loading?' loading':'')+
    '" data-tkpick="1" title="選日期（Esc 收合）"'+(cur?'':' disabled')+'>'+CAL_ICON+
    '<span class="num">'+(cur?cur.slice(5):'--/--')+'</span>'+
    '<span class="wd">'+(me?me.w:'')+'</span><span class="caret">▼</span></button>'+
  '<button class="nav-icon" data-tknav="1" title="後一個有錄到的日子（→）"'+
    (fwd?'':' disabled')+'>▶</button>'+
  /* 「今天」鈕與「即時」燈固定同寬、而且永遠只出現其中一顆 ——
     兩者寬度不一樣的話一按 ◀ 整條靠右對齊的 r1 就位移，連點時第 2 下會落到別的鈕上。
     ⚠️ 今天沒有錄到（休市／面板還沒開）時鈕要 disabled 而不是消失：
        消失＝整條位移，而且他會以為「今天」這條路不存在。 */
  ((cur&&cur===TK.today)
    ?'<span class="livelamp" title="今天"><i></i>今天</span>'
    :'<button class="jump2" data-tkday="'+TK.today+'"'+(tkTodayOk()?'':' disabled')+
     ' title="'+(tkTodayOk()?'回到今天（Home）':'今天還沒有紀錄')+'">今天</button>')+
  '</div><div class="r2">'+r2+'</div></div>';
}
function tkListHTML(){
 if(!TK.pick) return '';
 const L=TK.days||[];
 let rows=L.map(x=>{
   const D=TK.cache[x.d];
   // ⚠️ 沒載入過的日子**不顯示估算筆數** —— 這個專案不放沒把握的數字。
   //    筆數／涵蓋時段要真的切到那一天才由 /api/tick/day 給。
   const meta=x.empty?'只有檔頭，沒有錄到資料'
     :(D?(tkNum(D.n)+' 筆　'+((D.first||'').slice(0,8)+'~'+(D.last||'').slice(0,8))+
          (((D.gaps||[]).length||D.bad)?'　<span class="warn">⚠ 有缺口</span>':''))
        :'還沒載入');
   /* ⛔ 每一列都標種類（**兩種都標**，不是只標取樣那種）——
      他翻清單的時候要一眼分得出哪幾天是逐筆、哪幾天是取樣。
      只標其中一種的話，沒有標記等於「不知道」而不是「另一種」。 */
   const pol=(x.kind==='polled');
   const tag='<span class="kind'+(pol?' polled':'')+'" title="'+
     (pol?'tick_recorder.py 的取樣檔（約 0.5 秒一筆、沒有成交量）'
         :'面板逐筆落地的檔（每一筆成交與買賣價）')+'">'+(pol?'取樣':'逐筆')+'</span>';
   /* 同一天兩種檔都在：逐筆優先，另一份講出來（免得他以為那個檔不見了）。
      ⚠️ 字要短：舊版「＋另有取樣檔（未採用）」會把 .meta 擠到換行、
         那一列比別列高一截（.row 是 flex，換行就撐高）。長版說明放 title。 */
   const alt=x.alt?'<span class="alt" title="這天兩種檔都有，畫的是'+(pol?'取樣':'逐筆')+
     '檔；另一份'+(pol?'逐筆':'取樣')+'檔沒有採用">＋'+(pol?'逐筆':'取樣')+'檔</span>':'';
   return '<button class="row'+(x.d===TK.date?' on':'')+'" data-tkday="'+x.d+'"'+
     (x.empty?' disabled':'')+'><span class="dd">'+x.d.slice(5)+'</span>'+
     '<span class="wd">'+x.w+'</span>'+tag+
     '<span class="meta">'+meta+alt+'</span></button>';
 }).join('');
 if(!rows) rows='<div class="foot">還沒有任何逐筆紀錄。</div>';
 const foot=TK.since?('<div class="foot">更早以前沒有紀錄（'+TK.since+' 才開始錄）</div>'):'';
 /* ⛔ 被跳過的檔（檔名對、內容跟檔名對不起來）**在有資料的時候也要講**。
    舊版只把它寫在「一個紀錄都沒有」的空狀態裡 ⇒ 只要有任何一天有資料，
    「我明明有錄怎麼看不到」就完全無解（2026-09-07 lab-qa 退件 M1 的一部分）。 */
 const skip=(TK.skipped||[]).length
   ?('<div class="foot">⚠ 有 '+TK.skipped.length+' 個檔被跳過（'+
     TK.skipped.slice(0,3).join('、')+((TK.skipped.length>3)?' …':'')+
     '）：檔名對、但內容跟檔名不是同一種格式。原因印在面板的主控台，'+
     '檔案還在 tick_logs/，我們不改也不刪。</div>'):'';
 return '<div class="tk-list">'+rows+skip+foot+'</div>';
}
function tkToolsHTML(){
 const seg=(name,cur,items)=>'<div class="seg">'+items.map(it=>
   '<button class="'+(String(cur)===String(it[0])?'on':'')+'" data-'+name+'="'+it[0]+'">'+
   it[1]+'</button>').join('')+'</div>';
 const trades=tkAllTrades();
 const chip=(k,on,txt,dis,tip)=>'<button class="tk-chip'+(on?' on':'')+
   '" data-tkov="'+k+'"'+(dis?' disabled':'')+(tip?' title="'+tip+'"':'')+'>'+txt+'</button>';
 return '<span class="lab">圖種</span>'+
   seg('tkv',TK.v,[['C','秒K'],['A','折線'],['B','折線+價帶']])+
   (TK.v==='C'?('<span class="lab">桶寬</span>'+
     seg('tkbar',TK.bar,[['auto','自動'],[1,'1 秒'],[5,'5 秒'],[30,'30 秒']])):'')+
   '<span class="gap"></span>'+
   chip('trade',TK.ov.trade,'我的單',!trades.length,
        trades.length?'':'這天沒有下單')+
   chip('stop',TK.ov.stop,'±100',!trades.length,
        trades.length?'畫的是那一單的停利停損位置':'這天沒有下單')+
   /* ⛔ 取樣檔沒有單筆成交量 ⇒ 停用 ＋ **寫出原因**（跟「加權」那顆同一套處理）。
      ⛔⛔ 絕不可以拿 vol_ratio（累積量 ÷ 歷史中位）之類的東西湊一根假的量柱。 */
   chip('vol',TK.ov.vol&&tkHasVol(),'成交量',!tkHasVol(),
        tkHasVol()?'':'取樣檔只記價格與買賣價，沒有單筆成交量，這一頁畫不出量柱')+
   // 加權指數這顆一律停用。⛔ 不可以拿別的來源硬湊一條「看起來像」的線。
   // ⚠️ 兩種檔的原因不一樣，寫成同一句就會有一句是假的：逐筆檔真的沒有這一欄，
   //    取樣檔有（tick_recorder 有記 idx）—— 但這一頁沒有做這條線。
   chip('idx',false,'加權',true,
        tkIsPolled()?'取樣檔裡有加權指數，但這一頁沒有做這條線'
                    :'逐筆紀錄裡沒有加權指數，這一頁畫不出來')+
   chip('fixed',TK.ov.fixed,'時間軸固定',false,
        '固定 08:45~09:30 的框；關掉就只畫有資料的那段');
}
function tkReadHTML(){
 const D=TK.data;
 if(tkLoading()||!D||!D.len) return '<span class="lt">把游標移到圖上</span>';
 if(TK.hi==null||TK.hover==null) return '<span class="lt">把游標移到圖上　（滾輪縮放、拖曳平移、雙擊還原）</span>';
 const i=Math.min(TK.hi,D.len-1);
 const b=D.bl[i], a=D.ah[i];
 const w=tkBarSec();
 let o=D.o[i],h=D.h[i],l=D.l[i],c=D.c[i],vv=D.vq[i];
 if(TK.v==='C'&&w>1){
   const k=Math.floor(D.s[i]/w)*w, B=tkBars(), bar=B.find(x=>x.t===k);
   if(bar){ o=bar.o;h=bar.h;l=bar.l;c=bar.c;vv=bar.v; }
 }
 const up=c>=o;
 return '<span class="lt">時間</span><b>'+tkHMS(D.s[i])+'</b>'+
   (TK.v==='C'?'<span class="sep">·</span><span class="lt">桶</span><b>'+w+' 秒</b>':'')+
   '<span class="sep">·</span><span class="lt">開</span><b>'+o.toFixed(0)+'</b>'+
   '<span class="lt">高</span><b>'+h.toFixed(0)+'</b>'+
   '<span class="lt">低</span><b>'+l.toFixed(0)+'</b>'+
   '<span class="lt">收</span><b class="'+(up?'up':'down')+'">'+c.toFixed(0)+'</b>'+
   // ⛔ 取樣檔沒有量：寫「—」並講原因，不可以印 0 假裝那一秒沒成交
   '<span class="sep">·</span><span class="lt">量</span>'+
   (tkHasVol()?('<b>'+vv+'</b>'):'<b>—</b><span class="lt">取樣檔沒有量</span>')+
   ((isFinite(b)&&isFinite(a))
     ? '<span class="sep">·</span><span class="lt">買/賣</span><b>'+b.toFixed(0)+' / '+
       a.toFixed(0)+'</b><span class="lt">差 '+(a-b).toFixed(0)+'</span>'
     : '<span class="sep">·</span><span class="lt">買/賣</span><b>—</b>');
}
/* 副標的密度那三個字。⛔ 取樣日不可以寫「逐秒」——
   取樣中位 0.46 秒一筆、而且是「有變才記」，寫逐秒就是把取樣講成逐筆。 */
function tkDens(){ return tkIsPolled()?('取樣 '+tkRate()):'逐秒'; }
function tkSubHTML(){
 if(!TK.date) return '<span>08:45~09:30 · '+tkDens()+'</span>';
 if(tkLoading()) return '<span>'+TK.date+' · 08:45~09:30 · '+tkDens()+'</span>'+
   '<span class="sep">·</span><span>載入中…</span>';
 const D=TK.data;
 if(TK.err) return '<span>'+TK.date+'</span><span class="sep">·</span>'+
   '<span class="warn">這天的紀錄讀不出來（'+TK.err+'）</span>';
 const growing=(!D.complete&&D.date===TK.today);
 let out='<span>'+D.date+' · 08:45~09:30 · '+tkDens()+'</span>'+
   (tkIsPolled()?'<span class="sep">·</span><span class="warn">取樣資料，不是逐筆</span>'+
     /* ⛔ 取樣列只有一個 price，開高低收四個值都是它 ⇒ 那個「高低」是**取樣點**的極值，
        不是那一秒真正摸到的高低（實測 26.7% 的 1 秒桶高＝低）。金籤上也有同一句。 */
     '<span class="sep">·</span><span>高低＝取樣點的極值，不是那一秒真正的高低</span>':'')+
   '<span class="sep">·</span>'+
   (growing?'<span class="warn">錄製中 · 最後一筆 '+((D.last||'').slice(0,8)||'—')+'</span>'
           :'<span>已完成 · '+((D.first||'—').slice(0,8))+'~'+((D.last||'—').slice(0,8))+'</span>');
 // 只錄到一部分：⛔ 一定要說出「不是沒行情」
 const lateStart=D.len&&D.s[0]>30, earlyEnd=D.len&&!growing&&D.s[D.len-1]<TKSPAN-30;
 const holes=tkHoles();
 if(lateStart||earlyEnd||holes.length){
   const seg=lateStart?('08:45~'+((D.first||'').slice(0,5))):
     (holes.length?(tkHMS(holes[0][0]).slice(0,5)+'~'+tkHMS(holes[0][1]).slice(0,5)):
      (((D.last||'').slice(0,5))+'~09:30'));
   out+='<span class="sep">·</span><span class="warn">⚠ '+seg+
        (tkIsPolled()?' 沒有取樣到（沒錄到，或報價一直沒變）':' 沒有錄到（不是沒行情）')+
        '</span>';
 }
 const lost=(D.gaps||[]).reduce((a,g)=>a+(g.n||0),0);
 if(lost) out+='<span class="sep">·</span><span class="warn">佇列滿丟掉 '+lost+' 筆</span>';
 if(D.bad) out+='<span class="sep">·</span><span class="warn">有 '+D.bad+' 列讀不出來</span>';
 // 取樣當下面板沒有報價的那幾列（沒有價可畫）。⛔ 少了東西一定要有一個數字。
 if(D.nopx) out+='<span class="sep">·</span><span class="warn">有 '+D.nopx+
   ' 列取樣時沒有報價</span>';
 /* 08:45~09:30 之外被切掉的列（`tick_recorder.py --until 13:45` 錄到下午的那種檔）。
    ⛔ 這一頁只畫那 45 分鐘（一天最多 2,700 個桶是版面與效能的不變式），
       但**被切掉的部分一定要講出來** —— 安靜地少是這個專案明令禁止的。 */
 if(D.outwin) out+='<span class="sep">·</span><span class="warn">另有 '+
   D.outwin.toLocaleString()+' 列在 08:45~09:30 之外（這一頁不畫）</span>';
 if(D.heads>1) out+='<span class="sep">·</span><span>面板當天重啟 '+(D.heads-1)+' 次</span>';
 return out;
}
function tkFootHTML(){
 const D=(!tkLoading()&&TK.data)?TK.data:null;
 const growing=D&&!D.complete&&D.date===TK.today;
 const pol=tkIsPolled();
 return '<span>'+(pol?'取樣紀錄':'逐筆紀錄')+' · tick_logs/'+(TK.date||'YYYY-MM-DD')+
   (pol?'-polled':'')+'.jsonl</span>'+
   (pol?'<span class="sep">·</span><span>每 0.1 秒讀一次面板狀態、有變動才記一列'+
        '（tick_recorder.py）</span>':'')+
   (D?'<span class="sep">·</span><span>'+D.len.toLocaleString()+' 個 1 秒桶</span>':'')+
   // ⛔ 這句一定要寫出來：為了少這 1 秒去碰 on_tick／佇列＝去碰他的停損，
   //    所以這是刻意的取捨，不是缺陷。
   (growing?'<span class="sep">·</span><span>落地延遲約 1 秒（逐筆是每秒批次寫檔）</span>':'')+
   // ⚠️ 這一句刻意不寫「不做預測」四個字：頁尾那條免責已經寫了，而探針的禁詞掃描
   //    是掃 #tab-tick 的純文字、看不懂否定句 —— 讓一個否定句去污染紅線的尺不划算。
   '<span class="sep">·</span><span>只畫已經發生的事</span>';
}
/* 第一次打開一定看到的畫面就是空狀態 —— 上線當天合格的逐筆檔可能是 0 個，
   所以它不是邊角，是主流程。 */
function tkOverHTML(){
 if(tkLoading()&&TK.date)
   return '<div class="tk-skel">'+
     [38,52,44,61,55,70,64,48,57,72,66,80,74,59,68,52,63,47,58,66]
       .map(h=>'<i style="height:'+h+'%"></i>').join('')+'</div>';
 if(!TK.date){
   if((TK.days||[]).length===0)
     // ⚠️ 這段文案**不寫死任何日期**：它只在「一個逐筆檔都沒有」時出現，
     //    寫「明天 08:45」或某個特定日期，過幾天就變成假的（而且看不出來）。
     return '<div class="tk-empty"><div class="t">還沒有任何紀錄</div>'+
       '<div class="d">逐筆落地要面板重啟之後才開始；'+
       '之後每個交易日 08:45 一開盤就會自動錄，09:30 收手。</div>'+
       '<div class="d">檔案會落在 tools/shioaji/tick_logs/：'+
       'YYYY-MM-DD.jsonl 是逐筆，YYYY-MM-DD-polled.jsonl 是取樣（都不會上傳）。</div>'+
       ((TK.skipped||[]).length?'<div class="d">（有 '+TK.skipped.length+
         ' 個檔名對、但內容跟檔名對不起來的檔被跳過了）</div>':'')+'</div>';
   return '<div class="tk-empty"><div class="t">這天沒有紀錄</div>'+
     '<div class="d">用上面的 ◀ ▶ 或日期清單換到有錄到的日子。</div></div>';
 }
 if(TK.err)
   return '<div class="tk-empty"><div class="t">這天的紀錄讀不出來</div>'+
     '<div class="d">'+TK.err+'</div>'+
     '<button class="tk-chip go" data-tkact="retry">重試</button></div>';
 const D=TK.data;
 if(D&&!D.len){
   const growing=(!D.complete&&D.date===TK.today);
   return '<div class="tk-empty"><div class="t">'+
     (growing?'今天還沒開始錄（08:45 開始）'
             :(tkIsPolled()?'這個取樣檔裡一列都沒有':'這天只有檔頭，一筆成交都沒有錄到'))+'</div>'+
     '<div class="d">'+(growing?'開盤之後就會一秒一秒長出來。':'換到別的日子看看。')+'</div>'+
     (tkStep(-1)?'<button class="tk-chip go" data-tkday="'+tkStep(-1)+
       '">看上一個有錄的日子 →</button>':'')+'</div>';
 }
 // 使用者拖走過就給一顆「回到最新」，⛔ 不可以直接把他拉回去
 if(D&&!D.complete&&D.date===TK.today&&!TK.follow&&!TK.ov.fixed)
   return '<button class="tk-back" data-tkact="latest">回到最新</button>';
 return '';
}
function tkPaint(){
 if(TAB!=='tick') return;
 setEl('tksub',tkSubHTML());
 setEl('tkpager',tkPagerHTML());
 setEl('tkpick',tkListHTML());
 setEl('tktools',tkToolsHTML());
 setEl('tkread',tkReadHTML());
 setEl('tkfoot',tkFootHTML());
 const ov=tkOverHTML();
 setEl('tkover',ov);
 const cv=document.getElementById('tkcv');
 const blank=tkLoading()||!TK.date||TK.err||!(TK.data&&TK.data.len);
 if(cv) cv.style.visibility=blank?'hidden':'visible';
 if(!blank) tkDraw();
}
function tkGoDay(d){
 if(!d||d===TK.date&&!TK.pending) { TK.pick=false; tkPaint(); return; }
 TK.pick=false; TK.date=d; TK.err='';
 const c=TK.cache[d];
 if(c){ TK.data=c; TK.pending=false; TK.view=null; TK.follow=true; TK.hover=null;
        TK.hi=null; TKAXIS.key=null; TKBARC.key='';
        TK.sel=Math.max(0,c.trades.findIndex(t=>t.kind==='real')); tkPaint(); tkPoll(); }
 else { TK.pending=true; tkPaint(); tkFetchDay(d,false).then(tkPoll); }
}
function tkEnter(){
 tkPaint();
 if(!TK.booted){
   TK.booted=true;
   tkFetchDays().then(()=>{ tkPaint(); if(TK.date) tkFetchDay(TK.date,false).then(tkPoll); });
 }else{
   // 回到這一頁時重新看一次有哪幾天（今天那個檔可能剛被建出來）
   tkFetchDays().then(()=>{ tkPaint();
     if(TK.date&&!TK.cache[TK.date]) tkFetchDay(TK.date,false).then(tkPoll); else tkPoll(); });
 }
}
function tkClampView(){
 const F=tkFull(), v=TK.view, span=v.t1-v.t0, pad=span*0.5;
 if(v.t0<F.t0-pad){ v.t0=F.t0-pad; v.t1=v.t0+span; }
 if(v.t1>F.t1+pad){ v.t1=F.t1+pad; v.t0=v.t1-span; }
}
function tkBind(){
 const cv=document.getElementById('tkcv'); if(!cv) return;
 cv.addEventListener('wheel',e=>{
   if(TAB!=='tick'||tkLoading()) return;
   e.preventDefault();
   const v=tkView(), r=cv.getBoundingClientRect(), PW=TKC.W-TKR;
   const u=Math.min(1,Math.max(0,(e.clientX-r.left)/(r.width*(PW/TKC.W))));
   const anchor=v.t0+u*(v.t1-v.t0);
   const F=tkFull();
   // 縮放上限＝視窗 10 秒，下限＝完整 45 分鐘 ×1.2
   let span=Math.min((F.t1-F.t0)*1.2,Math.max(10,(v.t1-v.t0)*(e.deltaY>0?1.18:1/1.18)));
   TK.view={t0:anchor-u*span,t1:anchor+(1-u)*span};
   TK.follow=false; tkClampView(); tkPaint();
 },{passive:false});
 cv.addEventListener('pointerdown',e=>{
   if(TAB!=='tick'||tkLoading()) return;
   cv.setPointerCapture(e.pointerId);
   const v=tkView();
   TKDRAG={x:e.clientX,t0:v.t0,t1:v.t1,w:cv.getBoundingClientRect().width,moved:0};
 });
 cv.addEventListener('pointermove',e=>{
   if(TAB!=='tick') return;
   const r=cv.getBoundingClientRect();
   if(TKDRAG){
     const PW=(TKC.W-TKR)/TKC.W*r.width;
     const dt=(e.clientX-TKDRAG.x)/PW*(TKDRAG.t1-TKDRAG.t0);
     TKDRAG.moved+=Math.abs(e.clientX-TKDRAG.x);
     TK.view={t0:TKDRAG.t0-dt,t1:TKDRAG.t1-dt};
     if(Math.abs(dt)>0.5) TK.follow=false;
     tkClampView(); cv.style.cursor='grabbing'; tkPaint(); return;
   }
   TK.hover=(e.clientX-r.left)*(TKC.W/r.width);
   if(TK.hover>TKC.W-TKR){ TK.hover=null; TK.hi=null; }
   tkPaint();
 });
 cv.addEventListener('pointerup',()=>{ TKDRAG=null; cv.style.cursor='crosshair'; });
 cv.addEventListener('pointerleave',()=>{ TK.hover=null; TK.hi=null; tkPaint(); });
 cv.addEventListener('dblclick',()=>{ TK.view=null; TK.follow=true; TKAXIS.key=null; tkPaint(); });
 window.addEventListener('resize',()=>{ if(TAB==='tick') tkPaint(); });
 document.addEventListener('visibilitychange',()=>{ if(TAB==='tick') tkPoll(); });
}

document.addEventListener('click',function(e){
 if(TAB!=='tick') return;
 const nv=e.target.closest('[data-tknav]');
 if(nv){ tkGoDay(tkStep(parseInt(nv.getAttribute('data-tknav')))); return; }
 const pk=e.target.closest('[data-tkpick]');
 if(pk){ TK.pick=!TK.pick; tkPaint(); return; }
 const dy=e.target.closest('[data-tkday]');
 if(dy){ tkGoDay(dy.getAttribute('data-tkday')); return; }
 const vv=e.target.closest('[data-tkv]');
 // 三顆共用同一個視窗與同一套疊圖狀態：**切圖種不重設縮放**
 if(vv){ TK.v=vv.getAttribute('data-tkv');
   TK.ov.vol=(TK.v==='C');            // 折線與量柱是兩種語言，疊在全景下會吵
   TKAXIS.key=null; tkPaint(); return; }
 const bb=e.target.closest('[data-tkbar]');
 if(bb){ const x=bb.getAttribute('data-tkbar');
   TK.bar=(x==='auto')?'auto':parseInt(x); TKAXIS.key=null; tkPaint(); return; }
 const ov=e.target.closest('[data-tkov]');
 if(ov){ const k=ov.getAttribute('data-tkov');
   TK.ov[k]=!TK.ov[k];
   if(k==='fixed'){ TK.view=null; TK.follow=true; }
   TKAXIS.key=null; tkPaint(); return; }
 const ac=e.target.closest('[data-tkact]');
 if(ac){ const a=ac.getAttribute('data-tkact');
   if(a==='retry'){ TK.err=''; TK.pending=true; tkPaint(); tkFetchDay(TK.date,false); }
   if(a==='latest'){ TK.follow=true; tkFollow(); tkPaint(); }
   return; }
 if(TK.pick&&!e.target.closest('.tk-list')){ TK.pick=false; tkPaint(); }
});
document.addEventListener('keydown',function(e){
 if(TAB!=='tick') return;
 if(e.target.tagName==='INPUT'||e.target.tagName==='TEXTAREA') return;
 if(e.key==='ArrowLeft'){ e.preventDefault(); tkGoDay(tkStep(-1)); }
 else if(e.key==='ArrowRight'){ e.preventDefault(); tkGoDay(tkStep(1)); }
 else if(e.key==='Home'){ e.preventDefault(); if(tkTodayOk()) tkGoDay(TK.today); }
 else if(e.key==='Escape'&&TK.pick){ TK.pick=false; tkPaint(); }
});
tkBind();

/* ══════════════ 【程式下單】分頁：四種方向判斷的模擬對照（Canvas）══════════════

   這一頁回答一個問題，而且只有這一個：**「判斷方向」到底有沒有加分？**
   四種算法唯一的差別是方向怎麼決定，時刻／口數／±100／出場規則全部一樣。

   ⛔⛔ **永遠只是模擬，一張單都不會送出去。** 這一段一行都不碰 /api/enter、
        /api/real/*，也不產生任何 [data-act] / [data-rdir]。
   ⛔ 紅線比別頁嚴格：
      ・不做跨算法的綜合／多數決／共識／「四個裡有幾個看多」——那就是訊號強度。
      ・不排四格的優劣、不把「目前成績最好的那個」標起來。
      ・09:03:30 之前不顯示方向（不預告）。
      ・不到 30 筆不給勝率 %；不到 10 筆不畫累計線。
      ・**顏色只給損益**：方向、訊號值、筆數、門檻、天數一律中性色。
   ⚠️ 這一頁的資料**不掛在 500ms 的 tick 上**，只在切進來／換天／按窗口時抓。
*/
const ATSEC0=8*3600+45*60;          // 08:45:00（x 的原點）
const ATEND=13*3600+45*60;          // 13:45:00 日盤收盤＝模擬單的強制平倉（PM 拍板）
const ATSIG=9*3600+3*60+30;         // 09:03:30
const ATWATCH=9*3600+30*60;         // 09:30 他自己收手（**不是**模擬單的出場條件）
const ATSPAN=ATEND-ATSEC0;          // 18,000 秒
const ATR=64, ATTOP=12, ATBOT=26;   // 右側價格軸寬／上下留白（沿用面板）
const ATLANEH=22, ATLANEG=12;       // 一條泳道的高度／價格區與泳道之間的間隙
const ATZ=2.80, ATSIGMA=96.6;       // 後端沒回時的退路，正本在 live_panel.py 的常數
/* ⛔ ATKEYS 是**資料層**的 key（jsonl 的欄位、後端 rows 的鍵），⛔ 一個都不准上畫面 ——
   要顯示一律走 atName()（見下面 AT_NAME）。這裡的順序就是 AT_ORDER。 */
const ATKEYS=['A','B','C','D'];
const ATCOL={text:'#E9ECF1',dim:'#8D95A3',faint:'#5C6472',ghost:'#39414F',
             line:'#242C38',gold:'#E3A951',up:'#EE5A54',down:'#34B37E',bg:'#0E1116'};
const ATFONT='11px ui-monospace,"Microsoft JhengHei",monospace';
/* 泳道名字用 sans、600、⛔ 不准比 11px 更小（規格 §9.3：11px 是名字的下限，
   v2 那個 10px mono 是**代號**才擠得下的字級）。 */
const ATFONTN='600 11px "Microsoft JhengHei",ui-sans-serif,system-ui,sans-serif';
const ATLANEH2=30;                  // 堆疊排法（窄視窗）的列高：名字一列＋色帶一列
var ATLN={L:0,nw:0,laneH:ATLANEH,stacked:false};   // 最後一次量出來的泳道版面（探針會讀）
/* ⛔⛔ 名字（規格 §2.0，這一節優先於規格其他所有措辭；v2 被 Benson 退件過一次，
   他的原話：「我要從哪裡知道現在我看的是哪個做法？」）：
   **畫面上不准出現 A／B／C／D**，代號只活在資料層（jsonl 的 key、後端 rows 的 key）。
   ⛔ 不准自己改字、不准加「法／策略／模式」後綴、⛔ 不准為了塞得下而縮寫
      （「開盤・30」「開30」都不行 —— 放不下就改版面，見 atLaneNameW()）。
   ⛔ 不准在畫面任何地方留「A＝5 分 K」這種對照說明：需要那行字就代表命名失敗了。
   ⚠️ C 的名字**自帶門檻值**（門檻改 80 就變「開盤起・要 80 點」）⇒ 一定要走 atName()，
      ⛔ 不准把字串散在各處硬寫（否則改一次門檻要改七個地方）。
   ⚠️ 門檻的正本是後端常數 C_THRESH（隨 /api/auto/day 與 /api/auto/stats 送過來）；
      「換一個門檻看看」那支滑桿⛔ 不准改動任何地方的名字（它只試算，真正在跑的還是 C_THRESH）。 */
const AT_NAME={A:'5 分 K',B:'開盤起',C:'',D:'不判斷'};
const AT_SUB ={A:'09:00 起算',B:'08:45 起算',C:'08:45 起算・不夠就不做',D:'一律做多（基準）'};
const AT_ORDER=ATKEYS;                 // ⛔ 四處（今天卡／成績表／泳道／累計圖）一律同序
function atThr(){
 const v=atN((AT.stats||{}).thresh); if(v!=null) return v;
 const w=atN((AT.data||{}).thresh); if(w!=null) return w;
 return AT.thr;
}
function atName(k){ return k==='C'?('開盤起・要 '+atThr()+' 點'):(AT_NAME[k]||k); }
function atSub(k){ return AT_SUB[k]||''; }
const ATEXIT={tp:'停利',sl:'停損',eod:'收盤平'};
/* ⛔⛔ 沒有結算的日子有**兩種**，畫面上一定要分得出來（2026-09-08 修）：
   ・持倉中 ＝ 還沒摸到 ±100，而且那天的日盤還沒收 —— 正常的等待，不是異常。
   ・結算中 ＝ 日盤收了（或那是過去的日子），該算了卻還沒算出來 —— 該注意。
   以前只有「結算中」，而後端會拿**盤中最後一根 K 棒的收盤價**硬算出一個 eod ⇒
   畫面上是一個看起來很正常的假成績（他 2026-09-08 早上真的看到四條「09:05 收盤平」）。
   ⛔ 持倉中**只寫「持倉中」＋進場價，不算浮動損益**（規格 §16-3 拍板：
      會跳的模擬損益是這一頁最容易讓他開始盯盤的東西，而且沒有任何用途）。 */
function atSettleWord(o){
 if(!o) return '結算中';
 const done=o.runs?true:(o.done===true);
 return done?'已結算':(o.holding?'持倉中':'結算中');
}
const ATWHY={no_quote:'09:03:30 收不到報價（斷線、休市或國定假日）',
  quote_stale:'09:03:30 那一刻的報價太舊，沒有拿它記（拿舊價記＝假成績）',
  mid_only:'那一刻只有買賣價、還沒有成交價，沒有拿中價頂替',
  late:'09:03:30 的時候面板沒開著（或剛重啟），那一刻的價已經過去了',
  unknown:'原因沒有記下來'};
/* AT.date（想看哪天）與 AT.data.date（手上這份是哪天）**必須是兩個欄位** ——
   換日到新資料回來之間那一秒，只有一個欄位的話會把昨天的資料掛在今天的日期底下。 */
var AT={date:'',data:null,days:[],missing:[],today:'',now:'',nowAt:0,trackN:505,
        seq:0,sseq:0,pending:false,err:'',stats:null,swin:20,zoom:false,pick:false,
        booted:false,cache:{},adv:30,drawn:0,
        /* thr＝C 的門檻（名字自己帶著它）。⛔ 後端常數 C_THRESH 才是正本，
           這裡的 30 只是還沒拿到回應時的退路；serr／tickErr 是主迴圈那道 try
           攔下來的錯，⛔ 一定要畫在畫面上（不可以安靜地吞）。 */
        thr:30,serr:'',tickErr:0};
var ATC={W:0,H:0,DPR:0}, ATCC={W:0,H:0,DPR:0};

const atN=x=>(typeof x==='number'&&isFinite(x))?x:null;
function atSecOf(hm){
 if(typeof hm!=='string'||hm.length<5||hm[2]!==':') return null;
 const h=parseInt(hm.slice(0,2),10), m=parseInt(hm.slice(3,5),10);
 const s=(hm.length>=8&&hm[5]===':')?parseInt(hm.slice(6,8),10):0;
 if(!isFinite(h)||!isFinite(m)||!isFinite(s)) return null;
 return h*3600+m*60+s;
}
function atHM(sec){
 const t=Math.round(sec)+ATSEC0;
 return String(Math.floor(t/3600)).padStart(2,'0')+':'+
        String(Math.floor(t/60)%60).padStart(2,'0');
}
/* ⛔ 判斷「時刻到了沒」一律用**後端時鐘**當基準（跨午夜時瀏覽器與後端會不同一天，
   面板已經為這件事踩過一次）。瀏覽器的時鐘只用來算「從拿到那一刻起過了幾秒」。 */
function atNowSec(){
 const b=atSecOf(AT.now);
 if(b==null) return null;
 return b+Math.max(0,(Date.now()-AT.nowAt)/1000);
}
/* 天花板 ＝「這批資料至少要差多少，才有八成機率被抓到」＝ z·σ/√n。
   σ 是 ±100 停利停損規則下單筆點數的標準差，由後端量出來（實測 96.6）。 */
function atCeiling(n){
 if(!(n>0)) return null;
 const S=AT.stats||{}, s=atN(S.sigma)||ATSIGMA, z=atN(S.z)||ATZ;
 return Math.round(z*s/Math.sqrt(n)*10)/10;
}
/* 累計點數的「證不出來帶」半寬 ＝ 天花板(n)×n。**跟 atCeiling 同一個 z 與 σ** ——
   線還在帶子裡 ⇔ 每筆沒超過天花板，圖跟表講的必須是同一句話。 */
function atBand(n){
 if(!(n>0)) return 0;
 const S=AT.stats||{}, s=atN(S.sigma)||ATSIGMA, z=atN(S.z)||ATZ;
 return z*s*Math.sqrt(n);
}

/* ---------------- 取資料 ---------------- */
function atFetchDays(){
 return fetch('/api/auto/days').then(r=>r.json()).then(x=>{
   AT.days=(x&&x.days)||[]; AT.missing=(x&&x.missing)||[];
   AT.today=(x&&x.today)||''; AT.now=(x&&x.now)||''; AT.nowAt=Date.now();
   AT.trackN=(x&&x.track_n)||505;
   if(atN(x&&x.thresh)!=null) AT.thr=x.thresh;      // 名字自己帶著門檻，正本在後端
   /* ⛔ 主迴圈那道 try 攔下來的錯要**上畫面**（2026-09-07 退件 R5）：
      吞在後端變數裡而畫面上一個字都沒有，跟安靜地吞沒有差別。 */
   AT.serr=(x&&x.err)||''; AT.tickErr=(x&&x.tick_err)||0;
   if(!AT.date) AT.date=AT.today||((AT.days[0]||{}).d)||'';
 }).catch(()=>{ if(!AT.days) AT.days=[]; });
}
function atFetchStats(){
 const my=++AT.sseq;
 return fetch('/api/auto/stats?win='+AT.swin+'&src=live').then(r=>r.json()).then(x=>{
   if(my!==AT.sseq) return;      // 連按窗口鈕時只認最後一次
   AT.stats=x;
   if(atN(x&&x.thresh)!=null) AT.thr=x.thresh;
   if(x&&('err' in x)) AT.serr=x.err||'';
   if(x&&('tick_err' in x)) AT.tickErr=x.tick_err||0;
   atPaintStats();
 }).catch(()=>{});
}
function atFetchDay(d){
 /* ⛔ 換日請求要帶流水號，只認最後一次的回應。連按 ◀ 時先送的請求可能後回來，
    舊那天的資料會蓋回去；而且快取的日期跟當前選擇又剛好對得上 ⇒
    「載入中」那道守衛判定不出來（面板 2026-08-23 實測過同一個坑）。 */
 const my=++AT.seq;
 AT.pending=true;
 return fetch('/api/auto/day?date='+encodeURIComponent(d))
  .then(r=>r.json().then(j=>({ok:r.ok,j:j}))).then(res=>{
   if(my!==AT.seq) return;
   if(!res.ok){
     if(d===AT.date){ AT.err=(res.j&&res.j.error)||'讀不出來'; AT.pending=false; AT.data=null; }
     atPaint(); return;
   }
   AT.cache[d]=res.j;
   const ks=Object.keys(AT.cache);
   if(ks.length>3){ ks.sort(); for(const k of ks.slice(0,ks.length-3)) if(k!==d) delete AT.cache[k]; }
   if(d===AT.date){
     AT.err=''; AT.pending=false; AT.data=res.j;
     if(res.j.now){ AT.now=res.j.now; AT.nowAt=Date.now(); }
     if(res.j.today) AT.today=res.j.today;
   }
   atPaint();
  }).catch(()=>{
   if(my!==AT.seq) return;
   if(d===AT.date){ AT.pending=false; AT.err='連不上面板'; }
   atPaint();
  });
}

/* ---------------- 翻頁 ---------------- */
function atLoading(){ return AT.pending||!AT.data||AT.data.date!==AT.date; }
function atDayInfo(d){ return (AT.days||[]).find(x=>x.d===d)||null; }
/* ◀▶ 走的是**清單的索引**，不是「日期減一天」—— 沒有記錄的日子要選不到。
   今天永遠在清單第一個（就算還沒記錄）：他早上開面板看到的預設就是今天。 */
function atNavList(){
 const L=(AT.days||[]).map(x=>x.d);
 if(AT.today&&L.indexOf(AT.today)<0) L.unshift(AT.today);
 return L;
}
function atStep(dir){
 const L=atNavList(); if(!L.length) return null;
 const i=L.indexOf(AT.date);
 if(i<0) return dir<0?L[0]:null;
 const j=i-dir;                      // 清單是新到舊，往前一天＝索引 +1
 return (j>=0&&j<L.length)?L[j]:null;
}
function atPagerHTML(){
 const cur=AT.date, me=atDayInfo(cur), loading=atLoading();
 const D=(!loading&&AT.data)?AT.data:null;
 const back=atStep(-1), fwd=atStep(1);
 let r2;
 if(!cur) r2='<span>還沒有任何一天</span>';
 else if(loading) r2='<span>載入中…</span>';
 else if(AT.err) r2='<span class="warn">讀不出來</span>';
 else if(!D||D.px==null) r2='<span>這天沒有記錄</span>';
 else r2='<span>'+(D.at||'09:03:30')+' 進場 '+atN(D.px).toFixed(1)+'</span>'+
   /* ⛔ 「持倉中」與「結算中」是兩件事，不可以寫同一句（2026-09-08）：
      前者＝還沒摸到 ±100、日盤也還沒收，那是正常的等待；
      後者＝該算了卻還沒算出來。⛔ 持倉中**不給浮動損益**（規格 §16-3）——
      這一行只寫進場價，不寫「現在賺賠多少」。 */
   '<span class="sep">·</span><span>'+atSettleWord(D)+'</span>'+
   '<span class="sep k">·</span><span class="kbdgrp"><kbd>←</kbd><kbd>→</kbd> 換日</span>';
 return '<div class="pager">'+
  '<div class="r1">'+
  '<button class="nav-icon" data-atnav="-1" title="前一個有記錄的日子（←）"'+
    (back?'':' disabled')+'>◀</button>'+
  // ⚠️ 日期鈕的寬度不准隨狀態變：載入中只把日期轉灰（.loading），不可以換成「載入中…」
  //    —— 那會讓按鈕瞬間變寬，靠右對齊的 ◀ 被推出滑鼠底下。
  '<button class="dstamp'+(AT.pick?' open':'')+(loading?' loading':'')+
    '" data-atpick="1" title="選日期（Esc 收合）"'+(cur?'':' disabled')+'>'+CAL_ICON+
    '<span class="num">'+(cur?cur.slice(5):'--/--')+'</span>'+
    '<span class="wd">'+(me?me.w:'')+'</span><span class="caret">▼</span></button>'+
  '<button class="nav-icon" data-atnav="1" title="後一個有記錄的日子（→）"'+
    (fwd?'':' disabled')+'>▶</button>'+
  ((cur&&cur===AT.today)
    ?'<span class="livelamp" title="今天"><i></i>今天</span>'
    :'<button class="jump2" data-atday="'+AT.today+'"'+(AT.today?'':' disabled')+
     ' title="回到今天（Home）">今天</button>')+
  '</div><div class="r2">'+r2+'</div></div>';
}
function atListHTML(){
 if(!AT.pick) return '';
 const L=atNavList();
 let rows=L.map(d=>{
   const x=atDayInfo(d);
   let meta;
   if(!x) meta='今天　還沒有記錄';
   else{
     const dd=x.dirs||{};
     /* ⛔ 這裡也不准出現代號（規格 §2.0）。清單是 width:max-content ＋ max-width，
        字長了是整塊橫向捲，不會把某一列撐高。 */
     const t=AT_ORDER.map(k=>atName(k)+' '+
       (dd[k]==null?'—':(dd[k]===0?'—':(dd[k]>0?'多':'空')))).join('　');
     meta=t+'　'+(x.net==null?atSettleWord(x):('當天合計 '+pm(x.net,0)));
   }
   return '<button class="row'+(d===AT.date?' on':'')+'" data-atday="'+d+'">'+
     '<span class="dd">'+d.slice(5)+'</span>'+
     '<span class="wd">'+(x?x.w:'')+'</span>'+
     '<span class="meta">'+meta+'</span></button>';
 }).join('');
 if(!rows) rows='<div class="foot">還沒有任何一天的模擬紀錄。</div>';
 /* ⛔ 沒有記錄的日子要看得見、也要看得到原因 ——「我明明有開面板怎麼沒錄到」
    如果無解，他就會開始不相信這一頁的數字。 */
 const ms=(AT.missing||[]).length
   ?('<div class="foot">⚠ 有 '+AT.missing.length+' 天沒有記錄：'+
     AT.missing.slice(0,3).map(m=>m.d.slice(5)+'（'+(ATWHY[m.why]||m.why)+'）').join('、')+
     ((AT.missing.length>3)?' …':'')+'</div>'):'';
 return '<div class="tk-list">'+rows+ms+'</div>';
}
function atToolsHTML(){
 /* ⛔ 預設畫**整個日盤**：±100 大多數日子在 09:30 之前根本摸不到，
    只畫 08:45~09:30 的話四條泳道全部跑出畫面右邊（demo 第一版實測）。 */
 return '<button class="at-chip'+(AT.zoom?' on':'')+'" data-atzoom="1"'+
   ' title="放大到他自己的下單時段。⚠ ±100 常常要到中午才摸到，出場會落在畫面外">'+
   '只看 08:45~09:30</button>'+
   '<span class="lab">'+(AT.zoom?'08:45~09:30':'08:45~13:45')+'　1 分 K</span>';
}
function atDateHTML(){
 const d=AT.date; if(!d) return '';
 const x=atDayInfo(d);
 return d.slice(5)+(x?('（'+x.w+'）'):'')+(d===AT.today?' 今天':'');
}
function atSubHTML(){
 const D=(!atLoading()&&AT.data)?AT.data:null;
 if(atLoading()) return '<span>載入中…</span>';
 if(AT.err) return '<span class="warn">'+AT.err+'</span>';
 if(!D||D.px==null) return '<span>這天沒有 09:03:30 的記錄</span>';
 const bits=['四條共用同一個進場價 '+atN(D.px).toFixed(1)];
 /* ⚠️ 這句話必須跟 _auto_settle 真的用到的第一根一致（2026-09-07 lab-qa 退件 R2：
    舊版比較用 `>`，實際上是從 09:05 那根才開始算，這句話是**假話**）。
    標籤是起始時間 ⇒ 09:04 那根涵蓋 09:04~09:05，「含」這個字不可以省。 */
 if(D.settle_from) bits.push('出場從 '+D.settle_from+' 那根 1 分 K（含）開始算');
 /* ⛔ 持倉中只講狀態，⛔ 不算「現在賺賠多少」（規格 §16-3）。 */
 if(D.holding) bits.push('還沒摸到 ±100，日盤還沒收 ⇒ 持倉中');
 /* ⚠️ 這句講的是**圖上那幾張「真 ▲ 多」籤**（他當天真的下的單畫在 K 線上），
    跟被拿掉的成績表那兩列不是同一件事 ⇒ 保留。
    ⛔ 但用字改成「真實單」跟圖上的籤一致 —— 「你自己」這四個字已經是
    「成績表那兩列」的專屬字樣，畫面上留著會讓守衛（㉔ 零命中）分不出來。 */
 if((D.mine||[]).length) bits.push('真實單 '+D.mine.length+' 筆');
 return bits.map(b=>'<span>'+b+'</span>').join('<span class="sep">·</span>');
}

/* ---------------- 時態鎖：⛔ 時刻過了才顯示方向，不預告 ---------------- */
function atSigPassed(){
 /* 過去的日子當然過了；今天要看**後端時鐘**。
    ⛔ 09:03:30 之前不可以顯示「目前 A 會判斷做多」「還有 2 分 30 秒」這類東西 ——
       那是預測，而且是最像「建議」的一種形狀。 */
 if(AT.date!==AT.today) return true;
 const s=atNowSec();
 return s==null?false:(s>=ATSIG);
}
function atTodayHTML(){
 const D=AT.data;
 if(atLoading()||!D) return '';
 if(!atSigPassed())
   return '<div class="wait"><div class="t">今天 '+(D.signal_at||'09:03:30')+' 還沒到</div>'+
     '<div class="d" title="時刻還沒到就顯示「等一下會判斷什麼方向」，那是預測 —— '+
     '而且是最像建議的一種形狀。這一頁不做那件事。">時刻到了才會記下方向。</div></div>';
 if(D.px==null){
   const why=D.miss?(ATWHY[D.why]||D.why||ATWHY.unknown)
     :'還沒寫進紀錄（或那時候面板沒開著）';
   return '<div class="wait"><div class="t">'+(D.date===AT.today?'今天':'這天')+'沒有記錄</div>'+
     '<div class="d">'+why+'</div></div>';
 }
 return AT_ORDER.map(k=>{
   const dr=D.dirs?D.dirs[k]:null;
   const sg=(k==='A')?atN((D.sig||{}).A):(k==='D'?null:atN((D.sig||{}).B));
   const run=(D.runs||{})[k];
   /* ⛔ 方向一律中性色（.dir）。這是全站唯一「程式現在說了什麼方向」的地方，
      染成紅綠會讓它看起來像一個令人興奮的訊號。紅綠只給點數。 */
   let dtxt='—', cls='';
   if(dr==null){ dtxt='算不出訊號'; cls=' off'; }
   else if(dr===0){ dtxt='這天不做'; cls=' off'; }
   else dtxt=(dr>0?'做多':'做空');
   /* ⚠️ C 這裡**不再重複寫門檻** —— 名字裡已經寫著「要 N 點」了（規格 §9.3 同一條）。 */
   const sgt=(k==='D')?'不判斷方向'
     :(sg==null?'訊號 —':('訊號 '+pm(sg,1)+' 點'));
   let rs;
   /* ⛔ 還沒摸到 ±100、日盤也還沒收 ⇒ 「持倉中」，⛔ 不可以是一個假的點數
      （2026-09-08：舊版拿盤中最後一根 K 棒的收盤價算出「09:05 收盤平 ±67」）。 */
   /* ⚠️ 還沒結算的時候也要分得出「這天根本沒下單」：C 沒超過門檻的日子寫「持倉中」
      是假話（它沒有部位）。方向那一欄已經有 dirs，不必等 settle 才知道。 */
   if(!run) rs=(dr===0?'沒超過門檻，這天不下單'
              :(dr==null?'—':(D.holding?'持倉中':'結算中')));
   else if(run.dir===0) rs='沒超過門檻，這天不下單';
   else if(run.dir==null) rs='—';
   else rs=(run.exit_at||'—')+' '+(ATEXIT[run.why]||'—')+
     ' <span class="'+sgn(run.pts)+'">'+pm(run.pts,0)+'</span> 點';
   return '<div class="c"><div class="k"><b>'+atName(k)+'</b>'+atSub(k)+'</div>'+
     '<div class="dir'+cls+'">'+dtxt+'</div>'+
     '<div class="sg">'+sgt+'</div><div class="rs">'+rs+'</div></div>';
 }).join('');
}

/* ---------------- 成績表 ---------------- */
function atCountHTML(){
 const S=AT.stats; if(!S) return '';
 return S.n+' 個交易日'+(S.since?('　'+S.since.slice(5)+'~'+(S.until||'').slice(5)):'');
}
function atWinHTML(){
 /* ⚠️ 按筆數去重，只剩一個就整條不畫（沿用真實區 realWindows() 的做法）——
    上線第一個月四顆會是同一批資料，按了畫面完全不動比沒有這排按鈕更糟。 */
 const S=AT.stats;
 const tot=(S&&S.notes&&S.notes.days_total)||(AT.days||[]).length;
 const seen={}, out=[];
 for(const it of [[10,'近10'],[20,'近20'],[60,'近60'],[0,'全部']]){
   const eff=it[0]?Math.min(it[0],tot):tot;
   if(seen[eff]) continue;
   seen[eff]=1; out.push(it);
 }
 if(out.length<2) return '';
 return out.map(it=>'<button class="'+(AT.swin===it[0]?'on':'')+
   '" data-atwin="'+it[0]+'">'+it[1]+'</button>').join('');
}
function atRateCell(r){
 /* ⛔ **樣本少的時候不給百分比。** 3 筆 2 勝顯示「67%」看起來像一個測量結果，
    其實是三次擲銅板 —— 一個看起來可信卻無效的數字，比明顯沒用的更危險。
    30 這個門檻不是統計上的魔法數字，是「一眼看得出是運氣」與「人會開始相信」的分界。 */
 const min=(AT.stats&&AT.stats.rate_min_n)||30;
 if(!r.n||r.n<min) return '<td class="rate na">不到 '+min+' 筆，不算 %</td>';
 return '<td class="rate">'+r.rate+'%</td>';
}
function atProveCell(r){
 const c=r.ceiling;
 if(c==null||r.avg==null) return '<td><span class="tag">還不夠</span></td>';
 return '<td>'+(r.over
   ?'<span class="tag over" title="一次超過不等於證明">超過 '+c+' 點</span>'
   :'<span class="tag">在雜訊裡（&lt;'+c+' 點）</span>')+'</td>';
}
function atRow(k,name,sub,r,extra,cls){
 const pts=r.pts||0;
 return '<tr'+(cls?(' class="'+cls+'"'):'')+'>'+
  '<td class="nm">'+name+'<i>'+sub+'</i></td>'+
  '<td>'+(r.n||0)+(extra||'')+'</td>'+
  '<td>'+(r.w||0)+'–'+(r.l||0)+'</td>'+
  atRateCell(r)+
  '<td class="pts '+sgn(pts)+'">'+pm(pts,0)+'</td>'+
  '<td class="avg '+sgn(r.avg||0)+'">'+(r.avg==null?'—':pm(r.avg,1))+'</td>'+
  atProveCell(r)+'</tr>';
}
function atTblHTML(){
 const S=AT.stats;
 if(!S||!S.rows) return '';
 /* ⚠️ 對 Benson 一律叫「做法」，⛔ 不叫「算法／策略／模型」（規格 §2 的用字規矩）。 */
 let html='<tr><th>做法</th><th>筆數</th><th>勝–敗</th><th>勝率</th>'+
   '<th>累計點數</th><th>每筆</th><th>這批資料證得出來嗎</th></tr>';
 for(const k of AT_ORDER){
   const r=S.rows[k];
   /* ⛔ C 那一列的筆數要寫成「3（+17 天沒做）」，只寫 3 會讓人以為只累積了 3 天。 */
   const extra=(k==='C'&&r.skip)?(' <span class="sub">(+'+r.skip+' 天沒做)</span>'):'';
   /* ⛔ 第一欄是**名字**不是代號，底下那行小字是 AT_SUB（規格 §2.0 的名字表）。
      ⚠️ 那行小字有守衛在盯（autotest-tab.mjs ㉑）—— lab-qa 把它清空過，134/134 全綠。 */
   html+=atRow(k,atName(k),atSub(k),r,extra);
 }
 /* ⛔⛔ 「你自己（全部）」與「你自己（同口徑）」那兩列、連同上面那條
    「▼ 你自己真的做的（口徑不同…）」分隔，已於 2026-09-08 依 Benson 指示拿掉
    （原話：「然後我自己的這邊都拿掉」）。⛔ 不要再加回來。
    ⚠️ 後端 S.mine 照算、`_auto_mine_day`／`_auto_mine_rows` 與它們的
       「只讀不寫」守衛（autotest-backend.py ⑧c）**一行都不准刪** —— 他之後可能會
       想加回來，而那道守衛擋的是「這一頁會不會寫到他的真實紀錄」，跟畫不畫無關。
    守衛：autotest-tab.mjs ㉔ —— 成績表只有四列、DOM ＋ 兩張 canvas 上「你自己」零命中。 */
 return html;
}
function atCeilHTML(){
 const S=AT.stats; if(!S) return '';
 const n=S.n||0, c=atCeiling(n);
 if(!n||c==null)
   return '<span class="dot">●</span><span>0 筆 ⇒ 什麼都測不出來。</span>';
 // ⛔ 這一行也不准出現代號（規格 §2.0 ⑥「目前『X』超過這條線」）
 const over=AT_ORDER.filter(k=>S.rows&&S.rows[k]&&S.rows[k].over).map(k=>'「'+atName(k)+'」');
 return '<span class="dot">●</span>'+
  '<span>'+n+' 筆 ⇒ 只分得出「每筆差 <b>'+c+'</b> 點以上」。</span>'+
  '<span class="hit">'+(over.length
    ?('目前 '+over.join('、')+' 超過這條線（一次超過不等於證明）')
    :'目前沒有一條超過')+'</span>';
}
function atNotesHTML(){
 /* ⛔⛔ 2026-09-08 Benson：「下面這個我根本看不懂是在幹嘛的」⇒ **帳本那一行拿掉**
    （`同一根同時摸到 ±100 … 檔案 N 列＝訊號＋結算＋沒錄到＋重複＋讀不出來`）。
    ⚠️ 但**不可以整條刪掉**：這個專案明令禁止「安靜地少」，而
       `sig+settle+miss+dup+bad ＝ 總列數` 那條不變式與它的守衛都還在後端照算。
    ⇒ 拆成兩種：
       ・**常態統計**（帳本五項總和／收盤平幾筆／持倉中幾筆）＝ 他看不懂也不需要
         ⇒ ⛔ 不畫。日期清單、泳道、今天卡本來就一天一天寫得清清楚楚。
       ・**異常**（讀不出來／重複／沒有記錄的天／歷史回填／保守算停損／還沒結算／出錯）
         ⇒ **只在 > 0 的時候出現**。正常情況下這一整行是空的（.at-notes:empty 隱藏），
         他看不到任何東西；真的少了什麼的時候它會自己冒出來。
    ⛔ 不要把它改回「無論如何都印一行」，那就是被退件的那一行。 */
 const S=AT.stats; if(!S||!S.rows) return '';
 const both=ATKEYS.reduce((a,k)=>a+(S.rows[k].both||0),0);
 const pend=ATKEYS.reduce((a,k)=>a+(S.rows[k].pending||0),0);
 const N=S.notes||{}, bits=[];
 /* ⛔ 「同一根同時摸到 ±100」是**我們替他做了一個對他不利的假設**（保守算停損），
    真的發生時一定要講出來（CLAUDE.md）；沒發生就不用佔他一行字。 */
 if(both) bits.push('<span class="warn">同一根同時摸到 ±100（保守算停損）'+both+' 筆</span>');
 if(pend) bits.push('<span class="warn">還沒結算 '+pend+' 筆</span>');
 if(N.missing) bits.push('<span class="warn">沒有記錄 '+N.missing+' 天</span>');
 if(N.backfill) bits.push('<span class="warn">歷史回填 '+N.backfill+
   ' 筆（⛔ 不併入上面）</span>');
 /* ⛔ 檔案裡讀不出來／重複的列數是「有沒有東西被安靜吃掉」唯一看得見的出口 ——
    ⛔ 這兩個不可以跟著帳本一起拿掉，只是改成「有才寫」。 */
 const led=S.ledger||{};
 if(led.bad) bits.push('<span class="warn">檔案有 '+led.bad+' 列讀不出來（已排除）</span>');
 if(led.dup) bits.push('<span class="warn">檔案有 '+led.dup+' 列重複（已排除）</span>');
 /* ⛔⛔ 主迴圈那道 try 攔下來的錯**一定要在這裡看得見**（2026-09-07 lab-qa 退件 R5）：
    後端一直有在數（AUTO["tick_err"]）也有 console 警告，但 console 只印前 3 次、
    而他不會去看主控台 ⇒ 對「使用畫面的人」來說那還是安靜地吞掉了。
    ⛔ 出錯不是 0 的時候不可以只寫次數就算了，最後一個錯誤的原文也要寫出來。 */
 const te=atN(AT.tickErr)||0;
 if(te) bits.push('<span class="warn">程式下單那一段出錯 '+te+
   ' 次（⚠ 停損不受影響，主迴圈沒有中斷）</span>');
 if(AT.serr) bits.push('<span class="warn">最後一個錯誤：'+esc(AT.serr)+'</span>');
 return bits.map(b=>'<span>'+b+'</span>').join('<span class="sep">·</span>');
}
function atCumN(){
 const S=AT.stats; if(!S||!S.rows) return 0;
 let m=0;
 /* ⚠️ 2026-09-08 起不再把他自己那條算進來（那條線已經不畫了）—— 只數四個做法。 */
 for(const k of ATKEYS) m=Math.max(m,(S.rows[k].cum||[]).length);
 return m;
}
function atCumNHTML(){
 const n=atCumN();
 return n?(n+' 筆　灰帶＝這批資料證不出來的範圍'):'';
}
function atCumEmptyHTML(){
 const min=(AT.stats&&AT.stats.cum_min_n)||10, n=atCumN();
 return '<div class="t">只有 '+n+' 筆，還不畫線</div>'+
   '<div class="d" title="三、四個點連起來看起來就像一條趨勢，但那只是三、四個點">滿 '+
   min+' 筆才開始畫</div>';
}

/* ---------------- 配對對照（摺疊）---------------- */
function atPairHTML(){
 const S=AT.stats; if(!S||!S.pairs) return '';
 // ⛔ 標題與內文都用名字（規格 §2.0 ⑤：`5 分 K vs 不判斷`），⛔ 不准出現代號
 const base=atName('D');
 const cards=AT_ORDER.filter(k=>k!=='D').map(k=>{
   const p=S.pairs[k]||{m:0,w:0,l:0,diff:0,need:null,dots:[]};
   const dots=(p.dots||[]).map(v=>'<i class="'+(v>0?'w':(v<0?'l':''))+'"></i>').join('');
   return '<div class="at-pc"><div class="h">'+atName(k)+' vs '+base+'</div>'+
     '<div class="m">結果不一樣的只有 '+p.m+' 天 / '+S.n+' 天</div>'+
     '<div class="v">'+atName(k)+' 贏 '+p.w+' · '+base+' 贏 '+p.l+' · 差 '+
       '<span class="'+sgn(p.diff)+'">'+pm(p.diff,1)+'</span> 點</div>'+
     '<div class="at-dots">'+dots+'</div>'+
     '<div class="need">'+(p.need==null?'還沒有可以比的日子'
       :(p.need>p.m?('這 '+p.m+' 天全贏也還不算數')
                   :('贏 '+p.w+' 次 · 要 '+p.need+' 次才算數')))+'</div></div>';
 }).join('');
 return '<div class="at-pairs" title="四個做法大多數日子方向一樣，方向一樣的那幾天兩邊結果完全相同、'+
   '差值是 0。所以能拿來比的樣本數不是總天數，是「結果不一樣的那幾天」。'+
   '那幾天等於擲同樣次數的銅板 —— 要算數，得贏到 need 那個次數。">'+cards+'</div>';
}

/* ---------------- 門檻掃描（摺疊）----------------
   這是「⛔ 記數值，不只記方向」那條規矩的兌現：想試「要超過 50 點才做」，
   用同一批資料就算得出來，不用再等二十天。
   ⛔ 不顯示「最好的那一列」、⛔ 不畫門檻對報酬的曲線 —— 那種圖會讓人一眼挑峰值，
      而峰值幾乎一定是雜訊。 */
function atScanRow(o,cur){
 return '<tr'+(cur?' class="cur"':'')+'><td>'+o.t+' 點</td><td>'+o.n+'</td>'+
  '<td>'+o.w+'–'+o.l+'</td>'+
  '<td class="'+sgn(o.pts||0)+'">'+pm(o.pts||0,0)+'</td>'+
  '<td class="'+sgn(o.avg||0)+'">'+(o.avg==null?'—':pm(o.avg,1))+'</td>'+
  '<td>'+(o.ceiling==null?'—':(o.over?('超過 '+o.ceiling):('在雜訊裡（&lt;'+o.ceiling+'）')))+'</td></tr>';
}
function atLocalScan(t){
 const S=AT.stats, bs=(S&&S.bseries)||[];
 const pts=bs.filter(x=>Math.abs(x[0])>t).map(x=>x[1]);
 const w=pts.filter(p=>p>0).length, sum=pts.reduce((a,b)=>a+b,0);
 const avg=pts.length?Math.round(sum/pts.length*10)/10:null;
 const c=atCeiling(pts.length);
 return {t:t,n:pts.length,w:w,l:pts.length-w,pts:Math.round(sum*10)/10,avg:avg,
         ceiling:c,over:!!(c!=null&&avg!=null&&Math.abs(avg)>c)};
}
function atAdvHTML(){
 const S=AT.stats; if(!S) return '';
 const rows=(S.scan||[]).map(o=>atScanRow(o,false)).join('')+atScanRow(atLocalScan(AT.adv),true);
 return '<div class="at-advrow"><span>門檻</span>'+
  '<input type="range" id="atslider" min="0" max="200" step="5" value="'+AT.adv+'">'+
  '<b>'+AT.adv+' 點</b></div>'+
  '<table class="at-scan"><tr><th>門檻</th><th>筆數</th><th>勝–敗</th>'+
  '<th>累計點數</th><th>每筆</th><th>證得出來嗎</th></tr>'+rows+'</table>'+
  '<div class="note" title="門檻拉到 80 點只剩幾筆的時候，那一列的數字幾乎沒有意義">'+
  '門檻越高 ⇒ 筆數越少 ⇒ 那條線越高</div>'+
  '<div class="note" title="同一批資料被問越多次，挑到最好看的那個門檻通常只是挑到雜訊">'+
  '不是在找最好的門檻（刻意不標任何一列）</div>'+
  /* ⛔ 這裡也不准出現代號；而且滑桿⛔ 不准改動 atName('C')（規格 §2.0 最後一條）——
     它只是「用同一批資料試算另一個門檻」，真正在跑的永遠是後端常數 C_THRESH。 */
  '<div class="note">拉這個滑桿<b> 不會 </b>改變真正在跑的「'+atName('C')+
  '」（門檻寫在程式裡，只有改程式才會變）。</div>';
}

/* ---------------- canvas：這一天的圖 ＋ 四條泳道 ---------------- */
function atFit(){
 const cv=document.getElementById('atday'); if(!cv) return null;
 const box=cv.getBoundingClientRect();
 const w=Math.max(320,Math.round(box.width)), h=Math.max(180,Math.round(box.height));
 const dpr=Math.min(2,window.devicePixelRatio||1);
 /* ⛔ 這道 early-return 不准拿掉。寫 canvas.width（**就算寫同一個值**）會重新配置
    整張後備緩衝區並清空 —— demo 實測一次 draw 從 2ms 變 129ms。它在熱路徑上。 */
 if(w===ATC.W&&h===ATC.H&&dpr===ATC.DPR&&cv.width) return cv;
 ATC={W:w,H:h,DPR:dpr};
 cv.width=Math.round(w*dpr); cv.height=Math.round(h*dpr);
 cv.getContext('2d').setTransform(dpr,0,0,dpr,0,0);
 return cv;
}
function atNiceStep(raw){
 if(!(raw>0)) return 1;
 const e=Math.pow(10,Math.floor(Math.log10(raw))), r=raw/e;
 return (r<=1?1:r<=2?2:r<=2.5?2.5:r<=5?5:10)*e;
}
function atView(){ return AT.zoom?{t0:0,t1:ATWATCH-ATSEC0}:{t0:0,t1:ATSPAN}; }
/* 泳道左邊那一欄的寬度**是量出來的，不是猜的**（規格 §9.3）：
   對四個名字各跑一次 measureText，取最大值 NW，欄寬 L = ceil(NW) + 18。
   ⛔ 不准寫死一個 px 值 —— 門檻改成三位數（開盤起・要 200 點）名字就會變寬。
   ⛔⛔ 放不下不是縮寫的理由，是**重新排版**的理由（這一條被 Benson 退件過）：
      L 超過繪圖區 1/4 就切成堆疊排法（名字自己一列、色帶在下一列，列高 22→30），
      ⛔ 不准縮寫、不准截字、不准改小字級、也不可以改成壓縮泳道。 */
function atLaneLayout(ctx,PW){
 ctx.save(); ctx.font=ATFONTN;
 let nw=0;
 for(const k of AT_ORDER) nw=Math.max(nw,ctx.measureText(atName(k)).width);
 ctx.restore();
 const L=Math.ceil(nw)+18, stacked=(L>PW*0.25);
 ATLN={L:stacked?0:L,nw:nw,laneH:stacked?ATLANEH2:ATLANEH,stacked:stacked};
 return ATLN;
}
function atBars(){
 const D=AT.data, out=[];
 for(const b of ((D&&D.bars)||[])){
   const s=atSecOf(b[0]); if(s==null) continue;
   const o=atN(b[1]), h=atN(b[2]), l=atN(b[3]), c=atN(b[4]);
   if(o==null||h==null||l==null||c==null) continue;   // 一列壞資料只丟那一列
   out.push({t:s-ATSEC0,o:o,h:h,l:l,c:c});
 }
 return out;
}
function atAxis(bars,v){
 /* 價格軸：niceStep 貼齊（必抄）。
    ⚠️ **刻意沒有遲滯**：這張圖的視窗只有兩種固定值、資料只增不減 ⇒ 遲滯的
       early-return 結構上進不去（跟【細節】同一個坑，TICK-TAB-SPEC §4.1 有更正）。
       留一段不承重的程式，下一個人會以為它在守。
    ⛔ ±100 與交易標記**都不准撐大價格軸**：那 45 分鐘的真實振幅常常只有 30~60 點，
       硬把 ±200 塞進來會把唯一要看的波動壓成一條直線。畫不到就掛邊緣籤。
    ⚠️ 也因此完全不必碰真實單的 exit（它可能是 null，Math.min(lo,entry,exit) 會把它
       當成 0、價格軸整個掉到 0 —— 2026-09-02 踩過）。 */
 let hi=-1e18, lo=1e18;
 for(const b of bars){
   if(b.t+60<v.t0||b.t>v.t1) continue;
   if(b.h>hi)hi=b.h; if(b.l<lo)lo=b.l;
 }
 if(!(hi>lo)){ const m=(hi>-1e17?hi:12000); hi=m+5; lo=m-5; }
 const pad=Math.max(1,(hi-lo)*0.06);
 let aHi=hi+pad, aLo=lo-pad;
 const step=atNiceStep((aHi-aLo)/6);
 return {hi:Math.ceil(aHi/step)*step, lo:Math.floor(aLo/step)*step, step:step};
}
function atChip(ctx,x,y,txt,col){
 ctx.save(); ctx.font=ATFONT;
 const w=ctx.measureText(txt).width+14;
 ctx.fillStyle=ATCOL.bg; ctx.globalAlpha=.9;
 ctx.beginPath(); ctx.roundRect(x,y,w,17,4); ctx.fill(); ctx.globalAlpha=1;
 ctx.strokeStyle=col; ctx.globalAlpha=.5; ctx.lineWidth=1; ctx.stroke(); ctx.globalAlpha=1;
 ctx.fillStyle=col; ctx.fillText(txt,x+7,y+12); ctx.restore();
}
function atDrawDay(){
 const cv=atFit(); if(!cv) return 0;
 const D=AT.data; if(!D) return 0;
 const bars=atBars(); if(!bars.length) return 0;
 const ctx=cv.getContext('2d'), W=ATC.W, H=ATC.H;
 ctx.clearRect(0,0,W,H);
 const v=atView(), PW=W-ATR;
 /* ⛔ K 棒與泳道**必須共用同一個 xOf()**（規格 §9.3）——否則泳道的色帶跟上面的
    K 棒對不到同一個時刻。名字欄與 K 線圖因此共用同一條左界 LN.L。 */
 const LN=atLaneLayout(ctx,PW);
 const laneTop=H-ATBOT-AT_ORDER.length*LN.laneH;
 const pH=Math.max(60,laneTop-ATTOP-ATLANEG);
 const A=atAxis(bars,v);
 const gx=LN.L+4, gw=Math.max(20,PW-LN.L-8);
 const xOf=t=>gx+(t-v.t0)/(v.t1-v.t0)*gw;
 const yOf=p=>ATTOP+(A.hi-p)/(A.hi-A.lo)*pH;
 ctx.font=ATFONT; ctx.textBaseline='alphabetic';

 // 08:45~09:30 鋪淡金底（沿用面板「下單時段」的語言）
 const wx0=xOf(0), wx1=xOf(ATWATCH-ATSEC0);
 ctx.fillStyle='rgba(227,169,81,.035)';
 ctx.fillRect(Math.max(gx,wx0),ATTOP,Math.min(PW,wx1)-Math.max(gx,wx0),pH);

 // 價格軸（刻度從 lo 往上取第一個 step 整數倍開始畫）
 ctx.strokeStyle=ATCOL.line; ctx.lineWidth=1; ctx.fillStyle=ATCOL.faint;
 for(let p=Math.ceil(A.lo/A.step)*A.step;p<=A.hi+1e-9;p+=A.step){
   const y=Math.round(yOf(p))+.5;
   ctx.beginPath(); ctx.moveTo(gx,y); ctx.lineTo(PW,y); ctx.stroke();
   ctx.fillText(p.toFixed(0),PW+7,y+4);
 }
 // 時間軸：⛔ 印 HH:MM（印到秒是【細節】的事，這一頁印秒會讓人以為在看秒級圖）
 const tickEvery=(v.t1-v.t0)>7200?1800:600;
 for(let t=0;t<=v.t1;t+=tickEvery){
   const x=Math.round(xOf(t))+.5;
   if(x<gx-1||x>PW) continue;
   ctx.strokeStyle='rgba(36,44,56,.7)';
   ctx.beginPath(); ctx.moveTo(x,ATTOP); ctx.lineTo(x,ATTOP+pH); ctx.stroke();
   /* ⚠️ 第一格要夾住：`x-16` 在 x=0 時是 -16 ⇒ 08:45 那個標籤被切成「45」
      （截圖才看得出來，掃字串是掃不到的 —— 送進 canvas 的字串本身是對的）。 */
   ctx.fillStyle=ATCOL.faint; ctx.fillText(atHM(t),Math.max(2,Math.min(PW-34,x-16)),H-8);
 }

 // 1 分 K（台股慣例：紅漲綠跌）
 const bw=Math.max(1,Math.min(9,(60/(v.t1-v.t0))*PW-1));
 for(const b of bars){
   if(b.t+60<v.t0||b.t>v.t1) continue;
   const x=xOf(b.t+30), up=b.c>=b.o;
   ctx.strokeStyle=up?ATCOL.up:ATCOL.down; ctx.fillStyle=ctx.strokeStyle;
   ctx.lineWidth=1;
   ctx.beginPath(); ctx.moveTo(Math.round(x)+.5,yOf(b.h));
   ctx.lineTo(Math.round(x)+.5,yOf(b.l)); ctx.stroke();
   const y0=yOf(Math.max(b.o,b.c)), y1=yOf(Math.min(b.o,b.c));
   ctx.fillRect(x-bw/2,y0,bw,Math.max(1,y1-y0));
 }

 /* ⚠️ 疊放順序是**三層**，而且是截圖才看出來的（掃字串與「有沒有畫到畫布外」都抓不到）：
      ① 進場的金線與 ◆（最底）── 他自己那一單的 ▲ 常常就疊在同一個位置
      ② 他自己那幾張籤
      ③ 「09:03:30 模擬進場 12010.3」那張金籤（最上）
    理由：他的進場時刻本來就落在 09:03:30 附近、價也差不多。反過來排的話
    那個 ▲ 會蓋掉金籤上的一個字，而這一頁的主角就是那一筆。 */
 const px=atN(D.px);
 if(px!=null){
   const ex=xOf(ATSIG-ATSEC0);
   // 09:03:30 的金色垂直虛線 ＋ **一個**進場標記。
   // ⛔ 四條事實上就是同一個時刻同一個價，畫四個進場點是**假的**。
   ctx.save(); ctx.setLineDash([4,3]); ctx.strokeStyle=ATCOL.gold; ctx.lineWidth=1;
   ctx.beginPath(); ctx.moveTo(ex+.5,ATTOP); ctx.lineTo(ex+.5,H-ATBOT); ctx.stroke();
   ctx.restore();
   const ey=yOf(px);
   ctx.save(); ctx.fillStyle=ATCOL.gold;
   ctx.beginPath(); ctx.moveTo(ex,ey-6); ctx.lineTo(ex+6,ey); ctx.lineTo(ex,ey+6);
   ctx.lineTo(ex-6,ey); ctx.closePath(); ctx.fill(); ctx.restore();
   atDrawMine(ctx,D,xOf,yOf,A,PW,pH);            // ②：夾在 ◆ 與金籤之間
   // ⚠️ y 要夾住：進場價貼近價格軸上緣時，籤會跑到畫布外面（看不到就等於沒畫）
   atChip(ctx,Math.max(2,Math.min(ex+10,PW-190)),Math.max(ATTOP+1,ey-28),
     '09:03:30 模擬進場 '+px.toFixed(1),ATCOL.gold);
   /* ±100 兩條線。⛔ **不准標「停利／停損」** —— 同一條線對做多是停利、
      對做空是停損，標了一定有一邊是錯的。 */
   const tp=(atN(D.tp)||100), sl=(atN(D.sl)||100);
   // ⚠️ 負號用 ASCII 的 '-'，跟 pm() 印出來的點數一致 —— 同一張圖上兩種減號很難看
   [[px+tp,'進場價 +'+tp,1],[px-sl,'進場價 -'+sl,-1]].forEach(function(it){
     const p=it[0];
     if(p<=A.hi&&p>=A.lo){
       const y=Math.round(yOf(p))+.5;
       ctx.save(); ctx.setLineDash([3,4]); ctx.strokeStyle=ATCOL.line; ctx.lineWidth=1;
       ctx.beginPath(); ctx.moveTo(gx,y); ctx.lineTo(PW,y); ctx.stroke(); ctx.restore();
       /* ⚠️ 這兩張籤搬進左邊的名字欄（右對齊 L−8，規格 §9.3）——
          順便解掉 v2「進場價 +100 壓在 K 棒上」那個問題。沒有名字欄（堆疊排法）
          時退回畫在線的左上角。 */
       ctx.fillStyle=ATCOL.faint;
       if(LN.L>0){ ctx.textAlign='right'; ctx.fillText(it[1],LN.L-8,y+4); ctx.textAlign='left'; }
       else ctx.fillText(it[1],gx+4,y-3);
     }else{
       // ⛔ 畫不到就掛邊緣籤，不可以把線黏在邊緣假裝畫得出來
       const away=Math.round(it[2]>0?(p-A.hi):(A.lo-p));
       atChip(ctx,4,it[2]>0?(ATTOP+2):(ATTOP+pH-19),
         (it[2]>0?'↑ ':'↓ ')+it[1]+' 在畫面'+(it[2]>0?'上':'下')+'方 '+away+' 點',
         ATCOL.faint);
     }
   });
 }else{
   atDrawMine(ctx,D,xOf,yOf,A,PW,pH);          // 那天沒有記錄，但他自己的單照樣要畫
 }
 // 09:30：**他自己收手的時間**，不是模擬單的出場條件 —— 兩件事不要混
 const wx=xOf(ATWATCH-ATSEC0);
 if(wx>gx&&wx<PW){
   ctx.save(); ctx.setLineDash([2,4]); ctx.strokeStyle=ATCOL.line; ctx.lineWidth=1;
   ctx.beginPath(); ctx.moveTo(wx+.5,ATTOP); ctx.lineTo(wx+.5,ATTOP+pH); ctx.stroke();
   ctx.restore();
   ctx.fillStyle=ATCOL.ghost; ctx.fillText('09:30 你收手',wx+5,ATTOP+pH-5);
 }
 atDrawLanes(ctx,D,xOf,laneTop,PW,v,LN);
 AT.drawn++;
 return 1;
}
/* 他自己那一單（沿用即時分頁與【細節】的標記語言：▲ 多 ／ ▼ 空 ／「真」） */
function atDrawMine(ctx,D,xOf,yOf,A,PW,pH){
 /* ⚠️ 他一天 0~3 筆，而且**全部落在 08:45~09:30 那 45 分鐘裡** ——
    在整個日盤的橫軸上那是最左邊很窄的一段，三張籤的 x 幾乎一樣、
    價又都在進場價附近 ⇒ 不錯開就互相壓住（截圖才看見的，掃字串掃不到）。
    所以每一張往下錯開一列，並且夾在繪圖區裡面。 */
 let i=0;
 for(const t of ((D&&D.mine)||[])){
   const s=atSecOf(t.entry_time); if(s==null) continue;
   const p=atN(t.entry); if(p==null) continue;
   if(p>A.hi||p<A.lo) continue;               // ⛔ 不准為了畫它去撐大價格軸
   const x=xOf(s-ATSEC0);
   if(x<0||x>PW) continue;
   const short=(t.dir==='short'), y=yOf(p);
   ctx.save(); ctx.fillStyle=short?ATCOL.down:ATCOL.up; ctx.beginPath();
   if(short){ ctx.moveTo(x,y+9); ctx.lineTo(x-7,y-3); ctx.lineTo(x+7,y-3); }
   else{ ctx.moveTo(x,y-9); ctx.lineTo(x-7,y+3); ctx.lineTo(x+7,y+3); }
   ctx.closePath(); ctx.fill(); ctx.restore();
   const cy=Math.max(ATTOP+1,Math.min(ATTOP+pH-18,y+12+i*19));
   atChip(ctx,Math.max(2,Math.min(x+10,PW-150)),cy,
     '真 '+(short?'▼ 空 ':'▲ 多 ')+(t.entry_time||'').slice(0,5),
     short?ATCOL.down:ATCOL.up);
   i++;
 }
}
/* 四條泳道。
   【為什麼要泳道】四條在**同一時刻、同一個價**進場 ⇒ 四個進場標記完全重疊；
   出場價又只有三種可能（+100／−100／收盤價）⇒ 出場標記的 y 也重疊。
   解法是價格區只放一個，四條的身分全部下放到泳道。
   ⛔ D 跟 A/B 同向的日子泳道就是長得一樣 —— 那是事實不是 bug，**不要合併它們**，
      合併掉的話「今天判斷有沒有加分」正好看不見。
   ⛔ C 不做的日子畫成空的虛線 ＋「這天不做」＋ 訊號值，**不可以整條消失**。 */
function atDrawLanes(ctx,D,xOf,laneTop,PW,v,LN){
 const ex=xOf(ATSIG-ATSEC0);
 ctx.textBaseline='alphabetic';
 AT_ORDER.forEach(function(k,i){
   const y=laneTop+i*LN.laneH;
   /* ⛔⛔ 泳道左邊放的是**名字**不是代號（規格 §9.3，這一條被 Benson 退件過：
      「我要從哪裡知道現在我看的是哪個做法？」）。名字欄寬度是量出來的（atLaneLayout）。 */
   ctx.font=ATFONTN; ctx.fillStyle=ATCOL.dim;
   let mid, bx;                       // 色帶中線的 y、色帶的左界
   if(LN.stacked){
     ctx.fillText(atName(k),2,y+11);  // 窄視窗：名字自己一列（⛔ 仍然不縮寫）
     mid=y+LN.laneH-8; bx=0;
   }else{
     ctx.textAlign='right'; ctx.fillText(atName(k),LN.L-8,y+LN.laneH/2+4);
     ctx.textAlign='left';
     mid=y+LN.laneH/2; bx=LN.L;
   }
   ctx.font=ATFONT;
   const run=(D.runs||{})[k];
   const sg=(k==='A')?atN((D.sig||{}).A):(k==='D'?null:atN((D.sig||{}).B));
   const dr0=run?run.dir:((D.dirs||{})[k]);   /* 還沒結算時方向要看 dirs（C 沒下單就別寫持倉中） */
   if(D.px==null||!run||run.dir===0||run.dir==null){
     /* ⛔ 沒做／算不出來／還沒結算的日子畫成空的虛線 ＋ 一句話，**不可以整條消失**
        （消失＝壞掉，跟「休市日要選不到」是同一類處置）。
        ⚠️ 虛線要**從文字後面才開始畫**（規格 §9.3）—— 整條穿過文字會把字糊掉。
        ⚠️ 「這天不做」⛔ 不要再重複寫「沒超過 30」：名字裡已經寫著「要 30 點」了。 */
     /* ⛔ 這裡也要分「持倉中」與「結算中」（2026-09-08）：他早上盯的是**圖**，
        canvas 上的字掃字串掃不到，之前就為了同一件事退件過（【細節】M2）。 */
     const txt=(D.px==null)?'這天沒有記錄'
       :(dr0===0?('這天不做（訊號 '+(sg==null?'—':pm(sg,1))+' 點，不夠）')
       :(dr0==null?'這天算不出訊號'
       :(D.holding?'持倉中':'結算中')));
     const tx=Math.max(bx+8,ex+8);
     ctx.fillStyle=ATCOL.faint; ctx.fillText(txt,tx,mid+4);
     const ls=tx+ctx.measureText(txt).width+8;
     if(ls<PW){
       ctx.save(); ctx.setLineDash([3,4]); ctx.strokeStyle=ATCOL.ghost; ctx.lineWidth=1;
       ctx.beginPath(); ctx.moveTo(ls,mid+.5); ctx.lineTo(PW,mid+.5); ctx.stroke();
       ctx.restore();
     }
     return;
   }
   const out=atSecOf(run.exit_at);
   const ox=(out==null)?PW:xOf(out-ATSEC0);
   const off=(ox>PW);                     // 出場落在畫面外（切到 08:45~09:30 時很常見）
   const x1=Math.min(PW,Math.max(ex+2,ox));
   const win=(atN(run.pts)||0)>0;
   const col=win?ATCOL.up:ATCOL.down;     // 泳道顏色＝賺賠，這是損益，可以用紅綠
   ctx.save(); ctx.globalAlpha=.30; ctx.fillStyle=col;
   ctx.fillRect(ex,mid-6,Math.max(2,x1-ex),11); ctx.restore();
   ctx.strokeStyle=col; ctx.lineWidth=1.4;
   ctx.beginPath(); ctx.moveTo(ex+.5,mid-7); ctx.lineTo(ex+.5,mid+6); ctx.stroke();
   if(!off){ ctx.beginPath(); ctx.moveTo(x1-.5,mid-7); ctx.lineTo(x1-.5,mid+6); ctx.stroke(); }
   const txt=(run.dir>0?'多':'空')+' · '+(off?'畫面外 ':'')+(run.exit_at||'—')+' '+
     (ATEXIT[run.why]||'')+' '+pm(run.pts,0);
   const tw=ctx.measureText(txt).width;
   /* ⚠️ 放得下就用色帶顏色放在色帶右邊；放不下疊在色帶上時**改用 --text** ——
      同色字疊在 30% 透明的同色帶上讀不清楚（demo 第一版踩到，截圖才看見）。 */
   if(x1+8+tw<PW){ ctx.fillStyle=col; ctx.fillText((off?'▶ ':'')+txt,x1+8,mid+4); }
   else{ ctx.fillStyle=ATCOL.text; ctx.fillText((off?'▶ ':'')+txt,ex+8,mid+4); }
 });
}

/* ---------------- canvas：累計點數 ＋ 證不出來帶 ---------------- */
function atFitCum(){
 const cv=document.getElementById('atcum'); if(!cv) return null;
 const box=cv.getBoundingClientRect();
 const w=Math.max(320,Math.round(box.width)), h=Math.max(120,Math.round(box.height));
 const dpr=Math.min(2,window.devicePixelRatio||1);
 if(w===ATCC.W&&h===ATCC.H&&dpr===ATCC.DPR&&cv.width) return cv;   // ⛔ 同上，不准拿掉
 ATCC={W:w,H:h,DPR:dpr};
 cv.width=Math.round(w*dpr); cv.height=Math.round(h*dpr);
 cv.getContext('2d').setTransform(dpr,0,0,dpr,0,0);
 return cv;
}
/* ⛔ 顏色不編碼好壞（紅綠只給損益數字）。四條靠亮度與線型分，**線尾標名字**
   （⛔ 不是字母 —— 規格 §2.0 ④，代號不准上畫面）。
   ⛔「不判斷」不可以被畫成次要的：506 天的驗證裡它是目前的冠軍，所以它是最粗的那條。 */
/* ⛔ 2026-09-08：他自己那條金線（key `你`）跟著成績表那兩列一起從畫面上拿掉
   （「然後我自己的這邊都拿掉」）。⛔ 不要再加回來 —— 後端 S.mine 照算，只是不畫。 */
const ATLINES=[['D',ATCOL.text,2.4,false],['A',ATCOL.text,1.1,false],
               ['B',ATCOL.dim,1.4,false],['C',ATCOL.dim,1.4,true]];
/* 累計圖的線尾標籤一律走 atName()（⛔ 代號不准上畫面，規格 §2.0 ④）。 */
function atCumName(k){ return atName(k); }
function atCumSeries(){
 const S=AT.stats; if(!S||!S.rows) return {};
 return {D:S.rows.D.cum||[],A:S.rows.A.cum||[],B:S.rows.B.cum||[],C:S.rows.C.cum||[]};
}
function atDrawCum(){
 const cv=atFitCum(); if(!cv) return 0;
 const ctx=cv.getContext('2d'), W=ATCC.W, H=ATCC.H;
 ctx.clearRect(0,0,W,H);
 const ser=atCumSeries(), n=atCumN();
 /* ⛔ 不到 CUM_MIN_N 筆就**一條線都不畫**（連軸都不畫）——
    三、四個點連起來看起來就像一條趨勢，但那只是三、四個點。 */
 const min=(AT.stats&&AT.stats.cum_min_n)||10;
 if(n<min) return 0;
 const PW=W-ATR, pH=H-ATTOP-ATBOT;
 let hi=0, lo=0;
 for(const k in ser) for(const y of ser[k]){ if(y>hi)hi=y; if(y<lo)lo=y; }
 const band=atBand(n);
 hi=Math.max(hi,band*1.05); lo=Math.min(lo,-band*1.05);
 const step=atNiceStep((hi-lo)/5);
 hi=Math.ceil(hi/step)*step; lo=Math.floor(lo/step)*step;
 const xOf=i=>(i)/Math.max(1,n-1)*PW;
 const yOf=p=>ATTOP+(hi-p)/(hi-lo)*pH;
 ctx.font=ATFONT;
 /* 證不出來帶：隨 √n 張開的灰色喇叭。**任何一條線只要還在帶子裡面，
    就代表這份資料證不出它跟 0 有差別。** 把統計功效變成看得見的東西，
    他不用讀數字，看線有沒有跑出帶子就知道。 */
 ctx.save(); ctx.beginPath();
 for(let i=0;i<n;i++){ const x=xOf(i), b=atBand(i+1);
   if(i===0) ctx.moveTo(x,yOf(b)); else ctx.lineTo(x,yOf(b)); }
 for(let i=n-1;i>=0;i--){ const x=xOf(i), b=atBand(i+1); ctx.lineTo(x,yOf(-b)); }
 ctx.closePath();
 ctx.fillStyle='rgba(141,149,163,.075)'; ctx.fill();
 ctx.setLineDash([3,4]); ctx.strokeStyle='rgba(141,149,163,.35)'; ctx.lineWidth=1;
 ctx.stroke(); ctx.restore();
 // 0 線與刻度
 ctx.strokeStyle=ATCOL.line; ctx.lineWidth=1; ctx.fillStyle=ATCOL.faint;
 for(let p=Math.ceil(lo/step)*step;p<=hi+1e-9;p+=step){
   const y=Math.round(yOf(p))+.5;
   ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(PW,y); ctx.stroke();
   ctx.fillText((p>0?'+':'')+p.toFixed(0),PW+7,y+4);
 }
 const tags=[];
 for(const L of ATLINES){
   const arr=ser[L[0]]||[];
   if(arr.length<2) continue;
   ctx.save(); ctx.strokeStyle=L[1]; ctx.lineWidth=L[2];
   if(L[3]) ctx.setLineDash([5,4]);
   ctx.beginPath();
   arr.forEach((y,i)=>{ const x=xOf(i); if(i===0) ctx.moveTo(x,yOf(y)); else ctx.lineTo(x,yOf(y)); });
   ctx.stroke(); ctx.restore();
   const nm=atCumName(L[0]), tw=ctx.measureText(nm).width;
   /* ⚠️ 線尾標的是**名字**（規格 §2.0 ④；圖例已經刪掉了，這是唯一分得出誰是誰的地方）。
      ⛔ x 夾在 PW−4−字寬 以內 —— 名字比字母長很多（「開盤起・要 30 點」約 90px，
         而右邊價格刻度的留白只有 64px），不夾的話會**壓在刻度上**。 */
   tags.push({nm:nm,w:tw,col:L[1],
              x:Math.max(2,Math.min(PW-4-tw,xOf(arr.length-1)+5)),
              y:yOf(arr[arr.length-1])});
 }
 /* ⚠️ 四條線的終點常常擠在一起（同向的日子成績就是一樣）⇒ 標籤一定要**錯開**，
    否則兩三個名字疊在同一行、誰都讀不出來。**這件事只有截圖看得出來**，
    掃字串（字串完全正確）與「有沒有畫到畫布外」都抓不到 ——【細節】與這一頁都栽過。
    做法：照 y 排序 → 往下推到至少差 15px → 整體夾回畫布裡，字底下鋪一塊底色。 */
 tags.sort((a,b)=>a.y-b.y);
 for(let i=1;i<tags.length;i++)
   if(tags[i].y-tags[i-1].y<15) tags[i].y=tags[i-1].y+15;
 const spill=tags.length?(tags[tags.length-1].y-(H-ATBOT-4)):0;
 if(spill>0) for(const t of tags) t.y-=spill;
 for(const t of tags){
   t.y=Math.max(ATTOP+8,Math.min(H-ATBOT-4,t.y));
   ctx.save(); ctx.fillStyle=ATCOL.bg; ctx.globalAlpha=.85;
   ctx.fillRect(t.x-3,t.y-7,t.w+6,14); ctx.restore();
   ctx.fillStyle=t.col; ctx.fillText(t.nm,t.x,t.y+4);
 }
 ctx.fillStyle=ATCOL.faint; ctx.fillText('第 1 筆',2,H-8);
 ctx.fillText('第 '+n+' 筆',Math.max(60,PW-52),H-8);
 return 1;
}

/* ---------------- 畫面 ---------------- */
function atPaintStats(){
 if(TAB!=='auto') return;
 setEl('atcount',atCountHTML());
 setEl('atwin',atWinHTML());
 setEl('attbl',atTblHTML());
 setEl('atceil',atCeilHTML());
 setEl('atnotes',atNotesHTML());
 setEl('atcumn',atCumNHTML());
 setEl('atpairbody',atPairHTML());
 setEl('atadvbody',atAdvHTML());
 /* ⛔ 這個 hover 說明裡有數字（「505 筆的時候這條線是 12.0 點」），
    ⛔ **不可以寫死在 HTML 裡** —— 常數改了 title 就變成假話，而且 hover 才看得到、
    幾乎不可能被發現（守衛：autotest-tab.mjs ㉓④）。
    ⚠️ 2026-09-08 進度尺拿掉之後，這是最後一個「數字要算出來」的 title 了 ——
       ⛔ 不要因為只剩一個就把它改回寫死。 */
 const S0=AT.stats||{}, N0=atN(S0.track_n)||AT.trackN||505, C0=atCeiling(N0);
 const eCeil=document.getElementById('atceil');
 if(eCeil) eCeil.title='比這條線小的差距，跟運氣分不開。'+N0+' 筆的時候這條線是 '+C0+' 點。';
 const min=(AT.stats&&AT.stats.cum_min_n)||10, few=atCumN()<min;
 setEl('atcumempty',few?atCumEmptyHTML():'');
 const cv=document.getElementById('atcum');
 if(cv) cv.style.visibility=few?'hidden':'visible';
 if(!few) atDrawCum();
}
/* ⛔⛔ 進度尺（atTrackHTML）已於 2026-09-08 依 Benson 指示整條刪掉 —— 原話：
   「程式下單那邊這個欄位不需要」（他紅框圈的就是那條尺）。
   ⛔ 不要再加回來，也不要換個樣子重做一次（例如「還剩 485 天」的倒數 ——
      那本來就是被禁止的形狀：那是承諾不是事實）。
   ⚠️ 後端 `total_n`／`track_n` 兩個欄位**照端不要拿掉**：`track_n` 還在算天花板那個
      title 的數字（atPaintStats），`total_n` 是「累積幾天 vs 窗口幾天」那組語意的正本。
   守衛：autotest-tab.mjs ⑤（#attrack 不存在 ＋「這個測試跑到哪裡了」／「/ 505 筆」／
   「要到這裡才算數」在 DOM ＋ 兩張 canvas 上零命中）。 */
function atMsgHTML(){
 if(atLoading())
   return '<div class="at-skel"><i style="height:38%"></i><i style="height:52%"></i>'+
     '<i style="height:44%"></i><i style="height:61%"></i><i style="height:55%"></i>'+
     '<i style="height:70%"></i><i style="height:64%"></i><i style="height:48%"></i>'+
     '<i style="height:57%"></i><i style="height:72%"></i><i style="height:66%"></i>'+
     '<i style="height:80%"></i></div>';
 if(AT.err)
   return '<div class="at-blank"><div class="t">'+AT.err+'</div>'+
     '<div class="d">翻頁列還在，可以換到別天。</div>'+
     '<button class="tk-back" style="position:static;margin-top:6px" data-atact="retry">重試</button></div>';
 if(!AT.date||!(AT.days||[]).length&&!AT.today)
   return '<div class="at-blank"><div class="t">還沒有任何一天的模擬紀錄</div>'+
     '<div class="d">明天早上 09:03:30 會自動記下第一筆。不用做任何事，也不會有單送出去。</div></div>';
 if(!(AT.data&&(AT.data.bars||[]).length))
   return '<div class="at-blank"><div class="t">這天沒有 1 分 K 可以畫</div>'+
     '<div class="d">休市日，或當天的 K 棒還沒拿到。</div></div>';
 return '';
}
function atPaint(){
 if(TAB!=='auto') return;
 setEl('atdt',atDateHTML());
 setEl('atsub',atSubHTML());
 setEl('atpager',atPagerHTML());
 setEl('atpick',atListHTML());
 setEl('attools',atToolsHTML());
 setEl('attoday',atTodayHTML());
 const msg=atMsgHTML();
 setEl('atmsg',msg);
 const cv=document.getElementById('atday');
 if(cv) cv.style.visibility=msg?'hidden':'visible';
 atPaintStats();
 if(!msg) atDrawDay();
}
function atGoDay(d){
 if(!d) { AT.pick=false; atPaint(); return; }
 if(d===AT.date&&!AT.pending){ AT.pick=false; atPaint(); return; }
 AT.pick=false; AT.date=d; AT.err='';
 const c=AT.cache[d];
 if(c){ AT.data=c; AT.pending=false; atPaint(); }
 else { AT.pending=true; AT.data=null; atPaint(); atFetchDay(d); }
}
function atEnter(){
 atPaint();
 // ⚠️ 每次進來都重新問一次時鐘與日期清單（09:03:30 可能剛過）
 atFetchDays().then(()=>{ atPaint(); if(AT.date) atFetchDay(AT.date); atFetchStats(); });
 AT.booted=true;
}
document.addEventListener('click',function(e){
 if(TAB!=='auto') return;
 const nv=e.target.closest('[data-atnav]');
 if(nv){ atGoDay(atStep(parseInt(nv.getAttribute('data-atnav')))); return; }
 const pk=e.target.closest('[data-atpick]');
 if(pk){ AT.pick=!AT.pick; atPaint(); return; }
 const dy=e.target.closest('[data-atday]');
 if(dy){ atGoDay(dy.getAttribute('data-atday')); return; }
 const zm=e.target.closest('[data-atzoom]');
 if(zm){ AT.zoom=!AT.zoom; atPaint(); return; }
 const wn=e.target.closest('[data-atwin]');
 if(wn){ AT.swin=parseInt(wn.getAttribute('data-atwin')); atPaintStats(); atFetchStats(); return; }
 const ac=e.target.closest('[data-atact]');
 if(ac){ if(ac.getAttribute('data-atact')==='retry'){ AT.err=''; atPaint(); atFetchDay(AT.date); }
   return; }
 if(AT.pick&&!e.target.closest('.tk-list')){ AT.pick=false; atPaint(); }
});
document.addEventListener('input',function(e){
 if(TAB!=='auto'||e.target.id!=='atslider') return;
 AT.adv=parseInt(e.target.value)||0;
 setEl('atadvbody',atAdvHTML());
 const s=document.getElementById('atslider');
 if(s) s.focus();
});
document.addEventListener('keydown',function(e){
 if(TAB!=='auto') return;
 if(e.target.tagName==='INPUT'||e.target.tagName==='TEXTAREA') return;
 if(e.key==='ArrowLeft'){ e.preventDefault(); atGoDay(atStep(-1)); }
 else if(e.key==='ArrowRight'){ e.preventDefault(); atGoDay(atStep(1)); }
 else if(e.key==='Home'){ e.preventDefault(); if(AT.today) atGoDay(AT.today); }
 else if(e.key==='Escape'&&AT.pick){ AT.pick=false; atPaint(); }
});
window.addEventListener('resize',()=>{ if(TAB==='auto') atPaint(); });

rvBind();

/* ══════════════ 【自動下單】分頁：會真的送出委託單的那一頁 ══════════════

   版面沿用【自動下單（模擬）】那一套（同樣的 .card / .sec-head / .at-title / .at-tbl），
   ⛔ 差別只有三件事，而且每一件都要**一眼看得到**：
     ① 現在是開還是關　② 選了哪個做法　③ 今天送了沒／為什麼沒送

   ⛔⛔ 這一段**一顆按鈕都沒有**（沒有 button／form／input／[data-act]／[data-rdir]），
        也**不打任何 POST**。開關只有一條路：他自己在硬碟上建 AUTO_ORDERS_ON。
        畫面上按得到的開關 ＝「會不會亂送單」從「讀一個檔」變成「讀整個前端」。
   ⛔ 顏色沿用這個面板的規矩：**紅綠只給損益**；開／關用金色與中性灰
      （關著是正常狀態，⛔ 不是紅色 —— 跟休市的連線燈同一個道理）。
   ⛔ 名字只寫「5 分 K」「開盤起」，⛔ 畫面上不准出現 A／B 這種代號
      （【模擬】那一頁為這件事被退件過一次：「我要從哪裡知道現在我看的是哪個做法？」）。
   ⚠️ 這一頁不掛在 500ms 的 tick 上，只在切進來時抓 ＋ 停在這一頁時每 5 秒更新一次。
*/
var AL={data:null,err:'',pending:false,seq:0,timer:null};
/* ⭐ 「打開自動下單」的兩段式狀態（⛔ 只活在記憶體裡，⛔ 不寫 localStorage）：
     step='idle'    兩顆做法鈕
     step='confirm' 確認條（已經選了 mode，但**還沒**送出任何請求）
   ⛔ 切走分頁再回來一定要回到 'idle'（見 alEnter）——「我剛剛按到哪裡了」
      不可以跨分頁殘留，不然他回來時看到一條已經展開的確認條，
      很容易當成「我剛剛好像已經開了」而直接按下去。
   ⛔ 而且**不做逾時自動收回**：他可能中途去看別的分頁再回來（那條路由 alEnter 重設），
      在同一頁上等多久都不該讓畫面自己跳掉。 */
var ALON={step:'idle',mode:null,busy:false,err:''};
const ALWAY={A:{n:'5 分 K',s:'09:00 起算'},B:{n:'開盤起',s:'08:45 起算'}};
function alName(k){ const w=ALWAY[k]; return w?w.n:''; }
function alSub(k){ const w=ALWAY[k]; return w?w.s:''; }
function alN(v){ return (typeof v==='number'&&isFinite(v))?v:null; }
function alF(v,d){ const n=alN(v); return n==null?'—':n.toFixed(d==null?1:d); }
function alSigned(v){ const n=alN(v); return n==null?'—':(n>0?'+':'')+n.toFixed(1); }

function alEnter(){
 /* ⛔ 每次切進這一頁都回到「未確認」（⛔ 不可以留著上次展開到一半的確認條）。 */
 ALON.step='idle'; ALON.mode=null; ALON.busy=false; ALON.err='';
 alFetch();
 if(AL.timer) clearTimeout(AL.timer);
 AL.timer=setTimeout(alLoop,5000);
}
function alLoop(){
 if(TAB!=='fire'){ AL.timer=null; return; }
 alFetch(); AL.timer=setTimeout(alLoop,5000);
}
function alFetch(){
 const my=++AL.seq; AL.pending=true;
 return fetch('/api/fire/state').then(r=>r.json()).then(x=>{
   if(my!==AL.seq) return;
   AL.pending=false;
   /* 治具與舊版後端會回一個空物件 —— 那不是「關閉中」，是「問不到」，
      ⛔ 兩件事不可以寫同一句（寫同一句一定有一句是假的）。 */
   if(!x||typeof x!=='object'||!('armed' in x)){ AL.data=null; AL.err='這個面板還沒有自動下單那一段（後端沒有回報狀態）'; }
   else { AL.data=x; AL.err=x.error||''; }
   alPaint();
 }).catch(()=>{ if(my!==AL.seq) return; AL.pending=false; AL.data=null;
   AL.err='連不上面板'; alPaint(); });
}

/* 每一種「沒送」都要有自己的一句話。⛔ 正本在後端 auto_fire.WHY（隨端點送過來），
   這裡只是後端沒回時的退路 —— 兩邊寫不一樣的話，畫面上那句就是假的。 */
function alWhy(x,fallback){
 const T=(AL.data&&AL.data.why_texts)||{};
 return T[x]||fallback||x||'';
}

function alPaint(){
 const D=AL.data;
 if(!D){
   setEl('alstate','<span class="al-badge">讀不到狀態</span>');
   setEl('alsub','<span class="warn">'+esc(AL.err||'載入中…')+'</span>');
   /* ⛔ 讀不到狀態時那顆「關閉」鈕也要收起來 —— 我們根本不知道開關檔在不在，
      而那顆鈕的說明寫著「按下去就把 X 改名收起來」，留著就是一句沒把握的話。 */
   /* ⛔⛔ 讀不到狀態時「打開」那兩顆更不能留：我們連現在是真錢還是演練都不知道。 */
   setEl('algates',''); setEl('alhow',''); setEl('altoday',''); setEl('aloff','');
   setEl('alon','');
   setEl('altbl',''); setEl('alempty',''); setEl('alnotes','');
   return;
 }
 const armed=!!D.armed, m=D.method, live=!!D.live;
 /* ── ① 現在是開還是關 ＋ ② 選了哪個做法 ─────────────────────── */
 let st;
 if(armed) st='<span class="al-badge on">開啟中</span>'+
   '<span class="al-way">'+esc(alName(m)||m)+'<i>'+esc(alSub(m))+
   ' · '+esc(D.signal_at||'')+' 送出 '+esc(String(D.qty||1))+' 口</i></span>';
 else if(D.arm_why==='off') st='<span class="al-badge">關閉中</span>'+
   /* ⛔ 這句話 2026-09-09 跟著改：以前只能自己建檔，現在下面那兩顆就開得起來。
      舊句子（「要用請自己建 AUTO_ORDERS_ON」）留在畫面上會讓他以為按鈕不算數。 */
   '<span class="al-way off">按下面那兩顆就可以開始</span>';
 else st='<span class="al-badge">拒絕下單</span>'+
   '<span class="al-way off">'+esc(D.arm_msg||alWhy(D.arm_why))+'</span>';
 setEl('alstate',st);
 /* ⛔ 真單開關要跟自動開關一起講：他關掉真單就等於連自動也關掉，
    但畫面上如果只寫「開啟中」，他會以為單真的會出去。 */
 /* ⛔ 這一行不要複述上面那個名字（上面已經寫了「5 分 K」＋「09:00 起算」）——
    這裡要講的是**後果**：09:03:30 一到會發生什麼事。 */
 const sigT=esc(D.signal_at||''), eodT=esc(D.eod_at||'');
 let sub='<span>'+(armed
   ?(live?'⚠️ '+sigT+' 一到，程式會自己送出 1 口真單（你不在也會送）'
         :sigT+' 一到會走完整條路，但真單開關關著 ⇒ 不會真的送出去')
   :'一張單都不會送出去')+'</span>';
 /* 開關沒有有效期：⛔ 這件事一定要寫出來，不然他會以為「今天開的、今天有效」。 */
 if(armed) sub+='<span class="sep">·</span><span>開關沒有有效期，'+
   '<b>每個交易日都會送</b>，直到你自己關掉</span>';
 /* ⛔ 收盤平倉是這一段最會賠錢的地方（沒平就是抱過夜盤），一定要寫在最上面。 */
 if(armed&&eodT) sub+='<span class="sep">·</span><span>'+eodT+
   ' 會自動平倉（<b>只平自動下單開的那一口</b>，你自己開的單不會碰）</span>';
 /* ⚠️ 真單關著時最容易被誤會的一件事：症狀（按不了進場）跟原因（演練部位）
    看起來毫無關係，不寫他會以為面板壞了。 */
 if(armed&&!live) sub+='<span class="sep">·</span><span class="warn">'+
   '⚠️ 演練也會產生一個<b>演練部位</b> —— 那口部位開著的時候，'+
   '你自己在【即時】那一頁<b>按不了進場</b>（會寫「已經有部位了」）。'+
   '要自己下單就先按手動平倉，或把這個開關關掉。</span>';
 if(D.err) sub+='<span class="sep">·</span><span class="warn">送單那一段出過錯 '+
   esc(String(D.err_n||0))+' 次（停損不受影響）：'+esc(D.err)+'</span>';
 setEl('alsub',sub);

 setEl('algates',
   '<div class="c"><div class="k">自動下單開關（'+esc(D.flag||'')+'）</div>'+
     '<div class="v'+(armed?'':' off')+'">'+esc(armed?('開著 · '+alName(m)):'沒有這個檔')+'</div></div>'+
   '<div class="c"><div class="k">真單開關（'+esc(D.live_flag||'')+'）</div>'+
     '<div class="v'+(live?'':' off')+'">'+
       esc(live?'開著 · 會真的送出去':'關著 · 只會演練')+'</div></div>'+
   '<div class="c"><div class="k">送出去的內容</div>'+
     '<div class="v">'+esc(String(D.qty||1))+' 口 · 停利 &plusmn;'+esc(alF(D.tp,0))+
       ' 點 · 一天 1 次</div></div>');

 /* ── 怎麼開、怎麼關（⛔ 開只有一條路，關才有按鈕）─────────────────── */
 /* ⚠️ 這一段一定要**直接給可以貼的指令**：lab-qa 實測他最可能用的兩種寫法
    （Notepad 另存選 UTF-8 with BOM、PowerShell 的 "A" > 檔＝UTF-16LE）
    以前都會讓畫面寫「看不懂」。後端現在讀得懂了，但**第一次就給對的做法**
    才是真的把「第一次一定會失敗的路徑」當成主流程做。 */
 const FLG=esc(D.flag||'AUTO_ORDERS_ON');
 /* ⛔ 這一頁被 Benson 退件過一次，理由是「一大堆多餘的文字」。所以這一段
    **只留他真的要動手做的事**：怎麼建、怎麼關、方向怎麼判、風險。
    ⛔ 不准把「這一頁在算什麼」那種解釋牆搬進來。 */
 setEl('alhow',
   /* ⛔ 主要的路現在是上面那兩顆鈕（Benson 2026-09-09：「我按個鈕就可以開始了這樣」）。
      自己建檔那條**留著**：他之後可能在別的機器、或想在面板沒開著時先設好。 */
   '要開：按上面那兩顆（<b>會再問你一次</b>才真的打開）。也可以自己在 '+
   '<code>tools/shioaji/'+FLG+'</code> 裡寫一個 '+
   '<code>A</code>（'+esc(alName('A'))+'）或 <code>B</code>（'+esc(alName('B'))+'）；'+
   '不管走哪一條，都還要有 <code>'+esc(D.live_flag||'REAL_ORDERS_ON')+'</code> '+
   '才會真的送出去（<b>把真單關掉就等於連自動也關掉</b>）。<br>'+
   '<code class="cmd">Set-Content "tools\\shioaji\\'+FLG+'" "A" -Encoding ascii</code>'+
   '　<span class="warn">⚠️ 記事本存檔請選 <b>UTF-8</b>；PowerShell 的 '+
   '<b>&gt; 檔</b> 與 <b>Out-File</b> 存出來是 UTF-16。'+
   '（存錯也不會亂送 —— 讀不懂一律拒絕下單。）</span><br>'+
   '要關：按下面那顆。方向怎麼判：'+sigT+' 的價比參考價<b>高或持平</b>做多、低做空，'+
   '<b>剛好持平（差 0 點）算做多</b>（跟【自動下單（模擬）】同一把尺）。<br>'+
   '<span class="warn">⚠️ 永豐沒有停損單，停損活在這台電腦的面板迴圈裡 —— '+
   '面板關掉／當掉／電腦睡著就沒有停損，'+eodT+' 的自動平倉也不會發生；'+
   '平不掉會在上面寫出來，那時請自己到大戶投平。</span>');

 /* ── 打開（兩段式）：⛔ 只有開關檔**不在**的時候才畫（開著就只剩「關閉」）─── */
 setEl('alon', alOnHTML(D));

 /* ── 關閉鈕：⛔ 只有開關檔存在時才畫（沒東西可關就不該有按鈕）────── */
 setEl('aloff', D.flag_exists
   ? '<button class="btn flat2" data-aloff="1">關閉自動下單</button>'+
     '<span class="n">按下去就把 <b>'+FLG+'</b> 改名收起來（內容留著），'+
     '之後<b>不會再送任何單</b>。要再開就自己把檔名改回去。</span>'+
     (D.off_msg?'<span class="n">'+esc(D.off_msg)+'</span>':'')
   : (D.off_msg?'<span class="n">'+esc(D.off_msg)+'</span>':''));

 /* ── ③ 今天送了沒／為什麼沒送 ─────────────────────────────── */
 const days=D.days||[], today=D.today||'', row=days.find(r=>r&&r.date===today)||null;
 setEl('alcount',esc(today));
 setEl('altoday',alTodayHTML(D,row)+alEodHTML(D,row));

 /* ── 紀錄（要能跟【自動下單（模擬）】那一頁對得起來）───────────── */
 setEl('allogn',days.length?(days.length+' 天'):'');
 setEl('altbl',days.length?alTblHTML(D,days):'');
 setEl('alempty',days.length?'':
   '還沒有任何紀錄。開關關著的時候，'+esc(D.signal_at||'')+' 一到只會在這裡記一列「沒送」，'+
   '不會有任何委託單出去。');
 setEl('alnotes',alNotesHTML(D,days));
}

/* ⭐⭐ 「打開自動下單」——**這是這個面板上唯一一顆會武裝真錢的鈕**。
   ⛔ 兩段式：第一段（選做法）**一個請求都不送**，只把畫面換成確認條；
      第二段按「確定，打開」才會真的打 POST /api/fire/on。
   ⛔ 確認條那句話（真錢／演練）**只從後端拿**（D.arm_confirm.text），
      ⛔ 前端不准自己判斷 —— 這一頁另外有 D.live，但那句話的措辭是後端的正本，
      兩邊各寫一份就一定有一份是舊的。
   ⛔ 後端沒回 arm_confirm ⇒ **不畫開啟鈕**（我們連現在是不是真錢都不知道，
      這種時候給他一顆按鈕比不給更糟）。 */
function alOnHTML(D){
 if(D.flag_exists) return '';    /* 已經開著 ⇒ 這裡什麼都不畫（要換做法請先關掉） */
 const C=D.arm_confirm;
 if(!C||typeof C.text!=='string')
   return '<div class="err">這個面板還不能從畫面上打開（後端沒有回報現在是'+
     '真實下單還是演練）。要開請自己在 <b>tools/shioaji/'+
     esc(D.flag||'AUTO_ORDERS_ON')+'</b> 裡寫一個字母。</div>';
 if(ALON.step==='confirm'){
   const m=ALON.mode;
   /* ⛔ 這一條要當場講清楚**現在是哪一種**：真錢＝紅底（這個面板唯一的例外，
      紅綠平常只給損益）、演練＝中性灰。⛔ 兩種絕不可以長一樣。 */
   return '<div class="al-conf'+(C.live?' real':'')+'">'+
     '<div class="q">'+(C.live?'⚠️ ':'')+esc(C.text)+'<br>要用「<b>'+
       esc(alName(m))+'</b>」（'+esc(alSub(m))+'）開始嗎？</div>'+
     '<div class="btns2">'+
       '<button class="btn go" data-alyes="1"'+(ALON.busy?' disabled':'')+'>'+
         (ALON.busy?'打開中…':'確定，打開')+'</button>'+
       '<button class="btn no" data-alno="1"'+(ALON.busy?' disabled':'')+'>取消</button>'+
     '</div></div>'+
     (ALON.err?'<div class="err">'+esc(ALON.err)+'</div>':'');
 }
 /* 第一段：⛔ 做法直接寫在鈕上（他不必先去別的地方查哪個是哪個），
    名字一律走 alName()／alSub()（⛔ 不准自己發明名字，也不准出現代號）。 */
 return '<div class="row">'+
   '<button class="btn" data-alon="A">用「'+esc(alName('A'))+'」開始</button>'+
   '<button class="btn" data-alon="B">用「'+esc(alName('B'))+'」開始</button>'+
   '</div>'+
   '<div class="n">兩顆都是每個交易日 '+esc(D.signal_at||'')+' 送出 '+
     esc(String(D.qty||1))+' 口：<b>'+esc(alName('A'))+'</b> 拿 '+esc(alSub('A'))+
     '的價當參考、<b>'+esc(alName('B'))+'</b> 拿 '+esc(alSub('B'))+'的價當參考，'+
     esc(D.signal_at||'')+' 的價比它高或持平做多、低做空。按下去會再問你一次。</div>'+
   (ALON.err?'<div class="err">'+esc(ALON.err)+'</div>':'');
}

/* ⭐ 第二段真的送出去。⛔ 六道防護裡有兩道是請求要帶的（自訂標頭 ＋ token）——
   ⛔ 少帶一個後端就會擋（403），那是刻意的：**別的網頁帶不出這兩樣**。
   ⚠️ 2026-09-09：改走跟其他每一顆鈕**同一個** `pfetch()` 出口 ——
      這一頁自己寫一份標頭 ＝ 兩把尺，總有一天有一邊沒跟上。 */
function alArm(){
 if(ALON.busy) return;
 const m=ALON.mode;
 if(m!=='A'&&m!=='B'){ ALON.step='idle'; ALON.mode=null;
   ALON.err='沒有選到做法，請再按一次'; alPaint(); return; }
 ALON.busy=true; ALON.err=''; alPaint();
 pfetch('/api/fire/on',JSON.stringify({mode:m}))
  .then(r=>r.json()
    .catch(()=>({ok:false,msg:'面板回了看不懂的東西（HTTP '+r.status+'）'}))
    .then(j=>Object.assign({},j,{_code:r.status})))
  .then(r=>{ ALON.busy=false; ALON.step='idle'; ALON.mode=null;
    /* ⛔ 成功也可能有話要說（例如那一列紀錄沒寫進去）——不可以安靜地吞掉。 */
    /* ⚠️ token 是每次啟動換一次的：面板剛被看門狗重開過的話，畫面上這一份是舊的
       ⇒ 後端會擋（403）。⛔ 後端刻意不講「你是哪一道沒過」（那顆鈕沒有讓外面除錯的
       需求），所以由前端補一句他做得到的下一步。 */
    ALON.err=(r&&r.ok)?(r.warn||''):(((r&&r.msg)||'打不開')+
      (r&&r._code===403?'（如果面板剛重新啟動過，請再按一次）':'')); })
  .catch(()=>{ ALON.busy=false; ALON.step='idle'; ALON.mode=null;
    ALON.err='打不開：連不上面板'; })
  /* 按完立刻重抓狀態 —— 他要看得到「真的開起來了」，不是相信一句話。 */
  .then(()=>alFetch());
}

/* ⭐ 【自動下單】會改變狀態的動作只有兩個：打開（兩段式）與關閉（一鍵）。
   ・關掉**不跳確認**：那是安全方向（沿用真實下單那邊「平倉不跳確認」同一個道理）。
   ・打開**一定跳確認**：⛔ 這個不對稱是刻意的，不要「順手統一」。
   ・按完立刻重抓狀態 —— 他要看得到結果，不是相信一句 alert。 */
document.addEventListener('click', function(e){
 if(TAB!=='fire') return;
 /* 第一段：⛔ 只換畫面，**一個請求都不送**（fire-tab.mjs ⑪ 在守）。 */
 const on=e.target.closest('[data-alon]');
 if(on){ if(on.disabled) return;
   ALON.step='confirm'; ALON.mode=on.getAttribute('data-alon'); ALON.err='';
   alPaint(); return; }
 /* 取消：⛔ 要真的回到兩顆鈕的狀態（不是只把條子藏起來）。 */
 const no=e.target.closest('[data-alno]');
 if(no){ if(no.disabled) return;
   ALON.step='idle'; ALON.mode=null; ALON.err=''; alPaint(); return; }
 const yes=e.target.closest('[data-alyes]');
 if(yes){ if(yes.disabled) return; alArm(); return; }
 const b=e.target.closest('[data-aloff]');
 if(!b||b.disabled) return;
 b.disabled=true;
 pfetch('/api/fire/off')
  .then(r=>r.json())
  .then(r=>{ if(!r.ok&&r.msg) alert(r.msg); })
  .catch(()=>{ alert('關不掉：連不上面板'); })
  .then(()=>{ b.disabled=false; alFetch(); });
});

function alTodayHTML(D,r){
 const nowS=alSecs(D.now), sigS=alSecs(D.signal_at);
 if(!r){
   if(nowS!=null&&sigS!=null&&nowS<sigS)
     return '<div class="t off">還沒到 '+esc(D.signal_at||'')+'</div>'+
       '<div class="d">'+(D.armed?'開關是開著的（'+esc(alName(D.method))+'）。':
         '開關是關著的，'+esc(D.signal_at||'')+' 一到會在紀錄裡留一列「沒送」。')+'</div>';
   /* ⛔ 「沒有紀錄」跟「有紀錄、沒送」是兩件事，不可以寫同一句。 */
   return '<div class="t off">今天沒有紀錄</div>'+
     '<div class="d">'+esc(D.signal_at||'')+' 那一刻面板沒有跑到這一段（沒開著、或那時還在啟動）。'+
     '<b>不補單</b>。</div>';
 }
 if(r.rec==='result'&&r.ok){
   const dir=r.dir==='long'?'做多':(r.dir==='short'?'做空':'—');
   return '<div class="t">'+esc(live_word(r))+'：'+esc(alName(r.method)||'')+' → '+esc(dir)+
     '　1 口</div><div class="d">進場 <b>'+esc(alF(r.entry))+'</b>'+
     '　停利 <b>'+esc(alF(r.tp))+'</b>'+
     '　'+esc(D.signal_at||'')+' 的價 <b>'+esc(alF(r.px))+'</b>'+
     '　滑價 <b>'+esc(alSigned(r.slip))+'</b> 點'+
     (r.has_target?'':'　<span style="color:var(--gold)">停利單沒掛上去，請自己到大戶投補掛</span>')+
     (r.warn?'<br><span style="color:var(--gold)">'+esc(r.warn)+'</span>':'')+'</div>';
 }
 if(r.rec==='result'&&!r.ok)
   return '<div class="t off">沒有送成</div><div class="d">'+
     esc(r.why_msg||alWhy(r.why))+'</div>';
 if(r.rec==='fire')      /* stage 停在 sending ＝ 決定送單之後程式中斷了 */
   return '<div class="t off">不知道下場</div><div class="d">'+
     esc(r.why_msg||alWhy('crashed'))+'</div>';
 return '<div class="t off">沒有送單</div><div class="d">'+
   esc(r.why_msg||alWhy(r.why))+'</div>';
}
/* 收盤平倉：⛔ 「還沒到」「平掉了」「沒東西可平」「不是我的、不碰」「平不掉」
   五種**一句都不准混**（混一句就一定有一句是假的）。 */
function alEodHTML(D,r){
 const e=(r&&r.eod)||null, eodT=esc(D.eod_at||'');
 if(!eodT) return '';
 if(!e){
   const nowS=alSecs(D.now), edS=alSecs(D.eod_at);
   /* 只有「今天真的開出部位了」才需要預告收盤平倉，其他日子講了只是雜訊。 */
   const opened=!!(r&&r.rec==='result'&&r.ok);
   if(!opened) return '';
   if(nowS!=null&&edS!=null&&nowS<edS)
     return '<div class="d">'+eodT+' 會自動平倉（<b>只平這一口</b>）。</div>';
   return '<div class="al-alarm">⚠️ '+eodT+' 已經過了，但這一天<b>沒有留下收盤平倉的紀錄</b>'+
     ' —— 面板那時可能沒開著。請自己到大戶投確認部位。</div>';
 }
 const body=esc(e.why_msg||alWhy(e.why))+
   (e.at?'　<b>'+esc(e.at)+'</b>':'')+
   (alN(e.exit)!=null?'　出場 <b>'+esc(alF(e.exit))+'</b>':'')+
   (alN(e.points)!=null?'　<b>'+esc(alSigned(e.points))+'</b> 點':'');
 if(e.alarm) return '<div class="al-alarm">'+body+'</div>';
 return '<div class="d">'+body+'</div>';
}
function live_word(r){ return r.live?'已送出委託單':'演練（沒有真的送出去）'; }
function alSecs(hms){
 const m=/^(\d\d):(\d\d):(\d\d)/.exec(String(hms||''));
 return m?(+m[1]*3600+ +m[2]*60+ +m[3]):null;
}

function alTblHTML(D,days){
 const sim=D.sim||{};
 let h='<thead><tr><th>日期</th><th>做法</th><th>方向</th><th>結果</th>'+
   '<th>進場</th><th>停利</th><th>滑價</th><th>模擬那邊</th></tr></thead><tbody>';
 for(const r of days.slice(0,60)){
   const s=sim[r.date]||null;
   const sent=(r.rec==='result'&&r.ok);
   const dir=r.dir==='long'?'做多':(r.dir==='short'?'做空':'—');
   let res;
   if(sent) res='<span class="ok">'+esc(r.live?'送出去了':'演練')+'</span>';
   else if(r.rec==='fire') res='<span class="ok">不知道下場</span>';
   else res='<span class="no">沒送</span>';
   let simTxt='—';
   if(s){
     const run=(s.runs||{})[r.method||'B']||null;
     simTxt=(s.px==null?'—':alF(s.px))+(run&&alN(run.pts)!=null?
       '　'+alSigned(run.pts)+' 點':'');
     if(s.miss) simTxt='那邊也沒記到';
   }
   h+='<tr><td class="nm">'+esc(r.date||'')+'</td>'+
     '<td>'+esc(alName(r.method)||'—')+'</td>'+
     '<td>'+esc(sent?dir:'—')+'</td>'+
     '<td>'+res+(sent?'':'<span class="why">'+esc(r.why_msg||alWhy(r.why))+'</span>')+'</td>'+
     '<td>'+esc(sent?alF(r.entry):'—')+'</td>'+
     '<td>'+esc(sent?alF(r.tp):'—')+'</td>'+
     '<td>'+esc(sent?alSigned(r.slip):'—')+'</td>'+
     '<td>'+esc(simTxt)+'</td></tr>';
 }
 return h+'</tbody>';
}

/* ⛔ 常態統計不畫；**異常**才畫（沿用【模擬】那一頁 atNotesHTML 的規矩）。
   ⛔ 但「異常」一項都不准少 —— 安靜地少是這個專案明令禁止的失敗模式。 */
function alNotesHTML(D,days){
 const L=D.ledger||{}, out=[];
 /* ⛔ fire + result + skip + eod + bad ＝ 檔案總列數（收盤平倉那一列也要有去處）。 */
 const tot=alN(L.total)||0, sum=(alN(L.fire)||0)+(alN(L.result)||0)+(alN(L.skip)||0)+
   (alN(L.eod)||0);
 if(alN(L.bad)) out.push('讀不出來的紀錄 '+L.bad+' 列');
 if(tot&&sum+(alN(L.bad)||0)!==tot) out.push('⚠️ 帳本對不起來：檔案 '+tot+' 列、認得的只有 '+sum+' 列');
 const stuck=days.filter(r=>r&&r.rec==='fire').length;
 if(stuck) out.push('⚠️ 有 '+stuck+' 天停在「送出去了但不知道結果」，請自己到大戶投確認');
 /* ⛔ 收盤沒平掉／不敢動的日子要**單獨數出來**：那幾天的部位是抱過夜盤的。 */
 const eodBad=days.filter(r=>r&&r.eod&&r.eod.alarm).length;
 /* ⚠️ 這一句⛔ 不要再列舉原因（2026-09-09：`eod_cant_tell` 加進來時這裡差點漏掉——
    列舉式的文案每多一種結局就要記得改一次，而它不會有任何東西提醒你）。
    每一天為什麼沒平，那一列自己那句話（why_msg）寫得清清楚楚。 */
 if(eodBad) out.push('⚠️ 有 '+eodBad+' 天收盤沒有自動平掉（原因看那一天的紀錄）'+
   '，請自己到大戶投確認那幾天的部位');
 /* 模擬那邊有記、這邊卻連一列都沒有 ⇒ 兩頁不同步（面板版本不一致或接線掉了） */
 const miss=Object.keys(D.sim||{}).filter(d=>!days.some(r=>r&&r.date===d)).length;
 if(miss) out.push('模擬那一頁有 '+miss+' 天，這裡沒有對應的紀錄');
 if(D.armed&&!D.started) out.push('⚠️ 開關是開的，但送單執行緒沒有起來');
 if(D.armed&&!D.wired) out.push('⚠️ 開關是開的，但面板沒有把自動下單接起來');
 return out.map(t=>'<span>'+esc(t)+'</span>').join('<span class="sep">·</span>');
}

tick(); setInterval(tick,500);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    # ⛔ 連線層的逾時（2026-09-09）。⚠️ 光擋 `Content-Length: -1` 還不夠：
    #    宣告 1000 卻只送 1 個位元組的連線，`rfile.read(n)` 一樣會**一直等**，
    #    `ThreadingHTTPServer` 的執行緒就這樣一條一條被吃掉（外面的網頁做得到）。
    #    ⚠️ 這是 socket 操作的逾時，⛔ 不是「處理時間」的上限 ——
    #    面板在本機、最大的回應（K 棒／逐筆）也遠遠不到 30 秒。
    #    `handle_one_request()` 自己會接住 timeout 並收掉那條連線（不會噴 traceback）。
    timeout = 30

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        # ⛔ 武裝那顆只收 POST。沒有 do_HEAD 的話 BaseHTTPRequestHandler 會回 501
        #    （語意上也是拒絕），但這一顆要回**明確的 405**。
        #    其餘路徑維持原本的行為（這支面板從來不服務 HEAD）。
        if self.path.split("?", 1)[0] == "/api/fire/on":
            return self._json(405, {"ok": False, "msg": "這個端點只收 POST"})
        self.send_error(501, "Unsupported method ('HEAD')")

    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _real_enter(self, body):
        d = body.get("dir")
        if d not in ("long", "short"):
            return self._json(400, {"ok": False, "msg": "方向要是 long 或 short"})
        with state_lock:
            q = STATE.get("quote", "closed")
        st2 = CURRENT_STATE.get("today")
        px = st2.price if st2 else None
        ok, why = broker.can_enter(px, q == "live")
        if not ok:
            return self._json(409, {"ok": False, "msg": why})
        ok, err, pos = broker.enter(d, px, TP_POINTS)
        # ⚠️ ok=True 但 err 有值 ＝ **進場成功、可是停利沒掛上去**。
        # 這種不可以當成無聲成功（他會以為賺的那邊有保護），要跳出來；
        # 但也不可以當成失敗（部位是真的在）。所以另外用 warn 標記，
        # 前端只在 warn 或失敗時才 alert —— 演練模式那句成功訊息不要每次都彈。
        return self._json(200 if ok else 500,
                          {"ok": ok, "warn": bool(ok and err),
                           "msg": err or ("已送出" if broker.is_live()
                                          else "演練：單子已組好，沒有送出")})

    def do_POST(self):
        # ⛔⛔⛔ 【P0，2026-09-09 lab-qa】**這支面板每一個 POST 都會動到真錢或改變狀態。**
        #   以前防護只掛在 `/api/fire/on` 上，`/api/real/enter`／`/api/real/close`
        #   一道都沒有 —— lab-qa 用一張**純 HTML** 的表單（不必 JS、不必 CORS、不必 token）
        #     <form action="http://127.0.0.1:8770/api/real/enter" method="post"
        #           enctype="text/plain">
        #     <input name='{"dir":"long","x":"' value='"}'>
        #   真的打進去了：瀏覽器送出 `{"dir":"long","x":"="}` ＝合法 JSON ⇒
        #   `broker.enter('long', …)` 被呼叫、回 `200 {"ok": true, "msg": "已送出"}`。
        #   **他上網時任何一個網頁都可以在他不知情的狀況下用他的帳戶送單／平倉。**
        #
        #   ⛔ 所以守衛套在**入口**，不是逐條路由各自套：
        #     ・逐條套 ＝ 下一個人加端點時會忘記（這正是這次的成因）
        #     ・入口套 ＝ 結構上不可能有「沒被審過的 POST」
        #   ⛔ 這條規矩包含 `/api/fire/off`（關閉自動下單）。關掉雖然是**安全方向**
        #     （不會賠錢），但「他以為開著、其實被某個網頁關掉了」＝ 整天安靜地沒送單，
        #     而**安靜地少**是這個專案明令禁止的失敗模式。前端只有一個呼叫點，補標頭零風險。
        #   ⚠️ 前端**每一個** POST 都走 `pfetch()` 那一個出口（⛔ 不准有第二個地方自己寫
        #     `fetch(...,{method:'POST'})`），token 由 0.5 秒一次的 `/api/state` 一直換新
        #     ⇒ 看門狗重啟後最多 0.5 秒就對得上，⛔ 他的「平倉」不會因此按不動。
        #
        # ⛔ `Content-Length` 一定要自己解（2026-09-09 lab-qa）：
        #   ・`Content-Length: abc` ⇒ 舊寫法 `int()` 直接噴 traceback、斷連線
        #   ・`Content-Length: -1` ⇒ `rfile.read(-1)` 會**一直讀到對方關連線**，
        #     那條執行緒就這樣卡住（`ThreadingHTTPServer` 一條一條被吃掉）
        #   兩個都回 400，另外加上限（⛔ 心得最長也就幾 KB）。
        _cl = self.headers.get("Content-Length")
        try:
            n = int(_cl) if (_cl or "").strip() else 0
        except (TypeError, ValueError):
            return self._json(400, {"ok": False, "msg": "Content-Length 看不懂"})
        if n < 0 or n > MAX_POST_BYTES:
            return self._json(400, {"ok": False, "msg": "body 太大或長度不合理"})
        # ⚠️ 先把 body 讀掉再擋：擋下來卻不讀，連線裡剩下的位元組會被當成下一個請求解析。
        try:
            raw = self.rfile.read(n)
        except Exception:
            raw = b""
        ok, code, msg = fire_post_guard(self.headers)
        if not ok:
            return self._json(code, {"ok": False, "msg": msg})
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            body = {}
        st = CURRENT_STATE.get("today")
        price = st.price if st else None

        if self.path == "/api/real/enter":
            # 【伺服器端也要擋連按】ThreadingHTTPServer 會讓兩個請求真的同時跑。
            # 前端的鎖擋得住手快，擋不住重整、兩個視窗、或前端出錯 ——
            # 兩張都出去就是 2 口，而且先掛的那張停利單從此撤不掉
            # （lab-qa 退件第 1 條，探針實測相隔 0.7 秒送出兩張新倉單）。
            if not ENTER_LOCK.acquire(blocking=False):
                return self._json(409, {"ok": False, "msg": "上一筆還在送，請稍候"})
            try:
                return self._real_enter(body)
            finally:
                ENTER_LOCK.release()

        # 【自動下單】⭐ **關閉。這一條只會關，永遠不會開。**
        #   Benson 2026-09-09：「我用工具那邊關掉之後，才會失效」⇒ 開關沒有有效期，
        #   建了就一直有效，但要有一個關得掉的地方。
        #   ⛔ 設計原則是**開難、關易**（2026-09-09 下午他要求「開」也做到面板上之後
        #      仍然成立，只是「難」的意思變了）：
        #     ・開＝上面那一條，**兩段式**＋六道防護（他要按兩次，第二次會被告知這是真錢）
        #     ・關＝這一顆。關掉永遠是安全的方向，所以做成一鍵、**不跳確認**
        #       （沿用真實下單那邊「平倉不跳確認」同一個道理）。
        #       ⛔ 這個不對稱是刻意的，不要「順手統一」成兩邊都跳或兩邊都不跳。
        #   ⛔ auto_fire.disarm() 結構上只會把開關檔移走 —— **auto_fire.py 裡
        #      沒有任何一行會建立 ARM_FLAG**（test_auto_fire.py ⑬ 用 AST 在守）：
        #      會送單的那個模組打不開自己的開關。建檔只有 fire_arm_on() 一個地方。
        # 【自動下單】⭐ **打開**（2026-09-09 Benson 要求做在面板上）。
        #   ⛔⛔ 按下去就是武裝真錢 ⇒ 這一條跟上面那顆「關閉」的規矩完全相反：
        #     ・前端**兩段式**（兩顆「用XX開始」→ 確認條 →「確定，打開」）
        #     ・這裡**六道防護**（`fire_post_guard`：Content-Type／自訂標頭／Origin／
        #       一次性 token／Sec-Fetch-Site／Host）—— ⛔ 缺一道就有繞法：
        #       他電腦上任何一個開著的網頁都送得到 localhost:8770。
        #     ・`mode` ⛔ **先驗再寫**（不准寫進檔案再驗）
        #     ・已經開著再按 ⇒ 409（⛔ 不覆蓋、不當成換做法）
        #   ⛔ 路由是精確比對（`==`），⛔ 不可以 startswith／in ——
        #      放寬＝多開一批沒人審過的入口（`test_fire_routes.py` ④ 在守）。
        if self.path == "/api/fire/on":
            # ⚠️ 六道防護在 `do_POST` 的入口就過了（每一個 POST 都過），
            #    ⛔ 這裡不再呼叫第二次 —— 兩個地方各呼叫一次 ＝ 有一天會有一邊被拿掉。
            m = body.get("mode") if isinstance(body, dict) else None
            code, out = fire_arm_on(m, who=self.client_address[0]
                                    if self.client_address else "?")
            return self._json(code, out)

        if self.path == "/api/fire/off":
            try:
                ok, msg = auto_fire.disarm()
            except Exception as e:
                return self._json(500, {"ok": False, "msg": "關不掉：" + str(e)[:150]})
            return self._json(200 if ok else 409, {"ok": ok, "msg": msg,
                                                   "armed": auto_fire.arm()["on"]})

        if self.path == "/api/real/close":
            ok, err = broker.close("manual")
            return self._json(200 if ok else 409,
                              {"ok": ok, "msg": err or ("已送出平倉" if broker.is_live()
                                                        else "演練：平倉單已組好，沒有送出")})

        if self.path == "/api/enter":

            d = body.get("dir")
            if d not in ("long", "short"):
                return self._json(400, {"error": "方向要是 long 或 short"})
            # 【紀錄正確性】沒有即時報價就不准開單 —— 用一個舊價或最後收盤價記進
            # 練習成績，那筆成績就是假的。前端會把按鈕停用，這裡是最後一道防線。
            # 沒有 quote 欄位（例如測試治具）時當作 live，維持舊行為。
            with state_lock:
                q = STATE.get("quote", "live")
            if q != "live":
                return self._json(409, {"ok": False, "msg": QUOTE_MSG.get(
                    q, "現在沒有即時報價，無法進場")})
            ok, msg = open_position(d, price, body.get("note", ""))
            return self._json(200 if ok else 409, {"ok": ok, "msg": msg})

        if self.path == "/api/close":
            r = close_position(price, "manual")
            return self._json(200, {"ok": r is not None,
                                    "msg": "已平倉" if r else "目前沒有持倉"})

        if self.path == "/api/note":
            try:
                # 真實交易的心得走 broker（存在 real_trades/，**不上傳**）。
                # ⚠️ 刻意不放在 /api/real/* 底下 —— 那個前綴的意思是「會送出委託單」，
                #    寫心得不該混進去（有部位時我們有一條「不碰 /api/real/*」的鐵律）。
                if body.get("kind") == "real":
                    ok, msg = broker.set_trade_note(
                        body.get("date"), body.get("time"),
                        body.get("entry"), body.get("text"))
                    return self._json(200 if ok else 409, {"ok": ok, "msg": msg})
                if body.get("open"):
                    ok, msg = set_note(None, None, None, body.get("text"), on_open=True)
                else:
                    ok, msg = set_note(body.get("date"), body.get("time"),
                                       body.get("entry"), body.get("text"))
            except Exception as e:
                return self._json(400, {"ok": False, "msg": str(e)[:120]})
            return self._json(200 if ok else 409, {"ok": ok, "msg": msg})

        if self.path == "/api/replay":
            # Bar Replay 的判斷 → 只寫 replay_log/，不進 practice_trades/
            # ⛔ 欄位驗證（2026-09-09 lab-qa 提，比照 /api/enter）：
            #    舊版收到空 body 也回 200，並在 replay_log/ 寫進一列**整列都是 null**
            #    的假紀錄 —— 它會被算進勝率統計，而畫面上看不出那一列是垃圾。
            #    ⚠️ 日期是檔名（`replay_file(d)`），不驗形狀等於讓外面決定寫哪個檔。
            _d = body.get("date")
            if not (isinstance(_d, str) and _REPLAY_DATE_RE.match(_d)):
                return self._json(400, {"ok": False, "msg": "date 要是 YYYY-MM-DD"})
            if not isinstance(body.get("judged"), bool):
                return self._json(400, {"ok": False, "msg": "judged 要是 true／false"})
            if body.get("judged"):
                if body.get("dir") not in ("long", "short"):
                    return self._json(400, {"ok": False,
                                            "msg": "有判斷的話 dir 要是 long 或 short"})
                if not isinstance(body.get("entry"), (int, float)) \
                        or isinstance(body.get("entry"), bool):
                    return self._json(400, {"ok": False,
                                            "msg": "有判斷的話 entry 要是數字"})
            try:
                rec = {k: body.get(k) for k in
                       ("date", "judged", "dir", "entry", "time", "note",
                        "exit", "exit_time", "reason", "points", "net",
                        "same_dir", "day_dir", "day_time")}
                rec["ts"] = datetime.now().isoformat(timespec="seconds")
                n = save_replay(rec)
                return self._json(200, {"ok": True, "n": n, "tally": replay_tally()})
            except Exception as e:
                return self._json(500, {"ok": False, "msg": str(e)[:150]})

        if self.path == "/api/sync":
            # 兩個方向都做一次：先把手機寫的心得抓回來，再把這邊的推上去。
            # 順序不能反 —— 先推的話會用舊心得去覆蓋剛抓回來的。
            got, gmsg = pull_from_phone()
            ok, msg = sync_to_cloud()
            return self._json(200, {"ok": ok, "msg": (gmsg + "；" if got else "") + msg})

        if self.path == "/api/undo":
            # 誤按時可以撤銷最後一筆（只在剛平倉沒多久時合理）
            global POSITION
            if POSITION is not None:
                POSITION = None
                return self._json(200, {"ok": True, "msg": "已取消未平倉的那筆"})
            if TODAY_TRADES:
                TODAY_TRADES.pop()
                save_trades()
                return self._json(200, {"ok": True, "msg": "已刪除最後一筆紀錄"})
            return self._json(200, {"ok": False, "msg": "今天沒有紀錄可刪"})

        return self._json(404, {"error": "not found"})

    def do_GET(self):
        # ⚠️ days 要排在 day 前面 —— "/api/tick/days" 也 startswith("/api/tick/day")。
        if self.path.startswith("/api/tick/days"):
            try:
                return self._json(200, tick_days())
            except Exception as e:
                return self._json(500, {"error": str(e)[:200], "days": [],
                                        "skipped": [], "since": None,
                                        "today": str(date.today())})
        if self.path.startswith("/api/tick/day"):
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            want, frm = None, None
            for kv in q.split("&"):
                if kv.startswith("date="):
                    want = kv[5:]
                elif kv.startswith("from="):
                    try:
                        frm = int(kv[5:])
                    except ValueError:
                        frm = None
            # 日期一定要長得像日期才放行 —— 這個字串會被接成檔名
            if not want or not re.match(r"^\d{4}-\d{2}-\d{2}$", want):
                return self._json(400, {"error": "date 要是 YYYY-MM-DD"})
            try:
                out = tick_day(want, frm)
            except Exception as e:
                return self._json(500, {"error": str(e)[:200], "date": want})
            if out is None:
                return self._json(404, {"error": "這天沒有紀錄（逐筆與取樣都找不到）",
                                        "date": want})
            return self._json(200, out)
        # ⚠️ days 要排在 day 前面 —— "/api/auto/days" 也 startswith("/api/auto/day")。
        #    （【細節】那組已經為同一件事踩過，這裡照抄它的順序。）
        if self.path.startswith("/api/auto/days"):
            try:
                return self._json(200, auto_days())
            except Exception as e:
                return self._json(500, {"error": str(e)[:200], "days": [], "missing": [],
                                        "today": str(date.today()),
                                        "now": datetime.now().strftime("%H:%M:%S")})
        if self.path.startswith("/api/auto/stats"):
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            win, src = 20, "live"
            for kv in q.split("&"):
                if kv.startswith("win="):
                    try:
                        win = max(0, min(5000, int(kv[4:])))
                    except ValueError:
                        win = 20
                elif kv.startswith("src="):
                    # ⛔ 只認這三個值。src 一放寬就等於讓回測混進實跑的統計裡。
                    src = kv[4:] if kv[4:] in ("live", "backfill", "all") else "live"
            try:
                return self._json(200, auto_stats(win, src))
            except Exception as e:
                return self._json(500, {"error": str(e)[:200], "n": 0, "rows": {}})
        if self.path.startswith("/api/auto/day"):
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            want = None
            for kv in q.split("&"):
                if kv.startswith("date="):
                    want = kv[5:]
            # 日期一定要長得像日期才放行 —— 這個字串會被接成檔名
            if not want or not _AUTO_DATE.match(want):
                return self._json(400, {"error": "date 要是 YYYY-MM-DD"})
            try:
                out = auto_day(want)
            except Exception as e:
                return self._json(500, {"error": str(e)[:200], "date": want})
            if out is None:
                return self._json(404, {"error": "這天沒有紀錄", "date": want})
            return self._json(200, out)
        # 【自動下單】⚠️ 這是**唯讀**的：它回報「開關開了沒／今天送了沒／為什麼沒送」，
        #   另外帶兩樣「打開」那條路要用的東西：`arm_confirm`（確認條那句話的正本）
        #   與 `token`（跨站讀不到這份 JSON ⇒ 拿不到它）。
        #   ⛔ 改變狀態的 POST 只有兩個，都在 do_POST：`/api/fire/on`（兩段式＋六道防護）
        #      與 `/api/fire/off`（一鍵，關掉永遠是安全方向）。
        # ⛔⛔ 武裝那顆**只收 POST**：GET／HEAD 一律 405。
        #    網頁上一個 <img src>、一條他點下去的連結、瀏覽器的預抓
        #    都不可以變成「幫他打開自動下單」（GET 連 CORS 那一關都不用過）。
        if self.path.split("?", 1)[0] == "/api/fire/on":
            return self._json(405, {"ok": False, "msg": "這個端點只收 POST"})
        if self.path.startswith("/api/fire/state"):
            # ⛔⛔ 這份 JSON 裡有 `token` ⇒ 它自己也要過 ③⑤⑥（2026-09-09 lab-qa）。
            #    沒有這一道的話，DNS rebinding 下別的網站讀得到 token ⇒ 第 ④ 道形同虛設。
            ok, code, msg = fire_get_guard(self.headers)
            if not ok:
                return self._json(code, {"ok": False, "msg": msg})
            try:
                out = auto_fire.state()
                out["sim"] = fire_sim_pairs(out.get("days") or [])
                # ⛔ 「現在是真錢還是演練」那句話**在後端算**（前端不准猜），
                #    而且拿的是 auto_fire 算好的那個 live ⇒ 只有一把尺。
                out["arm_confirm"] = fire_arm_confirm(out.get("live"))
                # 兩段式確認第二段要帶的 token。跨站讀不到這份 JSON ⇒ 拿不到它。
                out["token"] = FIRE_TOKEN
                return self._json(200, out)
            except Exception as e:
                return self._json(500, {"error": str(e)[:200], "armed": False,
                                        "days": [], "sim": {},
                                        "today": str(date.today())})
        if self.path.startswith("/api/bars"):
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            want, tf, full = None, CHART_TF, False
            for kv in q.split("&"):
                if kv == "full=1":
                    full = True
                elif kv.startswith("date="):
                    try:
                        want = datetime.strptime(kv[5:], "%Y-%m-%d").date()
                    except Exception:
                        want = None
                elif kv.startswith("tf="):
                    if kv[3:] not in ("1", "5"):
                        return self._json(400, {"error": "tf 只接受 1 或 5"})
                    tf = int(kv[3:])
            out = day_bars(want, tf, full=full)
            if full:
                out["days"] = day_index()       # 即時分頁的日期選單（迷你月曆）
            else:
                out.update(traded_days())
            return self._json(200, out)
        if self.path.startswith("/api/review"):
            try:
                return self._json(200, review_payload())
            except Exception as e:
                return self._json(500, {"error": str(e)[:200], "trades": [], "days": []})
        if self.path.startswith("/api/replay"):
            return self._json(200, {"tally": replay_tally()})
        if self.path.startswith("/api/stats"):
            return self._json(200, practice_stats())
        if self.path.startswith("/api/export"):
            data = all_practice_trades()
            b = json.dumps(data, ensure_ascii=False, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition",
                             'attachment; filename="practice-trades.json"')
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        if self.path.startswith("/api/state"):
            # ⛔⛔ 這份 JSON 也帶 `token`（前端每 0.5 秒就換到最新的那一份 ⇒
            #    看門狗重啟後他的「平倉」鈕不會突然按不動），所以它跟 /api/fire/state
            #    同一個守衛。⛔ 拿掉這一道 ＝ 把 token 送給任何一個網頁。
            ok, code, msg = fire_get_guard(self.headers)
            if not ok:
                return self._json(code, {"ok": False, "msg": msg})
            LAST_CLIENT["at"] = time.time()      # 有人在看（桌面 App 靠這個判斷關窗）
            with state_lock:
                try:
                    payload = json.dumps(dict(STATE, token=FIRE_TOKEN),
                                         ensure_ascii=False).encode()
                except TypeError as e:
                    # 【最後一道】STATE 裡混進不能序列化的東西時，舊版整支端點會炸掉、
                    # 回空字串 ⇒ 前端拿不到任何狀態、**畫面整個凍住**
                    # （2026-09-01：真實部位帶著永豐的 Trade 物件，他手上有單卻看不到）。
                    # 根因已經在 broker.snapshot() 修掉，這裡是防下一次。
                    # 寧可少一塊資料，也不要讓整個面板瞎掉。
                    safe = {k: v for k, v in STATE.items()
                            if k not in ("real",)}
                    safe["real"] = {"error": f"狀態序列化失敗：{str(e)[:80]}",
                                    "live": False, "position": None,
                                    "can_enter": False,
                                    "why": "面板狀態出問題，請去大戶投確認部位"}
                    # ⛔⛔ 這一條退路**一定要帶 token**：少了它，前端下一次
                    #    `pfetch()` 就沒有 token ⇒ **他的「平倉」鈕當場按不動**。
                    #    「寧可少一塊資料也不要讓面板瞎掉」在這裡的意思包含「按鈕還要能按」。
                    safe["token"] = FIRE_TOKEN
                    payload = json.dumps(safe, ensure_ascii=False, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path.startswith("/api/idle"):
            # 幾秒沒有瀏覽器來要資料了。刻意不更新 LAST_CLIENT ——
            # 問的人是桌面 App 的啟動器，它不算觀眾。
            last = LAST_CLIENT["at"]
            b = json.dumps({"idle": None if last == 0 else round(time.time() - last, 1)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return

        if self.path.startswith("/manifest.webmanifest"):
            b = json.dumps(MANIFEST, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/manifest+json; charset=utf-8")
            # 換圖示或改名字時要能傳得過去，不要被瀏覽器壓在快取裡
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return

        if self.path.startswith("/icon-"):
            name = self.path.split("?", 1)[0].lstrip("/")
            # 只認自己產的那兩個檔名，不要讓路徑跑到別的地方去
            if name in ("icon-192.png", "icon-512.png"):
                f = HERE / name
                if f.exists():
                    b = f.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Cache-Control", "max-age=3600")
                    self.send_header("Content-Length", str(len(b)))
                    self.end_headers()
                    self.wfile.write(b)
                    return
            self.send_error(404)
            return

        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        # 【一定要 no-store】沒有快取標頭的話瀏覽器會自己猜著存 ——
        # 改完面板重開，看到的卻還是舊版，然後要教他按 Ctrl+Shift+R。
        # 面板是本機服務、每次都只是讀一個字串，沒有省這一下的必要。
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def port_taken():
    """已經有一個面板在跑就別再開第二個 —— 否則搶不到 port，看門狗會無限重試。"""
    import socket
    sk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sk.settimeout(0.5)
    try:
        return sk.connect_ex(("127.0.0.1", PORT)) == 0
    finally:
        sk.close()


def _index_age():
    """加權指數這個數字是幾秒前的。給前端顯示與量測用（None = 還沒抓到過）。"""
    at = INDEX.get("at")
    return None if at is None else round(time.time() - at, 1)


def _index_change(snap, px):
    """
    從快照取「今天漲跌幾點、幾 %」。

    永豐的快照本身就帶 change_price / change_rate（跟大戶投顯示的是同一組數字），
    直接用它最準。不同版本的 SDK 欄位名不保證一致，所以逐個 getattr、
    取不到就退回自己算（現價 − 昨收）；再取不到就回 None，畫面顯示「—」而不是亂編。
    """
    def num(*names):
        for nm in names:
            v = getattr(snap, nm, None)
            if v not in (None, ""):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
        return None

    chg = num("change_price", "price_change", "change")
    pct = num("change_rate", "change_percent", "pct_change")
    if chg is None:
        prev = num("yesterday_close", "prev_close", "reference_price")
        if prev:
            chg = px - prev
    if pct is None and chg is not None:
        prev = px - chg
        pct = (chg / prev * 100) if prev else None
    return {"chg": None if chg is None else round(chg, 2),
            "pct": None if pct is None else round(pct, 2)}


def poll_index():
    """
    背景固定每 INDEX_EVERY 秒抓一次加權指數快照。

    用快照輪詢而非訂閱：指數的 tick callback 型別跟期貨不同，
    成本極低，也不會卡住主迴圈（主迴圈 0.25 秒一圈）。

    ⛔ 【節奏要扣掉工作時間，不可以做完再固定睡 N 秒】2026-09-02 他回報
       「加權指數更新有點慢」。實測：期貨成交價 0.4 秒跳一次，**指數 6.2 秒才動一次**，
       但程式寫的是 3 秒 —— 因為 `api.snapshots()` 本身要花約 3 秒，
       `sleep(3)` 又疊在後面 ⇒ 實際週期 = 工作時間 ＋ 3 秒。
       現在改成「補足到固定週期」，順便把單次耗時記進 INDEX["ms"]，
       這樣下次要判斷「慢在網路還是慢在我們」有數字可看。
    """
    while True:
        t_start = time.time()
        try:
            api = SESSION_REF.get("api")
            t = datetime.now().time()
            if api is not None and CASH_OPEN <= t <= CASH_CLOSE:
                c = INDEX.get("contract")
                if c is None:
                    for _ in range(10):
                        try:
                            lst = list(api.Contracts.Indexs.TSE)   # 只能用屬性存取
                            hit = [x for x in lst if x.code == "IX0001"]
                            if hit:
                                c = INDEX["contract"] = hit[0]
                                break
                        except Exception:
                            pass
                        time.sleep(1)
                if c is not None:
                    snap = api.snapshots([c])[0]
                    px = snap.close
                    if px:
                        INDEX.update({"price": float(px), "at": time.time(),
                                      **_index_change(snap, float(px))})
            else:
                # 現貨沒開盤就不要顯示舊值（連漲跌一起清掉）
                INDEX.update({"price": None, "chg": None, "pct": None})
        except Exception:
            pass
        took = time.time() - t_start
        INDEX["ms"] = round(took * 1000)
        # 補足到固定週期；工作本身就超過週期時至少留 0.3 秒，不要把 API 打爆
        time.sleep(max(0.3, INDEX_EVERY - took))


def serve():
    # 只綁 127.0.0.1：面板是給這台電腦自己用的。
    # （曾短暫改成 0.0.0.0 讓手機連，但 Benson 的手機常不在同一個網路，
    #   用不到卻多開一個對外的口，所以退回本機。）
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


# ---------------------------------------------------------------- 主流程

def current_dayvol():
    """
    今天的波動度基準 = 最近 20 個交易日「日盤高低幅」的中位數。
    模型內部一律用「幾倍日常波動」比對，這個值負責把它換算回今天的點數。
    """
    px = pd.read_csv(HERE / "txf_1min.csv", usecols=["ts", "High", "Low"])
    px["ts"] = pd.to_datetime(px["ts"])
    t = px["ts"].dt.time
    day = px[(t >= SESSION_OPEN) & (t < DAY_END)]
    rng = day.groupby(day["ts"].dt.date).apply(
        lambda g: g["High"].max() - g["Low"].min(), include_groups=False)
    return float(rng.tail(20).median())


def prev_trading_close(api, contract, today):
    """上一個交易日的日盤收盤（週六會有週五夜盤，不能當交易日）。"""
    frames = []
    for back in range(1, 9):
        d = today - timedelta(days=back)
        try:
            df = pd.DataFrame({**api.kbars(contract, start=str(d), end=str(d))})
        except Exception:
            continue
        if not df.empty:
            frames.append(df)
    if not frames:
        return None
    all_df = pd.concat(frames, ignore_index=True)
    all_df["ts"] = pd.to_datetime(all_df["ts"])
    t = all_df["ts"].dt.time
    day = all_df[(t >= SESSION_OPEN) & (t < DAY_END)].sort_values("ts")
    if day.empty:
        return None
    last_day = day["ts"].dt.date.max()
    return float(day[day["ts"].dt.date == last_day]["Close"].iloc[-1])


def code_stamp():
    """
    正在跑的這份程式的指紋。

    【為什麼需要】看門狗會自動重啟面板（永豐 SDK 斷線會把行程帶掉），
    所以有人改了 broker.py 之後，下一次崩潰重啟就會自動載入那份改到一半的檔案，
    沒有任何提示。今天要確認「畫面上的程式＝正在跑的程式」，得去比對檔案時間戳
    跟行程啟動時間才推得出來 —— 那不該是判斷方式（lab-qa 退件第 9 條）。
    """
    out, newest = [], 0.0
    for f in ("broker.py", "live_panel.py"):
        p = HERE / f
        try:
            m = p.stat().st_mtime
            newest = max(newest, m)
            out.append(datetime.fromtimestamp(m).strftime("%m-%d %H:%M"))
        except Exception:
            out.append("?")
    # stale ＝ 硬碟上的程式比這個行程還新 ⇒ 我跑的不是最新的那份。
    # `panel_app.pyw` 靠這個決定「要不要先把舊伺服器收掉再開」，
    # 前端也靠它跳提醒 —— 不然關視窗再開只是接回同一個舊伺服器，
    # 改過的程式永遠載不進來，而且畫面上完全看不出來（2026-09-01 白重開一次）。
    return {"broker": out[0], "panel": out[1],
            "started": BOOT_AT.strftime("%m-%d %H:%M"),
            "stale": newest > BOOT_AT.timestamp() + 2}


def real_state(price, quote, age):
    """真實下單那張卡要的資料。順便在這裡跑停損監控 —— 兩者看的是同一組數字。"""
    try:
        check_real_position(price, age, quote if quote in ("live", "nodata") else "closed")
        broker.reconcile_tick()      # 【一定要獨立對帳】不能只靠 can_enter，見那個函式的說明
        snap = broker.snapshot()
        pos = snap.get("position")
        if pos:
            d = 1 if pos["dir"] == "long" else -1
            snap["float_pts"] = (round(d * ((price or pos["entry"]) - pos["entry"]), 1)
                                 if price else None)
            snap["tp"] = pos["entry"] + d * TP_POINTS
            snap["sl"] = pos["entry"] - d * SL_POINTS
        stale = REAL_STALE["since"]
        snap["stale_sec"] = round(time.time() - stale) if stale else None
        ok, why = broker.can_enter(price, quote == "live")
        snap["can_enter"], snap["why"] = ok, why
        snap["code"] = code_stamp()
        return snap
    except Exception as e:
        # 真實下單這一區出問題，絕不可以把整個面板帶掉
        return {"error": str(e)[:150], "live": False, "position": None,
                "can_enter": False, "why": "真實下單模組出錯，先不要用"}


def update_state(hist, today_state, vol_ref, now_time, replay=None, phase="live"):
    """
    phase: 'recording' = 08:45~09:30 下單時段（會記錄資料）
           'live'      = 日盤其他時間（照常顯示價格與趨勢，不記錄）
           'off'       = 夜盤／休市（只顯示價格與動能，沒有對照樣本）
    """
    min_idx = now_time.hour * 60 + now_time.minute
    feats = today_state.features(vol_ref, min_idx)
    CURRENT_STATE["today"] = today_state
    check_position(today_state.price)          # 每次更新都檢查有沒有觸及 ±100
    age = None if today_state.last_recv is None else round(time.time() - today_state.last_recv)
    sess = market_session()
    # 重播是拿歷史資料餵的，報價當然「新鮮」—— 不要被時鐘判成休市
    quote = "live" if replay else quote_state(today_state.price, age, sess)
    with state_lock:
        STATE.update({
            "period": hist.period, "n_days_total": hist.n_days,
            "clock": now_time.strftime("%H:%M:%S"), "replay": replay,
            "phase": phase, "market": sess, "quote": quote,
            "position": (dict(POSITION, float_pts=round(
                (1 if POSITION["dir"] == "long" else -1)
                * ((today_state.price or POSITION["entry"]) - POSITION["entry"]), 1))
                if POSITION else None),
            "today_trades": list(TODAY_TRADES),
            "age_sec": 0 if replay else age,
            "conn": CONN.copy(),
            "real": real_state(today_state.price, quote, age),
        })
        if today_state.price is None:
            # 【Bug A】一筆報價都還沒收到 ≠ 什麼都不能顯示。
            # 前端會照樣畫 K 線圖與日期選單（資料來自本機 csv／永豐歷史 K），
            # 只是把報價狀態誠實標成「休市中」或「盤中收不到報價」，並停用下單按鈕。
            STATE.update({"status": "waiting", "chips": None, "result": None,
                          "msg": QUOTE_MSG.get(quote, "等待第一筆成交…")})
            return

        # 夜盤：沒有日盤開高低，也沒有對照樣本 —— 只給價格與動能
        if feats is None:
            STATE.update({
                "status": "live",
                "chips": {"price": today_state.price,
                          "bid": today_state.bid, "ask": today_state.ask,
                          "is_mid": today_state.price_is_mid,
                          "idx": INDEX.get("price"), "idx_age": _index_age(),
                          "idx_chg": INDEX.get("chg"), "idx_pct": INDEX.get("pct"),
                          "basis": (round(today_state.price - INDEX["price"], 1)
                                    if INDEX.get("price") and today_state.price else None),
                          "mom5": today_state.price - today_state._price_ago(min_idx, 5),
                          "mom15": today_state.price - today_state._price_ago(min_idx, 15)},
                "result": None,
                "msg": "夜盤時段 —— 只顯示價格與動能，歷史對照樣本只涵蓋日盤。",
            })
            return

        STATE.update({
            "status": "live",
            "chips": {
                "price": today_state.price, "chg": feats["ret_open"], "gap": feats["gap"],
                "rng": feats["rng"], "pos": feats["pos"], "vol_ratio": feats["vol_ratio"],
                "mom5": feats["mom5"], "mom15": feats["mom15"],
                "bid": today_state.bid, "ask": today_state.ask,
                "is_mid": today_state.price_is_mid,
                "idx": INDEX.get("price"), "idx_age": _index_age(),
                "idx_chg": INDEX.get("chg"), "idx_pct": INDEX.get("pct"),
                "basis": (round(today_state.price - INDEX["price"], 1)
                          if INDEX.get("price") and today_state.price else None),
            },
            "result": hist.query(min_idx, feats, today_state.dayvol),
            "msg": None,
        })


def run_replay(hist, day_str):
    """用歷史某天的 1 分 K 重播，讓你先看效果（不連線、不用等開盤）。"""
    px = pd.read_csv(HERE / "txf_1min.csv")
    px["ts"] = pd.to_datetime(px["ts"])
    d = pd.to_datetime(day_str).date()
    g = px[(px["ts"].dt.date == d) & (px["ts"].dt.time >= SESSION_OPEN)
           & (px["ts"].dt.time <= WATCH_END)].sort_values("ts")
    if g.empty:
        print(f"{day_str} 沒有資料（休市日？）")
        return

    prev_days = sorted(x for x in px["ts"].dt.date.unique() if x < d)
    prev_close = None
    for pd_ in reversed(prev_days):
        s = px[(px["ts"].dt.date == pd_) & (px["ts"].dt.time >= SESSION_OPEN)
               & (px["ts"].dt.time < DAY_END)]
        if not s.empty:
            prev_close = float(s["Close"].iloc[-1])
            break

    vol_ref = float(hist.df["vol_cum"].median())
    t = Today(prev_close, current_dayvol())
    print(f"重播 {day_str}（每秒 = 盤中 1 分鐘，共 {len(g)} 分鐘）")
    for _, row in g.iterrows():
        t.feed(float(row["Close"]), int(row["Volume"]), row["ts"], in_session=True)
        update_state(hist, t, vol_ref, row["ts"].time(), replay=day_str)
        time.sleep(1)
    print("重播結束（面板停在最後狀態）。Ctrl+C 關閉。")
    while True:
        time.sleep(3600)


def main():
    # ⛔ 09:03:30 的掛勾只有這裡會接（接上去就會真的送單，見 auto_fire.py）。
    #    13:43:30 的收盤平倉掛勾同理（接上去就會真的送出平倉單）。
    #    沒有 global 的話下面那兩行只會建區域變數 ⇒ 接線靜靜地沒生效。
    global AUTO_SIG_HOOK, AUTO_EOD_HOOK
    if not MATRIX.exists():
        print("找不到 intraday.csv，請先跑 build_intraday.py")
        return
    if port_taken():
        print(f"連接埠 {PORT} 已被占用 —— 面板應該已經在跑了。")
        print(f"直接開 http://127.0.0.1:{PORT}/ 即可；要重開請先關掉原本那個。")
        sys.exit(2)          # 2 = 已在執行，start-panel.bat 看到就不再重試

    hist = History()
    print(f"歷史矩陣：{hist.n_days} 天（{hist.period}）")

    threading.Thread(target=serve, daemon=True).start()
    url = f"http://127.0.0.1:{PORT}/"
    print(f"面板網址：{url}")
    # 桌面 App（panel_app.pyw）自己會開一個專屬視窗，這裡再開一次就會多跳一個
    # 普通的瀏覽器分頁出來。它啟動時會帶 --no-open。
    if "--no-open" not in sys.argv:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    if "--replay" in sys.argv:
        run_replay(hist, sys.argv[sys.argv.index("--replay") + 1])
        return

    import shioaji as sj
    from shioaji import BidAskFOPv1, Exchange, TickFOPv1
    from _config import get_ca, get_credentials

    api_key, secret = get_credentials()
    vol_ref_by_min = hist.df.groupby("min_idx")["vol_cum"].median().to_dict()
    dayvol = current_dayvol()
    print(f"今天的波動度基準：{dayvol:.0f} 點（近 20 個交易日日盤高低幅的中位數）")

    # 可整天掛著：晚上開著 → 隔天 08:45 自動進入即時模式 → 09:30 收工存檔 → 繼續等下一天
    session = {"api": None, "contract": None, "date": None, "state": None, "saved": False}

    # ⛔⛔ 這兩個回呼跑在永豐 SDK 的執行緒上，而 st.feed() 更新的 self.price
    #     就是停損判斷看的那個價（永豐沒有停損單，停損活在面板的 Python 迴圈裡）。
    #     **裡面絕對不可以做同步磁碟 I/O。** 一筆卡 5ms，早上四萬筆就是 200 秒，
    #     塞住的時候塞住的是他的停損。TICKS.tick()／TICKS.bidask() 只 append 進
    #     記憶體佇列（實測單筆 < 0.15ms，400 筆合計 0.7ms），寫檔在另一條執行緒。
    def on_tick(exchange: Exchange, tick: TickFOPv1):
        ts = tick.datetime
        price, vol = float(tick.close), int(tick.volume)
        st = session["state"]
        if st is not None:
            # 價格任何時候都收（面板要一直顯示）；日盤才累積開高低與量。
            # 這一句排在落地之前 —— 停損看的價要先更新，其他事都排在後面。
            st.feed(price, vol, ts, in_session=SESSION_OPEN <= ts.time() < DAY_END)
        TICKS.tick(ts, price, vol)      # 只 append，不碰磁碟

    def on_bidask(exchange: Exchange, ba: BidAskFOPv1):
        try:
            # 原本就有的防線：五檔偶爾是空的／型別對不上，吞掉不要把 SDK 的執行緒打死
            bid, ask = float(ba.bid_price[0]), float(ba.ask_price[0])
            st = session["state"]
            if st is not None:
                st.feed_quote(bid, ask, ba.datetime)
        except Exception:
            return
        # 價差是事後絕對補不回來的東西，而這裡現在就拿得到 —— 跟成交記在同一個檔
        TICKS.bidask(ba.datetime, bid, ask)

    def pick_live_contract(api):
        """
        挑真正在交易的當月合約。

        【踩過的坑】TXFR1 是「近月連續」的合成代碼，只適合抓歷史資料 ——
        實測即時訂閱 60 秒收到 0 筆，同時間真正的當月合約 TXFH6 有 25 筆。
        用成交量挑，可以自動處理結算日換月（不必自己算第三個星期三）。
        """
        cat = getattr(api.Contracts.Futures, PRODUCT)
        cands = [c for c in cat
                 if not c.code.startswith(PRODUCT + "R")
                 and getattr(c, "delivery_month", "")]
        cands.sort(key=lambda c: c.delivery_month)
        near = cands[:3]
        try:
            snaps = api.snapshots(near)
            best = max(zip(near, snaps), key=lambda p: p[1].total_volume or 0)[0]
            return best
        except Exception:
            return near[0]

    def connect():
        """（重新）登入並訂閱。開盤前、以及偵測到斷線時都會呼叫。"""
        if session["api"] is not None:
            try:
                session["api"].logout()
            except Exception:
                pass
        api = sj.Shioaji()
        api.login(api_key=api_key, secret_key=secret)
        contract = pick_live_contract(api)
        # 【真實下單一定要憑證】沒啟用的話送單會被永豐擋下來（CA not activated）。
        # 模擬帳戶不需要憑證，所以模擬測兩輪都測不到這一關 —— 2026-09-01 第一次按真單才發現。
        ca = get_ca()
        if ca:
            try:
                api.activate_ca(ca_path=ca[0], ca_passwd=ca[1], person_id=ca[2])
                broker.CA_OK["ok"], broker.CA_OK["msg"] = True, None
                print("[憑證] 已啟用，可以真實下單")
            except Exception as e:
                broker.CA_OK["ok"] = False
                broker.CA_OK["msg"] = f"憑證啟用失敗：{str(e)[:120]}"
                print("[憑證] " + broker.CA_OK["msg"])
        else:
            broker.CA_OK["ok"] = False
            broker.CA_OK["msg"] = "還沒設定憑證（.env 裡的 SHIOAJI_CA_PATH / SHIOAJI_CA_PASSWD）"
        broker.configure(api, contract)      # 真實下單要用同一個連線與同一個合約
        api.set_on_tick_fop_v1_callback(on_tick)
        api.set_on_bidask_fop_v1_callback(on_bidask)
        # 成交 + 五檔都訂：成交價進模型，五檔負責讓畫面跟得上市場
        api.quote.subscribe(contract, quote_type=sj.constant.QuoteType.Tick,
                            version=sj.constant.QuoteVersion.v1)
        api.quote.subscribe(contract, quote_type=sj.constant.QuoteType.BidAsk,
                            version=sj.constant.QuoteVersion.v1)
        session["api"], session["contract"] = api, contract
        SESSION_REF["api"] = api
        CONN.update({"ok": True, "since": None, "retries": 0, "last_error": None,
                     "contract": contract.code,
                     "contract_name": getattr(contract, "name", "")})
        print(f"訂閱合約：{contract.code} {getattr(contract,'name','')}"
              f"（交割月 {contract.delivery_month}）")
        return api, contract

    def try_reconnect(reason):
        """斷線重連。失敗不會讓程式死掉，會留下錯誤訊息顯示在面板上。"""
        CONN["retries"] += 1
        if CONN["since"] is None:
            CONN["since"] = datetime.now().strftime("%H:%M:%S")
        print(f"[{datetime.now():%H:%M:%S}] {reason} → 第 {CONN['retries']} 次重連…")
        try:
            connect()
            print(f"[{datetime.now():%H:%M:%S}] 重連成功。")
            return True
        except Exception as e:
            msg = str(e)
            CONN.update({"ok": False, "last_error": msg[:200]})
            # API Key 綁 IP，VPN 換 IP 後登入會被擋 —— 這種錯要講清楚，別讓人以為只是網路慢
            if any(k in msg.lower() for k in ("ip", "unauthorized", "403", "401")):
                CONN["last_error"] = f"登入被拒（很可能是 IP 變了，例如開了 VPN）：{msg[:150]}"
            print(f"[{datetime.now():%H:%M:%S}] 重連失敗：{msg[:200]}")
            return False

    def seed_from_bars(st, today):
        """
        盤中啟動時，用當天已經發生的 1 分 K 把開盤價／最高／最低／量補起來。

        【沒有這段數字會是錯的】Today 只從「面板啟動的那一刻」開始累積，
        所以中午重開面板，對開盤／震幅／位階／量能全部會從那一刻重算 ——
        實測顯示過「震幅 20 點、量能 0.01 倍」這種明顯不合理的值。
        """
        api = SESSION_REF.get("api")
        now_t = datetime.now().time()
        if api is None or not (SESSION_OPEN <= now_t < DAY_END):
            return
        try:
            contract = getattr(api.Contracts.Futures, PRODUCT)[f"{PRODUCT}R1"]
            df = pd.DataFrame({**api.kbars(contract, start=str(today), end=str(today))})
            if df.empty:
                return
            df["ts"] = pd.to_datetime(df["ts"])
            g = df[(df["ts"].dt.time >= SESSION_OPEN)
                   & (df["ts"].dt.time <= now_t)].sort_values("ts")
            if g.empty:
                return
            st.open = float(g["Open"].iloc[0])
            st.high = float(g["High"].max())
            st.low = float(g["Low"].min())
            st.vol = float(g["Volume"].sum())
            st.price = float(g["Close"].iloc[-1])
            for r in g.itertuples():
                st.minute_close[r.ts.hour * 60 + r.ts.minute] = float(r.Close)
            print(f"  已用當日 {len(g)} 根 K 棒補齊開高低與量（開 {st.open:.0f}）")
        except Exception as e:
            print(f"  補齊當日資料失敗：{str(e)[:100]}")

    def start_day(today):
        try:
            api, contract = connect()
            prev_close = prev_trading_close(api, contract, today)
        except Exception as e:
            CONN.update({"ok": False, "last_error": str(e)[:200],
                         "since": datetime.now().strftime("%H:%M:%S")})
            print(f"[{today}] 連線失敗：{str(e)[:200]}")
            prev_close = session["state"].prev_close if session["state"] else None
        load_today_trades()
        st = Today(prev_close, dayvol)
        seed_from_bars(st, today)          # 盤中重啟時把當天已發生的部分補回來
        session.update({"date": today, "state": st, "saved": False})
        print(f"[{today}] 當日狀態已建立，上一交易日日盤收盤 {prev_close}")

    # 逐筆落地的寫檔執行緒要在 connect() 之前起來 —— start_day() 裡面就會訂閱報價了。
    # （就算晚起也不會掉資料，佇列會先接著；但沒必要讓它先積一段。）
    TICKS.start()

    # 【程式下單】的寫檔執行緒。⛔ 主迴圈只 put，一行 I/O 都不做（見 _auto_tick）。
    #    ⚠️ AUTO["started"] 要在這裡才打開：--replay 與測試治具不會走到這裡，
    #       所以重播歷史資料**不會**在 autotest/ 裡寫出假的一天。
    threading.Thread(target=_auto_worker, daemon=True).start()
    AUTO["started"] = True

    # 【自動下單】會真的送出委託單的那一段。⛔ **預設是關著的** ——
    #    沒有 tools/shioaji/AUTO_ORDERS_ON 這個檔就完全不送（而且那個檔只有他自己建）；
    #    就算有，也照樣受 REAL_ORDERS_ON 管（broker.enter → broker._send → is_live）。
    #    ⛔ 接線只能在這裡：--replay 與所有治具都走不到 main()，掛勾維持 no-op。
    auto_fire.configure(signal_at=SIGNAL_AT, signal_sec=SIGNAL_SEC,
                        late_ms=AUTO_LATE_MS, gap_s=AUTO_GAP_S, tp_points=TP_POINTS,
                        sig_fn=auto_sig, dirs_fn=auto_dirs, eod_at=EOD_CLOSE_AT)
    auto_fire.start()
    AUTO_SIG_HOOK = auto_fire.on_signal
    # ⛔⛔ 收盤自動平倉：**只平自動下單自己開的那一口**（auto_fire._looks_ours）。
    #    他自己手動進場的部位絕對不碰。
    AUTO_EOD_HOOK = auto_fire.on_eod
    _arm = auto_fire.arm()
    print("【自動下單】" + ("已開啟：" + _arm["msg"] +
                           ("（真單）" if broker.is_live() else "（真單開關關著 ⇒ 只會演練）")
                           if _arm["on"] else "關閉中 —— " + _arm["msg"]))
    print(f"【自動下單】收盤平倉 {EOD_CLOSE_AT}（⛔ 只平自動下單開的那一口）")

    # 啟動時一定要建立狀態，不能等到 08:30 ——
    # 否則半夜啟動的話 session["state"] 是 None，收到的報價全部被丟掉。
    start_day(date.today())
    session["opened"] = datetime.now().time() >= pd.Timestamp("08:30").time()
    with state_lock:
        # ⛔ 【這句不可以寫死】舊版不管幾點都說「等待 08:45 開盤」——
        #    他 2026-09-02 10:14 重開面板，畫面同時出現「盤中卻收不到報價」與
        #    「已連線，等待 08:45 開盤」，兩句互相打臉，看了只會更慌。
        #    盤中剛啟動時真正的情況是「還沒收到第一筆 tick」，那才是該說的話。
        STATE.update({"status": "waiting", "msg": boot_msg(),
                      "quote": quote_state(None, None, market_session()),
                      "market": market_session(),
                      "period": hist.period, "n_days_total": hist.n_days})
    threading.Thread(target=poll_index, daemon=True).start()
    threading.Thread(target=poll_phone, daemon=True).start()
    print("面板已啟動，可以整天掛著。每天 08:45~09:30 自動進入即時模式。（Ctrl+C 結束）")

    last_retry = 0.0
    try:
        while True:
            now = datetime.now()
            today, t = now.date(), now.time()

            # 每天 08:30 重新建立當日連線與狀態。
            # 兩種情況要重建：跨到新的一天，或今天還沒做過開盤前的重建
            # （例如面板是半夜啟動的，那次建立的狀態算不上「當日開盤狀態」）。
            if t >= pd.Timestamp("08:30").time() and (
                    session["date"] != today or not session.get("opened")):
                start_day(today)
                session["opened"] = True
            elif session["date"] != today and t < pd.Timestamp("08:30").time():
                # 過了午夜但還沒到 08:30：沿用現有連線（夜盤還在跑，不能斷），
                # 但「昨天的日盤數字」與「昨天的練習紀錄」一定要清掉 ——
                # 不清的話這段時間（正好是他早上開面板的時候）看到的是昨天的
                # 開高低量、震幅、位階、量能，而且練習清單會把昨天那筆算成今天，
                # 凌晨夜盤再下一單還會把昨天那筆一起寫進今天的檔案。
                session["date"] = today
                session["opened"] = False
                if session["state"] is not None:
                    reset_for_new_day(session["state"])
                load_today_trades()
                print(f"[{today}] 跨日：已清掉昨天的日盤數字與練習紀錄，"
                      f"等 08:30 重建當日狀態")

            st = session["state"]

            # ---- 斷線看門狗
            # 只要「市場應該有在交易」就要盯著：日盤 08:45~13:45、夜盤 15:00~05:00。
            # 之前只盯日盤，夜盤斷線會顯示警告卻永遠不重連 —— 那是 bug。
            # 星期也要看：週日晚上沒有夜盤、週一凌晨沒有夜盤尾巴，
            # 否則整個週末都會被判成「盤中收不到報價」而一直重連（見 market_session）。
            sess = market_session(now)
            in_day = sess == "day"
            market_open = sess in ("day", "night")

            if market_open:
                quiet = (time.time() - st.last_recv) if (st and st.last_recv) else None
                lost = (quiet is not None and quiet > STALE_SECONDS) or not CONN["ok"]
                if lost and time.time() - last_retry > RECONNECT_EVERY:
                    last_retry = time.time()
                    CONN["ok"] = False
                    where = "日盤" if in_day else "夜盤"
                    try_reconnect(f"{where}已 {int(quiet) if quiet else '?'} 秒沒收到報價")
            elif CONN["ok"] is False and time.time() - last_retry > RECONNECT_EVERY * 5:
                # 休市時段（13:45~15:00、05:00~08:45）慢慢重試就好
                last_retry = time.time()
                try_reconnect("休市時段定期重試")
            # 【程式下單】09:03:30 記一筆、13:47 之後補算 ±100。
            # ⚠️ 只有整數比較與 queue.put_nowait，⛔ 一行 I/O 都沒有 ——
            #    這條迴圈就是他的停損（永豐沒有停損單）。
            # ⛔⛔ **一定要走 _auto_tick_guarded**（那一層 try 在裡面）：這一頁是研究功能，
            #    它出任何差錯都不可以讓主迴圈中斷 —— 中斷了就是停損不再監控，
            #    而且畫面上看不出來。⛔ 不可以在這裡直接呼叫 _auto_tick。
            #    ⚠️ 但**不可以安靜地吞**：計數 ＋ 主控台警告 ＋ 畫面上看得到（禁止「安靜地少」）。
            _auto_tick_guarded(st, now, sess)

            min_idx = now.hour * 60 + now.minute
            vol_ref = vol_ref_by_min.get(min_idx, 1.0)

            if st is None:
                # 連狀態物件都還沒建（啟動的頭幾秒）。仍然要把報價狀態填進去，
                # 前端才分得出「休市中」與「盤中收不到報價」，K 線圖也才畫得出來。
                q = quote_state(None, None, sess)
                with state_lock:
                    STATE.update({"status": "waiting", "clock": now.strftime("%H:%M:%S"),
                                  "market": sess, "quote": q, "phase": "off",
                                  "position": None, "today_trades": list(TODAY_TRADES),
                                  "chips": None, "result": None, "age_sec": None,
                                  "conn": CONN.copy(),
                                  "msg": QUOTE_MSG.get(q, "等待第一筆成交…")})
            elif in_day and session["date"] == today and SESSION_OPEN <= t <= WATCH_END:
                # 下單時段：即時顯示 + 記錄資料
                update_state(hist, st, vol_ref, t, phase="recording")
            elif in_day and session["date"] == today and WATCH_END < t < DAY_END:
                # 日盤其餘時間：照常顯示價格與趨勢，但不記錄
                if not session["saved"] and st.open is not None:
                    session["saved"] = True
                    ok, msg = sync_to_cloud()
                    print(f"[{today}] 09:30 下單時段結束。雲端同步：{msg}")
                    # 逐筆落地的成績單。dropped 不是 0 就代表那個早上有一段是缺的
                    # （檔案裡也有痕跡列），下次要把 max_queue 或 flush_every 調一調。
                    ts_stat = TICKS.stats()
                    print(f"[{today}] 逐筆落地：寫出 {ts_stat['written']} 列"
                          f"，佇列滿丟棄 {ts_stat['dropped']} 列"
                          f"，寫檔失敗丟失 {ts_stat['lost_io']} 列"
                          f"（tick_logs/{today}.jsonl）")
                update_state(hist, st, vol_ref, t, phase="live")
            else:
                # 夜盤／收盤後：只顯示價格與動能（歷史對照樣本只涵蓋日盤）
                update_state(hist, st, vol_ref, t, phase="off")
            time.sleep(0.25)   # 模型查詢僅 12ms，跑 4Hz 沒有負擔
    except KeyboardInterrupt:
        print("\n收到中止訊號，關閉中…")
    finally:
        if session["api"] is not None:
            try:
                session["api"].logout()
            except Exception:
                pass


def session_contract():
    return CONN.get("contract")


if __name__ == "__main__":
    main()
