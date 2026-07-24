# 微台指交易日誌 📈

手機優先的 PWA，記錄每天一單的微台指模擬交易：進出場點數、勝敗、勝率統計與心得備註。

## 功能

- **每日記錄**：方向（多/空）、進場點數、出場點數、心得備註；自動計算損益點數與勝／敗
- **勝率統計**：7 日 / 10 日 / 30 日 / 3 月 / 半年 / 1 年 / 自訂區間，含勝敗場次、淨點數（約當台幣）與累積點數走勢圖
- **編輯／刪除**：點任一筆紀錄即可修改或刪除（一天一單，同日期會覆蓋）
- **備份**：可匯出 / 匯入 JSON，換裝置或備份用
- **PWA**：可加到主畫面、離線可用

> 顏色依台股慣例：🔴 紅＝賺／勝、🟢 綠＝賠／敗。

## 資料存放

資料只存在瀏覽器的 `localStorage`（key：`trade-log-v1`），不上傳任何伺服器。換裝置請用「匯出備份 → 匯入備份」。

## 技術

純靜態前端，零依賴：`index.html` + `css/style.css` + `js/app.js` + `sw.js` + `manifest.webmanifest`。

## 部署（GitHub Pages）

1. Repo → **Settings → Pages**
2. Source 選 **Deploy from a branch**，Branch 選 **main / (root)**，儲存
3. 幾分鐘後開 `https://xd1104.github.io/trade-log/`，手機瀏覽器可「加入主畫面」安裝

## 開發備忘

- 所有資源用**相對路徑**（Pages 在 `/trade-log/` 子路徑），`manifest` 的 `start_url`/`scope` 與 SW scope 皆為 `./`
- 改前端資源後，把 `sw.js` 的 `CACHE` 版本號 +1（`tradelog-shell-vN`）強制更新快取
- iOS：input 字級 ≥ 16px 防自動放大；換 icon 後已安裝的 PWA 要移除主畫面重加才會更新
- icon 由 `tools/gen-icons.js`（純 Node、無依賴）產生「走勢箭頭」設計，輸出到 `icons/`；改設計後重跑並把 SW 版本號 +1
