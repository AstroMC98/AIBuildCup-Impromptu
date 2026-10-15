// Reports replay listening for the creator's stats: how far the listener got and
// how long it actually played (seeking doesn't count). Sent every 15 s while
// playing, on pause and at the end, and as a beacon when the page goes away.
import { api } from "../api.js";
import { listenerId, uid } from "../util.js";

const EVERY_MS = 15000;

export class ListenTracker {
  constructor(showId, player) {
    this.showId = showId;
    this.audio = player.audio;
    this.sid = uid();
    this.played = 0;
    this.last = null;
    this.started = false;
    this.ended = false;
    const a = this.audio;
    this.handlers = {
      timeupdate: () => {
        const t = a.currentTime;
        if (this.last !== null && !a.paused) {
          const step = t - this.last;
          if (step > 0 && step < 2) this.played += step;  // a jump is a seek, not listening
        }
        this.last = t;
      },
      seeking: () => { this.last = null; },
      play: () => { this.started = true; this.send(); },
      pause: () => this.send(),
      ended: () => { this.ended = true; this.send(); },
    };
    for (const [ev, fn] of Object.entries(this.handlers)) a.addEventListener(ev, fn);
    this.timer = setInterval(() => { if (!a.paused) this.send(); }, EVERY_MS);
    this.onHide = () => { if (document.visibilityState === "hidden") this.send(true); };
    this.onPageHide = () => this.send(true);
    document.addEventListener("visibilitychange", this.onHide);
    addEventListener("pagehide", this.onPageHide);
  }

  send(beacon = false) {
    if (!this.started) return;
    api.listen(this.showId, {
      sid: this.sid, lid: listenerId(), pos: this.audio.currentTime || 0,
      played: Math.round(this.played), ended: this.ended,
    }, { beacon });
  }

  stop() {
    this.send(true);
    clearInterval(this.timer);
    for (const [ev, fn] of Object.entries(this.handlers)) this.audio.removeEventListener(ev, fn);
    document.removeEventListener("visibilitychange", this.onHide);
    removeEventListener("pagehide", this.onPageHide);
  }
}
