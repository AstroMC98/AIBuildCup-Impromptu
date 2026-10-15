// The phone layout of a live show: the stage. The tally lamp and the studio clock at the
// top, the episode (tap for Now playing), captions that light word by word, the hosts
// (the one talking glows with their voice), and a bar to call in, react or, for the
// producer, interrupt. Replays go straight to Now playing.
import {
  bubbles, callinsKey, captions, captionsKey, captionTick, chapterBar, chapterBarKey, chapterLabel, hostChips,
  hostChipsKey, liveBadge, liveBadgeKey, myCallin,
} from "../components.js";
import { html, setHtml } from "../html.js";
import { icon } from "../icons.js";
import { reduced } from "../motion.js";
import { fillRing, ring, secondsLit } from "../ring.js";
import { navigate } from "../router.js";
import { loginPath, session } from "../session.js";
import { Regions, bindActions } from "../ui.js";
import { fmtTime } from "../util.js";
import { sleeve } from "./library.js";
import { mountNowPlaying } from "./now-playing.js";
import { bindCallin, callinForm, floatHearts, focusCallin, showActions, tuneInOverlay } from "./shared.js";

export function mountMobile(root, ctl) {
  if (!ctl.live) {
    const page = document.createElement("div");
    page.className = "np-page stage";
    root.append(page);
    return mountNowPlaying(page, ctl, { page: true });
  }

  const s = ctl.store;
  const el = document.createElement("div");
  el.className = "live-m stage";
  setHtml(el, html`
    <header class="lm-top">
      <a class="icon-btn" href="${session.signedIn ? "/" : "/discover"}" data-link aria-label="${session.signedIn ? "Home" : "Discover"}">${icon("chevronDown", 24, 2.2)}</a>
      <span data-r="badge"></span>
      <span class="lm-clock">${ring({ cls: "clock-ring" })}<span class="clock" data-r="clock">0:00</span></span>
      <span class="lm-listen" data-r="listeners"></span>
      <button class="icon-btn" type="button" data-action="more" aria-label="More options">${icon("more")}</button>
    </header>
    <button class="lm-episode" type="button" data-action="now-playing" aria-label="Open Now playing">
      <span data-r="cover"></span>
      <span class="lm-ep-text"><span class="lm-ep-title" data-r="title"></span><span class="lm-ep-sub" data-r="sub"></span></span>
      <span class="lm-ep-up" aria-hidden="true">${icon("chevronUp", 18, 2.2)}</span>
    </button>
    <section class="lm-captions" data-r="captions" aria-live="polite"></section>
    <div class="lm-bubbles" data-r="bubbles"></div>
    <div class="lm-behind" data-r="behind"></div>
    <div class="chapter-bar thin" data-r="chapters" aria-hidden="true"></div>
    <div class="lm-mine" data-r="mine" aria-live="polite"></div>
    <div class="lm-hosts" data-r="hosts"></div>
    <div class="lm-bar">
      ${callinForm(ctl, { id: "lm-callin" })}
      <button class="round-btn ghost lm-heart" type="button" data-action="react" data-heart aria-label="React">${icon("heart")}<span class="lm-count" data-r="hearts"></span></button>
      ${s.producer ? html`<button class="round-btn white lm-interrupt" type="button" data-action="interrupt" aria-label="Interrupt the show">${icon("mic")}</button>` : ""}
    </div>
    <div class="tune-in" data-r="tunein" hidden></div>
    <div class="np-overlay stage" data-np hidden></div>`);
  root.append(el);

  const r = new Regions(el);
  const clockRing = el.querySelector(".clock-ring");
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
      const calm = reduced();
      r.set("clock", Math.floor(pos / s.rate), () => fmtTime(pos / s.rate));
      fillRing(clockRing, secondsLit(pos / s.rate));
      r.set("badge", liveBadgeKey(s, ctl, pos), () => liveBadge(s, ctl, pos));
      r.set("title", s.show.title, () => s.show.title || "Getting the studio ready");
      r.set("cover", `${s.show.title}|${s.show.accent}`, () => sleeve({ title: s.show.title, accent: s.show.accent, kind: s.show.kind }, "sm"));
      const sub = [s.hostNames(), chapterLabel(s, pos).replace(/^Chapter /, "chapter ")].filter(Boolean).join(", ");
      r.set("sub", sub, () => sub);
      r.set("captions", captionsKey(s, ctl, pos), () => captions(s, ctl, pos, { big: 30 }));
      captionTick(r.el("captions"), s, pos, { calm });
      r.set("chapters", chapterBarKey(s, pos), () => chapterBar(s, pos));
      r.set("mine", callinsKey(s, pos), () => myCallin(s, pos));
      r.set("hosts", hostChipsKey(s, pos), () => hostChips(s, pos));
      // The key light and the talking host's ring follow the voices.
      const lv = calm ? 0 : Math.min(1, (ctl.player.level?.() || 0) * 6);
      el.style.setProperty("--level", lv.toFixed(3));
      el.querySelector(".host-chip.talk")?.style.setProperty("--lv", lv.toFixed(3));
      const behind = ctl.behindLive(pos);
      r.set("behind", behind, () => behind
        ? html`<button class="behind-pill" type="button" data-action="jump-live">${icon("jumpLive", 16)}Back to live</button>` : "");
      np?.frame(pos);
    },
    onEvent(e) {
      r.set("bubbles", `${s.version}`, () => bubbles(s));
      r.set("listeners", `${s.listeners}`, () => (s.listeners > 1 ? `${s.listeners} listening` : ""));
      if (e.type === "react" && e.burst) floatHearts(el.querySelector("[data-heart]"), e.burst);
      r.set("hearts", s.hearts, () => (s.hearts ? String(s.hearts) : ""));
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
