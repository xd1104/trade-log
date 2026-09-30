# -*- coding: utf-8 -*-
"""
【交易分析師】週報裡的**數字**全部在這裡算（⛔ 不讓 AI 算；AI 只讀這份）。

用法（排程的 Claude 每週六跑）：
  python analyst_facts.py                 ⇒ 算「剛結束那一週」（週六／週日跑）或「這一週」，寫 analyst/facts/<id>.json
  python analyst_facts.py --date 2026-09-26
  python analyst_facts.py perf trend 2025-01-29 2025-03-19 ...
                                          ⇒ 某條策略在指定那幾天（夜盤＝開盤那晚）的模擬成績，
                                            給「下週大事 ／ 過去同類日子」那一欄用（⛔ AI 不准自己估）
⛔ 唯讀：只讀 sim_lanes／autofire／nightfire／real_trades／real_orders／1 分 K，只寫 analyst/facts/。
"""
import argparse
import json
import math
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import analyst          # noqa: E402
import risk_cap         # noqa: E402
import sim_lanes        # noqa: E402

REAL_LANES = (("union", "day", "多方聯軍", "日盤・1 口"), ("trend", "night", "夜盤跟勢", "夜盤・1 口・2% 保護停損"))
# 前瞻考試的三條候選：從上線那天起才算（之前那些是回填的，⛔ 不算進 25 筆）
# 三條候選 09-23 17:40 上線 ⇒ 夜盤那兩條從 09-23 那晚起、留倉那條從 09-24 的日盤起才是「上線後才發生」的
# ⭐ 真正要判斷的是「跟現行真單**不一樣**的那幾筆」（一個月約 2 筆），⛔ 不是候選自己出手的總筆數
#    ⇒ 每條都記「對照的現行那條」，滿 25 筆看的是 diff_n。
# ⭐ 2026-09-30 聯軍留倉下架（＝抱多過夜的大盤漂移，tick-research/day_bracket_results_2026-09-30.md）⇒ 不再是候選。
# ⭐ 2026-09-30 夜盤跟勢只做多下架（做空那半有真本事，只做多＝賭多頭，tick-research/night_review_2026-09-30.md）⇒ 不再是候選。
CANDS = (("nunion", "夜盤聯軍", "2026-09-23", "trend"), ("tvol", "夜盤跟勢・只做大波動晚", "2026-09-30", "trend"))
CAND_NEED = 25
ERR_DAY = {"cant_enter", "order_failed", "late", "quote_stale", "no_quote", "unsure", "not_wired"}
ERR_NIGHT = {"cannot", "send_fail", "unsure", "late", "no_quote"}


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def target_week(d):
    """週六／週日跑 ⇒ 剛結束那一週；平日跑 ⇒ 這一週（到今天為止）。"""
    if d.weekday() >= 5:
        d = d - timedelta(days=d.weekday() - 4)
    return analyst.week_range(d)


def _sim_rows():
    rows, _st = sim_lanes.read_rows()
    out = {}
    for (lane, d), r in rows.items():
        out.setdefault(lane, []).append(r)
    for v in out.values():
        v.sort(key=lambda r: r["date"])
    return out


def _trade_pts(r):
    return float(r["points"]) if (r.get("decision") != "不做" and _num(r.get("points"))
                                   and r.get("entry") is not None) else None


def _real_week(mon, fri):
    """這一週自動單真單（日盤 d、夜盤 E 在週一～週五）⇒ [{d, sess, pts, entry, entry_time}]。"""
    out = []
    months = sorted({str(mon)[:7], str(fri)[:7]})
    for m in months:
        ents = [e for e in risk_cap.auto_entries(m) if str(mon) <= e["d"] <= str(fri)]
        y, mm = int(m[:4]), int(m[5:])
        d0 = date(y, mm, 1)
        d1 = date(y + (mm == 12), mm % 12 + 1, 1) + timedelta(days=risk_cap.NIGHT_EXIT_MAX_D)
        trades = risk_cap._trades_between(d0, d1, risk_cap._dirs()[2])
        used = set()
        for e in ents:
            t = risk_cap._match(e, trades, used)
            out.append({"d": e["d"], "sess": e["sess"], "entry": e["entry"], "entry_time": e["entry_time"],
                        "pts": (float(t["points"]) if t is not None and _num(t.get("points")) else None),
                        "state": "open" if t is None else ("ok" if _num(t.get("points")) else "unknown")})
    return out


def strategies(mon, fri, sim):
    try:
        import health
        hs = {s["key"]: s for s in health.strategies()["strategies"]}
    except Exception as e:
        hs, herr = {}, str(e)[:120]
    else:
        herr = None
    real = _real_week(mon, fri)
    out = []
    for lane, sess, name, sub in REAL_LANES:
        wk = [r for r in sim.get(lane, []) if str(mon) <= r["date"] <= str(fri)]
        sim_days = [{"d": r["date"], "decision": r.get("decision"), "pts": _trade_pts(r),
                     "why": (r.get("reason") or "")[:80]} for r in wk]
        rl = [x for x in real if x["sess"] == sess]
        # 真單 vs 模擬（同一天兩邊都有點數才比）
        pair = []
        simmap = {x["d"]: x for x in sim_days}
        for x in rl:
            s = simmap.get(x["d"])
            if s and s["pts"] is not None and x["pts"] is not None:
                pair.append({"d": x["d"], "real": x["pts"], "sim": s["pts"], "diff": round(x["pts"] - s["pts"], 1)})
        h = hs.get(lane) or {}
        out.append({"key": lane, "name": name, "sub": sub,
                    "week_sim_pts": round(sum(x["pts"] for x in sim_days if x["pts"] is not None), 1),
                    "week_sim_n": sum(1 for x in sim_days if x["pts"] is not None),
                    "week_real_pts": round(sum(x["pts"] for x in rl if x["pts"] is not None), 1),
                    "week_real_n": sum(1 for x in rl if x["pts"] is not None),
                    "sim_days": sim_days, "real": rl, "pairs": pair,
                    "lamp": h.get("lamp"), "lamp_word": h.get("lamp_word"),
                    "n_all": h.get("n"), "avg_all": h.get("avg_all"), "avg30": h.get("avg30"),
                    "avg15": h.get("avg15"), "recent": h.get("recent"), "health_err": herr})
    return out


# ⭐ 2026-09-29（Benson：盤整期週報要固定講）⛔ 界線與每月點數是研究的數字，照抄、不另算：
#    tick-research/regime_results_2026-09-26.md —— 2020-08~2026-07 每月「日盤平均日振幅%」三等分。
#    ⛔ 放在這裡、不放 health.py：【健檢】那一頁規定只給數字、不下判斷；判斷只出現在週報。
REGIME_LO, REGIME_HI = 1.03, 1.23          # 低於 1.03% ＝ 低波動；1.23% 以上 ＝ 高波動
REGIME_PTS = {"低": 141, "中": 330, "高": 1362}   # 那種月份兩條真單合計每月平均（1 口、點）
VOL_WIN = 20                                # ＝ health.VOL_WIN（日盤波動度那張卡的天數）


def regime(day20, as_of=None):
    """
    ⭐ 週報固定那一行：現在算不算低波動（盤整）期、這種月份過去兩條合計每月賺多少。
    ⛔ 用研究的固定界線（REGIME_LO／HI），⛔ 不用滾動百分位 —— 每月點數是照那把尺分出來的，換尺就對不上。
    ⛔ 只描述過去，不預測接下來。
    """
    if day20 is None or not math.isfinite(day20):
        return {"level": None, "line": "日盤波動度算不出來（資料不足），這週不判斷是不是盤整期。"}
    lv = "低" if day20 < REGIME_LO else ("高" if day20 >= REGIME_HI else "中")
    head = "目前日盤是%s波動期（近 %d 天平均日振幅 %.2f%%；研究的界線：低於 %.2f%% 算低、%.2f%% 以上算高）。" % (
        lv, VOL_WIN, day20, REGIME_LO, REGIME_HI)
    tail = {"低": "這種月份過去兩條真單合計平均每月約 +%s 點（1 口），比平常少，屬正常，不代表策略壞掉。",
            "中": "這種月份過去兩條真單合計平均每月約 +%s 點（1 口），屬正常水準。",
            "高": "這種月份過去兩條真單合計平均每月約 +%s 點（1 口）；⚠️ 高波動不會一直持續，別把這種月份當常態。"}[lv]
    return {"level": lv, "day20": round(float(day20), 2), "pts": REGIME_PTS[lv], "as_of": as_of,
            "line": head + tail % format(REGIME_PTS[lv], ",")}


def market():
    try:
        import health
        hm = health.market()
        cards = hm["market"]
    except Exception as e:
        return {"err": "市場狀態算不出來：%s" % str(e)[:120], "cards": [], "regime": None}
    keep = ("key", "title", "note", "value", "unit", "pct", "lo", "hi", "n_pool", "lines", "as_of",
            "flag_word", "flag_note")
    # ⭐ 2026-09-29：「現在是不是盤整期、這種月份通常賺多少」—— 程式算好的一句，週報固定顯示（⛔ 不靠 AI 決定寫不寫）
    dv = next((c for c in cards if c.get("key") == "day_vol"), {})
    return {"err": None, "cards": [{k: c.get(k) for k in keep} for c in cards],
            "regime": regime(dv.get("value"), as_of=dv.get("as_of"))}


def _jsonl(p):
    return risk_cap._jsonl(p) if p.exists() else []


def system(mon, fri):
    af, nf, _t = risk_cap._dirs()
    months = sorted({str(mon)[:7], str(fri)[:7]})
    day_rows = [o for m in months for o in _jsonl(af / ("%s.jsonl" % m))
                if str(mon) <= str(o.get("date", "")) <= str(fri)]
    night_rows = [o for m in months for o in _jsonl(nf / ("%s.jsonl" % m))
                  if str(mon) <= str(o.get("E", "")) <= str(fri)]
    sent_day = [o for o in day_rows if o.get("rec") == "result"]
    sent_night = [o for o in night_rows if o.get("rec") == "result"]
    errs = Counter()
    for o in day_rows:
        if o.get("rec") in ("skip", "result") and o.get("why") in ERR_DAY:
            errs["日盤：" + str(o.get("why_msg") or o.get("why"))[:60]] += 1
        if o.get("rec") == "eod" and o.get("alarm"):
            errs["日盤收盤平倉：" + str(o.get("why_msg") or "")[:60]] += 1
    for o in night_rows:
        if o.get("rec") in ("skip", "result") and (o.get("why") in ERR_NIGHT or (o.get("rec") == "result" and not o.get("ok"))):
            errs["夜盤：" + str(o.get("msg") or o.get("err") or o.get("why"))[:60]] += 1
    slips = [float(o["slip"]) for o in sent_day if o.get("ok") and _num(o.get("slip"))]
    for o in sent_night:
        if o.get("ok") and _num(o.get("entry")) and _num(o.get("px")):
            sg = 1 if o.get("dir") == "long" else -1
            slips.append(round((float(o["entry"]) - float(o["px"])) * sg, 1))
    nofill = 0
    od = HERE / "real_orders"
    d = mon
    while d <= fri + timedelta(days=1):
        for o in _jsonl(od / ("%s.jsonl" % d)):
            if str(o.get("kind", "")).endswith("nofill"):
                nofill += 1
        d += timedelta(days=1)
    skips = Counter(str(o.get("why")) for o in day_rows + night_rows if o.get("rec") == "skip")
    return {"sent_day": len(sent_day), "ok_day": sum(1 for o in sent_day if o.get("ok")),
            "sent_night": len(sent_night), "ok_night": sum(1 for o in sent_night if o.get("ok")),
            "slip_avg": round(sum(slips) / len(slips), 1) if slips else None, "slip_n": len(slips),
            "ioc_nofill": nofill, "problems": [{"what": k, "n": v} for k, v in errs.most_common()],
            "skip_reasons": dict(skips)}


def candidates(sim):
    out = []
    for lane, name, start, base in CANDS:
        bmap = {r["date"]: _trade_pts(r) for r in sim.get(base, [])}

        def diffs(rows):
            out2 = []
            for r in rows:
                a, b = _trade_pts(r), bmap.get(r["date"])
                if (a or 0.0) != (b or 0.0):
                    out2.append(round((a or 0.0) - (b or 0.0), 1))
            return out2
        rs = sim.get(lane, [])
        fwd = diffs([r for r in rs if r["date"] >= start])
        back = diffs([r for r in rs if r["date"] < start])
        out.append({"key": lane, "name": name, "base": sim_lanes.LANE_NAME.get(base, base),
                    "diff_n": len(fwd), "need": CAND_NEED,
                    "diff_avg": round(sum(fwd) / len(fwd), 1) if fwd else None,
                    "diff_sum": round(sum(fwd), 1),
                    "back_n": len(back), "back_avg": round(sum(back) / len(back), 1) if back else None,
                    "since": start})
    return out


def _third_wed(y, m):
    d = date(y, m, 15)
    while d.weekday() != 2:
        d += timedelta(days=1)
    return d


def next_week(fri):
    mon = fri + timedelta(days=3)
    days = [mon + timedelta(days=i) for i in range(5)]
    notes = []
    for d in days:
        if d == _third_wed(d.year, d.month):
            notes.append({"d": str(d), "what": "台指期結算日（第三個週三；日盤 13:30 收，收盤平倉提早）"})
    if days[0].month != days[-1].month:
        first = next(d for d in days if d.month == days[-1].month)
        notes.append({"d": str(first), "what": "換月：風控本月額度從這天起重新計算"})
    return {"from": str(days[0]), "to": str(days[-1]), "notes": notes}


def perf(lane, dates, sim=None):
    """某條策略在指定那幾天的模擬成績 ⇒ {n_days, n_trades, avg, sum, rows}。⛔ 沒資料的日子照實列出。"""
    sim = sim if sim is not None else _sim_rows()
    by = {r["date"]: r for r in sim.get(lane, [])}
    rows, pts = [], []
    for d in dates:
        r = by.get(d)
        p = _trade_pts(r) if r else None
        rows.append({"d": d, "have": r is not None, "decision": (r or {}).get("decision"), "pts": p})
        if p is not None:
            pts.append(p)
    first = min(by) if by else None
    return {"lane": lane, "name": sim_lanes.LANE_NAME.get(lane, lane), "data_from": first,
            "n_days": len(dates), "n_have": sum(1 for x in rows if x["have"]), "n_trades": len(pts),
            "avg": round(sum(pts) / len(pts), 1) if pts else None, "sum": round(sum(pts), 1),
            "rows": rows}


# ⭐ 2026-09-29 退休標準（Benson 看過草案說「接進週報」）。⛔ 門檻是研究算的，照抄、不在這裡另算：
#    tick-research/risk_trades.csv 六年回測 —— 多方聯軍最大回落 2,018、18 個月合計從沒負過；
#    夜盤跟勢最大回落 4,711（而且 2022 底~2024-05 連 18 個月合計 −3,399 之後大賺 ⇒ ⛔ 不能用「虧很久」判它）。
#    降級門檻＝歷史最壞 × 1.5。一律 1 口的點數（模擬本來就是 1 口）；口數變多時照口數放大。
#    ⛔ 只描述「現在在哪一級」，⛔ 不動任何開關 —— 降級／升回都是 Benson 自己按。
RETIRE = {
    "union": {"dd": 3000, "roll18": True, "hist_dd": 2018},
    "trend": {"dd": 7000, "roll18": False, "hist_dd": 4711},
}
RETIRE_WORD = {"ok": "正常", "watch": "觀察", "down": "降級", "retire": "退休", "back": "可升回"}
RETIRE_AFTER = 50          # 降級後模擬再跑幾筆才判退休／升回


def _months_back(d, n):
    y, m = int(d[:4]), int(d[5:7]) - n
    while m <= 0:
        y, m = y - 1, m + 12
    return "%04d-%02d-%s" % (y, m, d[8:10])


def retire(sim, regime_level=None):
    """每條真單現在在退休標準的哪一級 ⇒ [{key, name, level, word, why[], ...}]。資料＝模擬定論（2024-08 起、1 口）。"""
    out = []
    for lane, _sess, name, _sub in REAL_LANES:
        R = RETIRE.get(lane)
        pts = [(r["date"], p) for r in sim.get(lane, []) for p in [_trade_pts(r)] if p is not None]
        if not R or len(pts) < 30:
            out.append({"key": lane, "name": name, "level": None, "word": "資料不足",
                        "why": ["模擬定論不到 30 筆，先不判斷"]})
            continue
        # ⚠️ 「降級」一旦發生就一直算數，直到**創新高**為止 —— ⛔ 不可以因為回落又縮回門檻以內就顯示「正常」
        #    （那樣他會在模擬還沒跑滿 50 筆前就把真單開回去）。
        eq, peak, peak_d, dd, ep_start = 0.0, 0.0, pts[0][0], 0.0, None
        for i, (d, p) in enumerate(pts):
            eq += p
            if eq > peak:
                peak, peak_d, ep_start = eq, d, None
            dd = peak - eq
            if dd > R["dd"] and ep_start is None:
                ep_start = i                        # 這一段「回落超過門檻」從第幾筆開始
        last = pts[-1][0]
        r18 = sum(p for d, p in pts if d > _months_back(last, 18))
        span_ok = pts[0][0] <= _months_back(last, 18)
        avg_all = sum(p for _d, p in pts) / len(pts)
        avg30 = sum(p for _d, p in pts[-30:]) / 30
        why, level = [], "ok"
        red18 = R["roll18"] and span_ok and r18 < 0
        if ep_start is not None or red18:
            level = "down"
            if ep_start is not None:
                why.append("%s 從最高點回落超過降級門檻 %s 點（歷史最壞 %s 的 1.5 倍）；現在回落 %s 點，還沒創新高" % (
                    pts[ep_start][0][5:].replace("-", "/"), format(R["dd"], ","), format(R["hist_dd"], ","),
                    format(int(round(dd)), ",")))
            if red18:
                why.append("近 18 個月合計 %+.0f 點（六年回測從沒負過）" % r18)
            if ep_start is not None:
                after = [p for _d, p in pts[ep_start + 1:]]
                if len(after) >= RETIRE_AFTER:
                    a = sum(after) / len(after)
                    if a < 0:
                        level = "retire"
                        why.append("降級後模擬又跑了 %d 筆，平均 %+.1f 點，還是負的" % (len(after), a))
                    elif a >= avg_all / 2:
                        level = "back"
                        why.append("降級後模擬 %d 筆平均 %+.1f 點，回到歷史平均（%+.1f）的一半以上" % (len(after), a, avg_all))
                else:
                    why.append("降級後模擬已跑 %d／%d 筆，滿了才判斷退休或升回" % (len(after), RETIRE_AFTER))
        elif avg30 < 0:
            if regime_level == "低":
                why.append("近 30 筆平均 %+.1f 點，但現在是低波動期，賺得少屬正常，不列入觀察" % avg30)
            else:
                level = "watch"
                why.append("近 30 筆平均 %+.1f 點，變負了（先看，不動）" % avg30)
        if level == "ok" and not why:
            why.append("從最高點回落 %s 點（降級門檻 %s）；近 30 筆平均 %+.1f 點" % (
                format(int(round(dd)), ","), format(R["dd"], ","), avg30))
        out.append({"key": lane, "name": name, "level": level, "word": RETIRE_WORD[level], "why": why,
                    "dd": round(dd), "dd_thr": R["dd"], "peak_date": peak_d, "roll18": round(r18),
                    "avg30": round(avg30, 1), "avg_all": round(avg_all, 1), "n": len(pts), "as_of": last})
    return out


def build(d):
    mon, fri = target_week(d)
    sim = _sim_rows()
    mk = market()
    facts = {"id": analyst.week_id(mon), "range": "%s～%s" % (mon.strftime("%m/%d"), fri.strftime("%m/%d")),
             "week": {"mon": str(mon), "fri": str(fri)},
             "made_at": datetime.now().isoformat(timespec="seconds"),
             "strategies": strategies(mon, fri, sim), "market": mk, "system": system(mon, fri),
             "risk": risk_cap.state(qty=1), "candidates": candidates(sim), "next_week": next_week(fri),
             "retire": retire(sim, ((mk.get("regime") or {}).get("level")))}
    return facts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="build")
    ap.add_argument("rest", nargs="*")
    ap.add_argument("--date")
    a = ap.parse_args()
    if a.cmd == "perf":
        if len(a.rest) < 2:
            raise SystemExit("用法：analyst_facts.py perf <lane> <日期...>")
        print(json.dumps(perf(a.rest[0], a.rest[1:]), ensure_ascii=False, indent=1))
        return
    d = date.fromisoformat(a.date) if a.date else date.today()
    f = build(d)
    p = analyst.FACTS_DIR / (f["id"] + ".json")
    analyst._write_json(p, f)
    print("facts：%s（%s）" % (p, f["range"]))
    for s in f["strategies"]:
        print("  %s：本週模擬 %+.0f（%d 筆）／真單 %+.0f（%d 筆）；燈 %s" % (
            s["name"], s["week_sim_pts"], s["week_sim_n"], s["week_real_pts"], s["week_real_n"], s["lamp_word"]))
    r = f["risk"]
    print("  風控：%s" % r.get("msg"))


if __name__ == "__main__":
    main()
