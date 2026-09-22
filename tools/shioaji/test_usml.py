# -*- coding: utf-8 -*-
"""
【模擬】第八條線「美股開盤模型」的探針（2026-09-22）。

⛔ 在守的五件事：
  ① **不偷看未來**：特徵只用到 T+5 為止的價 —— 把 T+5 之後的 K 棒全部改掉，特徵要一模一樣
  ② **缺什麼就講什麼**：沒有模型／沒有 SPY／沒有前一日收盤／K 棒沒到齊 ⇒ 各自的理由，
     ⛔ 不准偷偷用舊值或猜
  ③ **停損**：碰到 0.3% 就算停損；同一根兩邊都碰 ⇒ 算停損（保守）
  ④ **每一列都要有機率**（之後要用前瞻資料挑門檻，少記就白做了）
  ⑤ ⛔ 不碰正式的 sim_lanes/ 目錄、⛔ 不連永豐、⛔ 不送任何單

怎麼跑（PowerShell）：
    $env:PYTHONIOENCODING="utf-8"; $env:PYTHONUTF8="1"
    & "…\\trade-log\\.venv\\Scripts\\python.exe" "…\\tools\\shioaji\\test_usml.py"
"""
import json
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.stdout.reconfigure(encoding="utf-8")

import sim_lanes as S  # noqa: E402
import usml  # noqa: E402

FAIL = 0
REAL_SIM = S.SIM_DIR
REAL_CTX = S.USML_CTX


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

TMP = Path(tempfile.mkdtemp(prefix="usml-test-"))
S.SIM_DIR = TMP / "sim_lanes"
S.USML_CTX = TMP / "usml_ctx.json"

# ── 造一晚合成的 1 分 K（⛔ 假價，12000 附近；跟其他治具同一條規矩）
E = date(2026, 6, 10)            # 夏令 ⇒ 美股開盤 21:30
T = usml.us_open_min(E)
rows = []
base = 12000.0


def bar(d, mm, px, hi=None, lo=None):
    h = (d.hour, d.minute)
    rows.append({"ts": pd.Timestamp(d), "Open": px, "High": hi if hi else px + 2,
                 "Low": lo if lo else px - 2, "Close": px, "Volume": 10, "Amount": 1})


d0 = pd.Timestamp(E)
for k in range(8 * 60 + 46, 13 * 60 + 46):       # 日盤（給 day_close）
    bar(d0 + pd.Timedelta(minutes=k), k, base + (k % 7))
for k in range(15 * 60 + 1, 24 * 60):            # 當晚
    px = base + (5 if k >= T + 5 else 0)          # T+5 之後墊高 5 點
    bar(d0 + pd.Timedelta(minutes=k), k, px)
for k in range(0, 5 * 60 + 1):                   # 隔天清晨
    bar(pd.Timestamp(E + timedelta(days=1)) + pd.Timedelta(minutes=k), k, base + 5)
BARS = pd.DataFrame(rows)

CTX = {"day_close": {str(E - timedelta(days=x)): base for x in range(1, 4)},
       "night_range": {str(E - timedelta(days=x)): 0.5 for x in range(1, 25)}}

print("=== ① ⛔ 不偷看未來 ===")
spy = {T - 5: 500.0, T: 500.0, T + 5: 501.0, T + 30: 505.0, T + 35: 510.0}
tx = {int(k): float(v) for k, v in zip(*[S.night_frame(BARS, E)[0], S.night_frame(BARS, E)[3]])}
f1, en1, _ = usml.features(E, tx, spy, base, base, 0.5)
tx2 = dict(tx)
for k in list(tx2):
    if k > T + 5:
        tx2[k] = tx2[k] * 3          # ⛔ 把未來全部改掉
spy2 = dict(spy)
spy2[T + 30] = 9999.0
spy2[T + 35] = 9999.0
f2, en2, _ = usml.features(E, tx2, spy2, base, base, 0.5)
say(f1 == f2 and en1 == en2, "  改掉 T+5 之後的所有價格，特徵與進場價完全不變")
say(f1 is not None and abs(f1["spy5"] - 0.2) < 1e-9, "  SPY 那 5 分鐘走幅算得對", str(f1 and f1["spy5"]))

print("\n=== ② ⛔ 缺什麼就講什麼（不准偷偷用舊值）===")
r = S.usml_eval(E, BARS, None, CTX)
chk("  沒有 SPY ⇒ pending no_spy", (r.get("pending"), r.get("why")), (True, "no_spy"))
r = S.usml_eval(E, BARS, spy, {"day_close": {}, "night_range": {}})
chk("  沒有前一日收盤／波動歷史 ⇒ pending no_ctx", (r.get("pending"), r.get("why")),
    (True, "no_ctx"))
short = BARS[pd.to_datetime(BARS["ts"]) < pd.Timestamp(E) + pd.Timedelta(minutes=T + 10)]
r = S.usml_eval(E, short, spy, CTX)
chk("  K 棒還沒到齊 ⇒ pending incomplete", (r.get("pending"), r.get("why")),
    (True, "incomplete"))
_real = usml.MODEL_PATH
usml.MODEL_PATH = TMP / "nope.json"
r = S.usml_eval(E, BARS, spy, CTX)
chk("  沒有模型檔 ⇒ 不做、理由 no_model", (r.get("decision"), r.get("why")), ("不做", "no_model"))
usml.MODEL_PATH = _real

print("\n=== ③ 停損（0.3%）===")
r = S.usml_eval(E, BARS, spy, CTX)
say(r.get("decision") in ("做多", "做空"), "  正常情況算得出定論", str(r.get("reason"))[:48])
d = 1 if r["decision"] == "做多" else -1
entry = r["entry"]
hit = BARS.copy()
mask = (pd.to_datetime(hit["ts"]) > pd.Timestamp(E) + pd.Timedelta(minutes=T + 5)) & \
       (pd.to_datetime(hit["ts"]) <= pd.Timestamp(E) + pd.Timedelta(minutes=T + 35))
hit.loc[mask, "Low"] = entry * (1 - 0.01) if d > 0 else hit.loc[mask, "Low"]
hit.loc[mask, "High"] = entry * (1 + 0.01) if d < 0 else hit.loc[mask, "High"]
r2 = S.usml_eval(E, hit, spy, CTX)
chk("  碰到就算停損", r2.get("exit_reason"), "停損")
say(abs(r2["points"] - (-entry * S.USML_STOP_FRAC - r2["cost"])) < 0.6,
    "  停損的點數 ＝ −0.3% − 成本", "%.1f" % r2["points"])
both = BARS.copy()
both.loc[mask, "Low"] = entry * 0.99
both.loc[mask, "High"] = entry * 1.01
r3 = S.usml_eval(E, both, spy, CTX)
chk("  ⛔ 同一根兩邊都碰 ⇒ 算停損（保守）", r3.get("exit_reason"), "停損")

print("\n=== ④ 每一列都要有機率 ===")
say(isinstance(r.get("prob"), float) and 0 <= r["prob"] <= 1, "  有 prob 欄位", str(r.get("prob")))
say(r.get("model_through"), "  有記模型訓練到哪個月（以後才知道那一列是誰算的）",
    str(r.get("model_through")))
say(isinstance(r.get("feat"), dict) and len(r["feat"]) == len(usml.FEATURES),
    "  13 個特徵都存下來了（事後要重算得出來）")
say((r["decision"] == "做多") == (r["prob"] > 0.5), "  方向跟機率一致")

print("\n=== ⑤ ⛔ 收尾 ===")
say(S.SIM_DIR != REAL_SIM and str(TMP) in str(S.SIM_DIR), "  全程在暫存區", str(S.SIM_DIR))
say(not (HERE / "sim_lanes" / ("%s.jsonl" % str(E)[:7])).exists()
    or not any(str(E) in ln and '"usml"' in ln
               for ln in (HERE / "sim_lanes" / ("%s.jsonl" % str(E)[:7])).read_text(
                   encoding="utf-8").splitlines()),
    "  ⛔ 沒有把測試資料寫進正式的 sim_lanes/")
say("usml" in S.LANES and S.LANE_NAME["usml"] == "美股開盤模型", "  這一條有註冊在 LANES 裡")

shutil.rmtree(TMP, ignore_errors=True)
print("\n" + ("全部通過 ✅" if FAIL == 0 else f"⛔ 有 {FAIL} 項沒過"))
sys.exit(1 if FAIL else 0)
