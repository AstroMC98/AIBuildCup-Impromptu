// Runs on the audio thread. The mic at the device's rate in, 16 kHz 16-bit mono
// out, in 40 ms frames (what the co-host listens to). A low-pass filter (windowed
// sinc, cut off below 8 kHz) runs before the samples are picked, so nothing above
// what 16 kHz can carry folds back down into the speech: averaging alone let "s",
// "t" and "f" alias into a hiss that the co-host and its transcriber misheard.
// Muted, it keeps sending frames, of silence: the server uses them as the call's clock.
const OUT_RATE = 16000;
const FRAME = 640;   // 40 ms
const TAPS = 63;     // about 1.3 ms at 48 kHz; the delay is half that
const CUTOFF = 7200; // Hz: under the 8 kHz the output can carry, over most of speech

/** Low-pass taps for a sample rate: a sinc at `cutoff`, Blackman-windowed, summing to 1. */
function lowPass(rate, cutoff, taps) {
  const fc = cutoff / rate;
  const mid = (taps - 1) / 2;
  const h = new Float32Array(taps);
  let sum = 0;
  for (let k = 0; k < taps; k++) {
    const n = k - mid;
    const sinc = n === 0 ? 2 * fc : Math.sin(2 * Math.PI * fc * n) / (Math.PI * n);
    const w = 0.42 - 0.5 * Math.cos((2 * Math.PI * k) / (taps - 1)) + 0.08 * Math.cos((4 * Math.PI * k) / (taps - 1));
    h[k] = sinc * w;
    sum += h[k];
  }
  for (let k = 0; k < taps; k++) h[k] /= sum;
  return h;
}

class MicProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.step = sampleRate / OUT_RATE;   // input samples per output sample (3 at 48 kHz)
    // A device already at 16 kHz needs no filter; anything else gets one.
    this.h = this.step > 1.001 ? lowPass(sampleRate, CUTOFF, TAPS) : null;
    this.hist = new Float32Array(TAPS);  // the last TAPS input samples, as a ring
    this.at = 0;
    this.prev = 0;                       // the filtered sample before this one
    this.next = 0;                       // where the next output falls, in input samples from now
    this.frame = new Int16Array(FRAME);
    this.n = 0;
    this.energy = 0;
    this.muted = false;
    this.port.onmessage = (e) => {
      if ("muted" in e.data) this.muted = Boolean(e.data.muted);
    };
  }

  filtered(x) {
    if (!this.h) return x;
    this.hist[this.at] = x;
    let y = 0;
    for (let k = 0, i = this.at; k < TAPS; k++) {
      y += this.h[k] * this.hist[i];
      i = i === 0 ? TAPS - 1 : i - 1;
    }
    this.at = this.at === TAPS - 1 ? 0 : this.at + 1;
    return y;
  }

  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input) return true;
    for (let i = 0; i < input.length; i++) {
      const y = this.filtered(input[i]);
      // Output samples land between input samples: read them off the line from the
      // previous filtered sample to this one.
      while (this.next <= 0) {
        this.emit(this.prev + (y - this.prev) * (this.next + 1));
        this.next += this.step;
      }
      this.next -= 1;
      this.prev = y;
    }
    return true;
  }

  emit(v) {
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
}

registerProcessor("mic-processor", MicProcessor);
