// The show's audio as this listener holds it: s16 chunks keyed by their start
// sample on the show timeline (the server puts it in every frame header).
// Chunks arrive in order on one connection, so this is append-mostly:
// a chunk we already hold is ignored (reconnect overlap), and a chunk that
// leaves a hole is reported so the caller can start over at the live edge.

export class PcmTimeline {
  constructor(rate) {
    this.rate = rate;
    this.chunks = [];   // { start, data: Int16Array }, sorted, contiguous
  }

  get start() { return this.chunks.length ? this.chunks[0].start : 0; }

  get end() {
    const last = this.chunks[this.chunks.length - 1];
    return last ? last.start + last.data.length : 0;
  }

  get empty() { return this.chunks.length === 0; }

  /** "added" | "dup" | "gap" */
  add(start, data) {
    if (!data.length) return "dup";
    if (this.empty) {
      this.chunks.push({ start, data });
      return "added";
    }
    const end = this.end;
    if (start + data.length <= end) return "dup";
    if (start > end) return "gap";
    if (start < end) {
      data = data.subarray(end - start);
      start = end;
    }
    this.chunks.push({ start, data });
    return "added";
  }

  has(pos) {
    return !this.empty && pos >= this.start && pos < this.end;
  }

  _find(pos) {
    let lo = 0, hi = this.chunks.length - 1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      const c = this.chunks[mid];
      if (pos < c.start) hi = mid - 1;
      else if (pos >= c.start + c.data.length) lo = mid + 1;
      else return mid;
    }
    return -1;
  }

  /** Up to n samples from pos as Float32, or null if pos isn't held. */
  slice(pos, n) {
    let i = this._find(pos);
    if (i < 0) return null;
    const avail = Math.min(n, this.end - pos);
    const out = new Float32Array(avail);
    let o = 0;
    let at = pos;
    while (o < avail && i < this.chunks.length) {
      const c = this.chunks[i];
      const from = at - c.start;
      const take = Math.min(c.data.length - from, avail - o);
      for (let k = 0; k < take; k++) out[o + k] = c.data[from + k] / 32768;
      o += take;
      at += take;
      i++;
    }
    return out;
  }

  /** Forget audio before pos, keeping memory bounded on long shows. */
  trim(pos) {
    while (this.chunks.length > 1 && this.chunks[0].start + this.chunks[0].data.length <= pos) {
      this.chunks.shift();
    }
  }

  /** Forget audio from pos on: the producer interrupted there, and it was never heard. */
  truncate(pos) {
    while (this.chunks.length && this.chunks[this.chunks.length - 1].start >= pos) this.chunks.pop();
    const last = this.chunks[this.chunks.length - 1];
    if (last && last.start + last.data.length > pos) last.data = last.data.subarray(0, pos - last.start);
  }

  clear() {
    this.chunks = [];
  }
}
