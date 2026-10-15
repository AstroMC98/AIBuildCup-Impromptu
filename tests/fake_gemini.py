"""A tiny stand-in for the Gemini Interactions endpoint, for offline testing.

Run:  uvicorn tests.fake_gemini:app --port 8765
Then: GEMINI_API_KEY=fake GEMINI_BASE_URL=http://127.0.0.1:8765 uvicorn app.main:app

Planner calls get a fixed plan. Writer calls get canned dialogue that answers any
"[#id]" comments in the prompt, uses any "[Cn]" context it is told to bring in,
acknowledges a STYLE CHANGED, and declines comments containing DECLINE_ME. Image
calls get a canned description. TTS calls stream a sine tone as PCM.
GET /_seen returns the writer system instructions received, for assertions.
"""
import asyncio
import base64
import itertools
import json
import math
import os
import re
import struct

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
# Writer calls per beat. The smoke test wants short shows; set FAKE_BEAT_EVERY=6
# for a longer show when clicking around the UI.
BEAT_EVERY = int(os.environ.get("FAKE_BEAT_EVERY", "2"))
_calls = itertools.count(1)
_writer_calls = itertools.count(1)
requests_seen: list[dict] = []
writer_systems: list[str] = []


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


@app.get("/_seen")
async def seen() -> dict:
    return {"writer_systems": writer_systems, "count": len(requests_seen)}


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

    await asyncio.sleep(0.2)
    # The SDK wraps content lists in a user_input turn, so look anywhere in the input.
    if '"type": "image"' in json.dumps(body.get("input")):
        return _reply("A hand-drawn family tree: Adam and Eve at the top, Cain and Abel below them.")
    if '"type": "audio"' in json.dumps(body.get("input")):
        return _reply("FAKE TRANSCRIPT: why did the serpent talk?")

    props = body.get("response_format", {}).get("schema", {}).get("properties", {})

    if "beats" in props and "hosts" not in props:  # a re-plan after the producer steered
        return _reply(json.dumps({"beats": [
            {"title": "STEERED: the other side", "notes": "...", "minutes": 0.5},
            {"title": "STEERED: sign-off", "notes": "...", "minutes": 0.3},
        ]}))

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
            "reactions": [{"speaker": hosts[i % n]["name"], "kind": k, "text": f"REACTION {k}!"}
                          for i, k in enumerate(("ask", "deeper", "steer", "move_on"))],
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
