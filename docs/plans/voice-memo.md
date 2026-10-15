# Voice memo → finished show (Gemini Podcasts)

## Context
Today every pod is generated **live**. `ShowRunner` (`app/show.py:116`) plans, then writes and voices about 20 s at a time, paced by a listener-driven `LiveClock`. Pods can only be built from typed, pasted or uploaded source text. This feature adds a second way to make a pod:

1. You record a rough, disjointed voice note of about 2 minutes on a walk, or upload one.
2. The engine transcribes and understands it.
3. It runs live Google-grounded research.
4. It drafts a narrative outline.
5. It voices the show headlessly with the existing hosts and TTS.
6. It scores music.
7. It saves a **finished, replayable** show: 15 minutes by default, with 5 and 10 also offered.

Nothing in the app does transcription, grounding, music, mixing or background jobs yet. The runner is already decoupled from sockets through the `RoomPort` Protocol (`show.py:67`), so a headless render only needs a new room and clock, not a new generator.

## Locked decisions (from you)
- **Music:** the default is a **prepackaged library** (built once, shipped with the app, no per-show AI call, no extra cost). Opt-in **"Generate original music" with Lyria costs extra credits and takes extra time**, and the UI says both. Generated tracks are **cached per user** and can be reused for free. If Lyria fails, the show falls back to the library and the music credits are refunded. With an empty library, the show is voice-only.
- **Flow:** one-shot by default, plus an optional **"Review outline first"** toggle. It pauses after transcript + research + outline, and credits are charged at Produce.
- **No clips of your original memo voice** in v1. The hosts paraphrase the memo and credit you.
- **Hosts and format:** picked automatically from the memo, with an optional template/persona/style override.
- **Research:** Google Search grounding (Gemini `google_search` tool) for **any** pod visibility. You accepted the terms risk. Search Suggestions are shown to the owner, and source links are listed in the show notes.
- Existing rules still apply: 1 credit/min with unused minutes refunded, demo with no payments, private/connections/public visibility, templates named by format, host voice matches gender, single Cloud Run instance, and you run deploys.

## Step 0: worktree and API probe
1. **Worktree:** follow the existing `.claude/worktrees/<name>` convention (`cohost` and `review-personal-feeds-branching` already live there). Use the EnterWorktree tool to create `.claude/worktrees/voice-memo` on branch `worktree-voice-memo`, starting from `main` @ `964b051`.
   - Copy `.env` into it.
   - Reuse `..\..\..\.venv`, and `pip install numpy miniaudio` into it (harmless for main).
   - `data/` is gitignored, so the worktree starts with an empty local store.
   - Save this plan as `docs/plans/voice-memo.md` in the worktree and make that the branch's first commit.
   - Don't run the smoke test in two worktrees at once: it uses fixed ports 8765 and 8766.
2. **Merge risk:** the `cohost` worktree has uncommitted edits in `catalog.py`, `config.py`, `models.py` and `prompts.py`. Keep this branch's changes to those four files additive (new fields and blocks with defaults, no reshuffling).
3. **`tools/probe_memo_apis.py`** (real key, run once). Save the raw `interaction.model_dump_json()` output as fixtures in `tests/fixtures/` to shape the fake Gemini. It checks:
   - **Audio input part** `{"type":"audio","data":b64,"mime_type":…}` with Chrome `audio/webm;codecs=opus`, iOS Safari `audio/mp4`, and an iOS Voice Memos `.m4a`. The SDK enum lists webm, ogg, m4a, aac, mp3, wav, flac and opus. Inline limit is 20 MB including base64, so uploads are capped at about 14 MB.
   - **`tools=[{"type":"google_search"}]` combined with a JSON `response_format`** on `gemini-3.8-flash` (a Gemini 3 preview feature). If it's rejected, use two calls: a grounded text call, then a JSON condense call with no tools.
   - **Response steps:** `google_search_call.arguments.queries`, `google_search_result[].search_suggestions` (HTML), and `url_citation` annotations (`url`, `title`). Check whether the citation URLs are redirect links.
   - **Lyria:** the `lyria-3-clip-preview` response (`output_audio.data` base64 MP3, 30 s, 44.1 kHz stereo), how long it takes, and whether vocals leak in when the prompt asks for "Instrumental only".
   - **TTS speed:** seconds to voice a 40 s segment. This sets render time and `max_renders`.

## Pipeline
```
POST /api/memos ─► MemoJob.prepare:  transcribing → researching → outlining
                     └─ review on? → status "review" (stops; POST /api/memos/{id}/produce resumes)
Renders slot (FIFO, max_renders) ─► MemoJob.produce:
   voicing  ShowRunner(plan=…, room=RenderRoom)   ← Lyria task runs in parallel if "generate"
   scoring  generated | cached "mine" | library pack | none
   mixing   mixer.mix() in a thread → PCM + insert/offset map
   saving   chunked MP3 → put_audio; shifted segments/chapters; final doc; settle credits
```

## Phases (each leaves `tests.unit_test` and `tests.smoke_test` green)

### Phase 1: foundations (no visible change)
- **`requirements.txt`:** add `numpy>=2.1` and `miniaudio>=1.61` (self-contained cp313 wheels, no ffmpeg).
  - No PyAV: Gemini accepts the browser's formats directly, so only the music needs decoding.
- **Store.** Add to `app/store/__init__.py` (Protocol), `local.py` and `gcp.py`:
  - `put_show_file`/`get_show_file(show_id, name)`. Stored at `shows/{id}/{name}`, so the existing pod delete removes them too. Names are checked with a strict regex.
  - `put_user_file`/`get_user_file(uid, name)`, for cached music.
  - `shows_with_status(status)` and `ledger_for_show(uid, show_id)`. Both are single-field Firestore queries, like `gcp.py:315`, so no new indexes.
  - Extend `tests/store_contract.py`.
- **`app/sources.py`:** `sniff_audio(data) -> mime|None`, next to `sniff_image` (122). It covers RIFF/WAVE, ID3 and MPEG frame headers, ADTS, `fLaC`, `OggS`, EBML (`1A45DFA3` = webm), `ftyp` M4A/isom/mp42 brands (must not collide with HEIC), and AIFF.
- **Hide unpublished pods (fixes a leak).** `accounts.can_view` (`accounts.py:101`), `main._visible` (247), `list_shows` (819) and `my_stats` (1101) only exclude `draft` today. Add `UNPUBLISHED={"draft","rendering","review"}` and `published(doc)` in `accounts.py`, and use them in all four places. A failed memo pod with no audio is also unpublished.
- **`app/room.py`:** pull out `beats_with_times(...)` (614-622) and `refund_unused(...)` (493-509) as module functions. `ShowRoom` delegates to them, so behavior is unchanged.
- **`app/show.py`:**
  - `ShowRunner.__init__` gains `plan: ShowPlan|None=None`, `write_timeout: float|None=None` and `speech_retries: int=0`.
  - `run()` only calls `plan_show` when there is no plan (242-249), and still publishes the `plan` event.
  - `_write` reads the timeout at call time (the unit test at `unit_test.py:424` monkeypatches the constant).
  - `_speak` retries a segment that produced no audio.
- **`app/models.py`:** `ShowRequest` gains `mode: Literal["live","recorded"]="live"` and `direction: str=""` (max 4000 characters). `main._public_request` (224) strips `direction`.
- **`app/prompts.py`:** a `{direction}` block in the planner and writer system prompts. In the writer it goes in the static prefix between HOSTS and SOURCE, so prompt caching survives.
  - `mode="recorded"` drops the "live show", call-in and added-context rules.
  - **Live prompts must stay byte-identical** (unit check).
- **`app/catalog.py`:**
  - `MUSIC_MOODS` (upbeat, warm, reflective, dramatic, playful, calm, tech, newsy).
  - A new format-named template `idea-breakdown`: a curious host and an expert host.
  - `template_request(tid)`, the server-side version of `static/js/views/create.js:229-242`.
- **`app/limits.py`:** `MEMO_UPLOADS = Limiter(rate=1/300, burst=3)`, per user.

### Phase 2: headless render, mixer, music library
- **`app/render.py`:**
  - `RenderClock(LiveClock)`: `tick()` pins `live = audio_end`, so `ahead_s()` is 0 and `_wait_for_slot` (`show.py:475`) returns READY straight away.
  - `RenderRoom`, a `RoomPort` that caches plan, segment, segment_end, chapter and error events and appends PCM to a buffer.
  - **Never reuse `ShowRoom`:** its `_tick_loop` (`room.py:527`) ends a pod after 300 s with no listeners.
- **`app/mixer.py`** (numpy, 24 kHz mono, so it stays on the existing timeline the replay uses):
  - `mix(voice, segments, chapters, cues, rate) -> MixResult(pcm, inserts, regions)`, with `shift(x)` and `chapter_start(i, start)`.
  - Layout:
    - The intro plays alone for 6 s, then sits at −18 dB under the cold open and fades out.
    - A 2.5 s sting opens each chapter after the first, skipped if the previous one was less than 45 s earlier.
    - The outro comes in ducked under the last 12 s of the sign-off, then plays out for 8 s and fades.
  - Ducking uses fixed gains with 0.4 s ramps, since the speech positions are known. Speech is normalized to −18 dBFS RMS with a tanh soft limit.
  - `encode_mp3(...)` feeds `Mp3Writer` (`app/audio.py:15`) in chunks inside `asyncio.to_thread`, so live pods never stall.
  - With no cues, the voice passes through unchanged.
- **`app/music.py`:**
  - `Library.load(dir)` reads `music/manifest.json`, and `.pick(mood, seed)` chooses a pack.
  - `decode(data, rate)` uses `miniaudio.decode` to get 24 kHz mono s16.
  - `cues_from_theme(...)` slices intro, sting and outro from a 30 s theme, or uses the manifest's `cues`.
  - `music_prompt(mood, …)` is built only from enum values, never user text.
- **`tools/build_music_library.py`:**
  - `--from-files DIR --mood X` takes drop-in royalty-free tracks.
  - `--lyria` makes one 30 s theme per mood, about 8 × $0.04. You run it once and commit `music/`, about 3 MB.
  - The branch ships `music/manifest.json` as `{"version":1,"packs":[]}`, so the app works voice-only until the library is built.
  - Add `*.mp3 binary` to `.gitattributes`.

### Phase 3: Gemini calls (`app/llm.py`, `app/prompts.py`, `tests/fake_gemini.py`)
- Generalize `_json_call` (`llm.py:50`) to take `user: str|list`, `tools=` and `model=`, and add `_create()`, which returns the raw interaction so steps and annotations can be read.
- `analyze_memo(audio, mime, *, creator, client_duration_s) -> MemoBrief`, modeled on `describe_image` (`llm.py:112`). The base64 encoding runs in a thread.
- `research(brief, *, max_queries) -> ResearchDossier`. It sends the google_search tool with a JSON schema and falls back to two calls once on a 400. Queries, sources (deduped by URL) and suggestions (at most 5) are read from the steps, not from the model's JSON.
- `generate_music(prompt) -> (bytes, mime)` with `settings.music_model`.
- Prompts:
  - `MEMO_SYSTEM`/`memo_schema()`: enums for mood and template ids.
  - `RESEARCH_SYSTEM`/`research_schema()`: today's date, name an outlet for every finding, flag thin evidence.
  - `MEMO_DIRECTION`, the arc: hook → credit {name}'s memo and frame the question → context → what live research found (cite outlets) → tension and uncertainty → answer {name}'s questions → takeaway. It paraphrases, never claims to be live, and never quotes more than a few words.
  - `memo_source(brief, dossier, credit, excluded)`: transcript, core idea, key ideas, questions, `LIVE RESEARCH: [R1] claim — Outlet (date)`, and still unclear.
- **Research goes into the SOURCE, not into context items.** `format_context` (`prompts.py:202-207`) renders every background item as "use silently, never mention it", which blocks citing outlets.
- **`fake_gemini.py`** gets new branches:
  - Audio input → MemoBrief JSON (next to the image branch at line 88).
  - `tools` present → grounded steps with `url_citation` annotations.
  - Model starting with `lyria` → a `lameenc` tone MP3.
  - `POST /_config {"lyria_fail": true}` makes Lyria fail.

### Phase 4: job and API
- **`app/memo.py`**
  - **Voice length:** `voice_minutes(m, music_on) = max(2, m-1) if music_on else m`. The music adds about 25–35 s, and `OVERTIME_FACTOR` caps the result near the live pods' range.
  - **Building the request:**
    - `build_request(brief, options, user)` sets `mode="recorded"` and `direction=MEMO_DIRECTION`.
    - Precedence is options.template > vibe/style > brief.format. Host overrides win, and `_reconcile_hosts` (`llm.py:177`) still enforces voice gender.
  - **Review edits:** `apply_edits(plan, ProduceMemo, minutes)` re-normalizes the beat minutes (`llm._normalize_minutes`, 224) and drops the cold open if beat 1 was edited.
  - **`MemoJob`:**
    - `prepare()` runs the stages, then either stops at `review` or takes a `renders.slot()` and runs `_produce()`.
    - Progress is written through `Persister` (`store/__init__.py:114`), which coalesces writes.
    - Render settings: a `settings.model_copy(...)` with `segment_seconds=40`, `lines_per_segment=8`, writer timeout 45 s and `speech_retries=1`.
    - **On save:**
      - Shift each segment's `start` with `mix.shift`.
      - Rebuild beats with `beats_with_times(chapter_start(...))`.
      - Set `samples`/`duration_s` to the mixed length, plus `music.regions`.
      - Set `counts` with zeros for the live-only counts and `listener_timeline=[]`.
      - The replay transcript sync (`static/js/live/store.js:67,91,174-210`) depends on these shifted offsets.
  - **`Renders`** registry, like `Rooms` (`room.py:671`): `get/add/remove`, `active_for(uid)`, an FIFO `slot()` limited to `max_renders`, and `shutdown()` (cancels, never refunds).
  - **`settle(store, uid, show_id, keep, note)`** is ledger-based: it refunds `spent − keep`, so running it twice refunds nothing the second time.
  - **`sweep()`** runs on startup: shows still `rendering` are marked `failed` and settled. Pods in `review` survive.
- **`app/main.py`:**
  - Factor a `_new_show_doc()` helper out of `create_show` (749-783).
  - `POST /api/memos` (multipart: `audio`, plus `options` as JSON → `MemoOptions`):
    - Checks: `_require_origin`, `require_user`, the rate limit, `read(cap+1)`, `sniff_audio`, render capacity, one active render per user, credits.
    - Creates the doc with `status:"rendering"` and stores `memo.{ext}`.
    - **Charges at upload in one-shot mode** (like `create_show` with live, 784). Music credits are a separate `reason="music"` entry.
  - `POST /api/memos/{id}/produce` (`ProduceMemo`): for the owner, when the status is `review`, or `failed` with stages already done (Try again). Charges, then resumes. It works after a restart because the source, brief, research and plan draft are all stored.
  - `GET /api/memos/{id}/audio` (owner only) and `GET /api/me/music`.
  - `_show_doc` (179) prefers the in-memory render doc.
  - `_public_doc` (293) adds `origin`, `show_notes` and `render{stage,progress,error}`. `get_show` adds owner-only `memo`, `research` and `plan_draft`. `_list_item` (327) adds `origin` and `render`.
  - `delete_show` (1183) cancels the render, settles, then deletes.
  - `lifespan` (132) runs `memo.sweep` and `renders.shutdown`.
  - `/api/options` adds a `memo` block (caps, `music_credits`, whether generation is enabled, library packs). The page route `/memo` is added.

### Phase 5: frontend (vanilla ES modules, no build)
- **`static/js/audio/recorder.js`**
  - `MediaRecorder` MIME negotiation: webm/opus → ogg/opus → mp4.
  - Mono `getUserMedia` with noise suppression; pause and resume; a timer that auto-stops at 5 minutes; a level meter (`AnalyserNode`).
  - `navigator.wakeLock` while recording; a `beforeunload` guard. Duration comes from the timer, because Chrome reports webm duration as Infinity.
- **`static/js/views/memo.js`**
  - Record or upload (`accept="audio/*"`), preview, re-record.
  - Length chips 5/10/15, default 15. A "Review outline first" toggle.
  - **Music cards:**
    - "Studio library (included)"
    - "Generate original music · +N credits · takes a few extra minutes"
    - "Your music: …"
    - "No music"
  - Format "Auto (picked from your memo)" or a template/vibe/host override.
  - Visibility, sharing, albums and tags; reuse the Create screen's controls by pulling them into `static/js/pod-options.js`.
  - A cost line.
- **`static/js/views/memo-status.js`**, routed from `views/show.js` (before line 36) by status:
  - **Render screen:** a stage list with progress, "you can leave this page", a `getShow` poll every 3 s, and "Stop and delete".
  - **Review screen:**
    - The transcript and the memo's playback.
    - Findings with include toggles and source links.
    - Search Suggestions in a sandboxed `<iframe srcdoc>`, never `raw()`.
    - Editable title and beats (3–10).
    - Length and music, and "Produce · uses N credits".
  - **Failed (owner):** the reason and "Try again".
- **Elsewhere:**
  - `api.js`: `createMemo`, `produceMemo`, `memoAudioUrl`, `myMusic`.
  - `main.js`: the `/memo` route.
  - Library rows (`views/library.js:240-247`): "Producing… 40%", "Ready for review". `ledgerLabel` gets a `music` case.
  - A "Voice memo" entry next to New pod, plus a link on `create.js`.
  - A `showNotes` card (summary, "From a voice memo by …", sources, music credit) in `now-playing.js` and `studio.js`.
  - `static/css/memo.css`. Reuse the existing `mic` icon (`js/icons.js:37`).

### Phase 6: real API, library, docs
- `tests/live_test.py --memo FILE [--memo-minutes 15] [--music library|generate|none] [--review]`. It writes the MP3, transcript, show notes and per-stage timings into `data/live-tests/<ts>/`.
- Build the library: `python tools/build_music_library.py --lyria` (you run it; it costs API credits). Commit `music/`.
- README (feature, statuses, config, code map), `.env.example`, Dockerfile `COPY music ./music`. No apt packages. `deploy.sh` stays unchanged.

## Credits and failures (summary)
| Case | Rule |
|---|---|
| One-shot upload | Charge `cost(minutes)` (+ `music_credits` if generating) right away. A 402 refunds any partial charge. |
| Review upload | Balance check only; charge at Produce. Abandoned reviews are free; the rate limit caps their cost. |
| Finished | `settle(keep = ceil(final_mixed_s/60·cpm) + music_credits if generated music was actually used)` |
| Lyria fails or times out | Use the library (or no music); the music credits come back at settle. |
| Research fails | **Never fatal.** The hosts stick to the memo, and the detail line says so. |
| Transcription unusable, planner fails, <50% of the voice voiced, save fails, delete mid-render, restart sweep | `settle(keep=0)` refunds everything. A mix failure saves the unmixed voice instead. |

## Config (`app/config.py`, with defaults)
`memo_max_mb=14`, `memo_max_seconds=600`, `max_research_queries=5`, `research_timeout_seconds=120`, `max_renders=1` (raise after Step 0 measures TTS), `max_queued_renders=6`, `render_max_minutes=30`, `render_segment_seconds=40`, `render_lines_per_segment=8`, `render_write_timeout_seconds=45`, `render_mp3_kbps=64`, `music_dir="music"`, `music_model="lyria-3-clip-preview"` (empty turns off Generate), `music_credits=2`, `music_timeout_seconds=120`.

## Verification
```
.venv\Scripts\python -m tests.unit_test        # existing 33 + new
.venv\Scripts\python -m tests.smoke_test       # existing 68 + memo flows below
.venv\Scripts\python -m tests.store_contract
.venv\Scripts\python tools\probe_memo_apis.py --memo <chrome.webm> <iphone.mp4> <voicememo.m4a>
.venv\Scripts\python -m tests.live_test --memo <walk.m4a> --memo-minutes 15 --music generate
```
- **Unit:**
  - `sniff_audio`; `published()`; an injected plan skips `plan_show`; the speech retry; live prompts byte-identical; `template_request` voices match gender.
  - Mixer: offsets and contiguity, total length = voice + inserts, ducking level, peak ≤ 0.95, passthrough.
  - `Library.pick` and the empty library.
  - Headless `ShowRunner` + `RenderRoom` with a stub Gemini reaches `signed_off` with nobody listening.
  - `sweep` run twice refunds once.
- **Smoke (fake Gemini + temp music library):**
  1. **One-shot, public, library music:**
     - Invisible to others and not in Discover while rendering.
     - Finishes with `segments[0].start == intro_len`.
     - Gaps at chapter boundaries equal the sting length.
     - `samples == voice + inserts`.
     - The MP3 serves with Range requests.
     - The ledger nets correctly.
     - The writer prompts contain `[R1]` and `EPISODE DIRECTION`.
  2. **Review + generate with `lyria_fail`:**
     - Stops at review, and only the owner sees the brief, research and plan.
     - Produce with an edited beat, and the edit carries through.
     - Falls back to the library and refunds the music credits.
  3. Bad file → 422; too big → 413; a second render → 409.
  4. Delete mid-render refunds everything.
- **Manual:**
  - Record on Chrome desktop and Android and on iPhone Safari; upload an iOS Voice Memos file.
  - Replay: captions are blank during the intro and stings, transcript sync is correct after each sting, the chapter bar is right, and the MP3 length matches `duration_s`.
  - Killing the server mid-render gets swept and refunded on restart.

## Risks
1. **Grounding terms.** You accepted this for every visibility setting. Mitigations: Search Suggestions are shown to the owner, source links are listed in the show notes, and the hosts paraphrase.
2. **Cloud Run scale-in.** `--min-instances 0` (`deploy/deploy.sh:125`) means a render with no open request can be stopped. It's covered by the sweep plus Try again. If it happens often, use `--min-instances 1` (your cost call).
3. **TTS and rate limits next to up to 3 live pods.** Start with `max_renders=1` and measure in Step 0 and the live test.
4. **iOS:** whether `audio/mp4` is accepted is unconfirmed, and a locked screen can stop recording. Suggest "record in Voice Memos, then upload" for walks.
5. **Lyria:** latency, vocals leaking in, safety blocks and the SynthID watermark, which is disclosed in the show notes. All have fallbacks.
6. **Hosts saying "we're live".** Recorded mode plus the direction text counter it.
7. **Merge conflicts with the `cohost` worktree** in `catalog.py`, `config.py`, `models.py` and `prompts.py`. Keep those edits additive.

## After approval
- Update memory (`gemini-podcasts-decisions.md`) with this feature's decisions:
  - music: library by default, paid Lyria opt-in with caching
  - optional review
  - no memo clips in v1
  - auto hosts with override
  - Google grounding for every visibility setting, with the terms risk accepted
