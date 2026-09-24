# -*- coding: utf-8 -*-
"""
【交易分析師】每週報告的儲存、檢查、讀過狀態（2026-09-24 Benson 交辦）。

Benson 要的是「一個專門幫我盯著的分析師」：每週六出一份週報（國際金融消息放最前、附來源），
放在面板與手機右上角的信件裡；沒讀過的有特別標示，**手機或電腦任一邊讀過，兩邊都消失**。

分工（⛔ 數字不讓 AI 算）：
  ・`analyst_facts.py`：程式算好所有數字（策略、市場、系統、風控、模擬候選）⇒ facts
  ・AI（每週排程的 Claude）：只寫「判斷」那幾段 —— 結論一句、國際消息（⛔ 每則一定附來源）、
    下週大事、大環境觀察、建議（最多 3 條）⇒ ai.json
  ・這支 `publish()`：**先檢查 ai.json**（`validate()`），過了才跟 facts 合成一份週報存起來。

⛔ 面板鐵律的例外（Benson 2026-09-24 拍板）：只有分析師可以給**經營層面**的建議，而且要附根據；
   ⛔ 仍然禁止預測漲跌、進出場方向、勝率、期望值、訊號強度 —— `validate()` 用字眼擋一層。
   每則消息／建議貼標籤：`data`＝有數據（用我們的歷史資料算過）、`judge`＝判讀・未驗證（只能當研究題目）。

檔案（⛔ 全部 gitignore：週報裡有真單的點數與帳戶狀態，repo 是公開的）：
  analyst/facts/<id>.json     程式算的數字
  analyst/reports/<id>.json   合成後的週報（id ＝ ISO 週，例如 2026-W39）
  analyst/read.json           讀過狀態 {id: {"at", "via"}}（via＝pc／phone）
手機讀過 ⇒ 手機用鑰匙圈的金鑰把 `data/analyst-read.json`（只有週次＋時間，沒有內容）寫進 repo，
這裡的 `pull_phone_reads()` 每 3 分鐘抓一次、併進 read.json（⛔ 只會「變成讀過」，不會變回未讀）。
"""
import hashlib
import json
import re
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIR = HERE / "analyst"
FACTS_DIR = DIR / "facts"
REPORTS_DIR = DIR / "reports"
READ_FILE = DIR / "read.json"
PHONE_READ_URL = ("https://raw.githubusercontent.com/xd1104/trade-log/"
                  "main/data/analyst-read.json")
PHONE_EVERY = 180
KEEP = 12                       # 列表／手機最多帶幾週
TAGS = ("data", "judge")
LAMPS = ("ok", "wn", "bd")
LAMP_WORD = {"ok": "正常", "wn": "要注意", "bd": "要處理"}
TAG_WORD = {"data": "有數據", "judge": "判讀・未驗證"}
MAX_RECS = 3
_ID_RE = re.compile(r"^\d{4}-W\d{2}$")
# ⛔ 面板鐵律還在：預測漲跌／進出場方向／勝率／期望值／訊號強度一律不准出現（寫法變體一起擋）。
BANNED = [
    (re.compile(r"(明天|下週|後市|接下來|短線|今晚|盤勢)[^。；，\n]{0,6}(會|將|可望|恐|看)(大)?(漲|跌|反彈|回檔|上攻|下殺)"), "預測漲跌"),
    (re.compile(r"(建議|可以|應該)(先)?(做多|做空|買進|賣出|進場|加碼做|放空)"), "給進出場方向"),
    (re.compile(r"勝率"), "勝率"),
    (re.compile(r"期望值"), "期望值"),
    (re.compile(r"訊號強度"), "訊號強度"),
    (re.compile(r"(目標價|支撐在|壓力在)"), "價位預測"),
]
_LOCK = threading.Lock()
_ST = {"started": False, "last_pull": None, "last_err": None, "phone_n": 0}


def week_id(d):
    y, w, _ = d.isocalendar()
    return "%04d-W%02d" % (y, w)


def week_range(d):
    """d 所在那一週的週一～週五。"""
    mon = d - timedelta(days=d.weekday())
    return mon, mon + timedelta(days=4)


# ══ 檢查 AI 寫的那一半 ═════════════════════════════════════════════════

def _s(x):
    return isinstance(x, str) and x.strip() != ""


def _texts(ai):
    """把 ai.json 裡所有給人看的字串攤平（拿來掃禁用字眼）。"""
    out = []

    def walk(v, path):
        if isinstance(v, str):
            if not path.endswith(".url"):
                out.append((path, v))
        elif isinstance(v, dict):
            for k, x in v.items():
                walk(x, path + "." + k)
        elif isinstance(v, list):
            for i, x in enumerate(v):
                walk(x, "%s[%d]" % (path, i))
    walk(ai, "ai")
    return out


def validate(ai):
    """⇒ 錯誤清單（空的＝過）。⛔ 過不了就不發佈（不猜、不自動修）。"""
    errs = []
    if not isinstance(ai, dict):
        return ["ai.json 不是一個物件"]
    v = ai.get("verdict")
    if not isinstance(v, dict) or v.get("lamp") not in LAMPS or not _s(v.get("line")):
        errs.append("verdict 要有 lamp（ok／wn／bd）與 line（一句話結論）")
    news = ai.get("news")
    if not isinstance(news, list) or not news:
        errs.append("news 至少要一則（國際金融消息放最前面）")
        news = []
    for i, n in enumerate(news):
        p = "news[%d]" % i
        if not isinstance(n, dict):
            errs.append(p + " 不是物件")
            continue
        if n.get("tag") not in TAGS:
            errs.append(p + ".tag 只能是 data 或 judge")
        for k in ("date", "title", "summary"):
            if not _s(n.get(k)):
                errs.append("%s.%s 是空的" % (p, k))
        src = n.get("sources")
        good = [s for s in (src or []) if isinstance(s, dict) and _s(s.get("title"))
                and isinstance(s.get("url"), str) and re.match(r"^https://[^\s]+\.[^\s]+", s["url"])]
        if not good:
            errs.append(p + " 沒有來源（⛔ 每則新聞至少一個 https 連結，Benson 交代）")
        if len(good) != len(src or []):
            errs.append(p + ".sources 有格式不對的（要 {title, url}，url 以 https:// 開頭）")
        for j, im in enumerate(n.get("impacts") or []):
            if not (isinstance(im, dict) and _s(im.get("who")) and _s(im.get("text"))):
                errs.append("%s.impacts[%d] 要有 who 與 text" % (p, j))
    for key, need in (("calendar", ("when", "event")), ("env", ("title", "text"))):
        for i, c in enumerate(ai.get(key) or []):
            if not isinstance(c, dict) or any(not _s(c.get(k)) for k in need):
                errs.append("%s[%d] 要有 %s" % (key, i, "、".join(need)))
            elif c.get("tag") is not None and c.get("tag") not in TAGS:
                errs.append("%s[%d].tag 只能是 data 或 judge" % (key, i))
    recs = ai.get("recs") or []
    if len(recs) > MAX_RECS:
        errs.append("recs 最多 %d 條（現在 %d 條）" % (MAX_RECS, len(recs)))
    for i, r in enumerate(recs):
        if not isinstance(r, dict) or r.get("tag") not in TAGS \
                or any(not _s(r.get(k)) for k in ("title", "body", "ask")):
            errs.append("recs[%d] 要有 tag（data／judge）、title、body（根據與筆數）、ask（要他決定什麼）" % i)
    for path, t in _texts(ai):
        for rx, why in BANNED:
            if rx.search(t):
                errs.append("%s 出現禁止的內容（%s）：「%s」" % (path, why, t[:40]))
    return errs


# ══ 合成、存檔、列表 ══════════════════════════════════════════════════

def _write_json(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def _read_json(p, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def publish(facts, ai, reports_dir=None):
    """驗 ai → 合成 → 存。⇒ (ok, 錯誤清單 或 週報路徑)。⛔ 數字一律用 facts 那一份（AI 改不到）。"""
    errs = validate(ai)
    if not isinstance(facts, dict) or not _ID_RE.match(str(facts.get("id", ""))):
        errs.append("facts 缺 id（先跑 analyst_facts.py）")
    if errs:
        return False, errs
    rep = {"id": facts["id"], "range": facts.get("range"), "made_at": datetime.now().isoformat(timespec="seconds"),
           "trial": bool(ai.get("trial")),
           "verdict": ai["verdict"], "news": ai["news"], "calendar": ai.get("calendar") or [],
           "env": ai.get("env") or [], "recs": ai.get("recs") or [],
           "facts": {k: facts.get(k) for k in ("strategies", "market", "system", "risk", "candidates", "week")}}
    p = (reports_dir or REPORTS_DIR) / (facts["id"] + ".json")
    _write_json(p, rep)
    return True, str(p)


def load(rid, reports_dir=None):
    if not _ID_RE.match(str(rid or "")):
        return None
    return _read_json((reports_dir or REPORTS_DIR) / (rid + ".json"))


def read_map(read_file=None):
    m = _read_json(read_file or READ_FILE, {}) or {}
    return m if isinstance(m, dict) else {}


def _is_read(entry, made_at):
    """⭐ 讀的時間 ≥ 週報產生時間才算讀過（同一週重新產生過 ⇒ 會再變回未讀）。時間一律台灣本地、ISO 字串比大小。"""
    at = (entry or {}).get("at") if isinstance(entry, dict) else None
    return isinstance(at, str) and at[:19] >= str(made_at or "")[:19]


def mark_read(rid, via="pc", read_file=None, at=None):
    """⇒ True＝這次才變成讀過。⛔ 只會往「讀過」走：時間只會變新，不會被舊的蓋回去。"""
    if not _ID_RE.match(str(rid or "")):
        return False
    at = (at or datetime.now().isoformat(timespec="seconds"))[:19]
    f = read_file or READ_FILE
    with _LOCK:
        m = read_map(f)
        old = m.get(rid) if isinstance(m.get(rid), dict) else None
        if old and str(old.get("at", "")) >= at:
            return False
        m[rid] = {"at": at, "via": via}
        _write_json(f, m)
    return True


def index(reports_dir=None, read_file=None, keep=KEEP):
    """列表（新到舊）⇒ [{id, range, lamp, lamp_word, line, n_news, n_recs, read}]。"""
    d = reports_dir or REPORTS_DIR
    rm = read_map(read_file)
    out = []
    try:
        files = sorted(d.glob("*.json"), reverse=True)
    except OSError:
        files = []
    for p in files:
        if not _ID_RE.match(p.stem):
            continue
        r = _read_json(p)
        if not isinstance(r, dict):
            continue
        v = r.get("verdict") or {}
        out.append({"id": p.stem, "range": r.get("range"), "made_at": r.get("made_at"),
                    "lamp": v.get("lamp"), "lamp_word": LAMP_WORD.get(v.get("lamp"), ""),
                    "line": v.get("line"), "n_news": len(r.get("news") or []),
                    "n_recs": len(r.get("recs") or []), "read": _is_read(rm.get(p.stem), r.get("made_at"))})
        if len(out) >= keep:
            break
    return out


def bundle(reports_dir=None, keep=KEEP):
    """給手機的全文包（⛔ 由 monitor_push 加密後才送出去）＋內容指紋（沒變就不重送）。"""
    reps = []
    for it in index(reports_dir=reports_dir, keep=keep):
        r = load(it["id"], reports_dir)
        if r:
            reps.append(r)
    raw = json.dumps(reps, ensure_ascii=False, sort_keys=True)
    return {"reports": reps, "hash": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]}


# ══ 手機那邊讀過的（鑰匙圈金鑰寫進 repo 的 data/analyst-read.json）══════════════

def merge_phone(payload, read_file=None):
    """⇒ 這次新併進來幾筆。payload ＝ {"read": {id: at}}。⛔ 格式不對的整筆跳過。"""
    rd = (payload or {}).get("read") if isinstance(payload, dict) else None
    if not isinstance(rd, dict):
        return 0
    n = 0
    for rid, at in rd.items():
        # ⛔ 時間不是字串 ⇒ 整筆跳過（不可以拿「現在」頂替 —— 那會把還沒讀的標成讀過）
        if _ID_RE.match(str(rid)) and isinstance(at, str) and len(at) >= 19 \
                and mark_read(rid, via="phone", read_file=read_file, at=at):
            n += 1
    return n


def pull_phone_reads():
    url = PHONE_READ_URL + "?t=" + str(int(time.time()))
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        _ST["last_err"] = str(e)[:120]
        return 0
    _ST["last_pull"] = datetime.now().isoformat(timespec="seconds")
    _ST["last_err"] = None
    n = merge_phone(payload)
    _ST["phone_n"] += n
    return n


def _poll():
    while True:
        try:
            n = pull_phone_reads()
            if n:
                print("[分析師] 手機讀過 %d 份週報，已同步" % n, flush=True)
        except Exception as e:
            _ST["last_err"] = str(e)[:120]
        time.sleep(PHONE_EVERY)


def start():
    """面板 main() 叫一次（⛔ 包在 try 裡；起不來不影響送單與停損）。"""
    if _ST["started"]:
        return
    _ST["started"] = True
    threading.Thread(target=_poll, daemon=True, name="analyst_phone_reads").start()


def main(argv=None):
    """
    python analyst.py check <ai.json>             ⇒ 只檢查（過不了就列出原因）
    python analyst.py publish <ai.json> [facts]   ⇒ 檢查＋合成＋存（facts 預設用 analyst/facts/ 最新那一份）
    """
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if len(argv) < 2 or argv[0] not in ("check", "publish"):
        print(main.__doc__)
        return 2
    ai = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    if argv[0] == "check":
        errs = validate(ai)
        print("\n".join(["⛔ " + e for e in errs]) if errs else "✅ 檢查通過")
        return 1 if errs else 0
    fp = Path(argv[2]) if len(argv) > 2 else max(FACTS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
    ok, out = publish(json.loads(fp.read_text(encoding="utf-8")), ai)
    if not ok:
        print("\n".join("⛔ " + e for e in out))
        return 1
    print("✅ 已發佈：%s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
