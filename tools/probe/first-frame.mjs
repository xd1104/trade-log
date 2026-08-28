/* first-frame.mjs — 用真 Chrome 驗「第一次繪製那一幀 ＝ 動畫的起始狀態」
   ------------------------------------------------------------------
   為什麼要有它（v1.6.1，2026-08-27）：
     三支樣式表是非阻塞的 ⇒ 第一次繪製是 index.html 的關鍵路徑 inline CSS 畫的，
     css/splash.css 幾十毫秒之後才套用、動畫從頭跑。兩者只要不一致，畫面就會跳一次
     —— Benson 的螢幕錄影逐格（59.94fps）拍到的正是這個（畫格 89：符號突然變半透明、名字消失）。
     jsdom 沒有 CSS 引擎，t14 §75c 只能做**靜態比對**；這支是另一把獨立的尺：
     在真瀏覽器裡「CSS 套用前後各取一次樣」，比對 computed opacity 必須一致。

   量法（三個自證，缺一不可）：
     ① 逐 rAF 取樣（不是 --dump-dom 一次性快照，那拿不到時序）
     ② 取樣點用「頁面自己看得到的狀態」判斷（三支 link 的 media 都變成 all），不是用外部時鐘猜
     ③ **負控組**：把關鍵路徑塊換成 v1.6.0 的完成態再量一次，這把尺必須翻紅。
        沒有負控組的話「兩次取樣都一樣」有可能只是因為根本沒取到樣。

   順便驗第二件事：**#splash 在第一次繪製那一幀就要蓋滿整個 viewport**
   （Benson 的錄影在畫格 89–90 看到畫面下緣露出深色的 App 內容）。

   用法：
     node scripts/probe/first-frame.mjs
     node scripts/probe/first-frame.mjs --port=8181 --dev=9781 --delay=700
   exit 0 ＝ 過；1 ＝ 有東西在跳；2 ＝ 尺壞了（沒取到樣、找不到 Chrome…）
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
const PORT = Number(A.port || 8181);
const DEV = Number(A.dev || 9781);
const ROOT = path.resolve(import.meta.dirname, "../..");
/* 人為延遲 CSS，把那個窗口撐開到量得到（真實網路上它是幾十毫秒～幾百毫秒） */
const CSS_DELAY = Number(A.delay || 700);

if (!fs.existsSync(CHROME)) {
  console.log("[未能執行] 找不到 Chrome：" + CHROME + "（設環境變數 CHROME 指到執行檔）");
  console.log("           這支沒跑 ＝「第一幀有沒有跳」沒有被真瀏覽器驗過，不要當成通過。");
  process.exit(2);
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

/* ---- 極簡靜態站：可以延遲 CSS、也可以改寫 index.html（負控組用） ---- */
const MIME = {
  ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8", ".json": "application/json; charset=utf-8",
  ".webmanifest": "application/manifest+json; charset=utf-8", ".png": "image/png"
};
let rewriteIndex = null;
const server = http.createServer((req, res) => {
  let p = decodeURIComponent(req.url.split("?")[0]);
  if (p.endsWith("/")) p += "index.html";
  const file = path.join(ROOT, p);
  if (!file.startsWith(ROOT)) { res.writeHead(403).end(); return; }
  const send = () => fs.readFile(file, (err, buf) => {
    if (err) { res.writeHead(404).end("404"); return; }
    let body = buf;
    if (p.endsWith("index.html")) {
      /* ⚠️ 一律先把 Service Worker 的註冊拆掉。
         這支 App 的 index.html 在 controllerchange 時會 location.reload()，
         第一次進站 SW 一 claim 就整頁重載 ⇒ 取樣器的 window.__S 被沖掉、
         而且第二次載入是「熱啟動」（開場根本不播）。實測 navType="reload"、cold=false。
         ⚠️ Network.setBlockedURLs 擋不住 SW 的 script 抓取（走另一條 loader），
            所以要在 HTML 這一層拆。SW 那條路另外測。 */
      body = Buffer.from(buf.toString("utf8").replace("'serviceWorker' in navigator", "false"), "utf8");
    }
    if (p.endsWith("index.html") && rewriteIndex) {
      body = Buffer.from(rewriteIndex(body.toString("utf8")), "utf8");
    }
    res.writeHead(200, {
      "content-type": MIME[path.extname(file)] || "application/octet-stream",
      "cache-control": "no-store"
    });
    res.end(body);
  });
  if (/\.css$/.test(p)) { setTimeout(send, CSS_DELAY); } else { send(); }
});
await new Promise(r => server.listen(PORT, "127.0.0.1", r));

/* ---- 頁面裡的取樣器：逐 rAF 記錄 computed style ＋ #splash 的覆蓋範圍 ---- */
const SAMPLER = [
  "window.__S = [];",
  "(function(){",
  "  function loop(){",
  "    var sp = document.getElementById('splash');",
  "    if (sp) {",
  "      var g = document.querySelector('.sp-glyph'), n = document.querySelector('.sp-name');",
  "      var r = sp.getBoundingClientRect();",
  "      var bottom = document.elementFromPoint(Math.floor(innerWidth/2), innerHeight - 1);",
  "      window.__S.push({",
  "        t: Math.round(performance.now()),",
  "        media: [].slice.call(document.querySelectorAll('link[data-splash-css]'))",
  "                 .map(function(l){ return l.media; }).join(','),",
  "        go: g ? getComputedStyle(g).opacity : null,",
  "        no: n ? getComputedStyle(n).opacity : null,",
  "        spbg: getComputedStyle(sp).backgroundColor,",
  "        rect: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],",
  "        vw: innerWidth, vh: innerHeight,",
  "        bottomIsSplash: !!(bottom && (bottom.id === 'splash' || sp.contains(bottom)))",
  "      });",
  "    }",
  "    requestAnimationFrame(loop);",
  "  }",
  "  requestAnimationFrame(loop);",
  "})();"
].join("\n");

let runNo = 0;
async function measure(label) {
  /* ⚠️ 每一輪用**不同的** profile 目錄：Chrome 被 kill 之後 CrashpadMetrics-active.pma
     還會被鎖住幾秒，重用同一個目錄會 EBUSY（Windows）。 */
  const profile = path.join(os.tmpdir(), "tl-firstframe-" + DEV + "-" + (++runNo));
  try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) {}
  /* ⚠️ --user-data-dir 一定要指到暫存資料夾：不指的話會去搶使用者正在用的 profile，
     在 Windows 上直接卡住不回（不是報錯，是掛著）。 */
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
    /* 外部主機一律擋掉：不擋的話開場會等到保險絲，量到的是網路不是動畫。
       ⚠️ sw.js 也擋：這支 App 的 index.html 在 controllerchange 時會 location.reload()，
          第一次進站 SW 一 claim 就重載，會把取樣器的 window.__S 整個沖掉（實測 0 個樣本）。
          這一支量的是「第一次繪製那一幀」，跟 SW 無關。SW 那條路另外測。 */
    await c.send("Network.setBlockedURLs", {
      urls: ["*github.io*", "*githubusercontent.com*", "*api.github.com*", "*/sw.js*"]
    });
    await c.send("Emulation.setDeviceMetricsOverride",
      { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
    await c.send("Page.addScriptToEvaluateOnNewDocument", { source: SAMPLER });
    await c.send("Page.navigate", { url: "http://127.0.0.1:" + PORT + "/index.html" });
    await sleep(CSS_DELAY + 1200);
    const r = await c.send("Runtime.evaluate",
      { expression: "JSON.stringify(window.__S)", returnByValue: true });
    c.close();
    return { samples: JSON.parse(r.result.value), label: label };
  } finally {
    ch.kill();
  }
}

/* CSS 套用＝三支 link 的 media 全部是 all（頁面自己看得到的狀態，不是外部時鐘） */
const applied = s => s.media.length > 0 && s.media.split(",").every(m => m === "all");

function analyse(res) {
  const S = res.samples;
  const out = { label: res.label, bad: [], first: null, after: null, n: S.length };
  if (S.length < 5) { out.bad.push("尺壞了：只取到 " + S.length + " 個樣本"); return out; }
  const first = S[0];
  const idx = S.findIndex(applied);
  if (idx < 0) { out.bad.push("尺壞了：整段取樣裡沒有一幀是「三支 CSS 都套用了」"); return out; }
  if (applied(first)) { out.bad.push("尺壞了：第一個樣本就已經套用 CSS（--delay 太短，窗口沒撐開）"); return out; }
  const after = S[idx];
  out.first = first;
  out.after = after;
  /* ⚠️ 容差 0.08 是**取樣延遲**，不是放水：
     我們最早只能在「media 全部變成 all」的那一幀取到樣，而樣式套用是發生在那一幀的
     style recalc 之前 ⇒ 動畫已經跑了幾毫秒。600ms 的淡入在 60fps 下每幀約 0.028，
     給兩幀的餘裕＝0.056，取 0.08。
     真正的「跳」是 1 → 0（差 1.0），比容差大一個數量級 —— 負控組會印出實際差值，
     下面 sanity 那條會斷言它至少是容差的 10 倍，證明容差沒有把真問題吃掉。 */
  const TOL = 0.08;
  out.delta = {};
  [["go", "符號"], ["no", "名字"]].forEach(function (p) {
    const k = p[0], what = p[1];
    if (first[k] === null || after[k] === null) { out.bad.push("尺壞了：" + what + "取不到 opacity"); return; }
    const d = Math.abs(Number(first[k]) - Number(after[k]));
    out.delta[k] = d;
    if (d > TOL) {
      out.bad.push(what + "的 opacity 在 CSS 套用時跳了 " + d.toFixed(3) + "：" +
        first[k] + " → " + after[k] + "（容差 " + TOL + " ＝ 兩幀的取樣延遲）");
    }
  });
  out.tol = TOL;
  if (first.spbg !== after.spbg) out.bad.push("#splash 底色跳了：" + first.spbg + " → " + after.spbg);
  /* 覆蓋：第一次繪製那一幀 #splash 就要蓋滿整個 viewport，而且畫面最底下那一點屬於它 */
  [first, after].forEach(function (s) {
    if (s.rect[0] !== 0 || s.rect[1] !== 0 || s.rect[2] !== s.vw || s.rect[3] !== s.vh) {
      out.bad.push("t=" + s.t + " 時 #splash 沒有蓋滿 viewport：rect=" + s.rect.join(",") +
        " vs " + s.vw + "x" + s.vh);
    }
    if (!s.bottomIsSplash) {
      out.bad.push("t=" + s.t + " 時畫面最下緣那一點不屬於 #splash（會露出 App 內容）");
    }
  });
  return out;
}

console.log("量測條件：390x844、CSS 人為延遲 " + CSS_DELAY + "ms、外部主機全擋、逐 rAF 取樣\n");

const real = analyse(await measure("現行版"));
console.log("=== 現行版 ===");
console.log("  樣本 " + real.n + " 幀");
if (real.first) {
  console.log("  第一次繪製       t=" + real.first.t + "  media=" + real.first.media +
    "  符號 opacity=" + real.first.go + "  名字 opacity=" + real.first.no +
    "  #splash=" + real.first.spbg + "  rect=" + real.first.rect.join(",") +
    "  底緣屬於開場=" + real.first.bottomIsSplash);
  console.log("  CSS 套用後第一幀 t=" + real.after.t + "  media=" + real.after.media +
    "  符號 opacity=" + real.after.go + "  名字 opacity=" + real.after.no +
    "  #splash=" + real.after.spbg + "  rect=" + real.after.rect.join(",") +
    "  底緣屬於開場=" + real.after.bottomIsSplash);
}
real.bad.forEach(m => console.log("  [錯誤] " + m));
if (!real.bad.length) console.log("  OK 第一次繪製那一幀與動畫起始狀態逐項相同，#splash 全程蓋滿");

/* ---- 負控組：把關鍵路徑塊換回 v1.6.0 的「完成態」，這把尺必須翻紅 ---- */
rewriteIndex = src => src
  .replace(/\r?\n\s*opacity:0; transform:translateY\(var\(--lift\)\);/, "")
  .replace(/\r?\n\s*html\[data-splash-intro="light"\] \.sp-glyph\{opacity:0; transform:scale\(var\(--scale-in\)\);\}/, "");
const neg = analyse(await measure("負控組"));
rewriteIndex = null;
console.log("\n=== 負控組：關鍵路徑塊改回 v1.6.0 的完成態 ===");
if (neg.first) {
  console.log("  第一次繪製       符號 opacity=" + neg.first.go + "  名字 opacity=" + neg.first.no);
  console.log("  CSS 套用後第一幀 符號 opacity=" + neg.after.go + "  名字 opacity=" + neg.after.no);
}
neg.bad.forEach(m => console.log("  抓到：" + m));

server.close();
let bad = 0;
if (real.bad.length) { console.log("\n[未過] 現行版有東西在跳。"); bad = 1; }
if (!neg.bad.length) { console.log("\n[尺壞了] 負控組（舊版完成態）竟然也過關 ＝ 這支量的東西是恆綠的。"); bad = 2; }
/* 容差自證：負控組的實際差值必須遠大於容差，否則「容差把真問題吃掉了」 */
const negMax = Math.max(neg.delta ? (neg.delta.go || 0) : 0, neg.delta ? (neg.delta.no || 0) : 0);
if (neg.delta && negMax < (real.tol || 0.08) * 10) {
  console.log("\n[尺壞了] 負控組的最大跳幅只有 " + negMax.toFixed(3) +
    "，不到容差的 10 倍 ⇒ 容差有可能把真問題吃掉了。");
  bad = bad || 2;
} else if (neg.delta) {
  console.log("\n容差自證：現行版最大跳幅 " +
    Math.max(real.delta ? (real.delta.go || 0) : 0, real.delta ? (real.delta.no || 0) : 0).toFixed(3) +
    "、負控組 " + negMax.toFixed(3) + "、容差 " + (real.tol || 0.08) +
    " ⇒ 兩者差一個數量級，容差是取樣延遲不是放水。");
}
if (!bad) console.log("\n[通過] 現行版沒有跳；負控組被抓到 " + neg.bad.length + " 條 ⇒ 這把尺會紅。");
process.exit(bad);
