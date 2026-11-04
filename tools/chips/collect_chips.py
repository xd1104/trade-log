# -*- coding: utf-8 -*-
"""
【期交所籌碼每日收集】（2026-11-04 Benson 交辦）

期交所網站只留約 3 年；研究找到唯一的新機制（投信轉倉）以後要用長紀錄 ⇒ 每天自動補到最新，
留在 trade-log 的 `chips` 分支（公開市場數據，可以上 repo）。
⛔ 只用標準函式庫（GitHub Actions 不必裝套件）、⛔ 不登入、⛔ 不碰交易面板。

檔案（格式沿用 tick-research/chips/ 那幾支腳本產生的，研究腳本照樣讀得懂）：
  futco.csv          日盤三大法人 臺股期貨（大台）           futContractsDateDown
  mxf_inst.csv       日盤三大法人 小型臺指                   同上
  tmf_inst.csv       日盤三大法人 微型臺指（新）             同上
  fut_inst_all.csv   日盤三大法人 全部期貨契約（新）         同上（commodityId 留空）
  futco_ah.csv       夜盤三大法人 臺股期貨                   futContractsDateAhDown
  fut_inst_ah_all.csv 夜盤三大法人 全部期貨契約（新）        同上
  pcratio.csv        選擇權 P/C 比                            pcRatioDown
  mtx_oi.csv         小台全市場未平倉（一般時段、各月加總）  futDataDown MTX
  tx_oi.csv／tmf_oi.csv 大台／微台全市場未平倉（新）         futDataDown TX／TMF
  large_trader.csv   大額交易人（大台／小台／微台，新）       largeTraderFutDown
  etf_fut.csv        期貨型 ETF 每日期貨部位（新）：元大 00631L 正2／00632R 反1（PCF/Daily JSON，
                     `date`＝公告日、回傳的 trandate＝持倉日）、國泰 00663L 正2（GetETFDetailFutureList，
                     ⚠️ SearchDate 是不是持倉日還沒確認 ⇒ 來源欄標「查詢日」）。富邦 00675L 只有 HTML，沒收。

⛔ 期交所會 429（打太快）⇒ 每次請求間隔 GAP 秒、失敗退讓重試；一次最多抓 28 天。
⭐ 補漏日：每支檔各自從「自己最後一天」補到今天；另外拿 futco.csv 當交易日曆，
   最近 LOOKBACK_GAPS 天裡別支檔少了的日子會單獨再抓一次。

跑法：python collect_chips.py --dir <資料夾> [--start 2023-11-01] [--only futco.csv,...]
"""
import argparse
import csv
import io
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BASE = "https://www.taifex.com.tw/cht/3/"
UA = {"User-Agent": "Mozilla/5.0 (trade-log chips collector)"}
GAP = 8.0                 # 秒。⛔ 不要調小（期交所會 429）
CHUNK_DAYS = 28
LOOKBACK_GAPS = 60
YEARS_BACK = 3
TPE = timezone(timedelta(hours=8))
INDEX_CODES = {"TX", "MTX", "TMF"}
FUT_NAME = {"futco.csv": "臺股期貨", "mxf_inst.csv": "小型臺指期貨", "tmf_inst.csv": "微型臺指期貨"}


# ── 解析 ─────────────────────────────────────────────────────────────
def to_date(s):
    try:
        y, m, d = str(s).strip().replace("-", "/").split("/")
        return date(int(y), int(m), int(d))
    except Exception:
        return None


def fmt(d):
    return "%d/%02d/%02d" % (d.year, d.month, d.day)


def parse(txt, first_col):
    """期交所的 CSV ⇒ (表頭, 列)。⛔ 每列尾端多一個逗號 ⇒ 截到表頭長度；每格去頭尾空白。
    第一行不是 `first_col` 開頭 ⇒ 不是資料（查無資料／錯誤頁）⇒ (None, [])。"""
    txt = (txt or "").lstrip("﻿")
    lines = txt.splitlines()
    if not lines or not lines[0].strip().startswith(first_col):
        return None, []
    rd = list(csv.reader(io.StringIO(txt)))
    head = [h.strip() for h in rd[0]]
    while head and head[-1] == "":
        head.pop()
    rows = []
    for r in rd[1:]:
        r = [c.strip() for c in r][:len(head)]
        if not r or not any(r) or to_date(r[0]) is None:
            continue
        r += [""] * (len(head) - len(r))
        rows.append(r)
    return head, rows


def oi_sum(head, rows, session="一般"):
    """futDataDown ⇒ {日期: 全市場未平倉（該時段、各到期月份加總）}。'-' 不算。"""
    i_d, i_s, i_oi = head.index("交易日期"), head.index("交易時段"), head.index("未沖銷契約數")
    out = {}
    for r in rows:
        if r[i_s] != session:
            continue
        try:
            v = float(r[i_oi])
        except ValueError:
            continue
        out[r[i_d]] = out.get(r[i_d], 0.0) + v
    return out


# ── 讀寫本機檔 ───────────────────────────────────────────────────────
def read_csv(path):
    p = Path(path)
    if not p.exists():
        return None, []
    with p.open("r", encoding="utf-8", newline="") as f:
        rd = list(csv.reader(f))
    if not rd:
        return None, []
    return rd[0], rd[1:]


def dates_of(rows):
    return {d for d in (to_date(r[0]) for r in rows if r) if d}


def merge(path, head, new_rows):
    """append＋去重（整列相同才算重複）＋依日期排序。⇒ 新增幾列。
    ⛔ 舊檔表頭跟這次不一樣 ⇒ 拒絕（期交所改了格式要人看過，⛔ 不硬塞）。"""
    old_head, old = read_csv(path)
    if old_head is not None and [h.strip() for h in old_head] != head:
        raise ValueError("%s 的表頭跟期交所這次回的不一樣：%r vs %r" % (Path(path).name, old_head, head))
    seen = {tuple(r) for r in old}
    add = []
    for r in new_rows:
        t = tuple(r)
        if t not in seen:
            seen.add(t)
            add.append(r)
    if not add and old_head is not None:
        return 0
    allr = old + add
    allr.sort(key=lambda r: (to_date(r[0]) or date.min))      # 穩定排序：同一天保持原本順序
    tmp = Path(str(path) + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(head)
        w.writerows(allr)
    tmp.replace(path)
    return len(add)


# ── 抓 ───────────────────────────────────────────────────────────────
class Fetcher:
    def __init__(self, gap=GAP, opener=None, sleep=time.sleep, log=print):
        self.gap, self.sleep, self.log = gap, sleep, log
        self.opener = opener or self._urlopen
        self.last = 0.0
        self.n = 0          # 送出幾次（含重試）
        self.ok = 0         # 成功拿到回應幾次
        self.fail = 0       # 重試到放棄幾次

    @staticmethod
    def _urlopen(url, body):
        req = urllib.request.Request(url, data=body, headers=UA)
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read().decode("big5", "replace")

    def post(self, ep, data):
        return self.post_url(BASE + ep, urllib.parse.urlencode(data).encode())

    def post_url(self, url, body=None):
        ep = url.rsplit("/", 1)[-1][:40]
        for k in range(5):
            wait = self.gap - (time.monotonic() - self.last)
            if wait > 0:
                self.sleep(wait)
            self.last = time.monotonic()
            self.n += 1
            try:
                txt = self.opener(url, body)
                self.ok += 1
                return txt
            except Exception as e:                       # 429／逾時／斷線 ⇒ 退讓重試
                if k == 4:
                    self.fail += 1
                    self.log("  ⚠️ %s 放棄：%s" % (ep, str(e)[:80]))
                    return None
                self.sleep(20 * (2 ** k))
        return None


def chunks(d0, d1, days=CHUNK_DAYS):
    a = d0
    while a <= d1:
        b = min(a + timedelta(days=days - 1), d1)
        yield a, b
        a = b + timedelta(days=1)


def _range(a, b):
    f, l = fmt(a), fmt(b)
    return {"queryStartDate": f, "queryEndDate": l, "firstDate": f, "lastDate": l}


def get_inst(F, a, b, night=False):
    t = F.post("futContractsDateAhDown" if night else "futContractsDateDown",
               dict(_range(a, b), commodityId=""))
    return parse(t, "日期")


def get_pc(F, a, b):
    return parse(F.post("pcRatioDown", _range(a, b)), "日期")


def get_oi(F, a, b, code):
    d = {"down_type": "1", "commodity_id": code, "queryStartDate": fmt(a), "queryEndDate": fmt(b)}
    return parse(F.post("futDataDown", d), "交易日期")


def get_large(F, a, b):
    head, rows = parse(F.post("largeTraderFutDown", _range(a, b)), "日期")
    if head:
        rows = [r for r in rows if r[1] in INDEX_CODES]
    return head, rows


# ── 期貨型 ETF（投信網站，不是期交所）────────────────────────────────
ETF_HEAD = ["日期", "ETF", "契約", "月份", "口數", "權重%", "來源"]
ETF_GAP = 3.0
ETF_MAX_DAYS = 250         # 一次最多查幾個日子（Actions 一次跑不完的，下一次接著補）
ETF_START = date(2023, 11, 1)
YUANTA = ("https://etfapi.yuantaetfs.com/ectranslation/api/bridge?APIType=ETFAPI&CompanyName=YUANTAFUNDS"
          "&DeviceId=null&FuncId=PCF/Daily&AppName=ETF&Device=3&Platform=ETF&ticker=%s&date=%s")
CATHAY = "https://cwapi.cathaysite.com.tw/api/ETF/GetETFDetailFutureList?FundCode=%s&SearchDate=%s"
ETFS = [("yuanta", "00631L"), ("yuanta", "00632R"), ("cathay", "00663L")]
CATHAY_CODE = {"00663L": "87"}


def _get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read().decode("utf-8", "replace")


def etf_rows(kind, tk, d, txt):
    """投信回的 JSON ⇒ etf_fut.csv 的列。休市日／查無資料 ⇒ []。"""
    import json
    try:
        o = json.loads(txt)
    except Exception:
        return []
    out = []
    if kind == "yuanta":
        pcf = (o or {}).get("PCF") or {}
        td = str(pcf.get("trandate") or "")
        if len(td) != 8:
            return []
        dd = "%s/%s/%s" % (td[:4], td[4:6], td[6:])
        for f in ((o.get("FundWeights") or {}).get("FutureWeights") or []):
            q = f.get("qty")
            out.append([dd, tk, str(f.get("code", "")).strip(), str(f.get("ym", "")).strip(),
                        "%d" % float(q) if q is not None else "", str(f.get("weights", "")), "元大PCF持倉日"])
    else:
        for f in ((o or {}).get("result") or []):
            ym = str(f.get("ftDate", "")).replace("/", "").strip()
            q = str(f.get("volumn", "")).replace(",", "").strip()
            out.append([fmt(d), tk, str(f.get("ftNo", "")).strip(), ym, q, str(f.get("ntMkval", "")).strip(),
                        "國泰查詢日"])
    return out


def run_etf(dirp, today, start=None, F=None, log=print, max_days=ETF_MAX_DAYS):
    """⇒ 新增幾列。從 etf_fut.csv 最後一天往後一天一天查（週末跳過）。"""
    F = F or Fetcher(gap=ETF_GAP, opener=lambda url, body: _get(url), log=log)
    Path(dirp).mkdir(parents=True, exist_ok=True)
    dirp = Path(dirp)
    _h, rows = read_csv(dirp / "etf_fut.csv")
    ds = dates_of(rows)
    d = (max(ds) + timedelta(days=1)) if ds else (start or ETF_START)
    added, n = 0, 0
    while d <= today and n < max_days:
        if d.weekday() < 5:
            n += 1
            got = []
            for kind, tk in ETFS:
                url = (YUANTA % (tk, d.strftime("%Y%m%d")) if kind == "yuanta"
                       else CATHAY % (CATHAY_CODE[tk], fmt(d)))
                txt = F.post_url(url)
                if txt is None:                   # 重試到放棄 ⇒ 停在這一天，下次從這裡接著（⛔ 不跳過）
                    log("  etf      %s %s 抓不到，停在這一天" % (tk, d))
                    return added, F       # 這一天整天不寫（半天的資料會讓下次跳過它）
                got += etf_rows(kind, tk, d, txt)
            if got:
                added += merge(dirp / "etf_fut.csv", ETF_HEAD, got)
        d += timedelta(days=1)
    log("  etf      查了 %d 天 ⇒ 新增 %d 列（%s）" % (n, added, "還沒補完，下次接著" if d <= today else "到今天"))
    return added, F


# ── 每一組要更新哪幾支檔 ─────────────────────────────────────────────
OI_HEAD = ["交易日期", "oi"]
GROUPS = [
    # (名字, 這一組產生的檔, 抓法)
    ("inst", ["fut_inst_all.csv", "futco.csv", "mxf_inst.csv", "tmf_inst.csv"], "inst"),
    ("inst_ah", ["fut_inst_ah_all.csv", "futco_ah.csv"], "inst_ah"),
    ("pc", ["pcratio.csv"], "pc"),
    ("oi_mtx", ["mtx_oi.csv"], ("oi", "MTX")),
    ("oi_tx", ["tx_oi.csv"], ("oi", "TX")),
    ("oi_tmf", ["tmf_oi.csv"], ("oi", "TMF")),
    ("large", ["large_trader.csv"], "large"),
]


def fetch_group(F, how, a, b):
    """⇒ {檔名: (表頭, 列)}；抓不到 ⇒ {}。"""
    if how in ("inst", "inst_ah"):
        head, rows = get_inst(F, a, b, night=(how == "inst_ah"))
        if not head:
            return {}
        out = {("fut_inst_ah_all.csv" if how == "inst_ah" else "fut_inst_all.csv"): (head, rows)}
        names = {"futco_ah.csv": "臺股期貨"} if how == "inst_ah" else FUT_NAME
        for fn, nm in names.items():
            out[fn] = (head, [r for r in rows if r[1] == nm])
        return out
    if how == "pc":
        head, rows = get_pc(F, a, b)
        return {"pcratio.csv": (head, rows)} if head else {}
    if how == "large":
        head, rows = get_large(F, a, b)
        return {"large_trader.csv": (head, rows)} if head else {}
    if isinstance(how, tuple) and how[0] == "oi":
        head, rows = get_oi(F, a, b, how[1])
        if not head:
            return {}
        s = oi_sum(head, rows)
        fn = {"MTX": "mtx_oi.csv", "TX": "tx_oi.csv", "TMF": "tmf_oi.csv"}[how[1]]
        return {fn: (OI_HEAD, [[d, "%.1f" % v] for d, v in sorted(s.items(), key=lambda x: to_date(x[0]))])}
    raise ValueError(how)


def plan(dirp, files, today, start=None):
    """這一組從哪天補起：組裡**最舊**的那支檔的最後一天 +1（新檔 ⇒ start 或 3 年前）。"""
    lasts = []
    for fn in files:
        _h, rows = read_csv(dirp / fn)
        ds = dates_of(rows)
        lasts.append(max(ds) if ds else None)
    if any(x is None for x in lasts):
        return start or (today - timedelta(days=365 * YEARS_BACK + 5))
    return min(lasts) + timedelta(days=1)


def holes(dirp, files, today, look=LOOKBACK_GAPS):
    """拿 futco.csv 當交易日曆：最近 look 天裡這一組少了的交易日（每支檔在自己涵蓋範圍內才算）。"""
    _h, cal_rows = read_csv(dirp / "futco.csv")
    cal = {d for d in dates_of(cal_rows) if d >= today - timedelta(days=look)}
    miss = set()
    for fn in files:
        _h, rows = read_csv(dirp / fn)
        ds = dates_of(rows)
        if not ds:
            continue
        miss |= {d for d in cal if min(ds) <= d <= max(ds) and d not in ds}
    return sorted(miss)


def _spans(days):
    """連續的日子併成一段（相差 ≤ 4 天就算同一段，週末不拆）。"""
    out = []
    for d in days:
        if out and (d - out[-1][1]).days <= 4 and (d - out[-1][0]).days < CHUNK_DAYS:
            out[-1][1] = d
        else:
            out.append([d, d])
    return out


def run(dirp, today=None, start=None, only=None, F=None, log=print, F_etf=None):
    dirp = Path(dirp)
    dirp.mkdir(parents=True, exist_ok=True)
    today = today or datetime.now(TPE).date()
    F = F or Fetcher(log=log)
    report = {}
    for name, files, how in GROUPS:
        if only and not (set(files) & set(only)):
            continue
        d0 = plan(dirp, files, today, start)
        spans = [(a, b) for a, b in chunks(d0, today)] if d0 <= today else []
        if name != "inst":
            spans += [tuple(s) for s in _spans(holes(dirp, files, today))]
        added = {fn: 0 for fn in files}
        for a, b in spans:
            got = fetch_group(F, how, a, b)
            for fn, (head, rows) in got.items():
                if fn in added:
                    added[fn] += merge(dirp / fn, head, rows)
        report[name] = added
        log("  %-8s 從 %s 起抓 %d 段 ⇒ 新增 %s" % (name, d0, len(spans),
                                              "、".join("%s %d 列" % kv for kv in added.items())))
    if not only or "etf_fut.csv" in only:
        try:
            report["etf"] = {"etf_fut.csv": run_etf(dirp, today, log=log, F=F_etf)[0]}
        except Exception as e:                  # 投信網站壞了不影響期交所那幾支
            log("  ⚠️ etf 失敗：%s" % str(e)[:120])
    log("請求 %d 次、成功 %d 次、放棄 %d 次" % (F.n, F.ok, F.fail))
    return report, F


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--start")
    ap.add_argument("--only")
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    st = date.fromisoformat(a.start) if a.start else None
    only = a.only.split(",") if a.only else None
    _rep, F = run(a.dir, start=st, only=only)
    # 一次都沒抓成功 ＝ 被擋了（Actions 的 IP 被期交所擋掉就是這個樣子）⇒ 非零離開，讓人看得到
    return 3 if F.n and F.ok == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
