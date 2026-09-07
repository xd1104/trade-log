# -*- coding: utf-8 -*-
"""
逐筆報價落地 —— 把面板收到的每一筆成交與買賣價寫進 tick_logs/YYYY-MM-DD.jsonl。

⛔⛔ 這個檔存在的唯一理由：**不可以拖慢 on_tick**
=================================================
`on_tick` 跑在永豐 SDK 的回呼執行緒上，而它更新的 `Today.price` 就是停損判斷看的那個價
（永豐沒有停損單，停損活在面板的 Python 迴圈裡 —— Benson 2026-08-28 拍板承擔的風險）。
一筆 tick 卡 5ms，早上四萬筆就是 200 秒；真的塞住的時候，塞住的是他的停損。

所以：
  - 生產者（on_tick / on_bidask）**只做一件事：append 一個 tuple 進 deque**。
    沒有磁碟 I/O、沒有鎖、沒有 json.dumps、沒有 strftime（格式化全部丟給寫檔執行緒）。
  - 一條 daemon 執行緒每 `flush_every` 秒批次取出、格式化、寫檔、flush。
  - `deque.append` / `popleft` 在 CPython 是原子操作，所以熱路徑連鎖都不用拿。

【佇列滿了怎麼辦】**寧可丟資料，也不可以阻塞 on_tick。**
丟的是「新來的那一筆」不是隊伍最前面的舊資料 —— 舊的已經排好隊、丟掉它等於在檔案中間
挖一個看不出來的洞；丟新的則是「斷在這裡、之後接回來」，而且：
  - 計數（`dropped`），
  - 在檔案裡寫一列痕跡 `{"k":"x","n":N}`（就落在斷點附近，前後兩列的時間就是缺口的範圍），
  - 主控台印一行警告。
**不可以安靜地少。**（deque(maxlen=N) 剛好相反：它默默丟最舊的，所以刻意不用。）

【時段】只錄 08:45:00 ~ 09:30:00（Benson 的下單時段），判斷用**永豐給的 tick 時間**
（`tick.datetime`）不是本機時鐘，理由見 `_in_window()`。

【檔案】`tick_logs/YYYY-MM-DD.jsonl`，**一定是 append**（`"a"`）。
看門狗重啟是常態（永豐 SDK 斷線會把行程帶掉），覆寫就是把當天稍早的資料弄丟 ——
這個專案已經用 `TODAY_TRADES` 弄丟過一次真實資料，不再犯第二次。

【格式】一行一個 JSON 物件，欄位名刻意用單字母（一天十萬列，短欄位名省一半空間）：
    {"k":"h", ...}                                   檔頭（每次開檔寫一列，含格式說明）
    {"k":"t","t":"09:12:34.567","p":12245.0,"v":3}   成交（tick）
    {"k":"b","t":"09:12:34.567","b":12242.0,"a":12245.0}  買賣價（bidask）
    {"k":"x","wt":"09:12:35.001","after":"09:12:34.998","n":128,"why":"queue_full"}
                                                     出事的痕跡列（丟棄／日期亂跳）
  （上面的價格是**編的**，刻意離真實行情很遠 —— 這個 repo 是公開的，
    示範數字撞到他某一筆真實成交，看的人分不出是巧合還是外洩。）
  t = 永豐給的交易所時間，HH:MM:SS.mmm（日期看檔名）。
  讀檔的人：認得的 k 才處理，不認得的整列跳過（之後加新種類不會弄壞舊程式）。

【痕跡列（k=x）為什麼不叫 `t`】它身上**沒有交易所時間可用** —— 丟棄發生在生產者那一邊，
當下那一筆連進佇列都沒進來，寫檔執行緒 drain 到的時候只知道「這一輪之前掉了 n 筆」。
所以刻意改名：
  - `wt` = **本機時鐘**，而且是**寫檔執行緒 drain 的時刻**，不是丟棄發生的時刻
    （最多差一個 `flush_every`）。名字跟資料列的 `t` 分開，是為了不讓分析腳本
    把它當成交易所時間混進時間軸 —— 檔頭 `clock` 欄位寫著「不是本機時鐘」，
    那句話講的是 `t`，`wt` 是唯一的例外，所以必須一眼看得出來。
  - `after` = **同一批裡前一列的交易所時間**（沒有就 null）。缺口的起點下界在這裡，
    終點看檔案裡的下一列 —— 這一對才是分析真正用得上的東西。

【順序】檔案裡的列**照收到的順序寫，不保證時間單調遞增**。
重連補送、SDK 亂序、跨檔頭的重啟都可能讓時間往回跳一點；分析腳本要有序就自己排序。
"""

import json
import os
import threading
from collections import deque
from datetime import datetime, time as dtime
from pathlib import Path

# 預設時段。面板會把自己的 SESSION_OPEN / WATCH_END 明確傳進來（單一來源在呼叫端），
# 這裡的預設值是給單獨使用與測試用的。
DEFAULT_START = dtime(8, 45, 0)
DEFAULT_END = dtime(9, 30, 0)

# 佇列上限。正常一個早上約 4~10 萬列，這個數字是「寫檔執行緒整個卡住」時的記憶體天花板
# （一列是個小 tuple，20 萬列約 40MB）。到頂就丟新的並留痕跡，不阻塞生產者。
DEFAULT_MAX_QUEUE = 200_000

# 一批之內最多准許換幾次檔。正常一批全部是同一天（08:45~09:30 不跨午夜），
# 換一次已經是異常，換到第 5 次就是資料在亂跳。
# 為什麼要擋：每換一次日期就 close + open + 寫一列檔頭 —— 實測 200 筆交錯日期時
# 一次 drain 從 1.8ms 變 97.1ms，而且兩個檔各 200 列裡有 100 列是檔頭（檔案被檔頭灌爆）。
# 全部發生在寫檔執行緒、on_tick 不受影響，但那條執行緒卡住就代表「這段沒錄到」。
# 超過就停止分檔、全部寫進這一批的第一個日期，並留一列 {"k":"x","why":"date_thrash"}。
MAX_DAY_SWITCH = 4

FORMAT_VERSION = 1


class TickWriter:
    """
    用法：
        w = TickWriter(HERE / "tick_logs", win_start=SESSION_OPEN, win_end=WATCH_END)
        w.start()
        ...  w.tick(ts, price, volume)      ← 在 on_tick 裡呼叫，只 append
        ...  w.bidask(ts, bid, ask)         ← 在 on_bidask 裡呼叫，只 append
        w.stop()                            ← 收尾（測試用；面板整天掛著不會呼叫）
    """

    def __init__(self, out_dir, win_start=DEFAULT_START, win_end=DEFAULT_END,
                 max_queue=DEFAULT_MAX_QUEUE, flush_every=1.0, opener=None,
                 log=None):
        # ⚠️ 欄位名刻意是 win_start/win_end 不是 start/end —— start 是啟動執行緒的方法名，
        #    撞名的話 `self.start` 會被蓋掉（第一版就是這樣炸的）。
        self.out_dir = Path(out_dir)
        self.win_start = win_start
        self.win_end = win_end
        self.flush_every = flush_every
        self._max = max_queue
        # opener(path) -> file object。測試用它塞一個「故意很慢」的假 writer，
        # 就能證明磁碟慢的時候 on_tick 仍然不受影響。
        self._opener = opener or (lambda p: p.open("a", encoding="utf-8"))
        self._log = log or (lambda msg: print(msg))

        self._q = deque()
        self._droplock = threading.Lock()
        self._dropped_pending = 0      # 還沒寫進檔案的丟棄數
        self.dropped = 0               # 累計丟棄數：佇列滿（給報告/測試看）
        self.lost_io = 0               # 累計丟失數：寫檔失敗（已經離開佇列、救不回來）
        self.written = 0               # 累計寫出的資料列數（不含檔頭與痕跡列）
        self._stop = threading.Event()
        self._thread = None
        self._fh = None
        self._fh_day = None
        self._io_err = None

    # ------------------------------------------------------------ 熱路徑（on_tick）

    def _in_window(self, ts):
        """
        用**永豐給的交易所時間**判斷時段，不是本機時鐘。三個理由：

        1. 檔案裡存的就是這個時間 —— 兩者不一致的話會出現「時段外的時間戳」
           （例如本機比交易所快 2 秒，開盤瞬間會寫進幾列 08:44:59.x）。
        2. 分析時要跟 1 分 K 對得起來，而 1 分 K 是交易所時間。本機時鐘含網路延遲。
        3. 面板本體判斷 `in_session` 用的也是 `tick.datetime`（見 on_tick），同一把尺。

        代價：永豐時間壞掉的話會錄錯（實務上沒發生過；壞了看檔案裡的時間戳就看得出來）。
        跨日／夜盤不必另外防：08:45~09:30 這段只存在於日盤，夜盤是 15:00~05:00，不重疊。
        """
        # ⛔ 這是熱路徑上唯一的防呆，而且它擋的是**回呼執行緒上的例外**：
        #    ts 是 None 時 `ts.time()` 會丟 AttributeError，那個例外會往上冒回永豐
        #    SDK 的執行緒（on_tick 沒有 try）。守衛：test_tick_writer.py ①b。
        if ts is None:
            return False
        t = ts.time()
        return self.win_start <= t <= self.win_end

    def tick(self, ts, price, volume):
        """成交。⚠️ 這裡面不可以出現任何磁碟 I/O、鎖、或字串格式化。"""
        if not self._in_window(ts):
            return
        q = self._q
        if len(q) >= self._max:
            self._drop()
            return
        q.append(("t", ts, price, volume))

    def bidask(self, ts, bid, ask):
        """
        買賣價。價差是事後絕對補不回來的東西，而 on_bidask 現在就拿得到，
        所以跟成交記在同一個檔（用 k 分辨），不記白不記。
        """
        if not self._in_window(ts):
            return
        q = self._q
        if len(q) >= self._max:
            self._drop()
            return
        q.append(("b", ts, bid, ask))

    def _drop(self):
        """佇列滿了。只計數（極少發生，這裡拿鎖不影響正常路徑），痕跡由寫檔執行緒落地。"""
        with self._droplock:
            self._dropped_pending += 1
            self.dropped += 1

    # ------------------------------------------------------------ 寫檔執行緒

    def start(self):
        if self._thread is not None:
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="tick-writer", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout=5.0):
        """收尾：把還在佇列裡的寫完再關檔。面板整天掛著，平常不會呼叫。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        else:
            self._drain()
            self._close()

    def _loop(self):
        while not self._stop.is_set():
            self._stop.wait(self.flush_every)
            self._safe_drain()
        self._safe_drain()      # 收尾這一輪
        self._close()

    def _safe_drain(self):
        """
        寫檔出任何錯都不可以讓這條執行緒死掉 —— 死了之後佇列只會漲到滿，
        然後開始丟資料，而且沒有人會知道。
        """
        try:
            self._drain()
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            if msg != self._io_err:          # 同一個錯只吵一次
                self._io_err = msg
                self._log(f"⚠️ [tick_logs] 寫檔失敗（已丟失 {self.lost_io} 列）：{msg}")
            self._close()                    # 下一輪重開，也許就好了

    def _drain(self):
        q = self._q
        n = len(q)                # 只處理這一輪已經在隊伍裡的；新進來的下一輪再說
        rows = []
        for _ in range(n):
            try:
                rows.append(q.popleft())
            except IndexError:
                break
        with self._droplock:
            dropped, self._dropped_pending = self._dropped_pending, 0
        if not rows and not dropped:
            return

        # 一批可能跨到隔天（理論上不會，08:45~09:30 不跨午夜），還是照日期分檔寫。
        # ⚠️ 但「照日期分檔」在日期亂跳時會退化：每換一次就 close+open+寫檔頭
        #    ⇒ drain 慢 54 倍、檔案一半是檔頭。換超過 MAX_DAY_SWITCH 次就不再分檔。
        buf = []
        day = None
        first_day = rows[0][1].date() if rows else None
        switches = 0
        thrashed = 0            # 被強制寫進第一個日期的列數
        last_ts = None          # 這一批最後一列的交易所時間（給痕跡列的 after 用）
        for r in rows:
            ts = r[1]
            last_ts = ts
            d = ts.date()
            if thrashed:
                d = first_day
            elif d != day and day is not None:
                switches += 1
                if switches > MAX_DAY_SWITCH:
                    d = first_day
            if d != day:
                self._flush_buf(day, buf)
                buf, day = [], d
            if d != ts.date():
                thrashed += 1
            buf.append(_line(r))
        if thrashed:
            # 不是「安靜地混進去」：哪幾列被搬過家，檔案裡看得出來。
            buf.append(_mark(dropped=thrashed, why="date_thrash", after=last_ts))
            self._log(f"⚠️ [tick_logs] 一批之內日期跳超過 {MAX_DAY_SWITCH} 次，"
                      f"{thrashed} 列改寫進 {first_day}（停止分檔，避免檔頭灌爆檔案）")
        if dropped:
            # 痕跡就落在斷點附近：`after`（前一列的交易所時間）與檔案裡的下一列
            # 夾出缺口的範圍。`wt` 是本機時鐘、而且是 drain 的時刻 —— 看檔頭的說明。
            if day is None:
                day = datetime.now().date()
            buf.append(_mark(dropped=dropped, why="queue_full", after=last_ts))
            self._log(f"⚠️ [tick_logs] 佇列滿了，丟掉 {dropped} 筆（累計 {self.dropped}）")
        self._flush_buf(day, buf)
        self.written += len(rows)
        self._io_err = None

    def _flush_buf(self, day, buf):
        if not buf or day is None:
            return
        try:
            fh = self._file(day)
            fh.write("".join(buf))
            # ⛔ 這一行是「行程被砍也不會掉」的**唯一**保證：永豐 SDK 斷線會把整個行程
            #    帶掉、看門狗再重開，那條路上 stop() 根本不會被呼叫，Python 的檔案緩衝
            #    就跟著行程一起消失。真的斷電才可能掉 OS 快取那一段。
            #    守衛：test_tick_writer.py ②b（子行程寫幾筆 → os._exit(9) → 父行程去讀檔）。
            #    ⚠️ 測試每一節收尾都會 stop()（＝關檔＝一定落地），所以這一行在那些節裡
            #       **結構上不可能承重** —— 一定要用「不呼叫 stop() 就把行程幹掉」才驗得到。
            fh.flush()
        except Exception:
            # 寫不出去（磁碟滿了、檔案被鎖住…）＝ 這一批就是沒了。
            # 這幾列已經從佇列拿出來了，救不回來，但**一定要算得出來**：
            # 「安靜地少」比「少了但知道少多少」危險得多。
            self.lost_io += len(buf)
            self._close()
            raise

    def _file(self, day):
        if self._fh is not None and self._fh_day == day:
            return self._fh
        self._close()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / f"{day:%Y-%m-%d}.jsonl"
        # ⛔ 一定是 "a"。看門狗重啟後改成 "w" 就是把當天稍早錄好的資料整份抹掉。
        self._fh = self._opener(path)
        self._fh_day = day
        self._fh.write(json.dumps(_header(self.win_start, self.win_end), ensure_ascii=False,
                                  separators=(",", ":")) + "\n")
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

    # ------------------------------------------------------------ 給報告看的數字

    def stats(self):
        return {"written": self.written, "dropped": self.dropped,
                "lost_io": self.lost_io,
                "queued": len(self._q), "max_queue": self._max}


def _hms(ts):
    """HH:MM:SS.mmm（毫秒）。比 isoformat 短，日期看檔名就好。"""
    return f"{ts.hour:02d}:{ts.minute:02d}:{ts.second:02d}.{ts.microsecond // 1000:03d}"


def _mark(dropped, why, after=None):
    """
    痕跡列（k=x）。⚠️ 這裡刻意**沒有 `t` 欄位**。

    `t` 在這個檔案裡的定義是「永豐給的交易所時間」（檔頭 `clock` 那句），而痕跡列
    根本沒有交易所時間可用：丟棄發生在生產者那一端，那一筆連進佇列都沒進來。
    寫成 `t` 的話就是拿本機時鐘冒充交易所時間，而且冒充得看不出來 —— 所以分成兩欄：
      wt    = 本機時鐘，drain 的時刻（不是丟棄發生的時刻，最多差一個 flush_every）
      after = 同一批裡前一列的交易所時間，缺口的起點下界；沒有就 null
    """
    o = {"k": "x", "wt": _hms(datetime.now()),
         "after": _hms(after) if after is not None else None,
         "n": dropped, "why": why}
    return json.dumps(o, ensure_ascii=False, separators=(",", ":")) + "\n"


def _line(r):
    kind, ts, a, b = r
    if kind == "t":
        o = {"k": "t", "t": _hms(ts), "p": a, "v": b}
    else:
        o = {"k": "b", "t": _hms(ts), "b": a, "a": b}
    return json.dumps(o, ensure_ascii=False, separators=(",", ":")) + "\n"


def _header(start=DEFAULT_START, end=DEFAULT_END):
    return {
        "k": "h",
        "v": FORMAT_VERSION,
        "at": datetime.now().isoformat(timespec="milliseconds"),
        "pid": os.getpid(),
        "src": "live_panel.py on_tick / on_bidask",
        "win": f"{start:%H:%M:%S}~{end:%H:%M:%S}",
        "clock": "永豐給的交易所時間（tick.datetime），不是本機時鐘。"
                 "⚠️ 唯一的例外是痕跡列（k=x）的 wt，那一欄才是本機時鐘",
        "fmt": {
            "k": "t=成交　b=買賣價　x=痕跡（丟棄/日期亂跳）　h=檔頭",
            "t": "HH:MM:SS.mmm（日期看檔名）；只有 t/b 列有這一欄",
            "p": "成交價", "v": "單筆成交量",
            "b": "買價", "a": "賣價",
            "n": "受影響的筆數（丟棄的筆數／被改寫日期的列數）",
            "wt": "只在 k=x：本機時鐘，而且是寫檔執行緒 drain 的時刻，"
                  "不是事情發生的時刻（最多差一個 flush_every）",
            "after": "只在 k=x：同一批裡前一列的交易所時間（缺口的起點下界）；"
                     "沒有就是 null",
            "why": "只在 k=x：queue_full=佇列滿丟資料／date_thrash=一批之內日期跳太多次，"
                   "停止分檔、全部寫進第一個日期",
        },
        "order": "照收到的順序寫，**不保證時間單調遞增**（重連補送、SDK 亂序、"
                 "重啟接檔都可能讓時間往回跳）。要有序請自己排序，不要假設它已經排好。",
        "note": "append 模式：面板每重啟一次就多一列檔頭，中間的資料不會被蓋掉。",
    }
