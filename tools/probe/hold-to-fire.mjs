/*
  長按送單那顆按鈕的真滑鼠探針（36 項）。

  2026-09-01 右欄改成「練習／真實」兩個分頁之後，這支多守兩件事：
    - 站在練習分頁時，畫面上**一顆真實下單鈕都沒有**（結構上的防誤按）
    - 站在練習分頁時，真實那一邊出事（報價中斷）**照樣看得到**
  選擇器跟著新版改（.card.real → .n-zone.z-real、.rtrow → .n-row …），
  但每一項斷言的**意思一個字都沒放寬**。

  ================================================================
  為什麼一定要真滑鼠
  ================================================================
  這一段的 bug 全都藏在「事件的細節」裡，用 dispatchEvent 造假事件測不出來：
    - `mouseleave` **不冒泡**，造假事件加 bubbles:true 會得到假紅燈（CLAUDE.md 有記）。
    - `e.button` 要真的分得出左鍵與右鍵。
    - 長按期間卡片每 0.5 秒會重繪一次，按鈕會被銷毀 —— 只有真的按住 650ms 才踩得到。
  所以用 `Input.dispatchMouseEvent`（CDP），跟 press-scan.mjs 同一招。

  ================================================================
  安全性
  ================================================================
  **不連永豐、不碰 8770（他正在用的面板）。** 這支只打治具伺服器，
  治具的 POST 端點只記錄「前端送了幾次」，一張單都不會出去。
  治具要先跑起來（scratchpad/fe_harness.py），預設 8771 / 控制埠 8772。

  怎麼跑：
      node tools/probe/hold-to-fire.mjs
      node tools/probe/hold-to-fire.mjs --url=http://127.0.0.1:8771 --ctl=8772
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
const DEV = Number(A.dev || 9814);
const HOLD = 650;                      // live_panel.py 的 HOLD_MS

const sleep = ms => new Promise(r => setTimeout(r, ms));
let FAIL = 0;
const chk = (name, got, want) => {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) FAIL++;
  console.log((ok ? "  OK   " : "  FAIL ") + name + (ok ? "" : `  (得到 ${JSON.stringify(got)}，期待 ${JSON.stringify(want)})`));
};

const ctl = async p => (await fetch(`http://127.0.0.1:${CTL}${p}`)).json();
const posts = async () => (await ctl("/posts")).posts;

const profile = fs.mkdtempSync(path.join(os.tmpdir(), "hold-probe-"));
// ⚠️ 視窗一定要開夠大。headless 預設 800x600，面板的下單卡會被擠到 y=2306 ——
//    座標落在 viewport 外面，`Input.dispatchMouseEvent` **打不到任何元素**，
//    於是「沒送出單」那幾個測項全部變成假綠燈（第一版就是這樣，兩項白過）。
//    用啟動旗標開大，不要用 Emulation.setDeviceMetricsOverride（實測會卡住不回）。
const ch = spawn(CHROME, ["--headless=new", "--remote-debugging-port=" + DEV,
  "--user-data-dir=" + profile, "--no-first-run", "--no-default-browser-check",
  "--hide-scrollbars", "--window-size=1400,1000", "about:blank"],
  { stdio: "ignore", shell: false });
for (let i = 0; i < 200; i++) {
  try { await fetch(`http://127.0.0.1:${DEV}/json/version`); break; } catch { await sleep(100); }
}
const c = await CDP.attach(DEV);
await c.send("Page.enable");
await c.send("Runtime.enable");

const evalJS = async expr => (await c.send("Runtime.evaluate",
  { expression: expr, awaitPromise: true, returnByValue: true })).result.value;

// ⚠️ 一定要接 alert。面板送單成功但停利沒掛上時會 alert()，
//    而 alert 會**把整個頁面的 JS 卡住** ⇒ 之後每一個 Runtime.evaluate 都不會回，
//    探針就這樣無聲掛死在下一個測項（第一版跑到 ④ 就停住，看起來像當機）。
const DIALOGS = [];
c.on("Page.javascriptDialogOpening", async p => {
  DIALOGS.push(p.message);
  await c.send("Page.handleJavaScriptDialog", { accept: true });
});

await c.send("Page.navigate", { url: URL_ });
await sleep(2500);

// 右欄現在是兩個分頁，預設停在「練習」（老闆拍板：不自動切）。
// 真實下單開關住在真實那一區裡，所以要先切過去。
async function goTab(t) {
  await evalJS(`(()=>{const b=document.querySelector('[data-rtab="${t}"]'); if(b) b.click();})()`);
  await sleep(900);
}
await goTab("real");
// 打開「真實下單」開關（預設是關的，而且刻意不記憶狀態）
await evalJS(`document.querySelector('[data-rt]').click()`);
await sleep(900);

async function box(sel) {
  // 先捲到看得見，再量。量完**自己驗一次**座標真的落在畫面內、而且那個點上面就是目標 ——
  // 這把尺量錯的話，整份報告會全是假綠燈（第一版兩項就是這樣白過的）。
  const r = await evalJS(`(()=>{const e=document.querySelector(${JSON.stringify(sel)});
    if(!e) return null; e.scrollIntoView({block:'center'});
    const b=e.getBoundingClientRect();
    const x=Math.round(b.left+b.width/2), y=Math.round(b.top+b.height/2);
    return {x, y, w:Math.round(b.width), h:Math.round(b.height),
            vw:innerWidth, vh:innerHeight,
            onTop: !!document.elementFromPoint(x,y)?.closest('[data-rdir]')};})()`);
  if (!r) return null;
  if (r.x < 0 || r.y < 0 || r.x > r.vw || r.y > r.vh) {
    throw new Error(`按鈕在畫面外 (${r.x},${r.y})，viewport 只有 ${r.vw}x${r.vh} —— 按不到，綠燈都是假的`);
  }
  if (!r.onTop) {
    throw new Error(`(${r.x},${r.y}) 這一點上面不是那顆按鈕（被別的東西蓋住）—— 按下去打不到目標`);
  }
  await sleep(150);          // 等 scrollIntoView 停下來
  return r;
}

async function press(sel, { button = "left", ms = HOLD + 250, release = true } = {}) {
  const p = await box(sel);
  if (!p) throw new Error("找不到 " + sel);
  if (p.h < 20 || p.w < 40) {
    throw new Error(`按鈕被壓成 ${p.w}x${p.h} —— 版面不對，測了也不算數`);
  }
  const buttons = button === "left" ? 1 : 2;
  await c.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: p.x, y: p.y, button: "none", buttons: 0 });
  await c.send("Input.dispatchMouseEvent", { type: "mousePressed", x: p.x, y: p.y, button, buttons, clickCount: 1 });
  await sleep(ms);
  if (release) {
    await c.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: p.x, y: p.y, button, buttons: 0, clickCount: 1 });
  }
  return p;
}

console.log(`\n治具 ${URL_}（假狀態，沒有連永豐）\n長按門檻 ${HOLD}ms\n`);

console.log("=== ① 按住右鍵 900ms：一張單都不准出去 ===");
await ctl("/reset");
await ctl("/mode/flat");
await sleep(700);
await press('[data-rdir="long"]', { button: "right", ms: 900 });
await sleep(600);
chk("  右鍵長按沒有送出任何單", await posts(), []);

console.log("\n=== ② 按住左鍵不到門檻就放開：不准出去 ===");
await ctl("/reset");
await sleep(700);
await press('[data-rdir="long"]', { ms: HOLD - 300 });
await sleep(800);
chk("  按 350ms 就放開 → 沒送單", await posts(), []);

console.log("\n=== ③ 按住左鍵超過門檻：送出一張，而且只有一張 ===");
await ctl("/reset");
await ctl("/mode/flat");
await sleep(700);
await press('[data-rdir="long"]', { ms: HOLD + 350 });
await sleep(1200);
const p3 = await posts();
chk("  送出一張", p3.length, 1);
chk("  打的是真實下單端點、方向是做多", p3[0], ["/api/real/enter", { dir: "long" }]);
// 治具回的是「進場成功、但停利沒掛上」——這種**一定要跳出來**，不能無聲成功
chk("  停利沒掛上有跳警告給他看", /停利單沒掛上去/.test(DIALOGS.join("|")), true);

console.log("\n=== ④ 長按期間卡片會每 0.5 秒重繪，按鈕不可以被換掉 ===");
// 這是 09-01 他回報「長按按到一半自己取消」的那個 bug：卡片重繪把按住的按鈕銷毀了。
await ctl("/reset");
await ctl("/mode/flat");
await sleep(700);
const pos4 = await box('[data-rdir="short"]');
// ⛔ 這一項以前是**結構上永遠成立**的假斷言（lab-qa 2026-09-01 用變異測試抓到）：
//    舊寫法在按到一半時 `querySelector` **重新查一次 DOM** —— 按鈕就算被重繪換掉了，
//    查到的也是新生出來的那一顆，`contains()` 當然為真。
//    把 `if(!holdingNow) paintRight(...)` 這道守衛整個刪掉（＝09-01 那個 bug 完整復活），
//    35 項照樣全綠、什麼都抓不到。
//    正確做法：**在按下去之前**先掛監聽，把 mousedown 當下的那一顆節點抓住，
//    之後驗的是同一顆還在不在。（監聽一定要在 mousePressed 之前掛，不然抓不到。）
await evalJS(`window.__held=null;
  document.addEventListener('mousedown', function(e){
    var b=e.target.closest && e.target.closest('[data-rdir]'); if(b) window.__held=b;
  }, true); true`);
await c.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: pos4.x, y: pos4.y, button: "none", buttons: 0 });
await c.send("Input.dispatchMouseEvent", { type: "mousePressed", x: pos4.x, y: pos4.y, button: "left", buttons: 1, clickCount: 1 });
await sleep(HOLD - 120);
const alive = await evalJS(`(()=>({
  captured: !!window.__held,
  inDoc: !!window.__held && document.body.contains(window.__held),
  holding: !!window.holdingNow}))()`);
// 尺的自證：沒抓到節點的話下面那項會變成「假的通過」，所以先驗抓到了
chk("  （尺自證）真的抓到按下去的那一顆節點", alive.captured, true);
chk("  按到一半，按住的**那一顆**按鈕還在（不是被換掉的新的）", alive.inDoc, true);
chk("  面板知道自己正在長按（重繪要被擋住）", alive.holding, true);
await sleep(400);
await c.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: pos4.x, y: pos4.y, button: "left", buttons: 0, clickCount: 1 });
await sleep(1200);
chk("  撐過重繪，單子有出去", (await posts()).length, 1);

console.log("\n=== ⑤ 送出中連按第二下：只准有一張在路上 ===");
await ctl("/reset");
await ctl("/mode/flat");
await fetch(`http://127.0.0.1:${CTL}/slow`).catch(() => {});
await sleep(700);
await press('[data-rdir="long"]', { ms: HOLD + 300 });      // 第一張
await sleep(120);                                            // 券商還沒回報
const btnState = await evalJS(`(()=>{const b=document.querySelector('[data-rdir="long"]');
  return {firing: !!window.firing, disabled: !!(b&&b.disabled)};})()`);
chk("  送出中 firing 旗標立起來", btnState.firing, true);
await press('[data-rdir="long"]', { ms: HOLD + 300 });      // 手快再來一下
await sleep(1500);
chk("  連按第二下沒有變成第二張單", (await posts()).length, 1);

console.log("\n=== ⑥ 停利沒掛上去，卡片要照實說 ===");
await ctl("/mode/holding");        // has_target=false
await sleep(1200);
const holdTxt = await evalJS(`document.querySelector('.n-zone.z-real').innerText`);
chk("  有寫「沒掛上」", /沒掛上/.test(holdTxt), true);
chk("  不可以還寫「已掛在券商」", /已掛在券商/.test(holdTxt), false);
await ctl("/mode/with_target");    // has_target=true
await sleep(1200);
const okTxt = await evalJS(`document.querySelector('.n-zone.z-real').innerText`);
chk("  真的掛上時才寫「已掛在券商」", /已掛在券商/.test(okTxt), true);

console.log("\n=== ⑦ 卡片要看得出載入的是哪一版程式 ===");
// 版本那一行在「空手」那張卡上（有部位時卡片長得不一樣）
await ctl("/mode/flat");
await sleep(1300);
const flatTxt = await evalJS(`document.querySelector('.n-zone.z-real').innerText`);
chk("  有印出 broker.py 的版本與啟動時間", /程式 \S+ \S+.啟動 \S+ \S+/.test(flatTxt), true);
if (!/程式 \S+ \S+.啟動 \S+ \S+/.test(flatTxt)) console.log("    卡片實際內容：\n      " + flatTxt.replace(/\n/g, "\n      "));

console.log("\n=== ⑧ 今天的真實交易成績單（固定欄 ＋ 會捲不會壓扁）===");
// ⚠️ 新版是固定五欄的表格，順序改成「新的在上面」，所以不能再用陣列位序認人 ——
//    用那一筆自己的時間去找，斷言的意思跟舊版一樣。
// ⚠️ 一定要指名 .t-today：真實區現在有兩份清單（今天／過去），
//    用 `.n-zone.z-real .n-row` 會把過去那幾天一起數進來（09-03 這裡紅過一次）。
const led = await evalJS(`(()=>{
  const rows=[...document.querySelectorAll('.n-zone.z-real .t-today .n-row')];
  const find=re=>rows.find(r=>new RegExp(re).test(r.innerText));
  const cls=r=>{const p=r&&r.querySelector('.pt'); return p?p.className:'';};
  const net=document.querySelector('.n-zone.z-real .n-trh .net');
  const rl=document.querySelector('.n-zone.z-real .t-today');
  const hs=rows.map(r=>Math.round(r.getBoundingClientRect().height));
  const ih=rows.map(r=>r.clientHeight);
  return {n:rows.length, net:net&&net.innerText,
          winCls:cls(find('09:12')), lossCls:cls(find('10:44')),
          naTxt:(find('13:41')||{innerText:''}).innerText, naCls:cls(find('13:41')),
          note:!!document.querySelector('.n-zone.z-real .n-trnote'),
          minH:Math.min(...hs), maxH:Math.max(...hs),
          // clientHeight 不含 border：第一列刻意 border-top:0，拿含框的高度比會差 1px，
          // 那是尺壞了不是版面壞了（第一版就在這裡誤報過一次紅燈）。
          minIn:Math.min(...ih), maxIn:Math.max(...ih),
          scrolls: rl ? rl.scrollHeight > rl.clientHeight+2 : false};})()`);
chk("  八筆都列出來", led.n, 8);
chk("  賺的那筆是紅色（台股慣例，紅＝賺）", /\bup\b/.test(led.winCls), true);
chk("  賠的那筆是綠色", /\bdown\b/.test(led.lossCls), true);
chk("  問不到成交價的那筆顯示破折號、不編數字", /—/.test(led.naTxt), true);
chk("  而且有寫清楚為什麼那筆沒有點數", led.note, true);
// 【鐵律】有 max-height 的 flex 直欄，子元素沒有 flex:none 的話不是捲動而是把每一列壓扁。
// 筆數少的時候完全看不出來，所以治具刻意給了會超過容器高度的筆數。
chk("  筆數超過容器高度時是捲動", led.scrolls, true);
// 壓扁的話會掉到 20px 出頭（鐵律那次實測 107px 被壓成 21.6px），所以門檻放在 28px；
// 「每一列一樣高」用不含 border 的內容高度比，才不會被第一列的 border-top:0 誤導。
chk("  每一列沒有被壓扁（>= 28px）", led.minH >= 28, true);
chk("  而且每一列一樣高（沒有誰被擠掉）", led.minIn === led.maxIn, true);
console.log(`    實測列高 ${led.minH}~${led.maxH}px（含框）／${led.minIn}~${led.maxIn}px（內容）`);

console.log("\n=== ⑧b 過去的真實交易（他 09-03 問「昨天的紀錄都不見了」）===");
// 舊版真實區只列今天，過了午夜昨天的就從畫面上消失、只剩併進勝率的數字 ——
// 檔案好好的在 real_trades/，但**留得住不等於看得到**。
const past = await evalJS(`(()=>{
  const l=document.querySelector('.n-zone.z-real .t-past');
  if(!l) return {missing:true};
  const rows=[...l.querySelectorAll('.n-row')];
  const days=[...l.querySelectorAll('.n-dayh')];
  const today=[...document.querySelectorAll('.n-zone.z-real .t-today .n-row')]
                .map(r=>r.innerText);
  const ih=rows.map(r=>r.clientHeight);
  return {n:rows.length, days:days.map(d=>d.querySelector('.dt').innerText),
          dayNets:days.map(d=>{const n=d.querySelector('.net'); return n?n.innerText:null}),
          // 一天之內新的在上面：08-29 那三筆的進場時間應該是 09:50 → 09:20 → 09:02
          order:rows.slice(0,3).map(r=>r.innerText.split('\\n')[1].slice(0,5)),
          // 過去那幾筆要能事後補寫心得，而且已經寫過的要顯示出來
          editable:l.querySelectorAll('.noteline[data-nedit][data-nkind="real"]').length,
          written:[...l.querySelectorAll('.noteline')].filter(x=>/「/.test(x.innerText)).length,
          dup:rows.filter(r=>today.includes(r.innerText)).length,
          scrolls:l.scrollHeight>l.clientHeight+2,
          minIn:Math.min(...ih), maxIn:Math.max(...ih)};})()`);
chk("  過去的清單真的在畫面上", !past.missing, true);
chk("  四筆都列出來", past.n, 4);
chk("  分成兩天，新的日子在上面", past.days, ["08-29", "08-28"]);
chk("  每一天各自有小計", past.dayNets, ["-105 點", "+100 點"]);
chk("  一天之內也是新的在上面", past.order, ["09:50", "09:20", "09:02"]);
// ⛔ 今天那幾筆不可以在這裡再出現一次 —— 切「今天／過去」要用伺服器給的 R.today，
//    瀏覽器自己算 new Date() 在跨午夜那一刻會跟後端讀的檔案不同一天。
chk("  ⛔ 今天那幾筆沒有重複出現在過去", past.dup, 0);
chk("  過去的每一筆都能事後補寫心得", past.editable, 4);
chk("  已經寫過的心得看得到", past.written, 1);
// 跟今天那份同一條鐵律：有 max-height 的 flex 直欄，沒有 flex:none 就是壓扁不是捲動
chk("  超過容器高度時是捲動", past.scrolls, true);
chk("  每一列一樣高（沒有誰被壓扁）",
    past.minIn === past.maxIn && past.minIn >= 28, true);
console.log(`    實測列高 ${past.minIn}~${past.maxIn}px（內容）`);

console.log("\n=== ⑨ 站在練習分頁：畫面上一顆真實下單鈕都沒有（結構防呆）===");
await ctl("/mode/flat");
await sleep(700);
await goTab("sim");
const s9 = await evalJS(`(()=>({
  fire: document.querySelectorAll('[data-rdir]').length,
  fb: document.querySelectorAll('.n-fb').length,
  close: document.querySelectorAll('[data-rclose]').length,
  realZone: document.querySelectorAll('.n-zone.z-real').length,
  simZone: document.querySelectorAll('.n-zone.z-sim').length,
  simBtns: document.querySelectorAll('[data-act="long"],[data-act="short"]').length}))()`);
chk("  真實下單鈕一顆都沒有", s9.fire, 0);
chk("  真實那一整區都不在畫面上", s9.realZone, 0);
chk("  （負控組）練習那一區確實在，而且下單鈕看得到", [s9.simZone, s9.simBtns], [1, 2]);

console.log("\n=== ⑩ 站在練習分頁：真實那一邊出事照樣看得到 ===");
await ctl("/mode/stale");          // 有部位 ＋ 報價已中斷 27 秒
await sleep(1500);
const s10 = await evalJS(`(()=>{
  const x=document.querySelector('.n-x.n-bad'), r=x&&x.getBoundingClientRect();
  const b=document.getElementById('tabbadge');
  return {onSim:!!document.querySelector('.n-zone.z-sim'),
          alarm:!!x, txt:x?x.innerText:'',
          visible:!!(r&&r.width>0&&r.height>0),
          go:!!document.querySelector('[data-rgo]'),
          badge:(b&&!b.hidden)?b.textContent:null};})()`);
chk("  人還站在練習分頁", s10.onSim, true);
chk("  報價中斷的警報照樣看得到", s10.alarm, true);
chk("  而且是真的畫出來（不是有節點但沒尺寸）", s10.visible, true);
chk("  警報上寫著中斷了幾秒", /27/.test(s10.txt), true);
chk("  有一顆「去看部位」可以直接跳過去", s10.go, true);
chk("  真實頁籤上掛著「有事」的標記", s10.badge, "!");

console.log("\n=== ⑪ 做空的部位：平倉鈕要寫清楚送出去的是「買進」 ===");
// 09-01 出過事：做空按平倉送出 Sell，等於又加一口空單。鈕上直接寫會送出什麼。
await ctl("/mode/short");
await sleep(1500);
const s11 = await evalJS(`(()=>{const b=document.getElementById('tabbadge');
  return {badge:(b&&!b.hidden)?b.textContent:null};})()`);
// ⚠️ 不可以斷言固定數字。治具的浮動點數**刻意會跳**（靜態假狀態驗不出任何「重繪」
//    類的 bug，見 fe_harness.py 的 tick_px），所以這裡驗的是格式與**正負號**：
//    做空是虧的（現價高於進場）就一定要是負的 —— 號誌搞反才是會害到他的錯。
chk("  站在練習分頁時，真實頁籤直接把浮動點數掛出來（做空虧損 → 負號）",
  /^-\d+$/.test(String(s11.badge || "")), true);
await goTab("real");
const s11b = await evalJS(`(()=>{const c=document.querySelector('[data-rclose]');
  const d=document.querySelector('.n-dir');
  return {txt:c?c.innerText:'', dir:d?d.innerText:''};})()`);
chk("  部位顯示的是做空", /做空/.test(s11b.dir), true);
chk("  平倉鈕寫「買進」＋「回補空單」", /買進/.test(s11b.txt) && /回補空單/.test(s11b.txt), true);
chk("  而且沒有寫成賣出", /賣出/.test(s11b.txt), false);

c.close();
ch.kill();
try { fs.rmSync(profile, { recursive: true, force: true }); } catch { /* Chrome 還握著暫存檔，無所謂 */ }
console.log("\n總結:", FAIL ? `${FAIL} 項失敗` : "全部通過");
process.exit(FAIL ? 1 : 0);
