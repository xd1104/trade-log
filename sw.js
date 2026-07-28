// 微台指交易日誌 — Service Worker
// 改前端資源後把版本號 +1（tradelog-shell-vN）強制更新快取。
var CACHE = 'tradelog-shell-v9';
var SHELL = [
  './',
  './index.html',
  './css/style.css',
  './js/seed-data.js',
  './js/app.js',
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

  // 導覽請求：離線時回殼
  if (req.mode === 'navigate') {
    e.respondWith(fetch(req).catch(function () { return caches.match('./index.html'); }));
    return;
  }

  // 靜態資源：cache-first，背景更新
  e.respondWith(
    caches.match(req).then(function (cached) {
      var net = fetch(req).then(function (resp) {
        if (resp && resp.status === 200) {
          var copy = resp.clone();
          caches.open(CACHE).then(function (c) { c.put(req, copy); });
        }
        return resp;
      }).catch(function () { return cached; });
      return cached || net;
    })
  );
});
