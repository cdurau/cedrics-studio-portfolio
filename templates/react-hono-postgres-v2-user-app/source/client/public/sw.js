// Service worker for the Studio user-app profile.
//
// Strategy: network-first for navigations with an offline fallback; nothing
// user-specific is ever cached. The cache name carries the app slug so two
// apps on two origins never collide, and bumping the version string is the
// documented way to invalidate a stale shell.
const CACHE = "__APP_SLUG__-v1";
const OFFLINE_URL = "/offline.html";
const PRECACHE = [OFFLINE_URL, "/manifest.webmanifest", "/icons/icon-192.png", "/icons/icon-512.png"];

// Never cached — these carry session state or readiness truth and a stale
// answer would be worse than no answer.
const NEVER_CACHE = [/^\/api\//, /^\/healthz$/, /^\/readyz$/];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (NEVER_CACHE.some((re) => re.test(url.pathname))) return;

  if (request.mode === "navigate") {
    event.respondWith(fetch(request).catch(() => caches.match(OFFLINE_URL)));
    return;
  }

  event.respondWith(
    fetch(request)
      .then((response) => {
        // Only immutable build output is worth keeping.
        if (response.ok && url.pathname.startsWith("/assets/")) {
          const copy = response.clone();
          caches.open(CACHE).then((c) => c.put(request, copy));
        }
        return response;
      })
      .catch(() => caches.match(request)),
  );
});
