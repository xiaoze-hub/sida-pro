// PanWatch Service Worker
const CACHE_NAME = 'panwatch-v0.2.65.2-bust';

// 需要缓存的静态资源
// 注意: 不缓存 '/' (index.html) —— 每次发版 HTML 都变, 缓存旧 HTML 会导致
// 旧 hash 资源 404 → 白屏。SW 只缓存不可变的静态资源。
const STATIC_ASSETS = [
  '/manifest.json',
  '/icon-192.png',
  '/icon-512.png',
];

// 安装时缓存静态资源
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(STATIC_ASSETS);
    })
  );
  // 立即激活
  self.skipWaiting();
});

// 激活时清理旧缓存
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames
          .filter((name) => name !== CACHE_NAME)
          .map((name) => caches.delete(name))
      );
    })
  );
  // 立即接管所有客户端
  self.clients.claim();
});

// 网络优先策略（适合实时数据应用）
self.addEventListener('fetch', (event) => {
  // 只处理同源请求
  if (!event.request.url.startsWith(self.location.origin)) {
    return;
  }

  // API 请求不缓存，直接走网络
  if (event.request.url.includes('/api/')) {
    return;
  }

  // HTML 文档请求走网络且不写入缓存(发版后 index.html 变化, 缓存旧 HTML 会白屏)
  const reqInit = event.request.mode === 'navigate';
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        // 跳过 /api/* 响应缓存(避免旧 HTML fallback 缓存导致新版 JSON 被替换)
        const isApi = event.request.url.includes('/api/');
        if (response.ok && !reqInit && !isApi) {
          const responseClone = response.clone();
          caches.open(CACHE_NAME).then((cache) => {
            cache.put(event.request, responseClone);
          });
        }
        return response;
      })
      .catch(() => {
        // 网络失败: HTML 导航直接失败(不回退旧 HTML), 静态资源尝试缓存
        if (reqInit) return Response.error();
        return caches.match(event.request);
      })
  );
});

// 点击电脑系统通知时，聚焦已打开的 PanWatch 窗口或打开新窗口。
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const targetUrl = new URL(event.notification.data?.url || '/', self.location.origin).href;
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(async (clients) => {
      for (const client of clients) {
        if ('focus' in client) {
          if ('navigate' in client) await client.navigate(targetUrl);
          return client.focus();
        }
      }
      return self.clients.openWindow ? self.clients.openWindow(targetUrl) : undefined;
    })
  );
});
