"""Puts the music around a rendered show's voice.

    [theme 6 s][cold open, the theme ducked under it and fading out] ... chapter 1 ...
    [sting 2.5 s] chapter 2 ... [sting] ... sign-off, the theme rising under it [theme 8 s]

The intro, the stings and the outro's play-out are inserted: the voice after
them moves later by their length, so every segment and chapter start saved for
replay has to be shifted (MixResult.shift, MixResult.chapter_start) or captions
would drift. The ducked music under the cold open and the sign-off takes no time.

Speech is levelled to SPEECH_RMS_DBFS. Cues arrive levelled to MUSIC_RMS_DBFS, a
little under the speech, and drop by DUCK_DB under talking. A soft limiter keeps
peaks under PEAK. Everything is mono at the show's rate. Work goes in chunks, so
besides the voice it's given a mix holds only a float working copy and the
finished PCM (about 130 MB for 15 minutes). Run mix() and encode_mp3() in a
thread, since live rooms share the process.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .audio import Mp3Writer

INTRO_LEAD_S = 6.0        # the theme alone before anyone speaks...
INTRO_UNDER_S = 8.0       # ...then under the cold open, fading out
STING_S = 2.5             # between chapters
MIN_STING_GAP_S = 45.0    # no sting if the last music started this recently
OUTRO_UNDER_S = 12.0      # the theme rises under the sign-off...
OUTRO_PLAYOUT_S = 8.0     # ...then plays out alone
RAMP_S = 0.4              # ducking in and out
CLICK_S = 0.03            # fade-in that keeps a cut from clicking
INTRO_FADE_S = 1.5
STING_FADE_S = 0.6
OUTRO_RISE_S = 1.5        # the outro creeping in under the voice
OUTRO_FADE_S = 3.0
LIVE_INTRO_S = 7.0        # a live pod opens on the theme alone (nothing to duck under yet)
LIVE_INTRO_FADE_S = 2.0
SPEECH_RMS_DBFS = -18.0
MUSIC_RMS_DBFS = -20.0
DUCK_DB = -18.0
GATE_DBFS = -50.0         # quieter blocks are pauses, left out of a level measurement
LIMIT_FROM, PEAK = 0.85, 0.95
CHUNK = 1 << 20           # samples per step


def from_db(db: float) -> float:
    return 10 ** (db / 20)


@dataclass
class Cues:
    """Mono float32 at the show's rate, levelled to MUSIC_RMS_DBFS. Any may be missing."""
    intro: np.ndarray | None = None
    sting: np.ndarray | None = None
    outro: np.ndarray | None = None


@dataclass
class Insert:
    at: int          # the voice sample it goes in front of
    length: int      # samples
    kind: str        # intro | sting | outro


@dataclass
class MixResult:
    pcm: bytearray                               # s16le
    inserts: list[Insert] = field(default_factory=list)
    # Where music plays, in output samples: {start, end, kind}. Dicts, not [start, end,
    # kind] lists: the pod document keeps them, and Firestore rejects arrays of arrays.
    regions: list[dict] = field(default_factory=list)

    @property
    def samples(self) -> int:
        return len(self.pcm) // 2

    def shift(self, x: int) -> int:
        """Where voice sample x lands: after everything inserted at or before it."""
        return x + sum(i.length for i in self.inserts if i.at <= x)

    def chapter_start(self, i: int, start: int) -> int:
        """Where chapter i (its voice starting at `start`) begins: the first chapter
        with the intro, the others with the sting in front of them, if they got one."""
        return 0 if i == 0 else start + sum(ins.length for ins in self.inserts if ins.at < start)


def active_rms(x: np.ndarray, rate: int, scale: float = 1.0) -> float:
    """RMS (full scale = 1) of the parts that aren't near-silent: pauses would drag
    a plain RMS down. x is int16 (scale 1/32768) or float."""
    block = max(1, int(0.4 * rate))
    whole = len(x) // block * block
    if not whole:
        c = x.astype(np.float64) * scale
        return float(np.sqrt(np.mean(c * c))) if len(c) else 0.0
    step = max(block, CHUNK // block * block)
    energies = []
    for a in range(0, whole, step):
        c = x[a:min(whole, a + step)].astype(np.float32) * np.float32(scale)
        energies.append((c.reshape(-1, block) ** 2).mean(axis=1))
    energy = np.concatenate(energies)
    loud = energy[energy > from_db(GATE_DBFS) ** 2]
    return float(np.sqrt(loud.mean())) if len(loud) else 0.0


def plan_inserts(voice_len: int, chapters: dict[int, int], cues: Cues, rate: int) -> list[Insert]:
    """What goes where, in voice samples. chapters: beat index -> its first sample."""
    if not voice_len:
        return []
    inserts: list[Insert] = []
    last_music: int | None = None
    if cues.intro is not None and len(cues.intro):
        inserts.append(Insert(0, min(len(cues.intro), int(INTRO_LEAD_S * rate)), "intro"))
        last_music = 0
    if cues.sting is not None and len(cues.sting):
        length = min(len(cues.sting), int(STING_S * rate))
        for i, start in sorted(chapters.items()):
            if i == 0 or not 0 < start < voice_len:
                continue
            if last_music is not None and start - last_music < MIN_STING_GAP_S * rate:
                continue
            inserts.append(Insert(start, length, "sting"))
            last_music = start
    if cues.outro is not None and len(cues.outro):
        inserts.append(Insert(voice_len, min(len(cues.outro), int(OUTRO_PLAYOUT_S * rate)), "outro"))
    return inserts


def _envelope(n: int, points: list[tuple[float, float]]) -> np.ndarray:
    """Gains for n samples, straight lines between (sample, gain) points."""
    xs = np.maximum.accumulate(np.clip([p[0] for p in points], 0, n))
    return np.interp(np.arange(n, dtype=np.float32), xs, [p[1] for p in points]).astype(np.float32)


def _add(out: np.ndarray, at: int, music: np.ndarray, env: np.ndarray) -> None:
    end = min(len(out), at + len(music))
    if end > at:
        out[at:end] += music[:end - at] * env[:end - at]


def mix(voice: bytes | bytearray, chapters: dict[int, int], cues: Cues | None, rate: int) -> MixResult:
    """The finished show: voice (s16le) with the cues placed around it. With no
    cues the voice is only levelled."""
    v16 = np.frombuffer(voice, dtype="<i2")
    cues = cues or Cues()
    inserts = plan_inserts(len(v16), chapters, cues, rate)
    out = np.zeros(len(v16) + sum(i.length for i in inserts), dtype=np.float32)

    rms = active_rms(v16, rate, 1 / 32768)
    gain = np.float32((min(4.0, max(0.25, from_db(SPEECH_RMS_DBFS) / rms)) if rms else 1.0) / 32768)
    placed: list[tuple[Insert, int]] = []
    src = dst = 0
    for ins in inserts:
        piece = ins.at - src
        np.multiply(v16[src:ins.at], gain, out=out[dst:dst + piece], casting="unsafe")
        dst += piece
        placed.append((ins, dst))
        dst += ins.length
        src = ins.at
    np.multiply(v16[src:], gain, out=out[dst:], casting="unsafe")

    duck, ramp = from_db(DUCK_DB), RAMP_S * rate
    regions: list[dict] = []
    for ins, pos in placed:
        if ins.kind == "intro":
            clip = cues.intro[: ins.length + int(INTRO_UNDER_S * rate)]
            n = len(clip)
            env = _envelope(n, [(0, 0), (CLICK_S * rate, 1), (ins.length - ramp, 1), (ins.length, duck),
                                (n - INTRO_FADE_S * rate, duck), (n, 0)])
            _add(out, pos, clip, env)
        elif ins.kind == "sting":
            clip = cues.sting[: ins.length]
            n = len(clip)
            _add(out, pos, clip, _envelope(n, [(0, 0), (CLICK_S * rate, 1), (n - STING_FADE_S * rate, 1), (n, 0)]))
        else:
            # The theme's ending: rising under the last words, then alone. Never
            # under earlier music, so it can't stack on a late sting.
            prev_end = regions[-1]["end"] if regions else 0
            under = max(0, min(int(OUTRO_UNDER_S * rate), len(cues.outro) - ins.length, pos - prev_end))
            clip = cues.outro[len(cues.outro) - ins.length - under:]
            n = len(clip)
            env = _envelope(n, [(0, 0), (min(OUTRO_RISE_S * rate, under), duck), (under - ramp, duck), (under, 1),
                                (n - OUTRO_FADE_S * rate, 1), (n, 0)])
            _add(out, pos - under, clip, env)
            pos -= under
        regions.append({"start": pos, "end": pos + n, "kind": ins.kind})
    return MixResult(pcm=_to_pcm(out), inserts=inserts, regions=regions)


def intro_sting(intro: np.ndarray, rate: int) -> bytes:
    """A live pod's opening: the theme for a few seconds, fading as the hosts come
    in. Their words are written and voiced live, so there's nothing to mix it under."""
    clip = intro[: int(LIVE_INTRO_S * rate)]
    n = len(clip)
    env = _envelope(n, [(0, 0), (CLICK_S * rate, 1), (n - LIVE_INTRO_FADE_S * rate, 1), (n, 0)])
    return bytes(_to_pcm(clip * env))


def _to_pcm(out: np.ndarray) -> bytearray:
    """Soft-limit to PEAK (gently, above LIMIT_FROM), then to s16le, written
    straight into the buffer that's returned (a 15-minute show is 43 MB)."""
    buf = bytearray(len(out) * 2)
    pcm = np.frombuffer(buf, dtype="<i2")
    knee = PEAK - LIMIT_FROM
    for a in range(0, len(out), CHUNK):
        c = out[a:a + CHUNK]
        over = np.abs(c) > LIMIT_FROM
        if over.any():
            mag = np.abs(c[over])
            c[over] = np.sign(c[over]) * (LIMIT_FROM + knee * np.tanh((mag - LIMIT_FROM) / knee))
        pcm[a:a + CHUNK] = np.rint(c * 32767)
    return buf


def encode_mp3(pcm: bytes | bytearray, rate: int, kbps: int, chunk_s: float = 2.0) -> bytes:
    """The finished show as MP3, fed to the encoder a little at a time so the event
    loop (and every live room) gets turns while it works. Run it in a thread."""
    writer = Mp3Writer(rate, kbps)
    step = max(2, int(rate * chunk_s) * 2)
    view = memoryview(pcm)
    for a in range(0, len(view), step):
        writer.feed(bytes(view[a:a + step]))
    return writer.finish()
