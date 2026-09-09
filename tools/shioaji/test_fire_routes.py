# -*- coding: utf-8 -*-
"""
【自動下單】那兩個端點的守衛 —— **真的起一個服務、真的打進去。**

⛔⛔ 為什麼要有這一支（2026-09-09 lab-qa 退件 M1）：
   `test_auto_fire.py` ⑩ 原本用 `"/api/fire/state" in LPSRC` 當「這個端點存在」的證據，
   但**那個字串在 `live_panel.py` 裡出現兩次**，第二次是前端的 `fetch()` ⇒
   **後端路由改壞、刪掉、改名，那條斷言結構上永遠是真**。
   lab-qa 實測：把路由改成 `/api/fire/statXX` ⇒ **181/181 ＋ 73/73 全綠**。
   `fire-tab.mjs` 也抓不到，因為 `fire_harness.py` 自己重寫了一份 handler
   ⇒ **探針從頭到尾沒打到產品的路由**。

   CLAUDE.md 早就有這條規則：「**端點的測試一定要真的起服務打進去**
   （`ThreadingHTTPServer` 綁埠 0，⛔ 不是 8770）」。這一支就是那條規則的落實。

⚠️⚠️ **「回 200」不是證據** —— `do_GET` 的 fallthrough 會把整張 `PAGE`（HTML）以 200 端出來。
   所以路由壞掉時 `GET /api/fire/state` 仍然是 200，只是內容變成 HTML。
   斷言一律是「**回得出 JSON 而且裡面有 `armed`**」，另外用一個一定不存在的路徑當**尺的自證**
   （證明「沒中路由 ⇒ 回 HTML」這件事是真的）。

⛔ 不連永豐、不送任何單：`broker.enter` / `broker.close` 一被呼叫就記一筆並回失敗，
   收尾斷言「全程一次都沒被呼叫」。每一個會寫檔／讀開關的路徑都導到暫存區，
   而且 ⛔ **斷言真的 `tools/shioaji/AUTO_ORDERS_ON` 不存在**。

怎麼跑（PowerShell）：
    $env:PYTHONIOENCODING="utf-8"; $env:PYTHONUTF8="1"
    & "…\\trade-log\\.venv\\Scripts\\python.exe" "…\\tools\\shioaji\\test_fire_routes.py"

⚠️ 環境變數 `LP_SRC_DIR` 給突變測試用（`tools/probe/fire-mutate.py` 第二組）：
   指到一份**放在暫存區**的 `live_panel.py`，⛔ 絕不把壞版本寫進 tools/shioaji
   （看門狗是活的）。
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
# ⚠️ 順序有關係：突變版那一份要排在**前面**才會被載到
#    （寫反的話 LP_SRC_DIR 等於沒生效 ⇒ 整組路由突變會全部「打不紅」）。
sys.path.insert(0, str(HERE))
_MUT = os.environ.get("LP_SRC_DIR")
if _MUT:
    sys.path.insert(0, _MUT)          # 突變版的 live_panel.py（在暫存區）

import broker                       # noqa: E402
import auto_fire as AF              # noqa: E402
import live_panel as LP             # noqa: E402

FAIL = 0


def say(cond, name, extra=""):
    global FAIL
    FAIL += not cond
    print(("  OK   " if cond else "  FAIL ") + name + (f"  {extra}" if extra else ""))


def chk(name, got, want):
    global FAIL
    ok = got == want
    FAIL += not ok
    print(("  OK   " if ok else "  FAIL ") + name
          + ("" if ok else f"  (得到 {got!r}，期待 {want!r})"))


def _boom(kind, err, tb):
    """⛔ 自己掛掉時要印出一項具名的 FAIL ＋ 總結行（突變測試只數 FAIL 行會有假綠燈）。"""
    import traceback
    traceback.print_exception(kind, err, tb)
    print("  FAIL ⛔ 測試自己掛掉了（未捕捉的例外）：" + str(err)[:120])
    print("⛔ 有 ? 項沒過（測試中斷）")
    sys.stdout.flush()


sys.excepthook = _boom

# ⛔ 每一個會寫檔／讀開關的地方都導到暫存區。**漏掉一個就是污染他的真實紀錄。**
TMP = pathlib.Path(tempfile.mkdtemp(prefix="fire-routes-"))
REAL_ARM = AF.ARM_FLAG
REAL_PATHS = {"broker.ORDER_DIR": broker.ORDER_DIR, "broker.TRADE_DIR": broker.TRADE_DIR,
              "broker.REAL_FLAG": broker.REAL_FLAG, "AF.ARM_FLAG": AF.ARM_FLAG,
              "AF.FIRE_DIR": AF.FIRE_DIR, "LP.AUTO_DIR": LP.AUTO_DIR,
              "LP.AUTO_REAL_DIR": LP.AUTO_REAL_DIR,
              # ⛔ /api/replay 那條路會寫檔（`save_replay`）。2026-09-09 加測那條路時
              #    一起導走 —— 漏掉就是往他真的 replay_log/ 寫進測試垃圾。
              "LP.REPLAY_DIR": LP.REPLAY_DIR}
broker.ORDER_DIR = TMP / "real_orders"
broker.TRADE_DIR = TMP / "real_trades"
broker.REAL_FLAG = TMP / "REAL_ORDERS_ON"
AF.ARM_FLAG = TMP / "AUTO_ORDERS_ON"
AF.FIRE_DIR = TMP / "autofire"
LP.AUTO_DIR = TMP / "autotest"
LP.AUTO_REAL_DIR = TMP / "real_trades"
LP.REPLAY_DIR = TMP / "replay_log"

# ⛔⛔ 出貨狀態就是「那個檔不存在」。這一支跑之前先確認一次。
if REAL_ARM.exists():
    print("  FAIL ⛔ tools/shioaji/AUTO_ORDERS_ON 竟然存在！拒絕往下跑。")
    print("⛔ 有 1 項沒過")
    sys.exit(1)

# ⛔ 一被呼叫就記一筆（收尾斷言全程 0 次）。⛔ 不連永豐、不送單。
SENT = []
broker.enter = lambda *a, **k: (SENT.append(("enter",) + a) or (False, "測試治具", None))
broker.close = lambda *a, **k: (SENT.append(("close",) + a) or (False, "測試治具"))

# 跟 live_panel.main() 一樣的接線（⛔ 常數與算式的正本都在 live_panel）——
# ⛔ 但**不** start()、⛔ 也不動 AUTO_SIG_HOOK / AUTO_EOD_HOOK ⇒ 這一支永遠不會送單。
AF.configure(signal_at=LP.SIGNAL_AT, signal_sec=LP.SIGNAL_SEC, late_ms=LP.AUTO_LATE_MS,
             gap_s=LP.AUTO_GAP_S, tp_points=LP.TP_POINTS,
             sig_fn=LP.auto_sig, dirs_fn=LP.auto_dirs, eod_at=LP.EOD_CLOSE_AT)

srv = ThreadingHTTPServer(("127.0.0.1", 0), LP.Handler)
PORT = srv.server_address[1]
assert PORT != 8770, "⛔ 不可以用 8770（他的面板正開著）"
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}"


def _req(path, method="GET", body=None):
    rq = urllib.request.Request(BASE + path, data=body, method=method)
    try:
        with urllib.request.urlopen(rq, timeout=15) as r:
            return r.status, r.headers.get("Content-Type", ""), \
                r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), \
            e.read().decode("utf-8", "replace")


def get(p):
    return _req(p)


def post(p, body=b"{}"):
    return _req(p, "POST", body)


def as_json(s):
    try:
        return json.loads(s)
    except Exception:
        return None


print(f"=== 真的起一個服務打進去（埠 {PORT}，⛔ 不是 8770）===")

# ── ① 尺的自證：先證明「路由沒中的時候回的是 HTML 頁面」，不是 404 也不是 JSON
print("\n=== ① 尺的自證（⛔ 沒有這一段，下面那條斷言不算數）===")
c, ct, b = get("/api/fire/statXX")
say(as_json(b) is None and "html" in ct.lower(),
    "  一個不存在的路徑 ⇒ 回的是整張 HTML 頁面（**不是 JSON**）",
    f"{c} {ct[:40]}")
say(c == 200, "    而且照樣是 200 ⇒ ⛔「回 200」不可以當成端點存在的證據", str(c))

# ── ② GET /api/fire/state 真的存在，而且端得出產品的東西
print("\n=== ② GET /api/fire/state ===")
c, ct, b = get("/api/fire/state")
j = as_json(b) or {}
say("json" in ct.lower() and isinstance(as_json(b), dict),
    "  回得出 JSON（⇒ 路由真的中了）", f"{c} {ct[:40]} {b[:60]}")
chk("  armed == False（出貨狀態）", j.get("armed"), False)
chk("  flag_exists == False", j.get("flag_exists"), False)
chk("  講得出開關檔叫什麼", j.get("flag"), "AUTO_ORDERS_ON")
say("sim" in j, "  帶得出跟模擬那一頁的對照（fire_sim_pairs）", str(list(j))[:80])
chk("  帶得出收盤平倉的時刻（正本在 live_panel）", j.get("eod_at"), LP.EOD_CLOSE_AT)
say(isinstance(j.get("days"), list) and isinstance(j.get("ledger"), dict),
    "  帶得出每一天送了沒與帳本")
say(j.get("why_texts") == dict(AF.WHY), "  原因的正本跟後端一致（前後端不分岔）")

# ── ③ POST /api/fire/off：⭐ 這一頁唯一會改變狀態的動作，⛔ 而且只會關
print("\n=== ③ POST /api/fire/off（⭐ 唯一的 POST，⛔ 只會關不會開）===")
c, ct, b = post("/api/fire/off")
j = as_json(b) or {}
say(as_json(b) is not None, "  回得出 JSON（⇒ 路由真的中了）", f"{c} {b[:80]}")
say(j.get("ok") is True, "  開關本來就關著時也回成功（兩個視窗各按一次不會出錯）", str(j))
say(not AF.ARM_FLAG.exists(),
    "  ⛔⛔ 而且**沒有把開關建出來**（這條路結構上只會關）")

AF.ARM_FLAG.parent.mkdir(parents=True, exist_ok=True)
AF.ARM_FLAG.write_text("A", encoding="utf-8")
c, ct, b = get("/api/fire/state")
j = as_json(b) or {}
say(j.get("armed") is True and j.get("flag_exists") is True,
    "  前置：把開關打開之後端點看得到（armed=True）", str(j.get("arm_msg")))

c, ct, b = post("/api/fire/off")
j = as_json(b) or {}
say(j.get("ok") is True, "  按下「關閉」回報成功", str(j.get("msg")))
say(not AF.ARM_FLAG.exists(), "  ⛔ 開關檔真的不在了")
_kept = [p for p in AF.ARM_FLAG.parent.iterdir()
         if p.name.startswith(AF.ARM_FLAG.name + ".off-")]
chk("  是改名不是刪掉（他寫的內容留著）", len(_kept), 1)
chk("    內容真的留著", _kept[0].read_text(encoding="utf-8").strip(), "A")
c, ct, b = get("/api/fire/state")
j = as_json(b) or {}
chk("  ⇒ 端點立刻變成「關閉中」", (j.get("armed"), j.get("arm_why")), (False, "off"))
chk("    那顆鈕也跟著不見（flag_exists）", j.get("flag_exists"), False)

# ── ④ ⛔ 除了那一顆，沒有任何「開啟／改設定」的端點
print("\n=== ④ ⛔ 沒有任何「開啟自動下單」的端點 ===")
# ⛔⛔ 這裡**一定要有前綴撞名的那幾條**（2026-09-09 lab-qa 突變 Q12 打不紅）：
#    把 `self.path == "/api/fire/off"` 換成 `startswith` 的話，
#    `/api/fire/offXX`、`/api/fire/off/on`、`/api/fire/off?arm=A` 全都會中 ——
#    而舊的名單裡一條前綴撞名的都沒有 ⇒ 那個突變結構上打不紅。
#    ⚠️ 這一條的意義不只是「路由要精確」：那顆按鈕是**這一頁唯一會改變狀態的動作**，
#    路由比對放寬＝多開了一批沒人審過的入口。
for p in ("/api/fire/on", "/api/fire/arm", "/api/fire/state", "/api/fire",
          "/api/fire/method", "/api/fire/state?arm=A", "/api/fire/eod",
          "/api/fire/offXX", "/api/fire/off/on", "/api/fire/off?arm=A",
          "/api/fire/off/", "/api/fire/offon"):
    c, ct, b = post(p)
    say(c in (404, 405, 400), f"  POST {p} 被拒絕（{c}）", (b or "")[:60])
    say(not AF.ARM_FLAG.exists(), f"    ⇒ 而且開關仍然沒有被建出來（{p}）")
# 尺的自證：⛔ 精確的那一條**現在仍然通得過**（不然上面那一整排是恆真的）
c, ct, b = post("/api/fire/off")
say(as_json(b) is not None and (as_json(b) or {}).get("ok") is True,
    "  尺的自證：精確比對的 /api/fire/off 照樣通", f"{c} {b[:60]}")

# ── ④b ⛔ POST /api/replay 要驗欄位（⛔ 空 body 不可以回 200 並寫一列全 null）
print("\n=== ④b ⛔ POST /api/replay 的欄位驗證 ===")
# ⚠️ 這是既有的洞（不是這一輪長出來的）：舊版收空 body 也回 200，
#    並在 replay_log/ 寫進一列**每個欄位都是 null** 的紀錄 —— 它會被算進勝率統計，
#    而畫面上看不出那一列是垃圾。比照 /api/enter 加欄位驗證。
_rp_dir = LP.REPLAY_DIR


def _rp_files():
    return sorted(p.name for p in _rp_dir.glob("*.json")) if _rp_dir.exists() else []


_rp0 = _rp_files()
for name, payload in (
        ("空 body", b"{}"),
        ("完全空的（連 JSON 都不是）", b""),
        ("date 是 null", b'{"date":null,"judged":false}'),
        ("date 形狀不對", b'{"date":"2026/09/09","judged":false}'),
        ("date 想跳出資料夾", b'{"date":"../../boom","judged":false}'),
        # ⚠️ 下面這幾條是**只有 judged 那道檢查抓得到**的（2026-09-09 補）：
        #    上面那些壞 body 就算把 judged 那一道拿掉，也會被後面的 dir／entry 擋下來
        #    ⇒ 那個突變**打不紅**。要打紅就得挑「judged 是假的 ⇒ 直接略過 dir／entry」
        #    的形狀，那正是「一列全 null 寫進 replay_log/」真正的樣子。
        ("⛔ 根本沒給 judged（那一列會寫成 null）", b'{"date":"2026-09-09"}'),
        ("⛔ judged 是 null", b'{"date":"2026-09-09","judged":null}'),
        ("⛔ judged 是空字串（falsy ⇒ 會略過 dir／entry）",
         b'{"date":"2026-09-09","judged":""}'),
        ("⛔ judged 是 0", b'{"date":"2026-09-09","judged":0}'),
        ("judged 不是布林", b'{"date":"2026-09-09","judged":"yes"}'),
        ("說有判斷卻沒有方向", b'{"date":"2026-09-09","judged":true,"entry":12000}'),
        ("說有判斷卻沒有進場價",
         b'{"date":"2026-09-09","judged":true,"dir":"long"}'),
        ("方向不是 long／short",
         b'{"date":"2026-09-09","judged":true,"dir":"up","entry":12000}')):
    c, ct, b = post("/api/replay", payload)
    say(c == 400, f"  {name} ⇒ 被擋下來（{c}）", (b or "")[:70])
say(_rp_files() == _rp0, "  ⛔⛔ 而且一個字都沒有寫進 replay_log/", str(_rp_files()))
# 尺的自證：⛔ 正常的那一種**照樣寫得進去**（不然上面整排是恆真的）
c, ct, b = post("/api/replay",
                b'{"date":"2026-09-09","judged":true,"dir":"long","entry":12000,'
                b'"time":"09:20","note":"__tmp__"}')
j = as_json(b) or {}
say(c == 200 and j.get("ok") is True,
    "  尺的自證：欄位齊全的那一種照樣收（200）", f"{c} {b[:70]}")
say("2026-09-09.json" in _rp_files(),
    "    而且真的寫進**暫存區**的 replay_log/（⛔ 不是他真的那個）", str(_rp_files()))
c, ct, b = post("/api/replay", b'{"date":"2026-09-09","judged":false}')
say(c == 200, "  尺的自證：沒有判斷（judged=false）那一種也照樣收", f"{c} {b[:70]}")

# ── ⑤ ⛔ 全程一張單都沒有出去、也沒有碰他真的資料夾
print("\n=== ⑤ ⛔ 收尾 ===")
chk("  ⛔ broker.enter / broker.close 全程 0 次", SENT, [])
for k, real in REAL_PATHS.items():
    now = {"broker.ORDER_DIR": broker.ORDER_DIR, "broker.TRADE_DIR": broker.TRADE_DIR,
           "broker.REAL_FLAG": broker.REAL_FLAG, "AF.ARM_FLAG": AF.ARM_FLAG,
           "AF.FIRE_DIR": AF.FIRE_DIR, "LP.AUTO_DIR": LP.AUTO_DIR,
           "LP.AUTO_REAL_DIR": LP.AUTO_REAL_DIR, "LP.REPLAY_DIR": LP.REPLAY_DIR}[k]
    say(now != real and str(TMP) in str(now), f"  {k} 全程都在暫存區", str(now))
say(not REAL_ARM.exists(),
    "  ⛔⛔ 真的 AUTO_ORDERS_ON **不存在**（測試絕對不可以把它建出來）")

srv.shutdown()
shutil.rmtree(TMP, ignore_errors=True)
print("\n" + ("全部通過 ✅" if FAIL == 0 else f"⛔ 有 {FAIL} 項沒過"))
sys.exit(1 if FAIL else 0)
