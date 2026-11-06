# -*- coding: utf-8 -*-
"""
多口平倉：部位回報慢的時候不可以平過頭（2026-11-06 QA 在 main 重現：回報停在舊資料 ≥ 4.8 秒
⇒ 舊版整筆再送一次 ⇒ 反向全口數，還回報「平倉成功」）。
⛔ 離線：假券商＋假時鐘（_fake_broker_harness.py，QA 寫的那一套），不連永豐、不碰 8770、開關檔全導到暫存區。
驗：① 回報慢 0～10 秒、微台 10 口／小台 2 口 ⇒ 每一張平倉單口數都不超過當下持有、最後空手；
    ② 第一張真的沒撮到（IOC 取消、明細 0）⇒ 照樣送第二張、平乾淨（⛔ 不能因為修平過頭就變成平不掉）；
    ③ 第一張成交了但明細晚回報＋部位也慢 ⇒ 不送第二張、回報「稍後再試」，之後對帳空手。
跑法：.venv\\Scripts\\python.exe -X utf8 tools\\shioaji\\test_close_multi_lag.py
"""
import os
import pathlib
import sys

_D = pathlib.Path(__file__).resolve().parent
os.environ["BROKER_DIR"] = str(_D)
exec(open(_D / "_fake_broker_harness.py", encoding="utf-8").read())

print("=== ① 回報慢也不平過頭 ===")
for mini, qty in ((False, 10), (True, 10), (False, 2)):
    for lag in (0, 3.0, 4.7, 4.9, 6.0, 10.0):
        F = fresh(mini=mini)
        ok, err, p = broker.enter("long", 38000, 60, sl_points=200, qty=qty)
        assert ok, err
        F.lag = lag
        F.mkt = {"TMF": 37801.0, "MXF": 37800.0}
        ok, err = broker.close("sl")
        name = "%s %d 口等值、回報慢 %s 秒" % ("小台" if mini else "微台", qty, lag)
        chk(name + "：沒有平過頭", F.bad, [])
        chk(name + "：券商最後空手", F.hold, {"TMF": 0, "MXF": 0})

print("\n=== ② 第一張真的沒撮到 ⇒ 照樣送第二張 ===")
for lag in (0, 6.0):
    F = fresh(mini=False)
    broker.enter("long", 38000, 60, sl_points=200, qty=10)
    n0 = len(F.sent)
    cnt = {"n": 0}

    def pol(p, rec, cnt=cnt):
        if rec["oc"] == "Cover" and rec["pt"] != "LMT":
            cnt["n"] += 1
            if cnt["n"] == 1:
                return 0, F.mkt[p]            # 第一張 IOC 一口都沒撮到
        return rec["q"], F.mkt[p]
    F.policy = pol
    F.lag = lag
    ok, err = broker.close("sl")
    covers = [x for x in S(F, n0) if x[3] == "Cover" and x[4] != "LMT"]
    chk("回報慢 %s 秒：送了兩張平倉單（第一張沒撮到）" % lag, len(covers), 2)
    chk("回報慢 %s 秒：沒有平過頭" % lag, F.bad, [])
    chk("回報慢 %s 秒：券商最後空手" % lag, F.hold, {"TMF": 0, "MXF": 0})
    chk("回報慢 %s 秒：回報成功" % lag, ok, True)

print("\n=== ③ 成交了但明細晚回報＋部位也慢 ⇒ 不送第二張 ===")
import random as _r  # noqa: E402
F = fresh(mini=False)
broker.enter("long", 38000, 60, sl_points=200, qty=10)
n0 = len(F.sent)
F.lag = 6.0
F.hide, F.rng = (1.0, 3), _r.Random(1)
ok, err = broker.close("sl")
covers = [x for x in S(F, n0) if x[3] == "Cover" and x[4] != "LMT"]
chk("只送一張", len(covers), 1)
chk("沒有平過頭", F.bad, [])
chk("回報「稍後再試」而不是成功", (ok, "還沒回報" in (err or "")), (False, True))
cool()
CLK.t += 30
chk("之後對帳：空手", broker.reconcile(), None)

print("\n全部通過 ✅" if not FAIL else "\n%d 項沒過 ❌" % len(FAIL))
sys.exit(1 if FAIL else 0)
