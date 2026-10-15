"""Everything on local disk under DATA_DIR, in the same shape as Firestore + GCS.

    shows/{id}/show.json, segments/00001.json, comments/1.json, context/1.json,
              context/1.txt, source.txt, episode.mp3
              sessions/{id}.json, files/{name}
    users/{uid}/user.json, ledger/{id}.json, personas/{id}.json, styles/{id}.json,
              connections/{other_uid}.json, files/{name}
    albums/{id}.json    subscriptions/{id}.json

Every write goes to a temp file first and is then swapped in, so a crash never
leaves a half-written JSON file. Credit changes hold a per-user lock; this
backend assumes one server process, like the live rooms do.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
import uuid
from collections import defaultdict
from pathlib import Path

from starlette.responses import FileResponse, Response

from . import InsufficientCredits, check_file_name

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_bytes(data)
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: a reader still has the old file open
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _dump(doc: dict) -> bytes:
    return json.dumps(doc, ensure_ascii=False, indent=1).encode("utf-8")


def _safe(part: str) -> str:
    if not _SAFE_ID.match(part or ""):
        raise ValueError("bad id")
    return part


class LocalStore:
    def __init__(self, data_dir: str):
        self.root = Path(data_dir) / "shows"
        self.users = Path(data_dir) / "users"
        self.albums = Path(data_dir) / "albums"
        self.subs = Path(data_dir) / "subscriptions"
        for d in (self.root, self.users, self.albums, self.subs):
            d.mkdir(parents=True, exist_ok=True)
        self._credit_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _dir(self, show_id: str) -> Path:
        return self.root / _safe(show_id)

    def _udir(self, uid: str) -> Path:
        return self.users / _safe(uid)

    # ---- show documents -------------------------------------------------------

    async def create_show(self, doc: dict) -> None:
        doc = {**doc, "updated_at": time.time()}
        await asyncio.to_thread(_atomic_write, self._dir(doc["id"]) / "show.json", _dump(doc))

    async def update_show(self, show_id: str, fields: dict) -> None:
        def run() -> None:
            path = self._dir(show_id) / "show.json"
            doc = _read_json(path)
            if doc is None:
                return
            doc.update(fields)
            doc["updated_at"] = time.time()
            _atomic_write(path, _dump(doc))
        await asyncio.to_thread(run)

    async def get_show(self, show_id: str) -> dict | None:
        try:
            path = self._dir(show_id) / "show.json"
        except ValueError:
            return None
        return await asyncio.to_thread(_read_json, path)

    async def list_shows(self, *, owner_uid: str | None = None, visibility: str | None = None,
                         owner_in: list[str] | None = None, shared_with: str | None = None,
                         album_id: str | None = None, limit: int = 200) -> list[dict]:
        def keep(d: dict) -> bool:
            if album_id is not None and album_id not in d.get("album_ids", []):
                return False
            if owner_uid is not None and d.get("owner_uid") != owner_uid:
                return False
            if visibility is not None and d.get("visibility") != visibility:
                return False
            if owner_in is not None and d.get("owner_uid") not in owner_in:
                return False
            if shared_with is not None and shared_with not in d.get("shared_with", []):
                return False
            return True

        def run() -> list[dict]:
            docs = [d for p in self.root.glob("*/show.json") if (d := _read_json(p)) and keep(d)]
            docs.sort(key=lambda d: d.get("created_at", 0), reverse=True)
            return docs[:limit]
        return await asyncio.to_thread(run)

    async def shows_with_status(self, status: str, limit: int = 500) -> list[dict]:
        return [d for d in await self.list_shows(limit=100_000) if d.get("status") == status][:limit]

    async def delete_show(self, show_id: str) -> None:
        await asyncio.to_thread(shutil.rmtree, self._dir(show_id), True)

    # ---- show children -------------------------------------------------------------

    async def put_segment(self, show_id: str, seg: dict) -> None:
        path = self._dir(show_id) / "segments" / f"{seg['seq']:05d}.json"
        await asyncio.to_thread(_atomic_write, path, _dump(seg))

    async def put_comment(self, show_id: str, comment: dict) -> None:
        path = self._dir(show_id) / "comments" / f"{comment['id']}.json"
        await asyncio.to_thread(_atomic_write, path, _dump(comment))

    async def put_context(self, show_id: str, item: dict) -> None:
        path = self._dir(show_id) / "context" / f"{item['id']}.json"
        await asyncio.to_thread(_atomic_write, path, _dump(item))

    async def load_children(self, show_id: str) -> dict:
        def load(sub: str, key: str) -> list[dict]:
            docs = [d for p in (self._dir(show_id) / sub).glob("*.json") if (d := _read_json(p))]
            return sorted(docs, key=lambda d: d.get(key, 0))

        def run() -> dict:
            return {
                "segments": load("segments", "seq"),
                "comments": load("comments", "id"),
                "context": load("context", "id"),
            }
        return await asyncio.to_thread(run)

    # ---- show files ----------------------------------------------------------------------

    async def put_text(self, show_id: str, name: str, text: str) -> str:
        await asyncio.to_thread(_atomic_write, self._dir(show_id) / name, text.encode("utf-8"))
        return name

    async def get_text(self, show_id: str, name: str) -> str | None:
        path = self._dir(show_id) / name
        try:
            return await asyncio.to_thread(path.read_text, encoding="utf-8")
        except FileNotFoundError:
            return None

    async def put_audio(self, show_id: str, data: bytes) -> dict:
        await asyncio.to_thread(_atomic_write, self._dir(show_id) / "episode.mp3", data)
        return {"path": "episode.mp3", "bytes": len(data), "mime": "audio/mpeg"}

    async def audio_response(self, show_id: str, doc: dict, download: bool, filename: str) -> Response:
        path = self._dir(show_id) / "episode.mp3"
        if not path.exists():
            return Response("No audio saved for this show.", status_code=404)
        return FileResponse(
            path,
            media_type="audio/mpeg",
            filename=filename,
            content_disposition_type="attachment" if download else "inline",
        )

    async def mark_interrupted(self, keep: set[str]) -> int:
        count = 0
        for doc in await self.list_shows(limit=100_000):
            if doc.get("status") == "live" and doc["id"] not in keep:
                await self.update_show(doc["id"], {"status": "interrupted", "ended_reason": "restart"})
                count += 1
        return count

    # ---- listening sessions -------------------------------------------------------------------

    async def put_session(self, show_id: str, session: dict) -> None:
        path = self._dir(show_id) / "sessions" / f"{_safe(session['id'])}.json"

        def run() -> None:
            if not path.parent.parent.exists():
                return  # the show was deleted
            _atomic_write(path, _dump({**(_read_json(path) or {}), **session}))
        async with self._credit_locks[f"session:{show_id}"]:
            await asyncio.to_thread(run)

    async def get_session(self, show_id: str, session_id: str) -> dict | None:
        try:
            path = self._dir(show_id) / "sessions" / f"{_safe(session_id)}.json"
        except ValueError:
            return None
        return await asyncio.to_thread(_read_json, path)

    async def list_sessions(self, show_id: str) -> list[dict]:
        def run() -> list[dict]:
            docs = [d for p in (self._dir(show_id) / "sessions").glob("*.json") if (d := _read_json(p))]
            return sorted(docs, key=lambda d: d.get("joined_at", 0))
        return await asyncio.to_thread(run)

    # ---- files ---------------------------------------------------------------------------------

    async def put_show_file(self, show_id: str, name: str, data: bytes, mime: str) -> dict:
        await asyncio.to_thread(_atomic_write, self._dir(show_id) / "files" / check_file_name(name), data)
        return {"path": name, "bytes": len(data), "mime": mime}

    async def get_show_file(self, show_id: str, name: str) -> bytes | None:
        return await asyncio.to_thread(_read_bytes, self._dir(show_id) / "files" / check_file_name(name))

    async def put_user_file(self, uid: str, name: str, data: bytes, mime: str) -> dict:
        await asyncio.to_thread(_atomic_write, self._udir(uid) / "files" / check_file_name(name), data)
        return {"path": name, "bytes": len(data), "mime": mime}

    async def get_user_file(self, uid: str, name: str) -> bytes | None:
        return await asyncio.to_thread(_read_bytes, self._udir(uid) / "files" / check_file_name(name))

    # ---- albums ---------------------------------------------------------------------------------

    async def put_album(self, album: dict) -> None:
        await asyncio.to_thread(_atomic_write, self.albums / f"{_safe(album['id'])}.json", _dump(album))

    async def get_album(self, album_id: str) -> dict | None:
        try:
            path = self.albums / f"{_safe(album_id)}.json"
        except ValueError:
            return None
        return await asyncio.to_thread(_read_json, path)

    async def list_albums(self, *, owner_uid: str | None = None, visibility: str | None = None,
                          limit: int = 200) -> list[dict]:
        def run() -> list[dict]:
            docs = [d for p in self.albums.glob("*.json") if (d := _read_json(p))
                    and (owner_uid is None or d.get("owner_uid") == owner_uid)
                    and (visibility is None or d.get("visibility") == visibility)]
            docs.sort(key=lambda d: d.get("created_at", 0), reverse=True)
            return docs[:limit]
        return await asyncio.to_thread(run)

    async def delete_album(self, album_id: str) -> None:
        path = self.albums / f"{_safe(album_id)}.json"
        await asyncio.to_thread(lambda: path.unlink(missing_ok=True))

    # ---- subscriptions -------------------------------------------------------------------------

    async def put_subscription(self, sub: dict) -> None:
        await asyncio.to_thread(_atomic_write, self.subs / f"{_safe(sub['id'])}.json", _dump(sub))

    async def delete_subscription(self, sub_id: str) -> None:
        path = self.subs / f"{_safe(sub_id)}.json"
        await asyncio.to_thread(lambda: path.unlink(missing_ok=True))

    async def _all_subscriptions(self) -> list[dict]:
        return await asyncio.to_thread(lambda: [d for p in self.subs.glob("*.json") if (d := _read_json(p))])

    async def list_subscriptions(self, subscriber_uid: str) -> list[dict]:
        subs = [s for s in await self._all_subscriptions() if s.get("subscriber_uid") == subscriber_uid]
        return sorted(subs, key=lambda s: s.get("at", 0), reverse=True)

    async def count_subscribers(self, target: str) -> int:
        return sum(s.get("target") == target for s in await self._all_subscriptions())

    # ---- users -----------------------------------------------------------------------------

    async def get_user(self, uid: str) -> dict | None:
        try:
            path = self._udir(uid) / "user.json"
        except ValueError:
            return None
        return await asyncio.to_thread(_read_json, path)

    async def create_user(self, user: dict) -> bool:
        """Create the account if it doesn't exist yet. True if it was created."""
        async with self._credit_locks[user["uid"]]:
            path = self._udir(user["uid"]) / "user.json"
            if await asyncio.to_thread(path.exists):
                return False
            await asyncio.to_thread(_atomic_write, path, _dump(user))
            return True

    async def update_user(self, uid: str, fields: dict) -> None:
        async with self._credit_locks[uid]:
            def run() -> None:
                path = self._udir(uid) / "user.json"
                doc = _read_json(path)
                if doc is not None:
                    doc.update(fields)
                    _atomic_write(path, _dump(doc))
            await asyncio.to_thread(run)

    async def find_user_by_email(self, email: str) -> dict | None:
        email = email.strip().lower()
        for user in await self.list_users(limit=100_000):
            if user.get("email") == email:
                return user
        return None

    async def list_users(self, limit: int = 500) -> list[dict]:
        def run() -> list[dict]:
            users = [d for p in self.users.glob("*/user.json") if (d := _read_json(p))]
            users.sort(key=lambda u: u.get("created_at", 0), reverse=True)
            return users[:limit]
        return await asyncio.to_thread(run)

    async def adjust_credits(self, uid: str, amount: int, *, reason: str, show_id: str | None = None,
                             by: str | None = None, note: str = "") -> int:
        """Add (or, negative, spend) credits. Spending more than the balance raises."""
        async with self._credit_locks[uid]:
            path = self._udir(uid) / "user.json"
            user = await asyncio.to_thread(_read_json, path)
            if user is None:
                raise KeyError(uid)
            balance = int(user.get("credits", 0))
            if amount < 0 and balance + amount < 0:
                raise InsufficientCredits(balance, -amount)
            user["credits"] = balance + amount
            entry = {
                "id": f"{time.time_ns()}", "amount": amount, "balance": user["credits"], "reason": reason,
                "show_id": show_id, "by": by, "note": note[:200], "at": time.time(),
            }
            await asyncio.to_thread(_atomic_write, path, _dump(user))
            await asyncio.to_thread(_atomic_write, self._udir(uid) / "ledger" / f"{entry['id']}.json", _dump(entry))
            return user["credits"]

    async def ledger(self, uid: str, limit: int = 50) -> list[dict]:
        def run() -> list[dict]:
            entries = [d for p in (self._udir(uid) / "ledger").glob("*.json") if (d := _read_json(p))]
            entries.sort(key=lambda e: e.get("at", 0), reverse=True)
            return entries[:limit]
        return await asyncio.to_thread(run)

    async def ledger_for_show(self, uid: str, show_id: str) -> list[dict]:
        return [e for e in await self.ledger(uid, limit=100_000) if e.get("show_id") == show_id]

    # ---- personas and styles ------------------------------------------------------------------

    async def list_items(self, uid: str, kind: str) -> list[dict]:
        def run() -> list[dict]:
            items = [d for p in (self._udir(uid) / _safe(kind)).glob("*.json") if (d := _read_json(p))]
            return sorted(items, key=lambda d: d.get("created_at", 0))
        return await asyncio.to_thread(run)

    async def put_item(self, uid: str, kind: str, item: dict) -> None:
        path = self._udir(uid) / _safe(kind) / f"{_safe(item['id'])}.json"
        await asyncio.to_thread(_atomic_write, path, _dump(item))

    async def delete_item(self, uid: str, kind: str, item_id: str) -> None:
        path = self._udir(uid) / _safe(kind) / f"{_safe(item_id)}.json"
        await asyncio.to_thread(lambda: path.unlink(missing_ok=True))

    # ---- connections ------------------------------------------------------------------------------

    async def get_edges(self, uid: str) -> list[dict]:
        def run() -> list[dict]:
            return [d for p in (self._udir(uid) / "connections").glob("*.json") if (d := _read_json(p))]
        return await asyncio.to_thread(run)

    async def get_edge(self, uid: str, other: str) -> dict | None:
        try:
            path = self._udir(uid) / "connections" / f"{_safe(other)}.json"
        except ValueError:
            return None
        return await asyncio.to_thread(_read_json, path)

    async def put_edge(self, uid: str, other: str, edge: dict) -> None:
        path = self._udir(uid) / "connections" / f"{_safe(other)}.json"
        await asyncio.to_thread(_atomic_write, path, _dump(edge))

    async def delete_edge(self, uid: str, other: str) -> None:
        path = self._udir(uid) / "connections" / f"{_safe(other)}.json"
        await asyncio.to_thread(lambda: path.unlink(missing_ok=True))
