const CACHE = "softblue-v9";
// Relative URLs so the app works whether it's served from a domain root or a
// subpath (e.g. user.github.io/softblue/). The Cache API stores entries by
// their resolved absolute URL, so these resolve against the SW's scope.
const STATIC = [
  "./", "./index.html", "./style.css",
  "./tone-engine.js", "./app.js",
  "./manifest.json", "./favicon.ico",
  "./icon-192.png", "./icon-512.png",
];

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
  const url = new URL(e.request.url);
  // Optional backend (api/ws) always goes to network; the app degrades
  // gracefully to its localStorage store when the server isn't there.
  if (url.pathname.includes("/api/") || url.pathname.includes("/ws/")) return;
  if (e.request.method !== "GET") return;
  // Cache-first with ignoreSearch so versioned assets (app.js?v=1.2.3) still
  // match their cached, query-less entry when offline.
  e.respondWith(
    caches.match(e.request, { ignoreSearch: true }).then(cached => {
      if (cached) return cached;
      return fetch(e.request).catch(() =>
        // Offline navigation to any URL falls back to the cached shell.
        e.request.mode === "navigate" ? caches.match("./") : Response.error()
      );
    })
  );
});
