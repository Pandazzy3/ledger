// Ledger Service Worker — caching + background sync

const CACHE_VERSION = "ledger-v3";
const SYNC_TAG = "sync-expenses";
const STATIC_ASSETS = [
  "/",
  "/static/manifest.json",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/favicon.ico",
  "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css",
  "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js",
  "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"
];

// ---------------- INSTALL ----------------
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION)
      .then((cache) => cache.addAll(STATIC_ASSETS))
      .then(() => self.skipWaiting())
  );
});

// ---------------- ACTIVATE ----------------
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

// ---------------- FETCH ----------------
self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // Only cache GET requests
  if (req.method !== "GET") return;

  // Never cache API calls
  if (url.pathname.startsWith("/api/")) return;

  // Static assets: cache-first
  if (url.pathname.startsWith("/static/") ||
      url.hostname.includes("jsdelivr.net")) {
    event.respondWith(
      caches.match(req).then((cached) => cached || fetch(req))
    );
    return;
  }

  // Pages: network-first with cache fallback
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE_VERSION).then((c) => c.put(req, copy));
          return res;
        })
        .catch(() => caches.match(req).then((c) => c || caches.match("/")))
    );
  }
});

// ---------------- BACKGROUND SYNC ----------------
self.addEventListener("sync", (event) => {
  if (event.tag === SYNC_TAG) {
    event.waitUntil(drainQueue());
  }
});

// Drain the pending-expense queue from IndexedDB
async function drainQueue() {
  const pending = await readPending();
  if (!pending.length) return;

  const results = [];
  for (const item of pending) {
    try {
      const res = await fetch("/api/v1/expenses", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Authorization": `Bearer ${item.token}`,
        },
        body: JSON.stringify(item.payload),
      });
      if (res.ok) {
        results.push({ id: item.id, ok: true });
      } else {
        const text = await res.text();
        results.push({ id: item.id, ok: false, error: text });
      }
    } catch (e) {
      results.push({ id: item.id, ok: false, error: String(e) });
    }
  }

  // Remove successful ones
  for (const r of results) {
    if (r.ok) await deletePending(r.id);
  }

  // Notify clients to refresh
  const clients = await self.clients.matchAll({ type: "window" });
  clients.forEach((c) => c.postMessage({ type: "sync-complete", results }));
}

// ---------------- IndexedDB helpers ----------------
const DB_NAME = "ledger-offline";
const STORE = "pending";

function openDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE)) {
        db.createObjectStore(STORE, { keyPath: "id", autoIncrement: true });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function readPending() {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readonly");
    const req = tx.objectStore(STORE).getAll();
    req.onsuccess = () => resolve(req.result || []);
    req.onerror = () => reject(req.error);
  });
}

async function deletePending(id) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite");
    const req = tx.objectStore(STORE).delete(id);
    req.onsuccess = () => resolve();
    req.onerror = () => reject(req.error);
  });
}