# -*- coding: utf-8 -*-
"""
把面板收到的報價逐筆記下來。

⛔ 為什麼不自己連永豐
====================
最直覺的做法是另外開一個 shioaji 連線、掛 on_tick 自己收。**不要這樣做。**
永豐的 token 是有限的 slot（shioaji.log 裡看得到 "Removed invalid token from slot N"），
再登入一次有機會把面板那個連線擠掉 ⇒ 面板收不到報價 ⇒
**停損就不再監控了**（永豐沒有停損單，停損活在面板的 Python 迴圈裡）。
盤中他手上隨時可能有部位，這個代價不能冒。

所以這支只做一件事：**高頻去讀面板自己的 /api/state**，把每一次變動記下來。
唯讀、不送單、不登入、對面板的唯一影響是多幾個 HTTP 請求。

代價要講清楚（別假裝這是逐筆成交資料）
====================================
這是**取樣**，不是券商的逐筆 tick：
  - 同一個 20ms 內連續兩筆成交、而且價格一樣 → 看起來只有一筆
  - 面板本身也只保留「最後一筆」，中間跳掉的它自己也不知道
  - 真正的逐筆要另外向永豐要（或改面板的 on_tick 落地），那是另一件事
所以每一列都附 `ms`（跟上一列差幾毫秒），事後看得出取樣密不密。

怎麼跑：
    .venv\\Scripts\\python.exe tools\\shioaji\\tick_recorder.py --until 09:30
    .venv\\Scripts\\python.exe tools\\shioaji\\tick_recorder.py --until 13:45 --every 0.1
    .venv\\Scripts\\python.exe tools\\shioaji\\tick_recorder.py --all      （連沒變動的也記）

輸出：tools/shioaji/tick_logs/YYYY-MM-DD-**polled**.jsonl（一行一筆，隨寫隨 flush，
      中途被關掉也不會整份不見）

⚠️ 檔名裡的 `-polled` 不可以拿掉。`YYYY-MM-DD.jsonl` 是面板本體（live_panel.py 的
   on_tick／on_bidask，見 tick_writer.py）寫的**逐筆**資料，欄位完全不同：
   那邊是 `{"k":"t","t":"09:04:51.706","p":47245,"v":3}`，這邊是每 0.1 秒的整份盤面快照。
   混在同一個檔裡，讀的人會把取樣當成逐筆。**這支保留**是因為它有一個面板做不到的優點：
   不用重啟面板就能開始錄（盤中他手上可能有部位，面板不能停）。
"""
import argparse
import datetime
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
OUT_DIR = HERE / "tick_logs"
URL = "http://127.0.0.1:8770/api/state"

# 要留下來的欄位。刻意連 bid/ask 一起記 —— 只有成交價的話，
# 事後想看「那一刻的買賣價差」就永遠補不回來了。
CHIP_KEYS = ("price", "bid", "ask", "is_mid", "chg", "gap", "rng", "pos",
             "vol_ratio", "mom5", "mom15", "idx", "idx_age", "idx_chg",
             "idx_pct", "basis")


def fetch(timeout=2.0):
    with urllib.request.urlopen(URL, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--until", default="09:30", help="錄到幾點（HH:MM，本機時鐘）")
    ap.add_argument("--every", type=float, default=0.1, help="幾秒問一次（預設 0.1）")
    ap.add_argument("--all", action="store_true",
                    help="連沒變動的也記（預設只記價格/買賣價有變的那些）")
    ap.add_argument("--url", default=URL)
    args = ap.parse_args()

    hh, mm = (int(x) for x in args.until.split(":"))
    now = datetime.datetime.now()
    stop_at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if stop_at <= now:
        print(f"⛔ {args.until} 已經過了（現在 {now:%H:%M:%S}），沒有東西可以錄。")
        sys.exit(2)

    OUT_DIR.mkdir(exist_ok=True)
    # ⚠️ 一定要帶 -polled：不帶的話會跟面板逐筆落地的 YYYY-MM-DD.jsonl 撞檔名，
    #    而兩邊的欄位完全不同（那是逐筆，這是每 0.1 秒的取樣）。
    out = OUT_DIR / f"{now:%Y-%m-%d}-polled.jsonl"
    # append：同一天跑第二次要接在後面，不可以蓋掉早上already錄好的
    f = out.open("a", encoding="utf-8")

    print(f"錄到 {stop_at:%H:%M:%S}（剩 {(stop_at - now).total_seconds() / 60:.1f} 分鐘）")
    print(f"每 {args.every:.2f} 秒問一次　來源 {args.url}")
    print(f"寫到 {out}")
    print(f"模式：{'每一次都記' if args.all else '只記有變動的'}\n")

    n_poll = n_row = n_err = 0
    last_key = None
    last_t = None
    errs = {}
    try:
        while True:
            t0 = time.time()
            wall = datetime.datetime.now()
            if wall >= stop_at:
                break
            n_poll += 1
            try:
                s = fetch()
            except Exception as e:
                n_err += 1
                errs[type(e).__name__] = errs.get(type(e).__name__, 0) + 1
                time.sleep(args.every)
                continue

            c = s.get("chips") or {}
            key = (c.get("price"), c.get("bid"), c.get("ask"))
            if not args.all and key == last_key:
                time.sleep(max(0.0, args.every - (time.time() - t0)))
                continue

            row = {"t": wall.isoformat(timespec="milliseconds"),
                   # 跟上一列差幾毫秒 —— 事後看得出取樣有多密、有沒有斷過
                   "ms": None if last_t is None else round((wall - last_t).total_seconds() * 1000),
                   "clock": s.get("clock"),          # 面板自己的時鐘
                   "age": s.get("age_sec"),          # 最後一筆報價幾秒前收到的
                   "quote": s.get("quote"),          # live / nodata / closed
                   **{k: c.get(k) for k in CHIP_KEYS}}
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()                                 # 中途被關掉也不會整份不見
            n_row += 1
            last_key, last_t = key, wall

            if n_row % 100 == 0:
                print(f"  {wall:%H:%M:%S}  已記 {n_row} 筆（問了 {n_poll} 次）"
                      f"　現價 {c.get('price')}")

            time.sleep(max(0.0, args.every - (time.time() - t0)))
    except KeyboardInterrupt:
        print("\n（手動中止）")
    finally:
        f.close()

    print(f"\n收工 {datetime.datetime.now():%H:%M:%S}")
    print(f"  問了 {n_poll} 次，記下 {n_row} 筆")
    if n_err:
        print(f"  ⚠️ 有 {n_err} 次讀不到面板：{errs}")
        print("     （面板重啟或忙碌時會這樣；那幾個瞬間的報價就是沒有錄到）")
    print(f"  檔案：{out}（{out.stat().st_size / 1024:.0f} KB）")


if __name__ == "__main__":
    main()
