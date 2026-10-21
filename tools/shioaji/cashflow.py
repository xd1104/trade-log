# -*- coding: utf-8 -*-
"""
⭐ 2026-10-21【帳戶】金流明細（Benson：「帳戶那邊要有金流明細，實際的錢扣多少加多少」）。

⛔⛔ **唯讀、純函式**：只吃三份已經在硬碟上的紀錄，⛔ 不問券商、⛔ 不寫任何檔。
  ・equity/YYYY.jsonl   每天 14:00 一列（equity／deposit／settle_pl／fee／tax）—— **錢的正本**
  ・real_trades/D.jsonl 每一筆真單（每口點數、口數、進出場）—— 檔名＝出場那天
  ・autofire／nightfire 帳本 —— 只拿來對「這筆是哪一個策略送的」

一天的帳：
  當天淨額   ＝ 今天權益 − 前一天權益 − 當天出入金（出入金走 goal.eff_deposit：券商漏寫的也推得出來）
  交易金額   ＝ Σ 每口點數 × 口數 × 10（微台一點 10 元）
  手續費＋稅 ＝ 交易金額 − 當天淨額（⇒ **用券商的錢倒推**，⛔ 不用費率估）
  ⚠️ 倒推出來的費稅跟費率估的差超過 FEE_TOL ⇒ 那一天標「對不起來」＋差多少（⛔ 不靜靜吞掉）。
⛔ 週末那幾列券商會把上一個交易日的 settle_pl／fee 原封不動再給一次 ⇒ ⛔ 不拿 settle_pl 加總，只看權益差。
"""
import json
from datetime import date

PT_VALUE = 10                      # 微台一點 10 元
FEE_SIDE = 18                      # 手續費：單邊每口 18 元（他的費率）
TAX_RATE = 0.00002                 # 期交稅 十萬分之二（單邊，契約價值 ＝ 點數 × 10）
FEE_TOL = 30                       # 元。倒推的費稅跟費率估的差超過這個 ⇒ 標對不起來

CAND_NAME = {"fast": "快攻", "orb": "開箱", "rev": "純回馬"}
NIGHT_NAME = {"R": "跟勢", "T": "台積電快攻"}
WHY = {"tp": "停利", "sl": "停損", "eod": "收盤平", "night_eod": "04:58 平"}
WEEK = "一二三四五六日"


def _num(v):
    return float(v) if isinstance(v, (int, float)) else None


def est_fee(t):
    """費率估的這一筆來回手續費＋稅（只拿來判斷對不對得起來）。"""
    q = int(t.get("qty") or 1)
    tax = sum(round((_num(t.get(k)) or 0) * PT_VALUE * TAX_RATE) for k in ("entry", "exit"))
    return q * (2 * FEE_SIDE + tax)


def _is_night(t):
    return str(t.get("entry_time") or "") >= "15:00"


def strat_name(t, day_res, night_res):
    """這一筆是誰送的。⇒ (盤別, 名字)。對不到 ⇒ 名字 None（畫面寫「手動」）。"""
    e = _num(t.get("entry"))
    if _is_night(t):
        for r in night_res:
            if _num(r.get("entry")) == e:
                return "夜盤", NIGHT_NAME.get(r.get("method"), r.get("method"))
        return "夜盤", None
    for r in day_res.get(str(t.get("date")), []):
        if _num(r.get("entry")) == e:
            if r.get("method") == "A":
                return "日盤", "快攻回馬槍"
            return "日盤", CAND_NAME.get(r.get("cand"), r.get("cand"))
    return "日盤", None


def _night_label(t):
    """夜盤單算在出場那天，註明是哪一晚（出場在凌晨 ⇒ 前一個日曆日的晚上）。"""
    try:
        d = date.fromisoformat(str(t.get("date")))
    except ValueError:
        return None
    if str(t.get("exit_time") or "") < "15:00":
        d = date.fromordinal(d.toordinal() - 1)
    return "%02d-%02d 晚" % (d.month, d.day)


def build(rows, trades, day_res, night_res, eff_deposit, live=None, today=None):
    """
    ⇒ {"months": {"YYYY-MM": {sum…, "days": [...]}}, "keys": [...]}。
    rows：權益紀錄（照日期排）；trades：{date: [real_trades 列]}；day_res：{date: [autofire result 列]}；
    night_res：[nightfire result 列]；eff_deposit(prev, cur)：goal.eff_deposit；
    live：今天券商現在那一份（跟 rows 同欄位，可為 None）—— 有的話**取代**今天 14:00 那一列（比較新）。
    """
    rs = [r for r in rows if isinstance(r.get("equity"), (int, float))]
    if live and isinstance(live.get("equity"), (int, float)) and today:
        rs = [r for r in rs if str(r.get("date")) != str(today)] + [dict(live, date=str(today))]
    rs.sort(key=lambda r: str(r["date"]))
    days = []
    for i, r in enumerate(rs):
        if i == 0:
            continue                       # 第一列只當起點（那天之前的權益不知道）
        prev = rs[i - 1]
        dep = float(eff_deposit(prev, r) or 0)
        # 權益含未平倉損益 ⇒ 扣掉未平倉的變化（還沒平的部位不是金流；平倉那天才進明細）
        fl = (_num(r.get("float_pl")) or 0.0) - (_num(prev.get("float_pl")) or 0.0)
        net = round(float(r["equity"]) - float(prev["equity"]) - dep - fl)
        d = str(r["date"])
        items = []
        if dep:
            it = {"k": "dep", "amt": round(dep)}
            if not float(r.get("deposit") or 0):
                it["inferred"] = True      # 券商那欄寫 0、是用權益變化推出來的 ⇒ 畫面照實標「推算」
            items.append(it)
        tsum, est = 0, 0
        for t in sorted(trades.get(d, []), key=lambda x: str(x.get("entry_time") or "")):
            if not _is_night(t):
                continue
            items.append(_trade_item(t, day_res, night_res))
            tsum += items[-1]["amt"]
            est += est_fee(t)
        for t in sorted(trades.get(d, []), key=lambda x: str(x.get("entry_time") or "")):
            if _is_night(t):
                continue
            items.append(_trade_item(t, day_res, night_res))
            tsum += items[-1]["amt"]
            est += est_fee(t)
        fee = tsum - net                   # 倒推：交易 − 淨額 ＝ 付掉的費稅
        unexp = 0                          # 說不出來的錢（＋＝多出來、−＝不見了）
        if abs(fee - est) > FEE_TOL:
            # 對不起來 ⇒ 費稅照費率估，剩下的照實列成「差多少」（⛔ 不塞進費稅裡吞掉）
            fee = est
            unexp = round(net - (tsum - est))
        if fee:
            items.append({"k": "fee", "amt": -round(fee)})
        if not items and not unexp:
            continue                       # 什麼都沒發生的日子不列
        try:
            wd = WEEK[date.fromisoformat(d).weekday()]
        except ValueError:
            wd = ""
        days.append({"date": d, "wd": wd, "bal": round(float(r["equity"])), "items": items,
                     "dep": round(dep), "trade": round(tsum), "fee": -round(fee),
                     "diff": unexp or None, "start_bal": round(float(prev["equity"]))})
    months = {}
    for dd in days:
        ym = dd["date"][:7]
        M = months.setdefault(ym, {"dep": 0, "trade": 0, "fee": 0, "diff": 0, "days": []})
        M["dep"] += dd["dep"]
        M["trade"] += dd["trade"]
        M["fee"] += dd["fee"]
        M["diff"] += dd["diff"] or 0
        M["days"].append(dd)
    for ym, M in months.items():
        M["net"] = M["trade"] + M["fee"] + M["diff"]
        M["days"].sort(key=lambda x: x["date"], reverse=True)
    return {"months": months, "keys": sorted(months)}


def _trade_item(t, day_res, night_res):
    sess, name = strat_name(t, day_res, night_res)
    q = int(t.get("qty") or 1)
    pts = _num(t.get("points")) or 0
    return {"k": "trade", "sess": sess, "name": name or "手動",
            "dir": "做多" if t.get("dir") == "long" else ("做空" if t.get("dir") == "short" else ""),
            "pts": round(pts), "qty": q, "amt": round(pts * q * PT_VALUE),
            "entry": _num(t.get("entry")), "entry_time": str(t.get("entry_time") or "")[:8],
            "exit": _num(t.get("exit")), "exit_time": str(t.get("exit_time") or "")[:8],
            "why": WHY.get(t.get("reason"), t.get("reason") or ""),
            "night": _night_label(t) if sess == "夜盤" else None}


# ── 讀檔（面板呼叫；⛔ 任何一份讀壞只少那一份） ──────────────────────

def read_jsonl(p):
    out = []
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
    except OSError:
        pass
    return out


def load_trades(trade_dir):
    tr = {}
    for p in sorted(trade_dir.glob("*.jsonl")) if trade_dir.exists() else []:
        for t in read_jsonl(p):
            if t.get("date"):
                tr.setdefault(str(t["date"]), []).append(t)
    return tr


def load_day_res(fire_dir):
    out = {}
    for p in sorted(fire_dir.glob("20*.jsonl")) if fire_dir.exists() else []:
        for r in read_jsonl(p):
            if r.get("rec") == "result" and r.get("ok") and r.get("date"):
                out.setdefault(str(r["date"]), []).append(r)
    return out


def load_night_res(nf_dir):
    out = []
    for p in sorted(nf_dir.glob("20*.jsonl")) if nf_dir.exists() else []:
        out += [r for r in read_jsonl(p) if r.get("rec") == "result" and r.get("ok") is not False]
    return out
