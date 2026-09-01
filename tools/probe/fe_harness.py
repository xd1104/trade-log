# -*- coding: utf-8 -*-
"""
前端測試治具：把面板的 PAGE 端出來，配一份**捏造的**狀態。

不連永豐、不 import broker 的下單路徑、不碰 8770（他正在用的那個面板）。
POST /api/real/* 只記錄「前端送了幾次」，一張單都不會出去。
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


def state():
    s = json.loads(json.dumps(BASE))
    s["real"] = {"live": False, "ca_ok": True, "ca_msg": None,
                 "entries_today": 0, "max_entries": 3, "account": "0000000",   # 假的，repo 是公開的
                 "last_error": None, "stale_sec": None,
                 "code": {"broker": "09-01 12:45", "panel": "09-01 12:45",
                          "started": "09-01 12:50", "stale": False},
                 # 成績單：一筆停利、一筆停損、一筆問不到成交價（要留白）
                 "trades": [
                     {"dir": "long", "qty": 1, "entry_time": "09:05:11", "entry": 47144.0,
                      "exit_time": "09:12:40", "exit": 47244.0, "reason": "tp",
                      "points": 100.0},
                     {"dir": "short", "qty": 1, "entry_time": "10:31:02", "entry": 47010.0,
                      "exit_time": "10:44:19", "exit": 47110.0, "reason": "sl",
                      "points": -100.0},
                     {"dir": "long", "qty": 1, "entry_time": "13:39:21", "entry": 47144.0,
                      "exit_time": "13:41:17", "exit": None, "reason": "manual",
                      "points": None}]}
    if MODE["v"] == "flat":
        s["real"].update({"position": None, "can_enter": True, "why": None})
    else:
        # 有部位，而且**停利沒掛上去** —— 這正是 QC 第 3 項要顯示的狀態
        s["real"].update({
            "position": {"dir": "long", "entry": 47100.0, "qty": 1,
                         "entry_time": "09:05", "recovered": False,
                         "has_target": MODE["v"] == "with_target"},
            "float_pts": 12.0, "tp": 47200.0, "sl": 47000.0,
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
print("控制埠 8772：/mode/flat /mode/holding /mode/with_target /posts /reset", flush=True)
while True:
    time.sleep(60)
