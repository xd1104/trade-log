# -*- coding: utf-8 -*-
"""
【策略實驗室】離線測試。⛔ 不連永豐（每日抓取一律用假的 api 物件）、⛔ 不碰 8770。

  ① 用 tick_hist/ 的**真資料**驗兩組對照數字（逐筆口徑＝ hypotheses.run_bracket）
  ② 參數白名單（函式層＋真的 HTTP handler 回 400）
  ③ 429 互斥（同時只准一個查詢在算）
  ④ 每日抓取在「有部位／13:50 前／今天已存在／usage>85%」都不抓（＋週末、15:00 後、重試間隔、正控組）
  ⑤ 抓取丟例外不會影響呼叫端（fetch_today 與 fetch_loop 各驗一次）
  ⑥ 新分頁 HTML／JS 裡沒有 broker、place_order、/api/enter、/api/real/、/api/fire/on，也沒有建議口吻
  ⑦ 全程沒有寫進真的 tick_hist/（收尾比對雜湊）

⛔ 寫檔的測項（④⑤）一律把 strategy_lab.LAB_DIR／MIN1_CSV 導到暫存區，收尾斷言沒有指回真的資料夾。

跑法（在 tools\\shioaji 底下）：  ..\\..\\.venv\\Scripts\\python.exe test_strategy_lab.py
"""
import ast
import hashlib
import json
import pathlib
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import pandas as pd

import strategy_lab as SL
import live_panel as LP

FAIL = 0
REAL_LAB = SL.LAB_DIR
REAL_CSV = SL.MIN1_CSV


def chk(name, got, want):
    global FAIL
    ok = got == want
    FAIL += not ok
    print(("  OK   " if ok else "  FAIL ") + name + ("" if ok else f"  (得到 {got!r}，期待 {want!r})"))


def say(ok, name, extra=""):
    global FAIL
    FAIL += not ok
    print(("  OK   " if ok else "  FAIL ") + name + (f"  {extra}" if extra else ""))


def tree_hash(root):
    h = hashlib.sha256()
    if not root.exists():
        return "missing"
    for f in sorted(root.rglob("*")):
        if f.is_file():
            st = f.stat()
            h.update(f"{f.relative_to(root)}|{st.st_size}|{st.st_mtime_ns}".encode())
    return h.hexdigest()


def q(**kw):
    return SL.parse_params({k: [str(v)] for k, v in kw.items()})


REAL_HASH = tree_hash(REAL_LAB)

# ══ ① 兩組對照數字（真資料）══════════════════════════════════════════
print("\n=== ① 逐筆口徑對照（tick_hist 真資料）===")
say((REAL_LAB / "days.jsonl").exists(), "tick_hist/days.jsonl 在（沒有的話先跑 strategy_lab.py 建）")
t0 = time.perf_counter()
A = SL.run_exclusive(q(t="09:03:00", dir="bar5", tp=130, sl=130, cost=1))
el_a = time.perf_counter() - t0
t0 = time.perf_counter()
B = SL.run_exclusive(q(t="09:03:00", dir="bar5", tp=130, sl=130, cost=1, gap=100))
el_b = time.perf_counter() - t0
S = A["design"]
n_all = S["n"] + A["valid"]["n"]
# ⚠️ 兩組數字是 2024-07-29 ~ 2025-05-29 那 202 天的；之後每天會長新資料 ⇒ 只看那一段
if n_all != 202:
    S = SL.period_stats([r for r in S["series"] if r[0] <= "2025-05-29"])
chk("A 09:03:00 ±130：202 天", S["n"], 202)
chk("  勝率 54.5%", round(S["rate"], 1), 54.5)
say(abs(S["avg"] - 7.55) <= 0.1, "  每筆 +7.55（±0.1）", f"實測 {S['avg']:+.4f}")
chk("  停利／停損／收盤 87／67／48", (S["tp"], S["sl"], S["eod"]), (87, 67, 48))
S2 = B["design"]
if n_all != 202:
    S2 = SL.period_stats([r for r in S2["series"] if r[0] <= "2025-05-29"])
chk("＋gap=100：126 天", S2["n"], 126)
chk("  勝率 59.5%", round(S2["rate"], 1), 59.5)
say(abs(S2["avg"] - 20.39) <= 0.1, "  每筆 +20.39（±0.1）", f"實測 {S2['avg']:+.4f}")
ref = pathlib.Path(r"C:\Users\Administrator\Desktop\claude\tick-research\ticks2y\strategy_lab_ref.json")
if ref.exists():
    R = json.loads(ref.read_text(encoding="utf-8"))
    diff = [d for d, p, w in S["series"] if d in R and (p is None or abs(p - R[d]["pts"]) > 1e-6)]
    chk("  逐日點數跟 tick-research 的逐筆參考檔一天都不差", diff, [])
print(f"  ·    查詢耗時：全部 {el_a:.3f} 秒、gap=100 {el_b:.3f} 秒（{A['n_days']} 天）")
say(el_a < 3.0, "  一次查詢 < 3 秒")
# 其他方向與極端參數跑得完、筆數自洽
for kw in (dict(dir="open"), dict(dir="long"), dict(dir="short", cost=0),
           dict(t="13:00:00", tp=2000, sl=1), dict(t="08:46:00", skip="1,3", noexp=1, nr=300)):
    base = dict(t="09:03:00", dir="bar5", tp=130, sl=130)
    base.update(kw)
    o = SL.run(q(**base))
    for k in ("design", "valid"):
        s = o[k]
        say(s["n"] + s["skipped"] == s["days"] and s["tp"] + s["sl"] + s["eod"] == s["n"]
            and sum(s["skip_by"].values()) == s["skipped"], f"  {kw} {k} 筆數自洽")
o = SL.run(q(t="09:03:00", dir="bar5", tp=130, sl=130, skip="1,3", noexp=1))
bad = [r for r in SL.days() if (r["weekday"] in (0, 2) or r["expiry_week"])
       and any(x[0] == r["date"] and x[1] is not None for x in o["design"]["series"] + o["valid"]["series"])]
chk("  skip=1,3＋noexp=1：週一、週三、結算週一天都沒做", bad, [])


# ══ ①b 口徑的邊角（lab-qa 退件 R2：這五條原本突變打不紅）═══════════════════
print("\n=== ①b 口徑的邊角 ===")
sys.path.insert(0, r"C:\Users\Administrator\Desktop\claude\tick-research\scripts")
import numpy as np
import hypotheses as HY
from datetime import date as _d

# R2-1 cost=0：拿 hypotheses.run_bracket 逐筆當基準（進場用成交價、收盤出場用成交價、不扣手續費）
#   ⇒ 基準那邊把 bid／ask 換成成交價，run_bracket 的收盤出場就自然變成成交價
C0 = SL.run(q(t="09:03:00", dir="bar5", tp=130, sl=130, cost=0))
exp0 = []
for r in SL.days():
    if r["date"] > "2025-05-29":
        continue
    D = SL.load_day(r["date"])
    T, B0 = HY.ms(9, 3, 0), HY.ms(9, 0)
    i0, ipx = HY.first_at(D, B0), HY.last_before(D, T)
    if i0 < 0 or ipx < 0 or D["t"][i0] > T:
        continue
    d = 1 if D["p"][ipx] >= D["p"][i0] else -1
    Dp = {"t": D["t"], "p": D["p"], "bid": D["p"], "ask": D["p"]}
    cut = HY.T1330 if HY.is_expiry(_d.fromisoformat(r["date"])) else HY.T1343_30
    pts, why, _ = HY.run_bracket(Dp, ipx, float(D["p"][ipx]), d, 130, 130, cut)
    exp0.append((r["date"], round(float(pts), 1), why))
ser0 = {x[0]: (x[1], x[2]) for x in C0["design"]["series"] + C0["valid"]["series"]}
diff0 = [(dd, p, ser0.get(dd)) for dd, p, w in exp0
         if ser0.get(dd) is None or ser0[dd][0] is None or abs(ser0[dd][0] - p) > 1e-6 or ser0[dd][1] != w]
chk(f"  cost=0：{len(exp0)} 天逐日點數與出場原因＝基準（hypotheses.run_bracket、成交價進出、不扣費）", diff0[:3], [])
S0 = SL.period_stats([x for x in C0["design"]["series"] if x[0] <= "2025-05-29"])
avg_exp0 = sum(p for _, p, _ in exp0) / len(exp0)
say(len(exp0) == 202 and abs(S0["avg"] - avg_exp0) < 1e-3, "  cost=0：每筆平均＝基準", f"{S0['avg']:+.4f} vs {avg_exp0:+.4f}")

# R2-2 結算週＝第三個週三所在的週一～週五（⚠️ 刻意跟 filters.py 的週一～週三不同）
EXPW = {
    # 2026-09：1 號週二，第三個週三 16 號 ⇒ 14～18
    "2026-09-11": False, "2026-09-13": False, "2026-09-14": True, "2026-09-16": True,
    "2026-09-17": True, "2026-09-18": True, "2026-09-19": False, "2026-09-21": False,
    # 第三個週三剛好 15 號（2025-01-01 週三）⇒ 13～17
    "2025-01-10": False, "2025-01-13": True, "2025-01-15": True, "2025-01-17": True, "2025-01-20": False,
    # 第三個週三剛好 21 號（2026-01-01 週四）⇒ 19～23
    "2026-01-16": False, "2026-01-19": True, "2026-01-21": True, "2026-01-23": True, "2026-01-26": False,
    # 月初第一週跨月：2026-09-28(一)～10-02(五) 不是結算週（10 月的是 10-19～23）
    "2026-09-30": False, "2026-10-01": False, "2026-10-02": False, "2026-10-19": True, "2026-10-23": True,
}
bad_w = [k for k, v in EXPW.items() if SL.expiry_week(_d.fromisoformat(k)) != v]
chk(f"  expiry_week：週一～週五、15／21 號邊界、跨月第一週（{len(EXPW)} 個日子）", bad_w, [])
chk("  is_expiry：只有第三個週三（15、21 號邊界）",
    [SL.is_expiry(_d.fromisoformat(x)) for x in ("2025-01-15", "2026-01-21", "2026-01-14", "2026-01-22", "2026-09-16")],
    [True, True, False, False, True])
_row = {"date": "2026-09-17", "weekday": 3, "open": 100.0, "prev_close": 0.0, "night_range": 500.0,
        "expiry_week": True}
_P = q(t="09:03:00", dir="bar5", tp=130, sl=130, noexp=1)
chk("  _skip_reason：noexp=1 ＋ 結算週的週四 ⇒ expw", SL._skip_reason(_row, _P), "expw")
chk("  _skip_reason：noexp=0 ⇒ 照做", SL._skip_reason(_row, q(t="09:03:00", dir="bar5", tp=130, sl=130)), None)
chk("  _skip_reason：非結算週 ⇒ 照做", SL._skip_reason(dict(_row, expiry_week=False), _P), None)
chk("  _skip_reason：skip=4 跳週四（weekday 3）",
    SL._skip_reason(dict(_row, expiry_week=False), q(t="09:03:00", dir="bar5", tp=130, sl=130, skip="4")), "wd")

# R2-3 少於 30 天不給勝率
def _mk(n):
    return [(f"2025-01-{i:02d}", 10.0 if i % 2 else -5.0, "tp" if i % 2 else "sl") for i in range(1, n + 1)]


chk("  period_stats：29 天 ⇒ 勝率 null", SL.period_stats(_mk(29))["rate"], None)
say(SL.period_stats(_mk(30))["rate"] is not None, "  period_stats：30 天 ⇒ 有勝率", str(SL.period_stats(_mk(30))["rate"]))

# R2-4 跳空門檻：剛好等於門檻算「做」（≥）
_Pg = q(t="09:03:00", dir="bar5", tp=130, sl=130, gap=100)
_base = {"date": "2025-01-06", "weekday": 0, "expiry_week": False, "night_range": None}
chk("  gap=100：|開−昨收| 剛好 100（向上）⇒ 做", SL._skip_reason(dict(_base, open=22100.0, prev_close=22000.0), _Pg), None)
chk("  gap=100：剛好 100（向下）⇒ 做", SL._skip_reason(dict(_base, open=21900.0, prev_close=22000.0), _Pg), None)
chk("  gap=100：99 ⇒ 過濾（gap）", SL._skip_reason(dict(_base, open=22099.0, prev_close=22000.0), _Pg), "gap")
chk("  gap=100：沒有昨收 ⇒ 過濾（gap）", SL._skip_reason(dict(_base, open=22099.0, prev_close=None), _Pg), "gap")
chk("  nr=300：沒有夜盤資料 ⇒ 過濾（night）",
    SL._skip_reason(dict(_base, open=1.0, prev_close=None), q(t="09:03:00", dir="bar5", tp=130, sl=130, nr=300)), "night")

# R2-5 5 分 K「沒有東西可比」：09:05:00～09:08:00 一筆成交都沒有 ⇒ 那天排除（⛔ 不是當成做多）
def _ms(h, m, s=0):
    return (h * 3600 + m * 60 + s) * 1000


Dn = {"t": np.array([_ms(8, 45), _ms(9, 4, 59), _ms(9, 8, 30), _ms(9, 30), _ms(13, 40)]),
      "p": np.array([100.0, 90.0, 95.0, 300.0, 300.0]), "bid": np.array([99.0, 89.0, 94.0, 299.0, 299.0]),
      "ask": np.array([101.0, 91.0, 96.0, 301.0, 301.0])}
_rown = {"date": "2025-01-06"}
chk("  bar5 09:08:00、那根 5 分 K 到進場時刻沒成交 ⇒ (None, nodir)",
    SL.sim_day(_rown, Dn, q(t="09:08:00", dir="bar5", tp=130, sl=130)), (None, "nodir"))
chk("  同一天 dir=long ⇒ 照做（證明上面那條不是整天都做不了）",
    SL.sim_day(_rown, Dn, q(t="09:08:00", dir="long", tp=130, sl=130))[1], "tp")
chk("  09:08:31 進場（那根 K 棒有成交了）⇒ 有方向、不排除",
    SL.sim_day(_rown, Dn, q(t="09:08:31", dir="bar5", tp=130, sl=130))[1] != "nodir", True)

# ══ ② 參數白名單 ════════════════════════════════════════════════════
print("\n=== ② 參數白名單 ===")
good = dict(t="09:03:00", dir="bar5", tp="130", sl="130")
for label, kw in (("t=08:46:00 邊界", dict(t="08:46:00")), ("t=13:00:00 邊界", dict(t="13:00:00")),
                  ("tp=1／sl=2000 邊界", dict(tp="1", sl="2000")), ("skip=1,5", dict(skip="1,5")),
                  ("gap／nr 空白＝不限", dict(gap="", nr="")), ("noexp=1 cost=0", dict(noexp="1", cost="0"))):
    g = dict(good)
    g.update(kw)
    try:
        SL.parse_params({k: [v] for k, v in g.items()})
        say(True, "  合法：" + label)
    except SL.BadParam as e:
        say(False, "  合法：" + label, str(e))
BADS = (("t=08:45:59", dict(t="08:45:59")), ("t=13:00:01", dict(t="13:00:01")), ("t=9:03", dict(t="9:03")),
        ("t=09:60:00", dict(t="09:60:00")), ("dir=A", dict(dir="A")), ("tp=0", dict(tp="0")),
        ("tp=2001", dict(tp="2001")), ("tp=1.5", dict(tp="1.5")), ("tp=-5", dict(tp="-5")),
        ("sl 缺", dict(sl=None)), ("gap=0", dict(gap="0")), ("nr=abc", dict(nr="abc")),
        ("skip=0", dict(skip="0")), ("skip=6", dict(skip="6")), ("skip=1,1", dict(skip="1,1")),
        ("skip=1;2", dict(skip="1;2")), ("noexp=2", dict(noexp="2")), ("cost=yes", dict(cost="yes")),
        ("不認得的參數", dict(x="1")))
for label, kw in BADS:
    g = dict(good)
    g.update(kw)
    g = {k: [v] for k, v in g.items() if v is not None}
    try:
        SL.parse_params(g)
        say(False, "  擋下：" + label)
    except SL.BadParam:
        say(True, "  擋下：" + label)
try:
    SL.parse_params({"t": ["09:03:00", "09:04:00"], "dir": ["bar5"], "tp": ["1"], "sl": ["1"]})
    say(False, "  擋下：參數重複")
except SL.BadParam:
    say(True, "  擋下：參數重複")

# 真的 HTTP handler（產品的 Handler 架在空閒的埠上）
srv = ThreadingHTTPServer(("127.0.0.1", 0), LP.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"


def get(path, headers=None, timeout=30):
    req = urllib.request.Request(BASE + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
    except (TimeoutError, OSError) as e:          # ⛔ 卡住要變紅，不是卡死整支測試
        return "timeout", {"msg": repr(e)[:80]}


chk("  HTTP：合法 ⇒ 200", get("/api/lab/run?t=09:03:00&dir=bar5&tp=130&sl=130")[0], 200)
for p in ("/api/lab/run?t=08:00:00&dir=bar5&tp=130&sl=130", "/api/lab/run?t=09:03:00&dir=bar5&tp=0&sl=130",
          "/api/lab/run?t=09:03:00&dir=bar5&tp=130&sl=130&skip=7", "/api/lab/run?t=09:03:00&dir=up&tp=130&sl=130",
          "/api/lab/run?t=09:03:00&dir=bar5&tp=130&sl=130&evil=1", "/api/lab/run"):
    chk("  HTTP 400：" + p.split("?", 1)[-1], get(p)[0], 400)
st, m = get("/api/lab/meta")
chk("  HTTP：meta ⇒ 200 且有驗證期切點", (st, m.get("valid_from")), (200, "2026-01-01"))
chk("  HTTP：別的網站的分頁叫它 ⇒ 403", get("/api/lab/run?t=09:03:00&dir=bar5&tp=130&sl=130",
                                            {"Sec-Fetch-Site": "cross-site"})[0], 403)

# ══ ③ 429 互斥 ══════════════════════════════════════════════════════
print("\n=== ③ 同時只准一個查詢 ===")
# ⛔ 每一條都有 5 秒上限：鎖被改成「會等待」時要**變紅**，不是整支測試卡死（lab-qa 退件 R2）
SL._RUN_LOCK.acquire()
try:
    box = []

    def _try_run():
        try:
            SL.run_exclusive(q(t="09:03:00", dir="bar5", tp=130, sl=130))
            box.append("ran")
        except SL.Busy:
            box.append("busy")

    _t = threading.Thread(target=_try_run, daemon=True)
    _t.start()
    _t.join(5)
    chk("  鎖被拿著時 run_exclusive 5 秒內丟 Busy（不是等）", box, ["busy"])
    st, body = get("/api/lab/run?t=09:03:00&dir=bar5&tp=130&sl=130", timeout=5)
    chk("  HTTP 429「還在算上一組」", (st, body.get("msg")), (429, "還在算上一組"))
finally:
    SL._RUN_LOCK.release()
# 真的兩條同時打：把 run 拉長，兩個請求只能有一個 200
_real_run = SL.run
SL.run = lambda P: (time.sleep(0.8), {"ok": True})[1]
res = []
th = [threading.Thread(target=lambda: res.append(get("/api/lab/run?t=09:03:00&dir=bar5&tp=130&sl=130", timeout=5)[0]), daemon=True)
      for _ in range(2)]
for t in th:
    t.start()
    time.sleep(0.1)
_dead = time.perf_counter() + 5
for t in th:
    t.join(max(0.1, _dead - time.perf_counter()))
SL.run = _real_run
chk("  兩個請求同時到：一個 200、一個 429", sorted(res), [200, 429])
chk("  算完之後鎖有放開", SL._RUN_LOCK.locked(), False)
srv.shutdown()

# ══ ④ 每日抓取的四道門（假 api，⛔ 不連永豐）════════════════════════════
print("\n=== ④ 每日抓取：該不抓的時候一次都不准問永豐 ===")
TMP = pathlib.Path(tempfile.mkdtemp(prefix="strategy-lab-test-"))
SL.LAB_DIR = TMP / "tick_hist"
SL.MIN1_CSV = TMP / "no_such_1min.csv"


class U:
    def __init__(self, used, lim):
        self.bytes, self.limit_bytes = used, lim


class FakeContracts:
    class Futures:
        TMF = {"TMFR1": "TMFR1"}


class FakeApi:
    Contracts = FakeContracts

    def __init__(self, used=10, lim=100, rows=None, boom=None, usage_boom=False):
        self.used, self.lim, self.rows, self.boom, self.usage_boom = used, lim, rows, boom, usage_boom
        self.ticks_calls, self.usage_calls = 0, 0

    def usage(self, **kw):
        self.usage_calls += 1
        self.ukw = kw
        if self.usage_boom:
            raise RuntimeError("usage 壞了")
        return U(self.used, self.lim)

    def ticks(self, **kw):
        self.ticks_calls += 1
        self.kw = kw
        if self.boom:
            raise self.boom
        return self.rows


def fake_rows(day, last="13:44:59"):
    ts = pd.date_range(f"{day} 08:45:00", f"{day} {last}", periods=400)
    px = [12000 + (i % 40) for i in range(len(ts))]
    return {"ts": [int(x) for x in ts.as_unit("ns").asi8], "close": px, "volume": [1] * len(ts),
            "bid_price": [p - 1 for p in px], "bid_volume": [1] * len(ts),
            "ask_price": [p + 1 for p in px], "ask_volume": [1] * len(ts), "tick_type": [1] * len(ts)}


MON = datetime(2026, 9, 14, 13, 55)          # 週一
DAY = "2026-09-14"
QT = "RangeTime"


def reset():
    SL.FETCH.update(status="idle", msg="", at=None, last_try=None, done_day=None)
    f = SL._ticks_dir() / f"{DAY}.csv.gz"
    if f.exists():
        f.unlink()


def case(label, api, has_pos, now, want_status, want_calls=0):
    reset()
    st = SL.fetch_today(api, has_pos, now=now, qt=QT)
    chk(f"  {label} ⇒ {want_status}", st, want_status)
    chk(f"  {label} ⇒ api.ticks 被叫 {want_calls} 次", api.ticks_calls, want_calls)


api = FakeApi(rows=fake_rows(DAY))
case("有部位", api, lambda: True, MON, "position")
chk("  有部位 ⇒ 連流量都沒問", api.usage_calls, 0)
api = FakeApi(rows=fake_rows(DAY))
case("問部位時丟例外（當成有部位）", api, lambda: 1 / 0, MON, "position")
api = FakeApi(rows=fake_rows(DAY))
case("13:49:59（13:50 前）", api, lambda: False, datetime(2026, 9, 14, 13, 49, 59), "too_early")
api = FakeApi(rows=fake_rows(DAY))
reset()
SL._ticks_dir().mkdir(parents=True, exist_ok=True)
(SL._ticks_dir() / f"{DAY}.csv.gz").write_bytes(b"x")
st = SL.fetch_today(api, lambda: False, now=MON, qt=QT)
chk("  今天已存在 ⇒ exists", st, "exists")
chk("  今天已存在 ⇒ api.ticks 被叫 0 次", api.ticks_calls, 0)
(SL._ticks_dir() / f"{DAY}.csv.gz").unlink()
api = FakeApi(used=86, lim=100, rows=fake_rows(DAY))
case("usage 86%（>85%）", api, lambda: False, MON, "usage_high")
api = FakeApi(used=85, lim=100, rows=fake_rows(DAY))
case("usage 剛好 85%（不超過，要抓）", api, lambda: False, MON, "saved", 1)
api = FakeApi(rows=fake_rows(DAY))
case("週六", api, lambda: False, datetime(2026, 9, 19, 14, 0), "not_trading_day")
api = FakeApi(rows=fake_rows(DAY))
case("15:00:01 之後", api, lambda: False, datetime(2026, 9, 14, 15, 0, 1), "window_closed")
reset()
st = SL.fetch_today(None, lambda: False, now=MON, qt=QT)
chk("  api 是 None ⇒ no_api", st, "no_api")

# 正控組：全部條件都對 ⇒ 真的抓、存檔、補摘要
api = FakeApi(rows=fake_rows(DAY))
case("正控組（條件全對）", api, lambda: False, MON, "saved", 1)
chk("  正控組：api.usage／api.ticks 都帶明確的 timeout",
    (api.ukw.get("timeout"), api.kw.get("timeout")), (SL.USAGE_TIMEOUT_MS, SL.TICKS_TIMEOUT_MS))
say(0 < SL.USAGE_TIMEOUT_MS <= 30000 and 0 < SL.TICKS_TIMEOUT_MS <= 120000, "  timeout 是合理的有限值（毫秒）")
chk("  正控組：用 RangeTime 08:45:00~13:45:00 抓 TMFR1",
    (api.kw.get("contract"), api.kw.get("date"), api.kw.get("time_start"), api.kw.get("time_end")),
    ("TMFR1", DAY, "08:45:00", "13:45:00"))
say((SL._ticks_dir() / f"{DAY}.csv.gz").exists(), "  正控組：csv.gz 存進（暫存的）tick_hist/ticks/")
chk("  正控組：days.jsonl 補上那一天", [r["date"] for r in SL.days()], [DAY])
say((SL._cache_dir() / f"{DAY}.npz").exists(), "  正控組：快取也建好了")
api2 = FakeApi(rows=fake_rows(DAY))
st = SL.fetch_today(api2, lambda: False, now=datetime(2026, 9, 14, 14, 30), qt=QT)
chk("  存好之後同一天再來 ⇒ exists、不再問永豐", (st, api2.ticks_calls), ("exists", 0))

# 不完整（半天）不准存；10 分鐘內不重試、過了才重試
api = FakeApi(rows=fake_rows(DAY, last="12:00:00"))
case("資料只到 12:00（不完整）", api, lambda: False, MON, "incomplete", 1)
say(not (SL._ticks_dir() / f"{DAY}.csv.gz").exists(), "  不完整 ⇒ 不存檔")
st = SL.fetch_today(api, lambda: False, now=datetime(2026, 9, 14, 14, 4), qt=QT)
chk("  9 分鐘後 ⇒ 不重試", api.ticks_calls, 1)
api.rows = fake_rows(DAY)
st = SL.fetch_today(api, lambda: False, now=datetime(2026, 9, 14, 14, 6), qt=QT)
chk("  11 分鐘後 ⇒ 重試並存好", (st, api.ticks_calls), ("saved", 2))


# ══ ④b 昨收只從 tick_hist 取、缺前一天就沒有昨收（PM 必修）══════════════════
print("\n=== ④b 昨收與夜盤：資料缺就是缺，⛔ 不拿舊的頂 ===")
chk("  prev_close_date：沒有前一天 ⇒ None", SL.prev_close_date("2026-09-14", None, []), None)
chk("  週五→週一（3 天）⇒ 用週五", SL.prev_close_date("2026-09-14", "2026-09-11", []), "2026-09-11")
chk("  差 4 天 ⇒ 算數", SL.prev_close_date("2026-09-15", "2026-09-11", []), "2026-09-11")
chk("  差 5 天、1 分 K 沒涵蓋（停更）⇒ None", SL.prev_close_date("2026-09-16", "2026-09-11", ["2026-09-02"]), None)
chk("  差 5 天、1 分 K 涵蓋到前一天且中間沒有交易日（連假）⇒ 算數",
    SL.prev_close_date("2026-09-16", "2026-09-11", ["2026-09-11", "2026-09-16"]), "2026-09-11")
chk("  1 分 K 顯示中間有交易日（缺了一天逐筆）⇒ None",
    SL.prev_close_date("2026-09-14", "2026-09-10", ["2026-09-10", "2026-09-11", "2026-09-14"]), None)
chk("  tmf_1min.csv 停在 09-02、新抓的 09-14 前一個逐筆日是去年 ⇒ None",
    SL.prev_close_date("2026-09-14", "2025-05-29", ["2025-05-29", "2026-09-02"]), None)


def write_day(day, rows):
    """rows：[(HH:MM:SS, price)]。存成永豐 api.ticks 的欄位"""
    ts = [int(pd.Timestamp(f"{day} {t}").value) for t, _ in rows]
    px = [p for _, p in rows]
    df = pd.DataFrame({"ts": ts, "close": px, "volume": [1] * len(px), "bid_price": [p - 1 for p in px],
                       "bid_volume": [1] * len(px), "ask_price": [p + 1 for p in px], "ask_volume": [1] * len(px),
                       "tick_type": [1] * len(px)})
    SL._ticks_dir().mkdir(parents=True, exist_ok=True)
    df.to_csv(SL._ticks_dir() / f"{day}.csv.gz", index=False, compression="gzip")


for _f in list(SL._ticks_dir().glob("*.csv.gz")):
    _f.unlink()
SL.MIN1_CSV = TMP / "no_such_1min.csv"
write_day("2026-09-02", [("08:45:00", 1000), ("09:03:00", 1000), ("13:44:00", 1200), ("13:46:00", 9999)])
write_day("2026-09-04", [("08:45:00", 1300), ("09:03:00", 1300), ("13:44:00", 1300)])
write_day("2026-09-14", [("08:45:00", 1500), ("09:03:00", 1500), ("13:44:00", 1500)])
write_day("2026-09-16", [("08:45:00", 1510), ("09:03:00", 1510), ("13:29:00", 1400), ("13:40:00", 1777)])
write_day("2026-09-17", [("08:45:00", 1600), ("09:03:00", 1600), ("13:44:00", 1600)])
SL.refresh_days(force=True)
RD = {r["date"]: r for r in SL.days()}
chk("  09-02 日盤收盤＝13:45 前最後一筆（13:46 那筆不算）", RD["2026-09-02"]["close"], 1200.0)
chk("  09-04 昨收＝09-02 的 1200（從 tick_hist 取）", RD["2026-09-04"]["prev_close"], 1200.0)
chk("  09-14 跟前一個逐筆日差 10 天、沒 1 分 K 可證明是連假 ⇒ 沒有昨收", RD["2026-09-14"]["prev_close"], None)
chk("  09-16 是結算日 ⇒ 收盤＝13:30 前最後一筆 1400", RD["2026-09-16"]["close"], 1400.0)
chk("  09-17 昨收＝09-16 結算日收盤 1400", RD["2026-09-17"]["prev_close"], 1400.0)
chk("  沒有 1 分 K ⇒ 每天夜盤震幅都是 None", [RD[k]["night_range"] for k in sorted(RD)], [None] * 5)
_og = SL.run(q(t="09:03:00", dir="long", tp=130, sl=130, gap=1))
chk("  gap=1：沒有昨收的兩天（第一天 09-02、09-14）算進「過濾掉」", _og["valid"]["skip_by"], {"gap": 2})
_on = SL.run(q(t="09:03:00", dir="long", tp=130, sl=130, nr=1))
chk("  nr=1：沒有夜盤資料的 5 天全部算進「過濾掉」", (_on["valid"]["skipped"], _on["valid"]["skip_by"]), (5, {"night": 5}))
for _f in list(SL._ticks_dir().glob("*.csv.gz")):
    _f.unlink()
SL.refresh_days(force=True)

# ══ ⑤ 例外不外洩 ═══════════════════════════════════════════════════
print("\n=== ⑤ 抓取丟例外不會影響呼叫端 ===")
for label, api in (("api.ticks 丟例外", FakeApi(boom=RuntimeError("永豐斷線"))),
                   ("api.usage 丟例外", FakeApi(usage_boom=True, rows=fake_rows(DAY))),
                   ("ticks 回傳垃圾", FakeApi(rows={"ts": ["壞掉"], "close": [1]}))):
    reset()
    try:
        st = SL.fetch_today(api, lambda: False, now=MON, qt=QT)
        chk(f"  {label} ⇒ 沒丟出來、狀態 error", st, "error")
    except BaseException as e:
        say(False, f"  {label} ⇒ 例外衝出來了", repr(e))
say(not (SL._ticks_dir() / f"{DAY}.csv.gz").exists(), "  出錯 ⇒ 不留半個檔")


class StopLoop(BaseException):
    pass


_sleep = SL.time.sleep
loops = {"n": 0}


def fake_sleep(s):
    loops["n"] += 1
    if loops["n"] >= 3:
        raise StopLoop()


SL.time.sleep = fake_sleep
try:
    SL.fetch_loop(lambda: (_ for _ in ()).throw(RuntimeError("拿 api 就炸")), lambda: 1 / 0, every=0)
    say(False, "  fetch_loop 應該被測試的 StopLoop 停下來")
except StopLoop:
    say(True, "  fetch_loop：拿 api／問部位都丟例外，迴圈照樣轉了 3 圈（例外全吞在裡面）")
except BaseException as e:
    say(False, "  fetch_loop 讓例外衝出來了", repr(e))
finally:
    SL.time.sleep = _sleep

# ══ ⑥ 前端：沒有下單路徑、沒有建議口吻、預設值走注入常數 ═══════════════════
print("\n=== ⑥ 新分頁的 HTML／JS ===")
page = LP.PAGE
html = page[page.index('<div id="tab-lab"'):page.index("<!-- 【回顧】")]
_j0 = page.index("/* ══════════════ 【策略實驗室】分頁：歷史逐筆回測")
js = page[_j0:page.index("\nrvBind();", _j0)]
say(len(js) > 5000 and "function lbRun" in js and "function tkBind" not in js, "  切出來的確實是 lab 那一段 JS（不多不少）")
lab = html + js
# ⚠️ 用字檢查要先剝註解：註解本身就在寫「⛔ 不預測、不建議」，不剝會把紅線說明當成違規。
#    （下單路徑那組**不剝**，連註解裡都不准出現 —— 更嚴）
import re as _re
lab_code = _re.sub(r"<!--.*?-->", " ", html, flags=_re.S) + \
    "\n".join(_re.sub(r"//.*$", "", ln) for ln in _re.sub(r"/\*.*?\*/", " ", js, flags=_re.S).splitlines())
for w in ("broker", "place_order", "/api/enter", "/api/real/", "/api/fire/on", "/api/fire/off", "method:'POST'",
          'method:"POST"', "pfetch(", "data-act", "data-rdir", "<form", 'type="submit"'):
    chk(f"  沒有 {w}", w in lab, False)
say(all(w in page for w in ("/api/fire/on", "/api/real/", "data-rdir")),
    "  負控組：同一把尺掃整頁找得到 /api/fire/on、/api/real/、data-rdir（尺是活的）")
fetches = sorted(set(x.split("'")[1].split("?")[0] for x in lab.split("fetch(")[1:]))
chk("  只打 GET：/api/lab/meta、/api/lab/run、唯讀的 /api/fire/state", fetches,
    ["/api/fire/state", "/api/lab/meta", "/api/lab/run"])
for w in ("token", "PTOK"):
    chk(f"  lab 的程式碼沒有 {w}（讀 fire/state 只拿 method，⛔ 不取 token、不送 POST）", w in lab_code, False)
chk("  「現在真單用的」沒有寫死在 HTML 上", "現在真單用的" in _re.sub(r"<!--.*?-->", " ", html, flags=_re.S), False)
say("function lbFire" in js and "x.armed===true?x.method:null" in js and "['A','lbm-bar5'],['B','lbm-open']" in js,
    "  標籤照 /api/fire/state 的 method 標（A＝5 分 K、B＝開盤起；沒開就不標）")
chk("  文案不暗示會自動補齊過去的日子", ("自己長出來" in lab_code, "陸續補進來中" in lab_code), (False, True))
for w in ("建議", "推薦", "會賺", "明天", "應該進場", "最佳", "預測", "期望值", "訊號強度"):
    chk(f"  畫面文字沒有「{w}」", w in lab_code, False)
chk("  分頁鈕是「策略實驗室」且舊的「自動下單（模擬）」鈕不在了",
    ('data-tab="lab">策略實驗室<' in page, 'data-tab="auto"' in page), (True, False))
chk("  setTab 切的是 #tab-lab", "getElementById('tab-lab').hidden=(t!=='lab')" in page, True)
say("value=RULE_SIGNAL_AT" in js and "value=RULE_TP" in js and "value=RULE_SL" in js,
    "  進場時間／停利／停損預設值來自注入的 RULE_*")
chk("  lab 那一段沒有寫死 130 或 09:03", ("130" in lab, "09:03" in lab, "lbSec(9,3" in lab), (False, False, False))
chk("  頁面上沒有未替換的 RULE 佔位", "__RULE_" in page, False)

# 後端：strategy_lab 不 import broker／auto_fire；_lab_get 不碰 broker
tree = ast.parse((HERE / "strategy_lab.py").read_text(encoding="utf-8"))
imps = set()
for n in ast.walk(tree):
    if isinstance(n, ast.Import):
        imps |= {a.name for a in n.names}
    elif isinstance(n, ast.ImportFrom):
        imps.add(n.module)
    elif isinstance(n, ast.Name) and n.id in ("broker", "auto_fire"):
        imps.add("name:" + n.id)
chk("  strategy_lab.py 沒有 import／引用 broker、auto_fire", sorted(imps & {"broker", "auto_fire", "name:broker", "name:auto_fire"}), [])
src = (HERE / "live_panel.py").read_text(encoding="utf-8")
lp_tree = ast.parse(src)
fn = [n for n in ast.walk(lp_tree) if isinstance(n, ast.FunctionDef) and n.name == "_lab_get"][0]
chk("  Handler._lab_get 裡沒有 broker／auto_fire",
    sorted({n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and n.id in ("broker", "auto_fire")}), [])
calls = {(n.func.value.id, n.func.attr) for n in ast.walk(lp_tree) if isinstance(n, ast.Call)
         and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) and n.func.value.id == "strategy_lab"}
chk("  live_panel 只在 handler 呼叫 run_exclusive（主迴圈不會算回測）",
    sorted(c for c in calls if c[1] in ("run", "run_exclusive", "fetch_today")), [("strategy_lab", "run_exclusive")])


# ══ ⑥b 面板主流程跟新功能隔開（lab-qa 退件 R1，真錢）══════════════════════
print("\n=== ⑥b strategy_lab 壞掉時面板照跑 ===")
import os
import subprocess
_code = ("import sys; sys.modules['strategy_lab']=None\n"
         "import live_panel as LP\n"
         "assert LP.strategy_lab is None\n"
         "r = LP.start_strategy_lab_fetch()\n"
         "print('IMPORTED', r)\n")
pr = subprocess.run([sys.executable, "-c", _code], cwd=str(HERE), capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=180, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
say(pr.returncode == 0 and "IMPORTED False" in pr.stdout,
    "  ① strategy_lab 載入失敗 ⇒ import live_panel 照樣成功、start_strategy_lab_fetch 不丟例外",
    (pr.stdout[-120:] + pr.stderr[-300:]).replace("\n", " ") if pr.returncode else "")
say("策略實驗室】載入失敗" in pr.stdout, "  ① 而且有印警告（不是安靜地少）")
mfn = [n for n in ast.walk(lp_tree) if isinstance(n, ast.FunctionDef) and n.name == "main"][0]
mcalls = [(getattr(n.func.value, "id", None), n.func.attr) if isinstance(n.func, ast.Attribute)
          else (None, getattr(n.func, "id", None)) for n in ast.walk(mfn) if isinstance(n, ast.Call)]
say(("auto_fire", "configure") in mcalls and ("auto_fire", "start") in mcalls
    and (None, "start_strategy_lab_fetch") in mcalls,
    "  ① main() 照樣呼叫 auto_fire.configure／start，研究功能只走 start_strategy_lab_fetch()")
chk("  ① main() 裡沒有直接碰 strategy_lab.*（全部走有 try 的那一支）",
    sorted({n.attr for n in ast.walk(mfn) if isinstance(n, ast.Attribute)
            and getattr(n.value, "id", None) == "strategy_lab"}), [])
_lines = src.splitlines()
_i_af = next(i for i, l in enumerate(_lines) if l.strip() == "auto_fire.start()")
_i_lab = next(i for i, l in enumerate(_lines) if l.strip() == "start_strategy_lab_fetch()")
say(_i_af < _i_lab, "  ① 研究功能排在 auto_fire.start() 之後（就算它出事，自動下單已經起來了）")


class _Boom:
    def __getattr__(self, n):
        raise RuntimeError("fetch_loop 拿不到")


_saved = LP.strategy_lab
LP.strategy_lab = _Boom()
try:
    chk("  ② 起抓取執行緒時丟例外 ⇒ 呼叫端沒被打斷、回 False", LP.start_strategy_lab_fetch(), False)
except BaseException as e:
    say(False, "  ② 起抓取執行緒時例外衝出來了", repr(e))
finally:
    LP.strategy_lab = _saved

srv2 = ThreadingHTTPServer(("127.0.0.1", 0), LP.Handler)
threading.Thread(target=srv2.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv2.server_address[1]}"
LP.strategy_lab = None
try:
    st3 = get("/api/lab/run?t=09:03:00&dir=bar5&tp=130&sl=130", timeout=5)
    chk("  ③ strategy_lab 是 None ⇒ /api/lab/run 回 503「策略實驗室載入失敗」",
        (st3[0], st3[1].get("msg")), (503, "策略實驗室載入失敗"))
    chk("  ③ /api/lab/meta 也是 503", get("/api/lab/meta", timeout=5)[0], 503)
finally:
    LP.strategy_lab = _saved
    srv2.shutdown()
say("s===503" in js and "LB.err=" in js, "  ③ 前端 meta 回 503 時把錯誤寫到畫面上（lbMark 顯示 LB.err）")

# ══ ⑦ 收尾 ══════════════════════════════════════════════════════════
print("\n=== ⑦ 收尾 ===")
say(str(SL.LAB_DIR).startswith(str(TMP)), "  寫檔的測項全程 LAB_DIR 在暫存區", str(SL.LAB_DIR))
SL.LAB_DIR, SL.MIN1_CSV = REAL_LAB, REAL_CSV
chk("  真的 tick_hist/ 一個位元組都沒被動過", tree_hash(REAL_LAB), REAL_HASH)

print("\n總結:", "全部通過" if not FAIL else f"{FAIL} 項失敗")
sys.exit(1 if FAIL else 0)
