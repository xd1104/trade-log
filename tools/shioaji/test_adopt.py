# -*- coding: utf-8 -*-
"""
2026-11-05 Benson 同意的四項健檢修正（交易行為）—— 離線測試。
⛔ 不連永豐、⛔ 不碰 8770、⛔ 不送單；帳本一律導到暫存資料夾。

① 送單記成失敗、其實成交了 ⇒ 撿回那口時認得出來、接手（日盤 auto_fire／夜盤 night_fire）
③ 夜盤走幅歷史太舊改用交易日（過年長假後第一晚不再被擋）
④ 夜盤那口留過 05:00，白天重啟照夜盤停損顧（⛔ 04:58 已經有定論的那晚不認）
（② 一口平倉送前確認在 test_broker.py）
跑法：.venv\\Scripts\\python.exe tools\\shioaji\\test_adopt.py
"""
import json
import shutil
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import auto_fire as AF      # noqa: E402
import broker               # noqa: E402
import lots                 # noqa: E402
import night_fire as NF     # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="adopt-test-"))
REAL = {"AF": AF.FIRE_DIR, "NF": NF.NF_DIR, "TRADE": broker.TRADE_DIR, "ORDER": broker.ORDER_DIR}
AF.FIRE_DIR = TMP / "autofire"
NF.NF_DIR = TMP / "nightfire"
broker.TRADE_DIR = TMP / "real_trades"
broker.ORDER_DIR = TMP / "real_orders"
FAILS = []


def say(ok, name, got=""):
    print(("  OK     " if ok else "  FAIL   ") + name + ("" if ok else "  (得到 %r)" % (got,)))
    if not ok:
        FAILS.append(name)


try:
    say(str(AF.FIRE_DIR).startswith(str(TMP)) and str(NF.NF_DIR).startswith(str(TMP)),
        "尺的自證：帳本都導到暫存區")
    now = time.time()

    print("=== ① 日盤：記成失敗的那一張，撿回時要認得出來 ===")
    maybe = {"maybe_filled": True, "dir": "long", "px": 43800.0, "qty_sent": 7, "sent_at": now - 30,
             "sl_points": 219.0, "tp_points": 219.0, "no_tp": False}
    pos = {"dir": "long", "entry": 43812.0, "qty": 7, "recovered": True}
    say(AF.adopt_check(pos, maybe, now)[0], "方向、口數、價差、時間都對得上 ⇒ 是它")
    say(not AF.adopt_check(dict(pos, dir="short"), maybe, now)[0], "方向不一樣 ⇒ 不是")
    say(not AF.adopt_check(dict(pos, qty=8), maybe, now)[0], "口數比送出去的多 ⇒ 不是（多的是他自己的）")
    say(not AF.adopt_check(dict(pos, entry=43950.0), maybe, now)[0], "價差 150 點 ⇒ 不是")
    say(not AF.adopt_check(pos, dict(maybe, sent_at=now - 3600), now)[0], "一小時前送的 ⇒ 不是（多半是他後來自己開的）")
    say(not AF.adopt_check(pos, {}, now)[0], "今天沒有記成失敗的單 ⇒ 不是")
    m = AF._recover_decide(pos, None, "eod_no_entry", maybe)
    say(m.get("sl_src") == "autofire" and m.get("sl_points") == 219.0 and m.get("adopt"),
        "接手 ⇒ 停損用那一張的 219 點（⛔ 不是手動 130）", m)
    say(m.get("entry_qty") == 7, "多口 ⇒ 補回我們開了幾口", m)
    m2 = AF._recover_decide(pos, None, "eod_no_entry", dict(maybe, tp_points=None, no_tp=True))
    say(m2.get("no_tp") is True and m2.get("tp_points") is None, "開箱那種不設停利 ⇒ 接手後也不設", m2)
    m3 = AF._recover_decide(pos, None, "eod_no_entry", None)
    say(m3.get("sl_src") == "manual", "對照組：沒有那一張 ⇒ 跟以前一樣當手動單", m3)

    print("=== ① 日盤：接手後補寫帳本，收盤平倉認得它 ===")
    d = str(date.today())
    AF._append(dict(maybe, rec="result", ok=False, why="order_failed", date=d, stage="done", cand="fast",
                    method="U"))
    AF._MEM.update({"date": None, "entry": None, "state": None, "maybe": None})
    p2 = dict(pos, entry_time="09:03:35")
    broker._state["position"] = p2
    _saved_live = broker.is_live
    broker.is_live = lambda: False           # ⛔ 不掛停利單（演練）
    try:
        AF._recover_poll()                   # 第一圈：讀帳本、判定接手
        AF._recover_poll()                   # 第二圈：補寫那一列
    finally:
        broker.is_live = _saved_live
    ent, why = AF._auto_entry(d)
    say(ent is not None and ent.get("adopted") and ent.get("entry") == 43812.0,
        "帳本多了一列 result ok（adopted）⇒ 收盤平倉找得到這一口", (ent, why))
    say(AF._looks_ours(p2, ent)[0] if ent else False, "收盤認人：這口就是帳本那一口")
    say(p2.get("sl_points") == 219.0 and p2.get("sl_src") == "autofire", "部位的停損補成 219 點", p2)
    broker._state["position"] = None

    print("=== ① 夜盤：記成失敗的那一張，撿回時要認得出來 ===")
    E = date(2026, 11, 4)
    nmaybe = {"E": str(E), "rec": "result", "ok": False, "maybe_filled": True, "method": "R", "dir": "long",
              "px": 43790.0, "pts": 853.0, "qty": 3, "qty_sent": 3, "sent_at": now - 20}
    NF._append(nmaybe)
    NF._MEM.update(E=E, entry=None, maybe=None)
    npos = {"dir": "long", "entry": 43791.0, "qty": 3, "recovered": True, "sl_src": "manual",
            "entry_time": "22:40:02"}
    broker._state["position"] = npos
    NF._recover_poll()
    ent = NF._entry_of(NF.rows_of(E))
    say(ent is not None and ent.get("adopted"), "夜盤帳本補了一列 result ok（adopted）", ent)
    say(npos.get("sl_src") == "nightfire" and npos.get("sl_points") == float(int(43791 * 0.02)),
        "停損換成成交價 2%（875 點），⛔ 不是手動 130", npos)
    say(NF.looks_ours(npos, ent), "04:58 認人：這口就是帳本那一口")
    broker._state["position"] = None

    print("=== ③ 夜盤走幅歷史：用交易日算 ===")
    lots.KNOWN_CLOSED = {"2027-02-08", "2027-02-09", "2027-02-10", "2027-02-11", "2027-02-12"}
    gap = lots.trading_gap("2027-02-04", date(2027, 2, 15))
    say(gap <= NF.R_CTX_MAX_AGE_TD, "過年休 5 天後第一晚（日曆差 11 天）⇒ 交易日只差 %d ⇒ 照做" % gap, gap)
    say(lots.trading_gap("2026-10-20", date(2026, 11, 4)) > NF.R_CTX_MAX_AGE_TD,
        "對照組：面板兩週沒開 ⇒ 還是判太舊")
    src = (HERE / "night_fire.py").read_text(encoding="utf-8")
    say("lots.trading_gap(newest, E) > R_CTX_MAX_AGE_TD" in src, "判斷那一行真的換成交易日")
    lots.KNOWN_CLOSED = set()

    print("=== ④ 夜盤那口留過 05:00：白天也認得 ===")
    say(NF._mem_evening(datetime(2026, 11, 5, 9, 30)) == date(2026, 11, 4), "週四 09:30 ⇒ 認的是週三晚上")
    say(NF._mem_evening(datetime(2026, 11, 7, 9, 30)) == date(2026, 11, 6), "週六 09:30 ⇒ 週五晚上")
    say(NF._mem_evening(datetime(2026, 11, 9, 9, 30)) is None, "週一 09:30 ⇒ 週日沒有夜盤 ⇒ 不認")
    say(NF._mem_evening(datetime(2026, 11, 5, 16, 0)) == date(2026, 11, 5), "16:00 ⇒ 今晚（跟以前一樣）")
    E2 = date(2026, 11, 2)
    NF._append({"E": str(E2), "rec": "result", "ok": True, "method": "R", "dir": "short", "entry": 45000.0,
                "entry_time": "22:40:02", "qty": 1, "sl_points": 900.0, "tp_points": None, "no_tp": True})
    NF._MEM.update(E=None, entry=None)
    dpos = {"dir": "short", "entry": 45000.0, "qty": 1, "recovered": True, "sl_src": "manual"}
    broker._state["position"] = dpos
    NF.step(datetime(2026, 11, 3, 9, 30))
    say(dpos.get("sl_src") == "nightfire" and dpos.get("sl_points") == 900.0,
        "04:58 沒平掉、白天重啟 ⇒ 停損照夜盤 900 點（⛔ 不是 130）", dpos)
    E3 = date(2026, 11, 3)
    NF._append({"E": str(E3), "rec": "result", "ok": True, "method": "R", "dir": "long", "entry": 44000.0,
                "entry_time": "22:40:02", "qty": 1, "sl_points": 880.0, "tp_points": None, "no_tp": True})
    NF._append({"E": str(E3), "rec": "eod", "why": "closed", "msg": "04:58 平倉完成"})
    NF._MEM.update(E=None, entry=None)
    mpos = {"dir": "long", "entry": 44000.0, "qty": 1, "recovered": True, "sl_src": "manual"}
    broker._state["position"] = mpos
    NF.step(datetime(2026, 11, 4, 9, 30))
    say(mpos.get("sl_src") == "manual", "對照組：昨晚 04:58 已經平掉 ⇒ 現在這口不是夜盤的 ⇒ ⛔ 不認", mpos)
    broker._state["position"] = None
finally:
    AF.FIRE_DIR, NF.NF_DIR = REAL["AF"], REAL["NF"]
    broker.TRADE_DIR, broker.ORDER_DIR = REAL["TRADE"], REAL["ORDER"]
    broker._state["position"] = None
    shutil.rmtree(TMP, ignore_errors=True)

print("\n全部通過 ✅" if not FAILS else "\n%d 項沒過 ❌" % len(FAILS))
sys.exit(1 if FAILS else 0)
