/* ============================================================
   splash-boot.js — 開場的「第一幀」那一小段
   ------------------------------------------------------------
   ⚠️⚠️ 這支必須是 <head> 裡的**同步 script**（不要 defer、不要 async、
      不要搬到 body）。它是整個 app 唯一還站在「第一次繪製」關鍵路徑上的
      JS —— 所以它刻意很小，而且**只做四件事**：

        ① 讀 localStorage 的外觀快取（名字／符號／顏色）
        ② 把外觀寫成 :root 上的 CSS 變數
        ③ 冷／熱啟動判斷；熱啟動立刻掛 html[data-splash="off"]
        ④ 非阻塞 CSS 的閘門（data-cssgate）＋兩條保險絲

   為什麼要拆成兩支（2026-08-27，Benson 螢幕錄影逐格分析之後）：
     iPhone 從主畫面開 PWA 的順序是
       iOS 自己的啟動畫面（純黑）→ 約 0.5s 開始**淡出**、淡進 WKWebView
       → 我們的頁面如果還沒畫出任何東西，這一段就是 WebView 的白底
       → 我們第一次繪製（實測 0.73s）。
     所以那個白**不是「瀏覽器還沒畫」的瞬間空白，是一場賽跑**：
     只要趕在 iOS 開始淡出之前畫出第一幀，白色會整個消失、不是變短。
     舊版的 <head> 裡站著三支 CSS <link> ＋ 一支 17KB 的 splash.js，
     四個檔案全部是「畫出第一幀之前非到齊不可」。現在只剩這一支。

   ⚠️ 為什麼不能 defer／async：
     defer 之後外觀變數會在**第一次繪製之後**才寫進去
     ⇒ 冷啟動會「先畫預設值、再跳成鑰匙圈設定的名字」（中途換字，不可退步的鐵律）；
     ⇒ 熱啟動會先畫一幀開場再抽掉（每次切回來都閃一下）。

   ⚠️ onColor()（符號字色的算法）**只准存在這一份**。
      motion/splash.js 用的是這裡交出去的那一個，不是自己再寫一份。
      同一條規則活在兩個地方，鑰匙圈換色時一定分岔，而分岔的那一份沒有對比度下界。
      `node motion/check-splash.js` 會守這件事（splash.js 裡不可以有第二個 onColor）。

   對內（給 motion/splash.js 用，不是公開 API）：window.SplashBoot
   公開 API 仍然是 window.Splash（由 motion/splash.js 定義）。
   ============================================================ */
(function () {
  "use strict";

  var W = window;
  var D = document;
  var root = D.documentElement;
  var has = function (o, k) { return Object.prototype.hasOwnProperty.call(o, k); };

  /* iOS Safari 要有 touch 監聽，:active 才會生效
     （沒有這一行，手機上所有按下回饋都是死的）。
     刻意放在 boot 不放 splash.js：splash.js 現在在 body 尾端，
     萬一它沒載到，按下回饋不應該跟著一起死。 */
  try {
    D.addEventListener("touchstart", function () {}, { passive: true });
  } catch (e) {
    D.addEventListener("touchstart", function () {}, false);
  }

  /* ============================================================
     0. 設定
     ============================================================ */
  var CFG = W.SPLASH_CONFIG || {};
  var APP_ID = CFG.appId || "app";
  var APP_VER = String(CFG.version == null ? "1" : CFG.version);
  var KEYRING_URL = has(CFG, "keyringUrl")
    ? CFG.keyringUrl
    : "https://xd1104.github.io/keyring/keyring.json";
  var SPLASH_SEL = CFG.splashSelector || "#splash";
  var BOOT_SEL = CFG.bootSelector || "#app";
  var DEFAULTS = CFG.defaults || {};

  /* localStorage：外觀快取。key 由 PM 定死，不可以自己改。 */
  var CACHE_KEY = "splash:" + APP_ID;
  /* sessionStorage：冷啟動判斷。帶版本是為了改版後第一次進站再播一次。 */
  var SEEN_KEY = "splash-seen:" + APP_ID + ":" + APP_VER;

  var REDUCE = false;
  try {
    REDUCE = !!(W.matchMedia && W.matchMedia("(prefers-reduced-motion: reduce)").matches);
  } catch (e) {}

  var nowFn =
    W.performance && typeof W.performance.now === "function"
      ? function () { return W.performance.now(); }
      : function () { return Date.now(); };
  /* ⭐ t0 要在**這裡**取，不可以在 splash.js 取：splash.js 現在跑得比較晚，
     用它自己的起點算「最短顯示」會讓開場整段往後多停一截。 */
  var t0 = nowFn();
  function elapsed() { return nowFn() - t0; }

  /* ============================================================
     1. 外觀資料的清洗（鑰匙圈是外部來源，一律當成不可信）
     ------------------------------------------------------------
     每個欄位都可能不存在或是空字串 → 一律 fallback 到 app 的預設值。
     缺欄位不是錯誤，不要報錯、不要在 console 留紅字。
     ============================================================ */
  /* 契約只收 3 碼與 6 碼：開場底色帶 alpha 沒有意義（下面是空的），不該進契約。 */
  var RE_COLOR = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i;

  /* 丟掉控制字元（含換行——CSS 字串裡不能有），再去頭尾空白、截長度。
     刻意用迴圈不用正則的控制字元類別：原始碼裡不要出現真的控制字元。 */
  function txt(v, max) {
    if (v == null) return "";
    var raw = String(v);
    var s = "";
    for (var i = 0; i < raw.length; i++) {
      var c = raw.charCodeAt(i);
      if (c < 32 || c === 127) continue;
      s += raw.charAt(i);
    }
    s = s.replace(/^\s+|\s+$/g, "");
    return s.length > max ? s.slice(0, max) : s;
  }
  function col(v) {
    var s = txt(v, 12);
    return RE_COLOR.test(s) ? s : "";
  }
  function oneChar(v) {
    var s = txt(v, 8);
    if (!s) return "";
    /* 用 Array.from 才不會把 emoji／罕用字的代理對切成半個字 */
    var arr = typeof Array.from === "function" ? Array.from(s) : s.split("");
    return arr[0] || "";
  }

  /* ⭐ 符號字色：白字與深字各算一次 WCAG 對比度，取高的那個；
     近乎平手時偏好白字（見下方 15% 規則）。
     （PM 2026-08-25 拍板：符號本身的文字色「不進契約、不讓使用者設定」。
       多一個色票就多一種「調成看不見」的可能——keyring 踩過主要按鈕
       變透明、純功能測試抓不到的雷。自動算才有下界。
       這段程式碼與鑰匙圈後台的預覽縮圖是同一份，改的話兩邊要一起改，
       否則 Benson 在後台看到的跟實機不一樣。）

     ⚠️ 不要改回「亮度 > 門檻」那種猜法——飽和的綠／青會被誤判成暗底而給白字，
        實測最差只有 2.08:1（全色域 6.8% 低於 3:1）。這裡的 gamma 校正不是裝飾。
     ⭐ 這個做法可靠的理由不是「亮度猜得準」，而是
        「取兩個候選中對比較高的那個，所以最差情況有下界」。
        含 15% 平手偏白之後，全色域「窮舉」16,777,216 色實測最差 3.95:1 @ #438c83，
        0.000% 低於 3:1。
        ⚠️ 這個數字一定要用窮舉量：抽樣（step 8）會給出假的最差色 4.28:1 @ #e04000，
           比真值樂觀 8%，而且指向錯的顏色。
     ⚠️⚠️ 這是**全專案唯一一份** onColor()。2026-08-27 拆檔時它從 splash.js 搬到這裡，
        splash.js 不可以再寫一份、app 的 index.html 也不可以 inline 一份。 */
  function relLum(hex){
    var h = String(hex||"").replace("#","");
    /* 4 碼／8 碼帶 alpha：丟掉 alpha 再算。
       契約已經收窄成只收 3 碼與 6 碼（見 RE_COLOR），這裡仍然要正確處理——
       不要因為上游收窄了就假設不會發生。
       舊版對 4/8 碼一律 return 0（當成純黑）⇒ 永遠給白字，
       #ffffffff 會變成白字白底 1.00:1，「有下界」的保證整個破功。 */
    if(h.length === 4){ h = h.slice(0,3); }
    if(h.length === 8){ h = h.slice(0,6); }
    if(h.length === 3){ h = h[0]+h[0]+h[1]+h[1]+h[2]+h[2]; }
    if(!/^[0-9a-fA-F]{6}$/.test(h)){ return 0; }
    var c = [0,2,4].map(function(i){
      var v = parseInt(h.substr(i,2),16) / 255;
      return v <= 0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4);
    });
    return 0.2126*c[0] + 0.7152*c[1] + 0.0722*c[2];
  }
  function contrast(l1, l2){
    var hi = Math.max(l1,l2), lo = Math.min(l1,l2);
    return (hi + 0.05) / (lo + 0.05);
  }
  var ON_LIGHT = "#ffffff";
  var ON_DARK  = "#1a1310";
  function onColor(bg){
    var L = relLum(bg);
    var w = contrast(1, L);               /* 白字 */
    var d = contrast(relLum(ON_DARK), L); /* 深字 */
    if(w >= d){ return ON_LIGHT; }
    /* 近乎平手（差距 <15%）時偏好白字：兩者都夠讀，但深字會讓符號從
       「發光的徽章」變成「挖空的洞」，跟開場其餘的淺色字分屬兩套語言。
       ⚠️ 這個 15% 是拿「最差對比」換來的，動它之前先跑全色域斷言。 */
    return (d - w) / d < 0.15 ? ON_LIGHT : ON_DARK;
  }

  /* 把任意來源（鑰匙圈的 splash 物件 / 快取 / app 預設）洗成同一個形狀。
     只留下有值的欄位，空字串一律丟掉 ⇒ 合併時自然會 fallback。 */
  function clean(o) {
    var s = o && typeof o === "object" ? o : {};
    var out = {};
    var name = txt(s.name, 24);      if (name) out.name = name;
    var glyph = oneChar(s.glyph);    if (glyph) out.glyph = glyph;
    var tag = txt(s.tagline, 48);    if (tag) out.tagline = tag;
    var bg = col(s.bg);              if (bg) out.bg = bg;
    var accent = col(s.accent);      if (accent) out.accent = accent;
    var ink = col(s.ink);            if (ink) out.ink = ink;
    return out;
  }
  function isEmpty(o) {
    for (var k in o) { if (Object.prototype.hasOwnProperty.call(o, k)) return false; }
    return true;
  }
  function merge(base, over) {
    var out = {};
    var k;
    for (k in base) { if (Object.prototype.hasOwnProperty.call(base, k)) out[k] = base[k]; }
    for (k in over) { if (Object.prototype.hasOwnProperty.call(over, k)) out[k] = over[k]; }
    return out;
  }

  /* ============================================================
     2. 快取（localStorage）
     ------------------------------------------------------------
     隱私模式／存取被擋時全部安靜失敗——開場不可以因為存不了東西就掛掉。
     ============================================================ */
  function readCache() {
    try {
      var raw = W.localStorage.getItem(CACHE_KEY);
      if (!raw) return {};
      return clean(JSON.parse(raw));
    } catch (e) {
      return {};
    }
  }
  function writeCache(look) {
    try { W.localStorage.setItem(CACHE_KEY, JSON.stringify(look)); } catch (e) {}
  }
  function dropCache() {
    try { W.localStorage.removeItem(CACHE_KEY); } catch (e) {}
  }

  /* ============================================================
     3. 套用外觀（只動 :root 上的 --splash-* 變數）
     ------------------------------------------------------------
     ⭐ 只影響開場那一幕。不碰 app 的品牌色、標題、manifest、theme-color。
     文字要包成 CSS 字串（splash.css 是用 content:var(--splash-name) 畫的）。
     ============================================================ */
  function cssStr(s) {
    return '"' + String(s).replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"';
  }
  function setVar(name, value) {
    if (value) { root.style.setProperty(name, value); }
  }
  function applyLook(look) {
    setVar("--splash-bg", look.bg);
    setVar("--splash-ink", look.ink);
    if (look.accent) {
      setVar("--splash-accent", look.accent);
      /* 符號字色不是設定項，是算出來的（見 onColor） */
      setVar("--splash-on-accent", onColor(look.accent));
    }
    if (look.glyph) setVar("--splash-glyph", cssStr(look.glyph));
    if (look.name) setVar("--splash-name", cssStr(look.name));
    if (look.tagline) setVar("--splash-tagline", cssStr(look.tagline));
  }

  /* ============================================================
     4. 冷啟動判斷（只有冷啟動才播）
     ------------------------------------------------------------
     用 sessionStorage，不要用 localStorage：
     sessionStorage 的生命週期剛好等於「這一次啟動」——
     PWA 從主畫面重開＝新 session ⇒ 會播；
     切分頁、鎖屏解鎖、返回＝同一個 session ⇒ 不播。
     localStorage 會變成「一輩子只播一次」。
     ⚠️ 不准掛在 visibilitychange / focus / pageshow 上重播。
        bfcache 返回（pageshow.persisted）JS 根本不會重跑，什麼都不用做。
     ============================================================ */
  function isColdStart() {
    try {
      if (W.sessionStorage.getItem(SEEN_KEY)) return false;
      W.sessionStorage.setItem(SEEN_KEY, "1");
      return true;
    } catch (e) {
      return true; /* 隱私模式：寧可多播一次，不要整個 app 掛掉 */
    }
  }

  /* ============================================================
     5. 起手式（在 <head> 就跑完，body 還沒開始解析）
     ============================================================ */
  var builtin = clean(DEFAULTS);
  var cached = readCache();
  /* 快取優先，缺的欄位用 app 內建預設補。沒有快取就整個用內建預設。 */
  var LOOK = merge(builtin, cached);
  applyLook(LOOK);

  var COLD = isColdStart();
  if (!COLD) {
    /* 熱啟動：連一幀都不要畫出來（CSS: html[data-splash="off"] #splash{display:none}） */
    root.setAttribute("data-splash", "off");
  }

  /* ============================================================
     6. 非阻塞 CSS 的閘門（data-cssgate）
     ------------------------------------------------------------
     三支樣式表改成 media="print" onload="this.media='all'" ⇒ 它們不再擋第一次繪製。
     代價是：**熱啟動**（不播開場、直接看 app）在 CSS 到齊之前會看到
     一份沒套樣式的 DOM ＝ FOUC。所以 boot 一進來就先把閘門關上
     （關鍵路徑 CSS 裡：html[data-cssgate] #app{visibility:hidden}），
     CSS 到齊（或失敗、或保險絲燒了）才開。

     ⭐ 三條鐵律：
       ① 閘門**只有 boot 關得起來**。JS 被停用、或這支檔案沒載到 ⇒
          html 上根本不會有 data-cssgate ⇒ 畫面照常顯示。
          （守衛要寫成「有人負責開，才准關」。）
       ② 開閘條件是「每一支 link 都回報過」**或**保險絲時間到；兩條路都會
          強制把 media 改回 all —— 寧可 FOUC，也不可以把 app 永遠藏起來。
       ③ 閘門期間 <html> 的底色是 --splash-bg（深色），不是白的。
          那一段本來就是這場賽跑要贏的一段，不可以在這裡自己補一塊白。
     ============================================================ */
  var CSS_FUSE = 2000;
  var gateOpen = false;
  var domReady = false;

  root.setAttribute("data-cssgate", "");

  function deferredLinks() {
    try { return D.querySelectorAll("link[data-splash-css]"); } catch (e) { return []; }
  }
  function openGate() {
    if (gateOpen) return;
    gateOpen = true;
    /* 強制收尾：onload 沒觸發（某些 Safari 版本對 media 切換的怪癖）
       也要把樣式套上去，否則 app 會永遠是裸的 HTML。 */
    var ls = deferredLinks();
    for (var i = 0; i < ls.length; i++) {
      try { if (ls[i].media !== "all") { ls[i].media = "all"; } } catch (e) {}
    }
    root.removeAttribute("data-cssgate");
  }
  function checkGate() {
    if (gateOpen || !domReady) return;
    var ls = deferredLinks();
    for (var i = 0; i < ls.length; i++) {
      if (!ls[i].getAttribute("data-css-done")) return;
    }
    openGate();   /* 一支都沒有（app 用的是傳統阻塞式 CSS）也算到齊 */
  }
  /* 由 <link onload/onerror> 呼叫。刻意掛在 window 上：行內屬性只看得到全域。
     ⚠️ 404 也要呼叫（onerror）——「CSS 不見了」不可以變成「app 永遠看不見」。 */
  W.__splashCss = function (link) {
    try {
      link.media = "all";
      link.setAttribute("data-css-done", "1");
    } catch (e) {}
    checkGate();
  };
  W.setTimeout(openGate, CSS_FUSE);   /* 保險絲：寧可 FOUC 也不可以永遠藏起來 */

  /* ============================================================
     7. 交棒保險絲：splash.js 沒載到也一定要能用
     ------------------------------------------------------------
     ⭐ splash.js 現在在 <body> 尾端 ⇒ 它離線／漏檔沒載到的機率比以前高。
        以前保險絲住在 splash.js 自己裡面（＝那支檔案沒到就沒有保險絲），
        現在挪一份到 boot：沒有人接手就自己把開場收掉。
        （呼叫端 app.js 的 window.Splash && … 守衛仍然要留，這是第二層。）
     ============================================================ */
  var TAKEOVER_FUSE = 7000;
  /* ⚠️ hardRemove() **刻意不開閘門**。
     熱啟動時 splash.js 一到 DOM ready 就會呼叫它（開場連一幀都不播），
     那時候 CSS 通常還沒到 —— 在這裡順手開閘等於把 ② 的代價又還回去，
     熱啟動會露出沒套樣式的 DOM。閘門只有兩個開法：CSS 全回報，或 2 秒保險絲。
     （2026-08-27 §78c 實測抓到：原本寫在這裡，熱啟動的閘門 0ms 就開了。） */
  function hardRemove() {
    try {
      var sp = D.querySelector(SPLASH_SEL);
      if (sp && sp.parentNode) { sp.parentNode.removeChild(sp); }
      root.setAttribute("data-splash", "off");
    } catch (e) {}
  }
  W.setTimeout(function () {
    if (W.__splashTakeover) { return; }   /* splash.js 有接手，交給它 */
    hardRemove();
    /* 沒有人接手＝這一頁大概也出了別的事，閘門一起開掉，不要留一片空白 */
    openGate();
  }, TAKEOVER_FUSE);

  function onDomReady(fn) {
    if (D.readyState === "loading") {
      D.addEventListener("DOMContentLoaded", fn, { once: true });
    } else {
      fn();
    }
  }
  onDomReady(function () { domReady = true; checkGate(); });

  /* ============================================================
     7b. ⭐⭐ <meta name="theme-color"> 跟著開場的底色走（v1.6.3，2026-08-28）
     ------------------------------------------------------------
     為什麼要有這一段：
       iOS 從主畫面開 PWA 時，**頁面之外**的那一圈（safe-area／overscroll／
       home 指示條那一帶）不是我們畫的，是系統畫的，而系統參考的顏色候選有兩個：
       manifest 的 background_color（安裝當下抄走、改不動）與 **<meta theme-color>**。
       深色 app 開淺色開場（白起）時，這兩個值都還說「我是深色的」
       ⇒ 交接的那一兩格，畫面下緣有機會被畫成一條深色。
       movie-library 2026-08-28 的螢幕錄影就有這麼一條（58px、純 #0b0d12、
       home 指示條同時翻白＝ iOS 認定那塊是深色的）。
       **manifest.background_color 不准動**（動了 iOS 的系統啟動圖才會變，
       而那要使用者把 app 從主畫面移除重加）⇒ 我們能動的只有 theme-color。

     ⚠️ 這條深色帶有**兩個都說得通的成因**，而且本機複現不出來：
       ① 系統拿 theme-color 去畫頁面之外 ⇒ 這一段負責；
       ② 頁面自己有一塊沒被 #splash 蓋到、露出 body 的底色 ⇒ splash.css §7a2 負責。
       兩個成因的顏色**剛好一樣**（app 的 --bg ＝ theme-color ＝ manifest），
       所以錄影分不出來是哪一個。兩邊都修，不要挑一個信。

     做法：**追蹤 <html> 當下的 computed background-color**，不是自己再算一次漸變。
       - 顏色的真相來源只有一個（splash.css §7a 的 sp-sink-bg），
         這裡只是「把畫面上的顏色抄給系統」⇒ 不可能跟畫面對不上。
       - 因此**不需要知道任何時長**（--sp-lead／--sp-sink 都不必讀），
         換 token、換變體、reduce、CSS 沒載到，全部自動正確。
       - 印記變體與 reduce 之下 html 的底色本來就等於 theme-color
         ⇒ tcPut() 比對過就跳過，**一個位元組都不會寫** ＝ 這一段對它們是隱形的。

     ⚠️ 狀態列：Benson 的錄影顯示**淺色開場時狀態列文字已經是黑的**，
        而那時 theme-color 是 #0b0d12（深色）⇒ **iOS 沒有拿 theme-color 決定狀態列文字**
        （這支 app 用的是 apple-mobile-web-app-status-bar-style=black-translucent，
        iOS 自己看內容決定）。就算某個版本的 iOS 會看，這裡給的也永遠是
        **畫面當下的真實底色** ⇒ 「淺底配黑字、深底配白字」自動成立，
        不會出現「theme-color 說淺、畫面已經變深」那種字看不見的窗口。
     ⚠️ 收場一定要換回**原本的字面值**（不是等價的 rgb()），
        否則 app 跑起來之後 theme-color 會變成一個沒人設定過的字串。
     ⚠️ 只在冷啟動跑。熱啟動不播開場，theme-color 從頭到尾都該是 app 自己的深色。
     ============================================================ */
  var TC_META = null, TC_ORIG = null, TC_NOW = null, TC_OFF = false;

  /* 畫面當下的底色（＝ <html> 的 computed background-color）。
     透明或讀不到就回空字串 —— 寧可不寫，也不要把狀態列交給瀏覽器猜。 */
  function tcRead() {
    try {
      var v = W.getComputedStyle(root).backgroundColor;
      if (!v || v === "transparent" || v.indexOf(", 0)") > 0) { return ""; }
      return v;
    } catch (e) { return ""; }
  }
  /* 只為了比對「是不是同一個顏色」：#0b0d12 與 rgb(11, 13, 18) 必須算相等，
     否則印記變體每一幀都會白寫一次。 */
  function tcRGB(s) {
    var t = String(s || "").replace(/\s+/g, "").toLowerCase(), m;
    if (/^#[0-9a-f]{3}$/.test(t)) {
      return [parseInt(t.charAt(1) + t.charAt(1), 16),
              parseInt(t.charAt(2) + t.charAt(2), 16),
              parseInt(t.charAt(3) + t.charAt(3), 16)].join(",");
    }
    if (/^#[0-9a-f]{6}$/.test(t)) {
      return [parseInt(t.substr(1, 2), 16), parseInt(t.substr(3, 2), 16), parseInt(t.substr(5, 2), 16)].join(",");
    }
    m = /^rgba?\((-?\d+),(-?\d+),(-?\d+)/.exec(t);
    if (m) { return m[1] + "," + m[2] + "," + m[3]; }
    return t;
  }
  function tcPut(v) {
    if (!TC_META || !v) { return; }
    if (TC_NOW !== null && tcRGB(v) === tcRGB(TC_NOW)) { return; }
    TC_NOW = v;
    try { TC_META.setAttribute("content", v); } catch (e) {}
  }
  function tcRestore() {
    if (TC_OFF) { return; }
    TC_OFF = true;
    if (TC_META && TC_ORIG) {
      TC_NOW = null;   /* 強制寫回原字面值，不要因為「顏色一樣」而留下 rgb(...) */
      tcPut(TC_ORIG);
    }
  }
  /* 開場收掉了沒？三條路任一條成立都算（跟遮罩那條 CSS 是同一組判準）：
     data-splash="off"／#splash 離開 DOM／下面的保險絲。
     ⚠️ `#splash 不在` 這條**必須等 domReady**：這一段在 <head> 就跑了，
        那時 <body> 根本還沒解析，不擋的話第一個 rAF 就會把自己關掉。 */
  function tcOver() {
    try {
      if (root.getAttribute("data-splash") === "off") { return true; }
      if (domReady && !D.querySelector(SPLASH_SEL)) { return true; }
    } catch (e) { return true; }
    return false;
  }
  function tcNext(fn) {
    try {
      if (typeof W.requestAnimationFrame === "function") { W.requestAnimationFrame(fn); return; }
    } catch (e) {}
    W.setTimeout(fn, 16);
  }
  function tcTick() {
    if (TC_OFF) { return; }
    if (tcOver()) { tcRestore(); return; }
    tcPut(tcRead());
    tcNext(tcTick);
  }
  if (COLD) {
    try { TC_META = D.querySelector('meta[name="theme-color"]'); } catch (e) { TC_META = null; }
    if (TC_META && TC_META.getAttribute("content")) {
      TC_ORIG = TC_META.getAttribute("content");
      /* ⚠️ TC_NOW 要先設成原值，否則第一次寫入沒有東西可以比對 ⇒
         印記變體（底色本來就等於 theme-color）也會被改寫成等價的 rgb(...)。 */
      TC_NOW = TC_ORIG;
      tcPut(tcRead());     /* 第一次繪製之前就要對 */
      tcNext(tcTick);
      /* 保險絲：分頁被切到背景時 rAF 會停，這條保證 theme-color 一定換得回來。
         比交棒保險絲再晚一點（那時開場一定已經被收掉了）。 */
      W.setTimeout(tcRestore, TAKEOVER_FUSE + 1500);
    }
  }

  /* ============================================================
     8. 交給 motion/splash.js（不是公開 API）
     ============================================================ */
  W.SplashBoot = {
    appId: APP_ID, appVer: APP_VER, keyringUrl: KEYRING_URL,
    splashSel: SPLASH_SEL, bootSel: BOOT_SEL,
    cacheKey: CACHE_KEY, seenKey: SEEN_KEY,
    cold: COLD, reduce: REDUCE, look: LOOK, cached: cached,
    t0: t0, elapsed: elapsed, now: nowFn,
    onColor: onColor, relLum: relLum, contrast: contrast,
    clean: clean, merge: merge, isEmpty: isEmpty,
    applyLook: applyLook,
    readCache: readCache, writeCache: writeCache, dropCache: dropCache,
    hardRemove: hardRemove, openCssGate: openGate, onDomReady: onDomReady
  };
})();
