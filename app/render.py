"""A show voiced ahead of time: the ShowRunner with nobody listening.

A live show's playhead only moves while someone plays it (app/clock.py), so the
runner waits for listeners before voicing more. A render has no listeners. Its
playhead sits at the end of the voiced audio, so each segment is due the moment
the one before it has been voiced, and a 15-minute show takes only as long as
Gemini needs to write and speak it. The runner can't tell the difference.

RenderRoom is the RoomPort for that: it keeps what the runner publishes and the
voiced audio for the mixer (app/mixer.py), and broadcasts nothing. A headless
ShowRoom (scheduled briefings) also renders with nobody listening, but it encodes
and saves as it goes, so there would be nothing left to mix music into.
"""
from __future__ import annotations

import asyncio
from typing import Callable

from .clock import RenderClock


class RenderRoom:
    def __init__(self, rate: int, on_voiced: Callable[[float], None] | None = None):
        """on_voiced(seconds) is called after each segment, with the audio so far."""
        self.rate = rate
        self.clock = RenderClock(rate)
        self.wake = asyncio.Event()
        self.plan: dict | None = None
        self.segments: dict[int, dict] = {}
        self.chapters: dict[int, int] = {}      # beat index -> start sample
        self.errors: list[str] = []
        self.fatal: str | None = None
        self.voice = bytearray()                # 24 kHz mono s16le, every segment end to end
        self._on_voiced = on_voiced

    # ---- RoomPort -------------------------------------------------------------------

    def publish(self, event: dict) -> None:
        kind = event["type"]
        fields = {k: v for k, v in event.items() if k != "type"}
        if kind == "plan":
            self.plan = fields
        elif kind == "segment":
            self.segments[event["seq"]] = {**fields, "samples": None}
        elif kind == "segment_end":
            seg = self.segments.get(event["seq"])
            if seg is not None:
                seg["samples"] = event["samples"]
            if self._on_voiced:
                self._on_voiced(self.voiced_s)
        elif kind == "chapter":
            self.chapters[event["i"]] = event["start"]
        elif kind == "error":
            self.errors.append(event["message"])
            if event.get("fatal"):
                self.fatal = event["message"]

    def push_audio(self, seq: int, pcm: bytes) -> None:
        self.clock.add_audio(len(pcm) // 2)
        self.voice += pcm
        self.wake.set()

    def truncate(self, at: int, keep_seq: int, cut: dict | None) -> None:
        """Only a producer's interruption cuts a show, and nobody interrupts a render;
        kept so the RoomPort is whole. Drops the audio after `at` like ShowRoom does."""
        self.clock.audio_end = at
        self.clock.live = min(self.clock.live, float(at))
        del self.voice[at * 2:]
        for seq in [s for s in self.segments if s > keep_seq]:
            del self.segments[seq]
        if cut and cut["seq"] in self.segments:
            self.segments[cut["seq"]].update(lines=cut["lines"], samples=cut["samples"])
        self.chapters = {i: s for i, s in self.chapters.items() if s < at}
        self.wake.set()

    # ---- results ----------------------------------------------------------------------

    @property
    def voiced_s(self) -> float:
        return self.clock.audio_end / self.rate

    def segment_list(self) -> list[dict]:
        return [self.segments[k] for k in sorted(self.segments)]
