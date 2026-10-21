# -*- coding: utf-8 -*-
"""
【手機監控】面板狀態快照 ⇒ 加密 ⇒ 推到 GitHub 的 `monitor` 分支（2026-09-24，Benson 交辦）。

Benson：「晚上不在電腦前，想用手機遠端監控早盤儀表板 —— 不需要操作，只要看它還活著沒、有沒有自動下單；
要輸入密碼，不然知道網址的人都能看。」

⛔⛔ **這支只讀、只送出去**：
   ・資料來源 ＝ 面板**自己的**唯讀端點（/api/state、/api/fire/state、/api/nightfire/state），
     跟畫面上同一份 ⇒ 不另寫一套判斷；面板 HTTP 卡住 ⇒ 快照照樣送出「面板沒回應」（那正是要被看到的事）。
   ・⛔ 不 import broker／auto_fire／night_fire，⛔ 不碰任何鎖、不碰停損迴圈；自己一條 daemon 執行緒。
   ・⛔ 一個按鈕都沒有：手機那頁**結構上**不能下單、不能開關。
⛔⛔ **repo 是公開的** ⇒ 推上去的只有密文。金鑰檔 `MONITOR_KEY.json`（gitignore）只存「從密碼算出來的鑰匙」，
   ⛔ 不存密碼本身。密碼由 Benson 自己在 `monitor_setup.py` 輸入（agent 看不到）。
   ⛔ 帳號號碼、token 一律不放進快照。

加密（只用標準庫；手機端 WebCrypto 同一套）：
  PBKDF2-SHA256(密碼, salt, 600000) ⇒ 64 bytes ＝ k_enc(32) ‖ k_mac(32)
  金鑰流 ＝ HMAC-SHA256(k_enc, nonce ‖ 區塊序號 4 bytes 大端) 串起來；密文 ＝ 明文 XOR 金鑰流
  tag ＝ HMAC-SHA256(k_mac, "tlmon1" ‖ t ‖ nonce ‖ 密文)（先加密後驗證；手機先驗 tag 再解）

推送：git plumbing（hash-object → mktree → commit-tree → push -f 到 refs/heads/monitor）
  ⛔ 不 checkout、不動 index／工作目錄 ⇒ 跟 practice.json 那條同步互不干擾；分支永遠只有最新一份。
"""
import base64
import hashlib
import hmac
import json
import math
import os
import re
import socket
import subprocess
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
KEY_FILE = HERE / "MONITOR_KEY.json"
BRANCH = "monitor"
FILE = "monitor.json"
HC_FILE = HERE / "HC_PING_URL.txt"   # ⭐ 2026-10-14 斷線通知的心跳網址（Healthchecks.io；gitignore／本機 exclude，⛔ 不上傳）
AN_FILE = "analyst.json"      # ⭐ 2026-09-24【交易分析師】週報全文（加密）；內容沒變就沿用同一份密文（同一個 blob，不重傳）
USAGE_WARN_PCT = 80            # 券商流量用到幾 % 進「要注意的事」
EVERY_S = 120                # 每 2 分鐘一張（手機超過 6 分鐘沒更新就亮紅燈）
HTTP_TIMEOUT = 10
MAGIC = b"tlmon1"
KDF_ITER = 600000

STATE = {"on": False, "msg": "還沒啟動", "last_ok": None, "last_err": None, "pushes": 0, "fails": 0}
_AN = {"hash": None, "box": None}


# ── 加密 ──────────────────────────────────────────────────────────────

def derive(password, salt, iters=KDF_ITER):
    k = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters, dklen=64)
    return k[:32], k[32:]


def _stream(k_enc, nonce, n):
    out = bytearray()
    i = 0
    while len(out) < n:
        out += hmac.new(k_enc, nonce + i.to_bytes(4, "big"), hashlib.sha256).digest()
        i += 1
    return bytes(out[:n])


def seal(obj, key, t=None):
    """⇒ 可以直接寫成 JSON 的密文封包。"""
    t = str(int(t if t is not None else time.time()))
    pt = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    nonce = os.urandom(16)
    ct = bytes(a ^ b for a, b in zip(pt, _stream(key["k_enc"], nonce, len(pt))))
    tag = hmac.new(key["k_mac"], MAGIC + t.encode() + nonce + ct, hashlib.sha256).digest()
    b = lambda x: base64.b64encode(x).decode()
    return {"v": 1, "t": int(t), "kdf": {"alg": "PBKDF2-SHA256", "iter": key["iter"], "salt": b(key["salt"])},
            "nonce": b(nonce), "ct": b(ct), "tag": b(tag)}


def open_(box, key):
    """測試用：解開 seal() 的封包（⛔ 面板本身用不到）。tag 對不上 ⇒ ValueError。"""
    d = base64.b64decode
    nonce, ct, tag = d(box["nonce"]), d(box["ct"]), d(box["tag"])
    want = hmac.new(key["k_mac"], MAGIC + str(box["t"]).encode() + nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(want, tag):
        raise ValueError("密碼不對或內容被改過")
    pt = bytes(a ^ b for a, b in zip(ct, _stream(key["k_enc"], nonce, len(ct))))
    return json.loads(pt.decode("utf-8"))


def load_key(path=None):
    p = Path(path or KEY_FILE)
    o = json.loads(p.read_text(encoding="utf-8"))
    d = base64.b64decode
    return {"salt": d(o["salt"]), "iter": int(o["iter"]), "k_enc": d(o["k_enc"]), "k_mac": d(o["k_mac"])}


# ── 快照（⛔ 只讀面板自己的端點）──────────────────────────────────────

def _get(port, path):
    rq = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                headers={"Sec-Fetch-Site": "same-origin", "Host": "127.0.0.1:%d" % port})
    with urllib.request.urlopen(rq, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def _pick(d, keys):
    return {k: d.get(k) for k in keys if isinstance(d, dict) and k in d}


# ══ ⭐ 2026-10-19 手機減字版（老闆：字太多、看不懂「停損失靈」這類術語）══════════════════════
#    句子一律在這裡組好（⛔ 手機只照畫、不自己判斷）；舊的長句欄位照帶（電腦面板與舊手機相容）。
#    正本規格：lab-ux 的 phone-demo SPEC §3（要你處理）§4（白話短句）；trade-log CLAUDE.md「手機監控」那節有摘要。
#    ⛔ 這一段一樣只讀面板端出來的 JSON：⛔ 不 import broker／auto_fire／night_fire。

# 日盤沒送單的那一天（≤ 8 字）。⚠️ 代號正本是 auto_fire.WHY；沒列到的代號 ⇒「沒有送單」（⛔ 不猜原因）
DAY_SHORT = {
    "no_long": "沒有做多訊號", "not_fast": "沒有做多訊號", "no_reversal": "沒有做多訊號", "no_trade": "沒有做多訊號",
    "no_cand_rev": "沒有做多訊號", "union_done": "沒有做多訊號",
    "risk_cap": "本月停單中", "lots_zero": "已退休、不送", "late": "面板太晚開",
    "no_quote": "沒收到報價", "quote_stale": "沒收到報價", "mid_only": "沒收到報價", "no_signal": "沒收到報價",
    "no_hist": "歷史資料不夠", "cant_enter": "券商擋下來", "queue_full": "程式太忙沒送",
    "bad_method": "設定讀不到", "unreadable": "設定讀不到", "not_wired": "設定讀不到",
    "off": "開關關著", "crashed": "送到一半中斷",
}
DAY_CFG_BAD = ("bad_method", "unreadable", "not_wired")
# 夜盤沒送的那一晚（rec=skip）。⚠️ 代號正本是 night_fire.WHY
NIGHT_SHORT = {
    "not_fast": "走勢不夠強", "no_us": "沒收到報價", "no_ref": "沒收到報價", "no_quote": "沒收到報價",
    "late": "面板太晚開", "risk_cap": "本月停單中", "lots_zero": "已退休、不送", "no_hist": "歷史資料不夠",
    "bad_width": "資料怪怪的", "cannot": "券商擋下來", "unsure": "上次沒確定，今晚不送",
    "off": "開關關著", "bad": "設定讀不到",
}


def _day_short(why):
    w = why if isinstance(why, str) else ""
    if w in DAY_SHORT:
        return DAY_SHORT[w]
    if w.startswith(("wait_", "orb_")) or w.endswith("_short"):
        return "沒有做多訊號"
    return "沒有送單"


def _night_short(why):
    return NIGHT_SHORT.get(why if isinstance(why, str) else "", "沒有送單")


def _fin(x):
    """是不是一個正常的數字（⛔ bool、NaN、無限大都不算）。"""
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _total(pts, qty):
    """每口點數 × 口數 ⇒ 合計（手機一律看合計）；口數不知道 ⇒ 當 1 口。⛔ 永遠不丟例外。"""
    if not _fin(pts):
        return None
    q = qty if _fin(qty) and qty > 0 else 1
    return round(pts * q, 1)


def _phone_err(snap, what, e):
    """⭐ QA r1 必修 2：白話句組不出來 ⇒ 只少那一句、記在 phone_errs（⛔ 不進 errs：那會讓手機說「面板有一部分讀不到」）。"""
    try:
        snap.setdefault("phone_errs", []).append("%s：%s" % (what, str(e)[:80]))
    except Exception:                           # noqa: BLE001
        pass


def _day(rec, real, f=None):
    """一天的【自動下單】⇒ 手機看的那幾樣。"""
    f = f if isinstance(f, dict) else {}
    c = rec.get("cands") or {}
    out = {"date": rec.get("date"),
           "cands": [x.get("why_msg") for k in ("fast", "orb", "rev") for x in [c.get(k)] if x and x.get("why_msg")]}
    if rec.get("rec") == "result":
        out["trade"] = dict(_pick(rec, ("dir", "entry", "entry_time", "tp", "sl", "no_tp", "ok", "why_msg", "live", "cand",
                                        "qty")))
    elif rec.get("rec") in ("skip", "fire", "wait") and not out["cands"]:
        out["note"] = rec.get("why_msg")
    eod = rec.get("eod")
    if isinstance(eod, dict):
        out["eod"] = _pick(eod, ("why", "why_msg", "msg", "at", "ok", "alarm"))
    if isinstance(real, dict):
        out["real"] = _pick(real, ("state", "exit", "exit_time", "points", "why", "qty"))
    # ⭐ 2026-10-19 白話短句（沒送單的日子）＋還在等訊號的旗子（今天、三個候選還沒看完）
    #    ⛔ QA r1 必修 2：這一段自己包 try —— 出錯只少短句，上面的舊欄位照送
    try:
        if rec.get("rec") != "result":
            why = rec.get("why")
            out["why"] = why if isinstance(why, str) else None
            out["short"] = "正在送單" if (rec.get("rec") == "fire" and why != "crashed") else _day_short(why)
            rule = f.get("rule") if isinstance(f.get("rule"), dict) else {}
            last_at = str((rule.get("break_by") if f.get("method") == "U" else f.get("rev_at")) or "")
            nowt = str(f.get("now") or "")
            # ⚠️ no_long／no_trade ＝ 三個候選都看完了（定論）⇒ 不算「還在等」
            if (rec.get("date") == f.get("today") and out["short"] == "沒有做多訊號" and why not in ("no_long", "no_trade")
                    and last_at and nowt and nowt[:8] < last_at[:8]):
                out["short"], out["wait"], out["wait_until"] = "還在等訊號", True, last_at[:5]
        if isinstance(real, dict):
            out["real"]["total"] = _total(real.get("points"), real.get("qty"))
    except Exception:                           # noqa: BLE001
        pass
    return out


def _cushion_short(cu):
    """保證金還撐得住幾次停損（白話一句）；判不出來 ⇒ None（⛔ 不猜；⛔ 永遠不丟例外）。"""
    if not isinstance(cu, dict):
        return None
    if cu.get("warn"):
        return "不夠再停損 1 次"
    pts, stop = cu.get("pts"), cu.get("stop_pts")
    if cu.get("warn") is False and _fin(pts) and _fin(stop) and stop > 0:
        return "還撐得住 %d 次停損" % max(1, int(pts // stop))
    return None


def _wan(v):
    x = round(float(v) / 10000.0, 1)
    return ("%d" % x if x == int(x) else "%.1f" % x) + " 萬"


def _pos_extra(snap, day, night, now=None):
    """持有中那一句（⛔ 老闆 10-19：不放停損價／停利價／進場價）＋哪一盤＋合計浮動點數。"""
    r = snap.get("real") or {}
    p = r.get("position")
    if not isinstance(p, dict):
        return
    day = day if isinstance(day, dict) else {}
    hms = now.strftime("%H:%M:%S") if now else _panel_hms(day)
    night = night if isinstance(night, dict) else {}
    et = p.get("entry_time")
    et = et if isinstance(et, str) else ""
    if et:
        sess = "night" if (et >= "15:00" or et < "05:00") else "day"
    else:
        sess = "night" if (snap.get("panel") or {}).get("market") == "night" else "day"
    qty = p.get("qty") if _fin(p.get("qty")) and p.get("qty") > 0 else 1
    # 是不是自動單開的那一口（跟收盤平倉同一組認法：方向＋進場時刻到分）⇒ 才敢寫「幾點前自動平」
    ours, ours_E = False, None
    t0 = (day.get("days") or [{}])[0] or {}
    tr = t0.get("trade") or {}
    eod_at = str(day.get("eod_at") or "13:43:30")
    if sess == "day" and t0.get("date") == day.get("today") and tr.get("ok") \
            and tr.get("dir") == p.get("dir") and et and str(tr.get("entry_time") or "")[:5] == et[:5]:
        ours = True
    if sess == "night":
        for x in night.get("recent") or []:
            if x.get("rec") == "result" and x.get("ok") and x.get("dir") == p.get("dir") and et \
                    and str(x.get("entry_time") or "")[:5] == et[:5]:
                ours, ours_E = True, x.get("E")
                break
    # ⭐ QA r1 必修 4：收盤平倉那一刻已經過了（或今天已經有收盤紀錄）還抱著 ⇒ ⛔ 不寫「幾點前自動平」（那是一句假話）
    if sess == "day":
        past = bool((t0.get("date") == day.get("today") and isinstance(t0.get("eod"), dict) and t0["eod"].get("why"))
                    or hms >= eod_at[:8])
    else:
        past = any(x.get("rec") == "eod" and x.get("E") == ours_E for x in night.get("recent") or []) \
            or ("04:58:00" <= hms < "15:00:00")
    dw = {"long": "做多", "short": "做空"}.get(p.get("dir"))
    bits = [("%s %d 口" % (dw, qty)) if dw else ("%d 口（方向不明）" % qty)]
    if et:
        bits.append("%s 進場" % et[:5])
    if p.get("no_auto_exit"):
        bits.append("收盤不會自動平")
    elif ours and not past:
        bits.append("%s 前自動平" % ("04:58" if sess == "night" else eod_at[:5]))
    p["sess"] = sess
    p["short"] = "・".join(bits)
    r["float_total"] = _total(r.get("float_pts"), qty)


def _panel_hms(day, now_ts=None):
    """面板自己的時鐘（fire/state 的 now，HH:MM:SS）；沒有 ⇒ 這台電腦現在的時間（兩者本來就是同一台）。"""
    t = (day or {}).get("now") if isinstance(day, dict) else None
    if isinstance(t, str) and re.match(r"^\d\d:\d\d:\d\d$", t):
        return t
    return (datetime.fromtimestamp(now_ts) if now_ts else datetime.now()).strftime("%H:%M:%S")


def _stuck(p, day, hms):
    """⭐ QA r2 必修 1／2：部位過了它那一盤的收盤還在 ⇒ True（⛔ 不看紀錄是哪一天：13:43 沒平掉、過了午夜也一樣要叫）。
    日盤的部位：過了 eod_at、或還沒到 08:45（＝隔夜了）、或進場那天不是今天；夜盤的部位：04:58～15:00 之間還在。"""
    if not isinstance(p, dict):
        return False
    sess = p.get("sess")
    if sess == "day":
        eod_at = str(day.get("eod_at") or "13:43:30")[:8]
        ed = p.get("entry_day")
        return hms >= eod_at or hms < "08:45:00" or (isinstance(ed, str) and ed and day.get("today") and ed != day.get("today"))
    if sess == "night":
        return "04:58:00" <= hms < "15:00:00"
    return False


_CONN_BAD = {"since": None}       # 面板連不上券商從哪一張快照開始（只有這支知道「持續多久」）
_DEPTH_RE = re.compile(r"已 (\d+) 分鐘沒收到五檔")


def _int(x):
    try:
        return int(x) if _fin(x) or (isinstance(x, str) and x.strip().isdigit()) else 0
    except Exception:                           # noqa: BLE001
        return 0


def _todo(snap, lots_alarm, lots_todo, now_ts=None):
    """⭐ 手機「要你處理」⇒ [{lvl, text, act}]，排好 now → decide → later（⛔ 不出現術語；⛔ later 不推播）。
    ⚠️ 「面板 N 分鐘沒回報」只有手機知道 ⇒ 手機自己插在最前面，這裡不管。
    ⛔ QA r1 必修 2：每一條規則各自包 try —— 一條壞掉只少那一則，其他照出。"""
    now_ts = time.time() if now_ts is None else now_ts
    T = []

    def add(lvl, text, act):
        if not any(x["text"] == text for x in T):
            T.append({"lvl": lvl, "text": text, "act": act})

    real = snap.get("real") if isinstance(snap.get("real"), dict) else None
    pos = (real or {}).get("position")
    # ⭐ QA r1 必修 1：只有「確定沒部位」（real 讀得到、有 position 鍵、而且是空的）才算沒部位。
    #    real 讀不到、或面板剛開還沒問到券商（沒有 position 鍵）＝「不知道」⇒ 跟有部位一樣，「沒平掉／對不上帳」照出。
    known_flat = real is not None and "position" in real and pos is None
    maybe_pos = not known_flat
    day = snap.get("day") if isinstance(snap.get("day"), dict) else {}
    night = snap.get("night") if isinstance(snap.get("night"), dict) else {}
    eq = snap.get("equity") if isinstance(snap.get("equity"), dict) else {}
    rk = snap.get("risk") if isinstance(snap.get("risk"), dict) else {}

    def r_stale():
        if pos and real.get("stale_sec"):
            add("now", "報價斷了 %s 秒，手上的單現在沒人顧" % real["stale_sec"], "打開大戶投自己看部位")

    def r_stuck():
        if isinstance(pos, dict) and _stuck(pos, day, _panel_hms(day, now_ts)):
            pos["stuck"] = True
            add("now", "收盤沒平掉，部位還在", "立刻用大戶投平倉")

    def r_day():
        t0 = (day.get("days") or [{}])[0] or {}
        if t0.get("date") != day.get("today"):
            return
        ew = (t0.get("eod") or {}).get("why")
        if maybe_pos and ew in ("eod_failed", "eod_queue_full"):
            add("now", "收盤沒平掉，部位還在", "立刻用大戶投平倉")
        if maybe_pos and (ew in ("eod_cant_tell", "eod_unknown", "eod_unsure", "eod_crashed") or t0.get("why") == "crashed"):
            add("now", "程式對不上帳，不知道那筆平掉沒", "打開大戶投看還有沒有部位")
        if maybe_pos and ew == "eod_not_ours":
            add("decide", "現在這口不是自動單開的，程式不會碰", "要不要平，你自己決定")
        if t0.get("why") in DAY_CFG_BAD:
            add("later", "日盤自動下單的設定讀不到", "回電腦時看一下")

    def r_night():
        rec = [x for x in night.get("recent") or [] if isinstance(x, dict)]
        if not rec:
            return
        E0 = max(str(x.get("E") or "") for x in rec)
        for x in rec:
            if str(x.get("E") or "") != E0:
                continue
            if x.get("rec") == "eod" and maybe_pos and x.get("why") == "failed":
                add("now", "收盤沒平掉，部位還在", "立刻用大戶投平倉")
            if x.get("rec") == "eod" and maybe_pos and x.get("why") == "not_ours":
                add("decide", "現在這口不是自動單開的，程式不會碰", "要不要平，你自己決定")
            if x.get("rec") == "skip" and x.get("why") == "unsure" and E0 == str((night.get("tonight") or {}).get("E")):
                add("now", "夜盤上一次送單中斷，不確定有沒有成交", "打開大戶投看還有沒有部位")

    def r_lots_alarm():
        kinds = [str(a.get("kind") or "") for a in (lots_alarm or []) if isinstance(a, dict)]
        if "pos_unknown" in kinds:
            add("now", "這筆部位程式對不上帳，停損不會幫你平", "打開大戶投自己處理")
        if "pos_handover" in kinds or "pos_touched" in kinds:
            add("decide", "你自己加碼過，收盤不會自動平", "記得自己平掉")
        if "pos_fill" in kinds:
            add("later", "成交口數一度對不上", "有空到大戶投確認口數")

    def r_cushion():
        cu = eq.get("cushion") or {}
        if cu.get("warn"):
            tp = cu.get("topup")
            add("now", "帳戶的錢只比保證金多一點，再停損一次，下一筆會被券商退單",
                ("匯約 %s進期貨帳戶" % _wan(tp)) if _fin(tp) and tp > 0 else "補一點錢進期貨帳戶")

    def r_risk():
        if rk.get("err"):
            add("now", "算不出本月賺賠，自動單先不送", "看一下電腦面板")
        elif rk.get("blocked") and not rk.get("override"):
            try:
                y, m = (int(v) for v in str(rk.get("month") or "")[:7].split("-"))
                nxt = "%d/1" % (1 if m == 12 else m + 1)
            except Exception:                   # noqa: BLE001
                nxt = "下個月 1 號"
            cap = rk.get("cap")
            add("decide", "本月虧到上限%s，日盤、夜盤先停到月底" % (("（−%s 點）" % format(int(cap), ",")) if _fin(cap) else ""),
                "不動的話 %s 自動恢復；想提早恢復，要到電腦面板解除" % nxt)

    def r_lots_todo():
        for x in (lots_todo if isinstance(lots_todo, list) else []):
            if isinstance(x, dict) and isinstance(x.get("text"), str) and x["text"]:
                lv = x.get("lvl") if x.get("lvl") in ("now", "decide", "later") else "decide"
                add(lv, x["text"], x.get("act") if isinstance(x.get("act"), str) else "")

    def r_conn():
        panel = snap.get("panel") or {}
        conn = panel.get("conn") or {}
        if conn.get("ok") is False:
            if _CONN_BAD["since"] is None:
                _CONN_BAD["since"] = now_ts
            if maybe_pos:
                add("now", "面板連不上券商", "打開大戶投自己看部位")
            elif now_ts - _CONN_BAD["since"] > 10 * 60:
                add("later", "面板連不上券商", "回電腦時看一下")
        elif "conn" in panel:
            _CONN_BAD["since"] = None

    def r_misc():
        if snap.get("errs"):
            add("now", "面板有一部分讀不到", "看一下電腦面板有沒有當掉")
        if (eq.get("backup") or {}).get("warn"):
            add("later", "資料備份失敗", "回電腦時看一下")
        n_err = _int(day.get("err_n")) + _int(night.get("errors"))
        if n_err:
            add("later", "自動下單程式出錯 %d 次" % n_err, "回電腦時看一下")
        # ⭐ 2026-10-21 券商流量快用完（電腦面板的鈴鐺跟手機同一份）
        pct = (eq.get("usage") or {}).get("pct")
        if _fin(pct) and pct >= USAGE_WARN_PCT:
            add("later", "券商流量快用完了（今天用了 %d%%）" % int(pct), "回電腦時看一下")
        m = _DEPTH_RE.search(str((eq.get("depth") or {}).get("msg") or ""))
        if m and int(m.group(1)) >= 60:
            add("later", "五檔已經 %s 分鐘沒進來" % m.group(1), "回電腦時重開面板")

    for rule in (r_stuck, r_stale, r_day, r_night, r_lots_alarm, r_cushion, r_risk, r_lots_todo, r_conn, r_misc):
        try:
            rule()
        except Exception as e:                  # noqa: BLE001
            _phone_err(snap, "要你處理 %s" % rule.__name__, e)
    order = {"now": 0, "decide": 1, "later": 2}
    return sorted(T, key=lambda x: order.get(x["lvl"], 1))


def snapshot(port):
    """⇒ 一張快照（⛔ 永遠不丟例外：拿不到的那一塊寫進 errs）。"""
    snap = {"at": datetime.now().isoformat(timespec="seconds"), "host": socket.gethostname(), "errs": []}
    _lots_alarm, _lots_todo = [], []
    try:
        s = _get(port, "/api/state")
        r = s.get("real") or {}
        snap["panel"] = dict(_pick(s, ("status", "msg", "quote", "market", "phase", "clock", "age_sec")),
                             conn=_pick(s.get("conn") or {}, ("ok", "retries", "last_error", "contract_name")))
        snap["equity"] = _pick(s.get("equity") or {}, ("ok", "at", "equity", "day_pl", "float_pl"))
        # ⭐ 2026-09-24：還撐得住幾次停損（保證金提醒）—— 只帶那句話與燈號
        _cu = (s.get("equity") or {}).get("cushion") or {}
        snap["equity"]["cushion"] = _pick(_cu, ("warn", "msg", "topup"))
        try:                                                            # ⭐ 2026-10-19 白話一句（⛔ 出錯只少這一句）
            snap["equity"]["cushion"]["short"] = _cushion_short(_cu)
        except Exception as e:                  # noqa: BLE001
            _phone_err(snap, "保證金那一句", e)
        # ⭐ 2026-09-26 資料備份：失敗或太久沒備份才會在手機「要注意的事」出現
        snap["equity"]["backup"] = _pick((s.get("equity") or {}).get("backup") or {}, ("ok", "warn", "msg"))
        # ⭐ 2026-10-14 五檔看門狗：五檔停了（成交還在）⇒ 手機「要注意的事」
        snap["equity"]["depth"] = _pick((s.get("equity") or {}).get("depth") or {}, ("warn", "msg"))
        snap["equity"]["usage"] = _pick((s.get("equity") or {}).get("usage") or {}, ("pct",))
        snap["real"] = dict(_pick(r, ("live", "position", "stale_sec", "entries_today", "can_enter", "why",
                                      "last_error", "ca_ok")),
                            float_pts=r.get("float_pts"), sl=r.get("sl"), tp=r.get("tp"),
                            started=(r.get("code") or {}).get("started"))
    except Exception as e:
        snap["errs"].append("面板沒回應（/api/state）：%s" % str(e)[:120])
    try:
        f = _get(port, "/api/fire/state")
        names = f.get("method_names") or {}
        snap["day"] = dict(_pick(f, ("armed", "method", "live", "flag_exists", "arm_msg", "err", "err_n",
                                     "entries_today", "eod_at", "signal_at", "today", "eod_expiry")),
                           method_name=names.get(f.get("method")),
                           now=f.get("now"),      # ⭐ 10-19 QA r2：面板自己的時鐘（HH:MM:SS），判「部位過了收盤還在」用同一個鐘
                           closed=list(f.get("closed") or [])[-30:],
                           days=[_day(x, (f.get("real") or {}).get(x.get("date")), f) for x in (f.get("days") or [])[:5]])
        # ⭐ 2026-09-24 風控規則 B：**日盤＋夜盤共用**的本月額度 ⇒ 自己一塊（⛔ 不塞在 day 裡）
        snap["risk"] = _pick(f.get("risk") or {}, ("month", "since", "pnl", "cap", "hit", "override",
                                                   "blocked", "msg", "err"))
        # ⭐ 2026-10-14 口數：只帶後端組好的那幾句（級、a＋b、建議／減級提示）。⛔ 手機零按鈕、⛔ 不自己判斷。
        _lp = (f.get("lots") or {}).get("phone") or {}
        snap["lots"] = dict(_pick(_lp, ("label", "line", "att", "warn", "short")),
                            day=((f.get("lots") or {}).get("pills") or {}).get("day"),
                            night=((f.get("lots") or {}).get("pills") or {}).get("night"))
        _lots_alarm, _lots_todo = (f.get("lots") or {}).get("alarm") or [], _lp.get("todo") or []
    except Exception as e:
        snap["errs"].append("日盤自動下單讀不到：%s" % str(e)[:120])
    try:
        n = _get(port, "/api/nightfire/state")
        mname = {m.get("k"): m.get("name") for m in (n.get("methods") or [])}
        snap["night"] = dict(_pick(n, ("on", "method", "live", "msg", "tonight", "errors", "last_err", "flag_exists")),
                             method_name=mname.get(n.get("method")),
                             recent=[dict(_pick(x, ("E", "rec", "why", "msg", "method", "dir", "px", "entry", "entry_time",
                                                    "ok", "err", "sl_points", "tp_points", "at")),
                                          # ⭐ 2026-10-19 沒送的那一晚 ⇒ 白話短句
                                          **({"short": _night_short(x.get("why"))} if x.get("rec") == "skip" else {}))
                                     # ⚠️ 10-19 從 6 列放寬到 10 列：一晚最多兩列（送出＋04:58 平倉），
                                     #    6 列只夠 3 晚 ⇒ 手機「最近幾天」第 4 天的夜盤會變成「—」
                                     for x in (n.get("recent") or [])[:10]],
                             # ⭐ 2026-10-14：那一口後來怎麼了（面板唯讀比對 real_trades/ 的結果，鍵是那一晚 E）
                             real={E: dict(_pick(v, ("state", "exit", "exit_time", "exit_date", "points", "why", "qty")),
                                           total=_total(v.get("points"), v.get("qty")))
                                   for E, v in (n.get("real") or {}).items() if isinstance(v, dict)},
                             closed=list(n.get("closed") or [])[-30:])
    except Exception as e:
        snap["errs"].append("夜盤自動下單讀不到：%s" % str(e)[:120])
    try:
        a = _get(port, "/api/analyst/index")
        # ⭐ 只帶列表（週次、結論一句、讀過沒）＋全文的指紋；全文在 analyst.json（手機點開才抓）
        snap["analyst"] = {"hash": a.get("hash"), "unread": a.get("unread"),
                           "items": [_pick(x, ("id", "range", "made_at", "lamp", "lamp_word", "line",
                                                "n_news", "n_recs", "read")) for x in (a.get("items") or [])]}
    except Exception:
        snap["analyst"] = None
    # ⭐ 2026-10-19 手機減字版：持有中那一句＋要你處理（⛔ 包 try：組不出來只是少幾句，快照照送）
    try:
        _pos_extra(snap, snap.get("day"), snap.get("night"))
    except Exception as e:                      # noqa: BLE001
        _phone_err(snap, "持有中那一句", e)          # ⛔ 不進 errs（那會讓手機說面板有一部分讀不到）
    try:
        snap["todo"] = _todo(snap, _lots_alarm, _lots_todo)
    except Exception as e:                      # noqa: BLE001
        snap["todo"] = [{"lvl": "later", "text": "要你處理的清單組不出來", "act": "回電腦時看一下"}]
        _phone_err(snap, "要你處理", e)
    LAST.update(at=time.time(), todo=list(snap.get("todo") or []), lots_alarm=list(_lots_alarm or []))
    return snap


# ⭐ 2026-10-21 電腦面板右上角的鈴鐺「要注意的事」：⛔ 不寫第二份規則 —— 跟手機用**同一張快照的同一份 todo**。
#    手機那條每 2 分鐘本來就會算一次 ⇒ 鈴鐺優先拿那一份；太舊（或手機監控沒開）才當場算一張（⛔ 不推上 GitHub）。
LAST = {"at": 0.0, "todo": None, "lots_alarm": []}
_NLOCK = threading.Lock()
NOTICE_MAX_AGE = 150.0


def notice(port, max_age=NOTICE_MAX_AGE):
    """⇒ (todo, lots_alarm)。⛔ 永遠不丟例外（snapshot 自己就不丟）。"""
    with _NLOCK:
        if LAST["todo"] is None or time.time() - LAST["at"] > max_age:
            snapshot(port)
        return list(LAST["todo"] or []), list(LAST["lots_alarm"] or [])


# ── 推送（git plumbing，⛔ 不 checkout）────────────────────────────────

def _git(*args, inp=None):
    r = subprocess.run(["git"] + list(args), cwd=str(REPO), input=inp, capture_output=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError("git %s：%s" % (args[0], (r.stderr or b"").decode("utf-8", "replace")[-160:]))
    return r.stdout.decode("utf-8", "replace").strip()


def _analyst_box(key):
    """週報全文 ⇒ 加密；指紋沒變就回上一次那份密文（⛔ 不重新加密 ⇒ blob 一樣 ⇒ push 不重傳）。"""
    try:
        import analyst
        b = analyst.bundle()
    except Exception:
        return None
    if _AN["hash"] != b["hash"] or _AN["box"] is None:
        _AN["box"] = json.dumps(dict(seal({"reports": b["reports"]}, key), hash=b["hash"]),
                                separators=(",", ":")).encode("utf-8")
        _AN["hash"] = b["hash"]
    return _AN["box"]


def push(data_bytes, extra=None):
    blob = _git("hash-object", "-w", "--stdin", inp=data_bytes)
    lines = "100644 blob %s\t%s\n" % (blob, FILE)
    for name, data in sorted((extra or {}).items()):
        if data:
            lines += "100644 blob %s\t%s\n" % (_git("hash-object", "-w", "--stdin", inp=data), name)
    tree = _git("mktree", inp=lines.encode())
    commit = _git("commit-tree", tree, "-m", "monitor")
    _git("push", "-f", "-q", "origin", "%s:refs/heads/%s" % (commit, BRANCH))
    return commit


def hc_url():
    """心跳網址；沒有檔案或看不懂 ⇒ None（⛔ 不心跳，看門狗那條照舊）。"""
    try:
        u = HC_FILE.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    except Exception:
        return None
    return u if u.startswith("https://hc-ping.com/") else None


def heartbeat():
    """⭐ 2026-10-14【斷線通知】：GitHub 的 */5 排程實際上 3～7 小時才跑一次 ⇒ 改由 Healthchecks.io 盯著。
    ⛔ 只在快照**真的推上去之後**才跳一下 ⇒ 「Healthchecks 收不到」＝「手機也收不到新快照」，兩邊同一件事。
    它超過時間沒收到 ⇒ 叫 GitHub 跑一次看門狗（workflow_dispatch）⇒ 照舊用 Web Push 推到手機。
    ⛔ 失敗只記一筆，⛔ 不影響快照（那才是主線）。"""
    u = hc_url()
    if not u:
        return False
    try:
        with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "trade-log-panel"}),
                                    timeout=HTTP_TIMEOUT) as r:
            r.read()
        STATE["hc_ok"] = datetime.now().isoformat(timespec="seconds")
        return True
    except Exception as e:
        STATE["hc_err"] = str(e)[:160]
        return False


def once(port, key):
    box = seal(snapshot(port), key)
    push(json.dumps(box, separators=(",", ":")).encode("utf-8"), {AN_FILE: _analyst_box(key)})
    STATE["pushes"] += 1
    STATE["last_ok"] = datetime.now().isoformat(timespec="seconds")
    heartbeat()


def _loop(port, key):
    while True:
        try:
            once(port, key)
            STATE["msg"] = "正常"
        except Exception as e:
            STATE["fails"] += 1
            STATE["last_err"] = str(e)[:200]
            STATE["msg"] = "推送失敗"
            if STATE["fails"] <= 3 or STATE["fails"] % 30 == 0:
                print("⚠️ [手機監控] 推送失敗（第 %d 次）：%s" % (STATE["fails"], STATE["last_err"]), flush=True)
        time.sleep(EVERY_S)


def start(port):
    """面板 main() 叫一次。⛔ 沒有金鑰檔 ⇒ 不啟動、只印一句（⛔ 不可以用明文推上公開 repo）。"""
    if STATE["on"]:
        return
    if not KEY_FILE.exists():
        STATE["msg"] = "沒有 MONITOR_KEY.json（先跑 monitor_setup.py 設密碼）⇒ 手機監控關著"
        print("【手機監控】" + STATE["msg"], flush=True)
        return
    key = load_key()
    STATE["on"] = True
    STATE["msg"] = "啟動中"
    threading.Thread(target=_loop, args=(port, key), daemon=True, name="monitor_push").start()
    print("【手機監控】已啟動：每 %d 秒推一張加密快照到 GitHub（%s 分支）" % (EVERY_S, BRANCH), flush=True)
