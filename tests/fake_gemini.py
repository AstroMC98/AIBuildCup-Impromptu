"""A tiny stand-in for the Gemini Interactions endpoint, for offline testing.

Run:  uvicorn tests.fake_gemini:app --port 8765
Then: GEMINI_API_KEY=fake GEMINI_BASE_URL=http://127.0.0.1:8765 uvicorn app.main:app

Planner calls get a fixed plan. Writer calls get canned dialogue that answers any
"[#id]" comments in the prompt, uses any "[Cn]" context it is told to bring in,
acknowledges a STYLE CHANGED, and declines comments containing DECLINE_ME. Image
calls get a canned description. TTS calls stream a sine tone as PCM. Editor calls
(co-hosted calls) cut "can you hear me" phrases and drop "Loud and clear" answers.

The Live API (co-hosting) is a WebSocket that answers speech: loud mic audio
followed by 0.4 s of quiet is an utterance, transcribed as "Mic check, can you hear
me?" the first time and "Point number N about bread." after that, and answered
with a 3 s tone streamed at twice real time. Loud audio while it's answering
interrupts it. Producer cues get a short spoken reply; after replying to a mode
switch it recycles the connection (GoAway), as Gemini does every few minutes.

GET /_seen returns the writer system instructions, Live setups and Live text
received, for assertions.
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

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
# Writer calls per beat. The smoke test wants short shows; set FAKE_BEAT_EVERY=6
# for a longer show when clicking around the UI.
BEAT_EVERY = int(os.environ.get("FAKE_BEAT_EVERY", "2"))
_calls = itertools.count(1)
_writer_calls = itertools.count(1)
requests_seen: list[dict] = []
writer_systems: list[str] = []
live_setups: list[dict] = []
live_texts: list[str] = []
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


@app.get("/_seen")
async def seen() -> dict:
    return {"writer_systems": writer_systems, "count": len(requests_seen),
            "live_setups": live_setups, "live_texts": live_texts}


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

    props = body.get("response_format", {}).get("schema", {}).get("properties", {})

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
                        await reply("Loud and clear!" if n == 1 else f"Reply {n}: great point about bread.")
                        loud = quiet = 0
    except Exception:
        traceback.print_exc()
    finally:
        stop["now"] = True
