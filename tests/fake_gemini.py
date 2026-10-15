"""A tiny stand-in for the Gemini Interactions endpoint, for offline testing.

Run:  uvicorn tests.fake_gemini:app --port 8765
Then: GEMINI_API_KEY=fake GEMINI_BASE_URL=http://127.0.0.1:8765 uvicorn app.main:app

Planner calls get a fixed plan. Writer calls get canned dialogue that answers any
"[#id]" comments in the prompt, uses any "[Cn]" context it is told to bring in,
acknowledges a STYLE CHANGED, and declines comments containing DECLINE_ME. Image
calls get a canned description. TTS calls stream a sine tone as PCM.
Voice memos (audio input) get a canned brief, or an unusable one if the audio
contains the bytes SILENT. Google Search calls get grounded research: search steps,
Search Suggestions and url_citation annotations. Lyria calls get an MP3 tone.
GET /_seen returns the writer system instructions received, for assertions.
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

import numpy as np
from fastapi import FastAPI, Request
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
counts = {"memo": 0, "research": 0, "music": 0}

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
SUGGESTIONS = '<div class="container"><a class="chip" href="https://www.google.com/search?q=night+noise">night noise</a></div>'


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


def _grounded(text: str) -> JSONResponse:
    return JSONResponse({
        "id": f"t{next(_calls)}", "status": "completed",
        "steps": [
            {"type": "google_search_call", "id": "s1", "arguments": {"queries": BRIEF["research_queries"]}},
            {"type": "google_search_result", "call_id": "s1", "result": [{"search_suggestions": SUGGESTIONS}]},
            {"type": "model_output", "content": [{"type": "text", "text": text, "annotations": CITATIONS}]},
        ],
    })


@app.get("/_seen")
async def seen() -> dict:
    return {"writer_systems": writer_systems, "count": len(requests_seen), **counts}


@app.post("/_config")
async def configure(req: Request) -> dict:
    config.update(await req.json())
    return config


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
    audio = next((p for p in parts if p["type"] == "audio"), None)
    if audio is not None:
        counts["memo"] += 1
        if b"SILENT" in base64.b64decode(audio.get("data", "")):
            return _reply(json.dumps({**BRIEF, "usable": False, "transcript": "",
                                      "reason": "There's no speech in this memo."}))
        return _reply(json.dumps(BRIEF))

    props = body.get("response_format", {}).get("schema", {}).get("properties", {})

    if any(t.get("type") == "google_search" for t in body.get("tools") or []):
        if props and config["search_json_400"]:
            return _error(400, "Tool use with a response format is unsupported for this model.")
        counts["research"] += 1
        notes = "\n".join(f"- {f['claim']} ({f['outlet']}, {f['date']})" for f in RESEARCH["findings"])
        return _grounded(json.dumps(RESEARCH) if props else notes)
    if "findings" in props:            # notes from a search, written up as JSON
        return _reply(json.dumps(RESEARCH))

    if "beats" in props:
        n = props["hosts"]["minItems"]
        hosts = [{"name": "Kayla", "gender": "female", "persona": "Lives for drama.", "voice": "Leda"},
                 {"name": "Brit", "gender": "female", "persona": "Fact-checks everything.", "voice": "Kore"}][:n]
        return _reply(json.dumps({
            "title": "Genesis, But Make It Messy",
            "tagline": "The first family had the first group chat.",
            "about": "Kayla and Brit spill everything from Genesis 1 to 3.",
            "hosts": hosts,
            "beats": [{"title": t, "notes": "...", "minutes": m}
                      for t, m in (("Cold open", 0.3), ("The garden", 0.6), ("The fallout", 0.6), ("Sign-off", 0.5))],
            "cold_open": [{"speaker": h["name"], "text": f"COLD OPEN from {h['name']}."} for h in hosts * 2][:3],
        }))

    writer_systems.append(body.get("system_instruction", ""))
    names = props["lines"]["items"]["properties"]["speaker"]["enum"]
    prompt = body.get("input", "")
    comments = re.findall(r"\[#(\d+)\] ([^:]+): \"([^\"]*)\"", prompt)
    bring = [int(x) for x in re.findall(r"\[C(\d+)\]", _section(prompt, "BRING IN NOW"))]
    chapter = [int(x) for x in re.findall(r"\[C(\d+)\]", _section(prompt, "THIS CHAPTER"))]
    attached = [int(x) for x in re.findall(r"attached \[C(\d+)\]", prompt)]
    final = "FINAL SEGMENT" in prompt
    n = next(_writer_calls)

    lines, addressed, declined = [], [], []
    if "STYLE CHANGED" in prompt:
        lines.append({"speaker": names[0], "text": "Okay, STYLE SWITCH ACKNOWLEDGED, new vibe!", "style": ""})
    for cid, who, text in comments:
        if "DECLINE_ME" in text:
            declined.append(int(cid))
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
        "used_context_ids": used, "beat_complete": n % BEAT_EVERY == 0 or final,
    }))
