# -*- coding: utf-8 -*-
"""
【模擬】第九條線「夜盤跟勢」的探針（2026-09-23）。

⛔ 在守的事：
  ① **不偷看未來**：訊號只用「進場那一根」與「30 分鐘前那一根」；改掉進場之後的價，決定與進場價不變
  ② 缺什麼就講什麼：K 棒沒到齊／歷史不夠／不夠兇 ⇒ 各自的理由，⛔ 不猜
  ③ 門檻＝過去 40 晚第 80 百分位；⛔ 今晚自己的走幅不准進自己的門檻
  ④ 出場：⛔ 沒有停利停損，一律 04:58 收盤平
  ⑤ 落地：寫得進去、讀得回來、同一晚不重複
  ⑥ 內頁那張圖是夜盤圖、有進出場標記
  ⑦ ⛔ 不碰正式的 sim_lanes/、⛔ 不連永豐、⛔ 不連任何外部行情
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
S.TREND_CTX = TMP / "trend_ctx.json"


TMP_FIX = str(TMP)
S.SIM_DIR = TMP / "sim_lanes"

# ── 造一晚合成的 1 分 K（⛔ 假價，12000 附近）
E = date(2026, 6, 10)            # 夏令 ⇒ 美股開盤 21:30、進場 21:40
T = S.us_open_min(E)
base = 12000.0


def make_bars(E, slope=1.0, after=None):
    """slope：進場前 30 分鐘每分鐘走幾點（正＝往上）。after：進場之後要不要改價。"""
    rows = []
    d0 = pd.Timestamp(E)
    for k in range(15 * 60 + 1, 24 * 60):
        if k <= T + 10 - 30:
            px = base
        elif k <= T + 10:
            px = base + (k - (T + 10 - 30)) * slope
        else:
            px = base + 30 * slope
            if after:
                px = after(k, px)
        rows.append({"ts": d0 + pd.Timedelta(minutes=k), "Open": px, "High": px + 1,
                     "Low": px - 1, "Close": px, "Volume": 1, "Amount": 1})
    for k in range(0, 5 * 60 + 1):
        px = base + 30 * slope + 50
        if after:
            px = after(k + 1440, px)
        rows.append({"ts": pd.Timestamp(E + timedelta(days=1)) + pd.Timedelta(minutes=k), "Open": px,
                     "High": px + 1, "Low": px - 1, "Close": px, "Volume": 1, "Amount": 1})
    return pd.DataFrame(rows)


BARS = make_bars(E)                      # 前 30 分走 +30 點
CTX = {"sig": {str(E - timedelta(days=x)): (10.0 if x % 2 else -8.0) for x in range(1, 45)}}

print("=== ① 訊號與進場（⛔ 不偷看未來）===")
r = S.trend_eval(E, BARS, CTX)
chk("  走 +30 點 ≥ 門檻 ⇒ 做多", r.get("decision"), "做多")
chk("  進場那一根的標籤 ＝ 21:40（美股開盤+10）", r.get("c_label"), "21:40")
chk("  訊號起點標籤 ＝ 21:10（30 分鐘前）", r.get("ref_label"), "21:10")
chk("  走幅 ＝ +30 點", r.get("move"), 30.0)
mm, H, L, C = S.night_frame(BARS, E)
chk("  進場價 ＝ 21:40 那根的收盤", r.get("entry"), round(float(C[mm == T + 10][0]), 1))
r2 = S.trend_eval(E, make_bars(E, after=lambda k, px: px * 3), CTX)
chk("  ⛔ 把進場之後的價全部改掉，決定與進場價不變",
    (r2.get("decision"), r2.get("entry"), r2.get("move")), (r["decision"], r["entry"], r["move"]))
rdn = S.trend_eval(E, make_bars(E, slope=-1.0), CTX)
chk("  走 −30 點 ⇒ 做空", rdn.get("decision"), "做空")
gap = BARS[pd.to_datetime(BARS["ts"]) != pd.Timestamp(E) + pd.Timedelta(minutes=T + 10)]
chk("  21:40 那分鐘沒成交 ⇒ 往回拿 21:39（⛔ 不往後拿）", S.trend_eval(E, gap, CTX).get("c_label"), "21:39")

print("\n=== ② 缺什麼就講什麼 ===")
short = BARS[pd.to_datetime(BARS["ts"]) < pd.Timestamp(E) + pd.Timedelta(hours=23)]
chk("  K 棒沒到 04:58 ⇒ pending incomplete", S.trend_eval(E, short, CTX).get("why"), "incomplete")
chk("  歷史不夠 ⇒ 不做、no_hist", S.trend_eval(E, BARS, {"sig": {}}).get("why"), "no_hist")
flat = S.trend_eval(E, make_bars(E, slope=0.05), CTX)
chk("  走太少 ⇒ 不做、not_fast", flat.get("why"), "not_fast")
say("門檻" in (flat.get("reason") or ""), "  不做那句話講得出門檻", flat.get("reason"))

print("\n=== ③ 門檻只用過去 ===")
thr = float(np.percentile(np.abs([10.0, -8.0] * 20), 80))
chk("  門檻 ＝ 過去 40 晚 |走幅| 的第 80 百分位", r.get("thr_points"), round(thr, 1))
ctx2 = {"sig": dict(CTX["sig"], **{str(E): 999.0, str(E + timedelta(days=1)): 999.0})}
chk("  ⛔ 今晚與之後的走幅不影響今晚的門檻", S.trend_eval(E, BARS, ctx2).get("thr_points"), r.get("thr_points"))
chk("  記進歷史的走幅 ＝ 這一晚的走幅", S.trend_sig(E, BARS), 30.0)

print("\n=== ④ 出場：沒有停利停損、一律 04:58 ===")
chk("  出場原因＝收盤", r.get("exit_reason"), "收盤")
chk("  出場標籤 04:58", r.get("exit_label"), "04:58")
say("tpsl_points" not in r and "sl_points" not in r, "  ⛔ 這一列沒有停利停損欄位（畫面才不會畫出假的框）")
spike = S.trend_eval(E, make_bars(E, after=lambda k, px: px - 900 if k == T + 60 else px), CTX)
chk("  ⛔ 中途大跌也不停損（照規則抱到收盤）", spike.get("exit_reason"), "收盤")
chk("  點數 ＝ 方向 ×（出場 − 進場）− 成本",
    r.get("points"), round(float(C[-1]) - r["entry"] - r["cost"], 1))

print("\n=== ⑤ 落地 ===")
say(S.append_row(dict(r, calc="backfill")), "  定論寫得進去")
say(not S.append_row(dict(r, calc="backfill")), "  ⛔ 同一晚不會重複寫")
_rows, _st = S.read_rows()
back = _rows.get(("trend", str(E)))
say(back is not None and back["points"] == r["points"], "  讀得回來、點數一樣")
st = S.state(now=datetime(2026, 6, 11, 3, 0))
chk("  畫面六條：多方聯軍、台積電快攻、夜盤跟勢＋三條推導（2026-09-23 深夜）", list(st["lanes"]), ["union", "tsm", "trend", "hold", "tlong", "nunion"])
chk("  今天那一格讀的是自己的列", st["lanes"]["trend"]["today"].get("row", {}).get("points"), r["points"])
say(bool(st["lanes"]["trend"]["rule"]) and "沒有接上" not in st["lanes"]["trend"]["rule"], "  有規則句")
_d = S.lane_detail("trend", now=datetime(2026, 6, 11, 3, 0))
say(bool(_d["detail"]["plain"]) and len(_d["detail"]["steps"]) >= 6, "  內頁有白話一句＋逐條說明")

print("\n=== ⑥ 內頁那張圖 ===")
S.MIN1_CSV = TMP / "bars.csv"
BARS.to_csv(S.MIN1_CSV, index=False)
S._CSV.update(key=None, df=None)
_c = S.day_chart("trend", str(E), _rows)
chk("  是夜盤那張圖", _c["session"], "night")
_k = {m["kind"]: m for m in _c["marks"]}
chk("  進場標記在 21:40", _k.get("entry", {}).get("at"), "21:40")
say("exit" in _k, "  有出場標記")
say(not any(l["label"] in ("停利", "停損") for l in _c["lines"]), "  ⛔ 沒有停利停損線（這一條本來就沒有）")

print("\n=== ⑦ ⛔ 收尾 ===")
say(S.SIM_DIR != REAL_SIM and TMP_FIX in str(S.SIM_DIR), "  全程在暫存區", str(S.SIM_DIR))
say(not (HERE / "sim_lanes" / "2026-06.jsonl").exists()
    or '"trend"' not in (HERE / "sim_lanes" / "2026-06.jsonl").read_text(encoding="utf-8"),
    "  ⛔ 沒有把測試資料寫進正式的 sim_lanes/")
say("trend" in S.LANES and S.LANE_NAME["trend"] == "夜盤跟勢" and "trend" in S.SHOWN_LANES,
    "  LANES／畫面都註冊好了")

shutil.rmtree(TMP, ignore_errors=True)
print("\n" + ("全部通過 ✅" if FAIL == 0 else f"⛔ 有 {FAIL} 項沒過"))
sys.exit(1 if FAIL else 0)
