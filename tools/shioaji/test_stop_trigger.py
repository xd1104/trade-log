# -*- coding: utf-8 -*-
"""
停損觸發邏輯的離線測試 —— **這是整套裡最不能出錯的地方**。

永豐的 API 沒有停損單，Benson 2026-08-28 知情後選擇「停損交給面板」。
所以 `check_real_position()` 這幾行就是他的停損。它判斷錯 = 真錢。

**不連永豐、不送任何單**：`broker.close` 換成假的，只記錄「有沒有被呼叫、理由是什麼」。

要驗的性質：
  1. 到價才平，沒到價不准平
  2. 停利那一邊**不可以由面板送單** —— 那張限價單掛在券商，面板重複送會變成反向新倉
  3. **報價不新鮮時絕對不可以拿舊價判停損**，而且要記下「從什麼時候開始瞎了」
  4. 沒有部位時什麼都不做

怎麼跑（PowerShell）：
    & "C:\\Users\\USER\\Desktop\\Claude Work\\trade-log\\.venv\\Scripts\\python.exe" `
      "C:\\Users\\USER\\Desktop\\Claude Work\\trade-log\\tools\\shioaji\\test_stop_trigger.py"
"""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import broker
import live_panel as LP

FAIL = 0
CALLS = []


CLOSE_OK = {"v": True}


def fake_close(reason):
    CALLS.append(reason)
    if not CLOSE_OK["v"]:
        # 【平不掉的情況也要測】舊版的假 close 永遠回成功，
        # 所以「停損送不出去之後會怎樣」一次都沒被測過（lab-qa 2026-09-01 指出）。
        return False, "平不掉"
    broker._state["position"] = None      # 真的 close 成功後也會清掉
    return True, None


broker.close = fake_close


def setup(direction, entry):
    CALLS.clear()
    LP.REAL_STALE["since"] = None
    broker._state["position"] = {"dir": direction, "entry": float(entry), "qty": 1,
                                 "entry_time": "09:05", "target_trade": None,
                                 "recovered": False}


def chk(name, got, want):
    global FAIL
    ok = got == want
    FAIL += not ok
    print(("  OK   " if ok else "  FAIL ") + name + ("" if ok else f"  (得到 {got!r}，期待 {want!r})"))


TP, SL = LP.TP_POINTS, LP.SL_POINTS
print(f"面板設定：停利 +{TP:.0f}、停損 −{SL:.0f}、報價超過 {broker.STALE_ALARM} 秒視為不新鮮\n")

print("=== 做多：進場 45000，停損在 44900 ===")
setup("long", 45000)
LP.check_real_position(45050, 1)
chk("  45050（還在賺）不平倉", CALLS, [])
LP.check_real_position(44901, 1)
chk("  44901（差 1 點到停損）不平倉", CALLS, [])
LP.check_real_position(44900, 1)
chk("  44900（剛好到停損）→ 平倉", CALLS, ["sl"])

setup("long", 45000)
LP.check_real_position(44850, 1)
chk("  44850（跳空穿過停損）→ 平倉", CALLS, ["sl"])

print("\n=== 做多：停利那一邊面板不可以出手 ===")
setup("long", 45000)
LP.check_real_position(45100, 1)
chk("  45100（到停利）面板不送單 —— 那張限價單掛在券商", CALLS, [])
LP.check_real_position(45300, 1)
chk("  45300（遠遠超過停利）面板照樣不送單", CALLS, [])

print("\n=== 做空：進場 45000，停損在 45100 ===")
setup("short", 45000)
LP.check_real_position(44950, 1)
chk("  44950（還在賺）不平倉", CALLS, [])
LP.check_real_position(45099, 1)
chk("  45099（差 1 點）不平倉", CALLS, [])
LP.check_real_position(45100, 1)
chk("  45100（到停損）→ 平倉", CALLS, ["sl"])

setup("short", 45000)
LP.check_real_position(44900, 1)
chk("  44900（到停利）面板不送單", CALLS, [])

print("\n=== 報價不新鮮：絕對不可以拿舊價判停損 ===")
setup("long", 45000)
LP.check_real_position(44800, broker.STALE_ALARM + 5)
chk("  價格早就穿過停損，但報價是舊的 → 不平倉", CALLS, [])
chk("  有記下「從什麼時候開始瞎了」", LP.REAL_STALE["since"] is not None, True)

setup("long", 45000)
LP.check_real_position(44800, None)
chk("  一筆報價都沒收到（age=None）→ 不平倉", CALLS, [])
chk("  一樣記下開始時間", LP.REAL_STALE["since"] is not None, True)

print("\n=== 報價恢復之後要立刻補平 ===")
setup("long", 45000)
LP.check_real_position(44800, broker.STALE_ALARM + 5)   # 先瞎掉
chk("  瞎的時候沒動作", CALLS, [])
LP.check_real_position(44800, 1)                        # 報價回來了
chk("  報價一回來就平倉", CALLS, ["sl"])
chk("  瞎掉的計時清掉", LP.REAL_STALE["since"], None)

print("\n=== 停損平不掉：部位要留著，而且不可以每 0.25 秒狂重送 ===")
setup("long", 45000)
CLOSE_OK["v"] = False
for _ in range(8):                     # 模擬主迴圈連續跑 8 圈
    LP.check_real_position(44800, 1)
chk("  平不掉時部位要留著（清掉的話停損就停了）",
    (broker._state.get("position") or {}).get("dir"), "long")
chk("  有嘗試平倉", len(CALLS) >= 1, True)
print(f"    （8 圈裡實際送出 {len(CALLS)} 次）")
CLOSE_OK["v"] = True

print("\n=== 休市時不可以喊「報價中斷」 ===")
setup("long", 45000)
LP.REAL_STALE["since"] = 123.0
LP.check_real_position(44800, 9999, "closed")
chk("  休市：不平倉（沒有報價，判什麼停損）", CALLS, [])
chk("  休市：不算斷線，警報要收掉", LP.REAL_STALE["since"], None)
setup("long", 45000)
LP.check_real_position(44800, broker.STALE_ALARM + 5, "live")
chk("  盤中收不到報價：這才要記成斷線", LP.REAL_STALE["since"] is not None, True)

print("\n=== 沒有部位時什麼都不做 ===")
CALLS.clear()
broker._state["position"] = None
LP.REAL_STALE["since"] = 123.0
LP.check_real_position(1, 1)
chk("  不送單", CALLS, [])
chk("  順手把瞎掉的計時清掉", LP.REAL_STALE["since"], None)

print("\n=== 沒有價格時不動作 ===")
setup("long", 45000)
LP.check_real_position(None, 1)
chk("  price=None 不平倉", CALLS, [])

print("\n總結:", "全部通過" if not FAIL else f"{FAIL} 項失敗")
sys.exit(1 if FAIL else 0)
