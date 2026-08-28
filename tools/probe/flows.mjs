/* flows.mjs — 真瀏覽器實測兩個「狀態型」動效
   ------------------------------------------------------------------
     ① 表單面板開關：開啟播 tl-sheet-in、關閉播**獨立的** tl-sheet-out，
        而且 .closing 一定會被計時器拿掉（流程沒有掛 animationend）。
     ② 剛存的那一筆：存完之後那一筆掛 .fresh，畫面上真的跑 tl-fresh-glow，
        而且約 2 秒後不再標記（之後的重繪不會再閃一次）。

   ⚠️ 這支會在瀏覽器的 localStorage 裡寫測試資料 —— 那是 headless 的暫存 profile，
      **不會碰到 Benson 手機或電腦上的任何紀錄**（也沒有網路寫入：GitHub 全擋、沒有金鑰）。
   ⚠️ 一律拆掉 SW 註冊（controllerchange 會 location.reload()）。

   用法：node tools/probe/flows.mjs [--port=8611] [--dev=9611]
   exit 0 ＝ 過；1 ＝ 不合格；2 ＝ 尺壞了
*/
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import http from "node:http";
import { CDP } from "./cdp.mjs";

const CHROME = process.env.CHROME || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const A = Object.fromEntries(process.argv.slice(2).map(s => {
  const [k, v] = s.replace(/^--/, "").split("=");
  return [k, v ?? true];
}));
const PORT = Number(A.port || 8611);
const DEV = Number(A.dev || 9611);
const ROOT = path.resolve(import.meta.dirname, "../..");

if (!fs.existsSync(CHROME)) { console.log("[未能執行] 找不到 Chrome：" + CHROME); process.exit(2); }
const sleep = ms => new Promise(r => setTimeout(r, ms));

const MIME = {
  ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8", ".json": "application/json; charset=utf-8",
  ".webmanifest": "application/manifest+json; charset=utf-8", ".png": "image/png"
};
const server = http.createServer((req, res) => {
  let p = decodeURIComponent(req.url.split("?")[0]);
  if (p.endsWith("/")) p += "index.html";
  const file = path.join(ROOT, p);
  if (!file.startsWith(ROOT)) { res.writeHead(403).end(); return; }
  fs.readFile(file, (err, buf) => {
    if (err) { res.writeHead(404).end("404"); return; }
    let body = buf;
    if (p.endsWith("index.html")) {
      body = Buffer.from(buf.toString("utf8").replace("'serviceWorker' in navigator", "false"), "utf8");
    }
    res.writeHead(200, { "content-type": MIME[path.extname(file)] || "application/octet-stream", "cache-control": "no-store" });
    res.end(body);
  });
});
await new Promise(r => server.listen(PORT, "127.0.0.1", r));

const profile = path.join(os.tmpdir(), "tl-flows-" + DEV);
try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) {}
const ch = spawn(CHROME, ["--headless=new", "--remote-debugging-port=" + DEV,
  "--user-data-dir=" + profile, "--no-first-run", "--no-default-browser-check",
  "--hide-scrollbars", "about:blank"], { stdio: "ignore", shell: false });
for (let i = 0; i < 200; i++) {
  try { await fetch("http://127.0.0.1:" + DEV + "/json/version"); break; } catch (e) { await sleep(100); }
}
const t = await (await fetch("http://127.0.0.1:" + DEV + "/json/new?about:blank", { method: "PUT" })).json();
const ws = new WebSocket(t.webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener("open", r));
const c = new CDP(ws);
await c.send("Page.enable");
await c.send("Network.enable");
await c.send("Network.setBlockedURLs", { urls: ["*github.io*", "*githubusercontent.com*", "*api.github.com*"] });
await c.send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
c.on("Page.javascriptDialogOpening", () => c.send("Page.handleJavaScriptDialog", { accept: true }));
await c.send("Page.navigate", { url: "http://127.0.0.1:" + PORT + "/index.html" });
await sleep(3400);

async function ev(expr) {
  const r = await c.send("Runtime.evaluate", { expression: expr, returnByValue: true });
  if (r.exceptionDetails) throw new Error(String(r.exceptionDetails.exception && r.exceptionDetails.exception.description).slice(0, 300));
  return r.result.value;
}
const bad = [];
function must(cond, msg) { if (!cond) bad.push(msg); return cond; }
function line(k, v) { console.log("  " + String(k).padEnd(30) + v); }

/* ---------- ① 面板開關 ---------- */
console.log("=== ① 表單面板開關 ===");
await ev("document.getElementById('openBtn').click()");
await sleep(60);
const openState = JSON.parse(await ev(`JSON.stringify({
  cls: document.getElementById('sheet').className,
  anim: getComputedStyle(document.getElementById('sheet')).animationName,
  dur: getComputedStyle(document.getElementById('sheet')).animationDuration,
  scrimAnim: getComputedStyle(document.getElementById('scrim')).animationName,
  n: document.getAnimations().length
})`));
line("開啟時 #sheet class / 動畫", openState.cls + " / " + openState.anim + " " + openState.dur);
line("開啟時 #scrim 動畫", openState.scrimAnim);
line("開啟瞬間 running 動畫數", openState.n);
must(openState.anim === "tl-sheet-in", "① 開啟沒有播 tl-sheet-in（實測 " + openState.anim + "）");
must(openState.scrimAnim === "tl-fade-in", "① 遮罩開啟沒有播 tl-fade-in（實測 " + openState.scrimAnim + "）");
must(openState.n > 0, "① 開啟時 getAnimations() 是空的 ⇒ 尺壞了或動畫沒跑");

/* 位移真的有發生：抓幾幀 transform */
const mid = JSON.parse(await ev(`(function(){
  var s = document.getElementById('sheet');
  return JSON.stringify({ tf: getComputedStyle(s).transform });
})()`));
line("開啟中的 transform", mid.tf);

await sleep(400);
await ev("document.getElementById('cancelBtn').click()");
await sleep(60);
const closing = JSON.parse(await ev(`JSON.stringify({
  cls: document.getElementById('sheet').className,
  anim: getComputedStyle(document.getElementById('sheet')).animationName,
  fill: getComputedStyle(document.getElementById('sheet')).animationFillMode,
  scrimAnim: getComputedStyle(document.getElementById('scrim')).animationName,
  tf: getComputedStyle(document.getElementById('sheet')).transform
})`));
line("關閉中 class / 動畫 / fill", closing.cls + " / " + closing.anim + " / " + closing.fill);
line("關閉中 #scrim 動畫", closing.scrimAnim);
line("關閉中的 transform", closing.tf);
must(closing.anim === "tl-sheet-out", "① 關閉沒有播獨立的 tl-sheet-out（實測 " + closing.anim + "）");
must(closing.scrimAnim === "tl-fade-out", "① 遮罩關閉沒有播 tl-fade-out（實測 " + closing.scrimAnim + "）");

await sleep(500);
const after = JSON.parse(await ev(`JSON.stringify({
  cls: document.getElementById('sheet').className,
  scrimCls: document.getElementById('scrim').className,
  anim: getComputedStyle(document.getElementById('sheet')).animationName,
  n: document.getAnimations().length
})`));
line("關閉後 500ms class", "#sheet「" + after.cls + "」 #scrim「" + after.scrimCls + "」");
line("關閉後動畫", after.anim + "，running=" + after.n);
must(after.cls.indexOf("closing") < 0 && after.scrimCls.indexOf("closing") < 0,
  "① .closing 沒有被計時器拿掉（殘留：" + after.cls + " / " + after.scrimCls + "）");

/* 快速開關兩次：確認計時器有被取消、不會把剛開的面板弄成 closing */
await ev("document.getElementById('openBtn').click()");
await sleep(30);
await ev("document.getElementById('cancelBtn').click()");
await sleep(30);
await ev("document.getElementById('openBtn').click()");
await sleep(400);
const rapid = JSON.parse(await ev(`JSON.stringify({
  cls: document.getElementById('sheet').className,
  tf: getComputedStyle(document.getElementById('sheet')).transform
})`));
line("連續開關開之後", rapid.cls + " / transform=" + rapid.tf);
must(rapid.cls.indexOf("show") >= 0 && rapid.cls.indexOf("closing") < 0,
  "① 連續開關之後面板狀態壞掉：" + rapid.cls);
must(rapid.tf === "none" || /matrix\(1, 0, 0, 1, 0, 0\)/.test(rapid.tf),
  "① 連續開關之後面板沒有停在定位（transform=" + rapid.tf + "）");

/* ---------- ② 剛存的那一筆 ---------- */
console.log("\n=== ② 剛存的那一筆會高亮 ===");
/* 用今天的日期存一筆 → 會出現在「今日」格（.trade） */
await ev(`(function(){
  var e = document.getElementById('entry'), x = document.getElementById('exit');
  function set(el, v){
    var d = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    d.call(el, v); el.dispatchEvent(new Event('input', {bubbles:true}));
  }
  set(e, '23200'); set(x, '23300');
  document.getElementById('saveBtn').click();
})()`);
await sleep(120);
const fresh = JSON.parse(await ev(`(function(){
  var el = document.querySelector('#todaySlot .trade');
  if (!el) return JSON.stringify({ missing: true });
  var cs = getComputedStyle(el);
  return JSON.stringify({
    cls: el.className,
    anim: cs.animationName, dur: cs.animationDuration, delay: cs.animationDelay,
    fill: cs.animationFillMode, shadow: cs.boxShadow, border: cs.borderTopColor
  });
})()`));
line("今日那一筆 class", fresh.cls);
line("動畫 / 時長 / 延遲 / fill", fresh.anim + " / " + fresh.dur + " / " + fresh.delay + " / " + fresh.fill);
line("高亮期間 box-shadow", (fresh.shadow || "").slice(0, 70));
line("高亮期間 border-color", fresh.border);
must(!fresh.missing, "② 存完之後今日格沒有那一筆");
must((fresh.cls || "").indexOf("fresh") >= 0, "② 剛存的那一筆沒有掛 .fresh（class=" + fresh.cls + "）");
must(/tl-fresh-glow/.test(fresh.anim || ""), "② 沒有跑 tl-fresh-glow（實測 " + fresh.anim + "）");
must(/backwards/.test(fresh.fill || ""), "② fresh 的 fill-mode 不是 backwards（" + fresh.fill + "）⇒ 會把 :active 吃掉");

/* 動畫跑完 + 標記過期之後重繪一次，不可以再閃 */
await sleep(2600);
await ev("document.querySelector('#modeBar button[data-mode=real]').click(); document.querySelector('#modeBar button[data-mode=sim]').click(); 1");
await sleep(200);
const later = JSON.parse(await ev(`(function(){
  var el = document.querySelector('#todaySlot .trade');
  return JSON.stringify({ cls: el ? el.className : null, anim: el ? getComputedStyle(el).animationName : null,
                          tf: el ? getComputedStyle(el).transform : null });
})()`));
line("2.6 秒後重繪的 class", later.cls);
line("2.6 秒後重繪的動畫", later.anim);
line("2.6 秒後的 transform", later.tf);
must((later.cls || "").indexOf("fresh") < 0, "② 過了 2 秒還在標記 fresh（每次重繪都會再閃一次）");
must(later.tf === "none", "② 進場動畫殘留了 transform（" + later.tf + "）⇒ :active 會失效");

c.close(); ch.kill(); server.close();
console.log("");
if (bad.length) { bad.forEach(m => console.log("[錯誤] " + m)); console.log("\n[未過] " + bad.length + " 條。"); process.exit(1); }
console.log("[通過] 面板開關（獨立的 *-out ＋ .closing 有被計時器收掉）與剛存那一筆的高亮都合格。");
process.exit(0);
