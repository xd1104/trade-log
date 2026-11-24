# -*- coding: utf-8 -*-
"""
⭐ 2026-11-24 停損平不掉（Benson：「兩項都做」）。純離線、不連永豐、不送單、不推手機（全部換成假的）。

  ① lock_limit：做多貼跌停／做空貼漲停才算鎖死；方向反了、離很遠、基準拿不到 ⇒ None
  ② sl_failed：第一次馬上推、5 分鐘內不重推、換一筆部位馬上推；冷卻中／正在平／沒有部位／排隊中 ⇒ 不推不排
  ③ lock_try：鎖在停板 ⇒ 用現價排隊、結果有變才推
  ④ check_real_position：⛔ 停損迴圈不直接碰排隊（全丟背景）；排隊中鎖著 ⇒ 不送 IOC；鎖打開 ⇒ 背景先確認排隊單；
     換盤 ⇒ 照常平倉（價格回到停損之內也平）
     lock_release：排隊單已成交／問不到 ⇒ ⛔ 不送 IOC（品管 #2：平過頭）；沒成交完 ⇒ 平倉
     lock_verify：排隊單被撤／被拒 ⇒ 清掉重排＋推
  ④b sl_job：一次只跑一件、有間隔
  ⑤ broker.sess_key：日盤／夜盤／過午夜
  ⑥ broker.lock_queue：一口部位 ⛔ 不叫 _cancel_targets（品管 #1：target_trades 變 {} 會改走混搭路、成績單沒點數）；
     停利單已成交／撤不掉／對帳失敗／口數分不清 ⇒ 不掛；死單不撤（品管 #3）；帳本／混搭走各自的撤單
     lock_q_check：none／open／filled／dead／unknown
     收盤平倉：排隊中不撤排隊單
  ⑦ 撤停利單會清掉 lock_q
跑法：.venv\\Scripts\\python.exe -X utf8 tools\\shioaji\\test_lock_queue.py
"""
import sys
import threading
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.stdout.reconfigure(encoding="utf-8")

import testkit  # noqa: E402
testkit.sandbox()
import broker            # noqa: E402
import live_panel as LP  # noqa: E402

FAIL = []


def chk(name, got, want):
    ok = got == want
    print(("  OK   " if ok else "  FAIL ") + name + ("" if ok else "  (得到 %r，期待 %r)" % (got, want)))
    if not ok:
        FAIL.append(name)


DAY = datetime(2026, 11, 24, 10, 0)
NIGHT = datetime(2026, 11, 23, 23, 0)

print("① lock_limit")
orig_prev, orig_dc = LP.prev_day_close, LP.night_fire.day_close
LP.prev_day_close = lambda d: 40000.0          # 跌停 36000、漲停 44000
LP.night_fire.day_close = lambda: 36583.0      # 漲停 40241
chk("做多、貼跌停", LP.lock_limit(1, 36000.0, DAY), 36000)
chk("做多、跌停上 100 點（容忍 120）", LP.lock_limit(1, 36100.0, DAY), 36000)
chk("做多、跌停上 200 點 ⇒ 不算", LP.lock_limit(1, 36200.0, DAY), None)
chk("做空、貼漲停", LP.lock_limit(-1, 44000.0, DAY), 44000)
chk("做多、貼漲停（順向）⇒ 不算", LP.lock_limit(1, 44000.0, DAY), None)
chk("夜盤用日盤收盤當基準：做空貼漲停", LP.lock_limit(-1, 40241.0, NIGHT), 40241)
LP._PREVC[DAY.date()] = None
LP.prev_day_close = lambda d: None
chk("基準拿不到 ⇒ None", LP.lock_limit(1, 36000.0, DAY), None)
chk("問不到 ⇒ 不快取一整天", DAY.date() in LP._PREVC, False)
LP.prev_day_close = lambda d: 1 / 0
chk("基準出錯 ⇒ None", LP.lock_limit(1, 36000.0, DAY), None)
LP.prev_day_close, LP.night_fire.day_close = orig_prev, orig_dc

print("② sl_failed")
pushes, jobs, tried = [], [], []


def fake_push(t, b):
    pushes.append((t, b))


def fake_job(fn, gap=0.0):
    jobs.append(fn)


LP.SL_FAIL.update(key=None, at=0.0, q=None)
pos = {"dir": "long", "entry": 37000.0, "entry_time": "09:05:00"}
for msg in ("剛剛平倉失敗，10 秒後才會再試", "正在平倉中，請稍候", "沒有部位", "停板排隊中（36000）"):
    LP.sl_failed(pos, 1, 36800.0, msg, now=1000.0, push=fake_push, job=fake_job)
chk("冷卻中／正在平／沒有部位／排隊中 ⇒ 不推不排", (len(pushes), len(jobs)), (0, 0))
LP.sl_failed(pos, 1, 36800.0, "平不掉（範圍市價 IOC 連續 3 次沒撮到）", now=1000.0, push=fake_push, job=fake_job)
chk("第一次失敗 ⇒ 推一則、排一件背景工", ([t for t, _ in pushes], len(jobs)), (["停損平不掉"], 1))
LP.sl_failed(pos, 1, 36800.0, "平不掉", now=1200.0, push=fake_push, job=fake_job)
chk("5 分鐘內 ⇒ 不重推", len(pushes), 1)
LP.sl_failed(pos, 1, 36800.0, "平不掉", now=1301.0, push=fake_push, job=fake_job)
chk("超過 5 分鐘 ⇒ 再推", len(pushes), 2)
pos2 = {"dir": "short", "entry": 38000.0, "entry_time": "22:40:02"}
LP.sl_failed(pos2, -1, 38500.0, "平不掉", now=1302.0, push=fake_push, job=fake_job)
chk("換一筆部位 ⇒ 馬上推", len(pushes), 3)
LP.sl_failed(pos2, -1, 38500.0, "平不掉", now=1303.0, push=fake_push, job=lambda f: 1 / 0)
chk("背景工出錯 ⇒ 吞掉不拋", True, True)

print("③ lock_try")
orig_ll = LP.lock_limit
LP.lock_limit = lambda d, p, now=None: None
pushes.clear()
chk("沒鎖 ⇒ 不排隊", (LP.lock_try(1, 36800.0, push=fake_push, queue=lambda px: tried.append(px)), tried), (None, []))
LP.lock_limit = lambda d, p, now=None: 36000
LP.SL_FAIL["q"] = None
LP.lock_try(1, 36000.0, push=fake_push, queue=lambda px: (tried.append(px) or (True, "已在停板價 36000 掛單排隊")))
chk("鎖死 ⇒ 用現價排隊、推一則", (tried, [t for t, _ in pushes]), ([36000.0], ["停板鎖死：停損改排隊"]))
LP.lock_try(1, 36000.0, push=fake_push, queue=lambda px: (True, "已在停板價 36000 掛單排隊"))
chk("同一個結果不重推", len(pushes), 1)
LP.lock_try(1, 36000.0, push=fake_push, queue=lambda px: (True, "已經在停板價 36000 排隊"))
chk("「已經在排隊」不推", len(pushes), 1)
LP.lock_limit = orig_ll

print("④ check_real_position／lock_verify／lock_release")
calls = []
orig_close, orig_sf, orig_job = broker.close, LP.sl_failed, LP.sl_job
broker.close = lambda reason: (calls.append(reason) or (False, "平不掉"))
LP.sl_failed = lambda *a, **k: calls.append("sl_failed")
LP.sl_job = lambda fn, gap=0.0: calls.append(("job", fn.__code__.co_names[0] if fn.__code__.co_names else "?"))
key = broker.sess_key()
p = {"dir": "long", "entry": 37000.0, "sl_points": 185.0, "lock_q": {"px": 36000, "sess": key}}
broker._state["position"] = p
LP.check_real_position(36000.0, 0.1, "live")
chk("排隊中、還鎖在停板 ⇒ 不送 IOC、背景確認", calls, [("job", "lock_verify")])
calls.clear()
LP.check_real_position(36010.0, 0.1, "live")
chk("鎖打開 ⇒ 不直接送 IOC、交給背景先確認排隊單", calls, [("job", "lock_release")])
calls.clear()
p["lock_q"] = {"px": 36000, "sess": "2000-01-01D"}
LP.check_real_position(37500.0, 0.1, "live")
chk("換盤（ROD 已失效）⇒ 照常平倉（價格回到停損之內也平）", (calls, "lock_q" in p), (["sl", "sl_failed"], False))
calls.clear()
p.pop("sl_armed")
LP.check_real_position(36900.0, 0.1, "live")
chk("沒排隊、沒碰停損 ⇒ 不送", calls, [])
broker.close = lambda reason: (calls.append(reason) or (True, None))
LP.check_real_position(36800.0, 0.1, "live")
chk("碰停損、平掉 ⇒ 不走 sl_failed", calls, ["sl"])
broker.close, LP.sl_failed, LP.sl_job = orig_close, orig_sf, orig_job
broker._state["position"] = None

calls.clear()
CL = lambda r: calls.append(r) or (False, "平不掉")       # noqa: E731
FA = lambda *a, **k: calls.append("failed")               # noqa: E731
p = {"dir": "long", "lock_q": {"px": 36000}}
for st in ("filled", "unknown", "open"):
    LP.lock_release(p, 1, 36010.0, check=lambda st=st: st, close=CL, failed=FA, now=100.0)
chk("往有利方向打開：成交／問不到／明細說還在排（回報慢）⇒ ⛔ 不送 IOC（品管 #B 平過頭）", (calls, "lock_q" in p), ([], True))
LP.lock_release(p, 1, 36010.0, check=lambda: "filled", close=CL, failed=FA, now=144.0)
chk("44 秒 ⇒ 還在等", calls, [])
LP.lock_release(p, 1, 36010.0, check=lambda: "filled", close=CL, failed=FA, now=146.0)
chk("超過 45 秒部位還在 ⇒ 照常平倉（⛔ 停損不准永遠等，品管 #A）", (calls, "lock_q" in p, p.get("sl_armed")),
    (["sl", "failed"], False, True))
calls.clear()
p = {"dir": "long", "lock_q": {"px": 36000}}
LP.lock_release(p, 1, 35990.0, check=lambda: "open", close=CL, failed=FA, now=100.0)
chk("往不利方向離開（不是真停板）、沒成交 ⇒ 馬上撤掉平倉", (calls, "lock_q" in p), (["sl", "failed"], False))
calls.clear()
p = {"dir": "short", "lock_q": {"px": 44000}}
LP.lock_release(p, -1, 43990.0, check=lambda: "open", close=CL, failed=FA, now=100.0)
chk("做空：往下打開＝有利 ⇒ 等", calls, [])
p = {"dir": "long", "lock_q": {"px": 36000}}
pushes.clear()
LP.lock_verify(p, push=fake_push, check=lambda: "open", now=0.0)
chk("排隊單還活著 ⇒ 不動", ("lock_q" in p, pushes), (True, []))
LP.lock_verify(p, push=fake_push, check=lambda: "filled", now=10.0)
LP.lock_verify(p, push=fake_push, check=lambda: "filled", now=50.0)
chk("鎖著、說成交了 40 秒 ⇒ 還等", "lock_q" in p, True)
LP.lock_verify(p, push=fake_push, check=lambda: "filled", now=56.0)
chk("鎖著、說成交了但部位還在超過 45 秒 ⇒ 清掉 lock_q（停損迴圈照常平）", ("lock_q" in p, p.get("sl_armed")), (False, True))
p = {"dir": "long", "lock_q": {"px": 36000}}
LP.lock_verify(p, push=fake_push, check=lambda: "dead")
chk("排隊單被撤／被拒 ⇒ 清掉 lock_q＋推一則", ("lock_q" in p, len(pushes), p.get("sl_armed")), (False, 1, True))

print("④b sl_job")
ev = threading.Event()
LP.SL_JOB.update(th=None, at=0.0)
chk("第一件排上", LP.sl_job(lambda: ev.wait(2)), True)
chk("還在跑 ⇒ 第二件不排", LP.sl_job(lambda: None), False)
ev.set()
LP.SL_JOB["th"].join(2)
chk("跑完但在間隔內 ⇒ 不排", LP.sl_job(lambda: None, gap=60), False)
LP.SL_JOB["at"] = 0.0
chk("過了間隔 ⇒ 排上", LP.sl_job(lambda: None, gap=60), True)
LP.SL_JOB["th"].join(2)

print("⑤ sess_key")
chk("日盤", broker.sess_key(datetime(2026, 11, 24, 9, 0)), "2026-11-24D")
chk("夜盤 15:00", broker.sess_key(datetime(2026, 11, 24, 15, 0)), "2026-11-24N")
chk("夜盤過午夜算前一天", broker.sess_key(datetime(2026, 11, 25, 4, 50)), "2026-11-24N")

print("⑥ lock_queue")
saved = {k: getattr(broker, k) for k in ("is_live", "reconcile", "_cancel_targets", "_own_cancel_targets",
                                          "place_target", "_cancel_logged_targets")}
log = []


class T:
    def __init__(self, qty=1, dealt=0, st="PendingSubmit"):
        self.order = type("O", (), {"quantity": qty, "code": "TMF"})()
        deals = [type("D", (), {"quantity": dealt, "price": 36000})()] if dealt else []
        self.status = type("S", (), {"status": st, "deals": deals})()


class API:
    def update_status(self, acct):
        log.append("upd")

    def cancel_order(self, t):
        log.append("cxl1")


class API2(API):
    def cancel_order(self, t):
        raise RuntimeError("x")


broker._state["api"] = API()
broker.is_live = lambda: True
broker._cancel_logged_targets = lambda pos: False
broker.place_target = lambda px, qty=None: (log.append(("place", px, qty)) or (True, None))
broker._own_cancel_targets = lambda pos: (log.append("own_cancel") or False)
broker._cancel_targets = lambda pos: (log.append("mix_cancel") or False)
broker.reconcile = lambda: broker._state["position"]


def setpos(**kw):
    d = {"dir": "long", "entry": 37000.0, "qty": 1, "product": "TMF", "target_trade": None, "target_trades": None}
    d.update(kw)
    broker._state["position"] = d
    return d


def L():
    return [x for x in log if x != "upd"]


p = setpos(target_trade=T())
ok, msg = broker.lock_queue(36000.4)
chk("一口：自己撤停利單、⛔ 不叫 _cancel_targets、target_trades 仍是 None", (L(), p["target_trades"]),
    (["cxl1", ("place", 36000, 1)], None))
chk("記下 lock_q", (p.get("lock_q") or {}).get("px"), 36000)
log.clear()
ok, msg = broker.lock_queue(36000)
chk("同一盤同一價 ⇒ 不撤不掛", (ok, L()), (True, []))
p["lock_q"]["sess"] = "2000-01-01D"
p["target_trade"] = T(st="Cancelled")
broker.lock_queue(36000)
chk("換盤、舊單已失效 ⇒ 不撤死單、直接重掛", L(), [("place", 36000, 1)])
log.clear()
p = setpos(target_trade=T(dealt=1))
ok, _ = broker.lock_queue(36000)
chk("停利單已有成交（部位回報慢）⇒ 不撤不掛", (ok, L()), (False, []))
log.clear()
broker._state["api"] = API2()
p = setpos(target_trade=T())
ok, _ = broker.lock_queue(36000)
chk("一口：停利單撤不掉 ⇒ 不掛", (ok, L()), (False, []))
broker._state["api"] = API()
log.clear()
broker.reconcile = lambda: "unknown"
ok, _ = broker.lock_queue(36000)
chk("對帳失敗 ⇒ 不撤不掛", (ok, L()), (False, []))
broker.reconcile = lambda: broker._state["position"]
setpos(own=True, qty=3, entry_qty=3)
broker.lock_queue(36000)
chk("帳本部位 ⇒ 撤帳本的停利單、掛券商剩的口數", L(), ["own_cancel", ("place", 36000, 3)])
log.clear()
setpos(product="MIX", mix={"MXF": 1, "TMF": 2}, target_trades={})
broker.lock_queue(36000)
chk("混搭 ⇒ 走 _cancel_targets", L(), ["mix_cancel", ("place", 36000, None)])
log.clear()
for kw, name in (({"multi_unknown": True}, "分不出是誰的"), ({"qty": 2}, "手動／撿回的多口"),
                 ({"qty_sent": 3, "qty": 3}, "自己開的多口（非帳本）")):
    setpos(**kw)
    ok, _ = broker.lock_queue(36000)
    chk(name + " ⇒ 不掛", (ok, L()), (False, []))
    log.clear()
def half(px, qty=None):
    broker._state["position"]["target_trades"] = {"MXF": T()}
    return False, "TMF：券商拒絕"


broker.place_target = half
p = setpos(product="MIX", mix={"MXF": 1, "TMF": 2}, target_trades={})
ok, msg = broker.lock_queue(36000)
chk("混搭只掛上一腿 ⇒ 不記 lock_q、撤掉那一腿（品管 #A）", (ok, "lock_q" in p, L()), (False, False, ["mix_cancel", "mix_cancel"]))
log.clear()
broker.place_target = lambda px, qty=None: (False, "券商拒絕")
p = setpos()
ok, msg = broker.lock_queue(36000)
chk("掛不上 ⇒ 回失敗、不記 lock_q", (ok, "lock_q" in p), (False, False))

print("  lock_q_check")
setpos()
chk("沒在排隊 ⇒ none", broker.lock_q_check(), "none")
p = setpos(lock_q={"px": 36000}, target_trade=T())
chk("還在排 ⇒ open", broker.lock_q_check(), "open")
p["target_trade"] = T(dealt=1, st="Filled")
chk("全部成交 ⇒ filled", broker.lock_q_check(), "filled")
p["target_trade"] = T(st="Cancelled")
chk("被撤、沒成交 ⇒ dead", broker.lock_q_check(), "dead")
p["target_trade"] = None
chk("找不到那張單 ⇒ unknown", broker.lock_q_check(), "unknown")
setpos(lock_q={"px": 36000}, own=True, own_trades=[{"role": "target", "t": T(qty=3, dealt=1)}, {"role": "entry", "t": None}])
chk("帳本：部分成交 ⇒ open", broker.lock_q_check(), "open")

print("  收盤平倉不撤排隊單")
for k, v in saved.items():
    setattr(broker, k, v)
p = setpos(lock_q={"px": 36000, "sess": broker.sess_key()})
ok, msg = broker.close("eod")
chk("日盤收盤 ⇒ 不平、講停板排隊中", (ok, str(msg).startswith("停板排隊中")), (False, True))
ok, msg = broker.close("night_eod")
chk("夜盤收盤 ⇒ 同上", (ok, str(msg).startswith("停板排隊中")), (False, True))
p["lock_q"]["sess"] = "2000-01-01D"
ok, msg = broker.close("eod")
chk("上一盤的排隊（已失效）⇒ 照常走平倉", str(msg).startswith("停板排隊中"), False)

print("⑦ 撤停利單清掉 lock_q")
broker.is_live = lambda: True
broker._update_status = lambda: None
q = {"lock_q": {"px": 1}, "own_trades": []}
broker._own_cancel_targets(q)
chk("帳本路徑撤完 ⇒ 沒有 lock_q", "lock_q" in q, False)
q2 = {"lock_q": {"px": 1}, "product": "TMF", "target_trade": T()}
broker._cancel_targets(q2)
chk("混搭路徑撤完 ⇒ 沒有 lock_q", "lock_q" in q2, False)
broker.is_live = saved["is_live"]
broker._state["position"] = None
broker._state["api"] = None

print()
print("全部通過" if not FAIL else "⛔ 失敗 %d 項：%s" % (len(FAIL), "、".join(FAIL)))
sys.exit(1 if FAIL else 0)
