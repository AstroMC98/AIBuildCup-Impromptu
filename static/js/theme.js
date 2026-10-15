// Every episode is tinted by its vibe's accent color. The board derives the
// backgrounds by mixing the accent into the base ink: deep = 72% ink, ring = 45%.
export const INK = "#17122A";

export function mix(a, b, t) {
  const p = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
  const A = p(a), B = p(b);
  return "#" + A.map((v, i) => Math.round(v * (1 - t) + B[i] * t).toString(16).padStart(2, "0")).join("");
}

export function applyAccent(el, accent) {
  const a = /^#[0-9a-f]{6}$/i.test(accent || "") ? accent : "#B8336A";
  const deep = mix(a, INK, 0.72);
  el.style.setProperty("--accent", a);
  el.style.setProperty("--deep", deep);
  el.style.setProperty("--ring", mix(a, INK, 0.45));
  el.style.setProperty("--dim", mix(deep, "#000000", 0.45));
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta && el === document.documentElement) meta.content = deep;
}

// Stable avatar colors for callers, picked by name.
const CALLER = ["#6A5A9C", "#2F7F72", "#8A6A12", "#2E6F9E", "#6A4FB0", "#9C4A5A", "#3F7A3A"];
export function callerColor(name) {
  let h = 0;
  for (const ch of String(name || "?")) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return CALLER[h % CALLER.length];
}

export function initial(name) {
  const s = String(name || "").trim();
  return s ? s[0].toUpperCase() : "?";
}
