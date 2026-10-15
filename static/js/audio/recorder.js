// The microphone through MediaRecorder, in whatever container this browser records
// (WebM/Opus in Chrome, Firefox and on Android, MP4/AAC in Safari). Gemini accepts
// all of them and the server tells them apart by their bytes, so nothing is
// re-encoded here.
//
// Recorder: hold-to-talk for the producer's questions.
// MemoRecorder: a voice memo. Its length comes from our own clock: Chrome's WebM
// recordings say they're Infinity seconds long. The screen is kept awake while
// recording, where the browser lets us, because a phone that locks mid-memo can
// stop the microphone.

const TYPES = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4;codecs=mp4a.40.2", "audio/mp4"];
const MAX_MS = 60000;

export class Recorder {
  static get supported() {
    return Boolean(navigator.mediaDevices?.getUserMedia && window.MediaRecorder);
  }

  /** Asks for the microphone the first time. */
  async start() {
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
    });
    const type = TYPES.find((t) => MediaRecorder.isTypeSupported(t)) || "";
    this.rec = new MediaRecorder(this.stream, type ? { mimeType: type } : {});
    this.parts = [];
    this.rec.ondataavailable = (e) => { if (e.data.size) this.parts.push(e.data); };
    this.rec.start();
    this.timer = setTimeout(() => this.stop(), MAX_MS);
  }

  /** Resolves with what was recorded (an empty Blob if nothing was). */
  stop() {
    clearTimeout(this.timer);
    if (!this.rec || this.rec.state === "inactive") {
      this._release();
      return Promise.resolve(this._blob());
    }
    return new Promise((resolve) => {
      this.rec.onstop = () => {
        this._release();
        resolve(this._blob());
      };
      this.rec.stop();
    });
  }

  _blob() {
    return new Blob(this.parts || [], { type: (this.rec?.mimeType || "audio/webm").split(";")[0] });
  }

  _release() {
    for (const t of this.stream?.getTracks() || []) t.stop();
    this.stream = null;
  }
}

// ---- voice memos ------------------------------------------------------------------

export function recordingSupported() {
  return Boolean(window.isSecureContext && navigator.mediaDevices?.getUserMedia && window.MediaRecorder);
}

/** The extension a recording of this type gets when it's uploaded. */
export function extensionFor(type) {
  if (/ogg/.test(type)) return "ogg";
  if (/mp4|m4a|aac/.test(type)) return "m4a";
  if (/mpeg|mp3/.test(type)) return "mp3";
  if (/wav/.test(type)) return "wav";
  return "webm";
}

export class MemoRecorder {
  /** onTick(seconds), onLevel(0..1), onStop({ blob, seconds }); stops itself at maxSeconds. */
  constructor({ maxSeconds = 600, onTick, onLevel, onStop } = {}) {
    this.maxSeconds = maxSeconds;
    this.onTick = onTick || (() => {});
    this.onLevel = onLevel || (() => {});
    this.onStop = onStop || (() => {});
    this.state = "idle";          // idle | recording | paused | stopping | done
    this.elapsed = 0;
    this._since = 0;
    this._discard = false;
  }

  get seconds() {
    return this.elapsed + (this.state === "recording" ? (performance.now() - this._since) / 1000 : 0);
  }

  async start() {
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    const mimeType = TYPES.find((t) => window.MediaRecorder.isTypeSupported?.(t)) || "";
    this.recorder = new MediaRecorder(this.stream, { ...(mimeType ? { mimeType } : {}), audioBitsPerSecond: 32000 });
    this.chunks = [];
    this.recorder.ondataavailable = (e) => { if (e.data.size) this.chunks.push(e.data); };
    this.recorder.onstop = () => this._finished(mimeType);
    this.recorder.start(1000);    // a chunk a second, so nothing is lost if the page goes away
    this.state = "recording";
    this._since = performance.now();
    this._timer = setInterval(() => this._tick(), 250);
    this._meter();
    this._keepAwake();
  }

  pause() {
    if (this.state !== "recording") return;
    this.recorder.pause();
    this.elapsed = this.seconds;
    this.state = "paused";
    this.onTick(this.seconds);
  }

  resume() {
    if (this.state !== "paused") return;
    this.recorder.resume();
    this._since = performance.now();
    this.state = "recording";
  }

  stop() {
    if (this.state !== "recording" && this.state !== "paused") return;
    this.elapsed = this.seconds;
    this.state = "stopping";
    this.recorder.stop();
  }

  /** Stop and throw the recording away (leaving the page, starting over). */
  cancel() {
    this._discard = true;
    if (this.state === "recording" || this.state === "paused") this.stop();
    else this._cleanup();
  }

  _tick() {
    this.onTick(this.seconds);
    if (this.seconds >= this.maxSeconds) this.stop();
  }

  _finished(mimeType) {
    this._cleanup();
    this.state = "done";
    if (this._discard) return;
    const type = this.recorder.mimeType || mimeType || this.chunks[0]?.type || "audio/webm";
    this.onStop({ blob: new Blob(this.chunks, { type }), seconds: this.elapsed });
  }

  _meter() {
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      this._ctx = new Ctx();
      const analyser = this._ctx.createAnalyser();
      analyser.fftSize = 1024;
      this._ctx.createMediaStreamSource(this.stream).connect(analyser);
      const data = new Uint8Array(analyser.fftSize);
      const loop = () => {
        if (this.state !== "recording" && this.state !== "paused") return;
        analyser.getByteTimeDomainData(data);
        let sum = 0;
        for (const v of data) sum += ((v - 128) / 128) ** 2;
        this.onLevel(this.state === "paused" ? 0 : Math.min(1, Math.sqrt(sum / data.length) * 4));
        this._raf = requestAnimationFrame(loop);
      };
      loop();
    } catch { /* no meter: recording works without it */ }
  }

  async _keepAwake() {
    try {
      this._wake = await navigator.wakeLock?.request("screen");
    } catch { /* not allowed here: fine */ }
  }

  _cleanup() {
    clearInterval(this._timer);
    cancelAnimationFrame(this._raf);
    for (const t of this.stream?.getTracks() || []) t.stop();
    this._ctx?.close().catch(() => {});
    this._wake?.release?.().catch(() => {});
    this._wake = null;
  }
}
