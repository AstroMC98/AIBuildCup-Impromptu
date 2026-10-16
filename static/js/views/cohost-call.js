// /cohost/{id} — on the call (the live screen's design language): your co-host's
// avatar pulsing as they talk, big captions of what you both just said, the mode,
// mute and Hang up. After hanging up it shows the episode being edited, then opens it.
import { api } from "../api.js";
import { avatar, loader } from "../components.js";
import { startCallAudio, takeHandoff } from "../cohost/audio.js";
import { CallController } from "../cohost/controller.js";
import { html, setHtml } from "../html.js";
import { icon } from "../icons.js";
import { fillRing, ring, secondsLit } from "../ring.js";
import { navigate } from "../router.js";
import { loginPath, session } from "../session.js";
import { applyAccent } from "../theme.js";
import { Regions, bindActions } from "../ui.js";
import { fmtTime, store as local } from "../util.js";

const FRAME_MS = 80;
const STEPS = [
  ["listening", "Listening back"],
  ["notes", "Writing the title, show notes and chapters"],
  ["cutting", "Cutting flubs, crosstalk and dead air"],
  ["mastering", "Levelling and mastering"],
];
const TERMINAL_TEXT = {
  4403: "This call isn't yours.",
  4404: "This call doesn't exist any more.",
  4410: "You picked this call up somewhere else (another tab or device).",
};

export async function cohostCallView(root, { params }) {
  const id = params[0];
  if (!session.signedIn) {
    navigate(loginPath(), { replace: true });
    return null;
  }
  setHtml(root, loader("Dialing in…"));
  let data;
  try {
    data = await api.getShow(id);
  } catch (err) {
    if (err.status === 401) {
      navigate(loginPath(), { replace: true });
      return null;
    }
    return message(root, err.status === 404 ? "This call doesn't exist any more." : err.message);
  }
  if (!data.call) {               // it's an episode now
    navigate(`/show/${id}`, { replace: true });
    return null;
  }
  root.replaceChildren();
  applyAccent(document.documentElement, data.show.accent);
  document.title = "On a call – Impromptu";

  let terminal = "";
  const handoff = takeHandoff();
  const ctl = new CallController({
    showId: id,
    mic: handoff?.mic,
    onTerminal: (code) => {
      if (code === 4409) navigate(`/show/${id}`, { replace: true });
      else if (code === 4401) navigate(loginPath(), { replace: true });
      else {
        terminal = TERMINAL_TEXT[code] || (["saved", "ended"].includes(ctl.state) ? "" : "The call dropped.");
        render();
      }
    },
  });

  const you = session.hostColor(0);
  const them = session.hostColor(1);
  const el = document.createElement("div");
  el.className = "call stage";
  setHtml(el, html`
    <header class="lm-top call-top">
      <div class="lm-onair"><span data-r="badge"></span><span class="clock" data-r="clock">0:00</span></div>
      <span class="call-left" data-r="left"></span>
    </header>
    <div class="call-head">
      <span class="call-host" aria-hidden="true">${ring({ cls: "call-ring", r: 46, dot: 2.2 })}<span class="call-pulse"></span><span data-r="host-avatar"></span></span>
      <span class="col call-names">
        <span class="call-host-name" data-r="host-name"></span>
        <span class="muted small ellipsis" data-r="title"></span>
      </span>
    </div>
    <section class="lm-captions call-captions" data-r="captions" aria-live="polite"></section>
    <div class="call-modes" data-r="modes" role="group" aria-label="How your co-host plays it"></div>
    <div class="call-bar">
      <button class="rail-btn call-ctl" type="button" data-action="mute"><span class="rail-ic" data-r="mute-ic"></span><span data-r="mute-label">Mute</span></button>
      <button class="call-hangup" type="button" data-action="hangup" aria-label="Hang up and save the episode">${icon("phoneOff", 30, 2.2)}</button>
      <span class="rail-btn call-ctl" aria-hidden="true"><span class="rail-ic me-ic"><span class="me-meter"></span></span><span>You</span></span>
    </div>
    <p class="call-hint" data-r="hint"></p>
    <div class="call-overlay" data-r="overlay" hidden></div>`);
  root.append(el);
  const r = new Regions(el);
  const overlay = r.el("overlay");
  let version = 0;
  let openTimer = 0;

  // ---- pieces -------------------------------------------------------------------------

  const badge = () => {
    if (terminal) return html`<span class="badge ended">Off the line</span>`;
    if (ctl.state === "editing") return html`<span class="badge ended">Editing</span>`;
    if (ctl.state === "saved") return html`<span class="badge ended">Saved</span>`;
    if (ctl.state === "ended" || ctl.hungUp) return html`<span class="badge ended">Hung up</span>`;
    if (ctl.state === "connecting") return html`<span class="badge ended">Dialing</span>`;
    if (ctl.state === "reconnecting" || ctl.connection === "down") return html`<span class="badge ended">Reconnecting</span>`;
    return html`<span class="badge live"><span class="dot"></span>On air</span>`;
  };
  const hostName = () => ctl.host?.name || data.show.hosts?.[1]?.name || "Your co-host";
  const youName = () => ctl.you || data.show.hosts?.[0]?.name || "You";
  const modeLabel = () => (session.options.cohost_modes || []).find((m) => m.id === ctl.mode)?.label || "";

  const captions = () => {
    const said = ctl.said.filter((s) => s.text.trim());
    if (!said.length) {
      const status = ctl.state === "connecting" ? `Dialing ${hostName()}…`
        : ctl.onAir ? `${hostName()} is opening the show…` : "";
      return html`<p class="cap-status">${status}</p>`;
    }
    const line = (s, cls) => {
      const mine = s.who === "you";
      return html`
        <div class="cap ${cls}">
          <span class="cap-who" style="color:${mine ? you.label : them.label}">${mine ? youName() : hostName()}</span>
          <p>${s.text.trim()}</p>
        </div>`;
    };
    const now = said[said.length - 1];
    const prev = said[said.length - 2];
    return html`${prev ? line(prev, "prev") : ""}${line(now, "now")}`;
  };

  const modes = () => html`${(session.options.cohost_modes || []).map((m) => html`
    <button type="button" class="chip mode-chip" data-action="mode" data-mode="${m.id}" aria-pressed="${ctl.mode === m.id}" ${ctl.onAir ? "" : "disabled"}>${m.label}</button>`)}`;

  const hint = () => {
    if (!ctl.onAir) return "";
    if (ctl.mic?.muted) return html`${icon("micOff", 14)} You're muted. ${hostName()} can't hear you.`;
    if (ctl.lookingUp && performance.now() - ctl.lookingUp < 10000) {
      return html`${icon("search", 14)} ${hostName()} is checking your notes…`;
    }
    if (ctl.wrapping) return html`${hostName()} is wrapping up.`;
    if (!local.get("gp_cohost_hint")) {
      return html`${icon("headphones", 14)} Best with headphones, so ${hostName()} doesn't hear itself.
        <button type="button" class="link-btn" data-action="dismiss-hint">Got it</button>`;
    }
    return html`Talk any time. Cut in whenever you like.`;
  };

  const overlayBody = () => {
    if (terminal) {
      return html`<div class="tune-card"><p class="tune-title">Off the line</p><p class="muted">${terminal}</p>
        <a class="btn white big block" href="/" data-link>Your pods</a></div>`;
    }
    if (ctl.state === "editing" || (ctl.hungUp && !["saved", "ended"].includes(ctl.state))) {
      const at = STEPS.findIndex(([k]) => k === ctl.steps[ctl.steps.length - 1]);
      return html`<div class="tune-card edit-card">
        <p class="tune-title">Making your episode</p>
        <ol class="edit-steps">${STEPS.map(([, label], i) => html`
          <li class="${i < at ? "done" : i === at ? "now" : ""}">
            <span class="step-ic" aria-hidden="true">${i < at ? icon("check", 14, 3) : ""}</span>${label}</li>`)}</ol>
      </div>`;
    }
    if (ctl.state === "saved" && ctl.saved) {
      const st = ctl.saved.stats || {};
      const facts = [
        st.tightened_s >= 1 ? `${fmtTime(st.tightened_s)} of dead air tightened` : "",
        st.flubs_cut ? `${st.flubs_cut} flub${st.flubs_cut === 1 ? "" : "s"} cut` : "",
        st.cut_ins ? `${st.cut_ins} cut-in${st.cut_ins === 1 ? "" : "s"} cleaned up` : "",
      ].filter(Boolean);
      return html`<div class="tune-card edit-card">
        <p class="tune-title">${ctl.saved.title || "Your episode is ready"}</p>
        <p class="muted">${fmtTime(ctl.saved.duration)} episode${facts.length ? ` · ${facts.join(" · ")}` : ""}</p>
        <button class="btn white big block" type="button" data-action="open">${icon("play", 18)}Listen to it</button>
      </div>`;
    }
    if (ctl.state === "ended") {
      return html`<div class="tune-card"><p class="tune-title">No episode</p>
        <p class="muted">${ctl.ended?.message || "The call ended."}</p>
        <a class="btn white big block" href="/" data-link>Your pods</a></div>`;
    }
    if (ctl.needsMic) {
      return html`<div class="tune-card">
        <p class="tune-title">You're on a call with ${hostName()}</p>
        <p class="muted">Rejoin to turn your mic back on. The call ends by itself if you're away too long.</p>
        <p class="error" role="alert" data-mic-error></p>
        <button class="btn white big block go" type="button" data-action="rejoin">${icon("mic", 20, 2.2)}Rejoin the call</button>
        <button class="btn link" type="button" data-action="hangup">Hang up and save it</button>
      </div>`;
    }
    return "";
  };

  // ---- render ------------------------------------------------------------------------------

  const render = () => {
    version++;
    const k = `${version}`;
    r.set("badge", `${ctl.state}|${ctl.connection}|${ctl.hungUp}|${terminal}`, badge);
    r.set("host-avatar", hostName(), () => avatar(hostName(), them.avatar, 72));
    r.set("host-name", `${hostName()}|${modeLabel()}`, () => html`${hostName()} <span class="faint">· ${modeLabel()}</span>`);
    r.set("title", data.show.title, () => data.show.title || "");
    r.set("captions", k, captions);
    r.set("modes", `${ctl.mode}|${ctl.onAir}`, modes);
    const muted = Boolean(ctl.mic?.muted);
    r.set("mute-ic", muted, () => icon(muted ? "micOff" : "mic", 22));
    r.set("mute-label", muted, () => (muted ? "Unmute" : "Mute"));
    r.set("hint", k, hint);
    const body = overlayBody();
    overlay.hidden = !body;
    r.set("overlay", k, () => body);
    el.classList.toggle("muted-call", muted);
    if (ctl.accent) applyAccent(document.documentElement, ctl.accent);
    if (ctl.state === "saved" && !openTimer) {
      session.refresh();             // unused minutes came back
      openTimer = setTimeout(() => navigate(`/show/${id}`, { replace: true }), 2400);
    }
  };
  const off = ctl.on(render);

  const offActions = bindActions(el, {
    mute: () => ctl.toggleMute(),
    hangup: () => {
      ctl.hangUp();
      render();
    },
    mode: (b) => ctl.setMode(b.dataset.mode),
    open: () => navigate(`/show/${id}`, { replace: true }),
    "dismiss-hint": () => {
      local.set("gp_cohost_hint", true);
      render();
    },
    rejoin: async (b) => {
      b.disabled = true;
      try {
        ctl.attachMic(await startCallAudio());
        takeHandoff();
      } catch (err) {
        el.querySelector("[data-mic-error]").textContent = err.message;
        b.disabled = false;
      }
    },
  });

  // clock, time left, and the two voices' levels, a dozen times a second
  let raf = 0;
  let last = 0;
  const pulse = el.querySelector(".call-pulse");
  const meter = el.querySelector(".me-meter");
  const callRing = el.querySelector(".call-ring");
  const frame = (t) => {
    raf = requestAnimationFrame(frame);
    if (t - last < FRAME_MS) return;
    last = t;
    const elapsed = ctl.elapsed();
    r.set("clock", Math.floor(elapsed), () => fmtTime(elapsed));
    fillRing(callRing, secondsLit(elapsed));   // the studio clock around your co-host
    const left = Math.ceil(ctl.left());
    r.set("left", `${left}|${ctl.wrapping}|${ctl.onAir}`, () => (!ctl.onAir || !ctl.limitS ? ""
      : ctl.wrapping ? html`<span class="pill pink">Wrapping up · ${fmtTime(left)}</span>` : `${fmtTime(left)} left`));
    pulse.style.setProperty("--pulse", Math.min(1, ctl.player.level() * 7).toFixed(2));
    meter.style.setProperty("--me", ctl.mic && !ctl.mic.muted ? Math.min(1, ctl.mic.level * 9).toFixed(2) : "0");
  };
  raf = requestAnimationFrame(frame);

  const onLeave = (e) => {
    if (!ctl.onAir) return;
    e.preventDefault();
    e.returnValue = "";
  };
  addEventListener("beforeunload", onLeave);
  render();

  return {
    unmount() {
      cancelAnimationFrame(raf);
      clearTimeout(openTimer);
      removeEventListener("beforeunload", onLeave);
      off();
      offActions();
      ctl.destroy();
      document.title = "Impromptu";
      applyAccent(document.documentElement, "#B8336A");
    },
  };
}

function message(root, text) {
  setHtml(root, html`
    <div class="splash stage">
      ${ring({ lit: (i) => i % 15 === 0 })}
      <p class="splash-title">Off the line</p>
      <p class="muted">${text}</p>
      <a class="btn white" href="/" data-link>Your pods</a>
    </div>`);
  return null;
}
