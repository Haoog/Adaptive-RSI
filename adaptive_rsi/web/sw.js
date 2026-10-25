"use strict";
// 仅缓存不含用户数据的静态壳（HTML/CSS/JS/图标），采用 stale-while-revalidate；
// /api/ 响应、凭据与项目代码永远不进入离线缓存。
const SHELL_CACHE = "rsi-shell-v1";
const SHELL_ASSETS = /\.(?:html|js|css|svg|png|webmanifest)$/;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) =>
  event.waitUntil(
    (async () => {
      const names = await caches.keys();
      await Promise.all(names.filter((name) => name !== SHELL_CACHE).map((name) => caches.delete(name)));
      await self.clients.claim();
    })(),
  ),
);

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return;
  if (!SHELL_ASSETS.test(url.pathname) && !["/", "/install"].includes(url.pathname)) return;
  event.respondWith(
    (async () => {
      const cache = await caches.open(SHELL_CACHE);
      const cached = await cache.match(request, { ignoreSearch: url.pathname === "/" || url.pathname === "/install" });
      const network = fetch(request)
        .then((response) => {
          if (response && response.ok) cache.put(request, response.clone());
          return response;
        })
        .catch(() => cached);
      return cached || network;
    })(),
  );
});
