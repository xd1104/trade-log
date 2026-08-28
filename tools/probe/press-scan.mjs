/* press-scan.mjs — 用真 Chrome ＋ 真滑鼠事件，**全掃描**每一個可點元素的按下回饋
   ------------------------------------------------------------------
   為什麼要有它：
     「按下回饋每個可點的東西都要有」這件事，**列白名單一定會漏**
     （好雷嗎那一輪就是列白名單漏了第 5 顆，自測全綠）。
     而且 `animation-fill-mode:both` 把 :active 永久蓋掉那一類 bug，
     **只有在真瀏覽器裡壓下去量 computed transform 才抓得到** ——
     純功能測試（點得動、流程過）與靜態 CSS 檢查都抓不到。

   量法（四個自證，缺一不可）：
     ① 元素清單是**掃出來的**：button / a / [role=button] / [tabindex]
        ＋ 任何 computed cursor 是 pointer 的元素，取聯集。
        掃到少於 MIN_TARGETS 個就判定「尺壞了」，不是「通過」。
     ② 壓下去之前先做**命中測試**（elementFromPoint）：被面板／遮罩蓋住的
        這一輪不算「沒有回饋」，記成「這個階段測不到」。六個階段都測不到才算未能測，
        而且會被印出來 —— **不可以靜靜當成通過**。
     ③ 用 Input.dispatchMouseEvent 發**真的**滑鼠事件（不是 dispatchEvent 假事件，
        也不是 CSS.forcePseudoState），壓下去之後才讀 computed transform。
        ⚠️ 放開之前先把游標移到別的地方 ⇒ 不會觸發 click，
           才不會在掃描過程中真的按到「重設為初始資料」「強制更新」。
     ④ **負控組**：把 css/motion.css 的按下回饋換成 transform:none 再掃一次，
        這支必須抓到一堆「沒有回饋」。沒有負控組的話「全部都有回饋」有可能只是判準恆真。

   另外驗一件事：**清單重繪（換模式）之後，:active 仍然有效**
   —— 進場動畫如果用了 both／forwards，殘留的 transform 會把 :active 蓋掉。

   用法：
     node tools/probe/press-scan.mjs
     node tools/probe/press-scan.mjs --port=8411 --dev=9811
   exit 0 ＝ 過；1 ＝ 有可點元素沒有回饋／沒測到；2 ＝ 尺壞了
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
const PORT = Number(A.port || 8411);
const DEV = Number(A.dev || 9811);
const ROOT = path.resolve(import.meta.dirname, "../..");
const MIN_TARGETS = 20;          /* 掃到比這少 ＝ 尺壞了 */

/* ⭐ 明確的例外清單：可點、但**刻意沒有**按下回饋的元素。
   長度有斷言（防止有人把礙事的元素偷偷加進來矇混過關）。 */
const EXEMPT = [
  { sel: "#scrim", why: "背景遮罩：點它是關閉，但它不是一顆按鈕，縮放會像整個畫面在抖" }
];
const EXEMPT_COUNT = 1;

if (!fs.existsSync(CHROME)) {
  console.log("[未能執行] 找不到 Chrome：" + CHROME);
  console.log("           這支沒跑 ＝「每個可點元素都有按下回饋」沒有被真瀏覽器驗過。");
  process.exit(2);
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

const MIME = {
  ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8", ".json": "application/json; charset=utf-8",
  ".webmanifest": "application/manifest+json; charset=utf-8", ".png": "image/png"
};
let killMotion = false;          /* 負控組：把按下回饋換成 transform:none */
const server = http.createServer((req, res) => {
  let p = decodeURIComponent(req.url.split("?")[0]);
  if (p.endsWith("/")) p += "index.html";
  const file = path.join(ROOT, p);
  if (!file.startsWith(ROOT)) { res.writeHead(403).end(); return; }
  fs.readFile(file, (err, buf) => {
    if (err) { res.writeHead(404).end("404"); return; }
    let body = buf;
    /* ⚠️ 一律拆掉 SW 註冊：這支 App 在 controllerchange 時會 location.reload()，
       掃到一半整頁重載會讓量測作廢（實測 navType="reload"）。 */
    if (p.endsWith("index.html")) {
      body = Buffer.from(buf.toString("utf8").replace("'serviceWorker' in navigator", "false"), "utf8");
    }
    if (killMotion && p.endsWith("motion.css")) {
      body = Buffer.from(buf.toString("utf8").replace(/transform:scale\(var\(--press[^)]*\)\);/g, "transform:none;"), "utf8");
    }
    res.writeHead(200, {
      "content-type": MIME[path.extname(file)] || "application/octet-stream",
      "cache-control": "no-store"
    });
    res.end(body);
  });
});
await new Promise(r => server.listen(PORT, "127.0.0.1", r));

/* ---- 頁面裡的掃描器 ---- */
const SCAN = String.raw`
(function(){
  window.__desc = function(el){
    var s = el.tagName.toLowerCase();
    if (el.id) s += "#" + el.id;
    if (el.className && typeof el.className === "string") {
      s += "." + el.className.trim().split(/\s+/).filter(Boolean).slice(0,2).join(".");
    }
    var txt = (el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 14);
    return s + (txt ? " [" + txt + "]" : "");
  };
  /* 全掃描：button / a / [role=button] / [tabindex] + 任何 cursor:pointer 的元素。
     cursor 會**繼承**：.trow 底下的 span 也會是 pointer，但它們不是獨立的點擊目標
     （回饋發生在祖先身上）。所以「只有 cursor 中選」的元素，如果祖先已經在名單裡就丟掉；
     本身就是 button/a/[role=button]/[tabindex] 的一律留著（巢狀按鈕要測得到）。 */
  function ownTarget(el){
    var t = el.tagName.toLowerCase();
    return t === "button" || t === "a" || el.getAttribute("role") === "button" || el.hasAttribute("tabindex");
  }
  function anyTarget(el){
    return ownTarget(el) || getComputedStyle(el).cursor === "pointer";
  }
  window.__targets = function(){
    var all = [].slice.call(document.querySelectorAll("*"));
    var out = [];
    for (var i = 0; i < all.length; i++) {
      var el = all[i];
      if (el.closest && el.closest("#splash")) continue;
      var tag = el.tagName.toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select") continue;
      var cs = getComputedStyle(el);
      var own = ownTarget(el);
      if (!own && cs.cursor !== "pointer") continue;
      if (el.disabled) continue;
      var r = el.getBoundingClientRect();
      if (r.width < 6 || r.height < 6) continue;
      if (cs.visibility === "hidden" || cs.display === "none") continue;
      /* ⚠️ 只有「靠 cursor:pointer 中選」的元素要做祖先檢查 —— cursor 是**繼承**的，
         .trow 底下的 span 也會是 pointer，但回饋發生在 .trow 身上，不是它們身上。
         ⚠️ 祖先檢查**不可以**只在「已入選清單」裡找：清單重繪那一瞬間 .trow 正在跑
            進場動畫（opacity 中途值），第一版拿 opacity===0 當「看不見」把它濾掉了
            ⇒ 它的 span 子孫因此變成孤兒、被當成獨立目標，冒出 16 條假錯誤。
            現在直接往上走 parentElement，跟入選與否無關。 */
      if (!own) {
        var p = el.parentElement, anc = false;
        while (p && p !== document.documentElement) {
          if (anyTarget(p)) { anc = true; break; }
          p = p.parentElement;
        }
        if (anc) continue;
      }
      out.push(el);
    }
    window.__T = out;
    return out.map(window.__desc);
  };
  /* 命中測試：捲到畫面中央，再確認那個座標真的打得到它（不然就是被面板／遮罩蓋住） */
  window.__center = function(i){
    var el = window.__T[i];
    el.scrollIntoView({ block: "center", inline: "center" });
    var r = el.getBoundingClientRect();
    var x = Math.round(r.left + r.width / 2), y = Math.round(r.top + r.height / 2);
    var inView = r.top >= 0 && r.bottom <= innerHeight && r.left >= 0 && r.right <= innerWidth;
    var hit = document.elementFromPoint(x, y);
    var reachable = inView && !!hit && (hit === el || el.contains(hit));
    return { x: x, y: y, reachable: reachable, hit: hit ? window.__desc(hit) : "(none)" };
  };
  window.__tf = function(i){ return getComputedStyle(window.__T[i]).transform; };
})();
`;

let runNo = 0;
async function boot() {
  const profile = path.join(os.tmpdir(), "tl-press-" + DEV + "-" + (++runNo));
  try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) {}
  /* ⚠️ --user-data-dir 一定要指到暫存資料夾：不指的話會去搶 Benson 正在用的 profile。 */
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
  await c.send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: false });
  /* confirm()／alert() 會把 headless 卡住 —— 一律自動關掉 */
  c.on("Page.javascriptDialogOpening", () => c.send("Page.handleJavaScriptDialog", { accept: false }));
  await c.send("Page.addScriptToEvaluateOnNewDocument", { source: SCAN });
  await c.send("Page.navigate", { url: "http://127.0.0.1:" + PORT + "/index.html" });
  /* 等開場收完（MIN_SHOW 1490 ＋ 收場 400）＋ .boot 進場動畫跑完（1400） */
  await sleep(3600);
  return { c, ch };
}
async function ev(c, expr) {
  const r = await c.send("Runtime.evaluate", { expression: expr, returnByValue: true });
  if (r.exceptionDetails) throw new Error(expr + " -> " + String(r.exceptionDetails.exception && r.exceptionDetails.exception.description).slice(0, 200));
  return r.result.value;
}

/* 壓一顆下去，量 computed transform，然後把游標移開再放開（不觸發 click） */
async function press(c, i) {
  const pos = JSON.parse(await ev(c, "JSON.stringify(window.__center(" + i + "))"));
  if (!pos.reachable) return { skipped: true, hit: pos.hit };
  await c.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: pos.x, y: pos.y, button: "none", buttons: 0 });
  await c.send("Input.dispatchMouseEvent", { type: "mousePressed", x: pos.x, y: pos.y, button: "left", buttons: 1, clickCount: 1 });
  await sleep(140);                       /* > --dur-press 120ms，讓 transition 走完 */
  const during = await ev(c, "window.__tf(" + i + ")");
  await c.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: 2, y: 2, button: "left", buttons: 1 });
  await c.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: 2, y: 2, button: "left", buttons: 0, clickCount: 1 });
  await sleep(20);
  return { during };
}

/* matrix(a,b,c,d,e,f) 的 a 就是水平縮放 */
function scaleOf(tf) {
  if (!tf || tf === "none") return 1;
  const m = /^matrix\(([-\d.eE]+)/.exec(tf);
  return m ? Number(m[1]) : NaN;
}

async function scanPhase(c, label, prep) {
  if (prep) { await ev(c, prep); await sleep(700); }
  const list = JSON.parse(await ev(c, "JSON.stringify(window.__targets())"));
  const rows = [];
  for (let i = 0; i < list.length; i++) {
    const r = await press(c, i);
    if (r.skipped) { rows.push({ label, name: list[i], skipped: true, hit: r.hit }); continue; }
    rows.push({ label, name: list[i], during: r.during, s: scaleOf(r.during) });
  }
  return rows;
}

async function fullScan() {
  const { c, ch } = await boot();
  try {
    const rows = [];
    rows.push(...await scanPhase(c, "首頁", null));
    rows.push(...await scanPhase(c, "自訂區間",
      "document.querySelector('#seg button[data-type=custom]').click()"));
    rows.push(...await scanPhase(c, "記錄面板", "document.getElementById('openBtn').click()"));
    rows.push(...await scanPhase(c, "編輯面板",
      "document.getElementById('cancelBtn').click(); setTimeout(function(){var r=document.querySelector('.trow'); if(r) r.click();}, 350); 1"));
    rows.push(...await scanPhase(c, "手續費面板",
      "document.getElementById('cancelBtn').click(); setTimeout(function(){document.getElementById('settingsBtn').click();}, 350); 1"));
    rows.push(...await scanPhase(c, "換模式後（清單剛重繪）",
      "document.getElementById('feeCancelBtn').click(); setTimeout(function(){document.querySelector('#modeBar button[data-mode=real]').click(); setTimeout(function(){document.querySelector('#modeBar button[data-mode=sim]').click();}, 250);}, 350); 1"));
    return rows;
  } finally { ch.kill(); }
}

/* 跨階段合併：同一個元素在任何一個階段量到回饋就算過；
   六個階段都被遮住 ＝「未能測」，要出聲，不可以算成通過。 */
function analyse(rows) {
  const byName = new Map();
  rows.forEach(r => {
    const cur = byName.get(r.name) || { name: r.name, best: -1, phases: [], tested: 0, skipped: 0 };
    if (r.skipped) { cur.skipped++; }
    else {
      cur.tested++;
      const s = Number.isNaN(r.s) ? -1 : r.s;
      if (cur.best < 0 || s < cur.best) { cur.best = s; cur.bestPhase = r.label; }
      cur.phases.push(r.label + "=" + (Number.isNaN(r.s) ? "?" : r.s.toFixed(3)));
    }
    byName.set(r.name, cur);
  });
  const uniq = [...byName.values()];
  const bad = [], untested = [], exempted = [];
  uniq.forEach(r => {
    if (EXEMPT.some(e => r.name.indexOf(e.sel) >= 0)) { exempted.push(r); return; }
    if (!r.tested) { untested.push(r); return; }
    if (!(r.best < 0.9995)) bad.push(r.name + "：按下去沒有縮放（" + r.phases.join(", ") + "）");
  });
  return { uniq, bad, untested, exempted };
}

console.log("量測條件：390x844、真滑鼠事件、每顆壓 140ms 後讀 computed transform、放開前把游標移開（不觸發 click）");
console.log("六個階段：首頁／自訂區間／記錄面板／編輯面板／手續費面板／換模式後\n");

const real = analyse(await fullScan());
console.log("=== 現行版：全掃描到 " + real.uniq.length + " 個可點目標 ===");
real.uniq.slice().sort((a, b) => a.name.localeCompare(b.name)).forEach(r => {
  const ex = EXEMPT.some(e => r.name.indexOf(e.sel) >= 0);
  const mark = ex ? "（例外）" : (!r.tested ? "未能測" : (r.best < 0.9995 ? "  ✓ " : "  ✗ "));
  const val = r.tested ? ("scale=" + r.best.toFixed(3) + " @" + r.bestPhase) : ("（每個階段都被蓋住 " + r.skipped + " 次）");
  console.log("  " + mark + " " + val.padEnd(30) + " " + r.name);
});
real.bad.forEach(m => console.log("  [錯誤] " + m));
real.untested.forEach(r => console.log("  [未能測] " + r.name + "（六個階段都被別的東西蓋住）"));

killMotion = true;
const neg = analyse(await fullScan());
killMotion = false;
console.log("\n=== 負控組：把 motion.css 的 transform:scale(var(--press*)) 換成 none ===");
console.log("  抓到 " + neg.bad.length + " 個沒有回饋（現行版 " + real.bad.length + " 個）");
neg.bad.slice(0, 40).forEach(m => console.log("    · " + m.split("：")[0]));

server.close();
let code = 0;
if (real.uniq.length < MIN_TARGETS) { console.log("\n[尺壞了] 只掃到 " + real.uniq.length + " 個目標（門檻 " + MIN_TARGETS + "）。"); code = 2; }
if (EXEMPT.length !== EXEMPT_COUNT) { console.log("\n[尺壞了] 例外清單長度是 " + EXEMPT.length + "，斷言是 " + EXEMPT_COUNT + "。"); code = 2; }
if (neg.bad.length <= real.bad.length) { console.log("\n[尺壞了] 負控組抓到的數量沒有比現行版多 ⇒ 這支量的東西是恆綠的。"); code = 2; }
if (real.bad.length) { console.log("\n[未過] 有 " + real.bad.length + " 個可點元素沒有按下回饋。"); code = code || 1; }
if (real.untested.length) { console.log("\n[未過] 有 " + real.untested.length + " 個目標從頭到尾沒被測到，不算通過。"); code = code || 1; }
if (!code) {
  console.log("\n[通過] " + (real.uniq.length - real.exempted.length) + " 個可點目標全部有按下回饋；" +
    "例外 " + real.exempted.length + " 個（" + EXEMPT.map(e => e.sel).join("、") + "）；" +
    "負控組被抓到 " + neg.bad.length + " 個 ⇒ 這把尺會紅。");
}
process.exit(code);
