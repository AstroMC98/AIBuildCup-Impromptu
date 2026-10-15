"""Everything on Google Cloud: Firestore for documents, Cloud Storage for files.

Firestore (native mode, (default) database):
    shows/{id}                      show document (see store/__init__.py)
    shows/{id}/segments/{seq:05d}
    shows/{id}/comments/{id}
    shows/{id}/context/{id}
    shows/{id}/sessions/{id}        one per listen (live socket or replay)
    users/{uid}                     account + credit balance
    users/{uid}/ledger|personas|styles|connections/{id}
    albums/{id}
    subscriptions/{subscriber}__{kind}__{target}
Cloud Storage (GCS_BUCKET, uniform access, public access prevented):
    shows/{id}/source.txt
    shows/{id}/context/{id}.txt
    shows/{id}/episode.mp3

Only single-field queries are used (order by created_at, filter by status), so
no composite indexes are needed. Finished episodes are served with a V4 signed
URL, so seeking works natively and audio bytes never pass through the single
Cloud Run instance. Signing on Cloud Run uses the IAM signBlob API, which needs
the runtime service account to hold Service Account Token Creator on itself.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import time

import google.auth
import google.auth.transport.requests
from google.api_core.exceptions import Conflict, NotFound
from google.cloud import firestore, storage
from google.cloud.firestore_v1.base_query import FieldFilter
from starlette.responses import RedirectResponse, Response

from ..config import Settings
from . import InsufficientCredits

SIGNED_URL_TTL = dt.timedelta(hours=1)
SIGNED_URL_REUSE_SECONDS = 50 * 60


class GcpStore:
    def __init__(self, settings: Settings):
        project = settings.gcp_project or None
        self.db = firestore.AsyncClient(project=project, database=settings.firestore_database)
        self.gcs = storage.Client(project=project)
        self.bucket = self.gcs.bucket(settings.gcs_bucket)
        self._credentials, _ = google.auth.default()
        self._signed: dict[tuple[str, bool], tuple[str, float]] = {}

    def _show(self, show_id: str):
        return self.db.collection("shows").document(show_id)

    def _blob(self, show_id: str, name: str):
        return self.bucket.blob(f"shows/{show_id}/{name}")

    # ---- show documents -------------------------------------------------------

    async def create_show(self, doc: dict) -> None:
        await self._show(doc["id"]).set({**doc, "updated_at": time.time()})

    async def update_show(self, show_id: str, fields: dict) -> None:
        await self._show(show_id).set({**fields, "updated_at": time.time()}, merge=True)

    async def get_show(self, show_id: str) -> dict | None:
        snap = await self._show(show_id).get()
        return snap.to_dict() if snap.exists else None

    async def list_shows(self, *, owner_uid: str | None = None, visibility: str | None = None,
                         owner_in: list[str] | None = None, shared_with: str | None = None,
                         album_id: str | None = None, limit: int = 200) -> list[dict]:
        """One indexed field per query (no composite indexes); the rest is filtered here."""
        shows = self.db.collection("shows")
        if album_id is not None:
            queries = [shows.where(filter=FieldFilter("album_ids", "array_contains", album_id))]
        elif owner_uid is not None:
            queries = [shows.where(filter=FieldFilter("owner_uid", "==", owner_uid))]
        elif shared_with is not None:
            queries = [shows.where(filter=FieldFilter("shared_with", "array_contains", shared_with))]
        elif owner_in is not None:
            ids = list(owner_in)
            queries = [shows.where(filter=FieldFilter("owner_uid", "in", ids[i:i + 30])) for i in range(0, len(ids), 30)]
        elif visibility is not None:
            queries = [shows.where(filter=FieldFilter("visibility", "==", visibility))]
        else:
            queries = [shows.order_by("created_at", direction=firestore.Query.DESCENDING).limit(limit)]
        docs: list[dict] = []
        for q in queries:
            docs += [snap.to_dict() async for snap in q.limit(2000).stream()]
        if visibility is not None:
            docs = [d for d in docs if d.get("visibility") == visibility]
        if owner_uid is not None:
            docs = [d for d in docs if d.get("owner_uid") == owner_uid]
        docs.sort(key=lambda d: d.get("created_at", 0), reverse=True)
        return docs[:limit]

    async def delete_show(self, show_id: str) -> None:
        ref = self._show(show_id)
        for sub in ("segments", "comments", "context", "sessions"):
            async for snap in ref.collection(sub).stream():
                await snap.reference.delete()
        await ref.delete()

        def remove_files() -> None:
            for blob in self.gcs.list_blobs(self.bucket, prefix=f"shows/{show_id}/"):
                blob.delete()
        await asyncio.to_thread(remove_files)

    # ---- children -------------------------------------------------------------

    async def put_segment(self, show_id: str, seg: dict) -> None:
        await self._show(show_id).collection("segments").document(f"{seg['seq']:05d}").set(seg)

    async def delete_segment(self, show_id: str, seq: int) -> None:
        await self._show(show_id).collection("segments").document(f"{int(seq):05d}").delete()

    async def put_comment(self, show_id: str, comment: dict) -> None:
        await self._show(show_id).collection("comments").document(str(comment["id"])).set(comment)

    async def put_context(self, show_id: str, item: dict) -> None:
        await self._show(show_id).collection("context").document(str(item["id"])).set(item)

    async def load_children(self, show_id: str) -> dict:
        ref = self._show(show_id)

        async def load(sub: str, key: str) -> list[dict]:
            docs = [snap.to_dict() async for snap in ref.collection(sub).stream()]
            return sorted(docs, key=lambda d: d.get(key, 0))

        segments, comments, context = await asyncio.gather(
            load("segments", "seq"), load("comments", "id"), load("context", "id")
        )
        return {"segments": segments, "comments": comments, "context": context}

    async def put_session(self, show_id: str, session: dict) -> None:
        await self._show(show_id).collection("sessions").document(session["id"]).set(session, merge=True)

    async def get_session(self, show_id: str, session_id: str) -> dict | None:
        snap = await self._show(show_id).collection("sessions").document(session_id).get()
        return snap.to_dict() if snap.exists else None

    async def list_sessions(self, show_id: str) -> list[dict]:
        docs = [snap.to_dict() async for snap in self._show(show_id).collection("sessions").stream()]
        return sorted(docs, key=lambda d: d.get("joined_at", 0))

    # ---- albums and subscriptions ---------------------------------------------------

    async def put_album(self, album: dict) -> None:
        await self.db.collection("albums").document(album["id"]).set(album)

    async def get_album(self, album_id: str) -> dict | None:
        snap = await self.db.collection("albums").document(album_id).get()
        return snap.to_dict() if snap.exists else None

    async def list_albums(self, *, owner_uid: str | None = None, visibility: str | None = None,
                          limit: int = 200) -> list[dict]:
        albums = self.db.collection("albums")
        if owner_uid is not None:
            query = albums.where(filter=FieldFilter("owner_uid", "==", owner_uid))
        elif visibility is not None:
            query = albums.where(filter=FieldFilter("visibility", "==", visibility))
        else:
            query = albums
        docs = [snap.to_dict() async for snap in query.limit(2000).stream()]
        docs = [d for d in docs if visibility is None or d.get("visibility") == visibility]
        docs.sort(key=lambda d: d.get("created_at", 0), reverse=True)
        return docs[:limit]

    async def delete_album(self, album_id: str) -> None:
        await self.db.collection("albums").document(album_id).delete()

    async def put_subscription(self, sub: dict) -> None:
        await self.db.collection("subscriptions").document(sub["id"]).set(sub)

    async def delete_subscription(self, sub_id: str) -> None:
        await self.db.collection("subscriptions").document(sub_id).delete()

    async def list_subscriptions(self, subscriber_uid: str) -> list[dict]:
        query = self.db.collection("subscriptions").where(filter=FieldFilter("subscriber_uid", "==", subscriber_uid))
        subs = [snap.to_dict() async for snap in query.stream()]
        return sorted(subs, key=lambda s: s.get("at", 0), reverse=True)

    async def count_subscribers(self, target: str) -> int:
        query = self.db.collection("subscriptions").where(filter=FieldFilter("target", "==", target))
        result = await query.count().get()
        return int(result[0][0].value) if result and result[0] else 0

    # ---- files -------------------------------------------------------------------

    async def put_text(self, show_id: str, name: str, text: str) -> str:
        blob = self._blob(show_id, name)
        await asyncio.to_thread(blob.upload_from_string, text, content_type="text/plain; charset=utf-8")
        return name

    async def get_text(self, show_id: str, name: str) -> str | None:
        blob = self._blob(show_id, name)
        try:
            return await asyncio.to_thread(blob.download_as_text)
        except NotFound:
            return None

    async def put_audio(self, show_id: str, data: bytes) -> dict:
        blob = self._blob(show_id, "episode.mp3")
        await asyncio.to_thread(blob.upload_from_string, data, content_type="audio/mpeg")
        return {"path": "episode.mp3", "bytes": len(data), "mime": "audio/mpeg"}

    async def audio_response(self, show_id: str, doc: dict, download: bool, filename: str) -> Response:
        if not doc.get("audio"):
            return Response("No audio saved for this show.", status_code=404)
        key = (show_id, download)
        cached = self._signed.get(key)
        if cached and time.time() - cached[1] < SIGNED_URL_REUSE_SECONDS:
            return RedirectResponse(cached[0], status_code=307)
        url = await asyncio.to_thread(self._sign, show_id, download, filename)
        self._signed[key] = (url, time.time())
        return RedirectResponse(url, status_code=307)

    def _sign(self, show_id: str, download: bool, filename: str) -> str:
        creds = self._credentials
        creds.refresh(google.auth.transport.requests.Request())
        disposition = "attachment" if download else "inline"
        return self._blob(show_id, "episode.mp3").generate_signed_url(
            version="v4",
            expiration=SIGNED_URL_TTL,
            method="GET",
            service_account_email=getattr(creds, "service_account_email", None),
            access_token=creds.token,
            response_type="audio/mpeg",
            response_disposition=f'{disposition}; filename="{filename}"',
        )

    # ---- users -----------------------------------------------------------------------

    def _user(self, uid: str):
        return self.db.collection("users").document(uid)

    async def get_user(self, uid: str) -> dict | None:
        snap = await self._user(uid).get()
        return snap.to_dict() if snap.exists else None

    async def create_user(self, user: dict) -> bool:
        try:
            await self._user(user["uid"]).create(user)
            return True
        except Conflict:
            return False

    async def update_user(self, uid: str, fields: dict) -> None:
        await self._user(uid).set(fields, merge=True)

    async def find_user_by_email(self, email: str) -> dict | None:
        query = self.db.collection("users").where(filter=FieldFilter("email", "==", email.strip().lower())).limit(1)
        async for snap in query.stream():
            return snap.to_dict()
        return None

    async def list_users(self, limit: int = 500) -> list[dict]:
        query = self.db.collection("users").order_by("created_at", direction=firestore.Query.DESCENDING).limit(limit)
        return [snap.to_dict() async for snap in query.stream()]

    # ---- briefings (single-field range queries: no composite index needed) -------------------

    async def users_due_for_briefing(self, before: float, limit: int = 50) -> list[dict]:
        query = self.db.collection("users").where(filter=FieldFilter("briefing_due_at", "<=", before)).limit(limit)
        return [snap.to_dict() async for snap in query.stream()]

    async def expired_shows(self, before: float, limit: int = 100) -> list[dict]:
        query = self.db.collection("shows").where(filter=FieldFilter("expires_at", "<=", before)).limit(limit)
        return [snap.to_dict() async for snap in query.stream()]

    async def adjust_credits(self, uid: str, amount: int, *, reason: str, show_id: str | None = None,
                             by: str | None = None, note: str = "") -> int:
        """Add (or, negative, spend) credits in one transaction with its ledger entry."""
        ref = self._user(uid)
        entry_ref = ref.collection("ledger").document()

        @firestore.async_transactional
        async def run(tx) -> int:
            snap = await ref.get(transaction=tx)
            if not snap.exists:
                raise KeyError(uid)
            balance = int((snap.to_dict() or {}).get("credits", 0))
            if amount < 0 and balance + amount < 0:
                raise InsufficientCredits(balance, -amount)
            tx.update(ref, {"credits": balance + amount})
            tx.set(entry_ref, {
                "id": entry_ref.id, "amount": amount, "balance": balance + amount, "reason": reason,
                "show_id": show_id, "by": by, "note": note[:200], "at": time.time(),
            })
            return balance + amount

        return await run(self.db.transaction())

    async def ledger(self, uid: str, limit: int = 50) -> list[dict]:
        query = self._user(uid).collection("ledger").order_by("at", direction=firestore.Query.DESCENDING).limit(limit)
        return [snap.to_dict() async for snap in query.stream()]

    # ---- personas and styles ---------------------------------------------------------------

    async def list_items(self, uid: str, kind: str) -> list[dict]:
        items = [snap.to_dict() async for snap in self._user(uid).collection(kind).stream()]
        return sorted(items, key=lambda d: d.get("created_at", 0))

    async def put_item(self, uid: str, kind: str, item: dict) -> None:
        await self._user(uid).collection(kind).document(item["id"]).set(item)

    async def delete_item(self, uid: str, kind: str, item_id: str) -> None:
        await self._user(uid).collection(kind).document(item_id).delete()

    # ---- connections ---------------------------------------------------------------------------

    async def get_edges(self, uid: str) -> list[dict]:
        return [snap.to_dict() async for snap in self._user(uid).collection("connections").stream()]

    async def get_edge(self, uid: str, other: str) -> dict | None:
        snap = await self._user(uid).collection("connections").document(other).get()
        return snap.to_dict() if snap.exists else None

    async def put_edge(self, uid: str, other: str, edge: dict) -> None:
        await self._user(uid).collection("connections").document(other).set(edge)

    async def delete_edge(self, uid: str, other: str) -> None:
        await self._user(uid).collection("connections").document(other).delete()

    async def mark_interrupted(self, keep: set[str]) -> int:
        query = self.db.collection("shows").where(filter=FieldFilter("status", "==", "live"))
        count = 0
        async for snap in query.stream():
            if snap.id not in keep:
                await snap.reference.set(
                    {"status": "interrupted", "ended_reason": "restart", "updated_at": time.time()},
                    merge=True,
                )
                count += 1
        return count
