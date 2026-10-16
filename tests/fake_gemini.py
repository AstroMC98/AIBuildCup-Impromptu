"""A tiny stand-in for the Gemini Interactions endpoint, for offline testing.

Run:  uvicorn tests.fake_gemini:app --port 8765
Then: GEMINI_API_KEY=fake GEMINI_BASE_URL=http://127.0.0.1:8765 uvicorn app.main:app

Planner calls get a fixed plan (with Research on, chapter 3 asks for a lookup).
Writer calls get canned dialogue that answers any "[#id]" comments in the prompt,
uses any "[Cn]" context it is told to bring in, acknowledges a STYLE CHANGED, and
declines comments containing DECLINE_ME. Comments containing DEFER_ME are promised
for later ("we'll get to that in a second", deferred); SNEAKY_DEFER does the same
but marks it answered (the bug the runner guards against); RESEARCH_ME asks for a
lookup when the schema allows it. PROMISED call-ins are answered. Google Search
calls get a grounded answer: search steps, Search Suggestions, url_citation notes.
Image calls get a canned description. TTS calls stream a sine tone as PCM. Editor
calls (co-hosted calls) cut "can you hear me" phrases and drop "Loud and clear" answers.
Voice memos (audio asked for a memo brief) get a canned brief, or an unusable one if
the audio contains the bytes SILENT; other audio (the producer's spoken questions) is
transcribed. A memo's research gets grounded findings. Lyria calls get an MP3 tone.

The Live API (co-hosting) is a WebSocket that answers speech: loud mic audio
followed by 0.4 s of quiet is an utterance, transcribed as "Mic check, can you hear
me?" the first time and "Point number N about bread." after that, and answered
with a 3 s tone streamed at twice real time. Loud audio while it's answering
interrupts it. Producer cues get a short spoken reply; after replying to a mode
switch it recycles the connection (GoAway), as Gemini does every few minutes. A
session given the check_notes tool calls it on the first thing you say ("the house
loaf's hydration") and answers only once the tool's response comes back. A brief of
long notes (a text call asking what the co-host keeps in mind) gets a canned brief.

GET /_seen returns the writer system instructions, Live setups and Live text
received, and how many memo briefs, memo research and music calls came in.
POST /_config {"lyria_fail": true, "search_json_400": true} makes Lyria fail, or
refuses search with a JSON answer (as a model without that feature would).
"""
import asyncio
import base64
import itertools
import json
import math
import os
import re
import struct
import traceback

import numpy as np
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, StreamingResponse

from app.audio import Mp3Writer

app = FastAPI()
# Writer calls per beat. The smoke test wants short shows; set FAKE_BEAT_EVERY=6
# for a longer show when clicking around the UI.
BEAT_EVERY = int(os.environ.get("FAKE_BEAT_EVERY", "2"))
_calls = itertools.count(1)
_writer_calls = itertools.count(1)
requests_seen: list[dict] = []
writer_systems: list[str] = []
config = {"lyria_fail": False, "search_json_400": False}
counts = {"memo_briefs": 0, "memo_research": 0, "music": 0, "note_briefs": 0}

BRIEF = {
    "usable": True, "reason": "", "language": "English",
    "transcript": "So I was walking home and the city sounded way louder than it does in the day. "
                  "Is that real? Does sound carry farther at night? And does it mess with sleep?",
    "working_title": "Why Cities Sound Louder at Night",
    "thesis": "Cities seem louder at night, and that might be physics, not just quiet.",
    "key_ideas": ["Night feels louder than day", "Maybe sound travels differently at night"],
    "questions": ["Does sound really carry farther at night?", "Does night noise hurt sleep?"],
    "claims_to_check": ["Sound travels farther at night."],
    "research_queries": ["does sound travel farther at night temperature inversion",
                         "night traffic noise sleep health"],
    "tone": "curious", "music_mood": "calm", "format": "idea-breakdown",
}
RESEARCH = {
    "summary": "Sound does carry farther on calm nights, and night noise is linked to poorer sleep.",
    "findings": [
        {"claim": "On calm, clear nights a temperature inversion bends sound back to the ground, so it carries "
                  "farther.", "outlet": "Acoustical Society of America", "date": "2023", "answers": [1],
         "confidence": "solid"},
        {"claim": "Night traffic noise above 40 dB is linked to disturbed sleep.", "outlet": "World Health "
         "Organization", "date": "2018", "answers": [2], "confidence": "solid"},
        {"claim": "How much of 'louder at night' is perception is still debated.", "outlet": "Nature Cities",
         "date": "2025", "answers": [1], "confidence": "mixed"},
    ],
    "disagreements": ["Researchers disagree on how much of the effect is perception."],
    "unanswered": [],
}
CITATIONS = [
    {"type": "url_citation", "url": "https://www.who.int/publications/night-noise", "title": "Night noise guidelines",
     "start_index": 0, "end_index": 20},
    {"type": "url_citation", "url": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AbC123",
     "title": "acousticalsociety.org", "start_index": 21, "end_index": 40},
]
MEMO_SUGGESTIONS = '<div class="container"><a class="chip" href="https://www.google.com/search?q=night+noise">night noise</a></div>'
live_setups: list[dict] = []
live_texts: list[str] = []
live_tool_responses: list[dict] = []
_live_sessions = itertools.count(1)
_utterances = itertools.count(1)


def _tone(seconds: float, freq: float, rate: int = 24_000) -> bytes:
    n = int(seconds * rate)
    return b"".join(struct.pack("<h", int(2500 * math.sin(2 * math.pi * freq * i / rate))) for i in range(n))


def _reply(text: str) -> JSONResponse:
    return JSONResponse({
        "id": f"t{next(_calls)}", "status": "completed",
        "steps": [{"type": "model_output", "content": [{"type": "text", "text": text}]}],
    })


def _section(prompt: str, header: str) -> str:
    start = prompt.find(header)
    if start < 0:
        return ""
    end = prompt.find("\n\n", start)
    return prompt[start:end if end > 0 else None]


def _error(status: int, message: str) -> JSONResponse:
    code = "INVALID_ARGUMENT" if status == 400 else "INTERNAL"
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _parts(value):
    """Every typed content part anywhere in a request's input."""
    if isinstance(value, dict):
        if "type" in value:
            yield value
        for v in value.values():
            yield from _parts(v)
    elif isinstance(value, list):
        for v in value:
            yield from _parts(v)


def _theme_mp3(seconds: float = 30.0, rate: int = 24_000) -> bytes:
    t = np.arange(int(seconds * rate)) / rate
    chord = sum(np.sin(2 * np.pi * f * t) for f in (220, 277, 330)) / 3 * (0.5 + 0.3 * np.sin(2 * np.pi * t / 7))
    writer = Mp3Writer(rate, 64)
    writer.feed(np.rint(chord * 0.4 * 32767).astype("<i2").tobytes())
    return writer.finish()


def _grounded_memo(text: str) -> JSONResponse:
    return JSONResponse({
        "id": f"t{next(_calls)}", "status": "completed",
        "steps": [
            {"type": "google_search_call", "id": "s1", "arguments": {"queries": BRIEF["research_queries"]}},
            {"type": "google_search_result", "call_id": "s1", "result": [{"search_suggestions": MEMO_SUGGESTIONS}]},
            {"type": "model_output", "content": [{"type": "text", "text": text, "annotations": CITATIONS}]},
        ],
    })


writer_schemas: list[list[str]] = []      # the properties each writer call's schema offered
research_asked: list[str] = []
SUGGESTIONS = '<div class="container"><a class="chip" href="https://www.google.com/search?q=zion">zion</a></div>'


@app.get("/_seen")
async def seen() -> dict:
    return {"writer_systems": writer_systems, "count": len(requests_seen),
            "live_setups": live_setups, "live_texts": live_texts, "live_tool_responses": live_tool_responses,
            "writer_schemas": writer_schemas, "research": research_asked, **counts}


@app.post("/_config")
async def configure(req: Request) -> dict:
    config.update(await req.json())
    return config


def _grounded(question: str) -> JSONResponse:
    research_asked.append(question)
    return JSONResponse({
        "id": f"t{next(_calls)}", "status": "completed",
        "steps": [
            {"type": "google_search_call", "id": "s1", "arguments": {"queries": [question[:60]]}},
            {"type": "google_search_result", "call_id": "s1", "result": [{"search_suggestions": SUGGESTIONS}]},
            {"type": "model_output", "content": [{
                "type": "text", "text": "RESEARCH FINDINGS: Zion Williamson averaged 24 points per game (ESPN).",
                "annotations": [
                    {"type": "url_citation", "url": "https://www.espn.com/nba/player/_/id/zion", "title": "ESPN",
                     "start_index": 0, "end_index": 20},
                    {"type": "url_citation", "url": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/X",
                     "title": "nbcsports.com", "start_index": 21, "end_index": 40},
                ]}]},
        ],
    })


@app.post("/v1beta/interactions")
async def interactions(req: Request):
    body = await req.json()
    requests_seen.append(body)
    model = body.get("model", "")

    if "tts" in model:
        turns = body["input"][0]["content"]
        seconds = min(sum(len(t["text"]) for t in turns) / 40, 6.0)
        pcm = _tone(seconds, 330 + 40 * (len(requests_seen) % 5))

        async def events():
            def sse(obj):
                return "data: " + json.dumps(obj) + "\n\n"
            yield sse({"event_type": "interaction.created", "interaction": {"id": "a", "status": "in_progress"}})
            yield sse({"event_type": "step.start", "index": 0, "step": {"type": "model_output"}})
            step = 4801  # odd on purpose: the server must keep samples whole
            for i in range(0, len(pcm), step):
                await asyncio.sleep(0.01)
                chunk = base64.b64encode(pcm[i:i + step]).decode()
                yield sse({"event_type": "step.delta", "index": 0,
                           "delta": {"type": "audio", "mime_type": "audio/l16", "data": chunk}})
            yield sse({"event_type": "step.stop", "index": 0})
            yield sse({"event_type": "interaction.completed", "interaction": {"id": "a", "status": "completed"}})

        return StreamingResponse(events(), media_type="text/event-stream")

    if model.startswith("lyria"):
        counts["music"] += 1
        if config["lyria_fail"]:
            return _error(400, "The music couldn't be generated.")
        audio = {"type": "audio", "mime_type": "audio/mpeg", "data": base64.b64encode(_theme_mp3()).decode()}
        return JSONResponse({"id": f"t{next(_calls)}", "status": "completed", "model": model,
                             "steps": [{"type": "model_output", "content": [audio]}]})

    await asyncio.sleep(0.2)
    # The SDK wraps content lists in a user_input turn, so look anywhere in the input.
    parts = list(_parts(body.get("input")))
    if any(p["type"] == "image" for p in parts):
        return _reply("A hand-drawn family tree: Adam and Eve at the top, Cain and Abel below them.")
    props = body.get("response_format", {}).get("schema", {}).get("properties", {})

    audio = next((p for p in parts if p["type"] == "audio"), None)
    if audio is not None and "thesis" in props:      # a voice memo, for its brief
        counts["memo_briefs"] += 1
        if b"SILENT" in base64.b64decode(audio.get("data", "")):
            return _reply(json.dumps({**BRIEF, "usable": False, "transcript": "",
                                      "reason": "There's no speech in this memo."}))
        return _reply(json.dumps(BRIEF))
    if audio is not None:                            # the producer's spoken question
        return _reply("FAKE TRANSCRIPT: why did the serpent talk?")
    if "the co-host keeps in mind" in str(body.get("system_instruction", "")):   # a brief of long notes
        counts["note_briefs"] += 1
        return _reply("FAKE BRIEF: the notes are about sourdough, part by part; the house loaf's numbers are in there.")

    searching = any(t.get("type") == "google_search" for t in body.get("tools") or [])
    if "items" in props and searching:   # a briefing's news search
        asked = re.search(r"about: (.+)", str(body.get("input", "")))
        topic = asked.group(1).strip() if asked else "the news"
        items = [{"title": f"{topic} story {i}", "source": f"Outlet {i}", "url": f"https://outlet{i}.example/{i}",
                  "published": "2026-10-14", "summary": f"Summary {i} about {topic}."} for i in (1, 2, 3, 4)]
        return JSONResponse({"id": f"t{next(_calls)}", "status": "completed", "steps": [
            {"type": "google_search_call", "id": "c1", "arguments": {"queries": [topic]}},
            {"type": "google_search_result", "call_id": "c1",
             "result": [{"search_suggestions": "<div class=\"fake-suggestions\">FAKE SUGGESTIONS</div>"}]},
            {"type": "model_output", "content": [{"type": "text", "text": json.dumps({"items": items})}]},
        ]})

    if "beats" in props and "hosts" not in props:  # a re-plan after the producer steered
        return _reply(json.dumps({"beats": [
            {"title": "STEERED: the other side", "notes": "...", "minutes": 0.5},
            {"title": "STEERED: sign-off", "notes": "...", "minutes": 0.3},
        ]}))
    if searching and ("findings" in props or "research producer" in str(body.get("system_instruction", ""))):
        if props and config["search_json_400"]:      # a voice memo's research
            return _error(400, "Tool use with a response format is unsupported for this model.")
        counts["memo_research"] += 1
        notes = "\n".join(f"- {f['claim']} ({f['outlet']}, {f['date']})" for f in RESEARCH["findings"])
        return _grounded_memo(json.dumps(RESEARCH) if props else notes)
    if searching:
        return _grounded(str(body.get("input", "")))
    if "findings" in props:            # a memo's research notes, written up as JSON
        return _reply(json.dumps(RESEARCH))

    if "cut_phrases" in props:   # the editor of a co-hosted call
        prompt = body.get("input", "")
        cut = [int(p) for p in re.findall(r"\[p(\d+)\][^\n]*can you hear me", prompt, re.I)]
        drop = [int(t) for t in re.findall(r"^T(\d+) [^\n:]*: Loud and clear", prompt, re.M)]
        turns = [int(t) for t in re.findall(r"^T(\d+) ", prompt, re.M)]
        return _reply(json.dumps({
            "title": "Fake Edited Episode",
            "tagline": "Bread, debated.",
            "about": "A co-hosted chat about bread.",
            "chapters": [{"title": "Opening", "notes": "Hello.", "start_turn": 0},
                         {"title": "The middle", "notes": "Bread.", "start_turn": turns[len(turns) // 2] if turns else 0}],
            "cut_phrases": cut,
            "drop_turns": drop,
        }))

    if "beats" in props:
        n = props["hosts"]["minItems"]
        hosts = [{"name": "Kayla", "gender": "female", "persona": "Lives for drama.", "voice": "Leda"},
                 {"name": "Brit", "gender": "female", "persona": "Fact-checks everything.", "voice": "Kore"}][:n]
        # A briefing's planner input carries its run of show: keep those beats (shortened for tests).
        given = re.findall(r"^\d+\. (.+?) \| [\d.]+ min \|", str(body.get("input", "")), re.MULTILINE)
        beats = [(t, 0.4) for t in given] or [("Cold open", 0.3), ("The garden", 0.6), ("The fallout", 0.6),
                                             ("Sign-off", 0.5)]
        research = "research" in props["beats"]["items"]["properties"]   # Research on
        return _reply(json.dumps({
            "title": "Genesis, But Make It Messy",
            "tagline": "The first family had the first group chat.",
            "about": "Kayla and Brit spill everything from Genesis 1 to 3.",
            "hosts": hosts,
            "beats": [{"title": t, "notes": "...", "minutes": m,
                       **({"research": "garden of eden location theories"} if research and t == "The fallout" else {})}
                      for t, m in beats],
            "cold_open": [{"speaker": h["name"], "text": f"COLD OPEN from {h['name']}."} for h in hosts * 2][:3],
            "reactions": [{"speaker": hosts[i % n]["name"], "kind": k, "text": f"REACTION {k}!"}
                          for i, k in enumerate(("ask", "deeper", "steer", "move_on"))],
        }))

    writer_systems.append(body.get("system_instruction", ""))
    writer_schemas.append(sorted(props))
    names = props["lines"]["items"]["properties"]["speaker"]["enum"]
    prompt = body.get("input", "")
    comment_re = r"\[#(\d+)\] ([^:]+): \"([^\"]*)\""
    comments = re.findall(comment_re, _section(prompt, "LISTENER COMMENTS"))
    owed = re.findall(comment_re, _section(prompt, "CALL-INS YOU PROMISED"))
    bring = [int(x) for x in re.findall(r"\[C(\d+)\]", _section(prompt, "BRING IN NOW"))]
    chapter = [int(x) for x in re.findall(r"\[C(\d+)\]", _section(prompt, "THIS CHAPTER"))]
    attached = [int(x) for x in re.findall(r"attached \[C(\d+)\]", prompt)]
    final = "FINAL SEGMENT" in prompt
    n = next(_writer_calls)

    if "THE PRODUCER JUST INTERRUPTED THE SHOW" in prompt:
        kind = next(k for k, marker in (("ask", "They asked:"), ("deeper", "more depth"),
                                         ("steer", "a different way"), ("move_on", "move on")) if marker in prompt)
        asked = re.search(r'They asked: "([^"]*)"', prompt)
        cut = re.search(r'the last thing said was: "([^"]*)"', prompt)
        return _reply(json.dumps({
            "lines": [{"speaker": names[0], "text": f"INTERJECTION {kind}: {asked.group(1) if asked else ''}", "style": ""},
                      {"speaker": names[-1], "text": f"CUT AFTER: {cut.group(1) if cut else ''}", "style": ""}],
            "addressed_comment_ids": [], "declined_comment_ids": [], "used_context_ids": [], "beat_complete": False,
        }))

    lines, addressed, declined, deferred, lookups = [], [], [], [], []
    if "STYLE CHANGED" in prompt:
        lines.append({"speaker": names[0], "text": "Okay, STYLE SWITCH ACKNOWLEDGED, new vibe!", "style": ""})
    for cid, who, _text in owed:
        addressed.append(int(cid))
        found = "with the research" if "research came back" in prompt else "from memory"
        lines.append({"speaker": names[0], "text": f"As promised, back to {who}'s question {cid}, answered {found}.",
                      "style": ""})
    for cid, who, text in comments:
        if "DECLINE_ME" in text:
            declined.append(int(cid))
            continue
        if "DEFER_ME" in text or "SNEAKY_DEFER" in text:
            (addressed if "SNEAKY_DEFER" in text else deferred).append(int(cid))
            lines.append({"speaker": names[0], "text": f"Ooh {who}, we'll get to that in a second!", "style": ""})
            continue
        if "RESEARCH_ME" in text and "research_requests" in props:
            lookups.append({"comment_id": int(cid), "query": "Zion Williamson fantasy outlook"})
            lines.append({"speaker": names[0], "text": f"{who}, let me pull up those numbers.", "style": ""})
            continue
        addressed.append(int(cid))
        lines.append({"speaker": names[0], "text": f"Okay wait, {who} called in <gasp> with comment number {cid}!",
                      "style": "excited"})
    used = bring + chapter + attached
    for xid in bring + chapter:
        lines.append({"speaker": names[-1], "text": f"Someone sent us context C{xid}, love it.", "style": ""})
    for i in range(4):
        lines.append({"speaker": names[i % len(names)], "text": f"Line {i} of segment draft {n} |no way| about the garden.",
                      "style": ""})
    if final:
        lines.append({"speaker": names[0], "text": "That's the show, bye!", "style": ""})
    return _reply(json.dumps({
        "lines": lines, "addressed_comment_ids": addressed, "declined_comment_ids": declined,
        "deferred_comment_ids": deferred, "research_requests": lookups,
        "used_context_ids": used, "beat_complete": n % BEAT_EVERY == 0 or final,
    }))


# ---- the Live API (co-hosting) ----------------------------------------------------------

LIVE_PATH = "/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
MIC_RATE = 16_000
REPLY_SECONDS = 3.0
PACE = 0.5            # send the reply at twice real time, as Gemini does (faster than playback)


def _cue_reply(text: str) -> str:
    if "Open the show" in text:
        return "Welcome to the show! Great to have you here."
    if "switch it up" in text:
        return "Switching it up, gloves off!"
    if "wrapping up" in text:
        return "That's our time, thanks everyone!"
    if "line dropped" in text:
        return "And we're back!"
    return "Mm-hm."


@app.websocket(LIVE_PATH)
async def live(ws: WebSocket) -> None:
    await ws.accept()
    setup = json.loads(await ws.receive_text())
    live_setups.append(setup)
    session = next(_live_sessions)
    lock = asyncio.Lock()
    lookup = {"tool": "check_notes" in json.dumps(setup.get("setup", {}).get("tools") or []),
              "asked": False, "waiting": 0}

    async def send(obj: dict) -> None:
        async with lock:
            await ws.send_text(json.dumps(obj))

    await send({"setupComplete": {}})
    await send({"sessionResumptionUpdate": {"newHandle": f"handle-{session}", "resumable": True}})

    stop = {"now": False}
    speaking: list[asyncio.Task] = []

    async def respond(text: str, then_goaway: bool = False) -> None:
        pcm = _tone(REPLY_SECONDS, 440)
        step = 4801  # odd on purpose: the server must keep samples whole
        for i, off in enumerate(range(0, len(pcm), step)):
            if stop["now"]:
                return
            chunk = pcm[off:off + step]
            await send({"serverContent": {"modelTurn": {"parts": [
                {"inlineData": {"mimeType": "audio/pcm;rate=24000", "data": base64.b64encode(chunk).decode()}}]}}})
            if i == 0:
                await send({"serverContent": {"outputTranscription": {"text": text}}})
            await asyncio.sleep(len(chunk) / 2 / 24_000 * PACE)
        await send({"serverContent": {"turnComplete": True}})
        if then_goaway:
            await send({"goAway": {"timeLeft": "1s"}})
            await asyncio.sleep(0.5)
            await ws.close()

    async def reply(text: str, then_goaway: bool = False) -> None:
        await interrupt(quietly=True)
        stop["now"] = False
        speaking.append(asyncio.create_task(respond(text, then_goaway)))

    async def interrupt(quietly: bool = False) -> None:
        running = [t for t in speaking if not t.done()]
        if running:
            stop["now"] = True
            await asyncio.gather(*running, return_exceptions=True)
            if not quietly:
                await send({"serverContent": {"interrupted": True}})
        speaking.clear()

    loud = quiet = 0
    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            raw = message.get("text") or (message.get("bytes") or b"").decode()
            data = json.loads(raw)
            tool = data.get("tool_response") or data.get("toolResponse")
            if tool:
                answers = tool.get("function_responses") or tool.get("functionResponses") or []
                live_tool_responses.extend(answers)
                passages = (answers[0].get("response") or {}).get("passages") or [""] if answers else [""]
                await reply(f"Reply {lookup['waiting']}: my notes say {passages[0]}")
                continue
            rt = data.get("realtime_input") or data.get("realtimeInput") or {}
            if "text" in rt:
                live_texts.append(rt["text"])
                await reply(_cue_reply(rt["text"]), then_goaway="switch it up" in rt["text"])
            if rt.get("audioStreamEnd"):
                loud = quiet = 0
            audio = rt.get("audio")
            if audio:
                b64 = audio["data"]   # the SDK sends URL-safe base64 without padding
                pcm = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
                samples = struct.unpack(f"<{len(pcm) // 2}h", pcm[: len(pcm) // 2 * 2])
                rms = math.sqrt(sum(s * s for s in samples) / max(1, len(samples)))
                if rms > 500:
                    if loud == 0:
                        await interrupt()          # barge-in
                    loud += len(samples)
                    quiet = 0
                elif loud:
                    quiet += len(samples)
                    if loud >= 0.2 * MIC_RATE and quiet >= 0.4 * MIC_RATE:
                        n = next(_utterances)
                        said = "Mic check, can you hear me?" if n == 1 else f"Point number {n} about bread."
                        await send({"serverContent": {"inputTranscription": {"text": said}}})
                        if lookup["tool"] and not lookup["asked"]:   # check the notes, then answer
                            lookup.update(asked=True, waiting=n)
                            await send({"toolCall": {"functionCalls": [
                                {"id": f"fc-{session}", "name": "check_notes",
                                 "args": {"query": "the house loaf's hydration"}}]}})
                        else:
                            await reply("Loud and clear!" if n == 1 else f"Reply {n}: great point about bread.")
                        loud = quiet = 0
    except Exception:
        traceback.print_exc()
    finally:
        stop["now"] = True
