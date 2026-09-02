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

console.log("\n=== ⑧ 每一筆真實交易都要能寫心得（跟練習一樣）===");
// Benson 2026-09-02：「我還想要可以輸入每一筆交易的心得，就跟練習的一模一樣。」
await ctl("/mode/flat");
await goTab("real");
await sleep(1000);
const nt = await evalJS(`(()=>{
  const rows=[...document.querySelectorAll('.n-zone.z-real .n-row')];
  const eds=[...document.querySelectorAll('.n-zone.z-real [data-nedit]')];
  return {rows:rows.length, edits:eds.length,
          kinds:[...new Set(eds.map(e=>e.getAttribute('data-nkind')))],
          keys:eds.slice(0,2).map(e=>e.getAttribute('data-nedit')),
          written:eds.filter(e=>(e.getAttribute('data-note')||'').length>0).length};})()`);
chk("  每一筆底下都有心得入口", nt.edits, nt.rows);
chk("  標成 real（存進 real_trades，不走練習那條同步鏈）", nt.kinds, ["real"]);
chk("  分區字母是大寫 R（小寫 r 是回顧，撞了會兩個輸入框打架）",
  nt.keys.every(k => String(k).startsWith("R|")), true);
chk("  已經寫過的心得會顯示出來", nt.written >= 1, true);
// 點開來要真的出現輸入框，而且 0.5 秒的重繪不可以把它洗掉（中文輸入法會掉字）
// ⚠️ 要挑「已經寫過心得」的那一列 —— 清單是新的在上面，有心得的那筆在最下面，
//    抓第一個 [data-nedit] 會拿到還沒寫過的，然後誤判成「舊心得沒帶進來」。
await evalJS(`(()=>{const e=[...document.querySelectorAll('.n-zone.z-real [data-nedit]')]
  .find(x=>(x.getAttribute('data-note')||'').length>0);
  if(e) e.click();})()`);
await sleep(1600);
const ed = await evalJS(`(()=>{const t=document.getElementById('tnote');
  return {open:!!t, val:t?t.value:null};})()`);
chk("  點一下會展開輸入框", ed.open, true);
chk("  舊的心得帶進輸入框裡（不是空的）", (ed.val || "").length > 0, true);
await evalJS(`(()=>{const t=document.getElementById('tnote');
  if(t){ t.value='探針打的字'; t.dispatchEvent(new Event('input',{bubbles:true})); }})()`);
await sleep(1800);                       // 撐過 3 次 tick
const kept = await evalJS(`(()=>{const t=document.getElementById('tnote');
  return t?t.value:null;})()`);
chk("  ⛔ 打到一半不可以被重繪洗掉", kept, "探針打的字");
await evalJS(`(()=>{const b=document.querySelector('[data-ncancel]'); if(b) b.click();})()`);
await sleep(900);

console.log("\n=== ⑨ 真實交易要標在 K 線圖上（本來只有練習有）===");
const mk = await evalJS(`(()=>{const sv=document.getElementById('csvg');
  if(!sv) return {svg:false};
  const t=[...sv.querySelectorAll('text')].map(e=>e.textContent);
  const gold=[...sv.querySelectorAll('circle')]
    .filter(c=>(c.getAttribute('stroke')||'').toLowerCase()==='#e3a951').length;
  return {svg:true, real:t.filter(x=>/^真 /.test(x)).length, halo:gold};})()`);
chk("  圖畫得出來", mk.svg, true);
chk("  有標出真實交易（膠囊上寫「真」）", mk.real > 0, true);
chk("  真實那幾筆有金色光環，跟練習分得出來", mk.halo > 0, true);
// ⛔ 價格軸的合理性 —— 沒有這道，「軸掉到 0、K 棒被壓成一條線」不會被抓到。
//    2026-09-02 真的發生：真實單的出場價可能是 null，Math.min(lo, entry, null) 當成 0。
// ⚠️ 不要用「掃 svg 裡所有像價格的數字」當尺 —— 那會把**成交量軸**的數字也刮進來
//    （量是 1250 之類的四位數），第一版就這樣誤判成「軸被拉爆」。
//    直接驗程式算出來的自動範圍 window.AXIS，那才是被 bug 弄壞的東西。
const ax9 = await evalJS(`(()=>{const bars=(window.barsCache&&window.barsCache.bars)||[];
  return {axis:window.AXIS,
          hi:Math.max(...bars.map(b=>b.h)), lo:Math.min(...bars.map(b=>b.l))};})()`);
chk("  價格軸下緣沒有掉到 K 棒範圍外（null 被當成 0 的話會掉到 0）",
  ax9.axis.lo > ax9.lo - 3000, true);
chk("  價格軸上緣也合理", ax9.axis.hi < ax9.hi + 3000, true);
console.log(`    實測 軸 ${ax9.axis.lo}~${ax9.axis.hi}　K 棒 ${ax9.lo}~${ax9.hi}`);

console.log("\n=== ⑩ 資料還沒到齊 → 顯示載入中，不要給他看半成品 ===");
// Benson：「如果 k 圖還沒有畫好的話，可以顯示 loading 動畫，不要直接讓我看到錯的 k 圖。」
await ctl("/mode/loading");
await evalJS(`(()=>{ if(window.fetchBars) fetchBars(true); })()`);
await sleep(2200);
const ld = await evalJS(`(()=>{const card=document.getElementById('cchart');
  return {partial:!!(window.barsCache&&window.barsCache.partial),
          dimmed:!!(card&&card.classList.contains('kk-load')),
          caption:(document.querySelector('.chead')||{innerText:''}).innerText};})()`);
chk("  後端有回報還沒到齊", ld.partial, true);
chk("  圖要進入載入狀態（淡下去＋進度條）", ld.dimmed, true);
chk("  而且要寫出來是在載入什麼", /夜盤資料載入中/.test(ld.caption), true);
await ctl("/mode/flat");
await evalJS(`(()=>{ if(window.fetchBars) fetchBars(true); })()`);
await sleep(1800);
const done2 = await evalJS(`(()=>{const card=document.getElementById('cchart');
  return !!(card&&card.classList.contains('kk-load'));})()`);
chk("  資料到齊之後要退出載入狀態（不可以一直轉）", done2, false);

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
