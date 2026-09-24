# -*- coding: utf-8 -*-
"""
風控規則 B：**當月自動單真單虧到上限就整個月兩條都停**（2026-09-24 Benson 拍板）。

研究：`tick-research/risk_rules_results_2026-09-24.md`（兩條真單六年回測、1 口）——
  不設風控 最大回落 3,367／最慘一月 −1,498；上限 800 ⇒ 2,345／−1,114，幾乎不少賺。
  「從高點回落就暫停」那一類（C、E）反而更差（停在谷底、錯過反彈），⛔ 不要改成那種。

規則（Benson 2026-09-24 逐條確認）：
  ① 只算**自動下的真單**（autofire／nightfire 帳本裡 `result ok` 的那幾口），⛔ 他手動的單不算。
  ② 上限 ＝ `CAP_PER_LOT`（800 點）× 口數（`broker.QTY`）。點數 ＝ `real_trades` 的 points × qty。
  ③ 夜盤那一口算**開盤那晚（E）的月份**（7/31 晚上那筆算 7 月，雖然它 8/1 凌晨才平）。
  ④ 跨過上限的那一筆照算；之後當月剩下的日子**日盤、夜盤都不送**（模擬照常記）。
  ⑤ 下個月自動恢復。
  ⑥ **他可以手動解除**（面板兩段式按鈕 → `live_panel.risk_override_on()` 建 `RISK_OVERRIDE`，
     內容是那個月 `YYYY-MM`）。⛔ 解除只對寫在檔案裡的那個月有效 —— 下個月自動失效，不用記得收。

⛔⛔ 這個模組**只讀不寫**：不送單、不建任何開關檔、不碰 broker 的狀態。
   `RISK_OVERRIDE` 整個 repo 只有 `live_panel.risk_override_on()` 會建（同 AUTO_ORDERS_ON 的精神）。
⛔ **算不出來就擋**（讀檔炸掉之類）：風控本身壞掉時「不猜」＝ 這次不送，並把原因講出來。
   已經平倉但券商沒給出場價（points 是空的）⇒ ⛔ 不擋，算成「對不到點數」並寫在畫面上。
"""
import json
import pathlib
import sys
from datetime import date, timedelta

HERE = pathlib.Path(__file__).resolve().parent
FIRE_DIR = HERE / "autofire"            # ⛔ 唯讀（auto_fire 的帳本）
NF_DIR = HERE / "nightfire"             # ⛔ 唯讀（night_fire 的帳本）
TRADE_DIR = HERE / "real_trades"        # ⛔ 唯讀（broker 的成績單；檔名＝**出場**那天）
OVERRIDE_FLAG = HERE / "RISK_OVERRIDE"  # ⛔ gitignore；⛔ 這個模組不准建它
CAP_PER_LOT = 800.0                     # 每口每月上限（點）
# 有了這條規則之後，兩條真單 1 口在回測裡「從高點最多回落幾點」（risk_rules_results_2026-09-24.md 的 B 800）。
# ⛔ 只拿來算「每口建議本金」＝ 保證金 ＋ 這個數 × 2（未來可能比歷史更糟）× 每點金額。
DD_PER_LOT = 2345.0
PT_NTD = 10.0                           # 微台 1 點 ＝ 10 元（期交所契約規格）
NIGHT_EXIT_MAX_D = 4                    # 夜盤那一口最晚幾天後平（04:58 隔天平；週五晚上 ⇒ 週六）
PX_TOL = 0.51                           # 帳本與成績單的進場價對不對得上（兩邊都是券商的成交價）


def _month_of(d):
    return str(d)[:7]


def _jsonl(p):
    """讀一個 jsonl。⛔ 檔案不存在 ＝ 空的；讀檔失敗 ⇒ 丟出去（上層擋單）。壞列跳過。"""
    if not p.exists():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        try:
            o = json.loads(ln)
        except Exception:
            continue
        if isinstance(o, dict):
            out.append(o)
    return out


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _dirs():
    """
    ⭐ 帳本的位置**跟著那三個模組走**（auto_fire.FIRE_DIR／night_fire.NF_DIR／broker.TRADE_DIR）：
    測試把它們導到暫存資料夾時，這裡就跟著讀暫存資料夾 ⇒ ⛔ 測試不會讀到他真的帳本。
    （⛔ 不 import 它們 —— auto_fire／night_fire 會 import 這個檔，互相 import 會打結。）
    """
    af, nf, bk = (sys.modules.get(n) for n in ("auto_fire", "night_fire", "broker"))
    return (getattr(af, "FIRE_DIR", FIRE_DIR), getattr(nf, "NF_DIR", NF_DIR),
            getattr(bk, "TRADE_DIR", TRADE_DIR))


def auto_entries(month, fire_dir=None, nf_dir=None):
    """這個月自動下單真的開出去的每一口 ⇒ [{sess, d, entry_time, entry}]。"""
    dfire, dnf, _t = _dirs()
    fire_dir, nf_dir = fire_dir or dfire, nf_dir or dnf
    out = []
    for o in _jsonl(fire_dir / ("%s.jsonl" % month)):
        if o.get("rec") == "result" and o.get("ok") and _num(o.get("entry")) \
                and str(o.get("date", ""))[:7] == month:
            out.append({"sess": "day", "d": str(o["date"]),
                        "entry_time": str(o.get("entry_time") or ""), "entry": float(o["entry"])})
    for o in _jsonl(nf_dir / ("%s.jsonl" % month)):
        if o.get("rec") == "result" and o.get("ok") and _num(o.get("entry")) \
                and str(o.get("E", ""))[:7] == month:
            out.append({"sess": "night", "d": str(o["E"]),
                        "entry_time": str(o.get("entry_time") or ""), "entry": float(o["entry"])})
    out.sort(key=lambda x: (x["d"], x["sess"] == "night"))
    return out


def _trades_between(d0, d1, trade_dir):
    """成績單 d0 ~ d1（含）每一列，帶上檔名那天（＝出場日）。"""
    out = []
    d = d0
    while d <= d1:
        for o in _jsonl(trade_dir / ("%s.jsonl" % d)):
            out.append(dict(o, _file=str(d)))
        d += timedelta(days=1)
    return out


def _match(ent, trades, used):
    """帳本那一口 ⇒ 成績單那一列（進場時間一樣、進場價差 ≤ PX_TOL、出場日在合理範圍）。"""
    d = date.fromisoformat(ent["d"])
    lo, hi = (d, d) if ent["sess"] == "day" else (d, d + timedelta(days=NIGHT_EXIT_MAX_D))
    for i, t in enumerate(trades):
        if i in used:
            continue
        f = date.fromisoformat(t["_file"])
        if not (lo <= f <= hi):
            continue
        if str(t.get("entry_time") or "") != ent["entry_time"]:
            continue
        if not _num(t.get("entry")) or abs(float(t["entry"]) - ent["entry"]) > PX_TOL:
            continue
        used.add(i)
        return t
    return None


def override_month(flag=None):
    """解除檔寫的是哪個月（`YYYY-MM`）；沒有／看不懂 ⇒ None。"""
    flag = flag or OVERRIDE_FLAG
    try:
        if not flag.exists():
            return None
        txt = flag.read_bytes()[:32].decode("ascii", "replace").strip()
    except OSError:
        return None
    if len(txt) == 7 and txt[4] == "-" and txt[:4].isdigit() and txt[5:].isdigit():
        return txt
    return None


def state(month=None, qty=1, today=None, fire_dir=None, nf_dir=None, trade_dir=None, flag=None):
    """
    ⇒ 這個月的風控狀態（面板、手機、送單前都用這一份）。⛔ 讀檔失敗 ⇒ `err` 有值、`blocked=True`。
    """
    today = today or date.today()
    month = month or _month_of(today)
    cap = CAP_PER_LOT * max(int(qty or 1), 1)
    base = {"month": month, "cap": cap, "qty": int(qty or 1), "cap_per_lot": CAP_PER_LOT,
            "pnl": 0.0, "n": 0, "open": 0, "unknown": 0, "trades": [],
            "hit": False, "override": False, "blocked": False, "err": None}
    try:
        ents = auto_entries(month, fire_dir, nf_dir)
        y, m = int(month[:4]), int(month[5:])
        d0 = date(y, m, 1)
        d1 = (date(y + (m == 12), m % 12 + 1, 1) + timedelta(days=NIGHT_EXIT_MAX_D))
        trades = _trades_between(d0, d1, trade_dir or _dirs()[2])
        used = set()
        pnl = 0.0
        for e in ents:
            t = _match(e, trades, used)
            if t is None:
                base["open"] += 1               # 還沒平（或成績單還沒寫）
                continue
            if not _num(t.get("points")):
                base["unknown"] += 1            # 平了但沒有出場價 ⇒ 照實說，⛔ 不猜
                continue
            q = t.get("qty") if _num(t.get("qty")) and t.get("qty") > 0 else 1
            p = float(t["points"]) * float(q)
            pnl += p
            base["trades"].append({"d": e["d"], "sess": e["sess"], "pts": round(p, 1)})
        base["pnl"] = round(pnl, 1)
        base["n"] = len(base["trades"])
    except Exception as ex:
        base.update(err="算不出本月自動單損益：%s" % str(ex)[:120], blocked=True,
                    msg="風控算不出本月損益 —— 不猜，這次不送（%s）" % str(ex)[:80])
        return base
    base["hit"] = base["pnl"] <= -cap
    base["override"] = override_month(flag) == month
    base["blocked"] = base["hit"] and not base["override"]
    base["used_pct"] = max(0, min(100, int(round(-base["pnl"] * 100.0 / cap)))) if base["pnl"] < 0 else 0
    if base["blocked"]:
        base["msg"] = ("本月自動單已虧 %s 點，到了風控上限 %s 點 —— 這個月日盤、夜盤都不送，下個月自動恢復"
                       % (format(int(round(-base["pnl"])), ","), format(int(cap), ",")))
    elif base["hit"]:
        base["msg"] = ("本月自動單已虧 %s 點，超過上限 %s 點，但你已經手動解除 —— 照常送單"
                       % (format(int(round(-base["pnl"])), ","), format(int(cap), ",")))
    else:
        base["msg"] = ("本月自動單 %+d 點（上限 −%s）" % (int(round(base["pnl"])), format(int(cap), ",")))
    return base


def blocked(d, qty=1):
    """送單前問一次：`d` 是這一口算哪一天（日盤＝今天；夜盤＝開盤那晚 E）。⇒ (擋不擋, 那句話, state)。"""
    s = state(month=_month_of(d), qty=qty)
    return bool(s["blocked"]), s.get("msg") or "", s
