r"""
早盤趨勢面板（全時段顯示，08:45~09:30 記錄）— 本機網頁版
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
NIGHT_OPEN = pd.Timestamp("15:00").time()      # 夜盤 15:00 ~ 隔天 05:00
NIGHT_CLOSE = pd.Timestamp("05:00").time()

# 全部用「幾倍的當時日常波動」比對，不用絕對點數 ——
# 台指期 2020 年 12,400 點、2026 年 45,000 點，同樣 40 點的意義差了三倍以上。
FEATURES = ["mom5_n", "mom15_n", "ret_open_n", "gap_n", "rng_n", "pos", "vol_ratio"]
FEATURE_WEIGHT = np.array([3.0, 2.0, 1.0, 0.8, 0.8, 1.5, 0.8])
# 「當下的趨勢」是 Benson 要的重點 → mom5 / mom15 權重最高
MINUTE_WINDOW = 3          # 只跟前後 3 分鐘的歷史時刻比
K_NEIGHBOURS = 150   # 樣本從 242 天增到 1453 天，取更多鄰居仍然比以前更像

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

        idx = horizons[10]["prob_up"]
        if idx >= 60:
            direction, strength = "偏漲", "強"
        elif idx >= 55:
            direction, strength = "偏漲", "弱"
        elif idx <= 40:
            direction, strength = "偏跌", "強"
        elif idx <= 45:
            direction, strength = "偏跌", "弱"
        else:
            direction, strength = "沒方向", ""

        return {
            "n_days": n_days,
            "index": idx,
            "direction": direction,
            "strength": strength,
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
        self.vol = 0
        self.ticks = 0
        self.updated = None
        self.last_recv = None      # 最後一次真的收到 tick 的本機時間（判斷斷線用）
        self.minute_close = {}     # 分鐘索引 → 該分鐘最後成交價（算 mom5 / mom15 用）

    def feed(self, price, volume, when, in_session):
        """
        任何時候都記價格（面板要一直顯示現價與動能）；
        只有日盤（08:45~13:45）才累積開高低與成交量 —— 那些欄位的定義是日盤專用的。
        """
        self.price = price
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


# ---------------------------------------------------------------- 網頁

PAGE = r"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>早盤趨勢面板</title><style>
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
.trend{background:#161b22;border:1px solid #30363d;border-radius:11px;padding:19px 21px;
margin-bottom:14px;display:flex;gap:24px;align-items:center;flex-wrap:wrap}
.tleft{flex:1;min-width:260px}
.tlabel{font-size:13px;color:#8b949e;margin-bottom:5px}
.tval{font-size:38px;font-weight:700;font-variant-numeric:tabular-nums;line-height:1.1}
.tval span{font-size:16px;color:#8b949e;font-weight:400}
.tval em{font-size:17px;font-style:normal;font-weight:600}
.tmeta{font-size:11.5px;color:#8b949e;margin-top:7px}
.gauge{flex:1;min-width:230px}
.gtrack{position:relative;height:9px;background:#21262d;border-radius:5px}
.gfill{position:absolute;top:0;height:100%;opacity:.35;border-radius:5px}
.gmid{position:absolute;left:50%;top:-3px;width:1px;height:15px;background:#484f58}
.gpin{position:absolute;top:-3px;width:3px;height:15px;border-radius:2px;margin-left:-1px}
.glbl{display:flex;justify-content:space-between;font-size:10.5px;color:#6e7681;margin-top:6px}
.ht{width:100%;border-collapse:collapse;margin-top:15px;font-size:12.5px;
font-variant-numeric:tabular-nums}
.ht th{text-align:left;color:#8b949e;font-weight:500;padding:6px 8px;
border-bottom:1px solid #30363d;font-size:11.5px}
.ht td{padding:7px 8px;border-bottom:1px solid #21262d;color:#c9d1d9}
.ht tr:last-child td{border-bottom:none}
.split{margin-top:12px}
.sbar{display:flex;height:7px;border-radius:4px;overflow:hidden;background:#21262d}
.sbar i{display:block;height:100%}
.slbl{display:flex;justify-content:space-between;font-size:11px;color:#8b949e;margin-top:5px}
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
.dead{background:#f85149}
.alert{background:#f8514915;border:1px solid #f85149;border-left:4px solid #f85149;
border-radius:8px;padding:13px 16px;font-size:13px;color:#ffc1bd;margin-bottom:14px;line-height:1.65}
.alert b{color:#ff9d97}
</style></head><body><div class="wrap">
<h1>台指期 早盤趨勢面板</h1>
<div class="sub" id="sub">連線中…</div>
<div id="body"><div class="wait">等待資料…</div></div>
<div class="foot">
歷史統計，不是預測，也不是投資建議。進場與否由你決定。<br>
已扣掉大盤漂移，反映的是「當下動能」而非「那一年大盤在漲」；每個歷史日只取最相似的一刻計為一筆。
</div></div>
<script>
const f=(n,d=0)=>n==null?'—':n.toFixed(d);
async function tick(){
 let s; try{ s=await (await fetch('/api/state')).json(); }catch(e){ return; }
 const sub=document.getElementById('sub'), body=document.getElementById('body');
 const age=s.age_sec==null?99:s.age_sec;
 const PH={recording:['● 記錄中','#f85149','08:45~09:30 下單時段，資料會存檔'],
           live:['◐ 顯示中','#3fb950','日盤，照常顯示但不記錄'],
           off:['○ 夜盤','#8b949e','只顯示價格與動能']};
 const ph=PH[s.phase]||PH.off;
 const dead=(s.conn&&s.conn.ok===false)||age>90;
 sub.innerHTML='<span class="dot '+(dead?'dead':age>25?'stale':'')+'"></span>'+
   (s.replay?'重播模式 '+s.replay+' · ':'')+
   '<b style="color:'+ph[1]+'">'+ph[0]+'</b>（'+ph[2]+'） · 樣本 '+(s.n_days_total||0)+' 天 · '+(s.clock||'');
 let warn='';
 if(dead){
   const c=s.conn||{};
   warn='<div class="alert"><b>⚠ 報價已中斷 —— 畫面上的數字是舊的，不要當作現況</b><br>'+
     '最後收到報價：'+(age==null?'尚未收到':age+' 秒前')+
     (c.since?'　·　中斷起始 '+c.since:'')+
     (c.retries?'　·　已重試 '+c.retries+' 次':'')+
     (c.last_error?'<br>錯誤：'+c.last_error:'')+
     '<br>常見原因：切換 VPN 或網路中斷。程式每分鐘會自動重連。</div>';
 }
 if(s.status!=='live'){ body.innerHTML=warn+'<div class="wait">'+(s.msg||'等待中…')+'</div>'; return; }
 const c=s.chips, r=s.result;
 const dir=v=>v>0?'up':v<0?'down':'flat';
 let h=warn+'<div class="bar">'+
  chip('現價',f(c.price),'flat')+
  chip('最近5分鐘',(c.mom5>0?'+':'')+f(c.mom5)+' 點',dir(c.mom5))+
  chip('最近15分鐘',(c.mom15>0?'+':'')+f(c.mom15)+' 點',dir(c.mom15))+
  (c.chg==null?'':
   chip('對開盤',(c.chg>0?'+':'')+f(c.chg)+' 點',dir(c.chg))+
   chip('跳空',(c.gap>0?'+':'')+f(c.gap)+' 點','flat')+
   chip('今日震幅',f(c.rng)+' 點','flat')+
   chip('位階',f(c.pos*100)+'%','flat')+
   chip('量能',f(c.vol_ratio,2)+' 倍','flat'))+'</div>';
 if(!r){ h+='<div class="note">'+(s.msg||'目前沒有可比對的歷史樣本。')+'</div>';
         body.innerHTML=h; return; }
 h+=trendCard(r);
 h+='<div class="note"><b>怎麼讀：</b>拿現在的盤面，去歷史裡找 '+r.n_days+
    ' 個「同一時段、走勢長得最像」的日子（每天只取最像的一刻），看那些日子接下來怎麼走。'+
    '<br><b>這是方向傾向，不是賺賠預測。</b>實測過：方向猜對大約六成四，'+
    '但照這個方向下單、九種停損停利組合全部是賠的 —— 因為猜錯的時候賠得比較多。'+
    '請當看盤參考，不要當進場訊號。</div>';
 body.innerHTML=h;
}
function chip(l,v,cls){return '<div class="chip"><div class="l">'+l+'</div><div class="v '+cls+'">'+v+'</div></div>';}
function trendCard(r){
 if(!r) return '';
 const i=r.index;
 const col = i>=60?'#3fb950' : i<=40?'#f85149' : '#8b949e';
 const arrow = r.direction==='偏漲'?'▲' : r.direction==='偏跌'?'▼' : '—';
 let rows='';
 r.horizons.forEach(function(z){
   const c2 = z.prob_up>=55?'#3fb950' : z.prob_up<=45?'#f85149' : '#8b949e';
   rows+='<tr><td>'+z.h+' 分鐘後</td>'+
     '<td style="color:'+c2+';font-weight:600">'+z.prob_up.toFixed(0)+'%</td>'+
     '<td>'+z.ci[0].toFixed(0)+'~'+z.ci[1].toFixed(0)+'%</td>'+
     '<td style="color:'+c2+'">'+(z.move>0?'+':'')+z.move.toFixed(0)+' 點</td>'+
     '<td>'+(z.q1>0?'+':'')+z.q1.toFixed(0)+' ~ '+(z.q3>0?'+':'')+z.q3.toFixed(0)+'</td>'+
     '<td>'+(z.meaningful?'<b style="color:#58a6ff">★</b>':'—')+'</td></tr>';
 });
 return '<div class="trend"><div class="tleft">'+
  '<div class="tlabel">現在的趨勢</div>'+
  '<div class="tval" style="color:'+col+'">'+arrow+' '+r.direction+
  (r.strength?'<em style="color:'+col+'">（'+r.strength+'）</em>':'')+'</div>'+
  '<div class="tmeta">趨勢指數 <b style="color:'+col+'">'+i+'</b> / 100　·　樣本 '+r.n_days+' 天　·　'+
  (r.consistent?'<b style="color:#58a6ff">5/10/15 分鐘都指同一邊</b>':'<span>各時間長度方向不一致</span>')+
  '</div></div>'+
  '<div class="gauge"><div class="gtrack"><div class="gfill" style="left:'+Math.min(i,50)+
  '%;width:'+Math.abs(i-50)+'%;background:'+col+'"></div><div class="gmid"></div>'+
  '<div class="gpin" style="left:'+i+'%;background:'+col+'"></div></div>'+
  '<div class="glbl"><span>0 全跌</span><span>50 沒方向</span><span>100 全漲</span></div>'+
  '</div>'+
  '<table class="ht"><tr><th>時間</th><th>上漲機率</th><th>95% 區間</th>'+
  '<th>預期變動</th><th>常見範圍</th><th>可信</th></tr>'+rows+'</table>'+
  '</div>';
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
    age = None if today_state.last_recv is None else round(time.time() - today_state.last_recv)
    with state_lock:
        STATE.update({
            "period": hist.period, "n_days_total": hist.n_days,
            "clock": now_time.strftime("%H:%M:%S"), "replay": replay,
            "phase": phase,
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
    from shioaji import Exchange, TickFOPv1
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

    def connect():
        """（重新）登入並訂閱。開盤前、以及偵測到斷線時都會呼叫。"""
        if session["api"] is not None:
            try:
                session["api"].logout()
            except Exception:
                pass
        api = sj.Shioaji()
        api.login(api_key=api_key, secret_key=secret)
        contract = api.Contracts.Futures.TXF.TXFR1
        api.set_on_tick_fop_v1_callback(on_tick)
        api.quote.subscribe(contract, quote_type=sj.constant.QuoteType.Tick,
                            version=sj.constant.QuoteVersion.v1)
        session["api"], session["contract"] = api, contract
        CONN.update({"ok": True, "since": None, "retries": 0, "last_error": None})
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

    def start_day(today):
        try:
            api, contract = connect()
            prev_close = prev_trading_close(api, contract, today)
        except Exception as e:
            CONN.update({"ok": False, "last_error": str(e)[:200],
                         "since": datetime.now().strftime("%H:%M:%S")})
            print(f"[{today}] 開盤前連線失敗：{str(e)[:200]}")
            prev_close = session["state"].prev_close if session["state"] else None
        session.update({"date": today, "state": Today(prev_close, dayvol), "saved": False})
        print(f"[{today}] 當日狀態已建立，上一交易日日盤收盤 {prev_close}，等待 08:45…")

    try:
        connect()
    except Exception as e:
        CONN.update({"ok": False, "last_error": str(e)[:200],
                     "since": datetime.now().strftime("%H:%M:%S")})
        print(f"啟動時連線失敗（會持續重試）：{str(e)[:200]}")
    with state_lock:
        STATE.update({"status": "waiting", "msg": "已連線，等待 08:45 開盤…",
                      "period": hist.period, "n_days_total": hist.n_days})
    print("面板已啟動，可以整天掛著。每天 08:45~09:30 自動進入即時模式。（Ctrl+C 結束）")

    last_retry = 0.0
    try:
        while True:
            now = datetime.now()
            today, t = now.date(), now.time()

            # 每天 08:30 重新建立當日連線與狀態
            if session["date"] != today and t >= pd.Timestamp("08:30").time():
                start_day(today)

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
                    save_today(st)
                    session["saved"] = True
                    print(f"[{today}] 09:30 下單時段結束，已存檔當日資料。")
                update_state(hist, st, vol_ref, t, phase="live")
            else:
                # 夜盤／收盤後：只顯示價格與動能（歷史對照樣本只涵蓋日盤）
                update_state(hist, st, vol_ref, t, phase="off")
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n收到中止訊號，關閉中…")
    finally:
        if session["api"] is not None:
            try:
                session["api"].logout()
            except Exception:
                pass


def save_today(t):
    LOG_DIR.mkdir(exist_ok=True)
    rec = {"date": str(date.today()), "open": t.open, "high": t.high, "low": t.low,
           "close": t.price, "volume": t.vol, "prev_close": t.prev_close,
           "ticks": t.ticks, "saved_at": datetime.now().isoformat(timespec="seconds")}
    (LOG_DIR / f"{date.today()}-live.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
