# 期交所籌碼（每天自動補）

由 trade-log 的 GitHub Actions（`.github/workflows/chips.yml`、程式 `tools/chips/collect_chips.py`）
每個交易日台北 17:40、隔天 08:40 各跑一次，補到最新。全部是期交所／投信公開資料。

| 檔案 | 內容 |
|---|---|
| futco.csv | 日盤三大法人 臺股期貨（大台） |
| mxf_inst.csv | 日盤三大法人 小型臺指 |
| tmf_inst.csv | 日盤三大法人 微型臺指 |
| fut_inst_all.csv | 日盤三大法人 全部期貨契約 |
| futco_ah.csv | 夜盤三大法人 臺股期貨 |
| fut_inst_ah_all.csv | 夜盤三大法人 全部期貨契約 |
| pcratio.csv | 選擇權 P/C 比 |
| mtx_oi.csv／tx_oi.csv／tmf_oi.csv | 小台／大台／微台 全市場未平倉（一般時段、各月加總） |
| large_trader.csv | 大額交易人（TX／MTX／TMF） |
| etf_fut.csv | 期貨型 ETF 每日期貨部位：元大 00631L、00632R（持倉日）、國泰 00663L（查詢日） |

期交所網站只留約 3 年，這裡會一直累積下去。
