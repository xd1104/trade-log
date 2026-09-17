# -*- coding: utf-8 -*-
"""
【模擬】七條的**回填**（2026-09-17 加；Benson 要在面板上看到 2024-08 以來的模擬紀錄）。

    python backfill_sim.py [--from 2024-08-01] [--to YYYY-MM-DD] [--lanes fast,orb] [--dry]

面板背景只算「最近 10 個平日」，再往前的日子它一輩子不會回頭算（定論只 append、不重算）。
這支把**更早的日子**用同一份規則、同一份逐筆補齊，一次跑完就好。

⛔ **不連永豐、不下單、不寫 `fast_hist.jsonl`**（唯讀）。
   只讀本機的 `tick_hist/ticks/*.csv.gz`、`tmf_1min.csv`、`fast_hist.jsonl`，
   只寫 `sim_lanes/YYYY-MM.jsonl`。

⛔⛔ **規則一行都不自己寫**：逐筆那六條走 `sim_lanes.TICK_EVAL`、夜盤走 `sim_lanes.night_eval`，
   注入的正本跟面板 `live_panel.start_sim_lanes()` **逐字一樣**（auto_fire 的 fast_verdict／
   move_pct／tpsl_points／reversal_dir／hist_read ＋ live_panel 的 REV_SEC／FAST_PCTL／FAST_RULE）。
   在這裡另寫一份「>= 門檻」或「× 0.005」＝ 回填的成績跟面板從此分岔，⛔ 絕對不准。

⛔⛔ **不碰面板正在算的那幾天**：面板每分鐘會算最近 10 個平日並 append，
   兩個行程的鎖擋不住對方 ⇒ 上限一律夾在**面板窗口的前一天**（`--to` 指定得再晚也會被夾回來）。
   ⇒ 要補「最近 10 天」的唯一辦法是等面板自己算，⛔ 不是把這道閘門拆掉。

⛔ **每一列多一個 `calc: "backfill"`**（Benson 2026-09-17 裁示「要標」）：
   面板當天即時算的那些列**沒有**這個欄位。兩邊口徑一樣（同一份規則、同一份逐筆），
   差別是「事後重算」vs「當天跑出來的」—— 將來對帳查得到是哪一種。
   ⛔ 不做資料遷移：舊列沒有這個欄位就是「即時」。

⛔ **算不出來的日子不寫檔**（跟面板同一條鐵律）：沒有逐筆、夜盤 1 分 K 沒到齊 ⇒ 只印出來。
   ⚠️ 但「不做」是**定論**、照寫 —— 包含「走幅歷史不夠 20 天」：
   那是規則在那一天本來就會給的答案（2024-08 起前 40 個交易日都會是這樣），不是資料缺。
"""
import argparse
import sys
import time
from datetime import date, datetime, timedelta

import auto_fire as AF
import live_panel as LP
import sim_lanes as S
import strategy_lab as SL

CALC = "backfill"                 # ⛔ 面板即時算的那些列沒有這個欄位（差別就靠它）
DEFAULT_FROM = date(2024, 8, 1)


def wire():
    """⛔ 跟 live_panel.start_sim_lanes() 同一份正本、同一組關鍵字（位置參數對調不會報錯、只會算錯）。"""
    ok = S.configure(verdict_fn=AF.fast_verdict, move_fn=AF.move_pct, tpsl_fn=AF.tpsl_points,
                     hist_read_fn=AF.hist_read, reversal_fn=AF.reversal_dir, rev_sec=LP.REV_SEC,
                     pctl=LP.FAST_PCTL, rule=AF.FAST_RULE)
    if not ok or not S.wired():
        sys.exit("⛔ 規則函式沒有接上 —— 不回填（接不上就會算出跟面板不一樣的答案）")


def panel_cutoff(now):
    """面板背景每分鐘在算的那幾天 ⇒ ⛔ 一天都不碰。回 (逐筆六條的上限, 夜盤的上限)，兩個都是「含」。"""
    return (min(S.fast_days(now)) - timedelta(days=1),
            min(S.night_evenings(now)) - timedelta(days=1))


def put(row, rows, dry, tally, lane):
    """寫一列定論。⛔ 一定走 sim_lanes.append_row（它在鎖裡重讀一次、已有定論就不寫）。"""
    row = dict(row)
    row["calc"] = CALC
    if dry:
        tally[lane]["would"] += 1
        return
    try:
        if S.append_row(row):
            rows[(row["lane"], row["date"])] = row
            tally[lane]["wrote"] += 1
        else:
            tally[lane]["exists"] += 1
    except Exception as e:
        tally[lane]["error"] += 1
        print("  ⚠️ %s %s 寫不進去：%s" % (lane, row.get("date"), str(e)[:120]))


def run_ticks(d0, d1, lanes, rows, dry, tally, skips):
    """逐筆那六條。⛔ 一天只 load_day 一次、只建一次 day_pack（跟面板 _step_ticks 同一個做法）。"""
    lanes = [ln for ln in S.TICK_LANES if ln in lanes]
    if not lanes:
        return
    hist, bad, dup = AF.hist_read(S.FAST_HIST)
    print("走幅歷史 fast_hist.jsonl：%d 天（%s ~ %s）壞列 %d、重複 %d"
          % (len(hist), hist[0]["date"] if hist else "-", hist[-1]["date"] if hist else "-", bad, dup))
    days = [f.name[:10] for f in sorted(SL._ticks_dir().glob("*.csv.gz"))]
    days = [x for x in days if str(d0) <= x <= str(d1)]
    print("逐筆：%d 個交易日（%s ~ %s）" % (len(days), days[0] if days else "-", days[-1] if days else "-"))
    t0 = time.time()
    for i, ds in enumerate(days):
        want = [ln for ln in lanes if (ln, ds) not in rows]
        if not want:
            continue
        try:
            D = SL.load_day(ds)
        except Exception as e:
            skips.setdefault("逐筆讀不出來", []).append(ds)
            print("  ⚠️ %s 逐筆讀不出來：%s" % (ds, str(e)[:100]))
            continue
        if D is None:
            skips.setdefault("沒有當天逐筆", []).append(ds)
            continue
        need_box = any(ln in ("orb", "union") for ln in want)
        bh = S.box_window(ds) if need_box else None
        pk = S.day_pack(ds, D, hist, bh)
        for ln in want:
            try:
                res = S.TICK_EVAL[ln](ds, D, hist, bh, pack=pk)
            except Exception as e:      # ⛔ 一條爆掉只停那一條，其他五條照算（面板同一個做法）
                tally[ln]["error"] += 1
                print("  ⚠️ %s %s 算出錯：%s" % (S.LANE_NAME[ln], ds, str(e)[:120]))
                continue
            if res.get("pending"):
                skips.setdefault("%s：%s" % (S.LANE_NAME[ln], res.get("msg") or res.get("why")), []).append(ds)
                continue
            put(res, rows, dry, tally, ln)
        if (i + 1) % 25 == 0 or i + 1 == len(days):
            print("  …逐筆 %d/%d（%s，%.0f 秒）" % (i + 1, len(days), ds, time.time() - t0), flush=True)


def run_night(d0, d1, rows, dry, tally, skips):
    """
    夜盤。⛔ **只用本機 `tmf_1min.csv`**（這支不連永豐）——
    檔案沒蓋到的那幾晚留給面板背景自己去跟永豐要。
    ⚠️ `since` 整輪固定一個值 ⇒ 36MB 的 csv 只讀一次（`_csv_bars` 的快取鍵帶 since）。
    """
    since = d0 - timedelta(days=3)
    evs, d = [], d0
    while d <= d1:
        if d.weekday() < 5:
            evs.append(d)
        d += timedelta(days=1)
    print("夜盤：%d 個平日晚上（%s ~ %s）" % (len(evs), evs[0] if evs else "-", evs[-1] if evs else "-"))
    t0 = time.time()
    for i, E in enumerate(evs):
        es = str(E)
        if ("night", es) in rows:
            continue
        try:
            local, day_e, lo, hi = S.night_bars_local(E, since)
            mm = S.night_frame(local, E)[0] if local is not None else None
            if mm is None or not S.night_complete(mm):
                # ⛔ 本機 csv 前後都有、E 那天卻沒有日盤 ⇒ 那天休市，落地成定論（面板同一條）
                if lo is not None and lo < E and hi is not None and hi > E + timedelta(days=1) and not day_e:
                    put(S._none_row("night", E, "holiday",
                                    "%s 休市（本機 1 分 K 前後都有、那天沒有日盤）" % es),
                        rows, dry, tally, "night")
                else:
                    skips.setdefault("夜盤：1 分 K 沒到齊（本機 csv 沒蓋到）", []).append(es)
                continue
            res = S.night_eval(E, local)
            if res.get("pending"):
                skips.setdefault("夜盤：%s" % (res.get("msg") or res.get("why")), []).append(es)
                continue
            put(res, rows, dry, tally, "night")
        except Exception as e:
            tally["night"]["error"] += 1
            print("  ⚠️ 夜盤 %s 算出錯：%s" % (es, str(e)[:120]))
        if (i + 1) % 100 == 0 or i + 1 == len(evs):
            print("  …夜盤 %d/%d（%s，%.0f 秒）" % (i + 1, len(evs), es, time.time() - t0), flush=True)


def main():
    ap = argparse.ArgumentParser(description="【模擬】七條的回填（⛔ 不連永豐、不下單）")
    ap.add_argument("--from", dest="d0", default=str(DEFAULT_FROM), help="起（含），預設 2024-08-01")
    ap.add_argument("--to", dest="d1", default=None, help="訖（含），⛔ 會被夾在面板窗口的前一天")
    ap.add_argument("--lanes", default=",".join(S.LANES), help="只跑這幾條，逗號分隔")
    ap.add_argument("--dry", action="store_true", help="只算不寫（看看會寫幾列）")
    a = ap.parse_args()

    lanes = [x.strip() for x in a.lanes.split(",") if x.strip()]
    bad = [x for x in lanes if x not in S.LANES]
    if bad:
        sys.exit("⛔ 不認得這幾條：%s（只有 %s）" % (",".join(bad), ",".join(S.LANES)))

    now = datetime.now()
    cut_t, cut_n = panel_cutoff(now)
    d0 = date.fromisoformat(a.d0)
    d1_want = date.fromisoformat(a.d1) if a.d1 else max(cut_t, cut_n)
    d1_t, d1_n = min(d1_want, cut_t), min(d1_want, cut_n)

    wire()
    print("=" * 66)
    print("【模擬】回填%s　規則正本：auto_fire（第 %g 百分位、%s 回馬槍）"
          % ("（--dry 只算不寫）" if a.dry else "", LP.FAST_PCTL, S._hms_sec(LP.REV_SEC)))
    print("起 %s ／ 訖：逐筆 %s、夜盤 %s" % (d0, d1_t, d1_n))
    print("⛔ 面板窗口（最近 10 個平日）從 %s／%s 起，那幾天一天都不碰 —— 等面板自己算"
          % (min(S.fast_days(now)), min(S.night_evenings(now))))
    print("要跑的條：%s" % "、".join(S.LANE_NAME[x] for x in lanes))
    print("=" * 66)

    rows, st = S.read_rows()
    print("現有定論 %d 列（壞 %d、重複 %d）" % (st["ok"], st["bad"], st["dup"]))
    tally = {ln: {"wrote": 0, "exists": 0, "would": 0, "error": 0} for ln in S.LANES}
    skips = {}
    t0 = time.time()
    if d1_t >= d0:
        run_ticks(d0, d1_t, lanes, rows, a.dry, tally, skips)
    if "night" in lanes and d1_n >= d0:
        run_night(d0, d1_n, rows, a.dry, tally, skips)

    print("=" * 66)
    print("跑了 %.0f 秒" % (time.time() - t0))
    for ln in S.LANES:
        t = tally[ln]
        if any(t.values()):
            print("  %-5s 寫進 %4d 列／原本就有 %4d／要寫 %4d／出錯 %d"
                  % (S.LANE_NAME[ln], t["wrote"], t["exists"], t["would"], t["error"]))
    if skips:
        print("沒落地的（⛔ 刻意不寫、補得回來就還有救）：")
        for why, ds in sorted(skips.items(), key=lambda kv: -len(kv[1])):
            print("  %-46s %3d 天　%s%s" % (why[:46], len(ds), ds[0],
                                           " ~ " + ds[-1] if len(ds) > 1 else ""))
    _rows2, st2 = S.read_rows()
    print("跑完定論 %d 列（壞 %d、重複 %d；lines=ok+bad+dup+blank ⇒ %s）"
          % (st2["ok"], st2["bad"], st2["dup"],
             "對得起來" if st2["lines"] == st2["ok"] + st2["bad"] + st2["dup"] + st2["blank"] else "⛔ 對不起來"))


if __name__ == "__main__":
    main()
