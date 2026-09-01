# -*- coding: utf-8 -*-
"""
把面板整組關掉再開起來，然後告訴你載入的是哪一版。

【為什麼需要這支】關掉視窗**不等於**重開面板。`panel_app.pyw` 開視窗前會先看
伺服器活著沒、活著就直接接上去 —— 所以關視窗再點一次，接到的還是同一個舊伺服器，
改過的程式根本沒載入（2026-09-01 就這樣白重開一次，還以為是新版）。

用法（PowerShell 或 Run 按鈕都可以，整行沒有 $ 符號，不會被殼吃掉）：
    .venv\\Scripts\\python.exe tools\\shioaji\\restart-panel.py
    .venv\\Scripts\\python.exe tools\\shioaji\\restart-panel.py --dry    看看會關掉誰，不真的關

⚠️ 只會關命令列裡有 live_panel.py 或 panel_app.pyw 的行程，不碰別的東西。
"""
import json
import pathlib
import socket
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
DRY = "--dry" in sys.argv
PORT = 8770
MARKS = ("live_panel.py", "panel_app.pyw")

PS = ("Get-CimInstance Win32_Process | "
      "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")


def procs():
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", PS],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        rows = json.loads(out.stdout or "[]")
    except json.JSONDecodeError:
        print("讀不到行程清單，請改用工作管理員結束 python.exe")
        return []
    if isinstance(rows, dict):
        rows = [rows]
    hits = []
    for r in rows:
        cl = r.get("CommandLine") or ""
        if any(m in cl for m in MARKS):
            hits.append((r["ProcessId"], cl))
    return hits


def port_busy():
    with socket.socket() as sk:
        sk.settimeout(0.4)
        return sk.connect_ex(("127.0.0.1", PORT)) == 0


hits = procs()
print(f"找到 {len(hits)} 個面板相關行程：")
for pid, cl in hits:
    which = next(m for m in MARKS if m in cl)
    print(f"  PID {pid:<8} {which}")

if DRY:
    print("\n--dry：什麼都沒關。拿掉 --dry 才會真的重開。")
    sys.exit(0)

for pid, _ in hits:
    subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                   capture_output=True, text=True)

for _ in range(40):                       # 等連接埠真的放開，不然新的起不來
    if not port_busy():
        break
    time.sleep(0.25)
else:
    print(f"\n⛔ 連接埠 {PORT} 一直沒放開，舊的可能還活著。請用工作管理員看一下 python.exe")
    sys.exit(1)
print("\n舊的關乾淨了，重新開一個…")

app = HERE / "panel_app.pyw"
pyw = pathlib.Path(sys.executable).with_name("pythonw.exe")
subprocess.Popen([str(pyw), str(app)], cwd=str(HERE),
                 creationflags=0x00000008 | 0x08000000)   # DETACHED_PROCESS | NO_WINDOW

for _ in range(80):                       # 等它把伺服器叫起來
    if port_busy():
        break
    time.sleep(0.25)
else:
    print("⛔ 等不到面板起來。請直接點桌面的「早盤儀表板」。")
    sys.exit(1)

# 讀版本指紋 —— 這就是「到底載入哪一版」的答案，不要再用猜的
import urllib.request

for _ in range(20):
    try:
        s = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/state", timeout=3))
        break
    except Exception:
        time.sleep(0.5)
else:
    print("面板起來了，但還沒回狀態。等幾秒再看畫面。")
    sys.exit(0)

r = s.get("real") or {}
code = r.get("code")
print("\n面板起來了。")
if not code:
    print("⛔ 狀態裡沒有版本指紋 ⇒ **還是舊版**，改的東西沒生效。")
    sys.exit(1)
print(f"  程式 broker.py {code['broker']}　面板 {code['panel']}　啟動 {code['started']}")
print(f"  模式：{'真的會送單' if r.get('live') else '演練（不會送出）'}")
print(f"  部位：{r.get('position') or '空手'}")
print(f"  今天真實進場：{r.get('entries_today')} / {r.get('max_entries')}")
