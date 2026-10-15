// Motion helpers over the Web Animations API, using the spring tokens in tokens.css.
// Under reduced motion every animation becomes a short fade (or nothing), never a move.

const root = () => getComputedStyle(document.documentElement);

export const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

export function spring(name) {
  return root().getPropertyValue(`--spring-${name}`).trim() || "cubic-bezier(.16, 1, .3, 1)";
}

export function duration(name) {
  return parseFloat(root().getPropertyValue(`--dur-${name}`)) || 400;
}

const DONE = { finished: Promise.resolve(), cancel() {} };

/**
 * animate(el, keyframes, { spring, duration, easing, delay, fill, calm })
 *   spring: "snappy" | "smooth" | "bouncy" | "gentle" (sets duration and easing)
 *   calm:   keyframes for reduced motion (default: fade in if the first frame is hidden)
 */
export function animate(el, keyframes, opts = {}) {
  if (!el?.animate) return DONE;
  const { delay = 0, fill = "both" } = opts;
  if (reduced()) {
    const calm = opts.calm !== undefined ? opts.calm
      : keyframes[0]?.opacity === 0 ? [{ opacity: 0 }, { opacity: 1 }] : null;
    return calm ? el.animate(calm, { duration: 150, fill }) : DONE;
  }
  return el.animate(keyframes, {
    duration: opts.duration ?? (opts.spring ? duration(opts.spring) : 300),
    easing: opts.easing ?? (opts.spring ? spring(opts.spring) : "cubic-bezier(.16, 1, .3, 1)"),
    delay, fill,
  });
}

/** Elements enter one after another (opacity and a small rise), capped at 8 in a row. */
export function stagger(els, { step = 30, rise = 10, spring: s = "smooth", delay = 0 } = {}) {
  [...els].forEach((el, i) => animate(el, [{ opacity: 0, transform: `translateY(${rise}px)` }, { opacity: 1, transform: "none" }],
    { spring: s, delay: delay + Math.min(i, 8) * step }));
}

/** FLIP: measure, change the DOM, then play each element from where it was. */
export function flip(els, mutate, { spring: s = "snappy" } = {}) {
  const list = [...els];
  const first = new Map(list.map((el) => [el, el.getBoundingClientRect()]));
  mutate();
  if (reduced()) return;
  for (const el of list) {
    if (!el.isConnected) continue;
    const a = first.get(el), b = el.getBoundingClientRect();
    const dx = a.left - b.left, dy = a.top - b.top;
    if (Math.abs(dx) < 1 && Math.abs(dy) < 1) continue;
    animate(el, [{ transform: `translate(${dx}px, ${dy}px)` }, { transform: "none" }], { spring: s, fill: "none" });
  }
}

/** FLIP across a re-render: elements are matched by data-id, since the render replaces the
 *  nodes. Rows that stayed move from their old place; new rows fade in. */
export function flipById(container, paint, { spring: s = "snappy" } = {}) {
  const before = new Map([...container.querySelectorAll("[data-id]")].map((el) => [el.dataset.id, el.getBoundingClientRect()]));
  paint();
  if (reduced() || !before.size) return;
  for (const el of container.querySelectorAll("[data-id]")) {
    const a = before.get(el.dataset.id);
    if (!a) {
      animate(el, [{ opacity: 0 }, { opacity: 1 }], { duration: 220, fill: "none" });
      continue;
    }
    const b = el.getBoundingClientRect();
    const dx = a.left - b.left, dy = a.top - b.top;
    if (Math.abs(dx) < 1 && Math.abs(dy) < 1) continue;
    animate(el, [{ transform: `translate(${dx}px, ${dy}px)` }, { transform: "none" }], { spring: s, fill: "none" });
  }
}

/** A count that rolls to its new value digit by digit, like an odometer (the heart count).
 *  Updated in place; an empty value ("" or 0) shows nothing. */
export function odometer(el, value, { empty = "" } = {}) {
  if (!el) return;
  const text = value ? String(value) : "";
  if (el.dataset.value === text && el.dataset.value !== undefined) return;
  el.dataset.value = text;
  if (!text) {
    el.textContent = empty;
    return;
  }
  if (!el.querySelector(".odo")) el.textContent = "";
  el.setAttribute("aria-label", text);
  const digits = [...text];
  while (el.children.length < digits.length) {
    const col = document.createElement("span");
    col.className = "odo";
    col.setAttribute("aria-hidden", "true");
    const strip = document.createElement("span");
    for (let d = 0; d <= 9; d++) {
      const s = document.createElement("span");
      s.textContent = String(d);
      strip.append(s);
    }
    col.append(strip);
    el.prepend(col);
  }
  while (el.children.length > digits.length) el.firstElementChild.remove();
  digits.forEach((d, i) => el.children[i].style.setProperty("--d", d));
}

// ---- drag down to dismiss (sheets, Now playing) -----------------------------------------------

/**
 * dragToDismiss(el, { handle, onDrag, onDismiss, enabled })
 *   el follows the finger down from a handle (and rubber-bands upward). Let go past 120 px
 *   or on a flick faster than 0.5 px/ms and onDismiss runs; otherwise it springs back.
 *   handle(e): whether this pointerdown starts a drag. onDrag(0..1): how far down it is.
 *   While dragging el has .dragging (CSS turns its transition off).
 */
export function dragToDismiss(el, { handle, onDrag = () => {}, onDismiss, enabled = () => true }) {
  let drag = null;
  el.addEventListener("pointerdown", (e) => {
    if (!enabled() || !handle(e)) return;
    drag = { y0: e.clientY, last: e.clientY, t: performance.now(), v: 0, dy: 0 };
    el.setPointerCapture(e.pointerId);
    el.classList.add("dragging");
  });
  el.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const now = performance.now();
    drag.v = (e.clientY - drag.last) / Math.max(1, now - drag.t);
    drag.last = e.clientY;
    drag.t = now;
    drag.dy = e.clientY - drag.y0;
    const y = drag.dy > 0 ? drag.dy : -Math.pow(-drag.dy, 0.7);
    el.style.transform = `translateY(${y}px)`;
    onDrag(Math.max(0, Math.min(1, drag.dy / el.offsetHeight)));
  });
  const end = () => {
    if (!drag) return;
    const { dy, v } = drag;
    drag = null;
    el.classList.remove("dragging");
    const dismiss = dy > 120 || v > 0.5;
    if (!dismiss) el.style.transform = "";
    onDrag(0);
    if (dismiss) onDismiss(dy);
  };
  el.addEventListener("pointerup", end);
  el.addEventListener("pointercancel", end);
}

// ---- the segmented control's indicator ------------------------------------------------------
// The raised pill under the selected option slides to the new one. The controls are re-rendered
// on every pick, so where the pill last was is remembered per control (key).
const spots = new Map();
const spotOf = (on) => ({ x: on.offsetLeft, y: on.offsetTop, w: on.offsetWidth, h: on.offsetHeight });
const place = (ind, s) => {
  ind.style.transform = `translate(${s.x}px, ${s.y}px)`;
  ind.style.width = `${s.w}px`;
  ind.style.height = `${s.h}px`;
};
const selected = (group) => group.querySelector(':scope > [aria-selected="true"], :scope > [aria-pressed="true"]');

export function slideIndicator(group, key) {
  if (!group) return;
  let ind = group.querySelector(":scope > .seg-ind");
  if (!ind) {
    ind = document.createElement("span");
    ind.className = "seg-ind";
    ind.setAttribute("aria-hidden", "true");
    group.prepend(ind);
  }
  group.classList.add("has-ind");
  group.dataset.indKey = key;
  const on = selected(group);
  ind.hidden = !on;
  if (!on) return;
  const to = spotOf(on);
  const from = spots.get(key);
  spots.set(key, to);
  if (!from || reduced() || (from.x === to.x && from.y === to.y && from.w === to.w)) return place(ind, to);
  ind.style.transition = "none";
  place(ind, from);
  void ind.offsetWidth;
  ind.style.transition = "";
  place(ind, to);
}

// A rotated phone or a resized window moves the options: put each pill back under its option.
addEventListener("resize", () => {
  for (const group of document.querySelectorAll(".has-ind")) {
    const on = selected(group), ind = group.querySelector(":scope > .seg-ind");
    if (!on || !ind) continue;
    ind.style.transition = "none";
    place(ind, spotOf(on));
    spots.set(group.dataset.indKey, spotOf(on));
    requestAnimationFrame(() => { ind.style.transition = ""; });
  }
});

/** Restart a CSS animation class (e.g. the tally's ignite flicker). */
export function replayClass(el, cls) {
  if (!el) return;
  el.classList.remove(cls);
  void el.offsetWidth;
  el.classList.add(cls);
}
