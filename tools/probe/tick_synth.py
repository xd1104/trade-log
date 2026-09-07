# -*- coding: utf-8 -*-
"""
【細節】分頁的**合成**逐筆資料產生器（給治具與探針用）。

⛔ 一個真實成交價、真實進出場時間、真實點數都不准出現在這裡。
   價格一律從 **12000 附近**取（他的真實行情在 46xxx~47xxx，那個區間會撞到他的紀錄，
   `leak-scan.py` 會紅，而且看的人分不出巧合與外洩）。走勢是 LCG 隨機漫步，
   不是任何一天的真實形狀。

【為什麼要合成】逐筆落地 2026-09-07 才做好、面板還沒重啟，第一份真資料要等隔天 08:45。
   而效能結論只有在**真實量級**下才成立：`morning_logs/2026-08-11-live.json` 寫著
   `"ticks": 42718`（那天四萬多筆），加上買賣價 1~2 倍 ⇒ 一早上 8.5~13 萬列。
   所以預設就產這個量級，不要拿兩千列的樣本量效能。

【行尾】跟正式檔一樣寫 **CRLF** —— `tick_writer.py` 在 Windows 用文字模式寫檔，
   上磁碟就是 CRLF。解析器要能吃（JSON 把 \\r 當空白），這件事只有用 CRLF 的樣本才驗得到。

用法：
    py tools/probe/tick_synth.py <輸出資料夾>          # 產一整組治具資料
    from tick_synth import synth_day                   # 或當模組用
"""
import json
import sys
from pathlib import Path

BASE = 12000.0          # ⛔ 刻意離他的真實行情很遠
FMT_V = 1


class _Rnd:
    """固定種子的 LCG —— 探針要可重跑，不可以用 random 的全域狀態。"""

    def __init__(self, seed=20260908):
        self.s = seed & 0x7FFFFFFF

    def next(self):
        self.s = (self.s * 1103515245 + 12345) & 0x7FFFFFFF
        return self.s / 0x7FFFFFFF

    def pick(self, n):
        return int(self.next() * n)


def _hms(sec, ms):
    return "%02d:%02d:%02d.%03d" % (sec // 3600, sec // 60 % 60, sec % 60, ms)


def synth_lines(ticks=90000, bidask=45000, first=31502, last=34199,
                seed=20260908, heads=1, gaps=(), bad=0, drop=()):
    """
    產出一整天的列（str 的 list，不含行尾）。

    first / last  ＝ 涵蓋的當日秒數（08:45:00 ＝ 31500、09:30:00 ＝ 34200）
    heads         ＝ 檔頭列數（面板當天重啟幾次就有幾列）
    gaps          ＝ [(在第幾秒之後, 丟了幾筆, why)]，寫成 k=x 的痕跡列
    bad           ＝ 故意寫幾列壞掉的 JSON（要驗 bad 有沒有被吞掉）
    drop          ＝ [(起, 迄)] 這幾段秒數完全不產任何列（模擬「沒有錄到」）
    """
    r = _Rnd(seed)
    span = max(1, last - first)
    total = ticks + bidask
    px = BASE
    out = []
    out.append(json.dumps({
        "k": "h", "v": FMT_V, "at": "2026-09-08T08:44:59.000", "pid": 0,
        "src": "tick_synth.py（合成資料，不是真的行情）",
        "win": "08:45:00~09:30:00",
        "clock": "永豐給的交易所時間（tick.datetime），不是本機時鐘。"
                 "⚠️ 唯一的例外是痕跡列（k=x）的 wt，那一欄才是本機時鐘",
        "order": "照收到的順序寫，**不保證時間單調遞增**。",
        "note": "合成資料：價格取 12000 附近，與任何真實行情無關。",
    }, ensure_ascii=False, separators=(",", ":")))
    for i in range(1, heads):
        out.append(json.dumps({"k": "h", "v": FMT_V, "at": "2026-09-08T09:0%d:00.000" % i,
                               "pid": i, "src": "重啟接檔（合成）"},
                              ensure_ascii=False, separators=(",", ":")))
    nxt_gap = list(gaps)
    for i in range(total):
        u = i / max(1, total - 1)
        sec = first + int(u * span)
        ms = int((u * span * 1000)) % 1000
        if any(a <= sec <= b for a, b in drop):
            continue
        # 隨機漫步 ±1 檔，偶爾走一大步（讓高低點有東西可看）
        step = (r.next() - 0.5)
        px += step * (6.0 if r.next() > 0.985 else 1.0)
        px = round(px)
        if i % 3 == 2 and bidask:
            sp = 1 + r.pick(3)
            out.append(json.dumps({"k": "b", "t": _hms(sec, ms),
                                   "b": px - sp, "a": px + sp},
                                  ensure_ascii=False, separators=(",", ":")))
        else:
            out.append(json.dumps({"k": "t", "t": _hms(sec, ms), "p": float(px),
                                   "v": 1 + r.pick(4)},
                                  ensure_ascii=False, separators=(",", ":")))
        while nxt_gap and sec >= nxt_gap[0][0]:
            at, n, why = nxt_gap.pop(0)
            out.append(json.dumps({"k": "x", "wt": _hms(sec, 1), "after": _hms(sec, ms),
                                   "n": n, "why": why},
                                  ensure_ascii=False, separators=(",", ":")))
    for i in range(bad):
        out.insert(min(len(out), 5 + i * 977), '{"k":"t","t":"09:0' + str(i % 10) + ':00.000","p":')
    return out


def synth_day(path, **kw):
    """寫成一個逐筆檔（CRLF，跟正式檔一樣）。回寫出去的列數。"""
    lines = synth_lines(**kw)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))
    return len(lines)


def synth_polled(path, n=300):
    """
    **輪詢** schema 的檔（`tick_recorder.py` 加 -polled 後綴之前留下來的那一種）。
    欄位跟逐筆完全不同、**一個 k 都沒有** —— 拿來驗「不可以只看檔名」。
    ⚠️ 檔名故意用逐筆的樣子（YYYY-MM-DD.jsonl），否則測不到那個坑。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    r = _Rnd(7)
    px = BASE
    for i in range(n):
        px += (r.next() - 0.5) * 3
        rows.append(json.dumps({
            "t": "2026-09-07T09:%02d:%02d.000" % (i // 60 % 60, i % 60),
            "ms": None, "clock": "09:%02d:%02d" % (i // 60 % 60, i % 60),
            "age": 0, "quote": "live", "price": round(px, 1),
            "bid": round(px - 1, 1), "ask": round(px + 1, 1), "is_mid": False,
            "mom5": 0.0, "idx": round(px * 0.99, 2), "basis": 1.0}, ensure_ascii=False))
    p.write_bytes(("\r\n".join(rows) + "\r\n").encode("utf-8"))
    return len(rows)


def synth_polled_day(path, day, first=32700, last=34199, every_ms=460,
                     seed=8811, nopx=0, drop=(), badts=0, strpx=0,
                     with_k=False, all_bad_t=False):
    """
    **合格的取樣檔**（`tick_recorder.py` 寫的 `YYYY-MM-DD-polled.jsonl`）。

    跟上面的 `synth_polled()` 差別是「這一份是要**被收下**的」：
    欄位照 tick_recorder.py 的 CHIP_KEYS 產完整一份，`t` 是**完整 ISO 到毫秒**
    （逐筆那邊只有 HH:MM:SS.mmm），間隔約 `every_ms`（真實那份實測中位 457ms）。

    ⛔ 一列都不准有 `k` 欄位 —— 那是逐筆才有的東西。
    ⛔ 也**不准有任何成交量欄位**：取樣檔真的沒有單筆量，
       治具若偷塞一個 v，「量柱要 disabled」那條守衛就變成假的。
    nopx  ＝ 故意讓幾列的 price 是 null（面板當下沒有報價）。
    drop  ＝ [(起, 迄)] 這幾段秒數完全不產（模擬中途沒錄到）。

    下面這幾個是**要被擋掉／要被算進 bad** 的壞資料（負控組用，2026-09-07 加）：
    badts ＝ 幾列的 `t` 讀不懂（"??"）。⛔ 解析器必須算進 bad，
            **不准靜靜當成 08:45:00**（那是「安靜地少」，這個專案明令禁止）。
    strpx ＝ 幾列的 `price` 寫成**字串**。⛔ 舊版會讓 `round(float(px))` 或
            `px > row[1]` 冒出例外 ⇒ **整天回 500、好資料一列都看不到**。
            正確行為是只弄掉那一列（計入 bad）。
    with_k     ＝ 每一列多塞一個 `k` 欄位 ⇒ 這個檔是「取樣檔名 ＋ 逐筆內容」，
                 **嗅探必須擋掉它**（種類要跟檔名一致）。
    all_bad_t  ＝ 每一列的 `t` 都讀不懂 ⇒ 嗅探同樣必須擋掉（`t` 讀得懂是收下的條件）。
    ⚠️ 壞列一律從**第 3 列以後**才開始塞：前兩列要保持乾淨，
       否則 8KB 嗅探看不到任何一列合格的資料，整個檔會被擋掉、測不到解析那一半。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    r = _Rnd(seed)
    px = BASE
    rows = []
    t_ms = first * 1000
    prev = None
    i = 0
    used = 0            # 已經產了幾列「沒有報價」的（nopx 是**列數**，不是比例）
    while t_ms < last * 1000:
        sec = t_ms // 1000
        if not any(a <= sec <= b for a, b in drop):
            px += (r.next() - 0.5) * (6.0 if r.next() > 0.98 else 1.6)
            px = round(px, 1)
            sp = 1.0 + r.pick(3)
            has_px = True
            if used < nopx and i and i % 300 == 0:
                has_px = False
                used += 1
            rows.append({
                "t": "%sT%02d:%02d:%02d.%03d" % (day, sec // 3600, sec // 60 % 60,
                                                 sec % 60, t_ms % 1000),
                "ms": None if prev is None else int(t_ms - prev),
                "clock": "%02d:%02d:%02d" % (sec // 3600, sec // 60 % 60, sec % 60),
                "age": 0, "quote": "live",
                "price": (px if has_px else None),
                "bid": (px - sp if has_px else None),
                "ask": (px + sp if has_px else None),
                "is_mid": False, "chg": round(px - BASE, 1), "gap": 12.0,
                "rng": 40.0, "pos": round(r.next(), 3), "vol_ratio": round(r.next() * 3, 2),
                "mom5": round((r.next() - 0.5) * 20, 1),
                "mom15": round((r.next() - 0.5) * 30, 1),
                "idx": round(px * 0.99, 2), "idx_age": 1.0, "idx_chg": 5.0,
                "idx_pct": 0.04, "basis": round(px * 0.01, 1)})
            # ── 壞資料（負控組用）：一律從第 3 列以後才開始，前兩列保持乾淨 ──
            if all_bad_t:
                rows[-1]["t"] = "??"
            elif badts and 2 <= i < 2 + badts:
                rows[-1]["t"] = "??"
            elif strpx and 2 + badts <= i < 2 + badts + strpx:
                # ⚠️ 故意用**不能 float() 的字串**：數字字串只會炸在
                #    `px > row[1]`（TypeError），非數字字串連 `round(float(px))`
                #    都會炸（ValueError，就是 lab-qa 實測那一個）。取比較狠的那個。
                rows[-1]["price"] = "—"
            if with_k:
                rows[-1]["k"] = "t"
            prev = t_ms
            i += 1
        # 間隔抖動：±35%（真實那份的 p95 是中位的 1.6 倍左右）
        t_ms += max(60, int(every_ms * (0.65 + 0.7 * r.next())))
    p.write_bytes(("\r\n".join(json.dumps(x, ensure_ascii=False) for x in rows)
                   + "\r\n").encode("utf-8"))
    return len(rows)


def append_lines(path, lines):
    """往既有的逐筆檔續寫（模擬「今天還在長」）。⛔ 一定是 append，跟正式檔一樣。"""
    p = Path(path)
    with p.open("ab") as f:
        f.write(("\r\n".join(lines) + "\r\n").encode("utf-8"))
    return len(lines)


def append_half_line(path):
    """
    故意寫一個**沒有換行結尾**的半列（append 不是原子的，一定會讀到這個）。
    解析器必須把它丟掉、byte 位移退回上一個換行處。
    """
    Path(path).open("ab").write(b'{"k":"t","t":"09:2')


# ── 治具用的一組資料 ────────────────────────────────────────────────
# ⚠️ 日期**相對於「今天」**產生 —— 因為「今天還在長／增量／跟隨右緣」那一整條路
#    只有在檔名等於今天的時候才走得到。固定日期的話那一半永遠沒被測。
#    代價是探針不可以斷言固定日期字串，只能斷言「清單的第幾個」與相對關係。
KINDS = ("full", "gappy", "tiny", "blank", "decoy", "polled")


EXTRA = 5          # 再多幾天小檔案 —— 「連按 ◀ 5 次」那條測項需要翻得動 5 次


def fixture_dates(today):
    """today（date）→ {種類: 日期字串}。今天＝完整的那一份（還在長）。"""
    from datetime import timedelta
    d = {"full": str(today),
         "gappy": str(today - timedelta(days=1)),
         "tiny": str(today - timedelta(days=2)),
         "blank": str(today - timedelta(days=3)),
         "decoy": str(today - timedelta(days=4))}
    for i in range(EXTRA):
        d["extra%d" % i] = str(today - timedelta(days=5 + i))
    # 只有取樣檔、沒有逐筆檔的一天（放最舊，才不會擋到「連按 ◀ 5 次」那條）
    d["polled"] = str(today - timedelta(days=5 + EXTRA))
    return d


def write_gappy(out_dir, day, clean=False, big=True):
    """
    昨天那一份。clean=True ＝**負控組**：補滿缺的時段、拿掉痕跡列。
    「這段沒有錄到（不是沒行情）」「128」這幾句話必須跟著消失，
    證明它們不是恆真的裝飾字。

    ⚠️ 開頭故意少 **18 分鐘**（first 落在 09:03）。原本是 17 分鐘、first 落在 09:02，
       而 09:02 撞到他一筆真實交易的分鐘，那一輪就改掉了。
       ⚠️ **2026-09-07 之後這條規則已經被 PM 明確界定範圍**（見 CLAUDE.md
       「洩漏規則的適用範圍」）：**只到分鐘的 `HH:MM` 不在「命中一律改掉」的範圍內**
       —— 他 45 分鐘的下單窗口裡佔了十幾個分鐘，撞到幾乎必然，而單獨一個分鐘
       沒有價格／點數／方向任何旁證時資訊量近乎零；`leak-scan.py` 也刻意不掃它。
       所以 09:03 不必再改，**但同一行/相鄰行一旦還有價格或點數當旁證，就回到「一律改掉」**。
    """
    p = Path(out_dir) / f"{day}.jsonl"
    if clean:
        return synth_day(p, ticks=40000 if big else 3000, bidask=20000 if big else 1500,
                         first=31501, last=34199, seed=515)
    return synth_day(p, ticks=40000 if big else 3000, bidask=20000 if big else 1500,
                     first=32580, last=34198, seed=515,
                     drop=[(33000, 33180)],
                     gaps=[(33300, 128, "queue_full")], bad=2, heads=2)


def build(out_dir, today=None, big=True):
    """
    產一整組治具資料。big=False 時把最大那份縮小（跑得快，**但別拿它量效能**）。

    full  今天：完整量級的一天（真實量級 13.5 萬列）
    gappy 昨天：開頭少 18 分鐘 ＋ 中間一段完全沒錄 ＋ 一列 queue_full 128 筆 ＋ 2 列壞掉
    tiny  只有 3 列的合法逐筆檔（負控組：它**必須**出現在 days 裡）
    blank 只有檔頭（empty:true，UI 灰掉不可選）
    decoy **輪詢 schema** 但檔名長得像逐筆（⛔ 必須被 skipped，不可以進 days）
    polled 只有取樣檔的一天（kind="polled"：圖畫得出來、但沒有量、要標示清楚）
    另外在 full 那天**同時**放一個 -polled 檔 ⇒ 驗「兩種都有時逐筆優先、alt=True」
    """
    from datetime import date as _date
    today = today or _date.today()
    D = fixture_dates(today)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    made = {}
    made[D["full"]] = synth_day(out / f"{D['full']}.jsonl",
                                ticks=90000 if big else 6000,
                                bidask=45000 if big else 3000,
                                first=31502, last=34199, seed=20260908)
    made[D["gappy"]] = write_gappy(out, D["gappy"], clean=False, big=big)
    made[D["tiny"]] = synth_day(out / f"{D['tiny']}.jsonl", ticks=2, bidask=0,
                                first=31505, last=31506, seed=44)
    (out / f"{D['blank']}.jsonl").write_bytes(
        (json.dumps({"k": "h", "v": FMT_V, "src": "只有檔頭（合成）"},
                    ensure_ascii=False) + "\r\n").encode("utf-8"))
    made[D["blank"]] = 1
    made[D["decoy"]] = synth_polled(out / f"{D['decoy']}.jsonl")
    for i in range(EXTRA):
        k = D["extra%d" % i]
        made[k] = synth_day(out / f"{k}.jsonl", ticks=600, bidask=300,
                            first=31510, last=34100, seed=900 + i)
    # 只有取樣檔的一天：開頭少一段（09:05 才開始錄，跟他真實那份的形狀一樣）
    made[D["polled"]] = synth_polled_day(out / f"{D['polled']}-polled.jsonl",
                                         D["polled"], first=32700, last=34199,
                                         seed=8811, nopx=2)
    # full 那天**兩種檔都有** ⇒ 逐筆優先、alt=True（現在不會發生，但 tick_recorder 還在）
    synth_polled_day(out / f"{D['full']}-polled.jsonl", D["full"],
                     first=33000, last=33300, seed=1234)
    return made


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    d = args[0] if args else "__tmp__tick_logs"
    made = build(d, big=("--small" not in sys.argv))
    for k, v in sorted(made.items()):
        f = Path(d) / f"{k}.jsonl"
        print(f"{f.name}  {v:>7,} 列  {f.stat().st_size/1048576:.2f} MB")
