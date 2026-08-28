/* 狀態列帶探針（v21 起）＝ Benson 2026-08-28 回報那個 bug 的常駐防線。
   症狀：開場漸深時畫面最上緣有一條 26 CSS px 的淺色帶跟不上，約 1 秒後才自己跳成深色。
   根因：status-bar-style 是 `black` ⇒ iOS 在頁面**外面**另外畫一條實心的狀態列底，
         那條底取用 <meta theme-color> 的頻率遠低於每幀 ⇒ splash-boot §7b 追不上。
   修法：改成 `black-translucent` ⇒ 那一帶由我們自己畫 ⇒ 結構上不可能不同步。
   判準：整段開場，頂端 0~90px **不准出現硬邊**（相鄰兩列的 RGB 跳幅 > 6 就是硬邊）。
   ⚠️ 不可以拿 y=6 直接跟畫面中段比：body 有一層金色 radial-gradient，
      上緣本來就被染亮約 3%（CLAUDE.md 記過，第一版探針就是這樣被誤判成紅燈的）。
   自帶負控組：重現舊症狀（頂 26px 貼 #e0e0e0）必須翻紅。
   用法：node tools/probe/status-band.mjs [dark|light|reduce|hot] */
import { spawn } from "node:child_process";
import fs from "node:fs"; import os from "node:os"; import path from "node:path";
import { fileURLToPath } from "node:url";
import { CDP } from "./cdp.mjs";
import { decodePNG, pixel } from "./png.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "../..");
const CHROME = process.env.CHROME || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = 8793, DEV = 9793;
const MODE = process.argv[2] || "dark";
const INSET_T = 59;
const sleep = ms => new Promise(r => setTimeout(r, ms));
const hex = p => "#" + [p[0], p[1], p[2]].map(v => v.toString(16).padStart(2, "0")).join("");

/* 探針一律先把 serviceWorker 拿掉（CLAUDE.md：controllerchange 會整頁 reload 沖掉量測） */
const NOSW = `try{Object.defineProperty(navigator,'serviceWorker',{get:function(){return undefined}})}catch(e){}`;

const srv = spawn(process.execPath, [path.join(HERE, "server.mjs"), ROOT, String(PORT)], { stdio: "ignore", shell: false });
await sleep(600);
const profile = path.join(os.tmpdir(), "tl-band-" + DEV);
try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) {}
const ch = spawn(CHROME, ["--headless=new", "--remote-debugging-port=" + DEV,
  "--user-data-dir=" + profile, "--no-first-run", "--no-default-browser-check",
  "--hide-scrollbars", "about:blank"], { stdio: "ignore", shell: false });

let bad = 0;
try {
  for (let i = 0; i < 200; i++) { try { await fetch("http://127.0.0.1:" + DEV + "/json/version"); break; } catch (e) { await sleep(100); } }
  const t = await (await fetch("http://127.0.0.1:" + DEV + "/json/new?about:blank", { method: "PUT" })).json();
  const ws = new WebSocket(t.webSocketDebuggerUrl);
  await new Promise(r => ws.addEventListener("open", r));
  const c = new CDP(ws);
  await c.send("Page.enable"); await c.send("Network.enable");
  await c.send("Network.setBlockedURLs", { urls: ["*github.io*", "*githubusercontent.com*", "*api.github.com*"] });
  await c.send("Emulation.setDeviceMetricsOverride", { width: 393, height: 852, deviceScaleFactor: 1, mobile: true });
  await c.send("Emulation.setEmulatedMedia", {
    features: [{ name: "prefers-color-scheme", value: MODE === "light" ? "light" : "dark" },
               { name: "prefers-reduced-motion", value: MODE === "reduce" ? "reduce" : "no-preference" }]
  });
  await c.send("Emulation.setSafeAreaInsetsOverride", { insets: { top: INSET_T, bottom: 34, left: 0, right: 0 } });
  await c.send("Page.addScriptToEvaluateOnNewDocument", { source: NOSW });

  if (MODE === "hot") {   /* 熱啟動：先進一次，再用 SPA 式重進（同一個 session） */
    await c.send("Page.navigate", { url: "http://127.0.0.1:" + PORT + "/index.html" });
    await sleep(4000);
  }
  await c.send("Page.navigate", { url: "http://127.0.0.1:" + PORT + "/index.html" });

  /* ⚠️ 不可以拿 y=6 直接跟畫面中段比：body 有一層
     radial-gradient(1200px 500px at 50% -8%, ...)，上緣本來就被染亮約 3%（CLAUDE.md 記過）。
     Benson 回報的症狀是**一條硬邊**（狀態列底跟內容不同步），所以尺要量的是
     「頂端 0~90px 這一段有沒有跨列的硬跳」＋「跨越 inset 邊界（y=59）那一步有多大」。 */
  function scan(img) {
    let maxJump = 0, at = 0;
    for (let y = 1; y <= 90; y++) {
      const a = pixel(img, 8, y - 1), b = pixel(img, 8, y);
      const d = Math.max(Math.abs(a[0] - b[0]), Math.abs(a[1] - b[1]), Math.abs(a[2] - b[2]));
      if (d > maxJump) { maxJump = d; at = y; }
    }
    return { maxJump, at };
  }
  console.log("\n=== " + MODE + "：頂端 0~90px 有沒有硬邊（black-translucent 之下那一帶是我們畫的） ===");
  console.log("  " + "時間".padEnd(9) + "y=6".padEnd(11) + "y=62".padEnd(11) + "跨 inset Δ".padEnd(12) + "0~90 最大跨列跳（在 y=）");
  let prev = 0;
  for (const at of [90, 160, 260, 400, 560, 720, 900, 1200, 1600, 2200, 3000, 4000]) {
    await sleep(at - prev); prev = at;
    const s = await c.send("Page.captureScreenshot", { format: "png" });
    const img = decodePNG(Buffer.from(s.data, "base64"));
    const band = pixel(img, 8, 6), below = pixel(img, 8, 62);
    const d = Math.max(Math.abs(band[0] - below[0]), Math.abs(band[1] - below[1]), Math.abs(band[2] - below[2]));
    const sc = scan(img);
    const ok = sc.maxJump <= 6;      /* 硬邊 ＝ 一列之內跳超過 6 階 */
    if (!ok) bad++;
    console.log("  " + (at + "ms").padEnd(9) + hex(band).padEnd(11) + hex(below).padEnd(11)
      + String(d).padEnd(12) + sc.maxJump + "（y=" + sc.at + "）" + (ok ? "  ✓" : "  ✗"));
  }

  /* 尺的自證：故意重現舊症狀（在頂 26 CSS px 貼一條 #e0e0e0＝ Benson 錄影量到的顏色），必須翻紅。 */
  await c.send("Runtime.evaluate", {
    expression: `(()=>{const d=document.createElement('div');
      d.style.cssText='position:fixed;top:0;left:0;right:0;height:26px;z-index:9999;background:#e0e0e0';
      document.documentElement.appendChild(d); d.id='__tmp__neg';})()`
  });
  await sleep(250);
  const ns = await c.send("Page.captureScreenshot", { format: "png" });
  const ni = decodePNG(Buffer.from(ns.data, "base64"));
  const nsc = scan(ni);
  console.log("\n  負控組（重現舊症狀：頂 26px 貼 #e0e0e0）：最大跨列跳 " + nsc.maxJump + "（y=" + nsc.at + "）"
    + (nsc.maxJump > 6 ? "  ✓ 尺會紅" : "  ✗ 尺壞了"));
  if (nsc.maxJump <= 6) { bad++; }
  await c.send("Runtime.evaluate", { expression: "document.getElementById('__tmp__neg').remove()" });

  /* 收場後：帶的顏色必須等於 App 的 --bg（狀態列那塊由我們畫，不是 iOS 畫） */
  await sleep(300);
  const fin = JSON.parse((await c.send("Runtime.evaluate", {
    expression: `JSON.stringify({htmlBg:getComputedStyle(document.documentElement).backgroundColor,
      bodyBg:getComputedStyle(document.body).backgroundColor,
      bg:getComputedStyle(document.documentElement).getPropertyValue('--bg').trim(),
      splash:document.documentElement.getAttribute('data-splash'),
      cssgate:document.documentElement.hasAttribute('data-cssgate'),
      tc:[].map.call(document.querySelectorAll('meta[name=theme-color]'),m=>m.getAttribute('content')+' @ '+(m.getAttribute('media')||'-')),
      sbs:document.querySelector('meta[name="apple-mobile-web-app-status-bar-style"]').content})`,
    returnByValue: true
  })).result.value);
  console.log("\n=== 收場後 ===");
  console.log("  status-bar-style        " + fin.sbs);
  console.log("  data-splash / cssgate   " + fin.splash + " / " + (fin.cssgate ? "還在" : "已開"));
  console.log("  html / body 底色         " + fin.htmlBg + " / " + fin.bodyBg);
  console.log("  App --bg                " + fin.bg);
  console.log("  theme-color 兩條          " + fin.tc.join("  ｜  "));

  console.log("\n" + (bad ? "[未過] " + bad + " 項" : "[通過] 整段開場，頂端 0~90px 沒有硬邊（最大跨列跳 ≤ 6）"));
  c.close();
} finally { try { ch.kill(); } catch (e) {} try { srv.kill(); } catch (e) {} }
process.exit(bad ? 1 : 0);
