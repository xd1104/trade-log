# -*- coding: utf-8 -*-
"""
【夜盤自動下單】台積電快攻 的探針（2026-09-22）。

⛔ 全程假券商、假報價、假 Alpaca、暫存資料夾：⛔ 不連永豐、⛔ 不送任何單、⛔ 不碰真的開關檔。
在守的事：
  ① 關著就一張都不送；開著而且快才送；一晚最多一次（重跑、重啟都不會送第二張）
  ② 不快／太晚／拿不到台積電／報價不新鮮／不是夜盤／框寬不合理／不能進場 ⇒ 各自的理由、不送
  ③ 送到一半中斷（有 sending 沒 result）⇒ ⛔ 不重送
  ④ 04:58 只平自己那一口；手動的部位不碰；已經沒部位就記 flat；週五晚到週六凌晨也對
  ⑤ 重啟撿回部位：是今晚這一口 ⇒ 補回它自己的停損點數（⛔ 不掉回 130）
  ⑥ 時間對齊：美東 9:35（第一根收完）以前 first5_live 一律 None，⛔ 連網路都不問
  ⑦ AST：這個模組打不開自己的開關；面板 main() 的接線包在 try 裡、撿回鏈先問夜盤再問日盤
"""
import ast
import json
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.stdout.reconfigure(encoding="utf-8")

import broker  # noqa: E402
import night_fire as NF  # noqa: E402
import us_feed  # noqa: E402

FAIL = 0
REAL_FLAG = NF.ARM_FLAG


def say(cond, name, extra=""):
    global FAIL
    FAIL += not cond
    print(("  OK   " if cond else "  FAIL ") + name + (f"  {extra}" if extra else ""))


def chk(name, got, want):
    global FAIL
    ok = got == want
    FAIL += not ok
    print(("  OK   " if ok else "  FAIL ") + name + ("" if ok else f"  (得到 {got!r}，期待 {want!r})"))


REAL_EXISTED = REAL_FLAG.exists()
TMP = Path(tempfile.mkdtemp(prefix="nightfire-test-"))
NF.ARM_FLAG = TMP / "NIGHT_ORDERS_ON"
NF.NF_DIR = TMP / "nightfire"
NF.TSM_CTX = TMP / "tsm_ctx.json"

# ── 假的東西 ──
CALLS = {"enter": [], "close": []}
FAKE = {"can": (True, None), "first": None, "quote": (46000.0, 1.0), "sess": "night"}


def fake_enter(d, px, tp, sl_points=None):
    CALLS["enter"].append((d, px, tp, sl_points))
    pos = {"dir": d, "entry": px + 1, "qty": 1, "entry_time": "21:35:04", "sl_points": sl_points, "tp_points": tp}
    broker._state["position"] = pos
    return True, None, pos


def fake_close(reason):
    CALLS["close"].append(reason)
    broker._state["position"] = None
    return True, None


broker.enter = fake_enter
broker.close = fake_close
broker.can_enter = lambda px, live: FAKE["can"]
broker.is_live = lambda: False
us_feed.first5_live = lambda sym, E, feed="iex", now=None: FAKE["first"]
NF.past_mvs = lambda E, n=40, back_days=90: [0.3 if i % 2 else -0.2 for i in range(40)]
NF.configure(quote_fn=lambda: FAKE["quote"], session_fn=lambda now: FAKE["sess"])
NF.TSM_CTX.write_text(json.dumps({"rng": {str(date(2026, 5, 1) + timedelta(days=i)): 0.5 for i in range(30)}}),
                      encoding="utf-8")

E = date(2026, 6, 10)                       # 週三，夏令 ⇒ 21:35
T5 = NF.done_at(E)
FIRST = {"open": 100.0, "close": 101.0, "mv_pct": 1.0, "done_at": T5}


def reset():
    shutil.rmtree(NF.NF_DIR, ignore_errors=True)
    CALLS["enter"].clear(); CALLS["close"].clear()
    broker._state["position"] = None
    NF._DONE.update(decide=None, eod=None, warm=None)
    NF._MEM.update(E=None, entry=None)
    NF._ST.update(tried=None, eod_try_at=0.0, hist_cache={"E": None, "mvs": None})
    FAKE.update(can=(True, None), first=dict(FIRST), quote=(46000.0, 1.0), sess="night")


def run_until(t0, t1, step_s=1):
    t = t0
    while t <= t1:
        NF.step(t)
        t += timedelta(seconds=step_s)


def last_skip():
    rs = [o for o in NF.rows_of(E) if o.get("rec") == "skip"]
    return rs[-1]["why"] if rs else None


print("=== ① 開關與一晚一次 ===")
chk("  done_at 夏令 ＝ 21:35", T5, datetime(2026, 6, 10, 21, 35))
chk("  done_at 冬令 ＝ 22:35", NF.done_at(date(2026, 1, 14)), datetime(2026, 1, 14, 22, 35))
reset()
run_until(T5, T5 + timedelta(seconds=20))
chk("  關著 ⇒ 一張都不送", CALLS["enter"], [])
chk("  關著 ⇒ 記一列 off", last_skip(), "off")
reset()
NF.ARM_FLAG.write_text("t\n", encoding="utf-8")
chk("  開關寫小寫 t 也認得", NF.arm()["on"], True)
run_until(T5 - timedelta(seconds=5), T5 + timedelta(seconds=2))
chk("  ⛔ 21:35:02 以前一次都沒送（第一根收完後還要等 3 秒）", CALLS["enter"], [])
run_until(T5 + timedelta(seconds=3), T5 + timedelta(seconds=30))
chk("  開著而且快 ⇒ 送一張做多", [c[0] for c in CALLS["enter"]], ["long"])
w = CALLS["enter"][0][2] if CALLS["enter"] else None
chk("  停利 ＝ 停損 ＝ 進場價 × 過去 20 晚振幅平均（0.5%）", (w, CALLS["enter"][0][3] if CALLS["enter"] else None),
    (round(46000 * 0.005), round(46000 * 0.005)))
NF._DONE["decide"] = None                  # 模擬重啟：記憶體清掉
run_until(T5 + timedelta(seconds=30), T5 + timedelta(seconds=60))
chk("  ⛔ 重跑／重啟不會送第二張", len(CALLS["enter"]), 1)
recs = [o["rec"] for o in NF.rows_of(E)]
chk("  帳本先 sending 再 result", recs[:2], ["sending", "result"])
NF.ARM_FLAG.write_text("U", encoding="utf-8")
chk("  開關寫別的字 ⇒ 關著（⛔ 不猜）", NF.arm()["on"], False)
NF.ARM_FLAG.write_text("T", encoding="utf-8")

print("\n=== ② 各種不送的理由 ===")
for name, setup, when, want in [
    ("不快", lambda: FAKE.update(first=dict(FIRST, mv_pct=0.05)), 10, "not_fast"),
    ("太晚才走到（面板剛開機）", lambda: None, 300, "late"),
    ("報價不新鮮", lambda: FAKE.update(quote=(46000.0, 12.0)), 10, "no_quote"),
    ("不是夜盤時段", lambda: FAKE.update(sess="closed"), 10, "no_quote"),
    ("不能進場（已有部位等）", lambda: FAKE.update(can=(False, "已經有部位了")), 10, "cannot"),
]:
    reset()
    setup()
    NF.step(T5 + timedelta(seconds=when))
    chk("  %s ⇒ %s、不送" % (name, want), (last_skip(), CALLS["enter"]), (want, []))
reset()
FAKE["first"] = None
NF.step(T5 + timedelta(seconds=10))
chk("  拿不到台積電、還在 90 秒內 ⇒ 先不下定論", NF.rows_of(E), [])
NF.step(T5 + timedelta(seconds=100))
chk("  超過 90 秒還拿不到 ⇒ no_us、不送", (last_skip(), CALLS["enter"]), ("no_us", []))
reset()
NF.TSM_CTX.write_text(json.dumps({"rng": {str(date(2026, 5, 1) + timedelta(days=i)): 9.0 for i in range(30)}}),
                      encoding="utf-8")
NF.step(T5 + timedelta(seconds=10))
chk("  框寬不合理（9%）⇒ bad_width、不送", (last_skip(), CALLS["enter"]), ("bad_width", []))
NF.TSM_CTX.write_text(json.dumps({"rng": {str(date(2026, 5, 1) + timedelta(days=i)): 0.5 for i in range(30)}}),
                      encoding="utf-8")
reset()
NF.TSM_CTX.write_text(json.dumps({"rng": {"2026-06-09": 0.5}}), encoding="utf-8")
NF.step(T5 + timedelta(seconds=10))
chk("  振幅歷史不夠 ⇒ no_hist", last_skip(), "no_hist")
NF.TSM_CTX.write_text(json.dumps({"rng": {str(date(2026, 5, 1) + timedelta(days=i)): 0.5 for i in range(30)}}),
                      encoding="utf-8")
ctx_future = {"rng": dict({str(date(2026, 5, 1) + timedelta(days=i)): 0.5 for i in range(30)},
                          **{"2026-06-10": 50.0, "2026-06-11": 50.0})}
NF.TSM_CTX.write_text(json.dumps(ctx_future), encoding="utf-8")
chk("  ⛔ 振幅只用 E 以前的晚上（E 當晚與之後的不算）", max(NF.past_rngs(E)), 0.5)

print("\n=== ③ 送到一半中斷 ⇒ 不重送 ===")
reset()
NF._append({"E": str(E), "rec": "sending", "dir": "long", "px": 46000})
NF.step(T5 + timedelta(seconds=10))
chk("  有 sending 沒 result ⇒ ⛔ 不再送", CALLS["enter"], [])

print("\n=== ④ 04:58 平倉 ===")
reset()
run_until(T5, T5 + timedelta(seconds=10))
say(broker._state["position"] is not None, "  （前置）今晚開出一口")
E1 = datetime(2026, 6, 11, 4, 57, 55)
run_until(E1, E1 + timedelta(seconds=10))
chk("  04:58 平掉自己那一口", CALLS["close"], ["night_eod"])
reset()
run_until(T5, T5 + timedelta(seconds=10))
broker._state["position"] = {"dir": "short", "entry": 45000, "qty": 1}     # 他自己手動的
run_until(E1, E1 + timedelta(seconds=10))
chk("  ⛔ 不是自己那一口 ⇒ 不碰", CALLS["close"], [])
chk("  記 not_ours", [o["why"] for o in NF.rows_of(E) if o.get("rec") == "eod"], ["not_ours"])
reset()
run_until(T5, T5 + timedelta(seconds=10))
broker._state["position"] = None                                           # 之前已停利
run_until(E1, E1 + timedelta(seconds=10))
chk("  已經沒部位 ⇒ 記 flat、不送平倉", ([o["why"] for o in NF.rows_of(E) if o.get("rec") == "eod"], CALLS["close"]),
    (["flat"], []))
chk("  週五晚 ⇒ 週六 04:58 屬於週五那一晚", NF.evening_of(datetime(2026, 6, 13, 4, 58)), date(2026, 6, 12))
chk("  週日晚沒有夜盤", NF.evening_of(datetime(2026, 6, 14, 21, 35)), None)

print("\n=== ⑤ 重啟撿回部位 ===")
reset()
run_until(T5, T5 + timedelta(seconds=10))
ent = NF._MEM["entry"]
pos = {"dir": "long", "entry": ent["entry"], "qty": 1, "recovered": True}
NF._MEM.update(E=E, entry=NF._entry_of(NF.rows_of(E)))   # 重啟時 start() 會從帳本讀回這一口
_real_now = NF.datetime


class _FakeDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 6, 10, 23, 0)


NF.datetime = _FakeDT
got = NF.recover_meta(pos)
chk("  是今晚那一口 ⇒ 補回它自己的停損點數", (got or {}).get("sl_points"), float(ent["sl_points"]))
chk("  不是今晚那一口 ⇒ None（交給日盤那支）", NF.recover_meta({"dir": "short", "entry": 1}), None)
pos2 = dict(pos, sl_src="manual", sl_warn="x")
broker._state["position"] = pos2
NF._recover_poll()
chk("  主迴圈那一刻沒認出來（被判成手動 130）⇒ 工作執行緒補正", (pos2.get("sl_src"), pos2.get("sl_points")),
    ("nightfire", float(ent["sl_points"])))
NF.datetime = _real_now

print("\n=== ⑥ 時間對齊：9:35 以前不問 ===")
import urllib.request as _ur  # noqa: E402
_orig = _ur.urlopen
_hit = []
_ur.urlopen = lambda *a, **k: _hit.append(1) or (_ for _ in ()).throw(RuntimeError("不該連網路"))
import importlib  # noqa: E402
real_live = importlib.reload(us_feed).first5_live
chk("  21:34:59 ⇒ None", real_live("TSM", E, now=datetime(2026, 6, 10, 21, 34, 59)), None)
chk("  ⛔ 而且連網路都沒問", _hit, [])
_ur.urlopen = _orig

print("\n=== ⑦ AST ===")
src = (HERE / "night_fire.py").read_text(encoding="utf-8")
tree = ast.parse(src)
bad = []
for n in ast.walk(tree):
    if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "ARM_FLAG":
        if n.attr not in ("exists", "read_bytes", "replace", "with_name", "name"):
            bad.append(n.attr)
chk("  ⛔ night_fire 對開關檔只做 exists／read_bytes／replace／with_name", bad, [])
say("open(" not in src.split("def _append")[0].split("def disarm")[0], "  開關那段沒有 open()")
lp = (HERE / "live_panel.py").read_text(encoding="utf-8")
m = lp[lp.index("def main():"):]
say("night_fire.start()" in m and m.index("try:", m.index("夜盤自動下單】台積電快攻")) < m.index("night_fire.start()"),
    "  main() 接線包在 try 裡")
say("broker.RECOVER_HOOK = _recover_chain" in lp and lp.count("broker.RECOVER_HOOK =") == 1,
    "  撿回鏈只接一次，而且是先夜盤後日盤那支")
ch = lp[lp.index("def _recover_chain"):lp.index("def main():")]
say(ch.index("night_fire.recover_meta") < ch.index("auto_fire.recover_meta"), "  鏈子順序：夜盤 → 日盤")
say(REAL_FLAG.exists() == REAL_EXISTED, "  ⛔ 真的 NIGHT_ORDERS_ON 沒被碰（存在與否跟開跑前一樣）")
chk("  state() 讀得出來", NF.state(now=datetime(2026, 6, 10, 20, 0))["ok"], True)

shutil.rmtree(TMP, ignore_errors=True)
print("\n" + ("全部通過 ✅" if FAIL == 0 else f"⛔ 有 {FAIL} 項沒過"))
sys.exit(1 if FAIL else 0)
