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

【安全】API Key / Secret 填在同資料夾的 .env（已被 .gitignore 擋掉），不寫進程式碼、不上傳。
"""
import sys
import time
import shioaji as sj

from _config import get_credentials

# Windows 主控台預設不是 UTF-8，中文會變亂碼
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

def main():
    API_KEY, API_SECRET = get_credentials()
    api = sj.Shioaji(simulation=True)          # ★ 模擬模式：不碰真錢、免憑證
    api.login(api_key=API_KEY, secret_key=API_SECRET)
    fut = api.futopt_account
    print("① 登入成功。期貨帳戶：", fut.broker_id, fut.account_id, "／signed 旗標：", fut.signed)
    if not fut.signed:
        # 實測（2026-08-10）：簽署完成後這個旗標仍可能回 False，但下單其實會過。
        # 所以這裡只提示，不中斷 —— 真正的判定看第 ④ 步的委託狀態。
        print("   （註：signed 旗標常沒即時更新，不用理它。若下單被 406 擋才是真的沒簽。）")

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
    try:
        trade = api.place_order(contract, order)
    except sj.ServerError as e:
        msg = str(e)
        print("④ 下單被伺服器擋下：", msg)
        if "sign" in msg.lower() or "406" in msg:
            print("   → 原因：期貨帳戶還沒完成 API 簽署。")
            print("   → 做法：永豐『API 專區 → 簽署中心 → 期貨簽署』簽完，等幾分鐘再跑一次本腳本。")
        api.logout(); return
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
