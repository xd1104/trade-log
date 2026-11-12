# -*- coding: utf-8 -*-
"""
【帳戶總覽】的底層：問券商帳戶、期交所一口保證金、每天一列權益紀錄、夠不夠下一口、權益緩衝、
券商流量、資料備份與 SOXX 補抓的觸發（2026-11-12 健檢整理第 5 批，從 live_panel.py 原樣搬出來）。

⛔⛔ 這一整支**唯讀**：只叫 broker.account_margin()／held_position()（只讀記憶體），⛔ 一張單都不送。
⛔⛔ 真實金額不上傳：equity/、usage/ 都在 gitignore。
⛔ 不准 import live_panel（面板是用 `python live_panel.py` 跑的 ⇒ 再 import 一次會變成兩份狀態）。
   面板的東西由 live_panel 在載入時接上：PRODUCT、price_now（現價）、api_now（永豐連線）。
   沒接上 ⇒ 現價當不知道、不問流量（安全方向：照實說判不出來、不去碰券商）。
   留在 live_panel 的：背景那條 poll_equity（要拿 state_lock 寫 STATE）、目標卡、金流明細、鈴鐺。
"""
import json
import os
import sys
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

import pandas as pd

import auto_fire
import broker
import lots
import market_clock as MC
import night_fire
import risk_cap
from panel_depth import DEPTH_MAX_PCT, DEPTH_SUBS, depth_pause, depth_view, night_moments

try:                                    # 跟 live_panel 同一個理由：純計算模組壞了不可以把這支帶掉
    import goal
except Exception:                       # noqa: BLE001
    goal = None

HERE = Path(__file__).parent
PRODUCT = "TMF"            # live_panel 載入時會用它自己的 PRODUCT 蓋過去（同一個值）


def price_now():
    """live_panel 載入時換成「Today 的現價」。⛔ 沒接上 ⇒ None（權益緩衝照實說判不出來）。"""
    return None


def api_now():
    """live_panel 載入時換成「現在的永豐連線」。⛔ 沒接上 ⇒ None（不問流量）。"""
    return None


# ---------------------------------------------------------------- 【帳戶總覽】
#
# ⭐⭐ 2026-09-21 Benson 要的：「我的期貨帳戶裡面剩下多少錢」，外加三件事
#    ①「錢夠不夠明天那一口」②「今天賺賠多少（券商端的數字）」③ 每天記一個權益數字畫成曲線。
#
# ⛔⛔ **這一整段唯讀**：只叫 `broker.account_margin()`（`api.margin()`），⛔ 一張單都不送。
# ⛔⛔ **真實金額不上傳**：每天那一列寫在 `tools/shioaji/equity/`（已 gitignore），
#    跟 `real_orders/`、`real_trades/` 同一條鐵律 —— repo 是公開的。
# ⚠️ **這是券商端的真相，跟面板自己算的點數不是同一件事**（手續費、稅、其他部位都算進去）。
#    ⇒ 畫面上一律標「券商端」，⛔ 不可以拿它去對策略績效（那會得到兩個都對但不一樣的數字）。
# ⚠️ 節奏：盤中 60 秒一次、其餘 10 分鐘一次，而且 ⛔ **送單那幾刻前後 10 秒不問**
#    （09:03:30／09:15／13:43:30）—— 那幾秒的連線全部留給送單。

EQUITY_DIR = HERE / "equity"            # ⛔ gitignore（真實金額只留在這台電腦）
EQUITY_EVERY = 60.0                     # 盤中多久問一次
EQUITY_EVERY_OFF = 600.0                # 非盤中
EQUITY_BUSY_END = pd.Timestamp("14:30").time()   # 問得密一點的時段結束（日盤收完再留 45 分）
EQUITY_QUIET_S = 10                     # 送單那幾刻前後幾秒不問
EQUITY_WRITE_AFTER = 14 * 3600          # 每天幾點之後才把當天那一列落地（日盤收完）
EQUITY_HIST_MAX = 120                   # 曲線最多畫幾天
EQUITY = {"at": None, "m": None, "err": "還在問券商…（每分鐘更新一次）",
          "lot1": None, "lot1_at": None,
          "tfx": None, "tfx_at": None, "tfx_day": None, "tfx_try": 0.0, "tfx_err": None}
_EQ_HIST = {"key": None, "rows": []}

# ⭐ 2026-09-29（Benson：「下不下得了一口為甚麼判斷不出來？」）
#    「從自己帳戶學一口保證金」上線以來**一次都沒學到**（equity/ 每一列 lot1 都是 null）⇒ 畫面永遠「判不出來」。
#    改成先問**期交所公告的原始保證金**（官方一覽表，一天問一次、⛔ 不寫死數字）；問不到才退回學到的那個。
TAIFEX_MARGIN_URL = "https://www.taifex.com.tw/cht/5/indexMarging"
TAIFEX_NAME = {"TMF": "微型臺指期貨", "MXF": "小型臺指期貨", "TXF": "臺股期貨"}


def parse_taifex_margin(html, product):
    """期交所保證金一覽表 ⇒ (原始保證金, 維持保證金, 更新日期字串)；找不到 ⇒ None。⛔ 數字不合理也當找不到。"""
    import re
    name = TAIFEX_NAME.get(product)
    if not name:
        return None
    m = re.search(r"<td>\s*%s\s*</td>\s*<td[^>]*>\s*([\d,]+)\s*</td>\s*<td[^>]*>\s*([\d,]+)\s*</td>"
                  r"\s*<td[^>]*>\s*([\d,]+)\s*</td>" % re.escape(name), html)
    if not m:
        return None
    keep, init = float(m.group(2).replace(",", "")), float(m.group(3).replace(",", ""))
    if not (1000 <= keep <= init <= 5_000_000):
        return None
    d = re.search(r"更新日期：\s*(\d{4}/\d{2}/\d{2})", html)
    return init, keep, (d.group(1) if d else None)


def _taifex_lot1_tick(now):
    """一天問一次期交所（失敗 1 小時後再試）。⚠️ 在 poll_equity 那條執行緒；⛔ 壞了只記錯、不影響任何事。"""
    today = str(now.date())
    if EQUITY["tfx_day"] == today or time.time() - EQUITY["tfx_try"] < 3600:
        return
    EQUITY["tfx_try"] = time.time()
    try:
        req = urllib.request.Request(TAIFEX_MARGIN_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode("utf-8", "replace")
        got = parse_taifex_margin(html, PRODUCT)
        if not got:
            EQUITY["tfx_err"] = "期交所的表格看不懂（找不到%s）" % TAIFEX_NAME.get(PRODUCT, PRODUCT)
            return
        EQUITY.update(tfx=got[0], tfx_keep=got[1], tfx_at=got[2], tfx_day=today, tfx_err=None)
    except Exception as e:
        EQUITY["tfx_err"] = "問不到期交所：%s" % str(e)[:80]


def _lot1():
    """一口要多少原始保證金 ⇒ (金額, 從哪來的一句話)；都不知道 ⇒ (None, None)。期交所公告優先。"""
    if EQUITY.get("tfx"):
        return EQUITY["tfx"], "期交所公告" + ("（%s 起）" % EQUITY["tfx_at"][5:] if EQUITY.get("tfx_at") else "")
    if EQUITY.get("lot1"):
        return EQUITY["lot1"], "從帳戶學到（%s）" % (EQUITY.get("lot1_at") or "?")
    return None, None


def _eq_quiet(now):
    """現在是不是「送單那幾刻」⇒ ⛔ 不去問帳戶（連線留給送單）。"""
    sec = now.hour * 3600 + now.minute * 60 + now.second
    pts = [MC.sec(t) for t in MC.ORDER_MOMENTS]      # ⭐ 11-12 改讀正本（多了夜盤 ≥ 5 口的 04:50）
    # ⭐ 2026-11-05 健檢：原本只避開日盤三刻 ⇒ 夜盤送單（冬令 22:35／22:40）、04:58 夜盤平倉、
    #    結算日 13:28:30 都會跟送單搶同一條永豐連線。夜盤兩刻跟 night_fire 問（冬夏令自動換）。
    try:
        pts += [m.hour * 3600 + m.minute * 60 + m.second for m in night_moments(now)]
    except Exception:                     # noqa: BLE001
        pts += [21 * 3600 + 35 * 60, 21 * 3600 + 40 * 60, 22 * 3600 + 35 * 60, 22 * 3600 + 40 * 60]
    for t in pts:
        if t is not None and abs(sec - t) <= EQUITY_QUIET_S:
            return True
    return False


def equity_hist_read():
    """
    讀 `equity/*.jsonl`（一天一列，舊到新）。⚠️ 唯讀、`(mtime,size)` 快取。
    ⛔ 壞列跳過就好：這是給畫面看的曲線，⛔ 不可以因為一列壞掉就整段不顯示。
    """
    try:
        files = sorted(EQUITY_DIR.glob("*.jsonl"))
    except OSError:
        return []
    key = tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in files)
    if _EQ_HIST["key"] == key:
        return list(_EQ_HIST["rows"])
    rows, seen = [], set()
    for p in files:
        try:
            txt = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in txt.splitlines():
            if not line.strip():
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            d = o.get("date") if isinstance(o, dict) else None
            if not isinstance(d, str) or d in seen:
                continue
            seen.add(d)
            rows.append(o)
    rows.sort(key=lambda r: r["date"])
    _EQ_HIST.update(key=key, rows=rows)
    return list(rows)


def _equity_write_day(m, now):
    """
    把今天那一列落地（一天一列、只 append）。⇒ 有沒有真的寫。
    ⛔ 14:00 以前不寫（日盤還沒收完，寫下去的是半路的數字）。
    ⛔ 今天已經有一列就不再寫（⛔ 不覆蓋、不改舊列）。
    """
    if (now.hour * 3600 + now.minute * 60 + now.second) < EQUITY_WRITE_AFTER:
        return False
    today = str(now.date())
    if any(r.get("date") == today for r in equity_hist_read()):
        return False
    row = {"date": today, "at": now.strftime("%H:%M:%S"),
           "equity": m.get("equity_amount"), "avail": m.get("available_margin"),
           "deposit": m.get("deposit_withdrawal"),
           "settle_pl": m.get("future_settle_profitloss"),
           "float_pl": m.get("future_open_position"),
           "fee": m.get("fee"), "tax": m.get("tax"),
           "lot1": EQUITY.get("lot1"), "lot1_tfx": EQUITY.get("tfx")}
    try:
        EQUITY_DIR.mkdir(parents=True, exist_ok=True)
        with (EQUITY_DIR / (today[:4] + ".jsonl")).open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        _EQ_HIST["key"] = None          # ⛔ 快取要作廢，不然畫面到明天才看得到這一列
        return True
    except Exception as e:
        EQUITY["err"] = "今天那一列寫不進去：%s" % str(e)[:100]
        return False


def _equity_lot1(m):
    """
    「一口要壓多少保證金」—— **從他自己的帳戶學**：有部位的時候原始保證金是多少就記多少。
    ⛔⛔ 不准寫死一個數字（微台的保證金期交所會調，寫死的那天起畫面就是錯的），
       ⛔ 也不准拿沒有部位時的 0 當答案 ⇒ 沒學到就是**不知道**（畫面照實說）。
    ⚠️ 只在「剛好 1 口」的時候學（他就是只下 1 口；多口會學成 2 倍）。
    """
    im = m.get("initial_margin")
    if not isinstance(im, (int, float)) or im <= 0:
        return
    pos = broker.held_position()
    qty = int((pos or {}).get("qty") or 0)
    if qty != 1:
        return
    EQUITY["lot1"] = float(im)
    EQUITY["lot1_at"] = str(date.today())


def _equity_lot1_restore():
    """
    開機時把「上次學到的一口保證金」從落地的紀錄裡讀回來（最新那一列有值的）。
    ⛔ 讀不到就維持 None ＝ **不知道**（畫面照實說），⛔ 不猜一個數字。
    """
    try:
        for r in reversed(equity_hist_read()):
            v = r.get("lot1")
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                EQUITY["lot1"], EQUITY["lot1_at"] = float(v), r.get("date")
                return True
    except Exception:
        pass
    return False


def _equity_month(rows, m, now):
    """這個月帳戶變了多少（⛔ 扣掉出入金 —— 匯錢進去不是賺到）。⇒ dict 或 None。
    ⭐ 2026-10-07（Benson：「本月增加金額是不是算錯了？」—— 對）：舊版拿**這個月第一列**當月初，
       漏掉那一天自己的賺賠（10-01 +2,364），而且券商漏寫的入金（09-30 傍晚 1.3 萬）也扣不到。
       ⇒ 改成跟目標卡**同一套**（goal.months_actual：月初＝上個月最後一列、出入金用 eff_deposit），⛔ 不再各算各的。
       goal 模組沒載入才退回舊算法。"""
    mth = str(now.date())[:7]
    now_eq = m.get("equity_amount")
    if goal is not None and isinstance(now_eq, (int, float)):
        dep = m.get("deposit_withdrawal") if isinstance(m.get("deposit_withdrawal"), (int, float)) else 0.0
        cur = [x for x in goal.months_actual(rows, now_eq, dep, now.date(), live=m) if x["ym"] == mth]
        if not cur:
            return None
        x = cur[0]
        return {"from": x["from"] or str(now.date()), "n": x.get("n", 0), "base": float(x["base"]),
                "now": float(now_eq), "change": round(float(now_eq) - float(x["base"]), 1),
                "deposit": float(x["dep"]), "net": float(x["net"])}
    mine = [r for r in rows if str(r.get("date", ""))[:7] == mth
            and isinstance(r.get("equity"), (int, float))]
    now_eq = m.get("equity_amount")
    if not mine or not isinstance(now_eq, (int, float)):
        return None
    base = float(mine[0]["equity"])
    dep = sum(float(r.get("deposit") or 0) for r in mine)
    return {"from": mine[0]["date"], "n": len(mine), "base": base,
            "now": float(now_eq), "change": round(float(now_eq) - base, 1),
            "deposit": round(dep, 1),
            "net": round(float(now_eq) - base - dep, 1)}


def equity_view(now=None):
    """
    【帳戶總覽】那張卡要的一份（⚠️ 唯讀）。⇒ dict；⛔ 問不到就 `ok:False` ＋ 一句話
    （⛔ 不留上一次的數字在畫面上假裝是現在的）。
    """
    now = now or datetime.now()
    m, err = EQUITY.get("m"), EQUITY.get("err")
    if not m:
        return {"ok": False, "err": err or "還沒問到帳戶餘額", "at": EQUITY.get("at")}
    rows = equity_hist_read()
    lot1, lot1_src = _lot1()
    avail = m.get("available_margin")
    # 「錢夠不夠下一口」⛔ 不知道一口要多少 ⇒ 照實說不知道（⛔ 不猜一個數字）
    if not isinstance(avail, (int, float)):
        enough = {"ok": None, "why": "no_avail", "msg": "券商沒給可動用保證金 —— 判不出夠不夠"}
    elif not lot1:
        enough = {"ok": None, "why": "no_lot1",
                  "msg": "還不知道一口要壓多少保證金（%s）" % (EQUITY.get("tfx_err") or "還在問期交所")}
    else:
        # ⭐ 2026-10-14（Benson：有部位時一直跳「不夠，下一口會被退單」）：手上那一口正壓著保證金，
        #    可動用當然少一口 —— 而規則一次只開一口，下一口一定是平倉之後才送。
        #    ⇒ 有部位時改看「平倉後約可動用」＝ 可動用 ＋ 正在壓的原始保證金（⛔ 不是現在就能再開一口）。
        held = m.get("initial_margin")
        holding = isinstance(held, (int, float)) and held > 0
        after = float(avail) + (float(held) if holding else 0.0)
        # ⭐ 2026-10-14 多口：要壓的保證金 ＝ 一口 × max(a, b)（日夜盤部位不重疊；lots.margin_lots）。
        #    1＋1 ⇒ n＝1 ⇒ 底下每一個數字、每一句話都跟以前一模一樣。
        try:
            n = int(lots.margin_lots())
        except Exception:                      # noqa: BLE001  ⛔ 讀不到口數 ⇒ 照 1 口算（跟送單那邊同一個退路）
            n = 1
        need = float(lot1) * n
        ok = after >= need
        unit = "一口要" if n == 1 else ("%d 口要" % n)
        nxt = "下一口" if n == 1 else ("下一筆（%d 口）" % n)
        enough = {"ok": ok, "why": "holding" if holding else None, "need": need, "avail": float(avail),
                  "since": EQUITY.get("tfx_at") or EQUITY.get("lot1_at"), "src": lot1_src}
        if n != 1:
            enough.update(lots=n, lot1=lot1)
        if holding:
            enough["after"] = round(after, 1)
            enough["msg"] = ("現在有部位壓著 %s；平倉後約可動用 %s／%s %s（%s）—— %s"
                             % (format(int(round(held)), ","), format(int(round(after)), ","),
                                unit, format(int(round(need)), ","), lot1_src,
                                ("平倉後夠" + nxt) if ok else ("⚠️ 平倉後也不夠，%s會被券商退單" % nxt)))
        else:
            enough["msg"] = ("可動用 %s／%s %s（%s）—— %s"
                             % (format(int(round(avail)), ","), unit, format(int(round(need)), ","), lot1_src,
                                ("夠" + nxt) if ok else ("⚠️ 不夠，%s會被券商退單" % nxt)))
    fee = float(m.get("fee") or 0) + float(m.get("tax") or 0)
    pl = None
    if isinstance(m.get("future_open_position"), (int, float)) \
            and isinstance(m.get("future_settle_profitloss"), (int, float)):
        pl = round(float(m["future_open_position"]) + float(m["future_settle_profitloss"])
                   - fee, 1)
    return {"ok": True, "err": None, "at": EQUITY.get("at"),
            "equity": m.get("equity_amount"), "avail": avail,
            "float_pl": m.get("future_open_position"),
            "settle_pl": m.get("future_settle_profitloss"),
            "cost": round(fee, 1), "day_pl": pl,
            "risk": m.get("risk_indicator"), "margin_call": m.get("margin_call"),
            "deposit": m.get("deposit_withdrawal"),
            "enough": enough, "cushion": equity_cushion(m.get("equity_amount"), lot1),
            "usage": usage_view(), "depth": depth_view(), "backup": backup_view(now),
            "month": _equity_month(rows, m, now),
            "hist_n": len(rows)}


USAGE_DIR = HERE / "usage"             # ⛔ gitignore
USAGE_EVERY = 600.0
USAGE = {"at_ts": 0.0, "bytes": None, "limit": None, "conn": None, "at": None, "err": None}


def _usage_poll(now):
    """
    ⭐ 2026-09-26（Benson：要開始自己收集資料，**先確認券商流量上限**）。
    `api.usage()` ⇒ 今天用了多少、上限多少。⚠️ **唯讀**、10 分鐘一次、在 poll_equity 那條執行緒裡
    （已經避開送單那幾刻）。每次落地一列到 `usage/YYYY-MM.jsonl`，量一兩天就知道「現在的訂閱一天吃多少」，
    才決定能不能多錄五檔／現貨／其他期貨。
    """
    if time.time() - USAGE["at_ts"] < USAGE_EVERY:
        return
    USAGE["at_ts"] = time.time()
    api = api_now()
    if api is None:
        return
    try:
        u = api.usage(timeout=5000)
        b = float(getattr(u, "bytes", 0) or 0)
        lim = float(getattr(u, "limit_bytes", 0) or 0)
        conn = getattr(u, "connections", None)
        USAGE.update(bytes=b, limit=lim, conn=conn, at=now.strftime("%H:%M"), err=None)
        USAGE_DIR.mkdir(parents=True, exist_ok=True)
        with (USAGE_DIR / (now.strftime("%Y-%m") + ".jsonl")).open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": now.isoformat(timespec="seconds"), "bytes": b, "limit": lim,
                                "conn": conn}, ensure_ascii=False) + "\n")
        # ⭐ 研究用五檔吃太多流量 ⇒ 今天先退訂大台與選擇權（微台是真單合約，不碰）
        if lim and b / lim * 100 > DEPTH_MAX_PCT and DEPTH_SUBS["contracts"]:
            depth_pause(api, "券商流量已用 %.0f%%" % (b / lim * 100))
    except Exception as e:
        USAGE["err"] = "問不到流量：%s" % str(e)[:100]


BACKUP_STATUS = HERE / "backup" / "status.json"      # ⛔ gitignore；backup_data.py 寫、這裡只讀
BACKUP_FROM = dtime(5, 30)                          # 夜盤 04:58 已平、日盤 08:45 才開 ⇒ 一定沒有部位、網路空著
BACKUP_UNTIL = dtime(8, 0)
_BK = {"spawned": None}


def _backup_read():
    try:
        return json.loads(BACKUP_STATUS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _backup_tick(now):
    """
    ⭐ 2026-09-26（Benson：研究資料與歷史資料要自動備份到私人 repo trade-data）。
    每天 05:30~08:00 之間，今天還沒跑過 ⇒ **另開** `backup_data.py`（最低優先權、沒有視窗）。
    ⛔ 面板不等它、不讀它的輸出、不做任何上傳（停損迴圈不可以被拖住）。
    """
    if not (BACKUP_FROM <= now.time() < BACKUP_UNTIL):
        return
    today = str(now.date())
    if _BK["spawned"] == today:
        return
    st = _backup_read()
    if str(st.get("started") or "")[:10] == today:
        _BK["spawned"] = today
        return
    _BK["spawned"] = today
    import subprocess                      # ⛔ 模組最上面沒有 import（sync_to_cloud 也是在函式裡 import）
    (HERE / "backup").mkdir(exist_ok=True)
    logf = open(HERE / "backup" / "last-run.log", "w", encoding="utf-8")
    flags = 0x08000000 | 0x00004000 if os.name == "nt" else 0      # CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS
    subprocess.Popen([sys.executable, str(HERE / "backup_data.py")], cwd=str(HERE),
                     stdout=logf, stderr=subprocess.STDOUT, creationflags=flags,
                     env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    print("[備份] %s 已另開備份程式（最低優先權）" % now.strftime("%H:%M"), flush=True)


_SOXX = {"day": None}


def _soxx_tick(now):
    """
    ⭐ 2026-09-30【健檢】第四張卡（美股半導體 vs 台指夜盤）的美股資料：每天 05:30~08:00 補抓一次 SOXX 5 分 K
    （us_feed 的 Alpaca 管道、⛔ 只讀行情），存 us_bars/SOXX-sip-YYYY-MM.csv。
    ⛔ 另開執行緒、面板不等它；抓不到只是那張卡停在舊日期（卡上的「資料到」會照實寫）。
    """
    if not (BACKUP_FROM <= now.time() < BACKUP_UNTIL) or _SOXX["day"] == str(now.date()):
        return
    _SOXX["day"] = str(now.date())

    def run():
        try:
            import us_feed
            cur = now.strftime("%Y-%m")
            prv = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
            for ym in (prv, cur):
                us_feed.ensure_month("SOXX", "sip", ym)
        except Exception as e:                 # noqa: BLE001  ⛔ 看的東西，壞了不影響任何事
            print("⚠️ 【健檢】SOXX 補抓失敗：%s" % str(e)[:100], flush=True)
    threading.Thread(target=run, name="soxx-fetch", daemon=True).start()


def backup_view(now=None):
    """給【帳戶總覽】與手機監控：最後一次備份的結果。超過 2 天沒成功 ⇒ warn。"""
    now = now or datetime.now()
    st = _backup_read()
    if not st:
        return {"ok": None, "warn": False, "msg": "資料備份：還沒跑過（每天 05:30 自動跑）"}
    at = str(st.get("at") or st.get("started") or "")
    if st.get("running"):
        # ⭐ 2026-11-05 健檢：被砍掉／電腦重開 ⇒ 狀態會一直停在 running。正常一輪約 2 分鐘，超過 3 小時就是中斷了。
        try:
            run_h = (now - datetime.fromisoformat(str(st.get("started") or at))).total_seconds() / 3600
        except Exception:
            run_h = 0
        if run_h > 3:
            return {"ok": False, "warn": True,
                    "msg": "⚠️ 資料備份 %s 開始後就沒有結束（可能中斷了），等明天 05:30 重跑" % at[5:16].replace("T", " ")}
        return {"ok": None, "warn": False, "msg": "資料備份：正在跑（%s 開始）" % at[5:16].replace("T", " ")}
    try:
        age_d = (now - datetime.fromisoformat(at)).total_seconds() / 86400
    except Exception:
        age_d = 99
    if st.get("ok") and st.get("n_skipped"):
        # ⭐ 2026-11-05 健檢：有檔太大被跳過 ⇒ 不能再說「成功」
        sk = st.get("skipped") or []
        return {"ok": False, "warn": True, "msg": "⚠️ 資料備份 %s 有 %d 個檔太大沒備份到（%s）" % (
            at[5:16].replace("T", " "), st["n_skipped"], "、".join(sk[:3]))}
    if st.get("ok"):
        return {"ok": True, "warn": age_d > 2, "msg": "資料備份：%s 成功（推上 %s 個檔）%s" % (
            at[5:16].replace("T", " "), st.get("pushed_files", 0), "　⚠️ 已經超過 2 天沒有新的備份" if age_d > 2 else "")}
    return {"ok": False, "warn": True, "msg": "⚠️ 資料備份失敗（%s）：%s" % (at[5:16].replace("T", " "), str(st.get("err") or "")[:80])}


def usage_view():
    if USAGE.get("bytes") is None:
        return {"ok": False, "msg": USAGE.get("err") or "還沒問到券商流量（10 分鐘問一次）"}
    b, lim = USAGE["bytes"], USAGE["limit"] or 0
    return {"ok": True, "mb": round(b / 1e6, 1), "limit_mb": round(lim / 1e6), "at": USAGE["at"],
            "pct": round(b / lim * 100, 1) if lim else None,
            "msg": "券商流量：今天 %.0f MB／上限 %s MB（%s 更新）" % (
                b / 1e6, format(int(round(lim / 1e6)), ",") if lim else "?", USAGE["at"])}


def equity_cushion(eq, lot1, px=None, day_on=None, night_on=None):
    """
    ⭐ 2026-09-24（Benson：不夠就補錢 ⇒ 前提是**不夠的時候他要知道**）。
    權益離「一口保證金」還剩多少，夠不夠再吃一次停損。⇒ dict（`warn` True 就要亮）。
      停損距離：開著的那幾條裡最大的那個（夜盤 2%、日盤 0.5%，× 現價）。
      ⛔ 學不到一口保證金／沒有現價／兩條都關著 ⇒ 照實說判不出來，⛔ 不猜。
    建議本金 ＝ 保證金 ＋ 風控 B 下的最大回落 × 2 × 每點金額（`risk_cap.DD_PER_LOT`）。
    """
    if not isinstance(eq, (int, float)) or not lot1:
        return {"warn": None, "msg": ""}
    if px is None:
        px = price_now()
    try:
        if day_on is None:
            day_on = bool(auto_fire.arm().get("on"))
        if night_on is None:
            night_on = bool(night_fire.arm().get("on"))
    except Exception:
        day_on, night_on = True, True
    # ⭐ 2026-10-14 多口（QA 建議：口數問 lots）：不是 1＋1 ⇒ 保證金 × max(a, b)、一次停損 × 那一條的口數。
    #    ⛔ 1＋1（含 lots 讀不到）⇒ 走下面原本那一段，一個字都不差。
    try:
        la, lb = lots.base_lots()
    except Exception:                                  # noqa: BLE001
        la, lb = 1, 1
    if (la, lb) != (1, 1):
        return _equity_cushion_multi(eq, lot1, px, day_on, night_on, la, lb)
    rec = float(lot1) + 2 * risk_cap.DD_PER_LOT * risk_cap.PT_NTD
    cush = float(eq) - float(lot1)
    out = {"cushion": round(cush), "pts": int(cush // risk_cap.PT_NTD), "rec": int(round(rec, -3)),
           "topup": max(0, int(round(rec - float(eq), -3)))}
    if not isinstance(px, (int, float)) or px <= 0 or not (day_on or night_on):
        out.update(warn=None, msg="權益比一口保證金多 %s 元（約 %d 點）" % (
            format(int(round(cush)), ","), out["pts"]))
        return out
    stop = round(px * (0.02 if night_on else 0.005))
    out["stop_pts"] = stop
    out["warn"] = out["pts"] < stop
    if out["warn"]:
        out["msg"] = ("⚠️ 權益只比一口保證金多 %s 元（約 %d 點），比一次停損（約 %d 點）還少 —— "
                      "再停損一次，下一筆自動單會因為保證金不夠被券商退掉。建議本金每口約 %s 元（還差約 %s 元）"
                      % (format(int(round(cush)), ","), out["pts"], stop,
                         format(out["rec"], ","), format(out["topup"], ",")))
    else:
        out["msg"] = ("權益比一口保證金多 %s 元（約 %d 點），撐得住一次停損（約 %d 點）"
                      % (format(int(round(cush)), ","), out["pts"], stop))
    return out


def _equity_cushion_multi(eq, lot1, px, day_on, night_on, a, b):
    """多口版的 equity_cushion：保證金 ＝ 一口 × max(a, b)；一次停損 ＝ 開著的那幾條裡（每口停損點數 × 口數）最大的那個。"""
    n = max(a, b, 1)
    margin = float(lot1) * n
    rec = margin + 2 * risk_cap.DD_PER_LOT * risk_cap.PT_NTD * n
    cush = float(eq) - margin
    out = {"cushion": round(cush), "pts": int(cush // risk_cap.PT_NTD), "rec": int(round(rec, -3)),
           "topup": max(0, int(round(rec - float(eq), -3))), "lots": n}
    if not isinstance(px, (int, float)) or px <= 0 or not (day_on or night_on):
        out.update(warn=None, msg="權益比 %d 口保證金多 %s 元（約 %d 點）" % (
            n, format(int(round(cush)), ","), out["pts"]))
        return out
    stop = max(round(px * 0.02) * b if night_on else 0, round(px * 0.005) * a if day_on else 0)
    out["stop_pts"] = stop
    out["warn"] = out["pts"] < stop
    if out["warn"]:
        out["msg"] = ("⚠️ 權益只比 %d 口保證金多 %s 元（約 %d 點），比一次停損（%s，約 %d 點）還少 —— "
                      "再停損一次，下一筆自動單會因為保證金不夠被券商退掉。建議本金約 %s 元（還差約 %s 元）"
                      % (n, format(int(round(cush)), ","), out["pts"], "%d＋%d 口" % (a, b), stop,
                         format(out["rec"], ","), format(out["topup"], ",")))
    else:
        out["msg"] = ("權益比 %d 口保證金多 %s 元（約 %d 點），撐得住一次停損（%d＋%d 口，約 %d 點）"
                      % (n, format(int(round(cush)), ","), out["pts"], a, b, stop))
    return out
