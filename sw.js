// 微台指交易日誌 — Service Worker
// 改前端資源後把版本號 +1（tradelog-shell-vN）強制更新快取。
var CACHE = 'tradelog-shell-v29';
// ⭐ 2026-09-24 改版成【手機監控】：只剩這幾支。⚠️ 監控資料在 GitHub（跨網域）⇒ 這支 SW 不碰它（下面 origin 不同就放行）。
var SHELL = [
  './',
  './index.html',
  './css/monitor.css',
  './js/monitor.js',
  './js/keyring-unlock.js',
  './manifest.webmanifest',
  './icons/icon-192.png',
  './icons/icon-512.png',
  './icons/apple-touch-icon.png'
];

self.addEventListener('install', function (e) {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(SHELL); }).catch(function () {}));
});

self.addEventListener('activate', function (e) {
  e.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.map(function (k) { if (k !== CACHE) return caches.delete(k); }));
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener('fetch', function (e) {
  var req = e.request;
  if (req.method !== 'GET') return;
  var url = new URL(req.url);
  if (url.origin !== location.origin) return;

  // network-first：有網路一律拿最新版（並更新快取）；離線才退回快取。
  //
  // 【手機一直開到舊版的元凶】原本直接 fetch(req)，瀏覽器自己那層 HTTP 快取
  // 會先回舊檔，SW 拿到舊的又存進 CACHE，於是永遠更新不了。
  // 對自家的 HTML/JS/CSS 一律用 no-store 重新抓，繞過那層快取。
  var bust = req.mode === 'navigate' || /\.(html|js|css|webmanifest)$/.test(url.pathname);
  var hit = bust ? new Request(req.url, { cache: 'no-store', credentials: 'same-origin' }) : req;

  e.respondWith(
    fetch(hit).then(function (resp) {
      if (resp && resp.status === 200) {
        var copy = resp.clone();
        caches.open(CACHE).then(function (c) { c.put(req, copy); });
      }
      return resp;
    }).catch(function () {
      return caches.match(req).then(function (cached) {
        return cached || (req.mode === 'navigate' ? caches.match('./index.html') : undefined);
      });
    })
  );
});

// ⭐ 2026-09-24【斷線通知】GitHub Actions 的看門狗（.github/watchdog/watchdog.py）推過來的通知。
//   ⛔ 只顯示，不做任何事（這個 App 本來就只看不動）。
self.addEventListener('push', function (e) {
  var d = {};
  try { d = e.data ? e.data.json() : {}; } catch (x) { d = { title: '早盤儀表板', body: e.data ? e.data.text() : '' }; }
  e.waitUntil(self.registration.showNotification(d.title || '早盤儀表板', {
    body: d.body || '', tag: d.tag || 'panel', renotify: true,
    icon: 'icons/icon-192.png', badge: 'icons/icon-192.png'
  }));
});
self.addEventListener('notificationclick', function (e) {
  e.notification.close();
  e.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (ws) {
    for (var i = 0; i < ws.length; i++) { if ('focus' in ws[i]) return ws[i].focus(); }
    return clients.openWindow('./');
  }));
});
