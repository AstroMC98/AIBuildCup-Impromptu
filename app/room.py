"""A live show and everyone listening to it.

One ShowRoom per live show. It owns the ShowRunner, the live clock, the audio
(a rolling PCM window and the MP3 being built) and every connection. Events from
the runner are cached for late joiners, fanned out, and handed to the Persister.

Joining:  snapshot  ->  audio from a little behind the live edge  ->  live stream.
Binary frames are an 8-byte header, <seq:u32><start_sample:u32>, then s16le PCM.
The start sample places each chunk on the show timeline, so a client that
reconnects or joins late knows exactly where every chunk belongs, and a chunk it
already has is simply ignored.

Every connection gets its own bounded outbound queue and writer task, so one
slow phone never holds up the show or anyone else. A connection that falls more
than OUTBOUND_QUEUE_BYTES behind is closed with 1013 and its client reconnects.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import math
import secrets
import struct
import time
from typing import Callable

from fastapi import WebSocket

from .audio import Mp3Writer, PcmWindow
from .catalog import PRESETS_BY_ID, accent_for, style_label
from .clock import LiveClock
from .config import Settings
from .limits import (
    CONTEXT_CHARS_PER_SHOW, CONTEXT_ITEMS_PER_SHOW, OUTBOUND_QUEUE_BYTES,
    comment_bucket, message_bucket, react_bucket, restyle_bucket,
)
from .llm import Gemini
from .models import ContextItem, ShowRequest
from .show import ShowRunner
from .sources import SourceError
from .stats import TIMELINE_STEP_SECONDS
from .store import Persister, Store

log = logging.getLogger("podcasts.room")

TICK_SECONDS = 0.5
CLOCK_BROADCAST_SECONDS = 2.0
_conn_ids = itertools.count(1)
_background: set[asyncio.Task] = set()  # fire-and-forget tasks, kept alive until done


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


def style_revisions(doc: dict) -> list[dict]:
    """Every style the show has aired in, by revision. Segments carry their
    style_rev, so a late joiner or a replay re-tints exactly as the show did."""
    req = doc.get("request", {})
    first = {"rev": 0, "vibe": req.get("vibe"), "label": req.get("style_label") or style_label(req.get("vibe")),
             "accent": req.get("accent") or accent_for(req.get("vibe"), req.get("style", ""))}
    later = [{k: h.get(k) for k in ("rev", "vibe", "label", "accent")} for h in doc.get("style_history", [])]
    return [first, *later]


def beats_with_times(beats: list[dict], chapters: dict[int, int], end: int | None) -> list[dict]:
    """The plan's beats with where each one starts and ends on the show timeline
    (samples). The last chapter ends at `end`, which is None while it's still on."""
    beats = [dict(b) for b in beats]
    starts = sorted(chapters.items())
    for n, (i, start) in enumerate(starts):
        if i < len(beats):
            beats[i]["start"] = start
            nxt = starts[n + 1][1] if n + 1 < len(starts) else None
            beats[i]["end"] = nxt if nxt is not None else end
    return beats


class Conn:
    """One listener's socket."""

    def __init__(self, ws: WebSocket, role: str, name: str, ip: str, uid: str | None = None,
                 lid: str | None = None):
        self.id = next(_conn_ids)
        self.ws = ws
        self.role = role            # producer (the owner) | listener (signed in) | visitor (public, signed out)
        self.name = name
        self.ip = ip
        self.uid = uid
        self.playing = False
        self.closed = False
        # Their listening session, for the owner's stats (see stats.py).
        self.session_id = f"l-{secrets.token_hex(8)}"
        self.lid = lid              # a random id the browser keeps, so anonymous listeners count once
        self.joined_at = time.time()
        self.join_pos = 0.0
        self.listened_s = 0.0
        self.playing_since: float | None = None
        self.session_saved = False
        self.comments = comment_bucket(role == "producer")
        self.reacts = react_bucket()
        self.messages = message_bucket()
        self.restyles = restyle_bucket()
        self._queue: asyncio.Queue[tuple[str | bytes | None, int]] = asyncio.Queue()
        self._pending = 0
        self._writer = asyncio.create_task(self._write_loop())

    @property
    def producer(self) -> bool:
        return self.role == "producer"

    @property
    def signed_in(self) -> bool:
        return self.role != "visitor"

    def send(self, item: str | bytes) -> None:
        if self.closed:
            return
        size = len(item)
        if self._pending + size > OUTBOUND_QUEUE_BYTES:
            log.warning("listener %d fell too far behind; dropping them", self.id)
            self.close(1013)
            return
        self._pending += size
        self._queue.put_nowait((item, size))

    def error(self, code: str, message: str) -> None:
        self.send(json.dumps({"type": "error", "fatal": False, "code": code, "message": message}))

    async def _write_loop(self) -> None:
        try:
            while True:
                item, size = await self._queue.get()
                if item is None:
                    break
                self._pending -= size
                if isinstance(item, bytes):
                    await self.ws.send_bytes(item)
                else:
                    await self.ws.send_text(item)
        except Exception:  # socket gone; the receive side notices and calls leave()
            self.closed = True

    def close(self, code: int = 1000) -> None:
        if self.closed:
            return
        self.closed = True
        self._queue.put_nowait((None, 0))

        async def shut() -> None:
            try:
                await asyncio.wait_for(self._writer, timeout=2)
            except (asyncio.TimeoutError, Exception):
                self._writer.cancel()
            try:
                await self.ws.close(code)
            except Exception:
                pass
        _spawn(shut())


class ShowRoom:
    def __init__(
        self,
        *,
        doc: dict,
        request: ShowRequest,
        source: str,
        gemini: Gemini,
        settings: Settings,
        store: Store,
        on_closed: Callable[[str], None],
        intro: bytes = b"",
    ):
        """intro: the theme to open on (s16le), played while the episode is planned."""
        self.id: str = doc["id"]
        self._intro = intro
        self.doc = doc
        self.settings = settings
        self.store = store
        self.rate = settings.sample_rate
        self._on_closed = on_closed

        self.clock = LiveClock(self.rate, settings.clock_speed)
        self.wake = asyncio.Event()
        self.conns: dict[int, Conn] = {}
        self.pcm = PcmWindow()
        self.mp3 = Mp3Writer(self.rate, settings.mp3_kbps)
        self.persist = Persister(store, self.id)

        # caches for snapshots
        self.state = "planning"
        self.plan: dict | None = None
        self.segments: dict[int, dict] = {}
        self.comments: dict[int, dict] = {}
        self.context: dict[int, dict] = {}
        self.chapters: dict[int, int] = {}
        self.hearts = 0
        self._burst = 0
        self.style = {
            "rev": 0, "vibe": doc.get("vibe"), "label": doc.get("style_label", ""),
            "accent": doc.get("accent"), "style": request.style,
        }
        self.peak_listeners = 0
        self.listener_timeline: list[list[int]] = []   # [seconds into the show, listening]

        self.ended_reason: str | None = None
        self.ended_at: float | None = None
        self.saved: dict | None = None
        self.finalized = asyncio.Event()
        self._end_reason: str | None = None  # set when the room, not the hosts, ends the show
        self._started = time.monotonic()
        self._idle_since = time.monotonic()
        self._last_clock_broadcast = 0.0

        self.runner = ShowRunner(request, source, gemini, settings, self)
        self._runner_task: asyncio.Task[str] | None = None
        self._main = asyncio.create_task(self._run())
        self._ticker = asyncio.create_task(self._tick_loop())

    # ---- RoomPort: what the runner calls ---------------------------------------

    def publish(self, event: dict) -> None:
        kind = event["type"]
        if kind == "status":
            self.state = event["state"]
        elif kind == "plan":
            event = {**event, "accent": self.style["accent"]}
            self.plan = event
            fields = {k: event[k] for k in ("title", "tagline", "about", "hosts", "beats")}
            self.doc.update(fields)
            self.persist.show(**fields)
        elif kind == "segment":
            seg = {k: v for k, v in event.items() if k != "type"}
            seg["samples"] = None
            self.segments[seg["seq"]] = seg
            self.persist.segment(seg)
        elif kind == "segment_end":
            seg = self.segments.get(event["seq"])
            if seg is not None:
                seg["samples"] = event["samples"]
                self.persist.segment(seg)
            self.doc["samples"] = self.clock.audio_end
            self.persist.show(samples=self.clock.audio_end, duration_s=round(self.clock.audio_end / self.rate, 2))
        elif kind == "chapter":
            self.chapters[event["i"]] = event["start"]
            self.doc["beats"] = self._beats_with_times()
            self.persist.show(beats=self.doc["beats"])
        elif kind == "comment":
            c = {k: v for k, v in event.items() if k != "type"}
            self.comments[c["id"]] = c
            self.persist.comment(c)
        elif kind == "context":
            x = {k: v for k, v in event.items() if k != "type"}
            self.context[x["id"]] = x
            self.persist.context({**x, "text_path": f"context/{x['id']}.txt"})
        elif kind == "restyle":
            pass  # cached by restyle() before publishing
        self._broadcast(json.dumps(event))

    def push_audio(self, seq: int, pcm: bytes) -> None:
        start = self.clock.audio_end
        self.clock.add_audio(len(pcm) // 2)
        self.pcm.add(start, seq, pcm)
        self.mp3.feed(pcm)
        frame = struct.pack("<II", seq, start) + pcm
        for conn in list(self.conns.values()):
            conn.send(frame)
        if self.state in {"planning", "writing"}:
            self._set_state(self._listening_state())
        self.wake.set()

    # ---- listeners ----------------------------------------------------------------

    def conns_from(self, ip: str) -> int:
        return sum(c.ip == ip for c in self.conns.values())

    def join(self, conn: Conn, have_from: int | None, have_to: int | None) -> None:
        """Snapshot, then audio from a little behind live (or from where the client
        left off, if that is still in memory), then the live stream."""
        self.clock.tick()
        window_start = self.pcm.start
        if window_start is None:
            send_from = self.clock.audio_end
        elif have_to is not None and have_from is not None and window_start <= have_to <= self.clock.audio_end:
            send_from = have_to
        else:
            send_from = self._backfill_from()
        conn.send(json.dumps(self.snapshot(conn, send_from)))
        for start, seq, pcm in self.pcm.since(send_from):
            conn.send(struct.pack("<II", seq, start) + pcm)
        conn.joined_at = time.time()
        conn.join_pos = round(self.clock.live / self.rate, 1)
        self.conns[conn.id] = conn
        self.peak_listeners = max(self.peak_listeners, len(self.conns))
        self._listening_changed()

    def leave(self, conn: Conn) -> None:
        if self.conns.pop(conn.id, None) is not None:
            self._save_session(conn)
            conn.close()
            self._listening_changed()
            if not self.conns:
                self._idle_since = time.monotonic()

    def _backfill_from(self) -> int:
        target = max(0, int(self.clock.live) - int(self.settings.backfill_seconds * self.rate))
        start = target
        for seg in self.segments.values():
            length = seg["samples"] if seg["samples"] is not None else self.clock.audio_end - seg["start"]
            if seg["start"] <= target < seg["start"] + max(length, 1):
                start = seg["start"]
                break
        else:
            first = min((s["start"] for s in self.segments.values()), default=None)
            if first is None or target < first:
                start = 0   # still in the intro theme: from its beginning
        return max(start, self.pcm.start or 0)

    def _listening_state(self) -> str:
        return "live" if any(c.playing for c in self.conns.values()) else "holding"

    def _listening_changed(self) -> None:
        self.clock.set_listening(sum(c.playing for c in self.conns.values()))
        if self.state in {"live", "holding"}:
            self._set_state(self._listening_state())
        self.wake.set()

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self._broadcast(json.dumps({"type": "status", "state": state}))

    def _broadcast(self, text: str) -> None:
        for conn in list(self.conns.values()):
            conn.send(text)

    # ---- what listeners send ------------------------------------------------------

    def handle(self, conn: Conn, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "player":
            self._count_listening(conn, bool(msg.get("playing")))
            conn.playing = bool(msg.get("playing"))
            self._listening_changed()
        elif kind == "comment":
            self._comment(conn, msg)
        elif kind == "react":
            self._react(conn, msg)
        elif kind == "restyle":
            self._restyle(conn, msg)
        elif kind == "end":
            if not conn.producer:
                return conn.error("forbidden", "Only the producer can end the show.")
            if msg.get("mode") == "now":
                self.end_now("stopped")
            elif not self.runner.end_requested:
                self.runner.request_end()
                self._set_state("ending")
        elif kind == "remove_comment":
            if not conn.producer:
                return conn.error("forbidden", "Only the producer can remove call-ins.")
            try:
                self.runner.remove_comment(int(msg.get("id")))
            except (TypeError, ValueError):
                pass

    def _comment(self, conn: Conn, msg: dict) -> None:
        if not conn.signed_in:
            return conn.error("sign_in", "Sign in to call in.")
        if self.ended_reason:
            return conn.error("ended", "The show has ended.")
        if not conn.comments.take():
            return conn.error("rate_limited", "Easy there. You can call in again in a few seconds.")
        # Call-ins go out under the account's name, never a name the client picks.
        _, err = self.runner.add_comment(
            conn.name, str(msg.get("text", "")), conn.role, client_id=str(msg.get("client_id", "")),
        )
        if err == "line_full":
            conn.error("line_full", "The lines are full right now. Try again in a minute.")

    def _react(self, conn: Conn, msg: dict) -> None:
        try:
            n = max(1, min(10, int(msg.get("n", 1))))
        except (TypeError, ValueError):
            return
        if conn.reacts.take(n):
            self.hearts += n
            self._burst += n
            self.runner.note_hearts(n)

    def _restyle(self, conn: Conn, msg: dict) -> None:
        if not conn.producer:
            return conn.error("forbidden", "Only the producer can restyle the show.")
        if self.ended_reason or self.runner.finished_writing:
            return conn.error("ended", "The show is wrapping up.")
        if not conn.restyles.take():
            return conn.error("rate_limited", "Give the hosts a moment with the last style first.")
        vibe = msg.get("vibe")
        preset = PRESETS_BY_ID.get(vibe or "")
        if preset:
            style, label = preset["prompt"], preset["label"]
        else:
            vibe = None
            style = str(msg.get("style", "")).strip()[:2000]
            label = "Custom style"
            if not style:
                return conn.error("empty", "Pick a vibe or describe a style.")
        rev = self.runner.restyle(style)
        self.style = {"rev": rev, "vibe": vibe, "label": label, "accent": accent_for(vibe, style), "style": style}
        history = [*self.doc.get("style_history", []), {
            "rev": rev, "at": int(self.clock.live), "vibe": vibe, "style": style,
            "label": label, "accent": self.style["accent"],
        }]
        self.doc.update(style=style, vibe=vibe, style_label=label, accent=self.style["accent"], style_history=history)
        self.persist.show(style=style, vibe=vibe, style_label=label, accent=self.style["accent"], style_history=history)
        self.publish({"type": "restyle", **self.style})

    def add_context(
        self, item: ContextItem, role: str, comment: str = "", caller: str = "", client_id: str = ""
    ) -> tuple[ContextItem, dict | None]:
        """Hand something to the hosts. With a comment, it rides along with that call-in."""
        if self.ended_reason or self.runner.finished_writing:
            raise SourceError("The show is wrapping up, so it's too late to add anything.")
        items = self.runner.context.values()
        if len(items) >= CONTEXT_ITEMS_PER_SHOW:
            raise SourceError("This show already has as much added context as it can take.")
        if sum(len(x.text) for x in items) + len(item.text) > CONTEXT_CHARS_PER_SHOW:
            raise SourceError("That's more material than this show can take. Try something shorter.")
        if comment.strip():
            item.when = "now"
        item = self.runner.add_context(item)
        posted = None
        if comment.strip():
            c, err = self.runner.add_comment(caller, comment, role, client_id=client_id, context_id=item.id)
            if c is not None:
                self.runner.link_comment(item, c)
                posted = c.public()
            elif err == "line_full":
                raise SourceError("The lines are full right now. Try again in a minute.")
        return item, posted

    # ---- the show's life --------------------------------------------------------

    def end_now(self, reason: str) -> None:
        self._end_reason = self._end_reason or reason
        self.runner.stop()
        if self._runner_task and not self._runner_task.done():
            self._runner_task.cancel()

    async def _run(self) -> None:
        if self._intro:
            self.push_audio(0, self._intro)   # seq 0: audio before the first segment
        self._runner_task = asyncio.create_task(self.runner.run())
        try:
            reason = await self._runner_task
        except asyncio.CancelledError:
            reason = self._end_reason or "stopped"
        except Exception:
            log.exception("show %s crashed", self.id)
            reason = "error"
        if self._end_reason and reason in {"signed_off", "stopped"}:
            reason = self._end_reason
        await self._finalize(reason)

    async def _finalize(self, reason: str) -> None:
        self.ended_reason = reason
        self.ended_at = time.monotonic()
        self._set_state("ended")
        self._broadcast(json.dumps({"type": "ended", "reason": reason}))

        status = {"error": "failed", "shutdown": "interrupted"}.get(reason, "finished")
        audio = None
        data = self.mp3.finish()
        if data:
            try:
                audio = await self.store.put_audio(self.id, data)
            except Exception:
                log.exception("could not save the audio for show %s", self.id)
        duration = round(self.clock.audio_end / self.rate, 2)
        refund = await self._refund_unused(duration)
        fields = {
            "credits_refunded": refund,
            "status": status,
            "ended_reason": reason,
            "ended_at": time.time(),
            "samples": self.clock.audio_end,
            "duration_s": duration,
            "audio": audio,
            "beats": self._beats_with_times(),
            "counts": {
                "segments": len(self.segments),
                "comments": len(self.comments),
                "context": len(self.context),
                "hearts": self.hearts,
                "peak_listeners": self.peak_listeners,
            },
        }
        fields["listener_timeline"] = self.listener_timeline
        for conn in list(self.conns.values()):
            self._save_session(conn, to_end=True)
        self.doc.update(fields)
        self.persist.show(**fields)
        await self.persist.close()
        if audio:
            self.saved = {"audio_url": f"/api/shows/{self.id}/audio", "duration": duration}
            self._broadcast(json.dumps({"type": "saved", **self.saved}))
        self.finalized.set()
        log.info("show %s ended (%s), %.0fs of audio", self.id, reason, duration)

    async def _refund_unused(self, duration_s: float) -> int:
        """Credits are charged up front for the length asked for; give back what
        wasn't used (all of it if nothing was ever voiced)."""
        charged = int(self.doc.get("credits_charged") or 0)
        owner = self.doc.get("owner_uid")
        if not charged or not owner:
            return 0
        used = math.ceil(duration_s / 60 * self.settings.credits_per_minute) if duration_s else 0
        refund = max(0, charged - used)
        if refund:
            try:
                await self.store.adjust_credits(owner, refund, reason="refund", show_id=self.id,
                                                note="Unused minutes")
            except Exception:
                log.exception("could not refund %d credits for show %s", refund, self.id)
                return 0
        return refund

    async def _tick_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(TICK_SECONDS)
                live = self.clock.tick()
                self.pcm.trim(int(live - self.settings.retain_behind_seconds * self.rate))
                if self._burst:
                    self._broadcast(json.dumps({"type": "react", "total": self.hearts, "burst": self._burst}))
                    self._burst = 0
                now = time.monotonic()
                if now - self._last_clock_broadcast >= CLOCK_BROADCAST_SECONDS:
                    self._last_clock_broadcast = now
                    self._broadcast(json.dumps(self.clock_event()))

                if self.ended_reason is None:
                    self._sample_listeners()
                    idle = not self.conns and now - self._idle_since > self.settings.idle_end_seconds
                    if idle and not self.runner.end_urgent:
                        log.info("show %s: nobody listening; signing off", self.id)
                        self._end_reason = "idle"
                        self.runner.request_end(urgent=True)
                        self._set_state("ending")
                    if now - self._started > self.settings.max_show_hours * 3600:
                        self.end_now("stopped")
                elif self.finalized.is_set():
                    grace = now - (self.ended_at or now) > self.settings.ended_room_grace_seconds
                    if not self.conns or grace:
                        self.close()
                        return
        except asyncio.CancelledError:
            pass

    def close(self, code: int = 1000) -> None:
        for conn in list(self.conns.values()):
            conn.close(code)
        self.conns.clear()
        self._ticker.cancel()
        self._on_closed(self.id)

    # ---- listening stats ------------------------------------------------------------

    def listening_now(self) -> int:
        """Listeners playing right now, not counting the pod's owner."""
        return sum(1 for c in self.conns.values() if c.playing and not c.producer)

    def _count_listening(self, conn: Conn, playing: bool) -> None:
        now = time.monotonic()
        if conn.playing and not playing and conn.playing_since is not None:
            conn.listened_s += (now - conn.playing_since) * self.clock.speed
            conn.playing_since = None
        elif playing and not conn.playing:
            conn.playing_since = now

    def _sample_listeners(self) -> None:
        live_s = int(self.clock.live / self.rate)
        last = self.listener_timeline[-1] if self.listener_timeline else None
        if last is None or live_s - last[0] >= TIMELINE_STEP_SECONDS:
            self.listener_timeline.append([live_s, self.listening_now()])
            self.persist.show(listener_timeline=self.listener_timeline)

    def open_sessions(self) -> list[dict]:
        """Sessions of listeners still connected, as they'd be saved right now (for
        stats during a live pod). Nothing is saved or changed."""
        now = time.monotonic()
        out = []
        for c in self.conns.values():
            if c.producer or c.session_saved:
                continue
            playing = (now - c.playing_since) * self.clock.speed if c.playing and c.playing_since else 0.0
            out.append({
                "id": c.session_id, "kind": "live", "uid": c.uid, "name": c.name if c.uid else None,
                "lid": c.lid, "role": c.role, "joined_at": c.joined_at, "left_at": None,
                "join_pos": c.join_pos, "leave_pos": round(self.clock.live / self.rate, 1),
                "listened_s": round(c.listened_s + playing, 1), "to_end": True, "here_now": True,
            })
        return out

    def _save_session(self, conn: Conn, to_end: bool = False) -> None:
        """One listen, for the owner's stats. The owner's own listening isn't counted."""
        if conn.session_saved or conn.producer:
            return
        conn.session_saved = True
        self._count_listening(conn, False)
        self.persist.session({
            "id": conn.session_id, "kind": "live", "uid": conn.uid,
            "name": conn.name if conn.uid else None, "lid": conn.lid, "role": conn.role,
            "joined_at": conn.joined_at, "left_at": time.time(),
            "join_pos": conn.join_pos, "leave_pos": round(self.clock.live / self.rate, 1),
            "listened_s": round(conn.listened_s, 1), "to_end": to_end,
        })

    # ---- views --------------------------------------------------------------------

    def clock_event(self) -> dict:
        return {
            "type": "tick",
            "live": int(self.clock.live),
            "audio_end": self.clock.audio_end,
            "listeners": len(self.conns),
            "playing": self.clock.playing,
            "speed": self.clock.speed,
        }

    def _beats_with_times(self) -> list[dict]:
        end = self.clock.audio_end if self.ended_reason else None
        return beats_with_times((self.plan or {}).get("beats", []), self.chapters, end)

    def public_show(self) -> dict:
        d = self.doc
        return {
            "id": self.id,
            "status": "live" if self.ended_reason is None else d.get("status"),
            "title": d.get("title", ""),
            "tagline": d.get("tagline", ""),
            "about": d.get("about", ""),
            "accent": self.style["accent"],
            "vibe": self.style["vibe"],
            "style_label": self.style["label"],
            "minutes": d.get("minutes"),
            "language": d.get("language"),
            "hosts": d.get("hosts", []),
            "sources": d.get("source", {}).get("names", []),
            "created_at": d.get("created_at"),
            "owner_uid": d.get("owner_uid"),
            "owner_name": d.get("owner_name", ""),
            "visibility": d.get("visibility", "private"),
            "shared_with": d.get("shared_with", []),
            "album_ids": d.get("album_ids", []),
            "tags": d.get("tags", []),
        }

    def snapshot(self, conn: Conn, backfill_from: int) -> dict:
        return {
            "type": "snapshot",
            "show": self.public_show(),
            "role": conn.role,
            "share_url": f"/show/{self.id}",
            "plan": self.plan,
            "chapters": [{"i": i, "start": s} for i, s in sorted(self.chapters.items())],
            "segments": [self.segments[k] for k in sorted(self.segments)],
            "comments": [self.comments[k] for k in sorted(self.comments)],
            "context": [self.context[k] for k in sorted(self.context)],
            "hearts": self.hearts,
            "style": self.style,
            "styles": style_revisions(self.doc),
            "state": self.state,
            "rate": self.rate,
            "clock": self.clock_event(),
            "backfill_from": backfill_from,
            "ended": self.ended_reason,
            "saved": self.saved,
        }


class Rooms:
    """Every live room in this process. Rooms live in memory, which is why the
    Cloud Run service runs a single instance (see README)."""

    def __init__(self) -> None:
        self._rooms: dict[str, ShowRoom] = {}

    def get(self, show_id: str) -> ShowRoom | None:
        return self._rooms.get(show_id)

    def add(self, room: ShowRoom) -> None:
        self._rooms[room.id] = room

    def remove(self, show_id: str) -> None:
        self._rooms.pop(show_id, None)

    def ids(self) -> set[str]:
        return set(self._rooms)

    def live_count(self) -> int:
        return sum(r.ended_reason is None for r in self._rooms.values())

    async def shutdown(self, timeout: float) -> None:
        """On SIGTERM: save every live show as interrupted, then tell clients to retry."""
        live = [r for r in self._rooms.values() if not r.finalized.is_set()]
        for room in live:
            room.end_now("shutdown")
        if live:
            await asyncio.wait([asyncio.create_task(r.finalized.wait()) for r in live], timeout=timeout)
        for room in list(self._rooms.values()):
            room.close(1012)
