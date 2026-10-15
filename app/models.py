"""Data shapes passed between the API, the show loop and the model layer."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


# ---- What the browser sends to start a show ---------------------------------

Gender = Literal["", "female", "male"]   # "" = let the planner decide
TRAIT_CHARS = 300                        # likes, specialties, skills
NOTES_CHARS = 20_000                     # a persona's own private context


class HostRequest(BaseModel):
    name: str = ""          # blank = let the planner name the host
    voice: str = "auto"     # "auto" = let the planner pick from the catalog
    persona: str = Field(default="", max_length=600)  # blank = let the planner write one
    gender: Gender = ""
    likes: str = Field(default="", max_length=TRAIT_CHARS)
    specialties: str = Field(default="", max_length=TRAIT_CHARS)
    skills: str = Field(default="", max_length=TRAIT_CHARS)
    # What only this host knows: their own notes and background. It reaches the
    # writers, never listeners (see main._public_request).
    context: str = Field(default="", max_length=NOTES_CHARS)


class ShowRequest(BaseModel):
    source: str = Field(min_length=1)
    style: str = Field(min_length=1, max_length=2_000)
    vibe: str | None = None  # a STYLE_PRESETS id, or None for a custom style
    # Set when the style came from a template or the user's saved styles.
    style_label: str | None = Field(default=None, max_length=40)
    accent: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    hosts: list[HostRequest] = Field(min_length=1, max_length=2)
    minutes: int = Field(default=10, ge=2, le=15)  # 15 minutes is the longest pod
    language: str = Field(default="English", max_length=60)
    # Display names of the files and links the source was built from.
    source_names: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("language")
    @classmethod
    def _default_language(cls, v: str) -> str:
        return v.strip() or "English"


Visibility = Literal["private", "connections", "public"]


class NewShow(BaseModel):
    request: ShowRequest
    live: bool = True        # False saves a draft to start later
    visibility: Visibility = "private"
    shared_with: list[str] = Field(default_factory=list, max_length=200)  # connection uids
    template: str | None = None
    album_ids: list[str] = Field(default_factory=list, max_length=20)
    tags: list[str] = Field(default_factory=list, max_length=20)


class ShareSettings(BaseModel):
    visibility: Visibility
    shared_with: list[str] = Field(default_factory=list, max_length=200)


class Organize(BaseModel):
    """Which of your albums a pod sits in, and its tags. Omitted = unchanged."""
    album_ids: list[str] | None = Field(default=None, max_length=20)
    tags: list[str] | None = Field(default=None, max_length=20)


# ---- What a user keeps for reuse --------------------------------------------

class PersonaIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    persona: str = Field(min_length=1, max_length=600)
    voice: str = "auto"
    gender: Gender = ""
    likes: str = Field(default="", max_length=TRAIT_CHARS)
    specialties: str = Field(default="", max_length=TRAIT_CHARS)
    skills: str = Field(default="", max_length=TRAIT_CHARS)
    context: str = Field(default="", max_length=NOTES_CHARS)


class AlbumIn(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=1000)
    visibility: Visibility = "private"
    accent: str = Field(default="#6A4FB0", pattern=r"^#[0-9A-Fa-f]{6}$")


class StyleIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    prompt: str = Field(min_length=1, max_length=2_000)
    accent: str = Field(default="#2F6BD0", pattern=r"^#[0-9A-Fa-f]{6}$")


# ---- What the planner returns -----------------------------------------------

class Host(BaseModel):
    name: str
    persona: str
    voice: str
    gender: str = ""
    likes: str = ""
    specialties: str = ""
    skills: str = ""


class Beat(BaseModel):
    title: str
    notes: str
    minutes: float = 0.0     # planned length; drives pacing and the chapter bar


class ShowPlan(BaseModel):
    title: str
    tagline: str
    about: str = ""
    hosts: list[Host]
    beats: list[Beat]
    # The show's first lines, written with the plan so audio can start without
    # waiting on a writer call. Empty means the writers open the show.
    cold_open: list["Line"] = []


# ---- What the writer returns for each chunk of dialogue ---------------------

class Line(BaseModel):
    speaker: str
    text: str
    style: str = ""


class SegmentDraft(BaseModel):
    lines: list[Line]
    addressed_comment_ids: list[int] = []
    used_context_ids: list[int] = []
    declined_comment_ids: list[int] = []
    beat_complete: bool = False


# ---- Material handed to the hosts mid-show ----------------------------------

ContextKind = Literal["text", "file", "link", "photo"]
ContextWhen = Literal["now", "next", "background"]


class ContextItem(BaseModel):
    """Something the producer or a listener handed the hosts while the show is live."""
    id: int = 0
    kind: ContextKind
    name: str
    text: str
    when: ContextWhen = "now"
    cite: bool = True
    added_by: str = ""
    from_creator: bool = False
    beat_at_add: int = 0
    at: int = 0              # live playhead (samples) when it was added
    # dropped: it came attached to a call-in that was declined or removed
    status: Literal["pending", "background", "used", "dropped"] = "pending"
    used_seq: int | None = None
    comment_id: int | None = None

    def public(self) -> dict:
        data = self.model_dump(exclude={"text"})
        data["chars"] = len(self.text)
        return data


class ContextBrief(BaseModel):
    """How a context item is referred to in the writer's per-segment request."""
    id: int
    name: str
    kind: str
    added_by: str
    from_creator: bool
    cite: bool


# ---- Everything the writer needs to know for the next segment ---------------

class ListenerComment(BaseModel):
    id: int
    name: str
    text: str
    attachment: ContextBrief | None = None


class SegmentContext(BaseModel):
    transcript: list[Line]
    beat_index: int
    beat_lines_written: int
    beat_lines_target: int
    elapsed_minutes: float
    target_minutes: int
    comments: list[ListenerComment]
    must_end: bool
    lines_wanted: int
    words_wanted: int = 0               # the segment's length budget; 0 = lines only
    style_change: str | None = None
    context_now: list[ContextBrief] = []
    context_chapter: list[ContextBrief] = []
    hearts: int = 0
    beats_left: int = 0                 # beats after the current one
    planned_minutes_left: float = 0.0   # what the plan gives those beats


# ---- Co-hosting: you talk live with an AI co-host (see cohost.py) ------------

CohostMode = Literal["banter", "interview", "debate"]


class CohostRequest(BaseModel):
    host: HostRequest                     # the AI co-host, usually one of your personas
    mode: CohostMode = "banter"
    topic: str = Field(min_length=1, max_length=2_000)
    # Material for the co-host to draw on; trimmed to settings.cohost_source_chars.
    notes: str = Field(default="", max_length=400_000)
    notes_names: list[str] = Field(default_factory=list, max_length=20)
    your_name: str = Field(default="", max_length=40)   # blank = your account name
    minutes: Literal[5, 10, 15] = 10
    language: str = Field(default="English", max_length=60)

    @field_validator("language")
    @classmethod
    def _default_language(cls, v: str) -> str:
        return v.strip() or "English"


class NewCohost(BaseModel):
    request: CohostRequest
    visibility: Visibility = "private"
    shared_with: list[str] = Field(default_factory=list, max_length=200)


class EditChapter(BaseModel):
    title: str
    notes: str = ""
    start_turn: int = 0


class EditPlan(BaseModel):
    """The editor's notes on a recorded call: what to call it, and what to cut."""
    title: str
    tagline: str = ""
    about: str = ""
    chapters: list[EditChapter] = []
    cut_phrases: list[int] = []   # your phrases to cut (false starts, mic checks, ...)
    drop_turns: list[int] = []    # co-host turns that only answered something cut
