# -*- coding: utf-8 -*-
"""
研究用全天五檔：錄製器、訂閱、五檔看門狗、畫面那一行（2026-11-12 健檢整理第 5 批，從 live_panel.py 原樣搬出來）。

⛔ 這支**不下單、不碰停損**；live_panel 的 on_bidask 只呼叫 DEPTH.push（只 append），主迴圈只叫 depth_check（純函式）。
⛔ 不准 import live_panel（面板是用 `python live_panel.py` 跑的 ⇒ 再 import 一次會變成兩份狀態）。
   需要面板的東西由 live_panel 在載入時接上：PRODUCT、restart_blocks（沒接上 ⇒ depth_check 當成關鍵時段、不重連，安全方向）。
"""
from datetime import date, datetime
from pathlib import Path

import depth_writer
import market_clock as MC
import night_fire

HERE = Path(__file__).parent
PRODUCT = "TMF"            # live_panel 載入時會用它自己的 PRODUCT 蓋過去（同一個值）


def restart_blocks(now):
    """live_panel 載入時換成真的那支（重啟鈕的擋時段）。⛔ 沒接上 ⇒ 丟錯 ⇒ depth_check 當成關鍵時段（不重連）。"""
    raise RuntimeError("panel_depth.restart_blocks 還沒接上")


# ⭐ 2026-09-26 研究用：**全天**五檔（微台＋大台近月＋選擇權價平上下 5 檔；2026-09-30 加小台）落地到 depth_logs/。
#    券商不給過去的五檔，錯過就補不回來。⛔ 同一條鐵律：on_bidask 只 append（depth_writer.push）。
#    ⛔⛔ 大台／選擇權的報價**絕對不可以**進 Today（停損看的價）—— on_bidask 用代碼開頭擋（見 _is_main_code）。
DEPTH_DIR = HERE / "depth_logs"        # ⛔ gitignore；備份只帶壓好的 .gz
DEPTH = depth_writer.DepthWriter(DEPTH_DIR)
DEPTH_OPT_STRIKES = 10                 # 價平附近幾個履約價（× 買權／賣權 ＝ 20 個合約）
DEPTH_MAX_PCT = 70.0                   # 券商流量超過這個 % ⇒ 當天退訂大台與選擇權（微台照舊，下單不受影響）
DEPTH_SUBS = {"contracts": [], "codes": [], "paused_day": None, "err": None, "at": None}

# ⭐ 2026-10-14【五檔看門狗】10-12 08:59 永豐傳五檔那條線斷了（shioaji.log：「BidAsk FOP receiver disconnected」，
#    當天 08:30 換登入憑證之後），**成交那條沒斷** ⇒ 原本的斷線看門狗只看成交、什麼都沒發現，
#    五檔整整漏錄一天（日盤＋夜盤），畫面也沒有任何警告。
#    ⇒ 成交還在跳、五檔卻超過 DEPTH_STALE_S 秒沒進來 ⇒ 畫面與手機示警；**手上沒有部位**、
#       而且不在送單／平倉那幾刻前後 ⇒ 整條重連（跟原本斷線重連同一支 try_reconnect）。
#    ⛔ 有部位時**只示警不重連**：重連會卡住主迴圈幾秒，而主迴圈就是停損。等平倉後下一輪再重連。
DEPTH_STALE_S = 180
DEPTH_RETRY_EVERY = 600
DEPTH_QUIET_ZONES = tuple(MC.hms(t) for t in MC.ORDER_MOMENTS)   # ⭐ 11-12 改讀正本（多了結算日 13:28:30）；夜盤送單那兩刻見 night_moments()


def night_moments(now):
    """
    今晚夜盤兩個送單時刻（台積電快攻、夜盤跟勢）⇒ (done_at, trend_at) 兩個 datetime。
    ⛔ 2026-11-05：原本寫死夏令 21:35／21:40，11-02 美股換冬令後夜盤改 22:35／22:40，
       重啟鈕與五檔重連的「關鍵時刻」卻還擋在 21:3x ⇒ 真正送單那幾分鐘沒擋。
       ⇒ 一律跟 night_fire 問（同一支算法，美股冬夏令自動換）。
    """
    E = night_fire.evening_of(now) or now.date()
    return night_fire.done_at(E), night_fire.trend_at(E)
DEPTH_QUIET_PAD_S = 120
DEPTH_WD = {"last_ba": None, "stale_s": None, "retries": 0, "last_retry": 0.0, "held": False}


def depth_check(now_ts, last_ba, tick_quiet, market_open, conn_ok, has_pos, last_retry, clock):
    """⇒ (五檔幾秒沒進來 或 None, 要不要現在重連)。純函式（test_depth_watchdog 直接打）。
    ⚠️ 成交也停了（tick_quiet 大或 None）⇒ 那是整條斷線，交給原本的看門狗，這裡不管。"""
    if not market_open or not conn_ok or last_ba is None:
        return None, False
    if tick_quiet is None or tick_quiet > 60:
        return None, False
    q = now_ts - last_ba
    if q <= DEPTH_STALE_S:
        return None, False
    sec = clock.hour * 3600 + clock.minute * 60 + clock.second
    near = False
    zones = list(DEPTH_QUIET_ZONES)
    try:
        zones += [m.strftime("%H:%M:%S") for m in night_moments(clock)]
    except Exception:                     # noqa: BLE001  算不出來 ⇒ 退回夏令＋冬令兩組都當安靜（安全方向）
        zones += ["21:35:00", "21:40:00", "22:35:00", "22:40:00"]
    for z in zones:
        zs = int(z[:2]) * 3600 + int(z[3:5]) * 60 + int(z[6:8])
        d = abs(sec - zs)
        if min(d, 86400 - d) <= DEPTH_QUIET_PAD_S:
            near = True
    # ⭐ 2026-11-05 健檢：開箱突破（09:05～09:30 任何一刻）、夜盤參考價那段都不是「一個時刻」⇒
    #    跟重啟鈕吃同一份關鍵時段清單（restart_blocks），兩份清單不准再分岔。
    try:
        hm = clock.strftime("%H:%M")
        if any(a <= hm < b for a, b, _w in restart_blocks(clock)):
            near = True
    except Exception:                     # noqa: BLE001  算不出來 ⇒ 當成關鍵時段（不重連是安全方向）
        near = True
    return q, (not has_pos and not near and now_ts - last_retry >= DEPTH_RETRY_EVERY)


def depth_subscribe(api, main_contract, today=None, px=None):
    """
    訂大台近月＋選擇權近月價平上下 5 檔的五檔（只訂 BidAsk，不訂成交 —— 成交事後抓得到）。
    ⛔ 任何錯只記下來、不往外丟：connect() 不可以因為研究用的東西失敗。
    """
    today = today or date.today()
    if DEPTH_SUBS["paused_day"] == str(today):
        return []
    import shioaji as sj
    subs, exp = [], None
    try:
        txf = [c for c in api.Contracts.Futures.TXF
               if not c.code.startswith("TXFR") and getattr(c, "delivery_month", "")]
        txf.sort(key=lambda c: c.delivery_month)
        same = [c for c in txf if c.delivery_month == getattr(main_contract, "delivery_month", None)]
        fut = (same or txf)[0]
        subs.append(fut)
        # ⭐ 2026-09-30 加小台（Benson：口數多了要換小台，先錄一年的價差／掛單量再決定）。
        #    ⛔ 自己包 try：小台找不到只少錄它，大台與選擇權照訂。
        try:
            mxf = [c for c in api.Contracts.Futures.MXF
                   if not c.code.startswith("MXFR") and getattr(c, "delivery_month", "")]
            mxf.sort(key=lambda c: c.delivery_month)
            ms = [c for c in mxf if c.delivery_month == getattr(main_contract, "delivery_month", None)]
            if ms or mxf:
                subs.append((ms or mxf)[0])
        except Exception as e:
            print("⚠️ 【五檔錄製】小台找不到（其他照錄）：%s" % str(e)[:80], flush=True)
        if px is None:
            try:
                px = float(api.snapshots([fut])[0].close)
            except Exception:
                px = None
        if px:
            opts = [c for c in api.Contracts.Options.TXO if getattr(c, "delivery_date", None)
                    and c.delivery_date > today]
            if opts:
                exp = min(c.delivery_date for c in opts)
                near = [c for c in opts if c.delivery_date == exp]
                ks = sorted(set(c.strike_price for c in near), key=lambda k: (abs(k - px), k))[:DEPTH_OPT_STRIKES]
                subs += sorted([c for c in near if c.strike_price in ks],
                               key=lambda c: (c.strike_price, str(c.option_right)))
        DEPTH_SUBS.update(contracts=subs, codes=[c.code for c in subs])
        for c in subs:
            api.quote.subscribe(c, quote_type=sj.constant.QuoteType.BidAsk,
                                version=sj.constant.QuoteVersion.v1)
        DEPTH_SUBS.update(err=None, at=datetime.now().strftime("%m-%d %H:%M"))
        n_opt = sum(1 for c in subs if str(c.code).startswith("TXO"))
        print("【五檔錄製】已訂 %s＋選擇權 %d 個（價平 %s 附近，%s 到期）" % (
            "、".join(c.code for c in subs if not str(c.code).startswith("TXO")), n_opt,
            int(px) if px else "?", exp or "—"), flush=True)
    except Exception as e:
        DEPTH_SUBS["err"] = "訂閱失敗：%s" % str(e)[:120]
        print("⚠️ 【五檔錄製】" + DEPTH_SUBS["err"] + "（真單與停損不受影響）", flush=True)
    return subs


def depth_pause(api, why):
    """流量太高 ⇒ 今天退訂大台與選擇權。微台是真單合約，⛔ 不碰。"""
    import shioaji as sj
    DEPTH_SUBS["paused_day"] = str(date.today())
    for c in DEPTH_SUBS["contracts"]:
        try:
            api.quote.unsubscribe(c, quote_type=sj.constant.QuoteType.BidAsk,
                                  version=sj.constant.QuoteVersion.v1)
        except Exception:
            pass
    DEPTH_SUBS.update(contracts=[], codes=[])
    print("⚠️ 【五檔錄製】%s ⇒ 今天先停錄大台與選擇權（微台照錄、下單不受影響）" % why, flush=True)


def depth_view():
    s = DEPTH.stats()
    bc = s.get("by_code") or {}
    n_tmf = sum(v for k, v in bc.items() if str(k).startswith(PRODUCT))
    n_txf = sum(v for k, v in bc.items() if str(k).startswith("TXF"))
    n_mxf = sum(v for k, v in bc.items() if str(k).startswith("MXF"))
    n_opt = sum(v for k, v in bc.items() if str(k).startswith("TXO"))
    lost = s["dropped"] + s["lost_io"]
    stale = DEPTH_WD.get("stale_s")
    if stale:
        # ⭐ 2026-10-14 五檔看門狗：成交還在、五檔卻停了（10-12 那種）⇒ 一定要講出來
        head, warn = ("五檔錄製：⚠️ 已 %d 分鐘沒收到五檔（成交還在）—— %s"
                      % (int(stale // 60), "手上有部位，平倉後自動重連" if DEPTH_WD.get("held")
                         else "會自動重連（每 10 分鐘試一次）")), True
    elif DEPTH_SUBS["paused_day"] == str(date.today()):
        head, warn = "五檔錄製：流量偏高，今天只錄微台", True
    elif DEPTH_SUBS["err"]:
        head, warn = "五檔錄製：" + DEPTH_SUBS["err"], True
    else:
        head, warn = "五檔錄製：訂了 %d 個合約" % (len(DEPTH_SUBS["codes"]) + 1), False
    # ⚠️ 計數只算「面板這次開啟之後」（重啟就歸零；檔案本身是接著寫、不會少）⇒ 句子要講清楚，別讓人以為只錄了幾百筆
    msg = "%s（%s 這次開啟後已錄 大台 %s／小台 %s／微台 %s／選擇權 %s 筆）" % (
        head, (s.get("day") or "今天")[5:], format(n_txf, ","), format(n_mxf, ","), format(n_tmf, ","),
        format(n_opt, ","))
    if lost:
        msg += "　⚠️ 掉了 %s 筆" % format(lost, ",")
        warn = True
    return {"warn": warn, "msg": msg, "n": s["written"], "lost": lost}
