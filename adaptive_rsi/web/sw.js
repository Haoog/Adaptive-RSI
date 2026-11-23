"use strict";
// Executable assets and navigation always use the network so upgrades cannot mix builds.
// Only public icons are cached; credential-bearing URLs never enter the cache.
const SHELL_CACHE = "rsi-shell-icons-v3";
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
  const cache = caches.open(SHELL_CACHE).catch(() => null);
  const cached = cache.then((store) => store?.match(request)).catch(() => null);
  const network = fetch(request);
  const refresh = network.then(async (response) => {
    if (response.ok && !response.redirected) {
      const copy = response.clone();
      await (await cache)?.put(request, copy);
    }
  });
  event.waitUntil(refresh.catch(() => {}));
  event.respondWith(cached.then((response) => response || network));
});
