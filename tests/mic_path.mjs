// Runs audio through the app's own mic worklet (static/js/cohost/mic-worklet.js), the
// way a browser does, for tests/live_test.py --mic-path: 32-bit float mono at the
// device's rate in, the 16 kHz 16-bit frames the worklet sends out.
//
//   node tests/mic_path.mjs WORKLET.js RATE in.f32 out.s16
import { readFileSync, writeFileSync } from "node:fs";
import vm from "node:vm";

const [worklet, rateArg, inPath, outPath] = process.argv.slice(2);
const rate = Number(rateArg);
let Processor = null;
const frames = [];
// The worklet's scope on the audio thread: its rate, the base class, and registration.
const scope = vm.createContext({
  sampleRate: rate,
  AudioWorkletProcessor: class {
    constructor() { this.port = { postMessage: (m) => frames.push(new Int16Array(m.pcm)), onmessage: null }; }
  },
  registerProcessor: (_name, cls) => { Processor = cls; },
});
vm.runInContext(readFileSync(worklet, "utf8"), scope, { filename: worklet });
const proc = new Processor();

const buf = readFileSync(inPath);
const input = new Float32Array(buf.buffer, buf.byteOffset, buf.byteLength / 4);
const tail = new Float32Array(Math.ceil(rate * 0.05));   // flush the last frame
for (const part of [input, tail]) {
  for (let i = 0; i < part.length; i += 128) proc.process([[part.subarray(i, i + 128)]]);
}
const out = new Int16Array(frames.reduce((n, f) => n + f.length, 0));
let at = 0;
for (const f of frames) { out.set(f, at); at += f.length; }
writeFileSync(outPath, Buffer.from(out.buffer));
