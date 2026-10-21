# -*- coding: utf-8 -*-
"""
探針：右上角鈴鐺「要注意的事」＋【系統】分頁＋【帳戶】改版（2026-10-21）。

跑法：python -X utf8 test_notice.py
"""
import sys
from pathlib import Path

import monitor_push as MP
import live_panel as LP

bad = 0


def chk(name, got, want):
    global bad
    ok = got == want
    if not ok:
        bad += 1
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok else "  got=%r want=%r" % (got, want)))


print("\n  ── 手機那份規則多一條：券商流量快用完 ──")
snap = {"real": {"position": None}, "day": {}, "night": {}, "risk": {}, "equity": {"usage": {"pct": 85.0}}}
T = MP._todo(snap, [], [])
chk("用了 85% ⇒ 有空再看", [(x["lvl"], x["text"]) for x in T], [("later", "券商流量快用完了（今天用了 85%）")])
snap["equity"]["usage"]["pct"] = 40.0
chk("用了 40% ⇒ 沒事", MP._todo(snap, [], []), [])

print("\n  ── 鈴鐺：跟手機同一份，再補口數金條裡手機沒列的 ──")
_orig = MP.notice
MP.notice = lambda port, max_age=None: (
    [{"lvl": "now", "text": "報價斷了 95 秒，手上的單現在沒人顧", "act": "打開大戶投自己看部位"},
     {"lvl": "decide", "text": "可以升到 6＋3 了（日盤 6 口、夜盤 3 口）", "act": "想升就到電腦面板按確認"},
     {"lvl": "later", "text": "資料備份失敗", "act": "回電腦時看一下"}],
    [{"kind": "offer", "text": "口數：條件都過了，可以升到 6＋3"},
     {"kind": "lifted", "text": "停損失靈的判法更正 ⇒ 暫停已解除。"},
     {"kind": "pos_fill", "text": "部位：成交口數一度對不上。"}])
try:
    V = LP.notice_view()
finally:
    MP.notice = _orig
I = V["items"]
chk("ok", V["ok"], True)
chk("手機三則都在、順序不變", [x["text"] for x in I[:3]],
    ["報價斷了 95 秒，手上的單現在沒人顧", "可以升到 6＋3 了（日盤 6 口、夜盤 3 口）", "資料備份失敗"])
chk("只有「有空再看」可以關", [x["hide"] for x in I[:3]], [False, False, True])
chk("口數金條：可以升級／部位那幾種手機已經有 ⇒ 不重複；暫停解除補進來", [x["text"] for x in I[3:]],
    ["停損失靈的判法更正 ⇒ 暫停已解除。"])
chk("補進來的那則：有空再看、鑰匙跟原本金條的 ✕ 同一把（kind|text）", (I[3]["lvl"], I[3]["key"], I[3]["hide"]),
    ("later", "lifted|停損失靈的判法更正 ⇒ 暫停已解除。", True))

print("\n  ── 畫面守門 ──")
src = (Path(__file__).parent / "live_panel.py").read_text(encoding="utf-8")
tabs = src[src.index('<div class="tabs">'):src.index('</div>', src.index('<div class="tabs">'))]
order = [t for t in ("data-tab=\"live\"", "data-tab=\"acct\"", "data-tab=\"sim\"", "data-tab=\"hc\"", "data-tab=\"sys\"", "data-tab=\"fire\"")]
chk("分頁順序：即時／帳戶／模擬／健檢／系統／自動下單（自動下單最右）", [tabs.index(o) for o in order] == sorted(tabs.index(o) for o in order), True)
chk("鈴鐺在重啟鈕與信件之間", src.index('id="anrst"') < src.index('id="anbell"') < src.index('id="anmail"'), True)
chk("鈴鐺讀 /api/notice", "fetch('/api/notice'" in src, True)
chk("自動下單頁不再畫口數金條", "ltBarHTML(D.lots); setEl('allotsbar','');" in src, True)
g = src[src.index("function goalHTML(G){"):src.index("function paintRight(")]
chk("目標卡：拿掉「照計畫…」那一行", "G.vs" in g, False)
chk("目標卡：拿掉「每月實際」表格與註解", ("每月實際" in g, "gl-notes" in g), (False, False))
chk("目標卡：金條那一句只畫平常那句", "if(LL&&LL.att) LL=LL.plain||null;" in g, True)
a = src[src.index("function acctHTML(s){"):src.index("function goalWan(")]
chk("帳戶總覽：流量／五檔／備份搬走", ("E.usage" in a, "E.depth" in a, "E.backup" in a), (False, False, False))
chk("帳戶總覽：本月不帶括號說明", "已扣掉出入金" in a, False)
chk("帳戶總覽：保證金不夠才出金字", "if(en.ok===false&&en.msg) warns.push(en.msg);" in a, True)
y = src[src.index("function sysHTML(s){"):src.index("const BELL=")]
chk("系統分頁：四張卡（連線／流量／五檔／備份）", all(k in y for k in ("券商連線", "券商流量", "五檔錄製", "資料備份")), True)
chk("系統分頁：異常亮小金點", "sb.classList.toggle('ltdot', sysWarn(s))" in src, True)

print("\n%s" % ("全部通過" if not bad else "❌ %d 項沒過" % bad))
sys.exit(1 if bad else 0)
