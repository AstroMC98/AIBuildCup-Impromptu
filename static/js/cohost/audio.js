// Audio for a co-host call: the mic going out and the co-host's voice coming in,
// on one AudioContext tuned for talking ("interactive"), not for broadcast like
// the pod player (which buffers seconds ahead).
//
// Browsers only open the mic and start audio from a user gesture, so "Start the
// call" calls startCallAudio() inside the click; the open mic is handed to the call
// screen through pushState navigation (see router.js), which keeps it alive.

let ctx = null;
let handoff = null;   // { mic } from the click that started the call

export function callContext() {
  if (!ctx) ctx = new (window.AudioContext || window.webkitAudioContext)({ latencyHint: "interactive" });
  return ctx;
}

export function micSupported() {
  return Boolean(navigator.mediaDevices?.getUserMedia && window.AudioWorkletNode && window.isSecureContext);
}

/** Inside a click: unlock audio and open the mic. Throws a friendly Error if it can't. */
export async function startCallAudio() {
  if (!micSupported()) {
    throw new Error(window.isSecureContext
      ? "This browser can't stream the mic. Try a recent Chrome, Safari or Firefox."
      : "The mic needs a secure connection (https).");
  }
  const c = callContext();
  try {
    const blip = c.createBuffer(1, 1, c.sampleRate);   // convinces iOS Safari, too
    const src = c.createBufferSource();
    src.buffer = blip;
    src.connect(c.destination);
    src.start();
  } catch { /* ignore */ }
  if (c.state !== "running") await c.resume().catch(() => {});
  const mic = await Mic.open(c);
  handoff = { mic };
  return mic;
}

/** The mic opened by the click that started this call, if there was one. */
export function takeHandoff() {
  const h = handoff;
  handoff = null;
  return h;
}

export class Mic {
  static async open(c) {
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
      });
    } catch (err) {
      throw new Error(err?.name === "NotAllowedError"
        ? "Allow the microphone to co-host. Check your browser's site settings."
        : "Couldn't open the microphone.");
    }
    await c.audioWorklet.addModule("/static/js/cohost/mic-worklet.js");
    return new Mic(c, stream);
  }

  constructor(c, stream) {
    this.stream = stream;
    this.source = c.createMediaStreamSource(stream);
    this.node = new AudioWorkletNode(c, "mic-processor", { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1] });
    this.source.connect(this.node);
    this.node.connect(c.destination);   // silent, but keeps the worklet running
    this.level = 0;
    this.onFrame = null;                // (ArrayBuffer of Int16) => void
    this._muted = false;
    this.node.port.onmessage = (e) => {
      this.level = e.data.level;
      this.onFrame?.(e.data.pcm, e.data.level);
    };
  }

  get muted() { return this._muted; }
  set muted(v) {
    this._muted = Boolean(v);
    this.node.port.postMessage({ muted: this._muted });
  }

  close() {
    this.onFrame = null;
    for (const t of this.stream.getTracks()) t.stop();
    try { this.source.disconnect(); this.node.disconnect(); } catch { /* ignore */ }
  }
}

const JITTER_S = 0.06;    // cushion when a turn starts, against network jitter

/** The co-host's voice, played the moment it arrives. Chunks of a turn follow each
 * other exactly; a new turn starts after a short cushion. flush() silences it at
 * once (you cut in), and late chunks of a flushed turn are dropped. */
export class CallPlayer {
  constructor(c, rate = 24000) {
    this.ctx = c;
    this.rate = rate;
    this.out = c.createGain();
    this.analyser = c.createAnalyser();
    this.analyser.fftSize = 512;
    this.out.connect(this.analyser);
    this.analyser.connect(c.destination);
    this._buf = new Float32Array(this.analyser.fftSize);
    this.next = 0;
    this.turn = 0;
    this.sources = new Set();
    this.flushed = new Set();
  }

  push(turn, pcm) {
    if (this.flushed.has(turn) || !pcm.length) return;
    const buffer = this.ctx.createBuffer(1, pcm.length, this.rate);
    const data = buffer.getChannelData(0);
    for (let i = 0; i < pcm.length; i++) data[i] = pcm[i] / 32768;
    const now = this.ctx.currentTime;
    if (this.next < now + 0.01) this.next = now + JITTER_S;
    const src = this.ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(this.out);
    src.start(this.next);
    src.onended = () => this.sources.delete(src);
    this.sources.add(src);
    this.next += pcm.length / this.rate;
    this.turn = turn;
  }

  /** Seconds of the co-host still to play. */
  get ahead() { return Math.max(0, this.next - this.ctx.currentTime); }
  get playing() { return this.ahead > 0.02; }

  flush(turn = this.turn) {
    if (turn) this.flushed.add(turn);
    for (const s of this.sources) {
      try { s.stop(); } catch { /* already stopped */ }
    }
    this.sources.clear();
    this.next = 0;
    this.duck(false);
  }

  duck(on) {
    this.out.gain.setTargetAtTime(on ? 0.3 : 1, this.ctx.currentTime, 0.03);
  }

  level() {
    this.analyser.getFloatTimeDomainData(this._buf);
    let sum = 0;
    for (const v of this._buf) sum += v * v;
    return Math.sqrt(sum / this._buf.length);
  }

  close() {
    this.flush(0);
    try { this.analyser.disconnect(); this.out.disconnect(); } catch { /* ignore */ }
  }
}
