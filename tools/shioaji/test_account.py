# -*- coding: utf-8 -*-
"""
【帳戶總覽】（2026-09-21 Benson 交辦）的探針 —— 帳戶還剩多少錢那張卡。

⛔⛔ 這一支在守的五件事：
   ① **唯讀**：整場 `broker.enter` / `broker.close` 一次都不准被呼叫，
      而且假的 api 物件只允許 `margin()` 被叫（叫別的就記一筆、收尾斷言 0 次）。
   ② **問不到就留白**：查詢失敗／欄位看不懂／狀態不是 Fetched ⇒ `ok:False` ＋ 一句話，
      ⛔ 上一次的金額**不准**留在畫面上（`m` 要被清掉）。
   ③ **不准猜一口要壓多少保證金**：沒學到 ⇒ 照實說不知道；
      學是「剛好 1 口」時從券商的原始保證金學，⛔ 不寫死數字。
   ④ **一天一列**：14:00 以前不落地（日盤還沒收完）；同一天不寫第二次。
   ⑤ **送單那幾刻不問**（09:03:30／09:15／13:43:30 前後 10 秒）—— 連線留給送單。
⛔ 不連永豐、不碰他真的資料夾：所有路徑導到暫存區。

怎麼跑（PowerShell）：
    $env:PYTHONIOENCODING="utf-8"; $env:PYTHONUTF8="1"
    & "…\\trade-log\\.venv\\Scripts\\python.exe" "…\\tools\\shioaji\\test_account.py"
"""
import json
import pathlib
import shutil
import sys
import tempfile
from datetime import date, datetime

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import broker                       # noqa: E402
import live_panel as LP             # noqa: E402

FAIL = 0
CALLS = []


def say(cond, name, extra=""):
    global FAIL
    FAIL += not cond
    print(("  OK   " if cond else "  FAIL ") + name + (f"  {extra}" if extra else ""))


def chk(name, got, want):
    global FAIL
    ok = got == want
    FAIL += not ok
    print(("  OK   " if ok else "  FAIL ") + name
          + ("" if ok else f"  (得到 {got!r}，期待 {want!r})"))


def _boom(kind, err, tb):
    import traceback
    traceback.print_exception(kind, err, tb)
    print("  FAIL ⛔ 測試自己掛掉了（未捕捉的例外）：" + str(err)[:120])
    print("⛔ 有 ? 項沒過（測試中斷）")
    sys.stdout.flush()


sys.excepthook = _boom

if broker.REAL_FLAG.exists():
    print("  FAIL ⛔ tools/shioaji/REAL_ORDERS_ON 存在，拒絕往下跑")
    sys.exit(1)

TMP = pathlib.Path(tempfile.mkdtemp(prefix="acct-"))
LP.EQUITY_DIR = TMP / "equity"
broker.REAL_FLAG = TMP / "REAL_ORDERS_ON"
broker.ORDER_DIR = TMP / "real_orders"
broker.TRADE_DIR = TMP / "real_trades"
broker.enter = lambda *a, **k: (CALLS.append("enter") or (False, "測試治具", None))
broker.close = lambda *a, **k: (CALLS.append("close") or (False, "測試治具"))


class FakeMargin:
    """永豐 `Margin` 的替身（欄位名照 shioaji._core.Margin，⛔ 不改名）。"""

    def __init__(self, **kw):
        self.equity_amount = 250000.0
        self.equity = 250000.0
        self.available_margin = 180000.0
        self.initial_margin = 0.0
        self.maintenance_margin = 0.0
        self.margin_call = 0.0
        self.risk_indicator = 999.0
        self.future_open_position = 0.0
        self.today_future_open_position = 0.0
        self.future_settle_profitloss = 0.0
        self.deposit_withdrawal = 0.0
        self.fee = 0.0
        self.tax = 0.0
        self.today_balance = 250000.0
        self.yesterday_balance = 250000.0
        self.order_margin_premium = 0.0
        self.status = "Fetched"
        for k, v in kw.items():
            setattr(self, k, v)


class FakeApi:
    """⛔ 只准 `margin()` 被叫；叫別的就記一筆（收尾斷言 0 次）。"""

    def __init__(self, ret=None, boom=None):
        self.ret, self.boom, self.n = ret, boom, 0

    def margin(self, acc=None):
        self.n += 1
        if self.boom:
            raise RuntimeError(self.boom)
        return self.ret

    def __getattr__(self, name):
        CALLS.append("api." + name)
        raise AttributeError(name)


def wire(api):
    broker._state["api"] = api
    broker._state["account"] = object()
    broker._state["position"] = None


def reset_eq():
    LP.EQUITY.update({"at": None, "m": None, "err": "還在問", "lot1": None, "lot1_at": None})
    LP._EQ_HIST["key"] = None


print("=== ① 問得到：數字端得出來，而且 api 只被叫一次 ===")
api = FakeApi(FakeMargin())
wire(api)
reset_eq()
m, err = broker.account_margin()
chk("  沒有錯誤", err, None)
chk("  權益總值", m.get("equity_amount"), 250000.0)
chk("  可動用", m.get("available_margin"), 180000.0)
chk("  ⛔ api.margin 只被叫 1 次", api.n, 1)

print("\n=== ② ⛔ 問不到就是問不到（⛔ 不猜、⛔ 不留舊數字）===")
wire(FakeApi(boom="連線逾時"))
m2, err2 = broker.account_margin()
say(m2 is None and "連線逾時" in (err2 or ""), "  查詢爆掉 ⇒ (None, 一句話)", str(err2))
wire(FakeApi(FakeMargin(status="Fetching")))
m3, err3 = broker.account_margin()
say(m3 is None and err3, "  狀態不是 Fetched ⇒ 不當成問到", str(err3))
broker._state["api"] = None
m4, err4 = broker.account_margin()
say(m4 is None and "永豐" in (err4 or ""), "  還沒連上永豐 ⇒ 說清楚", str(err4))

print("\n=== ③ 畫面那一份：問不到的時候不准有金額 ===")
reset_eq()
LP.EQUITY.update({"m": None, "err": "問不到帳戶餘額：連線逾時"})
v = LP.equity_view(datetime(2026, 9, 21, 15, 0))
chk("  ok=False", v.get("ok"), False)
say("equity" not in v, "  ⛔ 整份裡面一個金額欄位都沒有", str(sorted(v))[:70])
say("連線逾時" in v.get("err", ""), "  而且講得出為什麼", v.get("err"))

print("\n=== ④ 「錢夠不夠下一口」：沒學到就照實說不知道 ===")
reset_eq()
LP.EQUITY.update({"m": dict(FakeMargin().__dict__), "err": None, "at": "15:00:00"})
LP.EQUITY["m"].pop("status", None)
v = LP.equity_view(datetime(2026, 9, 21, 15, 0))
chk("  還不知道一口要多少 ⇒ ok 是 None（不是 False）", v["enough"]["ok"], None)
say("還不知道" in v["enough"]["msg"], "  訊息照實說", v["enough"]["msg"])

print("\n=== ⑤ 學「一口要壓多少」：只在剛好 1 口的時候學 ===")
reset_eq()
broker._state["position"] = {"qty": 2, "dir": "long"}
LP._equity_lot1({"initial_margin": 26000.0})
chk("  2 口 ⇒ ⛔ 不學（會學成兩倍）", LP.EQUITY["lot1"], None)
broker._state["position"] = {"qty": 1, "dir": "long"}
LP._equity_lot1({"initial_margin": 13000.0})
chk("  1 口 ⇒ 學起來", LP.EQUITY["lot1"], 13000.0)
LP._equity_lot1({"initial_margin": 0.0})
chk("  ⛔ 沒有部位時的 0 不准覆蓋掉學過的", LP.EQUITY["lot1"], 13000.0)
LP.EQUITY.update({"m": dict(FakeMargin(available_margin=180000.0).__dict__), "err": None})
LP.EQUITY["m"].pop("status", None)
v = LP.equity_view(datetime(2026, 9, 21, 15, 0))
say(v["enough"]["ok"] is True and "夠" in v["enough"]["msg"], "  可動用 180,000 ⇒ 夠",
    v["enough"]["msg"])
LP.EQUITY["m"]["available_margin"] = 9000.0
v = LP.equity_view(datetime(2026, 9, 21, 15, 0))
say(v["enough"]["ok"] is False and "不夠" in v["enough"]["msg"],
    "  可動用 9,000 ⇒ ⚠️ 不夠，而且要講「會送失敗」", v["enough"]["msg"])

print("\n=== ⑥ 一天一列：14:00 以前不寫、同一天不寫第二次 ===")
reset_eq()
mm = dict(FakeMargin().__dict__)
mm.pop("status", None)
early = datetime(2026, 9, 21, 9, 30)
late = datetime(2026, 9, 21, 14, 5)
chk("  09:30 ⇒ ⛔ 不落地", LP._equity_write_day(mm, early), False)
chk("  14:05 ⇒ 落地", LP._equity_write_day(mm, late), True)
chk("  同一天再叫一次 ⇒ 不重複寫", LP._equity_write_day(mm, late), False)
rows = LP.equity_hist_read()
chk("  歷史剛好 1 列", len(rows), 1)
chk("  那一列是今天的權益", rows[0]["equity"], 250000.0)
say((LP.EQUITY_DIR / "2026.jsonl").exists(), "  檔名按年份分")

print("\n=== ⑦ 本月變化：⛔ 出入金要扣掉（匯錢進去不是賺到）===")
reset_eq()
LP.EQUITY_DIR.mkdir(parents=True, exist_ok=True)
(LP.EQUITY_DIR / "2026.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in [
    {"date": "2026-09-01", "equity": 200000.0, "deposit": 0.0},
    {"date": "2026-09-10", "equity": 205000.0, "deposit": 0.0},
    {"date": "2026-09-18", "equity": 305000.0, "deposit": 100000.0},
]), encoding="utf-8")
LP._EQ_HIST["key"] = None
LP.EQUITY.update({"m": dict(mm, equity_amount=308000.0), "err": None})
v = LP.equity_view(datetime(2026, 9, 21, 15, 0))
mo = v["month"]
chk("  帳戶帳面變化", mo["change"], 108000.0)
chk("  其中出入金", mo["deposit"], 100000.0)
chk("  ⭐ 真正賺到的（扣掉出入金）", mo["net"], 8000.0)

print("\n=== ⑧ 今日損益 ＝ 未平倉 ＋ 平倉 − 成本 ===")
reset_eq()
LP.EQUITY.update({"m": dict(mm, future_open_position=1200.0,
                            future_settle_profitloss=800.0, fee=60.0, tax=6.0),
                  "err": None})
v = LP.equity_view(datetime(2026, 9, 21, 15, 0))
chk("  成本 ＝ 手續費＋稅", v["cost"], 66.0)
chk("  今日損益", v["day_pl"], 1934.0)

print("\n=== ⑨ ⛔ 送單那幾刻不去問券商 ===")
for hh, mm_, ss, want, name in ((9, 3, 30, True, "09:03:30 快攻送單"),
                                (9, 3, 25, True, "09:03:25（前 5 秒）"),
                                (9, 15, 0, True, "09:15 純回馬"),
                                (13, 43, 30, True, "13:43:30 收盤平倉"),
                                (9, 5, 0, False, "09:05（沒事）"),
                                (11, 0, 0, False, "11:00（沒事）")):
    chk("  %s" % name, LP._eq_quiet(datetime(2026, 9, 21, hh, mm_, ss)), want)

print("\n=== ⑩ ⛔ 收尾 ===")
chk("  ⛔ broker.enter / broker.close 全程 0 次", [c for c in CALLS if "." not in c], [])
chk("  ⛔ 假的 api 只被叫過 margin()", [c for c in CALLS if c.startswith("api.")], [])
say(str(TMP) in str(LP.EQUITY_DIR), "  權益紀錄全程都在暫存區", str(LP.EQUITY_DIR))
say(not (HERE / "equity").exists(),
    "  ⛔⛔ 真的 tools/shioaji/equity/ 沒有被這支測試建出來")
say("tools/shioaji/equity/" in (HERE.parent.parent / ".gitignore").read_text(encoding="utf-8"),
    "  ⛔⛔ equity/ 有在 .gitignore 裡（真實金額不上傳）")

shutil.rmtree(TMP, ignore_errors=True)
print("\n" + ("全部通過 ✅" if FAIL == 0 else f"⛔ 有 {FAIL} 項沒過"))
sys.exit(1 if FAIL else 0)
