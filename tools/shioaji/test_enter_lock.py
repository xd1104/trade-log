# -*- coding: utf-8 -*-
"""
連按買進只准出去一張單 —— 用面板**自己那個 HTTP handler** 實測。

================================================================
為什麼要有這支
================================================================
券商回報部位有 1~2 秒延遲。他按了長按送出、畫面還沒變，很自然會再按一次
（09-01 那天他就說過「下單了，可是沒有改成有部位的顯示」）。舊版：

  - 平倉有 `closing` 擋著，**進場沒有** —— 對稱的東西又只做了一邊。
  - 就算前端擋住了，重整、開兩個視窗、或前端出錯都還是能連送兩個請求。
    `ThreadingHTTPServer` 會讓它們**真的同時跑**。

兩張都出去的後果不是「多送一張」而已：變成 2 口，而且兩張停利單都掛著、
面板只記得後面那張 ⇒ **先掛的那張永遠撤不掉**，成交後就是一個反向新倉。

所以這支不 mock handler，直接把 `live_panel.Handler` 架在一個空閒的埠上，
用兩條執行緒同時 POST `/api/real/enter`，數「`broker.enter` 到底被叫了幾次」。

**不連永豐、不送任何單**：`broker.enter` 換成假的（只記錄並睡 0.6 秒模擬券商延遲）。

怎麼跑（PowerShell）：
    & "C:\\Users\\USER\\Desktop\\Claude Work\\trade-log\\.venv\\Scripts\\python.exe" `
      "C:\\Users\\USER\\Desktop\\Claude Work\\trade-log\\tools\\shioaji\\test_enter_lock.py"
"""
import json
import pathlib
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import broker
import live_panel as LP

FAIL = 0
CALLS = []


def chk(name, got, want):
    global FAIL
    ok = got == want
    FAIL += not ok
    print(("  OK   " if ok else "  FAIL ") + name + ("" if ok else f"  (得到 {got!r}，期待 {want!r})"))


# ── 把會碰到錢的那一段換掉 ───────────────────────────────────────
def fake_enter(direction, price, tp_points):
    CALLS.append(direction)
    time.sleep(0.6)                      # 券商回報成交的延遲，就是這段空窗讓他想再按一次
    broker._state["position"] = {"dir": direction, "entry": float(price), "qty": 1,
                                 "entry_time": "09:05", "target_trade": None,
                                 "recovered": False}
    return True, None, broker._state["position"]


broker.enter = fake_enter
broker.can_enter = lambda price, live: (True, None)
broker.is_live = lambda: False           # 演練，額外一層保險
LP.STATE["quote"] = "live"
LP.CURRENT_STATE["today"] = type("T", (), {"price": 45000.0})()

srv = ThreadingHTTPServer(("127.0.0.1", 0), LP.Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
print(f"面板 handler 架在 127.0.0.1:{port}（沒有連永豐，broker.enter 是假的）\n")


def post(path, payload):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


print("=== 兩個請求同時到（＝他連按兩下 / 開了兩個視窗）===")
results = {}


def shoot(tag):
    results[tag] = post("/api/real/enter", {"dir": "long"})


t1 = threading.Thread(target=shoot, args=("a",))
t2 = threading.Thread(target=shoot, args=("b",))
t1.start()
time.sleep(0.05)                          # 第二下比第一下晚 50ms，第一張還在路上
t2.start()
t1.join()
t2.join()

codes = sorted(r[0] for r in results.values())
chk("  只有一張單真的送出去", len(CALLS), 1)
chk("  一個 200、一個 409（被擋下來那個要講原因）", codes, [200, 409])
blocked = [r[1]["msg"] for r in results.values() if r[0] == 409]
chk("  擋下來的訊息說得出在等什麼", "還在送" in (blocked[0] if blocked else ""), True)

print("\n=== 前一張送完之後，鎖要放掉（不可以從此再也下不了單）===")
CALLS.clear()
broker._state["position"] = None
code, body = post("/api/real/enter", {"dir": "short"})
chk("  下一筆送得出去", code, 200)
chk("  而且真的到 broker", CALLS, ["short"])

srv.shutdown()
print("\n總結:", "全部通過" if not FAIL else f"{FAIL} 項失敗")
sys.exit(1 if FAIL else 0)
