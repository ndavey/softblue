const CACHE = "softblue-v1";
const STATIC = ["/", "/style.css", "/tone-engine.js", "/app.js", "/manifest.json", "/favicon.ico", "/icon-192.png", "/icon-512.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(STATIC)));
  self.skipWaiting();
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
  ));
  self.clients.claim();
});

self.addEventListener("fetch", e => {
  // API and WebSocket calls always go to network.
  if (e.request.url.includes("/api/") || e.request.url.includes("/ws/")) return;
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request))
  );
});
