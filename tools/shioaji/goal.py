# -*- coding: utf-8 -*-
"""
【帳戶】分頁的「目標」那張卡（2026-09-30 Benson 交辦）。

他要的：「多少錢然後幾口、每個月獲利多少、我現在在哪裡、大概幾月可以到哪些錢，讓我有一個目標可以看」。

⛔⛔ 這一支**純計算**：不問券商、不讀檔、不寫檔 —— 權益紀錄由 live_panel 讀好傳進來。
⛔⛔ CLAUDE.md 開頭那條鐵律（UI 不准有預測／期望值）⇒ 這裡**不猜會賺多少**，畫的是兩條「計畫線」：
   ・保守線 ＝ 只算每月存進去的錢、一毛都不賺也會到的日期（純算術，確定的）。
   ・目標線 ＝ 存錢 ＋ 研究打折後的每口每月獲利（這是**他定的目標**，畫面照實寫「不是猜測」）。
   實際權益落在兩條線的哪裡，他一眼就知道自己超前還是落後。
   （Benson 09-30 在兩條都畫／只畫目標／只畫保守 裡選了「兩條都畫」。）

數字出處（⛔ 改之前先讀原檔，別在這裡發明第二份）：
  ・階梯：trade-log/CLAUDE.md「加碼／減碼規則」09-30 積極版 ＝ tick-research/ops_risk_and_scaling_2026-09-29.md
  ・每口每月：tick-research/capital_ladder_out.txt（優勢打折到 65%、重抽樣中位數）
      1＋1 ＝ 2,641、2＋1 ＝ 4,022（＋1 聯軍 ≈ 1,380）、3＋2 − 3＋1 ＝ 1,288（＋1 跟勢 ≈ 1,290）
  ・停損失靈最壞一次：同一份，1＋1 ＝ 23,883，跟聯軍口數成正比（聯軍 13:43:30 才平那一次）
  ・每月存 1.3 萬：Benson 09-30 定的。
⚠️ 真實金額不進 repo：起點的權益一律從 equity/（gitignore）讀，這裡只寫起算**日期**。
"""
import math
from datetime import date, datetime

GOAL_START = "2026-09-30"          # 他定「每月存 1.3 萬」那天 ⇒ 兩條計畫線從這天的權益起算
MONTHLY_DEPOSIT = 13_000           # 每月存進去多少（Benson 09-30）
UNION_PER_LOT = 1_370              # 多方聯軍每口每月（打折後中位數，元）
TREND_PER_LOT = 1_290              # 夜盤跟勢每口每月（同上）
WORST_PER_UNION = 23_883           # 停損失靈歷史最壞一次，每口聯軍（元）
UPGRADE_GAP = 3                    # 升完一級至少幾個月不再升（加碼規則第 5 條）
MAX_MONTHS = 72

# (權益門檻, 聯軍口數, 跟勢口數, 一年內要補錢的機率%) —— 09-30 積極版（他選的）
LADDER = (
    (70_000, 1, 1, 4),
    (100_000, 2, 1, 15),
    (150_000, 3, 1, 12),
    (200_000, 4, 2, 15),
    (250_000, 5, 2, 12),
    (300_000, 6, 3, 15),
    (350_000, 7, 3, 13),
    (400_000, 8, 3, 12),
    (450_000, 9, 4, 13),
    (500_000, 10, 4, 12),
)
GOAL = LADDER[-1][0]


def monthly_of(a, b):
    return a * UNION_PER_LOT + b * TREND_PER_LOT


def level_of(eq):
    """權益夠到第幾級（0 ＝ 還沒到 7 萬）。"""
    k = 0
    for i, L in enumerate(LADDER):
        if eq >= L[0]:
            k = i + 1
    return k


def lots_of(k):
    """第 k 級的 (聯軍, 跟勢)。第 0 級 ＝ 現在程式寫死的 1＋1。"""
    return (1, 1) if k <= 0 else LADDER[k - 1][1:3]


def _add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    m += 1
    last = [31, 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(d.day, last))


def _ym(d):
    return "%d-%02d" % (d.year, d.month)


def plan(e0, d0, months=None):
    """
    兩條計畫線，一個月一點 ⇒ [{"m", "date", "safe", "target", "lvl"}]（第 0 點 ＝ 起點）。
    目標線照加碼規則升級：每月初檢查、一次一級、升完 UPGRADE_GAP 個月不再升；⛔ 不看最近賺不賺。
    """
    if months is None:
        months = min(MAX_MONTHS, max(1, math.ceil((GOAL - e0) / MONTHLY_DEPOSIT)))
    safe, tgt = float(e0), float(e0)
    lvl = level_of(e0)
    last_up = -UPGRADE_GAP
    out = [{"m": 0, "date": str(d0), "safe": round(safe), "target": round(tgt), "lvl": lvl}]
    for m in range(1, months + 1):
        # 月初檢查升級（第 0 級到第 1 級是同一種 1＋1，不佔冷卻期）
        if lvl < len(LADDER) and tgt >= LADDER[lvl][0] and (lvl == 0 or m - last_up >= UPGRADE_GAP):
            if lvl > 0:
                last_up = m
            lvl += 1
        a, b = lots_of(lvl)
        tgt += MONTHLY_DEPOSIT + monthly_of(a, b)
        safe += MONTHLY_DEPOSIT
        out.append({"m": m, "date": str(_add_months(d0, m)), "safe": round(safe),
                    "target": round(tgt), "lvl": lvl})
    return out


def _reach(pts, key, thr):
    for p in pts:
        if p[key] >= thr:
            return p["date"][:7]
    return None


def _money(v):
    return format(int(round(v)), ",")


def _pm(v):
    n = int(round(v))
    return ("+" if n > 0 else "") + format(n, ",")


def months_actual(rows, eq_now=None, dep_today=0.0, today=None):
    """
    每個月實際賺賠（⛔ 扣掉存入 —— 匯錢進去不是賺到）。
    月初基準 ＝ 上個月最後一列；那個月之前沒紀錄 ⇒ 用那個月第一列（那一列當天的出入金已經在權益裡，不再扣）。
    ⚠️ 面板沒開的日子沒有那一列 ⇒ 那天的出入金會漏掉（畫面照實寫「從 equity 紀錄算」）。
    """
    rs = [r for r in rows if isinstance(r.get("equity"), (int, float))]
    by = {}
    for r in rs:
        by.setdefault(str(r["date"])[:7], []).append(r)
    out, prev_end = [], None
    today = today or date.today()
    cur_ym = _ym(today)
    for ym in sorted(by):
        mine = by[ym]
        if prev_end is not None:
            base, dep_rows = prev_end, mine
        else:
            base, dep_rows = float(mine[0]["equity"]), mine[1:]
        dep = sum(float(r.get("deposit") or 0) for r in dep_rows)
        end = float(mine[-1]["equity"])
        live = False
        if ym == cur_ym and isinstance(eq_now, (int, float)):
            end, live = float(eq_now), True
            if not any(str(r["date"]) == str(today) for r in mine):
                dep += float(dep_today or 0)
        out.append({"ym": ym, "base": round(base), "end": round(end), "dep": round(dep),
                    "net": round(end - base - dep), "live": live, "from": str(mine[0]["date"])})
        prev_end = float(mine[-1]["equity"])
    if isinstance(eq_now, (int, float)) and cur_ym not in by and prev_end is not None:
        out.append({"ym": cur_ym, "base": round(prev_end), "end": round(eq_now),
                    "dep": round(float(dep_today or 0)),
                    "net": round(eq_now - prev_end - float(dep_today or 0)), "live": True,
                    "from": None})
    return out


def view(rows, eq_now, dep_today=0.0, now=None):
    """
    ⇒ 目標卡要的整份（句子全在這裡組好，⛔ 前端不自己算、不自己寫評語）。
    `rows`：equity/ 的每日紀錄（舊到新）；`eq_now`：券商現在的權益（問不到 ⇒ None，退回最後一列）。
    """
    now = now or datetime.now()
    today = now.date()
    rows = [r for r in (rows or []) if isinstance(r.get("equity"), (int, float))]
    if not isinstance(eq_now, (int, float)):
        eq_now = float(rows[-1]["equity"]) if rows else None
    if eq_now is None:
        return {"ok": False, "msg": "還不知道帳戶有多少錢（券商還沒問到、也沒有任何一天的紀錄）"}

    start = next((r for r in rows if str(r["date"]) >= GOAL_START), None)
    if start is not None:
        d0, e0 = date.fromisoformat(str(start["date"])), float(start["equity"])
    else:
        d0, e0 = today, float(eq_now)
    pts = plan(e0, d0)

    # 今天兩條線應該在哪（按天數在月點之間內插）
    days = (today - d0).days
    frac = max(0.0, days / 30.44)
    i = min(int(frac), len(pts) - 2) if len(pts) > 1 else 0
    t = frac - i
    safe_now = pts[i]["safe"] + (pts[i + 1]["safe"] - pts[i]["safe"]) * t if len(pts) > 1 else e0
    tgt_now = pts[i]["target"] + (pts[i + 1]["target"] - pts[i]["target"]) * t if len(pts) > 1 else e0

    k = level_of(eq_now)
    a, b = lots_of(k)
    if k < len(LADDER):
        nxt = LADDER[k]
        gap = nxt[0] - eq_now
        pos = "現在 %s 元，%s。離下一級「%s 萬・%d＋%d 口」還差 %s 元。" % (
            _money(eq_now),
            ("還沒到第一級（7 萬），程式照舊 1＋1" if k == 0
             else "在第 %d 級（%d＋%d 口）" % (k, a, b)),
            nxt[0] // 10000, nxt[1], nxt[2], _money(gap))
    else:
        gap = 0
        pos = "現在 %s 元，已經到最後一級（%d＋%d 口）—— 50 萬以上要再用同一把尺往上算。" % (
            _money(eq_now), a, b)

    diff = eq_now - tgt_now
    if days <= 0:
        vs = "今天是起算日：兩條計畫線都從 %s 元出發。" % _money(e0)
    else:
        vs = "照計畫今天應該在：保守線 %s、目標線 %s ⇒ 你現在%s目標線 %s 元，%s保守線。" % (
            _money(safe_now), _money(tgt_now),
            "超前" if diff >= 0 else "落後", _money(abs(diff)),
            "高於" if eq_now >= safe_now else "低於")

    # 起算後實際存了多少、交易賺賠多少（⛔ 只用紀錄裡的出入金）
    dep_since = sum(float(r.get("deposit") or 0) for r in rows if str(r["date"]) > str(d0))
    if not any(str(r["date"]) == str(today) for r in rows) and today > d0:
        dep_since += float(dep_today or 0)
    trade_since = eq_now - e0 - dep_since
    since = ("從 %s 起：實際存入 %s（計畫 %s）、交易賺賠 %s。" % (
        str(d0)[5:].replace("-", "/"), _money(dep_since),
        _money(MONTHLY_DEPOSIT * sum(1 for p in pts[1:] if p["date"] <= str(today))),
        _pm(trade_since))) if days > 0 else None

    steps = []
    for j, (thr, la, lb, p_topup) in enumerate(LADDER, start=1):
        hit = next((str(r["date"]) for r in rows if float(r["equity"]) >= thr), None)
        if hit is None and eq_now >= thr:
            hit = str(today)
        steps.append({
            "lvl": j, "eq": thr, "lots": "%d＋%d" % (la, lb),
            "monthly": monthly_of(la, lb),
            "topup": p_topup,
            "worst": WORST_PER_UNION * la,
            "safe_at": _reach(pts, "safe", thr),
            "target_at": _reach(pts, "target", thr),
            "done": hit, "now": (j == k),
        })

    mon = months_actual(rows, eq_now, dep_today, today)
    for mrow in mon:
        mrow["goal"] = monthly_of(*lots_of(level_of(mrow["base"])))

    return {
        "ok": True,
        "title": "目標：滾到 %s 萬（%d＋%d 口）" % (GOAL // 10000, LADDER[-1][1], LADDER[-1][2]),
        "start": str(d0), "start_eq": round(e0),
        "eq": round(eq_now), "lvl": k, "lots": "%d＋%d" % (a, b), "gap": round(gap),
        "next": (LADDER[k][0] if k < len(LADDER) else None),
        "prev": (LADDER[k - 1][0] if k > 0 else 0),
        "pos": pos, "vs": vs, "since": since,
        "safe_now": round(safe_now), "target_now": round(tgt_now),
        # 目標線到 50 萬那一點就收（再畫下去整張圖的刻度會被它撐爆）
        "line": [{"d": p["date"], "s": p["safe"],
                  "t": (p["target"] if (j == 0 or pts[j - 1]["target"] < GOAL) else None)}
                 for j, p in enumerate(pts)],
        "actual": [{"d": str(r["date"]), "e": round(float(r["equity"]))}
                   for r in rows if str(r["date"]) >= str(d0)],
        "steps": steps,
        "months": mon[-12:],
        "notes": [
            "保守線＝每月只存 %s、一毛都不賺也會到的日期（純算術）。" % _money(MONTHLY_DEPOSIT),
            "目標線＝存錢＋研究打折後的每月獲利（聯軍每口約 %s、跟勢每口約 %s），是你定的目標、不是猜測；"
            "安靜的月份會少很多，一年下來虧錢的機率約 17%%。" % (_money(UNION_PER_LOT), _money(TREND_PER_LOT)),
            "升級照你 09-30 定的規則：每月第一個交易日檢查、一次一級、升完 3 個月不再升；"
            "權益掉到這一級門檻的 85% 以下就退一級。⚠️ 下單程式現在還寫死 1 口，接近 10 萬時才改。",
            "「每月實際」從每天 14:00 記的權益算、已扣掉出入金；面板沒開的日子沒有紀錄，那天的出入金會漏掉。",
        ],
    }
