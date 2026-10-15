// Plays a PcmTimeline through WebAudio with a pull scheduler.
//
// Instead of scheduling every chunk the moment it arrives (which makes seeking
// impossible), it keeps an anchor { ctxT, pos }: "timeline sample pos plays at
// AudioContext time ctxT". Every pump schedules half-second slices until about
// two seconds ahead of the playhead. Seeking just stops what's scheduled and
// moves the anchor, so Back 15s and Jump to live are both one seek.
// Pause suspends the AudioContext, which freezes currentTime, so the anchor
// math stays valid through a pause.

const SLICE_SECONDS = 0.5;
const LOOKAHEAD = 2;
const LOOKAHEAD_HIDDEN = 30;   // background tabs get throttled timers

export class LivePlayer {
  constructor(ctx, timeline, rate, { onState } = {}) {
    this.ctx = ctx;
    this.tl = timeline;
    this.rate = rate;
    this.onState = onState || (() => {});
    this.gain = ctx.createGain();
    this.gain.connect(ctx.destination);
    this.anchor = { ctxT: 0, pos: 0 };
    this.scheduledTo = 0;
    this.started = false;
    this.paused = false;
    this.buffering = false;
    this.sources = new Set();
    this.volume = 1;
    this._timer = setInterval(() => this.pump(), 200);
    this._onVisible = () => this.pump();
    document.addEventListener("visibilitychange", this._onVisible);
  }

  /** Current playhead in samples. */
  position() {
    if (!this.started) return this.anchor.pos;
    const p = this.anchor.pos + (this.ctx.currentTime - this.anchor.ctxT) * this.rate;
    return Math.max(this.anchor.pos, Math.min(p, this.scheduledTo));
  }

  /** True while the listener wants audio (even if it's still buffering). */
  get wantsAudio() {
    return this.started && !this.paused;
  }

  start(pos) {
    this.started = true;
    this.paused = false;
    if (this.ctx.state !== "running") this.ctx.resume().catch(() => {});
    this._seekTo(pos);
    this._emit();
  }

  seek(pos) {
    if (!this.started) {
      this.anchor = { ctxT: 0, pos };
      return;
    }
    this._seekTo(pos);
  }

  back(seconds) {
    this.seek(Math.max(this.tl.start, this.position() - seconds * this.rate));
  }

  pause() {
    if (!this.started || this.paused) return;
    this.paused = true;
    this.ctx.suspend().catch(() => {});
    this._emit();
  }

  resume() {
    if (!this.started) return;
    this.paused = false;
    this.ctx.resume().catch(() => {});
    this.pump();
    this._emit();
  }

  toggle() {
    if (this.paused) this.resume();
    else this.pause();
  }

  setVolume(v) {
    this.volume = v;
    this.gain.gain.value = v;
  }

  _seekTo(pos) {
    pos = Math.max(this.tl.empty ? pos : this.tl.start, Math.round(pos));
    this._stopSources();
    this.anchor = { ctxT: this.ctx.currentTime + 0.05, pos };
    this.scheduledTo = pos;
    this.pump();
  }

  pump() {
    if (!this.started || this.paused) return;
    const now = this.ctx.currentTime;
    const ideal = this.anchor.pos + (now - this.anchor.ctxT) * this.rate;
    if (ideal > this.scheduledTo + 0.02 * this.rate) {
      // Ran dry: everything scheduled has played. Resume from there once audio arrives.
      if (!this.tl.has(this.scheduledTo)) return this._setBuffering(true);
      this.anchor = { ctxT: now + 0.06, pos: this.scheduledTo };
    }
    const horizon = this.position() + (document.hidden ? LOOKAHEAD_HIDDEN : LOOKAHEAD) * this.rate;
    while (this.scheduledTo < horizon) {
      const f = this.tl.slice(this.scheduledTo, Math.round(SLICE_SECONDS * this.rate));
      if (!f || !f.length) break;
      const buf = this.ctx.createBuffer(1, f.length, this.rate);
      buf.copyToChannel(f, 0);
      const src = this.ctx.createBufferSource();
      src.buffer = buf;
      src.connect(this.gain);
      const when = this.anchor.ctxT + (this.scheduledTo - this.anchor.pos) / this.rate;
      src.start(Math.max(when, now));
      src.onended = () => this.sources.delete(src);
      this.sources.add(src);
      this.scheduledTo += f.length;
    }
    this._setBuffering(this.scheduledTo <= this.position() + 0.01 * this.rate && !this.tl.has(this.scheduledTo));
  }

  _setBuffering(b) {
    if (b !== this.buffering) {
      this.buffering = b;
      this._emit();
    }
  }

  _stopSources() {
    for (const s of this.sources) {
      try { s.stop(); } catch { /* already stopped */ }
    }
    this.sources.clear();
  }

  _emit() {
    this.onState({ started: this.started, paused: this.paused, buffering: this.buffering });
  }

  destroy() {
    clearInterval(this._timer);
    document.removeEventListener("visibilitychange", this._onVisible);
    this._stopSources();
    try { this.gain.disconnect(); } catch { /* ignore */ }
    if (this.paused) this.ctx.resume().catch(() => {});
  }
}
