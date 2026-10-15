// /new — set up a pod: source, template or vibe, hosts and personas, length,
// who can listen. Creating needs credits; going live spends them.
import { api } from "../api.js";
import { unlockAudio } from "../audio/context.js";
import { html, setHtml } from "../html.js";
import { icon, KIND_ICON } from "../icons.js";
import { bindPersonaForm, fillPersona, GENDER_LABEL, personaFields, readPersona } from "../personas.js";
import { navigate } from "../router.js";
import { loginPath, session } from "../session.js";
import { vibeTile } from "../sheets.js";
import { applyAccent, initial } from "../theme.js";
import { openSheet } from "../ui.js";
import { store as local, toast } from "../util.js";
import { appHeader, bindHeader, templateCard } from "./library.js";

const LENGTHS = [5, 10, 15];
const BLANK_HOST = { name: "", voice: "auto", persona: "", gender: "", likes: "", specialties: "", skills: "", context: "" };
const hostOf = (h) => ({ ...BLANK_HOST, ...Object.fromEntries(Object.keys(BLANK_HOST).map((k) => [k, h?.[k] ?? BLANK_HOST[k]])) });
const VISIBILITY = [
  ["private", "Private", "lock", "Only you, plus connections you pick."],
  ["connections", "Connections", "people", "All your connections can listen and call in."],
  ["public", "Public", "globe", "Anyone can listen, free. Signed-in listeners can call in."],
];

export async function createView(root, { query }) {
  if (!session.signedIn) {
    navigate(loginPath("/new"), { replace: true });
    return null;
  }
  await session.refresh();
  const saved = local.get("gp_create", {});
  const state = {
    text: "",
    attachments: [],          // { name, kind, text }
    pick: { kind: "preset", id: saved.vibe || "gossip" },   // preset | style | template | own
    own: "",
    saveStyle: false,
    hosts: (saved.hosts || [{}, {}]).map(hostOf),
    albums: [],               // album ids this pod goes into
    minutes: LENGTHS.includes(saved.minutes) ? saved.minutes : 10,
    research: Boolean(saved.research),
    language: saved.language || "English",
    visibility: saved.visibility || "private",
    sharedWith: [],
    busy: "",
  };
  const data = { personas: [], styles: [], connections: [], albums: [] };

  const el = document.createElement("div");
  el.className = "create-page";
  setHtml(el, html`
    ${appHeader({ search: false })}
    <main class="create">
      <div class="create-title">
        <h1>New pod</h1>
        <p class="muted">Start with anything. Add more while it's live.</p>
      </div>
      <div data-free></div>

      <section class="tpl-section" aria-labelledby="tpl-h">
        <h2 id="tpl-h" class="create-h2 first">Start from a template <span class="faint small">optional</span></h2>
        <div class="tpl-row" data-templates></div>
      </section>

      <section class="card about-box" aria-labelledby="about-label">
        <label id="about-label" for="about-text" class="card-label">What's it about?</label>
        <textarea id="about-text" class="field area" placeholder="Genesis, chapters 1 to 3. Or paste an article, meeting notes, last quarter's numbers…"></textarea>
        <div class="attach-chips" data-chips></div>
        <div class="row gap tools">
          <button type="button" class="btn outline grow" data-pick="file">${icon("file", 16)}File</button>
          <button type="button" class="btn outline grow" data-link-toggle>${icon("link", 16)}Link</button>
          <button type="button" class="btn outline grow" data-pick="photo">${icon("photo", 16)}Photo</button>
          <input type="file" data-file="file" multiple hidden accept="${[...(session.options.file_types || []), ...(session.options.image_types || [])].join(",")}">
          <input type="file" data-file="photo" hidden accept="${(session.options.image_types || []).join(",") || "image/*"}">
        </div>
        <form class="link-form row gap" data-link-form hidden>
          <label class="sr-only" for="link-url">Link to a page or PDF</label>
          <input id="link-url" class="field" type="url" inputmode="url" placeholder="https://…">
          <button class="btn quiet" type="submit">Fetch</button>
        </form>
        <p class="meta" data-meta aria-live="polite"></p>
      </section>

      <h2 class="create-h2">Pick a vibe</h2>
      <div class="vibes" data-vibes role="group" aria-label="Styles"></div>
      <label class="sr-only" for="own-style">Describe your own style</label>
      <input id="own-style" class="field pill-field" maxlength="2000" placeholder="Or describe your own: Bible stories as girl gossip">
      <label class="check-row save-style" data-save-style hidden><input type="checkbox" name="save_style"><span>Save this to my styles</span></label>

      <h2 class="create-h2">Who's hosting?</h2>
      <div class="host-cards" data-hosts></div>
      <button type="button" class="btn link" data-host-count></button>

      <h2 class="create-h2">How long?</h2>
      <div class="len-chips" role="group" aria-label="Length" data-lengths></div>

      <h2 class="create-h2">Research <span class="faint small">optional</span></h2>
      <div data-research></div>

      <h2 class="create-h2">Who can listen?</h2>
      <div class="radio-cards" role="radiogroup" aria-label="Who can listen" data-visibility></div>
      <div data-share-with></div>

      <h2 class="create-h2">Album and tags <span class="faint small">optional</span></h2>
      <div class="chips" data-albums role="group" aria-label="Albums"></div>
      <label class="sr-only" for="tags">Tags</label>
      <input id="tags" class="field pill-field tags-field" maxlength="300" placeholder="Tags, separated by commas: faith, genesis, comedy">

      <details class="more-options">
        <summary>More options</summary>
        <label class="field-label" for="language">Language</label>
        <input id="language" class="field" maxlength="60" value="${state.language}">
      </details>

      <p class="error" role="alert" data-error></p>
      <div class="go-bar">
        <p class="cost" data-cost></p>
        <button class="btn white big block go" type="button" data-go><span class="rec-dot" aria-hidden="true"></span>Go live</button>
        <button class="btn link" type="button" data-draft>Save for later</button>
      </div>
    </main>`);
  root.append(el);
  const offHead = bindHeader(el);
  const $ = (s) => el.querySelector(s);
  const textArea = $("#about-text");
  const own = $("#own-style");

  // ---- what the current pick means -------------------------------------------------

  const picked = () => {
    const p = state.pick;
    if (p.kind === "own") return { style: state.own, label: null, accent: null, vibe: null };
    if (p.kind === "template") {
      const t = session.template(p.id);
      return t ? { style: t.style, label: t.label, accent: t.accent, vibe: null } : null;
    }
    if (p.kind === "style") {
      const s = data.styles.find((x) => x.id === p.id);
      return s ? { style: s.prompt, label: s.name, accent: s.accent, vibe: null } : null;
    }
    const preset = session.preset(p.id);
    return preset ? { style: preset.prompt, label: null, accent: preset.accent, vibe: preset.id } : null;
  };
  const tint = () => applyAccent(document.documentElement, picked()?.accent || "#2F6BD0");

  // ---- rendering ----------------------------------------------------------------------

  const renderFree = () => {
    const cost = session.cost(state.minutes, state.research);
    setHtml($("[data-free]"), session.credits >= cost ? "" : html`
      <div class="free-note">${icon("coin", 18)}<span>
        ${session.credits ? html`<strong>You have ${session.credits} credits.</strong> This pod needs ${cost}. Pick a shorter length, or ask an admin for more.`
          : html`<strong>Free account.</strong> Creating pods uses credits (${session.options.credits_per_minute} per minute). You can still set one up, save personas and styles, and listen to and call in to other pods. Ask an admin for demo credits.`}
      </span></div>`);
  };
  const renderTemplates = () => setHtml($("[data-templates]"), html`${(session.options.templates || []).map((t) =>
    templateCard(t, { selected: state.pick.kind === "template" && state.pick.id === t.id }))}`);
  const renderVibes = () => {
    const presets = session.options.presets.map((p) => vibeTile(p, state.pick.kind === "preset" && state.pick.id === p.id));
    const mine = data.styles.map((s) => html`
      <button type="button" class="vibe" data-style="${s.id}" aria-pressed="${state.pick.kind === "style" && state.pick.id === s.id}" style="--tile:${s.accent}">
        <span class="vibe-label">${s.name}</span><span class="vibe-mine">Yours</span>
        <span class="vibe-shape" aria-hidden="true"></span>
        ${state.pick.kind === "style" && state.pick.id === s.id ? html`<span class="vibe-check" aria-hidden="true">${icon("check", 14, 3)}</span>` : ""}
      </button>`);
    setHtml($("[data-vibes]"), html`${presets}${mine}`);
    $("[data-save-style]").hidden = state.pick.kind !== "own";
  };
  const renderHosts = () => {
    setHtml($("[data-hosts]"), html`${state.hosts.map((h, i) => html`
      <button type="button" class="host-card" data-host="${i}" aria-label="Edit host ${i + 1}">
        <span class="avatar" style="--av:${session.hostColor(i).avatar};--size:36px">${h.name ? initial(h.name) : i + 1}</span>
        <span class="col"><span class="strong">${h.name || "Auto name"}</span>
          <span class="muted small">${h.persona || "The show writes a persona"}</span>
          <span class="faint small">${[h.gender ? GENDER_LABEL[h.gender] : "", h.voice === "auto" ? "Auto voice" : `${h.voice} voice`].filter(Boolean).join(" · ")}
            ${h.context ? html` · ${icon("lock", 11)} notes` : ""}</span></span>
      </button>`)}`);
    $("[data-host-count]").textContent = state.hosts.length === 2 ? "Switch to one host" : "Switch to two hosts";
  };
  const renderLengths = () => {
    setHtml($("[data-lengths]"), html`${LENGTHS.map((m) => html`
      <button type="button" class="len-chip" data-min="${m}" aria-pressed="${state.minutes === m}">${m} min</button>`)}`);
    renderCost();
  };
  const renderVisibility = () => {
    setHtml($("[data-visibility]"), html`${VISIBILITY.map(([id, title, ic, desc]) => html`
      <button type="button" role="radio" class="radio-card" data-vis="${id}" aria-checked="${state.visibility === id}">
        <span class="radio-dot" aria-hidden="true"></span>
        <span class="radio-text"><span class="radio-title">${icon(ic, 16)} ${title}</span><span class="radio-desc">${desc}</span></span>
      </button>`)}`);
    setHtml($("[data-share-with]"), state.visibility !== "private" ? "" : data.connections.length ? html`
      <p class="field-label share-label">Also share with</p>
      <div class="chips">${data.connections.map((c) => html`
        <button type="button" class="chip" data-share="${c.uid}" aria-pressed="${state.sharedWith.includes(c.uid)}">${c.name}</button>`)}</div>`
      : html`<p class="faint small share-label">Want friends to hear it? <a href="/connections" data-link>Add connections</a>.</p>`);
  };
  const renderAlbums = () => {
    setHtml($("[data-albums]"), html`${data.albums.map((a) => html`
      <button type="button" class="chip" data-album="${a.id}" aria-pressed="${state.albums.includes(a.id)}">${a.title}</button>`)}
      <button type="button" class="chip dashed" data-new-album>${icon("plus", 14, 2.6)}New album</button>`);
  };
  const renderResearch = () => {
    const extra = session.options.research_credits_per_minute || 0;
    setHtml($("[data-research]"), html`
      <button type="button" role="switch" class="radio-card research-card" data-toggle-research aria-checked="${state.research}">
        <span class="radio-dot" aria-hidden="true"></span>
        <span class="radio-text"><span class="radio-title">${icon("search", 16)} Let the hosts look things up</span>
          <span class="radio-desc">When a call-in or a chapter needs facts your source doesn't have (the latest stats,
            recent news), the hosts search Google while they keep talking, then answer and say where it's from.
            ${extra ? `+${extra} credit${extra === 1 ? "" : "s"} a minute.` : ""}</span></span>
      </button>`);
  };
  const renderCost = () => {
    const cost = session.cost(state.minutes, state.research);
    const enough = session.credits >= cost;
    $("[data-cost]").textContent = `Uses ${cost} credit${cost === 1 ? "" : "s"} when it goes live · you have ${session.credits}`;
    $("[data-cost]").classList.toggle("short", !enough);
    $("[data-go]").disabled = !enough;
    $("[data-draft]").disabled = !enough;
    renderFree();
  };
  const renderChips = () => {
    setHtml($("[data-chips]"), html`${state.attachments.map((a, i) => html`
      <span class="attach-chip">${icon(KIND_ICON[a.kind] || "file", 14)}<span class="ellipsis">${a.name}</span>
        <button type="button" data-remove="${i}" aria-label="Remove ${a.name}">${icon("close", 14, 2.2)}</button></span>`)}`);
    updateMeta();
  };
  const collect = () => {
    const parts = [];
    const typed = textArea.value.trim();
    if (typed) parts.push(typed);
    for (const a of state.attachments) parts.push(`--- ${a.name} ---\n${a.text}`);
    return parts.join("\n\n");
  };
  const updateMeta = () => {
    const n = collect().length;
    const max = session.options.max_source_chars;
    $("[data-meta]").textContent = state.busy || (!n ? ""
      : n > max ? `${n.toLocaleString()} characters. Only the first ${max.toLocaleString()} will be used.`
        : `${n.toLocaleString()} characters of source material`);
  };
  const setError = (msg) => { $("[data-error]").textContent = msg || ""; };

  const applyTemplate = (id) => {
    const t = session.template(id);
    if (!t) return;
    state.pick = { kind: "template", id };
    state.own = "";
    own.value = "";
    state.hosts = t.hosts.map(hostOf);
    state.minutes = LENGTHS.includes(t.minutes) ? t.minutes : 10;
    renderTemplates();
    renderVibes();
    renderHosts();
    renderLengths();
    tint();
  };

  // ---- sources ------------------------------------------------------------------------

  const addSources = async (fd, busy) => {
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

  // ---- events --------------------------------------------------------------------------

  el.addEventListener("click", async (e) => {
    const t = e.target;
    const tpl = t.closest("[data-template]");
    const vibe = t.closest("[data-vibe]");
    const mine = t.closest("[data-style]");
    if (tpl) {
      applyTemplate(tpl.dataset.template);
    } else if (vibe || mine) {
      state.pick = vibe ? { kind: "preset", id: vibe.dataset.vibe } : { kind: "style", id: mine.dataset.style };
      state.own = "";
      own.value = "";
      renderTemplates();
      renderVibes();
      tint();
    } else if (t.closest("[data-min]")) {
      state.minutes = Number(t.closest("[data-min]").dataset.min);
      renderLengths();
    } else if (t.closest("[data-toggle-research]")) {
      state.research = !state.research;
      renderResearch();
      renderCost();
    } else if (t.closest("[data-host]")) {
      editHost(Number(t.closest("[data-host]").dataset.host));
    } else if (t.closest("[data-host-count]")) {
      state.hosts = state.hosts.length === 2 ? [state.hosts[0]] : [state.hosts[0], hostOf({})];
      renderHosts();
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
      newAlbum();
    } else if (t.closest("[data-pick]")) {
      el.querySelector(`[data-file="${t.closest("[data-pick]").dataset.pick}"]`).click();
    } else if (t.closest("[data-link-toggle]")) {
      const f = $("[data-link-form]");
      f.hidden = !f.hidden;
      if (!f.hidden) f.querySelector("input").focus();
    } else if (t.closest("[data-remove]")) {
      state.attachments.splice(Number(t.closest("[data-remove]").dataset.remove), 1);
      renderChips();
    } else if (t.closest("[data-go]")) {
      submit(true, t.closest("[data-go]"));
    } else if (t.closest("[data-draft]")) {
      submit(false, t.closest("[data-draft]"));
    }
  });
  for (const input of el.querySelectorAll("[data-file]")) {
    input.addEventListener("change", async () => {
      const files = [...input.files];
      input.value = "";
      if (!files.length) return;
      const fd = new FormData();
      for (const f of files) fd.append("files", f);
      setError("");
      await addSources(fd, `Reading ${files.length === 1 ? files[0].name : `${files.length} files`}…`);
    });
  }
  $("[data-link-form]").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    const input = form.querySelector("input");
    if (!input.value.trim()) return;
    const fd = new FormData();
    fd.append("url", input.value.trim());
    setError("");
    if (await addSources(fd, "Fetching the page…")) {
      input.value = "";
      form.hidden = true;
    }
  });
  textArea.addEventListener("input", updateMeta);
  own.addEventListener("input", () => {
    state.own = own.value.trim();
    if (state.own) state.pick = { kind: "own" };
    else if (state.pick.kind === "own") state.pick = { kind: "preset", id: "gossip" };
    renderTemplates();
    renderVibes();
    tint();
  });

  // ---- hosts and personas -------------------------------------------------------------------

  function editHost(i) {
    const h = state.hosts[i];
    const presets = session.options.persona_presets || [];
    const form = document.createElement("form");
    form.className = "stack";
    setHtml(form, html`
      <p class="field-label">Cast a persona</p>
      ${data.personas.length ? html`
        <p class="faint small">Yours</p>
        <div class="chips">${data.personas.map((p) => html`<button type="button" class="chip" data-persona="${p.id}">${p.name}</button>`)}</div>` : ""}
      <p class="faint small">Premade</p>
      <div class="chips">${presets.map((p) => html`<button type="button" class="chip" data-preset="${p.id}">${p.name}</button>`)}</div>
      ${personaFields(h, { prefix: "host", required: false })}
      <label class="check-row"><input type="checkbox" name="save"><span>Save to my personas</span></label>
      <p class="error" role="alert" data-error></p>
      <button class="btn white big block" type="submit">Done</button>`);
    bindPersonaForm(form, "host");
    const sheet = openSheet({ title: `Host ${i + 1}`, body: form });
    form.addEventListener("click", (e) => {
      const mine = data.personas.find((x) => x.id === e.target.closest("[data-persona]")?.dataset.persona);
      const preset = session.personaPreset(e.target.closest("[data-preset]")?.dataset.preset);
      if (mine || preset) fillPersona(form, mine || preset, "host");
      if (e.target.closest("a[data-link]")) sheet.close();
    });
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const next = hostOf(readPersona(form, "host"));
      if (form.querySelector("input[name=save]").checked) {
        if (!next.name || !next.persona) {
          form.querySelector("[data-error]").textContent = "Give the persona a name and a personality to save it.";
          return;
        }
        try {
          data.personas.push(await api.create("personas", next));
          toast(`${next.name} saved to your personas.`);
        } catch (err) {
          form.querySelector("[data-error]").textContent = err.message;
          return;
        }
      }
      state.hosts[i] = next;
      renderHosts();
      sheet.close();
    });
  }

  function newAlbum() {
    const form = document.createElement("form");
    form.className = "stack";
    setHtml(form, html`
      <label class="field-label" for="album-title">Album name</label>
      <input id="album-title" class="field" maxlength="80" required placeholder="Bible stories, gossip edition">
      <p class="faint small">It starts private. Change who can see it on the album's page.</p>
      <p class="error" role="alert" data-error></p>
      <button class="btn white big block" type="submit">Create album</button>`);
    const sheet = openSheet({ title: "New album", body: form });
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const album = await api.createAlbum({ title: form.querySelector("#album-title").value.trim() });
        data.albums.unshift(album);
        state.albums.push(album.id);
        renderAlbums();
        sheet.close();
      } catch (err) {
        form.querySelector("[data-error]").textContent = err.message;
      }
    });
  }

  // ---- go live ------------------------------------------------------------------------------

  async function submit(live, btn) {
    setError("");
    const source = collect();
    if (!source) return setError("Give the hosts something to talk about: type a topic, paste text, or add a file.");
    const pick = picked();
    if (!pick?.style) return setError("Pick a vibe, a template, or describe one.");
    if (live && !session.options.api_key_set) return setError("GEMINI_API_KEY isn't set on the server, so pods can't start.");
    const language = $("#language").value.trim() || "English";
    const request = {
      source, style: pick.style, vibe: pick.vibe, style_label: pick.label, accent: pick.accent,
      hosts: state.hosts, minutes: state.minutes, language, research: state.research,
      source_names: state.attachments.map((a) => a.name).slice(0, 20),
    };
    local.set("gp_create", {
      vibe: state.pick.kind === "preset" ? state.pick.id : "gossip",
      hosts: state.hosts.map((h) => ({ ...h, context: "" })),  // private notes aren't kept in the browser
      minutes: state.minutes, language, visibility: state.visibility, research: state.research,
    });
    btn.disabled = true;
    if (live) await unlockAudio();  // inside the click, so the pod can start playing on its own
    try {
      if (state.pick.kind === "own" && el.querySelector("input[name=save_style]").checked) {
        await api.create("styles", { name: state.own.slice(0, 40), prompt: state.own, accent: "#2F6BD0" }).catch(() => {});
      }
      const res = await api.createShow({
        request, live, visibility: state.visibility,
        shared_with: state.visibility === "private" ? state.sharedWith : [],
        template: state.pick.kind === "template" ? state.pick.id : null,
        album_ids: state.albums,
        tags: $("#tags").value.split(",").map((t) => t.trim()).filter(Boolean),
      });
      session.refresh();
      navigate(live ? `/show/${res.id}` : "/");
    } catch (err) {
      if (err.status === 401) return navigate(loginPath("/new"));
      setError(err.message);
      btn.disabled = false;
      session.refresh().then(renderCost);
    }
  }

  // ---- first paint ---------------------------------------------------------------------------

  renderTemplates();
  renderVibes();
  renderHosts();
  renderResearch();
  renderLengths();
  renderVisibility();
  renderChips();
  tint();
  renderAlbums();
  const [personas, styles, conns, albums] = await Promise.all([
    api.list("personas").catch(() => ({ items: [] })),
    api.list("styles").catch(() => ({ items: [] })),
    api.connections().catch(() => ({ connected: [] })),
    api.albums().catch(() => ({ albums: [] })),
  ]);
  data.personas = personas.items;
  data.styles = styles.items;
  data.connections = conns.connected;
  data.albums = albums.albums;
  const intoAlbum = query.get("album");
  if (intoAlbum && data.albums.some((a) => a.id === intoAlbum)) state.albums = [intoAlbum];
  renderAlbums();
  const fromQuery = query.get("template");
  if (fromQuery && session.template(fromQuery)) applyTemplate(fromQuery);
  renderVibes();
  renderVisibility();
  textArea.focus();
  return { unmount() { offHead(); } };
}
