// Small helpers shared across views.

export const $ = (sel, el = document) => el.querySelector(sel);
export const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

export function fmtTime(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

export function fmtAgo(epochSeconds) {
  if (!epochSeconds) return "";
  const d = Date.now() / 1000 - epochSeconds;
  if (d < 60) return "just now";
  if (d < 3600) return `${Math.floor(d / 60)} min ago`;
  if (d < 86400) return `${Math.floor(d / 3600)} h ago`;
  return new Date(epochSeconds * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function slug(s) {
  return (s || "episode").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 60) || "episode";
}

export function joinNames(names) {
  const n = names.filter(Boolean);
  if (n.length <= 1) return n[0] || "";
  return `${n.slice(0, -1).join(", ")} and ${n[n.length - 1]}`;
}

// localStorage can throw (private mode, blocked storage); the app works without it.
export const store = {
  get(key, fallback = null) {
    try {
      const v = localStorage.getItem(key);
      return v === null ? fallback : JSON.parse(v);
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* ignore */ }
  },
};

/** A random id this browser keeps, so the owner's stats count an anonymous listener once. */
export function listenerId() {
  let id = store.get("gp_lid");
  if (!id) {
    id = uid();
    store.set("gp_lid", id);
  }
  return id;
}

export function uid() {
  return (crypto.randomUUID?.() || String(Math.random()).slice(2) + Date.now()).replace(/-/g, "").slice(0, 16);
}

export function saveBlob(blob, name) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

export function reducedMotion() {
  return matchMedia("(prefers-reduced-motion: reduce)").matches;
}

let toastTimer = 0;
export function toast(msg, ms = 4500) {
  const t = document.getElementById("toast");
  if (!t) return;
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), ms);
}
