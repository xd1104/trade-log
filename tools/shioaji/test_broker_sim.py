# -*- coding: utf-8 -*-
"""
用永豐的**模擬帳戶**把「真的送出去」那一段跑過一次。

到目前為止 broker.py 的所有測試都停在送出前一步 —— `api.place_order` 這個實際呼叫
一次都沒執行過。不補這一步的話，他的第一筆真單同時也是那段程式的第一次執行。

這支做的事：進場 → 掛停利 → 查部位 → 平倉 → 再查一次，全部走真正的下單流程。

================================================================
安全性
================================================================
- **這支自己建立連線，而且寫死 `simulation=True`。** 它不接受外部傳進來的 api，
  所以在結構上就碰不到真實帳戶。
- 紀錄寫到 `sim_orders/`，不會混進 `real_orders/`。
- 不會建立、也不會讀 `REAL_ORDERS_ON`。
- 口數沿用 broker 的 QTY（1 口）。

怎麼跑（PowerShell）：
    & "C:\\Users\\USER\\Desktop\\Claude Work\\trade-log\\.venv\\Scripts\\python.exe" `
      "C:\\Users\\USER\\Desktop\\Claude Work\\trade-log\\tools\\shioaji\\test_broker_sim.py"

⚠️ 面板同時開著也可以跑（模擬與正式是兩個不同的環境）。萬一面板剛好斷線重連，
   看門狗會自己接回來。
"""
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import shioaji as sj
from _config import get_credentials
import broker


def head(t):
    print("\n" + "=" * 58 + f"\n{t}\n" + "=" * 58)


def show(label, obj):
    print(f"  {label}")
    for k in ("status", "id", "seqno", "ordno", "price", "quantity", "action",
              "code", "direction", "last_price", "pnl"):
        v = getattr(obj, k, None)
        if v is None and hasattr(obj, "__dict__"):
            v = obj.__dict__.get(k)
        if v not in (None, ""):
            print(f"      {k:12} {v}")


head("① 用模擬帳戶登入（simulation=True，寫死在程式裡）")
api_key, secret = get_credentials()
api = sj.Shioaji(simulation=True)          # ← 這一行是安全性的核心，不要改
accounts = api.login(api_key=api_key, secret_key=secret)
print(f"  登入成功，拿到 {len(accounts or [])} 個帳號")
for a in accounts or []:
    print(f"      {getattr(a,'account_type',None)}  {getattr(a,'account_id','')}")

head("② 挑當月合約（用成交量，跟面板同一套）")
cat = getattr(api.Contracts.Futures, broker_product := "TMF")
cands = [c for c in cat if not c.code.startswith("TMFR") and getattr(c, "delivery_month", "")]
cands.sort(key=lambda c: c.delivery_month)
near = cands[:3]
try:
    snaps = api.snapshots(near)
    contract = max(zip(near, snaps), key=lambda p: p[1].total_volume or 0)[0]
    px = [s for c, s in zip(near, snaps) if c is contract][0].close
except Exception as e:
    contract, px = near[0], None
    print("  抓快照失敗（模擬環境常見）：", str(e)[:80])
print(f"  合約 {contract.code}　參考價 {px}")

head("③ 掛上 broker，並讓它真的送出（只在這支程式裡）")
acc = broker.configure(api, contract)
print(f"  期貨帳號 {getattr(acc,'account_id','(沒挑到)')}")
broker.ORDER_DIR = HERE / "sim_orders"        # 不要混進 real_orders/
broker.is_live = lambda: True                 # 這支就是要真的呼叫 place_order
if acc is None:
    print("\n  ⛔ 沒挑到期貨帳號，停在這裡（不會亂送單）")
    sys.exit(1)

head("④ 進場：買進 1 口（範圍市價 IOC）")
ok, err, pos = broker.enter("long", px or 0, 100)
print(f"  結果：{'成功' if ok else '失敗 → ' + str(err)}")
time.sleep(3)

head("⑤ 跟券商對帳：現在有沒有部位")
try:
    for p in api.list_positions(acc) or []:
        show(f"部位 {getattr(p,'code','')}", p)
    print("  broker 看到的：", broker.broker_position())
except Exception as e:
    print("  查不到：", str(e)[:120])

head("⑥ 委託回報（停利單掛上去了嗎）")
try:
    api.update_status(acc)
    ts = api.list_trades()
    print(f"  共 {len(ts)} 筆委託")
    for t in ts[-4:]:
        o, st = getattr(t, "order", None), getattr(t, "status", None)
        print(f"      {getattr(o,'action','')} {getattr(o,'price','')} "
              f"{getattr(o,'quantity','')} 口 → {getattr(st,'status','')}")
except Exception as e:
    print("  查不到：", str(e)[:120])

head("⑦ 平倉")
ok, err = broker.close("sim_test")
print(f"  結果：{'成功' if ok else '失敗 → ' + str(err)}")
time.sleep(3)

head("⑧ 再對帳一次：應該是空手")
try:
    rows = api.list_positions(acc) or []
    print(f"  剩下 {len(rows)} 筆部位")
    for p in rows:
        show(f"部位 {getattr(p,'code','')}", p)
except Exception as e:
    print("  查不到：", str(e)[:120])

head("⑨ 這一輪的紀錄（sim_orders/）")
import datetime, json
f = broker.ORDER_DIR / f"{datetime.date.today()}.jsonl"
if f.exists():
    for line in f.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            print(f"  {r['ts'][11:]} {r['kind']:14} {r.get('action')} {r.get('price')} "
                  f"{r.get('octype')} ok={r.get('ok')} {r.get('err','')}")

try:
    api.logout()
except Exception:
    pass
print("\n完成。以上全部是模擬帳戶，沒有任何真錢異動。")
