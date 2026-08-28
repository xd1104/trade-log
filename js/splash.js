/* ============================================================
   splash.js — 開場的時序、收場、鑰匙圈（印記 sigil ＋ B 蓋住等待）
   ------------------------------------------------------------
   ⚠️ 這支**不再站在第一次繪製的關鍵路徑上**（2026-08-27 拆檔）。
      它放在 <body> 尾端，跑在第一幀畫出來之後。
      「第一幀要長什麼樣」那一段已經搬到 motion/splash-boot.js
      （<head> 裡的同步 script）—— 拆檔的理由寫在那支檔案的檔頭。

   ⚠️ 這支**沒有** onColor()／relLum()／clean()／快取讀寫。
      那些全部住在 splash-boot.js，這裡一律走 window.SplashBoot.xxx。
      不要因為「這樣比較好讀」就在這裡再寫一份 —— 同一條規則活在兩個地方，
      鑰匙圈換色時一定分岔。`node motion/check-splash.js` 會擋。

   ⚠️ 收屍一律用 timer ＋ runId ＋ 保險絲，不掛 animationend。
      開場是全螢幕的，animationend 沒觸發＝整個 app 打不開，
      比某個面板關不掉嚴重一個等級。

   設定（在載入 splash-boot.js 之前先寫 window.SPLASH_CONFIG）：
     {
       appId:      "trade-log",                 // 要跟 keyring.json 的 apps[].id 一樣
       version:    "1",                         // 改版後第一次進站會再播一次
       keyringUrl: "https://xd1104.github.io/keyring/keyring.json",  // 設 "" 可完全關掉
       splashSelector:"#splash", bootSelector:"#app",
       defaults: { name:"交易日誌", glyph:"T", bg:"#101820",
                   accent:"#3a7bd5", ink:"#e6edf3", tagline:"紀律比行情重要" }
     }

   開場變體（opt-in，2026-08-27）：<html data-splash-intro="light"> ＝「白起」。
     CSS 那一半在 motion/splash.css §7；這支只需要跟著把最短顯示拉長（見 MIN_SHOW）。
     兩邊讀**同一個**屬性，才不可能出現「CSS 演白起、JS 用印記的長度」這種分岔。

   對外 API（window.Splash）：
     Splash.hold()    有資料要等的 app：在最上面呼叫，宣告「我會自己說什麼時候好」
     Splash.ready()   資料回來（成功或失敗都要）呼叫，開場就會收
     Splash.dismiss() 立刻收（測試用）
     Splash.state()   目前狀態（QA 用：冷啟動與否、實際套用的外觀、已過幾毫秒、
                      現在演的是哪一個變體 state().intro）
   ============================================================ */
(function () {
  "use strict";

  var W = window;
  var D = document;
  var root = D.documentElement;
  var B = W.SplashBoot;

  /* ============================================================
     0. 沒有 boot 就不要硬撐
     ------------------------------------------------------------
     splash-boot.js 沒載到（部署漏檔、SW 殼快取沒建完）時：
     外觀變數沒被設過、冷熱啟動也沒判斷過 —— 這支沒有能力補完那些事
     （補了就等於把 boot 的內容抄第二份，正是這次拆檔要消滅的東西）。
     所以退場方式是「把開場收掉、讓 app 正常用」，而且仍然要提供
     window.Splash 的四個方法，呼叫端才不會走到「模組不在」的降級路徑而多做一次收屍。
     ============================================================ */
  if (!B) {
    try {
      var sp0 = D.querySelector("#splash");
      if (sp0 && sp0.parentNode) { sp0.parentNode.removeChild(sp0); }
      root.setAttribute("data-splash", "off");
      root.removeAttribute("data-cssgate");
      D.addEventListener("touchstart", function () {}, { passive: true });
    } catch (e) {}
    W.Splash = {
      hold: function () {}, ready: function () {}, dismiss: function () {},
      state: function () { return { boot: false, cold: null, look: {}, dismissed: true }; }
    };
    return;
  }

  /* 告訴 boot「我接手了」，它那條 7 秒的交棒保險絲就不用出手 */
  W.__splashTakeover = true;

  var SPLASH_SEL = B.splashSel;
  var BOOT_SEL = B.bootSel;
  var REDUCE = B.reduce;
  var COLD = B.cold;
  var LOOK = B.look;

  /* ⭐ 開場變體（opt-in）：<html data-splash-intro="light"> ＝「白起」，見 motion/splash.css §7。
     這裡只需要知道一件事 —— 白起那一版的動作**比印記長**，所以最短顯示要跟著長。
     ⚠️ 讀的是 <html> 上的**靜態屬性**（CSS 也是讀同一個），不是另一個設定檔：
        兩邊看同一個開關，才不可能出現「CSS 是白起、JS 卻用印記的時間」這種分岔。 */
  var INTRO = "";
  try { INTRO = root.getAttribute("data-splash-intro") || ""; } catch (e) {}
  var LIGHT = INTRO === "light";

  /* 時間常數＝產品邏輯，不是動效 token（所以在 JS 不在 CSS） */
  var MIN_SHOW = REDUCE ? 300 : (LIGHT ? 1490 : 950);
  /* 白起（light）＝ 1490ms，逐項算出來的：
       --sp-lead 340（＝sp-hold 220 ＋ dur-press 120）起跑前先靜止住的那一拍
       ＋ 漸深 --sp-sink 700（＝dur-3 420 ＋ dur-2 280）
       ＋ 名字 sp-up dur-3 420（它是 delay = lead + sink 才開始的）
       ＝ 動作在 1460ms 結束，再留 30ms 定格。
     印記那一版是「動作 920ms 結束、最短顯示 950」＝ 同樣留 30ms
     —— 兩個變體用的是同一條規則，不是各喊各的數字。
     ⭐ 2026-08-27（v1.6.2）從 1230 → 1490：多出來的 340 是 --sp-lead 那一拍
        （見 motion/splash.css §7 的長註解：iOS 交出畫面之前開場已經演掉約 273ms），
        同時把定格從 110 縮成 30 把時間吐回去一點。**多的是「開場前面多靜止一下」，
        不是「動作變慢」** —— --dur-* 與 --sp-hold 一個都沒有動。
     ⚠️ 改 --sp-lead／--sp-sink／--dur-* 就要回來重算這個數字（沒有第二個地方會提醒你）。 */
  /* 印記（預設）＝ 950ms：B 模式最短顯示，避免快取秒回時「閃一下」的廉價感。
     ⭐ 950 是 Benson 2026-08-26 試用後拍板的：開場的動作本身約 640ms 就收尾
        （--sp-hold 220 ＋ 動作 420），最短顯示 650 等於「動作剛做完就走」，
        沒有一拍讓人看清楚 ⇒ 多留約 300ms 的定格。
     ⚠️ 要的是「多停留」不是「變慢」——動作速度是已定案的沉穩基調，
        不可以改用放慢 --dur-* 或 --sp-hold 來達成。
     ⚠️ 這一段是從 **boot 的 t0** 起算（B.elapsed()），不是從這支檔案被執行起算
        —— 拆檔之後兩者差幾十毫秒到幾百毫秒。 */
  var FUSE = 6000;                     /* 保險絲：不管發生什麼，超過就一定收 */
  var OUT_MS = REDUCE ? 60 : 340;      /* 收場動畫長度（要對得上 --sp-out） */
  var BOOT_MS = 1400;                  /* .boot 掛多久（久了會壓到 :active） */
  var KEYRING_TIMEOUT = 8000;

  /* ============================================================
     1. 小工具
     ============================================================ */
  function elapsed() { return B.elapsed(); }

  var timers = [];
  function later(fn, ms) {
    var id = W.setTimeout(fn, Math.max(0, ms));
    timers.push(id);
    return id;
  }
  function clearTimers() {
    for (var i = 0; i < timers.length; i++) { W.clearTimeout(timers[i]); }
    timers = [];
  }

  function isArr(v) { return Object.prototype.toString.call(v) === "[object Array]"; }

  /* ============================================================
     2. 收場（B 蓋住等待：資料好了就走，最短顯示 MIN_SHOW）
     ============================================================ */
  var runId = 0;
  var dismissed = false;
  var pendingDismiss = false;
  var manual = false;      /* app 有沒有宣告「我會自己說什麼時候好」 */
  var readyFired = false;

  function bootHosts() {
    try { return D.querySelectorAll(BOOT_SEL); } catch (e) { return []; }
  }

  function startBoot() {
    /* 銜接：收場動畫與內容進場要「重疊」不要「串接」。
       串接會有一個很明顯的空拍，那個空拍就是廉價感的來源。 */
    var hosts = bootHosts();
    if (!hosts || !hosts.length) return;
    var i;
    for (i = 0; i < hosts.length; i++) { hosts[i].classList.add("boot"); }
    later(function () {
      for (var j = 0; j < hosts.length; j++) { hosts[j].classList.remove("boot"); }
    }, BOOT_MS);
  }

  function hardRemove() { B.hardRemove(); }

  function dismiss() {
    if (dismissed) return;
    var sp = D.querySelector(SPLASH_SEL);
    if (!sp) {
      /* body 還沒解析到 #splash（極端狀況）：先記著，DOM 好了立刻收 */
      pendingDismiss = true;
      return;
    }
    dismissed = true;
    var my = ++runId;
    clearTimers();
    sp.classList.add("out");
    startBoot();
    later(function () {
      if (my !== runId) return;
      /* ⚠️ 收掉之後要 remove()，不是 hidden：
         骨架屏的 shimmer 是 infinite 動畫，留著會一直吃 GPU。 */
      if (sp.parentNode) { sp.parentNode.removeChild(sp); }
      root.setAttribute("data-splash", "off");
      afterSplash();
    }, OUT_MS + 60);
  }

  function fireReady() {
    if (readyFired) return;
    readyFired = true;
    /* 最短顯示還沒到就等一下，到了就立刻走 */
    later(dismiss, MIN_SHOW - elapsed());
  }

  /* ============================================================
     3. 保險絲（三重保護的第三層）
     ------------------------------------------------------------
     這兩條完全獨立於上面的流程：就算 ready 永遠不來、
     就算 dismiss 裡的 timer 被作廢，開場也一定會消失。
     （第四層在 splash-boot.js：連這支檔案都沒載到時由它收屍。）
     ============================================================ */
  W.setTimeout(function () { dismiss(); }, FUSE);
  W.setTimeout(function () { hardRemove(); }, FUSE + 1500);

  /* ============================================================
     4. 跟 DOM 接上
     ============================================================ */
  B.onDomReady(function () {
    if (!COLD) {
      /* 熱啟動：開場節點直接拿掉，不留在 DOM 裡 */
      hardRemove();
      afterSplash();
      return;
    }
    if (pendingDismiss) { pendingDismiss = false; dismiss(); return; }
    /* 沒有宣告 hold() 的 app：等 load（所有資源到齊）就收。
       有資料要等的 app 一定要在最上面呼叫 Splash.hold()，
       否則 load 會先到、開場提早走，就蓋不住等待了。 */
    if (!manual) {
      if (D.readyState === "complete") { fireReady(); }
      else { W.addEventListener("load", function () { fireReady(); }, { once: true }); }
    }
  });

  /* ============================================================
     5. 鑰匙圈：開場播完之後才去讀，而且完全不阻塞任何東西
     ------------------------------------------------------------
     ⭐ 讀到的新外觀「不會套用到這一次的畫面」，只寫進快取
        ⇒ 改名要下一次冷啟動才生效。這是刻意的：
        中途換字會看到名字跳動，比晚一次生效難看得多。
        （下一個接手的人請不要把這件事當 bug「修好」。）
     ⭐ 失敗必須完全無感：沒網路、404、JSON 壞掉、格式不符
        一律吞掉、保留舊快取、絕不影響 app。
     ============================================================ */
  function afterSplash() {
    /* 再往後挪一點，確定不會跟收場動畫搶資源 */
    W.setTimeout(refreshFromKeyring, 400);
  }

  function refreshFromKeyring() {
    if (!B.keyringUrl) return;
    if (typeof W.fetch !== "function") return;

    var ctrl = null;
    try { if (typeof W.AbortController === "function") ctrl = new W.AbortController(); } catch (e) {}
    var to = W.setTimeout(function () {
      if (ctrl) { try { ctrl.abort(); } catch (e) {} }
    }, KEYRING_TIMEOUT);

    var opt = { cache: "no-store" };
    if (ctrl) opt.signal = ctrl.signal;

    W.fetch(B.keyringUrl, opt)
      .then(function (r) {
        if (!r || !r.ok) throw new Error("bad response");
        return r.json();
      })
      .then(function (j) {
        W.clearTimeout(to);
        absorb(j);
      })
      .catch(function () {
        W.clearTimeout(to);
        /* 安靜失敗。這裡刻意不 console.error：
           沒網路是常態，不是錯誤，不要在使用者的 console 留紅字。 */
      });
  }

  function absorb(j) {
    var apps = j && j.apps;
    if (!isArr(apps)) return;                 /* 格式不對 → 當作沒發生 */

    var me = null;
    for (var i = 0; i < apps.length; i++) {
      var a = apps[i];
      if (a && typeof a === "object" && a.id === B.appId) { me = a; break; }
    }
    if (!me) return;                          /* 這個 app 還沒登記 → 保留舊快取 */

    var look = B.clean(me.splash);
    if (B.isEmpty(look)) {
      /* 有登記、但沒有（或清空了）splash 設定
         ＝「我不要自訂了」 ⇒ 下次冷啟動回到 app 內建預設 */
      B.dropCache();
      return;
    }
    B.writeCache(look);
  }

  /* ============================================================
     6. 對外 API
     ============================================================ */
  W.Splash = {
    /* 有資料要等的 app：在最上面呼叫，宣告「我會自己說什麼時候好」 */
    hold: function () { manual = true; },
    /* 資料回來（成功或失敗都要）呼叫。失敗也要叫，
       不然開場會變成當機畫面、停到保險絲才收。 */
    ready: function () { fireReady(); },
    dismiss: function () { dismiss(); },
    /* QA／除錯用：看實際套用到的是什麼 */
    state: function () {
      return {
        boot: true,
        cold: COLD,
        look: B.merge(LOOK, {}),
        onAccent: LOOK.accent ? B.onColor(LOOK.accent) : "(用 CSS 預設)",
        fromCache: !B.isEmpty(B.cached),
        dismissed: dismissed,
        elapsed: Math.round(elapsed()),
        /* 開場變體：""（預設印記）或 "light"（白起）。QA 要能直接讀到「我現在演的是哪一版」，
           不要靠時間去猜 —— 猜出來的東西會被機器忙碌度影響。 */
        intro: INTRO,
        minShow: MIN_SHOW,
        reduce: REDUCE,
        cacheKey: B.cacheKey,
        keyringUrl: B.keyringUrl
      };
    }
  };
})();
