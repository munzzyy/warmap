/* warmap mobile service worker.
 *
 * This only ever runs where the page is a secure context (https, or localhost).
 * Over a plain-HTTP LAN address the app skips registration entirely and
 * relies on its IndexedDB copy instead. See app.js's boot().
 *
 * Two jobs:
 *   1. Keep the app shell so warmap opens with the PC off or the wifi gone.
 *   2. Keep map tiles as they are fetched, so ground you have already looked
 *      at is still drawn when you are offline. Tiles are cached as you pan and
 *      never pre-fetched in bulk: OpenStreetMap's tile policy explicitly
 *      forbids "download an area for offline use", and the desktop proxy this
 *      talks to is subject to the same rule.
 *
 * The data itself is NOT cached here. It lives in IndexedDB, written by the
 * app after each sync, because it needs to be queried and re-rendered rather
 * than replayed as an HTTP response.
 */

var SHELL_CACHE = "warmap-shell-v1";
var TILE_CACHE = "warmap-tiles-v1";

// Kept small and explicit. Everything here is same-origin and served by the
// desktop; a miss just falls through to the network.
var SHELL = [
  "",
  "index.html",
  "app.css",
  "app.js",
  "map.js",
  "map.css",
  "manifest.webmanifest",
  "vendor/leaflet/leaflet.js",
  "vendor/leaflet/leaflet.css",
  "vendor/leaflet.markercluster/leaflet.markercluster.js",
  "vendor/leaflet.markercluster/MarkerCluster.css",
  "vendor/leaflet.markercluster/MarkerCluster.Default.css",
  "vendor/leaflet.heat/leaflet-heat.js",
];

// How many tiles to keep. Roughly a few hundred MB at the top end; trimmed
// oldest-first so a long drive can't grow the cache without limit.
var TILE_LIMIT = 3000;

self.addEventListener("install", function (event) {
  var base = self.registration.scope;
  event.waitUntil(
    caches.open(SHELL_CACHE).then(function (cache) {
      // addAll fails the whole install if any single request 404s, which would
      // leave the app with no shell at all. Add them individually instead and
      // accept a partial shell over none.
      return Promise.all(SHELL.map(function (path) {
        return cache.add(new Request(base + path, { cache: "reload" }))
          .catch(function () { /* one missing asset must not sink install */ });
      }));
    }).then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (names) {
      return Promise.all(names.map(function (name) {
        if (name !== SHELL_CACHE && name !== TILE_CACHE) {
          return caches.delete(name);
        }
      }));
    }).then(function () { return self.clients.claim(); })
  );
});

function trimCache(name, limit) {
  return caches.open(name).then(function (cache) {
    return cache.keys().then(function (keys) {
      if (keys.length <= limit) { return; }
      return Promise.all(keys.slice(0, keys.length - limit).map(function (k) {
        return cache.delete(k);
      }));
    });
  });
}

self.addEventListener("fetch", function (event) {
  var req = event.request;
  if (req.method !== "GET") { return; }

  var url = new URL(req.url);
  if (url.origin !== self.location.origin) { return; }

  // Data endpoints always go to the network. A stale cached response here
  // would quietly show yesterday's captures as if they were current; the
  // app's own IndexedDB copy is the offline story, and it knows it is a copy.
  if (url.pathname.indexOf("/api/") !== -1) { return; }

  // Tiles: cache first, then network, then whatever we have.
  if (url.pathname.indexOf("/tiles/") !== -1) {
    event.respondWith(
      caches.open(TILE_CACHE).then(function (cache) {
        return cache.match(req).then(function (hit) {
          if (hit) { return hit; }
          return fetch(req).then(function (resp) {
            if (resp && resp.status === 200) {
              cache.put(req, resp.clone());
              trimCache(TILE_CACHE, TILE_LIMIT);
            }
            return resp;
          });
        });
      })
    );
    return;
  }

  // App shell: network first so an updated warmap reaches the phone, falling
  // back to the cached copy when the PC is unreachable.
  event.respondWith(
    fetch(req).then(function (resp) {
      if (resp && resp.status === 200) {
        var copy = resp.clone();
        caches.open(SHELL_CACHE).then(function (c) { c.put(req, copy); });
      }
      return resp;
    }).catch(function () {
      return caches.match(req).then(function (hit) {
        return hit || caches.match(self.registration.scope);
      });
    })
  );
});
