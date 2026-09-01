# -*- coding: utf-8 -*-
"""
前端測試治具：把面板的 PAGE 端出來，配一份**捏造的**狀態。

不連永豐、不 import broker 的下單路徑、不碰 8770（他正在用的那個面板）。
POST /api/real/* 只記錄「前端送了幾次」，一張單都不會出去。

【2026-09-01 補：右欄改成練習／真實兩個分頁之後多出來的假狀態】
  - 兩邊的紀錄都給**超過容器高度**的筆數（真實 8 筆、練習 9 筆）——
    有 max-height 的 flex 直欄，子元素少的時候「被壓扁」看不出來，
    一定要用會捲的筆數才測得到（面板開發鐵律）。
  - 多一個 short 模式：做空部位，用來驗平倉鈕寫的是「買進 ×1（回補空單）」。
  - 多一個 stale 模式：有部位＋報價中斷，用來驗跨分頁警報。
"""
import json
import pathlib
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(r"C:\Users\USER\Desktop\Claude Work\trade-log\tools\shioaji")
sys.path.insert(0, str(HERE))
import live_panel as LP

SCRATCH = pathlib.Path(__file__).resolve().parent
BASE = json.loads((SCRATCH / "state_template.json").read_text(encoding="utf-8"))
POSTS = []
MODE = {"v": "flat"}
SLOW = {"v": 0.0}

# ── 今天的真實交易：前 5 筆是墊高用的（讓清單真的超過 max-height 而要捲），
#    後 3 筆是有意義的三種狀態：停利、停損、問不到成交價（要留白）。
REAL_TRADES = [
    {"dir": "long", "qty": 1, "entry_time": "08:52:03", "entry": 47080.0,
     "exit_time": "08:58:44", "exit": 47180.0, "reason": "tp", "points": 100.0},
    {"dir": "short", "qty": 1, "entry_time": "09:20:15", "entry": 47190.0,
     "exit_time": "09:26:02", "exit": 47090.0, "reason": "tp", "points": 100.0},
    {"dir": "long", "qty": 1, "entry_time": "09:41:30", "entry": 47120.0,
     "exit_time": "09:48:12", "exit": 47020.0, "reason": "sl", "points": -100.0},
    {"dir": "short", "qty": 1, "entry_time": "10:02:44", "entry": 47005.0,
     "exit_time": "10:08:51", "exit": 47041.0, "reason": "manual", "points": -36.0},
    {"dir": "long", "qty": 1, "entry_time": "10:15:09", "entry": 47060.0,
     "exit_time": "10:22:37", "exit": 47083.0, "reason": "closed_elsewhere", "points": 23.0},
    {"dir": "long", "qty": 1, "entry_time": "09:05:11", "entry": 47144.0,
     "exit_time": "09:12:40", "exit": 47244.0, "reason": "tp", "points": 100.0},
    {"dir": "short", "qty": 1, "entry_time": "10:31:02", "entry": 47010.0,
     "exit_time": "10:44:19", "exit": 47110.0, "reason": "sl", "points": -100.0},
    # 問不到成交價：出場價與點數都要留白，絕對不可以拿現價冒充
    {"dir": "long", "qty": 1, "entry_time": "13:39:21", "entry": 47144.0,
     "exit_time": "13:41:17", "exit": None, "reason": "manual", "points": None},
]

# ── 今天的練習交易：欄位跟 close_position() 寫出來的一模一樣
def _sim(t, xt, d, ep, xp, why):
    pts = (xp - ep) if d == "long" else (ep - xp)
    return {"date": "2026-09-01", "dir": d, "entry": ep, "exit": xp, "time": t,
            "note": "", "mode": "sim", "_exit_time": xt, "_reason": why,
            "_points": round(pts, 1), "_net": round(pts - 5.0, 1)}


SIM_TRADES = [
    _sim("08:46", "08:53:11", "long", 47010, 47110, "tp"),
    _sim("08:58", "09:04:20", "short", 47130, 47030, "tp"),
    _sim("09:07", "09:15:02", "long", 47055, 46955, "sl"),
    _sim("09:18", "09:23:44", "short", 46990, 47090, "sl"),
    _sim("09:29", "09:35:10", "long", 47040, 47062, "manual"),
    _sim("09:41", "09:52:33", "short", 47105, 47005, "tp"),
    _sim("10:03", "10:09:58", "long", 47020, 46920, "sl"),
    _sim("10:17", "10:25:41", "short", 47075, 47048, "manual"),
    _sim("10:33", "10:44:07", "long", 46980, 47080, "tp"),
]


# ── 假的 5 分 K：讓 paintChart() 真的畫得出來 ──────────────────────────────
#    沒有這一段的話 /api/bars 回 {}，前端會退到 paintFallback，
#    K 線圖那一整段（含新加的「真實部位三條線」）等於完全沒被測到。
DAY = "2026-09-01"


def bars():
    out, px = [], 46900.0
    mins = 8 * 60 + 45
    for i in range(60):                       # 08:45 ~ 13:45，5 分 K
        o = px
        c = o + ((i * 37) % 61) - 30
        out.append({"t": "%02d:%02d" % (mins // 60, mins % 60), "d": DAY,
                    "o": round(o, 1), "h": round(max(o, c) + 8, 1),
                    "l": round(min(o, c) - 8, 1), "c": round(c, 1),
                    "v": 400 + (i * 53) % 900})
        px = c
        mins += 5
    return {"date": DAY, "bars": out, "trades": [], "tf": 5, "full": True,
            "night_open": DAY, "ref": 46900.0,
            "days": [{"d": DAY, "w": "二", "closed": False, "n": len(SIM_TRADES),
                      "net": int(round(sum(t["_net"] for t in SIM_TRADES))),
                      "chg": 234.0, "pct": 0.5, "rng": 180.0, "close": round(px, 1)}]}


def stats():
    """/api/stats：練習成績那一區的資料，欄位照 live_panel.practice_stats() 的形狀。
    治具原本沒有這支（回 {}）⇒ statsBox() 直接回空字串，**練習成績整區等於沒被測到**。"""
    recs = SIM_TRADES

    def agg(sub):
        net = [r["_net"] for r in sub]
        w = sum(1 for x in net if x > 0)
        return {"n": len(sub), "wins": w, "losses": len(sub) - w,
                "win_rate": round(w / len(sub) * 100, 1),
                "total": round(sum(net), 1), "avg": round(sum(net) / len(sub), 1),
                "ntd": round(sum(net) * 10)}

    return {"windows": [{"label": lab, **agg(recs[-n:])}
                        for lab, n in [("近 7 筆", 7), ("近 10 筆", 10), ("全部", 10 ** 6)]],
            "recent": [{k: r.get(k) for k in
                        ("date", "time", "dir", "entry", "exit", "note",
                         "_net", "_reason", "_source")} for r in recs][::-1],
            "total": len(recs)}


def tick_px(base):
    """
    ⛔ 【假狀態一定要會動】lab-qa 2026-09-01 用變異測試證明：治具的現價是固定值時，
       `realBody()` 每一輪產出同一個字串，`setEl` 判定「沒變」就不碰 DOM ⇒
       **「重繪把按住的按鈕換掉」這個故障在治具上根本重現不了**。
       把 `if(!holdingNow) paintRight(...)` 那道守衛整個刪掉，探針照樣 35/35 全綠。
       現價跟著時間跳之後，同一份壞掉的程式立刻紅。
       靜態治具驗不出任何「重繪」類的 bug —— 這條對後面每一個新測項都成立。
    """
    return round(base + (int(time.time() * 2) % 9) - 4, 0)


def state():
    s = json.loads(json.dumps(BASE))
    s["today_trades"] = json.loads(json.dumps(SIM_TRADES))
    s["chips"]["price"] = tick_px(47134)
    s["real"] = {"live": False, "ca_ok": True, "ca_msg": None,
                 "entries_today": 0, "max_entries": 3, "account": "0000000",   # 假的，repo 是公開的
                 "last_error": None, "stale_sec": None,
                 "code": {"broker": "09-01 12:45", "panel": "09-01 12:45",
                          "started": "09-01 12:50", "stale": False},
                 "trades": json.loads(json.dumps(REAL_TRADES))}
    m = MODE["v"]
    if m == "closed":
        # 休市：沒有即時報價。兩區的下單鈕都必須真的 disabled（紀錄正確性，不是 UX 取捨）
        s["quote"] = "closed"
        s["chips"]["price"] = None
        s["real"].update({"position": None, "can_enter": False,
                          "why": "休市中，沒有即時報價 —— 開盤後才能下單"})
    elif m == "flat":
        s["real"].update({"position": None, "can_enter": True, "why": None})
    else:
        # holding      有部位、**停利沒掛上去**（QC 第 3 項要顯示的狀態）
        # with_target  有部位、停利真的掛上了
        # short        做空部位（平倉鈕要寫「買進 ×1（回補空單）」）
        # stale        有部位 ＋ 報價中斷（跨分頁警報）
        short = (m == "short")
        d = -1 if short else 1
        entry = 47100.0
        s["real"].update({
            "position": {"dir": "short" if short else "long", "entry": entry, "qty": 1,
                         "entry_time": "09:05", "recovered": False,
                         "has_target": m in ("with_target", "short", "stale")},
            # 浮動點數也要跳，理由同 tick_px
            "float_pts": float(tick_px(12)) * d,
            "tp": entry + d * 100, "sl": entry - d * 100,
            "stale_sec": 27 if m == "stale" else None,
            "can_enter": False, "why": "已經有部位了，先平倉才能再進場"})
    return s


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        b = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/api/state"):
            return self._send(200, json.dumps(state(), ensure_ascii=False))
        if self.path.startswith("/api/bars"):
            return self._send(200, json.dumps(bars(), ensure_ascii=False))
        if self.path.startswith("/api/stats"):
            return self._send(200, json.dumps(stats(), ensure_ascii=False))
        if self.path.startswith("/manifest.webmanifest"):
            # 治具原本把所有非 /api/ 的路徑都回 PAGE ⇒ 瀏覽器拿到 HTML 當 manifest 解，
            # console 就多一則 "Manifest: Syntax error"，把「零錯誤」那道尺弄髒。
            return self._send(200, json.dumps(LP.MANIFEST, ensure_ascii=False),
                              "application/manifest+json; charset=utf-8")
        if self.path.startswith("/api/"):
            return self._send(200, "{}")
        return self._send(200, LP.PAGE, "text/html; charset=utf-8")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        POSTS.append((self.path, body, time.time()))
        if SLOW["v"]:
            time.sleep(SLOW["v"])        # 模擬券商回報延遲，讓「送出中」那段看得到
        if self.path == "/api/real/enter":
            MODE["v"] = "holding"
            return self._send(200, json.dumps(
                {"ok": True, "warn": True,
                 "msg": "已經進場，但**停利單沒掛上去**（券商拒絕）—— 請立刻自己到大戶投補掛，或直接平倉"},
                ensure_ascii=False))
        return self._send(200, json.dumps({"ok": True, "msg": "ok"}, ensure_ascii=False))


srv = ThreadingHTTPServer(("127.0.0.1", 8771), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
print("治具起來了 http://127.0.0.1:8771/   （假狀態，沒有連永豐）", flush=True)

# 讓外面的腳本可以改模式／讀 POST 紀錄
class Ctl(H):
    def do_GET(self):
        p = self.path
        if p == "/posts":
            return self._send(200, json.dumps(
                {"posts": [(a, b) for a, b, _ in POSTS]}, ensure_ascii=False))
        if p == "/reset":
            POSTS.clear()                 # ⚠️ 只清紀錄，**不可以順手改 MODE**
            return self._send(200, "{}")
        if p.startswith("/mode/"):
            MODE["v"] = p.split("/")[-1]
            return self._send(200, "{}")
        if p.startswith("/slow"):
            SLOW["v"] = float(p.split("/")[-1]) if p.count("/") > 1 else 1.2
            return self._send(200, "{}")
        return self._send(200, "{}")


ctl = ThreadingHTTPServer(("127.0.0.1", 8772), Ctl)
threading.Thread(target=ctl.serve_forever, daemon=True).start()
print("控制埠 8772：/mode/flat /mode/holding /mode/with_target /mode/short /mode/stale /mode/closed"
      " /posts /reset /slow", flush=True)
while True:
    time.sleep(60)
