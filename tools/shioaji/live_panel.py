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
import sys
import threading
import time
import webbrowser
from datetime import date, datetime, timedelta
from datetime import time as dtime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pandas as pd

import broker            # 真實下單。預設 dry run，見那個檔開頭的說明

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
CURRENT_STATE = {"today": None}      # 讓 HTTP handler 拿得到當前的 Today 物件
# 最後一次有瀏覽器來要資料的時間。桌面 App 靠它判斷「視窗是不是被關掉了」——
# 用 Edge 的 --app-id 開視窗時，啟動的那個行程會立刻結束（Edge 交棒給既有的行程），
# 所以不能用「等那個行程結束」來判斷關窗，會一開就誤判（實測 1 秒就誤判）。
LAST_CLIENT = {"at": 0.0}
SESSION_REF = {"api": None}          # K 線圖要用它去跟永豐要 K 棒

# 加權指數（現貨）：Benson 下單時會看，所以面板一起顯示，並算出基差。
# 【只顯示，不做分析】永豐的指數歷史 1 分 K 只有 54 個破碎的交易日，
# 測不出東西 —— 這裡純粹是多一個客觀數字，不是訊號。
# 現貨 09:00 才開盤、13:30 收，比期貨晚開早收，所以會有「尚未開盤」的空窗。
INDEX = {"price": None, "chg": None, "pct": None, "at": None, "contract": None}
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


def check_real_position(price, age):
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
            g, base = session_frame(d)
            if g is None or g.empty:
                return {"date": str(d), "bars": [], "trades": [], "tf": tf, "full": True,
                        "error": None if SESSION_REF.get("api") else "尚未連線，且本機沒有這幾天的資料"}
            if d == date.today():
                g = overlay_live(g)          # 先在 1 分 K 這一層換掉還沒收完的那幾分鐘
            bars = to_timeframe(g, tf, base)
        except Exception as e:
            return {"error": str(e)[:120]}
        return {"date": str(d), "bars": bars, "trades": _day_trades(d), "tf": tf,
                "full": True, "night_open": base.strftime("%Y-%m-%d"),
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
# 今天的 kbars 一分鐘才變一次，不必每次要圖都跟永豐重抓（實測整支 0.5~0.85 秒）。
# 還沒收完的那幾分鐘由 overlay_live() 用即時 tick 換掉，所以壓成 20 秒不會讓畫面變舊。
TODAY_TTL = 20


def _csv_stamp():
    """tmf_1min.csv 的 mtime，當快取鍵用（檔案換了就代表資料可能補齊了）。"""
    f = HERE / "tmf_1min.csv"
    return f.stat().st_mtime if f.exists() else None


def _cached_raw(cache, key, days):
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
        if time.time() - hit["at"] < SESS_RETRY:
            return hit["df"]
    rep = {}
    df = _raw_days(days, report=rep)
    cache[key] = {"df": df, "at": time.time(), "mt": stamp,
                  "partial": bool(rep.get("incomplete"))}
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

    back = _cached_raw(_SESS_BACK, key, [d - timedelta(days=k) for k in range(5, 0, -1)])
    # 今天的 K 棒還在長，不能快取；過去的日子抓一次就夠
    own = _today_raw(d) if d == today else _cached_raw(_SESS_OWN, key, [d])

    pool = [x for x in (back, own) if x is not None and not x.empty]
    if not pool:
        return None, None
    px = pd.concat(pool, ignore_index=True).drop_duplicates("ts").sort_values("ts")
    tt, dd = px["ts"].dt.time, px["ts"].dt.date

    rows, n = [], None
    nights = px[(tt >= NIGHT_OPEN) & (dd < d)]
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
        return None, None
    g = pd.concat(rows, ignore_index=True).drop_duplicates("ts").sort_values("ts")
    base = (pd.Timestamp.combine(n, NIGHT_OPEN) if n is not None
            else pd.Timestamp.combine(d, SESSION_OPEN))
    return g, base


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
.pager .jump2:hover{background:rgba(227,169,81,.24)}
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
.quota{font-size:11px; color:var(--faint); font-family:var(--font-mono);
  text-align:right; margin-top:9px}
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
body.boot .right>#trade>.card{animation-delay:.10s}
body.boot .right>#stats>.card{animation-delay:.16s}
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
</style></head><body><div class="app">
<div class="topbar">
  <div class="brand"><div class="mark">&#9702;</div>
    <div><div class="nm">早盤儀表板</div><div class="sub" id="sub">連線中…</div></div></div>
  <div class="tabs">
    <button data-tab="live" class="on">即時</button>
    <button data-tab="review">回顧</button>
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
  </div><div class="right"><div id="trade"></div><div id="real"></div><div id="stats"></div></div></div>
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

<div class="foot">只顯示已經發生的客觀數字，不做預測、不給買賣訊號。<br>練習下單為模擬，不會送單到永豐。</div>
</div>
<script>
var WIN=7;
const f=(n,d=0)=>n==null?'—':Number(n).toFixed(d);
const sgn=v=>v>0?'up':v<0?'down':'flat';
const pm=(v,d=0)=>(v>0?'+':'')+f(v,d);

var lastMkt='', lastTrade='', lastStats='', lastWarn='', lastReal='', statsCache=null, statsAt=0;

/* ---------------- 真實下單 ----------------
   ⚠️ REAL_ON 刻意不記進 localStorage：每次開面板都要重新打開。
   記住狀態的話，某天心不在焉點到就是一個真實部位。 */
var REAL_ON=false, HOLD_MS=650, holdTimer=null, holdingNow=false;
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
function onUp(){ document.querySelectorAll('.rbtn.holding').forEach(holdEnd); }
function holdEnd(el){
  clearTimeout(holdTimer); holdTimer=null; holdingNow=false;
  if(!el) return;
  el.classList.remove('holding');
  el.removeEventListener('mouseleave',onLeave);
  window.removeEventListener('mouseup',onUp,true);
}
function realFire(dir){
  holdingNow=false;
  fetch('/api/real/enter',{method:'POST',headers:{'Content-Type':'application/json'},
                           body:JSON.stringify({dir:dir})})
   .then(r=>r.json()).then(r=>{ if(!r.ok) alert(r.msg||'送不出去'); lastReal=''; tick(true); })
   .catch(()=>alert('送不出去，面板可能剛好在重啟'));
}
function realClose(){
  fetch('/api/real/close',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})
   .then(r=>r.json()).then(r=>{ if(!r.ok) alert(r.msg||'平不掉'); lastReal=''; tick(true); })
   .catch(()=>{});
}
function realBox(s){
 const R=s.real||{}, P=R.position;
 let h='<div class="card real'+(P?(' holding'+(P.dir==='short'?' sh':'')):'')+'">'+
   '<div class="rtop"><div>'+
   '<div class="sec-head" style="margin-bottom:2px"><h2>真實下單</h2></div>'+
   '<div class="rlabel">'+(!REAL_ON?'關閉中・按右邊打開'
     :(R.live?'<b style="color:var(--gold)">真的會送單</b>　微台 1 口・固定 ±100'
             :'<b>演練模式</b>　單子照組、照記錄，<b>不會送出去</b>'))+'</div>'+
   '</div><div class="sw'+(REAL_ON?' on':'')+'" data-rt="1"><i></i></div></div>';
 if(!REAL_ON) return h+'</div>';
 h+='<div class="rbody">';
 if(R.error){ h+='<div class="whyoff">'+esc(R.error)+'</div></div></div>'; return h; }
 if(P){
   const fl=R.float_pts;
   h+='<div class="rpos"><div class="big '+sgn(fl)+'">'+(fl==null?'—':pm(fl))+'</div>'+
      '<span class="tag '+(P.dir==='long'?'l':'s')+'">'+(P.dir==='long'?'▲ 做多':'▼ 做空')+
      ' '+P.qty+' 口</span>'+
      (P.entry_time?'<span class="faint" style="font-size:12px">'+P.entry_time+' 進場</span>':'')+
      '</div>'+
      rrow('進場價',f(P.entry))+
      rrow('停利　+'+f(100),f(R.tp)+'　<span class="faint">已掛在券商</span>')+
      rrow('停損　−'+f(100),f(R.sl)+'　<span class="up">由面板監控</span>')+
      '<div class="rbtns"><button class="rbtn s" data-rclose="1">立刻平倉</button></div>';
   h+= R.stale_sec!=null
     ? '<div class="ralarm bad"><b style="color:var(--up)">⚠ 報價已中斷 '+R.stale_sec+' 秒</b><br>'+
       '停損是由這台電腦監控的，現在監控不到。<b>請立刻到大戶投確認部位</b>。</div>'
     : '<div class="ralarm">停損靠這台電腦。<b style="color:var(--gold)">'+
       '面板關掉、電腦睡著、網路斷掉都會失效</b>。</div>';
   if(P.recovered) h+='<div class="whyoff">這筆是面板重啟後從券商對帳撿回來的，'+
     '停利單的下落請自己到大戶投確認。</div>';
 } else {
   const px=(s.chips||{}).price, ok=R.can_enter;
   // 還沒選方向就不列停利停損 —— 兩個方向的數字互相干擾（Benson 2026-08-28）
   h+=rrow('現價',f(px))+rrow('口數','1 口　<span class="faint">固定，不能改</span>')+
      '<div class="rbtns">'+fireBtn('long',px,!ok)+fireBtn('short',px,!ok)+'</div>'+
      (ok?'':'<div class="whyoff">'+esc(R.why||'現在不能下單')+'</div>')+
      '<div class="quota">今天真實進場 '+(R.entries_today||0)+' / '+(R.max_entries||3)+'</div>';
 }
 return h+'</div></div>';
}
function rrow(k,v){ return '<div class="rrow"><span class="k">'+k+'</span><span class="v">'+v+'</span></div>'; }
function fireBtn(dir,px,dis){
 const long=dir==='long';
 const tp=px==null?null:(long?px+100:px-100), sl=px==null?null:(long?px-100:px+100);
 return '<button class="rbtn '+(long?'b':'s')+'"'+(dis?' disabled':'')+
   ' data-rdir="'+dir+'"><span class="fill"></span><span>'+(long?'買進 做多':'賣出 做空')+
   '</span><span class="hint">'+(tp==null?'&nbsp;':'停利 '+f(tp)+'　停損 '+f(sl))+'</span></button>';
}

/* ---------------- 心得：跟手機 App 同一個 note 欄位 ----------------
   面板是即時下單，成交當下沒空打字，所以心得一律「事後補寫」：
   點那一筆就展開輸入框，存檔後跟練習紀錄一起同步上雲，手機開 App 就看得到。
   NOTE.key 前面那個字母是分區（t=練習下單、s=練習成績、r=回顧），
   因為同一筆交易會同時出現在好幾個清單裡，不分區就會有兩個 id 相同的輸入框。 */
var NOTE={key:null,text:''};
function nkey(ns,t){
  return ns+'|'+(t.date||'')+'|'+String(t.time||'').slice(0,5)+'|'+Math.round(t.entry);
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
 if(nf||!nEditing('t')) setHTML('trade',tradeBox(s));
 // 【長按期間不准重繪這張卡】卡片裡有現價，每次報價變動整塊 innerHTML 就被換掉，
 // 按住的那顆按鈕當場被銷毀 —— 畫面看起來像「按到一半自己취消」。
 // 更糟的是計時器還握著那顆已經不在畫面上的按鈕，時間到照樣送單：
 // 使用者以為取消了，單卻出去了（2026-09-01 Benson 回報，查證後沒送出是因為
 // 剛好還有另一個 bug 把它擋掉，不是設計正確）。
 if(!holdingNow) setHTML('real',realBox(s));
 if(nf||!nEditing('s')) setHTML('stats',statsBox(statsCache));
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
 setEl('mfbody',
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
 const B=G.all.slice(G.from,G.to), T=(barsCache.trades)||[], P=s.position;
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
 T.forEach(t=>{ if(inView(t.time)){ hi=Math.max(hi,t.entry,t.exit); lo=Math.min(lo,t.entry,t.exit);} });
 if(P&&live&&G.live){ hi=Math.max(hi,P.tp); lo=Math.min(lo,P.sl); }
 const pad=(hi-lo)*0.08||10; hi+=pad; lo-=pad;
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
 for(let k=0;k<=5;k++){
   const v=lo+(hi-lo)*k/5, yy=y(v);
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
   const tri=long?('M'+(X-7.5)+' '+(Y+16)+' L'+X+' '+(Y+3.5)+' L'+(X+7.5)+' '+(Y+16)+' Z')
                 :('M'+(X-7.5)+' '+(Y-16)+' L'+X+' '+(Y-3.5)+' L'+(X+7.5)+' '+(Y-16)+' Z');
   g+='<path d="'+tri+'" fill="'+col+'" stroke="#0E1116" stroke-width="1.8" stroke-linejoin="round"/>'+
      '<circle cx="'+X.toFixed(1)+'" cy="'+Y.toFixed(1)+'" r="2.6" fill="'+col+
      '" stroke="#0E1116" stroke-width="1.2"/>';
   g+=axisChip(Y,String(Math.round(t.entry)),col);
   // 進出場很近就把兩枚膠囊合併成一枚，不要互相推擠（他的單多半 5~15 分鐘就結束）
   const near=hasExit&&(XE-X)<110;
   if(!near) laneX.push([X,(long?'▲ 進 ':'▼ 進 ')+t.time,col]);
   else laneX.push([(X+XE)/2,(long?'▲ ':'▼ ')+t.time+'→'+t._exit_time.slice(0,5)+
     '　'+pm(t._net),ec]);
   if(hasExit){
     g+='<line x1="'+XE.toFixed(1)+'" y1="'+YE.toFixed(1)+'" x2="'+XE.toFixed(1)+'" y2="'+(H-BOT)+
        '" stroke="'+ec+'" stroke-width="1" stroke-dasharray="2 4" opacity=".32"/>'+
        '<rect x="'+(XE-5.6).toFixed(1)+'" y="'+(YE-5.6).toFixed(1)+'" width="11.2" height="11.2"'+
        ' rx="2.4" transform="rotate(45 '+XE.toFixed(1)+' '+YE.toFixed(1)+')" fill="'+ec+
        '" stroke="#0E1116" stroke-width="1.8"/>';
     g+=axisChip(YE,String(Math.round(t.exit)),ec);
     if(!near) laneX.push([XE,'出 '+t._exit_time.slice(0,5)+'　'+pm(t._net),ec]);
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
 const r2=loading
   ? '<span>載入中…</span>'
   : ((noS&&noS!==dayS)?'<span>含 '+noS+' 夜盤 15:00 起</span><span class="sep">·</span>':'')+
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
     const busy=barsPending||curDay()!==((barsCache&&barsCache.date)||'');
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

function tradeBox(s){
 const P=s.position, T=s.today_trades||[];
 let h='<div class="sec-head"><h2>練習下單</h2><span class="warn">模擬・不會送單</span></div><div class="card">';
 if(P){
   h+='<div class="pnl"><div class="v '+sgn(P.float_pts)+'">'+pm(P.float_pts)+'</div>'+
      '<div class="l">'+(P.dir==='long'?'做多':'做空')+'　進場 '+f(P.entry)+'　'+P.entry_time+'</div></div>'+
      '<div class="plimit"><span>停利 '+f(P.tp)+'</span><span>停損 '+f(P.sl)+'</span></div>'+
      '<div class="btns"><button class="btn flat2" data-act="close">手動平倉</button>'+
      '<button class="btn ghost" data-act="undo">取消</button></div>'+
      noteBox('t|open',P.note,'data-nopen="1"','＋ 記下現在為什麼這樣做',
              '現在為什麼想這樣做？（平倉後會留在這筆紀錄裡）');
 } else {
   // 【紀錄正確性】沒有即時報價就不能開單 —— 拿舊價／收盤價記進練習成績，那筆成績是假的。
   // 這不是 UX 取捨，所以按鈕真的停用（後端 /api/enter 也擋一次）。
   const q=quoteState(s), off=q!=='live', dis=off?' disabled':'';
   h+='<div class="btns"><button class="btn long" data-act="long"'+dis+'>&#9650; 做多</button>'+
      '<button class="btn short" data-act="short"'+dis+'>&#9660; 做空</button></div>';
   if(off) h+='<div class="whyoff">'+(q==='closed'
     ?'休市中，沒有即時報價 —— 練習下單要用當下的真實成交價才有意義，開盤後才能按。'
     :'目前收不到報價，無法確定進場價 —— 恢復報價後才能按。')+'</div>';
 }
 if(T.length){
   let sum=0; T.forEach(t=>sum+=t._net);
   h+='<div class="list">';
   T.slice().reverse().forEach(t=>h+=row(t,'t'));
   h+='</div><div class="plimit" style="margin:12px 0 0">今天 '+T.length+' 筆　合計 '+pm(sum)+' 點</div>';
 }
 return h+'</div>';
}
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
 let h='<div class="sec-head"><h2>練習成績</h2><span class="count">共 '+ST.total+' 筆</span></div>'+
  '<div class="card">'+seg+
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
 fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:body})
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
         entry:ed.getAttribute('data-ne')||'', open:ed.hasAttribute('data-nopen')};
   nrepaint(); return;
 }
 const sv=e.target.closest('[data-nsave]');
 if(sv){
   const el=document.getElementById('tnote'), txt=el?el.value:NOTE.text;
   const b=NOTE.open?{open:true,text:txt}
     :{date:NOTE.date,time:NOTE.time,entry:Number(NOTE.entry),text:txt};
   sv.disabled=true;
   fetch('/api/note',{method:'POST',headers:{'Content-Type':'application/json'},
                      body:JSON.stringify(b)})
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
 if(e.target.closest('[data-rt]')){ realToggle(); return; }
 if(e.target.closest('[data-rclose]')){
   if(confirm('確定要立刻平倉？')) realClose();
   return; }
});
document.addEventListener('mousedown', function(e){
 const b=e.target.closest('[data-rdir]');
 if(b&&!b.disabled) holdStart(b,b.getAttribute('data-rdir'));
});
// 切走視窗（Alt+Tab、鎖螢幕）也算放開 —— 手離開鍵鼠了就不該繼續倒數
window.addEventListener('blur',function(){
 document.querySelectorAll('.rbtn.holding').forEach(holdEnd);
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
 fetch('/api/replay',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({date:RP.date,judged:!!J,
     dir:J?J.dir:null,entry:J?J.entry:null,time:J?J.time:null,note:J?J.note:'',
     exit:Rr?Rr.exit:null,exit_time:Rr?Rr.time:null,reason:Rr?Rr.reason:null,
     points:Rr?Rr.points:null,net:Rr?Rr.net:null,
     same_dir:!!(rt&&J&&rt.dir===J.dir),day_dir:rt?rt.dir:null,day_time:rt?rt.time:null})})
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
 document.getElementById('tab-live').hidden=(t!=='live');
 document.getElementById('tab-review').hidden=(t!=='review');
 document.querySelectorAll('.tabs button').forEach(b=>
   b.classList.toggle('on',b.getAttribute('data-tab')===t));
 if(t==='review'){ lastPane=''; if(!RV) rvFetch(); else rvRender(); }
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
rvBind();
tick(); setInterval(tick,500);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        st = CURRENT_STATE.get("today")
        price = st.price if st else None

        if self.path == "/api/real/enter":
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
            return self._json(200 if ok else 500,
                              {"ok": ok, "msg": err or ("已送出" if broker.is_live()
                                                        else "演練：單子已組好，沒有送出")})

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
            LAST_CLIENT["at"] = time.time()      # 有人在看（桌面 App 靠這個判斷關窗）
            with state_lock:
                payload = json.dumps(STATE, ensure_ascii=False).encode()
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
    背景每 3 秒抓一次加權指數快照。

    用快照輪詢而非訂閱：指數的 tick callback 型別跟期貨不同，
    而 3 秒一次成本極低，也不會卡住主迴圈（主迴圈 0.25 秒一圈）。
    """
    while True:
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
        time.sleep(3)


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


def real_state(price, quote, age):
    """真實下單那張卡要的資料。順便在這裡跑停損監控 —— 兩者看的是同一組數字。"""
    try:
        check_real_position(price, age)
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
                          "idx": INDEX.get("price"),
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
                "idx": INDEX.get("price"),
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

    def on_tick(exchange: Exchange, tick: TickFOPv1):
        ts = tick.datetime
        st = session["state"]
        if st is None:
            return
        # 價格任何時候都收（面板要一直顯示）；日盤才累積開高低與量
        st.feed(float(tick.close), int(tick.volume), ts,
                in_session=SESSION_OPEN <= ts.time() < DAY_END)

    def on_bidask(exchange: Exchange, ba: BidAskFOPv1):
        st = session["state"]
        if st is None:
            return
        try:
            st.feed_quote(float(ba.bid_price[0]), float(ba.ask_price[0]), ba.datetime)
        except Exception:
            pass

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

    # 啟動時一定要建立狀態，不能等到 08:30 ——
    # 否則半夜啟動的話 session["state"] 是 None，收到的報價全部被丟掉。
    start_day(date.today())
    session["opened"] = datetime.now().time() >= pd.Timestamp("08:30").time()
    with state_lock:
        STATE.update({"status": "waiting", "msg": "已連線，等待 08:45 開盤…",
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
