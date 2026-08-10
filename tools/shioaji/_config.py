# -*- coding: utf-8 -*-
"""
讀取 API Key / Secret：只從本機的 .env 或環境變數拿，絕不寫死在程式碼裡。
（這個 repo 是 public，寫死等於公開金鑰。）
"""
import os
import sys
from pathlib import Path

ENV_PATH = Path(__file__).with_name(".env")


def _load_env_file():
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_credentials():
    _load_env_file()
    api_key = os.environ.get("SHIOAJI_API_KEY", "")
    secret = os.environ.get("SHIOAJI_SECRET_KEY", "")
    if not api_key or not secret or "貼上" in api_key:
        print("找不到 API Key / Secret。")
        print(f"請在這個路徑建立 .env：{ENV_PATH}")
        print("內容（參考同資料夾的 .env.example）：")
        print("  SHIOAJI_API_KEY=你的KEY")
        print("  SHIOAJI_SECRET_KEY=你的SECRET")
        sys.exit(1)
    return api_key, secret
