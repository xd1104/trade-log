"""
跟永豐的查詢次數（2026-11-27 起）。

【為什麼有這支】永豐期貨 11-27 通知：API 客人查詢太多，每月要 ≥500 口才能用 API；
營業員說「查詢壓到每月 1,500 次以下」可以幫我們談。他說「應該都算」⇒ 登入、訂閱、K 棒、快照、
查部位、查委託、查餘額……**每一次叫券商**都算一次。

⇒ 這支做三件事：
  ① 計數：connect() 一建好 Shioaji 物件就 install(api)，把會連到券商的方法全部包一層，
     每叫一次記一筆（哪個方法、哪個呼叫者），落地到 usage/qcount-YYYY-MM.json（⛔ gitignore 的資料夾）。
  ② 預算：月目標 MONTH_CAP；「可有可無」的查詢（畫面用的指數、餘額、流量……）先問 optional_ok()，
     今天或本月用超過就不問。⛔ 送單／平倉／停損要用的查詢**永遠不擋**（擋了就是不能停損）。
  ③ 示警：本月用到 WARN_AT 這幾個數字各推一次手機。

⛔ 包裝層只做記數：不丟例外、不睡、不拿別人的鎖 —— 包壞了也要讓原本的呼叫照常跑。
⛔ list_trades() 是 SDK 本機的委託清單（不連線）⇒ 另記在 local，不算進總數（⚠️ 未跟永豐確認過）。
"""
import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIR = HERE / "usage"

MONTH_CAP = 1500            # 營業員給的線
MONTH_SOFT = 1200           # 過了這條，可有可無的查詢整個月都停
DAY_SOFT = 60               # 今天（含下單那些必要的）過了這條，可有可無的查詢今天停；月線 MONTH_SOFT 才是主要防線
WARN_AT = (600, 900, 1200, 1400, 1500)

# 會連到券商的方法（Shioaji 物件本身）
API_METHODS = ("login", "logout", "activate_ca", "list_accounts", "fetch_contracts",
               "list_positions", "list_position_detail", "list_profit_loss", "list_profit_loss_detail",
               "list_settlements", "margin", "update_status", "snapshots", "kbars", "ticks",
               "usage", "place_order", "cancel_order", "update_order", "credit_enquires",
               "short_stock_sources", "subscribe_trade", "unsubscribe_trade", "account_balance")
LOCAL_METHODS = ("list_trades",)
# ⭐ 2026-11-27 營業員確認：算進 1,500 次的只有「查部位、查權益數、查平倉」⇒ 總數／額度／推播只看這幾個；
#    其他（登入、訂閱、K 棒、委託狀態、下單…）照記，畫面另列「全部」。
BROKER_COUNTED = ("list_positions", "list_position_detail", "margin", "account_balance",
                  "list_profit_loss", "list_profit_loss_detail", "list_settlements")
QUOTE_METHODS = ("subscribe", "unsubscribe")
ORDER_METHODS = ("place_order", "cancel_order", "update_order")   # 下單（分開看，也算進總數）

# 系統分頁那張「API 次數」卡的分類（billed＝永豐算進 1,500 次的）
GROUPS = (
    ("查部位", True, ("list_positions", "list_position_detail")),
    ("查權益", True, ("margin", "account_balance")),
    ("查平倉", True, ("list_profit_loss", "list_profit_loss_detail", "list_settlements")),
    ("委託狀態", False, ("update_status",)),
    ("下單撤單", False, ("place_order", "cancel_order", "update_order")),
    ("行情查詢", False, ("kbars", "snapshots", "ticks")),
    ("訂閱報價", False, ("quote.subscribe", "quote.unsubscribe")),
    ("登入", False, ("login", "logout", "activate_ca", "list_accounts", "fetch_contracts")),
    ("流量查詢", False, ("usage",)),
)

_L = threading.Lock()
_S = {"month": None, "days": {}, "by": {}, "local": 0, "warned": [], "dirty": False, "saved": 0.0}
PUSH = None                 # live_panel 接上：fn(title, body)；⛔ 預設不推（測試／腳本）
SAVE_EVERY = 30.0


def _month(now=None):
    return (now or datetime.now()).strftime("%Y-%m")


def _path(month):
    return DIR / ("qcount-%s.json" % month)


def _load(month):
    try:
        d = json.loads(_path(month).read_text(encoding="utf-8"))
        return {"month": month, "days": d.get("days") or {}, "by": d.get("by") or {},
                "local": int(d.get("local") or 0), "warned": list(d.get("warned") or [])}
    except Exception:                       # noqa: BLE001
        return {"month": month, "days": {}, "by": {}, "local": 0, "warned": []}


def _roll(now=None):
    """換月（或第一次用）⇒ 讀本月的落地檔接著記。呼叫者要拿著 _L。"""
    m = _month(now)
    if _S["month"] != m:
        _S.update(_load(m), dirty=False, saved=0.0)


def _caller():
    """叫券商的是哪一支函式（跳過這一層）⇒ 'live_panel.poll_index' 這種字串。"""
    try:
        here = str(HERE).replace("\\", "/").lower()
        for fr in reversed(traceback.extract_stack(limit=12)):
            fn = os.path.abspath(fr.filename).replace("\\", "/").lower()   # runpy 跑的檔名是相對路徑
            if fn.startswith(here) and not fn.endswith("/qbudget.py"):
                return "%s.%s" % (Path(fr.filename).stem, fr.name)
    except Exception:                       # noqa: BLE001
        pass
    return "?"


def note(method, now=None, who=None):
    """記一次。⛔ 不丟例外。"""
    try:
        now = now or datetime.now()
        who = who or _caller()
        fire = None
        with _L:
            _roll(now)
            if method in LOCAL_METHODS:
                _S["local"] += 1
            else:
                day = now.strftime("%Y-%m-%d")
                dd = _S["days"].setdefault(day, {})
                dd[method] = int(dd.get(method) or 0) + 1
                k = "%s|%s" % (method, who)
                _S["by"][k] = int(_S["by"].get(k) or 0) + 1
                tot = _month_total_locked()
                for w in WARN_AT:
                    if tot >= w and w not in _S["warned"]:
                        _S["warned"].append(w)
                        fire = (w, tot)
            _S["dirty"] = True
        if fire is not None:
            _warn(*fire)
        maybe_save()
    except Exception:                       # noqa: BLE001
        pass


def _warn(w, tot):
    fn = PUSH
    if fn is None:
        return
    try:
        fn("永豐查詢次數 本月已 %d 次" % tot,
           "目標每月 %d 次以下。超過 %d 後畫面用的查詢會自動停，送單與停損照常。" % (MONTH_CAP, MONTH_SOFT))
    except Exception:                       # noqa: BLE001
        pass


def _month_total_locked(all_=False):
    return sum(sum(int(v) for k, v in d.items() if all_ or k in BROKER_COUNTED) for d in _S["days"].values())


def month_total(now=None):
    with _L:
        _roll(now)
        return _month_total_locked()


def day_total(now=None):
    now = now or datetime.now()
    with _L:
        _roll(now)
        return sum(int(v) for k, v in (_S["days"].get(now.strftime("%Y-%m-%d")) or {}).items() if k in BROKER_COUNTED)


def optional_ok(now=None):
    """可有可無的查詢現在能不能問。⛔ 送單／停損用的查詢不要叫這個（它們永遠要問）。"""
    try:
        return day_total(now) < DAY_SOFT and month_total(now) < MONTH_SOFT
    except Exception:                       # noqa: BLE001
        return True


def save(force=True):
    try:
        with _L:
            if not (_S["dirty"] or force) or _S["month"] is None:
                return
            body = {"month": _S["month"], "days": _S["days"], "by": _S["by"],
                    "local": _S["local"], "warned": _S["warned"],
                    "total": _month_total_locked(), "all": _month_total_locked(True),
                    "saved_at": datetime.now().isoformat(timespec="seconds")}
            _S["dirty"], _S["saved"] = False, time.time()
            month = _S["month"]
        DIR.mkdir(exist_ok=True)
        tmp = _path(month).with_suffix(".tmp")
        tmp.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(_path(month))
    except Exception:                       # noqa: BLE001
        pass


def maybe_save():
    if time.time() - _S["saved"] >= SAVE_EVERY:
        save(force=False)


def view(now=None):
    """給畫面／手機：本月總數、今天、各方法、前幾名呼叫者。"""
    now = now or datetime.now()
    with _L:
        _roll(now)
        tot = _month_total_locked()
        alls = _month_total_locked(True)
        today = {k: v for k, v in (_S["days"].get(now.strftime("%Y-%m-%d")) or {}).items() if k in BROKER_COUNTED}
        meth = {}
        for d in _S["days"].values():
            for k, v in d.items():
                meth[k] = meth.get(k, 0) + int(v)
        top = sorted(_S["by"].items(), key=lambda kv: -kv[1])[:12]
        local = _S["local"]
    today_all = dict(_S_today_all(now))
    groups = []
    seen = set()
    for label, billed, ms in GROUPS:
        seen.update(ms)
        groups.append({"k": label, "billed": billed, "m": sum(int(meth.get(x) or 0) for x in ms),
                       "d": sum(int(today_all.get(x) or 0) for x in ms)})
    other_m = sum(int(v) for k, v in meth.items() if k not in seen)
    if other_m:
        groups.append({"k": "其他", "billed": False, "m": other_m,
                       "d": sum(int(v) for k, v in today_all.items() if k not in seen)})
    return {"month": _month(now), "total": tot, "all": alls, "cap": MONTH_CAP, "today": sum(today.values()),
            "groups": groups,
            "today_by": today, "by_method": meth, "top": top, "local": local,
            "optional_ok": tot < MONTH_SOFT and sum(today.values()) < DAY_SOFT}


def _S_today_all(now):
    with _L:
        return dict(_S["days"].get(now.strftime("%Y-%m-%d")) or {})


def _wrap(obj, name):
    """包 Python 物件上的方法（api.quote 是 Python 物件，可以直接換）。"""
    f = getattr(obj, name, None)
    if f is None or getattr(f, "_qb", False) or not callable(f):
        return False

    def w(*a, **k):
        note(name)
        return f(*a, **k)
    w._qb = True
    w.__wrapped__ = f
    try:
        setattr(obj, name, w)
        return True
    except Exception:                       # noqa: BLE001
        return False


_COUNTED = frozenset(API_METHODS + LOCAL_METHODS)


class _Proxy:
    """代理：名單裡的方法先記一筆再轉過去，其他屬性原樣轉。"""
    __slots__ = ("_o", "_names", "_pre")

    def __init__(self, o, names, pre=""):
        object.__setattr__(self, "_o", o)
        object.__setattr__(self, "_names", names)
        object.__setattr__(self, "_pre", pre)

    def __getattr__(self, n):
        g = object.__getattribute__
        v = getattr(g(self, "_o"), n)
        if n in g(self, "_names") and callable(v):
            key = g(self, "_pre") + n

            def w(*a, **k):
                note(key)
                return v(*a, **k)
            return w
        return v

    def __setattr__(self, n, v):
        setattr(object.__getattribute__(self, "_o"), n, v)

    def __repr__(self):
        return "Counted(%r)" % (object.__getattribute__(self, "_o"),)


# ⭐ K 棒快取（品管 M4：畫面每秒要一次圖，原本今天的 kbars 每 15 分鐘就問一次、週末下週一的空結果也一直重問）
#    到期規則：
#      ・過去的日子、有資料 ⇒ 跨日 KBARS_PAST_SETTLE_S 秒之後抓到的才永久記（午夜剛過抓的可能少最後一根）
#      ・未來的日子（週末畫面指到下週一）⇒ 記到那一天開始
#      ・今天、有資料 ⇒ 記到「下一個盤別邊界（05:00、08:45、13:45、15:00）」和「今晚午夜」較早的那個
#        （品管 N1：夜盤那份不可以撐過午夜——過了午夜它變成「昨天」，K 線圖那條路會永久留著缺了晚上的那份）；
#        之後的分鐘由畫面用即時 tick 補（overlay_live）；斷線重連時 connect() 叫 kbars_reset() 清掉
#      ・今天但空的 ⇒ 開盤前記到下一個邊界；盤中 min(KBARS_EMPTY_TTL 秒, 下一個邊界)
#      ・呼叫端可以 `with qbudget.max_age(秒):` 再壓短（品管 N2：沒有即時 tick 補的路徑，例如 one_min_bars(今天)）
#    ⛔ 開盤補資料（seed_from_bars）一定要新的 ⇒ 包在 `with qbudget.fresh():` 裡就不走快取（結果照樣記住）。
KBARS_EMPTY_TTL = 300.0
KBARS_PAST_SETTLE_S = 600.0
KBARS_BOUNDARIES = ("05:00", "08:45", "13:45", "15:00")
KBARS_KEEP = 64
_KB = {}
_TL = threading.local()


class max_age:
    """`with qbudget.max_age(900):` 裡面叫的 kbars：快取超過這麼多秒就重問（結果照樣記住）。"""

    def __init__(self, s):
        self.s = float(s)

    def __enter__(self):
        self.prev = getattr(_TL, "max_age", None)
        _TL.max_age = self.s if self.prev is None else min(self.prev, self.s)
        return self

    def __exit__(self, *a):
        _TL.max_age = self.prev
        return False


def kbars_reset():
    """斷線重連後叫：今天的 K 棒快取全部作廢（斷線那段可能缺分鐘）。"""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        for k in [k for k in _KB if k[2][:10] >= today]:
            _KB.pop(k, None)
    except Exception:                       # noqa: BLE001
        pass


def _next_boundary(now_dt):
    from datetime import timedelta
    for d in (0, 1):
        day = (now_dt + timedelta(days=d)).date()
        for hm in KBARS_BOUNDARIES:
            t = datetime.strptime("%s %s" % (day, hm), "%Y-%m-%d %H:%M")
            if t > now_dt:
                return t.timestamp()
    return now_dt.timestamp() + 3600


def _kb_expire(key, out, now_dt):
    """⇒ 到期的時間戳（None＝永久）。"""
    end = key[2][:10]
    today = now_dt.strftime("%Y-%m-%d")
    n = len(getattr(out, "ts", []) or [])
    if end > today:
        return datetime.strptime(end, "%Y-%m-%d").timestamp()
    if end < today:
        midnight = datetime.strptime(today, "%Y-%m-%d").timestamp()
        if n and now_dt.timestamp() - midnight >= KBARS_PAST_SETTLE_S:
            return None
        return now_dt.timestamp() + KBARS_EMPTY_TTL
    from datetime import timedelta
    nb = _next_boundary(now_dt)
    midnight = (datetime.strptime(today, "%Y-%m-%d") + timedelta(days=1)).timestamp()
    if not n:
        hm = now_dt.strftime("%H:%M")
        in_session = ("08:45" <= hm < "13:45") or hm >= "15:00" or hm < "05:00"
        return min(nb, midnight) if not in_session else min(nb, midnight, now_dt.timestamp() + KBARS_EMPTY_TTL)
    return min(nb, midnight)


class fresh:
    """`with qbudget.fresh():` 裡面叫的 kbars 一律跟券商要新的（不讀快取，但回來的結果照樣記住）。"""

    def __enter__(self):
        _TL.fresh = getattr(_TL, "fresh", 0) + 1
        return self

    def __exit__(self, *a):
        _TL.fresh = max(0, getattr(_TL, "fresh", 1) - 1)
        return False


def _kb_key(a, k):
    try:
        c = a[0] if a else k.get("contract")
        st = str(k.get("start") if "start" in k else (a[1] if len(a) > 1 else ""))
        en = str(k.get("end") if "end" in k else (a[2] if len(a) > 2 else st))
        extra = tuple(sorted((x, str(v)) for x, v in k.items() if x not in ("contract", "start", "end")))
        return (str(getattr(c, "code", c)), st, en, extra)
    except Exception:                       # noqa: BLE001
        return None


def _kbars(v, a, k, now_dt=None):
    key = _kb_key(a, k)
    now_dt = now_dt or datetime.now()
    now = now_dt.timestamp()
    if key is not None and not getattr(_TL, "fresh", 0):
        hit = _KB.get(key)
        ma = getattr(_TL, "max_age", None)
        if hit is not None and (hit[0] is None or now < hit[0]) and (ma is None or now - hit[2] < ma):
            return hit[1]
    note("kbars")
    out = v(*a, **k)
    if key is not None:
        try:
            _KB[key] = (_kb_expire(key, out, now_dt), out, now)
            if len(_KB) > KBARS_KEEP:
                for kk in sorted(_KB, key=lambda x: (_KB[x][0] or float("inf")))[: len(_KB) - KBARS_KEEP]:
                    _KB.pop(kk, None)
        except Exception:                   # noqa: BLE001
            pass
    return out


class Counted(_Proxy):
    """
    Shioaji 1.x 本體是 Rust 寫的型別（builtins.Shioaji），方法唯讀、換不掉
    ⇒ 包一層代理：會連券商的方法先記一筆再轉過去；api.quote 再包一層（訂閱／退訂也記）；
    其他屬性（Contracts、set_*_callback…）原樣轉。
    ⛔ 面板裡所有地方都要拿**同一個**代理（broker 用 `_state["api"] is api` 比對身分）。
    """
    __slots__ = ()

    def __init__(self, api):
        _Proxy.__init__(self, api, _COUNTED)

    def __getattr__(self, n):
        if n == "quote":
            return _Proxy(getattr(object.__getattribute__(self, "_o"), "quote"), frozenset(QUOTE_METHODS), "quote.")
        if n == "kbars":
            v = getattr(object.__getattribute__(self, "_o"), "kbars")
            return lambda *a, **k: _kbars(v, a, k)
        return _Proxy.__getattr__(self, n)


def raw(api):
    """代理底下的原物件（只給測試／除錯用）。"""
    return object.__getattribute__(api, "_o") if isinstance(api, _Proxy) else api


def install(api):
    """
    ⇒ 包好的 api（Counted 代理）。connect() 一建好 sj.Shioaji() 就換成這個，之後**只用回傳值**。
    ⛔ 不丟例外：包不起來就回原物件（照常連線，只是不記數）。⛔ 這裡不碰 api 的任何屬性（登入前碰會丟 AuthError）。
    """
    if isinstance(api, Counted):
        return api
    try:
        return Counted(api)
    except Exception as e:                  # noqa: BLE001
        print("⚠️ [查詢計數] 包裝失敗（照常連線，只是不記數）：%s" % str(e)[:120], file=sys.stderr)
        return api
