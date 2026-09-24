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
import os
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
EVERY_S = 120                 # 每 2 分鐘一張（手機超過 6 分鐘沒更新就亮紅燈）
HTTP_TIMEOUT = 10
MAGIC = b"tlmon1"
KDF_ITER = 600000

STATE = {"on": False, "msg": "還沒啟動", "last_ok": None, "last_err": None, "pushes": 0, "fails": 0}


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


def _day(rec, real):
    """一天的【自動下單】⇒ 手機看的那幾樣。"""
    c = rec.get("cands") or {}
    out = {"date": rec.get("date"),
           "cands": [x.get("why_msg") for k in ("fast", "orb", "rev") for x in [c.get(k)] if x and x.get("why_msg")]}
    if rec.get("rec") == "result":
        out["trade"] = dict(_pick(rec, ("dir", "entry", "entry_time", "tp", "sl", "no_tp", "ok", "why_msg", "live", "cand")))
    elif rec.get("rec") in ("skip", "fire", "wait") and not out["cands"]:
        out["note"] = rec.get("why_msg")
    eod = rec.get("eod")
    if isinstance(eod, dict):
        out["eod"] = _pick(eod, ("why", "why_msg", "msg", "at", "ok", "alarm"))
    if isinstance(real, dict):
        out["real"] = _pick(real, ("state", "exit", "exit_time", "points", "why"))
    return out


def snapshot(port):
    """⇒ 一張快照（⛔ 永遠不丟例外：拿不到的那一塊寫進 errs）。"""
    snap = {"at": datetime.now().isoformat(timespec="seconds"), "host": socket.gethostname(), "errs": []}
    try:
        s = _get(port, "/api/state")
        r = s.get("real") or {}
        snap["panel"] = dict(_pick(s, ("status", "msg", "quote", "market", "phase", "clock", "age_sec")),
                             conn=_pick(s.get("conn") or {}, ("ok", "retries", "last_error", "contract_name")))
        snap["equity"] = _pick(s.get("equity") or {}, ("ok", "at", "equity", "day_pl", "float_pl"))
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
                           days=[_day(x, (f.get("real") or {}).get(x.get("date"))) for x in (f.get("days") or [])[:5]])
    except Exception as e:
        snap["errs"].append("日盤自動下單讀不到：%s" % str(e)[:120])
    try:
        n = _get(port, "/api/nightfire/state")
        mname = {m.get("k"): m.get("name") for m in (n.get("methods") or [])}
        snap["night"] = dict(_pick(n, ("on", "method", "live", "msg", "tonight", "errors", "last_err", "flag_exists")),
                             method_name=mname.get(n.get("method")),
                             recent=[_pick(x, ("E", "rec", "why", "msg", "method", "dir", "px", "entry", "entry_time",
                                               "ok", "err", "sl_points", "tp_points", "at"))
                                     for x in (n.get("recent") or [])[:6]])
    except Exception as e:
        snap["errs"].append("夜盤自動下單讀不到：%s" % str(e)[:120])
    return snap


# ── 推送（git plumbing，⛔ 不 checkout）────────────────────────────────

def _git(*args, inp=None):
    r = subprocess.run(["git"] + list(args), cwd=str(REPO), input=inp, capture_output=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError("git %s：%s" % (args[0], (r.stderr or b"").decode("utf-8", "replace")[-160:]))
    return r.stdout.decode("utf-8", "replace").strip()


def push(data_bytes):
    blob = _git("hash-object", "-w", "--stdin", inp=data_bytes)
    tree = _git("mktree", inp=("100644 blob %s\t%s\n" % (blob, FILE)).encode())
    commit = _git("commit-tree", tree, "-m", "monitor")
    _git("push", "-f", "-q", "origin", "%s:refs/heads/%s" % (commit, BRANCH))
    return commit


def once(port, key):
    box = seal(snapshot(port), key)
    push(json.dumps(box, separators=(",", ":")).encode("utf-8"))
    STATE["pushes"] += 1
    STATE["last_ok"] = datetime.now().isoformat(timespec="seconds")


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
