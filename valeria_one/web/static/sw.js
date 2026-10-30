/* Valeria One service worker (v0.18). What it keeps: Valeria One's own files (/static/…) and the offline page — nothing
   else. A signed-in page, an API answer (/app, /v1, /hooks…) or anyone's data is NEVER put in the cache: those always
   come from the network, and when there is none the offline page is shown. A new version empties the old cache. */
var VERSION = '__VERSION__';
var CACHE = 'vo-shell-' + VERSION;
var OFFLINE = '/offline';
var SHELL = [OFFLINE, '/static/app.css?v=' + VERSION, '/static/vo.css?v=' + VERSION, '/static/app.js?v=' + VERSION,
             '/static/pwa.js?v=' + VERSION, '/static/icon.svg', '/static/icons/icon-192.png'];

function cacheable(url) {                       /* the one rule: only our own static files */
  return url.origin === self.location.origin && url.pathname.indexOf('/static/') === 0;
}

self.addEventListener('install', function (event) {
  event.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(SHELL); }).then(function () { return self.skipWaiting(); }));
});

self.addEventListener('activate', function (event) {
  event.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) { return k.indexOf('vo-shell-') === 0 && k !== CACHE; })
      .map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});

self.addEventListener('fetch', function (event) {
  var req = event.request;
  if (req.method !== 'GET') return;
  var url = new URL(req.url);
  if (cacheable(url)) {
    event.respondWith(caches.match(req).then(function (hit) {
      return hit || fetch(req).then(function (res) {
        if (res && res.ok && res.type === 'basic') {
          var copy = res.clone();
          caches.open(CACHE).then(function (c) { c.put(req, copy); });
        }
        return res;
      });
    }));
    return;
  }
  if (req.mode === 'navigate') {                /* pages: always the network; offline -> the offline page (never a stored page) */
    event.respondWith(fetch(req).catch(function () { return caches.match(OFFLINE); }));
  }
});

self.addEventListener('push', function (event) {
  var d = {};
  try { d = event.data ? event.data.json() : {}; } catch (e) { d = {body: event.data ? event.data.text() : ''}; }
  event.waitUntil(self.registration.showNotification(d.title || 'Valeria One', {
    body: d.body || '', icon: '/static/icons/icon-192.png', badge: '/static/icons/icon-96.png',
    data: {url: (d.url && d.url.charAt(0) === '/') ? d.url : '/app'}
  }));
});

self.addEventListener('notificationclick', function (event) {
  event.notification.close();
  var url = (event.notification.data && event.notification.data.url) || '/app';
  event.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(function (list) {
    for (var i = 0; i < list.length; i++) { if ('focus' in list[i]) { list[i].navigate(url); return list[i].focus(); } }
    return self.clients.openWindow(url);
  }));
});
