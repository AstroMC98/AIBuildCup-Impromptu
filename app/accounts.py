"""Accounts, who can see what, and connections.

Access to a pod:
  public       anyone, signed in or not (signed-out visitors can only listen)
  connections  the owner's connections
  private      the owner, plus connections it was shared with by name
The owner always; admins too (so they can take things down).

Connections are mutual: one user sends a request, the other accepts. Each side
keeps its own edge (users/{uid}/connections/{other}) so listing them is one read.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .store import Store

USER_CACHE_SECONDS = 15.0

# Pods only their owner can see, whatever their visibility: drafts, and voice-memo
# pods still being made (rendering) or waiting for the owner to approve the outline.
UNPUBLISHED = {"draft", "rendering", "review"}


def published(doc: dict) -> bool:
    """Whether a pod is out, so others may see it. A memo pod that failed before
    any audio was saved never came out; a live pod that failed did (people heard it)."""
    status = doc.get("status")
    if status in UNPUBLISHED:
        return False
    return not (status == "failed" and doc.get("kind") == "memo" and not doc.get("audio"))


@dataclass
class User:
    uid: str
    email: str
    name: str
    photo: str
    credits: int
    admin: bool

    def public(self) -> dict:
        """What other people may see about this user."""
        return {"uid": self.uid, "name": self.name, "photo": self.photo}

    def me(self) -> dict:
        return {**self.public(), "email": self.email, "credits": self.credits, "admin": self.admin}


class Accounts:
    def __init__(self, store: Store, admins: set[str], signup_credits: int):
        self.store = store
        self.admins = admins
        self.signup_credits = signup_credits
        self._cache: dict[str, tuple[User, float]] = {}

    def _user(self, doc: dict) -> User:
        email = doc.get("email", "")
        return User(
            uid=doc["uid"], email=email, name=doc.get("name") or "Listener", photo=doc.get("photo", ""),
            credits=int(doc.get("credits", 0)), admin=email.lower() in self.admins,
        )

    def forget(self, uid: str) -> None:
        self._cache.pop(uid, None)

    async def get(self, uid: str | None) -> User | None:
        if not uid:
            return None
        hit = self._cache.get(uid)
        if hit and time.monotonic() - hit[1] < USER_CACHE_SECONDS:
            return hit[0]
        doc = await self.store.get_user(uid)
        if not doc:
            return None
        user = self._user(doc)
        self._cache[uid] = (user, time.monotonic())
        return user

    async def sign_in(self, profile: dict) -> User:
        """First sign-in creates the account (with any signup credits)."""
        now = time.time()
        doc = {
            "uid": profile["uid"], "email": profile["email"], "name": profile["name"][:60],
            "photo": profile.get("photo", ""), "credits": 0, "created_at": now, "last_seen": now,
        }
        created = await self.store.create_user(doc)
        if created and self.signup_credits > 0:
            await self.store.adjust_credits(profile["uid"], self.signup_credits, reason="signup",
                                            note="Welcome credits")
        if not created:
            await self.store.update_user(profile["uid"], {
                "email": profile["email"], "name": profile["name"][:60],
                "photo": profile.get("photo", ""), "last_seen": now,
            })
        self.forget(profile["uid"])
        return await self.get(profile["uid"])  # type: ignore[return-value]

    async def adjust_credits(self, uid: str, amount: int, **kw) -> int:
        try:
            return await self.store.adjust_credits(uid, amount, **kw)
        finally:
            self.forget(uid)

    async def top_up_credits(self, uid: str, target: int, **kw) -> tuple[int, int]:
        """Bring a balance back up to `target` (never down). Returns (balance, added)."""
        try:
            return await self.store.top_up_credits(uid, target, **kw)
        finally:
            self.forget(uid)

    # ---- access ----------------------------------------------------------------

    async def connected(self, a: str, b: str) -> bool:
        edge = await self.store.get_edge(a, b)
        return bool(edge and edge.get("status") == "connected")

    async def can_view(self, user: User | None, doc: dict) -> bool:
        if doc.get("visibility") == "public" and published(doc):
            return True
        if user is None:
            return False
        if user.uid == doc.get("owner_uid") or user.admin:
            return True
        if not published(doc):
            return False
        if user.uid in doc.get("shared_with", []):
            return True
        if doc.get("visibility") == "connections":
            return await self.connected(user.uid, doc.get("owner_uid", ""))
        return False

    def role(self, user: User | None, doc: dict) -> str:
        if user and user.uid == doc.get("owner_uid"):
            return "producer"
        return "listener" if user else "visitor"

    # ---- connections ---------------------------------------------------------------

    async def connection_ids(self, uid: str) -> list[str]:
        return [e["uid"] for e in await self.store.get_edges(uid) if e.get("status") == "connected"]

    def _edge(self, other: User, status: str, since: float) -> dict:
        return {"uid": other.uid, "name": other.name, "email": other.email, "photo": other.photo,
                "status": status, "since": since}

    async def request(self, me: User, other: User) -> str:
        """Ask to connect. If they already asked us, this accepts instead."""
        if me.uid == other.uid:
            raise ValueError("That's you.")
        mine = await self.store.get_edge(me.uid, other.uid)
        if mine and mine.get("status") == "connected":
            return "connected"
        if mine and mine.get("status") == "pending_in":
            return await self.accept(me, other)
        now = time.time()
        await self.store.put_edge(me.uid, other.uid, self._edge(other, "pending_out", now))
        await self.store.put_edge(other.uid, me.uid, self._edge(me, "pending_in", now))
        return "pending_out"

    async def accept(self, me: User, other: User) -> str:
        mine = await self.store.get_edge(me.uid, other.uid)
        if not mine or mine.get("status") not in {"pending_in", "connected"}:
            raise ValueError("There's no request from them to accept.")
        now = time.time()
        await self.store.put_edge(me.uid, other.uid, self._edge(other, "connected", now))
        await self.store.put_edge(other.uid, me.uid, self._edge(me, "connected", now))
        return "connected"

    async def remove(self, me: User, other_uid: str) -> None:
        """Decline, cancel, or disconnect: both sides forget each other."""
        await self.store.delete_edge(me.uid, other_uid)
        await self.store.delete_edge(other_uid, me.uid)
