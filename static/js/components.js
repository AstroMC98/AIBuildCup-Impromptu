// Pieces of the show screens that the phone and desktop layouts share. Each is a
// pure function of the store and a playhead position, returning markup.
import { html, renderSpoken, spokenText } from "./html.js";
import { icon, KIND_ICON } from "./icons.js";
import { session } from "./session.js";
import { ring } from "./ring.js";
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

/** The tally lamp. It flickers on (ignite) whenever it comes on. */
export function liveBadge(store, ctl, pos) {
  if (!ctl.live) return html`<span class="badge replay">Replay</span>`;
  if (store.ended) {
    return ctl.atAudioEdge(pos) ? html`<span class="badge ended">Ended</span>`
      : html`<span class="badge live"><span class="dot"></span>Signing off</span>`;
  }
  if (ctl.connection === "reconnecting") return html`<span class="badge ended">Reconnecting</span>`;
  return html`<span class="badge live ignite"><span class="dot"></span>On air</span>`;
}

/** The stage-world loader: the studio clock chasing, with what's happening. */
export function loader(text, { title = "" } = {}) {
  return html`<div class="splash stage">${ring({ chase: true })}${title ? html`<p class="splash-title">${title}</p>` : ""}<p>${text}</p></div>`;
}

/** The house's wait: a small studio clock chasing. It only shows once the wait passes
 *  300 ms (CSS), so a quick load never flashes it. inline: a line inside a card or sheet. */
export function waiting(label = "", { inline = false } = {}) {
  return html`<div class="waiting ${inline ? "inline" : ""}" role="status" aria-label="${label || "Getting it ready"}">
    ${ring({ chase: true, cls: "waiting-ring", r: 42, dot: 3.4 })}${label ? html`<span>${label}</span>` : ""}</div>`;
}

/** The hosts as chips; the one talking is lit, and its ring follows their voice (--lv). */
export function hostChips(store, pos) {
  const hosts = store.plan?.hosts || store.show.hosts || [];
  const at = store.lineAt(pos);
  const speaking = at && !at.done ? at.seg.lines[at.idx]?.speaker : "";
  return html`${hosts.map((h, i) => html`
    <button type="button" class="host-chip ${h.name && h.name === speaking ? "talk" : ""}" data-action="hosts">
      ${avatar(h.name || "?", session.hostColor(i).avatar, 28)}<span>${h.name || "Host"}</span></button>`)}`;
}

export function hostChipsKey(store, pos) {
  const at = store.lineAt(pos);
  return `${store.version}|${at && !at.done ? at.seg.lines[at.idx]?.speaker : ""}`;
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
    if (!store.segments.length && ctl.live && !store.ended) return greenRoom(store, pos, status);
    if (store.inIntro(pos)) return introCard(store);
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
  // As a detour starts, what the producer asked takes that place.
  const detour = cur.seg.interrupt;
  const asked = detour?.text && detourStarts(store, cur.seg) && cur.idx <= 1 && piece.i === 0;
  const prev = !asked && lines.length > 1 && piece.i === 0 && len <= 100 ? lines[0] : null;
  return html`
    ${asked ? html`
      <div class="cap prev asked">
        <span class="cap-who">${store.producer ? "You asked" : "The producer asked"}</span>
        <p>“${detour.text}”</p>
      </div>` : ""}
    ${prev ? html`
      <div class="cap prev">
        <span class="cap-who" style="color:${label(store.hostIndex(prev.line.speaker))}">${prev.line.speaker}</span>
        <p>${renderSpoken(prev.line.text, store.hostIndex(prev.line.speaker) ? 0 : 1)}</p>
      </div>` : ""}
    <div class="cap now" style="--cap:${size}px">
      <span class="cap-who-row">
        <span class="cap-who" style="color:${label(curHost)}">${cur.line.speaker}</span>
        ${caller ? html`<span class="pill pink">${store.isMine(caller) ? "Reading your call-in" : `Reading ${caller.name}'s call-in`}</span>`
          : detour ? html`<span class="pill next">${(store.producer ? DETOUR_MINE : DETOUR_THEIRS)[detour.kind] || "Detour"}</span>` : ""}
      </span>
      <p>${renderSpoken(piece.text, curHost ? 0 : 1)}</p>
    </div>
    ${status ? html`<p class="cap-status small">${status}</p>` : ""}`;
}

// ---- detours: when the producer interrupts --------------------------------------------------
const DETOUR_MINE = { ask: "Answering you", deeper: "Going deeper for you", steer: "Taking your steer", move_on: "Moving on, as you asked" };
const DETOUR_THEIRS = { ask: "Answering the producer", deeper: "Going deeper", steer: "The producer steered", move_on: "Moving on" };
const sameDetour = (a, b) => !!a && !!b && JSON.stringify(a) === JSON.stringify(b);

/** Whether seg is the first segment of a detour (the one the stinger opens). */
function detourStarts(store, seg) {
  if (!seg?.interrupt) return false;
  const before = store.segments[store.segments.indexOf(seg) - 1];
  return !sameDetour(before?.interrupt, seg.interrupt);
}

const cued = new WeakMap();

/** The stinger that opens a detour: the stage's gel flashes amber for 600 ms (never white),
 *  once per detour, and not for someone tuning in halfway through one. */
export function detourCue(store, pos, el) {
  const seg = store.segmentAt(pos);
  if (!detourStarts(store, seg) || cued.get(store) === seg.seq) return;
  cued.set(store, seg.seq);
  if (pos - seg.start > 2 * store.rate) return;
  el.classList.add("gel-flash");
  el.style.setProperty("--gel", "#FFB000");
  setTimeout(() => {
    el.style.removeProperty("--gel");
    el.classList.remove("gel-flash");
  }, 600);
}

// ---- the green room ---------------------------------------------------------------------
// Before the first line is heard, the wait shows real work: the theme plays while the
// hosts plan (the clock chases, bars move), then the plan's title and chapters arrive.
// Chapter cards drop in once; later re-renders (the status line changing) don't replay them.
const rundownShown = new WeakMap();

const themeBars = () => html`<span class="gr-theme" aria-hidden="true">${Array.from({ length: 24 }, (_, i) => html`<i style="--i:${i}"></i>`)}</span>`;

/** The show intro on air: while the theme plays, the show's name owns the stage; the
 *  captions take over as the welcome begins. */
const introShown = new WeakSet();
function introCard(store) {
  const name = store.show.show_name;
  const arrive = !introShown.has(store);   // the name lands once, not on every re-render
  introShown.add(store);
  return html`
    <div class="intro-card">
      ${themeBars()}
      <h2 class="ic-title ${arrive ? "arrive" : ""}">${name || store.plan?.title || store.show.title || "Theme music"}</h2>
      <p class="ic-sub">${name ? (store.plan?.title || store.show.title || "") : "Theme music"}</p>
    </div>`;
}

function greenRoom(store, pos, status) {
  const beats = store.beats();
  const seen = rundownShown.get(store) || 0;
  rundownShown.set(store, Math.max(seen, beats.length));
  const theme = store.inIntro?.(pos);
  return html`
    <div class="green-room">
      ${theme && store.show.show_name ? html`<p class="gr-show">${store.show.show_name}</p>` : ""}
      <span class="gr-clock">${ring({ chase: true, cls: "gr-ring" })}<span class="gr-step">${status || "Getting the studio ready"}</span></span>
      ${theme ? themeBars() : ""}
      ${store.plan?.title ? html`<h2 class="gr-title ${seen ? "" : "arrive"}">${store.plan.title}</h2>` : ""}
      ${beats.length ? html`<ol class="gr-rundown">${beats.map((b, i) => html`
        <li class="${i >= seen ? "drop" : ""}" style="--i:${Math.max(0, i - seen)}"><span>${i + 1}</span>${b.title}</li>`)}</ol>` : ""}
    </div>`;
}

// ---- kinetic captions ------------------------------------------------------------------
// captions() renders a piece of a line once; captionTick() then lights its words as
// they're said, every frame, without re-rendering: words are wrapped in spans the first
// time, weighted by their spoken characters, and the current word's underline fills
// (--wp). A new piece first narrows (the width axis) until it fits, then shrinks.

/** How far through the piece on screen the hosts are, 0 to 1. */
function pieceProgress(store, pos) {
  const at = store.lineAt(pos);
  const lines = store.heardLines(pos, 2);
  const cur = lines[lines.length - 1];
  if (!at || !cur || at.idx !== cur.idx || at.done) return 1;
  const text = cur.line.text;
  const frac = at.frac ?? 1;
  if (spokenText(text).length <= CHUNK_CHARS * 1.3) return frac;
  const parts = chunks(text);
  const found = parts.findIndex((p) => frac < p.to);
  const part = parts[found < 0 ? parts.length - 1 : found];
  return Math.max(0, Math.min(1, (frac - part.from) / Math.max(1e-6, part.to - part.from)));
}

function wordify(p) {
  const walker = document.createTreeWalker(p, NodeFilter.SHOW_TEXT);
  const texts = [];
  while (walker.nextNode()) texts.push(walker.currentNode);
  for (const t of texts) {
    if (t.parentElement.closest(".cue")) continue;
    const frag = document.createDocumentFragment();
    for (const part of t.textContent.split(/(\s+)/)) {
      if (!part) continue;
      if (/^\s+$/.test(part)) frag.append(part);
      else {
        const s = document.createElement("span");
        s.className = "w";
        s.textContent = part;
        frag.append(s);
      }
    }
    t.replaceWith(frag);
  }
  p.querySelectorAll(".cue").forEach((c) => c.classList.add("w"));
  return [...p.querySelectorAll(".w")];
}

/** Narrow the caption before shrinking it, until it fits its region (height and width). */
export function fitCaption(region) {
  const now = region.querySelector(".cap.now");
  const p = now?.querySelector("p");
  if (!p) return;
  const fits = () => region.scrollHeight <= region.clientHeight + 1 && p.scrollWidth <= p.clientWidth + 1;
  p.style.fontSize = "";
  for (const s of [100, 90, 80, 70, 62]) {
    now.style.fontStretch = `${s}%`;
    if (fits()) return;
  }
  for (let z = 0.92; z >= 0.6; z -= 0.08) {
    p.style.fontSize = `calc(var(--cap, 30px) * ${z.toFixed(2)})`;
    if (fits()) return;
  }
}

/** Light the words of the caption on screen; call every frame. calm: show it all at once. */
export function captionTick(region, store, pos, { calm = false } = {}) {
  const p = region?.querySelector(".cap.now p");
  if (!p) return;
  if (!p._words) {
    p._words = wordify(p);
    let acc = 0;
    p._ends = p._words.map((w) => (acc += w.classList.contains("cue") ? 2
      : Math.max(1, w.textContent.replace(/[^\p{L}\p{N}']/gu, "").length) + 1));
    p._total = acc || 1;
    p.classList.add("kinetic");
    fitCaption(region);
  }
  const spoken = (calm ? 1 : pieceProgress(store, pos)) * p._total;
  let now = p._ends.findIndex((e) => spoken < e);
  if (now < 0) now = p._words.length;
  if (p._now !== now) {
    p._words.forEach((w, i) => {
      w.classList.toggle("said", i < now);
      w.classList.toggle("now", i === now);
    });
    p._now = now;
  }
  if (now < p._words.length) {
    const start = now ? p._ends[now - 1] : 0;
    p._words[now].style.setProperty("--wp", ((spoken - start) / (p._ends[now] - start)).toFixed(3));
  }
}

/** The line being said, for screen readers, keyed so it's announced once per line. (The
 *  captions themselves aren't a live region: they re-render per chunk, and their words are
 *  rewritten as they're said.) */
export function lineNews(store, pos) {
  const at = store.lineAt(pos);
  if (!at) return { key: "", text: "" };
  const line = at.seg.lines[at.idx];
  return { key: `${at.seg.seq}:${at.idx}`, text: `${line.speaker}: ${spokenText(line.text)}` };
}

export function captionsKey(store, ctl, pos) {
  const at = store.lineAt(pos);
  const piece = at ? currentPiece(at.seg.lines[at.idx]?.text || "", at.frac ?? 1).i : 0;
  return `${store.version}|${at ? at.seg.seq + ":" + at.idx + ":" + piece : "-"}|${statusText(store, ctl, pos)}`;
}

/** Detour segments that have been heard by pos. */
const heardDetours = (store, pos) => store.segments.filter((g) => g.interrupt && g.start <= pos && g.samples !== 0);

/** The segmented chapter bar; widths follow the plan until a chapter has really happened.
 *  Detours drop in as amber pieces where they were heard. */
export function chapterBar(store, pos, { knob = false } = {}) {
  const parts = store.chapterBar(pos);
  if (!parts.length) return html`<span class="cb-part"><span class="cb-fill" style="width:0"></span></span>`;
  const detours = heardDetours(store, pos);
  const pct = (f) => `${(Math.max(0, Math.min(1, f)) * 100).toFixed(1)}%`;
  return html`${parts.map((p, i) => {
    const end = parts[i + 1]?.start;
    const marks = p.start === undefined ? [] : detours.filter((g) => g.start >= p.start && (end === undefined || g.start < end));
    return html`
      <span class="cb-part ${p.state}" style="flex:${p.flex.toFixed(0)}" title="${p.title}">
        <span class="cb-fill" style="width:${pct(p.fill)}"></span>
        ${marks.map((g) => {
          const x = Math.min(0.97, (g.start - p.start) / p.flex);
          return html`<span class="cb-detour" style="left:${pct(x)};width:${pct(Math.min(store.segLength(g) / p.flex, 1 - x))}"></span>`;
        })}
        ${knob && p.state === "now" ? html`<span class="cb-knob" style="left:${pct(p.fill)}"></span>` : ""}
      </span>`;
  })}`;
}

export function chapterBarKey(store, pos) {
  const now = store.chapterAt(pos);
  const parts = store.chapterBar(pos);
  return `${store.version}|${now}|${Math.round((parts[now]?.fill || 0) * 200)}|${heardDetours(store, pos).length}`;
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
        ${status && view.label ? lineState(lineView(store, c, pos), "callin-state") : ""}
      </div>
      ${producer && c.status === "queued" ? html`
        <button class="icon-btn small" data-action="remove-comment" data-id="${c.id}" aria-label="Remove this call-in">${icon("close", 16)}</button>` : ""}
    </div>`;
}

export function callinsKey(store, pos) {
  const seg = store.segmentAt(pos);
  // While a call-in is still on its way, re-render every second so its countdown moves.
  const waiting = [...store.comments.values()].some((c) => {
    if (c.status === "queued") return true;
    if (c.status === "promised") {
      const said = c.promised_seq ? store.segBySeq.get(c.promised_seq) : null;
      return !!said && pos < said.start;
    }
    const at = c.status === "on_deck" ? store.segBySeq.get(c.seq) : null;
    return c.status === "on_deck" && (!at || pos < at.start);
  });
  const second = waiting ? Math.floor(pos / store.rate) : "";
  return `${store.version}|${seg ? seg.seq : 0}|${seg && pos >= seg.start + store.segLength(seg) ? 1 : 0}|${second}`;
}

// ---- call-in lines -------------------------------------------------------------------------
// Call-ins wait on lit phone lines: a lamp that says where each one is (blinking amber:
// waiting; steady amber: up next or coming back; a chasing ring: being looked up; red: on
// air; off: done), with a few of its words and an honest ETA.

/** Where a call-in is: its lamp, and what its line says. */
export function lineView(store, c, pos) {
  const v = store.commentView(c, pos);
  const left = (start) => fmtTime(Math.max(0, (start - pos) / store.rate));
  switch (c.status) {
    case "queued": {
      if (c.eta == null) return { state: "wait", lamp: "amber blink", says: "Waiting for a gap" };
      const sec = (c.eta - pos) / store.rate;
      return { state: "wait", lamp: "amber blink", says: sec <= 1 ? "Any moment" : `About ${Math.ceil(sec / 5) * 5} s` };
    }
    case "promised": {
      const said = c.promised_seq ? store.segBySeq.get(c.promised_seq) : null;
      if (said && pos < said.start) return { state: "next", lamp: "amber", says: `Up next, ${left(said.start)}` };
      if (c.research === "pending") return { state: "look", lamp: "chase", says: "Looking it up" };
      return { state: "back", lamp: "amber", says: "Coming back", back: true };
    }
    case "on_deck": {
      if (v.tone === "live") return { state: "air", lamp: "red", says: "On air" };
      if (v.tone === "next") {
        const seg = c.seq ? store.segBySeq.get(c.seq) : null;
        return { state: "next", lamp: "amber", says: seg ? `Up next, ${left(seg.start)}` : "Up next" };
      }
      return { state: "done", lamp: "", says: v.label };
    }
    case "missed": return { state: "missed", lamp: "", says: "Not answered" };
    default: return { state: "done", lamp: "", says: v.label };
  }
}

function lineState(lv, cls) {
  return html`<span class="${cls} ${lv.state}"><span class="lamp ${lv.lamp}" aria-hidden="true"></span>${lv.back ? icon("ret", 14, 2.4) : ""}${lv.says}</span>`;
}

const ACTIVE = { air: 0, next: 1, back: 2, look: 2, wait: 3 };
const linesSeen = new WeakMap();

/** The live call-ins as phone lines (phones), on air first. Your own unanswered one stays,
 *  struck through, until you call in again. Keyed with callinsKey. */
export function callinLines(store, pos, { limit = 3 } = {}) {
  const all = [...store.comments.values()].filter((c) => c.status !== "removed");
  const mine = all.filter((c) => store.isMine(c)).sort((a, b) => b.id - a.id)[0];
  const rows = all.map((c) => ({ c, lv: lineView(store, c, pos) }))
    .filter(({ c, lv }) => lv.state in ACTIVE || (c === mine && lv.state === "missed"))
    .sort((a, b) => (ACTIVE[a.lv.state] ?? 4) - (ACTIVE[b.lv.state] ?? 4) || a.c.id - b.c.id)
    .slice(0, limit);
  const seen = linesSeen.get(store) || new Set();
  linesSeen.set(store, seen);
  return html`${rows.map(({ c, lv }) => {
    const arrive = !seen.has(c.id);
    seen.add(c.id);
    return html`
      <div class="callin-line ${lv.state} ${arrive ? "arrive" : ""}">
        <span class="lamp ${lv.lamp}" aria-hidden="true"></span>
        <span class="line-msg"><strong>${store.isMine(c) ? "You" : c.name}</strong>${c.text}</span>
        <span class="line-says">${lv.back ? icon("ret", 14, 2.4) : ""}${lv.says}</span>
      </div>`;
  })}`;
}

/** What a screen reader hears about your own call-in: once per change of state, never per second. */
export function myCallinNews(store, pos) {
  const mine = [...store.comments.values()].filter((c) => store.isMine(c)).sort((a, b) => b.id - a.id)[0];
  if (!mine) return "";
  const said = { wait: "Your call-in is waiting for a gap.", next: "Your call-in is up next.", air: "Your call-in is on air.",
                 back: "The hosts read your call-in and are coming back to it.", look: "The hosts are looking up your call-in.",
                 missed: "The hosts didn't get to your call-in." };
  return said[lineView(store, mine, pos).state] || "";
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
    ${(x.search_suggestions || []).slice(0, 1).map(searchSuggestions)}`;
}

/** Google's Search Suggestions, exactly as Google sends them (sandboxed, links open in a
 *  new tab). The design only frames them: a dashed box with a label, on their own row. */
export function searchSuggestions(markup) {
  return html`<figure class="g-frame"><figcaption>Google Search suggestions</figcaption>
    <iframe class="search-suggestions" title="Google Search suggestions" loading="lazy" referrerpolicy="no-referrer"
      sandbox="allow-popups allow-popups-to-escape-sandbox" srcdoc="${`<base target="_blank">${markup}`}"></iframe></figure>`;
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
