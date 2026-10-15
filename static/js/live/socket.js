// The live connection to a show. Reconnects with backoff and tells the server
// which audio it already holds, so it only resends what's missing.
import { listenerId } from "../util.js";

const TERMINAL = new Set([4400, 4401, 4403, 4404, 4409]);
const BACKOFF = [500, 1000, 2000, 4000, 8000];

export class LiveSocket {
  constructor({ showId, have, onEvent, onFrame, onOpen, onDown, onTerminal, keepAlive }) {
    this.keepAlive = keepAlive || (() => true);  // false once the show is over: no point reconnecting
    this.showId = showId;
    this.have = have;              // () => ({ from, to }) | null
    this.onEvent = onEvent;
    this.onFrame = onFrame;
    this.onOpen = onOpen || (() => {});
    this.onDown = onDown || (() => {});
    this.onTerminal = onTerminal || (() => {});
    this.ws = null;
    this.closed = false;
    this.attempt = 0;
    this.outbox = [];
    this._connect();
  }

  _url() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${location.host}/ws/show/${encodeURIComponent(this.showId)}`;
  }

  _connect() {
    if (this.closed) return;
    const ws = new WebSocket(this._url());
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onopen = () => {
      this.attempt = 0;
      const have = this.have?.();
      ws.send(JSON.stringify({ type: "join", lid: listenerId(), ...(have ? { have_from: have.from, have_to: have.to } : {}) }));
      const queued = this.outbox.splice(0);
      for (const m of queued) ws.send(m);
      this.onOpen();
    };
    ws.onmessage = (e) => {
      if (typeof e.data === "string") {
        this.onEvent(JSON.parse(e.data));
      } else if (e.data.byteLength > 8) {
        const view = new DataView(e.data);
        const seq = view.getUint32(0, true);
        const start = view.getUint32(4, true);
        this.onFrame(seq, start, new Int16Array(e.data, 8));
      }
    };
    ws.onclose = (e) => {
      if (this.ws !== ws) return;
      this.ws = null;
      if (this.closed) return;
      if (!this.keepAlive()) {
        this.closed = true;
        return;
      }
      if (TERMINAL.has(e.code)) {
        this.closed = true;
        this.onTerminal(e.code);
        return;
      }
      this.onDown(e.code);
      const wait = BACKOFF[Math.min(this.attempt++, BACKOFF.length - 1)];
      setTimeout(() => this._connect(), wait);
    };
  }

  send(obj) {
    const msg = JSON.stringify(obj);
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(msg);
    else if (obj.type === "comment") this.outbox.push(msg);  // keep call-ins across a blip
  }

  close() {
    this.closed = true;
    try { this.ws?.close(1000); } catch { /* ignore */ }
    this.ws = null;
  }
}
