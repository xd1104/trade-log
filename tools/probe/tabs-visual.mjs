/*
  右欄「練習／真實」兩個分頁的版面探針。

  hold-to-fire.mjs 守的是「長按送單那顆按鈕的行為」；這一支守的是**版面與狀態**：
    - 五種狀態 × 兩個分頁全部渲染過（休市另外在第 ⑤ 節單獨測），console 一個錯都不准有
    - 兩顆頁籤高度分毫不差（lab-ux 踩過：裸寫 .real 被既有樣式套中，差了 14px）
    - 練習那一區的紀錄清單超過高度時是捲動、不是把每一列壓扁（面板開發鐵律）
    - 真實部位的三條線（進場／停利／停損）真的畫到 K 線圖上，而且**站在練習分頁也看得到**
    - 右欄不准有橫向溢出

  ⚠️ 不連永豐、不碰 8770。只打治具（8771 / 控制埠 8772）。
      跑法：先起 tools/probe/fe_harness.py，再 node tools/probe/tabs-visual.mjs
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
const DEV = Number(A.dev || 9815);

const sleep = ms => new Promise(r => setTimeout(r, ms));
let FAIL = 0;
const chk = (name, got, want) => {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) FAIL++;
  console.log((ok ? "  OK   " : "  FAIL ") + name +
    (ok ? "" : `  (得到 ${JSON.stringify(got)}，期待 ${JSON.stringify(want)})`));
};
const ctl = async p => (await fetch(`http://127.0.0.1:${CTL}${p}`)).json();

const profile = fs.mkdtempSync(path.join(os.tmpdir(), "tabs-probe-"));
const ch = spawn(CHROME, ["--headless=new", "--remote-debugging-port=" + DEV,
  "--user-data-dir=" + profile, "--no-first-run", "--no-default-browser-check",
  "--hide-scrollbars", "--window-size=1500,1100", "about:blank"],
  { stdio: "ignore", shell: false });
for (let i = 0; i < 200; i++) {
  try { await fetch(`http://127.0.0.1:${DEV}/json/version`); break; } catch { await sleep(100); }
}
const c = await CDP.attach(DEV);
await c.send("Page.enable");
await c.send("Runtime.enable");
await c.send("Log.enable");

const ERRORS = [];
c.on("Runtime.exceptionThrown", p =>
  ERRORS.push("exception: " + (p.exceptionDetails?.exception?.description ||
    p.exceptionDetails?.text || "?").slice(0, 160)));
c.on("Log.entryAdded", p => {
  if (p.entry.level === "error") ERRORS.push("console: " + String(p.entry.text).slice(0, 160));
});
// alert 會把整頁 JS 卡死 ⇒ 之後每個 evaluate 都不回。一定要接。
c.on("Page.javascriptDialogOpening", async () =>
  c.send("Page.handleJavaScriptDialog", { accept: true }));

const evalJS = async expr => (await c.send("Runtime.evaluate",
  { expression: expr, awaitPromise: true, returnByValue: true })).result.value;

await c.send("Page.navigate", { url: URL_ });
await sleep(2800);

async function goTab(t) {
  await evalJS(`(()=>{const b=document.querySelector('[data-rtab="${t}"]'); if(b) b.click();})()`);
  await sleep(800);
}
// 打開真實下單開關（住在真實那一區裡）
await goTab("real");
await evalJS(`(()=>{const s=document.querySelector('[data-rt]'); if(s) s.click();})()`);
await sleep(900);

console.log(`\n治具 ${URL_}（假狀態，沒有連永豐）\n`);

console.log("=== ① 六種狀態 × 兩個分頁：全部渲染過，console 零錯誤 ===");
const STATES = ["flat", "holding", "with_target", "short", "stale"];
let painted = 0;
for (const st of STATES) {
  await ctl("/mode/" + st);
  for (const tb of ["sim", "real"]) {
    await goTab(tb);
    await sleep(700);
    const seen = await evalJS(
      `!!document.querySelector('.n-zone.z-${tb}') && !!document.querySelector('.n-tabs')`);
    if (!seen) { FAIL++; console.log(`  FAIL  ${st} × ${tb} 沒有畫出那一區`); }
    else painted++;
  }
}
chk("  10 種組合都渲染出來了", painted, 10);
chk("  console 沒有任何錯誤（第一輪渲染）", ERRORS, []);

console.log("\n=== ② 兩顆頁籤高度必須分毫不差（裸寫 .real 會差 14px）===");
const tabs = await evalJS(`(()=>{const b=[...document.querySelectorAll('.n-tab')];
  return b.map(e=>{const r=e.getBoundingClientRect();
    return {h:Math.round(r.height*10)/10, y:Math.round(r.top*10)/10};});})()`);
chk("  兩顆頁籤都在", tabs.length, 2);
chk("  高度一樣", tabs[0].h === tabs[1].h, true);
chk("  上緣在同一條水平線上", tabs[0].y === tabs[1].y, true);
console.log(`    實測 ${tabs[0].h} / ${tabs[1].h}px（y=${tabs[0].y} / ${tabs[1].y}）`);

console.log("\n=== ③ 練習紀錄：超過容器高度時要捲，不可以把每一列壓扁 ===");
await ctl("/mode/flat");
await goTab("sim");
await sleep(900);
const sim = await evalJS(`(()=>{
  const rl=document.querySelector('.n-zone.z-sim .n-trl');
  const its=[...document.querySelectorAll('.n-zone.z-sim .n-item')];
  const hs=its.map(e=>e.clientHeight);
  return {n:its.length, scrolls: rl? rl.scrollHeight>rl.clientHeight+2 : null,
          minH:Math.min(...hs), maxH:Math.max(...hs),
          notes: document.querySelectorAll('.n-zone.z-sim .n-item [data-nedit]').length,
          stats: !!document.querySelector('.n-zone.z-sim .n-sh')};})()`);
chk("  九筆練習紀錄都列出來", sim.n, 9);
chk("  超過高度時是捲動", sim.scrolls, true);
chk("  每一列一樣高、沒有被壓扁", sim.minH === sim.maxH && sim.minH >= 28, true);
console.log(`    實測列高 ${sim.minH}~${sim.maxH}px`);
// 心得（note）跟手機 App 是同一個欄位，改版不可以把它弄丟
chk("  每一筆都還點得開心得輸入框", sim.notes, 9);
chk("  練習成績也在同一區裡", sim.stats, true);

console.log("\n=== ④ 真實部位要畫到 K 線圖上（以前只有練習部位畫得到）===");
await ctl("/mode/with_target");
await sleep(1600);
const chart = async () => evalJS(`(()=>{const sv=document.getElementById('csvg');
  if(!sv) return {svg:false};
  const t=[...sv.querySelectorAll('text')].map(e=>e.textContent);
  return {svg:true, entry:t.some(x=>/真實進場/.test(x)),
          tp:t.some(x=>/真實停利/.test(x)), sl:t.some(x=>/真實停損/.test(x))};})()`);
await goTab("real");
await sleep(900);
const cR = await chart();
chk("  K 線圖真的畫出來了（治具有給 K 棒）", cR.svg, true);
chk("  進場價那條線在", cR.entry, true);
chk("  停利那條線在", cR.tp, true);
chk("  停損那條線在", cR.sl, true);
await goTab("sim");
await sleep(900);
const cS = await chart();
chk("  站在練習分頁也看得到那三條線（圖是共用的）",
  [cS.entry, cS.tp, cS.sl], [true, true, true]);

console.log("\n=== ⑤ 休市（沒有即時報價）：兩區的下單鈕都要真的停用 ===");
// 拿舊價／收盤價記進成績＝假成績。這是紀錄正確性，不是 UX 取捨，不可以放寬。
await ctl("/mode/closed");
await sleep(1600);
await goTab("sim");
const cs = await evalJS(`(()=>{
  const b=[...document.querySelectorAll('[data-act="long"],[data-act="short"]')];
  return {n:b.length, allDis:b.length>0&&b.every(e=>e.disabled),
          why:!!document.querySelector('.n-zone.z-sim .n-why'),
          px:(document.querySelector('.n-zone.z-sim .n-px')||{innerText:''}).innerText};})()`);
chk("  練習：兩顆鈕都在，而且都真的 disabled", [cs.n, cs.allDis], [2, true]);
chk("  練習：底下寫了為什麼不能按", cs.why, true);
chk("  練習：現價那一格明寫「非即時」", /非即時/.test(cs.px), true);
await goTab("real");
await sleep(900);
const cr = await evalJS(`(()=>{const b=[...document.querySelectorAll('[data-rdir]')];
  return {n:b.length, allDis:b.length>0&&b.every(e=>e.disabled),
          why:!!document.querySelector('.n-zone.z-real .n-why'),
          px:(document.querySelector('.n-zone.z-real .n-px')||{innerText:''}).innerText};})()`);
chk("  真實：兩顆鈕都在，而且都真的 disabled", [cr.n, cr.allDis], [2, true]);
chk("  真實：寫出了擋單原因", cr.why, true);
chk("  真實：現價那一格明寫「非即時」", /非即時/.test(cr.px), true);
await ctl("/mode/flat");
await sleep(1200);

console.log("\n=== ⑥ 右欄不准有橫向溢出 ===");
// ⚠️ 不可以用 scrollWidth>clientWidth 當尺：頁籤上的浮動點數 .badge 是
//    position:absolute; right:-6px，**刻意**凸出按鈕外面（那是設計，不是溢出）。
//    真正要問的是「有沒有東西跑出畫面」，所以量的是每個元素的左右邊界。
const of = await evalJS(`(()=>{const r=document.querySelector('.right');
  const bad=[...r.querySelectorAll('*')].map(e=>{const b=e.getBoundingClientRect();
      return {n:(e.className&&e.className.baseVal!==undefined?e.className.baseVal:e.className)
                 ||e.tagName, l:Math.round(b.left), rt:Math.round(b.right)};})
    .filter(x=>x.rt>window.innerWidth+1||x.l<-1);
  return {docOverflow: document.documentElement.scrollWidth>window.innerWidth,
          bad: bad.slice(0,5), vw:window.innerWidth};})()`);
chk("  整頁沒有橫向捲軸", of.docOverflow, false);
chk("  右欄裡沒有東西跑出畫面（含刻意凸出的浮動點數）", of.bad, []);

console.log("\n=== ⑦ 真實成績不受「真實下單」開關管 ===");
// Benson 2026-09-02：「不用打開真實下單也可以看得到，然後也會顯示勝率」。
// 開關管的是「會不會送單」，看自己過去的成績跟送不送單無關；
// 而且開關刻意不記憶（重啟後一定是關的），綁在一起等於每天早上都看不到昨天的成績。
await ctl("/mode/flat");
await goTab("real");
await sleep(900);
const swOn = await evalJS(`!!window.REAL_ON`);
if (swOn) {                       // 前面幾節把它打開了，這裡要關掉才測得到
  await evalJS(`(()=>{const s=document.querySelector('[data-rt]'); if(s) s.click();})()`);
  await sleep(1100);
}
const off = await evalJS(`(()=>{const z=document.querySelector('.n-zone.z-real');
  const rate=document.querySelector('.n-zone.z-real .score .rate .n');
  return {realOn:!!window.REAL_ON,
          fire:document.querySelectorAll('[data-rdir]').length,
          hasScore:!!document.querySelector('.n-zone.z-real .score'),
          hasBar:!!document.querySelector('.n-zone.z-real .wlbar'),
          rate:rate?rate.textContent:null,
          rows:document.querySelectorAll('.n-zone.z-real .n-row').length,
          txt:z?z.innerText:''};})()`);
chk("  開關確實是關著的", off.realOn, false);
chk("  ⛔ 但一顆真實下單鈕都不准出現（保險蓋還是要有效）", off.fire, 0);
chk("  勝率看得到", off.hasScore, true);
chk("  勝率是個數字", /^\d+$/.test(String(off.rate || "")), true);
chk("  勝敗條也在（跟練習成績同一套版面）", off.hasBar, true);
chk("  今天的交易清單看得到", off.rows > 0, true);
chk("  算不出點數的那幾筆有講出來，不是默默不算",
  /沒有算進勝率/.test(off.txt), true);

// ⛔ 這一項以前只在第 ① 節做過，後面幾節（休市、K 線圖、練習紀錄）產生的 console error
//    **只會被印出來，不算失敗、不影響 exit code** ⇒ 掛排程或只看「總結」的人會被安靜放行
//    （lab-qa 2026-09-01 退件第 3 條）。錯誤要在**全部跑完之後**再驗一次。
chk("  console 全程沒有任何錯誤（跑完所有測項）", ERRORS, []);
if (ERRORS.length) console.log("\nconsole 錯誤：\n  " + ERRORS.join("\n  "));
c.close();
ch.kill();
try { fs.rmSync(profile, { recursive: true, force: true }); } catch { /* Chrome 還握著暫存檔 */ }
console.log("\n總結:", FAIL ? `${FAIL} 項失敗` : "全部通過");
process.exit(FAIL ? 1 : 0);
