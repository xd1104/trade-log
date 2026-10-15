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
        for d in deals:
            dq, dp = float(getattr(d, "quantity", 0) or 0), float(d.price)
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
        if nq > 0 and abs(nq - n) < 1e-9:
            px, src = round((s[0] - float(used[0])) / nq, 2), "tp"
            cur["tgt_used"] = [s[0], s[1]]
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
            out.append({
                "entry": float(getattr(r, "entry_price", 0) or 0),
                "exit": float(getattr(r, "cover_price", 0) or 0),
                "pnl": float(getattr(r, "pnl", 0) or 0),
                "qty": int(getattr(r, "quantity", 0) or 0),
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
    return isinstance(p, dict) and qty_ok(p.get("qty_sent")) and p["qty_sent"] > 1 or \
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
    return {"dir": bp["dir"], "entry": float(bp.get("entry") or 0), "qty": q,
            "entry_time": None, "target_trade": None, "recovered": True, "sl_src": "manual",
            "handover": True, "bpx": float(bp.get("entry") or 0),
            "sl_warn": "程式開的那幾口已經出場；剩下 %d 口是你自己加碼的 —— 面板照手動單的規矩用 %g 點停損，"
                       "收盤／04:58 不會自動平" % (q, MANUAL_SL_POINTS)}


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
              "bpx", "fill_lo", "fill_hi", "entry_day", "real_base")
_META = {"last": None}


def _meta_wanted(p):
    return isinstance(p, dict) and (_multi_pos(p) or bool(p.get("handover")))


def _meta_sync():
    """把現在這口多口部位的狀態寫下來（沒變就不寫）；部位沒了 ⇒ 清掉。⛔ 永遠不丟例外。⛔ 不在 _lock 裡叫。"""
    try:
        p = _state["position"]
        path = ORDER_DIR / META_NAME
        if _meta_wanted(p) and p.get("multi_unknown"):
            return                          # 分不出（含等對帳中）⇒ ⛔ 不覆蓋落地檔（再重啟一次還是照原本那份比）
        if _meta_wanted(p):
            m = {k: p.get(k) for k in _META_KEYS if p.get(k) is not None}
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


def _meta_apply(got, meta):
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
    q = int(got.get("qty") or 0)
    if q == 1 and not meta.get("handover"):
        # ⭐ QA r4（G7）：撿回來的是 1 口、落地檔又不是交接 ⇒ 不套用落地檔，走原本的帳本認人（1 口那一套）
        #    ——落地檔可能是更早那一趟多口留下的（當掉沒清），拿它比 1 口只會把 1 口的單誤標成分不出。
        return None
    try:
        px = float(got.get("entry"))
        mq, mpx = int(meta.get("qty")), float(meta.get("bpx"))
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
        if k in ("qty",):
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
    out["tsums"] = _deal_sums(cur.get("target_trade"))
    out["esums"] = _deal_sums(cur.get("entry_trade"))
    return out


def reconcile():
    """啟動時／每次要動作之前叫。把券商的實況寫回 _state。"""
    pos = broker_position()
    if pos == "unknown":
        return "unknown"
    _cur0 = _state["position"]
    pre = _reconcile_prefetch(_cur0, pos)          # ⛔ 鎖外面問券商
    # ⭐ QA r2 必修 4：要撿回部位時，先把上次落地的多口狀態讀好（⛔ 鎖外面讀檔）
    meta_pre = _meta_load() if (_cur0 is None and isinstance(pos, dict)) else None
    out = _reconcile_locked(pos, _cur0, pre, meta_pre)
    _meta_sync()                                   # ⛔ 鎖外面寫檔（多口部位才寫；一口的部位什麼都不做）
    return out


def _reconcile_locked(pos, _cur0, pre, meta_pre):
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
                gone = _state["position"]
                _state["position"] = None
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
                   "target_trade": None, "recovered": True}
            _recover_meta(got)
            if meta_pre is not None:
                # ⭐ QA r2 必修 4：上次落地的多口狀態對得上 ⇒ 補回（他動過、出去過的段、後補成交、交接）
                # ⭐ QA r3：證據要全部對得上（口數、券商均價、成交價區間）；有一項對不上 ⇒ 標「分不出」、不平、示警
                _ma = _meta_apply(got, meta_pre)
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
            if (mine and qty_ok(oq) and 0 < pos["qty"] < oq
                    and cur.get("dir") == pos["dir"] and not _CLOSE_INFLIGHT["on"]):
                n = oq - pos["qty"]
                if cur.get("touched") and not _tp_matches(cur, n, tsums):
                    # 他加碼過、這次少的又不是停利單吃的 ⇒ 先算成**他自己那幾口**（他在別處減碼），
                    # 超過他那幾口的部分才算我們的（⛔ 不然我們剩幾口會被低估、停損少平）。
                    n -= min(n, max(0, oq - _ours_left(cur)))
                if n > 0:
                    _partial_leg(cur, n, tsums)
                if cur.get("touched") and _ours_left(cur) <= 0:
                    # 我們那幾口全部出去了，券商上剩的都是他的 ⇒ 這一趟收尾記帳、剩下的改成他的手動部位
                    record_trade(cur, None, "legs", qty=0)
                    _state["position"] = _his_manual(pos)
                    _log("position_handover", {"ok": True, "qty": pos["qty"],
                                               "why": "我們的口數都出場了；剩下 %d 口是他自己的 —— 改成手動部位" % pos["qty"]})
                    return _state["position"]
            if (mine and qty_ok(oq) and pos["qty"] > oq and cur.get("dir") == pos["dir"]
                    and not cur.get("touched") and pos["qty"] > _ours_left(cur)):
                # ⚠️ QA r4（G5）：只有**超過我們該有的口數**才要分「回報慢／他加碼」。重啟後從帳本補回的
                #    （帳本 fill_unsure ⇒ 我們有 3 口），券商從 1 口補到 3 口 ⇒ 本來就是我們的，⛔ 不標「他動過」。
                # ⭐ 2026-10-14 QA 必修 2／3：券商口數**變多**了。兩種可能，⛔ 用委託單自己的成交明細分：
                #   ① 進場那張單其實成交了更多口，只是部位回報慢一拍（明細 ＝ 現在口數、而且沒有出去過的段）
                #      ⇒ 是我們的：entry_qty 改成實際成交的口數（停利單只掛了先前那幾口 ⇒ 示警）。
                #   ② 其他（他自己在大戶投加碼）⇒ 標「他動過」（touched）：⛔ 不改進場價、收盤⛔ 不平、
                #      停損只平「我們剩下的口數」（見 _close_multi）。
                es = pre["esums"] if cur is _cur0 else None
                sent = cur.get("qty_sent") if qty_ok(cur.get("qty_sent")) else _own_qty(cur)
                if (es is not None and not cur.get("legs") and abs(es[1] - pos["qty"]) < 1e-9
                        and pos["qty"] <= sent):
                    cur["entry_qty"] = int(pos["qty"])
                    cur["late_fill"] = True
                    _log("entry_late_fill", {"ok": True, "was": oq, "now": pos["qty"],
                                             "why": "進場那張單其實成交了 %d 口（部位回報慢）—— 這幾口是我們的，"
                                                    "收盤照樣平；⚠️ 停利單只掛了 %d 口" % (pos["qty"], oq)})
                else:
                    cur["touched"] = True
                    _log("position_touched", {"ok": False, "was": oq, "now": pos["qty"],
                                              "why": "券商口數比我們開的多（他自己加碼？）—— 收盤不幫他平，"
                                                     "停損只平我們剩下的 %d 口" % _ours_left(cur)})
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
    return _state["position"]


# ---------------------------------------------------------------- 下單

def qty_ok(q):
    """一張單的口數合不合理：整數（⛔ 不收 bool／小數／字串）、1 ≤ q ≤ QTY_MAX。"""
    return isinstance(q, int) and not isinstance(q, bool) and 1 <= q <= QTY_MAX


def _own_qty(pos):
    """
    這一口部位裡「我們自己開的」有幾口（平倉每一張單最多送這麼多）。
    `entry_qty` 只有 enter() 寫、或撿回部位時從帳本補（多口才補）；沒有 ⇒ QTY（＝ 10-14 以前的行為：
    平倉每張 1 口、最多 CLOSE_TRIES 張）。
    """
    v = (pos or {}).get("entry_qty")
    return v if qty_ok(v) else QTY


def _order(action, price, price_type, order_type, octype, qty=None):
    import shioaji as sj
    q = QTY if qty is None else qty
    if not qty_ok(q):
        # ⛔ 口數看不懂／超過上限 ⇒ 組不出單（呼叫端的 _send 不會被叫到）
        raise ValueError("口數不合理（%r），不送" % (q,))
    kw = {"action": action, "price": price, "quantity": q,
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
    return _wait_fill_n(direction, 1)[0]


PARTIAL_STABLE = 2     # 多口：部分成交時，「部位口數＝委託單成交口數」要再連續看到幾次才定案（每次間隔 0.4 秒）


def _dealt_qty(trade):
    """委託單自己的成交口數（問券商一次 update_status）；問不到 ⇒ None。⛔ 不拿鎖。"""
    if trade is None or not is_live() or _state["api"] is None:
        return None
    _update_status()
    s = _deal_sums(trade)
    return None if s is None else s[1]


def _wait_fill_n(direction, want, trade=None):
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
    ok, err, res = _send(
        _order(act, 0, sj.FuturesPriceType.MKP, sj.OrderType.IOC, sj.FuturesOCType.New,
               qty=want),
        "entry")
    if not ok:
        return False, err, None

    try:
        fill, got, fill_warn = _wait_fill_n(direction, want, res if want > 1 else None)
    except RuntimeError as e:
        # 方向不符：券商那邊有東西但不是我們送的方向。停手、不掛停利、
        # **不要清掉部位**（清掉畫面會顯示空手，但帳上其實有東西）。
        _log("entry_mismatch", {"ok": False, "err": str(e)})
        return False, str(e), None
    if is_live() and fill is None:
        # IOC 沒成交。**絕對不可以掛停利** —— 那張 Cover 單留在場上，
        # 成交之後就變成一個反向的新部位。
        # （tp_points=None 那一口本來就不掛停利，這一段照樣要走 —— 沒成交就沒有部位。）
        _log("entry_nofill", {"ok": False, "why": "IOC 沒有成交，不掛停利"})
        with _lock:
            _state["position"] = None
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
        _state["position"] = {"dir": direction, "entry": entry, "qty": k,
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
    if want > 1 and is_live():
        _meta_sync()                    # ⭐ QA r2 必修 4：多口部位一成立就落地（重啟後補得回來）；一口的不寫
    if want > 1 and is_live():
        # ⭐ 多口：記下「進場那一刻券商已實現裡已經有的列」（之後對帳要排除）。
        #    QA r6：放在掛停利單**之前** ⇒ 停利單就算立刻成交，那一列也不會被誤記成「進場前就有」。一口的路不叫。
        _real_snapshot()
    if tp_points is None:
        # ⛔⛔ **一次 place_target 都不呼叫**（選項 A）。券商端這一口沒有任何掛單。
        return True, None, _state["position"]
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


def place_target(tp_price, qty=None):
    """
    把停利掛成券商端的限價單。做多→賣出，做空→買回。
    ⭐ 口數 ＝ 這一口部位**實際成交**的口數（`entry_qty`，enter() 寫的；部分成交就是 k 口）。
    """
    import shioaji as sj
    pos = _state["position"]
    if pos is None:
        return False, "沒有部位"
    act = sj.Action.Sell if pos["dir"] == "long" else sj.Action.Buy
    ok, err, res = _send(
        _order(act, round(float(tp_price)), sj.FuturesPriceType.LMT,
               sj.OrderType.ROD, sj.FuturesOCType.Cover,
               qty=(qty if qty is not None else _own_qty(pos))),
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
        return False, "沒有部位"
    if pos.get("dir") not in ("long", "short"):
        return False, "看不出部位方向，不敢送平倉單 —— 請自己到大戶投平倉"
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
    _CLOSE_INFLIGHT["on"] = True
    for attempt in range(CLOSE_TRIES):
        ok, err, res = _send(
            _order(act, 0, sj.FuturesPriceType.MKP, sj.OrderType.IOC,
                   sj.FuturesOCType.Cover, qty=to_close - done),
            "close_" + reason)
        last_err = err
        if not ok:
            break
        trades.append(res)
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
        pos["has_target"] = _state["position"].get("target_trade") is not None
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
