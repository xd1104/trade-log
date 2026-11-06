# -*- coding: utf-8 -*-
"""
小台（MXF）在永豐**模擬帳戶**實際送單（2026-11-06）。做多、做空各一輪，再一輪微台 4 口當對照。
⛔ 安全：自己建連線、寫死 simulation=True；紀錄寫 sim_orders/；小台開關用暫存檔（⛔ 不碰真的 MINI_ON／REAL_ORDERS_ON）。
   每一輪結束確認空手，沒平乾淨就中止。
驗：10 口等值 ⇒ 送 2 口小台、券商部位是小台 2 口（＝等值 10）、停損平倉送小台、平完空手。
跑法：.venv\\Scripts\\python.exe tools\\shioaji\\test_mini_sim.py （要在開盤時間）
"""
import datetime
import json
import pathlib
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import shioaji as sj              # noqa: E402
from _config import get_credentials   # noqa: E402
import broker                     # noqa: E402

FAIL = []


def chk(name, got, want):
    ok = got == want
    print(("  OK   " if ok else "  FAIL ") + name + ("" if ok else "  (得到 %r，期待 %r)" % (got, want)))
    if not ok:
        FAIL.append(name)


def head(t):
    print("\n" + "=" * 60 + "\n" + t + "\n" + "=" * 60)


head("① 模擬帳戶登入（simulation=True）")
api_key, secret = get_credentials()
api = sj.Shioaji(simulation=True)          # ← 安全性的核心，不要改
api.login(api_key=api_key, secret_key=secret)


def front(prod):
    cs = [c for c in getattr(api.Contracts.Futures, prod)
          if not c.code.startswith(prod + "R") and getattr(c, "delivery_month", "")]
    cs.sort(key=lambda c: c.delivery_month)
    return cs


tmf = front("TMF")[0]
mxf = next((c for c in front("MXF") if c.delivery_month == tmf.delivery_month), None)
print("  微台 %s　小台 %s（交割月 %s）" % (tmf.code, getattr(mxf, "code", None), tmf.delivery_month))
chk("小台合約跟微台同一個交割月", getattr(mxf, "delivery_month", None), tmf.delivery_month)
try:
    ref = api.snapshots([tmf])[0].close
except Exception:
    ref = 0

acc = broker.configure(api, tmf, {"MXF": mxf})
broker.ORDER_DIR = HERE / "sim_orders"
broker.TRADE_DIR = pathlib.Path(tempfile.mkdtemp(prefix="mini-sim-trades-"))
broker.MINI_FLAG = pathlib.Path(tempfile.mkdtemp(prefix="mini-sim-")) / "MINI_ON"
broker.MINI_FLAG.write_text("sim", encoding="utf-8")
broker.is_live = lambda: True               # 這支就是要真的呼叫 place_order（模擬帳戶）
if acc is None:
    print("⛔ 沒挑到期貨帳號，停")
    sys.exit(1)
chk("開關（暫存檔）＋小台合約 ⇒ mini_on", broker.mini_on(), True)


def marks():
    f = broker.ORDER_DIR / ("%s.jsonl" % datetime.date.today())
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()] if f.exists() else []


def wait_flat(stage):
    for _ in range(10):
        time.sleep(1)
        if broker.broker_position() is None:
            return True
    print("⛔ %s 之後還有部位：%r —— 中止，請到模擬帳戶確認" % (stage, broker.broker_position()))
    try:
        api.logout()
    except Exception:
        pass
    sys.exit(1)


def run(direction, qty, want_prod, want_n):
    zh = "做多" if direction == "long" else "做空"
    head("%s %d 口等值（期待 %d 口%s）" % (zh, qty, want_n, "小台" if want_prod == "MXF" else "微台"))
    wait_flat("開始前")
    n0 = len(marks())
    ok, err, pos = broker.enter(direction, ref or 0, None, sl_points=300, qty=qty)
    chk("  進場成功", ok, True)
    if not ok:
        print("   錯誤：", err)
        return
    ent = next((r for r in marks()[n0:] if r["kind"] == "entry"), {})
    chk("  送單合約／口數", (ent.get("product"), ent.get("qty")), (want_prod, want_n))
    # ⚠️ 模擬環境常常只撮合 1 口、其餘 IOC 取消（11-06 實跑：2 口小台只成交 1 口）⇒ 接受部分成交，
    #    驗的是「成交了幾口合約 × 比例＝部位等值」、而且本機跟券商一致。
    r = broker.RATIO[want_prod]
    pq = (pos or {}).get("qty") or 0
    chk("  本機部位：同一種合約、等值是比例的倍數、不超過送出量",
        ((pos or {}).get("product"), pq % r == 0 and 0 < pq <= qty), (want_prod, True))
    time.sleep(2)
    bp = broker.broker_position()
    chk("  券商部位跟本機一致（換算成等值）", ((bp or {}).get("product"), (bp or {}).get("qty"), (bp or {}).get("dir")),
        (want_prod, pq, direction))
    if pq < qty:
        print("   ⚠️ 部分成交：送 %d 口等值、成交 %d 口等值（模擬環境撮合量少）" % (qty, pq))
    print("   成交價 %s" % (pos or {}).get("entry"))
    n1 = len(marks())
    ok2, err2 = broker.close("sl")[:2]
    chk("  停損平倉送得出去", ok2, True)
    if not ok2:
        print("   錯誤：", err2)
    cl = [r for r in marks()[n1:] if str(r.get("octype", "")).endswith("Cover")]
    chk("  平倉單送到同一種合約", sorted({r.get("product") for r in cl}), [want_prod])
    wait_flat(zh)
    chk("  平完空手", broker.broker_position(), None)


run("long", 10, "MXF", 2)
run("short", 10, "MXF", 2)
run("long", 4, "TMF", 4)

try:
    api.logout()
except Exception:
    pass
print("\n總結：" + ("全部通過 ✅" if not FAIL else "%d 項沒過 ❌：%s" % (len(FAIL), "；".join(FAIL))))
print("以上全部是模擬帳戶，沒有任何真錢異動。")
sys.exit(1 if FAIL else 0)
