# -*- coding: utf-8 -*-
"""
研究用全天五檔（depth_writer.py ＋ live_panel 的接線）離線測試 —— 不連永豐、不碰真的 depth_logs/。

守的東西：
  ① push() 只 append：磁碟再慢也拖不到 on_bidask；壞掉的報價物件不會丟例外
  ② 格式：陣列列、價格／量轉成數字；夜盤過午夜分到下一天的檔；一定是 append
  ③ 佇列滿了留痕跡，不是安靜地少
  ④ 壓縮：只壓「以前的日子、沒開著、超過 1 小時沒動」的；壓完解得回原樣才刪原檔
  ⑤ ⛔⛔ 接線：大台／選擇權的五檔絕對不可以進 Today（停損看的價）
  ⑥ depth_subscribe：挑近月大台＋最近到期（> 今天）價平上下 10 個履約價 × 買賣權；出錯不往外丟
"""
import ast
import datetime as dt
import gzip
import json
import os
import pathlib
import shutil
import sys
import tempfile
import time
from decimal import Decimal

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.stdout.reconfigure(encoding="utf-8")

import depth_writer as DW

FAIL = 0


def chk(name, got, want):
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(("  ✅ " if ok else "  ❌ ") + name + ("" if ok else "：拿到 %r，要 %r" % (got, want)))


class BA:
    def __init__(self, code, ts, px=100.0):
        self.code, self.datetime = code, ts
        self.bid_price = [Decimal(str(px - i)) for i in range(5)]
        self.bid_volume = [i + 1 for i in range(5)]
        self.ask_price = [Decimal(str(px + 1 + i)) for i in range(5)]
        self.ask_volume = [10 + i for i in range(5)]


def lines(p):
    return [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


tmp = pathlib.Path(tempfile.mkdtemp(prefix="depthtest-"))
try:
    # ================================================================ ①
    print("\n=== ① push() 只 append，不碰磁碟、不丟例外 ===")
    w = DW.DepthWriter(tmp / "a", flush_every=3600, log=lambda m: None)   # 寫檔執行緒不跑
    ts = dt.datetime(2026, 9, 29, 9, 0, 0, 123000)
    t0 = time.perf_counter()
    for i in range(20000):
        w.push(BA("TXFJ6", ts))
    per = (time.perf_counter() - t0) / 20000 * 1e3
    chk("  單筆 < 0.05ms（實際 %.4fms）" % per, per < 0.05, True)
    chk("  還沒寫任何檔（push 不碰磁碟）", (tmp / "a").exists(), False)
    w.push(object())                       # 沒有任何欄位的怪東西
    w.push(None)
    chk("  壞掉的報價物件不丟例外、不進佇列", len(w._q), 20000)

    # ================================================================ ②
    print("\n=== ② 格式、跨午夜分檔、append ===")
    w = DW.DepthWriter(tmp / "b", log=lambda m: None)
    w.push(BA("TXO33900J6", dt.datetime(2026, 9, 29, 23, 59, 59, 999000), px=250.5))
    w.push(BA("TMFJ6", dt.datetime(2026, 9, 30, 0, 0, 0, 1000)))
    w.stop()
    f1, f2 = tmp / "b" / "2026-09-29.jsonl", tmp / "b" / "2026-09-30.jsonl"
    chk("  夜盤過午夜分成兩個檔", (f1.exists(), f2.exists()), (True, True))
    L = lines(f1)
    chk("  第一列是檔頭", json.loads(L[0])["k"], "h")
    row = json.loads(L[1])
    chk("  資料列是陣列", row, ["TXO33900J6", "23:59:59.999", [250.5, 249.5, 248.5, 247.5, 246.5],
                             [1, 2, 3, 4, 5], [251.5, 252.5, 253.5, 254.5, 255.5], [10, 11, 12, 13, 14]])
    chk("  整數價格不帶 .0", json.loads(lines(f2)[1])[2][0], 100)
    w = DW.DepthWriter(tmp / "b", log=lambda m: None)
    w.push(BA("TMFJ6", dt.datetime(2026, 9, 30, 0, 0, 5)))
    w.stop()
    chk("  重啟是接著寫（2 檔頭＋2 資料）", len(lines(f2)), 4)
    chk("  今天每個合約的筆數", w.stats()["by_code"], {"TMFJ6": 1})

    # ================================================================ ③
    print("\n=== ③ 佇列滿了留痕跡 ===")
    msgs = []
    w = DW.DepthWriter(tmp / "c", max_queue=3, flush_every=3600, log=msgs.append)
    for i in range(5):
        w.push(BA("TXFJ6", ts))
    chk("  丟了 2 筆", w.dropped, 2)
    w._safe_drain()
    w._close()
    L = lines(tmp / "c" / "2026-09-29.jsonl")
    mark = [json.loads(l) for l in L if l.startswith("{") and json.loads(l)["k"] == "x"]
    chk("  檔案裡有痕跡列 n=2", [m["n"] for m in mark], [2])
    chk("  主控台有警告", any("丟掉 2 筆" in m for m in msgs), True)

    # ================================================================ ④
    print("\n=== ④ 壓縮寫完的那幾天 ===")
    d = tmp / "d"
    d.mkdir()
    old = d / "2026-09-24.jsonl"
    old.write_text('{"k":"h"}\n["TXFJ6","09:00:00.000",[1],[1],[2],[1]]\n', encoding="utf-8")
    fresh = d / "2026-09-25.jsonl"
    fresh.write_text("x\n", encoding="utf-8")
    today = d / "2026-09-29.jsonl"
    today.write_text("y\n", encoding="utf-8")
    past = time.time() - 7200
    os.utime(old, (past, past))
    os.utime(today, (past, past))
    w = DW.DepthWriter(d, log=lambda m: None, today_fn=lambda: dt.date(2026, 9, 29))
    n = w.compress_old()
    chk("  只壓了 1 個", n, 1)
    chk("  舊的壓成 .gz、原檔刪掉", (old.exists(), (d / "2026-09-24.jsonl.gz").exists()), (False, True))
    chk("  解得回原樣", gzip.decompress((d / "2026-09-24.jsonl.gz").read_bytes()).decode("utf-8").count("\n"), 2)
    chk("  剛動過的不壓", fresh.exists(), True)
    chk("  今天的不壓", today.exists(), True)
    old.write_bytes(b"again\n")        # 同一天被重啟又寫了一段（用位元組：Windows 的 write_text 會換成 CRLF）
    os.utime(old, (past, past))
    w.compress_old()
    chk("  同一天第二段接在 .gz 後面", gzip.decompress((d / "2026-09-24.jsonl.gz").read_bytes())
        .decode("utf-8").endswith("again\n"), True)

    # ================================================================ ⑤
    print("\n=== ⑤ ⛔⛔ 接線：別的合約不可以進 Today ===")
    src = (HERE / "live_panel.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    ob = fns["on_bidask"]
    seg = ast.get_source_segment(src, ob)
    i_push, i_gate, i_feed = seg.find("DEPTH.push(ba)"), seg.find("_is_main_code("), seg.find("feed_quote(")
    chk("  on_bidask：先錄（push）→ 再擋（_is_main_code）→ 才餵 Today", 0 <= i_push < i_gate < i_feed, True)
    chk("  on_bidask：擋下來就 return", "return" in seg[i_gate:i_feed], True)
    chk("  on_bidask：TICKS.bidask 也在擋之後", i_gate < seg.find("TICKS.bidask("), True)
    ot = ast.get_source_segment(src, fns["on_tick"])
    chk("  on_tick：先擋再 feed", 0 <= ot.find("_is_main_code(") < ot.find(".feed("), True)
    # 把 _is_main_code 抽出來真的跑
    ns = {"PRODUCT": "TMF"}
    exec(ast.get_source_segment(src, fns["_is_main_code"]), ns)
    im = ns["_is_main_code"]
    chk("  微台 ⇒ 是", im("TMFJ6"), True)
    chk("  大台 ⇒ 不是", im("TXFJ6"), False)
    chk("  選擇權 ⇒ 不是", im("TXO33900J6"), False)
    chk("  代碼讀不到 ⇒ 當成是（跟以前一樣）", (im(None), im("")), (True, True))
    chk("  connect() 有呼叫 depth_subscribe", "depth_subscribe(api, contract)" in
        ast.get_source_segment(src, fns["connect"]), True)

    # ================================================================ ⑥
    print("\n=== ⑥ depth_subscribe 挑合約（假 api）===")
    import types

    class C:
        def __init__(self, code, **kw):
            self.code = code
            self.__dict__.update(kw)

    class Right:
        def __init__(self, s):
            self.s = s

        def __str__(self):
            return self.s

    futs = [C("TXFR1", delivery_month="202610"), C("TXFK6", delivery_month="202611"),
            C("TXFJ6", delivery_month="202610")]
    opts = []
    for exp in (dt.date(2026, 9, 29), dt.date(2026, 10, 21), dt.date(2026, 11, 18)):
        for k in range(30000, 40001, 100):
            for r in ("C", "P"):
                opts.append(C("TXO%d%s%s" % (k, r, exp.month), delivery_date=exp, strike_price=float(k),
                              option_right=Right(r)))
    subd = []

    class Quote:
        def subscribe(self, c, **kw):
            subd.append(c.code)

        def unsubscribe(self, c, **kw):
            subd.remove(c.code)

    api = types.SimpleNamespace(
        Contracts=types.SimpleNamespace(Futures=types.SimpleNamespace(TXF=futs),
                                        Options=types.SimpleNamespace(TXO=opts)),
        quote=Quote(), snapshots=lambda cs: [types.SimpleNamespace(close=34567.0)])
    fake_sj = types.ModuleType("shioaji")
    fake_sj.constant = types.SimpleNamespace(QuoteType=types.SimpleNamespace(BidAsk="BA"),
                                             QuoteVersion=types.SimpleNamespace(v1="v1"))
    sys.modules.setdefault("shioaji", fake_sj)
    real_sj = sys.modules["shioaji"]
    sys.modules["shioaji"] = fake_sj
    ns = {"date": dt.date, "datetime": dt.datetime, "DEPTH_OPT_STRIKES": 10, "print": lambda *a, **k: None,
          "DEPTH_SUBS": {"contracts": [], "codes": [], "paused_day": None, "err": None, "at": None}}
    for name in ("depth_subscribe", "depth_pause"):
        exec(ast.get_source_segment(src, fns[name]), ns)
    ds = ns["depth_subscribe"]
    got = ds(api, C("TMFJ6", delivery_month="202610"), today=dt.date(2026, 9, 29))
    codes = [c.code for c in got]
    chk("  第一個是同交割月的大台（不是 R1）", codes[0], "TXFJ6")
    chk("  20 個選擇權", len(codes) - 1, 20)
    chk("  到期日 > 今天（當天到期的不要）", {c.delivery_date for c in got[1:]}, {dt.date(2026, 10, 21)})
    chk("  履約價是 34567 附近 10 個", sorted({c.strike_price for c in got[1:]}),
        [float(k) for k in range(34100, 35001, 100)])
    chk("  真的有訂", len(subd), 21)
    ns["depth_pause"](api, "測試")
    chk("  流量太高 ⇒ 退訂全部研究用合約", subd, [])
    chk("  同一天不再訂", ds(api, C("TMFJ6", delivery_month="202610")), [])
    ns["DEPTH_SUBS"]["paused_day"] = None
    bad = types.SimpleNamespace(Contracts=None, quote=Quote())
    chk("  出錯不往外丟", ds(bad, C("TMFJ6"), today=dt.date(2026, 9, 29)), [])
    chk("  錯誤有記下來（畫面看得到）", bool(ns["DEPTH_SUBS"]["err"]), True)
    sys.modules["shioaji"] = real_sj
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n" + ("全部通過" if not FAIL else "❌ %d 項失敗" % FAIL))
sys.exit(1 if FAIL else 0)
