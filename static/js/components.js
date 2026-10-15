// Pieces of the show screens that the phone and desktop layouts share. Each is a
// pure function of the store and a playhead position, returning markup.
import { html, renderSpoken, spokenText } from "./html.js";
import { icon, KIND_ICON } from "./icons.js";
import { session } from "./session.js";
import { callerColor, initial } from "./theme.js";
import { fmtTime } from "./util.js";

export function avatar(name, color, size = 34, cls = "") {
  return html`<span class="avatar ${cls}" style="--av:${color};--size:${size}px" aria-hidden="true">${initial(name)}</span>`;
}

export function hostAvatars(store, size = 34) {
  const hosts = store.plan?.hosts || store.show.hosts || [];
  return html`<span class="avatars">${hosts.map((h, i) => avatar(h.name || "?", session.hostColor(i).avatar, size))}</span>`;
}

export function liveBadgeKey(store, ctl, pos) {
  return `${store.ended}|${ctl.connection}|${store.ended && ctl.live && !ctl.atAudioEdge(pos)}`;
}

export function liveBadge(store, ctl, pos) {
  if (!ctl.live) return html`<span class="badge replay">Replay</span>`;
  if (store.ended) {
    return ctl.atAudioEdge(pos) ? html`<span class="badge ended">Ended</span>`
      : html`<span class="badge live"><span class="dot"></span>Signing off</span>`;
  }
  if (ctl.connection === "reconnecting") return html`<span class="badge ended">Reconnecting</span>`;
  return html`<span class="badge live"><span class="dot"></span>Live</span>`;
}

/** The status line shown when there's no caption to show, or under the captions. */
export function statusText(store, ctl, pos) {
  if (store.ended === "error" && (!ctl.live || ctl.atAudioEdge(pos))) return "The pod hit a problem and stopped.";
  if (!store.segments.length) {
    if (store.state === "planning") return store.inIntro(pos) ? "Theme music, while the hosts get ready…" : "Planning the episode…";
    if (store.state === "ended") return "This show ended before it started.";
    return "Writing the cold open…";
  }
  if (!ctl.play.started) return "";
  if (ctl.play.paused) return ctl.live ? "Paused. You'll pick up where you left off." : "Paused";
  const endOfShow = store.ended && (ctl.live
    ? pos >= ctl.timeline.end - 0.2 * store.rate
    : pos >= (store.clock.audio_end || 0) - 0.3 * store.rate);
  if (endOfShow || ctl.play.ended) return "That's the show. Thanks for listening.";
  if (store.inIntro(pos)) return "Theme music";
  if (ctl.play.buffering || ctl.atAudioEdge(pos)) {
    return store.state === "holding" ? "Waiting for listeners…" : "The hosts are writing what comes next.";
  }
  return "";
}

const CHUNK_CHARS = 110;

/** A long line split into sentence-sized pieces, each with the share of the line it covers. */
function chunks(text) {
  const sentences = text.match(/[^.!?…]+(?:[.!?…]+["')\]]*|$)\s*/g) || [text];
  const out = [];
  for (const s of sentences) {
    const last = out[out.length - 1];
    if (last && spokenText(last).length + spokenText(s).length <= CHUNK_CHARS) out[out.length - 1] = last + s;
    else out.push(s);
  }
  const total = out.reduce((n, c) => n + Math.max(1, spokenText(c).length), 0);
  let acc = 0;
  return out.map((c) => {
    const from = acc / total;
    acc += Math.max(1, spokenText(c).length);
    return { text: c.trim(), from, to: acc / total };
  });
}

/** The part of the current line being spoken: the whole line if it's short, else one chunk. */
function currentPiece(text, frac) {
  if (spokenText(text).length <= CHUNK_CHARS * 1.3) return { text, i: 0, n: 1 };
  const parts = chunks(text);
  const found = parts.findIndex((p) => frac < p.to);
  const at = found < 0 ? parts.length - 1 : found;
  return { text: parts[at].text, i: at, n: parts.length };
}

/** Big captions: the previous line dimmed, the current line (or the part of it being said) large. */
export function captions(store, ctl, pos, { big = 30 } = {}) {
  const at = store.lineAt(pos);
  const status = statusText(store, ctl, pos);
  if (!at) {
    return html`<p class="cap-status">${status || "Starting…"}</p>`;
  }
  const lines = store.heardLines(pos, 2);
  const cur = lines[lines.length - 1];
  const caller = store.callinFor(cur.seg, cur.idx);
  const curHost = store.hostIndex(cur.line.speaker);
  const label = (i) => session.hostColor(i).label;
  const piece = currentPiece(cur.line.text, at.idx === cur.idx ? at.frac ?? 1 : 1);
  const len = spokenText(piece.text).length;
  const size = Math.round(big * (len > 130 ? 0.82 : len > 90 ? 0.9 : 1));
  // The previous line only while the current one starts; a continuing chunk stands alone.
  const prev = lines.length > 1 && piece.i === 0 && len <= 100 ? lines[0] : null;
  return html`
    ${prev ? html`
      <div class="cap prev">
        <span class="cap-who" style="color:${label(store.hostIndex(prev.line.speaker))}">${prev.line.speaker}</span>
        <p>${renderSpoken(prev.line.text, store.hostIndex(prev.line.speaker) ? 0 : 1)}</p>
      </div>` : ""}
    <div class="cap now" style="--cap:${size}px">
      <span class="cap-who-row">
        <span class="cap-who" style="color:${label(curHost)}">${cur.line.speaker}</span>
        ${caller ? html`<span class="pill pink">${store.isMine(caller) ? "Reading your call-in" : "Reading a call-in"}</span>` : ""}
      </span>
      <p>${renderSpoken(piece.text, curHost ? 0 : 1)}</p>
    </div>
    ${status ? html`<p class="cap-status small">${status}</p>` : ""}`;
}

export function captionsKey(store, ctl, pos) {
  const at = store.lineAt(pos);
  const piece = at ? currentPiece(at.seg.lines[at.idx]?.text || "", at.frac ?? 1).i : 0;
  return `${store.version}|${at ? at.seg.seq + ":" + at.idx + ":" + piece : "-"}|${statusText(store, ctl, pos)}`;
}

/** The segmented chapter bar; widths follow the plan until a chapter has really happened. */
export function chapterBar(store, pos, { knob = false } = {}) {
  const parts = store.chapterBar(pos);
  if (!parts.length) return html`<span class="cb-part"><span class="cb-fill" style="width:0"></span></span>`;
  return html`${parts.map((p) => html`
    <span class="cb-part ${p.state}" style="flex:${p.flex.toFixed(0)}" title="${p.title}">
      <span class="cb-fill" style="width:${(p.fill * 100).toFixed(1)}%"></span>
      ${knob && p.state === "now" ? html`<span class="cb-knob" style="left:${(p.fill * 100).toFixed(1)}%"></span>` : ""}
    </span>`)}`;
}

export function chapterBarKey(store, pos) {
  const now = store.chapterAt(pos);
  const parts = store.chapterBar(pos);
  return `${store.version}|${now}|${Math.round((parts[now]?.fill || 0) * 200)}`;
}

export function chapterLabel(store, pos) {
  const beats = store.beats();
  if (!beats.length) return "";
  const i = store.chapterAt(pos);
  return `Chapter ${i + 1} of ${beats.length}: ${beats[i]?.title || ""}`;
}

/** Call-ins, newest first, with "On air now" pulled to the top as a card. */
export function callins(store, pos, { limit = 0, onAirCard = true, producer = false } = {}) {
  let list = [...store.comments.values()].filter((c) => c.status !== "removed").sort((a, b) => b.id - a.id);
  const onAir = onAirCard ? list.find((c) => store.commentView(c, pos).tone === "live") : null;
  if (onAir) list = list.filter((c) => c !== onAir);
  if (limit) list = list.slice(0, limit);
  if (!onAir && !list.length) {
    return html`<p class="empty">No call-ins yet. Be the first: the hosts read every message on air.</p>`;
  }
  return html`
    ${onAir ? html`
      <div class="onair-card">
        <span class="badge live small">On air now</span>
        ${callinRow(store, onAir, pos, { status: false })}
      </div>` : ""}
    <ul class="callin-list">${list.map((c) => html`<li>${callinRow(store, c, pos, { producer })}</li>`)}</ul>`;
}

export function callinRow(store, c, pos, { status = true, producer = false } = {}) {
  const view = store.commentView(c, pos);
  const att = c.context_id ? store.context.get(c.context_id) : null;
  const found = c.research_id ? store.context.get(c.research_id) : null;
  return html`
    <div class="callin">
      ${avatar(c.name, callerColor(c.name), 30)}
      <div class="callin-body">
        <span class="callin-head"><strong>${c.name}</strong>${c.role === "producer" && c.name !== "Producer" ? html` <span class="tag">Producer</span>` : ""}
          ${store.isMine(c) ? html` <span class="tag">You</span>` : ""}
          ${c.at !== undefined ? html`<span class="time">${fmtTime(c.at / store.rate)}</span>` : ""}</span>
        <span class="callin-text">${c.text}</span>
        ${att ? html`<span class="attach">${icon(KIND_ICON[att.kind] || "file", 14)}${att.name}, added as context</span>` : ""}
        ${found ? html`<span class="attach">${icon("search", 14)}Looked up: ${(found.sources || []).slice(0, 3).map((s) => s.domain || s.title).join(", ") || found.name}</span>` : ""}
        ${status && view.label ? html`<span class="pill ${view.tone}">${view.label}</span>` : ""}
      </div>
      ${producer && c.status === "queued" ? html`
        <button class="icon-btn small" data-action="remove-comment" data-id="${c.id}" aria-label="Remove this call-in">${icon("close", 16)}</button>` : ""}
    </div>`;
}

/** Your own latest call-in while it's on its way or on air (phones, where the full
 *  list lives in Now playing). Keyed with callinsKey. */
export function myCallin(store, pos) {
  const latest = [...store.comments.values()].filter((c) => store.isMine(c)).sort((a, b) => b.id - a.id)[0];
  const view = latest && store.commentView(latest, pos);
  if (!view || !["wait", "next", "live"].includes(view.tone)) return "";
  return html`<span class="my-callin pill ${view.tone}">${icon("chat", 14)}Your call-in: ${view.label}</span>`;
}

export function callinsKey(store, pos) {
  const seg = store.segmentAt(pos);
  // While a call-in is still on its way, re-render every second so its countdown moves.
  const waiting = [...store.comments.values()].some((c) => {
    if (c.status === "queued") return true;
    const at = c.status === "on_deck" ? store.segBySeq.get(c.seq) : null;
    return c.status === "on_deck" && (!at || pos < at.start);
  });
  const second = waiting ? Math.floor(pos / store.rate) : "";
  return `${store.version}|${seg ? seg.seq : 0}|${seg && pos >= seg.start + store.segLength(seg) ? 1 : 0}|${second}`;
}

/** Recent call-in bubbles over the live view. */
export function bubbles(store) {
  const list = [...store.comments.values()].filter((c) => c.status !== "removed" && c.status !== "declined")
    .sort((a, b) => b.id - a.id).slice(0, 2).reverse();
  return html`${list.map((c) => {
    const att = c.context_id ? store.context.get(c.context_id) : null;
    return html`
      <div class="bubble">
        ${avatar(c.name, callerColor(c.name), 26)}
        <span class="bubble-body"><span><strong>${c.name}</strong> ${c.text}</span>
          ${att ? html`<span class="attach light">${icon(KIND_ICON[att.kind] || "file", 14)}${att.name}, added as context</span>` : ""}
        </span>
      </div>`;
  })}`;
}

/** Context the hosts are using: the main source, then everything added mid-show. */
export function contextList(store, pos, { variant = "list", pills = false } = {}) {
  const items = store.contextItems();
  return html`${items.map((x) => {
    const view = x.main ? { label: store.ended ? "Main source" : "Main source, in use", pill: store.ended ? "Used" : "In use", tone: "in" }
      : store.contextView(x, pos);
    const meta = x.main ? (pills ? "Main source" : view.label)
      : pills ? (x.kind === "research" ? researchFor(store, x)
        : x.comment_id ? `From ${x.added_by}'s call-in` : x.from_creator ? (x.kind === "text" ? "Note" : `${cap(x.kind)}, from the producer`) : `${cap(x.kind)} from ${x.added_by}`)
      : view.label;
    return html`
      <div class="ctx-item ${variant} ${x.kind === "research" ? "research" : ""}">
        <span class="ctx-icon" aria-hidden="true">${icon(KIND_ICON[x.kind] || "file", 18)}</span>
        <span class="ctx-text"><span class="ctx-name">${x.name}</span><span class="ctx-meta">${meta}</span>
          ${x.kind === "research" ? researchSources(x) : ""}</span>
        ${pills ? html`<span class="pill ${view.tone}">${view.pill}</span>` : ""}
      </div>`;
  })}`;
}

/** Changes only when the context list would: research cards hold Google's suggestions
 *  in frames, which go blank and reload whenever they're re-rendered. */
export function contextKey(store, pos) {
  const items = [...store.context.values()].map((x) => {
    const seg = x.used_seq ? store.segBySeq.get(x.used_seq) : null;
    return `${x.id}:${x.status}:${seg && pos >= seg.start ? 1 : 0}`;
  });
  return `${items.join(",")}|${store.ended ? 1 : 0}|${(store.show.sources || []).join("/")}`;
}

function researchFor(store, x) {
  if (x.comment_id) return `Looked up for ${store.comments.get(x.comment_id)?.name || "a"}'s call-in`;
  const beat = store.beats()[x.for_beat];
  return beat ? `Looked up for “${beat.title}”` : "Looked up by the research desk";
}

/** Where a lookup's facts came from, and Google's Search Suggestions for it (the
 *  grounding terms ask for them with the result; their HTML runs sandboxed). */
function researchSources(x) {
  const sources = (x.sources || []).slice(0, 4);
  return html`
    ${sources.length ? html`<span class="ctx-sources">${sources.map((s) => html`
      <a href="${s.url}" target="_blank" rel="noopener noreferrer nofollow">${s.domain || s.title}</a>`)}</span>` : ""}
    ${(x.search_suggestions || []).slice(0, 1).map((h) => html`
      <iframe class="search-suggestions" title="Google Search suggestions" sandbox="allow-popups allow-popups-to-escape-sandbox"
        srcdoc="${h}" loading="lazy"></iframe>`)}`;
}

function cap(s) {
  return s ? s[0].toUpperCase() + s.slice(1) : "";
}

/** Chapters as cards (carousel) or rows (rundown). */
export function chapters(store, pos, { rows = false } = {}) {
  const now = store.chapterAt(pos);
  return html`${store.beats().map((b, i) => {
    const start = store.chapterStarts.get(i);
    const state = i < now ? "done" : i === now ? "now" : i === now + 1 ? "next" : "later";
    const meta = state === "now" ? "Now" : start !== undefined && state === "done" ? fmtTime(start / store.rate)
      : state === "next" ? "Next" : start !== undefined ? fmtTime(start / store.rate) : "Later";
    const jump = start !== undefined && start !== null;   // chapters that have happened can be jumped to
    return html`
      <li class="chapter ${state} ${rows ? "row" : ""} ${jump ? "jump" : ""}" ${jump
        ? html`data-action="seek-chapter" data-start="${start}" role="button" tabindex="0" aria-label="Jump to ${b.title}"` : ""}>
        <span class="chapter-meta">${meta}</span>
        <span class="chapter-title">${b.title}</span>
      </li>`;
  })}`;
}

export function chaptersKey(store, pos) {
  return `${store.version}|${store.chapterAt(pos)}`;
}

/** Research sources that are web pages: only http(s) links are ever made clickable
 *  (the server keeps only those too, but a link is the browser's to guard). */
export function webSources(sources) {
  return (sources || []).filter((src) => /^https?:\/\//i.test(src?.url || ""));
}

/** Show notes: where a memo pod came from and what its research drew on, and any
 *  pod's theme music. "" when there's nothing to say. */
export function showNotes(store) {
  const notes = store.show.show_notes;
  const music = notes?.music || (store.show.music?.label ? `Theme music: ${store.show.music.label}.` : "");
  const sources = webSources(notes?.sources);
  if (!notes?.credit && !sources.length && !music) return "";
  return html`
    ${notes?.credit ? html`<p class="sn-credit">${icon("mic", 16)}<span>${notes.credit}</span></p>` : ""}
    ${sources.length ? html`
      <p class="sn-h">What the research drew on</p>
      <ul class="sn-sources">${sources.map((src) => html`
        <li><a href="${src.url}" target="_blank" rel="noopener noreferrer">${src.title || src.domain}</a>
          <span class="faint small">${src.domain}</span></li>`)}</ul>` : ""}
    ${music ? html`<p class="faint small">${music}</p>` : ""}`;
}

export function talking(store, pos) {
  const at = store.lineAt(pos);
  if (!at || at.done) return "";
  return `${at.seg.lines[at.idx].speaker} is talking`;
}
