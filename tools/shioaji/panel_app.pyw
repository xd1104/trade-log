# -*- coding: utf-8 -*-
"""
早盤儀表板・桌面 App 啟動器（Windows 11）

用 pythonw.exe 跑，所以不會有黑色的命令列視窗。做三件事：

  1. 伺服器還沒起來就把 live_panel.py 開起來（隱藏視窗），並看門狗顧著它 ——
     永豐 SDK 斷線時會把整個行程帶走，這點跟 start-panel.bat 一樣不能少。
  2. 用 Edge 的 app 模式開一個沒有網址列、沒有分頁的專屬視窗，
     `--user-data-dir` 給它自己的設定檔，工作列才會是獨立一顆、不會跟 Edge 混在一起。
  3. 關掉視窗＝離開 App：把伺服器一起收掉（只有這個 App 開起來的才收）。

【為什麼不是 Electron】那要多裝 Node、打包出來 150MB 起跳，
而 Edge 是 Windows 11 內建的，這樣做零安裝、開得快，出事也只有這一個檔要看。
"""
import hashlib
import os
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
PYTHON = os.path.join(REPO, ".venv", "Scripts", "python.exe")
PANEL = os.path.join(HERE, "live_panel.py")
PROFILE = os.path.join(os.environ.get("LOCALAPPDATA", HERE), "MorningPanelApp")
LOG = os.path.join(HERE, "panel-app.log")
URL = "http://127.0.0.1:8770/"
BOOT_TIMEOUT = 90          # 秒。第一次啟動要連永豐、載歷史矩陣，慢的時候要一分鐘

NO_WINDOW = 0x08000000     # CREATE_NO_WINDOW
NEW_GROUP = 0x00000200     # CREATE_NEW_PROCESS_GROUP

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

# Chromium 從 start_url 推出來的應用程式 id：SHA-256 前 16 bytes，每個 nibble 映成 a~p。
# 安裝之後就是用這個 id 啟動，Windows 才會把它當成一支獨立的應用程式（圖示、工作列、
# 「已安裝的應用程式」清單都跟著走）；只用 --app 開的話一律算在 Edge 頭上。
APP_ID = "".join(chr(ord("a") + (b >> 4)) + chr(ord("a") + (b & 15))
                 for b in hashlib.sha256(URL.encode()).digest()[:16])

_stop = threading.Event()


def installed():
    """這個設定檔裡裝過這支應用程式沒有。Edge 會把它的資料放在 Web Applications 底下。"""
    root = os.path.join(PROFILE, "Default", "Web Applications")
    if not os.path.isdir(root):
        return False
    for dirpath, dirnames, filenames in os.walk(root):
        if APP_ID in os.path.basename(dirpath) or any(APP_ID in f for f in filenames):
            return True
    return False


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def alive():
    """伺服器起來了沒。用 /api/state 而不是首頁 —— 首頁是靜態字串，還沒備妥也回得了。"""
    try:
        with urllib.request.urlopen(URL + "api/state", timeout=2):
            return True
    except Exception:
        return False


def edge():
    for p in EDGE_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def watchdog(started):
    """
    看門狗：程式結束就重開。跟 start-panel.bat 同一套道理 ——
    永豐 SDK 在斷線時會把整個行程帶掉（2026-08-12 19:17，沒有 traceback）。
    離開碼 2 ＝ 這個埠已經有面板在跑，那就不要再開了。
    """
    while not _stop.is_set():
        p = subprocess.Popen([PYTHON, PANEL, "--no-open"], cwd=HERE,
                             creationflags=NO_WINDOW | NEW_GROUP,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        started.append(p)
        code = p.wait()
        if _stop.is_set():
            return
        if code == 2:
            log("已經有面板在跑，看門狗結束")
            return
        log(f"面板結束（離開碼 {code}），10 秒後重開")
        for _ in range(100):
            if _stop.is_set():
                return
            time.sleep(0.1)


def main():
    log("=== 啟動 ===")
    started = []
    owns_server = False

    if not alive():
        if not os.path.exists(PYTHON):
            log(f"找不到 python：{PYTHON}")
            return 1
        owns_server = True
        threading.Thread(target=watchdog, args=(started,), daemon=True).start()
        deadline = time.time() + BOOT_TIMEOUT
        while time.time() < deadline and not alive():
            time.sleep(0.5)
        if not alive():
            log("等不到伺服器起來，還是把視窗開出來讓他看得到錯誤")
    else:
        log("伺服器本來就在跑，只開視窗")

    exe = edge()
    if exe is None:
        log("找不到 Edge，改用預設瀏覽器開")
        import webbrowser
        webbrowser.open(URL)
        return 0

    os.makedirs(PROFILE, exist_ok=True)
    args = [exe, f"--user-data-dir={PROFILE}",
            "--no-first-run", "--no-default-browser-check"]
    if "--install" in sys.argv:
        # 一次性：開一個「有網址列」的普通視窗，網址列右邊會出現安裝鈕。
        # 裝好之後工作列才會是我們的圖示（沒裝的話 Windows 一律算它是 Edge）。
        args += [URL, "--window-size=1200,900"]
        log("安裝模式：開普通視窗讓他按安裝")
    elif installed():
        # 已安裝 → 用 app-id 開，這樣才是「應用程式」，圖示與工作列都歸我們
        args += [f"--app-id={APP_ID}", "--window-size=1520,980"]
        log(f"以已安裝的應用程式開啟（app-id {APP_ID}）")
    else:
        args += [f"--app={URL}", "--window-size=1520,980"]
        log("尚未安裝，先用 app 模式開（工作列圖示會是 Edge 的）")
    win = subprocess.Popen(args, creationflags=NO_WINDOW)
    log("視窗已開，等它關閉")
    win.wait()

    # 關窗＝離開 App。只收自己開的伺服器：本來就在跑的（例如工具面板開的）不要動。
    if owns_server:
        log("視窗關閉，收掉伺服器")
        _stop.set()
        for p in started:
            try:
                p.terminate()
            except Exception:
                pass
        for p in started:
            try:
                p.wait(timeout=8)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
    log("=== 結束 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
