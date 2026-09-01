# -*- coding: utf-8 -*-
"""
用永豐的**模擬帳戶**把「真的送出去」那一段跑過一次。**做多與做空各一輪。**

================================================================
為什麼一定要兩個方向都跑
================================================================
2026-09-01：`broker_position()` 的方向判斷寫成
    "long" if ("buy" in d or qty > 0) else "short"
`qty > 0` 對任何部位都成立 ⇒ **永遠回報做多**。做多時每一步都對，
所以只測做多的模擬跑了兩輪、全綠、什麼都沒抓到。
Benson 第一筆真單做空就中：誤判沒成交、沒掛停利、畫面顯示反向、
按平倉又送出一張同方向的單。

**這一整支測試的存在理由就是那次。對稱的東西一定要兩邊都測。**

================================================================
安全性
================================================================
- 這支自己建立連線且**寫死 `simulation=True`**，不接受外部傳進來的 api
  ⇒ 結構上碰不到真實帳戶。
- 紀錄寫 `sim_orders/`，不混進 `real_orders/`。
- 不建立、也不讀 `REAL_ORDERS_ON`。
- 每一輪結束都會確認「空手」才進下一輪；沒平乾淨就中止，不會愈積愈多。

怎麼跑（PowerShell）：
    & "C:\\Users\\USER\\Desktop\\Claude Work\\trade-log\\.venv\\Scripts\\python.exe" `
      "C:\\Users\\USER\\Desktop\\Claude Work\\trade-log\\tools\\shioaji\\test_broker_sim.py"
"""
import datetime
import json
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import shioaji as sj
from _config import get_credentials
import broker

FAIL = 0
TP = 100


def head(t):
    print("\n" + "=" * 60 + f"\n{t}\n" + "=" * 60)


def chk(name, got, want):
    global FAIL
    ok = got == want
    FAIL += not ok
    print(("  OK   " if ok else "  FAIL ") + name + ("" if ok else f"  (得到 {got!r}，期待 {want!r})"))


def note(name, value):
    print(f"  ·      {name}: {value}")


head("① 用模擬帳戶登入（simulation=True，寫死在程式裡）")
api_key, secret = get_credentials()
api = sj.Shioaji(simulation=True)          # ← 安全性的核心，不要改
accounts = api.login(api_key=api_key, secret_key=secret)
print(f"  拿到 {len(accounts or [])} 個帳號")

head("② 挑當月合約（用成交量，跟面板同一套）")
cat = getattr(api.Contracts.Futures, "TMF")
cands = [c for c in cat if not c.code.startswith("TMFR") and getattr(c, "delivery_month", "")]
cands.sort(key=lambda c: c.delivery_month)
near = cands[:3]
try:
    snaps = api.snapshots(near)
    contract = max(zip(near, snaps), key=lambda p: p[1].total_volume or 0)[0]
    ref = [s for c, s in zip(near, snaps) if c is contract][0].close
except Exception as e:
    contract, ref = near[0], None
    print("  抓快照失敗（模擬環境常見）：", str(e)[:80])
note("合約", f"{contract.code}　參考價 {ref}")

acc = broker.configure(api, contract)
broker.ORDER_DIR = HERE / "sim_orders"
broker.is_live = lambda: True               # 這支就是要真的呼叫 place_order
if acc is None:
    print("\n  ⛔ 沒挑到期貨帳號，停在這裡（不會亂送單）")
    sys.exit(1)
note("期貨帳號", getattr(acc, "account_id", "?"))


def marks():
    """這一輪新產生的紀錄。"""
    f = broker.ORDER_DIR / f"{datetime.date.today()}.jsonl"
    if not f.exists():
        return []
    return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]


def flat_or_die(stage):
    time.sleep(2)
    pos = broker.broker_position()
    if pos not in (None,):
        print(f"\n  ⛔ {stage} 之後還有部位：{pos}")
        print("     中止，不再送任何單。請自己到模擬帳戶確認。")
        try:
            api.logout()
        except Exception:
            pass
        sys.exit(1)


def run(direction):
    """跑一輪：進場 → 檢查停利 → 平倉 → 檢查空手。"""
    zh = "做多" if direction == "long" else "做空"
    want_entry_act = "Action.Buy" if direction == "long" else "Action.Sell"
    want_cover_act = "Action.Sell" if direction == "long" else "Action.Buy"

    head(f"③ {zh}：送出進場單")
    before = len(marks())
    ok, err, pos = broker.enter(direction, ref or 0, TP)
    chk(f"  {zh}進場成功", ok, True)
    if not ok:
        print("     錯誤：", err)
        return
    time.sleep(2)

    new = marks()[before:]
    entry = next((r for r in new if r["kind"] == "entry"), None)
    target = next((r for r in new if r["kind"] == "target"), None)
    chk("  進場單方向正確", (entry or {}).get("action"), want_entry_act)
    chk("  進場是新倉", (entry or {}).get("octype"), "FuturesOCType.New")

    head(f"④ {zh}：對帳 —— 方向會不會被讀錯（09-01 就是這裡出事）")
    bp = broker.broker_position()
    chk("  券商回報的方向", (bp or {}).get("dir"), direction)
    fill = (bp or {}).get("entry")
    note("實際成交價", fill)

    head(f"⑤ {zh}：停利掛在成交價 {'+' if direction == 'long' else '−'}{TP}")
    chk("  停利單方向正確（跟進場相反）", (target or {}).get("action"), want_cover_act)
    chk("  停利是平倉單", (target or {}).get("octype"), "FuturesOCType.Cover")
    want_tp = (fill + TP) if direction == "long" else (fill - TP)
    chk(f"  停利價 = 成交價 {'+' if direction == 'long' else '−'}{TP}",
        (target or {}).get("price"), float(want_tp))

    head(f"⑥ {zh}：停損觸發 —— 模擬價格穿過停損，看它送不送得出去")
    # 直接呼叫 broker.close("sl")：面板的觸發判斷已經有離線測試守著
    # （test_stop_trigger.py），這裡要驗的是「決定要停損之後，單真的送得出去、
    # 而且方向是對的」—— 那一段從來沒有對著券商跑過。
    before2 = len(marks())
    ok2, err2 = broker.close("sl")
    chk("  停損平倉送得出去", ok2, True)
    if not ok2:
        print("     錯誤：", err2)
    new2 = marks()[before2:]
    closed = next((r for r in new2 if r["kind"].startswith("close_")), None)
    chk("  平倉方向正確（跟部位相反）", (closed or {}).get("action"), want_cover_act)
    # ⚠️ 不可以只檢查「有沒有出現 cancel_target」—— 撤失敗寫的是同一個字。
    #    這一項要守的正是「殘留停利單變成反向新倉」，只看名字等於沒守
    #    （lab-qa 2026-09-01 指出，同一件事 test_broker.py 有檢查 ok、這裡沒有）。
    cx = next((r for r in new2 if r["kind"] == "cancel_target"), None)
    chk("  有先撤掉還掛著的停利單，而且**真的撤掉了**", (cx or {}).get("ok"), True)

    head(f"⑦ {zh}：平完應該空手")
    time.sleep(2)
    chk("  券商那邊沒有部位了", broker.broker_position(), None)
    flat_or_die(f"{zh}那一輪")


run("long")
run("short")

head("⑧ 這一輪的完整紀錄")
today = datetime.date.today()
for r in marks():
    if r["ts"][:10] != str(today):
        continue
    print(f"  {r['ts'][11:]} {r['kind']:14} {str(r.get('action')):12} "
          f"{r.get('price')} {str(r.get('octype') or '')} ok={r.get('ok')}")

try:
    api.logout()
except Exception:
    pass
print("\n總結:", "全部通過" if not FAIL else f"{FAIL} 項失敗")
print("以上全部是模擬帳戶，沒有任何真錢異動。")
sys.exit(1 if FAIL else 0)
