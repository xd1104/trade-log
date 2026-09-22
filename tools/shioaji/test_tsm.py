# -*- coding: utf-8 -*-
"""
【模擬】第八條線「台積電快攻」的探針（2026-09-22 晚，取代 test_usml.py）。

⛔ 在守的事：
  ① **時間對齊**：Alpaca 標開始時間 ⇒ 美東 9:30 那一根要到 9:35 才收完（`done_at`），
     台指進場那一根＝標籤 ≤ done_at 的最後一根；改掉進場之後的價，決定與進場價不能變
  ② 缺什麼就講什麼：休市／還沒拿到／歷史不夠／K 棒沒到齊 ⇒ 各自的理由，⛔ 不猜
  ③ 快不快、停利停損寬度、同一根兩邊都碰 ⇒ 停損
  ④ 歷史只用 E 以前的晚上（⛔ 這一晚自己的走幅與振幅不准進自己的門檻）
  ⑤ 落地：寫得進去、讀得回來；⛔ 已下架的 usml 讀得到、但寫不進去、也不算壞資料
  ⑥ 內頁那張圖是夜盤圖、有進出場與停利停損線
  ⑦ 1 分 K 存回本機（從 test_usml 搬過來）
  ⑧ ⛔ 不碰正式的 sim_lanes/、⛔ 不連永豐、⛔ 不送任何單、⛔ 不連 Alpaca（us_feed 用假檔）
"""
import csv
import json
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.stdout.reconfigure(encoding="utf-8")

import sim_lanes as S  # noqa: E402
import us_feed  # noqa: E402

FAIL = 0
REAL_SIM = S.SIM_DIR


def say(cond, name, extra=""):
    global FAIL
    FAIL += not cond
    print(("  OK   " if cond else "  FAIL ") + name + (f"  {extra}" if extra else ""))


def chk(name, got, want):
    global FAIL
    ok = got == want
    FAIL += not ok
    print(("  OK   " if ok else "  FAIL ") + name + ("" if ok else f"  (得到 {got!r}，期待 {want!r})"))


def _boom(kind, err, tb):
    import traceback
    traceback.print_exception(kind, err, tb)
    print("  FAIL ⛔ 測試自己掛掉了：" + str(err)[:120])
    print("⛔ 有 ? 項沒過（測試中斷）")


sys.excepthook = _boom

TMP = Path(tempfile.mkdtemp(prefix="tsm-test-"))
S.SIM_DIR = TMP / "sim_lanes"
S.TSM_CTX = TMP / "tsm_ctx.json"
us_feed.DIR = TMP / "us_bars"
us_feed._fetch_month = lambda *a, **k: None       # ⛔ 不連 Alpaca

print("=== ① ⛔ 時間對齊（Alpaca 標開始時間）===")
E = date(2026, 6, 10)            # 夏令 ⇒ 美東 9:30 ＝ 台北 21:30
us_feed.DIR.mkdir(parents=True)
with (us_feed.DIR / "TSM-sip-2026-06.csv").open("w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["start_utc", "open", "close"])
    w.writerow(["2026-06-10T13:25:00Z", 99.0, 100.0])      # 盤前 9:25（⛔ 不是開盤那根）
    w.writerow(["2026-06-10T13:30:00Z", 100.0, 101.0])     # 開盤第一根 9:30~9:35
    w.writerow(["2026-06-10T13:35:00Z", 101.0, 150.0])     # 9:35~9:40（⛔ 未來，不准用）
    w.writerow(["2026-06-12T13:30:00Z", 100.0, 100.5])     # 06-11 沒有 ⇒ 休市
first = us_feed.first5("TSM", E)
say(isinstance(first, dict) and abs(first["mv_pct"] - 1.0) < 1e-9,
    "  取的是美東 9:30 開始那一根（+1.00%），⛔ 不是盤前、⛔ 也不是 9:35 那根", str(first and first["mv_pct"]))
chk("  那一根收完的台北時刻 ＝ 21:35（夏令）", first["done_at"], datetime(2026, 6, 10, 21, 35))
chk("  06-11 月檔在、那天沒有 ⇒ 美股休市", us_feed.first5("TSM", date(2026, 6, 11)), "closed")
chk("  06-30 之後沒資料 ⇒ None（還沒拿到，⛔ 不是休市）", us_feed.first5("TSM", date(2026, 6, 30)), None)
Ew = date(2026, 1, 14)           # 冬令
with (us_feed.DIR / "TSM-sip-2026-01.csv").open("w", newline="", encoding="utf-8") as f:
    f.write("start_utc,open,close\n2026-01-14T14:30:00Z,100,99\n")
chk("  冬令 ⇒ 22:35", us_feed.first5("TSM", Ew)["done_at"], datetime(2026, 1, 14, 22, 35))

# ── 造一晚合成的 1 分 K（⛔ 假價，12000 附近）
T = S.us_open_min(E)
base = 12000.0


def make_bars(E, after=None):
    rows = []
    d0 = pd.Timestamp(E)
    for k in range(15 * 60 + 1, 24 * 60):
        px = base + (k - 15 * 60) * 0.01
        if after and k > T + 5:
            px = after(k, px)
        rows.append({"ts": d0 + pd.Timedelta(minutes=k), "Open": px, "High": px + 1,
                     "Low": px - 1, "Close": px, "Volume": 1, "Amount": 1})
    for k in range(0, 5 * 60 + 1):
        px = base + 50
        if after:
            px = after(k + 1440, px)
        rows.append({"ts": pd.Timestamp(E + timedelta(days=1)) + pd.Timedelta(minutes=k), "Open": px,
                     "High": px + 1, "Low": px - 1, "Close": px, "Volume": 1, "Amount": 1})
    return pd.DataFrame(rows)


BARS = make_bars(E)
CTX = {"mv": {str(E - timedelta(days=x)): (0.3 if x % 2 else -0.2) for x in range(1, 45)},
       "rng": {str(E - timedelta(days=x)): 0.5 for x in range(1, 25)}}
r = S.tsm_eval(E, BARS, first, CTX)
chk("  走幅 1% ≥ 門檻 ⇒ 做多", r.get("decision"), "做多")
chk("  進場那一根的標籤 ＝ 21:35（≤ done_at 的最後一根）", r.get("c_label"), "21:35")
mm, H, L, C = S.night_frame(BARS, E)
chk("  進場價 ＝ 標籤 21:35 那根的收盤", r.get("entry"), round(float(C[mm == T + 5][0]), 1))
r_fut = S.tsm_eval(E, make_bars(E, after=lambda k, px: px * 1.001), first, CTX)
chk("  ⛔ 改掉進場之後的價，決定與進場價都不變",
    (r_fut.get("decision"), r_fut.get("entry")), (r["decision"], r["entry"]))
gap = BARS[pd.to_datetime(BARS["ts"]) != pd.Timestamp(E) + pd.Timedelta(minutes=T + 5)]
r_gap = S.tsm_eval(E, gap, first, CTX)
chk("  21:35 那分鐘沒成交 ⇒ 往回拿 21:34（⛔ 不往後拿 21:36）", r_gap.get("c_label"), "21:34")

print("\n=== ② 缺什麼就講什麼 ===")
chk("  美股休市 ⇒ 不做、us_closed",
    (S.tsm_eval(E, BARS, "closed", CTX).get("decision"), S.tsm_eval(E, BARS, "closed", CTX).get("why")),
    ("不做", "us_closed"))
chk("  還沒拿到台積電 ⇒ pending no_us", S.tsm_eval(E, BARS, None, CTX).get("why"), "no_us")
chk("  歷史不夠 ⇒ 不做、no_hist", S.tsm_eval(E, BARS, first, {"mv": {}, "rng": {}}).get("why"), "no_hist")
short = BARS[pd.to_datetime(BARS["ts"]) < pd.Timestamp(E) + pd.Timedelta(hours=23)]
chk("  K 棒沒到 04:58 ⇒ pending incomplete", S.tsm_eval(E, short, first, CTX).get("why"), "incomplete")

print("\n=== ③ 快不快／停利停損 ===")
slow = dict(first, mv_pct=0.1)
chk("  走幅 0.1% < 門檻 ⇒ 不做、not_fast", S.tsm_eval(E, BARS, slow, CTX).get("why"), "not_fast")
thr = float(np.percentile(np.abs(list(CTX["mv"].values())[:40]), S.TSM_PCTL))
chk("  門檻 ＝ 過去 40 晚 |走幅| 的第 80 百分位", r.get("tsm_thr"), round(thr, 4))
dn = dict(first, mv_pct=-1.0)
chk("  跌 1% ⇒ 做空", S.tsm_eval(E, BARS, dn, CTX).get("decision"), "做空")
w = r["entry"] * 0.5 / 100
say(abs(r["tpsl_points"] - round(w, 1)) < 0.11, "  框寬 ＝ 進場價 × 過去 20 晚振幅平均（0.5%）", str(r["tpsl_points"]))
hitsl = S.tsm_eval(E, make_bars(E, after=lambda k, px: r["entry"] - w - 5 if k == T + 20 else px), first, CTX)
chk("  跌破框 ⇒ 停損", hitsl.get("exit_reason"), "停損")
say(abs(hitsl["points"] - (-w - hitsl["cost"])) < 0.2, "  停損點數 ＝ −框寬 − 成本", str(hitsl["points"]))


def both(k, px):
    return px


BB = make_bars(E)
m = pd.to_datetime(BB["ts"]) == pd.Timestamp(E) + pd.Timedelta(minutes=T + 20)
BB.loc[m, "High"] = r["entry"] + w + 5
BB.loc[m, "Low"] = r["entry"] - w - 5
chk("  ⛔ 同一根兩邊都碰 ⇒ 算停損（保守）", S.tsm_eval(E, BB, first, CTX).get("exit_reason"), "停損")
chk("  都沒碰到 ⇒ 04:58 收盤平", r.get("exit_reason"), "收盤")
chk("  出場標籤 04:58", r.get("exit_label"), "04:58")

print("\n=== ④ 歷史只用 E 以前 ===")
ctx2 = {"mv": dict(CTX["mv"], **{str(E): 99.0, str(E + timedelta(days=1)): 99.0}),
        "rng": dict(CTX["rng"], **{str(E): 50.0})}
r4 = S.tsm_eval(E, BARS, first, ctx2)
chk("  ⛔ 這一晚與之後的走幅／振幅不影響門檻與框寬", (r4.get("tsm_thr"), r4.get("tpsl_points")),
    (r.get("tsm_thr"), r.get("tpsl_points")))
mv, rng = S.tsm_facts(E, BARS, first)
seg = (mm >= T + 5) & (mm <= S.NIGHT_EXIT_MIN)
want = (H[seg].max() - L[seg].min()) / C[mm == T + 5][0] * 100
say(mv == 1.0 and abs(rng - want) < 1e-5, "  記進歷史的振幅 ＝ 進場那根到 04:58（⛔ 不含進場前）",
    "%.4f vs %.4f" % (rng, want))
chk("  台積電那天休市 ⇒ 不記歷史", S.tsm_facts(E, BARS, "closed"), (None, None))

print("\n=== ⑤ 落地 ===")
say(S.append_row(dict(r, calc="backfill")), "  定論寫得進去")
say(not S.append_row(dict(r, calc="backfill")), "  ⛔ 同一天同一條不會重複寫")
_rows, _st = S.read_rows()
back = _rows.get(("tsm", str(E)))
say(back is not None and back["points"] == r["points"], "  讀得回來、點數一樣")
old = {"lane": "usml", "date": "2026-06-09", "decision": "做多", "entry": 1, "exit": 2,
       "exit_reason": "時間到", "points": 1.0}
with (S.SIM_DIR / "2026-06.jsonl").open("a", encoding="utf-8") as f:
    f.write(json.dumps(old, ensure_ascii=False) + "\n")
_rows, _st = S.read_rows()
chk("  ⛔ 已下架的 usml 舊列讀得進來、不算壞資料", (_st["bad"], ("usml", "2026-06-09") in _rows), (0, True))
try:
    S.append_row(dict(old, date="2026-06-08"))
    say(False, "  ⛔ 已下架的 usml 寫不進去")
except ValueError:
    say(True, "  ⛔ 已下架的 usml 寫不進去")
st = S.state(now=datetime(2026, 6, 11, 3, 0))      # 05:10 前 ⇒ 講的是昨晚那一場
chk("  畫面只有八條、沒有 usml", list(st["lanes"]), list(S.LANES))
chk("  今天那一格讀的是自己的列（⛔ 不是夜盤順勢那條）", st["lanes"]["tsm"]["today"].get("row", {}).get("points"),
    r["points"])

print("\n=== ⑥ 內頁那張圖 ===")
S.MIN1_CSV = TMP / "bars.csv"
BARS.to_csv(S.MIN1_CSV, index=False)
S._CSV.update(key=None, df=None)
_c = S.day_chart("tsm", str(E), _rows)
chk("  是夜盤那張圖", _c["session"], "night")
_k = {m["kind"]: m for m in _c["marks"]}
chk("  進場標記在 21:35", _k.get("entry", {}).get("at"), "21:35")
say("exit" in _k, "  有出場標記")
say({"停利", "停損"} <= {ln["label"] for ln in _c["lines"]}, "  有停利與停損線")

print("\n=== ⑦ 1 分 K 存回本機（2026-09-22：⛔ 不要再用完就丟）===")
S.MIN1_CSV = TMP / "store.csv"
_old = pd.DataFrame({"ts": ["2026-09-16 23:59:00"], "Open": [1], "High": [1],
                     "Low": [1], "Close": [1], "Volume": [1], "Amount": [1]})
_old.to_csv(S.MIN1_CSV, index=False)
_new = pd.DataFrame({"ts": pd.to_datetime(["2026-09-17 15:02:00", "2026-09-16 23:59:00",
                                           "2026-09-17 15:01:00"]),
                     "Open": [9, 9, 9], "High": [9, 9, 9], "Low": [9, 9, 9],
                     "Close": [9, 9, 9], "Volume": [9, 9, 9], "Amount": [9, 9, 9]})
chk("  新的存得進去（重複的不算）", S.min1_store(_new), (2, None))
_got = pd.read_csv(S.MIN1_CSV)
chk("  欄位沒被弄壞", list(_got.columns), S.MIN1_COLS)
chk("  ⛔ 依時間排序（night_frame 假設舊到新）", list(_got["ts"]), sorted(_got["ts"]))
chk("  ⛔ 舊的那一根不被新的蓋掉",
    float(_got[_got["ts"] == "2026-09-16 23:59:00"]["Close"].iloc[0]), 1.0)
chk("  再存一次不會重複長大", S.min1_store(_new)[0], 0)
S.min1_store(pd.DataFrame({"ts": pd.to_datetime(["2026-09-17 15:03:00"]),
                           "High": [8], "Low": [8], "Close": [8]}))
chk("  只有 H/L/C 的也存得進去、格式不壞",
    list(pd.read_csv(S.MIN1_CSV).columns), S.MIN1_COLS)
chk("  ⛔ 空的不會丟例外", S.min1_store(None), (0, None))

print("\n=== ⑧ ⛔ 收尾 ===")
say(S.SIM_DIR != REAL_SIM and str(TMP) in str(S.SIM_DIR), "  全程在暫存區", str(S.SIM_DIR))
say(not (HERE / "sim_lanes" / "2026-06.jsonl").exists()
    or '"2026-06-10"' not in (HERE / "sim_lanes" / "2026-06.jsonl").read_text(encoding="utf-8")
    or '"tsm"' not in (HERE / "sim_lanes" / "2026-06.jsonl").read_text(encoding="utf-8"),
    "  ⛔ 沒有把測試資料寫進正式的 sim_lanes/")
say("tsm" in S.LANES and "usml" not in S.LANES and S.LANE_NAME["tsm"] == "台積電快攻",
    "  LANES 裡是台積電快攻、沒有 usml")

shutil.rmtree(TMP, ignore_errors=True)
print("\n" + ("全部通過 ✅" if FAIL == 0 else f"⛔ 有 {FAIL} 項沒過"))
sys.exit(1 if FAIL else 0)
