# -*- coding: utf-8 -*-
"""
【停損失靈】判法（2026-10-19 改，Benson 同意）的探針。

起因：10/19 開箱做多停損 258 點，急跌時一口氣跳過停損價、面板 0.5 秒內停損成交，每口賠 293 點；
舊判法「賠超過停損＋20 點」把它判成失靈 ⇒ 誤把升級暫停到 2027-04。
新判法：① 出場不是停損／停利、而且賠超過停損；② 每口賠超過停損 × 1.5。任一條才算。
另外守：判法改了之後，以前誤判的暫停要能解除；事故清單算不出來（None）時暫停不動；解除不進手機「要注意的事」。
⛔ 寫檔全導到暫存區。
"""
import json
import pathlib
import shutil
import sys
import tempfile
from datetime import date

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import auto_fire                    # noqa: E402
import broker                       # noqa: E402
import night_fire                   # noqa: E402
import lots                         # noqa: E402

FAIL = 0


def chk(name, got, want):
    global FAIL
    ok = got == want
    FAIL += not ok
    print(("  OK   " if ok else "  FAIL ") + name + ("" if ok else f"  (得到 {got!r}，期待 {want!r})"))


TMP = pathlib.Path(tempfile.mkdtemp(prefix="incident-test-"))
keep = (auto_fire.FIRE_DIR, night_fire.NF_DIR, broker.TRADE_DIR)
try:
    auto_fire.FIRE_DIR, night_fire.NF_DIR, broker.TRADE_DIR = TMP / "af", TMP / "nf", TMP / "rt"
    for d in (auto_fire.FIRE_DIR, night_fire.NF_DIR, broker.TRADE_DIR):
        d.mkdir()

    def case(day, pts, reason, sl=258.0, entry=46695.0, t="09:09:02"):
        with (auto_fire.FIRE_DIR / (day[:7] + ".jsonl")).open("a", encoding="utf-8") as f:
            f.write(json.dumps({"rec": "result", "ok": True, "live": True, "date": day, "dir": "long",
                                "entry": entry, "entry_time": t, "sl_points": sl}) + "\n")
        with (broker.TRADE_DIR / (day + ".jsonl")).open("a", encoding="utf-8") as f:
            f.write(json.dumps({"date": day, "dir": "long", "qty": 1, "entry_time": t, "entry": entry,
                                "exit_time": "10:00:00", "exit": entry + pts, "reason": reason,
                                "points": pts}) + "\n")

    print("=== ① 判法 ===")
    case("2026-10-19", -293.0, "sl")        # 10/19 那筆：急跌滑價 ⇒ 不算
    case("2026-10-20", -390.0, "sl")        # 超過 258×1.5＝387 ⇒ 算
    case("2026-10-21", -300.0, "eod")       # 停損沒被執行、抱到收盤 ⇒ 算
    case("2026-10-22", -200.0, "eod")       # 收盤平、賠得比停損少 ⇒ 不算
    case("2026-10-23", 250.0, "tp")         # 停利 ⇒ 不算
    got = sorted(i["d"] for i in lots.incidents())
    chk("  只有「超過 1.5 倍」與「停損沒執行」兩筆算失靈", got, ["2026-10-20", "2026-10-21"])
    chk("  10/19 急跌那筆不算", "2026-10-19" in got, False)

    print("=== ② 誤判的暫停要能解除 ===")
    st = dict(lots.STATE_DEFAULT) if hasattr(lots, "STATE_DEFAULT") else {}
    st["pause_until"] = "2027-04-19"
    st2, ev = lots.downgrade(dict(st), 57077.0, {"union": "ok", "trend": "ok"}, [], date(2026, 10, 19))
    chk("  沒有事故 ⇒ 暫停解除", st2.get("pause_until"), None)
    chk("  記一筆 pause_lifted", [e["kind"] for e in ev], ["pause_lifted"])
    st3, ev3 = lots.downgrade(dict(st), 57077.0, {"union": "ok", "trend": "ok"}, None, date(2026, 10, 19))
    chk("  ⛔ 事故清單算不出來（None）⇒ 暫停不動", st3.get("pause_until"), "2027-04-19")
    inc = [{"d": "2026-10-20", "sess": "day", "pts": -390.0, "sl": 258.0}]
    st4, ev4 = lots.downgrade(dict(st), 57077.0, {"union": "ok", "trend": "ok"}, inc, date(2026, 10, 20))
    chk("  有真的事故 ⇒ 改成那一筆的 6 個月後", st4.get("pause_until"), "2027-04-20")

    print("=== ③ 解除不進手機「要注意的事」 ===")
    chk("  pause_lifted 是 lifted 類", lots._NOTICE_KINDS.get("pause_lifted"), "lifted")
    src = (HERE / "lots.py").read_text(encoding="utf-8")
    chk("  手機 warn 排除 lifted", 'notice.get("kind") != "lifted"' in src, True)
finally:
    auto_fire.FIRE_DIR, night_fire.NF_DIR, broker.TRADE_DIR = keep
    shutil.rmtree(TMP, ignore_errors=True)

print("全部通過 ✅" if not FAIL else "⛔ %d 項失敗" % FAIL)
sys.exit(1 if FAIL else 0)
