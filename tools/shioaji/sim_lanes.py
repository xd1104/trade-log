# -*- coding: utf-8 -*-
"""
【模擬】分頁的後端：七條策略每天**事後**照規則算一次、記下來。

⛔⛔ 這是**模擬**，一口單都不會送：
   ・⛔ 一行都不 import broker／auto_fire（`test_sim_lanes.py` 用 AST 在守）。
     門檻那幾支規則函式（`fast_verdict`／`move_pct`／`tpsl_points`／`reversal_dir`／`hist_read`）是
     live_panel 在 main() 用 `configure()` **注入**進來的同一份正本（⛔ 不准在這裡另寫一份
     「>= 門檻」「× 0.005」或「09:15 有沒有反轉」—— 兩把尺的話畫面說「快」、真單判「不快」，而且看不出來）。
   ・⛔ 只寫 `sim_lanes/YYYY-MM.jsonl`（已 gitignore）＋借用【策略實驗室】的 `tick_hist/`
     （補抓回來的逐筆放那裡，兩邊共用）。⛔ 不寫 autofire/、real_trades/、fast_hist.jsonl。
   ・⛔ 跟【自動下單】的真單紀錄**完全分開**：不同的檔、不同的端點（/api/sim/state）、不同的卡。
   ・⛔ 不碰 4Hz 主迴圈：全部跑在自己的 daemon 執行緒（`loop()`），任何例外吞掉但**計數＋主控台＋畫面**。

七條（lane）。⛔ 前六條都吃**同一份逐筆**，同一天只讀一次、也只建一次 `day_pack`（`_step_ticks`）：
■ fast「早盤快攻」＝真單「快攻回馬槍」的前半（口徑＝研究 search3.py／build_fast_hist.py）
  資料：tick_hist/ticks/YYYY-MM-DD.csv.gz（strategy_lab 每天 13:50~15:00 抓；缺的這裡背景補抓）
  ref＝09:00:00.000（含）以前最後一筆；px＝09:03:30.000（含）以前最後一筆（⛔ 要落在 ref 之後）
  門檻＝fast_hist.jsonl 裡**這一天以前**最近 40 列 move_pct 的 numpy.percentile(…, 80)（注入的 fast_verdict）
  快 ⇒ 方向 sign(px−ref)；進場＝px 那一筆的賣價（多）／買價（空），0 就用成交價
  停利停損＝tpsl_points(進場價)（±0.5%）；停損用觸發那筆成交價；13:43:30（結算日 13:30）前沒碰到
  ⇒ 最後一筆對手價平（＝ strategy_lab.run_bracket，cost=True）；手續費 5 點。
■ hmq「快攻回馬槍」＝真單現在跑的那一套（fast ＋ 慢的日子等 09:15 看反轉）
  快 ⇒ 跟 fast 一模一樣；**不快** ⇒ 等 09:15:00（含）以前最後一筆，方向跟 09:03:30 **相反**才順新方向做
  （反轉的判斷一律呼叫注入的 `reversal_dir`）。⛔ 一天最多一口：快的日子不會再看 09:15。
■ rev「回馬槍那一半」＝hmq 減掉 fast：**只做「慢且 09:15 反轉」**那一半，快的日子不做。
■ fast11「快攻 11:00 平」＝fast，**只把收盤平倉時刻換成 11:00:00**（先碰到停利停損一樣先出）。
■ orb「ORB 5 分＋箱子濾網」（⛔ 新規則，不在 auto_fire 裡）
  箱子＝09:00~09:05 的最高／最低，寬 w；**箱子寬度%（w ÷ 進場價 × 100）< 過去 20 個交易日該值的中位數 ⇒ 今天不做**。
  之後**第一次**突破箱子上緣 ⇒ 做多、跌破下緣 ⇒ 做空（一天最多 1 次）；**停損＝箱子另一端**、**不設停利**；
  13:43:30（結算日 13:30）平。手續費 5 點。
■ night「美股開盤順勢」（口徑＝研究 night_preview_1m.py 的 B0）
  晚上 E 的夜盤＝E 15:00 → E+1 05:00，1 分 K **標籤＝結束時間**（永豐 kbars／tmf_1min.csv 原樣，⛔ 不用
  one_min_bars()——那支已經往回挪成起始時間）。
  T＝美股開盤（E 在美國夏令 ⇒ 21:30，否則 22:30；夏令＝3 月第二個週日起到 11 月第一個週日前）
  ref＝標籤 ≤ T 最後一根收盤（正常就是標籤 T 那根）；c＝標籤 (T, T+5] 最後一根收盤（正常就是 T+5），
  那 5 分鐘少於 4 根 ⇒ 不做（研究同一條）；d＝sign(c−ref)，0 ⇒ 不做；進場＝c；
  停利停損 ±1%×c，看標籤 (T+5, 04:58] 每根的高低；**同一根兩邊都碰到算停損**；都沒碰到 ⇒ 標籤 ≤04:58
  最後一根收盤平；成本＝手續費 5 ＋ 價差 2。⚠️ 1 分 K 看不到同一分鐘誰先碰 ⇒ 只是近似（畫面寫明）。

落地 `sim_lanes/YYYY-MM.jsonl`（月份＝date 的月份）：一列＝一個（lane, date）的**定論**，只 append。
  ・「資料缺」**不是定論**、⛔ 不寫檔（只在記憶體 STATE 裡，畫面看得到原因），之後補到資料就補算。
  ・同一個（lane, date）已經有定論 ⇒ ⛔ 不重寫（寫之前在鎖裡重讀一次檔）。
  ・讀檔：壞列跳過並計數；第二列同一個（lane, date）只認第一列並計數；
    **等式**：總列數 ＝ ok ＋ bad ＋ dup ＋ blank（每一列都有去處）。
"""
import json
import re
import threading
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

import numpy as np
import pandas as pd

import strategy_lab as SL        # 逐筆讀取／run_bracket／結算日／抓取的逾時與流量上限（⛔ 那個模組也不碰下單）

HERE = Path(__file__).resolve().parent
SIM_DIR = HERE / "sim_lanes"          # ⚠️ 測試會導到暫存區；函式一律在呼叫時才讀這些常數
FAST_HIST = HERE / "fast_hist.jsonl"  # ⛔ 唯讀（真單在用的那一份，寫它的是 auto_fire）
MIN1_CSV = HERE / "tmf_1min.csv"      # ⛔ 唯讀（夜盤 1 分 K 先看本機，缺的才跟永豐要）

LANES = ("fast", "fast11", "hmq", "rev", "orb", "union", "night")
# 逐筆那六條：同一天只讀一次 tick_hist、也只算一次 day_pack（⛔ 不要一條算一次）
TICK_LANES = ("fast", "fast11", "hmq", "rev", "orb", "union")
# ⛔ 2026-09-16 Benson 定名：**面板文字一律用這些名字**。
#    lane key 刻意沒改（`sim_lanes/*.jsonl` 裡已經落地的舊資料照樣讀得到，⛔ 不做資料遷移）。
LANE_NAME = {"fast": "快攻", "fast11": "早收", "hmq": "回馬槍", "rev": "純回馬",
             "orb": "開箱", "union": "多方聯軍", "night": "夜盤順勢"}
SRC_NAME = {"fast": "逐筆", "fast11": "逐筆", "hmq": "逐筆", "rev": "逐筆",
            "orb": "逐筆", "union": "逐筆", "night": "1 分 K"}

# ── 早盤快攻（fast／hmq／rev／fast11 共用的前半）
FAST_REF_MS = SL.ms(9, 0, 0)              # 09:00:00.000（含）以前最後一筆
FAST_PX_MS = SL.ms(9, 3, 30)              # 09:03:30.000（含）以前最後一筆
FAST_REF_AT, FAST_PX_AT = "09:00:00", "09:03:30"
FAST_FEE = 5.0

# ── 快攻 11:00 平：⛔ 跟 fast 完全一樣，**只有收盤平倉的時刻不同**。
#    11:00 比結算日的 13:30 還早 ⇒ 結算日不必另外處理（⛔ 不要在這裡抄一份 is_expiry）。
FAST11_CUT_MS = SL.ms(11, 0, 0)
FAST11_CUT_AT = "11:00:00"

# ── ORB 5 分＋箱子濾網（這條是新規則，不在 auto_fire 裡；常數全部集中在這裡，⛔ 不准散落在函式裡）
ORB_BOX_FROM_MS = SL.ms(9, 0, 0)          # 箱子＝09:00:00.000 ~ 09:05:00.000（兩端都含，跟快攻同一種「≤」口徑）
ORB_BOX_TO_MS = SL.ms(9, 5, 0)
ORB_BOX_AT = "09:00~09:05"
ORB_HIST_N = 20                           # 箱子寬度%的中位數看過去幾個交易日
ORB_FEE = 5.0                             # ⚠️ 跟快攻同一把尺（手續費 5 點）；卡頂已經寫「成本已扣」
# ⛔ **不設停利**：run_bracket 一定要收一個停利點數 ⇒ 給一個價格永遠碰不到的哨兵。
#    真的被碰到就是程式壞了 ⇒ orb_eval 會攔下來記成錯誤，⛔ 不准把 10 億點當成績寫進檔案。
ORB_NO_TP = 10 ** 9
# ⛔⛔ **窗口跨度**上限（日曆天；lab-qa 2026-09-16 退件 S1）：他的 tick_hist 中間有 16 個月的洞
#    （2025-06~2026-08 全空），所以「過去 20 天」可能有 12 天是一年多以前的。規則的語意是
#    「**跟最近的波動比**」，跨一年就不是那個意思了；而定論只 append 不重算，寫錯會永久留著。
#    ⇒ 跨度超過這個數就 **⛔ 不做定論、記成資料缺**，並在卡上寫明原因（CLAUDE.md「資料不完整時不可以猜」）。
ORB_SPAN_MAX_DAYS = 90

# ── 多方聯軍（union）：三個候選裡「做多而且觸發最早」的那一個
UNION_FROM = ("fast", "orb", "rev")       # 三個候選（顯示名字用 LANE_NAME）
# 觸發時刻**一樣**時的先後（⛔ 一定要有定序，不然同一份資料算兩次會給不同答案）
UNION_TIE = {"fast": 0, "orb": 1, "rev": 2}

# ── 夜盤順勢
NIGHT_FROM_MIN = 15 * 60 + 1              # 夜盤第一根的標籤 15:01
NIGHT_TO_MIN = 5 * 60 + 1440              # 最後一根標籤 05:00（隔天）；研究不收 05:01
NIGHT_EXIT_MIN = 4 * 60 + 58 + 1440       # 標籤 ≤ 04:58 平
NIGHT_TAIL_MIN = 4 * 60 + 58 + 1440       # 「到齊了」＝ 標籤 04:58~05:00 至少有一根
NIGHT_WAIT_MIN = 5                        # T → T+5
NIGHT_FIRST_MIN_BARS = 4                  # 那 5 分鐘至少幾根（研究 len(first) < 4 就跳過）
NIGHT_MIN_BARS = 200                      # 整晚少於這麼多根 ⇒ 資料有洞（研究同一條），⛔ 不算定論
NIGHT_TPSL_FRAC = 0.01
NIGHT_FEE, NIGHT_SPREAD = 5.0, 2.0
NIGHT_READY = dtime(5, 10)                # E+1 05:10 之後才算

# ── 背景
WINDOW_N = 10                             # 最近 10 個交易日／晚上
# ⛔ 這段不跟永豐抓任何東西：整個日盤（真單 09:03:30／09:15 進場、13:43:30 才平）＋ strategy_lab 13:50 開抓前。
#    2026-09-15 lab-qa 退件 R1：原本 09:35 就放行 ⇒ 09:35~13:45 真單部位可能還開著、面板重啟後記憶體又說沒部位。
QUIET_FROM, QUIET_TO = dtime(8, 30), dtime(13, 50)
RETRY_S = 600                             # 失敗隔 10 分鐘
LOOP_EVERY = 60.0
MONTHS_SHOWN = 6
RECENT_N = 15

_MONTH_RE = re.compile(r"^\d{4}-\d{2}\.jsonl$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DECISIONS = ("做多", "做空", "不做")
_EXITS = ("停利", "停損", "收盤")

# 注入的規則正本（live_panel.main → start_sim_lanes → configure）。沒接 ⇒ 逐筆那幾條只記「沒接上」，⛔ 不猜。
_CFG = {"verdict": None, "move_pct": None, "tpsl": None, "reversal": None, "hist_read": None,
        "pctl": None, "rule": None, "rev_sec": None}

STATE = {"errors": 0, "last_err": None, "last_err_at": None, "steps": 0, "last_step_at": None,
         "hist_bad": 0, "hist_dup": 0,
         "pending": {k: {} for k in LANES},
         "fetch": {"ticks": {"status": "idle", "msg": "", "at": None},
                   "kbars": {"status": "idle", "msg": "", "at": None}},
         "file": {"lines": 0, "ok": 0, "bad": 0, "dup": 0, "blank": 0}}
_FAIL_AT = {"ticks": None, "kbars": None}     # 上一次失敗的時刻（隔 RETRY_S 才再試）
_TRIED = set()                                # (kind, 日期, 今天) ⇒ 今天問過而且對方說沒有 ⇒ ⛔ 今天不重抓
_NIGHT_API = {}                               # E ⇒ 從永豐補齊的那一晚（只放到齊的）
_BOX = {}                                     # 日期 ⇒ 那天的箱子寬度%（開箱的中位數濾網用；重算很貴，只放記憶體）
# ⛔ 掃箱子歷史時畫面要看得出「**還在算**」而不是「沒有資料」（PM 2026-09-16 裁示）：
#    面板剛啟動的第一輪要重掃過去 20 天的逐筆，全程在背景執行緒、⛔ 不擋面板啟動。
_BOX_ST = {"busy": False, "day": None, "done": 0, "need": ORB_HIST_N}
_FILE_LOCK = threading.Lock()                 # ⛔ 只保護 sim_lanes/ 的讀寫，跟面板任何鎖無關
_CSV = {"key": None, "df": None}
_LOGGED = {"bad": None}


def log(msg):
    try:
        print("[模擬] " + str(msg), flush=True)
    except Exception:
        pass


def _note_err(where, e):
    """⛔ 例外吞掉，但不可以安靜地少：計數 ＋ 主控台（前 5 次、之後每 50 次）＋ 畫面（state 的 errors／last_err）"""
    try:
        STATE["errors"] += 1
        STATE["last_err"] = "%s：%s" % (where, str(e)[:160])
        STATE["last_err_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if STATE["errors"] <= 5 or STATE["errors"] % 50 == 0:
            log("背景例外（已吞掉，第 %d 次）%s" % (STATE["errors"], STATE["last_err"]))
    except Exception:
        pass


def configure(verdict_fn, move_fn, tpsl_fn, hist_read_fn, pctl, rule, reversal_fn=None, rev_sec=None):
    """
    live_panel.main() 接上規則正本（auto_fire 的那幾支＋live_panel.FAST_PCTL／REV_SEC＋auto_fire.FAST_RULE）。
    ⛔ 這個模組自己不 import 它們（見檔頭）。任何一樣看不懂 ⇒ 不接（wired False），逐筆那幾條記「沒接上」。
    ⚠️ `reversal_fn`／`rev_sec` 是「09:15 回馬槍」那半（hmq／rev）用的；⛔ 一樣是注入，不在這裡另寫一份。
    """
    ok = (callable(verdict_fn) and callable(move_fn) and callable(tpsl_fn) and callable(hist_read_fn)
          and callable(reversal_fn)
          and not isinstance(pctl, bool) and isinstance(pctl, (int, float)) and 0 < pctl <= 100
          and not isinstance(rev_sec, bool) and isinstance(rev_sec, (int, float)) and 0 < rev_sec < 86400
          and isinstance(rule, dict) and isinstance(rule.get("window"), int) and isinstance(rule.get("min_n"), int))
    if not ok:
        _CFG.update(verdict=None, move_pct=None, tpsl=None, reversal=None, hist_read=None,
                    pctl=None, rule=None, rev_sec=None)
        return False
    _CFG.update(verdict=verdict_fn, move_pct=move_fn, tpsl=tpsl_fn, reversal=reversal_fn,
                hist_read=hist_read_fn, pctl=pctl, rule=rule, rev_sec=rev_sec)
    return True


def wired():
    return _CFG["verdict"] is not None


# ══ 日曆 ══════════════════════════════════════════════════════════════

def us_dst(d):
    """美國夏令：3 月第二個週日（含）起到 11 月第一個週日（不含）前（＝研究 night_fast_ticks.us_open_ms）"""
    mar = date(d.year, 3, 8) + timedelta(days=(6 - date(d.year, 3, 8).weekday()) % 7)
    nov = date(d.year, 11, 1) + timedelta(days=(6 - date(d.year, 11, 1).weekday()) % 7)
    return mar <= d < nov


def us_open_min(d):
    """E 那晚美股開盤的「分鐘數」（台北時間）：夏令 21:30、冬令 22:30"""
    return (21 * 60 + 30) if us_dst(d) else (22 * 60 + 30)


def _hm(m):
    m %= 1440
    return "%02d:%02d" % (m // 60, m % 60)


def weekdays_back(end, n):
    """end（含）往回 n 個平日，新到舊。⚠️ 沒有國定假日表：假日會在「資料缺」裡出現，⛔ 不會被當成定論。"""
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out


def fast_days(now):
    """快攻要看的日子：最近 10 個平日（含今天；今天要等 13:50 之後才可能有逐筆）"""
    return weekdays_back(now.date(), WINDOW_N)


def night_evenings(now):
    """夜盤要看的晚上 E：E+1 05:10 已經過了的最近 10 個平日晚上"""
    end = now.date() - timedelta(days=1)
    if now.time() < NIGHT_READY:
        end -= timedelta(days=1)
    return weekdays_back(end, WINDOW_N)


# ══ 純計算 ════════════════════════════════════════════════════════════

def _pending(why, msg):
    return {"pending": True, "why": why, "msg": msg}


def _none_row(lane, day, why, reason, extra=None):
    row = {"lane": lane, "date": str(day), "decision": "不做", "why": why, "reason": reason,
           "entry": None, "exit": None, "exit_reason": None, "points": None, "src": SRC_NAME[lane]}
    row.update(extra or {})
    return row


def _px(v):
    """價格的字面（給 reason 看）。⚠️ 只是顯示，判斷一律用數字。"""
    return "—" if v is None else ("%g" % float(v))


def _hms_sec(sec):
    """秒數 ⇒ HH:MM:SS（09:15 那個時刻是**注入**的 rev_sec，⛔ 不在這裡寫死）"""
    sec = int(sec)
    return "%02d:%02d:%02d" % (sec // 3600, sec % 3600 // 60, sec % 60)


def _hms_ms(msec):
    """當日毫秒數 ⇒ HH:MM:SS（開箱的突破時刻是**那一筆成交的時間**，不是固定時刻）"""
    return _hms_sec(int(msec) // 1000)


def _day_cutoff(day):
    """日盤收盤平倉的時刻：結算日 13:30，其他 13:43:30（⛔ 用 strategy_lab 的正本，不另寫 is_expiry）"""
    return SL.T1330 if SL.is_expiry(date.fromisoformat(str(day))) else SL.T1343_30


def _cut_at(cutoff):
    """收盤平倉時刻的字面（寫進落地那一列，畫面直接顯示）"""
    return {SL.T1330: "13:30:00", SL.T1343_30: "13:43:30", FAST11_CUT_MS: FAST11_CUT_AT}.get(cutoff, "")


def _fast_ctx(day, D, hist_rows, c):
    """
    fast／fast11／hmq／rev／union **共用的前半**：09:00 的 ref、09:03:30 的 px、開盤走幅、快不快。
    回 (ctx, stop)。⚠️ stop 是**中性的**（沒有 lane）—— 由 `_stop_row(lane, ...)` 具體化成那一條的列，
    這樣同一天只算一次、六條共用（⛔ 不准每條各算一次）。
    ⛔ 走幅與快不快一律呼叫注入的 `move_pct`／`fast_verdict`（跟真單同一份正本）。
    """
    if c.get("verdict") is None:
        return None, {"pending": True, "why": "not_wired", "msg": "規則函式沒有接上（面板 main() 沒有呼叫 configure）"}
    if D is None:
        return None, {"pending": True, "why": "no_ticks", "msg": "沒有當天逐筆"}
    if hist_rows is None:
        return None, {"pending": True, "why": "no_hist_file", "msg": "讀不到開盤走幅歷史（fast_hist.jsonl）"}
    t = D["t"]
    i_ref = int(np.searchsorted(t, FAST_REF_MS, side="right")) - 1
    i_px = int(np.searchsorted(t, FAST_PX_MS, side="right")) - 1
    if i_ref < 0:
        return None, {"why": "no_ref", "reason": "09:00 以前沒有成交"}
    if i_px <= i_ref:
        return None, {"why": "no_px", "reason": "09:00~09:03:30 沒有成交"}
    ref, px = float(D["p"][i_ref]), float(D["p"][i_px])
    mv = c["move_pct"](px, ref)
    v = c["verdict"](day, mv, hist_rows, pctl=c["pctl"])       # ⛔ 傳的是「這一天」：門檻只准用這天以前的列
    base = {"ref": ref, "px": px, "move_pct": None if mv is None else round(mv, 4),
            "thr_pct": None if v.get("thr_pct") is None else round(v["thr_pct"], 4), "n_hist": v.get("n")}
    if v.get("verdict") == "no_hist":
        return None, {"why": "no_hist", "base": base,
                      "reason": "歷史不夠（這天以前只有 %s 天，要 %s 天）"
                                % (v.get("n"), (c.get("rule") or {}).get("min_n", "?"))}
    if v.get("verdict") is None:
        return None, {"why": "no_move", "reason": "算不出開盤走幅", "base": base}
    return {"i_px": i_px, "ref": ref, "px": px, "mv": mv, "v": v, "base": base,
            "d": (px > ref) - (px < ref), "fast": v["verdict"] == "fast"}, None


def _stop_row(lane, day, stop):
    """把 `_fast_ctx` 的中性 stop 變成**那一條**的列（資料缺 ⇒ pending、⛔ 不寫檔；其餘是定論「不做」）"""
    if stop.get("pending"):
        return _pending(stop["why"], stop["msg"])
    return _none_row(lane, day, stop["why"], stop["reason"], stop.get("base"))


def day_pack(day, D, hist_rows, box_vals=None, cfg=None):
    """
    ⛔⛔ **一天只算一次**的共用結果：快攻那半的 ctx ＋ 開箱的箱子＋箱子寬度歷史。
    逐筆那六條全部吃這一份（`_step_ticks` 每天建一次）——
    ⛔ 不准每條各算一次：`load_day`、`_fast_ctx`、`orb_calc`、`box_hist` 每一支都很貴，
    而且各算一次還會讓六條有機會算出**不一致**的候選（多方聯軍就是靠這份一致性）。
    """
    c = cfg or _CFG
    ctx, stop = _fast_ctx(day, D, hist_rows, c)
    return {"day": str(day), "cfg": c, "ctx": ctx, "stop": stop,
            "orb": None if D is None else orb_calc(day, D), "box": box_vals}


def _fast_setup(lane, day, D, hist_rows, c, pack=None):
    """`_fast_ctx` ＋ 具體化成那一條的列（單獨呼叫某一條時用；`_step_ticks` 一律走 pack）"""
    p = pack if pack is not None else day_pack(day, D, hist_rows, None, c)
    ctx, stop = p["ctx"], p["stop"]
    return ctx, (None if stop is None else _stop_row(lane, day, stop))


def _enter(lane, day, D, i, d, cutoff, why, reason, base, c, extra=None):
    """
    順 d 在第 i 筆進場（多用賣價／空用買價，0 就用成交價），走完 ±`tpsl_points(進場價)` 的括號 ⇒ 定論那一列。
    ⛔ 停利停損點數一律呼叫注入的 `tpsl_points`（±0.5%），⛔ 不在這裡乘 0.005。
    """
    fill = float(D["ask"][i] if d > 0 else D["bid"][i])
    fill = fill if fill else float(D["p"][i])
    pts_tpsl = c["tpsl"](fill)
    if not pts_tpsl:
        return _pending("no_tpsl", "停利停損點數算不出來")
    raw, w = SL.run_bracket(D, i, fill, d, pts_tpsl, pts_tpsl, cutoff, cost=True)
    row = {"lane": lane, "date": str(day), "decision": "做多" if d > 0 else "做空", "why": why,
           "reason": reason, "entry": fill, "exit": round(fill + d * raw, 1),
           "exit_reason": {"tp": "停利", "sl": "停損", "eod": "收盤"}[w],
           "points": round(raw - FAST_FEE, 1), "cost": FAST_FEE, "tpsl_points": pts_tpsl,
           "cutoff": _cut_at(cutoff), "src": SRC_NAME[lane]}
    row.update(base or {})
    row.update(extra or {})
    return row


def _fast_like(lane, day, D, hist_rows, c, cutoff, pack=None):
    """fast 與 fast11：⛔ 兩條**只差收盤平倉的時刻**，所以共用這一支（避免兩份會分岔的規則）。"""
    ctx, stop = _fast_setup(lane, day, D, hist_rows, c, pack)
    if stop is not None:
        return stop
    v, base = ctx["v"], ctx["base"]
    if not ctx["fast"]:
        return _none_row(lane, day, "not_fast", "不快，不做（走 %.3f%%，門檻 %.3f%%）"
                         % (ctx["mv"], v["thr_pct"]), base)
    if ctx["d"] == 0:
        return _none_row(lane, day, "flat", "09:03:30 跟 09:00 一樣價，沒有方向", base)
    return _enter(lane, day, D, ctx["i_px"], ctx["d"], cutoff, "fast",
                  "快（走 %.3f%%，門檻 %.3f%%）" % (ctx["mv"], v["thr_pct"]), base, c, {"at": FAST_PX_AT})


def fast_eval(day, D, hist_rows, cfg=None, pack=None):
    """
    一天的「快攻」。D＝strategy_lab.load_day 的逐筆（t 毫秒、p、bid、ask，已排序）或 None；
    hist_rows＝hist_read 的 rows（舊到新）或 None（讀不到歷史檔）。
    回 **定論那一列**（dict，lane/date/decision…）或 `_pending(...)`（資料缺，⛔ 不寫檔）。
    """
    day = str(day)
    return _fast_like("fast", day, D, hist_rows, cfg or _CFG, _day_cutoff(day), pack)


def fast11_eval(day, D, hist_rows, cfg=None, pack=None):
    """「早收」：⛔ 跟快攻完全一樣，只把收盤平倉時刻換成 11:00:00（先碰到停利停損一樣先出）。"""
    return _fast_like("fast11", str(day), D, hist_rows, cfg or _CFG, FAST11_CUT_MS, pack)


def _rev_at(D, ctx, c):
    """不快那半的「09:15（注入的 rev_sec）以前最後一筆」⇒ (索引, 方向 d2)；沒成交／沒反轉 ⇒ 索引或 d2 是 None。
    ⛔ 「有沒有反轉」一律呼叫注入的 `reversal_dir`（跟真單同一支），⛔ 不在這裡自己比方向。"""
    i15 = int(np.searchsorted(D["t"], int(c["rev_sec"]) * 1000, side="right")) - 1
    if i15 <= ctx["i_px"]:
        return None, None
    return i15, c["reversal"](ctx["px"], ctx["d"], float(D["p"][i15]))


def _rev_leg(lane, day, D, ctx, c, cutoff):
    """
    不快的日子那一半：等 09:15（注入的 rev_sec）以前最後一筆，方向跟 09:03:30 **相反**才順新方向做 1 口。
    """
    at = _hms_sec(c["rev_sec"])
    i15 = int(np.searchsorted(D["t"], int(c["rev_sec"]) * 1000, side="right")) - 1
    if i15 <= ctx["i_px"]:
        return _none_row(lane, day, "no_p15", "%s~%s 沒有成交" % (FAST_PX_AT, at), ctx["base"])
    p15 = float(D["p"][i15])
    ex = {"p15": p15, "rev_at": at, "at": at}
    d2 = _rev_at(D, ctx, c)[1]
    tail = "（%s %s → %s %s）" % (FAST_PX_AT, _px(ctx["px"]), at, _px(p15))
    if d2 is None:
        return _none_row(lane, day, "no_rev", "不快；%s 沒有反轉，不做%s" % (at, tail), dict(ctx["base"], **ex))
    return _enter(lane, day, D, i15, d2, cutoff, "rev",
                  "不快；%s 反轉，順新方向%s" % (at, tail), ctx["base"], c, ex)


def hmq_eval(day, D, hist_rows, cfg=None, pack=None):
    """
    「回馬槍」（＝真單現在跑的那一套）：快 ⇒ 09:03:30 順勢；不快 ⇒ 等 09:15 反轉才做。
    ⛔ 一天最多一口：快的日子**不會**再看 09:15。
    """
    c = cfg or _CFG
    day = str(day)
    ctx, stop = _fast_setup("hmq", day, D, hist_rows, c, pack)
    if stop is not None:
        return stop
    cutoff = _day_cutoff(day)
    if ctx["fast"]:
        if ctx["d"] == 0:
            return _none_row("hmq", day, "flat", "09:03:30 跟 09:00 一樣價，沒有方向", ctx["base"])
        return _enter("hmq", day, D, ctx["i_px"], ctx["d"], cutoff, "fast",
                      "快（走 %.3f%%，門檻 %.3f%%）" % (ctx["mv"], ctx["v"]["thr_pct"]), ctx["base"], c,
                      {"at": FAST_PX_AT})
    return _rev_leg("hmq", day, D, ctx, c, cutoff)


def rev_eval(day, D, hist_rows, cfg=None, pack=None):
    """「純回馬」：⛔ **只做**「慢且 09:15 反轉」那一半，快的日子不做（回馬槍減掉快攻）。"""
    c = cfg or _CFG
    day = str(day)
    ctx, stop = _fast_setup("rev", day, D, hist_rows, c, pack)
    if stop is not None:
        return stop
    if ctx["fast"]:
        return _none_row("rev", day, "fast_skip", "快，這條不做（只做慢且反轉那一半；走 %.3f%%，門檻 %.3f%%）"
                         % (ctx["mv"], ctx["v"]["thr_pct"]), ctx["base"])
    return _rev_leg("rev", day, D, ctx, c, _day_cutoff(day))


# ── ORB 5 分＋箱子濾網 ─────────────────────────────────────────────────

def orb_box(D):
    """箱子＝09:00:00.000 ~ 09:05:00.000（兩端都含）的最高／最低 ⇒ (hi, lo, i1)；那段沒有成交 ⇒ None。
    i1＝箱子之後第一筆的索引（突破從這裡開始找）。"""
    t = D["t"]
    i0 = int(np.searchsorted(t, ORB_BOX_FROM_MS, side="left"))
    i1 = int(np.searchsorted(t, ORB_BOX_TO_MS, side="right"))
    if i1 <= i0:
        return None
    seg = D["p"][i0:i1]
    return float(seg.max()), float(seg.min()), i1


def orb_break(D, i1, hi, lo, cutoff):
    """箱子之後 **第一次** 穿出箱子的那一筆 ⇒ (索引, 方向)。收盤平倉之前都沒有 ⇒ None。
    ⛔ 上緣用 `>`、下緣用 `<`（碰到邊不算突破）；⛔ 一天最多 1 次（只認第一筆）。"""
    j1 = int(np.searchsorted(D["t"], cutoff, side="right"))
    if j1 <= i1:
        return None
    p = D["p"][i1:j1]
    up, dn = p > hi, p < lo
    iu = int(np.argmax(up)) if up.any() else None
    idn = int(np.argmax(dn)) if dn.any() else None
    if iu is None and idn is None:
        return None
    if idn is None or (iu is not None and iu <= idn):
        return i1 + iu, 1
    return i1 + idn, -1


def _orb_fill(D, i, d):
    """進場價：多用賣價、空用買價，0 就用成交價（跟快攻同一種口徑）"""
    f = float(D["ask"][i] if d > 0 else D["bid"][i])
    return f if f else float(D["p"][i])


def orb_calc(day, D):
    """
    一天的箱子＋突破 ⇒ dict（hi／lo／w／box_pct／i／d／fill）或 None（09:00~09:05 沒有成交）。
    **箱子寬度%＝箱寬 ÷ 進場價 × 100**（濾網比的就是這個值）。
    ⛔⛔ **規則原文沒有涵蓋「沒有突破的日子」**（那種日子根本沒有進場價）——
       這裡的處理是**實作決定**（PM 2026-09-16 裁示照做，並要求寫在這裡）：
       分母改用箱子最後一筆成交價。兩者差 < 1%，對中位數濾網沒有影響；
       而**每個交易日都要有值**才算得出中位數 —— 只收有突破的日子，那把尺就被挑過了。
    """
    b = orb_box(D)
    if b is None:
        return None
    hi, lo, i1 = b
    cutoff = _day_cutoff(day)
    br = orb_break(D, i1, hi, lo, cutoff)
    i, d, fill = (None, 0, float(D["p"][i1 - 1]))
    if br is not None:
        i, d = br
        fill = _orb_fill(D, i, d)
    w = hi - lo
    return {"hi": hi, "lo": lo, "w": w, "i": i, "d": d, "fill": fill,
            "box_pct": (w / fill * 100.0) if fill else 0.0, "cutoff": cutoff}


def orb_box_pct(day, D):
    """那一天的箱子寬度%（給中位數濾網當歷史用）；那天 09:00~09:05 沒有成交 ⇒ None。"""
    o = orb_calc(day, D)
    return None if o is None else o["box_pct"]


def orb_med(box_vals):
    """箱子寬度%的中位數（過去 ORB_HIST_N 個交易日）。⚠️ 天數不夠 ⇒ None（呼叫端當「資料缺」）。"""
    v = _box_win(box_vals)["vals"]
    if v is None or len(v) < ORB_HIST_N:
        return None
    return float(np.median(np.asarray(v, dtype=float)[-ORB_HIST_N:]))


def orb_span_bad(box_vals):
    """窗口跨度太寬 ⇒ 回那句原因（⛔ 呼叫端要當成**資料缺**，不可以做定論）；沒問題 ⇒ None。"""
    w = _box_win(box_vals)
    if w.get("span") is None or w["span"] <= ORB_SPAN_MAX_DAYS:
        return None
    return ("箱子寬度歷史跨了 %d 天（%s~%s），超過 %d 天 ⇒ 不做定論"
            "（資料有洞，這 %d 天不是「最近的波動」）"
            % (w["span"], w["d0"], w["d1"], ORB_SPAN_MAX_DAYS, len(w.get("vals") or [])))


def _orb_enter(lane, day, D, o, cutoff, why, reason, base, extra=None):
    """
    突破箱子之後的那一口：**停損＝箱子另一端**、⛔ **不設停利**、cutoff 前沒碰到就收盤平。
    ⛔ 「開箱」與「多方聯軍」共用這一支（兩份會分岔的出場規則是找死）。
    """
    d, fill = o["d"], o["fill"]
    sl = round(fill - o["lo"], 1) if d > 0 else round(o["hi"] - fill, 1)
    if sl <= 0:
        return _pending("bad_sl", "箱子另一端算不出停損")
    raw, w = SL.run_bracket(D, o["i"], fill, d, ORB_NO_TP, sl, cutoff, cost=True)
    if w == "tp":
        # ⛔ 不設停利：哨兵被碰到 ＝ 程式壞了。往外丟給 step() 吞（計數＋畫面），
        #    ⛔ 絕對不可以把 10 億點當成一天的成績寫進檔案。
        raise RuntimeError("開箱不設停利，卻走到停利（哨兵 %s 被碰到）" % ORB_NO_TP)
    row = {"lane": lane, "date": str(day), "decision": "做多" if d > 0 else "做空", "why": why,
           "reason": reason, "entry": fill, "exit": round(fill + d * raw, 1),
           "exit_reason": {"sl": "停損", "eod": "收盤"}[w],
           "points": round(raw - ORB_FEE, 1), "cost": ORB_FEE, "sl_points": sl,
           "cutoff": _cut_at(cutoff), "src": SRC_NAME[lane]}
    row.update(base or {})
    row.update(extra or {})
    return row


def orb_eval(day, D, box_hist, cfg=None, pack=None):
    """
    一天的「開箱」。box_hist＝**這一天以前**最近 ORB_HIST_N 個交易日的箱子寬度%（list）或 None（算不出來）。
    ⛔⛔ **這裡不要用 pack 裡的 ctx**：`_fast_ctx` 回 stop 的日子（09:00 前沒成交／走幅歷史不夠…）
       `pack["ctx"]` 會是 None，而開箱這條規則跟快攻那半完全無關，⛔ 不可以被它連坐。
       這支只准用 `pack["orb"]` 與 `pack["box"]`。
    ⛔ 箱子太窄（< 中位數）是**定論**「不做」；歷史不夠是**資料缺**（⛔ 不寫檔 —— 逐筆之後可能補得回來）。
    """
    day = str(day)
    if D is None:
        return _pending("no_ticks", "沒有當天逐筆")
    bh = pack["box"] if pack is not None else box_hist
    if _box_win(bh)["vals"] is None:
        return _pending("no_box_hist", "算不出過去的箱子寬度（沒有歷史逐筆）")
    o = pack["orb"] if pack is not None else orb_calc(day, D)
    if o is None:
        return _none_row("orb", day, "no_box", "%s 沒有成交，畫不出箱子" % ORB_BOX_AT)
    base = {"box_hi": o["hi"], "box_lo": o["lo"], "box_w": round(o["w"], 1),
            "box_pct": round(o["box_pct"], 4)}
    _w = _box_win(bh)
    med = orb_med(bh)
    if med is None:
        return _pending("few_box_hist", "箱子寬度歷史不夠（這天以前只有 %d 天，要 %d 天）"
                        % (len(_w["vals"]), ORB_HIST_N))
    bad = orb_span_bad(bh)                        # ⛔ 跨度太寬 ⇒ 資料缺（⛔ 不寫檔）
    if bad:
        return _pending("box_span", bad)
    base.update({"box_med_pct": round(med, 4), "box_win": box_win_txt(_w), "box_span": _w["span"]})
    if o["box_pct"] < med:
        return _none_row("orb", day, "narrow_box", "箱子太窄，不做（%.3f%%，%s中位數 %.3f%%）"
                         % (o["box_pct"], box_win_txt(_w), med), base)
    if o["i"] is None:
        return _none_row("orb", day, "no_break", "整天沒有突破箱子（%s ~ %s）" % (_px(o["lo"]), _px(o["hi"])), base)
    at = _hms_ms(int(D["t"][o["i"]]))
    reason = ("%s箱子（%s ~ %s，寬 %.3f%% ≥ %s中位數 %.3f%%），停損＝箱子另一端 %s"
              % ("突破" if o["d"] > 0 else "跌破", _px(o["lo"]), _px(o["hi"]),
                 o["box_pct"], box_win_txt(_w), med, _px(o["lo"] if o["d"] > 0 else o["hi"])))
    return _orb_enter("orb", day, D, o, _day_cutoff(day), "break", reason, base, {"at": at})


# ══ 多方聯軍（union）══════════════════════════════════════════════════

def union_cands(day, D, p):
    """
    當天把**三個候選**收齊（快攻／開箱／回馬槍），每個都有「觸發時刻」與「方向」。
    ⛔ 一律用同一份 `day_pack`（跟那三條看到的是同一份答案），⛔ 不重算逐筆。
      ・快攻　　09:03:30，走幅 ≥ 過去 40 天第 80 百分位才算觸發
      ・開箱　　箱子寬度% ≥ 過去 20 天中位數才算數；**第一次**突破那一刻觸發
      ・回馬槍　只有「09:03:30 判定不快」的日子才有；09:15:00 價與 09:03:30 價**方向相反**才算觸發

    ⛔⛔ **每個候選各自判斷可不可用**（PM 2026-09-16 裁示，口徑跟回測一致）：
       ・走幅歷史不夠（`no_hist`）／09:00 前沒成交（`no_ref`）… ⇒ 那天**沒有快攻與回馬槍這兩個候選**
       ・箱子寬度歷史不夠（< 20 天）⇒ 那天**沒有開箱這個候選**
       ⛔ 都**不是**「整條沒資料」—— 只要當天還有至少一個做多候選，多方聯軍照做。
       （第一版寫成「收不齊就整條資料缺」是錯的：面板剛開始跑、箱子歷史還在掃的那幾十天，
         union 會整條空白，跟回測對不起來。）

    ⛔ 只有這三種算**整條資料缺**（`_fast_ctx` 回的 pending）：沒接上規則、沒有當天逐筆、
       讀不到 `fast_hist.jsonl`。前兩個是「整天沒東西可算」，第三個是**檔案不見**——
       CLAUDE.md 明令不可以把「檔案不見」記成定論（部署前沒放種子會把那幾天永久寫錯）。

    回 (候選 list（照觸發時刻排好，早的在前）, 不可用的原因 list, 整條 stop or None)。
    """
    c = p["cfg"]
    ctx, stop = p["ctx"], p["stop"]
    if stop is not None and stop.get("pending"):
        return None, None, stop                 # ⛔ 只有「資料缺」那三種才整條停
    out, miss = [], []
    if ctx is None:                             # 定論級的算不出來（no_ref／no_px／no_hist／no_move）
        # ⛔ 名字一律用候選自己的正式名字：快攻／**純回馬**（「回馬槍」是 hmq 那一條，不是候選）
        miss.append("%s／%s：%s" % (LANE_NAME["fast"], LANE_NAME["rev"], stop.get("reason") or ""))
    else:
        if ctx["fast"] and ctx["d"] != 0:
            out.append({"kind": "fast", "dir": ctx["d"], "at_ms": FAST_PX_MS, "at": FAST_PX_AT, "i": ctx["i_px"]})
        if not ctx["fast"]:                     # ⛔ 回馬槍只有「不快」的日子才有這個候選
            i15, d2 = _rev_at(D, ctx, c)
            if i15 is not None and d2 is not None:
                out.append({"kind": "rev", "dir": d2, "at_ms": int(c["rev_sec"]) * 1000,
                            "at": _hms_sec(c["rev_sec"]), "i": i15})
    bh, o = p["box"], p["orb"]
    med, _span_bad = orb_med(bh), orb_span_bad(bh)
    if med is None:                             # ⛔ 只是少一個候選，不是整條沒資料
        miss.append("%s：箱子寬度歷史不夠（這天以前只有 %d 天，要 %d 天）"
                    % (LANE_NAME["orb"], len(_box_win(bh)["vals"] or []), ORB_HIST_N))
    elif _span_bad:                             # ⛔ 跨度太寬 ⇒ 同樣只是「少一個候選」
        miss.append("%s：%s" % (LANE_NAME["orb"], _span_bad))
    elif o is not None and o["i"] is not None and o["box_pct"] >= med:
        out.append({"kind": "orb", "dir": o["d"], "at_ms": int(D["t"][o["i"]]),
                    "at": _hms_ms(int(D["t"][o["i"]])), "i": o["i"]})
    out.sort(key=lambda x: (x["at_ms"], UNION_TIE[x["kind"]]))
    return out, miss, None


def _cand_txt(x):
    return "%s %s %s" % (LANE_NAME[x["kind"]], x["at"], "多" if x["dir"] > 0 else "空")


def _miss_txt(miss):
    """把「今天少了哪些候選」接成一句（⛔ 一定要寫進 reason —— 少一個候選會改變結果）"""
    return ("；不可用：" + "、".join(miss)) if miss else ""


def union_eval(day, D, hist_rows, box_vals=None, cfg=None, pack=None):
    """
    「多方聯軍」：候選裡**只取方向為做多**的（⛔ 說做空的略過，但當天要**繼續看下一個**，
    不是收工），在剩下的做多候選裡取**觸發時刻最早**的那一個，照它自己的進出場規則做，
    ⛔ **一天最多一口**。

    當天沒有任何做多候選 ⇒ 不做，而且 ⛔ **要分得出是哪一種**（將來看紀錄時意義完全不同）：
      ・`no_long`　有候選、但都說做空
      ・`no_cand`　一個候選都沒有（有的不可用、有的沒觸發，reason 寫清楚）
    """
    day = str(day)
    c = cfg or _CFG
    p = pack if pack is not None else day_pack(day, D, hist_rows, box_vals, c)
    cands, miss, stop = union_cands(day, D, p)
    if stop is not None:
        return _stop_row("union", day, stop)
    base = {"cands": [_cand_txt(x) for x in cands], "miss": list(miss)}
    longs = [x for x in cands if x["dir"] > 0]     # ⛔ 用濾的（不是「碰到做空就 break」）
    if cands and not longs:
        return _none_row("union", day, "no_long",
                         "候選都不是做多（%s），不做%s"
                         % ("、".join(_cand_txt(x) for x in cands), _miss_txt(miss)), base)
    if not cands:
        return _none_row("union", day, "no_cand",
                         ("今天沒有可用的候選，不做%s" % _miss_txt(miss)) if miss
                         else "三個候選都可用，但一個都沒觸發，不做", base)
    pick = longs[0]                               # cands 已經照 (觸發時刻, 定序) 排過 ⇒ 這就是最早的做多候選
    reason = ("照「%s」做多（%s 觸發，最早）；候選：%s%s"
              % (LANE_NAME[pick["kind"]], pick["at"],
                 "、".join(_cand_txt(x) for x in cands), _miss_txt(miss)))
    ex = {"pick": pick["kind"], "pick_name": LANE_NAME[pick["kind"]], "at": pick["at"]}
    cutoff = _day_cutoff(day)
    if pick["kind"] == "orb":
        return _orb_enter("union", day, D, p["orb"], cutoff, "union", reason, base, ex)
    return _enter("union", day, D, pick["i"], pick["dir"], cutoff, "union", reason, base, c, ex)


# ⛔ 逐筆那六條的統一入口：`_step_ticks` 一天建一次 `day_pack` 再餵給每一條（⛔ 不准各算各的）
TICK_EVAL = {
    "fast":   lambda day, D, hist, bh, pack=None: fast_eval(day, D, hist, pack=pack),
    "fast11": lambda day, D, hist, bh, pack=None: fast11_eval(day, D, hist, pack=pack),
    "hmq":    lambda day, D, hist, bh, pack=None: hmq_eval(day, D, hist, pack=pack),
    "rev":    lambda day, D, hist, bh, pack=None: rev_eval(day, D, hist, pack=pack),
    "orb":    lambda day, D, hist, bh, pack=None: orb_eval(day, D, bh, pack=pack),
    "union":  lambda day, D, hist, bh, pack=None: union_eval(day, D, hist, bh, pack=pack),
}


def night_frame(bars, E):
    """原始 1 分 K（ts＝結束時間標籤）⇒ E 那一晚的 (mm 分鐘數陣列, High, Low, Close)，舊到新。
    mm：E 的 15:01 起算，隔天的時間加 1440。"""
    if bars is None or len(bars) == 0:
        return np.array([], dtype=np.int64), np.array([]), np.array([]), np.array([])
    ts = pd.to_datetime(bars["ts"])
    m = (ts.dt.hour * 60 + ts.dt.minute).to_numpy(np.int64)
    dd = ts.dt.date.to_numpy()
    E1 = E + timedelta(days=1)
    eve = (dd == E) & (m >= NIGHT_FROM_MIN)
    mor = (dd == E1) & (m <= NIGHT_TO_MIN - 1440)
    keep = eve | mor
    mm = np.where(mor, m + 1440, m)[keep]
    o = np.argsort(mm, kind="stable")
    H = bars["High"].to_numpy(float)[keep][o]
    L = bars["Low"].to_numpy(float)[keep][o]
    C = bars["Close"].to_numpy(float)[keep][o]
    mm = mm[o]
    # 同一個標籤出現兩次（本機＋永豐合併）⇒ 只留第一根
    if len(mm):
        u = np.concatenate([[True], mm[1:] != mm[:-1]])
        mm, H, L, C = mm[u], H[u], L[u], C[u]
    return mm, H, L, C


def night_complete(mm):
    return bool(len(mm)) and bool(np.any(mm >= NIGHT_TAIL_MIN))


def night_eval(E, bars):
    """
    一晚的美股開盤順勢。bars：原始 1 分 K DataFrame（ts／High／Low／Close，ts＝結束時間標籤）。
    回定論那一列或 `_pending(...)`。⛔ 還沒到齊（缺 04:58 之後）一律是 pending，不是「不做」。
    """
    E = E if isinstance(E, date) else date.fromisoformat(str(E))
    mm, H, L, C = night_frame(bars, E)
    if not len(mm):
        return _pending("no_bars", "沒有這一晚的 1 分 K")
    if not night_complete(mm):
        return _pending("incomplete", "1 分 K 還沒到齊（最後一根 %s，要到 04:58）" % _hm(int(mm[-1])))
    if len(mm) < NIGHT_MIN_BARS:
        return _pending("few_bars", "這一晚只有 %d 根 1 分 K（少於 %d，資料有洞）" % (len(mm), NIGHT_MIN_BARS))
    T = us_open_min(E)
    base = {"us_open": _hm(T), "us_dst": us_dst(E)}
    prev = np.nonzero(mm <= T)[0]
    first = np.nonzero((mm > T) & (mm <= T + NIGHT_WAIT_MIN))[0]
    if not len(prev):
        return _none_row("night", E, "no_ref", "美股開盤前沒有 K 棒", base)
    if len(first) < NIGHT_FIRST_MIN_BARS:
        return _none_row("night", E, "few_first", "開盤那 5 分鐘只有 %d 根 K 棒" % len(first), base)
    ref, c = float(C[prev[-1]]), float(C[first[-1]])
    base.update({"ref": ref, "c": c, "ref_label": _hm(int(mm[prev[-1]])), "c_label": _hm(int(mm[first[-1]]))})
    d = (c > ref) - (c < ref)
    if d == 0:
        return _none_row("night", E, "flat", "開盤 5 分鐘收平（%s 跟 %s 一樣價）" % (base["c_label"], base["ref_label"]), base)
    after = np.nonzero((mm > T + NIGHT_WAIT_MIN) & (mm <= NIGHT_EXIT_MIN))[0]
    if not len(after):
        return _pending("no_after", "進場之後沒有 K 棒")
    h, l, cl = H[after], L[after], C[after]
    tp = sl = c * NIGHT_TPSL_FRAC
    tph = (h >= c + tp) if d > 0 else (l <= c - tp)
    slh = (l <= c - sl) if d > 0 else (h >= c + sl)
    it = int(np.argmax(tph)) if tph.any() else None
    isl = int(np.argmax(slh)) if slh.any() else None
    if it is None and isl is None:
        ex, why, k = float(cl[-1]), "收盤", after[-1]
    elif isl is not None and (it is None or isl <= it):      # ⛔ 同一根兩邊都碰 ⇒ 停損（保守）
        ex, why, k = c - d * sl, "停損", after[isl]
    else:
        ex, why, k = c + d * tp, "停利", after[it]
    cost = NIGHT_FEE + NIGHT_SPREAD
    row = {"lane": "night", "date": str(E), "decision": "做多" if d > 0 else "做空", "why": "trend",
           "reason": "%s 比 %s %s" % (base["c_label"], base["ref_label"], "漲" if d > 0 else "跌"),
           "entry": c, "exit": round(ex, 1), "exit_reason": why, "exit_label": _hm(int(mm[k])),
           "points": round(d * (ex - c) - cost, 1), "cost": cost, "tpsl_points": round(tp, 1),
           "src": SRC_NAME["night"]}
    row.update(base)
    return row


# ══ 落地 ══════════════════════════════════════════════════════════════

def _valid_row(o):
    if not isinstance(o, dict) or o.get("lane") not in LANES:
        return False
    d = o.get("date")
    if not isinstance(d, str) or not _DATE_RE.match(d) or o.get("decision") not in _DECISIONS:
        return False
    if o["decision"] == "不做":
        return True
    pts = o.get("points")
    return (not isinstance(pts, bool) and isinstance(pts, (int, float)) and np.isfinite(pts)
            and o.get("exit_reason") in _EXITS)


def _read_unlocked():
    rows, st = {}, {"lines": 0, "ok": 0, "bad": 0, "dup": 0, "blank": 0, "files": 0}
    d = SIM_DIR
    if not d.exists():
        return rows, st
    for f in sorted(d.iterdir()):
        if not _MONTH_RE.match(f.name):
            continue
        st["files"] += 1
        for ln in f.read_text(encoding="utf-8", errors="replace").splitlines():
            st["lines"] += 1
            if not ln.strip():
                st["blank"] += 1
                continue
            try:
                o = json.loads(ln)
            except Exception:
                st["bad"] += 1
                continue
            if not _valid_row(o):
                st["bad"] += 1
                continue
            k = (o["lane"], o["date"])
            if k in rows:
                st["dup"] += 1
                continue
            rows[k] = o
            st["ok"] += 1
    return rows, st


def read_rows():
    """⇒ ({(lane, date): 列}, 計數)。等式：lines ＝ ok ＋ bad ＋ dup ＋ blank。"""
    with _FILE_LOCK:
        rows, st = _read_unlocked()
    STATE["file"] = dict(st)
    if (st["bad"] or st["dup"]) and _LOGGED.get("bad") != (st["bad"], st["dup"]):
        _LOGGED["bad"] = (st["bad"], st["dup"])       # 數字變了才再講一次（畫面每分鐘問一次，不要刷屏）
        log("sim_lanes/ 有 %d 列讀不出來、%d 列是重複的（已跳過）" % (st["bad"], st["dup"]))
    return rows, st


def append_row(row):
    """同一個（lane, date）已經有定論 ⇒ ⛔ 不寫（鎖裡重讀一次檔）。回 True＝真的寫了。⛔ 一定是 open("a")。"""
    if not _valid_row(row):
        raise ValueError("模擬列格式不對：%r" % (row,)[:200])
    with _FILE_LOCK:
        rows, _st = _read_unlocked()
        if (row["lane"], row["date"]) in rows:
            return False
        SIM_DIR.mkdir(parents=True, exist_ok=True)
        out = dict(row)
        out["wrote_at"] = datetime.now().isoformat(timespec="seconds")
        with (SIM_DIR / (row["date"][:7] + ".jsonl")).open("a", encoding="utf-8") as f:
            f.write(json.dumps(out, ensure_ascii=False) + "\n")
            f.flush()
    return True


# ══ 抓資料（背景；沿用 strategy_lab.fetch_today 的防護）═════════════════════

def _set_fetch(kind, status, msg, now):
    STATE["fetch"][kind] = {"status": status, "msg": msg, "at": now.strftime("%Y-%m-%d %H:%M:%S")}
    return status


def fetch_gate(kind, api, has_position, now):
    """
    跟永豐要任何東西之前的共用防護。回 None＝可以抓，否則回狀態字串（已記進 STATE["fetch"][kind]）。
      ⛔ 08:30~13:50 不抓（整個日盤都是真單的時間，完全不跟它搶連線）
      ⛔ 上一次失敗還沒過 10 分鐘不抓
      ⛔ 有部位不抓（問不到 ⇒ 當成有部位；面板注入的 has_position 自己也是「沒有確定答案就回 True」）
      ⛔ 流量 > 85%（或讀不到上限）不抓
    ⚠️ 順序：部位排在「沒連線／隔 10 分鐘」後面 —— 面板注入的那支會跟券商問部位（唯讀），不要每分鐘白問。
    """
    if QUIET_FROM <= now.time() < QUIET_TO:
        return _set_fetch(kind, "quiet", "%s~%s 不抓（日盤）" % (QUIET_FROM.strftime("%H:%M"), QUIET_TO.strftime("%H:%M")), now)
    if api is None:
        return _set_fetch(kind, "no_api", "面板還沒連上永豐", now)
    lf = _FAIL_AT.get(kind)
    if lf is not None and (now - lf).total_seconds() < RETRY_S:
        return "retry_wait"
    try:
        pos = has_position()
    except Exception:
        pos = True
    if pos is not False:                  # ⛔ 只有明確的 False 才算沒部位（None／"unknown"／其他 ⇒ 當成有）
        return _set_fetch(kind, "position", "有部位（或問不到部位），先不抓", now)
    try:
        u = api.usage(timeout=SL.USAGE_TIMEOUT_MS)
        used, lim = float(u.bytes), float(u.limit_bytes)
    except Exception:
        _FAIL_AT[kind] = now              # 問不到流量也算失敗：隔 10 分鐘，⛔ 不要每分鐘去敲
        _set_fetch(kind, "error", "讀不到永豐流量，10 分鐘後再試", now)
        raise
    if lim <= 0 or used / lim > SL.USAGE_MAX:
        return _set_fetch(kind, "usage_high", "永豐流量已用 %.0f%%（超過 85%% 不抓）" % (used / lim * 100)
                          if lim > 0 else "讀不到流量上限，不抓", now)
    return None


def fetch_ticks_day(api, has_position, d, now, qt=None):
    """
    補抓某個**過去**交易日的日盤逐筆，存進 strategy_lab 的 tick_hist/ticks/（兩邊共用）。
    ⛔ 今天 15:00 以前不抓（13:50~15:00 是 strategy_lab.fetch_today 的班）；檔案已經在 ⇒ 不抓；
    今天問過而且沒有成交 ⇒ 今天不再問；失敗／不完整 ⇒ 隔 10 分鐘。
    ⛔ 例外往外丟給 step() 吞（計數）；呼叫前一定先過 fetch_gate。
    """
    ds = str(d)
    if (SL._ticks_dir() / (ds + ".csv.gz")).exists():
        return "exists"
    if d > now.date() or (d == now.date() and now.time() <= SL.FETCH_UNTIL):
        return "too_early"
    if ("ticks", ds, str(now.date())) in _TRIED:
        return "tried"
    g = fetch_gate("ticks", api, has_position, now)
    if g is not None:
        return g
    try:
        if qt is None:
            import shioaji as sj
            qt = (getattr(sj, "TicksQueryType", None) or sj.constant.TicksQueryType).RangeTime
        contract = api.Contracts.Futures.TMF[SL.CONTRACT_CODE]
        tk = api.ticks(contract=contract, date=ds, query_type=qt,
                       time_start="08:45:00", time_end="13:45:00", timeout=SL.TICKS_TIMEOUT_MS)
        df = pd.DataFrame({**tk})
    except Exception:
        _FAIL_AT["ticks"] = now
        _set_fetch("ticks", "error", "補抓 %s 的逐筆失敗，10 分鐘後再試" % ds, now)
        raise
    if df.empty:
        _TRIED.add(("ticks", ds, str(now.date())))
        return _set_fetch("ticks", "empty", "%s 沒有日盤成交（休市？）" % ds, now)
    if not SL._complete(df, d):
        _FAIL_AT["ticks"] = now
        return _set_fetch("ticks", "incomplete", "%s 的逐筆不完整，10 分鐘後再試" % ds, now)
    SL._ticks_dir().mkdir(parents=True, exist_ok=True)
    out = SL._ticks_dir() / (ds + ".csv.gz")
    tmp = out.with_name(out.name + ".tmp")
    df.to_csv(tmp, index=False, compression="gzip")
    tmp.replace(out)
    SL.build_cache(ds)
    _FAIL_AT["ticks"] = None
    return _set_fetch("ticks", "saved", "已補 %s（%s 筆）" % (ds, format(len(df), ",")), now)


def _csv_bars(since):
    """本機 1 分 K（只留 since 之後，依 mtime 快取）⇒ DataFrame(ts, High, Low, Close) 或 None"""
    f = MIN1_CSV
    if not f.exists():
        return None
    s = f.stat()
    key = (str(f), s.st_mtime_ns, s.st_size, str(since))
    if _CSV["key"] != key:
        px = pd.read_csv(f, usecols=["ts", "High", "Low", "Close"])
        px["ts"] = pd.to_datetime(px["ts"])
        px = px[px["ts"] >= pd.Timestamp(since)].reset_index(drop=True)
        _CSV.update(key=key, df=px)
    return _CSV["df"]


def night_bars_local(E, since):
    """⇒ (這一晚相關的本機 K 棒 DataFrame, E 那天日盤有沒有 K 棒, 本機最早日, 本機最晚日)"""
    px = _csv_bars(since)
    if px is None or px.empty:
        return None, False, None, None
    dd = px["ts"].dt.date
    E1 = E + timedelta(days=1)
    g = px[(dd == E) | (dd == E1)]
    tm = px["ts"].dt.hour * 60 + px["ts"].dt.minute
    day_e = bool(((dd == E) & (tm >= 8 * 60 + 46) & (tm <= 13 * 60 + 45)).any())
    return g, day_e, dd.min(), dd.max()


def fetch_night(api, has_position, E, local, now):
    """跟永豐要 E 與 E+1 兩天的 1 分 K，跟本機合併。回 DataFrame（到齊）或狀態字串。"""
    if ("kbars", str(E), str(now.date())) in _TRIED:
        return "tried"
    g = fetch_gate("kbars", api, has_position, now)
    if g is not None:
        return g
    frames = [] if local is None or local.empty else [local]
    got, errs = 0, []
    try:
        contract = api.Contracts.Futures.TMF[SL.CONTRACT_CODE]
    except Exception:
        _FAIL_AT["kbars"] = now
        _set_fetch("kbars", "error", "取不到合約，10 分鐘後再試", now)
        raise
    # ⚠️ 一天一天要（跟 live_panel._raw_days 同一招）：區間端點碰到非交易日（週六）永豐會整段回 404
    for dd in (E, E + timedelta(days=1)):
        try:
            df = pd.DataFrame({**api.kbars(contract, start=str(dd), end=str(dd))})
        except Exception as e:
            errs.append("%s：%s" % (dd, str(e)[:60]))
            continue
        if not df.empty:
            df = df.copy()
            df["ts"] = pd.to_datetime(df["ts"])
            frames.append(df[["ts", "High", "Low", "Close"]])
            got += 1
    if not got:
        if errs:
            _FAIL_AT["kbars"] = now
            _set_fetch("kbars", "error", "抓 %s 晚上的 1 分 K 失敗，10 分鐘後再試" % E, now)
            raise RuntimeError("kbars 失敗：" + "；".join(errs))
        _TRIED.add(("kbars", str(E), str(now.date())))
        return _set_fetch("kbars", "empty", "%s 與隔天都沒有 1 分 K（休市？）" % E, now)
    out = pd.concat(frames, ignore_index=True).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    mm, _h, _l, _c = night_frame(out, E)
    if not night_complete(mm):
        _FAIL_AT["kbars"] = now
        return _set_fetch("kbars", "incomplete", "%s 晚上的 1 分 K 還沒到齊，10 分鐘後再試" % E, now)
    _FAIL_AT["kbars"] = None
    _set_fetch("kbars", "saved", "已補 %s 晚上的 1 分 K" % E, now)
    return out


# ══ 一輪 ══════════════════════════════════════════════════════════════

def _tick_days_before(day, n):
    """tick_hist/ticks/ 裡**早於 day** 的日子，新到舊最多 n 個（⛔ 只看檔名，不讀檔）"""
    out, dd = [], SL._ticks_dir()
    if not dd.exists():
        return out
    for f in sorted(dd.iterdir(), key=lambda x: x.name, reverse=True):
        m = re.match(r"^(\d{4}-\d{2}-\d{2})\.csv\.gz$", f.name)
        if m and m.group(1) < str(day):
            out.append(m.group(1))
            if len(out) >= n:
                break
    return out


def _box_pairs(day, n=ORB_HIST_N):
    """
    ORB 濾網的歷史：**這一天以前**最近 n 個算得出箱子的交易日 ⇒ [(日期, 箱子寬度%)]（舊到新）。
    ⚠️ 每天要重讀一次逐筆很貴 ⇒ 算過的放 `_BOX` 記憶體快取（⛔ 不另外開檔案來寫）。
    ⚠️ 找不到足額**就讓它不足**（呼叫端會記「資料缺」等逐筆補回來），⛔ 不補 0、不放寬 n。
    """
    out = []
    _BOX_ST.update(busy=True, day=str(day), done=0, need=n)
    try:
        for ds in _tick_days_before(day, n * 2):    # 多找一些：那天 09:00~09:05 沒成交的要跳過
            if ds not in _BOX:
                try:
                    Dh = SL.load_day(ds)
                except Exception:
                    Dh = None
                _BOX[ds] = None if Dh is None else orb_box_pct(ds, Dh)
            if _BOX[ds] is not None:
                out.append((ds, _BOX[ds]))
                _BOX_ST["done"] = len(out)
                if len(out) >= n:
                    break
    finally:
        _BOX_ST["busy"] = False                    # ⛔ 例外也要收掉旗標，不然畫面永遠說「計算中」
    return sorted(out)


def box_window(day, n=ORB_HIST_N):
    """
    ⛔ 窗口**要帶起訖日期與跨度**（lab-qa 2026-09-16 退件 S1）：資料有洞的時候，
    「過去 20 天」可能橫跨一年多 —— 卡上只寫「過去 20 天」會騙人。
    ⇒ {"vals": [...], "d0": 最舊, "d1": 最新, "span": 起訖跨幾個日曆天}
    """
    pairs = _box_pairs(day, n)
    d0 = pairs[0][0] if pairs else None
    d1 = pairs[-1][0] if pairs else None
    span = None if not pairs else (date.fromisoformat(d1) - date.fromisoformat(d0)).days + 1
    return {"vals": [v for _d, v in pairs], "d0": d0, "d1": d1, "span": span}


def box_hist(day, n=ORB_HIST_N):
    """只要那串數字（探針與測試用）。⚠️ 面板那條路一律走 `box_window()` —— 它才帶得出跨度。"""
    return box_window(day, n)["vals"]


def _box_win(box_vals):
    """
    把 `box_vals` 正規化成窗口 dict。⚠️ 直接傳 list 進來＝**沒有日期資訊**（治具／探針用），
    這種情況跨度是 None ⇒ 跨度那道閘門跳過（⛔ 面板那條路一律傳 `box_window()` 的 dict）。
    """
    if isinstance(box_vals, dict):
        return box_vals
    return {"vals": None if box_vals is None else list(box_vals), "d0": None, "d1": None, "span": None}


def box_win_txt(w):
    """窗口的字面：「過去 20 天（2025-05-14~2026-09-14）」；沒有日期資訊就只寫天數。"""
    n = len(w.get("vals") or [])
    return ("過去 %d 天（%s~%s）" % (n, w["d0"], w["d1"])) if w.get("d0") else ("過去 %d 天" % n)


def box_scan_msg():
    """正在掃箱子歷史時給畫面的一句話（⛔ 空字串＝沒在掃）。⚠️ 只有面板剛啟動的第一輪會慢。"""
    if not _BOX_ST["busy"]:
        return ""
    return ("箱子寬度歷史計算中（%s：已掃 %d／%d 天）—— 只有面板剛啟動的第一輪會這樣"
            % (_BOX_ST["day"], _BOX_ST["done"], _BOX_ST["need"]))


def _step_ticks(now, rows, get_api, has_position):
    """逐筆那六條。⛔ **同一天只讀一次逐筆、只建一次 day_pack**，六條共用同一份答案。"""
    pend = {k: {} for k in TICK_LANES}
    hist = None
    try:
        if FAST_HIST.exists() and _CFG["hist_read"] is not None:
            hist, bad, dup = _CFG["hist_read"](FAST_HIST)
            STATE["hist_bad"], STATE["hist_dup"] = bad, dup
    except Exception as e:
        _note_err("讀 fast_hist.jsonl", e)
        hist = None
    hist_days = None if hist is None else {str(r.get("date")) for r in hist}
    fetched = False
    for d in fast_days(now):
        ds = str(d)
        want = [ln for ln in TICK_LANES if (ln, ds) not in rows]
        if not want:
            continue
        if d == now.date() and now.time() < SL.FETCH_FROM:
            continue                      # 今天還沒收盤：不是「缺」，是還沒到（今天狀態那一格講）
        try:
            D = SL.load_day(ds) if (SL._ticks_dir() / (ds + ".csv.gz")).exists() else None
            if D is None and not fetched and not (SL._ticks_dir() / (ds + ".csv.gz")).exists():
                fetched = True            # 一輪最多補抓一天（不要一口氣把流量吃掉）
                st = fetch_ticks_day(get_api(), has_position, d, now)
                if st == "saved":
                    D = SL.load_day(ds)
                elif st in ("empty", "tried", "exists", "too_early"):
                    fetched = False       # ⭐ 休市／已問過／不該抓：沒有真的抓到東西，⛔ 不佔「每輪補一天」的名額（lab-qa R3）
            # ⭐ 休市落地成定論（lab-qa R3）：永豐說那天沒有日盤成交、那天是過去的日子、
            #    而且 fast_hist 裡**沒有**那天（有的話那天一定開過盤 ⇒ 永豐回空只是暫時的，⛔ 不准記成休市）
            if (D is None and ("ticks", ds, str(now.date())) in _TRIED and d < now.date()
                    and hist_days is not None and ds not in hist_days):
                for ln in want:
                    res = _none_row(ln, ds, "holiday", "休市（永豐那天沒有日盤成交）")
                    if append_row(res):
                        rows[(ln, ds)] = res
                continue
            # ⛔ 一天只算一次：箱子歷史只有「開箱／多方聯軍」要用才掃（很貴）
            need_box = any(ln in ("orb", "union") for ln in want)
            bh = box_window(ds) if (D is not None and need_box) else None
            pk = day_pack(ds, D, hist, bh)
            for ln in want:
                try:
                    res = TICK_EVAL[ln](ds, D, hist, bh, pack=pk)
                    if res.get("pending") and res["why"] == "no_ticks" and ("ticks", ds, str(now.date())) in _TRIED:
                        res = _pending("no_ticks", "沒有當天逐筆（問過永豐：那天沒有日盤成交，休市？）")
                    if res.get("pending"):
                        pend[ln][ds] = res
                    elif append_row(res):
                        rows[(ln, ds)] = res
                except Exception as e:      # ⛔ 一條壞掉只停那一條，其他四條照算
                    _note_err("%s %s" % (LANE_NAME[ln], ds), e)
                    pend[ln][ds] = _pending("error", "計算出錯：" + str(e)[:80])
        except Exception as e:
            _note_err("逐筆 %s" % ds, e)
            for ln in want:
                pend[ln][ds] = _pending("error", "計算出錯：" + str(e)[:80])
    for ln in TICK_LANES:
        STATE["pending"][ln] = pend[ln]


def _step_night(now, rows, get_api, has_position):
    pend = {}
    evs = night_evenings(now)
    since = min(evs) - timedelta(days=3)
    fetched = False
    for E in evs:
        es = str(E)
        if ("night", es) in rows:
            continue
        try:
            local, day_e, lo, hi = night_bars_local(E, since)
            bars = local
            mm = night_frame(local, E)[0] if local is not None else np.array([])
            if not night_complete(mm):
                if lo is not None and lo < E and hi is not None and hi > E + timedelta(days=1) and not day_e:
                    res = _none_row("night", E, "holiday", "%s 休市（本機 1 分 K 前後都有、那天沒有日盤）" % es)
                    if append_row(res):
                        rows[("night", es)] = res
                    continue
                if E in _NIGHT_API:
                    bars = _NIGHT_API[E]
                elif not fetched:
                    fetched = True        # 一輪最多跟永豐要一晚
                    got = fetch_night(get_api(), has_position, E, local, now)
                    if isinstance(got, pd.DataFrame):
                        _NIGHT_API[E] = got
                        bars = got
                    elif got in ("empty", "tried"):
                        fetched = False   # ⭐ 休市／已問過：⛔ 不佔「每輪一晚」的名額（lab-qa R3）
                if ("kbars", es, str(now.date())) in _TRIED and not day_e:
                    # ⭐ 永豐說 E 與 E+1 兩天都沒有 1 分 K ⇒ 那一晚沒有夜盤，落地成定論（⛔ 不要每天重問）
                    #    ⛔ 本機 csv 有 E 的日盤 ⇒ 那天開過盤，永豐回空只是暫時的，不准記成休市
                    res = _none_row("night", E, "holiday", "休市（永豐 %s 與隔天都沒有 1 分 K）" % es)
                    if append_row(res):
                        rows[("night", es)] = res
                    continue
            res = night_eval(E, bars)
            if res.get("pending"):
                pend[es] = res
            elif append_row(res):
                rows[("night", es)] = res
        except Exception as e:
            _note_err("美股開盤順勢 %s" % es, e)
            pend[es] = _pending("error", "計算出錯：" + str(e)[:80])
    for k in [k for k in _NIGHT_API if k not in evs]:
        _NIGHT_API.pop(k, None)           # 滑出窗口的就丟掉（記憶體不要一直長）
    STATE["pending"]["night"] = pend


def step(get_api, has_position, now=None):
    """背景一輪。⛔ 永遠不往外丟例外（計數＋主控台＋畫面）。"""
    try:
        now = now or datetime.now()
        rows, _st = read_rows()
        _step_ticks(now, rows, get_api, has_position)
        _step_night(now, rows, get_api, has_position)
        STATE["steps"] += 1
        STATE["last_step_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
        return True
    except Exception as e:
        _note_err("step", e)
        return False


def loop(get_api, has_position, every=LOOP_EVERY):
    """面板 main() 起的 daemon 執行緒。⛔ 永遠不丟例外、不碰主迴圈。"""
    while True:
        try:
            step(get_api, has_position)
        except Exception as e:            # step 自己已經吞了；這層是保險
            _note_err("loop", e)
        try:
            time.sleep(every)
        except Exception:
            pass


# ══ 端點 /api/sim/state（唯讀）═══════════════════════════════════════════

def _rule_text(lane):
    """
    每一條的規則句。⛔ **後端給**：前端不准自己寫死時刻與點數（兩邊各寫一份就會分岔）。
    ⚠️ 時刻與百分比全部從常數／注入的設定組出來，⛔ 不在字串裡另外寫死。
    """
    if lane == "night":
        return ("美股開盤（夏令 21:30、冬令 22:30）後 5 分鐘往哪走就順勢做 1 口；停利停損 ±%g%%，"
                "同一分鐘兩邊都碰到算停損，沒碰到就 04:58 平" % (NIGHT_TPSL_FRAC * 100))
    if lane == "union":
        at = _hms_sec(_CFG["rev_sec"]) if _CFG.get("rev_sec") else "?"
        # ⛔ 候選的名字是「快攻／開箱／**純回馬**」（「回馬槍」是 hmq 那一條，⛔ 不要混用）
        return ("把三個候選收齊（%s %s／%s 第一次突破／%s %s），只取做多的，"
                "取觸發最早的那一個，照它自己的進出場規則做，一天最多一口；"
                "沒有做多的候選就不做" % (LANE_NAME["fast"], FAST_PX_AT, LANE_NAME["orb"],
                                    LANE_NAME["rev"], at))
    if lane == "orb":
        return ("%s 的最高最低當箱子；箱子寬度%%（箱寬÷進場價）比過去 %d 個交易日的中位數窄就不做，"
                "否則第一次突破上緣做多、跌破下緣做空（一天最多 1 次）；停損＝箱子另一端、不設停利，"
                "沒碰到就 %s 平（結算日 %s）"
                % (ORB_BOX_AT, ORB_HIST_N, _cut_at(SL.T1343_30), _cut_at(SL.T1330)))
    r, q = _CFG.get("rule") or {}, _CFG.get("pctl")
    if not r or q is None or _CFG.get("rev_sec") is None:
        return "（規則函式沒有接上）"
    share = ("%g 成" % (q / 10)) if q % 10 == 0 else ("第 %g 百分位" % q)
    frac = r.get("tpsl_frac")
    tp = ("%g%%" % (frac * 100)) if frac else "?"
    quick = "%s 比 %s 走得比過去 %d 天裡 %s的日子快" % (FAST_PX_AT, FAST_REF_AT, r["window"], share)
    at = _hms_sec(_CFG["rev_sec"])
    eod = "沒碰到就 %s 平（結算日 %s）" % (_cut_at(SL.T1343_30), _cut_at(SL.T1330))
    if lane == "fast":
        return "%s，就順勢做 1 口；停利停損 ±%s（以進場價算），%s" % (quick, tp, eod)
    if lane == "fast11":
        return ("%s，就順勢做 1 口；停利停損 ±%s（以進場價算），沒碰到就 %s 平"
                % (quick, tp, FAST11_CUT_AT))
    if lane == "hmq":
        return ("%s，就順勢做 1 口；「不快」就等 %s，方向跟 %s 相反才順新方向做 1 口；"
                "停利停損 ±%s（以進場價算），%s" % (quick, at, FAST_PX_AT, tp, eod))
    if lane == "rev":
        return ("只做「不快」那一半：等 %s，方向跟 %s 相反才順新方向做 1 口（快的日子不做）；"
                "停利停損 ±%s（以進場價算），%s" % (at, FAST_PX_AT, tp, eod))
    return ""


def _months(now, lane_rows):
    ym = [(now.year, now.month)]
    while len(ym) < MONTHS_SHOWN:
        y, m = ym[-1]
        ym.append((y, m - 1) if m > 1 else (y - 1, 12))
    out = []
    for i, (y, m) in enumerate(ym):
        key = "%04d-%02d" % (y, m)
        rs = [r for r in lane_rows if r["date"][:7] == key]
        # ⛔ 端點不可以被一列壞資料打成 500：只加「真的是數字」的點數（`_valid_row` 已經擋在前面，
        #    這裡是第二道 —— 萬一以後有人放寬了那道，`/api/sim/state` 也不會整個掛掉）。
        tr = [r for r in rs if r["decision"] != "不做"]
        pts = [r["points"] for r in tr
               if not isinstance(r.get("points"), bool) and isinstance(r.get("points"), (int, float))]
        out.append({"month": key, "label": "本月" if i == 0 else "%d 月" % m, "this": i == 0,
                    "points": round(sum(pts), 1), "trades": len(tr), "days": len(rs)})
    return out


def _slim(r):
    keys = ("date", "decision", "why", "reason", "entry", "exit", "exit_reason", "points",
            "move_pct", "thr_pct", "ref", "px", "c", "ref_label", "c_label", "us_open", "src", "exit_label")
    return {k: r.get(k) for k in keys if k in r}


def _today(lane, now, rows):
    t = now.date()
    if lane in TICK_LANES:
        if t.weekday() > 4:
            return {"date": str(t), "msg": "今天不是交易日"}
        r = rows.get((lane, str(t)))
        if r:
            return {"date": str(t), "msg": "已算好", "row": _slim(r)}
        if now.time() < SL.FETCH_FROM:
            return {"date": str(t), "msg": "今天 13:50 收盤後抓到當天逐筆才算"}
        p = STATE["pending"][lane].get(str(t))
        if p:
            return {"date": str(t), "msg": p["msg"]}
        # ⛔ 掃箱子歷史時要說「還在算」，⛔ 不可以看起來像「沒有資料」（PM 2026-09-16 裁示）
        scan = box_scan_msg() if lane in ("orb", "union") else ""
        return {"date": str(t), "msg": scan or "等背景下一輪（每分鐘一次）"}
    # 夜盤：今晚那一場要等 E+1 05:10；凌晨還沒到 05:10 時講的是昨晚那一場
    E = t if now.time() >= NIGHT_READY else t - timedelta(days=1)
    if E.weekday() > 4:
        return {"date": str(E), "msg": "%s 晚上沒有夜盤" % ("今天" if E == t else "昨天")}
    r = rows.get(("night", str(E)))
    if r:
        return {"date": str(E), "msg": "已算好", "row": _slim(r)}
    return {"date": str(E), "msg": "%s晚美股 %s 開盤，隔天 05:10 之後才算" % ("今" if E == t else "昨", _hm(us_open_min(E)))}


def state(now=None):
    """GET /api/sim/state 的內容。⛔ 唯讀（只讀 sim_lanes/ 與記憶體），不抓資料、不寫檔。"""
    now = now or datetime.now()
    rows, st = read_rows()
    lanes = {}
    for lane in LANES:
        lr = sorted((r for (ln, _d), r in rows.items() if ln == lane), key=lambda r: r["date"], reverse=True)
        pend = STATE["pending"][lane]
        lanes[lane] = {"name": LANE_NAME[lane], "rule": _rule_text(lane), "src": SRC_NAME[lane],
                       "months": _months(now, lr), "recent": [_slim(r) for r in lr[:RECENT_N]],
                       "n_rows": len(lr), "today": _today(lane, now, rows),
                       "pending": [{"date": d, "why": p["why"], "msg": p["msg"]}
                                   for d, p in sorted(pend.items(), reverse=True)],
                       "fetch": STATE["fetch"]["ticks" if lane in TICK_LANES else "kbars"],
                       "scan": box_scan_msg() if lane in ("orb", "union") else ""}
    eq = st["lines"] == st["ok"] + st["bad"] + st["dup"] + st["blank"]
    return {"ok": True, "now": now.strftime("%Y-%m-%d %H:%M:%S"), "wired": wired(),
            "note": "成本已扣；夜盤用 1 分 K 近似",
            "lanes": lanes, "file": dict(st, eq_ok=eq),
            "errors": STATE["errors"], "last_err": STATE["last_err"], "last_err_at": STATE["last_err_at"],
            "hist_bad": STATE["hist_bad"], "hist_dup": STATE["hist_dup"],
            "steps": STATE["steps"], "last_step_at": STATE["last_step_at"]}
