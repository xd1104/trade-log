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
    pos = {"dir": d, "entry": px + FAKE.get("slip", 1), "qty": 1, "entry_time": "21:35:04",
           "sl_points": sl_points, "tp_points": tp}
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
# ⭐ 2026-09-23 v3 驗收補：state() 要端出「現在是哪一條」—— 少了它，畫面上「目前在跑」標不出來、
#    點目前那一條會跳出「從（空白）換成…」的確認條、「不設停損」的揭露永遠不會出現。
chk("  state() 開著時端出 method（畫面靠它認得目前那一條）", NF.state()["method"], "T")
NF.ARM_FLAG.write_text("U", encoding="utf-8")
chk("  開關寫別的字 ⇒ 關著（⛔ 不猜）", NF.arm()["on"], False)
chk("  看不懂的開關檔 ⇒ state() 的 method 是 None（⛔ 不猜）", NF.state()["method"], None)
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

# ══ ⑦b ⭐⭐ 2026-09-23：**不變式換新的**（PM 授權）══════════════════════
#
# ⚠️⚠️ 舊的不變式是「**只有他自己建得出來** `NIGHT_ORDERS_ON`」——
#    Benson 2026-09-23 交辦「夜盤的開關也要做到畫面上」之後那條**已經不成立**了。
#    ⛔ 但**不可以只是把斷言刪掉**（那等於這塊從此沒人守）⇒ 換成更精確的一條：
#    **整個 repo 只有 `live_panel.night_arm_on()` 這一個地方建得出那個檔**，
#    而且它走的是 `O_CREAT|O_EXCL`（⇒ 結構上不可能蓋掉他已經有的那一個）。
#    ⛔ `night_fire` 那一半的老規矩一條都沒放寬（上面 ⑦ 照舊）：
#       **會送單的那個模組打不開自己的開關**。
print("\n=== ⑦b ⛔⛔ 只有 live_panel 的那個端點建得出 NIGHT_ORDERS_ON ===")
_lptree = ast.parse(lp)
_narm = next((n for n in ast.walk(_lptree)
              if isinstance(n, ast.FunctionDef) and n.name == "night_arm_on"), None)
say(_narm is not None, "  live_panel 有 night_arm_on()（唯一的建檔入口）")
_nlines = range(_narm.lineno, (_narm.end_lineno or _narm.lineno) + 1) if _narm else range(0)
say(_narm is not None and "O_EXCL" in ast.unparse(_narm),
    "  ⛔ 而且用 O_CREAT|O_EXCL（已經開著再按 ⇒ 409，⛔ 不覆蓋）")
say(_narm is not None and "night_fire.METHODS" in ast.unparse(_narm),
    "  ⛔ mode 拿 night_fire.METHODS 比（⛔ 不自己寫一份做法清單）")
# ⛔ 整個 tools/shioaji 掃一遍：除了 night_arm_on，沒有第二個地方碰得到那個檔名
_darm = next((n for n in ast.walk(_lptree)
              if isinstance(n, ast.FunctionDef) and n.name == "fire_arm_on"), None)
_dlines = range(_darm.lineno, (_darm.end_lineno or _darm.lineno) + 1) if _darm else range(0)
# ⭐ 2026-09-24：第三個具名入口 risk_override_on（風控規則 B 的手動解除，建 RISK_OVERRIDE）。
_rov = next((n for n in ast.walk(_lptree)
             if isinstance(n, ast.FunctionDef) and n.name == "risk_override_on"), None)
_rlines = range(_rov.lineno, (_rov.end_lineno or _rov.lineno) + 1) if _rov else range(0)
_creators = []
for _p in sorted(HERE.glob("*.py")):
    if _p.name.startswith("test_"):
        continue
    _s = _p.read_text(encoding="utf-8")
    for _n in ast.walk(ast.parse(_s)):
        if isinstance(_n, ast.Call) and "O_EXCL" in ast.unparse(_n):
            _where = _p.name
            if _p.name == "live_panel.py":
                _ln = getattr(_n, "lineno", -1)
                _where = ("night_arm_on" if _ln in _nlines
                          else ("fire_arm_on" if _ln in _dlines
                                else ("risk_override_on" if _ln in _rlines else "live_panel（別的地方）")))
            _creators.append(_where)
chk("  ⛔⛔ 整個 tools/shioaji 建得出開關檔的地方只有那三支"
    "（日盤 fire_arm_on／夜盤 night_arm_on／風控解除 risk_override_on）",
    sorted(set(_creators)), ["fire_arm_on", "night_arm_on", "risk_override_on"])
# 尺的自證：同一把尺在 live_panel 裡抓得到「日盤那一支」（⇒ 不是因為尺壞了才只有一個）
say(sum(1 for _n in ast.walk(_lptree)
        if isinstance(_n, ast.Call) and "O_EXCL" in ast.unparse(_n)) == 3,
    "  負控組：同一把尺在 live_panel 裡剛好抓到三個建檔點（日盤＋夜盤＋風控解除）")
say('self.path == "/api/nightfire/on"' in lp and 'self.path == "/api/nightfire/off"' in lp,
    "  ⛔ 夜盤有**自己的一組**端點（⛔ 沒有跟日盤共用同一支）")
say("night_fire.disarm()" in lp, "  ⛔ 關那一條走 night_fire.disarm()（改名不刪）")
m = lp[lp.index("def main():"):]
say("night_fire.start()" in m and m.index("try:", m.index("夜盤自動下單】台積電快攻")) < m.index("night_fire.start()"),
    "  main() 接線包在 try 裡")
say("broker.RECOVER_HOOK = _recover_chain" in lp and lp.count("broker.RECOVER_HOOK =") == 1,
    "  撿回鏈只接一次，而且是先夜盤後日盤那支")
ch = lp[lp.index("def _recover_chain"):lp.index("def main():")]
say(ch.index("night_fire.recover_meta") < ch.index("auto_fire.recover_meta"), "  鏈子順序：夜盤 → 日盤")
say(REAL_FLAG.exists() == REAL_EXISTED, "  ⛔ 真的 NIGHT_ORDERS_ON 沒被碰（存在與否跟開跑前一樣）")
chk("  state() 讀得出來", NF.state(now=datetime(2026, 6, 10, 20, 0))["ok"], True)


# ══════════════════════════════════════════════════════════════════════
print("\n=== ⑧ 夜盤跟勢（R，2026-09-23 接上送單）===")
# ⛔ 一樣全假：假券商、假報價、假分鐘收盤價、暫存的 trend_ctx.json。
import math  # noqa: E402
import random  # noqa: E402
import re  # noqa: E402

import pandas as pd  # noqa: E402

import sim_lanes  # noqa: E402
import trend_rule  # noqa: E402

NF.TREND_CTX = TMP / "trend_ctx.json"
MC = {}                                             # 假的 Today.minute_close（開始時間標記）
NF.configure(quote_fn=lambda: FAKE["quote"], session_fn=lambda now: FAKE["sess"],
             minute_close_fn=lambda m: MC.get(m))
TA = NF.trend_at(E)
W1 = date(2026, 1, 14)
chk("  夏令：夜盤跟勢 21:40 看", TA, datetime(2026, 6, 10, 21, 40))
chk("  冬令：22:40 看", NF.trend_at(W1), datetime(2026, 1, 14, 22, 40))
for _d in (E, W1):
    _t = NF.trend_at(_d)
    chk("  %s 跟模擬同一個時刻（us_open_min + ENTRY_OFFSET）" % _d,
        _t.hour * 60 + _t.minute, sim_lanes.us_open_min(_d) + trend_rule.ENTRY_OFFSET)


def ctx_write(n=40, last=E - timedelta(days=1), val=100.0):
    sig = {str(last - timedelta(days=i)): (val if i % 2 else -val * 0.5) for i in range(n)}
    NF.TREND_CTX.write_text(json.dumps({"sig": sig}), encoding="utf-8")


def r_reset(px=46000.0, ref=45800.0):
    reset()
    NF.ARM_FLAG.write_text("R", encoding="ascii")
    ctx_write()
    MC.clear()
    MC[21 * 60 + 9] = ref                          # 21:09 那一分鐘最後一筆 ＝ 模擬「21:10 那根」的收盤
    FAKE.update(quote=(px, 1.0), first=None)       # ⛔ 夜盤跟勢不看台積電（first 是 None 也要能做）


def r_rows():
    return [o for o in NF.rows_of(E) if o.get("rec") in ("skip", "result", "sending")]


# 門檻：過去 40 晚 |走幅| ＝ 一半 100、一半 50 ⇒ 第 80 百分位
thr_want = trend_rule.threshold([100.0 if i % 2 else -50.0 for i in range(40)])
SL0_WANT = float(math.floor(46000 * 0.02 * 0.975))   # 成交前帶的保守值
SL_WANT = float(math.floor(46001 * 0.02))            # 成交（假券商 +1 點滑價）後換成成交價 × 2%
r_reset()
run_until(T5, TA - timedelta(seconds=1))
chk("  ⛔ 開著 R：21:35 台積電那一刻不動、21:40 以前一張都不送", (CALLS["enter"], r_rows()), ([], []))
run_until(TA, TA + timedelta(seconds=30))
chk("  走 +200 點（≥ 門檻 %.0f）⇒ 送一張做多" % thr_want, [c[0] for c in CALLS["enter"]], ["long"])
c0 = CALLS["enter"][0] if CALLS["enter"] else (None,) * 4
chk("  ⛔⛔ 不設停利：broker.enter 的 tp 是 None（券商端一張限價單都不掛）", c0[2], None)
chk("  送單時先帶保守停損（報價 × 2% × 0.975）", c0[3], SL0_WANT)
chk("  ⭐ 成交後部位的停損換成 成交價 × 2%（無條件捨去）",
    (broker._state["position"] or {}).get("sl_points"), SL_WANT)
res = [o for o in NF.rows_of(E) if o.get("rec") == "result"]
chk("  帳本那一列標 method R、tp_points None、no_tp",
    (res[0].get("method"), res[0].get("tp_points"), res[0].get("no_tp")) if res else None, ("R", None, True))
chk("  帳本記下訊號、門檻、用了哪一分鐘（事後查得到為什麼做）",
    (res[0].get("mv"), res[0].get("thr"), res[0].get("ref_at")) if res else None,
    (200.0, round(thr_want, 1), "21:09"))
run_until(TA + timedelta(seconds=30), TA + timedelta(seconds=90))
chk("  ⛔ 一晚只送一次", len(CALLS["enter"]), 1)
# ⛔⛔ 偷看未來：歷史檔裡要是已經有今晚（或之後）的值，門檻一律不准用到
_c = json.loads(NF.TREND_CTX.read_text(encoding="utf-8"))
_c["sig"].update({str(E): 99999.0, str(E + timedelta(days=1)): 99999.0})
NF.TREND_CTX.write_text(json.dumps(_c), encoding="utf-8")
_past, _newest = NF.past_sigs(E)
chk("  ⛔ 門檻只用今晚以前的走幅（今晚與之後的值不准混進來）",
    (max(abs(v) for v in _past), _newest), (100.0, str(E - timedelta(days=1))))

print("  -- 不送的理由 --")
for name, setup, want in [
    ("走幅沒到門檻", lambda: FAKE.update(quote=(45850.0, 1.0)), "not_fast"),
    ("拿不到 30 分鐘前那一分鐘", lambda: MC.clear(), "no_ref"),
    ("歷史只有 10 晚", lambda: ctx_write(n=10), "no_hist"),
    ("歷史最新一晚是 20 天前（面板很久沒開）", lambda: ctx_write(last=E - timedelta(days=20)), "no_hist"),
    ("歷史檔不存在", lambda: NF.TREND_CTX.unlink(), "no_hist"),
    ("報價 30 秒沒更新", lambda: FAKE.update(quote=(46000.0, 30.0)), "no_quote"),
    ("不是夜盤時段", lambda: FAKE.update(sess="day"), "no_quote"),
    ("券商那一關擋下", lambda: FAKE.update(can=(False, "已有部位")), "cannot"),
]:
    r_reset()
    setup()
    run_until(TA, TA + timedelta(seconds=20))
    rs = [o for o in NF.rows_of(E) if o.get("rec") == "skip"]
    chk("  %s ⇒ 不送（%s）" % (name, want),
        (CALLS["enter"], rs[-1]["why"] if rs else None, rs[-1].get("method") if rs else None),
        ([], want, "R"))
r_reset(px=45600.0)
run_until(TA, TA + timedelta(seconds=20))
chk("  走 −200 點 ⇒ 做空", [c[0] for c in CALLS["enter"]], ["short"])

print("  -- ⛔⛔ 停損點數不可以超過面板的上限（超過會被換成手動 130 點）--")
_frac = float(re.search(r"^POS_POINTS_MAX_FRAC = ([0-9.]+)", lp, re.M).group(1))
chk("  面板的上限還是 2%（改了這裡要一起重想 R_SL_PCT）", _frac, 0.02)
_worst = None
for _px in (45987.0, 46025.0, 46030.0, 46049.0, 23012.5, 61237.0):
    for _slip in (-40, -30, -3, 0, 3, 30):
        r_reset(px=_px, ref=_px + 300)                  # 往下走 300 ⇒ 做空
        FAKE["slip"] = _slip
        run_until(TA, TA + timedelta(seconds=5))
        _p = broker._state["position"] or {}
        _sl0 = CALLS["enter"][0][3] if CALLS["enter"] else None
        for _v, _e in ((_p.get("sl_points"), _p.get("entry")), (_sl0, _px + _slip)):
            if _v is None or _e is None or _v > _e * _frac:
                _worst = (_px, _slip, _v, _e)
FAKE.pop("slip", None)
chk("  各種報價 × 滑價（做空成交比報價低 40 點也算）：送單時與成交後的停損都 ≤ 成交價 × 2%",
    _worst, None)
r_reset()
_late = TA + timedelta(seconds=NF.LATE_S + 5)
run_until(_late, _late + timedelta(seconds=20))
chk("  ⛔ 面板比 21:40 晚 2 分鐘以上才走到 ⇒ 不補單（late）", (CALLS["enter"], last_skip()), ([], "late"))

print("  -- 30 分鐘前那一分鐘：跟模擬對齊（面板開始時間標記 vs 1 分 K 結束時間標記）--")
MC.clear()
MC[21 * 60 + 9] = 45800.0
MC[21 * 60 + 10] = 45000.0                          # ⛔ 21:10 開始的那一分鐘 ≠ 模擬的「21:10 那根」
chk("  先找 21:09（＝模擬標籤 21:10 那根）", NF.ref_price(E), (45800.0, "21:09"))
MC.pop(21 * 60 + 9)
MC[21 * 60 + 6] = 45700.0
chk("  21:09～21:07 沒成交 ⇒ 往回找到 21:06（容忍 3 分鐘）", NF.ref_price(E), (45700.0, "21:06"))
MC.pop(21 * 60 + 6)
MC[21 * 60 + 5] = 45600.0
chk("  ⛔ 超過容忍（21:05）⇒ 拿不到，⛔ 不用更早的價", NF.ref_price(E), (None, None))
chk("  容忍度跟模擬是同一個數", sim_lanes.TREND_TOL, 3)

# 同一串假成交 ⇒ 模擬 trend_sig 與真單（現價 − ref_price）算出同一個走幅
rnd = random.Random(7)
_bad = None
for trial in range(40):
    trades, p = {}, 46000.0
    for mi in range(15 * 60, 22 * 60):              # 每分鐘最後一筆成交（開始時間標記）
        p += rnd.choice([-6, -3, 0, 3, 6])
        if rnd.random() < 0.3:                      # 有些分鐘沒有成交
            continue
        trades[mi] = p
    bars = pd.DataFrame([{"ts": datetime(2026, 6, 10, (mi + 1) // 60, (mi + 1) % 60),
                          "Open": v, "High": v, "Low": v, "Close": v} for mi, v in sorted(trades.items())])
    sim_mv = sim_lanes.trend_sig(E, bars)
    MC.clear()
    MC.update(trades)
    ks = [k for k in trades if k <= 21 * 60 + 39]
    last_mi = max(ks) if ks else None               # 21:40:01 那一刻的現價 ＝ 21:39 以前最後一筆
    ref, _ = NF.ref_price(E)
    live_mv = (round(trades[last_mi] - ref, 2)
               if (ref is not None and last_mi is not None and 21 * 60 + 39 - last_mi <= 3) else None)
    if sim_mv != live_mv:
        _bad = (trial, sim_mv, live_mv)
        break
chk("  40 串隨機成交（三成的分鐘沒成交）：模擬 trend_sig 與真單算出同一個走幅", _bad, None)

print("  -- 撿回部位（重啟）與 04:58 --")
r_reset()
run_until(TA, TA + timedelta(seconds=5))
pos = dict(broker._state["position"] or {})
_real_eo = NF.evening_of
NF.evening_of = lambda now: E                       # recover_meta 看「現在」是不是同一晚
got = NF.recover_meta(dict(pos))
NF.evening_of = _real_eo
chk("  ⛔ 撿回來：停損 2%、no_tp、⛔ 沒有 tp_points（不可以 float(None) 掉回手動 130）",
    got, {"sl_points": SL_WANT, "no_tp": True, "sl_src": "nightfire"})
broker._state["position"] = dict(pos, recovered=True, tp_points=130.0, sl_points=130.0, sl_src="unmatched")
NF._recover_poll()
p2 = broker._state["position"]
chk("  工作執行緒補正：停損換成 2%、no_tp、拿掉 130 的停利",
    (p2.get("sl_points"), p2.get("no_tp"), "tp_points" in p2, p2.get("sl_src")),
    (SL_WANT, True, False, "nightfire"))
broker._state["position"] = dict(pos)
E1 = E + timedelta(days=1)
run_until(datetime(E1.year, E1.month, E1.day, 4, 58, 0), datetime(E1.year, E1.month, E1.day, 4, 58, 10))
chk("  04:58 平掉夜盤跟勢那一口", CALLS["close"], ["night_eod"])

print("  -- 畫面 --")
st = NF.state(now=datetime(2026, 6, 10, 20, 0))
chk("  開著 R ⇒ state 的 method 是 R、今晚 21:40 看", (st["method"], st["tonight"]["look_at"]), ("R", "21:40"))
say("不設停利" in st["rules"]["R"] and "2%" in st["rules"]["R"], "  R 那句規則講得出「不設停利」與「2%」",
    st["rules"]["R"])
say(st["rules"]["T"] == st["rule"], "  舊紀錄的預設規則仍是台積電快攻那句")
NF.ARM_FLAG.write_text("T", encoding="ascii")
chk("  開著 T ⇒ 今晚 21:35 看", NF.state(now=datetime(2026, 6, 10, 20, 0))["tonight"]["look_at"], "21:35")
say(REAL_FLAG.exists() == REAL_EXISTED, "  ⛔ ⑧ 跑完真的 NIGHT_ORDERS_ON 仍然沒被碰")

shutil.rmtree(TMP, ignore_errors=True)
print("\n" + ("全部通過 ✅" if FAIL == 0 else f"⛔ 有 {FAIL} 項沒過"))
sys.exit(1 if FAIL else 0)
