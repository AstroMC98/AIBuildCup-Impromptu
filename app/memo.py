"""Voice-memo pods: a rough recording goes in, a finished episode comes out.

    POST /api/memos -> prepare: transcribing -> researching -> outlining
                         review on: stop at status "review"; POST .../produce carries on
    produce, a few at a time (Renders, first come first served):
                       queued -> voicing -> scoring -> mixing -> saving -> finished

A memo pod is a show document with kind "memo". While it's being made its status
is "rendering" (or "review"), so only its owner sees it (accounts.published).
Each stage's result is stored as it goes: the recording (files/memo.*), the brief
and research on the document, the outline (plan_draft) and source.txt. A pod
waiting for review survives a restart, and Try again picks up from the outline.

Voicing is the live show loop with nobody listening (app/render.py); the music
and the shifting of every timestamp it causes are app/mixer.py's job.

Credits: a one-shot memo is charged when it's uploaded, a reviewed one at Produce
(main.py). When it's made, settle() keeps what was used, worked out from the
ledger so running it twice never refunds twice: the minutes the episode runs,
plus the music credits if original music was made and used. A failure gives
everything back. A restart cancels renders without settling; sweep() at the
next start settles them and marks them failed.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import secrets
import time
from typing import Awaitable, Callable, TypeVar

from . import prompts
from .accounts import Accounts
from .catalog import PRESETS_BY_ID, TEMPLATES_BY_ID, accent_for, style_label, template_request
from .config import Settings
from .llm import Gemini, normalize_minutes
from .mixer import Cues, MixResult, encode_mp3, mix
from .models import Beat, MemoBrief, MemoOptions, ProduceMemo, ResearchDossier, ShowPlan, ShowRequest
from .music import MINE, MOOD_MUSIC, Library, cues_from_theme, decode, music_prompt, theme_for
from .render import RenderRoom
from .room import beats_with_times
from .show import PLAN_TIMEOUT_SECONDS, ShowRunner
from .store import Persister, Store

log = logging.getLogger("podcasts.memo")
T = TypeVar("T")

DEFAULT_FORMAT = "idea-breakdown"
ANALYZE_TIMEOUT_SECONDS = 150.0   # a few minutes of audio, listened to in full
SAVE_TIMEOUT_SECONDS = 120.0
RETRY_DELAYS = (2.0, 5.0)
MIN_VOICED_SHARE = 0.5            # less than this much of the episode voiced: it failed
# Where each stage starts on the progress bar; voicing fills most of it.
STAGE_PROGRESS = {
    "queued": 0.0, "transcribing": 0.03, "researching": 0.08, "outlining": 0.12, "review": 0.15,
    "voicing": 0.15, "scoring": 0.86, "mixing": 0.9, "saving": 0.95, "done": 1.0, "failed": 0.0,
}
VOICING_SHARE = STAGE_PROGRESS["scoring"] - STAGE_PROGRESS["voicing"]
RESTART_MESSAGE = "The server restarted while this pod was being made. Your credits are back; tap Try again."
TRY_AGAIN = "Your credits are back; tap Try again."


class MemoFailed(Exception):
    """A memo pod can't be made. The message is for its owner."""


# ---- what a memo pod is made from ---------------------------------------------------

def voice_minutes(minutes: int, music_on: bool) -> int:
    """How long the hosts talk. The theme and stings add about half a minute, so
    with music the talking aims a minute short and the episode lands near `minutes`."""
    return max(2, minutes - 1) if music_on else minutes


def credit_name(credit_as: str, user_name: str) -> str:
    """Who the hosts credit with the memo: the name asked for, else a first name."""
    if credit_as.strip():
        return credit_as.strip()
    return user_name.split()[0] if user_name.strip() else "the creator"


def fresh_render(review: bool) -> dict:
    now = time.time()
    return {"stage": "queued", "progress": 0.0, "detail": "", "error": None, "review": review,
            "timings": {}, "started_at": now, "updated_at": now}


def new_doc(show_id: str, *, uid: str, owner_name: str, opts: MemoOptions, memo: dict, intro: dict,
            shared_with: list[str], album_ids: list[str], tags: list[str]) -> dict:
    """A memo pod's show document, before anything has been made from the memo.
    The same fields as main.create_show's; the outline fills in the rest."""
    now = time.time()
    return {
        "id": show_id, "kind": "memo", "owner_uid": uid, "owner_name": owner_name,
        "visibility": opts.visibility, "shared_with": shared_with,
        "template": opts.template if opts.template in TEMPLATES_BY_ID else None,
        "album_ids": album_ids, "tags": tags, "status": "rendering", "ended_reason": None,
        "created_at": now, "started_at": now, "ended_at": None,
        "request": {}, "source": {"chars": 0, "names": [f"Voice memo by {memo['credit']}"]},
        "title": "Voice memo", "tagline": "", "about": "",
        "accent": opts.accent or accent_for(opts.vibe, opts.style), "vibe": opts.vibe, "style": opts.style,
        "style_label": opts.style_label or style_label(opts.vibe), "style_history": [],
        "hosts": [], "beats": [], "minutes": opts.minutes, "language": opts.language or "English",
        "credits_charged": 0, "music_credits_charged": 0, "samples": 0, "duration_s": 0, "audio": None,
        "counts": {}, "memo": memo, "memo_options": opts.model_dump(), "intro": intro,
        "render": fresh_render(opts.review),
    }


def build_request(brief: MemoBrief, opts: MemoOptions, source: str, *, credit: str, intro: dict | None) -> ShowRequest:
    """The episode's request. Its format: the template the owner picked, else their
    vibe or style with the memo's suggested format's hosts, else what the memo suggests."""
    fmt = template_request(opts.template or "") or template_request(brief.format) or template_request(DEFAULT_FORMAT)
    style = {k: fmt[k] for k in ("style", "style_label", "accent", "vibe")}
    preset = PRESETS_BY_ID.get(opts.vibe or "")
    if not opts.template and preset:
        style = {"style": preset["prompt"], "style_label": None, "accent": preset["accent"], "vibe": preset["id"]}
    elif not opts.template and opts.style.strip():
        style = {"style": opts.style.strip(), "style_label": opts.style_label, "accent": opts.accent, "vibe": None}
    intro = intro or {}
    return ShowRequest(
        source=source, hosts=[h.model_dump() for h in opts.hosts] or fmt["hosts"], minutes=opts.minutes,
        language=opts.language.strip() or brief.language, source_names=[f"Voice memo by {credit}"],
        mode="recorded", direction=prompts.memo_direction(credit),
        show_name=intro.get("show_name", ""), intro_line=intro.get("intro_line", ""), **style,
    )


def apply_edits(plan: ShowPlan, edits: ProduceMemo) -> ShowPlan:
    """The owner's changes to the outline. A beat keeps its planned length when its
    title survives; new ones get a typical beat's. If the beats change at all, the
    cold open (which previews them) goes, and the writers open the show instead."""
    plan = plan.model_copy(deep=True)
    if edits.title and edits.title.strip():
        plan.title = edits.title.strip()
    if edits.beats is not None:
        planned = {b.title.strip().casefold(): b.minutes for b in plan.beats}
        typical = sum(planned.values()) / len(planned) if planned else 1.0
        beats = [Beat(title=e.title.strip(), notes=e.notes.strip(), minutes=planned.get(e.title.strip().casefold(), typical))
                 for e in edits.beats]
        if [(b.title, b.notes) for b in beats] != [(b.title, b.notes) for b in plan.beats]:
            plan.cold_open = []
        plan.beats = beats
    return plan


# ---- credits ---------------------------------------------------------------------------

async def settle(store: Store, accounts: Accounts, uid: str, show_id: str, *, keep: int, note: str) -> int:
    """Give back whatever this pod was charged beyond `keep`. Worked out from the
    ledger, so a second call (a retry, a sweep after a crash) refunds nothing more."""
    spent = -sum(int(e.get("amount") or 0) for e in await store.ledger_for_show(uid, show_id))
    refund = max(0, spent - keep)
    if refund:
        await accounts.adjust_credits(uid, refund, reason="refund", show_id=show_id, note=note)
    return refund


async def sweep(store: Store, accounts: Accounts) -> int:
    """At startup: memo pods that were being made when the server stopped. Their
    renders are gone, so they're marked failed and their credits go back. Pods
    waiting for review keep waiting."""
    count = 0
    for doc in await store.shows_with_status("rendering"):
        if doc.get("kind") != "memo":
            continue
        try:
            await settle(store, accounts, doc["owner_uid"], doc["id"], keep=0, note="The server restarted")
        except Exception:
            log.exception("could not refund memo pod %s after a restart", doc["id"])
        render = {**(doc.get("render") or {}), "stage": "failed", "error": RESTART_MESSAGE, "updated_at": time.time()}
        await store.update_show(doc["id"], {"status": "failed", "ended_reason": "restart", "render": render})
        count += 1
    return count


# ---- the job ---------------------------------------------------------------------------

class MemoJob:
    def __init__(self, doc: dict, *, store: Store, accounts: Accounts, gemini: Gemini, settings: Settings,
                 library: Library, renders: Renders):
        self.id: str = doc["id"]
        self.uid: str = doc["owner_uid"]
        self.doc = doc
        self.store = store
        self.accounts = accounts
        self.gemini = gemini
        self.settings = settings
        self.library = library
        self.renders = renders
        self.rate = settings.sample_rate
        self.persist: Persister | None = None
        self.task: asyncio.Task | None = None
        self._stage_since = time.monotonic()

    @property
    def options(self) -> MemoOptions:
        return MemoOptions.model_validate(self.doc["memo_options"])

    @property
    def credit(self) -> str:
        return self.doc["memo"].get("credit") or "the creator"

    @property
    def mood(self) -> str:
        return ((self.doc["memo"].get("brief") or {}).get("music_mood")) or "warm"

    def start(self, phase: str, edits: ProduceMemo | None = None) -> None:
        """phase: "prepare" (from the recording) or "produce" (from the outline)."""
        self.renders.add(self)
        self.task = asyncio.create_task(self._run(phase, edits))

    def waiting(self, ahead: int) -> None:
        """In the queue for a render slot, behind `ahead` others."""
        self._update(stage="queued", detail="Next in line" if not ahead else f"{ahead} ahead of you")

    async def cancel(self) -> None:
        """Stop, keeping the document as it is (deleting or shutting down: the caller settles)."""
        if self.task and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task

    async def _run(self, phase: str, edits: ProduceMemo | None) -> None:
        self.persist = Persister(self.store, self.id)
        try:
            if phase == "prepare":
                await self._prepare()
                if self.options.review:
                    self._update({"status": "review"}, stage="review", detail="Ready for you to check")
                    return
            await self._produce(edits)
        except asyncio.CancelledError:
            raise
        except MemoFailed as exc:
            await self._fail(str(exc))
        except Exception:
            log.exception("memo pod %s failed", self.id)
            await self._fail(f"Something went wrong while making this pod. {TRY_AGAIN}")
        finally:
            await self.persist.close()
            self.renders.remove(self.id)

    def _update(self, fields: dict | None = None, *, stage: str | None = None, progress: float | None = None,
                detail: str | None = None, error: str | None = None) -> None:
        """Change the document here (polled by the owner) and in the store (coalesced)."""
        fields = dict(fields or {})
        if stage or progress is not None or detail is not None or error is not None:
            render = dict(self.doc.get("render") or {})
            if stage and stage != render.get("stage"):
                timings = dict(render.get("timings") or {})
                if render.get("stage"):
                    spent = time.monotonic() - self._stage_since
                    timings[render["stage"]] = round(timings.get(render["stage"], 0) + spent, 1)
                self._stage_since = time.monotonic()
                render.update(stage=stage, timings=timings, progress=STAGE_PROGRESS[stage], detail="")
            if progress is not None:
                render["progress"] = round(progress, 3)
            if detail is not None:
                render["detail"] = detail
            if error is not None:
                render["error"] = error
            render["updated_at"] = time.time()
            fields["render"] = render
        self.doc.update(fields)
        if self.persist:
            self.persist.show(**fields)

    async def _retry(self, call: Callable[[], Awaitable[T]], timeout: float, what: str) -> T:
        for attempt, delay in enumerate((*RETRY_DELAYS, None)):
            try:
                return await asyncio.wait_for(call(), timeout)
            except (asyncio.CancelledError, MemoFailed):
                raise
            except Exception as exc:
                if delay is None:
                    log.exception("memo pod %s: couldn't %s", self.id, what)
                    raise MemoFailed(f"We couldn't {what} just now. {TRY_AGAIN}") from exc
                log.warning("memo pod %s: couldn't %s (attempt %d): %s", self.id, what, attempt + 1, exc)
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    # ---- prepare: the memo, the research, the outline ------------------------------------------

    async def _prepare(self) -> None:
        s = self.settings
        memo = self.doc["memo"]
        self._update(stage="transcribing", detail="Listening to your memo")
        audio = await self.store.get_show_file(self.id, memo["file"])
        if not audio:
            raise MemoFailed("The recording is missing. Please upload it again.")
        brief = await self._retry(
            lambda: self.gemini.analyze_memo(audio, memo["mime"], creator=self.credit, seconds=memo.get("duration_s")),
            ANALYZE_TIMEOUT_SECONDS, "listen to your memo",
        )
        if not brief.usable:
            raise MemoFailed(brief.reason or "We couldn't find a story to tell in that memo. Try recording it again.")
        self._update({"memo": {**memo, "brief": brief.model_dump()}, "title": brief.working_title or "Voice memo"})

        self._update(stage="researching", detail="Looking things up")
        try:
            dossier = await asyncio.wait_for(self.gemini.research(brief, max_queries=s.max_research_queries),
                                             s.research_timeout_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("memo pod %s: research failed, carrying on without it: %s", self.id, exc)
            dossier = ResearchDossier(ok=False, searched_at=time.time())
        found = (f"{len(dossier.findings)} findings from {len(dossier.sources)} sources" if dossier.ok
                 else "The research didn't come back; the hosts will stick to your memo")
        self._update({"research": dossier.model_dump()}, detail=found)

        self._update(stage="outlining", detail="Planning the episode")
        source = prompts.memo_source(brief, dossier, self.credit)
        req = build_request(brief, self.options, source, credit=self.credit, intro=self.doc.get("intro"))
        plan = await self._retry(lambda: self.gemini.plan_show(req, source), PLAN_TIMEOUT_SECONDS, "plan the episode")
        await self.store.put_text(self.id, "source.txt", source)
        self._update(self._outline_fields(req, plan, source, template=self.options.template or brief.format))

    def _outline_fields(self, req: ShowRequest, plan: ShowPlan, source: str, template: str | None = None) -> dict:
        fields = {
            "request": req.model_dump(exclude={"source"}), "plan_draft": plan.model_dump(),
            "source": {"chars": len(source), "names": req.source_names},
            "title": plan.title, "tagline": plan.tagline, "about": plan.about,
            "hosts": [h.model_dump(include={"name", "persona", "voice", "gender"}) for h in plan.hosts],
            "beats": [b.model_dump() for b in plan.beats], "minutes": req.minutes, "language": req.language,
            "accent": req.accent or accent_for(req.vibe, req.style), "vibe": req.vibe, "style": req.style,
            "style_label": req.style_label or style_label(req.vibe),
        }
        if template:
            fields["template"] = template if template in TEMPLATES_BY_ID else None
        return fields

    # ---- produce: voice, score, mix, save -------------------------------------------------

    async def _outline(self, edits: ProduceMemo | None) -> tuple[ShowRequest, ShowPlan, str]:
        """The stored outline, with the owner's edits applied and saved."""
        source = await self.store.get_text(self.id, "source.txt")
        if not source or not self.doc.get("plan_draft"):
            raise MemoFailed("This pod's outline is missing. Please send your memo again.")
        req = ShowRequest.model_validate({**self.doc["request"], "source": source, "minutes": self.options.minutes})
        plan = ShowPlan.model_validate(self.doc["plan_draft"])
        if edits:
            fields: dict = {}
            if edits.excluded_findings is not None:
                excluded = sorted(set(edits.excluded_findings))
                brief = MemoBrief.model_validate(self.doc["memo"]["brief"])
                dossier = ResearchDossier.model_validate(self.doc.get("research") or {})
                source = prompts.memo_source(brief, dossier, self.credit, excluded=set(excluded))
                await self.store.put_text(self.id, "source.txt", source)
                req = req.model_copy(update={"source": source})
                fields["research_excluded"] = excluded
            plan = apply_edits(plan, edits)
            fields.update(self._outline_fields(req, plan, source))
            self._update(fields)
        return req, plan, source

    def _music_choice(self) -> str:
        """What the owner asked for; "auto" means their show intro's theme, if they set one."""
        choice = self.options.music
        return self._show_theme() if choice == "auto" else choice

    def _show_theme(self) -> str:
        saved = (self.doc.get("intro") or {}).get("music") or "auto"
        return saved if saved != "generate" else "auto"

    async def _produce(self, edits: ProduceMemo | None) -> None:
        s = self.settings
        req, plan, source = await self._outline(edits)
        choice = self._music_choice()
        generate = choice == "generate" and bool(s.music_model)
        music_on = choice != "none" and (generate or self.library.available or choice.startswith(MINE))
        talk = voice_minutes(req.minutes, music_on)
        req = req.model_copy(update={"minutes": talk})
        plan = plan.model_copy(deep=True)
        normalize_minutes(plan, talk)

        async with self.renders.slot(self):
            started = time.time()
            self._update({"status": "rendering"}, stage="voicing", detail="The hosts are recording")
            music = asyncio.create_task(self._generate_music()) if generate else None
            try:
                room = await self._voice(req, plan, source)
                self._update(stage="scoring", detail="Scoring the music")
                cues, music_info, generated = await self._score(choice, music)
            finally:
                if music and not music.done():
                    music.cancel()

            self._update(stage="mixing", detail="Mixing")
            try:
                mixed = await asyncio.to_thread(mix, room.voice, dict(room.chapters), cues, self.rate)
            except Exception:
                log.exception("memo pod %s: mixing failed; saving the voice on its own", self.id)
                mixed, music_info, generated = MixResult(pcm=room.voice), {"source": "none"}, False

            self._update(stage="saving", detail="Saving the episode")
            await self._save(room, plan, mixed, music_info, generated, started)

    async def _voice(self, req: ShowRequest, plan: ShowPlan, source: str) -> RenderRoom:
        s = self.settings
        target_s = req.minutes * 60

        def voiced(seconds: float) -> None:
            done = min(1.0, seconds / target_s)
            self._update(progress=STAGE_PROGRESS["voicing"] + VOICING_SHARE * done,
                         detail=f"{_clock(seconds)} of about {req.minutes}:00 recorded")

        room = RenderRoom(self.rate, on_voiced=voiced)
        render_settings = s.model_copy(update={"segment_seconds": s.render_segment_seconds,
                                               "lines_per_segment": s.render_lines_per_segment})
        runner = ShowRunner(req, source, self.gemini, render_settings, room, plan=plan,
                            write_timeout=s.render_write_timeout_seconds, speech_retries=1)
        try:
            reason = await asyncio.wait_for(runner.run(), s.render_max_minutes * 60)
        except TimeoutError as exc:
            raise MemoFailed(f"Making this episode took too long. {TRY_AGAIN}") from exc
        if reason == "error" or room.voiced_s < MIN_VOICED_SHARE * target_s:
            log.warning("memo pod %s: voicing ended %s with %.0fs of %ds (%s)", self.id, reason, room.voiced_s,
                        target_s, room.fatal or "")
            raise MemoFailed(f"The hosts couldn't finish recording this episode. {TRY_AGAIN}")
        return room

    async def _generate_music(self) -> tuple[bytes, str]:
        return await asyncio.wait_for(self.gemini.generate_music(music_prompt(self.mood)),
                                      self.settings.music_timeout_seconds)

    async def _score(self, choice: str, music: asyncio.Task | None) -> tuple[Cues | None, dict, bool]:
        """(cues, a note on the music, whether original music was made and used)."""
        if music is not None:
            try:
                data, mime = await music
                track = await self._keep_track(data, mime)
                cues = await asyncio.to_thread(lambda: cues_from_theme(decode(data, self.rate), self.rate))
                return cues, {"source": "generated", "track_id": track["id"], "label": track["label"]}, True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("memo pod %s: original music failed, using the library: %s", self.id, exc)
                self._update(detail="Original music didn't work out; using the studio library instead")
                choice = self._show_theme()
        cues, info = await theme_for(choice, library=self.library, store=self.store, uid=self.uid,
                                     mood=self.mood, seed=self.id, rate=self.rate)
        return cues, info, False

    async def _keep_track(self, data: bytes, mime: str) -> dict:
        """Original music is the owner's to keep: saved with their things, so it can
        be their show's theme or go on another pod without being made again."""
        track_id = secrets.token_hex(6)
        name = f"music-{track_id}.{'wav' if 'wav' in mime else 'mp3'}"
        await self.store.put_user_file(self.uid, name, data, mime)
        item = {
            "id": track_id, "label": f"{MOOD_MUSIC.get(self.mood, ('Original',))[0]} theme, {time.strftime('%b %d')}",
            "mood": self.mood, "file": name, "mime": mime, "origin": self.settings.music_model,
            "show_id": self.id, "created_at": time.time(),
        }
        await self.store.put_item(self.uid, "music", item)
        return item

    async def _save(self, room: RenderRoom, plan: ShowPlan, mixed: MixResult, music: dict, generated: bool,
                    started: float) -> None:
        s = self.settings
        mp3 = await asyncio.to_thread(encode_mp3, mixed.pcm, self.rate, s.render_mp3_kbps)
        audio = await self._retry(lambda: self.store.put_audio(self.id, mp3), SAVE_TIMEOUT_SECONDS, "save the episode")
        segments = room.segment_list()
        for seg in segments:
            await self.store.put_segment(self.id, {**seg, "start": mixed.shift(seg["start"])})
        planned = (room.plan or {}).get("beats") or [b.model_dump() for b in plan.beats]
        beats = beats_with_times(planned, {i: mixed.chapter_start(i, st) for i, st in room.chapters.items()},
                                 mixed.samples)
        duration = round(mixed.samples / self.rate, 2)
        keep = math.ceil(duration / 60 * s.credits_per_minute) + (s.music_credits if generated else 0)
        missed_music = self.options.music == "generate" and not generated
        note = "Unused minutes, and original music that didn't work out" if missed_music else "Unused minutes"
        refund = await settle(self.store, self.accounts, self.uid, self.id, keep=keep, note=note)
        now = time.time()
        self._update({
            "status": "finished", "ended_reason": "signed_off", "started_at": started, "ended_at": now,
            "created_at": now,       # it comes out now: new in followers' feeds
            "samples": mixed.samples, "duration_s": duration, "audio": audio, "beats": beats,
            "counts": {"segments": len(segments), "comments": 0, "context": 0, "hearts": 0, "peak_listeners": 0},
            "listener_timeline": [], "credits_refunded": refund,
            "music": {**music, "regions": mixed.regions},
            "show_notes": self._show_notes(music),
        }, stage="done", detail="", error="")
        log.info("memo pod %s made: %.0fs, %d segments, music %s", self.id, duration, len(segments), music["source"])

    def _show_notes(self, music: dict) -> dict:
        label = music.get("label") or ""
        about_music = {
            "generated": "Original theme music made for this episode with Lyria (it carries a SynthID watermark).",
            "library": f"Theme music: {label}, from the studio library.",
            "mine": f"Theme music: {label}, made with Lyria.",
        }.get(music.get("source"), "")
        return {
            "summary": self.doc.get("about", ""),
            "credit": f"Made from a voice memo by {self.credit}.",
            "sources": ((self.doc.get("research") or {}).get("sources") or [])[:12],
            "music": about_music,
        }

    async def _fail(self, message: str) -> None:
        try:
            refund = await settle(self.store, self.accounts, self.uid, self.id, keep=0, note="Pod couldn't be made")
        except Exception:
            log.exception("memo pod %s: could not refund", self.id)
            refund = 0
        self._update({"status": "failed", "ended_reason": "error", "ended_at": time.time(), "credits_refunded": refund},
                     stage="failed", error=message)


def _clock(seconds: float) -> str:
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


# ---- the queue ---------------------------------------------------------------------------

class Renders:
    """Memo pods being made in this process. Like the live rooms they're held in
    memory, so the service runs one instance. At most `slots` render at once (they
    share the Gemini quota with live pods); the rest wait their turn."""

    def __init__(self, slots: int):
        self._jobs: dict[str, MemoJob] = {}
        self._slots = asyncio.Semaphore(max(1, slots))
        self._waiting: list[MemoJob] = []

    def get(self, show_id: str) -> MemoJob | None:
        return self._jobs.get(show_id)

    def add(self, job: MemoJob) -> None:
        self._jobs[job.id] = job

    def remove(self, show_id: str) -> None:
        self._jobs.pop(show_id, None)

    def __len__(self) -> int:
        return len(self._jobs)

    def active_for(self, uid: str) -> MemoJob | None:
        return next((j for j in self._jobs.values() if j.uid == uid), None)

    @contextlib.asynccontextmanager
    async def slot(self, job: MemoJob):
        """Wait for a turn to render (first come, first served), telling the owner where they are."""
        self._waiting.append(job)
        self._tell_waiting()
        try:
            async with self._slots:
                self._waiting.remove(job)
                self._tell_waiting()
                yield
        finally:
            if job in self._waiting:
                self._waiting.remove(job)
                self._tell_waiting()

    def _tell_waiting(self) -> None:
        for ahead, job in enumerate(self._waiting):
            job.waiting(ahead)

    async def shutdown(self, timeout: float) -> None:
        """On SIGTERM: stop every render. Nothing is settled here; the next start's sweep does that."""
        jobs = list(self._jobs.values())
        if jobs:
            await asyncio.wait([asyncio.create_task(j.cancel()) for j in jobs], timeout=timeout)
