#!/usr/bin/env node
/* ============================================================
   check-splash.js — 範本的自我體檢（零依賴，Node 就能跑）
   ------------------------------------------------------------
   用法（在專案根目錄）：
     node motion/check-splash.js
     node motion/check-splash.js --quick               只跳過全色域窮舉（~20 秒那段）
     node motion/check-splash.js public/index.html public/manifest.json

   它檢查八件會「今天對、下次悄悄歪掉」的事：
     1. 開場底色三處一致（manifest / CSS / SPLASH_CONFIG）＋ theme-color
     2. 載入順序、splash-boot.js 不可 defer、splash.js 不可留在 <head>、不可裸呼叫 Splash.*
     3. hold() 有沒有配 ready()（inline 與外部 JS 檔一起掃）
     4. 關鍵路徑 CSS：在不在、排在所有樣式表 <link> 之前、不可宣告 --splash-on-accent
     4b. 非阻塞 CSS 三件套：media="print"＋onload/onerror＋noscript fallback＋data-cssgate 閘門
     5. icon 檔案在不在
     6. 每一組「底色＋壓在上面的文字」對比 >= 4.5:1（淺色與深色各一遍）
     7. 開場符號字色 onColor：形狀、釘住點、全色域窮舉下界，**而且只准存在一份**

   ⭐ 為什麼這些要用程式檢查：它們壞掉的時候，你在電腦上看不出來，
      也不會有人回報——只會覺得「這個 app 有點怪」。
      而這是範本，每一個缺陷都會原封不動複製給每一支新 app。
   ============================================================ */
"use strict";

var fs = require("fs");
var path = require("path");

var ARGS = process.argv.slice(2).filter(function (a) { return a.indexOf("--") !== 0; });
var FLAGS = process.argv.slice(2).filter(function (a) { return a.indexOf("--") === 0; });
var QUICK = FLAGS.indexOf("--quick") >= 0;

var HERE = __dirname;
/* ROOT：先用 motion/ 的上一層；那裡找不到 index.html 就退回 cwd。
   （motion/ 允許被放進 public/，那時上一層不是專案根。） */
var ROOT = path.resolve(HERE, "..");

var errors = [];
var warns = [];
var infos = [];

function read(p) {
  try { return fs.readFileSync(p, "utf8"); } catch (e) { return null; }
}
function readOrFail(p, what) {
  var s = read(p);
  if (s === null) errors.push("檔案不存在或讀不到（" + what + "）：" + p);
  return s;
}
function grab(text, re) {
  if (!text) return null;
  var m = re.exec(text);
  return m ? m[1] : null;
}
function find(cands, bases) {
  for (var b = 0; b < bases.length; b++) {
    for (var i = 0; i < cands.length; i++) {
      var p = path.resolve(bases[b], cands[i]);
      if (fs.existsSync(p)) return p;
    }
  }
  return null;
}

var BASES = [ROOT, process.cwd(), HERE];
var htmlPath = ARGS[0]
  ? path.resolve(process.cwd(), ARGS[0])
  : find(["index.html", "public/index.html", "docs/index.html", "web/index.html"], BASES);
var manifestPath = ARGS[1]
  ? path.resolve(process.cwd(), ARGS[1])
  : find(["manifest.json", "public/manifest.json", "docs/manifest.json", "web/manifest.json"], BASES);

if (!htmlPath) errors.push("找不到 index.html（試過 " + BASES.join(" / ") + "；可用第一個參數指定）");
if (!manifestPath) errors.push("找不到 manifest.json（可用第二個參數指定）");

var html = htmlPath ? readOrFail(htmlPath, "index.html") : null;   /* 下面會被塗掉註解的版本覆蓋 */
/* ⭐ trade-log 的落地補丁（唯一跟 app-template/motion/check-splash.js 不同的地方）：
   ------------------------------------------------------------
   範本那份假設四支模組檔跟守衛住在同一個資料夾（motion/）。
   這支 App 的前端直接住在 repo 根目錄的 css/ 與 js/（沒有 build、沒有 public/），
   守衛放在 tools/ ⇒ 要往別的地方找。
   找法是「HERE 優先，再依序試 ROOT/css、ROOT/js」——
   ⚠️ 找不到就照舊 readOrFail 報錯，**不可以安靜跳過**（尺自己壞掉時要出聲）。
   ⚠️ 這個補丁沒有動任何判準，只是換檔案來源。範本改版時把新版複製過來、
      再把這一段重貼一次即可（PM 若同意，也可以把這段推回範本正本，行為完全相容）。 */
var MOD_DIRS = [HERE, path.join(ROOT, "css"), path.join(ROOT, "js")];
function modPath(name) {
  for (var i = 0; i < MOD_DIRS.length; i++) {
    var p = path.join(MOD_DIRS[i], name);
    if (fs.existsSync(p)) return p;
  }
  return path.join(MOD_DIRS[0], name);   /* 找不到 → 回第一個候選，讓 readOrFail 印出人看得懂的路徑 */
}
var splashCss = readOrFail(modPath("splash.css"), "splash.css");
var motionCss = readOrFail(modPath("motion.css"), "motion.css");
var splashJs = readOrFail(modPath("splash.js"), "splash.js");
var bootJs = readOrFail(modPath("splash-boot.js"), "splash-boot.js");

/* ⭐ trade-log 落地補丁①b：SHA 鎖鏈的**上游那一段**。
   ------------------------------------------------------------
   鎖鏈是：app-template/motion/xxx ──SHA──▶ 這個 repo 的副本 ──SHA──▶ index.html 的 inline 副本。
   第二段由下面第 2b 節守（範本本來就有）；**第一段**範本守不到（它以為自己就是正本），
   所以在這裡補。
   ⚠️ 正本住在這台機器的 ../app-template/motion，**不在這個 repo 裡**
      ⇒ 在雲端 session／別台機器上驗不到。那時要**出聲**（warn），不可以安靜放行。
   路徑可用環境變數 MOTION_TEMPLATE 覆寫。 */
(function () {
  var crypto = require("crypto");
  var tpl = process.env.MOTION_TEMPLATE || path.resolve(ROOT, "..", "app-template", "motion");
  var names = ["splash.css", "splash.js", "splash-boot.js"];
  if (!fs.existsSync(tpl)) {
    warns.push("SHA 鎖鏈的上游沒有驗到：找不到範本正本 " + tpl +
      "（在別台機器或雲端 session 上是正常的；要驗請設 MOTION_TEMPLATE 指到 app-template/motion）");
    return;
  }
  var lf = function (s) { return String(s).replace(/\r\n/g, "\n"); };
  var sum = function (s) { return crypto.createHash("sha256").update(lf(s), "utf8").digest("hex"); };
  var ok = 0;
  names.forEach(function (n) {
    var mine = read(modPath(n)), theirs = read(path.join(tpl, n));
    if (mine === null || theirs === null) {
      errors.push("SHA 鎖鏈：讀不到 " + n + "（本地或正本）");
      return;
    }
    if (sum(mine) !== sum(theirs)) {
      errors.push("SHA 鎖鏈分岔：" + n + " 跟 " + path.join(tpl, n) +
        " 不一致（本地 " + sum(mine).slice(0, 12) + "…／正本 " + sum(theirs).slice(0, 12) +
        "…）。這三支是範本複製品，要改請回範本改再複製過來");
    } else { ok++; }
  });
  /* 自證：拿一份一定不一樣的內容餵同一把尺，它必須說不一樣 */
  if (sum("a") === sum("b")) errors.push("SHA 鎖鏈的尺自己壞了（兩個不同字串算出同一個雜湊）");
  if (ok === names.length) infos.push("SHA 鎖鏈：3 支範本複製品跟 " + tpl + " 逐位元組相同 ✓（換行已正規化）");
})();

/* ⚠️ 掃「標籤」一律用這份，不要用 html 原文。
   HTML 註解裡常常會寫到 <link>／<script src="app.js">／<noscript> 當說明
   （這份範本的註解就是這樣寫的），掃描器會把它們當成真的標籤：
   2026-08-27 實際踩到——註解裡一句「<noscript> 那兩行是給…」讓「哪些 link 在 noscript 裡」
   整組算錯，守衛回報「一支樣式表都沒掃到」。
   做法是**把註解塗成空白但保留長度與換行**，這樣所有 indexOf/index 位置都還是對的。 */
function blankComments(s) {
  if (!s) return s;
  return s.replace(/<!--[\s\S]*?-->/g, function (m) {
    return m.replace(/[^\n]/g, " ");
  });
}
/* ⚠️ 同一個病的第二種形狀（2026-08-27 v1.6.1 踩到）：**CSS 註解**裡也會寫到
   「motion/splash.css」「data-splash-intro="light"」當說明，而 blankComments 只塗 HTML 註解
   ⇒ 「motion.css 有沒有排在 splash.css 前面」與「data-splash-intro 有沒有寫在 <html> 上」
   兩條守衛雙雙誤判（訊息長得像實作壞了，其實是尺壞了）。
   一樣塗成等長空白，所有 indexOf 的位置才不會歪。 */
function blankStyleComments(s) {
  if (!s) return s;
  var STYLE_BLK = new RegExp("<style[^>]*>[\\s\\S]*?<\\/style>", "gi");
  var CSS_CMT = new RegExp("\\/\\*[\\s\\S]*?\\*\\/", "g");
  return s.replace(STYLE_BLK, function (blk) {
    return blk.replace(CSS_CMT, function (m) { return m.replace(/[^\n]/g, " "); });
  });
}
var htmlRaw = html;
/* ⭐⭐ splash-boot 的 inline 柵欄（2026-08-27 第二版）。
   正本永遠是 motion/splash-boot.js；index.html 裡那一份是它的**逐字副本**，
   由 `node motion/inline-boot.js` 產生、由下面第 2b 節釘死不准分岔。
   ⚠️ 柵欄是 HTML 註解，所以要從 htmlRaw（沒有被塗白的那一份）撈。
   ⚠️ 這裡刻意**一個正則都不用**，全部 indexOf/slice：
      這一段是「守衛的守衛」，愈笨愈好，而且省掉一整類跳脫字元的坑。 */
var BOOT_BEGIN = "<!-- SPLASH-BOOT-INLINE:BEGIN";
var BOOT_END = "<!-- SPLASH-BOOT-INLINE:END -->";
var TAG_OPEN = "<script>";
var TAG_CLOSE = "</" + "script>";   /* 拆開寫：這支檔案自己也可能被貼進 HTML 裡 */
function inlineBootBlock(raw) {
  if (!raw) return null;
  var b = raw.indexOf(BOOT_BEGIN), e = raw.indexOf(BOOT_END);
  if (b < 0 || e < 0 || e < b) return null;
  var seg = raw.slice(b, e + BOOT_END.length);
  var s1 = seg.indexOf(TAG_OPEN), s2 = seg.indexOf(TAG_CLOSE);
  if (s1 < 0 || s2 < 0 || s2 < s1) return null;
  var body = seg.slice(s1 + TAG_OPEN.length, s2);
  if (body.charAt(0) === "\r") body = body.slice(1);
  if (body.charAt(0) === "\n") body = body.slice(1);
  return body;
}
/* 換行正規化 ＋ SHA-256（比對一律在 LF 空間做，理由見第 2b 節） */
function toLF(x) { return String(x).split("\r\n").join("\n"); }
function sha256(x) { return require("crypto").createHash("sha256").update(toLF(x)).digest("hex"); }

html = blankStyleComments(blankComments(html));
htmlRaw = blankStyleComments(htmlRaw);   /* 柵欄是 HTML 註解、仍然看得到；被塗掉的只有 <style> 裡的 CSS 註解 */
/* 尺自證：塗掉註解之後長度必須一樣（否則所有 index 都會歪掉），
   而且至少要塗掉一段（這份範本的註解很多，一段都沒塗到代表 regex 壞了）。 */
if (htmlRaw) {
  if (html.length !== htmlRaw.length) {
    errors.push("註解塗白改變了檔案長度（" + htmlRaw.length + " → " + html.length +
      "），後面所有位置比較都不可信");
  }
  if (html === htmlRaw) {
    warns.push("index.html 裡一段 HTML 註解都沒有（尺自證：確認一下 blankComments 有在運作）");
  }
}

/* ============================================================
   0. 開場變體：<html data-splash-intro="…">（2026-08-27 新增）
   ------------------------------------------------------------
   ⭐ 預設（沒有這個屬性）＝「印記」：第一次繪製就是 --splash-bg 的深色。
   ⭐ "light" ＝「白起」（motion/splash.css §7，opt-in）：
      第一次繪製是 --sp-start（iOS 淡出後留下的那片白），700ms 內沉成 --splash-bg。

   ⚠️ 這個開關會**整段改寫下面第 1 與第 3 節的判準**，所以要在最前面就決定，
      而且值打錯（"Light"、"white"、"lite"…）一律當錯誤 —— 打錯的話 CSS 與 JS
      兩邊都不會生效，畫面回到印記那一版，**而且不會有任何徵兆**。 */
var INTRO_ALLOWED = ["light"];
var introRaw = grab(htmlRaw, /<html\b[^>]*\bdata-splash-intro\s*=\s*["']([^"']*)["']/i);
var INTRO = introRaw === null ? "" : introRaw;
var LIGHT = INTRO === "light";
if (htmlRaw && /\bdata-splash-intro\b/.test(htmlRaw) && introRaw === null) {
  errors.push("index.html 裡有 data-splash-intro，但它不在 <html> 標籤上（或沒有加引號）。" +
    "這個屬性只有寫在 <html> 上才會在第一次繪製時生效");
}
if (INTRO && INTRO_ALLOWED.indexOf(INTRO) < 0) {
  errors.push('data-splash-intro="' + INTRO + '" 不是認得的值（只認：' + INTRO_ALLOWED.join(" / ") +
    "）。打錯的話 CSS 與 JS 都不會生效、畫面會靜靜地退回預設的印記開場");
}
infos.push("開場變體：" + (LIGHT ? '"light"（白起，opt-in）' : "（預設：印記）"));

/* 白起的起始色。正本在 motion/splash.css 的 --sp-start，app 可以在落地那塊蓋掉。 */
var MOD_START = grab(splashCss, /--sp-start\s*:\s*(#[0-9a-fA-F]{3,8})/);
var appStart = grab(html, /--sp-start\s*:\s*(#[0-9a-fA-F]{3,8})/);
var SP_START = appStart || MOD_START;
if (LIGHT && !SP_START) {
  errors.push("開了白起變體，但 motion/splash.css 裡找不到 --sp-start（漸變的起點色）");
}

/* ============================================================
   1. 開場底色：manifest / CSS / SPLASH_CONFIG / theme-color 四處一致
   ------------------------------------------------------------
   ⚠️⚠️ 契約在 2026-08-27 被重新定義過（白起變體推翻了舊的理由）：

     舊契約：「--splash-bg 必須等於 manifest.background_color，
              否則 iPhone 從主畫面開 app 會白閃一下。」
              —— 前提是「第一次繪製的底色 ＝ --splash-bg」。

     新契約：**開場「結束時」沉到的底色** ＝ --splash-bg ＝ manifest.background_color
              ＝ <meta theme-color> ＝ app 自己的 --bg。
              白起變體**刻意讓第一幀跟 manifest 不一樣**（第一幀是 --sp-start 的白，
              manifest 維持深色），因為它要接住的正是 iOS 淡出後留下的那片白。

   兩個契約要守的東西其實是同一件：**畫面上不可以有一個沒人設計過的顏色跳動**。
   舊的做法是「全部對齊成同一個色」，白起的做法是「把跳動變成一段設計過的漸變，
   而漸變的終點仍然對齊」。所以這一節照樣是硬界線，只是量的是**終點**不是**第一幀**。
   （第一幀由第 3 節負責，那裡才是這次真正被翻過來的地方。）
   ============================================================ */
var cssBg = grab(html, /--splash-bg\s*:\s*(#[0-9a-fA-F]{3,8})/);
var cfgBg = grab(html, /defaults\s*:\s*\{[\s\S]{0,400}?\bbg\s*:\s*["'](#[0-9a-fA-F]{3,8})["']/);
var metaTheme = grab(html, /<meta[^>]+name=["']theme-color["'][^>]+content=["'](#[0-9a-fA-F]{3,8})["']/);
var modBg = grab(splashCss, /--splash-bg\s*:\s*(#[0-9a-fA-F]{3,8})/);
var manBg = null, manTheme = null, manifest = null;
if (manifestPath) {
  var raw = read(manifestPath);
  if (raw === null) {
    errors.push("檔案不存在或讀不到（manifest.json）：" + manifestPath);
  } else {
    try {
      manifest = JSON.parse(raw);
      manBg = manifest.background_color || null;
      manTheme = manifest.theme_color || null;
    } catch (e) {
      errors.push("manifest.json 不是合法的 JSON：" + e.message);
    }
  }
}

if (html && !cssBg) errors.push("index.html 裡找不到 --splash-bg");
if (html && !cfgBg) errors.push("index.html 裡找不到 SPLASH_CONFIG.defaults.bg");
if (manifest && !manBg) errors.push("manifest.json 裡沒有 background_color");
if (html && !metaTheme) warns.push("index.html 沒有 <meta name=\"theme-color\">（狀態列會用瀏覽器預設色）");

if (cssBg && manBg && cssBg !== manBg) {
  errors.push("底色不一致：manifest.background_color = " + manBg +
    "，但 CSS 的 --splash-bg = " + cssBg +
    (LIGHT
      ? "（白起變體：--splash-bg 是開場**沉到最後**的底色，它必須等於 manifest，"
        + "否則開場結束的那一刻會再跳一次色）"
      : "（iPhone 從主畫面開 app 會白閃一下）"));
}
/* ⭐ 白起變體專屬：起點色**必須**跟 manifest 不一樣，否則整個變體等於沒開。
   這條看起來跟上面那條互相矛盾，其實不是：一條管終點、一條管起點。
   沒有這條的話，有人把 --sp-start 改成 #0b0d12「讓顏色統一」就會靜靜地把白起變回印記。 */
if (LIGHT && SP_START && manBg && SP_START.toLowerCase() === manBg.toLowerCase()) {
  errors.push("白起變體的起點色 --sp-start = " + SP_START + " 跟 manifest.background_color 一樣 ⇒ " +
    "根本沒有漸變，等於沒開這個變體（起點要是 iOS 淡出後留下的那片白，實測 #ebebeb）");
}
if (cssBg && cfgBg && cssBg !== cfgBg) {
  errors.push("底色不一致：CSS 的 --splash-bg = " + cssBg + "，但 SPLASH_CONFIG.defaults.bg = " + cfgBg);
}
if (metaTheme && manTheme && metaTheme !== manTheme) {
  errors.push("theme-color 不一致：<meta> 是 " + metaTheme + "，manifest.theme_color 是 " + manTheme);
}
if (manTheme && manBg && manTheme !== manBg) {
  warns.push("theme_color(" + manTheme + ") 跟 background_color(" + manBg + ") 不一樣，狀態列會有色差");
}
if (modBg && cssBg && modBg !== cssBg) {
  infos.push("motion/splash.css 的預設底色是 " + modBg + "，被 index.html 蓋成 " + cssBg + "（正常，模組保持原樣就好）");
}

/* ============================================================
   2. 載入順序 ／ script 屬性 ／ 裸呼叫 Splash.*
   ============================================================ */
if (html) {
  /* ⭐ trade-log 落地補丁②：範本假設路徑是 motion/xxx，這支 App 的檔案在 css/ 與 js/。
     改成「不管在哪個資料夾，只要有這支檔就算數」——**判準沒有放寬**
     （在不在、順序對不對、位置對不對，一條都沒少），只是不再寫死資料夾名。 */
  var iMotion = html.search(/href=["'][^"']*motion\.css["']/);
  var iSplashCss = html.search(/href=["'][^"']*splash\.css["']/);
  var iBootJs = htmlRaw ? htmlRaw.indexOf(BOOT_BEGIN) : -1;   /* 柵欄的位置＝inline boot 的位置 */
  var iSplashJs = html.search(/src=["'][^"']*splash\.js["']/);
  var iCfg = html.indexOf("window.SPLASH_CONFIG");
  var headEnd = html.indexOf("</head>");
  if (iMotion < 0) errors.push("index.html 沒有載入 motion.css");
  if (iSplashCss < 0) errors.push("index.html 沒有載入 splash.css");
  if (iBootJs < 0) errors.push("index.html 裡找不到 splash-boot 的 inline 柵欄（" + BOOT_BEGIN + " … " + BOOT_END + "）：第一幀的外觀就是它設的");
  if (iSplashJs < 0) errors.push("index.html 沒有載入 splash.js");
  if (iMotion >= 0 && iSplashCss >= 0 && iMotion > iSplashCss) {
    errors.push("motion.css 必須排在 splash.css 前面（token 在 motion.css，晚到會讓 --sp-out 變無效值）");
  }
  if (iCfg >= 0 && iBootJs >= 0 && iCfg > iBootJs) {
    errors.push("window.SPLASH_CONFIG 必須寫在 inline 的 splash-boot 之前（boot 一跑就會讀它）");
  }

  /* ⭐⭐ 2026-08-27（第二版）：splash-boot **不再是外部 script，而是 inline 在柵欄裡**。
     理由：第一次繪製之前必須到齊的同源請求數 2 → 1（index.html 一支就夠）。
     桌機量不出差別（Service Worker 派送幾乎免費），但 iPhone 上每一個經過 SW 的子資源
     都要付一次 WKWebView 的代價 —— 那正是這一版在賭的東西。
     所以這一節要守的東西整個換掉：不是「那個 <script src> 有沒有 defer」，
     而是「柵欄在不在、位置對不對、裡面那份跟正本有沒有分岔」。
     ⚠️ 外部 <script src=…splash-boot.js> 出現＝有人把 round trip 加回來了，要擋。 */
  if (/<script[^>]*src=["'][^"']*splash-boot\.js["'][^>]*>/.test(html)) {
    errors.push("splash-boot 又變回外部 <script src>：第一次繪製之前就多一個同源請求。" +
      "要 inline 在柵欄裡（跑 `node motion/inline-boot.js`）");
  }
  if (iBootJs >= 0 && headEnd >= 0 && iBootJs > headEnd) {
    errors.push("splash-boot 的 inline 柵欄必須放在 <head> 裡");
  }

  /* splash.js 反過來：它**不可以**再留在 <head>。
     它有 17KB 而第一次繪製一個位元組都用不到，留在 head 會擋住整場賽跑。 */
  var spTag = grab(html, /(<script[^>]*src="motion\/splash\.js"[^>]*>)/);
  if (spTag && headEnd >= 0 && html.indexOf(spTag) < headEnd) {
    errors.push("motion/splash.js 還留在 <head> 裡：拆檔之後它要搬到 <body> 尾端，" +
      "留在 head 等於第一次繪製還要等它下載完（2026-08-27 的整個重點）");
  }
  if (spTag && /\b(defer|async)\b/.test(spTag)) {
    errors.push("motion/splash.js 不可以加 defer／async：它必須排在 app 自己的程式之前，" +
      "app 一開頭就會呼叫 Splash.hold()。同步 script 一定跑在 defer 之前，defer 就不保證了");
  }
  if (iBootJs >= 0 && iSplashJs >= 0 && iBootJs > iSplashJs) {
    errors.push("splash-boot.js 必須排在 splash.js 之前（splash.js 靠 window.SplashBoot 才跑得起來）");
  }
}

/* ============================================================
   2b. index.html 裡 inline 的 splash-boot 不可以跟正本分岔
   ------------------------------------------------------------
   ⭐ 判準：**把換行正規化成 LF 之後，逐位元組相同**（SHA-256）。
      連註解都要一模一樣 —— 這是刻意的，理由有兩層：

      ① PM 原本建議「剝掉註解＋壓縮空白再比對」。那需要一個 JS 註解剝除器，
         而 splash-boot.js 裡有 "https://xd1104.github.io/…" 這種**字串裡的 //**，
         天真的剝除器會把它當成行註解、把後面整段吃掉。剝除器只要在**寬鬆的方向**
         出錯，守衛就會在「程式其實已經分岔」時放行 —— 尺壞了但一片綠，最糟的失敗模式。
      ② 不剝的話 index.html 多約 14KB，但那 14KB **本來就要傳**（原本是 splash-boot.js
         那個獨立請求），只是換個地方走；而且它在 SW 殼快取裡。
         ⇒ 逐位元組比對是**更嚴格**的守衛（連改一行註解都會紅），成本卻是零。

   ⚠️ 比對前一律把換行正規化成 LF：這台機器有的 repo 是 CRLF、範本是 LF，
      不正規化的話 git 重新簽出一次就會「紅在行尾」而不是「紅在程式分岔」。
   ⚠️ 這一節刻意不用正則（見上面 inlineBootBlock 的註解）。
   ============================================================ */
if (htmlRaw && bootJs) {
  var inlineBoot = inlineBootBlock(htmlRaw);
  if (inlineBoot === null) {
    errors.push("柵欄裡撈不到 " + TAG_OPEN + "…" + TAG_CLOSE + "（inline boot 的守衛整節等於沒跑）");
  } else {
    if (sha256(inlineBoot) !== sha256(bootJs)) {
      errors.push("index.html 裡 inline 的 splash-boot 跟正本 motion/splash-boot.js **已經分岔**" +
        "（LF 正規化後 SHA-256 不一致）。正本永遠是 motion/splash-boot.js；" +
        "跑一次 `node motion/inline-boot.js` 重貼，不要手改 index.html 裡那一段");
    } else {
      infos.push("inline 的 splash-boot 跟正本逐位元組相同（SHA-256 " +
        sha256(bootJs).slice(0, 12) + "…，" + toLF(inlineBoot).length + " 字元）");
    }
    /* 逐字貼進 script 的兩個致命字串：有的話 HTML 解析器會提早關掉 script，
       整段程式碼會變成畫面上的文字，而且**看起來只是「怪」，不會報錯**。 */
    if (inlineBoot.toLowerCase().indexOf(TAG_CLOSE.slice(0, 7)) >= 0) {
      errors.push("inline 的 splash-boot 裡出現 " + TAG_CLOSE.slice(0, 7) +
        "：HTML 解析器會提早關掉 script，程式碼會變成畫面上的文字");
    }
    if (inlineBoot.indexOf("<!--") >= 0 || inlineBoot.indexOf("-->") >= 0) {
      errors.push("inline 的 splash-boot 裡出現 <!-- 或 -->：會跟柵欄的註解打架");
    }
    /* onColor 只准有一份真相來源：index.html 裡出現的每一份，都必須就是柵欄裡的那一份。 */
    var nOnAll = htmlRaw.split("function onColor").length - 1;
    var nOnIn = inlineBoot.split("function onColor").length - 1;
    if (nOnIn !== 1) {
      errors.push("柵欄裡的 onColor 有 " + nOnIn + " 份（應該剛好 1 份）—— 這一節的尺壞了");
    }
    if (nOnAll !== nOnIn) {
      errors.push("index.html 裡有 " + nOnAll + " 份 onColor，但柵欄裡只有 " + nOnIn +
        " 份 ⇒ 有人在柵欄外面又寫了一份。鑰匙圈換色時一定分岔，而分岔的那一份沒有對比度下界");
    }
    /* 位置：柵欄要排在第一支樣式表 <link> 之前（第一次繪製之前就要把外觀變數寫好） */
    var iFirstLink = htmlRaw.indexOf("<link rel=\"stylesheet\"");
    if (iFirstLink >= 0 && iBootJs >= 0 && iBootJs > iFirstLink) {
      warns.push("inline 的 splash-boot 排在第一支樣式表 <link> 後面：先寫變數再拉樣式比較穩");
    }
  }
  /* 負控組：證明抽取器與比對真的會回 false，不是恆真 */
  var FAKE_OK = BOOT_BEGIN + " x -->\n" + TAG_OPEN + "\nvar a=1;\n" + TAG_CLOSE + "\n" + BOOT_END;
  var negA = inlineBootBlock(FAKE_OK) === "var a=1;\n";
  var negB = inlineBootBlock("<html>沒有柵欄</html>") === null;
  var negC = sha256("a") !== sha256("b");
  var negD = sha256("a\r\nb") === sha256("a\nb");
  var negE = ("x\nfunction onColor(bg){}\n".split("function onColor").length - 1) === 1;
  if (!(negA && negB && negC && negD && negE)) {
    errors.push("inline boot 守衛的負控組沒過（抽取器或比對是恆真的）：" +
      [negA ? "" : "抽不出內容", negB ? "" : "沒柵欄卻抽得出東西",
       negC ? "" : "不同內容 SHA 相同", negD ? "" : "CRLF/LF 沒有正規化",
       negE ? "" : "onColor 計數壞了"].filter(function (x) { return x; }).join("、"));
  } else {
    infos.push("inline boot 守衛負控組 5 條全過（抽得出、沒柵欄回 null、不同內容會不同、CRLF 已正規化、onColor 數得對）");
  }
}

/* ---- 把 app 的 JS 全部收集起來：inline ＋ <script src> 指到的本地檔 ----
   ⚠️ 只掃 index.html 的字串是不夠的：README §3 與 splash.html ④ 教的就是
      「你的 app JS 呼叫 hold()／ready()」，多數 app 會把它放在 app.js。
      守衛如果只看 inline，就會在「最該擋的形狀」上完全啞掉。 */
var jsBlobs = [];
if (html) {
  var inlineRe = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g, mi;
  while ((mi = inlineRe.exec(html))) {
    jsBlobs.push({ name: path.basename(htmlPath) + " (inline)", code: mi[1] });
  }
  var htmlDir = path.dirname(htmlPath);
  var srcRe = /<script[^>]*\bsrc=["']([^"']+)["'][^>]*>/g, ms;
  while ((ms = srcRe.exec(html))) {
    var src = ms[1];
    if (/^(https?:)?\/\//i.test(src) || src.indexOf("data:") === 0) {
      warns.push("index.html 載入了外部 script（" + src + "）：這一系列 app 的原則是零外部資源");
      continue;
    }
    var jp = path.resolve(htmlDir, src.split("?")[0]);
    var code = read(jp);
    if (code === null) {
      /* 掃不到就要明講，不可以靜靜地放行——那等於守衛不存在 */
      errors.push("index.html 引用的 JS 掃不到，守衛無法檢查：" + jp);
      continue;
    }
    jsBlobs.push({ name: src, code: code });
  }
  infos.push("守衛掃到 " + jsBlobs.length + " 份 JS：" + jsBlobs.map(function (b) { return b.name; }).join("、"));
  if (jsBlobs.length < 2) {
    warns.push("只掃到 " + jsBlobs.length + " 份 JS，確認一下是不是漏掃了（尺自證）");
  }
}

/* ⚠️ 比對前要先把註解剝掉：Splash.ready() 只要出現在註解裡（例如
   「忘了叫 Splash.ready()」這種提醒），守衛就會被騙過去而放行。
   這是自己做負控組時真的踩到的——突變測試沒紅，追下去發現是尺被註解騙了。
   ⚠️ 已知限制：這是字串比對，它擋得住「完全沒有 ready()」，
      擋不住「ready() 寫了但那條路徑走不到」。後者只有實跑才看得出來
      （check-live.js 會等 2.6 秒後量畫面，開場沒收掉那邊會看得到）。 */
function stripComments(js) {
  return js.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/(^|[^:])\/\/[^\n]*/g, "$1 ");
}
var allJs = jsBlobs.map(function (b) { return b.code; }).join("\n");
/* ⭐ trade-log 落地補丁③：範本用 "motion/splash.js" 認出「模組自己那一支」，
   這支 App 的模組在 js/splash.js。改成認檔名，不認資料夾。
   （模組自己的檔頭註解會寫 `Splash.hold()` 之類的 API 說明 ——
     那不是呼叫，是文件；不排除的話會冒出 4 條假錯誤。） */
function isSplashModule(name) { return /(^|[\\/])splash\.js$/.test(String(name)); }
var appJs = stripComments(jsBlobs.filter(function (b) { return !isSplashModule(b.name); })
                   .map(function (b) { return b.code; }).join("\n"));

if (appJs.indexOf("Splash.hold()") >= 0 && appJs.indexOf("Splash.ready()") < 0) {
  errors.push("有呼叫 Splash.hold() 卻沒有 Splash.ready()：開場會停到保險絲 6 秒才收");
}
/* 裸呼叫：一行的第一個 token 就是 Splash.xxx ⇒ splash.js 載不到時整段 app 會死 */
jsBlobs.forEach(function (b) {
  if (isSplashModule(b.name)) return;
  var lines = b.code.split("\n");
  for (var i = 0; i < lines.length; i++) {
    if (/^\s*Splash\.\w+\s*\(/.test(lines[i])) {
      errors.push(b.name + " 第 " + (i + 1) + " 行裸呼叫 " + lines[i].trim() +
        "：splash.js 載不到時會丟 ReferenceError、整段 app 中止。要寫成 window.Splash && ...");
    }
  }
});

/* ============================================================
   3. 關鍵路徑 CSS：第一次繪製必定是**開場設計好的那一幀**，不可以是瀏覽器預設的白
   ------------------------------------------------------------
   ⚠️⚠️ 契約變更（2026-08-27）：這一節的標題以前是「第一次繪製必定是**深色**」。
        白起變體把那句話整個反過來 —— 開了 data-splash-intro="light" 的 app，
        **第一次繪製必須是 --sp-start（#ebebeb 的白）**，深色反而是錯的。

        為什麼是「需求變了」不是「放寬」：舊契約真正在守的東西是
        「第一次繪製的顏色是**我們決定**的，不是外部 CSS 載不載得到決定的」。
        那件事一個字都沒有放掉 —— 只是「我們決定的顏色」現在有兩種可能，
        而**到底是哪一種，由 <html data-splash-intro> 這個開關決定，守衛照著它換判準**。
        所以下面每一條斷言都還在，而且兩種變體各自都會紅。

   iPhone 從主畫面開 PWA 的順序是：
     系統開場（manifest.background_color ＋ icon）→ 建 WKWebView
     → **這一段 WebView 是白的** → 頁面第一次繪製。
   我們控制得了的只有兩件事：①第一次繪製多快 ②第一次繪製是什麼顏色。

   ②本來完全靠外部檔案。外部 CSS 只要有一支沒到位（離線、SW 殼快取還沒建完、
   部署漏檔），第一次繪製就是純白。2026-08-26 在 movie-library 用本機真 Chrome
   （--headless=new ＋ CDP 逐幀取樣量首幀像素）實測：
     CSS 全部 404 時，沒有這一塊的首幀是 #ffffff、有這一塊是 #0b0d12；
     符號字色對比 1.4:1 → 6.9:1；first paint 沒有變慢（303ms vs 305ms，在雜訊內）。

   ⭐ 為什麼要用程式守：這一塊「今天在、明天有人整理 <head> 就沒了」，
      而它壞掉的時候你在電腦上完全看不出來——本機 CSS 永遠載得到。
   ============================================================ */
function noCssComment(s) { return String(s).replace(/\/\*[\s\S]*?\*\//g, " "); }
function nows(s) { return String(s).replace(/\s+/g, ""); }

/* 抽一條規則的內容（大括號配對；這幾條規則裡沒有巢狀，非貪婪就夠，但寫穩一點沒壞處）。
   ⚠️⚠️ 只認**裸的**那一條（選擇器前面是檔頭、`}` 或 `;`），不認「某個祖先底下的同名規則」。
   2026-08-27 實際踩到：白起變體加了 `html[data-splash-intro="light"] #splash{background:…}`
   之後，天真的 `#splash\s*\{` 會先撈到那一條（它只有一行 background）⇒ 守衛回報
   「#splash 沒有蓋滿、沒有 z-index、沒有底色」＝ **尺壞了，但錯誤訊息長得像實作壞了**。
   這一類（先抓到錯的那一條規則）跟 X15 的「屬性順序一換就繞過去」是同一個病。 */
function ruleBody(css, sel) {
  var re = new RegExp("(?:^|[};])\\s*" + sel.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\s*\\{([\\s\\S]*?)\\}");
  var m = re.exec(css);
  return m ? m[1] : null;
}
/* ⚠️ 下面這幾個「判準」全部抽成函式，是為了能在同一支程式裡用假輸入回頭驗它們
   （見本節最後的負控組）。判準寫成一次性的 inline 正則就沒辦法證明它不是恆真。 */
function critHtmlBg(css) {
  var m = /html[^{]*\{[^}]*background:\s*var\(\s*--splash-bg\s*,\s*(#[0-9a-fA-F]{3,8})\s*\)/.exec(css);
  return m ? m[1] : null;
}
function critSplashBg(body) {
  var m = /background:\s*var\(\s*--splash-bg\s*,\s*(#[0-9a-fA-F]{3,8})\s*\)/.exec(body || "");
  return m ? m[1] : null;
}
/* ⭐ 白起變體專用的兩把尺（判準跟上面那兩把是同一個形狀，只是換一個變數名與選擇器）。
   一律先 nows() 再比對：關鍵路徑塊是手寫的，換行與縮排隨時會變，但選擇器的 token 順序不會。 */
function critLightHtmlBg(css) {
  var m = /html\[data-splash-intro="light"\]:not\(\[data-splash="off"\]\)[^{}]*\{[^}]*background:var\(--sp-start,(#[0-9a-fA-F]{3,8})\)/.exec(nows(css));
  return m ? m[1] : null;
}
function critLightSplashBg(css) {
  var m = /html\[data-splash-intro="light"\]#splash[^{}]*\{[^}]*background:var\(--sp-start,(#[0-9a-fA-F]{3,8})\)/.exec(nows(css));
  return m ? m[1] : null;
}
/* ⭐⭐ v1.6.3：開場播放中 <body> 自己的底色要讓開（motion/splash.css §7a2）。
   回傳那一條規則的 background 值（沒有那條規則就回 null）。 */
function critLightBodyBg(css) {
  var m = /html\[data-splash-intro="light"\]:not\(\[data-splash="off"\]\)body\{background:([^;}]+)[;}]/.exec(nows(css));
  return m ? m[1] : null;
}
function critVarFallback(css, name) {
  var m = new RegExp("var\\(\\s*" + name + "\\s*,\\s*(#[0-9a-fA-F]{3,8})\\s*\\)").exec(css);
  return m ? m[1] : null;
}
function critContentFallback(css, name) {
  var m = new RegExp("content:\\s*var\\(\\s*" + name + '\\s*,\\s*"([^"]*)"\\s*\\)').exec(css);
  return m ? m[1] : null;
}
/* ⚠️ 判準是「不可以有**宣告**」，不是「這個名字不准出現」：
   關鍵路徑塊會用 var(--splash-on-accent) **引用**它（值仍然只有 onColor() 算得出來）。
   引用不會讓它變成可調的旋鈕，宣告才會。 */
function declaresOnAccent(css) { return /--splash-on-accent\s*:/.test(css); }
/* 「這個 class 有沒有自己的規則」：只認 `.cls{` 與 `.cls,`（選擇器串列），
   不認 `.cls::before{content:…}`——那只是把字放進去，位置與字級都還在外部 CSS 裡。 */
function hasBareRule(css, cls) {
  return new RegExp("\\." + cls + "\\s*[,{]").test(css);
}

var STYLE_BLOCKS = [];
var CRIT = null;
if (html) {
  var styleRe = /<style[^>]*>([\s\S]*?)<\/style>/g, msty;
  while ((msty = styleRe.exec(html))) STYLE_BLOCKS.push(msty[1]);

  /* 「所有 <link>」指的是**樣式表** <link>：manifest／icon／apple-touch-icon 那幾條
     不是 render-blocking，排在前面沒有影響（movie-library 的實測版本也是這個順序）。 */
  var cssLinks = [];
  var linkRe = /<link\b[^>]*>/g, mlk;
  while ((mlk = linkRe.exec(html))) {
    if (/rel\s*=\s*["']stylesheet["']/i.test(mlk[0])) cssLinks.push({ tag: mlk[0], at: mlk.index });
  }

  /* 尺自證：掃不到 <style> 或掃不到樣式表 <link>，這一整節的結論都不可信 */
  if (!STYLE_BLOCKS.length || !cssLinks.length) {
    errors.push("關鍵路徑檢查的尺壞了：掃到 " + STYLE_BLOCKS.length + " 塊 <style>、" +
      cssLinks.length + " 支樣式表 <link>（兩者都必須 >= 1）");
  } else {
    var iStyle = html.search(/<style[^>]*>/);
    var iFirstCssLink = cssLinks[0].at;

    /* ① 在不在 ＋ ② 是不是排在所有樣式表 <link> 之前 */
    if (iStyle > iFirstCssLink) {
      errors.push("沒有關鍵路徑 CSS：第一塊 <style> 排在樣式表 <link> 後面。" +
        "外部 CSS 一支沒到位（離線、SW 殼快取沒建完、部署漏檔）第一次繪製就是純白，" +
        "符號字色對比只剩 1.4:1。要在所有樣式表 <link> 之前內嵌一塊（見 motion/README.md §1 ①）");
    } else {
      CRIT = noCssComment(STYLE_BLOCKS[0]);
      infos.push("關鍵路徑 CSS 在第 1 塊 <style>（共 " + STYLE_BLOCKS.length + " 塊），" +
        "排在 " + cssLinks.length + " 支樣式表 <link> 之前 ⇒ 這一節有真的跑");
      if (CRIT.indexOf("#splash") < 0) {
        errors.push("排在 <link> 之前的那塊 <style> 裡沒有 #splash 規則：" +
          "它不是關鍵路徑 CSS（關鍵路徑塊要負責畫出開場的第一幀）");
        CRIT = null;
      }
    }
  }
}

if (CRIT) {
  /* ③ html 底色：一定要寫在 html 上。body 的背景在 overscroll 時救不了，
        而且外部 CSS 沒到的時候 body 根本還沒有樣式。
        ⚠️ 白起變體下這一條**還是要在**：它管的是「熱啟動（不播開場）」與「CSS 閘門關著」
           那兩段的底色，那兩段本來就該是 app 自己的深色，不是開場的白。
           白起只改「冷啟動、開場播放中」那一段（下面的 ③b）。 */
  var htmlBg = critHtmlBg(CRIT);
  if (!htmlBg) {
    errors.push("關鍵路徑塊裡沒有 html 的底色（要寫成 html…{background:var(--splash-bg,#後備色);}）");
  } else if (manBg && htmlBg !== manBg) {
    errors.push("關鍵路徑塊的後備底色是 " + htmlBg + "，但 manifest.background_color 是 " + manBg +
      "（不一致＝白閃換成色差，一樣看得出來）");
  }

  /* ③b ⭐ 白起變體：開場播放中的第一幀底色必須是 --sp-start，而且**只能靠這一塊**。
         漸變本體住在 motion/splash.css（非阻塞），所以 CSS 全掛的時候只剩這一塊在畫第一幀 ——
         那正是這一條要守的情況。沒有它的話，開了 light 的 app 在 CSS 掛掉時第一幀會是
         深色（來自 ③），跟 splash.css 到位時的白差一整個顏色。 */
  if (LIGHT) {
    var lightHtmlBg = critLightHtmlBg(CRIT);
    if (!lightHtmlBg) {
      errors.push('開了 data-splash-intro="light"，但關鍵路徑塊裡沒有對應的 html 底色覆寫。' +
        '要寫成 html[data-splash-intro="light"]:not([data-splash="off"]){background:var(--sp-start,#後備色);}' +
        "（少了它 ⇒ 外部 CSS 掛掉時第一幀是深色，跟正常情況差一整個顏色）");
    } else if (SP_START && lightHtmlBg !== SP_START) {
      errors.push("關鍵路徑塊的白起後備色是 " + lightHtmlBg + "，但 --sp-start 是 " + SP_START +
        "（兩者不一致＝同一條規則活在兩個地方，CSS 到位的前後會跳一次色）");
    }
    var lightSpBg = critLightSplashBg(CRIT);
    if (!lightSpBg) {
      errors.push('開了 data-splash-intro="light"，但關鍵路徑塊裡的 #splash 沒有白起的底色覆寫。' +
        '要寫成 html[data-splash-intro="light"] #splash{background:var(--sp-start,#後備色);}');
    } else if (SP_START && lightSpBg !== SP_START) {
      errors.push("關鍵路徑塊 #splash 的白起後備色是 " + lightSpBg + "，但 --sp-start 是 " + SP_START);
    }
    /* ③c ⭐⭐ v1.6.3：**<body> 自己的底色**在開場播放中要讓開。
           app 的 `body{background:var(--bg)}` 畫在 html 畫布**上面** ⇒ ③b 讓 html 沉下去了，
           body 那一片深色照樣蓋在上面。只要有一塊畫面沒被 #splash 蓋到（iOS 初始化時
           safe-area／fixed 容器還沒定案的那一兩格），使用者看到的就是那片**沒有動過的深色**。
           movie-library 2026-08-28 的螢幕錄影：下緣 58px 一條純色 #0b0d12，兩格後恢復。
       ⚠️ 判準要求 `transparent`（讓開），不接受「body 也跑一次同樣的漸變」——
          那是同一條時間線活在兩個地方，改 token 時必分岔。
       ⚠️ 兩份實作都要有：這裡查關鍵路徑塊，下面查 motion/splash.css。 */
    var lightBodyBg = critLightBodyBg(CRIT);
    if (!lightBodyBg) {
      errors.push('開了 data-splash-intro="light"，但關鍵路徑塊裡沒有「開場播放中 body 底色讓開」那一條。' +
        '要寫成 html[data-splash-intro="light"]:not([data-splash="off"]) body{background:transparent;}' +
        "（少了它 ⇒ 任何沒被 #splash 蓋到的畫面會露出 app 的深色底，而不是正在沉的開場底色）");
    } else if (lightBodyBg !== "transparent") {
      errors.push("關鍵路徑塊的 body 讓開那一條寫的是 " + lightBodyBg + "，必須是 transparent" +
        "（讓開之後畫布只剩一條時間線；自己再跑一次漸變＝同一條規則活在兩個地方）");
    }
    if (splashCss && critLightBodyBg(splashCss) !== "transparent") {
      errors.push("motion/splash.css §7a2 少了（或寫錯）「開場播放中 body 底色讓開」那一條 —— " +
        "規則活在兩份實作裡（模組 ＋ 關鍵路徑塊），只改一邊等於沒改");
    }
    /* 漸變本體必須真的存在於模組裡（不是只有第一幀是白的、然後永遠停在白色）。
       ⚠️ 判準一定要寫成 `sp-sink\s*\{`，**不可以**用 `sp-sink\b` ——
          `\b` 在 `sp-sink-bg` 的那個連字號上也成立 ⇒ 把 sp-sink 整個改名，
          守衛會被 sp-sink-bg 餵飽而放行（2026-08-27 自己拿突變打出來的，改之前真的是綠的）。
       三條 keyframes 缺一不可：底色（html）、漸深（::before）、符號浮出。 */
    ["sp-sink", "sp-sink-bg", "sp-emerge"].forEach(function (kf) {
      if (splashCss && !new RegExp("@keyframes\\s+" + kf + "\\s*\\{").test(splashCss)) {
        errors.push("motion/splash.css 裡找不到 @keyframes " + kf +
          "：白起變體的動作會缺一段（可能只是停在白色不會沉下去）");
      }
    });
    /* ⭐ 減少動態：白起要整個關掉（沒有漸變的話，白只剩「多跳一次」）。
       這條規則活在兩份實作裡（模組 ＋ 關鍵路徑塊），所以兩邊都要檢查 ——
       「只改了一邊」是這個範本出過的真實事故。 */
    var critNows = nows(CRIT);
    if (critNows.indexOf("@media(prefers-reduced-motion:reduce)") < 0 ||
        !/@media\(prefers-reduced-motion:reduce\)\{[^]*?html\[data-splash-intro="light"\][^]*?background:var\(--splash-bg,/.test(critNows)) {
      errors.push("關鍵路徑塊少了「減少動態時白起退回深色」的覆寫。" +
        "reduce 之下 --dur-* 全部塌成 1ms ⇒ 沒有漸變可言，留著白只是多跳一次（白一下下 → 深）");
    }
    if (splashCss && !/@media\s*\(prefers-reduced-motion:\s*reduce\)[^]*html\[data-splash-intro="light"\]/.test(splashCss)) {
      errors.push("motion/splash.css 的 reduce 區塊沒有把白起關掉（規則活在兩份實作裡，只改一邊等於沒改）");
    }
    if (splashJs && !/data-splash-intro/.test(splashJs)) {
      errors.push("motion/splash.js 沒有讀 data-splash-intro ⇒ 最短顯示還是印記那一版的長度，" +
        "白起的名字（delay 700ms）會來不及演完就被收掉");
    }
    /* ⭐⭐ v1.6.2：開場**不可以在畫面真的被交到使用者眼前之前就演掉一段**。
       ------------------------------------------------------------
       iOS 從主畫面開 PWA 時，它自己的啟動畫面**還在淡出**，而我們的 WKWebView
       已經在後面繪製、CSS 已經套用、動畫已經開始跑。等它把畫面交出來，
       開場已經演掉一段 ⇒ 使用者看到的第一幀不是白的，是漸深走到一半的灰。
       movie-library 的螢幕錄影逐格（59.94fps，兩次開啟各量一次）量到那個盲窗約 273ms
       （交棒那一格是 #949494 ＝ #ebebeb → #0b0d12 走到 39%）。
       ⇒ 白起變體的每一條進場動畫都必須有 delay ≥ 盲窗，用 --sp-lead 統一給。
       ⛔ **不可以改用「把漸深變慢」來掩蓋**：放慢只是把台階變小，台階還在。
          所以這裡量的是**延遲**不是時長。 */
    var BLIND_MS = 273;
    var timeTok = {};
    [motionCss, splashCss].forEach(function (src) {
      if (!src) return;
      var clean = src.replace(/\/\*[\s\S]*?\*\//g, "");
      var re = /(--[a-z0-9-]+)\s*:\s*([^;{}]+);/gi, m;
      /* ⚠️ 先出現的贏：motion.css 最後面的 reduce 區塊會把 --dur-* 全部覆寫成 1ms，
         後者贏的話會量到「--sp-lead ＝ 2ms」然後對著正確的實作報錯（尺壞了）。 */
      while ((m = re.exec(clean))) { if (!(m[1] in timeTok)) timeTok[m[1]] = m[2].trim(); }
    });
    var toMs = function (v) {
      var m = /^(-?\d+(?:\.\d+)?)(ms|s)$/.exec(String(v).trim());
      return m ? Number(m[1]) * (m[2] === "s" ? 1000 : 1) : null;
    };
    var resolveTime = function (expr) {
      var s = String(expr), i;
      for (i = 0; i < 12 && s.indexOf("var(") >= 0; i++) {
        s = s.replace(/var\(\s*(--[a-z0-9-]+)\s*(?:,[^()]*)?\)/gi, function (all, n) {
          return (n in timeTok) ? timeTok[n] : all;
        });
      }
      for (i = 0; i < 8 && /calc\(/i.test(s); i++) {
        s = s.replace(/calc\(([^()]*)\)/i, function (all, inner) {
          var parts = inner.split(/([+-])/).map(function (x) { return x.trim(); })
            .filter(function (x) { return x !== ""; });
          var acc = toMs(parts[0]);
          if (acc === null) return "NaNms";
          for (var k = 1; k < parts.length; k += 2) {
            var v = toMs(parts[k + 1]);
            if (v === null) return "NaNms";
            acc = parts[k] === "-" ? acc - v : acc + v;
          }
          return acc + "ms";
        });
      }
      return s;
    };
    /* animation 簡寫：第一個 <time> 是時長、第二個是延遲（fill-mode／timing 擺哪裡都不影響） */
    var delayOfAnim = function (shorthand) {
      var s = resolveTime(shorthand), out = [], re = /(-?\d+(?:\.\d+)?)(ms|s)\b/g, m;
      while ((m = re.exec(s))) out.push(Number(m[1]) * (m[2] === "s" ? 1000 : 1));
      return out.length >= 2 ? out[1] : (out.length === 1 ? 0 : null);
    };
    /* 走訪葉節點規則（@media 會被攤平）。刻意不用 global 正則掃描 ——
       那會把前一條規則的 `}` 一起吃掉，下一條就失去錨點而被**安靜跳過**。 */
    var leafRules = function (css) {
      var out = [];
      (function walk(start, end) {
        var sel = "", i = start;
        while (i < end) {
          var c = css.charAt(i);
          if (c === "{") {
            var depth = 1, j = i + 1;
            while (j < end && depth > 0) {
              if (css.charAt(j) === "{") depth++;
              else if (css.charAt(j) === "}") depth--;
              j++;
            }
            var body = css.slice(i + 1, j - 1);
            if (body.indexOf("{") < 0) out.push({ sel: sel.replace(/\s+/g, " ").trim(), body: body });
            else walk(i + 1, j - 1);
            sel = ""; i = j;
          } else if (c === "}") { sel = ""; i++; }
          else { sel += c; i++; }
        }
      })(0, css.length);
      return out;
    };
    if (splashCss) {
      var lightAnims = leafRules(splashCss.replace(/\/\*[\s\S]*?\*\//g, "")).filter(function (r) {
        if (r.sel.indexOf('data-splash-intro="light"') < 0) return false;
        var a = /(?:^|[;{])\s*animation\s*:\s*([^;}]+)/.exec(r.body);
        if (!a) return false;
        r.anim = a[1].trim();
        return !/^none$/i.test(r.anim);      /* 明確關掉的（光環、reduce）不可能推進 */
      });
      /* 自證：掃不到就是尺壞了，不可以靜靜放行 */
      if (lightAnims.length < 3) {
        errors.push("[尺壞了] 白起變體只掃到 " + lightAnims.length +
          " 條會動的動畫（至少該有底色、漸深層、符號三條）⇒ 這一節的檢查等於沒跑");
      }
      lightAnims.forEach(function (r) {
        var d = delayOfAnim(r.anim);
        if (d === null || d < BLIND_MS) {
          errors.push("白起變體的「" + r.sel + "」delay 是 " + d + "ms，小於 iOS 交出畫面前的盲窗 " +
            BLIND_MS + "ms ⇒ 使用者看到的第一幀已經不是起始狀態（會看到漸深走到一半的灰）。" +
            "解法是給它 var(--sp-lead)，不是把動畫放慢");
        }
      });
      /* 負控組：判準必須真的會回 false，不然它只是裝飾品（這份守衛的老毛病） */
      var leadNeg = [
        /* ⚠️ 這幾條自證刻意用**字面值**不用 var(--sp-lead)：用真 token 的話，
           token 一旦被改短，這裡會跟著喊「尺壞了」而蓋過真正的錯誤訊息。 */
        [delayOfAnim("sp-sink 700ms linear 340ms forwards") === 340,
          "有寫 delay 的簡寫要讀得到那個值"],
        [delayOfAnim("sp-sink var(--sp-sink) linear forwards") === 0,
          "沒寫 delay 的簡寫要算成 0（＝不合格），不可以算成「沒有限制」"],
        [delayOfAnim("sp-up var(--dur-3) var(--ease) both calc(var(--sp-lead) + var(--sp-sink))") ===
          toMs(resolveTime("var(--sp-lead)")) + toMs(resolveTime("var(--sp-sink)")),
          "delay 擺在 fill-mode 後面、而且是巢狀 calc 也要算得對（跟 token 一起浮動，量的是解析器不是數值）"],
        [toMs(resolveTime("var(--dur-press)")) === 120,
          "token 要讀到一般情況的值，不是 reduce 區塊覆寫的 1ms"],
        [leafRules("a{x:1}@media(q){b{y:2}}").map(function (r) { return r.sel; }).join(",") === "a,b",
          "走訪器要攤得平 @media，而且不會把前一條的 } 吃掉害下一條被跳過"]
      ];
      var leadBad = leadNeg.filter(function (p) { return !p[0]; }).map(function (p) { return p[1]; });
      if (leadBad.length) {
        errors.push("[尺壞了] 盲窗守衛的負控組沒過：" + leadBad.join("；"));
      } else {
        infos.push("白起的「起跑前那一拍」：" + lightAnims.length + " 條動畫全部 delay ≥ " +
          BLIND_MS + "ms（--sp-lead 實測 " + toMs(resolveTime("var(--sp-lead)")) +
          "ms）；判準負控組 " + leadNeg.length + " 條全過");
      }
    }
  } else if (/data-splash-intro/.test(nows(CRIT))) {
    warns.push("沒有開白起變體，關鍵路徑塊裡卻有 data-splash-intro 的規則（死碼，或是有人漏了 <html> 上那個屬性）");
  }

  /* ④ #splash 一出現就要蓋滿，而且自己有底色（不靠繼承） */
  var spBody = ruleBody(CRIT, "#splash");
  if (!spBody) {
    errors.push("關鍵路徑塊裡沒有 #splash{...} 規則");
  } else {
    var sp = nows(spBody);
    if (!/position:fixed/.test(sp) || !/inset:0/.test(sp)) {
      errors.push("關鍵路徑塊的 #splash 沒有蓋滿（要 position:fixed ＋ inset:0），" +
        "外部 CSS 沒到時開場那一層蓋不住底下的 app");
    }
    if (!/z-index:200/.test(sp)) {
      errors.push("關鍵路徑塊的 #splash 沒有 z-index:200（要跟 motion/splash.css 一致，否則會被 app 蓋掉）");
    }
    var spBg = critSplashBg(spBody);
    if (!spBg) {
      errors.push("關鍵路徑塊的 #splash 沒有自己的底色（要 background:var(--splash-bg,#後備色)，不可以只靠繼承）");
    } else if (manBg && spBg !== manBg) {
      errors.push("關鍵路徑塊 #splash 的後備底色是 " + spBg + "，但 manifest.background_color 是 " + manBg);
    }
  }

  /* ⑤ 不可以再拉任何外部資源，否則等於沒有脫離關鍵路徑 */
  if (/@import/.test(CRIT) || /url\(/.test(CRIT)) {
    errors.push("關鍵路徑塊裡有 @import 或 url()：它必須完全不依賴網路，否則等於沒有脫離關鍵路徑");
  }

  /* ⑥ 全掃描：#splash 標記裡用到的每一個 sp-* class，關鍵路徑塊都要畫得出來。
        不是列白名單——白名單只涵蓋你想得到的那幾個。 */
  var spClasses = [];
  var clsRe = /class="(sp-[a-z-]+)"/g, mcl;
  while ((mcl = clsRe.exec(html))) { if (spClasses.indexOf(mcl[1]) < 0) spClasses.push(mcl[1]); }
  if (spClasses.length < 5) {
    errors.push("關鍵路徑檢查的尺壞了：#splash 標記裡只掃到 " + spClasses.length +
      " 個 sp-* class（應該至少 5 個：" + spClasses.join("、") + "）");
  }
  /* 豁免：.sp-ring 是第二拍的光環，靜態 opacity 本來就是 0，第一幀它只是一個看不見的空 div。
     豁免名單長度要斷言，擋人偷加。 */
  var EXEMPT = ["sp-ring"];
  if (EXEMPT.length !== 1) errors.push("關鍵路徑的豁免名單被改過（現在有 " + EXEMPT.length + " 個）：要加請先想清楚為什麼");
  spClasses.forEach(function (c) {
    if (EXEMPT.indexOf(c) >= 0) {
      if (new RegExp("\\." + c + "\\s*[,{]").test(CRIT)) {
        infos.push("." + c + " 出現在關鍵路徑塊裡（原本刻意不放：第一幀它是不可見的）");
      }
      return;
    }
    /* ⚠️ 判準刻意**不接受** `.sp-name::before{...}` 這種只有 content 的規則：
       它只把字放進去，位置、字級、字重全都還在外部 CSS 裡。
       （寫成 [,{:] 的話 X11「.sp-name{...} 整條刪掉」會逃掉——實際踩過。） */
    if (!hasBareRule(CRIT, c)) {
      errors.push("." + c + " 在關鍵路徑塊裡沒有規則：外部 CSS 全掛時這一塊畫不出完整的第一幀" +
        "（只有 ::before 的 content 不算——那沒有位置與字級）");
    }
  });

  /* ⑦ 符號字色：只准引用 onColor() 算出來的值，不可以在這裡寫死色碼 */
  var glBody = ruleBody(CRIT, ".sp-glyph");
  if (!glBody) {
    errors.push("關鍵路徑塊裡沒有 .sp-glyph{...} 規則");
  } else {
    var gl = nows(glBody);
    if (!/color:var\(--splash-on-accent\)/.test(gl)) {
      errors.push(".sp-glyph 的字色要寫成 color:var(--splash-on-accent)：" +
        "值由 splash-boot.js 的 onColor() 算（有對比度下界、會跟著鑰匙圈換色）");
    }
    if (/(^|;)color:#[0-9a-fA-F]{3,8}/.test(gl)) {
      errors.push("關鍵路徑塊的 .sp-glyph 寫死了字色色碼：同一條規則活在兩個地方，" +
        "鑰匙圈換色時一定分岔，而且分岔的那一份沒有對比度下界");
    }
  }

  /* ⑧ 後備字面值要是**你自己的品牌**，不是範本的。
        比對對象是 SPLASH_CONFIG.defaults（那是這支 app 自己宣告的身分），
        所以這條在每一支落地的 app 上都成立，不是只驗範本。 */
  var cfgBlock = grab(html, /defaults\s*:\s*\{([\s\S]{0,400}?)\}/);
  var cfgOf = function (k) {
    var m = new RegExp("\\b" + k + "\\s*:\\s*[\"']([^\"']*)[\"']").exec(cfgBlock || "");
    return m ? m[1] : null;
  };
  [["--splash-accent", "accent", "符號方塊底色"], ["--splash-ink", "ink", "開場文字色"]].forEach(function (p) {
    var fb = critVarFallback(CRIT, p[0]);
    var want = cfgOf(p[1]);
    if (!fb) { errors.push("關鍵路徑塊沒有 " + p[0] + " 的後備色（" + p[2] + "）"); return; }
    if (want && fb.toLowerCase() !== want.toLowerCase()) {
      errors.push("關鍵路徑塊的 " + p[0] + " 後備色是 " + fb + "，但 SPLASH_CONFIG.defaults." + p[1] +
        " 是 " + want + "（外部 CSS 掛掉時會看到不是自己的顏色）");
    }
  });
  [["--splash-glyph", "glyph", "符號"], ["--splash-name", "name", "app 名字"]].forEach(function (p) {
    var fb = critContentFallback(CRIT, p[0]);
    var want = cfgOf(p[1]);
    if (fb === null) { errors.push("關鍵路徑塊沒有 " + p[0] + " 的後備文字（" + p[2] + "）"); return; }
    if (want !== null && fb !== want) {
      errors.push("關鍵路徑塊的 " + p[2] + "後備值是「" + fb + "」，但 SPLASH_CONFIG.defaults." + p[1] +
        " 是「" + want + "」（外部 CSS 掛掉時畫面上會出現別人家的品牌）");
    }
  });
}

/* ⑨ --splash-on-accent 不可以被宣告——掃 index.html 的**每一塊** <style>，不是只看第一塊。
      （關鍵路徑塊與落地塊都要掃；模組自己的 motion/splash.css 有預設宣告，那是模組的事，不在這裡管。） */
if (html && STYLE_BLOCKS.length) {
  var allStyle = noCssComment(STYLE_BLOCKS.join("\n"));
  if (declaresOnAccent(allStyle)) {
    errors.push("index.html 的 <style> 宣告了 --splash-on-accent：它不是設定項，" +
      "值由 splash-boot.js 的 onColor() 算（白字與深字取對比高的那個，最差有下界）。" +
      "宣告它＝多一個可以「調成看不見」的旋鈕，而且會跟 onColor() 分岔。只能引用，不能宣告");
  }
}

/* ---- 負控組：證明上面那些判準真的會回 false，不是恆真 ----
   ⭐ 自己做突變測試時真的踩過：判準寫太鬆的話，把關鍵路徑塊整個刪掉守衛也不會紅。
      所以這裡拿「一定不合格」的假輸入回頭驗每一個判準；有任何一個放行，
      代表那條守衛是裝飾品，要當成錯誤講出來。 */
(function negativeControls() {
  var FAKE_NO_HTML_BG = "#splash{color:red;}";
  var FAKE_HARDCODED = "html{background:#241f1b;} #splash{background:#241f1b;}";
  var neg = [
    [critHtmlBg(FAKE_NO_HTML_BG) === null, "沒有 html 背景的 CSS 必須判為不合格"],
    [critHtmlBg(FAKE_HARDCODED) === null, "html 背景寫死色碼（沒走 var 後備）必須判為不合格"],
    [critSplashBg("background:#241f1b;") === null, "#splash 底色寫死色碼必須判為不合格"],
    [critVarFallback("background:var(--splash-accent);", "--splash-accent") === null, "var() 沒帶後備值必須判為不合格"],
    [critContentFallback('content:var(--splash-name);', "--splash-name") === null, "content var() 沒帶後備值必須判為不合格"],
    [ruleBody(FAKE_NO_HTML_BG, ".sp-glyph") === null, "沒有 .sp-glyph 的 CSS 必須抽不出規則"],
    [ruleBody('html[data-x="y"] #splash{background:red;}', "#splash") === null,
      "「祖先選擇器底下的 #splash」不可以被當成裸的 #splash 規則（2026-08-27 踩到的尺壞掉）"],
    [ruleBody('html[data-x="y"] #splash{background:red;}\n#splash{position:fixed;}', "#splash") === "position:fixed;",
      "有覆寫規則在前面時，仍然要抽到裸的那一條（證明這把尺不是恆 null ＝ 恆紅）"],
    [hasBareRule('.sp-name::before{content:var(--splash-name,"x");}', "sp-name") === false,
      "只有 ::before 的 content 不算「這個 class 有規則」"],
    [hasBareRule(".sp-name{font-size:20px;}", "sp-name") === true, "真的有 .sp-name{...} 時必須算數"],
    [declaresOnAccent("--splash-on-accent:#fff;") === true, "宣告 --splash-on-accent 必須被抓到"],
    [declaresOnAccent("color:var(--splash-on-accent);") === false, "只是引用 --splash-on-accent 不可以被誤判成宣告"],
    [noCssComment("/* --splash-on-accent:#fff; */x").indexOf("--splash-on-accent") < 0, "註解要先被剝掉，否則守衛會被註解騙過去"],
    /* 白起變體的兩把新尺（不管有沒有開變體都跑，這樣「尺壞掉」不會等到某支 app 開了才發現） */
    [critLightHtmlBg('html:not([data-splash="off"]){background:var(--splash-bg,#0b0d12);}') === null,
      "只有預設那條 html 底色時，白起的判準必須回 null（否則它會把預設誤判成已經支援白起）"],
    [critLightHtmlBg('html[data-splash-intro="light"]:not([data-splash="off"]){background:var(--splash-bg,#0b0d12);}') === null,
      "白起的選擇器有了、但值還是 --splash-bg（等於沒有白）必須判為不合格"],
    [critLightHtmlBg('html[data-splash-intro="light"]:not([data-splash="off"]){background:var(--sp-start,#ebebeb);}') === "#ebebeb",
      "正確寫法必須撈得到後備色（證明這把尺不是恆 null ＝ 恆綠）"],
    [critLightSplashBg('html[data-splash-intro="light"] #splash{background:var(--sp-start,#ebebeb);}') === "#ebebeb",
      "#splash 的白起覆寫也要撈得到"],
    [critLightSplashBg('#splash{background:var(--sp-start,#ebebeb);}') === null,
      "沒有 data-splash-intro 前綴的 #splash 規則不算白起覆寫（那會把所有 app 都變白）"],
    /* v1.6.3：body 讓開那一把尺 */
    [critLightBodyBg('html[data-splash-intro="light"]:not([data-splash="off"]) body{background:transparent;}') === "transparent",
      "正確寫法必須撈得到 transparent（證明這把尺不是恆 null ＝ 恆綠）"],
    [critLightBodyBg('html[data-splash-intro="light"]:not([data-splash="off"]){background:var(--sp-start,#ebebeb);}') === null,
      "只有 <html> 那一條時必須回 null —— body 的方框畫在畫布上面，html 沉了它照樣是深的"],
    [critLightBodyBg("body{background:transparent;}") === null,
      "沒有前綴的 body{background:transparent} 不算數（那會讓熱啟動與收場之後也透明）"],
    [critLightBodyBg('html[data-splash-intro="light"]:not([data-splash="off"]) body{background:var(--sp-start);}') === "var(--sp-start)",
      "「body 自己再跑一次漸變」要撈得到值，才判得出它不是 transparent（同一條時間線活在兩個地方）"]
  ];
  var bad = neg.filter(function (n) { return !n[0]; });
  if (bad.length) {
    bad.forEach(function (n) { errors.push("關鍵路徑守衛的負控組沒過（判準是恆真的、擋不到東西）：" + n[1]); });
  } else {
    infos.push("關鍵路徑守衛負控組 " + neg.length + " 條全過（判準會回 false，不是裝飾品）");
  }
})();

/* ============================================================
   3c. ⭐⭐ 第一次繪製的那一幀 ＝ 動畫的**起始狀態**（2026-08-27，v1.6.1）
   ------------------------------------------------------------
   Benson 回報開場「有點小奇怪」，螢幕錄影逐格（59.94fps）拆出來是：
     畫格 84–88  金色符號**實心**、名字已經看得到（淺色字壓在淺灰底上像鬼影）
     畫格 89     符號**突然變半透明、名字整個消失**    ← motion/splash.css 在這一格被套用
     畫格 91→    符號慢慢變回實心、名字之後才正常淡入
   ⇒ 實心 →（跳）淡掉 → 再淡回來。

   根因：樣式表是**非阻塞**的（media="print" onload），所以順序一定是
     第一次繪製（關鍵路徑 inline CSS 畫的）→ 幾十毫秒～數百毫秒 → splash.css 套用 → 動畫從頭跑。
   關鍵路徑塊以前畫的是「完成態」，而動畫的起始狀態是「還沒出現」⇒ 中間必然有一次跳動。
   深色底時代它也在（名字早就是 1 → 0 → 1），深底＋深色字看不出來；白起把底色變亮就全現形。

   ⇒ 硬界線：**關鍵路徑塊對每一個被動畫接手的屬性，靜態值必須逐字等於該動畫 from 的值**。
     連帶的另一半：靜態值一旦變成起始狀態，`backwards` 就撐不住終點了
     （動畫演完會退回起始狀態 ⇒ 名字自己不見）⇒ 那幾條動畫的 fill-mode 必須是 `both`／`forwards`。
     #splash 全程不可互動，所以通則「進場一律 backwards」的理由（保住 :active）在這裡不適用。

   ⚠️ 代價（PM 2026-08-27 拍板接受）：CSS 全 404 時名字不會出現（白起連符號也不會），
      只剩底色 ＋ splash-boot.js 的保險絲把開場收掉、app 正常可用。
      理由：**CSS 404 是罕見故障，這個跳動是每一次開 app 都會發生。**
   ============================================================ */
/* @keyframes <name>{ … } 的內容（大括號要配對，keyframes 裡面有巢狀規則） */
function keyframesBody(css, name) {
  var re = new RegExp("@keyframes\\s+" + name + "\\s*\\{");
  var m = re.exec(String(css || ""));
  if (!m) return null;
  var i = m.index + m[0].length, depth = 1;
  for (; i < css.length && depth > 0; i++) {
    if (css[i] === "{") depth++;
    else if (css[i] === "}") { depth--; if (!depth) break; }
  }
  return depth ? null : css.slice(m.index + m[0].length, i);
}
/* keyframes 裡的某一格（from／0%／to／100%） */
function keyframeStop(kf, names) {
  if (kf === null) return null;
  for (var i = 0; i < names.length; i++) {
    var m = new RegExp("(?:^|[};])\\s*" + names[i] + "\\s*\\{([^}]*)\\}").exec(kf);
    if (m) return m[1];
  }
  return null;
}
/* 一段宣告裡某個屬性的值（沒有就 null）。一律 nows 之後比對，換行縮排不算差異。 */
function declOf(body, prop) {
  if (body === null) return null;
  var m = new RegExp("(?:^|[;{])\\s*" + prop + "\\s*:([^;}]*)").exec(String(body));
  return m ? nows(m[1]) : null;
}
/* 值的等價正規化：`scale(1)`／`translateY(0)` 跟 `none` 是**同一件事**（單位矩陣），
   `1.0` 跟 `1` 也是。不正規化的話這把尺會對著「完全一樣的畫面」報錯
   —— 那是尺壞了，不是實作壞了。⚠️ 只認這幾個**恆等**寫法，別擴充成模糊比對。 */
function sameValue(prop, a, b) {
  if (a === null || b === null) return a === b;
  if (prop === "opacity") return Number(a) === Number(b);
  var norm = function (v) {
    var s = nows(v).toLowerCase();
    return (s === "scale(1)" || s === "scale(1,1)" || s === "scalex(1)" ||
            s === "translatey(0)" || s === "translatey(0px)" ||
            s === "translate(0,0)" || s === "translate(0px,0px)") ? "none" : s;
  };
  return norm(a) === norm(b);
}
/* 關鍵路徑塊的「有效靜態值」：只在這幾條選擇器之間做迷你 cascade
   （由高特異性到低特異性排好傳進來），找不到就回 CSS 初始值。 */
function critEffective(critCss, sels, prop, initial) {
  for (var i = 0; i < sels.length; i++) {
    var b = ruleBody(critCss, sels[i]);
    var v = declOf(b, prop);
    if (v !== null) return v;
  }
  return initial;
}
/* 回傳「不合格」的清單（空陣列 ＝ 過）。抽成函式才能拿假輸入回頭驗它不是恆綠。 */
function firstFrameMismatches(critCss, modCss, targets) {
  var out = [];
  targets.forEach(function (t) {
    var ruleTxt = ruleBody(modCss, t.modSel);
    if (ruleTxt === null) {
      out.push("找不到模組裡的規則 `" + t.modSel + "{…}`（尺壞了，或那一拍被改名了）");
      return;
    }
    var animM = /animation\s*:\s*([^;]+)/.exec(ruleTxt);
    if (!animM) { out.push("`" + t.modSel + "` 沒有 animation 簡寫"); return; }
    var anim = animM[1];
    if (anim.indexOf(t.anim) < 0) {
      out.push("`" + t.modSel + "` 的動畫不是 " + t.anim + "（實際：" + nows(anim) + "）");
      return;
    }
    var kf = keyframesBody(modCss, t.anim);
    var from = keyframeStop(kf, ["from", "0%"]);
    var to = keyframeStop(kf, ["to", "100%"]);
    if (from === null) { out.push("@keyframes " + t.anim + " 沒有 from／0%（尺壞了）"); return; }
    ["opacity", "transform"].forEach(function (prop) {
      var initial = prop === "opacity" ? "1" : "none";
      var start = declOf(from, prop);
      if (start === null) return;            /* 這一拍沒有動這個屬性 ⇒ 沒有約束 */
      var got = critEffective(critCss, t.critSels, prop, initial);
      if (!sameValue(prop, got, start)) {
        out.push("第一幀 ≠ 起始狀態：`" + t.critSels[0] + "` 的 " + prop + " 是 " + got +
          "，但 @keyframes " + t.anim + " 的 from 是 " + start +
          "（splash.css 一被套用畫面就會跳一次）");
      }
      /* 終點：靜態值已經是起點了 ⇒ 動畫必須自己撐住終點，不然演完會退回去 */
      var end = declOf(to, prop);
      if (end !== null && !sameValue(prop, end, got) && !/\b(both|forwards)\b/.test(anim)) {
        out.push("`" + t.modSel + "` 的 fill-mode 撐不住終點（" + prop + " 演完會退回 " + got +
          "）：靜態值＝起始狀態的時候，fill-mode 必須是 both／forwards");
      }
    });
  });
  return out;
}

if (CRIT && splashCss) {
  var FF_TARGETS = LIGHT ? [
    { cls: "sp-glyph", anim: "sp-emerge", modSel: 'html[data-splash-intro="light"] #splash .sp-glyph',
      critSels: ['html[data-splash-intro="light"] .sp-glyph', ".sp-glyph"] },
    { cls: "sp-name", anim: "sp-up", modSel: 'html[data-splash-intro="light"] #splash .sp-name',
      critSels: ['html[data-splash-intro="light"] .sp-name', ".sp-name"] }
  ] : [
    { cls: "sp-glyph", anim: "s-breathe", modSel: "#splash .sp-glyph", critSels: [".sp-glyph"] },
    { cls: "sp-name", anim: "sp-up", modSel: "#splash .sp-name", critSels: [".sp-name"] }
  ];
  /* 尺自證：掃到的目標數不對就別下結論 */
  if (FF_TARGETS.length < 2) {
    errors.push("「第一幀 ＝ 起始狀態」的尺壞了：只有 " + FF_TARGETS.length + " 個比對目標");
  }
  var modC = noCssComment(splashCss);
  firstFrameMismatches(CRIT, modC, FF_TARGETS).forEach(function (msg) { errors.push(msg); });

  /* #splash::before（白起的漸深層）：關鍵路徑塊刻意**沒有**這條規則 ⇒ 那個盒子根本不存在
     ＝ 畫不出東西，跟模組裡的靜態 opacity:0 等價。兩邊有任何一邊變了就要紅。 */
  if (LIGHT) {
    if (/#splash::before/.test(nows(CRIT))) {
      errors.push("關鍵路徑塊出現了 #splash::before：漸深那一層刻意不進關鍵路徑" +
        "（放了就是同一條動畫規則活在兩個地方）");
    }
    var beforeBody = ruleBody(modC, 'html[data-splash-intro="light"] #splash::before');
    if (declOf(beforeBody, "opacity") !== "0") {
      errors.push("motion/splash.css 的 #splash::before 靜態 opacity 不是 0：" +
        "關鍵路徑塊沒有這個盒子（＝畫不出來），模組這邊必須是 0 才對得起來");
    }
  }

  /* 負控組：拿「舊版的完成態」餵同一把尺，必須抓得到 —— 否則這一節是裝飾品 */
  (function () {
    var FAKE_DONE = LIGHT
      ? ".sp-name{font-size:20px;}\n.sp-glyph{width:76px;}"     /* 沒寫 opacity ⇒ 有效值 1 ＝ 完成態 */
      : ".sp-name{font-size:20px;}";
    var caught = firstFrameMismatches(FAKE_DONE, modC, FF_TARGETS);
    if (!caught.length) {
      errors.push("「第一幀 ＝ 起始狀態」的負控組沒過：拿舊版的完成態去驗，這把尺竟然放行（＝恆綠）");
    } else {
      infos.push("第一幀 ＝ 動畫起始狀態：" + FF_TARGETS.length + " 個目標逐一比對通過；" +
        "負控組（舊版完成態）被抓到 " + caught.length + " 條 ⇒ 這把尺會紅");
    }
    /* 這幾把小尺也各驗一次，不然「抓到了」有可能是因為它們恆 null */
    var negs = [
      [keyframesBody("@keyframes a{from{opacity:0;}to{opacity:1;}}", "a") !== null, "keyframesBody 抓得到存在的 keyframes"],
      [keyframesBody("@keyframes a{from{opacity:0;}}", "b") === null, "keyframesBody 對不存在的名字要回 null"],
      [keyframesBody("@keyframes sp-sink-bg{from{opacity:0;}}", "sp-sink") === null,
        "keyframesBody 不可以被 sp-sink-bg 餵飽（`\\b` 在連字號上也成立，這是踩過的坑）"],
      [keyframeStop("from{opacity:0;}to{opacity:1;}", ["from", "0%"]) === "opacity:0;", "keyframeStop 抓得到 from"],
      [declOf("opacity:0;transform:none;", "opacity") === "0", "declOf 抓得到屬性值"],
      [declOf("font-size:20px;", "opacity") === null, "declOf 對沒宣告的屬性要回 null"],
      [declOf("-webkit-opacity:9;", "opacity") === null, "declOf 不可以被帶前綴的屬性名餵飽"],
      [critEffective(".sp-name{opacity:0;}", [".sp-name"], "opacity", "1") === "0", "critEffective 讀得到宣告值"],
      [critEffective(".sp-name{font-size:20px;}", [".sp-name"], "opacity", "1") === "1", "critEffective 沒宣告時回初始值"],
      [critEffective('html[data-splash-intro="light"] .sp-glyph{opacity:0;}\n.sp-glyph{color:red;}',
        ['html[data-splash-intro="light"] .sp-glyph', ".sp-glyph"], "opacity", "1") === "0",
        "critEffective 的迷你 cascade 要先看高特異性那條"],
      [sameValue("transform", "scale(1)", "none") === true, "sameValue 認得 scale(1) ＝ none"],
      [sameValue("transform", "scale(.985)", "none") === false, "sameValue 不可以把真的縮放當成 none"],
      [sameValue("opacity", "1.0", "1") === true, "sameValue 認得 1.0 ＝ 1"],
      [sameValue("opacity", "0", "1") === false, "sameValue 分得出 0 與 1"]
    ].filter(function (n) { return !n[0]; });
    negs.forEach(function (n) { errors.push("「第一幀 ＝ 起始狀態」的小尺沒過：" + n[1]); });
  })();
}

/* ============================================================
   3b. 非阻塞 CSS ＋ 熱啟動 FOUC 閘門（2026-08-27 的三件套之二、之三）
   ------------------------------------------------------------
   背景：Benson 的螢幕錄影逐格拆開後發現，iPhone 從主畫面開 PWA 時
   iOS 大約在 0.5s 就開始把自己的啟動畫面**淡出**，而我們 0.73s 才第一次繪製
   ⇒ 中間那 0.23s 露出 WKWebView 的白底。這是賽跑不是漸進優化：
   趕在淡出前畫出第一幀，白色會整個消失。

   三件套（缺一不可，各自單獨做都等於零收益）：
     ① splash.js 拆出 splash-boot.js，head 裡只剩那一支小的（見上面第 2 節）
     ② 三支樣式表非阻塞：media="print" + onload 切回 all
     ③ 熱啟動 FOUC 閘門：html[data-cssgate] 把 body 底下的東西先壓住

   ⚠️ ② 沒有 ③ 的話，熱啟動會露出沒套樣式的 DOM；
      ③ 沒有「JS 停用時不會生效」的設計的話，會把 app 永遠藏起來。
      所以這三條要一起守。
   ============================================================ */
if (html) {
  /* 掃描式：把**每一支**非 noscript 的樣式表 <link> 抓出來逐一驗，不是列白名單 */
  var noscriptRanges = [];
  var nsRe = /<noscript[^>]*>([\s\S]*?)<\/noscript>/g, mns;
  while ((mns = nsRe.exec(html))) noscriptRanges.push([mns.index, mns.index + mns[0].length]);
  var inNoscript = function (at) {
    return noscriptRanges.some(function (r) { return at >= r[0] && at < r[1]; });
  };

  var allCssLinks = [];
  var lre = /<link\b[^>]*>/g, ml;
  while ((ml = lre.exec(html))) {
    if (/rel\s*=\s*["']stylesheet["']/i.test(ml[0])) {
      allCssLinks.push({ tag: ml[0], at: ml.index, ns: inNoscript(ml.index) });
    }
  }
  var liveLinks = allCssLinks.filter(function (l) { return !l.ns; });
  var nsLinks = allCssLinks.filter(function (l) { return l.ns; });

  /* 尺自證：掃不到就別下結論 */
  if (!liveLinks.length) {
    errors.push("非阻塞 CSS 檢查的尺壞了：一支 <noscript> 以外的樣式表 <link> 都沒掃到");
  } else {
    infos.push("非阻塞 CSS 檢查掃到 " + liveLinks.length + " 支樣式表 <link>（另有 " +
      nsLinks.length + " 支在 <noscript> 裡）");
  }

  var hrefOf = function (t) { var m = /href=["']([^"']+)["']/.exec(t); return m ? m[1] : "?"; };
  var nDefer = 0;
  liveLinks.forEach(function (l) {
    var href = hrefOf(l.tag);
    if (!/media\s*=\s*["']print["']/.test(l.tag)) {
      errors.push(href + " 還是 render-blocking 的樣式表（要 media=\"print\" ＋ onload 切回 all）：" +
        "第一次繪製要等它下載完，那正是這一輪要贏的那 0.4 秒");
      return;
    }
    if (!/\bdata-splash-css\b/.test(l.tag)) {
      errors.push(href + " 少了 data-splash-css：splash-boot.js 的保險絲是用這個屬性把它們掃出來的，" +
        "少了它 onload 沒觸發時就沒有人救");
    }
    if (!/onload\s*=/.test(l.tag) || l.tag.indexOf("media='all'") < 0) {
      errors.push(href + " 的 onload 沒有把 media 切回 all：樣式永遠不會生效");
    }
    if (!/onerror\s*=/.test(l.tag)) {
      errors.push(href + " 沒有 onerror：CSS 404 時閘門要靠 2 秒保險絲才開，" +
        "使用者會多盯 2 秒的空畫面");
    }
    if (!/__splashCss/.test(l.tag)) {
      errors.push(href + " 的 onload/onerror 沒有回報給 __splashCss()：閘門不知道它到齊了");
    }
    nDefer++;
  });
  if (liveLinks.length && nDefer !== liveLinks.length) {
    /* 上面已經逐條報過，這裡只留一行摘要 */
    infos.push("非阻塞 CSS：" + nDefer + "/" + liveLinks.length + " 支通過形態檢查");
  }

  /* noscript fallback：JS 停用時 onload 不會跑、media 永遠是 print ⇒ 整個 app 是裸 HTML */
  liveLinks.forEach(function (l) {
    var href = hrefOf(l.tag);
    var covered = nsLinks.some(function (n) { return hrefOf(n.tag) === href; });
    if (!covered) {
      errors.push(href + " 沒有 <noscript> 的 fallback：JS 被停用時它永遠是 media=print，" +
        "app 會變成沒套樣式的裸 HTML");
    }
  });
  /* JS 停用時也沒有人會收開場 ⇒ 全螢幕的 #splash 會永遠卡住 */
  var nsBody = noscriptRanges.map(function (r) { return html.slice(r[0], r[1]); }).join("\n");
  if (!/#splash\s*\{[^}]*display\s*:\s*none/.test(noCssComment(nsBody))) {
    errors.push("<noscript> 裡沒有把 #splash 關掉：JS 被停用時沒有人會收開場，" +
      "全螢幕的開場會永遠卡在畫面上＝app 打不開");
  }
}

/* 閘門本身：規則要在關鍵路徑塊裡（外部 CSS 全掛時它也得成立），
   而且**只能由 JS 掛得起來** —— 選擇器一定要帶 html[data-cssgate]。 */
function critGateHides(css) {
  return /html\[data-cssgate\][^{]*\{[^}]*visibility:hidden/.test(nows(css));
}
function critGateBg(css) {
  return /html\[data-cssgate\][^{,]*[,{][^}]*background:var\(--splash-bg,/.test(nows(css)) ||
         /html\[data-cssgate\]\{background:var\(--splash-bg,/.test(nows(css));
}
if (CRIT) {
  if (!critGateHides(CRIT)) {
    errors.push("關鍵路徑塊裡沒有 FOUC 閘門（html[data-cssgate] … {visibility:hidden}）：" +
      "CSS 改成非阻塞之後，熱啟動會露出沒套樣式的 DOM");
  }
  /* 閘門期間的底色也要是深的，不可以在這裡自己補一塊白 */
  if (!critGateBg(CRIT)) {
    errors.push("閘門期間（html[data-cssgate]）沒有指定 --splash-bg 底色：" +
      "熱啟動在 CSS 到齊之前會是瀏覽器預設的白 —— 那正是這一輪要消滅的東西");
  }
  /* 閘門是白名單型（列出要藏的東西）還是掃描型（藏 body 底下除了開場以外的全部）？
     白名單一定會漏（範本自己就差點漏掉 #fab）。 */
  if (nows(CRIT).indexOf("body>*") < 0) {
    warns.push("FOUC 閘門看起來是列白名單：建議寫成 " +
      "html[data-cssgate] body > *:not(#splash){visibility:hidden}，以後多一個浮動元素才不會漏");
  }
  /* ⭐⭐ 2026-08-27 v1.6.1：開場**播放中**也要把 app 內容壓住（不只閘門那一段）。
     來源：movie-library 的錄影在「splash.css 被套用」那兩格拍到畫面下緣露出 app 內容 ——
     時間點就是閘門開的那一刻。桌機量不到 #splash 沒蓋滿，所以修法選「開場播放中根本不畫 app」，
     把「#splash 有沒有蓋滿每一個實體像素」從正確性的前提裡拿掉。
     三個性質缺一不可，所以三條都要檢查（少一條就會變成「app 永遠看不見」）。 */
  function critPlayHides(css) {
    return /html:not\(\[data-splash="off"\]\)#splash:not\(\.out\)~\*\{visibility:hidden;?\}/.test(nows(css));
  }
  function noscriptUnhides(rawHtml) {
    var m = /<noscript[^>]*>([\s\S]*?)<\/noscript>/i.exec(String(rawHtml || ""));
    if (!m) return false;
    return /#splash~\*\{visibility:visible!important;?\}/.test(nows(m[1]));
  }
  if (!critPlayHides(CRIT)) {
    errors.push('關鍵路徑塊少了「開場播放中不畫 app」那一條：' +
      'html:not([data-splash="off"]) #splash:not(.out) ~ *{visibility:hidden;}。' +
      "少了它，#splash 只要有一塊沒蓋到（iOS 的 safe-area／fixed containing block），" +
      "閘門一開就會露出 app 的內容 —— 而那正好是使用者眼中「開場中間閃了一下」的那一格");
  } else if (!noscriptUnhides(html)) {
    errors.push("有「開場播放中不畫 app」那一條，但 <noscript> 裡沒有把它解除。" +
      'JS 停用時 <html> 上不會有 data-splash ⇒ 那條會匹配 ⇒ **app 被永遠藏起來**。' +
      "<noscript> 的 <style> 要補：#splash ~ *{visibility:visible !important;}");
  }

  /* 負控組：證明上面幾個判準會回 false，不是恆真 */
  var GFAKE = "#splash{position:fixed;} html{background:var(--splash-bg,#000);}";
  var negGate = [
    [!critGateHides(GFAKE) && !critGateBg(GFAKE), "FOUC 閘門的判準對假輸入必須回 false"],
    [!critPlayHides(GFAKE), "「開場播放中不畫 app」的判準對假輸入必須回 false"],
    [!critPlayHides('html #splash ~ *{visibility:hidden;}'),
      "少了 :not([data-splash=\"off\"]) 或 :not(.out) 的版本不可以算數（那會把熱啟動與收場一起藏掉）"],
    [critPlayHides('html:not([data-splash="off"]) #splash:not(.out) ~ *{visibility:hidden;}'),
      "正確寫法必須算數（證明這把尺不是恆 false ＝ 恆紅）"],
    [!noscriptUnhides("<noscript><style>#splash{display:none !important;}</style></noscript>"),
      "只有 display:none 的 <noscript> 不算解除（display:none 擋不住兄弟選擇器）"],
    [noscriptUnhides('<noscript><style>#splash ~ *{visibility:visible !important;}</style></noscript>'),
      "正確的 <noscript> 解除必須被認出來"]
  ].filter(function (n) { return !n[0]; });
  if (negGate.length) {
    negGate.forEach(function (n) { errors.push("FOUC／開場遮罩的判準是恆真或恆假的：" + n[1]); });
  } else {
    infos.push("FOUC 閘門＋開場遮罩的判準負控組 6 條全過（假輸入會被判成不合格）");
  }
}

/* ============================================================
   4. icon
   ============================================================ */
if (manifest && Array.isArray(manifest.icons)) {
  var base = manifestPath ? path.dirname(manifestPath) : ROOT;
  manifest.icons.forEach(function (ic) {
    if (!ic || !ic.src) return;
    if (/^https?:/i.test(ic.src)) {
      warns.push("icon 用了外部網址（" + ic.src + "）：這一系列 app 的原則是零外部資源");
      return;
    }
    if (!fs.existsSync(path.resolve(base, ic.src))) {
      errors.push("icon 檔案不存在：" + ic.src + "（每次載入都會多一則 404，底色必須是 " + (manBg || "背景色") + "）");
    }
  });
}

/* ============================================================
   5. 對比度：每一組「底色＋壓在上面的文字」都要 >= 4.5:1
   ------------------------------------------------------------
   ⭐ 這一節是這一輪最重要的退件修正。
      開場那一幕的符號字色算得好好的（有下界），交棒到 app 裡的同一個符號
      卻是寫死的白字、而且是全螢幕最低對比（深色模式 2.65:1）。
      規則只在開場那一幕被執行，過了那一幕就沒人守 —— 所以現在把
      「app 自己的色票」也納入同一條線。

   ⚠️ 量測用的 WCAG 實作是本檔自己寫的（refLum/refCt），
      不是借 splash.js 的——尺不可以來自被量的程式，
      否則有人把 relLum 改壞時，決策與量測會一起錯、互相掩護，這條線永遠不會紅。
   ============================================================ */
function refLum(hex) {
  var h = String(hex).replace("#", "");
  if (h.length === 4) h = h.slice(0, 3);
  if (h.length === 8) h = h.slice(0, 6);
  if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
  if (!/^[0-9a-fA-F]{6}$/.test(h)) return NaN;
  var acc = [0.2126, 0.7152, 0.0722], sum = 0;
  for (var i = 0; i < 3; i++) {
    var v = parseInt(h.substr(i * 2, 2), 16) / 255;
    sum += acc[i] * (v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4));
  }
  return sum;
}
function refCt(a, b) {
  var x = refLum(a), y = refLum(b);
  var hi = Math.max(x, y), lo = Math.min(x, y);
  return (hi + 0.05) / (lo + 0.05);
}
/* 尺的自證：量不出這三個，後面所有數字都不可信 */
var rulerOK = Math.abs(refCt("#ffffff", "#000000") - 21) < 0.01 &&
              Math.abs(refCt("#777777", "#777777") - 1) < 0.001 &&
              refLum("#ffffffff") === refLum("#ffffff") &&
              refLum("#fff8") === refLum("#ffffff");
if (!rulerOK) errors.push("對比度量測工具自己算錯了（白對黑要 21:1、8 碼要等於 6 碼），這一節結果不可信");

/* ---- 解析 motion.css 的 CSS 變數（三個區塊：淺色、深色 media、深色 attr）---- */
function scanBlocks(css) {
  var res = [], i = 0;
  while (i < css.length) {
    var open = css.indexOf("{", i);
    if (open < 0) break;
    var sel = css.slice(i, open).replace(/^[\s;}]+|[\s]+$/g, "");
    var d = 0, j = open, close = -1;
    for (; j < css.length; j++) {
      if (css[j] === "{") d++;
      else if (css[j] === "}") { d--; if (d === 0) { close = j; break; } }
    }
    if (close < 0) break;
    var body = css.slice(open + 1, close);
    if (sel.charAt(0) === "@") {
      scanBlocks(body).forEach(function (b) {
        b.media = sel + (b.media ? " " + b.media : "");
        res.push(b);
      });
    } else {
      res.push({ sel: sel, body: body, media: "" });
    }
    i = close + 1;
  }
  return res;
}
function decls(body) {
  var out = {}, re = /(--[a-zA-Z0-9-]+)\s*:\s*([^;]+);/g, m;
  while ((m = re.exec(body))) out[m[1]] = m[2].trim();
  return out;
}
function resolve(map, name, depth) {
  var v = map[name];
  if (v === undefined) return null;
  if (depth > 8) return null;
  var m = /^var\(\s*(--[a-zA-Z0-9-]+)\s*\)$/.exec(v);
  if (m) return resolve(map, m[1], (depth || 0) + 1);
  return v;
}

/* ⭐⭐ trade-log 落地補丁④：這一節（元件色票對比度）在這支 App 上**量不到東西**，
   而且那是刻意的 —— 不是尺壞了，是被量的對象搬走了。
   ------------------------------------------------------------
   範本的 motion.css 自己帶一整套色票（--bg / --surface / --accent / --ink…），
   這一節就是在驗那套色票的對比度。
   但這支 App 的 css/motion.css 是**新寫的、一個顏色都沒有**：
   範本的 --bg / --surface / --surface-2 / --line 跟 css/style.css **四個全部撞名**，
   整包抄會把交易日誌的深色與淺色配色一起蓋掉。
   ⇒ 這支 App 的顏色全部住在 css/style.css，變數名也不一樣（--text / --dim / --faint / --gold…），
     範本這一節的表格對不上，硬跑會產生 12 條假錯誤。

   ⚠️ 依手冊「守衛要把它看不到的東西講出來」：這裡**不可以安靜跳過**。
     做法是把它換成兩條這支 App 真正該守的斷言，並明講涵蓋範圍：
       ① css/motion.css 裡不可以出現任何色碼（這才是「只搬動效那一層」的紅線）；
       ② css/style.css 仍然是色票的家（8 個色票變數 ＋ 淺色模式那一組覆寫都還在）。
     顏色對比度本身**沒有被這支守衛涵蓋** —— 那是 lab-ux 定案 demo 的範圍，
     這一輪一個顏色都沒有動。 */
var COLOR_RE = /(#[0-9a-fA-F]{3,8}\b|\brgba?\s*\(|\bhsla?\s*\()/;
var motionCssNoColor = motionCss && !COLOR_RE.test(motionCss.replace(/\/\*[\s\S]*?\*\//g, ""));
if (motionCssNoColor) {
  infos.push("motion.css 零顏色 ✓（只有動效 token 與規則；撞名的 --bg/--surface/--surface-2/--line 一個都沒被宣告）");
  var appCss = read(path.join(ROOT, "css", "style.css"));
  if (!appCss) {
    errors.push("找不到 css/style.css —— 色票的家搬走了？這支守衛就再也證不了「顏色沒被動到」");
  } else {
    var wantVars = ["--bg", "--surface", "--surface-2", "--line", "--text", "--dim", "--faint", "--gold"];
    var missing = wantVars.filter(function (v) { return appCss.indexOf(v + ":") < 0; });
    if (missing.length) errors.push("css/style.css 少了色票變數：" + missing.join(" / "));
    if (!/@media\s*\(prefers-color-scheme\s*:\s*light\)/.test(appCss)) {
      errors.push("css/style.css 找不到淺色模式那一組覆寫（@media prefers-color-scheme: light）");
    }
    /* 自證：故意問一個不存在的變數，斷言上面那把尺會說「少了」 */
    if (appCss.indexOf("--this-var-does-not-exist:") >= 0) {
      errors.push("色票守衛的自證失敗：測試用的假變數竟然存在");
    }
    infos.push("色票仍住在 css/style.css ✓（" + wantVars.length + " 個變數 ＋ 淺色模式覆寫都在）");
  }
  infos.push("【涵蓋範圍】元件色票的對比度**不在這支守衛裡**：這支 App 的顏色不歸 motion.css 管，" +
             "而且這一輪一個顏色都沒有動。要驗對比度請跑 check-live.js 或直接量 css/style.css。");
}
var THEMES = {};
if (motionCss && !motionCssNoColor) {
  var clean = motionCss.replace(/\/\*[\s\S]*?\*\//g, "");
  var blocks = scanBlocks(clean);
  var light = {}, darkMedia = null, darkAttr = null;
  blocks.forEach(function (b) {
    if (b.sel === ":root" && !b.media) { Object.assign(light, decls(b.body)); }
    if (b.sel === ':root:not([data-theme="light"])' && /prefers-color-scheme:\s*dark/.test(b.media)) {
      darkMedia = decls(b.body);
    }
    if (b.sel === ':root[data-theme="dark"]' && !b.media) { darkAttr = decls(b.body); }
  });
  if (!Object.keys(light).length) errors.push("motion.css 解析不到 :root 的色票（解析器壞了？）");
  if (!darkMedia) errors.push("motion.css 找不到深色入口一：@media (prefers-color-scheme: dark) 裡的 :root:not([data-theme=\"light\"])");
  if (!darkAttr) errors.push("motion.css 找不到深色入口二：:root[data-theme=\"dark\"]");

  /* ⭐ 兩個深色入口必須逐條相同——這一輪的退件就是只改了其中一邊會發生的事 */
  if (darkMedia && darkAttr) {
    var keys = {}, k;
    for (k in darkMedia) keys[k] = 1;
    for (k in darkAttr) keys[k] = 1;
    Object.keys(keys).forEach(function (key) {
      if (darkMedia[key] !== darkAttr[key]) {
        errors.push("兩個深色入口不一致：" + key + " 在 media query 是 " +
          (darkMedia[key] || "(沒宣告)") + "，在 [data-theme=dark] 是 " + (darkAttr[key] || "(沒宣告)"));
      }
    });
  }
  THEMES.light = light;
  if (darkAttr) THEMES.dark = Object.assign({}, light, darkAttr);
}

var PAIRS = [
  ["--accent", "--on-accent", "色塊上的符號／按鈕文字"],
  ["--accent-soft", "--accent-ink", "淡底藥丸上的文字"],
  ["--bg", "--muted", "頁面上的次要文字"],
  ["--surface", "--muted", "卡片上的次要文字"],
  ["--surface-2", "--muted", "分頁未選中的文字"],
  ["--bg", "--ink", "主要文字"],
  ["--surface", "--ink-2", "卡片內文"],
  ["--surface-2", "--ink-2", "淡底按鈕文字"],
  ["--ok", "#ffffff", "完成態按鈕文字"],
  ["--danger", "#ffffff", "錯誤圖示"],
  ["--toast-bg", "#ffffff", "toast 文字"]
];
Object.keys(THEMES).forEach(function (theme) {
  var map = THEMES[theme];
  PAIRS.forEach(function (p) {
    var bg = p[0].indexOf("--") === 0 ? resolve(map, p[0], 0) : p[0];
    var fg = p[1].indexOf("--") === 0 ? resolve(map, p[1], 0) : p[1];
    if (!bg || !fg) {
      errors.push((theme === "light" ? "淺色" : "深色") + "：解析不到 " + p[0] + " 或 " + p[1] + "（" + p[2] + "）");
      return;
    }
    var c = refCt(bg, fg);
    if (isNaN(c)) { errors.push("色值看不懂：" + bg + " / " + fg); return; }
    if (c < 4.5) {
      errors.push((theme === "light" ? "淺色" : "深色") + "對比不足：" + p[2] + " " +
        bg + " 上的 " + fg + " 只有 " + c.toFixed(2) + ":1（要 >= 4.5:1）");
    }
  });
});

/* ============================================================
   6. 開場符號字色 onColor
   ------------------------------------------------------------
   為什麼檢查要放在這裡：這段算法會出現在每一支 App 的「開機第一個畫面」，
   而且鑰匙圈後台的預覽縮圖用的是同一份——兩端不一致，Benson 在後台看到的
   就跟實機不一樣。它壞掉不會有人回報，只會覺得「開場很醜」。
   （2026-08-25：舊版用未做 gamma 校正的亮度配 0.6 門檻，全色域 6.8% 低於 3:1、
     最差 #00d038 只有 2.08:1，已被推翻。）

   ⚠️ 不可以用非貪婪正則抓「第一個 }」：onColor 裡只要有一個 if 區塊，
      抓到的就是殘缺片段，症狀會變成「onColor 跑不起來」——會紅（方向安全），
      但訊息完全誤導人。所以用大括號配對找真正的結尾。
   ============================================================ */
function sliceFns(src) {
  if (!src) return null;
  var start = src.indexOf("function relLum(hex)");
  var head = src.indexOf("function onColor(bg)");
  if (start < 0 || head < 0 || head < start) return null;
  var i = src.indexOf("{", head), depth = 0;
  if (i < 0) return null;
  for (; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") { depth--; if (depth === 0) return src.slice(start, i + 1); }
  }
  return null;
}
/* ⭐ 2026-08-27：onColor 已經從 splash.js 搬到 splash-boot.js，而且**只准有一份**。
   （splash.js 走 window.SplashBoot.onColor。同一條規則活在兩個地方，
     鑰匙圈換色時一定分岔，而分岔的那一份沒有對比度下界。） */
if (splashJs && /function\s+onColor\s*\(/.test(splashJs)) {
  errors.push("motion/splash.js 裡有第二份 onColor()：它只准存在 motion/splash-boot.js。" +
    "兩份會在鑰匙圈換色時分岔，而且分岔的那一份沒有對比度下界");
}
if (splashJs && /function\s+relLum\s*\(/.test(splashJs)) {
  errors.push("motion/splash.js 裡有第二份 relLum()：色彩計算只准住在 motion/splash-boot.js");
}
var onSrc = sliceFns(bootJs);
var api = null;
if (bootJs && !onSrc) {
  errors.push("splash-boot.js 裡找不到 relLum / contrast / onColor（被改名或刪掉了？）");
} else if (onSrc) {
  var hasGamma = onSrc.indexOf("0.03928") >= 0 && onSrc.indexOf("1.055") >= 0 && onSrc.indexOf("2.4") >= 0;
  if (!hasGamma) {
    errors.push("onColor 少了 gamma 校正：不可以用未校正的 sRGB 亮度判斷。" +
      "飽和的綠／青會被誤判成暗底而給白字（實測最差 2.08:1）");
  }
  if (/\bL\s*[<>]=?\s*0*\.\d+/.test(onSrc) || /relLum\([^)]*\)\s*[<>]=?\s*0*\.\d+/.test(onSrc)) {
    errors.push("onColor 出現「亮度 > 門檻」的形狀：那個做法已被全色域實測推翻，禁止簡化回去。" +
      "正解是白字與深字各算一次對比度、取高的那個（最差情況才有下界）");
  }
  if (onSrc.indexOf("h.length === 4") < 0 || onSrc.indexOf("h.length === 8") < 0) {
    errors.push("relLum 沒有處理 4 碼／8 碼色碼：舊版一律 return 0（當成純黑）⇒ 永遠給白字，" +
      "#ffffffff 會變成白字白底 1.00:1");
  }
  try {
    api = new Function(onSrc + "\nreturn {onColor:onColor, ON_DARK:ON_DARK, ON_LIGHT:ON_LIGHT};")();
  } catch (e) {
    errors.push("onColor 跑不起來：" + e.message + "（也可能是它的形狀讓上面的抽取抓錯）");
  }
}

if (api && rulerOK) {
  /* 釘住點：三個 QA 抓到的飽和色、兩個範本自己的色、一個帶 alpha 的 */
  var PINS = [
    ["#00d038", api.ON_DARK, "飽和綠：未校正亮度會誤判成暗底"],
    ["#1db954", api.ON_DARK, "Spotify 綠"],
    ["#00bcd4", api.ON_DARK, "青"],
    ["#3a7bd5", api.ON_LIGHT, "中間調藍：平手帶內要偏白"],
    ["#101820", api.ON_LIGHT, "深底"],
    ["#ffffffff", api.ON_DARK, "8 碼白：alpha 防禦"]
  ];
  PINS.forEach(function (p) {
    var got = api.onColor(p[0]);
    if (got !== p[1]) {
      errors.push("onColor(" + p[0] + ") 應該給 " + p[1] + "，實際給 " + got + "（" + p[2] + "）");
      return;
    }
    var c = refCt(p[0], got);
    if (c < 3) errors.push("onColor(" + p[0] + ") 對比只有 " + c.toFixed(2) + ":1（要 >= 3:1）");
  });

  /* app 的 --on-accent 必須等於 onColor(--accent)：
     ⭐ 這條把「開場那一幕的規則」延伸到 app 內部，
        免得同一個符號在開場是算出來的、進了 app 就變成寫死的白字。 */
  Object.keys(THEMES).forEach(function (theme) {
    var acc = resolve(THEMES[theme], "--accent", 0);
    var on = resolve(THEMES[theme], "--on-accent", 0);
    if (!acc || !on) return;
    var want = api.onColor(acc);
    if (on.toLowerCase() !== want.toLowerCase()) {
      errors.push((theme === "light" ? "淺色" : "深色") + "的 --on-accent 是 " + on +
        "，但 onColor(" + acc + ") 算出來是 " + want +
        "（app 內的符號字色要跟開場用同一條規則，不可以各寫各的）");
    }
  });

  /* 全色域窮舉：抽樣會給出假的最差色（step 8 量到 4.28 @ #e04000，
     窮舉是 3.95 @ #438c83，樂觀 8% 而且指向錯的顏色）。
     所以這裡一定要窮舉，代價是 ~20 秒。趕時間用 --quick 跳過，但 CI 一律跑完整。 */
  if (QUICK) {
    infos.push("--quick：跳過全色域窮舉（16,777,216 色，約 20 秒）");
  } else {
    var t0 = Date.now();
    var LIN = new Array(256);
    for (var v = 0; v < 256; v++) {
      var x = v / 255;
      LIN[v] = x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4);
    }
    var HEX = new Array(256);
    for (var q = 0; q < 256; q++) HEX[q] = (q < 16 ? "0" : "") + q.toString(16);
    var L_LIGHT = refLum(api.ON_LIGHT), L_DARK2 = refLum(api.ON_DARK);
    var worst = 999, wbg = "", n = 0, below3 = 0;
    for (var r = 0; r < 256; r++) {
      for (var g = 0; g < 256; g++) {
        for (var b = 0; b < 256; b++) {
          var hex = "#" + HEX[r] + HEX[g] + HEX[b];
          var fg = api.onColor(hex);
          var L = 0.2126 * LIN[r] + 0.7152 * LIN[g] + 0.0722 * LIN[b];
          var lf = (fg === api.ON_LIGHT) ? L_LIGHT : L_DARK2;
          var hi = Math.max(lf, L), lo = Math.min(lf, L);
          var c2 = (hi + 0.05) / (lo + 0.05);
          n++;
          if (c2 < 3) below3++;
          if (c2 < worst) { worst = c2; wbg = hex; }
        }
      }
    }
    if (n !== 16777216) {
      errors.push("全色域窮舉只掃了 " + n + " 色（應該是 16777216），尺自己壞了");
    } else if (below3 > 0) {
      errors.push("全色域窮舉：" + below3 + " 個底色低於 3:1，最差 " +
        worst.toFixed(2) + ":1 @ " + wbg + "（開場符號會看不清楚）");
    } else {
      infos.push("onColor 全色域窮舉 " + n + " 色：最差 " + worst.toFixed(3) + ":1 @ " + wbg +
        "，0 個低於 3:1（" + ((Date.now() - t0) / 1000).toFixed(1) + "s）");
    }
  }
}

/* ---- 輸出 ---- */
function line(tag, s) { console.log(tag + " " + s); }
infos.forEach(function (s) { line("[說明]", s); });
warns.forEach(function (s) { line("[提醒]", s); });
errors.forEach(function (s) { line("[錯誤]", s); });

if (!errors.length) {
  console.log("[通過] 底色四處一致：" + (manBg || "?") + "；載入順序、對比度、onColor 全部過關。");
  process.exit(0);
}
console.log("");
console.log("共 " + errors.length + " 個錯誤。");
process.exit(1);
