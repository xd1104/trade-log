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

    // 日盤
    var d = s.day;
    if (d) {
      var h = '<h2>日盤自動下單 <span class="pill ' + (d.armed ? 'on' : '') + '">' + esc(modeTxt(d.armed, d.live)) + '</span></h2>' +
        row('做法', esc(d.method_name || d.method || '—')) + row('今天', esc(d.today || ''));
      // ⭐ 2026-09-24 風控規則 B：本月自動單（日盤＋夜盤）損益／上限
      var rk0 = d.risk || {};
      if (rk0.cap) h += row('本月風控', '<span class="num' + (rk0.hit ? ' gold' : '') + '">' + pm(rk0.pnl) + ' / −' + Math.round(rk0.cap).toLocaleString() + ' 點</span>' +
        (rk0.blocked ? '<span class="gold">　已停</span>' : (rk0.override ? '<span class="gold">　已手動解除</span>' : '')));
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
    var rk = (s.day || {}).risk || {};
    if (rk.blocked) w.push(rk.msg || '本月自動單到了風控上限，這個月不送');
    if (rk.err) w.push('風控算不出本月損益：' + rk.err);
    if (e.cushion && e.cushion.warn) w.push(e.cushion.msg);
    if (pn.conn && pn.conn.ok === false) w.push('跟永豐的連線有問題：' + (pn.conn.last_error || ''));
    // ⚠️ 券商的 last_error 沒有時間、而且背景對帳偶發失敗也會寫進來（跟真單無關）⇒ 不當警示，放最底下小字
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
  function showMain() { $('loading').hidden = true; $('lock').hidden = true; $('main').hidden = false; }

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

  keys = loadKeys();
  refresh();
  timer = setInterval(function () { if (!document.hidden) refresh(); }, POLL_MS);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) refresh(); });
  // 經過時間每 20 秒重畫一次（不用重抓）
  setInterval(function () { if (lastBox && keys && !$('main').hidden) open(lastBox, keys).then(function (s) { safeRender(lastBox, s); }); }, 20000);

  if ('serviceWorker' in navigator) { navigator.serviceWorker.register('sw.js').catch(function () {}); }
})();
