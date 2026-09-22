# -*- coding: utf-8 -*-
"""
【夜盤自動下單】台積電快攻 —— **會真的送單的那一段**（2026-09-22 加，⛔ 預設關閉）。

規則（跟【模擬】「台積電快攻」同一條，研究 tick-research/scripts/night_controls.py）：
  美股開盤第一根 5 分 K（美東 9:30 開始、9:35 收完）台積電 ADR 走幅 ≥ 過去 40 晚的 8 成
  ⇒ 順著它的方向做台指 1 口；停利停損同寬 ＝ 進場價 × 過去 20 晚「進場到 04:58」振幅平均；
  沒碰到就 04:58 平。
  ⚠️ 即時只拿得到 **IEX**（免費方案），所以快不快用 IEX 的走幅跟 IEX 的歷史比
     （研究驗過：IEX 版回測每筆 +53、SIP 版 +46，沒有變差）。

⛔⛔ 開關：`NIGHT_ORDERS_ON`（內容寫 `T`）。**跟日盤的 `AUTO_ORDERS_ON` 完全分開**，動這個不會碰到日盤。
   ⛔ 這個模組**沒有任何一行會建立**那個檔（只做 exists／read／replace）—— 只有他自己建得出來。
   就算開了，真的送不送照樣受 `REAL_ORDERS_ON` 管（broker._send → is_live；沒有就只演練）。
⛔ 送單與平倉都在**自己的執行緒**上；4Hz 主迴圈（＝他的停損）一行都沒被改。
   價格用面板注入的 `quote_fn`（讀 Today.price／last_recv，就是停損看的那一個價）。
⛔ 停損：`broker.enter(..., sl_points=w)` ⇒ 面板停損迴圈照這一口自己的點數；停利是券商端限價單。
   ⚠️ 永豐沒有停損單 ⇒ **面板要整晚開著**，這件事畫面上一定要講。
"""
import json
import re
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

import broker
import tsm_rule
import us_feed
from auto_fire import _decode_flag          # ⛔ BOM／UTF-16 的解法共用同一支（不寫第二份）

HERE = Path(__file__).resolve().parent
ARM_FLAG = HERE / "NIGHT_ORDERS_ON"      # ⛔ gitignore（NIGHT_ORDERS_ON*）；⛔ 這個模組不准建它
NF_DIR = HERE / "nightfire"              # ⛔ gitignore（含進場價與時間）
TSM_CTX = HERE / "tsm_ctx.json"          # 【模擬】那條每天早上更新的「過去每晚振幅」（唯讀）
ARM_MAX_BYTES = 64
METHODS = ("T",)
METHOD_NAME = {"T": "台積電快攻"}
SYM, FEED = "TSM", "iex"

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
       "send_fail": "送單失敗", "unsure": "上一次送到一半就中斷，不確定結果 —— 今晚不再送"}

_CFG = {"quote_fn": None, "session_fn": None}
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
            "msg": WHY["bad"] + "：讀到「%s」，要寫 T（台積電快攻）" % txt[:12]}


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
    lag = (now - done_at(E)).total_seconds()
    first_try = _ST.get("tried") != E
    _ST["tried"] = E
    if first_try and lag > LATE_S:
        _skip(E, "late", extra={"lag_s": round(lag)})
        return True
    first = us_feed.first5_live(SYM, E, feed=FEED, now=now)
    if not isinstance(first, dict):
        if lag < FETCH_UNTIL_S:
            return False                          # 下一輪再問
        _skip(E, "no_us")
        return True
    mvs, rngs = _hist(E), past_rngs(E)
    if len(mvs) < tsm_rule.MIN_N or len(rngs) < tsm_rule.MIN_N:
        _skip(E, "no_hist", "過去的歷史不夠（走幅 %d 晚、振幅 %d 晚，各要 %d 晚）"
              % (len(mvs), len(rngs), tsm_rule.MIN_N))
        return True
    mv = float(first["mv_pct"])
    thr = tsm_rule.threshold(mvs)
    info = {"mv": round(mv, 4), "thr": round(thr, 4), "tsm_open": first["open"], "tsm_close": first["close"]}
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
            pos.update(sl_points=float(ent["sl_points"]), tp_points=float(ent["tp_points"]),
                       sl_src="nightfire")
            pos.pop("sl_warn", None)
    print("[夜盤自動下單] 重啟後撿回今晚那一口：停損停利 %g 點（從帳本補回來）" % ent["sl_points"], flush=True)


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
            if arm()["on"]:
                _hist(E)                                  # 暖快取（會下載當月 IEX 檔）
        if _DONE["decide"] != E and now >= done_at(E) + timedelta(seconds=ENTRY_WAIT_S) and now.hour >= 15:
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


def configure(quote_fn, session_fn):
    _CFG.update(quote_fn=quote_fn, session_fn=session_fn)


def start():
    if _ST["started"]:
        return
    _ST["started"] = True
    E = evening_of(datetime.now())
    if E is not None:
        _MEM.update(E=E, entry=_entry_of(rows_of(E)))     # ⛔ 先讀好，重啟撿回部位時 recover_meta 才認得
    threading.Thread(target=_worker, daemon=True, name="night_fire").start()


# ── 畫面（唯讀）───────────────────────────────────────────────────────

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
    return {"ok": True, "on": a["on"], "msg": a["msg"], "live": broker.is_live(),
            "name": METHOD_NAME["T"],
            "rule": ("美股開盤第一根 5 分 K 台積電 ADR 走幅 ≥ 過去 %d 晚的 %g 成 ⇒ 順勢做台指 1 口；"
                     "停利停損 ＝ 進場價 × 過去 %d 晚振幅平均；04:58 平"
                     % (tsm_rule.WIN, tsm_rule.PCTL / 10, tsm_rule.RNG_N)),
            "tonight": ({"E": str(E), "look_at": done_at(E).strftime("%H:%M")} if E else None),
            "warn": "⚠️ 停損靠面板：夜盤有部位時面板要整晚開著、電腦不能睡",
            "how_on": "要開：在 tools\\shioaji 建一個 NIGHT_ORDERS_ON 檔，內容寫 T",
            "recent": recent[-10:][::-1], "errors": _ST["errors"], "last_err": _ST["last_err"]}
