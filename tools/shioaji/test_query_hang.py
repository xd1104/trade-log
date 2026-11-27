# -*- coding: utf-8 -*-
"""
⭐ 2026-11-23 券商查詢通道卡住（那晚 22:00 起 ReceiveClosed、查部位每次逾時 30 秒）。純離線、不連永豐、不送單。

  ① 停損迴圈／畫面不再自己問券商：real_state 一次都不叫 reconcile_tick／can_enter（⛔ 問券商就會被卡 30 秒）
  ② 券商卡 3 秒時，real_state 照樣立刻回來（量時間）
  ③ 查詢健康：連續 3 次問不到＋壞 30 秒 ⇒ 要重連；問到一次就歸零；120 秒內不重連第二次
  ④ 送單前問不到 ⇒ broker.ON_QUERY_FAIL 被叫 ⇒ 面板馬上要重連（不等 ③）
  ⑤ can_enter_retry：等到 150 秒；訊號不成立就不送；第二次問到就放行（那晚的情況）
  ⑥ 畫面那顆燈：部位有無變了、背景還沒問到 ⇒ 「正在跟券商確認…」（⛔ 不拿舊答案）
  ⑦ 三條自動下單都把 valid 傳進去；夜盤／日盤「送不出去」會推手機
跑法：.venv\\Scripts\\python.exe -X utf8 tools\\shioaji\\test_query_hang.py
"""
import sys
import time
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


def reset_query():
    LP.QUERY.update(fail=0, since=None, last_ok=None, last_s=None, last_err=None, kick=None,
                    reconn_at=0.0, reconn_n=0, view=None, view_key=None, args=None)


print("=== ① ② real_state 不問券商、不被卡住 ===")
calls = {"rt": 0, "ce": 0}
o_rt, o_ce, o_ens = broker.reconcile_tick, broker.can_enter, LP._query_bg_ensure


def slow_rt():
    calls["rt"] += 1
    time.sleep(3)
    return "unknown"


def slow_ce(p, q):
    calls["ce"] += 1
    time.sleep(3)
    return False, broker.RECON_FAIL_MSG


broker.reconcile_tick, broker.can_enter = slow_rt, slow_ce
LP._query_bg_ensure = lambda: None            # ⛔ 這段不開背景執行緒（只量主迴圈那一邊）
try:
    reset_query()
    t0 = time.time()
    snap = LP.real_state(45000.0, "live", 0)
    took = time.time() - t0
    chk("real_state 沒有叫 reconcile_tick", calls["rt"], 0)
    chk("real_state 沒有叫 can_enter", calls["ce"], 0)
    chk("券商卡 3 秒，real_state 照樣 0.5 秒內回來", took < 0.5, True)
    chk("背景還沒問到 ⇒ 燈號寫「確認中」", (snap.get("can_enter"), snap.get("why")), (False, "正在跟券商確認…"))
    chk("快照帶查詢健康", isinstance(snap.get("query"), dict), True)
finally:
    broker.reconcile_tick, broker.can_enter, LP._query_bg_ensure = o_rt, o_ce, o_ens

src = (HERE / "live_panel.py").read_text(encoding="utf-8")
body = src[src.index("def real_state("):src.index("def update_state(")]
chk("real_state 原始碼裡沒有 reconcile_tick( 呼叫", "broker.reconcile_tick(" in body, False)
us = src[src.index("def update_state("):]
us = us[:us.index("with state_lock:")]
chk("update_state 在拿 state_lock 之前就算好 real_state", "real_state(" in us, True)

print("\n=== ③ 查詢健康 ⇒ 重連 ===")
reset_query()
now = 10000.0
LP._query_note(False, 30.0, "逾時", now=now)
LP._query_note(False, 30.0, "逾時", now=now + 30)
chk("問不到 2 次 ⇒ 還不重連", LP.query_need_reconnect(now + 31), None)
LP._query_note(False, 30.0, "逾時", now=now + 60)
w = LP.query_need_reconnect(now + 61)
chk("連續 3 次、壞 60 秒 ⇒ 要重連", bool(w) and "連續 3 次" in w, True)
LP._query_note(True, 0.2, now=now + 62)
chk("問到一次 ⇒ 歸零、不重連", (LP.QUERY["fail"], LP.query_need_reconnect(now + 63)), (0, None))
LP._query_note(True, 20.0, now=now + 64)
chk("有回答但花 20 秒 ⇒ 也算壞（停損會被拖住那麼久）", LP.QUERY["fail"], 1)
reset_query()
for i in range(3):
    LP._query_note(False, 30.0, "逾時", now=now + i * 30)
LP.query_reconnected(now=now + 61)
for i in range(3):
    LP._query_note(False, 30.0, "逾時", now=now + 62 + i * 30)
chk("剛重連完 120 秒內不重連第二次", LP.query_need_reconnect(now + 150), None)
# ⭐ 2026-11-27 查詢預算：第一次重連之後還壞 ⇒ 第二次間隔拉長成 240 秒（送單前問不到那條照舊 120 秒）
chk("重連一次還壞：240 秒內不重連", LP.query_need_reconnect(now + 290), None)
chk("過了 240 秒還壞 ⇒ 再重連", bool(LP.query_need_reconnect(now + 310)), True)

print("\n=== ④ 送單前問不到 ⇒ 馬上要重連 ===")
reset_query()
LP._query_bg_ensure.__wrapped__ if hasattr(LP._query_bg_ensure, "__wrapped__") else None
broker.ON_QUERY_FAIL = LP.query_kick
broker._kick_reconnect("送單前問不到券商部位（第 1 次）")
chk("kick 之後主迴圈下一圈就重連", bool(LP.query_need_reconnect()), True)
broker.ON_QUERY_FAIL = lambda w: 1 / 0
broker._kick_reconnect("x")
chk("ON_QUERY_FAIL 自己壞掉 ⇒ 不丟例外", True, True)
broker.ON_QUERY_FAIL = None


class Clock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def retry(answers, valid=None, refresh=None):
    it = iter(answers)
    kicks = []
    o_ce, o_log, o_hook = broker.can_enter, broker._log, broker.ON_QUERY_FAIL
    broker.can_enter = lambda p, q: next(it)
    broker._log = lambda k, p: None
    broker.ON_QUERY_FAIL = kicks.append
    clk = Clock()
    try:
        out = broker.can_enter_retry(40100.0, True, refresh=refresh, sleep=clk.sleep, clock=clk.now, valid=valid)
    finally:
        broker.can_enter, broker._log, broker.ON_QUERY_FAIL = o_ce, o_log, o_hook
    return out, clk.t - 1000.0, kicks


print("\n=== ⑤ can_enter_retry ===")
BAD = (False, broker.RECON_FAIL_MSG)
chk("等的上限是 150 秒", broker.ENTER_RETRY_S, 150.0)
chk("送單裡面那次查部位維持 20 秒（busy 時不能重連）", broker.ENTRY_BASE_S, 20.0)
out, took, kicks = retry([BAD, (True, None)])
chk("第二次問到 ⇒ 放行", out[:3], (True, None, 2))
chk("第一次問不到就要求重連", len(kicks), 1)
out, took, kicks = retry([BAD] * 100)
chk("一直問不到 ⇒ 150 秒內放棄、照舊不送", (out[0], took <= 150.0, took >= 120.0), (False, True, True))
out, took, kicks = retry([BAD] * 100, valid=lambda q: (q > 40000, "跌回去了"),
                         refresh=lambda: (39900.0, True))
chk("重試時訊號不成立 ⇒ 不送、原因寫訊號", (out[0], "訊號已經不成立" in out[1]), (False, True))
out, took, kicks = retry([BAD, (True, None)], valid=lambda q: 1 / 0, refresh=lambda: (40100.0, True))
chk("驗訊號出錯 ⇒ 當不成立（不猜）", out[0], False)
out, took, kicks = retry([(False, "已經有部位了，先平倉才能再進場")])
chk("其他理由 ⇒ 不重試、不要求重連", (out[2], len(kicks)), (1, 0))

print("\n=== ⑥ 畫面那顆燈 ===")
reset_query()
LP.QUERY.update(view=(True, None), view_key=(45000.0, True, False))
chk("同樣狀態 ⇒ 用背景問好的", LP._can_enter_cached(45010.0, True, False), (True, None))
chk("部位有無變了 ⇒ 確認中", LP._can_enter_cached(45010.0, True, True), (False, "正在跟券商確認…"))
chk("報價變不新鮮 ⇒ 確認中", LP._can_enter_cached(45010.0, False, False), (False, "正在跟券商確認…"))

print("\n=== ⑦ 原始碼 ===")
nf = (HERE / "night_fire.py").read_text(encoding="utf-8")
af = (HERE / "auto_fire.py").read_text(encoding="utf-8")
chk("夜盤跟勢帶 valid", "valid=_still)" in nf, True)
chk("日盤帶 valid", "refresh=_fresh_quote, valid=_still)" in af, True)
chk("夜盤送不出去推手機", 'if why == "cannot":' in nf and "_push(" in nf, True)
chk("日盤送不出去推手機", '_push("日盤訊號成立但沒送出"' in af, True)

reset_query()
print("\n%s" % ("全部通過" if not FAIL else "FAIL %d 項：%s" % (len(FAIL), "、".join(FAIL))))
sys.exit(1 if FAIL else 0)
