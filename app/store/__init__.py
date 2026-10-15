"""Where everything is kept: accounts, credits, personas, styles, connections, shows.

Two backends behind one interface:
  LocalStore  JSON files under DATA_DIR. For dev, tests and single-box hosting.
  GcpStore    Firestore documents + Cloud Storage files. For Cloud Run.

A live show never awaits the store directly; it hands writes to a Persister,
which applies them in the background and coalesces show-document updates.

Users (users/{uid}): uid, email, name, photo, credits, created_at, last_seen
  ledger/{id}       every credit change: amount, balance, reason, show_id, by, note, at
  personas/{id}     name, gender, voice, persona, likes, specialties, skills, context (private notes)
  styles/{id}       name, prompt, accent
  connections/{uid} the other user: status (connected | pending_in | pending_out), name, email, photo, since
  feed_seen_at      on the user document: when they last opened their Subscriptions feed
Shows (shows/{id}): see main.create_show for the full document. Access fields:
  owner_uid, owner_name, visibility (private | connections | public), shared_with [uid]
  Organizing: album_ids [album id], tags [label]
  Children: segments (by seq), comments (by id), context (by id; text in context/{id}.txt),
            sessions (one per listen: live socket or replay; see stats.py)
Albums (albums/{id}): owner_uid, owner_name, title, description, visibility, accent, created_at
  A pod is in an album when the album's id is in the pod's album_ids.
Subscriptions (subscriptions/{subscriber}__{kind}__{target}): subscriber_uid, kind
  (creator | album), target_id, target ("creator:uid" / "album:id", what counts are queried by), at
All timeline positions are integer samples at 24 kHz.

Queries use a single field each (owner, visibility, or shared_with) and sort in
Python, so Firestore needs no composite indexes. Fine at demo scale; past a few
thousand shows per query, add an index on (visibility, created_at).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from starlette.responses import Response

from ..config import Settings

log = logging.getLogger("podcasts.store")


class InsufficientCredits(Exception):
    def __init__(self, balance: int, needed: int):
        super().__init__(f"needs {needed} credits, has {balance}")
        self.balance = balance
        self.needed = needed


class Store(Protocol):
    # shows
    async def create_show(self, doc: dict) -> None: ...
    async def update_show(self, show_id: str, fields: dict) -> None: ...
    async def get_show(self, show_id: str) -> dict | None: ...
    async def list_shows(self, *, owner_uid: str | None = None, visibility: str | None = None,
                         owner_in: list[str] | None = None, shared_with: str | None = None,
                         album_id: str | None = None, limit: int = 200) -> list[dict]: ...
    async def delete_show(self, show_id: str) -> None: ...
    async def put_segment(self, show_id: str, seg: dict) -> None: ...
    async def put_comment(self, show_id: str, comment: dict) -> None: ...
    async def put_context(self, show_id: str, item: dict) -> None: ...
    async def load_children(self, show_id: str) -> dict: ...
    async def put_text(self, show_id: str, name: str, text: str) -> str: ...
    async def get_text(self, show_id: str, name: str) -> str | None: ...
    async def put_audio(self, show_id: str, data: bytes) -> dict: ...
    async def audio_response(self, show_id: str, doc: dict, download: bool, filename: str) -> Response: ...
    async def mark_interrupted(self, keep: set[str]) -> int: ...
    async def put_session(self, show_id: str, session: dict) -> None: ...   # merges into an existing one
    async def get_session(self, show_id: str, session_id: str) -> dict | None: ...
    async def list_sessions(self, show_id: str) -> list[dict]: ...
    # albums
    async def put_album(self, album: dict) -> None: ...
    async def get_album(self, album_id: str) -> dict | None: ...
    async def list_albums(self, *, owner_uid: str | None = None, visibility: str | None = None,
                          limit: int = 200) -> list[dict]: ...
    async def delete_album(self, album_id: str) -> None: ...
    # subscriptions
    async def put_subscription(self, sub: dict) -> None: ...
    async def delete_subscription(self, sub_id: str) -> None: ...
    async def list_subscriptions(self, subscriber_uid: str) -> list[dict]: ...
    async def count_subscribers(self, target: str) -> int: ...
    # users
    async def get_user(self, uid: str) -> dict | None: ...
    async def create_user(self, user: dict) -> bool: ...
    async def update_user(self, uid: str, fields: dict) -> None: ...
    async def find_user_by_email(self, email: str) -> dict | None: ...
    async def list_users(self, limit: int = 500) -> list[dict]: ...
    async def adjust_credits(self, uid: str, amount: int, *, reason: str, show_id: str | None = None,
                             by: str | None = None, note: str = "") -> int: ...
    async def ledger(self, uid: str, limit: int = 50) -> list[dict]: ...
    # personas and styles (kind = "personas" | "styles")
    async def list_items(self, uid: str, kind: str) -> list[dict]: ...
    async def put_item(self, uid: str, kind: str, item: dict) -> None: ...
    async def delete_item(self, uid: str, kind: str, item_id: str) -> None: ...
    # connections (one edge per side)
    async def get_edges(self, uid: str) -> list[dict]: ...
    async def get_edge(self, uid: str, other: str) -> dict | None: ...
    async def put_edge(self, uid: str, other: str, edge: dict) -> None: ...
    async def delete_edge(self, uid: str, other: str) -> None: ...


def make_store(settings: Settings) -> Store:
    if settings.storage_backend == "gcp":
        from .gcp import GcpStore  # imported lazily: local dev needs no GCP libraries
        return GcpStore(settings)
    from .local import LocalStore
    return LocalStore(settings.data_dir)


SHOW_FLUSH_SECONDS = 2.0


class Persister:
    """Background writer for one live show.

    Child writes (segments, comments, context) are applied in order. Show
    document fields are merged and flushed at most every SHOW_FLUSH_SECONDS,
    which keeps Firestore well under its one-write-per-second-per-document pace.
    """

    def __init__(self, store: Store, show_id: str):
        self.store = store
        self.show_id = show_id
        self._queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()
        self._fields: dict = {}
        self._writer = asyncio.create_task(self._write_loop())
        self._flusher = asyncio.create_task(self._flush_loop())

    def show(self, **fields) -> None:
        self._fields.update(fields)

    def segment(self, seg: dict) -> None:
        self._queue.put_nowait(("segment", seg))

    def comment(self, comment: dict) -> None:
        self._queue.put_nowait(("comment", comment))

    def context(self, item: dict) -> None:
        self._queue.put_nowait(("context", item))

    def session(self, session: dict) -> None:
        self._queue.put_nowait(("session", session))

    async def _write_loop(self) -> None:
        while True:
            kind, doc = await self._queue.get()
            try:
                if kind == "segment":
                    await self.store.put_segment(self.show_id, doc)
                elif kind == "comment":
                    await self.store.put_comment(self.show_id, doc)
                elif kind == "session":
                    await self.store.put_session(self.show_id, doc)
                else:
                    await self.store.put_context(self.show_id, doc)
            except Exception:
                log.exception("could not save a %s for show %s", kind, self.show_id)
            finally:
                self._queue.task_done()

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(SHOW_FLUSH_SECONDS)
            await self._flush_fields()

    async def _flush_fields(self) -> None:
        if not self._fields:
            return
        fields, self._fields = self._fields, {}
        try:
            await self.store.update_show(self.show_id, fields)
        except Exception:
            log.exception("could not update show %s; will retry", self.show_id)
            self._fields = {**fields, **self._fields}

    async def drain(self) -> None:
        await self._queue.join()
        await self._flush_fields()

    async def close(self) -> None:
        try:
            await asyncio.wait_for(self.drain(), timeout=10)
        except asyncio.TimeoutError:
            log.warning("show %s: gave up flushing after 10 s", self.show_id)
        for task in (self._writer, self._flusher):
            task.cancel()
