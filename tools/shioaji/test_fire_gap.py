# -*- coding: utf-8 -*-
"""
【今天】那張卡的「現在離門檻還差幾點」（`live_panel.fire_gap`）—— 探針。

⭐ 2026-09-21 Benson 要的東西：開盤那段看得到「還差幾點」，不用等三個時刻跳結果。
   起因是 09-21 開箱箱子 170 點、門檻 178 點 —— **差 8 點沒做成**。
   這一支的主軸就是**把那一天重現一次**，數字對得出 170／178／8。

⛔⛔ 這一支在守的三件事（壞掉的話畫面會說謊）：
   ① **門檻與換算都走 auto_fire**（`fast_threshold` / `orb_hist_ready` / `approx_points`）
      ⇒ 這裡用它算出來的數字對答案，⛔ 不在測試裡自己乘一次百分比。
   ② **拿不到就留白**：沒報價／報價過期／不是多方聯軍／已經有定論 ⇒ **端 None**
      （⛔ 不可以端一個「—」或舊價算出來的數字，那看不出來是缺）。
   ③ **一個字都不准預測**（CLAUDE.md）：訊息裡不准出現機率／勝率／期望值／應該會…

⛔ 不連永豐、不送單、不碰他真的資料夾：每一個會讀檔／讀開關的地方都導到暫存區，
   並且 ⛔ 斷言真的 `tools/shioaji/AUTO_ORDERS_ON` 不存在。

怎麼跑（PowerShell）：
    $env:PYTHONIOENCODING="utf-8"; $env:PYTHONUTF8="1"
    & "…\\trade-log\\.venv\\Scripts\\python.exe" "…\\tools\\shioaji\\test_fire_gap.py"
"""
import json
import pathlib
import shutil
import sys
import tempfile
from datetime import date, datetime

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import auto_fire as AF              # noqa: E402
import broker                       # noqa: E402
import live_panel as LP             # noqa: E402

FAIL = 0


def say(cond, name, extra=""):
    global FAIL
    FAIL += not cond
    print(("  OK   " if cond else "  FAIL ") + name + (f"  {extra}" if extra else ""))


def chk(name, got, want):
    global FAIL
    ok = got == want
    FAIL += not ok
    print(("  OK   " if ok else "  FAIL ") + name
          + ("" if ok else f"  (得到 {got!r}，期待 {want!r})"))


def _boom(kind, err, tb):
    """⛔ 自己掛掉也要留下一項具名的 FAIL（只數 FAIL 行的話會有假綠燈）。"""
    import traceback
    traceback.print_exception(kind, err, tb)
    print("  FAIL ⛔ 測試自己掛掉了（未捕捉的例外）：" + str(err)[:120])
    print("⛔ 有 ? 項沒過（測試中斷）")
    sys.stdout.flush()


sys.excepthook = _boom

REAL_ARM = AF.ARM_FLAG
if REAL_ARM.exists():
    print("  FAIL ⛔ tools/shioaji/AUTO_ORDERS_ON 竟然存在！拒絕往下跑。")
    print("⛔ 有 1 項沒過")
    sys.exit(1)

# ⛔ 全部導到暫存區（fire_gap 自己不寫檔，但它會叫到會**讀檔**的那幾支）
TMP = pathlib.Path(tempfile.mkdtemp(prefix="fire-gap-"))
AF.ARM_FLAG = TMP / "AUTO_ORDERS_ON"
AF.FIRE_DIR = TMP / "autofire"
AF.FAST_HIST = TMP / "fast_hist.jsonl"
AF.ORB_HIST = TMP / "orb_hist.jsonl"
broker.REAL_FLAG = TMP / "REAL_ORDERS_ON"          # 不存在 ⇒ 演練（⛔ 不會送單）
broker.ORDER_DIR = TMP / "real_orders"
broker.TRADE_DIR = TMP / "real_trades"
LP.AUTO_DIR = TMP / "autotest"
LP.AUTO_REAL_DIR = TMP / "real_trades"
SENT = []
broker.enter = lambda *a, **k: (SENT.append("enter") or (False, "測試治具", None))
broker.close = lambda *a, **k: (SENT.append("close") or (False, "測試治具"))

AF.configure(signal_at=LP.SIGNAL_AT, signal_sec=LP.SIGNAL_SEC, late_ms=LP.AUTO_LATE_MS,
             gap_s=LP.AUTO_GAP_S, sig_fn=LP.auto_sig, dirs_fn=LP.auto_dirs,
             eod_at=LP.EOD_CLOSE_AT, pctl=LP.FAST_PCTL,
             rev_at=LP.REV_AT, rev_sec=LP.REV_SEC)

TODAY = str(date.today())
# 開關寫 U ⇒ 多方聯軍（三個候選那張卡、還有這一份距離，都只有 U 才有）
AF.ARM_FLAG.write_text("U", encoding="utf-8")

# 歷史：⚠️ 刻意讓門檻是**一個乾淨的數字**，這樣底下的期待值一眼看得出來從哪來。
#   ・快攻：40 天全部 0.2919% ⇒ 第 80 百分位就是 0.2919%（＝他真單現在的門檻）
#   ・開箱：20 天全部 0.3728% ⇒ 中位數 0.3728%（＝09-21 那天的門檻）
AF.FAST_HIST.write_text("".join(
    json.dumps({"date": "2026-07-%02d" % (d + 1), "move_pct": 0.2919}, ensure_ascii=False)
    + "\n" for d in range(AF.FAST_RULE["window"])), encoding="utf-8")
AF.ORB_HIST.write_text("".join(
    json.dumps({"date": "2026-08-%02d" % (d + 1), "box_pct": 0.3728}, ensure_ascii=False)
    + "\n" for d in range(AF.ORB_RULE["hist_n"])), encoding="utf-8")


def fresh_state():
    """每一段都重讀一次（快取是靠 mtime／size，⛔ 不要跨情境共用一份 out）。"""
    AF._ORB.update({"date": None, "box": None, "thr": None, "done": False, "msg": None})
    return AF.state()


def make_today(px, *, ref0900=None, p0900=None, box=None):
    """
    ⚠️ 用**真的** `LP.Today`（⛔ 不自己捏一個假物件）：`_auto_snap()` 讀哪幾個欄位、
       `minute_bar` 的分鐘索引怎麼算，都要跟正式那條路完全一樣。
      ref0900  09:00 以前最後一筆（走幅的分母）   ⇒ 餵一筆 08:59
      p0900    09:00 那一分鐘第一筆（方向）       ⇒ 餵一筆 09:00
      box      (hi, lo)：箱子那五分鐘餵兩筆，讓 minute_bar 的高低就是這一組
    """
    st = LP.Today(prev_close=47000.0)
    d = date.today()
    if ref0900 is not None:
        st.feed(ref0900, 1, datetime(d.year, d.month, d.day, 8, 59, 30), True)
    if p0900 is not None:
        st.feed(p0900, 1, datetime(d.year, d.month, d.day, 9, 0, 1), True)
    if box is not None:
        hi, lo = box
        st.feed(hi, 1, datetime(d.year, d.month, d.day, 9, 1, 0), True)
        st.feed(lo, 1, datetime(d.year, d.month, d.day, 9, 2, 0), True)
    st.feed(px, 1, datetime(d.year, d.month, d.day, 9, 2, 30), True)
    return st


def at(h, m, s=0):
    d = date.today()
    return datetime(d.year, d.month, d.day, h, m, s)


def gap_of(out, when, st):
    LP.CURRENT_STATE["today"] = st
    return LP.fire_gap(out, now=when)


print("=== ① 快攻：09:03:30 之前的「還差幾點」 ===")
out = fresh_state()
chk("  門檻讀出來是 0.2919%", round(out["fast"]["thr_pct"], 4), 0.2919)
st = make_today(47628.0, ref0900=47554.0, p0900=47554.0)
g = gap_of(out, at(9, 2, 30), st)
say(g is not None, "  09:02:30 端得出距離")
f = (g or {}).get("fast") or {}
chk("  現在走幾點（47628−47554）", f.get("now_pts"), 74)
chk("  門檻幾點（0.2919% × 47554）", f.get("need_pts"),
    AF.approx_points(0.2919, 47554.0))
chk("  還差幾點 ＝ 門檻 − 現在", f.get("gap_pts"), f.get("need_pts") - 74)
say("還差 65 點" in f.get("msg", ""), "  訊息裡有「還差 65 點」", f.get("msg", ""))
say("做多" in f.get("msg", ""), "  訊息講得出現在是做多", f.get("msg", ""))

print("\n=== ② 快攻：方向往下 ⇒ 要講「只做多會跳過」 ===")
st = make_today(47480.0, ref0900=47554.0, p0900=47554.0)
f = (gap_of(out, at(9, 2, 30), st) or {}).get("fast") or {}
chk("  現在走幾點（47554−47480）", f.get("now_pts"), 74)
say("只做多" in f.get("msg", ""), "  訊息講得出「只做多 ⇒ 會跳過」", f.get("msg", ""))

print("\n=== ③ 快攻：已經夠快 ⇒ 不可以說「今天會做」 ===")
st = make_today(47554.0 + 200, ref0900=47554.0, p0900=47554.0)
f = (gap_of(out, at(9, 2, 30), st) or {}).get("fast") or {}
say(f.get("gap_pts", 1) <= 0, "  還差的點數 ≤ 0", str(f.get("gap_pts")))
say("目前夠快" in f.get("msg", ""), "  用的是「目前夠快」這種講法", f.get("msg", ""))
say(LP.SIGNAL_AT in f.get("msg", ""), "  而且有註明「%s 那一刻才算數」" % LP.SIGNAL_AT)

print("\n=== ④ 開箱：把 2026-09-21 那天重現（箱子 170／需要 178／差 8）===")
out = fresh_state()
chk("  箱寬門檻（09:05 以前就答得出來）", out["orb"]["hist_med_pct"], 0.3728)
# ⚠️ 現價要**落在箱子裡**（09:03 那一刻本來就還在箱子那五分鐘內，它自己也算箱子的一部分）
st = make_today(47650.0, ref0900=47554.0, p0900=47554.0, box=(47693.0, 47523.0))
o = (gap_of(out, at(9, 3, 0), st) or {}).get("orb") or {}
chk("  箱子畫到現在幾點", o.get("now_pts"), 170)
chk("  需要幾點（0.3728% × 現價）", o.get("need_pts"),
    AF.approx_points(0.3728, 47650.0))
chk("  還差幾點", o.get("gap_pts"), 8)
say("還差 8 點" in o.get("msg", ""), "  訊息裡有「還差 8 點」", o.get("msg", ""))
say("進行中" in o.get("msg", ""),
    "  ⛔ 有標「進行中」（這是 minute_bar 的進度，不是 09:05 那把尺）", o.get("msg", ""))

print("\n=== ⑤ 開箱：箱子定案之後 ⇒ 離上緣還差幾點 ===")
AF._ORB.update({"date": TODAY, "box": {"hi": 47693.0, "lo": 47523.0, "w": 170.0},
                "thr": {"med": 0.3728, "n": 20}, "done": False, "msg": "等突破"})
out2 = AF.state()
chk("  這個時候是「等突破」", out2["orb"]["stage"], "wait")
st = make_today(47650.0, ref0900=47554.0, p0900=47554.0)
o = (gap_of(out2, at(9, 7, 0), st) or {}).get("orb") or {}
chk("  離上緣還差幾點（47693−47650）", o.get("gap_pts"), 43)
say("離上緣還差 43 點" in o.get("msg", ""), "  訊息講的是上緣", o.get("msg", ""))
say(AF.ORB_BREAK_BY in o.get("msg", "") or AF.ORB_BREAK_BY[:5] in o.get("msg", ""),
    "  有講突破的截止時刻", o.get("msg", ""))

print("\n=== ⑥ 純回馬：09:03:30 往下 ⇒ 要漲過那個價才算反轉 ===")
AF.FIRE_DIR.mkdir(parents=True, exist_ok=True)
(AF.FIRE_DIR / (TODAY[:7] + ".jsonl")).write_text(json.dumps(
    {"rec": "skip", "date": TODAY, "why": "not_fast", "why_msg": "快攻：今天開盤不夠快",
     "cand": "fast", "rule": "union", "method": "U", "px": 47132.0, "d": -1,
     "live": True, "at": "09:03:30.100"}, ensure_ascii=False) + "\n", encoding="utf-8")
out3 = fresh_state()
st = make_today(47089.0, ref0900=47554.0, p0900=47554.0)
r = (gap_of(out3, at(9, 7, 0), st) or {}).get("rev") or {}
chk("  還差幾點（47132−47089）", r.get("gap_pts"), 43)
say("要漲過 47132" in r.get("msg", ""), "  訊息講得出要漲過哪個價", r.get("msg", ""))
st = make_today(47200.0, ref0900=47554.0, p0900=47554.0)
r = (gap_of(out3, at(9, 7, 0), st) or {}).get("rev") or {}
say("已經漲過" in r.get("msg", "") and LP.REV_AT in r.get("msg", ""),
    "  漲過之後要說「%s 那一刻還在上面才算」" % LP.REV_AT, r.get("msg", ""))

print("\n=== ⑦ 純回馬：09:03:30 往上 ⇒ 反轉只會是做空 ⇒ 這個候選做不成 ===")
(AF.FIRE_DIR / (TODAY[:7] + ".jsonl")).write_text(json.dumps(
    {"rec": "skip", "date": TODAY, "why": "not_fast", "why_msg": "快攻：今天開盤不夠快",
     "cand": "fast", "rule": "union", "method": "U", "px": 47628.0, "d": 1,
     "live": True, "at": "09:03:30.100"}, ensure_ascii=False) + "\n", encoding="utf-8")
out4 = fresh_state()
st = make_today(47700.0, ref0900=47554.0, p0900=47554.0)
r = (gap_of(out4, at(9, 7, 0), st) or {}).get("rev") or {}
say("做空" in r.get("msg", "") and "做不成" in r.get("msg", ""),
    "  講得出「反轉只會變成做空 ⇒ 做不成」", r.get("msg", ""))

print("\n=== ⑧ ⛔ 拿不到就留白（⛔ 不准端一個看起來像數字的東西）===")
out5 = fresh_state()
st = make_today(47628.0, ref0900=47554.0, p0900=47554.0)
say(gap_of(out5, at(8, 30), st) is None, "  08:30（還沒開盤）⇒ None")
say(gap_of(out5, at(9, 31), st) is None, "  09:31（過了突破截止）⇒ None")
st_old = make_today(47628.0, ref0900=47554.0, p0900=47554.0)
st_old.last_recv = st_old.last_recv - 999          # 報價 999 秒沒動
say(gap_of(out5, at(9, 2), st_old) is None, "  報價過期 ⇒ None（⛔ 不拿舊價算）")
st_none = LP.Today(prev_close=47000.0)
say(gap_of(out5, at(9, 2), st_none) is None, "  沒有任何成交價 ⇒ None")
LP.CURRENT_STATE["today"] = None
say(LP.fire_gap(out5, now=at(9, 2)) is None, "  面板還沒有今天這個物件 ⇒ None")

print("\n=== ⑨ ⛔ 跑快攻回馬槍（A）的時候完全不端（A 沒有三個候選）===")
AF.ARM_FLAG.write_text("A", encoding="utf-8")
out6 = fresh_state()
chk("  現在不是多方聯軍", out6["union"], False)
st = make_today(47628.0, ref0900=47554.0, p0900=47554.0)
say(gap_of(out6, at(9, 2), st) is None, "  ⇒ 一行都不端")
AF.ARM_FLAG.write_text("U", encoding="utf-8")

print("\n=== ⑩ ⛔ 有定論之後就不端了（定論那一行才是真相）===")
(AF.FIRE_DIR / (TODAY[:7] + ".jsonl")).write_text(json.dumps(
    {"rec": "skip", "date": TODAY, "why": "orb_narrow", "why_msg": "開箱：箱子太窄",
     "cand": "orb", "rule": "union", "method": "U", "box_hi": 47693.0,
     "box_lo": 47523.0, "box_w": 170.0, "live": True, "at": "09:05:00"},
    ensure_ascii=False) + "\n", encoding="utf-8")
out7 = fresh_state()
chk("  開箱已經有定論", out7["orb"]["stage"], "done")
st = make_today(47694.0, ref0900=47554.0, p0900=47554.0, box=(47693.0, 47523.0))
g = gap_of(out7, at(9, 3), st)
say(((g or {}).get("orb")) is None, "  ⇒ 開箱那一行不端")

print("\n=== ⑪ ⛔ 一個字都不准預測 ===")
BAN = ["機率", "勝率", "期望值", "預估", "應該會", "很可能", "建議", "看好", "會賺"]
msgs = []
for o_, w_, s_ in ((out, at(9, 2, 30), make_today(47628.0, ref0900=47554.0, p0900=47554.0)),
                   (out2, at(9, 7), make_today(47650.0, ref0900=47554.0, p0900=47554.0)),
                   (out3, at(9, 7), make_today(47089.0, ref0900=47554.0, p0900=47554.0)),
                   (out4, at(9, 7), make_today(47700.0, ref0900=47554.0, p0900=47554.0))):
    gg = gap_of(o_, w_, s_) or {}
    msgs += [(gg.get(k) or {}).get("msg", "") for k in AF.CANDS]
hits = [(w, m) for m in msgs for w in BAN if w in m]
say(not hits, "  所有訊息都沒有預測字眼", str(hits[:3]))
say(any(msgs), "  （尺的自證：上面真的有話可以檢查）", "%d 句" % len([m for m in msgs if m]))

print("\n=== ⑫ ⛔ 它壞掉不可以把整份狀態帶掉 ===")
say("out[\"gap\"] = fire_gap(out)" in pathlib.Path(LP.__file__).read_text(encoding="utf-8"),
    "  端點有接上 fire_gap")
_real = LP.fire_gap
LP.fire_gap = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("故意壞的"))
try:
    try:
        broken = {"gap": LP.fire_gap({})}
    except Exception as e:
        broken = {"gap": None, "gap_err": str(e)}
    say(broken["gap"] is None and "故意壞的" in broken.get("gap_err", ""),
        "  壞掉的時候是「gap=None ＋ 一句話」，不是整份狀態掛掉")
finally:
    LP.fire_gap = _real

print("\n=== ⑬ ⛔ 收尾 ===")
chk("  ⛔ broker.enter / broker.close 全程 0 次", SENT, [])
say(str(TMP) in str(AF.FAST_HIST) and str(TMP) in str(AF.ORB_HIST)
    and str(TMP) in str(AF.FIRE_DIR), "  全程都在暫存區", str(TMP))
say(not REAL_ARM.exists(),
    "  ⛔⛔ 真的 AUTO_ORDERS_ON **不存在**（測試絕對不可以把它建出來）")

shutil.rmtree(TMP, ignore_errors=True)
print("\n" + ("全部通過 ✅" if FAIL == 0 else f"⛔ 有 {FAIL} 項沒過"))
sys.exit(1 if FAIL else 0)
