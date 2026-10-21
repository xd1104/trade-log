# -*- coding: utf-8 -*-
"""
【手機監控】monitor_push 的探針（2026-09-24）。⛔ 離線：不推 GitHub、不打真的面板（用假的 HTTP 服務）。
在守的事：
  ① 加解密來回一致；錯密碼、改一個位元組、改時間戳都會被擋
  ② 快照裡沒有 token、沒有帳號號碼；面板沒回應時照樣產出快照（errs 講出來）
  ③ 沒有 MONITOR_KEY.json ⇒ 不啟動（⛔ 不可以用明文推上公開 repo）
  ④ 推送只用 git plumbing（⛔ 不 checkout／不 add／不動工作目錄）；金鑰檔在 .gitignore
  ⑤ 面板 main() 的接線包在 try 裡
"""
import ast
import base64
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.stdout.reconfigure(encoding="utf-8")
import monitor_push as MP  # noqa: E402

FAIL = 0


def say(cond, name, extra=""):
    global FAIL
    FAIL += not cond
    print(("  OK   " if cond else "  FAIL ") + name + (f"  {extra}" if extra else ""))


print("=== ① 加解密 ===")
salt = os.urandom(16)
ke, km = MP.derive("正確的密碼", salt, 2000)
K = {"salt": salt, "iter": 2000, "k_enc": ke, "k_mac": km}
obj = {"中文": "測試", "n": list(range(300)), "x": None}
box = MP.seal(obj, K)
say(MP.open_(box, K) == obj, "  來回一致（含中文、長度跨很多區塊）")
say("測試" not in json.dumps(box, ensure_ascii=False) and "range" not in json.dumps(box), "  封包裡看不到明文")
ke2, km2 = MP.derive("錯的密碼", salt, 2000)
for name, bad, key in [
    ("錯密碼", box, {"salt": salt, "iter": 2000, "k_enc": ke2, "k_mac": km2}),
    ("改一個位元組", dict(box, ct=base64.b64encode(bytes([base64.b64decode(box["ct"])[0] ^ 1]) + base64.b64decode(box["ct"])[1:]).decode()), K),
    ("改時間戳（把舊快照說成新的）", dict(box, t=box["t"] + 600), K),
]:
    try:
        MP.open_(bad, key)
        say(False, "  %s ⇒ 要被擋" % name)
    except ValueError:
        say(True, "  %s ⇒ 擋下" % name)
say(MP.seal(obj, K)["nonce"] != box["nonce"], "  每次 nonce 都不一樣")
say(box["kdf"]["salt"] == base64.b64encode(salt).decode() and box["kdf"]["iter"] == 2000, "  封包帶 salt 與次數（手機才算得出同一把鑰匙）")

print("\n=== ② 快照（假面板）===")
FAKE = {
    "/api/state": {"status": "live", "quote": "live", "market": "night", "clock": "21:41:00", "age_sec": 0, "token": "SECRET-TOKEN",
                   "conn": {"ok": True, "retries": 0}, "equity": {"ok": True, "equity": 37150, "day_pl": 0, "at": "21:40"},
                   "real": {"live": True, "account": "2056045", "position": {"dir": "long", "entry": 48100, "qty": 1},
                            "float_pts": 12, "sl": 47138, "code": {"started": "09-24 18:00"}}},
    "/api/fire/state": {"armed": True, "method": "U", "live": True, "token": "SECRET-TOKEN", "method_names": {"U": "多方聯軍"},
                        "today": "2026-09-24", "days": [{"date": "2026-09-24", "cands": {"fast": {"why_msg": "快攻：不夠快"}}}],
                        "real": {}},
    "/api/nightfire/state": {"on": True, "method": "R", "live": True, "methods": [{"k": "R", "name": "夜盤跟勢"}],
                             "tonight": {"E": "2026-09-24", "look_at": "21:40"},
                             "recent": [{"E": "2026-09-24", "rec": "result", "ok": True, "dir": "long", "entry": 48100}]},
}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        b = json.dumps(FAKE.get(self.path.split("?")[0], {}), ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b)


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]
s = MP.snapshot(port)
txt = json.dumps(s, ensure_ascii=False)
say(s["errs"] == [], "  三個端點都讀得到", s["errs"])
say("SECRET-TOKEN" not in txt, "  ⛔ 快照裡沒有 token")
say("2056045" not in txt, "  ⛔ 快照裡沒有帳號號碼")
say(s["real"]["position"]["entry"] == 48100 and s["real"]["float_pts"] == 12, "  部位與浮動點數有帶")
say(s["day"]["method_name"] == "多方聯軍" and s["night"]["method_name"] == "夜盤跟勢", "  日夜盤的做法名字有帶")
say(s["day"]["days"][0]["cands"] == ["快攻：不夠快"], "  今天的候選訊息有帶")
srv.shutdown()
s2 = MP.snapshot(port)          # 服務已經關了
say(len(s2["errs"]) == 3 and "面板沒回應" in s2["errs"][0], "  面板沒回應 ⇒ 照樣產出快照、errs 講出來", s2["errs"][:1])

print("\n=== ③ 沒有鑰匙檔就不啟動 ===")
real_kf = MP.KEY_FILE
MP.KEY_FILE = HERE / "__no_such_key__.json"
MP.STATE["on"] = False
MP.start(8770)
say(MP.STATE["on"] is False and "MONITOR_KEY" in MP.STATE["msg"], "  ⛔ 沒有 MONITOR_KEY.json ⇒ 不啟動", MP.STATE["msg"])
MP.KEY_FILE = real_kf

print("\n=== ④ 推送只用 plumbing ===")
src = (HERE / "monitor_push.py").read_text(encoding="utf-8")
tree = ast.parse(src)
push_fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "push")
cmds = [c.args[0].value for c in ast.walk(push_fn) if isinstance(c, ast.Call) and getattr(c.func, "id", "") == "_git"
        and c.args and isinstance(c.args[0], ast.Constant)]
# ⭐ 2026-09-24：多推一個 analyst.json（分析師週報，加密）⇒ hash-object 會出現兩次；⛔ 種類仍然只准這四種
say(set(cmds) == {"hash-object", "mktree", "commit-tree", "push"} and cmds.count("push") == 1,
    "  push() 只呼叫 hash-object／mktree／commit-tree／push（push 只有一次）", cmds)
say(not any(w in src for w in ('"checkout"', '"add"', '"reset"', '"stash"')), "  ⛔ 沒有 checkout／add／reset／stash")
gi = (HERE.parent.parent / ".gitignore").read_text(encoding="utf-8")
say("tools/shioaji/MONITOR_KEY.json" in gi, "  ⛔ 金鑰檔在 .gitignore 裡")

print("\n=== ⑤ 面板接線 ===")
lp = (HERE / "live_panel.py").read_text(encoding="utf-8")
m = lp[lp.index("def main():"):]
i = m.index("monitor_push.start(PORT)")
say(m.rfind("try:", 0, i) > m.rfind("\n    print(", 0, i) and "except Exception" in m[i:i + 200],
    "  main() 起手機監控包在 try 裡（起不來不影響送單與停損）")

print("\n=== ⑥ 2026-10-19 手機減字版：白話欄位（句子後端組好、不出現術語、不帶機密）===")
import copy  # noqa: E402
import lots  # noqa: E402
# 術語黑名單：老闆看不懂的字、程式代號、空值 ⇒ 一個都不准出現在給手機的白話句裡
JARGON = ("停損失靈", "風控", "滑價", "對帳", "IOC", "eod", "EOD", "why", "None", "null", "undefined",
          "候選", "快攻", "開箱", "回馬", "⚠", "⛔", "cushion", "lots")


def clean(t):
    return isinstance(t, str) and t and not any(j in t for j in JARGON)


F2 = {
    "/api/state": {"status": "live", "market": "day", "token": "SECRET-TOKEN",
                   "conn": {"ok": True, "retries": 0, "last_error": None},
                   "equity": {"ok": True, "equity": 45980, "day_pl": 0, "at": "10:04",
                              "cushion": {"warn": True, "pts": 398, "stop_pts": 537, "topup": 43000, "msg": "⚠️ 長句"},
                              "backup": {"ok": False, "warn": True, "msg": "⚠️ 資料備份失敗"},
                              "depth": {"warn": True, "msg": "五檔錄製：⚠️ 已 75 分鐘沒收到五檔（成交還在）—— 會自動重連"}},
                   "real": {"live": True, "account": "2056045", "stale_sec": 42,
                            "position": {"dir": "long", "qty": 2, "entry": 26842, "entry_time": "09:03:31", "no_tp": True},
                            "float_pts": 38, "sl": 26708, "code": {"started": "10-19 08:41"}}},
    "/api/fire/state": {"armed": True, "method": "U", "live": True, "token": "SECRET-TOKEN", "method_names": {"U": "多方聯軍"},
                        "today": "2026-10-19", "now": "13:50:00", "eod_at": "13:43:30", "rev_at": "09:15:00",
                        "rule": {"break_by": "09:30:00"}, "err_n": 2,
                        "days": [{"date": "2026-10-19", "rec": "result", "ok": True, "dir": "long", "entry": 26842,
                                  "entry_time": "09:03:31", "qty": 2, "live": True,
                                  "eod": {"why": "eod_failed", "why_msg": "⚠️ 收盤平倉沒有成功", "alarm": True}},
                                 {"date": "2026-10-16", "rec": "skip", "why": "no_long", "cands": {}},
                                 {"date": "2026-10-15", "rec": "skip", "why": "orb_narrow", "cands": {}},
                                 {"date": "2026-10-14", "rec": "skip", "why": "risk_cap", "cands": {}},
                                 {"date": "2026-10-13", "rec": "skip", "why": "something_new", "cands": {}}],
                        "real": {"2026-10-19": {"state": "open"}},
                        "risk": {"month": "2026-12", "pnl": -812, "cap": 800, "hit": True, "override": False, "blocked": True},
                        "lots": {"alarm": [{"kind": "pos_touched", "text": "部位：你在這一筆上自己加碼過"}, {"kind": "pos_fill", "text": "x"}],
                                 "pills": {"day": "2 口", "night": "1 口"},
                                 "phone": {"label": "2＋1（試車）", "line": "長句", "att": True, "warn": "部位：…",
                                           "short": "試車中：0／1 筆，跑完才會再談升級",
                                           "todo": [{"lvl": "decide", "text": "夜盤表現掉到退休線，已自動壓到 1 口",
                                                     "act": "要不要關掉，到電腦面板決定"}]}}},
    "/api/nightfire/state": {"on": True, "method": "R", "live": True, "methods": [{"k": "R", "name": "夜盤跟勢"}], "errors": 1,
                             "tonight": {"E": "2026-10-19", "look_at": "21:40"},
                             "recent": [{"E": "2026-10-16", "rec": "eod", "why": "not_ours", "msg": "x"},
                                        {"E": "2026-10-16", "rec": "result", "ok": True, "dir": "long", "entry": 1, "entry_time": "21:40:02"},
                                        {"E": "2026-10-15", "rec": "skip", "why": "not_fast", "msg": "x"},
                                        {"E": "2026-10-14", "rec": "skip", "why": "bad_width", "msg": "x"},
                                        {"E": "2026-10-13", "rec": "skip", "why": "brand_new", "msg": "x"}],
                             "real": {"2026-10-16": {"state": "ok", "points": 86, "qty": 2, "why": "night_eod"}}},
}
_real_get = MP._get
MP._get = lambda port, path: copy.deepcopy(F2[path]) if path in F2 else (_ for _ in ()).throw(RuntimeError("沒有"))
MP._CONN_BAD["since"] = None
S6 = MP.snapshot(0)
t6 = json.dumps(S6, ensure_ascii=False)
say(not S6["errs"], "  假面板四個端點都讀得到（analyst 沒有 ⇒ None 不算錯）", S6["errs"])
say("SECRET-TOKEN" not in t6 and "2056045" not in t6, "  ⛔ 新欄位加了之後，快照裡還是沒有 token、沒有帳號號碼")
T6 = S6["todo"]
lv = [x["lvl"] for x in T6]
say(lv == sorted(lv, key=lambda v: {"now": 0, "decide": 1, "later": 2}[v]), "  要你處理排好 now → decide → later（有空再看一定最後）", lv)
say(all(set(x) == {"lvl", "text", "act"} and clean(x["text"]) and clean(x["act"]) for x in T6),
    "  每一則都是 {lvl, text, act}、白話、沒有術語", [x["text"] for x in T6 if not (clean(x["text"]) and clean(x["act"]))])
tx = [x["text"] for x in T6]
for want, lvl in [("報價斷了 42 秒，手上的單現在沒人顧", "now"), ("收盤沒平掉，部位還在", "now"),
                  ("帳戶的錢只比保證金多一點，再停損一次，下一筆會被券商退單", "now"),
                  ("本月虧到上限（−800 點），日盤、夜盤先停到月底", "decide"),
                  ("你自己加碼過，收盤不會自動平", "decide"), ("現在這口不是自動單開的，程式不會碰", "decide"),
                  ("夜盤表現掉到退休線，已自動壓到 1 口", "decide"),
                  ("成交口數一度對不上", "later"), ("資料備份失敗", "later"), ("自動下單程式出錯 3 次", "later"),
                  ("五檔已經 75 分鐘沒進來", "later")]:
    hit = [x for x in T6 if x["text"] == want]
    say(len(hit) == 1 and hit[0]["lvl"] == lvl, "  要你處理：「%s」（%s）" % (want, lvl), hit)
say(next(x["act"] for x in T6 if x["text"].startswith("帳戶的錢")) == "匯約 4.3 萬進期貨帳戶", "  保證金不夠 ⇒ 「匯約 4.3 萬進期貨帳戶」（topup 四捨五入到 0.1 萬）")
say("1/1 自動恢復" in next(x["act"] for x in T6 if x["text"].startswith("本月虧到上限")), "  12 月停單 ⇒ 「1/1 自動恢復」（跨年）")
P6 = S6["real"]["position"]
# ⭐ QA r1 必修 4：F2 今天收盤平倉失敗、部位還在 ⇒ ⛔ 不可以再寫「13:43 前自動平」（那時刻已經過了、也沒平掉）
say(P6.get("short") == "做多 2 口・09:03 進場" and P6.get("sess") == "day", "  收盤平倉失敗還抱著 ⇒ 不寫「幾點前自動平」", P6.get("short"))
from datetime import datetime as _dt  # noqa: E402
_sn = {"real": {"position": {"dir": "long", "qty": 1, "entry_time": "09:03:31"}, "float_pts": 5}, "panel": {}}
_dy = {"today": "2026-10-19", "eod_at": "13:43:30", "days": [{"date": "2026-10-19", "trade": {"ok": True, "dir": "long", "entry_time": "09:03:31"}}]}
MP._pos_extra(_sn, _dy, {}, now=_dt(2026, 10, 19, 10, 0))
say(_sn["real"]["position"]["short"] == "做多 1 口・09:03 進場・13:43 前自動平", "  自動單那一口、收盤前 ⇒ 寫「13:43 前自動平」", _sn["real"]["position"]["short"])
_sn["real"]["position"].pop("short")
MP._pos_extra(_sn, _dy, {}, now=_dt(2026, 10, 19, 13, 50))
say(_sn["real"]["position"]["short"] == "做多 1 口・09:03 進場", "  過了 13:43:30 還抱著 ⇒ 不寫", _sn["real"]["position"]["short"])
_sn = {"real": {"position": {"dir": "long", "qty": 1, "entry_time": "21:40:03"}, "float_pts": 5}, "panel": {}}
_ng = {"recent": [{"E": "2026-10-19", "rec": "eod", "why": "failed"},
                  {"E": "2026-10-19", "rec": "result", "ok": True, "dir": "long", "entry_time": "21:40:03"}]}
MP._pos_extra(_sn, {}, _ng, now=_dt(2026, 10, 20, 4, 59, 30))
say(_sn["real"]["position"]["short"] == "做多 1 口・21:40 進場", "  夜盤 04:58 平倉已經有紀錄（失敗）還抱著 ⇒ 不寫「04:58 前自動平」", _sn["real"]["position"]["short"])
_sn = {"real": {"position": {"dir": "", "qty": 0, "entry_time": ""}, "float_pts": None}, "panel": {}}
MP._pos_extra(_sn, {}, {}, now=_dt(2026, 10, 19, 10, 0))
say(_sn["real"]["position"]["short"] == "1 口（方向不明）", "  方向空白、口數 0 ⇒「1 口（方向不明）」", _sn["real"]["position"]["short"])

# ⭐ QA r1 必修 1（q_unknown_eodfail）：部位「不知道」≠ 確定沒部位 ⇒ 收盤沒平掉的警告照出
for _name, _mut in [("面板剛開（real 沒有 position 鍵）", lambda F: F["/api/state"]["real"].pop("position")),
                    ("/api/state 整個讀不到", lambda F: F.pop("/api/state"))]:
    _F = copy.deepcopy(F2)
    _mut(_F)
    MP._get = lambda port, path, _F=_F: copy.deepcopy(_F[path]) if path in _F else (_ for _ in ()).throw(RuntimeError("沒有"))
    _S = MP.snapshot(0)
    say(any(x["text"] == "收盤沒平掉，部位還在" and x["lvl"] == "now" for x in _S["todo"])
        and any(x["text"] == "現在這口不是自動單開的，程式不會碰" for x in _S["todo"]),
        "  %s ⇒ 日盤收盤沒平掉、夜盤不是自動單都照出" % _name, [x["text"] for x in _S["todo"]])
MP._get = lambda port, path: copy.deepcopy(F2[path]) if path in F2 else (_ for _ in ()).throw(RuntimeError("沒有"))

# ⭐ QA r1 必修 2：新組句出錯 ⇒ 只少那一句；舊欄位、部位照送，errs 不多（跟 main 版一樣）
for _name, _mut in [("保證金點數 NaN", lambda F: F["/api/state"]["equity"]["cushion"].update(pts=float("nan"), warn=False)),
                    ("保證金點數無限大", lambda F: F["/api/state"]["equity"]["cushion"].update(pts=float("inf"), warn=False)),
                    ("fire.rule 不是 dict", lambda F: F["/api/fire/state"].__setitem__("rule", "x")),
                    ("日盤 why 是清單", lambda F: F["/api/fire/state"]["days"][1].__setitem__("why", ["a"])),
                    ("夜盤 why 是 dict", lambda F: F["/api/nightfire/state"]["recent"][2].__setitem__("why", {"a": 1})),
                    ("夜盤口數是字串", lambda F: F["/api/nightfire/state"]["real"]["2026-10-16"].__setitem__("qty", "2")),
                    ("進場時間是數字", lambda F: F["/api/state"]["real"]["position"].__setitem__("entry_time", 930)),
                    ("err_n 是字串", lambda F: F["/api/fire/state"].__setitem__("err_n", "2")),
                    ("口數 todo 是字串", lambda F: F["/api/fire/state"]["lots"]["phone"].__setitem__("todo", "bad"))]:
    _F = copy.deepcopy(F2)
    _mut(_F)
    MP._get = lambda port, path, _F=_F: copy.deepcopy(_F[path]) if path in _F else (_ for _ in ()).throw(RuntimeError("沒有"))
    _S = MP.snapshot(0)
    say(not _S["errs"] and all(k in _S for k in ("real", "day", "night", "equity", "risk", "lots")) and _S["real"].get("position")
        and isinstance(_S.get("todo"), list) and _S["equity"]["cushion"].get("msg"),
        "  壞資料「%s」⇒ 舊欄位與部位照送、errs 沒多" % _name, (_S["errs"], _S.get("phone_errs")))
MP._get = lambda port, path: copy.deepcopy(F2[path]) if path in F2 else (_ for _ in ()).throw(RuntimeError("沒有"))
say("26842" not in P6["short"] and "26708" not in P6["short"], "  ⛔ 持有中那一句不放進場價／停損價（老闆 10-19 拍板）")
say(S6["real"]["float_total"] == 76, "  浮動點數合計＝每口 38 × 2 口", S6["real"]["float_total"])
D6 = S6["day"]["days"]
say([d.get("short") for d in D6[1:]] == ["沒有做多訊號", "沒有做多訊號", "本月停單中", "沒有送單"],
    "  日盤沒送那幾天的短句（沒列到的新代號 ⇒「沒有送單」，⛔ 不猜）", [d.get("short") for d in D6[1:]])
say(all(len(v) <= 8 for v in MP.DAY_SHORT.values()), "  日盤短句都 ≤ 8 字")
N6 = S6["night"]["recent"]
say([x.get("short") for x in N6 if x["rec"] == "skip"] == ["走勢不夠強", "資料怪怪的", "沒有送單"], "  夜盤沒送那幾晚的短句",
    [x.get("short") for x in N6 if x["rec"] == "skip"])
say(S6["night"]["real"]["2026-10-16"]["total"] == 172, "  夜盤點數合計＝每口 86 × 2 口")
say(S6["lots"]["short"] == "試車中：0／1 筆，跑完才會再談升級" and S6["equity"]["cushion"]["short"] == "不夠再停損 1 次",
    "  帳戶：升級那一列、保證金那一列", (S6["lots"]["short"], S6["equity"]["cushion"]["short"]))
say(all(clean(v) for v in list(MP.DAY_SHORT.values()) + list(MP.NIGHT_SHORT.values())), "  短句對照表沒有術語")
say(MP._cushion_short({"warn": False, "pts": 1209, "stop_pts": 537}) == "還撐得住 2 次停損"
    and MP._cushion_short({"warn": None, "pts": 1209}) is None, "  保證金：floor(點數／一次停損)；判不出來 ⇒ None（⛔ 不猜）")
# 確定沒部位 ⇒ 「收盤沒平掉」那類就不吵他（那件事已經過去了）
F3 = copy.deepcopy(F2)
F3["/api/state"]["real"]["position"] = None
MP._get = lambda port, path: copy.deepcopy(F3[path]) if path in F3 else (_ for _ in ()).throw(RuntimeError("沒有"))
S7 = MP.snapshot(0)
say(not any(x["text"] in ("收盤沒平掉，部位還在", "現在這口不是自動單開的，程式不會碰", "報價斷了 42 秒，手上的單現在沒人顧")
            for x in S7["todo"]), "  確定沒部位 ⇒ 不再出「收盤沒平掉／不是自動單／報價斷了」", [x["text"] for x in S7["todo"]])
# 面板連不上券商：有部位 ⇒ 現在；沒部位 ⇒ 撐過 10 分鐘才上「有空再看」
F3["/api/state"]["conn"]["ok"] = False
MP._CONN_BAD["since"] = None
snapA = MP.snapshot(0)
MP._CONN_BAD["since"] -= 11 * 60
snapB = MP.snapshot(0)
say(not any(x["text"] == "面板連不上券商" for x in snapA["todo"])
    and any(x["text"] == "面板連不上券商" and x["lvl"] == "later" for x in snapB["todo"]),
    "  連不上券商、沒部位：剛斷不吵、斷超過 10 分鐘 ⇒ 有空再看")
# 今天還在等訊號（多方聯軍 09:30 前）
r = MP._day({"date": "2026-10-19", "rec": "skip", "why": "not_fast", "cands": {}}, None,
            {"today": "2026-10-19", "method": "U", "now": "09:10:00", "rule": {"break_by": "09:30:00"}})
r2 = MP._day({"date": "2026-10-19", "rec": "skip", "why": "no_long", "cands": {}}, None,
             {"today": "2026-10-19", "method": "U", "now": "09:20:00", "rule": {"break_by": "09:30:00"}})
say(r.get("wait") and r["short"] == "還在等訊號" and not r2.get("wait") and r2["short"] == "沒有做多訊號",
    "  09:30 前還沒定論 ⇒「還在等訊號」；三個都看完（no_long）⇒ 定論", (r.get("short"), r2.get("short")))
MP._get = _real_get

# lots.py 的白話句（升級那一列、要你處理）
from datetime import date as _date, datetime  # noqa: E402
_today = _date(2026, 10, 19)
_st = {"pause_until": "2027-04-19"}
s_pause = lots._phone_short(_today, {"kind": "far"}, None, None, None, {"eq": 100000, "label": "2＋1", "gap": 46000},
                            {"status": "none"}, {}, [], _st, (1, 1), False)
s_far = lots._phone_short(_today, {"kind": "far"}, None, None, None, {"eq": 100000, "label": "2＋1", "gap": 46000},
                          {"status": "none"}, {}, [], {}, (1, 1), False)
s_pend = lots._phone_short(_today, {"kind": "pending"}, {"eff": "2026-10-21", "to": {"label": "2＋1"}, "kind": "trial"}, None, None,
                           None, {}, {}, [], {}, (1, 1), False)
s_blk = lots._phone_short(_today, {"kind": "blocked"}, None, None, None, {"eq": 100000, "label": "2＋1"}, {}, {},
                          [{"k": "slip", "ok": False, "name": "滑價"}, {"k": "eq", "ok": True}], {}, (1, 1), False)
say(s_far == "權益到 10 萬可以試車 2＋1（還差 4.6 萬）", "  升級：離下一級還遠", s_far)
say(s_pause.startswith("升級暫停到明年 4/19"), "  升級：暫停中（跨年寫「明年」）", s_pause)
say(s_pend == "10/21 起改 2＋1（試車）", "  升級：已確認還沒生效", s_pend)
say(s_blk == "先不升：成交價差太多", "  升級：條件卡住（⛔ 不寫「滑價」）", s_blk)
say(all(clean(x) and len(x) <= 30 for x in (s_far, s_pause, s_pend, s_blk)), "  升級那一句：白話、夠短")
t_off = lots._phone_todo({"to": {"label": "2＋1", "a": 2, "b": 1}}, None,
                         {"union": {"state": "ok"}, "trend": {"state": "retire", "cap": 1, "text": "x"}}, (1, 1), True)
say([x["text"] for x in t_off] == ["可以先試車 2＋1 了（日盤 2 口、夜盤 1 口）", "夜盤表現掉到退休線，已自動壓到 1 口"]
    and all(x["lvl"] == "decide" for x in t_off), "  口數的要你處理：可以升級＋退休（都是要你決定）", t_off)
P = lots.panel(now=datetime(2026, 10, 19, 10, 0), eq=54093.0)
say(isinstance((P.get("phone") or {}).get("short"), str) and isinstance(P["phone"].get("todo"), list),
    "  lots.panel() 的 phone 帶 short（字串）與 todo（清單），舊的 label／line／warn 照留",
    {k: P["phone"].get(k) for k in ("label", "short")})

print("\n=== ⑦ QA r2：部位還在的時候，任何一列都不准寫「已出場」===")
import re  # noqa: E402
import subprocess  # noqa: E402
import time as _time  # noqa: E402
from http.server import SimpleHTTPRequestHandler  # noqa: E402


def _trade(d, et="09:03:31", qty=1):
    return {"date": d, "rec": "result", "ok": True, "dir": "long", "entry": 26842, "entry_time": et, "live": True, "qty": qty}


def _base7():
    F = copy.deepcopy(F2)
    st, fs, nf = F["/api/state"], F["/api/fire/state"], F["/api/nightfire/state"]
    st["real"].pop("stale_sec", None)
    st["equity"]["cushion"] = {"warn": False, "pts": 1209, "stop_pts": 537, "msg": "x"}
    st["equity"]["backup"] = {"warn": False}
    st["equity"]["depth"] = {"warn": False, "msg": ""}
    fs.update(err_n=0, risk={"month": "2026-10", "pnl": 100, "cap": 800, "blocked": False},
              lots={"alarm": [], "pills": {"day": "1 口", "night": "1 口"}, "phone": {"label": "1＋1", "short": "x", "todo": []}})
    fs["days"] = [_trade("2026-10-19"), {"date": "2026-10-16", "rec": "skip", "why": "no_long", "cands": {}}]
    fs["real"] = {}
    nf.update(errors=0, recent=[{"E": "2026-10-16", "rec": "skip", "why": "not_fast"}], real={}, tonight={"E": "2026-10-19", "look_at": "21:40"})
    return F


SC7 = {}
# A 日盤 13:43 平倉失敗、一直沒平，過了午夜（週二 01:00；交易日還是週一 10-19）
F = _base7(); fs = F["/api/fire/state"]
fs.update(today="2026-10-20", now="01:00:00")
fs["days"][0]["eod"] = {"why": "eod_failed", "why_msg": "x", "alarm": True}
fs["real"]["2026-10-19"] = {"state": "none"}
F["/api/nightfire/state"]["recent"].insert(0, {"E": "2026-10-19", "rec": "skip", "why": "not_fast"})
F["/api/state"]["real"]["position"] = {"dir": "long", "qty": 1, "entry": 26842, "entry_time": "09:03:31"}
F["/api/state"]["real"]["float_pts"] = 40
SC7["日盤沒平掉過午夜"] = F
# B 夜盤 04:58 平倉失敗、現在 05:10（那一晚變成「上一晚」）
F = _base7(); fs = F["/api/fire/state"]; nf = F["/api/nightfire/state"]
fs.update(today="2026-10-20", now="05:10:00")
fs["real"]["2026-10-19"] = {"state": "ok", "points": 21, "why": "eod", "exit_time": "13:43:30", "qty": 1}
nf["tonight"] = {"E": "2026-10-20", "look_at": "21:40"}
nf["recent"][:0] = [{"E": "2026-10-19", "rec": "eod", "why": "failed"},
                    {"E": "2026-10-19", "rec": "result", "ok": True, "dir": "long", "entry": 26900, "entry_time": "21:40:03"}]
F["/api/state"]["real"]["position"] = {"dir": "long", "qty": 1, "entry": 26900, "entry_time": "21:40:03"}
F["/api/state"]["real"]["float_pts"] = 33
SC7["夜盤 04:58 沒平掉"] = F
# C 部位不知道（面板剛開）＋今天日盤那一筆點數還沒對到
F = _base7(); F["/api/fire/state"]["now"] = "14:10:00"; F["/api/state"]["real"].pop("position")
F["/api/fire/state"]["real"]["2026-10-19"] = {"state": "none"}
SC7["部位不知道"] = F
# D 日盤持有中（正常，13:43 前）
F = _base7(); F["/api/fire/state"]["now"] = "10:00:00"; F["/api/fire/state"]["real"]["2026-10-19"] = {"state": "open"}
F["/api/state"]["real"]["position"] = {"dir": "long", "qty": 1, "entry": 26842, "entry_time": "09:03:31"}
SC7["日盤持有中"] = F
# E 對照組：確定沒部位、那一筆點數還沒對到 ⇒ 這時候寫「已出場」是對的（證明量尺抓得到這兩個字）
F = _base7(); F["/api/fire/state"]["now"] = "14:10:00"; F["/api/fire/state"]["real"]["2026-10-19"] = {"state": "none"}
F["/api/state"]["real"]["position"] = None
SC7["對照：確定沒部位"] = F

S7 = {}
for _k, _F in SC7.items():
    MP._get = lambda port, path, _F=_F: copy.deepcopy(_F[path]) if path in _F else (_ for _ in ()).throw(RuntimeError("沒有"))
    S7[_k] = MP.snapshot(0)
for _k in ("日盤沒平掉過午夜", "夜盤 04:58 沒平掉"):
    _t = S7[_k]["todo"]
    say(_t and _t[0]["lvl"] == "now" and _t[0]["text"] == "收盤沒平掉，部位還在" and _t[0]["act"] == "立刻用大戶投平倉",
        "  %s ⇒ 第一則「現在：收盤沒平掉，部位還在 → 立刻用大戶投平倉」" % _k, [(x["lvl"], x["text"]) for x in _t])
say(not any(x["text"] == "收盤沒平掉，部位還在" for x in S7["日盤持有中"]["todo"]), "  日盤持有中、還沒到 13:43 ⇒ 不叫")
say("前自動平" not in S7["日盤沒平掉過午夜"]["real"]["position"]["short"]
    and "前自動平" not in S7["夜盤 04:58 沒平掉"]["real"]["position"]["short"], "  過了收盤還抱著 ⇒ 不寫「前自動平」（用面板的鐘判）")

# 畫面：真的用 headless Chrome 畫出來，全展開，量「已出場」（這台沒有 node ⇒ 用 Chrome；找不到就明講跳過）
CHROME = next((c for c in (r"C:/Program Files/Google/Chrome/Application/chrome.exe",
                           r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe") if os.path.exists(c)), None)
if not CHROME:
    print("  SKIP 找不到 Chrome／Edge ⇒ 畫面那一半沒驗（後端那一半上面已驗）")
else:
    _salt = b"__test_monitor_7"
    _ke, _km = MP.derive("__測試__", _salt, 1000)
    _K = {"salt": _salt, "iter": 1000, "k_enc": _ke, "k_mac": _km}
    _LS = json.dumps({"salt": base64.b64encode(_salt).decode(), "iter": 1000,
                      "enc": base64.b64encode(_ke).decode(), "mac": base64.b64encode(_km).decode()})
    _ROOT = HERE.parent.parent
    _HJS = ("(function(){var sc=new URLSearchParams(location.search).get('sc');localStorage.clear();"
            "localStorage.setItem('tlmon.key.v1',%s);var of=fetch.bind(window);"
            "window.fetch=function(u,o){u=String(u&&u.url||u);if(/monitor\\.json/.test(u))return of('/__box?sc='+sc);"
            "if(/github/.test(u))return Promise.reject(new Error('x'));return of(u,o)};"
            "try{navigator.serviceWorker.register=function(){return Promise.reject(1)}}catch(e){}"
            "var iv=setInterval(function(){var b=document.getElementById('board');if(!b||!b.children.length)return;clearInterval(iv);"
            "document.querySelectorAll('details').forEach(function(d){d.open=true});"
            "var p=document.createElement('pre');p.id='__res';p.textContent=b.innerText+'\\n'+document.getElementById('more').innerText;"
            "document.body.appendChild(p)},100)})();") % json.dumps(_LS)

    class _H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(_ROOT), **k)

        def log_message(self, *a):
            pass

        def _send(self, body, ct):
            b = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            from urllib.parse import urlparse, parse_qs
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/__box":
                return self._send(json.dumps(MP.seal(S7[q["sc"][0]], _K, t=_time.time() - 60)), "application/json")
            if u.path == "/__h.js":
                return self._send(_HJS, "application/javascript")
            if u.path == "/index.html" and "sc" in q:
                h = (_ROOT / "index.html").read_text(encoding="utf-8").replace("<head>", '<head><script src="/__h.js"></script>', 1)
                return self._send(h, "text/html; charset=utf-8")
            return super().do_GET()

    _srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=_srv.serve_forever, daemon=True).start()
    _prof = HERE / "__tmp__test_monitor_prof"
    from urllib.parse import quote  # noqa: E402
    TXT7 = {}
    for _k in S7:
        _o = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--user-data-dir=" + str(_prof),
                             "--host-resolver-rules=MAP fonts.googleapis.com 127.0.0.1:9, MAP fonts.gstatic.com 127.0.0.1:9",
                             "--virtual-time-budget=8000", "--dump-dom",
                             "http://127.0.0.1:%d/index.html?sc=%s" % (_srv.server_address[1], quote(_k))],
                            capture_output=True, timeout=120)
        _m = re.search(r'<pre id="__res">(.*?)</pre>', _o.stdout.decode("utf-8", "replace"), re.S)
        TXT7[_k] = _m.group(1) if _m else None
    _srv.shutdown()
    import shutil  # noqa: E402
    shutil.rmtree(_prof, ignore_errors=True)
    say(all(TXT7.values()), "  五個情境都畫得出來", [k for k, v in TXT7.items() if not v])
    for _k, _v in TXT7.items():
        if _k.startswith("對照"):
            say(bool(_v) and "已出場" in _v, "  對照組（確定沒部位）有寫「已出場」⇒ 量尺是活的")
        else:
            say(bool(_v) and "已出場" not in _v, "  ⛔ %s：任何一列都沒有「已出場」" % _k, (_v or "")[:160].replace("\n", "｜"))
    # ⭐ 2026-10-21「今天」改成程式下單卡（只看今天日盤＋今晚夜盤）⇒ 沒平掉的那一口改看「現在」那一格
    say(re.search(r"日盤持有中\s*\+40", TXT7["日盤沒平掉過午夜"] or "") is not None, "  日盤沒平掉過午夜 ⇒「現在」寫日盤持有中 +40")
    say(re.search(r"夜盤持有中\s*\+33", TXT7["夜盤 04:58 沒平掉"] or "") is not None, "  夜盤 04:58 沒平掉 ⇒「現在」寫夜盤持有中 +33")
    say("不知道" in (TXT7["部位不知道"] or ""), "  部位不知道 ⇒ 那一列「不知道」")
    for _k in ("日盤沒平掉過午夜", "夜盤 04:58 沒平掉"):
        print("    畫面文字［%s］：%s" % (_k, (TXT7[_k] or "").replace("\n", "｜")))
MP._get = _real_get

print("\n" + ("全部通過 ✅" if FAIL == 0 else f"⛔ 有 {FAIL} 項沒過"))
sys.exit(1 if FAIL else 0)
