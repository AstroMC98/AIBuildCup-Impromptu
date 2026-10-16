"""Rate limits and size caps.

Share links let anyone call in, so every input a guest can send is bounded: how
often, how big, and how many per show. The producer gets looser limits. The show
loop itself caps cost too: at most 1 + MAX_REWRITES writer calls per segment,
however many comments pour in.
"""
from __future__ import annotations

import time
from typing import Callable

COMMENT_CHARS = 500
NAME_CHARS = 40
QUEUED_COMMENTS_PER_SHOW = 25
COMMENTS_PER_SHOW = 300
CONTEXT_ITEMS_PER_SHOW = 20
CONTEXT_CHARS_PER_SHOW = 300_000
WS_MESSAGE_BYTES = 16 * 1024
OUTBOUND_QUEUE_BYTES = 8 * 1024 * 1024


class TokenBucket:
    def __init__(self, rate: float, burst: float, now: Callable[[], float] = time.monotonic):
        self.rate = rate          # tokens per second
        self.burst = burst
        self.tokens = burst
        self._now = now
        self._t = now()

    def take(self, n: float = 1.0) -> bool:
        t = self._now()
        self.tokens = min(self.burst, self.tokens + (t - self._t) * self.rate)
        self._t = t
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


class Limiter:
    """One bucket per key (an IP, a show and IP pair), forgotten once idle."""

    def __init__(self, rate: float, burst: float, ttl: float = 3600.0):
        self.rate, self.burst, self.ttl = rate, burst, ttl
        self._buckets: dict[str, tuple[TokenBucket, float]] = {}

    def take(self, key: str, n: float = 1.0) -> bool:
        now = time.monotonic()
        if len(self._buckets) > 10_000:
            self._buckets = {k: v for k, v in self._buckets.items() if now - v[1] < self.ttl}
        bucket = self._buckets.get(key, (TokenBucket(self.rate, self.burst), now))[0]
        self._buckets[key] = (bucket, now)
        return bucket.take(n)


# Per connection: comments, reactions, any message at all, and restyles.
def comment_bucket(creator: bool) -> TokenBucket:
    return TokenBucket(1 / 2, 3) if creator else TokenBucket(1 / 8, 3)


def react_bucket() -> TokenBucket:
    return TokenBucket(4, 8)


def message_bucket() -> TokenBucket:
    return TokenBucket(20, 40)


def restyle_bucket() -> TokenBucket:
    return TokenBucket(1 / 20, 1)


def interrupt_bucket() -> TokenBucket:
    """The producer interrupting the show: one every ten seconds, a couple in a row."""
    return TokenBucket(1 / 10, 2)


# Per show and client IP, for Add context over HTTP.
GUEST_CONTEXT = Limiter(rate=1 / 60, burst=2)
PRODUCER_CONTEXT = Limiter(rate=1 / 5, burst=3)

# Per user. Transcribing and researching a memo is paid for before any credits are
# (a memo held for review is only charged at Produce), so uploads are rationed.
MEMO_UPLOADS = Limiter(rate=1 / 300, burst=3, ttl=6 * 3600)
