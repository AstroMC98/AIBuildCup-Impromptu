"""Fast checks of the pieces that are hard to reach end to end.

    python -m tests.unit_test
"""
import asyncio
import json
import math
import os
import re
import struct
import sys
import tempfile
import traceback
from pathlib import Path

import httpx
import numpy as np

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="podcasts-unit-"))

from app import editing as ed  # noqa: E402
from app import prompts, sources  # noqa: E402
from app.catalog import VOICE_GENDER  # noqa: E402
from app.cohost import cast_cohost  # noqa: E402
from app.accounts import Accounts, published  # noqa: E402
from app.audio import Mp3Writer, PcmWindow  # noqa: E402
from app.auth import Auth, AuthError  # noqa: E402
from app.store import InsufficientCredits  # noqa: E402
from app.clock import LiveClock  # noqa: E402
from app.config import Settings  # noqa: E402
from app.limits import TokenBucket  # noqa: E402
from app.llm import Lookup  # noqa: E402
from app.models import (  # noqa: E402
    Beat, CohostRequest, ContextItem, Host, HostRequest, Line, ResearchRequest, SegmentDraft, ShowPlan, ShowRequest,
)
from app.room import refund_unused  # noqa: E402
from app.show import Draft, ShowRunner  # noqa: E402
from app.sources import SourceError, sniff_audio, sniff_image  # noqa: E402
from app.store.local import LocalStore  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name):
    def wrap(fn):
        try:
            out = fn()
            if asyncio.iscoroutine(out):
                asyncio.run(out)
            RESULTS.append((name, True, ""))
        except Exception:
            RESULTS.append((name, False, traceback.format_exc(limit=3)))
        return fn
    return wrap


class FakeTime:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


# ---- clock -------------------------------------------------------------------

@check("clock holds while nobody plays")
def _():
    t = FakeTime()
    c = LiveClock(1000, now=t)
    c.add_audio(5000)
    t.t += 2
    assert c.tick() == 0


@check("clock advances while playing and clamps to the voiced audio")
def _():
    t = FakeTime()
    c = LiveClock(1000, now=t)
    c.add_audio(3000)
    c.set_listening(1)
    t.t += 2
    assert c.tick() == 2000
    t.t += 5
    assert c.tick() == 3000 and not c.playing
    c.add_audio(1000)        # new audio resumes from the clamp, not from wall time
    t.t += 0.5
    assert c.tick() == 3500
    assert math.isclose(c.ahead_s(), 0.5)


@check("clock speed multiplies time")
def _():
    t = FakeTime()
    c = LiveClock(1000, speed=20, now=t)
    c.add_audio(100_000)
    c.set_listening(2)
    t.t += 1
    assert c.tick() == 20_000


# ---- limits ----------------------------------------------------------------------

@check("token bucket bursts then refills")
def _():
    t = FakeTime()
    b = TokenBucket(rate=1, burst=2, now=t)
    assert b.take() and b.take() and not b.take()
    t.t += 1
    assert b.take() and not b.take()


# ---- auth ------------------------------------------------------------------------

@check("sessions carry the user id; tampering fails")
def _():
    a = Auth(Settings(session_secret="s1"))
    cookie = a.issue_session("user-123")
    assert a.session_uid(cookie) == "user-123"
    assert a.session_uid(cookie[:-2] + "xx") is None
    assert a.session_uid(cookie.replace("v2.", "v1.")) is None
    assert Auth(Settings(session_secret="s2")).session_uid(cookie) is None
    forged = cookie.split(".")
    forged[1] = "YWRtaW4"  # "admin", with the original signature
    assert a.session_uid(".".join(forged)) is None


@check("firebase tokens: verified accounts in, unverified emails out from any provider")
async def _():
    a = Auth(Settings(session_secret="s1", auth_mode="firebase", firebase_project_id="p1"))
    a._verify = lambda token: {"sub": "u1", "email": "Ann@Example.com", "email_verified": True,
                               "name": "Ann", "firebase": {"sign_in_provider": "google.com"}}
    profile = await a.verify_firebase("x" * 30)
    assert profile == {"uid": "u1", "email": "ann@example.com", "name": "Ann", "photo": ""}
    # An unverified address must never be trusted (it decides admin rights), whatever
    # the provider, including a missing email_verified claim.
    for provider, claim in (("password", False), ("github.com", False), ("oidc.custom", None)):
        token = {"sub": "u2", "email": "boss@example.com", "firebase": {"sign_in_provider": provider}}
        if claim is not None:
            token["email_verified"] = claim
        a._verify = lambda _t, token=token: token
        try:
            await a.verify_firebase("x" * 30)
            raise AssertionError(f"an unverified {provider} email could have claimed an admin address")
        except AuthError:
            pass

    def bad(token):
        raise ValueError("expired")
    a._verify = bad
    try:
        await a.verify_firebase("x" * 30)
        raise AssertionError("accepted a bad token")
    except AuthError:
        pass


@check("dev sign-in can't be enabled on Google Cloud")
def _():
    try:
        Settings(storage_backend="gcp", gcs_bucket="b", session_secret="s", auth_mode="dev")
        raise AssertionError("dev auth allowed with the gcp backend")
    except ValueError:
        pass


# ---- accounts ------------------------------------------------------------------------

async def _accounts() -> tuple[Accounts, LocalStore]:
    s = LocalStore(tempfile.mkdtemp(prefix="acct-"))
    acc = Accounts(s, admins={"boss@example.com"}, signup_credits=5)
    for uid, email in [("ann", "ann@example.com"), ("bob", "bob@example.com"),
                       ("cat", "cat@example.com"), ("boss", "boss@example.com")]:
        await acc.sign_in({"uid": uid, "email": email, "name": uid.title()})
    return acc, s


@check("signup credits, spending, refusing to overspend, ledger")
async def _():
    acc, s = await _accounts()
    ann = await acc.get("ann")
    assert ann.credits == 5 and not ann.admin and (await acc.get("boss")).admin
    await acc.sign_in({"uid": "ann", "email": "ann@example.com", "name": "Ann B"})  # second sign-in: no bonus
    assert (await acc.get("ann")).credits == 5 and (await acc.get("ann")).name == "Ann B"
    assert await acc.adjust_credits("ann", -3, reason="pod", show_id="x") == 2
    try:
        await acc.adjust_credits("ann", -3, reason="pod")
        raise AssertionError("overspent")
    except InsufficientCredits as exc:
        assert exc.balance == 2 and exc.needed == 3
    assert (await acc.get("ann")).credits == 2
    reasons = [e["reason"] for e in await s.ledger("ann")]
    assert sorted(reasons) == ["pod", "signup"]


@check("demo reset tops a balance up to the target, never down, and the account cache sees it")
async def _():
    acc, s = await _accounts()
    assert (await acc.get("ann")).credits == 5            # cached now
    assert await acc.top_up_credits("ann", 30, reason="reset", note="Demo top-up") == (30, 25)
    assert (await acc.get("ann")).credits == 30           # the cache was dropped
    await acc.adjust_credits("ann", -10, reason="pod")
    assert await acc.top_up_credits("ann", 30, reason="reset") == (30, 10)
    await acc.adjust_credits("ann", 15, reason="grant")
    assert await acc.top_up_credits("ann", 30, reason="reset") == (45, 0)   # more than 30 stays
    assert [e["reason"] for e in await s.ledger("ann")].count("reset") == 2


@check("connections: request, accept, remove; asking back accepts")
async def _():
    acc, s = await _accounts()
    ann, bob, cat = await acc.get("ann"), await acc.get("bob"), await acc.get("cat")
    assert await acc.request(ann, bob) == "pending_out"
    assert (await s.get_edge("bob", "ann"))["status"] == "pending_in"
    assert not await acc.connected("ann", "bob")
    assert await acc.accept(bob, ann) == "connected"
    assert await acc.connected("ann", "bob") and await acc.connected("bob", "ann")
    await acc.request(cat, ann)
    assert await acc.request(ann, cat) == "connected"  # asking back accepts
    try:
        await acc.accept(bob, cat)
        raise AssertionError("accepted a request that was never sent")
    except ValueError:
        pass
    await acc.remove(ann, "bob")
    assert await s.get_edge("bob", "ann") is None and not await acc.connected("ann", "bob")


@check("who can see a pod")
async def _():
    acc, _ = await _accounts()
    ann, bob, cat, boss = [await acc.get(u) for u in ("ann", "bob", "cat", "boss")]
    await acc.request(ann, bob)
    await acc.accept(bob, ann)
    pod = {"owner_uid": "ann", "status": "finished", "visibility": "private", "shared_with": []}
    assert [await acc.can_view(u, pod) for u in (ann, bob, cat, boss, None)] == [True, False, False, True, False]
    pod["shared_with"] = ["cat"]
    assert await acc.can_view(cat, pod) and not await acc.can_view(bob, pod)
    pod.update(visibility="connections", shared_with=[])
    assert await acc.can_view(bob, pod) and not await acc.can_view(cat, pod) and not await acc.can_view(None, pod)
    pod["visibility"] = "public"
    assert all([await acc.can_view(u, pod) for u in (bob, cat, None)])
    pod["status"] = "draft"
    assert not await acc.can_view(bob, pod) and await acc.can_view(ann, pod)
    assert acc.role(ann, pod) == "producer" and acc.role(bob, pod) == "listener" and acc.role(None, pod) == "visitor"


@check("memo pods stay with their owner until they're out")
async def _():
    acc, _ = await _accounts()
    ann, bob = await acc.get("ann"), await acc.get("bob")
    pod = {"owner_uid": "ann", "kind": "memo", "visibility": "public", "shared_with": ["bob"], "audio": None}
    for status in ("rendering", "review"):
        pod["status"] = status
        assert not published(pod) and await acc.can_view(ann, pod)
        assert not await acc.can_view(bob, pod) and not await acc.can_view(None, pod)
    pod["status"] = "failed"                     # nothing was ever saved: it never came out
    assert not published(pod) and not await acc.can_view(None, pod)
    pod["audio"] = {"path": "episode.mp3"}
    assert published(pod)
    assert published({"status": "failed", "audio": None})          # a live pod that failed was heard
    pod.update(status="finished")
    assert await acc.can_view(None, pod) and await acc.can_view(bob, pod)


# ---- links and images -------------------------------------------------------------

@check("private addresses are refused")
async def _():
    for url in ["http://127.0.0.1/", "http://10.1.2.3/x", "http://169.254.169.254/latest",
                "http://[::1]/", "http://[::ffff:127.0.0.1]/", "ftp://example.com/"]:
        try:
            await sources._check_public(url)
        except SourceError:
            continue
        raise AssertionError(f"{url} was allowed")


@check("a redirect to a private address is refused")
async def _():
    real = sources._resolve

    async def fake_resolve(host, port):
        return {"public.example": ["93.184.216.34"], "sneaky.example": ["192.168.1.10"]}[host]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "public.example":
            return httpx.Response(302, headers={"location": "http://sneaky.example/admin"})
        return httpx.Response(200, text="secret")

    sources._resolve = fake_resolve
    try:
        await sources.url_to_text("http://public.example/", transport=httpx.MockTransport(handler))
    except SourceError as exc:
        assert "private" in str(exc)
    else:
        raise AssertionError("followed a redirect into a private network")
    finally:
        sources._resolve = real


@check("public pages are fetched, with a size cap")
async def _():
    real = sources._resolve

    async def fake_resolve(host, port):
        return ["93.184.216.34"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/big":
            return httpx.Response(200, content=b"x" * (sources.MAX_FETCH_BYTES + 10))
        return httpx.Response(200, html="<html><title>Hi</title><p>Hello there</p></html>")

    sources._resolve = fake_resolve
    try:
        title, text = await sources.url_to_text("http://ok.example/", transport=httpx.MockTransport(handler))
        assert title == "Hi" and "Hello there" in text
        try:
            await sources.url_to_text("http://ok.example/big", transport=httpx.MockTransport(handler))
            raise AssertionError("no size cap")
        except SourceError:
            pass
    finally:
        sources._resolve = real


@check("images are recognised by their bytes")
def _():
    assert sniff_image(b"\x89PNG\r\n\x1a\n....") == "image/png"
    assert sniff_image(b"\xff\xd8\xff\xe0....") == "image/jpeg"
    assert sniff_image(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff_image(b"\x00\x00\x00\x18ftypheic....") == "image/heic"
    assert sniff_image(b"GIF89a....") is None
    assert sniff_image(b"%PDF-1.7") is None


@check("recordings are recognised by their bytes, whatever the browser or phone")
def _():
    assert sniff_audio(b"RIFF\x24\x00\x00\x00WAVEfmt ") == "audio/wav"
    assert sniff_audio(b"OggS\x00\x02" + b"\x00" * 20) == "audio/ogg"                 # Firefox
    assert sniff_audio(b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\x82\x84webm") == "audio/webm"   # Chrome
    assert sniff_audio(b"\x1a\x45\xdf\xa3\x9f\x42\x82\x88matroska") is None
    assert sniff_audio(b"\x00\x00\x00\x1cftypM4A \x00\x00\x00\x00") == "audio/m4a"     # iOS Voice Memos
    assert sniff_audio(b"\x00\x00\x00\x1cftypiso5\x00\x00\x02\x00") == "audio/m4a"     # Safari
    assert sniff_audio(b"\x00\x00\x00\x18ftypheic....") is None                        # a photo
    assert sniff_audio(b"fLaC\x00\x00\x00\x22") == "audio/flac"
    assert sniff_audio(b"FORM\x00\x00\x10\x00AIFFCOMM") == "audio/aiff"
    assert sniff_audio(b"ID3\x04\x00\x00\x00\x00\x00\x00") == "audio/mpeg"
    assert sniff_audio(b"\xff\xfb\x90\x64") == "audio/mpeg"                            # a bare MP3 frame
    assert sniff_audio(b"\xff\xf1\x50\x80") == "audio/aac"                             # ADTS
    assert sniff_audio(b"\xff\xd8\xff\xe0") is None                                    # JPEG
    assert sniff_audio(b"%PDF-1.7") is None and sniff_audio(b"") is None
    assert all(m in sources.AUDIO_EXTENSIONS for m in ("audio/webm", "audio/m4a", "audio/ogg"))


# ---- audio ---------------------------------------------------------------------------

@check("mp3 writer produces mp3 frames, mid-show and at the end")
def _():
    w = Mp3Writer(24_000, 48)
    pcm = b"".join(struct.pack("<h", int(3000 * math.sin(i / 8))) for i in range(48_000))
    w.feed(pcm)
    mid = w.snapshot()
    final = w.finish()
    assert final[0] == 0xFF and final[1] & 0xE0 == 0xE0
    assert len(final) >= len(mid) > 0 and w.finish() is final


@check("pcm window drops what comes after an interruption")
def _():
    p = PcmWindow()
    p.add(0, 1, b"\x01\x00" * 100)
    p.add(100, 2, b"\x02\x00" * 100)
    p.truncate(150)
    assert [(c[0], len(c[2]) // 2) for c in p.since(0)] == [(0, 100), (100, 50)]
    p.truncate(100)
    assert [(c[0], len(c[2]) // 2) for c in p.since(0)] == [(0, 100)]


@check("pcm window serves from any sample and trims behind")
def _():
    p = PcmWindow()
    p.add(0, 1, b"\x01\x00" * 100)
    p.add(100, 2, b"\x02\x00" * 100)
    chunks = p.since(150)
    assert chunks[0][0] == 150 and chunks[0][1] == 2 and len(chunks[0][2]) == 100
    assert [c[0] for c in p.since(0)] == [0, 100]
    p.trim(120)
    assert p.start == 100


# ---- store ---------------------------------------------------------------------------

@check("local store round trip")
async def _():
    s = LocalStore(tempfile.mkdtemp(prefix="store-"))
    await s.create_show({"id": "a1", "status": "live", "created_at": 1.0})
    await s.create_show({"id": "b2", "status": "finished", "created_at": 2.0})
    await s.update_show("a1", {"title": "Hello"})
    assert (await s.get_show("a1"))["title"] == "Hello"
    assert [d["id"] for d in await s.list_shows()] == ["b2", "a1"]
    await s.put_segment("a1", {"seq": 2, "lines": []})
    await s.put_segment("a1", {"seq": 1, "lines": []})
    await s.put_comment("a1", {"id": 1, "text": "hi"})
    await s.put_context("a1", {"id": 1, "name": "x"})
    kids = await s.load_children("a1")
    assert [x["seq"] for x in kids["segments"]] == [1, 2] and kids["comments"][0]["text"] == "hi"
    await s.put_text("a1", "context/1.txt", "body")
    assert await s.get_text("a1", "context/1.txt") == "body"
    assert (await s.put_audio("a1", b"\xff\xfb"))["bytes"] == 2
    assert await s.mark_interrupted(set()) == 1 and (await s.get_show("a1"))["status"] == "interrupted"
    await s.delete_show("a1")
    assert await s.get_show("a1") is None and await s.get_show("../etc") is None


@check("local store keeps show and user files; a name can't be a path")
async def _():
    s = LocalStore(tempfile.mkdtemp(prefix="store-"))
    await s.create_show({"id": "m1", "status": "rendering", "created_at": 1.0})
    await s.create_show({"id": "m2", "status": "finished", "created_at": 2.0})
    assert (await s.put_show_file("m1", "memo.webm", b"abc", "audio/webm"))["bytes"] == 3
    assert await s.get_show_file("m1", "memo.webm") == b"abc" and await s.get_show_file("m1", "x.mp3") is None
    for bad in ("../show.json", "show", "a/b.mp3", "", "episode.mp3/.."):
        try:
            await s.put_show_file("m1", bad, b"x", "audio/mpeg")
            raise AssertionError(f"accepted {bad!r}")
        except ValueError:
            pass
    await s.put_user_file("u1", "music-1.mp3", b"\xff\xfb", "audio/mpeg")
    assert await s.get_user_file("u1", "music-1.mp3") == b"\xff\xfb"
    assert [d["id"] for d in await s.shows_with_status("rendering")] == ["m1"]
    await s.delete_show("m1")
    assert await s.get_show_file("m1", "memo.webm") is None


# ---- the show loop ------------------------------------------------------------------

class StubRoom:
    def __init__(self):
        self.clock = LiveClock(24_000)
        self.wake = asyncio.Event()
        self.events = []

    def publish(self, event):
        self.events.append(event)

    def push_audio(self, seq, pcm):
        self.clock.add_audio(len(pcm) // 2)

    def truncate(self, at, keep_seq, cut):
        self.events.append({"type": "truncate", "at": at, "keep_seq": keep_seq, "cut": cut})
        self.clock.audio_end = at
        self.clock.live = min(self.clock.live, float(at))


def _runner(research: bool = False) -> ShowRunner:
    req = ShowRequest(source="x", style="calm", hosts=[{"name": "A"}], minutes=4, research=research)
    runner = ShowRunner(req, "x", gemini=None, settings=Settings(), room=StubRoom())
    runner.plan = ShowPlan(
        title="T", tagline="t", hosts=[Host(name="A", persona="", voice="Kore")],
        beats=[Beat(title=f"b{i}", notes="", minutes=1) for i in range(4)],
    )
    return runner


def _draft(runner: ShowRunner, **kw) -> Draft:
    ctx, comments, context, hearts = runner._context()
    base = dict(segment=SegmentDraft(lines=[Line(speaker="A", text="hi")]), offered_comments=comments,
                offered_context=context, style_rev=runner.style_rev, must_end=ctx.must_end, hearts=hearts)
    return Draft(**{**base, **kw})


@check("drafts go stale on new comments, due context, restyle and end")
async def _():
    r = _runner()
    d = _draft(r)
    assert not r._is_stale(d)
    r.add_comment("Jo", "hello", "guest")
    assert r._is_stale(d)
    d = _draft(r)
    assert not r._is_stale(d)
    r.add_context(ContextItem(kind="text", name="n", text="t", when="next"))
    assert not r._is_stale(d)              # next-chapter items wait for the beat to move
    r.add_context(ContextItem(kind="text", name="n", text="t", when="now"))
    assert r._is_stale(d)
    d = _draft(r)
    r.restyle("loud")
    assert r._is_stale(d)
    d = _draft(r)
    assert d.segment and r._context()[0].style_change == "loud"
    r.request_end()
    assert r._is_stale(d)
    assert _draft(r).must_end


# ---- call-ins the hosts promise to come back to, and research -------------------------

def _seg(text: str, **kw) -> SegmentDraft:
    return SegmentDraft(lines=[Line(speaker="A", text=text)], **kw)


class ResearchDesk:
    """Stands in for Gemini.research: answers, fails, or hangs."""

    def __init__(self, mode: str = "ok"):
        self.mode, self.asked = mode, []

    async def research(self, question, *, show, asked_by="", language="English"):
        self.asked.append(question)
        if self.mode == "fail":
            raise RuntimeError("search is down")
        if self.mode == "hang":
            await asyncio.sleep(3600)
        return Lookup(text="Zion Williamson averaged 24 points.", queries=[question],
                      sources=[{"title": "ESPN", "url": "https://espn.com/z", "domain": "espn.com"}],
                      suggestions=['<div class="chips">zion</div>'])


@check("call-ins: one the hosts promise to come back to stays owed until they answer it")
def _():
    r = _runner()
    c, _ = r.add_comment("Marc Castro", "How about Zion Williams?", "producer")
    d = _draft(r, segment=_seg("Ooh, Marc asks about Zion. Hold that thought!", deferred_comment_ids=[c.id]))
    r._commit(d)
    assert c.status == "promised" and c.promised_seq == d.seq and d.promised == [c.id]
    ctx, offered, _, _ = r._context()
    assert [p.id for p in ctx.promised] == [c.id] and c.id in offered and not ctx.comments
    assert "CALL-INS YOU PROMISED TO COME BACK TO" in prompts.writer_input(ctx, r.plan)
    d2 = _draft(r, segment=_seg("Back to Marc: Zion is a top-30 pick.", addressed_comment_ids=[c.id]))
    r._commit(d2)
    assert c.status == "on_deck" and c.seq == d2.seq and c.promised_seq == d.seq
    assert not r._context()[0].promised


@check("call-ins: marked answered while the hosts say they'll get to it later, it stays owed")
def _():
    r = _runner()
    c, _ = r.add_comment("Marc Castro", "How about Zion Williams?", "producer")
    said = _draft(r, segment=SegmentDraft(lines=[
        Line(speaker="A", text="Oh wait, Marc Castro commented: How about Zion Williams?"),
        Line(speaker="A", text="Marc, darling, we'll get to his frontcourt mess in a second!"),
    ], addressed_comment_ids=[c.id]))
    r._commit(said)                                   # the Fantasy Hoops pod, segment 6
    assert c.status == "promised" and said.addressed == []
    c2, _ = r.add_comment("Jo", "Is Maxey overrated?", "guest")
    r._commit(_draft(r, segment=_seg("Jo, Maxey is overrated at his ADP, period.", addressed_comment_ids=[c2.id])))
    assert c2.status == "on_deck"                     # a real answer is just answered


@check("call-ins: whatever's still owed is offered to the sign-off, then marked missed if skipped")
async def _():
    r = _runner(research=True)
    r.gemini = ResearchDesk("hang")
    c, _ = r.add_comment("Jo", "Stats on Zion?", "guest")
    r._commit(_draft(r, segment=_seg("Looking it up!", research_requests=[ResearchRequest(comment_id=c.id, query="zion")])))
    assert c.research == "pending" and not r._context()[0].promised      # waits for its lookup
    r.request_end()
    ctx = r._context()[0]
    assert ctx.must_end and [p.id for p in ctx.promised] == [c.id] and ctx.promised[0].research_failed
    assert "Answer the promised call-ins below before signing off" in prompts.writer_input(ctx, r.plan)
    for t in r._research_tasks:
        t.cancel()


@check("research: a call-in's lookup runs in the background and comes back as cited context")
async def _():
    r = _runner(research=True)
    r.gemini = desk = ResearchDesk()
    c, _ = r.add_comment("Marc Castro", "How about Zion Williams?", "producer")
    assert r._context()[0].research_left == Settings().research_max_per_show
    d = _draft(r, segment=_seg("Marc! Let me pull up Zion's numbers.",
                               research_requests=[ResearchRequest(comment_id=c.id, query="Zion Williamson 2026-27 fantasy")]))
    r._commit(d)
    assert c.status == "promised" and c.research == "pending"
    meanwhile = _draft(r)                              # written while the lookup runs
    await asyncio.gather(*list(r._research_tasks))
    x = r.context[c.research_id]
    assert c.research == "ready" and x.kind == "research" and x.comment_id == c.id and x.cite
    assert x.sources[0]["domain"] == "espn.com" and x.search_suggestions and desk.asked == ["Zion Williamson 2026-27 fantasy"]
    assert r._is_stale(meanwhile)                      # the answer goes in now, not a segment later
    ctx = r._context()[0]
    assert ctx.promised[0].research.id == x.id and ctx.research_left == Settings().research_max_per_show - 1
    assert "research came back" in prompts.writer_input(ctx, r.plan)
    assert "research the show looked up; sources: espn.com" in prompts.format_context([x])
    answer = _draft(r, segment=_seg("Marc, ESPN has Zion at 24 a night.", addressed_comment_ids=[c.id]))
    r._commit(answer)
    assert c.status == "on_deck" and x.status == "used" and x.used_seq == answer.seq


@check("research: a lookup that fails lets the hosts answer from what they know")
async def _():
    r = _runner(research=True)
    r.gemini = ResearchDesk("fail")
    c, _ = r.add_comment("Jo", "Latest injury news?", "guest")
    r._commit(_draft(r, segment=_seg("Checking!", research_requests=[ResearchRequest(comment_id=c.id, query="injury")])))
    await asyncio.gather(*list(r._research_tasks))
    assert c.research == "failed" and r._context()[0].promised[0].research_failed
    assert "came back empty" in prompts.writer_input(r._context()[0], r.plan)


@check("research: off, nothing is looked up, and the schemas don't offer it")
def _():
    r = _runner(research=False)
    c, _ = r.add_comment("Jo", "Stats?", "guest")
    r._commit(_draft(r, segment=_seg("Let me check!", research_requests=[ResearchRequest(comment_id=c.id, query="stats")])))
    assert c.status == "promised" and c.research == "failed" and not r._research_tasks
    assert r._context()[0].research_left == 0 and not r._context()[0].research_on
    assert "research_requests" not in prompts.segment_schema(["A"], 5)["properties"]
    assert "research_requests" in prompts.segment_schema(["A"], 5, research=True)["properties"]
    assert "deferred_comment_ids" in prompts.segment_schema(["A"], 5)["properties"]
    assert "research" not in prompts.plan_schema(1)["properties"]["beats"]["items"]["properties"]
    assert "research" in prompts.plan_schema(1, research=True)["properties"]["beats"]["items"]["properties"]
    on = ShowRequest(source="x", style="s", hosts=[{"name": "A"}], research=True)
    assert "RESEARCH IS ON" in prompts.planner_system(on, "x", 3) and "RESEARCH IS ON" not in prompts.planner_system(r.req, "x", 3)


@check("research: thin chapters are looked up ahead (not the first), capped, and wait for their chapter")
async def _():
    r = _runner(research=True)
    r.gemini = ResearchDesk()
    for i, b in enumerate(r.plan.beats):
        b.research = f"query {i}"
    r._research_thin_beats()
    await asyncio.gather(*list(r._research_tasks))
    found = sorted(r.context.values(), key=lambda x: x.for_beat)
    assert [x.for_beat for x in found] == [1, 2, 3][:Settings().research_max_beats]
    one = found[0]
    assert r._due_context() == ([], [])              # beat 0: nothing for chapter 2 yet
    r.beat = 1
    assert one in r._due_context()[1]               # its chapter: work it in
    r.beat = 2
    assert one in r._due_context()[0]               # past it: bring it in now
    r2 = _runner(research=True)
    r2.gemini = ResearchDesk("hang")
    r2._lookups = Settings().research_max_per_show
    c, _ = r2.add_comment("Jo", "Q", "guest")
    r2._commit(_draft(r2, segment=_seg("Hmm!", research_requests=[ResearchRequest(comment_id=c.id, query="q")])))
    assert c.research == "failed" and not r2._research_tasks    # over the cap: no lookup


@check("credits: a pod with Research on is refunded at the rate it paid")
async def _():
    calls = []

    class Store:
        async def adjust_credits(self, uid, amount, **kw):
            calls.append(amount)

    doc = {"id": "p", "owner_uid": "u", "credits_charged": 8, "credits_per_minute": 1.5}
    assert await refund_unused(Store(), Settings(), doc, 120.0) == 8 - 3   # 2 minutes at 1.5
    assert await refund_unused(Store(), Settings(), {**doc, "credits_per_minute": None}, 120.0) == 8 - 2
    assert calls == [5, 6]


@check("cold open follows renamed hosts; an unknown speaker drops it")
def _():
    from app.llm import _cast_cold_open
    hosts = [Host(name="Ana", persona="", voice="Kore"), Host(name="Ben", persona="", voice="Puck")]
    lines = [Line(speaker="kayla", text="Hi!"), Line(speaker="Ben", text="Hey.")]
    assert [ln.speaker for ln in _cast_cold_open(lines, ["Kayla", "Brit"], hosts)] == ["Ana", "Ben"]
    assert _cast_cold_open([Line(speaker="Narrator", text="Once...")], ["Kayla", "Brit"], hosts) == []


@check("a hung writer call is abandoned and retried")
async def _():
    import app.show as show_mod
    r = _runner()
    calls = []

    class HangsOnce:
        async def write_segment(self, *args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                await asyncio.sleep(3600)
            return SegmentDraft(lines=[Line(speaker="A", text="back")])

    r.gemini = HangsOnce()
    old, show_mod.WRITE_TIMEOUT_SECONDS = show_mod.WRITE_TIMEOUT_SECONDS, 0.2
    try:
        draft = await asyncio.wait_for(r._start_writing(), 5)
    finally:
        show_mod.WRITE_TIMEOUT_SECONDS = old
    assert len(calls) == 2 and draft.segment.lines[0].text == "back"


@check("speech that stops streaming is skipped, not waited on forever")
async def _():
    import app.show as show_mod
    r = _runner()

    class Stalls:
        async def stream_speech(self, lines, hosts):
            yield b"\x00\x00" * 100
            await asyncio.sleep(3600)
            yield b""

    r.gemini = Stalls()
    old, show_mod.SPEECH_STALL_SECONDS = show_mod.SPEECH_STALL_SECONDS, 0.2
    try:
        d = _draft(r)
        r._commit(d)
        await asyncio.wait_for(r._speak(d), 5)
    finally:
        show_mod.SPEECH_STALL_SECONDS = old
    assert any(e["type"] == "error" and "no audio" in e["message"] for e in r.room.events)


@check("a line whose speech goes quiet before any audio is asked for again, not skipped")
async def _():
    import app.show as show_mod
    r = _runner()
    calls = []

    class QuietOnce:
        async def stream_speech(self, lines, hosts):
            calls.append(lines[0].text)
            if len(calls) == 1:          # the API goes quiet on this request
                await asyncio.sleep(3600)
            yield b"\x00\x00" * 2400

    r.gemini = QuietOnce()
    old, show_mod.SPEECH_STALL_SECONDS = show_mod.SPEECH_STALL_SECONDS, 0.3
    try:
        d = _draft(r, segment=SegmentDraft(lines=[Line(speaker="A", text="only line")]))
        r._commit(d)
        await asyncio.wait_for(r._speak(d), 5)
    finally:
        show_mod.SPEECH_STALL_SECONDS = old
    assert calls == ["only line", "only line"]
    assert not any(e["type"] == "error" for e in r.room.events)
    assert [e["samples"] for e in r.room.events if e["type"] == "segment_end"] == [2400]


@check("a plan made beforehand skips planning; speech that never started gets another try")
async def _():
    speech = []

    class Stub:
        async def plan_show(self, *args):
            raise AssertionError("planned again")

        async def write_segment(self, *args, **kwargs):
            return SegmentDraft(lines=[Line(speaker="A", text="hi")] * 3, beat_complete=True)

        async def stream_speech(self, lines, hosts):
            speech.append(1)
            if len(speech) == 1:
                raise RuntimeError("503 overloaded")
            yield b"\x00\x00" * 2400

    req = ShowRequest(source="x", style="calm", hosts=[{"name": "A"}], minutes=2, mode="recorded")
    plan = ShowPlan(title="T", tagline="t", hosts=[Host(name="A", persona="", voice="Kore")],
                    beats=[Beat(title="b1", notes="", minutes=0.01), Beat(title="b2", notes="", minutes=0.01)])
    room = StubRoom()
    runner = ShowRunner(req, "x", Stub(), Settings(), room, plan=plan, speech_retries=1)
    assert await asyncio.wait_for(runner.run(), 10) == "signed_off"
    assert [e["title"] for e in room.events if e["type"] == "plan"] == ["T"]
    assert not any(e.get("state") == "planning" for e in room.events)
    # Each line is voiced on its own: segment 1's first line fails, is tried again, then its
    # other two lines; segment 2's three lines.
    assert not any(e["type"] == "error" for e in room.events) and len(speech) == 7
    gap = int(0.12 * 24_000)
    assert [e["samples"] for e in room.events if e["type"] == "segment_end"] == [3 * 2400 + 2 * gap] * 2


@check("each line is voiced in its own speaker's voice, and announced where it really starts")
async def _():
    calls = []

    class Stub:
        async def plan_show(self, *args):
            raise AssertionError("planned again")

        async def write_segment(self, *args, **kwargs):
            return SegmentDraft(lines=[Line(speaker="Mia", text="Is that coffee mine?"),
                                       Line(speaker="Jordan", text="Absolutely not, I named it."),
                                       Line(speaker="Mia", text="You named your coffee?")], beat_complete=True)

        async def stream_speech(self, lines, hosts):
            assert len(lines) == 1 and len(hosts) == 1
            calls.append((lines[0].speaker, hosts[0].voice))
            n = {"Mia": 3000, "Jordan": 7000}[lines[0].speaker]   # the voices speak at their own pace
            yield b"\x00\x00" * (n // 2)
            await asyncio.sleep(0)
            yield b"\x00\x00" * (n - n // 2)

    req = ShowRequest(source="x", style="calm", hosts=[{"name": "Mia"}, {"name": "Jordan"}], minutes=2, mode="recorded")
    plan = ShowPlan(title="T", tagline="t", hosts=[Host(name="Mia", persona="", voice="Aoede"),
                                                   Host(name="Jordan", persona="", voice="Algieba")],
                    beats=[Beat(title="b1", notes="", minutes=0.01)])
    room = StubRoom()
    runner = ShowRunner(req, "x", Stub(), Settings(), room, plan=plan)
    assert await asyncio.wait_for(runner.run(), 10) == "signed_off"
    seg = next(e["seq"] for e in room.events if e["type"] == "segment")
    assert sorted(calls[:3]) == sorted([("Mia", "Aoede"), ("Jordan", "Algieba"), ("Mia", "Aoede")])
    gap = int(0.12 * 24_000)
    starts = [e["at"] for e in room.events if e["type"] == "line" and e["seq"] == seg]
    assert starts == [0, 3000 + gap, 3000 + gap + 7000 + gap], starts   # not a share of the characters
    # an interruption cuts at the line the listener was really in
    from app.show import _lines_heard
    lines = [Line(speaker="A", text="x" * 10), Line(speaker="B", text="y" * 90)]
    assert _lines_heard(lines, 0.2, [0, 9000], 8000) == 1      # still in the short first line
    assert _lines_heard(lines, 0.2) == 2                        # the character estimate would say the second


@check("beats get their start and end from the chapters")
def _():
    from app.room import beats_with_times
    beats = [{"title": "a"}, {"title": "b"}, {"title": "c"}]
    live = beats_with_times(beats, {0: 0, 1: 100}, end=None)
    assert (live[0]["start"], live[0]["end"], live[1]["start"], live[1]["end"]) == (0, 100, 100, None)
    assert "start" not in live[2] and "start" not in beats[0]           # the plan's own beats are untouched
    assert beats_with_times(beats, {0: 0, 1: 100}, end=250)[1]["end"] == 250
def _voiced(r: ShowRunner, texts: list[str], seconds: float, **segment) -> Draft:
    """Commit a segment and pretend it was voiced: `seconds` of audio, evenly spread."""
    d = _draft(r, segment=SegmentDraft(lines=[Line(speaker="A", text=t) for t in texts], **segment))
    r._commit(d)
    n = int(seconds * 24_000)
    r.room.clock.add_audio(n)
    d.samples = n
    return d


@check("interrupting mid-segment keeps only what was heard")
async def _():
    from app.show import Interrupt
    r = _runner()
    _voiced(r, ["one", "two", "three", "four"], 4)
    c, _ = r.add_comment("Jo", "hi", "guest")
    d2 = _draft(r, segment=SegmentDraft(lines=[Line(speaker="A", text=t) for t in ("five", "six", "seven", "eight")],
                                        addressed_comment_ids=[c.id]))
    r._commit(d2)
    r.room.clock.add_audio(4 * 24_000)
    d2.samples = 4 * 24_000
    assert c.status == "on_deck"
    it = Interrupt(kind="ask", text="why?", at=6 * 24_000)       # halfway through segment 2
    r._truncate(it)
    assert [ln.text for ln in r.transcript] == ["one", "two", "three", "four", "five", "six"]
    assert it.cut_line == "six" and r.room.clock.audio_end == 6 * 24_000
    ev = r.room.events[-1]
    assert ev["type"] == "truncate" and ev["keep_seq"] == 2 and [ln["text"] for ln in ev["cut"]["lines"]] == ["five", "six"]
    assert c.status == "queued"                                   # "Jo" was in the part they never heard
    # cutting into segment 1 drops segment 2 altogether
    r._truncate(Interrupt(kind="move_on", at=1 * 24_000))
    assert [ln.text for ln in r.transcript] == ["one"] and r.seq == 1 and r.room.events[-1]["keep_seq"] == 1


@check("the cut follows the captions' line weighting")
def _():
    from app.show import _lines_heard
    lines = [Line(speaker="A", text=t) for t in ("short", "x" * 100, "<laugh> |no way| also short")]
    assert _lines_heard(lines, 0.01) == 1
    assert _lines_heard(lines, 0.2) == 2       # the long line starts early and runs most of the way
    assert _lines_heard(lines, 0.99) == 3


@check("go deeper, move on and steer keep the episode's length")
def _():
    r = _runner()                               # 4 one-minute beats, 4 minutes
    r.room.clock.add_audio(int(0.5 * 60 * 24_000))
    total = lambda: round(sum(b.minutes for b in r.plan.beats), 6)  # noqa: E731
    r._extend_current_beat(1.5)
    assert r.plan.beats[0].minutes == 2.5 and total() == 4.0
    assert r._end_current_beat() and r.beat == 1 and total() == 4.0
    r._steer_to([Beat(title="new", notes="", minutes=5), Beat(title="wrap", notes="", minutes=5)])
    assert [b.title for b in r.plan.beats[2:]] == ["new", "wrap"] and r.beat == 2
    elapsed = r._elapsed_minutes()
    assert abs(sum(b.minutes for b in r.plan.beats[2:]) - (4 - elapsed)) < 0.05


@check("the writer answers an interruption, then picks the thread back up")
def _():
    from app.models import InterruptNote
    r = _runner()
    ctx = r._context()[0].model_copy(update={"interjection": InterruptNote(kind="ask", text="why?", cut_line="six")})
    text = prompts.writer_input(ctx, r.plan)
    assert "THE PRODUCER JUST INTERRUPTED" in text and 'They asked: "why?"' in text and '"six"' in text
    assert "Write at most" in text and "LISTENER COMMENTS" not in text
    r._bridge = InterruptNote(kind="ask", text="why?")
    assert "pick the show back up" in prompts.writer_input(r._context()[0], r.plan)


@check("call-ins get an ETA on the show timeline")
async def _():
    rate = 24_000
    r = _runner()
    r.room.clock.set_listening(1)
    r.room.clock.add_audio(30 * rate)        # 30 s queued: the next segment can still take it
    first, _ = r.add_comment("Jo", "one", "guest")
    second, _ = r.add_comment("Ana", "two", "guest")
    third, _ = r.add_comment("Bo", "three", "guest")
    assert first.eta == second.eta == 30 * rate         # the very next segment
    assert third.eta == (30 + 20 + 2 * 10) * rate       # behind a segment full of call-ins
    late = _runner()
    late.room.clock.set_listening(1)
    late.room.clock.add_audio(5 * rate)      # 5 s left: too late for the next segment
    c, _ = late.add_comment("Jo", "hi", "guest")
    assert c.eta == (5 + 20) * rate


@check("late input waits for the next segment instead of causing dead air")
async def _():
    r = _runner()
    d = _draft(r)
    r.add_comment("Jo", "hello", "guest")
    assert await r._wait_for_slot(d) == "stale"          # nobody playing: no rush, rewrite
    r.room.clock.set_listening(1)
    r.room.clock.add_audio(5 * 24_000)
    assert await r._wait_for_slot(d) == "ready"          # 5 s left can't fit a rewrite: voice it
    assert r._queued_comments()                          # ...and the call-in waits for the next one
    r.room.clock.add_audio(60 * 24_000)
    assert await r._wait_for_slot(d) == "stale"          # a minute of audio left: rewrite


@check("next-chapter context comes due when the beat moves on")
async def _():
    r = _runner()
    item = r.add_context(ContextItem(kind="text", name="n", text="t", when="next"))
    assert r._due_context() == ([], [])
    r.beat = 1
    assert r._due_context() == ([], [item])
    r.beat = 2
    assert r._due_context() == ([item], [])


@check("beats can't wrap early unless the show runs long")
async def _():
    r = _runner()  # 4 one-minute beats at 8.5 s a line: about 7 lines each

    def lines(n):
        return SegmentDraft(lines=[Line(speaker="A", text="hi")] * n, beat_complete=True)

    r._commit(_draft(r, segment=lines(2)))
    assert r.beat == 0                       # a minute planned, seconds used: held open
    r.room.clock.add_audio(55 * 24_000)
    r._commit(_draft(r, segment=lines(2)))
    assert r.beat == 1                       # 5 s of its minute left: under half a segment, move on
    r.room.clock.add_audio(95 * 24_000)
    r._commit(_draft(r, segment=lines(1)))
    assert r.beat == 2                       # 2:30 in, past the plan's 2:00 for beat 2: let it go


@check("segments aim at the configured length (20 s)")
def _():
    r = _runner()
    r.sec_per_line = 15.0                    # a solo deep dive
    assert r._segment_lines() == 2
    r.sec_per_line = 4.0                     # quick banter
    assert r._segment_lines() == Settings().lines_per_segment
    ctx = r._context()[0]
    assert ctx.words_wanted == 50 and "about 50 words" in prompts.writer_input(ctx, r.plan)
    r.add_comment("Jo", "hello", "guest")    # room to read and answer a call-in
    assert r._context()[0].words_wanted == 100


@check("comments are capped, deduped and attachments ride along")
async def _():
    r = _runner()
    c1, _ = r.add_comment("Jo", "hi", "guest", client_id="abc")
    again, _ = r.add_comment("Jo", "hi", "guest", client_id="abc")
    assert again is c1
    item = r.add_context(ContextItem(kind="photo", name="tree.png", text="a tree", when="now"))
    c2, _ = r.add_comment("Ana", "use this", "guest", context_id=item.id)
    r.link_comment(item, c2)
    ctx, offered_comments, offered_context, _ = r._context()
    assert ctx.context_now == [] and item.id in offered_context
    assert ctx.comments[1].attachment and ctx.comments[1].attachment.name == "tree.png"


# ---- rendering, mixing and music ---------------------------------------------------------

def _tone(seconds: float, rate: int, amp: float, freq: float = 30.0) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _pcm(x: np.ndarray) -> bytes:
    return np.rint(np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


def _mp3(seconds: float, rate: int = 24_000) -> bytes:
    from app.mixer import encode_mp3
    return encode_mp3(_pcm(_tone(seconds, rate, 0.3, 220)), rate, 64)


@check("a render's playhead keeps up with its audio")
def _():
    from app.render import RenderRoom
    voiced = []
    room = RenderRoom(1000, on_voiced=voiced.append)
    room.publish({"type": "chapter", "i": 0, "start": 0})
    room.publish({"type": "segment", "seq": 1, "start": 0, "lines": []})
    room.push_audio(1, b"\x01\x00" * 500)
    assert room.clock.ahead_s() == 0 and room.clock.live == 500     # never waits for a listener
    room.publish({"type": "segment_end", "seq": 1, "samples": 500})
    room.publish({"type": "error", "fatal": False, "message": "skipped"})
    assert room.segment_list()[0]["samples"] == 500 and room.chapters == {0: 0}
    assert len(room.voice) == 1000 and voiced == [0.5] and room.errors == ["skipped"] and room.fatal is None


@check("a show renders start to finish with nobody listening")
async def _():
    from app.render import RenderRoom

    class Stub:
        async def write_segment(self, *args, **kwargs):
            return SegmentDraft(lines=[Line(speaker="A", text="hello there")] * 3, beat_complete=True)

        async def stream_speech(self, lines, hosts):
            for _ in range(4):
                yield b"\x00\x01" * 6000            # a second of audio per segment, in pieces

    req = ShowRequest(source="x", style="calm", hosts=[{"name": "A"}], minutes=2, mode="recorded")
    plan = ShowPlan(title="T", tagline="t", hosts=[Host(name="A", persona="", voice="Kore")],
                    beats=[Beat(title=f"b{i}", notes="", minutes=0.01) for i in range(3)],
                    cold_open=[Line(speaker="A", text="Cold open.")])
    room = RenderRoom(24_000)
    runner = ShowRunner(req, "x", Stub(), Settings(), room, plan=plan)
    assert await asyncio.wait_for(runner.run(), 10) == "signed_off"
    segs = room.segment_list()
    assert segs[0]["lines"][0]["text"] == "Cold open." and segs[-1]["final"] and len(segs) >= 3
    assert all(b["start"] == a["start"] + a["samples"] for a, b in zip(segs, segs[1:]))    # contiguous
    assert sorted(room.chapters) == [0, 1, 2] and set(room.chapters.values()) <= {s["start"] for s in segs}
    assert len(room.voice) // 2 == sum(s["samples"] for s in segs) == room.clock.audio_end


@check("the mixer opens on the theme, stings a chapter, plays out, and says where everything moved")
def _():
    from app.mixer import INTRO_LEAD_S, OUTRO_PLAYOUT_S, STING_S, Cues, mix
    rate = 1000
    cues = Cues(intro=_tone(14, rate, 0.1), sting=_tone(3, rate, 0.1), outro=_tone(20, rate, 0.1))
    chapters = {0: 0, 1: 60 * rate, 2: 90 * rate}       # chapter 2 comes too soon after 1 for a sting
    m = mix(_pcm(_tone(120, rate, 0.3, 50)), chapters, cues, rate)
    intro, sting, playout = int(INTRO_LEAD_S * rate), int(STING_S * rate), int(OUTRO_PLAYOUT_S * rate)
    assert [(i.kind, i.at) for i in m.inserts] == [("intro", 0), ("sting", 60 * rate), ("outro", 120 * rate)]
    assert m.samples == 120 * rate + intro + sting + playout
    assert m.shift(0) == intro and m.shift(60 * rate - 1) == 60 * rate - 1 + intro
    assert m.shift(60 * rate) == 60 * rate + intro + sting
    assert m.chapter_start(0, 0) == 0 and m.chapter_start(1, 60 * rate) == 60 * rate + intro   # the sting opens it
    assert m.chapter_start(2, 90 * rate) == m.shift(90 * rate)
    assert [r["kind"] for r in m.regions] == ["intro", "sting", "outro"]
    assert (m.regions[1]["start"], m.regions[1]["end"]) == (60 * rate + intro, 60 * rate + intro + sting)
    assert m.regions[2]["end"] == m.samples


@check("music ducks under speech, and the limiter keeps peaks down")
def _():
    from app.mixer import DUCK_DB, INTRO_LEAD_S, PEAK, Cues, from_db, mix
    rate = 1000
    lead = int(INTRO_LEAD_S * rate)
    out = np.frombuffer(mix(bytes(2 * 60 * rate), {0: 0}, Cues(intro=_tone(14, rate, 0.1)), rate).pcm, "<i2") / 32768
    alone, under = np.abs(out[1000:lead - 500]).max(), np.abs(out[lead + 1000:lead + 4000]).max()
    assert math.isclose(under / alone, from_db(DUCK_DB), rel_tol=0.05)
    loud = _pcm(np.sign(_tone(30, rate, 1.0)))                       # a full-scale square wave
    pcm = mix(loud, {0: 0}, Cues(intro=_tone(14, rate, 0.9)), rate).pcm
    assert np.abs(np.frombuffer(pcm, "<i2")).max() / 32767 <= PEAK + 1e-4


@check("with no music the voice is only levelled")
def _():
    from app.mixer import SPEECH_RMS_DBFS, active_rms, from_db, mix
    rate = 1000
    m = mix(_pcm(_tone(10, rate, 0.05, 50)), {0: 0}, None, rate)
    assert m.inserts == [] and m.regions == [] and m.samples == 10 * rate and m.shift(500) == 500
    out = np.frombuffer(m.pcm, "<i2").astype(np.float32) / 32768
    assert math.isclose(active_rms(out, rate), from_db(SPEECH_RMS_DBFS), rel_tol=0.05)


@check("a theme becomes an intro, its punchiest bit and an outro, levelled")
def _():
    from app.mixer import MUSIC_RMS_DBFS, STING_S, active_rms, from_db
    from app.music import cues_from_theme
    rate = 1000
    c = cues_from_theme(_tone(30, rate, 0.5), rate)
    assert (len(c.intro), len(c.outro), len(c.sting)) == (14 * rate, 20 * rate, int(STING_S * rate))
    assert math.isclose(active_rms(c.intro, rate), from_db(MUSIC_RMS_DBFS), rel_tol=0.05)
    theme = _tone(30, rate, 0.05)
    theme[15 * rate:18 * rate] *= 4                                                   # the punchy bit
    punchy = cues_from_theme(theme, rate)
    assert np.abs(punchy.sting).max() > 3 * np.abs(punchy.intro).max()
    assert len(cues_from_theme(theme, rate, {"sting": [1, 2]}).sting) == rate          # a hand-picked window


@check("the music library picks by mood, falls back, skips what it can't play, and works empty")
def _():
    from app.music import Library
    d = Path(tempfile.mkdtemp(prefix="music-"))
    assert not Library.load(d).available and Library.load(d).pick("warm") is None
    for name in ("a.mp3", "b.mp3", "c.mp3"):
        (d / name).write_bytes(_mp3(1))
    packs = [{"id": "pop", "mood": "upbeat", "file": "a.mp3"},
             {"id": "folk", "mood": "warm", "also": ["calm"], "file": "b.mp3"},
             {"id": "folk2", "mood": "warm", "file": "c.mp3"},
             {"id": "ghost", "mood": "tech", "file": "missing.mp3"},
             {"id": "broken"}]
    (d / "manifest.json").write_text(json.dumps({"version": 1, "packs": packs}), encoding="utf-8")
    lib = Library.load(d)
    assert [p.id for p in lib.packs] == ["pop", "folk", "folk2"] and lib.available
    assert lib.pick("upbeat").id == "pop" and lib.pick("calm").id == "folk" and lib.pick("tech") is not None
    assert lib.pick("warm", seed="pod1").id == lib.pick("warm", seed="pod1").id
    assert {lib.pick("warm", seed=s).id for s in "abcdefgh"} == {"folk", "folk2"}        # ties are spread
    cues = lib.cues(lib.get("pop"), 24_000)
    assert abs(len(cues.intro) - 24_000) < 3_000 and lib.cues(lib.get("pop"), 24_000) is cues   # decoded once


@check("every music mood has a sound, and prompts never take user text")
def _():
    from app.catalog import MUSIC_MOODS
    from app.music import MOOD_MUSIC, music_prompt
    assert set(MOOD_MUSIC) == set(MUSIC_MOODS)
    assert all(music_prompt(m).startswith("Instrumental only, no vocals.") for m in MUSIC_MOODS)
    assert music_prompt("Taylor Swift") == music_prompt("warm")


# ---- voice memos: the brief, live research, and the episode's source -------------------

def _brief(**kw) -> "MemoBrief":
    from app.models import MemoBrief
    base = dict(usable=True, transcript="I keep wondering why the city sounds louder at night.",
                working_title="Louder at night", thesis="Night sounds louder.", key_ideas=["It feels louder"],
                questions=["Is it louder?", "Does it hurt sleep?"], research_queries=["night sound"])
    return MemoBrief(**{**base, **kw})


@check("a memo brief stays inside the catalog and the limits")
def _():
    from app.llm import _tidy_brief
    b = _tidy_brief(_brief(music_mood="Taylor Swift", format="huberman-lab",
                           research_queries=[" a ", "a", "", "b", "c", "d", "e", "f"]), max_queries=4)
    assert (b.music_mood, b.format, b.research_queries) == ("warm", "idea-breakdown", ["a", "b", "c", "d"])
    empty = _tidy_brief(_brief(transcript="  "), max_queries=4)
    assert not empty.usable and empty.reason


@check("a grounded answer's searches, cited pages and suggestions come from its steps")
def _():
    from app.llm import grounding
    note = lambda url, title="": {"type": "url_citation", "url": url, "title": title}  # noqa: E731
    interaction = {"steps": [
        {"type": "user_input", "content": [{"type": "text", "text": "q"}]},
        {"type": "google_search_call", "id": "1", "arguments": {"queries": ["night noise", "night noise ", "sleep"]}},
        {"type": "google_search_result", "call_id": "1", "result": [{"search_suggestions": f"<b>{i}</b>"} for i in range(7)]},
        {"type": "model_output", "content": [{"type": "text", "text": "...", "annotations": [
            note("https://www.who.int/x", "WHO guidelines"), note("https://www.who.int/x", "again"),
            note("https://vertexaisearch.cloud.google.com/grounding-api-redirect/Ab1", "nature.com"),
            note("javascript:alert(1)", "evil"), note("https:///nohost"), {"type": "place_citation"}]}]},
    ]}
    queries, sources, suggestions = grounding(interaction)
    assert queries == ["night noise", "sleep"] and len(suggestions) == 5
    assert [(s["domain"], s["title"]) for s in sources] == [("who.int", "WHO guidelines"), ("nature.com", "nature.com")]


@check("research findings are numbered, and malformed ones dropped")
def _():
    from types import SimpleNamespace
    from app.llm import _dossier
    interaction = SimpleNamespace(model_dump=lambda mode=None: {"steps": []})
    data = {"summary": " S ", "findings": [
        {"claim": "A.", "outlet": "Reuters", "confidence": "solid"}, "not a finding",
        {"claim": "B.", "outlet": " ", "confidence": "solid"}, {"claim": "C.", "outlet": "AP", "confidence": "odd"},
        {"claim": "D.", "outlet": "BBC", "confidence": "thin", "answers": [2]}], "disagreements": ["x", " "]}
    d = _dossier(data, interaction, searched_at=5.0)
    assert d.ok and [(f.id, f.outlet) for f in d.findings] == [(1, "Reuters"), (2, "BBC")]
    assert d.summary == "S" and d.disagreements == ["x"] and d.searched_at == 5.0
    assert not _dossier({"findings": []}, interaction, 0).ok


@check("the episode's source carries the memo, then each finding with its outlet")
def _():
    from app.models import Finding, ResearchDossier
    dossier = ResearchDossier(ok=True, summary="Mostly true.", searched_at=1_760_000_000, findings=[
        Finding(id=1, claim="Inversions carry sound.", outlet="Acoustical Society", date="2023", answers=[1]),
        Finding(id=2, claim="It's debated.", outlet="Nature", confidence="mixed"),
        Finding(id=3, claim="Left out.", outlet="Blog")], unanswered=["Why exactly?"])
    s = prompts.memo_source(_brief(), dossier, "Ana", excluded={3})
    assert s.startswith("VOICE MEMO from Ana") and "ANA'S QUESTIONS:\n1. Is it louder?" in s
    assert "[R1] Inversions carry sound. — Acoustical Society (2023) (question 1)" in s
    assert "[R2] It's debated. — Nature [sources disagree]" in s and "Left out" not in s
    assert "STILL UNCLEAR:\n- Why exactly?" in s and "October" in s
    fallback = prompts.memo_source(_brief(), ResearchDossier(ok=False), "Ana")
    assert "LIVE RESEARCH: none came back" in fallback and "[R" not in fallback


@check("memo prompts and schemas offer exactly the catalog's formats and moods")
def _():
    from app.catalog import MUSIC_MOODS, TEMPLATES
    schema = prompts.memo_schema(5)["properties"]
    assert schema["format"]["enum"] == [t["id"] for t in TEMPLATES] and schema["music_mood"]["enum"] == MUSIC_MOODS
    assert schema["research_queries"]["maxItems"] == 5 and "2 to 5 web searches" in prompts.memo_system(5)
    assert all(t["id"] in prompts.memo_system(5) for t in TEMPLATES)
    assert prompts.memo_research_schema()["properties"]["findings"]["items"]["required"] == ["claim", "outlet", "confidence"]
    assert ("plain notes" in prompts.memo_research_system(5, notes=True)
            and "plain notes" not in prompts.memo_research_system(5))
    direction = prompts.memo_direction("Ana")
    assert "Ana's memo" in direction and "pre-recorded" in direction and len(direction) < 4_000


# ---- making memo pods, and every pod's intro ------------------------------------------------

def _music_library(moods=("warm", "calm")) -> "Library":
    from app.music import Library
    d = Path(tempfile.mkdtemp(prefix="music-"))
    packs = []
    for mood in moods:
        (d / f"{mood}.mp3").write_bytes(_mp3(30))
        packs.append({"id": f"{mood}-pack", "label": mood.title(), "mood": mood, "file": f"{mood}.mp3"})
    (d / "manifest.json").write_text(json.dumps({"version": 1, "packs": packs}), encoding="utf-8")
    return Library.load(d)


@check("memo pods: talk time leaves room for the music; credit goes to a first name")
def _():
    from app.memo import credit_name, voice_minutes
    assert voice_minutes(15, True) == 14 and voice_minutes(15, False) == 15 and voice_minutes(2, True) == 2
    assert credit_name("", "Ana Lopez") == "Ana" and credit_name(" DJ Ana ", "x") == "DJ Ana"
    assert credit_name("", " ") == "the creator"


@check("a memo pod's format: your template, else your vibe or style, else the memo's pick")
def _():
    from app.catalog import PRESETS_BY_ID, TEMPLATES_BY_ID
    from app.memo import build_request
    from app.models import MemoOptions
    brief = _brief(format="debate-night", language="Spanish")
    intro = {"show_name": "Walk & Talk", "intro_line": "ideas from the sidewalk"}
    r = build_request(brief, MemoOptions(), "SRC", credit="Ana", intro=intro)
    assert r.style == TEMPLATES_BY_ID["debate-night"]["style"] and [h.name for h in r.hosts] == ["Theo", "Ruby"]
    assert (r.mode, r.language, r.show_name, r.intro_line) == ("recorded", "Spanish", "Walk & Talk", "ideas from the sidewalk")
    assert "Ana's memo" in r.direction and r.source_names == ["Voice memo by Ana"] and r.minutes == 15
    r = build_request(brief, MemoOptions(template="couple-chat", vibe="gossip"), "SRC", credit="Ana", intro=None)
    assert r.style_label == "Married couple chat" and r.hosts[0].name == "Mia" and r.show_name == ""
    r = build_request(brief, MemoOptions(vibe="gossip"), "SRC", credit="Ana", intro=None)
    assert r.vibe == "gossip" and r.style == PRESETS_BY_ID["gossip"]["prompt"] and r.hosts[0].name == "Theo"
    r = build_request(brief, MemoOptions(style="Very calm.", hosts=[{"name": "Solo"}], minutes=5), "SRC",
                      credit="Ana", intro=None)
    assert r.style == "Very calm." and [h.name for h in r.hosts] == ["Solo"] and r.minutes == 5


@check("outline edits keep beat lengths, and a changed outline loses its cold open")
def _():
    from app.memo import apply_edits
    from app.models import ProduceMemo
    plan = ShowPlan(title="T", tagline="t", hosts=[Host(name="A", persona="", voice="Kore")],
                    beats=[Beat(title="Hook", notes="n1", minutes=2), Beat(title="Middle", notes="n2", minutes=6),
                           Beat(title="End", notes="n3", minutes=2)], cold_open=[Line(speaker="A", text="Hi")])
    same = [{"title": b.title, "notes": b.notes} for b in plan.beats]
    kept = apply_edits(plan, ProduceMemo(title="New title", beats=same))
    assert kept.title == "New title" and kept.cold_open and plan.title == "T"
    edited = apply_edits(plan, ProduceMemo(beats=[same[0], {"title": "Brand new", "notes": "x"}, same[2]]))
    assert [b.minutes for b in edited.beats] == [2, 10 / 3, 2] and edited.cold_open == []


async def _memo_world(credits: int = 30):
    from app.memo import Renders
    s = LocalStore(tempfile.mkdtemp(prefix="memo-"))
    acc = Accounts(s, admins=set(), signup_credits=credits)
    await acc.sign_in({"uid": "ann", "email": "ann@example.com", "name": "Ann Lee"})
    return s, acc, Renders(1)


@check("settling a pod's credits is worked out from its ledger, so it never refunds twice")
async def _():
    from app.memo import settle
    s, acc, _ = await _memo_world()
    await acc.adjust_credits("ann", -15, reason="pod", show_id="p1")
    await acc.adjust_credits("ann", -2, reason="music", show_id="p1")
    assert await settle(s, acc, "ann", "p1", keep=11, note="x") == 6
    assert await settle(s, acc, "ann", "p1", keep=11, note="x") == 0 and (await acc.get("ann")).credits == 19
    assert await settle(s, acc, "ann", "p1", keep=0, note="x") == 11 and (await acc.get("ann")).credits == 30


@check("after a restart, memo pods being made fail with their credits back; reviews wait")
async def _():
    from app.memo import sweep
    s, acc, _ = await _memo_world()
    for sid, status, kind in (("m1", "rendering", "memo"), ("m2", "review", "memo"), ("l1", "live", "pod")):
        await s.create_show({"id": sid, "owner_uid": "ann", "status": status, "kind": kind, "render": {"stage": "voicing"}})
    await acc.adjust_credits("ann", -10, reason="pod", show_id="m1")
    assert await sweep(s, acc) == 1 and await sweep(s, acc) == 0
    m1 = await s.get_show("m1")
    assert m1["status"] == "failed" and "restarted" in m1["render"]["error"] and (await acc.get("ann")).credits == 30
    assert (await s.get_show("m2"))["status"] == "review" and (await s.get_show("l1"))["status"] == "live"


@check("renders take turns, first come first served, and say where you are in line")
async def _():
    from app.memo import Renders

    class Job:
        def __init__(self, n):
            self.id, self.uid, self.told = f"j{n}", f"u{n}", []

        def waiting(self, ahead):
            self.told.append(ahead)

    renders, order = Renders(1), []

    async def go(job, hold):
        async with renders.slot(job):
            order.append(job.id)
            await asyncio.sleep(hold)

    jobs = [Job(n) for n in range(3)]
    await asyncio.gather(go(jobs[0], 0.05), go(jobs[1], 0), go(jobs[2], 0))
    assert order == ["j0", "j1", "j2"] and set(jobs[1].told) == {0} and jobs[2].told == [1, 0]   # 1 ahead, then next


@check("a pod's theme: your saved music, a pack, or the library's pick; none when asked")
async def _():
    from app.music import theme_for
    lib = _music_library()
    s = LocalStore(tempfile.mkdtemp(prefix="theme-"))
    await s.put_user_file("ann", "music-abc.mp3", _mp3(30), "audio/mpeg")
    await s.put_item("ann", "music", {"id": "abc", "label": "Ann's theme", "file": "music-abc.mp3"})
    kw = dict(library=lib, store=s, uid="ann", seed="pod1", rate=24_000)
    cues, info = await theme_for("auto", mood="calm", **kw)
    assert info == {"source": "library", "pack_id": "calm-pack", "label": "Calm", "license": ""} and len(cues.intro)
    assert (await theme_for("warm-pack", mood="calm", **kw))[1]["pack_id"] == "warm-pack"
    cues, info = await theme_for("mine:abc", mood="calm", **kw)
    assert info["source"] == "mine" and info["label"] == "Ann's theme" and len(cues.outro)
    assert (await theme_for("mine:gone", mood="calm", **kw))[1]["source"] == "library"
    assert await theme_for("none", mood="calm", **kw) == (None, {"source": "none"})
    from app.music import Library
    assert (await theme_for("auto", **{**kw, "library": Library(Path("."), [])}, mood="calm"))[0] is None


@check("every pod's intro: the hosts welcome listeners, to your show by name if you have one")
def _():
    from app.catalog import MUSIC_MOOD_FOR, MUSIC_MOODS, PRESETS_BY_ID, TEMPLATES_BY_ID, music_mood_for
    r = _runner()
    p = prompts.planner_system(r.req, "SRC")
    assert "the episode's intro" in p and "welcome the listeners," in p and "introduce themselves" in p
    named = r.req.model_copy(update={"show_name": "Walk & Talk", "intro_line": "ideas from the sidewalk"})
    p = prompts.planner_system(named, "SRC")
    assert 'welcome the listeners to "Walk & Talk"' in p and 'keeping its words: "ideas from the sidewalk"' in p
    assert 'T (an episode of "Walk & Talk")' in prompts.writer_system(named, r.plan, "S", "STYLE", [])
    assert "SHOW: T. t" in prompts.writer_system(r.req, r.plan, "S", "STYLE", [])
    assert "welcome the listeners" in prompts.writer_input(r._context()[0], r.plan)    # no cold open: writers open
    assert set(MUSIC_MOOD_FOR) == set(PRESETS_BY_ID) | set(TEMPLATES_BY_ID) and set(MUSIC_MOOD_FOR.values()) <= set(MUSIC_MOODS)
    assert music_mood_for("gossip", "debate-night") == "newsy" and music_mood_for(None) == "warm"


@check("a live pod opens on its theme, streamed before the first segment")
async def _():
    from app.mixer import LIVE_INTRO_S, intro_sting
    from app.room import ShowRoom
    rate = 24_000
    sting = intro_sting(_tone(14, rate, 0.1), rate)
    out = np.frombuffer(sting, "<i2")
    assert len(out) == int(LIVE_INTRO_S * rate) and out[0] == 0 and abs(int(out[-1])) < 50   # fades in and out

    class Planning:
        async def plan_show(self, *args):
            await asyncio.sleep(3600)

    s = LocalStore(tempfile.mkdtemp(prefix="room-"))
    doc = {"id": "live1", "owner_uid": "ann", "vibe": None, "style_label": "", "accent": "#2F6BD0"}
    await s.create_show(doc)
    req = ShowRequest(source="x", style="calm", hosts=[{"name": "A"}], minutes=2)
    room = ShowRoom(doc=doc, request=req, source="x", gemini=Planning(), settings=Settings(), store=s,
                    on_closed=lambda _id: None, intro=sting)
    await asyncio.sleep(0.05)
    assert room.clock.audio_end == len(out) and room.pcm.since(0)[0][:2] == (0, 0) and room.segments == {}
    room.end_now("stopped")
    await asyncio.wait_for(room.finalized.wait(), 5)
    room.close()
    assert (await s.get_show("live1"))["samples"] == len(out)


class _StubGemini:
    """Every model call a memo pod makes, canned. Speech is 10 s of tone a segment."""

    def __init__(self, usable=True, music_fails=False):
        self.usable, self.music_fails, self.calls = usable, music_fails, []

    async def analyze_memo(self, audio, mime, *, creator, seconds=None):
        self.calls.append(("analyze", mime, creator))
        return _brief(usable=self.usable, reason="" if self.usable else "No speech in it.", music_mood="calm")

    async def research_memo(self, brief, *, max_queries):
        from app.models import Finding, ResearchDossier, WebSource
        self.calls.append(("research",))
        return ResearchDossier(ok=True, findings=[Finding(id=1, claim="It is louder.", outlet="Reuters")],
                               sources=[WebSource(title="r", url="https://reuters.com/x", domain="reuters.com")])

    async def plan_show(self, req, source):
        self.calls.append(("plan", req.mode, "[R1]" in source))
        return ShowPlan(title="Louder at Night", tagline="t", about="About it.",
                        hosts=[Host(name="Ivy", persona="", voice="Autonoe")],
                        beats=[Beat(title=f"b{i}", notes="", minutes=1) for i in range(3)],
                        cold_open=[Line(speaker="Ivy", text="Welcome!")])

    async def write_segment(self, ctx, req, plan, source, style, context, thinking=None):
        self.calls.append(("write", req.mode, req.minutes))
        return SegmentDraft(lines=[Line(speaker="Ivy", text="More.")] * 3, beat_complete=True)

    async def stream_speech(self, lines, hosts):
        yield _pcm(_tone(10, 24_000, 0.2, 200))

    async def generate_music(self, prompt):
        self.calls.append(("music",))
        if self.music_fails:
            raise RuntimeError("blocked")
        return _mp3(30), "audio/mpeg"


async def _make_memo_pod(gemini, *, music="auto", review=False, credits=30):
    from app.memo import MemoJob, new_doc
    from app.models import MemoOptions
    s, acc, renders = await _memo_world(credits)
    opts = MemoOptions(minutes=3, music=music, review=review)
    doc = new_doc("memo1", uid="ann", owner_name="Ann Lee", opts=opts, intro={"show_name": "", "music": "auto"},
                  memo={"file": "memo.webm", "mime": "audio/webm", "bytes": 4, "credit": "Ann"},
                  shared_with=[], album_ids=[], tags=[])
    await s.create_show(doc)
    await s.put_show_file("memo1", "memo.webm", b"\x1a\x45\xdf\xa3", "audio/webm")
    await acc.adjust_credits("ann", -3, reason="pod", show_id="memo1")
    if music == "generate":
        await acc.adjust_credits("ann", -2, reason="music", show_id="memo1")
    job = MemoJob(doc, store=s, accounts=acc, gemini=gemini, settings=Settings(), library=_music_library(),
                  renders=renders)
    job.start("prepare")
    await asyncio.wait_for(job.task, 60)
    return s, acc, doc


@check("a memo becomes a finished pod: researched, voiced, scored, every start shifted")
async def _():
    from app.mixer import INTRO_LEAD_S
    gemini = _StubGemini()
    s, acc, doc = await _make_memo_pod(gemini)
    saved = await s.get_show("memo1")
    assert saved["status"] == "finished" and saved["render"]["stage"] == "done" and saved["audio"]["bytes"] > 0
    assert ("plan", "recorded", True) in gemini.calls                    # the research reached the source
    assert {c[2] for c in gemini.calls if c[0] == "write"} == {2}         # 3 minutes asked, 2 of talk + music
    segs = (await s.load_children("memo1"))["segments"]
    assert segs[0]["start"] == int(INTRO_LEAD_S * 24_000) and segs[0]["lines"][0]["text"] == "Welcome!"
    assert saved["beats"][0]["start"] == 0 and saved["music"]["source"] == "library"
    assert saved["music"]["regions"][0]["kind"] == "intro" and saved["samples"] > sum(x["samples"] for x in segs)
    assert saved["show_notes"]["credit"] == "Made from a voice memo by Ann." and saved["show_notes"]["sources"]
    used = math.ceil(saved["duration_s"] / 60)
    assert (await acc.get("ann")).credits == 30 - used and saved["credits_refunded"] == 3 - used


@check("original music is kept for its owner; when it fails, the library steps in and credits come back")
async def _():
    s, acc, doc = await _make_memo_pod(_StubGemini(), music="generate")
    saved = await s.get_show("memo1")
    tracks = await s.list_items("ann", "music")
    assert saved["music"]["source"] == "generated" and tracks and tracks[0]["show_id"] == "memo1"
    assert await s.get_user_file("ann", tracks[0]["file"])
    assert (await acc.get("ann")).credits == 30 - math.ceil(saved["duration_s"] / 60) - 2
    s, acc, doc = await _make_memo_pod(_StubGemini(music_fails=True), music="generate")
    saved = await s.get_show("memo1")
    assert saved["music"]["source"] == "library" and (await acc.get("ann")).credits == 30 - math.ceil(saved["duration_s"] / 60)


@check("a memo held for review stops at the outline; an unusable memo fails with its credits back")
async def _():
    gemini = _StubGemini()
    s, acc, doc = await _make_memo_pod(gemini, review=True)
    saved = await s.get_show("memo1")
    assert saved["status"] == "review" and saved["plan_draft"]["title"] == "Louder at Night"
    assert not any(c[0] == "write" for c in gemini.calls) and await s.get_text("memo1", "source.txt")
    s, acc, doc = await _make_memo_pod(_StubGemini(usable=False))
    saved = await s.get_show("memo1")
    assert saved["status"] == "failed" and saved["render"]["error"] == "No speech in it."
    assert (await acc.get("ann")).credits == 30


# ---- prompts ---------------------------------------------------------------------------

@check("a chosen persona reaches the planner and survives it")
def _():
    from app.llm import _reconcile_hosts
    req = ShowRequest(source="x", style="calm", minutes=4,
                      hosts=[{"name": "Dr. Vance", "voice": "Kore", "persona": "A precise neuroscientist."}, {}])
    assert 'persona (keep it' in prompts.planner_system(req, "x") and "A precise neuroscientist." in prompts.planner_system(req, "x")
    planned = [Host(name="Z", persona="Planner idea", voice="Puck"), Host(name="Y", persona="Other", voice="Puck")]
    hosts = _reconcile_hosts(req, planned)
    assert hosts[0].persona == "A precise neuroscientist." and hosts[1].persona == "Other"
    assert hosts[0].name == "Dr. Vance" and hosts[1].voice != hosts[0].voice


@check("templates use real voices and stay within two hosts")
def _():
    from app.catalog import TEMPLATES, VOICES
    assert len(TEMPLATES) >= 6
    for t in TEMPLATES:
        assert 1 <= len(t["hosts"]) <= 2 and all(h["voice"] in VOICES for h in t["hosts"]), t["id"]
        assert t["style"] and t["minutes"] in (5, 10, 15)


@check("a template sets up a request the way the Create screen does")
def _():
    from app.catalog import TEMPLATES, VOICE_GENDER, template_request
    for t in TEMPLATES:
        fields = template_request(t["id"])
        req = ShowRequest(source="x", **fields)
        assert (req.style, req.style_label, req.accent, req.vibe) == (t["style"], t["label"], t["accent"], None)
        assert [h.name for h in req.hosts] == [h["name"] for h in t["hosts"]]
        assert all(VOICE_GENDER[h.voice] == h.gender for h in req.hosts)
    fields["hosts"][0]["name"] = "changed"
    assert TEMPLATES[-1]["hosts"][0]["name"] != "changed" and template_request("nope") is None


@check("recorded episodes drop the live-only rules and follow their direction")
def _():
    r = _runner()
    live_w = prompts.writer_system(r.req, r.plan, "SRC", "STYLE", [])
    live_p = prompts.planner_system(r.req, "SRC")
    head = prompts.source_block("SRC")
    assert live_w.startswith(head + prompts.WRITER_INTRO["live"]) and "Listener comments:" in live_w
    assert "EPISODE DIRECTION" not in live_w + live_p and "live audio show" in live_p
    rec = r.req.model_copy(update={"mode": "recorded", "direction": "Credit Ana's memo."})
    w = prompts.writer_system(rec, r.plan, "SRC", "STYLE", [])
    p = prompts.planner_system(rec, "SRC")
    assert w.startswith(head + prompts.WRITER_INTRO["recorded"]) and "Listener comments" not in w
    assert not re.search(r"\blive\b", w, re.IGNORECASE)
    assert w.index("SOURCE:") < w.index("HOSTS:") < w.index("EPISODE DIRECTION:\nCredit Ana's memo.")
    assert "pre-recorded" in p and "EPISODE DIRECTION:\nCredit Ana's memo.\n\nSTYLE:" in p


@check("every template host and premade persona has a voice of their gender")
def _():
    from app.catalog import PERSONA_PRESETS, TEMPLATES, VOICE_GENDER
    hosts = [h for t in TEMPLATES for h in t["hosts"]] + PERSONA_PRESETS
    assert all(VOICE_GENDER[h["voice"]] == h["gender"] for h in hosts), [
        h["name"] for h in hosts if VOICE_GENDER[h["voice"]] != h["gender"]]
    assert sum(g == "female" for g in VOICE_GENDER.values()) == 14 and len(VOICE_GENDER) == 30


@check("casting: voices follow gender; a voice you pick wins")
def _():
    from app.catalog import VOICE_GENDER
    from app.llm import _reconcile_hosts
    planned = [Host(name="Elena", persona="", voice="Sadaltager", gender="female"),   # planner slipped up
               Host(name="Ben", persona="", voice="Erinome", gender="male")]
    hosts = _reconcile_hosts(ShowRequest(source="x", style="y", hosts=[{}, {}]), planned)
    assert [VOICE_GENDER[h.voice] for h in hosts] == ["female", "male"] and hosts[0].voice != hosts[1].voice
    # a persona's gender beats the planner's; a chosen voice beats both
    req = ShowRequest(source="x", style="y", hosts=[{"gender": "male", "likes": "jazz"}, {"voice": "Kore"}])
    hosts = _reconcile_hosts(req, planned)
    assert hosts[0].gender == "male" and VOICE_GENDER[hosts[0].voice] == "male" and hosts[0].likes == "jazz"
    assert hosts[1].voice == "Kore" and hosts[1].gender == "female"
    # two hosts never share a voice, even when the planner gives both the same one
    same = [Host(name="A", persona="", voice="Kore", gender="female"), Host(name="B", persona="", voice="Kore", gender="female")]
    a, b = _reconcile_hosts(ShowRequest(source="x", style="y", hosts=[{}, {}]), same)
    assert a.voice == "Kore" and b.voice != "Kore" and VOICE_GENDER[b.voice] == "female"


@check("stats: unique listeners, drop-off, replays; the owner never counts")
def _():
    from app.stats import summarize
    doc = {"owner_uid": "me", "duration_s": 600, "counts": {"hearts": 7}}
    sessions = [
        {"id": "1", "kind": "live", "uid": "bo", "name": "Bo", "joined_at": 10, "left_at": 100,
         "join_pos": 0, "leave_pos": 130, "listened_s": 120},
        {"id": "2", "kind": "live", "uid": "bo", "name": "Bo", "joined_at": 200, "left_at": 700,
         "join_pos": 190, "leave_pos": 600, "listened_s": 400, "to_end": True},            # came back, stayed
        {"id": "3", "kind": "live", "uid": None, "lid": "anon1", "joined_at": 20, "left_at": 90,
         "join_pos": 10, "leave_pos": 75, "listened_s": 65},                               # left in minute 1
        {"id": "4", "kind": "live", "uid": "me", "name": "Me", "listened_s": 600},        # the owner
        {"id": "5", "kind": "replay", "uid": "cy", "name": "Cy", "joined_at": 900, "left_at": 1200,
         "join_pos": 0, "leave_pos": 300, "listened_s": 290},
    ]
    st = summarize(doc, sessions, timeline=[[0, 1], [10, 2], [20, 1]], comments=3)
    assert st["live"]["unique"] == 2 and st["live"]["signed_in"] == 1 and st["live"]["anonymous"] == 1
    assert st["live"]["peak"] == 2 and st["live"]["stayed_to_end"] == 1
    assert st["live"]["dropoff"] == [{"minute": 1, "left": 1}]                            # Bo came back, so only anon left
    assert st["live"]["listened_s"] == 585 and st["replays"]["plays"] == 1 and st["replays"]["avg_completion"] == 0.5
    names = [p["name"] for p in st["people"]]
    assert names == ["Bo", "Cy"] and "Me" not in names and st["engagement"]["hearts"] == 7


@check("writer system puts the source before the style, and context before the style")
def _():
    r = _runner()
    items = [ContextItem(id=1, kind="photo", name="tree.png", text="TREE", when="background", added_by="Ana")]
    s = prompts.writer_system(r.req, r.plan, "SOURCE TEXT", "THE STYLE", items)
    assert s.index("SOURCE TEXT") < s.index("TREE") < s.rindex("THE STYLE")
    assert "background: use silently" in s and s.rstrip().endswith("THE STYLE")


@check("the planner, the writers and the re-planner all open with the same source, for the cache")
def _():
    r = _runner()
    src = "A LONG SOURCE {with braces} " * 50
    head = prompts.source_block(src)
    calls = [prompts.planner_system(r.req, src), prompts.writer_system(r.req, r.plan, src, "STYLE", []),
             prompts.replan_system(r.req, r.plan, src, "STYLE", "go left", 4.0, 1)]
    assert all(c.startswith(head) and c.count("SOURCE:\n<<<") == 1 for c in calls)
    # a restyle or new context changes only what comes after the source
    later = prompts.writer_system(r.req, r.plan, src, "NEW STYLE",
                                  [ContextItem(id=1, kind="text", name="n", text="NEW", when="now", added_by="Ana")])
    assert later.startswith(head) and later[: len(head)] == calls[1][: len(head)]


# ---- how a live co-host hears you: the call's language, and its names in the vocabulary --

@check("co-host hearing: language names map to BCP-47; key terms keep names, drop sentence openers")
def _():
    from app.catalog import language_code
    from app.notes import key_terms
    assert [language_code(x) for x in ("English", " tagalog ", "en-PH", "Klingon", "")] == [
        "en", "fil", "en-PH", None, None]
    terms = key_terms("Is sourdough worth it? The judges at Major League Hacking saw GPT-5 and Q3 numbers.",
                      "Daniel Reyes judged it. Then Daniel Reyes won in 2026.")
    assert terms[0] == "Daniel Reyes"                         # the most frequent first
    assert {"Major League Hacking", "GPT-5", "Q3"} <= set(terms)
    assert not {"Is", "The", "Then", "2026"} & set(terms)
    assert key_terms("x " * 10, limit=5) == [] and len(key_terms(" ".join(f"Name{i}" for i in range(99)), limit=7)) == 7


@check("co-host hearing: the call's language and names reach the session; unknown language guesses")
def _():
    from app.cohost import CohostSession

    class FakeGemini: ...

    async def run():
        req = CohostRequest(host=HostRequest(name="Zoe Park"), topic="Pricing at Acme Corp", language="English")
        s = CohostSession(doc={"id": "c1", "intro": {"show_name": "Walk & Talk"}}, request=req,
                          host=cast_cohost(req.host), you="Marc", notes="Acme Corp raised prices in Q3.",
                          gemini=FakeGemini(), settings=Settings(), store=None, on_closed=lambda _id: None)
        try:
            assert s.languages == ["en"]
            assert s.vocabulary[:3] == ["Marc", "Zoe Park", "Walk & Talk"] and "Acme Corp" in s.vocabulary
        finally:
            for t in (s._main, s._ticker):
                t.cancel()
        req2 = req.model_copy(update={"language": "Klingon"})
        s2 = CohostSession(doc={"id": "c2"}, request=req2, host=cast_cohost(req2.host), you="Marc", notes="",
                           gemini=FakeGemini(), settings=Settings(), store=None, on_closed=lambda _id: None)
        for t in (s2._main, s2._ticker):
            t.cancel()
        assert s2.languages == []
    return run()


# ---- long notes for a live co-host: a brief, and a lookup over all of them ---------------

@check("notes: passages break between paragraphs, then sentences, and stay under the size")
def _():
    from app.notes import passages
    text = "Short one.\n\nAnother short one.\n\n" + " ".join(f"Sentence {i} is here." for i in range(60)) + "\n\n" + "x" * 250
    parts = passages(text, size=100)
    assert all(len(p) <= 100 for p in parts)
    assert parts[0].startswith("Short one.\n\nAnother short one.\n\nSentence 0")   # small pieces share a passage
    assert any(p.startswith("Sentence ") and p.endswith("here.") for p in parts)   # cut between sentences
    assert "".join(p for p in parts if set(p) == {"x"}) == "x" * 250  # an unbreakable run is cut, nothing lost


@check("notes: a lookup finds the matching passages, in the notes' order")
def _():
    from app.notes import NotesIndex
    filler = "\n\n".join(f"Paragraph {i}: sourdough is flour, water, salt and patience." for i in range(300))
    notes = (filler + "\n\nGrandma Rosa kept the house loaf at 78 percent hydration.\n\n" + filler
             + "\n\nHer starters were fed twice a day with rye.")
    idx = NotesIndex(notes)
    found = idx.search("house loaf hydration")
    assert found and "78 percent" in found[0]
    assert any("rye" in p for p in idx.search("how often was the starter fed"))   # starters ~ starter
    both = idx.search("rye starters hydration")
    assert "78 percent" in both[0] and "rye" in both[-1]                          # in the notes' order
    assert idx.search("quantum chromodynamics") == [] and NotesIndex("").search("anything") == []


@check("notes: one brief per person, reused for the same notes; new notes replace it; failures aren't kept")
async def _():
    from app.notes import Briefs
    briefs, calls = Briefs(), []

    async def make(text, delay=0.05, fail=False):
        calls.append(text)
        await asyncio.sleep(delay)
        if fail:
            raise RuntimeError("model down")
        return f"BRIEF of {text}"

    first = briefs.job("u1", "notes A", lambda: make("notes A", delay=0.2))
    assert await briefs.get("u1", "notes A", lambda: make("notes A"), timeout=2) == "BRIEF of notes A"
    assert calls == ["notes A"] and briefs.has("u1", "notes A") and first.done()
    slow = briefs.job("u1", "notes B", lambda: make("notes B", delay=5))
    await briefs.get("u1", "notes C", lambda: make("notes C"), timeout=2)       # newer notes replace B
    await asyncio.sleep(0)
    assert slow.cancelled() and not briefs.has("u1", "notes B") and briefs.has("u1", "notes C")
    assert await briefs.get("u2", "notes D", lambda: make("notes D", fail=True), timeout=2) is None
    assert await briefs.get("u2", "notes D", lambda: make("notes D"), timeout=2) == "BRIEF of notes D"   # retried
    assert await briefs.get("u3", "notes E", lambda: make("notes E", delay=5), timeout=0.05) is None    # too slow


@check("co-host: long notes come as a brief, with the check_notes tool to look up the rest")
def _():
    from google.genai import types
    from app.llm import CHECK_NOTES, ToolCall, _live_events
    req = CohostRequest(host=HostRequest(name="Nova"), topic="Bread")
    host = cast_cohost(req.host)
    briefed = prompts.cohost_system(req, host, "Marc", "THE BRIEF", lookup=True)
    whole = prompts.cohost_system(req, host, "Marc", "ALL THE NOTES")
    assert "THE BRIEF" in briefed and "check_notes" in briefed and "check_notes" not in whole
    assert CHECK_NOTES.name == "check_notes" and "query" in CHECK_NOTES.parameters.required
    msg = types.LiveServerMessage(tool_call=types.LiveServerToolCall(function_calls=[
        types.FunctionCall(id="fc1", name="check_notes", args={"query": "hydration"})]))
    assert _live_events(msg) == [ToolCall("fc1", "check_notes", {"query": "hydration"})]


# ---- co-hosting: recording a call and editing it into an episode ----------------------

R, M = ed.RATE, ed.MIC_RATE


def _tone(seconds: float, rate: int, amp: float = 0.3, f: float = 220.0) -> bytes:
    t = np.arange(int(seconds * rate)) / rate
    return ed.float_to_pcm((amp * np.sin(2 * np.pi * f * t)).astype(np.float32))


def _quiet(seconds: float, rate: int) -> bytes:
    return b"\x00\x00" * int(seconds * rate)


def _conversation() -> ed.Take:
    """You talk (1 s), the co-host answers (2 s) while you listen, you talk again (1 s)."""
    take = ed.Take()
    take.add_mic(_tone(1, M) + _quiet(1, M))
    take.add_ai_audio(_tone(2, R, f=330))
    take.add_ai_text("Great point. Tell me more!")
    take.finish_turn()
    take.add_mic(_quiet(2, M) + _tone(1, M) + _quiet(1, M))
    return take


@check("co-host: the mic is upsampled to 24 kHz without changing its length in time")
def _():
    x = ed.upsample_mic(np.zeros(M, np.float32))
    assert len(x) == R
    assert len(ed.upsample_mic(np.zeros(0, np.float32))) == 0


@check("co-host: speech spans find talk between silences")
def _():
    x = ed.pcm_to_float(_quiet(1, R) + _tone(2, R) + _quiet(1, R))
    spans = ed.speech_spans(x, -50.0)
    assert len(spans) == 1
    s, e = spans[0]
    assert abs(s - R) <= ed.FRAME and abs(e - 3 * R) <= ed.FRAME


@check("co-host: a take places each answer where it started playing, back to back")
def _():
    take = ed.Take()
    take.add_mic(_quiet(2, M))
    turn, pcm = take.add_ai_audio(_tone(1, R) + b"\x01")        # odd byte carried over
    assert turn.start == 2 * R and len(pcm) == 2 * R
    take.add_mic(_quiet(0.5, M))
    turn2, _ = take.add_ai_audio(b"\x02" + _tone(1, R))
    assert turn2 is turn and turn.end == 4 * R + 1                # one turn, no gap (+ the joined byte pair)
    take.finish_turn()
    nxt, _ = take.add_ai_audio(_tone(1, R))                       # the last one is still playing
    assert nxt.id == 2 and nxt.start == turn.end


@check("co-host: cutting in ends the answer where you did, even after it was all sent")
def _():
    take = ed.Take()
    take.add_mic(_quiet(1, M))
    take.add_ai_audio(_tone(3, R))
    take.finish_turn()                                            # the model already finished sending
    take.add_mic(_quiet(1, M))
    cut = take.interrupt()
    assert cut is not None and cut.cut_at == 2 * R and cut.samples == R and cut.end == 2 * R
    take.add_mic(_quiet(5, M))
    assert take.interrupt() is None                               # nothing playing any more


@check("co-host: late words of a finished answer stay with it")
def _():
    take = ed.Take()
    take.add_ai_audio(_tone(2, R))
    take.add_ai_text("Hello ")
    take.finish_turn()
    take.add_ai_text("there.")
    assert len(take.turns) == 1 and take.turns[0].text == "Hello there."


@check("co-host: an answer cut off in its first 1.5 s is dropped")
def _():
    take = ed.Take()
    take.add_mic(_quiet(1, M))
    take.add_ai_audio(_tone(3, R))
    take.add_mic(_tone(1, M))                                     # you cut in after 1 s
    take.interrupt()
    take.add_mic(_tone(1, M) + _quiet(0.5, M))
    edit = ed.build_edit(take)
    assert edit.dropped_cut_offs == 1
    assert [t.you for t in edit.turns] == [True]


@check("co-host: segments alternate speakers, sit back to back, and the chimes bracket them")
def _():
    edit = ed.build_edit(_conversation())
    assert [t.you for t in edit.turns] == [True, False, True]
    ep = ed.assemble(edit, ed.fallback_plan("Test"), "Marc", "Nova")
    segs = ep.segments
    assert [s["lines"][0]["speaker"] if s["lines"] else None for s in segs] == [None, "Nova", None]
    assert segs[0]["start"] == len(ed.sting()) + len(ed.silence(0.35))
    for a, b in zip(segs, segs[1:]):
        assert b["start"] == a["start"] + a["samples"] + len(ed.silence(ed.TURN_GAP_S))
    assert ep.samples == segs[-1]["start"] + segs[-1]["samples"] + len(ed.silence(0.5)) + len(ed.sting())
    assert segs[-1]["final"] and not segs[0]["final"]
    assert ep.beats[0]["start"] == 0 and ep.beats[-1]["end"] == ep.samples
    assert ep.stats["call_s"] == 6.0 and ep.stats["tightened_s"] > 0


@check("co-host: with a theme, the music opens and closes the episode and every start moves with it")
def _():
    from app.mixer import INTRO_LEAD_S, OUTRO_PLAYOUT_S, Cues
    edit = ed.build_edit(_conversation())
    plan = ed.EditPlan(title="T", chapters=[ed.EditChapter(title="A", start_turn=0),
                                            ed.EditChapter(title="B", start_turn=edit.turns[2].id)])
    plain = ed.assemble(edit, plan, "Marc", "Nova", chimes=False)
    segs = plain.segments
    assert segs[0]["start"] == 0 and plain.samples == segs[-1]["start"] + segs[-1]["samples"]   # no chimes
    assert plain.stats["tightened_s"] == round(6.0 - plain.samples / R, 1)
    theme = (0.2 * np.sin(2 * np.pi * 440 * np.arange(30 * R) / R)).astype(np.float32)
    ep, regions = ed.scored(plain, Cues(intro=theme, sting=theme[: 3 * R], outro=theme))
    lead = int(INTRO_LEAD_S * R)
    assert [s["start"] for s in ep.segments] == [s["start"] + lead for s in segs]
    assert [s["samples"] for s in ep.segments] == [s["samples"] for s in segs]
    assert ep.samples == plain.samples + lead + int(OUTRO_PLAYOUT_S * R)
    # chapter 1 opens on the intro; chapter 2 starts with its first turn (too soon after the intro for a sting)
    assert [b["start"] for b in ep.beats] == [0, ep.segments[2]["start"]] and ep.beats[-1]["end"] == ep.samples
    assert [r["kind"] for r in regions] == ["intro", "outro"] and regions[-1]["end"] == ep.samples
    assert all(isinstance(r, dict) for r in regions)                 # Firestore rejects arrays of arrays
    assert ep.stats["episode_s"] == round(ep.samples / R, 1)


@check("co-host: the opening welcomes listeners to your show by name, and only then")
def _():
    assert prompts.cohost_opening("Marc", 10) == (
        "(Producer: you're live and recording, about 10 minutes. Open the show: welcome the listeners, "
        "introduce yourself and Marc in a sentence, set up the topic with a hook, then hand it to Marc with "
        "a question. Keep it under 20 seconds.)")
    named = prompts.cohost_opening("Marc", 10, "Bread Talk", "Crumbs and all.")
    assert 'welcome the listeners to "Bread Talk",' in named and 'keeping its words: "Crumbs and all."' in named


@check("co-host: each mode has a theme mood, and so does each premade briefing")
def _():
    from app import briefing
    from app.catalog import COHOST_MODES, MUSIC_MOODS
    assert all(m["music_mood"] in MUSIC_MOODS for m in COHOST_MODES)
    assert all(briefing.music_mood(t) in MUSIC_MOODS for t in briefing.TEMPLATES)
    assert briefing.music_mood(briefing.TEMPLATES_BY_ID["morning"]) == "upbeat"
    assert briefing.music_mood({"vibe": "late-night"}) == "calm" and briefing.music_mood({}) == "newsy"


@check("co-host: dead air is trimmed and long pauses inside a turn tightened")
def _():
    take = ed.Take()
    take.add_mic(_quiet(2, M) + _tone(1.5, M) + _quiet(3, M) + _tone(1, M) + _quiet(2, M))
    edit = ed.build_edit(take)
    assert len(edit.turns) == 1 and len(edit.turns[0].phrases) == 2
    seg = ed.assemble(edit, ed.fallback_plan("T"), "Marc", "Nova").segments[0]
    pads = ed.PAD_BEFORE_S + ed.PAD_AFTER_S
    assert seg["samples"] <= (1.5 + 1 + ed.TIGHT_PAUSE_S + 2 * pads + 0.05) * R
    assert seg["samples"] >= (1.5 + 1 + ed.TIGHT_PAUSE_S) * R


@check("co-host: the editor's cuts and dropped answers are applied")
def _():
    take = ed.Take()
    take.add_mic(_tone(0.8, M) + _quiet(0.6, M))                 # "can you hear me?"
    take.add_heard("Can you hear me?")
    take.add_mic(_tone(1.5, M) + _quiet(1, M))
    take.add_heard("So, sourdough.")
    take.add_ai_audio(_tone(1, R, f=330))
    take.add_ai_text("Loud and clear!")
    take.finish_turn()
    take.add_mic(_quiet(1, M) + _tone(1, M) + _quiet(1, M))
    edit = ed.build_edit(take)
    you = edit.turns[0]
    assert len(you.phrases) == 2 and you.phrases[0].text == "Can you hear me?"
    host_turn = edit.turns[1]
    plan = ed.EditPlan(title="Bread", cut_phrases=[you.phrases[0].id], drop_turns=[host_turn.id],
                       chapters=[ed.EditChapter(title="Intro", start_turn=0),
                                 ed.EditChapter(title="More", start_turn=2)])
    ep = ed.assemble(edit, plan, "Marc", "Nova")
    assert ep.stats["flubs_cut"] == 1 and ep.stats["turns_dropped"] == 1
    assert len(ep.segments) == 2 and ep.segments[0]["lines"][0]["text"] == "So, sourdough."
    assert [b["title"] for b in ep.beats] == ["Intro", "More"]
    assert ep.beats[1]["start"] == ep.segments[1]["start"] and ep.segments[1]["beat"] == 1


@check("co-host: an answer you cut into stays in the episode, even if the editor drops it")
def _():
    take = ed.Take()
    take.add_mic(_quiet(1, M))
    take.add_ai_audio(_tone(3, R, f=330))
    take.add_ai_text("Well, the thing about starters is")
    take.add_mic(_quiet(1.6, M) + _tone(0.4, M))
    take.interrupt()                                  # you cut in 2 s into its answer
    take.add_mic(_tone(1, M) + _quiet(1, M))
    edit = ed.build_edit(take)
    ai = next(t for t in edit.turns if not t.you)
    assert ai.cut_off
    ep = ed.assemble(edit, ed.EditPlan(title="T", drop_turns=[ai.id]), "Marc", "Nova")
    assert any(ln["speaker"] == "Nova" for s in ep.segments for ln in s["lines"]) and ep.stats["turns_dropped"] == 0


@check("co-host: a cut only lands where the phrase's words fit its audio")
def _():
    assert not ed._fits(ed.Phrase(0, 0, int(0.4 * R), "All right, that's fair. Last"))  # a blip: misplaced text
    assert ed._fits(ed.Phrase(0, 0, int(2.3 * R), "So, sourdough is great."))
    assert ed._fits(ed.Phrase(0, 0, int(1.3 * R), "Um"))                                 # a drawn-out filler
    assert not ed._fits(ed.Phrase(0, 0, int(8 * R), "Um"))                               # 8 s of speech isn't "um"


@check("co-host: each turn is levelled to the same loudness")
def _():
    take = ed.Take()
    take.add_mic(_tone(2, M, amp=0.05) + _quiet(1, M))
    take.add_ai_audio(_tone(2, R, amp=0.5))
    take.finish_turn()
    take.add_mic(_quiet(3, M))
    ep = ed.assemble(ed.build_edit(take), ed.fallback_plan("T"), "Marc", "Nova")
    x = ed.pcm_to_float(ep.pcm)
    for seg in ep.segments:
        mid = x[seg["start"] + int(0.3 * R): seg["start"] + seg["samples"] - int(0.3 * R)]
        db = 20 * math.log10(float(np.sqrt(np.mean(mid.astype(np.float64) ** 2))))
        assert abs(db - ed.TARGET_DBFS) < 1.0, db


@check("co-host: the live transcript is shared out across the phrases it covers")
def _():
    turns = [ed.Turn(id=0, you=True, phrases=[ed.Phrase(0, 0, R), ed.Phrase(1, int(1.5 * R), int(2.5 * R))])]
    ed._attach_transcript(turns, [(int(0.6 * R), "Hi"), (int(0.9 * R), "there."),
                                  (3 * R, "one two three four")])
    p0, p1 = turns[0].phrases
    assert p0.text == "Hi there." and p1.text == "one two three four"
    turns = [ed.Turn(id=0, you=True, phrases=[ed.Phrase(0, 0, R), ed.Phrase(1, int(1.5 * R), int(2.5 * R))])]
    ed._attach_transcript(turns, [(3 * R, "one two three four")])  # all at once, after both
    p0, p1 = turns[0].phrases
    assert p0.text == "one two" and p1.text == "three four"


@check("co-host: mode switches tint the segments said after them")
def _():
    edit = ed.build_edit(_conversation())
    ep = ed.assemble(edit, ed.fallback_plan("T"), "Marc", "Nova", revs=[(int(3.5 * R), 1)])
    assert [s["style_rev"] for s in ep.segments] == [0, 0, 1]


@check("co-host: the unedited mix has both sides where they happened")
def _():
    take = _conversation()
    x = ed.pcm_to_float(ed.raw_mix(take))
    assert len(x) == take.length == 6 * R
    assert np.abs(x[int(2.5 * R):int(3.5 * R)]).max() > 0.2           # the co-host
    assert np.abs(x[int(5.2 * R):]).max() == 0


@check("co-host: nothing said means no episode")
def _():
    take = ed.Take()
    take.add_mic(_quiet(3, M))
    edit = ed.build_edit(take)
    assert edit.turns == []
    assert ed.assemble(edit, ed.fallback_plan("T"), "Marc", "Nova").segments == []


@check("co-host: cast with a voice of its own gender; a picked voice wins")
def _():
    h = cast_cohost(HostRequest(name="Dr. Maya", gender="female"))
    assert VOICE_GENDER[h.voice] == "female" and h.gender == "female" and h.persona
    h = cast_cohost(HostRequest(name="Sam", voice="Puck", gender="female"))
    assert h.voice == "Puck" and h.gender == "male"
    assert cast_cohost(HostRequest()).name == "Alex"


@check("co-host: the prompt carries the persona, its private notes, the mode and the topic")
def _():
    req = CohostRequest(host=HostRequest(name="Nova", persona="A skeptic.", context="SECRET NOTES"),
                        mode="debate", topic="Is cereal soup?", minutes=5)
    host = cast_cohost(req.host)
    s = prompts.cohost_system(req, host, "Marc", "SOME NOTES")
    assert "SECRET NOTES" in s and "SOME NOTES" in s and "Is cereal soup?" in s
    assert "Take the opposite side" in s and "co-hosting a podcast episode live with Marc" in s
    assert "Producer:" in prompts.cohost_mode_switch("interview")


@check("co-host: the editor reads your phrases with words, and the co-host's turns")
def _():
    text = prompts.editor_input([
        {"id": 0, "speaker": "Marc", "you": True, "phrases": [{"id": 0, "text": "Um"}, {"id": 1, "text": ""}]},
        {"id": 1, "speaker": "Nova", "you": False, "cut_off": True, "text": "Well I"},
    ])
    assert "[p0] Um" in text and "[p1]" not in text and "T1 Nova (cut off): Well I" in text


def main() -> int:
    width = max(len(n) for n, _, _ in RESULTS)
    for name, ok, err in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'} {name.ljust(width)}")
        if err:
            print(err)
    return 0 if all(ok for _, ok, _ in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
