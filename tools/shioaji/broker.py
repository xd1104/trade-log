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
2. **口數不從介面讀**。沒指定就是 `QTY`（1 口；手動真單永遠走這條）。
   ⭐ 2026-10-14 多口：自動下單的口數由 `lots.py`（唯一正本）決定、送單時傳進 `enter(qty=)`；
   每一張單在 `_order()` 再擋一次 1 ≤ 口數 ≤ `QTY_MAX`，介面或狀態檔被改壞也送不出 11 口。
   送 N 口只成交 k 口 ⇒ **接受 k 口當部位、⛔ 不追單補到 N**；停利、停損、平倉、成績單都照實際口數。
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
# 【跟 real_orders 的差別】real_orders 是「每一張委託單」的原始 log，給程式與除錯看的；
# real_trades 是「一趟來回」的成績單，給人看的：幾點進、幾點出、賺賠幾點。
# ⛔ 兩個都**不上傳**。repo 是公開的，Benson 2026-08 決定真實交易只留在這台電腦
#    （練習單才會同步到 data/practice.json 與手機）。
TRADE_DIR = HERE / "real_trades"
QTY = 1                                   # 沒指定口數時的預設（手動真單永遠是這個）。不從介面讀
QTY_MAX = 10                              # ⭐ 2026-10-14：一張單最多幾口（階梯最高一級聯軍 10 口）；超過一律不送
MAX_ENTRIES = 3                           # 每天最多進場幾次
# ══ 小台（2026-11-06 Benson：權益到 52 萬、階梯 10＋4 到頂 ⇒ 換小台）════════════════════════════════
# ⛔⛔ 這一支裡所有「口數」一律是**微台等值**（1 小台＝5 微台）：部位的 qty、成交明細、對帳、成績單、已實現損益
#    全部用微台等值 ⇒ auto_fire／night_fire／lots／risk_cap／cashflow／畫面**一個字都不用改**、也不會把小台少算 5 倍。
#    只有兩個邊界會換算：送單（_order：微台等值 ÷ 5 ＝ 小台口數）與讀券商（部位／成交明細／已實現：小台口數 × 5）。
# ⛔ 開關 MINI_ON（檔案存在才用小台；**只有 Benson 能開**，跟 REAL_ORDERS_ON 同一套規矩，gitignore）。
#    沒開 ⇒ 跟 11-06 以前一模一樣（只用微台）。
# 用小台的條件：開關開著、小台合約有拿到、這一筆微台等值是 5 的倍數 ⇒ 整筆用小台。
# ⭐ 2026-11-06 晚：混搭（Benson 定口數階梯每級聯軍＋1 口，11＝2 小台＋1 微台）⇒ 不是 5 的倍數、而且 ≥ 5 口
#    ⇒ product "MIX"：小台 q//5 口＋微台 q%5 口，**同一筆部位兩種合約**。部位多一個 "mix"：{"MXF": 等值, "TMF": 等值}
#    （qty＝兩者和、entry＝依等值加權的平均成交價）。進場兩張、停利兩張、平倉每一腿各自平（_enter_mix／_close_mix）。
#    任何一腿歸零 ⇒ 部位降回單一合約（product 改成剩下那種、刪掉 mix；"mix0" 留著記得「這一趟是混搭開的」）。
MINI_FLAG = HERE / "MINI_ON"
RATIO = {"TMF": 1, "MXF": 5}              # 一口＝幾口微台
QTY_MAX_EQ = QTY_MAX * RATIO["MXF"]       # 開了小台之後，一筆最多幾口微台等值（＝ 10 口小台）
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
# ⭐ 2026-10-02 重複記帳：13:43:30 收盤平倉送出後、等成交那幾秒，另一條執行緒的定期對帳（reconcile）
#    剛好問到「券商已經空手」⇒ 記了一筆 closed_elsewhere（出場價空白），平倉迴圈接著又記一筆 eod（+27）
#    ⇒ 同一口兩筆 ⇒ 畫面「對不到」、風控把那一口當成「問不到出場價」不算（虧錢那天就會少算）。
#    ⇒ **自己送出平倉單、還在等成交**的這段期間（_CLOSE_INFLIGHT），對帳**不記帳**，交給平倉迴圈記那一筆。
#    ⛔ 只管「記不記帳」：清本機部位、其他對帳行為一律照舊。
_CLOSE_INFLIGHT = {"on": False}
CLOSE_COOLDOWN = 15.0     # 秒。平倉失敗後隔多久才准再試（不然主迴圈每 0.25 秒就重送一輪）
_state = {
    "api": None,
    "contract": None,
    "contracts": {},      # ⭐ 2026-11-06 {"TMF": 微台合約, "MXF": 小台合約}；"contract" 永遠＝微台（舊路照舊）
    "account": None,
    "position": None,     # {'dir','entry','qty','entry_time','target_trade'}
    "last_error": None,
}


# ---------------------------------------------------------------- 基本狀態

def is_live():
    """現在是不是真的會送單。"""
    return REAL_FLAG.exists()


def mini_on():
    """小台開關開著、而且這次連線有拿到小台合約。"""
    return MINI_FLAG.exists() and _state["contracts"].get("MXF") is not None


def product_of(code):
    """合約代碼 ⇒ "TMF"／"MXF"；認不得 ⇒ None。"""
    c = str(code or "")
    for k in RATIO:
        if c.startswith(k):
            return k
    return None


def product_for(qty):
    """
    這一筆（微台等值）要用哪一種合約 ⇒ "TMF"／"MXF"／"MIX"。
    小台沒開（或沒拿到小台合約）、或不到 5 口 ⇒ 微台（跟 11-06 以前一樣）。
    ⭐ 2026-11-06 混搭：開著、≥ 5 口、不是 5 的倍數 ⇒ "MIX"（小台 q//5 口＋微台 q%5 口）；5 的倍數 ⇒ 整筆小台。
       超過 QTY_MAX_EQ ⇒ 微台（qty_ok 本來就擋掉，這裡只是不給一個看起來能送的答案）。
    """
    if mini_on() and isinstance(qty, int) and not isinstance(qty, bool) and qty >= RATIO["MXF"] \
            and qty <= QTY_MAX_EQ:
        return "MXF" if qty % RATIO["MXF"] == 0 else "MIX"
    return "TMF"


def mix_split(qty):
    """⭐ 2026-11-06 混搭：微台等值 ⇒ {"MXF": 小台那一腿的等值, "TMF": 微台那一腿的等值}（兩腿都 > 0 才是混搭）。"""
    m = RATIO["MXF"]
    return {"MXF": (qty // m) * m, "TMF": qty % m}


def _eq_ok(q):
    """
    ⭐ 2026-11-06：**已經開著的**小台／混搭部位，口數（微台等值）看得懂嗎 —— ⛔ 不看開關。
    為什麼不用 qty_ok：Benson 拿著 15 口等值的混搭部位時把 MINI_ON 刪掉，qty_ok(15) 變 False
    ⇒ _own_qty 掉回 1 口、平倉每張只認 1 口 ⇒ 停損平不乾淨。開關只管「新開的單用什麼」，⛔ 不管「手上的怎麼平」。
    """
    return isinstance(q, int) and not isinstance(q, bool) and 1 <= q <= QTY_MAX_EQ


def _pq_ok(p, q):
    """部位 p 的口數 q 合不合理：微台部位 ＝ qty_ok（⛔ 一個字都沒變）；小台／混搭 ＝ qty_ok 或 _eq_ok。"""
    if qty_ok(q):
        return True
    return isinstance(p, dict) and (p.get("product") in ("MXF", "MIX") or bool(p.get("mix0"))) and _eq_ok(q)


def _ratio_of_trade(trade):
    """一張委託單是哪種合約 ⇒ 一口＝幾口微台（讀不到 ⇒ 1，＝ 11-06 以前的行為）。"""
    try:
        return RATIO.get(product_of(getattr(getattr(trade, "contract", None), "code", None)), 1)
    except Exception:
        return 1


def configure(api, contract, extra=None):
    """面板連上永豐之後叫一次。`extra`＝{"MXF": 小台合約}（⭐ 2026-11-06；沒給 ⇒ 只有微台）。"""
    _loop_reset()                          # ⭐ 第七輪品管 #2：重新連線 ⇒ 不沿用上一次的平倉單起算
    with _lock:
        _state["api"] = api
        _state["contract"] = contract
        _state["contracts"] = {"TMF": contract}
        for k, c in (extra or {}).items():
            if k in RATIO and c is not None:
                _state["contracts"][k] = c
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


def _fill_price(trade):
    """
    跟券商問「這張單實際成交在哪裡」。

    市價單送出時價格欄是 0，所以**成交價只能事後問**。問不到就回 None ——
    ⛔ 絕對不可以拿參考價或現價冒充：那會讓成績單上出現一個看起來精確、
       其實是編的數字，比留白更糟。
    """
    if trade is None or not is_live() or _state["api"] is None:
        return None
    try:
        _state["api"].update_status(_state["account"])
    except Exception:
        pass                                   # 更新失敗就用手上這份，頂多問不到
    try:
        deals = list(getattr(getattr(trade, "status", None), "deals", None) or [])
        qty = sum(float(getattr(d, "quantity", 0) or 0) for d in deals)
        if qty <= 0:
            return None
        # 一口單通常只有一筆成交，但分批撮合時要用加權平均
        return round(sum(float(d.price) * float(d.quantity) for d in deals) / qty, 2)
    except Exception:
        return None


# ⭐ 2026-10-14 多口用的成交明細。⛔ 一口的路（_fill_price）一個字都沒改。
def _deal_sums(trade):
    """trade 的成交明細 ⇒ (Σ價×口, Σ口, 最低價, 最高價)；讀不到 ⇒ None。⛔ 不呼叫 update_status（呼叫端負責）。"""
    if trade is None:
        return None
    try:
        deals = list(getattr(getattr(trade, "status", None), "deals", None) or [])
        pq = q = 0.0
        lo = hi = None
        r = _ratio_of_trade(trade)           # ⭐ 2026-11-06 小台一口＝5 口微台等值
        for d in deals:
            dq, dp = float(getattr(d, "quantity", 0) or 0) * r, float(d.price)
            if dq <= 0:
                continue
            pq += dp * dq
            q += dq
            lo = dp if lo is None else min(lo, dp)
            hi = dp if hi is None else max(hi, dp)
        return pq, q, lo, hi
    except Exception:
        return None


def _update_status():
    try:
        _state["api"].update_status(_state["account"])
    except Exception:
        pass


def _fill_price_many(trades, want=None):
    """
    好幾張平倉單（多口時可能分好幾張才平完）合起來的加權平均成交價。
    ⛔ 任何一張讀不到明細、或合起來的口數不等於 `want` ⇒ None（⛔ 不拿部分成交冒充全部）。
    """
    if not trades or not is_live() or _state["api"] is None:
        return None
    _update_status()
    pq = q = 0.0
    for t in trades:
        s = _deal_sums(t)
        if s is None:
            return None
        pq, q = pq + s[0], q + s[1]
    if q <= 0 or (want is not None and abs(q - float(want)) > 1e-9):
        return None
    return round(pq / q, 2)


def _target_rest_px(pos, tsums=None):
    """
    部位整個不見（停利單把剩下的都吃掉了）那一刻，**剩下那幾口**的停利成交價。
    沒有記過「停利已經先成交幾口」（`tgt_used`）⇒ 跟 10-14 以前一樣直接 `_fill_price`（一口的路一字未改）。
    有 ⇒ 停利單全部明細 − 之前已經算進 legs 的那一段；口數對不上 ⇒ None（⛔ 不猜）。
    ⭐ 多口那條路的明細 `tsums` 由 reconcile 在拿鎖之前問好（⛔ 鎖裡不問券商）。
    """
    if pos.get("target_trades") is not None:
        # ⭐ 2026-11-06 混搭：停利是兩張（每一腿一張），明細由 reconcile 先合起來問好（tsums，微台等值）。
        #    ⛔ 沒有「一口的路」可退：一律用 tsums 扣掉之前算過的那段，口數對不上 ⇒ None（⛔ 不猜）。
        if not is_live() or _state["api"] is None or tsums is None:
            return None
        used = pos.get("tgt_used") or [0.0, 0.0]
        rq = tsums[1] - float(used[1])
        if rq <= 0 or abs(rq - float(pos.get("qty") or 0)) > 1e-9:
            return None
        return round((tsums[0] - float(used[0])) / rq, 2)
    t = pos.get("target_trade")
    used = pos.get("tgt_used")
    if not used:
        return _fill_price(t)
    if t is None or not is_live() or _state["api"] is None:
        return None
    s = tsums
    if s is None:
        return None
    rq = s[1] - float(used[1])
    if rq <= 0 or abs(rq - float(pos.get("qty") or 0)) > 1e-9:
        return None
    return round((s[0] - float(used[0])) / rq, 2)


def _partial_leg(cur, n, tsums=None):
    """
    ⭐ 多口：部位**少了 n 口、但不是我們在平倉**（停利單部分成交，或他自己在別處平掉幾口）。
    記一段 leg（幾口、什麼價、怎麼出去的）掛在部位上，最後整趟結束時 record_trade 一起算。
    價錢：停利單的明細扣掉之前算過的那段，剛好是 n 口 ⇒ 那就是停利成交價（src="tp"）；
    對不上 ⇒ 留白（src="elsewhere"，⛔ 不猜）。
    ⭐ 2026-10-14 QA 必修 7：拿著 _lock 被叫（reconcile 裡）⇒ ⛔ 這裡**不問券商**；
       停利單的成交明細 `tsums` 由 reconcile 在拿鎖**之前**先問好傳進來（_reconcile_prefetch）。
    """
    px, src = None, "elsewhere"
    s = tsums
    if s is not None:
        used = cur.get("tgt_used") or [0.0, 0.0]
        nq = s[1] - float(used[1])
        if nq > 0 and nq - n > -1e-9:
            # ⭐ 2026-11-02：明細比部位晚問，停利單可能已經又多成交幾口（實測部位少 6 口、明細已經 7 口）
            #    ⇒ 明細的口數比 n 多也是停利單；只記這 n 口，多出來的留給下一段算
            avg = (s[0] - float(used[0])) / nq
            px, src = round(avg, 2), "tp"
            cur["tgt_used"] = [float(used[0]) + avg * n, float(used[1]) + n]
    cur.setdefault("legs", []).append({"qty": int(n), "px": px, "src": src,
                                       "at": datetime.now().strftime("%H:%M:%S")})
    _log("position_partial", {"ok": True, "gone": int(n), "px": px, "src": src,
                              "why": "部位少了 %d 口（%s）—— 剩下的照樣看停損、收盤平剩下的" % (
                                  n, "停利單部分成交" if src == "tp" else "不是我們平的")})


def record_trade(pos, exit_price, reason, qty=None):
    """
    把一趟來回寫進成績單。**只在部位真的結束時叫。**

    exit_price 可以是 None（問不到成交價）—— 那就照實留白，points 也留白。

    ⭐ 2026-10-14 多口：`qty` ＝ 這一次出場的口數（沒給 ⇒ 部位的 qty，跟以前一樣）。
       部位上掛著 `legs`（停利單先吃掉幾口、他在別處平掉幾口）⇒ 這一列記**整趟**：
       qty ＝ 全部口數、exit ＝ 各段加權平均、points ＝ **每口**點數（risk_cap 乘 qty 就是總點數）；
       任何一段沒有成交價 ⇒ exit／points 留白（⛔ 不拿一部分冒充全部）。另附 `legs` 給人對帳。
       進場價用 `entry_fill`（成交那一刻的均價）——部分出場之後券商的均價會變。
       沒有 legs ⇒ 這一列跟 10-14 以前一個欄位都不差。
    """
    if not pos:
        return None
    entry = pos.get("entry")
    q = pos.get("qty", QTY) if qty is None else qty
    legs = list(pos.get("legs") or [])
    if legs:
        entry = pos.get("entry_fill", entry)
        tot = int(q) + sum(int(l.get("qty") or 0) for l in legs)
        # qty＝0 ⇒ 最後這一段沒有口數（我們的口數都已經在 legs 裡出去了；他加碼的那幾口交給手動部位）
        parts = ([(int(q), exit_price)] if int(q) > 0 else []) + \
            [(int(l.get("qty") or 0), l.get("px")) for l in legs]
        if parts and all(p is not None for _n, p in parts) and tot > 0:
            exit_price = round(sum(n * float(p) for n, p in parts) / tot, 2)
        else:
            exit_price = None
        if int(q) > 0:
            legs.append({"qty": int(q), "px": parts[0][1], "src": reason,
                         "at": datetime.now().strftime("%H:%M:%S")})
        q = tot
    pts = None
    if entry is not None and exit_price is not None:
        d = 1 if pos.get("dir") == "long" else -1
        pts = round(d * (float(exit_price) - float(entry)), 1)
    rec = {"date": str(date.today()),
           "dir": pos.get("dir"), "qty": q,
           "entry_time": pos.get("entry_time"), "entry": entry,
           "exit_time": datetime.now().strftime("%H:%M:%S"), "exit": exit_price,
           "reason": reason, "points": pts,
           "recovered": bool(pos.get("recovered"))}
    # ⭐ 2026-11-06 合約別（cashflow 估手續費要分小台／微台）：這一趟是混搭開的（mix0；中途剩一腿也一樣）⇒ "MIX"
    #    ＋ "mix"＝進場時兩腿各幾口等值（手續費照每一口合約算，進出各一次）；整筆小台 ⇒ "MXF"。
    #    ⛔ 微台**不寫** product（沒有＝微台，cashflow 也這樣讀）：微台那一列要跟 11-06 以前逐欄位相同
    #       （test_lots ② 拿改版前的 broker 跑同一套情境、比成績單每一個欄位）。
    if isinstance(pos.get("mix0"), dict) or pos.get("product") == "MIX":
        rec["product"] = "MIX"
        rec["mix"] = {p: int(v) for p, v in (pos.get("mix0") or pos.get("mix") or {}).items()}
    elif pos.get("product") == "MXF":
        rec["product"] = "MXF"
    if pos.get("late_leg") and isinstance(pos.get("entry0"), (int, float)) and entry is not None:
        # ⭐ 2026-11-06 進場一腿晚回報、併進來之後進場價重新加權過：
        #    entry 寫**帳本當初記的那個價**（entry0）——auto_fire／night_fire／risk_cap 都拿「進場價 ±1 點」認這一趟，
        #    寫加權後的價會對不到（漏進風控、收盤誤判）；加權後的真正均價放 entry_w，points 用它算（錢才對得起來）。
        rec["entry_w"] = float(entry)
        rec["entry"] = float(pos["entry0"])
    if legs:
        rec["legs"] = legs
    try:
        TRADE_DIR.mkdir(exist_ok=True)
        with (TRADE_DIR / f"{date.today()}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        _log("trade_record_failed", {"ok": False, "err": str(e)[:200]})
    return rec


_REALIZED = {"at": 0.0, "rows": []}
REALIZED_TTL = 60.0        # 秒。這是要跟券商拿的，不能每 0.25 秒問一次


def realized_today(fresh=False):
    """
    跟券商要今天的**已實現損益**。⚠️ 唯讀，不送任何單。

    這是損益的真相 —— 券商自己算的，含實際成交價。我們自己記的 deal_price 只是快路徑，
    對不上時以這裡為準。

    ⛔⛔ **「問不到」回 `None`，⛔ 不是空陣列**（2026-09-09 lab-qa 退件 M1）。
       舊版查詢失敗時靜靜地回上一次的快取（開機第一次就是 `[]`）⇒ 呼叫端**結構上**
       分不出「券商說今天沒有已實現損益」與「我們根本沒問到」。
       `auto_fire._already_closed()` 正是靠這一份在判「他是不是中途自己平掉、
       又自己開了一口新的」—— 它把「問不到」讀成「沒有平過」，於是**送出一張
       平掉他部位的單**（lab-qa 實測 X4／X5 各一張）。
       這就是 `broker_position()` 回 `"unknown"` 的同一條鐵律（**查不到 ≠ 沒有**），
       已實現損益這一條當初漏了 unknown 態。
         - 回 `list`（可能是空的）＝ **真的問到了**，那就是券商的答案
         - 回 `None` ＝ **問不到**，⛔ 呼叫端不准把它當成「沒有」
    ⛔ 失敗時**不准拿舊快取充數**：舊快取只證明「一分鐘前是這樣」，而 13:43:30
       那一刻要判的正是「這一分鐘之內他有沒有自己平掉」。

    `fresh=True` ⇒ 略過 TTL 快取、一定重問一次。**收盤自動平倉那一道守衛要用這個**：
    59 秒前的快照對「他剛剛自己平掉了沒」是無效證據（lab-qa 實測 X5 就是這個形狀 ——
    快取沒過期而且是空的 ⇒ 照樣送出一張平倉單）。
    ⚠️ 一般顯示用途（`trades_today()` 補出場價）維持吃快取 ——
       ⛔ 那條路每 0.25 秒會被畫面叫到，不能每次都去問券商。
    """
    if not is_live():
        # 演練模式：整個 broker 都是 dry run，券商那邊本來就不會有我們的成交
        # ⇒ 這是一個**確定的答案**（空的），不是「問不到」。
        return []
    if _state["api"] is None:
        return None                       # 還沒連上永豐 ⇒ ⛔ 問不到，不是「沒有」
    now = time.time()
    if not fresh and now - _REALIZED["at"] < REALIZED_TTL:
        return _REALIZED["rows"]
    d = str(date.today())
    out = _realized_day(d)
    if out is None:
        return None                       # ⛔ 不回舊快取、也不回 []
    _REALIZED["at"], _REALIZED["rows"] = now, out
    return out


def _realized_day(d):
    """
    某一天的已實現損益（⛔ 不用快取；問不到 ⇒ None）。realized_today 與「夜盤跨午夜對帳要看前一天」共用。
    ⭐ 2026-10-15 多帶 `id`（券商那一列的編號）：多口對帳要排除「進場那一刻就已經在的列」（見 _real_snapshot）。
    """
    try:
        rows = _state["api"].list_profit_loss(_state["account"], str(d), str(d)) or []
    except Exception as e:
        _state["last_error"] = "問不到已實現損益：" + str(e)[:120]
        return None
    out = []
    for r in rows:
        try:
            act = str(getattr(r, "direction", "")).lower()
            prod = product_of(getattr(r, "code", None))
            if prod is None and getattr(r, "code", None):
                continue                  # ⭐ 2026-11-06 不是微台也不是小台（別的商品）⇒ 不是我們的
            out.append({
                "entry": float(getattr(r, "entry_price", 0) or 0),
                "exit": float(getattr(r, "cover_price", 0) or 0),
                "pnl": float(getattr(r, "pnl", 0) or 0),
                "qty": int(getattr(r, "quantity", 0) or 0) * RATIO.get(prod, 1),   # 微台等值
                "product": prod or "TMF",
                # 期貨的 direction 是**進場**那一邊：Buy ＝ 做多
                "dir": "long" if "buy" in act else ("short" if "sell" in act else None),
                "id": getattr(r, "id", None)})
        except Exception:
            pass
    return out


def trades_today():
    """
    今天的成績單，舊到新。

    自己記的那份如果缺出場價（市價單送出時價格欄是 0，事後又問不到成交明細），
    就拿券商的已實現損益補 —— **補得起來才顯示數字，補不起來就留白**。

    ⚠️ `realized_today()` 現在會回 `None`（＝問不到）。這裡的行為**一個字都沒變**：
       問不到就補不起來 ⇒ 留白。⛔ 但不可以寫成 `realized_today() or []` ——
       那正是把「問不到」跟「沒有」壓成同一件事的寫法（M1 就是這樣長出來的）。
    """
    f = TRADE_DIR / f"{date.today()}.jsonl"
    rows = []
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    # ⚠️ 2026-10-14 多口：券商的已實現損益是**一口一列**（各口進場價不一定一樣），拿一列去補多口那一趟
    #    會補成「其中一口」的數字 ⇒ 多口的洞⛔ 不補、照實留白。一口的照舊。
    holes = [t for t in rows if t.get("points") is None
             and not (isinstance(t.get("qty"), (int, float)) and t["qty"] > 1)]
    if not holes:
        return rows
    real = realized_today()
    if real is None:
        return rows          # 問不到 ⇒ 補不起來 ⇒ 留白（跟舊版行為一模一樣）
    pool = list(real)
    for t in holes:
        hit = None
        for r in pool:
            if r["dir"] and t.get("dir") and r["dir"] != t["dir"]:
                continue
            if t.get("entry") is not None and abs(r["entry"] - float(t["entry"])) > 1.0:
                continue
            hit = r
            break
        if hit is None:
            continue
        pool.remove(hit)
        d = 1 if hit["dir"] == "long" else -1
        t["entry"] = t.get("entry") if t.get("entry") is not None else hit["entry"]
        t["exit"] = hit["exit"]
        t["points"] = round(d * (hit["exit"] - hit["entry"]), 1)
        t["pnl"] = hit["pnl"]
        t["src"] = "broker"          # 這一筆的數字是跟券商對來的，不是面板自己記的
    return rows


def set_trade_note(day, entry_time, entry, text):
    """
    給某一筆真實交易補心得。

    【認人方式跟練習那邊同一套】(日期 ＋ 進場時間 ＋ 進場價)，**不可以用陣列位序** ——
    位序會被「撤銷最後一筆」之類的動作弄歪（CLAUDE.md 記過）。
    進場時間只比到分（HH:MM）：畫面上顯示的就是分，秒是我們自己記的。

    ⛔ 真實交易的心得**只留在這台電腦**，跟成績單一樣不上傳
       （練習的才會同步到 data/practice.json 與手機）。
    """
    f = TRADE_DIR / f"{day}.jsonl"
    if not f.exists():
        return False, "找不到那一天的真實交易紀錄"
    rows = []
    for line in f.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    want_t = str(entry_time or "")[:5]
    hit = None
    for r in rows:
        if str(r.get("entry_time") or "")[:5] != want_t:
            continue
        if entry is not None and r.get("entry") is not None:
            if round(float(r["entry"])) != round(float(entry)):
                continue
        hit = r
        break
    if hit is None:
        return False, "對不到那一筆交易（時間或進場價不符）"
    hit["note"] = (text or "").strip() or None
    hit["note_at"] = datetime.now().isoformat(timespec="seconds")
    tmp = f.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                   encoding="utf-8")
    tmp.replace(f)                       # 換檔是原子的，寫到一半斷電不會留半個檔
    _HIST["at"] = 0.0                    # 快取作廢，畫面才會立刻看到新的字
    return True, None


_HIST = {"at": 0.0, "rows": []}
HIST_TTL = 5.0          # 秒。面板每 0.5 秒問一次，不快取就是每秒讀兩次磁碟


def trades_history(limit=120):
    """
    留下來的每一筆真實交易，**新的在前**（今天的在最上面）。給勝率統計用。

    今天那一份走 trades_today()，因為它會跟券商的已實現損益補出場價；
    過去的日子在平倉當下就記完了，直接讀檔。
    """
    now = time.time()
    if now - _HIST["at"] < HIST_TTL:
        return _HIST["rows"]
    out = list(reversed(trades_today()))
    today_file = f"{date.today()}.jsonl"
    if TRADE_DIR.exists():
        for f in sorted(TRADE_DIR.glob("*.jsonl"), reverse=True):
            if f.name == today_file:
                continue
            day = []
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        day.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
            out.extend(reversed(day))
            if len(out) >= limit:
                break
    out = out[:limit]
    _HIST.update({"at": now, "rows": out})
    return out


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


# ---------------------------------------------------------------- 帳戶還有多少錢

# ⭐⭐ 2026-09-21 Benson 要的【帳戶總覽】。**這一支只問、不動任何東西。**
#    ⛔ 放在這個檔是因為帳號（`_state["account"]`）在這裡 —— ⛔ 但它跟送單那條路
#       一點關係都沒有：不拿 `_lock`、不碰 `_state["position"]`、不送任何委託。
#    ⚠️ 永豐的欄位名對照（`shioaji._core.Margin`，⛔ 不要自己改名字）：
#       equity_amount 權益總值／equity 權益數／available_margin 可動用（可出金）
#       initial_margin 原始保證金／maintenance_margin 維持保證金／margin_call 追繳
#       risk_indicator 風險指標／future_open_position 未沖銷期貨浮動損益
#       future_settle_profitloss 期貨平倉損益／deposit_withdrawal 存提
#       fee 手續費／tax 期交稅／today_balance 今日餘額／yesterday_balance 前日餘額
MARGIN_FIELDS = ("equity_amount", "equity", "available_margin", "initial_margin",
                 "maintenance_margin", "margin_call", "risk_indicator",
                 "future_open_position", "today_future_open_position",
                 "future_settle_profitloss", "deposit_withdrawal", "fee", "tax",
                 "today_balance", "yesterday_balance", "order_margin_premium")


def _fetch_word(st):
    """
    永豐那個 `status` 到底是「拿到了」還是「還在拿」⇒ 回小寫字串（沒有 ⇒ `"none"`）。

    ⛔⛔ 2026-09-21 他第一天打開【帳戶總覽】就中：`FetchStatus` **不是 Python 的 Enum**，
       **沒有 `.name`** ⇒ 舊寫法 `getattr(st, "name", st)` 退回物件本身、
       `str()` 出來是 `"FetchStatus.Fetched"` ⇒ 比不到 `"fetched"` ⇒
       **明明拿到了卻一直說「還沒到齊（FetchStatus.Fetched）」**。
    ⇒ 三種來源都認：`.value`（'Fetched'）、`str()` 的最後一節、字串本身。
    ⚠️ 只有 Fetched / Fetching / Unfetch 三種（`shioaji._core.FetchStatus`）。
    """
    if st is None:
        return "none"
    v = getattr(st, "value", None)
    if not isinstance(v, str) or not v:
        v = str(st).rsplit(".", 1)[-1]
    return v.strip().lower()


def account_margin():
    """
    跟券商問「帳戶現在有多少錢」。⚠️ **唯讀**，⛔ 一張單都不會送。
    ⇒ `(數字 dict, None)`；問不到 ⇒ `(None, 一句話)`。
    ⛔ 問不到就是問不到：**不猜、不拿上一次的數字頂**（呼叫端負責留白）——
       錢的數字編一個出來比留白危險得多。
    ⚠️ 永豐回來的物件有 `status`（FetchStatus）：不是 Fetched 就當成沒問到。
    """
    api, acc = _state["api"], _state["account"]
    if api is None:
        return None, "面板還沒連上永豐"
    if acc is None:
        return None, "找不到期貨帳號（面板登入時沒撈到）"
    try:
        m = api.margin(acc)
    except Exception as e:
        return None, "問不到帳戶餘額：%s" % str(e)[:120]
    if m is None:
        return None, "永豐沒有回傳帳戶餘額"
    st = _fetch_word(getattr(m, "status", None))
    if st not in ("fetched", "none"):
        return None, "永豐回的帳戶餘額還沒到齊（%s）" % st
    out = {}
    for k in MARGIN_FIELDS:
        v = getattr(m, k, None)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue                      # ⛔ 看不懂的欄位就不端（⛔ 不要填 0 當作「沒有」）
        out[k] = float(v)
    if not out:
        return None, "永豐回的帳戶餘額看不懂（一個數字都讀不出來）"
    return out, None


# ---------------------------------------------------------------- 對帳

def _broker_legs():
    """
    ⭐ 2026-11-06 混搭：跟券商問部位，**每一種合約分開回** ⇒ {"TMF": {...}, "MXF": {...}}（沒有的那種不在裡面）；
    空手 ⇒ {}；問不到／看不出方向 ⇒ "unknown"。每一腿 {"dir","qty"(微台等值),"entry","product"}。
    broker_position() 用它合成一筆；平倉（_close_mix）每一腿送單前用它確認「那一腿還剩多少」。
    """
    api, acc = _state["api"], _state["account"]
    if api is None:
        return "unknown"
    try:
        rows = api.list_positions(acc) if acc else api.list_positions()
    except Exception as e:
        _state["last_error"] = f"對帳失敗：{str(e)[:120]}"
        return "unknown"
    # ⭐ 2026-11-05：問到了 ⇒ 之前那句「對帳失敗」已經過時，清掉（不然 11-03 04:11 的一次 500
    #    會一直掛在真單卡上，看起來像現在出事）。⛔ 只清對帳自己寫的那種，別的錯誤留著。
    if str(_state.get("last_error") or "").startswith("對帳失敗"):
        _state["last_error"] = None
    # ⭐ 2026-11-06 小台：微台、小台兩種都看（⛔ 只看一種的話，重啟時券商上的小台部位會被當成「沒部位」⇒ 沒有停損）。
    codes = {}
    _cs = dict(_state["contracts"] or {})
    _cs["TMF"] = _state["contract"]         # ⛔ 微台永遠以 _state["contract"] 為準（舊路／測試直接設它）
    for k, c in _cs.items():
        cc = getattr(c, "code", None)
        if cc:
            codes[cc] = k
    if not codes:
        cc = getattr(_state["contract"], "code", None)
        if cc:
            codes[cc] = "TMF"
    legs = {}
    for p in rows or []:
        pc = getattr(p, "code", None)
        if codes and pc not in codes:
            continue
        qty = int(getattr(p, "quantity", 0) or 0)
        if qty == 0:
            continue
        prod = codes.get(pc, "TMF")
        if prod in legs:
            continue                       # 同一種合約只認第一列（跟 11-06 以前一樣）
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
        legs[prod] = {"dir": side, "qty": abs(qty) * RATIO[prod], "entry": float(getattr(p, "price", 0) or 0),
                      "product": prod}
    return legs


def _mix_origin(p):
    """這一筆部位（或落地檔）是不是混搭開出來的。"""
    return isinstance(p, dict) and (p.get("product") == "MIX" or isinstance(p.get("mix0"), dict)
                                    or isinstance(p.get("mix_sent"), dict))


def _mix_ok():
    """
    ⭐ 2026-11-06 品管 M3：券商上兩種合約同時有部位時，要不要合成一筆混搭。
    開關開著 ⇒ 要；開關關著 ⇒ 只有本機部位（或重啟時的落地檔）本來就是混搭開的才要（不然手上的混搭重啟後沒有停損）；
    其他 ⇒ 不要（跟 main 一樣 unknown：開關關著時程式不會開混搭，那多半是他自己的單）。
    """
    if mini_on() or _mix_origin(_state["position"]):
        return True
    if _state["position"] is None:
        return _mix_origin(_meta_load())
    return False


def _leg_position(product):
    """⭐ 2026-11-06：券商上**某一種合約**那一腿 ⇒ dict／None（沒有）／"unknown"（問不到）。"""
    legs = _broker_legs()
    if legs == "unknown":
        return "unknown"
    leg = legs.get(product)
    return dict(leg) if isinstance(leg, dict) else None


def broker_position():
    """
    跟券商問「現在到底有沒有部位」。**記憶體不可信** —— 看門狗會重啟面板，
    重啟後 _state 是空的，但券商那邊的部位還在。

    回傳 {'dir','qty','entry','product'} 或 None；問不到回 'unknown'（不可以當成「沒有部位」）。
    ⭐ 2026-11-06 混搭：微台、小台**同方向**都有 ⇒ 合成一筆 {"product":"MIX","mix":{"MXF":等值,"TMF":等值},
       qty＝等值和、entry＝依等值加權}；**反方向** ⇒ unknown（⛔ 不猜哪一邊是我們的、什麼都不動、示警）。
       ⚠️ 開關沒開也照樣認（跟 11-06 小台同一條理由：手上的部位要看得到，不然重啟後沒有停損）。
    """
    legs = _broker_legs()
    if legs == "unknown":
        return "unknown"
    if not legs:
        return None
    if len(legs) == 1:
        return dict(next(iter(legs.values())))
    if not _mix_ok():
        # ⭐ 2026-11-06 品管 M3：開關關著、本機／落地檔也不是混搭開的 ⇒ 跟 main 一樣：兩種同時有部位 ⇒ unknown
        _state["last_error"] = "券商上微台和小台同時有部位 —— 分不出哪一筆是程式的，不敢動，請自己確認"
        return "unknown"
    m, t = legs["MXF"], legs["TMF"]
    if m["dir"] != t["dir"]:
        _state["last_error"] = "券商上微台和小台方向相反 —— 分不出哪一筆是程式的，不敢動，請自己確認"
        return "unknown"
    q = m["qty"] + t["qty"]
    return {"dir": m["dir"], "qty": q,
            "entry": round((m["entry"] * m["qty"] + t["entry"] * t["qty"]) / q, 2),
            "product": "MIX", "mix": {"MXF": m["qty"], "TMF": t["qty"]}}


# ⭐ 重啟撿回部位時，「這一口自己的停損點數」要從哪裡補（2026-09-15 加）。
#    自動下單改成 ±0.5% 之後，每一口部位帶自己的 `sl_points`（enter() 存進去）；
#    撿回來的部位沒有這個欄位 ⇒ 面板停損會掉回 SL_POINTS（手動那一套）⇒ 自動下單那一口
#    被提早洗掉。所以撿回來的那一刻問一次掛勾（`auto_fire.recover_meta`）。
# ⛔ 預設是 None（這個檔不 import auto_fire —— 下單的模組不依賴送單策略），
#    **只有 live_panel.main() 會接上去**（治具與測試不接就是「照舊用 SL_POINTS」）。
# ⛔⛔ reconcile() 在 4Hz 主迴圈上被叫（reconcile_tick），而且這裡拿著 _lock ⇒
#    掛上去的函式**只准讀記憶體**：一行 I/O、一次網路、一個鎖都不准有。
RECOVER_HOOK = None
# ⭐ `no_tp`（2026-09-16）：撿回來的那一口若對得上「開箱」開出來的部位，要把
#    「這一口沒有停利」也補回去 —— 少了它，畫面會顯示一個不存在的停利 130 點。
# ⭐ `entry_qty`／`entry_fill`（2026-10-14 多口）：撿回來的那口對得上帳本、而且帳本記的是多口 ⇒
#    補回「我們開了幾口、成交均價多少」（平倉每張最多送幾口、停損基準靠它）。一口的帳本不補 ⇒ 行為照舊。
_RECOVER_KEYS = ("sl_points", "tp_points", "sl_src", "sl_warn", "no_tp", "entry_qty", "entry_fill", "multi_unknown",
                 "unknown_src")


def _recover_meta(got):
    """把掛勾回的欄位補到撿回來的部位上。⛔ 永遠不往外丟例外（這裡是對帳路徑）。"""
    hook = RECOVER_HOOK
    if hook is None:
        return
    try:
        extra = hook(dict(got))
    except Exception as e:
        got["sl_src"] = "unmatched"
        got["sl_warn"] = "補停損點數時出錯（%s）—— 停損用手動真單那一套" % str(e)[:80]
        return
    if isinstance(extra, dict):
        got.update({k: extra[k] for k in _RECOVER_KEYS if k in extra})


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


def _multi_pos(p):
    """這一口是不是**我們自己開的多口**部位（送出去時就是多口）。⛔ 一口的部位與手動部位一律 False ⇒ 行為照舊。"""
    return isinstance(p, dict) and _pq_ok(p, p.get("qty_sent")) and p["qty_sent"] > 1 or \
        (isinstance(p, dict) and _own_qty(p) > 1)


def _ours_left(p):
    """這一筆還剩幾口是**我們的**：自己開的口數 − 先前已經出去的那幾段（legs）。"""
    own = _own_qty(p)
    gone = sum(int(l.get("qty") or 0) for l in (p or {}).get("legs") or [])
    return max(0, own - gone)


def _tp_matches(cur, n, tsums):
    """停利單的明細（扣掉之前算過的）剛好是 n 口 ⇒ 這 n 口確定是停利單吃掉的（我們的）。"""
    if tsums is None:
        return False
    used = cur.get("tgt_used") or [0.0, 0.0]
    return abs((tsums[1] - float(used[1])) - n) < 1e-9 and n > 0


def _his_manual(bp):
    """
    券商上剩下的、確定是**他自己加碼的**那幾口 ⇒ 當成手動部位（停損照手動那一套、收盤⛔ 不碰）。
    ⭐ 2026-10-14 QA r2 必修 1：掛 `handover` 旗子 —— 日盤收盤、夜盤 04:58 看到就⛔ 不平
       （舊版夜盤只比方向＋進場價，他加碼的價錢跟我們差不多 ⇒ 04:58 把他的口數當成我們的平掉，QA N1）。
    """
    q = int(bp.get("qty") or 0)
    out = {"dir": bp["dir"], "entry": float(bp.get("entry") or 0), "qty": q,
           "product": bp.get("product") or "TMF",
           "entry_time": None, "target_trade": None, "recovered": True, "sl_src": "manual",
           "handover": True, "bpx": float(bp.get("entry") or 0),
           "sl_warn": "程式開的那幾口已經出場；剩下 %d 口是你自己加碼的 —— 面板照手動單的規矩用 %g 點停損，"
                      "收盤／04:58 不會自動平" % (q, MANUAL_SL_POINTS)}
    if isinstance(bp.get("mix"), dict):
        out["mix"] = dict(bp["mix"])          # ⭐ 2026-11-06 他手上剩的是混搭 ⇒ 兩腿各幾口要跟著（平倉才分得了腿）
    return out


# ⭐ 手動單的停損點數（只拿來寫給人看的那句話；正本 live_panel.SL_POINTS，面板啟動時會蓋過來）
MANUAL_SL_POINTS = 130.0


def pos_note(p=None):
    """
    ⭐ 2026-10-14 QA r2 必修 2：多口部位「不照平常規矩走」時給人看的一句話 ⇒ (句子, 種類) 或 (None, None)。
    ⛔ 只讀記憶體（面板 poll 執行緒與畫面都會叫）。一口的部位一律 (None, None) ⇒ 畫面跟以前一樣。
    """
    p = _state["position"] if p is None else p
    if not isinstance(p, dict):
        return None, None
    sp = p.get("shrink_pend") if p.get("unknown_src") == "shrink" else None
    if p.get("multi_unknown") and isinstance(sp, dict) and sp.get("miss_since") is not None \
            and time.time() - float(sp.get("since") or sp["miss_since"]) >= REAL_WARN_S:
        # ⭐ 2026-10-15 QA r5：等對帳等超過 1 分鐘還問不到券商 ⇒ 照實講多久了
        mins = max(1, int((time.time() - float(sp.get("since") or sp["miss_since"])) // 60))
        return ("券商已實現損益問不到 %d 分鐘，停損不會執行，請自己處理（重啟後這筆 %s 口還沒對完帳）"
                % (mins, p.get("qty"))), "unknown"
    if p.get("multi_unknown") and isinstance(sp, dict) and p.get("sl_warn"):
        return str(p["sl_warn"]), "unknown"          # 等對帳（1 分鐘內）：照實講「正在對帳，對完之前不平」
    if p.get("multi_unknown"):
        return ("重啟後這筆部位（%s 口）對不上程式開的多口紀錄，分不出哪幾口是你自己的 ⇒ "
                "收盤／04:58 不會自動平、停損也不會幫你平，請自己到大戶投處理" % p.get("qty")), "unknown"
    if p.get("handover"):
        return ("程式開的那幾口已經出場；剩下 %s 口是你的（你自己加碼的）⇒ 面板照手動單的規矩用 %g 點停損，"
                "收盤／04:58 不會自動平，記得自己處理" % (p.get("qty"), MANUAL_SL_POINTS)), "handover"
    if p.get("touched"):
        return ("你在這一筆上自己加碼過 ⇒ 收盤／04:58 不會自動平，記得自己處理；停損只會平程式開的 %d 口"
                "（你加的那幾口不動）" % _ours_left(p)), "touched"
    if p.get("own") and isinstance(p.get("diff_susp"), dict):
        # ⭐ 第三輪品管：帳本部位 ⇒ 券商部位跟程式明細算的不一樣，確認中（⛔ 不採信、照明細平）
        _d = p["diff_susp"].get("sig") or [0, 0]
        return ("券商部位跟程式明細算的不一樣（小台 %+d、微台 %+d 口等值），確認中 —— 停損、收盤照程式明細的 %s 口等值平"
                % (_d[0], _d[1], p.get("qty"))), "fill"
    if p.get("late_pending"):
        # ⭐ 2026-11-06 品管 M7：看得到的已經平掉、只剩晚到那一腿（委託單明細說成交了、券商部位還沒回報）
        return ("進場時%s的成交券商還沒回報（委託單明細說成交了）—— 回報之前平倉單送不出去（看不到就不送），"
                "回報後停損、收盤照常平；等不及請到大戶投處理" % "、".join(
                    "%s %s 口等值" % ({"MXF": "小台", "TMF": "微台"}.get(k, k), v)
                    for k, v in p["late_pending"].items())), "fill"
    if p.get("fill_warn") and not p.get("late_fill"):
        return ("成交口數一度對不上，已照券商口數處理（送 %s 口，券商顯示 %s 口）—— 請到大戶投確認"
                % (p.get("qty_sent"), p.get("entry_qty"))), "fill"
    if p.get("late_fill"):
        return ("成交口數一度對不上（部位回報慢）；後來對上了，照實際成交的 %s 口處理" % p.get("entry_qty")), "fill"
    return None, None


# ── 多口部位的狀態落地（⭐ QA r2 必修 4：重啟後補回「他動過／出去過幾段／後補成交／交接」）──
#    ⛔ 只有多口部位（與交接出去的手動部位）才寫；一口的部位從不寫 ⇒ 一口那條路照舊。
#    檔案在 ORDER_DIR 底下（broker 自己的資料夾、gitignore、跟著備份）；寫法 tmp → replace。
META_NAME = "_open_multi.json"
_META_KEYS = ("dir", "entry_fill", "entry_qty", "qty_sent", "legs", "tgt_used", "touched", "late_fill",
              "handover", "entry_time", "sl_points", "tp_points", "no_tp", "fill_warn", "qty",
              "bpx", "fill_lo", "fill_hi", "entry_day", "real_base", "product",
              # ⭐ 2026-11-06 混搭：兩腿各幾口（mix）＋這一趟進場時兩腿各成交幾口（mix0）
              "mix", "mix0",
              # ⭐ 2026-11-06 晚到的腿：送出去的兩腿（等值、時刻、委託單編號）、帳本記的進場價、併過晚到的腿
              "mix_sent", "mix_sent_at", "mix_sent_ids", "entry0", "late_leg",
              # ⭐ 2026-11-06 品管 M4／M7：每一腿停利成交量、我們自己平掉的量（平倉上限用）、等晚到那一腿
              "tgt_fill", "closed_by_leg", "late_pending", "late_pending_at")
_META = {"last": None}


def _meta_wanted(p):
    return isinstance(p, dict) and (_multi_pos(p) or bool(p.get("handover")) or bool(p.get("own")))


def _meta_sync():
    """把現在這口多口部位的狀態寫下來（沒變就不寫）；部位沒了 ⇒ 清掉。⛔ 永遠不丟例外。⛔ 不在 _lock 裡叫。"""
    try:
        p = _state["position"]
        path = ORDER_DIR / META_NAME
        if _meta_wanted(p) and p.get("multi_unknown"):
            return                          # 分不出（含等對帳中）⇒ ⛔ 不覆蓋落地檔（再重啟一次還是照原本那份比）
        if _meta_wanted(p):
            # ⭐ 第三輪品管：帳本部位落地帳本（每一張單的明細數字＋委託單編號）
            m = _own_meta(p) if p.get("own") else {k: p.get(k) for k in _META_KEYS if p.get(k) is not None}
            m["day"] = str(date.today())
            key = json.dumps(m, ensure_ascii=False, sort_keys=True, default=str)
            if key == _META["last"]:
                return
            ORDER_DIR.mkdir(exist_ok=True)
            tmp = path.with_name(META_NAME + ".tmp")
            tmp.write_text(key, encoding="utf-8")
            for i in range(20):
                try:
                    tmp.replace(path)
                    break
                except PermissionError:
                    time.sleep(0.01)
            _META["last"] = key
        elif p is None and (_META["last"] is not None or not _META.get("swept")):
            # 部位沒了 ⇒ 清掉（這支程式第一次看到空手時也掃一次：上次當掉前留下的舊檔）
            # ⭐ 2026-11-06 第二輪品管 QA2-6：重啟時那一筆正在等晚到的腿（late_pending、LATE_LEG_S 內）⇒ 券商「空手」是假的，⛔ 不刪
            _m = _meta_load() if _META["last"] is None else None
            if not (isinstance(_m, dict) and (_m.get("own") or (_m.get("late_pending")
                    and time.time() - float(_m.get("late_pending_at") or 0) <= LATE_LEG_S))):
                path.unlink(missing_ok=True)
            _META.update(last=None, swept=True)
    except Exception:                       # noqa: BLE001  ⛔ 寫不進去不影響停損與平倉
        pass


def _meta_load():
    """⇒ 上次落地的多口狀態（dict）或 None。⛔ 在拿 _lock 之前叫（檔案 I/O）。"""
    try:
        path = ORDER_DIR / META_NAME
        if not path.exists():
            return None
        m = json.loads(path.read_text(encoding="utf-8"))
        return m if isinstance(m, dict) else None
    except Exception:                       # noqa: BLE001
        return None


META_PX_EPS = 0.05     # 券商均價「沒變」的容差（點；均價回報到小數第 2 位）
META_FILL_TOL = 1.0    # 我們那幾口的成交價區間外擴多少（＝ auto_fire.EOD_PX_TOL，跟 _ent_px_range 同一把尺）


def _meta_apply(got, meta, late_pre=None):
    """
    撿回來的部位 ＋ 落地的多口狀態 ⇒ "ok"（證據全部對得上，補回）／"mismatch"（落地的是新的、但有一項對不上）／
    None（沒有可用的落地檔：沒有、太舊）。
    ⭐ QA r3（PM 原則）：**寧可認不出來，也不可以認錯人** ⇒ 補回要證據全部對得上：
      ① 方向一樣；② 券商現在的口數 ＝ 落地時券商的口數（他動過 ⇒ 我們剩的＋他的，落地時就是那個合計）；
      ③ 券商現在的均價 ＝ 落地時券商的均價（±META_PX_EPS）——停機期間停利成交幾口、他自己平掉再開，均價或口數一定會動；
      ④ 沒動過、沒交接 ⇒ 券商均價也要落在我們成交價區間（fill_lo～fill_hi，沒有就 entry_fill）±META_FILL_TOL。
      任何一項對不上 ⇒ "mismatch"（呼叫端標「分不出」、不平、示警）。
    """
    if not isinstance(meta, dict):
        return None
    try:
        age = (date.today() - date.fromisoformat(str(meta.get("day")))).days
    except ValueError:
        return None
    if not (0 <= age <= 1):
        return None
    if late_pre:
        # ⭐ 2026-11-06 晚到的腿：證據對得上 ⇒ 拿「併好之後」的落地狀態往下比（對不上 ⇒ 照原本那份比 ⇒ mismatch）
        _ml = _meta_late(got, meta, late_pre)
        if _ml is not None:
            meta = _ml
    q = int(got.get("qty") or 0)
    if q == 1 and not meta.get("handover") and not meta.get("late_pending"):
        # ⭐ QA r4（G7）：撿回來的是 1 口、落地檔又不是交接 ⇒ 不套用落地檔，走原本的帳本認人（1 口那一套）
        #    ——落地檔可能是更早那一趟多口留下的（當掉沒清），拿它比 1 口只會把 1 口的單誤標成分不出。
        return None
    try:
        px = float(got.get("entry"))
        mq, mpx = int(meta.get("qty")), float(meta.get("bpx"))
    except (TypeError, ValueError):
        return "mismatch"
    # ⭐ 2026-11-06 混搭：合約也是證據。
    #    ・落地時是混搭、現在券商只剩其中一種（停機期間那一腿出場了）⇒ 剩的那一腿不可以比落地時那一腿多，
    #      其餘交給下面「口數變少」那條（還要過成交價區間、再跟已實現損益對帳）。
    #    ・兩邊都是混搭 ⇒ 每一腿都不可以變多（一腿少一腿多＝他動過）。
    #    ・其他合約對不上（微台↔小台、單一變混搭）⇒ mismatch（⛔ 不猜）。
    gp, mp = got.get("product") or "TMF", meta.get("product") or "TMF"
    gm = got.get("mix") if isinstance(got.get("mix"), dict) else {}
    mm = meta.get("mix") if isinstance(meta.get("mix"), dict) else {}
    try:
        if gp != mp:
            if not (mp == "MIX" and gp in RATIO and q <= int(mm.get(gp) or 0)):
                return "mismatch"
        elif gp == "MIX" and any(int(gm.get(k) or 0) > int(mm.get(k) or 0) for k in RATIO):
            return "mismatch"
    except (TypeError, ValueError):
        return "mismatch"
    late = shrink = False
    if (not meta.get("touched") and not meta.get("handover") and not meta.get("fill_warn")
            and meta.get("dir") == got.get("dir") and 1 <= q < mq):
        # ⭐ QA r4（G3）：停機期間口數**變少**（停利單部分成交、或他在別處平掉幾口）、沒動過、沒交接 ⇒
        #    剩下的「可能」都是我們的 —— 先過下面第 ④ 道（券商均價還在我們成交價區間 ±1），
        #    再由工作執行緒問一次券商今天的已實現損益（resolve_shrink，⛔ 不在 4Hz 迴圈打網路）：
        #    已實現裡我們平掉的口數**超過**帳上應該有的 ⇒ 有幾口是他的 ⇒ 維持分不出；沒有 ⇒ 認成我們的，剩幾口就幾口。
        shrink = True
    elif (meta.get("fill_warn") and not meta.get("touched") and not meta.get("legs") and not meta.get("handover")
            and meta.get("dir") == got.get("dir") and mq < q <= int(meta.get("qty_sent") or 0)):
        # 唯一允許的變化：落地時成交口數就還沒定案（券商部位慢一拍，委託單明細早就說成交了 qty_sent 口），
        # 重啟時券商補到 ≤ 送出去的口數 ⇒ 用委託單明細的成交價區間驗（下面 ④），對得上才算我們的（late_fill）
        late = True
    elif meta.get("dir") != got.get("dir") or q != mq or abs(px - mpx) > META_PX_EPS:
        return "mismatch"
    if meta.get("handover"):
        got.update(handover=True, sl_src="manual", bpx=px)
        got["sl_warn"] = pos_note(dict(got))[0]
        for k in ("sl_points", "tp_points", "no_tp", "multi_unknown", "unknown_src"):
            got.pop(k, None)
        return "ok"
    probe = {k: meta.get(k) for k in _META_KEYS if meta.get(k) is not None}
    if late:
        probe["entry_qty"] = q
    down_n = 0
    if shrink:
        down_n = _ours_left(probe) - q                    # 停機期間少掉的口數（價錢不知道 ⇒ 留白的一段 leg）
        if down_n <= 0:
            return "mismatch"
        probe["legs"] = list(probe.get("legs") or []) + [{"qty": down_n, "px": None, "src": "elsewhere",
                                                          "at": "停機期間"}]
    left = _ours_left(probe)
    if left <= 0 or (not meta.get("touched") and q != left):
        return "mismatch"
    if not meta.get("touched"):
        lo, hi = meta.get("fill_lo"), meta.get("fill_hi")
        if not (isinstance(lo, (int, float)) and isinstance(hi, (int, float))):
            lo = hi = meta.get("entry_fill")
        try:
            if not (float(lo) - META_FILL_TOL <= px <= float(hi) + META_FILL_TOL):
                return "mismatch"
        except (TypeError, ValueError):
            return "mismatch"
    for k in _META_KEYS:
        if k in ("qty", "product", "mix"):
            # ⭐ 2026-11-06：合約與兩腿口數一律以**券商現在**為準（落地的是停機前的；混搭一腿出場後會不一樣）
            continue
        if meta.get(k) is not None:
            got[k] = meta[k]
    if late:
        got.update(entry_qty=q, late_fill=True)
    if shrink:
        got["legs"] = probe["legs"]
    if got.get("entry_fill") is not None:
        got["entry"] = float(got["entry_fill"])
    got["bpx"] = px
    got["sl_src"] = "meta"
    if shrink:
        # 先標「分不出」（停損、收盤都不平），等工作執行緒對完已實現損益再放行（見 resolve_shrink）
        keep = {k: got.get(k) for k in ("sl_points", "tp_points", "no_tp") if got.get(k) is not None}
        got["shrink_pend"] = {"legs_before": sum(int(l.get("qty") or 0) for l in (meta.get("legs") or [])),
                              "down_n": down_n, "since": time.time()}
        _mark_unknown(got, "重啟後撿回的部位比面板關掉前少了 %d 口（停機期間有成交）—— 正在跟券商的已實現損益對帳，"
                           "對完之前停損、收盤都不會幫你平" % down_n, "shrink")
        got.update(keep)                    # 停損點數留著（放行之後要用）；分不出期間停損本來就不會送單
        return "shrink"                     # ⛔ 帳本那邊的補點數（_recover_poll）不要再蓋過去
    for k in ("multi_unknown", "unknown_src", "sl_warn"):
        got.pop(k, None)
    return "ok"


REAL_RETRY_S = 10.0    # 等對帳期間問不到已實現損益 ⇒ 每幾秒才再問一次（⛔ 不要每輪都打券商）
REAL_WARN_S = 60.0     # 等超過幾秒還問不到 ⇒ 提示改寫成「問不到 N 分鐘，停損不會執行」
REAL_PX_EPS = 0.01     # 已實現那一列的進場價要落在我們成交價區間內（券商的每口進場價＝成交價；⛔ 不再外擴 1 點）


def _real_snapshot():
    """
    ⭐ 2026-10-15：多口部位一成立（停利掛完之後、工作執行緒）問一次券商今天的已實現損益，記下**當下已經有的那幾列的 id**
    （`real_base`）—— 那些是我們進場**之前**就平掉的單（例如他稍早的手動單），之後對帳（resolve_shrink）一律排除。
    問不到 ⇒ real_base＝None（之後對帳就不排除任何列 ＝ 保守：只會更容易判成分不出）。⛔ 永遠不丟例外。
    """
    try:
        real = realized_today(fresh=True)
        base = None if real is None else sorted(str(r.get("id")) for r in real if r.get("id") is not None)
        with _lock:
            p = _state["position"]
            if isinstance(p, dict):
                p["real_base"] = base
        _meta_sync()
    except Exception:                       # noqa: BLE001
        pass


def resolve_shrink(now=None):
    """
    ⭐ QA r4（G3）：重啟時口數變少、先標了「分不出（shrink）」的部位 ⇒ 問券商的已實現損益再決定。
    ⚠️ 工作執行緒（auto_fire／night_fire 的 _recover_poll）叫；⛔ 不在 4Hz 迴圈、⛔ 不在 _lock 裡打網路。
    算「我們那幾口」平掉幾口：方向一樣、進場價落在我們成交價區間（±REAL_PX_EPS）、⭐ 而且**不是進場那一刻就已經在的列**
      （real_base；2026-10-15 QA r5 S4：他當天稍早同方向同價位的手動單）。
    ⭐ 夜盤跨午夜（進場那天 < 今天）：連進場那天的已實現一起看（只有夜盤會這樣；日盤 13:43:30 前一定平完）。
    ≤ 帳上應該有的（之前出去的段＋停機期間少掉的）⇒ 認成我們的；超過 ⇒ 分不出（meta）；
    問不到 ⇒ 先不動，⭐ 而且 REAL_RETRY_S 秒內不再問（2026-10-15 QA r5：舊版每輪都打券商，約每秒 4 次）。
    ⇒ "ok"／"mismatch"／None（沒事、問不到或還在退避）。
    """
    p = _state["position"]
    if not isinstance(p, dict) or p.get("unknown_src") != "shrink" or not isinstance(p.get("shrink_pend"), dict):
        return None
    sp = p["shrink_pend"]
    now = time.time() if now is None else now
    if now < float(sp.get("next_at") or 0):
        return None                                  # 退避中
    real = realized_today(fresh=True)               # ⛔ 鎖外面
    eday = p.get("entry_day")
    if real is not None and eday and str(eday) < str(date.today()):
        prev = _realized_day(eday)                   # 夜盤跨午夜：進場那天的也要
        real = None if prev is None else list(prev) + list(real)
    if real is None:
        with _lock:
            sp["next_at"] = now + REAL_RETRY_S
            sp.setdefault("miss_since", now)
        return None
    base = p.get("real_base")
    skip = {}
    for i in (base or []):
        skip[str(i)] = skip.get(str(i), 0) + 1
    lo, hi = p.get("fill_lo"), p.get("fill_hi")
    eps = REAL_PX_EPS
    if not (isinstance(lo, (int, float)) and isinstance(hi, (int, float))):
        # ⭐ QA r6（S5）：進場當下成交明細沒回報 ⇒ 沒有各口成交價區間，只剩均價 ⇒ ⛔ 不可以用 ±0.01
        #    （多口不同成交價時會漏數我們被平掉的口數 ⇒ 誤認回來）⇒ 退回 ±META_FILL_TOL（寧可多算、判成分不出）
        lo = hi = p.get("entry_fill")
        eps = META_FILL_TOL
    closed = 0
    for r in real:
        try:
            rid = str(r.get("id")) if r.get("id") is not None else None
            if rid is not None and skip.get(rid):
                skip[rid] -= 1                       # 進場那一刻就在的列 ⇒ 不是我們這一趟平的
                continue
            if r.get("dir") == p.get("dir") and float(lo) - eps <= float(r.get("entry")) <= float(hi) + eps:
                closed += int(r.get("qty") or 1)
        except (TypeError, ValueError):
            continue
    allowed = int(sp.get("legs_before") or 0) + int(sp.get("down_n") or 0)
    with _lock:
        if _state["position"] is not p:
            return None
        if closed > allowed:
            p["unknown_src"] = "meta"
            p["sl_warn"] = ("重啟後撿回的部位：券商今天的已實現裡我們那幾口平掉了 %d 口、比帳上的 %d 口多 ⇒ "
                            "剩下的有幾口是你的，分不出 —— 停損、收盤、04:58 都不會幫你平，請自己到大戶投處理" % (closed, allowed))
            p.pop("shrink_pend", None)
            out = "mismatch"
        else:
            for k in ("multi_unknown", "unknown_src", "sl_warn", "shrink_pend"):
                p.pop(k, None)
            p["sl_src"] = "meta"
            out = "ok"
    _log("position_shrink_" + out, {"ok": out == "ok", "closed_realized": closed, "allowed": allowed,
                                    "qty": p.get("qty"), "why": "停機期間口數變少 ⇒ 跟已實現損益對帳"})
    _meta_sync()
    return out


def _mark_unknown(got, why, src):
    """撿回來的部位標「分不出是誰的」：停損、收盤、04:58 都不平；畫面不畫停損價。"""
    got["multi_unknown"] = True
    got["unknown_src"] = src
    got["sl_src"] = "unmatched"
    got["sl_warn"] = why
    for k in ("sl_points", "tp_points", "no_tp"):
        got.pop(k, None)


def _reconcile_prefetch(cur, pos):
    """
    ⭐ 2026-10-14 QA 必修 7：reconcile 要用到的「問券商」那幾件事（update_status＋委託單成交明細）
    ⛔ 一律在拿 _lock **之前**做完 —— 主迴圈的停損檢查也要那把鎖。只有我們的多口部位口數變了才問。
    ⇒ {"tsums": 停利單明細, "esums": 進場那張單的明細}（問不到的就是 None）。
    """
    out = {"tsums": None, "esums": None}
    if not _multi_pos(cur) or not is_live() or _state["api"] is None:
        return out
    newq = None if pos in (None, "unknown") else pos.get("qty")
    if newq == cur.get("qty"):
        return out
    _update_status()
    if cur.get("target_trades") is not None or cur.get("entry_trades") is not None:
        # ⭐ 2026-11-06 混搭：兩腿各一張 ⇒ 明細合起來（_deal_sums 已經換成微台等值）。
        #    混搭開出來但只成交一腿的部位：停利是單張（target_trade）、進場單是兩張（entry_trades）⇒ 一起算。
        # ⭐ 第二輪品管 QA2-2：撤掉／成交完的停利單也要算（撤單那一瞬間成交的那段才有價錢，成績單才有點數）
        tl = list((cur.get("target_trades") or {}).values()) + [cur.get("target_trade")] + \
            list((cur.get("target_done_trades") or {}).values())
        out["tsums"] = _sum_deals(tl)
        out["esums"] = _sum_deals((cur.get("entry_trades") or {}).values())
        return out
    out["tsums"] = _deal_sums(cur.get("target_trade"))
    out["esums"] = _deal_sums(cur.get("entry_trade"))
    return out


def _sum_deals(trades):
    """
    ⭐ 2026-11-06 混搭：好幾張單的成交明細合起來 ⇒ (Σ價×口, Σ口, 最低價, 最高價)（微台等值）。
    沒有任何一張 ⇒ None；⛔ 任何一張讀不到 ⇒ None（⛔ 不拿一部分冒充全部）。⛔ 不呼叫 update_status（呼叫端負責）。
    """
    trades = [t for t in trades if t is not None]
    if not trades:
        return None
    pq = q = 0.0
    lo = hi = None
    for t in trades:
        s = _deal_sums(t)
        if s is None:
            return None
        pq, q = pq + s[0], q + s[1]
        if s[2] is not None:
            lo = s[2] if lo is None else min(lo, s[2])
            hi = s[3] if hi is None else max(hi, s[3])
    return pq, q, lo, hi


def reconcile():
    """啟動時／每次要動作之前叫。把券商的實況寫回 _state。"""
    if is_live():
        # ⭐ 第三輪品管：帳本部位（含重啟後從落地檔補回）走自己的對帳（⛔ 不拿舊資料決定任何事）
        cur = _state["position"]
        if cur is None:
            m = _meta_load()
            if isinstance(m, dict) and m.get("own"):
                r = _own_restore(m)
                if r is not None:
                    with _lock:
                        if _state["position"] is None:
                            _state["position"] = r
                            _log("own_restored", {"ok": True, "qty": r.get("qty"), "why": "重啟後從落地的帳本補回部位"})
                    cur = _state["position"]
        if isinstance(cur, dict) and cur.get("own"):
            out = _own_reconcile(cur)
            if isinstance(out, dict) and out.get("tp_pending") is not None:
                _own_tp_fill(out)
            _meta_sync()
            return out
    pos = broker_position()
    if pos == "unknown":
        return "unknown"
    if isinstance(pos, dict) and _state["position"] is None and time.time() - _RECENT_CLOSE["at"] < RECENT_CLOSE_S \
            and pos.get("dir") == _RECENT_CLOSE["dir"] \
            and all(int(v) <= int(_RECENT_CLOSE["legs"].get(p) or 0) for p, v in _mix_of(pos).items()):
        # ⭐ 第三輪品管 #3：帳本部位剛平完，券商還顯示同方向、不超過剛平掉的口數 ⇒ 舊資料，⛔ 不撿回
        # ⭐ 2026-11-06 第五輪品管：⛔ 這只是「不撿回成部位」，不是「空手」—— 記下這一刻，can_enter() 照樣擋
        _RECENT_CLOSE["hid_at"] = time.time()
        return None
    _cur0 = _state["position"]
    if isinstance(pos, dict) and isinstance(_cur0, dict) and isinstance(_cur0.get("mix_sent"), dict):
        # ⭐ 2026-11-06 晚到的腿：先認「券商多出來的那一腿是不是這一趟送出去、晚回報的成交」（⛔ 鎖外面問明細）
        add = _late_leg(_cur0, pos)
        if isinstance(add, dict):
            with _lock:
                if _state["position"] is _cur0:
                    _late_merge(_cur0, add)
    if isinstance(pos, dict) and isinstance(_cur0, dict):
        bad = _mix_mismatch(_cur0, pos)
        if bad:
            # ⭐ 2026-11-06：本機記的合約跟券商上的對不上 ⇒ ⛔ 不猜、不清本機部位、不平、示警
            _state["last_error"] = bad
            return "unknown"
    pre = _reconcile_prefetch(_cur0, pos)          # ⛔ 鎖外面問券商
    # ⭐ QA r2 必修 4：要撿回部位時，先把上次落地的多口狀態讀好（⛔ 鎖外面讀檔）
    meta_pre = _meta_load() if (_cur0 is None and isinstance(pos, dict)) else None
    # ⭐ 2026-11-06 晚到的腿＋重啟：落地時還沒看到的那一腿 ⇒ 用委託單編號問回兩張進場單的明細（⛔ 鎖外面）
    late_pre = _late_prefetch_meta(meta_pre, pos) if meta_pre is not None else None
    out = _reconcile_locked(pos, _cur0, pre, meta_pre, late_pre)
    _meta_sync()                                   # ⛔ 鎖外面寫檔（多口部位才寫；一口的部位什麼都不做）
    return out


# ── ⭐ 2026-11-06 晚到的腿（PM：「進場一腿晚回報 ⇒ unknown ⇒ 停損不執行」不接受）──────────────────────
#    混搭進場等 FILL_WAIT 秒，某一腿（或某一腿的一部分）那段時間沒出現在券商部位 ⇒ 先照實際看到的當部位；
#    之後券商多出來的那一腿，**證據全部對得上**才併進來（⛔ 不是只看時間）：
#      ① 方向一樣；② 那一腿累計不超過這一趟送出去的量（mix_sent）；
#      ③ 那張進場單**自己的成交明細**說它成交了 ≥ 券商現在那一腿的口數（⛔ 明細問不到／不夠 ⇒ 不認）；
#      ④ 送出後 LATE_LEG_S 秒內（太久以前的就不是「回報慢」了）；⑤ 他沒動過（touched／交接／分不出 ⇒ 不認）。
#    任何一項不過 ⇒ 不併，照舊走 _mix_mismatch（unknown）／多口的「口數變多」那條。
LATE_LEG_S = 300.0


def _leg_have(p):
    """⭐ 2026-11-06：這一趟每一腿**已經算進部位**的進場量（mix0；單一合約＝entry_qty）⇒ {合約: 等值}。"""
    if isinstance(p.get("mix0"), dict):
        return {k: int(v or 0) for k, v in p["mix0"].items()}
    k = p.get("product") if p.get("product") in RATIO else "TMF"
    return {k: int(p.get("entry_qty") or p.get("qty") or 0)}


def _late_leg(cur, pos):
    """⇒ {合約: {"up": 多出來的等值, "px": 那張進場單的成交均價, "lo","hi"}}；不是晚到的腿 ⇒ None。⛔ 鎖外面叫（會問券商明細）。"""
    ms = cur.get("mix_sent")
    if not isinstance(ms, dict) or not is_live() or _state["api"] is None:
        return None
    if cur.get("touched") or cur.get("handover") or cur.get("multi_unknown"):
        return None
    if pos.get("dir") != cur.get("dir"):
        return None
    try:
        age = time.time() - float(cur.get("mix_sent_at"))
    except (TypeError, ValueError):
        return None
    if not (0 <= age <= LATE_LEG_S):
        return None
    bm, cm = _mix_of(pos), _mix_of(cur)
    ups = {p: bm.get(p, 0) - cm.get(p, 0) for p in RATIO if bm.get(p, 0) > cm.get(p, 0)}
    if not ups:
        return None
    _update_status()
    ets = cur.get("entry_trades") or {}
    have = _leg_have(cur)
    out = {}
    for p, up in ups.items():
        if p not in ms:
            continue                          # 這一趟沒送這種合約 ⇒ 多出來的不是晚到的腿
        s = _deal_sums(ets.get(p))
        if s is None or s[1] <= 0:
            continue                          # 那張進場單的明細沒說它成交 ⇒ 不是它 ⇒ 不認（多出來的交給「他加碼」那套）
        # ⭐ 2026-11-06（性質測試抓到）：明細裡**已經算進部位**的那幾口不能再認一次 —— 平掉一部分之後券商回報退回舊資料
        #    （口數比本機多），舊版會把它當成晚到的腿併進來 ⇒ 部位變大、之後平過頭。只認「明細比我們已經算的還多」的那一段。
        # ⭐ 第二輪品管：券商那一腿比「晚到的成交」還多（他也加了）⇒ 只認明細那一段，剩的交給「他加碼」那套（舊版整個不認 ⇒
        #    我們晚到的那幾口被當成他的）。明細一定 ≤ 送出量 ⇒ 不會超過 mix_sent。
        u = min(int(up), int(round(s[1])) - int(have.get(p) or 0), int(ms.get(p) or 0) - int(have.get(p) or 0))
        u = (u // RATIO[p]) * RATIO[p]
        if u <= 0:
            continue
        out[p] = {"up": u, "px": round(s[0] / s[1], 2), "lo": s[2], "hi": s[3], "age": round(age, 1)}
    return out or None


def _late_merge(cur, add):
    """
    把晚到的腿併進部位（⛔ 呼叫端拿著 _lock、而且確認 cur 還是現在那一筆）：
    qty／entry_qty 加上去、進場價依等值重新加權（entry_fill 跟著改；帳本當初記的進場價留在 entry0）、
    兩腿都有 ⇒ MIX＋mix、mix0 加上那一腿；單張停利單改放進 target_trades（之後平倉走 _close_mix 一起撤）。
    ⚠️ 晚到那一腿**沒有停利單**（⛔ 鎖裡不送單）⇒ 寫進 fill_warn 講給他聽；停損、收盤照樣平整筆。
    """
    q0 = int(cur.get("qty") or 0)
    cm = _mix_of(cur)
    ef = float(cur.get("entry_fill", cur.get("entry")))
    addq = sum(v["up"] for v in add.values())
    tot = q0 + addq
    ef2 = round((ef * q0 + sum(v["px"] * v["up"] for v in add.values())) / tot, 2)
    nm = {p: int(cm.get(p, 0)) + int(add.get(p, {}).get("up", 0)) for p in ("MXF", "TMF")}
    cur.setdefault("entry0", ef)
    if cur.get("target_trade") is not None:
        cur["target_trades"] = {cur.get("product") or "TMF": cur["target_trade"]}
        cur["target_trade"] = None
    m0 = cur.get("mix0") if isinstance(cur.get("mix0"), dict) else cm
    cur["mix0"] = {p: int(m0.get(p, 0)) + int(add.get(p, {}).get("up", 0)) for p in ("MXF", "TMF")}
    both = [p for p in ("MXF", "TMF") if nm[p] > 0]
    if len(both) == 2:
        cur["product"], cur["mix"] = "MIX", nm
    else:
        cur["product"] = both[0]
        cur.pop("mix", None)
    for v in add.values():
        if v.get("lo") is not None:
            cur["fill_lo"] = min(float(cur.get("fill_lo", v["lo"])), v["lo"])
            cur["fill_hi"] = max(float(cur.get("fill_hi", v["hi"])), v["hi"])
    cur.update(qty=tot, entry_fill=ef2, entry=ef2, late_leg=True, late_fill=True,
               entry_qty=int(cur.get("entry_qty") or q0) + addq)
    word = "、".join("%s %d 口等值（晚 %g 秒）" % ({"MXF": "小台", "TMF": "微台"}[p], v["up"], v.get("age") or 0)
                    for p, v in add.items())
    cur["fill_warn"] = "進場時%s回報慢，委託單明細確認是這一趟成交的 ⇒ 已併進部位；⚠️ 晚到那幾口沒有停利單" % word
    _log("entry_late_leg", {"ok": True, "add": {p: v["up"] for p, v in add.items()}, "qty": tot, "entry": ef2,
                            "why": cur["fill_warn"]})


def _late_prefetch_meta(meta, pos):
    """
    重啟撿回時：落地檔記著這一趟送出去的兩腿（mix_sent_ids）、券商上有一腿比落地時多 ⇒ 用委託單編號跟券商要那兩張進場單的明細。
    ⇒ {合約: 明細}；用不到／問不到 ⇒ None。⛔ 鎖外面叫。
    ⚠️ 要 api.list_trades()（永豐 SDK 有；模擬帳戶要實跑確認一次）。
    """
    try:
        if not (isinstance(meta, dict) and isinstance(pos, dict) and isinstance(meta.get("mix_sent_ids"), dict)):
            return None
        if not is_live() or _state["api"] is None:
            return None
        if time.time() - float(meta.get("mix_sent_at")) > LATE_LEG_S:
            return None
        mm = meta.get("mix") if isinstance(meta.get("mix"), dict) else {meta.get("product") or "TMF": meta.get("qty")}
        gm = _mix_of(pos)
        if not any(gm.get(p, 0) > int(mm.get(p) or 0) for p in RATIO):
            return None
        _update_status()
        ids = {str(v): p for p, v in meta["mix_sent_ids"].items() if v is not None}
        out = {}
        for t in _state["api"].list_trades() or []:
            p = ids.get(str(getattr(getattr(t, "order", None), "id", None)))
            if p is not None:
                out[p] = _deal_sums(t)
        return out or None
    except Exception:                       # noqa: BLE001  問不到 ⇒ 不認（走原本的 mismatch）
        return None


def _meta_late(got, meta, lp):
    """
    重啟撿回：落地時還沒看到的那一腿，現在券商上有了 ⇒ 證據對得上（同 _late_leg 的 ①～④，明細用委託單編號問回來的 lp；
    另外沒變的那一腿口數要一樣、券商均價要等於「落地時均價＋晚到那一腿成交價」加權 ±META_FILL_TOL）⇒ 回一份「併好之後」的落地狀態
    讓 _meta_apply 照原本的規矩比；對不上 ⇒ None（照原本 mismatch）。
    """
    ms = meta.get("mix_sent")
    if not isinstance(ms, dict) or not isinstance(lp, dict):
        return None
    if meta.get("touched") or meta.get("handover") or meta.get("dir") != got.get("dir"):
        return None
    try:
        if time.time() - float(meta.get("mix_sent_at")) > LATE_LEG_S:
            return None
        mq, mpx = int(meta.get("qty")), float(meta.get("bpx"))
        ef = float(meta.get("entry_fill"))
        gpx = float(got.get("entry"))
    except (TypeError, ValueError):
        return None
    mm = meta.get("mix") if isinstance(meta.get("mix"), dict) else {meta.get("product") or "TMF": mq}
    mm = {p: int(mm.get(p) or 0) for p in ("MXF", "TMF")}
    gm = _mix_of(got)
    add = {}
    for p in ("MXF", "TMF"):
        g = int(gm.get(p, 0))
        if g > mm[p]:
            s = lp.get(p)
            if g > int(ms.get(p) or 0) or s is None or s[1] <= 0 or s[1] + 1e-9 < g:
                return None
            if g - mm[p] > s[1] - int(_leg_have(meta).get(p) or 0) + 1e-9:
                return None                       # 明細裡已經算進部位的那幾口不能再認一次（同 _late_leg）
            add[p] = {"up": g - mm[p], "px": round(s[0] / s[1], 2), "lo": s[2], "hi": s[3]}
        elif g != mm[p]:
            return None                       # 另一腿也變了 ⇒ 不在這裡猜
    if not add:
        return None
    addq = sum(v["up"] for v in add.values())
    tot = mq + addq
    want_px = (mpx * mq + sum(v["px"] * v["up"] for v in add.values())) / tot
    if abs(want_px - gpx) > META_FILL_TOL:
        return None
    m = dict(meta)
    m["qty"], m["bpx"] = tot, gpx
    m["entry_fill"] = round((ef * mq + sum(v["px"] * v["up"] for v in add.values())) / tot, 2)
    m.setdefault("entry0", ef)
    m["entry_qty"] = int(meta.get("entry_qty") or mq) + addq
    nm = {p: mm[p] + int(add.get(p, {}).get("up", 0)) for p in ("MXF", "TMF")}
    both = [p for p in nm if nm[p] > 0]
    m["product"] = "MIX" if len(both) == 2 else both[0]
    if len(both) == 2:
        m["mix"] = nm
    else:
        m.pop("mix", None)
    m0 = meta.get("mix0") if isinstance(meta.get("mix0"), dict) else mm
    m["mix0"] = {p: int(m0.get(p, 0)) + int(add.get(p, {}).get("up", 0)) for p in ("MXF", "TMF")}
    for v in add.values():
        if v.get("lo") is not None:
            m["fill_lo"] = min(float(m.get("fill_lo", v["lo"])), v["lo"])
            m["fill_hi"] = max(float(m.get("fill_hi", v["hi"])), v["hi"])
    m["late_leg"] = m["late_fill"] = True
    m["fill_warn"] = "重啟時發現進場那一腿回報慢（%s），委託單明細確認是這一趟成交的 ⇒ 已併進部位；⚠️ 那幾口沒有停利單" % (
        "、".join("%s %d 口等值" % ({"MXF": "小台", "TMF": "微台"}[p], v["up"]) for p, v in add.items()))
    return m


def _touch_step(cur, pos, oq):
    """
    ⭐ 2026-11-06（PM）「他加碼過」要持續證據。⛔ 呼叫端拿著 _lock；⛔ 這裡**不改**本機口數（呼叫端照樣更新成券商的值，
    跟 main 一樣：第二輪品管 QA2-10 —— 懷疑期間本機口數停在舊值，平倉起點跟 main 不一樣 ⇒ 明細晚到時平到他那口）。
    券商口數比我們該有的多（_ours_left）⇒ 先記懷疑（touch_susp）；相隔 ≥ CLOSE_COOLDOWN 還是比我們多、
    不在我們剛送平倉單的那段時間（_touch_quiet，跟部位綁）、我們剩的口數沒變（中間平過倉 ⇒ 重新起算）⇒ 才標 touched。
    ⚠️ 懷疑期間停損／收盤照樣只平「我們剩下的口數」（_close_multi／_close_mix 都扣 _ours_left）⇒ 不會平到他的。
    """
    now = time.time()
    sp = cur.get("touch_susp")
    if _touch_quiet(now, cur):
        cur.pop("touch_susp", None)
        return
    ours = _ours_left(cur)
    if not isinstance(sp, dict) or int(sp.get("ours", -1)) != int(ours):
        cur["touch_susp"] = {"since": now, "qty": int(pos["qty"]), "ours": int(ours)}
        _log("position_touch_suspect", {"ok": None, "was": oq, "now": pos["qty"], "ours": ours,
                                        "why": "券商口數比我們的多 —— 先不判他加碼（可能是舊資料），"
                                               "%g 秒後還是這樣才判" % CLOSE_COOLDOWN})
        return
    if now - float(sp.get("since") or now) < CLOSE_COOLDOWN:
        return
    cur.pop("touch_susp", None)
    cur["touched"] = True
    _log("position_touched", {"ok": False, "was": oq, "now": pos["qty"],
                              "why": "券商口數比我們開的多（他自己加碼？）—— 收盤不幫他平，"
                                     "停損只平我們剩下的 %d 口" % ours})


def _mix_mismatch(cur, pos):
    """
    ⭐ 2026-11-06 混搭：本機部位 cur 跟券商部位 pos 的合約對不對得上 ⇒ 對不上的那一句話；對得上 ⇒ None。
      ・同一種單一合約 ⇒ 對得上（11-06 以前就是這樣）。
      ・本機混搭、券商也混搭 ⇒ 兩腿不可以「一腿變多、一腿變少」（那是他動過，分不出哪幾口是誰的）。
      ・本機混搭、券商只剩其中一種、**同方向**、而且那一腿沒有變多 ⇒ 對得上：另一腿不見了（停利成交或他平掉），
        交給 _reconcile_locked 的「口數變少」那段記帳、部位降成單一合約。
      ・本機單一、券商混搭；本機微台、券商小台（或反過來）⇒ 對不上（⛔ 不猜）。
    """
    cp, bp = cur.get("product") or "TMF", pos.get("product") or "TMF"
    if cp == bp and cp != "MIX":
        return None
    if pos.get("dir") == cur.get("dir") and not cur.get("handover") and _leg_caps(cur) is not None:
        # ⭐ 2026-11-06 第二輪品管（PM 拍板）：我們每一腿最多有幾口分得出來（_leg_caps：進場量－停利成交－自己平掉的）
        #    ⇒ 一腿變多一腿變少、只剩一腿但比本機多、本機單一但券商多了另一種 ⇒ ⛔ 不再 unknown：
        #    照常對帳（口數照券商、多出來的先記懷疑），平倉時 _close_mix 每一腿只平我們的、剩的轉成他的手動部位。
        #    真的分不出（每一腿上限合起來比我們剩的還多）⇒ 由 _close_mix 擋下來、示警。
        return None
    if cp == "MIX":
        cm = cur.get("mix") if isinstance(cur.get("mix"), dict) else {}
        if bp == "MIX":
            bm = pos.get("mix") or {}
            up = [k for k in RATIO if int(bm.get(k) or 0) > int(cm.get(k) or 0)]
            down = [k for k in RATIO if int(bm.get(k) or 0) < int(cm.get(k) or 0)]
            if up and down:
                return ("混搭部位一腿變多、一腿變少（本機 小台%s／微台%s，券商 小台%s／微台%s）—— 分不出來，不敢動，請自己確認"
                        % (cm.get("MXF"), cm.get("TMF"), bm.get("MXF"), bm.get("TMF")))
            return None
        if pos.get("dir") != cur.get("dir"):
            return "本機是混搭%s、券商只剩%s而且方向是%s —— 分不出來，不敢動，請自己確認" % (
                cur.get("dir"), bp, pos.get("dir"))
        if int(pos.get("qty") or 0) > int(cm.get(bp) or 0):
            return "本機是混搭（%s 那一腿 %s 口等值）、券商只剩%s %s 口等值 —— 分不出來，不敢動，請自己確認" % (
                bp, cm.get(bp), bp, pos.get("qty"))
        return None
    return ("本機部位是%s、券商上的是%s —— 分不出來，不敢動，請自己確認" % (cp, bp))


def _reconcile_locked(pos, _cur0, pre, meta_pre, late_pre=None):
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
            _lp = (_state["position"] or {}).get("late_pending")
            if _lp and is_live() and time.time() - float(_state["position"].get("late_pending_at") or 0) <= LATE_LEG_S:
                # ⭐ 2026-11-06 品管 M7：晚到那一腿（進場單明細說成交了）券商還沒回報 ⇒ 券商「空手」是假的，⛔ 不清、不記帳，等它回報
                return _state["position"]
            if is_live() or (_state["position"] or {}).get("recovered"):
                gone = _state["position"]
                _state["position"] = None
                if _lp:
                    _log("late_pending_expired", {"ok": False, "pending": _lp,
                                                  "why": "晚到那一腿等了 %g 秒券商都沒回報 —— 照券商空手處理，請到大戶投確認" % LATE_LEG_S})
                if gone is not None:
                    # 【部位是自己不見的】面板沒送過平倉，所以幾乎一定是**停利單在券商成交了**
                    # ——那是好事，但如果不在這裡記一筆，這趟來回就完全不會出現在成績單上
                    # （面板永遠不送停利單，close() 也就不會被呼叫）。
                    # 也可能是他自己用別的工具平掉的，所以成交價一律去問那張停利單，
                    # 問不到就留白、理由記 closed_elsewhere（畫面上寫「別處平的」），不猜。
                    if _CLOSE_INFLIGHT["on"]:
                        # ⭐ 2026-10-02：是**我們自己**剛送出的平倉單成交了 ⇒ 那一筆由平倉迴圈記（有成交價），
                        #    ⛔ 這裡再記就是同一口兩筆（見 _CLOSE_INFLIGHT 的說明）。
                        _log("position_gone", {"ok": True, "exit": None, "skip_record": True,
                                               "why": "平倉單等成交中，券商已空手 —— 交給平倉那段記帳"})
                    else:
                        # ⭐ 多口：停利單先前已經部分成交過（tgt_used）⇒ 只算剩下那幾口的價；一口的路照舊 _fill_price
                        px = _target_rest_px(gone, pre["tsums"] if gone is _cur0 else None)
                        if gone.get("touched"):
                            # 他加碼過 ⇒ 成績單只記**我們的**口數（⛔ 不把他那幾口算進來；價錢對不上就留白）
                            record_trade(gone, None, "closed_elsewhere", qty=_ours_left(gone))
                        else:
                            record_trade(gone, px, "tp" if px is not None else "closed_elsewhere")
                        _log("position_gone", {"ok": True, "exit": px,
                                               "why": "券商那邊已經沒有部位（停利成交或他自己平掉）"})
        elif _state["position"] is None:
            # 重啟後撿回部位：只知道方向與均價，停利單的下落要另外查
            got = {"dir": pos["dir"], "entry": pos["entry"],
                   "qty": pos["qty"], "entry_time": None,
                   "target_trade": None, "recovered": True,
                   "product": pos.get("product") or "TMF"}
            if isinstance(pos.get("mix"), dict):
                # ⭐ 2026-11-06 券商上是同方向混搭 ⇒ 撿回成混搭部位（兩腿各幾口；平倉分腿平）
                got["mix"] = dict(pos["mix"])
                got["mix0"] = dict(pos["mix"])
            # ⭐ 第三輪品管 #3：撿回來（沒有進場明細）的部位要持續確認才准平小台／混搭（_close_mix 看 rec_ok）
            got["rec_since"], got["rec_sig"] = time.time(), [pos["qty"], got["product"], got.get("mix")]
            _recover_meta(got)
            if meta_pre is not None:
                # ⭐ QA r2 必修 4：上次落地的多口狀態對得上 ⇒ 補回（他動過、出去過的段、後補成交、交接）
                # ⭐ QA r3：證據要全部對得上（口數、券商均價、成交價區間）；有一項對不上 ⇒ 標「分不出」、不平、示警
                _ma = _meta_apply(got, meta_pre, late_pre)
                if _ma == "shrink":
                    _log("position_meta_shrink", {"ok": None, "qty": got.get("qty"), "was_qty": meta_pre.get("qty"),
                                                  "why": "停機期間口數變少 ⇒ 等工作執行緒跟已實現損益對帳"})
                elif _ma == "ok":
                    _log("position_meta_restored", {"ok": True, "touched": bool(got.get("touched")),
                                                    "handover": bool(got.get("handover")),
                                                    "ours_left": None if got.get("handover") else _ours_left(got),
                                                    "why": "重啟後撿回多口部位，從落地的狀態補回"})
                elif _ma == "mismatch":
                    _mark_unknown(got, "重啟後撿回的部位（%s 口、均價 %s）跟面板關掉前記下的（%s 口、均價 %s）對不上 —— "
                                       "停機期間有成交或你動過，分不出哪幾口是誰的：停損、收盤、04:58 都不會幫你平，"
                                       "請自己到大戶投處理" % (got.get("qty"), got.get("entry"),
                                                            meta_pre.get("qty"), meta_pre.get("bpx")), "meta")
                    _log("position_meta_mismatch", {"ok": False, "now_qty": got.get("qty"), "now_px": got.get("entry"),
                                                    "was_qty": meta_pre.get("qty"), "was_px": meta_pre.get("bpx"),
                                                    "why": "落地的多口狀態對不上 ⇒ 標分不出、不平"})
            _state["position"] = got
        else:
            # 【券商永遠是真相】本機那份跟券商不一樣時要以券商為準。
            # 舊版只在本機是空的時候才採用券商的資料，本機一旦記錯方向就**永遠改不回來** ——
            # 2026-09-01 就是這樣：本機記成做多、券商是空單，reconcile() 照樣回傳做多，
            # 平倉因此送出同方向的單。這一段就是在補那個洞（測試 FAIL 抓出來的）。
            cur = _state["position"]
            if cur.get("recovered") and not cur.get("rec_ok"):
                _sig = [pos["qty"], pos.get("product") or "TMF", pos.get("mix")]
                if cur.get("rec_sig") == _sig and time.time() - float(cur.get("rec_since") or 0) >= CLOSE_COOLDOWN:
                    cur["rec_ok"] = True      # 相隔 ≥ CLOSE_COOLDOWN 兩次對帳都一樣 ⇒ 確認了
                elif cur.get("rec_sig") != _sig:
                    cur["rec_since"], cur["rec_sig"] = time.time(), _sig
            if cur.get("late_pending") and cur is _cur0:
                # ⭐ 2026-11-06 品管 M7：只剩晚到那一腿的部位 ⇒ 券商回報的還比它少（回報到一半）⇒ 先不動（⛔ 不記成別處平掉）；
                #    回報齊了 ⇒ 拿掉 late_pending、照常對帳。
                if pos["dir"] == cur.get("dir") and int(pos["qty"]) < int(cur.get("qty") or 0) and \
                        time.time() - float(cur.get("late_pending_at") or 0) <= LATE_LEG_S:
                    return _state["position"]
                cur.pop("late_pending", None)
                cur.pop("late_pending_at", None)
            if cur.get("dir") != pos["dir"]:
                _log("position_fixed", {"ok": True,
                                        "was": cur.get("dir"), "now": pos["dir"],
                                        "why": "本機方向跟券商不符，以券商為準"})
            # ⭐ 2026-10-14 多口：**我們自己開的多口部位**少了幾口、而且不是我們在平倉 ⇒ 記一段 leg
            #    （停利單部分成交／他在別處平掉幾口）。剩下的照樣看停損、收盤平剩下的。
            #    ⛔ 只管 entry_qty > 1 的部位：一口的部位與手動多口部位行為跟 10-14 以前一模一樣。
            oq = cur.get("qty")
            mine = _multi_pos(cur) and cur is _cur0
            tsums = pre["tsums"] if cur is _cur0 else None
            if (mine and _pq_ok(cur, oq) and 0 < pos["qty"] < oq
                    and cur.get("dir") == pos["dir"] and not _CLOSE_INFLIGHT["on"]):
                n = oq - pos["qty"]
                if not _tp_matches(cur, n, tsums):
                    # 他加碼過、這次少的又不是停利單吃的 ⇒ 先算成**他自己那幾口**（他在別處減碼），
                    # 超過他那幾口的部分才算我們的（⛔ 不然我們剩幾口會被低估、停損少平）。
                    # ⭐ 2026-11-06 第二輪品管：本機口數現在跟著券商（含懷疑中、靜默期、一次暫時的舊資料）⇒
                    #    「本機比我們該有的多」那幾口一律先算成他的（⛔ 舊資料退掉時被記成「我們的口數在別處平掉」⇒ 停損少平）。
                    n -= min(n, max(0, oq - _ours_left(cur)))
                if n > 0:
                    _partial_leg(cur, n, tsums)
                if _ours_left(cur) <= 0:
                    # 我們那幾口全部出去了，券商上剩的都是他的 ⇒ 這一趟收尾記帳、剩下的改成他的手動部位
                    record_trade(cur, None, "legs", qty=0)
                    _state["position"] = _his_manual(pos)
                    _log("position_handover", {"ok": True, "qty": pos["qty"],
                                               "why": "我們的口數都出場了；剩下 %d 口是他自己的 —— 改成手動部位" % pos["qty"]})
                    return _state["position"]
            if (mine and _pq_ok(cur, oq) and pos["qty"] > oq and cur.get("dir") == pos["dir"]
                    and not cur.get("touched") and pos["qty"] > _ours_left(cur)):
                # ⚠️ QA r4（G5）：只有**超過我們該有的口數**才要分「回報慢／他加碼」。重啟後從帳本補回的
                #    （帳本 fill_unsure ⇒ 我們有 3 口），券商從 1 口補到 3 口 ⇒ 本來就是我們的，⛔ 不標「他動過」。
                # ⭐ 2026-10-14 QA 必修 2／3：券商口數**變多**了。兩種可能，⛔ 用委託單自己的成交明細分：
                #   ① 進場那張單其實成交了更多口，只是部位回報慢一拍（明細 ＝ 現在口數、而且沒有出去過的段）
                #      ⇒ 是我們的：entry_qty 改成實際成交的口數（停利單只掛了先前那幾口 ⇒ 示警）。
                #   ② 其他（他自己在大戶投加碼）⇒ 標「他動過」（touched）：⛔ 不改進場價、收盤⛔ 不平、
                #      停損只平「我們剩下的口數」（見 _close_multi）。
                es = pre["esums"] if cur is _cur0 else None
                sent = cur.get("qty_sent") if _pq_ok(cur, cur.get("qty_sent")) else _own_qty(cur)
                if (es is not None and not cur.get("legs") and abs(es[1] - pos["qty"]) < 1e-9
                        and pos["qty"] <= sent):
                    cur["entry_qty"] = int(pos["qty"])
                    cur["late_fill"] = True
                    _log("entry_late_fill", {"ok": True, "was": oq, "now": pos["qty"],
                                             "why": "進場那張單其實成交了 %d 口（部位回報慢）—— 這幾口是我們的，"
                                                    "收盤照樣平；⚠️ 停利單只掛了 %d 口" % (pos["qty"], oq)})
                else:
                    _touch_step(cur, pos, oq)
            elif (mine and not cur.get("touched") and cur.get("dir") == pos["dir"] and cur.get("touch_susp")
                  and pos["qty"] > _ours_left(cur)):
                _touch_step(cur, pos, oq)            # 懷疑中、券商還是比我們該有的多 ⇒ 繼續計時
            elif cur.get("touch_susp"):
                cur.pop("touch_susp", None)          # 回到正常 ⇒ 剛才那次是舊資料，懷疑取消
            cur["dir"] = pos["dir"]
            cur["entry"] = pos["entry"]
            if _multi_pos(cur) or cur.get("handover"):
                # ⭐ QA r3：券商**現在**的均價另外記一份（落地時一起寫）⇒ 重啟後比對「停機期間有沒有變」
                cur["bpx"] = pos["entry"]
            # ⭐ 多口：部分出場之後券商的均價會變（剩下那幾口的均價）⇒ 停損價會跟著漂。
            #    我們自己開的多口部位 ⇒ 進場價維持成交那一刻的均價（entry_fill），停損與成績單才是同一個基準
            #    （⭐ 他加碼過〔touched〕也照樣不蓋 —— PM 10-14：「不蓋進場價」）。⛔ 一口的部位照舊用券商的均價。
            ef = cur.get("entry_fill")
            if mine and isinstance(ef, (int, float)) and not isinstance(ef, bool):
                cur["entry"] = float(ef)
            cur["qty"] = pos["qty"]
            # ⭐ 2026-11-06 混搭：兩腿各幾口照券商。券商只剩一種（另一腿不見了，上面已經照「口數變少」記好那一段）
            #    ⇒ 部位降成單一合約：product 改成剩下那種、刪掉 mix（mix0 留著：這一趟是混搭開的，停利單有兩張要撤）。
            if (pos.get("product") or "TMF") != (cur.get("product") or "TMF") and not isinstance(cur.get("mix0"), dict) \
                    and cur.get("product") in RATIO and _pq_ok(cur, cur.get("entry_qty")):
                # ⭐ 第二輪品管：合約要變之前先記下「我們進場的是哪一種、幾口」（_leg_caps 的基準；不然會拿新的合約別當進場量）
                cur["mix0"] = {cur["product"]: int(cur["entry_qty"])}
            if (pos.get("product") or "TMF") == "MIX":
                cur["product"] = "MIX"
                cur["mix"] = dict(pos.get("mix") or {})
            elif cur.get("product") == "MIX":
                _log("position_mix_leg_gone", {"ok": True, "now": pos.get("product"), "qty": pos["qty"],
                                               "was": cur.get("mix"),
                                               "why": "混搭部位剩一種合約 ⇒ 降成單一合約部位"})
                cur["product"] = pos.get("product") or "TMF"
                cur.pop("mix", None)
            elif (pos.get("product") or "TMF") != (cur.get("product") or "TMF"):
                # ⭐ 第二輪品管：本機單一合約、券商是另一種（_mix_mismatch 已經確認分得出我們的上限）⇒ 合約照券商
                cur["product"] = pos.get("product") or "TMF"
    return _state["position"]


# ---------------------------------------------------------------- 下單

def qty_ok(q):
    """
    一筆的口數（微台等值）合不合理：整數（⛔ 不收 bool／小數／字串）、1 ≤ q ≤ QTY_MAX。
    ⭐ 2026-11-06：小台開著 ⇒ 上限放寬到 QTY_MAX_EQ（＝ 10 口小台）；沒開 ⇒ 跟以前一樣 10。
    ⭐ 2026-11-06 晚 混搭：11～14 口這種改成「小台＋微台」兩張單（product_for ⇒ "MIX"）⇒ 開關開著就收到 QTY_MAX_EQ。
       （同一天下午那版「只收 5 的倍數」是不混搭時的規矩，已經被混搭取代。）開關沒開 ⇒ 一樣只到 10。
    """
    if not (isinstance(q, int) and not isinstance(q, bool) and q >= 1):
        return False
    if q <= QTY_MAX:
        return True
    return mini_on() and q <= QTY_MAX_EQ


def _own_qty(pos):
    """
    這一口部位裡「我們自己開的」有幾口（平倉每一張單最多送這麼多）。
    `entry_qty` 只有 enter() 寫、或撿回部位時從帳本補（多口才補）；沒有 ⇒ QTY（＝ 10-14 以前的行為：
    平倉每張 1 口、最多 CLOSE_TRIES 張）。
    """
    v = (pos or {}).get("entry_qty")
    return v if _pq_ok(pos, v) else QTY


def _cur_product():
    return (_state["position"] or {}).get("product") or "TMF"


def _order(action, price, price_type, order_type, octype, qty=None, product=None):
    import shioaji as sj
    q = QTY if qty is None else qty
    # ⭐ 2026-11-06：小台那一張另外收 _eq_ok（⛔ 不看開關）—— 手上 15 口等值的小台／混搭部位，開關被關掉之後
    #    也要平得掉（下面還有一道「這種合約 1～QTY_MAX 口」守著）。微台的判準一個字都沒變。
    if not (qty_ok(q) or ((product or _cur_product()) == "MXF" and _eq_ok(q))):
        # ⛔ 口數看不懂／超過上限 ⇒ 組不出單（呼叫端的 _send 不會被叫到）
        raise ValueError("口數不合理（%r），不送" % (q,))
    prod = product or _cur_product()
    if prod not in RATIO:
        # ⭐ 2026-11-06 混搭：一張單只能是一種合約。"MIX" 部位一定要由呼叫端（_enter_mix／_close_mix／place_target）
        #    指定是哪一腿 —— 走到這裡＝有一條路忘了分腿 ⇒ ⛔ 不送（不准默默當成微台送出 11 口）。
        raise ValueError("混搭部位沒有指定合約（%r），不送" % (prod,))
    r = RATIO.get(prod, 1)
    # ⭐ 2026-11-06 微台等值 ⇒ 這種合約的口數。小台部位一定是 5 的倍數；不是（例：撿回部位時的預設 1 口）
    #    ⇒ 無條件進位成 1 口小台（＝「每張平 1 口」的那條舊路），⛔ 不會送出 0 口。
    n = -(-q // r)
    if not (1 <= n <= QTY_MAX):
        raise ValueError("口數不合理（%r 口%s），不送" % (n, prod))
    kw = {"action": action, "price": price, "quantity": n,
          "price_type": price_type, "order_type": order_type, "octype": octype}
    if _state["account"] is not None:
        kw["account"] = _state["account"]
    return sj.FuturesOrder(**kw)


TOUCH_QUIET_S = 2 * CLOSE_COOLDOWN   # 送出平倉單之後幾秒內，券商口數變多不拿來判「他加碼」（部位回報慢的舊資料）
_LAST_CLOSE_SEND = {"at": 0.0}


def _touch_quiet(now=None, cur=None):
    """⭐ 2026-11-06：現在是不是「我們剛送過平倉單」的那段時間（正在平倉、或最後一張平倉單送出後 TOUCH_QUIET_S 秒內）。"""
    now = time.time() if now is None else now
    cur = _state["position"] if cur is None else cur
    at = float((cur or {}).get("close_sent_at") or 0.0) if isinstance(cur, dict) else 0.0
    return bool(_CLOSE_INFLIGHT["on"]) or now - at < TOUCH_QUIET_S


def _send(order, why, product=None):
    """真正送出去的唯一出口。dry run 時只記錄不送。⭐ 2026-11-06 `product`＝送到哪一種合約（沒給 ⇒ 部位的那種）。"""
    api = _state["api"]
    if str(why).startswith("close_"):
        _LAST_CLOSE_SEND["at"] = time.time()  # ⭐ 2026-11-06：記下最後一張平倉單的時刻（判「他加碼」要避開這段，見 _touch_quiet）
        if isinstance(_state["position"], dict):
            _state["position"]["close_sent_at"] = _LAST_CLOSE_SEND["at"]   # ⭐ 第二輪品管：跟部位綁（新部位不受上一筆影響）
    prod = product or _cur_product()
    body = {"action": str(order.action), "price": order.price, "qty": order.quantity,
            "price_type": str(order.price_type), "order_type": str(order.order_type),
            "octype": str(order.octype), "why": why, "product": prod}
    if not is_live():
        return True, None, _log(why, {**body, "ok": True, "dry_run": True})
    if api is None:
        return False, "尚未連線", _log(why, {**body, "ok": False, "err": "no api"})
    try:
        c = (_state["contracts"] or {}).get(prod) if prod != "TMF" else _state["contract"]
        if c is None:
            _log(why, {**body, "ok": False, "err": "沒有%s合約" % prod})
            return False, "沒有%s合約（連線時沒拿到）" % prod, None
        trade = api.place_order(c, order)
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
    t0 = time.time()
    pos = reconcile()
    if pos == "unknown":
        return False, "跟券商對帳失敗，這種時候不送單"
    if pos:
        return False, "已經有部位了，先平倉才能再進場"
    if float(_RECENT_CLOSE.get("hid_at") or 0.0) >= t0:
        # ⭐ 2026-11-06 第五輪品管：剛平完、券商還顯示舊的同方向部位（RECENT_CLOSE_S 內不撿回）⇒ ⛔ 不當成空手放行
        #    （這時進場，確認成交會被舊部位騙 ⇒ 停損送太多口平過頭；main 這裡也會擋）
        return False, "剛平倉、券商部位還沒更新，稍後再進"
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
    return _wait_fill_n(direction, 1)[0]


PARTIAL_STABLE = 2     # 多口：部分成交時，「部位口數＝委託單成交口數」要再連續看到幾次才定案（每次間隔 0.4 秒）


def _dealt_qty(trade):
    """委託單自己的成交口數（問券商一次 update_status）；問不到 ⇒ None。⛔ 不拿鎖。"""
    if trade is None or not is_live() or _state["api"] is None:
        return None
    _update_status()
    s = _deal_sums(trade)
    return None if s is None else s[1]


def _wait_fill_n(direction, want, trade=None, product=None):
    """
    等成交 ⇒ (成交均價, 成交口數, 一句警示或 None)；沒成交 ⇒ (None, 0, None)。
    （`_wait_fill` 的多口版；want＝1 時行為完全一樣：看到部位就回）
    ⭐ 2026-10-14 多口：
      ・看到方向對、口數 ≥ want ⇒ 馬上回 (均價, want)（多出來的不是我們的，只認 want）。
      ・口數 < want（部分成交）⇒ ⭐ QA 必修 2：**用委託單自己的成交明細交叉確認**（部位回報可能慢一拍，
        只看部位口數會把 3 口當 1 口 ⇒ 收盤認不出、留倉過夜）：
          部位口數 ＝ 委託單成交口數 ⇒ 再連續看到 PARTIAL_STABLE 次才定案；
          不一致／問不到明細 ⇒ 繼續等到期限；期限到仍不一致 ⇒ 以「券商部位口數」為準並回一句警示
          （之後部位口數變多時，reconcile 會再用明細認出「是我們的」，收盤照樣平）。
        ⛔ 不追單補。
    """
    if not is_live():
        return None, 0, None              # 演練沒有真的部位可以等
    deadline = time.time() + FILL_WAIT
    other = None
    got, same = None, 0
    asked = failed = 0
    while time.time() < deadline:
        # ⭐ 2026-11-06 品管 M3：單一合約進場只看**送出那種合約**那一腿（他手上另有別種合約時，不把合併均價當成我們的成交價）
        pos = _leg_position(product) if product else broker_position()
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
                q = int(pos.get("qty") or 0)
                if q >= want:
                    return float(pos["entry"]), want, None
                if q > 0:
                    dq = _dealt_qty(trade)
                    agree = dq is not None and abs(dq - q) < 1e-9
                    same = same + 1 if (agree and got is not None and got[1] == q and got[2]) else 0
                    got = (float(pos["entry"]), q, agree)
                    if agree and same >= PARTIAL_STABLE:
                        return got[0], q, None
            else:
                other = pos          # 有部位但方向不是我們送的 —— 這很不對勁
        time.sleep(0.4)
    if got is not None:
        # 到期限：部分成交但明細一直對不上（或問不到）⇒ 以券商部位口數為準，並講出來
        return got[0], got[1], ("送 %d 口，券商部位顯示 %d 口，但委託單的成交明細對不上 —— "
                                 "先照 %d 口記；之後口數變多會再認，請到大戶投確認" % (want, got[1], got[1]))
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
    return None, 0, None


def enter(direction, price, tp_points, sl_points=None, qty=None):
    """
    進場。用範圍市價（MKP）＋ IOC：要就立刻成交，不要掛在那裡等。
    **確認成交之後**才把停利限價單掛到券商那邊 —— 那一張電腦關機也有效。

    `sl_points`（2026-09-15 加）：**這一口自己的停損點數**，存進部位裡給面板的停損迴圈讀
    （`live_panel.check_real_position`）。⛔ 手動真單不帶 ⇒ 部位裡沒有這個欄位 ⇒
    面板照舊用 SL_POINTS。自動下單帶 ±0.5% 算出來的點數。

    ⭐⭐ `tp_points=None`（2026-09-16 加，PM 裁示「選項 A」）：**這一口不設停利** ——
       ⛔ 一次 `place_target()` 都不呼叫，部位裡也**不寫 `tp_points`**，改寫 `no_tp=True`。
       「開箱」那個候選的規則就是不設停利（停損＝箱子另一端，其餘到收盤平）。
       ⚠️ 為什麼不用「塞一個碰不到的哨兵價」：那在真錢上會變成**一張真的掛在場上的
          限價單**，而且 `_send()` 在演練模式直接回 ok=True ⇒ 演練永遠驗不出來。
       ⚠️ `no_tp=True` 是**必要的**，不可以只留「沒有 tp_points 這個欄位」——
          `live_panel._pos_points()` 在欄位不存在時會回 default（TP_POINTS 130），
          畫面就會顯示一個**根本不存在的停利 130 點**（2026-09-16 實跑確認過）。
       ⚠️⚠️ 券商端因此**一張單都沒有**（永豐又沒有停損單）⇒ 整口靠面板活著：
          面板死掉／斷線／電腦睡著 ⇒ 停損與收盤平倉都不會發生。這件事要寫在畫面上。
    """
    import shioaji as sj
    # ⭐ 2026-10-14 多口：`qty`＝要送幾口（自動下單由 lots.py 決定；沒給 ⇒ QTY ＝ 1 口，手動真單永遠走這條）。
    #    ⛔ 不合理（不是整數、<1、>QTY_MAX）⇒ 一張都不送。
    want = QTY if qty is None else qty
    if not qty_ok(want):
        _log("entry_bad_qty", {"ok": False, "qty": repr(want)[:20], "why": "口數不合理，不送"})
        return False, "口數不合理（%r）—— 一張都沒送" % (want,), None
    act = sj.Action.Buy if direction == "long" else sj.Action.Sell
    prod = product_for(want)              # ⭐ 2026-11-06 小台開著＋5 的倍數 ⇒ 整筆小台
    own_path = prod != "TMF" or (want > 1 and OWN_TMF_MULTI)
    if is_live() and _state["position"] is None and time.time() - float(_RECENT_CLOSE.get("at") or 0.0) < RECENT_CLOSE_S:
        # ⭐ 2026-11-06 第六輪品管 #3：enter() 自己也擋「剛平完、券商還顯示舊部位」的時間窗（跟 can_enter 同一個記號），
        #    ⛔ 不只靠呼叫端先走 can_enter（can_enter 到 enter 中間有空檔；也可能有人直接叫 enter）。一張都不送。
        _bp = broker_position()
        if _bp == "unknown":
            _log("entry_blocked", {"ok": False, "why": "剛平倉、跟券商對帳失敗 —— 不送"})
            return False, "剛平倉、跟券商對帳失敗，這種時候不送單（一張都沒送）", None
        if isinstance(_bp, dict):
            _RECENT_CLOSE["hid_at"] = time.time()
            _log("entry_blocked", {"ok": False, "broker": {"dir": _bp.get("dir"), "qty": _bp.get("qty")},
                                   "why": "剛平倉、券商部位還沒更新（還顯示 %s %s 口）—— 不送" % (_bp.get("dir"), _bp.get("qty"))})
            return False, "剛平倉、券商部位還沒更新，稍後再進（一張都沒送）", None
    # ⭐ 2026-11-06 第五輪品管 (b)：帳本部位進場前先記下券商上**同方向**已經有的口數（剛平完、回報慢的舊部位）。
    #    有 ⇒ 進場後券商部位「增加多少」分不出來 ⇒ ⛔ 不拿券商部位確認成交口數，改用進場單自己的明細（_wait_deals）。
    base = _entry_base(direction) if (own_path and is_live()) else None
    if base and "?" in base:
        # ⭐ 第七輪品管：進場前跟券商確認不到部位（重問過）⇒ ⛔ 不送單（不知道送出後券商部位增加多少，只能照明細／估計，太慢）
        _log("entry_blocked", {"ok": False, "why": "進場前跟券商確認不到部位（問了 %d 次）—— 不送" % ENTRY_BASE_TRIES})
        return False, "進場前跟券商確認不到部位（問了 %d 次都失敗）—— 一張都沒送，稍後再試" % ENTRY_BASE_TRIES, None
    # ⭐ 第三輪品管：要送新的進場單了 ⇒「剛平完、別撿回舊資料」的記憶作廢（不然這一筆晚到的成交會被當成上一筆的舊資料，沒有停損）
    #    ⭐ 第五輪品管：這一筆最後沒有部位（沒成交）⇒ 還原（券商上的舊部位照樣不撿回）
    _RECENT_CLOSE.pop("unsure", None)
    _loop_reset()                         # ⭐ 第七輪品管 #2：新的一趟 ⇒ 上一趟的平倉單不再當起算
    _rc_saved = dict(_RECENT_CLOSE)
    _RECENT_CLOSE["at"] = 0.0
    if prod == "MIX":
        # ⭐ 2026-11-06 混搭（例 11＝2 小台＋1 微台）⇒ 另一條路（兩張進場單、兩張停利單）。
        #    ⛔ 下面單一合約那條一個字都沒動。
        out = _enter_mix(direction, price, tp_points, sl_points, want, act, base=base)
        _rc_restore(out, _rc_saved)
        return out
    ok, err, res = _send(
        _order(act, 0, sj.FuturesPriceType.MKP, sj.OrderType.IOC, sj.FuturesOCType.New,
               qty=want, product=prod),
        "entry", product=prod)
    if not ok:
        _rc_restore((False, err, None), _rc_saved)
        return False, err, None

    est = False
    try:
        if base:
            fill, got, fill_warn, est = _wait_deals_single(res, want, base, price)
        else:
            fill, got, fill_warn = _wait_fill_n(direction, want, res if want > 1 else None, product=prod)
    except RuntimeError as e:
        # 方向不符：券商那邊有東西但不是我們送的方向。停手、不掛停利、
        # **不要清掉部位**（清掉畫面會顯示空手，但帳上其實有東西）。
        _log("entry_mismatch", {"ok": False, "err": str(e)})
        return False, str(e), None
    if is_live() and fill is None and (prod != "TMF" or (want > 1 and OWN_TMF_MULTI)):
        # ⭐ 第三輪品管規則 ①（帳本部位）：我們的口數以自家委託單明細為準 ⇒ 券商部位 FILL_WAIT 內沒出現，
        #    但進場單自己的明細說成交了（部位回報慢）⇒ 照明細當部位（⛔ 不當成沒成交、讓它變成沒人顧的撿回部位）
        _update_status()
        _s = _deal_sums(res)
        if _s is not None and _s[1] > 0:
            fill, got = round(_s[0] / _s[1], 2), int(round(_s[1]))
            fill_warn = ((fill_warn + "；") if fill_warn else "") + \
                "券商部位 %g 秒內還沒回報，照委託單明細記成交 %d 口等值" % (FILL_WAIT, got)
    if is_live() and fill is None:
        # IOC 沒成交。**絕對不可以掛停利** —— 那張 Cover 單留在場上，
        # 成交之後就變成一個反向的新部位。
        # （tp_points=None 那一口本來就不掛停利，這一段照樣要走 —— 沒成交就沒有部位。）
        _log("entry_nofill", {"ok": False, "why": "IOC 沒有成交，不掛停利"})
        with _lock:
            _state["position"] = None
        _rc_restore((False, None, None), _rc_saved)
        return False, "沒有成交（範圍市價 IOC 當下沒撮到），沒有掛停利，也沒有部位", None

    entry = fill if fill is not None else float(price)
    # ⭐ 實際成交幾口（演練 ⇒ 當成全部成交）。部分成交 ⇒ 接受 k 口，⛔ 不追單補到 want。
    k = got if (fill is not None and qty_ok(got)) else want
    if k < want:
        _log("entry_partial", {"ok": True, "want": want, "got": k,
                               "why": "送 %d 口只成交 %d 口 —— 接受 %d 口當部位，⛔ 不追單補" % (want, k, k)})
    rng = None
    if want > 1 and fill is not None:
        # 各口成交價的高低（認「是不是我們那幾口」時用：部分出場後剩下那幾口的均價一定落在這之間）。
        # ⛔ 問券商要在拿 _lock 之前（主迴圈的對帳也要那把鎖）。
        _update_status()
        s = _deal_sums(res)
        if s is not None and s[2] is not None:
            rng = {"fill_lo": s[2], "fill_hi": s[3]}
    with _lock:
        _state["position"] = {"dir": direction, "entry": entry, "qty": k, "product": prod,
                              "entry_time": datetime.now().strftime("%H:%M:%S"),
                              "target_trade": None, "recovered": False,
                              "ref_price": float(price),
                              # ⭐ 多口：我們自己開了幾口／送了幾口／成交那一刻的均價（部分出場後券商均價會變）
                              "entry_qty": k, "qty_sent": want, "entry_fill": entry,
                              "entry_day": str(date.today())}
        if rng:
            _state["position"].update(rng)
        if want > 1:
            # ⭐ 多口：留著進場那張單（之後部位口數變多時，reconcile 用它的成交明細分「回報慢」與「他加碼」）
            #    ⛔ snapshot()／畫面一律濾掉 *_trade（永豐物件序列化不了）
            _state["position"]["entry_trade"] = res
            _state["position"]["bpx"] = entry    # 券商那一刻的均價（重啟比對用，見 _meta_apply）
        if fill_warn:
            _state["position"]["fill_warn"] = fill_warn
            _log("entry_fill_unsure", {"ok": False, "want": want, "got": k, "why": fill_warn})
        if tp_points is None:
            # ⛔⛔ 這一口**不設停利**：不寫 tp_points（寫了就是一個假的停利價），
            #     改掛一面旗子讓面板看得出「不是忘了寫，是真的沒有」。
            _state["position"]["no_tp"] = True
        else:
            _state["position"]["tp_points"] = float(tp_points)
        if sl_points is not None:
            # ⛔ 只有帶了才存：沒有這個欄位 ＝「用手動那一套」，那是停損迴圈的判準
            _state["position"]["sl_points"] = float(sl_points)
        if prod != "TMF" or (want > 1 and OWN_TMF_MULTI):
            # ⭐ 第三輪品管：整筆小台（＋微台多口，OWN_TMF_MULTI）改用帳本 ⇒ 進場單記下來；⛔ 一口微台照舊
            _ps = _state["position"]
            _ps["own"] = True
            _ps["entry0"] = entry
            _ps["mix_sent_at"] = time.time()
            _own_add(_ps, "entry", prod, want, res, conf=k, px=entry, est=est)
    if want > 1 and is_live():
        _meta_sync()                    # ⭐ QA r2 必修 4：多口部位一成立就落地（重啟後補得回來）；一口的不寫
    if want > 1 and is_live():
        # ⭐ 多口：記下「進場那一刻券商已實現裡已經有的列」（之後對帳要排除）。
        #    QA r6：放在掛停利單**之前** ⇒ 停利單就算立刻成交，那一列也不會被誤記成「進場前就有」。一口的路不叫。
        _real_snapshot()
    if tp_points is None:
        # ⛔⛔ **一次 place_target 都不呼叫**（選項 A）。券商端這一口沒有任何掛單。
        return True, None, _state["position"]
    if est:
        # ⭐ 第六輪品管 #1：成交口數是保守估計（明細還沒回報）⇒ ⛔ 不掛停利（可能根本沒成交：掛了會變成反向新倉）
        # ⭐ 第七輪品管：明細回報之後由對帳補掛（_own_tp_fill）
        with _lock:
            if _state["position"] is not None:
                _state["position"]["tp_pending"] = float(tp_points)
        return True, ("已經進場，但成交明細還沒回報（口數先照送出量保守估計、有程式停損）—— **停利單先不掛**，"
                      "明細回報了會自動補掛"), _state["position"]
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


# ---------------------------------------------------------------- 混搭（⭐ 2026-11-06）

def _wait_fill_legs(direction, legs):
    """
    ⭐ 2026-11-06 混搭：兩張進場單**各自**等成交。legs＝{"MXF": (要幾口等值, 那張單), "TMF": (...)}
    ⇒ {合約: (那一腿的成交均價, 成交口數〔等值〕, 一句警示或 None)}；那一腿沒成交 ⇒ (None, 0, None)。
    每一腿照 _wait_fill_n 同一套規矩（⛔ 那一支一個字都沒動）：
      ・那一腿方向對、口數 ≥ 要的 ⇒ 馬上定案（多出來的不是我們的，只認要的）。
      ・部分成交 ⇒ 用那張單自己的成交明細交叉確認，連續 PARTIAL_STABLE 次對得上才定案；
        期限到仍對不上 ⇒ 照券商那一腿的口數、回一句警示。⛔ 不追單補。
    ⛔ 整段期間兩腿都沒看到成交時才可以丟例外（大半查詢失敗 ⇒「不知道有沒有成交」；看到反方向 ⇒「方向不符」）——
       有任何一腿成交了就**一定要回成交**（丟例外的話 enter 不會寫部位 ⇒ 那一腿沒有停損）；反方向那件事改成警示。
    """
    if not is_live():
        return {p: (None, 0, None) for p in legs}
    deadline = time.time() + FILL_WAIT
    done, got, same = {}, {}, {}
    other = None
    asked = failed = 0
    while time.time() < deadline and len(done) < len(legs):
        bl = _broker_legs()
        asked += 1
        if bl == "unknown":
            failed += 1                          # 【查不到 ≠ 沒成交】單獨算，最後另外處理
        else:
            for p, (want, trade) in legs.items():
                if p in done:
                    continue
                leg = bl.get(p)
                if not isinstance(leg, dict) or leg.get("entry") is None:
                    continue
                if leg.get("dir") != direction:
                    other = leg                  # 那一腿有部位但方向不是我們送的 —— 很不對勁
                    continue
                q = int(leg.get("qty") or 0)
                if q >= want:
                    done[p] = (float(leg["entry"]), want, None)
                    continue
                if q > 0:
                    dq = _dealt_qty(trade)
                    agree = dq is not None and abs(dq - q) < 1e-9
                    g = got.get(p)
                    same[p] = same.get(p, 0) + 1 if (agree and g is not None and g[1] == q and g[2]) else 0
                    got[p] = (float(leg["entry"]), q, agree)
                    if agree and same[p] >= PARTIAL_STABLE:
                        done[p] = (got[p][0], q, None)
        if len(done) < len(legs):
            time.sleep(0.4)
    out = {}
    for p, (want, _t) in legs.items():
        if p in done:
            out[p] = done[p]
        elif p in got:
            g = got[p]
            out[p] = (g[0], g[1], ("%s 送 %d 口等值，券商部位顯示 %d 口等值，但委託單的成交明細對不上 —— "
                                    "先照 %d 口等值記，請到大戶投確認" % (p, want, g[1], g[1])))
        else:
            out[p] = (None, 0, None)
    if not any(v[1] > 0 for v in out.values()):
        if failed and failed >= asked / 2:
            raise RuntimeError(
                "送出去了，但跟券商對帳一直失敗，**不知道有沒有成交** —— "
                "沒有掛停利，請立刻自己到大戶投確認部位（小台、微台兩種都要看）")
        if other is not None:
            raise RuntimeError(
                f"券商回報的{other.get('product')}部位方向是 {other['dir']}，跟送出的 {direction} 不符 —— "
                "已經停手，請自己到大戶投確認並處理")
    elif other is not None:
        p = other.get("product")
        w = "%s 那一腿券商顯示反方向（%s）—— 請立刻到大戶投確認" % (p, other.get("dir"))
        v = out.get(p)
        if v is not None and v[1] == 0:
            out[p] = (None, 0, w)
        else:
            for k, v in out.items():
                if v[1] > 0:
                    out[k] = (v[0], v[1], ((v[2] + "；") if v[2] else "") + w)
                    break
    return out


ENTRY_BASE_TRIES = 3   # ⭐ 第七輪品管：進場前查券商部位，問不到最多問幾次（第一次＋重問 2 次）


def _entry_base(direction):
    """
    ⭐ 第五輪品管 (b)：進場前券商上同方向已經有的口數 ⇒ {合約: 微台等值}（沒有 ⇒ {}；問不到 ⇒ {"?": 1}＝增加量不明）。
    ⭐ 第七輪品管：問不到 ⇒ 重問（最多 ENTRY_BASE_TRIES 次、間隔 0.4 秒）；問到「確定沒有」⇒ 呼叫端走正常的部位確認；
       還是問不到 ⇒ 呼叫端⛔ 不送單（保守估計那條明細不回報時要等 60 秒才交給撿回，太慢）。
    """
    legs = "unknown"
    for i in range(ENTRY_BASE_TRIES):
        if i:
            time.sleep(0.4)
        legs = _broker_legs()
        if legs != "unknown":
            break
    if legs == "unknown":
        return {"?": 1}
    return {p: int(l.get("qty") or 0) for p, l in (legs or {}).items()
            if l.get("dir") == direction and int(l.get("qty") or 0) > 0}


def _rc_restore(out, saved):
    """這一筆進場最後沒有部位 ⇒ 把「剛平完、別撿回舊資料」還原（⛔ 不然券商上的舊部位會被撿回成一筆新的）。"""
    unsure = _RECENT_CLOSE.pop("unsure", None)
    if not (isinstance(out, tuple) and out and out[0]) and _state["position"] is None and not unsure:
        _RECENT_CLOSE.update(saved)


def _wait_deals(trades):
    """
    ⭐ 第五輪品管 (b)：只看進場單自己的明細等成交（券商部位裡混著舊部位、增加量分不出來時用）。
    ⇒ ({合約: (均價或 None, 成交微台等值)}, 全部結束了嗎)。⛔ 不拿鎖。
    """
    deadline = time.time() + FILL_WAIT
    while True:
        _update_status()
        out, alldone = {}, True
        for p, (n, t) in trades.items():
            s = _deal_sums(t)
            dq = s[1] if s is not None else 0.0
            st = str(getattr(getattr(t, "status", None), "status", "") or "")
            done = ("Cancel" in st or "Fail" in st or ("Filled" in st and "PartFilled" not in st)
                    or dq >= n - 1e-9)
            alldone = alldone and done
            out[p] = (round(s[0] / dq, 2) if dq > 0 else None, int(round(dq)), done)
        if alldone or time.time() >= deadline:
            return out, alldone
        time.sleep(0.4)


def _wait_deals_single(res, want, base, price):
    """
    單一合約版：⇒ (均價, 口數, 警示, 是不是保守估計)；沒成交 ⇒ (None, 0, None, False)。
    ⭐ 第六輪品管 #1：明細 FILL_WAIT 內沒結束 ⇒ ⛔ 不丟成「不知道」（那會變撿回部位、沒有程式停損、成績單拆好幾列）：
       照「明細＋還沒結束的進場單照送出量」保守估計當帳本部位（est）；之後明細回報了照明細（_own_book），
       平倉送單照樣 min(券商那一腿, 帳本上限)。
    """
    out, _all = _wait_deals({"_": (want, res)})
    px, q, done = out["_"]
    why = "進場前券商上已經有同方向的部位（%s，可能是剛平完、回報慢的舊資料）⇒ 照進場單明細確認成交" % (
        "問不到" if "?" in base else "、".join("%s %d 口等值" % (p, v) for p, v in base.items()))
    _log("entry_base", {"ok": None, "base": base, "dealt": q, "done": done, "why": why})
    if done:
        return (px, q, None, False) if q > 0 else (None, 0, None, False)
    w = why + "；明細 %g 秒內還沒回報完 ⇒ 口數先照送出量 %d 口保守估計（有程式停損、停利單先不掛）" % (FILL_WAIT, want)
    return (px if q > 0 else float(price)), want, w, True


def _enter_mix(direction, price, tp_points, sl_points, want, act, base=None):
    """
    ⭐ 2026-11-06 混搭進場（enter() 判出 product "MIX" 才走這裡）：小台 want//5 口＋微台 want%5 口。
    ・先送小台那張、再送微台那張（都是 MKP＋IOC＋New），兩張都送出去之後才一起等成交（⛔ 不讓微台那張晚 5 秒才送）。
    ・接受實際成交量：某一腿 0 口 ⇒ 就是單一合約部位；兩腿都 0 口 ⇒ 沒部位（跟 IOC 沒成交同一套）。⛔ 不追單補量。
    ・小台那張送不出去（券商拒絕／沒有小台合約）⇒ 微台那張也⛔ 不送、整筆不進場（一張都還沒出去，最乾淨）。
      微台那張送不出去 ⇒ 小台已經出去了 ⇒ 照小台的成交當部位，回一句警示。
    ・送單紀錄：小台那張記 "entry"（每天進場次數靠它數）、微台那張記 "entry_mix_tmf"（⛔ 不然一筆會算成兩次進場）。
    部位欄位跟單一合約那條一樣，多 "mix"／"mix0"（兩腿各幾口等值）、"entry_trades"（兩張進場單）。
    """
    import shioaji as sj
    split = mix_split(want)
    ok, err, res_m = _send(
        _order(act, 0, sj.FuturesPriceType.MKP, sj.OrderType.IOC, sj.FuturesOCType.New,
               qty=split["MXF"], product="MXF"),
        "entry", product="MXF")
    if not ok:
        return False, err, None
    tok, terr, res_t = _send(
        _order(act, 0, sj.FuturesPriceType.MKP, sj.OrderType.IOC, sj.FuturesOCType.New,
               qty=split["TMF"], product="TMF"),
        "entry_mix_tmf", product="TMF")
    legs = {"MXF": (split["MXF"], res_m)}
    if tok:
        legs["TMF"] = (split["TMF"], res_t)
    est_legs = set()
    try:
        if base:
            # ⭐ 第五輪品管 (b)：進場前券商就有同方向部位 ⇒ 只看兩張進場單自己的明細（券商增加量分不出來）
            dl, alldone = _wait_deals({p: legs[p] for p in legs})
            fills = {p: (v[0], v[1], None) for p, v in dl.items()}
            _log("entry_base", {"ok": None, "base": base, "dealt": {p: v[1] for p, v in dl.items()}, "done": alldone,
                                "why": "進場前券商上已經有同方向的部位 ⇒ 照兩張進場單的明細確認成交"})
            for p, v in dl.items():
                if not v[2]:
                    # ⭐ 第六輪品管 #1：那一腿明細還沒結束 ⇒ 照送出量保守估計（帳本部位、有程式停損、停利先不掛）
                    est_legs.add(p)
                    fills[p] = (v[0] if v[1] > 0 else float(price), int(legs[p][0]),
                                "進場前券商上已有同方向部位、%s那一腿明細 %g 秒內沒回報完 ⇒ 先照送出量 %d 口等值保守估計"
                                "（停利單先不掛）" % ({"MXF": "小台", "TMF": "微台"}[p], FILL_WAIT, int(legs[p][0])))
        else:
            fills = _wait_fill_legs(direction, legs)
    except RuntimeError as e:
        _log("entry_mismatch", {"ok": False, "err": str(e)})
        return False, str(e), None
    warns = []
    if not tok:
        warns.append("微台那張（%d 口）送不出去（%s），只有小台那張" % (split["TMF"], terr or "券商拒絕"))
    if is_live():
        filled = {p: v for p, v in fills.items() if v[1] > 0}
        for p, v in fills.items():
            if v[2] and v[1] == 0:
                warns.append(v[2])
            elif v[1] == 0 and any(x[1] > 0 for x in fills.values()):
                # ⭐ 2026-11-06（PM：一腿晚回報不准變 unknown）：這一腿 5 秒內沒看到成交 —— 可能 IOC 沒撮到，也可能回報慢。
                #    寫進 fill_warn ⇒ auto_fire／night_fire 帳本標 fill_unsure（認人最多認到送出去的口數），
                #    晚到的話 reconcile 用那張進場單的成交明細認回來、併進部位（_late_leg）。
                warns.append("%s那一腿 %g 秒內沒看到成交（可能沒撮到、也可能回報慢）—— 晚到的話會用那張單的成交明細認回來"
                             % ({"MXF": "小台", "TMF": "微台"}[p], FILL_WAIT))
    else:
        # 演練：當成兩腿全部成交在參考價（跟單一合約那條一樣）
        filled = {p: (float(price), legs[p][0], None) for p in legs}
    if not filled and is_live():
        # ⭐ 第三輪品管規則 ①：券商部位沒出現，但進場單自己的明細說成交了（回報慢）⇒ 照明細
        _update_status()
        for p in legs:
            _s = _deal_sums(legs[p][1])
            if _s is not None and _s[1] > 0:
                filled[p] = (round(_s[0] / _s[1], 2), int(round(_s[1])),
                             "券商部位還沒回報，照委託單明細記 %s 成交 %d 口等值" % (p, int(round(_s[1]))))
    if not filled:
        _log("entry_nofill", {"ok": False, "product": "MIX", "why": "IOC 兩腿都沒有成交，不掛停利"})
        with _lock:
            _state["position"] = None
        return False, ("沒有成交（範圍市價 IOC 當下小台、微台都沒撮到），沒有掛停利，也沒有部位"
                       + ("；" + "；".join(warns) if warns else "")), None
    for p, v in filled.items():
        if v[2]:
            warns.append(v[2])
    k = sum(v[1] for v in filled.values())
    if k < want:
        _log("entry_partial", {"ok": True, "want": want, "got": k, "product": "MIX",
                               "legs": {p: v[1] for p, v in fills.items()},
                               "why": "送 %d 口等值只成交 %d 口等值 —— 接受實際成交當部位，⛔ 不追單補" % (want, k)})
    trades = {"MXF": res_m, "TMF": res_t if tok else None}
    rng = None
    if is_live():
        _update_status()                     # ⛔ 鎖外面問券商
        s = _sum_deals(trades[p] for p in filled)
        if s is not None and s[2] is not None:
            rng = {"fill_lo": s[2], "fill_hi": s[3]}
    if len(filled) == 1:
        prod = next(iter(filled))
        entry = filled[prod][0]
    else:
        prod = "MIX"
        entry = round(sum(v[0] * v[1] for v in filled.values()) / k, 2)
    with _lock:
        pos = {"dir": direction, "entry": entry, "qty": k, "product": prod,
               "entry_time": datetime.now().strftime("%H:%M:%S"),
               "target_trade": None, "recovered": False,
               "ref_price": float(price),
               "entry_qty": k, "qty_sent": want, "entry_fill": entry,
               "entry_day": str(date.today()), "bpx": entry}
        if rng:
            pos.update(rng)
        if prod == "MIX":
            pos["mix"] = {p: int(filled[p][1]) for p in ("MXF", "TMF")}
            pos["mix0"] = dict(pos["mix"])
        else:
            # 只成交一腿 ⇒ 就是一筆普通的單一合約部位（後面的停利、平倉、對帳全部走原本那條）
            pos["entry_trade"] = trades[prod]
        # ⭐ 2026-11-06 晚到的腿：記下「這一趟送出去的兩腿」（等值、時刻、委託單編號）＋兩張進場單（含沒看到成交的那張），
        #    之後券商多出來的那一腿要靠它們認：同方向、不超過送出量、那張進場單的成交明細說它成交了（_late_leg）。
        #    entry0＝帳本記的進場價（晚到的腿併進來之後 entry 會變，auto_fire／night_fire 認人改比這個）。
        pos["entry_trades"] = {p: trades[p] for p in legs}
        pos["mix_sent"] = {p: int(legs[p][0]) for p in legs}
        pos["mix_sent_at"] = time.time()
        pos["mix_sent_ids"] = {p: getattr(getattr(trades[p], "order", None), "id", None) for p in legs}
        pos["entry0"] = entry
        if warns:
            pos["fill_warn"] = "；".join(warns)
            _log("entry_fill_unsure", {"ok": False, "want": want, "got": k, "why": pos["fill_warn"]})
        if tp_points is None:
            pos["no_tp"] = True
        else:
            pos["tp_points"] = float(tp_points)
        if sl_points is not None:
            pos["sl_points"] = float(sl_points)
        # ⭐ 第三輪品管：帳本部位 —— 送出去的每一張進場單都記下來（含 0 成交的那張：晚到的明細照樣讀得到）
        pos["own"] = True
        pos["entry0"] = entry
        for p in legs:
            _own_add(pos, "entry", p, legs[p][0], trades[p],
                     conf=(filled[p][1] if p in filled else 0), px=(filled[p][0] if p in filled else None),
                     est=p in est_legs)
        _state["position"] = pos
    if is_live():
        _meta_sync()                         # 多口部位一成立就落地（重啟後補得回來）
        _real_snapshot()                     # 進場那一刻券商已實現裡已經有的列（之後對帳排除）；在掛停利之前
    if tp_points is None:
        return True, None, _state["position"]
    if est_legs:
        # ⭐ 第六輪品管 #1：有一腿口數是保守估計（明細還沒回報）⇒ ⛔ 不掛停利（可能根本沒成交：掛了會變成反向新倉）
        # ⭐ 第七輪品管：明細回報之後由對帳補掛（_own_tp_fill）
        with _lock:
            if _state["position"] is not None:
                _state["position"]["tp_pending"] = float(tp_points)
        return True, ("已經進場，但%s成交明細還沒回報（口數先照送出量保守估計、有程式停損）—— **停利單先不掛**，"
                      "明細回報了會自動補掛" % "、".join({"MXF": "小台", "TMF": "微台"}[p]
                                                  for p in sorted(est_legs))), _state["position"]
    sgn = 1 if direction == "long" else -1
    if prod == "MIX":
        # 停利一律用**實際成交價**算：每一腿用自己那一腿的成交均價 ± tp_points（兩腿每口都是 tp_points 點）
        tok2, terr2 = _place_target_mix({p: filled[p][0] + sgn * tp_points for p in filled})
    else:
        tok2, terr2 = place_target(entry + sgn * tp_points)
    if not tok2:
        return True, ("已經進場，但**停利單沒掛上去**（%s）—— "
                      "請立刻自己到大戶投補掛，或直接平倉" % (terr2 or "券商拒絕")), _state["position"]
    return True, None, _state["position"]


def _place_target_mix(prices):
    """
    ⭐ 2026-11-06 混搭停利：每一腿各掛一張 LMT＋ROD＋Cover（口數＝那一腿現在的等值口數），
    存在部位的 "target_trades"＝{"MXF": 單, "TMF": 單}（⛔ snapshot 會濾掉 *_trades，永豐物件序列化不了）。
    ⇒ (全部掛上了嗎, 掛不上的那幾腿的原因)。⛔ 掛不上不可以吞掉（呼叫端會講給他聽）。
    """
    import shioaji as sj
    pos = _state["position"]
    if not isinstance(pos, dict) or not isinstance(pos.get("mix"), dict):
        return False, "沒有混搭部位"
    act = sj.Action.Sell if pos["dir"] == "long" else sj.Action.Buy
    got, errs = {}, []
    for p in ("MXF", "TMF"):
        q = int(pos["mix"].get(p) or 0)
        if q <= 0 or p not in prices:
            continue
        try:
            o = _order(act, round(float(prices[p])), sj.FuturesPriceType.LMT,
                       sj.OrderType.ROD, sj.FuturesOCType.Cover, qty=q, product=p)
        except ValueError as e:
            errs.append("%s：%s" % (p, e))
            continue
        ok, err, res = _send(o, "target", product=p)
        if ok:
            got[p] = res
        else:
            errs.append("%s：%s" % (p, err or "券商拒絕"))
    with _lock:
        if _state["position"] is pos:
            tt = dict(pos.get("target_trades") or {})
            tt.update(got)
            pos["target_trades"] = tt
            if pos.get("own"):
                for p, res in got.items():
                    _own_add(pos, "target", p, int(pos["mix"].get(p) or 0), res)   # ⭐ 第三輪品管：帳本記停利單
    if pos.get("own") and is_live():
        _meta_sync()                    # ⭐ 第三輪品管：停利單也要落地（重啟後帳本才算得到停利明細）
    return (not errs), ("；".join(errs) if errs else None)


def place_target(tp_price, qty=None):
    """
    把停利掛成券商端的限價單。做多→賣出，做空→買回。
    ⭐ 口數 ＝ 這一口部位**實際成交**的口數（`entry_qty`，enter() 寫的；部分成交就是 k 口）。
    """
    import shioaji as sj
    pos = _state["position"]
    if pos is None:
        return False, "沒有部位"
    if pos.get("product") == "MIX":
        # ⭐ 2026-11-06 混搭（例：auto_fire 接手撿回來的混搭部位時補掛）⇒ 每一腿各一張、同一個價
        return _place_target_mix({"MXF": tp_price, "TMF": tp_price})
    act = sj.Action.Sell if pos["dir"] == "long" else sj.Action.Buy
    ok, err, res = _send(
        _order(act, round(float(tp_price)), sj.FuturesPriceType.LMT,
               sj.OrderType.ROD, sj.FuturesOCType.Cover,
               qty=(qty if qty is not None else _own_qty(pos))),
        "target")
    if ok:
        with _lock:
            _state["position"]["target_trade"] = res
            if _state["position"].get("own"):
                # ⭐ 第三輪品管：帳本記下停利單（停利明細算進「我們持有」）
                _own_add(_state["position"], "target", _cur_product(),
                         qty if qty is not None else _own_qty(_state["position"]), res)
        if _state["position"].get("own") and is_live():
            _meta_sync()                # ⭐ 第三輪品管：停利單也要落地（重啟後帳本才算得到停利明細）
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
        _CLOSE_INFLIGHT["on"] = False      # ⛔ 不管成功、失敗、例外，出了平倉就一定放掉
        _meta_sync()                       # 多口部位的落地狀態跟著更新（一口的部位什麼都不做）
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
        if time.time() - _RECENT_CLOSE["at"] < RECENT_CLOSE_S and _RECENT_CLOSE.get("msg"):
            # ⭐ 第三輪品管（模擬帳戶實測 B）：這一筆剛被平完（平倉單明細晚到、對帳時才結算）⇒ 回「已經平完」，
            #    ⛔ 不回「沒有部位」（停損迴圈會誤判成失敗）
            return True, _RECENT_CLOSE["msg"]
        return False, "沒有部位"
    if pos.get("dir") not in ("long", "short"):
        return False, "看不出部位方向，不敢送平倉單 —— 請自己到大戶投平倉"
    if pos.get("own"):
        # ⭐ 第三輪品管：帳本部位（混搭／整筆小台／微台多口）⇒ 以自家委託單明細決定送多少（_close_own）
        return _close_own(pos, reason)
    if pos.get("multi_unknown"):
        # ⭐ QA r2 必修 4：重啟後撿回來的部位對不上程式開的多口紀錄（落地的狀態也補不回來）⇒ 分不出哪幾口是誰的。
        #    ⛔ 不准退到一口的迴圈一口一口平（QA D3：[1,1,1] 把他的也平掉）⇒ 不平、示警。
        # ⭐ QA r4：拒平的紀錄與主控台**同一筆部位只記一次**（面板停損迴圈每次冷卻過後都會再來問；⛔ 不刷紀錄）。
        #    冷卻照設（⛔ 不然停損迴圈每 0.25 秒就來一次，每次都先問券商一次部位）。
        _close_fail["at"] = time.time()
        if pos.get("refused_said") is None:
            pos["refused_said"] = True
            _log("close_refused", {"ok": False, "qty": pos.get("qty"),
                                   "why": "撿回來的部位對不上多口紀錄，分不出是誰的，不平"})
            print("⚠️ [真實] 這筆部位分不出是誰的（重啟後對不上），停損不會執行 —— 請自己到大戶投處理", flush=True)
        return False, pos_note(pos)[0]
    if pos.get("product") in ("MIX", "MXF") or pos.get("mix0") or pos.get("target_trades") is not None \
            or isinstance(pos.get("mix_sent"), dict):
        # ⭐ 2026-11-06 品管 M4：整筆小台也走這裡（停利剛成交＋部位回報慢時，用停利明細扣掉再平，不平過頭）。
        # ⭐ 2026-11-06 混搭（或這一趟是混搭開的、已經剩一腿）⇒ 每一腿各自平、⛔ 不平過頭（_close_mix）。
        #    ⛔ 下面單一合約那幾條一個字都沒動。
        return _close_mix(pos, reason)
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
    # ⭐ 2026-10-14 多口：own ＝ 我們自己開了幾口（每張平倉單最多送這麼多）；left ＝ 券商上還剩幾口。
    #    ⛔ 一口（own＝1，含手動真單與撿回來對不上的部位）⇒ 每張 1 口、下面每一行都跟 10-14 以前一樣。
    own = _own_qty(pos)
    # 多口那條路：我們開了多口、或送出去是多口（只成交 1 口那種 ⇒ 他再加碼的也⛔ 不能被一口的迴圈一起平掉）
    multi = own > 1 or _multi_pos(pos)
    if multi and t is not None and is_live():
        # 撤停利單之前那一瞬間它可能又成交了幾口 ⇒ 撤完再問一次剩幾口（⛔ 不然平倉單會比剩下的多 ⇒ 反向開新倉）。
        # 剩 0 口 ⇒ reconcile 已經把那一趟記成 tp。問不到 ⇒ 照上一次的數字平（停損優先）。
        again = reconcile()
        if again is None:
            return False, "沒有部位（撤停利單之前剩下的已經在停利成交了）"
        if isinstance(again, dict):
            pos = again
    act = sj.Action.Sell if pos["dir"] == "long" else sj.Action.Buy
    if multi:
        # ⭐ 我們自己開的多口部位 ⇒ 另一條路（只平「我們剩下的口數」、問不到就看委託單明細、平過頭就停手）。
        #    ⛔ 下面那段一口的迴圈一個字都沒動（逐位元相同的保證，test_lots ② 在守）。
        return _close_multi(pos, reason, act, stray)
    last_err = None
    # ⭐ 2026-10-02：從這裡開始是「我們自己送的平倉單在路上」⇒ 對帳那邊看到空手不記帳（見 _CLOSE_INFLIGHT）。
    #    ⚠️ 刻意排在上面那次 reconcile() **之後**：送單之前就已經空手（停利早就成交）的那一筆，照舊由對帳記 tp。
    _CLOSE_INFLIGHT["on"] = True
    res = None
    for attempt in range(CLOSE_TRIES):
        if attempt > 0 and is_live():
            # ⭐ 2026-11-05（Benson 同意健檢建議）：送第二張以前先確認部位還在。
            #    實測 09-23、10-15 第一張「5 秒內沒看到部位消失」，第二張 1 秒內就平掉 ⇒ 券商回報會慢。
            #    第一張其實已經成交、只是回報慢的話，第二張就是反向新倉 ⇒ ⛔ 沒確認還在就不送。
            #    查不到 ⇒ 這一輪停手（不清部位、停損迴圈冷卻後會再從頭 close() 一次，那裡會重新跟券商確認方向）。
            #    先問**上一張平倉單自己**：成交明細有口數 ⇒ 它成交了，只是部位回報慢 ⇒ ⛔ 不送第二張。
            #    （券商部位這時還顯示舊的那口，只看部位會誤判「還在」。）
            ost = _close_order_state(res)
            # ⚠️ 只有「一口」才能這樣推：手動 2 口以上（沒有 entry_qty）也走這條迴圈、每張平 1 口，
            #    上一張成交只代表平掉 1 口 ⇒ 照舊看部位還在不在（test_lots 守）。
            one = _num_qty(pos.get("qty")) <= 1
            chk = "flat" if (ost == "filled" and one) else _still_open(pos["dir"])
            if chk == "flat":
                exit_px = _fill_price(res) if res is not None else None
                rec = record_trade(pos, exit_px, reason)
                with _lock:
                    _state["position"] = None
                _log("close_confirmed", {"ok": True, "tries": attempt, "late_report": True,
                                         "stray_target": stray, "exit": exit_px,
                                         "points": (rec or {}).get("points")})
                return True, ("平倉成功，但那張停利單沒撤掉，請自己到大戶投刪除"
                              if stray else None)
            if chk != "open":
                last_err = ("跟券商確認不到部位（%s）—— 先不送下一張平倉單（避免平過頭變反向），"
                            "稍後會再試；請到大戶投看一下" % ("方向反了" if chk == "flip" else "查詢失敗"))
                _log("close_unsure", {"ok": False, "try": attempt + 1, "chk": chk, "why": last_err})
                break
        guarded = is_live() and (_LOOP["seed"] or _num_qty(pos.get("qty")) > 1)
        if guarded:
            # ⭐ 2026-11-06 第七輪品管 #2：每送一口前先看自己之前送出的平倉單（_sent_dealt：還沒回報的當成已成交），
            #    券商部位比「起算時 − 已平」還多 ⇒ 券商還沒反映（舊資料）⇒ ⛔ 這一輪不送，冷卻後再來（寧可沒平完，不平過頭）。
            #    起算的單：這個部位之前送過的一口平倉單，或剛乾淨平完那一趟帳本部位的平倉／停利單（_LOOP 種子）。
            #    ⛔ 一口部位（而且沒有種子）照舊：上面那段「上一張成交了沒／部位還在不在」就是它的保護，結果不變。
            why = _loop_guard(pos)
            if why:
                last_err = why
                break
        ok, err, res = _send(
            _order(act, 0, sj.FuturesPriceType.MKP, sj.OrderType.IOC,
                   sj.FuturesOCType.Cover),
            "close_" + reason)
        last_err = err
        if not ok:
            break
        if guarded:
            _LOOP["trades"].append((res, float(QTY)))
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
                # 平掉了才去問成交價 —— 問不到就留白，不要拿現價冒充
                exit_px = _fill_price(res)
                rec = record_trade(pos, exit_px, reason)
                with _lock:
                    _state["position"] = None
                _log("close_confirmed", {"ok": True, "tries": attempt + 1,
                                         "stray_target": stray,
                                         "exit": exit_px,
                                         "points": (rec or {}).get("points")})
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


def _cancel_targets(pos):
    """
    ⭐ 2026-11-06 混搭：撤掉部位上**每一張**停利單 ⇒ 有沒有撤不掉的（True＝有殘單）。
    兩種放法都撤：target_trades（每一腿一張）＋單張的 target_trade（混搭開出來但只成交一腿／整筆小台，照 product 那一腿）。
    ⛔ 2026-11-06 品管退件 M1：舊版只撤 target_trades ⇒ 只成交一腿的部位停損後，那張停利 ROD 還掛著、之後成交就是反向新倉。
    已經全部成交的那張不撤（撤一張成交完的單一定報錯 ⇒ 會被誤講成「請自己去刪」）。
    撤之前把每一張的成交明細記進 tgt_fill（每一腿 [Σ價×口, Σ口]，微台等值）——平倉時拿它扣掉「停利剛成交、部位還沒回報」的量（M4）。
    撤掉的從部位上拿掉；撤不掉的留在 target_trades，下一輪平倉再撤一次。⛔ 撤不掉照樣要平倉（裸部位比殘單危險），但要講出來。
    ⛔ 不拿 _lock 問券商。
    """
    tts = dict(pos.get("target_trades") or {})
    if pos.get("target_trade") is not None:
        tts.setdefault(pos.get("product") if pos.get("product") in RATIO else "TMF", pos["target_trade"])
    if not tts or not is_live() or _state["api"] is None:
        return False
    _update_status()                       # 【一定要先 update_status】不然 cancel_order 回 400
    left, stray = {}, False
    done = dict(pos.get("target_done_trades") or {})
    tf = {p: list(v) for p, v in (pos.get("tgt_fill") or {}).items()}
    for p, t in tts.items():
        if t is None:
            continue
        s = _deal_sums(t)
        if s is not None and s[1] > float((tf.get(p) or [0.0, 0.0])[1]):
            tf[p] = [s[0], s[1]]
        try:
            oq = float(getattr(getattr(t, "order", None), "quantity", 0) or 0) * _ratio_of_trade(t)
        except (TypeError, ValueError):
            oq = 0.0
        done[p] = t
        if s is not None and oq > 0 and s[1] >= oq - 1e-9:
            _log("cancel_target", {"ok": True, "product": p, "skip": True, "why": "這一腿的停利單已經全部成交，不用撤"})
            continue
        try:
            _state["api"].cancel_order(t)
            _log("cancel_target", {"ok": True, "product": p, "why": "平倉前先撤掉還掛著的停利單"})
        except Exception as e:
            stray = True
            left[p] = t
            _log("cancel_target", {"ok": False, "product": p, "err": str(e)[:200],
                                   "why": "撤不掉，平倉後請自己到大戶投刪掉那張停利單"})
    with _lock:
        pos["target_trades"] = left
        pos["target_trade"] = None
        pos["tgt_fill"] = tf
        pos["target_done_trades"] = {k: v for k, v in done.items() if k not in left}
    return stray


def _close_pend(trades):
    """
    ⭐ 2026-11-06 混搭平倉：同一腿已經送出去的平倉單 ⇒ (確定成交了幾口等值, **保守估計**可能成交了幾口等值)。
    ⛔ 直接用 main 的 _sent_dealt（PM 定：兩條平倉路同一把尺）：明細滿了 ⇒ 送出量；IOC 已經結束
       （'OrderStatus.Filled'〔含部分成交，模擬帳戶實測〕／Cancelled／Failed）⇒ 明細量；其他（還沒回報、問不到）⇒ 當成全部成交。
    ⚠️ 「已取消＋明細 0」當成沒成交、同一輪可以補送（PM：急跌時每 15 秒才送一張太慢）。模擬帳戶實測狀態與明細是同一次
       update 一起回來的；萬一「狀態先到、明細晚到」，_close_mix 補送前還有一道：那一腿券商部位比「上一張送出前 − 明細」少 ⇒ 不補。
    ⛔ 呼叫端先 _update_status。
    """
    return _sent_dealt(trades)


def _leg_caps(pos):
    """
    ⭐ 2026-11-06 品管 M4／M9：每一腿「我們最多還可能有幾口」＝進場那一腿（mix0；單一合約＝entry_qty）
    − 那一腿停利單已經成交的（tgt_fill）− 之前幾輪我們自己平掉的（closed_by_leg）。
    券商部位回報慢（停利剛成交、上一輪剛平掉）時，只看券商會多送 ⇒ 平倉每一腿送單量再跟這個取小。
    認不出進場量（撿回來的手動部位、交接）⇒ None（只看券商）。
    """
    if pos.get("handover") or not _pq_ok(pos, pos.get("entry_qty")):
        return None
    base = pos.get("mix0") if isinstance(pos.get("mix0"), dict) else \
        {pos.get("product") if pos.get("product") in RATIO else "TMF": int(pos.get("entry_qty"))}
    tf = pos.get("tgt_fill") or {}
    cl = dict(pos.get("closed_by_leg") or {})
    for p, lst in (pos.get("close_hist_trades") or {}).items():
        # ⭐ 第二輪品管（QA2-7 seed 104）：之前幾輪送出的平倉單，現在明細回報了（或還沒回報 ⇒ 當成全部成交）
        #    ⇒ 上一輪結算時明細晚到、券商又慢，closed_by_leg 會少算 ⇒ 這一輪上限偏大、平到他的。跨輪一起算、取大的。
        cl[p] = max(int(cl.get(p) or 0), int(round(_close_pend(lst)[1])))
    return {p: int(base.get(p) or 0) - int(round(float((tf.get(p) or [0.0, 0.0])[1]))) - int(cl.get(p) or 0)
            for p in RATIO}


# ══ ⭐ 2026-11-06 第三輪品管：「我們的口數」以自家委託單明細為準（帳本部位，own）════════════════════════════
#    三輪品管下來的根因都是同一類：**拿可能是舊資料的券商部位去決定送多少單、算誰的口數**。統一規則：
#    ① 我們每一腿持有 ＝ 進場單明細 − 停利單明細 − 平倉單明細（＋確認過的調整）。
#       還沒結束的平倉單 ⇒ 當成全部成交（送單上限 hi 用這個，保守）；進場單只算已回報的明細（保守不加）。
#       送平倉單 ＝ min(券商那一腿, 我們持有的保守值 hi)。撤停利單之後一定先 _update_status 再讀一次停利明細。
#    ② 券商部位只當確認用：比我們多／少／反方向，都要「我們的單都結束了＋最後一次送單後 ≥ TOUCH_QUIET_S＋
#       相隔 ≥ CLOSE_COOLDOWN 兩次對帳都一樣」才當真；之前照我們的明細行事（⛔ 不撿回、⛔ 不記別處平掉）。
#    ③ 程式開的部位**永遠不轉成手動部位**（老闆不會手動加碼）：確認過券商比我們多 ⇒ 當成我們晚到的成交、照程式規則平、大聲示警；
#       確認過比我們少 ⇒ 記一段「別處平掉」（停利明細能解釋的本來就已經算進停利那一段）。
#    ④ 我們這一趟平完（明細說 0、單都結束）⇒ 立刻記成績單、清部位；之後 RECENT_CLOSE_S 秒內券商還顯示同方向、
#       不超過剛平掉的口數 ⇒ 是舊資料，⛔ 不撿回。
#    ⑤ 確認過券商是反方向 ⇒ 我們這一趟收尾（記成績單）、券商上的改成「反向殘單」（照手動單停損，收盤不碰）。
#    帳本部位：enter() 送出去是多口、或用到小台（整筆小台／混搭）的部位（pos["own"]）。⛔ 一口微台照舊走原本那條（逐位元相同）。
OWN_TMF_MULTI = True          # 微台多口也用帳本（⭐ 第三輪品管：取代 _close_multi；False ⇒ 只有小台／混搭用帳本）
LEGACY_HANDOVER = False       # ⛔ 預設關：True ⇒ 確認過券商比我們多時，照舊把多的轉成「他的手動部位」（老闆不會手動加碼 ⇒ 不用）
IOC_FINAL_S = 60.0            # 範圍市價 IOC 送出後幾秒還沒回報結束 ⇒ 照已回報的明細當成結束（IOC 不可能這麼久之後才成交）
DIFF_CONFIRM_N = 3            # 券商跟帳本的差異要至少幾次對帳都一樣（再加上相隔 ≥ CLOSE_COOLDOWN）才採信
RECENT_CLOSE_S = 120.0        # 帳本部位平完之後幾秒內，券商同方向、不超過剛平掉的口數 ⇒ 當成舊資料、不撿回
_RECENT_CLOSE = {"at": 0.0, "dir": None, "legs": {}, "msg": None}


def _own_add(pos, role, p, n, trade, conf=None, px=None, est=False):
    """
    帳本記一張我們自己的單（role＝entry／target／close；n＝送出的微台等值）。
    ⭐ 2026-11-06 第四輪品管 #1：進場單多記 conf＝enter() 用券商部位（或明細）確認的口數、px＝那時的成交均價。
       那一腿的進場口數 ＝ max(conf, 進場明細)；明細還沒追上 conf 之前，進場單不算結束（⛔ 不會因為明細晚到算成 0 口）。
    """
    r = {"role": role, "p": p, "n": int(n), "t": trade, "at": time.time(),
         "id": getattr(getattr(trade, "order", None), "id", None)}
    if role == "entry":
        r["conf"] = int(conf if conf is not None else 0)
        if px is not None:
            r["px"] = float(px)
        if est:
            # ⭐ 第六輪品管 #1：conf 是「照送出量的保守估計」（明細還沒回報）⇒ 單結束之後改照明細（_own_book）
            r["est"] = True
    pos.setdefault("own_trades", []).append(r)


def _own_rec(rec, now):
    """
    一張單 ⇒ (已回報成交量, Σ價×量, 結束了嗎)。⛔ 不問券商（呼叫端先 _update_status）。
    結束了嗎：False／True（券商回報結束、或明細滿了）／"to"（逾時當成結束：IOC_FINAL_S，或重啟後問不到那張單）。
    ⭐ 2026-11-06 第四輪品管：
       · 進場單：明細 < conf（確認口數）⇒ 不算結束（明細還在路上）；送出 IOC_FINAL_S 秒後才逾時當成結束。
       · 重啟後問不到的單（t 是 None、落地時沒結束）⇒ 照落地的送出時間＋IOC_FINAL_S 算到期（⛔ 不能永遠「沒結束」：
         平倉保守上限會永遠是 0、停損永遠送不出去）。停利單（限價、會一直掛著）不逾時。
    """
    t = rec.get("t")
    age = now - float(rec.get("at") or now)
    to = rec["role"] != "target" and age >= IOC_FINAL_S
    if t is None:
        dq, dpq = float(rec.get("q0") or 0.0), float(rec.get("pq0") or 0.0)
        fin = rec.get("fin0")
        if fin and fin != "to":
            done = True
        elif fin == "to" or to:
            done = "to"
        else:
            done = False
        return dq, dpq, done
    s = _deal_sums(t)
    dq, dpq = (s[1], s[0]) if s is not None else (0.0, 0.0)
    st = str(getattr(getattr(t, "status", None), "status", "") or "")
    done = ("Cancel" in st or "Fail" in st or ("Filled" in st and "PartFilled" not in st)
            or (s is not None and dq >= rec["n"] - 1e-9))
    if done and rec["role"] == "entry" and not rec.get("est") and dq < int(rec.get("conf") or 0) - 1e-9:
        done = False                  # 狀態說結束、明細還沒追上確認口數 ⇒ 明細還在路上
    if not done and to:
        done = "to"
    return dq, dpq, done


def _own_book(pos, now=None):
    """
    ⇒ {"MXF": {...}, "TMF": {...}, "_all_final": bool, "_last_send": 時刻, "_live_tgt": [rec]}；每一腿：
       e／epq 進場、t／tpq 停利、c／cpq 平倉（已回報）、cp 平倉（保守：沒結束的當成全部成交）、
       ours ＝ e − t − c ＋ 調整、hi ＝ e − t − cp ＋ 調整（送平倉單的上限）。
    """
    now = time.time() if now is None else now
    b = {p: {"e": 0.0, "epq": 0.0, "t": 0.0, "tpq": 0.0, "c": 0.0, "cpq": 0.0, "cp": 0.0, "adj": 0.0,
             "late": 0.0, "late_pq": 0.0, "gone": 0.0, "edq": 0.0, "unsure": 0.0}
         for p in RATIO}
    # _all_final：每一張進場／平倉單都結束了（差異確認用）；_exit_final：平倉單都結束了（結算用）
    # _grow：還有進場單沒結束、整腿 0 口（晚到的腿還可能進來）⇒ ⛔ 不結算（第四輪品管建議 5）
    # _clean：沒有一張是「逾時當成結束」、進場明細都追上確認口數 ⇒ 結算後才套用 RECENT_CLOSE_S 不撿回（第四輪品管 #2）
    allf, exitf, grow, clean, last, live_t = True, True, False, True, 0.0, []
    for rec in pos.get("own_trades") or []:
        dq, dpq, done = _own_rec(rec, now)
        x = b.setdefault(rec["p"], {"e": 0.0, "epq": 0.0, "t": 0.0, "tpq": 0.0, "c": 0.0, "cpq": 0.0,
                                    "cp": 0.0, "adj": 0.0, "late": 0.0, "late_pq": 0.0, "gone": 0.0, "edq": 0.0,
                                    "unsure": 0.0})
        if rec["role"] == "entry":
            conf = float(rec.get("conf") or 0)
            if rec.get("est") and done:
                conf = 0.0            # 保守估計那一張已經結束 ⇒ 照明細（⭐ 第六輪品管 #1）
            x["edq"] += dq
            if dq < conf - 1e-9:
                # ⭐ 第四輪品管 #1：明細還沒追上 enter() 確認的口數 ⇒ 照確認口數（缺的那幾口用當初的成交均價）
                px = float(rec.get("px") or (dpq / dq if dq > 0 else 0.0))
                x["e"] += conf
                x["epq"] += dpq + (conf - dq) * px
                clean = False
                if rec.get("est"):
                    x["unsure"] += conf - dq
            else:
                x["e"] += dq
                x["epq"] += dpq
            if not done and conf <= 0 and dq <= 0:
                # 整腿都還沒看到（混搭晚到的那一腿）⇒ 還可能整腿進來。已經看到部分成交的單不算（IOC 部分成交是一次回報的）
                grow = True
        elif rec["role"] == "target":
            x["t"] += dq
            x["tpq"] += dpq
            if not done:
                live_t.append(rec)
        else:
            x["c"] += dq
            x["cpq"] += dpq
            x["cp"] += dq if done else float(rec["n"])
            exitf = exitf and bool(done)
        if done == "to":
            clean = False
        if rec["role"] != "target":
            allf = allf and bool(done)
            last = max(last, float(rec.get("at") or 0.0))
    for a in pos.get("own_adj") or []:
        x = b[a["p"]]
        q = float(a["q"])
        if q > 0:
            # ⭐ 第四輪品管 #4：採信過的「晚到 +q」要跟之後補進來的進場明細互相抵銷（⛔ 不重複算）：
            #    採信那一刻那一腿的進場是 e_at，之後明細多出來的部分先抵掉這筆調整
            e_at = a.get("e_at")
            used = max(0.0, x["e"] - float(e_at)) if e_at is not None else 0.0
            eff = max(0.0, q - used)
            x["late"] += eff
            if a.get("pq") is not None and q > 0:
                x["late_pq"] += float(a["pq"]) * eff / q
            x["adj"] += eff
        else:
            x["gone"] += -q
            x["adj"] += q
    for p in RATIO:
        x = b[p]
        x["ours"] = x["e"] - x["t"] - x["c"] + x["adj"]
        # ⭐ 第六輪品管 #1：保守估計（進場明細還沒回報）的口數算進 ours（有程式停損、口數不少算），
        #    ⛔ 不算進送平倉單的上限 hi（進場前券商上有舊部位 ⇒ min(券商, 上限) 擋不住，會平過頭）
        x["hi"] = x["e"] - x["t"] - x["cp"] + x["adj"] - x["unsure"]
    b["_all_final"], b["_last_send"], b["_live_tgt"] = allf, last, live_t
    b["_exit_final"], b["_grow"], b["_clean"] = exitf, grow, clean
    return b


def recent_unclean(window=None):
    """
    ⭐ 2026-11-06 第六輪品管 #2：最近 window 秒（預設 RECENT_CLOSE_S）內有一趟帳本部位是「不乾淨」結算的
    （有單逾時／問不到才當成結束）⇒ 券商上之後冒出來的部位可能是那一趟晚到的腿。收盤認人遇到「沒有部位」先別定案。
    """
    w = RECENT_CLOSE_S if window is None else float(window)
    return bool(_RECENT_CLOSE.get("unclean")) and time.time() - float(_RECENT_CLOSE.get("at") or 0.0) < w


def _own_tp_fill(pos):
    """
    ⭐ 2026-11-06 第七輪品管：保守估計進場（停利單先沒掛）⇒ 進場單都結束、明細回報了 ⇒ 照明細的口數與均價補掛停利單。
    ⛔ 鎖外面叫（會送單）。明細說 0 口 ⇒ 不掛（對帳會清掉部位）。
    """
    now = time.time()
    recs = [r for r in pos.get("own_trades") or [] if r["role"] == "entry"]
    if not recs or any(r.get("est") and not _own_rec(r, now)[2] for r in recs):
        return
    if any(r["role"] == "close" for r in pos.get("own_trades") or []):
        with _lock:
            pos.pop("tp_pending", None)          # 已經開始平倉 ⇒ 不補掛
        return
    book = _own_book(pos, now)
    tpp = float(pos.get("tp_pending"))
    sgn = 1 if pos.get("dir") == "long" else -1
    px = {p: book[p]["epq"] / book[p]["e"] for p in RATIO if book[p]["e"] > 0}
    with _lock:
        if _state["position"] is not pos:
            return
        pos.pop("tp_pending", None)
    if not px:
        return
    if pos.get("product") == "MIX":
        ok, err = _place_target_mix({p: v + sgn * tpp for p, v in px.items()})
    else:
        ok, err = place_target(next(iter(px.values())) + sgn * tpp)
    pos["tp_points"] = tpp
    if ok:
        _log("target_filled_in", {"ok": True, "why": "進場明細回報了 ⇒ 補掛停利單（%g 點）" % tpp})
    else:
        _state["last_error"] = "進場明細回報了，但補掛停利單失敗（%s）—— 請自己到大戶投補掛，或等停損／收盤" % (err or "券商拒絕")
        _log("target_filled_in", {"ok": False, "err": err, "why": _state["last_error"]})


def own_shell(pos):
    """
    ⭐ 2026-11-06 第五輪品管 (c)：帳本部位的「空殼」：已知的口數都平完了（口數 0），但還有一腿進場單沒結束
    （晚到的腿可能還會進來 ⇒ broker 先不結算）。auto_fire 收盤認人遇到它要等，⛔ 不可以判成「不是我們的」。
    """
    return isinstance(pos, dict) and bool(pos.get("own")) and int(pos.get("qty") or 0) <= 0


def _own_done(book):
    """帳本說「這一趟可以結算了」嗎（呼叫端另外看口數 ≤ 0）。"""
    return book["_exit_final"] and not book["_grow"]


def _own_fields(cur, book):
    """帳本 ⇒ 部位上給人看／給停損用的欄位（口數、合約、兩腿、進場均價）。⛔ 呼叫端拿著 _lock。"""
    ours = {p: max(0, int(round(book[p]["ours"]))) for p in RATIO}
    cur["qty"] = sum(ours.values())
    has = [p for p in ("MXF", "TMF") if ours[p] > 0]
    if len(has) == 2:
        cur["product"], cur["mix"] = "MIX", {p: ours[p] for p in ("MXF", "TMF")}
    elif len(has) == 1:
        cur["product"] = has[0]
        cur.pop("mix", None)
    eq = sum(book[p]["e"] + book[p]["late"] for p in RATIO)
    epq = sum(book[p]["epq"] + book[p]["late_pq"] for p in RATIO)   # 第四輪品管 #4：晚到調整用抵銷後的量
    if eq > 0:
        ef = round(epq / eq, 2)
        cur["entry_fill"] = ef
        cur["entry"] = ef
        if cur.get("entry_qty0") is None:
            cur["entry_qty0"] = int(cur.get("entry_qty") or round(eq))
        cur["entry_qty"] = int(round(eq))
        if cur["entry_qty"] > int(cur["entry_qty0"]):
            # 進場單的明細比 enter() 定案時多（成交回報慢）⇒ 那幾口也是我們的；auto_fire／night_fire 收盤認人靠 late_fill＋entry_qty
            cur["late_fill"] = True
        if cur.get("entry0") is not None and abs(ef - float(cur["entry0"])) > 0.005:
            cur["late_leg"] = True    # 進場價跟帳本當初記的不一樣（晚到的腿）⇒ auto_fire／night_fire 認人改比 entry0
    m0 = {p: int(round(book[p]["e"] + book[p]["late"])) for p in RATIO}
    if all(m0.values()) or cur.get("product") == "MIX":
        cur["mix0"] = m0              # 成績單記 MIX（兩腿都進過場）


def _own_finish(cur, now):
    """
    這一趟結束（帳本說我們每一腿都 0、單都結束了）⇒ 記一筆成績單（整趟）、清部位、記下「剛平掉」（不撿回舊資料）。
    ⛔ 呼叫端拿著 _lock。成績單：停利那段、別處平掉那段當 legs，平倉那段當最後一段（出場價都照各自的明細）。
    """
    try:
        # 先刪落地檔再記成績單：當在中間 ⇒ 少一筆（看得出來），⛔ 不會重啟後再記一次（重複）
        (ORDER_DIR / META_NAME).unlink(missing_ok=True)
        _META.update(last=None, swept=True)
    except OSError:
        pass
    book = _own_book(cur, now)
    tq = sum(book[p]["t"] for p in RATIO)
    tpq = sum(book[p]["tpq"] for p in RATIO)
    cq = sum(book[p]["c"] for p in RATIO)
    cpq = sum(book[p]["cpq"] for p in RATIO)
    gone = sum(book[p]["gone"] for p in RATIO)
    legs = []
    if tq > 0:
        legs.append({"qty": int(round(tq)), "px": round(tpq / tq, 2), "src": "tp", "at": datetime.now().strftime("%H:%M:%S")})
    if gone > 0:
        legs.append({"qty": int(round(gone)), "px": None, "src": "elsewhere", "at": datetime.now().strftime("%H:%M:%S")})
    reason = cur.get("close_reason") or ("tp" if tq > 0 and gone <= 0 else "closed_elsewhere")
    rp = dict(cur, legs=legs)
    rp.pop("own_trades", None)
    if cur.get("restored") or cur.get("recovered"):
        rp["recovered"] = True               # 成績單「重啟後撿回」那一欄照舊（跟 main 一樣：這一趟中間重啟過）
    if cq <= 0 and gone <= 0 and tq <= 0:
        # ⭐ 第六輪品管 #1：保守估計的進場單結束後明細說 0 成交（根本沒進場）⇒ ⛔ 不記 0 口的假成績單
        rec = None
        _log("own_empty", {"ok": None, "why": "這一趟進場單結束後明細是 0 口（沒有進場）—— 不記成績單"})
    elif cq <= 0 and gone <= 0 and tq > 0:
        # 整趟都是停利單吃掉的 ⇒ 成績單寫成跟以前一樣的一列（不掛 legs；main 那條「部位不見 ⇒ 記 tp」同一個格式）
        rp["legs"] = []
        rec = record_trade(rp, round(tpq / tq, 2), "tp", qty=int(round(tq)))
    else:
        rec = record_trade(rp, round(cpq / cq, 2) if cq > 0 else None, reason, qty=int(round(cq)))
    # ⭐ 第四輪品管 #2：「RECENT_CLOSE_S 內券商同方向不撿回」只套用在帳本確定平完、明細都滿了（_clean）；
    #    有任何一張是逾時當成結束、或進場明細沒追上確認口數 ⇒ ⛔ 不藏（券商上還有的就是真部位，照撿回規則處理、有停損）
    _RECENT_CLOSE.update(at=now, dir=cur.get("dir"),
                         legs=({p: int(round(book[p]["e"] + book[p]["late"])) for p in RATIO}
                               if book["_clean"] else {}),
                         msg="這一筆已經平完了（%s）" % reason)
    _RECENT_CLOSE["unclean"] = not book["_clean"]
    if book["_clean"]:
        # ⭐ 第七輪品管 #2：這一趟乾淨平完 ⇒ 平倉／停利單當「起算」：RECENT_CLOSE_S 過後券商若還顯示剛好同樣的口數（舊資料）
        #    被撿回來，一口迴圈也 ⛔ 不會再平（看這些單已經平掉多少）
        _loop_seed(cur.get("dir"), sum(book[p]["e"] + book[p]["late"] for p in RATIO),
                   trades=[(r["t"], float(r["n"])) for r in cur.get("own_trades") or []
                           if r["role"] in ("close", "target") and r.get("t") is not None],
                   px=cur.get("bpx") or cur.get("entry"))
    if not book["_clean"]:
        _log("own_finish_unclean", {"ok": None, "why": "這一趟有委託單是逾時／問不到才當成結束 —— 平完後券商上若還有部位，照撿回處理（不藏）"})
    _state["position"] = None
    _log("own_finished", {"ok": True, "reason": reason, "qty": (rec or {}).get("qty"), "exit": (rec or {}).get("exit"),
                          "points": (rec or {}).get("points"), "tp": tq, "close": cq, "elsewhere": gone})
    return rec


def _own_signed_legs(legs, direction):
    """券商每一腿 ⇒ 帶正負號的微台等值（同方向＋、反方向－）。"""
    out = {p: 0 for p in RATIO}
    for p, l in (legs or {}).items():
        q = int(l.get("qty") or 0)
        out[p] = q if l.get("dir") == direction else -q
    return out


def _own_reconcile(cur):
    """帳本部位的對帳（⛔ 不准拿舊資料決定任何事；差異要持續才採信）。⇒ 部位／None（這一趟結束）／"unknown"。"""
    legs = _broker_legs()
    if legs == "unknown":
        return "unknown"
    _update_status()                           # ⛔ 鎖外面
    now = time.time()
    book = _own_book(cur, now)
    if not mini_on() and "MXF" in legs and book["MXF"]["e"] <= 0 and not any(
            r["p"] == "MXF" for r in cur.get("own_trades") or []):
        # ⭐ 第三輪品管 #6：開關關著、本機是微台、券商上有小台 ⇒ 跟 main 一樣 unknown＋示警（⛔ 不動）
        _state["last_error"] = "本機部位是微台、券商上有小台 —— 分不出來，不敢動，請自己確認"
        return "unknown"
    sb = _own_signed_legs(legs, cur.get("dir"))
    diff = {p: sb[p] - int(round(book[p]["ours"])) for p in RATIO}
    with _lock:
        if _state["position"] is not cur:
            return _state["position"]
        seen = cur.setdefault("own_seen", {})
        for p in RATIO:
            if sb[p] > int(seen.get(p) or 0):
                seen[p] = sb[p]
        _own_fields(cur, book)
        if cur["qty"] <= 0 and _own_done(book) and not any(v < 0 for v in sb.values()):
            # 帳本說我們每一腿都 0、單都結束 ⇒ 這一趟結束（券商若還顯示同方向的口數：舊資料或不是這一趟的 ⇒
            # RECENT_CLOSE_S 內不撿回；之後還在才當成另一筆撿回、有停損）
            _own_finish(cur, now)
            return None
        if not any(diff.values()):
            cur.pop("diff_susp", None)
            return cur
        sig = [diff["MXF"], diff["TMF"]]
        can = book["_all_final"] and not _CLOSE_INFLIGHT["on"] and now - book["_last_send"] >= TOUCH_QUIET_S
        for p in RATIO:
            if diff[p] < 0 and int(seen.get(p) or 0) < int(round(book[p]["e"])) \
                    and now - float(cur.get("mix_sent_at") or book["_last_send"] or now) < LATE_LEG_S:
                can = False           # 那一腿券商從來沒顯示到我們的進場量（晚到的腿）⇒ LATE_LEG_S 內不採信「比較少」
        sp = cur.get("diff_susp")
        if not can:
            cur["diff_susp"] = {"sig": sig, "since": None, "wait": True}
            return cur
        if not isinstance(sp, dict) or sp.get("sig") != sig or sp.get("since") is None:
            cur["diff_susp"] = {"sig": sig, "since": now, "n": 1}
            _log("own_diff_suspect", {"ok": None, "diff": diff, "why": "券商部位跟程式明細算的不一樣 —— 先照明細，%g 秒後還一樣才採信"
                                      % CLOSE_COOLDOWN})
            return cur
        sp["n"] = int(sp.get("n") or 1) + 1
        if now - float(sp["since"]) < CLOSE_COOLDOWN or sp["n"] < DIFF_CONFIRM_N:
            # 持續＝相隔 ≥ CLOSE_COOLDOWN、而且至少 DIFF_CONFIRM_N 次對帳都一樣（中間有一次不一樣就重算）
            # （性質測試抓到：兩次互不相干的暫時舊資料剛好隔 16 秒、中間的對帳又問不到 ⇒ 只看兩次會被湊成「持續」）
            return cur
        cur.pop("diff_susp", None)
        if any(v < 0 for v in sb.values()):
            return _own_residual(cur, legs, sb, now)
        for p in RATIO:
            d = diff[p]
            if d > 0:
                if LEGACY_HANDOVER:
                    continue
                px = float((legs.get(p) or {}).get("entry") or cur.get("entry") or 0)
                cur.setdefault("own_adj", []).append({"p": p, "q": d, "pq": px * d, "src": "late",
                                                       "e_at": book[p]["e"]})
                _state["last_error"] = ("券商上%s比程式明細多 %d 口等值（確認 %g 秒以上）⇒ 當成程式晚到的成交，照程式停損／收盤平掉 —— "
                                        "請到大戶投確認" % ({"MXF": "小台", "TMF": "微台"}[p], d, CLOSE_COOLDOWN))
                _log("own_adopt", {"ok": False, "p": p, "q": d, "px": px, "why": _state["last_error"]})
                print("⚠️ [真實] " + _state["last_error"], flush=True)
            elif d < 0:
                cur.setdefault("own_adj", []).append({"p": p, "q": d, "pq": None, "src": "elsewhere"})
                _log("own_gone", {"ok": False, "p": p, "q": -d,
                                  "why": "券商上%s比程式明細少 %d 口等值（確認 %g 秒以上、單都結束了）⇒ 記成別處平掉" %
                                         ({"MXF": "小台", "TMF": "微台"}[p], -d, CLOSE_COOLDOWN)})
        book = _own_book(cur, now)
        _own_fields(cur, book)
        if cur["qty"] <= 0 and _own_done(book):
            _own_finish(cur, now)
            return None
        return cur


def _own_residual(cur, legs, sb, now):
    """⭐ 第三輪品管 #4：確認過券商是反方向 ⇒ 這一趟收尾（我們剩的記成別處平掉）、券商上的改成「反向殘單」。⛔ 呼叫端拿著 _lock。"""
    book = _own_book(cur, now)
    for p in RATIO:
        o = int(round(book[p]["ours"]))
        if o > 0:
            cur.setdefault("own_adj", []).append({"p": p, "q": -o, "pq": None, "src": "elsewhere"})
    _own_finish(cur, now)
    opp = {p: -v for p, v in sb.items() if v < 0}
    same = {p: v for p, v in sb.items() if v > 0}
    if same:
        _state["last_error"] = "券商上兩種合約方向相反 —— 分不出來，不敢動，請自己確認"
        return "unknown"
    d = "short" if cur.get("dir") == "long" else "long"
    q = sum(opp.values())
    px = sum(float((legs.get(p) or {}).get("entry") or 0) * v for p, v in opp.items()) / q
    rp = {"dir": d, "entry": round(px, 2), "qty": q, "entry_time": None, "target_trade": None, "recovered": True,
          "residual": True, "rec_ok": True, "sl_src": "manual", "bpx": round(px, 2),
          "product": "MIX" if len(opp) == 2 else next(iter(opp)),
          "sl_warn": "券商上是反方向 %d 口等值（反向殘單）—— 照手動單的停損規矩處理，收盤／04:58 不會自動平，請到大戶投確認" % q}
    if len(opp) == 2:
        rp["mix"] = {p: opp.get(p, 0) for p in ("MXF", "TMF")}
    _state["position"] = rp
    _state["last_error"] = rp["sl_warn"]
    _log("own_residual", {"ok": False, "dir": d, "qty": q, "why": rp["sl_warn"]})
    return rp


def _own_meta(p):
    """帳本部位的落地內容（重啟後補回帳本；委託單物件存不了 ⇒ 存當下的明細數字＋委託單編號，重啟後再用編號問回來）。"""
    now = time.time()
    recs = []
    for r in p.get("own_trades") or []:
        q, pq, fin = _own_rec(r, now)
        x = {"role": r["role"], "p": r["p"], "n": r["n"], "at": r.get("at"), "id": r.get("id"),
             "q0": q, "pq0": pq, "fin0": fin}
        for k in ("conf", "px", "lost", "est"):   # 第四輪品管：確認口數、成交均價、重啟後問不到；第六輪：保守估計
            if r.get(k) is not None:
                x[k] = r[k]
        recs.append(x)
    keep = ("dir", "entry", "entry_fill", "entry0", "entry_qty", "qty", "qty_sent", "product", "mix", "mix0",
            "entry_time", "entry_day", "sl_points", "tp_points", "no_tp", "fill_lo", "fill_hi", "mix_sent_at",
            "late_leg", "own_adj", "own_seen", "close_reason", "fill_warn", "bpx", "entry_qty0", "late_fill", "restored",
            "tp_pending")
    m = {k: p.get(k) for k in keep if p.get(k) is not None}
    m.update(own=True, own_recs=recs)
    return m


def _own_restore(m):
    """⇒ 從落地的帳本補回部位（⛔ 鎖外面叫：會用委託單編號跟券商問回委託單）；補不回 ⇒ None。"""
    try:
        age = (date.today() - date.fromisoformat(str(m.get("day")))).days
    except ValueError:
        return None
    if not (0 <= age <= 1):
        return None
    byid = {}
    try:
        if _state["api"] is not None and is_live():
            _update_status()
            for t in _state["api"].list_trades() or []:
                byid[str(getattr(getattr(t, "order", None), "id", None))] = t
    except Exception:                       # noqa: BLE001  問不到 ⇒ 用落地時的數字
        byid = {}
    cur = {k: v for k, v in m.items() if k not in ("own_recs", "day", "own")}
    cur.update(own=True, recovered=False, restored=True, sl_src="meta", target_trade=None, own_trades=[])
    lost = {"entry": 0, "close": 0, "target": 0}
    for r in m.get("own_recs") or []:
        rr = dict(r)
        rr["t"] = byid.get(str(r.get("id"))) if r.get("id") is not None else None
        if rr["t"] is None and not (r.get("fin0") and r.get("fin0") != "to"):
            # 落地時還沒結束（或是停利單）、重啟後又問不到 ⇒ 照落地數字＋送出時間到期（_own_rec），停利單當成「可能還掛著」
            rr["lost"] = True
            lost[r["role"]] = lost.get(r["role"], 0) + 1
        cur["own_trades"].append(rr)
    if any(lost.values()):
        # ⭐ 2026-11-06 第四輪品管 #3：重啟後問不到委託單 ⇒ 大聲示警（⛔ 不安靜照舊）
        w = "重啟後跟券商問不到%s —— 先照落地時的明細數字，進場／平倉單送出 %g 秒後當成結束" % (
            "、".join("%d 張%s單" % (v, {"entry": "進場", "close": "平倉", "target": "停利"}[k])
                     for k, v in lost.items() if v), IOC_FINAL_S)
        if lost["target"]:
            w += "；停利單可能還掛著，平倉後請到大戶投刪除"
        _state["last_error"] = w
        _log("own_restore_lost", {"ok": False, "lost": lost, "why": w})
        print("⚠️ [真實] " + w, flush=True)
    # 停機期間停利已經全部成交（帳本算出 0 口）也照樣補回 ⇒ 對帳時立刻結算、記成績單（⛔ 不可以當成「已經記過」丟掉）
    return cur


def _close_own(pos, reason):
    """
    ⭐ 第三輪品管：帳本部位平倉（混搭／整筆小台／微台多口）。⛔ 呼叫端已拿 _close_lock、已對帳。
    每一輪：問券商每一腿 ⇒ 每一腿送 min(券商那一腿, 我們持有的保守值 hi)；上一張還沒結束 ⇒ hi 已經當成全部成交扣掉 ⇒ ⛔ 不會再送。
    我們的明細說全部平完（單都結束）⇒ 記成績單、清部位（⛔ 不等券商部位回報空手：那是舊資料，RECENT_CLOSE_S 內不撿回）。
    """
    import shioaji as sj
    act = sj.Action.Sell if pos["dir"] == "long" else sj.Action.Buy
    if not is_live():
        for p, n in _mix_of(pos).items():
            if n > 0:
                _send(_order(act, 0, sj.FuturesPriceType.MKP, sj.OrderType.IOC, sj.FuturesOCType.Cover,
                             qty=n, product=p), "close_" + reason, product=p)
        with _lock:
            _state["position"] = None
        return True, None
    stray = _own_cancel_targets(pos)
    _update_status()                          # ⭐ #1／#8：撤完停利單一定再讀一次停利明細（撤單那一瞬間成交的那段）
    with _lock:
        pos["close_reason"] = reason
    _CLOSE_INFLIGHT["on"] = True
    errs, last_err, unsure = {}, None, []
    before = {}
    word = {"MXF": "小台", "TMF": "微台"}
    for attempt in range(CLOSE_TRIES):
        legs = _broker_legs()
        if legs == "unknown":
            last_err = "跟券商確認不到每一腿還剩多少 —— 先不送平倉單（避免平過頭），稍後會再試"
            break
        sb = _own_signed_legs(legs, pos["dir"])
        _update_status()
        book = _own_book(pos)
        if any(sb[p] < 0 and book[p]["hi"] > 0 for p in RATIO):
            last_err = "券商上有一腿是反方向 —— 那一腿先不送平倉單，對帳確認後會改成反向殘單處理"
        todo, unsure = {}, []
        for p in ("MXF", "TMF"):
            hi = int(round(book[p]["hi"]))
            b = max(0, sb[p])
            n = (min(b, hi) // RATIO[p]) * RATIO[p]
            if n > 0 and p in before and b < before[p][0] - (book[p]["c"] - before[p][1]) - 1e-9:
                # 補送前確認：券商那一腿比「上一張送出前 − 那之後明細說成交的」還少 ⇒ 有明細沒講到的成交 ⇒ ⛔ 不補，冷卻後再問。
                # ⚠️ 券商比較多（還沒反映剛成交的舊資料）⇒ 照規則 ① 送 min(券商, 帳本保守值)：帳本是真的（老闆不會手動交易；
                #    main 的 test_close_multi_lag ② 也要求部分成交＋回報慢時同一輪補送剩下的）
                n = 0
            if n > 0:
                todo[p] = n
                before[p] = (b, book[p]["c"])
            elif hi > 0:
                unsure.append(p)
        if not todo:
            break
        sent_now = []
        for p, n in todo.items():
            try:
                o = _order(act, 0, sj.FuturesPriceType.MKP, sj.OrderType.IOC, sj.FuturesOCType.Cover, qty=n, product=p)
            except ValueError as e:
                errs[p] = str(e)
                continue
            ok, err, res = _send(o, "close_" + reason, product=p)
            if ok:
                with _lock:
                    _own_add(pos, "close", p, n, res)
                sent_now.append(pos["own_trades"][-1])
            else:
                errs[p] = err
        if not sent_now:
            last_err = "；".join("%s：%s" % (p, e) for p, e in errs.items()) or last_err
            break
        for _ in range(int(FILL_WAIT / 0.4)):
            time.sleep(0.4)
            _update_status()
            now = time.time()
            if all(_own_rec(r, now)[2] for r in sent_now):
                break
    _update_status()
    now = time.time()
    book = _own_book(pos, now)
    with _lock:
        if _state["position"] is pos:
            _own_fields(pos, book)
            if pos["qty"] <= 0 and _own_done(book):
                _own_finish(pos, now)
                msg = []
                if stray:
                    msg.append(_STRAY_MSG[stray])
                return True, ("平倉成功，但" + "；".join(msg)) if msg else None
    _close_fail["at"] = time.time()
    parts = []
    for p in ("MXF", "TMF"):
        o = int(round(book[p]["ours"]))
        if o > 0:
            hi = int(round(book[p]["hi"]))
            if book[p]["unsure"] > 0 and hi < o:
                # ⭐ 第六輪品管 #1：進場前券商就有舊部位、進場明細還沒回報 ⇒ 估計的那幾口 ⛔ 不送平倉單（券商的舊部位會騙人）
                parts.append("%s那一腿還剩 %d 口等值（其中 %d 口是進場明細還沒回報的估計，先不送平倉單；明細回報了就平）"
                             % (word[p], o, int(round(book[p]["unsure"]))))
                continue
            parts.append("%s那一腿還剩 %d 口等值%s" % (word[p], o, "（平倉單的成交還沒回報，先不再送）" if hi <= 0 else
                                                  ("（%s）" % errs[p] if errs.get(p) else "")))
        elif book[p]["c"] > 0:
            parts.append("%s那一腿已平掉（%d 口等值）" % (word[p], int(round(book[p]["c"]))))
    if book["_grow"] and not any(int(round(book[p]["ours"])) > 0 for p in RATIO):
        # ⭐ 第四輪品管建議 5：我們已知的口數平完了，但還有一腿進場單沒結束（晚到的腿可能還會進來）⇒ 部位先留著、
        #    那一腿回報到了照程式停損平掉、成績單同一筆（⛔ 不先結算：結算後晚到的那一腿會變成沒有程式停損的撿回部位）
        parts = ["已知的口數都平掉了，但還有一腿進場單的成交回報還沒到（最多再等 %g 秒）—— 先留著部位、回報到了會照停損平掉"
                 % IOC_FINAL_S]
    msg = ("；".join(parts) or "平倉單的成交還沒回報完") + "—— 部位還在，%.0f 秒後會再處理；等不及請立刻自己到大戶投平倉" % CLOSE_COOLDOWN
    if last_err:
        msg = last_err + "；" + msg
    if stray:
        msg += "；另外" + _STRAY_MSG[stray]
    _log("close_failed", {"ok": False, "err": last_err, "own": True,
                          "left": {p: int(round(book[p]["ours"])) for p in RATIO}, "why": msg})
    return False, msg


_STRAY_MSG = {True: "有停利單沒撤掉，請自己到大戶投刪除",
              "unknown": "停利單可能還掛著，請到大戶投刪除（重啟後問不到那張單、撤不了）"}


def _own_cancel_targets(pos):
    """撤掉帳本裡還掛著的停利單 ⇒ 有沒有撤不掉的。⛔ 不拿 _lock 問券商。"""
    if not is_live() or _state["api"] is None:
        return False
    _update_status()
    now = time.time()
    stray = False
    for r in list(pos.get("own_trades") or []):
        if r["role"] != "target":
            continue
        dq, _dpq, done = _own_rec(r, now)
        if done:
            continue
        if r.get("t") is None:
            # ⭐ 2026-11-06 第四輪品管 Q4-5：重啟後問不到這張停利單 ⇒ 撤不了，⛔ 不可以安靜成功（呼叫端照 stray 講出來）
            stray = "unknown" if not stray else stray
            _log("cancel_target", {"ok": False, "product": r["p"],
                                   "why": "重啟後問不到這張停利單、撤不了 —— 停利單可能還掛著，請到大戶投刪除"})
            continue
        try:
            _state["api"].cancel_order(r["t"])
            _log("cancel_target", {"ok": True, "product": r["p"], "why": "平倉前先撤掉還掛著的停利單"})
        except Exception as e:
            stray = True
            _log("cancel_target", {"ok": False, "product": r["p"], "err": str(e)[:200],
                                   "why": "撤不掉，平倉後請自己到大戶投刪掉那張停利單"})
    with _lock:
        pos["target_trade"] = None
        pos["target_trades"] = {}
    return stray


def _close_mix(pos, reason):
    """
    ⭐ 2026-11-06 混搭平倉（停損／收盤／04:58／手動都走 close() ⇒ 部位是混搭、這一趟是混搭開的〔mix0／mix_sent〕、
    或整筆小台，就到這裡）。⛔ 呼叫端已拿 _close_lock、已跟券商對過帳、已擋掉「分不出是誰的」（multi_unknown）。
      ・先撤掉每一張停利單，再**每一腿各自**送 Cover（MKP＋IOC）；每一輪送之前都重新問券商「那一腿還剩多少」。
      ・⛔⛔ 絕對不平過頭：每一腿送出的口數 ≤ min(券商上那一腿現在的口數, 那一腿我們最多還可能有的〔_leg_caps〕,
        開始時那一腿 − 之前送出去「可能已經成交」的口數)。上一張的成交還沒回報 ⇒ 當成已經成交、⛔ 不再送。
      ・停利單剛成交、部位還沒回報（品管 M4）⇒ 用停利單明細扣掉、那一段補記成 tp。
      ・一腿平掉、另一腿平不掉 ⇒ 部位留下剩的那一腿（降成單一合約）、回報失敗原因；下一輪（冷卻後）再平剩下的。
      ・看得到的都平完了、但進場單明細說還有一腿成交了券商還沒回報（晚到腿，品管 M7）⇒ ⛔ 不清部位：
        留下那一腿（late_pending），回報之後下一輪平掉；整趟還是一筆成績單。
      ・券商上有反方向的腿 ⇒ 一張都不再送、大聲講。
      ・⭐ 2026-11-06 第二輪品管（PM 拍板）：**只平我們的口數**，跟單一合約一樣 —— 每一腿平 min(券商那一腿, _leg_caps 上限)、
        合計不超過 _ours_left；平完之後券商上剩的就是他的 ⇒ 轉成他的手動部位（handover）。他加碼過（touched）也照這樣平
        （舊版整筆拒平 ⇒ 停損失效）。每一腿上限合起來比我們剩的還多（真的分不出哪一腿是誰的）⇒ 才不平、示警。
    """
    import shioaji as sj
    act = sj.Action.Sell if pos["dir"] == "long" else sj.Action.Buy
    if is_live() and pos.get("recovered") and not pos.get("rec_ok") and not pos.get("own") \
            and (pos.get("product") in ("MXF", "MIX")):
        # ⭐ 第三輪品管 #3：撿回來（沒有進場明細）的小台／混搭，還沒持續確認（相隔 ≥ CLOSE_COOLDOWN 兩次對帳都一樣）
        #    ⇒ ⛔ 不准一次送整筆（可能是剛平完的舊資料）
        _close_fail["at"] = time.time()
        return False, "剛撿回來的部位還在跟券商確認（%g 秒內兩次對帳都一樣才平）—— 等不及請自己到大戶投處理" % CLOSE_COOLDOWN
    if not is_live():
        # 演練：照本機記的兩腿各送一張（跟單一合約那條一樣，送了就當平掉）
        for p, n in _mix_of(pos).items():
            if n > 0:
                _send(_order(act, 0, sj.FuturesPriceType.MKP, sj.OrderType.IOC, sj.FuturesOCType.Cover,
                             qty=n, product=p), "close_" + reason, product=p)
        with _lock:
            _state["position"] = None
        return True, None
    had_tgt = pos.get("target_trades") is not None or pos.get("target_trade") is not None
    stray = _cancel_targets(pos)
    if had_tgt:
        # 撤停利單之前那一瞬間它可能又成交了幾口 ⇒ 撤完再對一次帳（剩 0 口 ⇒ reconcile 已經把那一趟記成 tp）
        again = reconcile()
        if again is None:
            return False, "沒有部位（撤停利單之前剩下的已經在停利成交了）"
        if again == "unknown":
            _close_fail["at"] = time.time()
            return False, "跟券商對帳失敗（撤完停利單之後），不確定還剩多少 —— 先不送平倉單；請到大戶投看一下"
        pos = again
        if pos.get("multi_unknown"):
            _close_fail["at"] = time.time()
            return False, pos_note(pos)[0] or "分不出是誰的，不平"
    legs0 = _broker_legs()
    if legs0 == "unknown":
        _close_fail["at"] = time.time()
        return False, "跟券商對帳失敗，不確定每一腿還剩多少 —— 先不送平倉單；請自己到大戶投平倉"
    if any(l.get("dir") != pos["dir"] for l in legs0.values()):
        _close_fail["at"] = time.time()
        return False, "券商上有一腿方向跟部位相反 —— 不敢送平倉單，請立刻自己到大戶投處理"
    _update_status()
    caps = _leg_caps(pos)
    h0, his = {}, {}
    for p in ("MXF", "TMF"):                  # 固定先小台再微台（跟進場同一個順序，紀錄好讀）
        if p not in legs0:
            continue
        b = int(legs0[p].get("qty") or 0)
        q = b
        if caps is not None:
            q = max(0, min(q, caps.get(p, 0)))
            q = (q // RATIO[p]) * RATIO[p]
        his[p] = b - q                        # 這一腿券商上不是我們的（他加碼的）
        if q > 0:
            h0[p] = q
    if not h0:
        if pos.get("late_pending"):
            _close_fail["at"] = time.time()
            return False, pos_note(pos)[0] or "晚到的那一腿券商還沒回報，回報後會再平"
        return False, "沒有部位"
    own_ok = not pos.get("handover") and _pq_ok(pos, pos.get("entry_qty"))
    if own_ok and sum(h0.values()) > _ours_left(pos):
        # 券商上的比我們剩的多（對帳之後才多出來的）⇒ 分不出哪一腿是他的 ⇒ ⛔ 不平
        _close_fail["at"] = time.time()
        return False, ("券商上有 %d 口等值，比程式剩下的 %d 口多 —— 分不出哪一腿的哪幾口是你的，不平，請自己到大戶投處理"
                       % (sum(h0.values()), _ours_left(pos)))
    _tp_untracked(pos, sum(h0.values()) + sum(his.values()))
    sent = {p: [] for p in h0}
    before = {}
    errs, last_err = {}, None
    _CLOSE_INFLIGHT["on"] = True
    # ⭐ 第二輪品管：下面「券商那一腿還剩多少」一律先扣掉他的（his），剩的才是我們的
    bl = legs0
    for attempt in range(CLOSE_TRIES):
        if attempt > 0:
            bl = _broker_legs()
            if bl == "unknown":
                last_err = "跟券商確認不到每一腿還剩多少 —— 先不送下一張平倉單（避免平過頭變反向），稍後會再試"
                break
        if any(l.get("dir") != pos["dir"] for l in bl.values()):
            _close_fail["at"] = time.time()
            _log("close_flipped", {"ok": False, "legs": {p: (l.get("dir"), l.get("qty")) for p, l in bl.items()},
                                   "why": "平倉之後券商出現反方向的腿，停手"})
            return False, "平倉之後券商上出現反方向的腿 —— 已經停手，請立刻自己到大戶投處理"
        _update_status()
        todo, unsure = {}, []
        for p in h0:
            held = int((bl.get(p) or {}).get("qty") or 0) - int(his.get(p) or 0)
            dealt, pend = _close_pend(sent[p])
            if held <= 0 or dealt >= h0[p] - 1e-9:
                continue                      # 這一腿平完了（券商說沒了，或明細說全部成交了）
            n = int(min(held, h0[p] - pend))
            n = (n // RATIO[p]) * RATIO[p]      # 小台那一腿一定是 5 的倍數（不然 _order 進位會多平）
            if n > 0 and p in before and held < before[p][0] - (dealt - before[p][1]) - 1e-9:
                # ⭐ 2026-11-06（PM）：補送前再確認一次 —— 這一腿券商部位比「上一張送出前 − 那之後明細說成交的」還少
                #    ⇒ 有成交是明細沒講的（狀態先到、明細晚到）⇒ ⛔ 不補，冷卻後下一輪重新問
                n = 0
            if n <= 0:
                unsure.append(p)              # 券商還顯示有，但上一張可能已經成交、只是回報慢 ⇒ ⛔ 不送
            else:
                todo[p] = n
                before[p] = (held, dealt)     # 這一張送出前：券商那一腿口數、累計明細
        if not todo:
            if unsure:
                last_err = "上一張平倉單的成交還沒回報（%s）—— 先不送下一張（避免平過頭），稍後會再試" % "、".join(unsure)
            break
        sent_now = []
        for p, n in todo.items():
            try:
                o = _order(act, 0, sj.FuturesPriceType.MKP, sj.OrderType.IOC, sj.FuturesOCType.Cover,
                           qty=n, product=p)
            except ValueError as e:
                errs[p] = str(e)
                continue
            ok, err, res = _send(o, "close_" + reason, product=p)
            if ok:
                sent[p].append((res, n))
                sent_now.append(p)
                with _lock:
                    pos.setdefault("close_hist_trades", {}).setdefault(p, []).append((res, n))
            else:
                errs[p] = err
        if not sent_now:
            last_err = "；".join("%s：%s" % (p, e) for p, e in errs.items()) or last_err
            break
        flat = False
        for _ in range(int(FILL_WAIT / 0.4)):
            time.sleep(0.4)
            b2 = _broker_legs()
            if isinstance(b2, dict) and all(int((b2.get(p) or {}).get("qty") or 0) <= int(his.get(p) or 0)
                                            for p in sent_now):
                flat = True
                break
        if flat:
            continue
        _log("close_nofill", {
            "ok": False, "try": attempt + 1, "product": "MIX", "sent": todo,
            "why": "混搭：這一輪每一腿送出去了，下一輪先問券商每一腿還剩多少"})
    # ── 結算：每一腿還剩多少（券商說的、跟委託單明細說的取小的；明細說全部成交 ⇒ 0）
    _update_status()
    fin = _broker_legs()
    remain, alltr = {}, []
    for p in h0:
        dealt, _pend = _close_pend(sent[p])
        alltr += [t for t, _n in sent[p]]
        rd = max(0, int(round(h0[p] - dealt)))
        if isinstance(fin, dict):
            # ⭐ 第二輪品管：券商那一腿剩的要先扣掉他的，才是我們還剩的（⛔ 不可以拿他的口數推成「我們還沒平完」⇒ 下一輪平到他的）
            rb = max(0, int((fin.get(p) or {}).get("qty") or 0) - int(his.get(p) or 0))
            remain[p] = 0 if rd == 0 else min(rb, rd)
        else:
            remain[p] = rd
    gone = sum(h0[p] - remain[p] for p in h0)
    left = sum(remain.values())
    with _lock:
        cl = dict(pos.get("closed_by_leg") or {})
        for p in h0:
            cl[p] = int(cl.get(p) or 0) + int(h0[p] - remain[p])
        pos["closed_by_leg"] = cl
    pend = _late_pending(pos) if left == 0 else {}
    if left == 0 and not pend:
        exit_px = _fill_price_many(alltr, want=gone)
        rec = record_trade(pos, exit_px, reason, qty=gone)
        rest = _his_rest(fin, his, pos["dir"])
        with _lock:
            # 我們的都平完了；券商上剩的是他加碼的 ⇒ 轉成他的手動部位（跟 main 的 _close_multi 一樣）
            _state["position"] = _his_manual(rest) if rest else None
        _log("close_confirmed", {"ok": True, "product": "MIX", "stray_target": stray, "exit": exit_px,
                                 "points": (rec or {}).get("points"), "closed": gone,
                                 "legs": {p: h0[p] for p in h0}})
        msg = []
        if stray:
            msg.append("有停利單沒撤掉，請自己到大戶投刪除")
        if rest:
            msg.append("平掉我們的 %d 口等值；剩下 %d 口等值是你自己的，沒有動" % (gone, rest["qty"]))
        return True, ("平倉成功，但" + "；".join(msg)) if msg else None
    # 沒平完：⛔ 絕對不可以清掉本機部位。平掉的那一段記成一段 leg（整趟結束時一起記成績單），部位留下剩的。
    s = _sum_deals(alltr)
    px = round(s[0] / s[1], 2) if (s is not None and gone > 0 and abs(s[1] - gone) < 1e-9) else None
    with _lock:
        cur = _state["position"] if isinstance(_state["position"], dict) else pos
        if gone > 0:
            cur.setdefault("legs", []).append({"qty": int(gone), "px": px, "src": reason,
                                               "at": datetime.now().strftime("%H:%M:%S")})
        if pend:
            _pending_stub(cur, pend)
        else:
            cur["qty"] = int(left) + sum(int(his.get(p) or 0) for p in his)   # 本機口數＝券商口數（含他的，跟 main 一樣）
            both = [p for p in ("MXF", "TMF") if remain.get(p, 0) + int(his.get(p) or 0) > 0]
            if len(both) == 2:
                cur["product"] = "MIX"
                cur["mix"] = {p: int(remain.get(p, 0)) + int(his.get(p) or 0) for p in ("MXF", "TMF")}
            else:
                cur["product"] = both[0]
                cur.pop("mix", None)
        _state["position"] = cur
    _close_fail["at"] = time.time()
    word = {"MXF": "小台", "TMF": "微台"}
    if pend:
        msg = ("看得到的已經平掉 %d 口等值；進場時%s的成交券商還沒回報（委託單明細說成交了）—— 部位留著那一腿，"
               "回報之後會再平；等不及請立刻自己到大戶投平倉" %
               (gone, "、".join("%s %d 口等值" % (word[p], n) for p, n in pend.items())))
        if stray:
            msg += "；另外有停利單沒撤掉，請自己到大戶投刪除"
        _log("close_late_pending", {"ok": False, "closed": gone, "pending": pend, "why": msg})
        return False, msg
    parts = []
    for p in h0:
        if remain[p] == 0:
            parts.append("%s那一腿已平掉（%d 口等值）" % (word[p], h0[p]))
        else:
            parts.append("%s那一腿還剩 %d 口等值%s" % (word[p], remain[p],
                                                  ("（%s）" % errs[p]) if errs.get(p) else ""))
    msg = "；".join(parts) + "—— 部位還在，%.0f 秒後會再平剩下的；等不及請立刻自己到大戶投平倉" % CLOSE_COOLDOWN
    if last_err:
        msg = last_err + "；" + msg
    if stray:
        msg += "；另外有停利單沒撤掉，請自己到大戶投刪除"
    _log("close_failed", {"ok": False, "err": last_err, "product": "MIX", "remain": remain, "closed": gone,
                          "why": "混搭沒有平完，剩下的那一腿留著，本機狀態保留"})
    return False, msg


def _his_rest(fin, his, direction):
    """
    ⭐ 2026-11-06 第二輪品管：我們的都平完了，券商上剩下的（他加碼的）⇒ 給 _his_manual 用的部位 dict；沒有 ⇒ None。
    每一腿取 min(券商現在那一腿, 開始時判定是他的)（券商回報慢的話，現在那一腿可能還含我們剛平掉的）。
    """
    legs = {}
    for p, h in (his or {}).items():
        h = int(h or 0)
        if isinstance(fin, dict):
            f = fin.get(p) or {}
            if f and f.get("dir") != direction:
                continue
            h = min(h, int(f.get("qty") or 0))
            px = float(f.get("entry") or 0)
        else:
            px = 0.0
        if h > 0:
            legs[p] = (h, px)
    if not legs:
        return None
    q = sum(v[0] for v in legs.values())
    out = {"dir": direction, "qty": q, "entry": round(sum(v[0] * v[1] for v in legs.values()) / q, 2)}
    if len(legs) == 2:
        out.update(product="MIX", mix={p: legs[p][0] for p in ("MXF", "TMF")})
    else:
        out["product"] = next(iter(legs))
    return out


def _tp_untracked(pos, held_now):
    """
    ⭐ 2026-11-06 品管 M4：停利單剛成交、券商部位還沒回報（對帳沒記到那一段）⇒ 平倉前補記一段 tp leg，
    成績單才是整趟（不然那幾口從成績單上消失）。量 ＝ min(停利明細還沒算進 legs 的量, 本機口數 − 這次要平的)；
    價 ＝ 停利明細扣掉已經算過的那段。⛔ 不問券商（_cancel_targets 剛記好 tgt_fill）。
    """
    tf = pos.get("tgt_fill") or {}
    tq = sum(float(v[1]) for v in tf.values())
    tp_ = sum(float(v[0]) for v in tf.values())
    used = pos.get("tgt_used") or [0.0, 0.0]
    nq = tq - float(used[1])
    n = int(min(nq, int(pos.get("qty") or 0) - held_now))
    if n <= 0 or nq <= 0:
        return
    avg = (tp_ - float(used[0])) / nq
    with _lock:
        pos.setdefault("legs", []).append({"qty": n, "px": round(avg, 2), "src": "tp",
                                           "at": datetime.now().strftime("%H:%M:%S")})
        pos["tgt_used"] = [float(used[0]) + avg * n, float(used[1]) + n]
        pos["qty"] = int(pos.get("qty") or 0) - n
    _log("position_partial", {"ok": True, "gone": n, "px": round(avg, 2), "src": "tp",
                              "why": "平倉前發現停利單剛成交 %d 口（部位還沒回報）—— 補記成 tp" % n})


def _pending_stub(cur, pend):
    """
    ⭐ 2026-11-06 品管 M7：看得到的都平完了、晚到那一腿還沒回報 ⇒ 部位變成「只剩晚到那一腿」（⛔ 呼叫端拿著 _lock）。
    進場量（entry_qty／mix0）加上那一腿、進場價重新加權（帳本的價留在 entry0）、標 late_pending（對帳看到券商空手先不清，
    LATE_LEG_S 內等它回報）。之後停損／收盤／04:58 照常認得、照常平；整趟還是一筆成績單。
    """
    ets = cur.get("entry_trades") or {}
    eq0 = int(cur.get("entry_qty") or 0)
    ef = float(cur.get("entry_fill", cur.get("entry")))
    addpx = {}
    for p, n in pend.items():
        s = _deal_sums(ets.get(p))
        addpx[p] = (s[0] / s[1]) if (s is not None and s[1] > 0) else ef
        if s is not None and s[2] is not None:
            # 第二輪品管 QA2-6：這期間重啟要從落地檔補回 ⇒ 成交價區間要含晚到那一腿（_meta_apply 第 ④ 道）
            cur["fill_lo"] = min(float(cur.get("fill_lo", s[2])), s[2])
            cur["fill_hi"] = max(float(cur.get("fill_hi", s[3])), s[3])
    # 券商回報那一腿時的均價＝那一腿的成交均價（重啟後 _meta_apply 比對「券商均價沒變」用）
    cur["bpx"] = round(sum(addpx[p] * n for p, n in pend.items()) / max(1, sum(pend.values())), 2)
    tot = eq0 + sum(pend.values())
    ef2 = round((ef * eq0 + sum(addpx[p] * n for p, n in pend.items())) / tot, 2) if tot > 0 else ef
    m0 = cur.get("mix0") if isinstance(cur.get("mix0"), dict) else \
        {cur.get("product") if cur.get("product") in RATIO else "TMF": eq0}
    cur["mix0"] = {p: int(m0.get(p) or 0) + int(pend.get(p) or 0) for p in ("MXF", "TMF")}
    cur.setdefault("entry0", ef)
    cur.update(entry_qty=tot, entry_fill=ef2, entry=ef2, late_leg=True, late_fill=True,
               qty=int(sum(pend.values())), late_pending=dict(pend), late_pending_at=time.time())
    both = [p for p in ("MXF", "TMF") if pend.get(p)]
    if len(both) == 2:
        cur["product"], cur["mix"] = "MIX", {p: int(pend.get(p) or 0) for p in ("MXF", "TMF")}
    else:
        cur["product"] = both[0]
        cur.pop("mix", None)


def _late_pending(pos):
    """
    ⭐ 2026-11-06：這一趟送出去的進場單，明細說成交了、但部位裡沒算到的口數（＝還沒回報的晚到腿）⇒ {合約: 等值}。
    沒有／問不到 ⇒ {}。⛔ 呼叫端已經 _update_status。
    """
    ms, ets = pos.get("mix_sent"), pos.get("entry_trades") or {}
    if not isinstance(ms, dict) or not is_live():
        return {}
    have = pos.get("mix0") if isinstance(pos.get("mix0"), dict) else \
        {pos.get("product") or "TMF": int(pos.get("entry_qty") or pos.get("qty") or 0)}
    out = {}
    for p in ms:
        s = _deal_sums(ets.get(p))
        if s is not None and s[1] > int(have.get(p) or 0) + 1e-9:
            out[p] = int(round(s[1] - int(have.get(p) or 0)))
    return out


def _mix_of(pos):
    """部位 ⇒ {合約: 微台等值}（混搭 ⇒ 兩腿；單一合約 ⇒ 那一種）。"""
    if isinstance(pos.get("mix"), dict):
        return {p: int(pos["mix"].get(p) or 0) for p in ("MXF", "TMF")}
    return {pos.get("product") or "TMF": int(pos.get("qty") or 0)}


def _num_qty(q):
    try:
        return int(q) if q is not None else 1
    except (TypeError, ValueError):
        return 1


LOOP_SEED_S = 300.0    # ⭐ 第七輪品管 #2：剛平完那一趟的平倉單當「起算」最多用多久（券商舊資料卡更久 ⇒ 照一般撿回規則）
LOOP_PX_TOL = 0.5      # 券商均價跟剛平掉那一趟差多少以內才算「同一份舊資料」
_LOOP = {"dir": None, "base": 0.0, "trades": [], "seed": False, "at": 0.0, "tok": None, "px": None}
_LOOP_N = [0]


def _loop_reset():
    _LOOP.update(dir=None, base=0.0, trades=[], seed=False, at=0.0, tok=None, px=None)


def _loop_start(pos, base, now):
    """從現在起算：這個部位（記號 loop_tok）、券商口數 base、還沒送過。"""
    _LOOP_N[0] += 1
    pos["loop_tok"] = _LOOP_N[0]
    _LOOP.update(dir=pos.get("dir"), base=float(base), trades=[], seed=False, at=now, tok=_LOOP_N[0], px=None)


def _loop_seed(direction, base, trades, px):
    """
    帳本部位乾淨平完一趟 ⇒ 這一趟的平倉／停利單記成「起算」（種子）：之後券商還顯示**同方向、同樣口數、同樣均價**
    （舊資料）被撿回來 ⇒ 一口迴圈先看這些單已經平掉多少、⛔ 不再送。LOOP_SEED_S 之後失效。
    """
    if not trades or px is None:
        _loop_reset()
        return
    _LOOP.update(dir=direction, base=float(base), trades=list(trades), seed=True, at=time.time(), tok=None, px=float(px))


def _loop_guard(pos):
    """一口迴圈送下一張之前 ⇒ None（可以送）／一句話（先不送）。⛔ 不拿鎖。"""
    now = time.time()
    d = pos.get("dir")
    stale_seed = _LOOP["seed"] and now - _LOOP["at"] >= LOOP_SEED_S
    if _LOOP["dir"] != d or not _LOOP["trades"] or stale_seed \
            or (not _LOOP["seed"] and pos.get("loop_tok") != _LOOP["tok"]):
        # 這個部位第一次送 ⇒ 從現在（對帳剛確認的口數）起算（種子以外，起算的單只認同一個部位）
        _loop_start(pos, _num_qty(pos.get("qty")) or 0, now)
        return None
    _update_status()
    _dealt, pend = _sent_dealt(_LOOP["trades"])
    bp = broker_position()
    if bp == "unknown":
        return "跟券商確認不到部位 —— 先不送下一張平倉單（避免平過頭），稍後會再試"
    bq = float(_num_qty(bp.get("qty")) or 0) if isinstance(bp, dict) and bp.get("dir") == d else 0.0
    if bq <= _LOOP["base"] - pend + 1e-9:
        if _LOOP["seed"]:
            # 剛平完那一趟之後券商少於「應剩」⇒ 不是那份舊資料了 ⇒ 從現在起算
            _loop_start(pos, bq, now)
        return None
    if _LOOP["seed"] and (abs(bq - _LOOP["base"]) > 1e-9 or not isinstance(bp, dict)
                          or abs(float(bp.get("entry") or 0) - float(_LOOP.get("px") or 0)) > LOOP_PX_TOL):
        # 券商顯示的跟剛平掉的那一趟口數或均價不一樣 ⇒ 不是那份舊資料（是新的部位）⇒ 從現在起算
        _loop_start(pos, bq, now)
        return None
    msg = ("之前送出的平倉單可能已經成交 %g 口（明細還沒回報的當成已成交），券商部位還顯示 %g 口、沒有反映 —— "
           "判斷是券商舊資料，先不送下一張（避免平過頭變反向），%.0f 秒後再試；如果大戶投真的還有部位請自己平"
           % (pend, bq, CLOSE_COOLDOWN))
    if not pos.get("loop_said"):
        pos["loop_said"] = True
        _log("close_loop_wait", {"ok": False, "base": _LOOP["base"], "pend": pend, "broker": bq, "seed": _LOOP["seed"],
                                 "why": msg})
        print("⚠️ [真實] " + msg, flush=True)
    return msg


def _close_order_state(trade):
    """上一張平倉單 ⇒ 'filled'（明細有成交）／'unfilled'（IOC 已取消、沒成交）／'unknown'。⛔ 不拿鎖。"""
    if trade is None or _state["api"] is None:
        return "unknown"
    _update_status()
    s = _deal_sums(trade)
    if s is not None and s[1] >= 1:
        return "filled"
    st = str(getattr(getattr(trade, "status", None), "status", "") or "")
    if "Cancel" in st or "Fail" in st:
        return "unfilled"
    return "unknown"


def _still_open(direction, tries=6, gap=0.4):
    """⇒ 'open'（還在、方向一樣）／'flat'（已經沒有）／'flip'（方向反了）／'unknown'（一直查不到）。"""
    for i in range(tries):
        p = broker_position()
        if p is None:
            return "flat"
        if isinstance(p, dict):
            return "open" if p.get("dir") == direction else "flip"
        if i + 1 < tries:
            time.sleep(gap)
    return "unknown"


def _sent_dealt(trades):
    """
    ⭐ 2026-11-06（QA 實測：部位回報停在舊資料 ≥ 5 秒 ⇒ 多口平倉整筆再送一次 ⇒ 平過頭變反向全口數，還回報成功）。
    已經送出去的平倉單 [(trade, 送出口數)] ⇒ (確定成交幾口, **保守估計**可能成交幾口)。
    保守估計：明細滿了 ⇒ 送出量；IOC 已經結束（已成交／已取消／失敗）⇒ 明細量；
    **其他（還沒回報、問不到明細）⇒ 當成全部成交**。
    ⚠️ 2026-11-06 模擬帳戶實測：IOC 送 3 口只成交 1 口，狀態是 'OrderStatus.Filled'（⛔ 不是 Cancelled）
       ⇒ 「Filled」也要算結束、照明細量算；只認 Cancel 的話，部分成交會被當成全部成交 ⇒ 停損平一半要等下一輪。
    ⛔ 呼叫端先 _update_status。
    """
    dealt = pend = 0.0
    for t, n in trades:
        sm = _deal_sums(t)
        d = sm[1] if sm is not None else 0.0
        st = str(getattr(getattr(t, "status", None), "status", "") or "")
        dealt += d
        if sm is not None and d >= n - 1e-9:
            pend += n
        elif sm is not None and ("Cancel" in st or "Fail" in st or "Filled" in st):
            pend += d
        else:
            pend += n
    return dealt, pend


def _close_multi(pos, reason, act, stray):
    """
    ⭐ 2026-10-14 多口平倉（我們自己開的多口部位才走這裡；⛔ 呼叫端已拿 _close_lock、已撤停利單）。
      ・只平「**我們剩下的口數**」：自己開的口數 − 先前出去的那幾段（legs）。他自己加碼的那幾口 ⛔ 不碰 ——
        平完後券商上還剩的就是他的，本機改成「他的手動部位」（停損照手動那一套、收盤平倉⛔ 不碰）。
      ・每張只送還沒平掉的口數；部分成交 ⇒ 下一張送剩下的。
      ・⭐ QA M3：整個等待期間都問不到部位 ⇒ ⛔ 不盲目再送一張（會平過頭），改看**委託單自己的成交明細**：
        明細說平完了就算平完；明細也問不到 ⇒ 停手、講清楚。
      ・平過頭變成反向部位 ⇒ 一張都不再送、大聲講。
    """
    import shioaji as sj
    start_q = int(pos.get("qty") or 0)
    to_close = min(start_q, _ours_left(pos))
    if to_close <= 0:
        return False, "這一筆已經沒有我們的口數了（券商上剩的是你自己的）—— 不碰，要平請自己到大戶投"
    done, trades, last_err = 0, [], None
    sent = []                                   # ⭐ 2026-11-06 [(委託單, 送出口數)]
    _CLOSE_INFLIGHT["on"] = True
    for attempt in range(CLOSE_TRIES):
        n_send = to_close - done
        if sent and is_live():
            # ⭐⭐ 2026-11-06 平過頭的根因修法：第二張起，**先看前面那幾張自己的成交明細**，⛔ 不能只信部位
            #    （部位回報停在舊資料時 done 會是 0 ⇒ 舊版整筆再送一次）。
            #    明細說平完了 ⇒ 不送、算平完；還沒回報的那幾張 ⇒ 當成已經成交、⛔ 不再送（寧可這一輪平不完、冷卻後再來）。
            _update_status()
            dealt, pend = _sent_dealt(sent)
            if dealt >= to_close - 1e-9:
                done = to_close
                exit_px = _fill_price_many(trades, want=to_close)
                rec = record_trade(pos, exit_px, reason, qty=to_close)
                with _lock:
                    _state["position"] = None
                _log("close_confirmed", {"ok": True, "tries": attempt, "stray_target": stray, "exit": exit_px,
                                         "points": (rec or {}).get("points"), "closed": to_close,
                                         "his_left": 0, "by": "deals"})
                return True, ("平倉成功，但那張停利單沒撤掉，請自己到大戶投刪除" if stray else None)
            if pend > dealt and dealt > 0 and abs(done - dealt) < 1e-9:
                # 部分成交、IOC 狀態字串還不是「已取消」：**明細跟部位兩邊各自說的已平口數一樣** ⇒ 兩份獨立的回報對得上
                # ⇒ 那張 IOC 已經結束、剩下的是真的沒撮到 ⇒ 照實際成交量算（⛔ 不然部分成交時永遠送不出第二張＝停損平一半）。
                #    部位停在舊資料時 done 不會剛好等於明細的口數 ⇒ 照舊保守、不送。
                pend = dealt
            n_send = int(min(to_close - done, to_close - pend))
            if n_send <= 0:
                last_err = "上一張平倉單的成交還沒回報 —— 先不送下一張（避免平過頭變反向），稍後會再試"
                _log("close_wait_report", {"ok": False, "try": attempt + 1, "dealt": dealt, "pend": pend,
                                           "why": "前一張可能已經成交、只是回報慢 ⇒ 不再送"})
                break
        ok, err, res = _send(
            _order(act, 0, sj.FuturesPriceType.MKP, sj.OrderType.IOC,
                   sj.FuturesOCType.Cover, qty=n_send),
            "close_" + reason)
        last_err = err
        if not ok:
            break
        trades.append(res)
        sent.append((res, n_send))
        if not is_live():
            with _lock:
                _state["position"] = None
            return True, None
        finished, seen, rest = False, False, None
        for _ in range(int(FILL_WAIT / 0.4)):
            time.sleep(0.4)
            bp = broker_position()
            if bp is None:
                finished, done = True, to_close
                break
            if isinstance(bp, dict):
                seen = True
                if bp.get("dir") != pos["dir"]:
                    # ⛔ 平過頭變成反向部位 ⇒ 一張都不再送（再送只會越平越多），大聲講
                    _close_fail["at"] = time.time()
                    _log("close_flipped", {"ok": False, "now": bp.get("dir"), "qty": bp.get("qty"),
                                           "why": "平倉之後券商出現反向部位，停手"})
                    return False, ("平倉之後券商上變成反向 %s 口 —— 已經停手，請立刻自己到大戶投處理"
                                   % bp.get("qty"))
                done = max(done, start_q - int(bp.get("qty") or start_q))
                if done >= to_close:
                    finished, rest = True, bp
                    break
        if not finished and not seen:
            # 整段都問不到部位 ⇒ 看委託單自己的明細（⛔ 不盲目再送）
            _update_status()
            dealt = 0.0
            for tr in trades:
                s = _deal_sums(tr)
                if s is None:
                    dealt = None
                    break
                dealt += s[1]
            if dealt is None:
                last_err = "平倉單送出去了，但部位與成交明細都問不到 —— 不知道平掉了沒，沒有再送"
                break
            done = int(round(dealt))
            finished = done >= to_close
        if finished:
            exit_px = _fill_price_many(trades, want=to_close)
            rec = record_trade(pos, exit_px, reason, qty=to_close)
            his = int(rest.get("qty") or 0) if isinstance(rest, dict) else 0
            with _lock:
                _state["position"] = _his_manual(rest) if his > 0 else None
            _log("close_confirmed", {"ok": True, "tries": attempt + 1, "stray_target": stray,
                                     "exit": exit_px, "points": (rec or {}).get("points"),
                                     "closed": to_close, "his_left": his})
            msg = []
            if stray:
                msg.append("那張停利單沒撤掉，請自己到大戶投刪除")
            if his:
                msg.append("平掉我們的 %d 口；剩下 %d 口是你自己的，沒有動" % (to_close, his))
            return True, ("平倉成功，但" + "；".join(msg)) if msg else None
        _log("close_nofill", {"ok": False, "try": attempt + 1, "done": done,
                              "why": "送出了但還沒平完，下一張只送剩下的"})
    _close_fail["at"] = time.time()
    _log("close_failed", {"ok": False, "err": last_err, "done": done,
                          "why": "沒有平完，部位還在，本機狀態保留"})
    return False, (last_err or ("平不完（範圍市價 IOC 連續 %d 次沒撮到）—— 部位還在，"
                                "請立刻自己到大戶投平倉" % CLOSE_TRIES))


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
        pos = {k: v for k, v in pos.items() if not str(k).endswith("_trade")}   # ⭐ 多口多了 entry_trade，一樣濾掉
        # ⭐ 2026-11-06 混搭：兩腿的單放在 entry_trades／target_trades（dict 裡是永豐物件）⇒ 一樣濾掉
        pos = {k: v for k, v in pos.items() if not str(k).endswith("_trades")}
        pos["has_target"] = _state["position"].get("target_trade") is not None \
            or bool(_state["position"].get("target_trades"))
        # ⭐ QA r2 必修 2：多口部位不照平常規矩走時（他動過／交接／對不上／成交口數一度對不上）給畫面一句實話。
        #    一口的部位沒有這幾個欄位 ⇒ /api/state 跟以前一模一樣。
        _note, _kind = pos_note(_state["position"])
        if _note:
            pos.update(lot_note=_note, lot_note_kind=_kind,
                       no_auto_exit=_kind in ("touched", "handover", "unknown"))
            if _kind == "touched":
                pos["own_left"] = _ours_left(_state["position"])
            if _kind == "unknown":
                pos["stop_off"] = True      # ⭐ QA r3 必修 3：停損不會執行 ⇒ 畫面不畫停損價／刻度
    return {"live": is_live(), "position": pos, "trades": trades_today(),
            # 勝率要看得夠多筆才有意義，所以另外給「所有留下來的」
            "trades_all": trades_history(),
            # 前端要靠這個把 trades_all 切成「今天」與「過去」兩段。
            # ⚠️ 一定要由**伺服器**給：瀏覽器自己算 new Date() 在跨午夜那一刻會跟
            #    trades_today() 讀的檔案不同一天 ⇒ 同一筆交易在兩個清單裡各出現一次。
            "today": str(date.today()),
            "ca_ok": CA_OK["ok"], "ca_msg": CA_OK["msg"],
            "entries_today": entries_today(), "max_entries": MAX_ENTRIES,
            "account": str(getattr(_state["account"], "account_id", "")) or None,
            "last_error": _state["last_error"]}
