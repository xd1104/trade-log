# -*- coding: utf-8 -*-
"""
⭐ 2026-11-27 查詢預算（永豐：每月查詢壓到 1,500 次以下）。純離線、不連永豐、不送單、不推手機。

  ① qbudget.Counted：會連券商的方法記一筆、其他屬性原樣轉；quote.subscribe 也記；list_trades 記在 local（不算總數）
  ② K 棒快取：過去的日子記住、今天 TTL 內重用、fresh() 一定重問、空結果不永久記
  ③ optional_ok：今天／本月過線就 False；WARN_AT 每條只推一次；落地檔讀回來接著記
  ④ broker._recon_due：第一次、最短 3 秒、被提醒（5／30 秒）、有部位保底 2 小時、空手保底 6 小時、問不到重問
  ⑤ broker.can_enter(query=False)：⛔ 不叫券商；query=True 照舊對帳
  ⑥ live_panel._eq_due：第一次、檢查點各一次（平倉後不另外問）
  ⑦ live_panel.reconnect_gap：前 3 次 60 秒、之後拉長到 600；有部位一律 60
  ⑧ 模擬一整天（日盤一筆＋夜盤一筆）：背景對帳＋帳戶餘額加起來的次數
跑法：.venv\\Scripts\\python.exe -X utf8 tools\\shioaji\\test_qbudget.py
"""
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.stdout.reconfigure(encoding="utf-8")

import testkit  # noqa: E402
testkit.sandbox()
import qbudget as Q      # noqa: E402
import broker            # noqa: E402
import live_panel as LP  # noqa: E402

FAIL = []
TMP = Path(tempfile.mkdtemp(prefix="qb_"))
Q.DIR = TMP
PUSHED = []
Q.PUSH = lambda t, b: PUSHED.append(t)


def chk(name, got, want):
    ok = got == want
    print(("  OK   " if ok else "  FAIL ") + name + ("" if ok else "  (得到 %r，期待 %r)" % (got, want)))
    if not ok:
        FAIL.append(name)


def reset_q():
    Q._S.update({"month": None, "days": {}, "by": {}, "local": 0, "warned": [], "dirty": False, "saved": 0.0})
    for f in TMP.glob("*"):
        f.unlink()
    Q._KB.clear()
    PUSHED.clear()


class Kb(dict):
    @property
    def ts(self):
        return self.get("ts", [])


class FakeQuote:
    def __init__(self):
        self.n = 0

    def subscribe(self, *a, **k):
        self.n += 1


class FakeApi:
    def __init__(self):
        self.calls = []
        self.quote = FakeQuote()
        self.Contracts = "C"
        self.kb_ret = Kb(ts=[1, 2])

    def list_positions(self, *a):
        self.calls.append("list_positions")
        return []

    def margin(self, *a):
        self.calls.append("margin")
        return {"x": 1}

    def list_trades(self):
        self.calls.append("list_trades")
        return []

    def kbars(self, contract, start=None, end=None):
        self.calls.append("kbars")
        return self.kb_ret

    def set_order_callback(self, f):
        self.cb = f


print("① Counted 代理")
reset_q()
raw = FakeApi()
api = Q.install(raw)
chk("回傳代理", isinstance(api, Q.Counted), True)
chk("再包一次還是同一個", Q.install(api) is api, True)
api.list_positions()
api.margin()
api.list_trades()
api.quote.subscribe("TMF")
chk("屬性原樣轉", api.Contracts, "C")
api.set_order_callback(lambda *a: None)
chk("set_order_callback 轉到原物件", callable(getattr(raw, "cb", None)), True)
v = Q.view()
chk("總數＝3（list_trades 不算）", v["total"], 3)
chk("local＝1", v["local"], 1)
chk("quote.subscribe 有記", v["by_method"].get("quote.subscribe"), 1)
chk("原方法真的被叫到", raw.calls, ["list_positions", "margin", "list_trades"])
chk("呼叫者記得出來", any(k.startswith("list_positions|test_qbudget") for k, _ in v["top"]), True)

print("② K 棒快取")
reset_q()
raw = FakeApi()
api = Q.install(raw)
today = datetime.now().strftime("%Y-%m-%d")
api.kbars("TMFR1", start="2026-11-20", end="2026-11-20")
api.kbars("TMFR1", start="2026-11-20", end="2026-11-20")
chk("過去的日子第二次不問", raw.calls.count("kbars"), 1)
api.kbars("TMFR1", start=today, end=today)
api.kbars("TMFR1", start=today, end=today)
chk("今天到期前重用", raw.calls.count("kbars"), 2)
with Q.fresh():
    api.kbars("TMFR1", start=today, end=today)
chk("fresh() 一定重問", raw.calls.count("kbars"), 3)
k = ("TMFR1", today, today, ())
Q._KB[k] = (time.time() - 1, Q._KB[k][1], time.time())
api.kbars("TMFR1", start=today, end=today)
chk("今天過了到期時間重問", raw.calls.count("kbars"), 4)
raw.kb_ret = Kb(ts=[])
api.kbars("TMFR1", start="2026-11-21", end="2026-11-21")
chk("過去但空的 ⇒ 不永久記", Q._KB[("TMFR1", "2026-11-21", "2026-11-21", ())][0] is not None, True)
chk("kbars 記數＝真的問的次數", (Q.view()["by_method"].get("kbars"), raw.calls.count("kbars")), (5, 5))

print("③ 預算線、推播、落地")
reset_q()
d0 = datetime(2026, 11, 27, 10, 0)
for i in range(Q.DAY_SOFT - 1):
    Q.note("margin", d0, who="t")
chk("今天 89 次 ⇒ 還可以", Q.optional_ok(d0), True)
Q.note("margin", d0, who="t")
chk("今天 90 次 ⇒ 可有可無的停", Q.optional_ok(d0), False)
chk("隔天重新算", Q.optional_ok(d0 + timedelta(days=1)), True)
reset_q()
for i in range(Q.MONTH_SOFT):
    Q.note("margin", d0 - timedelta(days=i % 20), who="t")
chk("本月 1,200 次 ⇒ 整個月停", Q.optional_ok(datetime(2026, 11, 28, 9, 0)), False)
chk("推過 600、900、1200 各一次", len(PUSHED), 3)
Q.note("margin", d0, who="t")
chk("同一條不重推", len(PUSHED), 3)
Q.save()
tot = Q.month_total(d0)
Q._S["month"] = None
chk("落地檔讀回來接著記", Q.month_total(d0), tot)
chk("換月重新算", Q.month_total(datetime(2026, 12, 1, 9, 0)), 0)
reset_q()

print("④ broker._recon_due")
broker._LAST_RECONCILE.update(at=0.0, unknown=False)
broker._RECON_KICK.update(due=[], why=None, cp=set())
broker._state["position"] = None
t0 = datetime(2026, 11, 27, 10, 0).timestamp()
chk("第一次一定問", broker._recon_due(t0), "first")
broker._LAST_RECONCILE["at"] = t0
chk("空手、剛問過 ⇒ 不問", broker._recon_due(t0 + 60), None)
chk("空手 5 小時 ⇒ 不問（預設沒有檢查點）", broker._recon_due(t0 + 5 * 3600), None)
chk("空手 6 小時 ⇒ 保底問", broker._recon_due(t0 + 6 * 3600 + 1), "flat")
broker.recon_kick("送單", now=t0)
chk("被提醒：4 秒不問", broker._recon_due(t0 + 4), None)
r = broker._recon_due(t0 + 5.5)
chk("被提醒：5 秒後問", (r or "").startswith("kick"), True)
broker._LAST_RECONCILE["at"] = t0 + 5.5
chk("被提醒：還沒到 30 秒不問", broker._recon_due(t0 + 25), None)
r = broker._recon_due(t0 + 30.5)
chk("被提醒：30 秒後再問", (r or "").startswith("kick"), True)
broker._LAST_RECONCILE["at"] = t0 + 30.5
chk("被提醒問完 ⇒ 不再問", broker._recon_due(t0 + 120), None)
broker.recon_kick("a", now=t0 + 200)
broker.recon_kick("b", now=t0 + 201)
broker._LAST_RECONCILE["at"] = t0 + 199
chk("連著兩個提醒：合併成最後一個之後 5 秒（205.5 還不問）", broker._recon_due(t0 + 205.5), None)
chk("連著兩個提醒：206 問", (broker._recon_due(t0 + 206.1) or "").startswith("kick"), True)
broker._LAST_RECONCILE["at"] = t0 + 206.1
chk("連著兩個提醒：只剩 231 那一次", [round(x - t0) for x in broker._RECON_KICK["due"]], [231])
broker._RECON_KICK["due"] = []
broker._LAST_RECONCILE["at"] = t0
broker._state["position"] = {"dir": 1, "qty": 1}
chk("有部位 119 分 ⇒ 不問", broker._recon_due(t0 + 119 * 60), None)
chk("有部位 120 分 ⇒ 保底問", broker._recon_due(t0 + 120 * 60), "hold")
broker._LAST_RECONCILE["unknown"] = True
broker._LAST_RECONCILE["ok_once"] = True
chk("有部位、上次問不到：59 秒不問", broker._recon_due(t0 + 59), None)
chk("有部位、上次問不到：60 秒重問", broker._recon_due(t0 + 60), "retry-unknown")
broker._state["position"] = None
chk("空手、上次問不到：9 分不問", broker._recon_due(t0 + 540), None)
chk("空手、上次問不到：10 分重問", broker._recon_due(t0 + 600), "retry-unknown")
broker._LAST_RECONCILE["ok_once"] = False
chk("⭐M1 還沒成功對帳過＋空手＋問不到 ⇒ 60 秒就重問", broker._recon_due(t0 + 60), "retry-unknown")
broker._LAST_RECONCILE["ok_once"] = True
broker._LAST_RECONCILE["unknown"] = False
print("④b 過渡狀態（品管 M3）")
broker._state["position"] = {"dir": 1, "qty": 2, "own": True, "mix_sent_at": t0}
broker._LAST_RECONCILE["at"] = t0
chk("剛送進場單 14 秒 ⇒ 不問", broker._recon_due(t0 + 14), None)
chk("剛送進場單 15 秒 ⇒ 問", broker._recon_due(t0 + 15), "transient")
broker._LAST_RECONCILE["at"] = t0 + 100
chk("晚到腿窗口內（100 秒）119 秒後不問", broker._recon_due(t0 + 219), None)
chk("晚到腿窗口內 120 秒後問", broker._recon_due(t0 + 220), "transient")
broker._LAST_RECONCILE["at"] = t0 + 400
chk("過了 300 秒窗口 ⇒ 回到 2 小時保底", broker._recon_due(t0 + 1000), None)
for k, v in (("tp_pending", {"px": 1}), ("diff_susp", True), ("shrink_pend", True)):
    broker._state["position"] = {"dir": 1, "qty": 2, "own": True, k: v}
    broker._LAST_RECONCILE["at"] = t0
    chk("%s ⇒ 15 秒問一次" % k, broker._recon_due(t0 + 15), "transient")
broker._state["position"] = {"dir": 1, "qty": 0, "own": True}
chk("空殼部位 ⇒ 15 秒問一次", broker._recon_due(t0 + 15), "transient")
broker._state["position"] = None
o_cp = broker.RECON_CHECKPOINTS
broker.RECON_CHECKPOINTS = ("08:44",)
broker._RECON_KICK["cp"] = set()
w = datetime(2026, 11, 27, 8, 44, 10)
broker._LAST_RECONCILE["at"] = datetime(2026, 11, 27, 5, 0).timestamp()
chk("（有設檢查點時）08:44 問一次", broker._recon_due(w.timestamp(), w), "checkpoint 08:44")
broker._LAST_RECONCILE["at"] = w.timestamp()
w2 = w + timedelta(minutes=1)
chk("同一個檢查點不重問", broker._recon_due(w2.timestamp(), w2), None)
broker._RECON_KICK["cp"] = set()
w4 = datetime(2026, 11, 27, 9, 30)
broker._LAST_RECONCILE["at"] = datetime(2026, 11, 27, 9, 0).timestamp()
chk("檢查點之後已經問過 ⇒ 不再為檢查點問", broker._recon_due(w4.timestamp(), w4), None)
broker.RECON_CHECKPOINTS = o_cp

print("⑤ can_enter(query=False) 不叫券商")
called = []
orig_rec = broker.reconcile
broker.reconcile = lambda: called.append(1) or None
o_api, o_acc = broker._state.get("api"), broker._state.get("account")
o_ca = dict(broker.CA_OK)
o_live = broker.is_live
o_skew = broker.clock_skew
o_ent = broker.entries_today
broker._state["api"], broker._state["account"] = object(), object()
broker.CA_OK["ok"] = True
broker.clock_skew = lambda: 0.0
broker.entries_today = lambda: 0
broker._state["position"] = None
broker._LAST_RECONCILE["unknown"] = False
chk("空手 ⇒ 可以", broker.can_enter(100.0, True, query=False), (True, None))
chk("沒叫 reconcile", called, [])
broker._state["position"] = {"dir": 1}
chk("有部位 ⇒ 不行", broker.can_enter(100.0, True, query=False)[0], False)
broker._state["position"] = None
broker._LAST_RECONCILE["unknown"] = True
chk("上次對帳問不到 ⇒ 不行", broker.can_enter(100.0, True, query=False), (False, broker.RECON_FAIL_MSG))
broker._LAST_RECONCILE["unknown"] = False
chk("還是沒叫 reconcile", called, [])
broker.can_enter(100.0, True)
chk("預設 query=True ⇒ 照舊對帳", called, [1])
broker.reconcile = orig_rec
broker._state["api"], broker._state["account"] = o_api, o_acc
broker.CA_OK.clear()
broker.CA_OK.update(o_ca)
broker.clock_skew, broker.entries_today = o_skew, o_ent

seen = []
o_ce = broker.can_enter
broker.can_enter = lambda px, live, query=True: seen.append(query) or (True, None)
LP._CAN_ENTER_VIEW.update(at=0.0, key=None, v=None)
LP._can_enter_view(100.0, True, False, now=time.time() + 999)
broker.can_enter = o_ce
chk("畫面那顆燈叫 can_enter 時 query=False", seen, [False])

print("⑤b 平倉前對帳問不到 ⇒ 退避（品管 M2）")
calls = []
o_rec, o_live2 = broker.reconcile, broker.is_live
broker.reconcile = lambda: calls.append(1) or "unknown"
broker.is_live = lambda: True
kicks = []
o_hook = broker.ON_QUERY_FAIL
broker.ON_QUERY_FAIL = lambda why: kicks.append(why)
broker._CLOSE_UNK.update(n=0, at=0.0)
broker._close_fail["at"] = 0.0
broker._state["position"] = {"dir": 1, "qty": 1}
r = broker._close_locked("sl")
chk("第一次問不到 ⇒ 回對帳失敗", r[0], False)
chk("有請面板換線", len(kicks), 1)
for _ in range(20):
    broker._close_locked("sl")
chk("連叫 20 次（0.25 秒那種）只真的問了 1 次", len(calls), 1)
chk("退避中的訊息開頭＝剛剛平倉失敗（不重推手機）", broker._close_locked("sl")[1].startswith("剛剛平倉失敗"), True)
broker._CLOSE_UNK["at"] -= 2.1
broker._close_locked("sl")
chk("2 秒後再問一次", len(calls), 2)
chk("第 2 次之後要等 4 秒", round(broker._close_unknown_wait()), 4)
broker._CLOSE_UNK["n"] = 9
broker._CLOSE_UNK["at"] = time.time()
chk("封頂 10 秒", round(broker._close_unknown_wait()), 10)
broker.reconcile, broker.is_live = o_rec, o_live2
broker.ON_QUERY_FAIL = o_hook
broker._CLOSE_UNK.update(n=0, at=0.0)
broker._state["position"] = None

print("②b K 棒到期規則")
now_dt = datetime(2026, 11, 28, 10, 0)          # 週六
K = lambda e: ("TMFR1", e, e, ())
full, empty = Kb(ts=[1]), Kb(ts=[])
chk("過去的日子、有資料、跨日 10 分鐘後抓的 ⇒ 永久", Q._kb_expire(K("2026-11-27"), full, now_dt), None)
chk("過去的日子、午夜剛過抓的 ⇒ 5 分鐘", Q._kb_expire(K("2026-11-27"), full, datetime(2026, 11, 28, 0, 1)),
    datetime(2026, 11, 28, 0, 6).timestamp())
chk("未來的日子（週末指到週一）⇒ 記到週一", Q._kb_expire(K("2026-11-30"), empty, now_dt),
    datetime(2026, 11, 30).timestamp())
chk("今天有資料、盤中 10:00 ⇒ 記到 13:45", Q._kb_expire(K("2026-11-28"), full, now_dt),
    datetime(2026, 11, 28, 13, 45).timestamp())
chk("⭐N1 今天有資料、20:00 ⇒ 記到今晚午夜（不撐過午夜）", Q._kb_expire(K("2026-11-28"), full, datetime(2026, 11, 28, 20, 0)),
    datetime(2026, 11, 29, 0, 0).timestamp())
chk("今天有資料、00:30 ⇒ 記到 05:00", Q._kb_expire(K("2026-11-29"), full, datetime(2026, 11, 29, 0, 30)),
    datetime(2026, 11, 29, 5, 0).timestamp())
chk("今天空的、開盤前 08:44 ⇒ 記到 08:45", Q._kb_expire(K("2026-11-30"), empty, datetime(2026, 11, 30, 8, 44)),
    datetime(2026, 11, 30, 8, 45).timestamp())
chk("今天空的、週一 03:00（夜盤時段）⇒ 5 分鐘", Q._kb_expire(K("2026-11-30"), empty, datetime(2026, 11, 30, 3, 0)),
    datetime(2026, 11, 30, 3, 5).timestamp())
chk("今天空的、盤中 10:00 ⇒ 5 分鐘", Q._kb_expire(K("2026-11-28"), empty, now_dt), now_dt.timestamp() + 300)
Q._KB[K(datetime.now().strftime("%Y-%m-%d"))] = (None, full, 0)
Q._KB[K("2026-11-20")] = (None, full, 0)
Q.kbars_reset()
chk("重連後今天的快取作廢、過去的留著", sorted(k[1] for k in Q._KB), ["2026-11-20"])
Q._KB.clear()
raw2 = FakeApi()
api2 = Q.install(raw2)
api2.kbars("TMFR1", start=today, end=today)
with Q.max_age(900):
    api2.kbars("TMFR1", start=today, end=today)
chk("⭐N2 max_age 內：剛抓的照用", raw2.calls.count("kbars"), 1)
kk = ("TMFR1", today, today, ())
Q._KB[kk] = (Q._KB[kk][0], Q._KB[kk][1], time.time() - 901)
with Q.max_age(900):
    api2.kbars("TMFR1", start=today, end=today)
chk("⭐N2 max_age：超過 900 秒就重問", raw2.calls.count("kbars"), 2)
Q._KB[kk] = (Q._KB[kk][0], Q._KB[kk][1], time.time() - 901)
api2.kbars("TMFR1", start=today, end=today)
chk("沒包 max_age（K 線圖那條）照到期時間用快取", raw2.calls.count("kbars"), 2)
Q._KB.clear()
print("④c tp_pending 但已經在平倉 ⇒ 不算過渡")
broker._state["position"] = {"dir": 1, "qty": 2, "own": True, "tp_pending": {"px": 1}, "close_reason": "sl"}
broker._LAST_RECONCILE.update(at=t0, unknown=False)
chk("平倉過的 tp_pending 不再 15 秒一次", broker._transient_gap(t0 + 20), None)
broker._state["position"] = None
print("⑦b 查詢線壞掉的重連退避")
LP.QUERY.update(fail=5, since=1000.0, reconn_at=2000.0, kick=None, streak=0)
chk("第一次：120 秒後可以重連", (LP.query_need_reconnect(2119) is None, LP.query_need_reconnect(2121) is not None), (True, True))
LP.QUERY["streak"] = 2
chk("連續重連 2 次還沒好 ⇒ 480 秒", (LP.query_need_reconnect(2479) is None, LP.query_need_reconnect(2481) is not None), (True, True))
LP.QUERY["kick"] = "送單前"
chk("送單前問不到照舊 120 秒", LP.query_need_reconnect(2121) is not None, True)
LP._query_note(True, 1.0)
chk("查詢成功一次 ⇒ 歸零", LP.QUERY["streak"], 0)
LP.QUERY.update(fail=0, since=None, kick=None, streak=0, reconn_at=0.0)

print("⑥ live_panel._eq_due")
LP.EQ_DUE.update(first=True, had_pos=False, close_at=None, done=set(), last=None)
o_nm = LP.night_moments
LP.night_moments = lambda now: (None, datetime.combine(now.date(), datetime.min.time()) + timedelta(hours=22, minutes=40))
n0 = datetime(2026, 11, 27, 7, 0)
chk("第一次問", LP._eq_due(n0, has_pos=False), True)
chk("之後不問", LP._eq_due(n0 + timedelta(minutes=5), has_pos=False), False)
chk("08:40 檢查點", LP._eq_due(datetime(2026, 11, 27, 8, 40, 20), has_pos=False), True)
chk("08:41 不重問", LP._eq_due(datetime(2026, 11, 27, 8, 41), has_pos=False), False)
chk("有部位中不問", LP._eq_due(datetime(2026, 11, 27, 9, 10), has_pos=True), False)
chk("剛平掉當下不問", LP._eq_due(datetime(2026, 11, 27, 13, 43, 40), has_pos=False), False)
chk("平掉後不另外問（14:05 會問）", LP._eq_due(datetime(2026, 11, 27, 13, 44, 45), has_pos=False), False)
chk("14:05 檢查點", LP._eq_due(datetime(2026, 11, 27, 14, 5, 10), has_pos=False), True)
chk("夜盤送單前 6 分（22:34）", LP._eq_due(datetime(2026, 11, 27, 22, 34, 5), has_pos=False), True)
chk("週日不問", LP._eq_checkpoints(datetime(2026, 11, 29, 9, 0)), [])
chk("週六只問 05:06", LP._eq_checkpoints(datetime(2026, 11, 28, 9, 0)), ["05:06"])
chk("週一沒有 05:06", "05:06" in LP._eq_checkpoints(datetime(2026, 11, 30, 9, 0)), False)
LP.night_moments = o_nm

print("⑦ reconnect_gap")
chk("第 1 次 60", LP.reconnect_gap(1, False), 60)
chk("第 3 次 120", LP.reconnect_gap(3, False), 120)
chk("第 5 次 480", LP.reconnect_gap(5, False), 480)
chk("第 9 次封頂 600", LP.reconnect_gap(9, False), 600)
chk("有部位一律 60", LP.reconnect_gap(9, True), 60)
chk("休市、剛斷 ⇒ 300", LP.offhours_gap(0, datetime(2026, 11, 28, 10, 0)), 300)
chk("休市、連續失敗、離開盤遠 ⇒ 1 小時", LP.offhours_gap(5, datetime(2026, 11, 28, 10, 0)), 3600)
chk("開盤前 30 分 ⇒ 不拉到 1 小時", LP.offhours_gap(5, datetime(2026, 11, 27, 8, 20)), 480)

print("⑧ 模擬一整天（日盤 09:03～13:43、夜盤 22:40～隔天 04:58 各一筆）")
broker._LAST_RECONCILE.update(at=0.0, unknown=False)
broker._RECON_KICK.update(due=[], why=None, cp=set())
LP.EQ_DUE.update(first=True, had_pos=False, close_at=None, done=set(), last=None)
LP.night_moments = lambda now: (None, datetime.combine(now.date(), datetime.min.time()) + timedelta(hours=22, minutes=40))
start = datetime(2026, 11, 27, 5, 30)
events = {datetime(2026, 11, 27, 9, 3, 30): ("in", 1), datetime(2026, 11, 27, 13, 43, 30): ("out", 1),
          datetime(2026, 11, 27, 22, 40): ("in", 1), datetime(2026, 11, 28, 4, 58): ("out", 1)}
n_rec = n_eq = 0
t = start
while t < start + timedelta(days=1):
    e = events.get(t)
    if e:
        broker._state["position"] = {"dir": 1} if e[0] == "in" else None
        broker.recon_kick("送單", now=t.timestamp())
    if broker._recon_due(t.timestamp(), t):
        broker._LAST_RECONCILE["at"] = t.timestamp()
        n_rec += 1
    if t.second == 0 and t.minute % 1 == 0 and t.microsecond == 0 and int(t.timestamp()) % 30 == 0:
        if LP._eq_due(t, has_pos=broker._state["position"] is not None):
            n_eq += 1
    t += timedelta(seconds=0.5)
LP.night_moments = o_nm
broker._state["position"] = None
print("    背景對帳 %d 次、帳戶餘額 %d 次（舊版：對帳每 3 秒一次 ≈ 28,800 次、餘額約 400 次）" % (n_rec, n_eq))
chk("背景對帳一天 ≤ 25 次（一口微台，沒有過渡狀態）", n_rec <= 25, True)
chk("帳戶餘額一天 ≤ 5 次", n_eq <= 5, True)

shutil.rmtree(TMP, ignore_errors=True)
print()
print("全部通過" if not FAIL else "有 %d 項沒過：%s" % (len(FAIL), "、".join(FAIL)))
sys.exit(1 if FAIL else 0)
