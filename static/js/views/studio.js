// The desktop layout of a show (the board's "Desktop studio"): library on the
// left, the 16:9 live player in the middle, call-ins / context / rundown on the right.
import {
  callins, callinsKey, captions, captionsKey, chapterBar, chapterBarKey, chapterLabel, chapters, chaptersKey,
  contextKey, contextList, hostAvatars, liveBadge, liveBadgeKey,
} from "../components.js";
import { html, renderSpoken, setHtml } from "../html.js";
import { icon, VISIBILITY_LABEL } from "../icons.js";
import { navigate } from "../router.js";
import { loginPath, session } from "../session.js";
import { bindSubscribe, creatorBar, creatorBarKey } from "../social.js";
import { Regions, bindActions } from "../ui.js";
import { fmtTime, store as local } from "../util.js";
import { appHeader, bindHeader, mountLibraryList } from "./library.js";
import {
  bindCallin, bindSeekBar, callinForm, floatHearts, focusCallin, showActions, tuneInOverlay,
} from "./shared.js";

const TABS = [["callins", "Call-ins"], ["context", "Context"], ["rundown", "Rundown"], ["transcript", "Transcript"]];

export function mountStudio(root, ctl) {
  const s = ctl.store;
  const cohosted = s.show.kind === "cohost";   // a recorded call: nobody called in
  const tabs = cohosted ? TABS.filter(([id]) => id !== "callins") : TABS;
  let tab = tabs[0][0];
  let captionsOn = local.get("gp_captions", true);
  let muted = false;

  const el = document.createElement("div");
  el.className = "studio";
  setHtml(el, html`
    ${appHeader()}
    <div class="studio-body ${session.signedIn ? "" : "no-lib"}">
      ${session.signedIn ? html`<aside class="lib-pane" data-lib></aside>` : ""}
      <main class="stage">
        <section class="player" aria-label="${ctl.live ? "Live player" : "Player"}" data-player>
          <svg class="rings" aria-hidden="true" viewBox="0 0 700 700"><circle cx="350" cy="350" r="150"/><circle cx="350" cy="350" r="240"/><circle cx="350" cy="350" r="330"/></svg>
          <div class="pl-top">
            <span class="row gap-s"><span data-r="badge"></span><span class="clock" data-r="clock">0:00</span></span>
            <span class="pl-chapter" data-r="chapterlabel"></span>
          </div>
          <div class="pl-captions" data-r="captions" aria-live="polite"></div>
          <div class="pl-bottom">
            <div class="chapter-bar" data-r="chapters" aria-hidden="true"></div>
            <div class="pl-controls">
              <span class="row">
                <button class="icon-btn" type="button" data-action="toggle-play" data-r="play" aria-label="Play"></button>
                <button class="icon-btn" type="button" data-action="back15" aria-label="Back 15 seconds">${icon("back", 22)}</button>
                <button class="icon-btn" type="button" data-action="mute" data-r="mute" aria-label="Mute"></button>
                <span class="pl-time" data-r="time"></span>
                <span data-r="behind"></span>
              </span>
              <span class="row">
                <button class="icon-btn" type="button" data-action="captions" data-r="cc" aria-label="Captions"></button>
                ${s.producer && ctl.live ? html`<button class="icon-btn" type="button" data-action="restyle" aria-label="Restyle the show">${icon("restyle", 22)}</button>` : ""}
                <button class="icon-btn" type="button" data-action="fullscreen" aria-label="Full screen">${icon("fullscreen", 22)}</button>
              </span>
            </div>
          </div>
          <div class="tune-in" data-r="tunein" hidden></div>
        </section>
        <h1 class="stage-title" data-r="title"></h1>
        <div class="meta-row">
          <div class="row gap">
            <span data-r="avatars"></span>
            <span class="col"><strong data-r="hostnames"></strong><span class="muted small" data-r="stylemeta"></span></span>
          </div>
          <div class="row gap wrap">
            ${s.producer && ctl.live ? html`<button class="btn white" type="button" data-action="interrupt">${icon("mic", 18)}Interrupt</button>` : ""}
            ${ctl.live ? html`<button class="btn ${s.producer ? "chip-btn" : "white"}" type="button" data-action="add-context">${icon("addFile", 18)}Add context</button>` : ""}
            ${s.producer && ctl.live ? html`<button class="btn chip-btn" type="button" data-action="restyle">Restyle</button>` : ""}
            ${ctl.live ? html`<button class="btn chip-btn" type="button" data-action="react" data-heart>${icon("heart", 18)}<span data-r="hearts">React</span></button>` : ""}
            <button class="btn chip-btn" type="button" data-action="share">Share</button>
            <button class="btn chip-btn" type="button" data-action="more">More</button>
          </div>
        </div>
        <div class="creator-bar" data-r="creator"></div>
        <section class="card about" aria-label="About this episode">
          <p class="about-text" data-r="about"></p>
          <div class="col gap-s">
            <h3>Chapters</h3>
            <ol class="chapter-cards" data-r="chaptercards"></ol>
          </div>
          <div class="col gap-s">
            <h3>Context the hosts ${ctl.live ? "are using" : "used"}</h3>
            <div class="ctx-grid"><div class="ctx-grid-items" data-r="contextgrid"></div>
              ${ctl.live ? html`<button class="ctx-add" type="button" data-action="add-context">${icon("plus", 16, 2.4)}Add context</button>` : ""}</div>
          </div>
        </section>
      </main>
      <aside class="live-panel" aria-label="Live panel">
        <div class="tabs" role="tablist" aria-label="Live panel">
          ${tabs.map(([id, label]) => html`<button role="tab" type="button" class="tab" data-tab="${id}" aria-selected="${id === tab}">${label}</button>`)}
        </div>
        <div class="lp-body" data-r="panel" role="tabpanel"></div>
        ${ctl.live ? html`
          <div class="lp-foot">
            ${callinForm(ctl, { id: "d-callin", variant: "panel" })}
            <p class="faint small">The hosts read call-ins on air. Tap + to attach a file, link or photo and they'll use it too.</p>
          </div>` : html`<div class="lp-foot"><p class="faint small">${cohosted
            ? "Recorded live with an AI co-host, then edited into this episode."
            : "This pod has ended. Call-ins are shown as they aired."}</p></div>`}
      </aside>
    </div>`);
  root.append(el);

  const r = new Regions(el);
  const lib = session.signedIn ? mountLibraryList(el.querySelector("[data-lib]"), { activeId: ctl.id, compact: true }) : null;
  const offHead = bindHeader(el, { onSearch: (q) => lib?.search(q) });
  const offSub = bindSubscribe(el, { onChange: (st) => Object.assign(s.creator, st) });
  const player = el.querySelector("[data-player]");
  const offActions = bindActions(el, showActions(ctl, {
    callin: () => (ctl.canTalk ? focusCallin(el) : navigate(loginPath())),
    captions: () => {
      captionsOn = !captionsOn;
      local.set("gp_captions", captionsOn);
      r.reset();
    },
    mute: () => {
      muted = !muted;
      ctl.player.setVolume(muted ? 0 : 1);
      r.reset();
    },
    fullscreen: () => {
      if (document.fullscreenElement) document.exitFullscreen();
      else player.requestFullscreen?.().catch(() => {});
    },
  }));
  bindCallin(el, ctl);
  const offSeek = bindSeekBar(r.el("chapters"), ctl);
  el.querySelector(".tabs").addEventListener("click", (e) => {
    const t = e.target.closest("[data-tab]");
    if (!t) return;
    tab = t.dataset.tab;
    for (const b of el.querySelectorAll("[data-tab]")) b.setAttribute("aria-selected", String(b.dataset.tab === tab));
    r.keys.delete("panel");
  });

  const panel = (pos) => {
    if (tab === "callins") return callins(s, pos, { producer: s.producer && ctl.live });
    if (tab === "context") return html`<div class="ctx-col">${contextList(s, pos)}</div>
      ${ctl.live ? html`<button class="btn outline block" type="button" data-action="add-context">Add context</button>` : ""}`;
    if (tab === "rundown") return html`<ol class="rundown">${chapters(s, pos, { rows: true })}</ol>`;
    const lines = s.heardLines(pos, 200);
    if (!lines.length) return html`<p class="empty">The transcript appears here as the hosts talk.</p>`;
    return html`<div class="transcript">${lines.map((l) => html`
      <p class="t-line"><span class="t-who">${l.line.speaker}</span>${renderSpoken(l.line.text, s.hostIndex(l.line.speaker) ? 0 : 1)}</p>`)}</div>`;
  };
  const panelKey = (pos) => {
    if (tab === "callins") return `${tab}|${callinsKey(s, pos)}`;
    if (tab === "context") return `${tab}|${contextKey(s, pos)}`;
    if (tab === "rundown") return `${tab}|${chaptersKey(s, pos)}`;
    const at = s.lineAt(pos);
    return `${tab}|${s.version}|${at ? at.seg.seq + ":" + at.idx : "-"}`;
  };

  const tune = r.el("tunein");
  const renderTune = () => {
    const show = ctl.live && !ctl.play.started;
    tune.hidden = !show;
    if (show) r.set("tunein", `${s.version}`, () => tuneInOverlay(ctl));
  };

  const layout = {
    frame(pos) {
      r.set("clock", Math.floor(pos / s.rate), () => fmtTime(pos / s.rate));
      r.set("badge", liveBadgeKey(s, ctl, pos), () => liveBadge(s, ctl, pos));
      const label = chapterLabel(s, pos);
      r.set("chapterlabel", label, () => label);
      r.set("captions", `${captionsOn}|${captionsKey(s, ctl, pos)}`,
        () => (captionsOn ? captions(s, ctl, pos, { big: 31 }) : html`<p class="cap-status">Captions are off.</p>`));
      r.set("chapters", chapterBarKey(s, pos), () => chapterBar(s, pos));
      const paused = !ctl.play.started || ctl.play.paused;
      r.set("play", paused, () => (paused ? icon("play", 22, 2.6) : icon("pause", 22, 3)));
      r.el("play").setAttribute("aria-label", paused ? "Play" : "Pause");
      r.set("mute", muted, () => icon(muted ? "mute" : "volume", 22));
      r.set("cc", captionsOn, () => icon("captions", 22));
      r.el("cc").setAttribute("aria-pressed", String(captionsOn));
      const beats = s.beats();
      const t = `${fmtTime(pos / s.rate)}${beats.length ? ` in ${beats[s.chapterAt(pos)]?.title || ""}` : ""}`;
      r.set("time", t, () => html`${fmtTime(pos / s.rate)}${beats.length ? html` <span class="muted">in ${beats[s.chapterAt(pos)]?.title || ""}</span>` : ""}`);
      const behind = ctl.behindLive(pos);
      r.set("behind", behind, () => behind
        ? html`<button class="behind-pill" type="button" data-action="jump-live">${icon("jumpLive", 16)}Back to live</button>` : "");
      r.set("panel", panelKey(pos), () => panel(pos));
      r.set("chaptercards", chaptersKey(s, pos), () => chapters(s, pos));
      r.set("contextgrid", contextKey(s, pos), () => contextList(s, pos, { variant: "tile" }));
    },
    onEvent(e) {
      r.set("title", s.show.title, () => s.show.title || "Getting the studio ready");
      r.set("about", `${s.show.about}|${s.show.tagline}`, () => s.show.about || s.show.tagline || "");
      r.set("avatars", `${s.version}`, () => hostAvatars(s, 40));
      r.set("hostnames", s.hostNames(), () => s.hostNames() || "Hosts coming up");
      const by = !s.producer ? "" : s.show.kind === "briefing" ? "Your briefing" : VISIBILITY_LABEL[s.show.visibility] || "";
      r.set("creator", creatorBarKey(s), () => creatorBar(s, ctl.id));
      const meta = [by, s.style?.label || s.show.style_label, s.show.minutes ? `about ${s.show.minutes} minutes` : ""]
        .filter(Boolean).join(" · ");
      r.set("stylemeta", meta, () => meta);
      r.set("hearts", s.hearts, () => (s.hearts ? String(s.hearts) : "React"));
      if (e.type === "react" && e.burst) floatHearts(el.querySelector("[data-heart]"), e.burst);
      if (e.type === "plan" || e.type === "ended") lib?.refresh();
      renderTune();
    },
    onPlay() {
      renderTune();
    },
    destroy() {
      offActions();
      offHead();
      offSub();
      offSeek();
      lib?.destroy();
      el.remove();
    },
  };
  layout.onEvent({ type: "init" });
  return layout;
}
