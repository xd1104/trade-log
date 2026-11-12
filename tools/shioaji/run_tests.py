# -*- coding: utf-8 -*-
"""
一鍵跑全部測試（2026-11-12，健檢報告 KQMRS2RKX7cDbXYHKjTtv7 整理第 1 批）。

跑法（在 trade-log 資料夾或工作分支）：
    .venv\\Scripts\\python.exe -X utf8 tools\\shioaji\\run_tests.py          # 全部（⛔ 不含會連永豐的）
    .venv\\Scripts\\python.exe -X utf8 tools\\shioaji\\run_tests.py night    # 檔名含 night 的
    ... run_tests.py --sim                                                  # 連會登入永豐模擬帳戶的那三支也跑

判定：
  過      ＝結束碼 0、沒有任何 FAIL／✗ 行
  已知    ＝失敗的每一行都在 KNOWN 清單裡（清單寫了為什麼、誰定的）⇒ 不算新壞掉
  ⛔ 壞了 ＝其他（含逾時、當掉）⇒ 結束碼 1
另外守一件事（09-01 假交易寫進真成績單那類）：跑之前、之後比對真的資料檔（成績單、送單紀錄、夜盤帳本、口數狀態），
測試期間有被改到就大聲講（⚠ 面板開著時它自己也會寫，看時間判斷）。
⛔ 這支只跑測試，不送單、不連永豐（*_sim.py 要 --sim 才跑）。
"""
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.stdout.reconfigure(encoding="utf-8")
PY = sys.executable
TIMEOUT_S = 900
WORKERS = 4

# 會登入永豐（模擬帳戶）的測試 ⇒ 預設不跑
SIM_TESTS = {"test_broker_sim.py", "test_mini_sim.py", "test_mix_sim.py"}

# 已知會失敗的項目：{檔名: ([那一行裡一定有的字, ...], 為什麼)}。⛔ 加一筆要寫原因；修好了就刪掉。
KNOWN = {
    "test_qa_mix.py": (["QA-G"], "IOC 狀態先到、明細晚到又部位慢 ⇒ 會平過頭；PM 接受（CLAUDE.md 混搭品管段）"),
    "test_qa2_mix.py": (["QA2-1 ", "QA2-1b", "QA2-6", "QA2-7", "QA2-4 開關關 q3_tp_partial_stop"],
                        "QA2-1／6／7 是 main 的 _close_multi 範圍（CLAUDE.md 混搭品管段）；"
                        "QA2-4 q3_tp_partial_stop 偶爾只差進場秒數（兩邊各跑一次跨秒），不是行為不同"),
    "test_qa3_mix.py": (["QA3-5"], "品管攻擊測試留下的已知項（11-06 起就這樣，跟現在下單無關）"),
    "test_qa5_mix.py": (["Q5-1"], "明細永遠不回報那種極端情況成績單拆兩列；Benson 接受（Q5-1）"),
    "test_eod_expiry.py": (["沒驗到", "2026-02-23", "結算日 13:28:30", "結算日 13:43:30"],
                           "測試本身過時（11-05 健檢：用面板真的資料核對過 11-18、12-16 結算日都判得出）"),
    "test_sim_lanes.py": (["分頁列恰好五顆", "on_tick 跟基準", "main() 拿掉那一行"],
                          "測試本身過時：分頁後來多了「系統」、主程式改過，基準 a71087e 已經舊了"),
    "test_strategy_lab.py": (["tick_hist/days.jsonl", "09:03:00 ±130", "Traceback", "TypeError"],
                             "要的資料檔 tick_hist/days.jsonl 沒進程式庫（先跑 strategy_lab.py 才有）"),
    "test_sync_audit.py": (["有網址 ⇒ 跳一下", "推送成功 ⇒ 心跳一次"],
                           "心跳網址檔不進版控，工作分支沒有那個檔 ⇒ 這兩項只在面板資料夾驗得到"),
}

FAIL_RE = re.compile(r"^\s*(FAIL\b|✗)|^Traceback", re.M)
GUARD = ["real_trades", "real_orders", "nightfire", "LOTS_LEVEL.json", "sim_trades"]


def snapshot():
    out = {}
    for name in GUARD:
        p = HERE / name
        files = [p] if p.is_file() else (list(p.rglob("*")) if p.is_dir() else [])
        for f in files:
            if f.is_file():
                st = f.stat()
                out[str(f.relative_to(HERE))] = (st.st_size, st.st_mtime)
    return out


def run_one(path):
    t0 = time.time()
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        p = subprocess.run([PY, "-X", "utf8", str(path)], cwd=str(HERE), env=env,
                           capture_output=True, timeout=TIMEOUT_S)
        text = (p.stdout + p.stderr).decode("utf-8", "replace")
        code = p.returncode
    except subprocess.TimeoutExpired:
        text, code = "逾時 %d 秒" % TIMEOUT_S, "timeout"
    return path.name, code, text, time.time() - t0


def judge(name, code, text):
    bad = [l.strip() for l in text.splitlines() if FAIL_RE.search(l)]
    bad = [l for l in bad if not re.match(r"^\d+ FAIL: \[", l)]      # 檔尾的總結行
    if code == 0 and not bad:
        return "過", []
    if code == "timeout":
        return "⛔ 壞了", ["逾時"]
    keys, _why = KNOWN.get(name, ([], ""))
    new = [l for l in bad if not any(k in l for k in keys)]
    if not new and (bad or code != 0):
        if not bad:                                   # 結束碼非 0 但沒有 FAIL 行 ⇒ 看不出原因 ⇒ 不放過
            return "⛔ 壞了", ["結束碼 %s、沒有 FAIL 行（當掉？）" % code]
        return "已知", bad
    return "⛔ 壞了", new or ["結束碼 %s" % code]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    sim = "--sim" in sys.argv
    tests = sorted(HERE.glob("test_*.py"))
    if args:
        tests = [t for t in tests if any(a in t.name for a in args)]
    skipped = [t.name for t in tests if t.name in SIM_TESTS and not sim]
    tests = [t for t in tests if sim or t.name not in SIM_TESTS]
    print("跑 %d 支測試（%d 支同時跑）%s" % (len(tests), WORKERS,
          "；略過會連永豐的 %s（要跑加 --sim）" % "、".join(skipped) if skipped else ""), flush=True)
    before = snapshot()
    t0 = time.time()
    rows = []
    with ThreadPoolExecutor(WORKERS) as ex:
        for name, code, text, sec in ex.map(run_one, tests):
            verdict, lines = judge(name, code, text)
            rows.append((name, verdict, lines, sec))
            print("  %-6s %-28s %5.0f 秒" % (verdict, name, sec), flush=True)
    after = snapshot()
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    n = {v: sum(1 for r in rows if r[1] == v) for v in ("過", "已知", "⛔ 壞了")}
    print("\n總共 %d 支：過 %d、已知 %d、⛔ 壞了 %d（%.0f 秒）" % (len(rows), n["過"], n["已知"], n["⛔ 壞了"], time.time() - t0))
    for name, verdict, lines, _s in rows:
        if verdict == "⛔ 壞了":
            print("\n⛔ %s" % name)
            for l in lines[:8]:
                print("    " + l[:200])
    known = [r for r in rows if r[1] == "已知"]
    if known:
        print("\n已知（不算新壞掉）：")
        for name, _v, lines, _s in known:
            print("  %s：%d 項 —— %s" % (name, len(lines), KNOWN[name][1]))
    if changed:
        print("\n⚠️ 測試期間真的資料檔有變動（面板開著時它自己也會寫；不是面板就是測試寫錯地方）：")
        for k in changed[:20]:
            print("    " + k)
    return 1 if n["⛔ 壞了"] else 0


if __name__ == "__main__":
    sys.exit(main())
