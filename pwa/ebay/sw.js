// eBay QuickList Service Worker — Offline Draft Support
const CACHE_NAME = 'ebay-quicklist-v1';
const STATIC_ASSETS = [
    '/ebay',
    '/ebay/login',
    '/ebay/new',
    '/ebay/listings',
    '/ebay/stats',
    '/ebay/settings',
];

// Install — cache core pages
self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_NAME).then(cache => cache.addAll(STATIC_ASSETS))
    );
    self.skipWaiting();
});

// Activate — clean old caches
self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys().then(keys =>
            Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
        )
    );
    self.clients.claim();
});

// Fetch — network first, fall back to cache
self.addEventListener('fetch', event => {
    if (event.request.method !== 'GET') return;
    event.respondWith(
        fetch(event.request)
            .then(resp => {
                const clone = resp.clone();
                caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
                return resp;
            })
            .catch(() => caches.match(event.request))
    );
});
