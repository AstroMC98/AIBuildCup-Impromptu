// /show/{id}: a live pod or a replay, for its owner, the people it's shared
// with, or anyone if it's public. Loads over HTTP first, then picks the phone
// or desktop layout.
import { api } from "../api.js";
import { unlockAudio } from "../audio/context.js";
import { loader } from "../components.js";
import { countIn, goStage } from "../golive.js";
import { html, setHtml } from "../html.js";
import { ring } from "../ring.js";
import { icon, VISIBILITY_LABEL } from "../icons.js";
import { ShowController } from "../live/controller.js";
import { navigate } from "../router.js";
import { loginPath, session } from "../session.js";
import { applyAccent } from "../theme.js";
import { confirmSheet } from "../ui.js";
import { toast } from "../util.js";
import { sleeve } from "./library.js";
import { mountMobile } from "./live-mobile.js";
import { memoScreen } from "./memo-status.js";
import { mountStudio } from "./studio.js";

const FRAME_MS = 80;

export async function showView(root, { params }) {
  const id = params[0];
  setHtml(root, loader("Tuning in…"));

  let data;
  try {
    data = await api.getShow(id);
  } catch (err) {
    if (err.status === 401) {
      navigate(loginPath(), { replace: true });
      return null;
    }
    return errorScreen(root, err.status === 404
      ? "This pod doesn't exist, or it isn't shared with you. Ask its creator to share it with you as a connection."
      : err.message);
  }
  if (data.call) {   // a co-hosted call still going (or being edited): back to the call
    navigate(data.call, { replace: true });
    return null;
  }
  if (data.show.status === "draft") return draftScreen(root, data);
  // A voice-memo pod being made, waiting for review, or failed: only its owner sees these.
  if (data.show.kind === "memo" && ["rendering", "review", "failed"].includes(data.show.status) && !data.show.has_audio) {
    return memoScreen(root, data);
  }

  const ctl = new ShowController({
    id, data,
    onTerminal: (code) => {
      if (ctl.deleted) return;   // its creator just deleted it: no need to say it's gone
      if (code === 4409) navigate(location.pathname + location.search, { replace: true });
      else if (code === 4401) navigate(loginPath(), { replace: true });
      else toast(code === 4403 ? "This pod isn't shared with you any more." : "This pod isn't available.", 8000);
    },
  });
  root.classList.toggle("visitor", !ctl.canTalk);
  applyAccent(document.documentElement, data.show.accent);
  document.title = `${data.show.title || "Live show"} – Impromptu`;

  const mq = matchMedia("(min-width: 1024px)");
  let layout = null;
  const mount = () => {
    layout?.destroy();
    root.replaceChildren();
    layout = (mq.matches ? mountStudio : mountMobile)(root, ctl);
  };
  mq.addEventListener("change", mount);
  mount();

  // The creator arrives here straight from the "Go live" click, with audio unlocked.
  if (ctl.live && ctl.canAutoplay) ctl.tuneInWhenReady();

  let raf = 0;
  let last = 0;
  let accent = data.show.accent;
  const frame = (t) => {
    raf = requestAnimationFrame(frame);
    if (t - last < FRAME_MS) return;
    last = t;
    // Before tuning in, "where you are" is the live edge, not the start of the show.
    const pos = ctl.live && !ctl.player.started ? ctl.liveEdge() : ctl.player.position();
    const a = ctl.store.accentAt(pos);
    if (a && a !== accent) {
      accent = a;
      applyAccent(document.documentElement, a);
    }
    layout.frame(pos);
  };
  raf = requestAnimationFrame(frame);
  const markEnded = () => root.classList.toggle("show-ended", Boolean(ctl.store.ended));
  markEnded();
  const offStore = ctl.store.on((e) => {
    if (e.type === "plan") document.title = `${e.title} – Impromptu`;
    if (e.type === "saved" && ctl.store.producer) session.refresh();  // unused minutes come back
    markEnded();
    layout.onEvent?.(e);
  });
  const offCtl = ctl.on(() => layout.onPlay?.());

  return {
    unmount() {
      cancelAnimationFrame(raf);
      mq.removeEventListener("change", mount);
      offStore();
      offCtl();
      layout?.destroy();
      ctl.destroy();
      root.classList.remove("show-ended", "visitor");
      document.title = "Impromptu";
      applyAccent(document.documentElement, "#B8336A");
    },
  };
}

function errorScreen(root, message) {
  setHtml(root, html`
    <div class="splash stage">
      ${ring({ lit: (i) => i % 15 === 0 })}
      <p class="splash-title">Off air</p>
      <p class="muted">${message}</p>
      <a class="btn white" href="${session.signedIn ? "/" : "/discover"}" data-link>${session.signedIn ? "Your pods" : "Discover pods"}</a>
    </div>`);
  return null;
}

/** A pod saved for later, on the stage: its sleeve is the page, and Go live counts in. */
function draftScreen(root, data) {
  const s = data.show;
  const cost = session.cost(s.minutes, Boolean(s.request?.research));
  const hosts = (s.hosts || []).map((h) => h.name).filter(Boolean);
  applyAccent(document.documentElement, s.accent);
  setHtml(root, html`
    <div class="draft stage">
      <header class="draft-top">
        <a class="icon-btn" href="/" data-link aria-label="Your pods">${icon("chevronLeft", 24, 2.2)}</a>
        <span class="badge ended">Draft</span>
      </header>
      <div class="draft-body">
        ${sleeve(s, "lg")}
        <h1>${s.title || "Untitled pod"}</h1>
        <p class="muted">${[s.style_label, hosts.join(" and ")].filter(Boolean).join(", ")}</p>
        <dl class="draft-rows">
          <div><dt>Length</dt><dd>About ${s.minutes} minutes</dd></div>
          <div><dt>Who can listen</dt><dd>${VISIBILITY_LABEL[s.visibility] || "Only you"}</dd></div>
          ${s.language && s.language !== "English" ? html`<div><dt>Language</dt><dd>${s.language}</dd></div>` : ""}
          ${s.request?.research ? html`<div><dt>Research</dt><dd>On</dd></div>` : ""}
        </dl>
      </div>
      <div class="draft-foot">
        <p class="cost ${session.credits < cost ? "short" : ""}">Uses ${cost} credits when it goes live · you have ${session.credits}</p>
        <p class="error" role="alert" data-error></p>
        <button class="btn white big block go" type="button" data-go ${session.credits < cost ? "disabled" : ""}><span class="rec-dot" aria-hidden="true"></span>Go live</button>
        <button class="btn link" type="button" data-delete>Delete draft</button>
      </div>
    </div>`);
  root.querySelector("[data-go]").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    await unlockAudio();   // inside the click, so the pod can start playing on its own
    try {
      await countIn(btn, () => api.startDraft(s.id));
      session.refresh();
      goStage(`/show/${s.id}`, { replace: true });
    } catch (err) {
      root.querySelector("[data-error]").textContent = err.message;
      btn.disabled = false;
    }
  });
  root.querySelector("[data-delete]").addEventListener("click", async () => {
    if (!(await confirmSheet({ title: "Delete this draft?", message: "This can't be undone.", confirm: "Delete", danger: true }))) return;
    await api.deleteShow(s.id).catch((err) => toast(err.message));
    navigate("/", { replace: true });
  });
  return null;
}
