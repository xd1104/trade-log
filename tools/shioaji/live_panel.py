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
from urllib.parse import parse_qs, urlsplit

import numpy as np
import pandas as pd

import broker            # 真實下單。預設 dry run，見那個檔開頭的說明
import auto_fire         # 自動下單。預設**關著**（沒有 AUTO_ORDERS_ON 就完全不送）
import night_fire        # 夜盤自動下單（台積電快攻）。預設**關著**（沒有 NIGHT_ORDERS_ON 就完全不送；跟日盤開關分開）
import risk_cap          # 風控規則 B（2026-09-24）：本月自動單虧到上限就不送。⛔ 唯讀模組
import tick_writer       # 逐筆報價落地。⛔ 它的存在前提是「絕不在 on_tick 裡碰磁碟」
# 【健檢】那一頁（唯讀）。⛔ 不 import broker、不碰任何下單路徑、一個位元組都不寫。
# ⛔⛔ 一定要包 try（跟 strategy_lab 同一條理由）：它是「看的東西」，載入失敗
#    絕不可以讓 `import live_panel` 跟著失敗 —— 那會變成 main() 跑不到、
#    看門狗無限重開、**停損沒人盯**。失敗時 health=None：/api/health/state 回 503，其他照跑。
# 【交易分析師】每週報告（2026-09-24）。⛔ 唯讀＋只寫 analyst/read.json（讀過狀態）；包 try 同上理由。
try:
    import analyst
except Exception as _an_err:           # noqa: BLE001  ⛔ 刻意接住所有例外
    analyst = None
    print("⚠️ 【分析師】載入失敗（其他功能不受影響）：%s" % _an_err, flush=True)
try:
    import health
except Exception as _hc_err:           # noqa: BLE001  ⛔ 刻意接住所有例外
    health = None
    print("⚠️ 【健檢】載入失敗（其他功能不受影響）：%s" % _hc_err, flush=True)
# 【策略實驗室】歷史逐筆回測（唯讀）。⛔ 不 import broker、不碰任何下單路徑。
# ⛔⛔ 一定要包 try（2026-09-14 lab-qa 退件 R1）：這是研究功能，它載入失敗（少一個套件、
#    檔案壞掉）絕不可以讓 `import live_panel` 跟著失敗 —— 那會變成 main() 跑不到、
#    看門狗無限重開、**停損沒人盯**。失敗時 strategy_lab=None：/api/lab/* 回 503，其他照跑。
try:
    import strategy_lab
except Exception as _lab_err:          # noqa: BLE001  ⛔ 刻意接住所有例外
    strategy_lab = None
    print(f"⚠️ 【策略實驗室】載入失敗，這一頁先停用（其他功能不受影響）：{str(_lab_err)[:160]}")
# 【策略實驗室】最上面那張「模擬（不會下單）」（2026-09-15 晚上加）。⛔ 模擬：不 import broker／auto_fire，
#    規則函式由 start_sim_lanes() 注入。⛔ 同樣一定要包 try：它壞掉 ⇒ sim_lanes=None、/api/sim/state 回 503，其他照跑。
try:
    import sim_lanes
except Exception as _sim_err:          # noqa: BLE001  ⛔ 刻意接住所有例外
    sim_lanes = None
    print(f"⚠️ 【模擬】載入失敗，這張卡先停用（其他功能不受影響）：{str(_sim_err)[:160]}")

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

# 2026-09-14 Benson 從 ±100 改成 ±130（逐筆回測候選 1：09:03:00 ＋ ±130，202 天 54.5%／每筆 +7.5，**還沒過 2026 年驗證**）。
# ⛔ 這一組是：真單停損監控（手動真單那一口）、手動真單的停利單、練習下單、【自動下單（模擬）】結算、
#    前端的停利停損預覽與線（PAGE 定義完會把這兩個數字注入前端，前端不准自己寫死）。
# ⚠️⚠️ 2026-09-15 起**自動下單不再用這一組**：它改成「開盤快才做 ＋ ±0.5%」（正本在 auto_fire.FAST_RULE），
#    每一口部位自己帶 sl_points；check_real_position 只有在部位**沒有** sl_points 時才用 SL_POINTS。
TP_POINTS = 130.0
SL_POINTS = 130.0
# 當過產品常數的下單規則值（公開規格，git 歷史裡看得到，不是他的個人資料）。
# ⛔ 留著是給 tools/probe/leak-scan.py 讀的：它把「等於產品常數的值」當成公開資訊。
#    規則一改，repo 裡大量寫著 09:03:30／±100 的註解與測試就會被誤判成他的真實紀錄（實測 651 處）。
# ⛔ 這裡**只准放「曾經真的是產品常數」的值**，不准拿來替任何其他數字開後門。
#    （2026-09-15 改成 tuple：09:03:30 用到 09-13、09:03:00 用在 09-14、09-15 起又回到 09:03:30；
#      ±100 用到 09-13、±130 從 09-14 起。leak-scan 只對 `PAST_` 開頭的名字收 tuple。）
PAST_SIGNAL_AT = ("09:03:30", "09:03:00")
PAST_TP_POINTS = (100.0, 130.0)
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


# ⛔⛔ 部位自己帶的點數，上限 ＝ 進場價 × 這個比例（2026-09-15 QA 退件後 PM 升為必修）。
#    自動下單正常是 ±0.5%（約 230 點）；sl_points 是 inf 或 1e11 這種值時 `entry − d × 點數`
#    會落在永遠碰不到的地方 ⇒ **停損永遠不觸發**。超過就退回 SL_POINTS 並示警（寧可用手動那一套）。
POS_POINTS_MAX_FRAC = 0.02
POS_POINTS_BAD = {"n": 0}          # 看到壞點數的次數（示警每口每個值只印一次，這裡累計給人查）


def _pos_points(pos, key, default):
    """
    部位自己帶的點數（sl_points／tp_points）。
      - 部位沒有這個欄位（手動真單）⇒ default，**不示警**（那是正常狀態）
      - 有但不可信 ⇒ default **並示警**：bool／非數字／NaN／inf／≤0／
        超過進場價 × POS_POINTS_MAX_FRAC（2%）／進場價本身看不懂（無從檢查上限）
    示警 ＝ 部位上掛 `sl_warn`（或 `tp_warn`；/api/state 與畫面端得出去）＋ 主控台一次 ＋ 計數。
    ⛔ 純記憶體（停損迴圈 4Hz 會叫）：只改這個 dict、一行 I/O 都沒有；主控台那行每口只印一次。
    ⚠️ bool 要另外擋：isinstance(True, int) 是 True。
    """
    if not isinstance(pos, dict) or pos.get(key) is None:
        return default
    v = pos.get(key)
    e = pos.get("entry")
    bad = None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        bad = "看不懂（%r）" % (v,)
    elif not math.isfinite(v):
        bad = "不是有限的數（%r）" % (v,)
    elif v <= 0:
        bad = "不是正數（%g）" % v
    elif (isinstance(e, bool) or not isinstance(e, (int, float)) or not math.isfinite(e)
          or e <= 0):
        bad = "進場價看不懂（%r），檢查不了上限" % (e,)
    elif v > e * POS_POINTS_MAX_FRAC:
        bad = "%g 點超過進場價的 %g%%（%g 點）" % (v, POS_POINTS_MAX_FRAC * 100,
                                             round(e * POS_POINTS_MAX_FRAC))
    if bad is None:
        return float(v)
    wk = "sl_warn" if key == "sl_points" else "tp_warn"
    flag = "_bad_" + key
    if pos.get(flag) != repr(v):
        pos[flag] = repr(v)
        POS_POINTS_BAD["n"] += 1
        pos[wk] = ("這一口帶的%s點數壞掉了：%s —— 改用手動真單那一套 %g 點"
                   % ("停損" if key == "sl_points" else "停利", bad, default))
        print("⚠️ [真實] " + pos[wk], flush=True)
    return default


def pos_sl_points(pos):
    """這一口的停損點數：部位有 sl_points（自動下單）用它，沒有用 SL_POINTS（手動真單）。"""
    return _pos_points(pos, "sl_points", SL_POINTS)


def pos_tp_points(pos):
    """
    這一口的停利點數（畫面用；真正的停利單在券商那邊）。沒有就 TP_POINTS。

    ⭐⭐ 部位掛著 `no_tp`（2026-09-16，「開箱」那個候選不設停利）⇒ 回 **None**。
       ⛔⛔ 不可以掉回 `_pos_points(...)` 的 default —— 那一口**沒有 tp_points 這個欄位**，
       `_pos_points` 會安靜地回 TP_POINTS（130）⇒ 畫面畫出一條**根本不存在的停利線**
       （2026-09-16 實跑確認：entry 23000 的無停利部位被畫成停利 23130）。
       ⚠️ 呼叫端拿到 None 要自己處理（`real_state` 的 `snap["tp"]`、前端那張卡）。
    """
    if isinstance(pos, dict) and pos.get("no_tp"):
        return None
    return _pos_points(pos, "tp_points", TP_POINTS)


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
    # ⭐ 2026-09-15：**這一口自己的停損點數**（自動下單那一口帶 ±0.5% 算出來的 sl_points），
    #    沒有才用 SL_POINTS（手動真單那一口）。⛔ 只讀記憶體（這裡是 4Hz 主迴圈、是他的停損）。
    sl = pos["entry"] - d * pos_sl_points(pos)
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
#    ⚠️ 2026-09-14 前端那一頁換成【策略實驗室】（#tab-lab），**這一段後端照樣在跑**
#      （【自動下單】每一筆的「模擬那邊」讀的就是 autotest/），紅線一條都沒放寬。
#    這一段（以及 PAGE 裡 #tab-lab 那一段）**一行都不准** import broker、呼叫 broker.*、
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

# ⛔ 只有這一個地方定義，不要散在程式各處（前端也是從這裡注入）。
# 2026-09-14 Benson 從 09:03:30 改成 09:03:00（同上，候選 1）。這個之前的模擬／真單紀錄都是 09:03:30 進場。
# ⭐ 2026-09-15 改回 09:03:30：自動下單換成「開盤快才做」（研究的訊號窗是 09:00→09:03:30）。
#    ⚠️ 這兩個常數也餵【自動下單（模擬）】的底層記錄 —— **刻意共用同一份快照**（兩把尺原則），
#       所以模擬記錄的時刻跟著變；⛔ 但模擬的 ±130 結算不動（AUTO_TP／AUTO_SL 仍是 TP_POINTS）。
#    ⛔ 要改就三個一起改：SIGNAL_AT、SIGNAL_SEC、AUTO_SETTLE_FROM（autotest-backend.py 在守）。
SIGNAL_AT = "09:03:30"
SIGNAL_SEC = 9 * 3600 + 3 * 60 + 30

# ⭐⭐ 【自動下單】「快攻回馬槍」的回馬槍那一刻（2026-09-15 晚上 Benson 決定隔天起用）。
#    **正本只有這裡**，auto_fire 靠 configure(rev_at=REV_AT, rev_sec=REV_SEC) 接過去。
#    09:03:30 不夠快的日子，主迴圈跨過這一刻時快照（同一支 _auto_snap），順勢方向反轉了才做 1 口。
#    研究：tick-research/scripts/defs_research.py（09:10／09:15／09:20 × 7 種定義，「09:15 方向相反就算」）。
#    ⛔ 只管自動下單；【自動下單（模擬）】那一頁不看這個時刻。
#    ⛔ 要改就兩個一起改（字串與秒數），test_auto_fire.py ⑰ 斷言兩個對得上。
REV_AT = "09:15:00"
REV_SEC = 9 * 3600 + 15 * 60

# ⭐⭐ 【自動下單】「開盤快才做」的門檻百分位。**正本只有這裡**，auto_fire 靠 configure(pctl=…) 接過去
#    （⛔ auto_fire 裡不准再寫一份；前端從 /api/fire/state 的 rule.pctl 拿，⛔ 不准寫死）。
#    門檻 ＝ 過去 40 個交易日（不含今天）開盤走幅的 numpy.percentile(…, FAST_PCTL)。
# ⚠️ 2026-09-15 下午 Benson 拍板由 70 改成 80：研究的 17 組測試（tick-research benson_rule／exam25h2）
#    裡**唯一過多重檢定門檻**的是 80 百分位那一組；70 那一組沒過。⛔ 要改回去先重跑研究，不是調參。
# ⚠️ leak-scan 會把這個模組層級常數讀成公開值（"80"）⇒ 值剛好是 80 的欄位失去作證資格；
#    2026-09-15 lab-dev 實測：有／沒有這個常數，拿他的真實紀錄掃全 repo 都是命中 0 處（沒有值剛好是 80 的欄位被放掉）。
#    ⚠️ 但它確實讓「80」這個值失去作證資格 —— 常數清單變長是 leak-scan 自己要盯的那件事（見 leak-scan.py 檔頭 ①）。
FAST_PCTL = 80

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

# ⭐⭐⭐ 2026-09-16：**結算日日盤 13:30 就收盤**（不是 13:45）。
#   舊版 `EOD_CLOSE_AT` 只有一個值、沒有結算日分支 ⇒ 結算日那天 13:43:30 才要平倉，
#   而市場 13:30 就關了 ⇒ **單子送不出去、部位抱過夜，而且 13:45 起停損也停了**。
#   （約每 20 個交易日就來一次 —— 一個月一天。）
#   ⚠️ 兩個數字的關係跟平常那一組一模一樣：**收盤前 90 秒**（13:45−90s＝13:43:30／
#      13:30−90s＝13:28:30），因為 `auto_fire.EOD_WINDOW_S` 那 75 秒的重試窗口是實測值，
#      換一個收盤時刻不會改變它要多久。
#   ⛔ 要改的話兩件事一起改：字串與秒數（`test_auto_fire.py` 在守）。
EOD_CLOSE_AT_EXPIRY = "13:28:30"
EOD_CLOSE_SEC_EXPIRY = 13 * 3600 + 28 * 60 + 30
# 結算日的日盤收盤（＝觸發區間的上界；平常是 DAY_END 13:45）
DAY_END_SEC_EXPIRY = 13 * 3600 + 30 * 60
# 今天是不是結算日 —— **一天只算一次**（`_auto_tick` 在 4Hz 主迴圈上，
# ⛔ 那裡不准讀檔；`strategy_lab.expiry_state_cal` 會讀 days.jsonl）。
# ⭐ `sure`＝那個答案有沒有把握（False ⇒ 用了平常那一組，但我們其實**不知道**）。
EOD_DAY = {"date": None, "expiry": False, "sure": True,
           "at": EOD_CLOSE_AT, "sec": EOD_CLOSE_SEC, "end": None, "err": None}


def eod_plan(d):
    """
    今天的收盤平倉時刻。回 `EOD_DAY`（同一天只算一次，之後都讀記憶體）。

    ⚠️ **讀行事曆是磁碟 I/O** ⇒ 只在「跨日的第一圈」算一次，之後 4Hz 主迴圈只讀 dict。

    ⛔⛔⛔ **判不出來 ⇒ 用平常那一組（13:43:30）＋ 把原因寫進 `err` ＋ 端到畫面上**
       （PM 2026-09-17 裁示）。⛔ 不可以安靜地用 13:28:30，
       ⛔ 「今天是結算日」那句話**在不確定的時候不准出現**。
       ⚠️ 這條裁示**推翻了 2026-09-16 版的那句「有疑慮時寧可早平」**，理由是實測出來的：
          舊版把「行事曆查不到第三個週三」當成「那天休市 ⇒ 順延」⇒
          `days.jsonl` 只到 09-15 時，**09-17～09-30 整整 14 個平常日全被判成結算日**，
          每天安靜地提早 15 分鐘平倉、而且 `err` 是 None。
          「不確定就早平」聽起來保守，但它的真實成本是**每個月十幾天的系統性錯誤**；
          反過來那一邊（真的是移動過的結算日卻沒認出來）只在「農曆年 ＋ 行事曆過期」
          同時成立時才發生，而且畫面會大聲講「判不出來」。
       ⛔ 判斷的正本是 `strategy_lab.expiry_state()`（三態：是／不是／**不知道**）。

    ⚠️ 真正的第三個週三**仍然判得出來**（主判斷就是它）—— 會落到「不知道」的只有
       「第三個週三查不到、它後面也還沒有任何交易日」的那幾天。
    """
    ds = str(d)
    if EOD_DAY["date"] == ds:
        return EOD_DAY
    exp, sure, err = False, True, None
    try:
        if strategy_lab is None:
            raise RuntimeError("策略實驗室模組沒載起來（行事曆讀不到）")
        st, why = strategy_lab.expiry_state_cal(d if isinstance(d, date) else
                                                date.fromisoformat(ds))
        if st is None:
            sure = False
            err = ("判不出今天是不是結算日：%s —— 收盤平倉用平常那一組（%s），"
                   "⛔ 今天不保證 13:30 收盤前平得掉" % (why, EOD_CLOSE_AT))
            print("⚠️ [自動下單] " + err, flush=True)
        else:
            exp = bool(st)
    except Exception as e:
        sure = False
        err = "判不出今天是不是結算日（%s）—— 收盤平倉用平常那一組" % str(e)[:80]
        print("⚠️ [自動下單] " + err, flush=True)
    EOD_DAY.update({"date": ds, "expiry": exp, "sure": sure, "err": err,
                    "at": EOD_CLOSE_AT_EXPIRY if exp else EOD_CLOSE_AT,
                    "sec": EOD_CLOSE_SEC_EXPIRY if exp else EOD_CLOSE_SEC,
                    "end": DAY_END_SEC_EXPIRY if exp else None})
    if exp:
        print("[自動下單] 今天是結算日（日盤 13:30 收盤）⇒ 收盤平倉提前到 %s"
              % EOD_CLOSE_AT_EXPIRY, flush=True)
    return EOD_DAY
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
# ⚠️ 2026-09-14 規則改成 09:03:00 ＋ ±130 之後，用同一套量法（autotest-backend.py ⑦：tmf_1min.csv 最後 505 天、
#    一律做多、進場用訊號時刻那根 K 的收盤代理、不扣費）重量：σ = **119.27** ⇒ 天花板(505)=14.9、(20)=74.7。
#    上面 96.61 那一段是 ±100／09:03:30 時代的數字，留著當歷史。
CEIL_SIGMA = 119.3
CEIL_Z = 2.80                           # 1.96（不是雜訊）＋ 0.84（八成機率被抓到）

AUTO_TP = TP_POINTS                     # ⛔ 跟他真的在用的規則同一組常數，不另開一份
AUTO_SL = SL_POINTS
# 結算從**第一根整根都在進場之後的 1 分 K（含）**開始 —— 標籤是起始時間。
#   進場 09:03:30 時是標籤 09:04（09:03 那根含進場前的價）；
#   ⚠️ 2026-09-14 改成 09:03:00 進場 ⇒ 標籤 09:03 那根（09:03~09:04）整根都在進場之後 ⇒ 從 09:03 算，
#      不改的話 09:03:00~09:04:00 這一分鐘的觸價會整段看不到。⛔ 這個要跟 SIGNAL_AT 一起改。
#   ⭐ 2026-09-15 SIGNAL_AT 改回 09:03:30 ⇒ 這裡跟著改回 09:04（標籤 09:03 那根含進場前的價，要排掉）。
# ⛔ 比較一定要用 `>=`（見 _auto_settle 的說明；用 `>` 會少掉第一根）。
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
        # rev ＝ 今天回馬槍那一刻（REV_SEC）觸發過了沒（⛔ 同上，只防重複觸發；真相在 autofire/）
        "rev": False,
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

def _auto_snap(st, now, at_sec=None):
    """
    ⚠️ **這個函式跑在 4Hz 主迴圈上**，所以裡面只准有記憶體讀取 —— 一行 I/O、
       一次 pandas、一個鎖都不准有。真正的組裝與落地在 _auto_worker()。
    ⭐ `at_sec`（2026-09-15 晚上）：回馬槍那一刻（REV_SEC）也用**這一支**快照
       （⛔ 不另寫一份價來源），只差 `at_lag_ms` 從哪一刻起算；不給 ＝ SIGNAL_SEC（原行為）。

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
    # ⭐ 2026-09-15【自動下單】「開盤快不快」的參考價 ref0900 ＝ **09:00 以前最後一筆成交**
    #    （研究 benson_rule.py 的 `last_before(D, 09:00:00)`）。
    #    ⚠️ 跟上面 p0900 是**同兩個來源、順序相反**：方向（做法 A）沿用既有的「09:00 那一分鐘第一筆」，
    #       走幅要的是「09:00 以前最後一筆」—— ⛔ 不要為了少一個欄位把兩個併成一個（那會改到模擬的方向）。
    #    拿不到 minute_close[539] 才退到 minute_bar[540]["o"]，用了哪一種記在 ref_src。
    #    ⚠️ 已知：盤中重啟時 minute_close 是 seed_from_bars() 拿 1 分 K 補的（K 棒用**結束時間**標記），
    #       那時的 [539] 其實是 08:58~08:59 那根的收盤 —— 早了一分鐘。只影響「09:00 之後才重啟、
    #       又還趕得上 09:03:30」那幾分鐘，跟 p0900 的 prev_min_close 退路是同一個既有性質。
    ref0900, ref_src = None, None
    if st is not None and isinstance(st.minute_close.get(539), (int, float)):
        ref0900, ref_src = float(st.minute_close[539]), "prev_min_close"
    elif mi is not None and isinstance(mi.get("o"), (int, float)):
        ref0900, ref_src = float(mi["o"]), "bar_open"
    age = None if (st is None or st.last_recv is None) else (time.time() - st.last_recv)
    return {
        "date": str(now.date()),
        "at": now.strftime("%H:%M:%S.") + f"{now.microsecond // 1000:03d}",
        "at_lag_ms": int(round((now.hour * 3600 + now.minute * 60 + now.second
                                + now.microsecond / 1e6
                                - (SIGNAL_SEC if at_sec is None else at_sec)) * 1000)),
        "px": None if st is None else st.price,
        "bid": None if st is None else st.bid,
        "ask": None if st is None else st.ask,
        "is_mid": bool(st is not None and st.price_is_mid),
        "quote_age_ms": None if age is None else int(round(age * 1000)),
        "open0845": None if st is None else st.open,
        "p0900": p0900, "p0900_src": p0900_src,
        "ref0900": ref0900, "ref_src": ref_src,
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


def _auto_eod_noop(day, lag_ms, at=None):
    """預設的收盤平倉掛勾：**什麼都不做**（跟 `_auto_noop` 同一套規矩）。
    ⭐ `at`（2026-09-16）＝今天實際用的平倉時刻（結算日 13:28:30）。"""
    return None


# ⛔⛔ 13:43:30 那一刻唯一的對外出口（收盤自動平倉）。**預設是 no-op**，
#    只有 main() 會把它接到 auto_fire.on_eod。
#    ⚠️ 這一條會**送出平倉單**，所以規矩跟 AUTO_SIG_HOOK 一模一樣：
#      ・這一行以外不准有第二個地方指派它
#      ・掛上去的函式**只准 put_nowait**（真正的平倉在 auto_fire 自己的執行緒上跑，
#        `broker.close()` 會等成交最多十幾秒 —— ⛔ 那絕對不可以卡在 4Hz 主迴圈上，
#        那條迴圈就是他的停損）
AUTO_EOD_HOOK = _auto_eod_noop


def _auto_rev_noop(snap, day, lag_ms):
    """預設的回馬槍（09:15）掛勾：**什麼都不做**（跟 `_auto_noop` 同一套規矩）。"""
    return None


# ⛔⛔ 回馬槍那一刻（REV_AT）唯一的對外出口。**預設是 no-op**，只有 main() 會接到 auto_fire.on_reversal。
#    ⚠️ 這一條**會送出進場單**（反轉的日子）⇒ 規矩跟 AUTO_SIG_HOOK 一模一樣：
#      ・這一行以外不准有第二個地方指派它（`test_auto_fire.py` ⑧ 在守）
#      ・掛上去的函式**只准 put_nowait**
AUTO_REV_HOOK = _auto_rev_noop


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
                     "rev": False, "gaps": 0.0})
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
    # ⭐⭐ 回馬槍那一刻（REV_SEC，2026-09-15 晚上）。⚠️ 一定要排在 09:03:30 那一段**後面**：
    #     面板剛好在 09:15 之後才開起來時，同一圈裡 09:03:30 那一件（late）先進佇列，
    #     工作執行緒 FIFO 先處理它 ⇒ 09:15 那一件讀帳本時看得到那一天已經有定論。
    #     ⛔ 這裡只有整數比較、_auto_snap（記憶體）與 put_nowait；判斷與送單在 auto_fire 的工作執行緒。
    #     ⛔ 晚到（lag > AUTO_LATE_MS：面板 09:20 才開、看門狗剛重啟、時鐘往前跳）⇒ 快照給 None
    #        ⇒ 那邊記 late、**不補單**（跟 09:03:30 同一套）。
    if not AUTO["rev"] and secs >= REV_SEC:
        AUTO["rev"] = True
        lag_r = (secs - REV_SEC) * 1000 + now.microsecond // 1000
        if lag_r > AUTO_LATE_MS:
            AUTO_REV_HOOK(None, d, lag_r)
        else:
            AUTO_REV_HOOK(_auto_snap(st, now, REV_SEC), d, lag_r)
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
    #     ⭐⭐ 2026-09-16：**結算日日盤 13:30 收盤** ⇒ 平倉時刻與上界都要跟著換
    #        （13:28:30 ~ 13:30）。⛔ 舊版只有一組數字 ⇒ 結算日那天 13:43:30 才要平，
    #        那時市場已經關了 ⇒ 送不出去、抱過夜、13:45 起連停損都沒有。
    #        ⚠️ `eod_plan()` **一天只算一次**（跨日那一圈讀一次行事曆），之後只讀 dict ——
    #           這一段仍然只有整數比較與 put_nowait。
    _eod = EOD_DAY if EOD_DAY["date"] == d else eod_plan(d)
    _eod_sec = _eod["sec"]
    _eod_end = _eod["end"] or DAY_END_SEC
    if not AUTO["eod"] and _eod_sec <= secs < _eod_end:
        AUTO["eod"] = True
        AUTO_EOD_HOOK(d, (secs - _eod_sec) * 1000 + now.microsecond // 1000, _eod["at"])
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


# ---------------------------------- 【自動下單】那一口的「下場」（⛔ 唯讀 real_trades/）
#
# ⛔⛔ **這一段補的是一個真的 bug，不是新功能**（2026-09-10 lab-ux 查到、PM 拍板）：
#    `auto_fire._eod()` 在停利成交的日子走的是 `pos is None → eod_flat`，
#    而撈出場價的 `_eod_exit_of()` **只掛在 `eod_closed` 那一條路上** ⇒
#    **停利成交的那些天，`autofire/*.jsonl` 裡永遠不會有出場價與點數**
#    （不是「等收盤才有」，是永遠沒有）。而那正是他最想看的那幾天。
#
# ⇒ 修法選的是候選 1：**在端點層唯讀比對 `real_trades/`**。
#    ⛔ 不動 `auto_fire.py` 的邏輯、⛔ 不動 `broker.py`、⛔ 不碰停損那條路 ——
#       出場價的真相本來就已經在 `real_trades/` 裡（`broker.record_trade()` 寫的，
#       停利成交、他按平倉、收盤平倉、reconcile 發現部位不見了，四種都會留一列）。
#    ⛔ 讀法**沿用既有的 `_auto_mine_day()`**（明文標「唯讀 real_trades/，
#       一個位元組都不寫」），⛔ 不新寫一份檔案存取。
#
# ⛔⛔ **對不到／對到多筆一律留白**（沿用「問不到成交價就留白」那條鐵律）：
#    ⛔ 不准挑一筆、⛔ 不准拿現價頂。**留白看得出來是缺，編的數字看不出來。**
#    而且「對不起來」跟「還開著」**不是同一句話** —— 前者長得像「那口其實沒平掉」，
#    所以它要有自己的 tag ＋ 摘要那一行的示警（⛔ 但不擋，那不是錯誤）。
#
# ⛔ **效能**：`/api/fire/state` 每 5 秒被輪詢，而 HTTP 執行緒跟 4Hz 主迴圈
#    （＝他的停損）搶同一個 GIL ⇒ ⛔ 不可以每次全掃。兩道：
#      ① 只看畫面上那份清單的前 `FIRE_REAL_DAYS` 天（跟 `alListHTML()` 同一個數）；
#      ② 每一天一份 `(mtime_ns, size)` 快取 —— 過去的日子那個檔一輩子不會再變，
#         所以穩態下**一次 stat、零次讀檔**。
#    ⚠️ 快取鍵要帶 size，⛔ 只比 mtime 不夠（同一秒續寫會讓畫面停在舊資料，
#       跟 `_auto_read_month` 同一個坑）。

FIRE_REAL_DAYS = 60             # ⛔ 跟前端 alTblHTML() 的 slice(0, 60) 是同一個數
_FIRE_REAL_CACHE = {}           # date -> ((mtime_ns, size) | None, [那天的來回])
_FIRE_REAL_LOCK = threading.Lock()   # ⛔ 只保護這份快取，絕對不可以碰 state_lock


def _fire_real_day(d):
    """某一天他的 `real_trades/`（⛔ 唯讀）＋ `(mtime, size)` 快取。"""
    p = AUTO_REAL_DIR / f"{d}.jsonl"
    try:
        st = p.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None                  # 那天沒有檔（＝他那天沒有平掉任何一趟）
    with _FIRE_REAL_LOCK:
        hit = _FIRE_REAL_CACHE.get(d)
        if hit is not None and hit[0] == key:
            return hit[1]
    rows = _auto_mine_day(d) if key is not None else []
    with _FIRE_REAL_LOCK:
        _FIRE_REAL_CACHE[d] = (key, rows)
    return rows


def fire_real_pairs(days, today=None, now=None, eod_at=None):
    """
    【自動下單】那一口**後來怎麼了**。一天一筆，`date` 當鍵。**唯讀。**

    鑰匙 ＝ `(date, entry_time 比到分, entry ±EOD_PX_TOL, dir)` ——
    ⛔ **沿用既有的那套認人方式**（`broker.set_trade_note()`／
    `auto_fire._looks_ours()`／`_eod_exit_of()` 配的是同一組欄位），
    容差直接拿 `auto_fire.EOD_PX_TOL`，⛔ 不在這裡寫死第二個 1.0（那就是兩把尺）。

    `state` 五種，⛔ 一種都不准跟別種寫同一句：
      `ok`      對到**恰好一筆**（出場價／時間／點數／理由照抄那一列；
                ⚠️ `exit`／`points` 本來就可能是 None ＝ 問不到成交價，那也照實留白）
      `open`    今天、而且收盤平倉那一刻還沒到 ⇒ **那口還開著**（正常，不示警）
      `drill`   演練（`live:false`）⇒ 那一趟**結構上不存在**於 `real_trades/`
                （`broker.close()` 在 dry run 時**不會**呼叫 `record_trade()`），
                ⛔ 所以絕對不可以把它算成「對不起來」
      `none`    對不到 ⇒ **留白 ＋ 示警**（它跟「那口其實沒平掉」長得像）
      `many`    對到多筆 ⇒ 同上，⛔ 不准挑一筆
    """
    out = {}
    tol = auto_fire.EOD_PX_TOL
    # ⚠️ 兩個都是後端自己產的 `HH:MM:SS`（補零、等長）⇒ 直接比字串就是比時刻，
    #    ⛔ 不必再開一支「時分秒轉秒數」的函式（那就是第二把尺）。
    hhmmss = re.compile(r"^\d\d:\d\d:\d\d$")
    can_time = bool(hhmmss.match(str(now or "")) and hhmmss.match(str(eod_at or "")))
    for r in (days or [])[:FIRE_REAL_DAYS]:
        if not isinstance(r, dict):
            continue
        d = r.get("date")
        # ⛔ 只有「真的開出部位」那幾天才有下場可談；沒送的那幾天講什麼都是雜訊。
        if not d or r.get("rec") != "result" or not r.get("ok"):
            continue
        if not r.get("live"):
            out[d] = {"state": "drill"}
            continue
        e, dv = _auto_num(r.get("entry")), r.get("dir")
        want_t = str(r.get("entry_time") or "")[:5]
        hits = []
        for t in _fire_real_day(d):
            if t.get("dir") != dv or not dv:
                continue
            # ⚠️ 面板重啟後撿回來的部位 entry_time 是 None ⇒ 這裡對不上是**對的**
            #    （寧可留白，不可以把別人的一趟掛到自動下單頭上）。
            if not want_t or str(t.get("entry_time") or "")[:5] != want_t:
                continue
            te = _auto_num(t.get("entry"))
            if te is None or e is None or abs(te - e) > tol:
                continue
            hits.append(t)
        if len(hits) == 1:
            t = hits[0]
            out[d] = {"state": "ok", "exit": _auto_num(t.get("exit")),
                      "exit_time": t.get("exit_time"),
                      "points": _auto_num(t.get("points")), "why": t.get("why")}
        elif not hits:
            # 今天、而且還沒到收盤平倉那一刻 ⇒ 那口本來就還開著（⛔ 不是對不起來）。
            holding = (d == today and can_time and str(now) < str(eod_at))
            out[d] = {"state": "open" if holding else "none"}
        else:
            out[d] = {"state": "many", "n": len(hits)}
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


def _fire_wait_open(d):
    """
    今天帳本有 `rec:"wait"`（09:03:30 判成不快、等回馬槍）而且**還沒有定論**（沒有 fire／result／skip）。
    ⛔ 條件照抄 `auto_fire._rev()`（它就是看這兩件事決定 09:15 要不要往下走）。讀不到 ⇒ False。
    """
    try:
        rows = auto_fire._rows_of(d)
    except Exception:
        return False
    return (any(o.get("rec") == "wait" for o in rows)
            and not any(o.get("rec") in ("fire", "result", "skip") for o in rows))


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
    # ⓪ ⭐ 回馬槍還沒判（2026-09-15 晚上 lab-qa 限制②）：09:03:30 判成 wait、還沒有定論，
    #    而牆上時鐘還沒過 REV_SEC ＋ AUTO_LATE_MS ⇒ `_auto_tick` 跨過 REV_SEC 時照樣會叫
    #    AUTO_REV_HOOK、`auto_fire._rev()` 讀帳本看到 wait 就會**重新讀開關**然後送 ⇒ 要說「今天」。
    #    ⛔ 一定要排在 ① 前面：面板一路開著的話 done 早就是 True，① 會先說「下一個交易日」（那是假話）。
    #    ⛔ 判斷照抄 `_rev()` 看的東西（帳本有 wait、沒有 fire／result／skip）＋ `_auto_tick` 的
    #       `AUTO["rev"]`（今天已經跨過那一刻 ⇒ 那一件已經丟給工作執行緒了，不是這一句能改的）。
    #    ⚠️ 帳本讀的是 auto_fire._rows_of（HTTP 執行緒上的磁碟 I/O；這支本來就只在 HTTP 執行緒被叫）。
    _rev_secs = now.hour * 3600 + now.minute * 60 + now.second
    if (_rev_secs < REV_SEC + AUTO_LATE_MS / 1000
            and not (AUTO.get("day") == str(now.date()) and AUTO.get("rev"))
            and _fire_wait_open(str(now.date()))):
        rev_dt = datetime.combine(now.date(), dtime(0, 0)) + timedelta(seconds=REV_SEC)
        if market_session(rev_dt) == "day":
            return True
    # ① 今天那一刻已經過去了（`_auto_tick` 早就把 done 立起來了）⇒ 只能等下一個交易日
    if AUTO.get("day") == str(now.date()) and AUTO.get("done"):
        return False
    # ② 牆上時鐘。⛔ 用 SIGNAL_SEC（跟 `_auto_tick` 同一個常數），不是 SIGNAL_AT 那個字串
    #    ⛔⛔ **要加上 `AUTO_LATE_MS`**（2026-09-10 PM 裁示，原本只寫 `>= SIGNAL_SEC`）：
    #    `_auto_tick` 在 09:03:30 之後**還有 `AUTO_LATE_MS` 的補送窗口**（lag 在窗口內照樣送）。
    #    面板若在那幾秒**還在啟動**（`serve()` 起來了、`AUTO["started"]` 還沒打開，
    #    中間卡著 `connect()` 的網路等待），他這時候按下去 ——
    #    舊版畫面會說「下一個交易日」，但它**今天就會送一口真單**。
    #    ⇒ 錯的方向是危險那一邊，所以窗口整段都要算「今天」。
    #    正常情況（面板一直開著）由條件 ① 擋下來，行為不變。
    #    ⚠️ 邊界（誠實講）：`_auto_tick` 的判準是 `lag > AUTO_LATE_MS`（＝ lag 剛好
    #       3000ms 還是會送），而這裡只看到「秒」⇒ **09:03:33.000 那一個瞬間**
    #       它會送、這句話卻說「下一個交易日」。4Hz 的迴圈要剛好落在微秒 0 才踩得到，
    #       ⛔ 不要為了它把整個 09:03:33 那一秒都說成「今天」（那一秒其餘 999ms
    #       其實是 late、不會送，反過來又變成另一句假話）。
    if now.hour * 3600 + now.minute * 60 + now.second >= SIGNAL_SEC + AUTO_LATE_MS / 1000:
        return False
    # ③ 「今天的那一刻」是不是日盤 —— ⛔ 問 market_session，不要自己判斷星期
    sig_at = datetime.combine(now.date(), dtime(0, 0)) + timedelta(seconds=SIGNAL_SEC)
    return market_session(sig_at) == "day"


def fire_arm_confirm(live, now=None, mode=None):
    """
    兩段式確認**第二段那句話的正本**。⛔ 前端不准自己算「現在是不是真錢」——
    畫面上那句話講錯的代價是「他以為只是演練，結果那天真的送了一口單」。
    ⚠️ `live` 由呼叫端傳進來（就是 `auto_fire.state()` 算好的那一個），
       ⛔ 這裡**不再問一次** `broker.is_live()` —— 同一件事兩把尺一定會有一把是錯的。
    ⚠️ 「今天／下一個交易日」同理走 `fire_fires_today()`（跟 `_auto_tick` 同一組條件），
       ⛔ 不准在這裡自己寫「明天」兩個字。

    ⭐⭐ 2026-09-17：`mode`＝他正要打開的**那一個做法**（A 快攻回馬槍／U 多方聯軍）。
       ⛔⛔ **兩種的說明各自正確**（Benson 裁示）—— 兩條規則的候選數、做不做空、
          有沒有停利都不一樣，共用一句話就一定有一邊是假話。
    ⚠️ `rule`／`rule_line` 是**同一段話拆成兩半**：`text` 給確認條，`rule_line` 給
       「還沒按下去」那一段（⛔ 不是兩份文案 —— 見 `fire_rule_line()`）。
    """
    mode = mode if mode in auto_fire.METHODS else auto_fire.DEFAULT_METHOD
    when = ("今天 " if fire_fires_today(now) else "下一個交易日 ") + SIGNAL_AT
    name = auto_fire.METHOD_NAME[mode]
    if live:
        # ⚠️ 2026-09-15：自動下單的停利停損是 ±0.5%（auto_fire.FAST_RULE），⛔ 不是 TP_POINTS
        #    （那是手動真單的 ±130）。講錯的話他以為賭的是 130 點，實際一口是 230 點上下。
        # ⛔ 2026-09-17（lab-qa 建議 1）：**這裡不准寫 Markdown 的 `**`** ——
        #    前端是 `esc()` 之後直接塞進 HTML，星號會原樣印在他按下去之前看的最後一個畫面上。
        #    要強調就交給 `emb()`（前端把 `**…**` 轉成 <b>）—— 這裡一律寫純文字。
        return {"live": True, "when": when, "mode": mode, "method_name": name,
                "rule_line": fire_rule_line(mode, when),
                "text": ("現在是**真實下單模式**。打開之後，程式每個交易日會"
                         "**用你的錢**照「%s」送單，第一次是 %s。" % (name, when))}
    return {"live": False, "when": when, "mode": mode, "method_name": name,
            "rule_line": fire_rule_line(mode, when),
            "text": ("現在是演練模式，%s 會照「%s」跑完整條路，但不會真的送單。"
                     % (when, name))}


def fire_rule_line(mode, when=None):
    """
    ⭐⭐ **「這個做法每天會做什麼」那句話的正本**（⛔ 只有這一份）。

    ⛔⛔ 2026-09-17 lab-qa 建議 2：舊版確認條最後一行還掛著**兩候選**的舊描述
       （「開盤夠快就送、不夠快就等 09:15 看反轉」），而多方聯軍是三個候選、
       而且「夠快但做空要跳過」⇒ 那句「開盤夠快就送」在 U 底下是**假話**。
       ⇒ 兩條規則各一句、跟後端這一段對齊，⛔ 前端不准自己再寫一份。
    ⛔ 數字一律從常數來（SIGNAL_AT／REV_AT／auto_fire 的規則 dict），⛔ 不寫死。
    """
    when = when or SIGNAL_AT
    pct = auto_fire.FAST_RULE["tpsl_frac"] * 100
    if mode == "U":
        box = "%s~%s" % (auto_fire.ORB_BOX_FROM_AT[:5], auto_fire.ORB_BOX_TO_AT[:5])
        return ("一天最多 1 口、只做多，三個候選誰最早觸發就做誰 —— "
                "①快攻 %s：開盤夠快**而且方向是做多**才送（夠快但做空就跳過）；"
                "②開箱：%s 的箱子夠寬、**突破上緣**才送，"
                "**這一口不設停利**、停損是箱子的另一端，**券商端一張掛單都沒有**"
                "（停損與收盤平倉都靠面板）；"
                "③純回馬 %s：只有 %s 判定不夠快的日子才有，反轉成做多才送。"
                "快攻與純回馬的停利停損各 ±%g%%（以送單那一刻的價格算）。"
                % (when, box, REV_AT, SIGNAL_AT, pct))
    return ("一天最多 1 口 —— %s 開盤夠快就**順勢**送（做多做空都做）；"
            "不夠快就等 %s，反轉了才送。停利停損各 ±%g%%（以送單那一刻的價格算）。"
            % (when, REV_AT, pct))


# ---------------------------------- 【今天】那張卡：現在離每個門檻還有多遠（⛔ 唯讀）
#
# ⭐⭐ 2026-09-21 Benson 要的：「開盤的時候我想看到現在離這些門檻還有多遠」。
#    起因是 09-21 那天開箱的箱子 170 點、門檻 178 點 —— **差 8 點沒做成**，
#    而畫面要等 09:05 判完才看得出來差多少。三個候選各有各的截止時刻
#    （快攻 09:03:30／開箱 09:05 定箱、09:30 前突破／純回馬 09:15），
#    在那之前他只看得到「等 09:05」這種話，看不到「還差幾點」。
#
# ⛔⛔ **這一段一個判定都不做，也一個字都不准預測。**
#    這裡只做減法：「現在的價」減「auto_fire 已經算好的門檻」。
#    ⛔ 不准出現「今天會做」「應該過得了」「機率」之類的字
#       （CLAUDE.md：UI 上不得出現任何預測、勝率、期望值、買賣建議）。
#    真正的判定永遠只有那三次，而且一律以 `autofire/*.jsonl` 落地的那一列為準 ——
#    這一份在定論出現之後就**自己消失**（`stage=="done"` 就不端了）。
# ⛔ 門檻的數字**全部來自 auto_fire**：`fast_today()` 的 `thr_pct`、`orb_today()` 的
#    `hist_med_pct`、換算一律 `auto_fire.approx_points()`、方向一律 `auto_dirs()`。
#    ⛔ 這裡不准自己算百分位、不准自己乘 0.5%、不准自己判「快不快」。
# ⚠️ 只有「多方聯軍」才有三個候選 ⇒ 判準跟那張卡同一個（`state()` 的 `union`），
#    ⛔ 不在這裡比 `method == "U"`。
# ⚠️ 效能：只做記憶體讀取（`_auto_snap()` 與 `state()` 已經算好的那幾個數）⇒ **零 I/O**。
#    `/api/fire/state` 每 5 秒被輪詢、HTTP 執行緒跟 4Hz 主迴圈（＝他的停損）搶同一個 GIL。

FIRE_GAP_MAX_AGE_MS = 15000      # 報價超過這麼久沒動就不端距離（⛔ 寧可留白，不拿舊價算）
FIRE_GAP_FROM_SEC = 8 * 3600 + 45 * 60       # 日盤開盤（08:45）之前不講距離


def _gap_box_now(st):
    """
    箱子**畫到現在**的高低（⛔ 只是進度，不是定論）⇒ (hi, lo)；算不出來 ⇒ (None, None)。

    ⚠️⚠️ 這一份走 `Today.minute_bar`（on_tick 即時累的），而 09:05 的**定論**走
       `auto_fire._orb_step()` 讀 `tick_logs/` ⇒ 同一條 tick 流，但不是同一次讀
       （面板晚開、掉線重連的分鐘會少）。所以這一行在畫面上一律標「進行中」，
       ⛔ 不可以拿它去判「夠不夠寬」，⛔ 也不可以在 09:05 之後還端它。
    ⚠️ 分鐘索引 ＝ 時×60＋分（`minute_bar[540]` 就是 09:00 那一分鐘），
       範圍取 `auto_fire.ORB_BOX_FROM_MS` ~ `ORB_BOX_TO_MS` 的整數分鐘（⛔ 不寫死 540）。
    """
    if st is None or not getattr(st, "minute_bar", None):
        return None, None
    m0 = int(auto_fire.ORB_BOX_FROM_MS // 60000)
    m1 = int(auto_fire.ORB_BOX_TO_MS // 60000)
    hi = lo = None
    for mi in range(m0, m1):
        b = st.minute_bar.get(mi)
        if not isinstance(b, dict):
            continue
        h, l = b.get("h"), b.get("l")
        if isinstance(h, (int, float)) and not isinstance(h, bool):
            hi = h if hi is None else max(hi, h)
        if isinstance(l, (int, float)) and not isinstance(l, bool):
            lo = l if lo is None else min(lo, l)
    return (None, None) if hi is None or lo is None else (hi, lo)


def _gap_fast(out, snap, sec):
    """快攻：09:03:30 之前的「現在走幾點／門檻幾點」。⇒ 一列或 None。"""
    if sec >= SIGNAL_SEC:
        return None                     # 已經判過了，定論在卡片那一行
    thr_pct = (out.get("fast") or {}).get("thr_pct")
    px, ref = snap.get("px"), snap.get("ref0900")
    mv = auto_fire.move_pct(px, ref)
    if thr_pct is None or mv is None:
        return {"stage": "live", "msg": "現在還算不出距離（拿不到 09:00 的參考價）"}
    need = auto_fire.approx_points(thr_pct, ref)
    now = auto_fire.approx_points(mv, ref)
    # ⛔ 方向走 `auto_dirs()`（正本），⛔ 不在這裡拿 px−ref 自己判 —— 走幅用的
    #    參考價（ref0900＝09:00 以前最後一筆）跟方向用的（p0900＝09:00 第一筆）
    #    是**不同的兩個價**，自己判會悄悄換一把尺。
    sig_a, sig_b = auto_sig(px, snap.get("open0845"), snap.get("p0900"))
    d = auto_dirs(sig_a, sig_b).get("A")
    side = ("現在是往上（做多）" if d == 1 else
            "現在是往下 —— 只做多，所以就算夠快這個候選也會跳過" if d == -1 else
            "現在還判不出方向")
    if need - now > 0:
        msg = ("現在走 %d 點／門檻 %d 點 —— 還差 %d 點；%s（%s 那一刻才算數）"
               % (now, need, need - now, side, SIGNAL_AT))
    else:
        msg = ("現在走 %d 點／門檻 %d 點 —— 目前夠快；%s（%s 那一刻才算數）"
               % (now, need, side, SIGNAL_AT))
    return {"stage": "live", "now_pts": now, "need_pts": need,
            "gap_pts": need - now, "dir": d, "msg": msg}


def _gap_orb(out, snap, sec):
    """開箱：09:05 以前是「箱子畫到現在多寬」，之後是「離上緣還差幾點」。⇒ 一列或 None。"""
    O = out.get("orb") or {}
    if O.get("stage") == "done" or O.get("hist_ok") is False:
        return None                     # 有定論／今天本來就不可用 ⇒ 卡片那一行已經講了
    px = snap.get("px")
    if px is None:
        return None
    if sec < auto_fire.ORB_BOX_TO_MS / 1000.0:
        med = O.get("hist_med_pct")
        hi, lo = _gap_box_now(CURRENT_STATE.get("today"))
        if med is None or hi is None:
            return {"stage": "box", "msg": "箱子還在畫（現在算不出寬度）"}
        w = int(round(hi - lo))
        need = auto_fire.approx_points(med, px)
        tail = "（%s 定案，這是進行中的數字）" % auto_fire.ORB_BOX_TO_AT[:5]
        if need - w > 0:
            msg = "箱子畫到現在 %d 點（%g~%g）／需要 %d 點 —— 還差 %d 點%s" % (
                w, lo, hi, need, need - w, tail)
        else:
            msg = "箱子畫到現在 %d 點（%g~%g）／需要 %d 點 —— 目前夠寬%s" % (
                w, lo, hi, need, tail)
        return {"stage": "box", "now_pts": w, "need_pts": need,
                "gap_pts": need - w, "hi": hi, "lo": lo, "msg": msg}
    if O.get("stage") != "wait" or O.get("hi") is None:
        return None
    # 箱子已經定案、還沒突破 ⇒ 離上緣還差幾點（⛔ 只做多，所以只講上緣）
    gap = int(round(O["hi"] - px))
    if gap > 0:
        msg = ("箱子 %g~%g，現在 %g —— 離上緣還差 %d 點（%s 前往上突破才算；只做多）"
               % (O["lo"], O["hi"], px, gap, O.get("break_by") or ""))
    else:
        msg = ("箱子 %g~%g，現在 %g —— 已經在上緣之上，等它落地判定"
               % (O["lo"], O["hi"], px))
    return {"stage": "wait", "gap_pts": gap, "hi": O["hi"], "lo": O["lo"], "msg": msg}


def _gap_rev(out, snap, sec, day_row):
    """純回馬：09:03:30~09:15 的「要漲過哪個價才算反轉」。⇒ 一列或 None。"""
    if sec < SIGNAL_SEC or sec >= REV_SEC:
        return None                     # 還沒有這個候選／已經判過了
    fast = None
    for c in ((day_row or {}).get("cand_rows") or []):
        if c.get("cand") == "fast":
            fast = c
    if fast is None or fast.get("why") != "not_fast":
        return None                     # 只有 09:03:30 判定「不夠快」的日子才有這個候選
    px0, d = auto_fire._num(fast.get("px")), fast.get("d")
    px = snap.get("px")
    if px0 is None or px is None or d not in (1, -1):
        return None
    if d == 1:
        # 09:03:30 往上 ⇒ 反轉＝往下＝做空，而多方聯軍只做多 ⇒ 今天這個候選做不成。
        # ⛔ 這不是預測，是規則本身（`union_eval` 的 no_long）。
        return {"stage": "live", "dir": d,
                "msg": ("%s 是往上 %g —— 反轉只會變成做空，只做多 ⇒ 這個候選今天做不成"
                        % (SIGNAL_AT, px0))}
    gap = int(round(px0 - px))
    if gap > 0:
        msg = ("要漲過 %g 才算反轉（現在 %g）—— 還差 %d 點（%s 比一次）"
               % (px0, px, gap, REV_AT))
    else:
        msg = ("已經漲過 %g（現在 %g，多 %d 點）—— %s 那一刻還在上面才算反轉"
               % (px0, px, -gap, REV_AT))
    return {"stage": "live", "dir": d, "gap_pts": gap, "ref": px0, "msg": msg}


def fire_gap(out, now=None):
    """
    【今天】那張卡上，三個候選各一行「現在離門檻還有多遠」。**唯讀、零 I/O。**
    ⇒ `{"at", "px", "fast", "orb", "rev"}`；沒有東西可講 ⇒ None（⛔ 前端就不畫那幾行）。

    不端的情形（⛔ 一律留白，不拿舊價／猜的數字充數）：
      ・現在跑的不是多方聯軍（A 沒有三個候選）
      ・還沒到 08:45、或已經過了開箱的突破截止（`ORB_BREAK_BY`，那之後三個都有定論了）
      ・沒有即時報價，或報價超過 `FIRE_GAP_MAX_AGE_MS` 沒更新（斷線、休市）
    """
    if not out.get("union"):
        return None
    now = now or datetime.now()
    sec = now.hour * 3600 + now.minute * 60 + now.second
    if sec < FIRE_GAP_FROM_SEC or sec > auto_fire.ORB_BREAK_BY_MS / 1000.0:
        return None
    st = CURRENT_STATE.get("today")
    if st is None:
        return None
    snap = _auto_snap(st, now)
    age = snap.get("quote_age_ms")
    if snap.get("px") is None or age is None or age > FIRE_GAP_MAX_AGE_MS:
        return None
    day_row = next((r for r in (out.get("days") or [])
                    if r.get("date") == out.get("today")), None)
    g = {"at": now.strftime("%H:%M:%S"), "px": snap.get("px"),
         "fast": _gap_fast(out, snap, sec),
         "orb": _gap_orb(out, snap, sec),
         "rev": _gap_rev(out, snap, sec, day_row)}
    return g if any(g[k] for k in auto_fire.CANDS) else None


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

    - `mode` 只准 `auto_fire.METHODS` 裡那幾個（2026-09-17 起是 **A 快攻回馬槍／U 多方聯軍**），
      ⛔ **先驗再寫**（不准寫進檔案再回頭驗 —— 驗失敗那一瞬間開關就是開著的）。
    - 寫檔用 `O_CREAT|O_EXCL` ⇒ **結構上不可能蓋掉他已經有的那個檔**
      （已經開著再按 ⇒ 409，⛔ 不是換做法）。而且這一道連「兩個視窗同時按」
      那種競態都擋得住（不是「先 exists() 再寫」那種查完再做）。
    - 內容就是一個 ASCII 大寫字母，⛔ 不加 BOM、不加換行 ——
      跟 `auto_fire._decode_flag()` 讀的那條路對齊（那邊 `strip().upper()`）。
    """
    if not isinstance(mode, str) or mode not in auto_fire.METHODS:
        # ⛔ 2026-09-15 起 B 不支援。送 B 來的（舊前端／快取住的舊頁面）要講清楚是規則換了。
        if mode == "B":
            return 400, {"ok": False, "msg": auto_fire.MSG_ONLY_A}
        # ⚠️ 2026-09-17：可以選的做法不只一個了 ⇒ 這句話一律從 auto_fire.MSG_METHODS 來
        #    （⛔ 不准在這裡寫死「只能用 A」—— 那會讓新做法看起來像壞掉）。
        return 400, {"ok": False, "msg": auto_fire.MSG_METHODS}
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


# ══ ⭐⭐ 【夜盤自動下單】的開關（2026-09-23 Benson 交辦：補一顆畫面上的開關）══════
#
# ⛔⛔ **建檔只准寫在這裡**（跟日盤 `fire_arm_on()` 同一條規矩）：
#    `night_fire.py` 對 `ARM_FLAG` **只准** exists／read_bytes／replace／with_name
#    —— `test_night_fire.py` ⑦ 用 AST 在守。**會送單的那個模組打不開自己的開關。**
# ⛔ 關（`night_fire.disarm()`）是**改名不刪**：內容留著、檔名就是幾點關的。

# ⭐ 夜盤每一條做法的**附加揭露**（2026-09-23）。⛔ 這裡只放「畫面要多講的那幾樣」——
#   規則那句話的正本在 `night_fire.state()["rule"]`，代號與名字的正本在 `night_fire.METHODS`。
#   ⛔⛔ key 一定要跟 `night_fire.METHODS` 對得起來；這裡多列一條**不會**讓它出現在畫面上
#      （畫面跑的是 `night_fire.METHODS` 那一份）—— 這是刻意的：接不上的做法按不下去。
#   ⚠️ `risk`（這一條跟一般做法不一樣的出場／停損安排）是**畫面上一定要講兩次**的那一種
#      （確認條 ＋ 開著時的 `.al-risk`），⛔ 不可以只靠規則那句話裡有沒有提到。
#   ⭐ 2026-09-23 R 接上送單：原本設計「不設停利也不設停損」，Benson 當天拍板改成
#      **不設停利＋2% 保護停損** ⇒ `no_sl` 那個旗子拿掉，改成照實講的 `risk`。
NIGHT_METHOD_INFO = {
    "T": {},
    "R": {"beta": "尚未通過前瞻驗證",
          "note": "2024-07 起那段每筆 +122 點，但 2020-08~2024-06 那段每筆 −2.6 點。"
                  "目前在【模擬】那一頁考前瞻。",
          # ⛔ 用 **粗體** 標記、前端走 emb()（先 esc 再換 <b>）；⛔ 不准直接塞 HTML。
          "risk": ("這一條**不設停利**（券商端一張單都沒有），只有 **%g%% 保護停損**"
                   "（活在這台面板裡），其餘抱到 **04:58** 才平。"
                   "【模擬】那一條照研究不設停損，兩邊這一點不一樣。"
                   % (night_fire.R_SL_PCT * 100))},
}


def night_methods(rules=None):
    """
    夜盤畫面上那組**單選**的清單。⛔ 只有一個地方組（產品的 GET 與治具都叫這一支）——
    治具另寫一份的話，探針量到的是治具的樣子。
    `rules`：`night_fire.state()["rules"]`（每一條自己那句話）；舊呼叫端傳字串 ⇒ 每條都用它。
    """
    def _r(k):
        if isinstance(rules, dict):
            return rules.get(k) or ""
        return rules or ""
    return [dict({"k": k, "name": night_fire.METHOD_NAME.get(k, k), "rule": _r(k)},
                 **NIGHT_METHOD_INFO.get(k, {}))
            for k in night_fire.METHODS]


def night_arm_confirm(live, mode=None):
    """
    夜盤兩段式確認**第二段那句話的正本**（⛔ 前端不准自己算「現在是不是真錢」）。
    ⚠️ `live` 由呼叫端傳進來（就是 `night_fire.state()` 算的那一個 `broker.is_live()`），
       ⛔ 這裡不再問第二次 —— 同一件事兩把尺一定有一把是錯的。
    ⚠️ 這裡**不講「第一次是今天還是明天」**：夜盤那一刻（美股開盤）算不算「今晚」
       牽涉到夏令時間與 15:00 的分界，⛔ 猜一句比不講更糟 ——
       畫面上改寫「每個交易日的晚上」，而「今晚幾點看」由 `state()["tonight"]` 照實講。
    """
    mode = mode if mode in night_fire.METHODS else night_fire.METHODS[0]
    name = night_fire.METHOD_NAME[mode]
    if live:
        return {"live": True, "mode": mode, "method_name": name,
                "text": ("現在是**真實下單模式**。打開之後，程式每個交易日的晚上會"
                         "**用你的錢**照「%s」送單。" % name)}
    return {"live": False, "mode": mode, "method_name": name,
            "text": ("現在是演練模式，每個交易日的晚上會照「%s」跑完整條路，"
                     "但不會真的送單。" % name)}


def night_arm_on(mode, who="panel"):
    """
    ⭐⭐ **整個 repo 唯一一個會建立 `NIGHT_ORDERS_ON` 的地方。**
    回傳 `(http_code, payload)`。⛔ 呼叫端必須先過 `fire_post_guard()`。
    規矩逐條照抄日盤那一顆（⛔ 兩邊不一樣就是兩把尺）：
      - `mode` 只准 `night_fire.METHODS` 裡那幾個，⛔ **先驗再寫**
        （不准寫進檔案再回頭驗 —— 驗失敗那一瞬間開關就是開著的）。
      - 寫檔用 `O_CREAT|O_EXCL` ⇒ **結構上不可能蓋掉他已經有的那個檔**
        （已經開著再按 ⇒ 409，⛔ 不是換做法；連「兩個視窗同時按」都擋得住）。
      - 內容就是一個 ASCII 大寫字母，⛔ 不加 BOM、不加換行（跟 `_decode_flag()` 對齊）。
    """
    if not isinstance(mode, str) or mode not in night_fire.METHODS:
        return 400, {"ok": False,
                     "msg": "做法只認得 %s（%s）" % (
                         "／".join(night_fire.METHODS),
                         "／".join(night_fire.METHOD_NAME[k] for k in night_fire.METHODS))}
    flag = night_fire.ARM_FLAG
    live = broker.is_live()
    try:
        flag.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(flag), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return 409, {"ok": False, "armed": night_fire.arm()["on"],
                     "msg": "已經開著了，要換做法請先關掉"}
    except Exception as e:
        return 500, {"ok": False, "msg": "打不開：" + str(e)[:150]}
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(mode.encode("ascii"))
            f.flush()
    except Exception as e:
        # 檔案建出來但內容沒寫成 ⇒ 那是個「看不懂的開關檔」（night_fire 會拒絕下單），
        # ⛔ 但不可以留著讓他以為開好了：走產品自己的 disarm() 收乾淨。
        try:
            night_fire.disarm()
        except Exception:
            pass
        return 500, {"ok": False, "msg": "寫不進去：" + str(e)[:150]}
    a = night_fire.arm()
    # ⛔ 落地一列（跟日盤同一個格式、同一個資料夾規矩：⛔ 不塞進 nightfire/YYYY-MM.jsonl
    #    —— 那個檔的 rec 只認得 skip／result／eod，多一種會讓既有的統計看成壞資料）。
    warn = None
    try:
        night_fire.NF_DIR.mkdir(parents=True, exist_ok=True)
        p = night_fire.NF_DIR / ("arm-" + str(date.today())[:7] + ".jsonl")
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"rec": "arm", "date": str(date.today()),
                                "at": datetime.now().isoformat(timespec="seconds"),
                                "method": mode, "live": live, "who": str(who)[:60],
                                "src": "panel", "flag": flag.name,
                                "armed": a["on"]}, ensure_ascii=False) + "\n")
            f.flush()
    except Exception as e:
        warn = "開關打開了，但這一筆沒有記錄下來：" + str(e)[:120]
        print("⚠️ [夜盤自動下單] " + warn, flush=True)
    print("[夜盤自動下單] 從面板打開：%s（%s）" % (
        night_fire.METHOD_NAME.get(mode, mode),
        "真實下單模式" if live else "演練模式"), flush=True)
    return 200, {"ok": True, "armed": a["on"], "method": a["method"],
                 "live": live, "warn": warn,
                 "msg": a["msg"] if a["on"] else (a["msg"] or "開關建好了")}


# ---------------------------------------------------------------- 風控規則 B
# ⭐ 2026-09-24 Benson 拍板：當月自動單真單虧到 800 點（每口）⇒ 當月日盤、夜盤都不送；
#    他**可以手動解除**（兩段式），解除只對那個月有效。規則本體在 `risk_cap.py`（唯讀）。
#    ⛔ 這裡只做兩件事：① 給畫面看的那一份（快取）；② 建解除檔（整個 repo 只有這裡會建）。
RISK_TTL = 20.0                         # 秒。/api/fire/state 每 5 秒被問一次，⛔ 不要每次都讀帳本
RISK_LOG_DIR = HERE / "riskcap"         # ⛔ gitignore（解除紀錄）
_RISK = {"at": 0.0, "v": None}


def risk_view(force=False):
    """⇒ 這個月的風控狀態（`risk_cap.state()`，20 秒快取）。⛔ 算不出來也回一份（帶 err）。"""
    now = time.time()
    if not force and _RISK["v"] is not None and now - _RISK["at"] < RISK_TTL:
        return _RISK["v"]
    try:
        v = risk_cap.state(qty=broker.QTY)
    except Exception as e:
        v = {"err": "風控算不出來：%s" % str(e)[:120], "blocked": True,
             "msg": "風控算不出本月損益 —— 不猜，這段時間不送"}
    _RISK.update(at=now, v=v)
    return v


def risk_override_on(who="panel"):
    """
    ⭐⭐ **整個 repo 唯一一個會建立 `RISK_OVERRIDE` 的地方。** ⇒ (http_code, payload)。
    ⛔ 呼叫端必須先過 `fire_post_guard()`（前端是兩段式）。
      - ⛔ 只有「這個月真的到了上限」才准解除（沒到上限按了 ⇒ 409，⛔ 不預先解除）。
      - 內容 ＝ 這個月 `YYYY-MM`（ASCII、不加換行）⇒ 下個月自動失效，不用記得收。
      - 舊的解除檔（上個月的）先**改名**收起來再建（⛔ 不刪、⛔ 不覆蓋）；
        寫檔用 `O_CREAT|O_EXCL`（兩個視窗同時按也只有一個會成功）。
    """
    s = risk_view(force=True)
    month = str(date.today())[:7]
    if s.get("err"):
        return 409, {"ok": False, "msg": "風控現在算不出本月損益，不能解除：" + str(s["err"])}
    if not s.get("hit"):
        return 409, {"ok": False, "msg": "這個月還沒到上限，不需要解除"}
    if s.get("override"):
        return 409, {"ok": False, "msg": "這個月已經解除過了"}
    flag = risk_cap.OVERRIDE_FLAG
    try:
        if flag.exists():
            flag.replace(flag.with_name(flag.name + ".old-" + datetime.now().strftime("%Y%m%d-%H%M%S")))
        fd = os.open(str(flag), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return 409, {"ok": False, "msg": "剛剛已經有另一個視窗解除了"}
    except Exception as e:
        return 500, {"ok": False, "msg": "解除不了：" + str(e)[:150]}
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(month.encode("ascii"))
            f.flush()
    except Exception as e:
        return 500, {"ok": False, "msg": "寫不進去：" + str(e)[:150]}
    warn = None
    try:
        RISK_LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (RISK_LOG_DIR / ("override-" + month + ".jsonl")).open("a", encoding="utf-8") as f:
            f.write(json.dumps({"rec": "override", "month": month,
                                "at": datetime.now().isoformat(timespec="seconds"),
                                "pnl": s.get("pnl"), "cap": s.get("cap"),
                                "who": str(who)[:60]}, ensure_ascii=False) + "\n")
    except Exception as e:
        warn = "解除了，但這一筆沒有記錄下來：" + str(e)[:120]
    s = risk_view(force=True)
    print("[風控] %s 手動解除（本月 %s 點、上限 %s）" % (month, s.get("pnl"), s.get("cap")), flush=True)
    return 200, {"ok": True, "warn": warn, "risk": s,
                 "msg": "已解除：這個月剩下的日子照常送單，下個月自動恢復規則"}


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
/* ══ 2026-09-24【交易分析師】右上角信件（class 一律 an- 前綴）══
   ⛔ 未讀用金色（跟開關同一個「要注意」語彙）；⛔ 不准用紅綠（紅綠只給損益）。 */
.an-tr{display:flex; align-items:center; gap:14px}
.an-mail{position:relative; width:38px; height:38px; border-radius:11px; border:1px solid var(--line);
  background:var(--surface-2); color:var(--dim); display:grid; place-items:center; cursor:pointer; flex:none; padding:0}
.an-mail:hover{color:var(--text)}
.an-mail.has{color:var(--gold); border-color:var(--gold-line)}
.an-mail svg{width:19px; height:19px}
.an-mail .bdg{position:absolute; top:-6px; right:-6px; min-width:18px; height:18px; border-radius:9px;
  background:var(--gold); color:#1a1307; font:700 10.5px var(--font-mono); display:grid; place-items:center;
  padding:0 5px; box-shadow:0 0 0 3px var(--bg)}
.an-pop{position:fixed; z-index:60; width:min(400px,calc(100vw - 24px)); max-height:70vh; overflow:auto;
  background:var(--raise); border:1px solid var(--line); border-radius:16px; padding:12px;
  box-shadow:0 18px 50px rgba(0,0,0,.55)}
.an-ih{display:flex; justify-content:space-between; align-items:baseline; padding:2px 4px 10px}
.an-ih b{font-size:14.5px} .an-ih span{font-size:11.5px; color:var(--faint)}
.an-item{display:flex; gap:10px; width:100%; text-align:left; background:var(--surface); color:var(--text);
  border:1px solid var(--line-soft); border-radius:12px; padding:10px 12px; cursor:pointer;
  position:relative; overflow:hidden; font-family:var(--font-sans)}
.an-item+.an-item{margin-top:7px}
.an-item .dot{width:8px; height:8px; border-radius:50%; margin-top:6px; flex:none}
.an-item .bd{flex:1; min-width:0}
.an-item .r1{display:flex; justify-content:space-between; gap:8px; font-size:13.5px; color:var(--dim)}
.an-item .r1 small{font-family:var(--font-mono); font-size:11px; color:var(--faint)}
.an-item .ln{font-size:12px; color:var(--faint); margin-top:3px; line-height:1.55}
.an-item .ch{display:flex; gap:6px; margin-top:6px; flex-wrap:wrap}
.an-item.unread{background:linear-gradient(90deg,rgba(227,169,81,.10),var(--surface) 55%); border-color:var(--gold-line)}
.an-item.unread::before{content:''; position:absolute; left:0; top:0; bottom:0; width:3px; background:var(--gold)}
.an-item.unread .dot{background:var(--gold); box-shadow:0 0 0 3px var(--gold-soft)}
.an-item.unread .r1{color:var(--text); font-weight:700}
.an-item.unread .ln{color:var(--dim)}
.an-new{font-size:10px; font-weight:700; color:#1a1307; background:var(--gold); border-radius:4px; padding:0 5px}
.an-lamp{display:inline-block; flex:none; white-space:nowrap; font-size:10.5px; font-weight:700; border-radius:99px; padding:1px 9px;
  color:var(--dim); background:var(--surface-2); border:1px solid var(--line)}
.an-lamp.wn,.an-lamp.bd{color:var(--gold); background:var(--gold-soft); border-color:var(--gold-line)}
.an-tag{display:inline-block; font-size:10.5px; font-weight:650; border-radius:5px; padding:0 6px; white-space:nowrap}
.an-tag.data{color:var(--gold); background:var(--gold-soft); border:1px solid var(--gold-line)}
.an-tag.judge{color:var(--dim); border:1px dashed var(--faint)}
.an-empty{font-size:12.5px; color:var(--faint); padding:14px 4px}
.an-sheet{position:fixed; inset:0; z-index:70; background:rgba(8,10,14,.72); overflow:auto; padding:24px 16px}
.an-doc{max-width:1240px; margin:0 auto; background:var(--bg); border:1px solid var(--line); border-radius:18px;
  padding:18px 20px 22px}
.an-dh{display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; margin-bottom:14px}
.an-dh .t{font-size:12.5px; color:var(--faint)} .an-dh .t b{font-size:16px; color:var(--text); margin-right:8px}
.an-dh button{border:1px solid var(--line); background:var(--surface-2); color:var(--dim); border-radius:10px;
  padding:7px 14px; font-size:13px; cursor:pointer; font-family:var(--font-sans)}
.an-verdict{display:flex; gap:12px; align-items:flex-start; background:linear-gradient(180deg,var(--raise),#161C24);
  border:1px solid var(--line); border-radius:16px; padding:14px 16px; margin-bottom:16px}
.an-verdict p{font-size:16px; font-weight:650; line-height:1.55; margin:0}
.an-cols{display:grid; grid-template-columns:minmax(0,1fr) 360px; gap:18px; align-items:start}
@media(max-width:1100px){ .an-cols{grid-template-columns:minmax(0,1fr)} }
.an-stack{display:flex; flex-direction:column; gap:16px}
.an-card{background:var(--surface); border:1px solid var(--line-soft); border-radius:16px; padding:14px 16px}
.an-nw{display:flex; flex-direction:column; gap:5px; padding:11px 0}
.an-nw:first-child{padding-top:0} .an-nw:last-child{padding-bottom:0}
.an-nw+.an-nw{border-top:1px solid var(--line-soft)}
.an-nw .top{display:flex; gap:8px; align-items:center; flex-wrap:wrap; font-family:var(--font-mono); font-size:11.5px; color:var(--faint)}
.an-nw h3{font-size:14.5px; font-weight:650; line-height:1.45; margin:0}
.an-nw p{font-size:12.5px; color:var(--dim); margin:0; line-height:1.65}
.an-imp{background:var(--surface-2); border:1px solid var(--line-soft); border-radius:10px; padding:7px 10px;
  display:grid; grid-template-columns:auto 1fr; gap:3px 12px; font-size:12px}
.an-imp b{white-space:nowrap} .an-imp span{color:var(--dim)}
.an-src{font-size:11.5px; color:var(--faint)} .an-src a{color:var(--dim)}
.an-rows .r{display:flex; justify-content:space-between; gap:12px; font-size:12.5px; padding:6px 0}
.an-rows .r+.r{border-top:1px solid var(--line-soft)}
.an-rows .r .k{color:var(--faint); white-space:nowrap} .an-rows .r .v{text-align:right}
.an-rows .r .v small{display:block; color:var(--faint); font-size:11px}
.an-rail{display:grid; grid-template-columns:repeat(3,1fr); gap:8px; margin:6px 0}
.an-rail .k{font-size:10.5px; color:var(--faint)} .an-rail .v{font-size:14.5px; font-weight:650; font-family:var(--font-mono)}
.an-st+.an-st{border-top:1px solid var(--line-soft); padding-top:12px; margin-top:12px}
.an-st .hd{display:flex; justify-content:space-between; align-items:center}
.an-st .hd b{font-size:14px} .an-note{font-size:11.5px; color:var(--faint); line-height:1.6}
.an-pos{height:3px; border-radius:2px; background:var(--surface-2); margin:7px 0 3px; position:relative}
.an-pos i{position:absolute; top:-4px; width:3px; height:11px; border-radius:2px; background:var(--gold)}
.an-bar{height:5px; border-radius:3px; background:var(--surface-2); overflow:hidden; margin-top:4px}
.an-bar i{display:block; height:100%; background:var(--gold); opacity:.8}
.an-rec{background:var(--surface-2); border:1px solid var(--line-soft); border-radius:12px; padding:11px 13px;
  display:flex; flex-direction:column; gap:6px}
.an-rec+.an-rec{margin-top:9px}
.an-rec h3{font-size:14px; margin:0} .an-rec p{font-size:12.5px; color:var(--dim); margin:0; line-height:1.65}
.an-rec .ask{font-size:12.5px; color:var(--gold); border-top:1px dashed var(--line); padding-top:6px}
.an-foot{font-size:11px; color:var(--faint); text-align:center; margin-top:16px}
.an-totop{text-align:center; margin-top:14px}
.an-totop button{border:1px solid var(--line); background:var(--surface-2); color:var(--dim); border-radius:10px;
  padding:9px 18px; font-size:13px; cursor:pointer; font-family:var(--font-sans)}
.an-totop button:hover{color:var(--text)}
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
   （2026-09-16 前回顧分頁的 #rhead 也是同一套 qblock；那一頁拿掉了，規則不變。） */
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
#acct:empty{display:none}
/* 【帳戶】自己一頁（2026-09-21）：一張卡不要拉成整個螢幕寬，字會散掉 */
#tab-acct{max-width:560px}

/* ── 【帳戶總覽】（2026-09-21）：券商端的錢。
   ⛔ 紅綠只給「賺賠」那一個數字（跟這一頁其他地方同一條規矩）；
   ⛔ 金額一律等寬數字（每分鐘會換一次，不等寬整張卡會抖）。 */
.ac{border:1px solid var(--line); border-radius:var(--r-md); background:var(--surface);
  padding:13px 14px 12px}
.ac .hd{display:flex; align-items:baseline; justify-content:space-between; gap:8px}
.ac .hd .t{font-size:13px; font-weight:700; color:var(--text)}
.ac .hd .at{font-size:10.5px; color:var(--faint); font-family:var(--font-mono)}
.ac .big{font-size:23px; font-weight:700; color:var(--text); line-height:1.25; margin-top:6px;
  font-family:var(--font-mono); font-variant-numeric:tabular-nums}
.ac .sub{font-size:11.5px; color:var(--dim); line-height:1.65; margin-top:3px;
  font-family:var(--font-mono); font-variant-numeric:tabular-nums}
.ac .sub .up{color:var(--up)} .ac .sub .down{color:var(--down)}
.ac .line{margin-top:8px; padding-top:8px; border-top:1px solid var(--line-soft);
  font-size:11.5px; color:var(--dim); line-height:1.6}
.ac .warn{color:var(--gold); font-weight:650}
.ac .miss{font-size:12px; color:var(--faint); line-height:1.6; margin-top:5px}
.ac .spark{margin-top:9px}
.ac .spark svg{display:block; width:100%; height:42px}
.ac .spark .cap{font-size:10.5px; color:var(--faint); margin-top:3px}

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
/* 2026-09-14 減字：副標＋第二顆藥丸併成標頭右邊一行小字（「微台 1 口・±N 點」） */
.n-hd .meta{font-size:11.5px; color:var(--faint); font-family:var(--font-mono); white-space:nowrap}
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

/* ================= 分頁列 ＋ 心得編輯（跨分頁共用；⛔ 不另立一套顏色） ================= */
/* ⚠️ 2026-09-16【回顧】整頁拿掉，它專屬的 CSS（#tab-review／#rpane／.rp*／.tfsw／.dt*／.cmp／.verdict／.tally／.chips／.kbd／.hr／.trade.sel／.tr-note／.cday／.ctag／.daysel／.jinput／.hold／.btn.gw）一起刪掉。
   ⛔ 留下來的 .tabs／.noteline／.nedit／.empty／.btn.gold 是**即時分頁也在用**的（心得編輯就在那裡）。 */
[hidden]{display:none !important}
.tabs{display:flex; gap:3px; background:var(--surface-2); border-radius:12px; padding:3px;
  border:1px solid var(--line-soft)}
.tabs button{border:0; background:transparent; color:var(--dim); cursor:pointer; min-width:100px;
  font-family:var(--font-sans); font-size:13.5px; font-weight:650; padding:8px 18px; border-radius:9px;
  transition:color .15s var(--ease), background .15s var(--ease)}
.tabs button:hover{color:var(--text)}
.tabs button.on{background:var(--gold-soft); color:var(--gold)}
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
/* 【自動下單】開著＋真單也開著時副標整行併進標題（2026-09-14），空的就不要佔那 5px */
.at-title .s:empty{display:none}
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
/* 2026-09-14「證得出來嗎」那一欄拿掉之後最後一欄是數字（每筆）⇒ 靠右，不再靠左 */
.at-tbl th:last-child,.at-tbl td:last-child{padding-right:2px}
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
/* 2026-09-14 搬到表頭底下（免責只講一次：不算勝率／天花板／有沒有超過 全在這一行） */
.at-ceil{margin:-2px 2px 10px; font-size:11.5px; color:var(--dim); line-height:1.6;
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
/* ⭐⭐ 風險條（2026-09-10 取代整段〈怎麼開〉的教學）。
   ⛔ 這一段是**砍剩下來的兩句**，⛔ 一句都不准再刪：
     ① 停損活在這台電腦的面板迴圈裡（面板關掉／當掉／電腦睡著就沒有停損，
        13:43:30 的自動平倉也不會發生）
     ② 這個開關**沒有有效期**（每個交易日都會送，直到他自己關掉）
   ⛔ 用**金色**（這個面板既有的「注意」語彙），⛔ 不用紅綠 —— 紅綠只給損益，
      唯一的例外是兩段式確認條的「真錢」那一版。
   ⚠️ 2026-09-14 降層級（lab-ux 定案 C、Benson 拍板）：兩句**一字不刪**，只把整段金字改成
      灰字、金色只留左緣與圖示、關鍵字白 —— 整段塗金反而讓真正要看的兩個關鍵字
      （沒有停損／每個交易日都會送）失去對比。⛔ 仍然不用紅綠。 */
.al-risk{margin-top:12px; padding:10px 14px 11px 13px; border-radius:var(--r-md);
  background:var(--surface-2); border:1px solid var(--line-soft); border-left:3px solid var(--gold)}
.al-risk p{display:flex; gap:9px; font-size:12px; line-height:1.65; color:var(--dim); margin:0}
.al-risk p+p{margin-top:4px}
.al-risk i{font-style:normal; color:var(--gold); flex:none}
.al-risk b{color:var(--text); font-weight:700}
.al-gates{display:grid; grid-template-columns:repeat(3,1fr); gap:1px;
  background:var(--line-soft); border-radius:var(--r-md); overflow:hidden; margin-top:11px}
.al-gates .c{background:var(--surface); padding:9px 12px 10px; min-width:0}
.al-gates .k{font-size:10.5px; color:var(--faint); letter-spacing:.4px;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
.al-gates .v{font-size:14px; font-weight:650; color:var(--text); margin-top:3px;
  line-height:1.3; white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
.al-gates .v.off{color:var(--faint); font-weight:600}
.al-fast{margin-top:10px; font-size:12px; line-height:1.7; color:var(--dim)}
.al-fast:empty{display:none}
.al-fast .k{font-size:10.5px; color:var(--faint); letter-spacing:.4px; margin-right:8px}
.al-fast b{color:var(--text); font-weight:700}
.al-fast .warn{color:var(--gold)}
/* ⭐ 2026-09-16 多方聯軍：今天三個候選各一列（⛔ 沿用這一頁既有的灰階，紅綠只給損益） */
.al-cands{margin-top:11px; border:1px solid var(--line); border-radius:var(--r-md); overflow:hidden}
.al-cands .hd{font-size:10.5px; color:var(--faint); letter-spacing:.4px;
  padding:7px 12px 6px; background:var(--surface-2); border-bottom:1px solid var(--line)}
.al-cand{display:flex; gap:10px; align-items:baseline; padding:7px 12px;
  font-size:12px; line-height:1.6; color:var(--dim); background:var(--surface)}
.al-cand+.al-cand{border-top:1px solid var(--line)}
.al-cand .k{flex:none; width:52px; font-weight:700; color:var(--text)}
.al-cand .w{min-width:0; flex:1}
.al-cand.gold .w{color:var(--gold); font-weight:650}
/* ⭐ 2026-09-21「現在還差幾點」：接在候選那一行下面的第二行。
   ⛔ 用等寬數字（數字每 5 秒會跳，不等寬會整行左右抖）、⛔ 不用紅綠
   （這一頁的紅綠只給損益，見 .at-today 那一段的同一條規矩）。 */
.al-cand .gap{display:block; margin-top:3px; font-size:11.5px; color:var(--faint);
  font-family:var(--font-mono); font-variant-numeric:tabular-nums; line-height:1.55}
.al-today{margin-top:2px}
.al-today .t{font-size:17px; font-weight:700; color:var(--text); line-height:1.35}
.al-today .t.off{font-size:15px; color:var(--dim); font-weight:650}
.al-today .d{font-size:12px; color:var(--faint); line-height:1.7; margin-top:5px}
.al-today .d b{color:var(--dim); font-weight:650; font-family:var(--font-mono)}
/* ⭐⭐ 紀錄清單（2026-09-10 從表格改成卡片）。
   ⛔⛔ **照抄練習 `row(t,ns)` 與真實 `realCard(t)` 那一份 `.trade`，⛔ 不要重新設計。**
   （2026-09-03 Benson 退過一次：「真實的交易紀錄要跟練習的交易紀錄的形式長的一樣，
     我不是說過了嗎」——「像 X 一樣」講的是**形式**，換顏色換間距都解不掉。）
   所以這裡**一個 `.trade`／`.tr-*`／`.dir`／`.tag` 的樣式都沒有重寫**，
   只加兩條這一頁獨有的：
     ・`.al-list`：限寬 640px 靠左 —— 全寬 1395px 時 `.tr-px` 會把點數推到很遠，
       形式就不像了（`.tr-res` 是靠右的，中間那條 `flex:1` 拉多長就差多遠）。
     ・`.trade .al-meta`：做法／滑價／模擬那邊擠不進 `.tr-px` 的 157.6px
       （`.tag` 4 個字就已經吃掉餘裕），只好另起一行。
       ⛔ 樣式**逐字照抄 `.trade .noteline`**（同一個位置、11.5px、--faint、
       nowrap ＋ ellipsis）—— 那一行在練習／真實的卡片上就是「附註」的位置，
       自己另發明一種樣式就又不一樣了。 */
.al-list{max-width:640px}
/* 「今天」那一塊已出場時併進紀錄第一列（2026-09-14）：金框＋日期欄寫「今天」＋「已出場」標。
   ⛔ 金＝「現在」（跟翻頁列的即時燈同一個意思），⛔ 不是輸贏色。卡片高度不變（tag 仍 ≤4 字）。 */
.trade.tr-today{border-color:var(--gold-line)}
.trade.tr-today .tr-date{color:var(--text); font-weight:650}
.tr-chip{font-size:10px; font-weight:700; color:var(--gold); background:var(--gold-soft);
  border-radius:5px; padding:1px 6px; letter-spacing:.5px; white-space:nowrap}
.trade .al-meta{white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
  background:none; border:0; padding:0; margin-top:5px; font-size:11.5px;
  color:var(--faint); line-height:1.5; font-family:var(--font-mono)}
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
/* ⭐ 2026-09-24 風控規則 B（#alcap）。⛔ 用金色／中性灰，⛔ 不准用紅綠（紅綠只給損益）。 */
.al-cap{margin-top:12px}
.al-cap:empty{display:none}
.al-cap .cap{border:1px solid var(--line-soft); background:var(--surface-2);
  border-radius:var(--r-md); padding:10px 14px 11px}
.al-cap .cap.hit{border-color:var(--gold-line); background:var(--gold-soft)}
.al-cap .hd{display:flex; justify-content:space-between; align-items:baseline; gap:10px; flex-wrap:wrap}
.al-cap .k{font-size:10.5px; color:var(--faint); letter-spacing:.4px}
.al-cap .v{font-size:13.5px; font-weight:650; font-family:var(--font-mono);
  font-variant-numeric:tabular-nums}
.al-cap .cap.hit .v,.al-cap .cap.hit .t{color:var(--gold)}
.al-cap .bar{height:4px; border-radius:2px; background:var(--bg); margin:8px 0 6px; overflow:hidden}
.al-cap .bar i{display:block; height:100%; border-radius:2px; background:var(--dim)}
.al-cap .cap.hit .bar i{background:var(--gold)}
.al-cap .t{font-size:12px; color:var(--dim); line-height:1.7}
.al-cap .n{font-size:11px; color:var(--faint); line-height:1.7}
.al-cap .al-conf{margin-top:10px}
.al-cap .rbtn{margin-top:9px}
.al-cap .rbtn .btn{padding:8px 14px; font-size:13px; background:transparent;
  color:var(--gold); border-color:var(--gold-line)}
.al-cap .err{font-size:12px; color:var(--gold); margin-top:6px}
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
/* ══ 【模擬】分頁（class 一律 sm- 開頭，⛔ 不借別頁的名字；2026-09-16 從 #tab-lab 搬過來，class 名一個都沒改）══
   ⛔ 紅漲綠跌：沿用全站的 .up／.down 變數，這一頁不另立顏色。
   ⚠️ **並排**是 Benson 指定的（一眼比得出來）：2026-09-16 第七條「多方聯軍」加進來之後，
      1536 寬時七欄各約 194px ⇒ 每一條都是很窄的直欄，⛔ 不要在裡面再放兩欄的表格
      （塞不下會自己換行，變成高矮不一的爛版）。視窗窄一點就掉成 4＋3 兩列（PM 定案的退路）。
      每一條由上而下：名字 → **每月累計點數（他最在意的，放第一個）** → 今天 → 最近一筆 → 規則句。
   ⛔ 條數是後端決定的 ⇒ 這裡用 repeat(N) 寫死欄數只是**版面**，不是條數；加減條不必改 JS。 */
#tab-sim .sm-card{padding:14px 16px 12px}
.sm-note{font-size:11.5px; color:var(--faint); margin:-2px 2px 10px}
.sm-lanes{display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:9px}   /* 2026-09-22 起畫面只剩兩條（sim_lanes.SHOWN_LANES） */
@media(max-width:640px){ .sm-lanes{grid-template-columns:minmax(0,1fr)} }
.sm-lane{border:1px solid var(--line-soft); border-radius:var(--r-md); padding:10px 11px 9px; min-width:0}
.sm-lt{display:flex; flex-direction:column; gap:1px}
.sm-lt b{font-size:14px; color:var(--text); font-weight:650; line-height:1.3; overflow-wrap:anywhere}
.sm-lt small{font-size:10.5px; color:var(--faint)}
.sm-mh,.sm-lh{font-size:10.5px; color:var(--faint); letter-spacing:1px; margin:9px 0 4px}
.sm-months{display:flex; flex-direction:column; gap:2px}
.sm-months>*{flex:none}
.sm-m{display:flex; justify-content:space-between; gap:6px; font-size:12px; padding:3px 5px; border-radius:var(--r-sm)}
.sm-m.this{background:var(--surface-2)}
.sm-m span{color:var(--dim)} .sm-m.this span{color:var(--gold)}
.sm-m i{font-style:normal; font-size:10px; color:var(--faint); margin-left:3px}
.sm-today{font-size:11.5px; color:var(--dim); line-height:1.5; overflow-wrap:anywhere}
.sm-today em{font-style:normal; color:var(--faint); margin-right:4px}
.sm-pend{font-size:10.5px; color:var(--faint); margin-top:6px; line-height:1.5; overflow-wrap:anywhere}
.sm-empty{font-size:11.5px; color:var(--faint); padding:4px 0}
.sm-foot{font-size:11px; color:var(--faint); margin-top:10px}
.sm-foot.bad{color:var(--down)}
/* ⭐ 2026-09-17：卡上的「最近一筆」與規則句收掉（Benson 要的），改成整張卡可以點進去。
   ⛔ 不做彈出視窗：同一個位置把七條整個換成那一條的內頁 —— 窄視窗不會被遮住，
      Esc 或「← 全部策略」回得去。 */
.sm-lane{cursor:pointer; transition:border-color .12s ease, background .12s ease}
.sm-lane:hover{border-color:var(--ghost); background:var(--surface-2)}
.sm-lane:focus-visible{outline:2px solid var(--gold-line); outline-offset:2px}
.sm-more{font-size:10.5px; color:var(--faint); margin-top:9px;
  border-top:1px solid var(--line-soft); padding-top:7px}
.sm-lane:hover .sm-more{color:var(--gold)}
/* 內頁 */
.sm-dhead{display:flex; align-items:baseline; gap:12px; flex-wrap:wrap; margin-bottom:4px}
.sm-dhead h2{font-size:19px; font-weight:650; color:var(--text); margin:0}
.sm-dhead small{font-size:11.5px; color:var(--faint)}
.sm-back{font-size:12px; color:var(--dim); border:1px solid var(--line); border-radius:var(--r-sm);
  padding:5px 10px; cursor:pointer; user-select:none}
.sm-back:hover{background:var(--surface-2); color:var(--text)}
.sm-plain{font-size:14.5px; color:var(--text); line-height:1.75; margin:12px 0 16px;
  padding:12px 14px; background:var(--surface-2); border-radius:var(--r-md);
  border-left:3px solid var(--gold-line)}
.sm-steps{display:grid; grid-template-columns:max-content minmax(0,1fr); gap:8px 16px;
  font-size:12.5px; line-height:1.7; margin:0}
.sm-steps dt{color:var(--faint); white-space:nowrap}
.sm-steps dd{margin:0; color:var(--dim); overflow-wrap:anywhere}
.sm-steps dd b{color:var(--text); font-weight:650}
@media(max-width:640px){ .sm-steps{grid-template-columns:minmax(0,1fr); gap:1px 0}
  .sm-steps dd{margin-bottom:9px} }
.sm-tot{font-size:12.5px; color:var(--dim); line-height:1.7; margin:14px 0 2px;
  padding:10px 12px; border:1px solid var(--line-soft); border-radius:var(--r-md)}
.sm-tot b{font-family:var(--font-mono); font-variant-numeric:tabular-nums; color:var(--text)}
.sm-cols{display:grid; grid-template-columns:250px minmax(0,1fr); gap:18px; align-items:start}
@media(max-width:900px){ .sm-cols{grid-template-columns:minmax(0,1fr)} }
.sm-scroll{max-height:56vh; overflow:auto; border:1px solid var(--line-soft); border-radius:var(--r-md)}
.sm-tbl{width:100%; border-collapse:collapse; font-size:12px}
.sm-tbl th{position:sticky; top:0; z-index:1; background:var(--surface); text-align:left;
  font-weight:500; color:var(--faint); font-size:10.5px; letter-spacing:1px;
  padding:7px 9px; border-bottom:1px solid var(--line)}
.sm-tbl td{padding:6px 9px; border-bottom:1px solid var(--line-soft); vertical-align:top}
.sm-tbl tr:last-child td{border-bottom:0}
.sm-tbl .d{font-family:var(--font-mono); color:var(--dim); white-space:nowrap}
.sm-tbl .k{font-weight:650; white-space:nowrap} .sm-tbl .k.none{color:var(--faint); font-weight:500}
.sm-tbl .x{font-family:var(--font-mono); color:var(--dim); white-space:nowrap}
.sm-tbl .p{font-family:var(--font-mono); font-variant-numeric:tabular-nums; text-align:right; white-space:nowrap}
.sm-tbl .why{color:var(--faint); font-size:11px; line-height:1.5; overflow-wrap:anywhere}
.sm-tbl .src{color:var(--faint); font-size:10.5px; white-space:nowrap}
.sm-tbl .src.bf{color:var(--ghost)}
/* 月表點下去跳到那個月（⚠️ 只有內頁那張有 .hit；卡上那張刻意不可點） */
.sm-m.hit{cursor:pointer}
.sm-m.hit:hover{background:var(--raise)}
.sm-m.hit:focus-visible{outline:2px solid var(--gold-line); outline-offset:1px}
.sm-m.on{box-shadow:inset 3px 0 0 var(--gold)}
.sm-tbl tr.hl td{background:var(--gold-soft)}
/* ⭐ 點一天看圖（2026-09-18）：逐日紀錄每一列都點得下去 ⇒ 跳出那天的 1 分 K ＋ 進出場 */
.sm-tbl tbody tr[data-d]{cursor:pointer}
.sm-tbl tbody tr[data-d]:hover td{background:var(--raise)}
.sm-tbl tbody tr.cur td{background:var(--surface-2); box-shadow:inset 0 -1px 0 var(--gold-line)}
/* ⚠️ 靠上對齊、⛔ 不要垂直置中：每天內容高度不同（沒交易的日子沒有圖例／說明），
   置中的話整個浮層會上下跳 ⇒ 連點「後一天」第二下就點空（2026-09-18 實測踩到）。 */
.sm-day{position:fixed; inset:0; z-index:80; background:rgba(8,10,14,.72);
  display:flex; align-items:flex-start; justify-content:center; padding:4vh 24px 24px}
.sm-day[hidden]{display:none}
.sm-dbox{width:min(1180px,100%); max-height:100%; overflow:auto; background:var(--surface);
  border:1px solid var(--line); border-radius:var(--r-lg); padding:16px 18px 14px}
.sm-dtop{display:flex; align-items:center; gap:10px; flex-wrap:wrap}
.sm-dtop h3{margin:0; font-size:17px; font-weight:650; color:var(--text)}
.sm-dtop .k{font-weight:650} .sm-dtop .k.none{color:var(--faint); font-weight:500}
.sm-dtop .p{font-family:var(--font-mono); font-variant-numeric:tabular-nums; font-size:16px}
.sm-dtop .sp{flex:1}
.sm-nav{font-size:12px; color:var(--dim); border:1px solid var(--line); border-radius:var(--r-sm);
  padding:5px 10px; cursor:pointer; user-select:none}
.sm-nav:hover{background:var(--surface-2); color:var(--text)}
.sm-nav.off{opacity:.35; pointer-events:none}
.sm-dwhy{font-size:12.5px; color:var(--dim); line-height:1.6; margin:8px 0 10px}
.sm-dchart{border:1px solid var(--line-soft); border-radius:var(--r-md); background:var(--bg)}
.sm-dchart svg{display:block; width:100%; height:auto}
.sm-dleg{display:flex; flex-wrap:wrap; gap:6px 16px; font-size:11.5px; color:var(--dim); margin-top:9px}
.sm-dleg i{display:inline-block; width:14px; height:0; border-top:2px solid; vertical-align:middle; margin-right:5px}
.sm-dnote{font-size:11px; color:var(--faint); margin-top:6px; line-height:1.55}

/* ══ 【即時】右欄那兩張唯讀小卡（2026-09-23 v3）══
   ⛔ `.right` 自己有 gap，卡片再帶 margin-bottom 就是雙倍間距。 */
.right>.card{margin-bottom:0}
.right>.card:empty{display:none}

/* ══ 【自動下單】v3 新增的版面件（2026-09-23）══
   ⛔ 一個新顏色都沒加：全部用 :root 既有 token。⛔ 紅綠只給損益，開／關一律金色與中性灰。 */
.al-bar{display:flex; gap:9px; flex-wrap:wrap; align-items:stretch; margin-bottom:13px}
.al-pill{display:flex; align-items:center; gap:11px; background:var(--surface-2);
  border:1px solid var(--line); border-radius:999px; padding:7px 9px 7px 14px; flex:1 1 260px; min-width:0}
.al-pill .lb{font-size:10px; color:var(--faint); letter-spacing:1px; flex:none}
.al-pill .nm{font-size:13.5px; font-weight:700; color:var(--dim); white-space:nowrap}
.al-pill .st{font-size:11px; font-weight:700; border-radius:999px; padding:3px 10px 4px;
  background:var(--surface); color:var(--faint); margin-left:auto; white-space:nowrap}
.al-pill.on{border-color:var(--gold-line); background:var(--gold-soft)}
.al-pill.on .nm{color:var(--gold)}
.al-pill.on .st{background:rgba(227,169,81,.22); color:var(--gold)}
.al-pill .off{border:1px solid var(--line); background:transparent; color:var(--faint);
  font-family:var(--font-sans); font-size:11.5px; border-radius:999px; padding:5px 12px; cursor:pointer; flex:none}
.al-pill .off:hover{color:var(--text); border-color:var(--faint)}
.al-now{display:grid; grid-template-columns:minmax(0,1fr) 300px; gap:16px; align-items:start}
@media(max-width:900px){ .al-now{grid-template-columns:minmax(0,1fr)} }
.al-pos{background:var(--surface-2); border:1px solid var(--line-soft); border-radius:var(--r-md); padding:11px 13px 12px}
.al-pos.live{border-color:var(--gold-line)}
.al-pos .hd{font-size:10px; color:var(--faint); letter-spacing:1.2px; display:flex;
  justify-content:space-between; align-items:center; gap:8px}
.al-pos .big{font-size:27px; font-weight:700; font-family:var(--font-mono); font-variant-numeric:tabular-nums;
  line-height:1.1; margin:6px 0 8px}
.al-pos .r{display:flex; justify-content:space-between; gap:8px; font-size:11.5px; color:var(--dim);
  font-family:var(--font-mono); font-variant-numeric:tabular-nums; padding:3px 0; border-top:1px solid var(--line-soft)}
.al-pos .r span{color:var(--faint)}
.al-pos .n{font-size:11px; color:var(--faint); line-height:1.6; margin-top:7px}
.al-pos .none{font-size:13px; color:var(--dim); margin-top:8px; line-height:1.6}
.al-perf{display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:1px; background:var(--line-soft);
  border:1px solid var(--line-soft); border-radius:var(--r-md); overflow:hidden}
@media(max-width:620px){ .al-perf{grid-template-columns:repeat(2,minmax(0,1fr))}
  .al-perf .c:last-child{grid-column:1/-1} }
.al-perf .c{background:var(--surface); padding:10px 13px 11px; min-width:0}
.al-perf .k{font-size:10.5px; color:var(--faint); letter-spacing:.4px}
.al-perf .v{font-size:21px; font-weight:700; font-family:var(--font-mono); font-variant-numeric:tabular-nums; margin-top:2px}
.al-perf .s{font-size:11px; color:var(--faint); font-family:var(--font-mono); margin-top:2px}
.al-spark{max-width:760px; margin:10px 0 2px}
.al-spark svg{display:block; width:100%; height:auto}
.al-chips{display:flex; gap:6px; flex-wrap:wrap; margin:0 2px 9px}
.al-chip{border:1px solid var(--line); background:var(--surface-2); color:var(--dim); font-size:11.5px;
  font-family:var(--font-sans); border-radius:999px; padding:5px 13px; cursor:pointer}
.al-chip.on{background:var(--gold-soft); color:var(--gold); border-color:var(--gold-line)}
.al-fold{margin-top:9px}
.al-fold summary{cursor:pointer; font-size:12px; color:var(--dim); padding:6px 2px; list-style:none}
.al-fold summary::-webkit-details-marker{display:none}
.al-fold summary:before{content:'▸ '; color:var(--faint)}
.al-fold[open] summary:before{content:'▾ '}
.al-fold summary:hover{color:var(--text)}
.al-fold>div{font-size:12px; color:var(--dim); line-height:1.75; padding:4px 2px 8px}
.al-fold b{color:var(--text)}
/* ⭐ 開關（2026-09-23 定案）：日盤一組（⛔ 不給選做法，固定多方聯軍）、
   夜盤一組（做法**單選**）。⛔ 打開／換做法都是真錢動作 ⇒ 兩段式；關閉一鍵。 */
.al-sel{border:1px solid var(--line); border-radius:var(--r-md); overflow:hidden; margin-bottom:12px}
.al-selh{display:flex; align-items:center; gap:10px; flex-wrap:wrap; padding:9px 13px 10px;
  background:var(--surface-2); border-bottom:1px solid var(--line)}
.al-selh .lb{font-size:10.5px; color:var(--faint); letter-spacing:1.2px; font-weight:700}
.al-selh .st{font-size:11px; font-weight:700; border-radius:999px; padding:3px 10px 4px;
  background:var(--bg); color:var(--faint); white-space:nowrap}
.al-selh .st.on{background:var(--gold-soft); color:var(--gold)}
.al-selh .sp{flex:1; min-width:0}
.al-selh .off{border:1px solid var(--line); background:transparent; color:var(--faint);
  font-family:var(--font-sans); font-size:11.5px; font-weight:650; border-radius:999px;
  padding:4px 12px 5px; cursor:pointer; white-space:nowrap}
.al-selh .off:hover{color:var(--text); border-color:var(--faint)}
.al-selh .nm{font-size:14px; font-weight:700; color:var(--dim); white-space:nowrap}
.al-selh.on .nm{color:var(--gold)}
.al-selbody{padding:12px 13px 13px; background:var(--surface)}
.al-selbody .ds{font-size:11.5px; color:var(--faint); line-height:1.65}
/* 夜盤那一組是**單選**（⛔ 不准做成可以同時開：兩條方向一致 95%、損益相關 0.88） */
.al-picks{display:flex; flex-direction:column}
.al-pick{display:flex; gap:11px; align-items:flex-start; text-align:left; width:100%;
  padding:11px 13px 12px; background:var(--surface); border:0; border-top:1px solid var(--line-soft);
  cursor:pointer; font-family:var(--font-sans)}
.al-picks .al-pick:first-child{border-top:0}
.al-pick:hover{background:var(--surface-2)}
.al-pick:focus-visible{outline:1px solid var(--gold); outline-offset:-2px}
.al-pick .rd{width:15px; height:15px; border-radius:50%; border:1.5px solid var(--ghost);
  flex:none; margin-top:2px; position:relative}
.al-pick.on .rd{border-color:var(--gold)}
.al-pick.on .rd::after{content:''; position:absolute; inset:3px; border-radius:50%; background:var(--gold)}
.al-pick.sel .rd{border-color:var(--text)}
.al-pick.sel .rd::after{content:''; position:absolute; inset:3px; border-radius:50%; background:var(--text)}
.al-pick .w{min-width:0; flex:1}
.al-pick .nm{font-size:14px; font-weight:700; color:var(--text); display:flex; align-items:center;
  gap:8px; flex-wrap:wrap; line-height:1.35}
.al-pick.on .nm{color:var(--gold)}
.al-pick .ds{font-size:11.5px; color:var(--faint); line-height:1.6; margin-top:3px}
.al-pick .now{font-size:10px; font-weight:700; color:var(--gold); background:var(--gold-soft);
  border-radius:5px; padding:1px 7px; letter-spacing:.5px; white-space:nowrap}
.al-pick .beta{font-size:10px; font-weight:700; color:var(--gold); border:1px solid var(--gold-line);
  border-radius:5px; padding:1px 7px; white-space:nowrap}
.al-selfoot{padding:11px 13px 13px; background:var(--surface); border-top:1px solid var(--line-soft)}
.al-selfoot .n{font-size:11.5px; color:var(--faint); line-height:1.7}
.al-selfoot .n b{color:var(--dim); font-weight:650}
.al-selfoot .al-conf{margin-top:0}
.al-conf .q .w2{display:block; margin-top:6px; font-size:12px; font-weight:600; color:var(--dim)}
.al-conf.real .q .w2{color:var(--up)}

/* ══ 【健檢】分頁（2026-09-23 加；class 一律 hc- 前綴）══
   ⛔ 燈號用**面板既有語彙**（Benson 拍板）：正常＝中性灰、明顯變差＝金色描邊、
      連 15 筆為負＝金色實心。⛔ 不用紅黃綠 —— 這個面板的綠色是「賠錢」，會直接打架。
   ⛔ 燈號旁邊**永遠要有那個詞**，⛔ 不准只有顏色。 */
.hc-hint{font-size:11.5px; color:var(--faint); line-height:1.7; margin:-2px 2px 12px}
.hc-grid{display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px}
@media(max-width:860px){ .hc-grid{grid-template-columns:minmax(0,1fr)} }
.hc-card{background:var(--surface); border:1px solid var(--line-soft); border-radius:var(--r-lg); padding:15px 17px 14px}
.hc-card.warn{border-color:var(--gold-line)}
.hc-top{display:flex; align-items:center; gap:10px; flex-wrap:wrap}
.hc-nm{font-size:16px; font-weight:700; color:var(--text); line-height:1.25}
.hc-nm i{display:block; font-style:normal; font-size:11px; font-weight:500; color:var(--faint); margin-top:2px; font-family:var(--font-mono)}
.hc-lamp{margin-left:auto; display:inline-flex; align-items:center; gap:7px; font-size:11.5px;
  font-weight:650; border-radius:999px; padding:3px 11px 4px; border:1px solid var(--line);
  background:var(--surface-2); color:var(--dim); white-space:nowrap}
.hc-lamp b{width:8px; height:8px; border-radius:50%; background:currentColor; flex:none}
.hc-lamp.ok{color:var(--dim); background:var(--surface-2); border-color:var(--line)}
.hc-lamp.na{color:var(--faint); background:var(--surface-2); border-color:var(--line)}
.hc-lamp.wn{color:var(--gold); background:var(--gold-soft); border-color:var(--gold-line)}
.hc-lamp.bd{color:#12161C; background:var(--gold); border-color:var(--gold)}
.hc-nums{display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:1px; background:var(--line-soft);
  border:1px solid var(--line-soft); border-radius:var(--r-md); overflow:hidden; margin-top:13px}
@media(max-width:520px){ .hc-nums{grid-template-columns:repeat(2,minmax(0,1fr))} }
.hc-nums .c{background:var(--surface-2); padding:10px 13px 11px; min-width:0}
.hc-k{font-size:10.5px; color:var(--faint); letter-spacing:.4px}
.hc-v{font-size:24px; font-weight:700; font-family:var(--font-mono); font-variant-numeric:tabular-nums;
  line-height:1.15; margin-top:3px}
.hc-v small{font-size:12px; font-weight:600; color:var(--faint); margin-left:3px; letter-spacing:0}
.hc-d{font-size:11px; color:var(--faint); font-family:var(--font-mono); font-variant-numeric:tabular-nums; margin-top:4px}
.hc-d em{font-style:normal; color:var(--dim)}
.hc-spark{margin-top:12px}
.hc-spark svg{display:block; width:100%; height:auto}
.hc-foot{font-size:11px; color:var(--faint); font-family:var(--font-mono); margin-top:9px;
  display:flex; flex-wrap:wrap; gap:6px}
.hc-foot .sep{color:var(--ghost)}
.hc-real{font-size:11.5px; color:var(--dim); line-height:1.7; margin-top:9px;
  border-top:1px solid var(--line-soft); padding-top:8px}
.hc-real b{color:var(--text)}
.hc-na{font-size:12.5px; color:var(--dim); line-height:1.8; padding:14px 2px 6px}
.hc-na b{color:var(--text)}
.hc-mkt{display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px}
@media(max-width:860px){ .hc-mkt{grid-template-columns:minmax(0,1fr)} }
.hc-m{background:var(--surface); border:1px solid var(--line-soft); border-radius:var(--r-lg); padding:14px 16px 13px}
.hc-m.warn{border-color:var(--gold-line)}
.hc-m .t{font-size:12.5px; font-weight:650; color:var(--text)}
.hc-m .t i{display:block; font-style:normal; font-size:10.5px; font-weight:500; color:var(--faint); margin-top:2px; line-height:1.5}
.hc-m .v{font-size:30px; font-weight:700; font-family:var(--font-mono); font-variant-numeric:tabular-nums;
  line-height:1.1; margin-top:10px; color:var(--text)}
.hc-m .v small{font-size:14px; font-weight:600; color:var(--faint); margin-left:2px; letter-spacing:0}
.hc-band{position:relative; height:6px; border-radius:3px; background:var(--surface-2); margin-top:13px}
.hc-band i{position:absolute; top:0; bottom:0; border-radius:3px; background:var(--line)}
.hc-band u{position:absolute; top:-4px; width:2px; height:14px; border-radius:1px; background:var(--gold)}
.hc-scale{display:flex; justify-content:space-between; font-size:10px; color:var(--ghost);
  font-family:var(--font-mono); margin-top:6px}
.hc-pos{font-size:11.5px; color:var(--dim); margin-top:8px; font-family:var(--font-mono); font-variant-numeric:tabular-nums}
.hc-pos b{color:var(--text); font-weight:700}
.hc-lines{font-size:11.5px; color:var(--dim); margin-top:7px; line-height:1.7; font-family:var(--font-mono)}
.hc-legend{display:flex; gap:18px; flex-wrap:wrap; font-size:11px; color:var(--faint); margin:13px 2px 0}
.hc-legend span{display:inline-flex; align-items:center; gap:7px}
.hc-legend .hc-lamp{margin-left:0; font-style:normal; font-size:10.5px; padding:2px 9px 3px}
.hc-legend .hc-lamp b{width:7px; height:7px}
.hc-foot2{font-size:11.5px; color:var(--faint); line-height:1.7; margin:2px 4px 0}
</style></head><body><div class="app">
<div class="topbar">
  <div class="brand"><div class="mark">&#9702;</div>
    <div><div class="nm">早盤儀表板</div><div class="sub" id="sub">連線中…</div></div></div>
  <div class="tabs">
    <button data-tab="live" class="on">即時</button>
    <!-- 【帳戶】券商端的錢（2026-09-21 Benson 指定要自己一頁）。
         ⚠️ 排在【即時】右邊是刻意的：它講的是「現在的狀態」，跟即時是同一類；
         ⛔ 它**唯讀**，所以不影響「愈往右愈接近真錢」那條動線（⛔ 也不可以排到
         【自動下單】右邊 —— 那一頁永遠是最右邊的終點）。 -->
    <button data-tab="acct">帳戶</button>
    <!-- ⚠️ 2026-09-16 分頁重整（Benson 交辦）：拿掉【細節】與【回顧】（他已經不看了），
         把「模擬（不會下單）」從【策略實驗室】最上面搬出來獨立成一頁。
         現在四顆的動線是「現在 → 規則自己在跑（不下單）→ 研究 → 會真的送單」，
         愈往右愈接近真錢。⛔ 不可以把【自動下單】往左搬。
         ⚠️ 被拿掉的兩頁**後端沒有跟著拆**（/api/tick/*、/api/review、/api/bars 不帶 full）：
            /api/bars 與 day_bars() 是即時分頁也在用的，tick_logs 的逐筆落地更是獨立的資料線。 -->
    <!-- 【模擬】：七條策略每天事後照規則算一次，⛔ 一口單都不會送（後端 sim_lanes.py）。
         放在【即時】右邊：它講的是「同一天，如果照規則做會怎樣」，跟研究頁是兩件事。 -->
    <button data-tab="sim">模擬</button>
    <!-- ⭐⭐ 2026-09-23 v3：【策略實驗室】(#tab-lab) 的**畫面整頁移除**（Benson 交辦）。
         ⛔ **後端一行都沒拆**：/api/lab/meta、/api/lab/run、strategy_lab.py、
            autotest/ 的模擬紀錄與 /api/auto/* 照樣在跑
            ——【自動下單】每一筆紀錄的「模擬那邊」就是讀 /api/auto/*，拆掉會少一塊。
         ⚠️ 那一頁原本的結束標記（「到此」那行註解）是三把尺的切片邊界
            （autotest-backend.py ①／test_strategy_lab.py ⑥／test_sim_lanes.py ⑦）——
            移除時三把尺一起改成量新的【健檢】與【自動下單】（⛔ 不留量到空白區間的尺）。 -->
    <!-- ⭐ 【健檢】：正在跑真單那幾條的體檢 ＋ 市場狀態。⛔ 唯讀、⛔ 不預測不建議。
         排在【模擬】與【自動下單】中間是刻意的：動線是「現在 → 錢 → 規則在跑（不下單）
         → 正在跑真單的那幾條體檢 → 會真的送單」，愈往右愈接近真錢。
         ⛔ 不可以排到【自動下單】右邊（那一頁永遠是最右邊的終點）。 -->
    <button data-tab="hc">健檢</button>
    <!-- ⛔⛔ 【自動下單】＝**會真的送出委託單**的那一頁，所以放最右（動線的終點）。 -->
    <button data-tab="fire">自動下單</button>
  </div>
  <div class="an-tr">
   <div class="clock"><div class="d" id="clk">--:--</div><div class="w" id="ph"></div></div>
   <!-- ⭐ 2026-09-24【交易分析師】每週報告的信件（未讀＝金色＋數字）。⛔ 唯讀：點開只看報告。 -->
   <button class="an-mail" id="anmail" aria-label="分析師週報" title="分析師週報"></button>
  </div>
</div>
<div class="an-pop" id="anpop" hidden></div>
<div class="an-sheet" id="ansheet" hidden></div>

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
    <!-- ⭐⭐ 2026-09-23 v3（Benson 拍板：規格 §1 方案 A）：右欄**只留看盤要看的兩張唯讀小卡**。
         拿掉的是「練習下單」「真實下單」兩個操作區塊、練習交易紀錄、實際交易紀錄、
         練習成績／真實成績、以及練習／真實那兩顆頁籤（#ntabs／#tabbadge／#zone）。
         ⛔⛔ **這一輪只拆畫面，後端一行都沒拆**：/api/enter、/api/close、/api/real/enter、
            /api/real/close、practice_trades/、real_trades/、data/practice.json 的同步照舊
            ——手機 App 在讀 practice.json，real_trades/ 是【自動下單】出場價的唯一真相來源。
         右欄三個各自比對自己字串的節點：
           #xal    跨分頁警報（沒警報時 :empty 完全不佔位）
           #lvpos  今天的部位（⛔ 唯讀、⛔ 零按鈕）
           #lvacct 帳戶摘要（⛔ 唯讀、⛔ 零按鈕；金額一律標「券商端」） -->
    <div id="xal"></div>
    <div class="card" id="lvpos"></div>
    <div class="card" id="lvacct"></div>
  </div></div>
</div>

<!-- ══════════ 【帳戶】：券商端的「帳戶還剩多少錢」（2026-09-21）══════════
     ⚠️ 2026-09-21 傍晚 Benson 改主意：本來掛在【即時】右欄，他要**自己一個分頁**。
     ⛔ 這一頁**完全唯讀**：一顆會動到錢的按鈕都沒有，也不參與下單那條路。
     ⛔ 只放一個空容器：數字、判斷句（夠不夠下一口）、本月變化全部從後端來
        （/api/state 的 `equity`，後端每分鐘才真的問券商一次），
        曲線另外拿 /api/account/hist（一天才多一個點，10 分鐘拿一次）。 -->
<div id="tab-acct" hidden>
  <div id="acct"></div>
</div>

<!-- ══════════ 【模擬】：七條策略每天事後照規則算一次（⛔ 不會下單）══════════
     後端 sim_lanes.py，唯讀端點 GET /api/sim/state。
     ⚠️ 2026-09-16 從【策略實驗室】最上面那張卡搬出來獨立成一頁（Benson 交辦），
        class 一律沿用原本的 `sm-` 前綴 ⛔ 不改名。
     ⛔ 跟【自動下單】的真單紀錄完全分開：不同的檔、不同的端點、不同的卡，⛔ 不准混進同一個清單。
     ⛔ 只放空容器：規則說明、月合計、今天狀態、逐日紀錄全部從後端來（前端不寫死時刻與點數）；
        ⛔ 一顆會動到資料或錢的鈕都沒有 —— 只有「點進一條策略」與「回到七條」兩個純導覽的控制項
        （2026-09-17 加；它們只換畫面、不打任何 POST）。
     ⛔ 只列歷史模擬結果：不放勝率估計、不放預估、不給進場提示。
     ⚠️ .sm-lanes 是**獨立節點**：七條的骨架只在條數變動時重建，每條的內容各自比對自己的字串
        （整塊重繪會把捲動位置與剛畫好的內容一起換掉）。
     ⭐ #smdet ＝點進去之後那一條的內頁（GET /api/sim/lane?key=…，⛔ 只有點下去才打）。
        ⚠️ 兩張卡**同時只有一張看得見**：開內頁時 #smcard hidden、關掉時反過來。 -->
<div id="tab-sim" hidden>
 <div class="card sm-card" id="smcard">
  <div class="sec-head"><h2>模擬（不會下單）</h2><span class="count" id="smcount"></span></div>
  <div class="sm-note" id="smnote"></div>
  <div class="sm-lanes" id="smlanes"></div>
  <div class="sm-foot" id="smfoot"></div>
 </div>
 <div class="card sm-card" id="smdet" hidden></div>
 <div class="sm-day" id="smday" hidden></div>
</div>

<!-- ══════════ 【健檢】：正在跑真單那幾條的體檢 ＋ 市場狀態（2026-09-23 加）══════════
     後端 health.py，唯讀端點 GET /api/health/state（過 fire_get_guard）。
     ⛔⛔ **這一頁不准出現預測、勝率、期望值、訊號強度、買賣建議**（CLAUDE.md 開頭那條鐵律）。
        燈號**只描述已經發生的數字**，⛔ 不准附「要不要關掉／要不要加碼」。
     ⛔⛔ **不可以掛在 5 秒輪詢上**：切進這一頁才打一次，後端整天快取（來源檔的 mtime 當快取鍵）。
        市場狀態第一次要讀兩個大檔（幾秒，跑在後端的背景執行緒）⇒ 還沒算完時這裡會隔幾秒
        再問一次，⛔ 算完就停（見 hcEnter）。
     ⛔ class 一律 hc- 前綴（al-／at-／sm-／tk-／dt-／n- 都被別頁佔走了）。
     ⛔ 只放空容器：燈號、門檻、百分位全部由後端決定 —— 前端自己算一份就是第二把尺。 -->
<div id="tab-hc" hidden>
 <div class="card l1" style="margin-bottom:14px">
  <div class="sec-head"><h2>策略健檢</h2><span class="count" id="hcstamp"></span></div>
  <div class="hc-hint" id="hcsrc"></div>
  <div class="hc-grid" id="hccards"></div>
  <div class="hc-legend" id="hcleg"></div>
 </div>

 <div class="card">
  <div class="sec-head"><h2>市場狀態</h2><span class="count">現在 vs 過去一年</span></div>
  <div class="hc-hint">只是把已經發生的數字畫成位置，⛔ 沒有預測、沒有買賣建議。</div>
  <div class="hc-mkt" id="hcmkt"></div>
 </div>

 <div class="hc-foot2" id="hcfoot"></div>
</div>
<!-- ══ 【健檢】到此 ══ （⛔ 這行是 tools/probe/autotest-backend.py ①／
     test_strategy_lab.py ⑥／test_sim_lanes.py ⑦ 切「健檢那一段 HTML」的結束標記，
     ⛔ 全檔只准出現一次、⛔ 改字要三個檔一起改 ——
     抄第二份會被 page.index() 先找到、把切片切成負的。 -->

<!-- ══════════ 【自動下單】：會真的送出委託單的那一頁 ══════════
     ⛔⛔ **開難、關易**（Benson 2026-09-09 拍板；同日下午他要求「開」也做到畫面上）：
       ・**開**＝ #alon／#nfon 那幾顆做法鈕 → **第二段確認條** → 「確定，打開」
         → POST /api/fire/on（日盤）／POST /api/nightfire/on（夜盤，2026-09-23 加）。
         兩支都走六道防護（見 live_panel.fire_post_guard）。
         ⛔ **兩段式不可以拿掉**：這是這個面板上僅有的兩個會武裝真錢的地方。
         ⛔ 確認條那句話（現在是真錢還是演練）**一律從後端拿**（arm_confirm），
            ⛔ 前端不准自己猜 —— 講錯的代價是「他以為只是演練，結果真的送了一口」。
         ⛔ 第一段沒按「確定」之前**一個請求都不准出去**（fire-tab.mjs ⑪ 在守）。
       ・**關**是狀態列上那一顆（→ POST /api/fire/off ／ /api/nightfire/off）。
         關掉永遠是安全的動作，所以**不跳確認**（⛔ 開跳、關不跳，這個不對稱是刻意的）。
       ・⛔ 開著的時候**只有**「關閉」那一顆（⛔ 沒有「換做法」——再按一次開的話後端回 409）。
     ⭐⭐ 2026-09-23 v3 重新編排（Benson 拍板，規格 §2）：
       ① 狀態列（日盤／夜盤兩顆藥丸並排，「關閉」鈕就在這裡）
       ② 今天（左＝結論＋三個候選／右＝現在的部位）
       ③ 風險兩句（⛔ 一字不刪，位置從開關區搬到「今天」底下）
       ④ 最近做得如何（⛔ 不放勝率、不放期望值、不放勝敗場數）
       ⑤ 紀錄（日盤與夜盤**合併在同一份清單**，靠 chips 篩；⛔ 只在畫面上合併，
          ⛔ 不准合併檔案、⛔ 不准共用 rule 命名空間）
       ⑥ 開關與規則（沉到最底：一年按不到幾次的東西）
     ⛔ 這一頁永遠是分頁的最右邊（動線的終點）。 -->
<div id="tab-fire" hidden>

 <div class="card l1">
  <!-- ① 狀態列：日盤／夜盤兩顆藥丸。⛔ 開／關用金色與中性灰，⛔ 不准用紅綠（紅綠只給損益）。
       「關閉」鈕就在這裡（原 #aloff）：關掉永遠是安全的動作、要好按、⛔ 照舊不跳確認。 -->
  <div class="al-bar" id="albar"></div>
  <!-- 條件式的警告（真單關著／送單出過錯／結算日判不出來…）⛔ 一條都不准少 -->
  <div class="at-head"><div class="at-title"><div class="s" id="alsub"></div></div></div>
  <div class="al-gates" id="algates"></div>
  <!-- ⭐ 2026-09-24 風控規則 B：本月自動單損益／上限（＋到上限時的兩段式「手動解除」）。
       ⛔ 空容器：按鈕一律由 alPaint 畫進來（這一頁的靜態 HTML 不准有 button）。 -->
  <div class="al-cap" id="alcap"></div>
  <!-- ⭐ 2026-09-15：今天的門檻與判定（快／不快／歷史不夠）。⛔ 判定只從後端 /api/fire/state 的 fast 來。 -->
  <div class="al-fast" id="alfast"></div>

  <!-- ② 今天：左＝結論（七種結局的文案⛔ 一句都不改）＋三個候選，右＝現在的部位 -->
  <div class="al-now">
   <!-- 今天那一口**已經出場**時左半整塊收起來（alPaint 設 hidden），改併進底下「紀錄」的第一列
        （金框＋「今天」＋「已出場」標，2026-09-14 一件事只講一次）。
        ⛔ 其他狀態（還沒到／沒有紀錄／沒送成／持有中／收盤警示）照舊畫在這裡。 -->
   <div id="altodaybox">
    <div class="sec-head"><h2>今天</h2><span class="count" id="alcount"></span></div>
    <div class="al-today" id="altoday"></div>
   </div>
   <!-- ⛔ 右邊這張**唯讀**：一顆鈕都沒有。 -->
   <div id="alpos"></div>
  </div>

  <!-- ③ 風險兩句（2026-09-10）：⛔⛔ 一句都不准刪
       （停損活在這台電腦裡／這個開關沒有有效期）。開著才畫。 -->
  <div id="alrisk"></div>
 </div>

 <!-- ④ 最近做得如何：⛔ 不放勝率、不放期望值、不放勝敗場數。
      資料用 /api/fire/state 現有的帳本列＋real 出場價算，⛔ 不新開端點。 -->
 <div class="card">
  <div class="sec-head"><h2>最近做得如何</h2><span class="count" id="alperfn"></span></div>
  <div class="al-perf" id="alperf"></div>
  <div class="al-spark" id="alsparkbox"></div>
  <div class="al-empty">同一份數字在【健檢】那一頁有跟歷史基準的對照。</div>
 </div>

 <!-- ⑤ 紀錄 -->
 <div class="card">
  <div class="sec-head"><h2>紀錄</h2><span class="count" id="allogn"></span></div>
  <!-- 日盤／夜盤篩選 chips。⚠️ 兩邊是**不同的帳本**（autofire/ vs nightfire/），
       合併只發生在**畫面**上。
       ⛔⛔ 這裡是**空容器**：這一頁的靜態 HTML 一顆 button／form／input 都不准有
       （test_auto_fire.py ⑪ 在守）—— 所有控制項一律由 alPaint 畫進來。 -->
  <div class="al-chips" id="alfilter"></div>
  <!-- ⛔⛔ 跟練習／真實那份清單**同一種卡片**（`.list` ＋ `.trade`）。
       ⛔ `.list>*{flex:none}` 由 `.list` 自己帶著，⛔ 不可以省 ——
          `.list` 是有 max-height 的 flex 直欄，少了那條、筆數一多就是**把每一列壓扁**
          （實測 107px 被壓成 21.6px），而且筆數少的時候完全看不出來。 -->
  <div class="list al-list" id="altbl"></div>
  <div class="al-empty" id="alempty"></div>
  <div class="at-notes" id="alnotes"></div>
 </div>

 <!-- ⑥ 開關：沉到最底（開難關易 —— 關在最上面隨手可及，開在最底下要捲下來）。
      ⛔ 「打開」那幾顆鈕＋第二段紅底確認條**一道都不准少**。
      ⭐⭐ 2026-09-23 定案（規格 §2.7）：
        ・**日盤**＝多方聯軍，⛔ **畫面上不給選做法**（後端 auto_fire.METHODS 仍支援 A，
          但前端固定送 `U`；⛔ 整頁不准出現第二種做法可以選）。
        ・**夜盤**＝做法**單選**（清單從後端 night_fire.METHODS 來，⛔ 前端不寫死）。
          ⛔ 單選是刻意的：兩條同時開＝加倉，結構上就不可能。
        ・換做法一定是**先關再開**（開關檔不覆蓋，後端已經開著再 on 會回 409）。
      ⛔ 這裡只放空容器：兩組的內容都由 alPaint 畫進來（⛔ 靜態 HTML 一顆 button 都不准有）。 -->
 <div class="card" id="alswitchcard">
  <div class="sec-head"><h2>開關</h2><span class="count">一年按不到幾次的東西放這裡</span></div>
  <div id="alswitch"></div>
 </div>
</div>

<!-- ══ 【自動下單】到此 ══ （⛔ 這行是 tools/probe/autotest-backend.py ①／
     test_strategy_lab.py ⑥／test_sim_lanes.py ⑦ 切分頁那一段 HTML 的結束標記，
     ⛔ 全檔只准出現一次、⛔ 改字要三個檔一起改。
     ⚠️ 2026-09-23 v3：舊的邊界是【策略實驗室】後面那行「到此」註解，那一頁整個拿掉了 ⇒
        三把尺一起改成量【健檢】(#tab-hc → #tab-fire) 與【自動下單】(#tab-fire → 這一行)，
        ⛔ 不留一把量到空白區間還恆綠的尺。 -->

<!-- 頁尾那兩行（「只顯示已經發生的客觀數字…」「練習下單與【自動下單（模擬）】都是模擬…」）
     2026-09-14 依 lab-ux 定案 A 拿掉：第一句是給工程的原則（正本在 CLAUDE.md），
     第二句頁籤副標與【模擬】頁的鎖印已經在講。⛔ 原則本身沒有放寬。 -->
</div>
<script>
// ⛔ 下單規則的數字只在 Python 定義一次（TP_POINTS／SL_POINTS／SIGNAL_AT），PAGE 定義完就替換進來。前端不准自己寫死。
const RULE_TP=__RULE_TP__, RULE_SL=__RULE_SL__, RULE_SIGNAL_AT='__RULE_SIGNAL_AT__';
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
/* ⭐⭐ 403 的時候要對「他自己」說的那一句（⛔ 不是後端那句話）。
   ⛔⛔ 後端回的是「這個請求不是從面板發出來的」—— 那句話是講給**外面的網頁**聽的，
      端到他面前是**誤導**：他明明就是站在面板上按的。
   ⚠️⚠️ 這句話的第一件事是**把「沒送出去」講死**，理由不是美觀：
      `ENTER_LOCK`／`firing`／`closing` 只擋得住「同時按兩下」，
      擋不掉「他以為沒送、過三秒再按一次」。看門狗重啟後的那 ≤0.5 秒是一個
      **新的失敗外觀**，等於多給了他一個「再按一次」的理由 ——
      所以這句話絕對不可以讓他懷疑「是不是其實送出去了，只是畫面沒更新」。
   ⚠️ 它是**共用**的一句（十條路由都可能回 403）⇒ ⛔ 不准寫成「一張單都沒有」
      那種只對下單成立的字（存心得、寫重播也走這裡）。 */
var P403='這次沒有送出去，面板剛重新啟動過。請再按一次（剛剛那一下不算，不會變成兩筆）。';
function ptok(){
  if(PTOK) return Promise.resolve();
  return fetch('/api/state').then(r=>r.json())
    .then(s=>{ if(s&&s.token) PTOK=s.token; }).catch(()=>{});
}
function pfetch(url,body){
  return ptok().then(()=>fetch(url,{method:'POST',
    headers:{'Content-Type':'application/json','X-Panel':'1','X-Panel-Token':PTOK},
    body:body||'{}'})).then(function(r){
      if(r.status!==403) return r;
      /* 看門狗剛把面板重開過 ⇒ 手上這份 token 是舊的。把它清掉，
         下一次 `ptok()` 就會先去 `/api/state` 要一份新的（⛔ 那是一個 GET）。
         ⛔⛔ **絕對不做自動重送。** 這個出口上掛著 `/api/real/enter` ——
            自動重試就是教程式在他沒看見的情況下送第二張單。
            清 token ＋ 換一句話就夠了，那一下要由**他**再按。
         ⚠️ 回一個長得像 Response 的東西（`status` ＋ `json()`），
            呼叫端一律 `r.json()`／看 `r.status` ⇒ ⛔ 十個呼叫點一個都不用改。 */
      PTOK='';
      return {status:403, ok:false,
              json:function(){ return Promise.resolve({ok:false,msg:P403}); }};
    });
}

/* ══════════════════════════════════════════════════════════════════════
   右欄：跨分頁警報 ＋ 兩張**唯讀**小卡（2026-09-23 v3）
   ----------------------------------------------------------------------
   ⭐⭐ 2026-09-23 改版（Benson 拍板：規格 §1 方案 A）：【即時】**只留看盤**。
      「練習下單」「真實下單」兩個操作區塊、練習交易紀錄、實際交易紀錄、
      練習成績／真實成績整組從**畫面上**拿掉；右欄改成兩張唯讀小卡：
        ① 今天的自動下單（做法／方向／浮動點數／進場・停損・停利・收盤平倉時刻）
        ② 帳戶（權益總值／今日損益／還下不下得了一口）
      ⛔⛔ **兩張卡一顆按鈕都沒有**（沒有 [data-act]／[data-rdir]／form／submit）。
      ⛔⛔ **後端一行都沒拆**：/api/enter、/api/close、/api/real/enter、/api/real/close、
         practice_trades/、real_trades/、data/practice.json 的同步照舊 ——
         手機 App 在讀 practice.json、real_trades/ 是【自動下單】出場價的唯一真相來源。
      ⚠️ 兩張卡的資料**都已經在 /api/state 裡**（position／real／equity）⇒
         ⛔ 不新增端點、⛔ 不多一條輪詢。
   ⛔ 每一塊各自比對「上次自己設進去的字串」（快取在節點上，見 setEl）——
      整塊 innerHTML 每 0.5 秒重建的話，畫面會一直閃。
   ══════════════════════════════════════════════════════════════════════ */

/* 平倉原因的字。⛔ 【自動下單】那一頁的紀錄卡也在用（rwhy），
   ⛔ 不可以跟著右欄那兩區一起被刪掉。 */
const RWHY={sl:'停損', tp:'停利', manual:'手動', closed_elsewhere:'別處平的', eod:'收盤'};
function rwhy(t){ return RWHY[t&&t.reason]||'其他'; }

/* 沒有即時報價時，現價退回「最後一根 K 棒的收盤」。 */
function livePx(s){
  const p=(s.chips||{}).price;
  if(p!=null) return p;
  const B=(barsCache&&barsCache.bars)||[];
  return B.length?B[B.length-1].c:null;
}

/* ---------------- 跨分頁警報 ----------------
   停損活在這台電腦的 Python 迴圈裡。⛔ 他在哪一個分頁都要看得到。
   ⚠️ 秒數每秒在變，所以骨架與秒數拆成兩個節點（寫在一起整塊會每秒重建）。
   ⚠️ 這一塊不掛任何 CSS 動畫（舊版 .ralarm.bad 的 1.1 秒呼吸永遠演不完半個循環，
      因為每 0.5 秒就從頭開始 —— 看起來是在抖不是在呼吸）。
   ⚠️ 2026-09-23 v3：以前右上角那顆「去看部位 →」是切到右欄的【真實】分區，
      那一區已經拿掉了 ⇒ 那顆鈕跟著拿掉（⛔ 不留一顆按下去什麼都不會發生的鈕）。
      三種警報的文字**一個字都沒改**。 */
/* 不設停利（no_tp）的那一口是哪一條規則開的（2026-09-23 夜盤跟勢接上送單後才需要分）。
   ⚠️ 看**現在的時段**而不是進場時間：重啟撿回來的部位 entry_time 是空的。
      開箱只在日盤（09:0x 進、13:43:30 平），夜盤時段手上一口不設停利的只可能是夜盤跟勢。 */
function isNightNow(){
  const h=new Date().getHours();
  return h>=15||h<5;
}
function noTpTag(){ return isNightNow()?'夜盤跟勢':'開箱'; }
function xalHTML(R){
  const P=R.position;
  if(!P) return '';
  if(R.stale_sec!=null)
    return '<div class="n-x n-bad"><div class="g">&#9888; 報價已中斷 '+
      '<span class="num" id="xalsec"></span> 秒　停損現在沒人在看'+
      '<div class="s">停損活在這台電腦裡，收不到報價就判斷不了。'+
      '請立刻到大戶投確認部位。</div></div></div>';
  /* ⛔ 「開箱」那一口照規則就沒有停利（P.no_tp）⇒ ⛔ 不可以跳「停利沒有掛上券商」的警報
     （那是出事了才該跳的），但**券商端一張單都沒有**這件事要照實講。 */
  if(P.no_tp)
    return '<div class="n-x n-att"><div class="g">&#9888; '+noTpTag()+'：券商端無掛單'+
      '<div class="s">這一口沒有停利單、永豐又沒有停損單 —— '+
      '停損與收盤平倉<b>都靠面板</b>。面板關掉或電腦睡著就都不會發生。</div></div></div>';
  if(!P.has_target)
    return '<div class="n-x n-att"><div class="g">&#9888; 停利沒有掛上券商　賺的那一邊沒有保護'+
      '<div class="s">請到大戶投自己補掛一張 '+f(R.tp)+' 的平倉限價單，或直接平倉。</div></div>'+
      '</div>';
  return '';
}

/* ---------------- ① 今天的部位（唯讀小卡）----------------
   ⛔⛔ **唯讀**：⛔ 沒有任何按鈕、⛔ 不重複畫規則說明（規則的正本在【自動下單】那一頁）。
   ⛔ 每個數字都從 /api/state 的 `real` 來（跟停損監控同一份記憶體狀態）——
      ⛔ 前端不准自己拿現價減進場價去算浮動點數（後端 `float_pts` 才是正本），
      ⛔ 也不准自己算停利停損價（後端 `tp`／`sl` 是照這一口自己的點數算的）。
   ⚠️ 「這一口是不是自動下單開的」用的是**既有的那把尺**：`sl_points` 只有
      `auto_fire`／`night_fire` 送的那一口才會帶（手動真單不帶）——
      `pos_sl_points()` 判「用哪一組停損點數」看的就是它。⛔ 不另外發明一個旗子。
      判不出來就照實寫「手上有一口」，⛔ 不猜是誰開的。 */
function lvPosHTML(s){
  const R=s.real||{}, P=R.position;
  const head='<div class="sec-head"><h2>今天的部位</h2><span class="count">唯讀</span></div>';
  if(R.error) return head+'<div class="al-pos"><div class="hd"><span>現在的部位</span></div>'+
    '<div class="none">'+esc(R.error)+'</div></div>';
  if(!P)
    return head+'<div class="al-pos"><div class="hd"><span>現在的部位</span></div>'+
      '<div class="none">現在沒有部位。<br>自動下單與你自己下的單都會出現在這裡。</div></div>';
  const dir=P.dir==='long'?'▲ 做多':(P.dir==='short'?'▼ 做空':'方向不明');
  const fp=R.float_pts;
  const who=(P.sl_points!=null)?'自動下單那一口':'手上有一口';
  return head+
    '<div class="al-pos live"><div class="hd"><span>'+who+'</span>'+
      '<span style="color:var(--gold)">'+dir+' '+esc(String(P.qty==null?1:P.qty))+' 口</span></div>'+
    '<div class="big '+sgn(fp)+'">'+(fp==null?'—':pm(fp))+
      ' <small style="font-size:13px;font-weight:600;color:var(--faint)">點</small></div>'+
    '<div class="r"><span>進場</span><b>'+f(P.entry)+
      (P.entry_time?'（'+esc(String(P.entry_time).slice(0,8))+'）':'')+'</b></div>'+
    '<div class="r"><span>停損</span><b>'+(R.sl==null?'—':f(R.sl))+'</b></div>'+
    '<div class="r"><span>停利</span><b>'+(P.no_tp?'不設停利':(R.tp==null?'—':f(R.tp)))+'</b></div>'+
    (P.no_tp?'<div class="r"><span>券商端</span><b style="color:var(--gold)">無掛單</b></div>':'')+
    '</div>';
}

/* ---------------- ② 帳戶（唯讀小卡）----------------
   ⛔⛔ **唯讀**：⛔ 沒有任何按鈕。
   ⛔ 判斷句（夠不夠下一口）**整句都是後端算的**（live_panel.equity_view 的 `enough.msg`）——
      ⛔ 前端不准自己拿可動用去比保證金。
   ⛔ 欄位跟【帳戶】那一頁**同一組**（acctHTML）：⛔ 不在這裡另外算一份數字。
   ⛔ 金額一律標「券商端」——它含手續費、稅與其他部位，⛔ 不是策略績效。 */
function lvAcctHTML(s){
  const E=s&&s.equity;
  const head='<div class="sec-head"><h2>帳戶（券商端）</h2><span class="count">'+
    esc(E&&E.at?String(E.at).slice(0,5)+' 更新':'唯讀')+'</span></div>';
  if(!E) return '';                       /* 後端還沒有這一份 ⇒ 整張卡不畫（⛔ 不寫假的） */
  if(!E.ok)
    return head+'<div class="al-pos" style="border:0;background:transparent;padding:0">'+
      '<div class="none">'+esc(E.err||'問不到帳戶餘額')+'</div></div>';
  const en=E.enough||{};
  return head+'<div class="al-pos" style="border:0;background:transparent;padding:0">'+
    '<div class="r" style="margin-top:0"><span>權益總值</span>'+
      '<b style="font-size:17px;color:var(--text)">'+acctMoney(E.equity)+'</b></div>'+
    '<div class="r"><span>今日損益</span><b class="'+sgn(E.day_pl)+'" style="font-size:14px">'+
      acctPM(E.day_pl)+'</b></div>'+
    '<div class="r"><span>還下得了一口</span><b style="color:var(--text)">'+
      (en.ok===true?'可以':(en.ok===false?'不夠':'判不出來'))+'</b></div>'+
    '<div class="n">'+esc(en.msg||'')+'</div>'+
    /* ⭐ 2026-09-24：還撐得住幾次停損（⛔ 整句後端算，warn 才用金色） */
    ((E.cushion&&E.cushion.msg)?'<div class="n"'+(E.cushion.warn?' style="color:var(--gold)"':'')+'>'+
      esc(E.cushion.msg)+'</div>':'')+'</div>';
}


/* ══════════ 【帳戶總覽】（2026-09-21 Benson 交辦）══════════
   券商端的「帳戶還剩多少錢」。⛔ 這一區**唯讀**：沒有任何按鈕、不碰下單那條路。
   ⛔ 判斷句（夠不夠下一口、本月變化）**整句都是後端算的**（live_panel.equity_view）——
      前端不准自己拿可動用去比保證金，⛔ 也不准自己算「這個月賺多少」。
   ⛔ 問不到的時候要**照實說**：⛔ 不可以把上一次的金額留在畫面上假裝是現在的
      （後端問不到就把數字清掉了，這裡只負責把那句話印出來）。
   ⚠️ 當下的數字跟著 /api/state 每 0.5 秒進來（後端每分鐘才真的問一次券商）；
      曲線是另一支 /api/account/hist，10 分鐘拿一次就夠（它一天才多一個點）。 */
const ACCT={hist:null,at:0,pending:false,err:''};
function acctPoll(){
 if(ACCT.pending) return;
 const now=Date.now();
 if(ACCT.hist!==null&&now-ACCT.at<600000) return;
 ACCT.pending=true; ACCT.at=now;
 fetch('/api/account/hist',{cache:'no-store'}).then(r=>r.ok?r.json():null).then(x=>{
   ACCT.pending=false;
   ACCT.hist=(x&&x.ok&&Array.isArray(x.rows))?x.rows:[];
   ACCT.err=(x&&!x.ok&&x.msg)?String(x.msg):'';
 }).catch(()=>{ ACCT.pending=false; ACCT.hist=ACCT.hist||[]; });
}
function acctMoney(v){
 if(v==null||isNaN(v)) return '—';
 return Math.round(Number(v)).toLocaleString('en-US');
}
function acctPM(v){
 if(v==null||isNaN(v)) return '<span>—</span>';
 const n=Math.round(Number(v));
 const cls=n>0?'up':(n<0?'down':'');
 return '<span class="'+cls+'">'+(n>0?'+':'')+n.toLocaleString('en-US')+'</span>';
}
/* 權益曲線。⛔ 只畫已經落地的那幾天（一天一點），⛔ 不把「現在」補成一個點
   —— 那一點的時間跟其他點不是同一種東西（收盤後才記）。
   ⚠️ 出入金那天標一個點：不標的話他某天匯錢進去，曲線看起來會像大賺一筆。 */
function acctSpark(rows){
 const pts=(rows||[]).filter(r=>r&&r.equity!=null&&!isNaN(r.equity));
 if(pts.length<2) return '<div class="spark"><div class="cap">'+
   (pts.length?'已經記了 1 天 —— 兩天以上才畫得出曲線':'還沒有任何一天的紀錄（每天 14:00 之後記一次）')+
   '</div></div>';
 const ys=pts.map(r=>Number(r.equity)), lo=Math.min.apply(null,ys), hi=Math.max.apply(null,ys);
 const W=260,H=42,pad=3,rng=(hi-lo)||1;
 const xy=i=>[pad+(W-2*pad)*(pts.length<2?0:i/(pts.length-1)),
              H-pad-(H-2*pad)*((ys[i]-lo)/rng)];
 const d=pts.map((_,i)=>{const p=xy(i);return (i?'L':'M')+p[0].toFixed(1)+' '+p[1].toFixed(1);}).join(' ');
 const dots=pts.map((r,i)=>{
   if(!r.deposit||isNaN(r.deposit)||Number(r.deposit)===0) return '';
   const p=xy(i);
   return '<circle cx="'+p[0].toFixed(1)+'" cy="'+p[1].toFixed(1)+'" r="2.6" '+
     'fill="var(--gold)"></circle>';
 }).join('');
 const dep=pts.some(r=>r.deposit&&Number(r.deposit)!==0);
 return '<div class="spark"><svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none" '+
   'aria-hidden="true"><path d="'+d+'" fill="none" stroke="var(--dim)" stroke-width="1.4" '+
   'stroke-linejoin="round" vector-effect="non-scaling-stroke"></path>'+dots+'</svg>'+
   '<div class="cap">'+esc(pts[0].date)+' ~ '+esc(pts[pts.length-1].date)+'　'+pts.length+' 天'+
   (dep?'　●＝那天有出入金':'')+'</div></div>';
}
function acctHTML(s){
 const E=s&&s.equity;
 if(!E) return '';                       /* 後端還沒有這一份 ⇒ 整張卡不畫（⛔ 不寫假的） */
 const hd='<div class="hd"><span class="t">帳戶總覽</span><span class="at">'+
   (E.at?esc(E.at)+'　':'')+'券商端</span></div>';
 if(!E.ok){
   return '<div class="ac">'+hd+'<div class="miss">'+esc(E.err||'問不到帳戶餘額')+'</div></div>';
 }
 const en=E.enough||{};
 const mo=E.month;
 return '<div class="ac">'+hd+
   '<div class="big">'+acctMoney(E.equity)+'</div>'+
   '<div class="sub">今日 '+acctPM(E.day_pl)+
     '（未平倉 '+acctPM(E.float_pl)+'／平倉 '+acctPM(E.settle_pl)+
     '／成本 −'+acctMoney(E.cost)+'）</div>'+
   '<div class="line'+(en.ok===false?' warn':'')+'">'+esc(en.msg||'')+'</div>'+
   /* ⭐ 2026-09-24：還撐得住幾次停損（⛔ 整句後端算） */
   ((E.cushion&&E.cushion.msg)?'<div class="line'+(E.cushion.warn?' warn':'')+'">'+esc(E.cushion.msg)+'</div>':'')+
   /* ⭐ 2026-09-26 券商每日流量（整句後端算） */
   ((E.usage&&E.usage.msg)?'<div class="line">'+esc(E.usage.msg)+'</div>':'')+
   (mo?('<div class="line">本月 '+acctPM(mo.net)+
        '（從 '+esc(mo.from)+' 起記'+(mo.deposit?'，已扣掉出入金 '+acctMoney(mo.deposit):'')+
        '）</div>'):'')+
   acctSpark(ACCT.hist)+
   '</div>';
}

/* ---------------- 右欄總繪製（2026-09-23 v3：只剩警報 ＋ 兩張唯讀小卡）----------------
   ⛔ 三塊各自比對自己的字串（setEl），⛔ 不整塊重建。 */
function paintRight(s,nf){
  const R=s.real||{};
  // ① 跨分頁警報（骨架與秒數分成兩個節點）
  setEl('xal', xalHTML(R));
  setEl('xalsec', R.stale_sec==null?'':String(R.stale_sec));
  // ② 今天的部位（唯讀）　③ 帳戶（唯讀）
  setEl('lvpos', lvPosHTML(s));
  setEl('lvacct', lvAcctHTML(s));
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
  lastTrade=''; lastStats='';
  tick(true);
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
 /* ⚠️ 2026-09-23 v3：練習成績那一區從【即時】拿掉了 ⇒ 每 5 秒一次的 /api/stats 輪詢
    也跟著拿掉（它會讀所有紀錄檔，留著就是白白在 HTTP 執行緒上做磁碟 I/O）。
    ⛔ **後端 /api/stats 與 /api/export 一個字都沒拆** —— 手機 App 與下載那條路照舊。 */
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

 // 【帳戶】這一頁的數字就在這份 s 裡（後端每分鐘換一次）⇒ 在這裡畫，
 // ⛔ 不另開一條輪詢（那會變成同一份資料問兩次）。
 if(TAB==='acct'){ acctPoll(); setEl('acct', acctHTML(s)); }

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
 // ⚠️ 2026-09-23 v3：右欄只剩兩張**唯讀**小卡（沒有長按送單那顆鈕）⇒
 //    「長按期間不准重繪」那道守衛跟著它一起拿掉了。
 paintRight(s,nf);
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
 // ⚠️ `_pts` 留著原始的 points（可能是 null＝問不到成交價）：`_net` 為了畫圖把 null 當 0，
 //    翻頁列數「真單 N 筆 ±x 點」時不可以把那個 0 當成真的點數（pagerHTML 會看 `_pts`）。
 const T=T0.concat(RT.filter(t=>t.entry!=null&&t.entry_time).map(t=>({
     time:String(t.entry_time).slice(0,5), entry:t.entry, exit:t.exit,
     dir:t.dir, _exit_time:t.exit==null?null:String(t.exit_time||''),
     _net:t.points==null?0:t.points, _pts:t.points, _real:true})));
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
 /* ⭐ 2026-09-16：「開箱」那一口**沒有停利**（s.real.tp 是 null）—— 條件只能看 sl，
    ⛔ 不可以連 tp 一起要求，不然那一口連**停損線**都不會畫（那是最該看到的一條）。 */
 const RQ=(s.real&&s.real.position&&s.real.sl!=null)?s.real:null;
 if(RQ&&live&&G.live){ [RQ.tp,RQ.sl].forEach(v=>{ if(v!=null){ hi=Math.max(hi,v); lo=Math.min(lo,v); } }); }
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
 // 搬上來，跟昨收、更新時間放在一起。
 // 2026-09-14 減字：合約名（微型臺指期貨 202609）只留左上品牌列，這裡不再寫第二次。
 // ⚠ 更新時間放在獨立的 <span id="cupd">：它每秒都在變，寫進 #chead 的字串裡
 //   會讓整個標頭（含翻頁列按鈕）每秒被重建一次 —— paintChart 會另外單獨更新它。
 const qs=live
   ? (q==='live'
      ? '<span class="live"><i></i>即時</span><span class="sep">·</span>'+
        '<span>昨收 '+f(ref)+'</span>'+
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
 // ⛔ T 是 chartSVG 拼好的「練習 ＋ 真實」（真實那幾筆帶 `_real:true`，為了畫在圖上）——
 //    「練習 N 筆」只准數練習的。2026-09-14 修：那天右欄寫「還沒有練習紀錄」、
 //    翻頁列卻寫「練習 1 筆 −N 點」，那一筆其實是自動真單。真單另外數成「真單 N 筆」；
 //    問不到成交價的真單點數留白（不拿 _net 的 0 冒充）。
 //    日期索引那條路（me.n／me.net）本來就只算 practice_trades/，不必改。
 const TP=T?T.filter(t=>!t._real):null, TR=T?T.filter(t=>t._real):[];
 const n=TP?TP.length:((me&&me.n)||0);
 const net=TP?TP.reduce((a,t)=>a+t._net,0):((me&&me.net)||0);
 const rn=TR.length, rnet=TR.some(t=>t._pts==null)?null:TR.reduce((a,t)=>a+t._net,0);
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
     /* ⚠️ 2026-09-23 v3 驗收：「練習 N 筆／未練習」拿掉 —— 【即時】已經沒有練習下單，
        留著只會讓他以為還能練（手機 App 的練習紀錄照舊在 practice_trades/，後端沒動）。
        ⛔ `n`／`net` 那兩行上面照算不刪（日期索引那條路還在用）。 */
     (rn?('<span>真單 '+rn+' 筆'+(rnet==null?''
       // 負號用 U+2212 不用 hyphen，等寬字型下跟 + 對得齊（只改這裡，不動全域的 pm()）
       :(' <b class="'+sgn(rnet)+'">'+pm(rnet).replace('-','−')+'</b> 點'))+'</span>'+
     // 這個分隔點跟著鍵盤提示一起藏（窄視窗會把提示收掉，只留一個孤零零的「·」很醜）
       '<span class="sep k">·</span>'):'')+
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
      // ⚠️ 2026-09-16【回顧】拿掉之後，這裡原本還要順手更新回顧分頁的 RV 快取 —— 那段一起拿掉了。
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
 // ⚠️ 2026-09-23 v3：練習成績／真實成績的分段窗口鈕（data-win／data-rwin）跟著那兩區
 //    一起從【即時】拿掉了 ⇒ 這裡只剩「點別的地方就把月曆收掉」。
 // 月曆浮在圖上面（以前掛在圖下面，蓋不到東西）——
 // 點到圖或其他地方就要收掉，不然它會一直擋著 K 線。點月曆自己（換月）不算。
 if(pickOpen&&!e.target.closest('.calbox')){ pickOpen=false; tick(); }
});
/* ============================================================================
   分頁狀態 ＋ 全站共用的小工具
   ----------------------------------------------------------------------------
   ⚠️ 2026-09-16【細節】(#tab-tick) 與【回顧】(#tab-review) 兩頁整個拿掉（Benson 已經不看），
      連同它們專屬的 JS／CSS —— 包含「重播練習」（Bar Replay）。
   ⛔ 留在這裡的都是**別頁也在用**的：TAB（分頁狀態）、開場動畫、esc、today10。
   ⚠️ 後端刻意留著：/api/tick/*（逐筆落地 tick_logs 是獨立的資料線）、/api/review、
      /api/replay、/api/bars（不帶 full）與 day_bars(full=False)——
      day_bars() 是【即時】也在用的同一支，只是 full 參數不同。
   ============================================================================ */
var TAB='live';
/* 進場動畫只在開站後的頭 1.1 秒有效。過了就把 class 拿掉 ——
   不然之後每次卡片內容變動（下單、成績更新）都會整張再飛一次。 */
document.body.classList.add('boot');
setTimeout(function(){ document.body.classList.remove('boot'); }, 1100);

const esc=s=>String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;')
  .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
/* ⭐⭐ 後端那幾句話裡的 `**粗體**` —— ⛔⛔ 一定要**先 esc 再換**（先換就是開一個 XSS 的洞）。
   2026-09-17 lab-qa 建議 1：確認條用 esc() 直接印，於是後端寫的 `**只做多**` 把星號
   原樣印在畫面上 —— 而那是他按下去之前看的最後一個畫面。
   ⛔ 這一支只認 `**…**` 這一種，⛔ 不是實作一個 Markdown。 */
const emb=s=>esc(s).replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>');
const today10=()=>new Date(Date.now()-new Date().getTimezoneOffset()*60000)
  .toISOString().slice(0,10);
function setTab(t){
 if(t===TAB) return;
 TAB=t;
 // 離開【自動下單】就把 5 秒輪詢停掉（它只在那一頁前景時才該跑）
 if(t!=='fire'&&AL.timer){ clearTimeout(AL.timer); AL.timer=null; }
 // 離開【模擬】也把 60 秒輪詢停掉（smEnter 的 again 自己也會檢查一次）
 if(t!=='sim'&&SM.timer){ clearTimeout(SM.timer); SM.timer=null; }
 // 離開【健檢】就把「等市場狀態算完」那條重試停掉（⛔ 它不是輪詢，只是等第一次算完）
 if(t!=='hc'&&HC.timer){ clearTimeout(HC.timer); HC.timer=null; }
 document.getElementById('tab-live').hidden=(t!=='live');
 document.getElementById('tab-acct').hidden=(t!=='acct');
 document.getElementById('tab-sim').hidden=(t!=='sim');
 document.getElementById('tab-hc').hidden=(t!=='hc');
 document.getElementById('tab-fire').hidden=(t!=='fire');
 document.querySelectorAll('.tabs button').forEach(b=>
   b.classList.toggle('on',b.getAttribute('data-tab')===t));
 // 【模擬】不掛在 500ms 的 tick 上：切進來問一次，停在這一頁時每 60 秒再問（離開就停）。
 if(t==='sim'){ smEnter(); }
 // 【健檢】⛔⛔ **一次都不准掛在 500ms／5 秒的輪詢上**：切進來才問一次，後端整天快取。
 // 只有「市場狀態還在背景算」那一種情況才會隔幾秒再問一次（見 hcEnter）。
 else if(t==='hc'){ hcEnter(); }
 // 【自動下單】同樣不掛在 500ms 的 tick 上：後端的送單、持倉監控、±100 停利停損
 // 全程都在跑，切不切進這一頁完全不影響。
 else if(t==='fire'){ alEnter(); }
 // 【帳戶】唯讀：數字跟著 500ms 的 tick 走（後端每分鐘才真的問券商），
 // 切進來先畫一次，順便把曲線拿回來（⛔ 不要等 0.5 秒才有東西，那會閃一下空白）。
 else if(t==='acct'){ acctPoll(); if(LASTS) setEl('acct', acctHTML(LASTS)); }
 // 切回即時時立刻呼叫一次 tick()（後端的報價、持倉監控、±100 自動停利停損
 // 全程都在跑，切分頁完全不影響那一條路）
 else { lastMkt=''; lastTrade=''; lastStats=''; lastWarn=''; tick(); }
}
/* 分頁切換。⛔ 這一段要留著 —— 它原本寄生在【回顧】那個 click 監聽器的第一行。 */
document.addEventListener('click',function(e){
 const tb=e.target.closest('[data-tab]'); if(tb){ setTab(tb.getAttribute('data-tab')); return; }
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

/* ══════════════ 【模擬】分頁：七條策略（⛔ 不會下單）══════════════

   ⛔ 只打 GET /api/sim/state（唯讀）。一顆按鈕、一個 POST 都沒有。
   ⛔ 規則句、月合計、今天狀態、最近一筆全部從後端來（⛔ 前端不寫死任何時刻／點數／百分比）。
   ⛔ 跟【自動下單】的真單紀錄完全分開（不讀 /api/fire/*、不共用 AL 的清單）。
   ⛔ 七條的**順序與有哪幾條**由後端決定（x.lanes 的 key 順序），⛔ 前端不寫死 lane 名字。
   ⚠️ 不掛在 500ms 的 tick 上：切進這一頁問一次，停在這一頁時每 60 秒再問一次（離開就停）。
   ⚠️ 「沒變就別動 DOM」用節點上快取的字串比（⛔ 不讀回 innerHTML 比，見 CLAUDE.md）。
   ⚠️ 請求帶流水號，只認最後一次的回應。
*/
var SM={seq:0,dseq:0,timer:null,err:'',keys:'',det:'',bound:false};
const SMWD=['日','一','二','三','四','五','六'];
function smSet(id,html){ const e=document.getElementById(id); if(!e) return; if(e._smh!==html){ e._smh=html; e.innerHTML=html; } }
function smPts(v){ if(v==null) return '—'; return (v>0?'+':v<0?'−':'')+Math.abs(v).toLocaleString('en-US',{maximumFractionDigits:1}); }
function smCls(v){ return v>0?'up':v<0?'down':''; }
function smPx(v){ return v==null?'—':Number(v).toLocaleString('en-US',{maximumFractionDigits:1}); }
function smDay(s){ const p=String(s||'').split('-').map(Number); if(p.length<3) return esc(s);
  return esc(s.slice(5))+'（'+SMWD[new Date(p[0],p[1]-1,p[2]).getDay()]+'）'; }
// 內頁的清單跨了兩年多 ⇒ ⛔ 一定要帶年份（只有月日會把 2024 跟 2026 看成同一天）
function smDayY(s){ const p=String(s||'').split('-').map(Number); if(p.length<3) return esc(s);
  return esc(s)+'（'+SMWD[new Date(p[0],p[1]-1,p[2]).getDay()]+'）'; }
// 後端那幾句說明裡的 **粗體** ⇒ <b>。⛔ 先 esc 再換，順序反過來就等於開了 HTML 注入。
function smMd(s){ return esc(String(s==null?'':s)).replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>'); }
function smMonths(ms,all){
  let h='<div class="sm-months">';
  (ms||[]).forEach(m=>{
    // ⛔ 筆數旁邊一定要帶「算到幾天」：資料補得多寡不同時，兩條的月合計**不可比** ——
    //    只寫「2 筆」看不出來是「這個月只算到 9 天」還是「20 天只做了 2 筆」（lab-qa 退件 S2）。
    // ⭐ 內頁（all）那張月表點得下去 ⇒ 右邊的逐日紀錄跳到那個月（2026-09-17 Benson 交辦）。
    //    ⛔ 卡上那張**不可以**變成可點的：整張卡本來就是一顆「點進去」的鈕，
    //       月份再吃掉一次點擊，他會點不開內頁。
    const hit=all?' hit" data-m="'+esc(m.month)+'" role="button" tabindex="0':'';
    h+='<div class="sm-m'+(m.this?' this':'')+hit+'"><span>'+esc(m.label)+(all&&m.this?'（本月）':'')
      +'<i>'+m.trades+' 筆／'+m.days+' 天</i></span>'
      +'<b class="lb-mono '+smCls(m.points)+'">'+(m.days?smPts(m.points):'—')+'</b></div>';
  });
  return h+'</div>';
}
function smLane(L){
  if(!L) return '<div class="sm-empty">讀不到</div>';
  const t=L.today||{};
  // ⭐ 由上而下：名字 → 每月累計點數（他最在意的，放第一個）→ 今天 → 點進去的提示
  // ⭐ 2026-09-17：「最近一筆」與規則句搬進內頁（Benson 要的）—— 卡上只留他天天在看的那三塊。
  let h='<div class="sm-lt"><b>'+esc(L.name)+'</b><small>資料：'+esc(L.src)+'</small></div>'
    +'<div class="sm-mh">每月累計點數</div>'+smMonths(L.months,false);
  h+='<div class="sm-lh">今天</div><div class="sm-today">'+(t.date?'<em>'+smDay(t.date)+'</em>':'')
    +(t.row?(esc(t.row.decision)+(t.row.points!=null?'　<span class="'+smCls(t.row.points)+'">'+smPts(t.row.points)+' 點</span>':'')):esc(t.msg||''))+'</div>';
  if(L.pending&&L.pending.length){
    h+='<div class="sm-pend">等資料：'+L.pending.map(p=>smDay(p.date)+' '+esc(p.msg)).join('；')+'</div>';
  }
  if(L.fetch&&L.fetch.msg){ h+='<div class="sm-pend">補資料：'+esc(L.fetch.msg)+(L.fetch.at?'（'+esc(L.fetch.at.slice(5,16))+'）':'')+'</div>'; }
  // ⛔ 正在掃箱子寬度歷史時要說「還在算」，⛔ 不可以讓他以為是「沒有資料」
  if(L.scan){ h+='<div class="sm-pend">'+esc(L.scan)+'</div>'; }
  return h+'<div class="sm-more">點一下看全部紀錄與定義 →</div>';
}

/* ── 點進去一條策略的內頁（2026-09-17 加）────────────────────────────
   ⛔ 只打 GET /api/sim/lane?key=…（唯讀），⛔ 只有點下去才打（一次幾百列，不准併進 60 秒輪詢）。
   ⛔ 白話、逐項定義、規則句一律**後端給**（跟卡片同一條鐵律：前端不寫死時刻／點數／百分比）。
   ⚠️ 「回填」與「即時」要看得出來：`calc` 有值＝事後重算的，沒有＝面板當天即時算的。 */
function smDetRow(r){
  const trade=r.decision==='做多'||r.decision==='做空';
  // data-m ＝這一列屬於哪個月（月表點下去要靠它找到第一列）；data-d ＝ 哪一天（點下去看那天的圖）
  return '<tr data-m="'+esc(String(r.date||'').slice(0,7))+'" data-d="'+esc(String(r.date||''))+'">'
    +'<td class="d">'+smDayY(r.date)+'</td>'
    +'<td class="k '+(trade?(r.decision==='做多'?'up':'down'):'none')+'">'+esc(r.decision)+'</td>'
    +'<td class="x">'+(trade?smPx(r.entry)+' → '+smPx(r.exit)+'（'+esc(r.exit_reason||'')+'）':'')+'</td>'
    +'<td class="p '+(trade?smCls(r.points):'')+'">'+(trade?smPts(r.points):'')+'</td>'
    +'<td class="why">'+esc(r.reason||'')+'</td>'
    +'<td class="src'+(r.calc?' bf':'')+'">'+(r.calc?'回填':'即時')+'</td></tr>';
}
function smDetPaint(d){
  if(!d){ smSet('smdet','<span class="sm-back" role="button" tabindex="0">← 全部策略</span>'
    +'<div class="sm-empty">'+esc(SM.err||'讀取中…')+'</div>'); return; }
  const t=d.total||{}, dt=d.detail||{};
  let h='<div class="sm-dhead"><span class="sm-back" role="button" tabindex="0">← 全部策略</span>'
    +'<h2>'+esc(d.name)+'</h2><small>資料：'+esc(d.src)+'</small></div>';
  h+='<div class="sm-plain">'+smMd(dt.plain)+'</div>';
  if(d.wired===false){ h+='<div class="sm-pend">⚠️ 規則函式沒有接上 —— 下面的說明不完整</div>'; }
  h+='<div class="sm-mh">這一條是怎麼算的</div><dl class="sm-steps">';
  (dt.steps||[]).forEach(s=>{ h+='<dt>'+esc(s.k)+'</dt><dd>'+smMd(s.v)+'</dd>'; });
  h+='</dl>';
  h+='<div class="sm-tot">合計 '+esc(t.d0||'—')+' ~ '+esc(t.d1||'—')+'：算過 <b>'+(t.days||0)
    +'</b> 天、做了 <b>'+(t.trades||0)+'</b> 筆、<b class="'+smCls(t.points)+'">'+smPts(t.points)+'</b> 點'
    +(t.backfill?'　（其中 '+t.backfill+' 天是回填 —— 事後用同一份規則、同一份逐筆重算的；'
      +'其餘是面板當天即時算的）':'')+'</div>';
  h+='<div class="sm-cols"><div><div class="sm-mh">每月累計點數</div>'+smMonths(d.months,true)+'</div>'
    +'<div><div class="sm-mh">逐日紀錄（'+(d.rows||[]).length+' 天，新到舊．點一列看那天的圖）</div>'
    +'<div class="sm-scroll"><table class="sm-tbl"><thead><tr><th>日期</th><th>判斷</th>'
    +'<th>進 → 出</th><th>點數</th><th>說明</th><th>怎麼算的</th></tr></thead><tbody>'
    +((d.rows||[]).map(smDetRow).join('')||'<tr><td colspan="6" class="why">還沒有算好的日子</td></tr>')
    +'</tbody></table></div></div></div>';
  h+='<div class="sm-foot">'+esc(d.note||'')+'　規則原句：'+esc(d.rule||'')+'</div>';
  smSet('smdet',h);
}
/* 月表點下去 ⇒ 右邊的逐日紀錄捲到那個月的第一天（2026-09-17 Benson 交辦）。
   ⛔ 不用 scrollIntoView：它會把**整頁**一起捲走（內頁上半的定義就被推出畫面了），
      這裡要捲的只有 .sm-scroll 這個容器自己 ⇒ 用 getBoundingClientRect 的差值。
   ⚠️ 表頭是 position:sticky ⇒ 要**多扣掉表頭的高度**，不然跳過去的第一列被壓在表頭底下。
   ⚠️ m 只准是 YYYY-MM：它會被丟進 querySelector，先驗過才不會被亂七八糟的值打壞。 */
function smJump(m){
  if(!/^\d{4}-\d{2}$/.test(m||'')) return;
  const dt=document.getElementById('smdet'); if(!dt) return;
  const sc=dt.querySelector('.sm-scroll'), tr=dt.querySelector('.sm-tbl tbody tr[data-m="'+m+'"]');
  if(!sc||!tr) return;
  const head=dt.querySelector('.sm-tbl thead');
  sc.scrollTop+=tr.getBoundingClientRect().top-sc.getBoundingClientRect().top
                -(head?head.getBoundingClientRect().height:0);
  // 跳到哪個月要看得出來（月表那一列亮起來、那個月的逐日整段淡淡上色）
  dt.querySelectorAll('.sm-m.on').forEach(e=>e.classList.remove('on'));
  const row=dt.querySelector('.sm-m[data-m="'+m+'"]'); if(row) row.classList.add('on');
  dt.querySelectorAll('.sm-tbl tbody tr.hl').forEach(e=>e.classList.remove('hl'));
  dt.querySelectorAll('.sm-tbl tbody tr[data-m="'+m+'"]').forEach(e=>e.classList.add('hl'));
}
/* ── 點一天看圖（2026-09-18）────────────────────────────────────────
   ⛔ 只打 GET /api/sim/daychart?key=&date=（唯讀、點下去才打）。
   ⛔ 圖是**自己畫的獨立小圖**，⛔ 不借即時分頁那張（#csvg／paintChart）：那張綁著縮放拖曳、月曆，
      而且真單在跑的時候它也在用 —— 借來借去最容易把正在交易的那張弄壞。
   ⛔ 進出場時刻、價格、參考線全部**後端給**；這裡一個時刻都不寫死（前端只負責把它們畫上去）。
   ⚠️ 出場時刻找不到（後端給 null）⇒ 只畫水平的出場價，⛔ 不猜一個時間。 */
var SMD={seq:0,list:[],i:-1};
function smDayClose(){
  SMD.seq++; SMD.i=-1;
  const e=document.getElementById('smday'); if(e){ e.hidden=true; e._smh=null; e.innerHTML=''; }
  document.querySelectorAll('#smdet .sm-tbl tbody tr.cur').forEach(x=>x.classList.remove('cur'));
}
function smDayOpen(day){
  if(!SM.det||!/^\d{4}-\d{2}-\d{2}$/.test(day||'')) return;
  const rows=[...document.querySelectorAll('#smdet .sm-tbl tbody tr[data-d]')];
  SMD.list=rows.map(x=>x.getAttribute('data-d'));
  SMD.i=SMD.list.indexOf(day);
  rows.forEach(x=>x.classList.toggle('cur',x.getAttribute('data-d')===day));
  const e=document.getElementById('smday'); if(!e) return;
  e.hidden=false;
  smSet('smday','<div class="sm-dbox"><div class="sm-empty">讀取 '+esc(day)+' 的圖…</div></div>');
  const my=++SMD.seq, k=SM.det;
  fetch('/api/sim/daychart?key='+encodeURIComponent(k)+'&date='+encodeURIComponent(day),{cache:'no-store'})
    .then(r=>r.json().catch(()=>({})).then(b=>({s:r.status,b}))).then(({s,b})=>{
      if(my!==SMD.seq) return;
      if(s!==200||!b||!b.ok){ smSet('smday','<div class="sm-dbox">'+smDayHead(day,null)
        +'<div class="sm-empty">'+esc((b&&b.msg)||('讀不到那天的圖（'+s+'）'))+'</div></div>'); return; }
      smSet('smday','<div class="sm-dbox">'+smDayHead(day,b)+smDayChart(b)+'</div>');
    }).catch(()=>{ if(my!==SMD.seq) return;
      smSet('smday','<div class="sm-dbox">'+smDayHead(day,null)+'<div class="sm-empty">讀不到那天的圖（連不到面板）</div></div>'); });
}
function smDayStep(dir){
  // 清單是新到舊 ⇒ 「前一天」＝ 往下一列（index +1）
  const j=SMD.i+(dir<0?1:-1);
  if(j>=0&&j<SMD.list.length) smDayOpen(SMD.list[j]);
}
function smDayHead(day,b){
  const prevOk=SMD.i<SMD.list.length-1, nextOk=SMD.i>0;
  const trade=b&&(b.decision==='做多'||b.decision==='做空');
  let h='<div class="sm-dtop">'
    +'<span class="sm-nav'+(prevOk?'':' off')+'" role="button" tabindex="0" data-nav="-1">← 前一天</span>'
    +'<h3>'+smDayY(day)+(b?'　'+esc(b.name||''):'')+'</h3>';
  if(b) h+='<span class="k '+(trade?(b.decision==='做多'?'up':'down'):'none')+'">'+esc(b.decision||'')+'</span>'
    +(trade?'<span class="p '+smCls(b.points)+'">'+smPts(b.points)+' 點</span>':'')
    +(b.calc?'<small class="src bf">回填</small>':'');
  h+='<span class="sp"></span>'
    +'<span class="sm-nav'+(nextOk?'':' off')+'" role="button" tabindex="0" data-nav="1">後一天 →</span>'
    +'<span class="sm-nav" role="button" tabindex="0" data-nav="0">✕ 關閉</span></div>';
  if(b&&b.reason) h+='<div class="sm-dwhy">'+esc(b.reason)+'</div>';
  return h;
}
function smDayChart(b){
  const bars=b.bars||[];
  if(!bars.length) return '<div class="sm-empty">畫不出圖</div>'+smDayNotes(b);
  const W=1100,H=440,L=10,R=128,T=14,B=30,pw=W-L-R,ph=H-T-B;
  // y 範圍：K 棒 ＋ 參考線 ＋ 進出場價，全部塞得下
  let lo=Infinity,hi=-Infinity;
  bars.forEach(x=>{ lo=Math.min(lo,x[3]); hi=Math.max(hi,x[2]); });
  (b.lines||[]).concat(b.marks||[]).forEach(x=>{ if(x.price!=null){ lo=Math.min(lo,x.price); hi=Math.max(hi,x.price); } });
  const pad=(hi-lo)*0.06||10; lo-=pad; hi+=pad;
  const n=bars.length, cw=pw/n;
  const X=i=>L+cw*(i+0.5), Y=p=>T+ph*(hi-p)/(hi-lo);
  const at=s=>{ if(!s) return -1; const m=String(s).slice(0,5); return bars.findIndex(x=>x[0]===m); };
  let s='<svg viewBox="0 0 '+W+' '+H+'" xmlns="http://www.w3.org/2000/svg">';
  // 區塊（開箱的箱子那 5 分鐘）
  (b.zones||[]).forEach(z=>{ const a=at(z.from), c=at(z.to); if(a<0||c<0) return;
    s+='<rect x="'+(L+cw*a)+'" y="'+T+'" width="'+(cw*(c-a+1))+'" height="'+ph+'" fill="var(--gold-soft)"/>'
      +'<text x="'+(L+cw*a+3)+'" y="'+(T+12)+'" font-size="11" fill="var(--gold)">'+esc(z.label)+'</text>'; });
  // 價格格線（5 條）
  for(let k=0;k<=4;k++){ const p=lo+(hi-lo)*k/4, y=Y(p);
    s+='<line x1="'+L+'" x2="'+(L+pw)+'" y1="'+y+'" y2="'+y+'" stroke="var(--line-soft)"/>'
      +'<text x="'+(L+pw+R-6)+'" y="'+(y+4)+'" font-size="10.5" text-anchor="end" fill="var(--ghost)">'+smPx(Math.round(p))+'</text>'; }
  // 時間刻度：大約 8 個
  const step=Math.max(1,Math.round(n/8));
  for(let i=0;i<n;i+=step) s+='<text x="'+X(i)+'" y="'+(H-10)+'" font-size="10.5" text-anchor="middle" fill="var(--faint)">'+esc(bars[i][0])+'</text>';
  // K 棒（台股：紅漲綠跌）
  bars.forEach((x,i)=>{ const up=x[4]>=x[1], col=up?'var(--up)':'var(--down)';
    const yo=Y(x[1]), yc=Y(x[4]), bw=Math.max(1,cw*0.62);
    s+='<line x1="'+X(i)+'" x2="'+X(i)+'" y1="'+Y(x[2])+'" y2="'+Y(x[3])+'" stroke="'+col+'" stroke-width="1"/>'
      +'<rect x="'+(X(i)-bw/2)+'" y="'+Math.min(yo,yc)+'" width="'+bw+'" height="'+Math.max(1,Math.abs(yc-yo))+'" fill="'+col+'"/>'; });
  // 參考線（停利／停損／箱子／參考價）
  const LC={tp:'var(--gold)',sl:'var(--dim)',box:'var(--ghost)',ref:'var(--faint)'};
  (b.lines||[]).forEach(ln=>{ const y=Y(ln.price), c=LC[ln.style]||'var(--dim)';
    s+='<line x1="'+L+'" x2="'+(L+pw)+'" y1="'+y+'" y2="'+y+'" stroke="'+c+'" stroke-width="1.3" stroke-dasharray="6 4"/>'
      +'<text x="'+(L+pw+6)+'" y="'+(y-3)+'" font-size="11" fill="'+c+'">'+esc(ln.label)+'</text>'
      +'<text x="'+(L+pw+6)+'" y="'+(y+10)+'" font-size="10" fill="'+c+'">'+smPx(ln.price)+'</text>'; });
  // 進出場
  const ms=b.marks||[], en=ms.find(m=>m.kind==='entry'), ex=ms.find(m=>m.kind==='exit');
  const ie=en?at(en.at):-1, ix=ex?at(ex.at):-1;
  if(ie>=0&&ix>=0&&en.price!=null&&ex.price!=null)
    s+='<line x1="'+X(ie)+'" x2="'+X(ix)+'" y1="'+Y(en.price)+'" y2="'+Y(ex.price)+'" stroke="var(--text)" stroke-width="1.2" stroke-dasharray="2 3" opacity=".7"/>';
  if(en&&ie>=0){ const x=X(ie), y=Y(en.price), d=en.dir||1, tip=d>0?y+4:y-4, base=d>0?y+16:y-16;
    s+='<polygon points="'+x+','+tip+' '+(x-7)+','+base+' '+(x+7)+','+base+'" fill="var(--gold)" stroke="var(--bg)" stroke-width="1"/>'
      +'<text x="'+(x+10)+'" y="'+(d>0?base+4:base+2)+'" font-size="12" font-weight="650" fill="var(--gold)">'+esc(en.label)+'（'+esc(en.at)+'）</text>'; }
  if(ex&&ix>=0&&ex.price!=null){ const x=X(ix), y=Y(ex.price), c=(b.points||0)>=0?'var(--up)':'var(--down)';
    s+='<circle cx="'+x+'" cy="'+y+'" r="6" fill="var(--bg)" stroke="'+c+'" stroke-width="2.4"/>'
      +'<text x="'+(x-10)+'" y="'+(y-10)+'" font-size="12" font-weight="650" text-anchor="end" fill="'+c+'">'+esc(ex.label)+'（'+esc(ex.at)+'）</text>'; }
  else if(ex&&ex.price!=null){   // ⛔ 出場時刻找不到 ⇒ 只畫價格，不猜時間
    s+='<text x="'+(L+pw-4)+'" y="'+(Y(ex.price)-5)+'" font-size="11.5" text-anchor="end" fill="var(--dim)">'+esc(ex.label)+'（時刻不明）</text>'; }
  s+='</svg>';
  let leg='<div class="sm-dleg">';
  if(en) leg+='<span><b style="color:var(--gold)">▲</b> 進場</span>';
  if(ex) leg+='<span><b style="color:var(--up)">○</b> 出場（紅＝賺、綠＝賠）</span>';
  (b.lines||[]).forEach(ln=>{ leg+='<span><i style="border-color:'+(LC[ln.style]||'var(--dim)')+'"></i>'+esc(ln.label)+'</span>'; });
  if((b.zones||[]).length) leg+='<span><i style="border-color:var(--gold-soft);border-top-width:8px"></i>箱子的時段</span>';
  return '<div class="sm-dchart">'+s+'</div>'+leg+'</div>'+smDayNotes(b);
}
function smDayNotes(b){
  return (b&&b.notes&&b.notes.length)?'<div class="sm-dnote">'+b.notes.map(esc).join('<br>')+'</div>':'';
}
function smDetOpen(k){
  if(!k) return;
  SM.det=k;
  const c=document.getElementById('smcard'), e=document.getElementById('smdet');
  if(c) c.hidden=true;
  if(e){ e.hidden=false; e._smh=null; }
  SM.err=''; smDetPaint(null);
  const my=++SM.dseq;
  fetch('/api/sim/lane?key='+encodeURIComponent(k),{cache:'no-store'})
    .then(r=>r.json().catch(()=>({})).then(b=>({s:r.status,b}))).then(({s,b})=>{
      if(my!==SM.dseq||SM.det!==k) return;
      if(s!==200){ SM.err=(b&&b.msg)||('模擬紀錄讀取失敗（'+s+'）'); smDetPaint(null); return; }
      // ⛔ 200 但看不懂 ⇒ 也要**說出來**：停在「讀取中…」等於安靜地壞掉
      if(!b||!b.rows){ SM.err='模擬紀錄讀取失敗（回應看不懂）'; smDetPaint(null); return; }
      SM.err=''; smDetPaint(b);
    }).catch(()=>{ if(my!==SM.dseq||SM.det!==k) return;
      SM.err='模擬紀錄讀取失敗（連不到面板）'; smDetPaint(null); });
}
function smDetClose(){
  smDayClose();                         // 那天的圖是掛在這一條底下的 ⇒ 一起收
  SM.det=''; SM.dseq++;                 // ⛔ 流水號往前推：還在路上的那個回應回來時不准再畫
  const c=document.getElementById('smcard'), e=document.getElementById('smdet');
  if(e){ e.hidden=true; e._smh=null; }
  if(c) c.hidden=false;
  SM.err=''; smLoad();
}
function smBind(){
  if(SM.bound) return;
  SM.bound=true;
  const el=document.getElementById('smlanes'), dt=document.getElementById('smdet');
  // ⛔ 用委派：七條的骨架會被重建，直接掛在每一條上的事件會跟著不見
  // ⛔ lane 的 key 從節點 id 取（id＝"sm-"＋key），⛔ 前端不寫死任何一條的名字
  const open=n=>{ if(n&&n.id&&n.id.indexOf('sm-')===0) smDetOpen(n.id.slice(3)); };
  if(el){
    el.addEventListener('click',ev=>open(ev.target.closest('.sm-lane')));
    el.addEventListener('keydown',ev=>{ if(ev.key!=='Enter'&&ev.key!==' ') return;
      const n=ev.target.closest('.sm-lane'); if(n){ ev.preventDefault(); open(n); } });
  }
  if(dt){
    dt.addEventListener('click',ev=>{
      if(ev.target.closest('.sm-back')) return smDetClose();
      const m=ev.target.closest('.sm-m[data-m]'); if(m) return smJump(m.getAttribute('data-m'));
      const tr=ev.target.closest('tr[data-d]'); if(tr) smDayOpen(tr.getAttribute('data-d'));
    });
    dt.addEventListener('keydown',ev=>{
      if(ev.key!=='Enter'&&ev.key!==' ') return;
      if(ev.target.closest('.sm-back')){ ev.preventDefault(); return smDetClose(); }
      const m=ev.target.closest('.sm-m[data-m]'); if(m){ ev.preventDefault(); smJump(m.getAttribute('data-m')); }
    });
  }
  const dy=document.getElementById('smday');
  if(dy){
    dy.addEventListener('click',ev=>{
      const nv=ev.target.closest('[data-nav]');
      if(nv){ const v=+nv.getAttribute('data-nav'); return v===0?smDayClose():smDayStep(v); }
      if(ev.target===dy) smDayClose();          // 點到灰色背景 ⇒ 關掉
    });
    dy.addEventListener('keydown',ev=>{ if(ev.key!=='Enter'&&ev.key!==' ') return;
      const nv=ev.target.closest('[data-nav]'); if(!nv) return;
      ev.preventDefault(); const v=+nv.getAttribute('data-nav'); v===0?smDayClose():smDayStep(v); });
  }
  // ⭐ Esc 一次只退一層：先關「那天的圖」，再關「那一條的內頁」
  document.addEventListener('keydown',ev=>{
    const open=dy&&!dy.hidden;
    if(ev.key==='Escape'){ if(open) return smDayClose(); if(SM.det) smDetClose(); return; }
    if(open&&ev.key==='ArrowLeft'){ ev.preventDefault(); smDayStep(-1); }
    else if(open&&ev.key==='ArrowRight'){ ev.preventDefault(); smDayStep(1); }
  });
}
function smPaint(x){
  if(!x||!x.lanes){ smSet('smlanes',''); SM.keys=''; smSet('smfoot','<span>'+esc(SM.err||'讀取中…')+'</span>'); return; }
  smSet('smnote',esc(x.note||''));
  // 條數／順序由後端決定。⚠️ 骨架只在「有哪幾條」變動時重建，平常每條各自比自己的字串
  //    —— 每分鐘把整塊 innerHTML 換掉會把捲動位置與剛畫好的內容一起丟掉。
  const keys=Object.keys(x.lanes).filter(k=>/^[a-z0-9_]+$/.test(k));
  if(SM.keys!==keys.join('|')){
    SM.keys=keys.join('|');
    const el=document.getElementById('smlanes');
    // role/tabindex：整張卡是一個可以點、也可以用鍵盤打開的東西（⛔ 不用 <button>：
    //    這一頁刻意一顆按鈕都沒有，而這只是導覽、不會動到任何資料）
    if(el){ el.innerHTML=keys.map(k=>'<div class="sm-lane" id="sm-'+k+'" role="button" tabindex="0"></div>').join(''); el._smh=null; }
  }
  keys.forEach(k=>smSet('sm-'+k,smLane(x.lanes[k])));
  const f=x.file||{};
  smSet('smcount',esc('算到 '+(x.last_step_at||'—')));
  const bad=(x.errors>0)||(f.bad>0)||(f.dup>0)||(f.eq_ok===false)||(x.hist_bad>0)||!x.wired;
  const foot='紀錄 '+(f.ok||0)+' 列'+(f.bad?'、壞列 '+f.bad:'')+(f.dup?'、重複 '+f.dup:'')
    +(f.eq_ok===false?'、⚠️ 列數對不上':'')+(x.hist_bad?'、fast_hist 壞列 '+x.hist_bad:'')
    +(x.wired?'':'、⚠️ 規則函式沒有接上')
    +(x.errors?'、背景錯誤 '+x.errors+' 次（最近：'+(x.last_err||'')+'）':'');
  const e=document.getElementById('smfoot'); if(e) e.classList.toggle('bad',!!bad);
  smSet('smfoot',esc(foot));
}
function smLoad(){
  const my=++SM.seq;
  fetch('/api/sim/state',{cache:'no-store'}).then(r=>r.json().catch(()=>({})).then(b=>({s:r.status,b}))).then(({s,b})=>{
    if(my!==SM.seq) return;
    if(s!==200){ SM.err=(b&&b.msg)||('模擬讀取失敗（'+s+'）'); smPaint(null); return; }
    // ⛔ 200 但看不懂 ⇒ 也要**說出來**：停在「讀取中…」等於安靜地壞掉（這個專案明令禁止）
    if(!b||!b.lanes){ SM.err='模擬讀取失敗（回應看不懂）'; smPaint(null); return; }
    SM.err=''; smPaint(b);
  }).catch(()=>{ if(my!==SM.seq) return; SM.err='模擬讀取失敗（連不到面板）'; smPaint(null); });
}
function smEnter(){
  smBind();
  smLoad();
  if(SM.timer) clearTimeout(SM.timer);
  const again=()=>{ SM.timer=null; if(TAB!=='sim') return; smLoad(); SM.timer=setTimeout(again,60000); };
  SM.timer=setTimeout(again,60000);
}

/* ══════════════ 迷你圖（【健檢】與【自動下單】共用）══════════════
   ⛔ 一份就好（兩頁各寫一份 ＝ 兩把尺）。紅上綠下、左舊右新（跟全站的紅漲綠跌一致）。 */
function sparkBars(vals,h){
  h=h||54;
  const n=(vals||[]).length; if(!n) return '';
  const W=600, bw=W/n; let mx=0;
  vals.forEach(v=>{ mx=Math.max(mx,Math.abs(Number(v)||0)); }); mx=mx||1;
  const mid=h/2;
  let s='<svg viewBox="0 0 '+W+' '+h+'" preserveAspectRatio="none" style="height:'+h+'px">'+
    '<line x1="0" y1="'+mid+'" x2="'+W+'" y2="'+mid+'" stroke="#242C38" stroke-width="1"/>';
  vals.forEach((v0,i)=>{
    const v=Number(v0)||0, hh=Math.abs(v)/mx*(mid-4), col=v>=0?'#EE5A54':'#34B37E';
    s+='<rect x="'+(i*bw+bw*0.18).toFixed(2)+'" y="'+(v>=0?mid-hh:mid).toFixed(2)+
       '" width="'+(bw*0.64).toFixed(2)+'" height="'+Math.max(1,hh).toFixed(2)+'" fill="'+col+'" rx="1"/>';
  });
  return s+'</svg>';
}
function sparkLine(vals,h,col){
  h=h||46; col=col||'#8D95A3';
  const n=(vals||[]).length; if(n<2) return '';
  const W=600, mx=Math.max.apply(null,vals), mn=Math.min.apply(null,vals), rg=(mx-mn)||1;
  const d=vals.map((v,i)=>(i?'L':'M')+(i/(n-1)*W).toFixed(1)+' '+(h-4-(v-mn)/rg*(h-8)).toFixed(1)).join(' ');
  return '<svg viewBox="0 0 '+W+' '+h+'" preserveAspectRatio="none" style="height:'+h+'px">'+
    '<path d="'+d+'" fill="none" stroke="'+col+'" stroke-width="1.6" vector-effect="non-scaling-stroke"/></svg>';
}

/* ══════════════ 【健檢】分頁（2026-09-23 加）══════════════

   ⛔⛔ **這一頁不准出現預測、勝率、期望值、訊號強度、買賣建議**（CLAUDE.md 開頭那條鐵律）。
   ⛔⛔ **只打一次** GET /api/health/state（唯讀）——⛔ 不掛在 500ms 的 tick、
      ⛔ 也不掛在【自動下單】那條 5 秒輪詢上。後端整天快取（來源檔 mtime 當快取鍵）。
      唯一的例外：市場狀態那一區第一次要讀兩個大檔（後端背景執行緒在算）⇒
      `market_ready=false` 時隔 HC_RETRY 再問一次，⛔ 算完就停、⛔ 離開這一頁也停。
   ⛔ 一顆會動到錢的鈕都沒有：沒有表單、沒有送出鈕、沒有武裝那幾顆的觸發屬性、不打任何 POST。
   ⛔ 燈號、門檻、百分位**全部由後端決定**（health.py）——前端算一份就是第二把尺。 */
var HC={data:null, err:'', seq:0, timer:null, tries:0};
const HC_RETRY=3000, HC_TRIES=20;      /* 最多等 60 秒（⛔ 不無限重試） */
function hcPts(v){ return v==null?'—':((v>0?'+':v<0?'−':'')+Math.abs(v).toLocaleString('en-US',{maximumFractionDigits:1})); }
function hcCls(v){ return v>0?'up':v<0?'down':'flat'; }
function hcNum(v,dp){ return v==null?'—':Number(v).toFixed(dp==null?2:dp); }

/* 一張策略卡。⛔ 筆數不足 ⇒ **留白**，⛔ 不准用比較少的筆數硬算一個數字。 */
function hcCard(S){
  const head='<div class="hc-top"><div class="hc-nm">'+esc(S.name||'')+'<i>'+esc(S.sub||'')+'</i></div>'+
    '<span class="hc-lamp '+esc(S.lamp||'na')+'"><b></b>'+esc(S.lamp_word||'')+'</span></div>';
  /* 真單那一半：⛔ 跟模擬**分開寫**，⛔ 不准混成一個數字。 */
  const R=S.real||null;
  const real=R
    ? '<div class="hc-real"><b>真單</b>：'+esc(R.msg||'')+'</div>'
    : '<div class="hc-real"><b>真單</b>：這一條沒有真單（⛔ 不下單，只做前瞻考試）。</div>';
  if(!S.ready)
    return '<div class="hc-card">'+head+
      '<div class="hc-na">目前只有 <b>'+esc(String(S.n||0))+' 筆</b>，要 <b>'+
        esc(String(S.need||15))+' 筆</b>才算得出來。<br>'+
      '在那之前這裡<b>留白</b>，⛔ 不用比較少的筆數硬算一個數字給你看。</div>'+real+'</div>';
  const half=(S.avg_all==null)?null:S.avg_all/2;
  return '<div class="hc-card'+(S.lamp!=='ok'?' warn':'')+'">'+head+
    '<div class="hc-nums">'+
      '<div class="c"><div class="hc-k">全部平均</div>'+
        '<div class="hc-v '+hcCls(S.avg_all)+'">'+hcPts(S.avg_all)+'<small>點</small></div>'+
        '<div class="hc-d">共 '+esc(String(S.n||0))+' 筆</div></div>'+
      '<div class="c"><div class="hc-k">最近 30 筆</div>'+
        '<div class="hc-v '+hcCls(S.avg30)+'">'+hcPts(S.avg30)+'<small>點</small></div>'+
        '<div class="hc-d">每筆平均</div></div>'+
      '<div class="c"><div class="hc-k">最近 15 筆</div>'+
        '<div class="hc-v '+hcCls(S.avg15)+'">'+hcPts(S.avg15)+'<small>點</small></div>'+
        '<div class="hc-d">'+(half==null?'每筆平均':('全部平均的一半＝'+hcPts(half)))+'</div></div>'+
    '</div>'+
    '<div class="hc-spark">'+sparkBars(S.recent||[],56)+'</div>'+
    '<div class="hc-foot"><span>近 30 筆（左舊右新）</span><span class="sep">・</span>'+
      '<span>'+esc(S.src||'模擬')+'</span><span class="sep">・</span>'+
      '<span>'+esc(String(S.d0||'—'))+' ~ '+esc(String(S.d1||'—'))+'</span></div>'+
    real+'</div>';
}

/* 一張市場狀態小卡。⛔ 只放數字與位置，⛔ 不准寫「偏高／偏低／要小心」這種評語。 */
function hcMktCard(M){
  const dp=(M.dp==null?2:M.dp);
  const band=(M.pct==null)?''
    : '<div class="hc-band"><i style="left:10%;right:10%"></i>'+
      '<u style="left:calc('+Math.max(0,Math.min(100,M.pct))+'% - 1px)"></u></div>'+
      '<div class="hc-scale"><span>'+hcNum(M.lo,dp)+'</span><span>過去一年</span>'+
        '<span>'+hcNum(M.hi,dp)+'</span></div>'+
      '<div class="hc-pos">過去一年的第 <b>'+esc(String(M.pct))+'</b> 百分位</div>';
  const lines=(M.lines||[]).filter(x=>x).map(x=>'<div>'+esc(x)+'</div>').join('');
  return '<div class="hc-m'+(M.flag?' warn':'')+'">'+
    '<div class="t">'+esc(M.title||'')+'<i>'+esc(M.note||'')+'</i></div>'+
    '<div class="v">'+hcNum(M.value,dp)+(M.unit?'<small>'+esc(M.unit)+'</small>':'')+'</div>'+
    (M.flag?'<div class="hc-pos"><span class="hc-lamp '+esc(M.flag)+'"><b></b>'+
       esc(M.flag_word||'')+'</span> '+esc(M.flag_note||'')+'</div>':'')+
    band+
    (lines?'<div class="hc-lines">'+lines+'</div>':'')+
    (M.as_of?'<div class="hc-foot"><span>資料到 '+esc(M.as_of)+'</span></div>':'')+
    ((M.series&&M.series.length>1)?'<div class="hc-spark">'+sparkLine(M.series,42,'#8D95A3')+'</div>':'')+
    '</div>';
}

function hcPaint(){
  const D=HC.data;
  if(!D){
    setEl('hcstamp',''); setEl('hcsrc','');
    setEl('hccards','<div class="hc-na">'+esc(HC.err||'載入中…')+'</div>');
    setEl('hcleg',''); setEl('hcmkt',''); setEl('hcfoot','');
    return;
  }
  setEl('hcstamp', esc(D.as_of||''));
  /* ⛔⛔ 「這些數字是模擬還是真單」⼀定要寫在最上面（混在一起就是騙自己）。 */
  setEl('hcsrc','主要的數字來自【模擬】的定論（同一條規則每天事後算一次，回填到 2024-08）——'+
    '真單的筆數太少，15／30 筆的窗口算不出來。每張卡底下另外標真單的情形。');
  setEl('hccards',(D.strategies||[]).map(hcCard).join(''));
  const W=D.lamp_words||{}, N=D.lamp_notes||{};
  setEl('hcleg',['ok','wn','bd'].map(k=>'<span><em class="hc-lamp '+k+'"><b></b>'+
    esc(W[k]||'')+'</em>'+esc(N[k]||'')+'</span>').join(''));
  setEl('hcmkt', D.market_ready
    ? (D.market||[]).map(hcMktCard).join('')
    : '<div class="hc-na">'+esc(D.market_note||'還在算…')+'</div>');
  setEl('hcfoot','數字來源是歷史資料，不代表明天會怎樣。這一頁不下任何判斷、不給任何建議。'+
    (D.real_err?'　⚠️ 真單那一半讀不出來：'+esc(D.real_err):''));
}
function hcFetch(){
  const my=++HC.seq;
  fetch('/api/health/state',{cache:'no-store'}).then(r=>r.json()).then(x=>{
    if(my!==HC.seq) return;
    if(!x||!x.ok){ HC.data=null; HC.err=(x&&x.msg)||'讀不到健檢'; hcPaint(); return; }
    HC.data=x; HC.err=''; hcPaint();
    /* ⛔ 只有「市場狀態還在背景算」才再問一次，⛔ 而且有次數上限（不無限重試）。 */
    if(!x.market_ready && x.market_busy && HC.tries<HC_TRIES){
      HC.tries++;
      if(HC.timer) clearTimeout(HC.timer);
      HC.timer=setTimeout(()=>{ HC.timer=null; if(TAB==='hc') hcFetch(); }, HC_RETRY);
    }
  }).catch(()=>{ if(my!==HC.seq) return; HC.data=null; HC.err='連不上面板'; hcPaint(); });
}
function hcEnter(){
  HC.tries=0;
  if(HC.timer){ clearTimeout(HC.timer); HC.timer=null; }
  hcFetch();
}

/* ══════════════ 【自動下單】分頁：會真的送出委託單的那一頁 ══════════════

   版面沿用【自動下單（模擬）】那一套（同樣的 .card / .sec-head / .at-title / .at-tbl），
   ⛔ 差別只有三件事，而且每一件都要**一眼看得到**：
     ① 現在是開還是關　② 選了哪個做法　③ 今天送了沒／為什麼沒送

   ⛔⛔ 這一段**一顆按鈕都沒有**（沒有 button／form／input／[data-act]／[data-rdir]），
        也**不打任何 POST**。開關只有一條路：他自己在硬碟上建 AUTO_ORDERS_ON。
        畫面上按得到的開關 ＝「會不會亂送單」從「讀一個檔」變成「讀整個前端」。
   ⛔ 顏色沿用這個面板的規矩：**紅綠只給損益**；開／關用金色與中性灰
      （關著是正常狀態，⛔ 不是紅色 —— 跟休市的連線燈同一個道理）。
   ⛔ 名字只寫「開盤快才做」（2026-09-15 起只剩這一個；以前是「5 分 K」「開盤起」），⛔ 畫面上不准出現 A／B 這種代號
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
/* ⭐ 2026-09-24 風控規則 B 的「手動解除」—— 跟打開開關同一套兩段式規矩：
   第一段只換畫面（⛔ 零請求），第二段按「確定解除」才 POST /api/risk/override。 */
var RCAP={step:'idle',busy:false,err:'',ok:''};
/* 本月風控那張卡（#alcap）。⛔ 數字與那句話全部從後端 D.risk 來，前端不自己算。
   ⛔ 用金色／中性灰，⛔ 不准用紅綠（紅綠只給損益）。 */
function alCapHTML(R){
 if(!R||typeof R!=='object') return '';
 if(R.err) return '<div class="cap hit"><div class="hd"><span class="k">本月風控</span></div>'+
   '<div class="t">⚠️ '+esc(R.err)+' —— 算不出來的時候<b>不送單</b>（不猜）。</div></div>';
 const cap=Number(R.cap||0), pnl=Number(R.pnl||0), used=Number(R.used_pct||0);
 const fmt=v=>(v>0?'+':(v<0?'−':''))+Math.abs(Math.round(v)).toLocaleString();
 let h='<div class="cap'+(R.hit?' hit':'')+'">'+
   '<div class="hd"><span class="k">本月風控・日盤＋夜盤共用（'+esc(String(R.month||''))+
     (R.since?'，'+esc(String(R.since).slice(5))+' 起算':'')+'）</span>'+
   '<span class="v">'+fmt(pnl)+' 點　/　上限 −'+Math.round(cap).toLocaleString()+'</span></div>'+
   '<div class="bar"><i style="width:'+Math.max(0,Math.min(100,used))+'%"></i></div>';
 if(R.blocked)
   h+='<div class="t">⚠️ 到了上限：<b>這個月日盤、夜盤都不送</b>，下個月自動恢復。模擬照常記錄。</div>';
 else if(R.hit&&R.override)
   h+='<div class="t">超過上限，但你已經<b>手動解除</b> —— 這個月照常送單，下個月恢復規則。</div>';
 else
   h+='<div class="n">虧到上限 ⇒ 當月兩條都停、下個月自動恢復。只算自動下的真單；夜盤算開盤那晚的月份。'+
     (R.open?'　有 '+esc(String(R.open))+' 口還沒平（不算）。':'')+
     (R.unknown?'　有 '+esc(String(R.unknown))+' 口平了但問不到出場價（不算）。':'')+'</div>';
 if(R.blocked){
   if(RCAP.step==='confirm')
     h+='<div class="al-conf"><div class="q">確定要解除這個月的風控嗎？<br>'+
       '解除之後，<b>這個月剩下的日子兩條都會照常送真單</b>，直到月底；下個月自動恢復規則。</div>'+
       '<div class="btns2"><button class="btn go" data-rcyes="1"'+(RCAP.busy?' disabled':'')+'>'+
       (RCAP.busy?'解除中…':'確定解除')+'</button>'+
       '<button class="btn no" data-rcno="1"'+(RCAP.busy?' disabled':'')+'>取消</button></div></div>';
   else
     h+='<div class="rbtn"><button class="btn" data-rcon="1">手動解除本月風控</button></div>';
 }
 if(RCAP.err) h+='<div class="err">'+esc(RCAP.err)+'</div>';
 if(RCAP.ok) h+='<div class="n">'+esc(RCAP.ok)+'</div>';
 return h+'</div>';
}
function rcArm(){
 if(RCAP.busy) return;
 RCAP.busy=true; RCAP.err=''; RCAP.ok=''; alPaint();
 pfetch('/api/risk/override','{}')
  .then(r=>r.json().catch(()=>({ok:false,msg:'面板回了看不懂的東西（HTTP '+r.status+'）'})))
  .then(r=>{ RCAP.busy=false; RCAP.step='idle';
    if(r&&r.ok){ RCAP.ok=(r.msg||'已解除')+(r.warn?('（'+r.warn+'）'):''); }
    else RCAP.err=(r&&r.msg)||'解除不了'; })
  .catch(()=>{ RCAP.busy=false; RCAP.step='idle'; RCAP.err='解除不了：連不上面板'; })
  .then(()=>alFetch());
}
/* ⚠️ 2026-09-15 自動下單只剩 A，名字改成「開盤快才做」（跟後端 auto_fire.METHOD_NAME 同一組字）。
   B 從這張表拿掉 ⇒ 舊紀錄裡 method:"B" 的那幾天，alName() 回空字串、畫面照舊印「—」。 */
/* ⭐ 2026-09-15 晚上再改成「快攻回馬槍」（慢的日子 09:15 反轉也會做；跟後端 METHOD_NAME 同一組字）。 */
/* ⛔⛔ 2026-09-17 Benson 裁示：**A 還是「快攻回馬槍」，真單繼續跑它。**
   「多方聯軍」是**多一個可以選的做法（U）**，⛔ 不是取代 A ——
   他不去動 AUTO_ORDERS_ON 的內容，跑的就還是快攻回馬槍。
   ⚠️ 兩張表的字跟後端 auto_fire.METHOD_NAME／METHOD_SUB 是同一組（⛔ 對不上就是兩把尺）。 */
const ALWAY={A:{n:'快攻回馬槍',s:'09:00 起算'},
             U:{n:'多方聯軍',s:'三個候選裡最早觸發的那個做多'}};
/* 帳本那一天是哪一段送的。⛔ 名字從後端（新帳本走 D.cand_names＝auto_fire.CAND_NAME，
   舊帳本走 D.leg_names＝LEG_NAME），這裡只是退路。⛔ 兩張表都不准在前端寫死。 */
function alLeg(D,r){
  const T=(D&&D.cand_names)||{}, L=(D&&D.leg_names)||{};
  if(r&&r.cand&&T[r.cand]) return T[r.cand];
  const k=r&&r.leg; return k?(L[k]||''):'';
}
function alName(k){ const w=ALWAY[k]; return w?w.n:''; }
/* ⛔⛔ 2026-09-15 晚上 Benson 回報：紀錄清單把 09-10～09-15 標成「快攻回馬槍」，但那幾天用的是舊規則
   （09-10／11／14 是 09:03:30 ±100、09-15 是 09:03:00 ±130）。alName() 只看做法代號 A，
   而 A 這個代號從頭到尾沒換過、規則卻換了三次 ⇒ **一筆紀錄叫什麼名字，要看那一天當時的規則，不是現在的**。
   判準：帳本有 leg（快攻回馬槍才會落地）或日期在上線那天之後 ⇒ 現在的名字；
   否則照帳本那一天的送單時刻（at）與停利點數（tp_points）寫出當時的規則，⛔ 不准套現在的名字。 */
const AL_HMQ_FROM='2026-09-16';
/* ⭐⭐ 2026-09-16 第二次改名（「快攻回馬槍」→「多方聯軍」）。上線那一天起才是新名字。
   ⛔⛔ **日期閘門一定要排在 `r.leg` 前面**：舊版寫成 `if(r.leg||日期>=…)`，
      而 09-16 那一版的紀錄**每一列都有 leg** ⇒ `r.leg` 會短路掉日期閘門 ⇒
      `METHOD_NAME["A"]` 一改，09-16 那幾天的紀錄當場被改名成「多方聯軍」。
      （同一個坑 09-15 晚上已經踩過一次：一筆紀錄叫什麼名字，要看**那一天當時的規則**。）
   ⚠️ `AL_HMQ_NAME` 是**寫死的歷史名字**，⛔ 不准改成 alName(...)（那又會跟著現在的常數跑）。

   ⭐⭐⭐ 2026-09-17（PM 裁示 M3）：**根治的做法是「帳本那一列自己說它是哪條規則」**
      —— 後端送單時把規則代號寫進那一列（`r.rule`，見 auto_fire.RULE_ID），
      畫面照那一列取名（`D.rule_names`）⇒ **名字再也不會因為現在跑哪條規則而改動**，
      也不必猜「上線日是哪一天」。舊列沒有 `rule` ⇒ 退回下面那套日期閘門。

   ⛔⛔ `AL_UNION_FROM` **刻意是 null ＝ 待填**（⛔ 不要留一個會悄悄生效的日期）：
      這支還沒 merge、他的面板現在沒在跑、多方聯軍也**不是**預設做法
      （Benson 2026-09-17：真單繼續跑快攻回馬槍）⇒ 猜一個日期只會把某幾天標錯名字。
      ⭐ 這一行**只有在「多方聯軍真的上線當預設」那一天才需要填**，而且那時候新寫的列
         本來就會自帶 `rule` ⇒ 實務上多半永遠不必填。 */
const AL_UNION_FROM=null;
const AL_HMQ_NAME='快攻回馬槍';
function alRecName(D,r){
  if(!r) return '';
  const d=String(r.date||'');
  /* ⭐ ① 帳本那一列自己講（⛔ 這一道排最前面：它是唯一不會被「現在的常數」影響的來源） */
  const RN=(D&&D.rule_names)||{};
  if(r.rule&&RN[r.rule]) return RN[r.rule];
  if(AL_UNION_FROM&&d>=AL_UNION_FROM) return alName(r.method);
  if(d>=AL_HMQ_FROM||r.leg) return AL_HMQ_NAME;
  const at=String(r.at||'').slice(0,8), tp=alN(r.tp_points);
  return '舊規則'+(at?(' '+at):'')+(tp!=null?(' ±'+tp.toFixed(0)+'點'):'');
}
/* ⚠️ 2026-09-17 起畫面上不再用它（規則那句話的正本搬到後端 `fire_rule_line()`）——
   ⛔ 但**不刪**：`ALWAY` 那張表是舊紀錄的名字退路，這一支是它的另一半。 */
function alSub(k){ const w=ALWAY[k]; return w?w.s:''; }
function alN(v){ return (typeof v==='number'&&isFinite(v))?v:null; }
/* ⭐ 2026-09-15【自動下單】的規則那一句（一個地方組，三個地方用：標題小字／確認前說明／每天送那格）。
   ⛔ 數字全部從後端：時刻 D.signal_at、±% D.rule.tpsl_pct、今天約幾點 D.fast.pts（09:03:30 判過之後）
      或 D.pts_est（還沒到時照現價估）。⛔ 前端不准自己乘 0.005 —— 那是第二把尺。
   ⚠️ 這裡講的是**自動下單**的規則；手動真單／練習仍是 ±RULE_TP（兩套數字同時出現時要看得出是哪一套）。 */
function alPct(D){ const r=(D&&D.rule)||{}; return alN(r.tpsl_pct); }
function alPtsToday(D){
 const F=(D&&D.fast)||{};
 if(alN(F.pts)!=null) return {v:F.pts,est:false};
 if(alN(D&&D.pts_est)!=null) return {v:D.pts_est,est:true};
 return null;
}
/* 「比過去 40 天裡 8 成的日子快」—— 天數與百分位從後端 D.rule（正本 live_panel.FAST_PCTL），⛔ 不寫死 40／80。
   百分位是 10 的倍數就講「N 成」（他看得懂的說法），不是就照實講「第 P 百分位」。 */
function alPctlTxt(D){
 const r=(D&&D.rule)||{}, w=alN(r.window), q=alN(r.pctl);
 if(w==null||q==null) return '';
 return '比過去 '+w+' 天裡'+(q%10===0?(' '+(q/10)+' 成的日子'):('第 '+q+' 百分位'))+'快';
}
/* ⭐ 2026-09-16「多方聯軍」：規則句一個地方組、三個地方用。
   ⛔ 時刻／百分位／±% 全部從後端（D.signal_at／D.rule）—— 前端不准寫死 09:15／09:30／0.5%。
   ⚠️ 只做多、取最早觸發的那一個 —— 這兩句是這條規則跟舊的「快攻回馬槍」最大的差別，必須寫出來。 */
/* ⛔⛔ 2026-09-17：這一句以前寫死「多方聯軍：三個候選…」，而真單跑的是 A（快攻回馬槍）
   ⇒ 開著的時候標題那行小字整段是假話。⇒ **改成照現在跑的那個做法**，
   正本在後端 `fire_rule_line()`（⛔ 前端不准再寫第二份規則說明）。
   ⚠️ 「今天約 N 點」是這裡才有的（要看今天的價），接在後面。 */
function alRuleTxt(D){
 const t=alPtsToday(D), c=alPctlTxt(D), q=String(D.qty||1);
 const base=alRuleFull(D,D&&D.method)||'';
 return (base||('一天最多 '+q+' 口'))+
   (c?('　夠快的定義：'+c+'。'):'')+
   (t?('　今天約 '+t.v+' 點'+(t.est?'、照現價估':'')+'。'):'');
}
/* 今天的門檻與判定（快／不快／歷史不夠）。⛔ 判定只從後端 D.fast 來（fast_verdict 那一支），
   ⛔ 09:03:30 之前不預告（verdict 是 null 時只講門檻）。 */
function alFastHTML(D){
 const F=D&&D.fast; if(!F) return '';
 const r=(D.rule)||{}, pct=v=>(alN(v)==null?'—':v.toFixed(2)+'%'), pts=v=>(alN(v)==null?'':'（約 '+v+' 點）');
 const how='過去 '+esc(String(r.window||F.window||''))+' 個交易日開盤走幅的第 '+esc(String(r.pctl||F.pctl||''))+' 百分位';
 let h;
 if(F.verdict==='no_hist')
   h='<b>歷史不夠</b> —— 開盤走幅只記到 '+esc(String(alN(F.n)||0))+' 天（至少要 '+esc(String(r.min_n||F.min_n||''))+
     ' 天），算不出門檻，照規則不做';
 else if(F.verdict==='fast')
   h='<b>快</b> —— 今天走 '+pct(F.move_pct)+pts(F.move_pts)+'，門檻 '+pct(F.thr_pct)+pts(F.thr_pts)+'（'+how+'）';
 else if(F.verdict==='slow')
   /* ⚠️ 2026-09-15 晚上：不夠快 ⛔ 不再是「今天不做」—— 要等回馬槍那一刻（D.rev_at）看反轉 */
   h='<b>不夠快</b> —— 今天走 '+pct(F.move_pct)+pts(F.move_pts)+'，門檻 '+pct(F.thr_pct)+pts(F.thr_pts)+
     '（'+how+'）—— 等 '+esc(D.rev_at||'—')+' 看有沒有反轉';
 else
   h='今天的門檻 '+pct(F.thr_pct)+'（'+how+'，用了 '+esc(String(alN(F.n)||0))+' 天）· '+
     esc(D.signal_at||'')+' 才判定快不快';
 if(alN(F.bad)||alN(F.dup)) h+=' · <span class="warn">歷史檔有 '+esc(String(alN(F.bad)||0))+' 列讀不出來、'+
   esc(String(alN(F.dup)||0))+' 列重複，已跳過</span>';
 if(F.err) h+=' · <span class="warn">'+esc(F.err)+'</span>';
 if(D.hist_msg) h+=' · <span class="warn">'+esc(D.hist_msg)+'</span>';
 return '<span class="k">今天判定</span>'+h;
}
function alF(v,d){ const n=alN(v); return n==null?'—':n.toFixed(d==null?1:d); }
function alSigned(v){ const n=alN(v); return n==null?'—':(n>0?'+':'')+n.toFixed(1); }

function alEnter(){
 /* ⛔ 每次切進這一頁都回到「未確認」（⛔ 不可以留著上次展開到一半的確認條）。
    ⛔ 日盤與夜盤**各自一份**：他剛剛在夜盤按到一半，切走再回來也要回到 idle。 */
 ALON.step='idle'; ALON.mode=null; ALON.busy=false; ALON.err='';
 NFON.step='idle'; NFON.mode=null; NFON.busy=false; NFON.err='';
 RCAP.step='idle'; RCAP.busy=false; RCAP.err=''; RCAP.ok='';
 alFetch(); NF.n=0; nfFetch();
 if(AL.timer) clearTimeout(AL.timer);
 AL.timer=setTimeout(alLoop,5000);
}
function alLoop(){
 if(TAB!=='fire'){ AL.timer=null; return; }
 alFetch(); nfFetch(); AL.timer=setTimeout(alLoop,5000);
}
/* ⭐ 夜盤自動下單的狀態（GET /api/nightfire/state，唯讀、不帶 token）。
   ⭐⭐ 2026-09-23：夜盤補了畫面上的開關（Benson 交辦）⇒ 這一份狀態除了顯示，
      還負責決定「開／關那兩顆鈕要不要畫」。⛔ 狀態本身照舊是唯讀的 GET。 */
const NF={n:0, data:null, err:''};
/* 夜盤那顆「打開」的兩段式狀態。⛔ 只活在記憶體、⛔ 不寫 localStorage、⛔ 跨分頁不殘留。 */
var NFON={step:'idle', mode:null, busy:false, err:''};
function nfFetch(force){
 if(!force && (NF.n++)%6) return;              /* 跟著 alLoop 走，約 30 秒一次就夠 */
 fetch('/api/nightfire/state',{cache:'no-store'}).then(r=>r.json()).then(x=>{
   NF.data=(x&&x.ok)?x:null; NF.err=(x&&x.ok)?'':((x&&x.msg)||'讀不到夜盤自動下單的狀態');
   nfPaint();
 }).catch(()=>{ NF.data=null; NF.err='讀不到夜盤自動下單的狀態'; nfPaint(); });
}
/* 夜盤的狀態現在畫在 ① 狀態列與 ⑥ 開關區裡（跟日盤同一套版面）⇒ 交給 alPaint 一起畫。 */
function nfPaint(){ alPaint(); }
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

/* ⭐ ① 狀態列：日盤與夜盤兩顆對稱的藥丸（2026-09-23 v3）。
   ⛔ 關著＝中性灰、開著＝金色；⛔ 不准用紅綠（這個面板的紅綠只給損益）。
   ⛔ 「真錢 vs 演練」**從後端來**（日盤 D.live／夜盤 x.live），⛔ 前端不准自己猜。
   ⛔ 「關閉」鈕就放在這裡：關掉永遠是安全的動作、要好按、⛔ 照舊不跳確認；
      ⛔ 只有開著才畫（沒東西可關就不該有按鈕）。 */
/* `on`＝金色（真的會送單）；`canOff`＝**那顆「關閉」鈕要不要畫**。
   ⛔⛔ 兩件事**刻意分開**：開關檔存在但內容看不懂時 `on` 是 false（畫面不可以說「開著」），
      但那個檔還在 ⇒ `canOff` 是 true，**他關得掉**。
      （2026-09-09 lab-qa Q9：顯示條件看 `armed` 的話，壞掉的開關檔他關不掉，
        而「關」永遠是安全方向。） */
/* `on`＝金色（真的會送單）；`canOff`＝**那顆「關閉」鈕要不要畫**。
   ⛔⛔ 兩件事**刻意分開**：開關檔存在但內容看不懂時 `on` 是 false（畫面不可以說「開著」），
      但那個檔還在 ⇒ `canOff` 是 true，**他關得掉**。
      （2026-09-09 lab-qa Q9：顯示條件看 `armed` 的話，壞掉的開關檔他關不掉，
        而「關」永遠是安全方向。）
   ⚠️ 關著時右邊那顆是「到開關」——**純導覽**，⛔ 一個請求都不送（只捲到最底下那張卡）。 */
function alPillHTML(lb,nm,on,live,canOff,offAttr,msg){
 return '<div class="al-pill'+(on?' on':'')+'"><span class="lb">'+esc(lb)+'</span>'+
   '<span class="nm">'+esc(nm||'—')+'</span>'+
   '<span class="st">'+esc(on?(live?'開著・真錢':'開著・演練'):(msg||'關著'))+'</span>'+
   (canOff?('<button class="off" '+offAttr+'>關閉</button>')
          :'<button class="off" data-aljump="1">到開關</button>')+'</div>';
}
/* ⭐ 日盤藥丸上的名字：**那一盤實際在跑的做法**。
   ⛔ 沒開著的時候寫的是「不去動開關檔就是這一條」那一條（後端 default_method），
      ⛔ 不准寫死「多方聯軍」—— 帳本那一列叫什麼名字還是走 alRecName()。 */
function alDayName(D){
 const k=(D&&D.armed)?D.method:((D&&D.default_method)||'U');
 const MS=(D&&D.methods)||[];
 const hit=MS.filter(x=>x.k===k)[0];
 return (hit&&hit.name)||alName(k)||k;
}
function alBarHTML(D){
 const x=NF.data;
 /* ⛔⛔ 「關閉」鈕的顯示條件逐字就是 `D.flag_exists`（檔案在不在），⛔ 不准用 armed、
    ⛔ 不准 ||、⛔ 不准 &&（加料之後「關著的時候整頁 0 顆會動到錢的按鈕」那條鐵律就破了）。 */
 const dayOff=D.flag_exists;
 const dayArm=!!(D&&D.armed);
 /* 「檔在、但內容看不懂」⇒ 拒絕下單：⛔ 不可以畫成「開著」（那是假話），
    但也不是「關著」（那個檔還在）⇒ 照後端那句話講。 */
 const dayMsg=dayOff?(dayArm?'':(D.arm_msg||alWhy(D.arm_why)||'拒絕下單')):'關著';
 let h=alPillHTML('日盤', alDayName(D),
                  dayArm, !!(D&&D.live), dayOff, 'data-aloff="1"', dayMsg);
 /* 夜盤同一套規矩：⛔ 開關檔在不在（x.flag_exists）＝ 那顆「關閉」要不要畫，⛔ 不看 on。 */
 if(x) h+=alPillHTML('夜盤', nfName(x, x.on?x.method:null),
                     !!x.on, !!x.live, !!x.flag_exists, 'data-nfoff="1"',
                     x.flag_exists?(x.msg||'拒絕下單'):'關著');
 else h+='<div class="al-pill"><span class="lb">夜盤</span><span class="nm">—</span>'+
   '<span class="st">'+esc(NF.err||'載入中…')+'</span></div>';
 return h;
}
/* 夜盤那一條叫什麼名字。⛔ 名字的正本在後端（x.methods／x.name），⛔ 前端不准寫死。 */
function nfName(x,k){
 const MS=(x&&x.methods)||[];
 if(k){ const hit=MS.filter(m=>m.k===k)[0]; if(hit&&hit.name) return hit.name; }
 return (x&&x.name)||(MS[0]&&MS[0].name)||'—';
}

function alPaint(){
 const D=AL.data;
 if(!D){
   setEl('albar','<div class="al-pill"><span class="lb">日盤</span><span class="nm">—</span>'+
     '<span class="st">讀不到狀態</span></div>');
   setEl('alsub','<span class="warn">'+esc(AL.err||'載入中…')+'</span>');
   /* ⛔⛔ 讀不到狀態時「打開」那幾顆更不能留：我們連現在是真錢還是演練都不知道。
      ⛔ 「關閉」也一樣收起來 —— 我們不知道開關檔在不在。 */
   setEl('algates',''); setEl('alcap',''); setEl('alfast',''); setEl('alrisk',''); setEl('altoday','');
   setEl('alswitch',''); setEl('alpos','');
   setEl('alperf',''); setEl('alsparkbox',''); setEl('alperfn','');
   setEl('altbl',''); setEl('alempty',''); setEl('alnotes','');
   return;
 }
 const armed=!!D.armed, m=D.method, live=!!D.live;
 /* ── ① 狀態列（日盤／夜盤）───────────────────────────────── */
 setEl('albar', alBarHTML(D));
 /* ⛔ 真單開關要跟自動開關一起講：他關掉真單就等於連自動也關掉，
    但畫面上如果只寫「開啟中」，他會以為單真的會出去。 */
 /* ⛔ 這一行不要複述上面那個名字（上面已經寫了「開盤快才做」＋「09:00 起算」）——
    這裡要講的是**後果**：09:03:30 一到會發生什麼事。 */
 const sigT=esc(D.signal_at||''), eodT=esc(D.eod_at||'');
 const subs=[];
 /* ⛔ 兩個開關的關係要講清楚（原本寫在〈怎麼開〉那段，2026-09-10 整段砍掉）：
    `auto_fire` 裡**沒有**第二道 `REAL_ORDERS_ON` 判斷，一律走 `broker._send()`
    ⇒ 關掉真單就等於連自動也關掉。這句話只在真單關著時出現（＝條件式那幾條之一）。
    ⚠️ 2026-09-14：開著＋真單也開著時副標**整行是空的**（那句「會自己送 1 口真單（你不在也會送）」
       與「13:43:30 會自動平倉（只平那一口）」已併進上面標題那行小字，不再講第二次）。 */
 if(!armed) subs.push('<span>一張單都不會送出去</span>');
 else if(!live) subs.push('<span>'+sigT+' 一到會走完整條路，但真單開關關著 ⇒ 不會真的送出去'+
   '（<b>把真單關掉就等於連自動也關掉</b>）</span>');
 /* ⚠️ 「開關沒有有效期」那句 2026-09-10 **搬到底下的 `.al-risk`**（風險條）——
    它跟「停損活在這台電腦裡」是同一個等級的事，混在這排小字裡看不見。
    ⛔ 搬走不是刪掉：關著的時候它掛在那顆「打開」旁邊（見 alSwDay／alSwNight）。 */
 /* ⛔ 收盤平倉是這一段最會賠錢的地方（沒平就是抱過夜盤），一定要寫在最上面
    —— 2026-09-14 起寫在標題那行小字（st），不在這裡重複。 */
 let sub=subs.join('<span class="sep">·</span>');
 const sep=()=>(sub?'<span class="sep">·</span>':'');
 /* ⚠️ 真單關著時最容易被誤會的一件事：症狀（按不了進場）跟原因（演練部位）
    看起來毫無關係，不寫他會以為面板壞了。 */
 if(armed&&!live) sub+=sep()+'<span class="warn">'+
   '⚠️ 演練也會產生一個<b>演練部位</b> —— 那口部位開著的時候，'+
   '你自己在【即時】那一頁<b>按不了進場</b>（會寫「已經有部位了」）。'+
   '要自己下單就先按手動平倉，或把這個開關關掉。</span>';
 if(D.err) sub+=sep()+'<span class="warn">送單那一段出過錯 '+
   esc(String(D.err_n||0))+' 次（停損不受影響）：'+esc(D.err)+'</span>';
 /* ⭐⭐ 2026-09-17（PM 裁示 M1）：「今天是不是結算日」判不出來 ⇒ ⛔ 一定要講。
    結算日日盤 13:30 就收盤，而判不出來的那一天走的是平常那一組（13:43:30）——
    真的是結算日的話那一刻市場已經關了、平倉單送不出去。
    ⛔ 這句話只在**不確定**時出現（確定不是結算日的平常日子講它只是雜訊）。 */
 if(D.eod_sure===false) sub+=sep()+'<span class="warn">⚠️ 今天是不是結算日<b>判不出來</b>'+
   '（'+esc(String(D.eod_err||'行事曆不完整'))+'）—— 收盤平倉照平常那一組跑，'+
   '<b>今天請自己盯一下 13:30</b>。</span>';
 setEl('alsub',sub);

 /* 三顆狀態卡的短標（2026-09-14 lab-ux 定案 C）：「自動下單」「真單」「每天送」。
    開關檔叫什麼（AUTO_ORDERS_ON／REAL_ORDERS_ON）改掛在 title（滑過去看得到），
    ⛔ 不是拿掉 —— 「拒絕下單：讀到 K線」那條路他還是要知道去哪個檔看。 */
 setEl('algates',
   '<div class="c" title="'+esc(D.flag||'')+'"><div class="k">自動下單</div>'+
     '<div class="v'+(armed?'':' off')+'">'+esc(armed?('開著 · '+alName(m)):'沒有這個檔')+'</div></div>'+
   '<div class="c" title="'+esc(D.live_flag||'')+'"><div class="k">真單</div>'+
     '<div class="v'+(live?'':' off')+'">'+
       esc(live?'開著 · 會真的送出去':'關著 · 只會演練')+'</div></div>'+
   /* ⚠️ 2026-09-15：「每天送」→「快才送」—— 規則換成開盤快才做，寫「每天送」就是一句假話。
      停利停損是 ±%（後端 D.rule），⛔ 不是手動真單那個 ±RULE_TP。 */
   /* ⚠️ 2026-09-15 晚上：「快才送」→「怎麼送」—— 慢的日子反轉也會送，寫「快才送」是假話。 */
   '<div class="c"><div class="k">怎麼送</div>'+
     '<div class="v">'+esc(String(D.qty||1))+' 口 · 停利停損 &plusmn;'+
       esc(alPct(D)==null?'—':String(alPct(D)))+'% · 一天最多 1 次</div></div>');
 /* ⭐ 2026-09-24 風控規則 B：本月自動單損益／上限（到上限才有「手動解除」）。 */
 setEl('alcap', alCapHTML(D.risk));
 /* 今天的門檻與判定（快／不快／歷史不夠）—— 掛在開關區，今天那張卡收起來時也看得到。 */
 setEl('alfast',alFastHTML(D));

 /* ── ⭐⭐ 風險條（2026-09-10 取代整段〈怎麼開〉）───────────────────
    ⛔ **砍掉的是教學，不是風險。** 拿掉的是：自己建檔的做法、`Set-Content …`、
       UTF-8／UTF-16 的編碼提醒、「要關：按下面那顆」、方向怎麼判、
       「改名收起來、要再開就自己把檔名改回去」——
       那些現在畫面上都有更直接的東西（兩顆鈕就在下面、關的那顆也在下面）。
    ⛔⛔ **留下來的只有兩句，而且要更醒目**（金色，⛔ 不用紅綠）：
       ① 停損活在這台電腦裡 —— 這是這個工具最會賠錢的一件事，
          而且他**看不出來**（面板關掉的時候沒有任何東西會告訴他）。
       ② 這個開關沒有有效期 —— 他自己的原話是「我用工具那邊關掉之後，才會失效」，
          不寫他會以為「今天開的、今天有效」。
    ⛔ 顯示條件（2026-09-23 規格 §2③ 改過，⛔ 句子本身一個字都沒動）：
       ① 停損活在面板裡 …… **任一盤開著 或 手上還有部位**
       ② 開關沒有有效期 …… 任一盤開著（關著時照舊掛在「打開」鈕旁邊）
       ③④ 夜盤自己那兩句 …… 夜盤開著
    ⚠️⚠️ ① 從「開著才畫」放寬成「開著**或**還有部位」是刻意的：他按了關閉之後，
       手上那一口的停損**還是靠這台面板**，而舊規則會在那一刻把這句話收走 ——
       那正是最需要它的一刻。⛔ 這是**加**不是改。 */
 /* ⚠️ 2026-09-14 只降層級、一字不刪（樣式見 .al-risk）：圖示改用純文字 ⚠（&#9888;）而不是 emoji，
    金色才套得上去；整段灰字、關鍵字白。 */
 const nfd=NF.data, nOn=!!(nfd&&nfd.on);
 const holding=!!((LASTS&&LASTS.real&&LASTS.real.position)||null);
 const anyOn=armed||nOn;
 setEl('alrisk', (anyOn||holding)
   ? '<div class="al-risk">'+
       '<p><i>&#9888;</i><span>停損活在<b>這台電腦的面板迴圈</b>裡 —— '+
         '面板關掉／當掉／電腦睡著就<b>沒有停損</b>'+
         (eodT?('，'+eodT+' 的自動平倉也不會發生'):'')+'。</span></p>'+
       /* ⚠️ 2026-09-15：「每個交易日都會送」改成「都會看、夠快就送」—— 規則換成開盤快才做之後，
          寫「都會送」是一句假話（慢的日子不送）；但「沒有有效期」那半一字不動。
          ⛔ 2026-09-17（lab-qa 建議 2）：那句話**寫死了兩候選的舊描述** ——
             多方聯軍是三個候選、而且「夠快但做空要跳過」⇒ 在 U 底下整句是假話。
             ⇒ 改成照現在跑的那個做法（正本在後端 fire_rule_line）。 */
       (armed?('<p><i>&#9888;</i><span>這個開關<b>沒有有效期</b> —— '+
         '每個交易日都會照「<b>'+esc(alDayName(D))+'</b>」看一次（'+
         emb(alRuleFull(D,m))+'），直到你自己按上面那顆關掉。</span></p>'):'')+
       (!armed&&nOn?('<p><i>&#9888;</i><span>這個開關<b>沒有有效期</b> —— '+
         '每個晚上都會看一次，直到你自己關掉。</span></p>'):'')+
       /* ③④ 夜盤自己的兩句（CLAUDE.md 2026-09-22 那節）。⛔ 夜盤關著時不畫。 */
       (nOn?('<p><i>&#9888;</i><span>夜盤那一口的停損<b>一樣活在這台面板裡</b>'+
               '（永豐沒有停損單）。</span></p>'+
             '<p><i>&#9888;</i><span>所以有部位的晚上<b>面板要整晚開著</b> —— '+
               '04:58 才會自動平倉。</span></p>'):'')+
       /* ⭐ 夜盤那一條有特別的出場安排（夜盤跟勢：不設停利、2% 保護停損）時的第三重揭露
          （⛔ 只有那一條在跑時才出現；⛔ 句子是後端 NIGHT_METHOD_INFO 的 risk，前端不寫第二份）。 */
       (nOn&&nfRisk(nfd)?('<p><i>&#9888;</i><span>夜盤選的是<b>'+esc(nfName(nfd,nfd.method))+
         '</b> —— '+emb(nfRisk(nfd))+'</span></p>'):'')+
     '</div>'
   : '');

 /* ── ⑥ 開關（沉到最底）──────────────────────────────────────
    ⛔ 「打開」兩段式：⛔ 只有開關檔**不在**的時候才畫（開著就只剩「關閉」）。 */
 setEl('alswitch', alSwDay(D)+alSwNight());

 /* ── ② 今天送了沒／為什麼沒送 ─────────────────────────────── */
 const days=D.days||[], today=D.today||'', row=days.find(r=>r&&r.date===today)||null;
 setEl('alcount',esc(today));
 /* ⛔⛔ 「今天」這一塊吃的是**跟紀錄清單同一份資料**（2026-09-10 一起改）：
    只改清單不改這裡的話，畫面會同時寫著「紀錄：出場 +100」與
    「今天：已送出委託單，13:43:30 會自動平倉」—— 後面那句在那一刻已經是假話。
    ⭐ 2026-09-14 一件事只講一次：今天那一口**已經出場**（真單對到了 real_trades/ 那一趟、
       而且收盤那一段沒有警示）時，「今天」那半整塊收起來，改併進「紀錄」第一列
       （金框＋「今天」＋「已出場」標，見 alCard 的 today 版）—— 同一筆點數不再寫兩次。
    ⛔ 其他狀態（還沒到／沒有紀錄／沒送成／下落不明／持有中／對不到／收盤警示）照舊畫這裡。 */
 const merged=alTodayMerged(D,row);
 const tbox=document.getElementById('altodaybox');
 if(tbox) tbox.hidden=merged;
 /* ⭐⭐ 2026-09-23（規格 §2.7）：**關掉之後今天那一口還在**這一幕要畫得出來。
    舊寫法在開關關掉之後會走「今天沒有紀錄／還沒到」那條，跟紀錄清單上的「持有中」
    自相矛盾 —— 他會以為關掉就把那一口收回來了。
    ⛔ 判準是「今天真的送出去了 ＋ 開關現在關著」；⛔ 部位卡**不可以跟著清空**（見 alPosHTML）。 */
 const openedToday=!!(row&&row.rec==='result'&&row.ok);
 setEl('altoday', merged ? ''
   : ((openedToday&&!armed)
      ? ('<div class="t">日盤已經關掉了，但<b style="color:var(--gold)">今天那一口還在</b></div>'+
         '<div class="d">它照樣走完停損停利與 <b>'+esc(D.eod_at||'')+'</b> 的自動平倉。<br>'+
         '關掉生效的是<b>之後的日子</b> —— 明天開始不再送單。</div>'
         + alEodHTML(D,row))
      : (alTodayHTML(D,row)+alCandsHTML(D,row)+alEodHTML(D,row))));
 /* 右半：現在的部位（⛔ 唯讀；⛔ 開關關掉了也照樣畫 —— 部位還在就是還在） */
 setEl('alpos', alPosHTML(D,row));

 /* ── ④ 最近做得如何（⛔ 不放勝率、不放期望值、不放勝敗場數）──────── */
 alPerfPaint(D,days);

 /* ── ⑤ 紀錄（日盤與夜盤合併在同一份清單，靠 chips 篩）───────────── */
 const merged_rows=alRows(D,days,merged?today:'');
 const shown=merged_rows.filter(r=>ALF==='all'||r.kind===ALF);
 setEl('allogn',shown.length?(shown.length+' 筆'):'');
 setEl('altbl',shown.map(r=>r.html).join(''));
 setEl('alempty',merged_rows.length?(shown.length?'':'這個篩選底下沒有紀錄。'):
   '還沒有任何紀錄。開關關著的時候，'+esc(D.signal_at||'')+' 一到只會在這裡記一列「沒送」，'+
   '不會有任何委託單出去。');
 setEl('alnotes',alNotesHTML(D,days));
 /* chips 由這裡畫（⛔ 這一頁的靜態 HTML 一顆 button 都不准有）。 */
 setEl('alfilter',[['all','全部'],['day','日盤'],['night','夜盤']].map(
   x=>'<button class="al-chip'+(ALF===x[0]?' on':'')+'" data-alf="'+x[0]+'">'+x[1]+'</button>').join(''));
}

/* ⭐ ② 右半「現在的部位」（⛔ 唯讀、⛔ 一顆鈕都沒有）。
   ⛔ 部位那幾個數字來自 /api/state 的 `real`（跟【即時】那張卡、跟停損監控**同一份**）——
      ⛔ 這一頁不自己算浮動點數，也不自己算停利停損價。
   ⛔ 夜盤那一口**另起一行**（它是另一套帳、另一個帳本）。 */
function alPosHTML(D,row){
 const s=LASTS||{}, R=s.real||{}, P=R.position;
 const nfp=NF.data;
 const nfline='<div class="r"><span>夜盤那一口</span><b>'+
   esc(nfp?(nfp.on?(nfp.tonight?('今晚 '+(nfp.tonight.look_at||'')+' 才看'):'沒有部位'):'關著'):'—')+
   '</b></div>';
 if(!P){
   /* ⛔ 「現在沒有部位」與「最早幾點會有」是兩句不同的話（後者只有開著才成立）。 */
   const when=[];
   if(D&&D.armed&&D.signal_at) when.push('日盤那一口最早 '+D.signal_at);
   if(nfp&&nfp.on&&nfp.tonight&&nfp.tonight.look_at) when.push('夜盤那一口最早 '+nfp.tonight.look_at);
   return '<div class="al-pos"><div class="hd"><span>現在的部位</span></div>'+
     '<div class="none">現在沒有部位。'+(when.length?('<br>'+esc(when.join('、'))+'。'):'')+'</div></div>';
 }
 const fp=R.float_pts;
 const dir=P.dir==='long'?'▲ 做多':(P.dir==='short'?'▼ 做空':'方向不明');
 return '<div class="al-pos live"><div class="hd"><span>現在的部位</span>'+
   '<span style="color:var(--gold)">'+dir+' '+esc(String(P.qty==null?1:P.qty))+' 口</span></div>'+
   '<div class="big '+sgn(fp)+'">'+(fp==null?'—':pm(fp))+
     ' <small style="font-size:13px;font-weight:600;color:var(--faint)">點</small></div>'+
   '<div class="r"><span>進場</span><b>'+f(P.entry)+
     (P.entry_time?'（'+esc(String(P.entry_time).slice(0,8))+'）':'')+'</b></div>'+
   '<div class="r"><span>現價</span><b>'+f(livePx(s))+'</b></div>'+
   '<div class="r"><span>停損 / 停利</span><b>'+(R.sl==null?'—':f(R.sl))+' / '+
     (P.no_tp?'不設停利':(R.tp==null?'—':f(R.tp)))+'</b></div>'+
   /* ⚠️ 夜盤時段手上那一口是夜盤自動下單開的 ⇒ 04:58 平（⛔ 不是日盤的 13:43:30）。 */
   (isNightNow()
     ? '<div class="r"><span>沒平掉就</span><b>04:58 自動平</b></div>'
     : (D&&D.eod_at?'<div class="r"><span>沒平掉就</span><b>'+esc(D.eod_at)+' 自動平</b></div>':''))+
   nfline+
   (P.no_tp?'<div class="n" style="color:var(--gold)">'+noTpTag()+'：券商端無掛單 —— '+
     '這一口沒有停利單、永豐又沒有停損單，<b>停損與收盤平倉都靠面板</b>。</div>':'')+
   '</div>';
}

/* ⭐ ④ 「最近做得如何」。
   ⛔⛔ **不放勝率、不放期望值、不放勝敗場數**（CLAUDE.md 那條鐵律）。
   ⛔ 資料就是這一頁現有的那一份（帳本列 ＋ D.real 的出場價），⛔ 不新開端點、⛔ 不另寫一套算法。
   ⛔ 對不到出場的那幾天**不算一筆**（留白跟 0 點是兩回事）。 */
function alPts(D,days){
 const R=D.real||{}, out=[];
 /* days 是新到舊 ⇒ 反過來走，讓迷你柱狀圖左舊右新。 */
 for(let i=days.length-1;i>=0;i--){
   const r=days[i]; if(!r||r.rec!=='result'||!r.ok) continue;
   const X=R[r.date]; if(!X||X.state!=='ok'||!alN(X.points)) continue;
   out.push({date:r.date, pts:X.points});
 }
 return out;
}
function alPerfPaint(D,days){
 const P=alPts(D,days);
 const avg=n=>{ const a=P.slice(-n); return a.length?a.reduce((x,y)=>x+y.pts,0)/a.length:null; };
 const mo=(D.today||'').slice(0,7);
 const moRows=P.filter(x=>String(x.date).slice(0,7)===mo);
 const moSum=moRows.length?moRows.reduce((x,y)=>x+y.pts,0):null;
 setEl('alperfn', P.length?(P.length+' 筆算得出點數'):'還沒有算得出點數的紀錄');
 if(!P.length){
   setEl('alperf','');
   setEl('alsparkbox','');
   return;
 }
 const a10=avg(10), a30=avg(30);
 setEl('alperf',
   '<div class="c"><div class="k">近 10 筆 每筆平均</div><div class="v '+sgn(a10)+'">'+
     (a10==null?'—':pm(a10))+'</div><div class="s">點／筆</div></div>'+
   '<div class="c"><div class="k">近 30 筆 每筆平均</div><div class="v '+sgn(a30)+'">'+
     (a30==null?'—':pm(a30))+'</div><div class="s">點／筆</div></div>'+
   '<div class="c"><div class="k">本月合計</div><div class="v '+sgn(moSum)+'">'+
     (moSum==null?'—':pm(moSum))+'</div><div class="s">'+moRows.length+
     ' 筆・⛔ 不含手續費與稅</div></div>');
 setEl('alsparkbox', sparkBars(P.slice(-30).map(x=>x.pts),50));
}

/* ⭐ ⑤ 紀錄：日盤與夜盤**合併在同一份清單**（2026-09-23 v3）。
   ⚠️⚠️ 合併只發生在**畫面**上 —— 兩邊是**不同的帳本**（autofire/ vs nightfire/）、
      不同的端點、不同的 rule 命名空間，⛔ 不准合併檔案、⛔ 不准共用 rule。
   ⚠️ 夜盤帳本**沒有出場價**（出場在券商端成交，night_fire 那一側看不到）⇒
      夜盤那幾列的點數與出場價**留白**，⛔ 不猜、⛔ 不拿現價頂。
   每一列的 `.al-meta` 第一個詞就是規則名，一眼分得出是哪一套。 */
var ALF='all';
function nfCard(o){
 const dir=o.dir==='long'?'▲ 多':(o.dir==='short'?'▼ 空':'');
 const sent=(o.rec==='result');
 const tag=sent?(o.ok?'送出了':'沒送成'):(o.rec==='eod'?'收盤':'沒送');
 const px=sent&&o.entry!=null
   ? f(o.entry)+'<span class="arrow">&rarr;</span>— <span class="tag">'+esc(tag)+'</span>'
   : '— <span class="tag">'+esc(tag)+'</span>';
 const why=String(o.msg||o.why||o.err||'');
 return '<div class="trade"><div class="tr-top">'+
   '<span class="tr-date">'+esc(String(o.E||'').slice(5))+'</span>'+
   (dir?'<span class="dir '+(o.dir==='long'?'l':'s')+'">'+dir+'</span>':'')+
   '<span class="tr-px">'+px+'</span>'+
   '<span class="tr-res na">—</span></div>'+
   '<div class="al-meta">'+esc(nfName(NF.data,o.method||null))+'・04:58 平倉'+
     (why?' · '+esc(why):'')+'</div></div>';
}
function alRows(D,days,todayMerged){
 const out=[];
 /* ⛔ 照舊只印前 FIRE_REAL_DAYS（60）天 —— 跟後端 fire_real_pairs() 的天數是同一個數。 */
 days.slice(0,60).forEach(r=>out.push({kind:'day', date:String(r.date||''),
   html:alCard(D,r,!!(todayMerged&&r&&r.date===todayMerged))}));
 const x=NF.data;
 if(x&&x.recent) x.recent.forEach(o=>out.push({kind:'night', date:String(o.E||''), html:nfCard(o)}));
 /* 兩份帳本合在一起要重新排序（新到舊）。⛔ 同一天時日盤排前面（它先發生）。 */
 out.sort((a,b)=>(a.date<b.date?1:(a.date>b.date?-1:(a.kind==='day'?-1:1))));
 return out;
}

/* ══ ⑥ 開關（2026-09-23 定案，規格 §2.7）════════════════════════════
   ⛔⛔ **日盤畫面上不給選做法**：固定送 `D.default_method`（現在是 U 多方聯軍）。
      後端 `auto_fire.METHODS` 仍然支援 A，⛔ 不要為了配合畫面把它拆掉 ——
      他哪天自己手改開關檔成 A，帳本那一列的名字照舊走 `alRecName()`。
   ⛔⛔ **夜盤做法單選**：清單從後端 `x.methods`（正本 night_fire.METHODS），
      ⛔ 前端不准寫死名字，也⛔ 不准做成 checkbox（兩條同時開＝加倉）。
   ⛔ 規則那句話的正本一律在後端（日盤 fire_rule_line()／夜盤 night_fire.state().rule）。 */

/* 組標頭：左邊「日盤／夜盤 ＋ 做法名」、右邊狀態藥丸與「關閉」。 */
function alSelHead(lb,nm,on,live,canOff,offAttr){
 return '<div class="al-selh'+(on?' on':'')+'"><span class="lb">'+esc(lb)+'</span>'+
   (nm?('<span class="nm">'+esc(nm)+'</span>'):'')+
   '<span class="st'+(on?' on':'')+'">'+esc(on?(live?'開著・真錢':'開著・演練'):'關著')+
   '</span><span class="sp"></span>'+
   (canOff?('<button class="off" '+offAttr+'>關閉</button>'):'')+'</div>';
}
/* ⭐⭐ 關掉之前就要知道的事（規格 §2.7）：**已經開出去那一口不會跟著收回來**。
   ⛔ 日盤今天已進場／今天還沒進場／夜盤 —— 三句話**分得出來**，⛔ 不准一律印同一句。 */
function alOffNote(scope,entered,eodT){
 if(scope==='day'&&entered)
   return '<div class="n">關掉之後每個交易日就不再送單。'+
     '<b style="display:block;margin-top:5px;color:var(--gold)">&#9888; 今天已經開出去的那一口不受影響</b>'+
     '<span style="display:block">它照樣走完停損停利與 <b>'+esc(eodT||'')+
     '</b> 的自動平倉。關掉的是<b>之後的日子</b>。</span></div>';
 if(scope==='day')
   return '<div class="n">關掉之後每個交易日就不再送單。'+
     '<span style="display:block;margin-top:5px">&#9888; 如果關的時候手上已經有一口，那一口<b>不受影響</b> —— '+
     '照樣走完停損停利與自動平倉。關掉的是<b>之後的日子</b>。</span></div>';
 return '<div class="n">關掉之後每個晚上就不再送單。'+
   '<span style="display:block;margin-top:5px">&#9888; 如果關的時候那一晚已經進場了，那一口<b>不受影響</b> —— '+
   '照原本的做法抱到 <b>04:58</b>。關掉的是<b>之後的晚上</b>。</span></div>';
}
/* ── 日盤那一組：⛔ 沒有做法可以選 ───────────────────────────── */
function alSwDay(D){
 const armed=!!D.armed, off=D.flag_exists, live=!!D.live;
 const k=armed?D.method:((D&&D.default_method)||'U');
 const C=alConf(D,k);
 const row=(D.days||[]).find(r=>r&&r.date===(D.today||''))||null;
 const entered=!!(row&&row.rec==='result'&&row.ok);
 let h='<div class="al-sel">'+
   alSelHead('日盤', alDayName(D), armed, live, off, 'data-aloff="1"')+
   /* 規則那句話：⛔ 正本在後端（fire_rule_line）。開著的時候多接「今天約 N 點」
      （那是只有今天算得出來的，走 alRuleTxt → D.fast.pts／D.pts_est）。
      ⛔ 「差 0 點算做多」照舊併在這一行裡（整頁只剩這裡在講方向怎麼判）。 */
   '<div class="al-selbody"><div class="ds">'+
     (armed?emb(alRuleTxt(D)):emb(alRuleFull(D,k)||''))+
     '　差 0 點算做多'+(armed&&live?'（你不在也會送）':'')+'</div></div>'+
   '<div class="al-selfoot">';
 if(off) h+=alOffNote('day', entered, D.eod_at);
 else if(!C)
   h+='<div class="err">這個面板還不能從畫面上打開（後端沒有回報現在是真實下單還是演練）。'+
     '要開請自己在 <b>tools/shioaji/'+esc(D.flag||'AUTO_ORDERS_ON')+'</b> 裡寫一個字母。</div>';
 else if(ALON.step==='confirm')
   /* 第二段：⛔ 那句話一律從後端 arm_confirm 拿（⛔ 前端不准自己猜真錢／演練）。 */
   h+='<div class="al-conf'+(C.live?' real':'')+'">'+
     '<div class="q">'+(C.live?'⚠️ ':'')+emb(C.text)+
       '<span class="w2">'+emb(alRuleFull(D,k))+'</span>'+
       '<span class="w2">打開之後<b>每個交易日都會照這條規則看一次，直到你自己關掉</b>。</span></div>'+
     '<div class="btns2">'+
       '<button class="btn go" data-alyes="1"'+(ALON.busy?' disabled':'')+'>'+
         (ALON.busy?'打開中…':'確定，打開')+'</button>'+
       '<button class="btn no" data-alno="1"'+(ALON.busy?' disabled':'')+'>取消</button>'+
     '</div></div>';
 else
   h+='<div class="al-on" style="margin-top:0"><div class="row">'+
     '<button class="btn" data-alon="'+esc(k)+'">打開日盤自動下單</button></div>'+
     '<div class="n">這個開關<b>沒有有效期</b> —— 打開之後每個交易日都會照這條規則看一次，'+
     '直到你自己關掉。<br>按下去會<b>再問你一次</b>。</div></div>';
 if(ALON.err) h+='<div class="err">'+esc(ALON.err)+'</div>';
 if(D.off_msg) h+='<div class="n">'+esc(D.off_msg)+'</div>';
 return h+'</div></div>';
}
/* 現在開著的那一條夜盤做法有沒有特別的出場安排要講（⛔ 句子由後端給，⛔ 前端不准照代號猜）。
   ⇒ 那句話（含 **粗體** 標記，呼叫端走 emb()）；沒有 ⇒ ''。 */
function nfRisk(x){
 const MS=(x&&x.methods)||[], k=(x&&x.method)||null;
 const hit=MS.filter(m=>m.k===k)[0];
 return (hit&&typeof hit.risk==='string')?hit.risk:'';
}
/* ── 夜盤那一組：做法**單選** ＋ 開關 ───────────────────────── */
function alSwNight(){
 const x=NF.data;
 if(!x) return '<div class="al-sel"><div class="al-selh"><span class="lb">夜盤</span>'+
   '<span class="st">'+esc(NF.err||'載入中…')+'</span></div></div>';
 const on=!!x.on, cur=on?x.method:null, MS=x.methods||[];
 const pend=(NFON.step==='confirm')?NFON.mode:null;
 let h='<div class="al-sel">'+
   alSelHead('夜盤', on?nfName(x,cur):'', on, !!x.live, !!x.flag_exists, 'data-nfoff="1"')+
   '<div class="al-picks">';
 MS.forEach(m=>{
   const isCur=(m.k===cur), isPend=(m.k===pend);
   h+='<button class="al-pick'+(on&&isCur?' on':'')+((isPend&&!(on&&isCur))?' sel':'')+
     '" data-nfon="'+esc(m.k)+'"><span class="rd"></span><span class="w">'+
     '<span class="nm">'+esc(m.name||m.k)+
       (on&&isCur?'<span class="now">目前在跑</span>':'')+
       (m.beta?('<span class="beta">'+esc(m.beta)+'</span>'):'')+'</span>'+
     '<span class="ds">'+esc(m.rule||x.rule||'')+'</span></span></button>';
 });
 h+='</div><div class="al-selfoot">';
 /* ⭐ 2026-09-23：確認句**每條做法各一份**（x.arm_confirm[代號]）⇒ 拿「他正要打開的那一條」。
    ⛔ 拿不到那一條的 ⇒ 不畫確認鈕（跟日盤 alConf() 同一個規矩）。
    沒有在選的時候（只是要判斷「後端有沒有回報真錢／演練」）看清單第一條的那一份。 */
 const CS=(x.arm_confirm&&typeof x.arm_confirm==='object')?x.arm_confirm:{};
 const C=pend?(CS[pend]||null):(CS[(MS[0]||{}).k]||null);
 if(pend&&C&&typeof C.text==='string'){
   const R=MS.filter(m=>m.k===pend)[0]||{}, CU=MS.filter(m=>m.k===cur)[0]||{};
   const swap=(on&&pend!==cur);
   let q;
   if(swap)
     /* ⚠️⚠️ 換做法一定是「**先關再開**」，⛔ 不是覆蓋（後端已經開著再 on 會回 409）。 */
     q='要把夜盤的做法從<b>'+esc(CU.name||cur)+'</b>換成<b>'+esc(R.name||pend)+'</b>。'+
       '程式會<b>先關閉、再用新的做法重新開啟</b>（開關檔不覆蓋，只能先關再開）。'+
       '<span class="w2">已經進場的那一晚<b>不受影響</b> —— 照原本的做法（'+esc(CU.name||cur)+
       '）抱到 04:58。換的是<b>之後的晚上</b>。</span>'+
       /* 換過去之後是真錢還是演練，一樣要講（後端那一句，⛔ 前端不自己拼）。 */
       '<span class="w2">'+(C.live?'⚠️ ':'')+emb(C.text)+'</span>';
   else
     q=(C.live?'⚠️ ':'')+emb(C.text)+
       '<span class="w2">要用「<b>'+esc(R.name||pend)+'</b>」開始嗎？</span>';
   q+='<span class="w2">'+esc(R.rule||x.rule||'')+'</span>';
   if(R.beta&&R.note) q+='<span class="w2">&#9888; '+esc(R.name||pend)+' <b>'+esc(R.beta)+
     '</b>：'+esc(R.note)+'</span>';
   if(typeof R.risk==='string'&&R.risk) q+='<span class="w2">&#9888; '+emb(R.risk)+'</span>';
   h+='<div class="al-conf'+(C.live?' real':'')+'"><div class="q">'+q+'</div>'+
     '<div class="btns2">'+
       '<button class="btn go" data-nfyes="1"'+(NFON.busy?' disabled':'')+'>'+
         (NFON.busy?'處理中…':(swap?('確定，換成'+esc(R.name||pend)):'確定，打開'))+'</button>'+
       '<button class="btn no" data-nfno="1"'+(NFON.busy?' disabled':'')+'>取消</button>'+
     '</div></div>';
 } else if(on){
   h+=alOffNote('night')+
     (MS.length>1?'<div class="n" style="margin-top:7px">要換做法就點上面另一條 —— '+
       '<b>換做法也要按兩段</b>（會先關閉再重新開啟）。⛔ 兩條不能同時開。</div>':'');
 } else if(!C||typeof C.text!=='string'){
   h+='<div class="err">夜盤還不能從畫面上打開（後端沒有回報現在是真實下單還是演練）。'+
     esc(x.how_on||'')+'</div>';
 } else {
   h+='<div class="n">點一條做法就會跳出確認。這個開關<b>沒有有效期</b> —— '+
     '打開之後每個晚上都會看一次，直到你自己關掉。</div>';
 }
 if(NFON.err) h+='<div class="err">'+esc(NFON.err)+'</div>';
 if(x.errors) h+='<div class="err">夜盤背景出錯 '+esc(String(x.errors))+' 次：'+
   esc(x.last_err||'')+'</div>';
 if(NF.err) h+='<div class="err">'+esc(NF.err)+'</div>';
 return h+'</div></div>';
}
/* 夜盤第二段真的送出去。⛔ 跟日盤走**同一個出口** pfetch()（自訂標頭＋token 一份就好）。 */
function nfArm(){
 if(NFON.busy) return;
 const m=NFON.mode;
 if(!m){ NFON.step='idle'; NFON.err='沒有選到做法，請再按一次'; alPaint(); return; }
 NFON.busy=true; NFON.err=''; alPaint();
 /* ⚠️⚠️ **換做法一定是「先關再開」，⛔ 不是覆蓋**：後端在開關檔已經存在時
    `POST /api/nightfire/on` 會回 **409**（`O_CREAT|O_EXCL`，⛔ 結構上不覆蓋）。
    ⛔ 所以畫面不可以做成「一鍵切換」——那是後端做不到的事。
    ⚠️ 中間那一瞬間是**真的關著**。`off` 成功但 `on` 失敗 ⇒ 結果是「關著」，
       ⛔ 不是「維持舊的」⇒ 訊息一定要明講（⛔ 不准只寫「切換失敗」）。
    ⛔ 而且**不做自動回復**（失敗就把舊做法重新開回去）：自動幫他重新武裝真錢
       比停在關著更危險（PM Q5 裁示）。 */
 const x=NF.data, wasOn=!!(x&&x.on), curName=x?nfName(x,x.method):'';
 const newName=x?nfName(x,m):m;
 const send=()=>pfetch('/api/nightfire/on',JSON.stringify({mode:m}))
  .then(r=>r.json()
    .catch(()=>({ok:false,msg:'面板回了看不懂的東西（HTTP '+r.status+'）'}))
    .then(j=>Object.assign({},j,{_code:r.status})));
 const first=wasOn
   ? pfetch('/api/nightfire/off').then(r=>r.json().catch(()=>({ok:false,msg:'關不掉'})))
   : Promise.resolve({ok:true});
 first.then(r0=>{
   if(!(r0&&r0.ok)){
     NFON.busy=false; NFON.step='idle'; NFON.mode=null;
     NFON.err='沒有換成「'+newName+'」：舊的那一條關不掉（'+((r0&&r0.msg)||'')+
       '），現在還是「'+curName+'」。';
     return null;
   }
   return send();
 }).then(r=>{
   if(r===null) return;
   NFON.busy=false; NFON.step='idle'; NFON.mode=null;
   if(r&&r.ok){ NFON.err=r.warn||''; return; }
   NFON.err=wasOn
     /* ⛔ 這句話是 `esc()` 之後印出去的 ⇒ ⛔ 不准放 HTML／Markdown 星號（會原樣印在他臉上）。 */
     ? ('⚠️ 已經關閉，但沒有換成「'+newName+'」（'+((r&&r.msg)||'打不開')+
        '）—— 夜盤現在是關著的。')
     : ((r&&r.msg)||'打不開');
 }).catch(()=>{ NFON.busy=false; NFON.step='idle'; NFON.mode=null;
   NFON.err=wasOn?('⚠️ 連不上面板 —— 夜盤可能已經關閉而且沒有換成「'+newName+
                   '」，請看上面的狀態。'):'打不開：連不上面板'; })
  .then(()=>nfFetch(true));
}

/* ⭐⭐ 「打開自動下單」——**這是這個面板上唯一一顆會武裝真錢的鈕**。
   ⛔ 兩段式：第一段（選做法）**一個請求都不送**，只把畫面換成確認條；
      第二段按「確定，打開」才會真的打 POST /api/fire/on。
   ⛔ 確認條那句話（真錢／演練）**只從後端拿**（D.arm_confirm.text），
      ⛔ 前端不准自己判斷 —— 這一頁另外有 D.live，但那句話的措辭是後端的正本，
      兩邊各寫一份就一定有一份是舊的。
   ⛔ 後端沒回 arm_confirm ⇒ **不畫開啟鈕**（我們連現在是不是真錢都不知道，
      這種時候給他一顆按鈕比不給更糟）。 */
/* 拿「他正要打開的那個做法」的確認條正本。⛔ 沒有就回 null（呼叫端不准畫按鈕）。 */
function alConf(D,m){
  const C=(D&&D.arm_confirm)||null;
  if(!C||typeof C!=='object') return null;
  const one=C[m];
  return (one&&typeof one.text==='string')?one:null;
}
/* 這個做法的完整規則那句話（⛔ 正本在後端 fire_rule_line()，前端只負責顯示）。 */
function alRuleFull(D,m){ const c=alConf(D,m); return c?String(c.rule_line||''):''; }

/* ⭐ 第二段真的送出去。⛔ 六道防護裡有兩道是請求要帶的（自訂標頭 ＋ token）——
   ⛔ 少帶一個後端就會擋（403），那是刻意的：**別的網頁帶不出這兩樣**。
   ⚠️ 2026-09-09：改走跟其他每一顆鈕**同一個** `pfetch()` 出口 ——
      這一頁自己寫一份標頭 ＝ 兩把尺，總有一天有一邊沒跟上。 */
function alArm(){
 if(ALON.busy) return;
 const m=ALON.mode;
 /* ⛔ 只准送**後端自己列出來的**那幾個做法（⛔ 不准在前端寫死 'A'）——
    2026-09-17 起有兩個（A 快攻回馬槍／U 多方聯軍），寫死就等於新做法按不動。
    ⚠️ 後端 `fire_arm_on()` 還會再驗一次（那才是最後一道），這裡只是不送明顯錯的。 */
 const MS=((AL.data||{}).methods)||[];
 if(!m||!MS.some(x=>x.k===m)){ ALON.step='idle'; ALON.mode=null;
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
       需求）。⚠️ 2026-09-09：那句人話**搬進 `pfetch()`**（`P403`）——
       十條路由都可能 403，一頁補一句就是十把尺，總有一頁沒補到。
       所以這裡直接用 `r.msg` 就好，⛔ 不要再自己接一句（會變成兩句疊在一起）。 */
    ALON.err=(r&&r.ok)?(r.warn||''):((r&&r.msg)||'打不開'); })
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
 /* 紀錄的日盤／夜盤篩選（⛔ 純畫面，⛔ 一個請求都不送）。 */
 const ch=e.target.closest('[data-alf]');
 if(ch){ ALF=ch.getAttribute('data-alf'); alPaint(); return; }
 /* ⭐ 風控「手動解除」：第一段 ⛔ 零請求；第二段才 POST。 */
 const rc=e.target.closest('[data-rcon]');
 if(rc){ RCAP.step='confirm'; RCAP.err=''; RCAP.ok=''; alPaint(); return; }
 const rn=e.target.closest('[data-rcno]');
 if(rn){ if(rn.disabled) return; RCAP.step='idle'; RCAP.err=''; alPaint(); return; }
 const ry=e.target.closest('[data-rcyes]');
 if(ry){ if(ry.disabled) return; rcArm(); return; }
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
 /* 「到開關」＝**純導覽**：⛔ 一個請求都不送，只把最底下那張卡捲到眼前。 */
 const jp=e.target.closest('[data-aljump]');
 if(jp){ const c=document.getElementById('alswitchcard');
   if(c) c.scrollIntoView({behavior:'smooth',block:'start'}); return; }
 /* ⭐ 夜盤那幾顆：⛔ 跟日盤同一套規矩（第一段零請求、第二段才 POST、關不跳確認）。
    ⛔⛔ 開著時點**目前那一條** ⇒ 什麼都不做（⛔ 不跳確認、⛔ 零請求）。 */
 const non=e.target.closest('[data-nfon]');
 if(non){ if(non.disabled) return;
   const k=non.getAttribute('data-nfon'), x=NF.data;
   if(x&&x.on&&x.method===k){ NFON.step='idle'; NFON.mode=null; NFON.err=''; alPaint(); return; }
   NFON.step='confirm'; NFON.mode=k; NFON.err='';
   alPaint(); return; }
 const nno=e.target.closest('[data-nfno]');
 if(nno){ if(nno.disabled) return;
   NFON.step='idle'; NFON.mode=null; NFON.err=''; alPaint(); return; }
 const nyes=e.target.closest('[data-nfyes]');
 if(nyes){ if(nyes.disabled) return; nfArm(); return; }
 const noff=e.target.closest('[data-nfoff]');
 if(noff){ if(noff.disabled) return;
   noff.disabled=true;
   pfetch('/api/nightfire/off')
    .then(r=>r.json())
    .then(r=>{ if(!r.ok&&r.msg) alert(r.msg); })
    .catch(()=>{ alert('關不掉：連不上面板'); })
    .then(()=>{ noff.disabled=false; nfFetch(true); });
   return; }
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
   /* ⛔ 「送出去了」跟「已經出場了」是兩件事，⛔ 不准寫同一句 —— 停利在券商成交的
      那些天，`auto_fire` 那一側結構上看不到出場（見 fire_real_pairs 的說明），
      所以這裡跟紀錄清單一樣，出場那半從唯讀比對 `real_trades/` 的結果拿。 */
   const R=(D.real||{})[r.date]||null, st=R?R.state:null, done=(st==='ok');
   const head=done?('已出場（'+esc(rwhy({reason:R.why}))+'）'):esc(live_word(r));
   /* ⭐ 2026-09-15 晚上：看得出是「快攻」（09:03:30）還是「回馬槍」（09:15）送的 */
   const leg=alLeg(D,r);
   return '<div class="t">'+head+'：'+esc(alRecName(D,r)||'')+(leg?'・'+esc(leg):'')+' → '+esc(dir)+
     '　1 口</div><div class="d">進場 <b>'+esc(alF(r.entry))+'</b>'+
     (done?('　出場 <b>'+esc(alF(R.exit))+'</b>'+
            (R.exit_time?('（'+esc(String(R.exit_time).slice(0,8))+'）'):'')+
            (alN(R.points)!=null?('　<b>'+esc(alSigned(R.points))+'</b> 點'):
              '　<span style="color:var(--gold)">問不到成交價，點數留白</span>'))
          :(/* ⭐ 「開箱」那一口**沒有停利**（r.no_tp）⇒ ⛔ 不可以印一個空的停利格 */
            (r.no_tp?'　停利 <b>不設</b>':'　停利 <b>'+esc(alF(r.tp))+'</b>')+
            /* 2026-09-15：自動下單那一口的停損是 ±0.5%（帳本落地的 sl），不是手動那個 ±RULE_SL */
            (alN(r.sl)!=null?('　停損 <b>'+esc(alF(r.sl))+'</b>'):'')+
            (alN(r.sl_points)!=null?((r.no_tp?'（'+esc(alF(r.sl_points,0))+' 點＝箱子另一端）'
                                             :'（各 '+esc(alF(r.sl_points,0))+' 點）')):'')))+
     /* 回馬槍那一口的參考價是 09:15 的價（帳本 p15），⛔ 不可以標成 09:03:30 的價 */
     (r.leg==='reversal'
       ? '　'+esc(D.signal_at||'')+' 的價 <b>'+esc(alF(r.px_0903))+'</b>　'+esc(r.rev_at||D.rev_at||'')+
         ' 的價 <b>'+esc(alF(r.p15))+'</b>'
       : '　'+esc(D.signal_at||'')+' 的價 <b>'+esc(alF(r.px))+'</b>')+
     '　滑價 <b>'+esc(alSigned(r.slip))+'</b> 點'+
     /* ⛔ 「開箱」那一口照規則就沒有停利單 ⇒ ⛔ 不可以跳「停利單沒掛上去」那句（那是出事才該說的），
        但**券商端一張單都沒有**這件事要照實講（PM 2026-09-16 指定）。 */
     (r.no_tp?'<br><span style="color:var(--gold)">開箱：券商端無掛單 —— '+
        '這一口沒有停利單、永豐又沒有停損單，<b>停損與收盤平倉都靠面板</b>。</span>'
       :(((!done&&r.has_target)||done)?'':'　<span style="color:var(--gold)">停利單沒掛上去，請自己到大戶投補掛</span>'))+
     (r.warn?'<br><span style="color:var(--gold)">'+esc(r.warn)+'</span>':'')+
     /* ⛔ 對不起來要**說出來**（示警但不擋）：留白跟「那口其實沒平掉」長得一樣。 */
     ((st==='none'||st==='many')?'<br><span style="color:var(--gold)">'+
       '⚠️ 對不到這一趟來回的紀錄（出場價與點數留白）'+
       (st==='many'?'——同一天有好幾筆對得上，不挑':'')+
       '，請自己到大戶投確認那口部位。</span>':'')+'</div>';
 }
 if(r.rec==='result'&&!r.ok)
   return '<div class="t off">沒有送成</div><div class="d">'+
     esc(r.why_msg||alWhy(r.why))+'</div>';
 if(r.rec==='fire')      /* stage 停在 sending ＝ 決定送單之後程式中斷了 */
   return '<div class="t off">不知道下場</div><div class="d">'+
     esc(r.why_msg||alWhy('crashed'))+'</div>';
 /* ⭐ 2026-09-15 晚上：09:03:30 不夠快 ⇒ wait（還沒有定論）。⛔ 「等 09:15 中」與「09:15 過了卻沒留下紀錄」
    是兩件事，⛔ 不准寫同一句（後者＝那一刻面板沒開著／佇列滿了，不補單）。 */
 if(r.rec==='wait'){
   const revS=alSecs(r.rev_at||D.rev_at), rv=esc(r.rev_at||D.rev_at||'');
   if(nowS!=null&&revS!=null&&nowS<revS+10)
     return '<div class="t off">等 '+rv+' 看反轉</div><div class="d">'+
       esc(r.why_msg||alWhy('wait_rev'))+'</div>';
   return '<div class="t off">等反轉那一刻沒有留下紀錄</div><div class="d">'+esc(r.why_msg||'')+
     ' —— 但 '+rv+' 那一刻面板沒有跑到這一段（沒開著、或那時還在啟動）。<b>不補單</b>。</div>';
 }
 if(r.why==='no_reversal')
   return '<div class="t off">沒有反轉</div><div class="d">'+esc(r.why_msg||alWhy(r.why))+'</div>';
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
   /* ⛔⛔ **那一口已經出場了就不要再預告收盤平倉**（2026-09-10）：停利在券商成交的
      日子最常見，`auto_fire` 那一側不會留下 `eod` 那一列（它 13:43:30 才會跑），
      舊版於是在盤中一直寫「13:43:30 會自動平倉」、收盤後又跳金色警示
      「沒有留下收盤平倉的紀錄」—— **兩句在那一天都是假的**。
      ⛔ 這是第七種結局，有自己的一句話（⛔ 不准跟那六種混）。 */
   const R=(D.real||{})[r.date]||null;
   if(R&&R.state==='ok')
     return '<div class="d">這一口'+(R.exit_time?('已經在 <b>'+esc(String(R.exit_time).slice(0,8))+
       '</b> 出場了'):'已經出場了')+'（'+esc(rwhy({reason:R.why}))+'），'+
       '不需要 '+eodT+' 的自動平倉。</div>';
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
/* ⭐⭐ 2026-09-16「多方聯軍」：今天**三個候選各一列**（PM 裁示 4 的帳本形狀直接上畫面）。
   ⛔ 名字與時刻一律從後端 D.cands（auto_fire.CANDS／CAND_NAME／CAND_AT），⛔ 前端不准寫死。
   每一列的字從哪來（⛔ 不在前端重寫規則）：
     ・帳本那個候選有列 ⇒ 用那一列的 why_msg（那是送單那一刻寫下的定論）
     ・開箱還沒有定論 ⇒ 用後端 D.orb 的 stage／msg
       （09:05~突破之間就是 PM 裁示 3 指定的那句「箱子已畫好（上緣 X／下緣 Y），等突破」）
     ・純回馬還沒有定論 ⇒ 照快攻那一列推（只有「不快」的日子才有這個候選）
   ⚠️ 箱寬濾網**照回測口徑在突破那一刻才判** ⇒ 突破之前印不出「箱子太窄」，這是刻意的
      （⛔ 不准為了畫面好看改成「09:05 用箱子最後一筆當分母」—— 那是第二把尺）。 */
function alCandRow(D,r,c){
 const rows=(r&&r.cand_rows)||[], hit=rows.filter(x=>x.cand===c.k)[0]||null;
 let txt='', tone='';
 if(hit){
   if(hit.rec==='result'&&hit.ok){ txt='✔ 這一口是它送的'; tone='gold'; }
   else if(hit.rec==='fire'){ txt='送出去了，還不知道結果'; tone='gold'; }
   else txt=String(hit.why_msg||hit.why||'');
 }else if(c.k==='orb'){
   const O=D.orb||{};
   /* ⛔⛔ 2026-09-17（lab-qa 退件 R1）：**箱子寬度歷史還沒有的時候要當場講**。
      那個檔（orb_hist.jsonl）沒建 ⇒ 算不出中位數 ⇒ 開箱這個候選**開箱即不可用**，
      而舊版在 09:05 以前只寫「等 09:05 畫完箱子」⇒ 畫面講三個候選、實際只跑得出兩個。
      ⛔ 不可以安靜地少一個候選 ⇒ 這一句排在最前面（正本在後端 orb_hist_ready）。 */
   if(O.hist_ok===false&&O.hist_msg){ txt=String(O.hist_msg); tone='warn'; }
   /* ⚠️ 2026-09-21：底下那一行（D.gap）已經在報「箱子畫到現在幾點」的時候，
      ⛔ 這一行就不可以再說「之後才看得出來」—— 那兩句擺在一起有一句是假的。 */
   else if(O.stage==='before'&&(D.gap||{}).orb) txt='箱子畫的是 '+esc(O.box_at||'')+'（'+esc(O.box_at||'').slice(6)+' 定案）';
   else txt=O.msg||(O.stage==='before'?('箱子畫的是 '+esc(O.box_at||'')+'，'+esc(O.box_at||'').slice(6)+' 之後才看得出來'):'');
   if(!txt) txt='等 '+esc(O.box_at||'')+' 畫完箱子';
 }else if(c.k==='rev'){
   const f=rows.filter(x=>x.cand==='fast')[0]||null;
   txt=(f&&f.why==='not_fast')?('等 '+esc((D.rule||{}).rev_at||D.rev_at||'')+' 看有沒有反轉')
       :(f?'今天沒有這個候選（只有 09:03:30 判定「不快」的日子才有）':'等 '+esc(D.signal_at||''));
 }else{
   txt='等 '+esc(D.signal_at||'');
 }
 /* ⭐ 2026-09-21「現在還差幾點」：候選還沒有定論的那段時間才有（後端 D.gap）。
    ⛔ 那句話**整句都是後端寫的**（live_panel.fire_gap）—— 前端不准自己組字、
       不准自己拿現價減門檻（那就是第二把尺，而且會跟 09:05／09:15 的定論打架）。
    ⛔ 後端端 null ⇒ **這一行整行不畫**（⛔ 不寫「—」「計算中」充數：留白看得出來是缺）。 */
 const G=(D.gap||{})[c.k];
 const gap=(G&&G.msg)?'<span class="gap">'+esc(String(G.msg))+'</span>':'';
 return '<div class="al-cand'+(tone?' '+tone:'')+'"><span class="k">'+esc(c.name||'')+
   '</span><span class="w">'+emb(String(txt))+gap+'</span></div>';
}
function alCandsHTML(D,r){
 /* ⛔⛔ 2026-09-17：**只有「多方聯軍」才有三個候選**（Benson 裁示：真單繼續跑
    快攻回馬槍）。跑 A 的時候畫這一區就是一句假話（他今天根本沒有開箱這個候選）。
    ⛔ 判準用後端的 D.union，⛔ 前端不准自己比 method === 'U'（那是把代號寫死進畫面）。 */
 if(!(D&&D.union)) return '';
 const cs=(D&&D.cands)||[];
 if(!cs.length) return '';
 return '<div class="al-cands"><div class="hd">今天三個候選（只做多，取最早觸發的那一個）</div>'+
   cs.map(c=>alCandRow(D,r,c)).join('')+'</div>';
}
function live_word(r){ return r.live?'已送出委託單':'演練（沒有真的送出去）'; }
/* 「今天」卡要不要併進紀錄第一列：只有「送出去了 ＋ 已對到出場（state ok）＋ 收盤那段沒有警示」
   這一種結局才併（那是唯一資訊完全重疊的狀態）。⛔ 判準跟 alTodayHTML／alEodHTML 用同一份資料。 */
function alTodayMerged(D,r){
 if(!r||r.rec!=='result'||!r.ok) return false;
 const R=(D.real||{})[r.date]||null;
 if(!R||R.state!=='ok') return false;
 if(r.eod&&r.eod.alarm) return false;
 return true;
}
function alSecs(hms){
 const m=/^(\d\d):(\d\d):(\d\d)/.exec(String(hms||''));
 return m?(+m[1]*3600+ +m[2]*60+ +m[3]):null;
}

/* ⭐⭐ 紀錄清單（2026-09-10 從表格改成 `.trade` 卡片）。
   ⛔⛔ **這是照抄，不是重新設計。** 正本是練習的 `row(t,ns)` 與真實的 `realCard(t)`
   （2026-09-03 Benson 退件：「真實的交易紀錄要跟練習的交易紀錄的形式長的一樣，
     我不是說過了嗎」）—— 在他所有看得到紀錄的地方（手機 App／練習成績／真實成績），
   紀錄都是同一張卡片，這一頁不可以是唯一的例外。
   沿用：`.trade` / `.tr-date` / `.dir` / `.tr-px` / `.arrow` / `.tag` / `.tr-res` / `.list`。
   ⚠️ 數字的格式也照抄（`f()` 印價、`pm()` 印點數）——
      ⛔ 不要在這裡自己改成一位小數，那就又是「只有這一頁不一樣」。

   ⛔ 硬限制（`hold-to-fire.mjs` ⑧b6 記過的那條）：`.tr-px` 只有 **157.6px**，
      `.tag` **最多 4 個字** —— 6 字 ＝ 161px 會折行，那張卡 65px 變 80px，
      跟練習的卡片就不一樣高了，而「形式長的一樣」正是這一版的要求。
      所以 tag 一律 ≤4 字：停利／停損／收盤／持有中／對不起來／演練／沒送／下落不明。

   ⛔ 自動下單獨有的三件事（做法／滑價／模擬那邊）擠不進 `.tr-px` ⇒ 另起一行 `.al-meta`
      （樣式逐字照抄 `.trade .noteline`）。 */
/* 後端有沒有端出「那一口後來怎麼了」那一份對照。⛔ 沒有 ≠ 對不起來。 */
function alHasReal(D){ return !!(D&&D.real&&typeof D.real==='object'); }
function alSimTxt(D,r){
 const s=(D.sim||{})[r.date]||null;
 if(!s) return '模擬那邊 —';
 if(s.miss) return '模擬那邊也沒記到';
 const run=(s.runs||{})[r.method||'A']||null;
 return '模擬那邊 '+(run&&alN(run.pts)!=null?alSigned(run.pts):'—');
}
function alCard(D,r,isToday){
 /* ⛔ 出場那半的來源是後端唯讀比對 `real_trades/` 的結果（見 fire_real_pairs）。
    ⛔ 對不到就留白 —— 不猜輸贏、不拿現價頂。
    ⚠️ **「後端根本沒有端出 `real`」跟「對不到」是兩件事**（舊版後端／別的治具）：
       ⛔ 前者不可以寫成「對不起來」——那是一句假話（我們根本沒查過）。 */
 const R=alHasReal(D)?((D.real[r.date])||null):undefined, st=R?R.state:(R===null?null:'nodata');
 const sent=(r.rec==='result'&&r.ok), done=(st==='ok');
 const pts=done?alN(R.points):null;
 /* ⛔ 算得出點數才有輸贏色；還開著／對不起來／演練一律維持 `--ghost` 灰
    （`.trade::before` 的預設）—— 跟 `realCard()` 同一條規矩，
    勝敗也同一套定義：**點數 > 0 才算勝，0 算敗**。 */
 const cls=(pts==null)?'':(pts>0?' win':' loss');
 /* ⛔ 每一種狀態一個 tag，⛔ 一種都不准跟別種寫同一個字
    （「還開著」跟「對不起來」長得像，但一個是正常、一個要他去大戶投看）。 */
 let tag;
 /* ⭐ 2026-09-15 晚上：「等反轉」（wait，還沒定論）與「沒反轉」（no_reversal 終局）各自一個 tag（≤4 字） */
 if(!sent) tag=(r.rec==='fire')?'下落不明':(r.rec==='wait'?'等反轉':(r.why==='no_reversal'?'沒反轉':'沒送'));
 else if(done) tag=rwhy({reason:R.why});
 else if(st==='drill') tag='演練';
 else if(st==='open') tag='持有中';
 else if(st==='nodata') tag='送出了';    /* 後端沒查 ⇒ 只講我們知道的那半 */
 else tag='對不起來';
 const px=sent
   ? f(r.entry)+'<span class="arrow">&rarr;</span>'+
     (done?(alN(R.exit)==null?'—':f(R.exit)):'—')+' <span class="tag">'+tag+'</span>'
   : '— <span class="tag">'+tag+'</span>';
 const et=esc(String(r.entry_time||'').slice(0,8)), xt=esc(String((R&&R.exit_time)||'').slice(0,8));
 let meta;
 /* ⚠️ 「為什麼沒送」那句話**包在 `.why` 裡**（舊版表格的 `td .why` 同一個界定）：
    它印的是後端那句原話（例：「讀到『C』，只認得 A 或 B」）—— 講的是
    **你該往那個檔案裡寫什麼**，不是「這個做法叫什麼名字」，所以它不在
    「孤立 A／B 零命中」那把尺裡。⛔ 沒有這個包裝的話，那把尺會被自己的原話打紅，
    而唯一的「修法」就是去改後端那句話 —— 那是把尺弄壞不是把東西修好。 */
 if(!sent) meta='<span class="why">'+esc(r.why_msg||alWhy(r.why))+'</span> · '+
   esc(alSimTxt(D,r));
 /* 今天版（「今天」卡併進來的那一列）：把原本「今天」卡才有的兩樣（「不需要收盤平倉」／
    後端的 warn）接在同一行 —— 只講一次。
    ⚠️ lab-ux demo 這一行還有「（signal_at 的價 X）」：實測塞進去會超過 .al-meta 的 609px
       （670px，被 ellipsis 吃掉尾巴的「13:43:30 不會再平倉」），而那個價 ＝ 進場價 − 滑價、
       兩個都在這張卡上 ⇒ 拿掉它，不拿掉收盤那句。⛔ 不准折行（卡片要跟其他列一樣高）。 */
 else if(done&&isToday) meta=esc(alRecName(D,r)||'—')+(alLeg(D,r)?'・'+esc(alLeg(D,r)):'')+' · '+(et?(et+' 進'):'—')+
   (xt?(' → '+xt+' 出'):'')+
   ' · 滑價 '+esc(alSigned(r.slip))+' · '+esc(alSimTxt(D,r))+
   (D.eod_at?(' · '+esc(D.eod_at)+' 不會再平倉'):'')+
   (r.warn?(' · <span style="color:var(--gold)">'+esc(r.warn)+'</span>'):'');
 else if(done) meta=esc(alRecName(D,r)||'—')+(alLeg(D,r)?'・'+esc(alLeg(D,r)):'')+' · '+(et?(et+(xt?' → '+xt:'')):'—')+
   ' · 滑價 '+esc(alSigned(r.slip))+' · '+esc(alSimTxt(D,r));
 else if(st==='none'||st==='many') meta=esc(alRecName(D,r)||'—')+
   (et?(' · '+et+' 送出'):'')+' · 對不到那一趟來回的紀錄，出場請到大戶投看';
 else meta=esc(alRecName(D,r)||'—')+(alLeg(D,r)?'・'+esc(alLeg(D,r)):'')+(et?(' · '+et+' 送出'):'')+
   ' · 停利掛 '+esc(alF(r.tp,0))+' · 滑價 '+esc(alSigned(r.slip))+
   ' · '+esc(alSimTxt(D,r));
 /* 今天版：金框、日期欄寫「今天」、點數旁掛「已出場」標（⛔ 標掛在 .tr-res 前面，
    不塞進 157.6px 的 .tr-px —— 塞進去會折行，卡片就跟練習那份不一樣高了）。 */
 return '<div class="trade'+cls+(isToday?' tr-today':'')+'"><div class="tr-top">'+
   '<span class="tr-date">'+(isToday?'今天':esc(r.date?r.date.slice(5):''))+'</span>'+
   (sent&&(r.dir==='long'||r.dir==='short')
     ? '<span class="dir '+(r.dir==='long'?'l':'s')+'">'+
       (r.dir==='long'?'▲ 多':'▼ 空')+'</span>' : '')+
   '<span class="tr-px">'+px+'</span>'+
   (isToday&&done?'<span class="tr-chip">已出場</span>':'')+
   '<span class="tr-res '+(pts==null?'na':(pts>0?'r-win':'r-loss'))+'">'+
     (pts==null?'—':pm(pts))+'</span></div>'+
   '<div class="al-meta">'+meta+'</div></div>';
}
/* ⚠️ 2026-09-23 v3：舊的 alTblHTML() 併進 alRows()（日盤與夜盤合併成同一份清單）。
   ⛔ `days.slice(0,60)` 那個 60 跟後端 `FIRE_REAL_DAYS` 是**同一個數**：後端只對那幾天
      比對出場，多印一天就會有一張永遠寫「對不起來」的卡（而那是假的）——現在寫在 alRows()。 */

/* ⛔ 常態統計不畫；**異常**才畫（沿用【模擬】那一頁 atNotesHTML 的規矩）。
   ⛔ 但「異常」一項都不准少 —— 安靜地少是這個專案明令禁止的失敗模式。 */
function alNotesHTML(D,days){
 const L=D.ledger||{}, out=[];
 /* ⛔ fire + result + skip + eod + wait + bad ＝ 檔案總列數（收盤平倉那一列、等 09:15 那一列也要有去處）。 */
 const tot=alN(L.total)||0, sum=(alN(L.fire)||0)+(alN(L.result)||0)+(alN(L.skip)||0)+
   (alN(L.eod)||0)+(alN(L.wait)||0);
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
 /* ⛔⛔ 「對不到出場紀錄」要**示警但不擋**（2026-09-10）：那一天的卡片上是留白的，
    而**留白跟「那口其實沒平掉」長得一模一樣** —— 不講的話他只會覺得畫面壞了，
    講了他才知道要去大戶投看。⛔ 這跟上面那條「收盤沒平掉」是兩件事，不准併成一句
    （一個是程式知道自己沒平，一個是程式對不上帳）。 */
 const R=D.real||{};
 const nomatch=days.filter(r=>r&&R[r.date]&&
   (R[r.date].state==='none'||R[r.date].state==='many')).length;
 if(nomatch) out.push('⚠️ 有 '+nomatch+' 天對不到出場紀錄（那幾天的出場價與點數留白），'+
   '請自己到大戶投確認');
 /* 模擬那邊有記、這邊卻連一列都沒有 ⇒ 兩頁不同步（面板版本不一致或接線掉了） */
 const miss=Object.keys(D.sim||{}).filter(d=>!days.some(r=>r&&r.date===d)).length;
 if(miss) out.push('模擬那一頁有 '+miss+' 天，這裡沒有對應的紀錄');
 if(D.armed&&!D.started) out.push('⚠️ 開關是開的，但送單執行緒沒有起來');
 if(D.armed&&!D.wired) out.push('⚠️ 開關是開的，但面板沒有把自動下單接起來');
 return out.map(t=>'<span>'+esc(t)+'</span>').join('<span class="sep">·</span>');
}

/* ══════════ 【交易分析師】右上角信件（2026-09-24）══════════
   ⛔ 唯讀：列表與全文都是後端 analyst.py 給的；前端只負責畫。
   ⛔ 數字一律來自週報裡的 facts（程式算的），⛔ 前端不自己算。
   讀過 ⇒ POST /api/analyst/read（面板）；手機讀過會經 GitHub 同步回來（analyst.pull_phone_reads）。 */
var AN={items:[],unread:0,err:'',rep:null};
const AN_SVG='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="M3.5 6.5 12 13l8.5-6.5"/></svg>';
const AN_TAG={data:'有數據',judge:'判讀・未驗證'};
function anTag(t){ return t?'<span class="an-tag '+(t==='data'?'data':'judge')+'">'+esc(AN_TAG[t]||t)+'</span>':''; }
function anLamp(l,w){ return '<span class="an-lamp '+esc(l||'')+'">'+esc(w||({ok:'正常',wn:'要注意',bd:'要處理'})[l]||'—')+'</span>'; }
function anPts(v){ if(v==null||isNaN(v)) return '—'; const x=Math.round(v*10)/10;
  return '<span class="'+(x>0?'up':(x<0?'down':''))+'">'+(x>0?'+':(x<0?'−':''))+Math.abs(x).toLocaleString()+'</span>'; }
function anPaintBtn(){
 const b=document.getElementById('anmail'); if(!b) return;
 b.innerHTML=AN_SVG+(AN.unread?'<span class="bdg">'+AN.unread+'</span>':'');
 b.classList.toggle('has',AN.unread>0);
 b.title=AN.unread?('分析師週報：'+AN.unread+' 份沒讀'):'分析師週報';
}
function anFetch(){
 return fetch('/api/analyst/index',{cache:'no-store'}).then(r=>r.json()).then(x=>{
   if(x&&x.ok){ AN.items=x.items||[]; AN.unread=x.unread||0; AN.err=''; }
   else AN.err=(x&&x.msg)||'讀不到週報';
   anPaintBtn(); if(!document.getElementById('anpop').hidden) anList();
 }).catch(()=>{ AN.err='連不上面板'; anPaintBtn(); });
}
function anList(){
 const p=document.getElementById('anpop'), b=document.getElementById('anmail');
 const r=b.getBoundingClientRect();
 p.style.top=(r.bottom+10)+'px'; p.style.right=Math.max(12,window.innerWidth-r.right)+'px';
 let h='<div class="an-ih"><b>分析師週報</b><span>'+(AN.unread?AN.unread+' 份沒讀':'全部讀過了')+'</span></div>';
 if(AN.err) h+='<div class="an-empty">'+esc(AN.err)+'</div>';
 else if(!AN.items.length) h+='<div class="an-empty">還沒有週報。每週六早上會自動產生第一份。</div>';
 else h+=AN.items.map(it=>'<button class="an-item'+(it.read?'':' unread')+'" data-anid="'+esc(it.id)+'">'+
   '<span class="dot"></span><span class="bd"><span class="r1"><span>'+esc(it.id.replace('-W',' 第 '))+' 週　'+esc(it.range||'')+'</span>'+
   '<small>'+esc(String(it.made_at||'').slice(5,10))+'</small></span>'+
   '<span class="ln">'+esc(it.line||'')+'</span><span class="ch">'+(it.read?'':'<span class="an-new">未讀</span>')+
   anLamp(it.lamp,it.lamp_word)+(it.n_recs?'<span class="an-tag judge">'+it.n_recs+' 條建議</span>':'')+'</span></span></button>').join('');
 p.innerHTML=h; p.hidden=false;
}
function anSec(title,sub,body){ return '<div><div class="sec-head"><h2>'+esc(title)+'</h2><span class="count">'+esc(sub||'')+'</span></div><div class="an-card">'+body+'</div></div>'; }
function anReport(R){
 const F=R.facts||{};
 const news=(R.news||[]).map(n=>'<div class="an-nw"><div class="top"><span>'+esc(n.date)+'</span>'+anTag(n.tag)+'</div>'+
   '<h3>'+esc(n.title)+'</h3><p>'+esc(n.summary)+'</p>'+
   ((n.impacts||[]).length?'<div class="an-imp">'+n.impacts.map(i=>'<b>'+esc(i.who)+'</b><span>'+esc(i.text)+'</span>').join('')+'</div>':'')+
   '<div class="an-src">來源：'+(n.sources||[]).map(s=>'<a href="'+esc(s.url)+'" target="_blank" rel="noopener noreferrer">'+esc(s.title)+'</a>').join('、')+'</div></div>').join('');
 const cal=(R.calendar||[]).length?'<div class="an-rows">'+R.calendar.map(c=>'<div class="r"><span class="k">'+esc(c.when)+'</span><span class="v">'+
   anTag(c.tag)+' '+esc(c.event)+(c.who?'（'+esc(c.who)+'）':'')+(c.history?'<small>'+esc(c.history)+'</small>':'')+'</span></div>').join('')+'</div>':'<div class="an-empty">這週沒有特別要注意的事。</div>';
 const env=(R.env||[]).map(e=>'<div class="an-nw"><div class="top">'+anTag(e.tag)+(e.status?'<span class="an-lamp">'+esc(e.status)+'</span>':'')+'</div><h3>'+esc(e.title)+'</h3><p>'+esc(e.text)+'</p></div>').join('');
 const recs=(R.recs||[]).length?R.recs.map(r=>'<div class="an-rec"><div>'+anTag(r.tag)+'</div><h3>'+esc(r.title)+'</h3><p>'+esc(r.body)+'</p><div class="ask">要你決定：'+esc(r.ask)+'</div></div>').join(''):'<div class="an-empty">這週沒有建議。</div>';
 const st=(F.strategies||[]).map(s=>'<div class="an-st"><div class="hd"><b>'+esc(s.name)+' <small class="an-note">'+esc(s.sub||'')+'</small></b>'+anLamp(s.lamp==='ok'?'ok':(s.lamp==='na'?'':'wn'),s.lamp_word)+'</div>'+
   '<div class="an-rail"><div><div class="k">本週模擬</div><div class="v">'+anPts(s.week_sim_pts)+' <small class="an-note">'+s.week_sim_n+' 筆</small></div></div>'+
   '<div><div class="k">本週真單</div><div class="v">'+anPts(s.week_real_pts)+' <small class="an-note">'+s.week_real_n+' 筆</small></div></div>'+
   '<div><div class="k">近 15 筆每筆／歷史</div><div class="v">'+anPts(s.avg15)+' / '+anPts(s.avg_all)+'</div></div></div>'+
   '<div class="an-note">'+((s.pairs||[]).length?'真單 vs 模擬：'+s.pairs.map(p=>esc(p.d.slice(5))+' 差 '+(p.diff>0?'+':'')+p.diff+' 點').join('、'):'本週沒有可以對帳的真單')+'</div></div>').join('');
 const mk=((F.market||{}).cards||[]).map(c=>'<div class="an-st"><div class="hd"><b>'+esc(c.title)+'</b><span class="num">'+(c.value==null?'—':esc(c.value)+esc(c.unit||''))+'</span></div>'+
   (c.pct==null?'':'<div class="an-pos"><i style="left:'+Math.max(0,Math.min(100,c.pct))+'%"></i></div><div class="an-note">過去一年第 '+c.pct+' 百分位'+(c.flag_word?'・'+esc(c.flag_word):'')+'</div>')+'</div>').join('')||'<div class="an-empty">'+esc((F.market||{}).err||'沒有市場資料')+'</div>';
 const S=F.system||{}, K=F.risk||{};
 const sys='<div class="an-rows">'+
   '<div class="r"><span class="k">日盤送單</span><span class="v">'+(S.sent_day||0)+' 筆（成交 '+(S.ok_day||0)+'）</span></div>'+
   '<div class="r"><span class="k">夜盤送單</span><span class="v">'+(S.sent_night||0)+' 筆（成交 '+(S.ok_night||0)+'）</span></div>'+
   '<div class="r"><span class="k">進場滑價</span><span class="v">'+(S.slip_avg==null?'—':'平均 '+S.slip_avg+' 點（'+S.slip_n+' 筆）')+'</span></div>'+
   '<div class="r"><span class="k">送出沒撮到</span><span class="v">'+(S.ioc_nofill||0)+' 次</span></div>'+
   '<div class="r"><span class="k">本月風控</span><span class="v">'+anPts(K.pnl)+' / −'+Math.round(K.cap||0).toLocaleString()+' 點'+(K.blocked?'（已停）':'')+'</span></div>'+
   (S.problems||[]).map(p=>'<div class="r"><span class="k">⚠️</span><span class="v">'+esc(p.what)+'（'+p.n+' 次）</span></div>').join('')+'</div>';
 const cand=(F.candidates||[]).map(c=>'<div class="an-st"><div class="hd"><b>'+esc(c.name)+'</b><span class="num">'+c.diff_n+' / '+c.need+'</span></div>'+
   '<div class="an-bar"><i style="width:'+Math.min(100,Math.round(c.diff_n*100/(c.need||25)))+'%"></i></div>'+
   '<div class="an-note">只算跟「'+esc(c.base)+'」不一樣的那幾筆'+(c.diff_avg==null?'':'・每筆差 '+(c.diff_avg>0?'+':'')+c.diff_avg)+'（回填期每筆差 '+(c.back_avg==null?'—':(c.back_avg>0?'+':'')+c.back_avg)+'）</div></div>').join('');
 return '<div class="an-verdict">'+anLamp(R.verdict&&R.verdict.lamp)+'<p>'+esc((R.verdict||{}).line||'')+'</p></div>'+
  '<div class="an-cols"><div class="an-stack">'+anSec('國際金融消息','本週 '+(R.news||[]).length+' 則・每則附來源',news)+
  anSec('下週大事','附同類日子的歷史成績',cal)+(env?anSec('大環境觀察','會不會動搖賺錢的前提',env):'')+
  anSec('建議','最多 3 條・決定權在你',recs)+'</div>'+
  '<div class="an-stack">'+anSec('策略健康','模擬定論＋真單',st)+anSec('市場狀態','在過去一年的位置',mk)+
  anSec('系統與風控','本週',sys)+anSec('模擬候選','滿 25 筆才判斷',cand)+'</div></div>'+
  '<div class="an-foot">分析師不預測漲跌、不給進出場方向、不碰下單程式與開關；所有決定由你做。「判讀・未驗證」的只能當研究題目。</div>'+
  '<div class="an-totop"><button data-antop="1">↑ 回到最上面</button></div>';
}
function anOpen(id){
 document.getElementById('anpop').hidden=true;
 const sh=document.getElementById('ansheet');
 sh.innerHTML='<div class="an-doc"><div class="an-empty">載入中…</div></div>'; sh.hidden=false;
 fetch('/api/analyst/report?id='+encodeURIComponent(id),{cache:'no-store'}).then(r=>r.json()).then(x=>{
   if(!x||!x.ok){ sh.innerHTML='<div class="an-doc"><div class="an-dh"><span class="t">'+esc((x&&x.msg)||'讀不到')+'</span><button data-anclose="1">關閉 ✕</button></div></div>'; return; }
   const R=x.report;
   sh.innerHTML='<div class="an-doc"><div class="an-dh"><div class="t"><b>'+esc(R.id.replace('-W',' 第 '))+' 週報告</b>'+esc(R.range||'')+'・'+esc(String(R.made_at||'').slice(0,16).replace('T',' '))+' 產出</div>'+
     '<div style="display:flex;gap:8px"><button data-anback="1">‹ 回列表</button><button data-anclose="1">關閉 ✕</button></div></div>'+anReport(R)+'</div>';
   const it=AN.items.find(i=>i.id===id);
   if(it&&!it.read) pfetch('/api/analyst/read',JSON.stringify({id:id})).then(()=>anFetch()).catch(()=>{});
 }).catch(()=>{ sh.innerHTML='<div class="an-doc"><div class="an-dh"><span class="t">連不上面板</span><button data-anclose="1">關閉 ✕</button></div></div>'; });
}
document.addEventListener('click',function(e){
 const mb=e.target.closest('#anmail');
 if(mb){ const p=document.getElementById('anpop'); if(p.hidden){ anList(); anFetch(); } else p.hidden=true; return; }
 const it=e.target.closest('[data-anid]');
 if(it){ anOpen(it.getAttribute('data-anid')); return; }
 if(e.target.closest('[data-antop]')){ document.getElementById('ansheet').scrollTo({top:0,behavior:'smooth'}); return; }
 if(e.target.closest('[data-anclose]')){ document.getElementById('ansheet').hidden=true; return; }
 if(e.target.closest('[data-anback]')){ document.getElementById('ansheet').hidden=true; anList(); return; }
 if(e.target.id==='ansheet'){ document.getElementById('ansheet').hidden=true; return; }
 const p=document.getElementById('anpop');
 if(!p.hidden&&!e.target.closest('#anpop')) p.hidden=true;
});
document.addEventListener('keydown',function(e){ if(e.key==='Escape'){ document.getElementById('ansheet').hidden=true; document.getElementById('anpop').hidden=true; } });
anPaintBtn(); anFetch(); setInterval(anFetch,60000);

tick(); setInterval(tick,500);
</script></body></html>"""

# 下單規則的數字注入前端（⛔ 前端不准自己寫死 ±100／09:03:30 —— 2026-09-14 改規則時就是散在 4 個地方）。
# 在模組層級替換 ⇒ 治具／測試直接拿 live_panel.PAGE 也是替換過的版本。
PAGE = (PAGE.replace("__RULE_TP__", f"{TP_POINTS:g}")
            .replace("__RULE_SL__", f"{SL_POINTS:g}")
            .replace("__RULE_SIGNAL_AT__", SIGNAL_AT))
# ⛔ 漏替換的話前端整段腳本在第一行就炸（ReferenceError）⇒ 寧可面板啟動失敗，也不要畫面空白卻看不出原因
assert "__RULE_" not in PAGE, "PAGE 裡還有沒替換到的下單規則佔位"


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
        # ⛔ 武裝那兩顆只收 POST。沒有 do_HEAD 的話 BaseHTTPRequestHandler 會回 501
        #    （語意上也是拒絕），但這兩顆要回**明確的 405**。
        #    其餘路徑維持原本的行為（這支面板從來不服務 HEAD）。
        if self.path.split("?", 1)[0] in ("/api/fire/on", "/api/nightfire/on", "/api/risk/override"):
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

        # 【夜盤自動下單】⭐⭐ 2026-09-23 Benson 交辦：夜盤也補一顆畫面上的開關。
        #   ⛔⛔ 規矩逐條照抄日盤那兩顆（⛔ 兩邊不一樣就是兩把尺）：
        #     ・開＝**兩段式**（前端）＋這裡的六道防護（入口 `fire_post_guard` 已經過了）
        #     ・`mode` ⛔ **先驗再寫**；已經開著再按 ⇒ 409（⛔ 不覆蓋、不當成換做法）
        #     ・關＝一鍵、⛔ 不跳確認（關掉永遠是安全方向）；`disarm()` 是**改名不刪**
        #   ⛔ 路由是精確比對（`==`），⛔ 不可以 startswith／in。
        #   ⛔⛔ 建檔只在 `night_arm_on()`：`night_fire.py` 對開關檔只准
        #      exists／read_bytes／replace／with_name（test_night_fire.py ⑦ 的 AST 在守）。
        if self.path == "/api/nightfire/on":
            m = body.get("mode") if isinstance(body, dict) else None
            code, out = night_arm_on(m, who=self.client_address[0]
                                     if self.client_address else "?")
            return self._json(code, out)

        # ⭐ 2026-09-24【交易分析師】在面板上讀過那一週 ⇒ 記成讀過（⛔ 只會變成讀過）。
        if self.path == "/api/analyst/read":
            if analyst is None:
                return self._json(503, {"ok": False, "msg": "分析師載入失敗"})
            rid = body.get("id") if isinstance(body, dict) else None
            try:
                changed = analyst.mark_read(rid, via="pc")
            except Exception as e:
                return self._json(500, {"ok": False, "msg": "記不起來：" + str(e)[:150]})
            return self._json(200, {"ok": True, "changed": changed})

        # ⭐ 2026-09-24 風控規則 B 的「手動解除」：⛔ 前端兩段式；⛔ 路由精確比對。
        if self.path == "/api/risk/override":
            code, out = risk_override_on(who=self.client_address[0]
                                         if self.client_address else "?")
            return self._json(code, out)

        if self.path == "/api/nightfire/off":
            try:
                ok, msg = night_fire.disarm()
            except Exception as e:
                return self._json(500, {"ok": False, "msg": "關不掉：" + str(e)[:150]})
            return self._json(200 if ok else 409, {"ok": ok, "msg": msg,
                                                   "armed": night_fire.arm()["on"]})

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

    def _lab_get(self):
        """
        【策略實驗室】兩個唯讀端點。⛔ 只讀 tick_hist/，不碰 broker／auto_fire／任何寫交易紀錄的路徑。
        ⛔ 回測就在**這條 HTTP handler 執行緒**上算（不是主迴圈、不是 shioaji 回呼），
           strategy_lab.run_exclusive 保證同時只有一個查詢在算：第二個回 429。
        路由是精確比對（⛔ 不用 startswith）。
        """
        path, _, qs = self.path.partition("?")
        # 別的網站的分頁不准叫它（一次回測要吃掉零點幾秒 CPU，不能讓外面拿來一直打）
        ok, code, msg = fire_get_guard(self.headers)
        if not ok:
            return self._json(code, {"ok": False, "msg": msg})
        if strategy_lab is None:
            # 模組載入失敗（見檔頭的 try）：這一頁停用，⛔ 面板其他功能照跑
            return self._json(503, {"ok": False, "msg": "策略實驗室載入失敗"})
        if path == "/api/lab/meta":
            try:
                return self._json(200, strategy_lab.meta())
            except Exception as e:
                return self._json(500, {"ok": False, "msg": str(e)[:200]})
        try:
            P = strategy_lab.parse_params(parse_qs(qs, keep_blank_values=True))
        except strategy_lab.BadParam as e:
            return self._json(400, {"ok": False, "msg": str(e)})
        try:
            return self._json(200, strategy_lab.run_exclusive(P))
        except strategy_lab.Busy:
            return self._json(429, {"ok": False, "msg": "還在算上一組"})
        except Exception as e:
            return self._json(500, {"ok": False, "msg": "回測失敗：" + str(e)[:160]})

    def _sim_get(self):
        """
        【策略實驗室】「模擬（不會下單）」唯讀端點 GET /api/sim/state。
        ⛔ 只讀 sim_lanes/ 與 sim_lanes 的記憶體狀態：不抓資料、不寫檔、不碰 broker／auto_fire。
        """
        ok, code, msg = fire_get_guard(self.headers)
        if not ok:
            return self._json(code, {"ok": False, "msg": msg})
        if sim_lanes is None:
            return self._json(503, {"ok": False, "msg": "模擬載入失敗"})
        try:
            return self._json(200, sim_lanes.state())
        except Exception as e:
            return self._json(500, {"ok": False, "msg": "模擬狀態讀取失敗：" + str(e)[:160]})

    def _sim_lane_get(self, qs):
        """
        ⭐ 【模擬】點進去一條策略：唯讀端點 GET /api/sim/lane?key=<lane>（2026-09-17 加）。
        ⛔ 跟 /api/sim/state 同一道防護、同樣只讀 sim_lanes/：不抓資料、不寫檔、不碰 broker／auto_fire。
        ⚠️ 一次端出幾百列 ⇒ **只有點下去才打**，⛔ 不准併進每 60 秒輪詢的 /api/sim/state。
        """
        ok, code, msg = fire_get_guard(self.headers)
        if not ok:
            return self._json(code, {"ok": False, "msg": msg})
        if sim_lanes is None:
            return self._json(503, {"ok": False, "msg": "模擬載入失敗"})
        key = (parse_qs(qs).get("key") or [""])[0]
        try:
            out = sim_lanes.lane_detail(key)
        except Exception as e:
            return self._json(500, {"ok": False, "msg": "模擬紀錄讀取失敗：" + str(e)[:160]})
        if out is None:         # ⛔ 不認得的 key 要說出來，不要默默回一條空的
            return self._json(400, {"ok": False, "msg": "沒有這一條：" + key[:40]})
        return self._json(200, out)

    def _sim_daychart_get(self, qs):
        """
        ⭐ 【模擬】內頁點某一天 ⇒ 那天的 1 分 K ＋ 進出場標記：GET /api/sim/daychart?key=&date=（2026-09-18 加）。
        ⛔ 同一道防護、唯讀：只讀 sim_lanes/ 與本機逐筆／1 分 K，不抓資料、不寫檔、不碰 broker／auto_fire。
        """
        ok, code, msg = fire_get_guard(self.headers)
        if not ok:
            return self._json(code, {"ok": False, "msg": msg})
        if sim_lanes is None:
            return self._json(503, {"ok": False, "msg": "模擬載入失敗"})
        q = parse_qs(qs)
        key, day = (q.get("key") or [""])[0], (q.get("date") or [""])[0]
        try:
            out = sim_lanes.day_chart(key, day)
        except Exception as e:
            return self._json(500, {"ok": False, "msg": "那天的圖讀取失敗：" + str(e)[:160]})
        if out is None:
            return self._json(400, {"ok": False, "msg": "參數不對：" + (key + " " + day)[:60]})
        return self._json(200, out)

    def do_GET(self):
        if self.path.partition("?")[0] in ("/api/lab/meta", "/api/lab/run"):
            return self._lab_get()
        if self.path.partition("?")[0] == "/api/sim/state":
            return self._sim_get()
        if self.path.partition("?")[0] == "/api/sim/lane":
            return self._sim_lane_get(self.path.partition("?")[2])
        if self.path.partition("?")[0] == "/api/sim/daychart":
            return self._sim_daychart_get(self.path.partition("?")[2])
        # ⭐ 2026-09-21【帳戶總覽】的權益曲線（一天一點）。⚠️ **唯讀**。
        #    ⛔⛔ 這是**真實金額** ⇒ 跟 /api/state、/api/fire/state 同一個守衛
        #       （沒有這一道，DNS rebinding 下別的網頁讀得到他的帳戶餘額）。
        #    ⚠️ 當下的數字在 /api/state 的 `equity` 裡（每分鐘更新）；這一支只給歷史，
        #       ⛔ 不要在這裡再算一份「現在多少錢」（那就是第二把尺）。
        if self.path.partition("?")[0] == "/api/account/hist":
            ok, code, msg = fire_get_guard(self.headers)
            if not ok:
                return self._json(code, {"ok": False, "msg": msg})
            try:
                rows = equity_hist_read()[-EQUITY_HIST_MAX:]
            except Exception as e:
                return self._json(200, {"ok": False, "rows": [],
                                        "msg": "讀不出權益紀錄：%s" % str(e)[:120]})
            return self._json(200, {"ok": True, "rows": [
                {"date": r.get("date"), "equity": r.get("equity"),
                 "deposit": r.get("deposit")} for r in rows]})
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
        if self.path.split("?", 1)[0] in ("/api/fire/on", "/api/nightfire/on", "/api/risk/override"):
            return self._json(405, {"ok": False, "msg": "這個端點只收 POST"})
        # ⭐ 【健檢】（2026-09-23）：⛔ **唯讀**、⛔ 一個位元組都不寫、⛔ 不碰 _lock／部位。
        #   ⛔⛔ **不可以掛在高頻輪詢上**：前端只在切進【健檢】那一頁時打一次；
        #      後端整天快取（來源檔 mtime 當快取鍵），重活跑在 health 自己的背景執行緒上
        #      ⇒ 這條 HTTP 路徑本身很輕（只讀 sim_lanes 的定論）。
        #   ⛔ 沒有 token，但有真實的績效數字 ⇒ 跟 /api/state 同一道 GET 守衛。
        # ⭐ 2026-09-24【交易分析師】右上角信件：列表／全文（唯讀；跟 /api/state 同一道 GET 守衛）。
        #   ⚠️ 「手機讀過」的抓取執行緒在**第一次問列表時**才起（⛔ 不改 main()：那段有守衛在比對）。
        if self.path.split("?", 1)[0] in ("/api/analyst/index", "/api/analyst/report"):
            ok, code, msg = fire_get_guard(self.headers)
            if not ok:
                return self._json(code, {"ok": False, "msg": msg})
            if analyst is None:
                return self._json(503, {"ok": False, "msg": "分析師載入失敗"})
            try:
                if self.path.startswith("/api/analyst/index"):
                    analyst.start()
                    items = analyst.index()
                    return self._json(200, {"ok": True, "items": items,
                                            "unread": sum(1 for x in items if not x["read"]),
                                            "hash": analyst.bundle()["hash"]})
                q = parse_qs(urlsplit(self.path).query)
                rep = analyst.load((q.get("id") or [""])[0])
                if rep is None:
                    return self._json(404, {"ok": False, "msg": "找不到這一週的報告"})
                return self._json(200, {"ok": True, "report": rep})
            except Exception as e:
                return self._json(500, {"ok": False, "msg": "分析師讀不到：%s" % str(e)[:150]})
        if self.path.split("?", 1)[0] == "/api/health/state":
            ok, code, msg = fire_get_guard(self.headers)
            if not ok:
                return self._json(code, {"ok": False, "msg": msg})
            if health is None:
                return self._json(503, {"ok": False, "msg": "【健檢】載入失敗，這一頁暫時不能用"})
            try:
                return self._json(200, health.state())
            except Exception as e:
                return self._json(500, {"ok": False, "msg": "健檢算不出來：%s" % str(e)[:150]})
        if self.path.split("?", 1)[0] == "/api/nightfire/state":
            # 夜盤自動下單的狀態（唯讀：開關、今晚看幾點、最近幾列定論）。⛔ 沒有 token，但有真實價格 ⇒ 同一道 GET 守衛。
            ok, code, msg = fire_get_guard(self.headers)
            if not ok:
                return self._json(code, {"ok": False, "msg": msg})
            try:
                out = night_fire.state()
                # ⭐ 2026-09-23：夜盤補了畫面上的開關 ⇒ 確認條那句話的**正本**跟著端出來
                #   （⛔ 前端不准自己判斷現在是真錢還是演練）。
                #   ⛔ `live` 用 `state()` 已經算好的那一個，⛔ 不再問第二次 broker.is_live()。
                # ⭐ 2026-09-23：夜盤的做法清單（畫面上那組**單選**）。
                #   ⛔⛔ **代號與名字的正本是 `night_fire.METHODS`／`METHOD_NAME`** ——
                #      畫面上有幾條完全由那一份決定，⛔ 前端不准自己寫一份，
                #      ⛔ 也**不可以把還沒接好的做法先畫上去**（畫得出來就按得下去）。
                #   ⚠️ 每一條的附加揭露（規則一句話／beta 旗標／歷史數字／有沒有停損）
                #      放在 `NIGHT_METHOD_INFO`，⛔ 一樣是後端的一份（見那個常數）。
                out["methods"] = night_methods(out.get("rules") or out.get("rule"))
                # ⭐ 2026-09-23 夜盤有兩條了 ⇒ 確認句**每條做法各一份**（跟日盤 2026-09-17 同一招）。
                #   ⛔ 只給一份的話，選夜盤跟勢時確認條會寫「照『台積電快攻』跑」（實測抓到）。
                out["arm_confirm"] = {m: night_arm_confirm(out.get("live"), m)
                                      for m in night_fire.METHODS}
                return self._json(200, out)
            except Exception as e:
                return self._json(500, {"ok": False, "msg": "夜盤自動下單狀態讀不出來：%s" % str(e)[:120]})
        if self.path.startswith("/api/fire/state"):
            # ⛔⛔ 這份 JSON 裡有 `token` ⇒ 它自己也要過 ③⑤⑥（2026-09-09 lab-qa）。
            #    沒有這一道的話，DNS rebinding 下別的網站讀得到 token ⇒ 第 ④ 道形同虛設。
            ok, code, msg = fire_get_guard(self.headers)
            if not ok:
                return self._json(code, {"ok": False, "msg": msg})
            try:
                out = auto_fire.state()
                out["sim"] = fire_sim_pairs(out.get("days") or [])
                # ⛔ 「那一口後來怎麼了」**唯讀** real_trades/ 比對出來的
                #    （`auto_fire` 那一側結構上拿不到停利成交的出場價，見上面那一段）。
                #    ⛔ 對不到就留白，⛔ 不挑一筆、⛔ 不拿現價頂。
                out["real"] = fire_real_pairs(out.get("days") or [],
                                              today=out.get("today"),
                                              now=out.get("now"),
                                              eod_at=out.get("eod_at"))
                # ⛔ 「現在是真錢還是演練」那句話**在後端算**（前端不准猜），
                #    而且拿的是 auto_fire 算好的那個 live ⇒ 只有一把尺。
                # ⭐⭐ 2026-09-17：**每個做法各一份**（A 快攻回馬槍／U 多方聯軍）——
                #    兩條規則的說明不一樣，共用一句一定有一邊是假話。
                #    ⛔ 前端拿 `arm_confirm[他選的那個]`，⛔ 不准自己組字。
                out["arm_confirm"] = {m: fire_arm_confirm(out.get("live"), mode=m)
                                      for m in auto_fire.METHODS}
                # ⭐⭐ 今天是不是結算日（2026-09-17 PM 裁示 M1）：⛔ 「判不出來」
                #    一定要上畫面 —— 那一天收盤平倉用的是平常那一組（13:43:30），
                #    而真的是結算日的話市場 13:30 就關了。⛔ 不可以只留在主控台。
                _ep = eod_plan(date.today())
                out["eod_expiry"] = _ep["expiry"]
                out["eod_sure"] = _ep["sure"]
                out["eod_err"] = _ep["err"]
                # 「今天約 N 點」：09:03:30 還沒到（帳本／歷史都還沒有今天的價）時，
                #   拿**現價**照同一支 tpsl_points() 估（⛔ 前端不准自己乘 0.005）。只讀記憶體。
                _tt = CURRENT_STATE.get("today")
                out["pts_est"] = auto_fire.tpsl_points(getattr(_tt, "price", None))
                # ⭐ 2026-09-21：三個候選各一行「現在離門檻還差幾點」（⛔ 唯讀、零 I/O、
                #    ⛔ 不預測）。算不出來 ⇒ None ⇒ 前端就不畫那幾行（⛔ 不寫「—」充數）。
                #    ⛔ 一個 try 隔開：這是看的東西，**不可以讓它把整份狀態帶掉**
                #       （那份狀態裡有他的部位、停損、今天判定）。
                try:
                    out["gap"] = fire_gap(out)
                except Exception as _ge:
                    out["gap"] = None
                    out["gap_err"] = "算不出「還差幾點」：%s" % str(_ge)[:120]
                # ⭐ 2026-09-24 風控規則 B：本月自動單損益與上限（20 秒快取；⛔ 壞掉不帶掉整份）。
                try:
                    out["risk"] = risk_view()
                except Exception as _re:
                    out["risk"] = {"err": "風控算不出來：%s" % str(_re)[:120], "blocked": True}
                # 兩段式確認第二段要帶的 token。跨站讀不到這份 JSON ⇒ 拿不到它。
                out["token"] = FIRE_TOKEN
                return self._json(200, out)
            except Exception as e:
                # ⚠️ **這條退路刻意不帶 token**（⛔ 不是漏掉）。跟 `/api/state`
                #    那條退路**不是同一件事**：
                #    ① 前端的 token 只有**一個**來源，就是 `/api/state`
                #       （`tick()` 與 `ptok()` 那兩行 `PTOK=s.token`，
                #        `test_auto_fire.py` ⑬d 斷言整份前端**剛好兩個** ⇒
                #        這裡加了也沒有人會拿）。
                #    ② `/api/state` 那條退路回的是 **200 ＋ 一份能用的狀態**
                #       （「寧可少一塊資料也不要讓面板瞎掉」），少了 token
                #       他的平倉鈕會當場按不動；這裡回的是 **500 ＝ 這一頁掛了**，
                #       前端 `alFetch()` 當錯誤處理，本來就沒有「還要能按」這回事。
                #    ③ ⛔ token 的強度 ＝「端出它的那些 GET」的強度 ⇒
                #       **少一個端出它的地方就少一份要守的**。
                #    ⛔ 哪天有人讓前端改從 `/api/fire/state` 拿 token，這裡要一起改。
                return self._json(500, {"error": str(e)[:200], "armed": False,
                                        "days": [], "sim": {}, "real": {},
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


# ---------------------------------------------------------------- 【帳戶總覽】
#
# ⭐⭐ 2026-09-21 Benson 要的：「我的期貨帳戶裡面剩下多少錢」，外加三件事
#    ①「錢夠不夠明天那一口」②「今天賺賠多少（券商端的數字）」③ 每天記一個權益數字畫成曲線。
#
# ⛔⛔ **這一整段唯讀**：只叫 `broker.account_margin()`（`api.margin()`），⛔ 一張單都不送。
# ⛔⛔ **真實金額不上傳**：每天那一列寫在 `tools/shioaji/equity/`（已 gitignore），
#    跟 `real_orders/`、`real_trades/` 同一條鐵律 —— repo 是公開的。
# ⚠️ **這是券商端的真相，跟面板自己算的點數不是同一件事**（手續費、稅、其他部位都算進去）。
#    ⇒ 畫面上一律標「券商端」，⛔ 不可以拿它去對策略績效（那會得到兩個都對但不一樣的數字）。
# ⚠️ 節奏：盤中 60 秒一次、其餘 10 分鐘一次，而且 ⛔ **送單那幾刻前後 10 秒不問**
#    （09:03:30／09:15／13:43:30）—— 那幾秒的連線全部留給送單。

EQUITY_DIR = HERE / "equity"            # ⛔ gitignore（真實金額只留在這台電腦）
EQUITY_EVERY = 60.0                     # 盤中多久問一次
EQUITY_EVERY_OFF = 600.0                # 非盤中
EQUITY_BUSY_END = pd.Timestamp("14:30").time()   # 問得密一點的時段結束（日盤收完再留 45 分）
EQUITY_QUIET_S = 10                     # 送單那幾刻前後幾秒不問
EQUITY_WRITE_AFTER = 14 * 3600          # 每天幾點之後才把當天那一列落地（日盤收完）
EQUITY_HIST_MAX = 120                   # 曲線最多畫幾天
EQUITY = {"at": None, "m": None, "err": "還在問券商…（每分鐘更新一次）",
          "lot1": None, "lot1_at": None}
_EQ_HIST = {"key": None, "rows": []}


def _eq_quiet(now):
    """現在是不是「送單那幾刻」⇒ ⛔ 不去問帳戶（連線留給送單）。"""
    sec = now.hour * 3600 + now.minute * 60 + now.second
    for t in (SIGNAL_SEC, REV_SEC, EOD_CLOSE_SEC):
        if t is not None and abs(sec - t) <= EQUITY_QUIET_S:
            return True
    return False


def equity_hist_read():
    """
    讀 `equity/*.jsonl`（一天一列，舊到新）。⚠️ 唯讀、`(mtime,size)` 快取。
    ⛔ 壞列跳過就好：這是給畫面看的曲線，⛔ 不可以因為一列壞掉就整段不顯示。
    """
    try:
        files = sorted(EQUITY_DIR.glob("*.jsonl"))
    except OSError:
        return []
    key = tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in files)
    if _EQ_HIST["key"] == key:
        return list(_EQ_HIST["rows"])
    rows, seen = [], set()
    for p in files:
        try:
            txt = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in txt.splitlines():
            if not line.strip():
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            d = o.get("date") if isinstance(o, dict) else None
            if not isinstance(d, str) or d in seen:
                continue
            seen.add(d)
            rows.append(o)
    rows.sort(key=lambda r: r["date"])
    _EQ_HIST.update(key=key, rows=rows)
    return list(rows)


def _equity_write_day(m, now):
    """
    把今天那一列落地（一天一列、只 append）。⇒ 有沒有真的寫。
    ⛔ 14:00 以前不寫（日盤還沒收完，寫下去的是半路的數字）。
    ⛔ 今天已經有一列就不再寫（⛔ 不覆蓋、不改舊列）。
    """
    if (now.hour * 3600 + now.minute * 60 + now.second) < EQUITY_WRITE_AFTER:
        return False
    today = str(now.date())
    if any(r.get("date") == today for r in equity_hist_read()):
        return False
    row = {"date": today, "at": now.strftime("%H:%M:%S"),
           "equity": m.get("equity_amount"), "avail": m.get("available_margin"),
           "deposit": m.get("deposit_withdrawal"),
           "settle_pl": m.get("future_settle_profitloss"),
           "float_pl": m.get("future_open_position"),
           "fee": m.get("fee"), "tax": m.get("tax"),
           "lot1": EQUITY.get("lot1")}
    try:
        EQUITY_DIR.mkdir(parents=True, exist_ok=True)
        with (EQUITY_DIR / (today[:4] + ".jsonl")).open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        _EQ_HIST["key"] = None          # ⛔ 快取要作廢，不然畫面到明天才看得到這一列
        return True
    except Exception as e:
        EQUITY["err"] = "今天那一列寫不進去：%s" % str(e)[:100]
        return False


def _equity_lot1(m):
    """
    「一口要壓多少保證金」—— **從他自己的帳戶學**：有部位的時候原始保證金是多少就記多少。
    ⛔⛔ 不准寫死一個數字（微台的保證金期交所會調，寫死的那天起畫面就是錯的），
       ⛔ 也不准拿沒有部位時的 0 當答案 ⇒ 沒學到就是**不知道**（畫面照實說）。
    ⚠️ 只在「剛好 1 口」的時候學（他就是只下 1 口；多口會學成 2 倍）。
    """
    im = m.get("initial_margin")
    if not isinstance(im, (int, float)) or im <= 0:
        return
    pos = broker._state.get("position")
    qty = int((pos or {}).get("qty") or 0)
    if qty != 1:
        return
    EQUITY["lot1"] = float(im)
    EQUITY["lot1_at"] = str(date.today())


def _equity_lot1_restore():
    """
    開機時把「上次學到的一口保證金」從落地的紀錄裡讀回來（最新那一列有值的）。
    ⛔ 讀不到就維持 None ＝ **不知道**（畫面照實說），⛔ 不猜一個數字。
    """
    try:
        for r in reversed(equity_hist_read()):
            v = r.get("lot1")
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                EQUITY["lot1"], EQUITY["lot1_at"] = float(v), r.get("date")
                return True
    except Exception:
        pass
    return False


def _equity_month(rows, m, now):
    """這個月帳戶變了多少（⛔ 扣掉出入金 —— 匯錢進去不是賺到）。⇒ dict 或 None。"""
    mth = str(now.date())[:7]
    mine = [r for r in rows if str(r.get("date", ""))[:7] == mth
            and isinstance(r.get("equity"), (int, float))]
    now_eq = m.get("equity_amount")
    if not mine or not isinstance(now_eq, (int, float)):
        return None
    base = float(mine[0]["equity"])
    dep = sum(float(r.get("deposit") or 0) for r in mine)
    return {"from": mine[0]["date"], "n": len(mine), "base": base,
            "now": float(now_eq), "change": round(float(now_eq) - base, 1),
            "deposit": round(dep, 1),
            "net": round(float(now_eq) - base - dep, 1)}


def equity_view(now=None):
    """
    【帳戶總覽】那張卡要的一份（⚠️ 唯讀）。⇒ dict；⛔ 問不到就 `ok:False` ＋ 一句話
    （⛔ 不留上一次的數字在畫面上假裝是現在的）。
    """
    now = now or datetime.now()
    m, err = EQUITY.get("m"), EQUITY.get("err")
    if not m:
        return {"ok": False, "err": err or "還沒問到帳戶餘額", "at": EQUITY.get("at")}
    rows = equity_hist_read()
    lot1, avail = EQUITY.get("lot1"), m.get("available_margin")
    # 「錢夠不夠明天那一口」⛔ 學不到一口要多少 ⇒ 照實說不知道（⛔ 不猜一個數字）
    if not isinstance(avail, (int, float)):
        enough = {"ok": None, "why": "no_avail", "msg": "券商沒給可動用保證金 —— 判不出夠不夠"}
    elif not lot1:
        enough = {"ok": None, "why": "no_lot1",
                  "msg": "還不知道一口要壓多少保證金（等下一次有部位時就學起來）"}
    else:
        ok = avail >= lot1
        enough = {"ok": ok, "why": None, "need": lot1, "avail": float(avail),
                  "since": EQUITY.get("lot1_at"),
                  "msg": ("可動用 %s／一口約 %s —— %s"
                          % (format(int(round(avail)), ","), format(int(round(lot1)), ","),
                             "夠下一口" if ok else "⚠️ 不夠，明天那一口會送失敗"))}
    fee = float(m.get("fee") or 0) + float(m.get("tax") or 0)
    pl = None
    if isinstance(m.get("future_open_position"), (int, float)) \
            and isinstance(m.get("future_settle_profitloss"), (int, float)):
        pl = round(float(m["future_open_position"]) + float(m["future_settle_profitloss"])
                   - fee, 1)
    return {"ok": True, "err": None, "at": EQUITY.get("at"),
            "equity": m.get("equity_amount"), "avail": avail,
            "float_pl": m.get("future_open_position"),
            "settle_pl": m.get("future_settle_profitloss"),
            "cost": round(fee, 1), "day_pl": pl,
            "risk": m.get("risk_indicator"), "margin_call": m.get("margin_call"),
            "deposit": m.get("deposit_withdrawal"),
            "enough": enough, "cushion": equity_cushion(m.get("equity_amount"), lot1),
            "usage": usage_view(),
            "month": _equity_month(rows, m, now),
            "hist_n": len(rows)}


USAGE_DIR = HERE / "usage"             # ⛔ gitignore
USAGE_EVERY = 600.0
USAGE = {"at_ts": 0.0, "bytes": None, "limit": None, "conn": None, "at": None, "err": None}


def _usage_poll(now):
    """
    ⭐ 2026-09-26（Benson：要開始自己收集資料，**先確認券商流量上限**）。
    `api.usage()` ⇒ 今天用了多少、上限多少。⚠️ **唯讀**、10 分鐘一次、在 poll_equity 那條執行緒裡
    （已經避開送單那幾刻）。每次落地一列到 `usage/YYYY-MM.jsonl`，量一兩天就知道「現在的訂閱一天吃多少」，
    才決定能不能多錄五檔／現貨／其他期貨。
    """
    if time.time() - USAGE["at_ts"] < USAGE_EVERY:
        return
    USAGE["at_ts"] = time.time()
    api = SESSION_REF.get("api")
    if api is None:
        return
    try:
        u = api.usage(timeout=5000)
        b = float(getattr(u, "bytes", 0) or 0)
        lim = float(getattr(u, "limit_bytes", 0) or 0)
        conn = getattr(u, "connections", None)
        USAGE.update(bytes=b, limit=lim, conn=conn, at=now.strftime("%H:%M"), err=None)
        USAGE_DIR.mkdir(parents=True, exist_ok=True)
        with (USAGE_DIR / (now.strftime("%Y-%m") + ".jsonl")).open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": now.isoformat(timespec="seconds"), "bytes": b, "limit": lim,
                                "conn": conn}, ensure_ascii=False) + "\n")
    except Exception as e:
        USAGE["err"] = "問不到流量：%s" % str(e)[:100]


def usage_view():
    if USAGE.get("bytes") is None:
        return {"ok": False, "msg": USAGE.get("err") or "還沒問到券商流量（10 分鐘問一次）"}
    b, lim = USAGE["bytes"], USAGE["limit"] or 0
    return {"ok": True, "mb": round(b / 1e6, 1), "limit_mb": round(lim / 1e6), "at": USAGE["at"],
            "pct": round(b / lim * 100, 1) if lim else None,
            "msg": "券商流量：今天 %.0f MB／上限 %s MB（%s 更新）" % (
                b / 1e6, format(int(round(lim / 1e6)), ",") if lim else "?", USAGE["at"])}


def equity_cushion(eq, lot1, px=None, day_on=None, night_on=None):
    """
    ⭐ 2026-09-24（Benson：不夠就補錢 ⇒ 前提是**不夠的時候他要知道**）。
    權益離「一口保證金」還剩多少，夠不夠再吃一次停損。⇒ dict（`warn` True 就要亮）。
      停損距離：開著的那幾條裡最大的那個（夜盤 2%、日盤 0.5%，× 現價）。
      ⛔ 學不到一口保證金／沒有現價／兩條都關著 ⇒ 照實說判不出來，⛔ 不猜。
    建議本金 ＝ 保證金 ＋ 風控 B 下的最大回落 × 2 × 每點金額（`risk_cap.DD_PER_LOT`）。
    """
    if not isinstance(eq, (int, float)) or not lot1:
        return {"warn": None, "msg": ""}
    if px is None:
        px = getattr(CURRENT_STATE.get("today"), "price", None)
    try:
        if day_on is None:
            day_on = bool(auto_fire.arm().get("on"))
        if night_on is None:
            night_on = bool(night_fire.arm().get("on"))
    except Exception:
        day_on, night_on = True, True
    rec = float(lot1) + 2 * risk_cap.DD_PER_LOT * risk_cap.PT_NTD
    cush = float(eq) - float(lot1)
    out = {"cushion": round(cush), "pts": int(cush // risk_cap.PT_NTD), "rec": int(round(rec, -3)),
           "topup": max(0, int(round(rec - float(eq), -3)))}
    if not isinstance(px, (int, float)) or px <= 0 or not (day_on or night_on):
        out.update(warn=None, msg="權益比一口保證金多 %s 元（約 %d 點）" % (
            format(int(round(cush)), ","), out["pts"]))
        return out
    stop = round(px * (0.02 if night_on else 0.005))
    out["stop_pts"] = stop
    out["warn"] = out["pts"] < stop
    if out["warn"]:
        out["msg"] = ("⚠️ 權益只比一口保證金多 %s 元（約 %d 點），比一次停損（約 %d 點）還少 —— "
                      "再停損一次，下一筆自動單會因為保證金不夠被券商退掉。建議本金每口約 %s 元（還差約 %s 元）"
                      % (format(int(round(cush)), ","), out["pts"], stop,
                         format(out["rec"], ","), format(out["topup"], ",")))
    else:
        out["msg"] = ("權益比一口保證金多 %s 元（約 %d 點），撐得住一次停損（約 %d 點）"
                      % (format(int(round(cush)), ","), out["pts"], stop))
    return out


def poll_equity():
    """
    背景問「帳戶還有多少錢」。⚠️ **唯讀**（`broker.account_margin()` ⇒ `api.margin()`）。
    ⛔ 送單那幾刻前後 10 秒不問；⛔ 沒連上永豐就不問（⛔ 不重試到把流量吃完）。
    ⚠️ 節奏扣掉工作時間（跟 `poll_index` 同一個作法）。
    """
    while True:
        t0 = time.time()
        now = datetime.now()
        try:
            if SESSION_REF.get("api") is not None and not _eq_quiet(now):
                m, err = broker.account_margin()
                if m:
                    EQUITY.update({"m": m, "err": None,
                                   "at": now.strftime("%H:%M:%S")})
                    _equity_lot1(m)
                    _equity_write_day(m, now)
                # ⭐ 2026-09-26 券商每日流量（加錄資料之前先量基準）。⛔ 唯讀；自己 10 分鐘一次；壞了不影響帳戶查詢
                try:
                    _usage_poll(now)
                except Exception:
                    pass
                else:
                    # ⛔ 問不到就把數字清掉：畫面寧可寫「問不到」，
                    #    ⛔ 也不可以繼續顯示一個看起來是現在、其實是十分鐘前的金額。
                    EQUITY.update({"m": None, "err": err or "問不到帳戶餘額"})
                # ⛔⛔ **先算完再進鎖**：`equity_view()` 會讀 equity/（磁碟），
                #    而 `state_lock` 是 4Hz 主迴圈（＝他的停損）每一圈都要拿的鎖。
                #    ⛔ 不可以把任何 I/O 留在鎖裡面。
                view = equity_view(now)
                with state_lock:
                    STATE["equity"] = view
        except Exception as e:                        # noqa: BLE001 ⛔ 這條執行緒不准死
            try:
                EQUITY.update({"m": None, "err": "帳戶查詢出錯：%s" % str(e)[:100]})
                view = equity_view(now)               # ⛔ 同上：讀檔留在鎖外面
                with state_lock:
                    STATE["equity"] = view
            except Exception:
                pass
        t = now.time()
        # 盤中（08:45 開盤 ~ 14:30）問密一點；其餘時間 10 分鐘一次就夠
        every = EQUITY_EVERY if (SESSION_OPEN <= t <= EQUITY_BUSY_END) else EQUITY_EVERY_OFF
        time.sleep(max(5.0, every - (time.time() - t0)))


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
            # ⛔ 用**這一口自己的**點數（跟 check_real_position 同一支 pos_sl_points），
            #    不然自動下單那一口畫面寫停損 −130、實際卻是 −230 ⇒ 畫面那句是假的。
            # ⭐ 「開箱」那一口**沒有停利**（pos_tp_points 回 None）⇒ tp 一律 None，
            #    ⛔ 不可以算成 entry ± 130（那是一條不存在的線）。
            tpp = pos_tp_points(pos)
            snap["tp"] = None if tpp is None else pos["entry"] + d * tpp
            snap["sl"] = pos["entry"] - d * pos_sl_points(pos)
            snap["tp_pts"], snap["sl_pts"] = tpp, pos_sl_points(pos)
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


def _lab_has_position():
    """只讀 broker 記憶體裡的部位狀態，⛔ 不呼叫任何會送單／對帳的函式"""
    return broker._state.get("position") is not None


def start_strategy_lab_fetch():
    """
    【策略實驗室】收盤後補抓當天日盤逐筆的背景執行緒（main() 呼叫一次）。
    ⛔ 用面板**現有的**連線（SESSION_REF["api"]），⛔ 不准另外 login（會把面板自己踢下線）。
    ⛔ 有部位就不抓（問不到＝當成有部位，strategy_lab 那邊處理）。
    ⛔⛔ 這支**永遠不丟例外**（2026-09-14 lab-qa 退件 R1）：模組沒載入、起執行緒失敗，一律只印警告，
       呼叫端（main）照樣往下走 —— 研究功能出錯絕不可以擋住主迴圈與停損。
    ⚠️ 只接在 main()：--replay 與所有治具都走不到這裡 ⇒ 測試不會去連永豐。
    回傳是否真的起來了。
    """
    try:
        if strategy_lab is None:
            print("⚠️ 【策略實驗室】沒有載入，不補抓每日成交")
            return False
        threading.Thread(target=strategy_lab.fetch_loop,
                         args=(lambda: SESSION_REF.get("api"), _lab_has_position),
                         daemon=True, name="strategy-lab-fetch").start()
        return True
    except Exception as e:          # noqa: BLE001  ⛔ 刻意接住所有例外
        try:
            print(f"⚠️ 【策略實驗室】每日補抓起不來（其他功能不受影響）：{str(e)[:160]}")
        except Exception:
            pass
        return False


def _sim_has_position():
    """
    【模擬】背景補資料之前問「現在有沒有部位」。⛔⛔ **拿不到確定答案就回 True**（2026-09-15 lab-qa 退件 R1）。
    ・記憶體有部位 ⇒ True（不用再問）。
    ・記憶體是 None **不算答案**：看門狗重啟後 reconcile 回 unknown 時記憶體就是 None，但券商那邊部位還在。
      ⇒ 再跟券商問一次 `broker.broker_position()`（唯讀的 list_positions，⛔ 不對帳、不改 _state 部位、不送單）：
      只有它明確回 None（券商說沒有）才回 False；"unknown"／有部位／任何例外 ⇒ True。
    ⚠️ sim_lanes.fetch_gate 只在 13:50~隔天 08:30、真的要抓、沒在「隔 10 分鐘」時才叫這支 ⇒ 不會每分鐘去敲券商。
    ⚠️ 跟 `_lab_has_position`（strategy_lab 用、只讀記憶體）刻意分開，不動那一條。
    """
    try:
        if broker._state.get("position") is not None:
            return True
        return broker.broker_position() is not None
    except Exception:           # noqa: BLE001  ⛔ 問不到 ⇒ 當成有部位
        return True


def start_sim_lanes():
    """
    【模擬】分頁那張卡的背景執行緒（main() 呼叫一次，排在 start_strategy_lab_fetch 後面）。
    ⛔ 模擬：sim_lanes 自己不 import broker／auto_fire —— 規則那幾支函式在**這裡**注入
       （跟真單同一份正本：auto_fire.fast_verdict／move_pct／tpsl_points／reversal_dir／hist_read、
        FAST_PCTL、REV_SEC、FAST_RULE），⛔ 不准在 sim_lanes 裡另寫一份。
    ⛔ 有部位就不抓資料：注入 `_sim_has_position`（沒有確定答案就回 True，見那支；2026-09-15 lab-qa 退件 R1）。
    ⛔ 規則函式一律用**關鍵字**注入（lab-qa R4：位置參數對調 move_pct／tpsl_points 不會報錯、只會算錯）。
    ⛔⛔ 這支**永遠不丟例外**：模組沒載入、起執行緒失敗，一律只印警告，main 照樣往下走。
    ⚠️ 只接在 main()：--replay 與所有治具都走不到這裡 ⇒ 測試不會去連永豐。回傳是否真的起來了。
    """
    try:
        if sim_lanes is None:
            print("⚠️ 【模擬】沒有載入，不算模擬")
            return False
        ok = sim_lanes.configure(verdict_fn=auto_fire.fast_verdict, move_fn=auto_fire.move_pct,
                                 tpsl_fn=auto_fire.tpsl_points, hist_read_fn=auto_fire.hist_read,
                                 reversal_fn=auto_fire.reversal_dir, rev_sec=REV_SEC,
                                 pctl=FAST_PCTL, rule=auto_fire.FAST_RULE)
        if not ok:
            print("⚠️ 【模擬】規則函式接不上（逐筆那幾條會顯示「沒有接上」）")
        threading.Thread(target=sim_lanes.loop,
                         args=(lambda: SESSION_REF.get("api"), _sim_has_position),
                         daemon=True, name="sim-lanes").start()
        return True
    except Exception as e:          # noqa: BLE001  ⛔ 刻意接住所有例外
        try:
            print(f"⚠️ 【模擬】背景計算起不來（其他功能不受影響）：{str(e)[:160]}")
        except Exception:
            pass
        return False


def _health_real():
    """
    【健檢】那一頁「真單」那一半（⛔ 唯讀）。⇒ {lane_key: {"n", "avg", "msg"}}。

    ⛔⛔ **不另寫一套對帳法**：日盤走既有的 `fire_real_pairs()`（唯讀比對 `real_trades/`，
       跟【自動下單】那一頁的出場價是**同一把尺**）；夜盤走 `night_fire` 自己的帳本。
    ⚠️ **夜盤帳本沒有出場價**（出場在券商端成交，`night_fire` 那一側看不到）⇒
       夜盤只給「送出了幾口」，⛔ 點數留白、⛔ 不猜。
    ⚠️ 「夜盤跟勢」(trend) 是前瞻考試、⛔ 不下單 ⇒ 沒有這一條（呼叫端會照實寫出來）。
    ⛔ 這支跑在 HTTP 執行緒上 ⇒ 只讀那 60 天（`FIRE_REAL_DAYS`），⛔ 不掃整個帳本。
    """
    out = {}
    try:
        st = auto_fire.state()
        days = st.get("days") or []
        real = fire_real_pairs(days, today=st.get("today"), now=st.get("now"),
                               eod_at=st.get("eod_at"))
        pts = [v["points"] for v in real.values()
               if isinstance(v, dict) and v.get("state") == "ok"
               and isinstance(v.get("points"), (int, float))
               and not isinstance(v.get("points"), bool)]
        avg = round(sum(pts) / len(pts), 1) if pts else None
        out["union"] = {"n": len(pts), "avg": avg,
                        "msg": ("最近 %d 天有 %d 筆算得出點數，每筆平均 %s 點"
                                % (FIRE_REAL_DAYS, len(pts),
                                   ("%+.1f" % avg) if avg is not None else "—")
                                if pts else
                                "最近 %d 天還沒有算得出點數的真單" % FIRE_REAL_DAYS)}
    except Exception as e:
        out["union"] = {"n": None, "avg": None, "msg": "讀不出來：" + str(e)[:80]}
    # ⭐ 2026-09-23 夜盤有兩條了（T 台積電快攻／R 夜盤跟勢）⇒ 依帳本那一列的 `method` 分開數；
    #    ⚠️ 2026-09-23 以前的列沒有 `method`，那時只有 T ⇒ 算 T。⛔ 不可以兩條混成一張卡。
    try:
        n = {"T": 0, "R": 0}
        for f in sorted(night_fire.NF_DIR.glob("*.jsonl")) if night_fire.NF_DIR.exists() else []:
            if not night_fire._MONTH_RE.match(f.name):
                continue
            for ln in f.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    o = json.loads(ln)
                except Exception:
                    continue
                if o.get("rec") == "result" and o.get("ok"):
                    k = o.get("method") or "T"
                    n[k] = n.get(k, 0) + 1
        for k, lane in (("T", "tsm"), ("R", "trend")):
            out[lane] = {"n": n.get(k, 0), "avg": None,
                         "msg": ("送出了 %d 口；⛔ 夜盤帳本沒有出場價（出場在券商端成交），"
                                 "點數留白" % n[k]) if n.get(k) else "還沒有送出過真單"}
    except Exception as e:
        out["tsm"] = {"n": None, "avg": None, "msg": "讀不出來：" + str(e)[:80]}
    return out


def _nf_quote():
    """夜盤自動下單要的台指報價 ⇒ (價, 幾秒前)。⛔ 只讀屬性（同停損那一個價），拿不到回 (None, None)。"""
    st = CURRENT_STATE.get("today")
    if st is None or st.price is None or st.last_recv is None:
        return None, None
    return float(st.price), time.time() - st.last_recv


def _nf_minute_close(m):
    """
    夜盤跟勢要的「某一分鐘（開始時間標記）最後一筆成交價」⇒ 價或 None。
    ⛔ 只讀屬性（`Today.minute_close` 是停損迴圈那一份的同一個 dict，⛔ 不寫、不拷貝整份）。
    """
    st = CURRENT_STATE.get("today")
    if st is None:
        return None
    v = st.minute_close.get(int(m))
    return float(v) if isinstance(v, (int, float)) else None


def _recover_chain(pos):
    """broker.RECOVER_HOOK：夜盤那一口先問 night_fire，不認得才交給 auto_fire。⛔ 只讀記憶體、不丟例外。"""
    try:
        got = night_fire.recover_meta(pos)
    except Exception:
        got = None
    return got if got else auto_fire.recover_meta(pos)


def main():
    # ⛔ 09:03:30 的掛勾只有這裡會接（接上去就會真的送單，見 auto_fire.py）。
    #    13:43:30 的收盤平倉掛勾同理（接上去就會真的送出平倉單）。
    #    沒有 global 的話下面那兩行只會建區域變數 ⇒ 接線靜靜地沒生效。
    global AUTO_SIG_HOOK, AUTO_EOD_HOOK
    # ⭐ 09:15 回馬槍的掛勾同理（接上去就會在反轉的日子真的送單）。⚠️ 刻意另起一行 global
    #    （上面那一行是既有突變測試的目標字串，⛔ 不併進去）。
    global AUTO_REV_HOOK
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
    # ⚠️ 2026-09-15 不再傳 tp_points：自動下單的停利停損是 ±0.5%（auto_fire.FAST_RULE），
    #    ⛔ 不是這裡的 TP_POINTS（手動真單／練習／模擬的 ±130）。
    auto_fire.configure(signal_at=SIGNAL_AT, signal_sec=SIGNAL_SEC,
                        late_ms=AUTO_LATE_MS, gap_s=AUTO_GAP_S,
                        sig_fn=auto_sig, dirs_fn=auto_dirs, eod_at=EOD_CLOSE_AT,
                        pctl=FAST_PCTL, rev_at=REV_AT, rev_sec=REV_SEC)
    # ⛔⛔ 重啟撿回部位時補「自動下單那一口自己的停損點數」（沒補就掉回 SL_POINTS ⇒ 提早被洗掉）。
    #    **只有 main() 會接**（治具與 --replay 不接 ⇒ 撿回來的部位照舊用 SL_POINTS）。
    #    掛上去的 recover_meta 只讀記憶體（它在主迴圈的 reconcile 裡被叫）。
    # ⭐ 2026-09-22：夜盤那一口先問 night_fire（它認得就用它自己的點數），不認得才交給日盤那一支。
    broker.RECOVER_HOOK = _recover_chain
    auto_fire.start()
    AUTO_SIG_HOOK = auto_fire.on_signal
    # ⛔⛔ 收盤自動平倉：**只平自動下單自己開的那一口**（auto_fire._looks_ours）。
    #    他自己手動進場的部位絕對不碰。
    AUTO_EOD_HOOK = auto_fire.on_eod
    # ⭐⭐ 快攻回馬槍：09:03:30 不夠快的日子，REV_AT 那一刻反轉了才送（auto_fire._rev 讀帳本判斷）。
    AUTO_REV_HOOK = auto_fire.on_reversal
    # ⭐⭐ 2026-09-22【夜盤自動下單】台積電快攻。⛔ 自己的執行緒、自己的開關（NIGHT_ORDERS_ON）；
    #    主迴圈一行都不動。價格讀 Today.price／last_recv（就是停損看的那一個）。
    #    ⛔ 包 try：它起不來只印警告，日盤與停損照跑。
    try:
        night_fire.configure(quote_fn=_nf_quote, session_fn=market_session,
                             minute_close_fn=_nf_minute_close)
        night_fire.start()
        _na = night_fire.arm()
        print("【夜盤自動下單】" + (_na["msg"] + ("（真單）" if broker.is_live() else "（真單開關關著 ⇒ 只會演練）")
                                    if _na["on"] else "關閉中 —— " + _na["msg"]))
    except Exception as e:
        print("⚠️ 【夜盤自動下單】起不來（日盤不受影響）：%s" % str(e)[:160])
    _arm = auto_fire.arm()
    print("【自動下單】" + ("已開啟：" + _arm["msg"] +
                           ("（真單）" if broker.is_live() else "（真單開關關著 ⇒ 只會演練）")
                           if _arm["on"] else "關閉中 —— " + _arm["msg"]))
    _ep = eod_plan(date.today())
    # ⛔⛔ 「今天是結算日」那句話**只有在確定的時候才准出現**（PM 2026-09-17 裁示）——
    #    判不出來時走的是「不確定」那一句，⛔ 不是安靜地印「結算日提前到 …」。
    print(f"【自動下單】收盤平倉 {_ep['at']}"
          + ("（⭐ 今天是結算日，日盤 13:30 收盤）" if _ep["expiry"]
             else ("（⚠️ 今天是不是結算日**判不出來**）" if not _ep["sure"]
                   else f"（結算日提前到 {EOD_CLOSE_AT_EXPIRY}）"))
          + "（⛔ 只平自動下單開的那一口）"
          + (f"　⚠️ {_ep['err']}" if _ep["err"] else ""))

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
    # ⭐ 2026-09-21【帳戶總覽】：每分鐘問一次「帳戶還有多少錢」。⚠️ 唯讀、⛔ 一張單都不送；
    #    ⛔ 只接在 main()（治具與 --replay 走不到這裡 ⇒ 測試不會去連永豐）。
    # ⚠️ 先放一份「還在問」進去：不然第一分鐘那張卡整個不見，看起來像功能壞了。
    _eq_boot = equity_view()                          # ⛔ 讀檔留在鎖外面（見 poll_equity）
    with state_lock:
        STATE["equity"] = _eq_boot
    # ⭐ 開機先把上次學到的「一口要壓多少保證金」讀回來（⛔ 不然重開一次就忘記、
    #    畫面要等到下一次有部位才講得出夠不夠）。
    _equity_lot1_restore()
    threading.Thread(target=poll_equity, daemon=True, name="equity").start()

    # 【策略實驗室】收盤後補抓當天日盤逐筆。⛔ 起不來只印警告（見 start_strategy_lab_fetch）。
    start_strategy_lab_fetch()
    # 【策略實驗室】最上面那張「模擬（不會下單）」的背景計算。⛔ 起不來只印警告（見 start_sim_lanes）。
    start_sim_lanes()
    # 【健檢】真單那一半的接線（⛔ 唯讀）。⛔ 只接一次、⛔ 這裡不做任何 I/O；
    #   ⛔ 失敗只印警告（它是「看的東西」，絕不可以擋住送單與停損那條路）。
    try:
        if health is not None:
            health.configure(real_fn=_health_real)
    except Exception as e:          # noqa: BLE001  ⛔ 刻意接住所有例外
        print("⚠️ 【健檢】真單那一半接不上（其他功能不受影響）：%s" % str(e)[:160])
    # 【手機監控】（2026-09-24）每 2 分鐘把加密快照推到 GitHub 的 monitor 分支。⛔ 唯讀、自己的執行緒、
    #   只讀面板自己的端點；⛔ 沒有金鑰檔就不啟動；⛔ 起不來只印警告（絕不擋住送單與停損那條路）。
    try:
        import monitor_push
        monitor_push.start(PORT)
    except Exception as e:          # noqa: BLE001  ⛔ 刻意接住所有例外
        print("⚠️ 【手機監控】起不來（其他功能不受影響）：%s" % str(e)[:160])
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
