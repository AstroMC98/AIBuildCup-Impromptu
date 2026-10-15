"""HTTP + WebSocket surface.

Pages (all serve the single-page UI)
GET  /  /discover  /following  /new  /login  /personas  /styles  /connections  /admin
GET  /albums  /stats  /show/{id}  /show/{id}/stats  /album/{id}  /creator/{uid}  /invite/{uid}

API ("you" = the signed-in user; pods are visible per accounts.can_view)
GET    /healthz
GET    /api/options                      voices, presets, templates, sign-in config, you
POST   /api/auth/firebase {id_token}     sign in with a Firebase ID token
POST   /api/auth/dev {email, name}       AUTH_MODE=dev only
DELETE /api/auth/session                 sign out
GET    /api/me    /api/me/ledger         your account, pending requests, credit history
GET|POST /api/personas  PUT|DELETE /api/personas/{id}   your speaker personas (with private notes)
GET|POST /api/styles    PUT|DELETE /api/styles/{id}     your styles
GET    /api/connections                  connected, incoming, outgoing
POST   /api/connections {email|uid}      send (or accept) a request
POST   /api/connections/{uid}/accept     DELETE /api/connections/{uid}
GET    /api/users/{uid}                  name and photo, for invite links
POST   /api/extract                      files / photos / a link -> text for the source box
POST   /api/shows {request, live, visibility, shared_with, template}
                                         create a pod (needs credits); live charges them now
GET    /api/shows?scope=mine|shared|public
GET    /api/shows/{id}                   the pod (plus transcript etc. once it's over)
PATCH  /api/shows/{id} {visibility, shared_with}   owner
PATCH  /api/shows/{id}/organize {album_ids?, tags?}  owner
GET    /api/shows/{id}/stats             owner: listeners, drop-off, replays, who listened
POST   /api/shows/{id}/listen {sid, pos, played, ended?, lid?}   replay progress (sendBeacon-safe)
GET|POST /api/albums   GET|PUT|DELETE /api/albums/{id}   albums (playlists of your pods)
GET    /api/creators/{uid}               a creator's pods and albums you can see, subscriber count
GET|POST /api/subscriptions  DELETE /api/subscriptions/{kind}/{id}   creators and albums you follow
GET    /api/feed   POST /api/feed/seen   pods from your subscriptions; "new" since you last looked
GET    /api/me/stats                     your pods' listening at a glance, subscriber counts
POST   /api/shows/{id}/start             owner: take a draft live (charges credits)
DELETE /api/shows/{id}                   owner or admin
POST   /api/shows/{id}/context           signed-in listeners: text|file|link|photo (+ optional call-in)
GET    /api/shows/{id}/audio             the MP3 (so far, while live)
GET    /api/admin/users?q=               admin
POST   /api/admin/users/{uid}/credits {amount, note}   admin
WS     /ws/show/{id}                     one connection per listener of a live pod

WebSocket protocol
  close codes: 4401 sign in, 4403 no access, 4404 no such show,
               4409 not live (load it over HTTP), 4429 full or flooding,
               1013 fell too far behind (reconnect), 1012 server restarting
  roles: producer (the owner), listener (signed in), visitor (public pod, signed out:
         can listen and react, not call in)
  browser -> server (JSON, first message must be join)
    {"type": "join", "have_from"?, "have_to"?, "lid"?}   have_*: samples already held;
                                                  lid: the browser's listener id (stats)
    {"type": "player", "playing": bool}
    {"type": "comment", "text", "client_id"}
    {"type": "react", "n": 1-10}
    {"type": "restyle", "vibe"? | "style"?}       producer
    {"type": "end", "mode": "wrap" | "now"}       producer
    {"type": "remove_comment", "id"}              producer
  server -> browser
    JSON: snapshot | status | plan | segment | segment_end | chapter | comment |
          context | restyle | react | tick | ended | saved | error
    binary: <seq:u32 LE><start_sample:u32 LE> + raw 24 kHz mono s16le PCM
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from typing import Literal

from pydantic import BaseModel, Field, ValidationError
from starlette.requests import HTTPConnection
from fastapi.staticfiles import StaticFiles

from . import ingest as ingest_mod
from .accounts import Accounts, User
from .auth import COOKIE, SESSION_MAX_AGE, Auth, AuthError, client_ip, same_origin
from . import stats as stats_mod
from .catalog import (
    GENDERS, HOST_COLORS, PERSONA_PRESETS, STYLE_PRESETS, TEMPLATES, TEMPLATES_BY_ID, VOICE_GENDER, VOICE_INFO,
    VOICES, accent_for, style_label,
)
from .config import settings
from .limits import COMMENT_CHARS, GUEST_CONTEXT, NAME_CHARS, PRODUCER_CONTEXT, WS_MESSAGE_BYTES, Limiter
from .llm import Gemini
from .models import AlbumIn, ContextItem, NewShow, Organize, PersonaIn, ShareSettings, ShowRequest, StyleIn
from .room import Conn, Rooms, ShowRoom, style_revisions
from .sources import IMAGE_EXTENSIONS, SUPPORTED, SourceError
from .store import InsufficientCredits, make_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("podcasts")

STATIC = Path(__file__).resolve().parent.parent / "static"
INDEX = STATIC / "index.html"
MAX_SAVED_ITEMS = 50          # personas or styles per user
MAX_ALBUMS = 50               # per user
MAX_TAGS, TAG_CHARS = 12, 30
SIGN_IN_LIMIT = Limiter(rate=10 / 60, burst=10)
LISTEN_LIMIT = Limiter(rate=1 / 4, burst=4)   # replay progress reports, per playback
_TOKEN = re.compile(r"^[A-Za-z0-9]{6,40}$")

store = make_store(settings)
auth = Auth(settings)
accounts = Accounts(store, settings.admins, settings.signup_credits)
rooms = Rooms()
_gemini: Gemini | None = None


def gemini() -> Gemini:
    global _gemini
    if _gemini is None:
        _gemini = Gemini(settings)
    return _gemini


def gemini_or_none() -> Gemini | None:
    try:
        return gemini()
    except RuntimeError:
        return None


@asynccontextmanager
async def lifespan(_: FastAPI):
    stale = await store.mark_interrupted(set())
    if stale:
        log.info("marked %d shows that were live before a restart as interrupted", stale)
    yield
    await rooms.shutdown(timeout=6.0)


app = FastAPI(title="Gemini Podcasts", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def no_stale_ui(request: Request, call_next):
    """No build step means no hashed file names; make browsers revalidate after a deploy."""
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# ---- helpers -------------------------------------------------------------------

async def current_user(conn: HTTPConnection) -> User | None:
    return await accounts.get(auth.session_uid(conn.cookies.get(COOKIE)))


async def require_user(request: Request) -> User:
    user = await current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in first.")
    return user


def _require_origin(request: Request) -> None:
    if not same_origin(request.headers):
        raise HTTPException(status_code=403, detail="Cross-site request blocked.")


def _ip(request: Request) -> str:
    return client_ip(request.headers, request.client.host if request.client else None)


def _cost(minutes: int) -> int:
    return max(1, math.ceil(minutes * settings.credits_per_minute))


async def _show_doc(show_id: str) -> tuple[dict, ShowRoom | None]:
    room = rooms.get(show_id)
    doc = room.doc if room else await store.get_show(show_id)
    if not doc:
        raise HTTPException(status_code=404, detail="No such pod.")
    return doc, room


async def _viewable(request: Request, show_id: str) -> tuple[dict, ShowRoom | None, User | None]:
    doc, room = await _show_doc(show_id)
    user = await current_user(request)
    if not await accounts.can_view(user, doc):
        if user is None:
            raise HTTPException(status_code=401, detail="Sign in to listen to this pod.")
        raise HTTPException(status_code=404, detail="No such pod, or it isn't shared with you.")
    return doc, room, user


def _owner(doc: dict, user: User) -> None:
    if doc.get("owner_uid") != user.uid:
        raise HTTPException(status_code=403, detail="Only the pod's creator can do that.")


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (title or "episode").lower()).strip("-")[:60] or "episode"


def _working_title(req: ShowRequest, source: str) -> str:
    if req.source_names:
        first = req.source_names[0]
    else:
        first = source.strip().splitlines()[0] if source.strip() else "New pod"
    return first if len(first) <= 60 else first[:59].rstrip() + "…"


def _clean_tags(tags: list[str]) -> list[str]:
    """Free-form labels: trimmed, one space between words, no repeats (ignoring case)."""
    out: dict[str, str] = {}
    for t in tags:
        t = re.sub(r"\s+", " ", str(t)).strip().lstrip("#")[:TAG_CHARS]
        if t and t.casefold() not in out:
            out[t.casefold()] = t
    return list(out.values())[:MAX_TAGS]


def _public_request(req: dict) -> dict:
    """What a pod was asked for, minus anything private: a persona's own notes stay
    with its owner and the writers."""
    hosts = [{**{k: v for k, v in h.items() if k != "context"}, "has_notes": bool(h.get("context"))}
             for h in req.get("hosts", [])]
    return {**req, "hosts": hosts}


def _token(value) -> str | None:
    value = str(value or "")
    return value if _TOKEN.match(value) else None


def _fresh(doc: dict) -> dict:
    """A live pod's room has the up-to-date document."""
    room = rooms.get(doc["id"])
    return room.doc if room else doc


async def _visible(user: User | None, docs: list[dict], *, drafts: bool = False) -> list[dict]:
    out = []
    for d in docs:
        d = _fresh(d)
        if (drafts or d.get("status") != "draft") and await accounts.can_view(user, d):
            out.append(d)
    return out


async def _owned_album_ids(user: User, ids: list[str]) -> list[str]:
    mine = {a["id"] for a in await store.list_albums(owner_uid=user.uid)}
    return [i for i in dict.fromkeys(ids) if i in mine]


def _album_out(a: dict, pods: int | None = None) -> dict:
    keys = ("id", "title", "description", "visibility", "accent", "owner_uid", "owner_name", "created_at")
    return {**{k: a.get(k) for k in keys}, "pods": pods}


def _sub_id(uid: str, kind: str, target_id: str) -> str:
    return f"{uid}__{kind}__{target_id}"


async def _subscribed_to(user: User | None) -> set[tuple[str, str]]:
    if user is None:
        return set()
    return {(s["kind"], s["target_id"]) for s in await store.list_subscriptions(user.uid)}


async def _show_extras(doc: dict, user: User | None) -> dict:
    """The creator (to subscribe to) and the albums this pod is in that you can see."""
    owner_uid = doc.get("owner_uid", "")
    subs = await _subscribed_to(user)
    albums = []
    for album_id in doc.get("album_ids", []):
        album = await store.get_album(album_id)
        if album and await accounts.can_view(user, album):
            albums.append({"id": album["id"], "title": album["title"], "accent": album.get("accent")})
    owner = await accounts.get(owner_uid)
    return {
        "creator": {
            **(owner.public() if owner else {"uid": owner_uid, "name": doc.get("owner_name", ""), "photo": ""}),
            "subscribers": await store.count_subscribers(f"creator:{owner_uid}"),
            "subscribed": ("creator", owner_uid) in subs,
            "you": bool(user and user.uid == owner_uid),
        },
        "albums": albums,
    }


def _public_doc(doc: dict) -> dict:
    return {
        "styles": style_revisions(doc),
        "id": doc["id"],
        "status": doc.get("status"),
        "ended_reason": doc.get("ended_reason"),
        "title": doc.get("title", ""),
        "tagline": doc.get("tagline", ""),
        "about": doc.get("about", ""),
        "accent": doc.get("accent"),
        "vibe": doc.get("vibe"),
        "style_label": doc.get("style_label", ""),
        "style": doc.get("style", ""),
        "minutes": doc.get("minutes"),
        "language": doc.get("language"),
        "hosts": doc.get("hosts", []),
        "beats": doc.get("beats", []),
        "sources": doc.get("source", {}).get("names", []),
        "created_at": doc.get("created_at"),
        "duration_s": doc.get("duration_s", 0),
        "samples": doc.get("samples", 0),
        "has_audio": bool(doc.get("audio")),
        "counts": doc.get("counts", {}),
        "request": _public_request(doc.get("request", {})),
        "album_ids": doc.get("album_ids", []),
        "tags": doc.get("tags", []),
        "owner_uid": doc.get("owner_uid"),
        "owner_name": doc.get("owner_name", ""),
        "visibility": doc.get("visibility", "private"),
        "shared_with": doc.get("shared_with", []),
        "template": doc.get("template"),
    }


def _list_item(d: dict) -> dict:
    room = rooms.get(d["id"])
    live = bool(room and room.ended_reason is None)
    return {
        "id": d["id"],
        "title": d.get("title", ""),
        "status": "live" if live else d.get("status"),
        "vibe": d.get("vibe"),
        "style_label": d.get("style_label", ""),
        "accent": d.get("accent"),
        "created_at": d.get("created_at"),
        "duration_s": d.get("duration_s", 0),
        "minutes": d.get("minutes"),
        "hosts": [h.get("name", "") for h in d.get("hosts", [])],
        "owner_uid": d.get("owner_uid"),
        "owner_name": d.get("owner_name", ""),
        "visibility": d.get("visibility", "private"),
        "tags": d.get("tags", []),
        "album_ids": d.get("album_ids", []),
    }


def _start_room(doc: dict, req: ShowRequest, source: str, model: Gemini) -> ShowRoom:
    room = ShowRoom(doc=doc, request=req, source=source, gemini=model,
                    settings=settings, store=store, on_closed=rooms.remove)
    rooms.add(room)
    return room


def _check_capacity() -> Gemini:
    if rooms.live_count() >= settings.max_live_shows:
        raise HTTPException(status_code=429, detail="Too many pods are live right now. Try again soon.")
    try:
        return gemini()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


async def _charge(user: User, cost: int, show_id: str) -> None:
    try:
        await accounts.adjust_credits(user.uid, -cost, reason="pod", show_id=show_id)
    except InsufficientCredits as exc:
        raise HTTPException(status_code=402, detail=(
            f"This pod needs {exc.needed} credits and you have {exc.balance}. "
            "Creating pods uses credits; listening and calling in are free."
        )) from exc


async def _clean_share_list(user: User, uids: list[str]) -> list[str]:
    """Pods can only be shared by name with your connections."""
    if not uids:
        return []
    mine = set(await accounts.connection_ids(user.uid))
    return [u for u in dict.fromkeys(uids) if u in mine]


def _set_session(res: Response, uid: str) -> None:
    res.set_cookie(COOKIE, auth.issue_session(uid), max_age=SESSION_MAX_AGE,
                   httponly=True, samesite="lax", secure=auth.secure, path="/")


# ---- pages ---------------------------------------------------------------------

def _index() -> FileResponse:
    return FileResponse(INDEX, headers={"Referrer-Policy": "same-origin", "Cache-Control": "no-cache"})


@app.get("/")
@app.get("/discover")
@app.get("/following")
@app.get("/albums")
@app.get("/stats")
@app.get("/new")
@app.get("/login")
@app.get("/personas")
@app.get("/styles")
@app.get("/connections")
@app.get("/admin")
async def page() -> FileResponse:
    return _index()


@app.get("/show/{show_id}")
@app.get("/show/{show_id}/stats")
@app.get("/album/{album_id}")
@app.get("/creator/{uid}")
@app.get("/invite/{uid}")
async def page_with_id(show_id: str = "", uid: str = "", album_id: str = "") -> FileResponse:
    return _index()


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "live_shows": rooms.live_count()}


# ---- accounts --------------------------------------------------------------------

@app.get("/api/options")
async def options(request: Request) -> dict:
    user = await current_user(request)
    return {
        "voices": [{"name": k, "character": c, "gender": g} for k, (c, g) in VOICE_INFO.items()],
        "genders": list(GENDERS),
        "presets": STYLE_PRESETS,
        "templates": TEMPLATES,
        "persona_presets": PERSONA_PRESETS,
        "host_colors": HOST_COLORS,
        "file_types": SUPPORTED,
        "image_types": IMAGE_EXTENSIONS,
        "max_source_chars": settings.max_source_chars,
        "api_key_set": bool(settings.gemini_api_key),
        "models": {"script": settings.script_model, "tts": settings.tts_model},
        "credits_per_minute": settings.credits_per_minute,
        "auth": {
            "mode": auth.mode,
            "firebase": {
                "apiKey": settings.firebase_api_key,
                "authDomain": settings.firebase_auth_domain or f"{settings.firebase_project_id}.firebaseapp.com",
                "projectId": settings.firebase_project_id,
            } if auth.mode == "firebase" else None,
        },
        "me": user.me() if user else None,
        "limits": {
            "comment_chars": COMMENT_CHARS,
            "name_chars": NAME_CHARS,
            "guest_text_chars": ingest_mod.GUEST.text_chars,
            "producer_text_chars": ingest_mod.PRODUCER.text_chars,
            "guest_file_mb": ingest_mod.GUEST.file_bytes // ingest_mod.MB,
            "producer_file_mb": ingest_mod.PRODUCER.file_bytes // ingest_mod.MB,
            "photo_mb": ingest_mod.PRODUCER.photo_bytes // ingest_mod.MB,
        },
    }


class FirebaseSignIn(BaseModel):
    id_token: str = Field(min_length=20, max_length=8000)


class DevSignIn(BaseModel):
    email: str = Field(min_length=3, max_length=120)
    name: str = Field(default="", max_length=60)


@app.post("/api/auth/firebase")
async def sign_in_firebase(body: FirebaseSignIn, request: Request) -> JSONResponse:
    _require_origin(request)
    if not SIGN_IN_LIMIT.take(_ip(request)):
        raise HTTPException(status_code=429, detail="Too many sign-in attempts. Wait a minute.")
    try:
        profile = await auth.verify_firebase(body.id_token)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    user = await accounts.sign_in(profile)
    res = JSONResponse({"me": user.me()})
    _set_session(res, user.uid)
    return res


@app.post("/api/auth/dev")
async def sign_in_dev(body: DevSignIn, request: Request) -> JSONResponse:
    if auth.mode != "dev":
        raise HTTPException(status_code=404, detail="Not found.")
    _require_origin(request)
    email = body.email.strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(status_code=422, detail="That doesn't look like an email address.")
    user = await accounts.sign_in({
        "uid": Auth.dev_uid(email), "email": email, "name": body.name.strip() or email.split("@")[0],
    })
    res = JSONResponse({"me": user.me()})
    _set_session(res, user.uid)
    return res


@app.delete("/api/auth/session")
async def sign_out(request: Request) -> JSONResponse:
    _require_origin(request)
    res = JSONResponse({"ok": True})
    res.delete_cookie(COOKIE, path="/")
    return res


@app.get("/api/me")
async def me(request: Request) -> dict:
    user = await require_user(request)
    edges = await store.get_edges(user.uid)
    return {
        "me": user.me(),
        "incoming": sum(e.get("status") == "pending_in" for e in edges),
        "connections": sum(e.get("status") == "connected" for e in edges),
    }


@app.get("/api/me/ledger")
async def my_ledger(request: Request) -> dict:
    user = await require_user(request)
    return {"credits": user.credits, "entries": await store.ledger(user.uid, limit=100)}


# ---- personas and styles ------------------------------------------------------------

def _saved_item_routes(kind: str, model: type[BaseModel], noun: str) -> None:
    """GET/POST /api/{kind}, PUT/DELETE /api/{kind}/{id}. Bodies are validated
    against `model` by hand: a closure variable can't be a FastAPI annotation."""
    def clean(payload: dict) -> dict:
        try:
            data = model.model_validate(payload).model_dump()
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail="; ".join(e["msg"] for e in exc.errors())) from exc
        if kind == "personas":
            if data.get("voice") not in VOICES:
                data["voice"] = "auto"
            voice_gender = VOICE_GENDER.get(data["voice"])
            if voice_gender and data.get("gender") and data["gender"] != voice_gender:
                raise HTTPException(status_code=422, detail=(
                    f"{data['voice']} is a {voice_gender} voice. Pick a {data['gender']} voice, or Auto."))
            if voice_gender and not data.get("gender"):
                data["gender"] = voice_gender
        return {k: v.strip() if isinstance(v, str) else v for k, v in data.items()}

    @app.get(f"/api/{kind}", name=f"list_{kind}")
    async def list_items(request: Request) -> dict:
        user = await require_user(request)
        return {"items": await store.list_items(user.uid, kind)}

    @app.post(f"/api/{kind}", name=f"create_{kind}")
    async def create_item(request: Request, payload: dict = Body(...)) -> dict:
        _require_origin(request)
        user = await require_user(request)
        data = clean(payload)
        if len(await store.list_items(user.uid, kind)) >= MAX_SAVED_ITEMS:
            raise HTTPException(status_code=422, detail=f"You can keep up to {MAX_SAVED_ITEMS} {noun}s.")
        item = {"id": secrets.token_urlsafe(8), "created_at": time.time(), **data}
        await store.put_item(user.uid, kind, item)
        return item

    @app.put(f"/api/{kind}/{{item_id}}", name=f"update_{kind}")
    async def update_item(item_id: str, request: Request, payload: dict = Body(...)) -> dict:
        _require_origin(request)
        user = await require_user(request)
        data = clean(payload)
        existing = next((i for i in await store.list_items(user.uid, kind) if i["id"] == item_id), None)
        if not existing:
            raise HTTPException(status_code=404, detail=f"No such {noun}.")
        item = {**existing, **data, "updated_at": time.time()}
        await store.put_item(user.uid, kind, item)
        return item

    @app.delete(f"/api/{kind}/{{item_id}}", name=f"delete_{kind}")
    async def delete_item(item_id: str, request: Request) -> dict:
        _require_origin(request)
        user = await require_user(request)
        try:
            await store.delete_item(user.uid, kind, item_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=f"No such {noun}.") from exc
        return {"ok": True}


_saved_item_routes("personas", PersonaIn, "persona")
_saved_item_routes("styles", StyleIn, "style")


# ---- connections ---------------------------------------------------------------------

class ConnectRequest(BaseModel):
    email: str = ""
    uid: str = ""


@app.get("/api/connections")
async def list_connections(request: Request) -> dict:
    user = await require_user(request)
    edges = sorted(await store.get_edges(user.uid), key=lambda e: e.get("name", "").lower())
    by = lambda s: [e for e in edges if e.get("status") == s]  # noqa: E731
    return {"connected": by("connected"), "incoming": by("pending_in"), "outgoing": by("pending_out"),
            "invite_path": f"/invite/{user.uid}"}


@app.post("/api/connections")
async def connect(body: ConnectRequest, request: Request) -> dict:
    _require_origin(request)
    user = await require_user(request)
    other_doc = None
    if body.uid.strip():
        other_doc = await store.get_user(body.uid.strip())
    elif body.email.strip():
        other_doc = await store.find_user_by_email(body.email)
    if not other_doc:
        raise HTTPException(status_code=404, detail="No account with that email yet. Send them your invite link.")
    other = await accounts.get(other_doc["uid"])
    try:
        status = await accounts.request(user, other)  # type: ignore[arg-type]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": status, "user": other.public()}  # type: ignore[union-attr]


@app.post("/api/connections/{uid}/accept")
async def accept_connection(uid: str, request: Request) -> dict:
    _require_origin(request)
    user = await require_user(request)
    other = await accounts.get(uid)
    if other is None:
        raise HTTPException(status_code=404, detail="That account no longer exists.")
    try:
        return {"status": await accounts.accept(user, other)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/api/connections/{uid}")
async def remove_connection(uid: str, request: Request) -> dict:
    _require_origin(request)
    user = await require_user(request)
    try:
        await accounts.remove(user, uid)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="No such connection.") from exc
    return {"ok": True}


@app.get("/api/users/{uid}")
async def user_card(uid: str, request: Request) -> dict:
    other = await accounts.get(uid)
    if other is None:
        raise HTTPException(status_code=404, detail="No such account.")
    me_ = await current_user(request)
    status = None
    if me_:
        edge = await store.get_edge(me_.uid, uid)
        status = "you" if me_.uid == uid else (edge or {}).get("status")
    return {"user": other.public(), "status": status}


# ---- admin --------------------------------------------------------------------------------

class CreditGrant(BaseModel):
    amount: int = Field(ge=-100_000, le=100_000)
    note: str = Field(default="", max_length=200)


async def require_admin(request: Request) -> User:
    user = await require_user(request)
    if not user.admin:
        raise HTTPException(status_code=403, detail="Admins only.")
    return user


@app.get("/api/admin/users")
async def admin_users(request: Request, q: str = "") -> dict:
    await require_admin(request)
    users = await store.list_users(limit=1000)
    q = q.strip().lower()
    if q:
        users = [u for u in users if q in u.get("email", "").lower() or q in u.get("name", "").lower()]
    return {"users": [
        {k: u.get(k) for k in ("uid", "email", "name", "photo", "credits", "created_at", "last_seen")}
        for u in users[:200]
    ]}


@app.post("/api/admin/users/{uid}/credits")
async def admin_grant(uid: str, body: CreditGrant, request: Request) -> dict:
    _require_origin(request)
    admin = await require_admin(request)
    if body.amount == 0:
        raise HTTPException(status_code=422, detail="Enter an amount.")
    try:
        balance = await accounts.adjust_credits(uid, body.amount, reason="grant" if body.amount > 0 else "adjust",
                                                by=admin.email, note=body.note)
    except InsufficientCredits as exc:
        raise HTTPException(status_code=422, detail=f"They only have {exc.balance} credits.") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="No such account.") from exc
    return {"credits": balance}


# ---- creating pods --------------------------------------------------------------------------

@app.post("/api/extract")
async def extract(
    request: Request,
    files: list[UploadFile] = File(default=[]),
    url: str = Form(default=""),
) -> dict:
    _require_origin(request)
    await require_user(request)
    caps = ingest_mod.setup_caps(settings.max_source_chars)
    pieces = []
    try:
        for f in files:
            data = await f.read(caps.file_bytes + 1)
            got = await ingest_mod.ingest(
                "file", caps=caps, gemini=gemini_or_none(), filename=f.filename or "file", data=data,
            )
            pieces.append(got)
        if url.strip():
            pieces.append(await ingest_mod.ingest(
                "link", caps=caps, gemini=None, url=url, contact=str(request.base_url),
            ))
    except SourceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"sources": [{"name": p.name, "kind": p.kind, "chars": len(p.text), "text": p.text} for p in pieces]}


@app.post("/api/shows")
async def create_show(body: NewShow, request: Request) -> dict:
    _require_origin(request)
    user = await require_user(request)
    req = body.request
    cost = _cost(req.minutes)
    if user.credits < cost:
        raise HTTPException(status_code=402, detail=(
            f"Creating pods uses credits: this one needs {cost} and you have {user.credits}. "
            "Listening, calling in and connecting are free."
        ))
    source = req.source[: settings.max_source_chars]
    model = _check_capacity() if body.live else None
    show_id = secrets.token_urlsafe(9)
    now = time.time()
    doc = {
        "id": show_id,
        "owner_uid": user.uid,
        "owner_name": user.name,
        "visibility": body.visibility,
        "shared_with": await _clean_share_list(user, body.shared_with),
        "template": body.template if body.template in TEMPLATES_BY_ID else None,
        "album_ids": await _owned_album_ids(user, body.album_ids),
        "tags": _clean_tags(body.tags),
        "status": "live" if body.live else "draft",
        "ended_reason": None,
        "created_at": now,
        "started_at": now if body.live else None,
        "ended_at": None,
        "request": req.model_dump(exclude={"source"}),
        # A typed topic is the main source too; name it by its first line.
        "source": {"chars": len(source), "names": req.source_names or [_working_title(req, source)]},
        "title": _working_title(req, source),
        "tagline": "",
        "about": "",
        "accent": req.accent or accent_for(req.vibe, req.style),
        "vibe": req.vibe,
        "style": req.style,
        "style_label": req.style_label or style_label(req.vibe),
        "style_history": [],
        "hosts": [{"name": h.name, "persona": h.persona, "voice": h.voice, "gender": h.gender} for h in req.hosts],
        "beats": [],
        "minutes": req.minutes,
        "language": req.language,
        "credits_charged": 0,
        "samples": 0,
        "duration_s": 0,
        "audio": None,
        "counts": {},
    }
    if model is not None:
        await _charge(user, cost, show_id)
        doc["credits_charged"] = cost
    try:
        await store.create_show(doc)
        await store.put_text(show_id, "source.txt", source)
    except Exception:
        if doc["credits_charged"]:
            await accounts.adjust_credits(user.uid, cost, reason="refund", show_id=show_id, note="Couldn't start")
        raise
    if model is not None:
        _start_room(doc, req, source, model)
    return {"id": show_id, "trimmed": len(req.source) > settings.max_source_chars,
            "credits": (await accounts.get(user.uid)).credits}  # type: ignore[union-attr]


@app.get("/api/shows")
async def list_shows(request: Request, scope: str = "mine") -> dict:
    user = await current_user(request)
    if scope == "public":
        docs = await store.list_shows(visibility="public", limit=200)
    elif user is None:
        raise HTTPException(status_code=401, detail="Sign in first.")
    elif scope == "shared":
        named = await store.list_shows(shared_with=user.uid, limit=200)
        conns = await accounts.connection_ids(user.uid)
        by_conns = await store.list_shows(owner_in=conns, visibility="connections", limit=200) if conns else []
        docs = list({d["id"]: d for d in named + by_conns if d.get("owner_uid") != user.uid}.values())
    else:
        docs = await store.list_shows(owner_uid=user.uid, limit=500)
    by_id = {d["id"]: d for d in docs}
    for show_id in rooms.ids():  # live docs are fresher than the stored copies
        room = rooms.get(show_id)
        if room and show_id in by_id:
            by_id[show_id] = room.doc
    visible = [d for d in by_id.values() if scope == "mine" or d.get("status") != "draft"]
    visible.sort(key=lambda d: d.get("created_at", 0), reverse=True)
    return {"shows": [_list_item(d) for d in visible]}


@app.get("/api/shows/{show_id}")
async def get_show(show_id: str, request: Request) -> dict:
    doc, room, user = await _viewable(request, show_id)
    role = accounts.role(user, doc)
    share = f"/show/{show_id}"
    extras = await _show_extras(doc, user)
    if room and not room.finalized.is_set():
        return {"show": room.public_show(), "live": True, "role": role, "share_url": share, **extras}
    children = await store.load_children(show_id)
    return {
        "show": _public_doc(doc),
        "live": False,
        "role": role,
        "share_url": share,
        **extras,
        "segments": children["segments"],
        "comments": [c for c in children["comments"] if c.get("status") != "removed"],
        "context": [{k: v for k, v in x.items() if k != "text_path"} for x in children["context"]],
    }


@app.patch("/api/shows/{show_id}")
async def share_show(show_id: str, body: ShareSettings, request: Request) -> dict:
    _require_origin(request)
    user = await require_user(request)
    doc, room = await _show_doc(show_id)
    _owner(doc, user)
    fields = {"visibility": body.visibility, "shared_with": await _clean_share_list(user, body.shared_with)}
    if body.visibility == "public" and doc.get("visibility") != "public":
        fields["published_at"] = time.time()
    doc.update(fields)
    await store.update_show(show_id, fields)
    return {"visibility": doc["visibility"], "shared_with": doc["shared_with"]}


@app.patch("/api/shows/{show_id}/organize")
async def organize_show(show_id: str, body: Organize, request: Request) -> dict:
    _require_origin(request)
    user = await require_user(request)
    doc, _ = await _show_doc(show_id)
    _owner(doc, user)
    fields: dict = {}
    if body.album_ids is not None:
        fields["album_ids"] = await _owned_album_ids(user, body.album_ids)
    if body.tags is not None:
        fields["tags"] = _clean_tags(body.tags)
    if fields:
        doc.update(fields)
        await store.update_show(show_id, fields)
    return {"album_ids": doc.get("album_ids", []), "tags": doc.get("tags", [])}


# ---- albums ---------------------------------------------------------------------------------------

async def _album_or_404(album_id: str, user: User | None) -> dict:
    album = await store.get_album(album_id)
    if not album or not await accounts.can_view(user, album):
        raise HTTPException(status_code=404, detail="No such album, or it isn't shared with you.")
    return album


@app.get("/api/albums")
async def list_my_albums(request: Request) -> dict:
    user = await require_user(request)
    albums = await store.list_albums(owner_uid=user.uid)
    shows = await store.list_shows(owner_uid=user.uid, limit=1000)
    counts: dict[str, int] = {}
    for d in shows:
        for a in d.get("album_ids", []):
            counts[a] = counts.get(a, 0) + 1
    return {"albums": [_album_out(a, counts.get(a["id"], 0)) for a in albums]}


@app.post("/api/albums")
async def create_album(body: AlbumIn, request: Request) -> dict:
    _require_origin(request)
    user = await require_user(request)
    if len(await store.list_albums(owner_uid=user.uid)) >= MAX_ALBUMS:
        raise HTTPException(status_code=422, detail=f"You can keep up to {MAX_ALBUMS} albums.")
    album = {
        **body.model_dump(), "title": body.title.strip(), "description": body.description.strip(),
        "id": secrets.token_urlsafe(8), "owner_uid": user.uid, "owner_name": user.name,
        "shared_with": [], "created_at": time.time(),
    }
    await store.put_album(album)
    return _album_out(album, 0)


@app.get("/api/albums/{album_id}")
async def get_album(album_id: str, request: Request) -> dict:
    user = await current_user(request)
    album = await _album_or_404(album_id, user)
    mine = bool(user and user.uid == album["owner_uid"])
    shows = await _visible(user, await store.list_shows(album_id=album_id, limit=500), drafts=mine)
    subs = await _subscribed_to(user)
    owner = await accounts.get(album["owner_uid"])
    return {
        "album": _album_out(album, len(shows)),
        "mine": mine,
        "owner": owner.public() if owner else {"uid": album["owner_uid"], "name": album.get("owner_name", ""), "photo": ""},
        "shows": [_list_item(d) for d in shows],
        "subscribers": await store.count_subscribers(f"album:{album_id}"),
        "subscribed": ("album", album_id) in subs,
    }


@app.put("/api/albums/{album_id}")
async def update_album(album_id: str, body: AlbumIn, request: Request) -> dict:
    _require_origin(request)
    user = await require_user(request)
    album = await _album_or_404(album_id, user)
    if album["owner_uid"] != user.uid:
        raise HTTPException(status_code=403, detail="Only the album's creator can change it.")
    album.update(body.model_dump(), title=body.title.strip(), description=body.description.strip(),
                 updated_at=time.time())
    await store.put_album(album)
    return _album_out(album)


@app.delete("/api/albums/{album_id}")
async def delete_album(album_id: str, request: Request) -> dict:
    """The album goes; its pods stay, just no longer grouped."""
    _require_origin(request)
    user = await require_user(request)
    album = await _album_or_404(album_id, user)
    if album["owner_uid"] != user.uid and not user.admin:
        raise HTTPException(status_code=403, detail="Only the album's creator can delete it.")
    for d in await store.list_shows(album_id=album_id, limit=2000):
        d = _fresh(d)
        d["album_ids"] = [a for a in d.get("album_ids", []) if a != album_id]
        await store.update_show(d["id"], {"album_ids": d["album_ids"]})
    await store.delete_album(album_id)
    return {"ok": True}


# ---- creators and subscriptions ---------------------------------------------------------------------

@app.get("/api/creators/{uid}")
async def creator_page(uid: str, request: Request) -> dict:
    other = await accounts.get(uid)
    if other is None:
        raise HTTPException(status_code=404, detail="No such creator.")
    viewer = await current_user(request)
    shows = await _visible(viewer, await store.list_shows(owner_uid=uid, limit=300))
    albums = [a for a in await store.list_albums(owner_uid=uid) if await accounts.can_view(viewer, a)]
    counts: dict[str, int] = {}
    for d in shows:
        for a in d.get("album_ids", []):
            counts[a] = counts.get(a, 0) + 1
    subs = await _subscribed_to(viewer)
    return {
        "user": other.public(),
        "you": bool(viewer and viewer.uid == uid),
        "subscribers": await store.count_subscribers(f"creator:{uid}"),
        "subscribed": ("creator", uid) in subs,
        "albums": [_album_out(a, counts.get(a["id"], 0)) for a in albums],
        "shows": [_list_item(d) for d in shows],
    }


class SubscribeIn(BaseModel):
    kind: Literal["creator", "album"]
    id: str = Field(min_length=1, max_length=128)


@app.get("/api/subscriptions")
async def list_subscriptions(request: Request) -> dict:
    user = await require_user(request)
    out = []
    for s_ in await store.list_subscriptions(user.uid):
        if s_["kind"] == "creator":
            other = await accounts.get(s_["target_id"])
            if other:
                out.append({"kind": "creator", "id": other.uid, "title": other.name, "photo": other.photo, "at": s_["at"]})
        else:
            album = await store.get_album(s_["target_id"])
            if album and await accounts.can_view(user, album):
                out.append({"kind": "album", "id": album["id"], "title": album["title"], "accent": album.get("accent"),
                            "by": album.get("owner_name", ""), "at": s_["at"]})
    return {"subscriptions": out}


@app.post("/api/subscriptions")
async def subscribe(body: SubscribeIn, request: Request) -> dict:
    _require_origin(request)
    user = await require_user(request)
    if body.kind == "creator":
        if body.id == user.uid:
            raise HTTPException(status_code=422, detail="That's you.")
        if await accounts.get(body.id) is None:
            raise HTTPException(status_code=404, detail="No such creator.")
    else:
        album = await _album_or_404(body.id, user)
        if album["owner_uid"] == user.uid:
            raise HTTPException(status_code=422, detail="That's your own album.")
    target = f"{body.kind}:{body.id}"
    await store.put_subscription({
        "id": _sub_id(user.uid, body.kind, body.id), "subscriber_uid": user.uid, "kind": body.kind,
        "target_id": body.id, "target": target, "at": time.time(),
    })
    return {"subscribed": True, "subscribers": await store.count_subscribers(target)}


@app.delete("/api/subscriptions/{kind}/{target_id}")
async def unsubscribe(kind: str, target_id: str, request: Request) -> dict:
    _require_origin(request)
    user = await require_user(request)
    if kind not in {"creator", "album"}:
        raise HTTPException(status_code=404, detail="Unknown subscription.")
    try:
        await store.delete_subscription(_sub_id(user.uid, kind, target_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Unknown subscription.") from exc
    return {"subscribed": False, "subscribers": await store.count_subscribers(f"{kind}:{target_id}")}


@app.get("/api/feed")
async def feed(request: Request) -> dict:
    """Pods from the creators and albums you subscribe to that you're allowed to hear,
    newest first; "new" means since you last opened the feed."""
    user = await require_user(request)
    subs = await store.list_subscriptions(user.uid)
    seen = float((await store.get_user(user.uid) or {}).get("feed_seen_at") or 0)
    found: dict[str, tuple[dict, str]] = {}
    for s_ in subs:
        if s_["kind"] == "creator":
            other = await accounts.get(s_["target_id"])
            docs, via = await store.list_shows(owner_uid=s_["target_id"], limit=100), other.name if other else ""
        else:
            album = await store.get_album(s_["target_id"])
            if not album or not await accounts.can_view(user, album):
                continue
            docs, via = await store.list_shows(album_id=album["id"], limit=100), album["title"]
        for d in await _visible(user, [d for d in docs if d["id"] not in found and d.get("owner_uid") != user.uid]):
            found[d["id"]] = (d, via)
    items = sorted(found.values(), key=lambda x: x[0].get("created_at", 0), reverse=True)[:200]
    shows = [{**_list_item(d), "via": via, "new": float(d.get("created_at") or 0) > seen} for d, via in items]
    return {"shows": shows, "unseen": sum(s_["new"] for s_ in shows), "subscriptions": len(subs)}


@app.post("/api/feed/seen")
async def feed_seen(request: Request) -> dict:
    _require_origin(request)
    user = await require_user(request)
    await store.update_user(user.uid, {"feed_seen_at": time.time()})
    return {"ok": True}


# ---- stats ------------------------------------------------------------------------------------------

@app.get("/api/shows/{show_id}/stats")
async def show_stats(show_id: str, request: Request) -> dict:
    user = await require_user(request)
    doc, room = await _show_doc(show_id)
    if doc.get("owner_uid") != user.uid and not user.admin:
        raise HTTPException(status_code=403, detail="Only the pod's creator can see its stats.")
    sessions = await store.list_sessions(show_id)
    live = bool(room and not room.finalized.is_set())
    if live:
        sessions += room.open_sessions()  # type: ignore[union-attr]
    comments = len(room.comments) if live else int((doc.get("counts") or {}).get("comments", 0))  # type: ignore[union-attr]
    summary = stats_mod.summarize(
        doc, sessions, comments=comments,
        live_now=room.listening_now() if live else None,  # type: ignore[union-attr]
        timeline=room.listener_timeline if live else None,  # type: ignore[union-attr]
        subscribers=await store.count_subscribers(f"creator:{user.uid}"),
    )
    if live:
        summary["engagement"]["hearts"] = room.hearts  # type: ignore[union-attr]
    return {"show": {"id": doc["id"], "title": doc.get("title", ""), "accent": doc.get("accent"),
                     "status": "live" if live else doc.get("status"), "created_at": doc.get("created_at")},
            "live": live, "stats": summary}


@app.get("/api/me/stats")
async def my_stats(request: Request) -> dict:
    user = await require_user(request)
    docs = [_fresh(d) for d in await store.list_shows(owner_uid=user.uid, limit=60) if d.get("status") != "draft"]
    all_sessions = await asyncio.gather(*(store.list_sessions(d["id"]) for d in docs))
    rows = [stats_mod.overview_row(d, stats_mod.summarize(d, ss)) for d, ss in zip(docs, all_sessions)]
    albums = await store.list_albums(owner_uid=user.uid)
    album_subs = await asyncio.gather(*(store.count_subscribers(f"album:{a['id']}") for a in albums))
    return {
        "pods": rows,
        "totals": {
            "pods": len(rows),
            "listeners": sum(r["listeners"] for r in rows),
            "replays": sum(r["replays"] for r in rows),
            "listened_s": round(sum(r["listened_s"] for r in rows), 1),
        },
        "subscribers": {
            "creator": await store.count_subscribers(f"creator:{user.uid}"),
            "albums": [{"id": a["id"], "title": a["title"], "subscribers": n} for a, n in zip(albums, album_subs)],
        },
    }


@app.post("/api/shows/{show_id}/listen")
async def replay_progress(show_id: str, request: Request) -> dict:
    """The replay player reports how far it got (also from sendBeacon on page exit).
    Live listening is counted by the socket instead."""
    _require_origin(request)
    try:
        body = json.loads(await request.body() or b"{}")
        sid = _token(body.get("sid"))
        pos = max(0.0, float(body.get("pos") or 0))
        played = max(0.0, float(body.get("played") or 0))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="Bad progress report.") from exc
    if not sid:
        raise HTTPException(status_code=422, detail="Bad progress report.")
    if not LISTEN_LIMIT.take(sid):
        return {"ok": False}
    doc, room, user = await _viewable(request, show_id)
    if (room and not room.finalized.is_set()) or (user and user.uid == doc.get("owner_uid")):
        return {"ok": True}
    now = time.time()
    session_id = f"r-{sid}"
    before = await store.get_session(show_id, session_id) or {}
    started = float(before.get("joined_at") or now)
    duration = float(doc.get("duration_s") or 0)
    pos = min(pos, duration) if duration else pos
    furthest = max(pos, float(before.get("leave_pos") or 0))
    await store.put_session(show_id, {
        "id": session_id, "kind": "replay", "uid": user.uid if user else None,
        "name": user.name if user else None, "lid": _token(body.get("lid")), "role": accounts.role(user, doc),
        "joined_at": started, "left_at": now, "join_pos": before.get("join_pos", round(pos, 1)),
        "leave_pos": round(furthest, 1),
        # Never more listening than time has passed since they pressed play.
        "listened_s": round(max(float(before.get("listened_s") or 0), min(played, now - started + 10)), 1),
        "completed": bool(before.get("completed")) or bool(body.get("ended")) or (duration > 0 and furthest >= duration - 5),
    })
    return {"ok": True}


@app.post("/api/shows/{show_id}/start")
async def start_draft(show_id: str, request: Request) -> dict:
    _require_origin(request)
    user = await require_user(request)
    doc, room = await _show_doc(show_id)
    _owner(doc, user)
    if room or doc.get("status") != "draft":
        raise HTTPException(status_code=409, detail="Only drafts can be started.")
    source = await store.get_text(show_id, "source.txt") or ""
    try:
        req = ShowRequest(source=source, **doc["request"])
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="This draft can't be started: its source is missing.") from exc
    model = _check_capacity()
    cost = _cost(req.minutes)
    await _charge(user, cost, show_id)
    now = time.time()
    fields = {"status": "live", "started_at": now, "created_at": now, "credits_charged": cost}
    doc.update(fields)
    await store.update_show(show_id, fields)
    _start_room(doc, req, source, model)
    return {"id": show_id}


@app.delete("/api/shows/{show_id}")
async def delete_show(show_id: str, request: Request) -> dict:
    _require_origin(request)
    user = await require_user(request)
    doc, room = await _show_doc(show_id)
    if doc.get("owner_uid") != user.uid and not user.admin:
        raise HTTPException(status_code=403, detail="Only the pod's creator can delete it.")
    if room:
        room.end_now("stopped")
        try:
            await asyncio.wait_for(room.finalized.wait(), timeout=10)
        except asyncio.TimeoutError:
            pass
        room.close()
    await store.delete_show(show_id)
    return {"ok": True}


# ---- during a pod -------------------------------------------------------------------------

@app.post("/api/shows/{show_id}/context")
async def add_context(
    show_id: str,
    request: Request,
    kind: str = Form(...),
    when: str = Form(default="now"),
    cite: bool = Form(default=True),
    name: str = Form(default=""),
    text: str = Form(default=""),
    url: str = Form(default=""),
    file: UploadFile | None = File(default=None),
    comment: str = Form(default=""),
    client_id: str = Form(default=""),
) -> dict:
    _require_origin(request)
    doc, room, user = await _viewable(request, show_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in to add to the pod.")
    if not room or room.ended_reason:
        raise HTTPException(status_code=409, detail="This pod isn't live any more.")
    role = accounts.role(user, doc)
    producer = role == "producer"
    limiter = PRODUCER_CONTEXT if producer else GUEST_CONTEXT
    if not limiter.take(f"{show_id}:{user.uid}"):
        raise HTTPException(status_code=429, detail="One thing at a time. Give the hosts a minute with that.")
    if when not in {"now", "next", "background"} or kind not in {"text", "file", "link", "photo"}:
        raise HTTPException(status_code=422, detail="Unknown kind of context.")
    caps = ingest_mod.PRODUCER if producer else ingest_mod.GUEST
    data = await file.read(max(caps.file_bytes, caps.photo_bytes) + 1) if file else b""
    try:
        got = await ingest_mod.ingest(
            kind, caps=caps, gemini=gemini_or_none(), text=text, url=url, name=name,
            filename=(file.filename if file else "") or "", data=data, contact=str(request.base_url),
        )
        item = ContextItem(
            kind=got.kind, name=got.name, text=got.text, when=when, cite=cite,  # type: ignore[arg-type]
            added_by=user.name, from_creator=producer,
        )
        item, posted = room.add_context(item, role, comment=comment, caller=user.name, client_id=client_id)
    except SourceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await store.put_text(show_id, f"context/{item.id}.txt", item.text)
    return {"context": item.public(), "comment": posted}


@app.get("/api/shows/{show_id}/audio")
async def show_audio(show_id: str, request: Request, download: bool = False) -> Response:
    doc, room, _ = await _viewable(request, show_id)
    filename = f"{_slug(doc.get('title', ''))}.mp3"
    if room and not room.finalized.is_set():
        disposition = "attachment" if download else "inline"
        return Response(
            room.mp3.snapshot(),
            media_type="audio/mpeg",
            headers={"Content-Disposition": f'{disposition}; filename="{filename}"', "Cache-Control": "no-store"},
        )
    return await store.audio_response(show_id, doc, download, filename)


# ---- the live socket -------------------------------------------------------------------------

@app.websocket("/ws/show/{show_id}")
async def show_socket(ws: WebSocket, show_id: str) -> None:
    await ws.accept()
    if not same_origin(ws.headers):
        return await ws.close(4403)
    room = rooms.get(show_id)
    if room is None or room.finalized.is_set():
        exists = await store.get_show(show_id)
        return await ws.close(4409 if exists else 4404)

    user = await current_user(ws)
    if not await accounts.can_view(user, room.doc):
        return await ws.close(4401 if user is None else 4403)
    role = accounts.role(user, room.doc)

    ip = client_ip(ws.headers, ws.client.host if ws.client else None)
    if len(room.conns) >= settings.max_listeners or room.conns_from(ip) >= settings.max_conns_per_ip:
        return await ws.close(4429)

    try:
        first = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=10))
    except (asyncio.TimeoutError, WebSocketDisconnect, ValueError, KeyError, RuntimeError):
        return
    if not isinstance(first, dict) or first.get("type") != "join":
        return await ws.close(4400)

    def sample(v) -> int | None:
        return int(v) if isinstance(v, (int, float)) and v >= 0 else None

    conn = Conn(ws, role, user.name if user else "Visitor", ip, uid=user.uid if user else None,
                lid=_token(first.get("lid")))
    room.join(conn, sample(first.get("have_from")), sample(first.get("have_to")))
    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            raw = message.get("text")
            if raw is None:
                continue
            if len(raw) > WS_MESSAGE_BYTES:
                conn.error("too_large", "That message is too long.")
                continue
            if not conn.messages.take():
                conn.close(4429)
                break
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if isinstance(msg, dict):
                room.handle(conn, msg)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        room.leave(conn)
