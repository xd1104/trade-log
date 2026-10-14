# -*- coding: utf-8 -*-
"""
【五檔看門狗】（2026-10-14）的探針。

起因：10-12 08:59 永豐傳五檔那條線斷了、成交那條沒斷 ⇒ 原本只看成交的斷線看門狗沒發現，五檔漏錄一整天。
守的幾件事：
   ① 成交還在、五檔超過 DEPTH_STALE_S 秒沒進來 ⇒ 判成「五檔停了」；成交也停 ⇒ 不歸這裡管（交給原本的看門狗）。
   ② ⛔ 手上有部位 ⇒ 只示警、不重連（重連會卡住主迴圈幾秒，主迴圈就是停損）。
   ③ ⛔ 送單／平倉那幾刻前後 2 分鐘不重連；重連之間至少隔 DEPTH_RETRY_EVERY。
   ④ 畫面那句話講得出「幾分鐘沒收到」，手機快照帶得到。
   ⑤ 接線：on_bidask 第一行只記時間；主迴圈那段包在 try 裡；連線一建好就重設時間。
⛔ 不連永豐。

怎麼跑（PowerShell）：
    $env:PYTHONIOENCODING="utf-8"; $env:PYTHONUTF8="1"
    & "…\\trade-log\\.venv\\Scripts\\python.exe" "…\\tools\\shioaji\\test_depth_watchdog.py"
"""
import pathlib
import sys
from datetime import datetime

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import live_panel as LP             # noqa: E402
import monitor_push as MP           # noqa: E402

FAIL = 0


def chk(name, got, want):
    global FAIL
    ok = got == want
    FAIL += not ok
    print(("  OK   " if ok else "  FAIL ") + name + ("" if ok else f"  (得到 {got!r}，期待 {want!r})"))


def say(cond, name, extra=""):
    global FAIL
    FAIL += not cond
    print(("  OK   " if cond else "  FAIL ") + name + (f"  {extra}" if extra else ""))


T = 1_000_000.0
C = datetime(2026, 10, 12, 10, 30, 0)          # 平常盤中、離送單時刻很遠
S = LP.DEPTH_STALE_S

print("=== ① 判斷 ===")
chk("  五檔剛剛才來 ⇒ 沒事", LP.depth_check(T, T - 10, 2, True, True, False, 0, C), (None, False))
chk("  成交在跳、五檔停了 ⇒ 重連", LP.depth_check(T, T - S - 1, 2, True, True, False, 0, C), (S + 1, True))
chk("  成交也停了 ⇒ 不歸這裡管", LP.depth_check(T, T - S - 1, 200, True, True, False, 0, C), (None, False))
chk("  還沒收過任何成交 ⇒ 不判", LP.depth_check(T, T - S - 1, None, True, True, False, 0, C), (None, False))
chk("  休市 ⇒ 不判", LP.depth_check(T, T - S - 1, 2, False, True, False, 0, C), (None, False))
chk("  整條連線已經斷了 ⇒ 交給原本的看門狗", LP.depth_check(T, T - S - 1, 2, True, False, False, 0, C), (None, False))
chk("  連上之後還沒收過五檔（last_ba 是 None）⇒ 不判", LP.depth_check(T, None, 2, True, True, False, 0, C), (None, False))

print("=== ②③ 什麼時候不重連 ===")
chk("  ⛔ 手上有部位 ⇒ 只示警", LP.depth_check(T, T - S - 1, 2, True, True, True, 0, C), (S + 1, False))
chk("  ⛔ 10 分鐘內剛試過 ⇒ 先不試", LP.depth_check(T, T - S - 1, 2, True, True, False, T - 60, C), (S + 1, False))
for z, c in (("09:03:30", datetime(2026, 10, 12, 9, 2, 0)), ("09:15:00", datetime(2026, 10, 12, 9, 16, 30)),
             ("13:43:30", datetime(2026, 10, 12, 13, 42, 0)), ("21:40:00", datetime(2026, 10, 12, 21, 41, 0)),
             ("04:58:00", datetime(2026, 10, 13, 4, 57, 0))):
    chk("  ⛔ %s 前後不重連" % z, LP.depth_check(T, T - S - 1, 2, True, True, False, 0, c)[1], False)
chk("  離送單時刻 3 分鐘以上就可以", LP.depth_check(T, T - S - 1, 2, True, True, False, 0,
                                                datetime(2026, 10, 12, 9, 7, 0))[1], True)

print("=== ④ 畫面那句話 ＋ 手機 ===")
wd0 = dict(LP.DEPTH_WD)
LP.DEPTH_WD.update(stale_s=600.0, held=False)
v = LP.depth_view()
say(v["warn"] and "10 分鐘沒收到五檔" in v["msg"] and "自動重連" in v["msg"], "  沒部位：講得出幾分鐘、會自動重連", v["msg"])
LP.DEPTH_WD.update(held=True)
v = LP.depth_view()
say(v["warn"] and "平倉後" in v["msg"], "  有部位：講明平倉後才重連", v["msg"])
LP.DEPTH_WD.update(stale_s=None, held=False)
say(not LP.depth_view()["warn"] or LP.DEPTH_SUBS.get("err") or LP.DEPTH_SUBS.get("paused_day"),
    "  沒停 ⇒ 不示警")
LP.DEPTH_WD.clear()
LP.DEPTH_WD.update(wd0)
fake = {"/api/state": {"real": {"position": None},
                       "equity": {"depth": {"warn": True, "msg": "五檔停了", "n": 1, "lost": 0}}},
        "/api/fire/state": {}, "/api/nightfire/state": {}, "/api/analyst/index": {}}
g0 = MP._get
MP._get = lambda port, path: fake[path]
snap = MP.snapshot(1)
MP._get = g0
chk("  手機快照帶得到（只帶 warn／msg）", snap["equity"]["depth"], {"warn": True, "msg": "五檔停了"})
js = (HERE.parent.parent / "js" / "monitor.js").read_text(encoding="utf-8")
say("e.depth && e.depth.warn" in js, "  手機「要注意的事」會列出來")

print("=== ⑤ 接線（原始碼量尺） ===")
src = (HERE / "live_panel.py").read_text(encoding="utf-8")
i = src.index("def on_bidask(exchange: Exchange, ba: BidAskFOPv1):")
first = src[i:].splitlines()[1].strip()
chk("  on_bidask 第一行只記時間（⛔ 沒有 I/O）", first.split("#")[0].strip(), 'DEPTH_WD["last_ba"] = time.time()')
i = src.index("五檔看門狗（見 DEPTH_WD）")
blk = src[i:i + 900]
say(blk.splitlines()[1].strip() == "try:" and "except Exception" in blk, "  主迴圈那段包在 try 裡（出錯不打斷停損）")
say('broker._state.get("position") is not None' in blk, "  有沒有部位看的是 broker 的真部位")
i = src.index("        depth_subscribe(api, contract)\n")
say('DEPTH_WD["last_ba"] = time.time()' in src[i:i + 200], "  連線一建好就重設時間（⛔ 不拿舊連線的時間判斷）")

print("全部通過 ✅" if not FAIL else "⛔ %d 項失敗" % FAIL)
sys.exit(1 if FAIL else 0)
