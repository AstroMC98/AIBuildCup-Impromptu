"""Fast checks of the pieces that are hard to reach end to end.

    python -m tests.unit_test
"""
import asyncio
import math
import os
import struct
import sys
import tempfile
import traceback

import httpx
import numpy as np

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="podcasts-unit-"))

from app import editing as ed  # noqa: E402
from app import prompts, sources  # noqa: E402
from app.catalog import VOICE_GENDER  # noqa: E402
from app.cohost import cast_cohost  # noqa: E402
from app.accounts import Accounts  # noqa: E402
from app.audio import Mp3Writer, PcmWindow  # noqa: E402
from app.auth import Auth, AuthError  # noqa: E402
from app.store import InsufficientCredits  # noqa: E402
from app.clock import LiveClock  # noqa: E402
from app.config import Settings  # noqa: E402
from app.limits import TokenBucket  # noqa: E402
from app.models import (  # noqa: E402
    Beat, CohostRequest, ContextItem, Host, HostRequest, Line, SegmentDraft, ShowPlan, ShowRequest,
)
from app.show import Draft, ShowRunner  # noqa: E402
from app.sources import SourceError, sniff_image  # noqa: E402
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


# ---- the show loop ------------------------------------------------------------------

class StubRoom:
    def __init__(self):
        self.clock = LiveClock(24_000)
        self.wake = asyncio.Event()
        self.events = []

    def publish(self, event):
        self.events.append(event)

    def push_audio(self, seq, pcm):
        pass


def _runner() -> ShowRunner:
    req = ShowRequest(source="x", style="calm", hosts=[{"name": "A"}], minutes=4)
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
