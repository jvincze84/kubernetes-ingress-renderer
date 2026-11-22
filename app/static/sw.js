/* Ingress Query service worker.
 *
 * - App shell (/, manifest): network-first, so a new deployment is picked up immediately;
 *   the cached copy is only used offline.
 * - Icons: cache-first (they rarely change; a new CACHE version drops them).
 * - API GETs: network-first. When the network fails, the last successful response is served
 *   with an "X-SW-Cached-At" header so the UI can show that the data is stale.
 * - Nothing is ever written to the cluster, and only same-origin GETs are handled.
 */
const CACHE = "ingress-query-v1";
const SHELL = ["/", "/manifest.webmanifest", "/static/icons/icon.svg", "/static/icons/icon-192.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((c) => c.addAll(SHELL))
      .catch(() => {}) // e.g. auth proxy redirect during install: still install, cache fills later
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

async function networkFirst(request, cacheKey, markStale) {
  const cache = await caches.open(CACHE);
  try {
    const response = await fetch(request);
    // Only cache real successful same-origin answers (not auth redirects / errors).
    if (response.ok && response.type === "basic" && !response.redirected) {
      const copy = response.clone();
      const headers = new Headers(copy.headers);
      headers.set("X-SW-Fetched-At", new Date().toISOString());
      const body = await copy.blob();
      await cache.put(cacheKey, new Response(body, { status: copy.status, statusText: copy.statusText, headers }));
    }
    return response;
  } catch (err) {
    const cached = await cache.match(cacheKey);
    if (!cached) throw err;
    if (!markStale) return cached;
    const headers = new Headers(cached.headers);
    headers.set("X-SW-Cached-At", cached.headers.get("X-SW-Fetched-At") || "");
    return new Response(await cached.blob(), { status: cached.status, statusText: cached.statusText, headers });
  }
}

async function cacheFirst(request) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(request);
  if (cached) return cached;
  const response = await fetch(request);
  if (response.ok && response.type === "basic") await cache.put(request, response.clone());
  return response;
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (request.mode === "navigate") {
    // Every route is client-side (hash), so all navigations map to the shell.
    event.respondWith(networkFirst(request, "/", false));
  } else if (url.pathname.startsWith("/api/")) {
    event.respondWith(networkFirst(request, request, true));
  } else if (url.pathname.startsWith("/static/icons/")) {
    event.respondWith(cacheFirst(request));
  } else if (url.pathname === "/manifest.webmanifest") {
    event.respondWith(networkFirst(request, request, false));
  }
});
