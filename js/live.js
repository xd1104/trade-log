/* 即時盤面：與本機的早盤面板（tools/shioaji/live_panel.py）連動。
 *
 * 設計原則
 *  - 面板沒開、或在手機上（連不到電腦的 localhost）→ 整區自動隱藏，
 *    App 其他功能完全不受影響。這是「優雅降級」，不是錯誤狀態。
 *  - 練習平倉後，紀錄直接寫進 App 自己的 localStorage，
 *    跟手動記錄的交易同一份資料 —— 匯出／統計／月份分組全部通用。
 *  - 【鐵律】只做模擬練習，不會送任何委託到永豐。真實下單由 Benson 自己操作。
 */
(function () {
  var PANEL = 'http://127.0.0.1:8770';
  var POLL_ON = 700;      // 連得到時的更新頻率
  var POLL_OFF = 15000;   // 連不到時放慢，不要一直打
  var timer = null, lastPos = null, alive = false;
  // 只有內容真的變了才重繪 —— 否則每 0.7 秒重建一次 DOM，
  // 使用者剛好在那一瞬間按下去按鈕會失效（實測過）
  var lastGrid = '', lastAct = '';

  var $ = function (id) { return document.getElementById(id); };
  var f = function (n, d) { return n == null ? '—' : Number(n).toFixed(d || 0); };
  var pm = function (v, d) { return (v > 0 ? '+' : '') + f(v, d); };
  var sgn = function (v) { return v > 0 ? 'up' : v < 0 ? 'down' : ''; };

  function cell(label, value, cls, extra) {
    return '<div class="cell ' + (extra || '') + '"><div class="l">' + label +
      '</div><div class="v ' + (cls || '') + '">' + value + '</div></div>';
  }

  function render(s) {
    var c = s.chips || {}, P = s.position;
    var age = s.age_sec == null ? 99 : s.age_sec;
    var dead = (s.conn && s.conn.ok === false) || age > 90;
    var phase = { recording: '記錄中', live: '顯示中', off: '夜盤' }[s.phase] || '';

    $('liveMeta').textContent = ((s.conn && s.conn.contract_name) || '微台') +
      ' · ' + (s.clock || '').slice(0, 5) + (phase ? ' · ' + phase : '');

    var g = '<div class="cell px"><div class="l">成交價</div><div class="v">' +
      f(c.price) + '</div></div>' +
      cell('最近 5 分鐘', pm(c.mom5) + ' 點', sgn(c.mom5)) +
      cell('最近 15 分鐘', pm(c.mom15) + ' 點', sgn(c.mom15));
    if (c.chg != null) {
      g += cell('對開盤', pm(c.chg) + ' 點', sgn(c.chg)) +
        cell('跳空', pm(c.gap) + ' 點', sgn(c.gap)) +
        cell('今日震幅', f(c.rng) + ' 點') +
        cell('位階', f(c.pos * 100) + '%') +
        cell('量能', f(c.vol_ratio, 2) + ' 倍') +
        cell('買 / 賣', f(c.bid) + ' / ' + f(c.ask));
    }
    if (g !== lastGrid) { $('liveGrid').innerHTML = g; lastGrid = g; }

    var a = '';
    if (dead) {
      a += '<div class="live-dead"><b>報價已中斷</b>　畫面上的數字是舊的（' +
        (age == null ? '尚未收到' : age + ' 秒前') + '）。面板每分鐘會自動重連。</div>';
    }
    if (P) {
      a += '<div class="live-pnl"><div class="v ' + sgn(P.float_pts) + '">' +
        pm(P.float_pts) + '</div><div class="l">' +
        (P.dir === 'long' ? '做多' : '做空') + '　進場 ' + f(P.entry) + '　' + P.entry_time +
        '</div></div>' +
        '<div class="live-lim"><span>停利 ' + f(P.tp) + '</span><span>停損 ' + f(P.sl) + '</span></div>' +
        '<div class="live-btns"><button type="button" class="fl" data-live="close">手動平倉</button>' +
        '<button type="button" class="gh" data-live="undo">取消</button></div>';
    } else {
      a += '<div class="live-btns"><button type="button" class="lg" data-live="long">▲ 做多</button>' +
        '<button type="button" class="sh" data-live="short">▼ 做空</button></div>';
    }
    if (a !== lastAct) { $('liveAct').innerHTML = a; lastAct = a; }
  }

  /* 把面板今天已平倉的練習單同步進 App。
     不只在「持倉→無持倉」那一瞬間同步 —— 那樣的話平倉當下沒開 App 就永遠漏掉。
     改成每次輪詢都比對，靠 addTrade 的同日去重擋掉重複寫入。 */
  function absorbClosed(s) {
    var T = s.today_trades || [];
    if (!T.length || !window.TradeLog || !window.TradeLog.addTrade) return;
    T.forEach(function (t) {
      var added = window.TradeLog.addTrade({
        date: t.date, dir: t.dir, entry: t.entry,
        exit: t.exit, time: t.time, note: t.note || '', mode: 'sim'
      });
      if (added && lastPos) {
        var r = { tp: '停利', sl: '停損', manual: '手動平倉', close: '收盤平倉' }[t._reason] || '平倉';
        window.TradeLog.toast(r + '　' + pm(t._net) + ' 點，已記錄');
      }
    });
    lastPos = s.position;
  }

  function poll() {
    fetch(PANEL + '/api/state', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (s) {
        if (!alive) { alive = true; schedule(); }
        $('liveWrap').hidden = false;
        if (s.status === 'live') { render(s); absorbClosed(s); }
        else {
          $('liveMeta').textContent = (s.clock || '').slice(0, 5);
          var w = '<div class="cell px"><div class="l">' +
            (s.msg || '等待報價…') + '</div><div class="v">—</div></div>';
          if (w !== lastGrid) { $('liveGrid').innerHTML = w; lastGrid = w; }
          if (lastAct !== '') { $('liveAct').innerHTML = ''; lastAct = ''; }
        }
      })
      .catch(function () {
        // 手機、或電腦上沒開面板 —— 正常情況，不是錯誤
        if (alive) { alive = false; schedule(); }
        $('liveWrap').hidden = true;
      });
  }

  function schedule() {
    clearInterval(timer);
    timer = setInterval(poll, alive ? POLL_ON : POLL_OFF);
  }

  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-live]');
    if (!b) return;
    var a = b.getAttribute('data-live');
    var url = (a === 'long' || a === 'short') ? '/api/enter' : '/api/' + a;
    var body = (a === 'long' || a === 'short') ? JSON.stringify({ dir: a }) : '{}';
    b.disabled = true;
    fetch(PANEL + url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body })
      .then(function (r) { return r.json(); })
      .then(function (r) {
        if (!r.ok && r.msg && window.TradeLog) window.TradeLog.toast(r.msg);
        poll();
      })
      .catch(function () {})
      .then(function () { b.disabled = false; });
  });

  poll();
  schedule();
})();
