// "Now playing" (the board's player screen): cover, chapter bar, transport,
// live transcript, call-ins, context and chapters. An overlay over the phone
// live view, or the whole page for a replay.
import {
  callins, callinsKey, chapterBar, chapterBarKey, chapters, chaptersKey, contextList, statusText, talking,
} from "../components.js";
import { html, renderSpoken, setHtml } from "../html.js";
import { icon } from "../icons.js";
import { navigate } from "../router.js";
import { loginPath } from "../session.js";
import { bindSubscribe, creatorBar, creatorBarKey } from "../social.js";
import { Regions, bindActions } from "../ui.js";
import { fmtTime } from "../util.js";
import { bindCallin, callinForm, focusCallin, showActions } from "./shared.js";

export function mountNowPlaying(host, ctl, { page = false, onClose } = {}) {
  const s = ctl.store;
  const el = document.createElement("div");
  el.className = "np";
  setHtml(el, html`
    <header class="np-top">
      ${page
        ? html`<a class="icon-btn" href="/" data-link aria-label="Your pods">${icon("chevronLeft", 24, 2.2)}</a>`
        : html`<button class="icon-btn" type="button" data-action="close" aria-label="Back to the live view">${icon("chevronDown", 24, 2.2)}</button>`}
      <div class="np-from"><span class="faint small">${ctl.live ? "Playing live from" : "Replay of"}</span><span data-r="from"></span></div>
      <button class="icon-btn" type="button" data-action="more" aria-label="More options">${icon("more")}</button>
    </header>
    <div class="np-cover" role="img" data-r="cover"></div>
    <div class="np-titlebar">
      <div class="np-title"><h1 data-r="title"></h1><span class="muted" data-r="hosts"></span></div>
      ${ctl.live ? html`<button class="round-btn outline" type="button" data-action="add-context" aria-label="Add context">${icon("plus", 20, 2.4)}</button>` : ""}
    </div>
    <div class="creator-bar np-creator" data-r="creator"></div>
    <div class="np-progress">
      <div class="chapter-bar knobbed" data-r="chapters"></div>
      <div class="np-progress-meta"><span data-r="where"></span><span data-r="live"></span></div>
    </div>
    <div class="np-transport">
      ${s.producer && ctl.live ? html`<button class="icon-btn big" type="button" data-action="restyle" aria-label="Restyle the show">${icon("restyle")}</button>`
        : html`<span class="icon-btn big" aria-hidden="true"></span>`}
      <button class="icon-btn big" type="button" data-action="back15" aria-label="Back 15 seconds">${icon("back15", 28)}</button>
      <button class="play-btn" type="button" data-action="toggle-play" data-r="play" aria-label="Play"></button>
      ${ctl.live ? html`<button class="icon-btn big" type="button" data-action="jump-live" aria-label="Jump to live">${icon("jumpLive", 26)}</button>`
        : html`<span class="icon-btn big" aria-hidden="true"></span>`}
      ${ctl.live ? html`<button class="icon-btn big" type="button" data-action="callin" aria-label="Call in">${icon("chat")}</button>`
        : html`<span class="icon-btn big" aria-hidden="true"></span>`}
    </div>
    <div class="np-sub">
      <span class="talking" data-r="talking"></span>
      <span class="row">
        <button class="icon-btn" type="button" data-action="download-audio" aria-label="Download episode">${icon("download", 20)}</button>
        <button class="icon-btn" type="button" data-action="share" aria-label="Share">${icon("share", 20)}</button>
      </span>
    </div>
    <section class="card accent np-transcript" aria-labelledby="np-t">
      <div class="card-head"><h2 id="np-t">${ctl.live ? "Live transcript" : "Transcript"}</h2>
        <button class="icon-btn round dark" type="button" data-action="download-transcript" aria-label="Download the full transcript">${icon("download", 16, 2.4)}</button></div>
      <div class="np-lines" data-r="transcript" aria-live="polite"></div>
    </section>
    <section class="card" id="np-callins" aria-labelledby="np-c" ${s.show.kind === "cohost" ? "hidden" : ""}>
      <div class="card-head"><h2 id="np-c">Call-ins</h2></div>
      <div data-r="callins"></div>
      ${callinForm(ctl, { id: "np-callin", variant: "inline" })}
    </section>
    <section class="card" aria-labelledby="np-x">
      <h2 id="np-x" class="card-h">Context the hosts ${ctl.live ? "are using" : "used"}</h2>
      <div data-r="context"></div>
      ${ctl.live ? html`<button class="btn outline block" type="button" data-action="add-context">${icon("plus", 18, 2.2)}Add context</button>` : ""}
    </section>
    <section class="np-chapters" aria-labelledby="np-ch">
      <h2 id="np-ch" class="card-h">Chapters</h2>
      <ol class="chapter-cards" data-r="chaptercards"></ol>
    </section>`);
  host.append(el);

  const r = new Regions(el);
  const offActions = bindActions(el, showActions(ctl, {
    close: () => onClose?.(),
    callin: () => (ctl.canTalk ? focusCallin(el) : navigate(loginPath())),
  }));
  bindCallin(el, ctl);
  const onKey = (e) => { if (e.key === "Escape" && onClose && !document.body.classList.contains("sheet-open")) onClose(); };
  document.addEventListener("keydown", onKey);

  const offSub = bindSubscribe(el, { onChange: (st) => Object.assign(s.creator, st) });

  const renderStatic = () => {
    r.set("creator", creatorBarKey(s), () => creatorBar(s, ctl.id));
    const sources = s.show.sources || [];
    const from = sources.length ? `${sources[0]}${sources.length > 1 ? ` and ${sources.length - 1} more` : ""}` : s.show.title || "";
    r.set("from", from, () => html`<strong>${from}</strong>`);
    r.set("title", s.show.title, () => s.show.title || "Getting the studio ready");
    r.set("hosts", s.hostNames(), () => s.hostNames());
    r.set("cover", s.show.title, () => html`
      <span class="cover-brand">Gemini Podcasts</span>
      <span class="cover-glyph" aria-hidden="true">${icon("mic", 190, 0.9)}</span>
      <span class="cover-title">${s.show.title || "…"}</span>`);
    r.el("cover").setAttribute("aria-label", `Episode cover: ${s.show.title || ""}`);
  };

  const layout = {
    frame(pos) {
      r.set("chapters", chapterBarKey(s, pos), () => chapterBar(s, pos, { knob: true }));
      const beats = s.beats();
      const where = beats.length ? `${beats[s.chapterAt(pos)]?.title || ""}` : "";
      const t = fmtTime(pos / s.rate);
      r.set("where", `${where}|${t}`, () => html`<span class="strong">${where}</span>${where ? " at " : ""}${t}`);
      const liveState = !ctl.live ? "replay" : s.ended ? "ended" : ctl.behindLive(pos) ? "behind" : "live";
      r.set("live", liveState, () => liveState === "live" ? html`<span class="live-dot-label"><span class="dot"></span>Live</span>`
        : liveState === "behind" ? html`<button class="link-btn" type="button" data-action="jump-live">Behind live, jump ahead</button>`
          : liveState === "ended" ? "Ended" : fmtTime((s.clock.audio_end || 0) / s.rate));
      const paused = !ctl.play.started || ctl.play.paused;
      r.set("play", paused, () => (paused ? icon("play", 28, 2.6) : icon("pause", 28, 3.2)));
      r.el("play").setAttribute("aria-label", paused ? "Play" : "Pause");
      const talk = talking(s, pos);
      r.set("talking", talk, () => talk ? html`<span class="talking-ic">${icon("bars", 16, 3)}</span>${talk}` : "");
      const at = s.lineAt(pos);
      const status = statusText(s, ctl, pos);
      r.set("transcript", `${s.version}|${at ? at.seg.seq + ":" + at.idx : "-"}|${status}`, () => transcript(s, pos, status));
      r.set("callins", callinsKey(s, pos), () => callins(s, pos, { limit: 6, producer: s.producer && ctl.live }));
      r.set("context", `${callinsKey(s, pos)}`, () => contextList(s, pos));
      r.set("chaptercards", chaptersKey(s, pos), () => chapters(s, pos));
    },
    onEvent() {
      renderStatic();
    },
    onPlay() {},
    destroy() {
      offActions();
      offSub();
      document.removeEventListener("keydown", onKey);
      el.remove();
    },
  };
  layout.onEvent({ type: "init" });
  return layout;
}

function transcript(s, pos, status) {
  const lines = s.heardLines(pos, 3);
  if (!lines.length) return html`<p class="np-status">${status || "Starting…"}</p>`;
  const last = lines[lines.length - 1];
  return html`
    ${lines.map((l) => {
      const caller = s.callinFor(l.seg, l.idx);
      const cur = l === last;
      return html`
        <div class="np-line ${cur ? "cur" : ""}">
          <span class="np-who">${l.line.speaker}${caller ? html`<span class="pill ink">Call-in</span>` : ""}</span>
          <p>${renderSpoken(l.line.text, s.hostIndex(l.line.speaker) ? 0 : 1)}</p>
        </div>`;
    })}
    ${status ? html`<p class="np-status">${status}</p>` : ""}`;
}
