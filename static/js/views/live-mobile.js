// The phone layout of a show (the board's "Live" screen). Tapping the episode
// opens Now playing. Replays go straight to Now playing.
import {
  bubbles, callinsKey, captions, captionsKey, chapterBar, chapterBarKey, chapterLabel, hostAvatars, liveBadge,
  liveBadgeKey, myCallin,
} from "../components.js";
import { html, setHtml } from "../html.js";
import { icon } from "../icons.js";
import { navigate } from "../router.js";
import { loginPath, session } from "../session.js";
import { Regions, bindActions } from "../ui.js";
import { fmtTime } from "../util.js";
import { mountNowPlaying } from "./now-playing.js";
import { bindCallin, callinForm, floatHearts, focusCallin, showActions, tuneInOverlay } from "./shared.js";

export function mountMobile(root, ctl) {
  if (!ctl.live) {
    const page = document.createElement("div");
    page.className = "np-page";
    root.append(page);
    return mountNowPlaying(page, ctl, { page: true });
  }

  const s = ctl.store;
  const el = document.createElement("div");
  el.className = "live-m";
  setHtml(el, html`
    <svg class="rings" aria-hidden="true" viewBox="0 0 620 620"><circle cx="310" cy="310" r="130"/><circle cx="310" cy="310" r="205"/><circle cx="310" cy="310" r="285"/></svg>
    <header class="lm-top">
      <div class="lm-onair"><span data-r="badge"></span><span class="clock" data-r="clock">0:00</span></div>
      <nav class="lm-nav" aria-label="Shows">
        ${session.signedIn ? html`<a href="/" data-link>Your pods</a>` : html`<a href="/discover" data-link>Discover</a>`}
      </nav>
      <button class="icon-btn" data-action="more" aria-label="More options">${icon("more")}</button>
    </header>
    <button class="lm-episode" type="button" data-action="now-playing">
      <span class="cover-sm" aria-hidden="true">${icon("mic", 26, 1.8)}</span>
      <span class="lm-ep-text"><span class="lm-ep-title" data-r="title"></span><span class="lm-ep-sub" data-r="sub"></span></span>
    </button>
    <section class="lm-captions" data-r="captions" aria-live="polite"></section>
    <div class="lm-rail">
      <button class="rail-btn" type="button" data-action="hosts" aria-label="Hosts"><span data-r="avatars"></span><span>Hosts</span></button>
      <button class="rail-btn" type="button" data-action="react" data-heart><span class="rail-ic">${icon("heart")}</span><span data-r="hearts">React</span></button>
      <button class="rail-btn" type="button" data-action="callin"><span class="rail-ic">${icon("chat")}</span><span>Call in</span></button>
      <button class="rail-btn" type="button" data-action="add-context"><span class="rail-ic white">${icon("addFile")}</span><span>Add context</span></button>
      ${s.producer ? html`<button class="rail-btn" type="button" data-action="interrupt"><span class="rail-ic white">${icon("mic")}</span><span>Interrupt</span></button>` : ""}
      ${s.producer ? html`<button class="rail-btn" type="button" data-action="restyle"><span class="rail-ic">${icon("restyle")}</span><span>Restyle</span></button>` : ""}
      <button class="rail-btn" type="button" data-action="share"><span class="rail-ic">${icon("share")}</span><span>Share</span></button>
    </div>
    <div class="lm-bubbles" data-r="bubbles"></div>
    <div class="lm-behind" data-r="behind"></div>
    <div class="chapter-bar thin" data-r="chapters" aria-hidden="true"></div>
    <div class="lm-mine" data-r="mine" aria-live="polite"></div>
    ${callinForm(ctl, { id: "lm-callin" })}
    <div class="tune-in" data-r="tunein" hidden></div>
    <div class="np-overlay" data-np hidden></div>`);
  root.append(el);

  const r = new Regions(el);
  let np = null;
  const npHost = el.querySelector("[data-np]");
  const openNp = () => {
    if (np) return;
    npHost.hidden = false;
    np = mountNowPlaying(npHost, ctl, { onClose: closeNp });
    npHost.scrollTop = 0;
  };
  const closeNp = () => {
    np?.destroy();
    np = null;
    npHost.hidden = true;
    npHost.replaceChildren();
  };

  const offActions = bindActions(el, showActions(ctl, {
    "now-playing": openNp,
    callin: () => (ctl.canTalk ? focusCallin(el) : navigate(loginPath())),
  }));
  bindCallin(el, ctl);

  const tune = r.el("tunein");
  const renderTune = () => {
    const show = !ctl.play.started;
    tune.hidden = !show;
    if (show) r.set("tunein", `${s.version}`, () => tuneInOverlay(ctl));
  };

  const layout = {
    frame(pos) {
      r.set("clock", Math.floor(pos / s.rate), () => fmtTime(pos / s.rate));
      r.set("badge", liveBadgeKey(s, ctl, pos), () => liveBadge(s, ctl, pos));
      r.set("title", s.show.title, () => s.show.title || "Getting the studio ready");
      const sub = [s.hostNames(), chapterLabel(s, pos).replace(/^Chapter /, "chapter ")].filter(Boolean).join(", ");
      r.set("sub", sub, () => sub);
      r.set("captions", captionsKey(s, ctl, pos), () => captions(s, ctl, pos, { big: 30 }));
      r.set("chapters", chapterBarKey(s, pos), () => chapterBar(s, pos));
      r.set("mine", callinsKey(s, pos), () => myCallin(s, pos));
      const behind = ctl.behindLive(pos);
      r.set("behind", behind, () => behind
        ? html`<button class="behind-pill" type="button" data-action="jump-live">${icon("jumpLive", 16)}Back to live</button>` : "");
      np?.frame(pos);
    },
    onEvent(e) {
      r.set("avatars", `${s.version}`, () => hostAvatars(s, 34));
      r.set("bubbles", `${s.version}`, () => bubbles(s));
      r.set("listeners", `${s.listeners}`, () => (s.listeners > 1 ? `${s.listeners} listening` : ""));
      if (e.type === "react" && e.burst) floatHearts(el.querySelector("[data-heart]"), e.burst);
      r.set("hearts", s.hearts, () => (s.hearts ? String(s.hearts) : "React"));
      renderTune();
      np?.onEvent(e);
    },
    onPlay() {
      renderTune();
      np?.onPlay();
    },
    destroy() {
      offActions();
      closeNp();
      el.remove();
    },
  };
  layout.onEvent({ type: "init" });
  return layout;
}
