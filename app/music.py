"""Music for voice-memo pods: the prepackaged library, and turning a theme into cues.

The library is music/manifest.json plus one theme per pack, built once with
tools/build_music_library.py and shipped with the app, so a pod's music costs
nothing and adds no wait. Music generated for a pod (Lyria, for extra credits)
is a theme too, and both become cues the same way.

A theme is about 30 seconds. The mixer (app/mixer.py) takes three cues from it:
the intro (its opening), a sting between chapters (its punchiest few seconds)
and the outro (its ending). A manifest pack can pin those windows by hand.

    {"version": 1, "packs": [{"id": "warm-acoustic", "label": "Warm acoustic", "mood": "warm",
      "also": ["calm"], "file": "warm-acoustic.mp3", "seconds": 30,
      "origin": "lyria-3-clip-preview", "license": "Generated with Lyria",
      "cues": {"sting": [17.5, 20.0]}}]}
"""
from __future__ import annotations

import asyncio
import json
import logging
import zlib
from dataclasses import dataclass
from pathlib import Path

import miniaudio
import numpy as np

from .mixer import (
    INTRO_LEAD_S, INTRO_UNDER_S, MUSIC_RMS_DBFS, OUTRO_PLAYOUT_S, OUTRO_UNDER_S, STING_S, Cues, active_rms, from_db,
)

log = logging.getLogger("podcasts.music")

MANIFEST = "manifest.json"
STING_SEARCH_FROM_S = 8.0   # the sting comes from after the theme's opening bars

# What each of catalog.MUSIC_MOODS sounds like: a label for the picker, and the
# sound for a Lyria prompt. Prompts are built from these only, never from anything
# a user typed (artist names and the like get a generation blocked).
MOOD_MUSIC = {
    "upbeat": ("Bright pop", "bright, upbeat pop with punchy drums, clean electric guitar and synth stabs, about 118 BPM"),
    "warm": ("Warm acoustic", "warm acoustic folk with fingerpicked guitar, soft piano and light percussion, about 96 BPM"),
    "reflective": ("Reflective piano", "reflective ambient piano with soft strings and gentle pads, about 72 BPM"),
    "dramatic": ("Cinematic", "cinematic and suspenseful, pulsing low strings, deep drum hits and rising brass, about 100 BPM"),
    "playful": ("Playful", "playful and quirky, pizzicato strings, marimba and handclaps, about 112 BPM"),
    "calm": ("Lo-fi calm", "calm lo-fi hip hop with mellow electric piano, vinyl crackle and soft brushed drums, about 80 BPM"),
    "tech": ("Electronic", "modern electronic with arpeggiated synths, crisp beats and a driving bassline, about 122 BPM"),
    "newsy": ("Newsroom", "a confident news-magazine theme with tight drums, bold bass and bright brass hits, about 110 BPM"),
}


def music_prompt(mood: str) -> str:
    """A Lyria prompt for a 30-second theme in this mood."""
    _, sound = MOOD_MUSIC.get(mood, MOOD_MUSIC["warm"])
    return (f"Instrumental only, no vocals. A 30-second theme for a talk podcast: {sound}. "
            "[0:00-0:08] Intro: establish the main motif. [0:08-0:22] The hook, at full energy. "
            "[0:22-0:30] Outro: resolve and end cleanly, no fade to silence.")


def decode(data: bytes, rate: int) -> np.ndarray:
    """An MP3, WAV, FLAC or Ogg Vorbis file as mono float32 at `rate`."""
    sound = miniaudio.decode(data, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=rate)
    return np.frombuffer(sound.samples, dtype=np.int16).astype(np.float32) / 32768


def _punchiest(theme: np.ndarray, rate: int, seconds: float) -> np.ndarray:
    """The loudest stretch of `seconds` after the opening bars."""
    length = int(seconds * rate)
    if len(theme) <= length:
        return theme
    energy = np.concatenate([[0.0], np.cumsum(theme.astype(np.float64) ** 2)])
    first = min(int(STING_SEARCH_FROM_S * rate), len(theme) - length)
    starts = np.arange(first, len(theme) - length + 1, max(1, rate // 4))
    best = int(starts[np.argmax(energy[starts + length] - energy[starts])])
    return theme[best:best + length]


def cues_from_theme(theme: np.ndarray, rate: int, windows: dict | None = None) -> Cues:
    """Intro, sting and outro from one theme (mono float32 at `rate`), levelled.
    windows: {"intro" | "sting" | "outro": [start_s, end_s]} overrides any of them."""
    rms = active_rms(theme, rate)
    if rms:
        theme = theme * np.float32(from_db(MUSIC_RMS_DBFS) / rms)
    windows = windows or {}

    def window(name: str, default: np.ndarray) -> np.ndarray | None:
        if name in windows:
            start, end = windows[name]
            default = theme[int(start * rate):int(end * rate)]
        return default if len(default) else None

    return Cues(
        intro=window("intro", theme[:int((INTRO_LEAD_S + INTRO_UNDER_S) * rate)]),
        sting=window("sting", _punchiest(theme, rate, STING_S)),
        outro=window("outro", theme[max(0, len(theme) - int((OUTRO_UNDER_S + OUTRO_PLAYOUT_S) * rate)):]),
    )


@dataclass
class Pack:
    id: str
    label: str
    mood: str
    file: str
    also: tuple[str, ...] = ()       # other moods it suits
    seconds: float = 0.0
    origin: str = ""                 # lyria-3-clip-preview, or where the file came from
    license: str = ""
    cues: dict | None = None         # hand-picked cue windows, in seconds

    def public(self) -> dict:
        return {"id": self.id, "label": self.label, "mood": self.mood}


class Library:
    def __init__(self, root: Path, packs: list[Pack]):
        self.root = root
        self.packs = packs
        self._cues: dict[tuple[str, int], Cues] = {}

    @classmethod
    def load(cls, root: str | Path) -> Library:
        """The library in `root`. Missing or broken, it's empty, and pods are voice only."""
        root = Path(root)
        path = root / MANIFEST
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            log.info("no music library at %s; memo pods will have no music", path)
            return cls(root, [])
        except (OSError, ValueError) as exc:
            log.error("music library %s can't be read (%s); memo pods will have no music", path, exc)
            return cls(root, [])
        packs = []
        for p in data.get("packs", []):
            try:
                pack = Pack(
                    id=str(p["id"]), label=str(p.get("label") or p["id"]), mood=str(p["mood"]),
                    file=str(p["file"]), also=tuple(p.get("also", [])), seconds=float(p.get("seconds", 0)),
                    origin=str(p.get("origin", "")), license=str(p.get("license", "")), cues=p.get("cues"),
                )
            except (KeyError, TypeError, ValueError):
                log.warning("skipping a malformed pack in %s: %r", path, p)
                continue
            if Path(pack.file).name != pack.file or not (root / pack.file).is_file():
                log.warning("music pack %s: %s is missing", pack.id, pack.file)
                continue
            packs.append(pack)
        return cls(root, packs)

    @property
    def available(self) -> bool:
        return bool(self.packs)

    def get(self, pack_id: str) -> Pack | None:
        return next((p for p in self.packs if p.id == pack_id), None)

    def pick(self, mood: str, seed: str = "") -> Pack | None:
        """A pack for the mood, else one that also suits it, else any. The seed
        (a pod id) spreads pods over packs that tie, and always picks the same one."""
        options = ([p for p in self.packs if p.mood == mood] or [p for p in self.packs if mood in p.also]
                   or self.packs)
        return options[zlib.crc32(seed.encode()) % len(options)] if options else None

    def cues(self, pack: Pack, rate: int) -> Cues:
        """The pack's cues, decoded once. Call it in a thread: the first time decodes an MP3."""
        key = (pack.id, rate)
        if key not in self._cues:
            theme = decode((self.root / pack.file).read_bytes(), rate)
            self._cues[key] = cues_from_theme(theme, rate, pack.cues)
        return self._cues[key]


# Music made for a user (Lyria, on a memo pod) is kept as one of their items, so it
# can be their show's theme or reused on later pods without being made again.
MINE = "mine:"


async def theme_for(choice: str, *, library: Library, store, uid: str, mood: str, seed: str,
                    rate: int) -> tuple[Cues | None, dict]:
    """The cues a pod's music comes from, and a note on it for the pod's page.

    choice: "none", "mine:<id>" (music made for this user), a library pack id, or
    anything else ("auto"): a library pack for the mood. Music that can't be had
    falls back to the library, and an empty library means no music."""
    if choice == "none":
        return None, {"source": "none"}
    if choice.startswith(MINE):
        item = next((i for i in await store.list_items(uid, "music") if i.get("id") == choice[len(MINE):]), None)
        data = await store.get_user_file(uid, item["file"]) if item else None
        if data:
            cues = await asyncio.to_thread(lambda: cues_from_theme(decode(data, rate), rate))
            return cues, {"source": "mine", "track_id": item["id"], "label": item.get("label") or "Your theme"}
        log.warning("music %s of user %s is gone; using the library", choice, uid)
    pack = library.get(choice) or library.pick(mood, seed)
    if pack is None:
        return None, {"source": "none"}
    cues = await asyncio.to_thread(library.cues, pack, rate)
    return cues, {"source": "library", "pack_id": pack.id, "label": pack.label, "license": pack.license}
