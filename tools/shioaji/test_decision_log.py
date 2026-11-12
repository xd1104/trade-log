# -*- coding: utf-8 -*-
"""
【每日判斷快照】decision_log.py 的離線探針。⛔ 只用假數字（價格一律 12000 附近）、
⛔ 所有路徑導到暫存資料夾、⛔ 不連永豐、⛔ 不送單。
跑法：.venv\\Scripts\\python.exe tools\\shioaji\\test_decision_log.py
"""
import ast
import json
import pathlib
import shutil
import sys
import tempfile
from datetime import date, datetime

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.stdout.reconfigure(encoding="utf-8")

if (HERE / "AUTO_ORDERS_ON").exists() or (HERE / "NIGHT_ORDERS_ON").exists():
    print("⛔ 這個資料夾有真的開關檔 —— 拒跑（請把 tools/shioaji 複製到暫存區再跑）")
    sys.exit(2)

import auto_fire as AF        # noqa: E402
import night_fire as NF       # noqa: E402
import decision_log as DL     # noqa: E402

FAIL = 0
N = 0


def chk(name, got, want):
    global FAIL, N
    N += 1
    if got == want:
        print("  ok   " + name)
    else:
        FAIL += 1
        print("  FAIL %s：got %r want %r" % (name, got, want))


def say(cond, name):
    chk(name, bool(cond), True)


TMP = pathlib.Path(tempfile.mkdtemp(prefix="dlog-test-"))
REAL = {"LOG": DL.LOG_DIR, "FIRE": AF.FIRE_DIR, "NF": NF.NF_DIR}
DL.LOG_DIR = TMP / "decision_log"
DL.TICK_DIR = TMP / "tick_logs"
AF.FIRE_DIR = TMP / "autofire"
AF.FAST_HIST = TMP / "fast_hist.jsonl"
AF.ORB_HIST = TMP / "orb_hist.jsonl"
AF.ARM_FLAG = TMP / "AUTO_ORDERS_ON"
NF.NF_DIR = TMP / "nightfire"
NF.ARM_FLAG = TMP / "NIGHT_ORDERS_ON"
NF.TREND_CTX = TMP / "trend_ctx.json"
NF.TSM_CTX = TMP / "tsm_ctx.json"
AF._CFG["pctl"] = 80          # 面板 main() 裡 auto_fire.configure(pctl=FAST_PCTL) 接的同一個數
for p in (DL.TICK_DIR, AF.FIRE_DIR, NF.NF_DIR):
    p.mkdir(parents=True)

try:
    # ══ ① 逐筆檔的洞 ══════════════════════════════════════════════════
    print("\n=== ① tick_holes ===")
    lines = ['{"k":"h","v":1}']
    t = 8 * 3600 + 45 * 60 + 10          # 開盤 10 秒後才有第一筆（不算洞）
    while t < 9 * 3600 + 30 * 60 - 5:
        if 9 * 3600 + 10 * 60 <= t < 9 * 3600 + 11 * 60:   # 09:10~09:11 沒成交 ⇒ 一個 60 秒的洞
            t += 1
            continue
        hh, mm, ss = t // 3600, t % 3600 // 60, t % 60
        lines.append('{"k":"t","t":"%02d:%02d:%02d.000","p":12000.0,"v":1}' % (hh, mm, ss))
        lines.append('{"k":"b","t":"%02d:%02d:%02d.100","b":11999.0,"a":12001.0}' % (hh, mm, ss))
        t += 2
    lines.append('{"k":"x","why":"drop","n":3}')
    p = DL.TICK_DIR / "2030-01-07.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    h = DL.tick_holes(p)
    chk("找到一個洞", len(h["holes"]), 1)
    chk("洞的起迄", h["holes"][0][:2], ["09:09:58.000", "09:11:00.000"])
    chk("痕跡列", h["marks"], 1)
    chk("第一筆", h["first"], "08:45:10.000")
    h2 = DL.tick_holes(DL.TICK_DIR / "沒有這個檔.jsonl")
    chk("沒有檔 ⇒ exists False", h2["exists"], False)
    # 頭尾洞
    p3 = DL.TICK_DIR / "x.jsonl"
    p3.write_text('{"k":"t","t":"08:50:00.000","p":12000,"v":1}\n{"k":"t","t":"09:00:00.000","p":12000,"v":1}\n',
                  encoding="utf-8")
    h3 = DL.tick_holes(p3)
    chk("頭、中、尾三個洞", len(h3["holes"]), 3)

    # ══ ② 歷史漏記 ════════════════════════════════════════════════════
    print("\n=== ② hist_gaps（11-02 那個坑）===")
    fd = ["2030-01-0%d" % i for i in (1, 2, 3, 4, 7, 8)]
    od = ["2030-01-01", "2030-01-02", "2030-01-07"]
    g = DL.hist_gaps("2030-01-09", fd, od, tick_dates=["2030-01-09", "2030-01-08", "2030-01-06"])
    chk("orb_hist 漏的日子", g["orb_missing"], ["2030-01-03", "2030-01-04", "2030-01-08"])
    chk("fast_hist 漏的日子（逐筆有、走幅沒有）", g["fast_missing"], ["2030-01-06"])
    chk("今天本身不算", "2030-01-09" in g["orb_missing"], False)

    # ══ ③ 日盤一列：三個都不做 ════════════════════════════════════════
    print("\n=== ③ day_row：三個候選都不做 ===")
    D = "2030-01-09"
    fast_rows = [{"date": "2029-12-%02d" % i, "move_pct": 0.1 + i * 0.01, "ref": 12000.0, "px": 12010.0}
                 for i in range(1, 31)] + [{"date": "2030-01-0%d" % i, "move_pct": 0.2, "ref": 12000.0,
                                             "px": 12010.0} for i in (2, 3, 6, 7, 8)]
    fast_rows.append({"date": D, "move_pct": 0.05, "ref": 12000.0, "px": 12006.0})
    orb_rows = [{"date": r["date"], "box_pct": 0.40, "hi": 12030.0, "lo": 11982.0, "fill": 12000.0}
                for r in fast_rows if r["date"] < D]
    led = {"date": D, "cand_rows": [
        {"cand": "fast", "rec": "skip", "why": "not_fast", "why_msg": "快攻：不夠快",
         "method": "U", "arm_raw": "U", "px": 12006.0, "quote_age_ms": 180, "quote_gaps": 0.0,
         "fast": {"verdict": "slow", "move_pct": 0.05, "move_pts": 6, "thr_pct": 0.29,
                  "thr_pts": 35, "n": 35}},
        {"cand": "orb", "rec": "skip", "why": "orb_narrow", "why_msg": "開箱：箱子太窄",
         "box_hi": 12020.0, "box_lo": 11990.0, "box_w": 30.0, "box_pct": 0.25, "box_med_pct": 0.40,
         "px": 12000.0, "at": "09:06:00", "d": 1, "orb_hist": {"n": 20, "span": 30}},
        {"cand": "rev", "rec": "skip", "why": "no_reversal", "why_msg": "純回馬：沒有反轉",
         "px_0903": 12006.0, "p15": 12030.0, "d_0903": 1, "d2": None}],
        "eod": {"why": "eod_no_entry", "why_msg": "沒有東西要平", "at": "13:43:30"}}
    tk = {"exists": True, "n": 100, "holes": [], "marks": 0}
    r = DL.day_row(D, led, fast_rows, orb_rows, tk, {"method": "U", "qty": 7})
    chk("做法取帳本那一列", r["method"], "U")
    chk("結論", r["decision"]["did"], "不做")
    chk("快攻差幾點（6−35）", r["legs"]["fast"]["diff_pts"], -29)
    chk("快攻差幾 %", r["legs"]["fast"]["diff_pct"], -0.24)
    chk("快攻來源＝帳本", r["legs"]["fast"]["src"], "ledger")
    chk("開箱箱寬差幾 %", r["legs"]["orb"]["diff_pct"], -0.15)
    chk("開箱差幾點（−0.15% × 12000）", r["legs"]["orb"]["diff_pts"], -18)
    chk("開箱不夠寬", r["legs"]["orb"]["wide_enough"], False)
    chk("純回馬走了幾點", r["legs"]["rev"]["move_pts"], 24.0)
    chk("純回馬沒反轉", r["legs"]["rev"]["reversed"], False)
    chk("收盤那一列有帶到", r["eod"]["why"], "eod_no_entry")
    chk("fast_hist 門檻用了 35 天（不含今天）", r["hist"]["fast"]["n"], 35)
    say(r["hist"]["fast"]["today_written"], "今天的走幅有寫進歷史")
    chk("今天的箱寬沒寫進歷史 ⇒ 標出來", "今天的箱子寬度沒有寫進歷史" in r["flags"], True)
    say(r["hist"]["gaps"]["orb_missing"] == [], "歷史沒有漏記")
    chk("沒有出錯", r["errs"], [])
    json.dumps(r, ensure_ascii=False)

    # ══ ④ 日盤一列：快攻做了 ══════════════════════════════════════════
    print("\n=== ④ day_row：快攻送出去了 ===")
    led2 = {"date": D, "cand_rows": [
        {"cand": "fast", "rec": "result", "ok": True, "why": "sent", "method": "U", "qty": 7,
         "fast": {"verdict": "fast", "move_pct": 0.40, "move_pts": 48, "thr_pct": 0.29, "thr_pts": 35,
                  "n": 35}, "quote_age_ms": 7000, "quote_gaps": 3.0}]}
    r2 = DL.day_row(D, led2, fast_rows, orb_rows, {"exists": True, "holes": [["09:01", "09:02", 60]],
                                                    "marks": 2}, {"method": "U", "qty": 3})
    chk("結論＝做", (r2["decision"]["did"], r2["decision"]["by"]), ("做", "fast"))
    chk("口數取帳本（不是現在的 3）", r2["qty"], 7)
    chk("差 +13 點", r2["legs"]["fast"]["diff_pts"], 13)
    say(any("報價已經 7.0 秒" in f for f in r2["flags"]), "報價不新鮮有標")
    say(any("逐筆有 1 個洞" in f for f in r2["flags"]), "逐筆洞有標")
    say(any("痕跡" in f for f in r2["flags"]), "痕跡列有標")

    # ══ ⑤ 日盤一列：帳本沒有（開關關著時還是看得到快攻判定）═══════════
    print("\n=== ⑤ day_row：帳本一列都沒有 ===")
    r3 = DL.day_row(D, None, fast_rows, orb_rows[:-3], None, {"method": None, "qty": 1})
    chk("結論", r3["decision"]["did"], "沒有紀錄")
    chk("快攻從歷史檔判（不快）", (r3["legs"]["fast"]["src"], r3["legs"]["fast"]["verdict"]), ("hist", "slow"))
    say(r3["legs"]["fast"]["thr_pts"] is not None, "歷史檔算得出門檻點數")
    chk("orb_hist 漏 3 天抓得到", len(r3["hist"]["gaps"]["orb_missing"]), 3)
    say(any("漏記 3 天" in f for f in r3["flags"]), "漏記有標")

    # ══ ⑥ 夜盤一列 ════════════════════════════════════════════════════
    print("\n=== ⑥ night_row ===")
    E = "2030-01-09"
    nr = DL.night_row(E, [{"E": E, "rec": "skip", "why": "not_fast", "msg": "沒到門檻", "method": "R",
                           "mv": 40.0, "thr": 90.0, "ref": 12000.0, "sig": 12040.0}],
                      {"vol": 1.1, "thr": 1.03, "high": True, "qty": 3})
    chk("跟勢不做", nr["decision"]["did"], "不做")
    chk("差 −50 點", nr["signal"]["diff"], -50.0)
    chk("單位是點", nr["signal"]["unit"], "pts")
    chk("沒寫波動 ⇒ 用 plan", (nr["vol"]["pred"], nr["vol"]["src"]), (1.1, "plan"))
    nr2 = DL.night_row(E, [{"E": E, "rec": "sending", "method": "R", "mv": -130.0, "thr": 90.0},
                           {"E": E, "rec": "result", "ok": True, "method": "R", "mv": -130.0,
                            "thr": 90.0, "dir": "short", "qty": 3, "vol": 1.2, "vol_thr": 1.03,
                            "vol_high": True, "px": 12000.0, "sl_points": 240.0},
                           {"E": E, "rec": "eod", "why": "closed", "msg": "平倉完成"}])
    chk("做空 3 口", (nr2["decision"]["did"], nr2["signal"]["dir"], nr2["qty"]), ("做", "short", 3))
    chk("|走幅|−門檻", nr2["signal"]["diff"], 40.0)
    chk("收盤", nr2["eod"]["why"], "closed")
    nr3 = DL.night_row(E, [{"E": E, "rec": "sending", "method": "R"}])
    chk("只有 sending ⇒ 送單中斷", nr3["decision"]["did"], "送單中斷")
    nr4 = DL.night_row(E, [{"E": E, "rec": "skip", "why": "off", "msg": "關著"}], None,
                       {"mv": 70.0, "thr": 100.0, "n": 40, "newest": "2030-01-08"})
    chk("關著 ⇒ 參考值", (nr4["signal"]["src"], nr4["signal"]["diff"]), ("reference", -30.0))
    nr5 = DL.night_row(E, [{"E": E, "rec": "skip", "why": "not_fast", "method": "T", "mv": 0.5, "thr": 0.8}])
    chk("台積電單位是 %", nr5["signal"]["unit"], "pct")

    # ══ ⑦ step()：端到端（假帳本、暫存資料夾）══════════════════════════
    print("\n=== ⑦ step() 端到端 ===")
    import risk_cap
    import lots
    _rb, _np = risk_cap.blocked, lots.night_plan
    risk_cap.blocked = lambda *a, **k: (False, "沒到上限", {})
    lots.night_plan = lambda *a, **k: {"qty": 1, "vol": 0.9, "thr": 1.03, "high": False}
    try:
        AF.FAST_HIST.write_text("".join(json.dumps(x) + "\n" for x in fast_rows), encoding="utf-8")
        AF.ORB_HIST.write_text("".join(json.dumps(x) + "\n" for x in orb_rows), encoding="utf-8")
        with (AF.FIRE_DIR / "2030-01.jsonl").open("w", encoding="utf-8") as f:
            for c in led["cand_rows"]:
                f.write(json.dumps(dict(c, date=D)) + "\n")
            f.write(json.dumps({"rec": "skip", "date": "2030-01-08", "why": "off", "why_msg": "關著",
                                "cand": "day"}) + "\n")
        with (NF.NF_DIR / "2030-01.jsonl").open("w", encoding="utf-8") as f:
            f.write(json.dumps({"E": "2030-01-08", "rec": "skip", "why": "not_fast", "method": "R",
                                "mv": 20.0, "thr": 80.0, "msg": "沒到"}) + "\n")
        now = datetime(2030, 1, 9, 9, 33, 0)
        n1 = DL.step(now)
        keys = DL.keys_written()
        chk("今天日盤寫了", (D, "day") in keys, True)
        chk("昨天日盤補寫（帳本有）", ("2030-01-08", "day") in keys, True)
        chk("昨晚夜盤補寫", ("2030-01-08", "night") in keys, True)
        chk("今晚夜盤還沒到 ⇒ 不寫", (D, "night") in keys, False)
        chk("沒帳本的日子不補寫", ("2030-01-07", "day") in keys, False)
        rows = [json.loads(x) for x in (DL.LOG_DIR / "2030-01.jsonl").read_text(encoding="utf-8").splitlines()]
        src = {(x["date"], x["sess"]): x["src"] for x in rows}
        chk("今天是 live、昨天是 catchup", (src[(D, "day")], src[("2030-01-08", "day")]), ("live", "catchup"))
        chk("第二輪不重寫", DL.step(now), 0)
        chk("第一輪寫的列數", n1, 3)
        # 09:31 以前不寫今天
        shutil.rmtree(DL.LOG_DIR)
        DL.step(datetime(2030, 1, 9, 9, 20, 0))
        chk("09:32 以前不寫今天", (D, "day") in DL.keys_written(), False)
        # 夜盤到了
        DL.step(datetime(2030, 1, 9, 23, 30, 0))
        chk("今晚判斷完 5 分鐘後寫夜盤", (D, "night") in DL.keys_written(), True)
        # 週末不寫
        shutil.rmtree(DL.LOG_DIR)
        DL.step(datetime(2030, 1, 12, 10, 0, 0))
        chk("週六不寫週六", ("2030-01-12", "day") in DL.keys_written(), False)

        # ══ ⑧ 出錯隔離 ════════════════════════════════════════════════
        print("\n=== ⑧ 出錯隔離 ===")
        shutil.rmtree(DL.LOG_DIR)
        _ra, _hr = AF.read_all, AF.hist_read
        AF.read_all = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        AF.hist_read = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom2"))
        try:
            DL.step(now)
            rows = [json.loads(x) for x in (DL.LOG_DIR / "2030-01.jsonl").read_text(encoding="utf-8").splitlines()]
            today = next(x for x in rows if x["date"] == D and x["sess"] == "day")
            say(any("read_all" in e for e in today["errs"]), "帳本讀不到 ⇒ 照寫一列並記錯")
            say(any("fast_hist" in e for e in today["errs"]), "走幅歷史讀不到也記錯")
        finally:
            AF.read_all, AF.hist_read = _ra, _hr
        _ap = DL.append
        DL.append = lambda row: (_ for _ in ()).throw(OSError("disk full"))
        try:
            shutil.rmtree(DL.LOG_DIR, ignore_errors=True)
            e0 = DL._ST["errors"]
            try:
                DL.step(now)
                raised = False
            except Exception:
                raised = True
            chk("寫檔失敗會往 step 外丟（由 _loop 接住）", raised, True)
            # _loop 的那一層
            import threading as _th
            DL.POLL_S = 0.05
            DL._ST["started"] = False
            DL.start()
            import time as _t
            _t.sleep(0.3)
            say(DL._ST["errors"] > e0, "背景執行緒接住例外、計數、沒死")
            say(any(t.name == "decision-log" and t.is_alive() for t in _th.enumerate()), "執行緒還活著")
        finally:
            DL.append = _ap
    finally:
        risk_cap.blocked, lots.night_plan = _rb, _np

    # ══ ⑨ 結構守衛：完全不在送單路徑上 ═══════════════════════════════
    print("\n=== ⑨ AST：decision_log 一張單都送不出去 ===")
    tree = ast.parse((HERE / "decision_log.py").read_text(encoding="utf-8"))
    bad_calls = {"enter", "close", "place_order", "place_target", "cancel_order", "disarm",
                 "fire_arm_on", "night_arm_on", "on_signal", "on_eod", "on_reversal", "_send", "_fire",
                 "_rev", "_eod", "_append", "_skip", "step_nf"}
    hits = sorted({n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Attribute) and n.func.attr in bad_calls
                   and getattr(n.func.value, "id", "") in ("AF", "NF", "broker", "auto_fire", "night_fire")})
    chk("沒有呼叫任何送單／改狀態的函式", hits, [])
    imps = sorted({a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                   for a in n.names} | {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)})
    chk("沒有 import broker／live_panel", [m for m in imps if m in ("broker", "live_panel")], [])
    opens = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "open"]
    modes = sorted({a.value for n in opens for a in n.args if isinstance(a, ast.Constant)})
    chk("開檔只有 a（寫）與 r（讀）", modes, ["a", "r"])
    say(not any((isinstance(n, ast.Name) and n.id == "state_lock") or
                (isinstance(n, ast.Attribute) and n.attr == "state_lock") for n in ast.walk(tree)),
        "不碰 state_lock（AST，註解不算）")
    # 接線：main() 裡用 try 包著 decision_log.start()
    lp = ast.parse(__import__("testkit").panel_source(HERE / "live_panel.py"))
    main = next(n for n in ast.walk(lp) if isinstance(n, ast.FunctionDef) and n.name == "main")
    wrapped = [t for t in ast.walk(main) if isinstance(t, ast.Try)
               and any(isinstance(c, ast.Call) and getattr(c.func, "attr", "") == "start"
                       and getattr(c.func.value, "id", "") == "decision_log" for c in ast.walk(t))]
    chk("main() 用 try 包著 decision_log.start()", len(wrapped), 1)
    MAIN_LOOP = ("_auto_tick", "_auto_tick_guarded", "check_real_position", "on_tick", "_auto_snap", "_auto_put")
    used = sorted({fn.name for fn in ast.walk(lp) if isinstance(fn, ast.FunctionDef) and fn.name in MAIN_LOOP
                   for x in ast.walk(fn) if isinstance(x, ast.Name) and x.id == "decision_log"})
    chk("主迴圈那幾支一個字都沒提 decision_log", used, [])
    gi = (HERE.parent.parent / ".gitignore").read_text(encoding="utf-8")
    say("tools/shioaji/decision_log/" in gi, ".gitignore 擋住 decision_log/")
    say(REAL["LOG"] != DL.LOG_DIR and not any(TMP in q.parents for q in [REAL["LOG"]]), "全程沒指回真的資料夾")
except Exception:
    import traceback
    traceback.print_exc()
    FAIL += 1
    print("  FAIL 測試本身崩潰")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("\n總結：%d 項，失敗 %d 項" % (N, FAIL))
sys.exit(1 if FAIL else 0)
