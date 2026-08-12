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

TP_POINTS = 100.0        # Benson 固定 ±100
SL_POINTS = 100.0
FEE_POINTS = 5.0         # 來回 NT$50 ÷ 每點 NT$10
PORT = 8770

SESSION_OPEN = pd.Timestamp("08:45").time()
WATCH_END = pd.Timestamp("09:30").time()
DAY_END = pd.Timestamp("13:45").time()
NIGHT_OPEN = pd.Timestamp("15:00").time()      # 夜盤 15:00 ~ 隔天 05:00
NIGHT_CLOSE = pd.Timestamp("05:00").time()
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
        self.minute_close[when.hour * 60 + when.minute] = price
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
                    ("date", "time", "dir", "entry", "exit", "_net", "_reason", "_source")}
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


def day_bars(day=None):
    """
    取某一天 08:45~09:45 的 1 分 K，附上那天的練習交易（給 K 線圖標記用）。

    盤中即時抓得到當天的 K 棒（實測 0 分鐘延遲），所以不必自己從 tick 拼；
    直接跟永豐要，資料跟大戶投同源，也就不會對不起來。
    """
    api = SESSION_REF.get("api")
    if api is None:
        return {"error": "尚未連線"}
    d = day or date.today()
    try:
        contract = getattr(api.Contracts.Futures, PRODUCT)[f"{PRODUCT}R1"]
        df = pd.DataFrame({**api.kbars(contract, start=str(d), end=str(d))})
        if df.empty:
            return {"date": str(d), "bars": [], "trades": []}
        df["ts"] = pd.to_datetime(df["ts"])
        g = df[(df["ts"].dt.time >= SESSION_OPEN)
               & (df["ts"].dt.time < DAY_END)].sort_values("ts")
        bars = to_timeframe(g, CHART_TF)
    except Exception as e:
        return {"error": str(e)[:120]}

    # 那天的練習交易（今天的用記憶體，過去的讀檔）
    if d == date.today():
        trades = list(TODAY_TRADES)
    else:
        f = TRADE_DIR / f"{d}.json"
        trades = json.loads(f.read_text(encoding="utf-8")) if f.exists() else []
    return {"date": str(d), "bars": bars, "trades": trades}


def to_timeframe(g, minutes):
    """
    把 1 分 K 合成 N 分 K，並以每根的「起始時間」標示（跟看盤軟體一致）。

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
    base = pd.Timestamp.combine(g["ts"].iloc[0].date(), SESSION_OPEN)
    g["slot"] = ((start_ts - base).dt.total_seconds() // (minutes * 60)).astype(int)
    out = []
    for _, blk in g.groupby("slot", sort=True):
        start = base + pd.Timedelta(minutes=minutes * int(blk["slot"].iloc[0]))
        out.append({"t": start.strftime("%H:%M"),
                    "o": float(blk["Open"].iloc[0]), "h": float(blk["High"].max()),
                    "l": float(blk["Low"].min()), "c": float(blk["Close"].iloc[-1]),
                    "v": float(blk["Volume"].sum())})
    return out


def traded_days():
    """有練習紀錄的日子，給重播用。"""
    if not TRADE_DIR.exists():
        return []
    return sorted([f.stem for f in TRADE_DIR.glob("*.json")
                   if json.loads(f.read_text(encoding="utf-8") or "[]")], reverse=True)


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
.app{max-width:1500px; margin:0 auto; padding:0 24px 40px}
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
.cwrap{resize:both; overflow:hidden; min-width:420px; min-height:220px;
  max-width:100%; border-radius:8px}
.cwrap svg{display:block; width:100%; height:100%}
.chint{font-size:10.5px; color:var(--faint); text-align:right; margin-top:2px}
.chead{display:flex; align-items:baseline; justify-content:space-between; margin-bottom:10px}
.cpx{font-size:44px; font-weight:700; font-family:var(--font-mono);
  font-variant-numeric:tabular-nums; line-height:1}
.cchg{font-size:17px; font-weight:600; font-family:var(--font-mono)}
.cdate{font-size:12.5px; color:var(--gold); cursor:pointer; border:1px solid var(--line);
  border-radius:20px; padding:3px 11px; background:var(--surface-2)}
.cdate:hover{border-color:var(--gold)}
.daypick{display:flex; gap:6px; flex-wrap:wrap; margin-top:10px}
.daypick button{font-size:11.5px; padding:5px 10px; border-radius:7px; cursor:pointer;
  background:var(--surface-2); color:var(--dim); border:1px solid var(--line)}
.daypick button.on{background:var(--gold-soft); color:var(--gold); border-color:transparent}
.mini{display:flex; flex-wrap:wrap; gap:0 20px; font-size:12.5px; color:var(--dim);
  font-family:var(--font-mono); margin-top:12px; padding-top:11px; border-top:1px solid var(--line)}
.mini b{color:var(--text); font-weight:600}
.btns{display:flex; gap:10px}
.btn{flex:1; padding:15px; border-radius:12px; cursor:pointer;
  font-family:var(--font-sans); font-size:16px; font-weight:700; letter-spacing:1px}
.btn.long{background:var(--up-soft); color:var(--up); border:1px solid rgba(238,90,84,.4)}
.btn.short{background:var(--down-soft); color:var(--down); border:1px solid rgba(52,179,126,.4)}
.btn.flat2{background:var(--surface-2); color:var(--text); border:1px solid var(--line)}
.btn.ghost{flex:0 0 auto; background:transparent; color:var(--faint); font-size:13px;
  font-weight:400; border:1px solid var(--line)}
.btn:active{transform:scale(.99)}
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
.dl{display:inline-block; margin-top:12px; font-size:12px; color:var(--gold); text-decoration:none}
</style></head><body><div class="app">
<div class="topbar">
  <div class="brand"><div class="mark">&#9702;</div>
    <div><div class="nm">早盤儀表板</div><div class="sub" id="sub">連線中…</div></div></div>
  <div class="clock"><div class="d" id="clk">--:--</div><div class="w" id="ph"></div></div>
</div>
<div id="wait" class="wait">等待資料…</div>
<div id="warn"></div>
<div class="cols"><div id="mkt"></div><div class="right"><div id="trade"></div><div id="stats"></div></div></div>
<div class="foot">只顯示當下的客觀盤面數字，不做預測、不給買賣訊號。<br>練習下單為模擬，不會送單到永豐。</div>
</div>
<script>
var WIN=7;
const f=(n,d=0)=>n==null?'—':Number(n).toFixed(d);
const sgn=v=>v>0?'up':v<0?'down':'flat';
const pm=(v,d=0)=>(v>0?'+':'')+f(v,d);

var lastMkt='', lastTrade='', lastStats='', lastWarn='', statsCache=null, statsAt=0;

async function tick(){
 let s; try{ s=await (await fetch('/api/state')).json(); }catch(e){ return; }
 // 成績每 5 秒抓一次就好 —— 它會讀所有紀錄檔，沒必要跟著報價跳
 if(Date.now()-statsAt>5000){
   statsAt=Date.now();
   fetch('/api/stats').then(r=>r.json()).then(x=>{statsCache=x;}).catch(()=>{});
 }
 const PH={recording:['記錄中','var(--up)'],live:['顯示中','var(--down)'],off:['夜盤','var(--faint)']};
 const ph=PH[s.phase]||PH.off, age=s.age_sec==null?99:s.age_sec;
 const dead=(s.conn&&s.conn.ok===false)||age>90;
 document.getElementById('clk').textContent=(s.clock||'').slice(0,5);
 document.getElementById('ph').innerHTML='<span style="color:'+ph[1]+'">'+ph[0]+'</span>';
 document.getElementById('sub').innerHTML='<span class="dot '+(dead?'dead':age>25?'stale':'')+'"></span>'+
   ((s.conn&&s.conn.contract_name)||'微台')+(s.replay?'・重播':'');

 const W=document.getElementById('wait');
 if(s.status!=='live'){ W.hidden=false; W.textContent=s.msg||'等待中…';
   setHTML('mkt',''); setHTML('trade',''); setHTML('stats',''); return; }
 W.hidden=true;

fetchBars(false);
 const c=s.chips;
 const warn = dead ? '<div class="alert"><b>報價已中斷</b>　畫面上的數字是舊的（'+
   (age==null?'尚未收到':age+' 秒前')+'）。程式每分鐘會自動重連。</div>' : '';
 setHTML('warn',warn);
 if(!paintChart(s)){
   // 還沒有 K 棒（例如夜盤）→ 退回簡單的數字卡
   lastMktFallback = '<div class="card"><div class="grid">'+
     '<div class="cell px"><div class="l">成交價</div><div class="v flat">'+f(c.price)+'</div></div>'+
     cell('最近 5 分鐘',pm(c.mom5)+' 點',sgn(c.mom5))+
     cell('最近 15 分鐘',pm(c.mom15)+' 點',sgn(c.mom15))+'</div></div>';
   if(document.getElementById('mkt').innerHTML!==lastMktFallback)
     document.getElementById('mkt').innerHTML=lastMktFallback;
 }
 setHTML('trade',tradeBox(s));
 setHTML('stats',statsBox(statsCache));
}

// 只有內容真的變了才動 DOM。否則每 0.5 秒重建一次，
// 使用者剛好在那一瞬間按下去，按鈕會連同事件一起被換掉 → 第一下沒反應。
function setHTML(id,html){
 const box={mkt:'lastMkt',trade:'lastTrade',stats:'lastStats',warn:'lastWarn'}[id];
 if(window[box]===html) return;
 window[box]=html;
 document.getElementById(id).innerHTML=html;
}


// ---------------- K 線圖（純 SVG，不用外部套件） ----------------
var barsCache=null, barsAt=0, viewDate='', pickOpen=false, lastMktFallback='';
// 圖的尺寸記在 localStorage —— 每次重繪都套回去，拖過的大小不會被洗掉
var CW=+(localStorage.getItem('panel-cw')||1040);
var CH=+(localStorage.getItem('panel-ch')||440);


function fetchBars(force){
 if(!force && Date.now()-barsAt<3000) return;
 barsAt=Date.now();
 fetch('/api/bars'+(viewDate?('?date='+viewDate):''))
  .then(r=>r.json()).then(x=>{ barsCache=x; }).catch(()=>{});
}

function chartCard(s){
 if(!barsCache||!barsCache.bars||!barsCache.bars.length) return '';
 const B=barsCache.bars, T=barsCache.trades||[], P=s.position;
 const live=!viewDate;
 const last=B[B.length-1], first=B[0];
 const px = live && s.chips && s.chips.price!=null ? s.chips.price : last.c;
 const chg = px-first.o, pct=(chg/first.o*100);

 // 價格範圍：K 棒 + 進出場 + 停利停損都要看得到
 let hi=Math.max(...B.map(b=>b.h)), lo=Math.min(...B.map(b=>b.l));
 T.forEach(t=>{ hi=Math.max(hi,t.entry,t.exit); lo=Math.min(lo,t.entry,t.exit); });
 if(P&&live){ hi=Math.max(hi,P.tp,P.sl); lo=Math.min(lo,P.tp,P.sl); }
 const pad=(hi-lo)*0.08||10; hi+=pad; lo-=pad;

 const W=1040,H=440,L=0,R=64,TOP=12,BOT=26;   // R 留給右側價格刻度
 const cw=(W-R)/B.length, bw=Math.max(3,Math.min(16,cw*0.6));
 const y=v=>TOP+(hi-v)/(hi-lo)*(H-TOP-BOT);
 const x=i=>L+i*cw+cw/2;

 let g='';
 // 他的下單時段 08:45~09:30 用底色標出來，一眼知道自己在哪個區間操作
 {
   let a=-1,b=-1;
   B.forEach((bar,i)=>{ if(bar.t>='08:45'&&bar.t<'09:30'){ if(a<0)a=i; b=i; } });
   if(a>=0){
     const x0=L+a*cw, x1=L+(b+1)*cw;
     g+='<rect x="'+x0.toFixed(1)+'" y="'+TOP+'" width="'+(x1-x0).toFixed(1)+
        '" height="'+(H-TOP-BOT)+'" fill="#E3A951" opacity=".05"/>'+
        '<text x="'+(x0+6)+'" y="'+(TOP+15)+'" fill="#5A616E" font-size="11">下單時段</text>';
   }
 }
 // 水平參考線
 for(let k=0;k<=5;k++){
   const v=lo+(hi-lo)*k/5, yy=y(v);
   g+='<line x1="0" y1="'+yy.toFixed(1)+'" x2="'+(W-R)+'" y2="'+yy.toFixed(1)+
      '" stroke="#262D39" stroke-width="1"/>'+
      '<text x="'+(W-R+8)+'" y="'+(yy+4).toFixed(1)+'" fill="#5A616E" font-size="12" '+
      'font-family="ui-monospace,monospace">'+v.toFixed(0)+'</text>';
 }
 // K 棒（台股慣例：紅漲綠跌）
 B.forEach((b,i)=>{
   const up=b.c>=b.o, col=up?'#EE5A54':'#34B37E', X=x(i);
   g+='<line x1="'+X.toFixed(1)+'" y1="'+y(b.h).toFixed(1)+'" x2="'+X.toFixed(1)+
      '" y2="'+y(b.l).toFixed(1)+'" stroke="'+col+'" stroke-width="1"/>';
   const yo=y(b.o), yc=y(b.c), top=Math.min(yo,yc), h=Math.max(1.2,Math.abs(yc-yo));
   g+='<rect x="'+(X-bw/2).toFixed(1)+'" y="'+top.toFixed(1)+'" width="'+bw.toFixed(1)+
      '" height="'+h.toFixed(1)+'" fill="'+col+'"/>';
 });
 // 停利／停損（持倉中才畫）
 if(P&&live){
   [[P.tp,'#EE5A54','停利'],[P.sl,'#34B37E','停損']].forEach(z=>{
     const yy=y(z[0]);
     if(yy<TOP||yy>H-BOT) return;
     g+='<line x1="0" y1="'+yy.toFixed(1)+'" x2="'+(W-R)+'" y2="'+yy.toFixed(1)+
        '" stroke="'+z[1]+'" stroke-width="1.2" stroke-dasharray="5 4" opacity=".8"/>'+
        '<text x="6" y="'+(yy-5).toFixed(1)+'" fill="'+z[1]+'" font-size="11.5">'+z[2]+' '+z[0].toFixed(0)+'</text>';
   });
 }
 // 進出場標記
 // 交易時間要對到「所屬的那根 5 分 K」，不是剛好等於開始時間
 const idxOf=t=>{ let r=-1; for(let i=0;i<B.length;i++){ if(B[i].t<=t) r=i; else break; } return r; };
 T.forEach(t=>{
   const i=idxOf(t.time); if(i<0) return;
   const X=x(i), Y=y(t.entry), long=t.dir==='long', col=long?'#EE5A54':'#34B37E';
   g+='<line x1="0" y1="'+Y.toFixed(1)+'" x2="'+(W-R)+'" y2="'+Y.toFixed(1)+
      '" stroke="'+col+'" stroke-width="1" stroke-dasharray="2 4" opacity=".55"/>';
   g+=long
     ? '<path d="M'+(X-8)+' '+(Y+20)+' L'+X+' '+(Y+6)+' L'+(X+8)+' '+(Y+20)+' Z" fill="'+col+'"/>'
     : '<path d="M'+(X-8)+' '+(Y-20)+' L'+X+' '+(Y-6)+' L'+(X+8)+' '+(Y-20)+' Z" fill="'+col+'"/>';
   // 出場點：用 _exit_time，沒有就標在最後
   const j=t._exit_time?idxOf(t._exit_time.slice(0,5)):-1;
   const XE=j>=0?x(j):x(B.length-1), YE=y(t.exit), w=t._net>0;
   const ec=w?'#EE5A54':'#34B37E';
   g+='<line x1="'+(XE-6)+'" y1="'+(YE-6)+'" x2="'+(XE+6)+'" y2="'+(YE+6)+'" stroke="'+ec+'" stroke-width="2.5"/>'+
      '<line x1="'+(XE-6)+'" y1="'+(YE+6)+'" x2="'+(XE+6)+'" y2="'+(YE-6)+'" stroke="'+ec+'" stroke-width="2.5"/>';
 });
 // 時間刻度
 ['08:45','09:00','09:30','10:00','10:30','11:00','11:30','12:00','12:30','13:00','13:30'].forEach(tm=>{
   const i=idxOf(tm); if(i<0) return;
   g+='<text x="'+x(i).toFixed(1)+'" y="'+(H-7)+'" fill="#5A616E" font-size="11.5" '+
      'text-anchor="middle" font-family="ui-monospace,monospace">'+tm+'</text>';
 });

 const c=s.chips||{};
 let mini='';
 if(live&&c.chg!=null){
   mini='<div class="mini">'+
     '<span>5分 <b class="'+sgn(c.mom5)+'">'+pm(c.mom5)+'</b></span>'+
     '<span>15分 <b class="'+sgn(c.mom15)+'">'+pm(c.mom15)+'</b></span>'+
     '<span>跳空 <b>'+pm(c.gap)+'</b></span>'+
     '<span>震幅 <b>'+f(c.rng)+'</b></span>'+
     '<span>位階 <b>'+f(c.pos*100)+'%</b></span>'+
     '<span>量能 <b>'+f(c.vol_ratio,2)+'倍</b></span>'+
     '<span>買/賣 <b>'+f(c.bid)+' / '+f(c.ask)+'</b></span></div>';
 }
 let pick='';
 if(pickOpen){
   pick='<div class="daypick"><button class="'+(viewDate?'':'on')+'" data-day="">今天（即時）</button>';
   (barsCache.days||[]).forEach(d=>{
     if(d===new Date().toISOString().slice(0,10)) return;
     pick+='<button class="'+(viewDate===d?'on':'')+'" data-day="'+d+'">'+d.slice(5)+'</button>';
   });
   pick+='</div>';
 }
 const sumTxt=T.length?('　'+T.length+' 筆練習 '+pm(T.reduce((a,t)=>a+t._net,0))+' 點'):'';
 return {
   head:'<div><span class="cpx '+sgn(chg)+'">'+f(px)+'</span>'+
        ' <span class="cchg '+sgn(chg)+'">'+pm(chg)+' ('+pm(pct,2)+'%)</span></div>'+
        '<span class="cdate" data-pick="1">5 分 K・'+(viewDate?viewDate.slice(5):'今天')+sumTxt+' ▾</span>',
   svg:g, vb:'0 0 '+W+' '+H, pick:pick, mini:mini
 };
}

/* 圖的外框只建一次。之後只換 <svg> 裡面的線條與標題文字 ——
   若整塊重繪，使用者拖曳出來的尺寸會被洗掉，而且畫面會閃。 */
function paintChart(s){
 const d=chartCard(s);
 const box=document.getElementById('mkt');
 if(!d){ return false; }
 if(!document.getElementById('cwrap')){
   box.innerHTML='<div class="card chart">'+
     '<div class="chead" id="chead"></div>'+
     '<div class="cwrap" id="cwrap" style="width:'+CW+'px;height:'+CH+'px">'+
     '<svg id="csvg" preserveAspectRatio="none"></svg></div>'+
     '<div class="chint">右下角可拖曳縮放</div>'+
     '<div id="cpick"></div><div id="cmini"></div></div>';
   watchResize();
 }
 const hd=document.getElementById('chead');
 if(hd.innerHTML!==d.head) hd.innerHTML=d.head;
 const sv=document.getElementById('csvg');
 if(sv.getAttribute('viewBox')!==d.vb) sv.setAttribute('viewBox',d.vb);
 if(sv.innerHTML!==d.svg) sv.innerHTML=d.svg;
 const pk=document.getElementById('cpick');
 if(pk.innerHTML!==d.pick) pk.innerHTML=d.pick;
 const mn=document.getElementById('cmini');
 if(mn.innerHTML!==d.mini) mn.innerHTML=d.mini;
 return true;
}

function saveSize(){
 const w=document.getElementById('cwrap');
 if(!w) return;
 const r=w.getBoundingClientRect();
 if(r.width<50||r.height<50) return;         // 視窗隱藏時量到 0，不要存進去
 CW=Math.round(r.width); CH=Math.round(r.height);
 localStorage.setItem('panel-cw',CW); localStorage.setItem('panel-ch',CH);
}

function watchResize(){
 const w=document.getElementById('cwrap');
 if(!w||w._watched) return;
 w._watched=true;
 // 兩道保險：ResizeObserver 為主，放開滑鼠時再存一次。
 // 有些環境（例如視窗沒顯示）ResizeObserver 不會觸發，光靠它會漏。
 let t=null;
 try{
   new ResizeObserver(function(){
     clearTimeout(t); t=setTimeout(saveSize,250);
   }).observe(w);
 }catch(e){}
 w.addEventListener('mouseup',function(){ setTimeout(saveSize,60); });
 window.addEventListener('mouseup',function(){ setTimeout(saveSize,60); });
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
      '<button class="btn ghost" data-act="undo">取消</button></div>';
 } else {
   h+='<div class="btns"><button class="btn long" data-act="long">&#9650; 做多</button>'+
      '<button class="btn short" data-act="short">&#9660; 做空</button></div>';
 }
 if(T.length){
   let sum=0; T.forEach(t=>sum+=t._net);
   h+='<div class="list">';
   T.slice().reverse().forEach(t=>h+=row(t));
   h+='</div><div class="plimit" style="margin:12px 0 0">今天 '+T.length+' 筆　合計 '+pm(sum)+' 點</div>';
 }
 return h+'</div>';
}
function row(t){
 const rs={tp:'停利',sl:'停損',manual:'手動',close:'收盤'}[t._reason]||'';
 return '<div class="trade"><div class="tr-top">'+
  '<span class="tr-date">'+(t.date?t.date.slice(5):'')+'</span>'+
  '<span class="dir '+(t.dir==='long'?'l':'s')+'">'+(t.dir==='long'?'▲ 多':'▼ 空')+'</span>'+
  '<span class="tr-px">'+t.entry+'<span class="arrow">→</span>'+t.exit+
  (rs?' <span class="tag">'+rs+'</span>':'')+(t._source==='app'?' <span class="tag">App</span>':'')+'</span>'+
  '<span class="tr-res '+(t._net>0?'r-win':'r-loss')+'">'+pm(t._net)+'</span></div></div>';
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
   ST.recent.forEach(t=>h+=row(t));
   h+='</div><a class="dl" href="/api/export" download>下載練習紀錄（可匯入 App）</a>';
 }
 return h+'</div>';
}
// 事件委派：掛在 document 上，就算某一區重繪也不會掉事件
document.addEventListener('click', function(e){
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
document.addEventListener('click', function(e){
 if(e.target.closest('[data-pick]')){ pickOpen=!pickOpen; tick(); return; }
 const d=e.target.closest('[data-day]');
 if(d){ viewDate=d.getAttribute('data-day'); pickOpen=false; fetchBars(true);
        setTimeout(tick,300); return; }
 const b=e.target.closest('[data-win]');
 if(!b) return;
 WIN=parseInt(b.getAttribute('data-win'))||0;
 lastStats=''; tick();
});
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
            ok, msg = open_position(d, price, body.get("note", ""))
            return self._json(200 if ok else 409, {"ok": ok, "msg": msg})

        if self.path == "/api/close":
            r = close_position(price, "manual")
            return self._json(200, {"ok": r is not None,
                                    "msg": "已平倉" if r else "目前沒有持倉"})

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
            want = None
            for kv in q.split("&"):
                if kv.startswith("date="):
                    try:
                        want = datetime.strptime(kv[5:], "%Y-%m-%d").date()
                    except Exception:
                        want = None
            out = day_bars(want)
            out["days"] = traded_days()
            return self._json(200, out)
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
    with state_lock:
        STATE.update({
            "period": hist.period, "n_days_total": hist.n_days,
            "clock": now_time.strftime("%H:%M:%S"), "replay": replay,
            "phase": phase,
            "position": (dict(POSITION, float_pts=round(
                (1 if POSITION["dir"] == "long" else -1)
                * ((today_state.price or POSITION["entry"]) - POSITION["entry"]), 1))
                if POSITION else None),
            "today_trades": list(TODAY_TRADES),
            "age_sec": 0 if replay else age,
            "conn": CONN.copy(),
        })
        if today_state.price is None:
            STATE.update({"status": "waiting", "msg": "等待第一筆成交…"})
            return

        # 夜盤：沒有日盤開高低，也沒有對照樣本 —— 只給價格與動能
        if feats is None:
            STATE.update({
                "status": "live",
                "chips": {"price": today_state.price,
                          "bid": today_state.bid, "ask": today_state.ask,
                          "is_mid": today_state.price_is_mid,
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
                      "period": hist.period, "n_days_total": hist.n_days})
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
            in_day = SESSION_OPEN <= t < DAY_END
            in_night = t >= NIGHT_OPEN or t < NIGHT_CLOSE
            market_open = in_day or in_night

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

            if st is None or st.price is None:
                with state_lock:
                    STATE.update({"status": "waiting", "clock": now.strftime("%H:%M:%S"),
                                  "msg": "等待第一筆成交…"})
            elif session["date"] == today and SESSION_OPEN <= t <= WATCH_END:
                # 下單時段：即時顯示 + 記錄資料
                update_state(hist, st, vol_ref, t, phase="recording")
            elif session["date"] == today and WATCH_END < t < DAY_END:
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
