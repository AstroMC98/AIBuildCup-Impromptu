"""The live playhead of a show, kept by the server.

Positions are in samples on the show timeline: all voiced audio laid end to end,
exactly as it ends up in the saved MP3. Holds add no silence.

The playhead moves in real time while at least one listener is playing, and
never passes the end of the voiced audio. Everyone paused, or nobody there, and
the show holds, so nothing gets voiced (or billed) that nobody is hearing.
Each listener pauses and rewinds on their own device; this clock only paces
when the next segment gets voiced.
"""
from __future__ import annotations

import time
from typing import Callable


class LiveClock:
    def __init__(self, rate: int, speed: float = 1.0, now: Callable[[], float] = time.monotonic):
        self.rate = rate
        self.speed = speed
        self._now = now
        self._t = now()
        self.live = 0.0          # samples
        self.audio_end = 0       # samples voiced so far
        self.listening = 0       # listeners currently playing

    def tick(self) -> float:
        t = self._now()
        dt = (t - self._t) * self.speed
        self._t = t
        if self.listening > 0 and self.live < self.audio_end:
            self.live = min(float(self.audio_end), self.live + dt * self.rate)
        return self.live

    # Always tick before a change, so time already passed is charged to the old state.
    def set_listening(self, n: int) -> None:
        self.tick()
        self.listening = n

    def add_audio(self, samples: int) -> None:
        self.tick()
        self.audio_end += samples

    def ahead_s(self) -> float:
        """Seconds of voiced audio still in front of the playhead."""
        self.tick()
        return (self.audio_end - self.live) / self.rate

    @property
    def playing(self) -> bool:
        return self.listening > 0 and self.live < self.audio_end


class RenderClock(LiveClock):
    """For a pod made with nobody listening (a scheduled briefing): the playhead is
    always at the end of the voiced audio, so each segment is voiced the moment it's
    written and the whole show is made as fast as the models allow."""

    def tick(self) -> float:
        self._t = self._now()
        self.live = float(self.audio_end)
        return self.live
