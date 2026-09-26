# -*- coding: utf-8 -*-
"""
送一則自訂通知到他的手機（2026-09-26；提醒事項用）。
走跟【斷線通知】同一條 Web Push：訂閱在 repo 的 data/push-subs.json、私鑰在本機 VAPID_PRIVATE.txt（gitignore）。
⛔ 只送通知，不碰任何交易。

用法：py -3.11 tools/shioaji/push_note.py "標題" "內容"
      py -3.11 tools/shioaji/push_note.py --dry     ⇒ 只列出會送到幾支手機，不送
"""
import json
import sys
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
KEY = HERE / "VAPID_PRIVATE.txt"
SUBS_URL = "https://raw.githubusercontent.com/xd1104/trade-log/main/data/push-subs.json"
SUB_CLAIM = "https://xd1104.github.io"          # ⚠️ 只能是網域、不能帶路徑


def subs():
    local = HERE.parent.parent / "data" / "push-subs.json"
    try:
        with urllib.request.urlopen(SUBS_URL, timeout=15) as r:     # 手機可能剛更新過訂閱 ⇒ 先拿 GitHub 上最新的
            d = json.loads(r.read().decode("utf-8"))
    except Exception:
        d = json.loads(local.read_text(encoding="utf-8")) if local.exists() else {}
    return [s for s in (d.get("subs") or []) if isinstance(s, dict) and s.get("endpoint") and s.get("keys")]


def main():
    ss = subs()
    if "--dry" in sys.argv:
        print("會送到 %d 支手機" % len(ss))
        return 0
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    from pywebpush import webpush
    key = KEY.read_text(encoding="utf-8").strip()
    msg = {"title": sys.argv[1], "body": sys.argv[2], "tag": "note"}
    ok = 0
    for s in ss:
        try:
            webpush(subscription_info={"endpoint": s["endpoint"], "keys": s["keys"]},
                    data=json.dumps(msg, ensure_ascii=False), vapid_private_key=key,
                    vapid_claims={"sub": SUB_CLAIM}, ttl=6 * 3600)
            ok += 1
        except Exception as e:
            print("送不出去：%s" % str(e)[:160])
    print("通知「%s」送出 %d／%d 支手機" % (msg["title"], ok, len(ss)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
