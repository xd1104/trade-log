# -*- coding: utf-8 -*-
"""
⭐ 2026-11-23 漲跌停空間保護（Benson 點頭）。純離線、不連永豐、不送單。
  ① 11-23 那晚重現：日盤收 36,856、22:40 價 40,100 做多、停損 3% ⇒ 只剩約 440 點 < 1,203 ⇒ 不做
  ② 平常日子照做；做空看跌停那一側
  ③ 基準拿不到／看起來不對 ⇒ 判斷不了 ⇒ 照常（⛔ 不因缺資料擋單）
  ④ 夜盤基準＝日盤最後一分鐘（13:44 往回找到 13:25，結算日 13:29 也找得到）
  ⑤ 日盤開箱借用 09:03:30 快照記下的昨收；換日不借
  ⑥ 原始碼：夜盤、日盤都在送單前擋，重試期間也重驗；夜盤重啟會補日盤最後幾根
跑法：.venv\\Scripts\\python.exe -X utf8 tools\\shioaji\\test_limit_room.py
"""
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.stdout.reconfigure(encoding="utf-8")

import testkit  # noqa: E402
testkit.sandbox()
import limit_room as LR  # noqa: E402

FAIL = []


def chk(name, got, want):
    ok = got == want
    print(("  OK   " if ok else "  FAIL ") + name + ("" if ok else "  (得到 %r，期待 %r)" % (got, want)))
    if not ok:
        FAIL.append(name)


print("=== ① 11-23 那晚 ===")
ok, why, info = LR.check("long", 40100.0, 36856.0, math.floor(40100 * 0.03))
chk("漲停 ＝ 36856 × 1.1 ≈ 40542", info["limit_up"], 40542)
chk("剩 442 點", info["limit_room"], 442.0)
chk("442 < 停損 1203 ⇒ 不做", ok, False)
chk("原因講得出漲停價與點數", ("漲停 40542" in why) and ("1203" in why), True)

print("\n=== ② 平常日子 ===")
chk("離基準 +0.4% ⇒ 照做", LR.check("long", 37000.0, 36856.0, 1110)[0], True)
chk("做空看跌停那一側：基準 36856、價 33500（剩 329）、停損 1005 ⇒ 不做",
    LR.check("short", 33500.0, 36856.0, 1005)[0], False)
chk("做空離跌停很遠 ⇒ 照做", LR.check("short", 36000.0, 36856.0, 1080)[0], True)
chk("做多不看跌停：價 33500 做多 ⇒ 照做", LR.check("long", 33500.0, 36856.0, 1005)[0], True)
chk("剛好等於停損 ⇒ 照做（< 才擋）", LR.check("long", 40542.0 - 500, 36856.0, 500)[0], True)
chk("日盤 ±0.5% 停損（約 200 點）：剩 150 ⇒ 不做", LR.check("long", 40392.0, 36856.0, 200)[0], False)

print("\n=== ③ 判斷不了 ⇒ 照常 ===")
for nm, b in (("None", None), ("0", 0), ("負數", -1), ("nan", float("nan")), ("字串", "abc"),
              ("離現價 50%（資料壞）", 20000.0)):
    ok, why, info = LR.check("long", 40100.0, b, 1203)
    chk("基準 %s ⇒ 照常、room=None" % nm, (ok, why, info["limit_room"]), (True, None, None))
chk("方向看不懂 ⇒ 照常", LR.check("flat", 40100.0, 36856.0, 1203)[0], True)

print("\n=== ④ 夜盤基準＝日盤最後一分鐘 ===")
import night_fire as NF  # noqa: E402
_o = NF._CFG["minute_close_fn"]
try:
    NF._CFG["minute_close_fn"] = None
    chk("沒接 ⇒ None", NF.day_close(), None)
    mc = {13 * 60 + 44: 36856.0, 13 * 60 + 40: 36800.0, 22 * 60 + 39: 40100.0}
    NF._CFG["minute_close_fn"] = mc.get
    chk("用 13:44 那一分鐘", NF.day_close(), 36856.0)
    mc = {13 * 60 + 29: 35000.0, 22 * 60 + 39: 40100.0}
    NF._CFG["minute_close_fn"] = mc.get
    chk("結算日 13:30 收 ⇒ 找到 13:29", NF.day_close(), 35000.0)
    NF._CFG["minute_close_fn"] = {22 * 60 + 39: 40100.0}.get
    chk("日盤那段沒資料 ⇒ None（⛔ 不拿夜盤的價頂）", NF.day_close(), None)
    NF._CFG["minute_close_fn"] = lambda m: 1 / 0
    chk("讀分鐘出錯 ⇒ None", NF.day_close(), None)
finally:
    NF._CFG["minute_close_fn"] = _o

print("\n=== ⑤ 日盤開箱借昨收 ===")
import auto_fire as AF  # noqa: E402
AF._PREV_CLOSE.update(d=None, v=None)
AF._note_prev_close("2026-11-24", {"prev_close": 36856.0})
chk("快照有 ⇒ 用快照的", AF._prev_close_of("2026-11-24", {"prev_close": 36000.0}), 36000.0)
chk("開箱沒有 ⇒ 借今天記下的", AF._prev_close_of("2026-11-24", {"px": 37000.0}), 36856.0)
chk("換日 ⇒ 不借昨天的", AF._prev_close_of("2026-11-25", {"px": 37000.0}), None)
AF._PREV_CLOSE.update(d=None, v=None)

print("\n=== ⑥ 原始碼 ===")
nf = (HERE / "night_fire.py").read_text(encoding="utf-8")
af = (HERE / "auto_fire.py").read_text(encoding="utf-8")
lp = (HERE / "live_panel.py").read_text(encoding="utf-8")
i_lr, i_ce = nf.index('_skip(E, "near_limit"'), nf.index("broker.can_enter_retry(px, True, refresh=lambda: _fresh_quote(now), valid=_still)")
chk("夜盤跟勢：先擋漲跌停、才問券商", i_lr < i_ce, True)
chk("夜盤重試期間也重驗漲跌停", "limit_room.check(d0, q, base" in nf, True)
j_lr, j_fire = af.index('_skip(d, "near_limit"'), af.index('fire.update({"rec": "fire", "stage": "sending"')
chk("日盤：在落地 sending 之前擋", j_lr < j_fire, True)
chk("日盤重試期間也重驗漲跌停", "limit_room.check(direction, q, _lbase, pts)" in af, True)
chk("夜盤重啟會補日盤 13:26～13:45 的分鐘收盤", "(_tt > dtime(13, 25)) & (_tt <= dtime(13, 45))" in lp, True)

print("\n%s" % ("全部通過" if not FAIL else "FAIL %d 項：%s" % (len(FAIL), "、".join(FAIL))))
sys.exit(1 if FAIL else 0)
