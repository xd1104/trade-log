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
  function modeTxt(on, live) { return on ? (live ? '開著・真錢' : '開著・演練') : '關著'; }

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
      return '<div class="an-nw"><div class="tp">' + anLamp(s.lamp === 'ok' ? 'ok' : 'wn', s.lamp_word) + '</div><h3>' + esc(s.name) + '</h3>' +
        row('本週模擬', '<span class="num ' + cls(s.week_sim_pts) + '">' + pm(s.week_sim_pts) + '</span>（' + s.week_sim_n + ' 筆）') +
        row('本週真單', '<span class="num ' + cls(s.week_real_pts) + '">' + pm(s.week_real_pts) + '</span>（' + s.week_real_n + ' 筆）') +
        row('近 15 筆每筆／歷史', '<span class="num">' + pm(s.avg15) + ' / ' + pm(s.avg_all) + '</span>') + '</div>';
    }).join('');
    var mk = (((F.market || {}).cards) || []).map(function (c) { return row(c.title, '<span class="num">' + (c.value == null ? '—' : esc(c.value) + esc(c.unit || '')) + '</span>' + (c.pct == null ? '' : '<div class="faint" style="font-size:11.5px">過去一年第 ' + c.pct + ' 百分位</div>')); }).join('');
    var S = F.system || {}, K = F.risk || {};
    var sys = row('日盤送單', (S.sent_day || 0) + ' 筆（成交 ' + (S.ok_day || 0) + '）') + row('夜盤送單', (S.sent_night || 0) + ' 筆（成交 ' + (S.ok_night || 0) + '）') +
      row('進場滑價', S.slip_avg == null ? '—' : '平均 ' + S.slip_avg + ' 點') + row('送出沒撮到', (S.ioc_nofill || 0) + ' 次') +
      row('本月風控', '<span class="num ' + cls(K.pnl) + '">' + pm(K.pnl) + '</span> / −' + Math.round(K.cap || 0).toLocaleString() + ' 點' + (K.blocked ? '（已停）' : '')) +
      (S.problems || []).map(function (p) { return '<div class="err">⚠️ ' + esc(p.what) + '（' + p.n + ' 次）</div>'; }).join('');
    var cand = (F.candidates || []).map(function (c) { return row(c.name, '<span class="num">' + c.diff_n + ' / ' + c.need + '</span><div class="faint" style="font-size:11.5px">只算跟「' + esc(c.base) + '」不一樣的</div>'); }).join('');
    var recs = (R.recs || []).map(function (r) { return '<div class="an-nw an-rec"><div class="tp">' + anTag(r.tag) + '</div><h3>' + esc(r.title) + '</h3><p>' + esc(r.body) + '</p><div class="ask">要你決定：' + esc(r.ask) + '</div></div>'; }).join('') || '<div class="msg">這週沒有建議。</div>';
    var segs = [['news', '國際消息'], ['cal', '下週大事'], ['env', '大環境'], ['st', '策略'], ['mk', '市場'], ['sys', '系統'], ['cand', '候選'], ['rec', '建議']];
    return '<div class="an-seg">' + segs.map(function (x, i) { return '<button data-anjump="' + x[0] + '"' + (i ? '' : ' class="on"') + '>' + x[1] + '</button>'; }).join('') + '</div>' +
      '<div class="card"><div class="an-verdict">' + anLamp(R.verdict && R.verdict.lamp) + '<p>' + esc((R.verdict || {}).line || '') + '</p></div></div>' +
      anSec('news', '國際金融消息（每則附來源）', news) + anSec('cal', '下週大事', cal) + (env ? anSec('env', '大環境觀察', env) : '') +
      anSec('st', '策略健康', st) + anSec('mk', '市場狀態', mk) + anSec('sys', '系統與風控', sys) + anSec('cand', '模擬候選（滿 25 筆才判斷）', cand) +
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
      c.innerHTML = '<h2>斷線通知</h2><div class="msg">這樣打開收不到通知：請用 Safari「分享 → 加入主畫面」，之後從主畫面的圖示打開（iOS 16.4 以上）。</div>';
      return;
    }
    c.innerHTML = '<h2>斷線通知</h2><div class="msg">面板超過 10 分鐘沒回報（關機、斷網、當機）時手機會響，恢復時再響一次。</div>' +
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

  function render(box, s) {
    var age = Date.now() / 1000 - box.t;
    var dead = age > DEAD_S, slow = age > SLOW_S;
    var errs = (s.errs || []).slice();
    var st = dead ? 'dead' : (slow || errs.length ? 'slow' : 'ok');
    var title = dead ? '面板沒有回報了' : errs.length ? '面板有狀況' : slow ? '有點久沒回報' : '面板正常運作中';
    var sub = dead ? '已經 ' + ago(age).replace('前', '') + '沒收到 —— 可能是電腦睡著、斷網或面板關掉了'
      : '最後回報 ' + ago(age) + '（' + esc((s.at || '').slice(11, 16)) + '）';
    $('alive').className = 'card alive ' + st;
    $('alive').innerHTML = '<span class="dot"></span><div><div class="t">' + title + '</div><div class="s">' + sub + '</div></div>';
    $('upd').innerHTML = '更新 ' + (lastFetchOk ? ago((Date.now() - lastFetchOk) / 1000) : '—');

    // 部位
    var r = s.real || {}, p = r.position;
    if (p) {
      var fp = r.float_pts;
      $('pos').innerHTML = '<h2>現在的部位</h2><div class="big ' + cls(fp) + ' num">' + pm(fp) + ' <small class="dim" style="font-size:14px">點</small></div>' +
        row('方向', '<b>' + dirTxt(p.dir) + ' ' + (p.qty || 1) + ' 口</b>') +
        row('進場', '<span class="num">' + esc(p.entry) + '</span>' + (p.entry_time ? '（' + esc(String(p.entry_time).slice(0, 8)) + '）' : '')) +
        row('停損 / 停利', '<span class="num">' + esc(r.sl == null ? '—' : r.sl) + ' / ' + (p.no_tp ? '不設停利' : esc(r.tp == null ? '—' : r.tp)) + '</span>') +
        (r.stale_sec != null ? '<div class="err">⚠️ 報價已中斷 ' + esc(r.stale_sec) + ' 秒 —— 停損現在沒人在看</div>' : '');
    } else if (!s.real || !('position' in r)) {
      // ⛔ 面板沒回應、或剛啟動還沒問到券商 ≠ 沒有部位：不知道就講不知道
      $('pos').innerHTML = '<h2>現在的部位</h2><div class="big gold" style="font-size:20px">還不知道</div><div class="msg">' +
        (s.real ? '面板剛啟動，還在跟券商確認部位（下一次回報就會有）。' : '這一次面板沒回應，讀不到部位。') + '</div>';
    } else {
      $('pos').innerHTML = '<h2>現在的部位</h2><div class="big dim" style="font-size:20px">沒有部位</div>';
    }

    // ⭐ 2026-09-24 風控規則 B：日盤＋夜盤**共用**的本月額度 ⇒ 自己一張卡（⛔ 不放在日盤或夜盤裡）
    var rk0 = s.risk || {};
    $('risk').hidden = !(rk0.cap || rk0.err);
    if (rk0.err) {
      $('risk').innerHTML = '<h2>本月風控（日盤＋夜盤共用）</h2><div class="err">⚠️ 算不出本月損益：' + esc(rk0.err) + '（算不出來時不送單）</div>';
    } else if (rk0.cap) {
      var used = rk0.pnl < 0 ? Math.min(100, Math.round(-rk0.pnl * 100 / rk0.cap)) : 0;
      $('risk').innerHTML = '<h2>本月風控（日盤＋夜盤共用）' + (rk0.blocked ? ' <span class="pill on">已停單</span>' : (rk0.override ? ' <span class="pill on">已手動解除</span>' : '')) + '</h2>' +
        row('本月自動單', '<span class="num ' + (rk0.hit ? 'gold' : cls(rk0.pnl)) + '">' + pm(rk0.pnl) + ' 點</span>') +
        row('上限', '<span class="num">−' + Math.round(rk0.cap).toLocaleString() + ' 點</span>') +
        '<div class="bar"><i style="width:' + used + '%"></i></div>' +
        '<div class="msg">' + (rk0.blocked ? '到了上限：這個月日盤、夜盤都不送，下個月自動恢復。' :
          (rk0.override ? '超過上限，但你已經手動解除，這個月照常送。' : '虧到上限 ⇒ 這個月日盤、夜盤都停，下個月自動恢復。')) +
          (rk0.since ? '（' + esc(String(rk0.since).slice(5)) + ' 起算）' : '') + '</div>';
    }

    // 日盤
    var d = s.day;
    if (d) {
      var h = '<h2>日盤自動下單 <span class="pill ' + (d.armed ? 'on' : '') + '">' + esc(modeTxt(d.armed, d.live)) + '</span></h2>' +
        row('做法', esc(d.method_name || d.method || '—')) + row('今天', esc(d.today || ''));
      var today = (d.days || [])[0];
      if (today && today.date === d.today) {
        if (today.trade) {
          var t = today.trade;
          h += '<div class="msg"><b>' + (t.ok ? '已送出' : '沒送成') + '：' + dirTxt(t.dir) + '</b> 進場 <span class="num">' + esc(t.entry) + '</span>（' + esc(t.entry_time || '') + '）' +
            (t.ok ? '　停損 <span class="num">' + esc(t.sl) + '</span>　停利 ' + (t.no_tp ? '不設' : '<span class="num">' + esc(t.tp) + '</span>') : '　' + esc(t.why_msg || '')) + '</div>';
        }
        (today.cands || []).forEach(function (m) { h += '<div class="msg">' + esc(m) + '</div>'; });
        if (today.note) h += '<div class="msg">' + esc(today.note) + '</div>';
        if (today.real && today.real.state === 'ok') h += '<div class="msg">出場 ' + esc(today.real.exit_time || '') + '　<b class="' + cls(today.real.points) + '">' + pm(today.real.points) + ' 點</b></div>';
        if (today.eod && today.eod.alarm) h += '<div class="err">⚠️ ' + esc(today.eod.why_msg || today.eod.msg || '收盤平倉有狀況') + '</div>';
      } else {
        h += '<div class="msg">今天還沒有紀錄（' + esc(d.signal_at || '09:03:30') + ' 才開始看）</div>';
      }
      var hist = (d.days || []).filter(function (x) { return x.date !== d.today; }).slice(0, 4);
      if (hist.length) {
        h += '<div class="hist">' + hist.map(function (x) {
          var v = x.trade ? (x.real && x.real.state === 'ok' ? '<b class="' + cls(x.real.points) + '">' + pm(x.real.points) + ' 點</b>' : dirTxt(x.trade.dir) + '・點數對不到')
            : '<span class="faint">沒做</span>';
          return row(x.date.slice(5), v);
        }).join('') + '</div>';
      }
      if (d.err_n) h += '<div class="err">⚠️ 日盤背景出錯 ' + esc(d.err_n) + ' 次：' + esc(d.err || '') + '</div>';
      $('day').innerHTML = h;
    } else { $('day').innerHTML = '<h2>日盤自動下單</h2><div class="msg">讀不到</div>'; }

    // 夜盤
    var n = s.night;
    if (n) {
      var hn = '<h2>夜盤自動下單 <span class="pill ' + (n.on ? 'on' : '') + '">' + esc(modeTxt(n.on, n.live)) + '</span></h2>' +
        row('做法', esc(n.method_name || n.method || '—')) +
        (n.tonight ? row('今晚幾點看', esc(n.tonight.look_at || '')) : '');
      (n.recent || []).slice(0, 4).forEach(function (x) {
        var what = x.rec === 'result' ? ((x.ok ? '已送出 ' : '沒送成 ') + dirTxt(x.dir) + (x.entry ? ' 進場 ' + x.entry : '') + (x.err ? '：' + x.err : ''))
          : x.rec === 'eod' ? (x.msg || '04:58 平倉') : (x.msg || x.why || '');
        hn += row((x.E || '').slice(5), '<span class="dim" style="font-size:13px">' + esc(what) + '</span>');
      });
      if (n.errors) hn += '<div class="err">⚠️ 夜盤背景出錯 ' + esc(n.errors) + ' 次：' + esc(n.last_err || '') + '</div>';
      $('night').innerHTML = hn;
    } else { $('night').innerHTML = '<h2>夜盤自動下單</h2><div class="msg">讀不到</div>'; }

    // 帳戶
    var e = s.equity || {};
    $('acct').innerHTML = '<h2>帳戶（券商端）</h2>' +
      row('權益總值', '<span class="num">' + (e.equity != null ? Math.round(e.equity).toLocaleString() + ' 元' : '—') + '</span>') +
      row('今日損益', '<span class="num ' + cls(e.day_pl) + '">' + (e.day_pl != null ? pm(e.day_pl) + ' 元' : '—') + '</span>') +
      row('查詢時間', esc(e.at || '—')) +
      // ⭐ 2026-09-24：保證金還撐得住幾次停損（整句面板算好的）
      (e.cushion && e.cushion.msg ? '<div class="msg' + (e.cushion.warn ? ' gold' : '') + '">' + esc(e.cushion.msg) + '</div>' : '');

    // 警示
    var pn = s.panel || {}, w = errs.slice();
    // ⭐ 2026-09-24 風控規則 B 與保證金提醒：到了就放進「要注意的事」
    var rk = s.risk || {};
    if (rk.blocked) w.push(rk.msg || '本月自動單到了風控上限，這個月不送');
    if (rk.err) w.push('風控算不出本月損益：' + rk.err);
    if (e.cushion && e.cushion.warn) w.push(e.cushion.msg);
    if (e.backup && e.backup.warn) w.push(e.backup.msg);
    if (pn.conn && pn.conn.ok === false) w.push('跟永豐的連線有問題：' + (pn.conn.last_error || ''));
    // ⚠️ 券商的 last_error 沒有時間、而且背景對帳偶發失敗也會寫進來（跟真單無關）⇒ 不當警示，放最底下小字
    // ⭐ 分析師信件：列表跟著快照更新（打開中的全文不重畫，免得他看到一半跳掉）
    AN.snap = s.analyst || null;
    anPaintBtn();
    if (AN.view === 'list') anPaintList();
    $('warn').hidden = !w.length;
    $('warn').innerHTML = '<h2>要注意的事</h2>' + w.map(function (x) { return '<div class="item">⚠️ ' + esc(x) + '</div>'; }).join('');

    $('footInfo').innerHTML = '面板每 2 分鐘回報一次，手機最慢約 3 分鐘看到。電腦：' + esc(s.host || '') +
      (r.started ? '・面板 ' + esc(r.started) + ' 啟動' : '') +
      (r.last_error ? '<br><span class="faint">券商最後一次錯誤訊息（不一定是現在）：' + esc(String(r.last_error).slice(0, 60)) + '</span>' : '') + (fetchErr ? '<br><span class="gold">上一次更新失敗：' + esc(fetchErr) + '</span>' : '');
  }

  // ⛔ 畫面出錯 ≠ 密碼錯：解開之後的錯一律走這裡，照實講（第一版把畫面的錯誤誤報成「密碼不對」）。
  function safeRender(box, s) {
    try { render(box, s); }
    catch (e) {
      $('alive').className = 'card alive slow';
      $('alive').innerHTML = '<span class="dot"></span><div><div class="t">畫面出錯了</div><div class="s">' +
        esc(e && e.message || e) + '（資料有收到、密碼是對的）</div></div>';
    }
  }

  function showLock(msg) {
    $('loading').hidden = true; $('main').hidden = true; $('lock').hidden = false;
    $('lockErr').hidden = !msg; $('lockErr').textContent = msg || '';
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
