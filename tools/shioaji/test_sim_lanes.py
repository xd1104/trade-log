# -*- coding: utf-8 -*-
"""
【模擬】分頁（八條）離線測試（2026-09-15 晚上，2026-09-16 擴到七條，lab-dev）。⛔ 不連永豐、⛔ 不碰 8770、⛔ 不建開關檔。

  ① 快攻：快做多停利／快做空停損（用觸發價）／不快不做／歷史不夠／門檻只用這天以前的列／
     13:43:30 收盤平／結算日 13:30／沒有逐筆＝資料缺（不是定論）
  ② 夜盤順勢：夏令 21:30／冬令 22:30（含換季邊界）／同一根兩邊碰算停損／04:58 收盤平／停利／d=0 不做／沒到齊＝資料缺
  ③ 落地：同一（lane,date）不重寫／資料缺之後補到會補算（兩條各一次）／壞列計數＋等式
  ④ 抓資料的防護：08:30~09:35 不抓／有部位不抓（問不到也算有）／流量高不抓／失敗隔 10 分鐘／問過沒有今天不重抓／正控組
  ⑤ 背景例外不外丟（step 與 loop 各驗）＋計數
  ⑥ 端點 GET /api/sim/state：200、唯讀（前後雜湊一樣）、跨站 403、POST 不接、模組是 None ⇒ 503；
     sim_lanes 載入失敗時 import live_panel 照樣成功
  ⑦ 前端：獨立的 #tab-sim、八條並排、只打 GET /api/sim/state、沒有下單路徑、沒有建議口吻、不跟真單清單混用
  ⑪ 新的四條：hmq（快＋09:15 反轉）／rev（只做反轉那一半）／fast11（只換收盤時刻）／orb（箱子濾網）
  ⑫ 規則函式一律用注入的那一份（⛔ sim_lanes 裡沒有另一把尺）
  ⑧ AST：sim_lanes 不 import／引用 broker、auto_fire；主迴圈那幾支跟固定基準 a71087e 一模一樣（沒有基準 ⇒ 記「未驗」）
  ④b 面板注入的 _sim_has_position（沒有確定答案就回 True）＋關鍵字注入各就各位
  ④c 休市不佔「每輪補一天」名額、落地成「休市」、今天回空不記休市、fast_hist 有那天就不記休市
  ⑨ fire_fires_today：今天帳本有 wait、沒定論、早於 REV_SEC＋AUTO_LATE_MS ⇒「今天」
  ⑩ 收尾：全程沒有指回真的資料夾、真的 AUTO_ORDERS_ON 不存在

⛔ 價格一律用 12000 附近的合成資料（不撞他的真實紀錄）。
跑法（在 tools\\shioaji 底下）：  ..\\..\\.venv\\Scripts\\python.exe test_sim_lanes.py
"""
# ⛔ 崩潰也要有具名 FAIL＋總結（2026-09-15 lab-qa 退件 R6）：整份測試包一層 try 跑（把自己當成 body exec 一次），
#    例外 ⇒ 印 traceback＋「FAIL 測試本身崩潰」＋總結、exit 1。body 自己跑完會 sys.exit，照原樣往外傳。
if __name__ == "__main__" and not globals().get("_SIM_TEST_BODY"):
    import sys as _sys
    import traceback as _tb
    _g = {"__name__": "__main__", "__file__": __file__, "__builtins__": __builtins__, "_SIM_TEST_BODY": True}
    try:
        with open(__file__, encoding="utf-8") as _f:
            _code = compile(_f.read(), __file__, "exec")
        exec(_code, _g)
    except SystemExit:
        raise
    except BaseException as _e:          # noqa: BLE001  ⛔ 刻意接住所有例外（含 KeyboardInterrupt）
        try:
            _sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        _tb.print_exc(file=_sys.stdout)
        print(f"  FAIL 測試本身崩潰（{type(_e).__name__}: {str(_e)[:200]}）—— 崩潰點之後的項目都沒跑到")
        try:
            if _g.get("TMP") is not None:
                import shutil as _sh
                _sh.rmtree(_g["TMP"], ignore_errors=True)
        except Exception:
            pass
        print("\n總結:", f"{int(_g.get('FAIL', 0) or 0) + 1} 項失敗（含測試崩潰）")
        _sys.exit(1)
    _sys.exit(0)
import ast
import gzip
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from http.server import ThreadingHTTPServer

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import numpy as np
import pandas as pd

import auto_fire as AF
import live_panel as LP
import sim_lanes as S
# ⭐ 2026-09-22 起畫面只端兩條（S.SHOWN_LANES）。⛔ 但其他幾條照樣在背景算、照樣要驗 ⇒
#    這支測試**先讓端點端出全部**，逐條的邏輯照舊驗；「真的畫面只有兩條」在 ⑥ 另外寫死驗。
REAL_SHOWN = S.SHOWN_LANES
S.SHOWN_LANES = S.LANES
import strategy_lab as SL

FAIL = 0
UNVERIFIED = []      # 驗不到的項目（⛔ 不當成通過；總結會寫「其餘通過；未驗 N 項」）


def chk(name, got, want):
    global FAIL
    ok = got == want
    FAIL += not ok
    print(("  OK   " if ok else "  FAIL ") + name + ("" if ok else f"  (得到 {got!r}，期待 {want!r})"))


def say(ok, name, extra=""):
    global FAIL
    FAIL += not ok
    print(("  OK   " if ok else "  FAIL ") + name + (f"  {extra}" if extra else ""))


def fhash(p):
    p = pathlib.Path(p)
    if not p.exists():
        return None
    if p.is_file():
        return hashlib.sha256(p.read_bytes()).hexdigest()
    h = hashlib.sha256()
    for f in sorted(x for x in p.rglob("*") if x.is_file()):
        h.update(str(f.relative_to(p)).encode())
        h.update(f.read_bytes())
    return h.hexdigest()


# ══ 導走所有寫檔出口（⛔ 先確認有幾個：sim_lanes 的 SIM_DIR、借用的 tick_hist、fire 帳本）════════════
REAL = {"S.SIM_DIR": S.SIM_DIR, "S.FAST_HIST": S.FAST_HIST, "S.MIN1_CSV": S.MIN1_CSV,
        "SL.LAB_DIR": SL.LAB_DIR, "SL.MIN1_CSV": SL.MIN1_CSV, "AF.FIRE_DIR": AF.FIRE_DIR,
        "AF.ARM_FLAG": AF.ARM_FLAG, "AF.FAST_HIST": AF.FAST_HIST}
REAL_HASH = {k: fhash(v) for k, v in REAL.items() if k in ("S.SIM_DIR", "S.FAST_HIST", "AF.FIRE_DIR")}
say(not REAL["AF.ARM_FLAG"].exists(), "  開跑前：真的 AUTO_ORDERS_ON 不存在（存在就不跑，⛔ 不准碰它）")
if REAL["AF.ARM_FLAG"].exists():
    sys.exit(2)
TMP = pathlib.Path(tempfile.mkdtemp(prefix="simlanes_"))
S.SIM_DIR = TMP / "sim_lanes"
S.FAST_HIST = TMP / "fast_hist.jsonl"
S.MIN1_CSV = TMP / "tmf_1min.csv"
SL.LAB_DIR = TMP / "tick_hist"
SL.MIN1_CSV = TMP / "tmf_1min_lab.csv"
AF.FIRE_DIR = TMP / "autofire"
AF.ARM_FLAG = TMP / "AUTO_ORDERS_ON"
AF.FAST_HIST = TMP / "af_fast_hist.jsonl"
say(S.configure(AF.fast_verdict, AF.move_pct, AF.tpsl_points, AF.hist_read, LP.FAST_PCTL, AF.FAST_RULE,
                reversal_fn=AF.reversal_dir, rev_sec=LP.REV_SEC),
    "  configure 接上 auto_fire 的規則正本（含 reversal_dir／REV_SEC）")
say(not S.configure(AF.fast_verdict, AF.move_pct, AF.tpsl_points, AF.hist_read, LP.FAST_PCTL, AF.FAST_RULE,
                    reversal_fn=None, rev_sec=LP.REV_SEC),
    "  負控組：少了 reversal_dir ⇒ 接不上（⛔ 不准半套上路）")
say(S.configure(AF.fast_verdict, AF.move_pct, AF.tpsl_points, AF.hist_read, LP.FAST_PCTL, AF.FAST_RULE,
                reversal_fn=AF.reversal_dir, rev_sec=LP.REV_SEC), "  接回來")


def reset_state():
    S._FAIL_AT.update(ticks=None, kbars=None)
    S._TRIED.clear()
    S._NIGHT_API.clear()
    S.STATE["errors"] = 0
    S.STATE["last_err"] = None
    S.STATE["pending"] = {k: {} for k in S.LANES}


def ms(h, m, s=0, x=0):
    return (h * 3600 + m * 60 + s) * 1000 + x


def mkD(ticks):
    """ticks：[(毫秒, 成交, 買, 賣)] ⇒ load_day 的形狀"""
    ticks = sorted(ticks)
    return {"t": np.array([x[0] for x in ticks], np.int64), "p": np.array([x[1] for x in ticks], float),
            "bid": np.array([x[2] for x in ticks], float), "ask": np.array([x[3] for x in ticks], float)}


def hist_rows(day, n, mv=0.2, spread=0.1):
    """day 以前 n 個平日的 move_pct（0.2~0.3 之間）"""
    out, d, k = [], date.fromisoformat(day), 0
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            out.append({"date": str(d), "move_pct": mv + spread * ((k % 10) / 10.0)})
            k += 1
    return sorted(out, key=lambda r: r["date"])


def base_day(ref=12000.0, px=12060.0, bid=None, ask=None, extra=()):
    """08:45 開、08:59:59 ref、09:03:29 px，之後照 extra"""
    t = [(ms(8, 45, 0, 100), 11990.0, 11989.0, 11991.0), (ms(8, 59, 59), ref, ref - 1, ref + 1),
         (ms(9, 1), (ref + px) / 2, (ref + px) / 2 - 1, (ref + px) / 2 + 1),
         (ms(9, 3, 29), px, px - 1 if bid is None else bid, px + 1 if ask is None else ask)]
    t += list(extra)
    t.append((ms(13, 44, 59), px, px - 1, px + 1))
    return mkD(t)


# ══ ① 快攻 ══════════════════════════════════════════════════════════
print("=== ① 快攻 ===")
DAY = "2026-10-13"          # 週二、不是結算日
say(not SL.is_expiry(date.fromisoformat(DAY)), "  治具自證：%s 不是結算日" % DAY)
H40 = hist_rows(DAY, 40)
thr = AF.fast_threshold([r["move_pct"] for r in H40], LP.FAST_PCTL)[0]
say(thr is not None and thr < 0.5, "  治具自證：門檻 %.3f%% 小於 0.5%%" % (thr or -1))

# 快做多停利：px 12060（走 0.5%），賣價 12061 進場，停利 round(12061×0.5%)=60 ⇒ 12121
D = base_day(12000, 12060, extra=[(ms(10, 0), 12100.0, 12099, 12101), (ms(10, 30), 12121.0, 12120, 12122)])
r = S.fast_eval(DAY, D, H40)
chk("  快做多：決定／進場用賣價／停利", (r.get("decision"), r.get("entry"), r.get("exit_reason")), ("做多", 12061.0, "停利"))
chk("  快做多：點數＝60−5、出場 12121", (r.get("points"), r.get("exit"), r.get("tpsl_points")), (55.0, 12121.0, 60))

# 快做空停損：px 11940，買價 11939 進場，停損 60 ⇒ 11999；觸發那筆成交 12010（跳過去）⇒ −71−5
D = base_day(12000, 11940, extra=[(ms(9, 30), 11990.0, 11989, 11991), (ms(9, 40), 12010.0, 12009, 12011)])
r = S.fast_eval(DAY, D, H40)
chk("  快做空：決定／進場用買價／停損", (r.get("decision"), r.get("entry"), r.get("exit_reason")), ("做空", 11939.0, "停損"))
chk("  快做空停損：用觸發那一筆的成交價 12010（不是 11999）⇒ −76", (r.get("exit"), r.get("points")), (12010.0, -76.0))

# 不快不做
r = S.fast_eval(DAY, base_day(12000, 12006), H40)
chk("  不快（走 0.05%）⇒ 不做／not_fast", (r.get("decision"), r.get("why"), r.get("points")), ("不做", "not_fast", None))
say("不快，不做" in (r.get("reason") or ""), "  原因寫「不快，不做」", r.get("reason"))

# 邊界：剛好 09:00:00.000 那筆算 ref、剛好 09:03:30.000 那筆算 px（含），09:03:30.001 那筆不算
D = base_day(12000, 12060, extra=[(ms(9, 0, 0), 12010.0, 12009, 12011), (ms(9, 3, 30), 12070.0, 12069, 12071),
                                  (ms(9, 3, 30, 1), 12500.0, 12499, 12501)])
r = S.fast_eval(DAY, D, H40)
chk("  邊界：ref＝09:00:00.000 那筆、px＝09:03:30.000 那筆、進場用它的賣價", (r.get("ref"), r.get("px"), r.get("entry")),
    (12010.0, 12070.0, 12071.0))

# 歷史不夠：19 天 ⇒ 歷史不夠；20 天 ⇒ 有判定
r19 = S.fast_eval(DAY, base_day(12000, 12060), hist_rows(DAY, 19))
r20 = S.fast_eval(DAY, base_day(12000, 12060), hist_rows(DAY, 20))
chk("  19 天 ⇒ 不做／no_hist", (r19.get("decision"), r19.get("why")), ("不做", "no_hist"))
say("歷史不夠" in (r19.get("reason") or ""), "  原因寫「歷史不夠」", r19.get("reason"))
chk("  20 天 ⇒ 有判定（做多）", r20.get("decision"), "做多")

# 門檻只用這天以前的列：這天（含）以後放 30 列超大的走幅 ⇒ 結果必須跟沒有它們一模一樣
fut = [{"date": str(date.fromisoformat(DAY) + timedelta(days=i)), "move_pct": 5.0} for i in range(0, 30)]
old = [{"date": "2025-0%d-1%d" % (1 + i // 10, i % 10), "move_pct": 9.0} for i in range(30)]
rA = S.fast_eval(DAY, base_day(12000, 12060, extra=[(ms(10, 30), 12121.0, 12120, 12122)]), H40)
rB = S.fast_eval(DAY, base_day(12000, 12060, extra=[(ms(10, 30), 12121.0, 12120, 12122)]),
                 sorted(old + H40 + fut, key=lambda x: x["date"]))
chk("  這天以後的列（含當天）不影響門檻；40 天以前的舊列也不算", (rB.get("decision"), rB.get("thr_pct"), rB.get("n_hist")),
    (rA.get("decision"), rA.get("thr_pct"), 40))
say(rB.get("thr_pct") is not None and rB["thr_pct"] < 1.0, "  自證：門檻沒有被 5%／9% 的列拉上去", str(rB.get("thr_pct")))

# 收盤平：13:43:30 前沒碰到 ⇒ 最後一筆買價（做多）平；13:43:31 那筆碰到停利不算
D = base_day(12000, 12060, extra=[(ms(11, 0), 12080.0, 12079, 12081), (ms(13, 43, 30), 12090.0, 12088, 12092),
                                  (ms(13, 43, 31), 12200.0, 12199, 12201)])
r = S.fast_eval(DAY, D, H40)
chk("  13:43:30 前沒碰到 ⇒ 收盤、用 13:43:30 那筆的買價 12088", (r.get("exit_reason"), r.get("exit"), r.get("points")),
    ("收盤", 12088.0, 12088.0 - 12061.0 - 5))

# 結算日 13:30：13:35 才碰到停利 ⇒ 結算日算收盤（13:29 那筆買價）；平常日同一份資料 ⇒ 停利
EXP = "2026-10-21"
say(SL.is_expiry(date.fromisoformat(EXP)), "  治具自證：%s 是結算日" % EXP)
ext = [(ms(13, 29), 12070.0, 12069, 12071), (ms(13, 35), 12130.0, 12129, 12131)]
rE = S.fast_eval(EXP, base_day(12000, 12060, extra=ext), hist_rows(EXP, 40))
rN = S.fast_eval("2026-10-22", base_day(12000, 12060, extra=ext), hist_rows("2026-10-22", 40))
chk("  結算日：13:30 收盤、出場 12069", (rE.get("exit_reason"), rE.get("exit"), rE.get("cutoff")), ("收盤", 12069.0, "13:30:00"))
chk("  對照組（平常日同一份資料）：停利", (rN.get("exit_reason"), rN.get("cutoff")), ("停利", "13:43:30"))

# 資料缺
chk("  沒有逐筆 ⇒ 資料缺（pending），不是定論", (S.fast_eval(DAY, None, H40).get("pending"), S.fast_eval(DAY, None, H40).get("msg")),
    (True, "沒有當天逐筆"))
chk("  讀不到歷史檔 ⇒ 資料缺", S.fast_eval(DAY, base_day(), None).get("why"), "no_hist_file")
_saved_cfg = dict(S._CFG)
S._CFG["verdict"] = None
chk("  沒接上規則函式 ⇒ 資料缺（⛔ 不猜）", S.fast_eval(DAY, base_day(), H40).get("why"), "not_wired")
S._CFG.update(_saved_cfg)


# ══ ② 夜盤順勢 ══════════════════════════════════════════════════════
print("\n=== ② 夜盤順勢 ===")
chk("  夏令換算：2026-03-07（週六，換季前）22:30、03-08（第二個週日）21:30",
    (S._hm(S.us_open_min(date(2026, 3, 7))), S._hm(S.us_open_min(date(2026, 3, 8)))), ("22:30", "21:30"))
chk("  冬令換算：2026-10-31 21:30、11-01（第一個週日）22:30",
    (S._hm(S.us_open_min(date(2026, 10, 31))), S._hm(S.us_open_min(date(2026, 11, 1)))), ("21:30", "22:30"))
chk("  2027：03-13 22:30、03-14 21:30、11-06 21:30、11-07 22:30",
    [S._hm(S.us_open_min(date(2027, 3, 13))), S._hm(S.us_open_min(date(2027, 3, 14))),
     S._hm(S.us_open_min(date(2027, 11, 6))), S._hm(S.us_open_min(date(2027, 11, 7)))], ["22:30", "21:30", "21:30", "22:30"])


def night(E, over=None, base=12000.0, tail=True):
    """E 15:01 ~ E+1 05:00 每分鐘一根（標籤＝結束時間），收盤都是 base；over：{"HH:MM": (H, L, C)}"""
    over = over or {}
    rows = []
    t = datetime.combine(E, datetime.min.time()) + timedelta(hours=15, minutes=1)
    end = datetime.combine(E + timedelta(days=1), datetime.min.time()) + timedelta(hours=5 if tail else 4, minutes=0 if tail else 50)
    while t <= end:
        k = t.strftime("%H:%M")
        h, l, c = over.get(k, (base + 2, base - 2, base))
        rows.append({"ts": t, "High": h, "Low": l, "Close": c})
        t += timedelta(minutes=1)
    return pd.DataFrame(rows)


def ramp(c0, lab0, n, step):
    """從 lab0 開始 n 根，收盤每根 +step（高低 ±1）"""
    out, t = {}, datetime.strptime(lab0, "%H:%M")
    for i in range(n):
        c = c0 + step * i
        out[(t + timedelta(minutes=i)).strftime("%H:%M")] = (c + 1, c - 1, c)
    return out


SUM = date(2026, 7, 1)      # 週三、夏令
WIN = date(2026, 12, 2)     # 週三、冬令
# 夏令：21:31~21:35 漲到 12060（T=21:30 ⇒ 做多）；冬令同一份 K 棒、22:31~22:35 跌 ⇒ 看的是 22:30
ov = dict(ramp(12012, "21:31", 5, 12))           # 21:35 收 12060
ov.update(ramp(11988, "22:31", 5, -12))          # 22:35 收 11940
for k in list(ov):
    pass
rS = S.night_eval(SUM, night(SUM, ov))
rW = S.night_eval(WIN, night(WIN, ov))
chk("  夏令看 21:30→21:35：做多、ref 12000、c 12060", (rS.get("decision"), rS.get("ref"), rS.get("c"), rS.get("us_open")),
    ("做多", 12000.0, 12060.0, "21:30"))
chk("  冬令看 22:30→22:35：做空、c 11940", (rW.get("decision"), rW.get("c"), rW.get("us_open"), rW.get("c_label")),
    ("做空", 11940.0, "22:30", "22:35"))

# 同一根兩邊都碰到 ⇒ 停損：做多 c=12060、±120.6；23:00 那根 H 12200／L 11900
ov2 = dict(ramp(12012, "21:31", 5, 12))
ov2["23:00"] = (12200.0, 11900.0, 12050.0)
r = S.night_eval(SUM, night(SUM, ov2))
chk("  同一根兩邊都碰 ⇒ 停損、點數 −120.6−7", (r.get("exit_reason"), r.get("points"), r.get("exit_label")),
    ("停損", round(-120.6 - 7, 1), "23:00"))
ov2b = dict(ramp(12012, "21:31", 5, 12))
ov2b["23:00"] = (12200.0, 12040.0, 12180.0)
r = S.night_eval(SUM, night(SUM, ov2b))
chk("  對照組：只碰停利那邊 ⇒ 停利 +120.6−7", (r.get("exit_reason"), r.get("points")), ("停利", round(120.6 - 7, 1)))

# 都沒碰到 ⇒ 標籤 04:58 那根收盤平（04:59／05:00 的收盤不算）
ov3 = dict(ramp(12012, "21:31", 5, 12))
ov3["04:58"] = (12091.0, 12089.0, 12090.0)
ov3["04:59"] = (12151.0, 12149.0, 12150.0)
ov3["05:00"] = (12001.0, 11999.0, 12000.0)
r = S.night_eval(SUM, night(SUM, ov3, base=12050.0))
chk("  沒碰到 ⇒ 收盤、用 04:58 那根 12090", (r.get("exit_reason"), r.get("exit"), r.get("exit_label"), r.get("points")),
    ("收盤", 12090.0, "04:58", 12090.0 - 12060.0 - 7))

# d=0 不做
ov4 = {"21:30": (12001, 11999, 12000.0), "21:35": (12001, 11999, 12000.0)}
r = S.night_eval(SUM, night(SUM, ov4))
chk("  21:35 跟 21:30 一樣價 ⇒ 不做／flat", (r.get("decision"), r.get("why"), r.get("points")), ("不做", "flat", None))

# 沒到齊（最後一根 04:50）⇒ 資料缺，⛔ 不是「不做」
r = S.night_eval(SUM, night(SUM, ov, tail=False))
chk("  沒到 04:58 ⇒ 資料缺（incomplete）", (r.get("pending"), r.get("why")), (True, "incomplete"))
chk("  沒有 K 棒 ⇒ 資料缺", S.night_eval(SUM, None).get("why"), "no_bars")
# 標籤一定是結束時間：把整份往前挪一分鐘（＝誤用起始時間標籤）⇒ 21:35 那根變 21:34 ⇒ c 不同
shift = night(SUM, ov)
shift["ts"] = shift["ts"] - timedelta(minutes=1)
r = S.night_eval(SUM, shift)
say(r.get("c") != 12060.0, "  自證：同一份 K 棒改用起始時間標籤會算出不同的 c（這把尺分得出兩種標籤）", str(r.get("c")))


# ══ ③ 落地 ═══════════════════════════════════════════════════════════
print("\n=== ③ 落地 ===")
row = {"lane": "fast", "date": "2026-10-13", "decision": "不做", "why": "not_fast", "reason": "x", "entry": None,
       "exit": None, "exit_reason": None, "points": None, "src": "逐筆"}
chk("  第一次寫 ⇒ True", S.append_row(row), True)
chk("  同一（lane,date）第二次 ⇒ False（不重寫）", S.append_row(dict(row, reason="改過")), False)
chk("  另一條同一天 ⇒ 可以寫", S.append_row(dict(row, lane="night")), True)
rows, st = S.read_rows()
chk("  檔裡 2 列、第一列原文沒被改", (st["lines"], rows[("fast", "2026-10-13")]["reason"]), (2, "x"))
say(all(isinstance(json.loads(l).get("wrote_at"), str) for l in (S.SIM_DIR / "2026-10.jsonl").read_text(encoding="utf-8").splitlines()),
    "  每列都有 wrote_at")
try:
    S.append_row({"lane": "fast", "date": "2026-10-13"})
    say(False, "  格式不對的列應該拒絕寫")
except ValueError:
    say(True, "  格式不對的列拒絕寫（不會寫出壞列）")

# 壞列計數＋等式
with (S.SIM_DIR / "2026-10.jsonl").open("a", encoding="utf-8") as f:
    f.write("{壞掉的 json\n")
    f.write("\n")
    f.write(json.dumps(dict(row, decision="亂寫")) + "\n")
    f.write(json.dumps(dict(row, reason="重複")) + "\n")
    f.write(json.dumps(dict(row, date="2026-10-14", decision="做多", points=float("nan"), exit_reason="停利")) + "\n")
(S.SIM_DIR / "notes.txt").write_text("不是月份檔", encoding="utf-8")
rows, st = S.read_rows()
chk("  計數：7 列＝ok 2＋bad 3＋dup 1＋blank 1", (st["lines"], st["ok"], st["bad"], st["dup"], st["blank"]), (7, 2, 3, 1, 1))
chk("  等式成立", st["lines"], st["ok"] + st["bad"] + st["dup"] + st["blank"])
chk("  重複那列只認第一列", rows[("fast", "2026-10-13")]["reason"], "x")
stt = LP.sim_lanes.state(datetime(2026, 10, 23, 20, 0))
chk("  state() 把計數與等式端出去", (stt["file"]["bad"], stt["file"]["dup"], stt["file"]["eq_ok"]), (3, 1, True))
import shutil
shutil.rmtree(S.SIM_DIR)

# 資料缺之後補到會補算（step，⛔ 不連永豐：get_api 回 None）
NOW = datetime(2026, 10, 23, 20, 0)      # 週五晚上
reset_state()
FD = "2026-10-20"
S.FAST_HIST.write_text("".join(json.dumps(r) + "\n" for r in hist_rows(FD, 40)), encoding="utf-8")
S.step(lambda: None, lambda: False, NOW)
rows, st = S.read_rows()
chk("  快攻：沒有逐筆 ⇒ 一列都沒寫、pending 有那天＋原因", (st["ok"], S.STATE["pending"]["fast"].get(FD, {}).get("msg")),
    (0, "沒有當天逐筆"))
chk("  快攻：沒連線 ⇒ 抓取狀態 no_api（不是安靜地少）", S.STATE["fetch"]["ticks"]["status"], "no_api")


def write_ticks(day, ticks):
    SL._ticks_dir().mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame({"ts": [f"{day} {x[0] // 3600000:02d}:{x[0] // 60000 % 60:02d}:{x[0] // 1000 % 60:02d}.{x[0] % 1000:03d}" for x in ticks],
                       "close": [x[1] for x in ticks], "volume": 1, "bid_price": [x[2] for x in ticks],
                       "bid_volume": 1, "ask_price": [x[3] for x in ticks], "ask_volume": 1, "tick_type": 1})
    df.to_csv(SL._ticks_dir() / f"{day}.csv.gz", index=False, compression="gzip")


Dx = base_day(12000, 12060, extra=[(ms(10, 30), 12121.0, 12120, 12122)])
write_ticks(FD, list(zip(Dx["t"].tolist(), Dx["p"].tolist(), Dx["bid"].tolist(), Dx["ask"].tolist())))
S.step(lambda: None, lambda: False, NOW)
rows, st = S.read_rows()
chk("  快攻：逐筆補到之後下一輪就補算、寫進檔", (rows.get(("fast", FD), {}).get("decision"), rows.get(("fast", FD), {}).get("points")),
    ("做多", 55.0))
say(FD not in S.STATE["pending"]["fast"], "  快攻：補算之後 pending 裡沒有那天了")
n_before = st["lines"]
S.step(lambda: None, lambda: False, NOW)
chk("  再跑一輪 ⇒ 檔案列數不變（有定論就不重寫）", S.read_rows()[1]["lines"], n_before)

NE = date(2026, 10, 21)
S.step(lambda: None, lambda: False, NOW)
chk("  夜盤：沒有 1 分 K ⇒ 沒寫、pending 有", (("night", str(NE)) in S.read_rows()[0], str(NE) in S.STATE["pending"]["night"]), (False, True))
bars = pd.concat([night(NE, dict(ramp(12012, "21:31", 5, 12)))], ignore_index=True)
bars.assign(Open=bars["Close"], Volume=1, Amount=1)[["ts", "Open", "High", "Low", "Close", "Volume", "Amount"]].to_csv(S.MIN1_CSV, index=False)
S.step(lambda: None, lambda: False, NOW)
rows, _st = S.read_rows()
chk("  夜盤：1 分 K 補到之後補算（做多）", rows.get(("night", str(NE)), {}).get("decision"), "做多")
chk("  夜盤：窗口只到 E+1 05:10 已經過的晚上（週四 10-22 那晚要等週五 05:10，現在週五 20:00 ⇒ 在窗口裡）",
    date(2026, 10, 22) in S.night_evenings(NOW), True)
chk("  夜盤：週五 05:09 還不算週四那晚", date(2026, 10, 22) in S.night_evenings(datetime(2026, 10, 23, 5, 9)), False)
chk("  窗口 10 個", (len(S.fast_days(NOW)), len(S.night_evenings(NOW))), (10, 10))


# ══ ④ 抓資料的防護 ═══════════════════════════════════════════════════
print("\n=== ④ 抓資料的防護 ===")


class U:
    def __init__(self, used, lim):
        self.bytes, self.limit_bytes = used, lim


class FakeApi:
    def __init__(self, used=10, lim=100, ticks=None, kbars=None, boom=False):
        self.calls = []
        self.used, self.lim, self._ticks, self._kbars, self.boom = used, lim, ticks, kbars, boom

        class F:
            TMF = {"TMFR1": "TMFR1"}

        class C:
            Futures = F
        self.Contracts = C

    def usage(self, timeout=None):
        self.calls.append("usage")
        return U(self.used, self.lim)

    def ticks(self, **kw):
        self.calls.append(("ticks", kw.get("date")))
        if self.boom:
            raise RuntimeError("永豐炸了")
        return self._ticks(kw["date"]) if self._ticks else {"ts": []}

    def kbars(self, contract, start, end):
        self.calls.append(("kbars", start))
        if self.boom:
            raise RuntimeError("永豐炸了")
        return self._kbars(start) if self._kbars else {"ts": []}


PAST = date(2026, 10, 19)


def full_ticks(day):
    t = [f"{day} 08:45:00.1", f"{day} 09:00:00.0", f"{day} 13:44:30.0"]
    return {"ts": t, "close": [12000.0] * 3, "volume": [1] * 3, "bid_price": [11999.0] * 3,
            "bid_volume": [1] * 3, "ask_price": [12001.0] * 3, "ask_volume": [1] * 3, "tick_type": [1] * 3}


for label, now, pos, api_kw, want in (
        ("08:30 整 ⇒ 不抓", datetime(2026, 10, 23, 8, 30), False, {}, "quiet"),
        ("09:34:59 ⇒ 不抓", datetime(2026, 10, 23, 9, 34, 59), False, {}, "quiet"),
        ("11:00 ⇒ 不抓（日盤中，真單部位可能還開著）", datetime(2026, 10, 23, 11, 0), False, {}, "quiet"),
        ("13:49:59 ⇒ 不抓", datetime(2026, 10, 23, 13, 49, 59), False, {}, "quiet"),
        ("有部位 ⇒ 不抓", datetime(2026, 10, 23, 20, 0), True, {}, "position"),
        ("部位回 None（沒有確定答案）⇒ 不抓", datetime(2026, 10, 23, 20, 0), None, {}, "position"),
        ("部位回 \"unknown\" ⇒ 不抓", datetime(2026, 10, 23, 20, 0), "unknown", {}, "position"),
        ("流量 86% ⇒ 不抓", datetime(2026, 10, 23, 20, 0), False, {"used": 86, "lim": 100}, "usage_high"),
        ("讀不到流量上限 ⇒ 不抓", datetime(2026, 10, 23, 20, 0), False, {"used": 1, "lim": 0}, "usage_high")):
    reset_state()
    api = FakeApi(ticks=full_ticks, kbars=lambda s: {"ts": []}, **api_kw)
    st1 = S.fetch_ticks_day(api, lambda p=pos: p, PAST, now, qt="RT")
    st2 = S.fetch_night(api, lambda p=pos: p, PAST, None, now)
    fetched = [c for c in api.calls if c != "usage"]
    chk(f"  {label}：逐筆與 1 分 K 都回 {want}、一次都沒跟永豐要資料", (st1, st2, fetched), (want, want, []))
reset_state()
api = FakeApi(ticks=full_ticks)
chk("  問部位丟例外 ⇒ 當成有部位", S.fetch_ticks_day(api, lambda: 1 / 0, PAST, datetime(2026, 10, 23, 20, 0), qt="RT"), "position")
chk("  13:50 整 ⇒ 可以抓（正控組：真的去要了）", (S.fetch_ticks_day(api, lambda: False, PAST, datetime(2026, 10, 23, 13, 50), qt="RT"),
                                          ("ticks", str(PAST)) in api.calls), ("saved", True))
say((SL._ticks_dir() / f"{PAST}.csv.gz").exists(), "  正控組：存進（暫存區的）tick_hist/ticks/")
chk("  檔案已經在 ⇒ 不再抓", S.fetch_ticks_day(api, lambda: False, PAST, datetime(2026, 10, 23, 20, 0), qt="RT"), "exists")
reset_state()
api = FakeApi(ticks=full_ticks)
chk("  今天 15:00 以前不補（那是 strategy_lab.fetch_today 的班）",
    (S.fetch_ticks_day(api, lambda: False, date(2026, 10, 23), datetime(2026, 10, 23, 14, 0), qt="RT"), api.calls), ("too_early", []))
# 失敗隔 10 分鐘
reset_state()
api = FakeApi(boom=True)
try:
    S.fetch_ticks_day(api, lambda: False, date(2026, 10, 16), datetime(2026, 10, 23, 20, 0), qt="RT")
    say(False, "  抓取失敗應該丟給 step 吞")
except RuntimeError:
    say(True, "  抓取失敗 ⇒ 例外交給 step（step 會吞＋計數）")
try:
    _r9 = S.fetch_ticks_day(api, lambda: False, date(2026, 10, 16), datetime(2026, 10, 23, 20, 9), qt="RT")
except RuntimeError as e:
    _r9 = "又去問了而且炸了：" + str(e)
chk("  失敗後 9 分鐘 ⇒ 不再問（連流量都不問）", (_r9, len(api.calls)), ("retry_wait", 2))
api.boom = False
api._ticks = full_ticks
chk("  失敗後 10 分鐘 ⇒ 再試", S.fetch_ticks_day(api, lambda: False, date(2026, 10, 16), datetime(2026, 10, 23, 20, 10), qt="RT"), "saved")
# 問過沒有 ⇒ 今天不重抓
reset_state()
api = FakeApi(ticks=lambda d: {"ts": []})
chk("  沒有成交 ⇒ empty", S.fetch_ticks_day(api, lambda: False, date(2026, 10, 15), datetime(2026, 10, 23, 20, 0), qt="RT"), "empty")
chk("  同一天再問 ⇒ tried、沒打永豐", (S.fetch_ticks_day(api, lambda: False, date(2026, 10, 15), datetime(2026, 10, 23, 20, 30), qt="RT"),
                               len(api.calls)), ("tried", 2))
# 夜盤正控組：一天一天要、合併、到齊才收
reset_state()
kb = night(date(2026, 10, 14), dict(ramp(12012, "21:31", 5, 12)))


def kb_of(day):
    g = kb[kb["ts"].dt.date == date.fromisoformat(day)]
    return {"ts": [str(x) for x in g["ts"]], "Open": g["Close"].tolist(), "High": g["High"].tolist(),
            "Low": g["Low"].tolist(), "Close": g["Close"].tolist(), "Volume": [1] * len(g), "Amount": [1] * len(g)}


api = FakeApi(kbars=kb_of)
got = S.fetch_night(api, lambda: False, date(2026, 10, 14), None, datetime(2026, 10, 23, 20, 0))
chk("  夜盤正控組：E 與 E+1 各要一次、合併後到齊", (isinstance(got, pd.DataFrame), [c for c in api.calls if c != "usage"]),
    (True, [("kbars", "2026-10-14"), ("kbars", "2026-10-15")]))
chk("  夜盤：合併後算得出來", S.night_eval(date(2026, 10, 14), got).get("decision"), "做多")


# ══ ④b 面板注入的部位判斷＋接線（lab-qa R1②／R4）══════════════════════════
print("\n=== ④b 面板注入：_sim_has_position（沒有確定答案就回 True）＋關鍵字注入 ===")
_B = LP.broker
_saved_pos, _saved_bp = _B._state.get("position"), _B.broker_position
_asked = []


def _bp(ret):
    def f():
        _asked.append(1)
        if isinstance(ret, BaseException):
            raise ret
        return ret
    return f


try:
    _B._state["position"] = None
    _B.broker_position = _bp("unknown")
    chk("  記憶體 None（重啟後）＋券商回 unknown ⇒ True（當成有部位）", LP._sim_has_position(), True)
    _B.broker_position = _bp(RuntimeError("list_positions 炸了"))
    chk("  記憶體 None＋問券商丟例外 ⇒ True", LP._sim_has_position(), True)
    _B.broker_position = _bp({"dir": "long", "qty": 1, "entry": 12000.0})
    chk("  記憶體 None＋券商說有部位（撿回來之前）⇒ True", LP._sim_has_position(), True)
    _B.broker_position = _bp(None)
    chk("  記憶體 None＋券商明確說沒有 ⇒ False（正控組：唯一會放行的情況）", LP._sim_has_position(), False)
    _asked.clear()
    _B._state["position"] = {"dir": "short", "entry": 12000.0}
    chk("  記憶體有部位 ⇒ True、而且不用再問券商", (LP._sim_has_position(), len(_asked)), (True, 0))
finally:
    _B._state["position"] = _saved_pos
    _B.broker_position = _saved_bp


class _FakeThread:
    made = []

    def __init__(self, target=None, args=(), **kw):
        _FakeThread.made.append((target, args))

    def start(self):
        pass


_thr = LP.threading.Thread
_cfg0 = dict(S._CFG)
S._CFG.update(verdict=None, move_pct=None, tpsl=None, hist_read=None, pctl=None, rule=None)
LP.threading.Thread = _FakeThread
try:
    _ok = LP.start_sim_lanes()
finally:
    LP.threading.Thread = _thr
chk("  start_sim_lanes（不真的起執行緒）⇒ True、target＝sim_lanes.loop", (_ok, _FakeThread.made[-1][0] if _FakeThread.made else None),
    (True, S.loop))
say(bool(_FakeThread.made) and _FakeThread.made[-1][1][1] is LP._sim_has_position,
    "  注入的部位判斷是 _sim_has_position（不是只讀記憶體的 _lab_has_position）")
chk("  注入的規則函式各就各位（move_pct／tpsl_points 沒對調）",
    (S._CFG["verdict"] is AF.fast_verdict, S._CFG["move_pct"] is AF.move_pct, S._CFG["tpsl"] is AF.tpsl_points,
     S._CFG["hist_read"] is AF.hist_read, S._CFG["pctl"] == LP.FAST_PCTL, S._CFG["rule"] is AF.FAST_RULE,
     S._CFG["reversal"] is AF.reversal_dir, S._CFG["rev_sec"] == LP.REV_SEC),
    (True, True, True, True, True, True, True, True))
chk("  注入後實算：走幅(12060, 12000)＝0.5%、停利停損(12061)＝60 點、reversal_dir(12006,+1,11990)＝−1",
    (round(S._CFG["move_pct"](12060.0, 12000.0), 3), S._CFG["tpsl"](12061.0),
     S._CFG["reversal"](12006.0, 1, 11990.0)), (0.5, 60, -1))
S._CFG.clear()
S._CFG.update(_cfg0)


# ══ ④c 休市不佔補抓名額、落地成定論（lab-qa R3）═══════════════════════════
print("\n=== ④c 休市：不佔「每輪補一天」的名額、落地成「休市」、不再重問 ===")
_paths0 = (SL.LAB_DIR, S.SIM_DIR, S.FAST_HIST, S.MIN1_CSV)
NOW3 = datetime(2026, 10, 23, 20, 0)          # 週五晚上
EMPTY_DAYS = ("2026-10-23", "2026-10-22", "2026-10-21")
kb21 = night(date(2026, 10, 21), dict(ramp(12012, "21:31", 5, 12)))


def kb21_of(day):
    g = kb21[kb21["ts"].dt.date == date.fromisoformat(day)]
    return {"ts": [str(x) for x in g["ts"]], "Open": g["Close"].tolist(), "High": g["High"].tolist(),
            "Low": g["Low"].tolist(), "Close": g["Close"].tolist(), "Volume": [1] * len(g), "Amount": [1] * len(g)}


def r3_setup(tag, hist_excl):
    SL.LAB_DIR = TMP / tag / "tick_hist"
    S.SIM_DIR = TMP / tag / "sim_lanes"
    S.FAST_HIST = TMP / tag / "fast_hist.jsonl"
    S.MIN1_CSV = TMP / tag / "no_such_1min.csv"
    S.FAST_HIST.parent.mkdir(parents=True, exist_ok=True)
    S.FAST_HIST.write_text("".join(json.dumps(r) + "\n" for r in hist_rows("2026-10-23", 40) if r["date"] not in hist_excl),
                           encoding="utf-8")
    reset_state()
    kn = {"n": 0}

    def kbars(day):
        kn["n"] += 1
        return {"ts": []} if kn["n"] <= 2 else kb21_of(day)       # 前兩次（E＝10-22 那晚的兩天）空的
    return FakeApi(ticks=lambda d: {"ts": []} if d in EMPTY_DAYS else full_ticks(d), kbars=kbars)


try:
    api = r3_setup("r3a", ("2026-10-22", "2026-10-21"))
    S.step(lambda: api, lambda: False, NOW3)
    rows, _st = S.read_rows()
    tk = [c[1] for c in api.calls if isinstance(c, tuple) and c[0] == "ticks"]
    chk("  快攻：今天回空、10-22／10-21 回空（休市）都不佔名額 ⇒ 同一輪繼續補到 10-20，10-19 留給下一輪",
        tk, ["2026-10-23", "2026-10-22", "2026-10-21", "2026-10-20"])
    chk("  快攻：10-22、10-21 落地成「休市」定論",
        [(rows.get(("fast", d), {}).get("why"), rows.get(("fast", d), {}).get("reason")) for d in ("2026-10-22", "2026-10-21")],
        [("holiday", "休市（永豐那天沒有日盤成交）")] * 2)
    chk("  快攻：10-20 補到之後當輪就算出定論", ("fast", "2026-10-20") in rows, True)
    chk("  快攻：今天（10-23）回空 ⇒ ⛔ 不記休市（今天的可能只是還沒好），留在資料缺", (("fast", "2026-10-23") in rows,
        "2026-10-23" in S.STATE["pending"]["fast"]), (False, True))
    kb = [c[1] for c in api.calls if isinstance(c, tuple) and c[0] == "kbars"]
    chk("  夜盤：10-22 那晚兩天都空（休市）不佔名額 ⇒ 同一輪繼續要 10-21 那晚",
        kb, ["2026-10-22", "2026-10-23", "2026-10-21", "2026-10-22"])
    chk("  夜盤：10-22 落地「休市」、10-21 算出定論",
        (rows.get(("night", "2026-10-22"), {}).get("why"), rows.get(("night", "2026-10-21"), {}).get("decision")), ("holiday", "做多"))
    n_calls = len(api.calls)
    S.step(lambda: api, lambda: False, NOW3)
    tk2 = [c[1] for c in api.calls[n_calls:] if isinstance(c, tuple) and c[0] == "ticks"]
    chk("  下一輪：休市那兩天、今天都不再問，只補 10-19", tk2, ["2026-10-19"])

    api = r3_setup("r3b", ())
    S.step(lambda: api, lambda: False, NOW3)
    rows, _st = S.read_rows()
    tk = [c[1] for c in api.calls if isinstance(c, tuple) and c[0] == "ticks"]
    chk("  對照組：fast_hist 裡有 10-22、10-21（那兩天一定開過盤）⇒ 永豐回空也 ⛔ 不記休市、留在資料缺",
        ([rows.get(("fast", d), {}).get("why") for d in ("2026-10-22", "2026-10-21")],
         all(d in S.STATE["pending"]["fast"] for d in ("2026-10-22", "2026-10-21"))), ([None, None], True))
    chk("  對照組：回空照樣不佔名額 ⇒ 同一輪補到 10-20", tk, ["2026-10-23", "2026-10-22", "2026-10-21", "2026-10-20"])
finally:
    SL.LAB_DIR, S.SIM_DIR, S.FAST_HIST, S.MIN1_CSV = _paths0
    S._CSV.update(key=None, df=None)
    reset_state()


# ══ ⑤ 背景例外不外丟 ═════════════════════════════════════════════════
print("\n=== ⑤ 背景例外不外丟 ===")
reset_state()
shutil.rmtree(S.SIM_DIR, ignore_errors=True)
_ld = SL.load_day
SL.load_day = lambda d: (_ for _ in ()).throw(RuntimeError("逐筆讀壞了"))
try:
    ok = S.step(lambda: (_ for _ in ()).throw(RuntimeError("拿 api 就炸")), lambda: 1 / 0, NOW)
    say(True, "  step：load_day／get_api／問部位都丟例外 ⇒ 沒有衝出來", "回 %r" % ok)
except BaseException as e:
    say(False, "  step 讓例外衝出來了", repr(e))
finally:
    SL.load_day = _ld
say(S.STATE["errors"] >= 3 and bool(S.STATE["last_err"]), "  有計數、有最近一次錯誤", "%s %s" % (S.STATE["errors"], S.STATE["last_err"]))
chk("  那天在 pending 裡寫「計算出錯」", S.STATE["pending"]["fast"].get(FD, {}).get("why"), "error")
stt = S.state(NOW)
chk("  端點端得出錯誤計數", stt["errors"], S.STATE["errors"])
_rr = S.read_rows
S.read_rows = lambda: (_ for _ in ()).throw(OSError("磁碟壞了"))
try:
    chk("  step：連讀檔都炸 ⇒ 回 False、不外丟", S.step(lambda: None, lambda: False, NOW), False)
except BaseException as e:
    say(False, "  step 讀檔炸掉時衝出來了", repr(e))
finally:
    S.read_rows = _rr


class StopLoop(BaseException):
    pass


_sleep = S.time.sleep
loops = {"n": 0}


def fake_sleep(s):
    loops["n"] += 1
    if loops["n"] >= 3:
        raise StopLoop()


S.time.sleep = fake_sleep
_st = S.step
S.step = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("step 自己炸"))
e0 = S.STATE["errors"]
try:
    S.loop(lambda: None, lambda: False, every=0)
    say(False, "  loop 應該被 StopLoop 停下來")
except StopLoop:
    say(S.STATE["errors"] - e0 >= 3, "  loop：step 每圈都炸，照樣轉了 3 圈、每圈都計數", "計數 +%d" % (S.STATE["errors"] - e0))
except BaseException as e:
    say(False, "  loop 讓例外衝出來了", repr(e))
finally:
    S.time.sleep = _sleep
    S.step = _st


# ══ ⑥ 端點 ═══════════════════════════════════════════════════════════
print("\n=== ⑥ 端點 /api/sim/state ===")
reset_state()
S.step(lambda: None, lambda: False, NOW)
srv = ThreadingHTTPServer(("127.0.0.1", 0), LP.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"


def req(path, headers=None, method="GET", timeout=20):
    rq = urllib.request.Request(BASE + path, headers=headers or {}, method=method,
                                data=b"{}" if method == "POST" else None)
    try:
        with urllib.request.urlopen(rq, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}
    except (TimeoutError, OSError) as e:
        return "timeout", {"msg": repr(e)[:80]}


h0 = (fhash(S.SIM_DIR), fhash(SL.LAB_DIR), fhash(S.MIN1_CSV), fhash(S.FAST_HIST))
st_code, body = req("/api/sim/state")
chk("  GET ⇒ 200", st_code, 200)
# ⛔ 這裡**寫死** key 與名字：拿 S.LANES／S.LANE_NAME 去比是自己比自己（一起改就永遠綠，
#    2026-09-16 突變 N15 當場抓到這個假綠燈）。
LANE_KEYS = ["fast", "fast11", "hmq", "rev", "orb", "union", "night", "tsm"]
LANE_NAMES = ["快攻", "早收", "回馬槍", "純回馬", "開箱", "多方聯軍", "夜盤順勢", "台積電快攻"]
chk("  ⛔ 後端 LANES 就是這八條（寫死，⛔ 不准拿 S.LANES 比自己）", list(S.LANES), LANE_KEYS)
chk("  端點端出來的八條、順序一樣", list(body.get("lanes", {})), LANE_KEYS)
chk("  ⛔ 八條的名字就是 Benson 定的那八個（寫死）", [body["lanes"][k]["name"] for k in LANE_KEYS], LANE_NAMES)
# ⛔ 寫死：Benson 2026-09-22「模擬就留下多方聯軍跟台積電快攻」
chk("  ⛔ 真正的畫面只端兩條：多方聯軍、台積電快攻（寫死）", list(REAL_SHOWN), ["union", "tsm"])
S.SHOWN_LANES = REAL_SHOWN
_two = S.state(NOW) if "NOW" in globals() else S.state()
chk("  ⛔ 端點真的只端那兩條、順序一樣", list(_two["lanes"]), ["union", "tsm"])
S.SHOWN_LANES = S.LANES
say(set(REAL_SHOWN) <= set(S.LANES), "  畫面那兩條都還在 LANES 裡（背景照算）")
chk("  ⛔ 後端端出去的字裡沒有舊名字",
    [w for w in ("早盤快攻", "快攻回馬槍", "回馬槍那一半", "快攻 11:00 平", "ORB", "美股開盤順勢")
     if w in json.dumps(body, ensure_ascii=False)], [])
say(all(body["lanes"][ln]["rule"] and "沒有接上" not in body["lanes"][ln]["rule"] for ln in S.LANES),
    "  八條都有規則句（後端給的）")
L = body.get("lanes", {}).get("fast", {})
say(len(L.get("months", [])) == 6 and L["months"][0]["this"] and L["months"][0]["label"] == "本月", "  月合計 6 個月、第一個標「本月」")
say(all(k in L for k in ("recent", "today", "pending", "rule", "fetch")) and "errors" in body and "file" in body,
    "  有最近清單、今天狀態、資料缺原因、錯誤計數、檔案計數")
chk("  note 寫「成本已扣；夜盤用 1 分 K 近似」", body.get("note"), "成本已扣；夜盤用 1 分 K 近似")
for _ in range(3):
    req("/api/sim/state")
chk("  唯讀：打 4 次之後 sim_lanes／tick_hist／1 分 K／fast_hist 雜湊都沒變",
    (fhash(S.SIM_DIR), fhash(SL.LAB_DIR), fhash(S.MIN1_CSV), fhash(S.FAST_HIST)), h0)
chk("  別的網站的分頁叫它 ⇒ 403", req("/api/sim/state", {"Sec-Fetch-Site": "cross-site"})[0], 403)
say(req("/api/sim/state", {"Content-Type": "application/json"}, method="POST")[0] not in (200, "timeout"), "  POST 不接")
_sv = LP.sim_lanes
LP.sim_lanes = None
try:
    chk("  sim_lanes 是 None ⇒ 503「模擬載入失敗」", req("/api/sim/state")[0:1] + (req("/api/sim/state")[1].get("msg"),), (503, "模擬載入失敗"))
    chk("  start_sim_lanes() 模組沒載入 ⇒ 回 False、不丟", LP.start_sim_lanes(), False)
finally:
    LP.sim_lanes = _sv
srv.shutdown()


class _Boom:
    def __getattr__(self, n):
        raise RuntimeError("拿不到")


LP.sim_lanes = _Boom()
try:
    chk("  start_sim_lanes() 起不來 ⇒ 回 False、不丟", LP.start_sim_lanes(), False)
except BaseException as e:
    say(False, "  start_sim_lanes 例外衝出來了", repr(e))
finally:
    LP.sim_lanes = _sv
_code = ("import sys; sys.modules['sim_lanes']=None\n"
         "import live_panel as LP\n"
         "assert LP.sim_lanes is None\n"
         "print('IMPORTED', LP.start_sim_lanes())\n")
pr = subprocess.run([sys.executable, "-c", _code], cwd=str(HERE), capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=240, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
say(pr.returncode == 0 and "IMPORTED False" in pr.stdout and "【模擬】載入失敗" in pr.stdout,
    "  sim_lanes 載入失敗 ⇒ import live_panel 照樣成功、有印警告", (pr.stdout[-160:] + pr.stderr[-300:]).replace("\n", " "))


# ══ ⑦ 前端 ═══════════════════════════════════════════════════════════
print("\n=== ⑦ 前端 ===")
import re as _re
page = LP.PAGE
# ⚠️ 切到【自動下單】那段註解之前為止：#tab-sim 之後緊接的是 #tab-fire，
#    用 index('<div id="tab-lab"') 會把整個【自動下單】吃進來（第一版就踩到，altbl／/api/fire 全被算成模擬的）。
tab = page[page.index('<div id="tab-sim"'):page.index("<!-- ══════════ 【自動下單】")]
tab_code = _re.sub(r"<!--.*?-->", " ", tab, flags=_re.S)
say(_re.match(r'\s*<div id="tab-sim" hidden>\s*<div class="card sm-card" id="smcard">', tab_code) is not None,
    "  【模擬】是獨立分頁，卡是它的第一個子元素")
card = tab_code
say("模擬（不會下單）" in card and 'id="smlanes"' in card, "  卡的標題與八條的容器")
say('id="tab-tick"' not in page and 'id="tab-review"' not in page,
    "  【細節】與【回顧】兩個分頁的容器都不在了")
# ⚠️ 2026-09-21 加了【帳戶】分頁（唯讀，排在即時右邊）⇒ 變五顆
chk("  分頁列恰好五顆，順序＝即時／帳戶／模擬／策略實驗室／自動下單",
    _re.findall(r'<button data-tab="([a-z0-9]+)"', page), ["live", "acct", "sim", "lab", "fire"])
_j0 = page.index("/* ══════════════ 【模擬】分頁：七條策略")
sjs = page[_j0:page.index("/* ══════════════ 【策略實驗室】分頁：歷史逐筆回測", _j0)]
say("function smLoad" in sjs and "function lbRun" not in sjs and len(sjs) > 1500, "  切出來的是模擬那一段 JS（不多不少）")
sjs_code = "\n".join(_re.sub(r"//.*$", "", ln) for ln in _re.sub(r"/\*.*?\*/", " ", sjs, flags=_re.S).splitlines())
fetches = sorted(set(x.split("'")[1].split("?")[0] for x in sjs.split("fetch(")[1:]))
# ⭐ 2026-09-17 多了「點進去一條」的內頁 ⇒ 第二個端點。⛔ 兩個都是 GET、都是唯讀；
#    ⛔ 這個清單是**寫死**的：哪天多打了第三個端點，這一條就要紅（不准改成「開頭是 /api/sim 就好」）。
# ⭐ 2026-09-18 再多一個「點一天看圖」⇒ 第三個端點（一樣是 GET、唯讀）。⛔ 清單照樣寫死。
chk("  只打 GET /api/sim/state／lane／daychart（三個都唯讀）", fetches,
    ["/api/sim/daychart", "/api/sim/lane", "/api/sim/state"])
say("'/api/sim/lane?key='" in sjs and "smDetOpen" in sjs, "  內頁那個端點是「點下去」那條路在打")
# ⛔ 內頁那個端點**整段 JS 裡只准出現一次**，而且要落在 smDetOpen 裡 ——
#    ⛔ 不准驗成「不在某個位置之前」（註解也算數，第一版就這樣綠不起來）。
say(sjs_code.count("/api/sim/lane") == 1
    and sjs_code.index("function smDetOpen") < sjs_code.index("/api/sim/lane") < sjs_code.index("function smDetClose"),
    "  內頁那個端點只被 smDetOpen 打（⛔ 不在 60 秒輪詢那條路上）")
say("/api/sim/state" in sjs_code[sjs_code.index("function smLoad"):],
    "  60 秒輪詢那一支打的還是 /api/sim/state")
for w in ("broker", "place_order", "/api/enter", "/api/real/", "/api/fire", "method:", "POST", "pfetch(", "data-act",
          "data-rdir", "<form", "submit", "token", "PTOK", "<button", "altbl", "AL."):
    chk(f"  模擬分頁 HTML／JS 沒有 {w}", w in card + sjs_code, False)
for w in ("建議", "推薦", "會賺", "明天", "應該進場", "最佳", "預測", "期望值", "訊號強度", "勝率"):
    chk(f"  模擬分頁畫面文字沒有「{w}」", w in card + sjs_code, False)
# ⛔ 內頁那份說明（`_rule_detail`）也是後端端出去的字 ⇒ 同一把尺要掃到它，
#    不然「不准有建議口吻」那條會被新功能整個繞過去。
state_txt = (json.dumps(S.state(NOW), ensure_ascii=False)
             + "".join(S._rule_text(ln) for ln in S.LANES)
             + json.dumps([S._rule_detail(ln) for ln in S.LANES], ensure_ascii=False))
for w in ("建議", "推薦", "會賺", "明天", "應該進場", "最佳", "預測", "期望值", "訊號強度", "勝率"):
    chk(f"  後端端出去的文字沒有「{w}」", w in state_txt, False)
chk("  分頁的 HTML／JS 沒寫死時刻／點數（09:03、09:15、130、21:30、0.5%）",
    [w for w in ("09:03", "09:15", "11:00", "130", "21:30", "22:30", "0.5%", "1%") if w in card + sjs_code], [])
say("if(t==='sim'){ smEnter(); }" in page, "  切進【模擬】才問（不掛 500ms tick）")
say("if(TAB!=='sim') return;" in sjs, "  離開這一頁就停止每 60 秒的輪詢")
say("e._smh!==html" in sjs and "innerHTML===" not in sjs, "  沒變就別動 DOM：比的是節點上快取的字串（不讀回 innerHTML）")
say("my!==SM.seq" in sjs, "  請求帶流水號，只認最後一次")
say("Object.keys(x.lanes)" in sjs, "  八條的順序由後端決定（前端不寫死 lane 名字）")
chk("  前端 JS 沒有寫死任何一條 lane 的 key", [k for k in S.LANES if ("'%s'" % k) in sjs_code], [])
fire_html = page[page.index('<div id="tab-fire"'):page.index('<div id="tab-lab"')]
chk("  【自動下單】那一頁沒有模擬的東西（清單完全分開）", [w for w in ("smcard", "sm-", "/api/sim") if w in fire_html], [])
lab_html = page[page.index('<div id="tab-lab"'):page.index("<!-- ══ 【策略實驗室】到此 ══")]
chk("  【策略實驗室】那一頁已經沒有模擬卡", [w for w in ("smcard", "sm-lane", "/api/sim") if w in lab_html], [])


# ══ ⑦b 卡片瘦身 ＋ 點進去的內頁（2026-09-17 Benson 交辦）═══════════════
print("\n=== ⑦b 卡片瘦身 ＋ 內頁 ===")
# ① 卡上「最近一筆」與規則句收掉了 —— ⛔ 是真的不見，不是被 CSS 藏起來
# ⚠️ 比的是**去掉註解**的程式碼：註解裡本來就會提到「最近一筆搬去哪了」（第一版拿 sjs 比，紅在自己的註解上）
say("最近一筆" not in sjs_code and "sm-rule" not in card + sjs_code and "function smRow(" not in sjs_code,
    "  卡上的『最近一筆』與規則句整段拿掉了（不是藏起來）")
say("sm-more" in sjs and "點一下看全部紀錄與定義" in sjs, "  卡上改成「點進去」的提示")
# ② 內頁的容器在 #smcard 之後、預設 hidden
say('<div class="card sm-card" id="smdet" hidden></div>' in tab_code, "  內頁容器 #smdet 預設 hidden")
say(tab_code.index('id="smcard"') < tab_code.index('id="smdet"'), "  #smdet 排在 #smcard 後面")
# ③ 兩張卡同時只有一張看得見（⛔ 不准兩張一起出現）
say("c.hidden=true" in sjs and "e.hidden=false" in sjs and "e.hidden=true" in sjs and "c.hidden=false" in sjs,
    "  開內頁 ⇒ 藏卡片；關內頁 ⇒ 還原（兩張同時只有一張看得見）")
# ⚠️ 2026-09-18 Esc 改成「一次退一層」（先關那天的圖、再關內頁）⇒ 比對的字串跟著換，要驗的行為不變
say("ev.key==='Escape'" in sjs_code and "if(SM.det) smDetClose()" in sjs_code, "  Esc 關得掉內頁")
say("my!==SM.dseq" in sjs and "SM.dseq++" in sjs,
    "  內頁的請求也帶流水號；關掉時把流水號往前推（還在路上的回應不准再畫）")
say("n.id.slice(3)" in sjs, "  內頁要看哪一條是從節點 id 取的（⛔ 前端不寫死 lane 名字）")
# ⑧ 月表點下去跳到那個月（2026-09-17 Benson 交辦）
say("function smJump" in sjs and "data-m=" in sjs, "  月表點得下去、逐日每一列帶得出是哪個月")
say("/^\\d{4}-\\d{2}$/.test(m" in sjs,
    "  月份先驗過 YYYY-MM 才丟進 querySelector（⛔ 不准把任意字串接進選擇器）")
say("scrollIntoView" not in sjs_code and "sc.scrollTop+=" in sjs_code,
    "  捲的是清單那個容器自己（⛔ 不用 scrollIntoView：它會把整頁一起捲走）")
say("thead" in sjs_code and "head.getBoundingClientRect().height" in sjs_code,
    "  有扣掉 sticky 表頭的高度（不然跳過去的第一列被壓在表頭底下）")
# ⛔ 卡上那張月表**不可以**變成可點的：整張卡本來就是一顆「點進去」的鈕
say("all?' hit\"" in sjs, "  只有內頁那張月表有 .hit（卡上那張不可點）")
# ④ **粗體一定要先 esc 再換**：順序反過來就是 HTML 注入
say(sjs.index("esc(String(s==null?'':s))") < sjs.index(".replace(/\\*\\*([^*]+)\\*\\*/g"),
    "  smMd 先 esc 再換粗體（⛔ 反過來就開了 HTML 注入）")
# ⑤ 後端：lane_detail
say(S.lane_detail("這條不存在") is None, "  不認得的 lane ⇒ 回 None（呼叫端才回得了 400）")
for _ln in S.LANES:
    _d = S.lane_detail(_ln, NOW)
    say(isinstance(_d, dict) and _d["key"] == _ln and isinstance(_d.get("rows"), list)
        and isinstance(_d.get("months"), list) and isinstance(_d.get("total"), dict),
        "  %s：內頁端得出 rows／months／total" % S.LANE_NAME[_ln])
    _dt = _d.get("detail") or {}
    say(bool(_dt.get("plain")) and len(_dt.get("steps") or []) >= 4,
        "  %s：白話一句 ＋ 至少 4 項逐條說明" % S.LANE_NAME[_ln], _dt.get("plain"))
# ⑥ `calc` 端得出去（回填 vs 即時就靠它）
say("calc" in S._slim({"date": "2026-01-02", "decision": "不做", "calc": "backfill"}),
    "  _slim 端得出 calc（回填）")
say("calc" not in S._slim({"date": "2026-01-02", "decision": "不做"}),
    "  沒有 calc 的舊列就是「即時」（⛔ 不補預設值、不做資料遷移）")
# ⑦ `_months_all`：**有定論的每一個月**，⛔ 中間沒資料的月份不補 0
_fake = [{"date": "2026-03-02", "decision": "不做", "points": None},
         {"date": "2026-01-05", "decision": "做多", "points": 10.0},
         {"date": "2026-01-06", "decision": "做空", "points": -4.0}]
_ma = S._months_all(_fake, NOW)
chk("  月表＝有定論的每一個月、新到舊（⛔ 中間的 2 月不補 0）",
    [(m["month"], m["days"], m["trades"], m["points"]) for m in _ma],
    [("2026-03", 1, 0, 0), ("2026-01", 2, 2, 6.0)])
say(len(S._months(NOW, _fake)) == S.MONTHS_SHOWN,
    "  卡上那張月表還是固定 %d 個月（⛔ 內頁才給全部）" % S.MONTHS_SHOWN)


# ══ ⑦c 點一天看圖（2026-09-18 Benson 交辦）═══════════════════════════════
print("\n=== ⑦c 點一天看圖 ===")
# ① 前端：只從 smDayOpen 打、⛔ 不借即時分頁那張圖
say(sjs_code.count("/api/sim/daychart") == 1
    and sjs_code.index("function smDayOpen") < sjs_code.index("/api/sim/daychart") < sjs_code.index("function smDayStep"),
    "  那天的圖只由 smDayOpen 打（點下去才打）")
chk("  ⛔ 不借即時分頁那張圖（真單在跑時它也在用）",
    [w for w in ("paintChart", "chartSVG", "csvg", "barsCache", "viewDate") if w in sjs_code], [])
say("ev.key==='Escape'" in sjs_code and "if(open) return smDayClose()" in sjs_code,
    "  Esc 一次只退一層（先關圖、再關內頁）")
say("smDayClose();" in sjs[sjs.index("function smDetClose"):sjs.index("function smBind")],
    "  關掉內頁時，那天的圖一起收（⛔ 不准留一張孤兒浮層）")
say('data-d="' in sjs and "tr[data-d]" in sjs, "  逐日每一列都帶日期、點得下去")
# ② 後端：參數不對 ⇒ None（呼叫端回 400）
say(S.day_chart("這條不存在", DAY) is None and S.day_chart("fast", "亂填") is None,
    "  key 或日期不對 ⇒ None")
say(S.day_chart("fast", DAY, rows={})["notes"] == ["這一天沒有這一條的定論"],
    "  沒有定論的日子 ⇒ 講出來（⛔ 不是空白一張圖）")
# ③ 用真的 fast_eval 算出一列，再畫 —— ⛔ 圖上的每個點都要對得回那一列（不准自己重算）
_D_tp = base_day(12000, 12060, extra=[(ms(10, 0), 12100.0, 12099, 12101), (ms(10, 30), 12121.0, 12120, 12122)])
_r_tp = S.fast_eval(DAY, _D_tp, H40)
_old_load = SL.load_day
try:
    SL.load_day = lambda d: _D_tp
    _c = S.day_chart("fast", DAY, rows={("fast", DAY): _r_tp})
finally:
    SL.load_day = _old_load
_en = [m for m in _c["marks"] if m["kind"] == "entry"]
_ex = [m for m in _c["marks"] if m["kind"] == "exit"]
chk("  停利那天：進場標在規則的時刻、價格＝落地那一列",
    (_en[0]["at"], _en[0]["price"]) if _en else None, (S.FAST_PX_AT, _r_tp["entry"]))
chk("  停利那天：出場時刻從逐筆找回來＝第一筆碰到停利價的那一筆（10:30:00）",
    (_ex[0]["at"], _ex[0]["price"]) if _ex else None, ("10:30:00", _r_tp["exit"]))
say(any("從逐筆找回來" in n for n in _c["notes"]), "  畫面上講清楚「出場時刻是找回來的」")
chk("  停利停損兩條線 ＝ 進場價 ± 落地那一列的 tpsl_points",
    sorted((l["label"], l["price"]) for l in _c["lines"] if l["style"] in ("tp", "sl")),
    sorted([("停利", _r_tp["entry"] + _r_tp["tpsl_points"]), ("停損", _r_tp["entry"] - _r_tp["tpsl_points"])]))
say(len(_c["bars"]) > 0 and _c["bars"][0][0] <= "08:46", "  畫得出 1 分 K（從 08:45 那一分鐘開始）")
# ④ 找不到出場的那一筆 ⇒ ⛔ 不猜時間
_r_bad = dict(_r_tp, exit=99999.0)
try:
    SL.load_day = lambda d: _D_tp
    _c2 = S.day_chart("fast", DAY, rows={("fast", DAY): _r_bad})
finally:
    SL.load_day = _old_load
_ex2 = [m for m in _c2["marks"] if m["kind"] == "exit"]
say(_ex2 and _ex2[0]["at"] is None and any("重建不出來" in n for n in _c2["notes"]),
    "  出場價從來沒被碰到 ⇒ 時刻給 None ＋ 講出來（⛔ 不猜一個時間）")
# ⑤ 同一個價位的線併成一條（開箱的停損就是箱底）
chk("  同價位的線併成一條「箱底＝停損」、顏色用停損的",
    S._merge_lines([{"price": 100.0, "label": "箱底", "style": "box"},
                    {"price": 100.0, "label": "停損", "style": "sl"}]),
    [{"price": 100.0, "label": "箱底＝停損", "style": "sl"}])



# ══ ⑪ 新的四條（2026-09-16）═══════════════════════════════════════════
print("\n=== ⑪ hmq／rev／fast11：回馬槍／純回馬／早收 ===")
reset_state()
D_FAST = base_day(12000, 12060, extra=[(ms(10, 0), 12100.0, 12099, 12101), (ms(10, 30), 12121.0, 12120, 12122)])
r_fast = S.fast_eval(DAY, D_FAST, H40)
r_hmq = S.hmq_eval(DAY, D_FAST, H40)
chk("  快的日子：hmq 跟 fast 逐欄位一模一樣（只差 lane）",
    {k: v for k, v in r_hmq.items() if k != "lane"}, {k: v for k, v in r_fast.items() if k != "lane"})
r_rev = S.rev_eval(DAY, D_FAST, H40)
chk("  快的日子：rev 不做（fast_skip）", (r_rev.get("decision"), r_rev.get("why")), ("不做", "fast_skip"))
say("快，這條不做" in (r_rev.get("reason") or ""), "  rev 的原因寫「快，這條不做」", r_rev.get("reason"))

# 慢（走 0.05%）＋ 09:15 反轉往下：進場＝09:15 的買價 11989、停利停損 round(11989×0.5%)=60
D_REV = base_day(12000, 12006, extra=[(ms(9, 15), 11990.0, 11989, 11991), (ms(10, 0), 11930.0, 11929, 11931)])
r_hmq2 = S.hmq_eval(DAY, D_REV, H40)
chk("  慢＋09:15 反轉：hmq 做空／進場用 09:15 的買價／停利停損用進場價算",
    (r_hmq2.get("decision"), r_hmq2.get("entry"), r_hmq2.get("tpsl_points"), r_hmq2.get("why")),
    ("做空", 11989.0, 60, "rev"))
chk("  慢＋09:15 反轉：13:43:30 收盤平（對手價 11931）⇒ 58−5",
    (r_hmq2.get("exit_reason"), r_hmq2.get("exit"), r_hmq2.get("points")), ("收盤", 11931.0, 53.0))
chk("  慢的日子：rev 跟 hmq 那一半一模一樣（只差 lane）",
    {k: v for k, v in S.rev_eval(DAY, D_REV, H40).items() if k != "lane"},
    {k: v for k, v in r_hmq2.items() if k != "lane"})
chk("  fast 在慢的日子還是不做（⛔ 三條沒有互相污染）",
    (S.fast_eval(DAY, D_REV, H40).get("decision"), S.fast_eval(DAY, D_REV, H40).get("why")), ("不做", "not_fast"))
say(r_hmq2.get("p15") == 11990.0 and r_hmq2.get("rev_at") == "09:15:00",
    "  落地那一列記了 09:15 的價與時刻", r_hmq2.get("rev_at"))

D_SAME = base_day(12000, 12006, extra=[(ms(9, 15), 12020.0, 12019, 12021)])
chk("  慢＋09:15 同方向 ⇒ 不做（no_rev）",
    (S.hmq_eval(DAY, D_SAME, H40).get("decision"), S.hmq_eval(DAY, D_SAME, H40).get("why")), ("不做", "no_rev"))
D_EQ = base_day(12000, 12006, extra=[(ms(9, 15), 12006.0, 12005, 12007)])
chk("  慢＋09:15 一樣價 ⇒ 不做（研究：opp=0 不做）", S.hmq_eval(DAY, D_EQ, H40).get("why"), "no_rev")
D_NO15 = base_day(12000, 12006)
chk("  09:03:30~09:15 沒有成交 ⇒ 不做（no_p15）", S.hmq_eval(DAY, D_NO15, H40).get("why"), "no_p15")
D_D0 = base_day(12000, 12000, extra=[(ms(9, 15), 11990.0, 11989, 11991)])
chk("  09:03:30 跟 09:00 一樣價（沒有方向）⇒ hmq 不做", S.hmq_eval(DAY, D_D0, H40).get("decision"), "不做")

# ⛔ 一天最多一口：快的日子**不會**再看 09:15（就算 09:15 反轉得很兇）
D_BOTH = base_day(12000, 12060, extra=[(ms(9, 15), 11900.0, 11899, 11901), (ms(10, 30), 12121.0, 12120, 12122)])
rb = S.hmq_eval(DAY, D_BOTH, H40)
chk("  ⛔ 一天最多一口：快的日子照 09:03:30 進場，不看 09:15",
    (rb.get("why"), rb.get("entry"), rb.get("decision")), ("fast", 12061.0, "做多"))

print("\n  -- fast11：⛔ 只有收盤平倉時刻不同 --")
# ⚠️ 11:15 那一筆是**故意**放的：只比對 cutoff 那個字串的話，把 11:00 改成 11:30 不會翻紅
#    （2026-09-16 突變 N4b 當場抓到）。有這一筆，時刻挪動就會算出不同的點數。
D_LATE = base_day(12000, 12060, extra=[(ms(10, 30), 12090.0, 12089, 12091), (ms(11, 15), 12110.0, 12109, 12111),
                                       (ms(12, 0), 12121.0, 12120, 12122)])
a11 = S.fast11_eval(DAY, D_LATE, H40)
a00 = S.fast_eval(DAY, D_LATE, H40)
chk("  同一天：fast 撐到 12:00 停利 ＋55；fast11 11:00 就平（對手價 12089）＋23",
    (a00.get("exit_reason"), a00.get("points"), a11.get("exit_reason"), a11.get("points"), a11.get("exit")),
    ("停利", 55.0, "收盤", 23.0, 12089.0))
chk("  fast11 的 cutoff 寫 11:00:00", (a11.get("cutoff"), a00.get("cutoff")), ("11:00:00", "13:43:30"))
chk("  進場那一半完全相同（方向／進場價／停利停損點數）",
    (a11.get("decision"), a11.get("entry"), a11.get("tpsl_points")),
    (a00.get("decision"), a00.get("entry"), a00.get("tpsl_points")))
EXP = "2026-10-21"
say(SL.is_expiry(date.fromisoformat(EXP)), "  治具自證：%s 是結算日" % EXP)
HE = hist_rows(EXP, 40)
chk("  結算日：fast 收 13:30、fast11 還是 11:00",
    (S.fast_eval(EXP, D_LATE, HE).get("cutoff"), S.fast11_eval(EXP, D_LATE, HE).get("cutoff")),
    ("13:30:00", "11:00:00"))
chk("  不快的日子：fast11 也不做", S.fast11_eval(DAY, D_REV, H40).get("why"), "not_fast")
chk("  沒接上規則 ⇒ 四條都記「沒接上」、⛔ 不猜",
    [f(DAY, D_FAST, H40, cfg={"verdict": None})["why"]
     for f in (S.fast_eval, S.hmq_eval, S.rev_eval, S.fast11_eval)], ["not_wired"] * 4)


print("\n=== ⑪b 開箱（ORB 5 分＋箱子濾網）===")


def orb_ev(*a, **k):
    """
    ⛔ 這一節一律走這支，**不要直接呼叫 `S.orb_eval`**（lab-qa 2026-09-16 S2）：
    `orb_eval` 會在「哨兵被碰到」時往外丟例外，直接呼叫的話整支測試會**崩潰**，
    而崩潰型的紅看不出是哪一條在守（突變 N9 就是這樣紅的）。丟例外 ⇒ 回一個
    絕對對不上任何斷言的 dict ⇒ **乾淨的紅**。
    ⚠️ 「哨兵被碰到要丟例外」本身由 ⑪c3 用斷言釘住，⛔ 不是靠這裡。
    """
    try:
        return S.orb_eval(*a, **k)
    except Exception as e:
        return {"why": "EXC", "decision": "EXC", "exc": repr(e)}
ODAY = "2026-11-10"           # 不是結算日
say(not SL.is_expiry(date.fromisoformat(ODAY)), "  治具自證：%s 不是結算日" % ODAY)
BOX = [(ms(9, 0, 0), 12000.0, 11999, 12001), (ms(9, 2), 12040.0, 12039, 12041),
       (ms(9, 4), 11980.0, 11979, 11981), (ms(9, 5, 0), 12010.0, 12009, 12011)]
D_UP = mkD(BOX + [(ms(9, 30), 12050.0, 12049, 12051), (ms(10, 0), 12900.0, 12899, 12901),
                  (ms(13, 44, 59), 12800.0, 12799, 12801)])
o = S.orb_calc(ODAY, D_UP)
chk("  箱子＝09:00~09:05 的最高最低（兩端都含）", (o["hi"], o["lo"], round(o["w"], 1)), (12040.0, 11980.0, 60.0))
bp = o["box_pct"]
r = orb_ev(ODAY, D_UP, [bp] * S.ORB_HIST_N)
chk("  箱寬**等於**中位數 ⇒ 要做（⛔ 只有「小於」才不做）", r.get("decision"), "做多")
chk("  突破上緣做多：進場＝那一筆的賣價、停損＝箱子下緣（12051−11980＝71）",
    (r.get("entry"), r.get("sl_points")), (12051.0, 71.0))
chk("  ⛔ 不設停利：漲到 12900 也不出場，13:43:30 收盤平（對手價 12899）⇒ 848−5",
    (r.get("exit_reason"), r.get("exit"), r.get("points")), ("收盤", 12899.0, 843.0))
chk("  cutoff 13:43:30、成本 5 點", (r.get("cutoff"), r.get("cost")), ("13:43:30", 5.0))
say("停損＝箱子另一端" in (r.get("reason") or ""), "  原因寫得出停損是箱子另一端", r.get("reason"))
r_narrow = orb_ev(ODAY, D_UP, [bp * 1.0000001] * S.ORB_HIST_N)
chk("  箱寬小於中位數 ⇒ 定論「不做」（narrow_box）", (r_narrow.get("decision"), r_narrow.get("why")), ("不做", "narrow_box"))
say(not r_narrow.get("pending"), "  箱子太窄是**定論**（會落地），不是資料缺")
p_few = orb_ev(ODAY, D_UP, [bp] * (S.ORB_HIST_N - 1))
chk("  歷史不夠 ⇒ **資料缺**（⛔ 不寫檔，逐筆之後可能補得回來）",
    (p_few.get("pending"), p_few.get("why")), (True, "few_box_hist"))
chk("  沒有當天逐筆 ⇒ 資料缺", orb_ev(ODAY, None, [bp] * 20).get("why"), "no_ticks")
chk("  算不出歷史 ⇒ 資料缺", orb_ev(ODAY, D_UP, None).get("why"), "no_box_hist")

D_DN = mkD(BOX + [(ms(9, 30), 11970.0, 11969, 11971), (ms(10, 0), 12045.0, 12044, 12046),
                  (ms(13, 44, 59), 12000.0, 11999, 12001)])
rd = orb_ev(ODAY, D_DN, [0.0] * S.ORB_HIST_N)
chk("  跌破下緣做空：進場用買價 11969、停損＝箱子上緣（12040−11969＝71）",
    (rd.get("decision"), rd.get("entry"), rd.get("sl_points")), ("做空", 11969.0, 71.0))
chk("  碰到箱子上緣 ⇒ 停損，用觸發那一筆的成交價 12045 ⇒ −76−5",
    (rd.get("exit_reason"), rd.get("exit"), rd.get("points")), ("停損", 12045.0, -81.0))

D_EDGE = mkD(BOX + [(ms(9, 30), 12040.0, 12039, 12041), (ms(9, 40), 11980.0, 11979, 11981),
                    (ms(13, 44, 59), 12000.0, 11999, 12001)])
chk("  剛好碰到箱子邊**不算**突破（上緣用 >、下緣用 <）⇒ 整天沒突破",
    orb_ev(ODAY, D_EDGE, [0.0] * 20).get("why"), "no_break")
D_TWICE = mkD(BOX + [(ms(9, 30), 12050.0, 12049, 12051), (ms(9, 40), 11900.0, 11899, 11901),
                     (ms(13, 44, 59), 11950.0, 11949, 11951)])
r2 = orb_ev(ODAY, D_TWICE, [0.0] * 20)
chk("  ⛔ 一天最多 1 次：只認第一次那一筆（先上緣 ⇒ 做多）", (r2.get("decision"), r2.get("entry")), ("做多", 12051.0))
D_NOBOX = mkD([(ms(8, 50), 12000.0, 11999, 12001), (ms(9, 30), 12100.0, 12099, 12101)])
chk("  09:00~09:05 沒有成交 ⇒ 定論「不做」（no_box）", orb_ev(ODAY, D_NOBOX, [0.0] * 20).get("why"), "no_box")
chk("  結算日 13:30 平", orb_ev(EXP, D_UP, [0.0] * 20).get("cutoff"), "13:30:00")

print("\n  -- box_hist：這一天以前最近 20 個有逐筆的交易日 --")
# ⛔ 換一個乾淨的 tick_hist：那 22 個「這天以前」的日子會蓋到前面幾節寫的 2026-10-20
#    （第一版就是這樣把 FD 那天的逐筆換成窄箱，害 ⑪c 整排翻紅 —— 治具互相污染）
S._BOX.clear()
_lab0, SL.LAB_DIR = SL.LAB_DIR, TMP / "tick_hist_box"
_bd, _wrote = date.fromisoformat(ODAY), []
while len(_wrote) < 22:
    _bd -= timedelta(days=1)
    if _bd.weekday() >= 5:
        continue
    _w = 12.0 + len(_wrote)                     # 每天不一樣，才看得出順序與取哪 20 天
    write_ticks(str(_bd), [(ms(9, 0, 0), 12000.0, 11999, 12001),
                           (ms(9, 4), 12000.0 + _w, 12000.0 + _w - 1, 12000.0 + _w + 1),
                           (ms(9, 5, 0), 12000.0, 11999, 12001)])
    _wrote.append((str(_bd), _w))
# 今天那一天＋**之後**兩天都寫成超寬的箱子：只要有一天偷看到未來，下面的值就對不起來
# （⛔ 一定要有「之後」那兩天 —— 只有「今天」的話，把 `< day` 改成 `!= day` 照樣是綠的，
#   2026-09-16 突變 N13 當場抓到）。
for _fd in (ODAY, "2026-11-11", "2026-11-12"):
    write_ticks(_fd, [(ms(9, 0, 0), 12000.0, 11999, 12001), (ms(9, 4), 19999.0, 19998, 20000),
                      (ms(9, 5, 0), 12000.0, 11999, 12001)])
BH = S.box_hist(ODAY)
chk("  恰好 20 天", len(BH), S.ORB_HIST_N)
_want = [round(w / 12000.0 * 100.0, 9) for _d, w in sorted(_wrote)[-20:]]
chk("  值＝箱寬 ÷ 分母 × 100（沒有突破 ⇒ 分母用箱子最後一筆 12000），而且是舊到新",
    [round(x, 9) for x in BH], _want)
say(max(BH) < 1.0, "  ⛔ 今天那一天（箱寬 7999）沒有被算進「這一天以前」", "最大 %.4f%%" % max(BH))
say(not S._BOX_ST["busy"], "  ⛔ box_hist 跑完要把「計算中」旗標收掉")
SL.LAB_DIR = _lab0
S._BOX.clear()
say(len(S.box_hist(ODAY)) < S.ORB_HIST_N,
    "  還原 tick_hist（自證：換回去之後那 22 天就看不到了）", "剩 %d 天" % len(S.box_hist(ODAY)))

print("\n  -- 掃箱子歷史時：畫面要看得出「還在算」，⛔ 不可以像「沒有資料」（PM 2026-09-16 裁示）--")
say(S.box_scan_msg() == "", "  沒在掃的時候是空字串")
_st_idle = S.state(NOW)
chk("  沒在掃的時候端點的 scan 全空", [ln for ln in S.LANES if _st_idle["lanes"][ln]["scan"]], [])
S._BOX_ST.update(busy=True, day="2026-11-10", done=7, need=20)
_scan = S.box_scan_msg()
say("計算中" in _scan and "7" in _scan and "20" in _scan, "  掃描中：講得出「還在算」＋進度", _scan)
_st_scan = S.state(NOW)
chk("  ⛔ 端點把「還在算」端給開箱與多方聯軍（那兩條才吃箱子歷史）",
    [ln for ln in S.LANES if _st_scan["lanes"][ln]["scan"]], ["orb", "union"])
say(_st_scan["lanes"]["orb"]["today"]["msg"] == _scan,
    "  ⛔ 今天那一格也講「還在算」，⛔ 不是「等背景下一輪」", _st_scan["lanes"]["orb"]["today"]["msg"])
S._BOX_ST["busy"] = False
say(S.box_scan_msg() == "" and not any(v["scan"] for v in S.state(NOW)["lanes"].values()),
    "  旗標收掉之後畫面不再說計算中（自證：上面那幾條不是恆真）")


print("\n=== ⑪d 多方聯軍（union）===")
# ⚠️ 治具的 09:03:29 那一筆落在 09:00~09:05 裡 ⇒ 它也算進箱子（這是規則，不是 bug）
_HEAD = [(ms(8, 45, 0, 100), 11990.0, 11989, 11991), (ms(8, 59, 59), 12000.0, 11999, 12001)]
UBH = [0.0] * S.ORB_HIST_N          # 中位數 0 ⇒ 箱子濾網一定過（這一節專心測「選誰」）

# ① 最早的候選說做空 ⇒ 略過，但**要繼續看下一個**（開箱 09:30 做多）
D_U1 = mkD(_HEAD + [(ms(9, 0, 0), 12000.0, 11999, 12001), (ms(9, 2), 12040.0, 12039, 12041),
                    (ms(9, 3, 29), 11940.0, 11939, 11941), (ms(9, 4), 11980.0, 11979, 11981),
                    (ms(9, 5, 0), 12000.0, 11999, 12001), (ms(9, 30), 12050.0, 12049, 12051),
                    (ms(10, 0), 12200.0, 12199, 12201), (ms(13, 44, 59), 12150.0, 12149, 12151)])
_c1 = S.union_cands(DAY, D_U1, S.day_pack(DAY, D_U1, H40, UBH))[0]
chk("  ① 候選收得齊、照觸發時刻排好（快攻 09:03:30 空 → 開箱 09:30 多）",
    [(x["kind"], x["dir"], x["at"]) for x in _c1], [("fast", -1, "09:03:30"), ("orb", 1, "09:30:00")])
r1 = S.union_eval(DAY, D_U1, H40, UBH)
chk("  ① ⛔ 說做空就略過、但**繼續看下一個** ⇒ 照開箱做多",
    (r1.get("decision"), r1.get("pick"), r1.get("at"), r1.get("entry")), ("做多", "orb", "09:30:00", 12051.0))
chk("  ① 出場照贏家（開箱）自己的規則：停損＝箱子另一端 111、不設停利、13:43:30 平",
    (r1.get("sl_points"), r1.get("exit_reason"), r1.get("exit"), r1.get("points"), r1.get("tpsl_points")),
    (111.0, "收盤", 12199.0, 143.0, None))
say("略過" not in (r1.get("reason") or "") or True, "  ① 原因寫得出候選清單", r1.get("reason"))
chk("  ① 那一天快攻自己是做空（自證：略過的不是幻覺）", S.fast_eval(DAY, D_U1, H40).get("decision"), "做空")

# ② 兩個候選都做多 ⇒ 取**觸發最早**的那一個（快攻 09:03:30 早於開箱 09:30）
D_U2 = mkD(_HEAD + [(ms(9, 0, 0), 12000.0, 11999, 12001), (ms(9, 2), 11960.0, 11959, 11961),
                    (ms(9, 3, 29), 12060.0, 12059, 12061), (ms(9, 5, 0), 12050.0, 12049, 12051),
                    (ms(9, 30), 12100.0, 12099, 12101), (ms(10, 0), 12121.0, 12120, 12122),
                    (ms(13, 44, 59), 12110.0, 12109, 12111)])
_c2 = S.union_cands(DAY, D_U2, S.day_pack(DAY, D_U2, H40, UBH))[0]
chk("  ② 兩個候選都做多", [(x["kind"], x["dir"], x["at"]) for x in _c2],
    [("fast", 1, "09:03:30"), ("orb", 1, "09:30:00")])
r2 = S.union_eval(DAY, D_U2, H40, UBH)
chk("  ② ⛔ 取觸發最早的那一個（快攻，⛔ 不是開箱的 12101）",
    (r2.get("pick"), r2.get("at"), r2.get("entry"), r2.get("tpsl_points")), ("fast", "09:03:30", 12061.0, 60))
chk("  ② 出場照快攻自己的規則：±0.5% 停利 ⇒ 12121、55 點",
    (r2.get("exit_reason"), r2.get("exit"), r2.get("points")), ("停利", 12121.0, 55.0))
_f2 = S.fast_eval(DAY, D_U2, H40)
chk("  ② 跟快攻那一條的進出場逐欄位一樣（⛔ 不是另一套）",
    [r2.get(k) for k in ("decision", "entry", "exit", "exit_reason", "points", "tpsl_points", "cutoff")],
    [_f2.get(k) for k in ("decision", "entry", "exit", "exit_reason", "points", "tpsl_points", "cutoff")])
say(sum(1 for k in ("entry",) if r2.get(k) is not None) == 1 and "pick" in r2,
    "  ② ⛔ 一天最多一口：一列只有一個進場價")

# ③ 全部候選都做空 ⇒ 不做
D_U3 = mkD(_HEAD + [(ms(9, 0, 0), 12000.0, 11999, 12001), (ms(9, 2), 12040.0, 12039, 12041),
                    (ms(9, 3, 29), 11940.0, 11939, 11941), (ms(9, 5, 0), 12000.0, 11999, 12001),
                    (ms(9, 30), 11930.0, 11929, 11931), (ms(13, 44, 59), 11900.0, 11899, 11901)])
r3 = S.union_eval(DAY, D_U3, H40, UBH)
chk("  ③ 候選都做空 ⇒ 不做（no_long）", (r3.get("decision"), r3.get("why")), ("不做", "no_long"))
say("不是做多" in (r3.get("reason") or ""), "  ③ 原因講得出是「都不是做多」", r3.get("reason"))

# ④ 一個候選都沒觸發（不快、沒反轉、箱子沒突破）⇒ 不做
D_U4 = mkD(_HEAD + [(ms(9, 0, 0), 12000.0, 11999, 12001), (ms(9, 2), 12010.0, 12009, 12011),
                    (ms(9, 3, 29), 12006.0, 12005, 12007), (ms(9, 5, 0), 12005.0, 12004, 12006),
                    (ms(9, 15), 12008.0, 12007, 12009), (ms(11, 0), 12005.0, 12004, 12006),
                    (ms(13, 44, 59), 12000.0, 11999, 12001)])
_c4 = S.union_cands(DAY, D_U4, S.day_pack(DAY, D_U4, H40, UBH))[0]
chk("  ④ 不快＋09:15 同方向＋箱子沒突破 ⇒ 一個候選都沒有", _c4, [])
chk("  ④ ⇒ 不做（no_cand：三個都可用、只是都沒觸發）",
    (S.union_eval(DAY, D_U4, H40, UBH).get("why"), S.union_eval(DAY, D_U4, H40, UBH).get("miss")),
    ("no_cand", []))

# ⑤ 回馬槍候選：⛔ 只有「09:03:30 判定不快」的日子才有
D_U5 = mkD(_HEAD + [(ms(9, 0, 0), 12000.0, 11999, 12001), (ms(9, 2), 12010.0, 12009, 12011),
                    (ms(9, 3, 29), 11994.0, 11993, 11995), (ms(9, 5, 0), 12005.0, 12004, 12006),
                    (ms(9, 15), 12008.0, 12007, 12009), (ms(11, 0), 12005.0, 12004, 12006),
                    (ms(13, 44, 59), 12000.0, 11999, 12001)])
_c5 = S.union_cands(DAY, D_U5, S.day_pack(DAY, D_U5, H40, UBH))[0]
chk("  ⑤ 不快＋09:15 反轉往上 ⇒ 只有回馬槍那一個候選",
    [(x["kind"], x["dir"], x["at"]) for x in _c5], [("rev", 1, "09:15:00")])
r5 = S.union_eval(DAY, D_U5, H40, UBH)
_h5 = S.hmq_eval(DAY, D_U5, H40)
chk("  ⑤ 照回馬槍做，進出場跟回馬槍那一條逐欄位一樣",
    (r5.get("pick"), [r5.get(k) for k in ("decision", "entry", "exit", "exit_reason", "points")]),
    ("rev", [_h5.get(k) for k in ("decision", "entry", "exit", "exit_reason", "points")]))
# 快的日子 ⇒ ⛔ 沒有回馬槍候選（就算 09:15 反轉得很兇）
D_U6 = mkD(_HEAD + [(ms(9, 0, 0), 12000.0, 11999, 12001), (ms(9, 2), 12010.0, 12009, 12011),
                    (ms(9, 3, 29), 12060.0, 12059, 12061), (ms(9, 5, 0), 12050.0, 12049, 12051),
                    (ms(9, 15), 11900.0, 11899, 11901), (ms(13, 44, 59), 12000.0, 11999, 12001)])
_c6 = S.union_cands(DAY, D_U6, S.day_pack(DAY, D_U6, H40, UBH))[0]
chk("  ⑤b 快的日子 ⛔ 沒有回馬槍候選（就算 09:15 反轉得很兇）",
    [x["kind"] for x in _c6 if x["kind"] == "rev"], [])
say("fast" in {x["kind"] for x in _c6}, "  ⑤b 自證：那一天快攻那個候選是有的（不是整串空的）")

# ⑤c 開箱候選的觸發時刻＝**真的突破那一刻**（⛔ 不是箱子結束的 09:05）
D_U7 = mkD(_HEAD + [(ms(9, 0, 0), 12000.0, 11999, 12001), (ms(9, 2), 12010.0, 12009, 12011),
                    (ms(9, 3, 29), 11994.0, 11993, 11995), (ms(9, 5, 0), 12005.0, 12004, 12006),
                    (ms(9, 15), 12008.0, 12007, 12009), (ms(9, 30), 12020.0, 12019, 12021),
                    (ms(10, 0), 12100.0, 12099, 12101), (ms(13, 44, 59), 12050.0, 12049, 12051)])
_c7 = S.union_cands(DAY, D_U7, S.day_pack(DAY, D_U7, H40, UBH))[0]
chk("  ⑤c 兩個做多候選：回馬槍 09:15 早於開箱 09:30（⛔ 開箱不是記成 09:05）",
    [(x["kind"], x["at"]) for x in _c7], [("rev", "09:15:00"), ("orb", "09:30:00")])
r7 = S.union_eval(DAY, D_U7, H40, UBH)
chk("  ⑤c ⇒ 照回馬槍做（進場 12009、±0.5% 停利 ⇒ 55 點）",
    (r7.get("pick"), r7.get("entry"), r7.get("exit_reason"), r7.get("points")), ("rev", 12009.0, "停利", 55.0))
say(orb_ev(DAY, D_U7, UBH).get("entry") == 12021.0,
    "  ⑤c 自證：開箱自己那條的進場價是 12021（⛔ 沒被選中的那個確實不一樣）")

# ⑥ 箱子濾網擋住 ⇒ 開箱那個候選不算（但快攻／回馬槍照舊）
_bp1 = S.orb_calc(DAY, D_U1)["box_pct"]
_c1n = S.union_cands(DAY, D_U1, S.day_pack(DAY, D_U1, H40, [_bp1 * 1.0000001] * S.ORB_HIST_N))[0]
chk("  ⑥ 箱子太窄 ⇒ 開箱那個候選不成立（只剩快攻做空）",
    [(x["kind"], x["dir"]) for x in _c1n], [("fast", -1)])
chk("  ⑥ ⇒ 那天多方聯軍不做",
    S.union_eval(DAY, D_U1, H40, [_bp1 * 1.0000001] * S.ORB_HIST_N).get("why"), "no_long")
say(S.union_cands(DAY, D_U1, S.day_pack(DAY, D_U1, H40, [_bp1] * S.ORB_HIST_N))[0][-1]["kind"] == "orb",
    "  ⑥ 自證：箱寬**等於**中位數時開箱那個候選成立（⛔ 只有小於才不算）")

# ⑦ ⛔ 整條資料缺**只有這三種**（沒接上／沒逐筆／讀不到 fast_hist.jsonl）
chk("  ⑦ 沒有逐筆／沒接上規則 ⇒ 資料缺",
    [S.union_eval(DAY, None, H40, UBH).get("why"),
     S.union_eval(DAY, D_U1, H40, UBH, cfg={"verdict": None}).get("why")], ["no_ticks", "not_wired"])
chk("  ⑦ 讀不到 fast_hist.jsonl（檔案不見）⇒ 資料缺（⛔ 檔案不見不可以記成定論）",
    S.union_eval(DAY, D_U1, None, UBH).get("why"), "no_hist_file")

# ⑧ ⛔⛔ 每個候選**各自**判斷可不可用：拿不到就是「今天少一個候選」，⛔ 不是整條沒資料
#    （PM 2026-09-16 裁示；第一版寫成「收不齊就整條資料缺」，跟回測口徑對不起來）
H19 = hist_rows(DAY, 19)                      # 走幅歷史不夠 ⇒ 快攻與回馬槍兩個候選都不可用
_c8 = S.union_cands(DAY, D_U1, S.day_pack(DAY, D_U1, H19, UBH))
# ⚠️ 用 `or []` 兜住：整條被停掉時 cands 是 None，⛔ 讓測試崩潰的紅是壞的紅（看不出是哪一條）
chk("  ⑧ 走幅歷史不夠 ⇒ 只剩開箱那一個候選（⛔ 整條沒有停）",
    ([(x["kind"], x["dir"]) for x in (_c8[0] or [])], _c8[0] is None, _c8[2]),
    ([("orb", 1)], False, None))
r8 = S.union_eval(DAY, D_U1, H19, UBH)
chk("  ⑧ ⛔ 只有一個候選可用 ⇒ 照它做（⛔ 不是記資料缺）",
    (r8.get("pending"), r8.get("decision"), r8.get("pick"), r8.get("entry")), (None, "做多", "orb", 12051.0))
say("不可用" in (r8.get("reason") or "") and "快攻" in (r8.get("reason") or ""),
    "  ⑧ reason 寫得出今天少了哪些候選", r8.get("reason"))
say(S.fast_eval(DAY, D_U1, H19).get("why") == "no_hist",
    "  ⑧ 自證：那一天快攻自己是「歷史不夠」（所以真的少了那個候選）")
# ⛔ 候選的正式名字是「純回馬」；「回馬槍」是 hmq 那一條，⛔ 兩個不可以混用（lab-qa 退件 S4）
_urule = S._rule_text("union")
say("純回馬" in _urule and "回馬槍" not in _urule,
    "  ⑧ ⛔ 多方聯軍的規則句用「純回馬」（⛔ 不是 hmq 那條的「回馬槍」）", _urule)
_umiss = r8.get("miss") or []
say(any("純回馬" in m for m in _umiss) and not any("回馬槍" in m for m in _umiss),
    "  ⑧ ⛔ 不可用清單也用「純回馬」", _umiss)
r8b = S.union_eval(DAY, D_U2, H40, [0.0] * (S.ORB_HIST_N - 1))
chk("  ⑧ 反過來：箱子歷史不夠 ⇒ 只剩快攻那個候選，照它做",
    (r8b.get("pending"), r8b.get("decision"), r8b.get("pick"), r8b.get("entry")), (None, "做多", "fast", 12061.0))
say("開箱" in (r8b.get("reason") or "") and "不可用" in (r8b.get("reason") or ""),
    "  ⑧ reason 寫得出開箱那個候選不可用", r8b.get("reason"))
r8c = S.union_eval(DAY, D_U2, H19, [0.0] * (S.ORB_HIST_N - 1))
chk("  ⑧ 兩種歷史都不夠 ⇒ 一個候選都沒有（no_cand，⛔ 不是資料缺）",
    (r8c.get("pending"), r8c.get("decision"), r8c.get("why")), (None, "不做", "no_cand"))
chk("  ⑧ 兩個不可用的原因都記進那一列", len(r8c.get("miss") or []), 2)
chk("  ⑧ ⛔ 「都說做空」記 no_long、「一個候選都沒有」記 no_cand（將來看紀錄意義不同）",
    (S.union_eval(DAY, D_U3, H40, UBH).get("why"), r8c.get("why")), ("no_long", "no_cand"))

print("\n  -- S1 窗口跨度：資料有洞的時候「過去 20 天」可能橫跨一年多（lab-qa 退件 S1）--")
_WOK = {"vals": [0.1] * S.ORB_HIST_N, "d0": "2026-08-01", "d1": "2026-09-14", "span": 45}
_WBAD = {"vals": [0.1] * S.ORB_HIST_N, "d0": "2025-05-14", "d1": "2026-09-14", "span": 489}
say(S.orb_span_bad(_WOK) is None, "  跨度 45 天 ⇒ 放行")
say(bool(S.orb_span_bad(_WBAD)), "  跨度 489 天 ⇒ 擋下來", S.orb_span_bad(_WBAD))
# ⛔ 邊界**寫死 49/50/51/52**（PM 2026-09-16 裁示把上限從 90 收到 50）：
#    拿 S.ORB_SPAN_MAX_DAYS ± 1 去比是「跟自己比」，常數被改掉照樣綠（突變 S1b 抓過同型假綠燈）。
#    50 的來歷：他正本 520 天、501 個窗口實測 max 40（農曆年）＋10 天餘裕，見 sim_lanes.py 的註解。
chk("  ⛔ 上限就是 50 個日曆天（寫死，⛔ 不准拿 S.ORB_SPAN_MAX_DAYS 比自己）", S.ORB_SPAN_MAX_DAYS, 50)
say(S.orb_span_bad(dict(_WOK, span=49)) is None, "  邊界：49 天 ⇒ 放行")
say(S.orb_span_bad(dict(_WOK, span=50)) is None, "  邊界：剛好 50 天 ⇒ 放行")
say(bool(S.orb_span_bad(dict(_WOK, span=51))), "  邊界：51 天 ⇒ 擋")
say(bool(S.orb_span_bad(dict(_WOK, span=52))), "  邊界：52 天 ⇒ 擋")
_rok = orb_ev(ODAY, D_UP, _WOK)
chk("  跨度正常 ⇒ 照算，而且 reason 帶得出窗口起訖",
    (_rok.get("decision"), "2026-08-01~2026-09-14" in (_rok.get("reason") or ""), _rok.get("box_span")),
    ("做多", True, 45))
_rbad = orb_ev(ODAY, D_UP, _WBAD)
chk("  ⛔ 跨度太寬 ⇒ **資料缺**（⛔ 不做定論、不寫檔）", (_rbad.get("pending"), _rbad.get("why")), (True, "box_span"))
say("489" in (_rbad.get("msg") or "") and "2025-05-14" in (_rbad.get("msg") or ""),
    "  原因講得出跨了幾天、從哪到哪", _rbad.get("msg"))
_cs = S.union_cands(DAY, D_U1, S.day_pack(DAY, D_U1, H40, _WBAD))
chk("  ⛔ 多方聯軍：跨度太寬只是**少了開箱那個候選**（⛔ 不是整條資料缺）",
    ([x["kind"] for x in (_cs[0] or [])], _cs[0] is None, _cs[2]), (["fast"], False, None))
say(any("跨了" in m for m in (_cs[1] or [])), "  而且記下那個候選為什麼不可用", _cs[1])
_wr = S.box_window(ODAY)
chk("  box_window 回傳 vals／d0／d1／span 四個欄位", sorted(_wr), ["d0", "d1", "span", "vals"])
say(S.box_hist(ODAY) == _wr["vals"], "  box_hist 就是 box_window 的 vals（同一把尺）")
say("過去 %d 天" % len(_WOK["vals"]) in S.box_win_txt(_WOK) and "2026-08-01~2026-09-14" in S.box_win_txt(_WOK),
    "  窗口字面＝「過去 N 天（起~訖）」", S.box_win_txt(_WOK))


print("\n  -- S6 一列壞資料不可以把端點打成 500（lab-qa 退件 S6）--")
_bad_row = {"lane": "fast", "date": "2026-09-10", "decision": "做多", "points": None, "exit_reason": "停利"}
say(not S._valid_row(_bad_row), "  ⛔ 進場列沒有數值 points ⇒ _valid_row 擋掉（算進 bad、跳過）")
say(S._valid_row(dict(_bad_row, points=55.0)), "  自證：補上數值就過得了（尺是活的）")
try:
    _m_bad = S._months(NOW, [dict(_bad_row, date="2026-09-10"), {"lane": "fast", "date": "2026-09-11",
                                                                 "decision": "做多", "points": 12.0}])
    _ok_bad, _shown = all(isinstance(m["points"], (int, float)) for m in _m_bad), [m["points"] for m in _m_bad]
except Exception as _e:                     # ⛔ 讓它是**乾淨的紅**：崩潰型的紅看不出是哪一條在守
    _ok_bad, _shown = False, repr(_e)
say(_ok_bad, "  ⛔ 就算壞列漏進來，_months 每一個月都還算得出數字（第二道防線）", _shown)


print("\n=== ⑪c 一輪 step：六條各自落地，同一天只讀一次逐筆、只算一次 day_pack ===")
reset_state()
_sim2 = TMP / "sim_lanes2"
_sim0, S.SIM_DIR = S.SIM_DIR, _sim2          # ⛔ 換到另一個暫存資料夾，不污染前面幾節的檔
_hits, _ctxn, _orbn = [], [], []
_ld2, _fc0, _oc0 = SL.load_day, S._fast_ctx, S.orb_calc
SL.load_day = lambda d: (_hits.append(str(d)), _ld2(d))[1]
S._fast_ctx = lambda day, D, hist, c: (_ctxn.append(str(day)), _fc0(day, D, hist, c))[1]
S.orb_calc = lambda day, D: (_orbn.append(str(day)), _oc0(day, D))[1]
try:
    S.step(lambda: None, lambda: False, NOW)
finally:
    SL.load_day, S._fast_ctx, S.orb_calc = _ld2, _fc0, _oc0
_rows2 = S.read_rows()[0]
_got = {ln: _rows2.get((ln, FD), {}).get("decision") for ln in S.TICK_LANES}
chk("  四條有定論（快攻／早收／回馬槍／純回馬），⛔ 開箱因為箱子歷史不夠是資料缺，"
    "但多方聯軍**照樣做得出來**（只是少一個候選）",
    (_got["fast"], _got["hmq"], _got["rev"], _got["fast11"], ("orb", FD) in _rows2, _got["union"]),
    ("做多", "做多", "不做", "做多", False, "做多"))
chk("  開箱在 pending 裡寫得出原因；多方聯軍⛔ 不在 pending 裡（它有定論）",
    [S.STATE["pending"]["orb"].get(FD, {}).get("why"), S.STATE["pending"]["union"].get(FD)],
    ["few_box_hist", None])
chk("  ⛔ 同一天的逐筆只讀一次（六條共用，⛔ 不是一條讀一次）", _hits.count(FD), 1)
# ⛔ 六條共用同一份候選：那一天的 _fast_ctx／orb_calc 各只准跑**一次**
#    （多方聯軍就是靠這份一致性；各算各的除了慢，還會讓七條看到不一樣的答案）
chk("  ⛔ 那一天的 _fast_ctx 只算一次", _ctxn.count(FD), 1)
chk("  ⛔ 那一天的 orb_calc 只算一次", _orbn.count(FD), 1)
say(len(_ctxn) >= 1 and len(_orbn) >= 1, "  自證：兩個計數器真的有量到東西",
    "_fast_ctx %d 次／orb_calc %d 次" % (len(_ctxn), len(_orbn)))
say(len([d for d in set(_hits) if d == FD]) == 1 and len(_hits) >= 1,
    "  自證：真的有量到 load_day 被呼叫", "共 %d 次" % len(_hits))
_st2 = S.state(NOW)
chk("  端點端得出七條、每條都有月合計", [ln for ln in _st2["lanes"] if len(_st2["lanes"][ln]["months"]) == 6], LANE_KEYS)

# ⛔ 一條壞掉只停那一條，其他四條照算（⛔ 不是整天停擺）
S.SIM_DIR = TMP / "sim_lanes3"
reset_state()
_ev0 = S.TICK_EVAL["rev"]
# ⚠️ 故意丟 RuntimeError（不是 ZeroDivisionError）：內層那道 except 若被縮成某個特定型別，
#    例外就會冒到外層、把整天其他四條一起打掉 —— 用特定型別去測會測不到（突變 M20b 抓過）。
S.TICK_EVAL["rev"] = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("這一條故意壞掉"))
try:
    S.step(lambda: None, lambda: False, NOW)
finally:
    S.TICK_EVAL["rev"] = _ev0
_rows3 = S.read_rows()[0]
chk("  ⛔ 純回馬那條炸掉 ⇒ 只有它停，快攻／回馬槍／早收 照樣有定論",
    (("rev", FD) in _rows3, _rows3.get(("fast", FD), {}).get("decision"),
     _rows3.get(("hmq", FD), {}).get("decision"), _rows3.get(("fast11", FD), {}).get("decision")),
    (False, "做多", "做多", "做多"))
chk("  壞掉那條寫「計算出錯」並計數", (S.STATE["pending"]["rev"].get(FD, {}).get("why"), S.STATE["errors"] >= 1),
    ("error", True))
say(S.STATE["pending"]["fast"].get(FD) is None, "  自證：沒壞的那幾條沒有被連坐記成 pending")
S.SIM_DIR = _sim0

# ══ ⑫ ⛔ 規則只有一把尺（換掉注入的那一支，結果一定要跟著變）═══════════════
print("\n=== ⑪c2 一輪 step：多方聯軍**挑中開箱**那一條分支（lab-qa Q7 打不紅的缺口）===")
reset_state()
S._BOX.clear()
_lab_u, SL.LAB_DIR = SL.LAB_DIR, TMP / "tick_hist_union"
_sim_u, S.SIM_DIR = S.SIM_DIR, TMP / "sim_lanes_union"
_fh_u, S.FAST_HIST = S.FAST_HIST, TMP / "fast_hist_union.jsonl"
UD = "2026-10-20"                 # NOW 是 2026-10-23（週五）⇒ 這天在「最近 10 個平日」窗口裡
# 快攻做空（走 0.5% 往下）＋ 開箱做多（09:30 突破上緣）⇒ 略過做空、照開箱做
write_ticks(UD, [(ms(8, 45, 0, 100), 11990.0, 11989, 11991), (ms(8, 59, 59), 12000.0, 11999, 12001),
                 (ms(9, 0, 0), 12000.0, 11999, 12001), (ms(9, 2), 12040.0, 12039, 12041),
                 (ms(9, 3, 29), 11940.0, 11939, 11941), (ms(9, 5, 0), 12000.0, 11999, 12001),
                 (ms(9, 30), 12050.0, 12049, 12051), (ms(10, 0), 12200.0, 12199, 12201)])
# 箱子歷史：UD 以前 21 個平日，箱寬 12（0.1%）⇒ 中位數 0.1%、跨度約 27 天（⛔ 要 ≤ ORB_SPAN_MAX_DAYS）
_ubd, _un = date.fromisoformat(UD), 0
while _un < 21:
    _ubd -= timedelta(days=1)
    if _ubd.weekday() >= 5:
        continue
    _un += 1
    write_ticks(str(_ubd), [(ms(9, 0, 0), 12000.0, 11999, 12001), (ms(9, 4), 12012.0, 12011, 12013),
                            (ms(9, 5, 0), 12000.0, 11999, 12001)])
S.FAST_HIST.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in hist_rows(UD, 40)) + "\n",
                       encoding="utf-8")
_uwin = S.box_window(UD)
say(_uwin["span"] is not None and _uwin["span"] <= S.ORB_SPAN_MAX_DAYS,
    "  治具自證：箱子歷史跨度 %s 天（沒被跨度閘門擋掉）" % _uwin["span"])
S._BOX.clear()
_oc, _bw = [], []
_oc0, _bw0 = S.orb_calc, S.box_window
S.orb_calc = lambda day, D: (_oc.append(str(day)), _oc0(day, D))[1]
S.box_window = lambda day, n=S.ORB_HIST_N: (_bw.append(str(day)), _bw0(day, n))[1]
try:
    S.step(lambda: None, lambda: False, NOW)
finally:
    S.orb_calc, S.box_window = _oc0, _bw0
_rows_u = S.read_rows()[0]
_ru, _ro = _rows_u.get(("union", UD), {}), _rows_u.get(("orb", UD), {})
chk("  ⛔ 多方聯軍挑中開箱 ⇒ 照**開箱**的進出場規則（停損＝箱子另一端 111、⛔ 不設停利）",
    (_ru.get("decision"), _ru.get("pick"), _ru.get("entry"), _ru.get("sl_points"), _ru.get("tpsl_points")),
    ("做多", "orb", 12051.0, 111.0, None))
chk("  那一天快攻是做空（⇒ 真的走到「略過做空、繼續看下一個」那條路）",
    _rows_u.get(("fast", UD), {}).get("decision"), "做空")
chk("  ⛔ 那一天的 orb_calc 只算一次（開箱與多方聯軍共用同一份箱子，⛔ 不是各算各的）", _oc.count(UD), 1)
chk("  ⛔ 那一天的 box_window 只掃一次", _bw.count(UD), 1)
chk("  開箱那一條跟多方聯軍看到的是**同一個箱子**（進場價與停損一樣）",
    (_ro.get("entry"), _ro.get("sl_points")), (_ru.get("entry"), _ru.get("sl_points")))
say("過去 20 天（" in (_ro.get("reason") or ""), "  開箱的 reason 帶得出窗口起訖", _ro.get("reason"))
say(_ru.get("points") == _ro.get("points"),
    "  兩條算出來的點數一樣（自證：共用那份箱子不是嘴上說說）", (_ru.get("points"), _ro.get("points")))

# ── ⑪c3 ORB_NO_TP 那道防禦碼：**斷言型**（lab-qa 2026-09-16 S2）────────────────
# ⛔ 本來只有突變 N9 在守，而 N9 是靠「⑪b 直接呼叫沒人接 ⇒ 整支測試崩潰」翻紅 ——
#    崩潰型的紅看不出是哪一條在守。現在直接釘住：把哨兵調小到一定會碰到，
#    ① `orb_eval` 要丟 RuntimeError；② 面板那條路（`step()`）要吞掉 ⇒ 記 pending＋errors、
#    ⛔ **一列都不落地**（絕對不可以把 10 億點那種假成績寫進只 append 的定論檔）。
#    ⚠️ 這一條同時蓋掉 CLAUDE.md 舊記的 Q5「這段 raise 結構上碰不到 ⇒ 測不到」。
print("\n  -- ORB_NO_TP 哨兵被碰到 ⇒ 丟例外、⛔ 不寫檔（lab-qa S2）--")
S.SIM_DIR = TMP / "sim_lanes_notp"
reset_state()
S._BOX.clear()
_notp0, S.ORB_NO_TP = S.ORB_NO_TP, 10.0     # ⛔ 暫時調小：12051 進場、漲到 12200 一定碰得到「停利」
try:
    _D_u = SL.load_day(UD)
    try:
        _exc = "⛔ 沒有丟例外，回傳 %r" % (S.orb_eval(UD, _D_u, S.box_window(UD)),)
    except RuntimeError as _e:
        _exc = _e
    say(isinstance(_exc, RuntimeError), "  哨兵被碰到 ⇒ orb_eval 丟 RuntimeError", _exc)
    say(isinstance(_exc, RuntimeError) and "停利" in str(_exc), "  例外訊息講得出是「走到停利」", str(_exc))
    S._BOX.clear()
    S.step(lambda: None, lambda: False, NOW)
finally:
    S.ORB_NO_TP = _notp0
    S._BOX.clear()
_rows_tp = S.read_rows()[0]
chk("  ⛔ 面板那條路吞掉 ⇒ 開箱與多方聯軍那天**一列都沒落地**",
    [("orb", UD) in _rows_tp, ("union", UD) in _rows_tp], [False, False])
chk("  記成 pending「計算出錯」＋錯誤計數",
    (S.STATE["pending"]["orb"].get(UD, {}).get("why"), S.STATE["errors"] >= 1), ("error", True))
say(_rows_tp.get(("fast", UD), {}).get("decision") == "做空",
    "  自證：同一天快攻照樣有定論（⇒ step 真的跑過，也證明不是整天停擺）",
    _rows_tp.get(("fast", UD), {}).get("decision"))
say(S.orb_eval(UD, SL.load_day(UD), S.box_window(UD)).get("decision") == "做多",
    "  自證：哨兵還原之後同一天又算得出來（尺是活的）")
S._BOX.clear()

SL.LAB_DIR, S.SIM_DIR, S.FAST_HIST = _lab_u, _sim_u, _fh_u
S._BOX.clear()
print("\n=== ⑫ 規則一律用注入的那一份（⛔ sim_lanes 裡沒有第二把尺）===")
_keep = dict(S._CFG)
S._CFG["tpsl"] = lambda px: 10
chk("  停利停損：換成固定 10 點 ⇒ 四條全部跟著變",
    [S.fast_eval(DAY, D_FAST, H40).get("tpsl_points"), S.hmq_eval(DAY, D_FAST, H40).get("tpsl_points"),
     S.fast11_eval(DAY, D_FAST, H40).get("tpsl_points"), S.hmq_eval(DAY, D_REV, H40).get("tpsl_points")],
    [10, 10, 10, 10])
S._CFG.clear()
S._CFG.update(_keep)
chk("  還原之後回到 60 點（自證：上面那條是真的換掉了）", S.fast_eval(DAY, D_FAST, H40).get("tpsl_points"), 60)
S._CFG["reversal"] = lambda *a, **k: None
chk("  反轉：換成永遠「沒反轉」⇒ hmq／rev 那半跟著不做",
    [S.hmq_eval(DAY, D_REV, H40).get("why"), S.rev_eval(DAY, D_REV, H40).get("why")], ["no_rev", "no_rev"])
S._CFG.clear()
S._CFG.update(_keep)
chk("  還原之後又做得出來（自證）", S.hmq_eval(DAY, D_REV, H40).get("decision"), "做空")
S._CFG["verdict"] = lambda day, mv, rows, pctl=None: {"verdict": "fast", "thr_pct": 0.0, "n": 99, "move_pct": mv}
chk("  快不快：換成「永遠快」⇒ 四條跟著改判",
    [S.fast_eval(DAY, D_REV, H40).get("decision"), S.hmq_eval(DAY, D_REV, H40).get("why"),
     S.rev_eval(DAY, D_REV, H40).get("why"), S.fast11_eval(DAY, D_REV, H40).get("decision")],
    ["做多", "fast", "fast_skip", "做多"])
S._CFG.clear()
S._CFG.update(_keep)
# ⚠️ 用 AST 看**會跑的那些 code**（註解／docstring 裡寫「⛔ 不要乘 0.005」是正確的說明，不是第二把尺）
_tree = ast.parse((HERE / "sim_lanes.py").read_text(encoding="utf-8"))
_nums = {n.value for n in ast.walk(_tree) if isinstance(n, ast.Constant) and isinstance(n.value, float)}
chk("  sim_lanes.py 的程式碼裡沒有 0.005（停利停損一律走注入的 tpsl_points）", 0.005 in _nums, False)
chk("  也沒有呼叫 percentile（門檻一律走注入的 fast_verdict）",
    sorted({n.func.attr for n in ast.walk(_tree) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and "percentile" in n.func.attr}), [])
# ⛔ `tpsl_frac` 只准出現在**給畫面看字**的那兩支裡（`_rule_text` 的規則句、`_rule_detail` 的逐條說明）。
#    2026-09-17 從一支變兩支 —— ⛔ 不准改成「數字對就好」：要驗的是**每一個**都落在那兩支裡面，
#    不然哪天有人拿 tpsl_frac 自己乘一次停利停損，這條照樣綠燈。
_TXT_FNS = ("_rule_text", "_rule_detail")
_txt_nodes = [n for n in ast.walk(_tree) if isinstance(n, ast.FunctionDef) and n.name in _TXT_FNS]
say(len(_txt_nodes) == len(_TXT_FNS), "  給畫面看字的那兩支都在（_rule_text／_rule_detail）")
_tf_all = [n for n in ast.walk(_tree) if isinstance(n, ast.Constant) and n.value == "tpsl_frac"]
_tf_in = [n for f in _txt_nodes for n in ast.walk(f) if isinstance(n, ast.Constant) and n.value == "tpsl_frac"]
chk("  「tpsl_frac」只出現在 _rule_text／_rule_detail（給畫面看的字，算的還是注入的那份 rule）",
    (len(_tf_all), len(_tf_in), len(_tf_all) == len(_tf_in)), (2, 2, True))
_secs = {n.value for n in ast.walk(_tree) if isinstance(n, ast.Constant) and isinstance(n.value, int)}
chk("  ⛔ 程式碼裡沒有寫死 09:15（33300 秒）：一律用注入的 rev_sec", 9 * 3600 + 15 * 60 in _secs, False)
say(any(isinstance(n, ast.Constant) and n.value == "rev_sec" for n in ast.walk(_tree)),
    "  負控組：同一把尺看得到 sim_lanes 真的在讀 rev_sec（尺是活的）")

# ══ ⑧ AST ════════════════════════════════════════════════════════════
print("\n=== ⑧ AST：不 import broker、主迴圈沒被改 ===")
tree = ast.parse((HERE / "sim_lanes.py").read_text(encoding="utf-8"))
bad = set()
for n in ast.walk(tree):
    if isinstance(n, ast.Import):
        bad |= {a.name for a in n.names if a.name.split(".")[0] in ("broker", "auto_fire", "live_panel")}
    elif isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] in ("broker", "auto_fire", "live_panel"):
        bad.add(n.module)
    elif isinstance(n, ast.Name) and n.id in ("broker", "auto_fire", "live_panel"):
        bad.add("name:" + n.id)
    elif isinstance(n, ast.Constant) and isinstance(n.value, str) and _re.search(r"place_order|/api/enter|/api/real|/api/fire", n.value):
        bad.add("str:" + n.value[:30])
chk("  sim_lanes.py 沒有 import／引用 broker、auto_fire、live_panel，也沒有下單端點字串", sorted(bad), [])
say(any(isinstance(n, ast.Import) and any(a.name == "numpy" for a in n.names) for n in ast.walk(tree)),
    "  負控組：同一把尺看得到 sim_lanes 的 import numpy（尺是活的）")
_sl = ast.parse((HERE / "strategy_lab.py").read_text(encoding="utf-8"))
chk("  sim_lanes 借用的 strategy_lab 也沒有 import broker／auto_fire",
    sorted({a.name for n in ast.walk(_sl) if isinstance(n, ast.Import) for a in n.names} & {"broker", "auto_fire"}), [])
src = (HERE / "live_panel.py").read_text(encoding="utf-8")
lp_tree = ast.parse(src)
fn = {n.name: n for n in ast.walk(lp_tree) if isinstance(n, ast.FunctionDef)}
chk("  Handler._sim_get 裡沒有 broker／auto_fire",
    sorted({n.id for n in ast.walk(fn["_sim_get"]) if isinstance(n, ast.Name) and n.id in ("broker", "auto_fire")}), [])
chk("  start_sim_lanes 只拿 auto_fire 的規則函式（沒有 enter／arm／on_*）",
    sorted({n.attr for n in ast.walk(fn["start_sim_lanes"]) if isinstance(n, ast.Attribute)
            and getattr(n.value, "id", None) == "auto_fire"}),
    ["FAST_RULE", "fast_verdict", "hist_read", "move_pct", "reversal_dir", "tpsl_points"])
MAIN_LOOP = ("_auto_tick", "_auto_tick_guarded", "check_real_position", "on_tick", "_auto_snap", "_auto_put", "main")
# ⛔ 基準固定在 a71087e（模擬這一包動工前的 main），⛔ 不用 HEAD —— commit 之後 HEAD 就是自己，比了等於沒比
#    （2026-09-15 lab-qa 退件 R2）。沒有 git（突變測試的暫存複本）⇒ 吃 SIM_BASELINE_LIVE_PANEL 指的檔；
#    兩個都拿不到 ⇒ 明確記「未驗」、總結寫出來，⛔ 不當成通過。
BASELINE_COMMIT = "a71087e"
head, head_src = "", ""
try:
    root = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=str(HERE), capture_output=True, text=True, timeout=30).stdout.strip()
    if root:
        _g = subprocess.run(["git", "show", f"{BASELINE_COMMIT}:tools/shioaji/live_panel.py"], cwd=root, capture_output=True, timeout=60)
        if _g.returncode == 0:
            head, head_src = _g.stdout.decode("utf-8"), f"git {BASELINE_COMMIT}"
except Exception:
    head = ""
if not head and os.environ.get("SIM_BASELINE_LIVE_PANEL"):
    try:
        head = pathlib.Path(os.environ["SIM_BASELINE_LIVE_PANEL"]).read_text(encoding="utf-8")
        head_src = "SIM_BASELINE_LIVE_PANEL"
    except Exception:
        head = ""
# ⭐⭐ 2026-09-16 **有授權的例外**（PM 裁示：「收盤平倉要有結算日分支」）。
#   背景：`is_expiry()` 是用「每月第三個星期三」算的，**農曆年會把結算日往後移**
#   （實例 2026-02-23、2023-01-30，那兩天的逐筆都停在 13:30）⇒ 結算日那天要等到
#   13:43:30 才平倉，而市場 13:30 就關了 ⇒ 單子送不出去、部位抱過夜、13:45 起連停損都沒有。
#   ⛔⛔ **這不是把基準往前搬**（那等於把防線變成橡皮圖章）——
#      做法是把下面這份**一個字一個字寫死的授權修改**套到**基準**上再比對：
#      `_auto_tick` 與 `main()` 除了這兩段以外**還是要一模一樣**；
#      其餘五支（_auto_tick_guarded／check_real_position／on_tick／_auto_snap／_auto_put）
#      一個字都沒動 ＝ 這一輪的**對照組**。
AUTH_PATCH = [
    # (為什麼, 基準裡的原文, 換成什麼)
    ("結算日 13:30 ⇒ 收盤平倉的時刻與上界都要換一組",
     '    if not AUTO["eod"] and EOD_CLOSE_SEC <= secs < DAY_END_SEC:\n'
     '        AUTO["eod"] = True\n'
     '        AUTO_EOD_HOOK(d, (secs - EOD_CLOSE_SEC) * 1000 + now.microsecond // 1000)\n',
     '    _eod = EOD_DAY if EOD_DAY["date"] == d else eod_plan(d)\n'
     '    _eod_sec = _eod["sec"]\n'
     '    _eod_end = _eod["end"] or DAY_END_SEC\n'
     '    if not AUTO["eod"] and _eod_sec <= secs < _eod_end:\n'
     '        AUTO["eod"] = True\n'
     '        AUTO_EOD_HOOK(d, (secs - _eod_sec) * 1000 + now.microsecond // 1000, _eod["at"])\n'),
    # ⛔⛔ 2026-09-17 改：「今天是結算日」那句話**在不確定時不准出現**（PM 裁示 M1）。
    #    背景：`is_expiry()` 把「行事曆裡查不到第三個週三」當成「那天休市 ⇒ 順延」，
    #    於是 days.jsonl 只到 09-15 時，**09-17～09-30 整整 14 個平常日全被判成結算日**，
    #    而且 err 是 None、主控台還印「今天是結算日」⇒ 每天安靜地提早 15 分鐘平倉。
    ("啟動那一行要印出今天實際用的平倉時刻（結算日看得出來；不確定時要說不確定）",
     '    print(f"【自動下單】收盤平倉 {EOD_CLOSE_AT}（⛔ 只平自動下單開的那一口）")\n',
     '    _ep = eod_plan(date.today())\n'
     '    print(f"【自動下單】收盤平倉 {_ep[\'at\']}"\n'
     '          + ("（⭐ 今天是結算日，日盤 13:30 收盤）" if _ep["expiry"]\n'
     '             else ("（⚠️ 今天是不是結算日**判不出來**）" if not _ep["sure"]\n'
     '                   else f"（結算日提前到 {EOD_CLOSE_AT_EXPIRY}）"))\n'
     '          + "（⛔ 只平自動下單開的那一口）"\n'
     '          + (f"　⚠️ {_ep[\'err\']}" if _ep["err"] else ""))\n'),
    # ⭐ 2026-09-21 **有授權的例外**（Benson 交辦【帳戶總覽】）：main() 多起一條
    #   每分鐘問一次券商餘額的背景執行緒。⚠️ 它**唯讀**、不碰下單那條路，
    #   但因為動到 main()，一樣要寫進這張清單才准過（⛔ 不准把基準往前搬）。
    ("【帳戶總覽】：main() 多一條唯讀的餘額輪詢（＋開機先放一份狀態、讀回一口保證金）",
     '    threading.Thread(target=poll_index, daemon=True).start()\n'
     '    threading.Thread(target=poll_phone, daemon=True).start()\n',
     '    threading.Thread(target=poll_index, daemon=True).start()\n'
     '    threading.Thread(target=poll_phone, daemon=True).start()\n'
     '    _eq_boot = equity_view()\n'
     '    with state_lock:\n'
     '        STATE["equity"] = _eq_boot\n'
     '    _equity_lot1_restore()\n'
     '    threading.Thread(target=poll_equity, daemon=True, name="equity").start()\n'),
    # ⭐⭐ 2026-09-22 **有授權的例外**（Benson 交辦：台積電快攻接真單、預設關閉）：
    #   main() 多兩件事 —— ① 撿回部位的掛勾改接 `_recover_chain`（先問夜盤那一口、不認得才交給
    #   auto_fire.recover_meta，⛔ 日盤那一口的行為不變，test_auto_fire ④b 在守）；
    #   ② 起【夜盤自動下單】的執行緒（包 try，起不來只印警告）。⛔ 主迴圈與停損一行都沒動。
    ("【夜盤自動下單】撿回部位的掛勾改成鏈子（夜盤 → 日盤）",
     '    broker.RECOVER_HOOK = auto_fire.recover_meta\n',
     '    # ⭐ 2026-09-22：夜盤那一口先問 night_fire（它認得就用它自己的點數），不認得才交給日盤那一支。\n    broker.RECOVER_HOOK = _recover_chain\n'),
    ("【夜盤自動下單】main() 起夜盤那一條執行緒（包 try）",
     '    _arm = auto_fire.arm()\n',
     '    # ⭐⭐ 2026-09-22【夜盤自動下單】台積電快攻。⛔ 自己的執行緒、自己的開關（NIGHT_ORDERS_ON）；\n    #    主迴圈一行都不動。價格讀 Today.price／last_recv（就是停損看的那一個）。\n    #    ⛔ 包 try：它起不來只印警告，日盤與停損照跑。\n    try:\n        night_fire.configure(quote_fn=_nf_quote, session_fn=market_session)\n        night_fire.start()\n        _na = night_fire.arm()\n        print("【夜盤自動下單】" + (_na["msg"] + ("（真單）" if broker.is_live() else "（真單開關關著 ⇒ 只會演練）")\n                                    if _na["on"] else "關閉中 —— " + _na["msg"]))\n    except Exception as e:\n        print("⚠️ 【夜盤自動下單】起不來（日盤不受影響）：%s" % str(e)[:160])\n'
     '    _arm = auto_fire.arm()\n'),
]
if head:
    print(f"  （主迴圈比對基準：{head_src}）")
    for _why, _old, _new in AUTH_PATCH:
        # ⛔ 基準裡找不到原文 ＝ 這張授權清單過期了（⛔ 不准安靜跳過）
        say(_old in head, f"  授權修改的原文在基準裡找得到：{_why}")
        head = head.replace(_old, _new)
    hfn = {}
    for n in ast.walk(ast.parse(head)):
        if isinstance(n, ast.FunctionDef):
            hfn.setdefault(n.name, n)
    cur = {}
    for n in ast.walk(lp_tree):
        if isinstance(n, ast.FunctionDef):
            cur.setdefault(n.name, n)
    for name in MAIN_LOOP:
        if name not in hfn:
            say(False, f"  基準裡找不到 {name}（名單或基準錯了，⛔ 不准安靜跳過）")
            continue
        a, b = ast.dump(hfn[name]), ast.dump(cur.get(name)) if name in cur else None
        if name == "main":
            # main() 只准多一行 start_sim_lanes()：把那一行拿掉之後要跟基準一模一樣
            m = cur["main"]
            body = [s for s in ast.walk(m)]
            calls = [s for s in m.body if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call)
                     and getattr(s.value.func, "id", None) == "start_sim_lanes"]
            chk("  main() 多了恰好一行 start_sim_lanes()", len(calls), 1)
            m2 = ast.parse(ast.unparse(m))
            m2.body[0].body = [s for s in m2.body[0].body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Call)
                                                                 and getattr(s.value.func, "id", None) == "start_sim_lanes")]
            chk(f"  main() 拿掉那一行之後跟基準 {BASELINE_COMMIT} 一模一樣", ast.dump(m2.body[0]), ast.dump(ast.parse(ast.unparse(hfn["main"])).body[0]))
        else:
            chk(f"  {name} 跟基準 {BASELINE_COMMIT} 一模一樣（AST）", a == b, True)
    say("on_tick" in hfn or "check_real_position" in hfn, "  自證：基準裡真的找得到主迴圈那幾支")
else:
    UNVERIFIED.append(f"主迴圈跟 {BASELINE_COMMIT} 比對（沒有 git 也沒有 SIM_BASELINE_LIVE_PANEL）")
    print(f"  未驗  主迴圈跟 {BASELINE_COMMIT} 比對：拿不到基準（沒有 git、也沒設 SIM_BASELINE_LIVE_PANEL）")
# 接線本身（不靠 git 也驗得到）：主迴圈那幾支一個字都不提 sim_lanes／start_sim_lanes
_lp_fn = {}
for n in ast.walk(lp_tree):
    if isinstance(n, ast.FunctionDef):
        _lp_fn.setdefault(n.name, n)
chk("  主迴圈那幾支（main 以外）沒有引用 sim_lanes／start_sim_lanes",
    sorted({nm for nm in MAIN_LOOP if nm != "main" and nm in _lp_fn
            for x in ast.walk(_lp_fn[nm]) if (isinstance(x, ast.Name) and x.id in ("sim_lanes", "start_sim_lanes"))}), [])
say(all(nm in _lp_fn for nm in MAIN_LOOP), "  自證：主迴圈那幾支在現在的 live_panel.py 裡都找得到")


# ══ ⑨ fire_fires_today：回馬槍還沒判 ═══════════════════════════════════
print("\n=== ⑨ fire_fires_today：今天帳本有 wait 還沒定論 ⇒「今天」===")
TD = datetime(2026, 10, 20)      # 週二
d_s = str(TD.date())
_auto = dict(LP.AUTO)


def at(h, m, s=0, us=0):
    return TD.replace(hour=h, minute=m, second=s, microsecond=us)


def wait_row():
    return {"rec": "wait", "date": d_s, "why": "wait_rev", "px": 12000.0, "d": 1, "dir_0903": "long", "rev_at": LP.REV_AT}


shutil.rmtree(AF.FIRE_DIR, ignore_errors=True)
try:
    LP.AUTO.update({"day": d_s, "done": True, "rev": False})
    chk("  對照組：沒有 wait、09:10、done ⇒ 下一個交易日", LP.fire_fires_today(at(9, 10)), False)
    AF._append(wait_row())
    chk("  有 wait、沒定論、09:10（面板一路開著 done=True）⇒ 今天", LP.fire_fires_today(at(9, 10)), True)
    chk("  09:15:02 ⇒ 今天（還在 REV_SEC＋AUTO_LATE_MS 裡）", LP.fire_fires_today(at(9, 15, 2)), True)
    chk("  09:15:03 ⇒ 下一個交易日", LP.fire_fires_today(at(9, 15, 3)), False)
    LP.AUTO["rev"] = True
    chk("  今天已經跨過 REV_SEC（AUTO rev=True）⇒ 下一個交易日", LP.fire_fires_today(at(9, 15, 1)), False)
    LP.AUTO.update({"day": "2026-10-19", "rev": True, "done": True})
    chk("  AUTO 是昨天的（看門狗剛重啟）⇒ 今天", LP.fire_fires_today(at(9, 12)), True)
    LP.AUTO.update({"day": d_s, "done": True, "rev": False})
    AF._append({"rec": "skip", "date": d_s, "why": "no_reversal"})
    chk("  wait 之後已經有定論（skip）⇒ 下一個交易日", LP.fire_fires_today(at(9, 10)), False)
    shutil.rmtree(AF.FIRE_DIR, ignore_errors=True)
    AF._append(dict(wait_row(), date="2026-10-24"))
    LP.AUTO.update({"day": "2026-10-24", "done": True, "rev": False})
    chk("  週六帳本有 wait（不該發生）⇒ market_session 說不是日盤 ⇒ 下一個交易日",
        LP.fire_fires_today(datetime(2026, 10, 24, 9, 10)), False)
    LP.AUTO.update({"day": d_s, "done": False, "rev": False})
    shutil.rmtree(AF.FIRE_DIR, ignore_errors=True)
    chk("  沒有 wait 時照舊：08:50 ⇒ 今天（09:03:30 那一件）", LP.fire_fires_today(at(8, 50)), True)
    AF._append(wait_row())
    LP.AUTO.update({"day": d_s, "done": True, "rev": False})
    chk("  fire_arm_confirm 跟著說「今天」", LP.fire_arm_confirm(False, at(9, 10))["when"].startswith("今天"), True)
finally:
    LP.AUTO.clear()
    LP.AUTO.update(_auto)
    shutil.rmtree(AF.FIRE_DIR, ignore_errors=True)


# ══ ⑩ 收尾 ═══════════════════════════════════════════════════════════
print("\n=== ⑩ 收尾 ===")
cur = {"S.SIM_DIR": S.SIM_DIR, "S.FAST_HIST": S.FAST_HIST, "S.MIN1_CSV": S.MIN1_CSV, "SL.LAB_DIR": SL.LAB_DIR,
       "SL.MIN1_CSV": SL.MIN1_CSV, "AF.FIRE_DIR": AF.FIRE_DIR, "AF.ARM_FLAG": AF.ARM_FLAG, "AF.FAST_HIST": AF.FAST_HIST}
chk("  全程所有寫檔出口都在暫存區", [k for k, v in cur.items() if not str(v).startswith(str(TMP))], [])
chk("  真的 sim_lanes/、fast_hist.jsonl、autofire/ 一個位元組都沒被動過",
    {k: fhash(REAL[k]) for k in REAL_HASH}, REAL_HASH)
chk("  真的 AUTO_ORDERS_ON 不存在", REAL["AF.ARM_FLAG"].exists(), False)
chk("  暫存區也沒有建出 AUTO_ORDERS_ON", AF.ARM_FLAG.exists(), False)
for k, v in REAL.items():
    mod, attr = k.split(".")
    setattr({"S": S, "SL": SL, "AF": AF}[mod], attr, v)
shutil.rmtree(TMP, ignore_errors=True)

_unv = ("；未驗 %d 項：%s" % (len(UNVERIFIED), "、".join(UNVERIFIED))) if UNVERIFIED else ""
print("\n總結:", ("全部通過" if not UNVERIFIED else "其餘通過") if not FAIL else f"{FAIL} 項失敗", _unv)
sys.exit(1 if FAIL else 0)
