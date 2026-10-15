// Behaviour both show layouts share: the action map, the call-in form, the
// "Tune in" overlay and floating hearts.
import { html } from "../html.js";
import { icon } from "../icons.js";
import {
  downloadTranscript, openContextSheet, openHostsSheet, openInterruptSheet, openMoreSheet, openRestyleSheet,
  openShareSheet,
} from "../sheets.js";
import { ring } from "../ring.js";
import { navigate } from "../router.js";
import { loginPath } from "../session.js";
import { reducedMotion, toast } from "../util.js";

export function showActions(ctl, extra = {}) {
  return {
    "toggle-play": () => ctl.togglePlay(),
    back15: () => ctl.back15(),
    "jump-live": () => ctl.jumpLive(),
    "tune-in": () => ctl.tuneIn(),
    react: (el) => {
      ctl.react(1);
      floatHearts(el, 1);
    },
    "add-context": () => (ctl.canTalk ? openContextSheet(ctl) : navigate(loginPath())),
    restyle: () => openRestyleSheet(ctl),
    interrupt: () => openInterruptSheet(ctl),
    share: () => openShareSheet(ctl),
    hosts: () => openHostsSheet(ctl),
    more: () => openMoreSheet(ctl),
    "download-audio": () => { location.href = ctl.audioDownloadUrl(); },
    "download-transcript": () => downloadTranscript(ctl),
    "remove-comment": (el) => ctl.removeComment(Number(el.dataset.id)),
    "seek-chapter": (el) => ctl.seekTo(Number(el.dataset.start)),
    ...extra,
  };
}

const SEEK_STEP_SECONDS = 10;

/** Click or drag the chapter bar to jump there; arrow keys move 10 s. */
export function bindSeekBar(bar, ctl) {
  if (!bar) return () => {};
  const rate = ctl.store.rate;
  bar.classList.add("seekable");
  bar.removeAttribute("aria-hidden");
  bar.setAttribute("role", "slider");
  bar.setAttribute("tabindex", "0");
  bar.setAttribute("aria-label", "Seek: click or drag to jump, arrow keys move 10 seconds");
  let dragging = false;
  let last = 0;
  const seekAt = (e) => {
    const r = bar.getBoundingClientRect();
    const sample = ctl.store.sampleAtBar((e.clientX - r.left) / r.width);
    ctl.seekTo(Number.isFinite(sample) ? sample : Infinity);
  };
  const down = (e) => {
    dragging = true;
    bar.setPointerCapture?.(e.pointerId);
    seekAt(e);
  };
  const move = (e) => {
    const now = performance.now();
    if (dragging && now - last > 120) {   // the live player reschedules on every seek
      last = now;
      seekAt(e);
    }
  };
  const up = (e) => {
    if (dragging) seekAt(e);
    dragging = false;
  };
  const key = (e) => {
    const step = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (!step) return;
    e.preventDefault();
    ctl.seekTo(Math.max(ctl.heldFrom(), ctl.player.position() + step * SEEK_STEP_SECONDS * rate));
  };
  bar.addEventListener("pointerdown", down);
  bar.addEventListener("pointermove", move);
  bar.addEventListener("pointerup", up);
  bar.addEventListener("pointercancel", () => { dragging = false; });
  bar.addEventListener("keydown", key);
  return () => {
    bar.removeEventListener("pointerdown", down);
    bar.removeEventListener("pointermove", move);
    bar.removeEventListener("pointerup", up);
    bar.removeEventListener("keydown", key);
  };
}

export function callinForm(ctl, { id, variant = "bar" } = {}) {
  if (!ctl.live) return "";
  if (!ctl.canTalk) {
    return html`
      <div class="callin-form ${variant} signed-out" data-callin>
        <a class="btn white grow" href="${loginPath()}" data-link>${icon("chat", 18)}Sign in to call in</a>
      </div>`;
  }
  return html`
    <form class="callin-form ${variant}" data-callin>
      <button type="button" class="round-btn ghost" data-attach aria-label="Attach context to your call-in">${icon("plus", 22, 2.2)}</button>
      <label class="sr-only" for="${id}">Call in to the hosts</label>
      <input id="${id}" name="callin" maxlength="${500}" placeholder="Call in to the hosts" autocomplete="off" enterkeyhint="send">
      <button type="submit" class="round-btn white" aria-label="Send to the hosts">${icon("send", 22)}</button>
    </form>`;
}

export function bindCallin(root, ctl) {
  for (const form of root.querySelectorAll("form[data-callin]")) {
    const input = form.querySelector("input[name=callin]");
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const text = input.value.trim();
      if (!text) return input.focus();
      if (ctl.store.ended) return toast("The pod has ended.");
      if (ctl.sendComment(text)) {
        input.value = "";
        toast("Sent. The hosts will read it on air.");
      }
    });
    form.querySelector("[data-attach]").addEventListener("click", () => {
      openContextSheet(ctl, {
        callin: input.value.trim(),
        onSent: (withCallin) => { if (withCallin) input.value = ""; },
      });
    });
  }
}

export function focusCallin(root) {
  const input = [...root.querySelectorAll("[data-callin] input")].find((i) => i.offsetParent !== null);
  input?.focus();
  input?.scrollIntoView({ block: "center", behavior: reducedMotion() ? "auto" : "smooth" });
}

/** The tap that lets the browser play sound: one big target inside the studio clock. */
export function tuneInOverlay(ctl) {
  const s = ctl.store;
  const at = Math.max(0, (ctl.liveEdge?.() || 0) / s.rate);
  const mins = Math.floor(at / 60);
  return html`
    <div class="tune-card">
      <button class="tune-target" type="button" data-action="tune-in" aria-label="Tune in">
        ${ring({ lit: (i) => i <= Math.floor(at % 60), r: 46, dot: 2.2 })}<span class="tune-play">${icon("play", 34, 2.4)}</span></button>
      <p class="tune-title">Tune in</p>
      <p class="muted">${s.show.title ? html`<strong>${s.show.title}</strong>. ` : ""}${s.hostNames() || "The hosts"} ${at > 20 ? `are ${mins ? `${mins} min` : "a moment"} in` : "are just starting"}. You'll join live, and you can call in.</p>
    </div>`;
}

/** Hearts leave the button on arcs; under reduced motion, one rises and fades. */
export function floatHearts(anchor, n) {
  if (!anchor) return;
  const rect = anchor.getBoundingClientRect();
  const calm = reducedMotion();
  anchor.animate?.([{ transform: "scale(1)" }, { transform: "scale(.86)" }, { transform: "scale(1)" }], { duration: calm ? 1 : 300 });
  const count = calm ? 1 : Math.min(2 + n * 2, 8);
  for (let i = 0; i < count; i++) {
    const h = document.createElement("span");
    h.className = "float-heart";
    h.innerHTML = String(icon("heart", 22));
    h.querySelector("svg")?.setAttribute("fill", "currentColor");
    h.style.left = `${rect.left + rect.width / 2 - 11}px`;
    h.style.top = `${rect.top}px`;
    document.body.append(h);
    const dx = -70 + Math.random() * 70, dy = -170 - Math.random() * 120, rot = -30 + Math.random() * 60;
    const frames = calm
      ? [{ opacity: 1, transform: "none" }, { opacity: 0, transform: "translateY(-70px)" }]
      : [{ opacity: 0, transform: "translate(0, 0) scale(.4)" },
        { opacity: 1, transform: `translate(${dx * 0.3}px, ${dy * 0.25}px) scale(1.1) rotate(${rot * 0.3}deg)`, offset: 0.2 },
        { opacity: 0, transform: `translate(${dx}px, ${dy}px) scale(.9) rotate(${rot}deg)` }];
    h.animate(frames, { duration: calm ? 700 : 900 + Math.random() * 400, delay: i * 70, easing: "cubic-bezier(.2, .7, .3, 1)", fill: "both" })
      .finished.then(() => h.remove(), () => h.remove());
  }
}
