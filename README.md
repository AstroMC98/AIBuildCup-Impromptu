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
- **Personas** and **Styles** pages: keep speakers and styles to reuse. Copy any template
  host into your personas as a starting point.
- **Listen**: big live captions, a chapter bar, Back 15s, Jump to live, and a Now
  playing view with the live transcript.
- **Call in**: type a message; the hosts read it out with your name and react in
  character. Rude call-ins are skipped. Attach a file, link or photo and they use it too.
- **Add context**: text, a file, a link or a photo, used **right now**, at the **next
  chapter**, or as **background only**, credited on air or not.
- **Restyle** (creator): switch the vibe mid-show; the hosts switch within a couple of
  lines and the whole screen re-tints.
- **Your pods / Shared / Discover**: every pod is saved (audio, transcript, call-ins,
  context) to replay or download.

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

**2. Deploy** (from Git Bash on Windows, or any shell; safe to re-run, each run ships a new image)

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
| Firestore (native) | `shows/{id}` (+ segments, comments, context), `users/{uid}` (+ ledger, personas, styles, connections) | accounts, credits, library, transcripts |
| Cloud Storage `gs://PROJECT-podcasts` | source text, context text, `episode.mp3` | replays and downloads (served by signed URL) |
| Secret Manager | `gemini-api-key`, `session-secret` | injected as env vars |
| Firebase Authentication | Google + email sign-in | accounts; the server verifies ID tokens itself |
| Service account `podcasts-run` | Firestore user, bucket object admin, secret accessor, token creator on itself | least privilege; the last one signs replay URLs |
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
  process. `MAX_LIVE_SHOWS` (default 3) caps concurrent pods. To go beyond one
  instance, keep a Firestore lease saying which instance owns each live pod and fan
  events out through Memorystore (Redis) pub/sub; nothing else needs to change.
- **Deploy between pods.** A new revision ends pods that are live at that moment;
  they're saved as *interrupted* with the audio so far, and unused credits are refunded.
- **WebSockets last up to 60 minutes** on Cloud Run. Clients reconnect on their own and
  resume from the exact sample they had.
- **Queries** use one indexed field each (owner, visibility, or shared-with) and sort in
  the app, so Firestore needs no composite indexes. Past a few thousand pods per list,
  add an index on (visibility, created_at) and page the queries.
- Gemini TTS voices at most two speakers per request, so pods have one or two hosts.

## Security notes

- Sign-in: the browser signs in with Firebase and hands the server the ID token once;
  the server verifies it (signature, audience, expiry) with google-auth and sets its own
  HMAC-signed session cookie: `HttpOnly`, `SameSite=Lax`, `Secure` on Cloud Run, 30 days.
  Email + password accounts must verify their address first, so nobody can claim an
  admin's email. `AUTH_MODE=dev` can't be enabled with the Google Cloud backend.
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
- Looking someone up by email tells you whether they have an account. Fine for a demo;
  for a public launch, send requests by invite link only.

## Tuning

| Env var | Default | Effect |
|---|---|---|
| `CREDITS_PER_MINUTE` | `1` | what a pod costs per minute of requested length |
| `SIGNUP_CREDITS` | `0` | credits new accounts start with |
| `ADMIN_EMAILS` | | who can grant credits and delete any pod |
| `TTS_MODEL` | `gemini-3.8-flash-tts` | `gemini-3.8-flash-lite-tts` is faster and cheaper |
| `SCRIPT_MODEL` | `gemini-3.8-flash` | writes the plan and every segment, describes photos |
| `SCRIPT_THINKING_LEVEL` | `low` | `medium` writes better jokes but adds latency per segment |
| `SEGMENT_SECONDS` | `20` | audio per segment; lower = faster call-in replies, more API calls |
| `LINES_PER_SEGMENT` | `5` | the most lines in one segment |
| `LOW_WATER_SECONDS` | `8` | lower = faster replies, more risk of silent gaps |
| `MAX_REWRITES` | `2` | caps redo loops when input pours in |
| `MAX_LIVE_SHOWS` | `3` | live pods at once |
| `MAX_LISTENERS` | `50` | listeners per pod |
| `IDLE_END_SECONDS` | `300` | nobody connected this long: the hosts sign off and it's saved |

If you hit 429s from the TTS model, raise `SEGMENT_SECONDS` (fewer, longer calls) or
switch to the lite TTS model.

## Testing without an API key

```bash
python -m tests.unit_test      # clock, limits, sessions, Firebase checks, credits, connections,
                               # access rules, SSRF guard, audio, store, show loop, prompts
python -m tests.smoke_test     # full pods over real sockets against a Gemini stub
```

The smoke test signs in four people (an admin, her connection, a stranger, and a
signed-out visitor) and runs pods at 20x speed: credits (none, granted, charged,
refunded), personas and styles, connection requests, every visibility rule, call-ins
(answered under the account name, declined, rate limited), context and photos, a
restyle, reactions, a late joiner, a reconnect, saving and replay with Range requests,
publishing, a public live pod with a visitor, drafts, and a pod that ends itself.

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
  clock.py     LiveClock: the server-side live playhead
  audio.py     incremental MP3 encoder and the rolling PCM window
  llm.py       the only module that talks to Gemini
  prompts.py   planner, writer and image prompts; JSON schemas
  ingest.py    text / file / link / photo -> text, with per-role caps
  sources.py   file and link extraction, SSRF guard, image sniffing
  limits.py    rate limits and size caps
  store/       Store interface; local.py (JSON on disk), gcp.py (Firestore + GCS)
  catalog.py   voices, vocal tags, style presets, templates
  config.py    settings from env / .env
static/        no build step: index.html, css/, js/ (ES modules)
  js/firebase.js   Firebase sign-in (loaded only in firebase mode)
  js/live/         socket, store (selectors by playhead), controller
  js/audio/        timeline, live player (WebAudio pull scheduler), replay player
  js/views/        login, library (home, header, account menu), create, show (phone: live-mobile +
                   now-playing; desktop: studio), saved (personas, styles), connections, admin
deploy/deploy.sh   one-command Cloud Run setup
tests/           fake_gemini.py (Interactions API stub), smoke_test.py, unit_test.py
```
