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


class OutlineBeat(BaseModel):
    """A beat the planner must keep (a briefing's run of show), rather than invent."""
    title: str = Field(min_length=1, max_length=80)
    notes: str = Field(default="", max_length=600)
    minutes: float = Field(default=1.0, gt=0, le=15)


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
    # A fixed run of show. Empty: the planner chooses the beats.
    outline: list[OutlineBeat] = Field(default_factory=list, max_length=8)
    # Research: the hosts look things up (Google Search) when a call-in or a chapter
    # needs facts the source doesn't have. Costs extra credits.
    research: bool = False
    # recorded: written and voiced ahead of time (a voice memo's pod), so the
    # prompts leave out call-ins and the other live-only rules.
    mode: Literal["live", "recorded"] = "live"
    # Shape the planner and writers give the episode. Set by the server for memo
    # pods; private like a persona's notes (see main._public_request).
    direction: str = Field(default="", max_length=4_000)
    # From the creator's saved show intro, if the pod uses it (set by the server):
    # the hosts welcome listeners to the show by name and work in its intro line.
    show_name: str = Field(default="", max_length=60)
    intro_line: str = Field(default="", max_length=200)

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
    use_show_intro: bool = True   # open with your saved show intro, if you have one


# Theme music for a pod: "auto" (your show intro's theme, else one from the library
# that suits the pod), "none", a library pack id, or "mine:<id>" (music made for
# you before). A memo pod can also ask for "generate": original music, for credits.
MUSIC_CHARS = 80


class ShowIntroIn(BaseModel):
    """A creator's own show intro: every pod they make opens with it (unless they
    switch it off for one)."""
    show_name: str = Field(min_length=1, max_length=60)
    intro_line: str = Field(default="", max_length=200)
    music: str = Field(default="auto", max_length=MUSIC_CHARS)
    enabled: bool = True


class MemoOptions(BaseModel):
    """What comes with a voice memo upload (the multipart `options` field, as JSON)."""
    minutes: int = Field(default=15, ge=2, le=15)
    review: bool = False                    # stop after the outline for the owner to check
    music: str = Field(default="auto", max_length=MUSIC_CHARS)
    # Any of these overrides the format the memo suggests.
    template: str | None = None
    vibe: str | None = None
    style: str = Field(default="", max_length=2_000)
    style_label: str | None = Field(default=None, max_length=40)
    accent: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    hosts: list[HostRequest] = Field(default_factory=list, max_length=2)
    language: str = Field(default="", max_length=60)       # "" = the memo's own
    credit_as: str = Field(default="", max_length=40)      # "" = your first name
    use_show_intro: bool = True
    visibility: Visibility = "private"
    shared_with: list[str] = Field(default_factory=list, max_length=200)
    album_ids: list[str] = Field(default_factory=list, max_length=20)
    tags: list[str] = Field(default_factory=list, max_length=20)
    client_duration_s: float | None = Field(default=None, ge=0, le=7_200)


class BeatEdit(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    notes: str = Field(default="", max_length=600)


class ProduceMemo(BaseModel):
    """The owner's changes to a memo pod's outline before it's made. Omitted = unchanged."""
    title: str | None = Field(default=None, max_length=70)
    beats: list[BeatEdit] | None = Field(default=None, min_length=3, max_length=10)
    excluded_findings: list[int] | None = Field(default=None, max_length=20)
    minutes: int | None = Field(default=None, ge=2, le=15)
    music: str | None = Field(default=None, max_length=MUSIC_CHARS)


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
    research: str = ""       # with Research on: a search for what the source lacks here


class ShowPlan(BaseModel):
    title: str
    tagline: str
    about: str = ""
    hosts: list[Host]
    beats: list[Beat]
    # The show's first lines, written with the plan so audio can start without
    # waiting on a writer call. Empty means the writers open the show.
    cold_open: list["Line"] = []
    # Short reactions the hosts say the instant they're interrupted, voiced ahead of
    # time so one plays while the real answer is being written.
    reactions: list["Reaction"] = []


InterruptKind = Literal["ask", "deeper", "steer", "move_on"]
INTERRUPT_KINDS = ("ask", "deeper", "steer", "move_on")


class Reaction(BaseModel):
    speaker: str
    kind: str          # an InterruptKind
    text: str


class InterruptNote(BaseModel):
    """What the writer is told about an interruption (or, afterwards, how to bridge back)."""
    kind: str
    text: str = ""
    cut_line: str = ""   # the line the listener was hearing when they cut in


# ---- What the writer returns for each chunk of dialogue ---------------------

class Line(BaseModel):
    speaker: str
    text: str
    style: str = ""


class ResearchRequest(BaseModel):
    comment_id: int
    query: str


class SegmentDraft(BaseModel):
    lines: list[Line]
    addressed_comment_ids: list[int] = []    # call-ins this segment actually answers
    deferred_comment_ids: list[int] = []     # acknowledged on air, answered later
    used_context_ids: list[int] = []
    declined_comment_ids: list[int] = []
    research_requests: list[ResearchRequest] = []
    beat_complete: bool = False


# ---- What a voice memo becomes (see memo.py) ----------------------------------

class MemoBrief(BaseModel):
    """What the model heard in a voice memo: the makings of an episode."""
    usable: bool                     # False: no speech, or nothing a show could be built on
    reason: str = ""                 # why not, for the creator
    language: str = "English"
    transcript: str = ""             # tidied but faithful; for the owner only
    working_title: str = ""
    thesis: str = ""                 # the core idea or question
    key_ideas: list[str] = []
    questions: list[str] = []        # what the creator wondered about
    claims_to_check: list[str] = []
    research_queries: list[str] = []
    tone: str = ""
    music_mood: str = "warm"         # a catalog.MUSIC_MOODS entry
    format: str = "idea-breakdown"   # a catalog.TEMPLATES id


class Finding(BaseModel):
    id: int = 0                      # [R1] in the episode's source
    claim: str
    outlet: str                      # who says so: a publication, institution or site
    date: str = ""
    answers: list[int] = []          # the memo's questions it speaks to (1-based)
    confidence: Literal["solid", "mixed", "thin"] = "solid"


class WebSource(BaseModel):
    title: str
    url: str
    domain: str


class ResearchDossier(BaseModel):
    """What live research turned up. Searches, cited pages and suggestions are
    read from the grounded response itself, not from what the model says."""
    ok: bool = False
    summary: str = ""
    findings: list[Finding] = []
    disagreements: list[str] = []
    unanswered: list[str] = []
    queries: list[str] = []
    sources: list[WebSource] = []
    # Google's Search Suggestions (HTML). The grounding terms want them shown with
    # the results to the user who asked, so only the pod's owner sees them.
    search_suggestions: list[str] = []
    searched_at: float = 0.0


# ---- Material handed to the hosts mid-show ----------------------------------

ContextKind = Literal["text", "file", "link", "photo", "research"]
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
    # research: the chapter it was looked up for (it waits for that chapter), and
    # what it rests on. Search suggestions are Google's chips, shown with the result.
    for_beat: int | None = None
    sources: list[dict] = []          # {title, url, domain}
    queries: list[str] = []
    search_suggestions: list[str] = []

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
    research: ContextBrief | None = None     # what the research desk found for it
    research_failed: bool = False            # it looked, and came back empty


class SegmentContext(BaseModel):
    transcript: list[Line]
    beat_index: int
    beat_lines_written: int
    beat_lines_target: int
    elapsed_minutes: float
    target_minutes: int
    comments: list[ListenerComment]
    promised: list[ListenerComment] = []     # call-ins the hosts said they'd come back to
    research_on: bool = False                # the pod has Research on
    research_left: int = 0                   # lookups still allowed
    must_end: bool
    lines_wanted: int
    words_wanted: int = 0               # the segment's length budget; 0 = lines only
    style_change: str | None = None
    context_now: list[ContextBrief] = []
    context_chapter: list[ContextBrief] = []
    hearts: int = 0
    beats_left: int = 0                 # beats after the current one
    planned_minutes_left: float = 0.0   # what the plan gives those beats
    interjection: InterruptNote | None = None   # answer this interruption now
    bridge: InterruptNote | None = None         # an interruption was just answered: pick the thread back up


# ---- Co-hosting: you talk live with an AI co-host (see cohost.py) ------------

CohostMode = Literal["banter", "interview", "debate"]


class CohostRequest(BaseModel):
    host: HostRequest                     # the AI co-host, usually one of your personas
    mode: CohostMode = "banter"
    topic: str = Field(min_length=1, max_length=2_000)
    # Material for the co-host to draw on: given whole up to settings.cohost_source_chars,
    # past that as a brief plus a lookup over the rest (notes.py).
    notes: str = Field(default="", max_length=400_000)
    notes_names: list[str] = Field(default_factory=list, max_length=20)
    your_name: str = Field(default="", max_length=40)   # blank = your account name
    minutes: Literal[5, 10, 15] = 10
    language: str = Field(default="English", max_length=60)

    @field_validator("language")
    @classmethod
    def _default_language(cls, v: str) -> str:
        return v.strip() or "English"


class CohostNotes(BaseModel):
    """Long notes sent ahead from the setup screen, so their brief is ready by the call."""
    notes: str = Field(max_length=400_000)


class NewCohost(BaseModel):
    request: CohostRequest
    visibility: Visibility = "private"
    shared_with: list[str] = Field(default_factory=list, max_length=200)
    # Open with the creator's saved show intro (name, intro line, theme) if they have one.
    use_show_intro: bool = True


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
