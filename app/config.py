"""Runtime configuration. Every knob is an env var (or a line in .env)."""
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    gemini_api_key: str = ""
    # Only set this to point the SDK at a stub server (see tests/fake_gemini.py).
    gemini_base_url: str | None = None

    # Writes the episode plan and every dialogue segment, and describes photos.
    script_model: str = "gemini-3.8-flash"
    script_thinking_level: str = "low"  # low keeps segment latency down
    # Answers to an interruption. gemini-3.8-flash rejects "minimal" (HTTP 400), so "low"
    # is the fastest it allows; a model that accepts "minimal" could answer sooner.
    interjection_thinking_level: str = "low"
    max_interrupts: int = 30            # per show
    stinger_delay_seconds: float = 6.0  # voice the interruption reactions once the cold open is going
    # Voices the dialogue. gemini-3.8-flash-lite-tts is faster and cheaper.
    tts_model: str = "gemini-3.8-flash-tts"

    # Audio per segment. Shorter = hosts react to call-ins and restyles sooner, but
    # more API calls. Segments are sized in words from the measured speaking pace.
    segment_seconds: float = 20.0
    # The most lines in a segment (a fast-talking pair can fit more in the time).
    lines_per_segment: int = 5
    # Voice the next segment once the live playhead is less than this many
    # seconds from the end of the voiced audio. Anything written but not yet
    # voiced can still be rewritten to fit in a comment, new context or a restyle.
    low_water_seconds: float = 8.0
    # How many times a finished draft may be thrown away to fit in new input.
    max_rewrites: int = 2

    max_source_chars: int = 400_000
    sample_rate: int = 24_000  # Gemini TTS streams 24 kHz mono s16le PCM

    # ---- accounts -----------------------------------------------------------
    # firebase: Google / email sign-in through Firebase Authentication.
    # dev: type any name and email to sign in. Local development and tests only.
    auth_mode: Literal["firebase", "dev"] = "dev"
    firebase_project_id: str = ""
    firebase_api_key: str = ""        # the web app's public API key (not a secret)
    firebase_auth_domain: str = ""    # usually PROJECT.firebaseapp.com
    # Comma-separated emails that can grant credits and take pods down.
    admin_emails: str = ""
    # Signs session cookies. Changing it signs everyone out. Local dev
    # generates one under data_dir.
    session_secret: str = ""
    cookie_secure: bool = False

    # ---- credits --------------------------------------------------------------
    # Pods cost credits to create; listening, calling in and connecting are free.
    credits_per_minute: float = 1.0
    # Extra per minute for a pod with Research on (the hosts look things up live).
    research_credits_per_minute: float = 0.5
    # Credits every new account starts with. 0 = new accounts are free accounts.
    signup_credits: int = 0
    # Demo version: a button on the You page brings anyone's balance back up to this many
    # credits, as often as they like (it never takes credits away). 0 hides the button.
    demo_reset_credits: int = 30

    # ---- voice memos (a memo becomes a finished, pre-recorded pod; see memo.py) ----
    # Uploads go to Gemini inline, and requests cap at 20 MB after base64.
    memo_max_mb: int = 14
    memo_max_seconds: int = 600
    memo_model: str = ""              # "" = script_model
    max_research_queries: int = 5
    memo_research_timeout_seconds: float = 120.0
    # Memo pods render one at a time next to the live rooms; raise once TTS
    # throughput is measured (tests/live_test.py --memo).
    max_renders: int = 1
    max_queued_renders: int = 6
    render_max_minutes: float = 30.0
    # A render has no listeners to react to, so it writes bigger segments and can
    # wait longer on a slow writer call than a live show can.
    render_segment_seconds: float = 40.0
    render_lines_per_segment: int = 8
    render_write_timeout_seconds: float = 45.0
    render_mp3_kbps: int = 64         # music needs more than the live 48
    # The prepackaged music library (music/manifest.json). Empty = voice only.
    music_dir: str = "music"
    # Original music for a pod costs extra credits. "" turns that option off.
    music_model: str = "lyria-3-clip-preview"
    music_credits: int = 2
    music_timeout_seconds: float = 120.0
    # ---- personal briefings (see briefing.py) ---------------------------------
    briefing_minutes_per_credit: int = 5   # a 5-minute briefing costs 1 credit
    briefing_keep_hours: float = 48.0      # then it's deleted
    # A scheduled briefing starts rendering this long before the time it's wanted.
    briefing_lead_minutes: float = 30.0
    # How often scheduled briefings and expired ones are looked for.
    briefing_poll_seconds: float = 60.0
    # This app's public address: named in the watch fetcher's User-Agent, and what
    # wake-ups call. Blank = the address the last request came in on.
    public_url: str = ""
    # Cloud Tasks queue (projects/P/locations/R/queues/Q) that wakes the server for
    # scheduled briefings on Cloud Run. Blank (local) = no wake-ups needed.
    wake_queue: str = ""

    # ---- storage ------------------------------------------------------------
    storage_backend: Literal["local", "gcp"] = "local"
    data_dir: str = "data"
    gcs_bucket: str = ""
    gcp_project: str = ""
    firestore_database: str = "(default)"

    # ---- live rooms ---------------------------------------------------------
    # Speeds up the server's live clock. Tests run shows at 20x; leave at 1.
    clock_speed: float = 1.0
    # A late joiner gets this much audio behind the live edge, so Back 15s works.
    backfill_seconds: float = 20.0
    # Audio kept in memory behind the live edge, for backfill and reconnects.
    retain_behind_seconds: float = 90.0
    # With nobody connected for this long, the hosts sign off and the show is saved.
    idle_end_seconds: float = 300.0
    # How long an ended room stays around for listeners still playing its tail.
    ended_room_grace_seconds: float = 120.0
    max_live_shows: int = 3
    max_listeners: int = 50
    max_conns_per_ip: int = 5
    max_show_hours: float = 3.0
    mp3_kbps: int = 48

    # ---- research (pods with Research on; see show.py) ---------------------------
    # Google Search lookups per pod: for call-ins the source can't answer, and for
    # chapters the planner found thin. One per call-in.
    research_max_per_show: int = 6
    research_max_beats: int = 3          # of those, at most this many for chapters
    # A lookup taking longer is abandoned; the hosts answer with what they know.
    research_timeout_seconds: float = 30.0

    # ---- co-hosting (you talk live with an AI co-host; see cohost.py) ----------
    # A Gemini Live native-audio model: speech in, speech out, with barge-in.
    cohost_model: str = "gemini-3.1-flash-live-preview"
    # How long you pause before the co-host answers. Lower is snappier but
    # interrupts slow talkers mid-thought.
    cohost_silence_ms: int = 500
    # A call whose phone drops off is saved as an episode after this long.
    cohost_reconnect_seconds: float = 30.0
    # Your notes for the co-host: up to this many characters go to it whole. A Live
    # session bills its whole context on every turn, so longer notes are read into a
    # brief (about cohost_brief_words) and the co-host searches the rest with a tool
    # (notes.py), up to cohost_notes_max_chars.
    cohost_source_chars: int = 20_000
    cohost_notes_max_chars: int = 400_000
    cohost_brief_words: int = 1_200
    # Starting a call waits this long for a brief that is still being written, then
    # goes ahead with the notes' first part (and the lookup over all of them).
    cohost_brief_timeout_seconds: float = 40.0
    # Hang-up to saved episode: past this, the editor's notes are skipped and the
    # episode is saved with the mechanical edits only.
    cohost_edit_timeout_seconds: float = 40.0

    @property
    def admins(self) -> set[str]:
        return {e.strip().lower() for e in self.admin_emails.split(",") if e.strip()}

    @model_validator(mode="after")
    def _production_needs_secrets(self) -> "Settings":
        if self.storage_backend == "gcp":
            if self.auth_mode != "firebase":
                raise ValueError("STORAGE_BACKEND=gcp needs AUTH_MODE=firebase (dev sign-in is for local use only).")
            needed = ("session_secret", "gcs_bucket", "firebase_project_id", "firebase_api_key")
            missing = [n for n in needed if not getattr(self, n)]
            if missing:
                raise ValueError(f"STORAGE_BACKEND=gcp needs {', '.join(m.upper() for m in missing)}.")
        if self.auth_mode == "firebase" and not self.firebase_project_id:
            raise ValueError("AUTH_MODE=firebase needs FIREBASE_PROJECT_ID.")
        return self


settings = Settings()
