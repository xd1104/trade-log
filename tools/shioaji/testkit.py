# -*- coding: utf-8 -*-
"""
共用測試工具（2026-11-12，健檢報告整理第 1 批後半）。

① sandbox()：把各模組「真的資料檔／開關檔」的路徑常數一次導到暫存資料夾。
   09-01 那次假交易寫進真成績單，就是測試忘了導其中一個。以前每支測試自己導、各導各的（盤點時 28 支有漏），
   現在 run_tests.py 會在每支測試開跑**之前**先叫 sandbox() ⇒ 就算測試自己漏導，也寫不到真的檔。
   開關檔（REAL_ORDERS_ON、AUTO_ORDERS_ON、NIGHT_ORDERS_ON、MINI_ON…）導走之後預設「不存在」＝全部關著，最安全；
   要開的測試自己在暫存區建。測試自己再改常數，照樣以測試的為準。
② FakeClock：假時鐘（.t 秒數、.now()、.time()、advance()），新測試用它，不用每支自己寫一個。

⛔ 這支只給測試用：下單模組不准 import 它。
"""
import atexit
import importlib
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent

# {模組: [路徑常數, ...]} —— ⭐ 新增會寫檔的模組／常數，要加在這裡（test_testkit.py 會檢查沒有漏）
REDIRECT = {
    "broker": ["REAL_FLAG", "ORDER_DIR", "TRADE_DIR", "MINI_FLAG"],
    "auto_fire": ["ARM_FLAG", "FIRE_DIR", "FAST_HIST", "ORB_HIST"],
    "night_fire": ["ARM_FLAG", "NF_DIR"],
    "lots": ["LEVEL_FILE", "LOG_DIR"],
    "risk_cap": ["FIRE_DIR", "NF_DIR", "TRADE_DIR", "OVERRIDE_FLAG"],
    "decision_log": ["LOG_DIR"],
    "sim_lanes": ["SIM_DIR", "TSM_CTX", "TREND_CTX", "MIN1_CSV"],
    "monitor_push": ["KEY_FILE", "HC_FILE", "LOG_FILE"],
    "analyst": ["DIR", "FACTS_DIR", "REPORTS_DIR", "READ_FILE"],
    "live_panel": ["TRADE_DIR", "REPLAY_DIR", "AUTO_DIR", "AUTO_REAL_DIR", "RISK_LOG_DIR", "EQUITY_DIR", "USAGE_DIR",
                   "TICK_DIR", "DEPTH_DIR", "AUTO_RESTART_FILE", "SYNC_FILE"],
}

# 這些模組自己只讀、不寫（寫它的是別的模組，那邊已經導走）⇒ 不導，測試照樣讀得到真的資料
READONLY = {
    "auto_fire": ["TICK_DIR"],          # 寫的是 live_panel
    "decision_log": ["TICK_DIR"],
    "night_fire": ["TSM_CTX", "TREND_CTX"],   # 寫的是 sim_lanes
    "sim_lanes": ["FAST_HIST"],         # 寫的是 auto_fire
}

_STATE = {"root": None, "moved": {}}


def sandbox():
    """⇒ 暫存資料夾。重複叫只做一次。回傳前確認每一個常數都已經不在真的資料夾裡（⛔ 沒導成就丟例外，不讓測試開跑）。"""
    if _STATE["root"] is not None:
        return _STATE["root"]
    root = Path(tempfile.mkdtemp(prefix="trade-test-"))
    atexit.register(shutil.rmtree, root, True)
    for mod_name, attrs in REDIRECT.items():
        mod = importlib.import_module(mod_name)
        for a in attrs:
            cur = getattr(mod, a, None)
            if not isinstance(cur, Path):
                raise RuntimeError("testkit：%s.%s 不是路徑（改名了？要同步改 REDIRECT）" % (mod_name, a))
            new = root / mod_name / cur.name
            if cur.suffix == "" and not cur.name.isupper():      # 資料夾（開關檔是全大寫、沒有副檔名）
                new.mkdir(parents=True, exist_ok=True)
            else:
                new.parent.mkdir(parents=True, exist_ok=True)
            setattr(mod, a, new)
            _STATE["moved"]["%s.%s" % (mod_name, a)] = (cur, new)
    left = still_real()
    if left:
        raise RuntimeError("testkit：這些還指著真的資料夾：%s" % "、".join(left))
    _STATE["root"] = root
    return root


def still_real():
    """⇒ 還指著真的資料夾（tools/shioaji 底下）的常數名稱。"""
    out = []
    for mod_name, attrs in REDIRECT.items():
        mod = importlib.import_module(mod_name)
        for a in attrs:
            p = getattr(mod, a, None)
            if isinstance(p, Path):
                try:
                    p.resolve().relative_to(HERE)
                    out.append("%s.%s" % (mod_name, a))
                except ValueError:
                    pass
    return out


PAGE_LINE = 'PAGE = (HERE / "panel_page.html").read_text(encoding="utf-8")'


def panel_source(lp_file=None):
    """
    ⭐ 2026-11-12 整理第 3 批：網頁搬到 panel_page.html 之後，把「主程式＋網頁」組回搬家前的單一原始碼
    （PAGE = r\"\"\"…\"\"\"）。測試拿它找網頁字串、ast.parse 都照舊；突變工具也拿它寫出自給自足的單檔突變版。
    lp_file 沒給 ⇒ tools/shioaji/live_panel.py；網頁檔一律找 lp_file 旁邊的 panel_page.html，找不到就用正本那一份。
    已經是單檔（舊版／突變版）⇒ 原樣回傳。
    """
    lp = Path(lp_file) if lp_file else HERE / "live_panel.py"
    src = lp.read_text(encoding="utf-8")
    if PAGE_LINE not in src:
        return src
    page_f = lp.parent / "panel_page.html"
    if not page_f.exists():
        page_f = HERE / "panel_page.html"
    page = page_f.read_text(encoding="utf-8")
    return src.replace(PAGE_LINE, 'PAGE = r"""' + page + '"""', 1)


class FakeClock:
    """假時鐘：clk = FakeClock(datetime(2026, 6, 10, 21, 35)); clk.advance(5); clk.now(); clk.time()。"""

    def __init__(self, start=None):
        self.t = (start or datetime(2026, 1, 5, 9, 0)).timestamp()

    def time(self):
        return self.t

    def now(self):
        return datetime.fromtimestamp(self.t)

    def advance(self, seconds):
        self.t += seconds
        return self
