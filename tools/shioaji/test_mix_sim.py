# -*- coding: utf-8 -*-
"""
混搭（小台＋微台同一筆部位）在永豐**模擬帳戶**實際送單（2026-11-06）。
⛔ 安全：自己建連線、寫死 simulation=True；紀錄寫 sim_orders/；開關用暫存檔（⛔ 不碰真的 MINI_ON／REAL_ORDERS_ON）；
   落地檔／成績單導到暫存資料夾；每一輪結束確認兩種合約都空手，沒平乾淨就中止。
⚠️ 模擬環境每張單常常只撮合 1 口、其餘 IOC 取消 ⇒ 驗「實際成交多少、部位就記多少、平倉只平那麼多、最後空手」。
輪次：
  A 做多 11 口等值（送 2 小台＋1 微台）→ 停損平倉
  B 做空 13 口等值（送 2 小台＋3 微台）→ 停損平倉
  C 做多 7 口等值 → 模擬「面板重啟」（清掉本機部位）→ 對帳撿回 → 停損平倉
  D 做多 11 口等值、掛停利（tp 遠）→ 停損：停利單要先撤、平乾淨、券商上不留掛單
跑法：.venv\\Scripts\\python.exe -X utf8 tools\\shioaji\\test_mix_sim.py （開盤時間）
"""
import datetime
import json
import pathlib
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import shioaji as sj                 # noqa: E402
from _config import get_credentials  # noqa: E402
import broker                        # noqa: E402

FAIL = []


def chk(name, got, want):
    ok = got == want
    print(("  OK   " if ok else "  FAIL ") + name + ("" if ok else "  (得到 %r，期待 %r)" % (got, want)))
    if not ok:
        FAIL.append(name)


def head(t):
    print("\n" + "=" * 60 + "\n" + t + "\n" + "=" * 60)


head("登入模擬帳戶（simulation=True）")
k, s = get_credentials()
api = sj.Shioaji(simulation=True)          # ← 安全性的核心，不要改
api.login(api_key=k, secret_key=s)


def front(prod):
    cs = [c for c in getattr(api.Contracts.Futures, prod)
          if not c.code.startswith(prod + "R") and getattr(c, "delivery_month", "")]
    cs.sort(key=lambda c: c.delivery_month)
    return cs


tmf = front("TMF")[0]
mxf = next((c for c in front("MXF") if c.delivery_month == tmf.delivery_month), None)
print("  微台 %s　小台 %s" % (tmf.code, getattr(mxf, "code", None)))
TMP = pathlib.Path(tempfile.mkdtemp(prefix="mix-sim-"))
broker.ORDER_DIR = HERE / "sim_orders"
broker.TRADE_DIR = TMP / "trades"
broker.MINI_FLAG = TMP / "MINI_ON"
broker.MINI_FLAG.write_text("sim", encoding="utf-8")
broker.is_live = lambda: True
acc = broker.configure(api, tmf, {"MXF": mxf})
if acc is None:
    print("⛔ 沒挑到期貨帳號")
    sys.exit(1)
try:
    ref = api.snapshots([tmf])[0].close
except Exception:
    ref = 0


def legs():
    out = {}
    for p in api.list_positions(api.futopt_account) or []:
        q = int(getattr(p, "quantity", 0) or 0)
        if q and p.code in (tmf.code, mxf.code):
            out[p.code] = (str(p.direction), q)
    return out


def open_orders():
    api.update_status(api.futopt_account)
    n = 0
    for t in api.list_trades():
        st = str(getattr(t.status, "status", ""))
        if getattr(t.contract, "code", None) in (tmf.code, mxf.code) and ("Submitted" in st or "PartFilled" in st):
            n += 1
    return n


def flat_or_die(stage):
    for _ in range(12):
        time.sleep(1)
        if not legs():
            return
    print("⛔ %s 之後券商還有部位 %r —— 中止，請到模擬帳戶確認" % (stage, legs()))
    sys.exit(1)


def check_pos(pos, sent_eq, tag):
    bp = broker.broker_position()
    print("   本機部位 %s　券商 %s" % ({k: (pos or {}).get(k) for k in ("product", "qty", "mix", "entry")}, legs()))
    q = (pos or {}).get("qty") or 0
    chk(tag + "：成交口數 > 0 且不超過送出", 0 < q <= sent_eq, True)
    chk(tag + "：本機與券商等值口數一致", (bp or {}).get("qty") if isinstance(bp, dict) else bp, q)


def close_and_check(tag, reason="sl"):
    ok, err = broker.close(reason)[:2]
    for i in range(4):
        if ok or broker._state["position"] is None:
            break
        print("   第 %d 次沒平完：%s ⇒ 等冷卻再平" % (i + 1, err))
        time.sleep(broker.CLOSE_COOLDOWN + 1)
        broker.reconcile()
        ok, err = broker.close(reason)[:2]
    chk(tag + "：平倉成功", ok, True)
    if not ok:
        print("   錯誤：", err)
    flat_or_die(tag)
    chk(tag + "：兩種合約都空手", legs(), {})
    chk(tag + "：本機沒有部位", broker._state["position"], None)


flat_or_die("開始前")

head("A 做多 11 口等值（2 小台＋1 微台）→ 停損")
ok, err, pos = broker.enter("long", ref, None, sl_points=300, qty=11)
chk("A 進場成功", ok, True)
if ok:
    time.sleep(2)
    check_pos(broker._state["position"], 11, "A")
    close_and_check("A")

head("B 做空 13 口等值（2 小台＋3 微台）→ 停損")
ok, err, pos = broker.enter("short", ref, None, sl_points=300, qty=13)
chk("B 進場成功", ok, True)
if ok:
    time.sleep(2)
    check_pos(broker._state["position"], 13, "B")
    close_and_check("B")

head("C 做多 7 口等值 → 面板重啟（清本機）→ 撿回 → 停損")
ok, err, pos = broker.enter("long", ref, None, sl_points=300, qty=7)
chk("C 進場成功", ok, True)
if ok:
    time.sleep(2)
    q0 = broker._state["position"]["qty"]
    with broker._lock:
        broker._state["position"] = None          # 模擬重啟：記憶體清空（落地檔還在）
    got = broker.reconcile()
    print("   撿回：%r" % ({k: got.get(k) for k in ("product", "qty", "mix", "recovered")} if isinstance(got, dict) else got))
    chk("C 撿回口數跟重啟前一樣", (got or {}).get("qty") if isinstance(got, dict) else got, q0)
    close_and_check("C")

head("D 做多 11 口等值、掛停利（很遠）→ 停損：停利單要撤乾淨")
ok, err, pos = broker.enter("long", ref, 2000, sl_points=300, qty=11)
chk("D 進場成功", ok, True)
if ok:
    time.sleep(2)
    print("   掛著的單：%d 張" % open_orders())
    close_and_check("D")
    time.sleep(2)
    chk("D：券商上沒有殘留的停利單", open_orders(), 0)

try:
    api.logout()
except Exception:
    pass
print("\n總結：" + ("全部通過 ✅" if not FAIL else "%d 項沒過 ❌：%s" % (len(FAIL), "；".join(FAIL))))
print("以上全部是模擬帳戶，沒有任何真錢異動。")
sys.exit(1 if FAIL else 0)
