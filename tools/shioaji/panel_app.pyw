# -*- coding: utf-8 -*-
"""
早盤儀表板・桌面 App 啟動器（Windows 11）

用 pythonw.exe 跑，所以不會有黑色的命令列視窗。做三件事：

  1. 伺服器還沒起來就把 live_panel.py 開起來（隱藏視窗），並看門狗顧著它 ——
     永豐 SDK 斷線時會把整個行程帶走，這點跟 start-panel.bat 一樣不能少。
  2. 自己開一個視窗（pywebview，底層是 Windows 11 內建的 WebView2）。
     視窗是這個行程的，所以工作列顯示的是我們的圖示，關閉時機也拿得準。
  3. 關掉視窗＝離開 App：把伺服器一起收掉（只有這個 App 開起來的才收）。

【為什麼不是 Electron】那要多裝 Node、打包出來 150MB 起跳。
【為什麼不用 Edge 的 --app】視窗擁有者是 Edge ⇒ 工作列一律顯示 Edge 的圖示
（安裝成 PWA 也一樣），而且關窗時機測不準。詳見 open_window() 的說明。
"""
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
ICON = os.path.join(HERE, "panel.ico")
URL = "http://127.0.0.1:8770/"
BOOT_TIMEOUT = 90          # 秒。第一次啟動要連永豐、載歷史矩陣，慢的時候要一分鐘

NO_WINDOW = 0x08000000     # CREATE_NO_WINDOW
NEW_GROUP = 0x00000200     # CREATE_NEW_PROCESS_GROUP


_stop = threading.Event()


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


def open_window():
    """
    自己開一個視窗，不要交給 Edge。

    【為什麼不用 Edge 的 --app】那個視窗的擁有者是 Edge，Windows 就把它算在 Edge 頭上：
    工作列顯示的是 Edge 的圖示（Benson 2026-08-28 確認過，安裝成 PWA 也一樣）。
    而且沒辦法可靠地知道「視窗被關掉了」——
      ・--app-id 啟動的行程 1 秒就自己結束，等它＝一開就誤判
      ・改看「多久沒人來要資料」也不行：視窗被縮到最小或切到背景時，
        Edge 會把頁面的計時器降頻，看起來就像沒人在看，伺服器會被誤殺
        （實測 24 秒沒輪詢，其實視窗還開著）
    改用 pywebview（底層是 Windows 11 內建的 WebView2）：視窗是我們這個行程的，
    圖示、工作列、關閉時機全都拿得回來，也不必再管 Edge 的規矩。
    """
    import ctypes
    import webview

    # 工作列要認得這是「早盤儀表板」而不是 python.exe，一定要在開視窗之前設
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Benson.MorningPanel")
    except Exception:
        pass

    webview.create_window("早盤儀表板", URL, width=1520, height=980,
                          min_size=(1100, 700))
    kw = {"private_mode": False, "storage_path": PROFILE}
    if os.path.exists(ICON):
        kw["icon"] = ICON
    try:
        webview.start(**kw)          # 視窗關掉才會回來
    except TypeError:
        # 舊版 pywebview 沒有 icon / storage_path，掉回最陽春的用法
        webview.start()


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

    open_window()

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
