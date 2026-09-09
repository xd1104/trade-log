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
import re
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

# ⚠️ 只拿來對「路由名單有沒有列全」——⛔ 這一支的斷言一律是行為面的（真的打進去），
#    ⛔ 不准用「某個字串在原始碼裡」當某個端點存在的證據（那是退件 M1 的形狀）。
LPSRC = pathlib.Path(LP.__file__).read_text(encoding="utf-8")

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


def _req(path, method="GET", body=None, headers=None):
    rq = urllib.request.Request(BASE + path, data=body, method=method,
                                headers=headers or {})
    try:
        with urllib.request.urlopen(rq, timeout=15) as r:
            return r.status, r.headers.get("Content-Type", ""), \
                r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), \
            e.read().decode("utf-8", "replace")


def get(p):
    return _req(p)


def _hdr(**over):
    """⭐ 一個「完全合格」的 POST 標頭組（＝他的面板 `pfetch()` 送出來的那一種）。

    ⛔⛔ 2026-09-09 lab-qa 的 P0 之後，**每一個** POST 都要過 `fire_post_guard()`，
       所以這一支裡凡是「應該成功」的 POST 都得帶齊；⛔ 少帶就是 415／403，
       那正是下面那一整節在驗的事。
    """
    h = {"Content-Type": "application/json", "X-Panel": "1",
         "X-Panel-Token": LP.FIRE_TOKEN, "Sec-Fetch-Site": "same-origin"}
    h.update(over)
    return h


def post(p, body=b"{}", headers=None):
    return _req(p, "POST", body, _hdr() if headers is None else headers)


# ⭐⭐ 打開自動下單那一顆的請求產生器。**預設是「完全合格」的那一種**，
#    每一項測試只把**一樣東西**弄壞 ⇒ 哪一道防護被拿掉，就只有那一項會變綠。
#    ⚠️ `_KILL` 這個哨兵是用來「把某個標頭整個拿掉」的（跟「設成空字串」不同）。
_KILL = object()


def post_on(mode="A", ct="application/json", panel="1", origin=None, token=None,
            sfs="same-origin", host=None, raw=None, method="POST"):
    body = raw if raw is not None else json.dumps(
        {"mode": mode} if mode is not _KILL else {}).encode()
    hdr = {}
    if ct is not _KILL:
        hdr["Content-Type"] = ct
    if panel is not _KILL:
        hdr["X-Panel"] = panel
    if origin is not None:
        hdr["Origin"] = origin
    hdr["X-Panel-Token"] = LP.FIRE_TOKEN if token is None else token
    if token is _KILL:
        hdr.pop("X-Panel-Token")
    if sfs is not None:
        hdr["Sec-Fetch-Site"] = sfs
    if host is not None:
        hdr["Host"] = host
    rq = urllib.request.Request(BASE + "/api/fire/on", data=body, method=method,
                                headers=hdr)
    try:
        with urllib.request.urlopen(rq, timeout=15) as r:
            return r.status, as_json(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return e.code, as_json(e.read().decode("utf-8", "replace"))


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


# ── ③b ⭐⭐ POST /api/fire/on：**這是唯一一顆會武裝真錢的鈕**
print("\n=== ③b ⭐⭐ POST /api/fire/on（⛔ 按下去就是武裝真錢）===")


def arm_off():
    """把暫存區的開關收乾淨（⛔ 走產品的 disarm()，不自己 unlink）。"""
    AF.disarm()
    for p in AF.ARM_FLAG.parent.glob(AF.ARM_FLAG.name + ".off-*"):
        p.unlink()


arm_off()

# 尺的自證：⛔ 沒有這一段，下面那一整排「被擋下來」全部是恆真的
c, j = post_on("A")
say(c == 200 and (j or {}).get("ok") is True, "  尺的自證：完全合格的請求 ⇒ 真的打得開",
    f"{c} {str(j)[:90]}")
say(AF.ARM_FLAG.exists(), "    開關檔真的建出來了（⛔ 在暫存區）")
chk("    內容就是一個 ASCII 大寫字母（⛔ 沒有 BOM、沒有換行）",
    AF.ARM_FLAG.read_bytes(), b"A")
chk("    而且 auto_fire 讀得懂（跟讀開關那條路對得上）",
    (AF.arm()["on"], AF.arm()["method"]), (True, "A"))
# ⛔ 已經開著再按 ⇒ 409，⛔ 不覆蓋、⛔ 不當成換做法
c, j = post_on("B")
say(c == 409 and "已經開著" in ((j or {}).get("msg") or ""),
    "  ⛔ 已經開著再按 ⇒ 409（⛔ 不是換做法）", f"{c} {str(j)[:80]}")
chk("    ⛔⛔ 而且原本那個檔一個位元組都沒被動到", AF.ARM_FLAG.read_bytes(), b"A")
arm_off()
c, j = post_on("B")
say(c == 200 and AF.ARM_FLAG.read_bytes() == b"B", "  關掉之後可以改用另一個做法",
    f"{c} {AF.ARM_FLAG.read_bytes()!r}")
arm_off()

# ── ③b-1 ⛔⛔ 四道防護：**每一道單獨拿掉都要有一項在這裡變綠**
#    （每一個請求只壞一樣東西 ⇒ 只有對應那一道擋得住它）
print("\n  ── ⛔⛔ 防護（⛔ 每一道單獨拿掉都有繞法，缺一道等於沒有）")
_GUARD = [
    # （名字, kwargs, 期待的碼, 這一條在守哪一道）
    ("① Content-Type 是 text/plain（⛔ 表單送得出這種）",
     dict(ct="text/plain"), 415),
    ("① Content-Type 是表單（application/x-www-form-urlencoded）",
     dict(ct="application/x-www-form-urlencoded"), 415),
    ("① Content-Type 是 multipart/form-data（<form> 上傳那種）",
     dict(ct="multipart/form-data; boundary=x"), 415),
    ("① 根本沒有 Content-Type", dict(ct=_KILL), 415),
    # ⛔ 只認**逐字**的 application/json：寫成「含有 json 就算」會多開一批
    #    沒人審過的型別（2026-09-09 fire-mutate Ⓖ1b 打不紅，補的就是這一條）
    ("① Content-Type 是 application/jsonx（⛔ 不准「含有 json 就算」）",
     dict(ct="application/jsonx"), 415),
    ("① Content-Type 是 text/json", dict(ct="text/json"), 415),
    ("② 沒有自訂標頭 X-Panel（⛔ 簡單表單送不出自訂標頭）",
     dict(panel=_KILL), 403),
    ("② X-Panel 值不對", dict(panel="0"), 403),
    ("③ Origin 是別的網站（⛔ 跨站的 POST 一定帶 Origin）",
     dict(origin="https://evil.example"), 403),
    ("③ Origin 是 null（sandbox iframe／file://）", dict(origin="null"), 403),
    ("③ Origin 是看起來像本機的網域（⛔ 127.0.0.1.evil.com）",
     dict(origin="http://127.0.0.1.evil.com"), 403),
    # ⛔ 形狀（2026-09-09 lab-qa 建議 4）：真的 Origin 只有 scheme://host[:port]。
    #    `urlsplit("http://evil@127.0.0.1").hostname` 是 127.0.0.1 ⇒ 舊寫法會放行。
    ("③ Origin 帶了帳號（⛔ http://evil@127.0.0.1）",
     dict(origin="http://evil@127.0.0.1"), 403),
    ("③ Origin 帶了密碼（⛔ http://a:b@127.0.0.1:8770）",
     dict(origin="http://a:b@127.0.0.1:8770"), 403),
    ("③ Origin 帶了路徑（⛔ 真的 Origin 沒有路徑）",
     dict(origin="http://127.0.0.1:8770/"), 403),
    ("③ Origin 帶了查詢字串", dict(origin="http://127.0.0.1?x=1"), 403),
    ("③ Origin 帶了 fragment", dict(origin="http://127.0.0.1#x"), 403),
    ("③ Origin 不是 http／https（⛔ chrome-extension://…）",
     dict(origin="chrome-extension://127.0.0.1"), 403),
    ("④ 沒有 X-Panel-Token（⛔ 跨站讀不到 /api/fire/state 就拿不到它）",
     dict(token=_KILL), 403),
    ("④ token 猜錯", dict(token="0" * 32), 403),
    ("④ token 空字串", dict(token=""), 403),
    # ⛔⛔ 「放寬型」的兩條（2026-09-09 lab-qa 建議 3：QA 打不紅的那種突變）。
    #    上面「token 猜錯 ＝ 0*32」擋不住「只比前 8 碼」那個弱化 —— 0*32 的前 8 碼
    #    本來就不一樣。要打紅就得送一個**前 8 碼對、後面全錯**的 token。
    ("④ token 只有前 8 碼對（⛔ 不准只比前綴）",
     dict(token=(LP.FIRE_TOKEN[:8] + "0" * (len(LP.FIRE_TOKEN) - 8))), 403),
    ("④ token 對但後面多了一截（⛔ 不准 startswith）",
     dict(token=LP.FIRE_TOKEN + "x"), 403),
    ("④ token 只有前 8 碼（⛔ 長度也要一樣）",
     dict(token=LP.FIRE_TOKEN[:8]), 403),
    # ⛔ 非 ASCII：`compare_digest` 對非 ASCII 的 str 會 raise ⇒ 舊寫法會回 500
    #    （擋是擋住了，但**用例外當防線**，下一個人一個 try/pass 就破功）
    ("④ token 是非 ASCII（⛔ 不可以靠例外擋）", dict(token="ÿ" * 32), 403),
    ("⑤ Sec-Fetch-Site: cross-site（瀏覽器自己加的，網頁改不掉）",
     dict(sfs="cross-site"), 403),
    ("⑤ Sec-Fetch-Site: same-site（同網域不同來源也不算）",
     dict(sfs="same-site"), 403),
    ("⑥ Host 是別的網域（⛔ DNS rebinding：evil.com 指到 127.0.0.1）",
     dict(host="evil.example:80"), 403),
    # ⛔⛔ 「放寬型」（同上）：`h in FIRE_LOOPBACK` 被改成 `h.endswith(...)` 的話，
    #    上面那條 evil.example 照樣被擋 ⇒ **打不紅**。要打紅就得送一個「結尾剛好是
    #    本機位址」的主機名。
    ("⑥ Host 結尾剛好是本機位址（⛔ 不准 endswith：evil-127.0.0.1）",
     dict(host="evil-127.0.0.1:80"), 403),
    ("⑥ Host 是 127.0.0.1 的子網域（⛔ x.localhost）",
     dict(host="x.localhost:8770"), 403),
    ("⑥ Host 前面掛了本機位址（⛔ 不准 startswith：127.0.0.1.evil.com）",
     dict(host="127.0.0.1.evil.com"), 403),
    ("⑥ 根本沒有 Host（HTTP/1.0 送得出來）", dict(host=""), 403),
]
for name, kw, want in _GUARD:
    c, j = post_on(**kw)
    say(c == want, f"    {name} ⇒ 被擋（{c}）", str(j)[:70])
    say(not AF.ARM_FLAG.exists(), f"      ⇒ ⛔ 而且開關**沒有**被建出來（{name[:2]}）")
# ⛔ 這一條是上面那一排的尺自證：所有標頭都對的時候照樣通得過
c, j = post_on("A")
say(c == 200 and AF.ARM_FLAG.exists(),
    "    尺的自證：六道全過的那一種照樣打得開", f"{c} {str(j)[:60]}")
arm_off()
# 沒有 Origin ⛔ 不算違規（非瀏覽器的呼叫沒有它；擋的是「別的網站的分頁」）
c, j = post_on("A", sfs=None)
say(c == 200, "    沒有 Origin／Sec-Fetch-Site（非瀏覽器）照樣通", f"{c}")
arm_off()

# ── ③b-2 ⛔ 只收 POST
print("\n  ── ⛔ 只收 POST（GET／HEAD 一律 405）")
for m in ("GET", "HEAD"):
    c, ct2, b = _req("/api/fire/on", m)
    say(c == 405, f"    {m} /api/fire/on ⇒ 405", f"{c} {(b or '')[:50]}")
    say(not AF.ARM_FLAG.exists(), f"      ⇒ ⛔ 而且開關沒有被建出來（{m}）")
c, ct2, b = _req("/api/fire/on?mode=A", "GET")
say(c == 405, "    GET /api/fire/on?mode=A ⇒ 405（⛔ 帶查詢字串也不行）", str(c))
say(not AF.ARM_FLAG.exists(), "      ⇒ ⛔ 開關沒有被建出來")

# ── ③b-3 ⛔ mode 只准 A／B，而且**先驗再寫**
print("\n  ── ⛔ mode 只准兩種（⛔ 不准寫進檔案再驗）")
for name, kw in (("C（模擬那一頁才有）", dict(mode="C")),
                 ("D", dict(mode="D")),
                 ("小寫 a（⛔ 這條路不做寬容解讀）", dict(mode="a")),
                 ("空字串", dict(mode="")),
                 ("AB", dict(mode="AB")),
                 ("數字", dict(mode=1)),
                 ("null", dict(mode=None)),
                 ("陣列", dict(mode=["A"])),
                 ("根本沒給 mode", dict(mode=_KILL)),
                 ("body 是空的", dict(raw=b"")),
                 ("body 不是 JSON", dict(raw=b"mode=A")),
                 ("body 是 JSON 但不是物件", dict(raw=b'"A"'))):
    c, j = post_on(**kw)
    say(c == 400, f"    {name} ⇒ 400", str(j)[:70])
    say(not AF.ARM_FLAG.exists(), f"      ⇒ ⛔ 開關**沒有**被建出來（{name[:6]}）")

# ── ③b-4 落地一列紀錄，⛔ 而且不可以污染帳本的不變式
print("\n  ── 落地：誰在什麼時候用哪個做法打開的")
def _arm_rows():
    out = []
    for p in sorted(AF.FIRE_DIR.glob("arm-*.jsonl")):
        out += [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()
                if x.strip()]
    return out


_before = AF.read_all()[1]
_n0 = len(_arm_rows())
c, j = post_on("B")
say(c == 200, "    前置：打開", str(c))
_logs = sorted(AF.FIRE_DIR.glob("arm-*.jsonl"))
chk("    寫進 autofire/arm-YYYY-MM.jsonl（⛔ 一個檔）", len(_logs), 1)
_rows = _arm_rows()
chk("    ⛔ 只多一列（⛔ open(\"a\") append，不是覆寫）", len(_rows) - _n0, 1)
_r = _rows[-1] if _rows else {}
say(_r.get("rec") == "arm" and _r.get("method") == "B"
    and isinstance(_r.get("at"), str) and _r.get("date") == str(LP.date.today())
    and _r.get("live") is False and _r.get("who"),
    "    那一列講得出「誰、什麼時候、哪個做法、當下是不是真錢」", str(_r)[:140])
_after = AF.read_all()[1]
chk("    ⛔⛔ 而且**不會**被算進 autofire 的帳本（bad 沒有變多）",
    (_after["bad"], _after["total"]), (_before["bad"], _before["total"]))
arm_off()

# ── ③b-5 ⛔ 前綴撞名（同 ④ 的道理：路由放寬＝多開一批沒人審過的入口）
print("\n  ── ⛔ 路由是精確比對")
for p in ("/api/fire/onXX", "/api/fire/on/", "/api/fire/on?mode=A",
          "/api/fire/onoff", "/api/fire/on/A"):
    rq = urllib.request.Request(BASE + p, data=b'{"mode":"A"}', method="POST",
                                headers={"Content-Type": "application/json",
                                         "X-Panel": "1",
                                         "X-Panel-Token": LP.FIRE_TOKEN})
    try:
        with urllib.request.urlopen(rq, timeout=15) as r:
            c = r.status
    except urllib.error.HTTPError as e:
        c = e.code
    say(c in (404, 405, 400), f"    POST {p} 被拒絕（{c}）")
    say(not AF.ARM_FLAG.exists(), f"      ⇒ ⛔ 開關沒有被建出來（{p}）")

# ── ③b-6 ⛔ 那個 token 真的只從 /api/fire/state 拿得到，而且不是空的
print("\n  ── token")
c, ct2, b = get("/api/fire/state")
j = as_json(b) or {}
say(isinstance(j.get("token"), str) and len(j["token"]) >= 16,
    "    /api/fire/state 帶得出 token", str(j.get("token"))[:12] + "…")
chk("    就是這個行程的那一個", j.get("token"), LP.FIRE_TOKEN)
with urllib.request.urlopen(BASE + "/api/fire/state", timeout=15) as _r:
    _hdrs = [k.lower() for k in _r.headers]
say(not any(k.startswith("access-control") for k in _hdrs),
    "    ⛔⛔ 而且那份回應**沒有任何 CORS 標頭**（跨站送得出去但讀不到 ⇒ 拿不到 token）",
    str(_hdrs))
say(isinstance(j.get("arm_confirm"), dict)
    and isinstance(j["arm_confirm"].get("text"), str)
    and j["arm_confirm"].get("live") is broker.is_live(),
    "    ⛔ 確認條那句話由後端算（真錢／演練跟 broker 同一把尺）",
    str(j.get("arm_confirm"))[:90])

# ── ③c ⛔⛔⛔ 【P0】守衛套在**每一個** POST 上，不是只有 /api/fire/on
print("\n=== ③c ⛔⛔⛔ 【P0】每一個 POST 都要過守衛 ===")
# ⚠️⚠️ 2026-09-09 lab-qa 抓到的洞：防護原本只掛在 `/api/fire/on`，
#    `/api/real/enter`／`/api/real/close` **一道都沒有**。他用一張純 HTML 表單
#    （⛔ 不必 JS、不必 CORS、不必 token）真的打進去了：
#        <form action="http://127.0.0.1:8770/api/real/enter" method="post"
#              enctype="text/plain">
#          <input name='{"dir":"long","x":"   ' value='   "}'>
#        ⇒ 瀏覽器送出 b'{"dir":"long","x":"="}\r\n' ＝ 合法 JSON
#        ⇒ HTTP/1.0 200 OK {"ok": true, "warn": false, "msg": "已送出"}
#        ⇒ broker.enter('long', 12000.0, 100.0) 真的被呼叫
#    ⇒ **他上網時任何一個網頁都可以用他的帳戶送單／平倉。**
# ⛔ 所以這一節的名單要**涵蓋 do_POST 裡每一條路由**，⛔ 不是只有真錢那兩條。
_POSTS = ["/api/real/enter", "/api/real/close", "/api/fire/on", "/api/fire/off",
          "/api/enter", "/api/close", "/api/note", "/api/replay",
          "/api/sync", "/api/undo"]
# ⛔ 名單要跟原始碼對得起來 —— 少列一條就等於那條沒被驗到（而它照樣對外開著）。
_routes = sorted(set(re.findall(r'self\.path == "(/api/[a-z/]+)"',
                                LPSRC.split("def do_POST")[1].split("def do_GET")[0])))
chk("  ⛔ 名單 ＝ do_POST 裡真正的每一條路由（⛔ 少列一條就是漏驗）",
    _routes, sorted(_POSTS))

# ⭐ lab-qa 那張表單**逐字**重現：`text/plain` ＋ 那個古怪的 body。
_FORM_BODY = b'{"dir":"long","x":"="}\r\n'
_ATTACKS = [
    ("純 HTML <form enctype=\"text/plain\">（⛔ 不必 JS／CORS／token）",
     {"Content-Type": "text/plain;charset=UTF-8", "Origin": "https://evil.example",
      "Sec-Fetch-Site": "cross-site"}, 415),
    ("<form> 的另外兩種 enctype（urlencoded）",
     {"Content-Type": "application/x-www-form-urlencoded"}, 415),
    ("<form> 的另外兩種 enctype（multipart）",
     {"Content-Type": "multipart/form-data; boundary=x"}, 415),
    ("有 JSON 的 Content-Type 但沒有自訂標頭（⇒ 一定要 preflight）",
     {"Content-Type": "application/json"}, 403),
    ("有自訂標頭但沒有 token",
     {"Content-Type": "application/json", "X-Panel": "1"}, 403),
    ("標頭齊了但 Origin 是別的網站",
     _hdr(Origin="https://evil.example"), 403),
    ("標頭齊了但 Sec-Fetch-Site 是 cross-site（瀏覽器自己加的，網頁改不掉）",
     _hdr(**{"Sec-Fetch-Site": "cross-site"}), 403),
    ("標頭齊了但 Host 是別的網域（⛔ DNS rebinding）",
     _hdr(Host="evil.example:80"), 403),
]
for _p in _POSTS:
    for _name, _h, _want in _ATTACKS:
        c, ct, b = _req(_p, "POST", _FORM_BODY, _h)
        say(c == _want, f"  POST {_p} ← {_name} ⇒ 被擋（{c}）", (b or "")[:60])
chk("  ⛔⛔ 而且全程 broker.enter／broker.close **一次都沒有被呼叫**", SENT, [])
say(not AF.ARM_FLAG.exists(), "  ⛔ 開關檔也沒有被建出來")

# ── ③c-2 ⛔⛔ 尺的自證：**他自己的鈕還是按得動**
print("\n  ── ⛔⛔ 尺的自證：帶齊標頭的那一種（＝他面板上的 pfetch）照樣進得去")
# ⚠️ 沒有這一段，上面那一整排「被擋」只要把整個 do_POST 改成 `return 403` 就全綠 ——
#    而他的每一顆鈕都壞了。所以每一條都要證明「合格的請求真的走進路由自己的邏輯」。
# ⛔ 判準是「**走到那條路由自己的檢查**」（回的是那條路由自己的話），
#    ⛔ 不是「回 200」——「回 200」在這支裡是沒有意義的（fallthrough 也是 200）。
c, ct, b = post("/api/real/enter", b'{"dir":"nope"}')
say(c == 400 and "方向要是" in (b or ""),
    "    /api/real/enter：走到路由自己的欄位檢查（⛔ 不是被守衛擋）", f"{c} {b[:50]}")
c, ct, b = post("/api/enter", b'{"dir":"nope"}')
say(c == 400 and "方向要是" in (b or ""), "    /api/enter：同上", f"{c} {b[:50]}")
c, ct, b = post("/api/close")
say(c == 200 and "沒有持倉" in (b or ""), "    /api/close：真的執行了（沒有持倉）",
    f"{c} {b[:50]}")
c, ct, b = post("/api/undo")
say(c == 200 and "沒有紀錄" in (b or ""), "    /api/undo：真的執行了（沒有紀錄可刪）",
    f"{c} {b[:50]}")
c, ct, b = post("/api/note",
                b'{"kind":"real","date":"1999-01-01","time":"00:00",'
                b'"entry":0,"text":"__tmp__"}')
say(c == 409 and "找不到" in (b or ""),
    "    /api/note：走進 broker.set_trade_note（⛔ 暫存區裡沒有那一天）", f"{c} {b[:50]}")
c, ct, b = post("/api/fire/off")
say(c == 200 and (as_json(b) or {}).get("ok") is True, "    /api/fire/off：真的關得掉",
    f"{c} {b[:50]}")
# ⛔ `/api/real/close` 沒有「先驗欄位」那一段 —— 它一進去就送平倉單。
#    所以尺的自證改成「暫時換一個會記帳的替身」，⛔ 這樣上面那條 SENT==[] 的鐵律不受影響。
_close_seen = []
_saved_close = broker.close
broker.close = lambda *a, **k: (_close_seen.append(a) or (False, "尺的自證：治具"))
try:
    c, ct, b = post("/api/real/close")
finally:
    broker.close = _saved_close
say(c == 409 and "尺的自證" in (b or "") and len(_close_seen) == 1,
    "    /api/real/close：真的走到 broker.close（⛔ 不是被守衛擋）",
    f"{c} {b[:50]} calls={len(_close_seen)}")
chk("      ⛔ 而且那一次是刻意的替身，真正的 SENT 仍然是空的", SENT, [])
# ⛔⛔ `/api/sync` **刻意不做尺的自證**：它會真的 git pull／push 他的紀錄倉庫。
#    這一條的「合格請求進得去」由上面 8 條共用同一段入口守衛的事實承接
#    （守衛只有一份、在 `do_POST` 的入口），⛔ 不可以為了湊一條而真的去同步。
say(True, "    /api/sync：⛔ 刻意不做尺的自證（它會真的 push 他的紀錄倉庫）")
# ⛔ /api/replay 的尺自證在 ④b（那一節本來就會寫進暫存區的 replay_log/）

# ── ③d ⛔ Content-Length 的兩個坑（2026-09-09 lab-qa 建議 2）
print("\n  ── ⛔ Content-Length（⛔ 不可以讓外面決定要讀幾個位元組）")
# ⛔⛔ 判準要看**那一句話**，不可以只看 400 —— 這些 body 送到路由裡也是 400
#    （`{}` 少了 dir），只看碼的話「Content-Length 檢查整個拿掉」照樣全綠。
_CL_MSG = ("Content-Length 看不懂", "body 太大或長度不合理")
for _name, _cl, _want in (("abc（⛔ 舊版 int() 直接噴 traceback、斷連線）", "abc", 400),
                          ("-1（⛔ rfile.read(-1) ＝ 那條執行緒一直卡住）", "-1", 400),
                          ("-999999", "-999999", 400),
                          ("9999999999（⛔ 沒有上限就是一條執行緒被吃掉）",
                           "9999999999", 400),
                          ("262145（剛好超過 256 KB 一個位元組）", "262145", 400),
                          ("1e9（看起來像數字其實不是 int）", "1e9", 400)):
    _h = _hdr()
    _h["Content-Length"] = _cl
    # ⛔ 這裡要繞過 urllib 自己算長度：用 http.client 直接送
    import http.client                                          # noqa: E402
    _cn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=15)
    try:
        _cn.putrequest("POST", "/api/real/enter", skip_accept_encoding=True)
        for _k, _v in _h.items():
            _cn.putheader(_k, _v)
        # ⛔ **刻意不送 body**：伺服器看到壞掉的 Content-Length 會在讀 body 之前就回 400，
        #    此時若通道裡還躺著我們送出去的位元組，Windows 會在關連線時送 RST ⇒
        #    client 收到 `WinError 10053` 而不是那個 400（2026-09-09 實測，間歇性假紅）。
        #    ⚠️ 這不影響這一節要驗的事（驗的是**標頭**怎麼被解讀），
        #    而且突變版照樣紅：`int("abc")` 會噴例外、`read(-1)` 會等到逾時。
        _cn.endheaders()
        _r = _cn.getresponse()
        c, b = _r.status, _r.read().decode("utf-8", "replace")
    except Exception as e:
        c, b = -1, str(e)[:80]
    finally:
        _cn.close()
    say(c == _want and any(m in (b or "") for m in _CL_MSG),
        f"    Content-Length: {_name} ⇒ {_want} ＋ 那一句話（得到 {c}）", (b or "")[:60])
# 尺的自證：⛔ 正常的長度照樣進得去（不然上面那一排是「把 do_POST 改成 return 400」也全綠）
_cn = __import__("http.client", fromlist=["client"]).HTTPConnection(
    "127.0.0.1", PORT, timeout=15)
try:
    _cn.request("POST", "/api/real/enter", b'{"dir":"nope"}', _hdr())
    _r = _cn.getresponse()
    c, b = _r.status, _r.read().decode("utf-8", "replace")
finally:
    _cn.close()
say(c == 400 and "方向要是" in (b or ""),
    "    尺的自證：正常長度的 body 照樣進得去（⛔ 不是被 Content-Length 那道擋的）",
    f"{c} {b[:50]}")
chk("  ⛔ 而且沒有任何一張單因此出去", SENT, [])

# ── ③e ⛔ 端出 token 的那兩個 GET 也要有守衛（2026-09-09 lab-qa 建議 1）
print("\n  ── ⛔ /api/state 與 /api/fire/state（它們端出 token）")
for _p in ("/api/state", "/api/fire/state"):
    c, ct, b = _req(_p)
    j = as_json(b) or {}
    say(c == 200 and j.get("token") == LP.FIRE_TOKEN,
        f"    尺的自證：{_p} 正常拿得到 token（他的面板就是這樣拿的）", f"{c}")
    for _name, _h in (("Host 是別的網域（⛔ DNS rebinding 讀 token）",
                       {"Host": "evil.example:80"}),
                      ("Host 結尾剛好是本機位址（⛔ 不准 endswith）",
                       {"Host": "evil-127.0.0.1:80"}),
                      ("Origin 是別的網站", {"Origin": "https://evil.example"}),
                      ("Origin 是 null", {"Origin": "null"}),
                      ("Sec-Fetch-Site: cross-site", {"Sec-Fetch-Site": "cross-site"})):
        c, ct, b = _req(_p, "GET", None, _h)
        j = as_json(b) or {}
        say(c == 403 and "token" not in j, f"    {_p} ← {_name} ⇒ 403（{c}）",
            (b or "")[:50])
say(True, "    ⇒ ⛔ token 不再是「放在毫無防護的端點上」——"
          "④ 那一道的強度等於這兩個 GET 的強度")

# ── ④ ⛔ 除了那兩顆，沒有任何「改設定」的端點
print("\n=== ④ ⛔ 沒有其他會改變狀態的端點 ===")
# ⛔⛔ 這裡**一定要有前綴撞名的那幾條**（2026-09-09 lab-qa 突變 Q12 打不紅）：
#    把 `self.path == "/api/fire/off"` 換成 `startswith` 的話，
#    `/api/fire/offXX`、`/api/fire/off/on`、`/api/fire/off?arm=A` 全都會中 ——
#    而舊的名單裡一條前綴撞名的都沒有 ⇒ 那個突變結構上打不紅。
#    ⚠️ 這一條的意義不只是「路由要精確」：那顆按鈕是**這一頁唯一會改變狀態的動作**，
#    路由比對放寬＝多開了一批沒人審過的入口。
#    ⚠️ 2026-09-09：`/api/fire/on` **從這個名單搬走**了（那顆鈕現在做在畫面上），
#    它的守衛整組在 ③b —— ⛔ 那不是放寬：③b 對它做的檢查比這裡多十幾條。
for p in ("/api/fire/arm", "/api/fire/state", "/api/fire",
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
