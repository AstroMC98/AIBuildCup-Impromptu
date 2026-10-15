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
    # Credits every new account starts with. 0 = new accounts are free accounts.
    signup_credits: int = 0

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
