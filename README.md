# Gemini Podcasts

Feed it anything, pick a vibe or a template, and it runs a live AI podcast ("a pod")
that you and your connections can call in to. "Genesis, but girl gossip." "Your Q3
numbers as sports play-by-play." "A research paper as a science deep-dive." The script
is written a few lines at a time while you listen, so when someone calls in, the hosts
read it out and answer on air. You can hand them more material mid-show, or switch the
whole style.

## Run it locally

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # paste your GEMINI_API_KEY; put your email in ADMIN_EMAILS
uvicorn app.main:app --reload
```

Open http://localhost:8000 and sign in. Locally, `AUTH_MODE=dev` lets you sign in with any
name and email (use the one in `ADMIN_EMAILS` to be an admin), so you can try several
accounts at once in different browsers. Grant yourself credits on the **Admin** page.

## Accounts, credits and sharing

- **Accounts** come from Firebase Authentication: Google sign-in, or email and password
  (the address must be verified before it can sign in).
- **Credits.** Creating a pod costs credits: `CREDITS_PER_MINUTE` (default 1) times its
  length, charged when it goes live. Minutes it didn't use come back when it ends
  (all of them if it never got going). Listening, calling in, connecting, and making
  personas and styles are free. There are no payments in this demo: admins
  (`ADMIN_EMAILS`) grant credits on the Admin page, and new accounts start with
  `SIGNUP_CREDITS` (default 0, so new accounts are free accounts).
- **Connections** are mutual: send a request by email or share your invite link; the
  other person accepts.
- **Who can listen** is set per pod, when you create it or any time later from Share:
  - **Private**: just you, plus connections you pick by name
  - **Connections**: all your connections
  - **Public** (published): anyone, even without an account, and it's listed on
    **Discover**. Calling in still needs an account.
  Anyone who can listen can call in, react and add context; only the creator can
  restyle, end, re-share or delete a pod. Admins can delete any pod.

## What you can do

- **Create**: type a topic, paste text, add files (PDF, Word, text, CSV, HTML), links
  or photos. Start from a **template**, pick a vibe or one of **your styles**, or describe
  your own. Cast the hosts from **your personas** (name, who they are, voice), choose a
  length and who can listen. **Go live**, or **Save for later** as a draft.
- **Templates**: Science deep-dive (one expert host, long-form), Married couple chat,
  Inspirational talk, Girl talk, Masculine talk, Debate night. Each sets the style, the
  hosts (names, personas, voices) and a length; change anything before going live.
  They're named after formats, not real shows or people, so published pods never pass
  themselves off as someone else's podcast.
- **Personas** and **Styles** pages: keep speakers and styles to reuse. A persona has a
  name, gender and a voice of that gender, a personality, likes, specialties and skills,
  and **private notes** (typed, or pulled from a file or link) that only that host draws
  on: listeners never see them. Cast one of 13 **premade personas** as is, or customize it.
  Every host's voice matches their gender, whoever picks it.
- **Albums and tags**: group pods into albums, like playlists (a pod can be in several).
  An album is private, for connections, or public with its own page; people only see the
  pods in it they could hear anyway. Tags filter your pods.
- **Subscribe** to a creator or an album. **Following** lists their new pods, with a
  "New" badge and a "Live now" banner. Every creator has a page with their pods and albums.
- **Stats** (creator only): listeners now and at peak, unique listeners, average listen,
  who stayed to the end, a chart of listening over the show, when people left, replays and
  how far they got, and the names of signed-in listeners. Anonymous listeners are counted
  (by a random id their browser keeps), never named.
- **Listen**: big live captions, a chapter bar, Back 15s, Jump to live, and a Now
  playing view with the live transcript.
- **Call in**: type a message; the hosts read it out with your name and react in
  character. Rude call-ins are skipped. Attach a file, link or photo and they use it too.
  If the hosts say they'll come back to a call-in, they do: it's kept as *promised* and
  handed back to every segment until they answer it (the sign-off answers whatever's
  left), even when the writers marked it answered while saying "we'll get to that".
- **Research** (optional when creating, +`RESEARCH_CREDITS_PER_MINUTE`): when a call-in
  or a chapter needs facts the source doesn't have (latest stats, recent news), the hosts
  look them up with Google Search while they keep talking ("let me pull up his numbers"),
  then answer and name the outlet. Lookups show in the context list with their sources
  and Google's Search Suggestions. Without Research, the hosts answer from what they know.
- **Seek**: click or drag the progress bar, or tap a chapter, to jump there (a live
  pod: back as far as your browser holds, about 5 minutes); arrow keys move 10 s.
- **Add context**: text, a file, a link or a photo, used **right now**, at the **next
  chapter**, or as **background only**, credited on air or not.
- **Restyle** (creator): switch the vibe mid-show; the hosts switch within a couple of
  lines and the whole screen re-tints.
- **Interrupt** (creator): hold to talk or type, and the show stops where you are. A
  host reacts at once (pre-voiced), answers your question, then picks the thread back
  up. **Go deeper** gives the current chapter more time, **Move on** skips ahead, and
  **Steer** ("argue the other side") re-plans the rest of the episode. Audio that was
  voiced but never heard is dropped, so the recording is exactly what was heard.
- **Briefings**: a private pod made just for you from your **watchlist**: sites to
  monitor (new posts, via their RSS/Atom feed or new headline links), links to watch
  (what was added or removed on a pricing page, a status page, an agenda), and
  **searches** to follow in the news (Gemini with Google Search). Pick a premade
  **briefing template** (Morning roundup, Five-minute focus, Weekend deep-dive, Industry
  watch) or make your own: hosts, style, your topics, and what's in it. Make one on
  demand (it goes live, so you can interrupt it), or set a time and days and one is
  waiting for you, made ahead with nobody listening. 1 credit per 5 minutes, unused
  5-minute blocks refunded. Briefings are never shared or listed with your pods, and
  are deleted after 48 hours.
- **Your pods / Shared / Discover**: every pod is saved (audio, transcript, call-ins,
  context) to replay or download.
- **Co-host live**: talk into your phone and an AI co-host (one of your personas, or a
  premade one) banters with you, interviews you or debates you in real time. Switch
  modes mid-call. Hang up and it's edited into an episode in seconds: dead air
  tightened, flubs and crosstalk cut, levelled, titled, with show notes and chapters.
  See [Co-host live](#co-host-live).

## How a live pod runs

```
browsers (creator + listeners)        server: one ShowRoom per live pod               Gemini
──────────────────────────────        ─────────────────────────────────               ──────
POST /api/shows (credits charged) ──▶ plan_show() ───────────────────────────────────▶ gemini-3.8-flash
WS join (cookie, access checked) ───▶ snapshot + audio from just behind live            (JSON schema)
                                       │   the plan carries the cold open (first 2-4 lines),
                                       │   voiced at once as segment 1
                                       ┌─▶ write_segment() ─────────────────────────────▶ gemini-3.8-flash
                                       │     the next ~20 s of lines as JSON        ◀────
player {playing} ───▶ LiveClock        │   wait until live playhead is < 8 s from the end
                      (server-side)    │     (new call-in, due context, restyle?  throw the
                                       │      draft away and rewrite it)
◀──── segment (lines) ─────────────────│   commit
◀──── <seq><start_sample> + PCM ───────│   stream_speech() ──────────────────────────────▶ gemini-3.8-flash-tts
                                       │     while N+1 is already being written     ◀──── 24 kHz PCM stream
                                       └── MP3 encoded as it goes; Firestore/GCS written in the background
comment / react / context ───────────▶ queued, offered to the next draft, "Answered at 3:52"
ends ────────────────────────────────▶ saved; unused minutes refunded
```

What makes it feel live:

- **Audio starts before the show is written.** Nothing waits for the whole episode. The
  planner writes the cold open along with the plan, so speech starts about 12-15 s after
  Go live; each later segment (~20 s of audio, sized in words from the measured speaking
  pace) is written while the one before it plays.
- **A short buffer.** The server voices the next segment only when the live playhead
  is within `LOW_WATER_SECONDS` of the end of the voiced audio. Anything written but not
  yet voiced can still change.
- **Stale drafts get rewritten.** A finished draft that hasn't seen a waiting call-in,
  context that's due, a restyle or an end request is thrown away and rewritten (at most
  `MAX_REWRITES` times, which also caps cost when call-ins pour in). If there isn't
  enough audio left to rewrite without a silent gap, the draft plays as written and the
  next segment picks the input up.
- **The server keeps the clock.** The live playhead advances while at least one
  listener is playing and never passes the voiced audio. If everyone pauses, the pod
  holds and nothing is voiced (or billed). Pause, Back 15s and Jump to live are each
  listener's own; late joiners start at the live edge with ~20 s of audio behind them.
- **Every audio frame says where it belongs** (`<seq><start_sample>` header), so a
  phone that drops off reconnects and resumes exactly where it was.

Captions are timed by spreading each segment's audio over its lines by length. Lines
written ahead are never shown early; call-in statuses ("On air in about 15 s",
"Up next · on air in 6 s", "On air now", "Answered at 1:42") follow each listener's own
playhead. The server places each waiting call-in on the show timeline (the next segment
if a rewrite still fits, else the one after) and moves it when the timeline shifts.

## Co-host live

A call is you and one AI co-host, solo (no live audience); the finished episode gets
the usual visibility. It costs like a pod: the length you pick (5, 10 or 15 minutes)
is reserved when the call starts and unused minutes come back when it's saved.

```
phone                                  server: one CohostSession per call             Gemini Live
─────                                  ──────────────────────────────────             ───────────
mic → AudioWorklet → 16 kHz, 40 ms ─WS─▶ forwarded at once ───────────────────────────▶ gemini-3.1-flash-live
  frames, sent the whole call            + recorded: the call's clock                    (server VAD, barge-in,
speaker ◀── played on arrival ◀─────── <turn><24 kHz PCM> ◀── its voice ◀───────────────  transcripts both ways)
  (flushed when you cut in)              + recorded where it started playing
captions ◀─────────────────────────── both sides' words, live
hang up ─────────────────────────────▶ editor's notes (gemini-3.8-flash: title, show notes, chapters, cuts)
                                       + numpy edit → MP3 → saved as a normal pod (kind "cohost")
```

What keeps it quick:

- **Nothing waits on the way through.** Mic frames go straight to the Live session and
  its voice straight to the phone; recording is in memory. The phone plays the
  co-host as each chunk lands (a ~60 ms cushion), on an `interactive` AudioContext.
- **The co-host opens the show** the moment you join, so there's no dead air at the start.
- **Cutting in.** Gemini stops an answer when it hears you over it. Its audio arrives
  faster than it plays, though, so the phone listens too: talk over the co-host and it
  ducks at once; keep talking (0.4 s) and the phone stops it and tells the server.
- **Turn-taking** is Gemini's voice activity detection; `COHOST_SILENCE_MS` is how long
  you pause before it answers.
- **Connections are recycled** by Gemini every few minutes (GoAway); the server
  reconnects with a resumption handle, so the co-host keeps the whole conversation.
  If the phone drops off, the call waits `COHOST_RECONNECT_SECONDS`, then saves.

What hanging up does (`app/editing.py`, then `app/cohost.py`):

1. Your mic is one continuous track (the phone sends silence too, so it's the call's
   clock); each co-host turn sits where it started playing.
2. **Crosstalk**: your mic is muted while the co-host talks unless you cut in (that's
   speaker echo or an "mm-hm"); an answer you cut into ends where you did, and one cut
   off in its first 1.5 s goes.
3. **Dead air**: silence before and after every turn is trimmed; pauses over 1.2 s
   become 0.6 s.
4. **Flubs**: your talking is split into phrases at the pauses and matched to the live
   transcript; the editor (a Gemini call) marks false starts, mic checks and hang-up
   chatter, plus answers to them, and those are cut at the pauses around them.
5. **Polish**: every turn is levelled to the same loudness, peaks are softly limited,
   every cut gets a short fade, and a chime opens and closes the episode.
6. Saved as an ordinary pod: one segment per turn (so replay captions are exact), the
   editor's title, show notes and chapters, `episode.mp3`, and `raw.mp3`, the call
   before editing, which its owner can download from the More menu.

If the editor's call fails or takes longer than `COHOST_EDIT_TIMEOUT_SECONDS`, the
episode is saved with the mechanical edits only. A call where nothing was said leaves
no pod and refunds everything.

The mic needs a secure page: `localhost` works, a phone on your LAN over plain `http://`
doesn't (use the Cloud Run URL or a tunnel). Headphones keep the co-host from hearing
itself; echo cancellation is on, but phone speakers vary.

## Deploy to Google Cloud

You need a Google Cloud project with billing, plus `gcloud` (logged in) and Docker.

**1. Project and Firebase sign-in (once, in the console)**

1. Create the project and link billing:
   `gcloud projects create gemini-podcasts-123`, then
   https://console.cloud.google.com/billing/linkedaccount?project=gemini-podcasts-123
2. In the [Firebase console](https://console.firebase.google.com/), **Add project** and
   pick that same Google Cloud project.
3. **Authentication > Get started > Sign-in method**: enable **Google** and
   **Email/Password**.
4. **Project settings > General > Your apps > Web app (</>)**: register an app (no
   hosting needed) and copy its `apiKey`. That's `FIREBASE_API_KEY`. It's public by
   design: it identifies the project, it doesn't grant access.

**2. Deploy** (from Git Bash on Windows, Cloud Shell, or any shell; safe to re-run, each
run ships a new image. `.gitattributes` keeps the script's line endings Unix-style even on
Windows checkouts.)

```bash
FIREBASE_API_KEY=AIza... ADMIN_EMAILS=you@example.com \
  deploy/deploy.sh gemini-podcasts-123 europe-west2
```

The first run asks for your Gemini API key and keeps it in Secret Manager.

**3. Authorize the domain (once).** The script prints the Cloud Run address. Add it under
Firebase **Authentication > Settings > Authorized domains**, or Google sign-in will
refuse to open on it. Then sign in with your admin email and grant credits from Admin.

What the script sets up:

| Piece | What | Why |
|---|---|---|
| Cloud Run `podcasts` | the app, 1 instance, 2 vCPU, 2 GiB, 60-min request timeout | WebSockets + the live pod loop |
| Firestore (native) | `shows/{id}` (+ segments, comments, context, sessions), `users/{uid}` (+ ledger, personas, styles, connections, watches, watch_pages, briefing_templates), `albums/{id}`, `subscriptions/{id}` | accounts, credits, library, transcripts, albums, following, listening stats, briefings |
| Cloud Tasks `briefings-wake` | one task per scheduled briefing start, calling `/healthz` | wakes the scaled-to-zero service to make a scheduled briefing |
| Firestore security rules | deny all browser access (`deploy/firestore.rules`, released on every run) | the app only uses Firestore from the server; the Firebase web key is public |
| Cloud Storage `gs://PROJECT-podcasts` | source text, context text, `episode.mp3`, `raw.mp3` (co-hosted calls) | replays and downloads (served by signed URL) |
| Secret Manager | `gemini-api-key`, `session-secret` | injected as env vars |
| Firebase Authentication | Google + email sign-in | accounts; the server verifies ID tokens itself |
| Service account `podcasts-run` | Firestore user, bucket object admin, secret accessor, token creator on itself, Cloud Tasks enqueuer | least privilege; token creator signs replay URLs, enqueuer books wake-ups |
| Artifact Registry `podcasts` | container images | |

To change a setting later (admins, signup credits), re-run the script with new values.
To rotate the Gemini key:

```bash
printf '%s' 'new key' | gcloud secrets versions add gemini-api-key --data-file=- --project PROJECT
deploy/deploy.sh PROJECT REGION
```

If your organization blocks public services ("Domain restricted sharing"), replace
`--allow-unauthenticated` in the script with `--no-invoker-iam-check`. The app does its
own access control.

### Scaling and limits

- **One instance, on purpose.** Live rooms (listeners, the clock, audio being voiced)
  live in that instance's memory, so every listener of a pod must reach the same
  process. `MAX_LIVE_SHOWS` (default 3) caps concurrent pods and co-host calls together. To go beyond one
  instance, keep a Firestore lease saying which instance owns each live pod and fan
  events out through Memorystore (Redis) pub/sub; nothing else needs to change.
- **Deploy between pods.** A new revision ends pods that are live at that moment;
  they're saved as *interrupted* with the audio so far, and unused credits are refunded.
- **WebSockets last up to 60 minutes** on Cloud Run. Clients reconnect on their own and
  resume from the exact sample they had.
- **Queries** use one indexed field each (owner, visibility, shared-with, album, or a
  subscription's subscriber or target, counted with Firestore's count aggregation) and
  sort in the app, so Firestore needs no composite indexes. Past a few thousand pods per
  list, add an index on (visibility, created_at) and page the queries.
- Gemini TTS voices at most two speakers per request, so pods have one or two hosts.
- **Scheduled briefings wake the service.** Cloud Run scales to zero, so saving a
  schedule books a Cloud Task (queue `briefings-wake`, made by `deploy.sh`) that calls
  `/healthz` at the start time; that starts an instance, whose briefing loop makes the
  briefing headless and books the next day's wake-up. Nothing stays warm in between:
  with `--no-cpu-throttling` an idle instance is billed, so pinging every few minutes
  would cost far more. Expired briefings are deleted by the same loop.
- **Search watches cost real Google Search grounding.** Each check of a search is one
  grounded Gemini call (it may run a few Google queries), made only when a briefing
  needs fresh results or you press Check now; each person keeps at most 5 searches.

## Security notes

- Sign-in: the browser signs in with Firebase and hands the server the ID token once;
  the server verifies it (signature, audience, expiry) with google-auth and sets its own
  HMAC-signed session cookie: `HttpOnly`, `SameSite=Lax`, `Secure` on Cloud Run, 30 days.
  An unverified email address is refused whatever the sign-in provider (Google addresses
  are always verified; email + password accounts verify from their inbox), so nobody can
  claim an admin's email. `AUTH_MODE=dev` can't be enabled with the Google Cloud backend.
- Firestore is reached only by the server's service account. Its security rules deny all
  browser access, so the public Firebase web key can't be used to read data directly.
- A persona's private notes go to the writers only: they're stripped from every pod,
  snapshot and replay that listeners load. Listening stats are for the pod's creator;
  anonymous listeners are counted by a random id, never identified.
- Access to a pod is checked on every API call and when the WebSocket opens; drafts are
  visible only to their creator; sharing by name is limited to your connections.
- Credits change in a Firestore transaction together with their ledger entry, so a
  balance can't go negative under concurrent requests.
- Listeners are rate limited (call-ins, reactions, context, messages) and size limited
  (see `app/limits.py`, `app/ingest.py`). Links they submit are fetched only from public
  IP addresses, re-checked on every redirect, capped at 5 MB. Rate limits key on the
  client address Cloud Run appends, which callers can't forge.
- Everything people type is escaped before it reaches anyone's screen
  (`static/js/html.js`); profile photos must be `https://` URLs. Call-ins are audience
  messages, never instructions: the writers can play along but never drop the style,
  language or format, and they skip hateful or private ones. The creator can remove a
  queued call-in; admins can delete any pod.
- Photos are described once by Gemini; only the description is stored.
- Watched sites and links go through the same public-address guard as listener links,
  name the app in the User-Agent, honor robots.txt, use conditional requests, and space
  out requests to a host. Feeds are parsed with `defusedxml` (no entity tricks). What
  they say reaches the writers fenced as material to report on, never as instructions,
  and the hosts summarize and name the source rather than read articles out.
- Search watches use Grounding with Google Search. Google asks apps to show the Search
  Suggestions that come with grounded results: they're shown on the watch and on each
  briefing that used them, rendered from Google's HTML in a sandboxed iframe (no
  scripts; links open in a new tab).
- Briefings are always private: they can't be re-shared or put in albums, they're left
  out of every list, and they're deleted after `BRIEFING_KEEP_HOURS`.
- Looking someone up by email tells you whether they have an account. Fine for a demo;
  for a public launch, send requests by invite link only.

## Tuning

| Env var | Default | Effect |
|---|---|---|
| `CREDITS_PER_MINUTE` | `1` | what a pod costs per minute of requested length |
| `RESEARCH_CREDITS_PER_MINUTE` | `0.5` | extra per minute for a pod with Research on |
| `RESEARCH_MAX_PER_SHOW` | `6` | Google lookups per Research pod (one per call-in) |
| `RESEARCH_MAX_BEATS` | `3` | of those, at most this many for chapters the planner found thin |
| `RESEARCH_TIMEOUT_SECONDS` | `30` | a slower lookup is dropped; the hosts answer with what they know |
| `SIGNUP_CREDITS` | `0` | credits new accounts start with |
| `ADMIN_EMAILS` | | who can grant credits and delete any pod |
| `TTS_MODEL` | `gemini-3.8-flash-tts` | `gemini-3.8-flash-lite-tts` is faster and cheaper |
| `SCRIPT_MODEL` | `gemini-3.8-flash` | writes the plan and every segment, describes photos |
| `SCRIPT_THINKING_LEVEL` | `low` | `medium` writes better jokes but adds latency per segment |
| `SEGMENT_SECONDS` | `20` | audio per segment; lower = faster call-in replies, more API calls |
| `LINES_PER_SEGMENT` | `5` | the most lines in one segment |
| `LOW_WATER_SECONDS` | `8` | lower = faster replies, more risk of silent gaps |
| `MAX_REWRITES` | `2` | caps redo loops when input pours in |
| `INTERJECTION_THINKING_LEVEL` | `low` | answers to an interruption (`gemini-3.8-flash` rejects `minimal`) |
| `MAX_INTERRUPTS` | `30` | interruptions per show (also one per 10 s) |
| `STINGER_DELAY_SECONDS` | `6` | when the hosts' interruption reactions are voiced, after the cold open starts |
| `MAX_LIVE_SHOWS` | `3` | live pods and co-host calls at once |
| `MAX_LISTENERS` | `50` | listeners per pod |
| `IDLE_END_SECONDS` | `300` | nobody connected this long: the hosts sign off and it's saved |
| `COHOST_MODEL` | `gemini-3.1-flash-live-preview` | the Live (native audio) model your co-host talks with |
| `COHOST_SILENCE_MS` | `500` | how long you pause before the co-host answers; lower is snappier but cuts slow talkers off |
| `COHOST_RECONNECT_SECONDS` | `30` | a call whose phone drops off is saved after this long |
| `COHOST_SOURCE_CHARS` | `20000` | notes for the co-host are trimmed to this |
| `COHOST_EDIT_TIMEOUT_SECONDS` | `40` | past this, the episode is saved without the editor's notes |
| `BRIEFING_MINUTES_PER_CREDIT` | `5` | a briefing costs 1 credit per this many minutes |
| `BRIEFING_KEEP_HOURS` | `48` | briefings are deleted after this long |
| `BRIEFING_LEAD_MINUTES` | `30` | a scheduled briefing starts being made this long before its time |
| `BRIEFING_POLL_SECONDS` | `60` | how often due schedules and expired briefings are checked |
| `PUBLIC_URL` | | the app's address: in the watch fetcher's User-Agent, and what wake-ups call (`deploy.sh` sets it) |
| `WAKE_QUEUE` | | Cloud Tasks queue that wakes Cloud Run for scheduled briefings (`deploy.sh` sets it; blank locally) |

If you hit 429s from the TTS model, raise `SEGMENT_SECONDS` (fewer, longer calls) or
switch to the lite TTS model.

## Testing without an API key

```bash
python -m tests.unit_test      # clock, limits, sessions, Firebase checks, credits, connections,
                               # access rules, SSRF guard, audio, store, show loop, prompts
python -m tests.smoke_test     # full pods over real sockets against a Gemini stub
python -m tests.briefing_test  # watchlist (a fake web: feeds, page diffs, robots.txt, search),
                               # run of show, schedules, wake-ups, expiry
python -m tests.briefing_smoke # briefings end to end: on demand, private, refunds, a search,
                               # a scheduled one made with nobody listening
python -m tests.store_contract # LocalStore; with FIRESTORE_EMULATOR_HOST set, GcpStore too
```

`tests/store_contract.py` runs every Firestore query and transaction the app uses
against both backends. To check the Google Cloud one locally (Docker, no Java needed):

```bash
docker run -d --name podcasts-firestore -p 8681:8681 \
  gcr.io/google.com/cloudsdktool/google-cloud-cli:emulators \
  gcloud emulators firestore start --host-port=0.0.0.0:8681
FIRESTORE_EMULATOR_HOST=127.0.0.1:8681 python -m tests.store_contract
```

With a real `GEMINI_API_KEY`, `python -m tests.live_test --minutes 5 10 15 --public` runs
real pods, prints links you can open to listen along, and reports timing, stalls and how
fast call-ins were heard.

The smoke test signs in four people (an admin, her connection, a stranger, and a
signed-out visitor) and runs pods at 20x speed: credits (none, granted, charged,
refunded), personas and styles, connection requests, every visibility rule, call-ins
(answered under the account name, declined, rate limited), context and photos, a
restyle, reactions, a late joiner, a reconnect, saving and replay with Range requests,
publishing, a public live pod with a visitor, drafts, and a pod that ends itself. Then
it co-hosts a call against the stub's Live API, streaming mic audio in real time: the
co-host opening the show, transcripts, cutting in (by Gemini and by the phone), a mode
switch, a recycled Live connection, hanging up, the edit (cuts, dropped answers,
chapters), downloads, refunds, and a call nobody joined. `SMOKE_PORT=18765` moves it
off the default ports.

With a real API key (spends credits):

```bash
python -m tests.live_test --cohost   # a ~2-minute call: your lines voiced by TTS and streamed in
                                     # real time; reports answer latency, cut-in, mode switch,
                                     # hang-up to saved; keeps the episode and the unedited call
```

To click around the UI offline:

```bash
FAKE_BEAT_EVERY=8 uvicorn tests.fake_gemini:app --port 8765 &
GEMINI_API_KEY=fake GEMINI_BASE_URL=http://127.0.0.1:8765 ADMIN_EMAILS=you@example.com uvicorn app.main:app
```

Use a second browser (or `localhost` vs `127.0.0.1`, which keep separate cookies) to be
another user.

## Code map

```
app/
  main.py      routes + the /ws/show/{id} protocol (documented at the top)
  accounts.py  users, signup credits, who can see what, connections
  auth.py      Firebase token checks, session cookies, dev sign-in, origin checks
  room.py      ShowRoom: one live pod, its listeners, audio, snapshots, refunds; Rooms registry
  show.py      ShowRunner: the writing loop (drafts, staleness, commits, voicing)
  cohost.py    CohostSession: a live call with an AI co-host (the /ws/cohost/{id} protocol); Cohosts
  editing.py   a recorded call -> an edited episode (crosstalk, dead air, flubs, levels, chapters)
  clock.py     LiveClock: the server-side live playhead
  audio.py     incremental MP3 encoder and the rolling PCM window
  llm.py       the only module that talks to Gemini
  prompts.py   planner, writer and image prompts; JSON schemas
  ingest.py    text / file / link / photo -> text, with per-role caps
  sources.py   file and link extraction, SSRF guard, image sniffing
  limits.py    rate limits and size caps
  store/       Store interface; local.py (JSON on disk), gcp.py (Firestore + GCS)
  stats.py     listening stats from listening sessions
  briefing.py  personal briefings: templates, run of show, source, schedules, expiry
  watch.py     the watchlist: feeds, page changes, headline links, news searches; polite fetching
  wake.py      Cloud Tasks wake-ups for scheduled briefings on Cloud Run
  catalog.py   voices (with gender), vocal tags, style presets, templates, premade personas
  config.py    settings from env / .env
static/        no build step: index.html, css/, js/ (ES modules)
  js/firebase.js   Firebase sign-in (loaded only in firebase mode)
  js/live/         socket, store (selectors by playhead), controller
  js/audio/        timeline, live player (WebAudio pull scheduler), replay player, listen tracker
  js/cohost/       mic worklet (16 kHz frames), call audio + player, socket, controller (cut-in)
  js/personas.js   persona fields (gender-matched voices, traits, private notes)
  js/social.js     subscribe button, creator chip, album cards
  js/views/        login, library (home, Following, header, account menu), create, show (phone:
                   live-mobile + now-playing; desktop: studio), saved (personas, styles), albums,
                   creator, stats, connections, admin, cohost-setup, cohost-call (co-host live),
                   briefings (make, schedule, watchlist, templates)
deploy/deploy.sh   one-command Cloud Run setup
deploy/firestore.rules  deny-all browser access, released by deploy.sh
tests/           fake_gemini.py (Interactions + Live API stub), smoke_test.py, unit_test.py,
                 store_contract.py (LocalStore vs Firestore emulator), live_test.py (real Gemini;
                 --cohost for a co-host call), briefing_test.py, briefing_smoke.py
```
