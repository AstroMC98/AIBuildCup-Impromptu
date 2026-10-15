"""Who is signed in.

Accounts come from Firebase Authentication (Google or email + password). The
browser signs in with the Firebase JS SDK and hands us the ID token once; we
verify it and set our own HMAC-signed session cookie, so every later request
(and every WebSocket) is authenticated by that cookie without calling Google.

AUTH_MODE=dev skips Firebase: any name and email signs you in. It exists for
local development and the offline tests, and the app refuses to start with it
on Google Cloud (see config.py).

Rotating SESSION_SECRET signs everyone out.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from .config import Settings

log = logging.getLogger("podcasts.auth")

COOKIE = "gp_session"
SESSION_MAX_AGE = 30 * 24 * 3600


class AuthError(Exception):
    pass


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _load_secret(settings: Settings) -> bytes:
    if settings.session_secret:
        return settings.session_secret.encode()
    # Local dev: keep one secret on disk so sessions survive reloads.
    path = Path(settings.data_dir) / ".session_secret"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        log.info("generated a local session secret at %s", path)
    return path.read_text(encoding="utf-8").strip().encode()


class Auth:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.mode = settings.auth_mode
        self.secure = settings.cookie_secure
        self.admins = settings.admins
        self._secret = _load_secret(settings)
        if self.mode == "dev":
            log.warning("AUTH_MODE=dev: anyone can sign in as anyone. Local use only.")

    def _sign(self, message: str) -> str:
        return _b64(hmac.new(self._secret, message.encode(), hashlib.sha256).digest())

    def is_admin(self, email: str | None) -> bool:
        return bool(email) and email.strip().lower() in self.admins

    # ---- sessions ---------------------------------------------------------------

    def issue_session(self, uid: str) -> str:
        issued = int(time.time())
        u = _b64(uid.encode())
        return f"v2.{u}.{issued}.{self._sign(f'session|v2|{u}|{issued}')}"

    def session_uid(self, cookie: str | None) -> str | None:
        """The signed-in user's id, or None."""
        if not cookie:
            return None
        try:
            version, u, issued, sig = cookie.split(".", 3)
            age = time.time() - int(issued)
            uid = _unb64(u).decode()
        except (ValueError, UnicodeDecodeError):
            return None
        if version != "v2" or not 0 <= age <= SESSION_MAX_AGE:
            return None
        if not hmac.compare_digest(sig, self._sign(f"session|v2|{u}|{issued}")):
            return None
        return uid

    # ---- Firebase -----------------------------------------------------------------

    async def verify_firebase(self, id_token: str) -> dict:
        """Check a Firebase ID token and return {uid, email, name, photo}."""
        if self.mode != "firebase":
            raise AuthError("Firebase sign-in isn't enabled on this server.")
        try:
            claims = await asyncio.to_thread(self._verify, id_token)
        except Exception as exc:
            log.info("rejected a Firebase token: %s", exc)
            raise AuthError("That sign-in didn't check out. Try again.") from exc
        if claims.get("email") and not claims.get("email_verified", False) and \
                claims.get("firebase", {}).get("sign_in_provider") == "password":
            # Email + password accounts must verify their address first.
            raise AuthError("Check your inbox and verify your email address, then sign in again.")
        picture = claims.get("picture") or ""
        return {
            "uid": claims["sub"],
            "email": (claims.get("email") or "").lower(),
            "name": (claims.get("name") or (claims.get("email") or "Listener").split("@")[0])[:60],
            # Users can set their own photo URL in Firebase; only plain https images get shown.
            "photo": picture if picture.startswith("https://") else "",
        }

    def _verify(self, id_token: str) -> dict:
        import google.auth.transport.requests
        from google.oauth2 import id_token as google_id_token

        return google_id_token.verify_firebase_token(
            id_token,
            google.auth.transport.requests.Request(),
            audience=self.settings.firebase_project_id,
            clock_skew_in_seconds=10,
        )

    @staticmethod
    def dev_uid(email: str) -> str:
        return "dev-" + hashlib.sha256(email.strip().lower().encode()).hexdigest()[:20]


def client_ip(headers, fallback: str | None) -> str:
    """The caller's address, for rate limits. Cloud Run's front end appends the real
    client address as the last X-Forwarded-For entry; anything before it was sent by
    the client and could be forged to dodge limits, so it is ignored."""
    forwarded = headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return fallback or "unknown"


def same_origin(headers) -> bool:
    """Blocks other sites from driving a signed-in user's browser. Clients that
    send no Origin (curl, tests) are not browsers and carry no ambient cookie risk."""
    origin = headers.get("origin")
    if not origin:
        return True
    host = headers.get("x-forwarded-host") or headers.get("host") or ""
    return urlsplit(origin).netloc.lower() == host.lower()
