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

/** Restart a CSS animation class (e.g. the tally's ignite flicker). */
export function replayClass(el, cls) {
  if (!el) return;
  el.classList.remove(cls);
  void el.offsetWidth;
  el.classList.add(cls);
}
