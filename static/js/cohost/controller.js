// One co-host call on this phone: the socket, the mic, the co-host's voice, and
// what the call screen shows (state, captions, time, mode). Views subscribe with on().
//
// Cutting in: Gemini stops an answer when it hears you over it, but its audio
// arrives faster than it plays, so an answer can be fully sent and still playing.
// So the phone listens too: talk over the co-host and it ducks at once, and if you
// keep talking it stops and tells the server (cut_in). A short "mm-hm" just ducks.
import { CallPlayer, callContext } from "./audio.js";
import { CallSocket } from "./socket.js";

const TALK_LEVEL = 0.03;     // mic RMS that counts as you talking (after echo cancelling)
const DUCK_MS = 120;         // talking this long over the co-host ducks it
const CUT_MS = 400;          // and this long stops it
const RELEASE_MS = 600;      // un-duck after this long without talking

export class CallController {
  constructor({ showId, mic = null, onTerminal }) {
    this.id = showId;
    this.ctx = callContext();
    this.player = new CallPlayer(this.ctx);
    this.mic = null;
    this.state = "connecting";      // connecting | live | reconnecting | editing | saved | ended
    this.connection = "connecting"; // open | down
    this.host = null;
    this.you = "";
    this.mode = "banter";
    this.accent = null;
    this.title = "";
    this.limitS = 0;
    this.wrapping = false;
    this.said = [];                 // [{ who: "you" | "host", turn, text }]
    this.steps = [];                // editing steps done so far
    this.saved = null;
    this.ended = null;
    this.error = "";
    this.hungUp = false;
    this._elapsed = { s: 0, at: performance.now(), running: false };
    this._talkMs = 0;
    this._quietMs = 0;
    this._ducked = false;
    this._subs = new Set();
    this._wake = null;
    this._onVisible = () => { if (document.visibilityState === "visible") this._keepAwake(); };
    document.addEventListener("visibilitychange", this._onVisible);
    this.onTerminal = onTerminal || (() => {});
    this.socket = new CallSocket({
      showId,
      onEvent: (e) => this._event(e),
      onAudio: (turn, pcm) => { if (!this.hungUp) this.player.push(turn, pcm); },
      onOpen: () => { this.connection = "open"; this._changed(); },
      onDown: () => { this.connection = "down"; this.player.flush(0); this._changed(); },
      onTerminal: (code) => { this.connection = "down"; this._changed(); this.onTerminal(code); },
      keepAlive: () => !["saved", "ended"].includes(this.state),
    });
    if (mic) this.attachMic(mic);
  }

  on(fn) {
    this._subs.add(fn);
    return () => this._subs.delete(fn);
  }

  _changed(e = { type: "change" }) {
    for (const fn of this._subs) fn(e);
  }

  get needsMic() { return !this.mic && ["connecting", "live", "reconnecting"].includes(this.state); }
  get onAir() { return ["live", "reconnecting"].includes(this.state) && !this.hungUp; }

  elapsed() {
    const e = this._elapsed;
    const s = e.running ? e.s + (performance.now() - e.at) / 1000 : e.s;
    return this.limitS ? Math.min(s, this.limitS) : s;
  }

  left() { return Math.max(0, this.limitS - this.elapsed()); }

  attachMic(mic) {
    this.mic = mic;
    mic.onFrame = (pcm, level) => this._micFrame(pcm, level);
    this._keepAwake();
    this._changed();
  }

  // ---- the mic, 25 times a second ----------------------------------------------------

  _micFrame(pcm, level) {
    if (this.hungUp) return;
    this.socket.sendAudio(pcm);
    const talking = !this.mic.muted && level > TALK_LEVEL;
    if (talking) {
      this._talkMs += 40;
      this._quietMs = 0;
    } else {
      this._quietMs += 40;
      if (this._quietMs >= 120) this._talkMs = 0;
    }
    if (!this.player.playing) {
      if (this._ducked) this._duck(false);
      return;
    }
    if (this._talkMs >= CUT_MS) {
      this.player.flush();
      this._ducked = false;
      this.socket.send({ type: "cut_in" });
    } else if (this._talkMs >= DUCK_MS && !this._ducked) {
      this._duck(true);
    } else if (this._ducked && this._quietMs >= RELEASE_MS) {
      this._duck(false);
    }
  }

  _duck(on) {
    this._ducked = on;
    this.player.duck(on);
  }

  // ---- the server ------------------------------------------------------------------------

  _event(e) {
    switch (e.type) {
      case "ready":
        this.host = e.host;
        this.you = e.you;
        this.mode = e.mode;
        this.accent = e.accent;
        this.title = e.title;
        this.limitS = e.limit_s;
        this.wrapping = e.wrapping;
        this.said = e.said || [];
        this.saved = e.saved;
        this._setState(e.saved ? "saved" : e.state);
        this._elapsed = { s: e.elapsed_s, at: performance.now(), running: e.state === "live" };
        break;
      case "state":
        this._setState(e.state);
        if (e.state === "live" && !this._elapsed.running) {
          this._elapsed = { s: this._elapsed.s, at: performance.now(), running: true };
        }
        break;
      case "lookup":
        this.lookingUp = performance.now();   // the co-host is quiet while it checks your notes
        break;
      case "said": {
        if (e.who === "host") this.lookingUp = 0;
        const last = this.said[this.said.length - 1];
        if (last && last.who === e.who && last.turn === e.turn) last.text += e.text;
        else this.said.push({ who: e.who, turn: e.turn, text: e.text });
        if (this.said.length > 40) this.said.splice(0, this.said.length - 40);
        break;
      }
      case "interrupted":
        this.lookingUp = 0;
        this.player.flush(e.turn);
        this._ducked = false;
        break;
      case "mode":
        this.mode = e.mode;
        this.accent = e.accent;
        break;
      case "wrap":
        this.wrapping = true;
        break;
      case "editing":
        this._setState("editing");
        if (!this.steps.includes(e.step)) this.steps.push(e.step);
        break;
      case "saved":
        this.saved = e;
        this._setState("saved");
        break;
      case "ended":
        this.ended = e;
        this._setState("ended");
        break;
      case "error":
        this.error = e.message;
        break;
      default:
        break;
    }
    this._changed(e);
  }

  _setState(state) {
    if (state === this.state) return;
    this.state = state;
    if (!["connecting", "live", "reconnecting"].includes(state)) {
      if (this._elapsed.running) this._elapsed = { s: this.elapsed(), at: performance.now(), running: false };
      this._stopMic();
    }
  }

  // ---- what you can do -----------------------------------------------------------------

  setMode(mode) {
    if (mode !== this.mode && this.onAir) this.socket.send({ type: "mode", mode });
  }

  toggleMute() {
    if (!this.mic) return;
    this.mic.muted = !this.mic.muted;
    this._changed();
  }

  hangUp() {
    if (this.hungUp) return;
    this.hungUp = true;
    this.socket.send({ type: "hangup" });
    this.player.flush();
    this._stopMic();
    if (this._elapsed.running) this._elapsed = { s: this.elapsed(), at: performance.now(), running: false };
    this._changed();
  }

  _stopMic() {
    this.mic?.close();
    this.mic = null;
    this._wake?.release?.().catch(() => {});
    this._wake = null;
  }

  async _keepAwake() {
    if (!this.mic || this._wake || !navigator.wakeLock) return;
    try {
      const lock = await navigator.wakeLock.request("screen");
      lock.addEventListener("release", () => { if (this._wake === lock) this._wake = null; });
      this._wake = lock;
    } catch { /* not allowed right now (e.g. the tab is hidden) */ }
  }

  destroy() {
    document.removeEventListener("visibilitychange", this._onVisible);
    this.socket.close();
    this._stopMic();
    this.player.close();
    this._subs.clear();
  }
}
