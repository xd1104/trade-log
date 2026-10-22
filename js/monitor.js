/* 早盤儀表板・手機監控（2026-09-24）。
 * ⛔ 純看：這一頁沒有任何會動到交易的東西（沒有按鈕打面板、面板也沒有對外開任何門）。
 * 資料：面板每 2 分鐘把「加密快照」推到 GitHub 的 monitor 分支（tools/shioaji/monitor_push.py）。
 * 解密（跟 monitor_push.py 同一套，改要兩邊一起改）：
 *   PBKDF2-SHA256(密碼, salt, iter) ⇒ 64 bytes ＝ k_enc ‖ k_mac
 *   tag ＝ HMAC(k_mac, "tlmon1" ‖ t ‖ nonce ‖ ct)；先驗 tag，再用 HMAC(k_enc, nonce ‖ 序號) 的金鑰流 XOR 解開
 * ⚠️ 「記住這台手機」存的是算出來的鑰匙（不是密碼本身）。
 */
(function () {
  'use strict';
  var API = 'https://api.github.com/repos/xd1104/trade-log/contents/monitor.json?ref=monitor';
  var RAW = 'https://raw.githubusercontent.com/xd1104/trade-log/monitor/monitor.json';
  var POLL_MS = 90 * 1000;          // 不帶 token 的 GitHub API 一小時 60 次 ⇒ 90 秒一次（304 不算次數）
  var SLOW_S = 5 * 60, DEAD_S = 8 * 60;   // 面板 2 分鐘推一次、GitHub 最多再慢 1 分鐘
  var LS = 'tlmon.key.v1';
  var MAGIC = new TextEncoder().encode('tlmon1');

  var $ = function (id) { return document.getElementById(id); };
  var keys = null, lastBox = null, etag = null, lastFetchOk = null, fetchErr = null, timer = null;

  // ── base64 / bytes ──
  function b64(s) { var b = atob(s), u = new Uint8Array(b.length); for (var i = 0; i < b.length; i++) u[i] = b.charCodeAt(i); return u; }
  function ub64(u) { var s = ''; for (var i = 0; i < u.length; i++) s += String.fromCharCode(u[i]); return btoa(s); }
  function cat() { var n = 0, i, a = arguments; for (i = 0; i < a.length; i++) n += a[i].length;
    var o = new Uint8Array(n), p = 0; for (i = 0; i < a.length; i++) { o.set(a[i], p); p += a[i].length; } return o; }
  function eq(a, b) { if (a.length !== b.length) return false; var d = 0; for (var i = 0; i < a.length; i++) d |= a[i] ^ b[i]; return d === 0; }

  // ── 加解密 ──
  function derive(pw, salt, iter) {
    return crypto.subtle.importKey('raw', new TextEncoder().encode(pw), 'PBKDF2', false, ['deriveBits'])
      .then(function (k) { return crypto.subtle.deriveBits({ name: 'PBKDF2', salt: salt, iterations: iter, hash: 'SHA-256' }, k, 512); })
      .then(function (bits) { var u = new Uint8Array(bits); return { salt: ub64(salt), iter: iter, enc: u.slice(0, 32), mac: u.slice(32) }; });
  }
  function hmacKey(raw) { return crypto.subtle.importKey('raw', raw, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']); }
  function hmac(key, data) { return crypto.subtle.sign('HMAC', key, data).then(function (s) { return new Uint8Array(s); }); }

  function open(box, k) {
    var nonce = b64(box.nonce), ct = b64(box.ct), tag = b64(box.tag);
    var t = new TextEncoder().encode(String(box.t));
    return Promise.all([hmacKey(k.mac), hmacKey(k.enc)]).then(function (hk) {
      return hmac(hk[0], cat(MAGIC, t, nonce, ct)).then(function (want) {
        if (!eq(want, tag)) throw new Error('bad');
        var n = Math.ceil(ct.length / 32), jobs = [];
        for (var i = 0; i < n; i++) {
          var ctr = new Uint8Array([(i >>> 24) & 255, (i >>> 16) & 255, (i >>> 8) & 255, i & 255]);
          jobs.push(hmac(hk[1], cat(nonce, ctr)));
        }
        return Promise.all(jobs).then(function (blocks) {
          var ks = cat.apply(null, blocks), pt = new Uint8Array(ct.length);
          for (var j = 0; j < ct.length; j++) pt[j] = ct[j] ^ ks[j];
          return JSON.parse(new TextDecoder().decode(pt));
        });
      });
    });
  }

  // ── 記住的鑰匙 ──
  function saveKeys(k) { try { localStorage.setItem(LS, JSON.stringify({ salt: k.salt, iter: k.iter, enc: ub64(k.enc), mac: ub64(k.mac) })); } catch (e) {} }
  function loadKeys() {
    try { var o = JSON.parse(localStorage.getItem(LS) || 'null'); if (!o) return null;
      return { salt: o.salt, iter: o.iter, enc: b64(o.enc), mac: b64(o.mac) }; } catch (e) { return null; }
  }
  function clearKeys() { try { localStorage.removeItem(LS); } catch (e) {} }

  // ── 抓資料（API 為主：最多慢 60 秒；被限流才退到 raw：最多慢 5 分鐘）──
  function fetchBox() {
    var h = { 'Accept': 'application/vnd.github.raw' };
    if (etag) h['If-None-Match'] = etag;
    return fetch(API, { headers: h, cache: 'no-store' }).then(function (r) {
      if (r.status === 304 && lastBox) return lastBox;
      if (!r.ok) throw new Error('api ' + r.status);
      etag = r.headers.get('ETag') || etag;
      return r.json();
    }).catch(function () {
      return fetch(RAW + '?t=' + Date.now(), { cache: 'no-store' }).then(function (r) {
        if (!r.ok) throw new Error('raw ' + r.status);
        return r.json();
      });
    }).then(function (box) { lastBox = box; lastFetchOk = Date.now(); fetchErr = null; return box; });
  }

  // ── 畫面小工具 ──
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function pm(x) { if (x == null || isNaN(x)) return '—'; x = Math.round(x * 10) / 10; return (x > 0 ? '+' : x < 0 ? '−' : '') + Math.abs(x).toLocaleString(); }
  function cls(x) { return x > 0 ? 'win' : x < 0 ? 'loss' : ''; }
  function row(k, v) { return '<div class="row"><span class="k">' + esc(k) + '</span><span class="v">' + v + '</span></div>'; }
  function ago(s) { s = Math.max(0, Math.round(s)); if (s < 60) return s + ' 秒前'; if (s < 3600) return Math.floor(s / 60) + ' 分鐘前';
    var h = Math.floor(s / 3600); return h + ' 小時 ' + Math.floor((s % 3600) / 60) + ' 分前'; }
  function dirTxt(d) { return d === 'long' ? '做多' : d === 'short' ? '做空' : (d || ''); }

  // ══ 【交易分析師】週報信件（2026-09-24）══════════════════════════════
  // 列表跟著快照來（s.analyst：週次、結論、讀過沒）；全文在 monitor 分支的 analyst.json（加密，點開才抓）。
  // 讀過：先記在這台手機（立刻消失），再用鑰匙圈的金鑰把 data/analyst-read.json 寫進 repo ⇒ 電腦面板 3 分鐘內同步。
  // ⛔ 寫進 repo 的只有「週次＋時間」，沒有任何內容（repo 是公開的）。
  var AN_API = 'https://api.github.com/repos/xd1104/trade-log/contents/analyst.json?ref=monitor';
  var AN_RAW = 'https://raw.githubusercontent.com/xd1104/trade-log/monitor/analyst.json';
  var AN_READ_API = 'https://api.github.com/repos/xd1104/trade-log/contents/data/analyst-read.json';
  var AN_LS = 'tlmon.anread.v1', GH_TOKEN_KEY = 'tradelog_gh_pat';
  var AN = { snap: null, box: null, reports: null, view: null, cur: null, syncing: false, syncMsg: '' };
  var AN_TAG = { data: '有數據', judge: '判讀・未驗證' };
  var AN_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="M3.5 6.5 12 13l8.5-6.5"/></svg>';

  function anLocal() { try { return JSON.parse(localStorage.getItem(AN_LS) || '{}') || {}; } catch (e) { return {}; } }
  function anSaveLocal(m) { try { localStorage.setItem(AN_LS, JSON.stringify(m)); } catch (e) {} }
  // ⭐ 讀的時間 ≥ 週報產生時間才算讀過（同一週重新產生過 ⇒ 會再變回未讀）。時間一律手機本地（台灣）ISO。
  function localIso() { var d = new Date(); return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 19); }
  function anIsRead(it) { var at = anLocal()[it.id]; return !!(it.read || (typeof at === 'string' && at >= String(it.made_at || '').slice(0, 19))); }
  function anItems() { return ((AN.snap || {}).items) || []; }
  function anTag(t) { return t ? '<span class="an-tag ' + (t === 'data' ? 'data' : 'judge') + '">' + esc(AN_TAG[t] || t) + '</span>' : ''; }
  function anLamp(l, w) { return '<span class="an-lamp ' + esc(l || '') + '">' + esc(w || ({ ok: '正常', wn: '要注意', bd: '要處理' })[l] || '—') + '</span>'; }
  function anWeek(id) { return String(id || '').replace('-W', ' 第 ') + ' 週'; }
  function ghToken() { try { return localStorage.getItem(GH_TOKEN_KEY) || ''; } catch (e) { return ''; } }

  function anPaintBtn() {
    var b = $('anMail'); if (!b) return;
    var items = anItems(), n = items.filter(function (x) { return !anIsRead(x); }).length;
    b.hidden = !AN.snap;
    b.innerHTML = AN_SVG + (n ? '<span class="bdg">' + n + '</span>' : '');
    b.className = 'mailbtn' + (n ? ' has' : '');
  }
  function anShow(view) {
    AN.view = view;
    $('main').hidden = !!view; $('anView').hidden = !view;
    if (view === 'list') anPaintList(); else if (view) anPaintReport();
    window.scrollTo(0, 0);
  }
  function anPaintList() {
    var items = anItems(), n = items.filter(function (x) { return !anIsRead(x); }).length;
    var h = '<div class="an-back"><button data-an="home">‹ 返回監控</button><span>' + (n ? n + ' 份沒讀' : '全部讀過了') + '</span></div>';
    if (!items.length) h += '<div class="card"><div class="msg">還沒有週報。每週六早上會自動產生。</div></div>';
    h += items.map(function (it) {
      var r = anIsRead(it);
      return '<button class="an-item' + (r ? '' : ' unread') + '" data-anid="' + esc(it.id) + '"><span class="dot"></span><span class="bd">' +
        '<span class="r1"><span>' + esc(anWeek(it.id)) + '　' + esc(it.range || '') + '</span><small>' + esc(String(it.made_at || '').slice(5, 10)) + '</small></span>' +
        '<span class="ln">' + esc(it.line || '') + '</span><span class="ch">' + (r ? '' : '<span class="an-new">未讀</span>') +
        anLamp(it.lamp, it.lamp_word) + (it.n_recs ? '<span class="an-tag judge">' + it.n_recs + ' 條建議</span>' : '') + '</span></span></button>';
    }).join('');
    $('anView').innerHTML = h;
  }
  function anFetchReports() {
    var want = (AN.snap || {}).hash;
    if (AN.reports && AN.box && AN.box.hash === want) return Promise.resolve(AN.reports);
    return fetch(AN_API, { headers: { 'Accept': 'application/vnd.github.raw' }, cache: 'no-store' }).then(function (r) {
      if (!r.ok) throw new Error('api ' + r.status); return r.json();
    }).catch(function () {
      return fetch(AN_RAW + '?t=' + Date.now(), { cache: 'no-store' }).then(function (r) { if (!r.ok) throw new Error('raw ' + r.status); return r.json(); });
    }).then(function (box) {
      return open(box, keys).then(function (o) { AN.box = box; AN.reports = o.reports || []; return AN.reports; });
    });
  }
  function anSec(id, title, body) { return '<section class="card" id="an-s-' + id + '"><h2>' + esc(title) + '</h2>' + body + '</section>'; }
  function anReportHTML(R) {
    var F = R.facts || {};
    var news = (R.news || []).map(function (n) {
      return '<div class="an-nw"><div class="tp">' + esc(n.date) + ' ' + anTag(n.tag) + '</div><h3>' + esc(n.title) + '</h3><p>' + esc(n.summary) + '</p>' +
        (n.impacts || []).map(function (i) { return '<div class="imp"><b>' + esc(i.who) + '</b>　' + esc(i.text) + '</div>'; }).join('') +
        '<div class="src">來源：' + (n.sources || []).map(function (s) { return '<a href="' + esc(s.url) + '" target="_blank" rel="noopener noreferrer">' + esc(s.title) + '</a>'; }).join('、') + '</div></div>';
    }).join('');
    var cal = (R.calendar || []).map(function (c) { return row(c.when, anTag(c.tag) + ' ' + esc(c.event) + (c.history ? '<div class="faint" style="font-size:12px">' + esc(c.history) + '</div>' : '')); }).join('') || '<div class="msg">這週沒有特別要注意的事。</div>';
    var env = (R.env || []).map(function (e) { return '<div class="an-nw"><div class="tp">' + anTag(e.tag) + (e.status ? ' ' + anLamp('', e.status) : '') + '</div><h3>' + esc(e.title) + '</h3><p>' + esc(e.text) + '</p></div>'; }).join('');
    var st = (F.strategies || []).map(function (s) {
      return '<div class="an-nw"><div class="tp">' + anLamp((s.lamp === 'ok' || s.lamp === 'g') ? 'ok' : ((s.lamp === 'r' || s.lamp === 'rr' || s.lamp === 'bd') ? 'bd' : 'wn'), s.lamp_word) + '</div><h3>' + esc(s.name) + '</h3>' +
        row('本週模擬', '<span class="num ' + cls(s.week_sim_pts) + '">' + pm(s.week_sim_pts) + '</span>（' + s.week_sim_n + ' 筆）') +
        row('本週真單', '<span class="num ' + cls(s.week_real_pts) + '">' + pm(s.week_real_pts) + '</span>（' + s.week_real_n + ' 筆）') +
        row('近 15 筆每筆／歷史', '<span class="num">' + pm(s.avg15) + ' / ' + pm(s.avg_all) + '</span>') + '</div>';
    }).join('');
    var mk = (((F.market || {}).cards) || []).map(function (c) { return row(c.title, '<span class="num">' + (c.value == null ? '—' : esc(c.value) + esc(c.unit || '')) + '</span>' + (c.pct == null ? '' : '<div class="faint" style="font-size:11.5px">過去一年第 ' + c.pct + ' 百分位</div>')); }).join('');
    // ⭐ 2026-09-29 盤整期那一行：面板算好的整句，放在市場狀態最上面（⛔ 手機不自己判斷）
    var rg = (F.market || {}).regime;
    if (rg && rg.line) mk = '<div class="an-nw"><div class="tp">' + (rg.level ? anLamp(rg.level === '低' ? 'wn' : 'ok', rg.level + '波動') : '') + '</div><h3>現在是不是盤整期</h3><p>' + esc(rg.line) + '</p></div>' + mk;
    // ⭐ 2026-09-29 退休標準（面板算好的整句；⛔ 手機不自己判斷）
    var RL = { ok: 'ok', back: 'ok', watch: 'wn', down: 'bd', retire: 'bd' };
    var ret = (F.retire || []).map(function (r) {
      return '<div class="an-nw"><div class="tp">' + anLamp(RL[r.level] || '', r.word || '—') + '</div><h3>' + esc(r.name) + '</h3>' +
        (r.why || []).map(function (w) { return '<p>' + esc(w) + '</p>'; }).join('') + '</div>';
    }).join('');
    var S = F.system || {}, K = F.risk || {};
    var sys = row('日盤送單', (S.sent_day || 0) + ' 筆（成交 ' + (S.ok_day || 0) + '）') + row('夜盤送單', (S.sent_night || 0) + ' 筆（成交 ' + (S.ok_night || 0) + '）') +
      row('進場滑價', S.slip_avg == null ? '—' : '平均 ' + S.slip_avg + ' 點') + row('送出沒撮到', (S.ioc_nofill || 0) + ' 次') +
      row('本月風控', '<span class="num ' + cls(K.pnl) + '">' + pm(K.pnl) + '</span> / −' + Math.round(K.cap || 0).toLocaleString() + ' 點' + (K.blocked ? '（已停）' : '')) +
      (S.problems || []).map(function (p) { return '<div class="err">⚠️ ' + esc(p.what) + '（' + p.n + ' 次）</div>'; }).join('');
    var cand = (F.candidates || []).map(function (c) { return row(c.name, '<span class="num">' + c.diff_n + ' / ' + c.need + '</span><div class="faint" style="font-size:11.5px">只算跟「' + esc(c.base) + '」不一樣的</div>'); }).join('');
    var recs = (R.recs || []).map(function (r) { return '<div class="an-nw an-rec"><div class="tp">' + anTag(r.tag) + '</div><h3>' + esc(r.title) + '</h3><p>' + esc(r.body) + '</p><div class="ask">要你決定：' + esc(r.ask) + '</div></div>'; }).join('') || '<div class="msg">這週沒有建議。</div>';
    var segs = [['news', '國際消息'], ['cal', '下週大事'], ['env', '大環境'], ['st', '策略'], ['ret', '退休標準'], ['mk', '市場'], ['sys', '系統'], ['cand', '候選'], ['rec', '建議']];
    return '<div class="an-seg">' + segs.map(function (x, i) { return '<button data-anjump="' + x[0] + '"' + (i ? '' : ' class="on"') + '>' + x[1] + '</button>'; }).join('') + '</div>' +
      '<div class="card"><div class="an-verdict">' + anLamp(R.verdict && R.verdict.lamp) + '<p>' + esc((R.verdict || {}).line || '') + '</p></div></div>' +
      anSec('news', '國際金融消息（每則附來源）', news) + anSec('cal', '下週大事', cal) + (env ? anSec('env', '大環境觀察', env) : '') +
      anSec('st', '策略健康', st) + (ret ? anSec('ret', '退休標準（開關由你按）', ret) : '') + anSec('mk', '市場狀態', mk) + anSec('sys', '系統與風控', sys) + anSec('cand', '模擬候選（滿 25 筆才判斷）', cand) +
      anSec('rec', '建議（決定權在你）', recs) +
      '<div class="an-sync" id="anSync"></div>' +
      '<div class="foot dim">分析師不預測漲跌、不給進出場方向、不碰下單；「判讀・未驗證」的只能當研究題目。</div>' +
      '<div class="an-totop"><button data-antop="1">↑ 回到最上面</button></div>';
  }
  function anPaintSync() {
    var el = $('anSync'); if (!el) return;
    el.innerHTML = ghToken() ? esc(AN.syncMsg || '讀過的紀錄會同步到電腦面板')
      : '讀過的紀錄只記在這台手機 —— <button data-an="unlock">解鎖鑰匙圈</button>就能同步到電腦面板';
  }
  function anPaintReport() {
    var id = AN.cur;
    $('anView').innerHTML = '<div class="an-back"><button data-an="list">‹ 週報列表</button><span>' + esc(anWeek(id)) + '</span></div><div class="card"><div class="msg">載入中…</div></div>';
    anFetchReports().then(function (reps) {
      var R = null; for (var i = 0; i < reps.length; i++) if (reps[i].id === id) R = reps[i];
      if (!R) throw new Error('找不到這一週（面板可能還沒推上來，等 2 分鐘再試）');
      $('anView').innerHTML = '<div class="an-back"><button data-an="list">‹ 週報列表</button><span>' + esc(anWeek(id)) + '・' + esc(R.range || '') + '</span></div>' + anReportHTML(R);
      var m = anLocal(); m[id] = localIso(); anSaveLocal(m);
      anPaintBtn(); anPaintSync(); anSync();
      if (!ghToken() && window.Keyring && !anLocal()._asked) { var mm = anLocal(); mm._asked = 1; anSaveLocal(mm); Keyring.open('把讀過的週報同步到電腦面板'); }
    }).catch(function (e) {
      $('anView').innerHTML = '<div class="an-back"><button data-an="list">‹ 週報列表</button></div><div class="card"><div class="err">讀不到週報：' + esc(e && e.message || e) + '</div></div>';
    });
  }
  function b64s(str) { var b = new TextEncoder().encode(str), s = ''; for (var i = 0; i < b.length; i++) s += String.fromCharCode(b[i]); return btoa(s); }
  function anSync() {
    var tok = ghToken(); if (!tok || AN.syncing) return;
    var mine = anLocal(); delete mine._asked;
    if (!Object.keys(mine).length) return;
    AN.syncing = true;
    var h = { 'Accept': 'application/vnd.github+json', 'Authorization': 'Bearer ' + tok };
    fetch(AN_READ_API + '?ref=main', { headers: h, cache: 'no-store' }).then(function (r) {
      if (r.status === 404) return null; if (!r.ok) throw new Error('GitHub ' + r.status); return r.json();
    }).then(function (cur) {
      var remote = { read: {} };
      if (cur && cur.content) { try { remote = JSON.parse(new TextDecoder().decode(b64(cur.content.replace(/\s/g, '')))) || remote; } catch (e) {} }
      remote.read = remote.read || {};
      var changed = false;
      Object.keys(mine).forEach(function (k) { if (/^\d{4}-W\d{2}$/.test(k) && typeof mine[k] === 'string' && !(remote.read[k] >= mine[k])) { remote.read[k] = mine[k]; changed = true; } });
      if (!changed) return 'same';
      return fetch(AN_READ_API, { method: 'PUT', headers: Object.assign({ 'Content-Type': 'application/json' }, h),
        body: JSON.stringify({ message: 'chore: 手機讀過分析師週報', branch: 'main', sha: cur ? cur.sha : undefined,
          content: b64s(JSON.stringify(remote, null, 1)) }) }).then(function (r) {
        if (r.ok) return 'ok';
        throw new Error(r.status === 401 ? '金鑰無效或過期，重新解鎖看看' : r.status === 409 ? '剛好有別的裝置在寫，等一下再試' : 'GitHub ' + r.status);
      });
    }).then(function (how) { AN.syncMsg = how === 'same' ? '讀過的紀錄已經同步到電腦面板' : '已同步到電腦面板（最慢 3 分鐘看到）✓'; })
      .catch(function (e) { AN.syncMsg = '同步失敗：' + (e && e.message || '連不到 GitHub') + '（下次開週報會再試）'; })
      .then(function () { AN.syncing = false; anPaintSync(); });
  }
  // ══ 【斷線通知】（2026-09-24）══════════════════════════════════════
  // 面板超過 10 分鐘沒回報 ⇒ GitHub Actions 的看門狗推通知到這支手機（.github/watchdog/watchdog.py）。
  // 這裡做兩件事：① 問他允許通知並訂閱 ② 用鑰匙圈金鑰把「訂閱」存進 repo 的 data/push-subs.json（看門狗從那裡讀）。
  // ⚠️ iPhone：一定要從主畫面打開的 App 才有推播（iOS 16.4 以上）；允許通知一定要在按鈕的點擊裡問。
  var VAPID_PUBLIC = 'BKQoOkXK8Wpe3mSHL6ZkqTzMm6kKv1cpkCWs9wG1jT9cy7PhVVnpilv2SIM_zEOXl5w5CXw-TNH76JW__iPIqIM';
  var PUSH_API = 'https://api.github.com/repos/xd1104/trade-log/contents/data/push-subs.json';
  var PUSH_LS = 'tlmon.push.v1';
  var PUSH = { busy: false, msg: '' };
  function pushSupported() { return 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window; }
  function pushOn() { try { return localStorage.getItem(PUSH_LS) === 'on'; } catch (e) { return false; } }
  function pushPaint() {
    var c = $('pushCard'); if (!c) return;
    if (pushSupported() && pushOn() && Notification.permission === 'granted') { c.hidden = true; return; }
    c.hidden = false;
    if (!pushSupported()) {
      c.innerHTML = '<h2>斷線通知</h2><div class="msg">要從主畫面的圖示打開才收得到（Safari「分享 → 加入主畫面」）。</div>';
      return;
    }
    c.innerHTML = '<h2>斷線通知</h2><div class="msg">面板斷線時手機會響</div>' +
      '<button class="pushbtn" id="pushBtn"' + (PUSH.busy ? ' disabled' : '') + '>' + (PUSH.busy ? '設定中…' : '開啟斷線通知') + '</button>' +
      (PUSH.msg ? '<div class="err">' + esc(PUSH.msg) + '</div>' : '');
  }
  function urlB64(s) { s = s.replace(/-/g, '+').replace(/_/g, '/'); while (s.length % 4) s += '='; return b64(s); }
  function saveSub(sub) {
    var h = { 'Accept': 'application/vnd.github+json', 'Authorization': 'Bearer ' + ghToken() };
    return fetch(PUSH_API + '?ref=main', { headers: h, cache: 'no-store' }).then(function (r) {
      if (r.status === 404) return null; if (!r.ok) throw new Error('GitHub ' + r.status); return r.json();
    }).then(function (cur) {
      var d = { subs: [] };
      if (cur && cur.content) { try { d = JSON.parse(new TextDecoder().decode(b64(cur.content.replace(/\s/g, '')))) || d; } catch (e) {} }
      d.subs = (d.subs || []).filter(function (x) { return x && x.endpoint !== sub.endpoint; });
      d.subs.push({ endpoint: sub.endpoint, keys: sub.keys, at: localIso() });
      return fetch(PUSH_API, { method: 'PUT', headers: Object.assign({ 'Content-Type': 'application/json' }, h),
        body: JSON.stringify({ message: 'chore: 手機開啟斷線通知', branch: 'main', sha: cur ? cur.sha : undefined,
          content: b64s(JSON.stringify(d, null, 1)) }) }).then(function (r) {
        if (!r.ok) throw new Error(r.status === 401 ? '金鑰無效或過期，重新解鎖鑰匙圈看看' : 'GitHub ' + r.status);
      });
    });
  }
  function enablePush() {
    if (PUSH.busy) return;
    if (!ghToken()) {
      if (window.Keyring) { PUSH.msg = '先解鎖鑰匙圈（存通知設定要用），解完再按一次「開啟斷線通知」'; pushPaint(); Keyring.open('開啟斷線通知'); }
      else { PUSH.msg = '沒有鑰匙圈，存不了通知設定'; pushPaint(); }
      return;
    }
    PUSH.busy = true; PUSH.msg = ''; pushPaint();
    // ⚠️ requestPermission 一定要在點擊當下叫（iOS 規定），所以放在最前面
    Notification.requestPermission().then(function (p) {
      if (p !== 'granted') throw new Error('沒有允許通知（可以到 iPhone「設定 → 通知 → 儀表板監控」打開）');
      return navigator.serviceWorker.ready;
    }).then(function (reg) {
      return reg.pushManager.getSubscription().then(function (s) {
        return s || reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlB64(VAPID_PUBLIC) });
      });
    }).then(function (sub) { return saveSub(sub.toJSON()); })
      .then(function () { try { localStorage.setItem(PUSH_LS, 'on'); } catch (e) {} PUSH.msg = ''; })
      .catch(function (e) { PUSH.msg = '開不起來：' + (e && e.message || e); })
      .then(function () { PUSH.busy = false; pushPaint(); });
  }

  document.addEventListener('click', function (e) {
    var t = e.target;
    if (t.closest('#pushBtn')) { enablePush(); return; }
    if (t.closest('#anMail')) { anShow('list'); return; }
    if (t.closest('[data-antop]')) { window.scrollTo({ top: 0, behavior: 'smooth' }); return; }
    var go = t.closest('[data-an]');
    if (go) { var v = go.getAttribute('data-an');
      if (v === 'home') anShow(null); else if (v === 'list') anShow('list');
      else if (v === 'unlock' && window.Keyring) Keyring.open('把讀過的週報同步到電腦面板');
      return; }
    var it = t.closest('[data-anid]');
    if (it) { AN.cur = it.getAttribute('data-anid'); anShow('report'); return; }
    var j = t.closest('[data-anjump]');
    if (j) { var bs = document.querySelectorAll('.an-seg button'); for (var i = 0; i < bs.length; i++) bs[i].className = bs[i] === j ? 'on' : '';
      var sec = $('an-s-' + j.getAttribute('data-anjump')); if (sec) sec.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
  });

  // ══ ⭐ 2026-10-19 減字版畫面（照 lab-ux phone-demo；老闆試玩後同意）══════════════════════
  // 目標：打開 3 秒看懂「面板活著沒、有沒有部位、今天／今晚做了沒、賺賠、本月」；要他動手的事才跳出「要你處理」。
  // ⛔ 句子與等級一律後端組好（monitor_push.py：s.todo、days[i].short、recent[i].short、lots.short、cushion.short、position.short）；
  //    手機只自己算一件事：面板幾分鐘沒回報（只有手機知道）。
  // ⛔ 賺賠只看「點」（老闆 10-19 拍板，⛔ 不換算成元；只有帳戶的權益／今天損益本來就是元）。
  // ⛔ 持有中不顯示停損價／停利價／進場價（老闆 10-19 拍板：不是收起來，是不放）。
  // ⚠️ 舊快照（面板還沒重啟、沒有新欄位）要退回合理畫面：短句沒有就只寫結果、todo 沒有就用舊的「要注意的事」湊（legacyTodo）。
  var LV = { now: '現在', decide: '要你決定', later: '有空再看' };
  var WK = '日一二三四五六';
  var EXIT_WORD = { sl: '停損出場', tp: '停利出場', eod: '收盤平倉', night_eod: '04:58 平倉', manual: '手動平倉',
    closed_elsewhere: '已經平掉', legs: '分段出場' };
  var DETAILS_OPEN = {};    // 他正在展開的細節（重畫前記下、畫完補回；⛔ 不存 localStorage：每次打開 App 都是收起）
  function md(iso) { var p = String(iso || '').split('-'); if (p.length < 3) return esc(iso);
    var d = new Date(+p[0], +p[1] - 1, +p[2]); return (d.getMonth() + 1) + '/' + d.getDate() + ' ' + WK[d.getDay()]; }
  function wkday(iso) { var p = String(iso || '').split('-'); return p.length < 3 ? null : new Date(+p[0], +p[1] - 1, +p[2]).getDay(); }
  function minOf(age) { return Math.floor(age / 60); }
  function qtyOf(x) { return (typeof x === 'number' && x > 0) ? x : 1; }
  function totalOf(R) { return R.total != null ? R.total : (R.points == null ? null : R.points * qtyOf(R.qty)); }
  function qtyTxt(q) { return q > 1 ? '・' + q + ' 口' : ''; }
  function hhmm(t) { return String(t || '').slice(0, 5); }

  // ⭐ QA r1 建議 9：舊快照（面板還沒重啟）的長句 ⇒ 手機端先把已知術語換成白話、拿掉 ⚠️⛔，過渡期不嚇到他
  var PLAIN = [[/[⚠⛔]️?/g, ''], [/停損失靈/g, '賠得比停損多'], [/風控上限/g, '每月上限'], [/風控/g, '每月上限'],
    [/對帳/g, '核對'], [/滑價/g, '成交價差'], [/\s*——\s*/g, '，'], [/^\s+|\s+$/g, '']];
  function plain(t) { t = String(t || ''); PLAIN.forEach(function (x) { t = t.replace(x[0], x[1]); }); return t; }

  // 舊快照沒有 s.todo ⇒ 用舊的那幾個欄位湊（句子是舊的長句、沒有「→ 要做什麼」；術語先換白話）
  function legacyTodo(s) {
    var T = [], e = s.equity || {}, rk = s.risk || {}, r = s.real || {}, pn = s.panel || {}, d = s.day || {};
    function add(l, t) { if (t) T.push({ lvl: l, text: plain(t), act: '' }); }
    if ((s.errs || []).length) add('now', '面板有一部分讀不到');
    if (r.position && r.stale_sec != null) add('now', '報價斷了 ' + r.stale_sec + ' 秒，手上的單現在沒人顧');
    var t0 = (d.days || [])[0];
    if (t0 && t0.date === d.today && t0.eod && t0.eod.alarm) add('now', t0.eod.why_msg || t0.eod.msg || '收盤平倉有狀況');
    if (e.cushion && e.cushion.warn) add('now', e.cushion.msg);
    if (rk.err) add('now', '算不出本月賺賠，自動單先不送');
    else if (rk.blocked) add('decide', rk.msg || '本月虧到上限，日盤、夜盤先停到月底');
    if (pn.conn && pn.conn.ok === false) add('now', '面板連不上券商');
    if (s.lots && s.lots.warn) add('decide', s.lots.warn);
    if (e.backup && e.backup.warn) add('later', e.backup.msg);
    if (e.depth && e.depth.warn) add('later', e.depth.msg);
    return T;
  }

  function skipTxt(sh) { return sh === '本月停單中' ? { v: '停單中', n: 1, m: '本月到上限' } : { v: '沒做', n: 1, m: sh || '' }; }
  function posKnown(s) { return !!(s.real && ('position' in s.real)); }
  function floatTotal(r, p) { return r.float_total != null ? r.float_total : (r.float_pts == null ? null : r.float_pts * qtyOf(p.qty)); }
  // ⭐ QA r1 建議 7：持有中 ⇒ 右邊寫「持有中 ±N」（⛔ 不要跟已出場的點數長得一樣）
  function holdRow(r, p) { var ft = floatTotal(r, p); return { v: '持有中 ' + pm(ft), c: cls(ft), m: qtyOf(p.qty) > 1 ? qtyOf(p.qty) + ' 口' : '', hold: 1 }; }
  // ⭐ QA r1 建議 7：00:00～05:00 以交易日為準 ⇒ 「今天」＝今晚那一晚開盤的那天（夜盤 E 比日曆的今天早）
  // ⭐ QA r2：這一口部位是不是那一筆自動單（方向＋進場時刻到分，跟後端收盤平倉同一組認法）
  function matchPos(p, tr) { return !!(p && tr && p.dir && p.dir === tr.dir && hhmm(p.entry_time) && hhmm(p.entry_time) === hhmm(tr.entry_time)); }
  // ⛔ QA r2 原則：部位還在（或不知道）的時候，任何一列都不准寫「已出場」
  function notExited(s, m) {
    var p = (s.real || {}).position;
    if (!posKnown(s)) return { v: '不知道', n: 1, m: '部位還讀不到', gold: 1 };
    if (p) return { v: '還在算', n: 1, m: m || '點數還在算' };
    return null;
  }
  function tradeDay(s) {
    var d = s.day || {}, tE = ((s.night || {}).tonight || {}).E;
    return (tE && d.today && tE < d.today) ? tE : d.today;
  }

  // D 今天：日盤那一列（D＝交易日）
  function dayRow(s, D) {
    var d = s.day || {}, r = s.real || {}, p = r.position, isToday = D === d.today;
    var t = (d.days || []).filter(function (x) { return x.date === D; })[0];
    if (!s.day) return { v: '讀不到', n: 1, m: '' };
    if ((d.closed || []).indexOf(D) >= 0 || wkday(D) === 0 || wkday(D) === 6) return { v: '休市', n: 1, m: '' };
    if (t && t.trade) {
      var tr = t.trade, R = t.real || {}, q = qtyOf(R.qty || tr.qty);
      if (!tr.ok) return { v: '沒送成', n: 1, m: '', gold: 1 };
      if (R.state === 'ok') { var tot = totalOf(R), xw = (EXIT_WORD[R.why] || '出場') + ' ' + hhmm(R.exit_time);
        return tot == null ? (notExited(s, xw) || { v: '已出場', n: 1, m: xw })
          : { v: pm(tot) + ' 點', c: cls(tot), m: xw + qtyTxt(q) }; }
      if (R.state === 'drill') return { v: '演練', n: 1, m: '不是真單' };
      // ⭐ QA r2 必修 1：那一口還在（13:43 沒平掉、過了午夜也一樣）⇒ 持有中（⛔ 不看是不是今天）
      if (matchPos(p, tr) || (isToday && p && !p.entry_time && (p.sess ? p.sess === 'day' : true))) return holdRow(r, p);
      // ⭐ QA r1 必修 1：部位讀不到 ⇒ 講不知道（⛔ 不寫已出場：可能根本還沒平掉）
      return notExited(s) || { v: '已出場', n: 1, m: '點數還在算' };
    }
    if (!d.armed) return { v: '關著', n: 1, m: '' };
    if ((s.risk || {}).blocked) return { v: '停單中', n: 1, m: '本月到上限' };
    if (!t) return isToday ? { v: hhmm(d.signal_at || '09:03') + ' 看', n: 1, m: '還沒開始' } : { v: '—', n: 1, m: '' };
    if (t.wait) return { v: '還在看', n: 1, m: t.short || '' };
    return skipTxt(t.short);
  }
  // D 今天：某一晚夜盤那一列（E＝開盤那晚的日期）
  function nightRow(s, E) {
    var n = s.night || {}, R = (n.real || {})[E], r = s.real || {}, p = r.position, tE = (n.tonight || {}).E;
    if (!s.night) return { v: '讀不到', n: 1, m: '' };
    if ((n.closed || []).indexOf(E) >= 0) return { v: '休市', n: 1, m: '' };
    var rows = (n.recent || []).filter(function (x) { return x.E === E; });
    var res = rows.filter(function (x) { return x.rec === 'result'; })[0];
    var sk = rows.filter(function (x) { return x.rec === 'skip'; })[0];
    if (res && res.ok) {
      if (R && R.state === 'ok') { var tot = totalOf(R), w = R.why === 'sl' || R.why === 'tp' || R.why === 'manual' ? EXIT_WORD[R.why] + ' ' + hhmm(R.exit_time) : '04:58 平倉';
        return tot == null ? (notExited(s, w) || { v: '已出場', n: 1, m: w }) : { v: pm(tot) + ' 點', c: cls(tot), m: w + qtyTxt(qtyOf(R.qty)) }; }
      if (R && R.state === 'drill') return { v: '演練', n: 1, m: '不是真單' };
      // ⭐ QA r2 必修 2：04:58 沒平掉、過了 05:00 那一晚變成「上一晚」也一樣 ⇒ 持有中
      if (matchPos(p, res) || (p && E === tE && !p.entry_time && (p.sess ? p.sess === 'night' : true))) return holdRow(r, p);
      return notExited(s) || { v: '已出場', n: 1, m: '點數還在算' };
    }
    if (res && !res.ok) return { v: '沒送成', n: 1, m: '', gold: 1 };
    if (sk) return skipTxt(sk.short);
    if (E === tE && (s.risk || {}).blocked) return { v: '停單中', n: 1, m: '本月到上限' };
    if (E === tE) return n.on ? { v: (n.tonight.look_at || '') + ' 看', n: 1, m: '還沒開始' } : { v: '關著', n: 1, m: '' };
    return { v: '—', n: 1, m: '' };
  }
  function prevNightE(s, D) {
    var n = s.night || {}, best = null;
    (n.recent || []).forEach(function (x) { var E = String(x.E || ''); if (E && (!D || E < D) && (!best || E > best)) best = E; });
    return best;
  }
  function tlRow(k, sub, r) {
    return '<div class="ln"><div class="k"><b>' + k + '</b><span>' + esc(sub) + '</span></div><div class="m">' + esc(r.m) + '</div>' +
      '<div class="v num' + (r.n ? ' n' : '') + (r.c ? ' ' + r.c : '') + (r.gold ? ' gold' : '') + '">' + esc(r.v) + '</div></div>';
  }

  function statHTML(age, s) {
    var dead = age > DEAD_S, slow = age > SLOW_S;
    if (fetchErr) return { cls: 'slow', t: '手機連不到網路，顯示的是 ' + Math.max(1, minOf(age)) + ' 分鐘前的資料' };
    if (dead) return { cls: 'dead', t: '面板 ' + minOf(age) + ' 分鐘沒回報' };
    if (slow) return { cls: 'slow', t: minOf(age) + ' 分鐘沒回報，再等等' };
    if ((s.errs || []).length) return { cls: 'warn', t: '面板有一部分讀不到' };
    return { cls: 'ok', t: '面板正常・' + ago(age) };
  }

  // ══ ⭐ 2026-10-21 三頁（首頁／金流明細／系統）＋「今天」＝程式下單卡（Benson 看過 demo 定案）══════════
  //   分頁只活在記憶體（⛔ 不記：每次打開都是首頁）。看週報時整個 #main 收起來，分頁列跟著收。
  var PAGE = 'home', CASH_M = null;
  function pagePaint() {
    ['home', 'cash', 'sys'].forEach(function (k) { var el = $('pg-' + k); if (el) el.hidden = PAGE !== k; });
    var bs = document.querySelectorAll('#tabbar [data-pg]');
    for (var i = 0; i < bs.length; i++) bs[i].className = bs[i].getAttribute('data-pg') === PAGE ? 'on' : '';
  }
  // 一盤（日盤 d／夜盤 n）。資料 monitor_push._prog_day／_prog_night。
  function progSess(cls0, P) {
    var icon = cls0 === 'd' ? '☀' : '☾', title = cls0 === 'd' ? '日盤' : '夜盤';
    P = P || {};
    var head = '<div class="ps"><div class="ps-h"><span>' + icon + '</span><span class="nm">' + title + (P.name ? '・' + esc(P.name) : '') + '</span>' +
      (P.qty != null ? '<span class="lots"><b class="num">' + esc(String(P.qty)) + '</b> 口</span>' : '') + '</div>';
    if (P.err) return head + '<div class="ps-st"><span class="sub">' + esc(P.err) + '</span></div></div>';
    var S = P.state || {}, st = '';
    if (S.kind === 'pts') st = '<div class="ps-st"><span class="big num ' + cls(S.pts) + '">' + pm(S.pts) + ' 點</span></div>' +
      (S.line ? '<div class="ps-st"><span class="sub num">' + esc(S.line) + '</span></div>' : '');
    else if (S.kind === 'hold') st = '<div class="ps-st">' + (S.pts != null ? '<span class="big num ' + cls(S.pts) + '">' + pm(S.pts) + ' 點</span>' : '<span class="big w">持有中</span>') +
      '<span class="sub">持有中</span></div>' + (S.line ? '<div class="ps-st"><span class="sub num">' + esc(S.line) + '</span></div>' : '');
    else if (S.kind === 'wait') st = '<div class="ps-st"><span class="big w">' + esc(S.label || '') + '</span>' + (leftTo(S.at, S.night) ? '<span class="sub">' + leftTo(S.at, S.night) + '</span>' : '') + '</div>';
    else st = '<div class="ps-st"><span class="big w">' + esc(S.label || '—') + '</span>' + (S.sub ? '<span class="sub">' + esc(S.sub) + '</span>' : '') + '</div>';
    var rows = (P.rows || []).map(function (r) {
      var mid = (r.need > 0 && r.now != null)
        ? '<div class="pb' + (r.ok ? ' ok' : '') + '"><i style="width:' + Math.min(100, r.now / (r.need * 1.7) * 100).toFixed(1) + '%"></i><u style="left:' + (100 / 1.7).toFixed(1) + '%"></u></div>'
        : '<span class="t">' + esc(r.text || '') + '</span>';
      var v = r.v === 'ok' ? '<span class="v ok">✔</span>' : r.v === 'no' ? '<span class="v no">✕</span>' : r.v ? '<span class="v ok q">' + esc(r.v) + '</span>' : '<span></span>';
      return '<div class="pi' + (r.fired ? ' fired' : '') + '"><span class="k">' + esc(r.k) + '</span>' + mid + v +
        (r.nums ? '<span></span><span class="nums num">' + esc(r.nums) + '</span><span></span>' : '') + '</div>';
    }).join('');
    return head + st + rows + '</div>';
  }
  // 「還有幾分」：手機自己的時鐘（⚠️ 夜盤 21:40 在凌晨 05:00 前不倒數，那是明天的事）
  function leftTo(at, night) {
    var mm = /^(\d\d):(\d\d)(?::(\d\d))?/.exec(String(at || '')); if (!mm) return '';
    var d = new Date(), now = d.getHours() * 3600 + d.getMinutes() * 60 + d.getSeconds();
    if (night && now < 5 * 3600) return '';
    var x = (+mm[1]) * 3600 + (+mm[2]) * 60 + (+(mm[3] || 0)) - now; if (x <= 0) return '';
    var h = Math.floor(x / 3600), mi = Math.floor(x % 3600 / 60), se = x % 60;
    return '還有 ' + (h ? h + ' 小時 ' + mi + ' 分' : mi ? mi + ' 分 ' + se + ' 秒' : se + ' 秒');
  }
  // 第二頁：金流明細（資料＝電腦帳戶頁同一份，後端算好 ⇒ s.cash）。⛔ 手機不自己算錢。
  var WHY_WORD = { tp: '停利', sl: '停損', eod: '收盤平', night_eod: '04:58 平' };
  function money(x) { return x == null || isNaN(x) ? '—' : (x > 0 ? '+' : x < 0 ? '−' : '') + Math.abs(Math.round(x)).toLocaleString(); }
  // ⭐ 2026-10-22 B 版「每日卡片」（跟電腦帳戶頁同一套樣式）：本月一個主角大數字＋三個組成；每天一張小卡、右上當天賺賠
  function cashHTML(C) {
    var ms = (C && C.months) || [];
    if (!ms.length) return '<h1 class="pgt">金流明細</h1><section class="card"><div class="msg">還沒有資料（電腦面板更新後就會有）。</div></section>';
    var keys = ms.map(function (x) { return x.key; }); if (keys.indexOf(CASH_M) < 0) CASH_M = keys[keys.length - 1];
    var i = keys.indexOf(CASH_M), M = ms[i], DS = M.days || [];
    var bl = (M.start && DS.length) ? '月初 ' + Math.round(M.start[1]).toLocaleString() + ' → ' + (i === keys.length - 1 ? '現在 ' : '月底 ') + Math.round(DS[0].bal).toLocaleString() : '';
    var h = '<h1 class="pgt">金流明細</h1><div class="msw">' +
      (i > 0 ? '<button data-cm="' + esc(keys[i - 1]) + '">‹</button>' : '<button disabled>‹</button>') +
      '<b>' + esc(CASH_M.slice(0, 4)) + ' 年 ' + (+CASH_M.slice(5)) + ' 月</b>' +
      (i < keys.length - 1 ? '<button data-cm="' + esc(keys[i + 1]) + '">›</button>' : '<button disabled>›</button>') + '</div>' +
      '<div class="chero' + (M.net < 0 ? ' neg' : '') + '"><div class="k">本月實際賺賠</div><div class="big num ' + cls(M.net) + '">' + money(M.net) + '<small>元</small></div>' +
      (bl ? '<div class="bl num">' + bl + '</div>' : '') +
      '<div class="parts"><div><i class="p1"></i>入金<b class="num">' + money(M.dep) + '</b></div>' +
      '<div><i class="p2"></i>交易損益<b class="num ' + cls(M.pl) + '">' + money(M.pl) + '</b></div>' +
      '<div><i class="p3"></i>手續費＋稅<b class="num dim">' + money(M.fee) + '</b></div></div></div>';
    h += DS.map(function (d) {
      var tr = (d.items || []).filter(function (x) { return x.k === 'trade'; });
      var fee = (d.items || []).filter(function (x) { return x.k === 'fee'; }).reduce(function (a, x) { return a + (+x.amt || 0); }, 0);
      var net = tr.reduce(function (a, x) { return a + (+x.amt || 0); }, 0) + fee;
      var rows = (d.items || []).map(function (x) {
        if (x.k === 'dep') return '<div class="crow"><span class="ico dep">＋</span><span class="nm"><b>入金</b>' + (x.inferred ? '<small>券商沒寫，照權益推算</small>' : '') + '</span><span class="amt num gold">' + money(x.amt) + '</span></div>';
        if (x.k !== 'trade') return '';
        var nt = x.sess === '夜盤';
        return '<details class="ctr"><summary class="crow"><span class="ico ' + (nt ? 'n' : 'd') + '">' + (nt ? '☾' : '☀') + '</span>' +
          '<span class="nm"><b>' + esc((x.name || '') + ' ' + (x.dir || '')) + '</b><small>' + money(x.pts) + ' 點 × ' + esc(String(x.qty || 1)) + ' 口' + (x.night_of ? '・' + esc(x.night_of) + ' 晚' : '') + '</small></span>' +
          '<span class="amt num ' + cls(x.amt) + '">' + money(x.amt) + '</span></summary>' +
          '<div class="cdet">進場 <b class="num">' + esc(String(x.entry == null ? '—' : Math.round(x.entry))) + '</b>　' + esc(x.entry_time || '') +
          '<br>出場 <b class="num">' + esc(String(x.exit == null ? '—' : Math.round(x.exit))) + '</b>　' + esc(x.exit_time || '') + '　' + esc(x.why_txt || WHY_WORD[x.why] || x.why || '') + '</div></details>';
      }).join('');
      return '<div class="cdc"><div class="cdh2"><b>' + esc(d.d) + '</b><span>週' + esc(d.w || '') + '</span>' +
        (d.diff ? '<em class="gold">⚠ 差 ' + money(d.diff) + '</em>' : '') +
        (tr.length ? '<span class="dn num ' + cls(net) + '">' + money(net) + '</span>' : '<span class="dn none">沒有交易</span>') + '</div>' + rows +
        (tr.length || fee ? '<div class="cfee"><span></span><span>手續費＋稅</span><span class="num">' + money(fee) + '</span></div>' : '') +
        '<div class="cbal num">結餘 ' + (d.bal == null ? '—' : Math.round(d.bal).toLocaleString()) + '</div></div>';
    }).join('');
    return h;
  }
  // 第三頁：系統（原本首頁「系統」那格＋跟電腦系統分頁一樣的四件事）
  function sysHTML(s, age, dead) {
    var r = s.real || {}, P = s.panel || {}, c = P.conn || {}, e = s.equity || {}, u = e.usage || {}, dp = e.depth || {}, bk = e.backup || {};
    var ok = !dead && !(s.errs || []).length;
    var h = '<h1 class="pgt">系統</h1>' +
      '<section class="card"><div class="lbl">面板</div><div class="sbig"><span class="sdot' + (ok ? '' : ' bad') + '"></span>' + (dead ? '沒回報' : (s.errs || []).length ? '有一部分讀不到' : '正常') + '</div>' +
      '<div class="rw"><span class="k">面板最後回報</span><span class="v">' + ago(age) + '（' + esc(String(s.at || '').slice(11, 16)) + '）</span></div>' +
      '<div class="rw"><span class="k">電腦</span><span class="v">' + esc(s.host || '—') + (r.started ? '・' + esc(String(r.started).replace('-', '/')) + ' 開' : '') + '</span></div></section>' +
      '<section class="card"><div class="lbl">券商連線</div>' +
      '<div class="rw"><span class="k">合約</span><span class="v">' + esc(c.contract_name || '—') + '</span></div>' +
      '<div class="rw"><span class="k">報價</span><span class="v' + (P.quote === 'nodata' ? ' gold' : '') + '">' + (({ live: '正常', closed: '休市中', nodata: '盤中收不到' })[P.quote] || esc(P.quote || '—')) + '</span></div>' +
      '<div class="rw"><span class="k">今天重連</span><span class="v">' + (c.retries == null ? '—' : c.retries + ' 次') + '</span></div></section>';
    if (u.mb != null) h += '<section class="card"><div class="lbl">券商流量</div><div class="sbig num">' + Math.round(u.mb) + ' MB <small>／ ' + esc(String(u.limit_mb || '—')) + ' MB</small></div>' +
      '<div class="ubar"><i style="width:' + Math.min(100, +u.pct || 0) + '%"></i></div></section>';
    h += '<section class="card"><div class="lbl">五檔錄製・資料備份</div>' +
      '<div class="rw"><span class="k">五檔錄製</span><span class="v' + (dp.warn ? ' gold' : '') + '">' + (dp.msg ? (dp.warn ? '有問題' : '正常') : '—') + '</span></div>' +
      '<div class="rw"><span class="k">資料備份</span><span class="v' + (bk.warn ? ' gold' : '') + '">' + esc(String(bk.msg || '—').replace(/^資料備份：/, '')) + '</span></div></section>';
    return h;
  }
  document.addEventListener('click', function (ev) {
    var t = ev.target;
    var pg = t.closest('#tabbar [data-pg]');
    if (pg) { PAGE = pg.getAttribute('data-pg'); pagePaint(); window.scrollTo(0, 0); return; }
    var cm = t.closest('[data-cm]');
    if (cm) { CASH_M = cm.getAttribute('data-cm'); if (lastBox && keys) open(lastBox, keys).then(function (s) { safeRender(lastBox, s); }); }
  });

  function render(box, s) {
    var age = Date.now() / 1000 - box.t;
    var dead = age > DEAD_S;
    var st = statHTML(age, s);
    $('stat').className = 'stat ' + st.cls;
    $('stat').innerHTML = '<span class="dot"></span><span>' + esc(st.t) + '</span>';

    // ⭐ 分析師信件：列表跟著快照更新（打開中的全文不重畫，免得他看到一半跳掉）
    AN.snap = s.analyst || null;
    anPaintBtn();
    if (AN.view === 'list') anPaintList();

    var h = '';
    // B 要你處理（沒事就整張不出現；排序 now → decide → later，later 一定最後）
    var todo = Array.isArray(s.todo) ? s.todo.slice() : legacyTodo(s);
    var ORD = { now: 0, decide: 1, later: 2 };
    todo = todo.filter(function (x) { return x && x.text; }).map(function (x, i) { return { x: x, i: i }; })
      .sort(function (a, b) { return ((ORD[a.x.lvl] == null ? 1 : ORD[a.x.lvl]) - (ORD[b.x.lvl] == null ? 1 : ORD[b.x.lvl])) || (a.i - b.i); })
      .map(function (o) { return o.x; });
    if (dead && !fetchErr) todo.unshift({ lvl: 'now', text: '面板 ' + minOf(age) + ' 分鐘沒回報', act: '看一下電腦是不是當機、睡著或斷網' });
    if (todo.length) {
      h += '<section class="card todo"><div class="lbl">要你處理（' + todo.length + '）</div>' + todo.map(function (x) {
        var lv = LV[x.lvl] ? x.lvl : 'decide';
        return '<div class="it"><div class="t"><span class="lv ' + lv + '">' + LV[lv] + '</span>' + esc(x.text) + '</div>' +
          (x.act ? '<div class="a"><b>→</b><span>' + esc(x.act) + '</span></div>' : '') + '</div>';
      }).join('') + '</section>';
    }

    // C 現在（主角）
    var r = s.real || {}, p = r.position, rk = s.risk || {}, d = s.day || {}, n = s.night || {};
    var D = tradeDay(s);
    var hero = '<section class="card hero"><div class="row1"><span class="lbl">現在</span>';
    if (p) {
      var night = p.sess ? p.sess === 'night' : (String(p.entry_time || '') >= '15:00' || String(p.entry_time || '') < '05:00');
      var ft = r.float_pts;   // ⭐ 2026-10-21：點數一律每口（Benson 拍板；錢看帳戶）
      var dw = dirTxt(p.dir);
      // ⭐ QA r1 建議 10：方向空白 ⇒「1 口（方向不明）」
      var sub = p.short || ((dw ? dw + ' ' + qtyOf(p.qty) + ' 口' : qtyOf(p.qty) + ' 口（方向不明）') + (p.entry_time ? '・' + hhmm(p.entry_time) + ' 進場' : ''));
      hero += '<span class="chip hold">' + (night ? '夜盤' : '日盤') + '持有中</span></div>' +
        '<div class="big num ' + cls(ft) + '">' + pm(ft) + '<small>點</small></div>' +
        '<div class="sub">' + esc(sub) + (dead ? '（' + minOf(age) + ' 分鐘前的資料）' : '') + '</div>';
    } else if (!posKnown(s)) {
      // ⛔ 面板沒回應、或剛啟動還沒問到券商 ≠ 沒有部位：不知道就講不知道
      hero += '</div><div class="big unk">還不知道</div><div class="sub">' + (s.real ? '面板剛開，下一次回報就會有' : '這一次面板沒回應，讀不到部位') + '</div>';
    } else {
      var next = '', t0 = (d.days || []).filter(function (x) { return x.date === d.today; })[0], wd = wkday(d.today);
      var dayOpen = d.armed && wd >= 1 && wd <= 5 && (d.closed || []).indexOf(d.today) < 0 && D === d.today;
      var tonightE = (n.tonight || {}).E;
      if (rk.blocked) next = '本月停單中，不會下單';
      // ⭐ QA r1 建議 6：09:30 前還在等訊號 ⇒ 講日盤還在等（⛔ 不跳去講夜盤）
      else if (dayOpen && t0 && t0.wait) next = '日盤還在等訊號（' + esc(t0.wait_until || '09:30') + ' 前）';
      else if (dayOpen && !t0) next = '下一次：' + hhmm(d.signal_at || '09:03') + ' 日盤';
      // ⭐ QA r1 必修 5：今晚休市 ⇒ ⛔ 不寫「下一次：今晚夜盤」
      else if (n.on && tonightE && tonightE === d.today && (n.closed || []).indexOf(tonightE) < 0 &&
               !(n.recent || []).some(function (x) { return x.E === tonightE; })) next = '下一次：今晚 ' + esc(n.tonight.look_at) + ' 夜盤';
      hero += '</div><div class="big flat">沒有部位</div>' + (dead ? '<div class="sub">這是 ' + minOf(age) + ' 分鐘前的資料</div>' : next ? '<div class="sub">' + next + '</div>' : '');
    }
    h += hero + '</section>';

    // D 今天 ⭐ 2026-10-21 改成電腦即時頁「程式下單」那張卡（Benson 看過 demo 定案）：日盤三候選＋夜盤走勢／波動。
    //   資料全部後端組好（monitor_push._prog ⇒ s.prog），手機只畫；點數一律每口。
    //   舊快照（面板還沒重啟、沒有 s.prog）⇒ 退回舊的三列（上一晚 → 今天日盤 → 今晚）。
    if (s.prog) h += '<section class="card"><div class="lbl">今天</div>' + progSess('d', s.prog.day) + progSess('n', s.prog.night) + '</section>';
    else {
      var pE = prevNightE(s, D), tE = (n.tonight || {}).E;
      h += '<section class="card"><div class="lbl">今天</div><div class="tl">' +
        (pE ? tlRow('夜盤', md(pE) + ' 晚', nightRow(s, pE)) : '') +
        tlRow('日盤', '今天', dayRow(s, D)) +
        (tE ? tlRow('夜盤', '今晚', nightRow(s, tE)) : '') + '</div></section>';
    }
    $('board').innerHTML = h;

    // 帳戶（收起來；⚠️ 重畫前記下他展開的、畫完補回）
    // ⭐ 2026-10-21：拿掉「本月」「最近幾天」「自動下單」三塊；「系統」搬到第三頁；保證金那一行只有不夠的時候才出現。
    var ds = $('more').querySelectorAll('details[id]');
    for (var i = 0; i < ds.length; i++) DETAILS_OPEN[ds[i].id] = ds[i].open;
    var e = s.equity || {}, L = s.lots || {};
    var lab = String(L.label || '').replace('（起始級）', '');
    var m = '<details id="dAcct"><summary>帳戶<span class="r num">' + (e.equity != null ? Math.round(e.equity).toLocaleString() + ' 元' : '—') + '</span></summary><div class="db">' +
      '<div class="rw"><span class="k">今天損益</span><span class="v num ' + cls(e.day_pl) + '">' + (e.day_pl == null ? '—' : e.day_pl ? pm(e.day_pl) + ' 元' : '0 元') + '</span></div>' +
      (lab ? '<div class="rw"><span class="k">口數</span><span class="v">' + esc(lab) + (L.day || L.night ? '（日盤 ' + esc(L.day || '—') + '・夜盤 ' + esc(L.night || '—') + '）' : '') + '</span></div>' : '') +
      (L.short ? '<div class="rw"><span class="k">升級</span><span class="v">' + esc(L.short) + '</span></div>' : '') +
      (e.cushion && e.cushion.warn && e.cushion.short ? '<div class="rw"><span class="k">保證金</span><span class="v gold">' + esc(e.cushion.short) + '</span></div>' : '') +
      '</div></details>';
    $('more').innerHTML = m;
    for (var id in DETAILS_OPEN) { if (DETAILS_OPEN[id] && $(id)) $(id).open = true; }

    // 第二頁：金流明細　第三頁：系統（各自包 try：一頁壞掉不拖垮首頁）
    try { $('cash').innerHTML = cashHTML(s.cash); } catch (x) { $('cash').innerHTML = '<section class="card"><div class="err">金流明細畫不出來：' + esc(x && x.message || x) + '</div></section>'; }
    try { $('sys').innerHTML = sysHTML(s, age, dead); } catch (x) { $('sys').innerHTML = '<section class="card"><div class="err">系統頁畫不出來：' + esc(x && x.message || x) + '</div></section>'; }
    pagePaint();
  }

  // ⛔ 畫面出錯 ≠ 密碼錯：解開之後的錯一律走這裡，照實講（第一版把畫面的錯誤誤報成「密碼不對」）。
  function safeRender(box, s) {
    try { render(box, s); }
    catch (e) {
      $('stat').className = 'stat warn';
      $('stat').innerHTML = '<span class="dot"></span><span>畫面出錯了</span>';
      $('board').innerHTML = '<section class="card"><div class="err">' + esc(e && e.message || e) + '（資料有收到、密碼是對的）</div></section>';
    }
  }

  function showLock(msg) {
    $('loading').hidden = true; $('main').hidden = true; $('lock').hidden = false; $('anView').hidden = true; AN.view = null;
    $('lockErr').hidden = !msg; $('lockErr').textContent = msg || '';
    // 解鎖畫面：頂列只剩名字（⛔ 沒解開就不知道面板狀態，不畫燈）、信件藏起來
    $('stat').className = 'stat'; $('stat').innerHTML = '<span class="brand">早盤儀表板</span>'; $('anMail').hidden = true;
  }
  // ⚠️ 正在看週報的時候，每 90 秒的更新 ⛔ 不可以把監控主畫面疊回來
  function showMain() { $('loading').hidden = true; $('lock').hidden = true; $('main').hidden = !!AN.view; pushPaint(); }

  function refresh() {
    return fetchBox().then(function (box) {
      if (!keys) return showLock();
      if (box.kdf && keys.salt !== box.kdf.salt) { keys = null; clearKeys(); return showLock('電腦那邊換過密碼了，請輸入新的密碼'); }
      return open(box, keys).then(function (s) { showMain(); safeRender(box, s); }, function () {
        keys = null; clearKeys(); showLock('密碼不對（或電腦那邊換過密碼），請再輸入一次');
      });
    }).catch(function (e) {
      fetchErr = '讀不到 GitHub（' + (e && e.message || '') + '）';
      if (lastBox && keys) { return open(lastBox, keys).then(function (s) { showMain(); safeRender(lastBox, s); }); }
      $('loading').textContent = '讀不到資料：' + fetchErr + '。面板可能還沒開始推送，稍後再試。';
    });
  }

  $('lockForm').addEventListener('submit', function (ev) {
    ev.preventDefault();
    var pw = $('pw').value; if (!pw) return;
    $('unlockBtn').disabled = true; $('unlockBtn').textContent = '解鎖中…';
    // 輸入密碼時一律先抓最新一份（手上那份可能是電腦換密碼之前的）；抓不到才用手上的
    var go = fetchBox().catch(function (e) { if (lastBox) return lastBox; throw e; });
    go.then(function (box) {
      return derive(pw, b64(box.kdf.salt), box.kdf.iter).then(function (k) {
        return open(box, k).then(function (s) { return { k: k, s: s, box: box }; });
      });
    }).then(function (x) {
      keys = x.k; if ($('remember').checked) saveKeys(x.k);
      $('pw').value = ''; showMain(); safeRender(x.box, x.s);
    }, function () { showLock('密碼不對，請再試一次'); })
      .then(function () { $('unlockBtn').disabled = false; $('unlockBtn').textContent = '解鎖'; });
  });

  $('logout').addEventListener('click', function () { keys = null; clearKeys(); showLock(); });

  // 鑰匙圈：只為了「讀過的週報同步回電腦」。⛔ 不跳開場介紹（這頁不是日誌 App）；解鎖後補送一次。
  if (window.Keyring) {
    try {
      Keyring.init({ appId: 'trade-log', appName: '📈 早盤儀表板', tokenKey: GH_TOKEN_KEY, enabled: true,
        onChange: function () { anPaintSync(); anSync(); pushPaint(); } });
    } catch (e) {}
  }

  keys = loadKeys();
  refresh();
  timer = setInterval(function () { if (!document.hidden) refresh(); }, POLL_MS);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) refresh(); });
  // 經過時間每 20 秒重畫一次（不用重抓）
  setInterval(function () { if (lastBox && keys && !$('main').hidden) open(lastBox, keys).then(function (s) { safeRender(lastBox, s); }); }, 20000);

  if ('serviceWorker' in navigator) { navigator.serviceWorker.register('sw.js').catch(function () {}); }
})();
