# -*- coding: utf-8 -*-
"""
【每日判斷快照】（2026-11-04 Benson 交辦）—— ⛔ 只記錄，一張單都不送、一個開關都不碰。

起因：2026-11-02 才發現開箱的箱子寬度歷史（orb_hist.jsonl）漏記 8 天、多虧 738 點，
而畫面上當時完全看不出來。⇒ 每個交易日（日盤＋夜盤）不管有沒有下單，都落地一列
「當時看到什麼、為什麼做／不做」，讓真單 vs 模擬對帳、健檢燈號有依據，
**這類問題當天就看得出來**。

寫到 `decision_log/YYYY-MM.jsonl`（⛔ gitignore：含真實價格），一個 (日期, 時段) 一列、只 append。

  日盤一列（sess="day"）：09:32 以後寫（開箱突破截止 09:30 ＋ 逐筆落地延遲）
    ・三個候選（快攻／開箱／純回馬）各自：實際值、門檻、差多少、做／不做＋理由
    ・做法（AUTO_ORDERS_ON 的內容，⛔ 只讀）、口數、收盤平倉結果（有的話）
    ・fast_hist／orb_hist：門檻、用了幾天、**窗口裡漏了哪幾天**
    ・資料缺口：逐筆檔 08:45~09:30 有沒有洞、09:03:30 那一刻報價多舊
  夜盤一列（sess="night"）：判斷時刻＋5 分鐘以後寫
    ・跟勢 R／台積電 T：走幅、門檻、差多少、波動預測、口數、做／不做＋理由

⛔⛔ 鐵律
  ・**完全不在送單路徑上**：自己一條 daemon 執行緒，只讀帳本與歷史檔（auto_fire／night_fire
    已經落地的東西），⛔ 不呼叫任何會送單、會改狀態的函式，⛔ 不拿 state_lock。
  ・每一個區塊各自 try：一塊算不出來只在那一列寫 `errs`，其餘照寫；整輪壞掉只印一行。
  ・讀逐筆檔時每 2 萬列讓出一次 GIL（停損活在面板的 4Hz 主迴圈裡，⛔ 不可以被這裡塞住）。
  ・面板晚開／當天沒跑到 ⇒ 開機後補最近 CATCHUP_DAYS 天「帳本有紀錄、快照卻沒有」的那幾天，
    標 `src:"catchup"`（那幾天的口數／做法是補寫當下的，不一定是當時的 ⇒ 另標 `now_*`）。
"""
import json
import re
import threading
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG_DIR = HERE / "decision_log"
TICK_DIR = HERE / "tick_logs"

DAY_WRITE_AT = (9, 32, 0)        # 日盤那一列幾點以後寫
NIGHT_DELAY_S = 300              # 夜盤判斷時刻之後幾秒才寫
CATCHUP_DAYS = 10                # 開機時往回補幾天
POLL_S = 60
TICK_HOLE_S = 30                 # 逐筆相鄰兩筆成交超過幾秒算一個洞（跟【細節】的缺口門檻同一個數）
TICK_WIN = ("08:45:00", "09:30:00")
HIST_LOOK = 25                   # 檢查 fast_hist／orb_hist 漏記時往回看幾個交易日
_MONTH_RE = re.compile(r"^\d{4}-\d{2}\.jsonl$")

_ST = {"started": False, "errors": 0, "last_err": None, "written": 0, "last_write": None}
_LOCK = threading.Lock()

CAND_NAME = {"fast": "快攻", "orb": "開箱", "rev": "純回馬"}


def _num(x):
    try:
        if isinstance(x, bool) or x is None:
            return None
        v = float(x)
        return v if v == v and abs(v) != float("inf") else None
    except Exception:
        return None


def _r(x, n=4):
    v = _num(x)
    return None if v is None else round(v, n)


# ── 落地 ─────────────────────────────────────────────────────────────
def _path(d):
    return LOG_DIR / (str(d)[:7] + ".jsonl")


def keys_written(months=None):
    """已經寫過的 (date, sess)。⛔ 壞列跳過（不算寫過 ⇒ 會重寫一列，寧可多一列也不安靜地少）。"""
    out = set()
    if not LOG_DIR.exists():
        return out
    for p in sorted(LOG_DIR.iterdir()):
        if not _MONTH_RE.match(p.name) or (months and p.name[:7] not in months):
            continue
        try:
            for ln in p.read_text(encoding="utf-8").splitlines():
                try:
                    o = json.loads(ln)
                    out.add((o["date"], o["sess"]))
                except Exception:
                    continue
        except Exception:
            continue
    return out


def append(row):
    with _LOCK:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with _path(row["date"]).open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    _ST["written"] += 1
    _ST["last_write"] = "%s %s" % (row["date"], row["sess"])
    return row


# ── 資料缺口 ─────────────────────────────────────────────────────────
def _sec(hms):
    h, m, s = hms[:8].split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def tick_holes(path, win=TICK_WIN, hole_s=TICK_HOLE_S):
    """
    逐筆檔 08:45~09:30 的成交有沒有洞。⇒ {"exists","n","first","last","holes":[[起,迄,秒]],"marks"}
    ⚠️ 只看成交列（k=t），用字串切片不用 json（一早上十萬列，json 太慢）；每 2 萬列讓出 GIL。
    `marks`＝痕跡列（k=x：丟棄／日期亂跳）有幾列 ⇒ >0 就是有東西沒錄到。
    """
    out = {"exists": False, "n": 0, "first": None, "last": None, "holes": [], "marks": 0}
    p = Path(path)
    if not p.exists():
        return out
    out["exists"] = True
    a, b = _sec(win[0]), _sec(win[1])
    prev = None
    with p.open("r", encoding="utf-8", errors="replace") as f:
        for i, ln in enumerate(f):
            if i % 20000 == 0:
                time.sleep(0)
            if ln.startswith('{"k":"x"'):
                out["marks"] += 1
                continue
            if not ln.startswith('{"k":"t","t":"'):
                continue
            t = ln[14:26]
            try:
                s = _sec(t)
            except Exception:
                continue
            if not (a <= s < b):
                continue
            out["n"] += 1
            if out["first"] is None:
                out["first"] = t
            out["last"] = t
            if prev is not None and s - prev[0] > hole_s:
                out["holes"].append([prev[1], t, round(s - prev[0], 1)])
            prev = (s, t)
    # 頭尾也算洞：開盤後 30 秒還沒有第一筆／收手前 30 秒以前就沒了
    if out["first"] is not None:
        if _sec(out["first"]) - a > hole_s:
            out["holes"].insert(0, [win[0], out["first"], round(_sec(out["first"]) - a, 1)])
        if b - _sec(out["last"]) > hole_s:
            out["holes"].append([out["last"], win[1], round(b - _sec(out["last"]), 1)])
    return out


def hist_gaps(d, fast_dates, orb_dates, tick_dates=(), look=HIST_LOOK):
    """
    ⭐ 11-02 那個坑的機器判準：d 以前最近 `look` 個交易日裡，**fast_hist 有、orb_hist 沒有**的日子
    （fast_hist 不管開關開不開每天都寫 ⇒ 拿它當交易日曆）；以及逐筆檔有、fast_hist 卻沒有的日子。
    """
    cal = sorted({x for x in set(fast_dates) | set(tick_dates) if x < str(d)})[-look:]
    fs, os_ = set(fast_dates), set(orb_dates)
    return {"look": len(cal), "from": cal[0] if cal else None,
            "orb_missing": [x for x in cal if x in fs and x not in os_],
            "fast_missing": [x for x in cal if x not in fs]}


# ── 日盤一列 ─────────────────────────────────────────────────────────
def _did(c):
    """那個候選做了沒：(做/不做/送單中斷, 理由)。"""
    if not c:
        return None, None
    rec, why = c.get("rec"), c.get("why")
    if rec == "result":
        return ("做" if c.get("ok") else "送單失敗"), c.get("why_msg") or c.get("err") or why
    if rec == "fire":
        return "送單中斷", c.get("why_msg") or why
    return "不做", c.get("why_msg") or c.get("msg") or why


def leg_fast(c, hist_v=None):
    """快攻：走幅 % 對門檻 %。c＝帳本那一列（可能是 None）；hist_v＝fast_verdict(歷史檔) 的結果。"""
    f = (c or {}).get("fast") if isinstance((c or {}).get("fast"), dict) else None
    src = "ledger" if f else ("hist" if hist_v else None)
    f = f or hist_v or {}
    mv, thr = _num(f.get("move_pct")), _num(f.get("thr_pct"))
    mp, tp = _num(f.get("move_pts")), _num(f.get("thr_pts"))
    did, why = _did(c)
    return {"src": src, "verdict": f.get("verdict"), "move_pct": _r(mv), "thr_pct": _r(thr),
            "diff_pct": _r(mv - thr) if mv is not None and thr is not None else None,
            "move_pts": mp, "thr_pts": tp,
            "diff_pts": (mp - tp) if mp is not None and tp is not None else None,
            "n": f.get("n"), "px": (c or {}).get("px"),
            "quote_age_ms": (c or {}).get("quote_age_ms"), "quote_gaps": (c or {}).get("quote_gaps"),
            "did": did, "why": (c or {}).get("why"), "msg": why}


def leg_orb(c, hist_row=None, thr=None):
    """開箱：箱寬 % 對過去中位數 %，加上突破那一刻。"""
    c = c or {}
    bp = _num(c.get("box_pct")) if c else None
    med = _num(c.get("box_med_pct")) if c else None
    src = "ledger" if bp is not None else None
    if bp is None and hist_row:
        bp, src = _num(hist_row.get("box_pct")), "hist"
    if med is None and thr:
        med = _num(thr.get("med"))
    fill = _num(c.get("px")) or _num((hist_row or {}).get("fill"))
    diff = bp - med if bp is not None and med is not None else None
    did, why = _did(c if c else None)
    oh = c.get("orb_hist") if isinstance(c.get("orb_hist"), dict) else {}
    return {"src": src, "box_hi": c.get("box_hi", (hist_row or {}).get("hi")),
            "box_lo": c.get("box_lo", (hist_row or {}).get("lo")),
            "box_w": c.get("box_w", (hist_row or {}).get("w")),
            "box_pct": _r(bp), "med_pct": _r(med), "diff_pct": _r(diff),
            "diff_pts": int(round(diff * fill / 100.0)) if diff is not None and fill else None,
            "wide_enough": None if diff is None else diff >= 0,
            "n": oh.get("n", (thr or {}).get("n")), "span": oh.get("span", (thr or {}).get("span")),
            "break_at": c.get("at"), "break_px": c.get("break_px"), "dir": c.get("d"),
            "tick_bad": c.get("tick_bad"), "tick_drops": c.get("tick_drops"),
            "did": did, "why": c.get("why"), "msg": why}


def leg_rev(c):
    """純回馬：09:15 的價對 09:03:30 的價（反轉了沒）。"""
    c = c or {}
    a, b = _num(c.get("px_0903")), _num(c.get("p15"))
    did, why = _did(c if c else None)
    return {"px_0903": a, "p15": b, "d_0903": c.get("d_0903"), "d2": c.get("d2"),
            "move_pts": (b - a) if a is not None and b is not None else None,
            "reversed": None if not c or a is None or b is None else c.get("d2") is not None,
            "did": did, "why": c.get("why"), "msg": why}


def day_row(d, ledger_day, fast_rows, orb_rows, tick=None, now_info=None, src="live"):
    """
    組日盤那一列。⛔ 純函式（測試直接餵假數字）。
      ledger_day  auto_fire.read_all() 裡那一天（None ⇒ 那天帳本沒有任何一列）
      fast_rows   auto_fire.hist_read() 的 rows；orb_rows ＝ orb_hist_read() 的 rows
      tick        tick_holes() 的結果
      now_info    {"method","arm_raw","qty","risk"}：寫這一列當下讀到的（補寫時跟當天不一定一樣）
    """
    import auto_fire as AF          # 只用它的純函式（判斷正本，⛔ 不准第二把尺）
    d = str(d)
    errs = []
    L = ledger_day or {}
    cands = {c.get("cand"): c for c in (L.get("cand_rows") or []) if isinstance(c, dict)}
    dayc = cands.get("day")
    out = {"date": d, "sess": "day", "src": src,
           "wrote_at": datetime.now().isoformat(timespec="seconds"),
           "ledger": bool(L), "method": None, "arm_raw": None, "qty": None,
           "decision": None, "legs": {}, "hist": {}, "gaps": {}, "eod": None, "errs": errs}
    # 做法：帳本那一列寫的才是當天真的用的（⛔ 不用現在的開關內容冒充）
    for c in list(cands.values()):
        out["method"] = out["method"] or c.get("method")
        out["arm_raw"] = out["arm_raw"] or c.get("arm_raw")
        if c.get("rec") in ("result", "fire") and c.get("qty") is not None:
            out["qty"] = c.get("qty")
    if dayc:
        out["day_skip"] = {"why": dayc.get("why"), "msg": dayc.get("why_msg")}
    mine_f = next((r for r in fast_rows or [] if r.get("date") == d), None)
    try:
        hv = AF.fast_verdict(d, mine_f["move_pct"] if mine_f else None, fast_rows)
        if mine_f and hv.get("thr_pct") is not None:
            hv["move_pts"] = AF.approx_points(hv.get("move_pct"), mine_f.get("ref"))
            hv["thr_pts"] = AF.approx_points(hv.get("thr_pct"), mine_f.get("ref"))
    except Exception as e:                       # noqa: BLE001
        hv = None
        errs.append("fast_verdict: %s" % str(e)[:80])
    try:
        out["legs"]["fast"] = leg_fast(cands.get("fast"), hv)
    except Exception as e:                       # noqa: BLE001
        errs.append("fast: %s" % str(e)[:80])
    try:
        othr = AF.orb_threshold(orb_rows, d)
        mine_o = next((r for r in orb_rows or [] if r.get("date") == d), None)
        out["legs"]["orb"] = leg_orb(cands.get("orb"), mine_o, othr)
        out["hist"]["orb"] = {"n": othr.get("n"), "med_pct": _r(othr.get("med")),
                              "span": othr.get("span"), "why": othr.get("why"),
                              "today_written": mine_o is not None}
    except Exception as e:                       # noqa: BLE001
        errs.append("orb: %s" % str(e)[:80])
    try:
        out["legs"]["rev"] = leg_rev(cands.get("rev"))
    except Exception as e:                       # noqa: BLE001
        errs.append("rev: %s" % str(e)[:80])
    if hv is not None:
        out["hist"]["fast"] = {"n": hv.get("n"), "thr_pct": _r(hv.get("thr_pct")),
                               "today_written": mine_f is not None}
    try:
        g = hist_gaps(d, [r["date"] for r in fast_rows or []], [r["date"] for r in orb_rows or []],
                      tick_dates=(now_info or {}).get("tick_dates") or ())
        out["hist"]["gaps"] = g
    except Exception as e:                       # noqa: BLE001
        errs.append("hist_gaps: %s" % str(e)[:80])
    # 今天的結論：有一個候選做了 ⇒ 做；否則不做（理由＝整天那一列，或三個候選各自的）
    done = [k for k, v in out["legs"].items() if v.get("did") in ("做", "送單失敗", "送單中斷")]
    out["decision"] = {"did": out["legs"][done[0]]["did"] if done else ("不做" if L else "沒有紀錄"),
                       "by": done[0] if done else None,
                       "why": (out.get("day_skip") or {}).get("msg") if not done else None}
    if not L:
        out["decision"]["why"] = "那天自動下單的帳本一列都沒有（面板沒開、或還沒到判斷時刻）"
    e = L.get("eod") if isinstance(L.get("eod"), dict) else None
    if e:
        out["eod"] = {"why": e.get("why"), "msg": e.get("why_msg"), "at": e.get("at")}
    # 資料缺口
    if tick is not None:
        out["gaps"]["tick"] = tick
    fr = cands.get("fast") or {}
    out["gaps"]["quote_age_ms"] = fr.get("quote_age_ms")
    out["gaps"]["quote_gaps_s"] = fr.get("quote_gaps")
    flags = []
    if tick is not None:
        if not tick.get("exists"):
            flags.append("沒有逐筆檔")
        elif tick.get("holes"):
            flags.append("逐筆有 %d 個洞" % len(tick["holes"]))
        if tick.get("marks"):
            flags.append("逐筆有 %d 列痕跡（丟棄／日期亂跳）" % tick["marks"])
    if _num(fr.get("quote_age_ms")) is not None and fr["quote_age_ms"] > 5000:
        flags.append("09:03:30 報價已經 %.1f 秒沒更新" % (fr["quote_age_ms"] / 1000.0))
    if _num(fr.get("quote_gaps")):
        flags.append("早上有 %.0f 秒沒報價" % fr["quote_gaps"])
    g = out["hist"].get("gaps") or {}
    if g.get("orb_missing"):
        flags.append("箱子寬度歷史漏記 %d 天" % len(g["orb_missing"]))
    if g.get("fast_missing"):
        flags.append("走幅歷史漏記 %d 天" % len(g["fast_missing"]))
    if not (out["hist"].get("fast") or {}).get("today_written") and L:
        flags.append("今天的走幅沒有寫進歷史")
    if not (out["hist"].get("orb") or {}).get("today_written") and L:
        flags.append("今天的箱子寬度沒有寫進歷史")
    out["flags"] = flags
    if now_info:
        out["now_method"] = now_info.get("method")
        out["now_qty"] = now_info.get("qty")
        if out["qty"] is None and src == "live":
            out["qty"] = now_info.get("qty")
        if now_info.get("risk") is not None:
            out["risk"] = now_info.get("risk")
    return out


# ── 夜盤一列 ─────────────────────────────────────────────────────────
def night_row(E, rows, plan=None, ref=None, now_info=None, src="live"):
    """
    組夜盤那一列。⛔ 純函式。
      rows   night_fire.rows_of(E)（那一晚帳本的每一列）
      plan   lots.night_plan(E) 的結果（波動預測、口數；帳本沒寫時才用它）
      ref    帳本沒算走幅（關著／沒到那一步）時的參考值 {"mv","thr","n","newest"}，標 src=reference
    """
    E = str(E)
    rows = [r for r in rows or [] if isinstance(r, dict)]
    fin = [r for r in rows if r.get("rec") in ("skip", "result")]
    last = fin[-1] if fin else None
    sending = any(r.get("rec") == "sending" for r in rows)
    eod = next((r for r in rows[::-1] if r.get("rec") == "eod"), None)
    L = last or {}
    method = L.get("method") or (now_info or {}).get("method")
    mv, thr = _num(L.get("mv")), _num(L.get("thr"))
    vsrc = "ledger" if mv is not None else None
    if mv is None and ref:
        mv, thr, vsrc = _num(ref.get("mv")), _num(ref.get("thr")), "reference"
    unit = "pct" if method == "T" else "pts"
    if last is None:
        did, why = ("送單中斷" if sending else "沒有紀錄"), (
            "送出去之後沒有結果那一列（請自己到大戶投確認）" if sending
            else "那一晚夜盤自動下單的帳本一列都沒有（面板沒開、或還沒到判斷時刻）")
    elif L.get("rec") == "result":
        did, why = ("做" if L.get("ok") else "送單失敗"), L.get("err")
    else:
        did, why = "不做", L.get("msg") or L.get("why")
    p = plan or {}
    out = {"date": E, "sess": "night", "src": src,
           "wrote_at": datetime.now().isoformat(timespec="seconds"),
           "ledger": bool(rows), "method": method,
           "decision": {"did": did, "why": why, "code": L.get("why")},
           "signal": {"src": vsrc, "unit": unit, "mv": mv, "thr": thr,
                      "diff": round(abs(mv) - thr, 4) if mv is not None and thr is not None else None,
                      "dir": L.get("dir") or (None if mv is None else ("long" if mv > 0 else "short")),
                      "ref": L.get("ref"), "ref_at": L.get("ref_at"), "sig": L.get("sig"),
                      "sig_at": L.get("sig_at"), "newest": L.get("newest") or (ref or {}).get("newest"),
                      "n": (ref or {}).get("n")},
           "vol": {"pred": L.get("vol", p.get("vol")), "thr": L.get("vol_thr", p.get("thr")),
                   "high": L.get("vol_high", p.get("high")), "last": L.get("vol_last", p.get("last")),
                   "src": "ledger" if "vol" in L else ("plan" if plan else None)},
           "qty": L.get("qty") if L.get("rec") == "result" else p.get("qty"),
           "qty_sent": L.get("qty_sent"), "px": L.get("px"), "entry": L.get("entry"),
           "sl_points": L.get("sl_points", L.get("pts")),
           "eod": {"why": eod.get("why"), "msg": eod.get("msg"), "at": eod.get("at")} if eod else None,
           "errs": []}
    flags = []
    if L.get("why") == "no_quote":
        flags.append("判斷那一刻報價不新鮮（%s 秒）" % L.get("age"))
    if L.get("why") == "no_ref":
        flags.append("拿不到 30 分鐘前的價")
    if L.get("why") == "no_hist":
        flags.append("走幅歷史不夠或太舊")
    if sending and last is None:
        flags.append("送單中斷")
    out["flags"] = flags
    if now_info:
        out["now_method"] = now_info.get("method")
    return out


# ── 背景執行緒 ───────────────────────────────────────────────────────
def _day_due(now):
    return now.weekday() < 5 and (now.hour, now.minute, now.second) >= DAY_WRITE_AT


def _tick_dates():
    try:
        return sorted(p.name[:10] for p in TICK_DIR.glob("????-??-??.jsonl"))
    except Exception:
        return []


def day_ctx():
    """一輪只讀一次的東西（帳本、兩份歷史）。"""
    import auto_fire as AF
    ctx = {"errs": [], "ledger": [], "fast": [], "orb": [], "tick_dates": _tick_dates()}
    try:
        ctx["ledger"] = AF.read_all(limit_days=CATCHUP_DAYS + 5)[0]
    except Exception as e:                       # noqa: BLE001
        ctx["errs"].append("read_all: %s" % str(e)[:80])
    try:
        ctx["fast"] = AF.hist_read()[0]
    except Exception as e:                       # noqa: BLE001
        ctx["errs"].append("fast_hist: %s" % str(e)[:80])
    try:
        ctx["orb"] = AF.orb_hist_read()[0]
    except Exception as e:                       # noqa: BLE001
        ctx["errs"].append("orb_hist: %s" % str(e)[:80])
    return ctx


def build_day(d, src="live", ctx=None):
    import auto_fire as AF
    ctx = ctx or day_ctx()
    errs = list(ctx["errs"])
    L = next((r for r in ctx["ledger"] if r.get("date") == str(d)), None)
    fr, orr = ctx["fast"], ctx["orb"]
    try:
        tk = tick_holes(TICK_DIR / ("%s.jsonl" % d))
    except Exception as e:                       # noqa: BLE001
        tk = None
        errs.append("tick: %s" % str(e)[:80])
    ni = {"tick_dates": ctx["tick_dates"]}
    try:
        a = AF.arm()                              # ⛔ 只讀開關檔
        ni["method"] = a.get("method")
    except Exception as e:                       # noqa: BLE001
        errs.append("arm: %s" % str(e)[:80])
    try:
        ni["qty"] = AF._day_qty_view()
    except Exception as e:                       # noqa: BLE001
        errs.append("qty: %s" % str(e)[:80])
    try:
        import risk_cap
        rb, rmsg, _ = risk_cap.blocked(date.fromisoformat(str(d)))
        ni["risk"] = {"blocked": bool(rb), "msg": rmsg}
    except Exception as e:                       # noqa: BLE001
        errs.append("risk_cap: %s" % str(e)[:80])
    row = day_row(d, L, fr, orr, tk, ni, src=src)
    row["errs"] = errs + row["errs"]
    return row, L is not None


def build_night(E, src="live"):
    import night_fire as NF
    errs = []
    try:
        rows = NF.rows_of(E)
    except Exception as e:                       # noqa: BLE001
        rows = []
        errs.append("rows_of: %s" % str(e)[:80])
    plan = None
    try:
        import lots
        plan = lots.night_plan(E, lots.read_state()[0])
    except Exception as e:                       # noqa: BLE001
        errs.append("night_plan: %s" % str(e)[:80])
    ni = {}
    try:
        a = NF.arm()
        ni["method"] = a.get("method")
    except Exception as e:                       # noqa: BLE001
        errs.append("arm: %s" % str(e)[:80])
    ref = None
    fin = [r for r in rows if r.get("rec") in ("skip", "result") and r.get("mv") is not None]
    if not fin and src == "live":
        # 帳本沒算走幅（關著、或 T 沒走到）⇒ 用跟勢那一套算一份參考值（⛔ 標 reference，不是當時的判斷）
        try:
            import trend_rule
            past, newest = NF.past_sigs(E)
            p0, _ = NF.ref_price(E)
            p1, _ = NF.sig_price(E)
            if p0 is not None and p1 is not None:
                ref = {"mv": round(p1 - p0, 1), "newest": newest, "n": len(past),
                       "thr": round(trend_rule.threshold(past), 1)
                       if len(past) >= trend_rule.MIN_N else None}
        except Exception as e:                   # noqa: BLE001
            errs.append("reference: %s" % str(e)[:80])
    row = night_row(E, rows, plan, ref, ni, src=src)
    row["errs"] = errs + row["errs"]
    return row, bool(rows)


def step(now=None):
    """一輪：今天該寫的寫掉、補最近幾天漏的。⇒ 這一輪寫了幾列。"""
    now = now or datetime.now()
    have = keys_written()
    n = 0
    today = now.date()
    # 日盤
    ctx = None
    for k in range(CATCHUP_DAYS, -1, -1):
        d = today - timedelta(days=k)
        if d.weekday() >= 5 or (str(d), "day") in have:
            continue
        if k == 0 and not _day_due(now):
            continue
        ctx = ctx or day_ctx()
        row, has = build_day(d, src="live" if k == 0 else "catchup", ctx=ctx)
        if k == 0 or has:
            append(row)
            n += 1
    # 夜盤（E＝開盤那一晚）
    try:
        import night_fire as NF
        for k in range(CATCHUP_DAYS, -1, -1):
            E = today - timedelta(days=k)
            if E.weekday() >= 5 or (str(E), "night") in have:
                continue
            try:
                a = NF.arm()
                m = a.get("method") if a.get("on") else "R"
            except Exception:                    # noqa: BLE001
                m = "R"
            due = max(NF.decide_at(E, "R"), NF.decide_at(E, m)) + timedelta(seconds=NIGHT_DELAY_S)
            if now < due:
                continue
            live = now - due < timedelta(hours=3)
            row, has = build_night(E, src="live" if live else "catchup")
            if live or has:
                append(row)
                n += 1
    except Exception as e:                       # noqa: BLE001
        _err("night: %s" % e)
    return n


def _err(msg):
    _ST["errors"] += 1
    _ST["last_err"] = str(msg)[:200]
    if _ST["errors"] <= 3 or _ST["errors"] % 50 == 0:
        print("⚠️ [判斷快照] 出錯（送單與停損不受影響）：%s" % str(msg)[:200], flush=True)


def _loop():
    while True:
        try:
            step()
        except Exception as e:                   # noqa: BLE001
            _err("%s %s" % (e, traceback.format_exc(limit=2)[-200:]))
        time.sleep(POLL_S)


def start():
    if _ST["started"]:
        return
    _ST["started"] = True
    threading.Thread(target=_loop, name="decision-log", daemon=True).start()


def state():
    return dict(_ST)
