/*
  【自動下單】分頁（**會真的送出委託單**的那一頁）的前端探針。

  ⚠️ 不連永豐、⛔ **不碰 8770**（Benson 的面板正開著）。只打治具（8775／控制埠 8776）。
     跑法：先起 tools/probe/fire_harness.py，改過程式一定要**重起治具**，再
           node tools/probe/fire-tab.mjs

  ⛔ 價格一律 12000 附近，一個真實成交價／進出場時間／點數都不准出現。

  這一頁要守的三條紅線（其餘的行為在 tools/shioaji/test_auto_fire.py）：
    ① **現在是開還是關**、**選了哪個做法**、**今天送了沒／為什麼沒送** —— 三件都要在畫面上。
    ② ⭐ **開難、關易**（Benson 2026-09-09 拍板）：
       ・⛔ 這一頁**打不開**開關 —— 沒有任何「開啟」的按鈕、表單、輸入框；
         **開關關著的時候整頁 0 顆按鈕**（沒東西可關就不該有按鈕）。
       ・開著的時候**恰好 1 顆**「關閉自動下單」，而且它是這一頁唯一會改變狀態的東西。
       ・⛔ 整場**唯一**允許的 POST 就是那一顆按下去打的 `/api/fire/off`，
         而且它**只會關不會開**（按完檔案真的不見了、狀態真的變成關閉中）。
         ⚠️ 這條斷言是從舊版的「整場零個 POST」**改對**的，⛔ 不是放寬成不驗。
    ③ ⛔ 畫面上不准出現 A／B 這種代號（做法一律寫「5 分 K」「開盤起」）；
       「開著」與「關著」、「沒有紀錄」與「有紀錄但沒送」都不准寫同一句。

  ⚠️⚠️ **產品的路由不在這支的守備範圍**（2026-09-09 lab-qa 退件 M1）：治具
     `fire_harness.py` 自己重寫了一份 handler ⇒ 這支從頭到尾**沒有打到產品的路由**。
     路由由 `tools/shioaji/test_fire_routes.py` 守（真的起 `live_panel.Handler` 打進去）。
     這支守的是**畫面與行為**。

  【每一條都要有負控組】沒有負控組的綠燈在這個專案不算數。
  負控組用**原始碼突變**（把產品函式 toString() 出來、改掉那道守衛、eval 回去）——
  ⛔ 不寫壞版本到磁碟（看門狗是活的）。
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
const URL_ = A.url || "http://127.0.0.1:8775/";
const CTL = Number(A.ctl || 8776);
const DEV = Number(A.dev || 9823);

const sleep = ms => new Promise(r => setTimeout(r, ms));
let FAIL = 0, N = 0, DONE = false;
const say = (ok, name, extra) => {
  N++; if (!ok) FAIL++;
  console.log((ok ? "  OK   " : "  FAIL ") + name + (extra ? "  " + extra : ""));
};
function bail(e) {
  if (DONE) return;
  DONE = true;
  say(false, "⛔ 探針自己掛掉了（未捕捉的例外）—— 這一項就是那個 FAIL",
    String((e && e.stack) || e).slice(0, 300));
  console.log(`\n${"=".repeat(60)}\n共 ${N} 項，${FAIL} 項未過（⚠️ 探針中途中斷，後面沒跑到）`);
  try { c.close(); } catch { /* 還沒接上 */ }
  try { ch.kill(); } catch { /* 同上 */ }
  process.exit(1);
}
process.on("uncaughtException", bail);
process.on("unhandledRejection", bail);
const chk = (name, got, want) => say(JSON.stringify(got) === JSON.stringify(want), name,
  JSON.stringify(got) === JSON.stringify(want) ? "" : `(得到 ${JSON.stringify(got)}，期待 ${JSON.stringify(want)})`);
const ctl = async p => (await fetch(`http://127.0.0.1:${CTL}${p}`)).json();

const profile = fs.mkdtempSync(path.join(os.tmpdir(), "fire-probe-"));
const ch = spawn(CHROME, ["--headless=new", "--remote-debugging-port=" + DEV,
  "--user-data-dir=" + profile, "--no-first-run", "--no-default-browser-check",
  "--hide-scrollbars", "--window-size=1500,1100", "about:blank"],
  { stdio: "ignore", shell: false });
for (let i = 0; i < 200; i++) {
  try { await fetch(`http://127.0.0.1:${DEV}/json/version`); break; } catch { await sleep(100); }
}
const c = await CDP.attach(DEV);
await c.send("Page.enable"); await c.send("Runtime.enable"); await c.send("Log.enable");
/* ⛔⛔ 最硬的那一道：把這一頁**真的發出去的每一個請求**記下來（不是掃原始碼、
   也不是數按鈕）。整場跑完斷言：① 一個 POST 都沒有；② 沒有打到 /api/enter、/api/real/*。 */
await c.send("Network.enable");
const REQS = [];
c.on("Network.requestWillBeSent", p => REQS.push({ m: p.request.method, u: p.request.url }));
const ERRORS = [];
c.on("Runtime.exceptionThrown", p =>
  ERRORS.push("exception: " + (p.exceptionDetails?.exception?.description ||
    p.exceptionDetails?.text || "?").slice(0, 200)));
c.on("Log.entryAdded", p => {
  if (p.entry.level === "error") ERRORS.push("console: " + String(p.entry.text).slice(0, 200));
});
c.on("Page.javascriptDialogOpening", async () =>
  await c.send("Page.handleJavaScriptDialog", { accept: true }));

const ev = async (e) => {
  const r = await c.send("Runtime.evaluate",
    { expression: e, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error("evaluate: " +
    (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
  return r.result.value;
};
async function mutate(fn, from, to) {
  const r = await ev(`(()=>{
    const f=window[${JSON.stringify(fn)}];
    if(typeof f!=='function') return {ok:false,why:'找不到 '+${JSON.stringify(fn)}};
    const src=f.toString();
    if(src.indexOf(${JSON.stringify(from)})<0) return {ok:false,why:'目標字串已失效（尺壞了）'};
    const out=src.split(${JSON.stringify(from)}).join(${JSON.stringify(to)});
    if(out===src) return {ok:false,why:'突變沒有真的改到東西'};
    window.__orig=window.__orig||{};
    if(!window.__orig[${JSON.stringify(fn)}]) window.__orig[${JSON.stringify(fn)}]=f;
    window[${JSON.stringify(fn)}]=eval('('+out+')');
    return {ok:true};
  })()`);
  say(!!r.ok, `  突變已套用：${fn}`, r.ok ? "" : "(" + r.why + ")");
  return !!r.ok;
}
const unmutate = fn => ev(`(()=>{ if(window.__orig&&window.__orig[${JSON.stringify(fn)}])
  window[${JSON.stringify(fn)}]=window.__orig[${JSON.stringify(fn)}]; return 1; })()`);

/* ⚠️ 突變之後要戳破 setEl 的 e.__html 快取，不然重畫沖不掉舊節點
   （【模擬】那一頁第一版就是這樣紅了三條）。 */
const repaint = () => ev(`(()=>{
  for(const id of ['alstate','alsub','algates','alhow','aloff','altoday','altbl','alempty',
                   'alnotes','alcount','allogn']){
    const e=document.getElementById(id); if(e) e.__html=null; }
  alPaint(); return 1;})()`);

async function goFire() {
  await ev("setTab('fire')");
  for (let i = 0; i < 80; i++) {
    const st = await ev("[AL.pending,!!AL.data]");
    if (!st[0] && st[1]) { await sleep(120); return true; }
    await sleep(150);
  }
  return false;
}
async function refetch() {
  await ev("alFetch()");
  for (let i = 0; i < 80; i++) {
    if (!(await ev("AL.pending"))) { await sleep(120); return true; }
    await sleep(100);
  }
  return false;
}
const txt = async () => await ev(`document.getElementById('tab-fire').innerText`);

const W = await ctl("/f/where");
console.log(`治具資料：${W.dir}`);
console.log(`今天=${W.today}\n`);
say(W.prod_flag_exists === false,
  "⛔⛔ 出貨狀態：真的 tools/shioaji/AUTO_ORDERS_ON **不存在**");
await ctl("/f/reset");
await c.send("Page.navigate", { url: URL_ });
await sleep(2200);
say(await goFire(), "切進【自動下單】並載入完成");

/* ═══ ① 分頁 ═══════════════════════════════════════════════════════ */
console.log("=== ① 分頁 ===");
chk("TAB", await ev("TAB"), "fire");
chk("⛔ 分頁名是「自動下單」（沒有括號）",
  await ev(`document.querySelector('[data-tab="fire"]').textContent`), "自動下單");
chk("隔壁那顆是「自動下單（模擬）」",
  await ev(`document.querySelector('[data-tab="auto"]').textContent`), "自動下單（模擬）");
chk("其他分頁都藏起來了",
  await ev(`['tab-live','tab-tick','tab-review','tab-auto']
    .map(i=>document.getElementById(i).hidden)`), [true, true, true, true]);
chk("這一頁真的顯示出來了", await ev(`document.getElementById('tab-fire').hidden`), false);

/* ═══ ② ⛔ 這一頁打不開開關；關著的時候連按鈕都沒有 ═══════════════ */
console.log("\n=== ② ⛔ 開難關易：關著的時候 0 顆按鈕 ===");
for (const [nm, sel] of [["button", "button"], ["form", "form"], ["input", "input"],
["[data-act]", "[data-act]"], ["[data-rdir]", "[data-rdir]"],
["[type=submit]", "[type=submit]"], ["<a href>", "a[href]"]]) {
  chk(`  ${nm} 0 顆`, await ev(`document.querySelectorAll('#tab-fire ${sel}').length`), 0);
}
say((await ev(`[...document.querySelectorAll('#tab-fire *')]
  .filter(e=>e.onclick||e.getAttribute('onclick')).length`)) === 0,
  "  也沒有任何 onclick");
// 尺的自證：同一把尺在「即時」那一頁的真實下單區抓得到按鈕
say((await ev(`document.querySelectorAll('#tab-live button').length`)) > 0,
  "  負控組：同一把尺在【即時】抓得到按鈕",
  String(await ev(`document.querySelectorAll('#tab-live button').length`)));

/* ═══ ③ 關著：畫面要一眼看出來 ═══════════════════════════════════ */
console.log("\n=== ③ 關著（出貨狀態）===");
let t = await txt();
say(t.includes("關閉中"), "  寫著「關閉中」");
say(t.includes("AUTO_ORDERS_ON"), "  寫得出要建哪個檔");
say(t.includes("要用請自己建"), "  而且講得出怎麼開");
chk("  開關徽章不是「開啟中」",
  await ev(`document.querySelector('#tab-fire .al-badge').textContent`), "關閉中");
say((await ev(`getComputedStyle(document.querySelector('#tab-fire .al-badge')).color`))
  !== "rgb(238, 90, 84)",
  "  ⛔ 關著不是紅色（關著是正常狀態，紅綠只給損益）",
  await ev(`getComputedStyle(document.querySelector('#tab-fire .al-badge')).color`));
say(t.includes("永豐沒有停損單"),
  "  ⛔ 有把「面板關掉就沒有停損」這件事寫在畫面上");

/* ═══ ④ 開著：選了哪個做法要看得到，⛔ 不准寫代號 ══════════════════ */
console.log("\n=== ④ 開著：看得到選了哪個做法 ===");
await ctl("/f/arm/A"); await refetch();
t = await txt();
say(t.includes("開啟中"), "  寫著「開啟中」");
say(t.includes("5 分 K"), "  做法寫的是名字「5 分 K」");
say(t.includes("09:00 起算"), "  底下那行小字也在");
say(t.includes("09:03:30"), "  寫得出幾點送");
await ctl("/f/arm/B"); await refetch();
t = await txt();
say(t.includes("開盤起") && t.includes("08:45 起算"), "  換成 B 之後名字跟著換");
say(!(await ev(`document.getElementById('alstate').innerText`)).includes("5 分 K"),
  "  ⛔ 狀態那一行不會同時出現另一個做法的名字");
/* ⛔⛔ 孤立的 A／B 代號零命中（【模擬】那一頁為這件事被 Benson 退件過一次：
   「我要從哪裡知道現在我看的是哪個做法？」）。
   ⚠️ **這一頁有一個合法的例外，而且只有一個**：〈怎麼開〉那段（`#alhow`）——
      他要**自己把那個字母打進檔案裡**，那個字母就是操作本身，不是「做法的名字」。
      所以規則收緊成兩條，⛔ 不是放寬：
        ① `#alhow` **以外**的地方，孤立的 A／B 零命中；
        ② `#alhow` 裡面的每一個 A／B 都必須包在 <code> 裡（＝長得像檔案內容），
           ⛔ 不准以純文字出現（那就變回「A＝5 分 K」那種對照說明了）。 */
const lone = await ev(`(()=>{const sel=['#alstate','#altoday','.al-tbl td:nth-child(2)'];
  const out=[];
  for(const s of sel) for(const e of document.querySelectorAll('#tab-fire '+s))
    out.push(...((e.innerText||'').match(/(^|[^0-9A-Za-z])[AB]([^0-9A-Za-z]|$)/g)||[]));
  return out;})()`);
chk("  ⛔ 講「哪個做法」的三個地方（狀態／今天／做法欄）孤立字母零命中", lone, []);
/* ⚠️ 為什麼「原因欄」不在上面那把尺裡（⛔ 這是界定範圍，不是放寬）：
   原因欄印的是後端那句話（例：「讀到『C』，只認得 A 或 B」）——
   它講的是**你該往那個檔案裡寫什麼**，不是「這個做法叫什麼名字」。
   Benson 退件的那件事（「我要從哪裡知道現在我看的是哪個做法？」）發生在**做法的名字**上，
   而那三個地方已經被上面那條守死了。 */
say((await ev(`[...document.querySelectorAll('#tab-fire .al-tbl td:nth-child(2)')]
  .map(e=>e.innerText)`)).every(s => ["—", "5 分 K", "開盤起"].includes(s.trim())),
  "  做法欄只會出現名字或「—」",
  JSON.stringify(await ev(`[...new Set([...document.querySelectorAll(
    '#tab-fire .al-tbl td:nth-child(2)')].map(e=>e.innerText.trim()))]`)));
const howLoose = await ev(`(()=>{const h=document.getElementById('alhow').cloneNode(true);
  h.querySelectorAll('code').forEach(e=>e.replaceWith(document.createTextNode('〔〕')));
  document.body.appendChild(h); h.style.position='absolute'; h.style.left='-9999px';
  const s=h.innerText; h.remove();
  return (s.match(/(^|[^0-9A-Za-z])[AB]([^0-9A-Za-z]|$)/g)||[]);})()`);
chk("  ⛔〈怎麼開〉裡的 A／B 全部包在 <code> 裡（＝檔案內容，不是做法的名字）",
  howLoose, []);
say((await ev(`[...document.querySelectorAll('#alhow code')].map(e=>e.textContent)`))
  .filter(x => x === "A" || x === "B").length === 2,
  "  而且 A 與 B 各出現一次（他要打進檔案的就是那一個字母）");
say((await ev(`document.getElementById('tab-fire').innerText`)).includes("AUTO_ORDERS_ON"),
  "  （檔名裡的字母不算 —— 那是檔名不是做法代號）");

/* ═══ ⑤ 內容看不懂 ⇒ 拒絕下單並講出讀到什麼 ═════════════════════ */
console.log("\n=== ⑤ 開關內容看不懂 ===");
await ctl("/f/arm/junk"); await refetch();
t = await txt();
say(t.includes("拒絕下單"), "  徽章寫「拒絕下單」（⛔ 不是「關閉中」——兩件事不同）");
say(t.includes("K線"), "  而且把讀到的內容原樣寫出來", "讀到 K線");
say(t.includes("只認得 A 或 B") || t.includes("只支援"), "  講得出只認得什麼");

/* ═══ ⑥ 真單開關：兩個都要開才會真的送出去 ═══════════════════════ */
console.log("\n=== ⑥ ⛔ 同時受 REAL_ORDERS_ON 管 ===");
await ctl("/f/arm/B"); await ctl("/f/live/off"); await refetch();
t = await txt();
say(t.includes("只會演練"), "  真單關著時明講「只會演練」");
say(t.includes("REAL_ORDERS_ON"), "  寫得出是哪個開關");
say(t.includes("把真單關掉就等於連自動也關掉"), "  ⛔ 而且講清楚兩個開關的關係");
await ctl("/f/live/on"); await refetch();
t = await txt();
say(t.includes("會真的送出去"), "  真單開著時明講「會真的送出去」");
say(!t.includes("只會演練"), "  ⛔ 兩句不會同時出現（一定有一句是假的）");

/* ═══ ⑦ 今天送了沒 ═══════════════════════════════════════════════ */
console.log("\n=== ⑦ 今天送了沒／為什麼沒送 ===");
await ctl("/f/rows/today"); await refetch();
t = await txt();
say(t.includes("已送出委託單"), "  今天送出去了 → 明講");
say(t.includes("12013"), "  寫得出進場價（實際成交價）");
say(t.includes("12113"), "  寫得出停利價");
say(t.includes("滑價"), "  寫得出滑價（跟模擬對照的關鍵）");
await ctl("/f/rows/none"); await ctl("/f/clock/10:30:00"); await refetch();
t = await txt();
say(t.includes("今天沒有紀錄"), "  今天完全沒紀錄 → 「今天沒有紀錄」");
say(t.includes("不補單"), "  ⛔ 而且明講不補單");
await ctl("/f/clock/09:01:00"); await refetch();
t = await txt();
say(t.includes("還沒到 09:03:30"), "  ⛔ 09:03:30 之前寫「還沒到」（⛔ 不是「沒有紀錄」）");
say(!t.includes("今天沒有紀錄"),
  "  ⛔ 兩種狀態不准寫同一句（還沒到 ≠ 沒錄到）");
/* 時態鎖：09:03:30 之前不准預告方向 */
say(!/做多|做空/.test(await ev(`document.getElementById('altoday').innerText`)),
  "  ⛔ 09:03:30 之前不預告方向");
await ctl("/f/clock/10:30:00");

/* ═══ ⑧ 紀錄：每一種「沒送」的原因都要在畫面上 ═══════════════════ */
console.log("\n=== ⑧ 紀錄（每一種沒送的原因都看得到）===");
await ctl("/f/rows/mixed"); await refetch();
t = await txt();
for (const [nm, s] of [["開關關著", "自動下單是關著的"],
["報價中斷", "報價太舊"],
["面板沒開著", "沒開著"],
["內容看不懂", "只認得 A 或 B"],
["券商擋下來", "券商那一關擋下來"],
["算不出訊號", "算不出方向"],
["送到一半當掉", "不知道那一張單的下場"]]) {
  say(t.includes(s), `  ${nm} → 原因寫在畫面上`);
}
say(t.includes("做空") && t.includes("11987"), "  做空那一天的方向與進場價都看得到");
say(t.includes("模擬那邊"), "  有「跟模擬對得起來」那一欄");
const nrows = await ev(`document.querySelectorAll('#tab-fire .al-tbl tbody tr').length`);
say(nrows >= 8, "  每一天都有一列", String(nrows));

/* ═══ ⑧b 收盤自動平倉：畫面上要講得出來 ═════════════════════════════ */
console.log("\n=== ⑧b 收盤自動平倉（13:43:30）===");
await ctl("/f/arm/B"); await ctl("/f/live/on"); await ctl("/f/rows/today"); await refetch();
t = await txt();
const eodAt = await ev("(AL.data&&AL.data.eod_at)||''");
say(!!eodAt, "  後端端得出收盤平倉的時刻", String(eodAt));
say(t.includes(eodAt), "  ⛔ 那個時刻寫在畫面上", String(eodAt));
say(t.includes("只平自動下單開的那一口") || t.includes("只平自動下單自己開的那一口"),
  "  ⛔⛔ 而且明講「只平自動下單開的那一口」（他自己的單不會被碰）");
say(t.includes("開關沒有有效期"),
  "  ⛔ 明講開關沒有有效期（每個交易日都會送，直到你自己關掉）");
say(/剛好持平|差 0 點/.test(t),
  "  訊號剛好是 0 的時候算做多 —— 這件事寫在畫面上（跟模擬那一頁一致）");
/* ⚠️ 真單關著時最容易被誤會的一件事：症狀（按不了進場）跟原因（演練部位）看起來無關 */
await ctl("/f/live/off"); await refetch();
t = await txt();
say(t.includes("演練部位"), "  真單關著時明講「會產生一個演練部位」");
say(t.includes("按不了進場"),
  "  ⛔ 而且明講「那口部位開著時你自己按不了進場」（不寫他會以為面板壞了）");
await ctl("/f/live/on"); await refetch();
t = await txt();
say(!t.includes("演練部位"), "  ⛔ 真單開著時那句話不會出現（兩種狀態不寫同一句）");

/* ⛔⛔ 收盤平倉的三種結局：⛔ 一句都不准混，而且「要他自己動手」那兩種要跳出來 */
await ctl("/f/rows/eodok"); await refetch();
t = await txt();
say(t.includes("已經把自動下單那一口平掉"), "  平掉了 → 明講");
say(/12031|18\.0/.test(t), "    寫得出出場價與點數");
chk("    ⛔ 平掉了不是警示（不要狼來了）",
  await ev(`document.querySelectorAll('#tab-fire .al-alarm').length`), 0);

await ctl("/f/rows/eodfail"); await refetch();
t = await txt();
say(t.includes("收盤平倉沒有成功"), "  ⛔ 平不掉 → 明講「沒有成功」");
say(t.includes("大戶投"), "  ⛔ 而且叫他自己到大戶投平倉");
chk("  ⛔ 平不掉要跳出金色警示（⛔ 不可以混在一般紀錄裡）",
  await ev(`document.querySelectorAll('#tab-fire .al-alarm').length`), 1);
say((await ev(`getComputedStyle(document.querySelector('#tab-fire .al-alarm')).color`))
  !== "rgb(238, 90, 84)",
  "    ⛔ 用金色不是紅色（紅綠只給損益）",
  await ev(`getComputedStyle(document.querySelector('#tab-fire .al-alarm')).color`));
say(!t.includes("已經把自動下單那一口平掉"),
  "  ⛔ 平不掉的時候絕對不會同時寫「平掉了」（一定有一句是假的）");
say((await ev(`(()=>{const e=document.querySelector('#tab-fire .al-alarm');
  const r=e.getBoundingClientRect(); return r.width>200&&r.height>20;})()`)),
  "    而且那條警示真的畫得出來（尺寸量過）",
  JSON.stringify(await ev(`(()=>{const r=document.querySelector(
    '#tab-fire .al-alarm').getBoundingClientRect();
    return [Math.round(r.width),Math.round(r.height)];})()`)));

await ctl("/f/rows/eodnotours"); await refetch();
t = await txt();
say(t.includes("不是自動下單開的"), "  ⛔⛔ 那口不是自動下單開的 → 明講「不碰它」");
say(t.includes("你自己決定") || t.includes("請你自己"),
  "    而且把決定權交回給他");
chk("    也要跳出警示（那口部位會抱過夜盤）",
  await ev(`document.querySelectorAll('#tab-fire .al-alarm').length`), 1);
say(!t.includes("收盤平倉沒有成功"),
  "  ⛔ 「不是我的」跟「平不掉」不寫同一句");

/* ⛔⛔ 「查不到那一口的下場」（2026-09-09 lab-qa 退件 M3 的 Y1／Y2）：
   舊版這三條保守路徑全寫成「先前已經平掉了」，而部位其實**還開著**、還不示警
   ⇒ 他抱過夜盤、13:45 起停損也停了，而畫面告訴他已經平掉。 */
await ctl("/f/rows/eodcanttell"); await refetch();
t = await txt();
say(!t.includes("先前已經平掉了"),
  "  ⛔⛔ 查不到的時候**絕對不會**寫「先前已經平掉了」（那是一句假話）");
say(t.includes("不知道") || t.includes("對不了帳"),
  "  ⛔ 而是明講「對不了帳、不知道平掉了沒」");
say(t.includes("大戶投"), "  ⛔ 而且叫他自己到大戶投確認部位");
chk("  ⛔ 而且要跳出金色警示（Y1／Y2 舊版靜悄悄）",
  await ev(`document.querySelectorAll('#tab-fire .al-alarm').length`), 1);
say(!t.includes("收盤平倉沒有成功"),
  "  ⛔ 「查不到」跟「平不掉」也不寫同一句");
say((await ev(`(()=>{const r=document.querySelector(
  '#tab-fire .al-alarm').getBoundingClientRect(); return r.width>200&&r.height>20;})()`)),
  "    那條警示真的畫得出來（尺寸量過）",
  JSON.stringify(await ev(`(()=>{const r=document.querySelector(
    '#tab-fire .al-alarm').getBoundingClientRect();
    return [Math.round(r.width),Math.round(r.height)];})()`)));
await ctl("/f/rows/today"); await refetch();

/* ═══ ⑧c ⭐ 開難、關易：那一顆「關閉」鈕 ═══════════════════════════ */
console.log("\n=== ⑧c ⭐ 開難關易（唯一一顆會改變狀態的按鈕）===");
await ctl("/f/arm/B"); await refetch();
chk("  開著的時候，這一頁恰好 1 顆 button",
  await ev(`document.querySelectorAll('#tab-fire button').length`), 1);
chk("    而且它就是「關閉」那一顆",
  await ev(`document.querySelectorAll('#tab-fire [data-aloff]').length`), 1);
chk("    ⛔ 仍然沒有任何 form／input／[data-act]（＝沒有「開啟」的路）",
  await ev(`document.querySelectorAll(
    '#tab-fire form,#tab-fire input,#tab-fire [data-act],#tab-fire [data-rdir]').length`), 0);
const btnTxt = await ev(`document.querySelector('#tab-fire [data-aloff]').textContent`);
say(/關閉/.test(btnTxt) && !/開啟|啟用|打開/.test(btnTxt),
  "    按鈕上寫的是「關閉」，⛔ 沒有任何「開啟」字樣", btnTxt);
say((await ev(`(()=>{const b=document.querySelector('#tab-fire [data-aloff]');
  const r=b.getBoundingClientRect();
  return r.width>40&&r.height>20&&getComputedStyle(b).display!=='none';})()`)),
  "    按得到（尺寸與可見度都量過）",
  JSON.stringify(await ev(`(()=>{const r=document.querySelector(
    '#tab-fire [data-aloff]').getBoundingClientRect();
    return [Math.round(r.width),Math.round(r.height)];})()`)));
/* 內容看不懂（檔案在、但開不成）⇒ ⛔ 那顆鈕**還是要在**，不然他關不掉那個壞檔 */
await ctl("/f/arm/junk"); await refetch();
chk("  ⚠️ 內容看不懂時那顆鈕還在（檔案還在，要關得掉）",
  await ev(`document.querySelectorAll('#tab-fire [data-aloff]').length`), 1);
/* 關著 ⇒ 沒東西可關 ⇒ 那顆鈕不見 */
await ctl("/f/arm/off"); await refetch();
chk("  ⛔ 關著的時候那顆鈕不見（沒東西可關）",
  await ev(`document.querySelectorAll('#tab-fire button').length`), 0);

/* ⭐ 真的按下去（真滑鼠不必，這是一顆一般的 click；重點是「按完真的關掉了」）*/
await ctl("/f/arm/A"); await refetch();
const W1 = await ctl("/f/where");
say(W1.real_flag_exists === true, "  前置：治具的開關檔真的存在（暫存區）");
const postsBefore = REQS.filter(r => r.m !== "GET").length;
await ev(`document.querySelector('#tab-fire [data-aloff]').click()`);
for (let i = 0; i < 60; i++) {           // 等 POST 回來 ＋ 重抓狀態
  if (!(await ev("AL.pending")) && !(await ev("AL.data&&AL.data.flag_exists"))) break;
  await sleep(150);
}
await sleep(200);
const W2 = await ctl("/f/where");
say(W2.real_flag_exists === false, "  ⛔ 按下去之後開關檔真的不在了");
chk("    是改名不是刪掉（他寫的內容留著）", (W2.off_files || []).length, 1);
t = await txt();
say(t.includes("關閉中"), "  ⇒ 畫面立刻變成「關閉中」");
chk("  ⇒ 那顆鈕自己不見了",
  await ev(`document.querySelectorAll('#tab-fire button').length`), 0);
/* ⛔ 白名單要比「路徑＋查詢字串」，⛔ 不可以只比 pathname（2026-09-09 lab-qa 提 B5）：
   `pathname` 會把 `?arm=A` 整段丟掉 ⇒ 一個帶查詢字串的請求（例如
   `POST /api/fire/off?arm=A`）在這條尺上跟乾淨的那一個**長得一模一樣**。
   後端那條路是精確比對（`self.path == "/api/fire/off"`，`test_fire_routes.py` ④ 在守），
   但這一層是「這一頁到底發出了什麼」的最後一道帳，帳要記全。 */
const nonGetKey = r => r.m + " " + new URL(r.u).pathname + new URL(r.u).search;
const newPosts = REQS.filter(r => r.m !== "GET").slice(postsBefore);
chk("  ⛔ 這一下**只**送出一個 POST，而且是 /api/fire/off（含查詢字串）",
  newPosts.map(nonGetKey), ["POST /api/fire/off"]);
say(await ev("AL.data&&AL.data.armed===false"),
  "  ⛔⛔ 而且它**只會關不會開**：按完之後 armed 是 false",
  String(await ev("AL.data&&AL.data.armed")));
/* 尺的自證：這顆鈕真的是靠 [data-aloff] 觸發的（隨便點別的地方不會送 POST）*/
const p0 = REQS.filter(r => r.m !== "GET").length;
await ev(`document.getElementById('alstate').click();
          document.getElementById('alhow').click(); 1`);
await sleep(400);
chk("  負控組：點這一頁別的地方不會送出任何 POST",
  REQS.filter(r => r.m !== "GET").length - p0, 0);
await ctl("/f/reset"); await ctl("/f/arm/B"); await ctl("/f/live/on");
await ctl("/f/rows/mixed"); await refetch();

/* ═══ ⑨ 負控組（原始碼突變）═══════════════════════════════════════ */
console.log("\n=== ⑨ 負控組：把守衛拿掉要真的紅 ===");
if (await mutate("alPaint", "'<span class=\"al-badge\">關閉中</span>'",
  "'<span class=\"al-badge\">開啟中</span>'")) {
  await ctl("/f/arm/off"); await refetch(); await repaint();
  say((await txt()).includes("關閉中") === false,
    "  ⇒ 「關閉中」那句真的是 alPaint 畫的（拿掉就不見了）");
  await unmutate("alPaint");
  await ctl("/f/arm/B"); await refetch(); await repaint();
}
if (await mutate("alTodayHTML", "今天沒有紀錄", "還沒到 ")) {
  await ctl("/f/rows/none"); await refetch(); await repaint();
  const s = await ev(`document.getElementById('altoday').innerText`);
  say(!s.includes("今天沒有紀錄"),
    "  ⇒ 「沒有紀錄」與「還沒到」寫同一句時，⑦ 那條會分不出來（負控組成立）");
  await unmutate("alTodayHTML");
  await ctl("/f/rows/mixed"); await refetch(); await repaint();
}
/* ⛔ 「關著的時候不准有按鈕」那條的負控組：把顯示條件改成永遠成立，
   ⑧c 最後那一項（關著 ⇒ 0 顆 button）就必須抓得到。 */
if (await mutate("alPaint", "setEl('aloff', D.flag_exists",
  "setEl('aloff', true||D.flag_exists")) {
  await ctl("/f/arm/off"); await refetch(); await repaint();
  say((await ev(`document.querySelectorAll('#tab-fire button').length`)) > 0,
    "  ⇒ 關著時硬把那顆鈕畫出來，⑧c 那把尺真的會抓到（負控組成立）",
    String(await ev(`document.querySelectorAll('#tab-fire button').length`)));
  await unmutate("alPaint");
  await ctl("/f/arm/B"); await refetch(); await repaint();
  chk("  還原之後開著仍然只有 1 顆",
    await ev(`document.querySelectorAll('#tab-fire button').length`), 1);
}
if (await mutate("alName", "return w?w.n:''", "return k")) {
  await repaint();
  const l2 = await ev(`(()=>{const sel=['#alstate','#altoday','.al-tbl td:nth-child(2)'];
    let n=0;
    for(const s of sel) for(const e of document.querySelectorAll('#tab-fire '+s))
      n+=((e.innerText||'').match(/(^|[^0-9A-Za-z])[AB]([^0-9A-Za-z]|$)/g)||[]).length;
    return n;})()`);
  say(l2 > 0, "  ⇒ 名字換回代號時，④ 那條孤立字母的尺真的會抓到", String(l2));
  await unmutate("alName");
  await repaint();
}

/* ═══ ⑩ 收尾 ═══════════════════════════════════════════════════════ */
console.log("\n=== ⑩ 收尾 ===");
await refetch();
/* ⭐⭐ 這一條是從舊版的「整場零個 POST」**改對**的，⛔ 不是放寬成不驗：
   這一頁現在有一個會改變狀態的動作（關閉自動下單），所以規則收緊成三件事 ——
     ① 整場所有非 GET 的請求**只能是** POST /api/fire/off；
     ② 而且**只有我親手按那一下**才會有（恰好 1 次）；
     ③ 它只會關不會開（⑧c 已經量過：按完檔案不見、armed=false、鈕自己消失）。
   ⛔ 任何其他 POST（尤其是「開啟」）都算紅。 */
const nonGet = REQS.filter(r => r.m !== "GET");
/* ⛔ 同上：比「路徑＋查詢字串」，不比 pathname。 */
chk("⛔ 整場所有非 GET 的請求只有「關閉自動下單」那一個（含查詢字串）",
  [...new Set(nonGet.map(nonGetKey))],
  ["POST /api/fire/off"]);
/* 尺的自證：這把尺**真的看得見**查詢字串（不然上面那條等於還在比 pathname）。 */
chk("  自證：這把尺看得見查詢字串",
  nonGetKey({ m: "POST", u: "http://x/api/fire/off?arm=A" }),
  "POST /api/fire/off?arm=A");
chk("⛔ 而且它只發生一次（＝只有我親手按的那一下）", nonGet.length, 1);
chk("⛔ 全程沒有任何請求打到 /api/enter 或 /api/real/*",
  REQS.filter(r => /\/api\/(enter|real\/)/.test(r.u)).map(r => r.u), []);
chk("⛔ 全程沒有任何「開啟自動下單」的請求",
  REQS.filter(r => /\/api\/fire\/(on|arm|method)/.test(r.u)).map(r => r.u), []);
say(REQS.length > 10, "  自證：這一場真的攔到請求了",
  `${REQS.length} 個，方法有 ${[...new Set(REQS.map(r => r.m))].join("/")}`);
say(REQS.some(r => r.u.includes("/api/fire/state")),
  "  而且真的打過 /api/fire/state");
chk("全程 console／exception 零錯誤", ERRORS, []);
await ev("setTab('live')");
await sleep(600);
say((await ev("AL.timer")) === null, "  切走之後 5 秒輪詢已經停掉");
const after = await ctl("/f/where");
say(after.prod_flag_exists === false,
  "⛔⛔ 跑完之後真的 AUTO_ORDERS_ON 仍然**不存在**");

DONE = true;
console.log(`\n${"=".repeat(60)}\n共 ${N} 項，${FAIL ? FAIL + " 項未過" : "全部通過"}`);
c.close(); ch.kill();
process.exit(FAIL ? 1 : 0);
