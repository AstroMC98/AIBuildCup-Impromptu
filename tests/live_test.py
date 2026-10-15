"""Real pods against the real Gemini API, in real time. This spends API credits.

    python -m tests.live_test --minutes 5          # one 5-minute pod
    python -m tests.live_test --minutes 10 15      # two pods at once
    python -m tests.live_test --cohost             # a co-host call (Gemini Live), ~2 minutes

--cohost plays "you": your lines are voiced with the TTS model, then streamed into
a real co-host call as mic audio in real time (as if through headphones: the
co-host's voice isn't fed back). It measures how fast the co-host answers after
you stop talking, how fast cutting in stops it, a mode switch, and how long the
episode takes to edit after hanging up, then saves the episode, the unedited call
and the transcript.

Starts the app on its own port (dev sign-in, local storage under
data/live-tests/<time>/), signs in a producer and a connected listener, and for
each pod:
  - goes live from a template, with both of them "playing" at real speed
  - the listener calls in about a minute in; the producer adds context at ~40%
    and (10+ minute pods) restyles at ~60%
  - every audio chunk's arrival time is recorded and replayed against a
    real-time player, to find stalls: moments a listener would hear silence
Writes report.md and report.json, each pod's transcript and MP3, and the app log.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import secrets
import struct
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).resolve().parent.parent
PORT = 8777
BASE = f"http://127.0.0.1:{PORT}"
WS = f"ws://127.0.0.1:{PORT}"
RATE = 24_000

PODS = {
    5: {
        "template": "girl-talk",
        "source": "How to set boundaries with friends without losing them.",
        "comment": "What if my friend always cancels plans last minute?",
        "comment2": "How do I say no to being a bridesmaid?",
        "context": {"kind": "text", "when": "now", "cite": "true",
                    "text": "Producer note: share the 'one honest sentence' trick: say what you need in one sentence, no apology."},
        "restyle": None,
    },
    10: {
        "template": "science-deep-dive",
        "source": "Why we need sleep: sleep stages, memory consolidation, adenosine and caffeine, and the circadian rhythm.",
        "comment": "Is it bad to drink coffee after 2pm?",
        "comment2": "Does a nap make up for a short night?",
        "context": {"kind": "text", "when": "now", "cite": "true",
                    "text": "Listener fact: caffeine's half-life is about 5 to 6 hours in most adults."},
        "restyle": "bedtime",
    },
    15: {
        "vibe": "gossip",
        "source": "Genesis chapters 1 to 3: creation in six days, Adam and Eve in the garden of Eden, "
                  "the serpent, the forbidden fruit, and being sent out of the garden.",
        "comment": "Is Eve the original main character?",
        "comment2": "Wait, so was it actually an apple or not?",
        "context": {"kind": "link", "when": "now", "cite": "true", "url": "https://en.wikipedia.org/wiki/Garden_of_Eden"},
        "restyle": "sports",
    },
}


class Recorder:
    """A listener socket that keeps every event and audio chunk with its arrival time."""

    def __init__(self, label: str, show_id: str, cookie: str):
        self.label = label
        self.url = f"{WS}/ws/show/{show_id}"
        self.cookie = cookie
        self.events: list[tuple[float, dict]] = []
        self.frames: list[tuple[float, int, int, int]] = []   # (arrived, seq, start, samples)
        self.ws = None
        self.task: asyncio.Task | None = None

    async def open(self) -> "Recorder":
        self.ws = await websockets.connect(self.url, additional_headers={"Cookie": f"gp_session={self.cookie}"},
                                           max_size=None, ping_interval=20, ping_timeout=60)
        await self.send({"type": "join"})
        await self.send({"type": "player", "playing": True})
        self.task = asyncio.create_task(self._recv())
        return self

    async def send(self, obj: dict) -> None:
        await self.ws.send(json.dumps(obj))

    async def _recv(self) -> None:
        try:
            async for msg in self.ws:
                now = time.monotonic()
                if isinstance(msg, bytes):
                    seq, start = struct.unpack("<II", msg[:8])
                    self.frames.append((now, seq, start, (len(msg) - 8) // 2))
                else:
                    self.events.append((now, json.loads(msg)))
        except websockets.ConnectionClosed:
            pass

    def of(self, kind: str) -> list[dict]:
        return [e for _, e in self.events if e["type"] == kind]

    def first(self, kind: str) -> tuple[float, dict] | None:
        return next(((t, e) for t, e in self.events if e["type"] == kind), None)

    def live_seconds(self) -> float:
        ticks = self.of("tick")
        return ticks[-1]["live"] / RATE if ticks else 0.0

    async def close(self) -> None:
        try:
            await self.ws.close()
        except Exception:
            pass
        if self.task:
            await asyncio.wait([self.task], timeout=5)


def simulate_player(frames: list[tuple[float, int, int, int]]) -> dict:
    """Play the chunks at real speed the moment they arrive, like the browser's
    LivePlayer: start on the first chunk, and when the next sample isn't there
    yet, wait for it (a stall) and resume ~60 ms after it lands."""
    if not frames:
        return {"stalls": [], "heard_at": {}}
    t = None
    stalls: list[float] = []
    heard_at: dict[int, float] = {}        # start sample -> wall time it is heard
    for arrived, _seq, start, n in frames:
        if t is None:
            t = arrived + 0.08
        elif arrived > t:
            stalls.append(arrived - t + 0.06)
            t = arrived + 0.06
        heard_at[start] = t
        t += n / RATE
    return {"stalls": stalls, "heard_at": heard_at, "end": t}


async def run_pod(minutes: int, spec: dict, producer: httpx.AsyncClient, guest: httpx.AsyncClient,
                  cookies: dict, opts: dict, out: Path) -> dict:
    request = {
        "source": spec["source"], "minutes": minutes, "language": "English", "source_names": [],
        "hosts": [{"name": "", "voice": "auto"}, {"name": "", "voice": "auto"}],
    }
    if spec.get("template"):
        t = next(x for x in opts["templates"] if x["id"] == spec["template"])
        request.update(style=t["style"], style_label=t["label"], accent=t["accent"],
                       hosts=[{"name": h["name"], "voice": h["voice"], "persona": h["persona"]} for h in t["hosts"]])
    else:
        p = next(x for x in opts["presets"] if x["id"] == spec["vibe"])
        request.update(style=p["prompt"], vibe=p["id"])
    t_create = time.monotonic()
    r = await producer.post("/api/shows", json={"request": request, "live": True, "visibility": opts["visibility"],
                                                "template": spec.get("template")})
    r.raise_for_status()
    show_id = r.json()["id"]
    print(f"[{minutes} min] live: {show_id}  watch: {BASE}/show/{show_id}", flush=True)

    prod = await Recorder("producer", show_id, cookies["producer"]).open()
    lis = await Recorder("listener", show_id, cookies["guest"]).open()
    target = minutes * 60
    marks: dict[str, float] = {}
    deadline = time.monotonic() + target * 2 + 300
    last_print = 0.0
    while time.monotonic() < deadline:
        now = time.monotonic()
        ended = prod.first("ended")
        if prod.of("saved") or (ended and now - ended[0] > 30):
            break
        live = prod.live_seconds()
        if "comment" not in marks and live >= opts["first_callin"]:
            marks["comment"] = now
            marks["comment_live"] = live
            await lis.send({"type": "comment", "text": spec["comment"], "client_id": "live-test-1"})
        if "comment2" not in marks and live >= 0.75 * target:
            marks["comment2"] = now
            marks["comment2_live"] = live
            await lis.send({"type": "comment", "text": spec["comment2"], "client_id": "live-test-2"})
        if "react" not in marks and live >= 0.3 * target:
            marks["react"] = now
            await lis.send({"type": "react", "n": 3})
        if "context" not in marks and live >= 0.4 * target:
            marks["context"] = now
            marks["context_live"] = live
            rr = await producer.post(f"/api/shows/{show_id}/context", data=spec["context"])
            marks["context_status"] = rr.status_code
            marks["context_id"] = rr.json()["context"]["id"] if rr.status_code == 200 else None
            if rr.status_code != 200:
                marks["context_error"] = rr.text[:300]
        if spec.get("restyle") and "restyle" not in marks and live >= 0.6 * target:
            marks["restyle"] = now
            marks["restyle_live"] = live
            await prod.send({"type": "restyle", "vibe": spec["restyle"]})
        if now - last_print > 60:
            last_print = now
            print(f"[{minutes} min] {live / 60:.1f} min heard, {len(prod.of('segment'))} segments, "
                  f"{len(prod.of('error'))} errors", flush=True)
        await asyncio.sleep(0.5)
    await asyncio.sleep(1)
    await prod.close()
    await lis.close()

    # ---- what happened ------------------------------------------------------------
    sim = simulate_player(prod.frames)
    segs = prod.of("segment")
    ends = {e["seq"]: e["samples"] for e in prod.of("segment_end")}
    comments = {c["client_id"]: c for c in prod.of("comment")}
    contexts = {x["id"]: x for x in prod.of("context")}
    samples = sum(ends.values())
    duration = samples / RATE
    first_audio = prod.frames[0][0] - t_create if prod.frames else None

    def heard(seq: int) -> float | None:
        seg = next((s for s in segs if s["seq"] == seq), None)
        return sim["heard_at"].get(seg["start"]) if seg else None

    def callin(client_id: str, mark: str) -> tuple[dict, float | None, float | None]:
        """(final state, seconds until its segment was heard, ETA shown when sent).
        The ETA counts to the moment it's read, a few seconds into the segment."""
        events = [e for e in prod.of("comment") if e.get("client_id") == client_id]
        last = events[-1] if events else {}
        at = heard(last["seq"]) if last.get("seq") else None
        after = round(at - marks[mark], 1) if at and mark in marks else None
        eta = next((e["eta"] for e in events if e.get("eta") is not None), None)
        shown = round(eta / RATE - marks[f"{mark}_live"], 1) if eta is not None and mark in marks else None
        return last, after, shown

    c, comment_after, comment_eta = callin("live-test-1", "comment")
    c2, comment2_after, comment2_eta = callin("live-test-2", "comment2")
    x = contexts.get(marks.get("context_id"), {})
    context_heard = heard(x["used_seq"]) if x.get("used_seq") else None
    restyled = [s for s in segs if s.get("style_rev", 0) >= 1]
    restyle_heard = heard(restyled[0]["seq"]) if restyled else None
    stalls = sim["stalls"]
    real_stalls = [s for s in stalls if s > 0.3]
    wall = (sim["end"] - prod.frames[0][0]) if prod.frames else 0

    # transcript and audio
    lines = [f"# {minutes}-minute pod: {(prod.of('plan') or [{}])[0].get('title', show_id)}", ""]
    for p in prod.of("plan")[:1]:
        lines += [p.get("tagline", ""), ""] + [f"- **{h['name']}** ({h['voice']}): {h['persona']}" for h in p["hosts"]]
        lines += ["", "Chapters: " + " · ".join(f"{b['title']} ({b['minutes']} min)" for b in p["beats"]), ""]
    for s in segs:
        tag = f" _(style {s['style_rev']})_" if s.get("style_rev") else ""
        lines.append(f"<!-- segment {s['seq']}, chapter {s['beat'] + 1}, at {s['start'] / RATE:.0f}s{tag} -->")
        lines += [f"**{ln['speaker']}:** {ln['text']}" for ln in s["lines"]] + [""]
    (out / f"pod-{minutes}min.md").write_text("\n".join(lines), encoding="utf-8")
    audio = await producer.get(f"/api/shows/{show_id}/audio", follow_redirects=True)
    if audio.status_code == 200:
        (out / f"pod-{minutes}min.mp3").write_bytes(audio.content)
    ledger = (await producer.get("/api/me/ledger")).json()["entries"]
    charged = -sum(e["amount"] for e in ledger if e.get("show_id") == show_id and e["reason"] == "pod")
    refunded = sum(e["amount"] for e in ledger if e.get("show_id") == show_id and e["reason"] == "refund")

    result = {
        "minutes": minutes, "show_id": show_id, "title": (prod.of("plan") or [{}])[0].get("title"),
        "ended": (prod.of("ended") or [{}])[-1].get("reason"), "saved": bool(prod.of("saved")),
        "first_audio_s": round(first_audio, 1) if first_audio else None,
        "duration_s": round(duration, 1), "target_s": target,
        "segments": len(segs), "lines": sum(len(s["lines"]) for s in segs),
        "chapters": len((prod.of("plan") or [{"beats": []}])[0]["beats"]),
        "chapters_reached": len({s["beat"] for s in segs}),
        "stalls_over_300ms": len(real_stalls), "stall_total_s": round(sum(stalls), 1),
        "longest_stall_s": round(max(stalls), 1) if stalls else 0,
        "listen_wall_s": round(wall, 1),
        "comment_status": c.get("status"), "comment_heard_after_s": comment_after, "comment_eta_s": comment_eta,
        "comment2_status": c2.get("status"), "comment2_heard_after_s": comment2_after, "comment2_eta_s": comment2_eta,
        "comment_read_on_air": any(re.search(r"\bListener\b|\bJo\b", ln["text"]) for s in segs
                                   if c.get("seq") == s["seq"] for ln in s["lines"]),
        "context_http": marks.get("context_status"), "context_status": x.get("status"),
        "context_error": marks.get("context_error"),
        "context_heard_after_s": round(context_heard - marks["context"], 1) if context_heard and "context" in marks else None,
        "restyle": spec.get("restyle"), "restyle_heard_after_s": round(restyle_heard - marks["restyle"], 1)
        if restyle_heard and "restyle" in marks else None,
        "errors": [e.get("message") for e in prod.of("error")],
        "credits_charged": charged, "credits_refunded": refunded,
        "mp3_bytes": len(audio.content) if audio.status_code == 200 else 0,
    }
    print(f"[{minutes} min] done: {json.dumps(result)}", flush=True)
    return result


# ---- co-hosting ------------------------------------------------------------------------

MIC_RATE = 16_000
MIC_FRAME = 640    # 40 ms
COHOST_LINES = [
    "Hey! Sorry, can you hear me okay?",                           # a mic check, for the editor to cut
    "So I've been baking sourdough for about a year now, and honestly? I think it's a little overrated.",
    "Okay, but here's my problem. The flavor is great, but feeding a starter every single day is a chore.",
    "Wait, wait, let me stop you there. What about people who just want bread on a Tuesday night?",
    "Alright, that's fair. Last thing: what's the one tip you'd give a total beginner?",
]
CUT_IN_LINE = 3     # said over the co-host's answer, 1.5 s after it starts


async def voice_lines(lines: list[str]) -> list[bytes]:
    """Each line as 16 kHz mono s16le, voiced by the TTS model (a male voice)."""
    import numpy as np
    from app.config import Settings
    from app.llm import Gemini
    from app.models import Host, Line

    gemini = Gemini(Settings())
    host = Host(name="Marc", persona="", voice="Puck", gender="male")
    out = []
    for text in lines:
        pcm = b"".join([c async for c in gemini.stream_speech([Line(speaker="Marc", text=text)], [host])])
        x = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32)
        n = len(x) * MIC_RATE // RATE
        y = np.interp(np.arange(n) * (RATE / MIC_RATE), np.arange(len(x)), x)
        out.append(y.astype("<i2").tobytes())
    return out


class CallPhone:
    """A phone on a co-host call: streams its mic in real time and plays (in
    simulation) the co-host the moment each chunk arrives."""

    def __init__(self, show_id: str, cookie: str):
        self.url = f"{WS}/ws/cohost/{show_id}"
        self.cookie = cookie
        self.events: list[tuple[float, dict]] = []
        self.turn_first: dict[int, float] = {}      # turn -> when its first audio arrived
        self.play_end = 0.0                         # when the co-host's audio so far finishes playing
        self.flushed: set[int] = set()
        self.speech = b""
        self.ws = None
        self.tasks: list[asyncio.Task] = []

    async def open(self) -> "CallPhone":
        self.ws = await websockets.connect(self.url, additional_headers={"Cookie": f"gp_session={self.cookie}"},
                                           max_size=None, ping_interval=20, ping_timeout=60)
        await self.ws.send(json.dumps({"type": "join"}))
        self.tasks = [asyncio.create_task(self._recv()), asyncio.create_task(self._mic())]
        return self

    async def _recv(self) -> None:
        try:
            async for msg in self.ws:
                now = time.monotonic()
                if isinstance(msg, bytes):
                    turn = struct.unpack("<I", msg[:4])[0]
                    if turn in self.flushed:
                        continue
                    self.turn_first.setdefault(turn, now)
                    self.play_end = max(self.play_end, now + 0.06) + (len(msg) - 4) / 2 / RATE
                else:
                    e = json.loads(msg)
                    self.events.append((now, e))
                    if e["type"] == "interrupted":
                        self.flush(e["turn"])
        except websockets.ConnectionClosed:
            pass

    async def _mic(self) -> None:
        start, n = time.monotonic(), 0
        try:
            while True:
                chunk, self.speech = self.speech[: MIC_FRAME * 2], self.speech[MIC_FRAME * 2:]
                await self.ws.send(chunk + b"\x00" * (MIC_FRAME * 2 - len(chunk)))
                n += 1
                await asyncio.sleep(max(0.0, start + n * 0.04 - time.monotonic()))
        except (websockets.ConnectionClosed, asyncio.CancelledError):
            pass

    def flush(self, turn: int) -> None:
        self.flushed.add(turn)
        self.play_end = time.monotonic()

    @property
    def last_turn(self) -> int:
        return max(self.turn_first, default=0)

    @property
    def playing(self) -> bool:
        return time.monotonic() < self.play_end

    def of(self, kind: str) -> list[dict]:
        return [e for _, e in self.events if e["type"] == kind]

    def first(self, kind: str, pred=lambda e: True) -> float | None:
        return next((t for t, e in self.events if e["type"] == kind and pred(e)), None)

    async def say(self, pcm: bytes) -> float:
        """Talk; returns when you stopped (the last of it went out)."""
        self.speech += pcm
        await asyncio.sleep(len(pcm) / 2 / MIC_RATE)
        while self.speech:
            await asyncio.sleep(0.02)
        return time.monotonic()

    async def until(self, pred, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return True
            await asyncio.sleep(0.02)
        return False

    async def close(self) -> None:
        for t in self.tasks:
            t.cancel()
        try:
            await self.ws.close()
        except Exception:
            pass


async def run_cohost(producer: httpx.AsyncClient, cookie: str, opts: dict, out: Path) -> dict:
    print("[co-host] voicing your lines with the TTS model…", flush=True)
    lines = await voice_lines(COHOST_LINES)
    preset = next(p for p in opts["persona_presets"] if p["id"] == "comedian")
    r = await producer.post("/api/cohost", json={"request": {
        "host": {k: preset[k] for k in ("name", "gender", "voice", "persona", "likes", "specialties", "skills")},
        "mode": "banter", "topic": "Is sourdough worth the hassle?", "minutes": 5, "your_name": "Marc",
    }, "visibility": opts["visibility"]})
    r.raise_for_status()
    show_id = r.json()["id"]
    print(f"[co-host] on a call: {show_id}", flush=True)
    phone = await CallPhone(show_id, cookie).open()
    t_join = time.monotonic()
    marks: dict = {"answers": [], "jump_ins": 0}

    async def answer_after(stopped: float, after_turn: int) -> float | None:
        """Seconds from when you stopped to the co-host's answer. A turn it started
        while you were still talking (it took a pause for the end of your turn, then
        stopped when you carried on) is a jump-in, not the answer."""
        def answer() -> int | None:
            return next((t for t, at in sorted(phone.turn_first.items())
                         if t > after_turn and at >= stopped - 0.3), None)
        ok = await phone.until(lambda: answer() is not None, 15)
        marks["jump_ins"] += sum(1 for t, at in phone.turn_first.items() if t > after_turn and at < stopped - 0.3)
        return round(phone.turn_first[answer()] - stopped, 2) if ok else None

    async def let_it_finish() -> None:
        turn = phone.last_turn
        await phone.until(lambda: any(e["turn"] == turn for e in phone.of("turn_done")) or turn in phone.flushed, 30)
        await phone.until(lambda: not phone.playing, 30)
        await asyncio.sleep(0.6)

    await phone.until(lambda: phone.last_turn >= 1, 20)
    marks["opening_s"] = round(phone.turn_first[1] - t_join, 2) if phone.last_turn else None
    await let_it_finish()
    i = 0
    while i < len(lines):
        if i == 2:     # switch the mode, and time the co-host's answer to it
            before = phone.last_turn
            t = time.monotonic()
            await phone.ws.send(json.dumps({"type": "mode", "mode": "debate"}))
            marks["mode_switch_answer_s"] = await answer_after(t, before)
            await let_it_finish()
        before = phone.last_turn
        stopped = await phone.say(lines[i])
        marks["answers"].append(await answer_after(stopped, before))
        if i + 1 == CUT_IN_LINE:   # talk over this answer, 1.5 s into it
            turn = phone.last_turn
            await asyncio.sleep(1.5)
            t_cut = time.monotonic()
            speaking = asyncio.create_task(phone.say(lines[CUT_IN_LINE]))
            # the phone stops playback itself after 0.4 s of you talking over it (see controller.js)
            interrupted = await phone.until(lambda: phone.first("interrupted", lambda e: e["turn"] == turn), 0.4)
            if not interrupted:
                await phone.ws.send(json.dumps({"type": "cut_in"}))
                phone.flush(turn)
            t_int = phone.first("interrupted", lambda e: e["turn"] == turn)
            marks["cut_in"] = {"by": "gemini" if interrupted else "phone",
                               "after_s": round((t_int or time.monotonic()) - t_cut, 2)}
            stopped = await speaking
            marks["answers"].append(await answer_after(stopped, turn))
            i += 1
        await let_it_finish()
        i += 1

    t_hang = time.monotonic()
    await phone.ws.send(json.dumps({"type": "hangup"}))
    await phone.until(lambda: phone.of("saved") or phone.of("ended"), 120)
    saved_at = phone.first("saved")
    await phone.close()

    data = (await producer.get(f"/api/shows/{show_id}")).json()
    show, segs = data.get("show", {}), data.get("segments", [])
    md = [f"# Co-host call: {show.get('title')}", "", show.get("about", ""), "",
          "Chapters: " + " · ".join(f"{b['title']} ({b['start'] / RATE:.0f}s)" for b in show.get("beats", [])), ""]
    for s in segs:
        md += [f"**{ln['speaker']}:** {ln['text']}" for ln in s["lines"]] + [""]
    (out / "cohost.md").write_text("\n".join(md), encoding="utf-8")
    for name, params in (("cohost.mp3", {}), ("cohost-unedited.mp3", {"raw": 1})):
        audio = await producer.get(f"/api/shows/{show_id}/audio", params=params, follow_redirects=True)
        if audio.status_code == 200:
            (out / name).write_bytes(audio.content)
    answers = [a for a in marks["answers"] if a is not None]
    result = {
        "show_id": show_id, "saved": bool(saved_at), "title": show.get("title"),
        "opening_s": marks["opening_s"],
        "answers_s": marks["answers"],
        "answer_median_s": sorted(answers)[len(answers) // 2] if answers else None,
        "answer_max_s": max(answers) if answers else None,
        "cut_in": marks.get("cut_in"),
        "jump_ins": marks["jump_ins"],
        "mode_switch_answer_s": marks.get("mode_switch_answer_s"),
        "hangup_to_saved_s": round(saved_at - t_hang, 1) if saved_at else None,
        "call_s": show.get("call_s"), "episode_s": show.get("duration_s"), "edit": show.get("edit"),
        "segments": len(segs), "chapters": [b["title"] for b in show.get("beats", [])],
        "errors": [e.get("message") for e in phone.of("error")] + [e.get("message") for e in phone.of("ended")],
    }
    print(f"[co-host] done: {json.dumps(result)}", flush=True)
    return result


def cohost_report(result: dict, out: Path) -> None:
    rows = [
        ("Saved", result.get("saved")), ("Title", result.get("title")),
        ("Co-host opened the show after (s)", result.get("opening_s")),
        ("Answered after you stopped (s), each line", result.get("answers_s")),
        ("Answer latency median / max (s)", f"{result.get('answer_median_s')} / {result.get('answer_max_s')}"),
        ("Cut in: stopped by / after (s)", result.get("cut_in")),
        ("Co-host jumped in while you were still talking", result.get("jump_ins")),
        ("Answered a mode switch after (s)", result.get("mode_switch_answer_s")),
        ("Hang up to saved episode (s)", result.get("hangup_to_saved_s")),
        ("Call / episode length (s)", f"{result.get('call_s')} / {result.get('episode_s')}"),
        ("Edit", result.get("edit")), ("Chapters", result.get("chapters")), ("Errors", result.get("errors")),
    ]
    lines = [f"# Co-host live test, {datetime.now():%Y-%m-%d %H:%M}", "", "| | |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in rows]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    (out / "report.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    print("\n".join(lines))


async def main_async(minutes_list: list[int], out: Path, args: argparse.Namespace) -> list[dict]:
    async with httpx.AsyncClient(base_url=BASE, timeout=120) as producer, \
               httpx.AsyncClient(base_url=BASE, timeout=120) as guest:
        me = (await producer.post("/api/auth/dev", json={"email": "producer@live.test", "name": "Marc"})).json()["me"]
        await guest.post("/api/auth/dev", json={"email": "listener@live.test", "name": "Jo"})
        await producer.post(f"/api/admin/users/{me['uid']}/credits", json={"amount": 200, "note": "live test"})
        await producer.post("/api/connections", json={"email": "listener@live.test"})
        me_g = (await guest.get("/api/me")).json()["me"]
        await guest.post(f"/api/connections/{me['uid']}/accept")
        opts = (await producer.get("/api/options")).json()
        opts["visibility"] = "public" if args.public else "connections"
        opts["first_callin"] = args.first_callin
        cookies = {"producer": producer.cookies.get("gp_session"), "guest": guest.cookies.get("gp_session")}
        assert me_g["uid"]
        if args.cohost:
            try:
                return [await run_cohost(producer, cookies["producer"], opts, out)]
            except Exception as exc:
                return [{"crashed": f"{type(exc).__name__}: {exc}", "saved": False}]
        tasks = []
        for i, m in enumerate(minutes_list):
            async def one(m=m, delay=i * 20):
                await asyncio.sleep(delay)
                try:
                    return await run_pod(m, PODS[m], producer, guest, cookies, opts, out)
                except Exception as exc:  # keep the other pods going
                    return {"minutes": m, "crashed": f"{type(exc).__name__}: {exc}"}
            tasks.append(asyncio.create_task(one()))
        return await asyncio.gather(*tasks)


def report(results: list[dict], out: Path, log: str) -> None:
    posts = len(re.findall(r"HTTP Request: POST https://", log))
    r429 = len(re.findall(r'HTTP/1\.1 429', log))
    r5xx = len(re.findall(r'HTTP/1\.1 5\d\d', log))
    rewrites = len(re.findall(r"rewriting segment", log))
    rows = [
        ("Ended", "ended"), ("Saved", "saved"), ("Time to first audio (s)", "first_audio_s"),
        ("Length (s) / asked", None), ("Segments", "segments"), ("Chapters reached / planned", None),
        ("Stalls > 0.3 s", "stalls_over_300ms"), ("Total stall time (s)", "stall_total_s"),
        ("Longest stall (s)", "longest_stall_s"),
        ("Call-in 1: ETA shown / heard after (s)", "c1"), ("Call-in 2: ETA shown / heard after (s)", "c2"),
        ("Context", "context_status"),
        ("Context heard after (s)", "context_heard_after_s"), ("Restyle heard after (s)", "restyle_heard_after_s"),
        ("Credits charged / refunded", None), ("Errors", None),
    ]
    head = "| | " + " | ".join(f"{r['minutes']} min" for r in results) + " |"
    lines = [f"# Live test, {datetime.now():%Y-%m-%d %H:%M}", "", head, "|---" * (len(results) + 1) + "|"]
    for label, key in rows:
        cells = []
        for r in results:
            if "crashed" in r:
                cells.append(r["crashed"] if label == "Ended" else "")
            elif key in ("c1", "c2"):
                n = "" if key == "c1" else "2"
                cells.append(f"{r.get(f'comment{n}_eta_s')} / {r.get(f'comment{n}_heard_after_s')}")
            elif key:
                cells.append(str(r.get(key)))
            elif label.startswith("Length"):
                cells.append(f"{r['duration_s']} / {r['target_s']}")
            elif label.startswith("Chapters"):
                cells.append(f"{r['chapters_reached']} / {r['chapters']}")
            elif label.startswith("Credits"):
                cells.append(f"{r['credits_charged']} / {r['credits_refunded']}")
            else:
                cells.append(str(len(r["errors"])))
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    lines += ["", f"Gemini requests: {posts}, 429s: {r429}, 5xx: {r5xx}, draft rewrites: {rewrites}", ""]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    (out / "report.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    print("\n".join(lines))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, nargs="+", default=[5], choices=sorted(PODS))
    ap.add_argument("--public", action="store_true",
                    help="publish the pods so anyone can open the printed links and listen along")
    ap.add_argument("--first-callin", type=float, default=60, help="seconds into the pod for the first call-in")
    ap.add_argument("--cohost", action="store_true", help="a co-host call (Gemini Live) instead of pods")
    args = ap.parse_args()
    out = ROOT / "data" / "live-tests" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "STORAGE_BACKEND": "local", "DATA_DIR": str(out / "data"), "AUTH_MODE": "dev",
           "ADMIN_EMAILS": "producer@live.test", "SESSION_SECRET": secrets.token_urlsafe(24),
           "CLOCK_SPEED": "1", "MAX_LIVE_SHOWS": "3"}
    env.pop("GEMINI_BASE_URL", None)
    log_path = out / "app.log"
    srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)],
                           cwd=ROOT, env=env, stdout=open(log_path, "wb"), stderr=subprocess.STDOUT)
    try:
        for _ in range(100):
            try:
                httpx.get(f"{BASE}/healthz", timeout=1)
                break
            except httpx.HTTPError:
                time.sleep(0.2)
        results = asyncio.run(main_async(args.minutes, out, args))
    finally:
        srv.terminate()
        srv.wait(timeout=15)
    if args.cohost:
        cohost_report(results[0], out)
    else:
        report(results, out, log_path.read_text(encoding="utf-8", errors="replace"))
    print(f"\nTranscripts, MP3s and the app log are in {out}")
    return 0 if all(r.get("saved") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
