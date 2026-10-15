// /cohost — set up a live call with an AI co-host: what you'll talk about, who
// your co-host is (a persona), how they play it, how long, whether it opens with
// your saved show intro, and who can hear the episode. "Start the call" opens the
// mic (inside the click) and spends the credits.
import { api } from "../api.js";
import { micSupported, startCallAudio, takeHandoff } from "../cohost/audio.js";
import { html, setHtml } from "../html.js";
import { icon, KIND_ICON } from "../icons.js";
import { bindPersonaForm, fillPersona, personaCard, personaFields, readPersona } from "../personas.js";
import { introRow, lengthChip } from "../pod-options.js";
import { navigate } from "../router.js";
import { loginPath, session } from "../session.js";
import { applyAccent } from "../theme.js";
import { openSheet } from "../ui.js";
import { store as local, toast } from "../util.js";
import { appHeader, bindHeader } from "./library.js";

const LENGTHS = [5, 10, 15];
const BLANK = { name: "", voice: "auto", persona: "", gender: "", likes: "", specialties: "", skills: "", context: "" };
const hostOf = (h) => Object.fromEntries(Object.keys(BLANK).map((k) => [k, h?.[k] ?? BLANK[k]]));
const VISIBILITY = [
  ["private", "Private", "lock", "Only you, plus connections you pick."],
  ["connections", "Connections", "people", "All your connections can listen."],
  ["public", "Public", "globe", "Anyone can listen, free, and it's on Discover."],
];

export async function cohostSetupView(root) {
  if (!session.signedIn) {
    navigate(loginPath("/cohost"), { replace: true });
    return null;
  }
  await session.refresh();
  const modes = session.options.cohost_modes || [];
  const presets = session.options.persona_presets || [];
  const saved = local.get("gp_cohost", {});
  const state = {
    host: hostOf(saved.host || presets.find((p) => p.id === "comedian") || presets[0]),
    mode: modes.some((m) => m.id === saved.mode) ? saved.mode : "banter",
    minutes: LENGTHS.includes(saved.minutes) ? saved.minutes : 10,
    visibility: saved.visibility || "private",
    sharedWith: [],
    useIntro: true,      // open with your saved show intro, if you have one
    attachments: [],     // { name, kind, text }
    busy: "",
  };
  const data = { personas: [], connections: [], intro: null };

  const el = document.createElement("div");
  el.className = "create-page cohost-setup";
  setHtml(el, html`
    ${appHeader({ search: false, nav: false })}
    <main class="create">
      <div class="create-title">
        <h1>Co-host live</h1>
        <p class="muted">Talk into your phone and an AI co-host banters, asks or argues back in real time.
          Hang up, and it's an episode.</p>
      </div>
      <div data-free></div>

      <section class="card about-box" aria-labelledby="topic-label">
        <label id="topic-label" for="topic" class="card-label">What are you two talking about?</label>
        <textarea id="topic" class="field area" maxlength="2000" placeholder="Is sourdough worth the hassle? Or: the best and worst calls of last night's game."></textarea>
        <details class="notes-box" data-notes-box>
          <summary>Notes for your co-host <span class="faint small">optional</span></summary>
          <label class="sr-only" for="notes">Notes for your co-host</label>
          <textarea id="notes" class="field area" placeholder="Facts, an article, your outline. Your co-host draws on it; it isn't read out."></textarea>
          <div class="attach-chips" data-chips></div>
          <div class="row gap tools">
            <button type="button" class="btn outline grow" data-pick>${icon("file", 16)}File</button>
            <button type="button" class="btn outline grow" data-link-toggle>${icon("link", 16)}Link</button>
            <input type="file" data-file multiple hidden accept="${(session.options.file_types || []).join(",")}">
          </div>
          <form class="link-form row gap" data-link-form hidden>
            <label class="sr-only" for="notes-url">Link to a page or PDF</label>
            <input id="notes-url" class="field" type="url" inputmode="url" placeholder="https://…">
            <button class="btn quiet" type="submit">Fetch</button>
          </form>
          <p class="meta" data-meta aria-live="polite"></p>
        </details>
      </section>

      <h2 class="create-h2">Your co-host</h2>
      <button type="button" class="cohost-pick" data-host aria-label="Change your co-host"></button>

      <h2 class="create-h2">How should they play it?</h2>
      <div class="radio-cards mode-cards" role="radiogroup" aria-label="How your co-host plays it" data-modes></div>
      <p class="faint small mode-hint">You can switch during the call.</p>

      <h2 class="create-h2">How long?</h2>
      <div class="len-chips" role="group" aria-label="Length" data-lengths></div>

      <h2 class="create-h2">Intro</h2>
      <div data-intro></div>

      <h2 class="create-h2">Who can hear the episode?</h2>
      <div class="radio-cards" role="radiogroup" aria-label="Who can hear the episode" data-visibility></div>
      <div data-share-with></div>

      <details class="more-options">
        <summary>More options</summary>
        <label class="field-label" for="your-name">Your name on the show</label>
        <input id="your-name" class="field" maxlength="40" value="${saved.yourName || session.me.name.split(" ")[0]}">
        <label class="field-label" for="language">Language</label>
        <input id="language" class="field" maxlength="60" value="${saved.language || "English"}">
      </details>

      <p class="error" role="alert" data-error></p>
      <div class="go-bar">
        <p class="cost" data-cost></p>
        <button class="btn white big block go" type="button" data-go>${icon("mic", 20, 2.2)}Start the call</button>
        <p class="faint small go-hint">${icon("headphones", 14)} Best with headphones</p>
      </div>
    </main>`);
  root.append(el);
  const offHead = bindHeader(el);
  const $ = (s) => el.querySelector(s);
  const notes = $("#notes");

  // ---- rendering ---------------------------------------------------------------------

  const mode = () => modes.find((m) => m.id === state.mode) || modes[0];
  const tint = () => applyAccent(document.documentElement, mode()?.accent || "#B8336A");
  const renderFree = () => {
    const cost = session.cost(state.minutes);
    setHtml($("[data-free]"), session.credits >= cost ? "" : html`
      <div class="free-note">${icon("coin", 18)}<span>
        ${session.credits ? html`<strong>You have ${session.credits} credits.</strong> This call needs ${cost}. Pick a shorter length, or ask an admin for more.`
          : html`<strong>Free account.</strong> Co-hosting uses credits (${session.options.credits_per_minute} per minute of call time). Ask an admin for demo credits.`}
      </span></div>`);
  };
  const renderHost = () => {
    const h = state.host;
    setHtml($("[data-host]"), html`${personaCard({ ...h, name: h.name || "Your co-host", persona: h.persona || "Warm, quick-witted and curious about everything." })}
      <span class="pick-edit">${icon("edit", 18)}</span>`);
  };
  const renderModes = () => setHtml($("[data-modes]"), html`${modes.map((m) => html`
    <button type="button" role="radio" class="radio-card mode-card" data-mode="${m.id}" aria-checked="${state.mode === m.id}" style="--tile:${m.accent}">
      <span class="radio-dot" aria-hidden="true"></span>
      <span class="radio-text"><span class="radio-title">${m.label}</span><span class="radio-desc">${m.blurb}</span></span>
    </button>`)}`);
  const renderLengths = () => {
    setHtml($("[data-lengths]"), html`${LENGTHS.map((m) => html`
      ${lengthChip(m, state.minutes === m)}`)}`);
    renderCost();
  };
  const renderVisibility = () => {
    setHtml($("[data-visibility]"), html`${VISIBILITY.map(([id, title, ic, desc]) => html`
      <button type="button" role="radio" class="radio-card" data-vis="${id}" aria-checked="${state.visibility === id}">
        <span class="radio-dot" aria-hidden="true"></span>
        <span class="radio-text"><span class="radio-title">${icon(ic, 16)} ${title}</span><span class="radio-desc">${desc}</span></span>
      </button>`)}`);
    setHtml($("[data-share-with]"), state.visibility !== "private" || !data.connections.length ? "" : html`
      <p class="field-label share-label">Also share with</p>
      <div class="chips">${data.connections.map((c) => html`
        <button type="button" class="chip" data-share="${c.uid}" aria-pressed="${state.sharedWith.includes(c.uid)}">${c.name}</button>`)}</div>`);
  };
  const renderIntro = () => setHtml($("[data-intro]"), introRow(data.intro, state.useIntro, { cohost: true }));
  const renderCost = () => {
    const cost = session.cost(state.minutes);
    const enough = session.credits >= cost;
    $("[data-cost]").textContent = `Uses up to ${cost} credit${cost === 1 ? "" : "s"}; unused minutes come back · you have ${session.credits}`;
    $("[data-cost]").classList.toggle("short", !enough);
    $("[data-go]").disabled = !enough;
    renderFree();
  };
  const renderChips = () => {
    setHtml($("[data-chips]"), html`${state.attachments.map((a, i) => html`
      <span class="attach-chip">${icon(KIND_ICON[a.kind] || "file", 14)}<span class="ellipsis">${a.name}</span>
        <button type="button" data-remove="${i}" aria-label="Remove ${a.name}">${icon("close", 14, 2.2)}</button></span>`)}`);
    updateMeta();
  };
  const collectNotes = () => [notes.value.trim(), ...state.attachments.map((a) => `--- ${a.name} ---\n${a.text}`)]
    .filter(Boolean).join("\n\n");
  const updateMeta = () => {
    const n = collectNotes().length;
    const max = session.options.cohost_source_chars || 20000;
    $("[data-meta]").textContent = state.busy || (!n ? ""
      : n > max ? `${n.toLocaleString()} characters. Your co-host reads the first ${max.toLocaleString()}.`
        : `${n.toLocaleString()} characters of notes`);
  };
  const setError = (msg) => { $("[data-error]").textContent = msg || ""; };

  // ---- events ----------------------------------------------------------------------------

  el.addEventListener("click", (e) => {
    const t = e.target;
    if (t.closest("[data-host]")) editHost();
    else if (t.closest("[data-mode]")) {
      state.mode = t.closest("[data-mode]").dataset.mode;
      renderModes();
      tint();
    } else if (t.closest("[data-min]")) {
      state.minutes = Number(t.closest("[data-min]").dataset.min);
      renderLengths();
    } else if (t.closest("[data-vis]")) {
      state.visibility = t.closest("[data-vis]").dataset.vis;
      renderVisibility();
    } else if (t.closest("[data-share]")) {
      const uid = t.closest("[data-share]").dataset.share;
      state.sharedWith = state.sharedWith.includes(uid) ? state.sharedWith.filter((u) => u !== uid) : [...state.sharedWith, uid];
      renderVisibility();
    } else if (t.closest("[data-use-intro]")) {
      state.useIntro = !state.useIntro;
      renderIntro();
    } else if (t.closest("[data-pick]")) {
      $("[data-file]").click();
    } else if (t.closest("[data-link-toggle]")) {
      const f = $("[data-link-form]");
      f.hidden = !f.hidden;
      if (!f.hidden) f.querySelector("input").focus();
    } else if (t.closest("[data-remove]")) {
      state.attachments.splice(Number(t.closest("[data-remove]").dataset.remove), 1);
      renderChips();
    } else if (t.closest("[data-go]")) {
      start(t.closest("[data-go]"));
    }
  });
  const addNotes = async (fd, busy) => {
    state.busy = busy;
    updateMeta();
    try {
      const res = await api.extract(fd);
      for (const s of res.sources) state.attachments.push({ name: s.name, kind: s.kind, text: s.text });
      state.busy = "";
      renderChips();
      return true;
    } catch (err) {
      state.busy = "";
      updateMeta();
      setError(err.message);
      return false;
    }
  };
  $("[data-file]").addEventListener("change", async (e) => {
    const files = [...e.target.files];
    e.target.value = "";
    if (!files.length) return;
    const fd = new FormData();
    for (const f of files) fd.append("files", f);
    setError("");
    await addNotes(fd, `Reading ${files.length === 1 ? files[0].name : `${files.length} files`}…`);
  });
  $("[data-link-form]").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = e.currentTarget.querySelector("input");
    if (!input.value.trim()) return;
    const fd = new FormData();
    fd.append("url", input.value.trim());
    setError("");
    if (await addNotes(fd, "Fetching the page…")) {
      input.value = "";
      e.currentTarget.hidden = true;
    }
  });
  notes.addEventListener("input", updateMeta);

  function editHost() {
    const form = document.createElement("form");
    form.className = "stack";
    setHtml(form, html`
      <p class="field-label">Cast a persona</p>
      ${data.personas.length ? html`
        <p class="faint small">Yours</p>
        <div class="chips">${data.personas.map((p) => html`<button type="button" class="chip" data-persona="${p.id}">${p.name}</button>`)}</div>` : ""}
      <p class="faint small">Premade</p>
      <div class="chips">${presets.map((p) => html`<button type="button" class="chip" data-preset="${p.id}">${p.name}</button>`)}</div>
      ${personaFields(state.host, { prefix: "co", required: true })}
      <p class="error" role="alert" data-error></p>
      <button class="btn white big block" type="submit">Done</button>`);
    bindPersonaForm(form, "co");
    const sheet = openSheet({ title: "Your co-host", body: form });
    form.addEventListener("click", (e) => {
      const mine = data.personas.find((x) => x.id === e.target.closest("[data-persona]")?.dataset.persona);
      const preset = session.personaPreset(e.target.closest("[data-preset]")?.dataset.preset);
      if (mine || preset) fillPersona(form, mine || preset, "co");
    });
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      state.host = hostOf(readPersona(form, "co"));
      renderHost();
      sheet.close();
    });
  }

  // ---- start the call ----------------------------------------------------------------------

  async function start(btn) {
    setError("");
    const topic = $("#topic").value.trim();
    if (!topic) return setError("Say what you'll talk about, even just a few words.");
    if (!session.options.api_key_set) return setError("GEMINI_API_KEY isn't set on the server, so calls can't start.");
    const yourName = $("#your-name").value.trim();
    const language = $("#language").value.trim() || "English";
    local.set("gp_cohost", {
      host: { ...state.host, context: "" },  // private notes aren't kept in the browser
      mode: state.mode, minutes: state.minutes, visibility: state.visibility, yourName, language,
    });
    btn.disabled = true;
    let mic;
    try {
      mic = await startCallAudio();          // inside the click: the mic and audio unlock
    } catch (err) {
      setError(err.message);
      btn.disabled = false;
      return;
    }
    try {
      const res = await api.createCohost({
        request: {
          host: state.host, mode: state.mode, topic, notes: collectNotes(), minutes: state.minutes,
          notes_names: state.attachments.map((a) => a.name).slice(0, 20), your_name: yourName, language,
        },
        visibility: state.visibility,
        shared_with: state.visibility === "private" ? state.sharedWith : [],
        use_show_intro: state.useIntro,
      });
      if (res.trimmed) toast("Your notes were long, so your co-host reads the first part.");
      session.refresh();
      navigate(`/cohost/${res.id}`);
    } catch (err) {
      takeHandoff();
      mic.close();
      if (err.status === 401) return navigate(loginPath("/cohost"));
      setError(err.message);
      btn.disabled = false;
      session.refresh().then(renderCost);
    }
  }

  // ---- first paint ----------------------------------------------------------------------------

  if (!micSupported()) {
    setError(window.isSecureContext ? "This browser can't stream the mic. Try a recent Chrome, Safari or Firefox."
      : "Co-hosting needs the mic, which needs a secure connection (https).");
  }
  renderHost();
  renderModes();
  renderLengths();
  renderVisibility();
  renderIntro();
  renderChips();
  tint();
  const [personas, conns, intro] = await Promise.all([
    api.list("personas").catch(() => ({ items: [] })),
    api.connections().catch(() => ({ connected: [] })),
    api.showIntro().catch(() => ({ intro: null })),
  ]);
  data.personas = personas.items;
  data.connections = conns.connected;
  data.intro = intro.intro;
  renderVisibility();
  renderIntro();
  $("#topic").focus();
  return { unmount() { offHead(); } };
}
