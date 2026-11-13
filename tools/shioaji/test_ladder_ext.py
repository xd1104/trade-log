# -*- coding: utf-8 -*-
"""
⭐ 2026-11-09 延長階梯（11＋4～33＋12，微台等值）測試。純離線：⛔ 不連永豐、不讀寫真的狀態檔。
  ① 每一級的日盤口數、夜盤兩種口數（波動偏高 b／其他 b−1），小台開著時拆成「小台＋微台」要跟研究表的「實際下單」欄一致
  ② 小台開關沒開 ⇒ fit_qty 壓回 10 口（⛔ 不是整盤不送）；開著 ⇒ 原樣；不合理的口數 ⇒ 原樣（讓 qty_ok 擋）
  ③ 升級建議：現在 10＋4、試車過、權益 54.2 萬 ⇒ 建議跳 11＋4；53.9 萬 ⇒ 不出（還差多少講得出來）
  ④ 每一級都有研究算的代價（確認條不內插）、風控上限＝400×(a＋b)
  ⑤ 減級門檻：11＋4 時權益掉到 54 萬×0.85 以下才退
跑法：.venv\\Scripts\\python.exe -X utf8 tools\\shioaji\\test_ladder_ext.py
"""
import pathlib
import re
import sys
from datetime import date

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import broker   # noqa: E402
import goal     # noqa: E402
import lots     # noqa: E402

FAIL = []


def chk(name, got, want):
    ok = got == want
    print(("  OK   " if ok else "  FAIL ") + name + ("" if ok else "  (得到 %r，期待 %r)" % (got, want)))
    if not ok:
        FAIL.append(name)


def legs_txt(q):
    """微台等值 q ⇒ 研究表的寫法（2S+3M／4M／1S）。"""
    s, m = divmod(q, broker.RATIO["MXF"])
    if s and m:
        return "%dS+%dM" % (s, m)
    return ("%dS" % s) if s else ("%dM" % m)


# ── 研究表（實際下單欄）──
SRC = pathlib.Path(r"C:/Users/Administrator/Desktop/claude/tick-research/ladder_mix_out_2026-11-06.txt")
TABLE = {}
if SRC.exists():
    for l in SRC.read_text(encoding="utf-8").splitlines():
        m = re.match(r"(\d+)＋(\d+)\s+聯軍\s+(\S+)＋跟勢\s+(\S+)", l)
        if m:
            TABLE[(int(m.group(1)), int(m.group(2)))] = (m.group(3).replace("＋", "+"), m.group(4).replace("＋", "+"))

# 小台「開著」：假的開關檔＋假的小台合約（⛔ 不碰真的 MINI_ON）
import tempfile  # noqa: E402
TMP = pathlib.Path(tempfile.mkdtemp(prefix="ladder-ext-"))
REAL_FLAG = broker.MINI_FLAG
broker.MINI_FLAG = TMP / "MINI_ON"
_old_contracts = dict(broker._state.get("contracts") or {})


def set_mini(on):
    if on:
        broker.MINI_FLAG.write_text("test", encoding="utf-8")
        broker._state["contracts"]["MXF"] = object()
    else:
        if broker.MINI_FLAG.exists():
            broker.MINI_FLAG.unlink()
        broker._state["contracts"].pop("MXF", None)


print("=== ① 每一級怎麼拆單（小台開著）===")
set_mini(True)
ext = [L for L in goal.LADDER if L[1] > 10]
chk("延長階梯有 23 級", len(ext), 23)
for thr, a, b, _p in ext:
    bl = max(1, b - 1)
    day_ok = broker.qty_ok(a) and broker.product_for(a) in ("MXF", "MIX")
    nights = [broker.qty_ok(x) for x in (b, bl)]
    got = (legs_txt(a), legs_txt(b))
    want = TABLE.get((a, b))
    chk("%d＋%d：日盤 %s／夜盤偏高 %s、平常 %s 口都送得出去" % (a, b, legs_txt(a), legs_txt(b), legs_txt(bl)),
        (day_ok, nights), (True, [True, True]))
    if want is not None:
        chk("%d＋%d：拆法跟研究表一樣（%s＋%s）" % (a, b, want[0], want[1]), got, want)
    sp = broker.mix_split(a)
    chk("%d＋%d：日盤兩腿加起來＝%d" % (a, b, a), sp["MXF"] + sp["TMF"], a)
    chk("%d＋%d：fit_qty 原樣（小台開著）" % (a, b), broker.fit_qty(a), a)
if not TABLE:
    print("  （找不到研究表，跳過逐字對照）")

print("\n=== ② 小台開關沒開 ⇒ 壓回 10 口 ===")
set_mini(False)
for q in (11, 12, 15, 33, 50):
    chk("小台沒開：%d 口 ⇒ 10 口、而且 qty_ok" % q, (broker.fit_qty(q), broker.qty_ok(broker.fit_qty(q))), (10, True))
for q in (1, 4, 10):
    chk("小台沒開：%d 口 ⇒ 原樣" % q, broker.fit_qty(q), q)
for q in (0, -3, 51, True, 11.0, "11", None):
    chk("不合理的口數 %r ⇒ 原樣、qty_ok 照樣擋" % (q,), (broker.fit_qty(q) is q or broker.fit_qty(q) == q,
                                                   broker.qty_ok(broker.fit_qty(q))), (True, False))
set_mini(True)
chk("小台開著：51 口（超過 10 口小台）⇒ 原樣、擋掉", (broker.fit_qty(51), broker.qty_ok(51)), (51, False))
# 開關有、但這次連線沒拿到小台合約 ⇒ 當成沒開
broker._state["contracts"].pop("MXF", None)
chk("開關有、沒拿到小台合約 ⇒ 13 口壓回 10", broker.fit_qty(13), 10)
set_mini(True)

print("\n=== ③ 升級建議（現在的狀態：10＋4、試車已過、已跳過一次）===")
ST = dict(lots.DEFAULT, level=10, up_at="2026-11-06",
          trial=dict(lots.TRIAL_DEFAULT, status="passed", passed_at="2026-10-21", jumped=True))
Q = {"n": 30, "need": 30, "slip_avg": 3.99, "n_err": 0, "ok": True}
RET = {"union": "ok", "trend": "ok"}
r = lots.upgrade_check(ST, 542295.0, "2026-11-09", date(2026, 11, 9), Q, RET)
chk("54.2 萬 ⇒ 建議跳 11＋4、條件全過", (r["ok"], r["to"], r["lots"], r["kind"]), (True, 11, "11＋4", "jump"))
r = lots.upgrade_check(ST, 539000.0, "2026-11-09", date(2026, 11, 9), Q, RET)
chk("53.9 萬 ⇒ 不升、講得出還差 1,000", (r["ok"], r["to"], [c.get("gap") for c in r["conds"] if c["k"] == "eq"]),
    (False, 11, [1000.0]))
r = lots.upgrade_check(ST, 700000.0, "2026-11-09", date(2026, 11, 9), Q, RET)
chk("70 萬 ⇒ 直接跳到 14＋5（權益撐得起的那一級）", (r["ok"], r["to"], r["lots"]), (True, 14, "14＋5"))
ST33 = dict(ST, level=33)
r = lots.upgrade_check(ST33, 9e6, "2026-11-09", date(2026, 11, 9), Q, RET)
chk("33＋12 ⇒ 已經最高一級", (r["ok"], r["why"]), (False, ["已經是最高一級"]))
# ⭐ 11-13 老闆拍板：升級不再看平均滑價（真實情境：12＋4、權益 65.2 萬、30 筆平均 4.2 點）
ST12 = dict(ST, level=12, up_at="2026-11-10")
Q42 = {"n": 30, "need": 30, "slip_avg": 4.2, "n_err": 0, "errors": [], "ok": False,
       "why": "最近 30 筆平均滑價 4.2 點，超過 4 點"}
r = lots.upgrade_check(ST12, 652366.0, "2026-11-13", date(2026, 11, 13), Q42, RET)
chk("⭐ 11-13：滑價 4.2 不再擋 ⇒ 建議 13＋5、條件裡沒有「平均滑價」",
    (r["ok"], r["to"], r["lots"], [c["k"] for c in r["conds"] if c["k"] == "slip"], r["why"]),
    (True, 13, "13＋5", [], []))
r = lots.upgrade_check(ST12, 652366.0, "2026-11-13", date(2026, 11, 13),
                       dict(Q42, slip_avg=30.0, why="最近 30 筆平均滑價 30.0 點，超過 4 點"), RET)
chk("滑價 30 點也不擋（拿掉就是拿掉）", r["ok"], True)
r = lots.upgrade_check(ST12, 652366.0, "2026-11-13", date(2026, 11, 13),
                       dict(Q42, n_err=1, errors=["日盤：券商拒絕"], why="最近 30 筆期間有 1 次下單錯誤"), RET)
chk("下單錯誤照樣擋、講得出原因", (r["ok"], r["why"]), (False, ["執行品質沒過：最近 30 筆期間有 1 次下單錯誤"]))
r = lots.upgrade_check(ST12, 652366.0, "2026-11-13", date(2026, 11, 13),
                       {"n": 0, "need": 30, "slip_avg": None, "errors": [], "ok": False, "why": "還沒有任何自動單，算不出滑價"}, RET)
chk("一筆自動真單都沒有 ⇒ 照樣不升", (r["ok"], r["why"]), (False, ["執行品質沒過：還沒有任何自動真單"]))

print("\n=== ④ 代價表、風控上限、口數 ===")
chk("每一級都有研究算的代價", [tuple(L[1:3]) for L in goal.LADDER if tuple(L[1:3]) not in lots.LADDER_COST], [])
chk("11＋4 代價（回落中位, 最差5%, 事故最壞）", lots.LADDER_COST[(11, 4)], (157453, 298574, 262713))
chk("33＋12 代價", lots.LADDER_COST[(33, 12)], (472361, 895724, 788139))
chk("第 11 級口數", lots.ladder_lots(11), (11, 4))
chk("第 33 級口數", lots.ladder_lots(33), (33, 12))
chk("第 34 級（不存在）⇒ 1＋1", lots.ladder_lots(34), (1, 1))
try:
    import risk_cap
    chk("風控上限 11＋4 ⇒ 6,000 點", risk_cap.cap_of(lots=(11, 4)), 6000.0)
except Exception as e:   # noqa: BLE001
    chk("風控上限算得出來（%s）" % e, False, True)
chk("夜盤口數 b＝5：偏高 5、平常 4", (lots.night_qty(5, True), lots.night_qty(5, False)), (5, 4))

print("\n=== ⑤ 減級 ===")
s1, e1 = lots.downgrade(dict(ST, level=11), 460000.0, retire=RET, incs=[], today=date(2026, 11, 9))
chk("11＋4、權益 46 萬（> 54 萬×0.85＝45.9 萬）⇒ 不退", (s1["level"], e1), (11, []))
s2, e2 = lots.downgrade(dict(ST, level=11), 450000.0, retire=RET, incs=[], today=date(2026, 11, 9))
chk("11＋4、權益 45 萬（< 45.9 萬）⇒ 退到 9＋4", (s2["level"], [e["kind"] for e in e2]), (9, ["down_equity"]))
s3, e3 = lots.downgrade(dict(ST, level=33), 1300000.0, retire=RET, incs=[], today=date(2026, 11, 9))
chk("33＋12、權益 130 萬（< 162 萬×0.85＝137.7 萬）⇒ 退到 26＋10", (s3["level"], lots.ladder_lots(s3["level"])),
    (26, (26, 10)))

# 收尾：還原
broker.MINI_FLAG = REAL_FLAG
broker._state["contracts"].clear()
broker._state["contracts"].update(_old_contracts)
print("\n總結：" + ("全部通過 ✅" if not FAIL else "%d 項沒過 ❌：%s" % (len(FAIL), "；".join(FAIL))))
sys.exit(1 if FAIL else 0)
