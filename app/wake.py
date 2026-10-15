"""Waking the server up for scheduled briefings.

Cloud Run scales to zero, and the briefing loop (main._briefing_loop) only runs while
an instance is up. So whenever a schedule's next start time is set, a Cloud Task is
booked to call /healthz at that minute: the request starts an instance, and its
loop makes the briefing. Booking is cheap and nothing stays warm in between, unlike
pinging every few minutes, which with always-on CPU bills the idle instance.

Off unless WAKE_QUEUE is set (deploy.sh creates the queue): locally the process is
always running. A failed booking is logged, never raised: the schedule still holds,
and any visit to the app wakes it.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

log = logging.getLogger("podcasts.wake")


def _cloud_task(name: str, url: str, when: float) -> Any:
    from google.cloud import tasks_v2
    from google.protobuf import timestamp_pb2
    return tasks_v2.Task(name=name, schedule_time=timestamp_pb2.Timestamp(seconds=int(when)),
                         http_request=tasks_v2.HttpRequest(http_method=tasks_v2.HttpMethod.GET, url=url))


class Waker:
    def __init__(self, queue: str, url: str, *, client: Any = None, build: Callable[[str, str, float], Any] = _cloud_task):
        self.queue = queue      # projects/P/locations/R/queues/Q
        self.url = url.rstrip("/")
        self._client = client
        self._build = build
        self.booked: set[int] = set()

    @property
    def enabled(self) -> bool:
        return bool(self.queue and self.url)

    async def at(self, when: float | None) -> bool:
        """Book a wake-up for `when` (epoch seconds). One task per minute, however many
        listeners share it. True if a task was booked now."""
        if not self.enabled or not when:
            return False
        minute = int(when // 60)
        if minute in self.booked:
            return False
        name = f"{self.queue}/tasks/briefing-wake-{minute}"
        try:
            if self._client is None:
                from google.cloud import tasks_v2
                self._client = tasks_v2.CloudTasksAsyncClient()
            await self._client.create_task(parent=self.queue, task=self._build(name, f"{self.url}/healthz", minute * 60))
        except Exception as exc:  # AlreadyExists: someone else's schedule booked this minute
            if type(exc).__name__ != "AlreadyExists":
                log.warning("couldn't book a wake-up for %s: %s", minute * 60, exc)
                return False
        self.booked.add(minute)
        return True
