# -*- coding: utf-8 -*-
"""collect_chips.py 的離線測試：假的期交所（⛔ 不連網）、暫存資料夾。"""
import shutil
import sys
import tempfile
import urllib.parse
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.stdout.reconfigure(encoding="utf-8")
import collect_chips as C   # noqa: E402

FAIL = N = 0


def chk(name, got, want):
    global FAIL, N
    N += 1
    print(("  ok   %s" % name) if got == want else ("  FAIL %s：got %r want %r" % (name, got, want)))
    FAIL += got != want


INST_H = ("日期,商品名稱,身份別,多方交易口數,多方交易契約金額(千元),空方交易口數,空方交易契約金額(千元),"
          "多空交易口數淨額,多空交易契約金額淨額(千元),多方未平倉口數,多方未平倉契約金額(千元),"
          "空方未平倉口數,空方未平倉契約金額(千元),多空未平倉口數淨額,多空未平倉契約金額淨額(千元)")
AH_H = ("日期,商品名稱,身份別,多方交易口數,多方交易契約金額(千元),空方交易口數,空方交易契約金額(千元),"
        "多空交易口數淨額,多空交易契約金額淨額(千元)")
PC_H = "日期,賣權成交量,買權成交量,買賣權成交量比率%,賣權未平倉量,買權未平倉量,買賣權未平倉量比率%"
OI_H = ("交易日期,契約,到期月份(週別),開盤價,最高價,最低價,收盤價,漲跌價,漲跌%,成交量,結算價,未沖銷契約數,"
        "最後最佳買價,最後最佳賣價,歷史最高價,歷史最低價,是否因訊息面暫停交易,交易時段,價差對單式委託成交量")
LT_H = ("日期,商品(契約),商品名稱(契約名稱),到期月份(週別),交易人類別,前五大交易人買方,前五大交易人賣方,"
        "前十大交易人買方,前十大交易人賣方,全市場未沖銷部位數")


class FakeTaifex:
    """依請求的日期區間吐假資料（每列尾端多一個逗號，跟真的一樣）。closed＝休市日。"""

    def __init__(self, closed=(), fail_first=0, blocked=False):
        self.closed, self.calls, self.fail_first, self.blocked = set(closed), [], fail_first, blocked

    def days(self, a, b):
        d = a
        while d <= b:
            if d.weekday() < 5 and d not in self.closed:
                yield d
            d += timedelta(days=1)

    def __call__(self, url, body):
        q = dict(urllib.parse.parse_qsl(body.decode()))
        ep = url.rsplit("/", 1)[1]
        self.calls.append((ep, q))
        if self.blocked:
            raise OSError("HTTP Error 403: Forbidden")
        if self.fail_first > 0:
            self.fail_first -= 1
            raise OSError("HTTP Error 429: Too Many Requests")
        a, b = (C.to_date(q.get("queryStartDate")), C.to_date(q.get("queryEndDate")))
        ds = list(self.days(a, b))
        if not ds:
            return "<html>查無資料</html>"
        L = []
        if ep == "futContractsDateDown":
            L.append(INST_H)
            for d in ds:
                for nm in ("臺股期貨", "小型臺指期貨", "微型臺指期貨", "電子期貨"):
                    for who in ("自營商", "投信", "外資及陸資"):
                        L.append(",".join([C.fmt(d), nm, who] + ["%d" % (d.day * 10 + 1)] * 12) + ",")
        elif ep == "futContractsDateAhDown":
            L.append(AH_H)
            for d in ds:
                for nm in ("臺股期貨", "小型臺指期貨"):
                    L.append(",".join([C.fmt(d), nm, "外資及陸資"] + ["5"] * 6) + ",")
        elif ep == "pcRatioDown":
            L.append(PC_H)
            for d in reversed(ds):                        # 真的是新到舊
                L.append("%s,1000,1200,83.33,900,1000,90.00," % C.fmt(d))
        elif ep == "futDataDown":
            L.append(OI_H)
            for d in ds:
                for m, oi in (("202601", "100"), ("202602", "20"), ("202601W2", "-")):
                    L.append("%s,%s,%s,12000,12010,11990,12005,5,0.04%%,10,12005,%s,12004,12006,13000,11000,,一般,," %
                             (C.fmt(d), q["commodity_id"], m, oi))
                L.append("%s,%s,202601,12000,12010,11990,12005,5,0.04%%,10,-,-,12004,12006,13000,11000,,盤後,," %
                         (C.fmt(d), q["commodity_id"]))
        elif ep == "largeTraderFutDown":
            L.append(LT_H)
            for d in ds:
                for code in ("TX     ", "MTX    ", "TMF    ", "CA     "):
                    L.append("%s,%s,名,999999  ,0,1,2,3,4,5" % (C.fmt(d), code))
        return "\n".join(L) + "\n"


class FakeETF:
    """假的投信 API：元大回 PCF（持倉日＝前一個平日）、國泰回 result；週末回空。"""

    def __init__(self, fail_on=None):
        self.calls, self.fail_on = [], fail_on

    def __call__(self, url, body=None):
        import json
        self.calls.append(url)
        if self.fail_on and self.fail_on in url:
            raise OSError("timeout")
        if "yuantaetfs" in url:
            ds = url.rsplit("date=", 1)[1]
            d = date(int(ds[:4]), int(ds[4:6]), int(ds[6:]))
            if d.weekday() >= 5:
                return json.dumps({"PCF": None})
            p = d - timedelta(days=1 if d.weekday() else 3)
            q = -2450.0 if "00632R" in url else 50455.0
            return json.dumps({"PCF": {"trandate": p.strftime("%Y%m%d")},
                               "FundWeights": {"FutureWeights": [
                                   {"code": "TX", "ym": "203002", "qty": q, "weights": 159.2},
                                   {"code": "NYF", "ym": "203002", "qty": 7800.0, "weights": 2.86}]}})
        d = C.to_date(urllib.parse.unquote(url.rsplit("SearchDate=", 1)[1]))
        if d.weekday() >= 5:
            return json.dumps({"result": []})
        return json.dumps({"result": [{"ftNo": "TX        ", "ftDate": "2030/02", "volumn": "3,251",
                                       "ntMkval": "199.04"}]})


def ETF_F(fake=None):
    return C.Fetcher(gap=0, opener=fake or FakeETF(), sleep=lambda s: None, log=lambda *a: None)


def mk(fake, sleeps):
    return C.Fetcher(gap=0, opener=fake, sleep=lambda s: sleeps.append(s), log=lambda *a: None)


TMP = Path(tempfile.mkdtemp(prefix="chips-test-"))
try:
    print("=== ① parse ===")
    h, rows = C.parse(INST_H + "\n2030/01/02,臺股期貨,投信," + ",".join(["1"] * 12) + ",\n", "日期")
    chk("尾端逗號截掉", (len(h), len(rows[0])), (15, 15))
    chk("查無資料 ⇒ None", C.parse("<html>", "日期"), (None, []))
    h, rows = C.parse(OI_H + "\n2030/01/02,MTX,202601,1,1,1,1,1,1%,1,1,7,1,1,1,1,,一般,,\n"
                      "2030/01/02,MTX,202602,1,1,1,1,1,1%,1,1,3,1,1,1,1,,一般,,\n"
                      "2030/01/02,MTX,202601,1,1,1,1,1,1%,1,1,-,1,1,1,1,,盤後,,\n", "交易日期")
    chk("未平倉只加一般時段", C.oi_sum(h, rows), {"2030/01/02": 10.0})

    print("=== ② 從零開始補（新資料夾）＋ 第二輪不重複 ===")
    D = TMP / "a"
    fake, sl = FakeTaifex(closed={date(2030, 1, 15)}), []
    rep, F = C.run(D, today=date(2030, 2, 20), start=date(2030, 1, 1), F=mk(fake, sl), log=lambda *a: None, F_etf=ETF_F())
    _, fut = C.read_csv(D / "futco.csv")
    chk("futco 只有大台", {r[1] for r in fut}, {"臺股期貨"})
    chk("微台另外一支檔", {r[1] for r in C.read_csv(D / "tmf_inst.csv")[1]}, {"微型臺指期貨"})
    chk("全部契約那支有電子期貨", "電子期貨" in {r[1] for r in C.read_csv(D / "fut_inst_all.csv")[1]}, True)
    days = sorted(C.dates_of(fut))
    chk("從 01-01 到 02-20 每個交易日都有（休市日沒有）", (days[0], days[-1], date(2030, 1, 15) in days),
        (date(2030, 1, 1), date(2030, 2, 20), False))
    chk("一次最多 28 天", all((C.to_date(q["queryEndDate"]) - C.to_date(q["queryStartDate"])).days < 28
                          for _, q in fake.calls), True)
    oi = C.read_csv(D / "mtx_oi.csv")
    chk("mtx_oi 表頭與加總", (oi[0], oi[1][0][1]), (["交易日期", "oi"], "120.0"))
    lt = C.read_csv(D / "large_trader.csv")[1]
    chk("大額交易人只留大台／小台／微台、代碼去空白", sorted({r[1] for r in lt}), ["MTX", "TMF", "TX"])
    pc = C.read_csv(D / "pcratio.csv")[1]
    chk("P/C 依日期排好（舊到新）", [C.to_date(r[0]) for r in pc] == sorted(C.to_date(r[0]) for r in pc), True)
    n1 = len(fake.calls)
    rep2, _ = C.run(D, today=date(2030, 2, 20), F=mk(fake, sl), log=lambda *a: None, F_etf=ETF_F())
    chk("第二輪期交所那幾支一列都不加（ETF 一次 80 天、會接著補）", sum(sum(v.values()) for k, v in rep2.items() if k != "etf"), 0)
    chk("第二輪每組只問「今天之後」⇒ 0 次", len(fake.calls) - n1, 0)

    print("=== ③ 隔天：只補新的一天 ===")
    rep3, _ = C.run(D, today=date(2030, 2, 21), F=mk(fake, sl), log=lambda *a: None, F_etf=ETF_F())
    chk("futco 多 3 列（三種身份）", rep3["inst"]["futco.csv"], 3)
    chk("夜盤大台多 1 列", rep3["inst_ah"]["futco_ah.csv"], 1)

    print("=== ④ 補漏日：P/C 中間少一天會補回來 ===")
    h, rows = C.read_csv(D / "pcratio.csv")
    hole = date(2030, 2, 5)
    rows = [r for r in rows if C.to_date(r[0]) != hole]
    with (D / "pcratio.csv").open("w", encoding="utf-8", newline="") as f:
        import csv
        w = csv.writer(f, lineterminator="\n")
        w.writerow(h)
        w.writerows(rows)
    calls0 = len(fake.calls)
    rep4, _ = C.run(D, today=date(2030, 2, 21), only=["pcratio.csv"], F=mk(fake, sl), log=lambda *a: None, F_etf=ETF_F())
    chk("補回那一天", (rep4["pc"]["pcratio.csv"], hole in C.dates_of(C.read_csv(D / "pcratio.csv")[1])), (1, True))
    chk("只多問一次", len(fake.calls) - calls0, 1)

    print("=== ⑤ 沿用舊格式：research 那份檔接得上 ===")
    E = TMP / "b"
    E.mkdir()
    (E / "mtx_oi.csv").write_text("交易日期,oi\n2030/01/02,63172.0\n2030/01/03,55439.0\n", encoding="utf-8")
    C.run(E, today=date(2030, 1, 7), only=["mtx_oi.csv"], F=mk(FakeTaifex(), sl), log=lambda *a: None, F_etf=ETF_F())
    got = C.read_csv(E / "mtx_oi.csv")[1]
    chk("舊的兩列留著、從 01-04 接上", [r[0] for r in got], ["2030/01/02", "2030/01/03", "2030/01/06", "2030/01/07"]
        if False else ["2030/01/02", "2030/01/03", "2030/01/04", "2030/01/07"])
    (E / "pcratio.csv").write_text("日期,別的欄位\n2030/01/02,1\n", encoding="utf-8")
    try:
        C.run(E, today=date(2030, 1, 7), only=["pcratio.csv"], F=mk(FakeTaifex(), sl), log=lambda *a: None, F_etf=ETF_F())
        chk("表頭不一樣 ⇒ 拒絕", False, True)
    except ValueError:
        chk("表頭不一樣 ⇒ 拒絕（不硬塞）", True, True)

    print("=== ⑥ 429 退讓重試、被擋 ⇒ 非零離開 ===")
    fk, sl2 = FakeTaifex(fail_first=2), []
    F = C.Fetcher(gap=0, opener=fk, sleep=lambda s: sl2.append(s), log=lambda *a: None)
    t = F.post("pcRatioDown", {"queryStartDate": "2030/01/02", "queryEndDate": "2030/01/02"})
    chk("重試兩次後拿到", (t is not None, F.n, F.ok), (True, 3, 1))
    chk("退讓 20、40 秒", [s for s in sl2 if s >= 20], [20, 40])
    G = TMP / "c"
    import io, contextlib
    _orig = C.Fetcher.__init__

    def _blocked_init(self, *a, **k):
        _orig(self, gap=0, opener=FakeTaifex(blocked=True), sleep=lambda s: None, log=lambda *a: None)
    C.Fetcher.__init__ = _blocked_init
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            rc = C.main(["--dir", str(G), "--start", (date.today() - timedelta(days=3)).isoformat(), "--only", "pcratio.csv"])
    finally:
        C.Fetcher.__init__ = _orig
    chk("全部被擋 ⇒ 離開碼 3", rc, 3)
    chk("被擋時一支檔都不建", sorted(p.name for p in G.glob("*.csv")), [])
    print("=== ⑦ 期貨型 ETF ===")
    H = TMP / "etf"
    fe = FakeETF()
    n, _ = C.run_etf(H, date(2030, 1, 10), start=date(2030, 1, 6), F=ETF_F(fe), log=lambda *a: None)
    h, rows = C.read_csv(H / "etf_fut.csv")
    chk("表頭", h, C.ETF_HEAD)
    tx = [r for r in rows if r[1] == "00631L" and r[2] == "TX"]
    chk("元大用持倉日（公告日的前一個交易日）", [r[0] for r in tx], ["2030/01/04", "2030/01/07", "2030/01/08", "2030/01/09"])
    chk("反1 是負的口數", {r[4] for r in rows if r[1] == "00632R" and r[2] == "TX"}, {"-2450"})
    chk("國泰去千分位、月份去斜線", next(r for r in rows if r[1] == "00663L")[3:6], ["203002", "3251", "199.04"])
    chk("NYF 也收", any(r[2] == "NYF" for r in rows), True)
    n2, _ = C.run_etf(H, date(2030, 1, 10), F=ETF_F(fe), log=lambda *a: None)
    chk("第二輪不重複", n2, 0)
    K = TMP / "etf2"
    fe2 = FakeETF(fail_on="SearchDate=2030/01/08")
    C.run_etf(K, date(2030, 1, 10), start=date(2030, 1, 6), F=ETF_F(fe2), log=lambda *a: None)
    ds = C.dates_of(C.read_csv(K / "etf_fut.csv")[1])
    chk("抓不到那一天 ⇒ 停在那裡、那一天不寫", (max(ds), date(2030, 1, 8) in {d for d in ds}), (date(2030, 1, 7), False))
    fe2.fail_on = None
    C.run_etf(K, date(2030, 1, 10), F=ETF_F(fe2), log=lambda *a: None)
    chk("下一輪從那一天接上", max(C.dates_of(C.read_csv(K / "etf_fut.csv")[1])), date(2030, 1, 10))
    L = TMP / "etf3"
    C.run_etf(L, date(2030, 12, 31), start=date(2030, 1, 1), F=ETF_F(), log=lambda *a: None, max_days=10)
    chk("一次最多查 max_days 個平日", len({r[0] for r in C.read_csv(L / "etf_fut.csv")[1] if r[1] == "00663L"}), 10)
except Exception:
    import traceback
    traceback.print_exc()
    FAIL += 1
    print("  FAIL 測試本身崩潰")
finally:
    shutil.rmtree(TMP, ignore_errors=True)
print("\n總結：%d 項，失敗 %d 項" % (N, FAIL))
sys.exit(1 if FAIL else 0)
