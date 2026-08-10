# -*- coding: utf-8 -*-
"""
永豐 Shioaji：期貨 API 測試（模擬模式 simulation=True）
=====================================================
依官方文件：API 測試在「模擬模式」完成即可 —— 不碰真錢、不需要憑證(CA)。
內容：登入測試 login + 期貨下單測試 place_order；狀態 PendingSubmit / Submitted = 通過。

【前提】
- API Key 已開通「交易」權限（下單測試需要）
- 期貨已「簽署」，且簽署時間早於現在
- 測試時間：週一~週五 08:00~20:00（18:00 後限台灣 IP）
- pip install shioaji   （版本需 >= 1.2）

【安全】API Key / Secret 只填在自己電腦，別外流、別上傳。
"""
import time
import shioaji as sj

# ↓↓↓ 換成你自己的 ↓↓↓
API_KEY    = "貼上你的_API_KEY"
API_SECRET = "貼上你的_SECRET_KEY"

def main():
    api = sj.Shioaji(simulation=True)          # ★ 模擬模式：不碰真錢、免憑證
    accounts = api.login(api_key=API_KEY, secret_key=API_SECRET)
    print("① 登入成功：", accounts)

    # 期貨近月連續（大台）。測試用哪個期貨商品都可以。
    contract = api.Contracts.Futures.TXF.TXFR1
    print("② 商品：", contract.code, getattr(contract, "name", ""))

    # 取現價當委託價（模擬單，成不成交都沒差；盤中才抓得到現價）
    price = None
    try:
        snap = api.snapshots([contract])[0]
        price = snap.close or snap.sell_price or snap.buy_price
    except Exception as e:
        print("   （抓現價失敗：", e, "）")
    if not price:
        print("⚠️ 抓不到現價，可能非交易時段。請在盤中(8:45–13:45)再跑一次。")
        api.logout(); return
    print("③ 委託價：", price)

    order = sj.FuturesOrder(
        action=sj.Action.Buy,
        price=price,
        quantity=1,
        price_type=sj.FuturesPriceType.LMT,
        order_type=sj.OrderType.ROD,
        octype=sj.FuturesOCType.Auto,
        account=api.futopt_account,
    )
    trade = api.place_order(contract, order)
    time.sleep(1)
    api.update_status(api.futopt_account)
    print("④ 委託狀態：", trade.status.status)
    print(trade)

    if any(s in str(trade.status.status) for s in ("PendingSubmit", "Submitted")):
        print("   ✅ 通過條件達成！去『簽署中心 → 期貨簽署』等約 5 分鐘，"
              "重新整理看『python 測試』是否變『已測試』。")
    else:
        print("   ⚠️ 狀態不是 PendingSubmit/Submitted，把上面整段輸出貼給 Claude 看。")

    api.logout()

if __name__ == "__main__":
    main()
