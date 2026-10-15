"""Audio kept by a live room: the MP3 being built, and a short window of raw PCM.

The MP3 is encoded as audio arrives, so a show is downloadable mid-show and
there is no long encode at the end. Raw PCM is only kept from shortly behind the
live edge onwards, for late joiners and reconnects; a 30-minute show would
otherwise hold 86 MB of samples in memory.
"""
from __future__ import annotations

from collections import deque

import lameenc


class Mp3Writer:
    def __init__(self, rate: int, kbps: int):
        self._enc = lameenc.Encoder()
        self._enc.set_bit_rate(kbps)
        self._enc.set_in_sample_rate(rate)
        self._enc.set_channels(1)
        self._enc.set_quality(5)
        self._parts: list[bytes] = []
        self._finished: bytes | None = None

    def feed(self, pcm: bytes) -> None:
        if self._finished is None and pcm:
            out = self._enc.encode(pcm)
            if out:
                self._parts.append(bytes(out))

    def snapshot(self) -> bytes:
        """Everything encoded so far. A valid MP3, minus the last few milliseconds."""
        return self._finished if self._finished is not None else b"".join(self._parts)

    def finish(self) -> bytes:
        if self._finished is None:
            tail = self._enc.flush()
            if tail:
                self._parts.append(bytes(tail))
            self._finished = b"".join(self._parts)
            self._parts = []
        return self._finished


class PcmWindow:
    """(start_sample, seq, pcm) chunks in timeline order, trimmed from the front."""

    def __init__(self) -> None:
        self._chunks: deque[tuple[int, int, bytes]] = deque()

    def add(self, start: int, seq: int, pcm: bytes) -> None:
        self._chunks.append((start, seq, pcm))

    @property
    def start(self) -> int | None:
        return self._chunks[0][0] if self._chunks else None

    def since(self, sample: int) -> list[tuple[int, int, bytes]]:
        """Chunks covering [sample, end), the first one cut to start exactly at sample."""
        out = []
        for start, seq, pcm in self._chunks:
            end = start + len(pcm) // 2
            if end <= sample:
                continue
            if start < sample:
                pcm = pcm[(sample - start) * 2:]
                start = sample
            out.append((start, seq, pcm))
        return out

    def trim(self, before: int) -> None:
        while self._chunks:
            start, _, pcm = self._chunks[0]
            if start + len(pcm) // 2 > before:
                break
            self._chunks.popleft()
