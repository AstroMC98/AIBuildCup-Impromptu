"""Build the prepackaged music library: music/manifest.json and one theme per pack.

    python tools/build_music_library.py --lyria [--moods warm calm] [--dry-run]
    python tools/build_music_library.py --from-files DIR --mood warm --license "Pixabay Content License"

--lyria generates one 30-second theme per mood with MUSIC_MODEL (Lyria; about
$0.04 a theme, billed to GEMINI_API_KEY from .env). --from-files turns every
audio file in DIR (MP3, WAV, FLAC or Ogg Vorbis) into a pack in that mood. Only
add music you're allowed to publish: the license is kept with each pack.

Each theme is stored as a 24 kHz mono MP3: the rate and channel count pods are
mixed at, so nothing audible is lost and a pack is about 0.4 MB. A pack with the
same id is replaced. The server reads the library at startup; commit music/.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.catalog import MUSIC_MOODS  # noqa: E402
from app.mixer import encode_mp3  # noqa: E402
from app.music import MANIFEST, MOOD_MUSIC, decode, music_prompt  # noqa: E402

RATE = 24_000
KBPS = 96
FADE_IN_S, FADE_OUT_S = 0.05, 2.0
AUDIO_FILES = {".mp3", ".wav", ".flac", ".ogg"}


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "theme"


def theme_pcm(samples: np.ndarray, seconds: float) -> bytes:
    """The first `seconds` of mono float32 audio at RATE, faded in and out, as s16le."""
    x = samples[: int(seconds * RATE)].copy()
    fade_in, fade_out = min(len(x), int(FADE_IN_S * RATE)), min(len(x), int(FADE_OUT_S * RATE))
    x[:fade_in] *= np.linspace(0, 1, fade_in, dtype=np.float32)
    x[len(x) - fade_out:] *= np.linspace(1, 0, fade_out, dtype=np.float32)
    return np.rint(np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


def load_manifest(music_dir: Path) -> dict:
    path = music_dir / MANIFEST
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"version": 1, "packs": []}


def save_manifest(music_dir: Path, manifest: dict) -> None:
    manifest["packs"].sort(key=lambda p: (p["mood"], p["id"]))
    (music_dir / MANIFEST).write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def add_pack(manifest: dict, pack: dict) -> None:
    manifest["packs"] = [p for p in manifest["packs"] if p["id"] != pack["id"]] + [pack]


def from_files(args: argparse.Namespace, music_dir: Path) -> None:
    files = sorted(p for p in Path(args.from_files).iterdir() if p.suffix.lower() in AUDIO_FILES)
    if not files:
        sys.exit(f"No audio files ({', '.join(sorted(AUDIO_FILES))}) in {args.from_files}")
    manifest = load_manifest(music_dir)
    for n, path in enumerate(files, start=1):
        if args.label:
            label = args.label if len(files) == 1 else f"{args.label} {n}"
        else:
            label = re.sub(r"[_-]+", " ", path.stem).strip().title()
        label = label[:40]
        pack_id = slug(label) if slug(label).startswith(args.mood) else f"{args.mood}-{slug(label)}"
        print(f"{path.name} -> {pack_id}.mp3  ({label}, {args.mood})")
        if args.dry_run:
            continue
        pcm = theme_pcm(decode(path.read_bytes(), RATE), args.seconds)
        (music_dir / f"{pack_id}.mp3").write_bytes(encode_mp3(pcm, RATE, KBPS))
        add_pack(manifest, {
            "id": pack_id, "label": label, "mood": args.mood, "also": args.also, "file": f"{pack_id}.mp3",
            "seconds": round(len(pcm) / 2 / RATE, 2), "origin": path.name, "license": args.license,
        })
    if not args.dry_run:
        save_manifest(music_dir, manifest)
        print(f"{len(manifest['packs'])} packs in {music_dir / MANIFEST}")


def with_lyria(args: argparse.Namespace, music_dir: Path) -> None:
    from app.config import settings
    from app.llm import Gemini

    moods = args.moods or MUSIC_MOODS
    print(f"{len(moods)} themes from {settings.music_model} (about $0.04 each): {', '.join(moods)}")
    if args.dry_run:
        for mood in moods:
            print(f"  {mood}-lyria.mp3  {music_prompt(mood)}")
        return
    if not settings.music_model:
        sys.exit("MUSIC_MODEL is empty: set it in .env.")
    gemini = Gemini(settings)
    limit = asyncio.Semaphore(3)
    manifest = load_manifest(music_dir)

    async def make(mood: str) -> None:
        async with limit:
            try:
                data, _ = await gemini.generate_music(music_prompt(mood))
            except Exception as exc:
                print(f"  {mood}: failed ({exc})")
                return
        pcm = theme_pcm(decode(data, RATE), args.seconds)
        (music_dir / f"{mood}-lyria.mp3").write_bytes(encode_mp3(pcm, RATE, KBPS))
        add_pack(manifest, {
            "id": f"{mood}-lyria", "label": MOOD_MUSIC[mood][0], "mood": mood, "also": [], "file": f"{mood}-lyria.mp3",
            "seconds": round(len(pcm) / 2 / RATE, 2), "origin": settings.music_model,
            "license": "Generated with Lyria; carries a SynthID watermark", "prompt": music_prompt(mood),
        })
        print(f"  {mood}-lyria.mp3  {len(pcm) / 2 / RATE:.1f} s")

    async def run() -> None:
        await asyncio.gather(*(make(m) for m in moods))

    asyncio.run(run())
    save_manifest(music_dir, manifest)
    print(f"{len(manifest['packs'])} packs in {music_dir / MANIFEST}. Listen to the new ones before committing.")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--lyria", action="store_true", help="generate a theme per mood (costs API credits)")
    source.add_argument("--from-files", metavar="DIR", help="a folder of themes to add")
    p.add_argument("--moods", nargs="*", choices=MUSIC_MOODS, help="with --lyria: only these moods")
    p.add_argument("--mood", choices=MUSIC_MOODS, help="with --from-files: the files' mood")
    p.add_argument("--also", nargs="*", choices=MUSIC_MOODS, default=[], help="other moods they suit")
    p.add_argument("--license", help="with --from-files: the terms the music is used under")
    p.add_argument("--label", help="the name in the picker (default: from the file name)")
    p.add_argument("--seconds", type=float, default=30.0, help="how much of each theme to keep")
    p.add_argument("--music-dir", default=str(ROOT / "music"))
    p.add_argument("--dry-run", action="store_true", help="show what would be added, change nothing")
    args = p.parse_args()
    if args.from_files and not (args.mood and args.license):
        p.error("--from-files needs --mood and --license")
    music_dir = Path(args.music_dir)
    music_dir.mkdir(parents=True, exist_ok=True)
    if args.lyria:
        with_lyria(args, music_dir)
    else:
        from_files(args, music_dir)


if __name__ == "__main__":
    main()
