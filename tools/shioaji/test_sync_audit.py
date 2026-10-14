# -*- coding: utf-8 -*-
"""
2026-10-14 全面對帳（Benson：「交易儀表板跟手機 App 資料看起來沒有同步」）那一輪的探針。

守的幾件事：
   ① 夜盤那一口的出場價／點數：唯讀比對 real_trades/（檔名是出場那天 ⇒ 往後找），
      對到一筆才算 ok、對不到／多筆留白，⛔ 不會把同一天的日盤那口掛到夜盤頭上。
   ② 有部位時「夠不夠下一口」看平倉後，⛔ 不再每次有部位就喊「下一口會被退單」。
   ③ 休市日清單沿用月曆那一把尺（day_index 的 closed）。
   ④ 手機快照帶得出夜盤點數與休市日；心跳只在快照推上去之後才跳、沒網址就不跳、跳失敗不炸。
   ⑤ K 線圖的時間 → K 棒：夜盤時段不再掉到日盤最後一根（原始碼層的量尺；這台沒有 node）。
⛔ 不連永豐、不碰他真的資料夾：所有路徑導到暫存區；⛔ 不會真的打 hc-ping.com。

怎麼跑（PowerShell）：
    $env:PYTHONIOENCODING="utf-8"; $env:PYTHONUTF8="1"
    & "…\\trade-log\\.venv\\Scripts\\python.exe" "…\\tools\\shioaji\\test_sync_audit.py"
"""
import json
import pathlib
import shutil
import sys
import tempfile
from datetime import datetime

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import live_panel as LP             # noqa: E402
import monitor_push as MP           # noqa: E402

FAIL = 0


def say(cond, name, extra=""):
    global FAIL
    FAIL += not cond
    print(("  OK   " if cond else "  FAIL ") + name + (f"  {extra}" if extra else ""))


def chk(name, got, want):
    global FAIL
    ok = got == want
    FAIL += not ok
    print(("  OK   " if ok else "  FAIL ") + name + ("" if ok else f"  (得到 {got!r}，期待 {want!r})"))


TMP = pathlib.Path(tempfile.mkdtemp(prefix="sync_audit_"))
REAL_DIR0 = LP.AUTO_REAL_DIR
try:
    # ── ① 夜盤對帳 ────────────────────────────────────────────────
    print("=== ① 夜盤那一口後來怎麼了 ===")
    rd = TMP / "real_trades"
    rd.mkdir()
    LP.AUTO_REAL_DIR = rd
    LP._FIRE_REAL_CACHE.clear()

    def put(day, *rows):
        with (rd / f"{day}.jsonl").open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(dict(r, date=day), ensure_ascii=False) + "\n")

    # 10-13 晚做多 48730 ⇒ 10-14 04:58 平（+22）；同一個檔還有 10-14 白天那口（09:15 進）
    put("2026-10-14",
        {"dir": "long", "entry_time": "21:40:01", "entry": 48730.0, "exit_time": "04:58:01",
         "exit": 48752.0, "reason": "night_eod", "points": 22.0},
        {"dir": "long", "entry_time": "09:15:01", "entry": 48779.0, "exit_time": "09:54:58",
         "exit": 49023.0, "reason": "tp", "points": 244.0})
    # 週五 10-16 晚做空 ⇒ 週六 10-17 04:58 平
    put("2026-10-17", {"dir": "short", "entry_time": "21:40:02", "entry": 49000.0, "exit_time": "04:58:00",
                       "exit": 49100.0, "reason": "night_eod", "points": -100.0})
    # 10-20 晚 22:10 就停損（出場在同一天）
    put("2026-10-20", {"dir": "long", "entry_time": "21:40:01", "entry": 50000.0, "exit_time": "22:10:00",
                       "exit": 49000.0, "reason": "sl", "points": -1000.0})
    # 10-21 晚：成績單有兩列一模一樣 ⇒ many
    for _ in range(2):
        put("2026-10-22", {"dir": "long", "entry_time": "21:40:01", "entry": 50100.0, "exit_time": "04:58:00",
                           "exit": 50150.0, "reason": "night_eod", "points": 50.0})

    def res(E, d, entry, t="21:40:01", live=True):
        return {"E": E, "rec": "result", "ok": True, "dir": d, "entry": entry, "entry_time": t, "live": live}

    recent = [res("2026-10-13", "long", 48730.0), res("2026-10-16", "short", 49000.0),
              res("2026-10-20", "long", 50000.0), res("2026-10-21", "long", 50100.0),
              res("2026-10-23", "long", 51000.0), res("2026-10-24", "long", 51000.0),
              res("2026-10-27", "long", 1.0, live=False),
              {"E": "2026-10-28", "rec": "skip", "why": "not_fast"},
              {"E": "2026-10-29", "rec": "result", "ok": False, "err": "退單"}]
    P = LP.night_real_pairs(recent, now=datetime(2026, 10, 25, 3, 0))
    chk("  10-13 晚 ⇒ 對到 10-14 那一列、+22", (P["2026-10-13"]["state"], P["2026-10-13"]["points"],
                                         P["2026-10-13"]["exit_date"]), ("ok", 22.0, "2026-10-14"))
    chk("  ⛔ 同一個檔裡 10-14 白天那口（+244）沒被掛到夜盤", P["2026-10-13"]["exit"], 48752.0)
    chk("  出場理由帶得出來（畫面上寫「夜盤收盤」要靠它）", P["2026-10-13"]["why"], "night_eod")
    chk("  週五晚 ⇒ 週六出場的檔也找得到", (P["2026-10-16"]["state"], P["2026-10-16"]["points"]), ("ok", -100.0))
    chk("  當晚就停損（出場在同一天）", (P["2026-10-20"]["state"], P["2026-10-20"]["points"]), ("ok", -1000.0))
    chk("  對到兩筆 ⇒ many（⛔ 不挑一筆）", P["2026-10-21"]["state"], "many")
    chk("  還沒到 04:58 ⇒ open", P["2026-10-24"]["state"], "open")
    chk("  過了 04:58 還對不到 ⇒ none（留白）", P["2026-10-23"]["state"], "none")
    chk("  演練 ⇒ drill", P["2026-10-27"]["state"], "drill")
    say("2026-10-28" not in P and "2026-10-29" not in P, "  沒送／沒送成的那幾晚不談下場")
    chk("  往後找幾天跟風控同一個數", LP.NIGHT_REAL_SPAN, LP.risk_cap.NIGHT_EXIT_MAX_D + 1)

    # ── ② 有部位時夠不夠下一口 ─────────────────────────────────────
    print("=== ② 有部位時「夠不夠下一口」 ===")
    eq0, lot0 = dict(LP.EQUITY), LP._lot1
    LP._lot1 = lambda: (35050.0, "測試")
    LP.EQUITY.update(m={"equity_amount": 50614.0, "available_margin": 14024.0, "initial_margin": 35050.0},
                     err=None, at="09:30:39")
    v = LP.equity_view()
    e = v["enough"]
    chk("  有部位：看平倉後 ⇒ 夠", (e["ok"], e["why"]), (True, "holding"))
    say("平倉後" in e["msg"] and "退單" not in e["msg"], "  ⛔ 那句話不再喊會被退單", e["msg"])
    LP.EQUITY["m"] = {"equity_amount": 50614.0, "available_margin": 14024.0, "initial_margin": 0.0}
    e = LP.equity_view()["enough"]
    chk("  沒部位、真的不夠 ⇒ 照舊示警", (e["ok"], "退單" in e["msg"]), (False, True))
    LP.EQUITY["m"] = {"equity_amount": 20000.0, "available_margin": 1000.0, "initial_margin": 19000.0}
    e = LP.equity_view()["enough"]
    chk("  有部位、平倉後也不夠 ⇒ 示警", (e["ok"], "平倉後也不夠" in e["msg"]), (False, True))
    LP.EQUITY.clear()
    LP.EQUITY.update(eq0)
    LP._lot1 = lot0

    # ── ③ 休市日 ───────────────────────────────────────────────────
    print("=== ③ 休市日 ===")
    di0 = LP.day_index
    LP.day_index = lambda n=70: [{"d": "2026-10-08"}, {"d": "2026-10-09", "closed": True},
                                 {"d": "2026-10-12", "closed": False}]
    chk("  只挑 closed 的那幾天", LP.closed_days(), ["2026-10-09"])
    LP.day_index = di0

    # ── ④ 手機快照＋心跳 ───────────────────────────────────────────
    print("=== ④ 手機快照帶得出夜盤點數與休市日；心跳 ===")
    fake = {
        "/api/state": {"real": {"position": None}},
        "/api/fire/state": {"armed": True, "method": "U", "days": [], "closed": ["2026-10-09"]},
        "/api/nightfire/state": {"on": True, "method": "R", "recent": [res("2026-10-13", "long", 48730.0)],
                                 "real": {"2026-10-13": dict(P["2026-10-13"], junk=1)},
                                 "closed": ["2026-10-09"]},
        "/api/analyst/index": {"items": []},
    }
    get0 = MP._get
    MP._get = lambda port, path: fake[path]
    snap = MP.snapshot(1)
    MP._get = get0
    chk("  夜盤點數有帶到手機", snap["night"]["real"]["2026-10-13"]["points"], 22.0)
    say("junk" not in snap["night"]["real"]["2026-10-13"], "  ⛔ 只帶白名單欄位")
    chk("  日盤休市日", snap["day"]["closed"], ["2026-10-09"])
    chk("  夜盤休市日", snap["night"]["closed"], ["2026-10-09"])

    hc0 = MP.HC_FILE
    MP.HC_FILE = TMP / "nope.txt"
    chk("  沒有網址檔 ⇒ 不心跳", MP.heartbeat(), False)
    MP.HC_FILE = TMP / "bad.txt"
    MP.HC_FILE.write_text("http://evil.example/x\n", encoding="utf-8")
    chk("  ⛔ 不是 hc-ping.com 的網址一律不打", MP.hc_url(), None)
    MP.HC_FILE = TMP / "hc.txt"
    MP.HC_FILE.write_text("https://hc-ping.com/00000000-test\n", encoding="utf-8")
    hits = []

    class _R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"OK"

    uo0 = MP.urllib.request.urlopen
    MP.urllib.request.urlopen = lambda rq, timeout=None: (hits.append(rq.full_url), _R())[1]
    chk("  有網址 ⇒ 跳一下", (MP.heartbeat(), hits), (True, ["https://hc-ping.com/00000000-test"]))

    def _boom(*a, **k):
        raise OSError("斷網")
    MP.urllib.request.urlopen = _boom
    chk("  跳失敗 ⇒ 回 False、⛔ 不丟例外", MP.heartbeat(), False)

    # once()：推送失敗 ⇒ ⛔ 不心跳（心跳＝手機真的收得到新快照）
    hits.clear()
    MP.urllib.request.urlopen = lambda rq, timeout=None: (hits.append(rq.full_url), _R())[1]
    sn0, pu0 = MP.snapshot, MP.push
    MP.snapshot = lambda port: {"x": 1}
    key = {"salt": b"s" * 16, "iter": 1, "k_enc": b"e" * 32, "k_mac": b"m" * 32}

    def _pfail(*a, **k):
        raise RuntimeError("git push 失敗")
    MP.push = _pfail
    try:
        MP.once(1, key)
    except RuntimeError:
        pass
    chk("  推送失敗 ⇒ 沒有心跳", hits, [])
    MP.push = lambda *a, **k: "c0ffee"
    MP._AN.update(hash=None, box=None)
    MP.once(1, key)
    chk("  推送成功 ⇒ 心跳一次", len(hits), 1)
    MP.snapshot, MP.push = sn0, pu0
    MP.urllib.request.urlopen = uo0
    MP.HC_FILE = hc0

    # ── ⑤ 原始碼層：K 線圖、夜盤列、本月合計 ─────────────────────
    print("=== ⑤ 前端（原始碼量尺；這台沒有 node） ===")
    src = (HERE / "live_panel.py").read_text(encoding="utf-8")
    i = src.index("function idxAll(t){")
    body = src[i:src.index("\n}\n", i)]
    say("'eve'" in body and "'dawn'" in body, "  idxAll 先認時段（前一晚／凌晨／日盤）再找")
    say("if(b.t<'08:45') continue;" not in body, "  ⛔ 舊的「只找日盤」那一行不在了")
    i = src.index("const layPills=list=>{")
    body = src[i:src.index("\n };\n", i)]
    say(".sort((a,b)=>a.X-b.X)" in body and "lim-P[k].w" in body,
        "  底部膠囊一次排：照時間由左到右、超出右邊就整串往左推（⛔ 不會順序顛倒）")
    say("layPills(laneX)" in src, "  畫圖真的走 layPills")
    i = src.index("function alPts(D,days){")
    body = src[i:src.index("\nfunction alPerfPaint", i)]
    say("x.results" in body and "NR[E]" in body, "  最近做得如何：夜盤也算進來")
    say("mo:E" in body, "  本月照開盤那晚 E 歸月（跟風控規則③同一把尺）")
    say("night_real_pairs(res)" in src and 'out["closed"] = closed_days()' in src, "  兩個端點都端出新欄位")
    gi = (HERE.parent.parent / ".gitignore").read_text(encoding="utf-8")
    say("tools/shioaji/HC_PING_URL.txt" in gi, "  ⛔ 心跳網址檔不進版控")
finally:
    LP.AUTO_REAL_DIR = REAL_DIR0
    LP._FIRE_REAL_CACHE.clear()
    shutil.rmtree(TMP, ignore_errors=True)

print("全部通過 ✅" if not FAIL else "⛔ %d 項失敗" % FAIL)
sys.exit(1 if FAIL else 0)
