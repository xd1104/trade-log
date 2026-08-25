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
SESSION_REF = {"api": None}          # K 線圖要用它去跟永豐要 K 棒

# 加權指數（現貨）：Benson 下單時會看，所以面板一起顯示，並算出基差。
# 【只顯示，不做分析】永豐的指數歷史 1 分 K 只有 54 個破碎的交易日，
# 測不出東西 —— 這裡純粹是多一個客觀數字，不是訊號。
# 現貨 09:00 才開盤、13:30 收，比期貨晚開早收，所以會有「尚未開盤」的空窗。
INDEX = {"price": None, "at": None, "contract": None}
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
            bars = to_timeframe(g, tf, base)
            if d == date.today():
                bars = merge_live_tail(bars, tf, base)
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
        bars = to_timeframe(g, tf)
        if d == date.today():
            bars = merge_live_tail(bars, tf)
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


def _raw_days(days):
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
SESS_RETRY = 60     # 抓不到資料時隔多久再試一次


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
        if hit["df"] is not None or time.time() - hit["at"] < SESS_RETRY:
            return hit["df"]
    df = _raw_days(days)
    cache[key] = {"df": df, "at": time.time(), "mt": stamp}
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
    own = _raw_days([d]) if d == today else _cached_raw(_SESS_OWN, key, [d])

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


def merge_live_tail(bars, tf, base=None):
    """
    用即時 tick 補完「還在形成中」的那根 K 棒。

    永豐的 kbars 是一分鐘給一次，所以最新那根的量每分鐘才跳一次，
    看起來像卡住不動；大戶投是用即時成交累加的，才會一直在跑。
    這裡把「kbars 還沒涵蓋到的那幾分鐘」用 tick 累積的資料補上去。

    【夜盤要跨午夜】minute_bar 的鍵只有「分鐘」沒有日期。狀態每天 08:30 重建，
    所以在 08:45 之前看到的「15:00 以後」那些鍵，指的是昨晚的夜盤，不是今晚。
    """
    st = CURRENT_STATE.get("today")
    if st is None or not st.minute_bar:
        return bars
    now = datetime.now()
    nowt, today = now.time(), now.date()
    base = pd.Timestamp(base) if base is not None else pd.Timestamp.combine(today, SESSION_OPEN)

    live = []
    for mi, b in st.minute_bar.items():
        hh, mm = divmod(int(mi), 60)
        t0 = dtime(hh, mm)
        if not (SESSION_OPEN <= t0 < DAY_END or t0 >= NIGHT_OPEN or t0 <= NIGHT_TAIL):
            continue                            # 13:45~15:00 沒在交易，不畫
        d0 = today - timedelta(days=1) if (nowt < SESSION_OPEN and t0 >= NIGHT_OPEN) else today
        live.append((pd.Timestamp.combine(d0, t0), b))
    if not live:
        return bars
    live.sort()

    covered = base                              # kbars 已經完成到哪一刻
    if bars:
        last = bars[-1]
        covered = pd.Timestamp(f'{last["d"]} {last["t"]}') + pd.Timedelta(minutes=tf)
    extra = [(dt, b) for dt, b in live if dt >= covered]
    if not extra:
        return bars

    # 把多出來的分鐘照 tf 分組，接到後面
    out, slots = list(bars), {}
    for dt, b in extra:
        slots.setdefault(int((dt - base).total_seconds() // (tf * 60)), []).append(b)
    for k in sorted(slots):
        ms = slots[k]
        start = base + pd.Timedelta(minutes=tf * k)
        t, ds = start.strftime("%H:%M"), start.strftime("%Y-%m-%d")
        bar = {"t": t, "d": ds, "o": ms[0]["o"], "h": max(m["h"] for m in ms),
               "l": min(m["l"] for m in ms), "c": ms[-1]["c"],
               "v": sum(m["v"] for m in ms)}
        if out and out[-1]["t"] == t and out[-1].get("d") == ds:      # 同一根 → 合併
            prev = out[-1]
            out[-1] = {"t": t, "d": ds, "o": prev["o"], "h": max(prev["h"], bar["h"]),
                       "l": min(prev["l"], bar["l"]), "c": bar["c"],
                       "v": prev["v"] + bar["v"]}
        else:
            out.append(bar)
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


# ---------------------------------------------------------------- 網頁

PAGE = r"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>早盤盤面儀表板</title><style>
/* 配色與版型對齊 trade-log App（css/style.css）。
   含台股慣例：紅色=漲/贏、綠色=跌/輸 —— 跟 App 一致，避免看反方向。 */
:root{
  --bg:#0F1218; --surface:#171C25; --surface-2:#1F2530; --line:#262D39;
  --text:#E7E9ED; --dim:#8B92A0; --faint:#5A616E;
  --gold:#E3A951; --gold-soft:rgba(227,169,81,.14);
  --up:#EE5A54; --up-soft:rgba(238,90,84,.16);
  --down:#34B37E; --down-soft:rgba(52,179,126,.16);
  --radius:16px; --radius-sm:11px;
  --font-sans:-apple-system,BlinkMacSystemFont,"PingFang TC","Microsoft JhengHei","Noto Sans TC","Segoe UI",Roboto,sans-serif;
  --font-mono:ui-monospace,"SF Mono","JetBrains Mono","Roboto Mono",Menlo,Consolas,monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg); color:var(--text); font-family:var(--font-sans); line-height:1.5;
  background-image:radial-gradient(1200px 500px at 50% -8%, rgba(227,169,81,.06), transparent 70%);
  background-repeat:no-repeat; min-height:100vh}
/* 電腦大螢幕：左邊大圖、右邊操作區。Benson 只在電腦上開這個面板。 */
.app{max-width:1500px; margin:0 auto; padding:0 24px 24px}
.cols{display:grid; grid-template-columns:minmax(0,1fr) 380px; gap:18px; align-items:start}
@media(max-width:1100px){ .cols{grid-template-columns:minmax(0,1fr)} }
.right{display:flex; flex-direction:column; gap:12px}
.topbar{display:flex; align-items:center; justify-content:space-between; padding:20px 4px 16px}
.brand{display:flex; align-items:center; gap:10px}
.brand .mark{width:34px;height:34px;border-radius:10px;display:grid;place-items:center;
  background:var(--gold-soft); color:var(--gold); font-size:17px}
.brand .nm{font-size:15px; font-weight:650}
.brand .sub{font-size:11px; color:var(--faint); letter-spacing:1.2px; margin-top:1px}
.clock{text-align:right; font-family:var(--font-mono); font-variant-numeric:tabular-nums}
.clock .d{font-size:13.5px; font-weight:600}
.clock .w{font-size:11px; color:var(--faint)}
.card{background:var(--surface); border:1px solid var(--line); border-radius:var(--radius);
  padding:18px 20px; margin-bottom:12px}
.sec-head{display:flex; align-items:center; justify-content:space-between; margin:0 4px 8px}
.sec-head h2{font-size:12.5px; margin:0; color:var(--dim); letter-spacing:1.5px; font-weight:600}
.sec-head .count{font-size:11px; color:var(--faint); font-family:var(--font-mono)}
.grid{display:grid; grid-template-columns:1fr 1fr; gap:1px; background:var(--line);
  border-radius:var(--radius-sm); overflow:hidden}
.cell{background:var(--surface); padding:11px 13px}
.cell .l{font-size:11px; color:var(--dim)}
.cell .v{font-size:19px; font-weight:650; font-family:var(--font-mono);
  font-variant-numeric:tabular-nums; margin-top:2px}
.up{color:var(--up)} .down{color:var(--down)} .flat{color:var(--text)}
.px{grid-column:1/-1; text-align:center; padding:16px}
.px .v{font-size:38px; letter-spacing:-1px}
.chart{position:relative}
/* 用瀏覽器原生的縮放把手：右下角可拖曳改變寬高，尺寸記在 localStorage。
   preserveAspectRatio="none" 讓 K 線跟著容器拉伸，跟看盤軟體一樣。 */
.cwrap{overflow:hidden; border-radius:8px}
.cwrap svg{display:block; width:100%; height:auto; cursor:grab; touch-action:none;
  user-select:none}
.legend{display:flex; gap:16px; flex-wrap:wrap; font-size:12.5px; color:var(--dim);
  font-family:var(--font-mono); font-variant-numeric:tabular-nums; margin:2px 0 8px}
.legend b{color:var(--text); font-weight:600}
.legend .lt{color:var(--faint)}
.chint{font-size:11px; color:var(--faint); text-align:right; margin-top:4px}
.chint #cinfo{color:var(--dim)}
.chead{display:flex; align-items:baseline; justify-content:space-between; margin-bottom:10px;
  gap:14px; flex-wrap:wrap}
/* 即時分頁的標頭右邊是兩行的翻頁列，一定要底部對齊：baseline 會把右欄「第一行的基線」
   對到 44px 大字的基線，第二行整條就掛到大字底線以下，整塊白白多吃 22px（實測 45→67）。
   只掛在 #chead 上，不動 .chead 本身 —— 回顧分頁共用同一個 class，它是單行、
   改成 flex-end 會讓日期與 1分/5分 切換往下位移。 */
#chead{align-items:flex-end}
.cpx{font-size:44px; font-weight:700; font-family:var(--font-mono);
  font-variant-numeric:tabular-nums; line-height:1}
.cchg{font-size:17px; font-weight:600; font-family:var(--font-mono)}
.cdate{font-size:12.5px; color:var(--gold); cursor:pointer; border:1px solid var(--line);
  border-radius:20px; padding:3px 11px; background:var(--surface-2)}
.cdate:hover{border-color:var(--gold)}
/* 換日：圖上常駐一條翻頁列，右欄兩行 —— 上排「能按的」（◀ 日期 ▶ 今天／即時），
   下排 11px 灰字「只是說明的」（夜盤範圍、練習結果、鍵盤提示）。
   高度預算：r1 24px ＋ gap 2px ＋ r2 約 16px ＝ 42px，不可超過改版前的 45px；
   動任何一個字級／padding 都要重量一次 getBoundingClientRect().height，不能用字級推算。 */
.pager{display:flex; flex-direction:column; align-items:flex-end; gap:2px; position:relative}
.pager .r1{display:flex; align-items:center; gap:3px}
.pager .r2{display:flex; align-items:center; gap:7px; font-size:11px; color:var(--faint);
  line-height:1.25; padding-right:5px; flex-wrap:wrap; justify-content:flex-end}
.pager .r2 .sep{color:#333A47}
.pager .r2 b{font-family:var(--font-mono); font-variant-numeric:tabular-nums;
  font-size:11.5px; font-weight:650}
.pager .r2 .kbdgrp{display:inline-flex; gap:3px; align-items:center}
.pager .r2 kbd{font-family:var(--font-mono); font-size:10.5px; background:var(--surface-2);
  border:1px solid var(--line); border-radius:5px; padding:0 4px; color:var(--dim);
  line-height:1.35}
/* 箭頭去框變成幽靈圖示：有框的方塊視覺重量跟資訊一樣重，會跟日期互相搶。
   24×24 是上限，26×26 會讓整塊超過 45px。變灰用顏色不用 opacity（opacity 連背景一起淡，看起來髒）。 */
.pager .nav-icon{width:24px; height:24px; border-radius:8px; border:0; background:transparent;
  color:var(--faint); font-size:11px; line-height:1; display:grid; place-items:center;
  cursor:pointer; font-family:var(--font-sans); padding:0;
  transition:background .12s ease, color .12s ease}
.pager .nav-icon:hover:not(:disabled){background:var(--surface-2); color:var(--text)}
.pager .nav-icon:active:not(:disabled){background:var(--line)}
.pager .nav-icon:disabled{color:#333A47; cursor:default}
.pager .nav-icon:focus-visible{outline:1px solid var(--gold); outline-offset:1px}
/* 日期本身不再是金色（金色只留給「即時／現在」一個意思），line-height 全部鎖 1，
   否則行高會把 r1 撐過 24px */
.pager .dstamp{display:inline-flex; align-items:center; gap:7px; border:0; background:transparent;
  cursor:pointer; padding:3px 7px; border-radius:9px; color:var(--text); line-height:1;
  font-family:var(--font-mono); font-variant-numeric:tabular-nums; white-space:nowrap;
  transition:background .12s ease}
.pager .dstamp .num{font-size:15px; font-weight:650; letter-spacing:.3px; line-height:1}
/* 星期那一格永遠佔一個字寬（今天是週末/休市時 dayInfo 找不到、星期是空的）——
   翻頁列整條靠右對齊，這一格一縮 ◀ 就往右跑 12px，連點時第 2 下會落到日期鈕上（誤開月曆）。 */
.pager .dstamp .wd{font-family:var(--font-sans); font-size:12px; color:var(--dim);
  font-weight:500; line-height:1; min-width:1em; text-align:center}
.pager .dstamp .cal-i{color:var(--faint); transition:color .12s ease}
.pager .dstamp .caret{font-size:8px; color:var(--faint); transition:transform .15s ease, color .12s}
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
  transition:background .12s ease}
.pager .jump2:hover{background:rgba(227,169,81,.24)}
.pager .livelamp{display:inline-flex; align-items:center; gap:6px; font-size:12px; color:var(--dim);
  padding:4px 6px 4px 4px; line-height:1.15; white-space:nowrap}
.pager .livelamp i{width:6px; height:6px; border-radius:50%; background:var(--gold);
  box-shadow:0 0 0 3px var(--gold-soft)}

/* 月曆改成錨在翻頁列底下的浮層：以前掛在圖下面，展開會把整張 K 線圖往下推、
   而且離入口很遠。#cpick 仍然是獨立節點（不併進 #chead）—— #chead 每秒跟著報價重繪，
   月曆若在裡面會被整個重建，滑鼠停在哪一格都會被打斷。 */
.cheadwrap{position:relative}
.calpop{position:absolute; top:calc(100% + 8px); right:0; z-index:30}
.calpop .calbox{margin-top:0; box-shadow:0 14px 34px rgba(0,0,0,.5)}
.calbox{margin-top:10px; border:1px solid var(--line); border-radius:12px;
  padding:12px 14px 13px; background:var(--surface-2); width:max-content; max-width:100%}
.calhead{display:flex; align-items:center; justify-content:space-between; gap:18px;
  margin-bottom:10px}
.calhead .mo{font-family:var(--font-mono); font-size:12.5px; font-weight:700}
.calhead .cnav{display:flex; gap:5px}
.calhead .cnav button{width:24px; height:24px; border-radius:6px; font-size:12px;
  display:grid; place-items:center; padding:0; cursor:pointer;
  background:var(--surface); color:var(--dim); border:1px solid var(--line)}
.calhead .cnav button:hover:not(:disabled){border-color:var(--gold); color:var(--gold)}
.calhead .cnav button:disabled{opacity:.3; cursor:default}
.cal{display:grid; grid-template-columns:repeat(5,48px); gap:5px}
.cal .wd{font-size:10px; color:var(--faint); text-align:center; letter-spacing:1px}
.cal .cell{position:relative; height:44px; border-radius:9px; border:1px solid transparent;
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
.cal .cell.off .dd{color:#333A47; font-weight:400}
.cal .cell.prac{border-color:rgba(227,169,81,.42)}
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
.mini{display:flex; flex-wrap:wrap; gap:0 20px; font-size:12.5px; color:var(--dim);
  font-family:var(--font-mono); margin-top:12px; padding-top:11px; border-top:1px solid var(--line)}
.mini b{color:var(--text); font-weight:600}
.mini b.dim{color:var(--faint)}      /* 「休市中／收不到」不是正常數值，字色壓下來 */
.btns{display:flex; gap:10px}
.btn{flex:1; padding:15px; border-radius:12px; cursor:pointer;
  font-family:var(--font-sans); font-size:16px; font-weight:700; letter-spacing:1px}
.btn.long{background:var(--up-soft); color:var(--up); border:1px solid rgba(238,90,84,.4)}
.btn.short{background:var(--down-soft); color:var(--down); border:1px solid rgba(52,179,126,.4)}
.btn.flat2{background:var(--surface-2); color:var(--text); border:1px solid var(--line)}
.btn.ghost{flex:0 0 auto; background:transparent; color:var(--faint); font-size:13px;
  font-weight:400; border:1px solid var(--line)}
.btn:active{transform:scale(.99)}
/* 沒有即時報價時的下單按鈕：真的停用，並在底下寫清楚為什麼（不是純視覺的灰） */
.btn:disabled{opacity:.32; cursor:not-allowed}
.btn:disabled:active{transform:none}
.whyoff{margin-top:10px; font-size:11.5px; color:var(--faint); line-height:1.6}
.warn{font-size:10.5px; color:var(--gold); background:var(--gold-soft);
  border-radius:20px; padding:2px 9px; font-weight:600}
.pnl{text-align:center; padding:6px 0 10px}
.pnl .v{font-size:44px; font-weight:700; font-family:var(--font-mono);
  font-variant-numeric:tabular-nums; line-height:1}
.pnl .l{font-size:12px; color:var(--dim); margin-top:6px}
.plimit{display:flex; justify-content:center; gap:14px; font-size:11.5px; color:var(--faint);
  font-family:var(--font-mono); margin-bottom:12px}
.seg{display:flex; gap:4px; background:var(--surface-2); border-radius:10px; padding:3px; margin-bottom:16px}
.seg button{flex:1; border:0; background:transparent; color:var(--dim); cursor:pointer;
  font-family:var(--font-sans); font-size:12.5px; font-weight:600; padding:8px 4px; border-radius:8px}
.seg button.on{background:var(--gold-soft); color:var(--gold)}
.rate-row{display:flex; align-items:flex-end; gap:16px}
.rate-big{line-height:1}
.rate-big .num{font-family:var(--font-mono); font-size:46px; font-weight:680; letter-spacing:-1px;
  color:var(--gold); font-variant-numeric:tabular-nums}
.rate-big .pct{font-size:22px; color:var(--gold); font-family:var(--font-mono); margin-left:2px}
.rate-big .lab{font-size:12px; color:var(--dim); letter-spacing:2px; margin-top:6px}
.rate-meta{flex:1; display:flex; flex-direction:column; gap:8px; align-items:flex-end; padding-bottom:4px}
.wl{font-family:var(--font-mono); font-size:14px; font-variant-numeric:tabular-nums}
.wl .w{color:var(--up)} .wl .l{color:var(--down)}
.net{font-family:var(--font-mono); font-variant-numeric:tabular-nums}
.net .v{font-size:19px; font-weight:700}
.net .u{font-size:11px; color:var(--dim)}
.cash{font-size:11px; color:var(--faint); font-family:var(--font-mono)}
/* 交易列表自己捲動，整個儀表板才能一眼看完、不用捲整頁 */
.list{display:flex; flex-direction:column; gap:8px; margin-top:14px;
  max-height:290px; overflow-y:auto; padding-right:4px}
.list::-webkit-scrollbar{width:6px}
.list::-webkit-scrollbar-thumb{background:var(--line); border-radius:3px}
.trade{background:var(--surface-2); border:1px solid var(--line); border-radius:var(--radius-sm);
  padding:11px 13px}
.tr-top{display:flex; align-items:center; gap:9px}
.tr-date{font-family:var(--font-mono); font-size:12.5px; color:var(--dim); width:44px; flex:none}
.dir{font-size:11px; font-weight:700; padding:2px 7px; border-radius:6px; flex:none}
.dir.l{background:var(--up-soft); color:var(--up)}
.dir.s{background:var(--down-soft); color:var(--down)}
.tr-px{flex:1; font-family:var(--font-mono); font-size:12.5px; font-variant-numeric:tabular-nums}
.tr-px .arrow{color:var(--faint); margin:0 3px}
.tr-res{font-family:var(--font-mono); font-size:15px; font-weight:700; text-align:right;
  min-width:52px; flex:none; font-variant-numeric:tabular-nums}
.r-win{color:var(--up)} .r-loss{color:var(--down)}
.tag{font-size:10px; color:var(--faint); border:1px solid var(--line); border-radius:5px; padding:1px 5px}
.alert{background:var(--up-soft); border:1px solid var(--up); border-radius:var(--radius-sm);
  padding:12px 14px; font-size:12.5px; margin-bottom:12px; line-height:1.6}
.note{background:var(--surface); border:1px solid var(--line); border-radius:var(--radius-sm);
  padding:13px 15px; font-size:12px; color:var(--dim); margin-bottom:12px; line-height:1.65}
.note b{color:var(--text)}
.wait{text-align:center; padding:48px 20px; color:var(--dim)}
.foot{font-size:11px; color:var(--faint); text-align:center; margin-top:20px; line-height:1.7}
.dot{display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--down);margin-right:5px}
.dot.stale{background:var(--gold)} .dot.dead{background:var(--up)}
/* 休市中不是故障 —— 燈號用中性灰，不要每個週末都亮紅燈 */
.dot.off{background:var(--faint)}
.dl{display:inline-block; margin-top:12px; font-size:12px; color:var(--gold); text-decoration:none}

/* ================= 【回顧】分頁（沿用上面的顏色變數，不另立一套） ================= */
[hidden]{display:none !important}
.tabs{display:flex; gap:4px; background:var(--surface-2); border-radius:11px; padding:3px}
.tabs button{border:0; background:transparent; color:var(--dim); cursor:pointer; min-width:104px;
  font-family:var(--font-sans); font-size:13.5px; font-weight:650; padding:9px 18px; border-radius:9px}
.tabs button.on{background:var(--gold-soft); color:var(--gold)}
#tab-review .card{padding:16px 18px; margin-bottom:10px}
#tab-review .card.chart{padding:14px 16px 12px}
#tab-review .chead{gap:14px}
#tab-review .cpx{font-size:34px} #tab-review .cchg{font-size:15px}
#tab-review .cwrap svg{cursor:crosshair}
.cday{font-size:13px; color:var(--dim); font-family:var(--font-mono)}
.ctag{font-size:11px; color:var(--faint); border:1px solid var(--line); border-radius:6px;
  padding:2px 8px; margin-left:6px}
.tfsw{display:flex; gap:3px; background:var(--surface-2); border-radius:9px; padding:3px}
.tfsw button{border:0; background:transparent; color:var(--dim); cursor:pointer;
  font-family:var(--font-sans); font-size:11.5px; font-weight:600; padding:5px 12px; border-radius:7px}
.tfsw button.on{background:var(--gold-soft); color:var(--gold)}
/* 重播控制列：放在圖的正下方，眼睛不用離開圖 */
.rpbar{display:flex; align-items:center; gap:8px; margin-top:9px; padding:9px 10px;
  background:var(--surface-2); border:1px solid var(--line); border-radius:12px}
.rpbtn{border:1px solid var(--line); background:var(--surface); color:var(--text); cursor:pointer;
  font-family:var(--font-sans); font-size:13px; font-weight:600; padding:8px 13px; border-radius:9px;
  min-width:42px}
.rpbtn:hover{border-color:var(--gold)}
.rpbtn.play{background:var(--gold-soft); color:var(--gold); border-color:transparent; min-width:96px}
.rpbtn:disabled{opacity:.35; cursor:default}
.rpsp{display:flex; gap:2px; background:var(--surface); border-radius:9px; padding:3px;
  border:1px solid var(--line)}
.rpsp button{border:0; background:transparent; color:var(--faint); cursor:pointer;
  font-family:var(--font-mono); font-size:11.5px; font-weight:600; padding:5px 9px; border-radius:6px}
.rpsp button.on{background:var(--gold-soft); color:var(--gold)}
.rppos{flex:1; font-family:var(--font-mono); font-size:12.5px; color:var(--dim);
  font-variant-numeric:tabular-nums; text-align:right}
.rppos b{color:var(--text)}
.kbd{font-family:var(--font-mono); font-size:10.5px; color:var(--faint);
  border:1px solid var(--line); border-radius:5px; padding:1px 5px; background:var(--surface)}
.fstrip{display:grid; grid-template-columns:repeat(7,1fr); gap:1px; background:var(--line);
  border-radius:var(--radius-sm); overflow:hidden; margin-top:10px}
.fcell{background:var(--surface); padding:9px 11px}
.fcell .l{font-size:10.5px; color:var(--dim); white-space:nowrap}
.fcell .v{font-size:17px; font-weight:650; font-family:var(--font-mono);
  font-variant-numeric:tabular-nums; margin-top:1px}
.ftitle{font-size:11px; color:var(--faint); margin:12px 4px 0}
#tab-review .seg{margin-bottom:0}
.chips{display:flex; gap:6px; flex-wrap:wrap; margin:12px 0 4px}
.chips button{font-size:11.5px; padding:5px 11px; border-radius:20px; cursor:pointer;
  background:var(--surface-2); color:var(--dim); border:1px solid var(--line);
  font-family:var(--font-sans)}
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
.trade.sel{border-color:var(--gold); background:var(--gold-soft)}
.tr-note{font-size:11.5px; color:var(--faint); margin-top:5px; padding-left:49px;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
.dt{display:flex; flex-direction:column; gap:9px}
.dt-row{display:flex; justify-content:space-between; align-items:baseline; font-size:12.5px}
.dt-row .k{color:var(--dim)}
.dt-row .v{font-family:var(--font-mono); font-variant-numeric:tabular-nums; font-size:13.5px}
.dt-big{text-align:center; padding:2px 0 8px}
.dt-big .v{font-size:40px; font-weight:700; font-family:var(--font-mono); line-height:1;
  font-variant-numeric:tabular-nums}
.dt-big .l{font-size:12px; color:var(--dim); margin-top:5px}
.hr{height:1px; background:var(--line); margin:2px 0}
.noteline{font-size:12.5px; color:var(--text); background:var(--surface-2);
  border:1px solid var(--line); border-radius:9px; padding:9px 11px; line-height:1.6}
.noteline.empty{color:var(--faint)}
.noteline[data-nedit]{cursor:pointer}
.noteline[data-nedit]:hover{border-color:var(--gold); color:var(--gold)}
.trade .noteline{margin-top:8px; font-size:12px; padding:8px 10px}
.nedit{margin-top:8px}
.nedit textarea{width:100%; box-sizing:border-box; min-height:78px; resize:vertical;
  background:var(--surface-2); border:1px solid var(--gold); border-radius:9px;
  color:var(--text); font-family:var(--font-sans); font-size:12.5px; line-height:1.6;
  padding:9px 11px}
.nedit textarea::placeholder{color:var(--faint)}
.nedit textarea:focus{outline:none}
.nedit .nbtn{display:flex; gap:8px; margin-top:7px}
.nedit .nbtn .btn{flex:1; padding:7px 0; font-size:12.5px}
.empty{text-align:center; padding:36px 16px; color:var(--faint); font-size:12.5px; line-height:1.8}
.btn.gold{background:var(--gold-soft); color:var(--gold); border:1px solid transparent}
.btn.gw{flex:1}                     /* 回顧頁的次要按鈕要跟主按鈕一樣寬 */
.daysel{display:flex; gap:6px; flex-wrap:wrap; margin-top:10px}
.daysel button{font-size:12px; padding:7px 10px; border-radius:8px; cursor:pointer;
  background:var(--surface-2); color:var(--dim); border:1px solid var(--line);
  font-family:var(--font-mono)}
.daysel button.on{background:var(--gold-soft); color:var(--gold); border-color:transparent}
.daysel button .m{font-size:9.5px; color:var(--faint); margin-left:4px}
.jinput{width:100%; background:var(--surface-2); border:1px solid var(--line); border-radius:9px;
  color:var(--text); font-family:var(--font-sans); font-size:13px; padding:10px 11px; margin-top:9px}
.jinput::placeholder{color:var(--faint)}
.jinput:focus{outline:none; border-color:var(--gold)}
.hold{background:var(--surface-2); border:1px solid var(--line); border-radius:var(--radius-sm);
  padding:12px 14px}
.hold .v{font-size:34px; font-weight:700; font-family:var(--font-mono); line-height:1;
  font-variant-numeric:tabular-nums; text-align:center}
.hold .l{font-size:12px; color:var(--dim); text-align:center; margin-top:5px}
.cmp{display:flex; flex-direction:column; gap:8px}
.cmp .side{background:var(--surface-2); border:1px solid var(--line); border-radius:10px;
  padding:10px 12px}
.cmp .side.mine{border-color:rgba(227,169,81,.45)}
.cmp .side .h{font-size:11px; color:var(--dim); margin-bottom:4px; letter-spacing:.5px}
.cmp .side .b{display:flex; align-items:center; gap:8px; font-family:var(--font-mono); font-size:13px}
.cmp .side .b .res{margin-left:auto; font-size:16px; font-weight:700}
.verdict{border-radius:10px; padding:10px 12px; font-size:12.5px; line-height:1.6; text-align:center;
  font-weight:600}
.verdict.same{background:var(--gold-soft); color:var(--gold)}
.verdict.diff{background:var(--surface-2); color:var(--dim); border:1px solid var(--line)}
.tally{display:flex; gap:14px; justify-content:center; font-family:var(--font-mono); font-size:12px;
  color:var(--faint); padding-top:4px; flex-wrap:wrap}
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
  <div id="wait" class="wait">等待資料…</div>
  <div id="warn"></div>
  <div class="cols"><div id="mkt"></div><div class="right"><div id="trade"></div><div id="stats"></div></div></div>
</div>

<!-- 【回顧】：容器只建這一次，之後只換裡面的內容（重繪不打斷縮放／拖曳、也不閃） -->
<div id="tab-review" hidden>
 <div class="cols">
  <div>
   <div class="card chart">
    <div class="chead" id="rhead"></div>
    <div class="legend" id="rlegend"></div>
    <div class="cwrap"><svg id="rsvg" preserveAspectRatio="none"></svg></div>
    <div id="rctrl"></div>
   </div>
   <div class="ftitle" id="rftitle">進場當下的客觀盤面</div>
   <div class="fstrip" id="rfstrip"></div>
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

var lastMkt='', lastTrade='', lastStats='', lastWarn='', statsCache=null, statsAt=0;

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
 const c=s.chips||{};
 const warn = QMSG[q] || (dead ? '<div class="alert"><b>報價已中斷</b>　畫面上的數字是舊的（'+
   (s.age_sec==null?'尚未收到':age+' 秒前')+'）。程式每分鐘會自動重連。</div>' : '');
 setHTML('warn',warn);
 const W=document.getElementById('wait');
 const drew=paintChart(s);
 if(drew){
   W.hidden=true;
 } else if(q==='live'){
   // 有報價但還沒有 K 棒（例如剛連上的夜盤）→ 退回簡單的數字卡
   W.hidden=true;
   lastMktFallback = '<div class="card"><div class="grid">'+
     '<div class="cell px"><div class="l">成交價</div><div class="v flat">'+f(c.price)+'</div></div>'+
     cell('最近 5 分鐘',pm(c.mom5)+' 點',sgn(c.mom5))+
     cell('最近 15 分鐘',pm(c.mom15)+' 點',sgn(c.mom15))+'</div></div>';
   if(document.getElementById('mkt').innerHTML!==lastMktFallback)
     document.getElementById('mkt').innerHTML=lastMktFallback;
 } else {
   // 既沒有報價、也一根 K 棒都拿不到（本機沒資料又沒連上永豐）才是真的沒東西可看。
   // 舊圖要收掉，否則等待訊息底下還掛著一張沒人知道是哪天的圖。
   // ⚠️ 不能用 setHTML('mkt','') —— K 線圖是 paintChart 自己塞進 #mkt 的，
   //    lastMkt 從頭到尾都是 ''，setHTML 會判定「跟上次一樣」而整個略過。
   W.hidden=false; W.textContent=s.msg||'等待中…';
   const M=document.getElementById('mkt');
   if(M.innerHTML){ M.innerHTML=''; lastMkt=''; lastMktFallback=''; }
 }
 if(nf||!nEditing('t')) setHTML('trade',tradeBox(s));
 if(nf||!nEditing('s')) setHTML('stats',statsBox(statsCache));
}

// 只有內容真的變了才動 DOM。否則每 0.5 秒重建一次，
// 使用者剛好在那一瞬間按下去，按鈕會連同事件一起被換掉 → 第一下沒反應。
function setHTML(id,html){
 const box={mkt:'lastMkt',trade:'lastTrade',stats:'lastStats',warn:'lastWarn'}[id];
 if(window[box]===html) return;
 window[box]=html;
 document.getElementById(id).innerHTML=html;
}


// ---------------- K 線圖（純 SVG，TradingView 式操作） ----------------
// 滾輪＝縮放疏密（以游標位置為中心）、按住拖曳＝左右移動時間、雙擊＝還原。
// 價格軸自動貼合「畫面上看得到的那幾根」，跟看盤軟體一樣。
var barsCache=null, barsAt=0, viewDate='', pickOpen=false, calMonth='', lastMktFallback='';
var barsPending=false;        // 換日的資料還在路上（見 fetchBars）
var barsSeq=0;                // 換日請求的流水號，只採用最後一次的回應（見 fetchBars）
// n＝看得到幾根；end＝最右邊那根的索引（null＝跟著最新）
// vz＝價格軸縮放倍率（>1 放大、<1 壓縮）；voff＝價格軸平移量（單位：點）
var VIEW={n:60, end:null, vz:1, voff:0};
var lastSpan=0;   // 目前畫面的價格跨度（直向平移換算用）
var HOVER={i:null};   // 游標對到的 K 棒（全域索引），null＝顯示最新那根
var DRAG=null;

function fetchBars(force){
 if(!force && Date.now()-barsAt<3000) return;
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

function chartSVG(s){
 const G=chartGeom(); if(!G) return null;
 const B=G.all.slice(G.from,G.to), T=(barsCache.trades)||[], P=s.position;
 const live=!viewDate;
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
 g+='<rect x="'+(W-R)+'" y="0" width="'+R+'" height="'+H+'" fill="#1F2530" opacity=".45"/>';
 for(let k=0;k<=5;k++){
   const v=lo+(hi-lo)*k/5, yy=y(v);
   g+='<line x1="0" y1="'+yy.toFixed(1)+'" x2="'+(W-R)+'" y2="'+yy.toFixed(1)+
      '" stroke="#262D39" stroke-width="1"/>'+
      '<text x="'+(W-R+8)+'" y="'+(yy+4).toFixed(1)+'" fill="#5A616E" font-size="12" '+
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
      '" stroke="#4A5468" stroke-width="1" stroke-dasharray="2 4"/>'+
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
    '" y2="'+(H-BOT-VOLH-GAP/2).toFixed(1)+'" stroke="#262D39" stroke-width="1"/>';
 B.forEach((b,i)=>{
   const col=b.c>=b.o?'#EE5A54':'#34B37E', X=x(i), yy=vy(b.v);
   g+='<rect x="'+(X-bw/2).toFixed(1)+'" y="'+yy.toFixed(1)+'" width="'+bw.toFixed(1)+
      '" height="'+Math.max(0.8,H-BOT-yy).toFixed(1)+'" fill="'+col+'" opacity=".55"/>';
 });
 g+='<text x="'+(W-R+8)+'" y="'+(H-BOT-VOLH+10)+'" fill="#5A616E" font-size="11" '+
    'font-family="ui-monospace,monospace">'+(vmax>=10000?(vmax/1000).toFixed(0)+'k':vmax.toFixed(0))+'</text>'+
    '<text x="'+(W-R+8)+'" y="'+(H-BOT-2)+'" fill="#5A616E" font-size="11">量</text>';

 if(P&&live&&G.live){
   [[P.tp,'#EE5A54','停利'],[P.sl,'#34B37E','停損']].forEach(z=>{
     const yy=y(z[0]); if(yy<TOP||yy>PB) return;
     g+='<line x1="0" y1="'+yy.toFixed(1)+'" x2="'+(W-R)+'" y2="'+yy.toFixed(1)+
        '" stroke="'+z[1]+'" stroke-width="1.2" stroke-dasharray="5 4" opacity=".8"/>'+
        '<text x="6" y="'+(yy-5).toFixed(1)+'" fill="'+z[1]+'" font-size="11.5">'+z[2]+' '+z[0].toFixed(0)+'</text>';
   });
 }
 // 進出場標記：標籤絕不壓在 K 棒上。
 // 價位改掛在右側價格軸（本來就是空的灰帶），時間與損益用小字貼在標記旁邊。
 // 上一版把「進 09:05 46020」做成左緣大色塊，字是看清楚了，但整個擋住早盤那幾根
 // （Benson 2026-08-17 回報「時間標示有點擋路」）。
 const AXB=[], CAPB=[];
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
     '" rx="3" fill="'+col+'"/>'+
     '<text x="'+(W-R+7)+'" y="'+(py+h-5).toFixed(1)+'" fill="#0F1218" font-size="11.5"'+
     ' font-weight="700" font-family="ui-monospace,monospace">'+txt+'</text>';
 };
 // 標記旁的小字（時間／損益）：無底色、只描深色外框，佔的面積不到原本的五分之一
 const caption=(X,baseY,sign,txt,col)=>{
   const fs=11, w=tw(txt,fs), h=fs+3;
   let by=baseY;
   for(let k=0;k<5;k++){
     by=Math.max(TOP+fs,Math.min(H-BOT-2,baseY+sign*k*(h+3)));
     const bx=X-w/2;
     if(!CAPB.some(b=>bx<b.x+b.w&&b.x<bx+w&&by-fs<b.y+b.h&&b.y<by-fs+h)) break;
   }
   CAPB.push({x:X-w/2,y:by-fs,w:w,h:h});
   return '<text x="'+X.toFixed(1)+'" y="'+by.toFixed(1)+'" text-anchor="middle" fill="'+col+
     '" font-size="'+fs+'" font-weight="700" font-family="ui-monospace,monospace"'+
     ' stroke="#0F1218" stroke-width="3.2" paint-order="stroke" stroke-linejoin="round">'+txt+'</text>';
 };
 T.forEach(t=>{
   const ia=idxAll(t.time); if(ia<G.from||ia>=G.to) return;
   const i=ia-G.from, X=x(i), Y=y(t.entry), long=t.dir==='long', col=long?'#EE5A54':'#34B37E';
   g+='<line x1="0" y1="'+Y.toFixed(1)+'" x2="'+(W-R)+'" y2="'+Y.toFixed(1)+
      '" stroke="'+col+'" stroke-width="1" stroke-dasharray="5 4" opacity=".45"/>';
   const tipY=long?Y+7:Y-7, endY=long?Y+25:Y-25;
   g+='<path d="M'+(X-9)+' '+endY+' L'+X+' '+tipY+' L'+(X+9)+' '+endY+
      ' Z" fill="'+col+'" stroke="#0F1218" stroke-width="1.6" stroke-linejoin="round"/>';
   g+=caption(X,long?endY+13:endY-5,long?1:-1,'\u9032 '+t.time,col);
   g+=axisChip(Y,String(Math.round(t.entry)),col);
   const je=t._exit_time?idxAll(t._exit_time.slice(0,5)):-1;
   if(je>=G.from&&je<G.to){
     const XE=x(je-G.from), YE=y(t.exit), ec=t._net>0?'#EE5A54':'#34B37E';
     g+='<line x1="0" y1="'+YE.toFixed(1)+'" x2="'+(W-R)+'" y2="'+YE.toFixed(1)+
        '" stroke="'+ec+'" stroke-width="1" stroke-dasharray="5 4" opacity=".45"/>'+
        '<circle cx="'+XE.toFixed(1)+'" cy="'+YE.toFixed(1)+'" r="8.5" fill="'+ec+
        '" stroke="#0F1218" stroke-width="1.6"/>'+
        '<path d="M'+(XE-3.6)+' '+(YE-3.6)+' L'+(XE+3.6)+' '+(YE+3.6)+' M'+(XE-3.6)+' '+(YE+3.6)+
        ' L'+(XE+3.6)+' '+(YE-3.6)+'" stroke="#0F1218" stroke-width="2.2" stroke-linecap="round"/>';
     const down=YE<PB-40;
     g+=caption(XE,down?YE+22:YE-14,down?1:-1,'\u51fa '+t._exit_time.slice(0,5)+
        (t._net==null?'':'  '+pm(t._net)),ec);
     g+=axisChip(YE,String(Math.round(t.exit)),ec);
   }
 });
 // ---- 游標所在那根：畫垂直參考線 ----
 let legendBar=B[B.length-1], legendIdx=G.to-1, hovering=false;
 if(HOVER.i!=null && HOVER.i>=G.from && HOVER.i<G.to){
   legendIdx=HOVER.i; legendBar=G.all[HOVER.i]; hovering=true;
   const X=x(HOVER.i-G.from);
   g+='<line x1="'+X.toFixed(1)+'" y1="'+TOP+'" x2="'+X.toFixed(1)+'" y2="'+(H-BOT)+
      '" stroke="#8B92A0" stroke-width="1" stroke-dasharray="3 3" opacity=".6"/>';
 }

 // 時間刻度：依疏密自動決定間隔
 const step=Math.max(1,Math.ceil(B.length/8));
 B.forEach((b,i)=>{ if(i%step) return;
   g+='<text x="'+x(i).toFixed(1)+'" y="'+(H-7)+'" fill="#5A616E" font-size="11.5" '+
      'text-anchor="middle" font-family="ui-monospace,monospace">'+b.t+'</text>';
 });

 const c=s.chips||{};
 // 有什麼就顯示什麼 —— 原本整排綁在「日盤才有」的欄位上，
 // 休市時整排消失，連加權都看不到（Benson 回報找不到）。
 let mini='';
 if(live && q!=='live'){
   // 沒有即時報價時，上面那個大字是「最後一根 K 棒的收盤」——
   // 一定要講清楚它不是即時價，否則看起來跟開盤時一模一樣。
   mini+='<span>報價 <b class="dim">'+(q==='closed'?'休市中':'收不到')+
         '</b>（上面是收盤價，非即時）</span>';
 }
 if(live && q==='live'){
   if(c.mom5!=null) mini+='<span>5分 <b class="'+sgn(c.mom5)+'">'+pm(c.mom5)+'</b></span>';
   if(c.mom15!=null) mini+='<span>15分 <b class="'+sgn(c.mom15)+'">'+pm(c.mom15)+'</b></span>';
   if(c.chg!=null) mini+='<span>跳空 <b>'+pm(c.gap)+'</b></span>'+
     '<span>震幅 <b>'+f(c.rng)+'</b></span>'+
     '<span>位階 <b>'+f(c.pos*100)+'%</b></span>'+
     '<span>量能 <b>'+f(c.vol_ratio,2)+'倍</b></span>';
   if(c.bid!=null) mini+='<span>買/賣 <b>'+f(c.bid)+' / '+f(c.ask)+'</b></span>';
   // 加權指數與基差：現貨 09:00 才開盤、13:30 收，空窗期明講「未開盤」而不是消失
   mini+='<span>加權 <b>'+(c.idx==null?'<i class="dim">未開盤</i>':f(c.idx))+'</b></span>';
   if(c.basis!=null) mini+='<span>基差 <b class="'+sgn(c.basis)+'">'+pm(c.basis)+'</b></span>';
 }
 const pick=pickOpen?calHTML():'';
 const cur=curDay(), me=dayInfo(cur);
 // 換日之後、新的 K 棒還沒回來的那一秒，barsCache 還是上一天的 ——
 // 這時候標籤若照常顯示，會把上一天的練習筆數掛在新日期底下（看起來像那天有下單）。
 const loading=barsPending||cur!==(barsCache.date||'');
 const dayS=cur.slice(5), noS=((barsCache.night_open)||'').slice(5);
 // 下排：純說明。即時那天也照樣寫日期（金點已經在講「現在」了，再寫「今天」是重複）；
 // 沒練習寫「未練習」而不是整段消失 —— 消失會讓上下兩行的位置跳動。
 const net=T.reduce((a,t)=>a+t._net,0);
 const r2=loading
   ? '<span>載入中…</span>'
   : ((noS&&noS!==dayS)?'<span>含 '+noS+' 夜盤 15:00 起</span><span class="sep">·</span>':'')+
     '<span>'+(T.length
       // 負號用 U+2212 不用 hyphen，等寬字型下跟 + 對得齊（只改這裡，不動全域的 pm()）
       ? '練習 '+T.length+' 筆 <b class="'+sgn(net)+'">'+pm(net).replace('-','−')+'</b> 點'
       : '未練習')+'</span>'+
     // 這個分隔點跟著鍵盤提示一起藏（窄視窗會把提示收掉，只留一個孤零零的「·」很醜）
     '<span class="sep k">·</span>'+
     '<span class="kbdgrp"><kbd>←</kbd><kbd>→</kbd> 換日</span>';
 const nav=stepTarget(-1), fwd=stepTarget(1);
 return {
   svg:g, vb:'0 0 '+W+' '+H,
   head:'<div><span class="cpx '+sgn(chg)+'">'+f(px)+'</span>'+
        ' <span class="cchg '+sgn(chg)+'">'+pm(chg)+' ('+pm(pct,2)+'%)</span></div>'+
        '<div class="pager">'+
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
        '</div>',
   pick:pick, mini:mini,
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

/* 外框只建一次，之後只換 svg 內容 —— 重繪不會打斷你的縮放與拖曳 */
function paintChart(s){
 const d=chartSVG(s); if(!d) return false;
 if(!document.getElementById('csvg')){
   // #cpick 包在 .cheadwrap 裡：月曆是絕對定位的浮層，要錨在翻頁列正下方，
   // 定位基準必須是「標頭這一塊」而不是整張卡片（卡片是 position:relative）。
   document.getElementById('mkt').innerHTML='<div class="card chart">'+
     '<div class="cheadwrap"><div class="chead" id="chead"></div>'+
     '<div class="calpop" id="cpick"></div></div>'+
     '<div class="legend" id="clegend"></div>'+
     '<div class="cwrap"><svg id="csvg" preserveAspectRatio="none"></svg></div>'+
     '<div class="chint"><span id="cinfo"></span>　滾輪縮放・拖曳平移・雙擊還原</div>'+
     '<div class="mini" id="cmini"></div></div>';
   bindChart();
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
 set('cpick',d.pick); set('cmini',d.mini); set('cinfo',d.info);
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
 return '<div class="trade"><div class="tr-top">'+
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
 let h='<div class="sec-head"><h2>練習成績</h2><span class="count">共 '+ST.total+' 筆</span></div>'+
  '<div class="card">'+seg+
  '<div class="rate-row"><div class="rate-big"><span class="num">'+w.win_rate.toFixed(0)+
  '</span><span class="pct">%</span><div class="lab">勝率</div></div>'+
  '<div class="rate-meta"><div class="wl"><b class="w">'+w.wins+'</b> 勝　<b class="l">'+w.losses+'</b> 敗</div>'+
  '<div class="net"><span class="v '+cls+'">'+pm(w.total)+'</span> <span class="u">點</span></div>'+
  '<div class="cash">'+(w.ntd<0?'-':'+')+'NT$'+Math.abs(w.ntd).toLocaleString()+'</div>'+
  '</div></div>';
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
function rvBlank(msg){
 const sv=document.getElementById('rsvg');
 sv.setAttribute('viewBox','0 0 '+RW+' '+RH);
 sv.innerHTML='<rect x="0" y="0" width="'+RW+'" height="'+RH+'" fill="#171C25"/>'+
   '<text x="'+(RW/2)+'" y="'+(RH/2)+'" fill="#5A616E" font-size="16" text-anchor="middle">'+msg+'</text>';
 rset('rlegend','');
}
function rvDraw(C,D){
 if(!C.day){ rvBlank('還沒有任何練習紀錄'); rset('rhead',''); return; }
 if(!D){ rvBlank('載入中…'); return; }
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
        '" fill="#E7E9ED" opacity=".025"/>';
   }
 }

 /* 價格格線 */
 g+='<rect x="'+(RW-RR)+'" y="0" width="'+RR+'" height="'+RH+'" fill="#1F2530" opacity=".45"/>';
 for(let k=0;k<=5;k++){
   const v=lo+(hi-lo)*k/5, yy=y(v);
   g+='<line x1="0" y1="'+yy.toFixed(1)+'" x2="'+(RW-RR)+'" y2="'+yy.toFixed(1)+
      '" stroke="#262D39" stroke-width="1"/><text x="'+(RW-RR+8)+'" y="'+(yy+4).toFixed(1)+
      '" fill="#5A616E" font-size="12" font-family="ui-monospace,monospace">'+v.toFixed(0)+'</text>';
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
      '<text x="'+(x0+(RW-RR-x0)/2).toFixed(1)+'" y="'+(RTOP+26)+'" fill="#5A616E" font-size="12.5" '+
      'text-anchor="middle">後面還沒揭曉</text>';
 }

 /* 成交量 */
 g+='<line x1="0" y1="'+(RH-RBOT-RVOLH-RGAP/2).toFixed(1)+'" x2="'+(RW-RR)+'" y2="'+
    (RH-RBOT-RVOLH-RGAP/2).toFixed(1)+'" stroke="#262D39" stroke-width="1"/>';
 B.forEach((b,i)=>{ if(G.from+i>G.rev) return;
   const col=b.c>=b.o?'#EE5A54':'#34B37E', X=x(i), yy=vy(b.v);
   g+='<rect x="'+(X-bw/2).toFixed(1)+'" y="'+yy.toFixed(1)+'" width="'+bw.toFixed(1)+
      '" height="'+Math.max(0.8,RH-RBOT-yy).toFixed(1)+'" fill="'+col+'" opacity=".5"/>'; });
 g+='<text x="'+(RW-RR+8)+'" y="'+(RH-RBOT-RVOLH+10)+'" fill="#5A616E" font-size="11" '+
    'font-family="ui-monospace,monospace">'+(vmax>=10000?(vmax/1000).toFixed(0)+'k':vmax.toFixed(0))+'</text>';

 /* 09:30 下單時段結束 */
 { const i=B.findIndex(b=>b.t>='09:30');
   if(i>0){ const X=(i*cw).toFixed(1);
     g+='<line x1="'+X+'" y1="'+RTOP+'" x2="'+X+'" y2="'+(RH-RBOT)+'" stroke="#E3A951" '+
        'stroke-width="1" stroke-dasharray="2 5" opacity=".5"/>'+
        '<text x="'+(+X+5)+'" y="'+(RH-RBOT-6)+'" fill="#5A616E" font-size="10.5">09:30</text>'; } }

 /* ---- 進出場標記 ----------------------------------------------------------
    Benson 明講「進場跟出場的時間標記太小」，所以箭頭／✕ 與時間文字都放大，
    文字加半透明底色不會被 K 棒吃掉。進出場時間很近時（09:02 進、09:05 出）
    文字方塊會自動上下錯開，並拉一條細線指回標記，才不會分不清誰是誰。 */
 const LBOX=[];
 function txtW(s,fs){ let w=0;
   for(let i=0;i<s.length;i++) w+=(s.charCodeAt(i)>255?1.0:0.6)*fs;
   return w; }
 // 價位掛在右側價格軸（本來就是空的灰帶），時間／損益用小字貼在標記旁 ——
 // 兩版之前是「漂在圖中間的大膠囊」，上一版改成左緣大色塊，都還是壓住 K 棒
 // （Benson 2026-08-17：「時間標示有點擋路」）。現在圖區只剩標記本身。
 const AXB=[];
 function axisChip(aY,txt,col,dim){
   const h=17; let py=aY-h/2;
   for(let k=0;k<6;k++){
     py=Math.max(RTOP,Math.min(RPB-h,aY-h/2+(k%2?1:-1)*Math.ceil(k/2)*(h+2)));
     if(!AXB.some(b=>py<b+h&&b<py+h)) break;
   }
   AXB.push(py);
   const o=dim?.4:1;
   return '<rect x="'+(RW-RR+1)+'" y="'+py.toFixed(1)+'" width="'+(RR-2)+'" height="'+h+
     '" rx="3" fill="'+col+'" opacity="'+o+'"/>'+
     '<text x="'+(RW-RR+7)+'" y="'+(py+h-5).toFixed(1)+'" fill="#0F1218" font-size="11.5"'+
     ' font-weight="700" font-family="ui-monospace,monospace" opacity="'+o+'">'+txt+'</text>';
 }
 function caption(X,baseY,sign,txt,col,dim){
   const fs=11, w=txtW(txt,fs), h=fs+3;
   let by=baseY;
   for(let k=0;k<5;k++){
     by=Math.max(RTOP+fs,Math.min(RH-RBOT-2,baseY+sign*k*(h+3)));
     const bx=X-w/2;
     if(!LBOX.some(b=>bx<b.x+b.w&&b.x<bx+w&&by-fs<b.y+b.h&&b.y<by-fs+h)) break;
   }
   LBOX.push({x:X-w/2,y:by-fs,w:w,h:h});
   return '<text x="'+X.toFixed(1)+'" y="'+by.toFixed(1)+'" text-anchor="middle" fill="'+col+
     '" font-size="'+fs+'" font-weight="700" font-family="ui-monospace,monospace"'+
     ' stroke="#0F1218" stroke-width="3.2" paint-order="stroke" stroke-linejoin="round"'+
     (dim?' opacity=".4"':'')+'>'+txt+'</text>';
 }
 function mark(price,t,dir,kind,net,dim){
   const ia=idxAt(all,t), i=gi(ia);
   if(ia<0||i<0||i>=B.length) return '';
   const X=x(i), Y=y(price), long=dir==='long';
   const col=kind==='in'?(long?'#EE5A54':'#34B37E'):(net>0?'#EE5A54':'#34B37E');
   const op=dim?' opacity=".4"':'';
   // 價格線貫穿全圖，左緣掛小標籤
   let s='<line x1="0" y1="'+Y.toFixed(1)+'" x2="'+(RW-RR).toFixed(1)+'" y2="'+Y.toFixed(1)+
     '" stroke="'+col+'" stroke-width="1" stroke-dasharray="5 4" opacity="'+(dim?.22:.5)+'"/>';
   if(kind==='in'){
     // 三角形貼在 K 棒外側、尖端指向進場價；描深色邊框才不會被 K 棒吃掉
     const tipY=long?Y+7:Y-7, endY=long?Y+25:Y-25;
     s+='<path d="M'+(X-9)+' '+endY+' L'+X+' '+tipY+' L'+(X+9)+' '+endY+
        ' Z" fill="'+col+'" stroke="#0F1218" stroke-width="1.6" stroke-linejoin="round"'+op+'/>';
   } else {
     // 出場用實心圓底＋白叉，在任何 K 棒上都看得見
     const r=8.5;
     s+='<circle cx="'+X.toFixed(1)+'" cy="'+Y.toFixed(1)+'" r="'+r+'" fill="'+col+
        '" stroke="#0F1218" stroke-width="1.6"'+op+'/>'+
        '<path d="M'+(X-3.6)+' '+(Y-3.6)+' L'+(X+3.6)+' '+(Y+3.6)+' M'+(X-3.6)+' '+(Y+3.6)+
        ' L'+(X+3.6)+' '+(Y-3.6)+'" stroke="#0F1218" stroke-width="2.2" stroke-linecap="round"'+op+'/>';
   }
   const capY=kind==='in'?(long?Y+38:Y-30):(Y<RPB-40?Y+22:Y-14);
   const capS=kind==='in'?(long?1:-1):(Y<RPB-40?1:-1);
   s+=caption(X,capY,capS,(kind==='in'?'進 ':'出 ')+t+
     (kind==='out'&&net!=null?'  '+pm(net):''),col,dim);
   s+=axisChip(Y,String(Math.round(price)),col,dim);
   return s;
 }
 let mk='';
 if(MODE==='review'){
   // 一天多筆時全部畫出來：選中那筆實心，其餘半透明
   dayTrades(C.day).forEach(x=>{
     const t=x.t, dim=!(T&&x.i===sel.i);
     mk+=mark(t.entry,t.time,t.dir,'in',null,dim);
     if(t._exit_time) mk+=mark(t.exit,String(t._exit_time).slice(0,5),t.dir,'out',t._net,dim);
   });
 } else if(RP.judge){
   mk+=mark(RP.judge.entry,RP.judge.time,RP.judge.dir,'in');
   if(RP.result) mk+=mark(RP.result.exit,RP.result.time,RP.judge.dir,'out',RP.result.net);
   if(RP.state==='revealed'){
     const rt=dayTrade(RP.date);            // 揭曉後把當天實際那筆疊上去對照
     if(rt){ const i=gi(idxAt(all,rt.time));
       if(i>=0&&i<B.length){ const X=x(i),Y=y(rt.entry);
         mk+='<circle cx="'+X.toFixed(1)+'" cy="'+Y.toFixed(1)+'" r="11" fill="none" stroke="#E3A951" '+
             'stroke-width="2.2" stroke-dasharray="4 3"/>'+
             label(X,Y,'當天你的進場 '+rt.time,'#E3A951','mid'); } }
   }
 }

 /* 游標十字線（重播時不能指到未揭曉的地方） */
 let lb=vis[vis.length-1], hovering=false;
 if(RHOVER.i!=null&&RHOVER.i>=G.from&&RHOVER.i<G.to&&RHOVER.i<=G.rev){
   lb=all[RHOVER.i]; hovering=true;
   const X=x(gi(RHOVER.i));
   g+='<line x1="'+X.toFixed(1)+'" y1="'+RTOP+'" x2="'+X.toFixed(1)+'" y2="'+(RH-RBOT)+
      '" stroke="#8B92A0" stroke-width="1" stroke-dasharray="3 3" opacity=".6"/>';
 }
 /* 時間刻度 */
 const step=Math.max(1,Math.ceil(B.length/9));
 B.forEach((b,i)=>{ if(i%step) return;
   g+='<text x="'+x(i).toFixed(1)+'" y="'+(RH-6)+'" fill="#5A616E" font-size="11" '+
      'text-anchor="middle" font-family="ui-monospace,monospace">'+b.t+'</text>'; });
 g+=mk;                                   // 標記畫最後 → 壓在 K 棒上面，一眼看得到

 /* 容器只建一次，這裡只換 svg 內容 */
 const sv=document.getElementById('rsvg');
 if(sv.getAttribute('viewBox')!=='0 0 '+RW+' '+RH) sv.setAttribute('viewBox','0 0 '+RW+' '+RH);
 if(sv.innerHTML!==g) sv.innerHTML=g;

 /* 標題列與 OHLCV 圖例 */
 const op0=all[0].o, px=lb.c, chg=px-op0, pct=chg/op0*100;
 const wd=['日','一','二','三','四','五','六'][new Date(C.day+'T00:00:00').getDay()];
 const dts=dayTrades(C.day);
 rset('rhead','<div><span class="cpx '+sgn(chg)+'">'+f(px)+'</span> <span class="cchg '+sgn(chg)+'">'+
   pm(chg)+' ('+pm(pct,2)+'%)</span></div>'+
   '<div style="flex:1"><span class="cday">'+C.day+'（'+wd+'）</span>'+
   (dts.length?'<span class="ctag">當天有 '+dts.length+' 筆紀錄</span>':'<span class="ctag">當天沒下單</span>')+
   (MODE==='replay'?'<span class="ctag" style="color:var(--gold);border-color:rgba(227,169,81,.4)">重播中</span>':'')+
   '</div>'+
   (MODE==='review'
     ?'<div class="tfsw"><button data-tf="1" class="'+(TF===1?'on':'')+'">1 分</button>'+
      '<button data-tf="5" class="'+(TF===5?'on':'')+'">5 分</button></div>'
     :'<div class="cday" style="color:var(--faint)">1 分 K</div>'));
 const vol=lb.v>=10000?(lb.v/1000).toFixed(1)+'k':lb.v.toFixed(0);
 rset('rlegend','<span class="lt">'+lb.t+'</span><span>開 <b>'+f(lb.o)+'</b></span>'+
   '<span>高 <b>'+f(lb.h)+'</b></span><span>低 <b>'+f(lb.l)+'</b></span>'+
   '<span>收 <b style="color:'+(lb.c>=lb.o?'#EE5A54':'#34B37E')+'">'+f(lb.c)+'</b></span>'+
   '<span>量 <b>'+vol+'</b></span>'+(hovering?'':'<span class="lt">（最新一根）</span>'));
}

/* ---------------- 進場當下的客觀盤面（7 格） ---------------- */
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
 const box=document.getElementById('rfstrip');
 if(!F){ rset('rfstrip','<div class="fcell" style="grid-column:1/-1;color:var(--faint);'+
   'text-align:center;padding:16px">這一刻沒有本機 K 棒可以重建盤面</div>'); return; }
 const c=(l,v,cls)=>'<div class="fcell"><div class="l">'+l+'</div><div class="v '+(cls||'flat')+
   '">'+v+'</div></div>';
 rset('rfstrip',
   c('最近 5 分',pm(F.mom5),sgn(F.mom5))+
   c('最近 15 分',pm(F.mom15),sgn(F.mom15))+
   c('對開盤',pm(F.ret_open),sgn(F.ret_open))+
   c('跳空',pm(F.gap),sgn(F.gap))+
   c('今日震幅',f(F.rng))+
   c('位階',F.pos==null?'—':f(F.pos*100)+'%')+
   c('量能',F.vol_ratio==null?'—':f(F.vol_ratio,2)+'倍'));
}

/* ---------------- 右欄：翻紀錄 ---------------- */
function rowHTML(x){
 const t=x.t, rs=REASON[t._reason]||'';
 return '<div class="trade'+(x.i===SEL?' sel':'')+'" data-rpick="'+x.i+'">'+
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
/* 播放控制列（在圖的正下方，眼睛不用離開圖） */
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
 return '<div class="rpbar">'+
   '<button class="rpbtn" data-ract="rphome" title="回到 08:45">⏮</button>'+
   '<button class="rpbtn" data-ract="rpback" title="退一根">◀</button>'+
   '<button class="rpbtn play" data-ract="'+(playing?'rppause':'rpplay')+'">'+
     (playing?'❚❚ 暫停':'▶ 播放')+'</button>'+
   '<button class="rpbtn" data-ract="rpstep" title="下一根">▶▶</button>'+
   '<div class="rpsp">'+SPEEDS.map(s=>'<button data-rspeed="'+s[0]+'" class="'+
     (RP.speed===s[0]?'on':'')+'">×'+s[0]+'</button>').join('')+'</div>'+
   '<div class="rppos"><b>'+cur.t+'</b>　'+(RP.rev+1)+' / '+B.length+' 根'+
     (RP.state==='revealed'?'　已揭曉':'')+'</div></div>';
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
            ok, msg = sync_to_cloud()
            return self._json(200, {"ok": ok, "msg": msg})

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
            with state_lock:
                payload = json.dumps(STATE, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
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
                    px = api.snapshots([c])[0].close
                    if px:
                        INDEX.update({"price": float(px), "at": time.time()})
            else:
                INDEX["price"] = None          # 現貨沒開盤就不要顯示舊值
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
    try:
        webbrowser.open(url)
    except Exception:
        pass

    if "--replay" in sys.argv:
        run_replay(hist, sys.argv[sys.argv.index("--replay") + 1])
        return

    import shioaji as sj
    from shioaji import BidAskFOPv1, Exchange, TickFOPv1
    from _config import get_credentials

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
                # 過了午夜但還沒到 08:30：沿用現有連線，只標記今天尚未開盤重建
                session["date"] = today
                session["opened"] = False

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
