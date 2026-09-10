/*
  【自動下單】分頁（**會真的送出委託單**的那一頁）的前端探針。

  ⚠️ 不連永豐、⛔ **不碰 8770**（Benson 的面板正開著）。只打治具（8775／控制埠 8776）。
     跑法：先起 tools/probe/fire_harness.py，改過程式一定要**重起治具**，再
           node tools/probe/fire-tab.mjs

  ⛔ 價格一律 12000 附近，一個真實成交價／進出場時間／點數都不准出現。

  這一頁要守的三條紅線（其餘的行為在 tools/shioaji/test_auto_fire.py）：
    ① **現在是開還是關**、**選了哪個做法**、**今天送了沒／為什麼沒送** —— 三件都要在畫面上。
    ② ⭐⭐ **兩段式打開／一鍵關閉**（Benson 2026-09-09 下午：「我按個鈕就可以開始了」）：
       ・關著 ⇒ **恰好 2 顆**做法鈕（`[data-alon]`），⛔ 沒有表單、沒有輸入框；
         ⛔ **按第一段不准送出任何請求**（⑪ 用 CDP 攔請求量）。
       ・確認條要**當場講清楚現在是真錢還是演練**（那句話由後端算），
         ⛔ 兩種模式的文案與底色都不可以一樣；「取消」要真的回到兩顆鈕。
       ・開著 ⇒ **恰好 1 顆**「關閉自動下單」，⛔ 那兩顆「開始」必須消失。
       ・⛔ 整場允許的非 GET 請求**只有兩個**：我親手按的那兩下
         （`POST /api/fire/off` ＋ `POST /api/fire/on`），⛔ 一個都不准多。
         ⚠️ 這條斷言連改兩次（零個 POST → 只有 off → off＋on），
         ⛔ **每次都是收緊**：規則從「不准有」變成「只准有我按的那幾下」。
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
  for(const id of ['alstate','alsub','algates','alrisk','alon','aloff','altoday','altbl',
                   'alempty','alnotes','alcount','allogn']){
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

/* ═══ ② ⛔ 關著的時候：恰好兩顆「開始」，⛔ 一個表單／輸入框都沒有 ═══════
   ⚠️ 2026-09-09 這一條**改了語意**（Benson 要求把開關做到面板上）：
      舊版是「關著的時候整頁 0 顆按鈕」，現在是「關著的時候恰好 2 顆，
      而且那兩顆**按下去不會直接開**（第二段確認才會）」。
   ⛔ 這不是放寬：規則變成三條，⑪ 那一整節全部是新的紅線
      （沒確認就不准送請求／取消要真的取消／確認條要講出現在是不是真錢）。 */
console.log("\n=== ② 關著的時候：恰好兩顆「開始」 ===");
chk("  button 2 顆", await ev(`document.querySelectorAll('#tab-fire button').length`), 2);
chk("    而且就是那兩顆做法鈕",
  await ev(`document.querySelectorAll('#tab-fire [data-alon]').length`), 2);
chk("    ⛔ 沒有「關閉」那一顆（沒東西可關）",
  await ev(`document.querySelectorAll('#tab-fire [data-aloff]').length`), 0);
chk("    ⛔ 也還沒有確認條（⛔ 沒按之前不准出現）",
  await ev(`document.querySelectorAll('#tab-fire .al-conf').length`), 0);
for (const [nm, sel] of [["form", "form"], ["input", "input"],
["[data-act]", "[data-act]"], ["[data-rdir]", "[data-rdir]"],
["[type=submit]", "[type=submit]"], ["<a href>", "a[href]"]]) {
  chk(`  ${nm} 0 顆`, await ev(`document.querySelectorAll('#tab-fire ${sel}').length`), 0);
}
say((await ev(`[...document.querySelectorAll('#tab-fire *')]
  .filter(e=>e.onclick||e.getAttribute('onclick')).length`)) === 0,
  "  也沒有任何 onclick");
/* ⛔ 鈕上要直接寫做法的名字（他不必先去別的地方查哪個是哪個），
   ⛔ 而且不准出現代號（④ 的孤立字母那條尺涵蓋 #alon）。 */
chk("  ⛔ 兩顆鈕上寫的是做法的名字",
  await ev(`[...document.querySelectorAll('#tab-fire [data-alon]')].map(e=>e.textContent)`),
  ["用「5 分 K」開始", "用「開盤起」開始"]);
say((await ev(`(()=>[...document.querySelectorAll('#tab-fire [data-alon]')]
  .every(b=>{const r=b.getBoundingClientRect();
    return r.width>60&&r.height>24&&getComputedStyle(b).display!=='none';}))()`)),
  "    兩顆都按得到（尺寸與可見度量過）",
  JSON.stringify(await ev(`[...document.querySelectorAll('#tab-fire [data-alon]')]
    .map(b=>{const r=b.getBoundingClientRect();
      return [Math.round(r.width),Math.round(r.height)];})`)));
say((await ev(`document.getElementById('alon').innerText`)).includes("再問你一次"),
  "    ⛔ 旁邊那行小字講明「按下去會再問你一次」");
// 尺的自證：同一把尺在「即時」那一頁的真實下單區抓得到按鈕
say((await ev(`document.querySelectorAll('#tab-live button').length`)) > 0,
  "  負控組：同一把尺在【即時】抓得到按鈕",
  String(await ev(`document.querySelectorAll('#tab-live button').length`)));

/* ═══ ③ 關著：畫面要一眼看出來 ═══════════════════════════════════ */
console.log("\n=== ③ 關著（出貨狀態）===");
let t = await txt();
say(t.includes("關閉中"), "  寫著「關閉中」");
say(t.includes("AUTO_ORDERS_ON"), "  寫得出開關檔叫什麼");
say(t.includes("按下面那兩顆就可以開始"), "  而且講得出怎麼開（⛔ 就是那兩顆鈕）");
chk("  開關徽章不是「開啟中」",
  await ev(`document.querySelector('#tab-fire .al-badge').textContent`), "關閉中");
say((await ev(`getComputedStyle(document.querySelector('#tab-fire .al-badge')).color`))
  !== "rgb(238, 90, 84)",
  "  ⛔ 關著不是紅色（關著是正常狀態，紅綠只給損益）",
  await ev(`getComputedStyle(document.querySelector('#tab-fire .al-badge')).color`));
/* ⚠️⚠️ 2026-09-10：「面板關掉就沒有停損」那句話從〈怎麼開〉那段搬到**金色風險條**
   （`.al-risk`），而且**只在開著的時候畫** —— 開關關著就沒有那口自動部位可談，
   UX 規格定的「關著剩 5 行」就是這個意思。開著時那一條在 ⑧b 驗（那裡 arm=B）。 */
say(!t.includes("停損活在"),
  "  ⛔ 關著的時候不畫風險條（沒有自動下單就沒有那兩件事要提醒）");
chk("    而且風險條真的是空的", await ev(`document.getElementById('alrisk').innerHTML`), "");

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
   ⚠️⚠️ **2026-09-10：規則收緊了，⛔ 不是放寬。** 舊版有一個合法的例外
      ——〈怎麼開〉那段（`#alhow`），因為他要**自己把那個字母打進檔案裡**。
      那一整段已經隨開關區精簡整個砍掉（開／關兩顆鈕就在畫面上，
      不必再教他建檔）⇒ **那個例外也跟著沒了** ⇒ 現在是「整頁零命中」。 */
/* ⚠️ 量之前要把 `.why`（後端那句「為什麼沒送」的原話）**剝掉** ——
   舊版表格的做法欄是 `td:nth-child(2)`、原因在另一格 `td .why`，天生就分開；
   改成卡片之後兩者同在 `.al-meta` 這一行，所以產品把原話包進 `<span class="why">`，
   ⛔ 這是**保持原本的界定範圍**，不是放寬（原話講的是「你該往檔案裡寫什麼」，
   不是「這個做法叫什麼名字」，而 Benson 退件的那件事發生在名字上）。 */
const STRIP = `(e)=>{const c=e.cloneNode(true);
  c.querySelectorAll('.why').forEach(x=>x.remove());
  c.style.position='absolute'; c.style.left='-9999px';
  document.body.appendChild(c); const s=c.innerText; c.remove(); return s;}`;
const lone = await ev(`(()=>{const strip=${STRIP};
  const sel=['#alstate','#altoday','#altbl .al-meta','#alon','#alrisk'];
  const out=[];
  for(const s of sel) for(const e of document.querySelectorAll('#tab-fire '+s))
    out.push(...((strip(e)||'').match(/(^|[^0-9A-Za-z])[AB]([^0-9A-Za-z]|$)/g)||[]));
  return out;})()`);
chk("  ⛔ 講「哪個做法」的地方（狀態／今天／卡片第二行／開啟鈕／風險條）孤立字母零命中",
  lone, []);
say(!(await ev(`!!document.getElementById('alhow')`)),
  "  ⛔〈怎麼開〉那段（#alhow）已經整個不存在（開關區精簡）");
/* ⛔ 收緊之後的那條：**整個 #tab-fire** 都不准有孤立的 A／B。
   ⚠️ 檔名（AUTO_ORDERS_ON／REAL_ORDERS_ON）不會命中 —— 那個 A 兩邊都黏著字母。 */
const loneAll = await ev(`(()=>{const strip=${STRIP};
  return (strip(document.getElementById('tab-fire'))
    .match(/(^|[^0-9A-Za-z])[AB]([^0-9A-Za-z]|$)/g)||[]);})()`);
chk("  ⛔ 整個 #tab-fire 孤立 A／B 零命中（⛔ 這是收緊，不是放寬）", loneAll, []);
/* ⚠️ 為什麼「原因欄」不在這把尺的破口上（⛔ 這是界定範圍，不是放寬）：
   原因印的是後端那句話（例：「讀到『C』，只認得 A 或 B」）——
   它講的是**你該往那個檔案裡寫什麼**，不是「這個做法叫什麼名字」。
   ⚠️ 所以上面那條「整頁零命中」只在**沒有那種原因**的狀態下量（現在是 arm=B）；
      ⑤ 那一節（內容看不懂）另外量它自己那句話。 */
say((await ev(`[...document.querySelectorAll('#tab-fire #altbl .al-meta')]
  .map(e=>(e.innerText||'').split(' · ')[0])`))
  .every(s => ["—", "5 分 K", "開盤起"].includes(s.trim()) || s.length > 6),
  "  卡片第二行開頭是做法的名字（⛔ 不是代號）",
  JSON.stringify(await ev(`[...new Set([...document.querySelectorAll(
    '#tab-fire #altbl .al-meta')].map(e=>(e.innerText||'').split(' · ')[0].trim()))]`)));

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
/* ⚠️ 2026-09-10：改成 `.trade` 卡片之後，方向是那顆 `.dir` 藥丸（「▼ 空」）——
   ⛔ 那是**照抄**練習／真實那份卡片（`row()` / `realCard()` 都是這樣寫的），
   ⛔ 不可以為了讓這條斷言過就在這一頁自己改回「做空」兩個字。 */
say(t.includes("▼ 空") && t.includes("11987"), "  做空那一天的方向與進場價都看得到");
chk("    而且那顆藥丸掛的是 .dir.s（跟練習／真實同一套）",
  await ev(`[...document.querySelectorAll('#altbl .dir')].some(
    e=>e.classList.contains('s'))`), true);
say(t.includes("模擬那邊"), "  有「跟模擬對得起來」那一欄");
/* ⚠️ 2026-09-10：從表格改成 `.trade` 卡片（跟練習／真實同一種），所以數的是卡片。 */
const nrows = await ev(`document.querySelectorAll('#tab-fire #altbl .trade').length`);
say(nrows >= 8, "  每一天都有一張卡", String(nrows));
/* ⛔⛔ 「形式長的一樣」是這一版的要求（2026-09-03 Benson 退件過一次）⇒
   卡片裡的骨架必須跟練習／真實那份逐項對得上，⛔ 不可以只是「看起來很像」。 */
const cardShape = await ev(`(()=>{const c=document.querySelector('#tab-fire #altbl .trade');
  if(!c) return null;
  return {top:!!c.querySelector('.tr-top'), date:!!c.querySelector('.tr-date'),
          px:!!c.querySelector('.tr-px'), res:!!c.querySelector('.tr-res'),
          tag:!!c.querySelector('.tag'), meta:!!c.querySelector('.al-meta')};})()`);
chk("  ⛔ 卡片骨架跟練習／真實那份一樣（.tr-top/.tr-date/.tr-px/.tr-res/.tag ＋ .al-meta）",
  cardShape, { top: true, date: true, px: true, res: true, tag: true, meta: true });
/* ⛔ `.tag` 最多 4 個字：6 字 ＝ 161px > `.tr-px` 的 157.6px ⇒ 折行 ⇒ 那張卡 65→80px
   ⇒ 跟練習的卡片就不一樣高了（hold-to-fire.mjs ⑧b6 記過同一條）。 */
const tags = await ev(`[...new Set([...document.querySelectorAll('#tab-fire #altbl .tag')]
  .map(e=>e.textContent.trim()))]`);
say(tags.every(s => s.length <= 4), "  ⛔ 每一個 tag 都 ≤4 個字（超過會折行、卡片變高）",
  JSON.stringify(tags));
/* ⛔ 卡片不准折行（＝高度要跟練習那份一致）。同一批卡片高度只能有一種。 */
const hs = await ev(`[...new Set([...document.querySelectorAll('#tab-fire #altbl .trade')]
  .map(e=>Math.round(e.getBoundingClientRect().height)))]`);
say(hs.length === 1, "  ⛔ 每一張卡一樣高（沒有任何一張折行）", JSON.stringify(hs));

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
/* ⛔⛔ 開關區精簡之後**必須留下來、而且要更醒目**的那兩句（2026-09-10 UX 規格）。
   ⛔ 一句都不准再刪 —— 它們是這個工具最會賠錢、而他最看不出來的兩件事。 */
say(t.includes("停損活在") && t.includes("沒有停損"),
  "  ⛔⛔ 開著時把「停損活在這台電腦裡、面板關掉就沒有停損」寫在畫面上");
say(t.includes("的自動平倉也不會發生"),
  "    ⛔ 而且明講那時候收盤自動平倉也不會發生");
/* ⛔ 量的是**真的畫出來的顏色**（文字掛在 `p` 上，不是外框那一層）＋ 尺寸 ——
   ⛔ 不可以只驗「那段字在 DOM 裡」（keyring 那次「按鈕其實是透明的」的教訓）。 */
const riskCss = await ev(`(()=>{const e=document.querySelector('#alrisk .al-risk p');
  if(!e) return null; const s=getComputedStyle(e);
  const b=getComputedStyle(e.parentNode); const r=e.getBoundingClientRect();
  return [s.color, b.backgroundColor, Math.round(r.width), Math.round(r.height)];})()`);
say(riskCss && riskCss[0] === "rgb(227, 169, 81)",
  "    ⛔ 字是金色（面板既有的「注意」語彙，⛔ 不用紅綠）", JSON.stringify(riskCss));
say(riskCss && riskCss[2] > 200 && riskCss[3] > 15,
  "    ⛔ 而且真的畫得出來（尺寸量過）", JSON.stringify(riskCss));
chk("    ⛔ 恰好兩條（⛔ 一條都不准少）",
  await ev(`document.querySelectorAll('#alrisk .al-risk p').length`), 2);
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

/* ═══ ⑧d ⭐⭐ 那一口**後來怎麼了**（2026-09-10）═════════════════════
   ⛔⛔ 這一節在守的是一個**真的 bug**：`auto_fire._eod()` 在停利成交的日子走的是
      `pos is None → eod_flat`，而撈出場價的 `_eod_exit_of()` 只掛在 `eod_closed`
      那一條路 ⇒ **停利成交的日子，`autofire/*.jsonl` 永遠不會有出場價與點數**。
      修法是端點層唯讀比對 `real_trades/`（`live_panel.fire_real_pairs()`）。
   ⛔ 六種下場**一種都不准跟別種寫同一句**；⛔ 對不到就留白 ＋ 示警，
      ⛔ 不准挑一筆、不准拿現價頂（「留白看得出來是缺，編的數字看不出來」）。
   ⛔ 而且卡片的**形式**要跟練習／真實那份一樣（2026-09-03 Benson 退件過一次）。 */
console.log("\n=== ⑧d ⭐⭐ 出場那半（唯讀比對 real_trades/）===");
await ctl("/f/arm/B"); await ctl("/f/live/on");
await ctl("/f/rows/exits"); await ctl("/f/clock/10:30:00"); await refetch();
const EX = await ev(`(()=>[...document.querySelectorAll('#altbl .trade')].map(c=>({
  tag:(c.querySelector('.tag')||{}).textContent||'',
  px:c.querySelector('.tr-px').innerText.replace(/\\s+/g,''),
  res:c.querySelector('.tr-res').innerText.trim(),
  win:c.classList.contains('win'), loss:c.classList.contains('loss'),
  meta:c.querySelector('.al-meta').innerText,
  h:Math.round(c.getBoundingClientRect().height*10)/10})))()`);
chk("  七天七張卡", EX.length, 7);
chk("  ⛔ 六種下場各有自己的 tag（⛔ 一種都不准跟別種寫同一個字）",
  EX.map(r => r.tag), ["持有中", "停利", "停損", "收盤", "對不起來", "演練", "別處平的"]);
say(EX[1].px === "12013→12113停利" && EX[1].res === "+100" && EX[1].win,
  "  ⛔ 停利成交那一天：出場價與點數都印得出來（＝這一輪修掉的那個 bug）",
  EX[1].px + " " + EX[1].res);
say(EX[2].res === "-100" && EX[2].loss, "  停損那一天：點數是負的、左緣掛 .loss", EX[2].res);
say(EX[3].res === "0" && EX[3].loss,
  "  ⛔ 收盤平掉 0 點算敗（沿用「點數 > 0 才算勝」同一套定義）", EX[3].res);
say(EX[0].tag === "持有中" && EX[0].res === "—" && !EX[0].win && !EX[0].loss,
  "  ⛔ 還開著：點數留白，左緣**維持灰**（⛔ 不准先染紅綠）");
say(EX[4].tag === "對不起來" && EX[4].res === "—" && !EX[4].win && !EX[4].loss,
  "  ⛔ 對不起來：一樣留白（⛔ 不挑一筆、不拿現價頂）");
say(EX[4].meta.includes("大戶投"), "    而且告訴他去哪裡看", EX[4].meta);
say(EX[0].tag !== EX[4].tag,
  "  ⛔⛔ 「還開著」跟「對不起來」⛔ 不准長一樣（兩邊的留白一模一樣）");
say(EX[6].tag === "別處平的" && EX[6].res === "—" && !EX[6].win && !EX[6].loss,
  "  ⛔ 對到了但問不到成交價 ⇒ 出場價與點數照樣留白、不猜輸贏");
say(EX[5].tag === "演練", "  ⛔ 演練那一天有自己的 tag（結構上不會有 real_trades 那一列）");
say(!(await ev(`document.getElementById('alnotes').innerText`)).includes("演練") ||
  (await ev(`document.getElementById('alnotes').innerText`)).includes("對不到出場紀錄"),
  "  ⛔ 演練不可以被算成「對不起來」");
const nm = await ev(`document.getElementById('alnotes').innerText`);
say(/有\s*1\s*天對不到出場紀錄/.test(nm), "  ⛔ 摘要那一行示警**但不擋**（恰好 1 天）", nm);
say(EX.every(r => r.tag.length <= 4), "  ⛔ 每一個 tag ≤4 字",
  JSON.stringify([...new Set(EX.map(r => r.tag))]));
chk("  ⛔ 七張卡一樣高（沒有任何一張折行 ⇒ 形式跟練習那份一致）",
  [...new Set(EX.map(r => r.h))].length, 1);
/* ⛔⛔ 「今天」那一塊吃的是同一份資料 —— 不一起改的話會變成
   「紀錄寫 +100、今天還寫著 13:43:30 會自動平倉」。 */
await ctl("/f/rows/exittoday"); await refetch();
const T2 = await ev(`document.getElementById('altoday').innerText`);
say(T2.includes("已出場"), "  ⛔ 今天那一口平掉了 ⇒ 抬頭寫「已出場」（⛔ 不是「已送出委託單」）", T2.split("\n")[0]);
say(T2.includes("12113") && T2.includes("+100"), "    出場價與點數都在「今天」那一塊");
say(!T2.includes("會自動平倉"),
  "  ⛔⛔ 而且**不再預告 13:43:30 會自動平倉**（那一口已經不在了，講了就是假話）");
say(T2.includes("不需要"), "    改成講「不需要收盤平倉」（第七種結局，⛔ 不跟那六種混）", T2);
await ctl("/f/clock/14:00:00"); await refetch();
const T3 = await ev(`document.getElementById('altoday').innerText`);
say(!T3.includes("沒有留下收盤平倉的紀錄"),
  "  ⛔⛔ 收盤之後也不可以跳「沒有留下收盤平倉的紀錄」的金色警示（那一口 09:27 就出場了）",
  T3.replace(/\n/g, " | "));
await ctl("/f/clock/10:30:00"); await ctl("/f/rows/today"); await refetch();

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
/* 關著 ⇒ 沒東西可關 ⇒ 那顆鈕不見（⛔ 換成那兩顆「開始」，⛔ 不會兩組同時在）*/
await ctl("/f/arm/off"); await refetch();
chk("  ⛔ 關著的時候「關閉」那顆不見（沒東西可關）",
  await ev(`document.querySelectorAll('#tab-fire [data-aloff]').length`), 0);
chk("    而且換成那兩顆「開始」（⛔ 恰好 2 顆、⛔ 不會跟「關閉」同時在）",
  await ev(`document.querySelectorAll('#tab-fire button').length`), 2);

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
  await ev(`document.querySelectorAll('#tab-fire [data-aloff]').length`), 0);
chk("    ⇒ 而且畫面回到那兩顆「開始」",
  await ev(`document.querySelectorAll('#tab-fire [data-alon]').length`), 2);
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
          document.getElementById('alrisk').click(); 1`);
await sleep(400);
chk("  負控組：點這一頁別的地方不會送出任何 POST",
  REQS.filter(r => r.m !== "GET").length - p0, 0);
await ctl("/f/reset"); await ctl("/f/arm/B"); await ctl("/f/live/on");
await ctl("/f/rows/mixed"); await refetch();

/* ═══ ⑪ ⭐⭐ 打開自動下單（兩段式）═══════════════════════════════════
   ⛔⛔ 這是這個面板上**唯一一顆會武裝真錢**的鈕。這一節的每一條都是紅線：
     ① 沒按「確定」之前**一個請求都不准出去**（⛔ 用 CDP 攔請求量，不是看畫面）
     ② 確認條要**當場講清楚現在是真錢還是演練**（⛔ 兩種不可以寫同一句）
     ③「取消」要**真的回到兩顆鈕的狀態**（不是把條子藏起來）
     ④ 切走分頁再回來要重置成**未確認**
     ⑤ 按「確定」之後開關檔真的建出來、畫面真的變成「開啟中」 */
console.log("\n=== ⑪ ⭐⭐ 打開自動下單（兩段式）===");
await ctl("/f/reset"); await ctl("/f/live/off"); await refetch();
const P0 = REQS.filter(r => r.m !== "GET").length;
/* ── 第一段：按「用『5 分 K』開始」 —— ⛔ 只換畫面，⛔ 一個請求都不送 */
await ev(`[...document.querySelectorAll('#tab-fire [data-alon]')]
  .find(b=>b.getAttribute('data-alon')==='A').click()`);
await sleep(500);
chk("  ⛔⛔ 第一段按下去：**零個非 GET 請求**（沒確認就不准送）",
  REQS.filter(r => r.m !== "GET").length - P0, 0);
chk("    確認條出現了", await ev(`document.querySelectorAll('#tab-fire .al-conf').length`), 1);
chk("    ⛔ 兩顆做法鈕收起來了（⛔ 不可以還能再按另一顆）",
  await ev(`document.querySelectorAll('#tab-fire [data-alon]').length`), 0);
chk("    確認條上恰好兩顆鈕（確定／取消）",
  await ev(`document.querySelectorAll('#tab-fire .al-conf button').length`), 2);
chk("      文案逐字",
  await ev(`[...document.querySelectorAll('#tab-fire .al-conf button')]
    .map(e=>e.textContent)`), ["確定，打開", "取消"]);
let conf = await ev(`document.getElementById('alon').innerText`);
say(conf.includes("5 分 K"), "    講得出要用哪個做法", conf.slice(0, 60));
/* ── ⛔⛔ 演練模式：那句話要講「不會真的送單」，⛔ 而且不准嚇他 */
say(conf.includes("演練") && conf.includes("不會真的送單"),
  "  ⛔ 演練模式 ⇒ 明講「會照跑但不會真的送單」", conf.slice(0, 70));
say(!conf.includes("你的錢"), "    ⛔ 而且不准寫「會用你的錢」（那是假話）");
chk("    ⛔ 演練那條**不是**紅底（紅底只給真錢那一種）",
  await ev(`document.querySelector('#tab-fire .al-conf').classList.contains('real')`),
  false);
const dryBG = await ev(`getComputedStyle(document.querySelector(
  '#tab-fire .al-conf')).backgroundColor`);
/* ── ③ 取消：⛔ 要真的回到兩顆鈕 */
await ev(`document.querySelector('#tab-fire [data-alno]').click()`);
await sleep(400);
chk("  ⛔「取消」之後回到兩顆做法鈕",
  await ev(`document.querySelectorAll('#tab-fire [data-alon]').length`), 2);
chk("    確認條不見了",
  await ev(`document.querySelectorAll('#tab-fire .al-conf').length`), 0);
chk("    ⛔⛔ 而且從頭到尾一個非 GET 請求都沒有出去",
  REQS.filter(r => r.m !== "GET").length - P0, 0);
const W3 = await ctl("/f/where");
say(W3.real_flag_exists === false, "    ⛔ 開關檔當然也沒有被建出來");
/* ── ④ 切走分頁再回來 ⇒ 重置成未確認 */
await ev(`document.querySelector('#tab-fire [data-alon]').click()`);
await sleep(300);
chk("  前置：又展開了一次確認條",
  await ev(`document.querySelectorAll('#tab-fire .al-conf').length`), 1);
await ev("setTab('live')"); await sleep(400);
await ev("setTab('fire')"); await sleep(900);
chk("  ⛔ 切走再回來 ⇒ 回到未確認（⛔ 不可以留著展開到一半的確認條）",
  await ev(`document.querySelectorAll('#tab-fire .al-conf').length`), 0);
chk("    而且兩顆做法鈕回來了",
  await ev(`document.querySelectorAll('#tab-fire [data-alon]').length`), 2);
/* ── ⛔⛔ 真錢模式：那句話與底色都要換 */
await ctl("/f/live/on"); await refetch();
await ev(`[...document.querySelectorAll('#tab-fire [data-alon]')]
  .find(b=>b.getAttribute('data-alon')==='B').click()`);
await sleep(400);
conf = await ev(`document.getElementById('alon').innerText`);
say(conf.includes("真實下單模式") && conf.includes("你的錢"),
  "  ⛔⛔ 真錢模式 ⇒ 明講「會用你的錢真的送單」", conf.slice(0, 70));
say(conf.includes("09:03:30") && conf.includes("一天一次") && conf.includes("100 點"),
  "    ⛔ 而且講得出幾點送、多久一次、停利停損幾點", conf.slice(0, 90));
say(!conf.includes("演練"), "    ⛔ 兩種模式不會寫同一句（一定有一句是假的）");
say(conf.includes("開盤起"), "    這一次選的是另一個做法（鈕上選的就是要用的）");
chk("    ⛔ 真錢那條是紅底",
  await ev(`document.querySelector('#tab-fire .al-conf').classList.contains('real')`),
  true);
say((await ev(`getComputedStyle(document.querySelector(
  '#tab-fire .al-conf')).backgroundColor`)) !== dryBG,
  "    ⛔⛔ 兩種模式的底色真的不一樣（量過，⛔ 不是只有文字不同）",
  (await ev(`getComputedStyle(document.querySelector(
    '#tab-fire .al-conf')).backgroundColor`)) + " vs " + dryBG);
say((await ev(`(()=>{const r=document.querySelector(
  '#tab-fire .al-conf').getBoundingClientRect(); return r.width>200&&r.height>50;})()`)),
  "    那條確認條真的畫得出來（尺寸量過）",
  JSON.stringify(await ev(`(()=>{const r=document.querySelector(
    '#tab-fire .al-conf').getBoundingClientRect();
    return [Math.round(r.width),Math.round(r.height)];})()`)));
/* ⛔ 這一頁不准出現孤立的 A／B 代號 —— 關著（＝有那兩顆鈕）的時候也要守。
   ④ 那條尺跑的時候開關是開著的、#alon 是空的，所以這裡再量一次。 */
chk("  ⛔ 開啟鈕與確認條上孤立的 A／B 零命中",
  await ev(`(()=>((document.getElementById('alon').innerText||'')
    .match(/(^|[^0-9A-Za-z])[AB]([^0-9A-Za-z]|$)/g)||[]))()`), []);
/* ── ⑤ 按「確定，打開」：⛔ 這一下才會有請求 */
const P1 = REQS.filter(r => r.m !== "GET").length;
await ev(`document.querySelector('#tab-fire [data-alyes]').click()`);
for (let i = 0; i < 80; i++) {
  if (!(await ev("AL.pending")) && (await ev("AL.data&&AL.data.flag_exists"))) break;
  await sleep(150);
}
await sleep(300);
const newP = REQS.filter(r => r.m !== "GET").slice(P1);
chk("  ⛔ 這一下**只**送出一個 POST，而且是 /api/fire/on（含查詢字串）",
  newP.map(nonGetKey), ["POST /api/fire/on"]);
const W4 = await ctl("/f/where");
say(W4.real_flag_exists === true, "  ⛔ 開關檔真的建出來了（治具的暫存區）");
say(await ev("AL.data&&AL.data.armed===true"), "  ⇒ armed 變成 true");
t = await txt();
say(t.includes("開啟中"), "  ⇒ 畫面變成「開啟中」");
say(t.includes("開盤起"), "  ⇒ 而且跑的就是他按的那個做法");
chk("  ⛔ 開著之後那兩顆「開始」不見了（⛔ 開與關不可以同時在畫面上）",
  await ev(`document.querySelectorAll('#tab-fire [data-alon]').length`), 0);
chk("    只剩「關閉」那一顆",
  await ev(`document.querySelectorAll('#tab-fire button').length`), 1);
/* ⛔ 落地一列（誰在什麼時候用哪個做法開的、當下是不是真錢）*/
const armRows = (W4.arm_rows || []);
chk("  ⛔ 落地恰好一列", armRows.length, 1);
say(armRows[0] && armRows[0].rec === "arm" && armRows[0].method === "B"
  && armRows[0].live === true && !!armRows[0].at && !!armRows[0].who,
  "    那一列講得出「什麼時候、哪個做法、當下是不是真錢」",
  JSON.stringify(armRows[0] || null).slice(0, 140));
say(W4.ledger && W4.ledger.bad === 0,
  "  ⛔⛔ 而且沒有污染 autofire 的帳本（bad 還是 0）",
  JSON.stringify(W4.ledger));
/* ⛔ 再按一次「開」是不可能的（鈕不見了）——但後端那條 409 由
   test_fire_routes.py ③b 驗（那一支真的起 live_panel.Handler 打進去）。*/

/* ═══ ⑪b ⭐⭐ R2：確認條要說「今天」還是「下一個交易日」════════════════
   ⚠️⚠️ 2026-09-09 lab-qa 退件 R2：那句話原本寫死「**下一個交易日** 09:03:30」，
      但 `auto_fire` **沒有「今天開的不算」的閘門** ⇒ 他 08:50 按下去，13 分鐘後
      今天就送一口真單，而畫面告訴他是明天。
   ⭐ Benson 裁示：**改文案、不加閘門**。
   ⛔ 這裡要驗**兩種時間點各一次**（盤前／盤後）—— 只驗一種的話，
      「永遠寫今天」跟「永遠寫下一個交易日」有一種一定全綠。
   ⚠️ 那句話的正本在後端（`fire_arm_confirm` → `fire_fires_today`），治具用
      `/f/now/<ISO>` 塞一個假的「現在」進去；⛔ 前端一個字都不准自己算。
   ⛔ 這一節**一個 POST 都不送**（只按第一段、然後取消）。 */
console.log("\n=== ⑪b ⭐⭐ 確認條的「今天／下一個交易日」（盤前／盤後各一次）===");
const P2 = REQS.filter(r => r.m !== "GET").length;
const MON = "2026-09-14";            /* ⛔ 寫死的星期一，跟跑測試的日子無關 */
for (const [tag, iso, want, nope] of [
  ["盤前（08:50，13 分鐘後就會送）", `${MON}T08:50:00`, "今天 09:03:30", "下一個交易日"],
  ["盤後（10:30，今天那一刻已經過了）", `${MON}T10:30:00`,
    "下一個交易日 09:03:30", "今天"],
]) {
  await ctl("/f/reset"); await ctl("/f/live/on"); await ctl(`/f/now/${iso}`);
  await refetch();
  await ev(`[...document.querySelectorAll('#tab-fire [data-alon]')]
    .find(b=>b.getAttribute('data-alon')==='A').click()`);
  await sleep(400);
  const t2 = await ev(`document.getElementById('alon').innerText`);
  say(t2.includes(want), `  ⛔ ${tag} ⇒ 那句話寫「${want}」`, t2.slice(0, 90));
  say(!t2.includes(nope), `    ⛔ 而且沒有寫「${nope}」（寫錯就是他不知道今天會不會送）`);
  await ev(`document.querySelector('#tab-fire [data-alno]').click()`);
  await sleep(250);
}
chk("  ⛔⛔ 而且這一整節**一個非 GET 請求都沒有送**（只按第一段＋取消）",
  REQS.filter(r => r.m !== "GET").length - P2, 0);
/* 尺的自證：兩種時間點真的量到**不一樣**的字（不然上面兩條有一條是恆真的）——
   由上面「want／nope 互為對方」的結構保證：同一句話不可能同時滿足兩組。 */
await ctl("/f/now");                 /* ⛔ 收乾淨，回到「真的現在」 */

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
  /* ⚠️ ⛔ 這裡要數的是 `[data-aloff]` **不是** `button` —— 2026-09-09 之後
     關著的時候本來就有兩顆「開始」，數 button 的話這個負控組會變成**恆真**
     （突變沒生效也照樣大於 0）。 */
  say((await ev(`document.querySelectorAll('#tab-fire [data-aloff]').length`)) > 0,
    "  ⇒ 關著時硬把「關閉」那顆畫出來，⑧c 那把尺真的會抓到（負控組成立）",
    String(await ev(`document.querySelectorAll('#tab-fire [data-aloff]').length`)));
  await unmutate("alPaint");
  await ctl("/f/arm/B"); await refetch(); await repaint();
  chk("  還原之後開著仍然只有 1 顆",
    await ev(`document.querySelectorAll('#tab-fire button').length`), 1);
}
/* ⛔⛔ 「開著的時候不准還看得到『打開』」那條的負控組 */
if (await mutate("alOnHTML", "if(D.flag_exists) return ''", "if(false) return ''")) {
  await ctl("/f/arm/B"); await refetch(); await repaint();
  say((await ev(`document.querySelectorAll('#tab-fire [data-alon]').length`)) > 0,
    "  ⇒ 開著時硬把「打開」畫出來，⑪ 那把尺真的會抓到（負控組成立）",
    String(await ev(`document.querySelectorAll('#tab-fire [data-alon]').length`)));
  await unmutate("alOnHTML");
  await ctl("/f/arm/B"); await refetch(); await repaint();
}
/* ⛔⛔ 「確認條那句話是後端算的」那條的負控組：前端自己寫死一句演練，
   真錢模式下那句話就會變成假話 —— ⑪ 必須抓得到。 */
if (await mutate("alOnHTML", "esc(C.text)", "'現在是演練模式，會照跑但不會真的送單。'")) {
  await ctl("/f/arm/off"); await ctl("/f/live/on"); await refetch();
  await ev(`ALON.step='confirm'; ALON.mode='B'; 1`); await repaint();
  const s = await ev(`document.getElementById('alon').innerText`);
  say(!s.includes("你的錢") && s.includes("演練"),
    "  ⇒ 前端自己寫死文案時，⑪「真錢那句」那條真的會分不出來（負控組成立）",
    s.slice(0, 50));
  await unmutate("alOnHTML");
  await ev(`ALON.step='idle'; ALON.mode=null; 1`);
  await ctl("/f/arm/B"); await refetch(); await repaint();
}
/* ⛔ 「真錢那條是紅底」的負控組 */
if (await mutate("alOnHTML", "(C.live?' real':'')", "''")) {
  await ctl("/f/arm/off"); await ctl("/f/live/on"); await refetch();
  await ev(`ALON.step='confirm'; ALON.mode='B'; 1`); await repaint();
  say((await ev(`document.querySelector('#tab-fire .al-conf')
    .classList.contains('real')`)) === false,
    "  ⇒ 拿掉紅底之後，⑪ 那條會抓到（負控組成立）");
  await unmutate("alOnHTML");
  await ev(`ALON.step='idle'; ALON.mode=null; 1`);
  await ctl("/f/arm/B"); await refetch(); await repaint();
}
if (await mutate("alName", "return w?w.n:''", "return k")) {
  await repaint();
  const l2 = await ev(`(()=>{const strip=${STRIP};
    const sel=['#alstate','#altoday','#altbl .al-meta'];
    let n=0;
    for(const s of sel) for(const e of document.querySelectorAll('#tab-fire '+s))
      n+=((strip(e)||'').match(/(^|[^0-9A-Za-z])[AB]([^0-9A-Za-z]|$)/g)||[]).length;
    return n;})()`);
  say(l2 > 0, "  ⇒ 名字換回代號時，④ 那條孤立字母的尺真的會抓到", String(l2));
  await unmutate("alName");
  await repaint();
}

/* ═══ ⑩ 收尾 ═══════════════════════════════════════════════════════ */
console.log("\n=== ⑩ 收尾 ===");
await refetch();
/* ⭐⭐ 這一條連改兩次，⛔ 兩次都是**收緊**不是放寬：
     v1「整場零個 POST」→ v2「只有 POST /api/fire/off、只發生一次」
     → v3（2026-09-09，開關做到面板上）：整場非 GET 的請求**只有兩個**，
       就是我親手按的那兩下（關閉一次、打開一次），⛔ 一個都不准多。
   ⛔ 「打開」那一下**只有按過『確定』才會有**（⑪ 已經量過：第一段與取消都是 0 個）。
   ⛔ 任何其他 POST（換做法、改設定、重試…）都算紅。 */
const nonGet = REQS.filter(r => r.m !== "GET");
/* ⛔ 同上：比「路徑＋查詢字串」，不比 pathname。 */
chk("⛔ 整場所有非 GET 的請求只有那兩顆鈕（含查詢字串）",
  [...new Set(nonGet.map(nonGetKey))].sort(),
  ["POST /api/fire/off", "POST /api/fire/on"]);
/* 尺的自證：這把尺**真的看得見**查詢字串（不然上面那條等於還在比 pathname）。 */
chk("  自證：這把尺看得見查詢字串",
  nonGetKey({ m: "POST", u: "http://x/api/fire/off?arm=A" }),
  "POST /api/fire/off?arm=A");
chk("⛔ 而且恰好兩次（＝只有我親手按的那兩下）", nonGet.length, 2);
chk("⛔ 全程沒有任何請求打到 /api/enter 或 /api/real/*",
  REQS.filter(r => /\/api\/(enter|real\/)/.test(r.u)).map(r => r.u), []);
chk("⛔ 全程沒有任何 /api/fire/arm 或 /api/fire/method（那兩個端點不存在）",
  REQS.filter(r => /\/api\/fire\/(arm|method)/.test(r.u)).map(r => r.u), []);
chk("⛔ 而且「打開」那個端點**只被打過一次**（⛔ 沒有第二下）",
  REQS.filter(r => /\/api\/fire\/on/.test(r.u)).length, 1);
say(REQS.length > 10, "  自證：這一場真的攔到請求了",
  `${REQS.length} 個，方法有 ${[...new Set(REQS.map(r => r.m))].join("/")}`);
say(REQS.some(r => r.u.includes("/api/fire/state")),
  "  而且真的打過 /api/fire/state");
chk("全程 console／exception 零錯誤", ERRORS, []);
await ev("setTab('live')");
await sleep(600);
say((await ev("AL.timer")) === null, "  切走之後 5 秒輪詢已經停掉");
/* ⛔⛔ 【P0 的另一半】治具的 do_POST 走的是**產品的** `fire_post_guard()`
   ⇒ 前端只要有一個呼叫點漏帶標頭／token，那一下就會被擋成 403 並記在這裡。
   ⛔ 「零筆」＝ **他自己按的每一顆鈕都還按得動**（不是只證明攻擊被擋）。 */
const blk = await ctl("/f/blocked");
chk("⛔⛔ 全程沒有任何一下被守衛擋掉（＝他的鈕都還能用）", blk.blocked || [], []);
say(REQS.filter(r => r.m !== "GET").length === 2,
  "  自證：這一場真的按過會 POST 的鈕（不然「零筆被擋」是空話）",
  `${REQS.filter(r => r.m !== "GET").length} 個非 GET`);
const after = await ctl("/f/where");
say(after.prod_flag_exists === false,
  "⛔⛔ 跑完之後真的 AUTO_ORDERS_ON 仍然**不存在**");

DONE = true;
console.log(`\n${"=".repeat(60)}\n共 ${N} 項，${FAIL ? FAIL + " 項未過" : "全部通過"}`);
c.close(); ch.kill();
process.exit(FAIL ? 1 : 0);
