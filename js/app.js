(function () {
  'use strict';

  var KEY = 'trade-log-v1';
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
    return '<div class="trade" tabindex="0" data-date="' + t.date + '">' +
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
    return '<div class="trow" tabindex="0" data-date="' + t.date + '">' +
      '<span class="rw-date">' + fmtDate(t.date) + '</span>' + dir +
      '<span class="rw-px">' + nfmt(t.entry) + '<i>→</i>' + nfmt(t.exit) + '</span>' +
      '<span class="rw-res r-' + rc + '">' + signed(r) + '</span>' + badge + dot +
      '</div>';
  }

  var openMonths = null; // 展開中的月份 key 集合（null = 尚未初始化）

  function renderList() {
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
      return '<div class="month">' +
        '<div class="month-head' + (open ? ' open' : '') + '" data-mk="' + k + '" tabindex="0" role="button" aria-expanded="' + open + '">' +
          '<span class="mh-l"><span class="chev">▾</span>' + monthLabel(k) + '</span>' +
          '<span class="mh-sum">' + sum + '</span>' +
        '</div>' +
        (open ? '<div class="month-rows">' + items.map(rowHTML).join('') + '</div>' : '') +
        '</div>';
    }).join('');
    wireList();
  }

  function wireList() {
    var heads = $('list').querySelectorAll('.month-head');
    for (var i = 0; i < heads.length; i++) {
      (function (h) {
        var k = h.getAttribute('data-mk');
        var toggle = function () { openMonths[k] = !openMonths[k]; renderList(); };
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

  function renderAll() { $('feeLabel').textContent = fee; renderSummary(); renderToday(); renderList(); }

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
    scrim.classList.add('show'); sheet.classList.add('show');
  }
  function closeSheet() {
    scrim.classList.remove('show');
    sheet.classList.remove('show');
    $('settingsSheet').classList.remove('show');
    unlockScroll();
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
    // 同模式一天一單：移除該模式同日期、以及編輯前的原紀錄，再寫入
    data = data.filter(function (x) {
      var xm = x.mode || 'sim';
      var isTarget = x.date === date && xm === curMode;
      var isOrig = editingKey && x.date === editingKey.date && xm === editingKey.mode;
      return !isTarget && !isOrig;
    });
    data.push({ date: date, mode: curMode, dir: curDir, entry: entry, exit: exit, note: note });
    save(data);
    closeSheet(); renderAll();
    toast(editingKey ? '已更新' : '已記錄 ✓');
    editingKey = null;
  };

  $('deleteBtn').onclick = function () {
    if (!editingKey) return;
    if (!confirm('確定刪除 ' + fmtDate(editingKey.date) + ' 這筆交易？')) return;
    data = data.filter(function (x) { return !(x.date === editingKey.date && (x.mode || 'sim') === editingKey.mode); });
    save(data); closeSheet(); renderAll(); toast('已刪除'); editingKey = null;
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
        if (data.length && !confirm('匯入會取代目前 ' + data.length + ' 筆資料，確定嗎？')) return;
        data = withMode(arr.filter(function (x) { return x && x.date && x.dir; }));
        save(data); renderAll(); toast('已匯入 ' + data.length + ' 筆');
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
    scrim.classList.add('show'); settingsSheet.classList.add('show');
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

  // ---------- 版本顯示 + 強制更新 ----------
  // 手機 PWA 的快取很黏，沒有版本號時根本看不出自己在哪一版。
  var APP_VER = 'v15';
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
