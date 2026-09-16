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
    ("M12 沒有逐筆當成定論（不做）", S, 'return None, _pending("no_ticks", "沒有當天逐筆")', 'return None, _none_row(lane, day, "no_ticks", "沒有當天逐筆")'),
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
    ("N4 fast11 改用 13:43:30 收（跟 fast 一樣）", S, 'return _fast_like("fast11", str(day), D, hist_rows, cfg or _CFG, FAST11_CUT_MS)', 'return _fast_like("fast11", str(day), D, hist_rows, cfg or _CFG, _day_cutoff(day))'),
    ("N4b fast11 的時刻改成 11:30", S, "FAST11_CUT_MS = SL.ms(11, 0, 0)", "FAST11_CUT_MS = SL.ms(11, 30, 0)"),
    ("N5 反轉自己比方向（不呼叫注入的那一支）", S, '    d2 = c["reversal"](ctx["px"], ctx["d"], p15)', '    d2 = ((p15 > ctx["px"]) - (p15 < ctx["px"])) or None'),
    ("N5b 反轉「同方向」也做", S, '    d2 = c["reversal"](ctx["px"], ctx["d"], p15)', '    d2 = ((p15 > ctx["px"]) - (p15 < ctx["px"])) or ctx["d"]'),
    ("N6 orb 箱子太窄照做", S, '    if o["box_pct"] < med:', "    if False:"),
    ("N6b orb 濾網改成「小於等於」才不做", S, '    if o["box_pct"] < med:', '    if o["box_pct"] <= med:'),
    ("N6c orb 中位數改成平均", S, "    med = float(np.median(np.asarray(box_hist, dtype=float)[-ORB_HIST_N:]))", "    med = float(np.mean(np.asarray(box_hist, dtype=float)[-ORB_HIST_N:]))"),
    ("N7 orb 歷史不夠當成定論（會寫檔）", S, '        return _pending("few_box_hist", "箱子寬度歷史不夠（這天以前只有 %d 天，要 %d 天）"\n                        % (len(box_hist), ORB_HIST_N))', '        return _none_row("orb", day, "few_box_hist", "箱子寬度歷史不夠")'),
    ("N7b orb 歷史只要 10 天", S, "ORB_HIST_N = 20 ", "ORB_HIST_N = 10 "),
    ("N8 orb 停損用固定點數（不是箱子另一端）", S, '    sl = round(fill - o["lo"], 1) if d > 0 else round(o["hi"] - fill, 1)', "    sl = 100.0"),
    ("N9 orb 設了停利（不再是「不設停利」）", S, '    raw, w = SL.run_bracket(D, o["i"], fill, d, ORB_NO_TP, sl, o["cutoff"], cost=True)', '    raw, w = SL.run_bracket(D, o["i"], fill, d, sl, sl, o["cutoff"], cost=True)'),
    ("N10 orb 碰到箱子邊就算突破", S, "    up, dn = p > hi, p < lo", "    up, dn = p >= hi, p <= lo"),
    ("N11 orb 上下緣同時出現時挑後面那一筆", S, "    if idn is None or (iu is not None and iu <= idn):", "    if idn is not None and (iu is None or idn <= iu):"),
    ("N12 orb 箱子收在 09:04（少一分鐘）", S, "ORB_BOX_TO_MS = SL.ms(9, 5, 0)", "ORB_BOX_TO_MS = SL.ms(9, 4, 0)"),
    ("N12b orb 箱子不含 09:05 那一筆", S, '    i1 = int(np.searchsorted(t, ORB_BOX_TO_MS, side="right"))', '    i1 = int(np.searchsorted(t, ORB_BOX_TO_MS, side="left"))'),
    ("N13 orb 箱子寬度歷史偷看未來", S, "        if m and m.group(1) < str(day):", "        if m and m.group(1) != str(day):"),
    ("N13b orb 箱子寬度歷史沒排序（不是舊到新）", S, "    return [v for _d, v in sorted(out)]", "    return [v for _d, v in out]"),
    ("N14 五條各讀一次逐筆（同一天讀五次）", S, '                    res = (orb_eval(ds, D, box_hist(ds) if D is not None else None) if ln == "orb"\n                           else TICK_EVAL[ln](ds, D, hist))', '                    _D2 = SL.load_day(ds) if D is not None else None\n                    res = (orb_eval(ds, _D2, box_hist(ds) if _D2 is not None else None) if ln == "orb"\n                           else TICK_EVAL[ln](ds, _D2, hist))'),
    ("N15 少一條（orb 不算）", S, 'LANES = ("fast", "hmq", "rev", "fast11", "orb", "night")', 'LANES = ("fast", "hmq", "rev", "fast11", "night")'),
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
