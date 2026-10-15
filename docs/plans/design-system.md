# Impromptu design system: migration plan (Live Wire)

Branch `worktree-design-system` (worktree `.claude/worktrees/design-system`), planned 2026-10-15.
Direction **Live Wire** was chosen by the owner on 2026-10-15 over Recital and Switchboard.

**Round 2 (same day)** added screens for the features that landed since the first pass. They are covered in §5.1 and marked NEW in the prototypes:
- briefings
- interrupting a show (branching)
- research and call-in promises
- the seekable timeline
- voice memos and show intros

**Round 3** adapted the prototypes to what merged after that (§5.3): the final voice-memo flow, intros for briefings and co-hosted calls, show notes, and memo pods in the library. The branch now sits on `main` at `07cf067`. Boards that changed are marked UPDATED.

**Implementation (round 4, 2026-10-15).** The system is now in the app code on this branch, not just the prototypes. Every stylesheet was rewritten on the semantic tokens inside `@layer`. No alias layer was kept: nothing reads `--panel`, `--chip`, `--ink` and the rest any more. Smoke (132), unit (100) and briefing (21) tests pass.

| Phase | Done | Not yet |
|---|---|---|
| 0 Foundations | Self-hosted Anybody + Atkinson Next, layers, tokens, gel map, rename, favicon, `.stage` collision fixed (studio is `.studio-stage`), `.bar` scoped to `.bars .bar` | |
| 1 Motion core | `motion.js`, `ring.js`, typed view transitions with the 250 ms cap; sheet drag lives in `ui.js` | |
| 2 Stage | Kinetic captions, analyser `level()` driving `--level`/`--lv`, tally, studio clock, chapter bar, tune-in target, Now playing, desktop studio on stage | Cover FLIP into Now playing |
| 3 Waits | Boot ident, loaders as chase rings, green room, memo making (clock + lamp spine), co-host edit lamps, delayed skeletons | Go-live count-in |
| 4 House | Home composer, Library and Discover grids, tab bar / rail, gel vibe tiles, clock length chips, co-host setup | Live sleeve preview on Create; Draft keeps its old layout on new tokens |
| 5 Sheets | Spring sheets with recede and drag, stage-tinted sheets, cue-card toasts, hearts on arcs | Sliding segmented indicator, call-in lines (bubbles remain), odometer, restyle preview |
| 5b Round 2 | Briefings (gel covers, watch lamps), memo record / making / review, intro, show notes | Interrupt detour visuals, research desk, promised-call-in states, per-watch progress |
| 6 Account | You page, account pages, login on stage | `tests/visual.py`, gel contrast unit test |

Gotcha found while porting: `color-mix(in oklch, <near-grey>, transparent)` loses its hue in Chrome (powerless hue becomes `none`, rendered as 0°), which tints dark greys red. Neutral mixes use `in srgb`; gel mixes stay in `oklch`.

Prototypes (source in `docs/design/impromptu/`, published as private artifacts):

| Prototype | What it settles | File |
|---|---|---|
| [Directions](https://claude.ai/artifact/PiawtqtfAoB8ZcNvgvbMmd) | The three candidate languages side by side; why Live Wire | `directions.html` |
| [Motion Lab](https://claude.ai/artifact/TpoEn8ELZKotaY4MMSUrwz) | Every loading state and screen transition, with timing, the state that drives it, and the reduced-motion version | `motion-lab.html` |
| [Screens](https://claude.ai/artifact/CnvcZszsJka2uMoJ3qHRqn) | All 28 phone screens and 2 desktop layouts, each linked to the transition that leads to it | `screens.html` |

The prototypes are the visual spec. Where this document and a prototype disagree, this document wins and the prototype gets fixed.

---

## 1. What changes, in one page

- **Name.** "Gemini Podcasts" becomes **Impromptu**. The mark is the word set in Anybody italic, with the dot of the i replaced by a red tally lamp. On its own, the lamp-dotted i is the app icon.
- **Type.** Bricolage Grotesque (one family doing every job) is replaced by two families:
  - **Anybody** for display and captions. Its width axis lets long caption lines narrow instead of shrinking.
  - **Atkinson Hyperlegible Next** for everything else. It was drawn for low-vision readers, which suits a product built around live captions.
- **Color.** The single purple ink with an accent mixed in becomes two worlds:
  - **House** is the library side: home, library, create, account. It follows the device's light or dark setting.
  - **Stage** is anything on air: live, now playing, co-host, go-live. It is always dark and lit by the episode's **gel**, a bright light color derived from the vibe's accent, which changes as the chapter changes.
- **Motion.** Today views hard-swap (`router.js` calls `replaceChildren()`), and almost nothing animates. The new system adds:
  - real screen transitions (View Transitions API);
  - spring-based component motion (CSS `linear()` springs);
  - one recurring motif, the **studio clock**: a ring of 60 second-dots that serves as the loader, the count-in, the pod-length picker, the credits meter and the co-host's voice ring.
- **Waiting.** Every spinner and "Loading…" string is replaced by a loader that shows real progress from state the app already has. The Green room, for example, shows the plan's actual title and chapters as they arrive.
- **Navigation (needs your decision, see §9).** The phone gets a 4-tab bar (Home, Discover, Library, You) instead of the account-menu sheet, and Home leads with a composer.

---

## 2. Tokens

All tokens live in `static/css/tokens.css`, inside `@layer tokens`. Old token names stay as aliases until Phase 6 so unported screens keep working (§6).

### 2.1 Color

**House** (follows `prefers-color-scheme`; `data-house="light|dark"` on `<html>` can override it):

| Token | Light | Dark | Use |
|---|---|---|---|
| `--h-bg` | `#F1F3F6` | `#0F1117` | page |
| `--h-bg2` | `#E4E8EE` | `#181B24` | sunken areas, segmented controls |
| `--h-card` | `#FFFFFF` | `#161922` | cards, fields |
| `--h-ink` | `#141720` | `#ECEEF3` | text, primary button fill |
| `--h-ink2` | `#4F5667` | `#9AA1B2` | secondary text |
| `--h-ink3` | `#7A8193` | `#6E7586` | placeholder, disabled |
| `--h-line` | `#D6DBE3` | `#272B36` | 1px rings around cards (house cards use rings, not shadows) |
| `--h-live` | `#C81E3A` | `#FF7A8F` | "Live now" text on house surfaces |

**Stage.** Everything is derived from one registered property, so a chapter change animates the whole screen:

```css
@property --gel { syntax: "<color>"; inherits: true; initial-value: #FFB257; }
.stage {
  --stage:    color-mix(in oklch, var(--gel) 19%, #08090E);   /* the ground */
  --stage-hi: color-mix(in oklch, var(--gel) 46%, #0A0B11);   /* the key light, top-left */
  --glow:     color-mix(in oklch, var(--gel) 72%, transparent);
  --gel-text: color-mix(in oklch, var(--gel) 32%, #FFFFFF);   /* speaker names, the word being said */
  --on-dim:   color-mix(in oklch, var(--gel) 18%, #B9BCC8);   /* secondary text on stage */
  transition: --gel 1.2s ease;
}
```

**Fixed colors**: `--tally: #FF3B30` (on air only, never decorative); waiting-lamp amber `#FFB000`; call-in pink `#FFD7E6`.

**Gels.** The server keeps sending its current dark `accent` values (`app/catalog.py`), so no server change is needed. `theme.js` maps the known preset accents to hand-tuned gels and derives the rest:

| Vibe / template / mode | Server accent | Gel |
|---|---|---|
| Girl gossip, Girl talk, Banter | `#B8336A` | `#FF6B9A` |
| Late-night radio, Debate night | `#3B3F9E` | `#7C83FF` |
| Sports play-by-play, Married couple chat, Debate me | `#B5521C` | `#FF8A4C` |
| True crime, Masculine talk | `#3A4250` | `#A3B1CC` |
| Nature documentary, Science deep-dive | `#1F7F6A` | `#4FD1A5` |
| Rival professors, Inspirational talk | `#80620F` | `#F2C14E` |
| Bedtime story | `#6A4FB0` | `#B69CFF` |
| Morning radio chaos | `#2E6F9E` | `#6FB3FF` |
| Interview me | `#2F6BD0` | `#6FA0FF` |
| House default (no episode) | — | `#FFB257` (tungsten) |
| Custom styles (hashed accent) | any | `oklch(from <accent> max(l, .74) min(calc(c * 1.35), .2) h)` |

Contrast guard: captions are always white or `--gel-text` on `--stage`, whose OKLCH lightness stays under about 0.25 for every gel. A unit test asserts at least 7:1 for caption text and at least 4.5:1 for `--on-dim` across all gels (§8). Sleeve titles sit in the lower, darker half of the sleeve gradient, never over the light corner.

### 2.2 Type

| Role | Family | Size / line height | Weight, width |
|---|---|---|---|
| Wordmark | Anybody italic | as placed | 800, `font-stretch: 112%` |
| Display XL (desktop hero) | Anybody | 61 / 0.95 | 780, 86% |
| Display L | Anybody | 49 / 0.98 | 780, 86% |
| H1 (phone page title) | Anybody | 31 / 1.02 (25 on small phones) | 780, 86% |
| Captions | Anybody | 30 phone, 40 studio / 1.07 | 720; width fits 62–100% (§4.3) |
| H2 / section | Anybody | 20 / 1.1 | 760, 96% |
| Sleeve titles | Anybody | 30–66 / 0.86 | 860, 58% |
| Body | Atkinson Hyperlegible Next | 16 / 1.5 | 400 |
| UI / labels | Atkinson Hyperlegible Next | 14 / 1.4 | 650–800 |
| Micro (meta, never smaller) | Atkinson Hyperlegible Next | 12.5 / 1.35 | 600–700 |

- Scale: major third (×1.25) from 16.
- Every digit that ticks gets `font-variant-numeric: tabular-nums`: clocks, credits, counts.
- There are no ALL-CAPS labels. The only uppercase text is the physical lamp (ON AIR, LIVE) and the call-in pills, because that's what they are in a real studio.

**Fonts are self-hosted.** Drop the Google Fonts `<link>` from `index.html`.
- Put Latin-subset variable WOFF2 files in `static/fonts/`:
  - `anybody-var.woff2`, about 55 KB (axes wdth 50–150, wght 100–900, upright only);
  - `atkinson-next-var.woff2`, about 33 KB.
  - That's 88 KB total, against about 60 KB for Bricolage today.
- Preload both files.
- Declare `@font-face` with `font-stretch: 50% 150%` and `font-display: swap`.
- Add a fallback face, `Arial` with `size-adjust` and `ascent-override` tuned, so the swap doesn't shift captions.
- Anybody italic (another 55 KB) is only used by the wordmark. Ship the wordmark as inline SVG instead and skip that file.

### 2.3 Space, radius, layers

- Space: a 4 px base, using 4, 8, 12, 16, 20, 24, 32, 40, 56. The phone gutter is 16.
- Radius:
  - 6: tally, tags
  - 10: chips, small tiles
  - 12: sleeves, inputs
  - 16: cards
  - 24: sheets
  - 37: the phone frame in prototypes only
  - pill: 999
- Elevation:
  - House cards get a 1 px `--h-line` ring.
  - Shadows are reserved for things that float: sleeves, sheets, cue cards, the play button.
- Z layers: content 0 · sticky bars 20 · Now playing 40 · sheets 200 · cue cards 300.

### 2.4 Motion

Springs are real damped oscillators sampled into CSS `linear()` (the generator is in the prototypes). Each spring carries its own settle time:

| Token | Stiffness / damping | Settles | Use |
|---|---|---|---|
| `--spring-snappy` | 420 / 34 | 430 ms | toggles, chips, indicators, list reorder |
| `--spring-smooth` | 220 / 26 | 590 ms | sheets, cover morphs, caption line entry |
| `--spring-bouncy` | 260 / 16 | 790 ms | celebrations: lamp, chapter cards, cue cards, hearts |
| `--spring-gentle` | 120 / 22 | 850 ms | scene changes: house lights, reveals, folding |
| `--ease-out` | `cubic-bezier(.16,1,.3,1)` | | fades in, small moves |
| `--ease-in` | `cubic-bezier(.7,0,.84,0)` | | exits |
| `--ease-io` | `cubic-bezier(.65,0,.35,1)` | | clip-path wipes, cuts |

Rules:
- Things enter on a spring and leave with `--ease-in` at about 70% of the entry time.
- Stagger 24–40 ms, capped at 8 items.
- Only `transform`, `opacity`, `clip-path` and registered custom properties animate. Layout properties change only inside FLIP.
- Use one orchestrated moment per screen change, never a fade-up on every card.

**Reduced motion.** Every moment in the Motion Lab has its reduced version written down. In general:
- Movement becomes a 150–200 ms opacity change, or nothing.
- Captions show the whole line at once (Recital's surtitle fade).
- Lamps don't flicker.

The blanket `animation-duration: 1ms !important` rule in `tokens.css` goes away, because it also kills the opacity changes that carry meaning.

---

## 3. Architecture changes (no build step, still vanilla)

| Area | Today | New |
|---|---|---|
| CSS order | 10 files, load order is the cascade | `@layer reset, tokens, base, components, house, stage, screens, utilities;` each file declares its layer. Collisions (§7) get fixed as each screen is ported. |
| Accent | `theme.js` mixes hex in JS, sets `--accent/--deep/--ring/--dim` | `applyAccent(el, accent)` sets `--accent` and `--gel` (from the map in §2.1) and nothing else. CSS derives the rest. `meta[name=theme-color]` gets the computed `--stage`. |
| Route changes | `router.dispatch()` → `replaceChildren()`, no transition | `document.startViewTransition({ update, types })`; see §4.1. |
| Motion helpers | none (`util.js reducedMotion()` only) | New `static/js/motion.js` (§4.2). |
| Studio clock | `.spinner` in 3 places | New `static/js/ring.js` component (§4.4). |
| Captions | `components.js captions()` re-renders HTML when the line or chunk changes; size shrinks by a length heuristic | Word spans plus class toggles every frame; width-axis fit; a separate screen-reader line (§4.3). |
| Audio level | only co-host has an `AnalyserNode` | `LivePlayer` gets one: `gain → analyser → destination` (`static/js/audio/live-player.js:21-22`), with a `level()` like `cohost/audio.js:150`. The frame loop writes `--level` on the stage and `--lv` on the speaking host. `ReplayPlayer` uses `createMediaElementSource`. |
| Sheets | `ui.js openSheet()` rises 24 px over 200 ms; close doesn't slide | Spring in, the app recedes to 94%, drag to dismiss (§4.5), same API. |
| Toasts | `util.js toast()` white pill | Cue card with a timer bar, queued (§4.6). |
| Lists | `setHtml` on every refresh (every 15 s on Home) | Rows keyed by `data-id`, re-rendered with FLIP so filters and refreshes move rows instead of flashing them. |

### 3.1 Two worlds on one page

`<html>` gets `data-world="house"` or `data-world="stage"`. Views set it on mount:
- house: `libraryView`, `createView`, `cohostSetupView`, account pages;
- stage: `showView`, `cohostCallView`, `draftScreen` (go-live).

The House lights transition (§4.1) is literally the swap of this attribute inside a view transition.

---

## 4. The motion system, piece by piece

### 4.1 Screen transitions (`router.js`)

```js
async function dispatch() {
  // …match route as today…
  const swap = async () => { unmountCurrent(); root.replaceChildren(); window.scrollTo(0, 0);
                             const p = view(root, ctx); await Promise.race([p, sleep(250)]); return p; };
  if (!document.startViewTransition || reducedMotion()) return finish(await swap());
  const vt = document.startViewTransition({ update: swap, types: [transitionType(from, to)] });
  …
}
```

- **Types**:
  - `house-to-stage` (House lights down)
  - `stage-to-house` (up)
  - `forward` / `back` (push and pop within house)
  - `tab` (a cross-fade between tab roots)
  - Direction comes from a small route-depth table plus `popstate`.
- **Shared elements**:
  - Sleeves get `view-transition-name: sleeve-<showId>` in lists, in Live now, and on the stage cover. One name per page, so the list sets it only on the tapped sleeve.
  - The tap point goes to `--vt-x/--vt-y` on `<html>` for the circular flood (`::view-transition-new(root)` animates `clip-path: circle()`).
- **Async views**:
  - The update callback waits at most 250 ms for the view, so a slow fetch doesn't freeze the old page.
  - Views that load (`showView`, `cohostCallView`, `albumView`, `creatorView`, `inviteView`) render their designed loader first, so the transition lands on a loader, never on blank. Several of them are blank today (§5).
- **The draft "Go live" path** keeps `navigate(..., { replace: true })` and the unlocked `AudioContext` (the reason the router uses pushState). The count-in runs before `navigate`; see Motion Lab › Go live.

Fallback: browsers without View Transitions, or with reduced motion on, get a plain swap followed by a 150 ms opacity fade-in of the new root.

### 4.2 `motion.js`

- `tokens`: the springs and durations, read once from computed CSS.
- `animate(el, keyframes, { spring | duration, easing, delay })`: WAAPI with `fill: 'both'`. It returns `.finished`, and under reduced motion it collapses to an opacity change or a no-op.
- `flip(elements, mutate, { spring })`: measure, mutate, invert, play. It's used for list reorders, cover morphs and card-to-bar folding.
- `sequence(steps, { signal })`: async steps that abort cleanly when the view unmounts. Each view passes an `AbortController`, mirroring the `run` token in the Motion Lab, so a loader that's still awaiting never touches a dead DOM.
- `reduced`: a live boolean from `matchMedia`, replacing `util.js reducedMotion()`.

### 4.3 Kinetic captions (`components.js`)

- **Render**: render the current piece once as `<span class="w">` words. Then each frame:
  - set `said` / `now` classes from `at.frac`, weighting each word by its spoken characters;
  - set `--wp` to the current word's progress.
  - The underline under the current word is a background-size, so it never reflows.
- **Weight**: weight never changes per word. A heavier word would be wider and re-wrap the line mid-sentence. Emphasis comes from opacity, `--gel-text` and the underline.
- **Fit**: on each new piece, try `font-stretch` 100 → 62% until the line fits both height and width, then step the size down. This replaces `--cap` and the length heuristic in `captions()` (`components.js:95`). Long single words (a Latin bacterium name) are why width is checked too.
- **Keys**: `captionsKey` changes per piece, as today. Word state updates outside `Regions`, by toggling classes, so the 80 ms frame loop stays cheap.
- **Accessibility**: the word layer is `aria-hidden`. A visually hidden `aria-live="polite"` node gets the full line once per line. Today the live region re-announces on every chunk re-render.

### 4.4 The studio clock (`ring.js`)

One SVG of 60 dots, quarter marks brighter, with four modes:

| Mode | Used by |
|---|---|
| `progress(seconds)`: dots up to the second hand | the stage header clock, the tune-in target |
| `chase(rate)`: a comet of about 9 dots, indeterminate | planning, Standing by, and any wait with no number |
| `arcs(n, of)`: n of 4 quarter arcs lit | Green room rundown (one arc per chapter) |
| `minutes(m)`: the first m dots lit, one dot per minute | pod-length picker (5 / 10 / 15), credits meter |

Dots are `<circle>`s toggled by class. A ring never repaints more than once per frame.

### 4.5 Sheets and drag

- `openSheet` keeps its signature. The sheet springs up with `--spring-smooth`.
- The app recedes: `#app` gets `scale: .94` with a 28 px radius, and the scrim fades in.
  - `#app` being transformed changes the containing block for its `position: fixed` children (`.go-bar`, `.np-overlay`). They scale with the page, which looks right, but anything measuring `getBoundingClientRect` during an open sheet must allow for it.
- New `drag.js`, a pointer helper shared by sheets and Now playing:
  - It follows the finger.
  - It rubber-bands upward.
  - It closes past 120 px or at more than 0.5 px/ms.
  - Otherwise it snaps back with `--spring-snappy`.
- The segmented control's indicator slides between options with FLIP.

### 4.6 Small things

- **Cue-card toasts**: a 3 px tally strip on top, a ruled card background, `--spring-bouncy` in, a timer bar for the toast's life. Toasts queue instead of replacing each other.
- **Hearts**:
  - 5 per burst, on arcs, 900–1300 ms each.
  - The count rolls as an odometer (`--spring-snappy`).
  - It replaces `floatHearts` (`views/shared.js:90-103`).
- **Call-in lines**: the queue shows as lit phone lines (borrowed from Switchboard), driven by `store.commentView(c, pos)`. On air, your message rises into the captions with "Reading your call-in".

  | State (status / tone) | Lamp | Line says |
  |---|---|---|
  | queued, `wait` | blinking amber | "about 40 s" |
  | `promised` with research pending | chasing ring | "Looking it up" |
  | `promised` | steady amber, return arrow | "Coming back" |
  | `next` (on deck, or promised and about to play) | steady amber | "Up next, 0:20" |
  | `live` | red | "On air" |
  | done | off, line clears | "Done" |
  | `missed` | off, text struck through | "Not answered" |

- **The lamp is one component (`.lamp`).** Watches, call-ins and production stages all use it:
  - amber with a count means something is waiting for you;
  - a chasing ring means work in progress;
  - red means on air or failed;
  - off means quiet.

---

## 5. Screen-by-screen

How each loader binds to real state is listed under "Driven by" in the Motion Lab. Line numbers are from `64a5a26`.

| Route / surface | Code | World | Loading today → new | Transition in | Main changes |
|---|---|---|---|---|---|
| Boot | `main.js:18` | stage | blank #app → **ident** (wordmark opens, lamp ignites), inline in `index.html` so it shows before JS; skipped if `/api/me` answers in under 250 ms | — | "Off air" error uses the same ident |
| `/login` | `login.js:11` | stage | buttons only disable → button shows the ring in `chase` | ident hand-off | Google first, email second, "Browse public pods" link; stops inheriting the last `--deep` (it never calls `applyAccent` today) |
| `/`, `/discover`, `/following` | `library.js:280`, list `:125` | house | "Loading…" → delayed skeletons (300 ms), then rows fade in 30 ms apart | Opening the app / tab cross-fade | Composer on Home; Live now shelf; recent rows; free-account card; Discover by format chips; Following with live banner |
| Library tab | `library.js:125` (+ albums) | house | as above | tab | Sleeve grid; Yours / Shared / Albums; filter and tag chips reorder with FLIP |
| `/albums`, `/album/:id` | `albums.js:79`, `:114` | house | album page **blank until fetch** → skeleton hero | forward | Fanned sleeve stack; Subscribe beside the title |
| `/creator/:uid` | `creator.js:10` | house | **blank** → skeleton | forward | Albums shelf, pods list |
| `/new` | `create.js:25` | house | **blank during `session.refresh()`** (`:30`) → render immediately and refresh the credits line in place | forward from composer, carrying its text | Live sleeve preview; gel tiles for vibes; studio-clock length picker; Go live bar states the cost |
| Draft | `show.js:117` | house → stage | — | House lights | The sleeve is the page; delete moves to a quiet link |
| Go live | `create.js` / `show.js:131` | stage | button disables → **count-in** while `POST /api/shows` / `startDraft` runs; holds on "Standing by" if slower | container transform from the button | Lamp opens from the button's red dot; reveal from the lamp |
| `/show/:id` load | `show.js:18` | stage | "Tuning in…" spinner → **tuning dial** (needle hunts until `getShow` returns, static clears on snapshot) | House lights / Tuning in | — |
| Live, before audio | `components.js:33-50` status | stage | "Theme music, while the hosts get ready…" / "Planning the episode…" / "Writing the cold open…" → **Green room**. The tally lights with the theme, which is on air while the plan is written; the ring chases with theme bars under it; the plan's title and chapters arrive; on the welcome the rundown folds into the chapter bar. Uses `store.inIntro(pos)`, `store.state`, the `plan` event and the first `segment` (see §5.3) | — | — |
| Tune-in | `views/shared.js:79-88` | stage | card → **studio-clock target** (the gesture that unlocks audio) | — | Says how far in the show is |
| Live (phone) | `live-mobile.js:16` | stage | — | — | Kinetic captions; level-reactive hosts and key light; chapter bar; call-in lines; rail actions move to the bottom bar and the More sheet |
| Now playing | `now-playing.js:16`, opened `live-mobile.js:61` | stage | — | cover FLIP from 44 px; drag to dismiss (closing is instant today) | Transcript and call-ins as cards |
| Replay | `now-playing.js` as a page | stage | — | House lights | Chapter marks on the scrubber; transcript follows the playhead |
| Studio (≥1024) | `studio.js:19` | stage | — | House lights | 16:9 stage, 40 px captions; side panel tabs: Call-ins (as lines), Context, Rundown, Transcript. The 1024 breakpoint remount (`show.js:52-59`) keeps the store and only swaps the layout |
| Sheets | `ui.js:53` and 13 builders in `sheets.js`, `albums.js`, `create.js`, `saved.js`, `admin.js`, `cohost-setup.js` | over stage or house | "Loading connections…", "Loading your albums…" → ring `chase` inside the sheet | Sheets | Restyle previews the new gel on the stage behind it before you confirm |
| `/cohost` | `cohost-setup.js:25` | house | blank during refresh → immediate render | forward | Modes as gels; says the mic is needed before you start |
| `/cohost/:id` | `cohost-call.js:29` | stage | "Dialing in…" spinner → ring `chase` around the co-host's avatar | Go live (Start the call) | Co-host avatar inside the ring, `--lv` from `player.level()`; minutes left on the clock; mute, hang up, speaker |
| Editing a call | `cohost-call.js:155-162` | stage | 4-line checklist and a small spinner → **Making the episode** (listen back, chapter flags, flubs flash and close, levels settle to -16 LUFS, the waveform folds into the sleeve) from `CallController.steps` and `saved.stats` | — | The unedited call stays downloadable for the owner |
| You (account) | `library.js:63` sheet → tab | house | "Loading credit history…" → inline ledger skeleton | tab | Credits shown on the ring as minutes; menu becomes rows |
| `/personas`, `/styles` | `saved.js:47` | house | "Loading…" → skeletons | forward | Personas with voice and gender; premade shelf |
| `/connections`, `/invite/:uid` | `connections.js:22`, `:123` | house | invite is **blank while loading** → skeleton card | forward | Requests first with one-tap accept; invite link is a copy button |
| `/stats`, `/show/:id/stats` | `stats.js:138`, `:80` | house | "Loading…" → skeleton tiles | forward | Three tiles; listeners chart with a crosshair readout; where people left; who listened |
| `/admin` | `admin.js:11` | house | **no loading state** ("No users match." before the first load) → skeleton rows | forward | Grants need a note so the ledger explains itself |

### 5.1 Screens added since the first pass (round 2)

| Surface | Code | World | Loading / waiting → new | Transition in | Main changes |
|---|---|---|---|---|---|
| Home, morning | `library.js` | house | — | Opening the app | A ready briefing card above the composer; the composer gains a record tool that opens `/memo` |
| `/briefings` | `views/briefings.js:43` | house | "Loading…" and "Checking your watchlist…" → **watchlist check** (each watch's lamp lights amber with its count, or stays grey), then House lights into the briefing | Loading lists | Today's briefing first. Make one now: template chips, clock length picker with credits (1 per 5 min), one button that states the cost |
| Schedule | `briefings.js renderSchedule` | house | — | Sheets | The time as the hero (Display L); days as one row; the cost and the skip rule said next to Save; "Next one ready by …" |
| Watchlist | `briefings.js renderWatch` | house | "Looking at it…" → the row appears with a chasing lamp until its first check | — | Lamp plus count per watch; paused and errors in warning text; Check now and Pause in an overflow menu on phones |
| Briefing template | `briefings.js` template sheet | house sheet | — | Sheets | Segments as a checklist with one line each; topics as plain words |
| Briefing on air | `/show/:id`, `kind === "briefing"` | stage, sky gel | — | Checking your watchlist | "Only you" badge instead of a listener count; section names in the episode row; source chips under facts; Interrupt is the main button; "Deleted in 47 hours" where the call-in field would be |
| Interrupt the show | `sheets.js openInterruptSheet` | stage sheet | "Got it…" → the hold-to-talk ring follows the mic level | Interrupt the show | Hold-to-talk inside the studio clock; Ask field; Go deeper, Move on, Steer. On send, unheard words dissolve, an amber gel flash marks the stinger, and an amber detour piece drops into the chapter bar |
| Your detour | `now-playing.js` (Your detour pill), store `detourLabel` | stage | — | — | "You asked" over the previous line; "Answering you" pill; the detour piece in the chapter bar; the transcript marks it |
| Research toggle | `create.js renderResearch` | house | — | — | One switch with its price ("+0.5 credits a minute"); the Go live bar itemizes pod plus research |
| Research desk | `components.js researchSources` | stage | "Read out · looking it up…" → chasing lamp | Looking it up | Cards say who a lookup was for, list source domains, and frame Google's Search Suggestions (§5.2) |
| Seekable timeline | `views/shared.js bindSeekBar` | stage | — | Scrubbing back | The chapter bar becomes a scrubber: a knob, a bubble with "-2:10, The Gold Rush", the live edge marked, "Back to live" springs forward. Chapters that have happened can be tapped |
| `/memo` start | `views/memo.js renderRecorder` (idle) | house, with a stage card | — | House lights | "Make a pod from a voice memo"; the recorder is a small tungsten stage with the ring and "Tap to record"; Upload a recording plus the voice-memos-app tip and limits (14 MB, 10 min) |
| `/memo` record | `memo.js` + `audio/recorder.js MemoRecorder` | stage, tungsten | — | Recording a memo | Record button inside the studio clock (one dot per 10 s = the 10-minute cap); level bars from `onLevel`; Pause / Resume and Done; Start over as a quiet link. The ring dims while paused |
| Memo options | `memo.js` + `pod-options.js` | house | — | Recording a memo | One page, in this order: the memo plays (Record again, Choose another); length; `musicCards` (Your show's theme / Studio theme, Original music +N credits, Your theme: …, No music); Format, optional (From my memo, Idea breakdown, …); `introRow`; Review the outline first; who can listen, albums and tags; More options (credit, language) |
| Making your pod | `memo-status.js makingScreen` | stage | stage list, progress bar and detail line → **lamp spine** (one lamp per `render.stage`; queued only when waiting) with `render.detail` under the lamp; `render.progress` fills the ring | Making a pod from a memo | Stop and delete is a quiet link |
| Review the outline | `memo-status.js reviewScreen` | house | — | — | Your memo (player, "The idea:", What we heard); findings with outlet, date and a "Sources disagree" / "Thin evidence" flag; where sources disagree; sources; Search Suggestions frame; Title; chapters with title and "what it covers", moved with up/down and removed (3–10), Add a chapter; length; music; Produce states the cost |
| Didn't get made | `memo-status.js failedScreen` | stage | — | — | The spine shows where it stopped (red lamp, `render.error`); the credits came back; "Trying again uses N credits"; Try again resumes from the outline; Delete |
| `/intro` | `views/intro.js` | house | — | Loading lists | The show's card plays its intro and quotes the welcome preview ("Welcome to Walk & Talk, …"); Show name; Your intro line (optional); theme tiles starting with "Matched to each pod", then library themes, then your own; Use it on new pods; Save; Remove my show intro |
| Intro on air | every pod's opening | stage | — | Show intro | The show name owns the stage while the theme plays, then tucks into the episode row as the welcome begins. Live pods play it while planning; memo pods and co-host episodes have it mixed in; briefings open on a library theme matched to the template's mood and never use the saved intro |

### 5.3 Merged after round 2 (round 3)

`main` gained the final voice-memo flow (`86266b6`, merged in `4862c22`) and intros for briefings and co-hosted calls (`07cf067`). The prototypes mark the boards that changed UPDATED.

| Change | Code | What the design does |
|---|---|---|
| Live pods play the theme while planning | `store.inIntro(pos)`, `components.js statusText` ("Theme music, while the hosts get ready…") | The Green room is **on air from the first second**: the tally lights with the theme, and theme bars sit under the ring. The plan arrives under the music, and the rundown folds into the chapter bar when the welcome starts. (The earlier "warming up the hosts" step is dropped: no real state drives it.) |
| Show notes | `components.js showNotes`, Now playing and Studio | A Show notes card: who made it and from what (a mic line), "What the research drew on" (web links only, via `webSources`), the theme music by name |
| Memo pods in the library | `library.js row()`: rendering, review, failed | The sleeve carries the studio clock filled to `render.progress` with the percentage; review gets an amber REVIEW tab and the meta in the live color; failed reads "didn't get made". No visibility icon until it's published |
| Shared pod options | `pod-options.js` | One set of components for Create, Memo and Co-host: visibility cards, share-with chips, album chips, New album sheet, music cards, intro row. Port them once (Phase 4) |
| Create | `create.js` | A "Got a rough voice memo?" row at the top; the Intro row (the real wording, with Edit). No theme picker: live pods choose their own theme |
| Co-host setup and episodes | `cohost-setup.js` (`use_show_intro`), `editing.scored()` / `mixer.mix()` | The Intro row on setup ("Ada welcomes listeners to your show by name; your theme is added when you hang up"). In Making the episode, mastering now slides your theme in under the welcome and the sign-off, and stings pulse at the chapter flags |
| Briefings open on a theme | `briefing.py`, `music_mood` on templates | After the watchlist check, the stage holds on the theme briefly before the good morning |
| Ledger labels | `library.js ledgerLabel` | "Pod made", "Original music", "Refund: <reason>". The You tab shows them as written |
| Account menu | `library.js` | Show intro and "Pod from a voice memo" join the You tab |

### 5.2 Rules the new screens add

- **Google Search Suggestions** (briefings searches, research lookups, memo research) must render as Google sends them: `srcdoc` in a sandboxed iframe, unstyled.
  - The design only frames them: a dashed container, a label, and their own row.
  - Never re-render or restyle Google's markup.
  - Never put two frames inside one Regions key: they reload on every re-render, which `contextKey` already guards against.
- **Private pods** (briefings) never show listener counts, share actions or albums. The stage shows "Only you" and the time left before deletion.
- **Two new motion moments**, both in the Motion Lab:
  - Interrupt: hold, dissolve, stinger, detour.
  - Show intro: title card handing off to the captions.
  - Both obey §2.4. The gel flash is a 600 ms `--gel` change, never a white flash.
- **More collisions found while mocking**: `.st` (stage) against a watch status class, and `.talk` (a speaking host) against the hold-to-talk button. They confirm the §3 rule: component classes get a component prefix (`.lamp`, `.talk-btn`, `.watch-status`), and states use `data-state` or `aria-*`, never a bare word.

---

## 6. Phases

Each phase ships on its own and leaves the app consistent. Old token names (`--ink`, `--panel`, `--chip`, `--accent`, `--deep`, …) are kept as aliases onto the new tokens until Phase 6, so unported screens keep rendering.

**Phase 0: foundations** (no visual redesign yet)
1. Self-host the fonts and remove the Google Fonts link. Switch `--font` to Atkinson, and add `--f-display`.
2. Add `@layer` declarations and give every existing file a layer. Fix the collisions in §7.
3. Write the new tokens in §2 next to the alias layer, plus the `data-world` and `data-house` plumbing.
4. Replace `theme.js mix()` with the `--gel` map and CSS derivation. Delete `--dim` (set, never used).
5. Rename to Impromptu: the strings in §7.2, the favicon and app icon (the lamp-dotted i), and `theme-color`.

Done when: the app looks the same apart from fonts and name, all tests pass, and screenshots (§8) match except for type.

**Phase 1: motion core**
1. `motion.js`, `ring.js`, `drag.js`.
2. View transitions in `router.js` with types and the fallback.
3. The new reduced-motion rule.

Done when: every route change animates (or cross-fades under reduced motion), and no view ever shows blank during a transition.

**Phase 2: the stage**
1. Kinetic captions and the live-region split.
2. The analyser in `LivePlayer`, with `--level` and `--lv`.
3. Tally, studio clock header and chapter bar.
4. Tune-in target, Now playing (cover FLIP and drag), Replay, desktop Studio.

Done when: a 15-minute fake show plays with the gel following chapters; no frame is over 16 ms on a mid-range Android in a profile; the screen reader announces each line once.

**Phase 3: the waits**
1. Boot ident.
2. Tuning dial.
3. Green room.
4. Go-live count-in, including the slow-server hold.
5. Making the episode.
6. Delayed skeletons for every list.

Done when: no spinner or "Loading…" string is left in `static/` (grep is part of the check), and each loader is driven by the state listed in the Motion Lab.

**Phase 4: house screens**
1. Home with the composer.
2. Library grid, Discover and Following.
3. Album and creator pages.
4. Create with the live sleeve preview, the gel tiles and the clock length picker.
5. Draft.
6. Co-host setup.
7. Tab bar or rail, if approved (§9).

**Phase 5: sheets and small things**
1. Spring sheets with recede and drag.
2. Segmented indicator.
3. Call-in lines.
4. Cue-card toasts.
5. Hearts and the odometer.
6. Restyle preview.

**Phase 5b: round-2 surfaces**
1. Briefings: page, schedule, watchlist, template sheet, briefing on stage, watchlist-check loader.
2. Interrupt sheet and detour (stinger, detour piece, unheard-words dissolve).
3. Research toggle and research desk.
4. Promised call-in states on lines.
5. Seekable scrubber.
6. Voice memo: record, options, spine, review, failed.
7. Show intro page and the intro title card.

8. Show notes, memo states in the library, and the shared `pod-options.js` components (§5.3).

The voice-memo and intro work has merged, so port against `main` as it stands.

**Phase 6: account pages and cleanup**
1. You, personas, styles, connections, stats, admin, login.
2. Remove the alias tokens and dead CSS. Collapse the duplicated selectors (§7.1).

---

## 7. Fix along the way

### 7.1 CSS collisions and dead code (found in the inventory)

- **`.bar`** (`social.css:107-110`, the stats bar) also matches the phone call-in form `callin-form bar` (`views/shared.js:38, 43`). It sets max-width 36 px, height and background, which breaks the phone call-in bar on `main` today. Commit `e61d83d` on `worktree-review-personal-feeds-branching` fixes it; cherry-pick it into Phase 0 or rename the stats class to `.st-bar`.
- **`.tag`** (`base.css:121`) also matches tag chips `chip tag` (`library.js:166`) and wins on padding, radius and background.
- **`.small`** (`base.css:18`) leaks into `.icon-btn.small`, `.round-btn.small`, `.badge.small`, `.spinner.small` and `.cap-status.small`. Rename the modifiers to `--s`-style classes.
- **Defined in several files**: `.back-link`, `.seg-tab(s)`, `.lib-pane`, `.saved-card`, `.tune-card`, `.tune-in`, `.vibe-check`, `.vis-ic`, `.ctx-add`, `.spinner`, `.go-bar`, `.chapter`, `.ctx-item`. Each gets one home when its screen is ported.
- **Unused**: `--dim` is set and never read. Drop it.
- **Dead animation**: `.cb-fill`'s width transition never runs, because the chapter bar is rebuilt with `innerHTML`. Fix with a keyed bar that updates widths in place.
- **Replayed animation**: call-in bubbles replay `bubble-in` on every store version (`live-mobile.js:104`). Fix: render only new bubbles.
- **Hard-coded colors**: 14 hex values and about 40 `rgba()` literals outside `tokens.css`, plus about 29 hex values in JS (accent lists, the caller palette, `#30284F` / `#2F6BD0` fallbacks). All move to tokens.

### 7.2 Rename strings

- Title: `index.html:8`.
- Visible brand and aria-label: `library.js:21`, `login.js:23`.
- Cover text: `now-playing.js:95`.
- Invite copy: `connections.js:44`.
- Share fallback title: `sheets.js:277`.
- `document.title`: `show.js:50, 85, 101`, `cohost-call.js:52, 280`, `memo-status.js:32`.
  - Line numbers drift as `main` moves. Run `grep -rn "Gemini Podcasts" static/` at the start of Phase 0; it found 12 occurrences at `07cf067`.
- Keep the "GEMINI_API_KEY isn't set" errors (`library.js:317`, `create.js:429`, `cohost-setup.js:281`). They name a real env var and only admins see them.
- localStorage keys stay `gp_*` (`gp_scope`, `gp_filter`, `gp_lid`, `gp_mine_*`, `gp_create`, `gp_cohost`, `gp_cohost_hint`, `gp_captions`). Renaming them would silently reset everyone's preferences and drafts.

---

## 8. Testing

- **Visual regression.**
  - Add `tests/visual.py`: Playwright against `tests/fake_gemini.py` (the offline recipe in the README).
  - Capture the key routes at 390×844 and 1440×900, in house light and house dark, with and without `prefers-reduced-motion`.
  - Store baselines under `tests/visual/baseline/`. Animations are frozen with `document.getAnimations().forEach(a => a.finish())` before each shot.
- **Gel contrast.** A unit test computes, for every gel in §2.1 and 50 random custom accents, the contrast of white and `--gel-text` on `--stage` (at least 7:1) and of `--on-dim` (at least 4.5:1).
- **Performance budget.**
  - Fonts at most 110 KB.
  - Caption frame work at most 2 ms (Performance panel, 4× CPU throttle).
  - No layout shift from the font swap.
  - The 80 ms region loop stays for DOM work. The rAF loop only writes CSS variables.
- **Browsers.**
  - Current Chrome, Safari (iOS 17 and later) and Firefox.
  - Features and fallbacks:

    | Feature | Fallback |
    |---|---|
    | View Transitions | swap plus fade |
    | `linear()` | cubic-bezier via `@supports` |
    | `@property` | the gel snaps instead of animating |
    | `color-mix` | none; it's baseline |

  - Relative color syntax is only used for custom-style gels. Its fallback is the nearest preset gel.
- **Accessibility.**
  - Captions announce once per line.
  - Every animated control has a static equivalent.
  - Focus moves into sheets and back.
  - Drag-to-dismiss always has a visible close button too.
  - Contrast is checked in both house modes.
- **Existing suites**: `tests/smoke_test.py`, `tests/unit_test.py` and `tests/store_contract.py` must stay green after every phase.
  - They exercise the API and the show loop, not the browser, so `tests/visual.py` is the first UI coverage the app gets.
  - Keep the `data-action` / `data-r` hooks anyway: the visual suite drives the UI through them.

---

## 9. Decisions for you

1. **Phone navigation.** Replace the header plus account sheet with a 4-tab bar (Home, Discover, Library, You), with a left rail on desktop.
   - Recommended: it's the biggest UX gain on phones.
   - Cost: Phase 4 touches every house view.
2. **Home composer.** Start a pod from Home, with `/new` opening pre-filled. Recommended.
3. **House light mode.** Follow the device's light or dark setting on the library side (recommended), or keep the app dark everywhere. The stage is dark either way.
4. **Call-in lines.** Show the queue as lit lines on the live view and the studio panel. Recommended. It replaces the bubbles over captions on phones; bubbles stay on desktop for recent call-ins.
5. **Cherry-pick `e61d83d`** (the `.bar` fix) into Phase 0. Recommended, since the bug is live on `main`.

---

6. **Per-watch progress for briefings.** The watchlist-check loader is best when the make call reports each watch as it's checked, by streaming or by the page polling `api.watches()`. Without that, rows tick together when the call returns. Recommended as a small API addition.
7. **Where briefings and the show intro live.** Recommended:
   - a briefing card on Home when one is ready;
   - the full Briefings page and Show intro under the You tab;
   - a record tool in the Home composer for voice memos.

## 10. Risks

- **Merge conflicts.** `worktree-voice-memo` and `worktree-review-personal-feeds-branching` both edit `app/show.py`, views and CSS.
  - Land Phase 0 (tokens with aliases, fonts, layers) early and small.
  - Rebase this branch after each of those merges.
  - Port screens those branches don't touch first: login, library and account before live views.
- **View transitions and async views.** A view that awaits longer than 250 ms lands on its loader by design. Any view that still renders nothing before its first `await` must get a loader first (§5 lists them).
- **`#app` transform during sheets** changes the containing block for fixed children (§4.5). Test the go-bar and Now playing with a sheet open.
- **Gel legibility** on sleeves for bright gels (straw, sky). Titles stay in the darker lower half, and the contrast test covers the title area.
- **Anybody's character.** It is a distinctive face. Keep it to display, captions and sleeves; all running text and controls stay in Atkinson.
