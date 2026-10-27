"use strict";
// Executable assets and navigation always use the network so upgrades cannot mix builds.
// Only public icons are cached; credential-bearing URLs never enter the cache.
const SHELL_CACHE = "rsi-shell-icons-v2";
const SHELL_ASSETS = new Set(["/favicon.svg", "/icon-192.png", "/icon-512.png"]);

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) =>
  event.waitUntil(
    (async () => {
      const names = await caches.keys();
      await Promise.all(names.filter((name) => name.startsWith("rsi-shell-") && name !== SHELL_CACHE).map((name) => caches.delete(name)));
      await self.clients.claim();
    })(),
  ),
);

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.search || url.hash || !SHELL_ASSETS.has(url.pathname)) return;
  event.respondWith(
    (async () => {
      const cache = await caches.open(SHELL_CACHE);
      const cached = await cache.match(request);
      const network = fetch(request)
        .then((response) => {
          if (response && response.ok && !response.redirected) event.waitUntil(cache.put(request, response.clone()));
          return response;
        })
        .catch(() => cached);
      return cached || network;
    })(),
  );
});
