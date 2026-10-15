"""Step 0 for voice-memo pods: check the real Gemini API does what they rely on.

    python tools/probe_memo_apis.py --memo chrome.webm iphone.mp4 voicememo.m4a [--music] [--tts]

Spends real API credits (a few cents; Lyria about $0.04). Uses GEMINI_API_KEY
from .env and refuses to run against a GEMINI_BASE_URL stub. It checks:

  memos      each file's sniffed type, whether Gemini accepts it, and the brief
  research   Google Search with a JSON answer in one call (or why not), then the
             two-call way; what was searched, the cited pages (redirect links?)
             and the Search Suggestions
  --music    one Lyria theme: how long it takes, its type and length (saved as
             theme.mp3 to listen to: vocals? a clean ending?)
  --tts      how long voicing ~40 s of dialogue takes, i.e. how much faster than
             real time a render runs (this sets MAX_RENDERS)

Raw responses are saved as JSON under --out (default data/probe/<time>/), to set
against tests/fake_gemini.py, with a report.md of the findings.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import prompts  # noqa: E402
from app.config import settings  # noqa: E402
from app.llm import GOOGLE_SEARCH, Gemini, _dossier, _parse_json, _tidy_brief, grounding  # noqa: E402
from app.models import Host, Line, MemoBrief  # noqa: E402
from app.music import decode, music_prompt  # noqa: E402
from app.sources import sniff_audio  # noqa: E402

report: list[str] = []


def say(line: str = "") -> None:
    print(line)
    report.append(line)


def save(out: Path, name: str, interaction) -> None:
    raw = json.loads(interaction.model_dump_json())
    for step in raw.get("steps") or []:            # keep audio out of the fixtures
        for item in step.get("content") or []:
            if item.get("type") == "audio" and item.get("data"):
                item["data"] = f"<{len(item['data'])} base64 chars>"
    (out / f"{name}.json").write_text(json.dumps(raw, indent=1, ensure_ascii=False), encoding="utf-8")


async def probe_memo(g: Gemini, path: Path, out: Path) -> MemoBrief | None:
    data = path.read_bytes()
    mime = sniff_audio(data)
    say(f"### {path.name}: {len(data) / 1e6:.2f} MB, sniffed as {mime or 'not recognised'}")
    if not mime:
        return None
    started = time.monotonic()
    try:
        interaction = await g._create(
            system=prompts.memo_system(settings.max_research_queries),
            user=[{"type": "text", "text": prompts.memo_input("the creator", None)},
                  {"type": "audio", "data": base64.b64encode(data).decode(), "mime_type": mime}],
            schema=prompts.memo_schema(settings.max_research_queries),
            model=settings.memo_model or None,
        )
    except Exception as exc:
        say(f"- REFUSED after {time.monotonic() - started:.1f}s: {type(exc).__name__} "
            f"{getattr(exc, 'status_code', '')} {exc}")
        return None
    save(out, f"memo-{path.stem}", interaction)
    brief = _tidy_brief(MemoBrief.model_validate(_parse_json(interaction.output_text)), settings.max_research_queries)
    say(f"- accepted in {time.monotonic() - started:.1f}s; usable={brief.usable} {brief.reason}")
    say(f"- title: {brief.working_title} | format: {brief.format} | music: {brief.music_mood} | tone: {brief.tone}")
    say(f"- transcript: {brief.transcript[:300]}{'...' if len(brief.transcript) > 300 else ''}")
    say(f"- queries: {brief.research_queries}")
    return brief


async def probe_research(g: Gemini, brief: MemoBrief, out: Path) -> None:
    say("### Research")
    n = settings.max_research_queries
    started = time.monotonic()
    try:
        one = await g._create(system=prompts.memo_research_system(n), user=prompts.memo_research_input(brief),
                              schema=prompts.memo_research_schema(), tools=GOOGLE_SEARCH)
        save(out, "research-one-call", one)
        d = _dossier(_parse_json(one.output_text), one, time.time())
        say(f"- one call (search + JSON) WORKS: {time.monotonic() - started:.1f}s, {len(d.findings)} findings")
    except Exception as exc:
        say(f"- one call (search + JSON) refused: {getattr(exc, 'status_code', '')} {exc}")
    started = time.monotonic()
    notes = await g._create(system=prompts.memo_research_system(n, notes=True),
                            user=prompts.memo_research_input(brief), tools=GOOGLE_SEARCH)
    save(out, "research-notes", notes)
    queries, sources, suggestions = grounding(notes.model_dump(mode="json"))
    say(f"- two calls, the search half: {time.monotonic() - started:.1f}s")
    say(f"- searched: {queries}")
    for s in sources[:8]:
        say(f"- cites: {s['domain']} | {s['title']} | {s['url'][:90]}")
    redirects = sum("vertexaisearch" in s["url"] for s in sources)
    say(f"- {len(sources)} cited pages, {redirects} of them redirect links; {len(suggestions)} Search Suggestions")


async def probe_music(g: Gemini, out: Path) -> None:
    say(f"### Music ({settings.music_model})")
    started = time.monotonic()
    interaction = await g.client.aio.interactions.create(model=settings.music_model, input=music_prompt("warm"))
    took = time.monotonic() - started
    save(out, "music", interaction)
    audio = interaction.output_audio
    if audio is None or not audio.data:
        say(f"- no audio after {took:.1f}s; output_text: {interaction.output_text!r}")
        return
    data = base64.b64decode(audio.data)
    (out / "theme.mp3").write_bytes(data)
    say(f"- {took:.1f}s to generate; {audio.mime_type}, {len(data) / 1e3:.0f} kB, "
        f"{len(decode(data, 24_000)) / 24_000:.1f} s long. Listen: {out / 'theme.mp3'}")
    if interaction.output_text:
        say(f"- it also wrote: {interaction.output_text[:200]!r} (lyrics would mean vocals)")


async def probe_tts(g: Gemini) -> None:
    say(f"### Speech ({settings.tts_model})")
    hosts = [Host(name="Ivy", persona="", voice="Autonoe"), Host(name="Ravi", persona="", voice="Sadaltager")]
    text = ("So the memo starts with a walk home, and the city sounds louder than it does all day. "
            "Right, and the question is whether that's real or just that everything else went quiet.")
    lines = [Line(speaker=hosts[i % 2].name, text=text) for i in range(6)]
    started, first, samples = time.monotonic(), None, 0
    async for chunk in g.stream_speech(lines, hosts):
        first = first or time.monotonic() - started
        samples += len(chunk) // 2
    took, seconds = time.monotonic() - started, samples / settings.sample_rate
    say(f"- {seconds:.1f} s of speech in {took:.1f} s (first audio after {first or 0:.1f} s): "
        f"{seconds / took:.1f}x real time")


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--memo", nargs="+", required=True, type=Path, help="recordings to try")
    p.add_argument("--music", action="store_true", help="also generate one Lyria theme (~$0.04)")
    p.add_argument("--tts", action="store_true", help="also time the speech model")
    p.add_argument("--out", type=Path, default=ROOT / "data" / "probe" / time.strftime("%Y%m%d-%H%M%S"))
    args = p.parse_args()
    if settings.gemini_base_url:
        sys.exit(f"GEMINI_BASE_URL is set ({settings.gemini_base_url}): this probe is for the real API.")
    args.out.mkdir(parents=True, exist_ok=True)
    g = Gemini(settings)
    say(f"# Memo API probe, {time.strftime('%Y-%m-%d %H:%M')}: {settings.memo_model or settings.script_model}")
    briefs = [await probe_memo(g, path, args.out) for path in args.memo]
    usable = next((b for b in briefs if b and b.usable), None)
    if usable:
        await probe_research(g, usable, args.out)
    else:
        say("### Research: skipped (no usable memo)")
    if args.music:
        await probe_music(g, args.out)
    if args.tts:
        await probe_tts(g)
    (args.out / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"\nSaved under {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
