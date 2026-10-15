// Runs on the audio thread. The mic at the device's rate in, 16 kHz 16-bit mono
// out, in 40 ms frames (what the co-host listens to). Each output sample is the
// average of the input samples it covers, which is enough filtering for speech.
// Muted, it keeps sending frames, of silence: the server uses them as the call's clock.
const OUT_RATE = 16000;
const FRAME = 640;   // 40 ms

class MicProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.step = sampleRate / OUT_RATE;   // input samples per output sample
    this.t = 0;
    this.acc = 0;
    this.accN = 0;
    this.frame = new Int16Array(FRAME);
    this.n = 0;
    this.energy = 0;
    this.muted = false;
    this.port.onmessage = (e) => {
      if ("muted" in e.data) this.muted = Boolean(e.data.muted);
    };
  }

  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input) return true;
    for (let i = 0; i < input.length; i++) {
      this.acc += input[i];
      this.accN++;
      this.t += 1;
      if (this.t < this.step) continue;
      this.t -= this.step;
      const v = this.acc / this.accN;
      this.acc = 0;
      this.accN = 0;
      this.energy += v * v;
      const s = this.muted ? 0 : Math.max(-1, Math.min(1, v));
      this.frame[this.n++] = s < 0 ? s * 32768 : s * 32767;
      if (this.n === FRAME) {
        const level = Math.sqrt(this.energy / FRAME);   // what the mic hears, muted or not
        this.port.postMessage({ pcm: this.frame.buffer, level }, [this.frame.buffer]);
        this.frame = new Int16Array(FRAME);
        this.n = 0;
        this.energy = 0;
      }
    }
    return true;
  }
}

registerProcessor("mic-processor", MicProcessor);
