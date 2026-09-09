# -*- coding: utf-8 -*-
"""
【自動下單】分頁的前端治具：把面板的 PAGE 端出來，配一份**合成的** autofire 紀錄。

⚠️ 不連永豐、⛔ **不碰 8770**（Benson 的面板正開著）。
⛔⛔ **這支治具永遠不會送出任何單，也永遠不會建立真的 `AUTO_ORDERS_ON`**：
   - `auto_fire.ARM_FLAG` / `FIRE_DIR` 與 broker 的每一個路徑**全部導到暫存區**
     （啟動時會斷言真的 `AUTO_ORDERS_ON` 不存在，存在就直接拒絕啟動）
   - ⛔ **不呼叫 `auto_fire.start()`**（送單執行緒不起來）
   - ⛔ **不動 `live_panel.AUTO_SIG_HOOK`**（09:03:30 的掛勾維持 no-op）
⛔ 價格一律 12000 附近，一個真實成交價／時間／點數都沒有。

- 資料埠 8775：`/`（PAGE）、`/api/fire/state`、`/api/state`（前端每 500ms 會打）
- 控制埠 8776：探針用來切開關／切真單／換資料集

⭐ `/api/fire/state` 走的是**產品自己的** `auto_fire.state()` ＋ `live_panel.fire_sim_pairs()`
   ＋ `live_panel.fire_arm_confirm()` ＋ `live_panel.FIRE_TOKEN`
   —— 治具另寫一份 payload 的話，「治具對、產品錯」會全綠。
⭐ `POST /api/fire/on`（打開）同理走**產品的** `fire_post_guard()` ＋ `fire_arm_on()`：
   這樣「前端少帶一個標頭 ⇒ 後端會擋」在探針上才紅得起來。
   ⚠️ 它建的是**暫存區**那個 `AUTO_ORDERS_ON`（`AF.ARM_FLAG` 已經導走），
   ⛔ 真的 `tools/shioaji/AUTO_ORDERS_ON` 啟動時就斷言過不存在。

跑法：  PYTHONIOENCODING=utf-8 PYTHONUTF8=1 .venv\\Scripts\\python.exe tools\\probe\\fire_harness.py
        改過 live_panel.py／auto_fire.py 一定要**重起治具**。
"""
import json
import pathlib
import shutil
import sys
import tempfile
import threading
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "shioaji"))
import auto_fire as AF          # noqa: E402
import broker                   # noqa: E402
import live_panel as LP         # noqa: E402

# 埠可以用環境變數覆寫（⛔ 預設 8775／8776，⛔ 永遠不可以是 8770）。
# 改過 live_panel.py／auto_fire.py 一定要**重起治具**才吃得到新的 PAGE；
# 舊實例關不掉時就另起一份在別的埠，探針用 --url/--ctl 指過去。
import os as _os                      # noqa: E402
PORT = int(_os.environ.get("FIRE_PORT", "8775"))
CTL = int(_os.environ.get("FIRE_CTL", "8776"))
assert PORT != 8770 and CTL != 8770, "⛔ 不可以用 8770（他的面板正開著）"
TMP = pathlib.Path(tempfile.mkdtemp(prefix="fire-harness-"))
TODAY = str(date.today())

# ⛔⛔ 出貨狀態就是「那個檔不存在」。治具啟動前先確認一次 —— 存在的話代表
#    有人（或某支程式）真的把開關建出來了，這支就不該再往下跑。
if AF.ARM_FLAG.exists():
    raise SystemExit("⛔ tools/shioaji/AUTO_ORDERS_ON 竟然存在！治具拒絕啟動。")

AF.ARM_FLAG = TMP / "AUTO_ORDERS_ON"
AF.FIRE_DIR = TMP / "autofire"
broker.REAL_FLAG = TMP / "REAL_ORDERS_ON"
broker.ORDER_DIR = TMP / "real_orders"
broker.TRADE_DIR = TMP / "real_trades"
LP.AUTO_DIR = TMP / "autotest"
LP.AUTO_REAL_DIR = TMP / "real_trades"
# 接常數與算式（讓 wired=True），⛔ 但**不** start()、⛔ 也不動 AUTO_SIG_HOOK
AF.configure(signal_at=LP.SIGNAL_AT, signal_sec=LP.SIGNAL_SEC, late_ms=LP.AUTO_LATE_MS,
             gap_s=LP.AUTO_GAP_S, tp_points=LP.TP_POINTS,
             sig_fn=LP.auto_sig, dirs_fn=LP.auto_dirs, eod_at=LP.EOD_CLOSE_AT)

ST = {"arm": "off", "live": False, "rows": "mixed", "clock": "10:30:00",
      # ⭐ R2 用：確認條那句話要講「今天 09:03:30」還是「下一個交易日 09:03:30」，
      #    正本是 `LP.fire_fires_today(now)`。探針要驗**盤前／盤後兩種**，
      #    所以這裡可以塞一個假的「現在」（ISO 字串；None ＝ 真的現在）。
      #    ⛔ 治具不自己算那句話 —— 一律把這個 now 餵給產品的 `fire_arm_confirm()`。
      "now": None}
# ⛔ 被產品守衛擋掉的 POST（前端漏帶標頭時會落到這裡）
BLOCKED = []


def _fake_now():
    """`ST["now"]` 解析成 datetime；⛔ 解不出來就回 None（＝用真的現在）。"""
    v = ST.get("now")
    if not v:
        return None
    try:
        return datetime.fromisoformat(v)
    except Exception:
        return None


def _d(n):
    """n 天前（只用平日，避免日期看起來像週末）。"""
    return str(date.today() - timedelta(days=n))


def _sim_rows(days):
    """順手產一份模擬那一頁的紀錄，讓「跟模擬對得起來」那一欄有東西。"""
    LP.AUTO_DIR.mkdir(parents=True, exist_ok=True)
    for p in LP.AUTO_DIR.glob("*.jsonl"):
        p.unlink()
    for d, px in days:
        if px is None:
            row = {"rec": "miss", "date": d, "src": "live", "why": "no_quote"}
        else:
            row = {"rec": "sig", "date": d, "src": "live", "at": "09:03:30.100",
                   "px": px, "sig": {"A": 5.0, "B": 10.0},
                   "dirs": {"A": 1, "B": 1, "C": 0, "D": 1}, "thresh": 30.0}
        with (LP.AUTO_DIR / (d[:7] + ".jsonl")).open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _rebuild():
    """重建整個暫存資料集。⛔ 只寫暫存區。"""
    if AF.FIRE_DIR.exists():
        shutil.rmtree(AF.FIRE_DIR)
    AF.FIRE_DIR.mkdir(parents=True, exist_ok=True)
    LP.AUTO_CACHE.clear()
    rows, sim = [], []
    if ST["rows"] == "none":
        _sim_rows([])
        return
    if ST["rows"] in ("mixed", "today", "eodfail", "eodok", "eodnotours",
                      "eodcanttell"):
        # 今天：真的送出去了（做多）
        if ST["rows"] == "today":
            rows += [
                {"rec": "fire", "stage": "sending", "date": TODAY, "method": "B",
                 "dir": "long", "px": 12010.0, "live": True, "qty": 1, "tp_points": 100.0},
                {"rec": "result", "stage": "done", "date": TODAY, "method": "B",
                 "dir": "long", "px": 12010.0, "ok": True, "why": None, "why_msg": None,
                 "entry": 12013.0, "tp": 12113.0, "slip": 3.0, "has_target": True,
                 "warn": None, "live": True, "qty": 1},
            ]
            sim.append((TODAY, 12010.0))
    if ST["rows"] == "eodfail":
        # ⛔ 收盤平不掉：畫面上要跳出金色警示，叫他自己到大戶投平
        rows += [
            {"rec": "result", "stage": "done", "date": TODAY, "method": "B",
             "dir": "long", "px": 12010.0, "ok": True, "why": None, "why_msg": None,
             "entry": 12013.0, "tp": 12113.0, "slip": 3.0, "has_target": True,
             "entry_time": "09:03:31", "warn": None, "live": True, "qty": 1},
            {"rec": "eod", "stage": "done", "date": TODAY, "why": "eod_failed",
             "why_msg": AF.WHY["eod_failed"] + "（試了 2 次；券商說：平不掉）",
             "ok": False, "alarm": True, "at": "13:44:45", "eod_at": LP.EOD_CLOSE_AT,
             "dir": "long", "entry": 12013.0, "tries": 2, "live": True},
        ]
        sim.append((TODAY, 12010.0))
    if ST["rows"] == "eodok":
        # 平掉了：一般語氣（⛔ 不可以跟「平不掉」寫同一句）
        rows += [
            {"rec": "result", "stage": "done", "date": TODAY, "method": "B",
             "dir": "long", "px": 12010.0, "ok": True, "why": None, "why_msg": None,
             "entry": 12013.0, "tp": 12113.0, "slip": 3.0, "has_target": True,
             "entry_time": "09:03:31", "warn": None, "live": True, "qty": 1},
            {"rec": "eod", "stage": "done", "date": TODAY, "why": "eod_closed",
             "why_msg": AF.WHY["eod_closed"], "ok": True, "alarm": False,
             "at": "13:43:33", "eod_at": LP.EOD_CLOSE_AT, "dir": "long",
             "entry": 12013.0, "exit": 12031.0, "points": 18.0, "tries": 1,
             "live": True},
        ]
        sim.append((TODAY, 12010.0))
    if ST["rows"] == "eodnotours":
        # ⛔⛔ 現在那口不是自動下單開的 ⇒ 不碰，但要**大聲講**
        rows += [
            {"rec": "result", "stage": "done", "date": TODAY, "method": "B",
             "dir": "long", "px": 12010.0, "ok": True, "why": None, "why_msg": None,
             "entry": 12013.0, "tp": 12113.0, "slip": 3.0, "has_target": True,
             "entry_time": "09:03:31", "warn": None, "live": True, "qty": 1},
            {"rec": "eod", "stage": "done", "date": TODAY, "why": "eod_not_ours",
             "why_msg": AF.WHY["eod_not_ours"] + "（進場價差了 62.0 點）",
             "ok": False, "alarm": True, "at": "13:43:31", "eod_at": LP.EOD_CLOSE_AT,
             "dir": "long", "entry": 12013.0, "tries": 0, "live": True},
        ]
        sim.append((TODAY, 12010.0))
    if ST["rows"] == "eodcanttell":
        # ⛔⛔ 「查不到那一口的下場」（2026-09-09 lab-qa 退件 M3 的 Y1／Y2）：
        #    舊版這三條路全落到 eod_done_elsewhere ⇒ 畫面寫「先前已經平掉了」，
        #    但**部位其實還開著**，而且不示警。這一組治具就是在畫面上把它擋住。
        rows += [
            {"rec": "result", "stage": "done", "date": TODAY, "method": "B",
             "dir": "long", "px": 12010.0, "ok": True, "why": None, "why_msg": None,
             "entry": 12013.0, "tp": 12113.0, "slip": 3.0, "has_target": True,
             "entry_time": "09:03:31", "warn": None, "live": True, "qty": 1},
            {"rec": "eod", "stage": "done", "date": TODAY, "why": "eod_cant_tell",
             "why_msg": AF.WHY["eod_cant_tell"] + " —— 問不到券商的已實現損益（對不了帳）",
             "ok": False, "alarm": True, "at": "13:43:31", "eod_at": LP.EOD_CLOSE_AT,
             "dir": "long", "entry": 12013.0, "tries": 0, "live": True},
        ]
        sim.append((TODAY, 12010.0))
    if ST["rows"] == "mixed":
        # 過去幾天：每一種「沒送」各一天（⛔ 每一種都要在畫面上看得到原因）
        cases = [
            (1, {"rec": "result", "stage": "done", "method": "B", "dir": "short",
                 "px": 11990.0, "ok": True, "why": None, "why_msg": None,
                 "entry": 11987.0, "tp": 11887.0, "slip": 3.0, "has_target": True,
                 "live": True, "qty": 1}, 11990.0),
            (2, {"rec": "skip", "why": "off", "why_msg": AF.WHY["off"]}, 12005.0),
            (3, {"rec": "skip", "why": "quote_stale", "method": "B",
                 "why_msg": AF.WHY["quote_stale"]}, None),
            (4, {"rec": "skip", "why": "late", "why_msg": AF.WHY["late"]}, 12002.0),
            (5, {"rec": "skip", "why": "bad_method", "arm_raw": "K線",
                 "why_msg": AF.WHY["bad_method"] + "：讀到「K線」，只認得 A 或 B"}, 12001.0),
            (6, {"rec": "result", "stage": "done", "method": "A", "ok": False,
                 "why": "cant_enter", "live": True,
                 "why_msg": AF.WHY["cant_enter"] + "：券商已經有部位了"}, 12007.0),
            (7, {"rec": "fire", "stage": "sending", "method": "B", "dir": "long",
                 "px": 12003.0, "live": True}, 12003.0),
            (8, {"rec": "skip", "why": "no_signal", "method": "B",
                 "why_msg": AF.WHY["no_signal"]}, 12004.0),
        ]
        for n, base, px in cases:
            r = dict(base)
            r["date"] = _d(n)
            rows.append(r)
            sim.append((_d(n), px))
    for r in rows:
        with (AF.FIRE_DIR / (r["date"][:7] + ".jsonl")).open("a", encoding="utf-8") as f:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    _sim_rows(sim)


def _apply_arm():
    v = ST["arm"]
    # 上一輪按「關閉」留下來的 .off-* 檔要清掉，不然探針數不準
    for p in AF.ARM_FLAG.parent.glob(AF.ARM_FLAG.name + ".off-*"):
        p.unlink()
    if v == "off":
        if AF.ARM_FLAG.exists():
            AF.ARM_FLAG.unlink()
    else:
        AF.ARM_FLAG.write_text({"junk": "K線"}.get(v, v), encoding="utf-8")
    if ST["live"]:
        broker.REAL_FLAG.write_text("on", encoding="utf-8")
    elif broker.REAL_FLAG.exists():
        broker.REAL_FLAG.unlink()


_rebuild()
_apply_arm()

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
        raw = self.rfile.read(n)
        # ⭐⭐ 2026-09-09（lab-qa P0）：產品的 `do_POST` 是**在入口**過守衛的
        #    （每一個 POST，不是只有 /api/fire/on），治具照抄同一個位置。
        #    ⛔ 治具只在 /api/fire/on 過守衛的話，「關閉那顆忘了帶標頭」會全綠。
        ok, code, msg = LP.fire_post_guard(self.headers)
        if not ok:
            BLOCKED.append((self.path, code))
            return self._j(code, {"ok": False, "msg": msg})
        # ⭐⭐ 打開自動下單（2026-09-09 加）。⛔ 這裡**故意**走產品自己的
        #    `LP.fire_post_guard()` ＋ `LP.fire_arm_on()`：治具另寫一份的話，
        #    「前端少帶一個標頭、後端會擋」這件事在探針上會**全綠**
        #    （那正是 2026-09-09 退件 M1 的形狀：治具重寫 handler ⇒ 探針沒打到產品）。
        #    ⚠️ 端點路由本身與六道防護的每一道，由 `test_fire_routes.py` 真的起服務打。
        if self.path == "/api/fire/on":
            try:
                body = json.loads(raw or b"{}")
            except Exception:
                body = {}
            code, out = LP.fire_arm_on(body.get("mode"), who="harness")
            if out.get("ok"):
                ST["arm"] = out.get("method") or ST["arm"]   # 治具狀態跟著同步
            return self._j(code, out)
        # ⭐ 關閉自動下單。
        #    ⛔ 走的是**產品自己的** `auto_fire.disarm()`（治具另寫一份的話，
        #       「治具對、產品錯」會全綠 —— 這就是 2026-09-09 退件 M1 的形狀）。
        #    ⚠️ 產品路由本身（`/api/fire/off` 這個字串）由 `test_fire_routes.py` 守，
        #       那一支真的起 `live_panel.Handler` 打進去。
        if self.path == "/api/fire/off":
            ok, msg = AF.disarm()
            if ok:
                ST["arm"] = "off"          # 治具的狀態跟著同步，重建資料集才不會又寫回去
            return self._j(200 if ok else 409,
                           {"ok": ok, "msg": msg, "armed": AF.arm()["on"]})
        # ⛔ 除了上面那一顆，這一頁不該打到這裡來（探針會攔請求驗證）
        return self._j(404, {"ok": False, "msg": "治具不送單"})

    def do_GET(self):
        p = self.path
        # ⛔ 武裝那顆只收 POST（產品是 405；治具照抄同一個語意）
        if p.split("?", 1)[0] == "/api/fire/on":
            return self._j(405, {"ok": False, "msg": "這個端點只收 POST"})
        if p.startswith("/api/fire/state"):
            out = AF.state()
            out["sim"] = LP.fire_sim_pairs(out.get("days") or [])
            # ⛔ 兩段式確認那句話與 token 都走**產品的**那一份（見上面 do_POST 的理由）
            out["arm_confirm"] = LP.fire_arm_confirm(out.get("live"), _fake_now())
            out["token"] = LP.FIRE_TOKEN
            out["now"] = ST["clock"]           # ⛔ 前端一律用後端時鐘
            return self._j(200, out)
        if p.startswith("/api/state"):
            s = dict(STATE)
            s["clock"] = ST["clock"]
            # ⛔ 產品的 /api/state 帶 token（前端 `pfetch()` 靠它拿），治具照抄；
            #    少了它這一頁每一顆鈕都會被守衛擋成 403。
            s["token"] = LP.FIRE_TOKEN
            return self._j(200, s)
        if p.startswith("/api/"):
            return self._j(200, {})
        if p.startswith("/manifest.webmanifest"):
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
        if p.startswith("/f/arm"):
            ST["arm"] = p.rsplit("/", 1)[1]
            _apply_arm()
            return self._j({"arm": ST["arm"], "state": AF.arm()})
        if p.startswith("/f/live"):
            ST["live"] = p.rsplit("/", 1)[1] in ("1", "on", "yes")
            _apply_arm()
            return self._j({"live": broker.is_live()})
        if p.startswith("/f/rows"):
            ST["rows"] = p.rsplit("/", 1)[1]
            _rebuild()
            return self._j({"rows": ST["rows"]})
        if p.startswith("/f/clock"):
            ST["clock"] = p.split("/f/clock", 1)[1].lstrip("/") or "10:30:00"
            return self._j({"clock": ST["clock"]})
        if p.startswith("/f/now"):
            # ⭐ R2：塞一個假的「現在」給 `fire_arm_confirm()`（⛔ 只影響那句話的
            #    「今天／下一個交易日」，⛔ 不動任何送單邏輯）。空的 ＝ 用真的現在。
            ST["now"] = p.split("/f/now", 1)[1].lstrip("/") or None
            return self._j({"now": ST["now"],
                            "fires_today": LP.fire_fires_today(_fake_now())})
        if p.startswith("/f/blocked"):
            return self._j({"blocked": list(BLOCKED)})
        if p.startswith("/f/reset"):
            ST.update({"arm": "off", "live": False, "rows": "mixed",
                       "clock": "10:30:00", "now": None})
            BLOCKED.clear()
            _rebuild()
            _apply_arm()
            return self._j({"ok": True})
        if p.startswith("/f/where"):
            # 「打開」那一列紀錄（arm-YYYY-MM.jsonl）—— ⛔ 刻意跟 YYYY-MM.jsonl 分開，
            # 那個檔有硬不變式 fire+result+skip+eod+bad ＝ 總列數。
            _arm_rows = []
            if AF.FIRE_DIR.exists():
                for _p in sorted(AF.FIRE_DIR.glob("arm-*.jsonl")):
                    for _ln in _p.read_text(encoding="utf-8").splitlines():
                        if _ln.strip():
                            try:
                                _arm_rows.append(json.loads(_ln))
                            except Exception:
                                _arm_rows.append({"bad": _ln[:80]})
            return self._j({"dir": str(TMP), "today": TODAY, "arm_rows": _arm_rows,
                            "ledger": AF.read_all()[1],
                            "off_files": sorted(
                                x.name for x in AF.ARM_FLAG.parent.glob(
                                    AF.ARM_FLAG.name + ".off-*")),
                            "real_flag_exists": AF.ARM_FLAG.exists(),
                            "prod_flag_exists": (pathlib.Path(AF.__file__).parent
                                                 / "AUTO_ORDERS_ON").exists()})
        return self._j({"error": "?"})


if __name__ == "__main__":
    threading.Thread(target=lambda: ThreadingHTTPServer(("127.0.0.1", CTL), C).serve_forever(),
                     daemon=True).start()
    print(f"治具資料：{TMP}")
    print(f"今天：{TODAY}　開關：{ST['arm']}　真單：{ST['live']}")
    print(f"⛔ 送單執行緒沒起來（started={AF._ST['started']}）、"
          f"掛勾維持 no-op（{LP.AUTO_SIG_HOOK.__name__}）")
    print(f"http://127.0.0.1:{PORT}/　（控制埠 {CTL}）")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
