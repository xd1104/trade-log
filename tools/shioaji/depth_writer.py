# -*- coding: utf-8 -*-
"""
五檔全天落地 —— 研究用（2026-09-26，Benson：「只要我們錄的資料夠之後研究就好」）。

為什麼要錄：券商**不給過去的五檔**，錯過就永遠補不回來（成交、1 分 K、指數都抓得到過去的，所以不錄）。
錄什麼：面板收到的**每一筆五檔**（日盤＋夜盤全天），不限時段：
  - 微台（面板本來就訂的那一口，也是真單下單的合約）
  - 大台近月（主要市場，掛單最厚）
  - 台指選擇權近月、價平上下各 5 個履約價的買權＋賣權（盤中的「怕不怕」）

⛔⛔ 跟 tick_writer.py 同一條鐵律：**不可以拖慢 on_bidask**
  on_bidask 跑在永豐 SDK 的回呼執行緒上，微台的報價也從同一條執行緒進來、停損看的就是它。
  所以 push() **只 append 一個 tuple 進 deque**，連 float() 都不做；格式化、寫檔全在自己的執行緒。
  佇列滿了丟「新來的」並留痕跡列，⛔ 絕不阻塞。
  push() 自己包 try —— 這裡出任何錯都不可以冒回 SDK 執行緒。

【檔案】depth_logs/YYYY-MM-DD.jsonl（**交易所時間**的日期；夜盤過午夜就換下一天的檔），一定是 append。
  寫完的那幾天（日期 < 今天）由寫檔執行緒壓成 .jsonl.gz（約省 8~10 倍），原檔刪掉。
  ⚠️ 壓縮只碰「不是現在開著的檔」而且「超過 1 小時沒動」的，壓完先驗得回來才刪原檔。

【格式】一行一個 JSON：
  檔頭   {"k":"h", ...}（每次開檔寫一列，含欄位說明）
  五檔   ["TXFJ6","09:12:34.567",[買價×5],[買量×5],[賣價×5],[賣量×5]]   ← 陣列，省空間
  痕跡   {"k":"x","wt":"…","after":"…","n":N,"why":"queue_full"}
  讀檔：開頭是 "[" 的是資料；是 "{" 的看 k。
"""

import gzip
import json
import os
import threading
import time
from collections import deque
from datetime import datetime, date
from pathlib import Path

DEFAULT_MAX_QUEUE = 400_000      # 寫檔執行緒整個卡住時的記憶體天花板（一列小 tuple，約 100MB）
GZIP_AFTER_S = 3600              # 檔案超過這麼久沒動才壓
GZIP_EVERY_S = 1800              # 多久檢查一次要不要壓
FORMAT_VERSION = 1


class DepthWriter:
    def __init__(self, out_dir, max_queue=DEFAULT_MAX_QUEUE, flush_every=2.0, log=None, today_fn=None):
        self.out_dir = Path(out_dir)
        self.flush_every = flush_every
        self._max = max_queue
        self._log = log or (lambda msg: print(msg, flush=True))
        self._today = today_fn or date.today
        self._q = deque()
        self._droplock = threading.Lock()
        self._dropped_pending = 0
        self.dropped = 0
        self.lost_io = 0
        self.written = 0
        self.by_code = {}            # 今天（寫檔執行緒看到的交易所日期）每個合約幾筆
        self.by_code_day = None
        self.gz_done = 0
        self.gz_err = None
        self._last_gz = 0.0
        self._stop = threading.Event()
        self._thread = None
        self._fh = None
        self._fh_day = None
        self._io_err = None

    # ------------------------------------------------------------ 熱路徑（on_bidask）

    def push(self, ba):
        """⛔ 只 append。任何錯都吞掉（不可以冒回永豐的執行緒）。"""
        try:
            q = self._q
            if len(q) >= self._max:
                with self._droplock:
                    self._dropped_pending += 1
                    self.dropped += 1
                return
            q.append((ba.code, ba.datetime, ba.bid_price, ba.bid_volume, ba.ask_price, ba.ask_volume))
        except Exception:
            pass

    # ------------------------------------------------------------ 寫檔執行緒

    def start(self):
        if self._thread is not None:
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="depth-writer", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout=5.0):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        else:
            self._safe_drain()
            self._close()

    def _loop(self):
        while not self._stop.is_set():
            self._stop.wait(self.flush_every)
            self._safe_drain()
            if time.time() - self._last_gz >= GZIP_EVERY_S:
                self._last_gz = time.time()
                self.compress_old()
        self._safe_drain()
        self._close()

    def _safe_drain(self):
        try:
            self._drain()
        except Exception as e:
            msg = "%s: %s" % (type(e).__name__, e)
            if msg != self._io_err:
                self._io_err = msg
                self._log("⚠️ [depth_logs] 寫檔失敗（已丟失 %d 列）：%s" % (self.lost_io, msg))
            self._close()

    def _drain(self):
        q = self._q
        rows = []
        for _ in range(len(q)):
            try:
                rows.append(q.popleft())
            except IndexError:
                break
        with self._droplock:
            dropped, self._dropped_pending = self._dropped_pending, 0
        if not rows and not dropped:
            return
        by_day = {}
        last_ts = None
        for r in rows:
            ts = r[1]
            if ts is None:
                continue
            last_ts = ts
            line = _line(r)
            if line is None:
                continue
            d = ts.date()
            by_day.setdefault(d, []).append(line)
            if d != self.by_code_day:
                if self.by_code_day is None or d > self.by_code_day:
                    self.by_code, self.by_code_day = {}, d
            if d == self.by_code_day:
                self.by_code[r[0]] = self.by_code.get(r[0], 0) + 1
        if dropped:
            d = last_ts.date() if last_ts else self._today()
            by_day.setdefault(d, []).append(_mark(dropped, "queue_full", last_ts))
            self._log("⚠️ [depth_logs] 佇列滿了，丟掉 %d 筆（累計 %d）" % (dropped, self.dropped))
        for d in sorted(by_day):
            buf = by_day[d]
            try:
                fh = self._file(d)
                fh.write("".join(buf))
                fh.flush()          # 行程被砍也不會掉（看 tick_writer.py 同一行的說明）
            except Exception:
                self.lost_io += len(buf)
                self._close()
                raise
        self.written += len(rows)
        self._io_err = None

    def _file(self, day):
        if self._fh is not None and self._fh_day == day:
            return self._fh
        self._close()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / ("%s.jsonl" % day.isoformat())
        self._fh = path.open("a", encoding="utf-8")      # ⛔ 一定是 "a"
        self._fh_day = day
        self._fh.write(json.dumps(_header(), ensure_ascii=False, separators=(",", ":")) + "\n")
        return self._fh

    def _close(self):
        if self._fh is not None:
            try:
                self._fh.flush()
                self._fh.close()
            except Exception:
                pass
        self._fh = None
        self._fh_day = None

    # ------------------------------------------------------------ 壓縮寫完的那幾天

    def compress_old(self, now_ts=None):
        """日期 < 今天、不是開著的、超過 1 小時沒動 ⇒ 壓成 .gz，驗過再刪原檔。回傳這次壓了幾個。"""
        now_ts = now_ts or time.time()
        today = self._today()
        n = 0
        try:
            files = sorted(self.out_dir.glob("*.jsonl")) if self.out_dir.exists() else []
        except Exception:
            return 0
        for p in files:
            try:
                d = date.fromisoformat(p.stem)
            except ValueError:
                continue
            if d >= today or d == self._fh_day or now_ts - p.stat().st_mtime < GZIP_AFTER_S:
                continue
            gz = p.with_name(p.name + ".gz")
            tmp = p.with_name(p.name + ".gz.tmp")
            try:
                raw = p.read_bytes()
                if gz.exists():            # 同一天又被寫了一段（例如重啟）⇒ 接在後面：gzip 允許多段
                    old = gzip.decompress(gz.read_bytes())
                    raw = old + raw
                tmp.write_bytes(gzip.compress(raw, compresslevel=6))
                if gzip.decompress(tmp.read_bytes()) != raw:
                    raise IOError("壓完解不回原樣")
                os.replace(tmp, gz)
                p.unlink()
                n += 1
                self.gz_done += 1
                self.gz_err = None
            except Exception as e:
                self.gz_err = "%s：%s" % (p.name, str(e)[:80])
                try:
                    tmp.unlink()
                except Exception:
                    pass
        return n

    def stats(self):
        return {"written": self.written, "dropped": self.dropped, "lost_io": self.lost_io,
                "queued": len(self._q), "day": self.by_code_day.isoformat() if self.by_code_day else None,
                "by_code": dict(self.by_code), "gz_err": self.gz_err}


def _hms(ts):
    return "%02d:%02d:%02d.%03d" % (ts.hour, ts.minute, ts.second, ts.microsecond // 1000)


def _num(x):
    f = float(x)
    return int(f) if f == int(f) else f


def _line(r):
    code, ts, bp, bv, ap, av = r
    try:
        row = [str(code), _hms(ts), [_num(x) for x in bp], [int(x) for x in bv],
               [_num(x) for x in ap], [int(x) for x in av]]
    except Exception:
        return None
    return json.dumps(row, separators=(",", ":")) + "\n"


def _mark(n, why, after=None):
    o = {"k": "x", "wt": _hms(datetime.now()), "after": _hms(after) if after is not None else None,
         "n": n, "why": why}
    return json.dumps(o, ensure_ascii=False, separators=(",", ":")) + "\n"


def _header():
    return {
        "k": "h", "v": FORMAT_VERSION, "at": datetime.now().isoformat(timespec="milliseconds"),
        "pid": os.getpid(), "src": "live_panel.py on_bidask（全部訂閱的合約）",
        "clock": "永豐給的交易所時間（ba.datetime）；檔名也是交易所日期。痕跡列的 wt 才是本機時鐘",
        "row": "[合約代碼, HH:MM:SS.mmm, 買價×5（最好的在前）, 買量×5, 賣價×5, 賣量×5]；價格 0 ＝ 那一檔沒人掛",
        "codes": "TMF…＝微台（真單合約）；TXF…＝大台近月；TXO…＝台指選擇權（代碼含履約價，C/P 看月份字母：A~L 買權、M~X 賣權）",
        "order": "照收到的順序寫，不保證時間單調遞增；分析請自己排序",
        "note": "append 模式：面板每重啟一次就多一列檔頭",
    }
