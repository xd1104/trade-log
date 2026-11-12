# -*- coding: utf-8 -*-
"""
送單前「問不到券商部位」重試（2026-11-12，Benson 同意）。純離線、不連永豐、不送單。

那晚 22:40 夜盤跟勢訊號 +664 點（門檻 177），送單前問永豐部位剛好逾時，只問一次就放棄 ⇒ 整筆漏掉。
  ① 對帳失敗 ⇒ 會再問；第二次問到空手 ⇒ 放行，回傳問了幾次
  ② 一直問不到 ⇒ 20 秒內問完就放棄，⛔ 照舊不送（原因寫「問了幾次」）
  ③ 其他擋下理由（已有部位、沒報價…）⇒ ⛔ 不重試，一次就擋
  ④ 重試後用 refresh() 的新報價；新報價不新鮮 ⇒ 擋「沒有即時報價」
  ⑤ 夜盤跟勢、夜盤台積電、日盤自動下單三條都改走 can_enter_retry；手動按鈕與畫面燈號 ⛔ 不走（不能卡住）
跑法：.venv/Scripts/python.exe -X utf8 tools/shioaji/test_enter_retry.py
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.stdout.reconfigure(encoding="utf-8")

import testkit  # noqa: E402
testkit.sandbox()
import broker  # noqa: E402

FAIL = []


def chk(name, got, want):
    ok = got == want
    print(("  OK   " if ok else "  FAIL ") + name + ("" if ok else "  (得到 %r，期待 %r)" % (got, want)))
    if not ok:
        FAIL.append(name)


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def run(answers, refresh=None, price=45000.0, live=True):
    """answers：每一次 can_enter 要回的東西（依序）；記下每次被叫時的 (price, live)。"""
    seen = []
    it = iter(answers)
    orig = broker.can_enter
    orig_log = broker._log
    broker.can_enter = lambda p, q: (seen.append((p, q)), next(it))[1]
    broker._log = lambda kind, payload: None
    clk = Clock()
    try:
        out = broker.can_enter_retry(price, live, refresh=refresh, sleep=clk.sleep, clock=clk.now)
    finally:
        broker.can_enter = orig
        broker._log = orig_log
    return out, seen, clk


FAILED = (False, broker.RECON_FAIL_MSG)

print("=== ① 問不到 ⇒ 再問；第二次問到 ⇒ 放行 ===")
(ok, why, n, px), seen, clk = run([FAILED, (True, None)])
chk("放行", (ok, why), (True, None))
chk("問了 2 次、中間等 3 秒", (n, clk.slept), (2, [3.0]))

print("\n=== ② 一直問不到 ⇒ 20 秒內問完就放棄、照舊不送 ===")
(ok, why, n, px), seen, clk = run([FAILED] * 50)
chk("不放行", ok, False)
chk("20 秒內：問 7 次（0,3,…,18 秒）", n, 7)
chk("總共等不超過 20 秒", sum(clk.slept) <= broker.ENTER_RETRY_S, True)
chk("原因講出問了幾次", ("問了 7 次都問不到" in why) and why.startswith(broker.RECON_FAIL_MSG), True)

print("\n=== ③ 其他理由 ⇒ 不重試 ===")
for msg in ("已經有部位了，先平倉才能再進場", "現在沒有即時報價，不能用舊價下單", "今天真實進場已經 3 次，達到上限 3 次"):
    (ok, why, n, px), seen, clk = run([(False, msg)])
    chk("「%s」一次就擋" % msg[:10], (ok, why, n, clk.slept), (False, msg, 1, []))

print("\n=== ④ 重試後用新報價 ===")
(ok, why, n, px), seen, clk = run([FAILED, (True, None)], refresh=lambda: (45123.0, True))
chk("第二次問的是新報價", seen, [(45000.0, True), (45123.0, True)])
chk("回傳新報價（拿去送單、記帳）", px, 45123.0)
(ok, why, n, px), seen, clk = run([FAILED, (False, "現在沒有即時報價，不能用舊價下單")], refresh=lambda: (None, False))
chk("新報價不新鮮 ⇒ 第二次帶 live=False 進去（can_enter 擋）", seen[1], (None, False))
chk("不放行", ok, False)

print("\n=== ⑤ 三條自動下單都走重試；手動按鈕與燈號不走 ===")
src = {n: (HERE / n).read_text(encoding="utf-8") for n in ("night_fire.py", "auto_fire.py", "live_panel.py")}
chk("夜盤（跟勢＋台積電）兩處都用 can_enter_retry、不再直接叫 can_enter",
    (src["night_fire.py"].count("broker.can_enter_retry("), src["night_fire.py"].count("broker.can_enter(")), (2, 0))
chk("日盤自動下單用 can_enter_retry、不再直接叫 can_enter",
    (src["auto_fire.py"].count("broker.can_enter_retry("), src["auto_fire.py"].count("broker.can_enter(px")), (1, 0))
chk("面板（手動按鈕、燈號）⛔ 不用會睡覺的那支", src["live_panel.py"].count("can_enter_retry"), 0)
chk("日盤重試後重拿報價：面板有接 auto_fire._QUOTE", 'auto_fire._QUOTE["fn"] = _nf_quote' in src["live_panel.py"], True)
chk("日盤重試帶 refresh（⛔ 不沿用 09:03:30 的「新鮮」）", "can_enter_retry(px, fresh, refresh=_fresh_quote)" in src["auto_fire.py"], True)
import auto_fire as AF  # noqa: E402
_o = AF._QUOTE["fn"], AF._CFG["gap_s"]
try:
    AF._QUOTE["fn"] = None
    chk("沒接報價 ⇒ 當成沒有即時報價（不猜）", AF._fresh_quote(), (None, False))
    AF._CFG["gap_s"] = 5.0
    AF._QUOTE["fn"] = lambda: (45100.0, 1.2)
    chk("報價 1.2 秒前 ⇒ 新鮮", AF._fresh_quote(), (45100.0, True))
    AF._QUOTE["fn"] = lambda: (45100.0, 9.0)
    chk("報價 9 秒前 ⇒ 不新鮮", AF._fresh_quote(), (45100.0, False))
finally:
    AF._QUOTE["fn"], AF._CFG["gap_s"] = _o

print("\n=== ⑥ 多口送單前那一次查部位（_entry_base）也撐 20 秒 ===")


class FakeTime:
    def __init__(self):
        self.t = 5000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


def base_run(fail_n):
    calls = {"n": 0}

    def legs():
        calls["n"] += 1
        return "unknown" if calls["n"] <= fail_n else {}
    o_legs, o_time = broker._broker_legs, broker.time
    broker._broker_legs, broker.time = legs, FakeTime()
    try:
        out = broker._entry_base("long")
        took = broker.time.t - 5000.0
    finally:
        broker._broker_legs, broker.time = o_legs, o_time
    return out, calls["n"], took


out, n, took = base_run(5)
chk("問不到 5 次（約 7 秒）後問到空手 ⇒ 照常送（不是「一張都沒送」）", (out, n), ({}, 6))
out, n, took = base_run(999)
chk("一直問不到 ⇒ 20 秒內放棄、照舊不送", (out, took <= broker.ENTER_RETRY_S, n > broker.ENTRY_BASE_TRIES), ({"?": 1}, True, True))

print("\n=== ⑦ 多方聯軍：確定一張都沒送出的候選，不封住其他候選 ===")
D = "2026-11-13"


def fire(c):
    return {"rec": "fire", "stage": "sending", "cand": c, "date": D}


def res(c, ok, **k):
    return dict({"rec": "result", "cand": c, "ok": ok, "date": D}, **k)


chk("快攻確定沒送出（unsent）⇒ 今天還沒送過", AF._sent(D, [fire("fast"), res("fast", False, unsent=True)]), False)
chk("  ⇒ 快攻自己不會再送（_cand_done）", AF._cand_done(D, "fast", [fire("fast"), res("fast", False, unsent=True)]), True)
chk("快攻送出去但結果不明（沒有 unsent）⇒ 照舊算送過", AF._sent(D, [fire("fast"), res("fast", False, maybe_filled=True)]), True)
chk("只有 sending 沒有結果（送到一半當掉）⇒ 照舊算送過（寧可漏、不重送）", AF._sent(D, [fire("fast")]), True)
chk("快攻沒送出、開箱成交了 ⇒ 送過",
    AF._sent(D, [fire("fast"), res("fast", False, unsent=True), fire("orb"), res("orb", True)]), True)
chk("舊方法 A（沒有 cand）的 unsent ⇒ 照舊算送過",
    AF._sent(D, [{"rec": "fire", "date": D}, {"rec": "result", "ok": False, "unsent": True, "date": D}]), True)

print("\n=== ⑧ 送單計數：enter 前後沒動 ⇒ 確定沒送出 ===")
_src_b = (HERE / "broker.py").read_text(encoding="utf-8")
chk("place_order 前一行就加計數（叫到一半丟例外也算送過）",
    '_ORDER_SEQ["n"] += 1                 # ⛔ 叫之前就算（叫到一半丟例外也可能已經送到券商）\n'
    '        trade = api.place_order(c, order)' in _src_b, True)
chk("日盤、夜盤跟勢都用 order_seq 判斷有沒有送出",
    ("broker.order_seq() == _seq0" in src["auto_fire.py"], "broker.order_seq() == _seq0" in src["night_fire.py"]),
    (True, True))

print("\n=== ⑨ 日盤收盤：問不到券商是暫時的 ⇒ 窗口內再問 ===")


class _FT:
    def __init__(self):
        self.t = 9000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


def eod_run(seq, entry=44000.0):
    it = iter(seq)
    rows = []
    calls = {"n": 0}

    def ac(ent):
        calls["n"] += 1
        return next(it)
    saved = (AF._eod_settled, AF._auto_entry, AF._already_closed, AF._eod_row, AF.time)
    AF._eod_settled = lambda d: False
    AF._auto_entry = lambda d: ({"dir": "long", "entry": entry, "entry_time": "09:03:31"}, None)
    AF._already_closed = ac
    AF._eod_row = lambda d, why, extra, msg=None: rows.append(why)
    AF.time = _FT()
    try:
        AF._eod("2026-11-13", 0, AF.time.t, "13:43:30")
    finally:
        (AF._eod_settled, AF._auto_entry, AF._already_closed, AF._eod_row, AF.time) = saved
    return rows, calls["n"]


chk("第一次問不到、第二次問到「早就平掉」⇒ 照實記 eod_done_elsewhere",
    eod_run([("unknown", "x"), ("closed", "y")]), (["eod_done_elsewhere"], 2))
rows, n = eod_run([("unknown", "x")] * 50)
chk("一直問不到 ⇒ 窗口內問完才記 eod_cant_tell（不是問一次）", (rows, n > 5), (["eod_cant_tell"], True))
chk("沒有進場價（確定性的）⇒ 不重試", eod_run([("unknown", "沒有進場價")] * 5, entry=None), (["eod_cant_tell"], 1))

print("\n=== ⑩ 第 4 批：還沒連上也重試、時鐘差太多不送 ===")
(ok, why, n, px), seen, clk = run([(False, broker.NOT_CONNECTED_MSG), (True, None)])
chk("「還沒連上永豐」（剛重連）⇒ 也會重試、問到就放行", (ok, n), (True, 2))
(ok, why, n, px), seen, clk = run([(False, broker.NO_ACCOUNT_MSG), (True, None)])
chk("「找不到期貨帳號」⇒ 也會重試", (ok, n), (True, 2))
_ck = dict(broker.CLOCK_SKEW)
try:
    broker.CLOCK_SKEW.update(s=45.0, at=__import__("time").time())
    chk("時鐘差 45 秒 ⇒ clock_skew 回 45", broker.clock_skew(), 45.0)
    _src_ce = (HERE / "broker.py").read_text(encoding="utf-8")
    chk("can_enter 有擋「時鐘差太多」那一道", "本機時鐘跟交易所差" in _src_ce, True)
    broker.CLOCK_SKEW.update(s=45.0, at=__import__("time").time() - 600)
    chk("時鐘估計 10 分鐘沒更新（休市）⇒ 不拿舊的判斷", broker.clock_skew(), None)
finally:
    broker.CLOCK_SKEW.clear()
    broker.CLOCK_SKEW.update(_ck)

print("\n=== ⑪ 第 4 批：模擬資料洞不定論、風控日盤隔天平也對得到 ===")
import risk_cap as RC  # noqa: E402
_e = {"sess": "day", "d": "2026-11-13", "entry_time": "09:03:31", "entry": 44000.0}
_t = [{"_file": "2026-11-16", "entry_time": "09:03:31", "entry": 44000.0, "points": -100}]
chk("日盤 11-13 進場、11-16 才平（13:43:30 沒平掉）⇒ 對得到", RC._match(_e, _t, set()) is not None, True)
_t2 = [{"_file": "2026-11-16", "entry_time": "09:15:02", "entry": 44000.0, "points": -100}]
chk("  進場時間不同 ⇒ 不會配錯", RC._match(_e, _t2, set()), None)
_sl = (HERE / "sim_lanes.py").read_text(encoding="utf-8")
chk("模擬：進場時刻前沒有資料 ⇒ pending('hole')，不記不做", '_pending("hole"' in _sl, True)

print("\n總結：" +("全部通過 ✅" if not FAIL else "%d 項沒過 ❌：%s" % (len(FAIL), "；".join(FAIL))))
sys.exit(1 if FAIL else 0)
