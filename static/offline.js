// Offline helper for Ledger — queues expense submissions when offline

const DB_NAME = "ledger-offline";
const STORE = "pending";
const SYNC_TAG = "sync-expenses";

// ---------------- IndexedDB ----------------
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

async function queueExpense(payload, token) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite");
    const req = tx.objectStore(STORE).add({
      payload,
      token,
      queuedAt: Date.now(),
    });
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function countPending() {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readonly");
    const req = tx.objectStore(STORE).count();
    req.onsuccess = () => resolve(req.result || 0);
    req.onerror = () => reject(req.error);
  });
}

// ---------------- UI helpers ----------------

function ensureOfflineBanner() {
  let el = document.getElementById("offlineBanner");
  if (!el) {
    el = document.createElement("div");
    el.id = "offlineBanner";
    el.className = "alert alert-warning text-center mb-0 rounded-0";
    el.style.cssText = "position: sticky; top: 0; z-index: 1080; display: none;";
    document.body.insertBefore(el, document.body.firstChild);
  }
  return el;
}

function showBanner(message, type = "warning") {
  const el = ensureOfflineBanner();
  el.className = `alert alert-${type} text-center mb-0 rounded-0`;
  el.style.cssText = "position: sticky; top: 0; z-index: 1080;";
  el.textContent = message;
}

function hideBanner() {
  const el = document.getElementById("offlineBanner");
  if (el) el.style.display = "none";
}

async function updateStatusBanner() {
  const pending = await countPending();
  if (!navigator.onLine) {
    showBanner(`📴 You're offline${pending ? ` — ${pending} pending` : ""}`, "warning");
  } else if (pending > 0) {
    showBanner(`⏳ ${pending} pending — will sync shortly...`, "info");
  } else {
    hideBanner();
  }
}

// ---------------- Form interception ----------------

async function handleAddExpenseSubmit(event) {
  // Only intercept if we're offline
  if (navigator.onLine) return;

  event.preventDefault();
  const form = event.target;

  // Build the payload from the form
  const payload = {
    kind: form.querySelector('[name="kind"]').value,
    amount: parseFloat(form.querySelector('[name="amount"]').value),
    category: form.querySelector('[name="category"]').value,
    description: form.querySelector('[name="description"]').value,
    date: form.querySelector('[name="date"]').value,
  };

  // Get the API token — prefer the meta tag, fall back to localStorage
const token = document.querySelector('meta[name="ledger-api-token"]')?.content
             || localStorage.getItem("ledger_api_token");
  if (!token) {
    alert("You're offline and no API token is saved. Please add an API token under /api/tokens first.");
    return;
  }

  await queueExpense(payload, token);
  await registerSync();

  // Redirect back to dashboard
  window.location.href = "/dashboard";
}

async function registerSync() {
  if ("serviceWorker" in navigator && "SyncManager" in window) {
    const reg = await navigator.serviceWorker.ready;
    try {
      await reg.sync.register(SYNC_TAG);
    } catch (e) {
      console.warn("Background sync not registered:", e);
    }
  }
}

// ---------------- Init ----------------

window.addEventListener("load", () => {
  // Intercept the add-expense form
  const form = document.querySelector('form[action="/expenses/add"], form[data-offline-form]');
  if (form) {
    form.addEventListener("submit", handleAddExpenseSubmit);
  }

  // Update banner state periodically
  updateStatusBanner();
  setInterval(updateStatusBanner, 5000);

  window.addEventListener("online", () => {
    updateStatusBanner();
    registerSync();
  });
  window.addEventListener("offline", updateStatusBanner);

  // Listen for SW messages
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.addEventListener("message", (event) => {
      if (event.data?.type === "sync-complete") {
        const { results } = event.data;
        const ok = results.filter((r) => r.ok).length;
        const failed = results.length - ok;
        if (ok) showBanner(`✅ ${ok} expense(s) synced`, "success");
        if (failed) showBanner(`⚠️ ${failed} failed to sync`, "danger");
        setTimeout(() => {
          updateStatusBanner();
          if (ok) window.location.reload();
        }, 2000);
      }
    });
  }
});