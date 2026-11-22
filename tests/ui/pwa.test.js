// PWA behaviour of index.html: manifest/meta tags, install button, offline (cached data) banner.
const { JSDOM } = require("jsdom");
const html = require("fs").readFileSync(require("path").join(__dirname, "../../app/static/index.html"), "utf8");

const items = [{ namespace: "a", name: "web", ingressClass: null, hosts: [], addresses: [], services: [], created: null, health: { status: "UP", reasons: [] } }];
let cachedHeader = null; // value of X-SW-Cached-At the fake service worker returns
const dom = new JSDOM(html, { runScripts: "dangerously", url: "http://x/#/", beforeParse(w) {
  w.fetch = async () => ({ ok: true, headers: { get: (h) => (h === "X-SW-Cached-At" ? cachedHeader : null) }, json: async () => ({ items }) });
  w.Element.prototype.scrollIntoView = () => {};
} });
const w = dom.window, d = w.document, tick = () => new Promise((r) => setTimeout(r, 50));
const assert = (c, m) => { if (!c) { console.error("FAIL:", m); process.exit(1); } console.log("ok -", m); };

(async () => {
  await tick();
  const manifest = d.querySelector('link[rel="manifest"]');
  assert(manifest?.getAttribute("href") === "/manifest.webmanifest", "manifest linked");
  assert(manifest.getAttribute("crossorigin") === "use-credentials", "manifest fetched with credentials (auth proxy)");
  assert(d.querySelector('link[rel="apple-touch-icon"]'), "apple-touch-icon present");
  assert(d.querySelectorAll('meta[name="theme-color"]').length === 2, "theme-color for light and dark");

  const banner = d.getElementById("offline");
  assert(banner.hidden && d.getElementById("updated").textContent.startsWith("Updated"), "online: no banner");

  // Fresh data -> service worker answers from cache
  cachedHeader = "2026-01-02T03:04:05.000Z";
  d.getElementById("refresh").click(); await tick();
  assert(!banner.hidden && banner.textContent.includes("cached data"), "offline: banner shown");
  assert(d.getElementById("updated").textContent === "Offline", "offline: header label");
  assert(d.querySelectorAll("#list tbody tr").length === 1, "cached data still rendered");

  // Back online -> automatic reload clears the banner
  cachedHeader = null;
  w.dispatchEvent(new w.Event("online")); await tick();
  assert(banner.hidden, "online event reloads and hides banner");

  // Install button (Chromium beforeinstallprompt flow)
  const btn = d.getElementById("install");
  assert(btn.hidden, "install button hidden by default");
  let prompted = false;
  const ev = new w.Event("beforeinstallprompt", { cancelable: true });
  ev.prompt = () => { prompted = true; };
  ev.userChoice = Promise.resolve({ outcome: "accepted" });
  w.dispatchEvent(ev);
  assert(!btn.hidden && ev.defaultPrevented, "install button shown on beforeinstallprompt");
  btn.click(); await tick();
  assert(prompted && btn.hidden, "click prompts and hides the button");
  console.log("ALL OK");
})();
