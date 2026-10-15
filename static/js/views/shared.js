// Behaviour both show layouts share: the action map, the call-in form, the
// "Tune in" overlay and floating hearts.
import { html } from "../html.js";
import { icon } from "../icons.js";
import {
  downloadTranscript, openContextSheet, openHostsSheet, openMoreSheet, openRestyleSheet, openShareSheet,
} from "../sheets.js";
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

export function tuneInOverlay(ctl) {
  const s = ctl.store;
  return html`
    <div class="tune-card">
      <span class="badge live"><span class="dot"></span>Live now</span>
      <p class="tune-title">${s.show.title || "A live show"}</p>
      <p class="muted">${s.hostNames() ? `With ${s.hostNames()}. ` : ""}Listen in, call in, and the hosts will answer you on air.</p>
      <button class="btn white big block" type="button" data-action="tune-in">${icon("play", 20, 2.4)}Tune in</button>
    </div>`;
}

export function floatHearts(anchor, n) {
  if (!anchor || reducedMotion()) return;
  const rect = anchor.getBoundingClientRect();
  for (let i = 0; i < Math.min(n, 6); i++) {
    const h = document.createElement("span");
    h.className = "float-heart";
    h.innerHTML = String(icon("heart", 22));
    h.style.left = `${rect.left + rect.width / 2 - 11 + (Math.random() * 24 - 12)}px`;
    h.style.top = `${rect.top}px`;
    h.style.animationDelay = `${i * 90}ms`;
    document.body.append(h);
    setTimeout(() => h.remove(), 1800 + i * 90);
  }
}
