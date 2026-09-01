# -*- coding: utf-8 -*-
"""
真實下單。**這是整個專案唯一會動到真錢的地方。**

刻意獨立成一個檔，不跟面板其他幾千行混在一起 —— 要檢查「會不會亂送單」，
只需要讀這一個檔。

================================================================
⛔ 永豐的 API 沒有停損單
================================================================
`FuturesOrder(action, price, quantity, price_type, order_type, octype)`
價格型態只有 LMT / MKT / MKP，委託條件只有 ROD / IOC / FOK，**沒有觸發價**。
（SDK 裡唯一叫 trigger_price 的是報價掃描器的欄位，跟下單無關。）

代表：
- **+100 停利可以掛在券商那邊等**（做多就掛一張高 100 點的賣出限價單），電腦關機也有效。
- **−100 停損做不到券商端**。停損需要「跌到某價才觸發」，這個 API 給不了。

Benson 2026-08-28 知情後決定「停損也交給面板，風險我承擔」。
所以停損活在這台電腦的 Python 迴圈裡 —— 程式死掉／斷線／電腦睡著就沒有停損。
`STALE_ALARM` 那段是在盡量讓他**知道**這件事發生了，不是在消除風險。

================================================================
防呆（每一條都是刻意的，不要為了方便拿掉）
================================================================
1. **預設不送單**：`REAL` 檔案不存在就是 dry run —— 一切照跑、單子照組，就是不送出去。
2. **口數寫死 1 口**，不從介面讀。介面被改壞也不會變成 10 口。
3. **每天最多 MAX_ENTRIES 次進場**，計數落地存檔（重啟不會歸零）。
   這條是防「程式跑掉狂送單」最有效的一道。
4. **同時只能有一個部位**，且送單前一定要跟券商對過帳。
5. **重啟一定重新對帳**（看門狗會重啟，記憶體不可信）。
6. 每一次真實下單都寫進 `real_orders/YYYY-MM-DD.jsonl`，含送出去的完整內容。
"""
import json
import pathlib
import time
import threading
from datetime import date, datetime

HERE = pathlib.Path(__file__).resolve().parent
REAL_FLAG = HERE / "REAL_ORDERS_ON"      # 這個檔存在＝真的送單；不存在＝dry run
ORDER_DIR = HERE / "real_orders"
QTY = 1                                   # 口數。寫死，不從介面讀
MAX_ENTRIES = 3                           # 每天最多進場幾次
STALE_ALARM = 20                          # 有真實部位時，報價超過幾秒沒更新就示警

# 憑證狀態。真實下單一定要憑證，模擬帳戶不用 —— 所以模擬跑再多輪也測不到這一關
# （2026-09-01 第一次按真單才跳 CA not activated）。面板登入時填這個。
CA_OK = {"ok": False, "msg": "尚未檢查憑證"}

_lock = threading.Lock()
_state = {
    "api": None,
    "contract": None,
    "account": None,
    "position": None,     # {'dir','entry','qty','entry_time','target_trade'}
    "last_error": None,
}


# ---------------------------------------------------------------- 基本狀態

def is_live():
    """現在是不是真的會送單。"""
    return REAL_FLAG.exists()


def configure(api, contract):
    """面板連上永豐之後叫一次。"""
    with _lock:
        _state["api"] = api
        _state["contract"] = contract
        acc = None
        try:
            import shioaji as sj
            want = getattr(sj.AccountType, "Future", None)
            for a in api.list_accounts():
                t = getattr(a, "account_type", None)
                # 先比 enum 本身，比不出來才退回比字串。
                # ⚠️ 不可以用「字串裡有沒有 F」這種寫法 —— Stock / Intl 剛好都沒有 F
                #    只是運氣好，哪天多一個帳號型別就會抓錯帳號下單。
                if (want is not None and t == want) or str(t).endswith("Future"):
                    acc = a
                    break
            # ⚠️ 不看 signed 旗標：實測簽署完成之後它仍然是 False（2026-08 踩過），
            #    拿它當條件會變成明明能下單卻一直說沒簽。真的沒簽會在送單時報錯。
        except Exception as e:
            _state["last_error"] = f"讀不到帳號清單：{str(e)[:120]}"
        _state["account"] = acc
    return acc


def _log(kind, payload):
    ORDER_DIR.mkdir(exist_ok=True)
    rec = {"ts": datetime.now().isoformat(timespec="seconds"),
           "kind": kind, "live": is_live(), **payload}
    with (ORDER_DIR / f"{date.today()}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def entries_today():
    """今天已經真的送出幾次進場單。從檔案數，重啟不會歸零。"""
    f = ORDER_DIR / f"{date.today()}.jsonl"
    if not f.exists():
        return 0
    n = 0
    try:
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("kind") == "entry" and r.get("live") and r.get("ok"):
                n += 1
    except Exception:
        pass
    return n


# ---------------------------------------------------------------- 對帳

def broker_position():
    """
    跟券商問「現在到底有沒有部位」。**記憶體不可信** —— 看門狗會重啟面板，
    重啟後 _state 是空的，但券商那邊的部位還在。

    回傳 {'dir','qty','entry'} 或 None；問不到回 'unknown'（不可以當成「沒有部位」）。
    """
    api, acc = _state["api"], _state["account"]
    if api is None:
        return "unknown"
    try:
        rows = api.list_positions(acc) if acc else api.list_positions()
    except Exception as e:
        _state["last_error"] = f"對帳失敗：{str(e)[:120]}"
        return "unknown"
    code = getattr(_state["contract"], "code", None)
    for p in rows or []:
        if code and getattr(p, "code", None) != code:
            continue
        qty = int(getattr(p, "quantity", 0) or 0)
        if qty == 0:
            continue
        d = str(getattr(p, "direction", "")).lower()
        return {"dir": "long" if ("buy" in d or qty > 0) else "short",
                "qty": abs(qty), "entry": float(getattr(p, "price", 0) or 0)}
    return None


def reconcile():
    """啟動時／每次要動作之前叫。把券商的實況寫回 _state。"""
    pos = broker_position()
    if pos == "unknown":
        return "unknown"
    with _lock:
        if pos is None:
            # 【演練時不可以清掉本機部位】券商那邊當然沒有部位，拿它去清的話，
            # 長按送出的演練部位下一秒就被抹掉 —— 持倉那一整塊畫面根本演練不到
            # （2026-09-01 實測）。
            # ⚠️ 但**只跳過「清掉」這一件事**。第一版整個 return 掉，
            #    連「對帳失敗就不送單」「券商已有部位就不送單」兩道也一起失效 ——
            #    等於演練模式跟正式模式行為不一樣，那樣的演練就白練了。
            if is_live():
                _state["position"] = None
        elif _state["position"] is None:
            # 重啟後撿回部位：只知道方向與均價，停利單的下落要另外查
            _state["position"] = {"dir": pos["dir"], "entry": pos["entry"],
                                  "qty": pos["qty"], "entry_time": None,
                                  "target_trade": None, "recovered": True}
    return _state["position"]


# ---------------------------------------------------------------- 下單

def _order(action, price, price_type, order_type, octype):
    import shioaji as sj
    kw = {"action": action, "price": price, "quantity": QTY,
          "price_type": price_type, "order_type": order_type, "octype": octype}
    if _state["account"] is not None:
        kw["account"] = _state["account"]
    return sj.FuturesOrder(**kw)


def _send(order, why):
    """真正送出去的唯一出口。dry run 時只記錄不送。"""
    api = _state["api"]
    body = {"action": str(order.action), "price": order.price, "qty": order.quantity,
            "price_type": str(order.price_type), "order_type": str(order.order_type),
            "octype": str(order.octype), "why": why}
    if not is_live():
        return True, None, _log(why, {**body, "ok": True, "dry_run": True})
    if api is None:
        return False, "尚未連線", _log(why, {**body, "ok": False, "err": "no api"})
    try:
        trade = api.place_order(_state["contract"], order)
        _log(why, {**body, "ok": True,
                   "ordno": getattr(getattr(trade, "order", None), "id", None),
                   "status": str(getattr(getattr(trade, "status", None), "status", ""))})
        return True, None, trade
    except Exception as e:
        _log(why, {**body, "ok": False, "err": str(e)[:200]})
        return False, str(e)[:200], None


def can_enter(price, quote_live):
    """
    可不可以現在真實進場。回傳 (可以嗎, 不行的原因)。
    **每一條擋下來的理由都要講得出來** —— 按鈕變灰卻不說為什麼最讓人火大。
    """
    if price is None or not quote_live:
        return False, "現在沒有即時報價，不能用舊價下單"
    if _state["api"] is None:
        return False, "還沒連上永豐"
    if _state["account"] is None:
        return False, "找不到期貨帳號（可能是 API 還沒簽署完成）"
    # 憑證沒啟用就先擋在這裡，不要讓他長按完才吃到永豐的錯誤訊息
    if is_live() and not CA_OK["ok"]:
        return False, CA_OK["msg"] or "憑證沒啟用，不能真實下單"
    n = entries_today()
    if n >= MAX_ENTRIES:
        return False, f"今天真實進場已經 {n} 次，達到上限 {MAX_ENTRIES} 次"
    pos = reconcile()
    if pos == "unknown":
        return False, "跟券商對帳失敗，這種時候不送單"
    if pos:
        return False, "已經有部位了，先平倉才能再進場"
    return True, None


FILL_WAIT = 5.0        # 送出後最多等幾秒確認成交


def _wait_fill(direction):
    """
    等券商回報「真的成交了」，回傳實際成交均價；沒成交回 None。

    【為什麼一定要等】兩個理由，模擬帳戶實測（2026-09-01）都看得到：
    1. **停利／停損要用實際成交價算**。送單當下的參考價是 46833，實際成交 46835 ——
       用參考價算的話停利掛在 46933，比正確的 46935 少 2 點。市場快的時候差更多。
    2. **IOC 可能整張沒成交**。沒成交卻照樣掛停利平倉單的話，那張 Cover 單留在場上，
       之後成交就變成一個**反向的新部位** —— 明明沒進場卻突然有一個空單。
    """
    if not is_live():
        return None                       # 演練沒有真的部位可以等
    deadline = time.time() + FILL_WAIT
    while time.time() < deadline:
        pos = broker_position()
        if isinstance(pos, dict) and pos.get("dir") == direction and pos.get("entry"):
            return float(pos["entry"])
        time.sleep(0.4)
    return None


def enter(direction, price, tp_points):
    """
    進場。用範圍市價（MKP）＋ IOC：要就立刻成交，不要掛在那裡等。
    **確認成交之後**才把停利限價單掛到券商那邊 —— 那一張電腦關機也有效。
    """
    import shioaji as sj
    act = sj.Action.Buy if direction == "long" else sj.Action.Sell
    ok, err, res = _send(
        _order(act, 0, sj.FuturesPriceType.MKP, sj.OrderType.IOC, sj.FuturesOCType.New),
        "entry")
    if not ok:
        return False, err, None

    fill = _wait_fill(direction)
    if is_live() and fill is None:
        # IOC 沒成交。**絕對不可以掛停利** —— 那張 Cover 單留在場上，
        # 成交之後就變成一個反向的新部位。
        _log("entry_nofill", {"ok": False, "why": "IOC 沒有成交，不掛停利"})
        with _lock:
            _state["position"] = None
        return False, "沒有成交（範圍市價 IOC 當下沒撮到），沒有掛停利，也沒有部位", None

    entry = fill if fill is not None else float(price)
    with _lock:
        _state["position"] = {"dir": direction, "entry": entry, "qty": QTY,
                              "entry_time": datetime.now().strftime("%H:%M:%S"),
                              "target_trade": None, "recovered": False,
                              "ref_price": float(price)}
    # 停利一律用**實際成交價**算，不是送單當下的參考價
    tp = entry + (tp_points if direction == "long" else -tp_points)
    place_target(tp)
    return True, None, _state["position"]


def place_target(tp_price):
    """把停利掛成券商端的限價單。做多→賣出，做空→買回。"""
    import shioaji as sj
    pos = _state["position"]
    if pos is None:
        return False, "沒有部位"
    act = sj.Action.Sell if pos["dir"] == "long" else sj.Action.Buy
    ok, err, res = _send(
        _order(act, round(float(tp_price)), sj.FuturesPriceType.LMT,
               sj.OrderType.ROD, sj.FuturesOCType.Cover),
        "target")
    if ok:
        with _lock:
            _state["position"]["target_trade"] = res
    return ok, err


def close(reason):
    """
    立刻平倉（停損、手動、收盤）。範圍市價＋IOC。
    先把還掛著的停利單取消，免得平完倉那張變成反向新倉。
    """
    import shioaji as sj
    pos = _state["position"]
    if pos is None:
        return False, "沒有部位"
    t = pos.get("target_trade")
    if t is not None and is_live() and _state["api"] is not None:
        try:
            _state["api"].cancel_order(t)
            _log("cancel_target", {"ok": True, "why": "平倉前先撤掉還掛著的停利單"})
        except Exception as e:
            _log("cancel_target", {"ok": False, "err": str(e)[:200]})
    act = sj.Action.Sell if pos["dir"] == "long" else sj.Action.Buy
    ok, err, res = _send(
        _order(act, 0, sj.FuturesPriceType.MKP, sj.OrderType.IOC, sj.FuturesOCType.Cover),
        "close_" + reason)
    if ok:
        with _lock:
            _state["position"] = None
    return ok, err


def snapshot():
    """給面板顯示用。"""
    return {"live": is_live(), "position": _state["position"],
            "ca_ok": CA_OK["ok"], "ca_msg": CA_OK["msg"],
            "entries_today": entries_today(), "max_entries": MAX_ENTRIES,
            "account": str(getattr(_state["account"], "account_id", "")) or None,
            "last_error": _state["last_error"]}
