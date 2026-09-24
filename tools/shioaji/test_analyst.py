# -*- coding: utf-8 -*-
"""
【交易分析師】analyst.py 的探針 —— 離線、全部在暫存資料夾、⛔ 不碰真的 analyst/。
在守的事：
  ① 每則新聞一定要有 https 來源（Benson 交代）；沒有就不准發佈
  ② 預測漲跌／進出場方向／勝率／期望值 這類字眼擋下來（面板鐵律的例外只開給「經營層面」建議）
  ③ 建議最多 3 條、每條要有標籤／根據／要他決定什麼
  ④ 數字一律用 facts（AI 那份就算塞了數字也不會進到 facts 區）
  ⑤ 讀過：讀的時間 ≥ 產生時間才算；只會往讀過走；手機那份併得進來；重新產生會變回未讀
用法：python test_analyst.py
"""
import json
import pathlib
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import analyst as A  # noqa: E402

FAILS = []


def chk(name, got, want):
    ok = got == want
    print(("  OK     " if ok else "  FAIL   ") + name + ("" if ok else "  (得到 %r，期待 %r)" % (got, want)))
    if not ok:
        FAILS.append(name)


def good_ai():
    return {"verdict": {"lamp": "wn", "line": "兩條策略都正常，下週三凌晨聯準會決議落在夜盤持倉時間。"},
            "news": [{"date": "09/23", "tag": "data", "title": "美國 CPI 高於預期", "summary": "費半收跌。",
                      "impacts": [{"who": "夜盤跟勢", "text": "當晚過門檻做空。"}],
                      "sources": [{"title": "Reuters", "url": "https://www.reuters.com/x"}]}],
            "calendar": [{"when": "09/30 三", "event": "聯準會決議", "tag": "data", "history": "16 晚出手 9 次"}],
            "env": [{"tag": "judge", "title": "台指越來越像台積電指數", "text": "權重超過四成。"}],
            "recs": [{"tag": "data", "title": "決議那晚照常", "body": "過去 9 筆仍為正，筆數偏少。", "ask": "照常或手動關夜盤"}]}


TMP = pathlib.Path(tempfile.mkdtemp(prefix="analyst-"))
RD, RF = TMP / "reports", TMP / "read.json"
try:
    print("── ① 來源")
    chk("好的那份通過", A.validate(good_ai()), [])
    ai = good_ai(); ai["news"][0]["sources"] = []
    chk("沒有來源 ⇒ 擋", any("沒有來源" in e for e in A.validate(ai)), True)
    ai = good_ai(); ai["news"][0]["sources"] = [{"title": "x", "url": "http://a.com"}]
    chk("http（不是 https）⇒ 擋", any("沒有來源" in e for e in A.validate(ai)), True)
    ai = good_ai(); ai["news"] = []
    chk("一則新聞都沒有 ⇒ 擋", any("news 至少" in e for e in A.validate(ai)), True)

    print("── ② 禁用字眼")
    for txt in ("下週台指可望大漲", "建議做空", "這條勝率 60%", "期望值 +30", "目標價 25000", "明天恐跌"):
        ai = good_ai(); ai["env"][0]["text"] = txt
        chk("「%s」⇒ 擋" % txt, bool(A.validate(ai)), True)
    ai = good_ai(); ai["env"][0]["text"] = "過去波動在後 40% 的時期，這條每筆 +71（38 筆）。"
    chk("講歷史數字＋筆數 ⇒ 不擋", A.validate(ai), [])

    print("── ③ 建議")
    ai = good_ai(); ai["recs"] = ai["recs"] * 4
    chk("4 條 ⇒ 擋", any("最多 3" in e for e in A.validate(ai)), True)
    ai = good_ai(); ai["recs"][0]["ask"] = ""
    chk("沒寫要他決定什麼 ⇒ 擋", bool(A.validate(ai)), True)
    ai = good_ai(); ai["recs"][0]["tag"] = "maybe"
    chk("標籤不是 data／judge ⇒ 擋", bool(A.validate(ai)), True)

    print("── ④ 合成：數字只用 facts")
    facts = {"id": "2026-W39", "range": "09/21～09/25", "strategies": [{"name": "多方聯軍", "week_real_pts": -239}],
             "risk": {"pnl": -239, "cap": 800}}
    ai = good_ai(); ai["facts"] = {"strategies": [{"name": "多方聯軍", "week_real_pts": 999}]}
    ok, p = A.publish(facts, ai, reports_dir=RD)
    chk("發佈成功", ok, True)
    r = A.load("2026-W39", RD)
    chk("策略數字是 facts 那一份（−239，不是 AI 塞的 999）", r["facts"]["strategies"][0]["week_real_pts"], -239)
    ok, errs = A.publish({"id": "bad"}, good_ai(), reports_dir=RD)
    chk("facts 沒有正確 id ⇒ 不發佈", ok, False)
    ai = good_ai(); ai["news"][0]["sources"] = []
    ok, errs = A.publish(facts, ai, reports_dir=RD)
    chk("ai 沒過檢查 ⇒ 不發佈", ok, False)

    print("── ⑤ 讀過")
    made = r["made_at"]
    it = A.index(RD, RF)[0]
    chk("剛發佈 ⇒ 未讀", it["read"], False)
    chk("面板讀過 ⇒ True", A.mark_read("2026-W39", "pc", RF), True)
    chk("列表變讀過", A.index(RD, RF)[0]["read"], True)
    chk("再讀一次（時間沒變新）⇒ False", A.mark_read("2026-W39", "pc", RF, at="2000-01-01T00:00:00"), False)
    chk("⛔ 舊時間蓋不回去（還是讀過）", A.index(RD, RF)[0]["read"], True)
    # 重新產生（made_at 變新）⇒ 變回未讀
    r2 = dict(r); r2["made_at"] = "2099-01-01T00:00:00"
    (RD / "2026-W39.json").write_text(json.dumps(r2, ensure_ascii=False), encoding="utf-8")
    chk("同一週重新產生 ⇒ 變回未讀", A.index(RD, RF)[0]["read"], False)
    n = A.merge_phone({"read": {"2026-W39": "2099-01-02T08:00:00", "亂寫": "x", "2026-W40": 5}}, read_file=RF)
    chk("手機那份併進來（壞的跳過）", n, 1)
    chk("手機讀過 ⇒ 列表讀過", A.index(RD, RF)[0]["read"], True)
    chk("via 記成 phone", A.read_map(RF)["2026-W39"]["via"], "phone")
    chk("壞 payload 不炸", A.merge_phone("亂七八糟", read_file=RF), 0)

    print("── ⑥ 手機全文包")
    b1, b2 = A.bundle(RD), A.bundle(RD)
    chk("內容沒變 ⇒ 指紋一樣（不重傳）", b1["hash"], b2["hash"])
    chk("帶到週報全文", len(b1["reports"]), 1)

    print("── ⑦ 真的 analyst/ 沒被碰")
    chk("測試沒有寫到真的 read.json", A.READ_FILE.exists() and "2099" in A.READ_FILE.read_text(encoding="utf-8"), False)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print()
print("全部通過 ✅" if not FAILS else "⛔ %d 項失敗" % len(FAILS))
sys.exit(1 if FAILS else 0)
