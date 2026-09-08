# -*- coding: utf-8 -*-
"""
【程式下單】分頁的前端治具：把面板的 PAGE 端出來，配一份**合成的** autotest 資料。

⚠️ 不連永豐、⛔ **不碰 8770**（Benson 的面板正開著）、⛔ 不 import broker 的下單路徑。
⛔ 價格一律 12000 附近（`autotest_synth.py`），一個真實成交價／時間／點數都沒有。

- 資料埠 8773：`/`（PAGE）、`/api/auto/*`、`/api/state`（前端每 500ms 會打）
- 控制埠 8774：探針用來換資料集／換時鐘／注入回填／灌壞資料

⭐ **`/api/auto/*` 走的是產品自己的 `auto_days()／auto_stats()／auto_day()`**
   （只把 `AUTO_DIR` 指到暫存區、把 `one_min_bars()` 換成合成 K 棒）——
   治具另寫一份 payload 的話，「治具對、產品錯」會全綠。

跑法：  PYTHONIOENCODING=utf-8 PYTHONUTF8=1 .venv\\Scripts\\python.exe tools\\probe\\at_harness.py
        改過 live_panel.py 一定要**重起治具**。
"""
import json
import pathlib
import shutil
import sys
import tempfile
import threading
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "shioaji"))
import live_panel as LP          # noqa: E402
import autotest_synth as SY      # noqa: E402

PORT = 8773
CTL = 8774
TMP = pathlib.Path(tempfile.mkdtemp(prefix="at-harness-"))
TODAY = str(date.today())

# 治具狀態。⚠️ 時鐘是**假的**：探針要能把它撥到 09:01（不預告）與 09:05（已判斷）。
ST = {"bars": {}, "clock": "10:30:00", "today_mode": "have", "days": 20,
      "backfill": 0, "bad": 0, "mine": True,
      # /api/auto/day 故意慢回：驗「連按 ◀ 時先送的請求後回來，不可以蓋掉新的那天」
      # ⚠️ slow 是「每一個都慢」（那樣回應順序仍然大致照送出順序，負控組打不紅）；
      #    slow_first 是「**只有接下來那 k 個**慢」⇒ 先送的一定最後回來，
      #    才真的重現「舊那天的資料蓋回新的」那個故障。
      "slow": 0.0, "slow_first": 0, "slow_first_ms": 0.0,
      # 今天那一列要不要附 settle。⛔ 「今天已經記了訊號、但還沒摸到 ±100、日盤也還沒收」
      #    ＝ **持倉中**，那是他早上開面板時真正會看到的狀態
      #    （2026-09-08 那個 bug 就是把這個狀態寫成一個假的「09:05 收盤平」）。
      "settle_today": True}


def _rebuild():
    """重建整個暫存資料集。⛔ 只寫暫存區，一個位元組都不碰他的 autotest/。"""
    for p in TMP.glob("*.jsonl"):
        p.unlink()
    if (TMP / "real").exists():
        shutil.rmtree(TMP / "real")
    (TMP / "real").mkdir(parents=True, exist_ok=True)
    rows, bars = SY.build(ST["days"], end=date.today())
    ST["bars"] = bars
    if ST["today_mode"] == "none":
        # 今天整天沒有記錄（面板 09:03:30 時沒開）—— 空狀態是主流程，不是邊角
        rows = [r for r in rows if r["date"] != TODAY]
    if not ST["settle_today"]:
        # ⛔ 今天有訊號、但還沒結算 ⇒ 畫面上必須是「持倉中」，⛔ 不可以是一個假的點數
        rows = [r for r in rows if not (r["date"] == TODAY and r["rec"] == "settle")]
    SY.write(TMP, rows)
    if ST["backfill"]:
        # ⛔ 回填的那批**永遠不可以**跟實跑相加（分鐘資料切不出 09:03:30）
        brows, bbars = SY.build(ST["backfill"], end=date(2025, 6, 30), src="backfill")
        ST["bars"].update(bbars)
        SY.write(TMP, brows)
    if ST["bad"]:
        # ⛔ 一列壞資料只准弄掉那一列，不准弄掉一整個月
        with (TMP / f"{TODAY[:7]}.jsonl").open("a", encoding="utf-8") as f:
            f.write('{"rec":"sig","date":"' + TODAY + '","px":"一二三"}\n')
            f.write("{ 這一列不是 json\n")
            f.write('{"rec":"sig","date":"壞掉的日期","px":12000}\n')
    if ST["mine"]:
        for i, d in enumerate(sorted(ST["bars"])):
            n = (i % 4)
            if not n:
                continue
            recs = SY.mine_rows(d, n=n, first_why=("tp" if i % 2 else "sl"))
            with (TMP / "real" / f"{d}.jsonl").open("w", encoding="utf-8") as f:
                for r in recs:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
    LP.AUTO_CACHE.clear()


def _one_min_bars(d):
    """頂替產品的 one_min_bars() —— 治具不連永豐、也不讀他的 csv。"""
    return ST["bars"].get(str(d), [])


_REAL_DAY_OVER = LP._auto_day_over


def _day_over(d, now=None):
    """
    ⛔ 治具的時鐘是**假的**（ST["clock"]，探針會把它撥到 09:01／10:30），
       所以「那天的日盤收了沒」也一定要跟著同一把尺 —— 用真實時鐘的話，
       同一份治具在 13:47 之前跑跟之後跑會畫出**不一樣的畫面**（持倉中／結算中），
       探針就變成看時間才會綠的。過去的日子照樣走產品那支。
    """
    if d != TODAY:
        return _REAL_DAY_OVER(d, now)
    try:
        hh, mm, ss = (int(x) for x in ST["clock"].split(":"))
    except ValueError:
        return False
    return hh * 3600 + mm * 60 + ss >= LP.AUTO_SETTLE_AFTER


LP.AUTO_DIR = TMP
LP.AUTO_REAL_DIR = TMP / "real"
LP.one_min_bars = _one_min_bars
LP._auto_day_over = _day_over
LP.AUTO["started"] = False        # ⛔ 治具絕對不可以寫出真的一天
_rebuild()

STATE = {"status": "live", "clock": "10:30:00", "quote": "closed", "market": "closed",
         "phase": "off", "position": None, "today_trades": [], "chips": None,
         "result": None, "age_sec": None, "conn": {"ok": True},
         "msg": "治具：不連永豐", "real": {"live": False, "position": None,
                                           "can_enter": False, "why": "治具"}}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _j(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        # ⛔ 治具**不會**送出任何單。這一頁本來也不該打到這裡來。
        return self._j(200, {"ok": False, "msg": "治具不送單"})

    def do_GET(self):
        p = self.path
        if p.startswith("/api/auto/days"):
            out = LP.auto_days()
            out["today"] = TODAY
            out["now"] = ST["clock"]          # ⛔ 前端一律用後端時鐘
            return self._j(200, out)
        if p.startswith("/api/auto/stats"):
            q = p.split("?", 1)[1] if "?" in p else ""
            win, src = 20, "live"
            for kv in q.split("&"):
                if kv.startswith("win="):
                    win = int(kv[4:] or 20)
                elif kv.startswith("src="):
                    src = kv[4:]
            return self._j(200, LP.auto_stats(win, src))
        if p.startswith("/api/auto/day"):
            q = p.split("?", 1)[1] if "?" in p else ""
            want = None
            for kv in q.split("&"):
                if kv.startswith("date="):
                    want = kv[5:]
            if not want or not LP._AUTO_DATE.match(want):
                return self._j(400, {"error": "date 要是 YYYY-MM-DD"})
            import time as _t
            if ST["slow_first"] > 0:
                ST["slow_first"] -= 1
                _t.sleep(ST["slow_first_ms"])
            elif ST["slow"]:
                _t.sleep(ST["slow"])
            out = LP.auto_day(want)
            if out is None:
                return self._j(404, {"error": "這天沒有紀錄", "date": want})
            out["today"] = TODAY
            out["now"] = ST["clock"]
            return self._j(200, out)
        if p.startswith("/api/state"):
            s = dict(STATE)
            s["clock"] = ST["clock"]
            return self._j(200, s)
        if p.startswith("/api/"):
            return self._j(200, {})
        if p.startswith("/manifest.webmanifest"):
            # 沒有這一條的話瀏覽器會拿到 HTML ⇒ console 冒一條 Manifest 語法錯誤，
            # 而探針有一條「console 零錯誤」——那會變成一個永遠紅的假警報。
            b = json.dumps(LP.MANIFEST, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/manifest+json; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        b = LP.PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


class C(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _j(self, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = self.path.rstrip("/")
        if p.startswith("/at/clock"):
            ST["clock"] = p.split("/at/clock", 1)[1].lstrip("/") or "10:30:00"
            return self._j({"clock": ST["clock"]})
        if p.startswith("/at/days"):
            ST["days"] = max(0, int(p.rsplit("/", 1)[1]))
            _rebuild()
            return self._j({"days": ST["days"]})
        if p.startswith("/at/backfill"):
            ST["backfill"] = max(0, int(p.rsplit("/", 1)[1]))
            _rebuild()
            return self._j({"backfill": ST["backfill"]})
        if p.startswith("/at/bad"):
            ST["bad"] = max(0, int(p.rsplit("/", 1)[1]))
            _rebuild()
            return self._j({"bad": ST["bad"]})
        if p.startswith("/at/today"):
            ST["today_mode"] = p.rsplit("/", 1)[1]
            _rebuild()
            return self._j({"today_mode": ST["today_mode"]})
        if p.startswith("/at/settletoday"):
            ST["settle_today"] = p.rsplit("/", 1)[1] not in ("0", "off", "no")
            _rebuild()
            return self._j({"settle_today": ST["settle_today"]})
        if p.startswith("/at/slowfirst"):
            # /at/slowfirst/<k>/<ms>：接下來 k 個 /api/auto/day 慢 ms 毫秒，之後恢復
            bits = p.split("/")
            ST["slow_first"] = int(bits[-2])
            ST["slow_first_ms"] = max(0.0, float(bits[-1]) / 1000.0)
            return self._j({"slow_first": ST["slow_first"], "ms": ST["slow_first_ms"]})
        if p.startswith("/at/slow"):
            ST["slow"] = max(0.0, float(p.rsplit("/", 1)[1]) / 1000.0)
            return self._j({"slow": ST["slow"]})
        if p.startswith("/at/reset"):
            ST.update({"clock": "10:30:00", "today_mode": "have", "days": 20,
                       "backfill": 0, "bad": 0, "mine": True, "slow": 0.0,
                       "slow_first": 0, "slow_first_ms": 0.0, "settle_today": True})
            _rebuild()
            return self._j({"ok": True})
        if p.startswith("/at/where"):
            return self._j({"dir": str(TMP), "today": TODAY,
                            "dates": sorted(ST["bars"]), "state": ST["clock"],
                            "days": ST["days"]})
        return self._j({"error": "?"})


if __name__ == "__main__":
    threading.Thread(target=lambda: ThreadingHTTPServer(("127.0.0.1", CTL), C).serve_forever(),
                     daemon=True).start()
    print(f"治具資料：{TMP}")
    print(f"今天：{TODAY}　假時鐘：{ST['clock']}")
    print(f"http://127.0.0.1:{PORT}/　（控制埠 {CTL}）")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
