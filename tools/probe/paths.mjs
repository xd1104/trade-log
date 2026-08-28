/* paths.mjs — 真瀏覽器實測「四條降級路徑 ＋ 熱啟動 ＋ 淺色模式」
   ------------------------------------------------------------------
   開場模組是**加分項不是相依性**：任何一塊掉了，App 都必須完整可用。
   這支把每一條路各跑一次，量的是像素與 computed style，不是「看起來沒問題」。

     A 冷啟動（正常）        第一幀 = --sp-start(#ebebeb)、開場收得掉、App 出得來
     B CSS 遲到（+700ms）    同上（窗口撐開也不准跳）
     C 三支 CSS 全 404       第一幀仍是我們決定的 #ebebeb、保險絲收得掉、App 可用
     D js/splash.js 404      splashFallback() 立刻收掉、App 完整可用（會有一次白→深硬切）
     E JS 停用               樣式完整、開場不出現、**App 內容看得到**（noscript 的解除生效）
     F 熱啟動（同分頁二進）  全程深色、開場一幀都不播
     G 淺色模式冷啟動        App 本體正常（開場色票刻意不跟主題變）

   ⚠️ 一律拆掉 SW 註冊：這支 App 在 controllerchange 時會 location.reload()，
      第一次進站 SW 一 claim 就整頁重載，會把量測沖掉（實測 navType="reload"）。
   用法：node tools/probe/paths.mjs [--port=8511] [--dev=9711]
   exit 0 ＝ 全過；1 ＝ 有路徑不合格；2 ＝ 尺壞了
*/
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import http from "node:http";
import { CDP } from "./cdp.mjs";
import { decodePNG, pixel, hex } from "./png.mjs";

const CHROME = process.env.CHROME || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const A = Object.fromEntries(process.argv.slice(2).map(s => {
  const [k, v] = s.replace(/^--/, "").split("=");
  return [k, v ?? true];
}));
const PORT = Number(A.port || 8511);
const DEV = Number(A.dev || 9711);
const ROOT = path.resolve(import.meta.dirname, "../..");

const APP_DARK = "#0f1218";      /* = manifest.background_color = 深色 --bg */
const APP_LIGHT = "#f3f5f8";     /* = 淺色模式的 --bg */
const SP_START = "#ebebeb";      /* = css/splash.css 的 --sp-start */

if (!fs.existsSync(CHROME)) {
  console.log("[未能執行] 找不到 Chrome：" + CHROME);
  process.exit(2);
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

const MIME = {
  ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8", ".json": "application/json; charset=utf-8",
  ".webmanifest": "application/manifest+json; charset=utf-8", ".png": "image/png"
};
let MODE = {};                    /* { cssDelay, css404, splashJs404 } */
const server = http.createServer((req, res) => {
  let p = decodeURIComponent(req.url.split("?")[0]);
  if (p.endsWith("/")) p += "index.html";
  const file = path.join(ROOT, p);
  if (!file.startsWith(ROOT)) { res.writeHead(403).end(); return; }
  if (MODE.css404 && /\.css$/.test(p)) { res.writeHead(404).end("404"); return; }
  if (MODE.splashJs404 && /\/splash\.js$/.test(p)) { res.writeHead(404).end("404"); return; }
  const send = () => fs.readFile(file, (err, buf) => {
    if (err) { res.writeHead(404).end("404"); return; }
    let body = buf;
    if (p.endsWith("index.html")) {
      body = Buffer.from(buf.toString("utf8").replace("'serviceWorker' in navigator", "false"), "utf8");
    }
    res.writeHead(200, {
      "content-type": MIME[path.extname(file)] || "application/octet-stream",
      "cache-control": "no-store"
    });
    res.end(body);
  });
  if (MODE.cssDelay && /\.css$/.test(p)) setTimeout(send, MODE.cssDelay); else send();
});
await new Promise(r => server.listen(PORT, "127.0.0.1", r));

const SAMPLER = `
window.__S = [];
(function(){
  function loop(){
    try {
      var sp = document.getElementById('splash');
      var app = document.querySelector('.app');
      var tc = document.querySelector('meta[name="theme-color"]');
      window.__S.push({
        t: Math.round(performance.now()),
        sp: !!sp,
        out: !!(sp && sp.classList.contains('out')),
        ds: document.documentElement.getAttribute('data-splash'),
        gate: document.documentElement.hasAttribute('data-cssgate'),
        htmlbg: getComputedStyle(document.documentElement).backgroundColor,
        bodybg: getComputedStyle(document.body || document.documentElement).backgroundColor,
        appVis: app ? getComputedStyle(app).visibility : null,
        tc: tc ? tc.getAttribute('content') : null
      });
    } catch (e) {}
    requestAnimationFrame(loop);
  }
  requestAnimationFrame(loop);
})();
`;

let runNo = 0;
async function run(label, opts) {
  MODE = opts.mode || {};
  const profile = path.join(os.tmpdir(), "tl-paths-" + DEV + "-" + (++runNo));
  try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) {}
  const ch = spawn(CHROME, ["--headless=new", "--remote-debugging-port=" + DEV,
    "--user-data-dir=" + profile, "--no-first-run", "--no-default-browser-check",
    "--hide-scrollbars", "about:blank"], { stdio: "ignore", shell: false });
  try {
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
    await c.send("Emulation.setEmulatedMedia", {
      features: [{ name: "prefers-color-scheme", value: opts.scheme || "dark" },
                 { name: "prefers-reduced-motion", value: opts.reduce ? "reduce" : "no-preference" }]
    });
    if (opts.noJs) await c.send("Emulation.setScriptExecutionDisabled", { value: true });
    else await c.send("Page.addScriptToEvaluateOnNewDocument", { source: SAMPLER });
    await c.send("Page.navigate", { url: "http://127.0.0.1:" + PORT + "/index.html" });

    const shots = [];
    for (const at of (opts.shots || [])) {
      await sleep(at - (shots.length ? shots[shots.length - 1].at : 0));
      const s = await c.send("Page.captureScreenshot", { format: "png" });
      shots.push({ at, img: decodePNG(Buffer.from(s.data, "base64")) });
    }
    const waitMore = (opts.wait || 4000) - (shots.length ? shots[shots.length - 1].at : 0);
    if (waitMore > 0) await sleep(waitMore);

    let S = [], extra = null;
    if (!opts.noJs) {
      const r = await c.send("Runtime.evaluate", { expression: "JSON.stringify(window.__S)", returnByValue: true });
      S = JSON.parse(r.result.value || "[]");
      const e = await c.send("Runtime.evaluate", {
        expression: `JSON.stringify({
          rows: document.querySelectorAll('#list .trow').length,
          months: document.querySelectorAll('#list .month').length,
          rate: (document.getElementById('rateNum')||{}).textContent,
          fabTxt: (document.querySelector('.fab button')||{}).textContent,
          spInDom: !!document.getElementById('splash'),
          ds: document.documentElement.getAttribute('data-splash'),
          gate: document.documentElement.hasAttribute('data-cssgate'),
          appVis: getComputedStyle(document.querySelector('.app')).visibility,
          bodybg: getComputedStyle(document.body).backgroundColor,
          tc: (document.querySelector('meta[name="theme-color"]')||{}).content
        })`, returnByValue: true
      });
      extra = JSON.parse(e.result.value);
    }
    /* 熱啟動：同一個分頁再進一次（sessionStorage 還在） */
    let hot = null;
    if (opts.hot) {
      await c.send("Runtime.evaluate", { expression: "window.__S = []" });
      await c.send("Page.navigate", { url: "http://127.0.0.1:" + PORT + "/index.html" });
      await sleep(1200);
      const s2 = await c.send("Page.captureScreenshot", { format: "png" });
      const img2 = decodePNG(Buffer.from(s2.data, "base64"));
      const r2 = await c.send("Runtime.evaluate", { expression: "JSON.stringify(window.__S)", returnByValue: true });
      hot = { S: JSON.parse(r2.result.value || "[]"), img: img2 };
    }
    const final = await c.send("Page.captureScreenshot", { format: "png" });
    c.close();
    return { label, S, extra, shots, hot, finalImg: decodePNG(Buffer.from(final.data, "base64")) };
  } finally { ch.kill(); }
}

/* ⚠️ 量 App 的底色**不可以取左上角**：css/style.css 的 body 有一層
   radial-gradient(1200px 500px at 50% -8%, rgba(227,169,81,.06), transparent 70%)
   —— 畫面上半部整片被金色染掉約 3%（實測深色 #0f1218 → #161619、淺色 #f3f5f8 → #f2f2f2）。
   那是這支 App 本來就有的裝飾，不是 bug。漸層在 70% 之後就完全透明，
   所以「左下角」才是乾淨的 --bg（FAB 是置中的，x=2 不會碰到它）。
   第一次量的時候就是取角落，四條路全部誤判成紅燈 —— 先驗尺再量東西。 */
function corner(img) { return hex(pixel(img, 4, 4)); }              /* 開場中用：#splash 是不透明覆蓋層，沒有漸層問題 */
/* 收場後量 App 的底色：左邊界（x=2，.app 的 16px padding 內，不會有卡片）、
   畫面 62%／74% 兩個高度 —— 上面躲開金色漸層（半徑 500px，約到 y=433 就沒了），
   下面躲開 FAB 的 box-shadow（0 8px 24px rgba(0,0,0,.35)，會把最底下那幾列染暗一階：
   實測淺色模式 h-2 那一列是 #f2f4f7，正確值是 #f3f5f8）。
   ⚠️ 兩點必須一致，不一致就直接判尺壞了 —— 這是這把尺的自證。 */
function appBg(img) {
  const a = hex(pixel(img, 2, Math.round(img.h * 0.62)));
  const b = hex(pixel(img, 2, Math.round(img.h * 0.74)));
  return a === b ? a : ("尺壞了:" + a + "≠" + b);
}
function bottom(img) { return hex(pixel(img, Math.floor(img.w / 2), img.h - 2)); }

const bad = [];
function must(cond, msg) { if (!cond) bad.push(msg); return cond; }
function line(k, v) { console.log("  " + String(k).padEnd(26) + v); }

/* ---------- A 冷啟動（正常） ---------- */
{
  const r = await run("A 冷啟動（深色）", { shots: [90], wait: 4000, scheme: "dark" });
  const first = r.S[0], last = r.S[r.S.length - 1];
  const gone = r.S.find(s => !s.sp);
  console.log("\n=== A 冷啟動（正常、深色模式）===");
  must(r.S.length > 30, "A：只取到 " + r.S.length + " 幀 ⇒ 尺壞了");
  line("第一幀 t / html 底色", first.t + "ms / " + first.htmlbg);
  line("90ms 截圖角落像素", corner(r.shots[0].img));
  line("body 底色（開場中）", first.bodybg);
  line("開場離開 DOM", gone ? gone.t + "ms" : "（沒有離開！）");
  line("theme-color 變動次數", new Set(r.S.map(s => s.tc)).size + " 個相異值，最後 = " + last.tc);
  line("結束後 data-splash/gate", last.ds + " / " + (last.gate ? "還關著" : "已開"));
  line("App 內容", "月份 " + r.extra.months + " 組、列 " + r.extra.rows + " 筆、勝率 " + r.extra.rate);
  must(corner(r.shots[0].img).toLowerCase() === SP_START, "A：90ms 那一幀角落不是 " + SP_START + "（實測 " + corner(r.shots[0].img) + "）");
  must(first.bodybg === "rgba(0, 0, 0, 0)", "A：開場中 body 底色沒有讓開（實測 " + first.bodybg + "）");
  must(gone, "A：開場沒有從 DOM 移除");
  must(last.ds === "off" && !last.gate, "A：收場後 data-splash 不是 off 或閘門沒開");
  must(r.extra.rows > 0, "A：清單一筆都沒有 ⇒ App 沒有正常畫出來");
  must((last.tc || "").toLowerCase() === APP_DARK, "A：theme-color 沒有還原成 " + APP_DARK + "（實測 " + last.tc + "）");
  must(new Set(r.S.map(s => s.tc)).size > 5, "A：theme-color 沒有跟著開場底色走（相異值太少）");
}

/* ---------- B CSS 遲到 ---------- */
{
  const r = await run("B CSS 遲到", { mode: { cssDelay: 700 }, shots: [90], wait: 4600, scheme: "dark" });
  const last = r.S[r.S.length - 1];
  const gone = r.S.find(s => !s.sp);
  console.log("\n=== B CSS 遲到 700ms ===");
  line("90ms 截圖角落像素", corner(r.shots[0].img));
  line("開場離開 DOM", gone ? gone.t + "ms" : "（沒有離開！）");
  line("App 內容", "列 " + r.extra.rows + " 筆、勝率 " + r.extra.rate);
  must(corner(r.shots[0].img).toLowerCase() === SP_START, "B：90ms 那一幀角落不是 " + SP_START);
  must(gone && r.extra.rows > 0 && last.ds === "off", "B：開場沒收乾淨或 App 沒畫出來");
}

/* ---------- C 三支 CSS 全 404 ---------- */
{
  const r = await run("C CSS 全 404", { mode: { css404: true }, shots: [90], wait: 9000, scheme: "dark" });
  const last = r.S[r.S.length - 1];
  const gone = r.S.find(s => !s.sp);
  console.log("\n=== C 三支 CSS 全 404 ===");
  line("90ms 截圖角落像素", corner(r.shots[0].img));
  line("開場離開 DOM", gone ? gone.t + "ms" : "（沒有離開！）");
  line("結束後 data-splash/gate", last.ds + " / " + (last.gate ? "還關著" : "已開"));
  line("App 內容", "列 " + r.extra.rows + " 筆、勝率 " + r.extra.rate + "、FAB 文字「" + (r.extra.fabTxt || "").trim() + "」");
  must(corner(r.shots[0].img).toLowerCase() === SP_START, "C：90ms 那一幀角落不是 " + SP_START + "（實測 " + corner(r.shots[0].img) + "）");
  must(gone, "C：CSS 全掛時開場收不掉（保險絲失效）");
  must(!last.gate, "C：閘門沒有被保險絲打開 ⇒ App 會永遠看不見");
  must(r.extra.rows > 0, "C：App 沒有正常畫出來");
}

/* ---------- D js/splash.js 404 ---------- */
{
  const r = await run("D splash.js 404", { mode: { splashJs404: true }, shots: [90], wait: 4000, scheme: "dark" });
  const last = r.S[r.S.length - 1];
  const gone = r.S.find(s => !s.sp);
  console.log("\n=== D js/splash.js 404 ===");
  line("90ms 截圖角落像素", corner(r.shots[0].img));
  line("開場離開 DOM", gone ? gone.t + "ms（splashFallback）" : "（沒有離開！）");
  line("結束後 data-splash/gate", last.ds + " / " + (last.gate ? "還關著" : "已開"));
  line("最終畫面 左下/角落", appBg(r.finalImg) + " / " + corner(r.finalImg) + "（角落被 App 自己的金色漸層染色，正常）");
  line("App 內容", "列 " + r.extra.rows + " 筆、勝率 " + r.extra.rate);
  must(gone && gone.t < 2500, "D：splashFallback 沒有及時把開場收掉");
  must(!last.gate, "D：閘門沒開 ⇒ App 會看不見");
  must(r.extra.rows > 0, "D：App 沒有正常畫出來");
  must(appBg(r.finalImg).toLowerCase() === APP_DARK, "D：最終畫面左下角不是 App 的深色底（實測 " + appBg(r.finalImg) + "）");
}

/* ---------- E JS 停用 ---------- */
{
  const r = await run("E JS 停用", { noJs: true, wait: 2500, scheme: "dark" });
  const img = r.finalImg;
  const cor = appBg(img);
  /* 內容可見的證據：品牌那顆金色方塊（左上 34x34，約 x=22,y=?）——用整張圖找有沒有非底色像素 */
  let distinct = new Set();
  for (let y = 0; y < img.h; y += 7) for (let x = 0; x < img.w; x += 7) distinct.add(hex(pixel(img, x, y)));
  console.log("\n=== E JS 被停用 ===");
  line("畫面左下角像素", cor);
  line("整張圖相異色數", distinct.size + " 種（>1 ＝ 畫面上有內容，不是一整片底色）");
  must(cor.toLowerCase() === APP_DARK, "E：角落不是 App 的深色底（實測 " + cor + "）⇒ noscript 的 body 覆寫沒生效");
  must(distinct.size > 3, "E：整張圖只有 " + distinct.size + " 種顏色 ⇒ App 內容被藏死了");
}

/* ---------- F 熱啟動 ---------- */
{
  const r = await run("F 熱啟動", { shots: [], wait: 4000, hot: true, scheme: "dark" });
  const H = r.hot.S;
  /* ⚠️ 判準不可以寫成「#splash 在不在 DOM」：熱啟動時 splash-boot 在 body 解析前就掛了
     data-splash="off"（＝ display:none，一幀都沒畫），節點要等 DOM ready 才被 hardRemove()。
     「在 DOM 裡但 display:none」不叫「畫了開場」。要問的是**有沒有被畫出來**。 */
  const shownSplash = H.some(s => s.sp && s.ds !== "off");
  const bgs = new Set(H.map(s => s.htmlbg));
  console.log("\n=== F 熱啟動（同分頁再進一次）===");
  line("第一幀 t / html 底色", (H[0] || {}).t + "ms / " + (H[0] || {}).htmlbg);
  line("開場有沒有被畫出來", shownSplash ? "有（不該有！）" : "沒有 ✓（節點一開始就是 data-splash=off）");
  line("html 底色相異值", [...bgs].join(" / "));
  line("左下角像素", appBg(r.hot.img));
  line("theme-color 相異值", [...new Set(H.map(s => s.tc))].join(" / "));
  must(H.length > 5, "F：熱啟動只取到 " + H.length + " 幀 ⇒ 尺壞了");
  must(!shownSplash, "F：熱啟動竟然畫了開場");
  must(!bgs.has("rgb(235, 235, 235)"), "F：熱啟動出現過白起的白");
  must(appBg(r.hot.img).toLowerCase() === APP_DARK, "F：熱啟動左下角不是 App 深色底（實測 " + appBg(r.hot.img) + "）");
  must(new Set(H.map(s => s.tc)).size === 1, "F：熱啟動的 theme-color 被動過（應該一個位元組都不寫）");
}

/* ---------- G 淺色模式 ---------- */
{
  const r = await run("G 淺色模式", { shots: [90], wait: 4000, scheme: "light" });
  const last = r.S[r.S.length - 1];
  const gone = r.S.find(s => !s.sp);
  console.log("\n=== G 淺色模式冷啟動 ===");
  line("90ms 截圖角落像素", corner(r.shots[0].img) + "（開場色票刻意不跟主題變）");
  line("開場離開 DOM", gone ? gone.t + "ms" : "（沒有離開！）");
  line("最終畫面 左下/角落", appBg(r.finalImg) + " / " + corner(r.finalImg) + "（角落被金色漸層染色，正常）");
  line("結束後 body 底色", r.extra.bodybg);
  line("App 內容", "月份 " + r.extra.months + " 組、列 " + r.extra.rows + " 筆、勝率 " + r.extra.rate);
  must(corner(r.shots[0].img).toLowerCase() === SP_START, "G：90ms 那一幀角落不是 " + SP_START);
  must(gone && r.extra.rows > 0, "G：淺色模式下開場沒收乾淨或 App 沒畫出來");
  must(appBg(r.finalImg).toLowerCase() === APP_LIGHT, "G：收場後左下角不是淺色模式的 " + APP_LIGHT + "（實測 " + appBg(r.finalImg) + "）⇒ body 底色沒有交還");
  must(r.extra.bodybg !== "rgba(0, 0, 0, 0)", "G：收場後 body 底色還是 transparent（沒有交還給 App）");
}

/* ---------- H 減少動態 ---------- */
{
  const r = await run("H 減少動態", { shots: [90], wait: 3000, scheme: "dark", reduce: true });
  const last = r.S[r.S.length - 1];
  const gone = r.S.find(s => !s.sp);
  const white = r.S.some(s => s.htmlbg === "rgb(235, 235, 235)");
  console.log("\n=== H prefers-reduced-motion: reduce ===");
  line("90ms 截圖角落像素", corner(r.shots[0].img) + "（白起被關掉，第一幀就該是深色）");
  line("整段有沒有出現白起的白", white ? "有（不該有！）" : "沒有 ✓");
  line("開場離開 DOM", gone ? gone.t + "ms" : "（沒有離開！）");
  line("App 內容", "列 " + r.extra.rows + " 筆、勝率 " + r.extra.rate);
  must(corner(r.shots[0].img).toLowerCase() === APP_DARK, "H：reduce 之下第一幀不是 " + APP_DARK + "（實測 " + corner(r.shots[0].img) + "）");
  must(!white, "H：reduce 之下還是閃了一次白起的白");
  must(gone && gone.t < 2200, "H：reduce 之下開場沒有更早收掉（實測 " + (gone ? gone.t : "沒收") + "）");
  must(r.extra.rows > 0 && last.ds === "off", "H：reduce 之下 App 沒畫出來或開場沒收乾淨");
}

server.close();
console.log("");
if (bad.length) { bad.forEach(m => console.log("[錯誤] " + m)); console.log("\n[未過] " + bad.length + " 條。"); process.exit(1); }
console.log("[通過] A～H 八條路徑全部合格。");
process.exit(0);
