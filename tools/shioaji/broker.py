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
# 平倉的互斥鎖。停損（主迴圈執行緒）與他手按平倉（HTTP 執行緒）會同時發生，
# 沒有鎖的話兩邊各送一輪、每輪最多 3 張 —— 最壞情況反向開出好幾口
# （lab-qa 退件第 5 條）。拿不到鎖就直接回報「正在平倉中」，不排隊。
_close_lock = threading.Lock()
_close_fail = {"at": 0.0}
CLOSE_COOLDOWN = 15.0     # 秒。平倉失敗後隔多久才准再試（不然主迴圈每 0.25 秒就重送一輪）
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
        # 【2026-09-01 出過大事】舊寫法是
        #     "long" if ("buy" in d or qty > 0) else "short"
        # qty > 0 對任何部位都成立 ⇒ **永遠回報做多**。他做空的那一筆因此：
        #   ・_wait_fill 等不到「方向相符」的部位 → 誤判成沒成交 → 沒掛停利
        #   ・畫面把空單顯示成多單
        #   ・停損算在錯的一邊
        #   ・按平倉時送出的是 Sell（該送 Buy）⇒ 又加了一口空單
        # 現在：先認 direction 這個欄位，認不出來才看數量正負；
        # **兩者都判斷不出來就回 unknown，絕對不猜**。
        d = str(getattr(p, "direction", "")).lower()
        if "sell" in d or "short" in d:
            side = "short"
        elif "buy" in d or "long" in d:
            side = "long"
        elif qty < 0:
            side = "short"
        elif qty > 0:
            side = "long"
        else:
            _state["last_error"] = "券商回報的部位看不出方向，不敢動它"
            return "unknown"
        return {"dir": side, "qty": abs(qty), "entry": float(getattr(p, "price", 0) or 0)}
    return None


_LAST_RECONCILE = {"at": 0.0}
RECONCILE_EVERY = 3.0     # 秒。面板每 0.25 秒跑一圈，不節流會把券商 API 打爆


def reconcile_tick():
    """
    面板主迴圈每一圈都叫這個（自己節流）。

    【為什麼要獨立出來】舊版對帳只寫在 `can_enter()` 的最後一步，
    前面任何一關先擋下來就不會執行 —— 而「當天已達 3 次上限」與「憑證沒啟用」
    都會先擋。於是：下完第 3 單、部位還開著 → 看門狗重啟面板（永豐 SDK 斷線會把
    行程帶掉，是常態）→ 因為已達上限**永遠不再對帳** → 部位撿不回來 →
    **停損完全不監控、畫面顯示空手、連斷線警報都不會響**（警報也要有部位才亮）。
    lab-qa 2026-09-01 實測：達上限與憑證未啟用時，can_enter 呼叫券商查詢 0 次。
    """
    # 演練模式本來不必一直去問券商（那邊沒有我們的單），**但撿回來的真部位例外** ——
    # 那是券商真的有的東西，券商說平掉了就要跟著清，不然畫面會卡在一個鬼部位上。
    if not is_live() and not (_state["position"] or {}).get("recovered"):
        return _state["position"]
    now = time.time()
    if now - _LAST_RECONCILE["at"] < RECONCILE_EVERY:
        return _state["position"]
    _LAST_RECONCILE["at"] = now
    return reconcile()


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
            #
            # ⛔ 【recovered 的部位不算演練部位】2026-09-01 12:43 在他機器上實測到：
            #    面板在真實模式時從券商撿回一口多單（recovered=True），之後他把
            #    `REAL_ORDERS_ON` 刪掉改回演練、並自己在別的工具平掉了那口單 ——
            #    券商已經空手，面板卻因為「演練不准清」永遠掛著那口鬼部位：
            #    `can_enter` 一直回「已經有部位了，先平倉才能再進場」⇒ **從此不能再進場**，
            #    畫面上的浮動損益也是拿現價去對一個不存在的部位算的。
            #    要保護的只有「演練自己造出來的部位」（entry() 寫 recovered=False），
            #    從券商撿回來的那種本來就是真的，券商說沒了就要清掉。
            if is_live() or (_state["position"] or {}).get("recovered"):
                _state["position"] = None
        elif _state["position"] is None:
            # 重啟後撿回部位：只知道方向與均價，停利單的下落要另外查
            _state["position"] = {"dir": pos["dir"], "entry": pos["entry"],
                                  "qty": pos["qty"], "entry_time": None,
                                  "target_trade": None, "recovered": True}
        else:
            # 【券商永遠是真相】本機那份跟券商不一樣時要以券商為準。
            # 舊版只在本機是空的時候才採用券商的資料，本機一旦記錯方向就**永遠改不回來** ——
            # 2026-09-01 就是這樣：本機記成做多、券商是空單，reconcile() 照樣回傳做多，
            # 平倉因此送出同方向的單。這一段就是在補那個洞（測試 FAIL 抓出來的）。
            cur = _state["position"]
            if cur.get("dir") != pos["dir"]:
                _log("position_fixed", {"ok": True,
                                        "was": cur.get("dir"), "now": pos["dir"],
                                        "why": "本機方向跟券商不符，以券商為準"})
            cur["dir"] = pos["dir"]
            cur["entry"] = pos["entry"]
            cur["qty"] = pos["qty"]
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
CLOSE_TRIES = 3        # 平倉沒撮到就再送，最多幾次（停損要的是「一定要出去」）


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
    other = None
    asked = failed = 0
    while time.time() < deadline:
        pos = broker_position()
        asked += 1
        if pos == "unknown":
            # 【查不到 ≠ 沒成交】網路抖一下就把它當成沒成交的話，會清掉本機部位、
            # 跟他說「沒有成交」—— 但券商那邊其實已經有一口，沒停利也沒停損
            # （lab-qa 2026-09-01 用探針重現）。查詢失敗要單獨算，最後另外處理。
            failed += 1
        elif isinstance(pos, dict) and pos.get("entry") is not None:
            # ⚠️ 這裡以前寫 `pos.get("entry")`，成交價回報 0 就整條跳過 ——
            #    要判斷的是「有沒有這個欄位」不是「值真不真」。
            if pos.get("dir") == direction:
                return float(pos["entry"])
            other = pos          # 有部位但方向不是我們送的 —— 這很不對勁
        time.sleep(0.4)
    if failed and failed >= asked / 2:
        # 大半的查詢都失敗 ⇒ 我們根本不知道成交了沒。這種時候**絕不可以**說沒成交，
        # 也不可以掛停利（方向與價格都還不確定）。停手、講清楚、讓他自己去看。
        raise RuntimeError(
            "送出去了，但跟券商對帳一直失敗，**不知道有沒有成交** —— "
            "沒有掛停利，請立刻自己到大戶投確認部位")
    if other is not None:
        # 【不可以當成「沒成交」】那樣會清掉本機部位、讓畫面顯示空手，
        # 但券商那邊其實有東西。2026-09-01 就是這樣，還連帶讓後續平倉送錯邊。
        raise RuntimeError(
            f"券商回報的部位方向是 {other['dir']}，跟送出的 {direction} 不符 —— "
            "已經停手，請自己到大戶投確認並處理")
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

    try:
        fill = _wait_fill(direction)
    except RuntimeError as e:
        # 方向不符：券商那邊有東西但不是我們送的方向。停手、不掛停利、
        # **不要清掉部位**（清掉畫面會顯示空手，但帳上其實有東西）。
        _log("entry_mismatch", {"ok": False, "err": str(e)})
        return False, str(e), None
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
    tok, terr = place_target(tp)
    if not tok:
        # 【不可以吞掉】停利掛不上去卻回報一切正常的話，他會以為賺的那邊有保護，
        # 其實只剩下面板的停損 —— 而那個電腦關機就沒了（lab-qa 退件第 3 條）。
        return True, ("已經進場，但**停利單沒掛上去**（%s）—— "
                      "請立刻自己到大戶投補掛，或直接平倉" % (terr or "券商拒絕")), \
               _state["position"]
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

    【方向一定要跟券商重新確認】2026-09-01 出過事：本機記的方向是錯的，
    平倉送出去變成同方向再加一口。平倉是「反向下單」，方向錯 = 部位加倍，
    所以這裡寧可拒絕，也不用記憶體裡那份可能過期的資料。
    """
    import shioaji as sj
    if not _close_lock.acquire(blocking=False):
        return False, "正在平倉中，請稍候"
    try:
        return _close_locked(reason)
    finally:
        _close_lock.release()


def _close_locked(reason):
    import shioaji as sj
    # 失敗之後的冷卻：主迴圈每 0.25 秒就會再叫一次停損檢查，沒有冷卻的話
    # 只要價格還在停損之外就會一直重送（lab-qa 退件第 6 條）。
    if time.time() - _close_fail["at"] < CLOSE_COOLDOWN:
        return False, "剛剛平倉失敗，%.0f 秒後才會再試 —— 等不及請自己到大戶投平倉" % (
            CLOSE_COOLDOWN - (time.time() - _close_fail["at"]))
    pos = _state["position"]
    if is_live():
        fresh = reconcile()
        if fresh == "unknown":
            return False, "跟券商對帳失敗，不確定部位方向 —— 請自己到大戶投平倉"
        pos = fresh
    if pos is None:
        return False, "沒有部位"
    if pos.get("dir") not in ("long", "short"):
        return False, "看不出部位方向，不敢送平倉單 —— 請自己到大戶投平倉"
    t = pos.get("target_trade")
    stray = False
    if t is not None and is_live() and _state["api"] is not None:
        try:
            # 【一定要先 update_status】不先更新，cancel_order 會回
            # "StatusCode: 400, Detail: Please run update_status"（2026-09-01 實測）。
            _state["api"].update_status(_state["account"])
            _state["api"].cancel_order(t)
            _log("cancel_target", {"ok": True, "why": "平倉前先撤掉還掛著的停利單"})
        except Exception as e:
            # 撤不掉不可以默默吞掉：那張平倉限價單留在場上，成交之後就是一個反向新倉。
            # 但「有裸部位」比「有一張殘單」更危險，所以還是要繼續平倉，
            # 只是要把這件事講出來讓他自己去刪。
            stray = True
            _log("cancel_target", {"ok": False, "err": str(e)[:200],
                                   "why": "撤不掉，平倉後請自己到大戶投刪掉那張停利單"})
    act = sj.Action.Sell if pos["dir"] == "long" else sj.Action.Buy
    last_err = None
    for attempt in range(CLOSE_TRIES):
        ok, err, res = _send(
            _order(act, 0, sj.FuturesPriceType.MKP, sj.OrderType.IOC,
                   sj.FuturesOCType.Cover),
            "close_" + reason)
        last_err = err
        if not ok:
            break
        # 【「券商收到」不等於「平掉了」】範圍市價 IOC 當下沒撮到就整張不成交。
        # 舊版把 ok=True 當成平倉完成、直接清掉本機部位 ⇒ 券商還有部位、
        # 面板卻以為空手 ⇒ **停損不再監控，而且再按平倉會說「沒有部位」**
        # （2026-09-01 真的發生，他最後自己用別的工具平掉）。
        # 這跟進場那個 no-fill bug 是同一個形狀，當時只補了進場那邊。
        if not is_live():
            with _lock:
                _state["position"] = None
            return True, None
        for _ in range(int(FILL_WAIT / 0.4)):
            time.sleep(0.4)
            if broker_position() is None:
                with _lock:
                    _state["position"] = None
                _log("close_confirmed", {"ok": True, "tries": attempt + 1,
                                         "stray_target": stray})
                return True, ("平倉成功，但那張停利單沒撤掉，請自己到大戶投刪除"
                              if stray else None)
        _log("close_nofill", {"ok": False, "try": attempt + 1,
                              "why": "送出了但沒撮到，再試一次"})
    # 沒平掉：**絕對不可以清掉本機部位**，不然停損就停了
    _close_fail["at"] = time.time()
    _log("close_failed", {"ok": False, "err": last_err,
                          "why": "沒有平掉，部位還在，本機狀態保留"})
    return False, ("平不掉（範圍市價 IOC 連續 %d 次沒撮到）—— 部位還在，"
                   "請立刻自己到大戶投平倉" % CLOSE_TRIES if last_err is None else last_err)


def snapshot():
    """
    給面板顯示用。

    【一定要濾掉 target_trade】那是永豐的 Trade 物件，**json.dumps 序列化不了** ——
    直接把 _state["position"] 丟出去的話，一旦真的掛上停利單，
    /api/state 整支就會炸掉回空字串 ⇒ **前端拿不到任何狀態、畫面整個凍住**
    （2026-09-01：他做空成功、單也對，但面板不顯示部位，就是這個）。
    演練模式時 target_trade 是個 dict，序列化得了，所以演練永遠測不出來。
    """
    pos = _state["position"]
    if pos is not None:
        pos = {k: v for k, v in pos.items() if k != "target_trade"}
        pos["has_target"] = _state["position"].get("target_trade") is not None
    return {"live": is_live(), "position": pos,
            "ca_ok": CA_OK["ok"], "ca_msg": CA_OK["msg"],
            "entries_today": entries_today(), "max_entries": MAX_ENTRIES,
            "account": str(getattr(_state["account"], "account_id", "")) or None,
            "last_error": _state["last_error"]}
