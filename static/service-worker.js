/* ============================================================================
   Vantage service worker
   Served from /service-worker.js (see the Flask route in app/routes/views.py
   which adds Service-Worker-Allowed:/ so this SW controls the whole origin).
   Bumping CACHE_NAME here purges the legacy 'tiktok-finder-v1' caches
   (and any earlier 'vantage-v*' snapshots) on the next activate.
   ============================================================================ */
const CACHE_NAME = 'vantage-v2';

// Keep this list tight — every asset here is fetched on install and will
// block the service worker's `installed` transition if any URL 404s.
const PRECACHE_ASSETS = [
  '/offline',
  '/static/css/main.css',
  '/static/js/main.js',
  '/static/js/dashboard.js',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/img/avatar-placeholder.png',
];

const NAV_TIMEOUT_MS = 3000;

// URL patterns that should NEVER hit the cache — either because they're
// personalized (API responses, admin pages) or because their URLs expire
// (TikTok + ByteImg CDNs per the handover doc).
const NEVER_CACHE_PATHS = [/^\/api\//, /^\/app\/admin\//];
const NEVER_CACHE_HOSTS = [
  'tiktokcdn.com', 'tiktokcdn-us.com', 'ibyteimg.com',
];

// Stale-while-revalidate routes. Cache first for instant paint, refresh in
// the background so the next navigation sees the latest CSS/JS.
const SWR_PATH_PREFIXES = [
  '/static/css/', '/static/js/', '/static/img/', '/static/icons/',
];

// ---------- install ----------------------------------------------------------
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(PRECACHE_ASSETS))
      // If a single asset 404s we still want to activate; log and carry.
      .catch((err) => console.warn('[SW] precache failed:', err))
      .then(() => self.skipWaiting())
  );
});

// ---------- activate: purge old caches + claim clients ----------------------
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(
        names
          .filter((name) => name !== CACHE_NAME)
          .map((name) => caches.delete(name))
      )
    ).then(() => self.clients.claim())
  );
});

// ---------- message: SKIP_WAITING trigger from the update toast -------------
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SKIP_WAITING') self.skipWaiting();
});

// ---------- fetch router ----------------------------------------------------
self.addEventListener('fetch', (event) => {
  const req = event.request;

  // Pass through anything non-GET (POSTs, webhooks, CSRF-bearing forms).
  if (req.method !== 'GET') return;

  const url = new URL(req.url);

  // Cross-origin handling. TikTok + ByteImg CDN URLs expire, so we never
  // cache them — let the browser fetch fresh each time. Other cross-origin
  // GETs also pass through unchanged.
  if (url.origin !== self.location.origin) {
    if (NEVER_CACHE_HOSTS.some((h) => url.host.endsWith(h))) return;
    return;
  }

  // Same-origin but never-cache paths (API, admin).
  if (NEVER_CACHE_PATHS.some((rx) => rx.test(url.pathname))) return;

  // HTML navigations: network-first with timeout, fall back to cache, then /offline.
  const acceptsHTML =
    req.mode === 'navigate' ||
    (req.headers.get('accept') || '').includes('text/html');
  if (acceptsHTML) {
    event.respondWith(navigationHandler(req));
    return;
  }

  // Static assets: stale-while-revalidate
  if (SWR_PATH_PREFIXES.some((p) => url.pathname.startsWith(p))) {
    event.respondWith(staleWhileRevalidate(req));
    return;
  }

  // Default for anything else same-origin: network-first, fallback to cache,
  // no offline fallback (don't serve an HTML offline page for an image etc.).
  event.respondWith(networkFirstThenCache(req));
});

// ---------- handlers --------------------------------------------------------
function navigationHandler(req) {
  return fetchWithTimeout(req, NAV_TIMEOUT_MS)
    .then((resp) => {
      if (resp && resp.ok) {
        const copy = resp.clone();
        caches.open(CACHE_NAME).then((c) => c.put(req, copy)).catch(() => {});
      }
      return resp;
    })
    .catch(() =>
      caches.match(req).then((cached) => cached || caches.match('/offline'))
    );
}

function staleWhileRevalidate(req) {
  return caches.match(req).then((cached) => {
    const networkFetch = fetch(req)
      .then((resp) => {
        if (resp && resp.ok) {
          const copy = resp.clone();
          caches.open(CACHE_NAME).then((c) => c.put(req, copy)).catch(() => {});
        }
        return resp;
      })
      .catch(() => cached);
    return cached || networkFetch;
  });
}

function networkFirstThenCache(req) {
  return fetch(req)
    .then((resp) => {
      if (resp && resp.ok) {
        const copy = resp.clone();
        caches.open(CACHE_NAME).then((c) => c.put(req, copy)).catch(() => {});
      }
      return resp;
    })
    .catch(() => caches.match(req));
}

function fetchWithTimeout(req, ms) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error('nav-timeout')), ms);
    fetch(req).then(
      (resp) => { clearTimeout(t); resolve(resp); },
      (err) => { clearTimeout(t); reject(err); }
    );
  });
}
