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
import urllib.request

import numpy as np
import pandas as pd

import broker            # 真實下單。預設 dry run，見那個檔開頭的說明
import auto_fire         # 自動下單。預設**關著**（沒有 AUTO_ORDERS_ON 就完全不送）
import night_fire        # 夜盤自動下單（台積電快攻）。預設**關著**（沒有 NIGHT_ORDERS_ON 就完全不送；跟日盤開關分開）
import risk_cap          # 風控規則 B（2026-09-24）：本月自動單虧到上限就不送。⛔ 唯讀模組
import lots              # 口數的唯一正本（2026-10-14 多口）。狀態檔不存在 ⇒ 1＋1（跟以前一樣）
import tick_writer       # 逐筆報價落地。⛔ 它的存在前提是「絕不在 on_tick 裡碰磁碟」
import depth_writer      # 研究用全天五檔落地（同一條鐵律：on_bidask 只 append）
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
# 【帳戶】的目標卡（2026-09-30）。⛔ 純計算、不讀不寫檔；包 try 同上理由。
try:
    import goal
except Exception as _gl_err:           # noqa: BLE001  ⛔ 刻意接住所有例外
    goal = None
    print("⚠️ 【目標】載入失敗（其他功能不受影響）：%s" % _gl_err, flush=True)
# ⭐ 2026-10-21【帳戶】金流明細。⛔ 純計算、只讀檔；包 try 同上理由。
try:
    import cashflow
except Exception as _cf_err:           # noqa: BLE001  ⛔ 刻意接住所有例外
    cashflow = None
    print("⚠️ 【金流明細】載入失敗（其他功能不受影響）：%s" % _cf_err, flush=True)
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
broker.MANUAL_SL_POINTS = SL_POINTS   # ⭐ 2026-10-14：多口交接成手動部位時，那句話要講的停損點數（只拿來寫字）
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

# ⭐ 2026-11-12 整理第 2 批：時刻正本在 market_clock.py（⛔ 這裡不再寫數字；test_market_clock 守一致）
import market_clock as MC  # noqa: E402
SESSION_OPEN = MC.as_time(MC.DAY_OPEN)
WATCH_END = MC.as_time(MC.WATCH_END)
DAY_END = MC.as_time(MC.DAY_CLOSE)
NIGHT_OPEN = MC.as_time(MC.NIGHT_OPEN)      # 夜盤 15:00 ~ 隔天 05:00
NIGHT_CLOSE = MC.as_time(MC.NIGHT_CLOSE)
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

# ⭐ 2026-09-26 研究用全天五檔（錄製、訂閱、看門狗、畫面那一行）⇒ 2026-11-12 整理第 5 批原樣搬到 panel_depth.py。
#    ⛔ on_bidask 只 append 那條鐵律照舊（接線在下面 main() 裡）；⛔⛔ 大台／選擇權的報價不准進 Today（見 _is_main_code）。
import panel_depth as PD  # noqa: E402
from panel_depth import (DEPTH, DEPTH_MAX_PCT, DEPTH_QUIET_ZONES, DEPTH_STALE_S, DEPTH_SUBS,  # noqa: E402,F401
                         DEPTH_WD, depth_check, depth_pause, depth_subscribe, depth_view, night_moments)


def _is_main_code(code):
    """這筆報價是不是真單那個商品（微台）的？代碼讀不到 ⇒ 當成是（跟加這段之前一模一樣）。"""
    return not code or str(code).startswith(PRODUCT)


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
PD.PRODUCT = PRODUCT             # 五檔那支（panel_depth）用同一個

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
LOOP_ERR = {"n": 0, "run": 0, "last": None, "at": None}
LOOP_ERR_GIVEUP = 150   # 連續出錯超過這麼多圈（前 20 圈 0.25 秒、之後 2 秒 ⇒ 約 4.5 分鐘）⇒ 照舊退出交給看門狗   # ⭐ 2026-11-12：主迴圈接住的例外（不讓行程退出）
SESSION_REF = {"api": None}          # K 線圖要用它去跟永豐要 K 棒

# 加權指數（現貨）：Benson 下單時會看，所以面板一起顯示，並算出基差。
# 【只顯示，不做分析】永豐的指數歷史 1 分 K 只有 54 個破碎的交易日，
# 測不出東西 —— 這裡純粹是多一個客觀數字，不是訊號。
# 現貨 09:00 才開盤、13:30 收，比期貨晚開早收，所以會有「尚未開盤」的空窗。
INDEX = {"price": None, "chg": None, "pct": None, "at": None, "contract": None, "ms": None}
INDEX_EVERY = 2.0        # 秒。指數輪詢的目標週期（實際週期 = max(這個值, 單次耗時)）
CASH_OPEN = MC.as_time(MC.CASH_OPEN)
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
        # ⭐ 2026-11-12 整理第 4 批（Benson 選「只修畫面」）：同一分鐘索引最後一筆成交的完整時間（帶日期）。
        #    ⛔ 只給 K 線圖（overlay_live）用；自動下單讀的 minute_bar／minute_close 一個字都沒動。
        self.minute_ts = {}

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
        self.minute_ts[mi] = when
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


# ⭐ 練習紀錄的手機同步（心得抓回來、紀錄推上雲端）⇒ 2026-11-12 整理第 5 批原樣搬到 panel_phone.py。
import panel_phone as PH  # noqa: E402
from panel_phone import PHONE_EVERY, PHONE_URL, _note_wins, poll_phone, pull_from_phone, sync_to_cloud  # noqa: E402,F401



# ⭐ K 線圖與逐筆圖的資料（day_bars、session_frame、overlay_live、day_index、closed_days、tick_day…）
#    ⇒ 2026-11-12 整理第 5 批原樣搬到 panel_kline.py。⛔ 只給畫面看；自動下單讀的 minute_bar／minute_close 不在那裡。
import panel_kline as PK  # noqa: E402
from panel_kline import (SESS_RETRY, SESS_RETRY_PARTIAL, TICK_CACHE, TICK_KEEP, TICK_LOCK,  # noqa: E402,F401
                         TICK_SEC0, TICK_SPAN, TODAY_RETRY_EMPTY, TODAY_TTL, _cached_raw, _csv_stamp,
                         _LOCAL_PX, _day_trades, _local_span, _raw_days, _tick_complete, _tick_load, _tick_sniff,
                         _today_raw, closed_days, day_bars, day_index, local_bars, overlay_live,
                         session_frame, tick_day, tick_days, tick_trades, to_timeframe, traded_days)
PK.PRODUCT = PRODUCT
PK.CURRENT_STATE = CURRENT_STATE
PK.SESSION_REF = SESSION_REF
PK.today_trades = lambda: TODAY_TRADES
PK.trade_dir = lambda: TRADE_DIR
PK.tick_dir = lambda: TICK_DIR
PK.sim_lanes_mod = lambda: sim_lanes



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
SIGNAL_AT = MC.hms(MC.SIGNAL)
SIGNAL_SEC = MC.sec(MC.SIGNAL)

# ⭐⭐ 【自動下單】「快攻回馬槍」的回馬槍那一刻（2026-09-15 晚上 Benson 決定隔天起用）。
#    **正本只有這裡**，auto_fire 靠 configure(rev_at=REV_AT, rev_sec=REV_SEC) 接過去。
#    09:03:30 不夠快的日子，主迴圈跨過這一刻時快照（同一支 _auto_snap），順勢方向反轉了才做 1 口。
#    研究：tick-research/scripts/defs_research.py（09:10／09:15／09:20 × 7 種定義，「09:15 方向相反就算」）。
#    ⛔ 只管自動下單；【自動下單（模擬）】那一頁不看這個時刻。
#    ⛔ 要改就兩個一起改（字串與秒數），test_auto_fire.py ⑰ 斷言兩個對得上。
REV_AT = MC.hms(MC.REV)
REV_SEC = MC.sec(MC.REV)

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
EOD_CLOSE_AT = MC.hms(MC.DAY_EOD)
EOD_CLOSE_SEC = MC.sec(MC.DAY_EOD)

# ⭐⭐⭐ 2026-09-16：**結算日日盤 13:30 就收盤**（不是 13:45）。
#   舊版 `EOD_CLOSE_AT` 只有一個值、沒有結算日分支 ⇒ 結算日那天 13:43:30 才要平倉，
#   而市場 13:30 就關了 ⇒ **單子送不出去、部位抱過夜，而且 13:45 起停損也停了**。
#   （約每 20 個交易日就來一次 —— 一個月一天。）
#   ⚠️ 兩個數字的關係跟平常那一組一模一樣：**收盤前 90 秒**（13:45−90s＝13:43:30／
#      13:30−90s＝13:28:30），因為 `auto_fire.EOD_WINDOW_S` 那 75 秒的重試窗口是實測值，
#      換一個收盤時刻不會改變它要多久。
#   ⛔ 要改的話兩件事一起改：字串與秒數（`test_auto_fire.py` 在守）。
EOD_CLOSE_AT_EXPIRY = MC.hms(MC.DAY_EOD_EXPIRY)
EOD_CLOSE_SEC_EXPIRY = MC.sec(MC.DAY_EOD_EXPIRY)
# 結算日的日盤收盤（＝觸發區間的上界；平常是 DAY_END 13:45）
DAY_END_SEC_EXPIRY = 13 * 3600 + 30 * 60
# 今天是不是結算日 —— **一天只算一次**（`_auto_tick` 在 4Hz 主迴圈上，
# ⛔ 那裡不准讀檔；`strategy_lab.expiry_state_cal` 會讀 days.jsonl）。
# ⭐ `sure`＝那個答案有沒有把握（False ⇒ 用了平常那一組，但我們其實**不知道**）。
EOD_DAY = {"date": None, "expiry": False, "sure": True,
           "at": EOD_CLOSE_AT, "sec": EOD_CLOSE_SEC, "end": None, "err": None}


def contract_alive(c, now=None):
    """
    ⭐ 2026-10-21：這個期貨合約**現在**還有沒有在交易。⇒ True／False（判不出來 ⇒ True）。

    【踩到的坑】結算日（10-21）傍晚他重啟面板 ⇒ `pick_live_contract` 用「成交量最大」挑，
    挑到**今天 13:30 已經到期**的 202610（它白天量最大）⇒ 夜盤一筆報價都沒有、
    畫面整晚「盤中卻收不到報價」，⛔ 而且真單（broker.configure）也綁在那個已到期的合約上。
    ⇒ 到期日已過、或今天到期且已過 13:30 的，一律不要。
    ⚠️ 永豐的 delivery_date 可能是字串（"2026/10/21"）或 date ⇒ 只取數字比，⛔ 不假設型別。
    """
    now = now or datetime.now()
    ds = re.sub(r"\D", "", str(getattr(c, "delivery_date", "") or ""))[:8]
    if len(ds) < 8:
        return True
    today = now.strftime("%Y%m%d")
    if ds < today:
        return False
    if ds == today and now.hour * 3600 + now.minute * 60 + now.second >= DAY_END_SEC_EXPIRY:
        return False
    return True


def pick_front(cands, now=None):
    """
    ⭐ 2026-10-21：從（已依交割月排好的）候選裡挑**最近一個還沒到期**的月份。⇒ 合約或 None。

    ⛔ 不再用「成交量最大」挑：結算日前後新舊月份成交量會互相超車，
       **持有部位時斷線重連**若剛好改挑到下個月 ⇒ broker 只認新合約的部位 ⇒
       看不到原本那口、停損／平倉送到新月份（多開一口反向、舊的沒平）。
       ⇒ 同一天重連幾次都要挑到同一個合約：用到期日決定，不看量。
    ⚠️ 任何一個候選讀不出到期日 ⇒ 回 None（呼叫端退回舊的成交量挑法，⛔ 不猜）。
    """
    if not cands:
        return None
    for c in cands:
        if len(re.sub(r"\D", "", str(getattr(c, "delivery_date", "") or ""))) < 8:
            return None
    alive = [c for c in cands if contract_alive(c, now)]
    return alive[0] if alive else None


def quiet_seconds(last_recv, conn_at, sess, now_ts, now=None):
    """
    ⭐ 2026-10-21：斷線看門狗用的「多久沒收到報價」。⇒ 秒數或 None（None ＝ 不判斷）。

    【踩到的坑】舊版只看 last_recv：**重啟後一筆都還沒收到**（last_recv 是 None）就永遠不算斷線
    ⇒ 10-21 傍晚他重啟、訂到已到期的合約，畫面寫「程式每分鐘會自動重連」，實際上一次都沒重連。
    ⇒ 沒收過 ⇒ 從「連上那一刻」與「這一盤開盤那一刻」較晚的那個起算
       （⛔ 不可以只用連上時刻：08:30 連好、08:45 才開盤，那 15 分鐘本來就沒報價）。
    """
    if last_recv:
        return now_ts - last_recv
    if not conn_at or sess not in ("day", "night"):
        return None
    now = now or datetime.fromtimestamp(now_ts)
    if sess == "day":
        start = now.replace(hour=SESSION_OPEN.hour, minute=SESSION_OPEN.minute, second=0, microsecond=0)
    elif now.time() >= NIGHT_OPEN:
        start = now.replace(hour=NIGHT_OPEN.hour, minute=NIGHT_OPEN.minute, second=0, microsecond=0)
    else:
        start = (now - timedelta(days=1)).replace(hour=NIGHT_OPEN.hour, minute=NIGHT_OPEN.minute,
                                                   second=0, microsecond=0)
    base = max(conn_at, start.timestamp())
    return max(0.0, now_ts - base)


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
    #    ⭐ 2026-11-12：盤中重啟時 seed_from_bars() 補的 minute_close 已改成「K 棒結束時間 − 1 分鐘」⇒ [539] 是 08:59 那一分鐘。
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
    # ⭐ 2026-11-12 稽核：09:03:30 那一刻報價剛好斷了（> AUTO_GAP_S 沒更新）⇒ 先不拍快照，下一圈（0.25 秒後）再看，
    #    最多等到 AUTO_LATE_MS − 500 毫秒；恢復了就照常拍（舊版那一刻不新鮮就整天 quote_stale 不送）。
    #    ⛔ 等到上限還是不新鮮 ⇒ 照舊拍下去（auto_fire 記 quote_stale、不送）。只有整數比較與屬性讀取（⛔ 不加 I/O）。
    _sig_wait = False
    if not AUTO["done"] and secs >= SIGNAL_SEC:
        _lag0 = (secs - SIGNAL_SEC) * 1000 + now.microsecond // 1000
        _sig_wait = (_lag0 <= AUTO_LATE_MS - 500 and st is not None
                     and (st.last_recv is None or time.time() - st.last_recv > AUTO_GAP_S))
    if not AUTO["done"] and secs >= SIGNAL_SEC and not _sig_wait:
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
                    # ⭐ 2026-10-14 多口：那一趟幾口（points 是每口）⇒ 畫面「合計」＝ points × qty
                    "qty": _auto_num(o.get("qty")),
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



_SIM_LANE_CACHE = {}        # "YYYY-MM" -> ((mtime_ns, size) | None, {(lane, date): 列})


def fire_sim_lane(days):
    """
    ⭐ 2026-10-14（Benson：「資料看起來沒有同步」）：【自動下單】每一列的「模擬那邊」。
    舊的 `fire_sim_pairs()` 讀的是 autotest/ 那份四種做法（A／B）的模擬 ⇒ 09-17 改跑 U（多方聯軍）之後
    結構上永遠對不到 ⇒ 每一列都寫「模擬那邊 —」。U 的模擬其實一直在【模擬】那一頁（sim_lanes/ 的 union 線）。
    ⇒ 這裡**唯讀** sim_lanes/ 那幾個月的檔（⛔ 不寫、⛔ 不重算），一天給 {lane: 精簡列}；
       哪個做法對哪條線由 `auto_fire.RULE_ID` 決定（端點一併端出去，⛔ 前端不寫死）。
    ⛔ 每 5 秒被輪詢 ⇒ 每個月一份 (mtime, size) 快取，穩態零次讀檔。
    ⚠️ 模擬的點數**已扣成本**，真單那欄是**沒扣**的毛點數 ⇒ 兩邊差幾點是正常的。
    """
    if sim_lanes is None:
        return {}
    want = {str(r.get("date")) for r in (days or []) if isinstance(r, dict) and r.get("date")}
    lanes = set(auto_fire.RULE_ID.values())
    out = {}
    for mth in sorted({d[:7] for d in want}):
        f = sim_lanes.SIM_DIR / (mth + ".jsonl")
        try:
            st = f.stat()
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            key = None
        hit = _SIM_LANE_CACHE.get(mth)
        if hit is None or hit[0] != key:
            rows = {}
            if key is not None:
                for ln in f.read_text(encoding="utf-8", errors="replace").splitlines():
                    try:
                        o = json.loads(ln)
                    except Exception:
                        continue
                    if isinstance(o, dict) and o.get("lane") in lanes and (o["lane"], o.get("date")) not in rows:
                        rows[(o["lane"], o.get("date"))] = o
            hit = (key, rows)
            _SIM_LANE_CACHE[mth] = hit
        for (lane, d), o in hit[1].items():
            if d in want:
                out.setdefault(d, {})[lane] = {k: o.get(k) for k in ("decision", "entry", "exit", "points",
                                                                     "exit_reason")}
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
                      "points": _auto_num(t.get("points")), "why": t.get("why"),
                      "qty": _auto_num(t.get("qty"))}
        elif not hits:
            # 今天、而且還沒到收盤平倉那一刻 ⇒ 那口本來就還開著（⛔ 不是對不起來）。
            holding = (d == today and can_time and str(now) < str(eod_at))
            out[d] = {"state": "open" if holding else "none"}
        else:
            out[d] = {"state": "many", "n": len(hits)}
    return out


# 夜盤那一口出場可能落在 E 當晚（停損）、E+1（04:58 平）、週五晚 ⇒ 週六 ⇒ 往後找幾天跟風控**同一個數**
NIGHT_REAL_SPAN = risk_cap.NIGHT_EXIT_MAX_D + 1
_NF_RESULTS = {"key": None, "rows": []}


def night_results(months=120):
    """夜盤帳本裡「送出了」的那幾列（新到舊，最近 `months` 個月檔）。⛔ 唯讀；檔案沒變就不重讀。
    ⚠️ `night_fire.state()` 的 `recent` 只有最後 10 列（混著 skip／eod）⇒ 算成績要從這裡拿。"""
    d = night_fire.NF_DIR
    fs = [f for f in sorted(d.glob("*.jsonl")) if night_fire._MONTH_RE.match(f.name)][-months:] if d.exists() else []
    key = tuple((f.name, f.stat().st_mtime_ns, f.stat().st_size) for f in fs)
    if _NF_RESULTS["key"] == key:
        return _NF_RESULTS["rows"]
    rows = []
    for f in fs:
        for ln in f.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                o = json.loads(ln)
            except Exception:
                continue
            if isinstance(o, dict) and o.get("rec") == "result" and o.get("ok"):
                rows.append(o)
    rows = rows[::-1]
    _NF_RESULTS.update(key=key, rows=rows)
    return rows


def night_real_pairs(recent, now=None):
    """
    【夜盤自動下單】那一口**後來怎麼了**。⇒ {E: {...}}，E ＝ 夜盤帳本的「那一晚」。**唯讀。**

    ⭐ 2026-10-14 修（Benson：「資料看起來都沒有同步」）：舊註解說「夜盤帳本沒有出場價」——
       帳本本身確實沒有，但 `broker.close("night_eod")`／停損一樣會 `record_trade()` 寫進 `real_trades/`，
       只是**檔名是出場那天**（04:58 平 ⇒ 隔天；週五晚 ⇒ 週六）⇒ 所以要往後找 NIGHT_REAL_SPAN 天。
       （`risk_cap._match()` 早就這樣對夜盤了 ——【本月】卡的夜盤點數一直都有，只有這一頁的列表沒有。）
    ⛔ 認人方式跟日盤 `fire_real_pairs()` 同一組欄位（方向、進場時刻到分、進場價 ±EOD_PX_TOL），
       ⛔ 對不到／對到多筆一律留白（state 的意思也跟日盤同一套：ok／open／none／many／drill）。
    """
    out = {}
    tol = auto_fire.EOD_PX_TOL
    now = now or datetime.now()
    for r in (recent or []):
        if not isinstance(r, dict) or r.get("rec") != "result" or not r.get("ok"):
            continue
        E = str(r.get("E") or "")
        try:
            e0 = date.fromisoformat(E)
        except ValueError:
            continue
        if r.get("live") is False:
            out[E] = {"state": "drill"}
            continue
        ent, dv = _auto_num(r.get("entry")), r.get("dir")
        want_t = str(r.get("entry_time") or "")[:5]
        hits = []
        for k in range(NIGHT_REAL_SPAN):
            for t in _fire_real_day(str(e0 + timedelta(days=k))):
                if not dv or t.get("dir") != dv:
                    continue
                if not want_t or str(t.get("entry_time") or "")[:5] != want_t:
                    continue
                te = _auto_num(t.get("entry"))
                if te is None or ent is None or abs(te - ent) > tol:
                    continue
                hits.append(t)
        if len(hits) == 1:
            t = hits[0]
            out[E] = {"state": "ok", "exit": _auto_num(t.get("exit")),
                      "exit_time": t.get("exit_time"), "exit_date": t.get("date"),
                      "points": _auto_num(t.get("points")), "why": t.get("why"),
                      "qty": _auto_num(t.get("qty"))}   # ⚠️ _auto_mine_day 已把 reason 改名成 why
        elif not hits:
            # 還沒到那一晚的 04:58（＋1 分鐘讓券商回報）⇒ 那口本來就還開著
            close_at = datetime.combine(e0 + timedelta(days=1), datetime.min.time()).replace(hour=4, minute=59)
            out[E] = {"state": "open" if now < close_at else "none"}
        else:
            out[E] = {"state": "many", "n": len(hits)}
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
        # ⭐ 2026-10-14 多口：上限 ＝ 400 ×（本月用過最大的 a＋b）（lots.cap_lots；1＋1 ＝ 800 跟以前一樣）
        v = risk_cap.state(lots=lots.cap_lots())
    except Exception as e:
        v = {"err": "風控算不出來：%s" % str(e)[:120], "blocked": True,
             "msg": "風控算不出本月損益 —— 不猜，這段時間不送"}
    _RISK.update(at=now, v=v)
    return v


# ---------------------------------------------------------------- 口數（2026-10-14 多口第二階段）
# ⭐ 正本 lots.py。這裡只做三件事：① 給畫面的那一份（lots.panel，句子後端組好）② 升級／撤回兩個端點
#    ③ /api/state 帶一個很小的 `lots`（分頁小金點用，⛔ 不是整份）。
# ⛔⛔ 升級＝放大真錢曝險 ⇒ 跟「打開自動下單」同一級：前端**兩段式**、後端 do_POST 入口的六道防護（fire_post_guard）、
#    路由精確比對；只收「這個月建議的那一級」（offer_id），⛔ 不自動改升別級。
# ⛔ apply 要重驗全部條件（退休標準約 2.5 秒）⇒ 跑在 HTTP 執行緒上、⛔ 不拿 state_lock、⛔ 不碰主迴圈。
# ⛔ LOTS_LEVEL.json 的寫入者**只有 lots.py**（apply_upgrade／revoke_pending／daily_check）—— 這裡一個位元組都不寫
#    （test_lots 的 AST 名單在守）。
def lots_panel_now(now=None):
    """⇒ lots.panel()（權益用券商現在的；一口保證金用面板學到的；今晚＝夜盤那一晚）。⛔ 永遠不丟例外。"""
    now = now or datetime.now()
    try:
        m = EQUITY.get("m") or {}
        eq = m.get("equity_amount") if isinstance(m.get("equity_amount"), (int, float)) else None
        lot1 = _lot1()[0]
        E = night_fire.evening_of(now) or now.date()
        P = lots.panel(now=now, eq=eq, lot1=lot1, E=E)
        # ⭐ QA r2 必修 2：多口部位不照平常規矩走（他自己加碼過／交接成他的手動部位／重啟後對不上／成交口數一度對不上）
        #    ⇒ 【自動下單】頂部金條＋分頁小金點＋手機「要注意的事」都要有一句白話（句子＝broker.pos_note，只讀記憶體）。
        note, kind = broker.pos_note()
        _na = night_fire.eod_alarm()
        if _na and isinstance(P, dict):
            P["alarm"] = list(P.get("alarm") or []) + [{"kind": "night_eod", "text": "夜盤：" + _na + "。"}]
            P["dot"] = True
            ph = dict(P.get("phone") or {})
            ph["warn"] = ("夜盤：" + _na + "。") + ((" ／ " + ph["warn"]) if ph.get("warn") else "")
            ph["att"] = True
            P["phone"] = ph
        if note and isinstance(P, dict):
            P["alarm"] = list(P.get("alarm") or []) + [{"kind": "pos_" + str(kind), "text": "部位：" + note + "。"}]
            P["dot"] = True
            ph = dict(P.get("phone") or {})
            ph["warn"] = ("部位：" + note + "。") + ((" ／ " + ph["warn"]) if ph.get("warn") else "")
            ph["att"] = True
            P["phone"] = ph
        return P
    except Exception as e:                     # noqa: BLE001
        return {"ok": False, "err": "讀不到口數狀態（%s）—— 讀不到的時候照 1＋1 送" % str(e)[:80], "dot": False,
                "alarm": [], "phone": {"label": "1＋1", "line": "讀不到口數狀態，照 1＋1 送", "att": False, "warn": None}}


def lots_state_small(P=None):
    """/api/state 帶的那一小份（分頁小金點、狀態列）。⛔ 讀檔在鎖外面做好，呼叫端只在鎖裡指派。"""
    P = P if P is not None else lots_panel_now()
    return {"dot": bool(P.get("dot")), "label": ((P.get("run") or {}).get("label")), "ok": P.get("ok", False)}


def _lots_rows_now():
    """apply 要的「最新權益」：equity/ 每天那幾列，再接上券商**現在**那一個（有的話）。"""
    rows = list(equity_hist_read())
    m = EQUITY.get("m") or {}
    if isinstance(m.get("equity_amount"), (int, float)):
        rows.append({"date": str(date.today()), "equity": float(m["equity_amount"]), "live": True})
    return rows


def lots_upgrade(body, who="panel"):
    """POST /api/lots/upgrade ⇒ (code, payload)。⛔ 呼叫端已經過 fire_post_guard（do_POST 入口）。"""
    oid = body.get("offer_id") if isinstance(body, dict) else None
    if not isinstance(oid, str) or not oid or len(oid) > 40:
        return 400, {"ok": False, "msg": "要確認哪一個建議看不懂"}
    try:
        ok, msg = lots.apply_upgrade(oid, rows=_lots_rows_now())
    except Exception as e:                     # noqa: BLE001
        return 500, {"ok": False, "msg": "升級出錯，沒有升：%s" % str(e)[:120]}
    _lots_refresh_small()
    print("[口數] %s 升級：%s（%s）" % (who, msg, "成功" if ok else "拒絕"), flush=True)
    return (200 if ok else 409), {"ok": ok, "msg": msg}


def lots_revoke(who="panel"):
    """POST /api/lots/revoke ⇒ (code, payload)。⛔ 撤回是安全方向：一鍵、不跳確認（跟「關閉」同一個不對稱）。"""
    try:
        ok, msg = lots.revoke_pending()
    except Exception as e:                     # noqa: BLE001
        return 500, {"ok": False, "msg": "撤回出錯：%s" % str(e)[:120]}
    _lots_refresh_small()
    print("[口數] %s 撤回：%s" % (who, msg), flush=True)
    return (200 if ok else 409), {"ok": ok, "msg": msg}


def _lots_refresh_small():
    try:
        small = lots_state_small()
        with state_lock:
            STATE["lots"] = small
    except Exception:                          # noqa: BLE001
        pass


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


PK.day_features = day_features           # K 線那支（panel_kline）的 day_bars 要用
PK.prev_day_close = prev_day_close


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


# 手機同步那支（panel_phone）要讀寫練習紀錄 ⇒ 接上面板這邊的（TRADE_DIR 用 lambda：測試導走時跟著走）
PH.trade_dir = lambda: TRADE_DIR
PH.load_today_trades = load_today_trades
PH.all_practice_trades = all_practice_trades


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

# ⭐ 2026-11-12 整理第 3 批：網頁畫面搬到 panel_page.html（原封不動；這裡讀進來，下面照舊替換規則數字）。
#    ⛔ 改畫面只動 panel_page.html；test_panel_page 守送出去的網頁跟搬之前同一套內容。
PAGE = (HERE / "panel_page.html").read_text(encoding="utf-8")

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
        if self.path.split("?", 1)[0] in ("/api/fire/on", "/api/nightfire/on", "/api/risk/override",
                                         "/api/lots/upgrade", "/api/lots/revoke"):
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
        # ⭐ 2026-10-20【重啟鈕】前端兩段式；這裡再驗一次條件（有部位／單子在送／關鍵時刻／沒人顧 ⇒ 409）。
        #    ⛔ 六道防護在入口已經過了（同上）。
        if self.path == "/api/app/restart":
            code, out = request_restart()
            return self._json(code, out)

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

        # ⭐ 2026-10-14 口數：升級（⛔ 前端兩段式；只收這個月建議的那一級）／撤回（一鍵，安全方向）。
        #   ⛔ 路由精確比對；六道防護在 do_POST 入口已經過了（⛔ 這裡不再呼叫第二次）。
        if self.path == "/api/lots/upgrade":
            code, out = lots_upgrade(body, who=self.client_address[0] if self.client_address else "?")
            return self._json(code, out)

        if self.path == "/api/lots/revoke":
            code, out = lots_revoke(who=self.client_address[0] if self.client_address else "?")
            return self._json(code, out)

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
                 "deposit": r.get("deposit")} for r in rows], "goal": goal_view(),
                "cash": cashflow_view()})
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
        if self.path.split("?", 1)[0] in ("/api/fire/on", "/api/nightfire/on", "/api/risk/override",
                                         "/api/lots/upgrade", "/api/lots/revoke"):
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
        # ⭐ 2026-10-21 右上角鈴鐺「要注意的事」（⛔ 唯讀；句子＝手機那份同一張快照，見 monitor_push.notice）
        if self.path.split("?", 1)[0] == "/api/notice":
            try:
                return self._json(200, notice_view())
            except Exception as e:             # noqa: BLE001
                return self._json(200, {"ok": False, "items": [], "msg": "讀不到要注意的事：%s" % str(e)[:100]})
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
                # ⭐ 2026-10-14：那一口後來怎麼了（唯讀比對 real_trades/，鍵是那一晚 E）＋ 送出過的那幾列
                #   （畫面「最近做得如何」要日盤夜盤一起算，才會跟【本月風控】同一個數）。⛔ 包 try：算不出來只是留白。
                try:
                    res = night_results()
                    out["results"] = [{k: o.get(k) for k in ("E", "method", "dir", "entry", "entry_time", "live", "qty")}
                                      for o in res]
                    out["real"] = night_real_pairs(res)
                except Exception as e:      # noqa: BLE001
                    out["results"], out["real"], out["real_err"] = [], {}, str(e)[:120]
                try:
                    out["closed"] = closed_days()
                except Exception:           # noqa: BLE001
                    out["closed"] = []
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
                # ⭐ 2026-10-14：U（多方聯軍）的模擬在 sim_lanes/ ⇒ 另外一份（⛔ 包 try：算不出來只是留白）
                try:
                    out["sim_lane"] = fire_sim_lane(out.get("days") or [])
                    out["rule_id"] = dict(auto_fire.RULE_ID)
                except Exception:           # noqa: BLE001
                    out["sim_lane"], out["rule_id"] = {}, {}
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
                # ⭐ 2026-10-14：休市的平日（畫面上那一天改寫「休市」，⛔ 不是「收不到成交價」）
                try:
                    out["closed"] = closed_days()
                except Exception:           # noqa: BLE001
                    out["closed"] = []
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
                # ⭐ 2026-10-14 口數卡（lots.panel：句子全在後端組好；⛔ 讀不到也只是那一塊寫「讀不到、照 1＋1 送」）
                out["lots"] = lots_panel_now()
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
            try:
                _rs = restart_info()             # ⭐ 2026-10-20 重啟鈕（⛔ 鎖外面算；便宜）
            except Exception as e:               # noqa: BLE001
                _rs = {"ok": False, "why": "判斷不了能不能重啟：%s" % str(e)[:80]}
            with state_lock:
                try:
                    payload = json.dumps(dict(STATE, token=FIRE_TOKEN, restart=_rs),
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
                    safe["restart"] = _rs
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


# ⭐ 【帳戶總覽】的底層（問帳戶、一口保證金、權益紀錄、夠不夠下一口、流量、備份觸發）
#    ⇒ 2026-11-12 整理第 5 批原樣搬到 panel_account.py。背景那條 poll_equity 留在這裡（要拿 state_lock）。
import panel_account as PA  # noqa: E402
from panel_account import (EQUITY, EQUITY_BUSY_END, EQUITY_EVERY, EQUITY_EVERY_OFF, EQUITY_HIST_MAX, USAGE,  # noqa: E402,F401
                           _backup_read, _backup_tick, _eq_quiet, _equity_lot1, _equity_lot1_restore,
                           _equity_month, _equity_write_day, _lot1, _soxx_tick, _taifex_lot1_tick,
                           _usage_poll, backup_view, equity_cushion, equity_hist_read, equity_view,
                           parse_taifex_margin, usage_view)
PA.PRODUCT = PRODUCT
PA.price_now = lambda: getattr(CURRENT_STATE.get("today"), "price", None)
PA.api_now = lambda: SESSION_REF.get("api")



def goal_view(now=None):
    """
    ⭐ 2026-09-30【帳戶】的目標卡（Benson：「多少錢幾口、每月獲利、我現在在哪、大概幾月到哪」）。
    ⛔ 唯讀：全部計算在 goal.py（純函式）；這裡只把權益紀錄＋券商現在的權益遞過去。
    ⛔ 壞了只回一句話，⛔ 不可以把 /api/account/hist 整支帶掉。
    """
    if goal is None:
        return {"ok": False, "msg": "目標模組沒有載入"}
    try:
        m = EQUITY.get("m") or {}
        eq = m.get("equity_amount")
        dep = m.get("deposit_withdrawal") if isinstance(m.get("deposit_withdrawal"), (int, float)) else 0.0
        G = goal.view(equity_hist_read(), eq if isinstance(eq, (int, float)) else None, dep, now, live=m)
        # ⭐ 2026-10-14：權益夠了 ≠ 已經升級 ⇒ 「現在在跑哪一級」那句、階梯的狀態欄、#lotsline 那一行用口數的正本改
        #    （⛔ 計畫線照舊是 goal.py 算的；口數讀不到 ⇒ 原封不動）
        try:
            G = lots.goal_overlay(G, lots_panel_now(now))
        except Exception:                  # noqa: BLE001
            pass
        return G
    except Exception as e:                 # noqa: BLE001
        return {"ok": False, "msg": "目標算不出來：%s" % str(e)[:100]}


# 口數金條裡「手機清單已經有」的種類（⛔ 不重複列）：可以升級／退休 走 lots 的 phone.todo，部位那幾種走 r_lots_alarm
NOTICE_LOTS_SKIP = ("offer", "retire", "pos_unknown", "pos_handover", "pos_touched", "pos_fill")


def notice_view():
    """
    ⭐ 2026-10-21 右上角鈴鐺（Benson：所有需要我注意的提醒都放在裡面）。⇒ {"ok", "items":[{lvl,text,act,key,hide}]}
    ⛔ 規則**只有一份**：手機「要注意的事」（monitor_push._todo）。這裡只再補上口數金條裡手機沒列的那幾則
       （減級、事故暫停、暫停解除…）—— 那些原本是頁面上的金條，Benson 定：一律收進鈴鐺。
    hide：能不能按「知道了」關（只有「有空再看」可以；要馬上處理／要你決定 ⛔ 不給關）。
    """
    import monitor_push
    todo, alarm = monitor_push.notice(PORT)
    items = []
    for x in todo:
        if not isinstance(x, dict) or not x.get("text"):
            continue
        lvl = x.get("lvl") if x.get("lvl") in ("now", "decide", "later") else "later"
        items.append({"lvl": lvl, "text": x["text"], "act": x.get("act") or "",
                      "key": "todo|" + x["text"], "hide": lvl == "later"})
    for a in alarm:
        if not isinstance(a, dict) or not a.get("text") or str(a.get("kind")) in NOTICE_LOTS_SKIP:
            continue
        items.append({"lvl": "later", "text": str(a["text"]), "act": "到【自動下單】看口數",
                      "key": "%s|%s" % (a.get("kind") or "", a["text"]), "hide": True})
    return {"ok": True, "items": items}


def cashflow_view(now=None):
    """
    ⭐ 2026-10-21【帳戶】金流明細（Benson：實際的錢扣多少加多少）。
    ⛔ 唯讀：全部計算在 cashflow.py（純函式）；這裡只把權益紀錄、真單、兩本帳本、券商現在那一份遞過去。
    ⛔ 壞了只回一句話，⛔ 不可以把 /api/account/hist 整支帶掉。
    """
    if cashflow is None or goal is None:
        return {"ok": False, "msg": "金流明細模組沒有載入"}
    try:
        now = now or datetime.now()
        m = EQUITY.get("m") or None
        live = goal.live_row(m, now.date()) if m else None
        if live and EQUITY.get("at"):
            live["live_at"] = EQUITY["at"]  # 帳戶抓的時間：之後才出場的單先不列（不然會誤標對不起來）
        out = cashflow.build(equity_hist_read(), cashflow.load_trades(broker.TRADE_DIR),
                             cashflow.load_day_res(auto_fire.FIRE_DIR), cashflow.load_night_res(night_fire.NF_DIR),
                             goal.eff_deposit, live=live, today=str(now.date()))
        out["ok"] = True
        return out
    except Exception as e:                 # noqa: BLE001
        return {"ok": False, "msg": "金流明細算不出來：%s" % str(e)[:100]}


LOTS_ERR = {"err": None}               # 口數每日檢查最近一次的錯（第二階段畫面才端出去）


def poll_equity():
    """
    背景問「帳戶還有多少錢」。⚠️ **唯讀**（`broker.account_margin()` ⇒ `api.margin()`）。
    ⛔ 送單那幾刻前後 10 秒不問；⛔ 沒連上永豐就不問（⛔ 不重試到把流量吃完）。
    ⚠️ 節奏扣掉工作時間（跟 `poll_index` 同一個作法）。
    """
    while True:
        t0 = time.time()
        now = datetime.now()
        # ⭐ 2026-09-26 每天 05:30 觸發資料備份（另開行程、最低優先權；⛔ 面板自己不上傳）。壞了不影響任何事
        try:
            _backup_tick(now)
        except Exception:
            pass
        try:
            _soxx_tick(now)
        except Exception:
            pass
        # ⭐ 2026-09-29 期交所公告的一口原始保證金（一天一次；壞了只記錯）
        try:
            if not _eq_quiet(now):
                _taifex_lot1_tick(now)
        except Exception:
            pass
        try:
            if SESSION_REF.get("api") is not None and not _eq_quiet(now):
                m, err = broker.account_margin()
                if m:
                    EQUITY.update({"m": m, "err": None,
                                   "at": now.strftime("%H:%M:%S")})
                    _equity_lot1(m)
                    _equity_write_day(m, now)
                else:
                    # ⛔ 問不到就把數字清掉：畫面寧可寫「問不到」，
                    #    ⛔ 也不可以繼續顯示一個看起來是現在、其實是十分鐘前的金額。
                    EQUITY.update({"m": None, "err": err or "問不到帳戶餘額"})
                # ⭐ 2026-09-26 券商每日流量（加錄資料之前先量基準）。⛔ 唯讀；自己 10 分鐘一次；壞了不影響帳戶查詢
                # ⚠️ 2026-09-29 修：第一版把這個 try 插在 `if m:` 跟它的 `else:` 中間 ⇒ else 變成 try 的 else，
                #    流量一問成功就把剛問到的餘額清掉，畫面永遠「問不到帳戶餘額」。守衛：test_account ⑫
                try:
                    _usage_poll(now)
                except Exception:
                    pass
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
        # ⭐ 2026-10-14 口數（lots.py）每日檢查：equity/ 每多一列（14:00 落地）就跑一次 ——
        #    自動減級（安全方向）、記本月用過最大口數、已確認的升級到生效日轉正、每月第一個交易日產生升級建議。
        #    ⛔ 不拿 state_lock、⛔ 不碰下單路徑；沒新的一列時只讀一下狀態檔就回。壞了只記錯、不影響任何事。
        try:
            try:
                _closed = set(closed_days())          # ⭐ 休市平日：不出建議、夜盤振幅「太舊」用交易日算
            except Exception:                         # noqa: BLE001
                _closed = set()
            if _closed:
                lots.KNOWN_CLOSED = _closed           # ⛔ 只換整個集合（讀的人拿到的是舊的或新的那一份，不會半套）
            _lr = lots.daily_check(equity_hist_read(), now, closed=_closed,
                                   busy=broker.has_position())   # 有部位 ⇒ 06:00 那道先不跑
            if _lr.get("err"):
                LOTS_ERR["err"] = _lr["err"]
            # ⭐ 2026-11-02 入金當下就重算升級條件（⛔ 有部位、送單那幾刻不跑；只出建議，升級仍要他按確認）
            if not _eq_quiet(now):
                _lr = lots.live_check((EQUITY.get("m") or {}).get("equity_amount"), now,
                                      busy=broker.has_position())
                if _lr.get("err"):
                    LOTS_ERR["err"] = _lr["err"]
            _small = lots_state_small()               # ⛔ 讀檔在鎖外面，鎖裡只指派
            with state_lock:
                STATE["lots"] = _small
        except Exception as e:                        # noqa: BLE001
            LOTS_ERR["err"] = str(e)[:120]
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
    # ⭐ 2026-11-05 健檢：原本只看 broker.py／live_panel.py ⇒ 只改 auto_fire／night_fire／lots 時
    #    畫面說「最新」、其實跑的是舊的。⇒ 這個行程載入的所有本地模組都要看。
    for mod in list(sys.modules.values()):
        fn = getattr(mod, "__file__", None)
        try:
            if fn and Path(fn).parent == HERE and fn.endswith(".py"):
                newest = max(newest, Path(fn).stat().st_mtime)
        except Exception:                 # noqa: BLE001
            pass
    # ⭐ 2026-11-12 整理第 3 批：網頁畫面在 panel_page.html（不是模組，上面那圈看不到）⇒ 也要看
    try:
        newest = max(newest, (HERE / "panel_page.html").stat().st_mtime)
    except Exception:                     # noqa: BLE001
        pass
    # stale ＝ 硬碟上的程式比這個行程還新 ⇒ 我跑的不是最新的那份。
    # `panel_app.pyw` 靠這個決定「要不要先把舊伺服器收掉再開」，
    # 前端也靠它跳提醒 —— 不然關視窗再開只是接回同一個舊伺服器，
    # 改過的程式永遠載不進來，而且畫面上完全看不出來（2026-09-01 白重開一次）。
    return {"broker": out[0], "panel": out[1],
            "started": BOOT_AT.strftime("%m-%d %H:%M"),
            "stale": newest > BOOT_AT.timestamp() + 2}


# ══ 重啟按鈕（2026-10-20 Benson 要的）══════════════════════════════════════════════
# 效果＝關掉 App 再從桌面捷徑開：這個行程**自己結束**，外面的看門狗（panel_app.pyw／start-panel.bat）
# 看到它結束就用硬碟上最新的程式重開（10 秒後）。⛔ 這裡不自己開新行程（兩個面板搶同一個埠＋同一個永豐帳號）。
# ⛔ 離開碼用 1：start-panel.bat 是 `if errorlevel 2 goto already`（≥2 就停）、panel_app 只有 2 才停。
# ⛔ 擋的情況（他 10-20 選的）：手上有部位（重開那一分鐘停損沒人顧）、單子正在送、自動下單的關鍵時刻、
#    不是被看門狗顧著（例如從終端機直接跑）⇒ 結束了就不會回來。
RESTART_EXIT = 1
RESTART_BLOCK = (                      # (起, 迄, 原因)；含頭不含尾，時刻都是 HH:MM
    ("08:58", "09:31", "開盤自動下單判斷中（09:00～09:30）"),
    ("13:27", "13:31", "結算日收盤平倉前後"),
    ("13:42", "13:46", "收盤平倉前後（13:43:30）"),
    # 夜盤送單那段不寫在這裡：冬夏令會換（夏令 21:33～21:43、冬令 22:33～22:43），見 restart_blocks()
    # ⭐ 11-12：夜盤 ≥ 5 口 04:50 就平（market_clock.NIGHT_EOD_EARLY）⇒ 多擋 04:48～04:52（中間沒部位照樣可以重啟）
    ("04:48", "04:52", "夜盤 5 口以上收盤平倉前後（04:50）"),
    ("04:56", "05:01", "夜盤收盤平倉前後（04:58）"),
)
RESTART = {"at": None}                 # 已經要求重啟的時間（主迴圈看到就收工）
SUPERVISOR = {"who": None, "done": False}


def _find_supervisor():
    """
    往上找兩三層父行程，看是不是被看門狗顧著。⛔ 只在開機時背景跑一次（PowerShell 要好幾秒）。
    venv 的 python.exe 是個轉接器：真正的行程 → venv python.exe → panel_app.pyw（或 cmd.exe 跑 bat）。
    """
    try:
        import subprocess
        ps = ("Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,CommandLine "
              "| ConvertTo-Json -Compress")
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                             creationflags=0x08000000, timeout=60)
        rows = json.loads(out.stdout or "[]")
        rows = [rows] if isinstance(rows, dict) else rows
        by = {r.get("ProcessId"): r for r in rows}
        pid = os.getpid()
        for _ in range(4):
            r = by.get(pid)
            if not r:
                break
            pid = r.get("ParentProcessId")
            cl = ((by.get(pid) or {}).get("CommandLine") or "").lower()
            if "panel_app.pyw" in cl:
                SUPERVISOR["who"] = "app"
                break
            if "start-panel.bat" in cl or "start-morning.bat" in cl:
                SUPERVISOR["who"] = "bat"
                break
    except Exception as e:                  # noqa: BLE001  ⛔ 找不到就當沒人顧（按鈕不給按，安全方向）
        print("⚠️ 【重啟鈕】看不出是誰開的面板：%s" % str(e)[:120])
    SUPERVISOR["done"] = True


def restart_check(now=None):
    """能不能按重啟 ⇒ (ok, 原因)。⛔ 便宜（/api/state 每 0.5 秒會叫）：不碰磁碟、不拿 state_lock。"""
    now = now or datetime.now()
    if RESTART["at"]:
        return False, "重啟中…"
    if not SUPERVISOR["done"]:
        return False, "面板剛開，還在確認是誰開的（約半分鐘）"
    if not SUPERVISOR["who"]:
        return False, "這次面板不是用桌面捷徑開的，關掉不會自己開回來 —— 請手動重開"
    if broker.has_position():
        return False, "現在有部位，平倉後才能重啟（重開那一分鐘停損沒人顧）"
    if ENTER_LOCK.locked():
        return False, "單子正在送，等一下再按"
    hm = now.strftime("%H:%M")
    for a, b, why in restart_blocks(now):
        if a <= hm < b:
            # ⭐ 2026-11-05 Benson：09:03 進、09:09 停損出場之後還是被擋到 09:31。開盤那段擋的是「自動下單還在判斷」，
            #    今天那一口已經有結果（或三個候選都收工）＋上面已確認沒部位 ⇒ 沒東西要顧了，放行。
            #    ⛔ 只放開盤這一段；看帳本檔案（有 5 秒快取，這支每 0.5 秒會叫）；讀不到一律當還沒定論＝照擋。
            if a == MORNING_BLOCK_AT and _morning_settled(now):
                continue
            return False, "%s～%s %s，先不要重啟" % (a, b, why)
    return True, None


MORNING_BLOCK_AT = RESTART_BLOCK[0][0]


def _morning_settled(now):
    try:
        return bool(auto_fire.morning_settled(str(now.date())))
    except Exception:                     # noqa: BLE001  ⛔ 安全方向：看不出來就照擋
        return False


def restart_blocks(now):
    """固定的擋時段＋今晚夜盤送單那段（跟著美股冬夏令走）。算不出來 ⇒ 夏令、冬令兩段都擋。"""
    out = list(RESTART_BLOCK)
    try:
        d, t = night_moments(now)
        # ⭐ 2026-11-05 健檢：夜盤跟勢要用「進場前 30 分鐘那一分鐘」的價，那個價**只在面板記憶體裡**
        #    （Today.minute_close，重啟後夜盤那段不會補回來）⇒ 從那一分鐘之前就要擋，不然重啟＝今晚不做。
        ref = t - timedelta(minutes=night_fire.trend_rule.LOOKBACK + 3)
        a = min(ref, d - timedelta(minutes=2)).strftime("%H:%M")
        b = (t + timedelta(minutes=3)).strftime("%H:%M")
        out.append((a, b, "夜盤自動下單判斷中（%s 起要用記憶體裡的參考價，%s 送單）"
                    % (ref.strftime("%H:%M"), t.strftime("%H:%M"))))
    except Exception:                     # noqa: BLE001
        out.append(("21:33", "21:43", "夜盤自動下單判斷中（夏令）"))
        out.append(("22:33", "22:43", "夜盤自動下單判斷中（冬令）"))
    return out


PD.restart_blocks = restart_blocks       # ⭐ 五檔重連跟重啟鈕吃同一份關鍵時段（兩份清單不准分岔）


# ══ 自動重啟（2026-11-05 Benson 要的）════════════════════════════════════════════
# 程式有新版（code_stamp stale）＋ 沒有部位 ＋ 安全時段 ⇒ 面板自己按一次重啟鈕（走同一支 request_restart，
# 條件 restart_check 一條都不少）。⛔ 一天最多一次；⛔ 不自己去抓新版（git pull 仍由人做 ⇒ 只有確認過的版本會上線）。
# 安全時段：平日 05:05～08:30（夜盤 04:58 平倉完、08:30 每天重新登入之前）；週六 05:05 起、週日整天。
AUTO_RESTART = {"day": None, "checked": 0.0}
USAGE_DIR = HERE / "usage"             # ⛔ gitignore（流量紀錄在 panel_account 那邊寫，這裡只放自動重啟的紀錄）
AUTO_RESTART_FILE = USAGE_DIR / "auto_restart.json"   # ⛔ usage/ 在 gitignore 裡
AUTO_RESTART_EVERY_S = 30


def auto_restart_window(now):
    hm = now.strftime("%H:%M")
    wd = now.weekday()
    if wd == 6:
        return True
    if wd == 5:
        return hm >= "05:05"
    return "05:05" <= hm < "08:30"


def auto_restart_tick(now=None, stamp=None):
    """主迴圈每一圈叫（⛔ 便宜：30 秒才真的看一次）。⇒ True＝已經要求重啟。"""
    now = now or datetime.now()
    if AUTO_RESTART["day"] == now.date() or not auto_restart_window(now):
        return False
    t = time.time()
    if t - AUTO_RESTART["checked"] < AUTO_RESTART_EVERY_S:
        return False
    AUTO_RESTART["checked"] = t
    st = stamp if stamp is not None else code_stamp()
    if not st.get("stale"):
        return False
    ok, _why = restart_check(now)
    if not ok:
        return False
    AUTO_RESTART["day"] = now.date()
    try:
        AUTO_RESTART_FILE.parent.mkdir(parents=True, exist_ok=True)
        AUTO_RESTART_FILE.write_text(json.dumps({"at": now.isoformat(timespec="seconds"),
                                                 "old": st.get("started")}, ensure_ascii=False),
                                     encoding="utf-8")
    except Exception:                     # noqa: BLE001  寫不了只是少一則通知
        pass
    print("[自動重啟] 程式有新版、沒有部位、在安全時段 ⇒ 重啟換上新版", flush=True)
    code, _out = request_restart()
    return code == 200


def auto_restart_notice():
    """⚠️ 背景執行緒：上一次是自動重啟 ⇒ 送一則手機通知（⛔ 失敗只印一行）。"""
    try:
        import subprocess
        if not AUTO_RESTART_FILE.exists():
            return
        info = json.loads(AUTO_RESTART_FILE.read_text(encoding="utf-8"))
        AUTO_RESTART_FILE.unlink()
        body = "%s 換上新版（沒有部位、安全時段才自動重啟）。" % str(info.get("at", ""))[5:16].replace("T", " ")
        subprocess.run(["py", "-3.11", str(HERE / "push_note.py"), "面板已自動重啟", body],
                       capture_output=True, timeout=60)
        print("[自動重啟] 已通知手機：" + body, flush=True)
    except Exception as e:                # noqa: BLE001
        print("⚠️ [自動重啟] 通知手機失敗：%s" % str(e)[:120], flush=True)


def restart_info():
    ok, why = restart_check()
    return {"ok": ok, "why": why, "who": SUPERVISOR["who"], "pending": bool(RESTART["at"])}


def request_restart():
    """⇒ (http 狀態, 回應)。條件再驗一次（前端看到的可能是 0.5 秒前的）；過了就記下來，主迴圈收工。"""
    ok, why = restart_check()
    if not ok:
        return 409, {"ok": False, "msg": "現在不能重啟：" + str(why)}
    RESTART["at"] = time.time()
    print("[重啟] 他在面板上按了重啟 —— 收工後由看門狗重開", flush=True)

    def _hard():
        # 主迴圈 20 秒內還沒收完（例如卡在永豐的呼叫）⇒ 直接結束，看門狗照樣會重開
        time.sleep(20)
        os._exit(RESTART_EXIT)
    threading.Thread(target=_hard, daemon=True).start()
    return 200, {"ok": True, "msg": "收到，面板正在重啟，大約一分鐘後會自己接回來"}


# ⭐ 2026-11-05 健檢：real_state 每一圈（主迴圈、而且在 state_lock 裡）都叫 broker.can_enter，
#    它會跟永豐要一次 list_positions ⇒ 一秒好幾次查帳、停損那條迴圈每圈都在等網路，
#    查太頻繁還可能被永豐擋（11-03 04:11 那次 500 就是查部位）。
#    這裡只是**畫面上那句「能不能進場」**：每 CAN_ENTER_VIEW_S 秒才真的問一次；有沒有部位變了就馬上重問。
#    ⛔ 真的要送單時（手動進場端點、自動下單）各自都會重新叫 broker.can_enter，不吃這份快取。
#    ⛔ 對帳本身由 broker.reconcile_tick()（3 秒節流）負責，跟這裡無關。
CAN_ENTER_VIEW_S = 3.0
_CAN_ENTER_VIEW = {"at": 0.0, "key": None, "v": (False, "")}


def _can_enter_view(price, live, has_pos, now=None):
    now = time.time() if now is None else now
    key = (bool(live), bool(has_pos), price is None)
    c = _CAN_ENTER_VIEW
    if c["key"] == key and now - c["at"] < CAN_ENTER_VIEW_S:
        return c["v"]
    v = broker.can_enter(price, live)
    c.update(at=now, key=key, v=v)
    return v


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
            if pos.get("stop_off"):
                # ⭐ QA r3 必修 3：停損不會執行 ⇒ ⛔ 不端停損價／停利價（畫面才不會畫刻度、寫「離停損 N 點」）
                snap["sl"] = snap["tp"] = snap["tp_pts"] = snap["sl_pts"] = None
        stale = REAL_STALE["since"]
        snap["stale_sec"] = round(time.time() - stale) if stale else None
        ok, why = _can_enter_view(price, quote == "live", bool(pos))
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
    return broker.has_position()


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
        if broker.has_position():
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
    ⚠️ 夜盤帳本本身沒有出場價 ⇒ 2026-10-14 起走 `night_real_pairs()` 對 real_trades/（對不到才留白、⛔ 不猜）。
    ⚠️ 「夜盤跟勢」(trend) 是前瞻考試、⛔ 不下單 ⇒ 沒有這一條（呼叫端會照實寫出來）。
    ⛔ 這支跑在 HTTP 執行緒上 ⇒ 只讀那 60 天（`FIRE_REAL_DAYS`），⛔ 不掃整個帳本。
    """
    out = {}
    try:
        st = auto_fire.state()
        days = st.get("days") or []
        real = fire_real_pairs(days, today=st.get("today"), now=st.get("now"),
                               eod_at=st.get("eod_at"))
        # ⭐ 2026-10-19（Benson 截圖）：舊版把 09-17 換成多方聯軍之前的快攻回馬槍（A）也算進「多方聯軍」⇒ −57.1 是混出來的。
        #    ⇒ 照帳本那一列的做法分開算（U＝多方聯軍、A＝快攻回馬槍；舊列沒有 method 的一律算 A）。
        meth = {r.get("date"): (r.get("method") or "A") for r in days
                if isinstance(r, dict) and r.get("rec") == "result" and r.get("ok")}
        for lane, m, nm in (("union", "U", "多方聯軍"), ("hmq", "A", "快攻回馬槍")):
            pts = [v["points"] for d, v in real.items()
                   if meth.get(d) == m and isinstance(v, dict) and v.get("state") == "ok"
                   and isinstance(v.get("points"), (int, float))
                   and not isinstance(v.get("points"), bool)]
            avg = round(sum(pts) / len(pts), 1) if pts else None
            out[lane] = {"n": len(pts), "avg": avg,
                         "msg": ("最近 %d 天用%s下的真單有 %d 筆，每筆平均 %s 點"
                                 % (FIRE_REAL_DAYS, nm, len(pts), ("%+.1f" % avg) if avg is not None else "—")
                                 if pts else "最近 %d 天還沒有用%s下的真單" % (FIRE_REAL_DAYS, nm))}
    except Exception as e:
        out["union"] = {"n": None, "avg": None, "msg": "讀不出來：" + str(e)[:80]}
        out["hmq"] = out["union"]
    # ⭐ 2026-09-23 夜盤有兩條了（T 台積電快攻／R 夜盤跟勢）⇒ 依帳本那一列的 `method` 分開數；
    #    ⚠️ 2026-09-23 以前的列沒有 `method`，那時只有 T ⇒ 算 T。⛔ 不可以兩條混成一張卡。
    # ⭐ 2026-10-14：點數從 `night_real_pairs()` 拿（唯讀比對 real_trades/，跟【自動下單】那一頁同一把尺）。
    #    ⛔ 對不到的那幾口只算「送出了」、不算進平均。
    try:
        res = night_results()
        real = night_real_pairs(res)
        n, pts = {"T": 0, "R": 0}, {"T": [], "R": []}
        for o in res:
            k = o.get("method") or "T"
            n[k] = n.get(k, 0) + 1
            v = real.get(str(o.get("E") or ""))
            if isinstance(v, dict) and v.get("state") == "ok" and isinstance(v.get("points"), (int, float))                     and not isinstance(v.get("points"), bool):
                pts.setdefault(k, []).append(v["points"])
        for k, lane in (("T", "tsm"), ("R", "trend")):
            p = pts.get(k) or []
            avg = round(sum(p) / len(p), 1) if p else None
            if not n.get(k):
                msg = "還沒有送出過真單"
            elif p:
                msg = "送出了 %d 口，%d 口算得出點數，每筆平均 %+.1f 點" % (n[k], len(p), avg)
            else:
                msg = "送出了 %d 口；還對不到出場紀錄，點數留白" % n[k]
            out[lane] = {"n": n.get(k, 0), "avg": avg, "msg": msg}
    except Exception as e:
        out["tsm"] = {"n": None, "avg": None, "msg": "讀不出來：" + str(e)[:80]}
    return out


NIGHT_LANE = {"T": "tsm", "R": "trend"}


def _health_armed():
    """【健檢】要顯示哪幾條：現在**開著**的自動真單（日盤 AUTO_ORDERS_ON、夜盤 NIGHT_ORDERS_ON）⇒ 模擬那邊的 lane key。
    ⛔ 唯讀（arm() 只讀開關檔）。⚠️ 開著但 REAL_ORDERS_ON 關著時是「演練」，一樣算開著（畫面那一條會講）。"""
    keys = []
    a = auto_fire.arm()
    if a.get("on"):
        keys.append(auto_fire.RULE_ID.get(a.get("method")))
    n = night_fire.arm()
    if n.get("on"):
        keys.append(NIGHT_LANE.get(n.get("method")))
    return [k for k in keys if k]


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
        # ⛔ 只收真單那個商品的成交（研究用只訂五檔、不訂別的成交；這一行是多一道保險）
        if not _is_main_code(getattr(tick, "code", None)):
            return
        ts = tick.datetime
        price, vol = float(tick.close), int(tick.volume)
        st = session["state"]
        if st is not None:
            # 價格任何時候都收（面板要一直顯示）；日盤才累積開高低與量。
            # 這一句排在落地之前 —— 停損看的價要先更新，其他事都排在後面。
            st.feed(price, vol, ts, in_session=SESSION_OPEN <= ts.time() < DAY_END)
        TICKS.tick(ts, price, vol)      # 只 append，不碰磁碟
        # ⭐ 2026-11-12 稽核：本機時鐘 vs 交易所時間（09:03:30／22:40 都用本機時鐘）。只做幾個加減（⛔ 不加 I/O）。
        #    一分鐘內取最小值（濾掉網路延遲）⇒ broker.CLOCK_SKEW；差太多 can_enter 會擋。
        try:
            _now = time.time()
            _sk = _now - ts.timestamp()
            ck = broker.CLOCK_SKEW
            if _now - ck["win_at"] > 60:
                ck["s"], ck["at"] = ck["win_min"], ck["win_at"]
                ck["win_min"], ck["win_at"] = _sk, _now
            elif ck["win_min"] is None or _sk < ck["win_min"]:
                ck["win_min"] = _sk
        except Exception:               # noqa: BLE001
            pass

    def on_bidask(exchange: Exchange, ba: BidAskFOPv1):
        DEPTH_WD["last_ba"] = time.time()   # ⭐ 五檔看門狗：只記一個時間（⛔ 不加任何 I/O）
        DEPTH.push(ba)                  # 研究用全天五檔：只 append、自己吞錯
        # ⛔⛔ 大台／選擇權的五檔到這裡就停 —— 絕對不可以進 Today（停損看的價、報價新不新鮮）
        if not _is_main_code(getattr(ba, "code", None)):
            return
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
        # ⭐ 2026-10-21：先剔掉已到期的（結算日 13:30 後重啟會挑到舊月，見 contract_alive）
        cands = [c for c in cands if contract_alive(c)] or cands
        front = pick_front(cands)
        if front is not None:
            return front
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
        # ⭐ 2026-11-06 小台：跟微台**同一個交割月**的小台也交給 broker（開關 MINI_ON 沒開就只是備著，
        #    但對帳一定兩種都看 ⇒ 重啟時券商上的小台部位不會被當成「沒部位」）。找不到 ⇒ None（只用微台）。
        mxf = None
        try:
            want = getattr(contract, "delivery_month", None)
            mxf = next((c for c in getattr(api.Contracts.Futures, "MXF")
                        if getattr(c, "delivery_month", None) == want
                        and not str(c.code).startswith("MXFR")), None)
        except Exception as e:                # noqa: BLE001
            print("⚠️ [小台] 找不到跟微台同月的小台合約：%s" % str(e)[:120])
        broker.configure(api, contract, {"MXF": mxf})   # 真實下單要用同一個連線與同一組合約
        api.set_on_tick_fop_v1_callback(on_tick)
        api.set_on_bidask_fop_v1_callback(on_bidask)
        # 成交 + 五檔都訂：成交價進模型，五檔負責讓畫面跟得上市場
        api.quote.subscribe(contract, quote_type=sj.constant.QuoteType.Tick,
                            version=sj.constant.QuoteVersion.v1)
        api.quote.subscribe(contract, quote_type=sj.constant.QuoteType.BidAsk,
                            version=sj.constant.QuoteVersion.v1)
        session["api"], session["contract"] = api, contract
        session["conn_at"] = time.time()
        SESSION_REF["api"] = api
        CONN.update({"ok": True, "since": None, "retries": 0, "last_error": None,
                     "contract": contract.code,
                     "contract_name": getattr(contract, "name", "")})
        print(f"訂閱合約：{contract.code} {getattr(contract,'name','')}"
              f"（交割月 {contract.delivery_month}）")
        # ⭐ 研究用五檔（大台＋選擇權）。排在真單合約訂好、狀態都設好之後；⛔ 它自己吞錯，不會讓連線失敗
        depth_subscribe(api, contract)
        DEPTH_WD["last_ba"] = time.time()   # 剛訂好 ⇒ 從現在起算，⛔ 不拿上一條連線的舊時間判斷
        return api, contract

    def try_reconnect(reason):
        """斷線重連。失敗不會讓程式死掉，會留下錯誤訊息顯示在面板上。"""
        # ⭐ 2026-11-12 稽核：工作執行緒正在送進場單／平倉單 ⇒ 這一輪不重連（送到一半換連線，
        #    「已經送出哪幾張」的紀錄會被 broker.configure 清掉，下一張照過時的口數算 ⇒ 可能平過頭）。
        #    下一輪（RECONNECT_EVERY 秒後）再看；送單本身最多十幾秒。
        try:
            if broker.busy():
                print(f"[{datetime.now():%H:%M:%S}] {reason} —— 正在送單／平倉，這一輪先不重連")
                return False
        except Exception:                 # noqa: BLE001
            pass
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

    HEAL = {"at": 0.0, "th": None}

    def heal_tick():
        """
        ⭐ 2026-11-12 稽核：連線還在（報價正常、不會觸發重連），但憑證啟用／期貨帳號／小台合約這次沒拿到
        ⇒ 背景每 60 秒補一次（⛔ 舊版要等報價斷 90 秒才重連，報價正常時可以卡好幾個小時 ⇒ 每一筆都「憑證沒啟用」不送）。
        ⛔ 主迴圈只做判斷與開執行緒（不呼叫券商 API）；⛔ 正在送單／平倉時不動。
        """
        if time.time() - HEAL["at"] < 60:
            return
        th = HEAL["th"]
        if th is not None and th.is_alive():
            return
        api = session.get("api")
        if api is None or not CONN.get("ok") or broker.busy():
            return
        ca = get_ca()
        need_ca = bool(ca) and not broker.CA_OK["ok"]
        need_acc = broker._state.get("account") is None
        need_mxf = broker.MINI_FLAG.exists() and (broker._state.get("contracts") or {}).get("MXF") is None
        if not (need_ca or need_acc or need_mxf):
            return
        HEAL["at"] = time.time()
        contract = session.get("contract")

        def run():
            try:
                if need_ca:
                    try:
                        api.activate_ca(ca_path=ca[0], ca_passwd=ca[1], person_id=ca[2])
                        if session.get("api") is api:
                            broker.CA_OK["ok"], broker.CA_OK["msg"] = True, None
                            print("[憑證] 背景補啟用成功，可以真實下單", flush=True)
                    except Exception as e:      # noqa: BLE001
                        broker.CA_OK["msg"] = f"憑證啟用失敗（背景每分鐘重試中）：{str(e)[:120]}"
                mxf = None
                if need_mxf:
                    try:
                        want = getattr(contract, "delivery_month", None)
                        mxf = next((c for c in getattr(api.Contracts.Futures, "MXF")
                                    if getattr(c, "delivery_month", None) == want
                                    and not str(c.code).startswith("MXFR")), None)
                    except Exception:           # noqa: BLE001
                        mxf = None
                got = broker.heal(api, account=need_acc, mxf=mxf)
                if got:
                    print("[連線] 背景補到：%s" % "、".join(got), flush=True)
            except Exception as e:              # noqa: BLE001
                print("⚠️ [連線] 背景補憑證／帳號出錯：%s" % str(e)[:120], flush=True)
        HEAL["th"] = threading.Thread(target=run, name="conn-heal", daemon=True)
        HEAL["th"].start()

    def seed_from_bars(st, today):
        """
        盤中啟動時，用當天已經發生的 1 分 K 把開盤價／最高／最低／量補起來。

        【沒有這段數字會是錯的】Today 只從「面板啟動的那一刻」開始累積，
        所以中午重開面板，對開盤／震幅／位階／量能全部會從那一刻重算 ——
        實測顯示過「震幅 20 點、量能 0.01 倍」這種明顯不合理的值。
        """
        api = SESSION_REF.get("api")
        now_t = datetime.now().time()
        if api is not None and NIGHT_OPEN <= now_t:
            # ⭐ 2026-11-12 稽核：夜盤（15:00～23:59）重啟 ⇒ 只補 minute_close（夜盤跟勢要 30 分鐘前那一分鐘的收盤；
            #    舊版只補日盤 ⇒ 22:07～22:40 之間當機重開，今晚 no_ref 整晚不做）。⛔ 開高低量是日盤的數字，不碰。
            #    ⛔ 已經有的（重啟後即時收到的）不覆蓋。
            try:
                contract = getattr(api.Contracts.Futures, PRODUCT)[f"{PRODUCT}R1"]
                df = pd.DataFrame({**api.kbars(contract, start=str(today), end=str(today))})
                if not df.empty:
                    df["ts"] = pd.to_datetime(df["ts"])
                    g = df[(df["ts"].dt.time > NIGHT_OPEN) & (df["ts"].dt.time <= now_t)]
                    n = 0
                    for r in g.itertuples():
                        k = r.ts.hour * 60 + r.ts.minute - 1      # K 棒結束時間 − 1 分鐘（同日盤）
                        if k not in st.minute_close:
                            st.minute_close[k] = float(r.Close)
                            n += 1
                    print(f"  夜盤重啟：已用 {n} 根 1 分 K 補回分鐘收盤（夜盤跟勢的參考價用）")
            except Exception as e:              # noqa: BLE001
                print(f"  夜盤補分鐘收盤失敗：{str(e)[:100]}")
            return
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
                # ⭐ 2026-11-12 稽核：永豐 1 分 K 用**結束時間**標記（09:00 那根＝08:59～09:00）⇒ 減 1 分鐘才是
                #    minute_close 的「那一分鐘」（跟 on_tick 寫的同一把尺）。舊版早一分鐘 ⇒ 盤中重啟後 09:00 參考價拿到 08:58。
                st.minute_close[r.ts.hour * 60 + r.ts.minute - 1] = float(r.Close)
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
    DEPTH.start()

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
    auto_fire._QUOTE["fn"] = _nf_quote   # ⭐ 2026-11-12：送單前重試之後重拿報價（同停損那一個價）
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
            health.configure(real_fn=_health_real, armed_fn=_health_armed)
    except Exception as e:          # noqa: BLE001  ⛔ 刻意接住所有例外
        print("⚠️ 【健檢】真單那一半接不上（其他功能不受影響）：%s" % str(e)[:160])
    # 【手機監控】（2026-09-24）每 2 分鐘把加密快照推到 GitHub 的 monitor 分支。⛔ 唯讀、自己的執行緒、
    #   只讀面板自己的端點；⛔ 沒有金鑰檔就不啟動；⛔ 起不來只印警告（絕不擋住送單與停損那條路）。
    try:
        import monitor_push
        monitor_push.start(PORT)
    except Exception as e:          # noqa: BLE001  ⛔ 刻意接住所有例外
        print("⚠️ 【手機監控】起不來（其他功能不受影響）：%s" % str(e)[:160])
    # 【每日判斷快照】（2026-11-04）每個交易日日盤＋夜盤各落地一列「看到什麼、為什麼做／不做」。
    #   ⛔ 只記錄：只讀帳本與歷史檔、自己的執行緒、⛔ 不拿 state_lock；起不來只印警告。
    try:
        import decision_log
        decision_log.start()
    except Exception as e:          # noqa: BLE001  ⛔ 刻意接住所有例外
        print("⚠️ 【判斷快照】起不來（其他功能不受影響）：%s" % str(e)[:160])
    print("面板已啟動，可以整天掛著。每天 08:45~09:30 自動進入即時模式。（Ctrl+C 結束）")

    threading.Thread(target=_find_supervisor, daemon=True).start()   # ⭐ 重啟鈕：誰在顧這個面板
    threading.Thread(target=auto_restart_notice, name="auto-restart-note", daemon=True).start()

    last_retry = 0.0
    try:
        while True:
            if RESTART["at"] and time.time() - RESTART["at"] > 1.5:   # 先讓「收到」那個回應送回畫面
                print("[重啟] 主迴圈收工", flush=True)
                break
            try:
                now = datetime.now()
                today, t = now.date(), now.time()
                # ⭐ 2026-11-05 健檢：主迴圈（停損就在這條）的心跳。手機監控看它多久沒動 ⇒ 卡住就示警、停掉
                #    Healthchecks 心跳讓外面的看門狗響。⛔ 只寫一個數字，不拿鎖（讀的人自己算差多少秒）。
                STATE["loop_at"] = time.time()
                try:
                    auto_restart_tick(now)            # ⭐ 2026-11-05 自動重啟（條件跟重啟鈕同一支）
                except Exception:                     # noqa: BLE001  ⛔ 不准擋停損迴圈
                    pass
                try:
                    heal_tick()                       # ⭐ 2026-11-12 憑證／帳號／小台合約沒拿到 ⇒ 背景補
                except Exception:                     # noqa: BLE001  ⛔ 不准擋停損迴圈
                    pass

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
                    quiet = quiet_seconds(st.last_recv if st else None, session.get("conn_at"),
                                          sess, time.time())
                    lost = (quiet is not None and quiet > STALE_SECONDS) or not CONN["ok"]
                    if lost and time.time() - last_retry > RECONNECT_EVERY:
                        last_retry = time.time()
                        CONN["ok"] = False
                        where = "日盤" if in_day else "夜盤"
                        try_reconnect(f"{where}已 {int(quiet) if quiet else '?'} 秒沒收到報價")
                    # ⭐ 2026-10-14 五檔看門狗（見 DEPTH_WD）。⛔ 包 try：研究用的東西出錯不可以打斷停損迴圈。
                    try:
                        _has_pos = broker.has_position()
                        _dq, _go = depth_check(time.time(), DEPTH_WD["last_ba"], quiet, market_open,
                                               CONN["ok"] and not lost, _has_pos, DEPTH_WD["last_retry"], now)
                        DEPTH_WD["stale_s"], DEPTH_WD["held"] = _dq, bool(_dq and _has_pos)
                        if _go:
                            DEPTH_WD["last_retry"] = time.time()
                            DEPTH_WD["retries"] += 1
                            try_reconnect("五檔已 %d 秒沒進來（成交還在）" % int(_dq))
                    except Exception as _de:      # noqa: BLE001
                        print("⚠️ 五檔看門狗出錯（不影響停損）：%s" % str(_de)[:120])
                elif CONN["ok"] is False and time.time() - last_retry > RECONNECT_EVERY * 5:
                    # 休市時段（13:45~15:00、05:00~08:45）慢慢重試就好
                    last_retry = time.time()
                    try_reconnect("休市時段定期重試")
                if not market_open:
                    DEPTH_WD["stale_s"] = None      # 休市時本來就沒有五檔 ⇒ 不示警
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
                        # ⭐ 2026-11-05 健檢：git 推送原本在主迴圈上同步跑（每步最多 60 秒）⇒ 09:30 日盤單
                        #    多半還開著，網路卡住時停損會有幾秒到幾分鐘沒人看 ⇒ 改丟背景執行緒。
                        def _bg_sync(day=today):
                            try:
                                _ok, _msg = sync_to_cloud()
                            except Exception as _e:          # noqa: BLE001
                                _msg = "同步失敗：%s" % str(_e)[:120]
                            print(f"[{day}] 09:30 下單時段結束。雲端同步：{_msg}", flush=True)
                        threading.Thread(target=_bg_sync, name="sync-0930", daemon=True).start()
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
            except Exception as _le:          # noqa: BLE001
                # ⭐ 2026-11-12 稽核：迴圈裡任何一處意外的例外 ⇒ 記下來、下一圈照跑（⛔ 不讓整個行程退出、
                #    等看門狗一分鐘才重開 —— 那一分鐘停損沒人顧）。前 5 次與之後每 400 次（約 100 秒）印一次。
                LOOP_ERR["n"] += 1
                LOOP_ERR["run"] += 1
                LOOP_ERR["last"] = "%s: %s" % (type(_le).__name__, str(_le)[:160])
                LOOP_ERR["at"] = datetime.now().isoformat(timespec="seconds")
                try:
                    STATE["loop_err"] = dict(LOOP_ERR)        # 畫面／手機監控看得到（⛔ 不拿鎖：只換一個參照）
                except Exception:                 # noqa: BLE001
                    pass
                if LOOP_ERR["run"] > LOOP_ERR_GIVEUP:
                    # 連續錯了好幾分鐘 ⇒ 照舊版：讓行程退出、看門狗重開（重新登入、狀態全部重建，可能就好了）
                    print("⚠️ 主迴圈連續出錯 %d 圈 —— 交給看門狗重開" % LOOP_ERR["run"], flush=True)
                    raise
                if LOOP_ERR["n"] <= 5 or LOOP_ERR["n"] % 400 == 0:
                    import traceback as _tb
                    print("⚠️ 主迴圈出錯（第 %d 次，已接住、照跑）：%s\n%s" % (
                        LOOP_ERR["n"], LOOP_ERR["last"], _tb.format_exc()[-1200:]), flush=True)
            else:
                LOOP_ERR["run"] = 0
            # ⭐ 2026-11-12：連續出錯 20 圈以上（例如 08:30 重建一直失敗）⇒ 放慢到 2 秒一圈，⛔ 不要每秒對永豐連打好幾次
            if LOOP_ERR["run"] > 20:
                time.sleep(1.75)
            time.sleep(0.25)   # 模型查詢僅 12ms，跑 4Hz 沒有負擔
    except KeyboardInterrupt:
        print("\n收到中止訊號，關閉中…")
    finally:
        if session["api"] is not None:
            try:
                session["api"].logout()
            except Exception:
                pass
    if RESTART["at"]:
        os._exit(RESTART_EXIT)          # ⭐ 重啟鈕：已登出 ⇒ 結束，看門狗重開（⛔ os._exit：別等背景執行緒）


def session_contract():
    return CONN.get("contract")


if __name__ == "__main__":
    main()
