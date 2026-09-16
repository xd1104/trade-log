# -*- coding: utf-8 -*-
"""
【模擬】分頁的前端治具：端 `live_panel.PAGE` ＋ 一份**捏造的** `GET /api/sim/state`（七條）。

⛔ 不連永豐、⛔ 不 import broker 的下單路徑、⛔ 不碰 8770（他正在用的那個面板）。
⛔ 所有寫檔出口（sim_lanes/、tick_hist/、autofire/、fast_hist.jsonl、tmf_1min.csv）
   開跑前就全部導到暫存區 —— 這支治具**一個位元組都不會寫進 tools/shioaji/**。

為什麼要另外一支（不併進 fe_harness.py）：fe_harness 是【即時】與【自動下單】那幾支探針
在用的常駐治具，在它的模組層級造假資料會影響那些探針的起始狀態（治具互相污染踩過）。

跑法（在 repo 根目錄）：
    .venv\\Scripts\\python.exe tools\\probe\\sim_harness.py [埠，預設 8788]
然後瀏覽器開 http://127.0.0.1:8788/ → 點【模擬】。
"""
import json
import pathlib
import sys
import tempfile
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent.parent / "shioaji"
sys.path.insert(0, str(HERE))

import auto_fire as AF          # noqa: E402  ⛔ 只用它的**純計算**函式（configure 注入），不碰送單那半
import live_panel as LP         # noqa: E402
import sim_lanes as S           # noqa: E402
import strategy_lab as SL       # noqa: E402

# ══ ⛔ 先把每一個會寫檔的常數導到暫存區，再做任何事 ═══════════════════════════
TMP = pathlib.Path(tempfile.mkdtemp(prefix="sim_harness_"))
S.SIM_DIR = TMP / "sim_lanes"
S.FAST_HIST = TMP / "fast_hist.jsonl"
S.MIN1_CSV = TMP / "tmf_1min.csv"
SL.LAB_DIR = TMP / "tick_hist"
SL.MIN1_CSV = TMP / "tmf_1min_lab.csv"
AF.FIRE_DIR = TMP / "autofire"
AF.ARM_FLAG = TMP / "AUTO_ORDERS_ON"
AF.FAST_HIST = TMP / "af_fast_hist.jsonl"

WIRED = S.configure(AF.fast_verdict, AF.move_pct, AF.tpsl_points, AF.hist_read,
                    LP.FAST_PCTL, AF.FAST_RULE, reversal_fn=AF.reversal_dir, rev_sec=LP.REV_SEC)

# ══ 捏造的定論（價格一律 12000 附近，⛔ 不撞他的真實紀錄）════════════════════
NOW = datetime.now()
S.SIM_DIR.mkdir(parents=True, exist_ok=True)
_d0 = NOW.date()
for _li, _lane in enumerate(S.LANES):
    _k, _made = 1, 0
    while _made < 9:
        _d = _d0 - timedelta(days=_k)
        _k += 1
        if _d.weekday() > 4:
            continue
        _made += 1
        if (_made + _li) % 3 == 0:
            S.append_row({"lane": _lane, "date": str(_d), "decision": "不做", "why": "not_fast",
                          "reason": "不快，不做（走 0.052%，門檻 0.272%）",
                          "entry": None, "exit": None, "exit_reason": None, "points": None,
                          "src": S.SRC_NAME[_lane]})
        else:
            _dir = "做多" if (_made + _li) % 2 else "做空"
            _pts = round(((-1) ** (_made + _li)) * (37 + 11 * _made + 3 * _li), 1)
            S.append_row({"lane": _lane, "date": str(_d), "decision": _dir, "why": "fast",
                          "reason": "快（走 0.503%，門檻 0.272%）",
                          "entry": 12061.0, "exit": 12121.0,
                          "exit_reason": ["停利", "停損", "收盤"][_made % 3], "points": _pts,
                          "src": S.SRC_NAME[_lane]})
# 一條「等資料」，讓那一行也看得到
S.STATE["pending"]["orb"] = {str(_d0 - timedelta(days=1)):
                             S._pending("few_box_hist", "箱子寬度歷史不夠（這天以前只有 12 天，要 20 天）")}
S.STATE["steps"] = 7
S.STATE["last_step_at"] = NOW.strftime("%Y-%m-%d %H:%M:%S")


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ct):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/api/sim/state"):
            return self._send(200, json.dumps(S.state(datetime.now()), ensure_ascii=False),
                              "application/json; charset=utf-8")
        # ⛔ 其他 /api/ 一律 404（這支治具只服務【模擬】那一頁；別頁請用 fe_harness.py）
        if self.path.startswith("/api/"):
            return self._send(404, '{"ok":false,"msg":"這支治具只有 /api/sim/state"}',
                              "application/json; charset=utf-8")
        return self._send(200, LP.PAGE, "text/html; charset=utf-8")

    def do_POST(self):
        self._send(405, '{"ok":false,"msg":"⛔ 這支治具不收 POST"}', "application/json; charset=utf-8")


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8788
    assert port != 8770, "⛔ 不可以用 8770（他的面板正開著）"
    print("治具起來了 http://127.0.0.1:%d/   → 點【模擬】（假資料，沒有連永豐）" % port, flush=True)
    print("暫存區：%s（⛔ tools/shioaji/ 一個位元組都沒寫）" % TMP, flush=True)
    print("規則函式接上了嗎：%s" % ("是" if WIRED else "否"), flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


if __name__ == "__main__":
    main()
