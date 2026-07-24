(function () {
  'use strict';

  var KEY = 'trade-log-v1';
  var TICK = 10; // 微台指每點 NT$10

  // ---------- storage ----------
  function load() {
    try { var r = localStorage.getItem(KEY); if (r) return JSON.parse(r); } catch (e) {}
    return [];
  }
  function save(d) { try { localStorage.setItem(KEY, JSON.stringify(d)); } catch (e) {} }
  var data = load();

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
    var asc = sortAsc(data), ref = todayISO();
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
      $('sparkN').textContent = '0 筆交易';
      $('spark').innerHTML = '';
      return;
    }
    $('rateNum').textContent = s.rate;
    wl.innerHTML = '<span class="w"><b>' + s.w + '</b> 勝</span>　<span class="l"><b>' + s.l + '</b> 敗</span>' +
      (s.f ? '　<span class="f"><b>' + s.f + '</b> 平</span>' : '');
    net.className = 'net ' + cls(s.net);
    net.innerHTML = '<span class="v">' + signed(s.net) + '</span><span class="u">點</span>';
    var nt = s.net * TICK;
    cash.textContent = '≈ ' + (nt < 0 ? '−' : '+') + 'NT$' + nfmt(Math.abs(nt));
    $('sparkN').textContent = list.length + ' 筆交易';
    renderSpark(list);
  }

  function svgEl(name, attrs) {
    var e = document.createElementNS('http://www.w3.org/2000/svg', name);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }
  function renderSpark(list) {
    var svg = $('spark'); svg.innerHTML = '';
    if (list.length < 2) return;
    var cum = [], run = 0;
    list.forEach(function (t) { run += res(t); cum.push(run); });
    var min = Math.min(0, Math.min.apply(null, cum)), max = Math.max(0, Math.max.apply(null, cum));
    var range = (max - min) || 1, W = 100, H = 40, pad = 3;
    var X = function (i) { return (i / (list.length - 1)) * W; };
    var Y = function (v) { return H - pad - ((v - min) / range) * (H - pad * 2); };
    var pts = cum.map(function (v, i) { return X(i) + ',' + Y(v); });
    var up = cum[cum.length - 1] >= 0, col = up ? 'var(--win)' : 'var(--loss)', y0 = Y(0);
    svg.appendChild(svgEl('line', { x1: 0, x2: W, y1: y0, y2: y0, stroke: 'var(--line)', 'stroke-width': 1, 'stroke-dasharray': '2 2', 'vector-effect': 'non-scaling-stroke' }));
    svg.appendChild(svgEl('polygon', { points: '0,' + y0 + ' ' + pts.join(' ') + ' ' + W + ',' + y0, fill: col, opacity: .12 }));
    svg.appendChild(svgEl('polyline', { points: pts.join(' '), fill: 'none', stroke: col, 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round', 'vector-effect': 'non-scaling-stroke' }));
    var last = cum.length - 1;
    svg.appendChild(svgEl('circle', { cx: X(last), cy: Y(cum[last]), r: 2.4, fill: col, stroke: 'var(--surface)', 'stroke-width': 1.5, 'vector-effect': 'non-scaling-stroke' }));
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
    var rec = data.filter(function (x) { return x.date === t; })[0];
    if (rec) {
      slot.innerHTML = '<div class="sec-head"><h2>今日</h2><span class="count">已記錄・點擊編輯</span></div>' + tradeHTML(rec);
    } else {
      slot.innerHTML = '<div class="sec-head"><h2>今日</h2><span class="count">尚未記錄</span></div>' +
        '<div class="card today-cta"><div class="ic">✍️</div><div class="tx">' +
        '<div class="a">今天還沒記錄交易</div><div class="b">交易完點下方按鈕，記一筆進出場與心得</div></div></div>';
    }
    wire(slot);
  }

  function renderList() {
    var desc = sortAsc(data).reverse().filter(function (x) { return x.date !== todayISO(); });
    $('histCount').textContent = desc.length + ' 筆';
    if (desc.length === 0 && data.length === 0) {
      $('list').innerHTML = '<div class="card empty-card"><div class="big">還沒有任何交易紀錄</div>' +
        '<div class="sm">每天交易完記一筆，勝率與走勢就會長出來。<br>想先看看畫面？點下方「載入範例資料」。</div></div>';
      return;
    }
    if (desc.length === 0) { $('list').innerHTML = ''; return; }
    $('list').innerHTML = desc.map(tradeHTML).join('');
    wire($('list'));
  }

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

  function renderAll() { renderSummary(); renderToday(); renderList(); }

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
      var asc = sortAsc(data);
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

  // ---------- sheet (add / edit) ----------
  var sheet = $('sheet'), scrim = $('scrim'), curDir = 'long', editingDate = null;
  var entryEl = $('entry'), exitEl = $('exit'), noteEl = $('note'), dateEl = $('date');

  function setDir(dir) {
    curDir = dir;
    var btns = document.querySelectorAll('.dir-toggle button');
    for (var i = 0; i < btns.length; i++) btns[i].setAttribute('aria-pressed', btns[i].dataset.dir === dir ? 'true' : 'false');
    updatePreview();
  }

  function openSheet(dateToEdit) {
    editingDate = dateToEdit || null;
    var rec = editingDate ? data.filter(function (x) { return x.date === editingDate; })[0] : null;
    if (rec) {
      $('sheetTitle').textContent = '編輯交易';
      entryEl.value = rec.entry; exitEl.value = rec.exit; noteEl.value = rec.note || '';
      dateEl.value = rec.date; setDir(rec.dir);
      $('deleteBtn').hidden = false;
    } else {
      $('sheetTitle').textContent = '記錄今日交易';
      entryEl.value = ''; exitEl.value = ''; noteEl.value = '';
      dateEl.value = todayISO(); setDir('long');
      $('deleteBtn').hidden = true;
    }
    updatePreview();
    scrim.classList.add('show'); sheet.classList.add('show');
  }
  function closeSheet() { scrim.classList.remove('show'); sheet.classList.remove('show'); }

  $('openBtn').onclick = function () { openSheet(null); };
  $('cancelBtn').onclick = closeSheet;
  scrim.onclick = closeSheet;

  var dirBtns = document.querySelectorAll('.dir-toggle button');
  for (var i = 0; i < dirBtns.length; i++) dirBtns[i].onclick = function () { setDir(this.dataset.dir); };
  entryEl.oninput = updatePreview; exitEl.oninput = updatePreview;

  function updatePreview() {
    var pv = $('preview'), sv = $('saveBtn');
    var e = parseFloat(entryEl.value), x = parseFloat(exitEl.value);
    if (isNaN(e) || isNaN(x)) { pv.className = 'preview idle'; pv.textContent = '輸入進出場點數，自動計算損益'; sv.disabled = true; return; }
    var r = curDir === 'long' ? (x - e) : (e - x), rc = cls(r), lab = r > 0 ? '勝' : r < 0 ? '敗' : '平';
    pv.className = 'preview';
    pv.innerHTML = '<span class="pv-res r-' + rc + '">' + signed(r) + ' 點</span>' +
      '<span class="pv-lab">·　' + lab + '　·　≈ ' + (r * TICK < 0 ? '−' : '+') + 'NT$' + nfmt(Math.abs(r * TICK)) + '</span>';
    sv.disabled = false;
  }

  $('form').onsubmit = function (e) {
    e.preventDefault();
    var entry = parseFloat(entryEl.value), exit = parseFloat(exitEl.value);
    if (isNaN(entry) || isNaN(exit)) return;
    var date = dateEl.value || todayISO(), note = noteEl.value.trim();
    // 一天一單：移除舊的（編輯前日期）與目標日期，再寫入
    data = data.filter(function (x) { return x.date !== date && x.date !== editingDate; });
    data.push({ date: date, dir: curDir, entry: entry, exit: exit, note: note });
    save(data);
    closeSheet(); renderAll();
    toast(editingDate ? '已更新' : '已記錄 ✓');
    editingDate = null;
  };

  $('deleteBtn').onclick = function () {
    if (!editingDate) return;
    if (!confirm('確定刪除 ' + fmtDate(editingDate) + ' 這筆交易？')) return;
    data = data.filter(function (x) { return x.date !== editingDate; });
    save(data); closeSheet(); renderAll(); toast('已刪除'); editingDate = null;
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
        data = arr.filter(function (x) { return x && x.date && x.dir; });
        save(data); renderAll(); toast('已匯入 ' + data.length + ' 筆');
      } catch (err) { toast('匯入失敗：檔案格式不正確'); }
    };
    reader.readAsText(f); this.value = '';
  };

  $('sampleBtn').onclick = function () {
    if (data.length && !confirm('載入範例會取代目前資料，確定嗎？')) return;
    data = buildSample(); save(data); renderAll(); toast('已載入範例資料');
  };

  function buildSample() {
    var tpl = [
      ['long', 23010, 23040, '開盤紅K順勢進場，有守住停利'],
      ['short', 23080, 23050, '跌破前低放空，達標就出'],
      ['long', 23120, 23100, '追高被套，停損認賠'],
      ['long', 23060, 23110, '等回踩支撐再進，紀律有守'],
      ['short', 23150, 23170, '逆勢空，錯了快跑'],
      ['long', 23200, 23260, '跳空續強抱住，今天最順的一單'],
      ['short', 23300, 23280, '高檔爆量轉弱，小賺'],
      ['long', 23250, 23230, '假突破被巴一巴，該再等確認'],
      ['long', 23180, 23180, '盤整沒方向，平出場保本'],
      ['short', 23220, 23160, '週一開高走低，順勢波段'],
      ['long', 23100, 23070, '手癢搶反彈，失敗，要戒掉'],
      ['long', 23050, 23120, '止跌背離進場，抱到大波段'],
      ['short', 23200, 23210, '太早空被軋一下，停損'],
      ['long', 23180, 23240, '突破盤整帶續抱，紀律優'],
      ['long', 23280, 23260, '開高追多回檔停損，別追高'],
      ['short', 23320, 23260, '高檔十字轉空，達標出場'],
      ['long', 23230, 23250, '回測支撐進場，小賺'],
      ['short', 23300, 23290, '小賺就走，紀律優先']
    ];
    // 從昨天往回取足夠的交易日（週一~週五）
    var dates = [], d = addDays(parseISO(todayISO()), -1);
    while (dates.length < tpl.length) {
      var wday = d.getDay();
      if (wday !== 0 && wday !== 6) dates.unshift(toISO(d));
      d = addDays(d, -1);
    }
    return tpl.map(function (t, i) {
      return { date: dates[i], dir: t[0], entry: t[1], exit: t[2], note: t[3] };
    });
  }

  // ---------- toast ----------
  var toastTimer;
  function toast(msg) {
    var el = $('toast'); el.textContent = msg; el.classList.add('show');
    clearTimeout(toastTimer); toastTimer = setTimeout(function () { el.classList.remove('show'); }, 1800);
  }

  renderAll();
})();
