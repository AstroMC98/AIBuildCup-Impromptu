// /memo — a voice memo in, a finished episode out. Record one here, or upload one
// from your phone's voice memos app; pick a length and the music; send it. What
// happens next shows on the pod's own page (views/memo-status.js).
import { api } from "../api.js";
import { MemoRecorder, extensionFor, recordingSupported } from "../audio/recorder.js";
import { html, setHtml } from "../html.js";
import { icon } from "../icons.js";
import { albumChips, introRow, lengthChip, musicCards, newAlbumSheet, shareWith, visibilityCards } from "../pod-options.js";
import { fillRing, ring } from "../ring.js";
import { navigate } from "../router.js";
import { loginPath, session } from "../session.js";
import { applyAccent } from "../theme.js";
import { fmtTime, store as local } from "../util.js";
import { appHeader, bindHeader, templateCard } from "./library.js";

const LENGTHS = [5, 10, 15];
const AUDIO_EXT = /\.(m4a|mp3|wav|webm|ogg|oga|opus|flac|aac|aiff?)$/i;

export async function memoView(root) {
  if (!session.signedIn) {
    navigate(loginPath("/memo"), { replace: true });
    return null;
  }
  await session.refresh();
  const saved = local.get("gp_memo", {});
  const limits = session.options.memo || { max_mb: 14, max_seconds: 600 };
  const state = {
    memo: null,                   // { blob, seconds, name, url }
    level: 0,
    minutes: LENGTHS.includes(saved.minutes) ? saved.minutes : 15,
    music: "auto",
    template: null,               // null: the format the memo suggests
    review: Boolean(saved.review),
    visibility: saved.visibility || "private",
    sharedWith: [],
    albums: [],
    useIntro: true,
    sending: false,
  };
  const data = { connections: [], albums: [], music: [], intro: null };
  let recorder = null;

  applyAccent(document.documentElement, "#2E6F9E");
  const el = document.createElement("div");
  el.className = "create-page";
  setHtml(el, html`
    ${appHeader({ search: false, nav: false })}
    <main class="create memo-page">
      <div class="create-title">
        <h1>Make a pod from a voice memo</h1>
        <p class="muted">Talk it through, rough is fine. The hosts look things up, shape it into a story and record a finished episode, music and all.</p>
      </div>
      <div data-free></div>
      <section class="card recorder" style="--gel:#FFB257" aria-label="Your memo" data-recorder></section>
      <input type="file" accept="audio/*,.m4a,.mp3,.wav,.webm,.ogg,.flac,.aac" data-file hidden>

      <h2 class="create-h2">How long?</h2>
      <div class="len-chips" role="group" aria-label="Length" data-lengths></div>

      <h2 class="create-h2">Theme music</h2>
      <div class="radio-cards" role="radiogroup" aria-label="Theme music" data-music></div>

      <h2 class="create-h2">Format <span class="faint small">optional</span></h2>
      <div class="tpl-row" data-formats></div>

      <h2 class="create-h2">Intro</h2>
      <div data-intro></div>

      <div class="switch-row review-row">
        <span id="review-label" class="col"><span>Review the outline first</span>
          <span class="muted small">See what was heard, what the research found and the plan, and change it, before it's recorded. Credits are used when you press Produce.</span></span>
        <button type="button" role="switch" class="switch" aria-labelledby="review-label" data-review></button>
      </div>

      <h2 class="create-h2">Who can listen?</h2>
      <div class="radio-cards" role="radiogroup" aria-label="Who can listen" data-visibility></div>
      <div data-share-with></div>

      <h2 class="create-h2">Album and tags <span class="faint small">optional</span></h2>
      <div class="chips" data-albums role="group" aria-label="Albums"></div>
      <label class="sr-only" for="memo-tags">Tags</label>
      <input id="memo-tags" class="field pill-field tags-field" maxlength="300" placeholder="Tags, separated by commas: ideas, cities">

      <details class="more-options">
        <summary>More options</summary>
        <label class="field-label" for="memo-credit">Credit the memo to</label>
        <input id="memo-credit" class="field" maxlength="40" placeholder="${session.me.name.split(" ")[0]}">
        <label class="field-label" for="memo-language">Language</label>
        <input id="memo-language" class="field" maxlength="60" placeholder="The one you speak in the memo">
      </details>

      <p class="error" role="alert" data-error></p>
      <div class="go-bar">
        <p class="cost" data-cost></p>
        <button class="btn white big block go" type="button" data-go></button>
      </div>
    </main>`);
  root.append(el);
  const offHead = bindHeader(el);
  const $ = (s) => el.querySelector(s);
  const setError = (msg) => { $("[data-error]").textContent = msg || ""; };

  // ---- the recording -------------------------------------------------------------------

  // The recorder is a small stage, lit tungsten: the record button sits inside the studio
  // clock, which fills one dot per tenth of the time you have (10 s a dot for 10 minutes),
  // and the level bars follow your voice. Once there's a memo, it's back to the house.
  const BARS = 28;
  const levels = new Array(BARS).fill(0);
  let lastBars = 0;
  const dotsFor = (sec) => Math.min(60, Math.floor((sec / limits.max_seconds) * 60) + 1);   // the first dot lights at once
  const renderRecorder = () => {
    const box = $("[data-recorder]");
    const rec = recorder && ["recording", "paused", "stopping"].includes(recorder.state) ? recorder : null;
    box.classList.toggle("stage", !state.memo);
    if (rec) {
      const paused = rec.state === "paused";
      setHtml(box, html`
        <div class="rec-live ${paused ? "paused" : ""}">
          <div class="rec-clock">${ring({ cls: "rec-ring", r: 46, dot: 2 })}
            <span class="rec-core"><span class="rec-dot" aria-hidden="true"></span>
              <span class="rec-time" data-rec-time aria-live="off">${fmtTime(rec.seconds)}</span></span></div>
          <span class="rec-bars" aria-hidden="true">${levels.map(() => html`<i></i>`)}</span>
          <span class="faint small">${paused ? "Paused" : `Up to ${fmtTime(limits.max_seconds)}`}</span>
        </div>
        <div class="row gap">
          <button class="btn outline grow" type="button" data-rec-pause>${paused ? "Resume" : "Pause"}</button>
          <button class="btn white grow" type="button" data-rec-stop>${icon("stop", 16, 2.4)}Done</button>
        </div>
        <button class="btn link" type="button" data-rec-cancel>Start over</button>`);
      fillRing(box.querySelector(".rec-ring"), dotsFor(rec.seconds));
      paintBars();
      return;
    }
    if (state.memo) {
      const m = state.memo;
      setHtml(box, html`
        <p class="card-label">Your memo</p>
        <audio class="memo-audio" controls preload="metadata" src="${m.url}"></audio>
        <p class="muted small">${m.name}${m.seconds ? ` · ${fmtTime(m.seconds)}` : ""} · ${(m.blob.size / 1e6).toFixed(1)} MB</p>
        <div class="row gap">
          ${recordingSupported() ? html`<button class="btn outline grow" type="button" data-rec-start>${icon("mic", 16)}Record again</button>` : ""}
          <button class="btn outline grow" type="button" data-upload>${icon("addFile", 16)}Choose another</button>
        </div>`);
      return;
    }
    setHtml(box, html`
      <p class="card-label">Your memo</p>
      <p class="muted small">Say what's on your mind: the idea, why it matters to you, what you wonder about. A couple of minutes is plenty.</p>
      <div class="rec-start">
        ${recordingSupported() ? html`
          <div class="rec-clock">${ring({ cls: "rec-ring", r: 46, dot: 2, lit: (i) => i % 15 === 0 })}
            <button class="rec-btn" type="button" data-rec-start aria-label="Start recording">${icon("mic", 32, 2)}</button></div>
          <span class="strong">Tap to record</span>` : html`
          <p class="faint small">Recording needs the app on a secure (https) address. You can still upload a recording.</p>`}
      </div>
      <button class="btn outline block" type="button" data-upload>${icon("addFile", 16)}Upload a recording</button>
      <p class="faint small">On a walk with your phone locked? Record in your phone's voice memos app, then upload it here.
        Up to ${limits.max_mb} MB and ${Math.round(limits.max_seconds / 60)} minutes.</p>`);
  };

  const paintBars = () => {
    const bars = el.querySelectorAll(".rec-bars i");
    bars.forEach((b, i) => b.style.setProperty("--v", levels[i].toFixed(3)));
  };

  const keepMemo = (blob, seconds, name) => {
    if (state.memo) URL.revokeObjectURL(state.memo.url);
    state.memo = { blob, seconds, name, url: URL.createObjectURL(blob) };
    renderRecorder();
    renderCost();
  };

  const startRecording = async () => {
    setError("");
    recorder = new MemoRecorder({
      maxSeconds: limits.max_seconds,
      onTick: (sec) => {
        const t = el.querySelector("[data-rec-time]");
        if (t) t.textContent = fmtTime(sec);
        fillRing(el.querySelector(".rec-live .rec-ring"), dotsFor(sec));
      },
      onLevel: (v) => {
        // A rolling history of how loud you've been, about a dozen steps a second.
        const now = performance.now();
        if (now - lastBars < 80) return;
        lastBars = now;
        levels.push(v);
        levels.shift();
        paintBars();
      },
      onStop: ({ blob, seconds }) => {
        recorder = null;
        keepMemo(blob, seconds, `Memo, ${new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}.${extensionFor(blob.type)}`);
      },
    });
    try {
      await recorder.start();
    } catch (err) {
      recorder = null;
      setError(err?.name === "NotAllowedError"
        ? "The microphone is blocked. Allow it for this site in your browser's settings, or upload a recording instead."
        : "Couldn't start the microphone. You can upload a recording instead.");
    }
    renderRecorder();
    renderCost();
  };

  const useFile = async (file) => {
    setError("");
    if (!file.type.startsWith("audio/") && !AUDIO_EXT.test(file.name)) {
      return setError("That isn't an audio recording. Try M4A, MP3, WAV, WebM or Ogg.");
    }
    if (file.size > limits.max_mb * 1024 * 1024) return setError(`Recordings can be up to ${limits.max_mb} MB.`);
    keepMemo(file, await durationOf(file), file.name);
    if (state.memo.seconds > limits.max_seconds) {
      setError(`That's ${fmtTime(state.memo.seconds)} long; memos can be up to ${fmtTime(limits.max_seconds)}.`);
    }
  };

  // ---- the rest of the choices ------------------------------------------------------------

  const cost = () => session.cost(state.minutes) + (state.music === "generate" ? session.options.music?.credits || 0 : 0);
  const renderCost = () => {
    const c = cost();
    const enough = session.credits >= c;
    $("[data-cost]").textContent = `Uses ${c} credit${c === 1 ? "" : "s"}${
      state.review ? " when you press Produce" : ""} · you have ${session.credits}`;
    $("[data-cost]").classList.toggle("short", !enough);
    const recording = Boolean(recorder);
    const go = $("[data-go]");
    go.disabled = !enough || !state.memo || recording || state.sending;
    setHtml(go, state.sending ? html`<span class="lamp chase" aria-hidden="true"></span>Sending your memo…`
      : html`${icon("sparkle", 20)}${state.review ? "Make the outline" : "Make my pod"}`);
    setHtml($("[data-free]"), enough ? "" : html`
      <div class="free-note">${icon("coin", 18)}<span>
        ${session.credits ? html`<strong>You have ${session.credits} credits.</strong> This pod needs ${c}. Pick a shorter length${state.music === "generate" ? " or studio music" : ""}, or ask an admin for more.`
          : html`<strong>Free account.</strong> Making pods uses credits (${session.options.credits_per_minute} per minute). Ask an admin for demo credits.`}
      </span></div>`);
  };
  const renderLengths = () => {
    setHtml($("[data-lengths]"), html`${LENGTHS.map((m) => html`
      ${lengthChip(m, state.minutes === m)}`)}`);
    renderCost();
  };
  const renderMusic = () => {
    setHtml($("[data-music]"), musicCards(state.music, { mine: data.music, generate: true, intro: state.useIntro ? data.intro : null }));
    renderCost();
  };
  const renderFormats = () => setHtml($("[data-formats]"), html`
    <button type="button" class="tpl auto" data-template="" aria-pressed="${!state.template}" style="--tile:#30284F">
      <span class="tpl-top"><span class="tpl-label">Auto</span></span>
      <span class="tpl-blurb">Picked from your memo: the format that fits what you said, with its hosts.</span>
      ${!state.template ? html`<span class="vibe-check" aria-hidden="true">${icon("check", 14, 3)}</span>` : ""}
    </button>
    ${(session.options.templates || []).map((t) => templateCard(t, { selected: state.template === t.id }))}`);
  const renderIntro = () => setHtml($("[data-intro]"), introRow(data.intro, state.useIntro));
  const renderReview = () => $("[data-review]").setAttribute("aria-checked", String(state.review));
  const renderVisibility = () => {
    setHtml($("[data-visibility]"), visibilityCards(state.visibility));
    setHtml($("[data-share-with]"), shareWith(state.visibility, data.connections, state.sharedWith));
  };
  const renderAlbums = () => setHtml($("[data-albums]"), albumChips(data.albums, state.albums));

  // ---- events -----------------------------------------------------------------------------

  el.addEventListener("click", (e) => {
    const t = e.target;
    if (t.closest("[data-rec-start]")) startRecording();
    else if (t.closest("[data-rec-pause]")) {
      if (recorder.state === "paused") recorder.resume(); else recorder.pause();
      renderRecorder();
    } else if (t.closest("[data-rec-stop]")) recorder?.stop();
    else if (t.closest("[data-rec-cancel]")) {
      recorder?.cancel();
      recorder = null;
      renderRecorder();
      renderCost();
    } else if (t.closest("[data-upload]")) $("[data-file]").click();
    else if (t.closest("[data-min]")) {
      state.minutes = Number(t.closest("[data-min]").dataset.min);
      renderLengths();
    } else if (t.closest("[data-music]")) {
      state.music = t.closest("[data-music]").dataset.music;
      renderMusic();
    } else if (t.closest("[data-template]")) {
      state.template = t.closest("[data-template]").dataset.template || null;
      renderFormats();
    } else if (t.closest("[data-use-intro]")) {
      state.useIntro = !state.useIntro;
      renderIntro();
      renderMusic();
    } else if (t.closest("[data-review]")) {
      state.review = !state.review;
      renderReview();
      renderCost();
    } else if (t.closest("[data-vis]")) {
      state.visibility = t.closest("[data-vis]").dataset.vis;
      renderVisibility();
    } else if (t.closest("[data-share]")) {
      const uid = t.closest("[data-share]").dataset.share;
      state.sharedWith = state.sharedWith.includes(uid) ? state.sharedWith.filter((u) => u !== uid) : [...state.sharedWith, uid];
      renderVisibility();
    } else if (t.closest("[data-album]")) {
      const id = t.closest("[data-album]").dataset.album;
      state.albums = state.albums.includes(id) ? state.albums.filter((a) => a !== id) : [...state.albums, id];
      renderAlbums();
    } else if (t.closest("[data-new-album]")) {
      newAlbumSheet((album) => {
        data.albums.unshift(album);
        state.albums.push(album.id);
        renderAlbums();
      });
    } else if (t.closest("[data-go]")) submit();
  });
  $("[data-file]").addEventListener("change", (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (file) useFile(file);
  });

  // A memo you haven't sent yet is only in this tab: don't lose it to a stray reload.
  const guard = (e) => {
    if (recorder || (state.memo && !state.sending)) {
      e.preventDefault();
      e.returnValue = "";
    }
  };
  addEventListener("beforeunload", guard);

  async function submit() {
    setError("");
    if (!state.memo) return setError("Record or upload a memo first.");
    if (state.memo.seconds > limits.max_seconds) return setError(`Memos can be up to ${fmtTime(limits.max_seconds)} long.`);
    state.sending = true;
    renderCost();
    const fd = new FormData();
    fd.append("audio", state.memo.blob, state.memo.name);
    fd.append("options", JSON.stringify({
      minutes: state.minutes, review: state.review, music: state.music, template: state.template,
      use_show_intro: state.useIntro, visibility: state.visibility,
      shared_with: state.visibility === "private" ? state.sharedWith : [], album_ids: state.albums,
      tags: $("#memo-tags").value.split(",").map((x) => x.trim()).filter(Boolean),
      credit_as: $("#memo-credit").value.trim(), language: $("#memo-language").value.trim(),
      client_duration_s: Number.isFinite(state.memo.seconds) && state.memo.seconds > 0 ? Math.round(state.memo.seconds) : null,
    }));
    local.set("gp_memo", { minutes: state.minutes, review: state.review, visibility: state.visibility });
    try {
      const res = await api.createMemo(fd);
      session.refresh();
      navigate(`/show/${res.id}`);
    } catch (err) {
      if (err.status === 401) return navigate(loginPath("/memo"));
      state.sending = false;
      setError(err.message);
      session.refresh().then(renderCost);
    }
  }

  // ---- first paint ----------------------------------------------------------------------------

  renderRecorder();
  renderLengths();
  renderFormats();
  renderReview();
  renderVisibility();
  renderAlbums();
  renderIntro();
  renderMusic();
  const [conns, albums, music, intro] = await Promise.all([
    api.connections().catch(() => ({ connected: [] })),
    api.albums().catch(() => ({ albums: [] })),
    api.myMusic().catch(() => ({ music: [] })),
    api.showIntro().catch(() => ({ intro: null })),
  ]);
  data.connections = conns.connected;
  data.albums = albums.albums;
  data.music = music.music;
  data.intro = intro.intro;
  renderVisibility();
  renderAlbums();
  renderIntro();
  renderMusic();
  return {
    unmount() {
      offHead();
      removeEventListener("beforeunload", guard);
      recorder?.cancel();
      if (state.memo) URL.revokeObjectURL(state.memo.url);
    },
  };
}

/** How long an uploaded recording is, if the browser can tell (0 if not). */
function durationOf(file) {
  return new Promise((resolve) => {
    const audio = new Audio();
    const url = URL.createObjectURL(file);
    let settled = false;
    const done = (v) => {
      if (settled) return;
      settled = true;
      URL.revokeObjectURL(url);
      resolve(Number.isFinite(v) ? v : 0);
    };
    audio.preload = "metadata";
    audio.onloadedmetadata = () => done(audio.duration);
    audio.onerror = () => done(0);
    setTimeout(() => done(0), 4000);
    audio.src = url;
  });
}
