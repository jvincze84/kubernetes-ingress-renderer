// Service worker caching strategy (app/static/sw.js), run in a fake worker scope with Node's fetch primitives.
const fs = require("fs"), path = require("path"), vm = require("vm");
const src = fs.readFileSync(path.join(__dirname, "../../app/static/sw.js"), "utf8");
const assert = (c, m) => { if (!c) { console.error("FAIL:", m); process.exit(1); } console.log("ok -", m); };

const ORIGIN = "https://iq.example";
const store = new Map(); // single fake cache: url -> Response
const keyOf = (k) => (typeof k === "string" ? new URL(k, ORIGIN).href : k.url);
const cache = {
  put: async (k, r) => { store.set(keyOf(k), r); },
  match: async (k) => store.get(keyOf(k))?.clone(),
  addAll: async (urls) => { for (const u of urls) store.set(keyOf(u), new Response("shell")); },
};
let network = null; // (request) => Response | throws
const listeners = {};
const scope = {
  location: new URL(ORIGIN + "/sw.js"),
  addEventListener: (t, fn) => { listeners[t] = fn; },
  skipWaiting: async () => {},
  clients: { claim: async () => {} },
};
const ctx = vm.createContext({
  self: scope, caches: { open: async () => cache, keys: async () => ["old-cache"], delete: async () => true },
  fetch: async (req) => network(req), Response, Headers, Request, URL, Promise, console, Date,
});
vm.runInContext(src, ctx);

function dispatchFetch(url, { method = "GET", mode = "cors" } = {}) {
  const request = new Request(ORIGIN + url, { method });
  Object.defineProperty(request, "mode", { value: mode });
  let responded = null;
  listeners.fetch({ request, respondWith: (p) => { responded = p; } });
  return responded; // null when the worker lets the browser handle it
}
const ok = (body, extra = {}) => Object.assign(new Response(body, { status: 200 }), {}, extra);
const basic = (r, redirected = false) => { Object.defineProperty(r, "type", { value: "basic" }); Object.defineProperty(r, "redirected", { value: redirected }); return r; };

(async () => {
  await new Promise((res) => listeners.install({ waitUntil: (p) => p.then(res) }));
  assert(store.has(ORIGIN + "/"), "install precaches the shell");

  network = () => basic(ok('{"items":[1]}'));
  let r = await dispatchFetch("/api/ingresses");
  assert((await r.text()) === '{"items":[1]}' && !r.headers.get("X-SW-Cached-At"), "API online: network response, not marked");
  assert(store.has(ORIGIN + "/api/ingresses"), "API response cached");

  network = () => { throw new TypeError("offline"); };
  r = await dispatchFetch("/api/ingresses");
  assert((await r.text()) === '{"items":[1]}', "API offline: cached response served");
  assert(!isNaN(Date.parse(r.headers.get("X-SW-Cached-At"))), "API offline: marked with X-SW-Cached-At timestamp");

  let failed = false;
  try { await dispatchFetch("/api/ingresses/x/y"); } catch { failed = true; }
  assert(failed, "API offline without cache: error propagates");

  r = await dispatchFetch("/ingress-detail-whatever", { mode: "navigate" });
  assert((await r.text()) === "shell", "navigation offline: shell served");

  network = () => basic(ok("login page"), true);
  await dispatchFetch("/api/ingresses/a/b");
  assert(!store.has(ORIGIN + "/api/ingresses/a/b"), "redirected (auth proxy) responses are not cached");

  assert(dispatchFetch("/api/ingresses", { method: "POST" }) === null, "non-GET requests are not intercepted");
  assert(dispatchFetch("/healthz") === null, "other paths are not intercepted");
  console.log("ALL OK");
})();
