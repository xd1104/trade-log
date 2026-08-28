/* safe-area 探針（v21 起）：status-bar-style 是 black-translucent
   ⇒ WebView 延伸到狀態列底下 ⇒ 每一個貼頂／貼底的東西都必須自己讓開。
   這支用 CDP 的 Emulation.setSafeAreaInsetsOverride 注入 iPhone 15 Pro 的真實 inset（59/34），
   逐項量 getBoundingClientRect()，**不准用字級或 padding 推算**。
   自帶尺的自證：注入前 env() 必須是 0、注入後必須是 59/34，量不到就直接中止。
   用法：node tools/probe/safe-area.mjs [dark|light] */
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { CDP } from "./cdp.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "../..");
const CHROME = process.env.CHROME || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PORT = 8791, DEV = 9791;
const SCHEME = process.argv[2] === "light" ? "light" : "dark";
const INSET_T = 59, INSET_B = 34;   /* iPhone 15 Pro 直向的實際值 */
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

if (!fs.existsSync(CHROME)) { console.log("[未能執行] 找不到 Chrome：" + CHROME); process.exit(1); }

const srv = spawn(process.execPath, [path.join(HERE, "server.mjs"), ROOT, String(PORT)],
  { stdio: "ignore", shell: false });
await sleep(600);

const profile = path.join(os.tmpdir(), "tl-safearea-" + DEV);
try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) {}
const ch = spawn(CHROME, ["--headless=new", "--remote-debugging-port=" + DEV,
  "--user-data-dir=" + profile, "--no-first-run", "--no-default-browser-check",
  "--hide-scrollbars", "about:blank"], { stdio: "ignore", shell: false });

let bad = 0;
function line(k, v, ok) {
  if (ok === false) bad++;
  console.log("  " + k.padEnd(34, " ") + v + (ok === false ? "   ✗" : ok === true ? "   ✓" : ""));
}

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
  await c.send("Emulation.setDeviceMetricsOverride", { width: 393, height: 852, deviceScaleFactor: 1, mobile: true });
  await c.send("Emulation.setEmulatedMedia", {
    features: [{ name: "prefers-color-scheme", value: SCHEME },
               { name: "prefers-reduced-motion", value: "no-preference" }]
  });

  /* ---- 尺的自證 ①：注入前 env() 必須是 0；注入後必須是 59/34。量不到就整支中止。 ---- */
  let injected = "無";
  await c.send("Page.navigate", { url: "http://127.0.0.1:" + PORT + "/index.html" });
  await sleep(300);
  /* ⚠️ 一定要 box-sizing:content-box：這支 App 的 style.css 有 *{box-sizing:border-box}，
     用 getBoundingClientRect() 量會把 border 吃掉，量到的 top 會小掉一個 bottom（實際踩過：59 量成 25）。 */
  const PROBE = `(()=>{const d=document.createElement('div');
    d.style.cssText='box-sizing:content-box;position:fixed;top:0;left:0;width:1px;height:env(safe-area-inset-top,0px);'
      +'border-bottom:env(safe-area-inset-bottom,0px) solid transparent;';
    document.documentElement.appendChild(d); const cs=getComputedStyle(d);
    const o={top:parseFloat(cs.height)||0, bottom:parseFloat(cs.borderBottomWidth)||0};
    d.remove(); return JSON.stringify(o);})()`;
  const before = JSON.parse((await c.send("Runtime.evaluate", { expression: PROBE, returnByValue: true })).result.value);

  try {
    await c.send("Emulation.setSafeAreaInsetsOverride",
      { insets: { top: INSET_T, bottom: INSET_B, left: 0, right: 0 } });
    injected = "Emulation.setSafeAreaInsetsOverride";
  } catch (e) {
    console.log("  [說明] setSafeAreaInsetsOverride 不可用：" + e.message);
  }
  await sleep(200);
  const after = JSON.parse((await c.send("Runtime.evaluate", { expression: PROBE, returnByValue: true })).result.value);

  console.log("\n=== 尺的自證（" + SCHEME + "）===");
  line("注入手段", injected);
  line("注入前 env(top/bottom)", before.top + " / " + before.bottom, before.top === 0);
  line("注入後 env(top/bottom)", after.top + " / " + after.bottom, after.top === INSET_T && after.bottom === INSET_B);
  if (after.top !== INSET_T) {
    console.log("\n[未能執行] env() 注入不了 ⇒ 這支量到的都不算數，中止。");
    throw new Error("safe-area 注入失敗");
  }

  /* ---- 等開場收掉、資料畫完 ---- */
  await sleep(4000);

  const MEASURE = `(()=>{
    const g=s=>document.querySelector(s);
    const box=s=>{const e=g(s); if(!e) return null; const r=e.getBoundingClientRect();
      return {top:+r.top.toFixed(1), bottom:+r.bottom.toFixed(1), h:+r.height.toFixed(1), w:+r.width.toFixed(1)};};
    const cs=(s,p)=>{const e=g(s); return e?getComputedStyle(e)[p]:null};
    return JSON.stringify({
      vh: innerHeight, vw: innerWidth,
      safeT: getComputedStyle(document.documentElement).getPropertyValue('--safe-t'),
      topbar: box('.topbar'), topbarPadTop: cs('.topbar','paddingTop'),
      brand: box('.brand'), chip: box('.today-chip'),
      modebar: box('#modeBar'), summary: box('.summary'),
      fab: box('.fab button'), foot: box('.foot'),
      scrollH: document.documentElement.scrollHeight
    });})()`;
  const m = JSON.parse((await c.send("Runtime.evaluate", { expression: MEASURE, returnByValue: true })).result.value);

  console.log("\n=== 主畫面：貼頂／貼底元素（視窗 " + m.vw + "×" + m.vh + "）===");
  line("--safe-t 解析", m.safeT.trim());
  line(".topbar padding-top", m.topbarPadTop, parseFloat(m.topbarPadTop) >= INSET_T);
  line(".topbar 頂端 y", m.topbar.top);
  line("品牌列 .brand 頂端 y", m.brand.top, m.brand.top >= INSET_T);
  line("日期 .today-chip 頂端 y", m.chip.top, m.chip.top >= INSET_T);
  line("#modeBar 頂端 y", m.modebar.top, m.modebar.top >= INSET_T);
  line(".summary 頂端 y", m.summary.top, m.summary.top >= INSET_T);
  line("FAB 底端 y（距畫面底）", m.fab.bottom + "（剩 " + (m.vh - m.fab.bottom).toFixed(1) + "）",
    (m.vh - m.fab.bottom) >= INSET_B);

  /* ---- 捲到底：最後一塊內容不可以被 home 指示條蓋住 ---- */
  await c.send("Runtime.evaluate", { expression: "scrollTo(0, document.documentElement.scrollHeight)" });
  await sleep(400);
  const bot = JSON.parse((await c.send("Runtime.evaluate", {
    expression: `(()=>{const r=document.querySelector('.foot').getBoundingClientRect();
      return JSON.stringify({bottom:+r.bottom.toFixed(1), vh:innerHeight, y:scrollY});})()`,
    returnByValue: true
  })).result.value);
  console.log("\n=== 捲到底 ===");
  line("頁尾 .foot 底端距畫面底", (bot.vh - bot.bottom).toFixed(1) + "px",
    (bot.vh - bot.bottom) >= INSET_B);
  await c.send("Runtime.evaluate", { expression: "scrollTo(0,0)" });
  await sleep(200);

  /* ---- 兩個 sheet：升起後頂端不可以進到 safe-area 裡 ---- */
  for (const [name, opener, sel] of [["#sheet（記錄交易）", "document.getElementById('openBtn').click()", "#sheet"],
                                     ["#settingsSheet（手續費）", "document.getElementById('settingsBtn').click()", "#settingsSheet"]]) {
    await c.send("Runtime.evaluate", { expression: opener });
    await sleep(700);
    const s = JSON.parse((await c.send("Runtime.evaluate", {
      expression: `(()=>{const e=document.querySelector('${sel}'); const r=e.getBoundingClientRect();
        const h=e.querySelector('.handle'); const hr=h?h.getBoundingClientRect():null;
        const cssMax=getComputedStyle(e).maxHeight;
        return JSON.stringify({top:+r.top.toFixed(1), bottom:+r.bottom.toFixed(1), h:+r.height.toFixed(1),
          maxH:cssMax, handleTop:hr?+hr.top.toFixed(1):null, vh:innerHeight, sh:e.scrollHeight});})()`,
      returnByValue: true
    })).result.value);
    console.log("\n=== " + name + " ===");
    line("max-height 解析後", s.maxH);
    line("面板頂端 y", s.top + "（內容自然高 " + s.sh + "）", s.top >= INSET_T);
    line("把手 .handle 頂端 y", s.handleTop, s.handleTop === null || s.handleTop >= INSET_T);
    line("面板底端 y（距畫面底）", s.bottom + "（剩 " + (s.vh - s.bottom).toFixed(1) + "）");
    /* 把面板內容灌高，逼它撞到 max-height */
    await c.send("Runtime.evaluate", {
      expression: `(()=>{const e=document.querySelector('${sel}');
        const p=document.createElement('div'); p.id='__tmp__pad'; p.style.height='2000px'; e.appendChild(p);})()`
    });
    await sleep(200);
    const s2 = JSON.parse((await c.send("Runtime.evaluate", {
      expression: `(()=>{const r=document.querySelector('${sel}').getBoundingClientRect();
        return JSON.stringify({top:+r.top.toFixed(1), h:+r.height.toFixed(1), vh:innerHeight});})()`,
      returnByValue: true
    })).result.value);
    line("★ 內容灌到 2000px 後頂端 y", s2.top + "（高 " + s2.h + "）", s2.top >= INSET_T);
    await c.send("Runtime.evaluate", {
      expression: `(()=>{const p=document.getElementById('__tmp__pad'); if(p)p.remove();
        const s=document.getElementById('scrim'); if(s)s.click();})()`
    });
    await sleep(600);
  }

  console.log("\n" + (bad ? "[未過] " + bad + " 項不合格" : "[通過] 全部貼邊元素都讓開了"));
  c.close();
} finally {
  try { ch.kill(); } catch (e) {}
  try { srv.kill(); } catch (e) {}
}
process.exit(bad ? 1 : 0);
