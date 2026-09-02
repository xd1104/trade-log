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
import ctypes
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


def server_is_stale():
    """
    正在跑的那個伺服器，是不是比硬碟上的程式舊。

    【為什麼要有這個】關視窗**不等於**重開面板 —— 下面 main() 看到伺服器活著就直接
    接上去（用意是不要每次開窗都重連永豐、等一分鐘）。副作用是改過程式之後，
    關視窗再點一次，接到的還是那個舊伺服器，**新程式永遠載不進來，畫面上還完全看不出來**
    （2026-09-01 就這樣白重開一次，以為在測新版）。
    現在面板會自己回報 `code.stale`（硬碟上的檔案比行程新），據此先把舊的收掉。
    """
    try:
        with urllib.request.urlopen(URL + "api/state", timeout=3) as r:
            s = json.load(r)
    except Exception as e:
        log(f"問不到伺服器版本（{e}），當作不用重開")
        return False
    code = (s.get("real") or {}).get("code")
    if code is None:
        return True          # 連版本指紋都沒有 ⇒ 舊到還沒有這個欄位，一定要換掉
    return bool(code.get("stale"))


def running_panels():
    """正在跑的 live_panel.py 行程（不含自己）。只認命令列，不亂猜。"""
    ps = "Get-CimInstance Win32_Process | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", creationflags=NO_WINDOW, timeout=40)
        rows = json.loads(out.stdout or "[]")
    except Exception as e:
        log(f"列不出行程：{e}")
        return []
    if isinstance(rows, dict):
        rows = [rows]
    me = os.getpid()
    return [r["ProcessId"] for r in rows
            if r.get("ProcessId") != me and "live_panel.py" in (r.get("CommandLine") or "")]


def kill_stale_server():
    """把舊的伺服器收乾淨，等連接埠真的放開。"""
    pids = running_panels()
    log(f"舊伺服器比程式舊，收掉 {pids}")
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                       capture_output=True, creationflags=NO_WINDOW)
    for _ in range(40):
        if not alive():
            return True
        time.sleep(0.25)
    log("等不到舊伺服器退場，只好接著用它")
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


def window_box(want_w, want_h):
    """
    算出視窗要多大、擺在哪 —— **置中**，而且保證整個視窗都在螢幕的可用區內。

    【為什麼要自己算】不給 x/y 的話 pywebview 會用系統預設位置，每次開都不一樣，
    常常黏在左上角或跨到螢幕外（Benson 2026-09-02 截圖：視窗貼在左上、右邊切掉）。

    ⛔ 【一定要先設 DPI 感知再量】pywebview 自己會在建立視窗時呼叫 `SetProcessDPIAware()`
       （`webview/platforms/winforms.py`），之後它的座標是**實體像素**。
       我們如果在那之前量，系統回的是**被虛擬化過的邏輯像素**（他的機器縮放 125%：
       量到 1536×816，實際是 1920×1040）—— 兩套座標差 1.25 倍，算出來的中心會整個偏掉。
       所以這裡先自己呼叫一次（pywebview 再呼叫是無害的），確保兩邊同一套。
    ⚠️ 用「工作區」不是整個螢幕：扣掉工作列，視窗才不會被工作列蓋住底部。

    【他遇到的其實是這個】原本寫死 1520×980，而他的可用高度只有 1040 —— 視窗比螢幕
    還高，Windows 只好隨便擺，右邊與底部就被切掉了（2026-09-02 截圖）。所以除了置中，
    **一定要夾住尺寸**。
    """
    try:
        u32 = ctypes.windll.user32
        try:
            u32.SetProcessDPIAware()
        except Exception:
            pass
        # 可用工作區（扣掉工作列）。SPI_GETWORKAREA = 0x0030
        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        r = RECT()
        if not u32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):
            raise OSError("SystemParametersInfoW 失敗")
        aw, ah = r.right - r.left, r.bottom - r.top
        # 螢幕比視窗小就縮到塞得下（留一點邊，不要頂滿）
        w = max(900, min(want_w, aw - 40))
        h = max(640, min(want_h, ah - 40))
        x = r.left + max(0, (aw - w) // 2)
        y = r.top + max(0, (ah - h) // 2)
        log(f"視窗 {w}x{h} @ ({x},{y})　工作區 {aw}x{ah}（實體像素）")
        return w, h, x, y
    except Exception as e:
        # 算不出來就退回原本的行為（讓系統決定位置），不要因為擺位失敗就開不了視窗
        log(f"算不出視窗位置（{e}），用系統預設")
        return want_w, want_h, None, None


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

    w, h, x, y = window_box(1520, 980)
    webview.create_window("早盤儀表板", URL, width=w, height=h, x=x, y=y,
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

    # 接上去之前先確認它跑的是不是最新的程式；不是就先收掉，下面會開一個新的。
    if alive() and server_is_stale():
        kill_stale_server()

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
