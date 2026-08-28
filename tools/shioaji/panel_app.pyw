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
import json
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
IDLE_CLOSE = 12            # 秒。超過這麼久沒有瀏覽器來要資料就當作視窗關了

NO_WINDOW = 0x08000000     # CREATE_NO_WINDOW
NEW_GROUP = 0x00000200     # CREATE_NEW_PROCESS_GROUP

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

_stop = threading.Event()


def installed_app_id():
    """
    裝好的應用程式 id，沒裝就回 None。

    【不要自己算】原本是照 Chromium 那套「SHA-256 前 16 bytes、每個 nibble 映成 a~p」
    去推，實測跟 Edge 實際用的對不起來（算出 kkbjodnh…，Edge 用的是 gflghkeo…），
    於是永遠判定成「沒安裝」。直接去設定檔裡看它建了什麼才是可靠的：
    Edge 安裝後會留下 Web Applications/_crx__<id> 與 Manifest Resources/<id>。
    """
    root = os.path.join(PROFILE, "Default", "Web Applications")
    for base in (root, os.path.join(root, "Manifest Resources")):
        if not os.path.isdir(base):
            continue
        try:
            names = os.listdir(base)
        except OSError:
            continue
        for name in names:
            aid = name[6:] if name.startswith("_crx__") else name
            if len(aid) == 32 and aid.isalpha() and aid.islower():
                return aid
    return None


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


def idle_seconds():
    """面板那邊有多久沒有瀏覽器來要資料了。問不到就回 None。"""
    try:
        with urllib.request.urlopen(URL + "api/idle", timeout=3) as r:
            return json.loads(r.read()).get("idle")
    except Exception:
        return None


def wait_until_closed():
    """
    等到視窗被關掉。

    【不能等 Edge 那個行程結束】用 --app-id 開已安裝的應用程式時，我們啟動的那個
    行程會立刻交棒給既有的 Edge 行程然後自己結束 —— 實測 1 秒就被誤判成「關窗」，
    於是伺服器被收掉，畫面就變成「127.0.0.1 拒絕連線」（Benson 2026-08-28 遇到的）。
    改成問面板「多久沒人來要資料了」：開著的視窗每 0.5 秒就會要一次。
    """
    grace = time.time() + 40          # 先給視窗開起來、開始輪詢的時間
    while not _stop.is_set():
        time.sleep(2)
        idle = idle_seconds()
        if idle is None:              # 伺服器不見了 —— 看門狗會處理，這裡繼續等
            continue
        if time.time() < grace:
            continue
        if idle > IDLE_CLOSE:
            log(f"已經 {idle:.0f} 秒沒有人在看，視為視窗已關閉")
            return


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
    else:
        # 【一律用 --app=URL】試過 --app-id 與 Edge 自己捷徑用的 msedge_proxy
        # （--profile-directory=Default --app-id=… --app-url=…），在這個專屬設定檔底下
        # 兩種都**開不出視窗**（實測：沒有任何視窗、面板那邊 idle 一直是 null），
        # 而 --app=URL 一開就出來、頁面也開始輪詢。裝過之後 Edge 會自己把這個網址
        # 認回已安裝的應用程式，所以圖示照樣是我們的。
        args += [f"--app={URL}", "--window-size=1520,980"]
        aid = installed_app_id()
        log(f"開啟視窗（{'已安裝 ' + aid if aid else '尚未安裝'}）")
    subprocess.Popen(args, creationflags=NO_WINDOW)
    log("視窗已開，等它關閉")
    wait_until_closed()

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
