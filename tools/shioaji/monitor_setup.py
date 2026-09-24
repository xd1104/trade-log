# -*- coding: utf-8 -*-
"""
【手機監控】設定密碼（2026-09-24）。⛔ 只要執行一次；換密碼就再跑一次（手機那邊輸入新密碼即可）。

    python monitor_setup.py

⛔ 密碼**不會**存下來：只存「從密碼算出來的鑰匙」到 MONITOR_KEY.json（已 gitignore、只留這台電腦）。
   手機輸入同一組密碼才解得開面板推上去的快照。
⚠️ 跑完要**重啟面板**才會開始推。
"""
import base64
import getpass
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import monitor_push as MP  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def main():
    print("設定手機監控的密碼（輸入時畫面不會顯示字，是正常的）")
    p1 = getpass.getpass("密碼：")
    if len(p1) < 6:
        print("⛔ 至少 6 個字，沒有存。")
        return 1
    p2 = getpass.getpass("再輸入一次：")
    if p1 != p2:
        print("⛔ 兩次不一樣，沒有存。")
        return 1
    salt = os.urandom(16)
    print("計算中（約幾秒）…")
    k_enc, k_mac = MP.derive(p1, salt)
    b = lambda x: base64.b64encode(x).decode()
    tmp = MP.KEY_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"v": 1, "salt": b(salt), "iter": MP.KDF_ITER,
                               "k_enc": b(k_enc), "k_mac": b(k_mac)}), encoding="utf-8")
    tmp.replace(MP.KEY_FILE)
    # 自我驗證：用剛存的鑰匙封一包、再解開
    key = MP.load_key()
    assert MP.open_(MP.seal({"ok": 1}, key), key) == {"ok": 1}
    print("✅ 設定好了（%s）。請重啟面板，手機輸入同一組密碼就能看。" % MP.KEY_FILE.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
