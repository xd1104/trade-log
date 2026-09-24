# -*- coding: utf-8 -*-
"""
【面板看門狗】GitHub Actions 每 5 分鐘跑一次（2026-09-24 Benson：「面板斷了，不管是關機還是網路斷了，就響」）。

為什麼在 GitHub 跑：電腦關機／斷網時，它自己沒辦法通知任何人 ⇒ 一定要由外面來盯。
怎麼判斷斷了：面板每 2 分鐘把加密快照推到 `monitor` 分支 ⇒ 看那個分支最後一次 commit 的時間，
超過 STALE_MIN 分鐘沒更新就算斷了。⛔ 不需要解密快照（看時間就夠），所以這裡不碰監控密碼。
怎麼響：Web Push 推到手機 App（iPhone 要先把 App 加到主畫面、在 App 裡按過「開啟斷線通知」）。
  訂閱清單 `data/push-subs.json`（手機用鑰匙圈金鑰寫進來）；推播私鑰在 Actions secret `VAPID_PRIVATE_KEY`。
只響兩次：斷掉那一刻一次、恢復時一次（狀態存在 `watchdog` 分支的 state.json）。⛔ 不會每 5 分鐘一直吵。
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

STALE_MIN = 10
TW = timezone(timedelta(hours=8))
REPO = os.environ.get("GITHUB_REPOSITORY", "xd1104/trade-log")
SUB_CLAIM = "https://xd1104.github.io/trade-log/"


def gh(path):
    req = urllib.request.Request("https://api.github.com/repos/%s/%s" % (REPO, path),
                                 headers={"Accept": "application/vnd.github+json",
                                          "Authorization": "Bearer " + os.environ.get("GH_TOKEN", "")})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def last_report():
    """monitor 分支最後一次 commit 的時間（UTC aware）。"""
    b = gh("branches/monitor")
    return datetime.fromisoformat(b["commit"]["commit"]["committer"]["date"].replace("Z", "+00:00"))


def decide(state, last, now, stale_min=STALE_MIN):
    """⇒ (要送的通知 或 None, 新狀態)。純函式（測試直接打這一支）。"""
    age = (now - last).total_seconds() / 60.0
    was = (state or {}).get("status", "up")
    hm = lambda t: t.astimezone(TW).strftime("%m/%d %H:%M")
    if age > stale_min:
        if was == "down":
            return None, state
        msg = {"title": "⚠️ 早盤儀表板沒有回報了",
               "body": "已經 %d 分鐘沒收到（最後一次 %s）。可能是關機、斷網或當機；手上有部位的話，請用大戶投確認。"
                       % (int(age), hm(last)), "tag": "panel-down"}
        return msg, {"status": "down", "since": last.isoformat()}
    if was == "down":
        since = datetime.fromisoformat(state.get("since")) if state.get("since") else last
        gap = int((last - since).total_seconds() / 60.0)
        msg = {"title": "✅ 早盤儀表板恢復回報",
               "body": "中斷大約 %d 分鐘（%s ～ %s）。" % (max(gap, 0), hm(since), hm(last)), "tag": "panel-up"}
        return msg, {"status": "up"}
    return None, {"status": "up"}


def subs():
    try:
        with open("data/push-subs.json", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return []
    return [s for s in (d.get("subs") or []) if isinstance(s, dict) and s.get("endpoint") and s.get("keys")]


def send(msg):
    from pywebpush import webpush, WebPushException
    key = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    if not key:
        print("⛔ 沒有 VAPID_PRIVATE_KEY（Actions secret 還沒設）⇒ 送不出去")
        return 0
    ok = 0
    for s in subs():
        try:
            webpush(subscription_info={"endpoint": s["endpoint"], "keys": s["keys"]},
                    data=json.dumps(msg, ensure_ascii=False), vapid_private_key=key,
                    vapid_claims={"sub": SUB_CLAIM}, ttl=3600)
            ok += 1
        except WebPushException as e:
            print("送不出去（%s…）：%s" % (s["endpoint"][:40], str(e)[:160]))
    print("通知「%s」送出 %d／%d 支手機" % (msg["title"], ok, len(subs())))
    return ok


def main():
    sin, sout = os.environ.get("STATE_IN"), os.environ.get("STATE_OUT")
    try:
        state = json.load(open(sin, encoding="utf-8")) if sin else {}
    except Exception:
        state = {}
    if os.environ.get("TEST") == "true":
        send({"title": "🔔 測試通知", "body": "收到這則就代表斷線通知設定好了。面板斷線超過 10 分鐘會再響。", "tag": "test"})
    now = datetime.now(timezone.utc)
    try:
        last = last_report()
    except Exception as e:
        print("讀不到 monitor 分支：%s（這次不判斷）" % str(e)[:160])
        new = state
    else:
        print("面板最後回報 %s（%.1f 分鐘前）；上一次狀態 %s" % (last.astimezone(TW), (now - last).total_seconds() / 60, state.get("status", "up")))
        msg, new = decide(state, last, now)
        if msg:
            send(msg)
    if sout:
        with open(sout, "w", encoding="utf-8") as f:
            json.dump(new or {"status": "up"}, f, ensure_ascii=False, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
