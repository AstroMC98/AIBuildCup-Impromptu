// Hold-to-talk for the producer's questions. Uses the browser's own encoder
// (WebM/Opus, Ogg/Opus or MP4/AAC, whichever it has): Gemini accepts all of them,
// so nothing is re-encoded here.

const TYPES = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"];
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
