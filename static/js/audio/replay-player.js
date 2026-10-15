// Plays a finished episode's MP3 with the same interface as LivePlayer, so the
// views don't care whether a show is live or a replay. Positions are samples.

export class ReplayPlayer {
  constructor(url, rate, { onState } = {}) {
    this.rate = rate;
    this.onState = onState || (() => {});
    this.audio = new Audio();
    this.audio.preload = "metadata";
    this.audio.src = url;
    this.started = false;
    this.buffering = false;
    this.ended = false;
    const emit = () => this._emit();
    this.audio.addEventListener("play", () => { this.started = true; this.ended = false; emit(); });
    this.audio.addEventListener("pause", emit);
    this.audio.addEventListener("waiting", () => { this.buffering = true; emit(); });
    this.audio.addEventListener("playing", () => { this.buffering = false; emit(); });
    this.audio.addEventListener("ended", () => { this.ended = true; emit(); });
    this.audio.addEventListener("error", () => { this.error = true; emit(); });
  }

  get paused() { return this.audio.paused; }
  get wantsAudio() { return this.started && !this.audio.paused; }

  position() {
    return this.audio.currentTime * this.rate;
  }

  start(pos = 0) {
    this.started = true;
    if (pos) this.audio.currentTime = pos / this.rate;
    return this.audio.play().catch(() => {});
  }

  resume() { this.started = true; return this.audio.play().catch(() => {}); }
  pause() { this.audio.pause(); }
  toggle() { if (this.audio.paused) this.resume(); else this.pause(); }

  seek(pos) {
    this.audio.currentTime = Math.max(0, pos / this.rate);
  }

  back(seconds) {
    this.audio.currentTime = Math.max(0, this.audio.currentTime - seconds);
  }

  setVolume(v) { this.audio.volume = v; }

  _emit() {
    this.onState({ started: this.started, paused: this.audio.paused, buffering: this.buffering, ended: this.ended });
  }

  destroy() {
    this.audio.pause();
    this.audio.removeAttribute("src");
    this.audio.load();
  }
}
