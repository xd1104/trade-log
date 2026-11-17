"""
⭐ 2026-11-17 老闆拍板：口數升級改成自動（lots.AUTO_UPGRADE）。
   照規則能跳就跳、入金當下（live_check）到門檻就升；降級照舊自動。這支只測「自動」那一段：
   ① 入金當下到門檻 ⇒ 直接升、紀錄寫 by=auto、回 upgrade_auto 事件（面板拿去推播）
   ② 每日檢查到門檻 ⇒ 直接升
   ③ 條件沒過（策略不正常）⇒ 不升
   ④ 手上有部位 ⇒ 入金當下那條不跑（權益含未實現）
   ⑤ AUTO_UPGRADE=False ⇒ 只出建議、不升（退路）
   ⑥ 降級照舊自動、回 down_equity 事件
⛔ 寫檔全部導到暫存資料夾，不碰真的 LOTS_LEVEL.json。
用法：.venv\Scripts\python.exe -X utf8 tools\shioaji\test_lots_auto.py
"""
import json
import pathlib
import shutil
import sys
import tempfile
from datetime import datetime

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import lots  # noqa: E402

FAIL = 0


def chk(name, got, want):
    global FAIL
    ok = got == want
    FAIL += not ok
    print(("  OK   " if ok else "  FAIL ") + name + ("" if ok else "  (得到 %r，期待 %r)" % (got, want)))


REAL_LEVEL, REAL_LOG = lots.LEVEL_FILE, lots.LOG_DIR
TMP = pathlib.Path(tempfile.mkdtemp(prefix="lots-auto-"))
lots.LEVEL_FILE = TMP / "LOTS_LEVEL.json"
lots.LOG_DIR = TMP / "lots"
lots.VOL_CSV = TMP / "__no_such.csv"
lots.warm_night = lambda *a, **k: None          # ⛔ 不在背景讀真的振幅


def fresh(**kw):
    shutil.rmtree(lots.LOG_DIR, ignore_errors=True)
    st = json.loads(json.dumps(lots.DEFAULT))
    st.update(kw)
    lots.LEVEL_FILE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")


def ctx_ok(**kw):
    c = {"retire": {"union": "ok", "trend": "ok"}, "retire_err": None,
         "quality": {"ok": True, "n": 30, "need": 30, "slip_avg": 3.2, "errors": []}, "incidents": []}
    c.update(kw)
    return c


def logs():
    out = []
    for f in sorted(lots.LOG_DIR.glob("*")) if lots.LOG_DIR.exists() else []:
        out += [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
    return out


TP = dict(lots.TRIAL_DEFAULT, status="passed", passed_at="2026-10-21", jumped=True)
N = datetime(2026, 11, 2, 1, 0)
chk("預設是自動升級", lots.AUTO_UPGRADE, True)

print("\n=== ① 入金當下到門檻 ⇒ 直接升（能跳就跳）===")
fresh(level=5, checked="2026-11-01", trial=TP)
r = lots.live_check(351000.0, now=N, ctx=ctx_ok())
st, _ = lots.read_state()
kinds = [e["kind"] for e in r["events"]]
chk("  5 級 → 直接跳到 7 級（7＋3）", (st["level"], lots.ladder_lots(st["level"])), (7, (7, 3)))
chk("  回 upgrade_auto 事件（面板推播用）", "upgrade_auto" in kinds, True)
chk("  建議標成已確認、沒有 pending", (bool(st["suggest"].get("confirmed")), st.get("pending")), (True, None))
L = [x for x in logs() if x.get("kind") == "upgrade_effective"]
chk("  紀錄寫的是 by=auto", [x.get("by") for x in L], ["auto"])
msg = [e for e in r["events"] if e["kind"] == "upgrade_auto"][0]["msg"]
chk("  通知那句話開頭是「自動升級」", msg.startswith("自動升級"), True)
r = lots.live_check(351000.0, now=N, ctx=ctx_ok())
st, _ = lots.read_state()
chk("  再跑一次不會再升", (st["level"], [e["kind"] for e in r["events"]]), (7, []))

print("\n=== ② 每日檢查到門檻 ⇒ 直接升 ===")
fresh(level=5, checked="2026-11-01", trial=TP)
r = lots.daily_check([{"date": "2026-11-02", "equity": 351000.0}], now=datetime(2026, 11, 2, 14, 5), ctx=ctx_ok())
st, _ = lots.read_state()
chk("  14:05 每日檢查 ⇒ 升到 7 級、回傳的 level 也是 7", (st["level"], r["level"]), (7, 7))
chk("  回 upgrade_auto 事件", "upgrade_auto" in [e["kind"] for e in r["events"]], True)

print("\n=== ③ 條件沒過 ⇒ 不升 ===")
fresh(level=5, checked="2026-11-01", trial=TP)
r = lots.daily_check([{"date": "2026-11-02", "equity": 351000.0}], now=datetime(2026, 11, 2, 14, 5),
                     ctx=ctx_ok(retire={"union": "ok", "trend": "down"}))
st, _ = lots.read_state()
chk("  夜盤策略在「降級」⇒ 不升、沒有 upgrade_auto", (st["level"], "upgrade_auto" in [e["kind"] for e in r["events"]]),
    (5, False))

print("\n=== ④ 手上有部位 ⇒ 入金當下那條不跑 ===")
fresh(level=5, checked="2026-11-01", trial=TP)
r = lots.live_check(351000.0, now=N, ctx=ctx_ok(), busy=True)
st, _ = lots.read_state()
chk("  有部位 ⇒ 不升", (r["ran"], st["level"]), (False, 5))

print("\n=== ⑤ AUTO_UPGRADE=False ⇒ 只出建議（退路）===")
lots.AUTO_UPGRADE = False
fresh(level=5, checked="2026-11-01", trial=TP)
r = lots.live_check(351000.0, now=N, ctx=ctx_ok())
st, _ = lots.read_state()
chk("  只出建議、級數不變", (st["level"], st["suggest"]["ok"], bool(st["suggest"].get("confirmed"))), (5, True, False))
lots.AUTO_UPGRADE = True

print("\n=== ⑥ 降級照舊自動 ===")
fresh(level=7, checked="2026-11-01", trial=TP)
thr7 = lots._ladder()[6][0]
r = lots.daily_check([{"date": "2026-11-02", "equity": thr7 * 0.8}], now=datetime(2026, 11, 2, 14, 5), ctx=ctx_ok())
st, _ = lots.read_state()
chk("  權益跌破 85% ⇒ 自動往下、回 down_equity 事件",
    (st["level"] < 7, "down_equity" in [e["kind"] for e in r["events"]]), (True, True))

chk("⛔ 沒碰到真的狀態檔路徑", (lots.LEVEL_FILE != REAL_LEVEL, lots.LOG_DIR != REAL_LOG), (True, True))
shutil.rmtree(TMP, ignore_errors=True)
print("\n總結：" + ("全部通過 ✅" if not FAIL else "⛔ 有 %d 項沒過" % FAIL))
sys.exit(1 if FAIL else 0)
