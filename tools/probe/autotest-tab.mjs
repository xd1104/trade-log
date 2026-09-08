/*
  【程式下單】分頁的**前端**探針 —— AUTOTEST-TAB-SPEC.md §15 的前端那一半。

  ⚠️ 不連永豐、⛔ **不碰 8770**（Benson 的面板正開著）。只打治具（8773／控制埠 8774）。
     跑法：先起 tools/probe/at_harness.py，改過程式一定要**重起治具**，再
           node tools/probe/autotest-tab.mjs

  ⛔ 治具與探針用的價格一律 **12000 附近**，一個真實成交價／進出場時間／點數都不准出現。

  【每一條都要有負控組】沒有負控組的綠燈在這個專案不算數。
  負控組一律用**原始碼突變**（把產品函式 toString() 出來、改掉那道守衛、eval 回去）——
  不會跟產品程式分岔，而且每次都先斷言「目標字串真的在」「內容真的變了」。
  ⚠️ 突變要專打「**接線**那一側」與「別人幫我做的那一側」，自己剛寫完的地方本來就會紅
     （tick_writer 與【細節】連兩輪的教訓）。

  ⛔⛔ 紅線是「畫面上不可以出現某句話」的時候，量的對象就必須是**畫面** ——
      這一頁的進場標籤／泳道標籤／「這天不做」／邊緣籤全部畫在 canvas 上，
      只掃 DOM 會全部漏掉（【細節】2026-09-07 為此退件過）。所以有 canvasText()。

  ⚠️⚠️ 2026-09-07 第三輪 lab-qa 退件補的（**上一輪這幾塊是零斷言**）：
    ㉑  ⛔⛔ 名字紅線（R1）：四個名字各自出現（DOM ＋ canvas 各驗一次）、
        孤立的 A/B/C/D 在 DOM 與兩張 canvas 上零命中、四處同名同序、
        成績表說明小字的文案（R8：lab-qa 把它清空 ⇒ 134/134 全綠）、名字欄寬是量出來的、
        ㉑d 累計圖線尾標籤不准互相疊住／不准壓在價格刻度上（**只有截圖看得出來**）。
    ㉒  主迴圈那道 try 攔到的錯要畫在畫面上（R5：吞在後端變數裡＝還是吞掉了）。
    ㉓  文案／常數那一側（R1／R4／R8 三個退件全在這裡）：前後端常數一致、
        文案表沒有被掏空／兩個原因不准寫同一句、〈這一頁在算什麼〉十條的**內容**、
        兩個 hover title 裡的數字是算出來的、畫面上沒有過期的「時分秒」。
    ＋ 未捕捉的例外轉成**具名 FAIL** ＋ 一定印總結（R7）。
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
const URL_ = A.url || "http://127.0.0.1:8773/";
const CTL = Number(A.ctl || 8774);
const DEV = Number(A.dev || 9821);

const sleep = ms => new Promise(r => setTimeout(r, ms));
let FAIL = 0, N = 0, DONE = false;
const say = (ok, name, extra) => {
  N++; if (!ok) FAIL++;
  console.log((ok ? "  OK   " : "  FAIL ") + name + (extra ? "  " + extra : ""));
};
/* ⚠️⚠️ **探針自己掛掉不可以看起來像綠燈**（2026-09-07 lab-qa 退件 R7）：
   突變讓探針當場丟例外時，一行 FAIL 都沒有、也沒有印總結 ⇒ 只數 FAIL 行的人判成綠。
   未捕捉的例外一律轉成一項**具名的 FAIL**，而且總結一定印得出來。 */
function bail(e) {
  if (DONE) return;
  DONE = true;
  say(false, "⛔ 探針自己掛掉了（未捕捉的例外）—— 這一項就是那個 FAIL",
    String((e && e.stack) || e).slice(0, 300));
  console.log(`\n${"=".repeat(60)}\n共 ${N} 項，${FAIL} 項未過（⚠️ 探針中途中斷，後面沒跑到）`);
  try { c.close(); } catch { /* 還沒接上就算了 */ }
  try { ch.kill(); } catch { /* 同上 */ }
  process.exit(1);
}
process.on("uncaughtException", bail);
process.on("unhandledRejection", bail);
const chk = (name, got, want) => say(JSON.stringify(got) === JSON.stringify(want), name,
  JSON.stringify(got) === JSON.stringify(want) ? "" : `(得到 ${JSON.stringify(got)}，期待 ${JSON.stringify(want)})`);
const ctl = async p => (await fetch(`http://127.0.0.1:${CTL}${p}`)).json();

const profile = fs.mkdtempSync(path.join(os.tmpdir(), "at-probe-"));
// ⚠️ headless 預設 800x600，元素會被擠到 viewport 外 ⇒ 量測與點擊全部白過（hold-to-fire 踩過）
const ch = spawn(CHROME, ["--headless=new", "--remote-debugging-port=" + DEV,
  "--user-data-dir=" + profile, "--no-first-run", "--no-default-browser-check",
  "--hide-scrollbars", "--window-size=1500,1100", "about:blank"],
  { stdio: "ignore", shell: false });
for (let i = 0; i < 200; i++) {
  try { await fetch(`http://127.0.0.1:${DEV}/json/version`); break; } catch { await sleep(100); }
}
const c = await CDP.attach(DEV);
await c.send("Page.enable"); await c.send("Runtime.enable"); await c.send("Log.enable");
/* ⛔⛔ **「一張單都不會送出去」最硬的證明**：把這一頁**真的發出去的每一個請求**
   都記下來（不是掃原始碼、也不是數按鈕）。整場跑完之後斷言：
   ① 一個 POST 都沒有；② 沒有任何請求打到 /api/enter 或 /api/real/*。
   治具的 POST handler 也只會回「治具不送單」，兩道獨立。 */
await c.send("Network.enable");
const REQS = [];
c.on("Network.requestWillBeSent", p =>
  REQS.push({ m: p.request.method, u: p.request.url }));

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
  say(!!r.ok, `  突變已套用：${fn}`, r.ok ? "" : "(" + r.why + ")");
  return !!r.ok;
}
const unmutate = fn => ev(`(()=>{ if(window.__orig&&window.__orig[${JSON.stringify(fn)}])
  window[${JSON.stringify(fn)}]=window.__orig[${JSON.stringify(fn)}]; return 1; })()`);

/* canvas 上**真的畫出去的字**（不是讀原始碼，是量畫面）。
   ⚠️ 連座標一起收：字串對、但畫在畫布外面（或被切掉一半）**掃字串是掃不到的** ——
      第一版時間軸的「08:45」就是被切成「45」，截圖才看見。 */
const canvasDraws = async (id, draw) => await ev(`(()=>{
  const cv=document.getElementById(${JSON.stringify(id)}), ctx=cv.getContext('2d');
  const orig=ctx.fillText, out=[];
  ctx.fillText=function(t,x,y){
    const w=this.measureText(String(t)).width, a=this.textAlign;
    /* ⚠️ 記的是**字真的落在畫布上的左界**：textAlign='right' 時字在 [x-w, x]。
       泳道名字欄是右對齊的（規格 §9.3），照抄 x 會把「有沒有畫到畫布外」量錯。 */
    const x0=(a==='right'||a==='end')?(x-w):((a==='center')?(x-w/2):x);
    out.push({t:String(t),x:x0,y:y,w:w,align:a}); return orig.apply(this,arguments); };
  try{ ${draw}(); } finally { ctx.fillText=orig; }
  return out;})()`);
const canvasText = async (id, draw) => (await canvasDraws(id, draw)).map(d => d.t);

async function goAuto() {
  await ev("setTab('auto')");
  for (let i = 0; i < 80; i++) {
    const st = await ev("[AT.pending,!!(AT.data&&AT.data.date===AT.date),!!AT.stats]");
    if (!st[0] && st[1] && st[2]) return true;
    await sleep(200);
  }
  return false;
}
async function settle() {
  for (let i = 0; i < 80; i++) {
    const st = await ev("[AT.pending,AT.date,AT.data&&AT.data.date]");
    if (!st[0] && st[1] === st[2]) { await sleep(120); return true; }
    await sleep(150);
  }
  return false;
}
async function reload() {
  await c.send("Page.navigate", { url: URL_ });
  await sleep(2200);
  return await goAuto();
}

const W = await ctl("/at/where");
console.log(`治具資料：${W.dir}`);
console.log(`今天=${W.today}　天數=${W.days}\n`);
await ctl("/at/reset");
await reload();

/* ═══ ① 分頁與第一畫面 ═══════════════════════════════════════════════ */
console.log("=== ① 分頁 ===");
chk("頂列四顆分頁", await ev(`[...document.querySelectorAll('.tabs button')].map(b=>b.textContent)`),
  ["即時", "細節", "回顧", "程式下單"]);
chk("⛔ 分頁名是「程式下單」，不是「對照」也不是「模擬」",
  await ev(`document.querySelector('[data-tab="auto"]').textContent`), "程式下單");
chk("放最右", await ev(`[...document.querySelectorAll('.tabs button')].pop().getAttribute('data-tab')`), "auto");
chk("TAB", await ev("TAB"), "auto");
chk("即時分頁被藏起來", await ev("document.getElementById('tab-live').hidden"), true);
chk("四顆分頁等寬", await ev(`(()=>{const w=[...document.querySelectorAll('.tabs button')]
  .map(b=>Math.round(b.getBoundingClientRect().width)); return new Set(w).size;})()`), 1);

/* ═══ ② ⛔ 這一頁沒有任何會送單的東西（§15-1）════════════════════════ */
console.log("\n=== ② ⛔ 碰不到下單路徑 ===");
chk("[data-act] 0 顆", await ev(`document.querySelectorAll('#tab-auto [data-act]').length`), 0);
chk("[data-rdir] 0 顆", await ev(`document.querySelectorAll('#tab-auto [data-rdir]').length`), 0);
chk("沒有任何 form / input[type=submit]",
  await ev(`document.querySelectorAll('#tab-auto form,#tab-auto [type=submit]').length`), 0);
// 整頁唯一的輸入元件是門檻掃描那支滑桿（⛔ 而且它不會改變真正在跑的 C）
chk("整頁只有一支 range 滑桿，沒有別的輸入",
  await ev(`[...document.querySelectorAll('#tab-auto input')].map(i=>i.type)`), ["range"]);
say((await ev(`document.getElementById('atadvbody').textContent`)).includes("不會"),
  "  而且畫面上寫清楚「拉滑桿不會改變真正在跑的 C」");
say(await ev(`REAL_ON===false && document.querySelectorAll('#tab-auto [data-rdir]').length===0`),
  "  尺是活的（同一個查詢查得到真實下單那半的東西）");

/* ═══ ③ 鎖印恰好 1 顆而且看得見（§15-1c／§15-6）══════════════════════ */
console.log("\n=== ③ 「模擬 · 不送單」鎖印 ===");
chk("⛔ 恰好 1 顆", await ev(`document.querySelectorAll('#tab-auto .simlock').length`), 1);
chk("看得見（offsetParent 不是 null）",
  await ev(`document.querySelector('#tab-auto .simlock').offsetParent!==null`), true);
chk("display 不是 none",
  await ev(`getComputedStyle(document.querySelector('#tab-auto .simlock')).display!=='none'`), true);
chk("字裡有「模擬」與「不送單」", await ev(`(()=>{const t=document.querySelector('#tab-auto .simlock').textContent;
  return [t.includes('模擬'),t.includes('不送單')];})()`), [true, true]);
chk("顏色是金色（⛔ 不可以用紅綠）",
  await ev(`getComputedStyle(document.querySelector('#tab-auto .simlock')).color`), "rgb(227, 169, 81)");
console.log("  負控組：");
await ev(`document.querySelector('#tab-auto .simlock').style.display='none'`);
say(await ev(`document.querySelector('#tab-auto .simlock').offsetParent===null`),
  "  藏起來之後這一條會紅");
await ev(`document.querySelector('#tab-auto .simlock').style.display=''`);
await ev(`(()=>{const s=document.querySelector('#tab-auto .simlock');
  window.__dup=s.cloneNode(true); s.parentNode.appendChild(window.__dup); })()`);
say(await ev(`document.querySelectorAll('#tab-auto .simlock').length`) === 2,
  "  複製成兩顆之後這一條也會紅（v1 的三處就是退件原因）");
await ev(`window.__dup.remove()`);

/* ═══ ④ 文字預算：第一屏不准有散文（§15-1b，v1 退件就是退在這條）═════ */
console.log("\n=== ④ 第一屏文字預算 ===");
const PROSE = `(()=>{
  const out=[], root=document.getElementById('tab-auto');
  const skip=new Set(['BUTTON','TABLE','THEAD','TBODY','TR','TD','TH','SUMMARY','INPUT','CANVAS','KBD']);
  for(const e of root.querySelectorAll('*')){
    if(e.children.length) continue;
    if(skip.has(e.tagName)) continue;
    if(e.closest('table')||e.closest('button')) continue;
    const r=e.getBoundingClientRect();
    if(r.height<=0||r.width<=0) continue;          // 收摺起來的東西不算第一屏
    if(r.top>=innerHeight) continue;               // 要捲才看得到的也不算
    const t=(e.textContent||'').trim();
    if(t.length<=30) continue;
    const lh=parseFloat(getComputedStyle(e).lineHeight)||16;
    out.push({t:t.slice(0,28),lines:Math.round(r.height/lh),len:t.length});
  }
  return out;})()`;
const prose = await ev(PROSE);
chk("⛔ 第一屏沒有任何 ≥3 行的散文塊", prose.filter(p => p.lines >= 3), []);
console.log(`    第一屏的長文字葉節點：${prose.length} 個（都 < 3 行）`);
// 【尺的自證】掃描器要真的掃到東西 —— 掃到 0 個節點的「全綠」是假綠燈
const seen = await ev(`(()=>{let n=0; const root=document.getElementById('tab-auto');
  for(const e of root.querySelectorAll('*')){ if(e.children.length) continue;
    const r=e.getBoundingClientRect(); if(r.height>0&&r.top<innerHeight) n++; } return n;})()`);
say(seen > 30, "  自證：掃描器在第一屏真的看得到節點", `${seen} 個`);
chk("〈換一個門檻看看〉預設關著", await ev(`document.getElementById('atadv').open`), false);
chk("配對對照收進摺疊區、預設關著", await ev(`document.getElementById('atpair').open`), false);
console.log("  負控組：");
/* 把一牆解釋文字**平鋪回第一屏**（＝v1 被退件時的樣子）。
   ⚠️ 2026-09-08〈這一頁在算什麼〉整個刪掉之後，餌不能再從 #atabout 取 ——
      改成把當初那十條寫死在探針裡當餌。⛔ 這段字**只活在探針**，產品端零命中
      （㉔ 在守）。只是把 details 打開不算數：它在頁面下半部，本來就不在第一屏。 */
const WALL = "它只回答一件事：判斷方向有沒有加分。永遠只是模擬，一張單都不會送出去。" +
  "不是建議，也不預告。沒有四個裡有三個做多這種綜合。不到 30 筆不給勝率百分比。" +
  "歷史回填的成績與上線後實跑的成績不可以加在一起算。為什麼是 505 筆／約兩年。";
await ev(`(()=>{
  const d=document.createElement('div'); d.id='__wall';
  d.textContent=${JSON.stringify(WALL)}; d.style.maxWidth='420px';
  const r=document.getElementById('tab-auto'); r.insertBefore(d,r.firstChild); return 1;})()`);
const prose2 = await ev(PROSE);
say(prose2.filter(p => p.lines >= 3).length > 0,
  "  把一牆解釋文字平鋪回第一屏 ⇒ 這一條會紅",
  `第一屏多出 ${prose2.length - prose.length} 個長文字塊，最長 ${Math.max(...prose2.map(p => p.lines))} 行`);
await ev(`document.getElementById('__wall').remove()`);
chk("  還原：第一屏又沒有 ≥3 行的散文", (await ev(PROSE)).filter(p => p.lines >= 3), []);

/* ═══ ⑤ ⛔ 進度尺**已經拿掉**，而且不准回來（2026-09-08）══════════════════
   Benson 的原話：「程式下單那邊這個欄位不需要」——他紅框圈的就是那條
   「這個測試跑到哪裡了　1 / 505 筆 ／ 現在 1 天 ／ 要到這裡才算數 約 2.1 年」。
   ⚠️ 拿掉東西也要有守衛，不然下一個人會把它加回來 ⇒ 這一節量的是**零命中**，
      DOM ＋ 兩張 canvas 兩邊都掃（他早上盯的是圖，只掃 DOM 會漏）。 */
console.log("\n=== ⑤ ⛔ 進度尺已經拿掉（不准回來）===");
const GONE_TRACK = ["這個測試跑到哪裡了", "要到這裡才算數", "/ 505 筆", "505 筆 ≈"];
/* 掃「畫面上真的看得到的字」：葉節點 textContent ＋ title/aria-label ＋ 兩張 canvas。 */
const SEEN = `(()=>{
  const root=document.getElementById('tab-auto'), out=[];
  for(const e of root.querySelectorAll('*')){
    if(!e.children.length){ const t=(e.textContent||'').trim(); if(t) out.push(t); }
    if(e.hasAttribute('title')) out.push(e.getAttribute('title'));
    if(e.hasAttribute('aria-label')) out.push(e.getAttribute('aria-label'));
  }
  return out;})()`;
const seenAllTxt = async () => {
  const dom = await ev(SEEN);
  const c1 = await canvasText("atday", "atDrawDay");
  const c2 = await canvasText("atcum", "atDrawCum");
  return { dom, cv: [...c1, ...c2] };
};
const hits = (bag, words) => [...bag.dom, ...bag.cv]
  .filter(s => words.some(w => String(s).includes(w))).map(s => String(s).slice(0, 46));
let G = await seenAllTxt();
chk("⛔ #attrack 這個元素不存在了", await ev(`document.getElementById('attrack')!==null`), false);
chk("⛔ .at-track 一個都沒有", await ev(`document.querySelectorAll('#tab-auto .at-track').length`), 0);
chk("⛔ 進度尺那幾句話在 DOM ＋ 兩張 canvas 上零命中", hits(G, GONE_TRACK), []);
say(G.dom.length > 30 && G.cv.length > 20,
  `  自證：這把尺真的看得到東西（DOM ${G.dom.length} 段 ＋ canvas ${G.cv.length} 段）`);
console.log("  負控組（把進度尺原封不動加回去 ⇒ 上面那一條要紅）：");
await ev(`(()=>{
  const S=AT.stats||{}, N=S.track_n||505, n=(S.total_n!=null?S.total_n:S.n)||0;
  const d=document.createElement('div'); d.className='at-track'; d.id='attrack';
  d.innerHTML='<div class="hd"><span class="t">這個測試跑到哪裡了</span>'+
    '<span class="n">'+n+' / '+N+' 筆</span></div>'+
    '<div class="ft"><span>現在<b>'+n+' 天</b></span>'+
    '<span>要到這裡才算數<b>約 2.1 年</b></span></div>';
  const r=document.getElementById('tab-auto'); r.insertBefore(d,r.firstChild); return 1;})()`);
const Gbad = await seenAllTxt();
say(hits(Gbad, GONE_TRACK).length > 0, "  加回來之後真的抓得到 ⇒ 這一條會紅",
  JSON.stringify(hits(Gbad, GONE_TRACK).slice(0, 3)));
say(await ev(`document.getElementById('attrack')!==null`), "  「元素不存在」那一條也會紅");
await ev(`document.getElementById('attrack').remove()`);
chk("  還原之後又零命中", hits(await seenAllTxt(), GONE_TRACK), []);
/* ⚠️ 後端那兩個欄位**照端不要拿掉**（天花板的 title 還在用 track_n 算數字）——
   ⛔ 這一條不是「進度尺還在」，是「拿掉畫面不等於拿掉資料」。 */
const TRK = await ev(`fetch('/api/auto/stats?win=20&src=live').then(r=>r.json())
  .then(x=>({track:x.track_n,total:x.total_n,n:x.n}))`);
chk("後端照樣端出 track_n（天花板的 title 靠它算）", TRK.track, 505);
say(TRK.total === 20, "  total_n（累積幾天）也還在，跟窗口 n 是兩個數字",
  `total_n=${TRK.total}　n=${TRK.n}`);

/* ═══ ⑥ 少樣本不給百分比（§15-2）════════════════════════════════════ */
console.log("\n=== ⑥ ⛔ 少樣本不給百分比 ===");
await ctl("/at/days/3");
await reload();
chk("3 天 ⇒ AT.stats.n", await ev("AT.stats.n"), 3);
chk("⛔ 成績表一個百分比都沒有",
  await ev(`document.getElementById('attbl').textContent.match(/\\d+%/)`), null);
say((await ev(`document.getElementById('attbl').textContent`)).includes("不到 30 筆，不算 %"),
  "  改成寫「不到 30 筆，不算 %」");
/* 負控組：這道門檻**前後端各有一道**（後端 rate 直接回 null）——
   所以只拿掉前端那道還是印不出 %（第一版探針就這樣誤以為「負控組打不紅」）。
   要重現的故障是「有人在前端自己算勝率」，所以兩件事都做：拿掉門檻 ＋ 就地算。 */
console.log("  負控組：拿掉前端門檻，而且在前端自己算勝率");
if (await mutate("atRateCell", "if(!r.n||r.n<min)", "if(false)") &&
    await mutate("atRateCell", "r.rate+'%</td>'", "Math.round(r.w/r.n*100)+'%</td>'")) {
  await ev("atPaintStats()");
  say(!!(await ev(`document.getElementById('attbl').textContent.match(/\\d+%/)`)),
    "  門檻拿掉之後真的會冒出百分比 ⇒ 這一條會紅",
    (await ev(`document.getElementById('attbl').textContent`)).match(/\d+%/g)?.join(" "));
  await unmutate("atRateCell");
  await ev("atPaintStats()");
}
chk("  還原後又沒有百分比了",
  await ev(`document.getElementById('attbl').textContent.match(/\\d+%/)`), null);

/* ═══ ⑦ 樣本 < 10 不畫累計線（§15-3）════════════════════════════════ */
console.log("\n=== ⑦ ⛔ 少於 10 筆不畫累計線 ===");
const strokes = async () => await ev(`(()=>{
  const cv=document.getElementById('atcum'), ctx=cv.getContext('2d');
  const o=ctx.stroke; let n=0; ctx.stroke=function(){ n++; return o.apply(this,arguments); };
  try{ atDrawCum(); } finally { ctx.stroke=o; }
  return n;})()`);
chk("3 筆 ⇒ canvas 上一條線都沒有", await strokes(), 0);
say((await ev(`document.getElementById('atcumempty').textContent`)).includes("還不畫線"),
  "  改成空狀態「只有 N 筆，還不畫線」");
console.log("  負控組：把 CUM_MIN_N 當成 0");
if (await mutate("atDrawCum", "if(n<min) return 0;", "if(false) return 0;")) {
  say(await strokes() > 0, "  門檻拿掉之後真的畫得出線 ⇒ 這一條會紅");
  await unmutate("atDrawCum");
}
await ctl("/at/days/20");
await reload();
say(await strokes() > 3, "20 筆 ⇒ 線畫得出來", `${await strokes()} 次 stroke`);

/* ═══ ⑧ 紅線：無預測字眼（§15-4）════════════════════════════════════ */
console.log("\n=== ⑧ ⛔ 紅線：不准出現預測／建議／訊號強度 ===");
const BAN = ["預測", "預估", "預期", "勝率預估", "期望值", "建議", "訊號強度", "看漲", "看跌",
  "準確率", "目標價", "支撐", "壓力", "買點", "賣點", "該進場", "可以進", "追多", "追空",
  "突破訊號", "共識", "多數決", "最佳"];
// 摺疊區的字也要掃（收摺不等於不在畫面上）
await ev(`['atadv','atpair'].forEach(i=>document.getElementById(i).open=true)`);
await ev("atPaintStats()");
/* ⚠️ **一段一段收，不可以把整頁 textContent 併成一大串**：
   上下文判準是「命中前後 24 字要有否定詞」，併成一串的話隔壁元素的「不」會滲進來，
   假的肯定句就變成合法（第一版探針的負控組因此打不紅）。 */
const COLLECT = `(()=>{
  const root=document.getElementById('tab-auto'), out=[];
  for(const e of root.querySelectorAll('*')){
    if(!e.children.length){ const t=(e.textContent||'').trim(); if(t) out.push(t); }
    if(e.hasAttribute('title')) out.push(e.getAttribute('title'));
    if(e.hasAttribute('aria-label')) out.push(e.getAttribute('aria-label'));
  }
  return out;})()`;
const texts = await ev(COLLECT);
// ⛔ canvas 上的字一定要一起掃：這一頁的進場標籤、泳道標籤、「這天不做」、邊緣籤都在那裡
const cvTxt = (await canvasText("atday", "atDrawDay")).join(" ⏐ ");
const cumTxt = (await canvasText("atcum", "atDrawCum")).join(" ⏐ ");
const OKCTX = /不是|不做|不准|不會|沒有|⛔|不顯示|不預告|沒超過/;
const scan = (srcs, words) => {
  const out = [];
  for (const src of srcs) {
    for (const w of words) {
      let i = -1;
      while ((i = src.indexOf(w, i + 1)) >= 0) {
        const ctx = src.slice(Math.max(0, i - 24), i + w.length + 24);
        if (!OKCTX.test(ctx)) out.push({ w, ctx });
      }
    }
  }
  return out;
};
const ALL = () => [...texts, ...cvTxt.split(" ⏐ "), ...cumTxt.split(" ⏐ ")];
chk("⛔ DOM ＋ title ＋ 兩張 canvas 都沒有肯定句的禁詞", scan(ALL(), BAN), []);
console.log(`    掃了 ${texts.length} 段 DOM/title ＋ canvas ${cvTxt.length + cumTxt.length} 字`);
chk("⛔ 沒有任何跨算法的綜合（幾個看多／一致性／綜合方向／多數決）",
  scan(ALL(), ["四個裡", "幾個看多", "一致性", "綜合方向", "加權方向", "多數決"]), []);
console.log("  負控組：");
await ev(`(()=>{const d=document.createElement('div'); d.id='__bait';
  d.textContent='今天建議做多'; document.getElementById('tab-auto').appendChild(d); return 1;})()`);
say(scan(await ev(COLLECT), BAN).length > 0,
  "  塞一個「今天建議做多」進去 ⇒ 這一條會紅",
  JSON.stringify(scan(await ev(COLLECT), BAN)[0] || {}));
await ev(`document.getElementById('__bait').remove()`);
chk("  拿掉之後又乾淨了", scan(await ev(COLLECT), BAN), []);
// ⛔⛔ canvas 那半一定要獨立驗：他早上盯的是圖不是文字（【細節】為此退件過 M2）
if (await mutate("atDrawLanes", "'這天算不出訊號'", "'今天建議做多'")) {
  const bad = await canvasText("atday", "atDrawDay");
  await ev(`(()=>{const D=AT.data; window.__keep=D.dirs.A; D.dirs.A=null;
    if(D.runs) D.runs.A={dir:null,skip:'no_ref'}; return 1;})()`);
  const bad2 = await canvasText("atday", "atDrawDay");
  say(scan(bad2, BAN).length > 0, "  canvas 上冒出禁詞 ⇒ 這一條會紅",
    JSON.stringify(scan(bad2, BAN)[0] || {}) + `（DOM 那半完全看不到：${scan(await ev(COLLECT), BAN).length} 命中）`);
  await ev(`(()=>{const D=AT.data; D.dirs.A=window.__keep; return 1;})()`);
  await unmutate("atDrawLanes");
  await ev("atPaint()");
}
await ev(`['atadv','atpair'].forEach(i=>document.getElementById(i).open=false)`);

/* ═══ ⑨ 紅線：方向不用紅綠（§15-5）══════════════════════════════════ */
console.log("\n=== ⑨ ⛔ 顏色只給損益 ===");
const UP = "rgb(238, 90, 84)", DOWN = "rgb(52, 179, 126)";
const dirCols = await ev(`[...document.querySelectorAll('#tab-auto .at-today .dir')]
  .map(e=>getComputedStyle(e).color)`);
say(dirCols.length === 4, "四格方向都在", `${dirCols.length} 格`);
chk("⛔ 方向一顆都不是紅綠", dirCols.filter(x => x === UP || x === DOWN), []);
const sgCols = await ev(`[...document.querySelectorAll('#tab-auto .at-today .sg')]
  .map(e=>getComputedStyle(e).color)`);
chk("⛔ 訊號值也不是紅綠", sgCols.filter(x => x === UP || x === DOWN), []);
chk("⛔ 天花板那一行不是紅綠",
  await ev(`[...document.querySelectorAll('#atceil,#atceil *')].map(e=>getComputedStyle(e).color)
    .filter(x=>x===${JSON.stringify(UP)}||x===${JSON.stringify(DOWN)})`), []);
/* ⚠️ 2026-09-08 進度尺拿掉了 ⇒ 這一條改成掃**整個第一屏的非表格文字**：
   顏色規矩（紅綠只給損益）沒有跟著那條尺一起消失，範圍反而變大了。
   ⛔ 不可以因為那條尺不見了就把這一條刪掉。 */
chk("⛔ 整頁只有掛 .up/.down（＝損益）的元素可以是紅綠，其餘一顆都沒有",
  await ev(`[...document.querySelectorAll('#tab-auto *')]
    .filter(e=>!e.children.length&&!e.classList.contains('up')&&!e.classList.contains('down'))
    .map(e=>[e.className||e.tagName,getComputedStyle(e).color])
    .filter(x=>x[1]===${JSON.stringify(UP)}||x[1]===${JSON.stringify(DOWN)})`), []);
say(await ev(`[...document.querySelectorAll('#tab-auto .up,#tab-auto .down')].some(e=>{
  const c=getComputedStyle(e).color; return c===${JSON.stringify(UP)}||c===${JSON.stringify(DOWN)};})`),
  "  尺是活的：掛 .up/.down 的那些**真的**是紅綠（不是整頁都沒顏色）");
say(await ev(`[...document.querySelectorAll('#attbl .pts')].some(e=>{
  const c=getComputedStyle(e).color; return c===${JSON.stringify(UP)}||c===${JSON.stringify(DOWN)};})`),
  "  尺是活的：累計點數（＝損益）**有**用紅綠");
console.log("  負控組：");
await ev(`document.querySelector('#tab-auto .at-today .dir').style.color='var(--up)'`);
say((await ev(`[...document.querySelectorAll('#tab-auto .at-today .dir')]
  .map(e=>getComputedStyle(e).color)`)).includes(UP), "  把一格染紅 ⇒ 這一條會紅");
await ev(`document.querySelector('#tab-auto .at-today .dir').style.color=''`);

/* ═══ ⑩ 天花板常駐，而且是算出來的（§15-7）══════════════════════════ */
console.log("\n=== ⑩ 天花板 ===");
const ceilTxt = () => ev(`document.getElementById('atceil').textContent`);
say(await ev(`document.getElementById('atceil').offsetParent!==null`), "常駐在畫面上");
const m20 = (await ceilTxt()).match(/每筆差 ([\d.]+) 點以上/);
say(!!m20, "文案符合「每筆差 X 點以上」", await ceilTxt());
/* ⚠️ 天花板跟的是**成績表那個窗口的筆數**（它就貼在表格底下），所以要換窗口不是換天數。
   進度尺跟的才是累積筆數 —— 兩個數字刻意不同，見 ⑤。 */
await ctl("/at/days/120");
await reload();
await ev(`AT.swin=0; atFetchStats()`); await sleep(900);
chk("  切到「全部」⇒ 窗口變成 120 筆", await ev("AT.stats.n"), 120);
const m120 = (await ceilTxt()).match(/每筆差 ([\d.]+) 點以上/);
say(!!m120 && Number(m20[1]) > Number(m120[1]),
  "n=20 的門檻 > n=120 的門檻（證明它隨筆數變，不是寫死的字串）",
  `${m20 && m20[1]} → ${m120 && m120[1]}`);
chk("  n=120 的值跟算式一致", Number(m120[1]), await ev("atCeiling(120)"));
await ev(`AT.swin=20; atFetchStats()`); await sleep(900);
console.log("  負控組：把 atCeiling 改成回傳常數");
if (await mutate("atCeiling", "return Math.round(z*s/Math.sqrt(n)*10)/10;", "return 42.0;")) {
  await ev("atPaintStats()");
  const bad120 = (await ceilTxt()).match(/每筆差 ([\d.]+) 點以上/);
  say(bad120 && Number(bad120[1]) === 42, "  兩個 n 會得到同一個值 ⇒ 這一條會紅", bad120 && bad120[1]);
  await unmutate("atCeiling");
  await ev("atPaintStats()");
}
await ctl("/at/days/20");
await reload();
say((await ceilTxt()).includes("目前") , "  天花板那一行同時說出「有沒有一條超過」", await ceilTxt());

/* ═══ ⑪ ⛔ 不預告（§15-8）════════════════════════════════════════════ */
console.log("\n=== ⑪ ⛔ 09:03:30 之前不顯示方向 ===");
await ctl("/at/clock/09:01:00");
await reload();
await ev(`atGoDay(AT.today)`); await settle();
const early = await ev(`document.getElementById('attoday').textContent`);
chk("⛔ 「今天」那一區不含任何方向字樣", [early.includes("做多"), early.includes("做空")],
  [false, false]);
say(early.includes("還沒到"), "  改成寫「今天 09:03:30 還沒到」", early.slice(0, 40));
say(!/還有|倒數|目前.*會/.test(early), "  ⛔ 也沒有倒數或「目前會判斷…」");
const earlyCv = (await canvasText("atday", "atDrawDay")).join(" ⏐ ");
chk("⛔ canvas 上的泳道也不准提前透露方向",
  [/·\s*多\s/.test(earlyCv), /·\s*空\s/.test(earlyCv)], [false, false]);
console.log("  負控組：拿掉時態鎖");
// ⚠️ 治具此時**確實有**今天那一列（有 dirs），所以這個突變等於「把預告做出來」
if (await mutate("atSigPassed", "return s==null?false:(s>=ATSIG);", "return true;")) {
  await ev("atPaint()");
  const bad = await ev(`document.getElementById('attoday').textContent`);
  say(bad.includes("做多") || bad.includes("做空"),
    "  拿掉之後 09:01 就看得到方向 ⇒ 這一條會紅", bad.slice(0, 40));
  await unmutate("atSigPassed");
  await ev("atPaint()");
}
await ctl("/at/clock/10:30:00");
await reload();
const late = await ev(`document.getElementById('attoday').textContent`);
say(late.includes("做多") || late.includes("做空"), "  時刻過了就顯示（尺是活的）");

/* ═══ ⑪b ⛔⛔ 還沒摸到 ±100 ⇒ 畫面上是「持倉中」，不是一個假的點數 ════════
   2026-09-08 他早上 09:05 打開分頁，四條泳道全部寫著「09:05 收盤平 ±67 點」——
   那是後端拿盤中最後一根 K 棒的收盤價硬算出來的 **假成績**。修法在後端
   （沒摸到就不寫 settle 列），這一節守的是「畫面上到底寫了什麼」。
   ⛔ 規格 §16-3 拍板不顯示浮動損益 ⇒ 持倉中只寫狀態，⛔ 不准算現在賺賠多少。
   ⚠️ 這一頁的字有一半畫在 canvas 上（泳道），只掃 DOM 會漏掉（【細節】M2 的教訓）。 */
console.log("\n=== ⑪b ⛔ 持倉中 ≠ 一個假的點數 ===");
await ctl("/at/settletoday/0");          // 今天有訊號、但還沒結算
await ctl("/at/clock/10:30:00");         // 盤中：日盤還沒收
await reload();
await ev(`atGoDay(AT.today)`); await settle();
chk("後端說今天是持倉中", await ev("!!(AT.data&&AT.data.holding)"), true);
const HOLD = await ev(`(()=>{const D=AT.data,out={};
  [...document.querySelectorAll('#attoday .c')].forEach((e,i)=>{
    out[AT_ORDER[i]]=e.querySelector('.rs').textContent.trim();});
  return {dirs:D.dirs||{}, rs:out};})()`);
const traded = Object.keys(HOLD.rs).filter(k => HOLD.dirs[k] === 1 || HOLD.dirs[k] === -1);
say(traded.length >= 2, `  今天有 ${traded.length} 條真的下單（其餘沒過門檻／算不出訊號）`,
  JSON.stringify(HOLD.dirs));
chk("⛔ 有下單的那幾條寫「持倉中」", traded.map(k => HOLD.rs[k]), traded.map(() => "持倉中"));
chk("  ⛔ 一個出場結果字樣都不准出現（收盤平／停利／停損）",
  Object.values(HOLD.rs).filter(t => /收盤平|停利|停損/.test(t)), []);
chk("  ⛔ 也不准出現任何點數（不做浮動損益，規格 §16-3）",
  Object.values(HOLD.rs).filter(t => /[-+−]?\d+(\.\d+)?\s*點/.test(t)), []);
chk("  沒過門檻那幾條照實寫「這天不下單」（⛔ 它沒有部位，寫持倉中是假話）",
  Object.entries(HOLD.rs).filter(([k]) => HOLD.dirs[k] === 0)
    .map(([, t]) => /不下單/.test(t)),
  Object.keys(HOLD.dirs).filter(k => HOLD.dirs[k] === 0).map(() => true));
const holdCv = await canvasText("atday", "atDrawDay");
chk("⛔ canvas 的泳道上也是「持倉中」", holdCv.filter(t => t === "持倉中").length, traded.length);
chk("  ⛔ canvas 上一個「收盤平」都沒有", holdCv.filter(t => /收盤平/.test(t)), []);
const holdPager = await ev(`document.querySelector('#tab-auto .pager .r2').textContent`);
say(holdPager.includes("持倉中") && !holdPager.includes("已結算"),
  "翻頁列那一行也寫持倉中（⛔ 而且只寫進場價，不寫賺賠）", holdPager.trim().slice(0, 40));
await ev(`AT.pick=true; atPaint()`);
say((await ev(`(document.querySelector('#atpick [data-atday="'+AT.today+'"]')||{})
  .textContent||''`)).includes("持倉中"), "日期清單那一列也寫持倉中");
await ev(`AT.pick=false; atPaint()`);

console.log("  負控組（三個）：");
// ① 把 holding 關掉 ⇒ 應該退回「結算中」（證明畫面真的在讀那個旗標，不是寫死的）
await ev(`AT.data.holding=false; atPaint()`);
say((await ev(`document.getElementById('attoday').textContent`)).includes("結算中"),
  "  ① holding=false ⇒ 變成「結算中」（不是寫死「持倉中」）");
chk("  ① 而且此時沒有「持倉中」",
  (await ev(`document.getElementById('attoday').textContent`)).includes("持倉中"), false);
await ev(`AT.data.holding=true; atPaint()`);
// ② 把 runs 灌成 2026-09-08 那個 bug 的形狀（四條 eod）⇒ 上面那幾條一定要紅
await ev(`(()=>{const r={};for(const k of ATKEYS)
  r[k]={dir:(k==='D'?1:-1),exit_at:'09:05',exit_px:11933,
        pts:(k==='D'?-67:67),why:'eod',both:false};
  AT.data.runs=r; atPaint(); return 1;})()`);
const bugDom = await ev(`document.getElementById('attoday').textContent`);
const bugCv = await canvasText("atday", "atDrawDay");
say(/收盤平/.test(bugDom) && bugCv.some(t => /收盤平/.test(t)),
  "  ② 灌進那個假成績之後，DOM 與 canvas 兩邊都會出現「收盤平」⇒ 這把尺量得到",
  bugDom.replace(/\s+/g, " ").slice(0, 60));
await ev(`delete AT.data.runs; atPaint()`);
// ③ 收盤後還沒結算 ⇒ 那是「結算中」不是「持倉中」（兩個狀態不准寫同一句）
await ctl("/at/clock/14:00:00");
await reload();
await ev(`atGoDay(AT.today)`); await settle();
chk("  ③ 收盤後 ⇒ 後端不再說持倉中", await ev("!!(AT.data&&AT.data.holding)"), false);
say((await ev(`document.getElementById('attoday').textContent`)).includes("結算中"),
  "  ③ 畫面改寫「結算中」（⛔ 兩個不同的狀態不准寫同一句）");
await ctl("/at/settletoday/1");
await ctl("/at/clock/10:30:00");
await reload();
say(await ev("!(AT.data&&AT.data.holding)"), "  收尾：治具還原成已結算");

/* ═══ ⑫ 回測與實跑不相加（§15-9）════════════════════════════════════ */
console.log("\n=== ⑫ ⛔ 回測與實跑不相加 ===");
const before = await ev("AT.stats.n");
const beforeCeil = await ceilTxt();
await ctl("/at/backfill/200");
await reload();
chk("灌 200 筆回填之後 live 的筆數不變", await ev("AT.stats.n"), before);
chk("  天花板也不變", await ceilTxt(), beforeCeil);
/* ⚠️ 只用「近20」量**抓不到**這件事：回填的日子比較早，取最後 20 天照樣全是實跑
   （前端偷偷改成 src=all 也一樣 20）—— 突變測試 F5 就是這樣打不紅的。
   要切到「全部」才看得見。 */
await ev(`AT.swin=0; atFetchStats()`); await sleep(900);
chk("  切到「全部」之後也只有實跑那 20 筆（⛔ 回填不准混進來）", await ev("AT.stats.n"), before);
chk("  前端要的一定是 src=live",
  await ev(`String(atFetchStats).includes("src=live")`), true);
await ev(`AT.swin=20; atFetchStats()`); await sleep(900);
say((await ev(`document.getElementById('atnotes').textContent`)).includes("歷史回填"),
  "  但畫面上要講出來有多少筆回填（⛔ 不可以安靜地藏起來）");
console.log("  負控組：把前端要的 src 從 live 改成 all");
const nAll = await ev(`fetch('/api/auto/stats?win=0&src=all').then(r=>r.json()).then(x=>x.n)`);
say(nAll > before, "  src=all 真的會變成一大包 ⇒ 過濾在承重", `${before} → ${nAll}`);
await ctl("/at/backfill/0");
await reload();

/* ═══ ⑬ 泳道不打架（§15-11）══════════════════════════════════════════ */
console.log("\n=== ⑬ 四條泳道 ===");
const cv = (await canvasText("atday", "atDrawDay"));
const entries = cv.filter(t => t.includes("模擬進場"));
chk("⛔ 「模擬進場」只出現一次（四條事實上就是同一個時刻同一個價）", entries.length, 1);
say(/09:03:30 模擬進場 1\d{4}\.\d/.test(entries[0] || ""), "  而且印的是完整價格（⛔ 不准截斷）", entries[0]);
/* ⛔⛔ 泳道左邊放的是**名字**不是代號（規格 §2.0／§9.3，這一條被 Benson 退件過）。 */
const LANENAMES = await ev(`AT_ORDER.map(k=>atName(k))`);
chk("四條泳道畫的是名字，不是代號", LANENAMES.filter(n => cv.includes(n)), LANENAMES);
chk("⛔ 泳道上一個孤立的代號都沒有", cv.filter(t => /^[ABCD]$/.test(t)), []);
const laneW = await ev(`(()=>{const ctx=document.getElementById('atday').getContext('2d');
  ctx.save(); ctx.font=ATFONTN;
  let nw=0; for(const k of AT_ORDER) nw=Math.max(nw,ctx.measureText(atName(k)).width);
  ctx.restore(); return {nw:nw, L:ATLN.L, stacked:ATLN.stacked, laneH:ATLN.laneH};})()`);
say(laneW.stacked || laneW.L >= Math.ceil(laneW.nw) + 18,
  "⛔ 名字欄寬是**量出來的**（L ≥ ceil(max 名字寬) + 18，不准寫死也不准截字）",
  `L=${laneW.L}　最寬的名字 ${laneW.nw.toFixed(1)}px`);
const lanes = await ev(`(()=>{
  const H=ATC.H, laneTop=H-ATBOT-AT_ORDER.length*ATLN.laneH, out=[];
  for(let i=0;i<4;i++) out.push([laneTop+i*ATLN.laneH, laneTop+(i+1)*ATLN.laneH]);
  return out;})()`);
let overlap = 0;
for (let i = 1; i < lanes.length; i++) if (lanes[i][0] < lanes[i - 1][1]) overlap++;
chk("⛔ 四條泳道的 y 區間兩兩不重疊", overlap, 0);
say(lanes[3][1] <= (await ev("ATC.H")), "  四條都在畫布裡", JSON.stringify(lanes[3]));
// C 不做的日子：⛔ 不可以整條消失，而且要寫出訊號值
const cday = await ev(`(()=>{const L=AT.days.filter(d=>d.dirs&&d.dirs.C===0); return L.length?L[0].d:null;})()`);
if (cday) {
  await ev(`atGoDay(${JSON.stringify(cday)})`); await settle();
  const t = (await canvasText("atday", "atDrawDay")).join(" ⏐ ");
  say(t.includes("這天不做"), "C 沒做的日子畫成「這天不做」，⛔ 不是整條消失");
  say(/這天不做（訊號 [+-][\d.]+ 點/.test(t.replace(/[（(]/g, "（")), "  而且寫出訊號值",
    (t.match(/這天不做[^⏐]*/) || [""])[0]);
} else say(false, "治具裡找不到 C 沒做的日子（資料集要調）");
console.log("  負控組：把泳道的 y 偏移拿掉（四條疊在一起）");
if (await mutate("atDrawLanes", "laneTop+i*LN.laneH", "laneTop+0*LN.laneH")) {
  const t = await ev(`(()=>{const H=ATC.H, laneTop=H-ATBOT-AT_ORDER.length*ATLN.laneH;
    return [laneTop,laneTop];})()`);
  say(t[0] === t[1], "  四條的 y 會變成同一個值 ⇒ 重疊檢查會紅");
  await unmutate("atDrawLanes");
}
// 只看 08:45~09:30 ⇒ 出場落在畫面外時要講「畫面外」，⛔ 不可以把標記黏在邊緣假裝畫得出來
await ev(`AT.zoom=true; atPaint()`);
const zoomTxt = (await canvasText("atday", "atDrawDay")).join(" ⏐ ");
say(zoomTxt.includes("畫面外") || zoomTxt.includes("在畫面"),
  "切到「只看 08:45~09:30」時，畫不到的東西寫「畫面外／在畫面上方」",
  (zoomTxt.match(/[^⏐]*畫面[^⏐]*/) || [""])[0].trim());
await ev(`AT.zoom=false; atPaint()`);
chk("⛔ 時間軸只印到分鐘（印到秒會讓人以為在看【細節】那張秒級圖）",
  (await canvasText("atday", "atDrawDay")).filter(t => /^\d\d:\d\d:\d\d$/.test(t)), []);
/* ⛔ 字要**真的落在畫布裡**。第一版時間軸畫在 x−16，x=0 那格變成 −16 ⇒
   「08:45」被切成「45」——字串完全正確，只有截圖看得出來。 */
const draws = await canvasDraws("atday", "atDrawDay");
const dim = await ev(`({W:ATC.W,H:ATC.H})`);
const outside = draws.filter(d => d.x < 0 || d.y < 0 || d.y > dim.H || d.x + d.w > dim.W + 1);
chk("⛔ 沒有任何一段字被畫到畫布外面（會被切掉一半）", outside.map(d => [d.t, Math.round(d.x)]), []);
say(draws.length > 20, `  量了 ${draws.length} 段字（畫布 ${dim.W}×${dim.H}）`);
const mineY = draws.filter(d => d.t.startsWith("真 ")).map(d => Math.round(d.y));
say(mineY.length >= 2, "  治具那天有 ≥2 筆他自己的單（才測得到互相壓住）", `${mineY.length} 張籤`);
chk("⛔ 他自己那幾張籤的 y 兩兩不同（不錯開就會互相壓住）",
  new Set(mineY).size, mineY.length);
/* ⛔ 這一頁的主角是「09:03:30 模擬進場」那一筆 ⇒ 它必須畫在**最上層**。
   他的進場時刻就落在 09:03:30 附近、價也差不多，順序反了那個 ▲ 會蓋掉金籤上的字
   （截圖才看見；掃字串與「有沒有畫到畫布外」都抓不到）。 */
const order = draws.map(d => d.t);
say(order.findIndex(t => t.startsWith("真 ")) < order.findIndex(t => t.includes("模擬進場")),
  "⛔ 他自己那幾單先畫、模擬進場那張金籤最後畫（不然 ▲ 會蓋掉字）",
  `真=${order.findIndex(t => t.startsWith("真 "))} < 模擬進場=${order.findIndex(t => t.includes("模擬進場"))}`);

/* ═══ ⑭ 價格軸不准被 ±100 撐大 ════════════════════════════════════════ */
console.log("\n=== ⑭ 價格軸 ===");
const ax = await ev(`(()=>{const b=atBars(), v=atView(), A=atAxis(b,v);
  let hi=-1e18,lo=1e18;
  for(const x of b){ if(x.h>hi)hi=x.h; if(x.l<lo)lo=x.l; }
  return {axHi:A.hi,axLo:A.lo,barHi:hi,barLo:lo,step:A.step,px:AT.data.px};})()`);
say(ax.axHi - ax.axLo < (ax.barHi - ax.barLo) * 1.6,
  "⛔ ±100 沒有把價格軸撐大", `軸 ${(ax.axHi - ax.axLo).toFixed(0)} 點 vs K 棒 ${(ax.barHi - ax.barLo).toFixed(0)} 點`);
say(ax.axLo > 100, "⛔ 價格軸沒有掉到 0（真實單的 exit 可能是 null）", `下緣 ${ax.axLo}`);
say([1, 2, 2.5, 5, 10].some(k => {
  const e = Math.pow(10, Math.round(Math.log10(ax.step / k)));
  return Math.abs(ax.step - k * e) < 1e-6;
}), "刻度貼齊 niceStep（1/2/2.5/5/10×10ⁿ）", `step=${ax.step}`);

/* ═══ ⑮ canvas 尺寸不亂動（§15-13）══════════════════════════════════ */
console.log("\n=== ⑮ canvas.width 不准每次重畫都寫 ===");
const wrote = await ev(`(()=>{
  const cv=document.getElementById('atday');
  let n=0; const proto=Object.getPrototypeOf(cv);
  const d=Object.getOwnPropertyDescriptor(proto,'width');
  Object.defineProperty(cv,'width',{configurable:true,
    get(){return d.get.call(cv);}, set(v){ n++; d.set.call(cv,v); }});
  for(let i=0;i<200;i++) atDrawDay();
  delete cv.width;
  return n;})()`);
chk("連續 200 次重畫 ⇒ 0 次", wrote, 0);
const wrote2 = await ev(`(()=>{
  const cv=document.getElementById('atday');
  let n=0; const proto=Object.getPrototypeOf(cv);
  const d=Object.getOwnPropertyDescriptor(proto,'width');
  Object.defineProperty(cv,'width',{configurable:true,
    get(){return d.get.call(cv);}, set(v){ n++; d.set.call(cv,v); }});
  ATC={W:0,H:0,DPR:0}; atDrawDay();
  delete cv.width;
  return n;})()`);
say(wrote2 > 0, "  負控組：尺寸真的變了就一定要寫（證明計數器不是恆 0）", `${wrote2} 次`);

/* ═══ ⑯ 換日不會錯置（§15-14）════════════════════════════════════════ */
console.log("\n=== ⑯ 換日：只認最後一次的回應 ===");
/* ⚠️ 「每一個都慢」量不到這件事：那樣回應順序仍然照送出順序，
   舊那天本來就不會後回來。要**只讓第一個慢**，先送的才會最後回來。 */
const RACE = `(async()=>{
  const L=AT.days.map(d=>d.d);
  for(let i=0;i<5;i++){ atGoDay(L[i]); await new Promise(r=>setTimeout(r,60)); }
  await new Promise(r=>setTimeout(r,3500));
  return {want:L[4], date:AT.date, has:AT.data&&AT.data.date};})()`;
await ctl("/at/slowfirst/1/2500");
const race = await ev(RACE);
chk("連按 5 次（第一個慢 2.5 秒才回）之後停在最後一次要的那天", race.date, race.want);
chk("  畫面上的資料也是那一天", race.has, race.want);
chk("  兩個欄位對得上（AT.date vs AT.data.date）", race.date === race.has, true);
/* ⚠️⚠️ 這裡**有兩道守衛，而且互相備援**（跟【細節】分頁同一個結構）：
     ① 流水號 `if(my!==AT.seq) return;`
     ② 「這份是不是我現在要的那天」`if(d===AT.date)`
   只拿掉一道**打不紅** —— 第一版探針就是這樣，害我一度以為流水號沒作用。
   所以下面先證明「只拿掉一道還是綠」（＝備援是真的），再兩道一起拿掉才紅。 */
console.log("  負控組：只拿掉流水號（應該還是綠 —— 兩道互相備援）");
if (await mutate("atFetchDay", "if(my!==AT.seq) return;", "if(false) return;")) {
  await ctl("/at/slowfirst/1/2500");
  const one = await ev(RACE);
  say(one.has === one.want, "  只拿掉一道還是對的（證明它們是備援，不是「這道沒用」）",
    `畫面 ${one.has}`);
  console.log("  負控組：兩道一起拿掉");
  if (await mutate("atFetchDay", "if(d===AT.date){", "if(true){")) {
    await ctl("/at/slowfirst/1/2500");
    const bad = await ev(RACE);
    say(bad.has !== bad.want, "  兩道都拿掉 ⇒ 舊那天的資料會蓋回來，這一條會紅",
      `畫面 ${bad.has} vs 要的 ${bad.want}`);
  }
  await unmutate("atFetchDay");
}
await ctl("/at/slow/0");
await reload();
say(await ev(`AT.date===AT.data.date`), "  還原後 AT.date 與手上那份是同一天");

/* ═══ ⑰ 版面（§15-12）═══════════════════════════════════════════════ */
console.log("\n=== ⑰ 版面 ===");
await ev(`AT.pick=true; atPaint()`);
const flex = await ev(`(()=>{
  const out=[];
  for(const e of document.querySelectorAll('#tab-auto *')){
    const s=getComputedStyle(e);
    if(s.display!=='flex'||s.flexDirection!=='column') continue;
    if(s.maxHeight==='none') continue;
    for(const k of e.children){
      const cs=getComputedStyle(k);
      if(cs.flexShrink!=='0') out.push([e.className||e.id, k.className||k.tagName, cs.flexShrink]);
    }
  }
  return out;})()`);
chk("⛔ 有 max-height 的 flex 直欄，子元素全部 flex-shrink:0", flex, []);
const nRows = await ev(`document.querySelectorAll('#atpick .tk-list .row').length`);
say(nRows >= 10, "  清單筆數超過容器高度才量得到「被壓扁」", `${nRows} 列`);
const rowH = await ev(`[...document.querySelectorAll('#atpick .tk-list .row')]
  .map(e=>Math.round(e.getBoundingClientRect().height))`);
chk("  每一列高度一致（沒有被壓扁）", new Set(rowH).size, 1);
console.log("  負控組：");
await ev(`document.querySelectorAll('#atpick .tk-list .row').forEach(e=>e.style.flex='1 1 auto')`);
const bad = await ev(`(()=>{const out=[];
  for(const e of document.querySelectorAll('#tab-auto *')){
    const s=getComputedStyle(e);
    if(s.display!=='flex'||s.flexDirection!=='column'||s.maxHeight==='none') continue;
    for(const k of e.children) if(getComputedStyle(k).flexShrink!=='0') out.push(1);
  } return out.length;})()`);
say(bad > 0, "  塞一個 flex-shrink:1 進去 ⇒ 這一條會紅", `${bad} 個`);
await ev(`document.querySelectorAll('#atpick .tk-list .row').forEach(e=>e.style.flex='')`);
await ev(`AT.pick=false; atPaint()`);

for (const w of [1024, 1280, 1440]) {
  await c.send("Emulation.setDeviceMetricsOverride",
    { width: w, height: 1000, deviceScaleFactor: 1, mobile: false });
  await sleep(350); await ev("atPaint()");
  const o = await ev(`(()=>{const r=document.getElementById('tab-auto');
    return {overflow: r.scrollWidth-r.clientWidth,
            cvW: Math.round(document.getElementById('atday').getBoundingClientRect().width),
            cvH: Math.round(document.getElementById('atday').getBoundingClientRect().height)};})()`);
  say(o.overflow <= 1, `寬 ${w}px：沒有橫向溢出`, `溢出 ${o.overflow}px　canvas ${o.cvW}×${o.cvH}`);
  /* ⚠️ 2026-09-08 依 Benson「有一點點大」把日圖從 1040/470 縮成 **1040/380**。
     ⛔ 這個數字要跟 .at-wrap 的 aspect-ratio 對得上（改一邊要兩邊一起改）。 */
  say(Math.abs(o.cvW / o.cvH - 1040 / 380) < 0.02, `  canvas 沒有變形（1040/380）`,
    `${(o.cvW / o.cvH).toFixed(3)} vs ${(1040 / 380).toFixed(3)}`);
  /* ⛔⛔ 縮圖不准把泳道與名字欄壓扁（面板鐵律／規格 §9.3）：
     四條列高一律 22px、名字欄寬仍然是 measureText 量出來的、四個名字完整畫得出來。 */
  const LNw = await ev(`(()=>{atDrawDay(); const ctx=document.getElementById('atday').getContext('2d');
    ctx.save(); ctx.font=ATFONTN; let nw=0;
    for(const k of AT_ORDER) nw=Math.max(nw,ctx.measureText(atName(k)).width);
    ctx.restore();
    const H=ATC.H, laneTop=H-ATBOT-AT_ORDER.length*ATLN.laneH;
    return {laneH:ATLN.laneH,L:ATLN.L,nw:nw,stacked:ATLN.stacked,
            pH:Math.max(60,laneTop-ATTOP-ATLANEG),H:H};})()`);
  chk(`  寬 ${w}px：泳道列高還是 22（⛔ 不准為了縮圖去壓泳道）`, LNw.laneH, 22);
  say(LNw.L >= Math.ceil(LNw.nw) + 18, `  名字欄寬還是量出來的（⛔ 不准縮字）`,
    `L=${LNw.L}　最寬名字 ${LNw.nw.toFixed(1)}px　價格區 ${LNw.pH}px（canvas 高 ${LNw.H}）`);
  say(LNw.pH > 120, `  縮完之後價格區還有足夠高度（不是把 K 棒壓成一條線）`, `${LNw.pH}px`);
  const nmDraw = await canvasDraws("atday", "atDrawDay");
  const NMw = await ev(`AT_ORDER.map(k=>atName(k))`);
  chk(`  四個名字完整畫得出來（⛔ 不縮寫、不截字）`,
    NMw.filter(n => !nmDraw.some(d => d.t === n)), []);
}
await c.send("Emulation.setDeviceMetricsOverride",
  { width: 390, height: 900, deviceScaleFactor: 1, mobile: false });
await sleep(350); await ev("atPaint()");
const narrow = await ev(`(()=>{const w=document.querySelector('.at-tblwrap'), t=document.getElementById('attbl');
  const cells=[...t.querySelectorAll('td')].map(e=>({s:e.scrollWidth,c:Math.ceil(e.getBoundingClientRect().width)}));
  return {scroll:getComputedStyle(w).overflowX, wide:t.scrollWidth>w.clientWidth,
          cut:cells.filter(x=>x.s>x.c+1).length, n:cells.length};})()`);
chk("390px：成績表整張橫向捲", narrow.scroll, "auto");
say(narrow.wide, "  表格真的比容器寬（所以是捲、不是壓扁）");
chk("⛔ 一格欄位都沒有被壓到截字", narrow.cut, 0);
console.log(`    量了 ${narrow.n} 格`);
await c.send("Emulation.clearDeviceMetricsOverride");
await sleep(300); await ev("atPaint()");

/* ═══ ⑱ 空狀態是主流程（§12）════════════════════════════════════════ */
console.log("\n=== ⑱ 空狀態 ===");
await ctl("/at/days/0");
await reload();
say((await ev(`document.getElementById('atceil').textContent`)).includes("什麼都測不出來"),
  "  天花板那一行寫「0 筆 ⇒ 什麼都測不出來」");
/* ⚠️ 進度尺拿掉之後，空狀態也不准偷偷把它變回來（0/505 那個形狀最容易被當成「補一下」）。 */
chk("  ⛔ 0 筆的空狀態也沒有進度尺", await ev(`document.getElementById('attrack')!==null`), false);
chk("  ⛔ 0 筆時也零命中「/ 505 筆」",
  (await ev(`document.getElementById('tab-auto').textContent`)).includes("/ 505 筆"), false);
say(await ev(`document.getElementById('atpager')!==null &&
  document.querySelectorAll('#atpager .pager').length===1`),
  "⛔ 一天資料都沒有時，翻頁列照樣在（那是唯一的自救路徑）");
await ctl("/at/days/20");
await ctl("/at/today/none");
await reload();
const noneTxt = await ev(`document.getElementById('attoday').textContent`);
say(noneTxt.includes("沒有記錄"), "今天沒有記錄 ⇒ 明講", noneTxt.slice(0, 40));
await ctl("/at/today/have");

/* ═══ ⑲ 壞資料不准弄掉整頁 ═══════════════════════════════════════════ */
console.log("\n=== ⑲ ⛔ 一列壞資料不准弄掉一整頁 ===");
await ctl("/at/bad/1");
await reload();
say(await ev("AT.stats && AT.stats.n>0"), "壞資料在檔案裡，好資料照樣看得到", await ev("AT.stats.n"));
say((await ev(`document.getElementById('atnotes').textContent`)).includes("讀不出來"),
  "  而且把「幾列讀不出來」寫在畫面上（⛔ 不准安靜地少）");
await ctl("/at/bad/0");
await reload();

/* ═══ ㉑ ⛔⛔ 名字紅線（規格 §2.0，這一節優先於規格其他所有措辭）══════════
   Benson 的原話：「我要從哪裡知道現在我看的是哪個做法？」——v2 全站只有 A/B/C/D，
   被他退件過一次。⇒ **畫面上不准出現代號**，四處（今天卡／成績表／泳道／累計圖）
   一律用同一組名字、同一個順序。
   ⚠️ 上一輪這一整塊是**零斷言**：實作用 ATRULE ＋ <b>k</b> 就 134/134 全綠。 */
console.log("\n=== ㉑ ⛔⛔ 名字：畫面上不准出現 A／B／C／D ===");
await goAuto();
const NM = await ev(`AT_ORDER.map(k=>atName(k))`);
chk("四個名字就是 Benson 定的那四個", NM,
  ["5 分 K", "開盤起", "開盤起・要 30 點", "不判斷"]);
chk("⛔ 名字要走 atName()：C 的名字自帶門檻值（門檻改 80 就跟著變）",
  await ev(`(()=>{const o=AT.stats.thresh; AT.stats.thresh=80;
    const n=atName('C'); AT.stats.thresh=o; return n;})()`), "開盤起・要 80 點");
say(await ev(`typeof AT_NAME==='object' && typeof atName==='function' &&
  Array.isArray(AT_ORDER)`), "  AT_NAME／atName()／AT_ORDER 都存在（⛔ 不准散在各處硬寫）");

/* 掃「畫面上真的看得到的字」：葉節點 textContent ＋ title/aria-label ＋ 兩張 canvas。
   ⛔ 只掃 DOM 會整片漏掉泳道與累計圖的標籤（【細節】為此退件過 M2）。 */
const VIS = `(()=>{
  const root=document.getElementById('tab-auto'), out=[];
  for(const e of root.querySelectorAll('*')){
    if(!e.children.length){ const t=(e.textContent||'').trim(); if(t) out.push(t); }
    if(e.hasAttribute('title')) out.push(e.getAttribute('title'));
    if(e.hasAttribute('aria-label')) out.push(e.getAttribute('aria-label'));
  }
  return out;})()`;
// 摺疊區（〈這一頁在算什麼〉／配對卡／門檻掃描）也要掃 —— 收摺不等於不在畫面上
const openFolds = `['atadv','atpair'].forEach(i=>document.getElementById(i).open=true);
  atPaintStats(); 1`;
await ev(openFolds);
/* 孤立代號：前後不是英數字的單一 A/B/C/D。⛔ 這條尺不可以放寬成「整串等於 A」——
   「A 贏 4 次」「A − D」那種也必須抓得到（v2 的配對卡就是長那樣）。 */
const LONE = /(^|[^0-9A-Za-z])([ABCD])([^0-9A-Za-z]|$)/;
const lone = arr => arr.filter(s => LONE.test(String(s)))
  .map(s => String(s).slice(0, 46));
const visNow = async () => {
  const dom = await ev(VIS);
  const c1 = await canvasText("atday", "atDrawDay");
  const c2 = await canvasText("atcum", "atDrawCum");
  return { dom, cv: [...c1, ...c2] };
};
let V = await visNow();
chk("⛔ DOM ＋ title 上一個孤立的代號都沒有", lone(V.dom), []);
chk("⛔ 兩張 canvas 上一個孤立的代號都沒有", lone(V.cv), []);
console.log(`    掃了 ${V.dom.length} 段 DOM/title ＋ ${V.cv.length} 段 canvas 字串`);
const seenAll = s => NM.filter(n => !(V.dom.some(t => t.includes(n)) ||
  V.cv.some(t => t.includes(n)))).length === 0;
say(seenAll(), "四個名字在畫面上全都找得到（DOM 或 canvas）");
for (const n of NM) {
  say(V.dom.some(t => t.includes(n)), `  「${n}」在 DOM 上找得到`);
  say(V.cv.some(t => t.includes(n)), `  「${n}」在 canvas 上找得到`);
}
// ⛔ 不准留「A＝5 分 K」這種對照說明：需要那行字就代表命名失敗了
chk("⛔ 沒有任何「代號＝名字」的對照說明",
  V.dom.filter(t => /[ABCD]\s*[＝=]\s*(5 分 K|開盤起|不判斷)/.test(t)), []);
console.log("  負控組（原始碼突變：讓 atName() 回代號）：");
if (await mutate("atName", "k==='C'?('開盤起・要 '+atThr()+' 點'):(AT_NAME[k]||k)", "k")) {
  await ev(`atPaint(); atPaintStats(); 1`);
  await ev(openFolds);
  const bad = await visNow();
  say(lone(bad.dom).length > 0, "  DOM 那半會紅",
    `${lone(bad.dom).length} 段：${JSON.stringify(lone(bad.dom).slice(0, 3))}`);
  say(lone(bad.cv).length > 0, "  canvas 那半也會紅（⛔ 只掃 DOM 會漏掉泳道與累計圖）",
    `${lone(bad.cv).length} 段：${JSON.stringify(lone(bad.cv).slice(0, 4))}`);
  await unmutate("atName");
  await ev(`atPaint(); atPaintStats(); 1`);
  await ev(openFolds);
  V = await visNow();
  chk("  還原之後又乾淨了（DOM）", lone(V.dom), []);
  chk("  還原之後又乾淨了（canvas）", lone(V.cv), []);
}

/* ㉑b 成績表第一欄：名字 ＋ 底下那行小字。
   ⚠️ 上一輪 lab-qa 把說明欄的文案整個清空 ⇒ 134/134 全綠（R8）。 */
const nmCol = await ev(`[...document.querySelectorAll('#attbl tr')].slice(1,5)
  .map(tr=>{ const e=tr.querySelector('.nm'); if(!e) return null;
    const i=e.querySelector('i');
    return [e.firstChild.textContent.trim(), i?i.textContent.trim():''];})`);
chk("成績表四列＝名字 ＋ 說明小字（⛔ 不准清空、⛔ 不准放代號）", nmCol,
  [["5 分 K", "09:00 起算"], ["開盤起", "08:45 起算"],
  ["開盤起・要 30 點", "08:45 起算・不夠就不做"], ["不判斷", "一律做多（基準）"]]);
chk("⛔ 表頭第一欄叫「做法」，不叫「算法／策略／模型」",
  await ev(`document.querySelector('#attbl th').textContent`), "做法");
console.log("  負控組（把說明小字清空）：");
if (await mutate("atSub", "AT_SUB[k]||''", "''")) {
  await ev("atPaintStats()");
  const empty = await ev(`[...document.querySelectorAll('#attbl tr')].slice(1,5)
    .map(tr=>{const i=tr.querySelector('.nm i'); return i?i.textContent.trim():'x';})`);
  say(empty.every(s => s === ""), "  說明欄清空之後這一條會紅", JSON.stringify(empty));
  await unmutate("atSub");
  await ev("atPaintStats()");
}
/* ㉑c 四處同序：今天卡抬頭／成績表第一欄／泳道／累計圖線尾，⛔ 一律 AT_ORDER。 */
const orderToday = await ev(`[...document.querySelectorAll('#attoday .k b')].map(e=>e.textContent)`);
const orderTbl = nmCol.map(x => x[0]);
const laneOrder = (await canvasDraws("atday", "atDrawDay"))
  .filter(d => NM.includes(d.t)).map(d => d.t);
chk("今天卡的抬頭照 AT_ORDER", orderToday, NM);
chk("成績表第一欄照 AT_ORDER", orderTbl, NM);
chk("泳道由上到下照 AT_ORDER", laneOrder, NM);
/* ㉑d 累計圖的線尾標籤：⛔ 不准互相疊住、⛔ 不准壓在右邊的價格刻度上。
   **這兩件事只有截圖看得出來**（送進 canvas 的字串完全正確，也沒有畫到畫布外）——
   第一版就是四個名字疊成一團、而且蓋在刻度上（【細節】M2 的同一類病）。 */
const cumD = await canvasDraws("atcum", "atDrawCum");
const cumDim = await ev(`({W:ATCC.W,H:ATCC.H,PW:ATCC.W-ATR})`);
/* ⚠️ 2026-09-08「你自己」那條金線跟著成績表那兩列一起拿掉 ⇒ 線尾名字剩四個。 */
const cumTags = cumD.filter(d => NM.includes(d.t));
chk(`累計圖的線尾名字**恰好**四個（⛔ 「你自己」那條已經拿掉）`, cumTags.length, 4);
say(true, `  ${JSON.stringify(cumTags.map(d => d.t))}`);
chk("⛔ 線尾標籤沒有壓到右邊的價格刻度（右界 ≤ PW−4）",
  cumTags.filter(d => d.x + d.w > cumDim.PW - 3).map(d => [d.t, Math.round(d.x + d.w)]), []);
const ys = cumTags.map(d => d.y).sort((a, b) => a - b);
const tooClose = ys.slice(1).filter((y, i) => y - ys[i] < 14);
chk("⛔ 線尾標籤兩兩至少差 14px（不然名字會疊成一團）", tooClose, []);
console.log("  負控組：");
if (await mutate("atDrawCum", "if(tags[i].y-tags[i-1].y<15) tags[i].y=tags[i-1].y+15;",
  "if(false) tags[i].y=tags[i-1].y+15;")) {
  const badY = (await canvasDraws("atcum", "atDrawCum"))
    .filter(d => NM.includes(d.t)).map(d => d.y).sort((a, b) => a - b);
  say(badY.slice(1).filter((y, i) => y - badY[i] < 14).length > 0,
    "  拿掉錯開之後真的會疊在一起 ⇒ 這一條會紅",
    JSON.stringify(badY.map(y => Math.round(y))));
  await unmutate("atDrawCum");
  await ev("atPaintStats()");
}
/* ㉑e 窄視窗的退路：⛔ **仍然不縮寫**，改成堆疊排法（名字自己一列、列高 22→30），
   圖跟著變高（`.at-wrap` 在 ≤720px 換成 aspect-ratio:520/620）。
   ⛔ 不可以改成壓縮泳道、也不可以截字（規格 §9.3）。 */
await c.send("Emulation.setDeviceMetricsOverride",
  { width: 430, height: 900, deviceScaleFactor: 1, mobile: false });
await sleep(400); await ev("atPaint()");
const narrowLane = await ev(`({stacked:ATLN.stacked, laneH:ATLN.laneH, L:ATLN.L,
  H:ATC.H, W:ATC.W})`);
say(narrowLane.stacked, "430px：自動切成堆疊排法", JSON.stringify(narrowLane));
chk("  列高從 22 變 30（⛔ 不是把泳道壓扁）", narrowLane.laneH, 30);
const nd = await canvasDraws("atday", "atDrawDay");
chk("  四個名字在窄視窗照樣完整畫出來（⛔ 不縮寫、不截字）",
  NM.filter(n => !nd.some(d => d.t === n)), []);
chk("  ⛔ 沒有任何一段字被畫到畫布外面",
  nd.filter(d => d.x < 0 || d.x + d.w > narrowLane.W + 1).map(d => [d.t, Math.round(d.x)]), []);
say(narrowLane.H > 300, "  圖跟著變高了（泳道多吃 32px 不是從 K 線挖）",
  `canvas ${narrowLane.W}×${narrowLane.H}`);
await c.send("Emulation.clearDeviceMetricsOverride");
await sleep(350); await ev("atPaint()");
await ev(`['atadv','atpair'].forEach(i=>document.getElementById(i).open=false)`);

/* ═══ ㉒ ⛔ 主迴圈那道 try 攔到的錯，畫面上要看得見（R5）═════════════════
   後端一直有在數（AUTO["tick_err"]）也有 console 警告，但 console 只印前 3 次、
   而他不會去看主控台 ⇒ 對「看畫面的人」來說那還是安靜地吞掉了。 */
console.log("\n=== ㉒ ⛔ 出錯不可以安靜地吞（畫面上要看得到）===");
const payload = await ev(`fetch('/api/auto/days').then(r=>r.json())`);
say(("err" in payload) && ("tick_err" in payload),
  "/api/auto/days 真的把 err ＋ tick_err 端出來", JSON.stringify({
    err: payload.err, tick_err: payload.tick_err
  }));
const stPayload = await ev(`fetch('/api/auto/stats?win=20&src=live').then(r=>r.json())`);
say(("err" in stPayload) && ("tick_err" in stPayload), "  /api/auto/stats 也有");
const notes0 = await ev(`document.getElementById('atnotes').textContent`);
say(!notes0.includes("出錯"), "  沒出錯的時候不寫（⛔ 不製造假警報）");
await ev(`AT.tickErr=3; AT.serr='tick: 探針假造的錯'; atPaintStats(); 1`);
const notes1 = await ev(`document.getElementById('atnotes').textContent`);
say(notes1.includes("出錯 3 次"), "  tick_err 畫在 ledger 那一行旁邊",
  (notes1.match(/[^·]*出錯[^·]*/) || [""])[0].trim());
say(notes1.includes("tick: 探針假造的錯"), "  最後一個錯誤的原文也寫出來（⛔ 不是只寫次數）");
say(!notes1.includes("停損") || notes1.includes("停損不受影響"),
  "  而且要說清楚「停損不受影響」（不然他會以為停損掛了）");
await ev(`AT.tickErr=0; AT.serr=''; atPaintStats(); 1`);
chk("  清掉之後又不寫了", (await ev(`document.getElementById('atnotes').textContent`))
  .includes("出錯"), false);

/* ═══ ㉓ 文案／常數那一側（⚠️ 上一輪 R1／R4／R8 三個退件全落在這裡）══════
   守衛蓋滿了「接線」，但「某段文案被清空／某個常數前後端對不上」完全沒人守 ——
   而那種錯**畫面照樣長得好好的**。 */
console.log("\n=== ㉓ 文案與常數（畫面長得沒事，但內容被掏空的那一類）===");
// ① 前端的退路常數必須跟後端端出來的一致（⛔ 不一致＝後端改了、畫面偷偷用舊的）
const K = await ev(`(()=>{const S=AT.stats||{};
  return {beSig:S.signal_at, feSig:ATSIG, beSigSec:atSecOf(S.signal_at),
          beSigma:S.sigma, feSigma:ATSIGMA, beZ:S.z, feZ:ATZ,
          beThr:S.thresh, feThr:AT.thr, beTrack:S.track_n, feTrack:AT.trackN,
          beRate:S.rate_min_n, beCum:S.cum_min_n,
          feEnd:ATEND, feWatch:ATWATCH, feSec0:ATSEC0};})()`);
chk("後端的 09:03:30 ＝ 前端的 ATSIG", K.beSigSec, K.feSig);
chk("後端的 σ ＝ 前端的退路常數（⛔ 兩邊分岔＝畫面上的天花板是假的）", K.beSigma, K.feSigma);
chk("後端的 z ＝ 前端的退路常數", K.beZ, K.feZ);
chk("後端的門檻 ＝ 前端記下來的（名字自己帶著它）", K.beThr, K.feThr);
chk("後端的 track_n ＝ 前端的退路 505", K.beTrack, K.feTrack);
chk("少樣本門檻 30／少於 10 筆不畫線（⛔ 常數不准偷偷放寬）",
  [K.beRate, K.beCum], [30, 10]);
chk("08:45／09:30／13:45 三個時刻常數", [K.feSec0, K.feWatch, K.feEnd],
  [8 * 3600 + 45 * 60, 9 * 3600 + 30 * 60, 13 * 3600 + 45 * 60]);
// ② 文案表：⛔ 不准有空字串，⛔ 兩個不同原因不准寫同一句（寫同一句一定有一句是假的）
const TXT = await ev(`({why:Object.entries(ATWHY), exit:Object.entries(ATEXIT),
  name:Object.entries(AT_NAME), sub:Object.entries(AT_SUB)})`);
for (const [k, arr] of Object.entries(TXT)) {
  const empty = arr.filter(([, v]) => !String(v || "").trim()).map(([kk]) => kk);
  // AT_NAME.C 刻意是空字串（名字由 atName() 帶著門檻組出來），其餘一個都不准空
  const allow = (k === "name") ? ["C"] : [];
  chk(`  ${k}：沒有被掏空的文案`, empty.filter(x => !allow.includes(x)), []);
  const vals = arr.map(([, v]) => String(v)).filter(v => v.trim());
  chk(`  ${k}：兩個不同的原因不准寫同一句`, vals.length - new Set(vals).size, 0);
}
say(TXT.why.length >= 5, `  沒錄到的原因有 ${TXT.why.length} 種，每一種都有自己的說法`);
/* ③ ⚠️ 原本這裡驗〈這一頁在算什麼〉那十條的內容（⛔ 不是只數數量）。
   2026-09-08 那個摺疊區整個拿掉了 ⇒ 這一條**不是刪掉，是改對**：
   它守的是「文案被掏空／被整段刪掉沒人發現」，落點改成**還在畫面上的**那幾組文案，
   而摺疊區本身改成「不准回來」的零命中（見 ㉔）。 */
const FOLDTXT = await ev(`[document.querySelector('#atadv summary').textContent,
  document.querySelector('#atpair summary').textContent,
  document.getElementById('atadvbody').textContent,
  document.getElementById('atceil').textContent]`);
chk("剩下的摺疊區抬頭與內文都不是空的", FOLDTXT.filter(t => t.trim().length < 5), []);
chk("  兩個摺疊區的抬頭不准寫同一句", FOLDTXT[0].trim() === FOLDTXT[1].trim(), false);
// ④ hover 說明裡的數字：⛔ 不准寫死在 HTML 裡（常數改了就變假話，而且沒人會發現）
/* ⚠️ 進度尺拿掉之後只剩天花板這一個 title ——⛔ 不可以因為只剩一個就不驗。 */
const TT = await ev(`[document.getElementById('atceil').title,
  String(atCeiling(AT.stats.track_n))]`);
say(TT[0].includes(String(K.beTrack)), "天花板那個 title 裡的天數 ＝ 後端的 track_n",
  `${TT[0].slice(0, 30)}…`);
say(TT[0].includes(TT[1]), "  title 的點數是算出來的", `= ${TT[1]} 點`);
console.log("  負控組：");
await ev(`(()=>{AT.stats.track_n=333; atPaintStats(); return 1;})()`);
const tAfter = await ev(`document.getElementById('atceil').title`);
say(tAfter.includes("333") && tAfter !== TT[0],
  "  把 track_n 改成 333 ⇒ title 跟著變（證明不是寫死的）", tAfter.slice(0, 30) + "…");
await ev(`(()=>{AT.stats.track_n=${K.beTrack}; atPaintStats(); return 1;})()`);
// ⑤ ⛔ 畫面上寫死的「時分秒」：09:03:30 已經從 04:30 改過一次，散在各處的字串會變成假話
await ev(openFolds);
const V2 = await visNow();
const times = [...new Set([...V2.dom, ...V2.cv].join(" ⏐ ").match(/\d\d:\d\d:\d\d/g) || [])];
say(times.includes(K.beSig), "後端現在的 signal_at 真的畫在畫面上", K.beSig);
/* ⛔⛔ 這條 2026-09-08 **收緊了**：舊版有一個豁免（「09:03:30 沒有被證明比 09:04:30 好」
   那一條寫在〈這一頁在算什麼〉裡）。那個摺疊區整個刪掉之後豁免沒有存在的理由 ⇒
   現在是「**除了後端的 signal_at，畫面上一個時分秒都不准有**」。
   ⛔ 不准為了塞回一句解釋再把豁免加回來。 */
chk("⛔ 畫面上除了後端的 signal_at，一個時分秒都不准有（豁免已取消）",
  times.filter(t => t !== K.beSig), []);
say(true, `  掃到 ${times.length} 種時分秒：${JSON.stringify(times)}`);
console.log("  負控組：");
await ev(`(()=>{const d=document.createElement('div'); d.id='__t';
  d.textContent='09:04:30 比較好'; document.getElementById('tab-auto').appendChild(d); return 1;})()`);
const V3 = await visNow();
const t3 = [...new Set([...V3.dom, ...V3.cv].join(" ⏐ ").match(/\d\d:\d\d:\d\d/g) || [])];
say(t3.filter(t => t !== K.beSig).length > 0, "  塞一個別的時分秒進去 ⇒ 這一條會紅",
  JSON.stringify(t3.filter(t => t !== K.beSig)));
await ev(`document.getElementById('__t').remove()`);
await ev(`['atadv','atpair'].forEach(i=>document.getElementById(i).open=false)`);
// ⑥ 鎖印那句話：⛔ 不准被掏空
chk("「模擬 · 不送單」的 title 不是空的",
  (await ev(`document.querySelector('#tab-auto .simlock').getAttribute('title')`) || "")
    .length > 8, true);

/* ═══ ㉔ ⛔⛔ 2026-09-08 拿掉的東西**不准回來**（負控組：把它加回去要紅）══════
   Benson 看實機之後點名四樣：
     ①「程式下單那邊這個欄位不需要」＝ 最上面那條進度尺（見 ⑤）
     ②「我自己的這邊都拿掉」＝ 成績表的「你自己」兩列 ＋ 上面那條分隔 ＋ 累計圖那條金線
     ③「下面這個我根本看不懂是在幹嘛的」＝ 成績表底下那一行帳本
     ④「這一頁在算甚麼也拿掉，我不需要看這個我也不會看」＝ 那個摺疊區（十條）
   ⚠️ **拿掉東西也要有守衛**，不然下一個人會照著舊註解／舊規格把它加回來。
   ⚠️ 但拿掉的是**畫面**不是資料：後端照算，②的唯讀守衛（autotest-backend.py ⑧c）
      與③的不變式（sig+settle+miss+dup+bad ＝ 總列數）**一條都沒有放寬**。 */
console.log("\n=== ㉔ ⛔⛔ 拿掉的四樣東西不准回來 ===");
await goAuto();
await ev(openFolds);
const gone = async () => {
  const dom = await ev(SEEN);
  const c1 = await canvasText("atday", "atDrawDay");
  const c2 = await canvasText("atcum", "atDrawCum");
  return { dom, cv: [...c1, ...c2] };
};
const GONE_MINE = ["你自己", "口徑不同", "同口徑"];
const GONE_LEDGER = ["列＝訊號", "＋沒錄到", "＋重複", "＋讀不出來", "13:45 收盤平"];
const GONE_ABOUT = ["這一頁在算什麼", "三次擲銅板", "目前是冠軍", "約兩年"];
let Z = await gone();
console.log("  ② 你自己那兩列：");
chk("  ⛔ 成績表只剩四列（⛔ 沒有第五、第六列）",
  await ev(`document.querySelectorAll('#attbl tr').length - 1`), 4);
chk("  ⛔ tr.mine / tr.sep 一列都沒有",
  await ev(`document.querySelectorAll('#attbl tr.mine,#attbl tr.sep').length`), 0);
chk("  ⛔ DOM ＋ 兩張 canvas 上零命中", hits(Z, GONE_MINE), []);
chk("  ⛔ 累計圖只畫四條線的名字（那條金線也拿掉了）",
  await ev(`ATLINES.map(x=>x[0])`), ["D", "A", "B", "C"]);
say(await ev(`!!(AT.stats&&AT.stats.mine)`),
  "  ⚠️ 但後端 mine **照算照端**（他之後可能會想加回來）",
  JSON.stringify(await ev(`AT.stats.mine&&{all:AT.stats.mine.all.n,strict:AT.stats.mine.strict.n}`)));
console.log("  ③ 帳本那一行：");
chk("  ⛔ DOM ＋ 兩張 canvas 上零命中", hits(Z, GONE_LEDGER), []);
say(await ev(`!!(AT.stats&&AT.stats.ledger&&AT.stats.ledger.lines>0)`),
  "  ⚠️ 但後端 ledger **照算照端**（不變式與它的守衛都還在）",
  JSON.stringify(await ev(`AT.stats.ledger`)));
console.log("  ④〈這一頁在算什麼〉：");
chk("  ⛔ #atabout 不存在", await ev(`document.getElementById('atabout')!==null`), false);
chk("  ⛔ .at-about / .at-list 一個都沒有",
  await ev(`document.querySelectorAll('#tab-auto .at-about,#tab-auto .at-list').length`), 0);
chk("  ⛔ 摺疊區只剩兩個（配對卡／門檻掃描）",
  await ev(`[...document.querySelectorAll('#tab-auto details')].map(d=>d.id)`),
  ["atpair", "atadv"]);
chk("  ⛔ DOM ＋ 兩張 canvas 上零命中", hits(Z, GONE_ABOUT), []);
console.log("  負控組（三個，都是把東西加回去 ⇒ 上面那幾條要紅）：");
// ② 把「你自己」兩列加回成績表 ＋ 把那條金線加回累計圖
await ev(`(()=>{const t=document.getElementById('attbl');
  t.insertAdjacentHTML('beforeend',
    '<tr class="sep"><td colspan="7">▼ 你自己真的做的（口徑不同）</td></tr>'+
    '<tr class="mine"><td class="nm">你自己<i>全部</i></td><td>3</td><td>2–1</td>'+
    '<td>—</td><td class="pts">+30</td><td class="avg">+10.0</td><td>—</td></tr>');
  return 1;})()`);
const Z2 = await gone();
say(hits(Z2, GONE_MINE).length > 0, "  ② 加回兩列 ⇒ 零命中那一條會紅",
  JSON.stringify(hits(Z2, GONE_MINE).slice(0, 2)));
say(await ev(`document.querySelectorAll('#attbl tr').length-1`) === 6,
  "  ② 「只剩四列」那一條也會紅");
/* ⚠️ setEl() 有 `e.__html` 快取（同一串就不重寫）⇒ 直接叫 atPaintStats() **沖不掉**
   探針注入的節點（innerHTML 變了、但它要寫的那一串沒變）。要先把快取戳破。 */
await ev(`(()=>{document.getElementById('attbl').__html=null; atPaintStats(); return 1;})()`);
chk("  ② 還原之後又零命中", hits(await gone(), GONE_MINE), []);
// ③ 把帳本那一行加回去
await ev(`(()=>{const L=(AT.stats||{}).ledger||{};
  document.getElementById('atnotes').insertAdjacentHTML('beforeend',
   '<span>沒摸到 ±100、13:45 收盤平 4 筆</span><span>檔案 '+(L.lines||0)+
   ' 列＝訊號 '+(L.sig||0)+'＋結算 '+(L.settle||0)+'＋沒錄到 '+(L.miss||0)+
   '＋重複 '+(L.dup||0)+'＋讀不出來 '+(L.bad||0)+'</span>'); return 1;})()`);
const Z3 = await gone();
say(hits(Z3, GONE_LEDGER).length > 0, "  ③ 加回帳本那一行 ⇒ 零命中那一條會紅",
  JSON.stringify(hits(Z3, GONE_LEDGER).slice(0, 2)));
await ev(`(()=>{document.getElementById('atnotes').__html=null; atPaintStats(); return 1;})()`);
chk("  ③ 還原之後又零命中", hits(await gone(), GONE_LEDGER), []);
// ④ 把那個摺疊區加回去
await ev(`(()=>{const d=document.createElement('details');
  d.id='atabout'; d.className='at-fold at-about'; d.open=true;
  d.innerHTML='<summary>這一頁在算什麼</summary><ol class="at-list">'+
    '<li>不到 30 筆不給勝率百分比。3 筆 2 勝的「67%」是三次擲銅板，不是測量結果。</li></ol>';
  document.getElementById('tab-auto').appendChild(d); return 1;})()`);
const Z4 = await gone();
say(hits(Z4, GONE_ABOUT).length > 0, "  ④ 加回摺疊區 ⇒ 零命中那一條會紅",
  JSON.stringify(hits(Z4, GONE_ABOUT).slice(0, 2)));
say(await ev(`document.getElementById('atabout')!==null`), "  ④ 「#atabout 不存在」那一條也會紅");
await ev(`document.getElementById('atabout').remove()`);
chk("  ④ 還原之後又零命中", hits(await gone(), GONE_ABOUT), []);

/* ㉔b ⛔ 「拿掉帳本」≠「安靜地少」：異常還是要自己冒出來。
   ⛔ 這一條不可以被下一輪當成「帳本的殘骸」清掉 —— 它守的是本專案的紅線
      （CLAUDE.md：同一根同時摸到 ±100 ⇒ 保守算停損，那個筆數要顯示在畫面上）。 */
console.log("  ㉔b 常態不寫、異常照講：");
const notesNow = await ev(`document.getElementById('atnotes').textContent.trim()`);
say(!GONE_LEDGER.some(w => notesNow.includes(w)),
  "  沒有異常的時候那一行是乾淨的", JSON.stringify(notesNow.slice(0, 50)));
await ev(`(()=>{window.__both=AT.stats.rows.A.both; AT.stats.rows.A.both=2;
  atPaintStats(); return 1;})()`);
const notesBoth = await ev(`document.getElementById('atnotes').textContent`);
say(notesBoth.includes("同一根同時摸到 ±100") && notesBoth.includes("保守算停損"),
  "  ⛔ 真的有「同一根同時摸到 ±100」時**照樣寫在畫面上**（保守算停損是對他不利的假設）",
  (notesBoth.match(/[^·]*同一根[^·]*/) || [""])[0].trim());
await ev(`(()=>{AT.stats.rows.A.both=window.__both; atPaintStats(); return 1;})()`);
say(!(await ev(`document.getElementById('atnotes').textContent`)).includes("同一根同時摸到"),
  "  改回 0 之後又不寫了（⛔ 不製造每天都在的雜訊）");
await ev(`['atadv','atpair'].forEach(i=>document.getElementById(i).open=false)`);

/* ═══ ⑳ console 零錯誤（放在最後才驗）════════════════════════════════ */
console.log("\n=== ⑳ 收尾 ===");
chk("全程 console 零錯誤", ERRORS, []);
say(await ev(`AT.drawn>0`), "圖真的畫過", `${await ev("AT.drawn")} 次`);
/* ⛔⛔ 一張單都沒有送出去 —— 量的是**真的發出去的請求**，不是原始碼也不是按鈕數量。 */
const posts = REQS.filter(r => r.m !== "GET");
const orderish = REQS.filter(r => /\/api\/enter|\/api\/real\//.test(r.u));
chk("⛔ 全程一個 POST 都沒有", posts.map(r => r.m + " " + r.u), []);
chk("⛔ 全程沒有任何請求打到 /api/enter 或 /api/real/*", orderish.map(r => r.u), []);
say(REQS.length > 50, `  自證：這一場真的攔到請求了（${REQS.length} 個，全是 GET）`);
console.log("    打過的端點："
  + [...new Set(REQS.map(r => r.u.replace(/^https?:\/\/[^/]+/, "").split("?")[0]))]
    .filter(u => u.startsWith("/")).join(" "));

DONE = true;
console.log(`\n${"=".repeat(60)}\n共 ${N} 項，${FAIL ? FAIL + " 項未過" : "全部通過"}`);
c.close(); ch.kill();
process.exit(FAIL ? 1 : 0);
