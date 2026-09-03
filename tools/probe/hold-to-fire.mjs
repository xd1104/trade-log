/*
  長按送單那顆按鈕的真滑鼠探針（101 項）。

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
          // 11:06 ＝ 治具那筆「問不到成交價」的出場時間（見 fe_harness.py）
          naTxt:(find('11:06')||{innerText:''}).innerText, naCls:cls(find('11:06')),
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

console.log("\n=== ⑧b 成績區的卡片清單：過去幾天的紀錄要看得到（他 09-03 問「昨天的紀錄都不見了」）===");
// 【這一節的來歷】原本驗的是 `.t-past`（照日期分組、每天一個 .n-dayh 小計的第二份清單）。
// 2026-09-03 的 A 版把那份整個換成成績區底下的 .trade 卡片清單（realCard），
// 所以**斷言要跟著搬到新結構上，不是刪掉了事** —— 這一節守的意思沒有變：
// 過去幾天的每一筆都列得出來、新的在上面、今天的不重複、能補心得、不被壓扁。
const past = await evalJS(`(()=>{
  const l=document.querySelector('.n-zone.z-real .list');
  if(!l) return {missing:true};
  const cs=[...l.querySelectorAll('.trade')];
  const txt=e=>e.innerText.split(String.fromCharCode(10));
  const ih=cs.map(e=>e.clientHeight);
  // 卡片第一行是日期（.tr-date）
  const dates=cs.map(e=>e.querySelector('.tr-date').textContent);
  // 「今天」那份 .n-row 清單裡的進場時間，用來確認同一筆沒有在卡片裡多長一份
  const todayRows=[...document.querySelectorAll('.n-zone.z-real .t-today .n-row')].length;
  return {n:cs.length, dates:dates,
          // 過去那兩天（08-29 ×3、08-28 ×1）都要在
          pastN:dates.filter(d=>d!=='09-01').length,
          // 新到舊：日期字串必須是不遞增的
          sorted:dates.every((d,i)=>i===0||dates[i-1]>=d),
          // ⛔ 【同一天之內也要新到舊】只比日期的話，同一天 8 筆的順序被打亂
          //    完全測不出來（QA 實證：把 realSorted() 的比較字串拿掉 entry_time，
          //    兩支探針照樣全綠）。realSorted() 唯一的排序保證就是這個，要直接驗。
          //    卡片上沒有印進場時間，所以用「日期＋進場價」回頭對 trades_all 拿時間。
          byDay:(()=>{
            const all=(LASTS.real&&LASTS.real.trades_all)||[];
            const seq=cs.map(e=>{
              const d=e.querySelector('.tr-date').textContent;
              const ep=Number(e.querySelector('.tr-px').textContent.split('→')[0].trim());
              const t=all.find(x=>x.date.slice(5)===d && Math.round(x.entry)===ep);
              return {d:d, t:t?t.entry_time:null};
            });
            return seq;})(),
          // 真的有兩個以上不同的日子（否則「新的日子在上面」等於沒驗到）
          days:[...new Set(dates)],
          // ⚠️ 上面那個 byDay 用「日期＋進場價」回頭對 trades_all 拿時間 ——
          //    治具若出現**同日同進場價**的兩筆，all.find() 會對兩張卡回同一個
          //    entry_time ⇒ 斷言會翻紅但**指錯原因**（看起來像產品壞了，其實是尺壞了）。
          //    先驗這把尺的前提：(date, entry) 在 trades_all 裡必須唯一。
          keyDup:(()=>{
            const all=(LASTS.real&&LASTS.real.trades_all)||[];
            const seen={}, dup=[];
            all.forEach(t=>{const k=t.date+'|'+Math.round(t.entry);
              if(seen[k]) dup.push(k); seen[k]=1;});
            return dup;})(),
          // 每一筆都能事後補寫心得（含今天與過去），而且標成 real
          editable:l.querySelectorAll('.noteline[data-nedit][data-nkind="real"]').length,
          written:[...l.querySelectorAll('.noteline')].filter(x=>/「/.test(x.innerText)).length,
          // 分區字母：卡片用 S、今天那份 .n-row 用 R。同一個字母的話點一下會展開
          // 兩個 id 都叫 tnote 的 textarea，第二個打的字存不進去。
          ns:[...new Set([...l.querySelectorAll('[data-nedit]')]
                .map(e=>e.getAttribute('data-nedit')[0]))],
          rowNs:[...new Set([...document.querySelectorAll('.n-zone.z-real .t-today [data-nedit]')]
                .map(e=>e.getAttribute('data-nedit')[0]))],
          todayRows:todayRows,
          scrolls:l.scrollHeight>l.clientHeight+2,
          minIn:Math.min(...ih), maxIn:Math.max(...ih)};})()`);
chk("  卡片清單真的在畫面上", !past.missing, true);
// 12 筆（今天 8 ＋ 過去 4）都在，而且**不跟著分段窗口縮**（預設是「近 7 筆」）——
// 跟著縮的話昨天的紀錄又會消失，只是這次藏在按鈕後面。練習那邊也是固定 12 筆。
chk("  12 筆都列出來（清單固定最近 12 筆，不跟著分段縮）", past.n, 12);
chk("  ⛔ 過去那兩天的 4 筆都看得到", past.pastN, 4);
chk("  新的在上面（日期不遞增）", past.sorted, true);
// （尺的自證）沒有這條的話，資料剛好只剩一天時「新的日子在上面」會變成恆真
chk("  （尺的自證）真的跨了三個不同的日子", past.days, ["09-01", "08-29", "08-28"]);
// ⛔ 同一天之內依 entry_time 新到舊。realSorted() 的排序保證就這一條，要直接驗。
// ⚠️ 先驗尺：下面那條靠「日期＋進場價」認人，治具有同日同價的兩筆就會對到同一筆，
//    斷言會紅但指錯原因。這一條紅＝**治具要改**，不是產品壞了。
chk("  （尺的自證）治具沒有同日同進場價的兩筆（有的話是治具要改，不是產品壞了）",
    past.keyDup, []);
chk("  （尺的自證）每一張卡都對回得到它的進場時間",
    past.byDay.filter(x => !x.t).length, 0);
chk("  ⛔ 同一天之內也是新到舊（進場時間遞減）",
    past.byDay.every((x, i) => i === 0 || past.byDay[i - 1].d !== x.d ||
      past.byDay[i - 1].t > x.t), true);
console.log(`    卡片順序 ${past.byDay.map(x => x.d + " " + String(x.t).slice(0, 5)).join(" / ")}`);
// 原「每一天各自有小計」（.n-dayh 的 net）在新結構上**沒有對應**：卡片是一筆一張、
// 沒有日期分組那一層。日期改由每張卡自己的 .tr-date 負責，見上面的 dates 斷言。
chk("  每一筆都能事後補寫心得", past.editable, 12);
chk("  已經寫過的心得看得到", past.written, 2);
chk("  ⛔ 卡片的分區字母是 S、今天那份是 R（撞了兩個輸入框會打架）",
    [past.ns, past.rowNs], [["S"], ["R"]]);
chk("  （負控組）今天那份 .n-row 清單還在，沒有被卡片取代", past.todayRows, 8);
// 跟今天那份同一條鐵律：有 max-height 的 flex 直欄，沒有 flex:none 就是壓扁不是捲動
chk("  超過容器高度時是捲動", past.scrolls, true);
chk("  每一張卡一樣高（沒有誰被壓扁）",
    past.minIn === past.maxIn && past.minIn >= 28, true);
console.log(`    實測卡高 ${past.minIn}~${past.maxIn}px　日期 ${past.dates.join(" ")}`);

console.log("\n=== ⑧b2 卡片高度要跟練習區逐像素相同（A 版＝完全照練習抄）===");
const rcard = await evalJS(`(()=>{const cs=[...document.querySelectorAll('.n-zone.z-real .list .trade')];
  const norm=v=>{const d=document.createElement('div');
    d.style.color=getComputedStyle(document.documentElement).getPropertyValue(v).trim();
    document.body.appendChild(d); const c=getComputedStyle(d).color; d.remove(); return c;};
  return {h:[...new Set(cs.map(e=>e.clientHeight))],
          rect:[...new Set(cs.map(e=>Math.round(e.getBoundingClientRect().height*100)/100))],
          // 左緣色條：算得出點數的照練習掛 .win/.loss，算不出的留 --ghost 灰
          winloss:cs.filter(e=>e.classList.contains('win')||e.classList.contains('loss')).length,
          na:cs.filter(e=>e.querySelector('.tr-res.na')).length,
          // 逐張比對「有沒有點數」與「有沒有色條」，並且驗紅綠對得上正負
          pair:cs.map(e=>{
            const r=e.querySelector('.tr-res');
            const naC=r.classList.contains('na');
            const txt=r.textContent.trim();
            return {na:naC, num:naC?null:Number(txt.replace('+','')),
                    win:e.classList.contains('win'), loss:e.classList.contains('loss'),
                    bar:getComputedStyle(e,'::before').backgroundColor};}),
          // ⚠️ CSS 變數是 #RRGGBB，computed style 回的是 rgb() —— 直接比會永遠不相等
          //    （尺壞了不是版面壞了）。丟給瀏覽器自己正規化一次再比。
          upColor:norm('--up'), downColor:norm('--down'), ghost:norm('--ghost'),
          naTxt:(cs.find(e=>e.querySelector('.tr-res.na'))||{innerText:''}).innerText,
          dl:document.querySelectorAll('.n-zone.z-real .dl').length,
          // 內部代號不准外露（認不得的一律寫「其他」）
          code:cs.filter(e=>/closed_elsewhere|sl_test|tp_|_test/.test(e.innerText)).length};})()`);
await goTab("sim");
await sleep(600);
const scard = await evalJS(`(()=>{const cs=[...document.querySelectorAll('.n-zone.z-sim .list .trade')];
  return {h:[...new Set(cs.map(e=>e.clientHeight))],
          rect:[...new Set(cs.map(e=>Math.round(e.getBoundingClientRect().height*100)/100))]};})()`);
await goTab("real");
await sleep(600);
chk("  （尺的自證）兩邊都真的有卡片", [rcard.h.length > 0, scard.h.length > 0], [true, true]);
chk("  真實的卡片高度只有一種（沒有誰折行變高）", rcard.h.length, 1);
chk("  ⛔ 跟練習的卡片逐像素相同（clientHeight）", rcard.h, scard.h);
chk("  ⛔ 連含框的高度也一樣", rcard.rect, scard.rect);
console.log(`    實測 真實 ${rcard.h[0]}px / ${rcard.rect[0]}px　練習 ${scard.h[0]}px / ${scard.rect[0]}px`);
// 【左緣色條】Benson 2026-09-03 拍板：算得出點數的照練習掛 .win/.loss（紅綠條紋），
// 算不出的維持 --ghost 灰 —— 那筆問不到成交價，猜輸贏就是編數字。
chk("  算得出點數的那 11 張都有色條", rcard.winloss, rcard.pair.filter(p => !p.na).length);
chk("  （尺的自證）兩種都真的出現在畫面上（不是全有或全無）",
    [rcard.pair.filter(p => p.win).length > 0, rcard.pair.filter(p => p.loss).length > 0,
     rcard.pair.filter(p => p.na).length > 0], [true, true, true]);
// ⛔ 逐張驗，不是抽樣：有點數 → 必須有色條且紅綠對得上正負（台股慣例紅＝賺）
chk("  ⛔ 賺的掛 .win、賠的掛 .loss（0 算敗，跟練習同一套）",
    rcard.pair.filter(p => !p.na && !(p.num > 0 ? p.win && !p.loss : p.loss && !p.win)).length, 0);
chk("  ⛔ 算不出點數那張不准有 .win/.loss",
    rcard.pair.filter(p => p.na && (p.win || p.loss)).length, 0);
// 色條是真的畫出來的顏色，不是只有 class（改底色也要被抓到）
chk("  ⛔ .win 的色條是紅（台股慣例紅＝賺）",
    [...new Set(rcard.pair.filter(p => p.win).map(p => p.bar))], [rcard.upColor]);
chk("  ⛔ .loss 的色條是綠", [...new Set(rcard.pair.filter(p => p.loss).map(p => p.bar))],
    [rcard.downColor]);
chk("  ⛔ 算不出點數那張的色條維持中性灰",
    [...new Set(rcard.pair.filter(p => p.na).map(p => p.bar))], [rcard.ghost]);
chk("  問不到成交價那筆印破折號、用 .na", rcard.na, 1);
chk("  而且出場價也留白，不編數字", /→\s*—/.test(rcard.naTxt), true);
chk("  ⛔ 內部代號沒有外露（closed_elsewhere 之類）", rcard.code, 0);
// ⛔ 老闆拍板：真實區不放「下載紀錄」——練習那顆寫著「可匯入 App」，而 App 會同步到公開 repo
chk("  ⛔ 真實區沒有「下載紀錄」按鈕（不可以順手補上）", rcard.dl, 0);

console.log("\n=== ⑧b3 分段按鈕 .seg：窗口去重後只剩一個就整條不畫 ===");
// 真實筆數少，近 7／近 10／近 30／全部 常常對到同一批資料 ——
// 按了畫面完全不動比沒有這排按鈕更糟。兩種情境都要驗，只驗一種必有一半是假綠。
await ctl("/vol/few");                 // 5 筆 ⇒ 四個窗口全同 ⇒ 去重剩 1
await sleep(1400);
const segFew = await evalJS(`(()=>({
  seg:document.querySelectorAll('.n-zone.z-real .seg').length,
  btn:document.querySelectorAll('.n-zone.z-real .seg button').length,
  cards:document.querySelectorAll('.n-zone.z-real .list .trade').length,
  rate:(document.querySelector('.n-zone.z-real .score .rate .n')||{}).textContent}))()`);
chk("  只剩一個窗口時，那排按鈕整條不畫", [segFew.seg, segFew.btn], [0, 0]);
chk("  （負控組）成績本身還是畫得出來", [segFew.cards, !!segFew.rate], [5, true]);
console.log(`    5 筆時：勝率 ${segFew.rate}%、卡片 ${segFew.cards} 張、按鈕 ${segFew.btn} 顆`);

await ctl("/vol/full");                // 12 筆 ⇒ 近7=7／近10=10／（近30 涵蓋全部）⇒ 剩 3
await sleep(1400);
const segFull = await evalJS(`(()=>({
  labels:[...document.querySelectorAll('.n-zone.z-real .seg button')].map(b=>b.textContent),
  on:[...document.querySelectorAll('.n-zone.z-real .seg button.on')].map(b=>b.textContent),
  rate:(document.querySelector('.n-zone.z-real .score .rate .n')||{}).textContent,
  cnt:[...document.querySelectorAll('.n-zone.z-real .wlfoot span')][1].textContent}))()`);
// ⚠️ 第三顆是「全部」不是「近 30 筆」：那個窗口實際上已經涵蓋全部 12 筆，
//    寫「近 30 筆」會讓人以為還有更早的沒算進去（練習那邊的窗口也是近7／近10／全部）。
chk("  窗口變多時真的畫得出來（去重後 3 顆）", segFull.labels, ["近 7 筆", "近 10 筆", "全部"]);
chk("  預設選中「近 7 筆」", segFull.on, ["近 7 筆"]);
// 按下去要真的換一批資料：勝率或筆數其中之一一定要變（兩個都不變＝按了等於沒事發生）
await evalJS(`(()=>{const b=[...document.querySelectorAll('.n-zone.z-real .seg button')]
  .find(x=>x.textContent==='全部'); if(b) b.click();})()`);
await sleep(1400);
const segAfter = await evalJS(`(()=>({
  on:[...document.querySelectorAll('.n-zone.z-real .seg button.on')].map(b=>b.textContent),
  rate:(document.querySelector('.n-zone.z-real .score .rate .n')||{}).textContent,
  cnt:[...document.querySelectorAll('.n-zone.z-real .wlfoot span')][1].textContent,
  cards:document.querySelectorAll('.n-zone.z-real .list .trade').length}))()`);
chk("  按了之後選中的那一顆換人", segAfter.on, ["全部"]);
chk("  ⛔ 而且畫面上的數字真的變了（按了不動比沒有更糟）",
    segAfter.rate !== segFull.rate || segAfter.cnt !== segFull.cnt, true);
chk("  但卡片清單不跟著縮（固定最近 12 筆）", segAfter.cards, 12);
console.log(`    近 7 筆：勝率 ${segFull.rate}%・${segFull.cnt}　→　全部：勝率 ${segAfter.rate}%・${segAfter.cnt}`);
// 收尾把窗口切回預設，後面的測項才是在同一個狀態上量
await evalJS(`(()=>{const b=[...document.querySelectorAll('.n-zone.z-real .seg button')]
  .find(x=>x.textContent.indexOf('7')>=0); if(b) b.click();})()`);
await sleep(1200);

console.log("\n=== ⑧b4 卡片上的心得：展開 → 打字 → 儲存，整條走通 ===");
await ctl("/reset");
const opened = await evalJS(`(()=>{
  const c=[...document.querySelectorAll('.n-zone.z-real .list .trade')]
    .find(e=>e.querySelector('.tr-date').textContent==='08-28');
  if(!c) return {missing:true};
  const nl=c.querySelector('[data-nedit]');
  const key=nl.getAttribute('data-nedit');
  nl.click();
  return {key:key, nd:nl.getAttribute('data-nd'), nt:nl.getAttribute('data-nt'),
          kind:nl.getAttribute('data-nkind')};})()`);
await sleep(900);
const nbox = await evalJS(`(()=>{const t=document.getElementById("tnote");
  return {n:document.querySelectorAll('#tnote').length, has:!!t};})()`);
chk("  點一下展開輸入框", nbox.has, true);
// ⛔ 同一筆今天的交易會同時出現在兩份清單裡；分區字母撞了就會冒出兩個 id 都叫 tnote 的框
chk("  ⛔ 畫面上只有一個 tnote（分區字母沒撞）", nbox.n, 1);
chk("  卡片的心得走 kind=real（存進 real_trades，不上傳）", opened.kind, "real");
// 打字（治具與測試裡的心得一律自己編，不可以抄他真實的心得）
await evalJS(`(()=>{const t=document.getElementById('tnote');
  t.value='__測試用假心得__'; t.dispatchEvent(new Event('input',{bubbles:true}));})()`);
await sleep(200);
await evalJS(`(()=>{const b=document.querySelector('[data-nsave]'); if(b) b.click();})()`);
await sleep(1200);
const posted = (await ctl("/posts")).posts.filter(p => p[0] === "/api/note");
chk("  儲存有真的送出 /api/note", posted.length, 1);
chk("  ⛔ 送的是那一筆自己的日期（不是今天）", posted[0] && posted[0][1].date, "2026-08-28");
// 08-28 那一筆治具給的是 09:11 進場、47310 —— 認人靠（日期＋進場時間＋進場價），
// 用陣列位序的話「撤銷最後一筆」就會認錯人。
chk("  進場時間與進場價也對得上",
    [posted[0] && posted[0][1].time, posted[0] && posted[0][1].entry], ["09:11", 47310]);
chk("  ⛔ kind 是 real", posted[0] && posted[0][1].kind, "real");
chk("  文字有帶出去", posted[0] && posted[0][1].text, "__測試用假心得__");
console.log(`    送出的內容：${JSON.stringify(posted[0] && posted[0][1])}`);
await ctl("/reset");

console.log("\n=== ⑧b5 ⛔ 點「今天」那一筆的卡片：不可以冒出兩個輸入框 ===");
// ⚠️ 上面那一節點的是 **08-28**（只存在於卡片清單），所以就算兩份清單的分區字母撞在一起
//    也不會有事 —— **那條斷言涵蓋不到真正會壞的情況**（變異測試實證：把卡片改回分區 R，
//    ⑧b4 照樣全綠）。真正會撞的是**今天**那幾筆：它們同時出現在 .n-row 與卡片兩份清單裡，
//    同 key 就會展開兩個 id 都叫 tnote 的 textarea，
//    `document.getElementById('tnote')` 只拿得到第一個 ⇒ 在另一個打的字存不進去。
const dup = await evalJS(`(()=>{
  const c=[...document.querySelectorAll('.n-zone.z-real .list .trade')]
    .find(e=>e.querySelector('.tr-date').textContent==='09-01');
  if(!c) return {missing:true};
  const nl=c.querySelector('[data-nedit]');
  const key=nl.getAttribute('data-nedit');
  // 同一筆在今天那份 .n-row 清單裡的 key（只差分區字母就是撞了）
  const same=[...document.querySelectorAll('.n-zone.z-real .t-today [data-nedit]')]
    .filter(e=>e.getAttribute('data-nedit').slice(1)===key.slice(1)).length;
  nl.click();
  return {key:key, twin:same};})()`);
await sleep(900);
const dup2 = await evalJS(`document.querySelectorAll('#tnote').length`);
chk("  （尺的自證）挑到的真的是「今天」而且兩份清單都有這一筆", [!dup.missing, dup.twin], [true, 1]);
chk("  ⛔ 只有一個輸入框被展開", dup2, 1);
await evalJS(`(()=>{const b=document.querySelector('[data-ncancel]'); if(b) b.click();})()`);
await sleep(700);
await ctl("/reset");

console.log("\n=== ⑧b7 ⛔ 正在寫心得時按分段按鈕，要當場生效（而且字不可以掉）===");
// 【為什麼要有這一條】QA 2026-09-03 退件：整塊 #realstats 重畫會把 .list 裡正在編輯的
// textarea 換掉，所以重繪守衛把整塊擋下來 ⇒ **按了畫面完全不動、關掉編輯器才突然跳**。
// 「按了不動」正是這排按鈕當初要避免的毛病，延遲跳動又更像壞掉。
// 修法（(a) 案）：把只跟窗口有關的那幾塊切成獨立節點 #realscore，按鈕只換那一塊。
// ⚠️ 要同時驗兩件事：① 數字真的變了 ② 正在打的那個 textarea **是同一顆節點**且字還在。
const segEdit = await evalJS(`(()=>{
  const c=[...document.querySelectorAll('.n-zone.z-real .list .trade')][0];
  c.querySelector('[data-nedit]').click();
  return true;})()`);
await sleep(900);
await evalJS(`(()=>{const t=document.getElementById('tnote');
  window.__ta=t;                       // ⚠️ 先抓住那一顆，之後比 === （不可以重新 query）
  t.value='__編輯中__'; t.dispatchEvent(new Event('input',{bubbles:true}));})()`);
await sleep(300);
const before = await evalJS(`(()=>({
  rate:(document.querySelector('.n-zone.z-real .score .rate .n')||{}).textContent,
  on:[...document.querySelectorAll('.n-zone.z-real .seg button.on')].map(b=>b.textContent),
  held:!!window.__ta}))()`);
chk("  （尺的自證）真的抓到了正在編輯的那一顆 textarea", [segEdit, before.held], [true, true]);
await evalJS(`(()=>{const b=[...document.querySelectorAll('.n-zone.z-real .seg button')]
  .find(x=>x.textContent==='全部'); if(b) b.click();})()`);
await sleep(1600);                      // 撐過 3 次 tick
const after = await evalJS(`(()=>({
  rate:(document.querySelector('.n-zone.z-real .score .rate .n')||{}).textContent,
  on:[...document.querySelectorAll('.n-zone.z-real .seg button.on')].map(b=>b.textContent),
  sameNode:document.getElementById('tnote')===window.__ta,
  alive:!!(window.__ta&&document.body.contains(window.__ta)),
  val:window.__ta?window.__ta.value:null}))()`);
chk("  ⛔ 按下去就生效（不是關掉編輯器才跳）", after.on, ["全部"]);
chk("  ⛔ 而且數字真的變了", after.rate !== before.rate, true);
chk("  ⛔ 正在編輯的那一顆 textarea 還是同一顆（沒有被重繪換掉）",
    [after.sameNode, after.alive], [true, true]);
chk("  ⛔ 打的字還在", after.val, "__編輯中__");
console.log(`    勝率 ${before.rate}% → ${after.rate}%（${before.on} → ${after.on}）`);
await evalJS(`(()=>{const b=document.querySelector('[data-ncancel]'); if(b) b.click();})()`);
await sleep(900);
await evalJS(`(()=>{const b=[...document.querySelectorAll('.n-zone.z-real .seg button')]
  .find(x=>x.textContent.indexOf('7')>=0); if(b) b.click();})()`);
await sleep(1200);

console.log("\n=== ⑧b6 ⛔ 每一種平倉理由的標籤都不准把卡片撐高（4 字硬上限的守衛）===");
// 【為什麼要有這一條】卡片那一行的 `.tr-px` 只有 157.6px，裡面是「價格→價格 ＋ 標籤」。
// 實測 2 字標籤要 120.5px、4 字 140.5px（放得下）、**6 字 161px ⇒ 會折行**，
// 那張卡就從 65px 變成 80px —— 而「形式跟練習長得一樣」是老闆對這一版的主要要求。
// `.tr-px` 沒有 nowrap，所以**折行不會報錯、也不會截斷**，純靠肉眼根本看不出來。
// ⚠️ 要「掃 RWHY 的每一個標籤」不是列白名單：治具原本的資料不見得每種理由都有
//    （認不得的代號 → 「其他」就完全沒有）。/vol/reasons 每種各造一張卡。
await ctl("/vol/reasons");
await sleep(1500);
const why = await evalJS(`(()=>{
  // RWHY 是面板自己那份 map（單一來源，.n-row 與卡片共用）
  const want=Object.values(RWHY).concat(['其他']);
  const cs=[...document.querySelectorAll('.n-zone.z-real .list .trade')];
  const got=cs.map(e=>e.querySelector('.tr-px .tag').textContent);
  const px=cs.map(e=>e.querySelector('.tr-px'));
  return {want:want, wantN:want.length, got:got,
          missing:want.filter(x=>got.indexOf(x)<0),
          maxLen:Math.max(...want.map(x=>x.length)),
          cardH:[...new Set(cs.map(e=>e.clientHeight))],
          // 折行 ⇒ .tr-px 從 18.8px 變成 37.5px；門檻放在 24px
          wrapped:px.filter(e=>e.getBoundingClientRect().height>24)
                    .map(e=>e.textContent+' (h='+Math.round(e.getBoundingClientRect().height)+')'),
          // 內部代號絕不可以外露
          code:cs.filter(e=>/sl_test|closed_elsewhere|_test/.test(e.innerText)).length};})()`);
// 尺的自證：治具真的把每一種都造出來了，否則這條等於沒驗到
chk("  （尺的自證）RWHY 的每一種＋「其他」都真的出現在畫面上", why.missing, []);
chk("  （尺的自證）掃到的卡片數＝理由種類數", why.got.length, why.wantN);
chk("  ⛔ 每個標籤都在 4 字以內", why.maxLen <= 4, true);
chk("  ⛔ 沒有任何一張卡的價格欄折行", why.wrapped, []);
chk("  所以每一張卡一樣高", why.cardH.length, 1);
chk("  ⛔ 認不得的代號印成「其他」，代號本身沒有外露", [why.got.indexOf("其他") >= 0, why.code],
    [true, 0]);
// 跟練習的卡片比一次（這一節的門檻就是「跟練習一樣高」）
await goTab("sim");
await sleep(600);
const simH = await evalJS(`[...new Set([...document.querySelectorAll('.n-zone.z-sim .list .trade')]
  .map(e=>e.clientHeight))]`);
await goTab("real");
await sleep(600);
chk("  ⛔ 而且跟練習的卡片一樣高", why.cardH, simH);
console.log(`    標籤 ${why.got.join("／")}　卡高 ${why.cardH.join(",")}px（練習 ${simH.join(",")}px）`);
await ctl("/vol/full");            // 收尾切回預設，後面的測項才是在同一個狀態上量
await sleep(1400);

console.log("\n=== ⑧c ⛔ 成交價不准被截（真實區的欄寬要跟練習區一模一樣）===");
// 【為什麼要有這一條】2026-09-03：`realStats()` 把兩份 .n-trl 包在 .n-bd 裡面，
// 多吃一層 padding:0 18px 16px 20px（左右合計 38px），而 .n-trl 自己已經有左右內距 ⇒
// 那 38px 是重複的，又全部從 .n-row 唯一的 1fr（價格欄）身上扣：
// 實測 .n-row 348→310px、.px 76→38px，畫面上是「4701…」而不是「47010→47110」。
// ⛔ 註解裡的價格一律自己編（這裡用的是治具那組假資料）——這個 repo 是公開的，
//    他真實的成交價與心得絕對不上傳，連舉例都不可以抄。
// `.n-row .px` 有 text-overflow:ellipsis，所以**不會折行也不會報錯**，
// 而且筆數少的時候完全看不出來 —— 這個 bug 活了兩天，因為以前沒有人在守它。
// ⚠️ 要「掃全部逐一驗」不是抽前三顆；並且自證真的掃到東西。
//    ⚠️ 2026-09-03 A 版之後 `.n-row` **只剩今天那 8 筆**（過去那份 .t-past 換成卡片了），
//       所以自證的數字從 12 改成 8 —— 這是結構真的變了，不是把門檻放寬：
//       今天那份少一筆照樣會紅。卡片那邊的價格欄（.tr-px）在下面另外驗。
const cut = await evalJS(`(()=>{
  const px=[...document.querySelectorAll('.n-zone.z-real .n-row .px')];
  const row=[...document.querySelectorAll('.n-zone.z-real .n-row')];
  const cpx=[...document.querySelectorAll('.n-zone.z-real .list .tr-px')];
  const w=a=>[...new Set(a.map(e=>Math.round(e.getBoundingClientRect().width*100)/100))];
  const bad=px.filter(e=>e.scrollWidth>e.clientWidth);
  // 卡片的價格欄沒有 nowrap ⇒ 放不下時是**折行**（那張卡變高），不是截斷。
  // 兩種都要驗：只驗截斷的話，折行那種會安靜地讓卡片長高一截而測不出來。
  const cbad=cpx.filter(e=>e.scrollWidth>e.clientWidth||e.getBoundingClientRect().height>24);
  return {n:px.length, rowW:w(row), pxW:w(px), badN:bad.length,
          bad:bad.slice(0,3).map(e=>e.textContent+' ('+e.scrollWidth+'>'+e.clientWidth+')'),
          cn:cpx.length, cbadN:cbad.length,
          cbad:cbad.slice(0,3).map(e=>e.textContent+' (h='+
            Math.round(e.getBoundingClientRect().height)+')')};})()`);
chk("  （尺的自證）今天那份真的掃到 8 個價格欄", cut.n, 8);
chk("  ⛔ 沒有任何一筆的成交價被截掉", [cut.badN, cut.bad], [0, []]);
chk("  每一列的寬度都一樣", cut.rowW.length, 1);
// 卡片那份：12 張卡的價格欄一個都不准截、也不准折行（折行＝那張卡比別人高）
chk("  （尺的自證）卡片那份真的掃到 12 個價格欄", cut.cn, 12);
chk("  ⛔ 卡片的成交價也沒有被截或折行", [cut.cbadN, cut.cbad], [0, []]);
await goTab("sim");
await sleep(500);
const simW = await evalJS(`(()=>{
  const r=document.querySelector('.n-zone.z-sim .n-row');
  const p=document.querySelector('.n-zone.z-sim .n-row .px');
  const w=e=>e?Math.round(e.getBoundingClientRect().width*100)/100:null;
  return {row:w(r), px:w(p)};})()`);
// 兩區的清單是同一套五欄格線，寬度不一樣就代表其中一邊被多包了一層有 padding 的祖先。
chk("  真實區的 .n-row 跟練習區一樣寬", [cut.rowW[0], simW.row], [simW.row, simW.row]);
chk("  價格欄也一樣寬", [cut.pxW[0], simW.px], [simW.px, simW.px]);
console.log(`    實測 .n-row ${cut.rowW[0]} vs ${simW.row}px、.px ${cut.pxW[0]} vs ${simW.px}px`);
await goTab("real");
await sleep(500);

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
