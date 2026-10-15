"""Turning a recorded co-host call into an episode.

A call is recorded as it happens (Take): your mic as one continuous 16 kHz track,
and each of the co-host's turns placed on the same clock, where it started
playing. When you hang up, build_edit() works out who was talking when, splits
your talking into phrases at the pauses and lines the live transcript up with
them. The editor (llm.edit_cohost) reads that and says what to cut, and
assemble() applies it:

  crosstalk  your mic is muted while the co-host talks, unless you cut in (what
             it hears then is the speaker's echo, or an "mm-hm"). A co-host turn
             you cut into ends where you did; one cut off in its first 1.5 s goes.
  dead air   silence before and after every turn is trimmed, pauses inside a turn
             longer than 1.2 s become 0.6 s, and turns meet with a short gap
  flubs      the phrases the editor marked are cut at the pauses around them
  polish     every turn is levelled to the same loudness, peaks are softly
             limited, every cut gets a short fade, and a chime opens and closes it

Pure functions on numpy arrays, so all of it is tested without a server or a
model. Positions are samples at 24 kHz on the call clock unless they say otherwise.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import numpy as np

from .models import EditChapter, EditPlan

RATE = 24_000                 # the episode, and the co-host's voice
MIC_RATE = 16_000             # what the browser sends and the Live API listens to
FRAME = RATE // 50            # 20 ms analysis frames

PHRASE_GAP_S = 0.3            # a pause at least this long ends a phrase
MIN_SPEECH_S = 0.08           # shorter blips are clicks, not speech
PAD_BEFORE_S = 0.12           # kept around each phrase so no consonant is clipped
PAD_AFTER_S = 0.18
LONG_PAUSE_S = 1.2            # pauses inside a turn longer than this...
TIGHT_PAUSE_S = 0.6           # ...become this long
CUT_JOIN_S = 0.25             # the pause left where a phrase was cut
TURN_GAP_S = 0.3              # between one speaker and the next
CUT_IN_GAP_S = 0.08           # when you cut in on the co-host
CUT_IN_PREROLL_S = 0.8        # you started talking a little before it stopped
MIN_CUT_OFF_TURN_S = 1.5      # a co-host turn cut off sooner than this is dropped
TRANSCRIPT_LAG_S = 0.4        # the live transcript trails the speech by about this
TARGET_DBFS = -20.0
MAX_GAIN_DB = 12.0
FADE_S = 0.01
STING_S = 1.2
LINE_CHARS = 160


# ---- recording ---------------------------------------------------------------------

@dataclass
class AiTurn:
    id: int
    start: int                                  # where it started playing (call clock)
    pcm: bytearray = field(default_factory=bytearray)   # 24 kHz s16le
    text: str = ""
    cut_at: int | None = None                   # where you cut in (call clock)
    done: bool = False

    @property
    def samples(self) -> int:
        n = len(self.pcm) // 2
        return min(n, max(0, self.cut_at - self.start)) if self.cut_at is not None else n

    @property
    def end(self) -> int:
        return self.start + self.samples


class Take:
    """A call as it happened: your mic, the co-host's turns, the live transcript.

    The browser sends mic audio continuously, silence included, so how much of it
    has arrived is the call clock. A co-host turn starts where the clock was when
    its first audio arrived (or where the previous turn ends, if that is later);
    the rest of the turn follows on without gaps, as the phone plays it."""

    def __init__(self) -> None:
        self.mic = bytearray()                      # 16 kHz s16le
        self.turns: list[AiTurn] = []
        self.heard: list[tuple[int, str]] = []      # (call clock on arrival, your words)
        self._carry = b""

    @property
    def now(self) -> int:
        return len(self.mic) // 2 * RATE // MIC_RATE

    @property
    def length(self) -> int:
        return max(self.now, self.turns[-1].end if self.turns else 0)

    @property
    def current(self) -> AiTurn | None:
        return self.turns[-1] if self.turns and not self.turns[-1].done else None

    def add_mic(self, pcm: bytes) -> None:
        self.mic += pcm[: len(pcm) // 2 * 2]

    def _turn(self) -> AiTurn:
        turn = self.current
        if turn is None:
            after = self.turns[-1].end if self.turns else 0
            turn = AiTurn(id=len(self.turns) + 1, start=max(self.now, after))
            self.turns.append(turn)
        return turn

    def add_ai_audio(self, pcm: bytes) -> tuple[AiTurn, bytes]:
        """Returns the turn and the whole samples to play (a chunk can end mid-sample)."""
        turn = self._turn()
        data = self._carry + pcm
        whole = len(data) // 2 * 2
        self._carry = data[whole:]
        turn.pcm += data[:whole]
        return turn, data[:whole]

    def add_ai_text(self, text: str) -> AiTurn:
        """The co-host's words. Words arriving after its turn was complete, while that
        turn is still playing, are its late transcript, not a new turn."""
        last = self.turns[-1] if self.turns else None
        if self.current is None and last is not None and last.cut_at is None and self.playing(last):
            turn = last
        else:
            turn = self._turn()
        turn.text += text
        return turn

    def add_heard(self, text: str) -> None:
        self.heard.append((self.now, text))

    def playing(self, turn: AiTurn) -> bool:
        return self.now < turn.start + len(turn.pcm) // 2

    def interrupt(self) -> AiTurn | None:
        """You cut in. The co-host's last turn ends here if it was still playing, even
        if the model had finished sending it (it sends faster than the phone plays).
        Returns the turn that was cut, if one was."""
        turn = self.turns[-1] if self.turns else None
        self.finish_turn()
        if turn is None or turn.cut_at is not None or not self.playing(turn):
            return None
        turn.cut_at = max(turn.start, self.now)
        return turn

    def finish_turn(self) -> AiTurn | None:
        turn = self.current
        if turn is not None:
            turn.done = True
            self._carry = b""
        return turn


# ---- signal helpers ------------------------------------------------------------------

def pcm_to_float(pcm: bytes) -> np.ndarray:
    return np.frombuffer(bytes(pcm), dtype="<i2").astype(np.float32) / 32768.0


def float_to_pcm(x: np.ndarray) -> bytes:
    return (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def upsample_mic(x16: np.ndarray) -> np.ndarray:
    """16 kHz to 24 kHz. Linear interpolation is plenty for speech going up in rate."""
    if not len(x16):
        return np.zeros(0, np.float32)
    n = len(x16) * RATE // MIC_RATE
    return np.interp(np.arange(n) * (MIC_RATE / RATE), np.arange(len(x16)), x16).astype(np.float32)


def frame_db(x: np.ndarray) -> np.ndarray:
    frames = len(x) // FRAME
    if not frames:
        return np.zeros(0)
    rms = np.sqrt(np.mean(np.square(x[: frames * FRAME].reshape(frames, FRAME), dtype=np.float64), axis=1))
    return 20 * np.log10(rms + 1e-9)


def speech_threshold(x: np.ndarray) -> float:
    """Somewhat above the room's noise floor, within sane bounds for speech."""
    db = frame_db(x)
    db = db[db > -90]
    floor = float(np.percentile(db, 10)) if len(db) else -90.0
    return min(max(floor + 15.0, -50.0), -30.0)


def speech_spans(x: np.ndarray, threshold_db: float) -> list[tuple[int, int]]:
    """Runs of speech (sample ranges within x), blips under MIN_SPEECH_S dropped."""
    loud = frame_db(x) > threshold_db
    spans, start = [], None
    for i, on in enumerate(np.append(loud, False)):
        if on and start is None:
            start = i
        elif not on and start is not None:
            if (i - start) * FRAME >= MIN_SPEECH_S * RATE:
                spans.append((start * FRAME, i * FRAME))
            start = None
    return spans


def merge_spans(spans: list[tuple[int, int]], gap_s: float) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for s, e in spans:
        if out and s - out[-1][1] < gap_s * RATE:
            out[-1][1] = e
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def pad_spans(spans: list[tuple[int, int]], lo: int, hi: int) -> list[tuple[int, int]]:
    """Room around each span, within [lo, hi) and never into a neighbour."""
    out = []
    for i, (s, e) in enumerate(spans):
        s2 = max(lo, s - int(PAD_BEFORE_S * RATE))
        e2 = min(hi, e + int(PAD_AFTER_S * RATE))
        if i > 0:
            s2 = max(s2, (spans[i - 1][1] + s) // 2)
        if i + 1 < len(spans):
            e2 = min(e2, (e + spans[i + 1][0]) // 2)
        out.append((s2, e2))
    return out


def fade(x: np.ndarray) -> np.ndarray:
    n = min(int(FADE_S * RATE), len(x) // 2)
    if n:
        ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
        x = x.copy()
        x[:n] *= ramp
        x[-n:] *= ramp[::-1]
    return x


def level(clip: np.ndarray, speech: np.ndarray | None = None) -> np.ndarray:
    """Bring speech to TARGET_DBFS, within ±MAX_GAIN_DB. Measured on `speech` (the
    clip without its pauses) when given, so pauses don't make a turn look quiet."""
    ref = clip if speech is None else speech
    if not len(clip) or not len(ref):
        return clip
    rms = math.sqrt(float(np.mean(np.square(ref, dtype=np.float64))))
    if rms < 1e-6:
        return clip
    gain_db = max(-MAX_GAIN_DB, min(MAX_GAIN_DB, TARGET_DBFS - 20 * math.log10(rms)))
    return clip * np.float32(10 ** (gain_db / 20))


def soft_limit(x: np.ndarray, knee: float = 0.9) -> np.ndarray:
    a = np.abs(x)
    over = a > knee
    if not over.any():
        return x
    y = x.copy()
    y[over] = np.sign(x[over]) * (knee + (1 - knee) * np.tanh((a[over] - knee) / (1 - knee)))
    return y


def sting(rising: bool = True) -> np.ndarray:
    """A short chime (C, E, G) to open the episode; falling to close it."""
    t = np.arange(int(STING_S * RATE)) / RATE
    out = np.zeros(len(t), np.float32)
    notes = [523.25, 659.25, 783.99]
    for i, f in enumerate(notes if rising else notes[::-1]):
        at = int(i * 0.11 * RATE)
        tt = t[: len(t) - at]
        env = np.exp(-tt * 4.5) * (1 - np.exp(-tt * 200))
        tone = np.sin(2 * np.pi * f * tt) + 0.25 * np.sin(4 * np.pi * f * tt)
        out[at:] += (0.12 * env * tone).astype(np.float32)
    return out


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * RATE), np.float32)


_SENTENCE = re.compile(r"(?<=[.!?…])\s+")


def to_lines(text: str, limit: int = LINE_CHARS) -> list[str]:
    """Sentence-sized caption lines."""
    lines: list[str] = []
    cur = ""
    for part in (p.strip() for p in _SENTENCE.split(text.strip())):
        if not part:
            continue
        if cur and len(cur) + 1 + len(part) > limit:
            lines.append(cur)
            cur = part
        else:
            cur = f"{cur} {part}".strip()
    if cur:
        lines.append(cur)
    return lines


def _words_kept(text: str, fraction: float) -> str:
    words = text.split()
    if fraction >= 1 or not words:
        return text.strip()
    keep = max(1, math.ceil(len(words) * fraction))
    return " ".join(words[:keep]) + ("—" if keep < len(words) else "")


# ---- what was said, when ----------------------------------------------------------------

@dataclass
class Phrase:
    id: int
    start: int
    end: int
    text: str = ""


@dataclass
class Turn:
    id: int
    you: bool
    phrases: list[Phrase]
    text: str = ""            # the co-host's words (yours are on the phrases)
    cut_off: bool = False     # the co-host's turn, cut short by you
    cut_in: bool = False      # your turn, cutting in on the co-host
    ai_id: int = 0            # the co-host's turn: its AiTurn.id

    @property
    def start(self) -> int:
        return self.phrases[0].start

    @property
    def end(self) -> int:
        return self.phrases[-1].end


@dataclass
class Edit:
    turns: list[Turn]
    you: np.ndarray                   # your mic at 24 kHz
    ai: dict[int, np.ndarray]         # turn id -> the co-host's audio (call clock = turn.start)
    ai_start: dict[int, int]
    length: int
    dropped_cut_offs: int = 0

    def editor_turns(self, you_name: str, host_name: str) -> list[dict]:
        """What the editor reads (see prompts.editor_input)."""
        out = []
        for t in self.turns:
            if t.you:
                out.append({"id": t.id, "speaker": you_name, "you": True,
                            "phrases": [{"id": p.id, "text": p.text} for p in t.phrases]})
            else:
                out.append({"id": t.id, "speaker": host_name, "you": False, "cut_off": t.cut_off, "text": t.text})
        return out


def build_edit(take: Take) -> Edit:
    you = upsample_mic(pcm_to_float(take.mic))
    threshold = speech_threshold(you)
    ai_turns = [t for t in take.turns if t.samples > 0]
    dropped = [t for t in ai_turns if t.cut_at is not None and t.samples < MIN_CUT_OFF_TURN_S * RATE]
    ai_turns = [t for t in ai_turns if t not in dropped]

    raw: list[tuple[int, Turn]] = []
    ai_audio: dict[int, np.ndarray] = {}
    ai_start: dict[int, int] = {}
    phrase_ids = iter(range(1_000_000))

    def your_turn(lo: int, hi: int, cut_in: bool) -> None:
        lo, hi = max(0, lo), min(len(you), hi)
        if hi - lo < FRAME:
            return
        spans = merge_spans(speech_spans(you[lo:hi], threshold), PHRASE_GAP_S)
        spans = pad_spans([(lo + s, lo + e) for s, e in spans], lo, hi)
        if spans:
            raw.append((spans[0][0], Turn(id=0, you=True, cut_in=cut_in,
                                          phrases=[Phrase(next(phrase_ids), s, e) for s, e in spans])))

    cursor, cut_in = 0, False
    for t in sorted(ai_turns, key=lambda t: t.start):
        your_turn(cursor, t.start, cut_in)
        audio = pcm_to_float(t.pcm)[: t.samples]
        spans = merge_spans(speech_spans(audio, -50.0), PHRASE_GAP_S)
        spans = pad_spans(spans, 0, len(audio))
        cut_off = t.cut_at is not None
        if spans:
            fraction = t.samples / max(1, len(t.pcm) // 2)
            text = _words_kept(t.text, fraction) if cut_off else t.text.strip()
            turn = Turn(id=0, you=False, cut_off=cut_off, text=text, ai_id=t.id,
                        phrases=[Phrase(-1, t.start + s, t.start + e) for s, e in spans])
            raw.append((turn.start, turn))
            ai_audio[t.id] = audio
            ai_start[t.id] = t.start
        cursor = t.end
        cut_in = cut_off
        if cut_off:
            cursor = max(t.start, t.end - int(CUT_IN_PREROLL_S * RATE))
    your_turn(cursor, len(you), cut_in)

    turns = [t for _, t in sorted(raw, key=lambda x: x[0])]
    for i, t in enumerate(turns):
        t.id = i
    _attach_transcript(turns, take.heard)
    return Edit(turns=turns, you=you, ai=ai_audio, ai_start=ai_start, length=take.length,
                dropped_cut_offs=len(dropped))


def _attach_transcript(turns: list[Turn], heard: list[tuple[int, str]]) -> None:
    """The live transcript of your speech arrives in fragments, a little behind the
    words. Each fragment goes to the phrases that had started by then and have no
    words yet, shared out by length; one that arrives late joins the last phrase."""
    phrases = [p for t in turns if t.you for p in t.phrases]
    k = 0
    lag = int(TRANSCRIPT_LAG_S * RATE)
    for at, text in heard:
        words = text.split()
        if not words or not phrases:
            continue
        n = 0
        while k + n < len(phrases) and phrases[k + n].start <= at - lag:
            n += 1
        if n == 0:
            target = phrases[max(0, k - 1)]
            target.text = f"{target.text} {text.strip()}".strip()
            continue
        group = phrases[k:k + n]
        total = sum(p.end - p.start for p in group) or 1
        bounds, acc = [], 0
        for p in group:
            acc += p.end - p.start
            bounds.append(acc / total)
        shares: list[list[str]] = [[] for _ in group]
        for i, w in enumerate(words):
            pos = (i + 0.5) / len(words)
            shares[next(j for j, b in enumerate(bounds) if pos <= b + 1e-9)].append(w)
        for p, ws in zip(group, shares):
            p.text = " ".join(ws)
        k += n


# ---- the episode -----------------------------------------------------------------------

@dataclass
class Episode:
    pcm: bytes                       # 24 kHz s16le
    segments: list[dict]
    beats: list[dict]
    stats: dict

    @property
    def samples(self) -> int:
        return len(self.pcm) // 2


def fallback_plan(title: str) -> EditPlan:
    return EditPlan(title=title, chapters=[EditChapter(title="The conversation", start_turn=0)])


def assemble(edit: Edit, plan: EditPlan, you_name: str, host_name: str,
             revs: list[tuple[int, int]] = ()) -> Episode:
    """revs: (call clock, style revision) for every mode switch, so each segment
    carries the mode it was said in and a replay re-tints where the call did."""
    cut = set(plan.cut_phrases)
    drop = {t for t in plan.drop_turns if any(x.id == t and not x.you for x in edit.turns)}
    parts: list[np.ndarray] = [sting(rising=True), silence(0.35)]
    pos = sum(len(p) for p in parts)
    segments: list[dict] = []
    seg_turns: list[int] = []
    flubs = 0
    prev_turn: Turn | None = None

    for t in edit.turns:
        if not t.you and t.id in drop:
            continue
        phrases = t.phrases
        if t.you:
            flubs += sum(p.id in cut for p in phrases)
            phrases = [p for p in phrases if p.id not in cut]
            if not phrases:
                continue
        clip = _turn_audio(edit, t, phrases)
        if not len(clip):
            continue
        if prev_turn is not None:
            gap = silence(CUT_IN_GAP_S if t.you and t.cut_in else TURN_GAP_S)
            parts.append(gap)
            pos += len(gap)
        text = " ".join(p.text for p in phrases if p.text).strip() if t.you else t.text
        speaker = you_name if t.you else host_name
        rev = max((r for at, r in revs if at <= t.start), default=0)
        segments.append({
            "seq": len(segments) + 1, "beat": 0, "start": pos, "samples": len(clip), "final": False,
            "style_rev": rev, "lines": [{"speaker": speaker, "text": ln, "style": ""} for ln in to_lines(text)],
            "addressed": [], "declined": [], "used_context": [],
        })
        seg_turns.append(t.id)
        parts.append(clip)
        pos += len(clip)
        prev_turn = t

    if not segments:
        return Episode(pcm=b"", segments=[], beats=[], stats={"call_s": round(edit.length / RATE, 1)})
    segments[-1]["final"] = True
    parts += [silence(0.5), sting(rising=False)]
    audio = soft_limit(np.concatenate(parts))
    beats = _beats(plan.chapters, segments, seg_turns, len(audio))
    for seg in segments:
        seg["beat"] = max(i for i, b in enumerate(beats) if b["start"] <= seg["start"])
    call_s = edit.length / RATE
    episode_s = len(audio) / RATE
    stats = {
        "call_s": round(call_s, 1),
        "episode_s": round(episode_s, 1),
        "tightened_s": round(max(0.0, call_s - (episode_s - 2 * STING_S - 0.85)), 1),
        "flubs_cut": flubs,
        "turns_dropped": len(drop) + edit.dropped_cut_offs,
        "cut_ins": sum(1 for t in edit.turns if t.cut_off),
    }
    return Episode(pcm=float_to_pcm(audio), segments=segments, beats=beats, stats=stats)


def _turn_audio(edit: Edit, t: Turn, phrases: list[Phrase]) -> np.ndarray:
    """The turn's phrases with tightened pauses, faded at every edge, then levelled."""
    if t.you:
        src, origin = edit.you, 0
    else:
        src, origin = edit.ai[t.ai_id], edit.ai_start[t.ai_id]
    pieces: list[np.ndarray] = []
    speech: list[np.ndarray] = []
    prev: Phrase | None = None
    for p in phrases:
        if prev is not None:
            gap = (p.start - prev.end) / RATE
            skipped = t.you and any(prev.id < q.id < p.id for q in t.phrases)
            pieces.append(silence(CUT_JOIN_S if skipped else TIGHT_PAUSE_S if gap > LONG_PAUSE_S else gap))
        clip = src[p.start - origin:p.end - origin]
        speech.append(clip)
        pieces.append(fade(clip))
        prev = p
    if not pieces:
        return np.zeros(0, np.float32)
    return level(np.concatenate(pieces), np.concatenate(speech))


def _beats(chapters: list[EditChapter], segments: list[dict], seg_turns: list[int], total: int) -> list[dict]:
    """The editor's chapters, placed where their first kept turn starts. The first
    one starts at 0, so the opening chime belongs to it."""
    starts: list[tuple[int, EditChapter]] = []
    for c in sorted(chapters or [EditChapter(title="The conversation")], key=lambda c: c.start_turn):
        at = next((s["start"] for s, tid in zip(segments, seg_turns) if tid >= c.start_turn), None)
        if at is None:
            continue
        at = 0 if not starts else at
        if starts and at <= starts[-1][0]:
            continue
        starts.append((at, c))
    if not starts:
        starts = [(0, EditChapter(title="The conversation"))]
    beats = []
    for i, (at, c) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else total
        beats.append({"title": c.title.strip()[:80] or f"Part {i + 1}", "notes": c.notes.strip()[:300],
                      "minutes": round((end - at) / RATE / 60, 2), "start": at, "end": end})
    return beats


def raw_mix(take: Take) -> bytes:
    """The call unedited: both sides where they happened, the co-host cut where you cut in."""
    you = upsample_mic(pcm_to_float(take.mic))
    mix = np.zeros(max(len(you), take.length), np.float32)
    mix[: len(you)] += you
    for t in take.turns:
        audio = pcm_to_float(t.pcm)[: t.samples]
        end = min(len(mix), t.start + len(audio))
        mix[t.start:end] += audio[: end - t.start]
    return float_to_pcm(soft_limit(mix)) if len(mix) else b""
