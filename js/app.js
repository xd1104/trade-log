(function () {
  'use strict';

  /* ---------- 開場畫面（js/splash.js）----------
     ⚠️ 一定要寫成 window.Splash && …，**不可以裸寫 Splash.hold()**。
     那支模組載不到的時候（離線、SW 沒預快取、部署漏檔）裸寫會丟 ReferenceError
     ⇒ 整支 app.js 的 IIFE 當場中止 ⇒ 一筆紀錄都畫不出來、沒套樣式的 #splash
     永遠卡在畫面上，而且**保險絲就住在那支沒載到的檔案裡**，不會有人來救。
     （範本那一輪 QA 實測過的災情，見 lab 手冊 D 段。） */
  var hasSplash = !!(window.Splash && window.Splash.hold && window.Splash.ready);
  if (hasSplash) { try { Splash.hold(); } catch (e) { hasSplash = false; } }
  if (!hasSplash) splashFallback();

  function splashFallback() {
    /* 自己把開場收掉。全螢幕的東西卡住＝App 打不開，比白畫面嚴重一個等級。 */
    try {
      var sp = document.getElementById('splash');
      if (sp && sp.parentNode) sp.parentNode.removeChild(sp);
      document.documentElement.setAttribute('data-splash', 'off');
    } catch (e) {}
    /* splash.js 平常會掛這一行；沒有它的話 iOS Safari 的 :active 不會觸發
       ＝ 手機上所有按下回饋都是死的。 */
    try { document.addEventListener('touchstart', function () {}, { passive: true }); }
    catch (e) { try { document.addEventListener('touchstart', function () {}, false); } catch (e2) {} }
  }

  /* 畫面畫好了就叫一次，開場才會收。
     這支 App 的資料在 localStorage、開啟是即時的，所以「畫好」就是 renderAll() 跑完
     —— 不必等網路（pullPractice 是背景同步，讓它去等開場沒有意義）。
     只認第一次：之後同步進來的重繪都不該再影響開場。
     ⚠️ 失敗也要叫，不然開場會變成當機畫面、要停到 6 秒保險絲才走。 */
  var splashDone = false;
  function splashReady() {
    if (splashDone) return;
    splashDone = true;
    if (window.Splash && window.Splash.ready) { try { Splash.ready(); } catch (e) {} }
  }

  var KEY = 'trade-log-v1';

  /* ---------- 跟電腦面板雙向同步 ----------
     面板 → 手機：面板 push data/practice.json，這邊 pullPractice() 抓下來合併。
     手機 → 面板：這邊用鑰匙圈解開的 GitHub 金鑰把 data/phone.json 寫回 repo，
                  面板背景輪詢那個檔案。兩邊各寫各的檔，不會互相蓋掉。
     【只同步練習（sim）】repo 是公開的，真實交易永遠不上傳 —— 這條沒有例外。
     心得誰新誰贏，靠 note_at 時間戳判斷；沒有時間戳的一律不覆蓋別人。 */
  var GH = { owner: 'xd1104', repo: 'trade-log', branch: 'main' };
  var TOKEN_KEY = 'tradelog_gh_pat';
  var PHONE_FILE = 'data/phone.json';
  function ghToken() {
    try { return localStorage.getItem(TOKEN_KEY) || sessionStorage.getItem(TOKEN_KEY) || ''; }
    catch (e) { return ''; }
  }
  function nowStamp() { return new Date().toISOString().slice(0, 19); }
  function isSim(x) { return (x.mode || 'sim') === 'sim'; }
  /* 心得誰比較新：有時間戳的贏；都有就比大小；都沒有就當作平手（不動） */
  function noteWins(incoming, current) {
    var a = (incoming.note || '').trim(), b = (current.note || '').trim();
    if (a === b) return false;
    var ta = incoming.note_at || '', tb = current.note_at || '';
    if (ta && !tb) return true;
    if (!ta && tb) return false;
    if (ta && tb) return ta > tb;
    return !b && !!a;            // 兩邊都沒時間戳：只補空的，不覆蓋
  }
  var TICK = 10; // 微台指每點 NT$10

  // ---------- storage ----------
  var SEED = (typeof window !== 'undefined' && window.TRADE_LOG_SEED) || [];
  function withMode(arr) {
    return arr.map(function (t) { return t.mode ? t : Object.assign({}, t, { mode: 'sim' }); });
  }
  function load() {
    try {
      var r = localStorage.getItem(KEY);
      if (r !== null) return withMode(JSON.parse(r)); // 已初始化（含空陣列）→ 尊重本機
    } catch (e) {}
    var seeded = withMode(SEED); // 首次開啟：載入內建歷史資料（皆為模擬）
    save(seeded);
    return seeded;
  }
  function save(d) { try { localStorage.setItem(KEY, JSON.stringify(d)); } catch (e) {} }
  var data = load();

  // 模式：sim（模擬）/ real（真實操作）。所有統計與紀錄依當前模式分流。
  var MODE_KEY = 'trade-log-mode';
  var mode = 'sim';
  try { var _m = localStorage.getItem(MODE_KEY); if (_m === 'real' || _m === 'sim') mode = _m; } catch (e) {}
  function saveMode(m) { mode = m; try { localStorage.setItem(MODE_KEY, m); } catch (e) {} }
  function md() { return data.filter(function (x) { return (x.mode || 'sim') === mode; }); }

  var FEE_KEY = 'trade-log-fee-v1';
  function loadFee() { var v = parseFloat(localStorage.getItem(FEE_KEY)); return isNaN(v) ? 50 : v; }
  var fee = loadFee();
  function saveFee(v) { fee = v; try { localStorage.setItem(FEE_KEY, String(v)); } catch (e) {} }

  // ---------- helpers ----------
  function res(t) { return t.dir === 'long' ? (t.exit - t.entry) : (t.entry - t.exit); }
  function signed(n) { var r = Math.round(n); if (r > 0) return '+' + r; if (r < 0) return '−' + Math.abs(r); return '±0'; }
  function cls(n) { return n > 0 ? 'win' : n < 0 ? 'loss' : 'flat'; }
  function sortAsc(a) { return a.slice().sort(function (x, y) { return x.date < y.date ? -1 : 1; }); }
  function esc(s) { return String(s).replace(/[&<>]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]; }); }
  function nfmt(n) { return n.toLocaleString('en-US'); }

  var wd = ['日', '一', '二', '三', '四', '五', '六'];
  function fmtDate(iso) { var p = iso.split('-'); return p[1] + '/' + p[2]; }
  function pad(n) { return ('0' + n).slice(-2); }
  function todayISO() { var d = new Date(); return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()); }
  function parseISO(s) { var p = s.split('-'); return new Date(p[0], p[1] - 1, p[2], 12, 0, 0); }
  function toISO(d) { return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()); }
  function addDays(d, n) { var x = new Date(d.getTime()); x.setDate(x.getDate() + n); return x; }
  function addMonths(d, n) { var x = new Date(d.getTime()); x.setMonth(x.getMonth() + n); return x; }

  // ---------- period ----------
  // {type:'days',n} | {type:'months',n} | {type:'range',from,to}
  var period = { type: 'days', n: 30 };
  function periodList() {
    var asc = sortAsc(md()), ref = todayISO();
    if (period.type === 'range') {
      return asc.filter(function (x) { return x.date >= period.from && x.date <= period.to; });
    }
    var cut = period.type === 'months'
      ? toISO(addMonths(parseISO(ref), -period.n))
      : toISO(addDays(parseISO(ref), -(period.n - 1)));
    return asc.filter(function (x) { return x.date >= cut && x.date <= ref; });
  }

  function stats(list) {
    var w = 0, l = 0, f = 0, net = 0;
    list.forEach(function (t) { var r = res(t); net += r; if (r > 0) w++; else if (r < 0) l++; else f++; });
    var total = list.length;
    return { w: w, l: l, f: f, net: net, total: total, rate: total ? Math.round(w / total * 100) : 0 };
  }

  // ---------- render: summary ----------
  var $ = function (id) { return document.getElementById(id); };

  function renderSummary() {
    var list = periodList(), s = stats(list);
    var wl = $('wl'), net = $('net'), cash = $('cash');
    if (s.total === 0) {
      $('rateNum').textContent = '—';
      wl.innerHTML = '<span class="f">此區間尚無交易</span>';
      net.className = 'net zero'; net.innerHTML = '<span class="v">±0</span><span class="u">點</span>';
      cash.textContent = '';
      return;
    }
    $('rateNum').textContent = s.rate;
    wl.innerHTML = '<span class="w"><b>' + s.w + '</b> 勝</span>　<span class="l"><b>' + s.l + '</b> 敗</span>' +
      (s.f ? '　<span class="f"><b>' + s.f + '</b> 平</span>' : '');
    net.className = 'net ' + cls(s.net);
    net.innerHTML = '<span class="v">' + signed(s.net) + '</span><span class="u">點</span>';
    var nt = s.net * TICK - fee * s.total;
    cash.textContent = '淨 ' + (nt < 0 ? '−' : '+') + 'NT$' + nfmt(Math.abs(nt));
  }

  // ---------- 剛存的那一筆：讓他認得出「就是這張」 ----------
  // 存完／改完之後，那一筆在畫面上會走「進場 ＋ 金色光暈」（css/motion.css 第 4 段）。
  // ⚠️ 用計時器把標記拿掉，**不掛 animationend**：動畫是 backwards fill，
  //    跑完自己回到常態，class 留著也沒有殘留效果；標記拿掉是為了讓之後的重繪
  //    （背景同步拉回紀錄、換月份）不要再閃一次。
  var freshKey = null, freshTimer = null;
  function markFresh(date, m) {
    freshKey = date + '|' + (m || 'sim');
    clearTimeout(freshTimer);
    freshTimer = setTimeout(function () { freshKey = null; }, 2000);
  }
  function freshCls(t) {
    return (freshKey && t.date + '|' + (t.mode || 'sim') === freshKey) ? ' fresh' : '';
  }

  // ---------- render: today + list ----------
  function tradeHTML(t) {
    var r = res(t), rc = cls(r);
    var dir = t.dir === 'long'
      ? '<span class="dir"><span class="ar">▲</span>多</span>'
      : '<span class="dir"><span class="ar">▼</span>空</span>';
    var badge = r > 0 ? '<span class="badge b-win">勝</span>'
      : r < 0 ? '<span class="badge b-loss">敗</span>'
      : '<span class="badge b-flat">平</span>';
    var note = t.note ? '<div class="tr-note">' + esc(t.note) + '</div>'
      : '<div class="tr-note empty">（無備註）</div>';
    return '<div class="trade' + freshCls(t) + '" tabindex="0" data-date="' + t.date + '">' +
      '<div class="tr-top">' +
        '<span class="tr-date">' + fmtDate(t.date) + '</span>' + dir +
        '<span class="tr-px">' + nfmt(t.entry) + '<span class="arrow">→</span>' + nfmt(t.exit) + '</span>' +
        '<span class="tr-res r-' + rc + '">' + signed(r) + '</span>' + badge +
      '</div>' + note + '</div>';
  }

  function renderToday() {
    var slot = $('todaySlot'), t = todayISO();
    var rec = md().filter(function (x) { return x.date === t; })[0];
    if (rec) {
      slot.innerHTML = '<div class="sec-head"><h2>今日</h2><span class="count">已記錄・點擊編輯</span></div>' + tradeHTML(rec);
    } else {
      slot.innerHTML = '<div class="sec-head"><h2>今日</h2><span class="count">尚未記錄</span></div>' +
        '<div class="card today-cta"><div class="ic">✍️</div><div class="tx">' +
        '<div class="a">今天還沒記錄交易</div><div class="b">交易完點下方按鈕，記一筆進出場與心得</div></div></div>';
    }
    wire(slot);
  }

  function monthKey(iso) { return iso.slice(0, 7); }
  function monthLabel(k) { var p = k.split('-'); return p[0] + '年' + (+p[1]) + '月'; }

  // 精簡單行列
  function rowHTML(t) {
    var r = res(t), rc = cls(r);
    var dir = t.dir === 'long' ? '<span class="rw-dir">多</span>' : '<span class="rw-dir">空</span>';
    var badge = r > 0 ? '<span class="badge b-win">勝</span>'
      : r < 0 ? '<span class="badge b-loss">敗</span>'
      : '<span class="badge b-flat">平</span>';
    var dot = t.note ? '<span class="rw-note" title="有備註"></span>' : '<span class="rw-note ph"></span>';
    return '<div class="trow' + freshCls(t) + '" tabindex="0" data-date="' + t.date + '">' +
      '<span class="rw-date">' + fmtDate(t.date) + '</span>' + dir +
      '<span class="rw-px">' + nfmt(t.entry) + '<i>→</i>' + nfmt(t.exit) + '</span>' +
      '<span class="rw-res r-' + rc + '">' + signed(r) + '</span>' + badge + dot +
      '</div>';
  }

  var openMonths = null; // 展開中的月份 key 集合（null = 尚未初始化）

  /* renderList(quietExcept)
       undefined / false → 這是「換了一批資料」（換模式、存檔、初次載入）：
                           月份卡與列都播錯開進場。
       '2026-08'（月份 key）→ 展開某個月份：**只有那個月的列**播進場，
                           其他月份不重播（不然點一下手風琴整張清單會跟著閃一次）。
       true                → 收合月份：整張都不播。
     ⚠️ 「這一批要不要播」是狀態，只有 JS 知道；進場動畫本身是 CSS 的事
        （css/motion.css 第 3 段），所以這裡只負責掛不掛 .anim。 */
  function renderList(quietExcept) {
    var quiet = quietExcept !== undefined && quietExcept !== false;
    var openedMk = (typeof quietExcept === 'string') ? quietExcept : null;
    var listAll = md();
    var desc = sortAsc(listAll).reverse().filter(function (x) { return x.date !== todayISO(); });
    $('histCount').textContent = desc.length + ' 筆';
    if (listAll.length === 0) {
      $('list').innerHTML = '<div class="card empty-card"><div class="big">' +
        (mode === 'real' ? '真實操作還沒有紀錄' : '還沒有任何模擬紀錄') + '</div>' +
        '<div class="sm">' + (mode === 'real'
          ? '八月開始，記下你的第一筆真單 💪'
          : '每天交易完記一筆，勝率就會長出來。<br>想還原內建歷史資料？點下方「重設為初始資料」。') +
        '</div></div>';
      return;
    }
    if (desc.length === 0) { $('list').innerHTML = ''; return; }

    // 依月份分組（維持新到舊順序）
    var order = [], map = {};
    desc.forEach(function (t) {
      var k = monthKey(t.date);
      if (!map[k]) { map[k] = []; order.push(k); }
      map[k].push(t);
    });
    if (openMonths === null) { openMonths = {}; openMonths[order[0]] = true; } // 初次/換模式：預設只展開最新月份

    $('list').innerHTML = order.map(function (k) {
      var items = map[k], w = 0, l = 0, net = 0;
      items.forEach(function (t) { var r = res(t); net += r; if (r > 0) w++; else if (r < 0) l++; });
      var rate = Math.round(w / items.length * 100);
      var open = !!openMonths[k];
      var sum = '<span class="ms-rate">勝率 ' + rate + '%</span>' +
        '<span class="ms-wl"><b class="w">' + w + '</b>勝 <b class="l">' + l + '</b>敗</span>' +
        '<span class="ms-net ' + cls(net) + '">' + signed(net) + '點</span>';
      var rowsAnim = (!quiet || k === openedMk) ? ' anim' : '';
      return '<div class="month' + (quiet ? '' : ' anim') + '">' +
        '<div class="month-head' + (open ? ' open' : '') + '" data-mk="' + k + '" tabindex="0" role="button" aria-expanded="' + open + '">' +
          '<span class="mh-l"><span class="chev">▾</span>' + monthLabel(k) + '</span>' +
          '<span class="mh-sum">' + sum + '</span>' +
        '</div>' +
        (open ? '<div class="month-rows' + rowsAnim + '">' + items.map(rowHTML).join('') + '</div>' : '') +
        '</div>';
    }).join('');
    wireList();
  }

  function wireList() {
    var heads = $('list').querySelectorAll('.month-head');
    for (var i = 0; i < heads.length; i++) {
      (function (h) {
        var k = h.getAttribute('data-mk');
        /* 展開 → 只讓這個月的列播進場；收合 → 整張都不播（見 renderList 的說明） */
        var toggle = function () {
          openMonths[k] = !openMonths[k];
          renderList(openMonths[k] ? k : true);
        };
        h.onclick = toggle;
        h.onkeydown = function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); } };
      })(heads[i]);
    }
    var rows = $('list').querySelectorAll('.trow');
    for (var j = 0; j < rows.length; j++) {
      (function (el) {
        var d = el.getAttribute('data-date');
        el.onclick = function () { openSheet(d); };
        el.onkeydown = function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openSheet(d); } };
      })(rows[j]);
    }
  }

  // 「今日」卡片仍用完整卡片樣式
  function wire(root) {
    var els = root.querySelectorAll('.trade');
    for (var i = 0; i < els.length; i++) {
      (function (el) {
        var d = el.getAttribute('data-date');
        el.onclick = function () { openSheet(d); };
        el.onkeydown = function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openSheet(d); } };
      })(els[i]);
    }
  }

  /* ⚠️ finally：畫面畫壞了也要把開場收掉，不然開場會變成當機畫面、
     要停到 splash.js 那條 6 秒保險絲才走。 */
  function renderAll() {
    try { $('feeLabel').textContent = fee; renderSummary(); renderToday(); renderList(); }
    finally { splashReady(); }
  }

  // ---------- top date ----------
  (function () {
    var d = new Date();
    $('todayD').textContent = (d.getMonth() + 1) + '/' + d.getDate();
    $('todayW').textContent = '週' + wd[d.getDay()];
  })();

  // ---------- period control ----------
  var custRange = $('custRange');
  $('seg').addEventListener('click', function (e) {
    var b = e.target.closest('button'); if (!b) return;
    var btns = this.querySelectorAll('button');
    for (var i = 0; i < btns.length; i++) btns[i].classList.remove('on');
    b.classList.add('on');
    if (b.dataset.type === 'custom') {
      var asc = sortAsc(md());
      if (!$('cFrom').value) {
        $('cFrom').value = asc.length ? asc[0].date : todayISO();
        $('cTo').value = todayISO();
      }
      custRange.hidden = false; applyCustom();
    } else {
      custRange.hidden = true;
      period = { type: b.dataset.type, n: +b.dataset.n };
      renderSummary();
    }
  });
  function applyCustom() {
    var f = $('cFrom').value, t = $('cTo').value; if (!f || !t) return;
    if (f > t) { var tmp = f; f = t; t = tmp; }
    period = { type: 'range', from: f, to: t }; renderSummary();
  }
  $('cApply').onclick = applyCustom;

  // ---------- mode switch (模擬 / 真實) ----------
  (function () {
    var bar = $('modeBar');
    function paint() {
      var btns = bar.querySelectorAll('button');
      for (var i = 0; i < btns.length; i++) btns[i].classList.toggle('on', btns[i].dataset.mode === mode);
    }
    paint();
    bar.addEventListener('click', function (e) {
      var b = e.target.closest('button'); if (!b || b.dataset.mode === mode) return;
      saveMode(b.dataset.mode);
      openMonths = null;   // 換模式後重新預設展開最新月份
      paint();
      renderAll();
    });
  })();

  // ---------- sheet (add / edit) ----------
  var sheet = $('sheet'), scrim = $('scrim'), curDir = 'long', curMode = 'sim', editingKey = null;
  var entryEl = $('entry'), exitEl = $('exit'), noteEl = $('note'), dateEl = $('date');

  function setFormMode(m) {
    curMode = m;
    var btns = document.querySelectorAll('#modeToggle button');
    for (var i = 0; i < btns.length; i++) btns[i].setAttribute('aria-pressed', btns[i].dataset.mode === m ? 'true' : 'false');
  }

  // 面板開啟時鎖住背後主頁（避免滑動時背景跟著捲）
  var scrollLockY = 0;
  function lockScroll() {
    scrollLockY = window.scrollY || window.pageYOffset || 0;
    document.body.style.top = (-scrollLockY) + 'px';
    document.body.classList.add('sheet-open');
  }
  function unlockScroll() {
    document.body.classList.remove('sheet-open');
    document.body.style.top = '';
    window.scrollTo(0, scrollLockY);
  }

  function setDir(dir) {
    curDir = dir;
    var btns = document.querySelectorAll('.dir-toggle button');
    for (var i = 0; i < btns.length; i++) btns[i].setAttribute('aria-pressed', btns[i].dataset.dir === dir ? 'true' : 'false');
    updatePreview();
  }

  function openSheet(dateToEdit) {
    var rec = dateToEdit ? md().filter(function (x) { return x.date === dateToEdit; })[0] : null;
    editingKey = rec ? { date: rec.date, mode: rec.mode || 'sim' } : null;
    if (rec) {
      $('sheetTitle').textContent = '編輯交易';
      entryEl.value = rec.entry; exitEl.value = rec.exit; noteEl.value = rec.note || '';
      dateEl.value = rec.date; setDir(rec.dir); setFormMode(rec.mode || 'sim');
      $('deleteBtn').hidden = false;
    } else {
      $('sheetTitle').textContent = mode === 'real' ? '記錄真實交易' : '記錄今日交易';
      entryEl.value = ''; exitEl.value = ''; noteEl.value = '';
      dateEl.value = todayISO(); setDir('long'); setFormMode(mode);
      $('deleteBtn').hidden = true;
    }
    updatePreview();
    lockScroll();
    openPanel(sheet);
  }

  /* ---------- 面板開關的動效（css/motion.css 第 5 段）----------
     開：.show 播 tl-sheet-in／tl-fade-in
     關：換成 .closing 播獨立的 tl-sheet-out／tl-fade-out（**不是**把進場反著跑
         —— animation-name 沒變的話瀏覽器不保證重播）。
     ⚠️ 收尾用計時器把 .closing 拿掉，**不掛 animationend**：
        animationend 被中斷／不觸發時面板會永遠關不掉。
        就算這個計時器沒跑到，.closing 的終態（面板在畫面外、遮罩全透明）
        跟關閉後的靜態值一模一樣 ⇒ 失敗模式是「看不出來」，不是「卡住」。 */
  var CLOSE_MS = 260;               // > --dur-1(180ms) ＋ 餘裕
  var closeTimer = null;
  function panels() { return [scrim, sheet, $('settingsSheet')]; }
  function openPanel(el) {
    clearTimeout(closeTimer);
    var ps = panels();
    for (var i = 0; i < ps.length; i++) { if (ps[i]) ps[i].classList.remove('closing'); }
    scrim.classList.add('show');
    el.classList.add('show');
  }
  function closeSheet() {
    clearTimeout(closeTimer);
    var ps = panels(), closing = [], i;
    for (i = 0; i < ps.length; i++) {
      if (ps[i] && ps[i].classList.contains('show')) {
        ps[i].classList.remove('show');
        ps[i].classList.add('closing');
        closing.push(ps[i]);
      }
    }
    unlockScroll();
    if (!closing.length) return;
    closeTimer = setTimeout(function () {
      for (var j = 0; j < closing.length; j++) closing[j].classList.remove('closing');
    }, CLOSE_MS);
  }

  $('openBtn').onclick = function () { openSheet(null); };
  $('cancelBtn').onclick = closeSheet;
  scrim.onclick = closeSheet;

  var dirBtns = document.querySelectorAll('.dir-toggle button');
  for (var i = 0; i < dirBtns.length; i++) dirBtns[i].onclick = function () { setDir(this.dataset.dir); };
  var modeTgBtns = document.querySelectorAll('#modeToggle button');
  for (var mi = 0; mi < modeTgBtns.length; mi++) modeTgBtns[mi].onclick = function () { setFormMode(this.dataset.mode); };
  entryEl.oninput = updatePreview; exitEl.oninput = updatePreview;

  function updatePreview() {
    var pv = $('preview'), sv = $('saveBtn');
    var e = parseFloat(entryEl.value), x = parseFloat(exitEl.value);
    if (isNaN(e) || isNaN(x)) { pv.className = 'preview idle'; pv.textContent = '輸入進出場點數，自動計算損益'; sv.disabled = true; return; }
    var r = curDir === 'long' ? (x - e) : (e - x), rc = cls(r), lab = r > 0 ? '勝' : r < 0 ? '敗' : '平';
    var netNt = r * TICK - fee;
    pv.className = 'preview';
    pv.innerHTML = '<span class="pv-res r-' + rc + '">' + signed(r) + ' 點</span>' +
      '<span class="pv-lab">·　' + lab + '　·　淨 ' + (netNt < 0 ? '−' : '+') + 'NT$' + nfmt(Math.abs(netNt)) + '</span>';
    sv.disabled = false;
  }

  $('form').onsubmit = function (e) {
    e.preventDefault();
    var entry = parseFloat(entryEl.value), exit = parseFloat(exitEl.value);
    if (isNaN(entry) || isNaN(exit)) return;
    var date = dateEl.value || todayISO(), note = noteEl.value.trim();
    var prev = null;
    data.forEach(function (x) { if (x.date === date && (x.mode || 'sim') === curMode) prev = x; });
    var prevNote = prev ? (prev.note || '') : '', prevNoteAt = prev ? prev.note_at : '';
    // 同模式一天一單：移除該模式同日期、以及編輯前的原紀錄，再寫入
    data = data.filter(function (x) {
      var xm = x.mode || 'sim';
      var isTarget = x.date === date && xm === curMode;
      var isOrig = editingKey && x.date === editingKey.date && xm === editingKey.mode;
      return !isTarget && !isOrig;
    });
    // 心得有改才換時間戳，否則沿用舊的 —— 不然每次編輯進出場價都會讓這筆
    // 在同步時「看起來比較新」，把電腦面板上比較新的心得蓋掉。
    // 【要沿用舊欄位】以前這裡是「整筆重建」，time 沒被帶過來就直接消失了。
    // 而在手機上編輯過的紀錄剛好就是有心得的那幾筆 ⇒ 電腦面板用
    // （日期＋進場時間＋進場價）配對時全部對不上，心得永遠傳不回去。
    var rec = Object.assign({}, prev || {}, {
      date: date, mode: curMode, dir: curDir, entry: entry, exit: exit, note: note
    });
    // 清空也要蓋時間戳，否則「在手機上把心得刪掉」傳不回面板（面板那筆有戳、
    // 這筆沒戳 → 面板判定自己比較新，就把刪掉的字又補回來）。面板端同一套規則。
    if (note !== (prevNote || '')) { rec.note_at = nowStamp(); }
    else if (prevNoteAt) { rec.note_at = prevNoteAt; }
    data.push(rec);
    save(data);
    schedulePush();
    markFresh(date, curMode);       // 讓他在清單上認得出「就是這張」
    closeSheet(); renderAll();
    toast(editingKey ? '已更新' : '已記錄 ✓');
    editingKey = null;
  };

  $('deleteBtn').onclick = function () {
    if (!editingKey) return;
    if (!confirm('確定刪除 ' + fmtDate(editingKey.date) + ' 這筆交易？')) return;
    data = data.filter(function (x) { return !(x.date === editingKey.date && (x.mode || 'sim') === editingKey.mode); });
    save(data); schedulePush(); closeSheet(); renderAll(); toast('已刪除'); editingKey = null;
  };

  // ---------- backup: export / import / sample ----------
  $('exportBtn').onclick = function () {
    if (!data.length) { toast('目前沒有資料'); return; }
    var blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    var url = URL.createObjectURL(blob), a = document.createElement('a');
    a.href = url; a.download = 'trade-log-backup-' + todayISO() + '.json';
    document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
    toast('已匯出備份');
  };
  $('importBtn').onclick = function () { $('importFile').click(); };
  $('importFile').onchange = function () {
    var f = this.files[0]; if (!f) return;
    var reader = new FileReader();
    reader.onload = function () {
      try {
        var arr = JSON.parse(reader.result);
        if (!Array.isArray(arr)) throw new Error('格式錯誤');
        var incoming = withMode(arr.filter(function (x) { return x && x.date && x.dir; }));
        // 合併而非覆蓋 —— 原本是整個取代，同步一次就會蓋掉手機上原有的紀錄
        var seen = {};
        data.forEach(function (x) { seen[x.date + '|' + (x.mode || 'sim')] = 1; });
        var added = 0;
        incoming.forEach(function (x) {
          var k = x.date + '|' + (x.mode || 'sim');
          if (!seen[k]) { data.push(x); seen[k] = 1; added++; }
        });
        save(data); renderAll();
        toast(added ? '已合併 ' + added + ' 筆新紀錄' : '沒有新紀錄（已是最新）');
      } catch (err) { toast('匯入失敗：檔案格式不正確'); }
    };
    reader.readAsText(f); this.value = '';
  };

  $('sampleBtn').onclick = function () {
    if (!confirm('重設為內建的初始資料（' + SEED.length + ' 筆，皆為模擬）？目前的資料會被取代。')) return;
    data = withMode(SEED); save(data); renderAll(); toast('已重設為初始資料');
  };

  // ---------- settings (手續費) ----------
  var settingsSheet = $('settingsSheet');
  $('settingsBtn').onclick = function () {
    $('feeInput').value = fee;
    lockScroll();
    openPanel(settingsSheet);
  };
  $('feeCancelBtn').onclick = closeSheet;
  $('feeSaveBtn').onclick = function () {
    var v = parseFloat($('feeInput').value);
    if (isNaN(v) || v < 0) v = 0;
    saveFee(v); closeSheet(); renderAll(); toast('手續費已設為 NT$' + v);
  };

  // ---------- toast ----------
  var toastTimer;
  function toast(msg) {
    var el = $('toast'); el.textContent = msg; el.classList.add('show');
    clearTimeout(toastTimer); toastTimer = setTimeout(function () { el.classList.remove('show'); }, 1800);
  }

  // ---------- 自動同步：拉回電腦面板推上來的練習紀錄 ----------
  // 手機常不在家裡的網路，所以用 GitHub Pages 當中間人：
  // 面板推 data/practice.json → 手機開 App 時自動抓下來合併。
  // 只有練習（sim）會同步；真實交易不上傳，只存在這台裝置。
  var PULLED_KEY = 'trade-log-pulled';
  function pullPractice() {
    fetch('./data/practice.json?t=' + Date.now(), { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (p) {
        if (!p || !p.trades || !p.trades.length) return;
        var pulled = {};
        try { pulled = JSON.parse(localStorage.getItem(PULLED_KEY) || '{}'); } catch (e) {}
        var seen = {};
        data.forEach(function (x) { seen[x.date + '|' + (x.mode || 'sim')] = 1; });
        // 面板的心得是「事後補寫」的，常常在這筆交易早就同步過來之後才寫。
        // 所以除了塞新紀錄，也要把後來補上的心得補進已存在的那筆 ——
        // 但【只補空白、不覆蓋】，否則會蓋掉他直接在手機上打的字。
        var local = {};
        data.forEach(function (x) { if ((x.mode || 'sim') === 'sim') local[x.date] = x; });
        var added = 0, filled = 0;
        p.trades.forEach(function (t) {
          if (!t || !t.date || !t.dir) return;
          var k = t.date + '|sim';
          // 已經在本機、或使用者曾經刪掉（拉過就記著）→ 不重複塞回來
          if (seen[k] || pulled[k]) {
            var cur = local[t.date];
            if (cur && noteWins(t, cur)) { cur.note = t.note; cur.note_at = t.note_at || nowStamp(); filled++; }
            return;
          }
          data.push({ date: t.date, mode: 'sim', dir: t.dir, entry: Number(t.entry),
                      exit: Number(t.exit), time: t.time || '', note: t.note || '',
                      note_at: t.note_at || '' });
          seen[k] = 1; pulled[k] = 1; added++;
        });
        p.trades.forEach(function (t) { if (t && t.date) pulled[t.date + '|sim'] = 1; });
        try { localStorage.setItem(PULLED_KEY, JSON.stringify(pulled)); } catch (e) {}
        if (added || filled) {
          save(data); renderAll();
          toast(added ? ('已同步 ' + added + ' 筆練習紀錄')
                      : ('已補上 ' + filled + ' 筆心得'));
        }
      })
      .catch(function () {});   // 沒網路或還沒有這個檔案 —— 正常，不吵使用者
  }
  pullPractice();

  // ---------- 手機 → 面板：把練習紀錄寫回 repo ----------
  // 電腦面板讀 data/phone.json。沒有金鑰就安靜跳過（App 照樣能用，只是單向）。
  var pushTimer = null, pushing = false;
  function schedulePush() {
    if (!ghToken()) { syncLine(); return; }
    clearTimeout(pushTimer);
    pushTimer = setTimeout(pushPhone, 1500);   // 連續編輯只推最後一次
  }
  function ghFetch(url, opts) {
    var h = { Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28' };
    var tk = ghToken();
    if (tk) h.Authorization = 'token ' + tk;
    if (opts && opts.body) h['Content-Type'] = 'application/json';
    return fetch(url, { method: (opts && opts.method) || 'GET', headers: h, body: opts && opts.body });
  }
  function b64(str) {
    var b = new TextEncoder().encode(str), s = '';
    for (var i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
    return btoa(s);
  }
  function pushPhone() {
    if (pushing || !ghToken()) return;
    var trades = data.filter(isSim).map(function (x) {
      return { date: x.date, dir: x.dir, entry: Number(x.entry), exit: Number(x.exit),
               time: x.time || '', note: x.note || '', note_at: x.note_at || '' };
    }).sort(function (a, b) { return a.date < b.date ? -1 : 1; });
    var body = JSON.stringify({ updated: nowStamp(), count: trades.length, trades: trades }, null, 2);
    var api = 'https://api.github.com/repos/' + GH.owner + '/' + GH.repo + '/contents/' + PHONE_FILE;
    pushing = true; syncLine('同步中…');
    ghFetch(api + '?ref=' + GH.branch)
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (cur) {
        // 內容一樣就不要送 —— 每次開 App 都推一版只會洗版 commit 紀錄
        if (cur && cur.content) {
          try {
            var old = new TextDecoder().decode(
              Uint8Array.from(atob(cur.content.replace(/\n/g, '')), function (c) { return c.charCodeAt(0); }));
            var a = JSON.parse(old), b = JSON.parse(body);
            if (JSON.stringify(a.trades) === JSON.stringify(b.trades)) { return 'same'; }
          } catch (e) {}
        }
        return ghFetch(api, {
          method: 'PUT',
          body: JSON.stringify({ message: 'chore: 手機同步練習紀錄（' + trades.length + ' 筆）',
                                 content: b64(body), branch: GH.branch,
                                 sha: cur ? cur.sha : undefined })
        }).then(function (r) {
          if (r.ok) return 'ok';
          return r.json().catch(function () { return {}; }).then(function (j) {
            throw new Error(pushMsg(r.status, j));
          });
        });
      })
      .then(function (how) { syncLine(how === 'same' ? '已是最新' : '已同步到電腦面板 ✓'); })
      .catch(function (e) { syncLine('同步失敗：' + (e.message || '連不到 GitHub')); })
      .then(function () { pushing = false; });
  }
  function pushMsg(status, j) {
    if (status === 401) return 'GitHub 金鑰無效或過期，重新解鎖看看';
    if (status === 403) return '金鑰權限不足（要 Contents: Read and write）';
    if (status === 404) return '這把金鑰沒有授權這個 repo';
    if (status === 409) return '剛好有別的裝置在寫，等一下會自動再試';
    return 'GitHub 錯誤 ' + status + '：' + ((j && j.message) || '');
  }
  var syncMsg = '';
  function syncLine(msg) {
    if (msg != null) syncMsg = msg;
    var el = $('syncLine'); if (!el) return;
    el.innerHTML = ghToken()
      ? '練習紀錄與心得會同步到電腦面板 · <span style="opacity:.6">' + (syncMsg || '待命') + '</span><br>'
      : '<span style="opacity:.6">解開鑰匙才會把心得同步回電腦面板</span><br>';
  }

  // ---------- 鑰匙圈 ----------
  if (window.Keyring) {
    Keyring.init({
      appId: 'trade-log',
      appName: '📈 微台指交易日誌',
      tokenKey: TOKEN_KEY,
      enabled: true,
      toast: toast,
      onChange: function () { renderKr(); syncLine(); schedulePush(); }
    });
  }
  function renderKr() {
    var el = $('krChip');
    if (el && window.Keyring) el.innerHTML = Keyring.chipHtml();
  }
  renderKr(); syncLine();
  if (window.Keyring) Keyring.maybeIntro();
  schedulePush();

  // ---------- 版本顯示 + 強制更新 ----------
  // 手機 PWA 的快取很黏，沒有版本號時根本看不出自己在哪一版。
  var APP_VER = 'v20';
  var vl = $('verLabel'); if (vl) vl.textContent = '版本 ' + APP_VER;
  var fu = $('forceUpdBtn');
  if (fu) fu.onclick = function () {
    toast('更新中…');
    var jobs = [];
    if ('serviceWorker' in navigator) {
      jobs.push(navigator.serviceWorker.getRegistrations().then(function (rs) {
        return Promise.all(rs.map(function (r) { return r.unregister(); }));
      }));
    }
    if (window.caches) {
      jobs.push(caches.keys().then(function (ks) {
        return Promise.all(ks.map(function (k) { return caches.delete(k); }));
      }));
    }
    Promise.all(jobs).then(function () {
      location.replace(location.pathname + '?u=' + Date.now());
    });
  };

  renderAll();
})();
