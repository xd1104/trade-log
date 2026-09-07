/*
  【細節】分頁（逐筆早盤圖）的探針 —— TICK-TAB-SPEC.md §12 的 12 條驗收。

  ⚠️ 不連永豐、⛔ **不碰 8770**（Benson 的面板正開著）。只打治具（8771／控制埠 8772）。
     跑法：先起 tools/probe/fe_harness.py，改過程式一定要**重起治具**，再
           node tools/probe/tick-tab.mjs

  ⛔ 治具與探針用的價格一律 **12000 附近**，一個真實成交價／真實進出場時間／
     真實點數都不准出現（repo 是公開的，leak-scan.py 在守）。

  【每一條都要有負控組】沒有負控組的綠燈在這個專案不算數（「有測試」≠「測試在保護那件事」）。
  這支的負控組一律用**原始碼突變**：把產品函式 toString() 出來、刪掉那道守衛、eval 回去。
  好處是**不會跟產品程式分岔**（手抄一份 stub 只能證明 stub 沒問題），
  而且每次突變都會先斷言「目標字串真的在原始碼裡」「檔案內容真的變了」——
  突變沒套用卻回報一片綠，是這類工具最典型的假綠燈。
*/
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { CDP } from "./cdp.mjs";

const CHROME = process.env.CHROME || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const A = Object.fromEntries(process.argv.slice(2).map(s => {
  const [k, v] = s.replace(/^--/, "").split("=");
  return [k, v ?? true];
}));
const URL_ = A.url || "http://127.0.0.1:8771/";
const CTL = Number(A.ctl || 8772);
const DEV = Number(A.dev || 9817);
const SOAK = Number(A.soak || 60);          // §12-10「停留 60 秒」

const sleep = ms => new Promise(r => setTimeout(r, ms));
let FAIL = 0, N = 0;
const say = (ok, name, extra) => {
  N++; if (!ok) FAIL++;
  console.log((ok ? "  OK   " : "  FAIL ") + name + (extra ? "  " + extra : ""));
};
const chk = (name, got, want) => say(JSON.stringify(got) === JSON.stringify(want), name,
  JSON.stringify(got) === JSON.stringify(want) ? "" : `(得到 ${JSON.stringify(got)}，期待 ${JSON.stringify(want)})`);
const le = (name, got, max) => say(typeof got === "number" && got <= max, name, `= ${got}（門檻 ≤ ${max}）`);
const ge = (name, got, min) => say(typeof got === "number" && got >= min, name, `= ${got}（門檻 ≥ ${min}）`);
const ctl = async p => (await fetch(`http://127.0.0.1:${CTL}${p}`)).json();

const profile = fs.mkdtempSync(path.join(os.tmpdir(), "tick-probe-"));
// ⚠️ headless 預設視窗只有 800x600，元素會被擠到 viewport 外 ⇒ 真滑鼠事件打不到任何東西，
//    「不該觸發」那一類測項全部白過（hold-to-fire.mjs 踩過）。
const ch = spawn(CHROME, ["--headless=new", "--remote-debugging-port=" + DEV,
  "--user-data-dir=" + profile, "--no-first-run", "--no-default-browser-check",
  "--hide-scrollbars", "--window-size=1500,1100", "about:blank"],
  { stdio: "ignore", shell: false });
for (let i = 0; i < 200; i++) {
  try { await fetch(`http://127.0.0.1:${DEV}/json/version`); break; } catch { await sleep(100); }
}
const c = await CDP.attach(DEV);
await c.send("Page.enable"); await c.send("Runtime.enable"); await c.send("Log.enable");

const ERRORS = [];
c.on("Runtime.exceptionThrown", p =>
  ERRORS.push("exception: " + (p.exceptionDetails?.exception?.description ||
    p.exceptionDetails?.text || "?").slice(0, 200)));
c.on("Log.entryAdded", p => {
  if (p.entry.level === "error") ERRORS.push("console: " + String(p.entry.text).slice(0, 200));
});
c.on("Page.javascriptDialogOpening", async () =>
  await c.send("Page.handleJavaScriptDialog", { accept: true }));

const ev = async (e) => {
  const r = await c.send("Runtime.evaluate",
    { expression: e, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error("evaluate: " +
    (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
  return r.result.value;
};

/* ── 原始碼突變（負控組的唯一工具）───────────────────────────────────────
   自證兩道：① 目標字串必須真的在原始碼裡（不在＝尺壞了，不是程式沒事）
             ② 突變後的字串必須跟原檔不同 */
async function mutate(fn, from, to) {
  const r = await ev(`(()=>{
    const f=window[${JSON.stringify(fn)}];
    if(typeof f!=='function') return {ok:false,why:'找不到 '+${JSON.stringify(fn)}};
    const src=f.toString();
    if(src.indexOf(${JSON.stringify(from)})<0) return {ok:false,why:'目標字串已失效（尺壞了）'};
    const out=src.split(${JSON.stringify(from)}).join(${JSON.stringify(to)});
    if(out===src) return {ok:false,why:'突變沒有真的改到東西'};
    window.__orig=window.__orig||{};
    if(!window.__orig[${JSON.stringify(fn)}]) window.__orig[${JSON.stringify(fn)}]=f;
    window[${JSON.stringify(fn)}]=eval('('+out+')');
    return {ok:true};
  })()`);
  say(!!r.ok, `突變已套用：${fn}`, r.ok ? "" : "(" + r.why + ")");
  return !!r.ok;
}
const unmutate = fn => ev(`(()=>{ if(window.__orig&&window.__orig[${JSON.stringify(fn)}])
  window[${JSON.stringify(fn)}]=window.__orig[${JSON.stringify(fn)}]; return 1; })()`);

/* ── canvas 上**真的畫出去的字**（lab-qa 2026-09-07 退件 M2）─────────────
   這一頁最要命的兩條紅線都畫在 canvas 上，DOM 一個字都讀不到：
   ①「這段沒有錄到（**不是沒行情**）」那句 —— 取樣日不可以講（我們證不了）；
   ② 圖上左下角那張取樣金籤。
   舊版探針只掃副標（`#tksub`）⇒ 把 canvas 那句改回「不是沒行情」照樣 159/159 全綠，
   **而他早上盯的是圖不是副標**。
   量法：把那個 2d context 的 fillText 暫時包一層，跑一次 tkDraw 收集這一次繪製
   真的送進 canvas 的字串 —— 不是讀原始碼，是量畫面。 */
const canvasText = async () => await ev(`(()=>{
  const cv=document.getElementById('tkcv'), ctx=cv.getContext('2d');
  const orig=ctx.fillText, out=[];
  ctx.fillText=function(t){ out.push(String(t)); return orig.apply(this,arguments); };
  try{ tkDraw(); } finally { ctx.fillText=orig; }
  return out;})()`);
const canvasSays = async () => (await canvasText()).join(" ⏐ ");

const box = async sel => await ev(`(()=>{const e=document.querySelector(${JSON.stringify(sel)});
  if(!e) return null; const r=e.getBoundingClientRect();
  return {x:r.x,y:r.y,w:r.width,h:r.height};})()`);

async function goTick() {
  await ev("setTab('tick')");
  for (let i = 0; i < 60; i++) {
    const st = await ev("[TK.pending,!!(TK.data&&TK.data.date===TK.date)]");
    if (!st[0] && st[1]) return true;
    await sleep(200);
  }
  return false;
}
async function goDay(d) {
  await ev(`tkGoDay(${JSON.stringify(d)})`);
  for (let i = 0; i < 60; i++) {
    const st = await ev("[TK.pending,TK.date,TK.data&&TK.data.date]");
    if (!st[0] && st[1] === st[2]) return true;
    await sleep(200);
  }
  return false;
}

const W = await ctl("/tick/where");
const D = W.dates;
console.log(`治具資料：${W.dir}`);
console.log(`日期：今天=${D.full} 缺口日=${D.gappy} 三列=${D.tiny} 只有檔頭=${D.blank} 假逐筆=${D.decoy} 取樣日=${D.polled}\n`);
await ctl("/tick/reset");
await ctl("/tick/clock/");

await c.send("Page.navigate", { url: URL_ });
await sleep(2200);

/* ═══ ① 進得去、畫得出來、console 零錯誤 ═══════════════════════════════ */
console.log("=== ① 分頁與第一畫面 ===");
chk("頂列三顆分頁", await ev(`[...document.querySelectorAll('.tabs button')].map(b=>b.textContent)`),
  ["即時", "細節", "回顧"]);
chk("分頁順序：細節在中間",
  await ev(`[...document.querySelectorAll('.tabs button')][1].getAttribute('data-tab')`), "tick");
say(await goTick(), "切進【細節】並載入完成");
chk("TAB", await ev("TAB"), "tick");
chk("即時分頁被藏起來", await ev("document.getElementById('tab-live').hidden"), true);
chk("預設圖種＝秒K", await ev("TK.v"), "C");
chk("預設桶寬＝自動", await ev("TK.bar"), "auto");
chk("預設疊圖", await ev("JSON.stringify(TK.ov)"),
  JSON.stringify({ trade: true, stop: true, vol: true, idx: false, fixed: true }));
ge("畫出來的 K 棒根數", await ev("TK.drawn"), 60);
chk("45 分鐘全景的自動桶寬＝30 秒", await ev("tkBarSec()"), 30);
chk("預設看的是今天", await ev("TK.date===TK.today"), true);
// 尺的自證：headless 若把頁面判成背景，2 秒輪詢那一條會整個測不到
chk("頁面是可見的（否則 §12-10 的輪詢等於沒測）", await ev("document.hidden"), false);

/* ═══ ①b 買賣價帶真的有資料、也真的畫得出來（lab-qa 2026-09-07 退件 R1）═══
   「折線＋買賣價帶」是**出貨的三種圖種之一**，但這 108 項裡原本連一條
   「價帶真的有東西」都沒有 ⇒ QA 把後端 `elif k == "b":` 改成永不匹配，
   前後端全綠、圖上只剩一條沒有價帶的折線。**這一節就是補那個洞。** */
console.log("\n=== ①b 買賣價帶（三種圖種之一，不可以是空的）===");
const band = await ev(`(()=>{const D=TK.data; let n=0,ok=0;
  for(let i=0;i<D.len;i++) if(isFinite(D.bl[i])&&isFinite(D.ah[i])){
    n++; if(D.ah[i]>=D.bl[i]) ok++; }
  return {len:D.len,n:n,ok:ok};})()`);
ge("有買賣價帶的桶數佔比（%）", Math.round(band.n / band.len * 100), 90);
chk("有價帶的桶都是 最高賣價 ≥ 最低買價", band.ok, band.n);
/* 而且要真的畫上去：同一份資料、同一個視窗，折線(A) 與 折線+價帶(B) 的畫面必須不同。
   價帶整個沒有資料時 B 會**退化成 A**（tkLine 的 isFinite 會把整條帶跳過、
   tkAxis 也不會把 bl/ah 納入）—— 那正是後端不記錄 k=="b" 時畫面的樣子。 */
const bandPix = await ev(`(()=>{
  const cv=document.getElementById('tkcv'), ctx=cv.getContext('2d');
  const grab=()=>{const d=ctx.getImageData(0,0,cv.width,cv.height).data;
    let s=0; for(let i=0;i<d.length;i+=4) s+=d[i]+d[i+1]+d[i+2]; return s;};
  const v0=TK.v, view0=TK.view;
  TK.v='A'; TK.view=null; TKAXIS.key=null; tkDraw(); const a=grab();
  TK.v='B'; TKAXIS.key=null; tkDraw(); const b=grab();
  TK.v=v0; TK.view=view0; TKAXIS.key=null; tkPaint();
  return {a:a,b:b};})()`);
say(bandPix.a !== bandPix.b,
  "折線+價帶 畫出來的畫面跟純折線不一樣（價帶真的畫上去了）",
  `A=${bandPix.a} B=${bandPix.b}`);

/* ═══ ⑥ 舊的輪詢檔不可以被當成逐筆檔（§12-6）══════════════════════════ */
console.log("\n=== ⑥ 輪詢檔不可以被當成逐筆檔 ===");
const days = await ev("JSON.stringify({days:TK.days,skipped:TK.skipped,since:TK.since})");
const DD = JSON.parse(days);
chk(`假逐筆檔 ${D.decoy} 不在 days 裡`, DD.days.some(x => x.d === D.decoy), false);
chk(`假逐筆檔 ${D.decoy} 出現在 skipped 裡`, DD.skipped.indexOf(D.decoy) >= 0, true);
// 負控組：一個只有 3 列的**合法**逐筆檔必須出現在 days 裡（證明尺不是「什麼都跳過」）
chk(`負控組：合法的 3 列逐筆檔 ${D.tiny} 出現在 days 裡`,
  DD.days.some(x => x.d === D.tiny), true);
chk(`只有檔頭的 ${D.blank} 列得出來但 empty:true`,
  (DD.days.find(x => x.d === D.blank) || {}).empty, true);
chk("只有檔頭的那天在清單裡是 disabled",
  await ev(`(()=>{ TK.pick=true; tkPaint();
    const b=document.querySelector('[data-tkday="${D.blank}"]');
    const r=b?b.disabled:null; TK.pick=false; tkPaint(); return r;})()`), true);
chk("◀▶ 跳過沒有錄到的日子（走清單索引，不是日期減一天）",
  await ev(`(()=>{const L=(TK.days||[]).filter(x=>!x.empty).map(x=>x.d);
    return L.indexOf(${JSON.stringify(D.blank)})<0;})()`), true);

/* ═══ ⑤ 紅線：無預測字眼（§12-5）════════════════════════════════════ */
console.log("\n=== ⑤ 紅線：不得出現預測字眼 ===");
const BAN = ["預測", "預估", "預期", "勝率", "期望值", "建議", "訊號", "強度", "看漲", "看跌",
  "機率", "準確率", "目標價", "支撐", "壓力", "買點", "賣點", "該進場", "可以進",
  "追多", "追空", "反轉", "突破訊號"];
const scan = async () => await ev(`(()=>{
  const root=document.getElementById('tab-tick');
  let txt=root.textContent||'';
  root.querySelectorAll('[title],[aria-label]').forEach(e=>{
    txt+=' '+(e.getAttribute('title')||'')+' '+(e.getAttribute('aria-label')||''); });
  return txt; })()`);
const scanHits = async () => { const t = await scan(); return BAN.filter(w => t.indexOf(w) >= 0); };
// 每一種畫面狀態都要掃到（只掃一個狀態＝涵蓋範圍比宣稱的小）
const states = [];
for (const [name, setup] of [
  ["秒K", `TK.v='C';TK.bar='auto';tkPaint()`],
  ["折線", `TK.v='A';tkPaint()`],
  ["折線+價帶", `TK.v='B';tkPaint()`],
  ["日期清單展開", `TK.pick=true;tkPaint()`],
  ["hover 讀值列", `TK.pick=false;TK.hover=400;tkPaint()`],
]) { await ev(setup); states.push([name, await scanHits()]); }
for (const [name, hits] of states) chk(`禁詞命中（${name}）`, hits, []);
await ev("TK.v='C';TK.hover=null;TK.pick=false;tkPaint()");
// 負控組：往 DOM 塞一個含禁詞的節點，掃描必須紅
await ev(`(()=>{const d=document.createElement('div');d.id='__ban';
  d.textContent='勝率 62%，建議追多';document.getElementById('tab-tick').appendChild(d);})()`);
ge("負控組：塞進禁詞後掃描必須命中", (await scanHits()).length, 1);
await ev(`document.getElementById('__ban').remove()`);
chk("負控組移除後回到 0 命中", await scanHits(), []);
// 紅綠只准代表漲跌／賺賠
const redgreen = await ev(`(()=>{
  const up='rgb(238, 90, 84)', dn='rgb(52, 179, 126)';
  const out=[];
  document.querySelectorAll('#tab-tick *').forEach(e=>{
    const cs=getComputedStyle(e);
    if(cs.color===up||cs.color===dn)
      out.push({tag:e.tagName,cls:e.className,txt:(e.textContent||'').slice(0,12),
                inRead:!!e.closest('#tkread')});
  });
  return out; })()`);
chk("用到紅綠的 DOM 元素全部在讀值列（＝那一格是收盤價的漲跌）",
  redgreen.filter(x => !x.inRead), []);

/* ═══ ⑧ canvas 尺寸不亂動（§12-8）══════════════════════════════════ */
console.log("\n=== ⑧ canvas 的 width/height 只有尺寸真的變才准動 ===");
await ev(`(()=>{
  const cv=document.getElementById('tkcv');
  const dw=Object.getOwnPropertyDescriptor(HTMLCanvasElement.prototype,'width');
  const dh=Object.getOwnPropertyDescriptor(HTMLCanvasElement.prototype,'height');
  window.__wset=0;
  Object.defineProperty(cv,'width',{configurable:true,
    get(){return dw.get.call(cv)}, set(v){window.__wset++; dw.set.call(cv,v)}});
  Object.defineProperty(cv,'height',{configurable:true,
    get(){return dh.get.call(cv)}, set(v){dh.set.call(cv,v)}});
})()`);
await ev("window.__wset=0; for(let i=0;i<200;i++) tkDraw();");
chk("連續 200 次 tkDraw()，canvas.width 的 setter 呼叫次數", await ev("window.__wset"), 0);
if (await mutate("tkFit", "if(w===TKC.W&&h===TKC.H&&dpr===TKC.DPR&&cv.width) return cv;", "")) {
  await ev("window.__wset=0; for(let i=0;i<200;i++) tkDraw();");
  chk("負控組：拿掉 early-return 之後必須是 200", await ev("window.__wset"), 200);
  await unmutate("tkFit");
  await ev("window.__wset=0; for(let i=0;i<200;i++) tkDraw();");
  chk("裝回去之後回到 0", await ev("window.__wset"), 0);
}

/* ═══ ⑦ 價格軸不亂跳（§12-7）═══════════════════════════════════════ */
console.log("\n=== ⑦ 價格軸不亂跳（逐秒餵一整天）===");
await ev(`window.__axisScan=function(){
  const D=TK.data, full=D.len;
  TK.v='C'; TK.bar='auto'; TK.ov.fixed=true; TK.view=null; TKAXIS.key=null; TKBARC.key='';
  let prev=null, changes=0; const seq=[];
  for(let n=30;n<=full;n++){
    D.len=n; TKBARC.key='';           // 資料長大了，桶快取失效
    tkDraw();
    const k=TKAXIS.hi+'|'+TKAXIS.lo;
    if(prev!==null&&k!==prev){ changes++; seq.push(n+':'+k); }
    prev=k;
  }
  D.len=full; TKBARC.key=''; TKAXIS.key=null; tkDraw();
  return {changes:changes, steps:full-30, seq:seq.join(' ')};
}`);
const ax = await ev("window.__axisScan()");
let axNiceOff = null;              // 負控組（拿掉 niceStep 貼齊）的改變次數，收尾要印
ge("模擬的步數（尺的自證：真的逐秒餵了一整天）", ax.steps, 2000);
le("整段期間價格軸上下緣改變次數（秒K 預設）", ax.changes, 3);
/* 負控組 A：把 niceStep 的貼齊拿掉 —— 這是穩定價格軸的**主要**那一層，必須爆增。 */
if (await mutate("tkAxis",
  "aHi=Math.ceil(aHi/step)*step; aLo=Math.floor(aLo/step)*step;", "")) {
  const axN = await ev("window.__axisScan()");
  axNiceOff = axN.changes;
  /* 門檻取「≥ 3 倍且至少多 3 次」：不貼齊整數刻度時，軸的變動次數 ≈ 隨機漫步的
     running min/max 更新次數（O(log n)，實測 16 次），本來就不會像滑動視窗那樣爆到上百次。
     規格引用的「8 次 vs 1 次」是即時分頁**滑動視窗**的情境，這張圖的時間軸是固定的。 */
  ge("負控組：拿掉 niceStep 貼齊之後改變次數必須明顯變多", axN.changes, ax.changes * 3 + 3);
  await unmutate("tkAxis");
  le("裝回去之後回到門檻內", (await ev("window.__axisScan()")).changes, 3);
}
/* 遲滯那一層：在「時間軸固定 ＋ 資料只增不減」的情況下，新範圍**永遠**不會被舊軸包住
   （min/max 對更多資料是單調的）⇒ 遲滯的 early-return 結構上進不去。
   ⚠️ 所以這裡不假裝它有負控組，而是**證明它在這個模式下是不承重的**：
      把它拿掉，整條軸的變化序列必須逐字相同。這是誠實的量法，也是交給下一個人的訊息 ——
      真正在守這張圖的是 niceStep，遲滯是為了「使用者停在同一個視窗、資料在動」而留的。 */
await ev(`window.__origAxis=window.tkAxis;
  window.tkAxis=function(){ TKAXIS.key=null; return window.__origAxis(); }`);
const ax2 = await ev("window.__axisScan()");
chk("拿掉遲滯之後的軸序列（固定時間軸下遲滯結構上不承重，序列必須一模一樣）",
  ax2.seq === ax.seq, true);
await ev("window.tkAxis=window.__origAxis;");
const ax3 = await ev("window.__axisScan()");
le("裝回去之後回到門檻內", ax3.changes, 3);
/* 遲滯 key 帶了「視窗起｜視窗迄」⇒ 縮放時必須立刻重新貼合，不可以留「軸黏在上一個視窗」的鬼影 */
const zoomFit = await ev(`(()=>{
  TK.view=null; TKAXIS.key=null; tkDraw();
  const wide=TKAXIS.hi-TKAXIS.lo;
  const v=tkView(), mid=(v.t0+v.t1)/2;
  TK.view={t0:mid-30,t1:mid+30}; tkDraw();
  const narrow=TKAXIS.hi-TKAXIS.lo;
  TK.view=null; TKAXIS.key=null; tkDraw();
  return {wide:wide, narrow:narrow}; })()`);
say(zoomFit.narrow < zoomFit.wide, "縮放之後價格軸立刻重新貼合（沒有上一個視窗的鬼影）",
  `全景跨度 ${zoomFit.wide} → 放大到 60 秒 ${zoomFit.narrow}`);

/* ═══ ② 4 萬點的繪製與拖曳（§12-1）════════════════════════════════ */
console.log("\n=== ② 40,000 點的繪製與拖曳 ===");
/* ⚠️ 注入的 s 用 **Float64Array（次秒解析度）** 而不是整數秒。
   真的逐筆是毫秒級的；用整數秒的話 40,000 個點只落在 2,700 個相異 x 上，
   「不分桶」那條折線會退化成一堆零長度線段、被瀏覽器便宜地帶過 ——
   負控組因此量到 27~56 ms（跑三次差兩倍），完全反映不出真實成本。
   （產品路徑上 s 是後端給的整秒，型別不影響繪圖，只讀數值。） */
await ev(`window.__inject=function(n){
  const cap=n+8;
  const N={date:TK.date,len:n,cap:cap,idx:Object.create(null),
    s:new Float64Array(cap),o:new Float32Array(cap),h:new Float32Array(cap),
    l:new Float32Array(cap),c:new Float32Array(cap),vq:new Int32Array(cap),
    bl:new Float32Array(cap),ah:new Float32Array(cap),
    gaps:[],bad:0,n:n,heads:1,v:1,vmix:false,
    first:'08:45:00.000',last:'09:29:59.000',complete:true,trades:[],sec0:31500};
  let px=12000,seed=7;
  const rnd=()=>{seed=(seed*1103515245+12345)&0x7fffffff; return seed/0x7fffffff;};
  for(let i=0;i<n;i++){
    N.s[i]=i/(n-1)*2700;
    px+=(rnd()-0.5)*(rnd()>0.99?8:1.6);
    N.o[i]=px; N.h[i]=px+1; N.l[i]=px-1; N.c[i]=px;
    N.vq[i]=1+((i*7)%5); N.bl[i]=px-1.5; N.ah[i]=px+1.5;
  }
  TK.cache[TK.date]=N; TK.data=N; TKBARC.key=''; TKAXIS.key=null; TK.view=null;
  TK.hover=null; tkPaint(); return {len:N.len};
}`);
chk("注入 40,000 點", await ev("window.__inject(40000)"), { len: 40000 });

// 真滑鼠拖曳 60 步，量每一步「事件處理完成」的耗時
await ev(`(()=>{
  const cv=document.getElementById('tkcv');
  window.__steps=[]; window.__t0=0;
  // 起點掛在 window 的捕捉相位（一定排在目標元素的 listener 之前）
  window.addEventListener('pointermove',()=>{window.__t0=performance.now();},true);
  // 終點掛在 canvas 上、而且是**最後才加**的 ⇒ 排在 app 那顆之後
  cv.addEventListener('pointermove',()=>{window.__steps.push(performance.now()-window.__t0);},false);
})()`);
const cvbox = await box("#tkcv");
const px0 = Math.round(cvbox.x + cvbox.w * 0.5), py0 = Math.round(cvbox.y + cvbox.h * 0.5);
// 尺的自證：那一點必須真的在畫面內，而且上面真的是 canvas
chk("拖曳的落點上真的是 canvas",
  await ev(`(document.elementFromPoint(${px0},${py0})||{}).id`), "tkcv");
async function drag(steps) {
  await ev("window.__steps=[]; TK.times=[];");
  await c.send("Input.dispatchMouseEvent",
    { type: "mousePressed", x: px0, y: py0, button: "left", buttons: 1, clickCount: 1 });
  for (let i = 0; i < steps; i++) {
    await c.send("Input.dispatchMouseEvent",
      { type: "mouseMoved", x: px0 - i * 3, y: py0, button: "left", buttons: 1 });
  }
  await c.send("Input.dispatchMouseEvent",
    { type: "mouseReleased", x: px0 - steps * 3, y: py0, button: "left", buttons: 0 });
  const s = await ev("window.__steps.slice().sort((a,b)=>a-b)");
  return { n: s.length, median: s.length ? +s[s.length >> 1].toFixed(2) : null };
}
/* ⚠️ 兩種時間要分開看（demo 的 __bench 註解就講過這件事）：
   ① **事件處理耗時**＝JS 把繪圖指令堆出來的時間（真滑鼠拖曳量到的就是這個）；
   ② **每格畫面耗時**＝每畫一次就讓出去等一個 rAF，逼瀏覽器真的合成、真的點陣化。
   規格 §8.1 那張表（155.1 / 152.8 / 15.4 ms）量的是 ②。只量 ① 會系統性低估。 */
await ev(`window.__frameBench=function(n){ return new Promise(res=>{
  const t=[]; let i=0, prev=performance.now();
  const step=()=>{
    const v=tkView(), d=(v.t1-v.t0)*0.004;
    TK.view={t0:v.t0+d,t1:v.t1+d}; tkClampView();
    tkDraw();
    requestAnimationFrame(()=>{ const now=performance.now(); t.push(now-prev); prev=now;
      if(++i<n) step(); else { t.shift(); t.sort((a,b)=>a-b);
        res({n:t.length, median:+t[t.length>>1].toFixed(2),
             fps:+(1000/t[t.length>>1]).toFixed(1)}); } });
  }; step(); }); };
window.__idleFrames=function(n){ return new Promise(res=>{
  const t=[]; let i=0, prev=performance.now();
  const step=()=>requestAnimationFrame(()=>{ const now=performance.now();
    t.push(now-prev); prev=now; if(++i<n) step();
    else { t.shift(); t.sort((a,b)=>a-b); res(+t[t.length>>1].toFixed(2)); } });
  step(); }); };`);
/* ⭐ 先驗尺：headless 若沒有強迫合成，rAF 會被節流到個位數 fps ⇒ 上面那些數字全部是骰子
      （keyring 那一輪實測過 1.7fps）。空迴圈的每格時間必須接近 60fps 才准往下量。 */
const idleMs = await ev("window.__idleFrames(30)");
le("尺的自證：空 rAF 迴圈每格 ms（不接近 16.7 就代表沒有真的在合成）", idleMs, 25);

const perf = {};
await ev("TK.v='C'; TK.bar='auto'; TK.view=null; TKAXIS.key=null; tkPaint();");
perf.barsDrag = await drag(60);
ge("秒K 拖曳真的有 60 步（尺的自證）", perf.barsDrag.n, 55);
perf.barsFrame = await ev("window.__frameBench(60)");
le("秒K（自動桶寬）每格畫面 ms", perf.barsFrame.median, 20);
say(perf.barsDrag.median <= 20, "秒K 真滑鼠拖曳的事件處理每步中位 ms",
  `= ${perf.barsDrag.median}`);
await ev("TK.v='B'; TK.ov.vol=false; TK.view=null; TKAXIS.key=null; tkPaint();");
perf.lineDrag = await drag(60);
perf.lineFrame = await ev("window.__frameBench(60)");
le("折線+價帶（像素分桶）每格畫面 ms", perf.lineFrame.median, 30);
say(perf.lineDrag.median <= 30, "折線+價帶 真滑鼠拖曳的事件處理每步中位 ms",
  `= ${perf.lineDrag.median}`);
const single = await ev(`(()=>{TK.times=[]; for(let i=0;i<25;i++) tkDraw();
  const s=TK.times.slice().sort((a,b)=>a-b); return +s[s.length>>1].toFixed(2);})()`);
le("單次繪製中位 ms（25 次）", single, 12);

/* 負控組：把折線改成「一個點都不省」（＝ Canvas 全部點，spec §8.1 那一列）。
   ⚠️ 這一條**不是刪守衛**而是換演算法，所以在探針裡寫出來 —— 它的存在意義是
      證明上面那些綠燈不是「量錯了」。 */
await ev(`window.__origLine=window.tkLine;
window.tkLine=function(ctx,D,i0,i1,xOf,yOf,PW){
  if(TK.v==='B'){
    ctx.fillStyle='rgba(124,140,168,.20)'; ctx.beginPath();
    for(let i=i0;i<=i1;i++){const x=xOf(D.s[i]); i===i0?ctx.moveTo(x,yOf(D.ah[i])):ctx.lineTo(x,yOf(D.ah[i]));}
    for(let i=i1;i>=i0;i--) ctx.lineTo(xOf(D.s[i]),yOf(D.bl[i]));
    ctx.closePath(); ctx.fill();
  }
  ctx.strokeStyle='#E9ECF1'; ctx.lineWidth=1.4; ctx.lineJoin='round'; ctx.beginPath();
  for(let i=i0;i<=i1;i++){const x=xOf(D.s[i]),y=yOf(D.c[i]); i===i0?ctx.moveTo(x,y):ctx.lineTo(x,y);}
  ctx.stroke(); return i1-i0+1;
}`);
perf.rawDrag = await drag(60);
perf.rawFrame = await ev("window.__frameBench(60)");
/* ⚠️ 規格寫的負控組門檻是「每步 ≥ 100 ms」（lab-ux 在 demo 上量到 152.8 ms）。
   我在這台機器上量不到那麼慢：40,000 點不分桶是 **55.9 ms / 17.9 fps**。
   差異的來源之一是資料形狀（我的合成資料時間解析度只到秒，40,000 點只落在 2,700 個
   相異 x 上，折線的線段大量退化成零長度）。**不把門檻降到剛好會過**，改成兩條
   站得住腳的判準：①比分桶慢 ≥ 5 倍 ②fps 掉到 ≤ 30（規格自己淘汰 SVG 的線是 6.4 fps）。
   絕對值另外印出來，讓看的人自己判斷。 */
ge("負控組：不分桶比分桶慢幾倍（每格畫面）",
  +(perf.rawFrame.median / perf.lineFrame.median).toFixed(1), 5);
le("負控組：不分桶的 fps 必須掉到不能用", perf.rawFrame.fps, 30);
console.log(`  ⚠️ 規格的負控組門檻是「每步 ≥ 100 ms」，這台機器實測 ${perf.rawFrame.median} ms` +
  `（${perf.rawFrame.fps} fps）—— 沒有達到規格那個絕對值，數字照實列在報告裡。`);
console.log(`  （參考）不分桶的事件處理每步中位 ${perf.rawDrag.median} ms`);
await ev("window.tkLine=window.__origLine;");

/* ═══ ③ 抽稀之後看不到什麼（§12-2，硬證明）═════════════════════════ */
console.log("\n=== ③ 像素分桶是無損的（逐欄 min/max 比對）===");
/* ⚠️ 差異只准出現在「同一像素欄之內的先後順序」—— 那正是我們丟掉的東西，
      下一個人要知道分桶丟的是什麼。每欄的最高最低一個都沒少。 */
await ev(`window.__colDiff=function(){
  const D=TK.data, v=tkView(), PW=TKC.W-64;
  const xOf=s=>(s-v.t0)/(v.t1-v.t0)*PW;
  const r=tkRange(), i0=r[0], i1=r[1];
  const cols=Math.max(1,Math.ceil(PW));
  const C=tkCols(D,i0,i1,xOf,cols);                 // 受測：產品的分桶
  const rmn=new Float64Array(cols).fill(Infinity), rmx=new Float64Array(cols).fill(-Infinity);
  for(let i=i0;i<=i1;i++){ let cc=Math.floor(xOf(D.s[i]));
    if(cc<0)cc=0; if(cc>=cols)cc=cols-1;
    if(D.l[i]<rmn[cc])rmn[cc]=D.l[i]; if(D.h[i]>rmx[cc])rmx[cc]=D.h[i]; }
  let maxd=0, used=0, missing=0;
  for(let cc=0;cc<cols;cc++){
    const a=isFinite(C.mn[cc]), b=isFinite(rmn[cc]);
    if(!a&&!b) continue;
    used++;
    if(a!==b){ missing++; continue; }
    maxd=Math.max(maxd,Math.abs(C.mn[cc]-rmn[cc]),Math.abs(C.mx[cc]-rmx[cc]));
  }
  return {maxColDiff:maxd, cols:used, missing:missing, pts:i1-i0+1};
}`);
await ev("TK.v='B'; TK.view=null; TKAXIS.key=null; tkPaint();");
const cd = await ev("window.__colDiff()");
ge("比對到的像素欄數（尺的自證）", cd.cols, 500);
ge("比對到的資料點數（尺的自證）", cd.pts, 30000);
chk("maxColDiff（分桶 vs 全部點，逐欄最高最低）", cd.maxColDiff, 0);
chk("沒有任何一欄憑空多出來或消失", cd.missing, 0);
if (await mutate("tkCols", "for(let i=i0;i<=i1;i++){", "for(let i=i0;i<=i1;i+=30){")) {
  const cd2 = await ev("window.__colDiff()");
  say(cd2.maxColDiff > 0 || cd2.missing > 0,
    "負控組：改成『每 30 筆取一筆』之後必須對不上",
    `maxColDiff=${cd2.maxColDiff} missing=${cd2.missing}`);
  await unmutate("tkCols");
  chk("裝回去之後 maxColDiff 回到 0", (await ev("window.__colDiff()")).maxColDiff, 0);
}

/* ═══ ④ 換日不會錯置（§12-3）══════════════════════════════════════ */
console.log("\n=== ④ 換日不會錯置 ===");
await ev("TK.cache={}; TK.data=null; TK.pending=false;");
await goDay(D.full);
// 第一個 /api/tick/day 請求延遲 800ms、後續不延遲
let firstHeld = true, held = 0;
/* ⚠️ 攔在哪一個 stage 會決定測到什麼：
     Request  ＝ 請求還沒送到後端就先擋住（後端稍後才讀檔）
     Response ＝ 請求照常打到後端、只把**回應**延後 ⇒ 這一份拿到的是「比較舊的檔案內容」
   要驗「舊的回應不可以蓋掉新的」必須用 Response，用 Request 的話兩份會讀到同一份檔案，
   長度一樣、負控組永遠不會紅（第一版就是這樣拿到假綠燈的）。 */
const holdStage = { v: "Request" };
const enableFetch = () => c.send("Fetch.enable",
  { patterns: [{ urlPattern: "*api/tick/day*", requestStage: holdStage.v }] });
await enableFetch();
c.on("Fetch.requestPaused", async p => {
  if (firstHeld) { firstHeld = false; held++; await sleep(800); }
  try {
    if (p.responseStatusCode != null || p.responseErrorReason)
      await c.send("Fetch.continueResponse", { requestId: p.requestId });
    else await c.send("Fetch.continueRequest", { requestId: p.requestId });
  } catch { }
});
await ev(`window.__samples=[]; window.__stop=false;
(function loop(){ if(window.__stop) return;
  const d=TK.data;
  window.__samples.push({want:TK.date, got:d?d.date:null, pending:TK.pending,
    sub:document.getElementById('tksub').textContent,
    pager:document.getElementById('tkpager').textContent});
  requestAnimationFrame(loop); })();`);
await ev("TK.cache={};");                 // 逼每一次都真的打 API
for (let i = 0; i < 5; i++) { await ev("tkGoDay(tkStep(-1))"); await sleep(60); }
const wantLast = await ev("TK.date");
await sleep(2500);
await ev("window.__stop=true");
const S = await ev("JSON.stringify(window.__samples)");
const samples = JSON.parse(S);
await c.send("Fetch.disable");
ge("換日期間的 rAF 取樣數（尺的自證）", samples.length, 30);
chk("最後畫面上的日期＝最後一次請求的日期", await ev("TK.data&&TK.data.date"), wantLast);
chk("按了 5 次 ◀ 之後真的換到第 6 天（清單索引）",
  wantLast, (JSON.parse(days).days.filter(x => !x.empty).map(x => x.d))[5]);
const NUM = /[\d,]{2,}\s*筆/;
const bad = samples.filter(s => s.want !== s.got && (NUM.test(s.pager) || NUM.test(s.sub)));
chk("載入中（TK.date !== TK.data.date）時，畫面上不可以出現任何筆數", bad.length, 0);
const loadingSamples = samples.filter(s => s.want !== s.got);
ge("真的取樣到「載入中」的畫面（尺的自證）", loadingSamples.length, 1);
say(loadingSamples.every(s => s.pager.indexOf("載入中") >= 0),
  "載入中時翻頁列寫的是「載入中…」");
// 日期鈕寬度不准隨狀態變
const wNormal = await ev(`(()=>{TK.pending=false;tkPaint();
  const b=document.querySelector('#tkpager .dstamp'); return b?+b.getBoundingClientRect().width.toFixed(2):null;})()`);
const wLoading = await ev(`(()=>{TK.pending=true;tkPaint();
  const b=document.querySelector('#tkpager .dstamp'); return b?+b.getBoundingClientRect().width.toFixed(2):null;})()`);
await ev("TK.pending=false;tkPaint()");
chk("日期鈕寬度：載入中 vs 正常（差 px）", +Math.abs(wNormal - wLoading).toFixed(2), 0);
say(await ev(`(()=>{TK.pending=true;tkPaint();
  const b=document.querySelector('#tkpager .dstamp');
  const r=b&&b.classList.contains('loading'); TK.pending=false; tkPaint(); return r;})()`),
  "載入中只把日期轉灰（.loading），不換字");

/* 負控組 A：把「手上這份是不是我想看的那天」那道守衛拿掉 ⇒ 連按 ◀ 必須錯置。
   （這是防「日期掛錯」的那一道 —— 即時分頁 2026-08-23 就是死在這裡。） */
async function pressFive() {
  await ev("clearTimeout(TK.timer); TK.timer=null; TK.cache={}; TK.data=null;");
  await c.send("Fetch.disable");
  await goDay(D.full);            // 先安安靜靜載入今天，⚠️ 這一發**不可以**被攔
  // 攔截要在這裡才打開：第一版寫在 goDay 之前，於是「被 hold 800ms 的那一發」是
  // goDay 自己那一發，5 次連按全部沒有被延遲 ⇒ 根本沒有製造出競態，負控組當然不會紅。
  holdStage.v = "Request"; firstHeld = true;
  await enableFetch();
  await ev("TK.cache={};");
  for (let i = 0; i < 5; i++) { await ev("tkGoDay(tkStep(-1))"); await sleep(60); }
  const want = await ev("TK.date");
  await sleep(2600);
  const got = await ev("TK.data&&TK.data.date");
  await c.send("Fetch.disable");
  return { want, got };
}
/* ⚠️ 這裡**兩道守衛都要拿掉**才紅得起來 —— 它們是互相備援的：
   舊那天的回應先被流水號攔下，就輪不到 `d===TK.date` 出手。
   （第一版只拿掉一道，測項照樣全綠 ⇒ 那會被誤讀成「這道守衛沒有用」。） */
if (await mutate("tkFetchDay", "if(d===TK.date){", "if(true){") &&
    await mutate("tkFetchDay", "if(my!==TK.seq) return;", "")) {
  const r = await pressFive();
  say(r.got !== r.want, "負控組 A：兩道守衛都拿掉之後，連按 ◀ 必須錯置",
    `想看 ${r.want}、手上是 ${r.got}`);
  await unmutate("tkFetchDay");
  const r2 = await pressFive();
  chk("裝回去之後不再錯置", r2.got === r2.want, true);
}
/* 負控組 B：流水號守衛真正在守的是**同一天的兩個請求誰後回來**（先送的後回來，
   會用比較舊的那份蓋掉比較新的 —— 檔案還在長的今天，舊那份就是少一段）。
   ⚠️ 上面那道 `d===TK.date` 擋不掉這一種（兩個請求的 d 都等於 TK.date）。 */
// ⚠️ 09:12 要跟 fe_harness 的 /tick/short（last=33120）是同一個時刻。
//    原本兩邊都是 09:10，而 09:10 撞到他一筆真實交易的分鐘（lab-qa 2026-09-07 抓到）。
await ctl("/tick/clock/09:12");
await ctl("/tick/short");
async function staleRace() {
  await ev(`clearTimeout(TK.timer); TK.timer=null; TK.cache={}; TK.data=null;
            TK.pending=false; TK.date=${JSON.stringify(D.full)};`);
  await c.send("Fetch.disable");
  holdStage.v = "Response"; firstHeld = true;      // 只延後**回應**，讓 A 讀到比較舊的檔案
  await enableFetch();
  // ⚠️ 不可以 await 它的 promise（awaitPromise 會擋住，兩個請求就不會重疊）
  await ev(`(function(){ tkFetchDay(TK.date,false); return 1; })()`);   // A：讀到舊的、回應被 hold
  await sleep(150);
  await ctl("/tick/grow/1500");                   // 檔案在這中間長大了
  await ev(`(function(){ tkFetchDay(TK.date,false); return 1; })()`);   // B：後送先回，拿到新的
  await sleep(450);
  const mid = await ev("TK.data?TK.data.len:null");    // B 落地之後
  await sleep(2200);
  const end = await ev("TK.data?TK.data.len:null");    // A（比較舊那份）落地之後
  await c.send("Fetch.disable");
  return { mid, end };
}
/* ⚠️ 一定要**在同一輪之內**比 mid/end：檔案每跑一輪就長大一次，
   拿「這一輪 vs 上一輪」的長度去比，兩邊的基準不一樣，負控組永遠比不出來
   （第一版就是這樣拿到 1686 vs 1686 的假綠燈）。 */
const raceGuard = await staleRace();
chk("流水號守衛：舊的那份回來之後，畫面上還是新的那份",
  raceGuard.end === raceGuard.mid, true);
if (await mutate("tkFetchDay", "if(my!==TK.seq) return;", "")) {
  const raceNo = await staleRace();
  say(raceNo.end < raceNo.mid, "負控組 B：拿掉流水號守衛之後，舊的那份會蓋掉新的",
    `B 落地時 ${raceNo.mid} 個桶 → A 落地後變成 ${raceNo.end} 個桶`);
  await unmutate("tkFetchDay");
  const raceBack = await staleRace();
  say(raceBack.end === raceBack.mid, "裝回去之後又守得住",
    `${raceBack.mid} → ${raceBack.end}`);
}
await ctl("/tick/clock/");
await ctl("/tick/reset");
await ev("TK.cache={}; TK.data=null;");

/* ═══ ⑨ 缺口有講出來（§12-4）══════════════════════════════════════ */
console.log("\n=== ⑨ 缺口有講出來 ===");
await ctl("/tick/gappy/dirty");
await ev("TK.cache={};");
say(await goDay(D.gappy), `切到缺口日 ${D.gappy}`);
const gapText = async () => await ev(`document.getElementById('tksub').textContent+' '+
  document.getElementById('tkpager').textContent`);
let gt = await gapText();
say(gt.indexOf("沒有錄到") >= 0, "副標寫出「沒有錄到」");
say(gt.indexOf("不是沒行情") >= 0, "副標寫出「不是沒行情」（⛔ 這句不能少）");
say(gt.indexOf("128") >= 0, "畫面上找得到丟棄筆數 128");
say(gt.indexOf("還沒到") < 0, "過去的日子不可以出現「還沒到」");
say(gt.indexOf("列讀不出來") >= 0, "解析壞列有講出來（不可以吞掉）");
// 圖上真的有斜線區（量 canvas 像素：不是純底色）
const hatch = await ev(`(()=>{
  const cv=document.getElementById('tkcv'), ctx=cv.getContext('2d');
  const dpr=TKC.DPR;
  // 08:45~09:03 那段的中間：x 取畫面左邊 1/8 處
  const x=Math.round(TKC.W*0.08*dpr), y=Math.round(TKC.H*0.3*dpr);
  const d=ctx.getImageData(x,y,60,60).data;
  const set=new Set();
  for(let i=0;i<d.length;i+=4) set.add(d[i]+','+d[i+1]+','+d[i+2]);
  return set.size; })()`);
ge("斜線區內的取樣不是純底色（相異顏色數）", hatch, 2);
// 負控組：補滿缺的時段、拿掉痕跡列 ⇒ 三句話必須全部消失
await ctl("/tick/gappy/clean");
await ev("TK.cache={};");
await goDay(D.full); await goDay(D.gappy);
gt = await gapText();
say(gt.indexOf("沒有錄到") < 0 && gt.indexOf("不是沒行情") < 0 && gt.indexOf("128") < 0,
  "負控組：補滿之後那三句話必須全部消失", `(現在是「${gt.slice(0, 60)}…」)`);
const hatch2 = await ev(`(()=>{
  const cv=document.getElementById('tkcv'), ctx=cv.getContext('2d'); const dpr=TKC.DPR;
  const x=Math.round(TKC.W*0.08*dpr), y=Math.round(TKC.H*0.3*dpr);
  const d=ctx.getImageData(x,y,4,4).data; return d[0]+','+d[1]+','+d[2]; })()`);
say(true, "（參考）補滿之後同一點的顏色 = " + hatch2);
await ctl("/tick/gappy/dirty");
await ev("TK.cache={};");

/* ═══ ⑬ 取樣日（-polled 檔）：畫得出來，而且**畫面上**明講那是取樣 ═══════
   2026-09-07：他早上的行情只有取樣檔（面板 12:16 才重啟，逐筆落地那時才生效），
   而這一頁完全讀不到它 —— 因為檔名不符 YYYY-MM-DD.jsonl。
   「不要讓取樣冒充逐筆」的正解是**標示清楚**，不是整個不給看。
   ⛔ 標籤要**真的畫在畫面上**（圖上的籤、日期清單的標記、副標），不是只放在資料裡：
      下面每一條都配一個負控組（拿掉標籤／把種類判斷反過來，必須紅）。
   ⛔ 取樣檔沒有單筆成交量 ⇒ 量柱 disabled ＋ 寫出原因，
      而且**開不開那顆疊圖畫出來要一模一樣**（＝真的一根量柱都沒有）。 */
console.log("\n=== ⑬ 取樣日：讀得到，而且畫面上明講那是取樣 ===");
await goDay(D.full);
// ── 先在**逐筆日**量一組對照值 ─────────────────────────────────────────
const tickSub = await ev("document.getElementById('tksub').textContent");
const tickKind = await ev("TK.data.kind");
const tickVolChip = await ev(`(()=>{const b=document.querySelector('[data-tkov="vol"]');
  return {dis:!!b.disabled, on:b.className.indexOf('on')>=0};})()`);
const tickBar60 = await ev(`(()=>{const v0=TK.view; TK.view={t0:0,t1:60};
  const w=tkBarSec(); TK.view=v0; TKAXIS.key=null; return w;})()`);
const GRAB = `(()=>{const cv=document.getElementById('tkcv'),ctx=cv.getContext('2d');
  const d=ctx.getImageData(0,0,cv.width,cv.height).data; let s=0;
  for(let i=0;i<d.length;i+=4) s+=d[i]+d[i+1]+d[i+2]; return s;})()`;
const VOLTOGGLE = `(()=>{const v0=TK.ov.vol;
  TK.ov.vol=false; TKAXIS.key=null; TKBARC.key=''; tkDraw();
  const off=${GRAB};
  TK.ov.vol=true;  TKAXIS.key=null; TKBARC.key=''; tkDraw();
  const on=${GRAB};
  TK.ov.vol=v0; TKAXIS.key=null; TKBARC.key=''; tkDraw();
  return {off:off,on:on};})()`;
const tickVolPix = await ev(VOLTOGGLE);
chk("對照組（逐筆日）：kind", tickKind, "tick");
chk("對照組（逐筆日）：成交量疊圖可用（不是 disabled）", tickVolChip.dis, false);
say(tickVolPix.on !== tickVolPix.off,
  "對照組（逐筆日）：開關成交量畫出來的東西不一樣（＝那一天真的有量柱）",
  `${tickVolPix.off} vs ${tickVolPix.on}`);
say(tickSub.indexOf("逐秒") >= 0 && tickSub.indexOf("取樣") < 0,
  "對照組（逐筆日）：副標寫「逐秒」、沒有「取樣」", tickSub.slice(0, 46));
/* 對照組（**有缺口的逐筆日**）：canvas 上那句話必須寫著「不是沒行情」 ——
   這是尺的自證：證明 canvasText() 真的量得到圖上那句，取樣日沒有它才有意義。 */
await goDay(D.gappy);
const gapCanvas = await canvasSays();
say(gapCanvas.indexOf("不是沒行情") >= 0,
  "對照組（有缺口的逐筆日）：圖上那句話寫著「不是沒行情」（尺的自證）",
  gapCanvas.slice(0, 90));
const tickIdxTip = await ev(`(()=>{const b=document.querySelector('[data-tkov="idx"]');
  return {dis:!!b.disabled, title:b.getAttribute('title')||''};})()`);

// ── 切到取樣日 ────────────────────────────────────────────────────────
say(await goDay(D.polled), `切到取樣日 ${D.polled}`);
chk("這一天的 kind", await ev("TK.data.kind"), "polled");
chk("has_vol", await ev("TK.data.has_vol"), false);
ge("桶數（1 秒桶，跟逐筆同一種格式）", await ev("TK.data.len"), 100);
ge("繪出來的東西不是空的", await ev("(()=>{tkDraw();return TK.drawn;})()"), 20);
chk("每一個桶的量都是 0（⛔ 不准拿 vol_ratio 之類的東西湊假量柱）",
  await ev("(()=>{let s=0;for(let i=0;i<TK.data.len;i++)s+=TK.data.vq[i];return s;})()"), 0);

// ① 副標：⛔「逐秒」那三個字對取樣日是假的
const pSub = await ev("document.getElementById('tksub').textContent");
say(pSub.indexOf("取樣") >= 0, "副標寫出「取樣」", pSub.slice(0, 60));
say(pSub.indexOf("逐秒") < 0, "副標**不可以**再寫「逐秒」", pSub.slice(0, 60));
say(pSub.indexOf("不是逐筆") >= 0, "副標明講「不是逐筆」");
const pPager = await ev("document.getElementById('tkpager').textContent");
say(pPager.indexOf("取樣") >= 0 && pPager.indexOf("逐筆 ") < 0,
  "翻頁列寫「取樣 N 筆」不是「逐筆 N 筆」", pPager.replace(/\s+/g, " ").slice(0, 50));
const pFoot = await ev("document.getElementById('tkfoot').textContent");
say(pFoot.indexOf("-polled.jsonl") >= 0, "頁尾寫的是取樣檔的檔名（不是逐筆那個）");

// ② 圖上的標籤：⛔ 要真的畫在畫面上 —— 負控組把那一行拿掉，那塊像素必須改變
const BADGE = `(()=>{const cv=document.getElementById('tkcv'),ctx=cv.getContext('2d');
  const dpr=TKC.DPR;                       // 取樣日 VH=0 ⇒ priceH 到 H-TKBOT
  const x=0, y=Math.max(0,Math.round((TKC.H-TKBOT-26)*dpr));
  const w=Math.min(cv.width,Math.round(250*dpr)), h=Math.round(24*dpr);
  const d=ctx.getImageData(x,y,w,h).data; let s=0; const set=new Set();
  for(let i=0;i<d.length;i+=4){ s+=d[i]+d[i+1]+d[i+2]; set.add(d[i]+','+d[i+1]+','+d[i+2]); }
  return {sum:s, colors:set.size};})()`;
await ev("tkDraw()");
const bWith = await ev(BADGE);
/* ⚠️ 舊版這裡是 `繪圖區左下顏色數 ≥3` —— **那條是裝飾性的**（lab-qa 2026-09-07 指出）：
   那一塊本來就有缺口斜線底紋，把金籤整行拿掉照樣 ≥3，它從來沒有承重過。
   （FM1 那次會紅的其實是 mutate() 自己的「目標字串已失效」自證，不是這條斷言。）
   改成量**圖上真的畫了哪些字**，那才是「這張籤畫在畫面上」的直接證據。 */
console.log(`  （參考）繪圖區左下的顏色數 = ${bWith.colors}（裝飾性資訊，不當斷言）`);
const pCanvas = await canvasSays();
say(pCanvas.indexOf("取樣") >= 0 && pCanvas.indexOf("不是逐筆") >= 0,
  "圖上真的畫出了取樣金籤的字（不是只在資料裡）", pCanvas.slice(0, 90));
/* ⛔ 取樣列只有**一個** price，開高低收四個值全部來自那一個取樣點 ⇒ 那個「高低」是
   取樣點的極值，不是那一秒真正摸到的高低。而秒 K 被選成預設的理由正是
   「K 棒多給的是這一段摸到多高多低」—— 他那份真檔實測 1 秒桶 **26.7% 高＝低**。
   這句話要在**圖上**（他早上盯的是圖），副標也有一份。 */
say(pCanvas.indexOf("高低") >= 0 && pCanvas.indexOf("取樣點") >= 0,
  "而且圖上講出「高低＝取樣點的極值」（⛔ 秒 K 的高低對取樣日不是那一秒真的高低）",
  pCanvas.slice(0, 90));
/* ⚠️ 這張籤的字每加一句就長一截 —— 量一次寬度，別讓下一個人把它加到畫出繪圖區外面。
   ⛔ 不准用「幾個字 × 字級」推算（手冊 D 段，這個專案為同一個病退件過兩次），
      用 ctx.measureText 量產品程式**真的要畫的那一串**。 */
const badgeW = await ev(`(()=>{const cv=document.getElementById('tkcv'),ctx=cv.getContext('2d');
  ctx.save(); ctx.font='11px ui-monospace,"Microsoft JhengHei",monospace';
  const w=ctx.measureText(TKPOLLBADGE()).width+14; ctx.restore();
  return {w:+w.toFixed(1), pw:+(TKC.W-TKR).toFixed(1), txt:TKPOLLBADGE()};})()`);
say(6 + badgeW.w <= badgeW.pw - 6, "金籤整串字畫得進繪圖區（左邊界 6px 起算）",
  `籤寬 ${badgeW.w}px、繪圖區寬 ${badgeW.pw}px`);
const BADGELINE = " if(tkIsPolled()) tkChip(ctx,6,TKTOP+priceH-22,TKPOLLBADGE(),'#E3A951');";
if (await mutate("tkDraw", BADGELINE, "")) {
  await ev("tkDraw()");
  const bNo = await ev(BADGE);
  say(bWith.sum !== bNo.sum,
    "負控組：把圖上的取樣標籤拿掉之後，那塊像素真的變了（＝標籤畫在畫面上，不是只在資料裡）",
    `${bWith.sum} → ${bNo.sum}`);
  const cNo = await canvasSays();
  say(cNo.indexOf("不是逐筆") < 0 && cNo.indexOf("取樣點") < 0,
    "負控組：拿掉那一行之後，圖上那幾個字真的不見了（＝上面兩條不是恆真的）",
    cNo.slice(0, 70));
  await unmutate("tkDraw");
  await ev("tkDraw()");
  chk("裝回去之後標籤又回來了", (await ev(BADGE)).sum, bWith.sum);
}

// ③ 日期清單：⛔ 兩種都標（只標一種的話，沒有標記等於「不知道」而不是「另一種」）
const LISTTAGS = `(()=>{ TK.pick=true; tkPaint();
  const out=[...document.querySelectorAll('.tk-list .row')].map(r=>{
    const k=r.querySelector('.kind');
    return {d:r.getAttribute('data-tkday'), tag:k?k.textContent:'',
            w:k?+k.getBoundingClientRect().width.toFixed(1):0}; });
  TK.pick=false; tkPaint(); return out; })()`;
const listTags = await ev(LISTTAGS);
ge("日期清單真的有列（尺的自證）", listTags.length, 5);
say(listTags.every(x => x.tag === "逐筆" || x.tag === "取樣"),
  "清單每一列都標了種類（逐筆／取樣兩種都標）",
  JSON.stringify(listTags.map(x => x.tag)));
say(listTags.every(x => x.w > 0), "而且那個標記真的看得見（有寬度，不是 display:none）");
chk("取樣那天標的是「取樣」", (listTags.find(x => x.d === D.polled) || {}).tag, "取樣");
chk("逐筆那天標的是「逐筆」", (listTags.find(x => x.d === D.full) || {}).tag, "逐筆");
if (await mutate("tkListHTML", "'</span>'+tag+", "'</span>'+")) {
  const noTags = await ev(LISTTAGS);
  say(noTags.every(x => x.tag === ""),
    "負控組：把清單的種類標記拿掉之後，這條會紅（標記不是恆真的裝飾）");
  await unmutate("tkListHTML");
  say((await ev(LISTTAGS)).every(x => x.tag), "裝回去之後標記又回來了");
}

/* ③b 清單的版面：⚠️ **不准用字級推算，實際量 getBoundingClientRect().height**
   （手冊 D 段，這個專案為同一個病退件過兩次）。
   「＋另有取樣檔（未採用）」那串字會把 .meta 擠到換行 ⇒ 那一列比別列高一截。 */
const listBox = await ev(`(()=>{ TK.pick=true; tkPaint();
  const rows=[...document.querySelectorAll('.tk-list .row')].map(r=>({
    d:r.getAttribute('data-tkday'),
    h:+r.getBoundingClientRect().height.toFixed(1),
    alt:!!r.querySelector('.alt')}));
  const foot=[...document.querySelectorAll('.tk-list .foot')].map(f=>f.textContent).join(' ');
  const sc=document.querySelector('.tk-list');
  const over=sc?+(sc.scrollWidth-sc.clientWidth).toFixed(1):0;
  TK.pick=false; tkPaint(); return {rows:rows, foot:foot, over:over};})()`);
ge("清單真的有列（尺的自證）", listBox.rows.length, 5);
say(listBox.rows.some(r => r.alt), "治具裡真的有「兩種檔都有」的那一列（尺的自證）",
  JSON.stringify(listBox.rows.filter(r => r.alt).map(r => r.d)));
chk("清單每一列的高度都一樣（⛔ 有 alt 標的那列不可以被撐高）",
  new Set(listBox.rows.map(r => r.h)).size, 1);
le("清單不會橫向溢出（px）", listBox.over, 0);
console.log(`  （參考）每列高度 ${listBox.rows[0].h}px`);

/* ③c 被跳過的檔**在有資料的時候也要講**（lab-qa 2026-09-07 退件 M1 的一部分）。
   舊版只寫在「一個紀錄都沒有」的空狀態裡 ⇒ 只要有任何一天有資料，
   「我明明有錄怎麼看不到」就完全無解。治具裡的 decoy（逐筆檔名 ＋ 取樣內容）就是那種檔。 */
ge("治具真的有被跳過的檔（尺的自證）", (await ev("TK.skipped.length")), 1);
say(listBox.foot.indexOf("被跳過") >= 0,
  "清單上一直看得到「有 N 個檔被跳過」（不是只有空狀態才講）",
  listBox.foot.replace(/\s+/g, " ").slice(0, 80));
if (await mutate("tkListHTML", "const skip=(TK.skipped||[]).length",
                 "const skip=''&&(TK.skipped||[]).length")) {
  const noFoot = await ev(`(()=>{ TK.pick=true; tkPaint();
    const t=[...document.querySelectorAll('.tk-list .foot')].map(f=>f.textContent).join(' ');
    TK.pick=false; tkPaint(); return t;})()`);
  say(noFoot.indexOf("被跳過") < 0,
    "負控組：拿掉那一句之後，被跳過的檔又完全沒地方講了（這條會紅）");
  await unmutate("tkListHTML");
}

// ④ 成交量：disabled ＋ 寫出原因，而且真的一根量柱都沒有
const pVol = await ev(`(()=>{const b=document.querySelector('[data-tkov="vol"]');
  return {dis:!!b.disabled, on:b.className.indexOf('on')>=0,
          title:b.getAttribute('title')||''};})()`);
chk("取樣日的成交量疊圖是 disabled", pVol.dis, true);
chk("而且不是亮著的", pVol.on, false);
say(pVol.title.indexOf("沒有單筆成交量") >= 0, "而且寫出了原因", pVol.title);
const pVolPix = await ev(VOLTOGGLE);
chk("取樣日：開關成交量畫出來一模一樣（＝真的一根量柱都沒有）",
  pVolPix.on === pVolPix.off, true);
const pRead = await ev(`(()=>{TK.hover=400;tkPaint();tkPaint();
  const t=document.getElementById('tkread').textContent;
  TK.hover=null;TK.hi=null;tkPaint(); return t;})()`);
say(pRead.indexOf("取樣檔沒有量") >= 0,
  "讀值列的「量」寫「—」＋原因（⛔ 不印 0 假裝那一秒沒成交）", pRead.slice(-40));

// ⑤ 價帶：取樣檔**有** bid/ask ⇒「折線＋價帶」那個圖種對取樣日照樣能用
const pBand = await ev(`(()=>{const D=TK.data; let n=0,ok=0;
  for(let i=0;i<D.len;i++) if(isFinite(D.bl[i])&&isFinite(D.ah[i])){
    n++; if(D.ah[i]>=D.bl[i]) ok++; }
  return {len:D.len,n:n,ok:ok};})()`);
ge("取樣日有買賣價帶的桶數佔比（%）", Math.round(pBand.n / pBand.len * 100), 90);
chk("有價帶的桶都是 最高賣價 ≥ 最低買價", pBand.ok, pBand.n);
const pBandPix = await ev(`(()=>{
  const cv=document.getElementById('tkcv'), ctx=cv.getContext('2d');
  const grab=()=>{const d=ctx.getImageData(0,0,cv.width,cv.height).data;
    let s=0; for(let i=0;i<d.length;i+=4) s+=d[i]+d[i+1]+d[i+2]; return s;};
  const v0=TK.v, view0=TK.view;
  TK.v='A'; TK.view=null; TKAXIS.key=null; tkDraw(); const a=grab();
  TK.v='B'; TKAXIS.key=null; tkDraw(); const b=grab();
  TK.v=v0; TK.view=view0; TKAXIS.key=null; tkPaint();
  return {a:a,b:b};})()`);
say(pBandPix.a !== pBandPix.b,
  "取樣日：折線+價帶 跟純折線畫出來不一樣（bid/ask 真的被讀進去也畫上去）",
  `A=${pBandPix.a} B=${pBandPix.b}`);

// ⑥ auto 桶寬：取樣約 0.46 秒一筆 ⇒ 1 秒桶一半只有 1 個點，auto 的下限拉到 2 秒
const pBar60 = await ev(`(()=>{const v0=TK.view; TK.view={t0:0,t1:60};
  const w=tkBarSec(); TK.view=v0; TKAXIS.key=null; return w;})()`);
chk("逐筆日：視窗 60 秒時 auto 桶寬（對照組）", tickBar60, 1);
chk("取樣日：視窗 60 秒時 auto 桶寬（下限 2 秒）", pBar60, 2);
chk("手動按 1 秒還是給他 1 秒（⛔ 只動 auto，不動他自己選的）",
  await ev("(()=>{const b0=TK.bar; TK.bar=1; const w=tkBarSec(); TK.bar=b0; return w;})()"), 1);

// ⑦ 缺口的那句話：取樣日不可以說「不是沒行情」（有變才記，空白有兩種可能）
const pGapTxt = await ev("document.getElementById('tksub').textContent");
say(pGapTxt.indexOf("沒有取樣到") >= 0, "開頭沒錄到的那段：寫「沒有取樣到」",
  pGapTxt.slice(0, 70));
say(pGapTxt.indexOf("不是沒行情") < 0,
  "⛔ 取樣日不可以寫「不是沒行情」（取樣工具只在有變動時才記一列，證不了這句）");
/* ⛔⛔ **同一條紅線在 canvas 上也要守**（lab-qa 2026-09-07 退件 M2）：
   圖上缺口中間那塊底色壓著同一句話，而**他早上盯的是圖不是副標**。
   上面那兩條只掃 `#tksub` ⇒ QA 把 canvas 的 MISS 常數改回「不是沒行情」，159/159 全綠。 */
const pGapCanvas = await canvasSays();
say(pGapCanvas.indexOf("沒有取樣到") >= 0,
  "圖上缺的那段也寫「沒有取樣到」（不是只有副標）", pGapCanvas.slice(0, 90));
say(pGapCanvas.indexOf("不是沒行情") < 0,
  "⛔⛔ 圖上**絕對不可以**出現「不是沒行情」（那是取樣日證不了的話）",
  pGapCanvas.slice(0, 90));
// 負控組（就是 lab-qa 那個突變）：把 MISS 併回逐筆那一句
const MISSLINE = "tkIsPolled()?'這段沒有取樣到（沒錄到，或報價一直沒變）'";
if (await mutate("tkDraw", MISSLINE, "false?'這段沒有取樣到（沒錄到，或報價一直沒變）'")) {
  const mNo = await canvasSays();
  say(mNo.indexOf("不是沒行情") >= 0 && mNo.indexOf("沒有取樣到") < 0,
    "FM5 負控組：MISS 併回單一句之後，取樣日的圖上真的冒出「不是沒行情」（這條會紅）",
    mNo.slice(0, 90));
  await unmutate("tkDraw");
  const mBack = await canvasSays();
  say(mBack.indexOf("不是沒行情") < 0 && mBack.indexOf("沒有取樣到") >= 0,
    "裝回去之後圖上又只寫「沒有取樣到」");
}

/* ⑦b 「加權」那顆的停用理由**必須分兩種寫**（lab-qa 2026-09-07 退件 M2）：
   逐筆檔真的沒有 `idx` 這一欄，**取樣檔有**（tick_recorder.py 有記）——
   寫同一句就一定有一句是假的。這條原本零守衛：QA 把它併回單一句，159/159 全綠。 */
const pIdxTip = await ev(`(()=>{const b=document.querySelector('[data-tkov="idx"]');
  return {dis:!!b.disabled, title:b.getAttribute('title')||''};})()`);
chk("對照組（逐筆日）：加權那顆是 disabled", tickIdxTip.dis, true);
chk("取樣日：加權那顆一樣是 disabled（這一輪仍然不畫那條線）", pIdxTip.dis, true);
say(tickIdxTip.title.indexOf("逐筆紀錄裡沒有加權指數") >= 0,
  "逐筆日的理由：「逐筆紀錄裡沒有加權指數」", tickIdxTip.title);
say(pIdxTip.title.indexOf("取樣檔裡有加權指數") >= 0,
  "取樣日的理由：「取樣檔裡有加權指數，但這一頁沒有做這條線」", pIdxTip.title);
say(pIdxTip.title !== tickIdxTip.title,
  "⛔ 兩種日子的理由必須不一樣（寫同一句就一定有一句是假的）");
const IDXLINE = "tkIsPolled()?'取樣檔裡有加權指數，但這一頁沒有做這條線'";
if (await mutate("tkToolsHTML", IDXLINE,
                 "false?'取樣檔裡有加權指數，但這一頁沒有做這條線'")) {
  const nTip = await ev(`(()=>{tkPaint();
    const b=document.querySelector('[data-tkov="idx"]');
    return b.getAttribute('title')||'';})()`);
  say(nTip === tickIdxTip.title,
    "FM6 負控組：併回單一句之後，取樣日被寫上逐筆那句假話（這條會紅）", nTip);
  await unmutate("tkToolsHTML");
  const bTip = await ev(`(()=>{tkPaint();
    const b=document.querySelector('[data-tkov="idx"]');
    return b.getAttribute('title')||'';})()`);
  say(bTip !== tickIdxTip.title, "裝回去之後兩種日子的理由又分開了", bTip);
}

/* ⑦c 副標的取樣密度是**量出來的**，不是寫死的（後端 ms_med 那條的前端這一半）。
   ⚠️ 順帶守住 tkRate() 的去尾零：舊版 `.replace(/0$/,'')` 只去掉一個 0 ⇒
      ms_med=1000 印成「約 1.0 秒一筆」（lab-qa 抓到）。 */
const rateCases = await ev(`(()=>{const m0=TK.data.ms_med, out={};
  for(const m of [1000,500,460,250]){ TK.data.ms_med=m; out[m]=tkRate(); }
  TK.data.ms_med=m0; tkPaint(); return out;})()`);
chk("ms_med=1000 ⇒「約 1 秒一筆」（⛔ 不是「約 1.0 秒一筆」）", rateCases["1000"], "約 1 秒一筆");
chk("ms_med=500 ⇒「約 0.5 秒一筆」", rateCases["500"], "約 0.5 秒一筆");
chk("ms_med=460 ⇒「約 0.46 秒一筆」", rateCases["460"], "約 0.46 秒一筆");
chk("ms_med=250 ⇒「約 0.25 秒一筆」", rateCases["250"], "約 0.25 秒一筆");

/* ⑦d 09:30 之後被切掉的列要講出來（後端 outwin 那條的前端這一半）。
   ⛔ 「安靜地少」是這個專案明令禁止的失敗模式 —— 少畫一段就要有一個數字。 */
const owSub = await ev(`(()=>{const o0=TK.data.outwin; TK.data.outwin=7649; tkPaint();
  const t=document.getElementById('tksub').textContent;
  TK.data.outwin=o0; tkPaint(); return t;})()`);
say(owSub.indexOf("7,649") >= 0 && owSub.indexOf("08:45~09:30 之外") >= 0,
  "取樣檔錄到 09:30 之後時，副標寫出被切掉幾列", owSub.slice(-60));
if (await mutate("tkSubHTML", " if(D.outwin) out+=", " if(false) out+=")) {
  const owNo = await ev(`(()=>{const o0=TK.data.outwin; TK.data.outwin=7649; tkPaint();
    const t=document.getElementById('tksub').textContent;
    TK.data.outwin=o0; tkPaint(); return t;})()`);
  say(owNo.indexOf("7,649") < 0,
    "負控組：拿掉那一句之後，被切掉的 7,649 列就安靜地不見了（這條會紅）");
  await unmutate("tkSubHTML");
}

// ⑧ 紅線：取樣日的畫面同樣不得出現預測字眼
chk("禁詞命中（取樣日）", await scanHits(), []);

// ⑨ 負控組：把種類判斷反過來 ⇒ 逐筆日被標成取樣，必須紅
await goDay(D.full);
if (await mutate("tkKind", "?D.kind:tkKindOf(TK.date)", "?'polled':'polled'")) {
  await ev("tkPaint()");
  const s2 = await ev("document.getElementById('tksub').textContent");
  say(s2.indexOf("取樣") >= 0 && s2.indexOf("逐秒") < 0,
    "負控組：種類判斷反過來之後，逐筆日被標成取樣（這條會紅）", s2.slice(0, 46));
  await unmutate("tkKind");
  await ev("tkPaint()");
  const s3 = await ev("document.getElementById('tksub').textContent");
  say(s3.indexOf("逐秒") >= 0 && s3.indexOf("取樣") < 0,
    "裝回去之後逐筆日又寫「逐秒」（⛔ 逐筆日不可以被標成取樣）", s3.slice(0, 46));
}

/* ═══ ⑩ 我的單／±100 不撐大價格軸、null 不會把軸拉到 0 ══════════════ */
console.log("\n=== ⑩ 我的單與 ±100（含 exit=null 的陷阱）===");
await goDay(D.full);
const tr = await ev("JSON.stringify(TK.data.trades)");
const TR = JSON.parse(tr);
ge("今天有交易可以標", TR.length, 1);
say(TR.some(t => t.exit === null), "治具裡有一筆 exit=null 的真實單（那正是 2026-09-02 的坑）");
const axis = await ev("JSON.stringify(TKAXIS)");
const AX = JSON.parse(axis);
ge("價格軸下緣沒有掉到 0（exit=null 不可以被當成 0）", AX.lo, 10000);
le("價格軸的跨度沒有被 ±100 撐大", AX.hi - AX.lo, 260);
say(AX.hi - AX.lo >= 20, "價格軸的跨度沒有塌掉 = " + (AX.hi - AX.lo));
/* 負控組：把 tkAxis 裡的 null 過濾換回 2026-09-02 那個寫法（`Math.min(lo,entry,exit)`），
   價格軸必須整個掉到 0。
   ⚠️ 舊版這裡是探針**就地**算 `Math.min(entry,null)` 給你看 —— 那證明的是
      **JS 的語言性質**（null 會被當成 0），跟產品程式有沒有守住一點關係都沒有
      （lab-qa 2026-09-07 指出）。現在改成真的去動產品函式。 */
if (await mutate("tkAxis", "const a=tkN(t.entry), b=tkN(t.exit);",
  "const a=Math.min(t.entry,t.exit), b=Math.max(t.entry,t.exit);")) {
  const axBad = await ev(`(()=>{TKAXIS.key=null; tkDraw(); return TKAXIS.lo;})()`);
  say(axBad < 1000, "負控組：換回不過濾 null 的舊算式之後，價格軸整個掉到 0",
    `軸下緣 = ${axBad}`);
  await unmutate("tkAxis");
  const axOk = await ev(`(()=>{TKAXIS.key=null; tkDraw(); return TKAXIS.lo;})()`);
  ge("裝回去之後價格軸又回到行情的區間", axOk, 10000);
}

/* ═══ ⑪ 版面（§12-9）══════════════════════════════════════════════ */
console.log("\n=== ⑪ 版面 ===");
const shrink = await ev(`(()=>{
  const out=[];
  document.querySelectorAll('#tab-tick *').forEach(e=>{
    const cs=getComputedStyle(e);
    if(cs.display!=='flex'||cs.flexDirection!=='column') return;
    if(cs.maxHeight==='none') return;
    [...e.children].forEach(k=>{
      const s=getComputedStyle(k).flexShrink;
      if(s!=='0') out.push({parent:e.className,child:k.className,shrink:s});
    });
  });
  return out; })()`);
chk("有 max-height 的 flex 直欄，子元素 flex-shrink 全部是 0", shrink, []);
// 骨架與真圖的高度
const hReal = (await box("#tkwrap")).h;
const hSkel = await ev(`(()=>{TK.pending=true;tkPaint();
  const h=document.getElementById('tkwrap').getBoundingClientRect().height;
  TK.pending=false;tkPaint(); return h;})()`);
le("骨架與真圖的高度差（px）", +Math.abs(hReal - hSkel).toFixed(3), 0.5);
say(await ev(`(()=>{TK.pending=true;tkPaint();
  const s=document.querySelector('#tkover .tk-skel');
  const ok=!!s&&getComputedStyle(s).position==='absolute';
  TK.pending=false;tkPaint(); return ok;})()`),
  "骨架的假 K 棒是絕對定位（不會反過來把容器撐高）");
for (const w of [1024, 1280, 1440]) {
  await c.send("Emulation.setDeviceMetricsOverride",
    { width: w, height: 1000, deviceScaleFactor: 1, mobile: false });
  await sleep(250); await ev("tkPaint()");
  const r = await ev(`(()=>{
    const t=document.getElementById('tktools'), wr=document.getElementById('tkwrap');
    const rt=t.getBoundingClientRect(), rw=wr.getBoundingClientRect();
    return {overflow:+(t.scrollWidth-t.clientWidth), ratio:+(rw.width/rw.height).toFixed(3),
            bodyOverflow:+(document.documentElement.scrollWidth-document.documentElement.clientWidth)};
  })()`);
  le(`視窗 ${w}px：.tk-tools 溢出`, r.overflow, 0);
  le(`視窗 ${w}px：整頁橫向溢出`, r.bodyOverflow, 0);
  le(`視窗 ${w}px：canvas 長寬比偏差`, +Math.abs(r.ratio - 1040 / 470).toFixed(3), 0.02);
}
await c.send("Emulation.clearDeviceMetricsOverride");
await sleep(200);

/* ═══ ⑫ 增量輪詢期間 `/api/state` 的回應時間 ＋ 今天的增量合併 ═══════════
   ⚠️ **這一節的名字原本叫「不影響即時」，那是誇大的**（lab-qa 2026-09-07 指出）：
      治具 `fe_harness.py` 只有 HTTP 服務，**根本沒有面板那條 4Hz 主迴圈**
      （停損就活在那條迴圈裡）⇒ 這支探針**結構上量不到**「會不會拖慢他的停損」。
      現在照它真正在量的東西命名：**增量輪詢期間 `/api/state` 的回應時間**。
   為什麼不在治具裡補一條假主迴圈：那條迴圈會是治具自己的，不是產品的
      （產品那條在 `live_panel.main()` 裡、沒辦法單獨拉出來跑）⇒ 量到的是
      「我寫的假迴圈被 HTTP 執行緒卡多久」，卻會**看起來像**在守他的停損 ——
      那比沒有這條測試更危險。主迴圈的實測數字（冷解析期間停損檢查週期
      276 → 970 ms、對照組 `/api/bars` 換日 1732 ms）用獨立治具量過一次，
      寫在 CLAUDE.md 的「面板開發鐵律」裡，那是這支面板**既有的架構性質**。
   ⚠️ 中位數會蓋掉尾巴：QA 量到冷解析期間 `/api/state` 的 **max 758 ms**，
      所以這裡把 max 一起印出來、也一起設上界。 */
console.log(`\n=== ⑫ 增量輪詢期間 /api/state 的回應時間（停留 ${SOAK} 秒）＋ 增量合併 ===`);
const stateMs = async (n) => await ev(`(async()=>{
  const t=[];
  for(let i=0;i<${n};i++){ const a=performance.now();
    await fetch('/api/state').then(r=>r.json()); t.push(performance.now()-a); }
  t.sort((a,b)=>a-b);
  return {median:+t[t.length>>1].toFixed(2), max:+t[t.length-1].toFixed(2)}; })()`);
await ev("setTab('live')"); await sleep(1200);
const before = await stateMs(15);
// 把後端的「現在」固定在 09:12 ⇒ 今天那份 complete=false ⇒ 2 秒輪詢真的會跑；
// 同時把今天那份縮成只錄到 09:12，之後 grow 才有地方往後長（不然只能長到 09:30 之外）
await ctl("/tick/clock/09:12");
await ctl("/tick/short");
// ⚠️ 一定要把 TK.data 也清掉：只清 TK.cache 的話 goTick() 會看到「舊那份還在、pending=false」
//    立刻回來，量到的是切過去之前那一份（第一版就這樣量到 complete=true 的假紅燈）
await ev("clearTimeout(TK.timer); TK.timer=null; TK.cache={}; TK.data=null;");
say(await goTick(), "重新載入今天那份（錄到 09:12 為止）");
const len0 = await ev("TK.data.len");
chk("錄製中（complete=false）", await ev("TK.data.complete"), false);
/* 數「增量請求真的送了幾次」。⚠️ `tkPoll` 的節奏是 **2 秒**，停留 60 秒 ⇒ 應該是 ~29 次；
   前一位 dev 的交付報告寫「12 次」對不上（那個數字沒有寫進任何檔案，只在報告裡）。
   量出來寫在這裡，下一個人就不必再用猜的。 */
await ev(`(()=>{ window.__incr=0;
  if(!window.__fetch0){ window.__fetch0=window.fetch;
    window.fetch=function(u){ if(typeof u==='string'&&u.indexOf('from=')>=0) window.__incr++;
      return window.__fetch0.apply(this,arguments); }; }
  return 1; })()`);
const t0 = Date.now(); let grew = 0;
while ((Date.now() - t0) / 1000 < SOAK) {
  await sleep(5000);
  await ctl("/tick/grow/400"); grew++;
  if (grew === 2) await ctl("/tick/half");     // 故意留一個半列，解析器必須丟掉
}
await sleep(3000);
const soakSec = (Date.now() - t0) / 1000;
const incrN = await ev("window.__incr");
await ev(`(()=>{ if(window.__fetch0){window.fetch=window.__fetch0; window.__fetch0=null;} return 1;})()`);
ge(`增量輪詢次數（${soakSec.toFixed(0)} 秒 ÷ 2 秒 ⇒ 期待 ~${Math.round(soakSec / 2)} 次）`,
  incrN, Math.round(soakSec / 2) * 0.6);
const after = await stateMs(15);
const len1 = await ev("TK.data.len");
const uniq = await ev(`(()=>{const s=new Set();
  for(let i=0;i<TK.data.len;i++) s.add(TK.data.s[i]); return s.size;})()`);
ge("輪詢期間確實有新桶進來（尺的自證）", len1 - len0, 1);
chk("增量合併之後沒有重複的秒（同 s 覆蓋、不是 concat）", uniq, len1);
chk("s 仍然是遞增的", await ev(`(()=>{for(let i=1;i<TK.data.len;i++)
  if(TK.data.s[i]<=TK.data.s[i-1]) return false; return true;})()`), true);
console.log(`  /api/state 回應：切過去之前 中位 ${before.median} ms / max ${before.max} ms、` +
  `停留 ${SOAK} 秒之後 中位 ${after.median} ms / max ${after.max} ms`);
le("停留後 /api/state 中位回應不可以超過之前的 2 倍", after.median,
  Math.max(before.median * 2, before.median + 5));
/* max 的上界刻意訂得寬（1.5 秒）：這支面板是**單行程**，重活（解析一天逐筆、
   `/api/bars` 換日）跑在 HTTP 執行緒上，尖峰本來就會被拉高（QA 冷解析期間量到 758 ms）。
   這條不是在保證「很快」，是在擋「從幾百毫秒變成好幾秒」那種等級的退步。 */
le("停留後 /api/state 的 max 不可以掉到秒級", after.max, 1500);
await ctl("/tick/clock/");

/* ═══ 收尾 ══════════════════════════════════════════════════════════ */
console.log("\n=== 收尾 ===");
// ⚠️「零錯誤」要放在跑完之後驗，不是只驗第一節（只印不 FAIL++ 的話會被安靜放行）
chk("全程 console／exception 零錯誤", ERRORS.slice(0, 5), []);
await ev("setTab('live')"); await sleep(800);
chk("切回【即時】之後 K 線圖還在", await ev(`!!document.querySelector('#tab-live svg, #tab-live .cwrap')`), true);
chk("切回【即時】之後【細節】的 2 秒輪詢已經停掉", await ev("TK.timer===null"), true);

console.log("\n──── 實測數字 ────");
/* ⚠️ 這幾個標籤 2026-09-07 改過（lab-qa 指出舊標籤會誤導下一個人）：
   ① 拖曳量到的是**事件處理**時間，真正拿來比 5 倍的是**每格畫面**（見 § ② 的註解）
      —— 舊版只印 `負控組不分桶每步中位ms`，看的人會以為負控組只慢一點點。
   ② 舊版的 `負控組價格軸改變次數` 印的是 `ax2`，而 ax2 是**拿掉遲滯**那一組
      （它本來就該跟正常組一模一樣）。真正的負控組是**拿掉 niceStep 貼齊**那一組。 */
console.log(JSON.stringify({
  秒K拖曳每步事件處理中位ms: perf.barsDrag.median,
  折線價帶拖曳每步事件處理中位ms: perf.lineDrag.median,
  負控組不分桶每步事件處理中位ms: perf.rawDrag.median,
  每格畫面ms_秒K: perf.barsFrame.median,
  每格畫面ms_折線價帶: perf.lineFrame.median,
  每格畫面ms_負控組不分桶: perf.rawFrame.median,
  負控組慢幾倍_每格畫面: +(perf.rawFrame.median / perf.lineFrame.median).toFixed(1),
  單次繪製中位ms: single,
  價格軸改變次數: ax.changes,
  負控組價格軸改變次數_拿掉niceStep: axNiceOff,
  拿掉遲滯的改變次數_應與正常組相同: ax2.changes,
  maxColDiff: cd.maxColDiff, 比對欄數: cd.cols, 比對點數: cd.pts,
  state中位ms前: before.median, state最大ms前: before.max,
  state中位ms後: after.median, state最大ms後: after.max,
  增量輪詢次數: incrN, 價帶桶數: band.n,
}, null, 0));
console.log(`\n總結：${N - FAIL}/${N} 通過`);
if (ERRORS.length) console.log("console 錯誤：", ERRORS.slice(0, 8));
c.close(); ch.kill();
process.exit(FAIL ? 1 : 0);
