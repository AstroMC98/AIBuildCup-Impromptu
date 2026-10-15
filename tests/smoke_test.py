"""End-to-end check of accounts and live pods against tests/fake_gemini.py.

    python -m tests.smoke_test

Starts the stub and the app (local storage in a temp dir, dev sign-in, the live
clock at 20x) and plays four people:
  alice  an admin with no credits at first   bob    alice's connection, free account
  carol  signed in, not connected             anon   signed out
It checks:
  - creating needs an account and credits; admins grant credits; free accounts can't create
  - personas and styles are saved per user, and a persona reaches the hosts
  - a persona's voice must match its gender; its private notes reach the writers, never listeners
  - albums (playlists) respect who can see them and each pod in them; tags are cleaned up
  - subscriptions to creators and albums feed a Subscriptions list with "new" badges
  - stats: who listened live and on replay, for the owner only
  - the producer interrupts: unheard audio is dropped, a reaction plays, the hosts
    answer in a detour, a steer re-plans the rest; listeners can't interrupt
  - connection requests are accepted; a "connections" pod is visible to bob, not carol or anon
  - bob calls in (under his account name) and the hosts answer; rude call-ins are declined;
    bursts are rate limited; bob can't restyle; alice and bob add context the hosts use
  - restyle, reactions, a late joiner and a reconnect, contiguous audio
  - the pod signs off, is saved, replays with Range requests, unused credits are refunded
  - publishing makes it public for anon; visitors on a public live pod can't call in
  - drafts, deleting, and a pod with nobody listening ending by itself
  - every live pod opens on a theme from the music library, before the first segment
  - voice memos: a saved show intro; bad, oversized and unaffordable uploads refused; a
    one-shot memo hidden until it's made, then researched, scored and timed to the sample;
    a reviewed memo made with the owner's edits, Lyria failing and its credits coming
    back; deleting a memo pod mid-way refunds it
Then alice co-hosts a call live with an AI co-host (the stub's Live API), streaming
mic audio in real time:
  - starting needs credits, one call at a time; others can't see or join it while it's on
  - a newer connection takes the call over; the co-host opens the show itself
  - your words are transcribed; cutting in stops it; mode switches reach it; a recycled
    Live connection resumes the session
  - hanging up edits it (flubs cut, the answer to them dropped, cut-off answers handled)
    and saves a finished pod with both voices, chapters, an MP3 and the unedited call
  - unused minutes come back; a call nobody joined leaves no pod and refunds everything
"""
import asyncio
import json
import re
import math
import os
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import numpy as np
import websockets
from websockets.exceptions import ConnectionClosed

from app.audio import Mp3Writer
from app.mixer import INTRO_LEAD_S, LIVE_INTRO_S, OUTRO_PLAYOUT_S, STING_S

# SMOKE_PORT moves both servers (the stub on it, the app on the next port), so two
# checkouts can run the smoke test at the same time.
FAKE_PORT = int(os.environ.get("SMOKE_PORT", "8765"))
APP_PORT = FAKE_PORT + 1
BASE = f"http://127.0.0.1:{APP_PORT}"
WS = f"ws://127.0.0.1:{APP_PORT}"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
WEBM = b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\x82\x84webm" + b"\x00" * 2000   # what Chrome records
PERSONA = "A precise neuroscientist who loves mechanisms and practical protocols."
RATE = 24_000


def music_library(root: Path) -> Path:
    """A one-pack music library: a 30 s chord, enough for every pod's theme."""
    t = np.arange(30 * RATE) / RATE
    chord = sum(np.sin(2 * np.pi * f * t) for f in (220, 277, 330)) / 3
    writer = Mp3Writer(RATE, 64)
    writer.feed(np.rint(chord * 0.3 * 32767).astype("<i2").tobytes())
    root.mkdir(parents=True, exist_ok=True)
    (root / "theme.mp3").write_bytes(writer.finish())
    (root / "manifest.json").write_text(json.dumps({"version": 1, "packs": [
        {"id": "warm-test", "label": "Warm test", "mood": "warm", "file": "theme.mp3"}]}))
    return root


async def wait_for_status(client: httpx.AsyncClient, show_id: str, statuses: set[str], timeout: float = 120) -> dict:
    deadline = time.time() + timeout
    data: dict = {}
    while time.time() < deadline:
        data = (await client.get(f"/api/shows/{show_id}")).json()
        if data["show"]["status"] in statuses:
            return data
        await asyncio.sleep(0.5)
    raise AssertionError(f"memo pod {show_id} never got to {statuses}: {data.get('show', {}).get('render')}")


def _start(cmd: list[str], env: dict, log_path: Path) -> subprocess.Popen:
    # Log to a file, not a pipe: an unread pipe fills up and freezes the server.
    log = open(log_path, "wb")
    return subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT)


def _wait_up(url: str, timeout: float = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            httpx.get(url, timeout=1)
            return
        except httpx.HTTPError:
            time.sleep(0.2)
    raise RuntimeError(f"{url} never came up")


class Listener:
    """A WebSocket client that records every event and audio frame."""

    def __init__(self, label: str, url: str, cookie: str | None = None):
        self.label, self.url = label, url
        self.headers = {"Cookie": f"gp_session={cookie}"} if cookie else None
        self.events: list[dict] = []
        self.frames: list[tuple[int, int, int]] = []   # (seq, start_sample, samples)
        self._frames_at: list[int] = []                # frames received before each event
        self.close_code: int | None = None
        self.ws = None
        self._task: asyncio.Task | None = None

    async def connect(self, join: bool = True, have: tuple[int, int] | None = None) -> "Listener":
        self.ws = await websockets.connect(self.url, additional_headers=self.headers, max_size=None)
        if join:
            extra = {"have_from": have[0], "have_to": have[1]} if have else {}
            await self.send({"type": "join", **extra})
            await self.send({"type": "player", "playing": True})
        self._task = asyncio.create_task(self._recv())
        return self

    async def send(self, obj: dict) -> None:
        await self.ws.send(json.dumps(obj))

    async def _recv(self) -> None:
        try:
            async for msg in self.ws:
                if isinstance(msg, bytes):
                    assert (len(msg) - 8) % 2 == 0, "PCM frame split a sample"
                    seq, start = struct.unpack("<II", msg[:8])
                    self.frames.append((seq, start, (len(msg) - 8) // 2))
                else:
                    self._frames_at.append(len(self.frames))
                    self.events.append(json.loads(msg))
        except ConnectionClosed as exc:
            self.close_code = exc.rcvd.code if exc.rcvd else None
        if self.close_code is None and self.ws.close_code is not None:
            self.close_code = self.ws.close_code

    def of(self, kind: str) -> list[dict]:
        return [e for e in self.events if e["type"] == kind]

    def frames_after(self, event: dict) -> list[tuple[int, int, int]]:
        """The audio frames that arrived after this event."""
        i = next(i for i, e in enumerate(self.events) if e is event)
        return self.frames[self._frames_at[i]:]

    async def until(self, pred, timeout: float = 60, what: str = "") -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pred():
                return
            if self._task and self._task.done():
                break
            await asyncio.sleep(0.05)
        if not pred():
            segs = self.of("segment")
            status = [e["state"] for e in self.of("status")][-3:]
            tick = (self.of("tick") or [{}])[-1]
            raise AssertionError(
                f"{self.label}: timed out waiting for {what}. {len(segs)} segments "
                f"(beats {[s['beat'] for s in segs]}, final {[s['final'] for s in segs][-1:]}), "
                f"status {status}, last tick {tick}, close {self.close_code}, "
                f"errors {[e.get('message') for e in self.of('error')]}"
            )

    async def close(self) -> None:
        try:
            await self.ws.close()
        except Exception:
            pass
        if self._task:
            await asyncio.wait([self._task], timeout=5)


async def closed_with(url: str, cookie: str | None = None) -> int | None:
    lst = Listener("probe", url, cookie)
    await lst.connect(join=False)
    await lst.until(lambda: lst.close_code is not None, timeout=10, what="close")
    return lst.close_code


def contiguous(frames: list[tuple[int, int, int]]) -> bool:
    return all(b[1] == a[1] + a[2] for a, b in zip(frames, frames[1:]))


MIC_FRAME = 640   # 40 ms at 16 kHz
TALK = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 220 * i / 16_000))) for i in range(MIC_FRAME))
HUSH = b"\x00\x00" * MIC_FRAME


class Phone(Listener):
    """A phone on a co-host call: streams its mic in real time (talking or quiet),
    and counts the co-host's audio per turn (frames are <turn:u32> + PCM)."""

    def __init__(self, label: str, url: str, cookie: str | None = None):
        super().__init__(label, url, cookie)
        self.talking = False
        self.turn_samples: dict[int, int] = {}
        self._mic: asyncio.Task | None = None

    async def connect(self, join: bool = True, have=None) -> "Phone":
        self.ws = await websockets.connect(self.url, additional_headers=self.headers, max_size=None)
        if join:
            await self.send({"type": "join"})
        self._task = asyncio.create_task(self._recv())
        return self

    def start_mic(self) -> None:
        self._mic = asyncio.create_task(self._mic_loop())

    async def _mic_loop(self) -> None:
        start, n = time.monotonic(), 0
        try:
            while True:
                await self.ws.send(TALK if self.talking else HUSH)
                n += 1
                await asyncio.sleep(max(0.0, start + n * 0.04 - time.monotonic()))
        except (ConnectionClosed, asyncio.CancelledError):
            pass

    async def say(self, seconds: float) -> None:
        self.talking = True
        await asyncio.sleep(seconds)
        self.talking = False

    def done(self, turn: int) -> bool:
        return any(e["turn"] == turn for e in self.of("turn_done"))

    async def _recv(self) -> None:
        try:
            async for msg in self.ws:
                if isinstance(msg, bytes):
                    assert (len(msg) - 4) % 2 == 0, "PCM frame split a sample"
                    turn = struct.unpack("<I", msg[:4])[0]
                    self.turn_samples[turn] = self.turn_samples.get(turn, 0) + (len(msg) - 4) // 2
                else:
                    self.events.append(json.loads(msg))
        except ConnectionClosed as exc:
            self.close_code = exc.rcvd.code if exc.rcvd else None
        if self.close_code is None and self.ws.close_code is not None:
            self.close_code = self.ws.close_code

    async def close(self) -> None:
        if self._mic:
            self._mic.cancel()
        await super().close()


async def sign_in(client: httpx.AsyncClient, email: str, name: str) -> dict:
    r = await client.post("/api/auth/dev", json={"email": email, "name": name})
    r.raise_for_status()
    return r.json()["me"]


async def scenario(checks: dict) -> None:
    def request(**over) -> dict:
        return {
            "source": "Genesis 1-3", "style": "girl gossip", "vibe": "gossip",
            "hosts": [{"name": "Dr. Vance", "voice": "Kore", "persona": PERSONA}, {"name": "Brit", "voice": "auto"}],
            "minutes": 2, "language": "English", "source_names": ["Genesis 1-3.pdf"], **over,
        }

    clients = [httpx.AsyncClient(base_url=BASE, timeout=30) for _ in range(4)]
    alice_c, bob_c, carol_c, anon = clients
    try:
        opts = (await anon.get("/api/options")).json()
        opts_gender = {v["name"]: v["gender"] for v in opts["voices"]}
        checks["signed out: options, no 'me'"] = opts["me"] is None and opts["auth"]["mode"] == "dev" and len(opts["templates"]) >= 6
        checks["signed out can't create"] = (await anon.post("/api/shows", json={"request": request()})).status_code == 401

        alice = await sign_in(alice_c, "alice@example.com", "Alice")
        bob = await sign_in(bob_c, "bob@example.com", "Bob")
        carol = await sign_in(carol_c, "carol@example.com", "Carol")
        checks["accounts: alice is admin, nobody has credits"] = alice["admin"] and not bob["admin"] and alice["credits"] == bob["credits"] == 0

        r = await alice_c.post("/api/shows", json={"request": request(), "live": True})
        checks["no credits -> 402"] = r.status_code == 402
        checks["non-admin can't grant"] = (await bob_c.post(f"/api/admin/users/{bob['uid']}/credits", json={"amount": 50})).status_code == 403
        r = await alice_c.post(f"/api/admin/users/{alice['uid']}/credits", json={"amount": 100, "note": "demo"})
        checks["admin grants credits"] = r.status_code == 200 and r.json()["credits"] == 100
        checks["free account can't create a draft either"] = (await bob_c.post("/api/shows", json={"request": request(), "live": False})).status_code == 402

        # personas and styles
        p = await alice_c.post("/api/personas", json={"name": "Dr. Vance", "persona": PERSONA, "voice": "Kore"})
        s = await alice_c.post("/api/styles", json={"name": "Cozy science", "prompt": "Warm and nerdy.", "accent": "#1F7F6A"})
        await alice_c.put(f"/api/styles/{s.json()['id']}", json={"name": "Cozy science", "prompt": "Warm, nerdy, kind.", "accent": "#1F7F6A"})
        bad = await alice_c.post("/api/personas", json={"name": "", "persona": "x"})
        wrong_voice = await alice_c.post("/api/personas", json={"name": "Max", "persona": "x", "voice": "Kore", "gender": "male"})
        guessed = await alice_c.post("/api/personas", json={"name": "Leo", "persona": "x", "voice": "Puck", "likes": "jazz"})
        checks["persona voice must match its gender"] = wrong_voice.status_code == 422 and guessed.json()["gender"] == "male"
        await alice_c.delete(f"/api/personas/{guessed.json()['id']}")
        personas = (await alice_c.get("/api/personas")).json()["items"]
        styles = (await alice_c.get("/api/styles")).json()["items"]
        checks["personas and styles saved per user"] = (
            p.status_code == 200 and bad.status_code == 422 and len(personas) == 1 and styles[0]["prompt"] == "Warm, nerdy, kind."
            and (await bob_c.get("/api/personas")).json()["items"] == [])

        # connections
        r = await alice_c.post("/api/connections", json={"email": "bob@example.com"})
        incoming = (await bob_c.get("/api/connections")).json()["incoming"]
        accept = await bob_c.post(f"/api/connections/{alice['uid']}/accept")
        conns = (await alice_c.get("/api/connections")).json()
        checks["connection request and accept"] = (
            r.json()["status"] == "pending_out" and [e["uid"] for e in incoming] == [alice["uid"]]
            and accept.json()["status"] == "connected" and [e["uid"] for e in conns["connected"]] == [bob["uid"]])
        checks["unknown email -> 404"] = (await alice_c.post("/api/connections", json={"email": "nobody@x.com"})).status_code == 404

        # albums
        album = (await alice_c.post("/api/albums", json={"title": "Bible gossip", "visibility": "public"})).json()
        secret = (await alice_c.post("/api/albums", json={"title": "Drafts", "visibility": "private"})).json()
        checks["albums: private album hidden from others"] = (
            (await anon.get(f"/api/albums/{secret['id']}")).status_code == 404
            and (await bob_c.get(f"/api/albums/{secret['id']}")).status_code == 404)

        # subscriptions
        r1 = await bob_c.post("/api/subscriptions", json={"kind": "creator", "id": alice["uid"]})
        r2 = await bob_c.post("/api/subscriptions", json={"kind": "album", "id": album["id"]})
        await carol_c.post("/api/subscriptions", json={"kind": "creator", "id": alice["uid"]})
        checks["subscribe to a creator and an album"] = (
            r1.json()["subscribers"] == 1 and r2.json()["subscribers"] == 1
            and (await alice_c.post("/api/subscriptions", json={"kind": "creator", "id": alice["uid"]})).status_code == 422
            and len((await bob_c.get("/api/subscriptions")).json()["subscriptions"]) == 2)

        # the main pod: visible to connections, in the public album; host 1 brings private notes
        secret_host = {"name": "Dr. Vance", "voice": "Kore", "persona": PERSONA,
                       "context": "SECRET_NOTE: Dr. Vance grew up in a lighthouse."}
        r = await alice_c.post("/api/shows", json={
            "request": request(hosts=[secret_host, {"name": "Brit", "voice": "auto"}]), "live": True,
            "visibility": "connections", "template": "girl-talk",
            "album_ids": [album["id"], "not-mine"], "tags": ["Bible", "bible", "  gossip  ", "#Genesis"]})
        show_id = r.json()["id"]
        checks["going live charges credits"] = r.json()["credits"] == 98
        draft = (await alice_c.post("/api/shows", json={"request": request(minutes=5), "live": False})).json()
        idle = (await alice_c.post("/api/shows", json={"request": request(), "live": True})).json()

        alice_cookie, bob_cookie, carol_cookie = (c.cookies.get("gp_session") for c in (alice_c, bob_c, carol_c))
        url = f"{WS}/ws/show/{show_id}"
        checks["signed out ws -> 4401"] = await closed_with(url) == 4401
        checks["not connected ws -> 4403"] = await closed_with(url, carol_cookie) == 4403
        checks["not connected api -> 404"] = (await carol_c.get(f"/api/shows/{show_id}")).status_code == 404
        shared = (await bob_c.get("/api/shows", params={"scope": "shared"})).json()["shows"]
        checks["connection sees it under 'shared'"] = [s["id"] for s in shared] == [show_id]

        prod = await Listener("alice", url, alice_cookie).connect()
        guest = await Listener("bob", url, bob_cookie).connect()
        await prod.until(lambda: prod.of("snapshot"), what="snapshot")
        await guest.until(lambda: guest.of("snapshot"), what="bob snapshot")
        checks["roles"] = prod.of("snapshot")[0]["role"] == "producer" and guest.of("snapshot")[0]["role"] == "listener"

        # organizing, albums and the feed (the listeners are in, so the pod plays on meanwhile)
        org = await alice_c.patch(f"/api/shows/{draft['id']}/organize", json={"album_ids": [album["id"]], "tags": ["later"]})
        checks["organize: owner files a pod, nobody else can"] = (
            org.json() == {"album_ids": [album["id"]], "tags": ["later"]}
            and (await bob_c.patch(f"/api/shows/{show_id}/organize", json={"tags": ["x"]})).status_code in {403, 404})
        mine_list = {s_["id"]: s_ for s_ in (await alice_c.get("/api/shows")).json()["shows"]}
        checks["tags cleaned, foreign album ids dropped"] = (
            mine_list[show_id]["tags"] == ["Bible", "gossip", "Genesis"] and mine_list[show_id]["album_ids"] == [album["id"]])
        anon_album = (await anon.get(f"/api/albums/{album['id']}")).json()
        bob_album = (await bob_c.get(f"/api/albums/{album['id']}")).json()
        checks["album shows each viewer only the pods they can hear"] = (
            anon_album["shows"] == [] and [x["id"] for x in bob_album["shows"]] == [show_id]
            and bob_album["subscribed"] and (await alice_c.get(f"/api/albums/{album['id']}")).json()["album"]["pods"] == 2)
        bob_feed = (await bob_c.get("/api/feed")).json()
        carol_feed = (await carol_c.get("/api/feed")).json()
        await bob_c.post("/api/feed/seen")
        checks["feed: subscribed pods you can hear, marked new until seen"] = (
            [x["id"] for x in bob_feed["shows"]] == [show_id] and bob_feed["shows"][0]["new"] and bob_feed["unseen"] == 1
            and (await bob_c.get("/api/feed")).json()["unseen"] == 0 and carol_feed["shows"] == [])

        await guest.until(lambda: guest.of("segment"), what="first segment")
        await guest.send({"type": "comment", "name": "Mallory", "text": "Is Eve the original main character?", "client_id": "c1"})
        await guest.send({"type": "comment", "text": "DECLINE_ME you are awful", "client_id": "c2"})
        await guest.send({"type": "comment", "text": "Third!", "client_id": "c3"})
        await guest.send({"type": "comment", "text": "Fourth!", "client_id": "c4"})
        await guest.until(lambda: any(e.get("code") == "rate_limited" for e in guest.of("error")), what="rate limit")
        checks["comment burst rate limited"] = True
        await guest.send({"type": "restyle", "vibe": "sports"})
        await guest.until(lambda: any(e.get("code") == "forbidden" for e in guest.of("error")), what="forbidden")
        checks["listener can't restyle"] = True

        r = await alice_c.post(f"/api/shows/{show_id}/context", data={
            "kind": "text", "when": "now", "cite": "true", "text": "Genesis 4: Cain and Abel, the first sibling feud.",
        })
        ctx_id = r.json()["context"]["id"] if r.status_code == 200 else -1
        r = await bob_c.post(f"/api/shows/{show_id}/context", data={
            "kind": "photo", "when": "now", "cite": "true", "comment": "Here's the family tree, use it", "client_id": "a1",
        }, files={"file": ("Family tree.png", PNG, "image/png")})
        checks["listener photo call-in accepted"] = r.status_code == 200 and r.json()["comment"]["context_id"] is not None
        photo_id = r.json()["context"]["id"] if r.status_code == 200 else -1
        r = await bob_c.post(f"/api/shows/{show_id}/context", data={"kind": "link", "url": "http://127.0.0.1:22/"})
        checks["private link refused"] = r.status_code in {422, 429}
        checks["carol can't add context"] = (await carol_c.post(f"/api/shows/{show_id}/context", data={"kind": "text", "text": "hi"})).status_code == 404

        await guest.send({"type": "react", "n": 3})
        await prod.send({"type": "restyle", "vibe": "sports"})

        await prod.until(lambda: len(prod.of("segment")) >= 4, what="4 segments")
        late = await Listener("late", url, bob_cookie).connect()
        await late.until(lambda: late.of("snapshot") and late.frames, what="late join")
        blip = await Listener("blip", url, bob_cookie).connect()
        await blip.until(lambda: len(blip.frames) >= 3, what="blip frames")
        await blip.close()
        held = (blip.frames[0][1], blip.frames[-1][1] + blip.frames[-1][2])
        back = await Listener("back", url, bob_cookie).connect(have=held)
        await back.until(lambda: back.of("snapshot") and back.frames, what="resume")
        checks["reconnect resumes where it left off"] = (
            back.of("snapshot")[0]["backfill_from"] == held[1] and back.frames[0][1] == held[1])
        await back.close()

        await prod.until(lambda: prod.of("saved"), timeout=120, what="show saved")
        for lst in (prod, guest, late):
            await lst.close()

        # Segments committed before the producer's socket joined (the cold open can be)
        # came in its snapshot rather than as events.
        early = prod.of("snapshot")[0]["segments"]
        segs = sorted({s["seq"]: s for s in early + prod.of("segment")}.values(), key=lambda s: s["seq"])
        comments = {c["id"]: c for c in prod.of("comment")}
        by_client = {c["client_id"]: c for c in comments.values()}
        contexts = {x["id"]: x for x in prod.of("context")}
        ends = {s["seq"]: s["samples"] for s in early if s.get("samples") is not None}
        ends.update({e["seq"]: e["samples"] for e in prod.of("segment_end")})
        first_comment_seq = min((s["seq"] for s in segs if by_client.get("c1", {}).get("id") in s["addressed"]), default=0)

        checks["call-in answered in a later segment"] = first_comment_seq >= 2
        checks["call-in uses the account name"] = by_client.get("c1", {}).get("name") == "Bob" and any(
            "Bob called in" in ln["text"] for s in segs for ln in s["lines"])
        checks["rude call-in declined"] = by_client.get("c2", {}).get("status") == "declined"
        checks["producer context used"] = contexts.get(ctx_id, {}).get("status") == "used"
        checks["photo context used"] = contexts.get(photo_id, {}).get("status") == "used"
        seen = httpx.get(f"http://127.0.0.1:{FAKE_PORT}/_seen").json()["writer_systems"]
        checks["context in writer system prompt"] = any("first sibling feud" in s for s in seen)
        checks["persona notes reach the writers"] = any("Dr. Vance's own notes" in s and "SECRET_NOTE" in s for s in seen)
        plan_hosts = (prod.of("plan") or [e["plan"] for e in prod.of("snapshot") if e.get("plan")])[0]["hosts"]
        checks["every host's voice matches their gender"] = all(
            h["gender"] in {"female", "male"} and opts_gender[h["voice"]] == h["gender"] for h in plan_hosts
        ) and plan_hosts[0]["voice"] != plan_hosts[1]["voice"]
        checks["persona reaches the hosts"] = any(re.search(rf"Dr\. Vance \((female|male)\): {re.escape(PERSONA)}", s) for s in seen)
        checks["style last in system prompt"] = any(s.index("SOURCE:") < s.rindex("STYLE:") for s in seen)
        restyled = [s for s in segs if s["style_rev"] == 1]
        checks["restyle acknowledged"] = bool(restyled) and any(
            "STYLE SWITCH ACKNOWLEDGED" in ln["text"] for s in restyled for ln in s["lines"])
        checks["reactions broadcast"] = any(e["total"] >= 3 for e in prod.of("react"))
        checks["segments contiguous"] = all(b["start"] == a["start"] + ends.get(a["seq"], -1) for a, b in zip(segs, segs[1:]))
        checks["producer frames contiguous"] = contiguous(prod.frames) and prod.frames[0][1] == 0
        snap = late.of("snapshot")[0]
        checks["late join starts at a segment start"] = late.frames[0][1] == snap["backfill_from"] and (
            snap["backfill_from"] in {0} | {s["start"] for s in snap["segments"]}   # 0: still in the intro theme
        ) and snap["backfill_from"] <= snap["clock"]["live"]
        checks["late frames contiguous"] = contiguous(late.frames)
        checks["the planner's cold open is voiced first"] = segs[0]["lines"][0]["text"].startswith("COLD OPEN")
        checks["ended then saved"] =[e["type"] for e in prod.events if e["type"] in {"ended", "saved"}] == ["ended", "saved"]

        # replay and access after the fact
        data = (await bob_c.get(f"/api/shows/{show_id}")).json()
        checks["replay data for the connection"] = (not data["live"] and data["show"]["status"] == "finished"
                                                    and len(data["segments"]) == len(segs) and data["show"]["has_audio"])
        checks["a live pod opens on its theme, then the hosts"] = (
            prod.frames[0][0] == 0 and data["segments"][0]["start"] == int(LIVE_INTRO_S * RATE)
            and data["show"]["music"]["source"] == "library")
        accents = {s["rev"]: s["accent"] for s in data["show"]["styles"]}
        checks["replay keeps each style's accent"] = accents.get(0) == "#B8336A" and accents.get(1) == "#B5521C"
        checks["anon can't replay a connections pod"] = (await anon.get(f"/api/shows/{show_id}")).status_code == 401
        bob_view = await bob_c.get(f"/api/shows/{show_id}")
        checks["persona notes never reach listeners"] = (
            "SECRET_NOTE" not in bob_view.text and bob_view.json()["show"]["request"]["hosts"][0]["has_notes"]
            and bob_view.json()["creator"]["subscribed"] and bob_view.json()["albums"][0]["id"] == album["id"])

        # stats: bob listened live; then replays a bit
        beacon = {"sid": "replay0001", "pos": 30, "played": 25, "lid": "bobbrowser1"}
        r_ok = await bob_c.post(f"/api/shows/{show_id}/listen", json=beacon)
        r_anon = await anon.post(f"/api/shows/{show_id}/listen", json={**beacon, "sid": "replay0002"})
        st = (await alice_c.get(f"/api/shows/{show_id}/stats")).json()["stats"]
        named = {x["name"]: x for x in st["people"]}
        checks["stats: live listeners named for the owner, replays counted"] = (
            r_ok.status_code == 200 and r_anon.status_code == 401 and st["live"]["unique"] >= 1
            and "Bob" in named and named["Bob"]["live"] and named["Bob"]["replay"]
            and st["replays"]["plays"] == 1 and "Alice" not in named and st["live"]["timeline"])
        checks["stats: owner only"] = (await bob_c.get(f"/api/shows/{show_id}/stats")).status_code == 403
        overview = (await alice_c.get("/api/me/stats")).json()
        checks["stats overview with subscriber counts"] = (
            any(r_["id"] == show_id and r_["listeners"] >= 1 for r_ in overview["pods"])
            and overview["subscribers"]["creator"] == 2
            and {a["id"]: a["subscribers"] for a in overview["subscribers"]["albums"]}[album["id"]] == 1)
        creator = (await anon.get(f"/api/creators/{alice['uid']}")).json()
        checks["creator page: public view, subscriber count"] = (
            creator["subscribers"] == 2 and creator["shows"] == [] and [a["id"] for a in creator["albums"]] == [album["id"]])
        audio = await bob_c.get(f"/api/shows/{show_id}/audio")
        checks["audio is mp3"] = audio.status_code == 200 and audio.headers["content-type"] == "audio/mpeg"
        ranged = await bob_c.get(f"/api/shows/{show_id}/audio", headers={"Range": "bytes=0-99"})
        checks["audio range -> 206"] = ranged.status_code == 206 and len(ranged.content) == 100
        async def credits_for(sid: str, reason: str) -> int:
            entries = (await alice_c.get("/api/me/ledger")).json()["entries"]
            return sum(e["amount"] for e in entries if e["reason"] == reason and e["show_id"] == sid)

        # Charged for the minutes asked; whatever wasn't voiced comes back.
        charged = -await credits_for(show_id, "pod")
        unused = max(0, charged - math.ceil(data["show"]["duration_s"] / 60))
        checks["refund matches unused minutes"] = charged > 0 and await credits_for(show_id, "refund") == unused

        # publishing
        r = await alice_c.patch(f"/api/shows/{show_id}", json={"visibility": "public"})
        public = (await anon.get("/api/shows", params={"scope": "public"})).json()["shows"]
        checks["publish -> public for everyone"] = (
            r.status_code == 200 and (await anon.get(f"/api/shows/{show_id}")).status_code == 200
            and (await carol_c.get(f"/api/shows/{show_id}")).status_code == 200 and show_id in [s["id"] for s in public])
        checks["only the owner can change sharing"] = (await bob_c.patch(f"/api/shows/{show_id}", json={"visibility": "private"})).status_code == 403
        r = await alice_c.patch(f"/api/shows/{show_id}", json={"visibility": "private", "shared_with": [carol["uid"], bob["uid"]]})
        checks["sharing by name is limited to connections"] = r.json()["shared_with"] == [bob["uid"]] and (
            await carol_c.get(f"/api/shows/{show_id}")).status_code == 404

        # a public live pod: visitors listen, can't call in
        pub = (await alice_c.post("/api/shows", json={"request": request(), "live": True, "visibility": "public"})).json()
        visitor = await Listener("anon", f"{WS}/ws/show/{pub['id']}").connect()
        await visitor.until(lambda: visitor.of("snapshot"), what="visitor snapshot")
        await visitor.send({"type": "comment", "text": "hello"})
        await visitor.until(lambda: any(e.get("code") == "sign_in" for e in visitor.of("error")), what="sign-in error")
        checks["visitor listens but can't call in"] = visitor.of("snapshot")[0]["role"] == "visitor"
        await visitor.close()
        checks["delete live pod"] = (await alice_c.delete(f"/api/shows/{pub['id']}")).status_code == 200

        # library, drafts, idle end
        mine = {s["id"]: s["status"] for s in (await alice_c.get("/api/shows")).json()["shows"]}
        checks["library lists pod and draft"] = mine.get(show_id) == "finished" and mine.get(draft["id"]) == "draft"
        checks["drafts are private to the owner"] = (await bob_c.get(f"/api/shows/{draft['id']}")).status_code == 404
        checks["delete draft"] = (await alice_c.delete(f"/api/shows/{draft['id']}")).status_code == 200 and (
            await alice_c.get(f"/api/shows/{draft['id']}")).status_code == 404
        deadline = time.time() + 90
        idle_doc = {}
        while time.time() < deadline:
            idle_doc = (await alice_c.get(f"/api/shows/{idle['id']}")).json()
            if not idle_doc.get("live"):
                break
            await asyncio.sleep(1)
        checks["idle pod signs off by itself"] = idle_doc.get("show", {}).get("ended_reason") == "idle"
        checks["idle pod refunds unused minutes"] = await credits_for(idle["id"], "refund") >= 1

        await memo_scenario(checks, alice_c, bob_c, anon, credits_for, opts)
        # interrupting: the producer cuts in, asks, then steers
        pod = (await alice_c.post("/api/shows", json={"request": request(minutes=5), "live": True,
                                                      "visibility": "connections"})).json()
        purl = f"{WS}/ws/show/{pod['id']}"
        host = await Listener("alice-int", purl, alice_cookie).connect()
        fan = await Listener("bob-int", purl, bob_cookie).connect()
        await host.until(lambda: len(host.of("segment")) >= 2, what="two segments before interrupting")
        await fan.send({"type": "player", "playing": False})       # only the producer is listening now
        await fan.send({"type": "interrupt", "kind": "ask", "text": "me too?", "at": 0})
        await fan.until(lambda: any(e.get("code") == "forbidden" for e in fan.of("error")), what="listener refused")
        checks["only the producer can interrupt"] = True
        await host.send({"type": "player", "playing": False})      # holding the talk button
        # Nobody's playing now, so the live clock is frozen: the next tick says exactly where.
        ticks = len(host.of("tick"))
        await host.until(lambda: len(host.of("tick")) > ticks, timeout=10, what="a tick after pausing")
        at = host.of("tick")[-1]["live"]
        await host.send({"type": "interrupt", "kind": "ask", "text": "why did the serpent talk?", "at": at})
        await host.until(lambda: any((e.get("interrupt") or {}).get("kind") == "ask" and "INTERJECTION" in e["lines"][0]["text"]
                                     for e in host.of("segment")), what="the hosts answer the question")
        truncs = host.of("truncate")
        detours = [e for e in host.of("segment") if e.get("branch") == "detour"]
        # Audio voiced ahead of the playhead may have arrived before the cut (a pod's
        # intro theme gives it a head start), so look at what came after the truncate.
        frames_after = host.frames_after(truncs[0]) if truncs else []
        checks["interrupt: rewind, a reaction, then the answer"] = (
            bool(truncs) and truncs[0]["at"] <= at + 1
            and detours[0]["lines"][0]["text"] == "REACTION ask!"
            and detours[0]["start"] == truncs[0]["at"] and "serpent" in detours[1]["lines"][0]["text"]
            and bool(frames_after) and frames_after[0][1] == truncs[0]["at"]
            and fan.of("truncate") and fan.of("truncate")[0]["at"] == truncs[0]["at"])
        await host.until(lambda: any(e["seq"] == detours[-1]["seq"] for e in host.of("segment_end")), what="answer voiced")
        await host.send({"type": "interrupt", "kind": "steer", "text": "argue the other side",
                         "at": (host.of("tick") or [{"live": 0}])[-1]["live"]})
        await host.until(lambda: any(any("STEERED" in b["title"] for b in e["beats"]) for e in host.of("plan")),
                         what="a steer re-plans the rest")
        checks["steer: the rest of the episode is re-planned"] = True
        r_talk = await alice_c.post(f"/api/shows/{pod['id']}/transcribe",
                                    files={"audio": ("q.webm", b"\x1aE\xdf\xa3fake", "audio/webm")})
        r_bob = await bob_c.post(f"/api/shows/{pod['id']}/transcribe",
                                 files={"audio": ("q.webm", b"\x1aE\xdf\xa3fake", "audio/webm")})
        checks["voice question transcribed, for the producer only"] = (
            r_talk.status_code == 200 and "serpent" in r_talk.json()["text"] and r_bob.status_code == 403)
        await host.send({"type": "end", "mode": "now"})
        await host.until(lambda: host.of("saved") or host.of("ended"), what="interrupted pod ends")
        for lst in (host, fan):
            await lst.close()
    finally:
        for c in clients:
            await c.aclose()


async def callins_scenario(checks: dict) -> None:
    """Call-ins the hosts promise to come back to are answered; with Research on,
    they look things up (the stub's Google Search) and cite it."""
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as alice_c:
        alice = await sign_in(alice_c, "alice@example.com", "Alice")
        req = {"source": "Fantasy basketball fades", "style": "girl gossip", "vibe": "gossip",
               "hosts": [{"name": "Kayla", "voice": "Leda"}, {"name": "Nia", "voice": "Kore"}],
               "minutes": 2, "language": "English", "research": True}
        r = await alice_c.post("/api/shows", json={"request": req, "live": True})
        show_id = r.json()["id"]
        checks["research: a pod with Research on costs more (1.5 a minute)"] = (
            r.status_code == 200 and r.json()["credits"] == alice["credits"] - 3)
        prod = await Listener("alice-r", f"{WS}/ws/show/{show_id}", alice_c.cookies.get("gp_session")).connect()
        await prod.until(lambda: prod.of("segment"), what="first segment")
        for i, text in enumerate(["DEFER_ME how about Zion Williams?", "SNEAKY_DEFER what about Maxey?",
                                  "RESEARCH_ME Zion's numbers this season?"]):
            await prod.send({"type": "comment", "text": text, "client_id": f"r{i}"})
        await prod.until(lambda: prod.of("saved"), timeout=120, what="the research pod saved")
        await prod.close()

        latest = {c["client_id"]: c for c in prod.of("comment")}       # each call-in's last state
        contexts = {x["id"]: x for x in prod.of("context")}
        lines = [ln["text"] for s in prod.of("segment") for ln in s["lines"]]

        def kept(c: dict) -> bool:
            return c.get("status") == "on_deck" and c.get("promised_seq") and c["seq"] > c["promised_seq"]
        checks["call-ins: one the hosts promised to come back to is answered later"] = kept(latest["r0"])
        checks["call-ins: one marked answered while promising to come back is still answered later"] = kept(latest["r1"])
        looked = latest["r2"]
        found = contexts.get(looked.get("research_id"), {})
        checks["research: a call-in's lookup comes back cited, then it's answered"] = (
            kept(looked) and looked.get("research") == "ready" and found.get("kind") == "research"
            and found.get("status") == "used" and [s["domain"] for s in found.get("sources", [])] == ["espn.com", "nbcsports.com"]
            and bool(found.get("search_suggestions")) and any("answered with the research" in t for t in lines))
        checks["research: a thin chapter is looked up ahead of time"] = any(
            x.get("kind") == "research" and x.get("for_beat") == 2 for x in contexts.values())
        seen = httpx.get(f"http://127.0.0.1:{FAKE_PORT}/_seen").json()
        checks["research: only Research pods can ask for lookups"] = (
            any("research_requests" in s for s in seen["writer_schemas"])
            and any("research_requests" not in s and "deferred_comment_ids" in s for s in seen["writer_schemas"]))

        data = (await alice_c.get(f"/api/shows/{show_id}")).json()
        saved = {c["client_id"]: c for c in data["comments"]}
        checks["call-ins: promises and research are saved for the replay"] = (
            kept(saved["r0"]) and saved["r2"]["research"] == "ready"
            and any(x["kind"] == "research" and x["sources"] for x in data["context"]))
        entries = (await alice_c.get("/api/me/ledger")).json()["entries"]
        refund = sum(e["amount"] for e in entries if e["reason"] == "refund" and e["show_id"] == show_id)
        checks["research: unused minutes come back at the Research rate"] = (
            refund == max(0, 3 - math.ceil(data["show"]["duration_s"] / 60 * 1.5)))


async def cohost_scenario(checks: dict) -> None:
    clients = [httpx.AsyncClient(base_url=BASE, timeout=60) for _ in range(3)]
    alice_c, bob_c, anon = clients
    R = 24_000
    try:
        alice = await sign_in(alice_c, "alice@example.com", "Alice")
        await sign_in(bob_c, "bob@example.com", "Bob")
        alice_cookie, bob_cookie = (c.cookies.get("gp_session") for c in (alice_c, bob_c))
        body = {"request": {
            "host": {"name": "Nova", "gender": "female", "persona": "A sharp, funny skeptic.",
                     "context": "NOVA PRIVATE NOTE"},
            "mode": "banter", "topic": "Is sourdough worth it?", "notes": "Starter is 3 days old.", "minutes": 5,
        }, "visibility": "public"}

        async def credits_for(sid: str, reason: str) -> int:
            entries = (await alice_c.get("/api/me/ledger")).json()["entries"]
            return sum(e["amount"] for e in entries if e["reason"] == reason and e["show_id"] == sid)

        checks["co-host: signed out can't start a call"] = (await anon.post("/api/cohost", json=body)).status_code == 401
        checks["co-host: a free account can't start a call"] = (await bob_c.post("/api/cohost", json=body)).status_code == 402
        r = await alice_c.post("/api/cohost", json=body)
        call_id = r.json()["id"]
        checks["co-host: starting a call reserves its minutes"] = r.status_code == 200 and r.json()["credits"] == alice["credits"] - 5
        checks["co-host: one call at a time"] = (await alice_c.post("/api/cohost", json=body)).status_code == 409

        public = [s["id"] for s in (await anon.get("/api/shows", params={"scope": "public"})).json()["shows"]]
        mine = {s["id"]: s for s in (await alice_c.get("/api/shows")).json()["shows"]}
        own = (await alice_c.get(f"/api/shows/{call_id}")).json()
        checks["co-host: a call in progress is hidden from everyone else"] = (
            call_id not in public and (await bob_c.get(f"/api/shows/{call_id}")).status_code == 409)
        checks["co-host: its owner sees it live, with a way back to the call"] = (
            mine[call_id]["status"] == "live" and mine[call_id]["kind"] == "cohost"
            and own["live"] and own["call"] == f"/cohost/{call_id}")

        url = f"{WS}/ws/cohost/{call_id}"
        checks["co-host: only the owner can join the call"] = await closed_with(url, bob_cookie) == 4403
        first = await Phone("phone1", url, alice_cookie).connect()
        await first.until(lambda: first.of("ready"), what="ready")
        phone = await Phone("phone", url, alice_cookie).connect()
        await phone.until(lambda: phone.of("ready"), what="ready")
        await first.until(lambda: first.close_code is not None, timeout=10, what="being replaced")
        checks["co-host: a newer connection takes the call over"] = first.close_code == 4410
        phone.start_mic()
        ready = phone.of("ready")[-1]
        checks["co-host: ready says who's on the call"] = (
            ready["host"]["name"] == "Nova" and ready["host"]["gender"] == "female" and ready["you"] == "Alice"
            and ready["limit_s"] == 300 and ready["mode"] == "banter")

        # The opening starts as soon as the first phone joins, so its words may have gone
        # to that phone, or into the second one's ready snapshot, before the handover.
        await phone.until(lambda: phone.done(1) or first.done(1), timeout=20, what="the opening")
        said = first.of("said") + ready["said"] + phone.of("said")
        checks["co-host: the co-host opens the show"] = (
            any(e["who"] == "host" and "Welcome" in e["text"] for e in said)
            and (first.turn_samples.get(1, 0) + phone.turn_samples.get(1, 0)) > 0)
        await asyncio.sleep(1.6)                  # let it finish playing

        await phone.say(1.0)                      # a mic check (the stub hears "can you hear me?")
        await phone.until(lambda: phone.done(2), timeout=15, what="the answer to the mic check")
        checks["co-host: your words are transcribed live"] = any(
            e["who"] == "you" and "hear me" in e["text"] for e in phone.of("said"))
        await asyncio.sleep(1.6)

        await phone.say(1.0)                      # a point, then cut in as soon as it answers
        await phone.until(lambda: phone.turn_samples.get(3), timeout=15, what="the third answer")
        phone.talking = True
        await phone.until(lambda: phone.of("interrupted"), timeout=10, what="interrupted")
        await asyncio.sleep(0.8)
        phone.talking = False
        checks["co-host: cutting in stops the answer"] = phone.of("interrupted")[0]["turn"] == 3 and not phone.done(3)
        await phone.until(lambda: phone.done(4), timeout=15, what="the answer after cutting in")
        await asyncio.sleep(1.6)

        await phone.send({"type": "mode", "mode": "debate"})
        await phone.until(lambda: phone.of("mode") and phone.done(5), timeout=15, what="the mode switch")
        await asyncio.sleep(0.7)
        await phone.send({"type": "cut_in"})      # the phone cut it off, after it was all sent
        await asyncio.sleep(1.5)                  # the stub recycles the connection meanwhile
        seen = httpx.get(f"http://127.0.0.1:{FAKE_PORT}/_seen").json()
        setups = seen["live_setups"]
        checks["co-host: a mode switch reaches the co-host"] = any(
            "switch it up" in t and "opposite side" in t for t in seen["live_texts"])
        checks["co-host: a recycled Live connection resumes the session"] = (
            len(setups) >= 2 and setups[1]["setup"].get("sessionResumption", {}).get("handle") == "handle-1")
        first_setup = json.dumps(setups[0])
        checks["co-host: the co-host gets its persona, private notes, the topic and your notes"] = (
            "NOVA PRIVATE NOTE" in first_setup and "Starter is 3 days old" in first_setup
            and "Is sourdough worth it?" in first_setup and f'"{ready["host"]["voice"]}"' in first_setup)
        await phone.say(1.0)
        await phone.until(lambda: phone.done(6), timeout=15, what="an answer after the reconnect")
        await asyncio.sleep(1.6)

        await phone.send({"type": "hangup"})
        await phone.until(lambda: phone.of("saved") or phone.of("ended"), timeout=60, what="saved")
        saved = (phone.of("saved") or [{}])[0]
        checks["co-host: hanging up edits it and saves it"] = (
            saved.get("url") == f"/show/{call_id}"
            and [e["step"] for e in phone.of("editing")] == ["listening", "notes", "cutting", "mastering"])
        await phone.close()

        data = (await alice_c.get(f"/api/shows/{call_id}")).json()
        show, segs = data["show"], data["segments"]
        speakers = {ln["speaker"] for s in segs for ln in s["lines"]}
        text = " ".join(ln["text"] for s in segs for ln in s["lines"])
        checks["co-host: saved as a finished, titled pod"] = (
            show["status"] == "finished" and show["kind"] == "cohost" and show["title"] == "Fake Edited Episode"
            and show["has_audio"] and show["has_raw_audio"] and data["live"] is False)
        checks["co-host: both voices are in the transcript"] = {"Alice", "Nova"} <= speakers
        checks["co-host: the mic check is cut, and the answer to it"] = (
            "hear me" not in text and "Loud and clear" not in text and show["edit"]["flubs_cut"] == 1)
        switched = [s for s in segs if any("Switching" in ln["text"] for ln in s["lines"])]
        checks["co-host: an answer cut off at once is dropped; one cut later is shortened"] = (
            "Reply 2" not in text and len(switched) == 1 and 1.5 * R < switched[0]["samples"] < 2.95 * R)
        checks["co-host: segments sit back to back with chapters"] = (
            all(b["start"] > a["start"] + a["samples"] for a, b in zip(segs, segs[1:]))
            and [b["title"] for b in show["beats"]] == ["Opening", "The middle"] and show["beats"][0]["start"] == 0)
        checks["co-host: the replay re-tints where the mode switched"] = (
            [s["style_rev"] for s in segs][-1] == 1 and len(show["styles"]) == 2
            and show["styles"][1]["label"] == "Co-host · Debate me")

        audio = await alice_c.get(f"/api/shows/{call_id}/audio")
        raw = await alice_c.get(f"/api/shows/{call_id}/audio", params={"raw": 1})
        checks["co-host: the episode and the unedited call download"] = (
            audio.status_code == raw.status_code == 200 and audio.headers["content-type"] == "audio/mpeg"
            and len(raw.content) > len(audio.content) / 3)
        checks["co-host: only the owner gets the unedited call"] = (
            (await bob_c.get(f"/api/shows/{call_id}/audio", params={"raw": 1})).status_code == 403
            and (await anon.get(f"/api/shows/{call_id}/audio", params={"raw": 1})).status_code == 401
            and (await anon.get(f"/api/shows/{call_id}/audio")).status_code == 200)
        listed = (await anon.get(f"/api/shows/{call_id}")).text
        checks["co-host: the co-host's private notes stay private"] = "NOVA PRIVATE NOTE" not in listed
        refund = await credits_for(call_id, "refund")
        checks["co-host: unused minutes come back"] = (
            await credits_for(call_id, "pod") == -5 and refund == 5 - math.ceil(show["call_s"] / 60) and refund > 0)

        # a call nobody joins: no pod, every credit back
        r = await alice_c.post("/api/cohost", json={**body, "visibility": "private"})
        ghost = r.json()["id"]
        deadline = time.time() + 30
        while time.time() < deadline and (await alice_c.get(f"/api/shows/{ghost}")).status_code != 404:
            await asyncio.sleep(0.5)
        checks["co-host: a call nobody joined leaves no pod and refunds everything"] = (
            (await alice_c.get(f"/api/shows/{ghost}")).status_code == 404 and await credits_for(ghost, "refund") == 5)
    finally:
        for c in clients:
            await c.aclose()


async def memo_scenario(checks: dict, alice_c, bob_c, anon, credits_for, opts: dict) -> None:
    def memo(audio: bytes = WEBM, **options) -> dict:
        return {"files": {"audio": ("walk.webm", audio, "audio/webm")},
                "data": {"options": json.dumps({"minutes": 2, **options})}}

    music_credits = opts["music"]["credits"]
    checks["options list the music library and memo limits"] = (
        [p["id"] for p in opts["music"]["library"]] == ["warm-test"] and opts["music"]["generate"]
        and "webm" in opts["memo"]["audio_types"])

    # your show intro: every pod you make opens with it
    r = await alice_c.put("/api/me/intro", json={"show_name": "Walk & Talk", "intro_line": "ideas from the sidewalk"})
    checks["show intro saved"] = r.status_code == 200 and (
        await alice_c.get("/api/me/intro")).json()["intro"]["show_name"] == "Walk & Talk"
    checks["show intro: unknown music refused"] = (
        await alice_c.put("/api/me/intro", json={"show_name": "x", "music": "nope"})).status_code == 422

    # uploads that can't become a pod
    checks["memo: not a recording -> 422"] = (await alice_c.post("/api/memos", **memo(b"just some text"))).status_code == 422
    checks["memo: too big -> 413"] = (await alice_c.post("/api/memos", **memo(WEBM + b"\x00" * (1024 * 1024)))).status_code == 413
    checks["memo: free account -> 402"] = (await bob_c.post("/api/memos", **memo())).status_code == 402

    # a one-shot public memo: charged on upload, hidden until it's made. (Credits are
    # checked in the ledger: /api/me caches balances for a few seconds.)
    r = await alice_c.post("/api/memos", **memo(visibility="public"))
    memo_id = r.json().get("id", "")
    checks["memo: charged when it's sent"] = r.status_code == 200 and await credits_for(memo_id, "pod") == -2
    public = [s["id"] for s in (await anon.get("/api/shows", params={"scope": "public"})).json()["shows"]]
    checks["memo pod hidden while it's made"] = (
        memo_id not in public and (await anon.get(f"/api/shows/{memo_id}")).status_code == 401
        and (await bob_c.get(f"/api/shows/{memo_id}")).status_code == 404)
    checks["one memo pod at a time -> 409"] = (await alice_c.post("/api/memos", **memo())).status_code == 409
    data = await wait_for_status(alice_c, memo_id, {"finished", "failed"})
    show, segs = data["show"], data["segments"]
    stings = sum(r_["kind"] == "sting" for r_ in show["music"]["regions"])
    inserted = int(INTRO_LEAD_S * RATE) + stings * int(STING_S * RATE) + int(OUTRO_PLAYOUT_S * RATE)
    checks["memo pod made: intro, stings and outro, every start shifted"] = (
        show["status"] == "finished" and show["kind"] == "memo" and show["render"]["stage"] == "done"
        and segs[0]["start"] == int(INTRO_LEAD_S * RATE) and show["samples"] == sum(s["samples"] for s in segs) + inserted
        # back to back, or with a sting between chapters
        and all(b["start"] - a["start"] - a["samples"] in {0, int(STING_S * RATE)} for a, b in zip(segs, segs[1:])))
    seen = httpx.get(f"http://127.0.0.1:{FAKE_PORT}/_seen").json()
    memo_writers = [s for s in seen["writer_systems"] if "EPISODE DIRECTION" in s]
    checks["memo pod: written from the memo and the live research, as a recorded show"] = bool(memo_writers) and all(
        "[R1]" in s and "Acoustical Society of America" in s and "pre-recorded" in s and "Listener comments" not in s
        for s in memo_writers)
    checks["memo pod opens with your show intro"] = all('an episode of "Walk & Talk"' in s for s in memo_writers)
    checks["memo pod: show notes credit the memo and list the sources"] = (
        show["show_notes"]["credit"] == "Made from a voice memo by Alice." and
        [x["domain"] for x in show["show_notes"]["sources"]] == ["who.int", "acousticalsociety.org"])
    anon_view = await anon.get(f"/api/shows/{memo_id}")
    checks["memo pod: out for everyone, the memo itself owner-only"] = (
        anon_view.status_code == 200 and "memo" not in anon_view.json() and data["memo"]["brief"]["transcript"]
        and (await alice_c.get(f"/api/memos/{memo_id}/audio")).content == WEBM
        and (await alice_c.get(f"/api/memos/{memo_id}/audio", headers={"Range": "bytes=0-9"})).status_code == 206
        and (await bob_c.get(f"/api/memos/{memo_id}/audio")).status_code in {403, 404})
    ranged = await anon.get(f"/api/shows/{memo_id}/audio", headers={"Range": "bytes=0-99"})
    charged = -await credits_for(memo_id, "pod")
    checks["memo pod: mp3 with Range; unused minutes refunded"] = ranged.status_code == 206 and await credits_for(
        memo_id, "refund") == max(0, charged - math.ceil(show["duration_s"] / 60))

    # held for review, made with the owner's edits; original music fails and its credits come back
    httpx.post(f"http://127.0.0.1:{FAKE_PORT}/_config", json={"lyria_fail": True})
    r = await alice_c.post("/api/memos", **memo(review=True, music="generate", use_show_intro=False))
    rid = r.json().get("id", "")
    checks["reviewed memo: not charged until Produce"] = r.status_code == 200 and await credits_for(rid, "pod") == 0
    data = await wait_for_status(alice_c, rid, {"review", "failed"})
    owner = data.get("memo") or {}
    checks["review: the owner sees the transcript, research and outline"] = (
        data["show"]["status"] == "review" and owner["brief"]["transcript"] and len(owner["research"]["findings"]) == 3
        and owner["research"]["search_suggestions"] and len(owner["plan"]["beats"]) >= 3)
    checks["review: nobody else sees it"] = (await bob_c.get(f"/api/shows/{rid}")).status_code == 404
    beats = [{"title": b["title"], "notes": b["notes"]} for b in owner["plan"]["beats"]]
    beats[1]["title"] = "EDITED BEAT"
    p = await alice_c.post(f"/api/memos/{rid}/produce", json={"beats": beats, "excluded_findings": [3]})
    checks["produce charges the minutes and the music"] = p.status_code == 200 and (
        await credits_for(rid, "pod"), await credits_for(rid, "music")) == (-2, -music_credits)
    data = await wait_for_status(alice_c, rid, {"finished", "failed"})
    show = data["show"]
    spent = -sum([await credits_for(rid, k) for k in ("pod", "music", "refund")])
    checks["the outline made is the edited one"] = (
        show["status"] == "finished" and show["beats"][1]["title"] == "EDITED BEAT"
        and data["memo"]["excluded_findings"] == [3])
    checks["Lyria failed: library music, music credits back"] = (
        show["music"]["source"] == "library" and spent == min(2 + music_credits, math.ceil(show["duration_s"] / 60)))
    httpx.post(f"http://127.0.0.1:{FAKE_PORT}/_config", json={"lyria_fail": False})

    # deleting a memo pod while it's being made gives everything back
    r = await alice_c.post("/api/memos", **memo())
    did = r.json().get("id", "")
    await asyncio.sleep(0.5)
    deleted = await alice_c.delete(f"/api/shows/{did}")
    checks["deleting a memo pod mid-way refunds it"] = deleted.status_code == 200 and await credits_for(did, "refund") == 2


def main() -> int:
    data_dir = tempfile.mkdtemp(prefix="podcasts-smoke-")
    env = {
        **os.environ, "GEMINI_API_KEY": "fake", "GEMINI_BASE_URL": f"http://127.0.0.1:{FAKE_PORT}",
        "STORAGE_BACKEND": "local", "DATA_DIR": data_dir, "AUTH_MODE": "dev", "SESSION_SECRET": "smoke",
        "ADMIN_EMAILS": "alice@example.com", "SIGNUP_CREDITS": "0",
        "CLOCK_SPEED": "20", "LOW_WATER_SECONDS": "3", "LINES_PER_SEGMENT": "4", "IDLE_END_SECONDS": "5",
        "MUSIC_DIR": str(music_library(Path(data_dir, "music"))), "MEMO_MAX_MB": "1",
        "STINGER_DELAY_SECONDS": "0",
        "COHOST_RECONNECT_SECONDS": "4",
    }
    py = sys.executable
    app_log = Path(data_dir, "app.log")
    fake = _start([py, "-m", "uvicorn", "tests.fake_gemini:app", "--port", str(FAKE_PORT)], env, Path(data_dir, "fake.log"))
    srv = _start([py, "-m", "uvicorn", "app.main:app", "--port", str(APP_PORT)], env, app_log)
    checks: dict[str, bool] = {}
    error = None
    try:
        _wait_up(f"http://127.0.0.1:{FAKE_PORT}/docs")
        _wait_up(f"{BASE}/healthz")
        asyncio.run(scenario(checks))
        asyncio.run(callins_scenario(checks))
        asyncio.run(cohost_scenario(checks))
    except Exception as exc:  # report what passed so far, then the failure
        error = exc
    finally:
        for p in (srv, fake):
            p.terminate()
            p.wait(timeout=10)
        out = app_log.read_text(encoding="utf-8", errors="replace")

    for name, ok in checks.items():
        print(("PASS " if ok else "FAIL ") + name)
    saved = list(Path(data_dir, "shows").glob("*/episode.mp3"))
    print(f"{len(saved)} episodes saved under {data_dir}")
    if error or not all(checks.values()):
        if error:
            print(f"ERROR {type(error).__name__}: {error}")
        print(out[-6000:])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
