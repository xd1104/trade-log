/*
  長按送單那顆按鈕的真滑鼠探針。

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
await c.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: pos4.x, y: pos4.y, button: "none", buttons: 0 });
await c.send("Input.dispatchMouseEvent", { type: "mousePressed", x: pos4.x, y: pos4.y, button: "left", buttons: 1, clickCount: 1 });
await sleep(HOLD - 120);
const alive = await evalJS(`(()=>{const e=document.querySelector('[data-rdir="short"]');
  return {inDoc: !!e && document.body.contains(e), holding: !!window.holdingNow};})()`);
chk("  按到一半按鈕還在畫面上", alive.inDoc, true);
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
const holdTxt = await evalJS(`document.querySelector('.card.real').innerText`);
chk("  有寫「沒掛上」", /沒掛上/.test(holdTxt), true);
chk("  不可以還寫「已掛在券商」", /已掛在券商/.test(holdTxt), false);
await ctl("/mode/with_target");    // has_target=true
await sleep(1200);
const okTxt = await evalJS(`document.querySelector('.card.real').innerText`);
chk("  真的掛上時才寫「已掛在券商」", /已掛在券商/.test(okTxt), true);

console.log("\n=== ⑦ 卡片要看得出載入的是哪一版程式 ===");
// 版本那一行在「空手」那張卡上（有部位時卡片長得不一樣）
await ctl("/mode/flat");
await sleep(1300);
const flatTxt = await evalJS(`document.querySelector('.card.real').innerText`);
chk("  有印出 broker.py 的版本與啟動時間", /程式 \S+ \S+.啟動 \S+ \S+/.test(flatTxt), true);
if (!/程式 \S+ \S+.啟動 \S+ \S+/.test(flatTxt)) console.log("    卡片實際內容：\n      " + flatTxt.replace(/\n/g, "\n      "));

console.log("\n=== ⑧ 今天的真實交易成績單 ===");
const led = await evalJS(`(()=>{const rows=[...document.querySelectorAll('.rtrow')].map(r=>r.innerText);
  const net=document.querySelector('.rnet');
  const up=[...document.querySelectorAll('.rtn')].map(e=>e.className);
  return {n:rows.length, rows, net:net&&net.innerText, cls:up,
          note:!!document.querySelector('.rtrades .whyoff')};})()`);
chk("  三筆都列出來", led.n, 3);
chk("  賺的那筆是紅色（台股慣例，紅＝賺）", /\bup\b/.test(led.cls[0]), true);
chk("  賠的那筆是綠色", /\bdown\b/.test(led.cls[1]), true);
chk("  問不到成交價的那筆顯示破折號、不編數字", /—/.test(led.rows[2]), true);
chk("  而且有寫清楚為什麼那筆沒有點數", led.note, true);

c.close();
ch.kill();
try { fs.rmSync(profile, { recursive: true, force: true }); } catch { /* Chrome 還握著暫存檔，無所謂 */ }
console.log("\n總結:", FAIL ? `${FAIL} 項失敗` : "全部通過");
process.exit(FAIL ? 1 : 0);
