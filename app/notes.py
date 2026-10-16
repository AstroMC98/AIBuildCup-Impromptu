"""Notes too long to hand a live co-host whole: a brief it keeps in mind, and the
full text it searches with the check_notes tool.

A Gemini Live session bills its whole context, system instruction included, on every
turn, and holds about 131k tokens, so long notes don't go in whole. The co-host gets
a brief (llm.brief_notes) and a check_notes tool instead. The Live model waits for a
tool's answer before it speaks, so the search runs here, in memory, in a few
milliseconds: BM25 over passages of the notes, with no network call.

The brief takes several seconds to write, so the setup screen asks for it as soon as
the notes are in (POST /api/cohost/notes) and starting the call picks up the same job.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import time
from collections import Counter
from typing import Awaitable, Callable

log = logging.getLogger("podcasts.notes")

PASSAGE_CHARS = 1_200
RESULTS = 3                # passages a lookup hands back, at most
_WORD = re.compile(r"\w+")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _terms(text: str) -> list[str]:
    """Lower-cased words, with a plural's s dropped so "starters" finds "starter"."""
    out = []
    for w in _WORD.findall(text.lower()):
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        if len(w) > 1 or w.isdigit():
            out.append(w)
    return out


def passages(text: str, size: int = PASSAGE_CHARS) -> list[str]:
    """The notes in passages of up to `size` characters, broken between paragraphs
    where possible, then between sentences, and mid-sentence only as a last resort."""
    units: list[tuple[str, str]] = []      # (text, what joins it to the unit before)
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        sep = "\n\n"
        for sentence in (_SENTENCE_END.split(para) if len(para) > size else [para]):
            for i in range(0, len(sentence), size):
                units.append((sentence[i:i + size], sep))
                sep = ""          # the rest of an over-long sentence carries straight on
            sep = " "
    out: list[str] = []
    for unit, sep in units:
        if out and len(out[-1]) + len(sep) + len(unit) <= size:
            out[-1] += sep + unit
        else:
            out.append(unit)
    return out


class NotesIndex:
    """BM25 over passages of the notes."""
    K1, B = 1.5, 0.75

    def __init__(self, text: str):
        self.passages = passages(text)
        self._tf = [Counter(_terms(p)) for p in self.passages]
        self._len = [sum(tf.values()) for tf in self._tf]
        self._avg = sum(self._len) / len(self._len) if self._len else 1.0
        n = len(self.passages)
        df = Counter(t for tf in self._tf for t in tf)
        self._idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    def search(self, query: str, k: int = RESULTS) -> list[str]:
        """The k passages that best match, in the order they come in the notes."""
        wanted = set(_terms(query))
        scored = []
        for i, tf in enumerate(self._tf):
            norm = self.K1 * (1 - self.B + self.B * self._len[i] / self._avg)
            score = sum(self._idf[t] * tf[t] * (self.K1 + 1) / (tf[t] + norm) for t in wanted if t in tf)
            if score > 0:
                scored.append((score, i))
        best = sorted(i for _, i in sorted(scored, reverse=True)[:k])
        return [self.passages[i] for i in best]


def digest(notes: str) -> str:
    # surrogatepass: notes cut to length in the browser can end on half an emoji
    return hashlib.sha256(notes.encode("utf-8", "surrogatepass")).hexdigest()


class Briefs:
    """Briefs being written: one job per person, for the notes they last sent."""
    KEEP_SECONDS = 3600

    def __init__(self) -> None:
        self._jobs: dict[str, tuple[str, asyncio.Task, float]] = {}   # uid -> (digest, job, started)

    def has(self, uid: str, notes: str) -> bool:
        held = self._jobs.get(uid)
        return held is not None and held[0] == digest(notes) and _usable(held[1])

    def job(self, uid: str, notes: str, make: Callable[[], Awaitable[str]]) -> asyncio.Task:
        """The brief for these notes: the one already being written, or a new one.
        Newer notes replace older ones; a brief nobody will use is stopped."""
        now = time.monotonic()
        for key, (_, task, at) in list(self._jobs.items()):
            if task.done() and now - at > self.KEEP_SECONDS:
                del self._jobs[key]
        key = digest(notes)
        held = self._jobs.get(uid)
        if held and held[0] == key and _usable(held[1]):
            return held[1]
        if held and not held[1].done():
            held[1].cancel()
        task = asyncio.ensure_future(make())
        task.add_done_callback(_note_failure)
        self._jobs[uid] = (key, task, now)
        return task

    async def get(self, uid: str, notes: str, make: Callable[[], Awaitable[str]], timeout: float) -> str | None:
        """The brief, or None if it can't be had within `timeout` seconds."""
        task = self.job(uid, notes, make)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout)
        except asyncio.TimeoutError:
            log.warning("a brief took longer than %.0fs; going ahead without it", timeout)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise                      # we were cancelled, not the brief
        except Exception:
            pass                           # logged by _note_failure
        return None


def _usable(task: asyncio.Task) -> bool:
    return not task.done() or (not task.cancelled() and task.exception() is None)


def _note_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.warning("couldn't write a brief of the notes: %s", task.exception())
