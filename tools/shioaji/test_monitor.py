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
say(cmds == ["hash-object", "mktree", "commit-tree", "push"], "  push() 只呼叫 hash-object／mktree／commit-tree／push", cmds)
say(not any(w in src for w in ('"checkout"', '"add"', '"reset"', '"stash"')), "  ⛔ 沒有 checkout／add／reset／stash")
gi = (HERE.parent.parent / ".gitignore").read_text(encoding="utf-8")
say("tools/shioaji/MONITOR_KEY.json" in gi, "  ⛔ 金鑰檔在 .gitignore 裡")

print("\n=== ⑤ 面板接線 ===")
lp = (HERE / "live_panel.py").read_text(encoding="utf-8")
m = lp[lp.index("def main():"):]
i = m.index("monitor_push.start(PORT)")
say(m.rfind("try:", 0, i) > m.rfind("\n    print(", 0, i) and "except Exception" in m[i:i + 200],
    "  main() 起手機監控包在 try 裡（起不來不影響送單與停損）")

print("\n" + ("全部通過 ✅" if FAIL == 0 else f"⛔ 有 {FAIL} 項沒過"))
sys.exit(1 if FAIL else 0)
