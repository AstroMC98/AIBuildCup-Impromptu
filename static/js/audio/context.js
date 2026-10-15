// One AudioContext for the whole session. Browsers only let audio start from a
// user gesture, so "Go live" and "Tune in" call unlockAudio() inside the click.
let ctx = null;

export function audioContext() {
  if (!ctx) ctx = new (window.AudioContext || window.webkitAudioContext)({ latencyHint: "playback" });
  return ctx;
}

export async function unlockAudio() {
  const c = audioContext();
  // A silent blip inside the gesture convinces iOS Safari, too.
  try {
    const blip = c.createBuffer(1, 1, c.sampleRate);
    const src = c.createBufferSource();
    src.buffer = blip;
    src.connect(c.destination);
    src.start();
  } catch { /* ignore */ }
  if (c.state !== "running") {
    try { await c.resume(); } catch { /* ignore */ }
  }
  return c.state === "running";
}

export function audioUnlocked() {
  return Boolean(ctx && ctx.state === "running");
}
