// Going live. The button becomes the studio: it grows to fill the screen, its red dot flies
// to the middle of a studio clock, the clock counts three seconds in while the server starts
// the show, and the dot opens into the ON AIR lamp. The caller then navigates, and the stage
// floods out from the lamp. If the server is slower than the count, the clock chases on
// "Standing by": it never says on air before it is. Under reduced motion the button just
// says "Starting…".
//
// The overlay lives inside #app, so the route swap that opens the stage removes it, and the
// view transition's "before" picture is the lit lamp.
import { html, setHtml } from "./html.js";
import { animate, reduced, spring } from "./motion.js";
import { fillRing, ring } from "./ring.js";
import { navigate } from "./router.js";
import { setWorld } from "./theme.js";

const COUNT = 3;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

/**
 * countIn(button, start, { gel }): runs start() (the API call) behind the count-in.
 * Resolves with start()'s result once the lamp is lit; then call goStage(path) (also
 * fine under reduced motion, where there's no overlay and no transition).
 * If start() fails, the studio folds back into the button and the error is thrown.
 */
export async function countIn(button, start, { gel = "" } = {}) {
  const job = Promise.resolve().then(start);
  let failed = false;
  job.catch(() => { failed = true; });   // a refusal stops the count at once (and is never left unhandled)
  if (reduced()) {
    const label = [...button.childNodes];
    button.textContent = "Starting…";
    try {
      return await job;
    } catch (err) {
      button.replaceChildren(...label);
      throw err;
    }
  }

  const app = document.getElementById("app");
  const world = document.documentElement.dataset.world;
  const box = button.getBoundingClientRect();
  const W = innerWidth, H = innerHeight;
  const el = document.createElement("div");
  el.className = "golive stage";
  if (gel) el.style.setProperty("--gel", gel);
  setHtml(el, html`
    <div class="gl-clock">${ring({ cls: "gl-ring", r: 46, dot: 1.9 })}<span class="gl-num" aria-hidden="true"></span>
      <span class="gl-lamp"><span>On air</span></span></div>
    <p class="gl-cap" role="status">Standing by</p>`);
  app.append(el);
  const ringEl = el.querySelector(".gl-ring");
  const num = el.querySelector(".gl-num");
  const lamp = el.querySelector(".gl-lamp");
  const cap = el.querySelector(".gl-cap");
  const alive = () => el.isConnected;

  // The button grows into the studio (a container transform on the clip). Once it covers the
  // screen, the page itself goes to the stage (dark scrollbars, the phone's status bar).
  const r = `${Math.min(box.height / 2, 999)}px`;
  const from = `inset(${box.top}px ${W - box.right}px ${H - box.bottom}px ${box.left}px round ${r})`;
  animate(el, [{ clipPath: from }, { clipPath: "inset(0 0 0 0 round 0px)" }], { duration: 460, easing: "cubic-bezier(.65, 0, .35, 1)" })
    .finished.then(() => { if (alive()) setWorld("stage"); }).catch(() => {});

  // Its red dot flies to the middle of the clock and dims, ready to be the lamp.
  const dot = button.querySelector(".rec-dot")?.getBoundingClientRect();
  lamp.style.opacity = "0";
  if (dot) {
    const ghost = document.createElement("span");
    ghost.className = "gl-ghost";
    ghost.style.cssText = `left:${dot.left}px;top:${dot.top}px;width:${dot.width}px;height:${dot.height}px`;
    el.append(ghost);
    await animate(ghost, [{ transform: "none" }, { transform: `translate(${W / 2 - dot.left - dot.width / 2}px, ${H / 2 - dot.top - dot.height / 2}px)`, background: "#3A1512" }],
      { duration: 560, easing: spring("smooth"), fill: "forwards" }).finished.catch(() => {});
    ghost.remove();
  } else {
    await wait(460);
  }
  lamp.style.opacity = "";

  // Three seconds on the clock: 60 dots, one numeral a second narrowing as it lands.
  const t0 = performance.now();
  await new Promise((done) => {
    let shown = 0;
    const frame = () => {
      if (!alive() || failed) return done();
      const e = (performance.now() - t0) / 1000;
      fillRing(ringEl, Math.min(60, e * (60 / COUNT)));
      const n = COUNT - Math.floor(e);
      if (e < COUNT && n !== shown) {
        shown = n;
        num.textContent = String(n);
        animate(num, [{ fontStretch: "150%", opacity: 0, transform: "scale(.82)" }, { fontStretch: "70%", opacity: 1, offset: 0.7 },
          { fontStretch: "62%", opacity: 1, transform: "none" }], { duration: 900, easing: spring("smooth") });
      }
      if (e >= COUNT) return done();
      requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
  });
  num.textContent = "";

  // Slower than the count: the clock chases until the server says the show has started.
  const late = setTimeout(() => {
    if (!alive() || failed) return;
    cap.textContent = "Standing by. The hosts are getting ready.";
    fillRing(ringEl, 0);
    ringEl.querySelectorAll("circle").forEach((c, i) => c.style.setProperty("--i", i));
    ringEl.classList.add("chase");
  }, 60);
  let res;
  try {
    res = await job;
  } catch (err) {
    clearTimeout(late);
    await fold(el, from, world);
    throw err;
  }
  clearTimeout(late);
  if (!alive()) return res;
  ringEl.classList.remove("chase");
  fillRing(ringEl, 60);

  // The dot opens into the ON AIR lamp, which flickers on.
  cap.textContent = "";
  lamp.classList.add("open");
  await wait(620);
  return res;
}

/** Into the stage, flooding out from the lit lamp. */
export function goStage(path, { replace = false } = {}) {
  const lamp = document.querySelector(".golive .gl-lamp");
  if (lamp) {
    const b = lamp.getBoundingClientRect();
    const s = document.documentElement.style;
    s.setProperty("--vt-x", `${b.left + b.width / 2}px`);
    s.setProperty("--vt-y", `${b.top + b.height / 2}px`);
  }
  navigate(path, { replace, vt: "to-stage" });
}

async function fold(el, to, world) {
  setWorld(world || "house");
  await animate(el, [{ clipPath: "inset(0 0 0 0 round 0px)", opacity: 1 }, { clipPath: to, opacity: 0 }],
    { duration: 380, easing: "cubic-bezier(.65, 0, .35, 1)" }).finished.catch(() => {});
  el.remove();
}
