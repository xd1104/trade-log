# -*- coding: utf-8 -*-
"""
【夜盤自動下單】台積電快攻 —— **會真的送單的那一段**（2026-09-22 加，⛔ 預設關閉）。

規則（跟【模擬】「台積電快攻」同一條，研究 tick-research/scripts/night_controls.py）：
  美股開盤第一根 5 分 K（美東 9:30 開始、9:35 收完）台積電 ADR 走幅 ≥ 過去 40 晚的 8 成
  ⇒ 順著它的方向做台指 1 口；停利停損同寬 ＝ 進場價 × 過去 20 晚「進場到 04:58」振幅平均；
  沒碰到就 04:58 平。
  ⚠️ 即時只拿得到 **IEX**（免費方案），所以快不快用 IEX 的走幅跟 IEX 的歷史比
     （研究驗過：IEX 版回測每筆 +53、SIP 版 +46，沒有變差）。

⭐⭐ 2026-09-23 加第二條「夜盤跟勢」（`R`，Benson 知情後裁示可以上真單；⛔ 仍未通過前瞻驗證）：
  美股開盤後 10 分鐘（夏令 21:40／冬令 22:40）那一刻，台指現價 − 30 分鐘前那一分鐘的收盤
  ≥ 過去 40 晚同一個量的第 80 百分位（`trend_rule`，歷史讀【模擬】寫的 `trend_ctx.json`）⇒ 順勢 1 口。
  ⛔ **不設停利**（券商端一張單都沒有）；⭐ **2% 保護停損**（Benson 2026-09-23 拍板 ——
  研究是兩年只觸發 1 次、每月少賺約 2 點；【模擬】那條照研究不設停損，兩邊這一點**刻意不同**）。
  其餘抱到 04:58 平（跟台積電快攻同一段收盤平倉）。
  ⛔ T 與 R 同一時間只能開一條（開關檔只寫一個字母）—— 兩條同時出手方向一致 95%、損益相關 0.88。

⛔⛔ 開關：`NIGHT_ORDERS_ON`（內容寫 `T` 或 `R`）。**跟日盤的 `AUTO_ORDERS_ON` 完全分開**，動這個不會碰到日盤。
   ⛔ 這個模組**沒有任何一行會建立**那個檔（只做 exists／read／replace）—— 只有他自己建得出來。
   就算開了，真的送不送照樣受 `REAL_ORDERS_ON` 管（broker._send → is_live；沒有就只演練）。
⛔ 送單與平倉都在**自己的執行緒**上；4Hz 主迴圈（＝他的停損）一行都沒被改。
   價格用面板注入的 `quote_fn`（讀 Today.price／last_recv，就是停損看的那一個價）。
⛔ 停損：`broker.enter(..., sl_points=w)` ⇒ 面板停損迴圈照這一口自己的點數；停利是券商端限價單。
   ⚠️ 永豐沒有停損單 ⇒ **面板要整晚開著**，這件事畫面上一定要講。
"""
import json
import math
import re
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

import broker
import risk_cap          # ⛔ 唯讀：本月自動單虧到上限就不送（2026-09-24 風控規則 B）
import trend_rule
import tsm_rule
import us_feed
from auto_fire import _decode_flag          # ⛔ BOM／UTF-16 的解法共用同一支（不寫第二份）

HERE = Path(__file__).resolve().parent
ARM_FLAG = HERE / "NIGHT_ORDERS_ON"      # ⛔ gitignore（NIGHT_ORDERS_ON*）；⛔ 這個模組不准建它
NF_DIR = HERE / "nightfire"              # ⛔ gitignore（含進場價與時間）
TSM_CTX = HERE / "tsm_ctx.json"          # 【模擬】那條每天早上更新的「過去每晚振幅」（唯讀）
TREND_CTX = HERE / "trend_ctx.json"      # 【模擬】「夜盤跟勢」每天早上更新的過去每晚走幅（唯讀）
ARM_MAX_BYTES = 64
# ⛔ 代號跟日盤的 A／U 分開命名空間；⛔ 不准用 C／D（【自動下單（模擬）】的代號）。
# ⛔ METHOD_NAME 只准新增、不准改值（改值會讓舊紀錄當場跟著改名）。
METHODS = ("T", "R")
METHOD_NAME = {"T": "台積電快攻", "R": "夜盤跟勢"}
SYM, FEED = "TSM", "iex"

# ── 夜盤跟勢（R）──
R_WAIT_S = 1            # 美股開盤 +10 分鐘那一刻之後等幾秒再看（跟模擬「那一根的收盤」只差這一點）
R_SL_PCT = 0.02         # ⭐ 保護停損＝成交價的 2%（Benson 2026-09-23）；⛔ 不設停利
R_SL_SAFE = 0.975       # 成交前先帶的保守停損 ＝ 報價 × 2% × 這個數（滑價 2.5% 以內都不會超過上限）
R_CTX_MAX_AGE_D = 10    # 走幅歷史最新那一晚離今晚超過幾天 ⇒ 歷史太舊（面板很久沒開）⇒ 不做

ENTRY_WAIT_S = 3        # 第一根收完（done_at）之後等幾秒再問 Alpaca（K 棒要一點時間才出來）
FETCH_UNTIL_S = 90      # 問到 done_at + 90 秒還拿不到 ⇒ 今晚不做（⛔ 不猜）
LATE_S = 120            # 面板比 done_at 晚 2 分鐘以上才走到這一步（剛開機／看門狗）⇒ 不補單
QUOTE_MAX_AGE = 5       # 台指報價超過 5 秒沒更新就不送
WIDTH_MIN, WIDTH_MAX = 0.001, 0.03      # 框寬占進場價的合理範圍；超出 ⇒ 資料一定有問題 ⇒ 不送
EOD_FROM = (4, 58, 0)   # 04:58:00 起平倉
EOD_UNTIL = (4, 59, 50)
EOD_RETRY_S = 5.0
EOD_PX_TOL = 3.0        # 認「是不是我們那一口」：方向一樣、進場價差 ≤ 3 點
POLL_S = 0.5
_MONTH_RE = re.compile(r"^\d{4}-\d{2}\.jsonl$")

WHY = {"off": "夜盤自動下單是關著的（沒有 NIGHT_ORDERS_ON）",
       "bad": "開關檔看不懂", "late": "面板太晚才走到這一步（剛開機或剛重啟），今晚不補單",
       "no_us": "拿不到台積電 ADR 開盤那根 5 分 K（美股休市、Alpaca 沒回、或還沒出來）",
       "no_hist": "過去的歷史不夠", "not_fast": "不夠快", "no_quote": "台指報價不新鮮或不是夜盤時段",
       "bad_width": "停利停損框寬不合理（資料有問題）", "cannot": "現在不能進場",
       "send_fail": "送單失敗", "unsure": "上一次送到一半就中斷，不確定結果 —— 今晚不再送",
       "no_ref": "拿不到 30 分鐘前那一分鐘的台指價（面板那時沒收到報價）",
       "risk_cap": "本月自動單到了風控上限，這個月不送"}

_CFG = {"quote_fn": None, "session_fn": None, "minute_close_fn": None}
_ST = {"started": False, "errors": 0, "last_err": None, "last_err_at": None, "eod_try_at": 0.0,
       "hist_cache": {"E": None, "mvs": None}}
_MEM = {"E": None, "entry": None}         # 今晚那一口（recover_meta 只讀這裡）
_LOCK = threading.Lock()                  # ⛔ 只保護帳本寫檔（HTTP 執行緒會同時讀）


# ── 開關 ─────────────────────────────────────────────────────────────

def arm():
    """⇒ {"on", "method", "msg"}。⛔ 讀不懂就是關著（不猜一個做法）。"""
    try:
        if not ARM_FLAG.exists():
            return {"on": False, "method": None, "msg": WHY["off"]}
        txt = _decode_flag(ARM_FLAG.read_bytes()[:ARM_MAX_BYTES]).strip().upper()
    except Exception as e:
        return {"on": False, "method": None, "msg": WHY["bad"] + "：" + str(e)[:80]}
    if txt in METHODS:
        return {"on": True, "method": txt, "msg": "開著：%s" % METHOD_NAME[txt]}
    return {"on": False, "method": None,
            "msg": WHY["bad"] + "：讀到「%s」，要寫 %s" % (
                txt[:12], " 或 ".join("%s（%s）" % (k, METHOD_NAME[k]) for k in METHODS))}


def disarm():
    """⭐ 只會關、永遠不會開：把開關檔**改名**（內容留著，檔名就是幾點關的）。"""
    if not ARM_FLAG.exists():
        return True, "本來就是關著的"
    dest = ARM_FLAG.with_name(ARM_FLAG.name + ".off-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    try:
        ARM_FLAG.replace(dest)
    except Exception as e:
        return False, "關不掉：" + str(e)[:80]
    return True, "已經關掉夜盤自動下單（開關檔改名成 %s）" % dest.name


# ── 時刻 ─────────────────────────────────────────────────────────────

def evening_of(now):
    """現在屬於哪一晚的夜盤（15:00 以後＝今天；05:00 以前＝昨天）。不是平日晚上 ⇒ None。"""
    E = now.date() if now.hour >= 15 else (now.date() - timedelta(days=1) if now.hour < 5 else None)
    return E if (E is not None and E.weekday() < 5) else None


def done_at(E):
    """E 那晚美股開盤第一根 5 分 K 收完的台北時刻（夏令 21:35、冬令 22:35）。"""
    ny = datetime(E.year, E.month, E.day, 9, 35, tzinfo=us_feed.NY)
    return ny.astimezone(us_feed.TPE).replace(tzinfo=None)


def trend_at(E):
    """E 那晚「夜盤跟勢」看的那一刻：美股開盤 +10 分鐘的台北時刻（夏令 21:40、冬令 22:40）。"""
    ny = datetime(E.year, E.month, E.day, 9, 30, tzinfo=us_feed.NY) + timedelta(
        minutes=trend_rule.ENTRY_OFFSET)
    return ny.astimezone(us_feed.TPE).replace(tzinfo=None)


def decide_at(E, method):
    """那一條做法今晚幾點開始判斷（⛔ 關著時照台積電快攻的時刻記「關著」那一列）。"""
    if method == "R":
        return trend_at(E) + timedelta(seconds=R_WAIT_S)
    return done_at(E) + timedelta(seconds=ENTRY_WAIT_S)


def look_at(E, method):
    """畫面上「今晚幾點看」。"""
    return (trend_at(E) if method == "R" else done_at(E)).strftime("%H:%M")


def _in(now, a, b):
    t = (now.hour, now.minute, now.second)
    return a <= t < b


# ── 帳本（nightfire/YYYY-MM.jsonl；E 那晚的列都記在 E 的月份）─────────

def _path(E):
    return NF_DIR / ("%s.jsonl" % str(E)[:7])


def _append(row):
    with _LOCK:
        NF_DIR.mkdir(parents=True, exist_ok=True)
        out = dict(row, at=datetime.now().isoformat(timespec="seconds"))
        with _path(date.fromisoformat(row["E"])).open("a", encoding="utf-8") as f:
            f.write(json.dumps(out, ensure_ascii=False) + "\n")
            f.flush()
    return out


def rows_of(E):
    p = _path(E)
    out = []
    try:
        for ln in p.read_text(encoding="utf-8").splitlines():
            try:
                o = json.loads(ln)
            except Exception:
                continue
            if isinstance(o, dict) and o.get("E") == str(E):
                out.append(o)
    except OSError:
        pass
    return out


def _entry_of(rows):
    """今晚真的開出去的那一口（result ok）；沒有 ⇒ None。"""
    for o in rows:
        if o.get("rec") == "result" and o.get("ok"):
            return o
    return None


def _decided(rows):
    """今晚已經有定論（skip／result）或送到一半（sending 沒有 result）⇒ True。"""
    return any(o.get("rec") in ("skip", "result", "sending") for o in rows)


# ── 規則需要的歷史 ────────────────────────────────────────────────────

def past_mvs(E, n=tsm_rule.WIN, back_days=90):
    """E 以前最多 n 個美股交易日的 IEX 開盤 5 分走幅（⛔ 不含 E）。"""
    out = []
    d = E - timedelta(days=1)
    stop = E - timedelta(days=back_days)
    while d >= stop and len(out) < n:
        if d.weekday() < 5:
            f = us_feed.first5(SYM, d, feed=FEED)
            if isinstance(f, dict):
                out.append(f["mv_pct"])
        d -= timedelta(days=1)
    return list(reversed(out))


def _hist(E):
    """past_mvs 的快取（一晚算一次；⭐ 開盤前 20 分鐘先暖好，21:35 那一刻不必等下載）。"""
    c = _ST["hist_cache"]
    if c["E"] != E or c["mvs"] is None:
        c.update(E=E, mvs=past_mvs(E))
    return c["mvs"]


def past_rngs(E):
    """【模擬】那條記下的過去每晚「進場到 04:58」振幅%（⛔ 不含 E）。"""
    try:
        c = json.loads(TSM_CTX.read_text(encoding="utf-8"))
        rng = dict(c.get("rng") or {})
    except Exception:
        return []
    return [v for k, v in sorted(rng.items()) if k < str(E)][-tsm_rule.RNG_N:]


def past_sigs(E):
    """
    【模擬】「夜盤跟勢」記下的過去每晚走幅（點，⛔ 不含 E）⇒ (清單, 最新那一晚)。
    ⛔ 跟模擬**同一份**（`trend_ctx.json`，唯讀）：門檻兩邊各算一份就是兩把尺。
    """
    try:
        c = json.loads(TREND_CTX.read_text(encoding="utf-8"))
        sig = dict(c.get("sig") or {})
    except Exception:
        return [], None
    ks = [k for k in sorted(sig) if k < str(E) and isinstance(sig[k], (int, float))]
    return [float(sig[k]) for k in ks][-trend_rule.WIN:], (ks[-1] if ks else None)


def ref_price(E):
    """
    「30 分鐘前那一分鐘」的台指價 ⇒ (價, 用了哪一分鐘 HH:MM)；拿不到 ⇒ (None, None)。
    ⚠️⚠️ **對齊模擬**：模擬的 1 分 K 用**結束時間**標記，面板的 `Today.minute_close`
       用**開始時間**標記（那一分鐘裡最後一筆成交）。模擬的 p0 ＝「標籤 21:10 那根的收盤」
       ＝ 21:09:xx 最後一筆 ⇒ 這裡從 `進場分鐘 − 30 − 1` 開始找。
       往回最多找 `sim_lanes.TREND_TOL`（3）分鐘，跟模擬 `_trend_at` 同一個容忍度。
    ⛔ 拿不到就是拿不到（⛔ 不用更早的價、⛔ 不用現價頂）⇒ 今晚不做。
    """
    fn = _CFG["minute_close_fn"]
    if fn is None:
        return None, None
    t = trend_at(E) - timedelta(minutes=trend_rule.LOOKBACK + 1)
    m0 = t.hour * 60 + t.minute
    for m in range(m0, m0 - 4, -1):                   # TREND_TOL＝3 ⇒ 看 4 個分鐘
        try:
            v = fn(m)
        except Exception:
            v = None
        if isinstance(v, (int, float)) and np.isfinite(v) and v > 0:
            return float(v), "%02d:%02d" % (m // 60, m % 60)
    return None, None


# ── 進場 ─────────────────────────────────────────────────────────────

def _skip(E, why, msg=None, extra=None):
    row = dict({"E": str(E), "rec": "skip", "why": why, "msg": msg or WHY.get(why, why)}, **(extra or {}))
    print("[夜盤自動下單] %s 不送：%s" % (E, row["msg"]), flush=True)
    return _append(row)


def _decide(E, now):
    """E 那晚到了該看的時刻 ⇒ 判斷、要送就送。⇒ True＝今晚已有定論（不必再來）。"""
    rows = rows_of(E)
    if _decided(rows):
        return True
    a = arm()
    if not a["on"]:
        _skip(E, "off", a["msg"])
        return True
    m = a["method"]
    # ⛔ 「太晚」照**那一條自己的**時刻算（R 比 T 晚 5 分鐘看）。
    lag = (now - (trend_at(E) if m == "R" else done_at(E))).total_seconds()
    first_try = _ST.get("tried") != E
    _ST["tried"] = E
    if first_try and lag > LATE_S:
        _skip(E, "late", extra={"lag_s": round(lag), "method": m})
        return True
    # ⭐ 2026-09-24 風控規則 B（`risk_cap.py`）：本月自動單真單虧到上限 ⇒ 今晚不送。
    #    ⛔ 月份照**開盤那晚 E** 算（7/31 晚上那一口算 7 月）。T、R 兩條共用這一道。
    try:
        rb, rmsg, _rs = risk_cap.blocked(E, broker.QTY)
    except Exception as e:                       # ⛔ 風控自己壞掉 ⇒ 不猜，今晚不送
        rb, rmsg = True, "風控算不出本月損益 —— 不猜，今晚不送（%s）" % str(e)[:80]
    if rb:
        _skip(E, "risk_cap", WHY["risk_cap"] + "：" + rmsg, {"method": m})
        return True
    if m == "R":
        return _decide_trend(E, now)
    first = us_feed.first5_live(SYM, E, feed=FEED, now=now)
    if not isinstance(first, dict):
        if lag < FETCH_UNTIL_S:
            return False                          # 下一輪再問
        _skip(E, "no_us", extra={"method": "T"})
        return True
    mvs, rngs = _hist(E), past_rngs(E)
    if len(mvs) < tsm_rule.MIN_N or len(rngs) < tsm_rule.MIN_N:
        _skip(E, "no_hist", "過去的歷史不夠（走幅 %d 晚、振幅 %d 晚，各要 %d 晚）"
              % (len(mvs), len(rngs), tsm_rule.MIN_N), {"method": "T"})
        return True
    mv = float(first["mv_pct"])
    thr = tsm_rule.threshold(mvs)
    info = {"method": "T", "mv": round(mv, 4), "thr": round(thr, 4),
            "tsm_open": first["open"], "tsm_close": first["close"]}
    if not tsm_rule.is_fast(mv, thr):
        _skip(E, "not_fast", "台積電 ADR 開盤 5 分鐘 %+.2f%%，沒到門檻 %.2f%%" % (mv, thr), info)
        return True
    qf, sf = _CFG["quote_fn"], _CFG["session_fn"]
    px, age = qf() if qf else (None, None)
    if px is None or age is None or age > QUOTE_MAX_AGE or (sf and sf(now) != "night"):
        _skip(E, "no_quote", extra=dict(info, px=px, age=age))
        return True
    px = float(px)
    w = float(round(tsm_rule.width(px, rngs)))
    if not (WIDTH_MIN * px <= w <= WIDTH_MAX * px):
        _skip(E, "bad_width", "框寬 %g 點不合理（進場價 %g）" % (w, px), info)
        return True
    ok, why = broker.can_enter(px, True)
    if not ok:
        _skip(E, "cannot", "現在不能進場：%s" % why, info)
        return True
    d = "long" if mv > 0 else "short"
    base = dict(info, dir=d, px=px, pts=w, live=broker.is_live())
    _append(dict(base, E=str(E), rec="sending"))          # ⛔ 先落地「送出去了」再送（中斷也看得出來）
    ok, err, pos = broker.enter(d, px, w, sl_points=w)
    res = dict(base, E=str(E), rec="result", ok=bool(ok), err=err)
    if ok and isinstance(pos, dict):
        res.update(entry=pos.get("entry"), entry_time=pos.get("entry_time"), sl_points=w, tp_points=w)
    out = _append(res)
    if ok:
        _MEM.update(E=E, entry=out)
    print("[夜盤自動下單] %s %s %s：%s" % (E, "做多" if d == "long" else "做空", px,
                                      "已送出" if ok else ("失敗：%s" % err)), flush=True)
    return True


def _trend_sl_after_fill(pos, sl0):
    """
    成交之後把這一口的停損換成「成交價 × 2%、無條件捨去」⇒ 實際用的點數。
    ⛔ 只改 broker 那一口自己的 `sl_points`（拿 broker._lock，跟 `_recover_poll` 同一招）；
    ⛔ 成交價看不懂 ⇒ 留著保守的 sl0（⛔ 不猜）。
    """
    try:
        e = float(pos.get("entry"))
        if not (np.isfinite(e) and e > 0):
            return sl0
        sl = float(math.floor(e * R_SL_PCT))
    except Exception:
        return sl0
    with broker._lock:
        if broker._state.get("position") is pos:
            pos["sl_points"] = sl
    return sl


def _decide_trend(E, now):
    """
    ⭐ 夜盤跟勢（R）。⛔ 每一道「不做」都落地一列理由，⛔ 不猜。
    訊號 ＝ 現價 − 30 分鐘前那一分鐘的收盤（點）；門檻 ＝ 過去 40 晚 |走幅| 的第 80 百分位
    （跟【模擬】`sim_lanes.trend_eval` 同一個算法、同一份歷史）。
    ⛔ 不設停利（`tp_points=None` ⇒ broker 一張限價單都不掛、部位標 `no_tp`）；
    ⭐ 停損 ＝ 進場價 × 2%（面板 4Hz 停損迴圈照這一口自己的點數）。
    """
    info = {"method": "R"}
    past, newest = past_sigs(E)
    if len(past) < trend_rule.MIN_N:
        _skip(E, "no_hist", "過去的走幅歷史不夠（%d 晚，要 %d 晚）" % (len(past), trend_rule.MIN_N), info)
        return True
    if newest is None or (E - date.fromisoformat(newest)).days > R_CTX_MAX_AGE_D:
        _skip(E, "no_hist", "走幅歷史太舊（最新一晚是 %s）—— 面板太久沒開，門檻不可信" % newest,
              dict(info, newest=newest))
        return True
    qf, sf = _CFG["quote_fn"], _CFG["session_fn"]
    px, age = qf() if qf else (None, None)
    if px is None or age is None or age > QUOTE_MAX_AGE or (sf and sf(now) != "night"):
        _skip(E, "no_quote", extra=dict(info, px=px, age=age))
        return True
    px = float(px)
    p0, p0_at = ref_price(E)
    if p0 is None:
        _skip(E, "no_ref", extra=info)
        return True
    mv = px - p0
    thr = trend_rule.threshold(past)
    info.update(mv=round(mv, 1), thr=round(thr, 1), ref=p0, ref_at=p0_at, newest=newest)
    if not trend_rule.is_fast(mv, thr):
        _skip(E, "not_fast", "台指 30 分鐘走 %+.0f 點，沒到門檻 %.0f 點" % (mv, thr), info)
        return True
    # ⛔⛔ 停損點數**絕對不可以超過成交價的 2%**：面板 `_pos_points()` 有一道上限
    #    （POS_POINTS_MAX_FRAC＝2%），超過就判成「點數壞了」⇒ **改用手動那套 130 點** ——
    #    一口說好 2% 停損的單會被 130 點洗掉。而「報價 × 2%」在做空滑價（成交比報價低）
    #    或四捨五入進位時就會超過。⇒ 送單時先帶保守的 `sl0`（報價 2% 的 97.5%），
    #    **成交之後**才換成「成交價 × 2%、無條件捨去」（結構上 ≤ 上限）。
    sl0 = float(math.floor(px * R_SL_PCT * R_SL_SAFE))
    if not (WIDTH_MIN * px <= sl0 <= WIDTH_MAX * px):
        _skip(E, "bad_width", "停損 %g 點不合理（進場價 %g）" % (sl0, px), info)
        return True
    ok, why = broker.can_enter(px, True)
    if not ok:
        _skip(E, "cannot", "現在不能進場：%s" % why, info)
        return True
    d = "long" if mv > 0 else "short"
    base = dict(info, dir=d, px=px, pts=sl0, live=broker.is_live())
    _append(dict(base, E=str(E), rec="sending"))          # ⛔ 先落地「送出去了」再送（中斷也看得出來）
    ok, err, pos = broker.enter(d, px, None, sl_points=sl0)
    res = dict(base, E=str(E), rec="result", ok=bool(ok), err=err)
    sl = sl0
    if ok and isinstance(pos, dict):
        sl = _trend_sl_after_fill(pos, sl0)
        res.update(entry=pos.get("entry"), entry_time=pos.get("entry_time"),
                   sl_points=sl, tp_points=None, no_tp=True)
    out = _append(res)
    if ok:
        _MEM.update(E=E, entry=out)
    print("[夜盤自動下單] %s 夜盤跟勢 %s %s（停損 %g 點、不設停利）：%s" % (
        E, "做多" if d == "long" else "做空", px, sl,
        "已送出" if ok else ("失敗：%s" % err)), flush=True)
    return True


# ── 04:58 平倉（⛔ 只平自己那一口）────────────────────────────────────

def looks_ours(pos, ent):
    if not isinstance(pos, dict) or not isinstance(ent, dict):
        return False
    try:
        return (pos.get("dir") == ent.get("dir")
                and abs(float(pos.get("entry")) - float(ent.get("entry"))) <= EOD_PX_TOL)
    except Exception:
        return False


def _eod(E, now):
    """⇒ True＝這一晚的收盤平倉有定論了。"""
    rows = rows_of(E)
    if any(o.get("rec") == "eod" for o in rows):
        return True
    ent = _entry_of(rows)
    if ent is None:
        return True                                   # 今晚沒開出部位 ⇒ 沒事
    pos = broker._state.get("position")
    if pos is None:
        _append({"E": str(E), "rec": "eod", "why": "flat", "msg": "04:58 已經沒有部位（之前已停利或停損）"})
        return True
    if not looks_ours(pos, ent):
        _append({"E": str(E), "rec": "eod", "why": "not_ours",
                 "msg": "04:58 的部位不是夜盤自動下單那一口 ⇒ ⛔ 不碰"})
        return True
    if time.time() - _ST["eod_try_at"] < EOD_RETRY_S:
        return False
    _ST["eod_try_at"] = time.time()
    ok, err = broker.close("night_eod")
    if ok:
        _append({"E": str(E), "rec": "eod", "why": "closed", "msg": "04:58 平倉完成"})
        print("[夜盤自動下單] %s 04:58 平倉完成" % E, flush=True)
        return True
    print("⚠️ [夜盤自動下單] %s 04:58 平倉沒成功：%s（%g 秒後再試）" % (E, err, EOD_RETRY_S), flush=True)
    if not _in(now, EOD_FROM, EOD_UNTIL):
        _append({"E": str(E), "rec": "eod", "why": "failed",
                 "msg": "04:58 平不掉（%s）—— 請立刻自己到大戶投平倉" % err})
        return True
    return False


# ── 重啟撿回部位 ──────────────────────────────────────────────────────

def recover_meta(pos):
    """
    ⚠️ 會在 4Hz 主迴圈上被呼叫（broker.RECOVER_HOOK 的鏈子）。⛔ 只讀記憶體、⛔ 不丟例外。
    是今晚夜盤自動下單那一口 ⇒ 回它自己的停損／停利點數；不是 ⇒ None（交給日盤那一支判斷）。
    """
    try:
        ent = _MEM.get("entry")
        if ent and looks_ours(pos, ent) and evening_of(datetime.now()) == _MEM.get("E"):
            # ⭐ 夜盤跟勢那一口沒有停利（tp_points=None）⇒ 補回 `no_tp`，⛔ 不可以 float(None)
            #    （丟例外 ⇒ broker 把它判成 unmatched ⇒ 停損掉回手動 130 點）。
            if ent.get("tp_points") is None:
                return {"sl_points": float(ent["sl_points"]), "no_tp": True, "sl_src": "nightfire"}
            return {"sl_points": float(ent["sl_points"]), "tp_points": float(ent["tp_points"]),
                    "sl_src": "nightfire"}
    except Exception:
        return None
    return None


def _recover_poll():
    """
    ⚠️ 工作執行緒（⛔ 不是主迴圈）：重啟撿回來的部位若是今晚這一口、而停損還沒換成它自己的點數
    （主迴圈上 recover_meta 那一刻記憶體還沒讀到帳本 ⇒ 被日盤那支判成手動 130），在這裡補正。
    """
    pos = broker._state.get("position")
    ent = _MEM.get("entry")
    if not isinstance(pos, dict) or not pos.get("recovered") or not ent:
        return
    if pos.get("sl_src") == "nightfire" or not looks_ours(pos, ent):
        return
    with broker._lock:
        if broker._state.get("position") is pos:
            if ent.get("tp_points") is None:              # 夜盤跟勢：不設停利
                pos.update(sl_points=float(ent["sl_points"]), no_tp=True, sl_src="nightfire")
                pos.pop("tp_points", None)
            else:
                pos.update(sl_points=float(ent["sl_points"]), tp_points=float(ent["tp_points"]),
                           sl_src="nightfire")
            pos.pop("sl_warn", None)
    print("[夜盤自動下單] 重啟後撿回今晚那一口：%s %g 點（從帳本補回來）" % (
        "停損（不設停利）" if ent.get("tp_points") is None else "停損停利", ent["sl_points"]), flush=True)


# ── 工作執行緒 ────────────────────────────────────────────────────────

_DONE = {"decide": None, "eod": None, "warm": None}


def step(now=None):
    """一輪。⛔ 永遠不往外丟例外。"""
    try:
        now = now or datetime.now()
        E = evening_of(now)
        if E is None:
            return
        if _MEM.get("E") != E:
            _MEM.update(E=E, entry=_entry_of(rows_of(E)))
        _recover_poll()
        if _DONE["warm"] != E and now.hour >= 15 and now >= done_at(E) - timedelta(minutes=20):
            _DONE["warm"] = E
            a0 = arm()
            if a0["on"] and a0["method"] == "T":
                _hist(E)                                  # 暖快取（會下載當月 IEX 檔）
        # ⭐ 判斷時刻看**現在開著的那一條**（T 21:35:03／R 21:40:01）；關著就照 T 的時刻記「關著」。
        #   ⚠️ 開關是每一輪重讀的：21:35~21:40 之間從 R 換成 T ⇒ T 那一刻已過 ⇒ 走「太晚」不補單。
        if _DONE["decide"] != E and now.hour >= 15 and now >= decide_at(E, "T"):   # T 是最早的那一刻
            a1 = arm()
            if now >= decide_at(E, a1["method"] if a1["on"] else "T"):
                if _decide(E, now):
                    _DONE["decide"] = E
        if _DONE["eod"] != E and now.hour < 5 and _in(now, EOD_FROM, (5, 0, 0)):
            if _eod(E, now):
                _DONE["eod"] = E
    except Exception as e:
        _ST["errors"] += 1
        _ST["last_err"] = str(e)[:200]
        _ST["last_err_at"] = datetime.now().isoformat(timespec="seconds")
        if _ST["errors"] <= 3:
            print("⚠️ [夜盤自動下單] 出錯（已吞掉，第 %d 次）：%s" % (_ST["errors"], _ST["last_err"]), flush=True)


def _worker():
    while True:
        step()
        time.sleep(POLL_S)


def configure(quote_fn, session_fn, minute_close_fn=None):
    """`minute_close_fn(分鐘索引)` ⇒ 那一分鐘最後一筆成交價（夜盤跟勢要 30 分鐘前的價）；沒接 ⇒ R 一律不做。"""
    _CFG.update(quote_fn=quote_fn, session_fn=session_fn, minute_close_fn=minute_close_fn)


def start():
    if _ST["started"]:
        return
    _ST["started"] = True
    E = evening_of(datetime.now())
    if E is not None:
        _MEM.update(E=E, entry=_entry_of(rows_of(E)))     # ⛔ 先讀好，重啟撿回部位時 recover_meta 才認得
    threading.Thread(target=_worker, daemon=True, name="night_fire").start()


# ── 畫面（唯讀）───────────────────────────────────────────────────────

# 每一條做法那句話的**正本**（畫面上的單選清單、確認條都讀這裡）。
RULE_LINE = {
    "T": ("美股開盤第一根 5 分 K 台積電 ADR 走幅 ≥ 過去 %d 晚的 %g 成 ⇒ 順勢做台指 1 口；"
          "停利停損 ＝ 進場價 × 過去 %d 晚振幅平均；04:58 平"
          % (tsm_rule.WIN, tsm_rule.PCTL / 10, tsm_rule.RNG_N)),
    "R": ("美股開盤後 %d 分鐘，台指前 %d 分鐘走幅 ≥ 過去 %d 晚的 %g 成 ⇒ 順勢做台指 1 口；"
          "不設停利、停損 ＝ 進場價的 %g%%；04:58 平"
          % (trend_rule.ENTRY_OFFSET, trend_rule.LOOKBACK, trend_rule.WIN,
             trend_rule.PCTL / 10, R_SL_PCT * 100)),
}

def state(now=None):
    now = now or datetime.now()
    a = arm()
    E = evening_of(now) or (now.date() if now.weekday() < 5 else None)
    recent = []
    for f in sorted(NF_DIR.glob("*.jsonl"))[-2:] if NF_DIR.exists() else []:
        if not _MONTH_RE.match(f.name):
            continue
        for ln in f.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                o = json.loads(ln)
            except Exception:
                continue
            if o.get("rec") in ("skip", "result", "eod"):
                recent.append(o)
    # ⛔⛔ `flag_exists` ≠ `on`（2026-09-23 補畫面上的開關時加）：開關檔**存在但內容看不懂**
    #    （UTF-16／打錯字）時 `on` 是 False，但那個檔還在 ⇒ 面板上那顆「關閉」鈕的顯示條件
    #    要看**這一個**，⛔ 不是 `on` —— 看 `on` 的話壞掉的開關檔他關不掉，
    #    而「關」永遠是安全方向（日盤 2026-09-09 lab-qa Q9 已經踩過同一個坑）。
    #    ⚠️ `exists` 是這個模組對開關檔**唯一被允許的四個動作之一**（test_night_fire.py ⑦）。
    # ⛔ `method`＝現在開關檔裡是哪一條（關著是 None）。v3 驗收（2026-09-23）抓到少了這一欄：
    #    畫面上「目前在跑」標不出來、點目前那一條會跳出「從（空白）換成…」的確認條、
    #    而「不設停損」那句揭露（nfNoSL）結構上永遠不會出現。⛔ 前端不准自己照代號猜。
    # ⚠️ `name`／`rule` 是**舊紀錄的預設**（2026-09-23 以前的列沒有 `method`，一律是台積電快攻）；
    #    每一條自己的說明在 `rules`（⛔ 規則那句話的正本就在這裡，前端不准寫第二份）。
    rules = {"T": RULE_LINE["T"], "R": RULE_LINE["R"]}
    return {"ok": True, "on": a["on"], "method": a["method"], "msg": a["msg"],
            "live": broker.is_live(),
            "flag_exists": ARM_FLAG.exists(), "flag": ARM_FLAG.name,
            "name": METHOD_NAME["T"],
            "rule": rules["T"], "rules": rules,
            "tonight": ({"E": str(E), "look_at": look_at(E, a["method"] if a["on"] else "T")}
                        if E else None),
            "warn": "⚠️ 停損靠面板：夜盤有部位時面板要整晚開著、電腦不能睡",
            "how_on": "要開：在【自動下單】最底下的「開關」選一條做法（或在 tools\\shioaji 建 "
                      "NIGHT_ORDERS_ON，內容寫 T 或 R）",
            "recent": recent[-10:][::-1], "errors": _ST["errors"], "last_err": _ST["last_err"]}
