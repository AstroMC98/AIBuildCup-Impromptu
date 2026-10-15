"""Every call to Gemini goes through this module.

The rest of the app only sees plain Python objects (ShowPlan, SegmentDraft,
PCM bytes), so swapping a model or provider is a change here and nowhere else.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from typing import AsyncIterator

from google import genai
from pydantic import ValidationError

from . import prompts
from .catalog import GENDERS, VOICE_GENDER, VOICES, pick_voice
from .config import Settings
from .models import ContextItem, Host, Line, SegmentContext, SegmentDraft, ShowPlan, ShowRequest

log = logging.getLogger("podcasts.llm")

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


class ModelOutputError(RuntimeError):
    """The model answered, but not with something usable."""


def _parse_json(text: str | None) -> dict:
    if not text:
        raise ModelOutputError("empty response")
    try:
        return json.loads(_FENCE.sub("", text.strip()))
    except json.JSONDecodeError as exc:
        raise ModelOutputError(f"invalid JSON: {exc}") from exc


class Gemini:
    def __init__(self, settings: Settings):
        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is not set. Add it to .env and restart the server.")
        http_options = {"base_url": settings.gemini_base_url} if settings.gemini_base_url else None
        self.client = genai.Client(api_key=settings.gemini_api_key, http_options=http_options)
        self.settings = settings

    # ---- text ---------------------------------------------------------------

    async def _json_call(self, *, system: str, user: str, schema: dict) -> dict:
        interaction = await self.client.aio.interactions.create(
            model=self.settings.script_model,
            system_instruction=system,
            input=user,
            response_format={"type": "text", "mime_type": "application/json", "schema": schema},
            generation_config={"thinking_level": self.settings.script_thinking_level},
        )
        return _parse_json(interaction.output_text)

    async def plan_show(self, req: ShowRequest, source: str) -> ShowPlan:
        data = await self._json_call(
            system=prompts.planner_system(req, source),
            user=prompts.PLANNER_INPUT,
            schema=prompts.plan_schema(len(req.hosts)),
        )
        try:
            plan = ShowPlan.model_validate(data)
        except ValidationError as exc:
            raise ModelOutputError(f"plan did not match the schema: {exc}") from exc
        planned_names = [h.name for h in plan.hosts]
        plan.hosts = _reconcile_hosts(req, plan.hosts)
        plan.cold_open = _cast_cold_open(plan.cold_open, planned_names, plan.hosts)
        _normalize_minutes(plan, req.minutes)
        return plan

    async def write_segment(
        self,
        ctx: SegmentContext,
        req: ShowRequest,
        plan: ShowPlan,
        source: str,
        style: str,
        context: list[ContextItem],
    ) -> SegmentDraft:
        names = [h.name for h in plan.hosts]
        system = prompts.writer_system(req, plan, source, style, context)
        user = prompts.writer_input(ctx, plan)
        schema = prompts.segment_schema(names, max_lines=ctx.lines_wanted + 3)

        last_error: Exception | None = None
        for attempt in range(2):
            try:
                draft = SegmentDraft.model_validate(
                    await self._json_call(system=system, user=user, schema=schema)
                )
            except (ModelOutputError, ValidationError) as exc:
                last_error = exc
                log.warning("segment attempt %d unusable: %s", attempt + 1, exc)
                continue
            draft.lines = [
                Line(speaker=ln.speaker, text=ln.text.strip(), style=ln.style.strip()[:60])
                for ln in draft.lines
                if ln.speaker in names and ln.text.strip()
            ]
            if draft.lines:
                return draft
            last_error = ModelOutputError("segment had no usable lines")
        raise ModelOutputError(f"could not write a segment: {last_error}")

    # ---- images -------------------------------------------------------------

    async def describe_image(self, data: bytes, mime: str, name: str) -> str:
        """Turn a photo into prose the hosts can talk about. Only the text is kept."""
        interaction = await self.client.aio.interactions.create(
            model=self.settings.script_model,
            system_instruction=prompts.IMAGE_SYSTEM,
            input=[
                {"type": "text", "text": prompts.image_input(name)},
                {"type": "image", "data": base64.b64encode(data).decode("ascii"), "mime_type": mime},
            ],
            generation_config={"thinking_level": self.settings.script_thinking_level},
        )
        text = (interaction.output_text or "").strip()
        if not text:
            raise ModelOutputError("the photo description came back empty")
        return text[:4000]

    # ---- speech -------------------------------------------------------------

    async def stream_speech(self, lines: list[Line], hosts: list[Host]) -> AsyncIterator[bytes]:
        """Yield raw 24 kHz mono s16le PCM chunks as Gemini synthesizes them."""
        multi = len(hosts) > 1
        content = []
        for ln in lines:
            annotation: dict = {"type": "speech_metadata"}
            if multi:
                annotation["speaker"] = ln.speaker
            if ln.style:
                annotation["style"] = ln.style
            item: dict = {"type": "text", "text": ln.text}
            if len(annotation) > 1:
                item["annotations"] = [annotation]
            content.append(item)

        if multi:
            speech_config: dict | list = {
                "speakers": [{"speaker": h.name, "voice": h.voice} for h in hosts]
            }
        else:
            speech_config = [{"voice": hosts[0].voice}]

        stream = await self.client.aio.interactions.create(
            model=self.settings.tts_model,
            input=[{"type": "user_input", "content": content}],
            response_format={
                "type": "audio",
                "mime_type": "audio/l16",
                "sample_rate": self.settings.sample_rate,
            },
            generation_config={"speech_config": speech_config},
            stream=True,
        )
        try:
            async for event in stream:
                kind = getattr(event, "event_type", None)
                if kind == "step.delta":
                    delta = event.delta
                    if getattr(delta, "type", None) == "audio" and getattr(delta, "data", None):
                        yield base64.b64decode(delta.data)
                elif kind == "error":
                    detail = getattr(getattr(event, "error", None), "message", None) or "unknown"
                    raise ModelOutputError(f"speech stream error: {detail}")
        finally:
            await stream.close()


def _reconcile_hosts(req: ShowRequest, planned: list[Host]) -> list[Host]:
    """User choices win over the planner's; names and voices end up distinct, and
    every host's voice matches their gender.

    A voice the user picked is kept, and the host's gender follows it. Otherwise
    the gender comes from the user (a persona) or the planner, and a planner voice
    of the other gender is swapped for one that fits, with a similar character."""
    hosts: list[Host] = []
    for i, wanted in enumerate(req.hosts):
        base = planned[i] if i < len(planned) else Host(name=f"Host {i + 1}", persona="", voice="")
        name = (wanted.name.strip() or base.name.strip() or f"Host {i + 1}")[:40]
        taken = {h.voice for h in hosts}
        if wanted.voice in VOICES:
            voice, gender = wanted.voice, VOICE_GENDER[wanted.voice]
        else:
            gender = wanted.gender or (base.gender if base.gender in GENDERS else "") or VOICE_GENDER.get(base.voice, "")
            voice = base.voice
            if voice not in VOICES or VOICE_GENDER[voice] != gender or voice in taken:
                voice = pick_voice(gender or "female", avoid=taken, like=base.voice)
            gender = VOICE_GENDER[voice]
        # A persona the user wrote (or picked) is theirs; the planner's only fills gaps.
        persona = wanted.persona.strip() or base.persona
        hosts.append(Host(
            name=name, persona=persona, voice=voice, gender=gender,
            likes=wanted.likes.strip(), specialties=wanted.specialties.strip(), skills=wanted.skills.strip(),
        ))

    if len(hosts) == 2 and hosts[0].name.casefold() == hosts[1].name.casefold():
        hosts[1].name += " 2"
    return hosts


def _cast_cold_open(lines: list[Line], planned: list[str], hosts: list[Host]) -> list[Line]:
    """Speakers follow the hosts when the user's names replaced the planner's. If any
    line has a speaker nobody can voice, drop the cold open; the writers open instead."""
    names = {n.strip().casefold(): h.name for n, h in zip(planned, hosts)}
    names.update({h.name.casefold(): h.name for h in hosts})
    cast = []
    for ln in lines:
        speaker = names.get(ln.speaker.strip().casefold())
        if not speaker or not ln.text.strip():
            log.warning("dropping the cold open: %r isn't a host", ln.speaker)
            return []
        cast.append(Line(speaker=speaker, text=ln.text.strip(), style=ln.style))
    return cast


def _normalize_minutes(plan: ShowPlan, target: int) -> None:
    """Beat lengths drive pacing and the chapter bar, so they must add up to the target."""
    values = [b.minutes if b.minutes and b.minutes > 0 else 0.0 for b in plan.beats]
    known = [v for v in values if v]
    fill = sum(known) / len(known) if known else 1.0
    values = [v or fill for v in values]
    scale = target / sum(values)
    for beat, v in zip(plan.beats, values):
        beat.minutes = round(v * scale, 2)
