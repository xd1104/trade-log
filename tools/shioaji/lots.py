# -*- coding: utf-8 -*-
"""
口數（多口）的**唯一正本**（2026-10-14 加；規則是 Benson 09-30 拍板的「加減碼規則・積極版」，
10-14 再拍板：升級要兩段式確認、夜盤波動規則 B，以及 PM 補的四條細節）。

「今天日盤送幾口、今晚夜盤送幾口、風控 B 上限用幾口算、保證金要算幾口」**全部問這一支**，
⛔ 其他模組不准自己算口數（那就是第二把尺）。

================================================================
⛔⛔ 最重要的一條：**狀態檔不存在 ⇒ 1＋1（跟 10-14 以前一模一樣）**
================================================================
目前的「級」存在 `LOTS_LEVEL.json`（⛔ gitignore）。
  ・檔案不存在 ⇒ 第 0 級 ⇒ 聯軍 1 口＋跟勢 1 口。
  ・檔案看不懂（壞掉、被改壞、欄位型別不對）⇒ **一律退回 1＋1**（少送是安全方向），⛔ 不猜，⛔ 也不覆蓋那個檔。
  ・階梯上的口數（`goal.LADDER`）最大 10 口；broker 那一層另有 `QTY_MAX` 再擋一次。
階梯的正本是 `goal.LADDER`（⛔ 這裡不另寫一份表）。第 0 級（7 萬以下）與第 1 級（7 萬）都是 1＋1：
⭐ 第 0 → 1 級**不算一次升級**（不用確認、不佔冷卻；權益到 7 萬 daily_check 自己標上去，跟 goal.plan 同一條）。

================================================================
升級／減級（規則：trade-log/CLAUDE.md「📝 加碼／減碼規則」09-30 積極版）
================================================================
・**減級是自動的**（安全方向）：`daily_check()` 每多一列 equity/ 紀錄就跑一次（面板 poll_equity 那條執行緒；
  14:00 落地之後 ⇒ 當晚夜盤＝「下一個交易日」起生效）。
    ① 權益 < 目前這一級門檻 × 0.85 ⇒ 退到權益符合的那一級（goal.level_of）。
    ② 某一條在退休標準的「降級」⇒ 那條最多 1 口（`cap`）；「退休」⇒ 0 口
       ⚠️ 退休 ⇒ 0 口預設**不做**（`RETIRE_ZERO=False`，見下面那段）。
    ③ 停損失靈事故（停損沒被執行：出場不是停損卻賠超過停損，或每口賠超過停損 × INCIDENT_MULT）⇒ 暫停升級 6 個月。
・**升級要 Benson 在面板上兩段式確認**（10-14 拍板；同一天晚上老闆再改規則 ⇒ 見下面「升級（試車 → 跳級）」那一段）：
    ⛔ 09-30 的「每月第一個交易日、一次一級、冷卻 3 個月」**10-14 起作廢**。
    每個交易日收盤後（daily_check）重算：在 1＋1 ⇒ 建議試車 2＋1；試車通過 ⇒ 建議跳到權益撐得起的那一級；
    之後再升至少隔一個日曆月。建議有 `id`＝「建議那天:從>到」；條件掉了就**收回**（`withdrawn`）。⛔ 不自己升。
    `apply_upgrade(offer_id)`（面板 POST /api/lots/upgrade 呼叫；過 fire_post_guard、前端兩段式）：**只接受現在建議的那一級**，
    而且當下**重驗一次條件**（品質／策略用每日檢查存好的那一份）⇒ ⭐ 10-21 起**確認後立刻生效**（見 apply_upgrade）；
    生效前 `revoke_pending()` 可以撤回；策略在生效前掉到觀察／降級 ⇒ 自動作廢。
・風控 B 上限 ＝ 400 ×（**本月用過的最大** a＋b）—— ⛔ 不跟著月中減級掉（不然減級那天就立刻停單到月底）。

================================================================
夜盤波動規則 B（10-14 拍板，`NIGHT_VOL_ON=True`）
================================================================
階梯上的 b ＝「預測夜盤波動偏高」那晚的口數；其他晚上 max(1, b−1) 口。
預測值 ＝（前 1 晚 ＋ 前 5 晚平均 ＋ 前 22 晚平均 的夜盤振幅%）÷ 3；偏高 ＝ ≥ 1.03%（⛔ 固定不調）。
出處 tick-research/sizing_results_2026-09-30.md 第 30 行（S2）。
⭐ 1＋1（b＝1）時兩邊都是 1 口 ⇒ 跟現在完全一樣（test_lots 有證明）。
振幅沿用【健檢】的同一把尺（`health._sessions()`，tmf_1min.csv）；⛔ 不另算一份。
⚠️ 研究的 1.03% 是用**大台** 1 分 K 算的，這裡用**微台**（面板的 txf_1min.csv 08-11 起就沒再更新）。
   PM 2026-10-14 同意：微台與大台價差中位數只有 1 點，振幅% 幾乎一樣。

⛔ 這一支**不送單、不碰 broker 的狀態**。會寫的檔只有兩個出口：`LEVEL_FILE`、`LOG_DIR`（測試要一起導走）。
"""
import json
import math
import pathlib
import threading
import time
from datetime import date, datetime, timedelta

try:
    import goal          # ⛔ 階梯的唯一正本（goal.LADDER）
except Exception:        # noqa: BLE001  ⛔ 讀不到階梯 ⇒ 永遠 1＋1（少送是安全方向）
    goal = None

HERE = pathlib.Path(__file__).resolve().parent
LEVEL_FILE = HERE / "LOTS_LEVEL.json"    # ⛔ gitignore；不存在 ⇒ 1＋1
LOG_DIR = HERE / "lots"                  # ⛔ gitignore；每一次減級／建議／確認／生效／撤回一列（只 append）
VOL_CSV = None                           # None ⇒ health.MIN1_CSV（tmf_1min.csv）；測試會導走

DOWN_FRAC = 0.85            # 權益 < 目前這一級門檻 × 0.85 ⇒ 減級
UP_GAP_MONTHS = getattr(goal, "UPGRADE_GAP", 3)   # 升完幾個月不再升（goal.py 那個數）
PAUSE_MONTHS = 6            # 停損失靈事故 ⇒ 暫停升級幾個月
RECENT_N = 30               # 升級看「最近幾筆自動單」（不滿就用現有的筆數，PM 10-14）
SLIP_MAX = 4.0              # 平均滑價上限（點）
CAP_PER_LEG = 400           # 風控 B：每一條每口（＝ risk_cap.CAP_PER_LEG）
NOTICE_DAYS = 3             # 減級／降級／暫停的提示顯示幾個交易日（UI 規格建議 3）
# ⭐⭐ 2026-10-19 改（Benson 同意）：舊版「每口賠超過停損＋20 點」把 10/19 急跌的正常停損判成失靈
#    （09:57:29.67 買價 46450 → 0.13 秒後 46424，一口氣跳過停損價 46437；面板 0.5 秒內停損成交 46402，多賠 35 點）
#    ⇒ 暫停升級到 2027-04 是誤判。「失靈」要抓的是**停損沒被執行**（停電、面板當掉、抱到收盤才平），不是急跌滑價。
#    新判法（任一條）：① 出場原因不是停損／停利，而且每口賠超過自己的停損點數；② 每口賠超過停損點數 × INCIDENT_MULT。
#    ⛔ 1.5 這個數是 **PM 2026-10-19 定的**，⛔ 不是研究算出來的。
INCIDENT_MULT = 1.5
INCIDENT_EXEC = ("sl", "tp")     # 這兩種出場＝停損／停利有被執行
# ⚠️ 退休 ⇒ 0 口：09-30 的加碼規則寫「退休 ⇒ 0 口」，但 09-29 退休標準寫「開關一律 Benson 自己按」。
#    ⭐ PM 2026-10-14 拍板：**維持不自動歸零**（退休也只壓到 1 口），要不要關掉由他自己按；
#       畫面要講清楚「已退休：自動壓到 1 口，要不要關掉由你按」。
RETIRE_ZERO = False

# ⭐ 確認條成本表（每一級的代價）。⛔ 數字照抄研究，前端不算、不內插：
#    tick-research/capital_ladder_aggr_out_2026-10-14.txt（跑 scripts/capital_ladder.py 同一顆種子、同一套算法，
#    只把階梯換成 09-30 積極版；原本就有的幾列跟 capital_ladder_out.txt 逐字相同 ⇒ 對得上）。
#    (聯軍 a, 跟勢 b) ⇒ (從高點回落中位數, 回落最差 5%, 停損失靈最壞一次)，單位：元。
#    ⚠️ 保證金 ⛔ 不在這裡：一律用面板從帳戶／期交所學到的一口保證金 × max(a, b)。
LADDER_COST = {
    (1, 1): (20483, 42725, 23883), (2, 1): (30514, 59019, 47766), (3, 1): (42375, 80014, 71649),
    (3, 2): (50177, 100626, 71649), (4, 2): (61029, 118039, 95532), (5, 2): (72809, 138745, 119415),
    (6, 2): (84751, 160028, 143298), (6, 3): (91544, 177059, 143298), (7, 3): (103205, 197300, 167181),
    (8, 3): (115232, 218318, 191064), (9, 4): (133717, 255749, 214947), (10, 4): (145618, 277490, 238830),
}
LADDER_COST_SRC = "tick-research/capital_ladder_aggr_out_2026-10-14.txt"

# ── 夜盤波動規則 B ──
NIGHT_VOL_ON = True         # ⭐ 10-14 Benson 拍板採用 B
NIGHT_VOL_THR = 1.03        # ⛔ 固定不調（設計期第 67 百分位）
NIGHT_VOL_N = 22            # 要有前 22 晚才算得出預測
# 最新那一晚離今晚隔了超過幾個**交易日** ⇒ 資料太舊（面板／排程沒跑）⇒ 當成不偏高。
# ⭐ QA 10-14 建議：用交易日算（舊版用日曆天 4 天 ⇒ 春節／連假之後第一晚會被誤判太舊）。
#    交易日 ＝ 平日、而且不在 KNOWN_CLOSED（面板把 closed_days() 認得的休市平日餵進來）。
NIGHT_VOL_MAX_GAP_TD = 2
KNOWN_CLOSED = set()        # 'YYYY-MM-DD'；live_panel.poll_equity 每輪更新（⛔ 這一支不自己判斷假日）

LANES = ("union", "trend")  # 聯軍（日盤，a）／跟勢（夜盤，b）—— 跟 analyst_facts.REAL_LANES 同一組代號
LANE_NAME = {"union": "多方聯軍", "trend": "夜盤跟勢"}
# 試車（10-14 新規則）：none＝還沒試／on＝試車中（start＝試車生效那天）／passed＝通過（記住，減級之後不必重試）
TRIAL_DEFAULT = {"status": "none", "start": None, "n": 0, "slip": None, "reset": None, "passed_at": None,
                 "jumped": False, "miss": None}
DEFAULT = {"v": 1, "level": 0, "cap": {"union": None, "trend": None},
           "pause_until": None, "up_at": None, "checked": None, "up_month": None,
           "suggest": None, "pending": None, "month_max": None, "conds": None, "since": None,
           "trial": TRIAL_DEFAULT, "void": None}

_LOCK = threading.Lock()    # ⛔ 只保護狀態檔的讀改寫（poll 執行緒／之後的 HTTP 執行緒）


# ══ 小工具 ══════════════════════════════════════════════════════════

def _ladder():
    return tuple(getattr(goal, "LADDER", ()) or ())


def level_max():
    return len(_ladder())


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _d(x):
    return x if isinstance(x, date) and not isinstance(x, datetime) else (
        x.date() if isinstance(x, datetime) else date.fromisoformat(str(x)[:10]))


def next_weekday(d):
    """
    d 之後的下一個交易日（平日、而且不在 KNOWN_CLOSED）。
    ⚠️ 休市表只認得面板 closed_days() 認出來的：認不出來的國定假日就是那天沒盤，下一個真的盤照樣生效。
    """
    d = _d(d) + timedelta(days=1)
    while d.weekday() >= 5 or str(d) in KNOWN_CLOSED:
        d += timedelta(days=1)
    return d


def _add_weekdays(d, n):
    d = _d(d)
    for _ in range(n):
        d = next_weekday(d)
    return d


def trading_gap(d0, d1, closed=None):
    """d0 之後到 d1（含）有幾個交易日（平日、而且不在休市表）。d1 ≤ d0 ⇒ 0。"""
    d0, d1 = _d(d0), _d(d1)
    closed = KNOWN_CLOSED if closed is None else set(closed)
    n, d = 0, d0
    while d < d1 and n < 999:
        d += timedelta(days=1)
        if d.weekday() < 5 and str(d) not in closed:
            n += 1
    return n


def month_end(d):
    d = _d(d)
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1) - timedelta(days=1)


def first_weekday_of_month(y, m):
    d = date(y, m, 1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _add_months(d, n):
    if goal is not None:
        return goal._add_months(_d(d), n)
    d = _d(d)
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, min(d.day, 28))


def _fresh():
    return json.loads(json.dumps(DEFAULT))


def _trial_of(o):
    """狀態檔裡的 trial ⇒ 整理過的一份（看不懂的格子 ⇒ 預設；⛔ trial 只影響升級建議，不影響口數）。"""
    t = dict(TRIAL_DEFAULT)
    if not isinstance(o, dict):
        return t
    if o.get("status") in ("none", "on", "passed"):
        t["status"] = o["status"]
    for k in ("start", "reset", "passed_at"):
        v = o.get(k)
        if isinstance(v, str):
            try:
                date.fromisoformat(v[:10])
                t[k] = v[:10]
            except ValueError:
                pass
    if _is_int(o.get("n")) and 0 <= o["n"] <= 999:
        t["n"] = o["n"]
    if _num(o.get("slip")):
        t["slip"] = o["slip"]
    if isinstance(o.get("miss"), str):
        t["miss"] = o["miss"][:120]
    t["jumped"] = o.get("jumped") is True
    if t["status"] == "on" and not t["start"]:
        t["status"] = "none"
    return t


# ══ 狀態檔 ══════════════════════════════════════════════════════════

def _valid(o):
    """狀態檔內容合不合理 ⇒ None（合理）或一句原因。⛔ 任何一格不對就整份不用。"""
    if not isinstance(o, dict):
        return "內容不是一個物件"
    lv = o.get("level")
    if not _is_int(lv) or not (0 <= lv <= level_max()):
        return "level 不合理（%r）" % (lv,)
    cap = o.get("cap", {})
    if not isinstance(cap, dict):
        return "cap 不是物件"
    for k in LANES:
        c = cap.get(k)
        if c is not None and (not _is_int(c) or not (0 <= c <= 1)):
            return "cap.%s 不合理（%r）" % (k, c)
    for k in ("pause_until", "up_at"):
        v = o.get(k)
        if v is not None:
            try:
                date.fromisoformat(str(v))
            except ValueError:
                return "%s 不是日期（%r）" % (k, v)
    p = o.get("pending")
    if p is not None:
        if not isinstance(p, dict) or not _is_int(p.get("to")) or not (0 < p["to"] <= level_max()) \
                or p["to"] <= lv:
            return "pending 不合理（%r）" % (p,)
        try:
            date.fromisoformat(str(p.get("eff")))
        except ValueError:
            return "pending.eff 不是日期"
    mm = o.get("month_max")
    if mm is not None:
        ab = mm.get("ab") if isinstance(mm, dict) else None
        if not (isinstance(ab, list) and len(ab) == 2 and all(_is_int(x) and 0 <= x <= 99 for x in ab)):
            return "month_max 不合理（%r）" % (mm,)
    return None


def read_state(path=None):
    """
    ⇒ (狀態, 錯誤)。檔案不存在 ⇒ (預設＝第 0 級, None)；看不懂 ⇒ (預設, 一句原因)。
    ⛔ 看不懂時**照樣回預設**（1＋1）—— 呼叫端只要拿口數的話不必另外處理。
    """
    p = path or LEVEL_FILE
    o, last = None, None
    for i in range(IO_TRIES):
        try:
            if not p.exists():
                return _fresh(), None
            txt = p.read_text(encoding="utf-8")
        except OSError as e:
            # ⭐ QA 必修 5：Windows 上別的執行緒剛好在 replace ⇒ PermissionError（QA 實測狂讀寫時 360 次退回 1＋1）。
            #    稍等再讀；⛔ 還是讀不到也**不退回 1＋1**（那會讓 3＋1 的那天突然只送 1 口、風控上限也變小）
            #    ⇒ 用上一次讀成功的那份（同一支程式裡寫入者只有 lots.py 自己，上一次讀到／寫下的就是現況）。
            last = e
            if i + 1 < IO_TRIES:
                time.sleep(IO_WAIT)
            continue
        try:
            o = json.loads(txt)
        except ValueError as e:
            # 內容壞了（⛔ 不是讀到一半：replace 是整個換掉，實測狂讀寫 4.8 萬次只有 PermissionError、沒有半個檔）
            # ⇒ 照舊一律 1＋1、講得出原因、⛔ 不覆蓋那個檔
            return _fresh(), "狀態檔讀不出來（%s）" % str(e)[:80]
        break
    if o is None:
        good = _LAST_GOOD.get(str(p))
        if good is not None:
            return json.loads(json.dumps(good)), None
        return _fresh(), "狀態檔讀不出來（%s）" % str(last)[:80]
    bad = _valid(o)
    if bad:
        return _fresh(), "狀態檔看不懂：" + bad
    st = _fresh()
    st.update({k: o.get(k, st[k]) for k in DEFAULT})
    st["cap"] = {k: (o.get("cap") or {}).get(k) for k in LANES}
    st["trial"] = _trial_of(o.get("trial"))
    _LAST_GOOD[str(p)] = json.loads(json.dumps(st))
    return st, None


IO_TRIES = 8                # 狀態檔讀碰到「檔案正被換掉」最多試幾次（送單路徑也會讀 ⇒ 最多約 0.1 秒）
W_TRIES = 40                # 寫最多試幾次（寫的人是 poll／HTTP 執行緒，⛔ 不在送單路徑；最多約 0.6 秒）
IO_WAIT = 0.015             # 每次之間等幾秒
_LAST_GOOD = {}             # 路徑 ⇒ 上一次讀成功的那份（⛔ 讀不到時用它，不退回 1＋1）


def _write_state(st, path=None):
    """
    原子寫（tmp → replace）。⛔ 只有 daily_check／apply_upgrade／revoke_pending 會叫（都拿著 _LOCK）。
    ⭐ QA 必修 5：Windows 上 replace 碰到別的執行緒正在讀 ⇒ PermissionError ⇒ 稍等再試；試完還不行才往外丟
    （呼叫端 daily_check 會吞掉、下一輪再來；apply 會回「寫不進去」）。
    """
    p = path or LEVEL_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    for i in range(W_TRIES):
        try:
            tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(p)
            _LAST_GOOD[str(p)] = json.loads(json.dumps(st))
            return
        except PermissionError:
            if i + 1 >= W_TRIES:
                raise
            time.sleep(IO_WAIT)


def _log(kind, payload, now=None):
    now = now or datetime.now()
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (LOG_DIR / ("%d.jsonl" % now.year)).open("a", encoding="utf-8") as f:
            f.write(json.dumps(dict({"at": now.isoformat(timespec="seconds"), "kind": kind}, **payload),
                               ensure_ascii=False) + "\n")
    except Exception:                           # noqa: BLE001  ⛔ 紀錄寫不進去不影響口數
        pass


def read_log(n=50):
    """口數紀錄最近 n 列（新到舊）。⛔ 唯讀；壞列跳過。"""
    out = []
    try:
        files = sorted(LOG_DIR.glob("*.jsonl"), reverse=True)[:2]
    except OSError:
        return out
    for p in files:
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for ln in reversed(lines):
            try:
                o = json.loads(ln)
            except Exception:                   # noqa: BLE001
                continue
            if isinstance(o, dict):
                out.append(o)
            if len(out) >= n:
                return out
    return out


# ══ 口數 ════════════════════════════════════════════════════════════

def ladder_lots(level):
    """第 level 級的 (聯軍 a, 跟勢 b)。⛔ 正本 goal.lots_of；讀不到階梯 ⇒ (1, 1)。"""
    if goal is None or not _is_int(level) or level <= 0:
        return (1, 1)
    try:
        a, b = goal.lots_of(level)
        return (int(a), int(b))
    except Exception:                           # noqa: BLE001
        return (1, 1)


def label(ab):
    return "%d＋%d" % tuple(ab)


def effective_level(st, d=None):
    """
    某一盤（日盤＝那天 d；夜盤＝開盤那晚 E）實際用第幾級。
    ⭐ 已確認、還沒「轉正」的升級（pending）：那一盤的日期 ≥ 生效日 ⇒ 用新的級
       （daily_check 之後才把它寫回 level；這裡先照生效日算，⛔ 不等 14:00）。
    """
    lv = st.get("level") or 0
    p = st.get("pending")
    if p and d is not None:
        try:
            if _d(d) >= _d(p["eff"]):
                return p["to"]
        except (KeyError, ValueError, TypeError):
            pass
    return lv


def _capped(n, cap):
    if cap is None:
        return n
    if cap == 0 and not RETIRE_ZERO:
        cap = 1
    return min(n, cap)


def base_lots(st=None, d=None):
    """⇒ (a, b)：那一盤的級的階梯口數，再套上「那一條降級 ⇒ 最多 1 口」。⛔ 不含夜盤波動規則。"""
    st = st if st is not None else read_state()[0]
    d = d if d is not None else date.today()
    a, b = ladder_lots(effective_level(st, d))
    cap = st.get("cap") or {}
    return _capped(a, cap.get("union")), _capped(b, cap.get("trend"))


def day_lots(st=None, d=None):
    """今天（d）日盤（多方聯軍）送幾口。"""
    return base_lots(st, d)[0]


def cap_lots(st=None, d=None):
    """
    風控 B 上限用的 (a, b) ⇒ 上限 ＝ 400 ×（a＋b）；1＋1 ＝ 800（跟 10-14 以前一樣）。
    ⭐ PM 10-14：用「**本月用過的最大** a＋b」—— ⛔ 不跟著月中減級掉（減級那天上限突然變小 ⇒ 立刻停單到月底）。
       本月最大（`month_max`，daily_check 記）跟「那一盤的級的階梯口數」取 a＋b 大的那一個；換月就重算。
    ⚠️ 用階梯口數（⛔ 不套單條降級的 cap、不套夜盤波動）。`d` ＝ 那一口算哪一天（夜盤＝開盤那晚 E）。
    """
    st = st if st is not None else read_state()[0]
    d = _d(d) if d is not None else date.today()
    ab = ladder_lots(effective_level(st, d))
    mm = st.get("month_max")
    if isinstance(mm, dict) and mm.get("month") == str(d)[:7]:
        m_ab = tuple(int(x) for x in mm.get("ab") or (1, 1))
        if sum(m_ab) > sum(ab):
            return m_ab
    return ab


def margin_lots(st=None, d=None):
    """保證金要算幾口 ＝ max(a, b)（日夜盤部位不重疊）。夜盤波動規則只會讓 b 變少，所以用 b 就夠。"""
    a, b = base_lots(st, d)
    return max(a, b, 1)


# ══ 夜盤波動規則 B ══════════════════════════════════════════════════

def night_vol_forecast(amps):
    """
    ⇒ 預測夜盤波動%（純函式）。`amps` ＝ 今晚**以前**每晚的振幅%（舊到新，⛔ 不含今晚）。
    ＝（前 1 晚 ＋ 前 5 晚平均 ＋ 前 22 晚平均）÷ 3（跟 tick-research/scripts/sizing.py forecast() 同一條式子）。
    不到 22 晚、或最近 22 晚有看不懂的數 ⇒ None（⛔ 不猜）。
    """
    v = list(amps or [])[-NIGHT_VOL_N:]
    if len(v) < NIGHT_VOL_N:
        return None
    try:
        v = [float(x) for x in v]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(x) and x >= 0 for x in v):
        return None
    return (v[-1] + sum(v[-5:]) / 5.0 + sum(v) / float(NIGHT_VOL_N)) / 3.0


def night_qty(b, high):
    """規則 B：偏高 ⇒ b；其他（含算不出來）⇒ max(1, b−1)。b ≤ 0（退休歸零）⇒ 0。"""
    if not _is_int(b) or b <= 0:
        return 0
    if not NIGHT_VOL_ON:
        return b
    return b if high is True else max(1, b - 1)


_AMP = {"key": None, "rows": None, "err": None}
_AMP_LOCK = threading.Lock()


def night_amps(path=None):
    """
    ⇒ ([(那一晚 E 字串, 振幅%)] 舊到新, 錯誤)。⚠️ 讀 tmf_1min.csv（約 40 MB）—— ⛔ 只准在工作執行緒呼叫。
    ⛔ 沿用【健檢】那一把尺：`health._read_csv_chunked`（分批讓出 GIL）＋ `health._sessions`（一晚 ≥200 根才算）。
    快取鍵 ＝ 檔案 (mtime_ns, size)：檔案沒變就不重讀。
    """
    try:
        import health
    except Exception as e:                      # noqa: BLE001
        return [], "讀不到【健檢】模組（%s）" % str(e)[:80]
    p = path or VOL_CSV or health.MIN1_CSV
    try:
        s = p.stat()
        key = (str(p), s.st_mtime_ns, s.st_size)
    except OSError:
        return [], "找不到 %s" % getattr(p, "name", p)
    with _AMP_LOCK:
        if _AMP["key"] == key:
            return list(_AMP["rows"] or []), _AMP["err"]
        try:
            px = health._read_csv_chunked(p, parse_dates=["ts"])
            if px is None or not len(px):
                rows, err = [], "%s 是空的" % p.name
            else:
                g, _gd = health._sessions(px)
                rows = sorted((str(k), float(v)) for k, v in g["amp"].items())
                err = None
        except Exception as e:                  # noqa: BLE001
            rows, err = [], "算不出夜盤振幅（%s）" % str(e)[:80]
        _AMP.update(key=key, rows=rows, err=err)
        return list(rows), err


def cached_amps():
    """已經算好的那一份（⛔ 不讀檔；畫面用）。還沒算過 ⇒ None。"""
    return list(_AMP["rows"]) if _AMP["rows"] is not None else None


def night_vol_of(E, rows):
    """
    今晚 E 的預測波動（純函式）⇒ {"vol", "high", "thr", "last", "n", "why"}。
    `rows` ＝ night_amps() 那一份。⛔ 只用 E **以前**的晚上；最新那一晚離 E 隔了超過 NIGHT_VOL_MAX_GAP_TD 個交易日 ⇒ 不算。
    """
    es = str(E)
    past = [(k, v) for k, v in (rows or []) if k < es]
    out = {"vol": None, "high": None, "thr": NIGHT_VOL_THR, "last": past[-1][0] if past else None,
           "n": len(past), "why": None}
    if not past:
        out["why"] = "沒有今晚以前的夜盤振幅"
        return out
    try:
        gap = trading_gap(past[-1][0], es)
    except ValueError:
        gap = 999
    if gap > NIGHT_VOL_MAX_GAP_TD:
        out["why"] = "夜盤振幅資料太舊（最新一晚 %s，離今晚隔了 %d 個交易日）" % (past[-1][0], gap)
        return out
    f = night_vol_forecast([v for _k, v in past])
    if f is None:
        out["why"] = "今晚以前的夜盤不到 %d 晚，算不出振幅指標" % NIGHT_VOL_N
        return out
    out["vol"] = round(f, 4)
    out["high"] = bool(f >= NIGHT_VOL_THR)
    return out


def night_plan(E, st=None, rows=None, path=None):
    """
    今晚 E 夜盤（夜盤跟勢）送幾口 ⇒ {"qty", "b", "vol", "thr", "high", "last", "level", "rule", "why"}。
    ⭐⭐ 2026-10-14 QA 必修 1：⛔ **送單那一刻絕對不讀檔、不算振幅**（舊版冷快取時讀 40 MB ⇒ 1＋1 也慢 1.6 秒）。
      ・b ≤ 1（1＋1 ⇒ 不管偏不偏高都是 1 口）⇒ ⛔ 連振幅都不看，直接回（跟 10-14 以前一模一樣、一樣快）。
      ・b ≥ 2 ⇒ **只用已經算好的快取**（夜盤判斷前 20 分鐘由背景執行緒先算，見 warm_night）；⛔ 不等。
        規則 B（10-14 核准的那一條）：**偏高才 b 口；不偏高或不知道一律 max(1, b−1)**。
        ⇒ 快取沒有（沒暖到／檔案讀不到）、資料太舊、不夠 22 晚 ⇒ 都是「不知道」⇒ max(1, b−1)。
        （⚠️ 第三輪 dev 曾自行改成「快取沒有 ⇒ b 口」，那不是核准的規則，第四輪改回。）
      ・`rows` 有給（畫面、測試）⇒ 用它；`path` 只給測試導檔用（⛔ 會讀檔，正式送單路徑不傳）。
    """
    st = st if st is not None else read_state()[0]
    b = base_lots(st, E)[1]
    out = {"b": b, "level": effective_level(st, E), "rule": "B" if NIGHT_VOL_ON else "off",
           "vol": None, "thr": NIGHT_VOL_THR, "high": None, "last": None, "why": None}
    if not NIGHT_VOL_ON or b <= 1:
        if NIGHT_VOL_ON and b == 1:
            out["why"] = "1 口：不用看振幅"
        out["qty"] = night_qty(b, None) if NIGHT_VOL_ON else b
        return out
    err = None
    if rows is None:
        if path is not None:
            rows, err = night_amps(path)
        else:
            rows, err = cached_amps(), _AMP["err"]
    if rows is None or (err and not rows):
        out["why"] = "振幅指標還沒算好（%s）" % (err or "背景還沒讀完")
        out["qty"] = night_qty(b, None)            # 不知道 ⇒ 規則 B：max(1, b−1)
        return out
    nv = night_vol_of(E, rows)
    out.update(vol=nv["vol"], high=nv["high"], last=nv["last"], why=err or nv["why"])
    out["qty"] = night_qty(b, out["high"])
    return out


_WARM = {"th": None}


def warm_night(E=None):
    """
    夜盤判斷前先把振幅讀好。⭐ 在**自己的背景執行緒**讀（約 1.6 秒）—— ⛔ 不擋夜盤工作執行緒
    （他 21:39 才開開關、warm 跟判斷擠在同一輪的話，送單會被拖慢；QA 必修 1）。
    ⛔ 永遠不往外丟例外。已經在讀 ⇒ 不再開一條。⇒ 有沒有開出新的一條。
    """
    try:
        th = _WARM["th"]
        if th is not None and th.is_alive():
            return False

        def run():
            try:
                night_amps()
            except Exception:                   # noqa: BLE001
                pass
        th = threading.Thread(target=run, name="lots-warm-night", daemon=True)
        _WARM["th"] = th
        th.start()
        return True
    except Exception:                           # noqa: BLE001
        return False


# ══ 升級要看的東西（⛔ 全部沿用既有算法）══════════════════════════════

def _month_files(d):
    try:
        return sorted(p for p in d.glob("*.jsonl") if len(p.stem) == 7 and p.stem[4] == "-")
    except OSError:
        return []


# ⭐⭐ 「下單錯誤」（升級條件用）＝ **真的送出去之後失敗**（PM 2026-10-14 拍板）。
#    ⛔ 跟週報 analyst_facts.day_err／night_err **刻意不同**（那把尺不改，週報照舊）：
#       週報把「09:03:30 收不到成交價」「太晚」「券商那一關擋下（還沒送）」「休市」這類**根本沒送**的也算成問題，
#       那是在講系統健不健康；這裡講的是「多口時送出去會不會出事」⇒ 只算這幾種：
#         日盤：result 列 ok=False、why=order_failed（券商拒絕／送單例外／IOC 一口都沒撮到／方向不符）
#               eod 列 why=eod_failed（收盤平倉單送出去了但沒平掉）
#         夜盤：result 列 ok=False（送單失敗）；eod 列 why=failed（04:58 平倉單送了但沒平掉）
#       ⛔ 不算：cant_enter（券商那一關擋下＝沒送）、late／no_quote／quote_stale／not_wired（沒送）、
#          unsure／crashed（不知道下場 —— 那是另一種問題，週報照樣會列）。
DAY_SENT_FAIL = {"order_failed"}


def day_sent_fail(o):
    """日盤帳本一列 ⇒ 「送出去之後失敗」的那句話；不是 ⇒ None。"""
    if o.get("rec") == "result" and not o.get("ok") and o.get("why") in DAY_SENT_FAIL:
        return "日盤：" + str(o.get("why_msg") or o.get("why"))[:60]
    if o.get("rec") == "eod" and o.get("why") == "eod_failed":
        return "日盤收盤平倉：" + str(o.get("why_msg") or "")[:60]
    return None


def night_sent_fail(o):
    """夜盤帳本一列 ⇒ 「送出去之後失敗」的那句話；不是 ⇒ None。"""
    if o.get("rec") == "result" and not o.get("ok"):
        return "夜盤：" + str(o.get("err") or o.get("msg") or "送單失敗")[:60]
    if o.get("rec") == "eod" and o.get("why") == "failed":
        return "夜盤 04:58 平倉：" + str(o.get("msg") or "")[:60]
    return None


def exec_quality(n=RECENT_N):
    """
    最近 n 筆自動**真單**（日盤＋夜盤**合計**的 `rec:"result"`；⛔ 演練 live=False 不算）的執行品質 ⇒ {"n", "need", "slip_avg", "errors", "ok", "why"}。
    「滑價」用 analyst_facts 週報那一把尺（`day_slip／night_slip`）；「下單錯誤」用上面那一層過濾
    （只算送出去之後失敗，見 DAY_SENT_FAIL 那段）。窗口裡（第一筆那天起）的任何一筆都算。
    ⭐ PM 10-14：**不滿 n 筆就用現有的筆數算**（`n` 端出實際筆數）；一筆都沒有 ⇒ 算不出滑價 ⇒ 不過。
    """
    import analyst_facts as AF
    import risk_cap
    af_dir, nf_dir, _t = risk_cap._dirs()
    sent, errs = [], []
    for p in _month_files(af_dir):
        for o in risk_cap._jsonl(p):
            if o.get("live") is False:
                continue                                    # ⭐ QA 10-14：演練的單不算（不是真的送到券商）
            d = str(o.get("date") or "")
            key = (d, str(o.get("entry_time") or o.get("wrote_at") or ""))
            if o.get("rec") == "result":
                sent.append((key, AF.day_slip(o)))
            e = day_sent_fail(o)
            if e:
                errs.append((key, e))
    for p in _month_files(nf_dir):
        for o in risk_cap._jsonl(p):
            if o.get("live") is False:
                continue
            d = str(o.get("E") or "")
            key = (d, "N" + str(o.get("at") or ""))          # 同一天：夜盤排在日盤後面
            if o.get("rec") == "result":
                sent.append((key, AF.night_slip(o)))
            e = night_sent_fail(o)
            if e:
                errs.append((key, e))
    sent.sort(key=lambda x: x[0])
    win = sent[-n:]
    out = {"n": len(win), "need": n, "slip_avg": None, "slip_n": 0, "errors": [], "ok": False,
           "why": None, "since": None}
    if not win:
        out["why"] = "還沒有任何自動單，算不出滑價"
        return out
    since = win[0][0][0]
    out["since"] = since
    out["errors"] = [m for (k, m) in errs if k[0] >= since]
    slips = [s for _k, s in win if s is not None]
    out["slip_n"] = len(slips)
    out["slip_avg"] = round(sum(slips) / len(slips), 2) if slips else None
    head = ("最近 %d 筆" % n) if len(win) >= n else ("目前 %d 筆（還不到 %d 筆）" % (len(win), n))
    if out["errors"]:
        out["why"] = "%s期間有 %d 次下單錯誤" % (head, len(out["errors"]))
    elif out["slip_avg"] is None:
        out["why"] = "%s算不出滑價" % head
    elif out["slip_avg"] > SLIP_MAX:
        out["why"] = "%s平均滑價 %.1f 點，超過 %g 點" % (head, out["slip_avg"], SLIP_MAX)
    else:
        out["ok"] = True
    return out


def retire_status():
    """兩條真單在退休標準的哪一級 ⇒ ({lane: level}, err)。⛔ 沿用 analyst_facts.retire()（週報那一份）。"""
    try:
        import analyst_facts as AF
        mk = AF.market()
        rr = AF.retire(AF._sim_rows(), ((mk.get("regime") or {}).get("level")))
        return {r["key"]: r.get("level") for r in rr}, None
    except Exception as e:                      # noqa: BLE001
        return {}, "算不出退休標準（%s）" % str(e)[:80]


def incidents():
    """
    停損失靈事故 ⇒ [{"d", "sess", "pts", "sl"}]：見 INCIDENT_MULT 上面那段（停損沒被執行，或每口賠超過停損 × 1.5）。
    ⛔ 認人沿用 risk_cap（帳本 × 成績單：進場時間＋進場價）；停損點數讀帳本那一列的 sl_points。
    """
    import risk_cap
    af_dir, nf_dir, trade_dir = risk_cap._dirs()
    months = sorted({p.stem for p in _month_files(af_dir) + _month_files(nf_dir)})
    out = []
    for m in months:
        ents = risk_cap.auto_entries(m)
        y, mm = int(m[:4]), int(m[5:])
        d0 = date(y, mm, 1)
        d1 = date(y + (mm == 12), mm % 12 + 1, 1) + timedelta(days=risk_cap.NIGHT_EXIT_MAX_D)
        trades = risk_cap._trades_between(d0, d1, trade_dir)
        used = set()
        for e in ents:
            t = risk_cap._match(e, trades, used)
            sl = e.get("sl_points")
            if t is None or not risk_cap._num(t.get("points")) or not risk_cap._num(sl):
                continue
            loss, slp = -float(t["points"]), float(sl)
            not_exec = str(t.get("reason") or "") not in INCIDENT_EXEC and loss > slp
            if not_exec or loss > slp * INCIDENT_MULT:
                out.append({"d": e["d"], "sess": e["sess"], "pts": float(t["points"]), "sl": float(sl)})
    return out


def _latest_eq(rows):
    for r in reversed(list(rows or [])):
        v = r.get("equity") if isinstance(r, dict) else None
        if _num(v):
            return str(r.get("date")), float(v)
    return None, None


def _gather(ctx):
    """升級條件要的三樣東西 ⇒ (quality, retire, retire_err, incidents)。`ctx` 有給（測試）就用它的。"""
    ctx = ctx or {}
    if "retire" in ctx:
        ret, rerr = ctx.get("retire") or {}, ctx.get("retire_err")
    else:
        ret, rerr = retire_status()
    incs = ctx["incidents"] if "incidents" in ctx else _safe(incidents, None)   # None＝算不出來 ⇒ 暫停不動
    q = ctx["quality"] if "quality" in ctx else _safe(exec_quality, {"ok": False, "why": "算不出來"})
    return q, ret, rerr, incs


# ══ 減級（純函式）════════════════════════════════════════════════════

def downgrade(st, eq, retire=None, incs=None, today=None):
    """
    ⇒ (新狀態, [事件])。純函式：⛔ 不讀檔、不寫檔。
    ① 權益 < 目前這一級門檻 × DOWN_FRAC ⇒ 退到 goal.level_of(權益)（一定比現在低）；還沒生效的升級一併作廢。
    ② 退休標準：降級 ⇒ 那條 cap 1；退休 ⇒ cap 0（RETIRE_ZERO=False 時 base_lots 照樣只壓到 1）。⛔ 只會收緊。
    ③ 事故 ⇒ pause_until ＝ 事故那天 + 6 個月（取比較晚的那一個）；還沒生效的升級一併作廢。
    """
    st = json.loads(json.dumps(st))
    ev = []
    today = _d(today or date.today())
    lv = st.get("level") or 0
    lad = _ladder()
    if lv >= 1 and _num(eq) and lad:
        thr = lad[lv - 1][0]
        if eq < thr * DOWN_FRAC:
            nl = goal.level_of(eq) if goal is not None else 0
            nl = min(nl, lv - 1)
            old, new = ladder_lots(lv), ladder_lots(nl)
            # 第 1 級 → 第 0 級（都是 1＋1）不算一次減級（不提示），只記一列 free
            ev.append({"kind": "down_equity" if old != new else "free", "from": lv, "to": nl, "eq": eq, "thr": thr,
                       "line": thr * DOWN_FRAC, "from_ab": list(old), "to_ab": list(new),
                       "lots_changed": old != new,
                       # ⭐ QA 10-14：白話、照規格那一句（「權益 8.31 萬，低於這一級的保護線 8.5 萬 ⇒ 明天起退回 1＋1。」）
                       "msg": "權益 %s，低於這一級的保護線 %s ⇒ 明天起退回 %s。" % (
                           wan2(eq), wan2(thr * DOWN_FRAC), label(new))})
            st["level"] = nl
            st["since"] = str(today)
            if st.get("pending"):
                ev.append({"kind": "pending_void", "msg": "減級了 ⇒ 已確認還沒生效的升級作廢"})
                st["pending"] = None
    for lane in LANES:
        lvl = (retire or {}).get(lane)
        want = 1 if lvl == "down" else (0 if lvl == "retire" else None)
        if want is None:
            continue
        cur = (st.get("cap") or {}).get(lane)
        if cur is None or want < cur:
            st.setdefault("cap", {})[lane] = want
            ev.append({"kind": "cap", "lane": lane, "status": lvl, "cap": want,
                       "msg": ("%s 進入退休標準的「降級」⇒ 自動壓到 1 口。" % LANE_NAME[lane]) if lvl == "down"
                       else ("%s 已退休：自動壓到 1 口，要不要關掉由你按。" % LANE_NAME[lane]
                             if not RETIRE_ZERO else "%s 已退休 ⇒ 0 口（不送）。" % LANE_NAME[lane])})
    if incs is not None:
        want = None
        for inc in incs:
            try:
                u = str(_add_months(date.fromisoformat(inc["d"]), PAUSE_MONTHS))
            except (KeyError, ValueError):
                continue
            want = u if (want is None or u > want) else want
        old = st.get("pause_until")
        # ⭐ 2026-10-19：判法改了之後，以前誤判的暫停要能解除（⛔ 只在「算得出事故清單」時才動）
        if old and (want is None or want < old):
            st["pause_until"] = want
            ev.append({"kind": "pause_lifted", "from": old, "to": want,
                       "msg": "停損失靈的判法更正（急跌滑價不算失靈）⇒ 原本暫停升級到 %s 已解除%s。" % (
                           old, ("，改成 %s" % want) if want else "")})
    for inc in incs or []:
        try:
            until = str(_add_months(date.fromisoformat(inc["d"]), PAUSE_MONTHS))
        except (KeyError, ValueError):
            continue
        if st.get("pause_until") is None or until > st["pause_until"]:
            st["pause_until"] = until
            ev.append({"kind": "pause", "until": until, "inc": inc,
                       "msg": "%s %s 停損失靈：每口賠 %.0f 點（停損 %.0f）⇒ 暫停升級到 %s。" % (
                           inc["d"], "夜盤" if inc.get("sess") == "night" else "日盤",
                           -inc["pts"], inc["sl"], until)})
            if st.get("pending") and str(today) < str(st["pending"].get("eff")):
                ev.append({"kind": "pending_void", "msg": "停損失靈事故 ⇒ 已確認還沒生效的升級作廢"})
                st["pending"] = None
    return st, ev


# ══ 升級（2026-10-14 老闆新規則：先試車 2＋1，過了就跳到權益撐得起的那一級）══════════════
#
# ⭐ 取代 09-30 的「每月第一個交易日、一次一級、升完冷卻 3 個月」（10-14 起作廢）：
#   ① 每個交易日收盤後都檢查（daily_check；equity/ 每多一列跑一次）。
#   ② 試車：在 1＋1、權益 ≥ 2＋1 那一級門檻（10 萬）、其他條件都過 ⇒ 建議「2＋1（試車）」；兩段式確認、確認後立刻生效（10-21）。
#   ③ 試車通過：試車生效那天起，**實際成交 ≥ 2 口**的自動真單（日盤或夜盤）有 TRIAL_N（1）筆、**零下單錯誤**、
#      滑價 ≤ 4 點（⭐ 老闆 2026-10-16：5 筆改 2 筆；2026-10-21：再改 1 筆）。只成交 1 口、滑價 > 4 ⇒ 不算、等下一筆。試車期間任何一次下單錯誤 ⇒ 筆數從 0 重算。通過之後**記在狀態檔**（`trial.status="passed"`）。
#   ④ 通過之後：權益撐得到哪一級就直接建議跳到哪一級（可以跳級，照積極版階梯）；兩段式確認、確認後立刻生效（10-21）。
#      試車通過的同一次每日檢查就出建議。
#   ⑤ 之後再升：權益跨過更高一級時可以再建議，但離**上一次升級生效**至少 1 個日曆月（11/20 生效 ⇒ 12/20 起）。
#      ⚠️ 試車通過後的**第一次跳級不受這一條限制**（試車本身就是上一次升級；否則試車跑得快也要乾等一個月）。
#   ⑥ 減級照舊（自動）。已經試車通過的人減級之後**不必再試車**；試車還沒過就被退回 1＋1 ⇒ 這一次試車作廢、之後重來。
#   ⑦ 已確認、還沒生效的升級：有一條策略掉到觀察／降級（或算不出來）⇒ **作廢**（畫面講為什麼）。
#      每天第一次跑（含生效那天開盤前）都會看一次；收盤那次也會看。
# 其他條件（每一次升級都要）：最近 30 筆自動真單沒有下單錯誤、平均滑價 ≤ 4／兩條策略都「正常」／沒有停損失靈暫停。

TRIAL_TO = 2                # 試車那一級（2＋1）
TRIAL_N = 1                 # 試車要幾筆「實際成交 ≥ 2 口」的自動真單（⭐ 老闆 10-16：5 → 2；10-21：2 → 1）


def _tslip():
    """試車那幾句的「滑價」用字：只看 1 筆 ⇒「滑價」；多筆 ⇒「平均滑價」（跟著 TRIAL_N，⛔ 不寫死）。"""
    return "滑價" if TRIAL_N == 1 else "平均滑價"
REUP_GAP_MONTHS = 1         # 再升：離上一次升級生效至少幾個日曆月
PENDING_GUARD_HOUR = 6      # 已確認的升級每天幾點以後重看一次策略（夜盤 05:00 收完、05:30 備份之後）


def _trial(st):
    return _trial_of((st or {}).get("trial"))


def _ab_of(o):
    q = o.get("qty")
    return q if (_is_int(q) and q >= 1) else 1


def trial_events(start):
    """
    試車生效那天（含）起的自動**真單**（⛔ 演練不算）⇒ [((日期, 時間), "trade"／"err", 滑價或那句話)]，依時間排。
    "trade" ＝ 帳本 `rec:"result"` ok、**實際成交 ≥ 2 口**；"err" ＝ 送出去之後失敗（day_sent_fail／night_sent_fail 那一把尺）。
    """
    import analyst_facts as AF
    import risk_cap
    af_dir, nf_dir, _t = risk_cap._dirs()
    s = str(start)[:10]
    out = []
    for p in _month_files(af_dir):
        if p.stem < s[:7]:
            continue
        for o in risk_cap._jsonl(p):
            d = str(o.get("date") or "")
            if d < s or o.get("live") is False:
                continue
            key = (d, str(o.get("entry_time") or o.get("wrote_at") or ""))
            e = day_sent_fail(o)
            if e:
                out.append((key, "err", e))
            elif o.get("rec") == "result" and o.get("ok") and _ab_of(o) >= 2:
                out.append((key, "trade", AF.day_slip(o)))
    for p in _month_files(nf_dir):
        if p.stem < s[:7]:
            continue
        for o in risk_cap._jsonl(p):
            d = str(o.get("E") or "")
            if d < s or o.get("live") is False:
                continue
            key = (d, "N" + str(o.get("at") or ""))
            e = night_sent_fail(o)
            if e:
                out.append((key, "err", e))
            elif o.get("rec") == "result" and o.get("ok") and _ab_of(o) >= 2:
                out.append((key, "trade", AF.night_slip(o)))
    out.sort(key=lambda x: x[0])
    return out


def trial_eval(events):
    """
    純函式：試車那幾筆 ⇒ {"n", "total", "slip", "slip_n", "reset", "passed", "why"}。
    一次下單錯誤 ⇒ 從 0 重算。看「最近 TRIAL_N 筆」：滿 TRIAL_N 筆、這幾筆的滑價都算得出來、平均 ≤ SLIP_MAX ⇒ 通過。
    （平均超過 4 點 ⇒ 不過，繼續看下一筆 —— 滾動看最近 TRIAL_N 筆，⛔ 不是永遠卡死。）
    """
    win, reset = [], None
    for key, kind, val in events or []:
        if kind == "err":
            win, reset = [], key[0]
        else:
            win.append(val)
    last = win[-TRIAL_N:]
    slips = [float(x) for x in last if _num(x)]
    avg = round(sum(slips) / len(slips), 2) if slips else None
    passed = len(last) >= TRIAL_N and len(slips) == TRIAL_N and avg <= SLIP_MAX
    why = None
    if not passed:
        if len(last) < TRIAL_N:
            why = "還差 %d 筆" % (TRIAL_N - len(last))
        elif len(slips) < TRIAL_N:
            why = "最近 %d 筆有 %d 筆算不出滑價" % (TRIAL_N, TRIAL_N - len(slips))
        else:
            why = "最近 %d 筆%s %.1f 點，超過 %g 點" % (TRIAL_N, _tslip(), avg, SLIP_MAX)
    # ⭐ QA 10-21：畫面的筆數只算「會計入通過」的 —— 窗口滿了卻沒過（滑價太大／算不出來）⇒ 最新那幾筆不算，
    #    最多顯示 TRIAL_N−1（1 筆制 ⇒ 0／1），⛔ 不可以寫成「1／1 筆」讓人以為跑完了。
    n_ok = TRIAL_N if passed else min(len(win), TRIAL_N - 1 if len(last) >= TRIAL_N else TRIAL_N)
    return {"n": n_ok, "total": len(win), "slip": avg, "slip_n": len(slips),
            "reset": reset, "passed": passed, "why": why, "miss": why if len(last) >= TRIAL_N and not passed else None}


def _trial_miss(tr):
    """
    ⭐ QA 10-21：試車中、最近那幾筆沒算進去（滑價超過／算不出來）⇒ 給人看的一句。沒有 ⇒ None。
    1 筆制：「上一筆滑價 9.0 點超過 4 點，不算，等下一筆。」
    """
    if not isinstance(tr, dict) or tr.get("status") != "on":
        return None
    sl = tr.get("slip")
    if _num(sl) and sl > SLIP_MAX:
        if TRIAL_N == 1:
            return "上一筆滑價 %.1f 點超過 %g 點，不算，等下一筆。" % (sl, SLIP_MAX)
        return "最近 %d 筆平均滑價 %.1f 點超過 %g 點，還不算，等下一筆。" % (TRIAL_N, sl, SLIP_MAX)
    if tr.get("miss") and "算不出滑價" in str(tr["miss"]):
        return ("上一筆算不出滑價，不算，等下一筆。" if TRIAL_N == 1
                else "最近 %d 筆有算不出滑價的，還不算，等下一筆。" % TRIAL_N)
    return None


def _target(st, eq):
    """
    這一次要看的是升到哪一級 ⇒ (to, kind)。kind：
      "trial"    ＝ 還沒試車、在 1＋1 ⇒ 試車那一級（2＋1）
      "jump"     ＝ 試車過了 ⇒ 權益撐得起的那一級（goal.level_of；不夠就給「下一級」讓畫面講還差多少）
      "trialing" ＝ 試車中 ⇒ 不出建議（to＝None）
      None       ＝ 已經最高一級
    """
    lv = st.get("level") or 0
    lad = _ladder()
    if lv >= len(lad):
        return None, None
    tr = _trial(st)
    if tr["status"] == "passed":
        want = min(goal.level_of(eq), len(lad)) if (goal is not None and _num(eq)) else 0
        if want > lv and ladder_lots(want) != ladder_lots(lv):
            return want, "jump"
        return _next_of(lv), "jump"
    if tr["status"] == "on" or ladder_lots(lv) != (1, 1):
        # 試車中（還沒試、卻已經不在 1＋1 ⇒ 照理不會有；daily_check 會把它標成試車中）
        return None, "trialing"
    return TRIAL_TO, "trial"


def upgrade_check(st, eq, eq_date, today, quality, retire, retire_err=None, to=None):
    """
    ⇒ {"ok", "id", "kind", "from", "to", "lots", "conds": [...], "why": [...]}（純函式）。⛔ 不看最近賺不賺。
    `to`（apply 用）：照建議那一級驗（權益後來又漲過更高一級也不會變成「級數對不上」）。
    `conds` 給畫面（順序：權益／下單錯誤／平均滑價／策略狀態／試車／上次升級／事故）。
    ⚠️ 營運那幾條（自動登入、他會手動平倉、升 5＋2 前談停電備援）程式查不到 ⇒ 不在這裡判，畫面確認時要他自己看。
    """
    today = _d(today)
    lv = st.get("level") or 0
    lad = _ladder()
    tr = _trial(st)
    tgt, kind = _target(st, eq)
    if to is not None and kind in ("trial", "jump"):
        tgt = to
    res = {"ok": False, "id": None, "kind": kind, "to": tgt, "lots": None, "from": lv, "eq": eq,
           "eq_date": eq_date, "why": [], "conds": [], "made": str(today), "until": None}
    if kind is None:
        res["why"].append("已經是最高一級")
        return res
    q = quality or {}
    conds = []
    if kind == "trialing":
        conds.append({"k": "trial", "name": "試車", "ok": False, "status": "on", "n": tr.get("n"), "need": TRIAL_N,
                      "slip": tr.get("slip"), "start": tr.get("start"), "reset": tr.get("reset")})
        res["conds"] = conds
        res["why"] = ["試車中（%d／%d 筆）：有 %d 筆 2 口以上的真單才看下一步" % (tr.get("n") or 0, TRIAL_N, TRIAL_N)]
        return res
    if not _is_int(tgt) or not (lv < tgt <= len(lad)):
        res["why"].append("要升的那一級不合理（%r）" % (tgt,))
        return res
    thr = lad[tgt - 1][0]
    res.update(lots=label(ladder_lots(tgt)), thr=thr, id="%s:%d>%d" % (today, lv, tgt))
    ok_eq = _num(eq) and eq >= thr
    conds.append({"k": "eq", "name": "權益", "ok": bool(ok_eq), "eq": eq, "need": thr,
                  "gap": (max(0, thr - eq) if _num(eq) else None)})
    errs = list(q.get("errors") or [])
    n_err = q.get("n_err") if _is_int(q.get("n_err")) else len(errs)
    conds.append({"k": "err", "name": "下單錯誤", "ok": (q.get("n") or 0) > 0 and not n_err,
                  "n": q.get("n"), "need_n": q.get("need", RECENT_N), "errors": n_err})
    conds.append({"k": "slip", "name": "平均滑價", "ok": bool(q.get("ok")) or bool(
        _num(q.get("slip_avg")) and q["slip_avg"] <= SLIP_MAX and not n_err),
                  "avg": q.get("slip_avg"), "max": SLIP_MAX, "n": q.get("n")})
    states = {lane: (retire or {}).get(lane) for lane in LANES}
    conds.append({"k": "strat", "name": "策略狀態", "ok": (not retire_err) and all(v == "ok" for v in states.values()),
                  "states": states, "err": retire_err})
    if kind == "jump":
        conds.append({"k": "trial", "name": "試車", "ok": True, "status": "passed", "passed_at": tr.get("passed_at"),
                      "need": TRIAL_N})
    # 上次升級：試車那一次、試車後第一次跳級 ⇒ 不用等；之後 ⇒ 離上一次升級生效至少 1 個日曆月
    cool_ok, free, mode = True, None, ("trial" if kind == "trial" else ("first_jump" if not tr.get("jumped") else "gap"))
    if mode == "gap" and st.get("up_at"):
        try:
            free = _add_months(date.fromisoformat(st["up_at"]), REUP_GAP_MONTHS)
            cool_ok = today >= free
        except ValueError:
            cool_ok = False
    conds.append({"k": "cool", "name": "上次升級", "ok": cool_ok, "up_at": st.get("up_at"), "mode": mode,
                  "free": (str(free) if free else None)})
    pu = st.get("pause_until")
    conds.append({"k": "pause", "name": "停損失靈事故", "ok": not (pu and str(today) < pu), "until": pu})
    why = []
    if not _num(eq):
        why.append("不知道權益（equity/ 沒有紀錄）")
    elif not ok_eq:
        why.append("權益 %s 還沒到 %s 那一級的門檻 %s" % (format(int(eq), ","), res["lots"], format(int(thr), ",")))
    if not conds[1]["ok"] or not conds[2]["ok"]:
        why.append("執行品質沒過：%s" % (q.get("why") or "算不出來"))
    if retire_err:
        why.append(retire_err)
    for lane in LANES:
        if states[lane] != "ok":
            why.append("%s 在退休標準不是「正常」（%s）" % (LANE_NAME[lane], states[lane] or "算不出來"))
    if not cool_ok:
        why.append("%s 才升過，%s 起才能再升" % (st.get("up_at"), free))
    if pu and str(today) < pu:
        why.append("停損失靈事故後暫停升級到 %s" % pu)
    res["why"] = why
    res["conds"] = conds
    res["ok"] = not why
    return res


def _strat_bad(retire, retire_err):
    """已確認的升級要不要作廢 ⇒ 一句原因或 None（⛔ 算不出來也算：不知道 ≠ 正常）。"""
    if retire_err:
        return "算不出策略狀態（%s）" % retire_err
    bad = ["%s 掉到「%s」" % (LANE_NAME[l], _STRAT_WORD.get((retire or {}).get(l), (retire or {}).get(l)))
           for l in LANES if (retire or {}).get(l) != "ok"]
    return "、".join(bad) or None


def _void_pending(st, why, today):
    p = st.get("pending") or {}
    st["pending"] = None
    st["void"] = {"at": str(today), "to": p.get("to"), "eff": p.get("eff"), "why": why}
    if st.get("suggest"):
        st["suggest"] = dict(st["suggest"], confirmed=None, withdrawn=str(today), why=[why])
    to_ab = ladder_lots(p.get("to")) if _is_int(p.get("to")) else None
    return {"kind": "pending_void", "to": p.get("to"), "to_ab": (list(to_ab) if to_ab else None),
            "msg": "已確認的升級（%s）作廢：%s。維持現在的口數，條件回來之後會再出建議。" % (
                label(to_ab) if to_ab else "?", why)}


def _q_small(q):
    """執行品質存進狀態檔的那一份（apply 重驗用；⛔ 不存整串錯誤明細）。"""
    q = q or {}
    return {"n": q.get("n"), "need": q.get("need"), "slip_avg": q.get("slip_avg"), "slip_n": q.get("slip_n"),
            "n_err": len(q.get("errors") or []), "ok": bool(q.get("ok")), "why": q.get("why"), "since": q.get("since")}


def _cached_ctx(st):
    """
    ⭐ QA 建議（4Hz 變慢）：apply 重驗**用每日檢查算好存下來的那一份**（品質、策略狀態）⇒ 不在 HTTP 執行緒上
    重算退休標準（約 2.5 秒 CPU，跟主迴圈搶 GIL）。沒有存過 ⇒ None（照舊現算）。
    """
    c = (st or {}).get("conds") or {}
    if isinstance(c.get("q"), dict) and isinstance(c.get("states"), dict):
        return {"quality": c["q"], "retire": c["states"], "retire_err": c.get("rerr"), "incidents": []}
    return None


def _apply_precheck(st, offer_id, today):
    """apply 的「建議本身對不對」那幾道 ⇒ None（都過）或一句原因。⛔ 純函式。"""
    sug = st.get("suggest") or {}
    if not isinstance(offer_id, str) or not offer_id:
        return "要確認的建議看不懂（%r）" % (offer_id,)
    if not sug:
        return "現在沒有升級建議"
    if st.get("pending"):
        return "已經確認過了（%s 起生效）—— 不會再升一次" % st["pending"].get("eff")
    if sug.get("id") != offer_id:
        return "只能確認現在建議的那一級（%s），不能確認 %s" % (sug.get("id"), offer_id)
    if sug.get("withdrawn"):
        return "這個建議 %s 已經收回：%s" % (sug["withdrawn"], "；".join(sug.get("why") or []))
    if not sug.get("ok"):
        return "現在的建議是「不升」：" + "；".join(sug.get("why") or [])
    lv = st.get("level") or 0
    if sug.get("from") != lv or not _is_int(sug.get("to")) or sug["to"] <= lv:
        return "建議之後級數變過（現在第 %s 級）—— 這個建議作廢" % lv
    if sug.get("kind") == "trial" and _trial(st)["status"] != "none":
        return "試車已經開始過了 —— 這個建議作廢"
    if sug.get("kind") == "jump" and _trial(st)["status"] != "passed":
        return "試車還沒通過 —— 不能跳級"
    if str(sug.get("made") or "") > str(today):
        return "建議的日期不對（%s）" % sug.get("made")
    return None


def apply_upgrade(offer_id, now=None, rows=None, ctx=None, path=None):
    """
    ⭐ 畫面「兩段式確認」第二段之後呼叫的那一支（POST /api/lots/upgrade；撤回是 /api/lots/revoke）。⇒ (成功嗎, 一句話)。
    ⛔ 保險：
      ・只接受**現在建議的那一級**：`offer_id` 要等於狀態檔 `suggest.id`（「建議那天:從>到」）——
        建議以外的級、已經收回／不升的建議、建議之後級數變過、試車沒過就跳級 ⇒ 拒絕＋原因。
      ・已經確認過（有 pending）⇒ 拒絕（連按兩下不會升兩次）。
      ・**當下重驗一次全部條件**：權益用 `rows` 最新那一列；品質／策略狀態用每日檢查存下來的那一份
        （⭐ QA 建議：⛔ 不在這裡重算退休標準，那會讓主迴圈變慢；策略之後掉了由 _pending_guard 作廢）。
    ⭐⭐ 2026-10-21 老闆拍板：**確認後立刻生效**（取代「下一個交易日」與「生效前撤回」）：
       成功當下就把新的級寫進狀態檔（level／since／up_at＝今天、單條降級上限解除、本月最大 a＋b 跟著更新）。
       送單那一刻才讀口數（auto_fire._send／night_fire._decide 問 lots）⇒ 只影響**之後的新單**：
       今晚夜盤起、下一筆日盤起；⛔ 已經送出的單、手上的部位照實際口數處理（broker 用部位自己的 entry_qty，不追加、不平）。
       夜盤 b ≥ 2 ⇒ 順手在背景把振幅讀好（warm_night），21:40 之前確認也來得及用規則 B。
    """
    now = now or datetime.now()
    today = now.date()
    st0, err = read_state(path)
    if err:
        return False, "口數狀態檔看不懂，不升（%s）" % err
    bad = _apply_precheck(st0, offer_id, today)
    if bad:
        return False, bad
    used_cache = False
    if ctx is None:
        ctx = _cached_ctx(st0)
        used_cache = ctx is not None
    q, ret, rerr, _incs = _gather(ctx)          # ⛔ 鎖外面（有存好的那一份 ⇒ 不算任何東西）
    if rows is not None:
        eq_d, eq = _latest_eq(rows)
    else:
        eq_d, eq = (st0.get("suggest") or {}).get("eq_date"), (st0.get("suggest") or {}).get("eq")
    with _LOCK:
        st, err = read_state(path)
        if err:
            return False, "口數狀態檔看不懂，不升（%s）" % err
        bad = _apply_precheck(st, offer_id, today)
        if bad:
            return False, bad
        sug = st.get("suggest") or {}
        chk = upgrade_check(st, eq, eq_d, today, q, ret, rerr, to=sug["to"])
        if not chk["ok"] or chk["to"] != sug["to"] or chk["kind"] != sug.get("kind"):
            return False, "條件現在沒有全部通過，不升：" + "；".join(chk["why"] or ["級數對不上"])
        eff = today
        tr = _trial(st)
        st["level"] = sug["to"]
        st["since"] = str(today)
        if sug["to"] >= 2:
            st["up_at"] = str(today)                  # ⭐ 「隔一個月」從**生效那天**（＝確認那天）算
        st["cap"] = {k: None for k in LANES}          # 升級要兩條都正常 ⇒ 單條降級的上限一併解除
        st["pending"] = None
        if tr["status"] == "passed":
            tr["jumped"] = True
        elif ladder_lots(sug["to"]) != (1, 1):
            tr = dict(TRIAL_DEFAULT, status="on", start=str(today))
        st["trial"] = tr
        st["month_max"] = _bump_month_max(st, today)  # 風控 B 上限：本月最大 a＋b 跟著更新（400 × 本月最大）
        st["suggest"] = dict(sug, confirmed=now.isoformat(timespec="seconds"))
        st["void"] = None
        try:
            _write_state(st, path)
        except OSError as e:
            return False, "口數狀態檔寫不進去（%s）—— 沒有升，請再按一次" % str(e)[:60]
    before, after = ladder_lots(sug["from"]), ladder_lots(sug["to"])
    msg = "已確認：現在起改成 %s%s（原本 %s）—— 今晚夜盤起／下一筆日盤起照新口數；手上已經有的部位照原本口數處理" % (
        label(after), "（試車）" if sug.get("kind") == "trial" else "", label(before))
    _log("upgrade_confirmed", {"from": sug["from"], "to": sug["to"], "eff": str(eff), "kind": sug.get("kind"),
                               "from_ab": list(before), "to_ab": list(after), "by": "you", "msg": msg}, now)
    _log("upgrade_effective", {"to": sug["to"], "to_ab": list(after), "by": "you",
                               "msg": "%s 起改成 %s（確認後立刻生效）" % (eff, label(after))}, now)
    if after[1] >= 2:
        warm_night()                                  # 今晚 21:40 用得到振幅（⛔ 背景執行緒，不擋這裡）
    return True, msg


def revoke_pending(now=None, path=None):
    """撤回「已確認、還沒生效」的升級 ⇒ (成功嗎, 一句話)。⛔ 安全方向：一鍵、不再確認。生效之後就撤不了。"""
    now = now or datetime.now()
    with _LOCK:
        st, err = read_state(path)
        if err:
            return False, "口數狀態檔看不懂（%s）" % err
        p = st.get("pending")
        if not p:
            return False, "沒有等著生效的升級"
        if str(now.date()) >= str(p["eff"]):
            return False, "%s 已經生效了，撤不回來（要降只能等自動減級）" % p["eff"]
        st["pending"] = None
        if st.get("suggest"):
            st["suggest"] = dict(st["suggest"], confirmed=None, revoked=now.isoformat(timespec="seconds"))
        try:
            _write_state(st, path)
        except OSError as e:
            return False, "口數狀態檔寫不進去（%s）—— 還沒撤回，請再按一次" % str(e)[:60]
    msg = "已撤回：維持 %s" % label(ladder_lots(st.get("level")))
    _log("upgrade_revoked", {"to": p["to"], "eff": p["eff"], "by": "you", "msg": msg}, now)
    return True, msg


# ══ 每天一次 ═══════════════════════════════════════════════════════

def first_open_weekday(y, m, closed=None):
    """那個月第一個「平日而且不是已知休市」的日子（10-14 起升級改成每天檢查，這支只剩畫面／測試在用）。"""
    closed = set(closed or ())
    d = date(y, m, 1)
    while d.weekday() >= 5 or str(d) in closed:
        d += timedelta(days=1)
    return d


def _pending_guard(st0, now, ctx=None, path=None):
    """
    ⭐ 老闆 10-14（QA 問題 2）：已確認、還沒生效的升級 ⇒ 每天看一次策略狀態（PENDING_GUARD_HOUR 以後；
    生效那天開盤前也會看到），有一條不是「正常」⇒ 作廢。⇒ [事件]。⛔ 重的計算在鎖外面。
    """
    p = (st0 or {}).get("pending")
    today = now.date()
    if not p or str(today) > str(p.get("eff")) or p.get("vchk") == str(today) or now.hour < PENDING_GUARD_HOUR:
        return []
    if ctx is not None and "retire" in ctx:
        ret, rerr = ctx.get("retire") or {}, ctx.get("retire_err")
    else:
        ret, rerr = retire_status()
    ev = []
    with _LOCK:
        st, err = read_state(path)
        p2 = st.get("pending") if not err else None
        if not p2 or p2.get("id") != p.get("id"):
            return []
        why = _strat_bad(ret, rerr)
        if why:
            ev.append(_void_pending(st, why, today))
        else:
            p2["vchk"] = str(today)
        _write_state(st, path)
    for e in ev:
        _log(e["kind"], dict(e, by="auto"), now)
        print("[口數] " + str(e.get("msg")), flush=True)
    return ev


def daily_check(rows, now=None, ctx=None, path=None, closed=None, busy=False):
    """
    ⚠️ 面板 poll_equity 那條執行緒呼叫（⛔ 不是主迴圈）。每多一列 equity/ 紀錄才真的做事
    （`checked` ＝ 上次看過的那一列日期）；其餘時候只做「已確認的升級每天看一次策略」（_pending_guard）。順序：
      ① 記下「本月用過的最大 a＋b」（風控上限用；⛔ 一定在減級之前記）
      ② 已確認的升級到了生效日 ⇒ 轉正（level、up_at＝生效日；試車那一次 ⇒ 試車開始）
      ③ 權益到 7 萬 ⇒ 第 0 級標成第 1 級（同樣 1＋1，不算升級）
      ④ 減級（自動）；試車還沒過就退回 1＋1 ⇒ 這次試車作廢
      ⑤ 試車進度（通過 ⇒ 記下來）
      ⑥ 已確認還沒生效的升級：策略掉了 ⇒ 作廢
      ⑦ 升級建議：**每個交易日**都重算；條件都過 ⇒ 出建議（同一級就沿用原本那一份）；條件掉了 ⇒ 收回
    `closed`：休市平日（'YYYY-MM-DD' 的集合；面板傳 closed_days()）。
    `ctx`（測試用）：{"retire": {...}, "retire_err": ..., "quality": {...}, "incidents": [...], "trial_events": [...]}。
    `busy`：手上有部位 ⇒ 「已確認的升級每天看一次策略」先不跑（不跟停損迴圈搶 CPU）；14:00 收盤那次照跑（那時本來就沒部位）。
    ⇒ {"ran", "events", "level", "err"}。⛔ 永遠不往外丟例外。
    ⚠️ 重的計算（退休標準、帳本）⛔ 在鎖外面先算好，拿鎖之後只做判斷與寫檔（apply_upgrade 也要拿同一把鎖）。
    """
    now = now or datetime.now()
    today = now.date()
    closed = set(closed or ())
    try:
        st0, err = read_state(path)
        if err:
            # ⛔ 狀態檔壞了：口數照樣 1＋1，但**不覆蓋**那個檔（留著給人看是怎麼壞的）
            return {"ran": False, "events": [], "level": 0, "err": err}
        d, eq = _latest_eq(rows)
        if d is None or st0.get("checked") == d:
            # ⭐ QA r2：手上有部位（busy）時跳過 —— 退休標準約 2.5 秒 CPU，會讓 4Hz 停損迴圈慢到 ~140ms；等沒部位再跑
            g = [] if busy else _pending_guard(st0, now, ctx, path)
            return {"ran": False, "events": g, "level": st0.get("level"), "err": None}
        q, ret, rerr, incs = _gather(ctx)          # ⛔ 鎖外面
        # 試車那幾筆（⛔ 鎖外面讀帳本）：試車中 ⇒ 從開始那天；今天剛好轉正的試車 ⇒ 從生效那天
        tr0, p0 = _trial(st0), st0.get("pending") or {}
        t_start = tr0.get("start") if tr0["status"] == "on" else (
            p0.get("eff") if (p0.get("kind") == "trial" and str(today) >= str(p0.get("eff"))) else None)
        tev = None
        if t_start:
            tev = (ctx or {}).get("trial_events") if "trial_events" in (ctx or {}) else _safe(
                lambda: trial_events(t_start), None)
        with _LOCK:
            st, err = read_state(path)
            if err:
                return {"ran": False, "events": [], "level": 0, "err": err}
            if st.get("checked") == d:
                return {"ran": False, "events": [], "level": st.get("level"), "err": None}
            ev = []
            # ① 本月最大（用「今天實際在跑」的那一級；換月就重來）
            st["month_max"] = _bump_month_max(st, today)
            # ② 轉正
            tr = _trial(st)
            p = st.get("pending")
            if p and str(today) >= str(p["eff"]):
                st["level"] = p["to"]
                st["since"] = str(p["eff"])
                if p["to"] >= 2:
                    st["up_at"] = str(p["eff"])           # ⭐ 「隔一個月」從**生效那天**算
                st["cap"] = {k: None for k in LANES}      # 升級要兩條都正常 ⇒ 單條降級的上限一併解除
                st["pending"] = None
                if tr["status"] == "passed":
                    tr["jumped"] = True
                elif ladder_lots(p["to"]) != (1, 1):
                    tr = dict(TRIAL_DEFAULT, status="on", start=str(p["eff"]))
                ev.append({"kind": "upgrade_effective", "to": p["to"], "to_ab": list(ladder_lots(p["to"])),
                           "msg": "%s 起改成 %s%s（你 %s 確認的）" % (
                               p["eff"], label(ladder_lots(p["to"])), "（試車）" if p.get("kind") == "trial" else "",
                               str(p.get("at"))[:16].replace("T", " "))})
            # ③ 第 0 級 → 第 1 級（同樣 1＋1）
            lad = _ladder()
            if (st.get("level") or 0) == 0 and lad and _num(eq) and eq >= lad[0][0]:
                st["level"] = 1
                ev.append({"kind": "free", "to": 1, "msg": "權益到 %s ⇒ 標成第 1 級（一樣 1＋1，不算升級）"
                                                            % format(int(lad[0][0]), ",")})
            # ④ 減級
            st, ev2 = downgrade(st, eq, ret, incs, today)
            ev += ev2
            if tr["status"] == "on" and ladder_lots(st.get("level") or 0) == (1, 1):
                tr = dict(TRIAL_DEFAULT)
                ev.append({"kind": "trial_void", "msg": "試車還沒過就退回 1＋1 ⇒ 這次試車作廢；權益回到 10 萬、條件都過會再建議試車"})
            if tr["status"] == "none" and ladder_lots(st.get("level") or 0) != (1, 1):
                # 不是經過試車升上來的（照理不會有；例如手動改過狀態檔）⇒ 當成試車中，從現在這一級開始那天算
                tr = dict(TRIAL_DEFAULT, status="on", start=str(st.get("since") or today))
            # ⑤ 試車進度
            if tr["status"] == "on" and tev is not None:
                te = trial_eval([e for e in tev if e[0][0] >= str(tr["start"])])
                tr.update(n=te["n"], slip=te["slip"], reset=te["reset"], miss=te.get("miss"))
                if te["passed"]:
                    tr.update(status="passed", passed_at=str(today))
                    ev.append({"kind": "trial_passed", "msg": "試車通過：%d 筆 2 口以上的真單、沒有下單錯誤、%s %.1f 點"
                                                              % (TRIAL_N, _tslip(), te["slip"])})
            st["trial"] = tr
            # ⑥ 已確認還沒生效 ⇒ 策略掉了就作廢
            if st.get("pending"):
                why = _strat_bad(ret, rerr)
                if why:
                    ev.append(_void_pending(st, why, today))
                else:
                    st["pending"]["vchk"] = str(today)
            # ⑦ 升級建議（每個交易日）
            chk = upgrade_check(st, eq, d, today, q, ret, rerr)
            st["conds"] = {"at": str(today), "eq": eq, "eq_date": d, "to": chk["to"], "kind": chk["kind"],
                           "list": chk["conds"], "why": chk["why"], "ok": chk["ok"], "q_n": q.get("n"),
                           "q_need": q.get("need"), "q": _q_small(q),
                           "states": {lane: (ret or {}).get(lane) for lane in LANES}, "rerr": rerr}
            sug = st.get("suggest")
            active = bool(sug and sug.get("ok") and not sug.get("withdrawn") and not sug.get("confirmed"))
            open_day = today.weekday() < 5 and str(today) not in closed
            if not st.get("pending"):
                same = active and sug.get("from") == chk["from"] and sug.get("to") == chk["to"] \
                    and sug.get("kind") == chk["kind"]
                if chk["ok"] and same:
                    st["suggest"] = dict(sug, conds=chk["conds"], eq=eq, eq_date=d, last_ok=str(today))
                elif chk["ok"] and open_day:
                    st["suggest"] = dict(chk)
                    ev.append({"kind": "suggest", "ok": True, "to": chk["to"], "why": [],
                               "msg": "可以升到 %s%s —— 要你在面板上確認才生效" % (
                                   chk["lots"], "（試車）" if chk["kind"] == "trial" else "")})
                elif active and not chk["ok"]:
                    st["suggest"] = dict(sug, withdrawn=str(today), why=chk["why"] or ["級數變了"])
                    ev.append({"kind": "suggest_withdrawn", "to": sug.get("to"),
                               "msg": "升到 %s 的建議收回：%s" % (sug.get("lots"),
                                                             "；".join(chk["why"] or ["級數變了"]))})
            st["checked"] = d
            _write_state(st, path)
        for e in ev:
            _log(e.get("kind"), dict(e, eq=eq, eq_date=d, by="auto"), now)
            if e.get("kind") not in ("suggest",):
                print("[口數] " + str(e.get("msg")), flush=True)
        return {"ran": True, "events": ev, "level": st.get("level"), "err": None}
    except Exception as e:                      # noqa: BLE001  ⛔ 這條執行緒不准死
        return {"ran": False, "events": [], "level": None, "err": "口數每日檢查出錯：%s" % str(e)[:120]}


def _bump_month_max(st, today):
    cur = list(ladder_lots(effective_level(st, today)))
    mm = st.get("month_max")
    month = str(today)[:7]
    if not isinstance(mm, dict) or mm.get("month") != month:
        return {"month": month, "ab": cur}
    if sum(cur) > sum(mm.get("ab") or (0, 0)):
        return {"month": month, "ab": cur}
    return mm


def _safe(fn, dflt):
    try:
        return fn()
    except Exception as e:                      # noqa: BLE001
        if isinstance(dflt, dict):
            return dict(dflt, why="%s（%s）" % (dflt.get("why") or "算不出來", str(e)[:60]))
        return dflt


# ══ 給畫面的一份（唯讀、便宜：只讀狀態檔＋紀錄尾巴；⛔ 不算升級條件）══════════════

_NOTICE_KINDS = {"down_equity": "down", "cap": "demote", "pause": "pause", "pending_void": "void",
                 "trial_void": "void", "pause_lifted": "lifted"}


def view(now=None, E=None, eq=None):
    """
    【自動下單】口數卡／手機要的資料（第二階段畫面才接；欄位對照見 LOTS-UI-SPEC.md §5）。
    ⛔ 唯讀；⛔ 不跑任何重的計算（升級條件用 daily_check 存下來的 `conds`；夜盤振幅只用已經算好的快取）。
    `E` ＝ 今晚那一晚（沒給 ⇒ 今天）；`eq` ＝ 券商現在的權益（沒給 ⇒ 用條件存下來的那一個）。
    """
    now = now or datetime.now()
    today = now.date()
    st, err = read_state()
    run_lv = effective_level(st, today)
    a, b = base_lots(st, today)
    lad = _ladder()
    nxt = None
    if run_lv < len(lad):
        to = run_lv + 1
        if run_lv == 0 and ladder_lots(1) == ladder_lots(0) and len(lad) >= 2:
            to = 2                                   # 7 萬那一級跟起始級一樣 ⇒ 下一個「會變口數」的是 10 萬
        ab = ladder_lots(to)
        nxt = {"lvl": to, "eq": lad[to - 1][0], "a": ab[0], "b": ab[1], "label": label(ab)}
    conds = st.get("conds") or {}
    eq_v = eq if _num(eq) else conds.get("eq")
    if nxt and _num(eq_v):
        nxt["gap"] = max(0, nxt["eq"] - eq_v)
    floor = (lad[run_lv - 1][0] * DOWN_FRAC) if (run_lv >= 2 and lad) else None
    sug = st.get("suggest") or None
    offer = None
    if sug and sug.get("ok") and not sug.get("withdrawn") and not sug.get("confirmed") \
            and not st.get("pending") and _is_int(sug.get("to")) and sug.get("from") == (st.get("level") or 0):
        ab = ladder_lots(sug["to"])
        offer = {"id": sug["id"], "kind": sug.get("kind"),
                 "to": {"lvl": sug["to"], "a": ab[0], "b": ab[1], "label": label(ab),
                        "eq": lad[sug["to"] - 1][0] if lad else None},
                 "until": None, "made": sug.get("made"), "eff": str(today),
                 "floor_after": (lad[sug["to"] - 1][0] * DOWN_FRAC) if lad else None,
                 "cap_after": CAP_PER_LEG * sum(ab), "cap_now": CAP_PER_LEG * sum(cap_lots(st, today))}
    pend = st.get("pending")
    pending = None
    if pend:
        ab = ladder_lots(pend["to"])
        pending = {"to": {"lvl": pend["to"], "a": ab[0], "b": ab[1], "label": label(ab)},
                   "eff": pend["eff"], "at": pend.get("at"), "kind": pend.get("kind")}
    logs = read_log(40)
    notice = None
    for o in logs:
        k = _NOTICE_KINDS.get(o.get("kind"))
        if not k:
            continue
        try:
            dd = date.fromisoformat(str(o.get("at"))[:10])
        except ValueError:
            continue
        until = _add_weekdays(dd, NOTICE_DAYS)
        if today <= until:
            notice = {"kind": k, "date": str(dd), "text": o.get("msg"), "until": str(until)}
        break
    hist = [{"date": str(o.get("at"))[:10], "by": o.get("by") or "auto", "kind": o.get("kind"),
             "from": (label(o["from_ab"]) if o.get("from_ab") else None),
             "to": (label(o["to_ab"]) if o.get("to_ab") else None), "why": o.get("msg")}
            for o in logs if o.get("kind") in ("down_equity", "cap", "pause", "upgrade_confirmed",
                                               "upgrade_effective", "upgrade_revoked", "suggest_withdrawn",
                                               "pending_void", "trial_passed", "trial_void")][:3]
    cap = st.get("cap") or {}
    per = {lane: {"cap": cap.get(lane), "lots": (a if lane == "union" else b)} for lane in LANES}
    cab = cap_lots(st, today)
    mm = st.get("month_max") or {}
    cap_note = None
    if mm.get("month") == str(today)[:7] and sum(cab) > sum(ladder_lots(run_lv)):
        cap_note = "本月用過最多 %s ⇒ 這個月上限維持 %s，下個月起照 %s 算" % (
            label(cab), format(CAP_PER_LEG * sum(cab), ","), label(ladder_lots(run_lv)))
    E = E or today
    # ⭐ 跟送單路徑問同一支（rows 不給 ⇒ 只用快取、⛔ 不讀檔）⇒ 畫面寫的口數就是「現在送的話會送幾口」
    tp = night_plan(E, st)
    tonight = {k: tp.get(k) for k in ("b", "qty", "vol", "thr", "high", "last", "why")}
    # 下一次檢查（10-14 起每個交易日收盤後都檢查）：今天是交易日、而且今天收盤那一次還沒跑 ⇒ 今天；否則下一個交易日
    done_today = str(st.get("checked") or "") >= str(today)
    if today.weekday() < 5 and str(today) not in KNOWN_CLOSED and not done_today:
        nxt_check = today
    else:
        nxt_check = next_weekday(today)
    return {"ok": err is None, "err": err, "file": LEVEL_FILE.exists(),
            "run": {"lvl": run_lv, "a": a, "b": b, "label": label((a, b)),
                    "ladder": label(ladder_lots(run_lv)), "since": st.get("since")},
            "tonight": tonight, "eq": eq_v, "eq_at": conds.get("eq_date"),
            "next": nxt, "floor": floor, "prev_eq": (lad[run_lv - 1][0] if (run_lv >= 1 and lad) else 0),
            "check": {"next": str(nxt_check), "today": nxt_check == today, "done_today": done_today},
            "trial": _trial(st), "void": st.get("void"),
            "conds": conds.get("list") or [], "conds_why": conds.get("why") or [], "conds_at": conds.get("at"),
            "offer": offer, "pending": pending, "notice": notice, "hist": hist, "per": per,
            "pause_until": st.get("pause_until"), "up_at": st.get("up_at"),
            "cap": {"per_lot": CAP_PER_LEG, "ab": list(cab), "total": CAP_PER_LEG * sum(cab), "note": cap_note},
            "margin_lots": max(a, b, 1), "night_vol_on": NIGHT_VOL_ON}


# ══ 給畫面的整份（LOTS-UI-SPEC.md §5）：句子全部在這裡組好，⛔ 前端只畫、⛔ 不自己算 ═══════════════
#    唯讀、便宜：狀態檔＋紀錄尾巴＋（已經算好的）夜盤振幅快取。⛔ 不跑升級條件（用 daily_check 存下來的 conds）。

_WD = "一二三四五六日"
FAR_GAP = 40000             # 離下一級還差超過 4 萬 ⇒ 「離下一級還遠」；4 萬以內 ⇒ 「還差 X 萬」（UI 規格 §4 的語氣）


def wan2(v):
    """元 ⇒「8.31 萬」「8.5 萬」「10 萬」（到 0.01 萬，去掉尾巴的 0）—— 減級那一句要看得出差多少。"""
    if not _num(v):
        return "—"
    x = "%.2f" % (float(v) / 10000.0)
    return x.rstrip("0").rstrip(".") + " 萬"


def wan(v):
    """元 ⇒「5.1 萬」「10 萬」（四捨五入到 0.1 萬，整數就不帶 .0）。"""
    if not _num(v):
        return "—"
    x = round(float(v) / 10000.0, 1)
    return ("%d" % x if x == int(x) else "%.1f" % x) + " 萬"


def dtxt(d, wd=True):
    """'2026-11-03' ⇒「11/3（二）」。"""
    try:
        d = _d(d)
    except Exception:                           # noqa: BLE001
        return str(d or "")
    return "%d/%d" % (d.month, d.day) + (("（%s）" % _WD[d.weekday()]) if wd else "")


def _money(v):
    return format(int(round(v)), ",") if _num(v) else "—"


def _tier(lv):
    lad = _ladder()
    if lv <= 1 or not lad:
        return "起始級"
    return "第 %d 級・%s" % (lv, wan(lad[lv - 1][0]))


def _next_of(lv):
    """下一個「會變口數」的級（7 萬那一級跟起始級一樣 ⇒ 第 0 級的下一級是 10 萬那一級）。"""
    lad = _ladder()
    if lv >= len(lad):
        return None
    to = lv + 1
    while to < len(lad) and ladder_lots(to) == ladder_lots(lv):
        to += 1
    return to


_STRAT_WORD = {"watch": "觀察", "down": "降級", "retire": "退休", "back": "降級（可升回）", None: "算不出來"}


def _cond_html(c):
    """一條升級條件 ⇒ 給人看的那一句（⛔ 前端只畫）。"""
    k = c.get("k")
    if k == "eq":
        eq, need = c.get("eq"), c.get("need")
        if not _num(eq):
            return "不知道權益（equity/ 還沒有紀錄）"
        if c.get("ok"):
            return "<b>%s</b> ≥ %s" % (wan(eq), wan(need))
        return "<b>%s</b>，要 %s，還差 <b>%s</b>" % (wan(eq), wan(need), wan(max(0, need - eq)))
    if k == "err":
        n, need = c.get("n") or 0, c.get("need_n") or RECENT_N
        if not n:
            return "還沒有任何自動單"
        head = ("最近 %d 筆" % need) if n >= need else ("目前 <b>%d</b> 筆（還不到 %d 筆）" % (n, need))
        return "%s送出去之後失敗 <b>%d</b> 次" % (head, c.get("errors") or 0)
    if k == "slip":
        avg, n = c.get("avg"), c.get("n") or 0
        if not _num(avg):
            return "算不出平均滑價（還沒有成交紀錄）"
        head = ("最近 %d 筆" % RECENT_N) if n >= RECENT_N else ("目前 %d 筆" % n)
        return "%s平均 <b>%.1f</b> 點%s" % (head, avg, ("（要 ≤ %g）" % SLIP_MAX) if c.get("ok") else ("，要 ≤ %g" % SLIP_MAX))
    if k == "strat":
        if c.get("err"):
            import html
            return "算不出來（%s）" % html.escape(str(c["err"]))
        S = c.get("states") or {}
        if S and all(v == "ok" for v in S.values()):
            return "兩條都<b>正常</b>（不在觀察／降級）"
        return "、".join("%s在<b>%s</b>" % (LANE_NAME.get(l, l), _STRAT_WORD.get(v, v))
                        for l, v in S.items() if v != "ok") or "算不出來"
    if k == "trial":
        if c.get("status") == "passed":
            return "<b>已通過</b>" + ("（%s）" % dtxt(c["passed_at"], False) if c.get("passed_at") else "") + \
                "：可以直接跳到權益撐得起的那一級"
        n, need = c.get("n") or 0, c.get("need") or TRIAL_N
        t = "2＋1 試車中：<b>%d／%d 筆</b>（2 口以上的真單）" % (n, need)
        if _num(c.get("slip")):
            t += "，%s %.1f 點" % (_tslip(), c["slip"])
        if c.get("reset"):
            t += "；%s 有一次下單錯誤，從那之後重算" % dtxt(c["reset"], False)
        return t
    if k == "cool":
        mode = c.get("mode")
        if mode == "trial":
            return "第一次升級是試車（2＋1），不用等"
        if mode == "first_jump":
            return "試車通過後第一次跳級，不用等"
        if not c.get("up_at"):
            return "還沒升過"
        return "上次升級 <b>%s</b> 生效，%s" % (dtxt(c["up_at"], False),
                                         ("<b>%s</b> 起才能再升（至少隔一個月）" % dtxt(c.get("free"), False))
                                         if not c.get("ok") else "已經隔滿一個月")
    if k == "pause":
        if not c.get("until") or c.get("ok"):
            return "最近 6 個月沒有"
        return "停損失靈多賠超過原停損 ⇒ <b>%s</b> 前不升" % c["until"]
    return ""


def _strip(s):
    import re
    return re.sub(r"<[^>]+>", "", str(s or ""))


def panel(now=None, eq=None, lot1=None, E=None):
    """
    【自動下單】口數卡／頂部金條／分頁小金點／藥丸／今天兩張卡／【帳戶】那一行／手機 要的整份（LOTS-UI-SPEC §5）。
    `eq`＝券商現在的權益（沒有 ⇒ 用條件存下來的那一個）；`lot1`＝一口原始保證金（面板學到的；⛔ 不寫死）；
    `E`＝今晚那一晚（沒給 ⇒ 今天）。⛔ 讀不到狀態 ⇒ ok=False＋一句話（照實講：讀不到的時候照 1＋1 送）。
    ⛔ 永遠不往外丟例外（這一份跟著 /api/fire/state 端出去，⛔ 不可以把整份帶掉）。
    """
    now = now or datetime.now()
    today = now.date()
    try:
        return _panel(now, today, eq, lot1, E)
    except Exception as e:                      # noqa: BLE001
        return {"ok": False, "err": "讀不到口數狀態（%s）—— 讀不到的時候照 1＋1 送" % str(e)[:80],
                "dot": False, "alarm": [],
                "phone": {"label": "1＋1", "line": "讀不到口數狀態，照 1＋1 送", "att": False, "warn": None}}


def _panel(now, today, eq, lot1, E):
    V = view(now=now, E=E, eq=eq)
    st, serr = read_state()
    out = dict(V)
    if serr:
        out["ok"], out["err"] = False, "讀不到口數狀態（%s）—— 讀不到的時候照 1＋1 送" % serr
    lad = _ladder()
    run = dict(V["run"])
    lv, a, b = run["lvl"], run["a"], run["b"]
    la, lb = ladder_lots(lv)
    run["tier"] = _tier(lv)
    capped = []
    if a < la:
        capped.append("日盤被降級壓在 %d 口" % a)
    if b < lb:
        capped.append("夜盤被降級壓在 %d 口" % b)
    run["capped"] = "、".join(capped) or None
    run["sub"] = ("日盤多方聯軍 <b>%d 口</b>・夜盤跟勢 <b>%d 口</b>" % (a, b) +
                  ("（振幅偏高的晚上；其他晚上 %d 口）" % max(1, b - 1) if (b >= 2 and NIGHT_VOL_ON) else ""))
    out["run"] = run
    # 下一級：⭐ 跳過跟現在同口數的那一級（7 萬那一級跟起始級一樣）
    nto = _next_of(lv)
    nxt = None
    if nto is not None:
        ab = ladder_lots(nto)
        nxt = {"lvl": nto, "eq": lad[nto - 1][0], "a": ab[0], "b": ab[1], "label": label(ab)}
        if _num(V.get("eq")):
            nxt["gap"] = max(0, nxt["eq"] - V["eq"])
    out["next"] = nxt
    out["prev_eq"] = lad[lv - 1][0] if (lv >= 2 and lad) else 0
    # 進度條（⛔ 前端不自己算百分比、不自己換成「萬」）
    if nxt:
        span = nxt["eq"] - out["prev_eq"]
        pct = (max(0.0, min(100.0, (V["eq"] - out["prev_eq"]) * 100.0 / span))
               if (_num(V.get("eq")) and span > 0) else 0.0)
        out["bar"] = {"pct": round(pct, 1), "eq_txt": wan(V.get("eq")), "next_txt": wan(nxt["eq"]),
                      "lo_txt": (("退級線 " + wan(V["floor"])) if V.get("floor") else
                                 (wan(out["prev_eq"]) if out["prev_eq"] else "0"))}
        nxt["eq_txt"] = wan(nxt["eq"])
    else:
        out["bar"] = None
    # 夜盤：今晚幾口（⛔ 只用已經算好的振幅快取；沒算過就照實講）
    T = dict(V.get("tonight") or {})
    if b <= 1:
        q = 1 if b == 1 else 0
        line = "今晚 <b>%d 口</b>" % q + ("（%s 的夜盤也是 1 口）" % label((a, b)) if (a >= 2 and b == 1) else "")
    elif T.get("vol") is not None:
        q = T.get("qty") if T.get("qty") is not None else night_qty(b, T.get("high"))
        line = "今晚 <b>%d 口</b>：近幾晚夜盤振幅指標 <b>%.2f%%</b>（≥ %.2f%% 算偏高）。沒偏高的晚上做 %d 口。" % (
            q, T["vol"], NIGHT_VOL_THR, max(1, b - 1))
    else:
        q = T.get("qty") if T.get("qty") is not None else max(1, b - 1)
        # 快取還沒有／資料太舊／不夠 22 晚 ⇒ 都是「不知道」⇒ 規則 B 照沒偏高算（max(1, b−1)）
        line = "今晚 <b>%d 口</b>：%s" % (q, T.get("why") or "振幅資料不夠新") + ("，照沒偏高算" if q < b else "")
    T.update(qty=q, line=line)
    out["tonight"] = T
    # 每個交易日收盤後檢查（10-14 新規則）
    chk_next = _d(V["check"]["next"])
    out["check"] = dict(V["check"], next_txt=("今天收盤後" if chk_next == today else dtxt(chk_next) + "收盤後"),
                        head=("今天已經檢查過" if V["check"].get("done_today") else "每個交易日收盤後檢查"))
    # 試車進度（⭐ 老闆 10-14：口數卡要看得到「試車 0／1 筆」；格數＝TRIAL_N）
    tr = V.get("trial") or dict(TRIAL_DEFAULT)
    trial = {"status": tr.get("status"), "n": tr.get("n") or 0, "need": TRIAL_N, "line": None, "pips": None}
    if tr.get("status") == "on":
        trial["pips"] = [i < (tr.get("n") or 0) for i in range(TRIAL_N)]
        trial["line"] = ("試車 <b>%d／%d 筆</b>：2＋1 有 %d 筆 2 口以上的真單、沒有下單錯誤、%s ≤ %g 點，"
                         "就可以直接跳到權益撐得起的那一級。" % (trial["n"], TRIAL_N, TRIAL_N, _tslip(), SLIP_MAX))
        miss = _trial_miss(tr)
        if miss:
            trial["line"] += miss
        elif _num(tr.get("slip")):
            trial["line"] += "目前%s %.1f 點。" % (_tslip(), tr["slip"])
        if tr.get("reset"):
            trial["line"] += "（%s 有一次下單錯誤，筆數從那之後重算。）" % dtxt(tr["reset"], False)
    elif tr.get("status") == "passed":
        trial["line"] = "試車已通過（%s）：之後權益撐得到哪一級，就直接建議跳到哪一級；每次升級至少隔一個月。" % (
            dtxt(tr.get("passed_at"), False) if tr.get("passed_at") else "—")
    out["trial"] = trial
    # 條件（句子）
    L = [{"k": c.get("k"), "name": c.get("name"), "ok": c.get("ok"), "html": _cond_html(c)}
         for c in (V.get("conds") or [])]
    if nxt and nxt["lvl"] >= 5:
        L.append({"k": "ops", "name": "營運", "ok": None, "html": "升到 5＋2 以上前要再確認停電備援（09-30 說好的）"})
    out["conds"] = L
    out["conds_note"] = None if any(c["k"] != "ops" for c in L) else "升級條件還沒算過（面板每個交易日 14:00 收盤後算一次）"
    offer, pend, notice = V.get("offer"), V.get("pending"), V.get("notice")
    out["conds_open"] = bool(offer or notice or any(c["ok"] is False and c["k"] != "eq" for c in L))
    bad = [c["name"] for c in L if c["ok"] is False and c["k"] != "eq"]
    # 一句結論
    eqv = V.get("eq")
    is_trial = bool(offer and offer.get("kind") == "trial")
    if pend:
        vd = {"kind": "pending", "title": "已確認：%s日盤起改成 %s%s" % (
                  dtxt(pend["eff"]), pend["to"]["label"], "（試車）" if pend.get("kind") == "trial" else ""),
              "sub": "今天照舊 %s。在那之前改主意可以撤回；策略在那之前掉到觀察或降級，會自動作廢。" % label((a, b)),
              "att": False}
    elif offer:
        vd = {"kind": "offer",
              "title": ("條件都過了，可以先試車 %s" if is_trial else "條件都過了，可以升到 %s") % offer["to"]["label"],
              "sub": ("試車＝先用 %s 跑 %d 筆 2 口以上的真單，看下單順不順；過了之後權益撐得到哪一級就直接跳。"
                      % (offer["to"]["label"], TRIAL_N) if is_trial else "") +
                     "要你按確認才會生效。每個交易日收盤後重新檢查，條件掉了就自動收回。",
              "att": True}
    elif notice and notice.get("kind") == "void":
        vd = {"kind": "void", "title": "已確認的升級作廢了",
              "sub": str(notice.get("text") or ""), "att": True}
    elif notice and notice.get("kind") == "down":
        vd = {"kind": "down", "title": "%s 起已自動退回 %s" % (dtxt(notice["date"], False), label((a, b))),
              "sub": "%s 減級是自動的，不用你按。" % (notice.get("text") or ""), "att": True}
    elif nxt is None:
        vd = {"kind": "top", "title": "已經到最後一級（%s）" % label((la, lb)),
              "sub": "50 萬以上要再用同一把尺往上算。", "att": False}
    elif tr.get("status") == "on":
        vd = {"kind": "trial", "title": "試車中：%d／%d 筆" % (trial["n"], TRIAL_N),
              "sub": (_trial_miss(tr) or "") + "試車跑完之前不會出升級建議。", "att": False}
    elif _num(eqv) and eqv >= nxt["eq"]:
        vd = {"kind": "blocked", "title": "先不升：卡在%s" % ("、".join("「%s」" % x for x in bad) or "條件"),
              "sub": "權益已經夠 %s 了；%s會再檢查一次。" % (nxt["label"], out["check"]["next_txt"]), "att": False}
    else:
        gap = nxt.get("gap")
        far =(not _num(gap)) or gap > FAR_GAP
        vd = {"kind": "far", "title": "離下一級還遠" if far else "離下一級還差 %s" % wan(gap),
              "sub": ("權益到 %s、而且其他條件都過，那天收盤後就會出現%s。" % (
                  wan(nxt["eq"]), "試車建議（先跑 2＋1）" if tr.get("status") != "passed" else "升級建議")) +
              (("權益掉到 %s 以下（%s × 0.85），隔天會自動退回 %s。" % (
                  wan(V["floor"]), wan(lad[lv - 1][0]), label(ladder_lots(max(0, lv - 1)))))
               if V.get("floor") else ""),
              "att": False}
    out["verdict"] = vd
    # 升級建議：日期句子＋確認條的代價表（⛔ 保證金用面板學到的 lot1 × max(a, b)，⛔ 不寫死）
    if offer:
        ab0, ab1 = (la, lb), (offer["to"]["a"], offer["to"]["b"])
        c0, c1 = LADDER_COST.get(ab0), LADDER_COST.get(ab1)
        i0 = max(1, lv)
        p0 = lad[i0 - 1][3] if lad else None
        p1 = lad[offer["to"]["lvl"] - 1][3] if lad else None

        def cst(C, i):
            return ("約 " + wan(C[i])) if C else "沒算過"

        def mg(x):
            return _money(float(lot1) * max(x)) if _num(lot1) else "不知道（還沒學到一口保證金）"
        jump_n = offer["to"]["lvl"] - max(1, lv) if offer.get("kind") == "jump" else 0
        power = None
        if ab1[0] >= 5:
            # ⭐ 老闆 10-14：目標日盤 ≥ 5 口 ⇒ 確認條多一行醒目的停電代價＋提醒（⛔ 數字照研究表，沒算過就照實講）
            power = {"line": "停電沒人處理時最壞一次約 <b>%s</b>" % (wan(c1[2]) if c1 else "（沒算過）"),
                     "remind": "升到這一級之前要再確認停電備援"}
        offer = dict(offer, to=dict(offer["to"], eq_txt=wan(offer["to"]["eq"])),
                     until_txt=None, keep_txt="條件還在就一直有效（每個交易日收盤後重新檢查）",
                     eff_txt="確認後立刻生效", power=power,
                     arm_txt=("先試車 %s" % label(ab1)) if offer.get("kind") == "trial" else ("升到 %s" % label(ab1)),
                     q=((("確定從 <b>%s</b> 先試車 <b>%s</b>？" % (label(ab0), label(ab1))) if offer.get("kind") == "trial"
                        else ("確定從 <b>%s</b> 直接跳到 <b>%s</b>（跳 %d 級）？" % (label(ab0), label(ab1), jump_n))
                        if jump_n >= 2 else ("確定從 <b>%s</b> 升到 <b>%s</b>？" % (label(ab0), label(ab1)))) +
                        ("日盤每一筆從 %d 口變 %d 口，賺賠跟著放大。" % (ab0[0], ab1[0]) if ab1[0] != ab0[0] else "") +
                        ("夜盤（振幅偏高的晚上）從 %d 口變 %d 口。" % (ab0[1], ab1[1]) if ab1[1] != ab0[1] else "")),
                     costs=[{"k": "口數（日盤＋夜盤）", "now": label(ab0) + " 口", "after": label(ab1) + " 口"},
                            {"k": "要壓的保證金", "now": mg(ab0), "after": mg(ab1)},
                            {"k": "從高點回落：一般（中位數）", "now": cst(c0, 0), "after": cst(c1, 0)},
                            {"k": "從高點回落：很糟的年（最差 5%）", "now": cst(c0, 1), "after": cst(c1, 1)},
                            {"k": "停損失靈最壞一次", "now": cst(c0, 2), "after": cst(c1, 2)},
                            {"k": "一年內要補錢的機率（剛好在門檻起步時）",
                             "now": ("約 %s%%（%s）" % (p0, wan(lad[i0 - 1][0]))) if p0 is not None else "沒算過",
                             "after": ("約 %s%%（%s）" % (p1, wan(offer["to"]["eq"]))) if p1 is not None else "沒算過"},
                            {"k": "本月風控上限", "now": "−%s 點" % _money(CAP_PER_LEG * sum(ab0)),
                             "after": "−%s 點" % _money(CAP_PER_LEG * sum(ab1))}],
                     fine=["<b>確認後立刻生效：今晚夜盤起／下一筆日盤起</b>照新口數；手上已經有的部位照原本口數處理（不追加、不平）。"
                           "確認之後不能撤回（要降只能等自動減級）。",
                           "權益掉到 <b>%s</b> 以下，隔天自動退回權益撐得起的那一級。" % wan(offer["floor_after"])] +
                          (["試車：有 %d 筆 2 口以上的真單、沒有下單錯誤、%s ≤ %g 點就算通過；"
                            "試車中被退回 1＋1 的話，這次試車作廢。" % (TRIAL_N, _tslip(), SLIP_MAX)] if offer.get("kind") == "trial" else []) +
                          [
                           "停損失靈最壞一次＝白天斷線沒處理、留到 13:43:30 才平的那一次（2025-04-09）。",
                           "以上回落與機率是<b>回測推估、不是保證</b>（優勢打折到 65%、重抽樣 2 萬次）。"],
                     src=LADDER_COST_SRC)
        out["offer"] = offer
    if pend:
        pend = dict(pend, eff_txt=dtxt(pend["eff"]) + "日盤",
                    at_txt=str(pend.get("at") or "")[5:16].replace("T", " ").replace("-", "/"))
        out["pending"] = pend
    # 每一條（降級／退休的說明句）
    S = {}
    for c in (V.get("conds") or []):
        if c.get("k") == "strat":
            S = c.get("states") or {}
    per = {}
    for i, lane in enumerate(LANES):
        cp = (st.get("cap") or {}).get(lane)
        s = S.get(lane)
        txt = None
        if s == "retire" or cp == 0:
            txt = ("%s 已退休：自動壓到 1 口，要不要關掉由你按" % LANE_NAME[lane]) if not RETIRE_ZERO \
                else ("%s 已退休：0 口（不送）" % LANE_NAME[lane])
        elif cp == 1:
            txt = "%s 進入退休標準的降級 ⇒ 壓在 1 口" % LANE_NAME[lane]
        per[lane] = {"state": s, "cap": cp, "lots": (a, b)[i], "text": txt}
    out["per"] = per
    # 頂部金條／分頁小金點（⛔ 「卡在條件」不是事，不上金條）
    alarm = []
    if offer and not pend:
        alarm.append({"kind": "offer", "text": "口數：條件都過了，可以%s %s（要你按確認才會生效）。"
                                                 % ("先試車" if is_trial else "升到", offer["to"]["label"])})
    if notice:
        alarm.append({"kind": notice.get("kind"), "text": notice.get("text")})
    for lane in LANES:
        if per[lane]["text"] and (per[lane]["state"] == "retire" or per[lane]["cap"] == 0):
            alarm.append({"kind": "retire", "text": per[lane]["text"]})
    out["alarm"] = alarm
    out["dot"] = bool(alarm)
    cab = V["cap"]["ab"]
    out["cap"] = dict(V["cap"], line="上限 ＝ %d 點 ×（%s 口）＝ %s" % (CAP_PER_LEG, label(cab), _money(CAP_PER_LEG * sum(cab))))
    # 藥丸／今天兩張卡的口數
    pills = {"day": "%d 口" % a, "night": "%d 口" % q}
    if pend and _d(pend["eff"]) == next_weekday(today) and pend["to"]["a"] != a:
        pills["day"] = "%d 口 → 明天 %d 口" % (a, pend["to"]["a"])
    out["pills"] = pills
    yday = None
    if notice and notice.get("kind") == "down" and notice.get("date") == str(today):
        for h in V.get("hist") or []:
            if h.get("kind") == "down_equity" and h.get("date") == str(today) and h.get("from"):
                yday = h["from"].split("＋")[0]
    out["day_line"] = "今天 <b>%d 口</b>" % a + (("（昨天是 %s 口）" % yday) if (yday and yday != str(a)) else "")
    # 【帳戶】目標卡那一行
    # ⭐ 2026-10-20：通知那一句可以被他按 ✕ 關掉（前端記在瀏覽器）⇒ 平常那一句一律算好放 plain，關掉後改顯示它
    bits = ["現在做 <b>%s</b>" % label((a, b))]
    if pend:
        bits.append("（<b>%s</b> 起 %s）" % (dtxt(pend["eff"], False), pend["to"]["label"]))
    if vd["kind"] == "blocked":
        bits.append("・卡在" + ("、".join("「%s」" % x for x in bad) or "條件"))
    if tr.get("status") == "on":
        bits.append("・試車 %d／%d 筆" % (trial["n"], TRIAL_N))
    bits.append("・下次檢查 " + out["check"]["next_txt"])
    if V.get("floor"):
        bits.append("・掉到 %s 會自動退一級" % wan(V["floor"]))
    plain = {"text": "".join(bits), "att": False}
    if offer and not pend:
        al = {"text": "口數：可以%s <b>%s</b> 了，要到【自動下單】按確認。" % ("先試車" if is_trial else "升到",
                                                                    offer["to"]["label"]),
              "att": True}
    elif notice:
        # seen_key ＝ 跟【自動下單】頂部金條同一把鑰匙（種類｜那句話）⇒ 任何一邊按 ✕，兩邊都收
        al = {"text": "口數：" + str(notice.get("text") or ""), "att": True,
              "seen_key": "%s|%s" % (notice.get("kind"), notice.get("text") or ""), "plain": plain}
    else:
        al = plain
    out["lots_line"] = al
    # 手機（⛔ 零按鈕；可以升級／剛被減級才進「要注意的事」）
    plabel = label((a, b)) + ("（起始級）" if lv <= 1 else ("（試車）" if tr.get("status") == "on" else ""))
    if pend:
        plabel = label((a, b)) + "（%s 起%s %s）" % (dtxt(pend["eff"], False),
                                                   "試車" if pend.get("kind") == "trial" else "", pend["to"]["label"])
    warn = None
    if offer and not pend:
        warn = "可以%s %s 了 —— 要在電腦面板【自動下單】按確認才會生效" % (
            "先試車" if is_trial else "升到", offer["to"]["label"])
    elif notice and notice.get("kind") != "lifted":      # 解除暫停是好消息，⛔ 不進「要注意的事」
        warn = notice.get("text")
    out["phone"] = {"label": plabel, "line": _strip(vd["title"] + "。" + vd["sub"]), "att": bool(warn), "warn": warn}
    # ⭐ 2026-10-19 手機減字版（老闆：字太多、看不懂術語）：一句白話＋「要你處理」那幾則（⛔ 手機只照畫、不自己判斷）。
    #    舊的 label／line／warn 照留（電腦面板與舊手機相容）。
    try:
        out["phone"]["short"] = _phone_short(today, vd, pend, offer, notice, nxt, tr, trial, L, st, (a, b), is_trial)
        out["phone"]["todo"] = _phone_todo(offer, pend, per, (a, b), is_trial)
    except Exception:                           # noqa: BLE001  ⛔ 白話句組不出來不可以把整份口數帶掉
        out["phone"]["short"], out["phone"]["todo"] = None, []
    out["hist"] = [dict(h, date_txt=dtxt(h.get("date"), False), who=("你按的" if h.get("by") == "you" else "自動"))
                   for h in (V.get("hist") or [])]
    out["rule_foot"] = ("每個交易日收盤後檢查；升級要你按確認才生效。先在 1＋1 試車 2＋1：有 %d 筆 2 口以上的真單、"
                        "沒有下單錯誤、%s ≤ %g 點就算通過；通過之後權益撐得到哪一級就直接建議跳到哪一級，"
                        "之後每次升級至少隔一個月。減級是自動的（權益低於這一級門檻的 85%%，隔天就退）。"
                        "⛔ 不看最近賺不賺。" % (TRIAL_N, _tslip(), SLIP_MAX))
    return out


# ══ 手機減字版（2026-10-19）：G「升級」那一列＋「要你處理」（⛔ 不出現術語；正本規格見 phone-demo SPEC §3 §4）══
# 升級條件卡住時的白話（≤ 12 字；⛔ 不寫「停損失靈」「滑價」這類術語）
_COND_PLAIN = {"err": "最近下單有失敗", "slip": "成交價差太多", "strat": "策略表現還在觀察",
               "trial": "試車還沒跑完", "cool": "離上次升級不到一個月", "pause": "暫停升級中"}


def _md_plain(d, today):
    """'2027-04-19' ⇒「明年 4/19」；同一年 ⇒「4/19」。"""
    try:
        d = _d(d)
    except Exception:                           # noqa: BLE001
        return str(d or "")
    return ("明年 " if d.year == today.year + 1 else ("%d 年 " % d.year if d.year > today.year + 1 else "")) + \
        "%d/%d" % (d.month, d.day)


def _pause_inc_day(st):
    """停損失靈暫停是哪一天那筆造成的（從紀錄尾巴找最近一筆 pause）；找不到 ⇒ None。"""
    try:
        for o in read_log(80):
            if o.get("kind") == "pause" and isinstance(o.get("inc"), dict) and o["inc"].get("d"):
                if str(o.get("until") or "") == str(st.get("pause_until") or ""):
                    return o["inc"]["d"]
    except Exception:                           # noqa: BLE001
        pass
    return None


def _phone_short(today, vd, pend, offer, notice, nxt, tr, trial, L, st, ab, is_trial):
    """手機【帳戶】「升級」那一列（一句，≤ 22 字）。"""
    k = vd.get("kind")
    if pend:
        return "%s 起改 %s%s" % (dtxt(pend["eff"], False), pend["to"]["label"],
                                "（試車）" if pend.get("kind") == "trial" else "")
    if offer:
        return ("可以試車 %s（等你按確認）" if is_trial else "可以升到 %s（等你按確認）") % offer["to"]["label"]
    if k == "down" and notice:
        return "%s 起自動退回 %s（權益變少）" % (dtxt(notice.get("date"), False), label(ab))
    if k == "void":
        return "已確認的升級作廢了"
    if k == "top":
        return "已經到最高一級"
    if k == "trial":
        if _trial_miss(tr):
            # ⭐ QA 10-21：上一筆沒算進去 ⇒ 手機那一列也要講（⛔ 不能只寫筆數讓人以為跑完了）
            sl = tr.get("slip")
            if _num(sl) and sl > SLIP_MAX:
                return ("上一筆滑價 %.1f 點不算，試車等下一筆" if TRIAL_N == 1
                        else "最近%d筆平均滑價 %.1f 點，還不算") % ((sl,) if TRIAL_N == 1 else (TRIAL_N, sl))
            return "上一筆算不出滑價，試車等下一筆"
        return "試車中：%d／%d 筆，跑完才會再談升級" % (trial.get("n") or 0, trial.get("need") or TRIAL_N)
    pu = st.get("pause_until")
    if pu and str(today) < str(pu):
        inc = _pause_inc_day(st)
        md = _md_plain(pu, today)
        return "升級暫停到" + (md if md[:1] in ("明",) or "年" in md else " " + md) + \
            (("（%s 那筆賠得比停損多）" % dtxt(inc, False)) if inc else "（有一筆賠得比停損多）")
    if k == "blocked":
        bad = [_COND_PLAIN.get(c.get("k"), c.get("name")) for c in L if c.get("ok") is False and c.get("k") not in ("eq", "ops")]
        return "先不升：" + ("、".join(x for x in bad if x) or "條件還沒過")
    if nxt:
        what = ("試車 2＋1" if tr.get("status") != "passed" else "升到 %s" % nxt["label"])
        gap = nxt.get("gap")
        return "權益到 %s可以%s" % (wan(nxt["eq"]), what) + (("（還差 %s）" % wan(gap)) if _num(gap) and gap > 0 else "")
    return None


def _phone_todo(offer, pend, per, ab, is_trial):
    """手機「要你處理」裡跟口數有關的那幾則 ⇒ [{lvl, text, act}]（lvl：now／decide／later）。"""
    out = []
    if offer and not pend:
        to = offer["to"]
        out.append({"lvl": "decide",
                    "text": ("可以先試車 %s 了（日盤 %d 口、夜盤 %d 口）" if is_trial else "可以升到 %s 了（日盤 %d 口、夜盤 %d 口）")
                    % (to["label"], to["a"], to["b"]),
                    "act": "想升就到電腦面板按確認；不按就維持 %s" % label(ab)})
    for lane in LANES:
        p = per.get(lane) or {}
        if p.get("text") and (p.get("state") == "retire" or p.get("cap") == 0):
            who = "日盤" if lane == "union" else "夜盤"
            out.append({"lvl": "decide",
                        "text": "%s表現掉到退休線，%s" % (who, "已停掉不送" if RETIRE_ZERO else "已自動壓到 1 口"),
                        "act": "要不要關掉，到電腦面板決定"})
    return out


def goal_overlay(G, P):
    """
    ⭐ 【帳戶】目標卡：權益夠了 ≠ 已經升級（goal.view 只看權益）⇒ 用口數的正本改三樣：
      ① pos 那句（現在在跑哪一級；權益夠了但沒升要講卡在哪）
      ② 階梯每一列的狀態（run／wait／blocked／pending／空），now ＝ **實際在跑**的那一級（還沒到 7 萬標在 7 萬那列）
      ③ lots_line（#lotsline 那一行）
    ⛔ 不改 goal.py 的計畫線算法；P 是 panel() 那一份。⛔ P 讀不到 ⇒ 原封不動回 G。
    """
    if not isinstance(G, dict) or not G.get("ok") or not isinstance(P, dict) or not P.get("run"):
        return G
    G = dict(G)
    run = P.get("run") or {}
    lv = run.get("lvl") or 0
    lvrow = max(1, lv)
    offer, pend, nxt = P.get("offer"), P.get("pending"), P.get("next")
    eq = G.get("eq")
    steps = []
    for s in G.get("steps") or []:
        s = dict(s)
        j = s.get("lvl")
        state, txt = "", ""
        if j == lvrow:
            state, txt = "run", "現在在跑"
        elif pend and j == pend["to"]["lvl"]:
            state, txt = "pending", "已確認・%s 起" % dtxt(pend["eff"], False)
        elif offer and j == offer["to"]["lvl"]:
            state, txt = "wait", "權益到了・等你確認"
        elif nxt and j == nxt["lvl"] and _num(eq) and eq >= s.get("eq", float("inf")):
            state, txt = "blocked", "權益到了・條件沒過"
        s.update(state=state, state_txt=txt, now=(j == lvrow))
        steps.append(s)
    G["steps"] = steps
    G["lvl"] = lv
    G["lots"] = run.get("label")
    tier = ("在跑起始級 %s" % run.get("label")) if lv <= 1 else ("在跑第 %d 級（%s 口）" % (lv, run.get("label")))
    if nxt and _num(eq):
        if eq >= nxt["eq"]:
            if pend:
                more = "你已確認，%s 起升級。" % dtxt(pend["eff"], False)
            elif offer:
                more = "等你到【自動下單】確認。"
            else:
                bad = [c["name"] for c in (P.get("conds") or []) if c.get("ok") is False and c.get("k") != "eq"]
                tri = P.get("trial") or {}
                if tri.get("status") == "on":
                    more = "但要先跑完試車（%d／%d 筆）。" % (tri.get("n") or 0, tri.get("need") or TRIAL_N)
                else:
                    more = "但卡在%s，%s再檢查。" % ("、".join("「%s」" % x for x in bad) or "條件",
                                               (P.get("check") or {}).get("next_txt") or "下一個交易日")
            G["pos"] = "現在 %s 元，%s。權益已經夠「%s・%s」，%s" % (_money(eq), tier, wan(nxt["eq"]), nxt["label"], more)
        else:
            G["pos"] = "現在 %s 元，%s。離下一級「%s・%s」還差 %s 元。" % (
                _money(eq), tier, wan(nxt["eq"]), nxt["label"], _money(nxt["eq"] - eq))
    elif _num(eq):
        G["pos"] = "現在 %s 元，%s，已經到最後一級 —— 50 萬以上要再用同一把尺往上算。" % (_money(eq), tier)
    G["lots_line"] = P.get("lots_line")
    return G
