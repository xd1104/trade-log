# -*- coding: utf-8 -*-
"""
【模擬】推導三條（夜盤跟勢只做多／夜盤聯軍／聯軍留倉）的探針（2026-09-23 深夜）。
⛔ 全部假資料、暫存資料夾；⛔ 不連永豐、不碰真的 sim_lanes/。
在守的事：
  ① 只做多：做多照抄、做空改不做、本尊沒定論 ⇒ pending
  ② 夜盤聯軍：跟勢有做 ⇒ 照抄；只有台積電快攻 ⇒ 21:40 進場、方向與框寬照它、停利／停損／收盤三種出場；
     兩條都不做 ⇒ 不做；任一條沒定論 ⇒ pending
  ③ 聯軍留倉：日盤就出場 ⇒ 照抄；結算日 ⇒ 不留；收盤沒出場 ⇒ 夜盤停損／停利／04:58；開箱沒有停利；
     記下擋掉的夜盤跟勢點數；夜盤資料沒到齊 ⇒ pending
  ④ step() 排在本尊之後、背景跑出來的列跟回填同一支正本
"""
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.stdout.reconfigure(encoding="utf-8")
import sim_lanes as S  # noqa: E402

FAIL = 0
REAL_DIR = S.SIM_DIR


def say(cond, name, extra=""):
    global FAIL
    FAIL += not cond
    print(("  OK   " if cond else "  FAIL ") + name + (f"  {extra}" if extra else ""))


def chk(name, got, want):
    global FAIL
    ok = got == want
    FAIL += not ok
    print(("  OK   " if ok else "  FAIL ") + name + ("" if ok else f"  (得到 {got!r}，期待 {want!r})"))


TMP = Path(tempfile.mkdtemp(prefix="derived-test-"))
S.SIM_DIR = TMP / "sim_lanes"
E = date(2026, 6, 10)                   # 週三，夏令 ⇒ 美股 21:30、跟勢 21:40


def night_bars(path):
    """path(分鐘) ⇒ 價；做出 E 15:01 ~ E+1 05:00 的 1 分 K（結束時間標記，高低＝收盤±1）"""
    rows = []
    t = datetime(E.year, E.month, E.day, 15, 1)
    end = datetime(E.year, E.month, E.day, 5, 0) + timedelta(days=1)
    m = 15 * 60 + 1
    while t <= end:
        p = path(m)
        rows.append({"ts": t.strftime("%Y-%m-%d %H:%M:%S"), "High": p + 1, "Low": p - 1, "Close": p})
        t += timedelta(minutes=1)
        m += 1
    return pd.DataFrame(rows)


FLAT = night_bars(lambda m: 46000.0)
T40 = 21 * 60 + 40


def trend(decision, pts=100.0):
    r = {"lane": "trend", "date": str(E), "decision": decision, "why": "trend_fast" if decision != "不做" else "not_fast",
         "reason": "假的", "points": pts if decision != "不做" else None, "entry": 46000.0, "exit": 46100.0,
         "exit_reason": "收盤" if decision != "不做" else None, "c_label": "21:40", "at": "21:40"}
    return r


def tsm(decision, w=200.0):
    return {"lane": "tsm", "date": str(E), "decision": decision, "why": "tsm_fast" if decision != "不做" else "not_fast",
            "reason": "假的", "tpsl_points": w if decision != "不做" else None, "points": 1.0}


print("=== ① 夜盤跟勢只做多 ===")
r = S.tlong_eval(E, trend("做多", 123.0))
chk("  做多 ⇒ 照抄（點數一樣、lane 換成 tlong）", (r["lane"], r["decision"], r["points"]), ("tlong", "做多", 123.0))
r = S.tlong_eval(E, trend("做空", -50.0))
chk("  做空 ⇒ 不做、記下它的點數", (r["decision"], r["why"], r["trend_points"]), ("不做", "short_skip", -50.0))
chk("  跟勢不做 ⇒ 不做", S.tlong_eval(E, trend("不做"))["decision"], "不做")
say(S.tlong_eval(E, None).get("pending"), "  跟勢沒定論 ⇒ pending（⛔ 不猜）")

print("\n=== ② 夜盤聯軍 ===")
r = S.nunion_eval(E, FLAT, trend("做空", 77.0), tsm("做多"))
chk("  跟勢有做 ⇒ 照跟勢（⛔ 不管台積電）", (r["decision"], r["points"], r["from"]), ("做空", 77.0, "trend"))
chk("  兩條都不做 ⇒ 不做", S.nunion_eval(E, FLAT, trend("不做"), tsm("不做"))["decision"], "不做")
say(S.nunion_eval(E, FLAT, None, tsm("做多")).get("pending"), "  跟勢沒定論 ⇒ pending")
say(S.nunion_eval(E, FLAT, trend("不做"), None).get("pending"), "  台積電沒定論 ⇒ pending")
up = night_bars(lambda m: 46000.0 + (300.0 if m >= T40 + 30 else 0.0))       # 22:10 起漲 300
r = S.nunion_eval(E, up, trend("不做"), tsm("做多", 200.0))
chk("  只有台積電快攻做多 ⇒ 21:40 進場、漲 300 碰到停利 200", (r["decision"], r["at"], r["exit_reason"], r["exit"]),
    ("做多", "21:40", "停利", 46200.0))
chk("    點數 ＝ 200 − 成本 7", r["points"], 193.0)
r = S.nunion_eval(E, up, trend("不做"), tsm("做空", 200.0))
chk("  台積電做空、漲 300 ⇒ 停損 −200−7", (r["exit_reason"], r["points"]), ("停損", -207.0))
r = S.nunion_eval(E, FLAT, trend("不做"), tsm("做多", 200.0))
chk("  整晚沒動 ⇒ 04:58 收盤、只扣成本", (r["exit_reason"], r["exit_label"], r["points"]), ("收盤", "04:58", -7.0))
half = night_bars(lambda m: 46000.0 if m < 23 * 60 else 45000.0)
half = half[pd.to_datetime(half["ts"]) < datetime(E.year, E.month, E.day, 23, 0)]
say(S.nunion_eval(E, half, trend("不做"), tsm("做多")).get("pending"), "  1 分 K 沒到齊 ⇒ pending")

print("\n=== ③ 聯軍留倉 ===")


def union(exit_reason="收盤", d="做多", cutoff="13:43:30", tp=230.0, sl=None, entry=46000.0):
    r = {"lane": "union", "date": str(E), "decision": d, "why": "union", "reason": "照「快攻」做多",
         "entry": entry, "exit": entry + 10, "exit_reason": exit_reason, "points": 5.0, "cost": 5.0,
         "cutoff": cutoff, "pick": "fast", "at": "09:03:30"}
    if tp:
        r["tpsl_points"] = tp
    if sl:
        r["sl_points"] = sl
    return r


r = S.hold_eval(E, FLAT, union("停利"), None)
chk("  日盤就出場（停利）⇒ 照抄聯軍", (r["lane"], r["points"], r.get("held")), ("hold", 5.0, None))
r = S.hold_eval(E, FLAT, union("收盤", cutoff="13:30:00"), None)
chk("  結算日 ⇒ 不留倉（照抄）", (r["points"], r.get("held")), (5.0, None))
chk("  聯軍不做 ⇒ 不做", S.hold_eval(E, FLAT, dict(union(), decision="不做"), None)["decision"], "不做")
say(S.hold_eval(E, FLAT, None, None).get("pending"), "  聯軍沒定論 ⇒ pending")
r = S.hold_eval(E, FLAT, union("收盤"), trend("做多", 88.0))
chk("  收盤沒出場、整晚沒動 ⇒ 04:58 平、扣 5+1", (r["held"], r["exit_reason"], r["exit_label"], r["points"]),
    (True, "收盤", "04:58", -6.0))
chk("  記下這晚擋掉的夜盤跟勢", r["trend_lost"], 88.0)
chk("  日盤那段的點數留著（對照用）", r["day_points"], 5.0)
r = S.hold_eval(E, up, union("收盤", tp=230.0), None)
chk("  夜盤漲 300 ⇒ 碰到原本的停利 230", (r["exit_reason"], r["exit"], r["points"]), ("停利", 46230.0, 224.0))
dn = night_bars(lambda m: 46000.0 - (300.0 if m >= T40 else 0.0))
r = S.hold_eval(E, dn, union("收盤", tp=230.0), None)
chk("  夜盤跌 300 ⇒ 碰到原本的停損 230", (r["exit_reason"], r["exit"], r["points"]), ("停損", 45770.0, -236.0))
r = S.hold_eval(E, up, union("收盤", tp=None, sl=150.0), None)
chk("  開箱那一口（沒有停利）漲 300 ⇒ ⛔ 不停利、抱到 04:58", (r["exit_reason"], r["points"]), ("收盤", 294.0))
say(S.hold_eval(E, half, union("收盤"), None).get("pending"), "  夜盤資料沒到齊 ⇒ pending")
chk("  沒有夜盤跟勢那晚 ⇒ trend_lost 是 None", S.hold_eval(E, FLAT, union("收盤"), trend("不做"))["trend_lost"], None)

print("\n=== ④ 背景一輪與回填走同一支正本 ===")
rows = {("trend", str(E)): trend("不做"), ("tsm", str(E)): tsm("做多", 200.0), ("union", str(E)): union("收盤")}
S._NIGHT_API[E] = up
S.night_evenings = lambda now: [E]
S._step_derived(datetime(2026, 6, 11, 6, 0), rows)
got = {k: rows.get((k, str(E))) for k in S.DERIVED_LANES}
chk("  三條都落地", sorted(k for k, v in got.items() if v), sorted(S.DERIVED_LANES))
chk("  跟直接叫正本一樣（夜盤聯軍）", got["nunion"]["points"], S.nunion_eval(E, up, trend("不做"), tsm("做多"))["points"])
chk("  跟直接叫正本一樣（聯軍留倉）", got["hold"]["points"], S.hold_eval(E, up, union("收盤"), trend("不做"))["points"])
back, _ = S.read_rows()
chk("  真的寫進（暫存的）sim_lanes/", sorted(k for (k, d) in back if d == str(E)), sorted(S.DERIVED_LANES))
say(all(S.STATE["pending"][k] == {} for k in S.DERIVED_LANES), "  沒有殘留 pending")
src = (HERE / "sim_lanes.py").read_text(encoding="utf-8")
st = src[src.index("def step("):src.index("def loop(")]
say(st.index("_step_trend(") < st.index("_step_derived("), "  ⛔ step() 裡推導那步排在本尊之後")
say(all(("\"%s\"" % k) in src for k in S.DERIVED_LANES), "  三條都有規則說明")
say(all(S._rule_text(k) and "勝率" not in S._rule_text(k) for k in S.DERIVED_LANES),
    "  規則說明有字、⛔ 沒有「勝率」")
say(REAL_DIR.exists() is REAL_DIR.exists() and S.SIM_DIR != REAL_DIR, "  ⛔ 全程寫的是暫存區，不是真的 sim_lanes/")

shutil.rmtree(TMP, ignore_errors=True)
print("\n" + ("全部通過 ✅" if FAIL == 0 else f"⛔ 有 {FAIL} 項沒過"))
sys.exit(1 if FAIL else 0)
