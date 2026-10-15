// /show/{id}: a live pod or a replay, for its owner, the people it's shared
// with, or anyone if it's public. Loads over HTTP first, then picks the phone
// or desktop layout.
import { api } from "../api.js";
import { html, setHtml } from "../html.js";
import { icon } from "../icons.js";
import { ShowController } from "../live/controller.js";
import { navigate } from "../router.js";
import { loginPath, session } from "../session.js";
import { applyAccent } from "../theme.js";
import { confirmSheet } from "../ui.js";
import { toast } from "../util.js";
import { mountMobile } from "./live-mobile.js";
import { mountStudio } from "./studio.js";

const FRAME_MS = 80;

export async function showView(root, { params }) {
  const id = params[0];
  setHtml(root, html`<div class="splash"><span class="spinner" aria-hidden="true"></span><p>Tuning in…</p></div>`);

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
  if (data.show.status === "draft") return draftScreen(root, data);

  const ctl = new ShowController({
    id, data,
    onTerminal: (code) => {
      if (code === 4409) navigate(location.pathname + location.search, { replace: true });
      else if (code === 4401) navigate(loginPath(), { replace: true });
      else toast(code === 4403 ? "This pod isn't shared with you any more." : "This pod isn't available.", 8000);
    },
  });
  root.classList.toggle("visitor", !ctl.canTalk);
  applyAccent(document.documentElement, data.show.accent);
  document.title = `${data.show.title || "Live show"} – Gemini Podcasts`;

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
    if (e.type === "plan") document.title = `${e.title} – Gemini Podcasts`;
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
      document.title = "Gemini Podcasts";
      applyAccent(document.documentElement, "#B8336A");
    },
  };
}

function errorScreen(root, message) {
  setHtml(root, html`
    <div class="splash">
      <p class="splash-title">Off air</p>
      <p class="muted">${message}</p>
      <a class="btn white" href="${session.signedIn ? "/" : "/discover"}" data-link>${session.signedIn ? "Your pods" : "Discover pods"}</a>
    </div>`);
  return null;
}

function draftScreen(root, data) {
  const s = data.show;
  const cost = session.cost(s.minutes);
  applyAccent(document.documentElement, s.accent);
  setHtml(root, html`
    <div class="page narrow">
      <a class="back-link" href="/" data-link>${icon("chevronLeft", 20)}Your pods</a>
      <div class="draft-card">
        <span class="badge ended">Draft</span>
        <h1>${s.title || "Untitled pod"}</h1>
        <p class="muted">${s.style_label}, about ${s.minutes} minutes${s.language && s.language !== "English" ? `, in ${s.language}` : ""}</p>
        <p class="faint">${s.style}</p>
        <p class="cost ${session.credits < cost ? "short" : ""}">Uses ${cost} credits when it goes live · you have ${session.credits}</p>
        <p class="error" role="alert" data-error></p>
        <button class="btn white big block go" type="button" data-go ${session.credits < cost ? "disabled" : ""}><span class="rec-dot"></span>Go live</button>
        <button class="btn link danger-text" type="button" data-delete>${icon("trash", 18)}Delete draft</button>
      </div>
    </div>`);
  root.querySelector("[data-go]").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    const { unlockAudio } = await import("../audio/context.js");
    await unlockAudio();
    try {
      await api.startDraft(s.id);
      session.refresh();
      navigate(`/show/${s.id}`, { replace: true });
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
