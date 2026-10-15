// One show on screen: the store, the player and (while live) the socket.
// Views call these actions and read ctl.store / ctl.player; they never touch
// the socket or the timeline directly.
import { api } from "../api.js";
import { audioContext, audioUnlocked, unlockAudio } from "../audio/context.js";
import { ListenTracker } from "../audio/listen-tracker.js";
import { LivePlayer } from "../audio/live-player.js";
import { ReplayPlayer } from "../audio/replay-player.js";
import { PcmTimeline } from "../audio/timeline.js";
import { session } from "../session.js";
import { toast, uid } from "../util.js";
import { LiveSocket } from "./socket.js";
import { ShowStore } from "./store.js";

const KEEP_BEHIND_SECONDS = 300;
const BEHIND_LIVE_SECONDS = 3;

export class ShowController {
  constructor({ id, data, onTerminal }) {
    this.id = id;
    this.live = data.live;
    this.onTerminal = onTerminal || (() => {});
    this.store = new ShowStore({ id, role: data.role, shareUrl: data.share_url });
    this.store.show = data.show;
    this.store.creator = data.creator || null;   // who made it, and whether you subscribe
    this.store.albums = data.albums || [];       // albums it's in that you can see
    this.play = { started: false, paused: false, buffering: false };
    this.connection = "connecting";
    this._subs = new Set();
    this._hearts = 0;
    this._heartTimer = 0;
    this._reported = null;
    this._jumpOnAudio = false;

    if (this.live) {
      const rate = 24000;
      this.timeline = new PcmTimeline(rate);
      this.player = new LivePlayer(audioContext(), this.timeline, rate, { onState: (s) => this._onPlay(s) });
      this.socket = new LiveSocket({
        showId: id,
        have: () => (this.timeline.empty ? null : { from: this.timeline.start, to: this.timeline.end }),
        onEvent: (e) => this._event(e),
        onFrame: (seq, start, pcm) => this._frame(seq, start, pcm),
        onOpen: () => {
          this.connection = "open";
          this._reported = null;
          this._report();
          this._notify();
        },
        onDown: () => {
          this.connection = "reconnecting";
          this._notify();
        },
        onTerminal: (code) => this.onTerminal(code),
        // After the sign-off, the audio we hold plays out; the server closing the room is expected.
        keepAlive: () => !this.store.ended,
      });
    } else {
      this.store.loadReplay(data);
      this.player = new ReplayPlayer(api.audioUrl(id), this.store.rate, { onState: (s) => this._onPlay(s) });
      this.connection = "replay";
      this.listens = new ListenTracker(id, this.player);
    }
  }

  on(fn) {
    this._subs.add(fn);
    return () => this._subs.delete(fn);
  }

  _notify() {
    for (const fn of this._subs) fn();
  }

  // ---- incoming ------------------------------------------------------------------

  _event(e) {
    if (e.type === "snapshot") {
      // A hole between what we hold and what the server resends: start over at live.
      if (!this.timeline.empty && e.backfill_from !== this.timeline.end) {
        this.timeline.clear();
        this._jumpOnAudio = true;
      }
      this.store.applySnapshot(e);
      this._snapshotSeen = true;
      if (this._autoTune) {
        this._autoTune = false;
        this.tuneIn();
      }
      return;
    }
    if (e.type === "error") {
      if (e.fatal) toast(e.message, 8000);
      else if (e.message) toast(e.message);
    }
    this.store.apply(e);
  }

  _frame(seq, start, pcm) {
    let result = this.timeline.add(start, pcm);
    if (result === "gap") {
      this.timeline.clear();
      result = this.timeline.add(start, pcm);
      this._jumpOnAudio = true;
    }
    if (result === "added") this.store.noteAudio(seq, pcm.length);
    if (this._jumpOnAudio && this.player.started) {
      this._jumpOnAudio = false;
      this.player.seek(this.liveEdge());
    }
    this.player.pump();
    if (this.player.started) this.timeline.trim(this.player.position() - KEEP_BEHIND_SECONDS * this.store.rate);
  }

  _onPlay(s) {
    this.play = s;
    this._report();
    this._notify();
  }

  _report() {
    if (!this.live) return;
    const wants = this.player.wantsAudio;
    if (wants !== this._reported) {
      this._reported = wants;
      this.socket.send({ type: "player", playing: wants });
    }
  }

  // ---- playback --------------------------------------------------------------------

  liveEdge() {
    if (!this.live) return this.store.clock.audio_end;
    if (this.timeline.empty) return this.store.clock.live;
    return Math.max(this.timeline.start, Math.min(this.store.liveEstimate(), this.timeline.end));
  }

  get canAutoplay() {
    return this.live ? audioUnlocked() : false;
  }

  async tuneIn() {
    if (this.live) {
      await unlockAudio();
      this.player.start(this.liveEdge());
    } else {
      await this.player.start(0);
    }
  }

  /** Start on our own once we know where live is (the snapshot), if audio is unlocked. */
  tuneInWhenReady() {
    if (this._snapshotSeen) this.tuneIn();
    else this._autoTune = true;
  }

  togglePlay() {
    if (!this.player.started) return this.tuneIn();
    this.player.toggle();
  }

  back15() {
    if (!this.player.started) return;
    this.player.back(15);
  }

  jumpLive() {
    if (!this.player.started) return this.tuneIn();
    if (this.player.paused) this.player.resume();
    this.player.seek(this.liveEdge() - 0.25 * this.store.rate);
  }

  /** Jump to a sample: anywhere in a replay; live, anywhere in the audio this browser
   *  still holds (about 5 minutes back), and past the live edge means live. */
  seekTo(sample) {
    const rate = this.store.rate;
    if (!this.live) {
      const end = this.store.clock.audio_end || Infinity;
      const pos = Math.max(0, Math.min(sample, end - 0.5 * rate));
      return this.player.started ? this.player.seek(pos) : this.player.start(pos);
    }
    if (!this.player.started) return this.tuneIn();
    if (sample >= this.liveEdge() - 0.5 * rate) return this.jumpLive();
    if (this.player.paused) this.player.resume();
    this.player.seek(Math.max(this.timeline.start, sample));
  }

  /** The earliest point a live listener can seek back to (what's still held). */
  heldFrom() {
    return this.live ? (this.timeline.empty ? 0 : this.timeline.start) : 0;
  }

  behindLive(pos) {
    if (!this.live || !this.player.started || this.store.ended) return false;
    return this.liveEdge() - pos > BEHIND_LIVE_SECONDS * this.store.rate;
  }

  /** Seconds of audio past the end of what we hold, for "the hosts are writing" hints. */
  atAudioEdge(pos) {
    if (!this.live) return false;
    return this.timeline.empty || pos >= this.timeline.end - 0.15 * this.store.rate;
  }

  // ---- actions -----------------------------------------------------------------------

  /** Signed-out visitors on a public pod can listen and react, not call in or add context. */
  get canTalk() {
    return this.store.role !== "visitor" && session.signedIn;
  }

  sendComment(text) {
    if (!this.live || this.store.ended || !this.canTalk) return false;
    const client_id = uid();
    this.store.remember(client_id);
    this.socket.send({ type: "comment", text, client_id });
    return true;
  }

  react(n = 1) {
    if (!this.live) return;
    this._hearts += n;
    if (!this._heartTimer) {
      this._heartTimer = setTimeout(() => {
        this.socket.send({ type: "react", n: Math.min(10, this._hearts) });
        this._hearts = 0;
        this._heartTimer = 0;
      }, 500);
    }
  }

  restyle(choice) {
    this.socket?.send({ type: "restyle", ...choice });
  }

  end(mode) {
    this.socket?.send({ type: "end", mode });
  }

  removeComment(id) {
    this.socket?.send({ type: "remove_comment", id });
  }

  async addContext(form, { withComment = "" } = {}) {
    if (withComment) {
      const client_id = uid();
      this.store.remember(client_id);
      form.append("comment", withComment);
      form.append("client_id", client_id);
    }
    return api.addContext(this.id, form);
  }

  shareLink() {
    const path = this.store.shareUrl || `${location.pathname}${location.search}`;
    return new URL(path, location.origin).toString();
  }

  audioDownloadUrl() {
    return api.audioUrl(this.id, true);
  }

  destroy() {
    clearTimeout(this._heartTimer);
    this.listens?.stop();
    this.socket?.close();
    this.player.destroy();
    this._subs.clear();
  }
}
