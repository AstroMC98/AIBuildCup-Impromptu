"""Every call to Gemini goes through this module.

The rest of the app only sees plain Python objects (ShowPlan, SegmentDraft,
PCM bytes), so swapping a model or provider is a change here and nowhere else.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator
from urllib.parse import urlsplit

from google import genai
from google.genai import types
from pydantic import ValidationError

from . import prompts
from .catalog import GENDERS, MUSIC_MOODS, TEMPLATES_BY_ID, VOICE_GENDER, VOICES, pick_voice
from .config import Settings
from .models import (
    INTERRUPT_KINDS, Beat, ContextItem, EditPlan, Finding, Host, Line, MemoBrief, Reaction, ResearchDossier,
    SegmentContext, SegmentDraft, ShowPlan, ShowRequest, WebSource,
)

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
    # Searching and answering in JSON in one call is a preview feature. If the model
    # refuses it, research_memo() searches, then writes the JSON in a second call.
    _search_with_json = True

    def __init__(self, settings: Settings):
        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is not set. Add it to .env and restart the server.")
        http_options = {"base_url": settings.gemini_base_url} if settings.gemini_base_url else None
        self.client = genai.Client(api_key=settings.gemini_api_key, http_options=http_options)
        self.settings = settings
        if settings.gemini_base_url and settings.gemini_base_url.startswith("http://"):
            # The SDK opens Live sessions over wss:// only; the local stub speaks ws://.
            api = self.client._api_client
            ws_url = "ws://" + settings.gemini_base_url.removeprefix("http://").rstrip("/")
            api._websocket_base_url = lambda: ws_url
            api._websocket_ssl_ctx = {}

    # ---- text ---------------------------------------------------------------

    async def _create(self, *, system: str, user: str | list, schema: dict | None = None,
                      tools: list | None = None, model: str | None = None, thinking: str | None = None):
        """One call to a text model. Returns the whole interaction, for callers that
        read its steps (what a grounded answer searched for and cites)."""
        extra: dict = {}
        if schema is not None:
            extra["response_format"] = {"type": "text", "mime_type": "application/json", "schema": schema}
        if tools:
            extra["tools"] = tools
        return await self.client.aio.interactions.create(
            model=model or self.settings.script_model,
            system_instruction=system,
            input=user,
            generation_config={"thinking_level": thinking or self.settings.script_thinking_level},
            **extra,
        )

    async def _json_call(self, *, system: str, user: str | list, schema: dict, tools: list | None = None,
                         model: str | None = None, thinking: str | None = None) -> dict:
        interaction = await self._create(system=system, user=user, schema=schema, tools=tools, model=model,
                                         thinking=thinking)
        return _parse_json(interaction.output_text)

    async def plan_show(self, req: ShowRequest, source: str) -> ShowPlan:
        data = await self._json_call(
            system=prompts.planner_system(req, source, self.settings.research_max_beats),
            user=prompts.planner_input(req),
            schema=prompts.plan_schema(len(req.hosts), research=req.research),
        )
        try:
            plan = ShowPlan.model_validate(data)
        except ValidationError as exc:
            raise ModelOutputError(f"plan did not match the schema: {exc}") from exc
        planned_names = [h.name for h in plan.hosts]
        plan.hosts = _reconcile_hosts(req, plan.hosts)
        plan.cold_open = _cast_cold_open(plan.cold_open, planned_names, plan.hosts)
        plan.reactions = _cast_reactions(plan.reactions, planned_names, plan.hosts)
        normalize_minutes(plan, req.minutes)
        return plan

    async def write_segment(
        self,
        ctx: SegmentContext,
        req: ShowRequest,
        plan: ShowPlan,
        source: str,
        style: str,
        context: list[ContextItem],
        thinking: str | None = None,
    ) -> SegmentDraft:
        names = [h.name for h in plan.hosts]
        system = prompts.writer_system(req, plan, source, style, context)
        user = prompts.writer_input(ctx, plan)
        schema = prompts.segment_schema(names, max_lines=ctx.lines_wanted + 3, research=ctx.research_on)

        last_error: Exception | None = None
        for attempt in range(2):
            try:
                draft = SegmentDraft.model_validate(
                    await self._json_call(system=system, user=user, schema=schema, thinking=thinking)
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

    # ---- interruptions -------------------------------------------------------

    async def replan(self, req: ShowRequest, plan: ShowPlan, source: str, style: str, transcript: list[Line],
                     minutes_left: float, steer: str, beat_index: int) -> list[Beat]:
        """New beats for the rest of the episode, going where the producer steered it."""
        data = await self._json_call(
            system=prompts.replan_system(req, plan, source, style, steer, minutes_left, beat_index),
            user=prompts.replan_input(transcript),
            schema=prompts.replan_schema(),
        )
        try:
            beats = [Beat.model_validate(b) for b in data.get("beats", [])]
        except ValidationError as exc:
            raise ModelOutputError(f"re-plan did not match the schema: {exc}") from exc
        beats = [b for b in beats if b.title.strip()]
        if not beats:
            raise ModelOutputError("the re-plan had no beats")
        return beats

    async def transcribe(self, data: bytes, mime: str) -> str:
        """The producer's spoken question as text."""
        interaction = await self.client.aio.interactions.create(
            model=self.settings.script_model,
            system_instruction=prompts.TRANSCRIBE_SYSTEM,
            input=[
                {"type": "text", "text": "Transcribe this."},
                {"type": "audio", "data": base64.b64encode(data).decode("ascii"), "mime_type": mime},
            ],
            generation_config={"thinking_level": "low"},
        )
        return (interaction.output_text or "").strip()[:1000]

    async def search_news(self, query: str, days: int, language: str = "English") -> tuple[list[dict], str]:
        """Recent stories for a briefing's search string, with Grounding with Google Search.
        Returns (items, the Search Suggestions HTML Google asks apps to show next to
        grounded results). Items: title, source, url, published, summary."""
        interaction = await self.client.aio.interactions.create(
            model=self.settings.script_model,
            system_instruction=prompts.NEWS_SEARCH_SYSTEM,
            input=prompts.news_search_input(query, days, language),
            tools=[{"type": "google_search"}],
            response_format={"type": "text", "mime_type": "application/json", "schema": prompts.news_schema()},
            generation_config={"thinking_level": "low"},
        )
        suggestions = ""
        for step in getattr(interaction, "steps", None) or []:
            if getattr(step, "type", "") == "google_search_result":
                for result in getattr(step, "result", None) or []:
                    suggestions = suggestions or (getattr(result, "search_suggestions", "") or "")
        items = []
        for item in _parse_json(interaction.output_text).get("items", [])[:5]:
            title, source = str(item.get("title", "")).strip(), str(item.get("source", "")).strip()
            if title and source:
                items.append({"title": title[:200], "source": source[:80], "url": str(item.get("url") or "")[:2000],
                              "published": str(item.get("published") or "")[:40],
                              "summary": str(item.get("summary") or "")[:700]})
        return items, suggestions
    # ---- research -----------------------------------------------------------

    async def research(self, question: str, *, show: str, asked_by: str = "", language: str = "English"
                       ) -> "Lookup":
        """A web lookup grounded with Google Search: a short brief for the hosts, the
        pages it rests on, and Google's Search Suggestions (shown with the result)."""
        interaction = await self.client.aio.interactions.create(
            model=self.settings.script_model,
            system_instruction=prompts.RESEARCH_SYSTEM.format(language=language),
            input=prompts.research_input(question, show=show, asked_by=asked_by),
            tools=GOOGLE_SEARCH,
            generation_config={"thinking_level": self.settings.script_thinking_level},
        )
        text = (interaction.output_text or "").strip()
        if not text:
            raise ModelOutputError("the lookup came back empty")
        queries, sources, suggestions = grounding(interaction.model_dump(mode="json"))
        return Lookup(text=text[:2000], sources=sources[:8], queries=queries[:6], suggestions=suggestions)

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

    # ---- voice memos ----------------------------------------------------------

    async def analyze_memo(self, audio: bytes, mime: str, *, creator: str, seconds: float | None = None) -> MemoBrief:
        """Listen to a voice memo (sent as recorded: Gemini takes WebM, Ogg, M4A and
        the rest) and write the brief for its episode."""
        data = await asyncio.to_thread(lambda: base64.b64encode(audio).decode("ascii"))  # up to ~19 MB
        max_queries = self.settings.max_research_queries
        result = await self._json_call(
            system=prompts.memo_system(max_queries),
            user=[{"type": "text", "text": prompts.memo_input(creator, seconds)},
                  {"type": "audio", "data": data, "mime_type": mime}],
            schema=prompts.memo_schema(max_queries),
            model=self.settings.memo_model or None,
        )
        try:
            brief = MemoBrief.model_validate(result)
        except ValidationError as exc:
            raise ModelOutputError(f"the memo brief did not match the schema: {exc}") from exc
        return _tidy_brief(brief, max_queries)

    async def research_memo(self, brief: MemoBrief, *, max_queries: int) -> ResearchDossier:
        """Live research on the memo, grounded with Google Search. Raises when the
        search fails; the memo's episode then goes ahead without it."""
        user = prompts.memo_research_input(brief)
        started = time.time()
        if self._search_with_json:
            try:
                interaction = await self._create(system=prompts.memo_research_system(max_queries), user=user,
                                                 schema=prompts.memo_research_schema(), tools=GOOGLE_SEARCH)
                return _dossier(_parse_json(interaction.output_text), interaction, started)
            except Exception as exc:
                if getattr(exc, "status_code", None) != 400:
                    raise
                log.warning("search with a JSON answer was refused (%s); searching, then writing JSON", exc)
                self._search_with_json = False
        interaction = await self._create(system=prompts.memo_research_system(max_queries, notes=True), user=user,
                                         tools=GOOGLE_SEARCH)
        notes = (interaction.output_text or "").strip()
        if not notes:
            raise ModelOutputError("the research came back empty")
        data = await self._json_call(system=prompts.MEMO_RESEARCH_CONDENSE, user=notes,
                                     schema=prompts.memo_research_schema())
        return _dossier(data, interaction, started)

    async def generate_music(self, prompt: str) -> tuple[bytes, str]:
        """A theme from Lyria (settings.music_model): its audio, and the audio's type."""
        interaction = await self.client.aio.interactions.create(model=self.settings.music_model, input=prompt)
        audio = interaction.output_audio
        if audio is None or not audio.data:
            raise ModelOutputError("no music came back")
        return await asyncio.to_thread(base64.b64decode, audio.data), audio.mime_type or "audio/mpeg"

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

    # ---- live co-hosting ----------------------------------------------------

    @asynccontextmanager
    async def cohost_link(self, *, system: str, voice: str, handle: str | None = None
                          ) -> AsyncIterator["CohostLink"]:
        """A Gemini Live session: it hears the user and answers in speech. `handle`
        resumes an earlier session (see ResumeHandle) with its whole conversation."""
        config = types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))),
            system_instruction=system,
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    silence_duration_ms=self.settings.cohost_silence_ms)),
            # Connections are recycled every few minutes (GoAway); a handle carries
            # the conversation over. Compression lifts the audio session's length cap.
            session_resumption=types.SessionResumptionConfig(handle=handle),
            context_window_compression=types.ContextWindowCompressionConfig(sliding_window=types.SlidingWindow()),
        )
        async with self.client.aio.live.connect(model=self.settings.cohost_model, config=config) as session:
            yield CohostLink(session)

    async def edit_cohost(self, turns: list[dict], you: str, host: str, minutes: float) -> EditPlan:
        """Title, show notes, chapters and what to cut, for a recorded co-host call."""
        data = await self._json_call(
            system=prompts.editor_system(you, host, minutes),
            user=prompts.editor_input(turns),
            schema=prompts.editor_schema(),
        )
        try:
            return EditPlan.model_validate(data)
        except ValidationError as exc:
            raise ModelOutputError(f"edit notes did not match the schema: {exc}") from exc


# ---- what a Live session sends back, as plain objects --------------------------------

@dataclass
class AudioOut:
    data: bytes          # 24 kHz mono s16le


@dataclass
class InputText:
    text: str            # the user's words, transcribed as they speak


@dataclass
class OutputText:
    text: str            # the co-host's words


@dataclass
class Interrupted:
    """The user started talking over the co-host; its turn stops here."""


@dataclass
class TurnDone:
    """The co-host finished its turn (all of its audio has been sent)."""


@dataclass
class GoAway:
    seconds: float | None    # this connection closes soon; reconnect with the handle


@dataclass
class ResumeHandle:
    handle: str


LiveEvent = AudioOut | InputText | OutputText | Interrupted | TurnDone | GoAway | ResumeHandle
MIC_MIME = "audio/pcm;rate=16000"


class CohostLink:
    def __init__(self, session) -> None:
        self._session = session

    async def send_audio(self, pcm: bytes) -> None:
        await self._session.send_realtime_input(audio=types.Blob(data=pcm, mime_type=MIC_MIME))

    async def send_text(self, text: str) -> None:
        await self._session.send_realtime_input(text=text)

    async def pause(self) -> None:
        """The mic went away (the phone dropped off): don't wait for more of it."""
        await self._session.send_realtime_input(audio_stream_end=True)

    async def events(self) -> AsyncIterator[LiveEvent]:
        """Until the connection closes. The SDK's receive() stops after each turn."""
        while True:
            got = False
            async for message in self._session.receive():
                got = True
                for event in _live_events(message):
                    yield event
            if not got:
                return


def _live_events(message: types.LiveServerMessage) -> list[LiveEvent]:
    out: list[LiveEvent] = []
    content = message.server_content
    if content:
        for part in (content.model_turn.parts or []) if content.model_turn else []:
            if part.inline_data and part.inline_data.data:
                out.append(AudioOut(part.inline_data.data))
        if content.input_transcription and content.input_transcription.text:
            out.append(InputText(content.input_transcription.text))
        if content.output_transcription and content.output_transcription.text:
            out.append(OutputText(content.output_transcription.text))
        if content.interrupted:
            out.append(Interrupted())
        if content.turn_complete:
            out.append(TurnDone())
    if message.go_away:
        match = re.match(r"([\d.]+)s", str(message.go_away.time_left or ""))
        out.append(GoAway(float(match.group(1)) if match else None))
    update = message.session_resumption_update
    if update and update.resumable and update.new_handle:
        out.append(ResumeHandle(update.new_handle))
    return out


# ---- research: what a grounded answer searched for and cites ---------------------------

GOOGLE_SEARCH = [{"type": "google_search"}]
# Grounded answers can cite redirect links on this host; the site is then in the title.
GROUNDING_REDIRECT_HOST = "vertexaisearch.cloud.google.com"


@dataclass
class Lookup:
    text: str
    sources: list[dict]        # {title, url, domain}
    queries: list[str]
    suggestions: list[str]     # Google's Search Suggestions (HTML), at most 5


def grounding(interaction: dict) -> tuple[list[str], list[dict], list[str]]:
    """From a grounded interaction (as JSON): what was searched, the web pages the
    answer cites, and Google's Search Suggestions (at most 5, as the terms allow)."""
    queries: list[str] = []
    sources: dict[str, dict] = {}
    suggestions: list[str] = []
    for step in interaction.get("steps") or []:
        kind = step.get("type")
        if kind == "google_search_call":
            queries += (step.get("arguments") or {}).get("queries") or []
        elif kind == "google_search_result":
            suggestions += [r["search_suggestions"] for r in step.get("result") or [] if r.get("search_suggestions")]
        elif kind == "model_output":
            for item in step.get("content") or []:
                for note in item.get("annotations") or []:
                    url = note.get("url") or ""
                    if note.get("type") == "url_citation" and url not in sources and _web_page(url):
                        sources[url] = _web_source(url, note.get("title") or "")
    return list(dict.fromkeys(q.strip() for q in queries if q.strip())), list(sources.values()), suggestions[:5]


def _web_page(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme in {"http", "https"} and bool(parts.hostname)


def _web_source(url: str, title: str) -> dict:
    title = title.strip()
    domain = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    if domain.endswith(GROUNDING_REDIRECT_HOST) and "." in title and " " not in title:
        domain = title.lower().removeprefix("www.")
    return {"title": (title or domain)[:200], "url": url, "domain": domain}


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


def _cast_reactions(reactions: list[Reaction], planned: list[str], hosts: list[Host]) -> list[Reaction]:
    """Like the cold open, but a reaction with an unknown speaker is just dropped."""
    names = {n.strip().casefold(): h.name for n, h in zip(planned, hosts)}
    names.update({h.name.casefold(): h.name for h in hosts})
    cast = []
    for r in reactions:
        speaker = names.get(r.speaker.strip().casefold())
        text = r.text.strip()
        if speaker and text and r.kind in INTERRUPT_KINDS and len(text) <= 80:
            cast.append(Reaction(speaker=speaker, kind=r.kind, text=text))
    return cast[:8]


def _tidy_brief(brief: MemoBrief, max_queries: int) -> MemoBrief:
    """Keep a memo brief inside the catalog and the limits, whatever came back."""
    if brief.usable and not brief.transcript.strip():
        brief.usable, brief.reason = False, brief.reason or "We couldn't make out what was said in that memo."
    if brief.music_mood not in MUSIC_MOODS:
        brief.music_mood = MemoBrief.model_fields["music_mood"].default
    if brief.format not in TEMPLATES_BY_ID:
        brief.format = MemoBrief.model_fields["format"].default
    queries = (q.strip()[:200] for q in brief.research_queries if q.strip())
    brief.research_queries = list(dict.fromkeys(queries))[:max_queries]
    brief.working_title = brief.working_title.strip()[:70]
    brief.language = brief.language.strip()[:60] or "English"
    return brief


def _dossier(data: dict, interaction, searched_at: float) -> ResearchDossier:
    """The research as the model wrote it, plus what the grounded call itself shows."""
    queries, sources, suggestions = grounding(interaction.model_dump(mode="json"))
    findings: list[Finding] = []
    for f in data.get("findings") or []:
        try:
            finding = Finding.model_validate({**f, "id": len(findings) + 1})
        except (TypeError, ValidationError):
            continue
        if finding.claim.strip() and finding.outlet.strip():
            findings.append(finding)

    def texts(key: str) -> list[str]:
        return [str(x).strip() for x in data.get(key) or [] if str(x).strip()][:6]
    return ResearchDossier(
        ok=bool(findings), summary=str(data.get("summary") or "").strip(), findings=findings[:12],
        disagreements=texts("disagreements"), unanswered=texts("unanswered"),
        queries=queries, sources=[WebSource(**s) for s in sources], search_suggestions=suggestions,
        searched_at=searched_at,
    )


def normalize_minutes(plan: ShowPlan, target: int) -> None:
    """Beat lengths drive pacing and the chapter bar, so they must add up to the target."""
    values = [b.minutes if b.minutes and b.minutes > 0 else 0.0 for b in plan.beats]
    known = [v for v in values if v]
    fill = sum(known) / len(known) if known else 1.0
    values = [v or fill for v in values]
    scale = target / sum(values)
    for beat, v in zip(plan.beats, values):
        beat.minutes = round(v * scale, 2)
