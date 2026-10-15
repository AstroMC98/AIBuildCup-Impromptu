// Every episode is lit by a gel: a bright light color derived from its vibe's accent.
// The server sends dark accents (made to be mixed into ink); the stage needs light, so
// known presets get hand-tuned gels and anything else is lifted in OKLCH. CSS derives the
// rest from --gel (tokens.css, .stage). The house/stage "world" sets the browser chrome.
export const HOUSE_GEL = "#FFB257";
const DEFAULT_ACCENT = "#B8336A";

// Preset accents (app/catalog.py) and the gel each one lights the stage with.
const GELS = {
  "#B8336A": "#FF6B9A",   // Girl gossip, Girl talk, Banter
  "#3B3F9E": "#7C83FF",   // Late-night radio, Debate night
  "#B5521C": "#FF8A4C",   // Sports play-by-play, Married couple chat, Debate me
  "#3A4250": "#A3B1CC",   // True crime, Masculine talk
  "#1F7F6A": "#4FD1A5",   // Nature documentary, Science deep-dive
  "#80620F": "#F2C14E",   // Rival professors, Inspirational talk
  "#6A4FB0": "#B69CFF",   // Bedtime story
  "#2E6F9E": "#6FB3FF",   // Morning radio chaos, Idea breakdown, briefings
  "#2F6BD0": "#6FA0FF",   // Interview me
};

const valid = (hex) => /^#[0-9a-f]{6}$/i.test(hex || "");

/** The gel for an accent: hand-tuned for presets, otherwise the accent lifted to a light. */
export function gelFor(accent) {
  const a = (valid(accent) ? accent : DEFAULT_ACCENT).toUpperCase();
  return GELS[a] || lift(a);
}

export function applyAccent(el, accent) {
  const a = valid(accent) ? accent : DEFAULT_ACCENT;
  el.style.setProperty("--accent", a);
  el.style.setProperty("--gel", gelFor(a));
  if (el === document.documentElement) paintChrome();
}

/** "stage" while something is on air (dark, lit by the gel), "house" otherwise. */
export function setWorld(world) {
  document.documentElement.dataset.world = world;
  paintChrome();
}

function paintChrome() {
  const meta = document.querySelector('meta[name="theme-color"]');
  if (!meta) return;
  const root = document.documentElement;
  if (root.dataset.world === "stage") {
    const gel = root.style.getPropertyValue("--gel").trim() || HOUSE_GEL;
    meta.content = mix(gel, "#08090E", 0.81);
  } else {
    meta.content = getComputedStyle(document.body || root).backgroundColor || "#F1F3F6";
  }
}

export function mix(a, b, t) {
  const p = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
  const A = p(a), B = p(b);
  return "#" + A.map((v, i) => Math.round(v * (1 - t) + B[i] * t).toString(16).padStart(2, "0")).join("");
}

// ---- OKLCH lift for custom accents (Björn Ottosson's OKLab) ------------------------------

const toLinear = (c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
const toGamma = (c) => (c <= 0.0031308 ? 12.92 * c : 1.055 * c ** (1 / 2.4) - 0.055);

function hexToOklch(hex) {
  const [r, g, b] = [1, 3, 5].map((i) => toLinear(parseInt(hex.slice(i, i + 2), 16) / 255));
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
  const L = 0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s;
  const A = 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s;
  const B = 0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s;
  return [L, Math.hypot(A, B), Math.atan2(B, A)];
}

function oklchToRgb(L, C, h) {
  const A = C * Math.cos(h), B = C * Math.sin(h);
  const l = (L + 0.3963377774 * A + 0.2158037573 * B) ** 3;
  const m = (L - 0.1055613458 * A - 0.0638541728 * B) ** 3;
  const s = (L - 0.0894841775 * A - 1.2914855480 * B) ** 3;
  return [
    4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
  ];
}

/** Light enough to read as light on the dark stage; chroma eased down until it's in gamut. */
function lift(hex) {
  const [L0, C0, h] = hexToOklch(hex);
  const L = Math.max(L0, 0.74);
  let C = Math.min(C0 * 1.35, 0.2);
  let rgb = oklchToRgb(L, C, h);
  while (C > 0.01 && rgb.some((v) => v < -0.001 || v > 1.001)) {
    C -= 0.01;
    rgb = oklchToRgb(L, C, h);
  }
  return "#" + rgb.map((v) => Math.round(toGamma(Math.min(1, Math.max(0, v))) * 255).toString(16).padStart(2, "0")).join("");
}

// ---- callers --------------------------------------------------------------------------------

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
