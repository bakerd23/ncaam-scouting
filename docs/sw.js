// Service worker: keeps a copy of the site (and the database reads it has made) on the
// device, so after one online visit the pages still open with no signal - and the report
// form's offline outbox (app.js) can take it from there. Registered by app.js.
//
// Everything is network-first: online, you always get the live site and live data (and the
// saved copy is refreshed); the saved copy is only used when the network fails. Site files
// (pages, css, js) also fall back to it when the network is too slow, so a weak signal
// doesn't hang the page - but database reads never do: a saved copy of flags or reports can
// be out of date, and showing that while online looks like a change didn't save. iPhones
// clear this after ~7 days without a visit unless the site is on the home screen.
//
// Bump CACHE when this file's caching logic changes (old caches are deleted on activate).
const CACHE = "scouting-v3";
const SUPABASE_JS = "https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/dist/umd/supabase.js";
const SHELL = ["./", "index.html", "player.html", "report.html", "login.html", "style.css", "app.js", "config.js", SUPABASE_JS];
// On a weak signal, wait this long for a site file before falling back to the saved copy.
const SLOW_MS = 4000;

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) =>
      // One at a time so a single failure (e.g. the CDN) doesn't block the rest.
      Promise.all(SHELL.map((url) => cache.add(url).catch((err) => console.warn("sw: couldn't cache", url, err))))
    )
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return; // report inserts/edits always go straight to the network
  const url = new URL(req.url);
  const isPage = req.mode === "navigate";
  const isOwnFile = url.origin === self.location.origin;
  // Database reads only (players, reports, career stats, splits) - not auth or RPC calls.
  const isDbRead = url.pathname.startsWith("/rest/v1/") && !url.pathname.startsWith("/rest/v1/rpc/");
  if (!(isPage || isOwnFile || isDbRead || req.url === SUPABASE_JS)) return;
  event.respondWith(networkFirst(event, req, isPage, !isDbRead));
});

// The first page of a counted query (fetchAllPages) comes back 206 Partial Content, which the
// Cache API refuses to store - so save it as a 200 with the same body and headers (the total
// count rides in its Content-Range header).
async function saveCopy(cache, key, res) {
  if (res.status === 206) {
    res = new Response(await res.blob(), { status: 200, statusText: "OK", headers: res.headers });
  }
  return cache.put(key, res);
}

async function networkFirst(event, req, isPage, fallBackWhenSlow) {
  const cache = await caches.open(CACHE);
  // A page is the same file whatever its ?id= is; data requests differ by their query string.
  const cached = await cache.match(req, { ignoreSearch: isPage, ignoreVary: true });

  const network = fetch(req).then((res) => {
    if (res.ok) {
      event.waitUntil(saveCopy(cache, isPage ? new URL(req.url).pathname : req, res.clone()));
    }
    return res;
  });

  if (!cached) return network; // nothing saved - the page's own error handling takes over
  network.catch(() => {}); // a failure here is answered from the cache below
  const slow = fallBackWhenSlow ? [new Promise((resolve) => setTimeout(() => resolve(cached), SLOW_MS))] : [];
  try {
    const res = await Promise.race([network, ...slow]);
    return res.ok || res === cached ? res : cached;
  } catch (e) {
    return cached;
  }
}
