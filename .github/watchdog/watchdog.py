# -*- coding: utf-8 -*-
"""
【面板看門狗】GitHub Actions 每 5 分鐘跑一次（2026-09-24 Benson：「面板斷了，不管是關機還是網路斷了，就響」）。

為什麼在 GitHub 跑：電腦關機／斷網時，它自己沒辦法通知任何人 ⇒ 一定要由外面來盯。
怎麼判斷斷了：面板每 2 分鐘把加密快照推到 `monitor` 分支 ⇒ 看那個分支最後一次 commit 的時間，
超過 STALE_MIN 分鐘沒更新就算斷了。⛔ 不需要解密快照（看時間就夠），所以這裡不碰監控密碼。
怎麼響：Web Push 推到手機 App（iPhone 要先把 App 加到主畫面、在 App 裡按過「開啟斷線通知」）。
  訂閱清單 `data/push-subs.json`（手機用鑰匙圈金鑰寫進來）；推播私鑰在 Actions secret `VAPID_PRIVATE_KEY`。
只響兩次：斷掉那一刻一次、恢復時一次（狀態存在 `watchdog` 分支的 state.json）。⛔ 不會每 5 分鐘一直吵。

⭐ 2026-11-03 第二道「斷線通知失效」：11-02 09:41 起面板照推快照、卻沒打 Healthchecks 心跳 ⇒ Healthchecks
早就是 down，隔天真當機時沒有新的「轉 down」可觸發，手機沒響。⇒ 快照 commit 的說明欄帶心跳時間
（monitor_push.commit_msg），快照是新的、心跳卻停超過 HC_STALE_MIN 分鐘 ⇒ 推「斷線通知失效」；恢復再推一則。
Healthchecks 轉 down 本來就會觸發這支 ⇒ 心跳停了大約 12 分鐘手機就會知道。
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

STALE_MIN = 10
HC_STALE_MIN = 10       # 心跳停多久算失效（⚠️ 要 ≤ Healthchecks 的 period＋grace＝12 分，轉 down 觸發這支時才判得出來）
TW = timezone(timedelta(hours=8))
REPO = os.environ.get("GITHUB_REPOSITORY", "xd1104/trade-log")
SUB_CLAIM = "https://xd1104.github.io"          # ⚠️ 只能是網域、不能帶路徑（py_vapid 會當成沒填）


def gh(path):
    req = urllib.request.Request("https://api.github.com/repos/%s/%s" % (REPO, path),
                                 headers={"Accept": "application/vnd.github+json",
                                          "Authorization": "Bearer " + os.environ.get("GH_TOKEN", "")})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def last_report():
    """monitor 分支最後一次 commit ⇒ (時間 UTC aware, 說明欄)。"""
    c = gh("branches/monitor")["commit"]["commit"]
    return datetime.fromisoformat(c["committer"]["date"].replace("Z", "+00:00")), c.get("message") or ""


def parse_msg(msg):
    """快照 commit 說明欄 ⇒ 心跳的參考時間（最後一次成功；還沒成功過就用面板啟動時間）。
    舊格式（只有 `monitor`）、`hc=off`（沒設心跳網址）、看不懂 ⇒ None（⛔ 不判斷，免得亂響）。"""
    m = re.search(r"hc_ok=(\S+) up=(\S+)", msg or "")
    if not m:
        return None
    for v in m.groups():
        if v != "none":
            try:
                t = datetime.fromisoformat(v)
            except ValueError:
                return None
            return t if t.tzinfo else None
    return None


def decide_hc(state, last, msg):
    """快照還是新的時候看心跳 ⇒ (要送的通知 或 None, 新的 hc 狀態 "ok"/"bad"/原值)。純函式。"""
    was = (state or {}).get("hc", "ok")
    ref = parse_msg(msg)
    if ref is None:
        return None, was
    age = (last - ref).total_seconds() / 60.0
    if age > HC_STALE_MIN:
        if was == "bad":
            return None, "bad"
        return ({"title": "⚠️ 斷線通知失效了",
                 "body": "面板還活著，但已經 %d 分鐘沒打心跳（最後一次 %s）。這段時間電腦當機手機不會響，請重啟面板。"
                         % (int(age), ref.astimezone(TW).strftime("%m/%d %H:%M")), "tag": "hc-bad"}, "bad")
    if was == "bad":
        return {"title": "✅ 斷線通知恢復", "body": "面板又開始打心跳了。", "tag": "hc-ok"}, "ok"
    return None, "ok"


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
    from pywebpush import webpush
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
        except Exception as e:          # ⛔ 一支手機送不出去不可以讓整個看門狗掛掉（WebPushException 也在這裡）
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
        last, cmsg = last_report()
    except Exception as e:
        print("讀不到 monitor 分支：%s（這次不判斷）" % str(e)[:160])
        new = state
    else:
        print("面板最後回報 %s（%.1f 分鐘前）；上一次狀態 %s；說明欄「%s」"
              % (last.astimezone(TW), (now - last).total_seconds() / 60, state.get("status", "up"), cmsg[:80]))
        msg, new = decide(state, last, now)
        if msg:
            send(msg)
        new = dict(new or {"status": "up"})
        if new.get("status") == "up":
            # ⛔ 面板本身斷了就不看心跳（上面那則已經響了）
            hmsg, new["hc"] = decide_hc(state, last, cmsg)
            if hmsg:
                send(hmsg)
        elif "hc" in (state or {}):
            new["hc"] = state["hc"]
    if sout:
        with open(sout, "w", encoding="utf-8") as f:
            json.dump(new or {"status": "up"}, f, ensure_ascii=False, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
