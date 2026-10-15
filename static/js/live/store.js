// Everything known about one show, fed by socket events (live) or by the saved
// show (replay). Views never ask "what's happening?" without a playhead: each
// listener hears the show at their own position, so captions, chapters and
// call-in statuses are all selectors of pos.
import { spokenText } from "../html.js";
import { joinNames, store as local } from "../util.js";

const CHARS_PER_SECOND = 14;  // until a segment's real length is known

export class ShowStore {
  constructor({ id, role, shareUrl, rate = 24000 }) {
    this.id = id;
    this.role = role;
    this.shareUrl = shareUrl;
    this.rate = rate;
    this.mode = "live";
    this.show = {};
    this.plan = null;
    this.segments = [];
    this.segBySeq = new Map();
    this.chapterStarts = new Map();
    this.comments = new Map();
    this.context = new Map();
    this.styles = new Map();     // style rev -> { accent, label, vibe }
    this.style = { rev: 0 };
    this.hearts = 0;
    this.burst = 0;
    this.listeners = 0;
    this.state = "planning";
    this.ended = null;
    this.saved = null;
    this.clock = { live: 0, audio_end: 0, playing: false, speed: 1, at: performance.now() };
    this.version = 0;
    this.mine = new Set(local.get(`gp_mine_${id}`, []));
    this._subs = new Set();
  }

  get producer() { return this.role === "producer"; }

  on(fn) {
    this._subs.add(fn);
    return () => this._subs.delete(fn);
  }

  _changed(event) {
    this.version++;
    for (const fn of this._subs) fn(event);
  }

  remember(clientId) {
    this.mine.add(clientId);
    local.set(`gp_mine_${this.id}`, [...this.mine].slice(-50));
  }

  // ---- feeding -----------------------------------------------------------------

  applySnapshot(s) {
    this.mode = "live";
    this.role = s.role;
    this.canInterrupt = Boolean(s.can_interrupt);
    if (s.share_url) this.shareUrl = s.share_url;
    this.rate = s.rate || this.rate;
    this.show = s.show;
    this.plan = s.plan;
    this.segments = [];
    this.segBySeq.clear();
    for (const seg of s.segments) this._addSegment(seg);
    this.chapterStarts = new Map(s.chapters.map((c) => [c.i, c.start]));
    this.comments = new Map(s.comments.map((c) => [c.id, c]));
    this.context = new Map(s.context.map((x) => [x.id, x]));
    this.style = s.style;
    for (const st of s.styles || []) this.styles.set(st.rev, st);
    if (!this.styles.has(0)) this.styles.set(0, { accent: s.show.accent, label: s.show.style_label, vibe: s.show.vibe });
    this.styles.set(s.style.rev, s.style);
    this.hearts = s.hearts;
    this.state = s.state;
    this.ended = s.ended;
    this.saved = s.saved;
    this._clock(s.clock);
    this._changed({ type: "snapshot" });
  }

  loadReplay(data) {
    this.mode = "replay";
    this.role = data.role;
    this.shareUrl = data.share_url;
    this.show = data.show;
    const beats = data.show.beats || [];
    this.plan = { title: data.show.title, tagline: data.show.tagline, about: data.show.about,
                  hosts: data.show.hosts, beats, accent: data.show.accent };
    for (const seg of data.segments) this._addSegment(seg);
    beats.forEach((b, i) => { if (b.start !== undefined && b.start !== null) this.chapterStarts.set(i, b.start); });
    for (const c of data.comments) this.comments.set(c.id, c);
    for (const x of data.context) this.context.set(x.id, x);
    this.styles.set(0, { accent: data.show.accent, label: data.show.style_label, vibe: data.show.vibe });
    for (const st of data.show.styles || []) {
      if (st.accent) this.styles.set(st.rev, st);
    }
    this.hearts = data.show.counts?.hearts || 0;
    this.state = "ended";
    this.ended = data.show.ended_reason || data.show.status;
    this.clock = { live: data.show.samples, audio_end: data.show.samples, playing: false, speed: 1, at: performance.now() };
    this._changed({ type: "replay" });
  }

  apply(e) {
    switch (e.type) {
      case "status": this.state = e.state; break;
      case "plan":
        this.plan = e;
        Object.assign(this.show, { title: e.title, tagline: e.tagline, about: e.about, hosts: e.hosts });
        break;
      case "segment": this._addSegment({ ...e, samples: null }); break;
      case "segment_end": {
        const seg = this.segBySeq.get(e.seq);
        if (seg) seg.samples = e.samples;
        break;
      }
      case "chapter": this.chapterStarts.set(e.i, e.start); break;
      case "truncate": this._truncate(e); break;
      case "comment": this.comments.set(e.id, { ...this.comments.get(e.id), ...e }); break;
      case "context": this.context.set(e.id, { ...this.context.get(e.id), ...e }); break;
      case "restyle":
        this.style = e;
        this.styles.set(e.rev, e);
        break;
      case "react":
        this.hearts = e.total;
        this.burst = e.burst;
        break;
      case "tick":
        this.listeners = e.listeners;
        this._clock(e);
        this._changed(e);
        return;  // ticks don't bump the version: nothing structural changed
      case "ended": this.ended = e.reason; this.state = "ended"; break;
      case "saved": this.saved = e; break;
      default: return;
    }
    this._changed(e);
  }

  noteAudio(seq, samples) {
    const seg = this.segBySeq.get(seq);
    if (seg) seg.received = (seg.received || 0) + samples;
  }

  _clock(c) {
    this.clock = { live: c.live, audio_end: c.audio_end, playing: c.playing, speed: c.speed || 1, at: performance.now() };
  }

  /** The producer interrupted at e.at: audio after it was never heard. */
  _truncate(e) {
    for (const seg of this.segments) if (seg.seq > e.keep_seq) this.segBySeq.delete(seg.seq);
    this.segments = this.segments.filter((seg) => seg.seq <= e.keep_seq);
    const cut = e.cut && this.segBySeq.get(e.cut.seq);
    if (cut) this._addSegment({ ...cut, lines: e.cut.lines, samples: e.cut.samples });
    for (const [i, start] of this.chapterStarts) if (start >= e.at) this.chapterStarts.delete(i);
    this.clock = { ...this.clock, audio_end: e.at, live: Math.min(this.clock.live, e.at), at: performance.now() };
  }

  _addSegment(seg) {
    const weights = seg.lines.map((l) => Math.max(8, spokenText(l.text).length));
    const total = weights.reduce((a, b) => a + b, 0) || 1;
    const s = { ...seg, weights, total, estimate: (total / CHARS_PER_SECOND) * this.rate, received: seg.received || 0 };
    const old = this.segBySeq.get(s.seq);
    if (old) Object.assign(old, s);
    else {
      this.segments.push(s);
      this.segments.sort((a, b) => a.seq - b.seq);
      this.segBySeq.set(s.seq, s);
    }
  }

  // ---- selectors -------------------------------------------------------------------

  /** The server's live edge, extrapolated between ticks. */
  liveEstimate() {
    const c = this.clock;
    if (!c.playing) return c.live;
    const dt = ((performance.now() - c.at) / 1000) * c.speed * this.rate;
    return Math.min(c.audio_end, c.live + dt);
  }

  segLength(seg) {
    if (seg.samples !== null && seg.samples !== undefined) return seg.samples;
    return Math.max(seg.received || 0, seg.estimate);
  }

  segmentAt(pos) {
    let found = null;
    for (const seg of this.segments) {
      if (seg.start > pos) break;
      if (seg.samples === 0) continue;  // never voiced
      found = seg;
    }
    return found;
  }

  /** { seg, idx } of the line being spoken at pos, or null before the first line. */
  lineAt(pos) {
    const seg = this.segmentAt(pos);
    if (!seg) return null;
    const len = this.segLength(seg);
    const off = pos - seg.start;
    if (off >= len) return { seg, idx: seg.lines.length - 1, done: true, frac: 1 };
    const target = (off / len) * seg.total;
    let acc = 0;
    for (let i = 0; i < seg.weights.length; i++) {
      if (target < acc + seg.weights[i]) return { seg, idx: i, frac: (target - acc) / seg.weights[i] };
      acc += seg.weights[i];
    }
    return { seg, idx: seg.lines.length - 1, frac: 1 };
  }

  /** Lines heard up to pos, oldest first (unvoiced segments count as heard once passed). */
  heardLines(pos, limit = 3) {
    const at = this.lineAt(pos);
    const out = [];
    for (const seg of this.segments) {
      if (at && seg.seq > at.seg.seq) break;
      if (!at && seg.start > pos) break;
      const upto = at && seg.seq === at.seg.seq ? at.idx : seg.lines.length - 1;
      for (let i = 0; i <= upto; i++) out.push({ seg, idx: i, line: seg.lines[i] });
    }
    return limit ? out.slice(-limit) : out;
  }

  hostIndex(name) {
    const hosts = this.plan?.hosts || this.show.hosts || [];
    const i = hosts.findIndex((h) => h.name === name);
    return i < 0 ? 0 : i;
  }

  hostNames() {
    return joinNames((this.plan?.hosts || this.show.hosts || []).map((h) => h.name).filter(Boolean));
  }

  beats() {
    return this.plan?.beats || this.show.beats || [];
  }

  chapterAt(pos) {
    let current = 0;
    for (const [i, start] of [...this.chapterStarts].sort((a, b) => a[1] - b[1])) {
      if (start <= pos) current = i;
    }
    return current;
  }

  /** [{ title, flex, fill (0..1), state: done|now|later, start }] for the chapter bar. */
  chapterBar(pos) {
    const beats = this.beats();
    const now = this.chapterAt(pos);
    return beats.map((b, i) => {
      const start = this.chapterStarts.get(i);
      const next = this.chapterStarts.get(i + 1);
      const planned = Math.max(0.2, b.minutes || 1) * 60 * this.rate;
      const actual = start !== undefined && next !== undefined ? next - start : null;
      const flex = actual ?? planned;
      let fill = 0;
      if (i < now) fill = 1;
      else if (i === now && start !== undefined) fill = Math.min(0.98, (pos - start) / (actual ?? planned));
      return { title: b.title, flex, fill: Math.max(0, fill), state: i < now ? "done" : i === now ? "now" : "later", start };
    });
  }

  accentAt(pos) {
    const at = this.lineAt(pos);
    const rev = at ? at.seg.style_rev || 0 : 0;
    return (this.styles.get(rev) || this.styles.get(0) || {}).accent || this.show.accent;
  }

  styleLabelAt(pos) {
    const at = this.lineAt(pos);
    const rev = at ? at.seg.style_rev || 0 : 0;
    return (this.styles.get(rev) || this.styles.get(0) || {}).label || this.show.style_label || "";
  }

  /** The call-in a line is reading out, if any. */
  callinFor(seg, idx) {
    if (!seg?.addressed?.length) return null;
    const text = (seg.lines[idx]?.text || "").toLowerCase();
    for (const id of seg.addressed) {
      const c = this.comments.get(id);
      if (!c) continue;
      const first = (c.name || "").split(/\s+/)[0].toLowerCase();
      if (first && text.includes(first)) return c;
    }
    return null;
  }

  isMine(c) {
    return Boolean(c?.client_id && this.mine.has(c.client_id));
  }

  secondsAt(sample) {
    return sample / this.rate;
  }

  commentView(c, pos) {
    const seg = c.seq ? this.segBySeq.get(c.seq) : null;
    switch (c.status) {
      case "queued":
        // The server's estimate of where on the timeline it lands, counted from this
        // listener's own playhead (someone 15 s behind live waits 15 s longer).
        if (c.eta == null) return { label: "Waiting for a gap", tone: "wait" };
        return { label: onAirIn((c.eta - pos) / this.rate, { guess: true }), tone: "wait" };
      case "declined": return { label: "Not aired", tone: "done" };
      case "removed": return { label: "Removed", tone: "done" };
      case "too_late": return { label: "Came in after the sign-off", tone: "done" };
      case "on_deck":
        if (!seg) return { label: "Up next", tone: "next" };
        if (pos < seg.start) {
          return { label: `Up next · ${onAirIn((seg.start - pos) / this.rate)}`, tone: "next" };
        }
        if (pos < seg.start + this.segLength(seg)) return { label: "On air now", tone: "live" };
        return { label: `Answered at ${fmt(this.secondsAt(seg.start))}`, tone: "done" };
      default: return { label: "", tone: "" };
    }
  }

  contextView(x, pos) {
    if (x.status === "background") return { label: "Background, always on", pill: "Background", tone: "bg" };
    if (x.status === "dropped") return { label: "Not used", pill: "Not used", tone: "done" };
    if (x.status === "used") {
      const seg = this.segBySeq.get(x.used_seq);
      if (seg && pos >= seg.start) return { label: `Used at ${fmt(this.secondsAt(seg.start))}`, pill: "Used", tone: "done" };
      return { label: "Coming up", pill: "Up next", tone: "wait" };
    }
    if (x.when === "next") return { label: "Coming up next chapter", pill: "Next chapter", tone: "wait" };
    return { label: "Coming up", pill: "Right now", tone: "wait" };
  }

  /** The show's main sources first, then everything added mid-show. */
  contextItems() {
    const sources = (this.show.sources || []).map((name, i) => ({
      id: `s${i}`, kind: "source", name, main: true,
    }));
    if (!sources.length && this.show.request?.source_names?.length) {
      this.show.request.source_names.forEach((name, i) => sources.push({ id: `s${i}`, kind: "source", name, main: true }));
    }
    const added = [...this.context.values()].sort((a, b) => a.id - b.id);
    return [...sources, ...added];
  }

  transcriptMarkdown() {
    const p = this.plan || this.show;
    const lines = [`# ${p.title || "Episode"}`, "", p.tagline || "", ""];
    for (const h of p.hosts || []) lines.push(`- **${h.name}**: ${h.persona || ""}`);
    lines.push("");
    let detour = null;
    for (const s of this.segments) {
      if (s.interrupt && s.interrupt !== detour && JSON.stringify(s.interrupt) !== JSON.stringify(detour)) {
        lines.push(`> ↳ ${detourLabel(s.interrupt)}`, "");
      }
      detour = s.interrupt || null;
      for (const l of s.lines) lines.push(`**${l.speaker}:** ${l.text}`, "");
    }
    return lines.join("\n");
  }
}

/** How a detour is labelled in the transcript and on screen. */
export function detourLabel(it) {
  if (!it) return "";
  return { ask: `You asked: "${it.text}"`, deeper: "You asked to go deeper", steer: `You steered: "${it.text}"`,
           move_on: "You skipped ahead" }[it.kind] || "Interruption";
}

/** "On air in about 25 s" while it's a guess; exact once its segment is written. */
function onAirIn(sec, { guess = false } = {}) {
  if (sec <= 1) return "On air any moment";
  return guess ? `On air in about ${Math.ceil(sec / 5) * 5} s` : `on air in ${Math.ceil(sec)} s`;
}

function fmt(sec) {
  sec = Math.max(0, Math.floor(sec));
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
}
