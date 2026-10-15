"""A live co-host call: you talk, an AI co-host answers, and hanging up saves it as an episode.

One CohostSession per call. The phone streams its mic to /ws/cohost/{id} and plays
whatever comes back; the session sits in between, and the Gemini Live session it
holds (llm.cohost_link) does the listening, the turn-taking and the talking. On the
way through nothing is awaited but the two sockets, so the co-host answers as
quickly as the model does.

  browser -> server  binary: 16 kHz mono s16le mic audio, ~40 ms a frame, sent the
                     whole call, silence included (it is the call's clock)
                     JSON: join | mode {mode} | cut_in | hangup
  server -> browser  binary: <turn:u32 LE> + 24 kHz mono s16le, the co-host's voice
                     JSON: ready | state | said | interrupted | turn_done | mode |
                           wrap | editing | saved | ended | error

Cutting in: Gemini stops a turn when it hears you over it (`interrupted`). It sends
audio faster than the phone plays it, though, so a long answer can be fully sent
and still playing when you start talking; the phone notices that itself, stops
playback and sends `cut_in`.

Everything is recorded as it happens (editing.Take). Hanging up, running out of
time, or the phone staying away longer than cohost_reconnect_seconds ends the call;
the take is edited (editing.py, with notes from llm.edit_cohost) and saved as an
ordinary pod with kind "cohost", which replays like any other.

Gemini recycles a Live connection every few minutes (GoAway), and one can drop.
The session reconnects with the latest resumption handle, so the co-host keeps the
whole conversation; without one, it starts afresh with a recap.
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from typing import Callable

from . import prompts
from .audio import Mp3Writer
from .catalog import COHOST_MODES_BY_ID, VOICE_GENDER, VOICES, pick_voice
from .config import Settings
from .editing import RATE, Take, assemble, build_edit, fallback_plan, raw_mix, scored
from .limits import TokenBucket
from .llm import (
    AudioOut, CohostLink, Gemini, GoAway, InputText, Interrupted, OutputText, ResumeHandle, TurnDone,
)
from .mixer import Cues
from .models import CohostRequest, Host, HostRequest
from .music import Library, theme_for
from .room import Conn, refund_unused
from .store import Store

log = logging.getLogger("podcasts.cohost")

TICK_SECONDS = 0.5
WRAP_UP_SECONDS = 30         # the co-host is told to start wrapping up this long before the end
LINK_RETRIES = 2             # reconnects in a row before the call is ended and saved
RECAP_LINES = 16
SAID_KEPT = 12               # caption entries a rejoining phone gets back
MIC_BYTES_PER_SECOND = 32_000
FRAME_BYTES = 16 * 1024
_background: set[asyncio.Task] = set()

DEFAULT_PERSONA = "A warm, quick-witted podcast co-host who is curious about everything."


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


def cast_cohost(wanted: HostRequest) -> Host:
    """The co-host as asked for. A voice that was picked wins and the gender follows
    it; otherwise the voice is picked to match the gender, so they always agree."""
    if wanted.voice in VOICES:
        voice, gender = wanted.voice, VOICE_GENDER[wanted.voice]
    else:
        gender = wanted.gender or "female"
        voice = pick_voice(gender)
    return Host(
        name=(wanted.name.strip() or "Alex")[:40],
        persona=wanted.persona.strip() or DEFAULT_PERSONA,
        voice=voice, gender=gender,
        likes=wanted.likes.strip(), specialties=wanted.specialties.strip(), skills=wanted.skills.strip(),
    )


def mic_bucket() -> TokenBucket:
    """Mic bytes: real time is 32 KB/s; allow twice that, plus a catch-up burst."""
    return TokenBucket(2 * MIC_BYTES_PER_SECOND, 8 * MIC_BYTES_PER_SECOND)


class CohostSession:
    def __init__(
        self,
        *,
        doc: dict,
        request: CohostRequest,
        host: Host,
        you: str,
        notes: str,
        gemini: Gemini,
        settings: Settings,
        store: Store,
        on_closed: Callable[[str], None],
        music: Library | None = None,
    ):
        """music: the library the episode's theme comes from (none: open and close on a chime)."""
        self.id: str = doc["id"]
        self.doc = doc
        self.request = request
        self.host = host
        self.you = you
        self.notes = notes
        self.gemini = gemini
        self.settings = settings
        self.store = store
        self.music = music
        self._on_closed = on_closed

        self.take = Take()
        self.conn: Conn | None = None
        self.state = "connecting"   # connecting | live | reconnecting | editing | saved | ended
        self.mode = request.mode
        self.revs: list[tuple[int, int]] = []        # (call clock, style rev) per mode switch
        self.limit_s = request.minutes * 60
        self.started: float | None = None             # when the co-host came on air
        self.resume_handle: str | None = None
        self.ended_reason: str | None = None
        self.saved: dict | None = None
        self.finalized = asyncio.Event()

        self._joined = asyncio.Event()
        self._hangup = asyncio.Event()
        self._end_reason: str | None = None
        self._link: CohostLink | None = None
        self._send_lock = asyncio.Lock()
        self._left_at: float | None = time.monotonic()
        self._wrapped = False
        # You cut in on the phone while the model was still sending that answer: the
        # rest of it is dropped until the model ends the turn (or a few seconds pass).
        self._discard_since: float | None = None
        self._refunded: int | None = None
        self._said: list[dict] = []
        self._modes = TokenBucket(1 / 5, 2)
        self._main = asyncio.create_task(self._run())
        self._ticker = asyncio.create_task(self._tick_loop())

    # ---- the phone ----------------------------------------------------------------

    def attach(self, conn: Conn) -> None:
        """The phone (re)joined. A newer connection replaces an older one."""
        old, self.conn = self.conn, conn
        if old is not None:
            old.close(4410)
        self._left_at = None
        self._joined.set()
        conn.send(json.dumps(self.ready_event()))

    def detach(self, conn: Conn) -> None:
        if self.conn is conn:
            self.conn = None
            self._left_at = time.monotonic()
            if self._link is not None and self.ended_reason is None:
                _spawn(self._quietly(self._link.pause()))
        conn.close()

    async def on_audio(self, conn: Conn, pcm: bytes) -> None:
        if conn is not self.conn or self.ended_reason is not None or self._hangup.is_set():
            return
        self.take.add_mic(pcm)
        link = self._link
        if link is not None and self.state == "live":
            try:
                async with self._send_lock:
                    await link.send_audio(pcm)
            except Exception:  # the connection is going; the pump notices and reconnects
                pass

    def handle(self, conn: Conn, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "hangup":
            self.end_now("hung_up")
        elif kind == "cut_in":
            if self.take.current is not None:
                self._discard_since = time.monotonic()
            turn = self.take.interrupt()
            if turn is not None:
                log.info("call %s: cut in on turn %d", self.id, turn.id)
        elif kind == "mode":
            mode = msg.get("mode")
            if mode not in COHOST_MODES_BY_ID or mode == self.mode or self.ended_reason:
                return
            if not self._modes.take():
                conn.error("slow_down", "One switch at a time.")
                return
            self._switch_mode(mode)

    def end_now(self, reason: str) -> None:
        self._end_reason = self._end_reason or reason
        self._hangup.set()

    # ---- the call -----------------------------------------------------------------

    async def _run(self) -> None:
        try:
            reason = await self._call()
        except asyncio.CancelledError:
            reason = "stopped"
        except Exception:
            log.exception("call %s failed", self.id)
            reason = "error"
        await self._finalize(self._end_reason or reason)

    async def _call(self) -> str:
        try:
            await asyncio.wait_for(self._joined.wait(), timeout=self.settings.cohost_reconnect_seconds)
        except asyncio.TimeoutError:
            return "never_joined"
        retries, fresh = 0, False
        while not self._hangup.is_set():
            try:
                async with self.gemini.cohost_link(system=self._system(), voice=self.host.voice,
                                                   handle=self.resume_handle) as link:
                    self._link = link
                    retries = 0
                    if self.started is None:
                        self.started = time.monotonic()
                        intro = self.doc.get("intro") or {}
                        await self._send_text(prompts.cohost_opening(
                            self.you, self.request.minutes, intro.get("show_name", ""), intro.get("intro_line", "")))
                    elif fresh:
                        await self._send_text(prompts.cohost_resume(self.you, self._recap()))
                    self._set_state("live")
                    if await self._pump(link) == "ended":
                        break
                    fresh = self.resume_handle is None   # GoAway: straight back with the handle
            except Exception as exc:
                if self._hangup.is_set():
                    break
                retries += 1
                log.warning("call %s: Live connection failed (%s), attempt %d", self.id, exc, retries)
                if retries > LINK_RETRIES:
                    if self.started is None:
                        self._error("The co-host couldn't come on the line. Try again in a minute.")
                        return "error"
                    return "link_lost"
                fresh = self.resume_handle is None
                self._set_state("reconnecting")
                await asyncio.sleep(0.5 * retries)
            finally:
                self._link = None
        return self._end_reason or "hung_up"

    async def _pump(self, link: CohostLink) -> str:
        """Model -> phone until the call ends ("ended") or Gemini recycles the
        connection ("goaway"). Raises if the connection drops."""
        events = link.events().__aiter__()
        stop = asyncio.create_task(self._hangup.wait())
        try:
            while True:
                nxt = asyncio.ensure_future(events.__anext__())
                done, _ = await asyncio.wait({nxt, stop}, return_when=asyncio.FIRST_COMPLETED)
                if stop in done:
                    nxt.cancel()
                    return "ended"
                try:
                    event = nxt.result()
                except StopAsyncIteration:
                    raise ConnectionError("the Live session closed") from None
                if isinstance(event, GoAway):
                    log.info("call %s: Live connection recycled (%.0fs left)", self.id, event.seconds or 0)
                    return "goaway"
                self._on_event(event)
        finally:
            stop.cancel()

    @property
    def _discarding(self) -> bool:
        if self._discard_since is not None and time.monotonic() - self._discard_since > 5:
            self._discard_since = None
        return self._discard_since is not None

    def _on_event(self, event) -> None:
        take = self.take
        if isinstance(event, AudioOut):
            if self._discarding:
                return
            turn, pcm = take.add_ai_audio(event.data)
            if pcm and self.conn is not None:
                self.conn.send(struct.pack("<I", turn.id) + pcm)
        elif isinstance(event, OutputText):
            if not self._discarding:
                turn = take.add_ai_text(event.text)
                self._say("host", turn.id, event.text)
        elif isinstance(event, InputText):
            take.add_heard(event.text)
            self._say("you", len(take.turns), event.text)
        elif isinstance(event, Interrupted):
            self._discard_since = None
            take.interrupt()
            if take.turns:
                self._send({"type": "interrupted", "turn": take.turns[-1].id})
        elif isinstance(event, TurnDone):
            self._discard_since = None
            turn = take.finish_turn()
            if turn is not None:
                self._send({"type": "turn_done", "turn": turn.id})
        elif isinstance(event, ResumeHandle):
            self.resume_handle = event.handle

    async def _tick_loop(self) -> None:
        try:
            while not self.finalized.is_set():
                await asyncio.sleep(TICK_SECONDS)
                if self._hangup.is_set():
                    continue
                now = time.monotonic()
                if self.started is not None:
                    elapsed = now - self.started
                    if not self._wrapped and elapsed >= self.limit_s - WRAP_UP_SECONDS:
                        self._wrapped = True
                        left = max(0, round(self.limit_s - elapsed))
                        self._send({"type": "wrap", "left_s": left})
                        _spawn(self._quietly(self._send_text(prompts.cohost_wrap_up(self.you, left))))
                    if elapsed >= self.limit_s:
                        log.info("call %s: time's up", self.id)
                        self.end_now("time")
                away = self._left_at is not None and self._joined.is_set()
                if away and now - self._left_at > self.settings.cohost_reconnect_seconds:
                    log.info("call %s: the phone didn't come back; saving", self.id)
                    self.end_now("dropped")
        except asyncio.CancelledError:
            pass

    def _switch_mode(self, mode: str) -> None:
        self.mode = mode
        info = COHOST_MODES_BY_ID[mode]
        rev = len(self.revs) + 1
        self.revs.append((self.take.now, rev))
        history = [*self.doc.get("style_history", []), {
            "rev": rev, "at": self.take.now, "vibe": None, "style": info["prompt"],
            "label": f"Co-host · {info['label']}", "accent": info["accent"],
        }]
        self.doc.update(style_history=history, style=info["prompt"], style_label=history[-1]["label"],
                        accent=info["accent"])
        if self._link is not None:
            _spawn(self._quietly(self._send_text(prompts.cohost_mode_switch(mode))))
        self._send({"type": "mode", "mode": mode, "accent": info["accent"]})

    # ---- after the call --------------------------------------------------------------

    async def _finalize(self, reason: str) -> None:
        self.ended_reason = reason
        self._ticker.cancel()
        call_s = time.monotonic() - self.started if self.started is not None else 0.0
        try:
            if reason == "deleted":    # the owner deleted it mid-call: nothing to save
                await self._refund(call_s)
            else:
                await self._save(reason, call_s)
        except Exception:
            log.exception("call %s: could not save the episode", self.id)
            refund = await self._refund(call_s)
            fields = {"status": "failed", "ended_reason": "error", "ended_at": time.time(),
                      "credits_refunded": refund}
            self.doc.update(fields)
            try:
                await self.store.update_show(self.id, fields)
            except Exception:
                log.exception("call %s: could not mark it failed", self.id)
            self._send({"type": "ended", "reason": "error",
                        "message": "Something went wrong saving this episode. Your unused credits are back."})
        finally:
            self.finalized.set()
            _spawn(self._close_soon())

    async def _save(self, reason: str, call_s: float) -> None:
        self._set_state("editing")
        self.doc["status"] = "editing"
        await self.store.update_show(self.id, {"status": "editing"})

        self._step("listening")
        edit = await asyncio.to_thread(build_edit, self.take)
        if not edit.turns:
            await self._discard_empty(reason)
            return

        self._step("notes")
        plan = None
        if reason != "shutdown":
            try:
                plan = await asyncio.wait_for(
                    self.gemini.edit_cohost(edit.editor_turns(self.you, self.host.name), self.you,
                                            self.host.name, call_s / 60),
                    timeout=self.settings.cohost_edit_timeout_seconds,
                )
            except Exception as exc:
                log.warning("call %s: no editor's notes (%s); mechanical edits only", self.id, exc)
        plan = plan or fallback_plan(self.doc.get("title") or "Co-hosted episode")

        self._step("cutting")
        cues, music = await self._theme()
        episode = await asyncio.to_thread(assemble, edit, plan, self.you, self.host.name, self.revs, cues is None)
        if not episode.segments:
            await self._discard_empty(reason)
            return

        self._step("mastering")
        if cues is not None:
            try:
                episode, regions = await asyncio.to_thread(scored, episode, cues)
                music = {**music, "regions": regions}
            except Exception:
                log.exception("call %s: couldn't mix the theme in; chimes instead", self.id)
                cues, music = None, {"source": "none"}
                episode = await asyncio.to_thread(assemble, edit, plan, self.you, self.host.name, self.revs)
        mp3, raw = await asyncio.to_thread(self._encode, episode.pcm, cues is not None)
        audio = await self.store.put_audio(self.id, mp3)
        raw_audio = await self.store.put_audio(self.id, raw, name="raw.mp3")
        writes = asyncio.Semaphore(8)

        async def put(seg: dict) -> None:
            async with writes:
                await self.store.put_segment(self.id, seg)
        await asyncio.gather(*(put(s) for s in episode.segments))

        refund = await self._refund(call_s)
        duration = round(episode.samples / RATE, 2)
        fields = {
            "title": plan.title.strip()[:80] or self.doc.get("title", ""),
            "tagline": plan.tagline.strip()[:200],
            "about": plan.about.strip()[:1200],
            "beats": episode.beats,
            "status": "finished",
            "ended_reason": reason,
            "ended_at": time.time(),
            "samples": episode.samples,
            "duration_s": duration,
            "call_s": round(call_s, 1),
            "audio": audio,
            "raw_audio": raw_audio,
            "credits_refunded": refund,
            "edit": episode.stats,
            "music": music,
            "counts": {"segments": len(episode.segments), "comments": 0, "context": 0, "hearts": 0,
                       "peak_listeners": 0},
            "style_history": self.doc.get("style_history", []),
        }
        self.doc.update(fields)
        await self.store.update_show(self.id, fields)
        self.saved = {"url": f"/show/{self.id}", "duration": duration, "stats": episode.stats,
                      "title": fields["title"]}
        self._set_state("saved")
        self._send({"type": "saved", **self.saved})
        log.info("call %s saved (%s): %.0fs call, %.0fs episode", self.id, reason, call_s, duration)

    async def _refund(self, call_s: float) -> int:
        """Unused minutes back, once, however saving goes. Billed by the call's length
        (what the Live session costs), not the edited episode's."""
        if self._refunded is None:
            self._refunded = await refund_unused(self.store, self.settings, self.doc, call_s)
        return self._refunded

    async def _discard_empty(self, reason: str) -> None:
        """Nothing was said: there's no episode, so no pod, and every credit comes back."""
        await self._refund(0)
        await self.store.delete_show(self.id)
        self._set_state("ended")
        message = ("The co-host couldn't come on the line, so nothing was recorded." if reason == "error"
                   else "Nothing was recorded, so there's no episode. Your credits are back.")
        self._send({"type": "ended", "reason": reason, "empty": True, "message": message})

    async def _theme(self) -> tuple[Cues | None, dict]:
        """The episode's theme: the saved show intro's music, or the library's pick for
        the mode the call started in. None to be had: it opens and closes on a chime."""
        if self.music is None:
            return None, {"source": "none"}
        try:
            return await theme_for((self.doc.get("intro") or {}).get("music") or "auto", library=self.music,
                                   store=self.store, uid=self.doc["owner_uid"],
                                   mood=COHOST_MODES_BY_ID[self.request.mode]["music_mood"], seed=self.id, rate=RATE)
        except Exception:
            log.exception("call %s: no theme music", self.id)
            return None, {"source": "none"}

    def _encode(self, episode_pcm: bytes, music: bool = False) -> tuple[bytes, bytes]:
        """The episode (at the memo pods' bitrate when it has music in it) and the raw call."""
        mp3 = Mp3Writer(RATE, self.settings.render_mp3_kbps if music else self.settings.mp3_kbps)
        mp3.feed(episode_pcm)
        raw = Mp3Writer(RATE, self.settings.mp3_kbps)
        raw.feed(raw_mix(self.take))
        return mp3.finish(), raw.finish()

    async def _close_soon(self) -> None:
        """Give the phone a moment to read the result, then let the call go."""
        await asyncio.sleep(2)
        if self.conn is not None:
            self.conn.close()
            self.conn = None
        self._on_closed(self.id)

    # ---- helpers ------------------------------------------------------------------------

    def _system(self) -> str:
        return prompts.cohost_system(self.request.model_copy(update={"mode": self.mode}), self.host,
                                     self.you, self.notes)

    async def _send_text(self, text: str) -> None:
        link = self._link
        if link is not None:
            async with self._send_lock:
                await link.send_text(text)

    @staticmethod
    async def _quietly(coro) -> None:
        try:
            await coro
        except Exception:
            pass

    def _send(self, event: dict) -> None:
        if self.conn is not None:
            self.conn.send(json.dumps(event))

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self._send({"type": "state", "state": state})

    def _step(self, step: str) -> None:
        self._send({"type": "editing", "step": step})

    def _error(self, message: str) -> None:
        self._send({"type": "error", "fatal": True, "code": "cohost", "message": message})

    def _say(self, who: str, turn: int, text: str) -> None:
        """Captions: fragments of the same speaker's turn are joined."""
        last = self._said[-1] if self._said else None
        if last and last["who"] == who and last["turn"] == turn:
            last["text"] += text
        else:
            self._said.append({"who": who, "turn": turn, "text": text})
            del self._said[:-SAID_KEPT]
        self._send({"type": "said", "who": who, "turn": turn, "text": text})

    def _recap(self) -> list[tuple[str, str]]:
        names = {"you": self.you, "host": self.host.name}
        return [(names[s["who"]], s["text"].strip()) for s in self._said[-RECAP_LINES:] if s["text"].strip()]

    def elapsed_s(self) -> float:
        return round(time.monotonic() - self.started, 1) if self.started is not None else 0.0

    def ready_event(self) -> dict:
        return {
            "type": "ready",
            "id": self.id,
            "state": self.state,
            "you": self.you,
            "host": {"name": self.host.name, "voice": self.host.voice, "gender": self.host.gender,
                     "persona": self.host.persona},
            "mode": self.mode,
            "accent": COHOST_MODES_BY_ID[self.mode]["accent"],
            "title": self.doc.get("title", ""),
            "elapsed_s": self.elapsed_s(),
            "limit_s": self.limit_s,
            "wrapping": self._wrapped,
            "said": self._said,
            "saved": self.saved,
            "rate": RATE,
        }

    def public_show(self) -> dict:
        """What GET /api/shows/{id} says while the call is on (to its owner only)."""
        d = self.doc
        return {
            "id": self.id, "kind": "cohost", "status": "live" if self.ended_reason is None else d.get("status"),
            "title": d.get("title", ""), "tagline": "", "about": "", "accent": d.get("accent"),
            "vibe": None, "style_label": d.get("style_label", ""), "minutes": d.get("minutes"),
            "language": d.get("language"), "hosts": d.get("hosts", []), "sources": d.get("source", {}).get("names", []),
            "created_at": d.get("created_at"), "owner_uid": d.get("owner_uid"),
            "owner_name": d.get("owner_name", ""), "visibility": d.get("visibility", "private"),
            "shared_with": d.get("shared_with", []), "album_ids": d.get("album_ids", []),
            "tags": d.get("tags", []),
        }


class Cohosts:
    """Every call in this process (in memory, like Rooms)."""

    def __init__(self) -> None:
        self._calls: dict[str, CohostSession] = {}

    def get(self, show_id: str) -> CohostSession | None:
        return self._calls.get(show_id)

    def add(self, call: CohostSession) -> None:
        self._calls[call.id] = call

    def remove(self, show_id: str) -> None:
        self._calls.pop(show_id, None)

    def ids(self) -> set[str]:
        return set(self._calls)

    def live_count(self) -> int:
        return sum(c.ended_reason is None for c in self._calls.values())

    def active_for(self, uid: str) -> CohostSession | None:
        return next((c for c in self._calls.values()
                     if c.doc.get("owner_uid") == uid and not c.finalized.is_set()), None)

    async def shutdown(self, timeout: float) -> None:
        """On SIGTERM: end every call and save what was said (no editor's notes)."""
        live = [c for c in self._calls.values() if not c.finalized.is_set()]
        for call in live:
            call.end_now("shutdown")
        if live:
            await asyncio.wait([asyncio.create_task(c.finalized.wait()) for c in live], timeout=timeout)
        for call in list(self._calls.values()):
            if call.conn is not None:
                call.conn.close(1012)
