"""The storage contract: LocalStore and GcpStore must answer the same way.

    python -m tests.store_contract                 # LocalStore only
    FIRESTORE_EMULATOR_HOST=127.0.0.1:8681 python -m tests.store_contract
                                                   # ...and GcpStore against the Firestore emulator

Start the emulator with Docker (no Java needed):
    docker run -d --name podcasts-firestore -p 8681:8681 \\
      gcr.io/google.com/cloudsdktool/google-cloud-cli:emulators \\
      gcloud emulators firestore start --host-port=0.0.0.0:8681

Covers every Firestore query the app makes (single-field ones only, so production
needs no composite indexes), the credit transaction, and the newer collections:
albums, subscriptions (with count aggregation) and listening sessions. Cloud
Storage isn't emulated; GcpStore's file methods are left out.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import traceback
import types
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.store import InsufficientCredits  # noqa: E402
from app.store.local import LocalStore  # noqa: E402


async def contract(store, tag: str) -> list[str]:
    """Runs every check; returns the names of the ones that failed."""
    run = uuid.uuid4().hex[:8]          # fresh ids, so emulator reruns don't collide
    u1, u2, u3 = f"u1-{run}", f"u2-{run}", f"u3-{run}"
    s1, s2, s3 = f"s1-{run}", f"s2-{run}", f"s3-{run}"
    a1, a2 = f"a1-{run}", f"a2-{run}"
    failed: list[str] = []

    async def check(name: str, fn) -> None:
        try:
            await fn()
            print(f"  PASS {name}")
        except Exception:
            failed.append(name)
            print(f"  FAIL {name}")
            traceback.print_exc()

    # ---- users and credits ----
    async def users():
        now = time.time()
        for uid, email in ((u1, f"ann-{run}@x.com"), (u2, f"bo-{run}@x.com"), (u3, f"cy-{run}@x.com")):
            assert await store.create_user({"uid": uid, "email": email, "name": uid, "credits": 0, "created_at": now})
        assert not await store.create_user({"uid": u1, "email": "again@x.com", "name": "x", "credits": 0})
        await store.update_user(u1, {"feed_seen_at": 123.5})
        assert (await store.get_user(u1))["feed_seen_at"] == 123.5
        assert (await store.find_user_by_email(f"bo-{run}@x.com"))["uid"] == u2
        assert await store.adjust_credits(u1, 10, reason="grant") == 10
        assert await store.adjust_credits(u1, -4, reason="pod", show_id=s1) == 6
        try:
            await store.adjust_credits(u1, -7, reason="pod")
            raise AssertionError("overspent")
        except InsufficientCredits:
            pass
        assert [e["amount"] for e in await store.ledger(u1)] == [-4, 10]
        await store.adjust_credits(u1, 1, reason="refund", show_id=s1)
        assert sorted(e["amount"] for e in await store.ledger_for_show(u1, s1)) == [-4, 1]
        assert await store.ledger_for_show(u1, s2) == [] and await store.ledger_for_show(u2, s1) == []
    await check("users, credit transaction, ledger, a show's ledger entries", users)

    async def items():
        await store.put_item(u1, "personas", {"id": "p1", "name": "Maya", "context": "secret " * 2000, "created_at": 1})
        await store.put_item(u1, "personas", {"id": "p2", "name": "Ben", "created_at": 2})
        names = [i["name"] for i in await store.list_items(u1, "personas")]
        assert names == ["Maya", "Ben"], names
        await store.delete_item(u1, "personas", "p2")
        assert len(await store.list_items(u1, "personas")) == 1 and await store.list_items(u2, "personas") == []
    await check("personas with large private notes", items)

    async def edges():
        await store.put_edge(u1, u2, {"uid": u2, "status": "connected"})
        assert (await store.get_edge(u1, u2))["status"] == "connected" and await store.get_edge(u2, u1) is None
        assert [e["uid"] for e in await store.get_edges(u1)] == [u2]
        await store.delete_edge(u1, u2)
        assert await store.get_edges(u1) == []
    await check("connections", edges)

    # ---- shows and their queries ----
    async def shows():
        await store.create_show({"id": s1, "owner_uid": u1, "visibility": "public", "status": "finished",
                                 "created_at": 3.0, "album_ids": [a1], "shared_with": []})
        await store.create_show({"id": s2, "owner_uid": u1, "visibility": "private", "status": "live",
                                 "created_at": 2.0, "album_ids": [a1, a2], "shared_with": [u2]})
        await store.create_show({"id": s3, "owner_uid": u3, "visibility": "connections", "status": "finished",
                                 "created_at": 1.0, "album_ids": [], "shared_with": []})
        await store.update_show(s1, {"title": "Hello", "album_ids": [a1]})
        assert (await store.get_show(s1))["title"] == "Hello"
        ids = lambda docs: [d["id"] for d in docs if d["id"] in {s1, s2, s3}]  # noqa: E731
        assert ids(await store.list_shows(owner_uid=u1)) == [s1, s2]
        assert ids(await store.list_shows(album_id=a1)) == [s1, s2]
        assert ids(await store.list_shows(album_id=a2)) == [s2]
        assert ids(await store.list_shows(shared_with=u2)) == [s2]
        assert ids(await store.list_shows(owner_in=[u1, u3], visibility="connections")) == [s3]
        assert s1 in ids(await store.list_shows(visibility="public")) and s2 not in ids(await store.list_shows(visibility="public"))
        assert ids(await store.shows_with_status("live")) == [s2]
    await check("shows: owner, album, shared, connections, public and status queries", shows)

    async def children():
        await store.put_segment(s1, {"seq": 2, "lines": []})
        await store.put_segment(s1, {"seq": 1, "lines": []})
        await store.put_comment(s1, {"id": 1, "text": "hi"})
        await store.put_context(s1, {"id": 1, "name": "note"})
        kids = await store.load_children(s1)
        assert [s["seq"] for s in kids["segments"]] == [1, 2] and kids["comments"][0]["text"] == "hi"
    await check("segments, call-ins, context", children)

    async def sessions():
        await store.put_session(s1, {"id": "l-1", "kind": "live", "uid": u2, "joined_at": 5.0, "listened_s": 30})
        await store.put_session(s1, {"id": "r-1", "kind": "replay", "uid": None, "joined_at": 9.0, "leave_pos": 10})
        await store.put_session(s1, {"id": "r-1", "leave_pos": 40, "completed": True})   # merges
        r = await store.get_session(s1, "r-1")
        assert r["kind"] == "replay" and r["leave_pos"] == 40 and r["completed"] and r["joined_at"] == 9.0
        assert [x["id"] for x in await store.list_sessions(s1)] == ["l-1", "r-1"]
        assert await store.get_session(s1, "nope") is None
    await check("listening sessions: upsert merges, list in join order", sessions)

    # ---- albums and subscriptions ----
    async def albums():
        await store.put_album({"id": a1, "owner_uid": u1, "title": "One", "visibility": "public", "created_at": 1.0})
        await store.put_album({"id": a2, "owner_uid": u1, "title": "Two", "visibility": "private", "created_at": 2.0})
        assert [a["id"] for a in await store.list_albums(owner_uid=u1)] == [a2, a1]
        assert a1 in [a["id"] for a in await store.list_albums(visibility="public")]
        assert a2 not in [a["id"] for a in await store.list_albums(visibility="public")]
        await store.put_album({**(await store.get_album(a1)), "title": "Uno"})
        assert (await store.get_album(a1))["title"] == "Uno"
        await store.delete_album(a2)
        assert await store.get_album(a2) is None
    await check("albums: by owner, public, update, delete", albums)

    async def subscriptions():
        for sub_uid, kind, target in ((u2, "creator", u1), (u3, "creator", u1), (u2, "album", a1)):
            await store.put_subscription({"id": f"{sub_uid}__{kind}__{target}", "subscriber_uid": sub_uid, "kind": kind,
                                          "target_id": target, "target": f"{kind}:{target}", "at": time.time()})
        await store.put_subscription({"id": f"{u2}__creator__{u1}", "subscriber_uid": u2, "kind": "creator",
                                      "target_id": u1, "target": f"creator:{u1}", "at": time.time()})  # idempotent
        assert await store.count_subscribers(f"creator:{u1}") == 2
        assert await store.count_subscribers(f"album:{a1}") == 1
        assert {s["kind"] for s in await store.list_subscriptions(u2)} == {"creator", "album"}
        await store.delete_subscription(f"{u3}__creator__{u1}")
        assert await store.count_subscribers(f"creator:{u1}") == 1
        assert await store.count_subscribers(f"creator:nobody-{run}") == 0
    await check("subscriptions: idempotent, counted, removable", subscriptions)

    # ---- the listener timeline a live show saves (Firestore takes no arrays of arrays) ----
    async def timeline():
        from app.stats import timeline_doc, timeline_pairs
        await store.update_show(s3, {"listener_timeline": timeline_doc([[0, 1], [10, 2]])})
        assert timeline_pairs((await store.get_show(s3))["listener_timeline"]) == [[0, 1], [10, 2]]
        assert timeline_pairs([[0, 1]]) == [[0, 1]]          # older local saves still read
    await check("a show's listener timeline saves and reads back", timeline)

    # ---- personal briefings ----
    async def briefings():
        now = time.time()
        await store.update_user(u1, {"briefing_due_at": now - 60, "briefing_schedule": {"enabled": True}})
        await store.update_user(u2, {"briefing_due_at": now + 3600})
        await store.update_user(u3, {"briefing_due_at": None})
        due = [u["uid"] for u in await store.users_due_for_briefing(now, limit=500) if u["uid"] in {u1, u2, u3}]
        assert due == [u1], due
        b1, b2 = f"b1-{run}", f"b2-{run}"
        await store.create_show({"id": b1, "kind": "briefing", "owner_uid": u1, "expires_at": now - 5, "created_at": now})
        await store.create_show({"id": b2, "kind": "briefing", "owner_uid": u1, "expires_at": now + 999, "created_at": now})
        expired = [d["id"] for d in await store.expired_shows(now, limit=500) if d["id"] in {b1, b2, s1, s2, s3}]
        assert expired == [b1], expired
        await store.put_item(u1, "watches", {"id": "w1", "url": "https://a.example", "pending": [{"title": "x"}],
                                             "created_at": 1})
        await store.put_item(u1, "watch_pages", {"id": "w1", "paras": [{"h": "abc", "t": "Pro: $25"}]})
        await store.put_item(u1, "briefing_templates", {"id": "t1", "name": "Mine", "created_at": 1})
        assert [w["pending"] for w in await store.list_items(u1, "watches")] == [[{"title": "x"}]]
        assert (await store.list_items(u1, "watch_pages"))[0]["paras"] == [{"h": "abc", "t": "Pro: $25"}]
        await store.delete_item(u1, "watches", "w1")
        assert await store.list_items(u1, "watches") == [] and len(await store.list_items(u1, "briefing_templates")) == 1
        await store.delete_show(b1)
        await store.delete_show(b2)
    await check("briefings: due schedules, expired briefings, watches and templates", briefings)

    # ---- lifecycle ----
    async def lifecycle():
        assert await store.mark_interrupted({s1, s3}) >= 1
        assert (await store.get_show(s2))["status"] == "interrupted"
        await store.delete_show(s1)
        assert await store.get_show(s1) is None and await store.list_sessions(s1) == []
    await check("restart marks live shows interrupted; delete removes sessions too", lifecycle)

    print(f"{tag}: {'all passed' if not failed else f'{len(failed)} failed'}")
    return failed


def gcp_store_on_emulator():
    """GcpStore wired to the Firestore emulator; Cloud Storage stubbed (it isn't emulated)."""
    from google.cloud import firestore

    from app.store.gcp import GcpStore
    store = GcpStore.__new__(GcpStore)
    store.db = firestore.AsyncClient(project="demo-podcasts")
    store.gcs = types.SimpleNamespace(list_blobs=lambda bucket, prefix: [])
    store.bucket = None
    store._signed = {}
    return store


async def main() -> int:
    failed = await contract(LocalStore(tempfile.mkdtemp(prefix="store-contract-")), "LocalStore")
    if os.environ.get("FIRESTORE_EMULATOR_HOST"):
        failed += await contract(gcp_store_on_emulator(), f"GcpStore (Firestore emulator at {os.environ['FIRESTORE_EMULATOR_HOST']})")
    else:
        print("GcpStore: skipped (set FIRESTORE_EMULATOR_HOST to run it against the Firestore emulator)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
