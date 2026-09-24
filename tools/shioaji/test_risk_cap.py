# -*- coding: utf-8 -*-
"""
風控規則 B（`risk_cap.py`）的探針 —— 離線、全部在暫存資料夾、⛔ 不碰真的帳本與開關。
用法：python test_risk_cap.py
"""
import ast
import json
import pathlib
import shutil
import sys
import tempfile
from datetime import date

sys.stdout.reconfigure(encoding="utf-8")
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import risk_cap as RC  # noqa: E402

_REAL_START = RC.RULE_START
RC.RULE_START = "2000-01-01"          # 下面的假資料在 2026-07／09；起算日另外在 ⑫ 驗

FAILS = []


def chk(name, got, want):
    ok = got == want
    print(("  OK     " if ok else "  FAIL   ") + name + ("" if ok else "  (得到 %r，期待 %r)" % (got, want)))
    if not ok:
        FAILS.append(name)


TMP = pathlib.Path(tempfile.mkdtemp(prefix="riskcap-"))
FD, ND, TD = TMP / "autofire", TMP / "nightfire", TMP / "real_trades"
FLAG = TMP / "RISK_OVERRIDE"
for p in (FD, ND, TD):
    p.mkdir()


def put(p, rows):
    with p.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def st(month, qty=1):
    return RC.state(month=month, qty=qty, fire_dir=FD, nf_dir=ND, trade_dir=TD, flag=FLAG)


try:
    print("── ① 空的 ⇒ 0 點、不擋")
    s = st("2026-07")
    chk("pnl 0", s["pnl"], 0.0)
    chk("不擋", s["blocked"], False)

    print("── ② 日盤：帳本 result ok × 成績單（同一天、同進場時間與價）")
    put(FD / "2026-07.jsonl", [
        {"rec": "result", "ok": True, "date": "2026-07-01", "entry": 22000.0, "entry_time": "09:03:30"},
        {"rec": "result", "ok": False, "date": "2026-07-02", "entry": 22100.0, "entry_time": "09:03:30"},
        {"rec": "skip", "date": "2026-07-03"},
    ])
    put(TD / "2026-07-01.jsonl", [
        {"date": "2026-07-01", "entry_time": "09:03:30", "entry": 22000.0, "points": -300.0, "qty": 1},
        # 他自己手動的單（帳本沒有）⇒ ⛔ 不算
        {"date": "2026-07-01", "entry_time": "10:11:12", "entry": 22050.0, "points": -900.0, "qty": 1},
    ])
    s = st("2026-07")
    chk("只算自動那一口 −300（手動 −900 不算、送失敗那列不算）", s["pnl"], -300.0)
    chk("n=1", s["n"], 1)

    print("── ③ 夜盤：7/31 晚上那口 8/1 凌晨平 ⇒ 算 7 月")
    put(ND / "2026-07.jsonl", [
        {"rec": "result", "ok": True, "E": "2026-07-31", "entry": 22300.0, "entry_time": "21:40:02"},
    ])
    put(TD / "2026-08-01.jsonl", [
        {"date": "2026-08-01", "entry_time": "21:40:02", "entry": 22300.0, "points": -450.0, "qty": 1},
    ])
    s = st("2026-07")
    chk("7 月 = −300 − 450 = −750", s["pnl"], -750.0)
    chk("還沒到 800 ⇒ 不擋", s["blocked"], False)
    chk("8 月不會把那一口算進去", st("2026-08")["pnl"], 0.0)

    print("── ④ 跨過上限那一筆照算 ⇒ 擋")
    put(FD / "2026-07.jsonl", [
        {"rec": "result", "ok": True, "date": "2026-07-06", "entry": 22200.0, "entry_time": "09:15:00"},
    ])
    put(TD / "2026-07-06.jsonl", [
        {"date": "2026-07-06", "entry_time": "09:15:00", "entry": 22200.0, "points": -100.0, "qty": 1},
        # 同一天同一時刻、但進場價差 5 點 ⇒ ⛔ 不是同一口（不可以被配上去）
        {"date": "2026-07-06", "entry_time": "09:15:00", "entry": 22205.0, "points": -999.0, "qty": 1},
    ])
    s = st("2026-07")
    chk("−850 ⇒ hit", (s["pnl"], s["hit"]), (-850.0, True))
    chk("⇒ 擋", s["blocked"], True)
    chk("那句話講得出虧多少、上限多少", ("850" in s["msg"] and "800" in s["msg"]), True)

    print("── ⑤ 口數放大 ⇒ 上限跟著放大")
    s2 = st("2026-07", qty=2)
    chk("2 口上限 1,600 ⇒ −850 不擋", (s2["cap"], s2["blocked"]), (1600.0, False))

    print("── ⑥ 手動解除只對寫的那個月有效")
    FLAG.write_text("2026-06", encoding="ascii")
    chk("寫 6 月 ⇒ 7 月照擋", st("2026-07")["blocked"], True)
    FLAG.write_text("2026-07", encoding="ascii")
    s = st("2026-07")
    chk("寫 7 月 ⇒ 7 月不擋", (s["hit"], s["override"], s["blocked"]), (True, True, False))
    FLAG.write_text("亂寫", encoding="utf-8")
    chk("看不懂的內容 ⇒ 當沒解除", st("2026-07")["blocked"], True)
    FLAG.unlink()

    print("── ⑦ 平了但沒有出場價 ⇒ 不猜、不擋、照實說")
    put(FD / "2026-09.jsonl", [
        {"rec": "result", "ok": True, "date": "2026-09-01", "entry": 23000.0, "entry_time": "09:03:30"},
        {"rec": "result", "ok": True, "date": "2026-09-02", "entry": 23100.0, "entry_time": "09:03:30"},
    ])
    put(TD / "2026-09-01.jsonl", [
        {"date": "2026-09-01", "entry_time": "09:03:30", "entry": 23000.0, "points": None},
    ])
    s = st("2026-09")
    chk("unknown 1、open 1、pnl 0", (s["unknown"], s["open"], s["pnl"]), (1, 1, 0.0))

    print("── ⑧ 讀檔炸掉 ⇒ 擋（⛔ 不猜）")
    s = RC.state(month="bad", fire_dir=FD, nf_dir=ND, trade_dir=TD, flag=FLAG)
    chk("月份壞掉 ⇒ err 有值且擋", (bool(s["err"]), s["blocked"]), (True, True))

    print("── ⑨ 結構：risk_cap 一個位元組都不寫")
    tree = ast.parse((HERE / "risk_cap.py").read_text(encoding="utf-8"))
    writes = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and n.attr in ("write_text", "write_bytes", "unlink", "rename",
                                                        "replace", "mkdir", "touch"):
            writes.append(n.attr)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "open":
            writes.append("open")
        if isinstance(n, ast.Attribute) and n.attr == "open" and getattr(n.value, "id", "") == "os":
            writes.append("os.open")
    chk("沒有任何寫檔／刪檔／改名", writes, [])

    print("── ⑩ 結構：兩條送單路都在「落地 sending／送單」之前問風控")
    af = (HERE / "auto_fire.py").read_text(encoding="utf-8")
    body = af[af.index("def _send("):af.index("def _fmt_px(")]
    chk("auto_fire._send 有問 risk_cap.blocked", "risk_cap.blocked(" in body, True)
    chk("…而且在 _append(fire) 之前", body.index("risk_cap.blocked(") < body.index("_append(fire)"), True)
    nf = (HERE / "night_fire.py").read_text(encoding="utf-8")
    body = nf[nf.index("def _decide("):nf.index("def _trend_sl_after_fill(")]
    chk("night_fire._decide 有問 risk_cap.blocked", "risk_cap.blocked(E" in body, True)
    chk("…而且在分 T／R 之前（兩條都管到）",
        body.index("risk_cap.blocked(") < body.index("return _decide_trend(E, now)"), True)
    chk("…而且在送單之前", body.index("risk_cap.blocked(") < body.index("broker.enter("), True)

    print("── ⑫ 起算日之前的單不算（Benson：只從 09-23 算）")
    chk("正本起算日是 2026-09-23", _REAL_START, "2026-09-23")
    RC.RULE_START = "2026-07-05"
    s = st("2026-07")
    chk("7/1 那筆 −300 不算、7/6 −100 與 7/31 夜盤 −450 照算", s["pnl"], -550.0)
    chk("since 標出起算日", s["since"], "2026-07-05")
    RC.RULE_START = "2000-01-01"

    print("── ⑪ 真的 RISK_OVERRIDE 沒被碰")
    chk("真的 RISK_OVERRIDE 不存在（測試不准建）", (HERE / "RISK_OVERRIDE").exists(), False)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print()
print("全部通過 ✅" if not FAILS else "⛔ %d 項失敗" % len(FAILS))
sys.exit(1 if FAILS else 0)
