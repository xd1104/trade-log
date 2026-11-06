# -*- coding: utf-8 -*-
"""
小台（MXF）支援的離線測試（2026-11-06）。⛔ 不連永豐、不送任何單（演練模式＋會爆炸的假 place_order）。
驗：開關沒開 ⇒ 跟以前一模一樣；開了 ⇒ 5 的倍數整筆用小台、口數在送單／讀券商兩個邊界正確換算、
    對帳兩種合約都看、兩種反方向同時有部位或跟本機對不上 ⇒ 不動（同方向 ⇒ 混搭，見 test_mini_mix.py）。
跑法：.venv\\Scripts\\python.exe tools\\shioaji\\test_mini.py
"""
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import broker            # noqa: E402
import shioaji as sj     # noqa: E402

TMP = pathlib.Path(tempfile.mkdtemp(prefix="mini-test-"))
broker.ORDER_DIR = TMP / "real_orders"
broker.TRADE_DIR = TMP / "real_trades"
broker.REAL_FLAG = TMP / "REAL_ORDERS_ON"        # 不存在 ⇒ dry run
broker.MINI_FLAG = TMP / "MINI_ON"
FAIL = []


def chk(name, got, want):
    ok = got == want
    print(("  OK   " if ok else "  FAIL ") + name + ("" if ok else "  (得到 %r，期待 %r)" % (got, want)))
    if not ok:
        FAIL.append(name)


C_TMF = type("C", (), {"code": "TMFK6"})()
C_MXF = type("C", (), {"code": "MXFK6"})()


class API:
    def __init__(self, rows=(), pl=()):
        self.rows, self.pl = list(rows), list(pl)

    def place_order(self, contract, order):
        raise AssertionError("演練模式竟然真的送單了")

    def list_positions(self, acc=None):
        return self.rows

    def list_profit_loss(self, acc, a, b):
        return self.pl

    def list_accounts(self):
        return []


def P(code, q, d="Action.Buy", px=38000):
    return type("P", (), {"code": code, "quantity": q, "direction": d, "price": px})()


def setup(api, mini=False, with_mxf=True):
    broker.configure(api, C_TMF, {"MXF": C_MXF} if with_mxf else None)
    broker._state["position"] = None
    if mini:
        broker.MINI_FLAG.write_text("on", encoding="utf-8")
    elif broker.MINI_FLAG.exists():
        broker.MINI_FLAG.unlink()


print("=== ① 開關沒開 ⇒ 跟 11-06 以前一樣 ===")
setup(API())
chk("10 口 ⇒ 微台", broker.product_for(10), "TMF")
chk("口數上限仍是 10", (broker.qty_ok(10), broker.qty_ok(11), broker.qty_ok(15)), (True, False, False))
o = broker._order(sj.Action.Buy, 0, sj.FuturesPriceType.MKP, sj.OrderType.IOC, sj.FuturesOCType.New, qty=10)
chk("微台 10 口送 10 口", o.quantity, 10)
setup(API(), mini=True, with_mxf=False)
chk("開關開了但沒拿到小台合約 ⇒ 微台", broker.product_for(10), "TMF")

print("\n=== ② 開關開著 ⇒ 5 的倍數整筆小台 ===")
setup(API(), mini=True)
chk("10 口 ⇒ 小台", broker.product_for(10), "MXF")
chk("5 口 ⇒ 小台", broker.product_for(5), "MXF")
# ⭐ 2026-11-06 晚 混搭上線：開關開著時「不是 5 的倍數、≥5 口」改成混搭（下午那版是「不混搭 ⇒ 微台／不合理」）。
#    開關沒開的期待（①）一個都沒改；混搭的完整測試在 test_mini_mix.py。
chk("4／1 口 ⇒ 微台；7 口 ⇒ 混搭（1 小台＋2 微台）", [broker.product_for(q) for q in (4, 7, 1)], ["TMF", "MIX", "TMF"])
chk("上限放寬到 50（＝10 口小台）", (broker.qty_ok(50), broker.qty_ok(51)), (True, False))
chk("11～14 口改成混搭 ⇒ 合理", [broker.qty_ok(q) for q in (11, 12, 14, 15)], [True, True, True, True])
ok, err, _p = broker.enter("long", 38000, None, sl_points=470, qty=11)
chk("叫它下 11 口 ⇒ 混搭進場（演練）", (ok, (_p or {}).get("product"), (_p or {}).get("mix")),
    (True, "MIX", {"MXF": 10, "TMF": 1}))
broker._state["position"] = None
o = broker._order(sj.Action.Buy, 0, sj.FuturesPriceType.MKP, sj.OrderType.IOC, sj.FuturesOCType.New,
                  qty=10, product="MXF")
chk("微台等值 10 ⇒ 送 2 口小台", o.quantity, 2)
o = broker._order(sj.Action.Sell, 0, sj.FuturesPriceType.MKP, sj.OrderType.IOC, sj.FuturesOCType.Cover,
                  qty=1, product="MXF")
chk("預設 1 口（撿回部位的舊路）⇒ 進位成 1 口小台，不送 0 口", o.quantity, 1)
try:
    broker._order(sj.Action.Buy, 0, sj.FuturesPriceType.MKP, sj.OrderType.IOC, sj.FuturesOCType.New,
                  qty=20, product="TMF")
    chk("微台 20 口 ⇒ 組不出單", False, True)
except ValueError:
    chk("微台 20 口 ⇒ 組不出單（一張單最多 10 口）", True, True)

print("\n=== ③ 成交明細換算 ===")
deal = type("D", (), {"quantity": 2, "price": 38100.0})()
tr = type("T", (), {"contract": C_MXF, "status": type("S", (), {"deals": [deal]})()})()
chk("小台成交 2 口 ⇒ 微台等值 10", broker._deal_sums(tr)[1], 10.0)
tr2 = type("T", (), {"contract": C_TMF, "status": type("S", (), {"deals": [deal]})()})()
chk("微台成交 2 口 ⇒ 2", broker._deal_sums(tr2)[1], 2.0)
tr3 = type("T", (), {"status": type("S", (), {"deals": [deal]})()})()
chk("讀不到合約 ⇒ 照舊當微台", broker._deal_sums(tr3)[1], 2.0)

print("\n=== ④ 對帳：兩種合約都看 ===")
setup(API([P("MXFK6", 2)]), mini=True)
bp = broker.broker_position()
chk("券商上 2 口小台 ⇒ 微台等值 10、標小台", (bp["qty"], bp["product"], bp["dir"]), (10, "MXF", "long"))
setup(API([P("TMFK6", 3, "Action.Sell")]), mini=True)
bp = broker.broker_position()
chk("微台 3 口空單 ⇒ 3、微台", (bp["qty"], bp["product"], bp["dir"]), (3, "TMF", "short"))
setup(API([P("TMFK6", 3), P("MXFK6", 1)]), mini=True)
bp = broker.broker_position()
chk("兩種同方向 ⇒ 合成混搭（不再是 unknown）", (bp or {}).get("mix") if isinstance(bp, dict) else bp,
    {"MXF": 5, "TMF": 3})
setup(API([P("TMFK6", 3, "Action.Sell"), P("MXFK6", 1)]), mini=True)
chk("⛔ 兩種反方向 ⇒ unknown（不動）", broker.broker_position(), "unknown")
setup(API([P("TXFK6", 1)]), mini=True)
chk("大台部位不是我們的 ⇒ 沒部位", broker.broker_position(), None)
setup(API([P("MXFK6", 2)]), mini=False)
bp = broker.broker_position()
chk("⛔ 開關關著也要看得到小台部位（重啟撿回）", (bp or {}).get("product"), "MXF")

print("\n=== ⑤ 重啟撿回小台部位、本機與券商合約對不上 ===")
setup(API([P("MXFK6", 2)]), mini=True)
got = broker.reconcile()
chk("撿回的部位記得是小台、10 口等值", (got["product"], got["qty"], got["recovered"]), ("MXF", 10, True))
setup(API([P("MXFK6", 2)]), mini=True)
broker._state["position"] = {"dir": "long", "entry": 38000, "qty": 10, "product": "TMF",
                             "entry_time": "09:07", "target_trade": None, "recovered": False}
chk("⛔ 本機記微台、券商是小台 ⇒ unknown", broker.reconcile(), "unknown")
chk("  本機部位沒被清掉", (broker._state["position"] or {}).get("product"), "TMF")

print("\n=== ⑥ 已實現損益換算 ===")
setup(API(pl=[type("R", (), {"code": "MXFK6", "quantity": 2, "direction": "Action.Buy", "entry_price": 38000,
                             "cover_price": 38100, "pnl": 10000, "id": 1})(),
              type("R", (), {"code": "TXFK6", "quantity": 1, "direction": "Action.Buy", "entry_price": 38000,
                             "cover_price": 38100, "pnl": 20000, "id": 2})()]), mini=True)
rows = broker._realized_day("2026-11-06")
chk("小台 2 口 ⇒ 10 口等值；大台那列不是我們的", [(r["product"], r["qty"]) for r in rows], [("MXF", 10)])

print("\n=== ⑦ 演練進場：整筆小台 ===")
setup(API([]), mini=True)
ok, err, pos = broker.enter("long", 38000, None, sl_points=470, qty=10)
chk("進場成功（演練）", ok, True)
chk("部位標小台、口數仍是微台等值 10", (pos["product"], pos["qty"]), ("MXF", 10))
import json  # noqa: E402
logs = [json.loads(x) for x in next(broker.ORDER_DIR.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()]
ent = [x for x in logs if x.get("kind") == "entry"][-1]
chk("送單紀錄：小台、2 口", (ent.get("product"), ent.get("qty")), ("MXF", 2))
setup(API([]), mini=True)
ok, err, pos = broker.enter("long", 38000, None, sl_points=470, qty=4)
chk("4 口 ⇒ 微台", (pos["product"], pos["qty"]), ("TMF", 4))

print("\n=== ⑧ 演練停損平倉：送到小台 ===")
setup(API([]), mini=True)
broker.enter("long", 38000, None, sl_points=470, qty=10)
ok, err = broker.close("sl")[:2]
logs = [json.loads(x) for x in next(broker.ORDER_DIR.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()]
cl = [x for x in logs if str(x.get("octype", "")).endswith("Cover")][-1:]
chk("平倉成功（演練）", ok, True)
chk("平倉單送小台、2 口", [(x.get("product"), x.get("qty")) for x in cl], [("MXF", 2)])
chk("平完沒部位", broker._state["position"], None)

print("\n全部通過 ✅" if not FAIL else "\n%d 項沒過 ❌" % len(FAIL))
sys.exit(1 if FAIL else 0)
