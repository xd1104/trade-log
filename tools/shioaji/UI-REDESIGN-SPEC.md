# 早盤儀表板・視覺升級規格（lab-ux → lab-dev）

對象檔案：`tools/shioaji/live_panel.py`（HTML/CSS/JS inline 在 `PAGE`，約 1516~3676 行）
可試玩的設計稿：`tools/shioaji/ui-demo.html`（雙擊直接用瀏覽器打開，零外部依賴）

> **先看 demo 再動手。** 這份文件只寫「值」與「規則」，長相以 demo 為準。
> demo 右下角有控制台：切版面方案 A/B/C、切新舊進出場標記、切休市狀態、重播載入動畫、關掉動畫。

---

## 0. 診斷：為什麼會覺得「生硬、有點醜」

不是配色問題（紅綠金這套是對的），是**語意層級沒有做出來**：

| 症狀 | 真正的病 |
|---|---|
| 整頁很平 | 每一張卡都是 `--surface` ＋ 同一條 `--line` ＋ 同一個圓角 ⇒ K 線圖（主角）跟一排篩選鈕（配角）視覺重量一樣 |
| 大字報價區很單薄 | 只有兩個層級（大數字、小字），漲跌幅是「跟旁邊一樣的純文字」，沒有被獨立出來 |
| 底下那排客觀數字很擠 | 9 個數字同一個字級、同一個顏色、只用空白隔開 ⇒ 讀起來是一長串連續的字，沒有分組 |
| 勝率 43% 那塊怪 | 全頁最大最亮的數字是**金色的勝率**，而金色在翻頁列的定義是「即時／現在」⇒ 一個顏色兩種意思；而且把「勝率」放在視覺頂端，暗示它是這個工具的目標（工具的定位是練習與檢討，不是追勝率） |
| 進出場標記不明顯又擋路 | 標記是「圖上的形狀」，但標籤也想擠在圖上 ⇒ 兩件事搶同一塊空間 |
| Bar Replay 控制列生硬 | 五顆長得一模一樣的方框按鈕，看不出哪個是主要動作，也看不出「現在走到哪、還有多長」 |

**修法一句話**：把「圖表」升成唯一的主角層，其餘東西各自退到該在的層級；所有數字加上分組與標籤層；所有標籤搬離 K 棒。

---

## 1. 設計代幣（改 `:root`，live_panel.py 第 1521 行）

```css
:root{
  --bg:#0E1116;             /* 舊 #0F1218，再暗一階，讓卡片浮得起來 */
  --surface:#151A22;        /* 舊 #171C25 — L2 一般卡 */
  --surface-2:#1C222C;      /* 舊 #1F2530 — 卡內凹槽（輸入框、分段控制、列表項）*/
  --raise:#1A212B;          /* 新增 — L1 主卡（K 線圖）*/
  --line:#242C38;           /* 舊 #262D39 */
  --line-soft:#1E2530;      /* 新增 — 弱邊框／分隔線 */
  --text:#E9ECF1; --dim:#8D95A3; --faint:#5C6472;
  --ghost:#39414F;          /* 新增 — 取代散在各處的 #333A47 */
  --gold:#E3A951; --gold-soft:rgba(227,169,81,.14); --gold-line:rgba(227,169,81,.42);
  --up:#EE5A54;   --up-soft:rgba(238,90,84,.15);    --up-line:rgba(238,90,84,.38);
  --down:#34B37E; --down-soft:rgba(52,179,126,.15); --down-line:rgba(52,179,126,.38);
  --r-lg:16px; --r-md:12px; --r-sm:9px; --r-xs:6px;        /* 取代 --radius/--radius-sm */
  --shadow-1:0 18px 40px -22px rgba(0,0,0,.85);
  --shadow-2:0 24px 60px -20px rgba(0,0,0,.7);
  --ease:cubic-bezier(.22,.68,.36,1);
}
```

- **紅漲綠跌不准動**；`--up/--down` 色碼完全沿用舊值，只補了 soft/line 兩個衍生色。
- `--radius`/`--radius-sm` 可以保留當別名（`--radius:var(--r-lg)`），避免一次改上百處。
- **金色只准有一個意思：即時／現在／目前選取。** 勝率、統計數字一律不准用金色（見 §5）。

**字級尺標**（全部用這 9 級，不要再出現 13.5/12.5 以外的碎值）：
`10.5 / 11.5 / 12.5 / 13.5 / 15 / 19 / 26 / 40 / 52`
數字一律 `--font-mono` ＋ `font-variant-numeric:tabular-nums`；≥26px 的數字加 `letter-spacing:-1px`（52px 用 `-1.6px`）。

**動態曲線**：
| 用途 | 值 |
|---|---|
| 進場、K 線展開 | `.40~.52s var(--ease)` |
| hover／色彩變化 | `.12~.15s var(--ease)` |
| 換日淡出／淡入 | `.22s ease` |
| 骨架呼吸 | `1.7s ease-in-out infinite`（舊 1.6s，慢一點比較不吵） |
| 骨架掃光 | `1.6s`，亮度 `rgba(255,255,255,.045)`（舊 .05） |

---

## 2. 卡片層級（新增，取代目前只有一種的 `.card`）

```css
.card{background:var(--surface); border:1px solid var(--line-soft);
      border-radius:var(--r-lg); padding:16px 18px}          /* L2：一般卡 */
.card.l1{background:linear-gradient(180deg,var(--raise),#161C24);
      border-color:var(--line); box-shadow:var(--shadow-1);
      padding:16px 18px 13px; position:relative}             /* L1：只有 K 線圖用 */
.card.l1::before{content:''; position:absolute; left:16px; right:16px; top:0; height:1px;
      background:linear-gradient(90deg,transparent,rgba(255,255,255,.07),transparent)}
.card.l3{background:transparent; border-color:transparent; padding:0}  /* L3：純容器 */
```

- **L1 只給兩張**：即時分頁的 K 線卡、回顧分頁的 K 線卡。全頁只有它們有陰影。
  → `paintChart()` 產生的 `<div class="card chart kk-in" id="cchart">` 要加上 `l1`；
    回顧分頁 HTML（第 2007 行附近）那張 `<div class="card chart">` 也加 `l1`。
- **L3 給**：篩選 chips 那一列、`.ftitle`、只放一行提示的區塊 —— 不要再包一張有邊框的卡。
- ⚠ `.card.l1` **不要**加 `overflow:hidden`：迷你月曆 `.calpop` 是絕對定位浮出卡片外的。

---

## 3. 報價區（`.chead` / `.cpx` / `.cchg`，第 1582~1584 行；HTML 在 `chartSVG()` 的 `head:`）

**現況**：`<span class="cpx">44496</span> <span class="cchg">-5 (-0.01%)</span>` 兩個平的文字。

**新規格**（三層）：
```html
<div class="qblock">
  <div class="qmain">
    <span class="cpx up|down|flat">44496</span>
    <span class="cchg up|down|flat">-5<span class="pct">-0.01%</span></span>
  </div>
  <div class="qsub">
    <span class="live"><i></i>即時</span><span class="sep">·</span>
    <span>昨收 44700</span><span class="sep">·</span><span>微台 TMFI6</span>
  </div>
</div>
```
```css
.qblock{display:flex; flex-direction:column; gap:6px; min-width:0}
.qmain{display:flex; align-items:baseline; gap:11px; flex-wrap:wrap}
.cpx{font-size:52px; line-height:.94; letter-spacing:-1.6px; font-weight:700}   /* 舊 44px */
.cchg{display:inline-flex; align-items:baseline; gap:7px; font-size:15px; font-weight:700;
      padding:4px 10px 5px; border-radius:9px; line-height:1}
.cchg.up{background:var(--up-soft); color:var(--up)}
.cchg.down{background:var(--down-soft); color:var(--down)}
.cchg.flat{background:var(--surface-2); color:var(--dim)}
.cchg .pct{font-size:12.5px; font-weight:600; opacity:.85}
.qsub{display:flex; align-items:center; gap:9px; font-size:11.5px; color:var(--faint);
      font-family:var(--font-mono); flex-wrap:wrap}
.qsub .sep{color:var(--ghost)}
.qsub .live{color:var(--gold); display:inline-flex; align-items:center; gap:5px}
.qsub .live i{width:6px;height:6px;border-radius:50%;background:var(--gold);
      box-shadow:0 0 0 3px var(--gold-soft)}
```
- `qsub` 那行要把**現行 mini 列開頭那句「報價 休市中（上面是收盤價，非即時）」搬進來**：
  - `quote==='live'` → `即時 · 昨收 X · 微台 TMFI6 · HH:MM:SS 更新`
  - `closed` → `休市中 · 昨收 X · 上面是收盤價，非即時`（金點換成 `--faint` 灰點）
  - `nodata` → `收不到報價 · …`（維持現行文案：可能是國定假日、也可能是連線問題）
  - 看歷史日 → `歷史日 · 昨收 X · 13:45 收盤`
- ⚠ **`#chead` 每次報價變動就整個重建**：這一塊裡面**不准有任何 animation/transition**（`.cchg` 的底色是靜態的，沒問題）。
- ⚠ **翻頁列 `.pager` 的 DOM 與尺寸一格都不要動**（高度預算 45px、星期固定寬、靠右對齊）。
  只換色票：`#333A47` → `var(--ghost)`、`var(--line)` → `var(--line-soft)`、transition 加上 `var(--ease)`。
  改完**一定要重量一次** `document.querySelector('.pager').getBoundingClientRect().height`，不准用字級推算。

---

## 4. 資料軌（取代 `.mini` 第 1694 行 與 `.fstrip`/`.fcell` 第 1880 行）

現況兩處是兩套（即時是一行純文字、回顧是 7 格方塊），改成**同一個元件**：

```css
.rail{display:flex; align-items:stretch; flex-wrap:wrap; margin-top:12px; padding-top:11px;
      border-top:1px solid var(--line-soft)}
.rail .grp{display:flex; gap:18px; padding:0 18px; border-right:1px solid var(--line-soft)}
.rail .grp:first-child{padding-left:0}
.rail .grp:last-child{border-right:0; padding-right:0}
.rail .it{min-width:44px}
.rail .k{font-size:10.5px; color:var(--faint); letter-spacing:.4px; white-space:nowrap; line-height:1.3}
.rail .v{font-size:15px; font-weight:650; font-family:var(--font-mono);
      font-variant-numeric:tabular-nums; line-height:1.25; margin-top:2px; white-space:nowrap}
.rail .v small{font-size:10.5px; font-weight:500; color:var(--faint); margin-left:2px}
.rail .track{height:3px; border-radius:2px; background:var(--surface-2); margin-top:5px; position:relative}
.rail .track i{position:absolute; top:0; bottom:0; left:0; border-radius:2px; background:var(--dim)}
.rail .track i.hot{background:var(--gold)}
.rail .muted .v{color:var(--dim)}
```

**分組（順序固定，欄位一個都不能少）**：

| 組 | 即時分頁 | 回顧分頁（進場當下） |
|---|---|---|
| 動能 | 5 分・15 分 | 最近 5 分・最近 15 分 |
| 今天 | 跳空・今日震幅・位階＊・量能＊ | 對開盤・跳空・今日震幅・位階＊・量能＊ |
| 盤口 | 買 / 賣 | —— |
| 現貨 | 加權・基差 | —— |

＊ 位階 = `track` 寬度 `pos*100%`；量能 = `track` 寬度 `min(100, vol_ratio/3*100)%`，
`pos>0.8 || pos<0.2`、`vol_ratio>1.5` 時 `i` 加 `.hot`（金色）。
**這兩條線只是把已發生的數字畫成長度，不得加任何「強／弱／偏多」之類的評語。**

- 沒有即時報價時：整條軌換成「開/高/低/收・震幅・收在區間・跳空・總量」＋一格 `報價 非即時`（`.muted`）。
  → 這修掉現行「休市時整排數字直接消失」的觀感問題（資料本來就有，只是不是即時的）。
- 回顧分頁的 `.fstrip` 整個換成 `.rail`，`#rfstrip` 改成 `<div class="rail" id="rfstrip">`，`fstrip(D)` 只改產生的 HTML。

---

## 5. 練習成績（`.rate-row`/`.rate-big` 第 1723~1735 行；`statsBox()` 第 2749 行）

**現況**：金色 46px 勝率 + 右邊三行小字。
**新規格**：

```html
<div class="score">
  <div class="rate"><span class="n">43</span><span class="p">%</span><div class="lab">勝率</div></div>
  <div class="sum"><div><span class="n down">-135</span><span class="u">點</span></div>
                   <div class="cash">-NT$1,350</div></div>
</div>
<div class="wlbar"><i class="w" style="flex:3"></i><i class="l" style="flex:4"></i></div>
<div class="wlfoot"><span class="w"><b>3</b> 勝</span><span>7 筆</span><span class="l"><b>4</b> 敗</span></div>
```
```css
.score{display:flex; align-items:flex-start; justify-content:space-between; gap:14px}
.score .rate .n{font-size:40px; font-weight:680; letter-spacing:-1.2px; color:var(--text)}  /* 不再是金色 */
.score .rate .p{font-size:19px; color:var(--dim)}
.score .rate .lab{font-size:11px; color:var(--faint); letter-spacing:2px; margin-top:7px}
.score .sum{text-align:right; font-family:var(--font-mono); font-variant-numeric:tabular-nums}
.score .sum .n{font-size:26px; font-weight:700; letter-spacing:-.5px; line-height:1.1}  /* 這裡才吃紅綠 */
.score .sum .u{font-size:11px; color:var(--dim); margin-left:3px}
.score .sum .cash{font-size:11.5px; color:var(--faint); margin-top:5px}
.wlbar{display:flex; height:6px; border-radius:3px; overflow:hidden; margin-top:13px; gap:2px}
.wlbar i{height:100%; border-radius:3px} .wlbar i.w{background:var(--up)} .wlbar i.l{background:var(--down)}
.wlfoot{display:flex; justify-content:space-between; font-size:11.5px; color:var(--faint);
        font-family:var(--font-mono); margin-top:6px}
.wlfoot .w b{color:var(--up)} .wlfoot .l b{color:var(--down)}
```
**設計理由（別改回去）**：勝率是「已經發生的統計」，把它從金色降成中性色、把紅綠讓給真正的結果（合計點數），
既解掉「金色兩種意思」，也不會讓畫面把勝率捧成這個工具的目標。勝敗條讓比例一眼看得出來，不必讀數字。

---

## 6. 練習紀錄列（`.trade` 第 1741 行）

```css
.trade{background:var(--surface-2); border:1px solid var(--line-soft); border-radius:var(--r-md);
       padding:10px 12px 10px 13px; position:relative; overflow:hidden;
       transition:border-color .15s var(--ease), background .15s var(--ease)}
.trade::before{content:''; position:absolute; left:0; top:0; bottom:0; width:2px; background:var(--ghost)}
.trade.win::before{background:var(--up)} .trade.loss::before{background:var(--down)}
.trade .noteline{background:rgba(0,0,0,.18); font-size:12px; padding:8px 10px}
```
`row()`（第 2731 行）與 `rowHTML()`（第 3277 行）產生的最外層 class 補上 `win`/`loss`（依 `_net`）。
其餘（日期寬 42px、方向膠囊、價格、結果右對齊）不動。

---

## 7. 進出場標記（`chartSVG()` 第 2310~2367 行；`rvDraw()` 第 3114~3180 行）

**現況的問題**：一筆交易會畫出「兩條橫貫全圖的虛線 ＋ 三角形 ＋ 兩塊描邊文字」，文字就落在 K 棒上。
而他的單 **5~15 分鐘就結束**（實測 08-21、08-25：±100 點只要 1~3 根 5 分 K），
所以進出場在 x 軸上非常靠近，兩塊文字必然互相推擠、且一定壓到那幾根關鍵 K 棒。

**新規格：圖區只留形狀，文字全部搬到本來就空著的兩條軌（右側價格軸、底部時間軸）。**

一筆交易畫這幾樣（座標同現行 `chartSVG` 的 `x()/y()`，`W=1040, R=64, TOP=12, BOT=26`）：

1. **持有區間底色**：`rect`，x 從 `x(進)-cw/2` 到 `x(出)+cw/2`，y 介於進出場價之間，
   `fill=結果色 opacity=".10"`。
2. **進場價／出場價虛線**：`stroke-dasharray="4 3" stroke-width="1.1" opacity=".7"`，
   **只畫在區間內**（不再橫貫全圖）。進場線用方向色、出場線用結果色。
3. **連線**：進場點→出場點 `stroke-width="1.8" opacity=".9" stroke-linecap="round"`，結果色。
4. **進場標記**：三角形（做多朝上、做空朝下），寬 15、高 12.5，`fill=方向色`，
   `stroke="#0E1116" stroke-width="1.8" stroke-linejoin="round"`，尖端離價格 3.5px；中心再補一顆 r=2.6 的圓點。
5. **出場標記**：11.2×11.2 的圓角方塊 `rx="2.4"` 旋轉 45°（菱形），`fill=結果色`，同樣深色描邊 1.8。
6. **引導線**：從標記垂直落到時間軸帶，`stroke-dasharray="2 4" opacity=".32"`（讓眼睛把形狀跟底下的標籤接起來）。
7. **價格軸掛牌**（保留現行 `axisChip`，只把 `rx` 3→4）：進場價、出場價各一塊，落在右側 64px 的價格軸上。
8. **時間軸膠囊**（新）：畫在 `y=H-BOT+3`、高 17、`rx=5`、`fill="#0E1116" fill-opacity=".92"`、
   `stroke=對應色 stroke-opacity=".55"`，字 10.5px 700。
   - 進出場 x 距離 **< 110px** ⇒ **合併成一枚**，內容 `▼ 09:10→09:20　+95`（顏色用結果色）。
   - ≥ 110px ⇒ 兩枚：`▲ 進 09:10`（方向色）、`出 09:20　+95`（結果色）。
   - 膠囊之間水平互相避讓（碰到就往右推 `w+5`），並被夾在 `0 ~ W-R` 內。
   - **時間刻度**（`B.forEach` 那段 `<text>`）遇到膠囊 ±58px 內就跳過，不要疊字。

回顧分頁 `rvDraw()` 用同一套（`H=430`），差別只有：已揭曉的重播要同時畫「你這次的判斷」與「當天實際的決定」，
後者色彩 `opacity` 降到 .55 並在膠囊前綴「當天」。

**不准**：任何形式的預測箭頭、目標價、勝率提示、訊號強弱色階。

---

## 8. Bar Replay 控制列（`.rpbar` 第 1862 行；`ctrlHTML()` 第 3484 行）

現況是單行五顆同款按鈕。新規格＝**兩行**：上行運鏡控制、下行時間軸。

```html
<div class="rpbar">
  <div class="rprow">
    <button class="rpbtn" data-ract="rphome">⏮</button>
    <button class="rpbtn" data-ract="rpback">◀</button>
    <button class="rpbtn play" data-ract="rpplay">▶ 播放</button>
    <button class="rpbtn" data-ract="rpstep">▶▶</button>
    <div class="rpsp">×0.5 ×1 ×2 ×4</div>
    <div class="rppos"><b>09:20</b>　36 / 299 根</div>
  </div>
  <div class="rpscrub" data-rseek="1">
    <div class="trk"></div><div class="win"></div><div class="fill"></div>
    <div class="jm"></div><div class="knob"></div>
    <span class="tk">08:45</span>…
  </div>
</div>
```
```css
.rpbar{margin-top:10px; padding:10px 12px 9px; background:var(--surface-2);
       border:1px solid var(--line-soft); border-radius:14px}
.rprow{display:flex; align-items:center; gap:8px}
.rpbtn{border:1px solid var(--line); background:var(--surface); color:var(--dim);
       font-size:13px; font-weight:600; padding:8px 12px; border-radius:var(--r-sm); min-width:40px;
       transition:color .15s var(--ease), border-color .15s var(--ease)}
.rpbtn:hover:not(:disabled){color:var(--text); border-color:var(--faint)}
.rpbtn.play{background:var(--gold-soft); color:var(--gold); border-color:transparent;
       min-width:104px; font-size:13.5px; padding:9px 14px}
.rppos{flex:1; text-align:right; color:var(--faint); font-size:12.5px}
.rppos b{color:var(--text); font-size:14px; font-weight:650}
.rpscrub{position:relative; height:20px; margin-top:8px; cursor:pointer}
.rpscrub .trk{position:absolute; left:0; right:0; top:5px; height:4px; border-radius:2px; background:var(--surface)}
.rpscrub .win{position:absolute; top:5px; height:4px; background:var(--gold-soft)}   /* 08:45~09:30 */
.rpscrub .fill{position:absolute; left:0; top:5px; height:4px; border-radius:2px;
       background:linear-gradient(90deg,rgba(227,169,81,.5),var(--gold))}
.rpscrub .knob{position:absolute; top:2px; width:10px; height:10px; border-radius:50%;
       background:var(--gold); box-shadow:0 0 0 3px rgba(227,169,81,.18); margin-left:-5px}
.rpscrub .jm{position:absolute; top:0; width:2px; height:14px; border-radius:1px; margin-left:-1px} /* 判斷點 */
.rpscrub .tk{position:absolute; top:12px; font-size:9.5px; color:var(--ghost);
       font-family:var(--font-mono); transform:translateX(-50%)}
```
- 次要按鈕顏色從 `--text` 降成 `--dim`（hover 才變亮），播放鍵是唯一的金色 ⇒ 一眼看得出主要動作。
- 時間軸：`.win` 標出 08:45~09:30（他真正下單的時段）、`.jm` 標出這次按下判斷的那一根（多紅／空綠）。
- **點時間軸可以跳到那一根**（`data-rseek`）：跳之前先 `rpStop()`；`state==='revealed'` 時不接受跳轉。
- 鍵盤（空白鍵播放/暫停、←→ 單步、↑↓ 判斷）全部保留。

---

## 9. 載入（`.skel` 第 1790~1814 行、`kk-*` 動畫第 1782 行起）

現行的骨架／淡入／K 線展開／換日進度條**架構是對的，保留**，只做三件事：

1. `.skel .chead{min-height:XX}` 要跟著新的報價區重新對高（52px 大字＋qsub 兩行）。
   **不准用字級推算**：改完在瀏覽器實測 `getBoundingClientRect().height`，把真圖與骨架兩個數字都寫進註解。
   demo 裡先填的是 74px，那是估計值，dev 必須實測後校正。
2. `.skel .mini` 換成 `.skel .rail{min-height:XX; align-items:center}`，`.skel .rail .sk{width:60px;height:26px}`。
   同樣要實測（資料軌是兩行＋量尺，比舊的 mini 高，CLS 一定要重驗）。
3. 骨架呼吸與掃光參數見 §1；`.skel .bars i` 拿掉固定的 `opacity:.5`（改由 `kk-breathe` 控制）。

**保留不動的鐵律**：假 K 棒必須絕對定位；`kk-wipe` 用「加 class → 計時器移除」，不可留在元素上；
`#chead` 這種每 0.5 秒重建的區塊一律不掛 animation；`@media (prefers-reduced-motion: reduce)` 全部關掉。

---

## 10. 高頻重繪的分工（demo 已示範，實作照抄）

- hover／滾輪縮放／拖曳 → **只換 `#csvg` 的內容與 legend**，不要碰 `#chead`、`#cmini/#crail`、右欄。
  （現行 `paintChart()` 已有快取比對，繼續沿用；新增的 `.rail` 也要走同一套「比對上次自己設進去的字串」，
  **不可以讀回 `innerHTML` 比對**。）
- 重播每走一根 → 只換 `#rsvg` 與控制列；右欄若正在編輯心得（或 `#jnote` 有焦點）不得重建。

---

## 11. 不能動的清單（動到就是退件）

1. **不得出現任何預測、勝率預估、期望值、買賣建議、訊號強度**（含顏色暗示強弱的量尺）。
2. **紅＝漲/賺、綠＝跌/賠**，不准反。
3. 現有功能一個都不能少：換日翻頁列＋迷你月曆、練習下單、練習成績、每筆心得展開輸入、
   Bar Replay 控制列、下載練習紀錄。
4. **沒有即時報價就不准開模擬單**（按鈕真的 `disabled` ＋ 底下寫原因，後端也擋）——這是紀錄正確性，不是 UX 取捨。
5. 翻頁列的結構、尺寸與「星期固定寬」不要動；`disabled` 的裸屬性比對規則不要動。
6. K 線圖維持純 SVG，viewBox 1040×470（回顧 1040×430）；滾輪縮放、拖曳平移、雙擊還原全部保留。
7. `practice_trades/` 與 `replay_log/` 不可混用；改版期間**不要拿真的紀錄下去測**（會觸發 push 到公開 repo）。

---

## 12. 交件自檢表（dev 自己跑，QA 會重驗）

- [ ] 三個高度實測值寫進註解：`.chead` 真圖 vs 骨架、`.rail` 真圖 vs 骨架、`.pager` 總高（≤45px）。
- [ ] 開站 → 骨架換真圖時，`document.querySelector('#mkt').getBoundingClientRect().height` 前後差 < 1px。
- [ ] 連點 ◀ 五下，`◀` 按鈕中心 x 位移 < 2px（載入中不得讓中間欄位變寬）。
- [ ] hover 掃過整張圖 3 秒，`#chead` 與 `#cpick` 的重建次數 = 0。
- [ ] 開啟系統「減少動態」後，整頁沒有任何 animation/transition 在跑。
- [ ] 休市狀態下：下單鈕 `disabled`、資料軌顯示歷史數字＋「非即時」、連線燈是中性灰。
- [ ] 全頁搜尋沒有出現「預測／勝算／期望值／建議／訊號」等字樣。

---

## 13. 要老闆拍板的一件事：版面方案 A / B / C

demo 右下角控制台可以直接切，三個方案**共用同一套色票與元件**，只差「重心放哪」：

| 方案 | 長相 | 適合 |
|---|---|---|
| **A 沉穩**（推薦） | K 線卡浮起來（唯一有陰影），報價在卡內、右欄卡片安靜 | 每天開一整個早上、久看不累；改動風險最小 |
| **B 頭條** | 報價區升成整條橫幅（底色＋58px 大字），視覺重心在最上面 | 想要「一眼看到價格」，但長時間看會比較躁 |
| **C 極簡** | 全面去框，只用留白與底色深淺分層 | 最乾淨，但層級靠留白撐，資訊密度高時比較弱 |

**lab-ux 推薦 A**：這個工具是「盯著 K 線做判斷」，主角應該是圖不是價格數字；
B 的橫幅會把視線一直往上拉，C 在資料軌那種高密度區塊會失去分組感。

---

## 14. demo 的已知取捨（不是 bug）

- demo 只放了 08-21（日盤）與 08-25（含前一晚夜盤）兩天的真實 K 棒，月曆其他日期刻意不可點。
- 回顧分頁的練習紀錄是假造的（從真實收盤價長出來），選任一筆都用 08-21 的 K 棒示意。
- 重播只有 08-21 有資料，選別天會看到「這天沒有本機 K 棒可以重播」的空狀態——那也是設計的一部分。


---

## 15. 驗收後的裁定（PM，2026-08-25）

lab-qa 驗收時抓到規格自己前後打架，以下為最終裁定，**以本節為準**：

### 15.1 翻頁列的「今天」鈕與「即時」燈固定同寬 52px —— 採用

§11.5 原本寫翻頁列「一格都不要動」。實作時改成兩顆互斥按鈕同寬，理由成立：
原尺寸（46 / 44）按 ◀ 之後 ◀ 會位移 **2.00px**，本來就沒過自檢表的「< 2px」；
改完是 0.00px，而且翻頁列的 DOM、總高、總寬、星期固定寬全部量到一模一樣。
§11.5 那條的用意就是在防「一縮 ◀ 就往右跑、連點第 2 下誤開月曆」——這個改動正是在服務它。

### 15.2 位階／量能量尺的金色 `.hot` —— 保留，§11.1 的括號作廢

§4 要求「位階 >0.8 或 <0.2、量能 >1.5 就標金」，§11.1 括號又寫「含顏色暗示強弱的量尺」不准。
裁定**保留 `.hot`**：CLAUDE.md 那條紅線禁的是「預測、勝率預估、期望值、買賣建議、訊號強度」，
也就是**任何指向未來或方向的東西**。`.hot` 只是把「這個已經發生的數字落在極端」標出來，
不指方向、不建議動作，性質跟 K 線上一根特別長的棒子一樣。老闆拍板的 demo 也是這個樣子。

⚠️ 界線在這裡：**可以標「這個數字很極端」，不可以標「所以會漲／會跌／該進場」。**
以後要加任何顏色暗示，先回去對 CLAUDE.md 那一條。

### 15.3 已知、非本次引入、另開單處理

- 完全抓不到 K 棒時，整張 K 線卡連同翻頁列與迷你月曆一起消失 ⇒ 換不了日期。舊版就有。
- `_net === 0` 的紀錄會被歸成 loss（綠色左緣）。實務上 ±100 含手續費不會剛好 0。


### 15.4 上線後才發現的（2026-08-25）

**清單被壓扁**：`.list` 是有 `max-height` 的 flex 直欄，子元素沒設 `flex:none`，
筆數一多就把每一列壓扁而不是捲動（107px → 21.6px，字全切掉）。
QA 沒抓到是因為治具的假資料筆數少、總高沒超過 `max-height` —— **這種清單一定要用
「超過容器高度的筆數」測**，已補進自檢表。

**清單裡的心得改成一行、去框去底**：帶框的完整樣子留給回顧分頁的「這一筆」。
清單是拿來掃結果的，每列 107px 的話 290px 只放得下 2.7 列。改完是 66.8px、一次看得到 3.9 列。
