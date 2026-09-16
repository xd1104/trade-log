# -*- coding: utf-8 -*-
"""
【策略實驗室】「模擬（不會下單）」的突變測試（2026-09-15 晚上，lab-dev）。

把 tools/shioaji 整個複製到暫存區（⛔ 不含 tick_hist／txf_1min.csv／sim_lanes／autofire），
在**複本**上一次改一處、跑 test_sim_lanes.py、還原（SHA-256 驗證還原無誤）。⛔ 不動 repo 裡的原檔。
總結分得出「沒跑（目標字串失配）」與「紅」——目標字串會跟著產品一起腐爛。

    python sim-mutate.py [WORK_DIR] [平行數，預設 4] [只跑名字含這段字的突變]
"""
import concurrent.futures as cf
import hashlib
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")
SRC = pathlib.Path(__file__).resolve().parents[1] / "shioaji"
WORK = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(tempfile.mkdtemp(prefix="sim_mutate_"))
NPAR = int(sys.argv[2]) if len(sys.argv) > 2 else 4
ONLY = sys.argv[3] if len(sys.argv) > 3 else ""
PY = sys.executable

S, L = "sim_lanes.py", "live_panel.py"
M = [
    ("M1 門檻偷看未來（傳 9999-12-31 當這一天）", S, 'v = c["verdict"](day, mv, hist_rows, pctl=c["pctl"])', 'v = c["verdict"]("9999-12-31", mv, hist_rows, pctl=c["pctl"])'),
    ("M2 進場不用買賣價、用成交價", S, '    fill = fill if fill else float(D["p"][i])', '    fill = float(D["p"][i])'),
    ("M3 停損不用觸發價、用停損價", S, "    raw, w = SL.run_bracket(D, i, fill, d, pts_tpsl, pts_tpsl, cutoff, cost=True)\n", "    raw, w = SL.run_bracket(D, i, fill, d, pts_tpsl, pts_tpsl, cutoff, cost=True)\n    raw = -pts_tpsl if w == 'sl' else raw\n"),
    ("M4 結算日不看 13:30", S, "    return SL.T1330 if SL.is_expiry(date.fromisoformat(str(day))) else SL.T1343_30", "    return SL.T1343_30"),
    ("M5 快攻沒扣手續費", S, '"points": round(raw - FAST_FEE, 1)', '"points": round(raw, 1)'),
    ("M6 夜盤同一根兩邊碰算停利", S, "(it is None or isl <= it)", "(it is None or isl < it)"),
    ("M7 夏令起點錯一天", S, "return mar <= d < nov", "return mar < d < nov"),
    ("M7b 冬令起點錯一天", S, "return mar <= d < nov", "return mar <= d <= nov"),
    ("M8 夜盤收盤用到 05:00", S, "(mm <= NIGHT_EXIT_MIN))[0]", "(mm <= NIGHT_TO_MIN))[0]"),
    ("M9 d=0 當做多", S, "    d = (c > ref) - (c < ref)\n    if d == 0:\n        return _none_row(\"night\"", "    d = 1 if c >= ref else -1\n    if d == 0:\n        return _none_row(\"night\""),
    ("M10 夜盤沒扣價差", S, "cost = NIGHT_FEE + NIGHT_SPREAD", "cost = NIGHT_FEE"),
    ("M11 同一 (lane,date) 重寫", S, '        if (row["lane"], row["date"]) in rows:\n            return False', "        if False:\n            return False"),
    ("M12 沒有逐筆當成定論（不做）", S, '        return None, {"pending": True, "why": "no_ticks", "msg": "沒有當天逐筆"}', '        return None, {"why": "no_ticks", "reason": "沒有當天逐筆"}'),
    ("M13 壞列不計數（當 ok）", S, "            if not _valid_row(o):\n                st[\"bad\"] += 1\n                continue\n", ""),
    ("M13b 重複列不計數（後寫蓋前面）", S, "            if k in rows:\n                st[\"dup\"] += 1\n                continue\n", ""),
    ("M14 08:30~13:50 照抓", S, "if QUIET_FROM <= now.time() < QUIET_TO:", "if False:"),
    ("M15 有部位照抓", S, "    if pos is not False:", "    if False:"),
    ("M15b R1 部位回 None／unknown 當成沒部位", S, "    if pos is not False:", "    if pos is True:"),
    ("M29 R1 安靜時段改回 09:35", S, "QUIET_FROM, QUIET_TO = dtime(8, 30), dtime(13, 50)", "QUIET_FROM, QUIET_TO = dtime(8, 30), dtime(9, 35)"),
    ("M30 R3 快攻休市照樣佔補抓名額", S, '                elif st in ("empty", "tried", "exists", "too_early"):\n                    fetched = False', '                elif st in ("tried", "exists", "too_early"):\n                    fetched = False'),
    ("M31 R3 快攻休市不落地", S, 'if (D is None and ("ticks", ds, str(now.date())) in _TRIED and d < now.date()', 'if (False and ("ticks", ds, str(now.date())) in _TRIED and d < now.date()'),
    ("M32 R3 fast_hist 有那天也記休市", S, "                    and hist_days is not None and ds not in hist_days):", "                    ):"),
    ("M33 R3 今天回空也記休市", S, "in _TRIED and d < now.date()", "in _TRIED and d <= now.date()"),
    ("M34 R3 夜盤休市照樣佔名額", S, '                    elif got in ("empty", "tried"):', '                    elif got in ("tried",):'),
    ("M35 R3 夜盤休市不落地", S, 'if ("kbars", es, str(now.date())) in _TRIED and not day_e:', "if False:"),
    ("M36 R6 讓測試崩潰（night_frame 改名）", S, "def night_frame(bars, E):", "def night_frame_gone(bars, E):"),
    ("M16 問不到部位當沒部位", S, "    except Exception:\n        pos = True", "    except Exception:\n        pos = False"),
    ("M17 流量門檻放寬到 99%", S, "used / lim > SL.USAGE_MAX", "used / lim > 0.99"),
    ("M18 失敗不隔 10 分鐘", S, "(now - lf).total_seconds() < RETRY_S", "(now - lf).total_seconds() < 0"),
    ("M19 問過沒有照樣重抓", S, 'if ("ticks", ds, str(now.date())) in _TRIED:', "if False:"),
    ("M20 step 不吞例外", S, '    except Exception as e:\n        _note_err("step", e)\n        return False', '    except ZeroDivisionError as e:\n        _note_err("step", e)\n        return False'),
    ("M20b 單日計算例外不吞", S, "                except Exception as e:      # \u26d4 一條壞掉只停那一條", "                except ZeroDivisionError as e:  # \u26d4 一條壞掉只停那一條"),
    ("M21 loop 不吞例外", S, "        except Exception as e:            # step 自己已經吞了", "        except ZeroDivisionError as e:            # step 自己已經吞了"),
    ("M22 夜盤沒到齊也算", S, "return bool(len(mm)) and bool(np.any(mm >= NIGHT_TAIL_MIN))", "return bool(len(mm))"),
    ("M23 夜盤 05:10 前就算前一晚", S, "    if now.time() < NIGHT_READY:\n", "    if False:\n"),
    ("M24 今天 15:00 前也補抓", S, "(d == now.date() and now.time() <= SL.FETCH_UNTIL)", "False"),
    ("M25 sim_lanes import broker", S, "import strategy_lab as SL ", "import broker\nimport strategy_lab as SL "),
    ("M26 快攻不快也做", S, '    if not ctx["fast"]:\n        return _none_row(lane, day, "not_fast"', '    if False:\n        return _none_row(lane, day, "not_fast"'),
    ("M27 ref 不含 09:00:00.000 那筆", S, "i_ref = int(np.searchsorted(t, FAST_REF_MS, side=\"right\")) - 1", "i_ref = int(np.searchsorted(t, FAST_REF_MS, side=\"left\")) - 1"),
    ("M27b px 不含 09:03:30.000 那筆", S, "i_px = int(np.searchsorted(t, FAST_PX_MS, side=\"right\")) - 1", "i_px = int(np.searchsorted(t, FAST_PX_MS, side=\"left\")) - 1"),
    ("M28 端點 state() 順手寫檔（不唯讀）", S, "    rows, st = read_rows()\n    lanes = {}", "    rows, st = read_rows()\n    SIM_DIR.mkdir(parents=True, exist_ok=True)\n    (SIM_DIR / (now.strftime(\"%Y-%m\") + \".jsonl\")).open(\"a\", encoding=\"utf-8\").write(\"\\n\")\n    lanes = {}"),
    ("L1 端點拿掉跨站守衛", L, '        ok, code, msg = fire_get_guard(self.headers)\n        if not ok:\n            return self._json(code, {"ok": False, "msg": msg})\n        if sim_lanes is None:', "        if sim_lanes is None:"),
    ("L2 import sim_lanes 不包 try", L, "try:\n    import sim_lanes\nexcept Exception as _sim_err:          # noqa: BLE001  ⛔ 刻意接住所有例外\n    sim_lanes = None", "import sim_lanes\nif False:\n    _sim_err = None\n    sim_lanes = None"),
    ("L3 start_sim_lanes 不吞例外", L, "    except Exception as e:          # noqa: BLE001  ⛔ 刻意接住所有例外\n        try:\n            print(f\"⚠️ 【模擬】背景", "    except ZeroDivisionError as e:          # noqa: BLE001\n        try:\n            print(f\"⚠️ 【模擬】背景"),
    ("L4 前端送 POST", L, "fetch('/api/sim/state',{cache:'no-store'})", "fetch('/api/sim/state',{cache:'no-store',method:'POST'})"),
    ("L5 切進模擬分頁不問資料", L, "if(t==='sim'){ smEnter(); }", "if(t==='sim'){ }"),
    ("L6 fire_fires_today ⓪ 整段失效", L, "            and _fire_wait_open(str(now.date()))):", "            and False):"),
    ("L7 ⓪ 窗口拉到 60 秒", L, "_rev_secs < REV_SEC + AUTO_LATE_MS / 1000", "_rev_secs < REV_SEC + 60"),
    ("L7b ⓪ 窗口拿掉補送 3 秒", L, "_rev_secs < REV_SEC + AUTO_LATE_MS / 1000", "_rev_secs < REV_SEC"),
    ("L8 ⓪ 不看 AUTO rev", L, '            and not (AUTO.get("day") == str(now.date()) and AUTO.get("rev"))', "            and True"),
    ("L8b ⓪ 看 rev 不看是哪一天", L, '            and not (AUTO.get("day") == str(now.date()) and AUTO.get("rev"))', '            and not AUTO.get("rev")'),
    ("L9 wait 已有定論也說今天", L, '    return (any(o.get("rec") == "wait" for o in rows)\n            and not any(o.get("rec") in ("fire", "result", "skip") for o in rows))', '    return any(o.get("rec") == "wait" for o in rows)'),
    ("L10 ⓪ 不問 market_session", L, '        if market_session(rev_dt) == "day":', "        if True:"),
    ("L11 前端把 lane 寫死成兩條（不聽後端）", L, "const keys=Object.keys(x.lanes).filter(k=>/^[a-z0-9_]+$/.test(k));", "const keys=['fast','night'];"),
    ("L11b 模擬的卡改名（#tab-sim 裡沒有那張卡）", L, '<div id="tab-sim" hidden>\n <div class="card sm-card" id="smcard">', '<div id="tab-sim" hidden>\n <div class="card sm-card2" id="smcard2">'),
    ("L11c 分頁列那顆「模擬」指到不存在的分頁", L, '<button data-tab="sim">模擬</button>', '<button data-tab="simx">模擬</button>'),
    ("L12 R1 注入部位判斷：券商 unknown 當沒部位", L, "        return broker.broker_position() is not None", "        return isinstance(broker.broker_position(), dict)"),
    ("L12b R1 注入部位判斷：例外當沒部位", L, "    except Exception:           # noqa: BLE001  ⛔ 問不到 ⇒ 當成有部位\n        return True", "    except Exception:           # noqa: BLE001\n        return False"),
    ("L12c R1 改回只讀記憶體的 _lab_has_position", L, 'args=(lambda: SESSION_REF.get("api"), _sim_has_position)', 'args=(lambda: SESSION_REF.get("api"), _lab_has_position)'),
    ("L13 R4 move_pct／tpsl_points 對調", L, "move_fn=auto_fire.move_pct,\n                                 tpsl_fn=auto_fire.tpsl_points,", "move_fn=auto_fire.tpsl_points,\n                                 tpsl_fn=auto_fire.move_pct,"),
    ("L15 不注入 reversal_dir（回馬槍那半接不上）", L, "reversal_fn=auto_fire.reversal_dir, rev_sec=REV_SEC,", "rev_sec=REV_SEC,"),
    ("L16 rev_sec 注入錯的時刻（09:15 變 09:20）", L, "reversal_fn=auto_fire.reversal_dir, rev_sec=REV_SEC,", "reversal_fn=auto_fire.reversal_dir, rev_sec=REV_SEC + 300,"),
    ("L14 R2 主迴圈 _auto_put 被改（複本沒 git 也要驗到）", L, "    key = (kind, str(arg))\n    if key in AUTO[\"queued\"]:", "    key = (kind, str(arg), 0)\n    if key in AUTO[\"queued\"]:"),
    # ── 2026-09-16 新的四條 ───────────────────────────────────────────
    ("N1 hmq 快的日子還去看 09:15（一天兩口）", S, '    if ctx["fast"]:\n        if ctx["d"] == 0:', '    if False:\n        if ctx["d"] == 0:'),
    ("N2 hmq 慢的日子不等 09:15", S, '    return _rev_leg("hmq", day, D, ctx, c, cutoff)', '    return _none_row("hmq", day, "not_fast", "不快，不做", ctx["base"])'),
    ("N3 rev 快的日子也做", S, '    if ctx["fast"]:\n        return _none_row("rev", day, "fast_skip"', '    if False:\n        return _none_row("rev", day, "fast_skip"'),
    ("N4 fast11 改用 13:43:30 收（跟 fast 一樣）", S, 'return _fast_like("fast11", str(day), D, hist_rows, cfg or _CFG, FAST11_CUT_MS, pack)', 'return _fast_like("fast11", str(day), D, hist_rows, cfg or _CFG, _day_cutoff(day), pack)'),
    ("N4b fast11 的時刻改成 11:30", S, "FAST11_CUT_MS = SL.ms(11, 0, 0)", "FAST11_CUT_MS = SL.ms(11, 30, 0)"),
    ("N5 反轉自己比方向（不呼叫注入的那一支）", S, '    return i15, c["reversal"](ctx["px"], ctx["d"], float(D["p"][i15]))', '    _q = float(D["p"][i15])\n    return i15, ((_q > ctx["px"]) - (_q < ctx["px"])) or None'),
    ("N5b 反轉「同方向」也做", S, '    return i15, c["reversal"](ctx["px"], ctx["d"], float(D["p"][i15]))', '    _q = float(D["p"][i15])\n    return i15, ((_q > ctx["px"]) - (_q < ctx["px"])) or ctx["d"]'),
    ("N6 orb 箱子太窄照做", S, '    if o["box_pct"] < med:', "    if False:"),
    ("N6b orb 濾網改成「小於等於」才不做", S, '    if o["box_pct"] < med:', '    if o["box_pct"] <= med:'),
    ('N6c orb 中位數改成平均', S, '    return float(np.median(np.asarray(v, dtype=float)[-ORB_HIST_N:]))', '    return float(np.mean(np.asarray(v, dtype=float)[-ORB_HIST_N:]))'),
    ('N7 orb 歷史不夠當成定論（會寫檔）', S, '        return _pending("few_box_hist", "箱子寬度歷史不夠（這天以前只有 %d 天，要 %d 天）"\n                        % (len(_w["vals"]), ORB_HIST_N))', '        return _none_row("orb", day, "few_box_hist", "箱子寬度歷史不夠")'),
    ('N7c orb 歷史天數不夠也給中位數', S, '    if v is None or len(v) < ORB_HIST_N:\n        return None', '    if v is None or len(v) < 1:\n        return None'),
    ("N7b orb 歷史只要 10 天", S, "ORB_HIST_N = 20 ", "ORB_HIST_N = 10 "),
    ("N8 orb 停損用固定點數（不是箱子另一端）", S, '    sl = round(fill - o["lo"], 1) if d > 0 else round(o["hi"] - fill, 1)', "    sl = 100.0"),
    ("N9 orb 設了停利（不再是「不設停利」）", S, '    raw, w = SL.run_bracket(D, o["i"], fill, d, ORB_NO_TP, sl, cutoff, cost=True)', '    raw, w = SL.run_bracket(D, o["i"], fill, d, sl, sl, cutoff, cost=True)'),
    ("N10 orb 碰到箱子邊就算突破", S, "    up, dn = p > hi, p < lo", "    up, dn = p >= hi, p <= lo"),
    ("N11 orb 上下緣同時出現時挑後面那一筆", S, "    if idn is None or (iu is not None and iu <= idn):", "    if idn is not None and (iu is None or idn <= iu):"),
    ("N12 orb 箱子收在 09:04（少一分鐘）", S, "ORB_BOX_TO_MS = SL.ms(9, 5, 0)", "ORB_BOX_TO_MS = SL.ms(9, 4, 0)"),
    ("N12b orb 箱子不含 09:05 那一筆", S, '    i1 = int(np.searchsorted(t, ORB_BOX_TO_MS, side="right"))', '    i1 = int(np.searchsorted(t, ORB_BOX_TO_MS, side="left"))'),
    ("N13 orb 箱子寬度歷史偷看未來", S, "        if m and m.group(1) < str(day):", "        if m and m.group(1) != str(day):"),
    ('N13b orb 箱子寬度歷史沒排序（不是舊到新）', S, '    return sorted(out)', '    return out'),
    ("N14 六條各讀一次逐筆（同一天讀六次）", S, "                    res = TICK_EVAL[ln](ds, D, hist, bh, pack=pk)", "                    _D2 = SL.load_day(ds) if D is not None else None\n                    res = TICK_EVAL[ln](ds, _D2, hist, bh, pack=None)"),
    ("N15 少一條（orb 不算）", S, 'LANES = ("fast", "fast11", "hmq", "rev", "orb", "union", "night")', 'LANES = ("fast", "fast11", "hmq", "rev", "union", "night")'),
    ("N15b 條的順序被換掉", S, 'LANES = ("fast", "fast11", "hmq", "rev", "orb", "union", "night")', 'LANES = ("night", "fast", "fast11", "hmq", "rev", "orb", "union")'),
    ("N15c 名字改回舊的", S, '"fast": "快攻", "fast11": "早收"', '"fast": "早盤快攻", "fast11": "快攻 11:00 平"'),
    ("N17 掃箱子歷史時畫面說不出「還在算」", S, '    if not _BOX_ST["busy"]:\n        return ""', '    if True:\n        return ""'),
    ("N17b 掃完沒把旗標收掉（畫面永遠說計算中）", S, '        _BOX_ST["busy"] = False ', '        _BOX_ST["busy"] = True  '),
    # ── 多方聯軍 ──────────────────────────────────────────────────────
    ("U1 碰到做空就收工（不繼續看下一個）", S, '    longs = [x for x in cands if x["dir"] > 0]     ', '    longs = ([] if (cands and cands[0]["dir"] < 0) else [x for x in cands if x["dir"] > 0])  '),
    ("U2 取最晚觸發的那一個", S, "    pick = longs[0]  ", "    pick = longs[-1]  "),
    ("U2b 做空的也一起挑（不只取做多）", S, '    longs = [x for x in cands if x["dir"] > 0]     ', "    longs = list(cands)     "),
    ("U3 不用共用的候選、自己重算一次", S, "    p = pack if pack is not None else day_pack(day, D, hist_rows, box_vals, c)\n    cands, miss, stop = union_cands(day, D, p)", '    p = day_pack(day, D, hist_rows, (pack["box"] if pack is not None else box_vals), c)\n    cands, miss, stop = union_cands(day, D, p)'),
    ("U4 快的日子也算回馬槍候選", S, '        if not ctx["fast"]:                     ', "        if True:                     "),
    ("U5 開箱候選不看箱子濾網", S, '    elif o is not None and o["i"] is not None and o["box_pct"] >= med:', '    elif o is not None and o["i"] is not None:'),
    ("U5b 開箱候選要「大於」中位數才算", S, '    elif o is not None and o["i"] is not None and o["box_pct"] >= med:', '    elif o is not None and o["i"] is not None and o["box_pct"] > med:'),
    ("U6 快攻候選不看快不快", S, '    if ctx["fast"] and ctx["d"] != 0:', '    if ctx["d"] != 0:'),
    ("U7 贏家是開箱時不照它的出場規則", S, '    if pick["kind"] == "orb":\n        return _orb_enter("union"', '    if False:\n        return _orb_enter("union"'),
    ("U9 候選沒有定序（時刻一樣時會飄）", S, '    out.sort(key=lambda x: (x["at_ms"], UNION_TIE[x["kind"]]))', '    out.sort(key=lambda x: -x["at_ms"])'),
    ('S1 窗口跨度那道閘門整個失效', S, '    if w.get("span") is None or w["span"] <= ORB_SPAN_MAX_DAYS:\n        return None', '    if True:\n        return None'),
    ('S1b 跨度上限放寬到 9999 天', S, 'ORB_SPAN_MAX_DAYS = 90', 'ORB_SPAN_MAX_DAYS = 9999'),
    ('S1c 跨度太寬也做定論（會寫檔）', S, '        return _pending("box_span", bad)', '        return _none_row("orb", day, "box_span", bad)'),
    ('S1d union 把跨度太寬當成整條資料缺', S, '    elif _span_bad:                             # ⛔ 跨度太寬 ⇒ 同樣只是「少一個候選」\n        miss.append(', '    elif _span_bad:\n        return None, None, {"pending": True, "why": "box_span", "msg": _span_bad}\n    elif False:\n        miss.append('),
    ('S1e reason 不帶窗口起訖（只寫「過去 20 天」）', S, '    return ("過去 %d 天（%s~%s）" % (n, w["d0"], w["d1"])) if w.get("d0") else ("過去 %d 天" % n)', '    return "過去 %d 天" % n'),
    ('S1f box_window 不算跨度', S, '    span = None if not pairs else (date.fromisoformat(d1) - date.fromisoformat(d0)).days + 1', '    span = None'),
    ('S4 union 規則句把候選叫成「回馬槍」', S, '                                    LANE_NAME["rev"], at))', '                                    LANE_NAME["hmq"], at))'),
    ('S4b union 的不可用清單把候選叫成「回馬槍」', S, '        miss.append("%s／%s：%s" % (LANE_NAME["fast"], LANE_NAME["rev"], stop.get("reason") or ""))', '        miss.append("%s／%s：%s" % (LANE_NAME["fast"], LANE_NAME["hmq"], stop.get("reason") or ""))'),
    ('S6 _valid_row 不擋沒有點數的進場列', S, '    return (not isinstance(pts, bool) and isinstance(pts, (int, float)) and np.isfinite(pts)\n            and o.get("exit_reason") in _EXITS)', '    return o.get("exit_reason") in _EXITS'),
    ('S6b _months 第二道防線失效', S, '        pts = [r["points"] for r in tr\n               if not isinstance(r.get("points"), bool) and isinstance(r.get("points"), (int, float))]', '        pts = [r["points"] for r in tr]'),
    ('Q7 union 挑中開箱時自己再算一次箱子', S, '        return _orb_enter("union", day, D, p["orb"], cutoff, "union", reason, base, ex)', '        return _orb_enter("union", day, D, orb_calc(day, D), cutoff, "union", reason, base, ex)'),
    ('Q7b union 挑中開箱時用快攻那半的出場規則', S, '    if pick["kind"] == "orb":\n        return _orb_enter("union"', '    if False:\n        return _orb_enter("union"'),
    ('U11 走幅歷史不夠也當成整條資料缺', S, '    if stop is not None and stop.get("pending"):', '    if stop is not None:'),
    ('U12 箱子歷史不夠也當成整條資料缺', S, '    if med is None:                             # ⛔ 只是少一個候選，不是整條沒資料\n        miss.append(', '    if med is None:\n        return None, None, {"pending": True, "why": "few_box_hist", "msg": "箱子寬度歷史不夠"}\n    if False:\n        miss.append('),
    ('U13 「一個候選都沒有」跟「都說做空」混為一談', S, '        return _none_row("union", day, "no_cand",', '        return _none_row("union", day, "no_long",'),
    ('U14 不可用的候選沒寫進 reason', S, '    return ("；不可用：" + "、".join(miss)) if miss else ""', '    return ""'),
    ('U15 沒有候選時記成資料缺（⛔ 那是定論）', S, '    if not cands:\n        return _none_row("union", day, "no_cand",', '    if not cands:\n        return _pending("no_cand", "今天沒有候選")\n    if False:\n        return _none_row("union", day, "no_cand",'),
    ("U10 開箱候選的觸發時刻寫成 09:05（不是真的突破那一刻）", S, '        out.append({"kind": "orb", "dir": o["d"], "at_ms": int(D["t"][o["i"]]),', '        out.append({"kind": "orb", "dir": o["d"], "at_ms": ORB_BOX_TO_MS,'),
    ("N16 configure 少了 reversal 也算接上", S, "          and callable(reversal_fn)\n", ""),
]


def main():
    global M
    M = [m for m in M if ONLY in m[0]]
    if WORK.exists():
        shutil.rmtree(WORK)
    for k in range(NPAR):
        shutil.copytree(SRC, WORK / ("w%d" % k), ignore=shutil.ignore_patterns("tick_hist", "txf_1min.csv", "__pycache__", "sim_lanes", "autofire"))
    # 複本裡沒有 git ⇒ 主迴圈比對基準（a71087e 的 live_panel.py）先抽出來，用環境變數交給測試（lab-qa R2）
    base = WORK / "baseline_live_panel.py"
    g = subprocess.run(["git", "show", "a71087e:tools/shioaji/live_panel.py"], cwd=str(SRC.parents[1]), capture_output=True, timeout=60)
    if g.returncode == 0:
        base.write_bytes(g.stdout)
        os.environ["SIM_BASELINE_LIVE_PANEL"] = str(base)
    else:
        print("⚠️ 抽不到 a71087e 的 live_panel.py ⇒ 複本裡主迴圈比對會是「未驗」，L14 會打不紅")
    # ⛔ 對照組：沒改的複本一定要綠、而且沒有「未驗」—— 不然每個突變都會「紅」得毫無意義
    c = subprocess.run([PY, "test_sim_lanes.py"], cwd=str(WORK / "w0"), capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=900, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    last = [ln for ln in c.stdout.splitlines() if ln.startswith("總結")]
    print("對照組（不改）：exit=%s  %s" % (c.returncode, last[-1] if last else c.stdout[-300:] + c.stderr[-300:]))
    if c.returncode != 0 or not last or "未驗" in last[-1]:
        print("⛔ 對照組不是乾淨的綠 ⇒ 突變結果不可信，停止")
        sys.exit(2)
    groups = {k: [] for k in range(NPAR)}
    for i, m in enumerate(M):
        groups[i % NPAR].append(m)

    def worker(k):
        w = WORK / ("w%d" % k)
        out = []
        for name, fn, old, new in groups[k]:
            p = w / fn
            orig = p.read_bytes()
            txt = orig.decode("utf-8")
            if old not in txt:
                out.append((name, "沒跑（目標字串失配）", 0))
                continue
            mut = txt.replace(old, new, 1)
            if mut == txt:
                out.append((name, "沒跑（內容沒變）", 0))
                continue
            p.write_bytes(mut.encode("utf-8"))
            try:
                r = subprocess.run([PY, "test_sim_lanes.py"], cwd=str(w), capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=900, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
                fails = [ln.strip() for ln in r.stdout.splitlines() if ln.strip().startswith("FAIL")]
                res = ("紅" if r.returncode != 0 else "綠（沒打紅）"), r.returncode, fails[:3], r.stderr[-200:] if r.returncode not in (0, 1) else ""
            except subprocess.TimeoutExpired:
                res = ("逾時",)
            finally:
                p.write_bytes(orig)
                assert hashlib.sha256(p.read_bytes()).hexdigest() == hashlib.sha256(orig).hexdigest()
            out.append((name, res, len(fails) if res[0] != "逾時" else -1))
        return out

    with cf.ThreadPoolExecutor(NPAR) as ex:
        allres = [x for part in ex.map(worker, range(NPAR)) for x in part]
    order = {m[0]: i for i, m in enumerate(M)}
    red = 0
    for name, res, nf in sorted(allres, key=lambda x: order[x[0]]):
        if isinstance(res, str):
            print("%-34s %s" % (name, res))
            continue
        red += res[0] == "紅"
        print("%-34s %s  exit=%s  紅 %d 項  例：%s %s" % (name, res[0], res[1] if len(res) > 1 else "", nf,
                                                     (res[2][0] if len(res) > 2 and res[2] else ""), res[3] if len(res) > 3 else ""))
    print("總結：%d 個突變，打紅 %d 個" % (len(M), red))


if __name__ == "__main__":
    main()
