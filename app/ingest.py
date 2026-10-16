"""Turn anything someone hands the hosts into text: typed notes, files, links, photos.

Used by the Create screen (the main source) and by Add context mid-show. Guests
holding a share link get tighter caps than the producer. Photos are described
once by Gemini and only the description is kept.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .llm import Gemini
from .sources import IMAGE_EXTENSIONS, SourceError, file_to_text, sniff_image, url_to_text

log = logging.getLogger("podcasts.ingest")

MB = 1024 * 1024


@dataclass(frozen=True)
class Caps:
    text_chars: int
    file_bytes: int
    file_chars: int
    photo_bytes: int = 7 * MB


GUEST = Caps(text_chars=4_000, file_bytes=5 * MB, file_chars=15_000)
# Added context sits after the source in the writer's instructions (prompts.source_block),
# so once a segment has read it, every later segment gets it from Gemini's cache.
PRODUCER = Caps(text_chars=100_000, file_bytes=25 * MB, file_chars=100_000)


def setup_caps(max_source_chars: int) -> Caps:
    """The main source on the Create screen may be as long as the whole show allows."""
    return Caps(text_chars=max_source_chars, file_bytes=25 * MB, file_chars=max_source_chars)


@dataclass
class Ingested:
    kind: str     # text | file | link | photo
    name: str
    text: str


def _looks_like_image(filename: str) -> bool:
    return any(filename.lower().endswith(ext) for ext in IMAGE_EXTENSIONS)


def _title_from_text(text: str) -> str:
    first = text.strip().splitlines()[0].strip()
    return first if len(first) <= 48 else first[:47].rstrip() + "…"


async def ingest(
    kind: str,
    *,
    caps: Caps,
    gemini: Gemini | None,
    text: str = "",
    filename: str = "",
    data: bytes = b"",
    url: str = "",
    name: str = "",
    contact: str = "",
) -> Ingested:
    if kind == "text":
        text = text.strip()
        if not text:
            raise SourceError("Write something for the hosts first.")
        if len(text) > caps.text_chars:
            raise SourceError(f"Keep notes under {caps.text_chars:,} characters.")
        return Ingested("text", name.strip()[:80] or _title_from_text(text), text)

    if kind in {"file", "photo"}:
        if not data:
            raise SourceError("That file is empty.")
        mime = sniff_image(data)
        if mime or kind == "photo" or _looks_like_image(filename):
            if not mime:
                raise SourceError("Photos need to be PNG, JPEG, WebP or HEIC.")
            if len(data) > caps.photo_bytes:
                raise SourceError(f"Photos need to be under {caps.photo_bytes // MB} MB.")
            if gemini is None:
                raise SourceError("Photos need GEMINI_API_KEY to be set.")
            label = filename or "Photo"
            try:
                description = await gemini.describe_image(data, mime, label)
            except Exception as exc:
                log.warning("couldn't describe %s: %s", label, exc)
                raise SourceError("The hosts couldn't make out that photo. Try another one.") from exc
            return Ingested("photo", label, description)
        if len(data) > caps.file_bytes:
            raise SourceError(f"{filename or 'That file'} is over {caps.file_bytes // MB} MB.")
        extracted = file_to_text(filename or "file", data)
        return Ingested("file", filename or "File", _cap(extracted, caps.file_chars))

    if kind == "link":
        if not url.strip():
            raise SourceError("Paste a link first.")
        title, extracted = await url_to_text(url, contact=contact)
        if not extracted.strip():
            raise SourceError("That page had no readable text.")
        return Ingested("link", title.strip()[:120] or url, _cap(extracted, caps.file_chars))

    raise SourceError(f"Unknown kind of context: {kind}")


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n[…trimmed]"
