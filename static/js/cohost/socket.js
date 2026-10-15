// The phone's line to a co-host call (protocol: app/cohost.py). Mic frames go up
// as binary; the co-host's voice comes down as <turn:u32> + 24 kHz PCM. Reconnects
// with backoff like the pod socket; the server keeps the call going meanwhile.

// 4410: the call was picked up somewhere else (another tab, or this one reconnecting)
const TERMINAL = new Set([4400, 4401, 4403, 4404, 4409, 4410]);
const BACKOFF = [300, 600, 1000, 2000, 4000];

export class CallSocket {
  constructor({ showId, onEvent, onAudio, onOpen, onDown, onTerminal, keepAlive }) {
    this.showId = showId;
    this.onEvent = onEvent;
    this.onAudio = onAudio;
    this.onOpen = onOpen || (() => {});
    this.onDown = onDown || (() => {});
    this.onTerminal = onTerminal || (() => {});
    this.keepAlive = keepAlive || (() => true);
    this.ws = null;
    this.closed = false;
    this.attempt = 0;
    this._connect();
  }

  get open() { return this.ws?.readyState === WebSocket.OPEN; }

  _connect() {
    if (this.closed) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/cohost/${encodeURIComponent(this.showId)}`);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onopen = () => {
      this.attempt = 0;
      ws.send(JSON.stringify({ type: "join" }));
      this.onOpen();
    };
    ws.onmessage = (e) => {
      if (typeof e.data === "string") this.onEvent(JSON.parse(e.data));
      else if (e.data.byteLength > 4) {
        this.onAudio(new DataView(e.data).getUint32(0, true), new Int16Array(e.data, 4));
      }
    };
    ws.onclose = (e) => {
      if (this.ws !== ws) return;
      this.ws = null;
      if (this.closed) return;
      if (!this.keepAlive() || TERMINAL.has(e.code)) {
        this.closed = true;
        this.onTerminal(e.code);
        return;
      }
      this.onDown(e.code);
      setTimeout(() => this._connect(), BACKOFF[Math.min(this.attempt++, BACKOFF.length - 1)]);
    };
  }

  sendAudio(buffer) {
    if (this.open && this.ws.bufferedAmount < 256 * 1024) this.ws.send(buffer);
  }

  send(obj) {
    if (this.open) this.ws.send(JSON.stringify(obj));
  }

  close() {
    this.closed = true;
    try { this.ws?.close(1000); } catch { /* ignore */ }
    this.ws = null;
  }
}
