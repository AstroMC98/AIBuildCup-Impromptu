"""The live show loop: what the hosts say next, and when it gets voiced.

Timeline for one segment:

    write draft  ->  wait until the live playhead nears the end of the voiced
                     audio  ->  commit  ->  voice it
                     (new input while waiting throws the draft away and it is
                      rewritten: a call-in, context due now, a restyle, an end)

While segment N is being voiced, segment N+1 is already being written, so the
hosts never stop talking unless a rewrite is needed. Only ~low_water_seconds of
audio sit ahead of the live playhead (see app/clock.py), which is what lets a
call-in land in the show within roughly one segment.

Interruptions (the producer cutting in): everything in flight is stopped, the
timeline is rewound to exactly what the listener heard, a pre-voiced reaction plays
at once, and a short answer (a "detour") is written and voiced. Then the show picks
the thread back up, goes deeper, skips ahead, or follows a new plan.

The runner only generates. It never touches a socket or the database: events
and audio go to a RoomPort (app/room.py), which caches, fans out and persists
them, so a slow listener or a slow write can never stall the show.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Protocol

from .clock import LiveClock
from .config import Settings
from .limits import COMMENT_CHARS, COMMENTS_PER_SHOW, NAME_CHARS, QUEUED_COMMENTS_PER_SHOW
from .llm import Gemini
from .models import (
    Beat, ContextBrief, ContextItem, InterruptNote, Line, ListenerComment, SegmentContext, SegmentDraft, ShowPlan,
    ShowRequest,
)

log = logging.getLogger("podcasts.show")

# Starting guess for speaking time per line, by number of hosts: a solo host talks in
# long turns (~15 s), a pair trades shorter ones (~10 s). Replaced by the first real
# measurement, then refined as audio arrives.
SECONDS_PER_LINE = {1: 15.0, 2: 10.0}
CHARS_PER_SECOND = 14.0         # rough speaking rate, for elapsed-time estimates
MAX_COMMENTS_PER_SEGMENT = 2
MAX_CONTEXT_PER_SEGMENT = 2
OVERTIME_FACTOR = 1.1           # force the sign-off past 110% of the target length
EXTRA_SECONDS = 10.0            # ...plus this much for each call-in, context item or restyle it has to work in
WORDS_PER_SECOND = 2.5          # starting guess for speaking pace (real pods run 2.2-2.7); measured as audio arrives
VOCAL_TAG = re.compile(r"<[^>]*>")  # <laugh>, <short pause>: sounds, not words
HEARTS_TO_MENTION = 3           # tell the writers about reactions once there are this many...
HEARTS_EVERY_SEGMENTS = 4       # ...and no more often than this
WRITE_RETRY_DELAYS = (2.0, 5.0) # transient API failures get two more tries
# A Gemini call can hang without ever failing (one writer call in a live test took
# 111 s, leaving 90 s of silence). Give up well before the buffered audio runs out:
# writes take 4-8 s, so a retry after 15 s still lands inside the ~28 s queued.
WRITE_TIMEOUT_SECONDS = 15.0
PLAN_TIMEOUT_SECONDS = 45.0     # plans take ~11 s; listeners are waiting on Go live
SPEECH_STALL_SECONDS = 15.0     # speech that stops streaming this long is skipped
# A rewrite costs a writer call plus the wait for first audio. Both are measured as the
# show runs; these are the starting guesses, and the margin covers a slow call.
WRITE_SECONDS_GUESS = 8.0
SPEECH_LAG_GUESS = 3.0
REWRITE_MARGIN_SECONDS = 2.0

# Interruptions: how long each kind of answer is, in seconds of speech and in lines.
INTERJECTION_SECONDS = {"ask": 30, "deeper": 25, "steer": 20, "move_on": 8}
INTERJECTION_LINES = {"ask": 4, "deeper": 3, "steer": 3, "move_on": 2}
DEEPER_MINUTES = 1.5            # "go deeper" gives the current beat this much more time
VOICED_KEPT = 12                # recent segments kept, so an interruption can rewind them
ASIDE = re.compile(r"\|[^|]*\|")   # |no way| interjections, as the captions handle them

READY, STALE, STOPPED, INTERRUPTED = "ready", "stale", "stopped", "interrupted"


class RoomPort(Protocol):
    clock: LiveClock
    wake: asyncio.Event

    def publish(self, event: dict) -> None: ...
    def push_audio(self, seq: int, pcm: bytes) -> None: ...
    def truncate(self, at: int, keep_seq: int, cut: dict | None) -> None: ...


@dataclass
class Comment:
    id: int
    name: str
    text: str
    role: str                   # producer | guest
    at: int                     # live playhead (samples) when it came in
    client_id: str = ""
    status: str = "queued"      # queued -> on_deck | declined | removed | too_late
    seq: int | None = None      # the segment that answers (or declines) it
    context_id: int | None = None
    eta: int | None = None      # while queued: where on the timeline (samples) it should be heard

    def public(self) -> dict:
        return asdict(self)


@dataclass
class Interrupt:
    kind: str                   # ask | deeper | steer | move_on
    text: str = ""
    at: int = 0                 # the sample where the listener stopped hearing the show
    cut_line: str = ""          # the line they were hearing then


@dataclass
class Draft:
    segment: SegmentDraft
    offered_comments: frozenset[int]
    offered_context: frozenset[int]
    style_rev: int
    must_end: bool
    hearts: int
    beat: int = 0
    seq: int = 0
    start: int = 0
    final: bool = False
    lines: list[Line] = field(default_factory=list)
    addressed: list[int] = field(default_factory=list)
    declined: list[int] = field(default_factory=list)
    used_context: list[int] = field(default_factory=list)
    prev_style_acked: int = 0
    held: bool = False          # input arrived too late to rewrite this one
    samples: int | None = None  # voiced length, once known
    transcript_len: int = 0     # transcript length before this segment (for rewinding)
    prev_beat_lines: int = 0
    advanced: bool = False      # it finished a beat and moved the show on
    branch: str = "main"        # main | detour (part of an answer to an interruption)
    interrupt: dict | None = None   # on detour segments: {kind, text}
    bridged: bool = False       # its writer was told to pick the thread back up


def _spoken_words(lines: list[Line]) -> int:
    return sum(len(VOCAL_TAG.sub("", ln.text).split()) for ln in lines)


def _lines_heard(lines: list[Line], fraction: float) -> int:
    """How many lines of a segment the listener had started hearing, `fraction` of the
    way through its audio. Weighted by spoken length exactly as the captions are
    (static/js/live/store.js), so the server and every screen cut at the same line."""
    weights = [max(8, len(" ".join(ASIDE.sub("", VOCAL_TAG.sub("", ln.text)).split()))) for ln in lines]
    target = fraction * sum(weights)
    heard, start = 0, 0.0
    for w in weights:
        if start >= target:
            break
        heard += 1
        start += w
    return max(1, heard)


async def _collect(stream) -> bytes:
    return b"".join([chunk async for chunk in stream])


class ShowRunner:
    def __init__(
        self,
        request: ShowRequest,
        source: str,
        gemini: Gemini,
        settings: Settings,
        room: RoomPort,
    ):
        self.req = request
        self.source = source
        self.gemini = gemini
        self.settings = settings
        self.room = room
        self.rate = settings.sample_rate

        self.plan: ShowPlan | None = None
        self.transcript: list[Line] = []
        self.beat = 0
        self.beat_lines = 0
        self.seq = 0
        self.finished_writing = False
        self._chapter: int | None = None

        self.style = request.style
        self.style_rev = 0
        self.style_acked = 0

        self.comments: dict[int, Comment] = {}
        self._next_comment_id = 1
        self.context: dict[int, ContextItem] = {}
        self._next_context_id = 1
        self.hearts_since = 0
        self._hearts_seq = -HEARTS_EVERY_SEGMENTS

        self.sec_per_line = SECONDS_PER_LINE[min(2, len(request.hosts))]
        self.words_per_sec = WORDS_PER_SECOND
        self.write_s = WRITE_SECONDS_GUESS        # how long a writer call takes
        self.speech_lag_s = SPEECH_LAG_GUESS      # how long until speech starts streaming
        self._voicing_end = 0                     # estimated end of the segment streaming in now
        self._rate_measured = False
        self._unsent_chars = 0          # committed text not yet voiced
        self._stopped = False
        self.end_requested = False
        self.end_urgent = False         # end without waiting for anyone to listen
        self._pending: asyncio.Task[Draft] | None = None
        self._speaking: asyncio.Task[None] | None = None

        # interruptions
        self._interrupt: Interrupt | None = None
        self._bridge: InterruptNote | None = None
        self.interrupts_used = 0
        self._voiced: list[Draft] = []
        self._beat_starts: dict[int, int] = {}
        self.stingers: dict[str, list[tuple[Line, bytes]]] = {}
        self._stinger_task: asyncio.Task[None] | None = None

    # ---- input from the room --------------------------------------------------

    def add_comment(
        self, name: str, text: str, role: str, client_id: str = "", context_id: int | None = None
    ) -> tuple[Comment | None, str | None]:
        """Returns (comment, None) or (None, error code)."""
        name = (name or "").strip()[:NAME_CHARS] or ("Producer" if role == "producer" else "A listener")
        text = (text or "").strip()[:COMMENT_CHARS]
        if not text:
            return None, "empty"
        if client_id:
            for c in self.comments.values():
                if c.client_id == client_id:
                    return c, None  # a retry after a reconnect
        queued = sum(c.status == "queued" for c in self.comments.values())
        if len(self.comments) >= COMMENTS_PER_SHOW or queued >= QUEUED_COMMENTS_PER_SHOW:
            return None, "line_full"
        comment = Comment(
            id=self._next_comment_id, name=name, text=text, role=role,
            at=int(self.room.clock.live), client_id=client_id[:40], context_id=context_id,
        )
        self._next_comment_id += 1
        if self.finished_writing:
            comment.status = "too_late"
        else:
            comment.eta = self._eta_for(queued)
        self.comments[comment.id] = comment
        self._publish_comment(comment)
        self.room.wake.set()
        return comment, None

    def add_context(self, item: ContextItem) -> ContextItem:
        item.id = self._next_context_id
        self._next_context_id += 1
        item.beat_at_add = self.beat
        item.at = int(self.room.clock.live)
        item.status = "background" if item.when == "background" else "pending"
        self.context[item.id] = item
        self._publish_context(item)
        self.room.wake.set()
        return item

    def link_comment(self, item: ContextItem, comment: Comment) -> None:
        item.comment_id = comment.id
        self._publish_context(item)

    def restyle(self, style: str) -> int:
        self.style = style
        self.style_rev += 1
        self.room.wake.set()
        return self.style_rev

    def remove_comment(self, comment_id: int) -> None:
        c = self.comments.get(comment_id)
        if c and c.status == "queued":
            c.status = "removed"
            self._publish_comment(c)
            self._drop_attachment(c)
            self.room.wake.set()

    def note_hearts(self, n: int) -> None:
        self.hearts_since += n

    def request_end(self, urgent: bool = False) -> None:
        self.end_requested = True
        self.end_urgent = self.end_urgent or urgent
        self.room.wake.set()

    def stop(self) -> None:
        self._stopped = True
        self.room.wake.set()

    def interrupt(self, kind: str, text: str, at: int) -> str | None:
        """The producer cut in at sample `at`. Stops whatever is being written or voiced;
        the run loop rewinds and answers. Returns an error code, or None."""
        if self.plan is None or self.seq == 0:
            return "starting"
        if self.finished_writing or self.end_requested:
            return "ending"
        if self._interrupt is not None:
            return "busy"
        if self.interrupts_used >= self.settings.max_interrupts:
            return "limit"
        self.interrupts_used += 1
        self._interrupt = Interrupt(kind=kind, text=text.strip()[:COMMENT_CHARS], at=int(at))
        for task in (self._speaking, self._pending):
            if task is not None and not task.done():
                task.cancel()
        self.room.wake.set()
        return None

    # ---- the loop -------------------------------------------------------------

    async def run(self) -> str:
        """Runs the show to its end and returns why it ended."""
        try:
            self.room.publish({"type": "status", "state": "planning"})
            started = time.monotonic()
            try:
                self.plan = await asyncio.wait_for(
                    self.gemini.plan_show(self.req, self.source), PLAN_TIMEOUT_SECONDS)
            except TimeoutError:
                log.warning("planning hung for %.0fs; trying once more", PLAN_TIMEOUT_SECONDS)
                self.plan = await asyncio.wait_for(
                    self.gemini.plan_show(self.req, self.source), PLAN_TIMEOUT_SECONDS)
            log.info("planned in %.1fs", time.monotonic() - started)
            self._publish_plan()
            self.room.publish({"type": "status", "state": "writing"})

            self._pending = self._cold_open() or self._start_writing()
            self._stinger_task = asyncio.create_task(self._voice_stingers())
            rewrites = 0
            while not self._stopped:
                if self._interrupt is not None:
                    await self._handle_interrupt()
                    rewrites = 0
                    continue
                if self._pending is None:
                    self._pending = self._start_writing()
                await asyncio.wait([self._pending])
                if self._interrupt is not None:
                    continue
                if self._pending.cancelled():
                    self._pending = None
                    continue
                draft = self._pending.result()
                self._pending = None
                verdict = await self._wait_for_slot(draft)
                if verdict == STOPPED:
                    break
                if verdict == INTERRUPTED:
                    continue
                if verdict == STALE and rewrites < self.settings.max_rewrites:
                    rewrites += 1
                    log.info("new input arrived; rewriting segment %d", self.seq + 1)
                    self._pending = self._start_writing()
                    continue
                rewrites = 0

                is_final = self._commit(draft)
                self.finished_writing = is_final
                self._announce(draft)
                self._refresh_etas()
                if not is_final:
                    self._pending = self._start_writing()  # write N+1 while N is voiced
                self._speaking = asyncio.create_task(self._speak(draft))
                await asyncio.wait([self._speaking])
                self._speaking = None
                if self._interrupt is not None:
                    continue
                if is_final:
                    break

            if self._stopped:
                return "stopped"
            for c in self.comments.values():
                if c.status == "queued":
                    c.status = "too_late"
                    self._publish_comment(c)
            return "signed_off"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # surface everything else to the listeners
            log.exception("show failed")
            self.room.publish({"type": "error", "fatal": True, "message": str(exc)})
            return "error"
        finally:
            for task in (self._pending, self._speaking, self._stinger_task):
                if task is not None and not task.done():
                    task.cancel()

    # ---- writing ----------------------------------------------------------------

    def _queued_comments(self) -> list[Comment]:
        queued = [c for c in self.comments.values() if c.status == "queued"]
        return sorted(queued, key=lambda c: c.id)[:MAX_COMMENTS_PER_SEGMENT]

    def _due_context(self) -> tuple[list[ContextItem], list[ContextItem]]:
        """(bring in now, work in this chapter). Items attached to a call-in ride
        along with that comment instead. "Next chapter" items wait for the beat to
        move on, and become urgent if a whole chapter passes without them."""
        assert self.plan is not None
        last = len(self.plan.beats) - 1
        now: list[ContextItem] = []
        chapter: list[ContextItem] = []
        for x in sorted(self.context.values(), key=lambda x: x.id):
            if x.status != "pending" or x.comment_id is not None:
                continue
            if x.when == "now" or self.beat > x.beat_at_add + 1:
                now.append(x)
            elif self.beat > x.beat_at_add or self.beat == last:
                chapter.append(x)
        return now[:MAX_CONTEXT_PER_SEGMENT], chapter[:MAX_CONTEXT_PER_SEGMENT]

    def _brief(self, x: ContextItem) -> ContextBrief:
        return ContextBrief(
            id=x.id, name=x.name, kind=x.kind, added_by=x.added_by,
            from_creator=x.from_creator, cite=x.cite,
        )

    def _elapsed_minutes(self) -> float:
        voiced = self.room.clock.audio_end / self.rate
        return (voiced + self._unsent_chars / CHARS_PER_SECOND) / 60

    def _lines_target(self) -> int:
        assert self.plan is not None
        beat = self.plan.beats[min(self.beat, len(self.plan.beats) - 1)]
        return max(3, round(beat.minutes * 60 / self.sec_per_line))

    def _segment_lines(self) -> int:
        """Long-winded hosts (a solo deep dive runs ~15 s a line) get fewer lines per
        segment, so a call-in or restyle never waits behind a minute-long segment."""
        by_time = round(self.settings.segment_seconds / self.sec_per_line)
        return max(2, min(self.settings.lines_per_segment, by_time))

    def _context(self) -> tuple[SegmentContext, frozenset[int], frozenset[int], int]:
        assert self.plan is not None
        total_beats = len(self.plan.beats)
        lines_target = self._lines_target()
        elapsed = self._elapsed_minutes()
        on_last_beat = self.beat >= total_beats - 1
        segment_lines = self._segment_lines()
        # The next segment will take about this long; if it would carry the last beat
        # past the target, make it the sign-off.
        next_segment_minutes = segment_lines * self.sec_per_line / 60
        must_end = (
            self.end_requested
            or elapsed >= self.req.minutes * OVERTIME_FACTOR
            or (on_last_beat and self.beat_lines >= lines_target)
            or (on_last_beat and self.beat_lines and elapsed + next_segment_minutes >= self.req.minutes)
        )
        beats_left = self.plan.beats[min(self.beat, total_beats - 1) + 1:]
        comments = self._queued_comments()
        now, chapter = self._due_context()
        style_change = self.style if self.style_rev > self.style_acked else None
        hearts = (
            self.hearts_since
            if self.hearts_since >= HEARTS_TO_MENTION and self.seq - self._hearts_seq >= HEARTS_EVERY_SEGMENTS
            else 0
        )
        listed = []
        offered_context = {x.id for x in now + chapter}
        for c in comments:
            attachment = self.context.get(c.context_id) if c.context_id else None
            if attachment:
                offered_context.add(attachment.id)
            listed.append(ListenerComment(
                id=c.id, name=c.name, text=c.text,
                attachment=self._brief(attachment) if attachment else None,
            ))
        extra = (2 if comments else 0) + (1 if now else 0) + (1 if style_change else 0)
        ctx = SegmentContext(
            transcript=list(self.transcript),
            beat_index=min(self.beat, total_beats - 1),
            beat_lines_written=self.beat_lines,
            beat_lines_target=lines_target,
            elapsed_minutes=elapsed,
            target_minutes=self.req.minutes,
            comments=listed,
            must_end=must_end,
            # give busy segments a little extra room so the plan still moves
            lines_wanted=segment_lines + min(4, extra),
            # Length is set in words: a solo host's lines run 30 to 70 words, so a line
            # count alone let segments drift past two minutes.
            words_wanted=round((self.settings.segment_seconds + EXTRA_SECONDS * min(4, extra)) * self.words_per_sec),
            style_change=style_change,
            context_now=[self._brief(x) for x in now],
            context_chapter=[self._brief(x) for x in chapter],
            hearts=hearts,
            beats_left=len(beats_left),
            planned_minutes_left=sum(b.minutes for b in beats_left),
            bridge=self._bridge,
        )
        return ctx, frozenset(c.id for c in comments), frozenset(offered_context), hearts

    def _cold_open(self) -> asyncio.Task[Draft] | None:
        """The planner writes the first lines along with the plan, so they're voiced
        without waiting on a writer call: audio starts several seconds sooner, and
        segment 2 is written while the cold open plays."""
        assert self.plan is not None
        if not self.plan.cold_open:
            return None
        draft = Draft(
            segment=SegmentDraft(lines=list(self.plan.cold_open)),
            offered_comments=frozenset(), offered_context=frozenset(),
            style_rev=0,  # written in the original style; a restyle since is still due
            must_end=False, hearts=0,
        )
        return asyncio.create_task(asyncio.sleep(0, result=draft))

    def _start_writing(self) -> asyncio.Task[Draft]:
        # Snapshot everything now, before anything else changes.
        ctx, offered_comments, offered_context, hearts = self._context()
        context = [x.model_copy() for x in sorted(self.context.values(), key=lambda x: x.id)
                   if x.status != "dropped"]
        return asyncio.create_task(self._write(
            ctx, self.style, self.style_rev, context, offered_comments, offered_context, hearts,
            bridged=ctx.bridge is not None,
        ))

    async def _write(
        self,
        ctx: SegmentContext,
        style: str,
        style_rev: int,
        context: list[ContextItem],
        offered_comments: frozenset[int],
        offered_context: frozenset[int],
        hearts: int,
        thinking: str | None = None,
        bridged: bool = False,
    ) -> Draft:
        assert self.plan is not None
        for attempt, delay in enumerate((*WRITE_RETRY_DELAYS, None)):
            try:
                started = time.monotonic()
                segment = await asyncio.wait_for(
                    self.gemini.write_segment(ctx, self.req, self.plan, self.source, style, context, thinking=thinking),
                    WRITE_TIMEOUT_SECONDS,
                )
                took = time.monotonic() - started
                self.write_s = 0.5 * self.write_s + 0.5 * took
                log.info("segment %d written in %.1fs", self.seq + 1, took)
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if delay is None:
                    raise
                if isinstance(exc, TimeoutError):
                    # A hung call, not an overloaded API: go again straight away.
                    log.warning("writing hung for %.0fs (attempt %d); retrying now", WRITE_TIMEOUT_SECONDS, attempt + 1)
                    continue
                log.warning("writing failed (attempt %d): %s; retrying in %.0fs", attempt + 1, exc, delay)
                await asyncio.sleep(delay)
        return Draft(
            segment=segment,
            offered_comments=offered_comments,
            offered_context=offered_context,
            style_rev=style_rev,
            must_end=ctx.must_end,
            hearts=hearts,
            bridged=bridged,
        )

    def _is_stale(self, draft: Draft) -> bool:
        """A draft is stale if input is waiting that it never saw."""
        if any(c.id not in draft.offered_comments for c in self._queued_comments()):
            return True
        now, _ = self._due_context()
        if any(x.id not in draft.offered_context for x in now):
            return True
        if draft.style_rev != self.style_rev:
            return True
        return self.end_requested and not draft.must_end

    async def _wait_for_slot(self, draft: Draft) -> str:
        """Block until the live playhead needs more audio, or the draft goes stale."""
        while not self._stopped:
            self.room.wake.clear()
            if self._interrupt is not None:
                return INTERRUPTED
            if self._is_stale(draft) and self._rewrite_lands_in_time(draft):
                return STALE
            if self.end_urgent:
                return READY
            if self.room.clock.ahead_s() <= self.settings.low_water_seconds:
                return READY
            try:
                await asyncio.wait_for(self.room.wake.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass
        return STOPPED

    def _rewrite_lands_in_time(self, draft: Draft) -> bool:
        """When listeners would run out of audio before a rewrite could be heard, voice
        this draft as it is: the new input stays queued for the next segment, which is
        better than dead air. With nobody playing there's no rush."""
        clock = self.room.clock
        if self.end_urgent or not clock.listening or clock.ahead_s() > self._rewrite_needs_s():
            return True
        if not draft.held:
            draft.held = True
            log.info("new input with %.1fs of audio left; it goes in the next segment", clock.ahead_s())
        return False

    def _rewrite_needs_s(self) -> float:
        """Seconds of audio a rewrite needs in front of the playhead to be heard in time."""
        return (self.write_s + self.speech_lag_s + REWRITE_MARGIN_SECONDS) * self.settings.clock_speed

    # ---- call-in ETAs -----------------------------------------------------------------

    def _eta_for(self, place: int) -> int | None:
        """Where on the show timeline (samples) the call-in at `place` in the queue
        should be heard, so each listener can count down from their own playhead. It
        lands in the next unvoiced segment if a rewrite still fits, else the one after;
        each segment takes MAX_COMMENTS_PER_SEGMENT call-ins, so later ones wait more."""
        if self.plan is None or self.finished_writing:
            return None
        clock = self.room.clock
        start = max(clock.audio_end, self._voicing_end)
        ahead = clock.ahead_s() + (start - clock.audio_end) / self.rate
        fits = not clock.listening or ahead > self._rewrite_needs_s()
        busy = place // MAX_COMMENTS_PER_SEGMENT  # segments full of earlier call-ins
        skip = 0 if fits else 1
        wait = (busy + skip) * self.settings.segment_seconds + busy * 2 * EXTRA_SECONDS
        return int(start + wait * self.rate)  # the start of its segment, when "On air now" shows

    def _refresh_etas(self) -> None:
        """Re-estimate waiting call-ins whenever the timeline moves on, and tell
        listeners about any that shifted by more than a second."""
        queued = sorted((c for c in self.comments.values() if c.status == "queued"), key=lambda c: c.id)
        for place, c in enumerate(queued):
            eta = self._eta_for(place)
            if eta != c.eta and (eta is None or c.eta is None or abs(eta - c.eta) > self.rate):
                c.eta = eta
                self._publish_comment(c)

    # ---- committing and voicing -------------------------------------------------------

    def _commit(self, draft: Draft) -> bool:
        assert self.plan is not None
        self.seq += 1
        draft.seq = self.seq
        draft.beat = min(self.beat, len(self.plan.beats) - 1)
        draft.start = self.room.clock.audio_end  # exact: voicing is strictly sequential
        draft.lines = draft.segment.lines
        draft.transcript_len = len(self.transcript)
        draft.prev_beat_lines = self.beat_lines
        self.transcript.extend(draft.lines)
        self.beat_lines += len(draft.lines)
        self._voiced = [*self._voiced, draft][-VOICED_KEPT:]
        if draft.bridged:
            self._bridge = None
        self._unsent_chars = sum(len(ln.text) for ln in draft.lines)
        # Until its audio is in, guess where this segment ends from its words.
        self._voicing_end = draft.start + int(_spoken_words(draft.lines) / self.words_per_sec * self.rate)

        if draft.beat != self._chapter:
            self._chapter = draft.beat
            self._beat_starts[draft.beat] = draft.start
            self.room.publish({"type": "chapter", "i": draft.beat, "start": draft.start})

        seg = draft.segment
        for cid in seg.addressed_comment_ids:
            c = self.comments.get(cid)
            if c and c.status == "queued" and cid in draft.offered_comments:
                c.status, c.seq = "on_deck", draft.seq
                draft.addressed.append(cid)
                self._publish_comment(c)
                attached = self.context.get(c.context_id) if c.context_id else None
                if attached and attached.status == "pending":
                    self._mark_used(attached, draft)
        for cid in seg.declined_comment_ids:
            c = self.comments.get(cid)
            if c and c.status == "queued" and cid in draft.offered_comments:
                c.status, c.seq = "declined", draft.seq
                draft.declined.append(cid)
                self._publish_comment(c)
                self._drop_attachment(c)
        for xid in seg.used_context_ids:
            x = self.context.get(xid)
            if x and x.status == "pending" and xid in draft.offered_context:
                self._mark_used(x, draft)

        draft.prev_style_acked = self.style_acked
        self.style_acked = max(self.style_acked, draft.style_rev)
        if draft.hearts:
            self.hearts_since = max(0, self.hearts_since - draft.hearts)
            self._hearts_seq = draft.seq

        on_last_beat = self.beat >= len(self.plan.beats) - 1
        beat_complete = seg.beat_complete and not self._wrapping_early()
        is_final = draft.must_end or (on_last_beat and beat_complete)
        if beat_complete and not on_last_beat:
            self.beat += 1
            self.beat_lines = 0
            draft.advanced = True
        draft.final = is_final
        return is_final

    def _beat_minutes_left(self) -> float:
        """How much of the current beat's planned time is still unspent (negative when
        the show is running behind the plan)."""
        assert self.plan is not None
        planned_end = sum(b.minutes for b in self.plan.beats[: self.beat + 1])
        return planned_end - self._elapsed_minutes()

    def _wrapping_early(self) -> bool:
        """The writers like to wrap a chapter after a couple of exchanges, which left
        pods 10-15% short. Hold a beat open while more than half a segment of its planned
        time is left: one more segment then lands closer to the plan than wrapping now."""
        if self.end_requested:
            return False
        early = self._beat_minutes_left() * 60 > self.settings.segment_seconds / 2
        if early:
            log.info("beat %d marked complete after %d lines; holding it open", self.beat, self.beat_lines)
        return early

    def _mark_used(self, x: ContextItem, draft: Draft) -> None:
        x.status, x.used_seq = "used", draft.seq
        draft.used_context.append(x.id)
        self._publish_context(x)

    def _drop_attachment(self, c: Comment) -> None:
        x = self.context.get(c.context_id) if c.context_id else None
        if x and x.status == "pending":
            x.status = "dropped"
            self._publish_context(x)

    def _announce(self, draft: Draft) -> None:
        self.room.publish({
            "type": "segment",
            "seq": draft.seq,
            "beat": draft.beat,
            "start": draft.start,
            "final": draft.final,
            "style_rev": draft.style_rev,
            "lines": [ln.model_dump() for ln in draft.lines],
            "addressed": draft.addressed,
            "declined": draft.declined,
            "used_context": draft.used_context,
            "branch": draft.branch,
            "interrupt": draft.interrupt,
        })

    async def _speak(self, draft: Draft) -> None:
        assert self.plan is not None
        carry = b""
        samples = 0
        cancelled = False
        started = time.monotonic()
        stream = self.gemini.stream_speech(draft.lines, self.plan.hosts)
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(anext(stream), SPEECH_STALL_SECONDS)
                except StopAsyncIteration:
                    break
                if self._stopped:
                    return
                if not samples and not carry:
                    lag = time.monotonic() - started
                    self.speech_lag_s = 0.5 * self.speech_lag_s + 0.5 * lag
                    log.info("segment %d speech started after %.1fs", draft.seq, lag)
                data = carry + chunk
                cut = len(data) - (len(data) % 2)  # keep 16-bit samples whole
                carry = data[cut:]
                if cut:
                    self.room.push_audio(draft.seq, data[:cut])
                    samples += cut // 2
        except asyncio.CancelledError:
            cancelled = True  # an interruption: what was voiced stays; the room rewinds it
            raise
        except Exception as exc:
            reason = f"no audio for {SPEECH_STALL_SECONDS:.0f}s" if isinstance(exc, TimeoutError) else str(exc)
            log.exception("speech failed for segment %d: %s", draft.seq, reason)
            self.room.publish({
                "type": "error", "fatal": False,
                "message": f"Segment {draft.seq} couldn't be voiced ({reason}). Skipping ahead.",
            })
            self._revert(draft)
        finally:
            draft.samples = samples
            self._unsent_chars = 0
            self._voicing_end = 0  # the segment's real end is in the clock now
            self.room.publish({"type": "segment_end", "seq": draft.seq, "samples": samples})
            if samples and draft.lines and not cancelled:
                seconds = samples / self.rate
                words = _spoken_words(draft.lines)
                keep = 0.7 if self._rate_measured else 0.0  # the first measurement beats any guess
                self.sec_per_line = min(25.0, max(2.0, keep * self.sec_per_line + (1 - keep) * seconds / len(draft.lines)))
                self.words_per_sec = min(4.0, max(1.5, keep * self.words_per_sec + (1 - keep) * words / seconds))
                self._rate_measured = True
            self._refresh_etas()

    # ---- interruptions ---------------------------------------------------------------

    async def _handle_interrupt(self) -> None:
        """Stop, rewind to what the listener heard, react, answer, and carry on."""
        it = self._interrupt
        assert it is not None and self.plan is not None
        await self._settle()
        if self._stopped:
            return
        self._truncate(it)
        self._play_stinger(it)
        replan = asyncio.create_task(self._replan(it)) if it.kind == "steer" else None
        draft: Draft | None = None
        try:
            draft = await self._start_interjection(it)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("couldn't answer the interruption; carrying on")
        new_beats: list[Beat] | None = None
        if replan is not None:
            try:
                new_beats = await replan
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("couldn't re-plan after a steer; moving on instead")
        if self._stopped:
            return
        if draft is not None:
            draft.branch = "detour"
            draft.interrupt = {"kind": it.kind, "text": it.text}
            draft.segment.beat_complete = False
            self._commit(draft)
            self._announce(draft)
        self._apply_interrupt(it, new_beats)
        self._interrupt = None
        self._refresh_etas()
        self._pending = self._start_writing()   # the show carries on while the answer plays
        if draft is not None:
            self._speaking = asyncio.create_task(self._speak(draft))
            await asyncio.wait([self._speaking])
            self._speaking = None

    async def _settle(self) -> None:
        """Wait for the cancelled writer and voice to actually stop."""
        tasks = [t for t in (self._speaking, self._pending) if t is not None]
        for t in tasks:
            if not t.done():
                t.cancel()
        if tasks:
            await asyncio.wait(tasks)
        for t in tasks:
            if not t.cancelled():
                t.exception()  # a draft nobody will use; don't warn about it
        self._speaking = self._pending = None

    def _truncate(self, it: Interrupt) -> None:
        """Rewind to the moment the listener cut in: segments after it were voiced but
        never heard, so their lines, call-ins, context and style changes go back to
        waiting, and the segment they were in stops where they stopped hearing it."""
        clock = self.room.clock
        clock.tick()
        at = max(0, min(int(it.at), clock.audio_end))
        it.at = at
        gone = [d for d in self._voiced if d.start >= at]
        kept = [d for d in self._voiced if d.start < at]
        if gone:
            first = min(gone, key=lambda d: d.seq)
            for d in sorted(gone, key=lambda d: d.seq, reverse=True):
                self._revert(d)
            self.beat, self.beat_lines = first.beat, first.prev_beat_lines
            del self.transcript[first.transcript_len:]
            self.seq = first.seq - 1
            self._beat_starts = {b: s for b, s in self._beat_starts.items() if s < at}
        cut: dict | None = None
        last = kept[-1] if kept else None
        if last is not None:
            length = last.samples if last.samples else max(1, at - last.start)
            if at < last.start + length:  # they were in the middle of it
                heard = _lines_heard(last.lines, (at - last.start) / length)
                if heard < len(last.lines):
                    del self.transcript[last.transcript_len + heard:]
                    last.lines = last.lines[:heard]
                spoken = " ".join(ln.text for ln in last.lines)
                for c in self.comments.values():  # a call-in its unheard lines would have read out
                    if c.seq == last.seq and c.status in {"on_deck", "declined"} and c.name not in spoken:
                        c.status, c.seq = "queued", None
                        self._publish_comment(c)
                self.beat, self.beat_lines = last.beat, last.prev_beat_lines + len(last.lines)
                last.advanced = False
                last.samples = at - last.start
                cut = {"seq": last.seq, "lines": [ln.model_dump() for ln in last.lines], "samples": last.samples}
            it.cut_line = last.lines[-1].text if last.lines else ""
        self._voiced = kept
        self._chapter = last.beat if last is not None else None
        self._unsent_chars = 0
        self._voicing_end = 0
        self.room.truncate(at, keep_seq=self.seq, cut=cut)
        log.info("interrupted (%s) at %.1fs; %d unheard segments dropped", it.kind, at / self.rate, len(gone))

    def _play_stinger(self, it: Interrupt) -> None:
        """A pre-voiced reaction ("Ooh, good question.") at once, while the answer is written."""
        options = self.stingers.get(it.kind)
        if not options or self.plan is None:
            return
        line, pcm = random.choice(options)
        d = Draft(
            segment=SegmentDraft(lines=[line]), offered_comments=frozenset(), offered_context=frozenset(),
            style_rev=self.style_acked, must_end=False, hearts=0, branch="detour",
            interrupt={"kind": it.kind, "text": it.text},
        )
        self.seq += 1
        d.seq, d.beat, d.start, d.lines = self.seq, min(self.beat, len(self.plan.beats) - 1), self.room.clock.audio_end, [line]
        d.transcript_len, d.prev_beat_lines = len(self.transcript), self.beat_lines
        self.transcript.append(line)
        self._voiced = [*self._voiced, d][-VOICED_KEPT:]
        self._announce(d)
        self.room.push_audio(d.seq, pcm)
        d.samples = len(pcm) // 2
        self.room.publish({"type": "segment_end", "seq": d.seq, "samples": d.samples})

    def _start_interjection(self, it: Interrupt) -> asyncio.Task[Draft]:
        """A short answer, written with the urgency (and the smaller budget) of a reply."""
        ctx, _, _, _ = self._context()
        ctx = ctx.model_copy(update={
            "comments": [], "context_now": [], "context_chapter": [], "style_change": None, "hearts": 0,
            "must_end": False, "bridge": None,
            "interjection": InterruptNote(kind=it.kind, text=it.text, cut_line=it.cut_line),
            "lines_wanted": INTERJECTION_LINES[it.kind],
            "words_wanted": round(INTERJECTION_SECONDS[it.kind] * self.words_per_sec),
        })
        context = [x.model_copy() for x in sorted(self.context.values(), key=lambda x: x.id) if x.status != "dropped"]
        return asyncio.create_task(self._write(
            ctx, self.style, self.style_rev, context, frozenset(), frozenset(), 0,
            thinking=self.settings.interjection_thinking_level,
        ))

    async def _replan(self, it: Interrupt) -> list[Beat]:
        assert self.plan is not None
        minutes_left = max(1.0, self.req.minutes - self._elapsed_minutes())
        return await asyncio.wait_for(self.gemini.replan(
            self.req, self.plan, self.source, self.style, list(self.transcript), minutes_left, it.text,
            min(self.beat, len(self.plan.beats) - 1),
        ), PLAN_TIMEOUT_SECONDS)

    def _apply_interrupt(self, it: Interrupt, new_beats: list[Beat] | None) -> None:
        """What the interruption does to the plan: more time here, skip ahead, or new beats.
        The episode's total length stays the same: time moves between beats."""
        changed = False
        if it.kind == "deeper":
            self._extend_current_beat(DEEPER_MINUTES)
            changed = True
        elif it.kind == "move_on":
            changed = self._end_current_beat()
        elif it.kind == "steer":
            if new_beats:
                self._steer_to(new_beats)
                changed = True
            else:
                changed = self._end_current_beat()
        self._bridge = InterruptNote(kind=it.kind, text=it.text)
        if changed:
            self._publish_plan()

    def _extend_current_beat(self, minutes: float) -> None:
        assert self.plan is not None
        beats = self.plan.beats
        i = min(self.beat, len(beats) - 1)
        later = beats[i + 1:]
        total = sum(b.minutes for b in later)
        beats[i].minutes += minutes
        if later and total > 0:
            take = min(minutes, max(0.0, total - 0.3 * len(later)))
            for b in later:
                b.minutes = max(0.3, b.minutes - take * b.minutes / total)

    def _end_current_beat(self) -> bool:
        """Move on now: the current beat's unused time goes to the beats after it."""
        assert self.plan is not None
        beats = self.plan.beats
        i = min(self.beat, len(beats) - 1)
        if i >= len(beats) - 1:
            return False  # nothing to move on to: the writers wrap up as usual
        spare = max(0.0, self._beat_minutes_left())
        beats[i].minutes = max(0.1, beats[i].minutes - spare)
        later = beats[i + 1:]
        total = sum(b.minutes for b in later) or float(len(later))
        for b in later:
            b.minutes += spare * (b.minutes or 1.0) / total
        self.beat, self.beat_lines = i + 1, 0
        return True

    def _steer_to(self, new_beats: list[Beat]) -> None:
        """The rest of the episode follows the new direction, in the time that's left."""
        assert self.plan is not None
        beats = self.plan.beats
        i = min(self.beat, len(beats) - 1)
        beats[i].minutes = max(0.1, beats[i].minutes - max(0.0, self._beat_minutes_left()))
        left = max(1.0, self.req.minutes - self._elapsed_minutes())
        total = sum(max(0.1, b.minutes) for b in new_beats)
        for b in new_beats:
            b.minutes = max(0.1, b.minutes) * left / total
        self.plan.beats = beats[: i + 1] + new_beats
        self.beat, self.beat_lines = i + 1, 0

    def _publish_plan(self) -> None:
        assert self.plan is not None
        self.room.publish({"type": "plan", **self.plan.model_dump(exclude={"cold_open", "reactions"})})

    async def _voice_stingers(self) -> None:
        """Voice the plan's reactions once the cold open is on its way, so one can play
        the instant the producer interrupts. A few seconds of TTS per show."""
        await asyncio.sleep(self.settings.stinger_delay_seconds)
        assert self.plan is not None
        for r in self.plan.reactions:
            if self._stopped:
                return
            line = Line(speaker=r.speaker, text=r.text)
            try:
                pcm = await asyncio.wait_for(_collect(self.gemini.stream_speech([line], self.plan.hosts)),
                                             SPEECH_STALL_SECONDS * 2)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("couldn't voice a reaction: %s", exc)
                continue
            pcm = pcm[: len(pcm) - len(pcm) % 2]
            if pcm:
                self.stingers.setdefault(r.kind, []).append((line, pcm))

    def _revert(self, draft: Draft) -> None:
        """Nobody heard this segment: give its call-ins, context and restyle another chance."""
        for c in self.comments.values():
            if c.seq == draft.seq and c.status in {"on_deck", "declined"}:
                c.status, c.seq = "queued", None
                self._publish_comment(c)
        for x in self.context.values():
            if x.used_seq == draft.seq:
                x.status, x.used_seq = "pending", None
                self._publish_context(x)
        self.style_acked = draft.prev_style_acked
        self.hearts_since += draft.hearts

    # ---- events -------------------------------------------------------------------

    def _publish_comment(self, c: Comment) -> None:
        self.room.publish({"type": "comment", **c.public()})

    def _publish_context(self, x: ContextItem) -> None:
        self.room.publish({"type": "context", **x.public()})
