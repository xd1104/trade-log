# -*- coding: utf-8 -*-
"""
⭐ 2026-11-06 探針：永豐**模擬帳戶**上 IOC 範圍市價單結束後，trade.status.status 的實際字串是什麼
（broker 多口平倉第二張起靠它判斷「上一張結束了沒」）。⛔ simulation=True 寫死；送 1 口進、1 口平。
"""
import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import shioaji as sj                      # noqa: E402
from _config import get_credentials      # noqa: E402

api = sj.Shioaji(simulation=True)
k, s = get_credentials()
api.login(api_key=k, secret_key=s)
c = sorted([x for x in api.Contracts.Futures.TMF if not x.code.startswith("TMFR")], key=lambda x: x.delivery_month)[0]


def go(action, oc, q):
    o = sj.FuturesOrder(action=action, price=0, quantity=q, price_type=sj.FuturesPriceType.MKP,
                        order_type=sj.OrderType.IOC, octype=oc, account=api.futopt_account)
    t = api.place_order(c, o)
    for i in range(6):
        time.sleep(1)
        api.update_status(api.futopt_account)
        print("  %s %s q=%d  t+%ds  status=%r  deals=%s" % (action, oc, q, i + 1, str(t.status.status),
                                                       [(d.price, d.quantity) for d in (t.status.deals or [])]))
    return t


print("合約", c.code)
go(sj.Action.Buy, sj.FuturesOCType.New, 3)
time.sleep(2)
pos = [p for p in api.list_positions(api.futopt_account) if p.code == c.code]
q = sum(int(p.quantity) for p in pos)
print("部位", [(p.code, p.direction, p.quantity) for p in pos])
if q:
    go(sj.Action.Sell, sj.FuturesOCType.Cover, q)
time.sleep(2)
print("最後部位", [(p.code, p.direction, p.quantity) for p in api.list_positions(api.futopt_account) if p.code == c.code])
api.logout()
