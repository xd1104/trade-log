"use strict";
/*
 * keyring-unlock.js — 鑰匙圈解鎖模組（可重用，複製到各 App 的前端就能用）
 *
 * 它做什麼：
 *   到公開的鑰匙圈 repo 抓 keyring.json（裡面只有密文），讓使用者選自己、輸密碼，
 *   在瀏覽器裡用 WebCrypto 解出這個 App 的 GitHub 金鑰，寫進該 App 既有的 localStorage key。
 *   密碼錯 = AES-GCM 驗證失敗（沒有伺服器可以驗，也不需要存密碼 hash）。
 *
 * 別的 App 要用：把這一個檔案複製過去（自帶樣式、零依賴），然後
 *   Keyring.init({ appId:"你的 App id", tokenKey:"你原本存金鑰的 localStorage key",
 *                  onChange:function(){ 重新載入資料並重繪 }, toast:你的 toast });
 *   footer 放 Keyring.chipHtml()；寫入守門處呼叫 Keyring.open("動作名")；
 *   首頁畫完呼叫一次 Keyring.maybeIntro()。
 *
 * ── 解鎖畫面 v3「公版」（2026-08-20，Benson 拍板）──────────────────────────────
 *   解鎖是「進 App 之前」的一層，不屬於任何一個 App 的視覺語言 —— 所以它是
 *   **滿版暖墨深色**，而且**每個 App 長得一模一樣**（同一份 computed style）。
 *   ⚠️ 顏色歸屬鐵律（這是根治「半套主題化」的規則本身）：
 *     1. `#kr-full` 子樹內**一個宿主變數都不准讀**，連 `var(--acc, fallback)` 都不行
 *        （fallback 一樣會造成「一半吃變數、一半寫死」）。模組自己的常數用 `--krs-*`。
 *     2. 唯一的例外是**身分藥丸 `.kr-chip`**（它住在 App 的畫面裡）：
 *        主色只准上**前景**（`.kr-cta` 的文字色），**底色永遠是模組固定的中性色**。
 *        新 App 接進來什麼都不用設；想讓「點我解鎖」跟著 App 走就設一個 `--acc`。
 *   加解密參數與 keyring/server.js 是同一套，改要兩邊一起改。
 *
 * ── 解鎖畫面 v4「質感＋動畫」（2026-08-21，Benson 拍板）─────────────────────────
 *   規格：keyring/DESIGN.md §4（v4 整段）。四條動不得的通則，改這個檔以前一定要讀：
 *     §4-7 容器 class **只准在 paint() 整份指派**（`layer.className = …`），
 *          其他地方**不准 `classList.remove()` 局部拆** —— 拆掉會讓 CSS 規則對
 *          「還活著的元素」重新命中 ⇒ 動畫從第 0 格重播 ⇒ 使用者看到「卡」跟「閃」。
 *          唯一例外是 `applyDensity()`（鍵盤／轉向隨時改高度，拿不掉），
 *          代價是**密度 class（.kr-short/.kr-tiny/.kr-micro）底下不准寫 animation／transition**。
 *     §4-8 為了視覺效果延後某件事之前先問「它有沒有承載狀態」：純視覺可以延後，
 *          文字標籤／可用性／可點區域**不可以**（否則會開出「說謊而且按得下去」的窗）。
 *     §4-9 多段式轉場的時序優先用 CSS `animation-delay` ＋ `fill:both`，不要用 JS 計時器串；
 *          成功路徑的尾段是**零計時器**（只有 closeLayer 的收尾 leaveT 一個，且守衛在 paint()）。
 *     另：`paint()` 的守衛條件必須是 `layer.hidden || layer.classList.contains("kr-leave")`，
 *          **不可以只寫 hidden** —— 退場途中 hidden 還是 false。
 *   `prefers-reduced-motion` 是真的不播（不是播很快），CSS 總閘在檔尾、JS 有四處。
 */
(function (global) {
  var DEFAULT_URL = "https://raw.githubusercontent.com/xd1104/keyring/main/keyring.json";

  var CFG = {
    appId: "",
    tokenKey: "",
    url: DEFAULT_URL,
    ns: "",                 /* 本機記憶的 key 前綴，預設 keyring.<appId>. */
    enabled: true,
    introDelay: 900,
    appName: "",            /* 滿版左上角顯示的 App 名字；沒給就用 document.title（零設定） */
    onChange: null,         /* function(state) 解鎖／換人／金鑰更新後呼叫 */
    toast: null,            /* function(msg, isErr) 借用 App 自己的 toast */
    /* 一個人一個顏色（跨 App 都是同一個顏色）。
     * v3 起頭像改回**滿彩度＋圓形**：滿版是深底，需要彩度才看得見人；
     * 而且「圓形＝人、圓角矩形＝內容（旅程／菜色）」用形狀分語意，顏色就不必退讓。
     * （v2.1 曾把它壓成 20% tint，那是因為當時解鎖 sheet 疊在旅程封面旁邊會撞；
     *   v3 的滿版層畫面上根本沒有封面可以撞 —— 這是刻意反轉，別當 bug 修回去。） */
    themes: {
      sunset: "linear-gradient(135deg,#ff8a80,#ff5f7e 55%,#c94b9d)",
      ocean:  "linear-gradient(135deg,#38c3a7,#2f8fd6)",
      night:  "linear-gradient(135deg,#6a7bf0,#8e54c9)",
      forest: "linear-gradient(135deg,#7ec96f,#3f9d8a)",
      sand:   "linear-gradient(135deg,#f5c65d,#f0855c)"
    }
  };

  var ring = null;          /* 抓回來的 keyring.json */
  var ringErr = "";
  var loading = null;       /* 抓取中的 promise */
  var device = null;        /* {userId,name,emoji,theme,at,k,t,remember} */
  var started = false;
  var layer = null;
  var prevOverflow = null;  /* 開滿版前宿主的 body overflow，關掉時原樣還回去 */
  /* view：unlock（選人／輸密碼）／id（身分頁）／switch（換人確認）——只用來分辨「這是哪一屏」，
   * 狀態機的流程仍然由 step／device 決定（v4 新增，見 screenKey()）。
   * done／shake 是成功與打錯的一次性視覺旗標。 */
  var ui = {
    view: "unlock", step: "who", userId: null, reason: "", tries: 0,
    busy: false, done: false, shake: false, show: false, remember: true, open: false
  };
  /* 下一次 chipHtml() 要不要帶「剛解鎖」的彈出動畫（用完即丟）。
   * 藥丸是**宿主**畫的（onChange → 它自己重繪 footer），模組只能在 HTML 上做記號；
   * 宿主可能等資料載完才重繪（旅途手帳就是先跳「整理行李中…」再畫 footer），
   * 所以記一個時間戳：超過 5 秒才畫的就不彈了 —— 那時候「落點提示」已經沒有意義，
   * 也免得旗標一直留著，害之後某次無關的重繪莫名其妙彈一下。 */
  var chipFresh = false, chipFreshAt = 0;

  /* ---------------- 小工具 ---------------- */
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function grad(t) { return CFG.themes[t] || CFG.themes.sunset; }
  function appLabel() { return CFG.appName || (document && document.title) || ""; }
  function say(msg, isErr) { if (typeof CFG.toast === "function") { try { CFG.toast(msg, isErr); } catch (e) { } } }
  function b64ToBytes(b64) {
    var bin = atob(String(b64 || "").replace(/\s/g, ""));
    var out = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }
  function bytesToB64(buf) {
    var b = new Uint8Array(buf), s = "";
    for (var i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
    return btoa(s);
  }
  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { } }
  function lsDel(k) { try { localStorage.removeItem(k); } catch (e) { } }
  function ssGet(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } }
  function ssSet(k, v) { try { sessionStorage.setItem(k, v); } catch (e) { } }
  function ssDel(k) { try { sessionStorage.removeItem(k); } catch (e) { } }
  function K(name) { return CFG.ns + name; }

  /* ---------------- 加解密（WebCrypto；需要 https 或 localhost） ---------------- */
  function subtle() {
    var c = global.crypto || (typeof crypto !== "undefined" ? crypto : null);
    return c && c.subtle ? c.subtle : null;
  }
  function deriveKey(password, saltB64, iter) {
    var s = subtle();
    if (!s) return Promise.reject(new Error("這個瀏覽器不支援解密（需要 https）"));
    return s.importKey("raw", new TextEncoder().encode(String(password)), { name: "PBKDF2" }, false, ["deriveKey"])
      .then(function (base) {
        return s.deriveKey(
          { name: "PBKDF2", salt: b64ToBytes(saltB64), iterations: iter || 600000, hash: "SHA-256" },
          base, { name: "AES-GCM", length: 256 }, true, ["decrypt"]);
      });
  }
  function decryptEntry(key, entry) {
    var s = subtle();
    return s.decrypt({ name: "AES-GCM", iv: b64ToBytes(entry.iv) }, key, b64ToBytes(entry.cipher))
      .then(function (buf) { return new TextDecoder().decode(buf); });
  }
  function exportKey(key) { return subtle().exportKey("raw", key).then(function (b) { return bytesToB64(b); }); }
  function importKey(b64) {
    return subtle().importKey("raw", b64ToBytes(b64), { name: "AES-GCM", length: 256 }, true, ["decrypt"]);
  }

  /* ---------------- 鑰匙圈讀取 ---------------- */
  /* 每次要用的時候都重新抓（Benson 剛在後台加了人，這邊不重整也要看得到）。
   * 只用很短的 TTL 擋連點狂打；**失敗絕對不快取**，下次開 sheet 要能重試。 */
  var ringAt = 0;
  var RING_TTL = 4000;   /* 只用來擋連點狂打，別長到「他說設好了但還是看不到」 */
  function fetchRing(force) {
    if (!force && ring && (Date.now() - ringAt) < RING_TTL) return Promise.resolve(ring);
    if (loading) return loading;   /* 同一輪抓取共用，別開一堆重複請求 */
    /* raw CDN 會快取好幾分鐘 -> 一定要加 cache-buster */
    var url = CFG.url + (CFG.url.indexOf("?") >= 0 ? "&" : "?") + "t=" + Date.now();
    loading = fetch(url, { cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error("拿不到鑰匙圈（" + r.status + "）");
      return r.json();
    }).then(function (j) {
      ring = j && Array.isArray(j.users) ? j : { version: 1, apps: [], users: [] };
      ringAt = Date.now();
      ringErr = "";
      loading = null;
      return ring;
    }).catch(function (e) {
      ringErr = (e && e.message) || "拿不到鑰匙圈";
      ring = null;
      ringAt = 0;
      loading = null;           /* 失敗不留快取，下次再抓一次 */
      throw e;
    });
    return loading;
  }
  function ringUsers() {
    if (!ring) return [];
    /* 只列得到這個 App 鑰匙的人（沒勾這個 App 的人列出來也解不開東西） */
    return ring.users.filter(function (u) { return u.apps && u.apps[CFG.appId]; });
  }
  function ringUser(id) {
    var list = ring ? ring.users : [];
    for (var i = 0; i < list.length; i++) if (list[i].id === id) return list[i];
    return null;
  }
  /* 同名的人怎麼分辨（純前端，不動後台也不動資料格式）：
   *   後台有填 hint（選填欄位）就用 hint；沒有、但名字在這個 App 的名單裡重複，
   *   就退而顯示 id（ASCII slug，本來就唯一）。名字不重複又沒 hint 就不顯示。 */
  function hintOf(u) {
    if (!u) return "";
    if (u.hint) return u.hint;
    var dup = ringUsers().filter(function (x) { return x.name === u.name; }).length > 1;
    return dup ? (u.id || "") : "";
  }

  /* ---------------- 裝置記憶 ---------------- */
  function readDevice() {
    var raw = ssGet(K("device")) || lsGet(K("device"));
    if (!raw) return null;
    try { return JSON.parse(raw); } catch (e) { return null; }
  }
  function writeDevice(d) {
    device = d;
    var json = JSON.stringify(d);
    if (d.remember) { lsSet(K("device"), json); ssDel(K("device")); }
    else { ssSet(K("device"), json); lsDel(K("device")); }
    writeToken(d.t, d.remember);
  }
  function writeToken(token, remember) {
    if (!CFG.tokenKey) return;
    if (remember) { lsSet(CFG.tokenKey, token); ssDel(CFG.tokenKey); }
    else { ssSet(CFG.tokenKey, token); lsDel(CFG.tokenKey); }
  }
  function forget() {
    device = null;
    lsDel(K("device")); ssDel(K("device"));
    if (CFG.tokenKey) { lsDel(CFG.tokenKey); ssDel(CFG.tokenKey); }
  }

  /* 每次載入都拿目前的鑰匙圈對一次：
   *   人被刪／這個 App 被收回  -> 清掉，回到只看看
   *   密碼被換掉（金鑰解不開）  -> 清掉，回到只看看
   *   只是換了金鑰（PAT 換新）  -> 用記著的金鑰解出新的，自動換過去、不用重解鎖
   * 抓不到鑰匙圈（離線）時什麼都不動，維持現狀。 */
  function refreshFromRing() {
    if (pub.is) return Promise.resolve();   /* 公開模式跟「某個人的密文」無關，這條路不適用 */
    if (!device) return Promise.resolve();
    return fetchRing(true).then(function () {
      var u = ringUser(device.userId);
      var entry = u && u.apps ? u.apps[CFG.appId] : null;
      if (!u || !entry) {
        forget();
        say("鑰匙圈更新過了，這台裝置回到只看看");
        notify();
        return;
      }
      /* 顯示用的資料（名字／emoji／顏色）在後台改過就跟著換。
       * 解鎖時存的是當下的快照，不回頭對的話後台改名這邊永遠是舊的。
       * 金鑰完全不動，所以不用重新輸密碼。 */
      if (device.name !== u.name || device.emoji !== u.emoji || device.theme !== u.theme) {
        device.name = u.name;
        device.emoji = u.emoji;
        device.theme = u.theme;
        writeDevice(device);
        notify();
      }
      if (!device.k) return;
      return importKey(device.k)
        .then(function (key) { return decryptEntry(key, entry); })
        .then(function (token) {
          if (token && token !== device.t) {
            device.t = token;
            writeDevice(device);
            notify();
          }
        })
        .catch(function () {
          forget();
          say("鑰匙換過了，要用新密碼再解一次");
          notify();
        });
    }).catch(function () { /* 離線：維持現狀 */ });
  }
  function notify() { if (typeof CFG.onChange === "function") { try { CFG.onChange(publicState()); } catch (e) { } } }
  /* ==================================================================
   * 公開模式（2026-08-24）
   * ------------------------------------------------------------------
   * 有些 App 用的是免費查詢金鑰（電影評分的 TMDB／OMDb），不是憑證。
   * 後台可以把那種 App 標成「公開」：值不加密，直接放在 keyring.json 的
   * apps[] 裡（`public:true` ＋ `plain:"<值>"`，跟 iv／cipher 明確分開）。
   *
   * 這種 App 在這支模組裡的行為是**整條解鎖流程都不存在**：
   *   ・不彈解鎖層（open／maybeIntro 直接 return）
   *   ・不畫身分藥丸（chipHtml 回空字串）
   *   ・不跟使用者、不跟密碼有任何關係
   *   ・值抓到就直接 writeToken 給宿主，跟解鎖成功那條路走同一個出口
   * 宿主要問的話：Keyring.isPublic() / Keyring.whenReady()。
   * ================================================================== */
  var pub = { is: false, value: null, ready: false };
  var readyWaiters = [];

  function ringApp(id) {
    var list = (ring && Array.isArray(ring.apps)) ? ring.apps : [];
    for (var i = 0; i < list.length; i++) if (list[i] && list[i].id === id) return list[i];
    return null;
  }
  /* 公開的值長什麼樣：apps[] 裡那一筆有 public:true 而且 plain 是字串 */
  function publicValueOf(id) {
    var a = ringApp(id);
    return (a && a.public === true && typeof a.plain === "string") ? a.plain : null;
  }
  function markReady() {
    pub.ready = true;
    var ws = readyWaiters; readyWaiters = [];
    for (var i = 0; i < ws.length; i++) { try { ws[i](publicState()); } catch (e) { } }
  }
  /* 抓到鑰匙圈之後套用公開模式。回傳「值有沒有變」。 */
  function applyPublic() {
    var v = publicValueOf(CFG.appId);
    if (v === null) {
      if (pub.is) {            /* 本來是公開、後台關掉了 → 清掉快取，回到正常解鎖流程 */
        pub.is = false; pub.value = null;
        lsDel(K("pub"));
        if (CFG.tokenKey) { lsDel(CFG.tokenKey); ssDel(CFG.tokenKey); }
        return true;
      }
      return false;
    }
    var changed = !pub.is || pub.value !== v;
    pub.is = true; pub.value = v;
    /* 公開值不是祕密（它本來就放在公開 repo），所以存 localStorage：
       下次開站在網路回來之前就有東西可以用。 */
    lsSet(K("pub"), v);
    writeToken(v, true);
    /* 這個 App 不需要身分了。裝置上如果還留著上一輪解鎖的記憶，靜靜清掉，
       但**不要**走 forget() 那條（它會連 tokenKey 一起刪，把剛寫進去的公開值洗掉）。 */
    if (device) { device = null; lsDel(K("device")); ssDel(K("device")); }
    return changed;
  }

  function publicState() {
    if (pub.is) return { unlocked: true, public: true, ready: pub.ready, userId: null, name: "", emoji: "", theme: "" };
    return device
      ? { unlocked: true, public: false, ready: pub.ready, userId: device.userId, name: device.name, emoji: device.emoji, theme: device.theme }
      : { unlocked: false, public: false, ready: pub.ready };
  }

  /* ---------------- 樣式（只注入一次） ----------------
   * ⚠️ 這份 CSS 是「注入到別人家的 App」裡跑的，宿主自己就有一整套 CSS。權重規則：
   *   ・滿版層內：一律 `#kr-full .kr-x{}`（1,1,0）——綁的是**模組自己的 id**，
   *     不是宿主結構，複製到任何 App 都成立，而且壓得過宿主任何 class 選擇器。
   *   ・滿版層外（只有身分藥丸）：`.kr-chip.kr-chip{}`（0,2,0），同一個 class 寫兩次。
   *   ・**絕對禁止** `.home-foot .kr-chip`／`#app .kr-chip` 這種綁宿主 DOM 的寫法
   *     —— 那樣下一個複製這個檔案的 App 又會壞一次。
   * 血淚：v2.0 的解鎖鈕被自己的 `.kr-sheet button` 通則壓成透明、footer 藥丸被
   *      travel-book 的 `.home-foot button` 壓成灰色小字。 */
  var CSS = ''
    /* ── 滿版容器：所有顏色都是模組自己的常數（--krs-*），一個宿主變數都不讀 ── */
    + '#kr-full{position:fixed; left:0; right:0; z-index:99999;'
    /* v4：iOS 對焦 input 時 Safari 會把 layout viewport 往上捲（visualViewport.offsetTop 40~60），
     * 而 position:fixed 是釘在 layout viewport 的 ⇒ 整層在螢幕上跟著往上跑、頂欄被切、底下露白。
     * fitVH() 把 offsetTop 寫進 --kr-top 抵銷掉它。 */
    + ' top:var(--kr-top,0px);'
    + ' height:100dvh; height:var(--kr-vh,100dvh);'
    + ' display:flex; flex-direction:column;'
    + ' --krs-ink:#f7f2e8; --krs-mut:#a49a8c; --krs-line:rgba(255,255,255,.12);'
    + ' --krs-face:rgba(255,255,255,.055); --krs-facehi:rgba(255,255,255,.10);'
    + ' --krs-btn:#f6efe1; --krs-btn-ink:#241e17;'
    + ' --krs-err-bg:rgba(255,120,105,.14); --krs-err-line:rgba(255,120,105,.30); --krs-err-ink:#ffb3a6;'
    + ' --krs-warn-bg:rgba(255,198,120,.13); --krs-warn-line:rgba(255,198,120,.28); --krs-warn-ink:#f2d5a1;'
    /* v4 新增的兩個常數：
     *  --krs-bleed ＝「層以外」那一圈要畫的顏色（鍵盤讓出來的區域，見 ::before 的 box-shadow）。
     *    刻意比層合成後（約 rgb(36,31,26)）再深一點：讀起來像「層的陰影往外收」而不是一條接縫。
     *  --krs-ease  ＝ 全部動畫共用的曲線。 */
    + ' --krs-bleed:#191510; --krs-ease:cubic-bezier(.22,.9,.3,1);'
    /* 刻意不做到 100% 不透明：後面的 App 還看得到形狀但看不清細節 ⇒「你沒有離開 App」。
     * 不支援 backdrop-filter 的瀏覽器就是一片暖墨底，不會壞。 */
    + ' background:rgba(26,21,16,.955); -webkit-backdrop-filter:blur(18px); backdrop-filter:blur(18px);'
    + ' color:var(--krs-ink);'
    + ' font-family:-apple-system,BlinkMacSystemFont,"PingFang TC","Noto Sans TC","Microsoft JhengHei",sans-serif;'
    + ' font-size:15px; line-height:1.5; letter-spacing:normal; text-align:left;'
    /* ⬇ 把「會被繼承」的屬性全部鎖死。這是跟「權重被壓」不同的第二條滲漏路徑：
     * 宿主在 html/body/`*` 上設的繼承屬性會直接流進來，選擇器權重擋不住它（因為模組根本沒宣告）。
     * 實測抓到的：travel 有 `-webkit-tap-highlight-color:transparent`、recipe 沒有
     * ⇒ 同一顆頭像磚在 iPhone 上一個 App 點下去會閃灰、另一個不會；`text-size-adjust` 也是 auto vs 100%。
     * 公版的定義是「兩個 App 逐項相同」，所以這裡寧可寫多也不要漏。 */
    + ' box-sizing:border-box; -webkit-tap-highlight-color:transparent;'
    + ' -webkit-text-size-adjust:100%; text-size-adjust:100%;'
    + ' font-weight:400; font-style:normal; font-variant:normal; font-variant-numeric:normal;'
    + ' font-stretch:normal; text-transform:none; text-indent:0; text-shadow:none;'
    + ' word-spacing:normal; white-space:normal; word-break:normal; overflow-wrap:normal;'
    + ' direction:ltr; cursor:default; visibility:visible; list-style:none; tab-size:4; hyphens:manual;'
    + ' -webkit-font-smoothing:auto; -moz-osx-font-smoothing:auto;'
    + ' -webkit-touch-callout:default; -webkit-user-select:auto; user-select:auto;'
    + ' animation:kr-in .26s var(--krs-ease);}'
    + '#kr-full[hidden]{display:none;}'
    /* ::before 一層做兩件事：
     *  (1) 頂部暖光（跟各 App 的暖米白是同一個色溫的兩端：白天／關燈）＋ 底部極淡收邊
     *  (2) box-shadow 的大 spread 把「層以外」整片畫成 --krs-bleed —— 鍵盤彈出時 --kr-vh 縮小，
     *      層底下那一段本來沒有任何元素去畫、會露出宿主的白（Benson 說的「下面會是白色的」）。
     *      ⚠️ 不能改用 position:fixed 的子元素去補：#kr-full 有 backdrop-filter，
     *         會成為 fixed 子孫的 containing block，逃不出去（試過，死路）。
     *      100vmax 是**陰影擴散半徑**不是尺寸，不需要跟著鍵盤變，跟「不可用 100vh」那條無關。
     *      ⚠️ 底部那道 vignette 不要調深（現在 .16）：調到 .30 以上就會跟 bleed 對出一條看得見的線。 */
    + '#kr-full::before{content:""; position:absolute; inset:0; pointer-events:none;'
    + ' background:radial-gradient(120% 70% at 50% -14%, rgba(255,203,140,.16), rgba(255,203,140,0) 62%),'
    + ' radial-gradient(150% 95% at 50% 116%, rgba(0,0,0,.16), rgba(0,0,0,0) 60%);'
    + ' box-shadow:0 0 0 100vmax var(--krs-bleed);}'
    /* ── 動畫的原料（全部尊重 prefers-reduced-motion，總閘在檔尾） ── */
    + '@keyframes kr-in{from{opacity:0; transform:scale(1.02);} to{opacity:1; transform:none;}}'
    + '@keyframes kr-sp{to{transform:rotate(360deg);}}'
    + '@keyframes kr-rise{from{opacity:0; transform:translate3d(0,9px,0);} to{opacity:1; transform:none;}}'
    + '@keyframes kr-pop{from{opacity:0; transform:translate3d(0,10px,0) scale(.96);} to{opacity:1; transform:none;}}'
    + '@keyframes kr-fade{from{opacity:0;} to{opacity:1;}}'
    + '@keyframes kr-ring{from{opacity:.85; transform:scale(1);} to{opacity:0; transform:scale(1.5);}}'
    + '@keyframes kr-okpop{0%{transform:scale(1);} 42%{transform:scale(1.035);} 100%{transform:scale(1);}}'
    + '@keyframes kr-nudge{0%,100%{transform:translateX(0);} 18%{transform:translateX(-6px);}'
    + ' 40%{transform:translateX(5px);} 62%{transform:translateX(-3px);} 82%{transform:translateX(2px);}}'
    + '@keyframes kr-out{to{opacity:0; transform:scale(1.03);}}'
    + '@keyframes kr-outdn{to{opacity:0; transform:scale(.985);}}'
    + '@keyframes kr-tick{from{transform:scale(.4); opacity:0;} to{transform:scale(1); opacity:1;}}'
    /* ⚠️ pointer-events 排進 @keyframes 是**刻意的離散動畫技巧**，不是誤植：
     * kr-chipin 帶 .24s delay ＋ fill:both ⇒ 延遲期間停在第 0 格（opacity:0），
     * 那段時間藥丸看不見，就不該點得到；動畫一開始播（1%）就自己恢復，不需要任何 JS 收尾。
     * 萬一某個瀏覽器忽略 keyframes 裡的 pointer-events，退化行為只是
     * 「藥丸提早 240ms 就可以點」——那 240ms 內它的文字已經是正確狀態，不會壞。 */
    + '@keyframes kr-chipin{from{opacity:0; transform:translateY(7px) scale(.94); pointer-events:none;}'
    + ' 1%{pointer-events:auto;} to{opacity:1; transform:none; pointer-events:auto;}}'
    + '@keyframes kr-chipglow{from{box-shadow:0 0 0 0 rgba(120,96,60,.34);} to{box-shadow:0 0 0 15px rgba(120,96,60,0);}}'
    + '#kr-full *{box-sizing:border-box;}'
    + '#kr-full button{font-family:inherit; font-size:15px; border:none; background:none; cursor:pointer;'
    + ' color:inherit; padding:0; margin:0; line-height:1.4; text-align:inherit;'
    + ' -webkit-appearance:none; appearance:none; box-shadow:none;}'
    /* checkbox 雖然是視覺隱藏的（1×1 透明），font-size 還是會被宿主的 `input{}` 決定
     * ⇒ 兩個 App 量到 13.33px vs 16px。一律鎖 16px（順便符合 iOS 不自動放大那條）。 */
    + '#kr-full input{font-family:inherit; font-size:16px; font-weight:400; line-height:normal;'
    + ' letter-spacing:normal; -webkit-appearance:none; appearance:none;}'
    /* ── 上／中／下三段：全篇不用 position:fixed 的子元素 ──
     * 鍵盤彈出時 .kr-foot 自然停在鍵盤正上方。這是刻意的，不要「優化」成 fixed。 */
    + '#kr-full .kr-top{flex:0 0 auto; display:flex; align-items:center; justify-content:space-between; gap:10px;'
    + ' width:100%; max-width:1120px; margin:0 auto; position:relative; z-index:1;'
    + ' padding:calc(10px + env(safe-area-inset-top)) max(14px, env(safe-area-inset-right)) 4px max(14px, env(safe-area-inset-left));}'
    + '#kr-full .kr-app{font-size:13px; font-weight:700; color:var(--krs-mut); white-space:nowrap;'
    + ' overflow:hidden; text-overflow:ellipsis;}'
    /* v4：手機沒有 hover，每個可按的東西都要有 :active，否則按下去完全沒有回饋 */
    + '#kr-full .kr-back{min-height:44px; padding:0 12px 0 0; font-size:14.5px; font-weight:700;'
    + ' color:var(--krs-mut); display:inline-flex; align-items:center;'
    + ' transition:color .14s var(--krs-ease);}'
    + '#kr-full .kr-back:hover, #kr-full .kr-back:active{color:var(--krs-ink);}'
    + '#kr-full .kr-x{min-width:46px; min-height:46px; font-size:19px; color:var(--krs-mut); flex:0 0 auto;'
    + ' border-radius:99px; display:flex; align-items:center; justify-content:center;'
    + ' transition:color .14s var(--krs-ease), background-color .14s var(--krs-ease), transform .14s var(--krs-ease);}'
    + '#kr-full .kr-x:hover{background-color:var(--krs-face); color:var(--krs-ink);}'
    + '#kr-full .kr-x:active{transform:scale(.9); color:var(--krs-ink);}'
    + '#kr-full .kr-main{flex:1 1 auto; overflow-y:auto; -webkit-overflow-scrolling:touch; position:relative; z-index:1;'
    + ' display:flex; flex-direction:column;}'
    /* flex:1 0 auto ⇒ 內容短時垂直置中、內容長時撐開可捲、不會截頂 */
    + '#kr-full .kr-mid{flex:1 0 auto; display:flex; align-items:center; justify-content:center;'
    + ' padding:6px max(18px, env(safe-area-inset-right)) 18px max(18px, env(safe-area-inset-left));}'
    + '#kr-full .kr-wrap{width:100%; max-width:560px;}'
    + '#kr-full .kr-wrap.narrow{max-width:380px;}'
    + '#kr-full .kr-foot{flex:0 0 auto; border-top:1px solid var(--krs-line); position:relative; z-index:1;'
    + ' padding:0 max(14px, env(safe-area-inset-right)) calc(6px + env(safe-area-inset-bottom)) max(14px, env(safe-area-inset-left));}'
    /* ── 標題區 ── */
    + '#kr-full .kr-h{font-size:25px; font-weight:800; margin:0 0 6px; letter-spacing:-.2px; color:var(--krs-ink);}'
    + '#kr-full .kr-sub{font-size:14.5px; color:var(--krs-mut); margin:0 0 18px; line-height:1.6;}'
    /* ⚠️ v4 鐵律：`background-color` 與 `background-image` 一定要分開寫，禁止用 `background`
     * 簡寫接漸層 —— 簡寫會把 computed 的 background-color 變成 transparent，那正是 v2.0
     * 那顆隱形解鎖鈕的死法，而且 lab-qa 量的就是 background-color。沒接漸層的也一律寫
     * background-color，免得以後有人加 image 時踩到同一顆雷。 */
    + '#kr-full .kr-why{background-color:var(--krs-facehi); border:1px solid var(--krs-line); color:var(--krs-ink);'
    + ' font-size:13.5px; font-weight:600; border-radius:13px; padding:11px 14px; margin:0 0 16px; line-height:1.55;}'
    /* ── 選人磚：圓形頭像＝人（跟 App 裡圓角矩形的旅程／菜色卡分得開） ── */
    + '#kr-full .kr-grid{display:flex; flex-wrap:wrap; gap:10px; justify-content:center;}'
    /* v4：底色從純平面改成「上緣一道極淡高光 ＋ 外投影」，深底上才有材質、不再是黑框框 */
    + '#kr-full .kr-tile{flex:0 0 calc(50% - 5px); min-height:126px; display:flex; flex-direction:column;'
    + ' align-items:center; justify-content:center; gap:9px; padding:14px 10px; border-radius:18px;'
    + ' background-color:var(--krs-face);'
    + ' background-image:linear-gradient(180deg, rgba(255,255,255,.05), rgba(255,255,255,0) 62%);'
    + ' border:1px solid var(--krs-line); color:var(--krs-ink); text-align:center;'
    + ' box-shadow:inset 0 1px 0 rgba(255,255,255,.07), 0 2px 6px rgba(0,0,0,.16);'
    + ' transition:background-color .16s var(--krs-ease), transform .16s var(--krs-ease), box-shadow .18s var(--krs-ease);}'
    + '#kr-full .kr-tile:hover{background-color:var(--krs-facehi);'
    + ' box-shadow:inset 0 1px 0 rgba(255,255,255,.10), 0 6px 18px rgba(0,0,0,.22);}'
    + '#kr-full .kr-tile:active{transform:scale(.965);'
    + ' box-shadow:inset 0 1px 0 rgba(255,255,255,.07), 0 1px 3px rgba(0,0,0,.18);}'
    + '#kr-full .kr-grid.many .kr-tile{flex:0 0 calc(33.333% - 7px); min-height:112px;}'
    + '#kr-full .kr-grid.many .kr-av{width:54px; height:54px; font-size:25px;}'
    /* v4：position:relative 是給成功光圈（.kr-ok::after）用的；
     * 三層 shadow 讓頭像從「貼紙」變成「有受光的球」 */
    + '#kr-full .kr-av{position:relative; width:66px; height:66px; border-radius:50%; display:flex; align-items:center;'
    + ' justify-content:center; font-size:30px; line-height:1; flex:0 0 auto;'
    + ' box-shadow:0 4px 14px rgba(0,0,0,.28), inset 0 1px 0 rgba(255,255,255,.30),'
    + ' inset 0 0 0 1px rgba(255,255,255,.14);}'
    /* 解鎖成功那一刻，頭像亮一圈（第一拍的「確認」） */
    + '#kr-full .kr-av.kr-ok::after{content:""; position:absolute; inset:-5px; border-radius:50%;'
    + ' border:2px solid rgba(247,242,232,.7); animation:kr-ring .55s ease-out forwards;}'
    + '#kr-full .kr-tx{display:block; width:100%; min-width:0;}'
    + '#kr-full .kr-nm{font-size:14.5px; font-weight:700; line-height:1.35; max-width:100%;'
    + ' overflow:hidden; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; word-break:break-word;}'
    + '#kr-full .kr-hint{display:block; font-size:11.5px; font-weight:500; color:var(--krs-mut); margin-top:3px;'
    + ' white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:100%;}'
    /* ── 「先看看就好」＝畫面第二個焦點，不是角落小連結。每一屏都要有。 ── */
    + '#kr-full .kr-peek{display:block; width:100%; min-height:62px; padding:10px 14px; border-radius:14px;'
    + ' text-align:center; color:var(--krs-ink); transition:background-color .16s var(--krs-ease);}'
    + '#kr-full .kr-peek:hover, #kr-full .kr-peek:active{background-color:var(--krs-face);}'
    + '#kr-full .kr-peek b{display:block; font-size:15.5px; font-weight:700;}'
    + '#kr-full .kr-peek span{display:block; font-size:12.5px; color:var(--krs-mut); margin-top:2px;}'
    /* ── 身分列（輸密碼／已解鎖） ── */
    /* ⚠️ 副標一定要寫成 `.kr-id div span`（1,1,2）而不是 `.kr-id span`（1,1,1）：
     * 頭像本身也是 .kr-id 底下的 <span>，寫成後者會壓過 `#kr-full .kr-av`（1,1,0），
     * 66px 的圓頭像就會變成 13.5px 灰字、emoji 縮在左上角。（demo 有這個洞，實測抓到） */
    + '#kr-full .kr-id{display:flex; flex-direction:column; align-items:center; text-align:center; gap:10px; margin:0 0 20px;}'
    + '#kr-full .kr-id b{display:block; font-size:21px; font-weight:800;}'
    + '#kr-full .kr-id div span{display:block; font-size:13.5px; color:var(--krs-mut); margin-top:4px;}'
    /* ── 密碼欄（font-size 16px：iOS 才不會自動放大） ── */
    + '#kr-full .kr-field{display:block; position:relative; margin:0 0 12px;}'
    /* 打錯密碼時抖一下。只有真的打錯那一次會抖（ui.shake 一次性旗標，drawPw() 開頭消費掉） */
    + '#kr-full .kr-field.kr-shake{animation:kr-nudge .36s ease both;}'
    + '#kr-full .kr-field input{width:100%; min-height:54px; font-size:16px; font-weight:500; line-height:normal;'
    + ' padding:0 56px 0 16px; margin:0; border-radius:14px; border:1px solid var(--krs-line);'
    + ' background-color:var(--krs-face); color:var(--krs-ink); outline:none; box-shadow:none;'
    + ' transition:border-color .16s var(--krs-ease), background-color .16s var(--krs-ease), box-shadow .16s var(--krs-ease);}'
    + '#kr-full .kr-field input::placeholder{color:var(--krs-mut); opacity:1;}'
    + '#kr-full .kr-field input:focus{border-color:rgba(255,255,255,.45); background-color:var(--krs-facehi);'
    + ' box-shadow:0 0 0 3px rgba(255,255,255,.07);}'
    + '#kr-full .kr-eye{position:absolute; right:3px; top:50%; transform:translateY(-50%); width:48px; height:48px;'
    + ' display:flex; align-items:center; justify-content:center; font-size:17px; border-radius:12px;'
    + ' transition:background-color .14s var(--krs-ease);}'
    + '#kr-full .kr-eye:hover, #kr-full .kr-eye:active{background-color:var(--krs-face);}'
    /* 錯誤條自己帶 kr-rise：每次重繪都是新元素，自然會跑（不必用 JS 排時序） */
    + '#kr-full .kr-err{background-color:var(--krs-err-bg); border:1px solid var(--krs-err-line); color:var(--krs-err-ink);'
    + ' font-size:13.5px; font-weight:700; border-radius:13px; padding:11px 14px; margin:0 0 12px; line-height:1.5;'
    + ' animation:kr-rise .22s var(--krs-ease) both;}'
    + '#kr-full .kr-err span{display:block; font-weight:500; font-size:12.5px; margin-top:4px; opacity:.85;}'
    + '#kr-full .kr-warn{background-color:var(--krs-warn-bg); border:1px solid var(--krs-warn-line); color:var(--krs-warn-ink);'
    + ' font-size:13.5px; border-radius:14px; padding:12px 14px; margin:0 0 16px; line-height:1.6;}'
    /* ── 勾選 ── */
    + '#kr-full .kr-check{display:flex; align-items:center; gap:11px; min-height:50px; margin:0; cursor:pointer;'
    + ' position:relative; text-align:left;}'
    + '#kr-full .kr-check input{position:absolute; opacity:0; width:1px; height:1px; margin:0;}'
    + '#kr-full .kr-box{width:26px; height:26px; border-radius:8px; border:1.5px solid var(--krs-mut);'
    + ' background-color:transparent; flex:0 0 auto; display:flex; align-items:center; justify-content:center;'
    + ' font-size:15px; color:var(--krs-btn-ink); line-height:1;'
    + ' transition:background-color .16s var(--krs-ease), border-color .16s var(--krs-ease);}'
    + '#kr-full .kr-check input:checked + .kr-box{background-color:var(--krs-btn); border-color:var(--krs-btn);}'
    + '#kr-full .kr-check input:checked + .kr-box::after{content:"\\2713";}'
    /* 打勾的彈跳只掛在 .kr-tickable 上，而那個 class 只有在使用者「真的動到勾選框」時才加
     * （onchange → Keyring.tick）。勾選框預設就是打勾的，paint() 每次重繪都產生新元素 ⇒
     * 不加這道閘的話，光是按「顯示密碼」勾勾就會跟著彈一次（實測按 9 次彈 9 次）。
     * class 加上去就不拆，元素重繪時自然跟著死。 */
    + '#kr-full .kr-check input:checked + .kr-box.kr-tickable::after{animation:kr-tick .2s var(--krs-ease) both;}'
    + '#kr-full .kr-lb{font-size:15px; font-weight:600;}'
    + '#kr-full .kr-lb small{display:block; font-size:12.5px; color:var(--krs-mut); font-weight:500;'
    + ' margin-top:2px; line-height:1.5;}'
    /* ── 按鈕：主鈕是暖白底＋深字，刻意不是珊瑚／橘（那是 App 的顏色，這一層沒有） ──
     * ⭐ 2026-08-24：這三顆（.kr-go/.kr-ghost/.kr-danger）以前**沒有宣告自己的 padding**，
     *   宿主只要有一條 `button{padding:0}` 這種 reset，字就會貼著邊——好雷嗎的 QA 連兩輪抓到。
     *   ⚠️ 這不是權重輸了：`#kr-full .kr-x` 是 (1,1,0)，壓得過宿主任何 `button{}`／`.btn{}`。
     *      **是我們根本沒寫那個屬性**，沒寫就沒有東西可以壓，宿主的宣告直接生效。
     *      所以修法是「把會被繼承／被 reset 的屬性通通寫死」，不是再加一次 class：
     *      層內的權重規則照舊維持 `#kr-full .kr-x{}`（見檔頭 CSS 那段的約定），
     *      層外的藥丸才需要 `.kr-chip.kr-chip` 自我加倍——那裡沒有 id 可以綁。
     *   以後在這一層新增任何按鈕，照抄下面這組「宿主滲漏防護」。 */
    + '#kr-full .kr-go{width:100%; min-height:54px; border-radius:15px;'
    + ' background-color:var(--krs-btn);'
    + ' background-image:linear-gradient(180deg, rgba(255,255,255,.60), rgba(255,255,255,0) 58%);'
    + ' color:var(--krs-btn-ink); font-size:16.5px; font-weight:800; display:flex; align-items:center;'
    + ' justify-content:center; gap:9px; margin:6px 0 0; padding:0 18px;'
    + ' font-family:-apple-system,BlinkMacSystemFont,"PingFang TC","Noto Sans TC","Microsoft JhengHei",sans-serif;'
    + ' line-height:1.3; letter-spacing:normal; text-align:center; text-decoration:none; text-transform:none;'
    + ' text-indent:0; text-shadow:none; word-spacing:normal; font-style:normal; font-variant:normal;'
    + ' box-sizing:border-box; cursor:pointer; -webkit-appearance:none; appearance:none; border:0;'
    + ' -webkit-tap-highlight-color:transparent; -webkit-text-size-adjust:100%; text-size-adjust:100%;'
    + ' box-shadow:0 6px 20px rgba(0,0,0,.30), inset 0 -1px 0 rgba(0,0,0,.05);'
    + ' transition:transform .14s var(--krs-ease), box-shadow .18s var(--krs-ease), opacity .18s;}'
    + '#kr-full .kr-go:active{transform:scale(.985); box-shadow:0 3px 10px rgba(0,0,0,.26);}'
    + '#kr-full .kr-go[disabled]{opacity:.66;}'
    /* 成功第一拍：「✓ 解開了」。要連 [disabled] 那條一起蓋掉，否則會變暗看起來像失敗 */
    + '#kr-full .kr-go.kr-ok, #kr-full .kr-go.kr-ok[disabled]{opacity:1; animation:kr-okpop .38s var(--krs-ease);}'
    + '#kr-full .kr-ghost{width:100%; min-height:54px; border-radius:15px; border:1px solid var(--krs-line);'
    + ' background-color:var(--krs-face); background-image:none; color:var(--krs-ink); font-size:16px;'
    + ' font-weight:700; display:flex; align-items:center; justify-content:center; gap:8px;'
    + ' margin:0; padding:0 18px;'
    + ' font-family:-apple-system,BlinkMacSystemFont,"PingFang TC","Noto Sans TC","Microsoft JhengHei",sans-serif;'
    + ' line-height:1.3; letter-spacing:normal; text-align:center; text-decoration:none; text-transform:none;'
    + ' text-indent:0; text-shadow:none; word-spacing:normal; font-style:normal; font-variant:normal;'
    + ' box-sizing:border-box; cursor:pointer; -webkit-appearance:none; appearance:none;'
    + ' -webkit-tap-highlight-color:transparent; -webkit-text-size-adjust:100%; text-size-adjust:100%;'
    + ' transition:transform .14s var(--krs-ease), background-color .16s var(--krs-ease);}'
    + '#kr-full .kr-ghost:hover{background-color:var(--krs-facehi);}'
    + '#kr-full .kr-ghost:active{transform:scale(.985);}'
    + '#kr-full .kr-danger{width:100%; min-height:54px; border-radius:15px; background-color:var(--krs-err-bg);'
    + ' background-image:none; border:1px solid var(--krs-err-line); color:var(--krs-err-ink); font-size:16px;'
    + ' font-weight:700; display:flex; align-items:center; justify-content:center;'
    + ' margin:0; padding:0 18px;'
    + ' font-family:-apple-system,BlinkMacSystemFont,"PingFang TC","Noto Sans TC","Microsoft JhengHei",sans-serif;'
    + ' line-height:1.3; letter-spacing:normal; text-align:center; text-decoration:none; text-transform:none;'
    + ' text-indent:0; text-shadow:none; word-spacing:normal; font-style:normal; font-variant:normal;'
    + ' box-sizing:border-box; cursor:pointer; -webkit-appearance:none; appearance:none;'
    + ' -webkit-tap-highlight-color:transparent; -webkit-text-size-adjust:100%; text-size-adjust:100%;'
    + ' transition:transform .14s var(--krs-ease);}'
    + '#kr-full .kr-danger:active{transform:scale(.985);}'
    + '#kr-full .kr-spin{width:17px; height:17px; border-radius:50%; border:2.5px solid rgba(0,0,0,.22);'
    + ' border-top-color:var(--krs-btn-ink); animation:kr-sp .7s linear infinite;}'
    + '#kr-full .kr-empty{text-align:center; font-size:15px; color:var(--krs-mut); line-height:1.85;}'
    + '#kr-full .kr-empty .big{font-size:42px; display:block; margin-bottom:10px;}'
    + '#kr-full .kr-stack{display:flex; flex-direction:column; gap:10px; margin-top:18px;}'
    /* ── 進場層次（.kr-a-open）：只有「這一層剛打開」才跑整套 ──────────────────
     * 由 paint() 判斷要不要掛這個 class；同一屏的重繪（顯示密碼／打錯／送出中）完全不動，
     * 否則按一下顯示密碼整屏就重新蹦一次 —— 那才是真的廉價。 */
    + '#kr-full.kr-a-open .kr-top{animation:kr-fade .30s ease both .06s;}'
    + '#kr-full.kr-a-open .kr-wrap > *{animation:kr-rise .34s var(--krs-ease) both;}'
    + '#kr-full.kr-a-open .kr-wrap > *:nth-child(1){animation-delay:.05s;}'
    + '#kr-full.kr-a-open .kr-wrap > *:nth-child(2){animation-delay:.09s;}'
    + '#kr-full.kr-a-open .kr-wrap > *:nth-child(3){animation-delay:.13s;}'
    + '#kr-full.kr-a-open .kr-wrap > *:nth-child(n+4){animation-delay:.16s;}'
    /* 磚要各自動，所以整個 .kr-grid 不動。⚠️ 這條必須排在上面 :nth-child 那幾條**之後**
     * （同為 (1,3,0)，靠源序決勝）。 */
    + '#kr-full.kr-a-open .kr-wrap > .kr-grid{animation:none;}'
    + '#kr-full.kr-a-open .kr-tile{animation:kr-pop .34s var(--krs-ease) both;'
    + ' animation-delay:calc(.09s + var(--kr-i,0) * .032s);}'
    + '#kr-full.kr-a-open .kr-foot{animation:kr-rise .34s var(--krs-ease) both .17s;}'
    /* ── 換屏（.kr-a-step）：頂欄與底條留在原地不動，只有中段換 ── */
    + '#kr-full.kr-a-step .kr-wrap > *{animation:kr-rise .26s var(--krs-ease) both;}'
    + '#kr-full.kr-a-step .kr-wrap > *:nth-child(2){animation-delay:.04s;}'
    + '#kr-full.kr-a-step .kr-wrap > *:nth-child(n+3){animation-delay:.07s;}'
    + '#kr-full.kr-a-step .kr-wrap > .kr-grid{animation:none;}'
    + '#kr-full.kr-a-step .kr-tile{animation:kr-pop .28s var(--krs-ease) both;'
    + ' animation-delay:calc(.02s + var(--kr-i,0) * .026s);}'
    /* 頭像共享形變（runMorph 接手）時，.kr-id 自己不要再位移，否則落點會晃。
     * ⚠️ 這個開關掛在 .kr-id 這個「元素」身上（.kr-nofx），**不是**掛在 #kr-full 的容器 class 上：
     *   容器 class 只要被 remove，上面 .kr-a-step 那條 `.kr-wrap > *` 就會對「還活著的 .kr-id」
     *   重新命中，瀏覽器當成全新動畫從第 0 格再播一次 ⇒ 畫面明明安定了、身分列又自己動一下＝「卡」，
     *   而且 kr-rise 第一格是 opacity:0 ⇒ 還會「閃一下」。（v4 第一版就是這樣被退件的，見 §4-7。）
     *   元素級 class 跟著 innerHTML 一起生、一起死，規則沒有機會重新命中。
     * ⚠️ 這兩條必須排在 .kr-a-step 那幾條**之後**。 */
    + '#kr-full .kr-wrap > .kr-id.kr-nofx{animation:none;}'
    + '#kr-full .kr-id.kr-nofx > div{animation:kr-rise .28s var(--krs-ease) both .06s;}'
    /* 離場：解鎖成功往前退開（像門打開）、使用者自己關掉往後收。
     * .kr-leave 帶 pointer-events:none ⇒ 底下的 App 馬上可以點。 */
    + '#kr-full.kr-leave{pointer-events:none; animation:kr-outdn .19s ease-in forwards;}'
    + '#kr-full.kr-leave.kr-up{animation:kr-out .22s ease-in forwards;}'
    /* ── 電腦版：置中一欄、磚變小顆橫排，1 人／8 人都不會變荒原 ── */
    + '@media(min-width:900px){'
    +   '#kr-full .kr-h{font-size:32px; text-align:center;}'
    +   '#kr-full .kr-sub{font-size:15.5px; text-align:center; margin-bottom:26px;}'
    +   '#kr-full .kr-why{text-align:center;}'
    +   '#kr-full .kr-wrap{max-width:700px;}'
    +   '#kr-full .kr-wrap.narrow{max-width:400px;}'
    +   '#kr-full .kr-tile{flex:0 0 152px; min-height:150px;}'
    +   '#kr-full .kr-grid.many .kr-tile{flex:0 0 152px; min-height:150px;}'
    +   '#kr-full .kr-grid.many .kr-av{width:76px; height:76px; font-size:34px;}'
    +   '#kr-full .kr-av{width:76px; height:76px; font-size:34px;}'
    +   '#kr-full .kr-nm{font-size:15px;}'
    +   '#kr-full .kr-foot{padding-bottom:calc(10px + env(safe-area-inset-bottom));}'
    +   '#kr-full .kr-peek{max-width:520px; margin:0 auto;}'
    + '}'
    /* ── 矮螢幕密度（.kr-short / .kr-tiny / .kr-micro）──────────────────────────
     * ⚠️ **絕對不要改回 `@media(max-height:…)`**：媒體查詢吃的是 **CSS 視窗高度**，
     * 而 **iOS 鍵盤彈出時只縮 `visualViewport`、CSS 視窗高度一動也不動** ⇒ 媒體查詢永遠不觸發，
     * Android 會過、iPhone 不會（QA 退件實證：`--kr-vh` 設 407 時解鎖鈕可見高度 0px）。
     * 所以密度等級由 `applyDensity()` 依**實際量到的滿版高度**加／移除 class，
     * 跟 `--kr-vh`／`fitVH()` 走同一個高度來源；ResizeObserver 保證任何原因造成的高度變化都會重算。
     * 門檻是用**最壞情境**（理由條 ＋ 已打錯 2 次，錯誤條有兩行）定出來的，不是用單純情境。
     * ⚠️ **這一整段底下只准寫尺寸與間距，不准寫 `animation`／`transition`。**
     *    密度 class 是全檔唯一「在元素活著的時候 add/remove」的地方（applyDensity()，
     *    鍵盤／轉向隨時會改高度，拿不掉），一旦有人在這底下掛了動畫，鍵盤一動就會重播（§4-7）。
     *    這條有機器檢查在守：`node tools/check-unlock.js`（A. 密度 class 不得含動畫）。 */
    + '#kr-full.kr-short .kr-top{padding-top:calc(6px + env(safe-area-inset-top)); padding-bottom:2px;}'
    + '#kr-full.kr-short .kr-mid{padding-top:4px; padding-bottom:12px;}'
    + '#kr-full.kr-short .kr-h{font-size:21px; margin-bottom:4px;}'
    + '#kr-full.kr-short .kr-sub{font-size:13.5px; margin-bottom:12px;}'
    + '#kr-full.kr-short .kr-why{margin-bottom:10px; padding:9px 12px;}'
    + '#kr-full.kr-short .kr-tile{min-height:104px; gap:7px; padding:10px 8px;}'
    + '#kr-full.kr-short .kr-av{width:54px; height:54px; font-size:25px;}'
    + '#kr-full.kr-short .kr-grid.many .kr-tile{min-height:96px;}'
    + '#kr-full.kr-short .kr-grid.many .kr-av{width:44px; height:44px; font-size:21px;}'
    /* 身分區從直排改橫排：這是矮螢幕省高度最有效的一刀 */
    + '#kr-full.kr-short .kr-id{flex-direction:row; align-items:center; text-align:left; gap:12px; margin-bottom:14px;}'
    + '#kr-full.kr-short .kr-id .kr-av{width:48px; height:48px; font-size:23px;}'
    + '#kr-full.kr-short .kr-id b{font-size:18px;}'
    + '#kr-full.kr-short .kr-field{margin-bottom:10px;}'
    + '#kr-full.kr-short .kr-field input{min-height:50px;}'
    + '#kr-full.kr-short .kr-err{margin-bottom:10px; padding:9px 12px;}'
    + '#kr-full.kr-short .kr-check{min-height:46px;}'
    + '#kr-full.kr-short .kr-go, #kr-full.kr-short .kr-ghost, #kr-full.kr-short .kr-danger{min-height:50px;}'
    + '#kr-full.kr-short .kr-stack{margin-top:14px; gap:8px;}'
    + '#kr-full.kr-short .kr-peek{min-height:54px; padding:8px 14px;}'
    /* .kr-tiny（≤500）：副標讓路。順序要在 .kr-short 之後 —— 同權重靠後者勝出。 */
    + '#kr-full.kr-tiny .kr-x{min-width:44px; min-height:44px;}'
    + '#kr-full.kr-tiny .kr-mid{padding-top:2px; padding-bottom:8px;}'
    + '#kr-full.kr-tiny .kr-h{font-size:19px;}'
    + '#kr-full.kr-tiny .kr-sub{margin-bottom:10px;}'
    + '#kr-full.kr-tiny .kr-why{margin-bottom:8px; padding:8px 11px; font-size:13px;}'
    + '#kr-full.kr-tiny .kr-id{gap:10px; margin-bottom:10px;}'
    + '#kr-full.kr-tiny .kr-id .kr-av{width:32px; height:32px; font-size:17px;}'
    + '#kr-full.kr-tiny .kr-id b{font-size:16.5px;}'
    + '#kr-full.kr-tiny .kr-id div span{display:none;}'
    + '#kr-full.kr-tiny .kr-lb small{display:none;}'
    + '#kr-full.kr-tiny .kr-field{margin-bottom:8px;}'
    + '#kr-full.kr-tiny .kr-field input{min-height:48px;}'
    + '#kr-full.kr-tiny .kr-err{margin-bottom:8px; padding:8px 11px; font-size:13px;}'
    + '#kr-full.kr-tiny .kr-err span{font-size:11.5px; margin-top:2px;}'
    + '#kr-full.kr-tiny .kr-check{min-height:44px;}'
    + '#kr-full.kr-tiny .kr-go, #kr-full.kr-tiny .kr-ghost, #kr-full.kr-tiny .kr-danger{min-height:48px; margin-top:4px;}'
    + '#kr-full.kr-tiny .kr-tile{min-height:92px;}'
    + '#kr-full.kr-tiny .kr-av{width:44px; height:44px; font-size:21px;}'
    + '#kr-full.kr-tiny .kr-peek{min-height:50px; padding:5px 12px;}'
    + '#kr-full.kr-tiny .kr-peek b{font-size:14.5px;}'
    + '#kr-full.kr-tiny .kr-peek span{font-size:11.5px; margin-top:1px;}'
    /* .kr-micro（≤460）＝鍵盤佔掉大半個畫面。這一階刻意讓掉三樣**次要**的東西，
     * 換「密碼欄＋解鎖鈕＋出口」完整可見（它們是這一屏唯一的任務）：
     *   ① 理由條（上一屏已經看過同一句話）② 錯誤條的第二行說明 ③ 出口的第二行說明。
     * 觸控目標仍然全部 ≥44px、密碼欄仍然 16px —— 那兩條不准讓。 */
    + '#kr-full.kr-micro .kr-top{padding-top:calc(5px + env(safe-area-inset-top)); padding-bottom:1px;}'
    + '#kr-full.kr-micro .kr-mid{padding-top:2px; padding-bottom:6px;}'
    + '#kr-full.kr-micro .kr-h{font-size:18px; margin-bottom:2px;}'
    + '#kr-full.kr-micro .kr-sub{font-size:12.5px; margin-bottom:8px;}'
    + '#kr-full.kr-micro .kr-why{display:none;}'
    + '#kr-full.kr-micro .kr-id{gap:9px; margin-bottom:6px;}'
    + '#kr-full.kr-micro .kr-id .kr-av{width:26px; height:26px; font-size:14px;}'
    + '#kr-full.kr-micro .kr-id b{font-size:15.5px;}'
    + '#kr-full.kr-micro .kr-field{margin-bottom:6px;}'
    + '#kr-full.kr-micro .kr-field input{min-height:46px;}'
    + '#kr-full.kr-micro .kr-err{margin-bottom:6px; padding:6px 10px; font-size:12.5px;}'
    + '#kr-full.kr-micro .kr-err span{display:none;}'
    + '#kr-full.kr-micro .kr-check{min-height:44px;}'
    + '#kr-full.kr-micro .kr-go, #kr-full.kr-micro .kr-ghost, #kr-full.kr-micro .kr-danger{min-height:46px; margin-top:2px;}'
    + '#kr-full.kr-micro .kr-stack{margin-top:10px; gap:8px;}'
    + '#kr-full.kr-micro .kr-tile{min-height:78px; gap:5px; padding:8px 6px;}'
    + '#kr-full.kr-micro .kr-av{width:36px; height:36px; font-size:18px;}'
    + '#kr-full.kr-micro .kr-nm{font-size:13.5px;}'
    + '#kr-full.kr-micro .kr-peek{min-height:44px; padding:4px 12px;}'
    + '#kr-full.kr-micro .kr-peek b{font-size:14px;}'
    + '#kr-full.kr-micro .kr-peek span{display:none;}'
    /* ── 身分藥丸：住在 App 的畫面裡，所以只有它可以碰主色 ──
     * 提權用「同一個 class 寫兩次」(0,2,0)，不准綁宿主結構。
     * 底色永遠是模組固定的中性色，主色只准上前景（.kr-cta 的文字）。 */
    + '.kr-chip.kr-chip{display:inline-flex; align-items:center; gap:9px; min-height:44px; padding:0 15px;'
    + ' border-radius:99px; background-color:#f6f2ea; background-image:none; border:1px solid #e6dfd2; color:#5d554a;'
    + ' font-family:-apple-system,BlinkMacSystemFont,"PingFang TC","Noto Sans TC","Microsoft JhengHei",sans-serif;'
    + ' font-size:13.5px; font-weight:700; line-height:1.3; letter-spacing:normal; text-align:left;'
    + ' text-decoration:none; cursor:pointer; margin:0; box-sizing:border-box;'
    + ' -webkit-appearance:none; appearance:none; box-shadow:none;'
    /* 同樣要擋繼承滲漏（它住在宿主的 footer 裡，宿主什麼都可能設） */
    + ' -webkit-tap-highlight-color:transparent; -webkit-text-size-adjust:100%; text-size-adjust:100%;'
    + ' font-style:normal; font-variant:normal; font-variant-numeric:normal; text-transform:none;'
    + ' text-indent:0; text-shadow:none; word-spacing:normal; white-space:nowrap;'
    + ' -webkit-font-smoothing:auto; -moz-osx-font-smoothing:auto;'
    + ' transition:background-color .16s cubic-bezier(.22,.9,.3,1), transform .14s cubic-bezier(.22,.9,.3,1);}'
    + '.kr-chip.kr-chip:hover{background-color:#f1ece2;}'
    + '.kr-chip.kr-chip:active{transform:scale(.97);}'
    + '.kr-chip.kr-chip .kr-dot{width:22px; height:22px; border-radius:50%; box-sizing:border-box; flex:0 0 auto;'
    + ' display:flex; align-items:center; justify-content:center; font-size:12px; line-height:1;'
    + ' box-shadow:inset 0 0 0 1px rgba(255,255,255,.35);}'
    + '.kr-chip.kr-chip .kr-cta{color:var(--acc,#c1553f); font-weight:800; box-sizing:border-box;}'
    /* ── 第三拍：剛解鎖完，藥丸彈出來並亮一圈，眼睛才知道「身分」跑到哪去了 ──
     * ⚠️ 這兩個 delay 就是第三拍的**時序本體**，不要改成用 JS setTimeout 排（§4-9，三輪退件換來的）。
     *   kr-chipin 帶 fill:both ⇒ delay 期間停在第 0 格（opacity:0、pointer-events:none）：
     *   層還在淡出的那 230ms 內藥丸完全看不見也點不到，層一消失才開始彈出。
     *   .24s = kr-out .22s ＋ closeLayer 收尾 230ms 取整。
     *   ⚠️ 240 與 230 只差 10ms：主執行緒抖一下兩者可能對調。退化行為只是藥丸
     *      早幾毫秒開始彈（那時標籤已經是對的、層是 pointer-events:none），不會壞；
     *      所以 tools/check-unlock.js 的紅線只畫在 dt≤200ms 那段，不拿交界當判準。
     * 光暈刻意用**中性暖色寫死**、不吃 --acc：藥丸的規則仍是「主色只准上前景（.kr-cta 的文字）」。 */
    + '.kr-chip.kr-chip.kr-new{animation:kr-chipin .34s cubic-bezier(.22,.9,.3,1) .24s both,'
    + ' kr-chipglow .9s ease-out .42s;}'
    /* ── 減少動態效果：全部降級成「瞬間到位」，只留讀取轉圈（那是狀態指示不是裝飾） ──
     * ⚠️ 一定要放在檔尾（總閘）。所有 kr-rise/kr-pop/kr-fade 都是 both 填充 ⇒ animation:none
     *    之後元素回到自然狀態（opacity 1、無位移），不會有東西不見。
     * ⚠️ 偽元素要單獨列（`*` 選不到），否則成功光圈還是會動。
     * ⚠️ `.kr-chip.kr-chip{animation:none}` 那條是**承重的**：kr-chipin 帶 .24s delay ＋ fill:both，
     *    延遲期間藥丸是 opacity:0、pointer-events:none。萬一哪天 kr-new 在減少動態下被掛上去，
     *    就靠這條把 fill 一起關掉，藥丸才不會卡成隱形。（正常路徑不會掛：chipHtml 有擋。） */
    + '@media (prefers-reduced-motion: reduce){'
    +   '#kr-full{animation:none !important;}'
    +   '#kr-full *:not(.kr-spin){animation:none !important; transition:none !important;}'
    +   '#kr-full::before, #kr-full *::before, #kr-full *::after{animation:none !important;}'
    +   '.kr-chip.kr-chip{animation:none !important; transition:none !important;}'
    + '}';

  /* 樣式要在 init() 就注入：footer 的身分藥丸每次進站都看得到，
   * 不能等到使用者第一次打開解鎖畫面（paint）才有樣式。 */
  function ensureStyle() {
    if (document.getElementById("kr-style")) return;
    var head = document.head || document.getElementsByTagName("head")[0];
    if (!head) { document.addEventListener("DOMContentLoaded", ensureStyle); return; }
    var st = document.createElement("style");
    st.id = "kr-style";
    st.textContent = CSS;
    head.appendChild(st);
  }

  /* 滿版蓋住整個畫面 -> 高度不能用 100vh（iOS 的 100vh 是「鍵盤沒彈出時」的高度）。
   * 用 visualViewport 維護 --kr-vh：鍵盤彈出時整層跟著縮，
   * .kr-foot 是 flex item（不是 fixed）所以自然停在鍵盤正上方。 */
  function fitVH() {
    var vv = global.visualViewport;
    var h = vv ? vv.height : global.innerHeight;
    /* v4：iOS 對焦 input 時會把 layout viewport 往上捲，fixed 的整層跟著跑掉（頂欄被切、底下露白）。
     * offsetTop 就是它跑掉的量，寫進 --kr-top 抵銷回來。這裡不用加 listener：
     * fitVH 已經掛在 visualViewport 的 resize ＋ scroll（iOS 捲動時是 scroll 在動）。 */
    var t = (vv && vv.offsetTop) ? vv.offsetTop : 0;
    if (h) document.documentElement.style.setProperty("--kr-vh", h + "px");
    document.documentElement.style.setProperty("--kr-top", t + "px");
    applyDensity();
  }
  /* 密度等級：**用實際量到的滿版高度**決定，不是用 CSS 視窗高度。
   * ⚠️ 這裡不可以改用 @media(max-height:…)：iOS 鍵盤彈出只縮 visualViewport，
   *    CSS 視窗高度不變 -> 媒體查詢永遠不觸發（Android 會過、iPhone 不會）。
   * 量 layer 自己的高度（而不是只看 visualViewport），任何原因把它縮小都算數，
   * 包含測試用直接改 --kr-vh 的情況。 */
  var DENSITY = [["kr-short", 640], ["kr-tiny", 500], ["kr-micro", 460]];
  function applyDensity() {
    if (!layer) return;
    /* 離場動畫進行中不要重算：那時候層在做 scale，量到的高度不是真的 */
    if (layer.classList.contains("kr-leave")) return;
    /* v4：用 offsetHeight 不用 getBoundingClientRect —— 後者會把進／離場的 scale() 算進去
     * （實測進場時量到 636 而不是 624），高度剛好卡在門檻上時會在鍵盤彈出的瞬間閃一次密度切換。 */
    var h = layer.offsetHeight || layer.getBoundingClientRect().height;
    if (!h) {   /* 還沒顯示（display:none）時退回可視高度 */
      h = global.visualViewport ? global.visualViewport.height : global.innerHeight;
    }
    if (!h) return;
    /* ⚠️ 這是全檔唯一「在元素活著的時候 remove class」的地方（§4-7 的唯一例外，拿不掉）。
     *   它現在無害是有條件的：密度 class 底下只准寫尺寸與間距，不准寫 animation／transition。 */
    for (var i = 0; i < DENSITY.length; i++) {
      if (h <= DENSITY[i][1]) layer.classList.add(DENSITY[i][0]);
      else layer.classList.remove(DENSITY[i][0]);
    }
  }

  function ensureDom() {
    if (layer) return layer;
    ensureStyle();
    layer = document.createElement("div");
    layer.id = "kr-full";
    layer.hidden = true;
    document.body.appendChild(layer);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !layer.hidden) close();
    });
    if (global.visualViewport) {
      global.visualViewport.addEventListener("resize", fitVH);
      global.visualViewport.addEventListener("scroll", fitVH);
    }
    global.addEventListener("resize", fitVH);
    /* 滿版高度只要變了就重算密度等級（鍵盤、轉向、或直接改 --kr-vh 都算）。
     * class 只影響子元素的尺寸、不影響 layer 自己的高度，所以不會回授成無限迴圈。 */
    if (global.ResizeObserver) {
      try { new global.ResizeObserver(applyDensity).observe(layer); } catch (e) { }
    }
    return layer;
  }
  /* ---------------- 動畫的判定與時序（v4，規格在 DESIGN.md §4） ----------------
   * 只有三種模式，由 paint() 自己判斷，呼叫端一行都不用改：
   *   open ＝ 這一層剛打開（整套進場）
   *   step ＝ 換屏（選人↔輸密碼、身分頁、換人確認）→ 只有中段動，頂欄與底條留在原地
   *   ""   ＝ 同一屏重繪（顯示密碼／打錯／送出中）→ 完全不動
   * 判定靠 screenKey()：屏的身分（view/step/選到誰/名單狀態）變了才算換屏；
   * ui.tries／ui.show／ui.busy／ui.done 刻意**不進 key**（那些是同一屏的狀態變化）。 */
  var lastKey = null;       /* 上一次畫的是哪一屏 */
  var morphFrom = null;     /* FLIP：剛才那顆磚上的頭像位置 */
  var leaveT = null;        /* 離場收尾計時器（全檔唯一的計時器，守衛在 paint()） */
  function reducedMotion() {
    return !!(global.matchMedia && global.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }
  function screenKey() {
    return ui.view + "|" + ui.step + "|" + (ui.userId || "")
      + "|" + (ring ? "r" : (ringErr ? "e" : "l"));   /* 名單：ready／error／loading */
  }
  /* 頭像共享形變（FLIP）：把「剛才那顆磚上的頭像」的位置，換屏後套回新頭像再放掉，
   * 等於用動作講「你選的是這個人」。量不到就安靜放棄（那一屏只是沒有進場動畫，不會壞）。 */
  function runMorph() {
    var to = layer.querySelector(".kr-id .kr-av"), f = morphFrom;
    morphFrom = null;
    if (!to || !f) return;
    var b = to.getBoundingClientRect();
    if (!b.width) return;
    var dx = (f.left + f.width / 2) - (b.left + b.width / 2);
    var dy = (f.top + f.height / 2) - (b.top + b.height / 2);
    to.style.transition = "none";
    to.style.transform = "translate(" + dx + "px," + dy + "px) scale(" + (f.width / b.width) + ")";
    requestAnimationFrame(function () {
      to.style.transition = "transform .36s cubic-bezier(.22,.9,.3,1)";
      to.style.transform = "none";
      /* 收尾只清自己設的 inline style（transform 當下已經是 none，設空沒有視覺變化），
       * 刻意**不碰任何 class** —— 碰了就會讓 CSS 規則重新命中而重播（§4-7）。 */
      setTimeout(function () { to.style.transition = ""; to.style.transform = ""; }, 400);
    });
  }
  /* 三段式：頂欄／中段（垂直置中、可捲）／底部常駐出口。foot 傳空字串就沒有那一條。 */
  function paint(top, body, foot, focusId) {
    ensureDom();
    /* ⚠️ 守衛（§4-9）：退場途中（class 還有 kr-leave）或已經藏起來 ⇒ 先 reopen()
     *   （內含 clearTimeout(leaveT)），否則收尾計時器會在 230ms 後把剛打開的畫面關掉，
     *   變成「狀態機說開著、DOM 說關著」。
     * ⚠️ 條件**不可以只寫 layer.hidden**：退場途中 hidden 還是 false，那條路就永遠不清計時器。
     * 守衛放在這裡（每一屏都會經過）而不是各入口，將來新增入口不必記得補。 */
    if (layer.hidden || layer.classList.contains("kr-leave")) reopen();
    if (layer.hidden) prevOverflow = document.body.style.overflow;
    var k = screenKey();
    var mode = layer.hidden ? "open" : (k !== lastKey ? "step" : "");
    lastKey = k;
    if (reducedMotion()) mode = "";
    var will = (mode === "step") && !!morphFrom;
    /* ⚠️ 容器 class 只准在這裡**整份指派**，其他地方一律不准 classList.remove() 局部拆（§4-7）。
     * 這一行會把密度 class 一起清掉，下面的 fitVH()（內含 applyDensity()）會補回來，順序不可調換。 */
    layer.className = (mode === "open" ? "kr-a-open" : mode === "step" ? "kr-a-step" : "");
    layer.innerHTML = '<div class="kr-top">' + top + '</div>'
      + '<div class="kr-main"><div class="kr-mid">' + body + '</div></div>'
      + (foot ? '<div class="kr-foot">' + foot + '</div>' : '');
    /* ⚠️ 順序不可調換：.kr-nofx 一定要在任何「會強迫算樣式／版面」的動作之前標上去
     * （下一段的 fitVH() 會讀 offsetHeight ＝ 強迫 layout，動畫在那一刻就定生死了）。 */
    if (will) { var idEl = layer.querySelector(".kr-id"); if (idEl) idEl.classList.add("kr-nofx"); }
    layer.hidden = false;
    ui.open = true;
    document.body.style.overflow = "hidden";
    fitVH();
    if (will) runMorph(); else morphFrom = null;
    if (focusId) setTimeout(function () { var el = document.getElementById(focusId); if (el) el.focus(); }, 70);
  }
  /* 離場：up=true 是解鎖成功（往前退開，像門打開），false 是使用者自己關掉（往後收）。
   * 收尾不做局部 remove：先 hidden（display:none 之後不可能有動畫跑）再整份指派 className。 */
  function closeLayer(up) {
    if (!layer || layer.hidden) return;
    if (layer.classList.contains("kr-leave")) return;   /* 已經在退場：不要重排時序 */
    ui.open = false;
    lastKey = null;
    document.body.style.overflow = (prevOverflow == null ? "" : prevOverflow);
    prevOverflow = null;
    if (reducedMotion()) { layer.hidden = true; layer.className = ""; layer.innerHTML = ""; return; }
    layer.classList.add("kr-leave");
    if (up) layer.classList.add("kr-up");
    clearTimeout(leaveT);
    leaveT = setTimeout(function () {
      layer.hidden = true; layer.className = ""; layer.innerHTML = "";
    }, up ? 230 : 200);
  }
  /* 把層歸零到「可以重新進場」的狀態。只在 paint() 的守衛裡呼叫（同一件事只留一個地方做）。 */
  function reopen() {
    clearTimeout(leaveT);
    layer.hidden = true;
    layer.className = "";
    lastKey = null;
  }
  function close() { closeLayer(false); }

  /* ---------------- 版面零件 ---------------- */
  /* App 的身分靠文字不靠顏色（零調色成本、零撞色風險） */
  function topBar(left) {
    return (left || '<div class="kr-app">' + esc(appLabel()) + '</div>')
      + '<button class="kr-x" onclick="Keyring.close()" aria-label="關閉">✕</button>';
  }
  /* 「先看看就好」＝底部常駐全寬出口。滿版不是鎖屏，隨時要走得掉：
   * ①這一條 ②右上 ✕ ③標題寫「誰要編輯？」而不是「誰在用？」。改文案等於改掉這個設計。 */
  function peekBar() {
    return '<button class="kr-peek" onclick="Keyring.peek()">'
      + '<b>👀 先看看就好</b><span>不用密碼也能看，只是不能改東西</span></button>';
  }

  /* ---------------- 解鎖畫面 ---------------- */
  function open(reason) {
    /* 公開的 App 沒有「解鎖」這回事：打開網址就能用，不要彈任何東西 */
    if (pub.is) return;
    ui = {
      view: "unlock", step: "who", userId: null, reason: reason || "", tries: 0,
      busy: false, done: false, shake: false, show: false, remember: true, open: true
    };
    morphFrom = null;
    /* ⚠️ 這裡刻意不呼叫 reopen()：守衛集中在 paint()（§4-9），同一件事只留一個地方做。 */
    if (device) { openIdentity(); return; }
    draw();
    /* 每次開都對一次名單：後台剛加的人，這邊不用重整就看得到。
     * ⚠️ 只有「名單真的變了」才重畫（v4 修）：`fetchRing` 在 TTL 內會用 Promise.resolve
     * 立刻回來，無條件重畫等於在同一個 tick 裡再 paint 一次 ——
     * 第二次的 screenKey 一樣 ⇒ mode 變成 ""，剛掛上去的 `kr-a-open` 會被整份指派清掉，
     * **進場動畫等於沒播**（實測 class 是空的、磚的 delay 是 0s）；
     * 網路慢一點時則是播到一半被截斷。同時也省掉一次沒必要的閃動。 */
    var sig0 = listSig();
    var again = function () { if (ui.open && listSig() !== sig0) draw(); };
    fetchRing(false).then(again, again);
  }
  /* 名單抓失敗／還沒有人時的重試 */
  function retry() {
    ringErr = ""; ring = null;
    draw();
    var sig0 = listSig();
    var again = function () { if (ui.open && listSig() !== sig0) draw(); };
    fetchRing(true).then(again, again);
  }
  /* 名單的「畫面指紋」：只含真的會畫出來的欄位。用來判斷抓回來的鑰匙圈跟現在畫的一不一樣。 */
  function listSig() {
    if (!ring) return "none|" + (ringErr ? "err" : "loading");
    return ringUsers().map(function (u) {
      return u.id + ":" + u.name + ":" + (u.emoji || "") + ":" + (u.theme || "") + ":" + (u.hint || "");
    }).join("|");
  }
  function draw() {
    ui.view = "unlock";
    /* 只有一個人時跳過選人，直接進輸密碼（但仍顯示他的頭像確認是誰的鑰匙圈） */
    if (ui.step === "who" && ring && ringUsers().length === 1) {
      ui.step = "pw"; ui.userId = ringUsers()[0].id;
    }
    if (ui.step === "who") drawWho(); else drawPw();
  }
  function whyBar(short) {
    if (!ui.reason) return "";
    return '<div class="kr-why">要「' + esc(ui.reason) + '」得先解鎖'
      + (short ? '。' : '，選一下你是誰就好。') + '</div>';
  }
  function drawWho() {
    if (!ring) {
      /* 抓不到鑰匙圈：一定要講「你已經看得到內容，只是暫時不能編輯」——告訴他沒有壞掉 */
      var body = ringErr
        ? '<div class="kr-wrap narrow"><div class="kr-empty"><span class="big">🌧️</span>'
          + '現在拿不到鑰匙圈<br>可能是網路的關係。<br>你已經看得到內容，只是暫時不能編輯。'
          + '<div class="kr-stack"><button class="kr-ghost" onclick="Keyring.retry()">↻ 再抓一次</button></div>'
          + '</div></div>'
        : '<div class="kr-wrap narrow"><div class="kr-empty">正在拿鑰匙圈…</div></div>';
      paint(topBar(), body, peekBar());
      return;
    }
    var users = ringUsers();
    if (!users.length) {
      paint(topBar(),
        '<div class="kr-wrap narrow"><div class="kr-empty"><span class="big">🔑</span>'
        + '這個鑰匙圈裡還沒有人可以編輯。<br>跟 Benson 說一聲，他那邊配一組給你。'
        + '<div class="kr-stack"><button class="kr-ghost" onclick="Keyring.retry()">他說配好了？↻ 再抓一次</button></div>'
        + '</div></div>', peekBar());
      return;
    }
    var many = users.length > 6;
    /* style="--kr-i:N" 是磚的序號，進場時各自錯開（animation-delay 用它算）；
     * onclick 多傳一個 event 是給頭像形變用的（pick 要拿到被點的那顆磚）。 */
    var tiles = users.map(function (u, i) {
      var h = hintOf(u);
      return '<button class="kr-tile" style="--kr-i:' + i + '" onclick="Keyring.pick(\'' + esc(u.id) + '\',event)">'
        + '<span class="kr-av" style="background:' + grad(u.theme) + '">' + esc(u.emoji || "🧑") + '</span>'
        + '<span class="kr-tx"><span class="kr-nm">' + esc(u.name) + '</span>'
        + (h ? '<span class="kr-hint">' + esc(h) + '</span>' : '') + '</span></button>';
    }).join("");
    /* 標題是「誰要編輯？」不是「誰在用？」：後者暗示「你必須是其中一個」 */
    paint(topBar(),
      '<div class="kr-wrap">'
      + '<h2 class="kr-h">誰要編輯？</h2>'
      + '<p class="kr-sub">選自己、輸一次密碼，這台裝置就記住了。</p>'
      + whyBar(false)
      + '<div class="kr-grid' + (many ? ' many' : '') + '">' + tiles + '</div></div>',
      peekBar());
  }
  function drawPw() {
    var u = ringUser(ui.userId) || { name: "", emoji: "🧑", theme: "sunset" };
    var multi = ringUsers().length > 1;
    var h = hintOf(u);
    /* 抖一下的旗標在這裡「消費掉」：只有真的打錯的那一次會抖，
     * 之後按顯示密碼重繪不會再抖（重繪產生的是新元素，class 沒帶上就不播）。 */
    var sh = ui.shake; ui.shake = false;
    var err = "";
    if (ui.tries > 0 && !ui.done) {
      err = '<div class="kr-err">密碼不對，再試一次'
        + (ui.tries >= 2 ? '<span>想不起來的話跟 Benson 說一聲，他那邊可以幫你換一組新的。</span>' : '')
        + '</div>';
    }
    var body = '<div class="kr-wrap narrow">'
      + whyBar(true)
      + '<div class="kr-id"><span class="kr-av' + (ui.done ? " kr-ok" : "") + '" style="background:' + grad(u.theme) + '">' + esc(u.emoji || "🧑") + '</span>'
      + '<div><b>嗨，' + esc(u.name) + '</b><span>' + (h ? esc(h) + '・' : '') + '輸入密碼就可以編輯</span></div></div>'
      + '<form onsubmit="return Keyring.submit(event)">'
      + '<div class="kr-field' + (sh ? " kr-shake" : "") + '">'
      + '<input id="kr-pw" type="' + (ui.show ? "text" : "password") + '" inputmode="text" '
      + 'autocomplete="current-password" autocapitalize="off" spellcheck="false" placeholder="你的密碼">'
      + '<button type="button" class="kr-eye" onclick="Keyring.toggleShow()" aria-label="顯示密碼">' + (ui.show ? "🙈" : "👁") + '</button>'
      + '</div>'
      + err
      /* onchange 那道閘：使用者真的動到勾選框才開啟打勾的彈跳（預設就是打勾的，見 CSS 註解） */
      + '<label class="kr-check"><input type="checkbox" id="kr-remember" onchange="Keyring.tick(this)" ' + (ui.remember ? "checked" : "") + '>'
      + '<span class="kr-box"></span>'
      + '<span class="kr-lb">記住這台裝置<small>下次打開就直接能編輯。別人的電腦記得取消。</small></span></label>'
      + '<button class="kr-go' + (ui.done ? " kr-ok" : "") + '" type="submit" ' + ((ui.busy || ui.done) ? "disabled" : "") + '>'
      + (ui.done ? '✓ 解開了' : (ui.busy ? '<span class="kr-spin"></span>解開中…' : '解鎖')) + '</button>'
      + '</form></div>';
    var left = multi ? '<button class="kr-back" onclick="Keyring.backToWho()">‹ 換一個人</button>' : null;
    /* 成功那一拍不要再把游標搶回密碼欄（已經在退場了） */
    paint(topBar(left), body, peekBar(), ui.done ? null : "kr-pw");
  }

  /* 選人：先把那顆頭像現在的位置記下來，換屏後讓它「飛」到新位置（等於在講你選到的是誰）。
   * ev 是 onclick 傳進來的原生 event；沒有它（或減少動態）就只是沒有形變，不會壞。 */
  function pick(id, ev) {
    var t = (ev && ev.currentTarget) ? ev.currentTarget.querySelector(".kr-av") : null;
    morphFrom = (t && !reducedMotion()) ? t.getBoundingClientRect() : null;
    ui.step = "pw"; ui.userId = id; ui.tries = 0; ui.show = false; ui.done = false;
    draw();
  }
  function backToWho() { morphFrom = null; ui.step = "who"; ui.userId = null; ui.done = false; draw(); }
  /* 使用者第一次動到勾選框時才開啟打勾動畫。class 加上去就不拆，元素重繪時自然跟著死。 */
  function tick(el) { var b = el && el.nextElementSibling; if (b) b.classList.add("kr-tickable"); }
  function peek() { close(); say("好，就先看看。想編輯再點最下面那條"); }
  function toggleShow() {
    var inp = document.getElementById("kr-pw");
    var v = inp ? inp.value : "";
    var rem = document.getElementById("kr-remember");
    ui.remember = rem ? rem.checked : true;
    ui.show = !ui.show;
    draw();
    inp = document.getElementById("kr-pw");
    if (inp) { inp.value = v; inp.focus(); }
  }
  function submit(e) {
    if (e && e.preventDefault) e.preventDefault();
    if (ui.busy || ui.done) return false;
    var inp = document.getElementById("kr-pw");
    var pw = inp ? inp.value : "";
    var rem = document.getElementById("kr-remember");
    ui.remember = rem ? rem.checked : true;
    if (!pw) { say("先輸入密碼喔", true); return false; }
    var u = ringUser(ui.userId);
    var entry = u && u.apps ? u.apps[CFG.appId] : null;
    if (!entry) { say("這個人還沒有這個 App 的鑰匙", true); return false; }
    ui.busy = true; draw();
    var kdf = u.kdf || {};
    var theKey = null;
    deriveKey(pw, kdf.salt, kdf.iter)
      .then(function (key) { theKey = key; return decryptEntry(key, entry); })
      .then(function (token) { return exportKey(theKey).then(function (kb64) { return { token: token, k: kb64 }; }); })
      .then(function (r) {
        /* ── 解鎖成功走三拍（DESIGN.md §4-5）────────────────────────────────
         * 第一拍（0~430ms）確認：`✓ 解開了` ＋ 頭像亮一圈。
         *   ⚠️ 金鑰**立刻**寫下去（那是狀態，不是視覺，不可以為了動畫延後 —— §4-8）；
         *      延後的只有「關掉這一層」與「通知宿主重繪」，那兩件事是視覺與時序。
         * 第二拍（430~660ms）退場：closeLayer(true) 往前退開，kr-leave 讓底下馬上可點。
         * 第三拍落點：身分藥丸彈出＋亮一圈 —— **不排任何計時器**，
         *   標籤（狀態）在 notify() 那一刻就已經誠實，動畫靠 CSS 的 animation-delay 自己等（§4-9）。 */
        ui.busy = false;
        ui.done = true;
        writeDevice({
          userId: u.id, name: u.name, emoji: u.emoji, theme: u.theme,
          at: new Date().toISOString(), k: r.k, t: r.token, remember: ui.remember
        });
        lsSet(K("introSeen"), "1");
        draw();                       /* 第一拍：這是狀態的呈現，減少動態時也照畫一幀 */
        setTimeout(function () {
          closeLayer(true);
          chipFresh = !reducedMotion(); chipFreshAt = Date.now();
          say("解開了，" + u.name + " 現在可以編輯 🎉");
          notify();                   /* 宿主重繪 footer ⇒ 藥丸帶著 kr-new 生出來 */
        }, reducedMotion() ? 0 : 430);
      })
      .catch(function () {
        /* 密碼錯的唯一症狀就是解不開——不做次數鎖定、不做倒數 */
        ui.busy = false; ui.tries++;
        ui.shake = !reducedMotion();
        draw();
        var el = document.getElementById("kr-pw");
        if (el) el.focus();
      });
    return false;
  }

  /* ---------------- 已解鎖：身分頁（v3 起也是滿版，跟解鎖同一套語言） ---------------- */
  function openIdentity() {
    if (!device) { open(""); return; }
    ui.open = true;
    ui.view = "id";
    /* ⚠️ 這裡刻意不呼叫 reopen()：守衛集中在 paint()（§4-9）。
     * 血淚：舊寫法各入口自己補 `if(hidden) reopen()`，而退場途中 hidden 還是 false ⇒
     *      退場中點藥丸開身分頁，120ms 後又被舊計時器關掉。 */
    /* 這一頁沒有 peek bar：已經解鎖了，「先看看」沒有意義，✕ 就是出口 */
    paint(topBar(),
      '<div class="kr-wrap narrow">'
      + '<div class="kr-id"><span class="kr-av" style="background:' + grad(device.theme) + '">' + esc(device.emoji || "🧑") + '</span>'
      + '<div><b>' + esc(device.name) + '</b><span>這台裝置記住了你的鑰匙，可以編輯</span></div></div>'
      + '<div class="kr-stack"><button class="kr-go" onclick="Keyring.close()">好，繼續用</button>'
      + '<button class="kr-ghost" onclick="Keyring.askSwitch()">🔄 換人用</button></div></div>', "");
  }
  function askSwitch() {
    ui.open = true;
    ui.view = "switch";
    paint(topBar(),
      '<div class="kr-wrap narrow">'
      + '<h2 class="kr-h">要換人嗎？</h2>'
      + '<div class="kr-warn">換人會把這台裝置記住的鑰匙清掉，回到「只看看」。下一個人自己選名字、輸密碼就好。</div>'
      + '<div class="kr-stack"><button class="kr-danger" onclick="Keyring.doSwitch()">清掉，換人</button>'
      + '<button class="kr-ghost" onclick="Keyring.openIdentity()">先不要</button></div></div>', "");
  }
  function doSwitch() {
    forget();
    notify();
    say("已經清掉，換誰用都可以");
    open("");
  }

  /* ---------------- App 畫面裡的身分藥丸 ---------------- */
  function chipHtml() {
    if (!CFG.enabled || pub.is) return "";   /* 公開模式沒有身分可言，不要在 App 畫面上留一顆沒用的藥丸 */
    ensureStyle();   /* 宿主可能還沒開過解鎖畫面，藥丸自己要保證有樣式 */
    /* 第三拍（見 submit）：剛解鎖完的**第一次**重繪帶 kr-new，彈出＋亮一圈。
     * 用完即丟：藥丸是宿主重繪出來的，模組只能在 HTML 上做記號，不能事後去拆 class（§4-7）。
     * kr-new 也不需要拆——chipHtml 每次都產生新元素，class 跟著元素一起死；
     * kr-chipglow 沒有 forwards，播完自己回到 box-shadow:none。
     * 減少動態時根本不掛（CSS 那邊還有一道總閘接住）。 */
    var nw = (chipFresh && (Date.now() - chipFreshAt) < 5000) ? " kr-new" : "";
    chipFresh = false;
    if (device) {
      return '<button class="kr-chip' + nw + '" onclick="Keyring.openIdentity()">'
        + '<span class="kr-dot" style="background:' + grad(device.theme) + '"></span>'
        + esc(device.name) + '・可以編輯</button>';
    }
    /* 主色只上前景：.kr-cta 吃 var(--acc)，底色永遠是模組固定的中性色 */
    return '<button class="kr-chip' + nw + '" onclick="Keyring.open(\'\')">🔒 只看看模式・'
      + '<span class="kr-cta">點我解鎖 ›</span></button>';
  }

  /* 這台裝置從來沒解鎖過、也沒看過解鎖畫面 -> 進站約 0.9 秒主動端一次，之後永遠不再自動彈 */
  function maybeIntro() {
    if (!CFG.enabled || device || started || pub.is) return;
    if (lsGet(K("introSeen")) === "1") return;
    started = true;
    fetchRing(false).then(function () {
      if (device || !ringUsers().length) return;
      setTimeout(function () {
        if (device || ui.open) return;
        lsSet(K("introSeen"), "1");
        open("");
      }, CFG.introDelay);
    }).catch(function () { /* 抓不到就別打擾 */ });
  }

  /* ---------------- init ---------------- */
  function init(opts) {
    Object.keys(opts || {}).forEach(function (k) { CFG[k] = opts[k]; });
    if (!CFG.ns) CFG.ns = "keyring." + (CFG.appId || "app") + ".";
    if (!CFG.enabled) return publicState();
    ensureStyle();   /* 進站就注入：身分藥丸在還沒開過解鎖畫面之前就要是對的樣子 */
    /* 本機救援用：想指到別份鑰匙圈（例如本機後台的預覽）就設 localStorage <ns>src */
    var override = lsGet(K("src"));
    if (override) CFG.url = override;
    device = readDevice();
    /* 同步把金鑰塞回 App 既有的 key，開機第一次判斷「能不能寫」就是對的 */
    if (device && device.t) writeToken(device.t, device.remember);
    /* 公開模式：上次抓到的值先頂上（網路還沒回來、或根本沒網路時，App 照樣打得開）。
       ⚠️ 這裡只是「先用著」，下面還是會跟鑰匙圈對一次，後台改了就會換過去。 */
    var cachedPub = lsGet(K("pub"));
    if (cachedPub) { pub.is = true; pub.value = cachedPub; writeToken(cachedPub, true); }
    /* 背景跟鑰匙圈對一次：
       ・公開的 App → 套用公開值（不需要密碼、不需要選人）
       ・其他 → 原本那條（換金鑰自動換過去；被刪／換密碼就靜默降級） */
    setTimeout(function () {
      fetchRing(false).then(function () {
        var changed = applyPublic();
        if (pub.is) { markReady(); if (changed) notify(); return; }
        markReady();
        return refreshFromRing();
      }, function () {
        /* 抓不到鑰匙圈：維持現狀（快取的公開值還在用），不要把人踢回去 */
        markReady();
        if (pub.is) notify();
      });
    }, 0);
    return publicState();
  }

  var API = {
    init: init,
    open: open,
    close: close,
    peek: peek,
    retry: retry,
    pick: pick,
    backToWho: backToWho,
    tick: tick,
    toggleShow: toggleShow,
    submit: submit,
    openIdentity: openIdentity,
    askSwitch: askSwitch,
    doSwitch: doSwitch,
    chipHtml: chipHtml,
    maybeIntro: maybeIntro,
    forget: function () { forget(); notify(); },
    /* 公開模式：宿主用這兩支決定「要不要顯示解鎖入口」與「金鑰什麼時候會到」 */
    isPublic: function () { return !!pub.is; },
    whenReady: function () {
      if (pub.ready) return Promise.resolve(publicState());
      return new Promise(function (resolve) { readyWaiters.push(resolve); });
    },
    isUnlocked: function () { return !!device || !!pub.is; },
    current: function () { return publicState(); },
    reload: function () { return fetchRing(true); },
    /* 給測試／進階用 */
    _cfg: CFG,
    _deriveKey: deriveKey,
    _decryptEntry: decryptEntry
  };

  global.Keyring = API;
  if (typeof module !== "undefined" && module.exports) module.exports = API;
})(typeof window !== "undefined" ? window : this);
