r"""
早盤即時勝率面板（08:45~09:30）— 本機網頁版
=============================================================================
即時接收台指期價格跳動，算出「現在這一刻進場」的歷史同情境勝率。

做法：
  1. 即時算出當下盤面狀態（最近 5/15 分鐘動能、相對開盤、跳空、震幅、位階、量能）
  2. 到 241 天歷史裡，找「同一分鐘、狀態最像」的時刻
  3. 看那些時刻後來到 13:45 為止，做多／做空各是贏是輸

畫面上每個勝率都會標三件事：
  - 用了幾天的樣本
  - 95% 信賴區間
  - 對比「同期基準」多給了幾 %  ← 這才是情境真正提供的資訊

【兩個必要的修正，缺一數字就會虛高】
1. 一天只算一筆：每個歷史日只取「最像現在」的那一分鐘。
   若讓同一天貢獻多筆，等於假設你能在同一波行情裡反覆進場，
   實測會把勝率灌水到 15 個百分點（60% vs 45%）。
2. 去趨勢：樣本期間台指期漲 81.7%，日盤中位漂移約 +25 點。
   在 ±100 點的框架下這會讓做多勝率虛胖，所以outcome 已扣掉這段漂移。
   剩下的才是「當下動能會不會延續」。

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
MATRIX = HERE / "intraday.csv"
LOG_DIR = HERE / "morning_logs"
PORT = 8770

SESSION_OPEN = pd.Timestamp("08:45").time()
WATCH_END = pd.Timestamp("09:30").time()
DAY_END = pd.Timestamp("13:45").time()

#                mom5  mom15 ret_open gap  rng  pos  vol_ratio
FEATURES = ["mom5", "mom15", "ret_open", "gap", "rng", "pos", "vol_ratio"]
FEATURE_WEIGHT = np.array([3.0, 2.0, 1.0, 0.8, 0.8, 1.5, 0.8])
# 「當下的趨勢」是 Benson 要的重點 → mom5 / mom15 權重最高
MINUTE_WINDOW = 3          # 只跟前後 3 分鐘的歷史時刻比
K_NEIGHBOURS = 80

state_lock = threading.Lock()
STATE = {"status": "starting", "msg": "啟動中…"}


# ---------------------------------------------------------------- 歷史矩陣

class History:
    def __init__(self):
        df = pd.read_csv(MATRIX)
        df["min_idx"] = df["minute"].map(lambda s: int(s[:2]) * 60 + int(s[3:]))
        self.df = df
        self.n_days = df["date"].nunique()
        self.period = f"{df['date'].min()} ~ {df['date'].max()}"

    def query(self, min_idx, feats):
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

        by_day = per_day.set_index("date")[["win_long_dt", "win_short_dt", "net_long_dt", "net_short_dt"]]
        n_days = len(by_day)
        if n_days < 15:
            return None

        def summarise(col_win, col_net, base_win):
            p = float(by_day[col_win].mean())
            se = float(by_day[col_win].std(ddof=1) / np.sqrt(n_days)) if n_days > 1 else 0.5
            lo, hi = max(0.0, p - 1.96 * se), min(1.0, p + 1.96 * se)
            return {
                "win": round(p * 100, 1),
                "ci": [round(lo * 100, 1), round(hi * 100, 1)],
                "edge": round((p - base_win) * 100, 1),
                "net": round(float(by_day[col_net].mean()), 1),
                # 信賴區間整段都在 50% 同一側才算有訊號
                "meaningful": bool(lo > 0.5 or hi < 0.5),
            }

        base = pool.groupby("date")[["win_long_dt", "win_short_dt"]].mean()
        base_long = float(base["win_long_dt"].mean())
        base_short = float(base["win_short_dt"].mean())

        return {
            "n_days": n_days,
            "n_points": n_days,
            "base_long": round(base_long * 100, 1),
            "base_short": round(base_short * 100, 1),
            "long": summarise("win_long_dt", "net_long_dt", base_long),
            "short": summarise("win_short_dt", "net_short_dt", base_short),
        }


# ---------------------------------------------------------------- 今日盤面

class Today:
    def __init__(self, prev_close):
        self.prev_close = prev_close
        self.open = None
        self.high = None
        self.low = None
        self.price = None
        self.vol = 0
        self.ticks = 0
        self.updated = None
        self.minute_close = {}     # 分鐘索引 → 該分鐘最後成交價（算 mom5 / mom15 用）

    def feed(self, price, volume, when):
        if self.open is None:
            self.open = price
            self.high = self.low = price
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.price = price
        self.vol += volume
        self.ticks += 1
        self.updated = when
        self.minute_close[when.hour * 60 + when.minute] = price

    def _price_ago(self, now_idx, minutes):
        """N 分鐘前的價格；那一分鐘沒成交就往更早找，找不到就用開盤價。"""
        for m in range(now_idx - minutes, now_idx - minutes - 10, -1):
            if m in self.minute_close:
                return self.minute_close[m]
        return self.open

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
            "pos": (self.price - self.low) / rng if rng > 0 else 0.5,
            "vol_ratio": (self.vol / vol_ref) if vol_ref else 1.0,
        }


# ---------------------------------------------------------------- 網頁

PAGE = r"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>早盤勝率面板</title><style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,"Segoe UI","Microsoft JhengHei",sans-serif;
background:#0d1117;color:#e6edf3;padding:18px;line-height:1.5}
.wrap{max-width:940px;margin:0 auto}
h1{font-size:17px;font-weight:600;margin-bottom:2px}
.sub{font-size:12px;color:#8b949e;margin-bottom:16px}
.bar{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px}
.chip{background:#161b22;border:1px solid #30363d;border-radius:7px;padding:9px 13px;flex:1;min-width:112px}
.chip .l{font-size:11px;color:#8b949e}
.chip .v{font-size:19px;font-weight:600;margin-top:2px;font-variant-numeric:tabular-nums}
.up{color:#3fb950}.down{color:#f85149}.flat{color:#e6edf3}
.cards{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:16px}
@media(max-width:640px){.cards{grid-template-columns:1fr}}
.card{background:#161b22;border:1px solid #30363d;border-radius:11px;padding:17px}
.card h2{font-size:13px;color:#8b949e;font-weight:500;margin-bottom:9px}
.big{font-size:44px;font-weight:700;font-variant-numeric:tabular-nums;line-height:1}
.big span{font-size:19px;color:#8b949e;font-weight:400}
.ci{font-size:12px;color:#8b949e;margin-top:7px;font-variant-numeric:tabular-nums}
.edge{margin-top:11px;padding-top:11px;border-top:1px solid #30363d;font-size:13px}
.edge b{font-size:16px;font-variant-numeric:tabular-nums}
.tag{display:inline-block;font-size:11px;padding:2px 8px;border-radius:20px;margin-top:9px}
.tag.no{background:#21262d;color:#8b949e;border:1px solid #30363d}
.tag.yes{background:#1f6feb22;color:#58a6ff;border:1px solid #1f6feb66}
.note{background:#161b22;border:1px solid #30363d;border-left:3px solid #d29922;
border-radius:7px;padding:13px 15px;font-size:12.5px;color:#c9d1d9;margin-bottom:12px}
.note b{color:#e6edf3}
.foot{font-size:11.5px;color:#6e7681;text-align:center;margin-top:18px;line-height:1.7}
.wait{text-align:center;padding:56px 20px;color:#8b949e}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:#3fb950;margin-right:5px}
.stale{background:#d29922}
</style></head><body><div class="wrap">
<h1>台指期 早盤勝率面板</h1>
<div class="sub" id="sub">連線中…</div>
<div id="body"><div class="wait">等待資料…</div></div>
<div class="foot">
歷史統計，不是預測，也不是投資建議。進場與否由你決定。<br>
數字已扣掉大盤漂移（去趨勢），反映的是「當下動能」而非「那一年大盤在漲」。<br>
每個歷史日只取最相似的一刻計為一筆，對應你每天只下一單的實況。
</div></div>
<script>
const f=(n,d=0)=>n==null?'—':n.toFixed(d);
async function tick(){
 let s; try{ s=await (await fetch('/api/state')).json(); }catch(e){ return; }
 const sub=document.getElementById('sub'), body=document.getElementById('body');
 const age=s.age_sec==null?99:s.age_sec;
 sub.innerHTML='<span class="dot '+(age>15?'stale':'')+'"></span>'+
   (s.replay?'重播模式 '+s.replay+' · ':'')+
   '樣本 '+(s.period||'')+'（'+(s.n_days_total||0)+' 天） · 更新 '+(s.clock||'');
 if(s.status!=='live'){ body.innerHTML='<div class="wait">'+(s.msg||'等待中…')+'</div>'; return; }
 const c=s.chips, r=s.result;
 let h='<div class="bar">'+
  chip('現價',f(c.price),c.chg>0?'up':c.chg<0?'down':'flat')+
  chip('最近5分鐘',(c.mom5>0?'+':'')+f(c.mom5)+' 點',c.mom5>0?'up':c.mom5<0?'down':'flat')+
  chip('最近15分鐘',(c.mom15>0?'+':'')+f(c.mom15)+' 點',c.mom15>0?'up':c.mom15<0?'down':'flat')+
  chip('對開盤',(c.chg>0?'+':'')+f(c.chg)+' 點',c.chg>0?'up':c.chg<0?'down':'flat')+
  chip('跳空',(c.gap>0?'+':'')+f(c.gap)+' 點','flat')+
  chip('今日震幅',f(c.rng)+' 點','flat')+
  chip('位階',f(c.pos*100)+'%','flat')+
  chip('量能',f(c.vol_ratio,2)+' 倍','flat')+'</div>';
 if(!r){ h+='<div class="note">樣本不足，無法比對。</div>'; body.innerHTML=h; return; }
 h+='<div class="cards">'+card('做多',r.long,'#3fb950')+card('做空',r.short,'#f85149')+'</div>';
 h+='<div class="note"><b>怎麼讀：</b>大數字是歷史上「同一時段、盤面長得像」的日子裡，'+
    '做多／做空賺錢的比例（共 '+r.n_days+' 天）。'+
    '<b>「vs 基準」才是這個情境真正多給你的資訊</b> —— '+
    '基準是同時段所有日子的平均（多 '+r.base_long+'% / 空 '+r.base_short+'%）。'+
    '信賴區間若跨過 50%，代表這個數字跟丟銅板分不出來。</div>';
 body.innerHTML=h;
}
function chip(l,v,cls){return '<div class="chip"><div class="l">'+l+'</div><div class="v '+cls+'">'+v+'</div></div>';}
function card(name,d,col){
 const sig=d.meaningful;
 return '<div class="card"><h2>'+name+'　現在進場的歷史勝率</h2>'+
  '<div class="big" style="color:'+col+'">'+d.win.toFixed(1)+'<span>%</span></div>'+
  '<div class="ci">95% 信賴區間 '+d.ci[0].toFixed(1)+'% ~ '+d.ci[1].toFixed(1)+'%</div>'+
  '<div class="edge">vs 基準 <b style="color:'+(d.edge>0?'#3fb950':'#f85149')+'">'+
  (d.edge>0?'+':'')+d.edge.toFixed(1)+'%</b>　平均 '+(d.net>0?'+':'')+d.net.toFixed(1)+' 點/筆</div>'+
  '<span class="tag '+(sig?'yes':'no')+'">'+(sig?'★ 區間未跨 50%':'— 與丟銅板無法區分')+'</span></div>';
}
tick(); setInterval(tick,2000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
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
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


# ---------------------------------------------------------------- 主流程

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


def update_state(hist, today_state, vol_ref, now_time, replay=None):
    min_idx = now_time.hour * 60 + now_time.minute
    feats = today_state.features(vol_ref, min_idx)
    with state_lock:
        STATE.update({
            "period": hist.period, "n_days_total": hist.n_days,
            "clock": now_time.strftime("%H:%M:%S"), "replay": replay,
            "age_sec": 0 if today_state.updated else None,
        })
        if feats is None:
            STATE.update({"status": "waiting", "msg": "等待 08:45 開盤第一筆成交…"})
            return
        result = hist.query(min_idx, feats)
        STATE.update({
            "status": "live",
            "chips": {
                "price": today_state.price, "chg": feats["ret_open"], "gap": feats["gap"],
                "rng": feats["rng"], "pos": feats["pos"], "vol_ratio": feats["vol_ratio"],
                "mom5": feats["mom5"], "mom15": feats["mom15"],
            },
            "result": result,
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
    t = Today(prev_close)
    print(f"重播 {day_str}（每秒 = 盤中 1 分鐘，共 {len(g)} 分鐘）")
    for _, row in g.iterrows():
        t.feed(float(row["Close"]), int(row["Volume"]), row["ts"])
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
    from shioaji import Exchange, TickFOPv1
    from _config import get_credentials

    api_key, secret = get_credentials()
    api = sj.Shioaji()
    api.login(api_key=api_key, secret_key=secret)
    contract = api.Contracts.Futures.TXF.TXFR1

    today = date.today()
    prev_close = prev_trading_close(api, contract, today)
    print(f"上一交易日日盤收盤：{prev_close}")

    # 量能基準：歷史同時段累計量的中位數（依分鐘查表）
    vol_ref_by_min = hist.df.groupby("min_idx")["vol_cum"].median().to_dict()

    t_state = Today(prev_close)

    def on_tick(exchange: Exchange, tick: TickFOPv1):
        ts = tick.datetime
        if SESSION_OPEN <= ts.time() <= WATCH_END:
            t_state.feed(float(tick.close), int(tick.volume), ts)

    api.set_on_tick_fop_v1_callback(on_tick)
    api.quote.subscribe(contract, quote_type=sj.constant.QuoteType.Tick,
                        version=sj.constant.QuoteVersion.v1)

    with state_lock:
        STATE.update({"status": "waiting", "msg": "已連線，等待 08:45 開盤…",
                      "period": hist.period, "n_days_total": hist.n_days})

    print("已連線。等待 08:45~09:30 監看窗…（Ctrl+C 結束）")
    try:
        while True:
            now = datetime.now()
            if SESSION_OPEN <= now.time() <= WATCH_END:
                min_idx = now.hour * 60 + now.minute
                update_state(hist, t_state, vol_ref_by_min.get(min_idx, 1.0), now.time())
            elif now.time() > WATCH_END:
                with state_lock:
                    STATE.update({"status": "waiting",
                                  "msg": "今天的監看窗（08:45~09:30）已結束。"})
                if t_state.open is not None:
                    save_today(t_state)
                    print("已存檔今日開盤資料。結束。")
                    break
            else:
                with state_lock:
                    STATE.update({"status": "waiting", "msg": "已連線，等待 08:45 開盤…",
                                  "clock": now.strftime("%H:%M:%S")})
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        api.logout()


def save_today(t):
    LOG_DIR.mkdir(exist_ok=True)
    rec = {"date": str(date.today()), "open": t.open, "high": t.high, "low": t.low,
           "close": t.price, "volume": t.vol, "prev_close": t.prev_close,
           "ticks": t.ticks, "saved_at": datetime.now().isoformat(timespec="seconds")}
    (LOG_DIR / f"{date.today()}-live.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
