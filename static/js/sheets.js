// The sheets that open over a show: Add context, Restyle, Share, Hosts, More.
import { api } from "./api.js";
import { avatar, contextList } from "./components.js";
import { html, setHtml } from "./html.js";
import { icon, VISIBILITY_ICON } from "./icons.js";
import { session } from "./session.js";
import { confirmSheet, openSheet } from "./ui.js";
import { saveBlob, slug, toast } from "./util.js";
import { Recorder } from "./audio/recorder.js";

// ---- Add context ----------------------------------------------------------------

const KINDS = [["text", "Text", "text"], ["file", "File", "file"], ["link", "Link", "link"], ["photo", "Photo", "photo"]];
const WHENS = [
  ["now", "Right now", "They break in and bring it up within the next few lines."],
  ["next", "Next chapter", "Held until the current chapter wraps up."],
  ["background", "Background only", "Facts to draw on. Never announced."],
];

/** opts.callin: text of the call-in this context rides along with. */
export function openContextSheet(ctl, { callin = "", onSent } = {}) {
  const store = ctl.store;
  const limits = session.options?.limits || {};
  const producer = store.producer;
  const state = { kind: "text", when: "now", cite: true, file: null };
  const form = document.createElement("form");
  form.className = "ctx-form";
  form.noValidate = true;

  const render = () => {
    setHtml(form, html`
      <p class="muted">Hand the hosts something new. They'll work it into the show.</p>
      <div class="segmented four" role="group" aria-label="What are you adding?">
        ${KINDS.map(([id, label, ic]) => html`
          <button type="button" class="seg-btn" data-kind="${id}" aria-pressed="${state.kind === id}">${icon(ic, 18)}${label}</button>`)}
      </div>
      <div class="ctx-input">${kindInput(state, limits, producer)}</div>
      ${callin ? html`
        <label class="check-row"><input type="checkbox" name="with_callin" checked>
          <span>Send with your call-in: <em>“${callin}”</em></span></label>` : html`
        <h3 class="sheet-h3">When should the hosts use it?</h3>
        <div class="radio-cards" role="radiogroup" aria-label="When should the hosts use it?">
          ${WHENS.map(([id, title, desc]) => html`
            <button type="button" role="radio" class="radio-card" data-when="${id}" aria-checked="${state.when === id}">
              <span class="radio-dot" aria-hidden="true"></span>
              <span class="radio-text"><span class="radio-title">${title}</span><span class="radio-desc">${desc}</span></span>
            </button>`)}
        </div>`}
      <div class="switch-row">
        <span id="cite-label">Say where it came from on air</span>
        <button type="button" role="switch" class="switch" aria-checked="${state.cite}" aria-labelledby="cite-label" data-cite></button>
      </div>
      <p class="error" role="alert" data-error></p>
      <button class="btn white big block" type="submit" data-submit>Add to the show</button>
      <h3 class="sheet-h3 rule">Already in this show</h3>
      <div class="ctx-already">${contextList(store, ctl.player.position(), { pills: true })}</div>`);
  };

  form.addEventListener("click", (e) => {
    const kind = e.target.closest("[data-kind]");
    const when = e.target.closest("[data-when]");
    const choose = e.target.closest("[data-choose]");
    if (kind) {
      saveTyped();
      state.kind = kind.dataset.kind;
      state.file = null;
      render();
    } else if (when) {
      saveTyped();
      state.when = when.dataset.when;
      render();
    } else if (e.target.closest("[data-cite]")) {
      saveTyped();
      state.cite = !state.cite;
      render();
    } else if (choose) {
      form.querySelector("input[type=file]")?.click();
    }
  });
  form.addEventListener("change", (e) => {
    if (e.target.type === "file") {
      state.file = e.target.files[0] || null;
      saveTyped();
      render();
    }
  });
  const saveTyped = () => {
    state.text = form.querySelector("textarea[name=text]")?.value ?? state.text;
    state.url = form.querySelector("input[name=url]")?.value ?? state.url;
  };

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    saveTyped();
    const err = form.querySelector("[data-error]");
    const fd = new FormData();
    fd.append("kind", state.kind);
    fd.append("when", callin ? "now" : state.when);
    fd.append("cite", String(state.cite));
    if (state.kind === "text") {
      if (!state.text?.trim()) return (err.textContent = "Write something for the hosts first.");
      fd.append("text", state.text);
    } else if (state.kind === "link") {
      if (!state.url?.trim()) return (err.textContent = "Paste a link first.");
      fd.append("url", state.url.trim());
    } else {
      if (!state.file) return (err.textContent = state.kind === "photo" ? "Choose a photo first." : "Choose a file first.");
      fd.append("file", state.file, state.file.name);
    }
    const sendCallin = callin && form.querySelector("input[name=with_callin]")?.checked;
    const btn = form.querySelector("[data-submit]");
    btn.disabled = true;
    btn.textContent = state.kind === "link" ? "Reading the page…" : state.kind === "photo" ? "Looking at the photo…" : "Adding…";
    try {
      const res = await ctl.addContext(fd, { withComment: sendCallin ? callin : "" });
      toast(`${res.context.name} is with the hosts now.`);
      sheet.close();
      onSent?.(Boolean(sendCallin));
    } catch (ex) {
      err.textContent = ex.message;
      btn.disabled = false;
      btn.textContent = "Add to the show";
    }
  });

  render();
  const sheet = openSheet({ title: callin ? "Attach to your call-in" : "Add context", body: form, className: "ctx-sheet" });
  const off = store.on(() => {
    const list = form.querySelector(".ctx-already");
    if (list) setHtml(list, contextList(store, ctl.player.position(), { pills: true }));
  });
  const close = sheet.close;
  sheet.close = () => { off(); close(); };
  return sheet;
}

function kindInput(state, limits, producer) {
  const textMax = producer ? limits.producer_text_chars : limits.guest_text_chars;
  const fileMb = producer ? limits.producer_file_mb : limits.guest_file_mb;
  if (state.kind === "text") {
    return html`<label class="sr-only" for="ctx-text">Context for the hosts</label>
      <textarea id="ctx-text" name="text" class="field area" maxlength="${textMax || 4000}"
        placeholder="${producer ? "A note for the hosts, like “Keep it PG” or the next chapter's facts" : "Something the hosts should know"}">${state.text || ""}</textarea>`;
  }
  if (state.kind === "link") {
    return html`<label class="sr-only" for="ctx-link">Link</label>
      <input id="ctx-link" name="url" class="field" type="url" inputmode="url" placeholder="https://…" value="${state.url || ""}">
      <p class="hint">The hosts read the page when you add it.</p>`;
  }
  const photo = state.kind === "photo";
  const accept = photo ? (session.options?.image_types || []).join(",") || "image/*"
    : [...(session.options?.file_types || []), ...(session.options?.image_types || [])].join(",");
  return html`
    <input type="file" name="file" accept="${accept}" hidden>
    <button type="button" class="dropzone" data-choose>
      ${icon(photo ? "photoAdd" : "addFile", 26, 1.8)}
      <span class="dz-title">${state.file ? state.file.name : photo ? "Choose a photo or screenshot" : "Choose a file"}</span>
      <span class="dz-sub">${state.file ? "Tap to choose a different one"
        : photo ? "The hosts can describe what's in it" : `PDF, Word, text, CSV or HTML, up to ${fileMb || 5} MB`}</span>
    </button>`;
}

// ---- Restyle ------------------------------------------------------------------------

export function openRestyleSheet(ctl) {
  const presets = session.options?.presets || [];
  let choice = ctl.store.style?.vibe || null;
  const form = document.createElement("form");
  form.className = "stack";
  const render = () => setHtml(form, html`
    <p class="muted">The hosts switch within a couple of lines, in character.</p>
    <div class="vibes">${presets.map((p) => vibeTile(p, choice === p.id))}</div>
    <label class="sr-only" for="restyle-own">Describe your own style</label>
    <input id="restyle-own" class="field pill-field" name="own" placeholder="Or describe your own: a nature documentary about it" maxlength="2000">
    <button class="btn white big block" type="submit">Switch the style</button>`);
  form.addEventListener("click", (e) => {
    const t = e.target.closest("[data-vibe]");
    if (!t) return;
    choice = t.dataset.vibe;
    const own = form.querySelector("input").value;
    render();
    form.querySelector("input").value = own;
  });
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const own = form.querySelector("input").value.trim();
    if (own) ctl.restyle({ style: own });
    else if (choice) ctl.restyle({ vibe: choice });
    else return;
    toast("Restyling. The hosts will switch in a moment.");
    sheet.close();
  });
  render();
  const sheet = openSheet({ title: "Restyle the show", body: form, wide: true });
}

export function vibeTile(p, selected) {
  return html`
    <button type="button" class="vibe" data-vibe="${p.id}" aria-pressed="${selected}" style="--tile:${p.accent}">
      <span class="vibe-label">${p.label}</span>
      <span class="vibe-shape" aria-hidden="true"></span>
      ${selected ? html`<span class="vibe-check" aria-hidden="true">${icon("check", 14, 3)}</span>` : ""}
    </button>`;
}

// ---- Share ---------------------------------------------------------------------------

const VIS = [
  ["private", "Private", "Only you, plus connections you pick below."],
  ["connections", "Connections", "All your connections can listen and call in."],
  ["public", "Public", "Published: anyone can listen for free, even without an account."],
];

/** Owners choose who can listen; everyone can copy the link (it only opens for people with access). */
export async function openShareSheet(ctl) {
  const s = ctl.store;
  if (s.show.kind === "briefing") {
    openSheet({ title: "Just for you", body: html`<p class="muted">Briefings are private: they can't be shared,
      and they're deleted ${s.show.expires_at ? `${Math.max(1, Math.ceil((s.show.expires_at - Date.now() / 1000) / 3600))} hours from now`
        : "after two days"}. Download the audio if you want to keep it.</p>` });
    return;
  }
  const owner = s.producer;
  const state = { visibility: s.show.visibility || "private", shared: [...(s.show.shared_with || [])] };
  let connections = null;
  const div = document.createElement("div");
  div.className = "stack";
  const render = () => {
    const link = ctl.shareLink();
    setHtml(div, html`
      ${owner ? html`
        <div class="radio-cards" role="radiogroup" aria-label="Who can listen">
          ${VIS.map(([id, title, desc]) => html`
            <button type="button" role="radio" class="radio-card" data-vis="${id}" aria-checked="${state.visibility === id}">
              <span class="radio-dot" aria-hidden="true"></span>
              <span class="radio-text"><span class="radio-title">${icon(VISIBILITY_ICON[id], 16)} ${title}</span><span class="radio-desc">${desc}</span></span>
            </button>`)}
        </div>
        ${state.visibility === "private" ? connections === null ? html`<p class="faint small">Loading connections…</p>`
          : connections.length ? html`
            <p class="field-label">Share with</p>
            <div class="chips">${connections.map((c) => html`
              <button type="button" class="chip" data-share="${c.uid}" aria-pressed="${state.shared.includes(c.uid)}">${c.name}</button>`)}</div>`
            : html`<p class="faint small">No connections yet. <a href="/connections" data-link>Add some</a> to share privately.</p>` : ""}
        <p class="error" role="alert" data-error></p>
        <button class="btn white big block" type="button" data-save>Save</button>` : html`
        <p class="muted">${s.show.visibility === "public" ? "This pod is public: anyone with the link can listen."
          : "The link works for people this pod is shared with."}</p>`}
      <div class="copy-row">
        <input class="field" readonly value="${link}" aria-label="Link to this pod">
        <button class="btn quiet" type="button" data-copy>${icon("copy", 18)}Copy</button>
      </div>
      ${navigator.share ? html`<button class="btn quiet block" type="button" data-native>${icon("share", 18)}Share…</button>` : ""}`);
  };
  div.addEventListener("click", async (e) => {
    const link = ctl.shareLink();
    const vis = e.target.closest("[data-vis]");
    const share = e.target.closest("[data-share]");
    if (vis) {
      state.visibility = vis.dataset.vis;
      render();
    } else if (share) {
      const uid = share.dataset.share;
      state.shared = state.shared.includes(uid) ? state.shared.filter((u) => u !== uid) : [...state.shared, uid];
      render();
    } else if (e.target.closest("[data-save]")) {
      try {
        const r = await api.shareShow(ctl.id, state.visibility, state.visibility === "private" ? state.shared : []);
        Object.assign(s.show, { visibility: r.visibility, shared_with: r.shared_with });
        toast(r.visibility === "public" ? "Published. Anyone can listen now." : r.visibility === "connections"
          ? "Shared with your connections." : r.shared_with.length ? "Shared with the people you picked." : "Private: only you.");
        sheet.close();
      } catch (ex) {
        div.querySelector("[data-error]").textContent = ex.message;
      }
    } else if (e.target.closest("[data-copy]")) {
      try {
        await navigator.clipboard.writeText(link);
        toast("Link copied.");
      } catch {
        div.querySelector(".copy-row input").select();
      }
    } else if (e.target.closest("[data-native]")) {
      navigator.share({ title: s.show.title || "Impromptu", url: link }).catch(() => {});
    } else if (e.target.closest("a[data-link]")) {
      sheet.close();
    }
  });
  render();
  const sheet = openSheet({ title: owner ? "Who can listen?" : "Share this pod", body: div });
  if (owner) {
    connections = (await api.connections().catch(() => ({ connected: [] }))).connected;
    render();
  }
}

// ---- Hosts ---------------------------------------------------------------------------

export function openHostsSheet(ctl) {
  const s = ctl.store;
  const hosts = s.plan?.hosts || s.show.hosts || [];
  openSheet({
    title: "Your hosts",
    body: html`<ul class="host-list">${hosts.map((h, i) => html`
      <li>${avatar(h.name, session.hostColor(i).avatar, 44)}
        <span><strong>${h.name}</strong><span class="muted block">${h.persona || "Persona coming up…"}</span>
        <span class="faint small">Voice: ${h.voice}</span></span></li>`)}</ul>
      <p class="faint small">Style: ${s.styleLabelAt(ctl.player.position())}</p>`,
  });
}

// ---- More ----------------------------------------------------------------------------

export function openMoreSheet(ctl) {
  const s = ctl.store;
  const div = document.createElement("div");
  div.className = "menu";
  const live = ctl.live && !s.ended;
  const briefing = s.show.kind === "briefing";
  setHtml(div, html`
    ${live && ctl.canTalk ? html`
      <button type="button" class="menu-item" data-ctx>${icon("addFile", 20)}Add context <span class="faint">a note, file, link or photo</span></button>` : ""}
    ${live && s.producer && !briefing ? html`
      <button type="button" class="menu-item" data-restyle>${icon("restyle", 20)}Restyle the show</button>` : ""}
    <button type="button" class="menu-item" data-dl-audio>${icon("download", 20)}Download audio${live ? " so far" : ""}</button>
    ${s.producer && s.show.has_raw_audio ? html`
      <button type="button" class="menu-item" data-dl-raw>${icon("download", 20)}Download the unedited call <span class="faint">before the editor's cuts</span></button>` : ""}
    <button type="button" class="menu-item" data-dl-text>${icon("note", 20)}Download transcript</button>
    ${briefing ? html`<a class="menu-item" href="/briefings" data-link>${icon("sparkle", 20)}Your briefings</a>`
      : html`<button type="button" class="menu-item" data-share>${icon("share", 20)}Share</button>`}
    ${s.producer && live ? html`
      <button type="button" class="menu-item" data-wrap>${icon("chat", 20)}Wrap it up <span class="faint">The hosts sign off soon</span></button>
      <button type="button" class="menu-item danger" data-stop>${icon("stop", 20)}Stop now</button>` : ""}
    ${s.producer ? html`
      ${briefing ? "" : html`
      <a class="menu-item" href="/show/${ctl.id}/stats" data-link>${icon("people", 20)}Stats <span class="faint">who listened, when they left</span></a>
      <button type="button" class="menu-item" data-organize>${icon("bars", 20)}Albums and tags</button>`}
      <a class="menu-item" href="/new" data-link>${icon("plus", 20)}Start a new pod</a>` : ""}
    ${!s.producer && s.creator ? html`
      <a class="menu-item" href="/creator/${s.creator.uid}" data-link>${icon("user", 20)}More from ${s.creator.name}</a>` : ""}`);
  const sheet = openSheet({ title: s.show.title || "This show", body: div });
  div.addEventListener("click", async (e) => {
    if (e.target.closest("[data-dl-audio]")) {
      location.href = ctl.audioDownloadUrl();
    } else if (e.target.closest("[data-dl-raw]")) {
      location.href = api.rawAudioUrl(ctl.id);
    } else if (e.target.closest("[data-dl-text]")) {
      saveBlob(new Blob([s.transcriptMarkdown()], { type: "text/markdown" }), `${slug(s.show.title)}.md`);
    } else if (e.target.closest("[data-share]")) {
      sheet.close();
      openShareSheet(ctl);
      return;
    } else if (e.target.closest("[data-ctx]")) {
      sheet.close();
      openContextSheet(ctl);
      return;
    } else if (e.target.closest("[data-restyle]")) {
      sheet.close();
      openRestyleSheet(ctl);
      return;
    } else if (e.target.closest("[data-organize]")) {
      sheet.close();
      openOrganizeSheet(ctl);
      return;
    } else if (e.target.closest("[data-wrap]")) {
      ctl.end("wrap");
      toast("The hosts will wrap it up.");
    } else if (e.target.closest("[data-stop]")) {
      sheet.close();
      const ok = await confirmSheet({
        title: "Stop the show now?", message: "Everyone listening hears the show cut off. It's saved as it is.",
        confirm: "Stop now", danger: true,
      });
      if (ok) ctl.end("now");
      return;
    } else if (e.target.closest("a[data-link]")) {
      sheet.close();
      return;
    }
    sheet.close();
  });
}

// ---- Albums and tags (the pod's creator) ------------------------------------------------

export async function openOrganizeSheet(ctl) {
  const s = ctl.store;
  const div = document.createElement("div");
  div.className = "stack";
  const chosen = new Set(s.show.album_ids || s.albums.map((a) => a.id));
  let albums = null;
  const draw = () => setHtml(div, html`
    <p class="field-label">Albums</p>
    ${albums === null ? html`<p class="faint small">Loading your albums…</p>` : albums.length ? html`
      <div class="chips">${albums.map((a) => html`
        <button type="button" class="chip" data-album="${a.id}" aria-pressed="${chosen.has(a.id)}">${a.title}</button>`)}</div>`
      : html`<p class="faint small">No albums yet. <a href="/albums" data-link>Make one</a>, then file pods into it.</p>`}
    <label class="field-label" for="org-tags">Tags</label>
    <input id="org-tags" class="field" maxlength="300" value="${(s.show.tags || []).join(", ")}" placeholder="faith, genesis, comedy">
    <p class="error" role="alert" data-error></p>
    <button class="btn white big block" type="button" data-save>Save</button>`);
  draw();
  const sheet = openSheet({ title: "Albums and tags", body: div });
  albums = (await api.albums().catch(() => ({ albums: [] }))).albums;
  draw();
  div.addEventListener("click", async (e) => {
    const chip = e.target.closest("[data-album]");
    if (chip) {
      const id = chip.dataset.album;
      if (chosen.has(id)) chosen.delete(id); else chosen.add(id);
      chip.setAttribute("aria-pressed", String(chosen.has(id)));
    } else if (e.target.closest("a[data-link]")) {
      sheet.close();
    } else if (e.target.closest("[data-save]")) {
      const tags = div.querySelector("#org-tags").value.split(",").map((t) => t.trim()).filter(Boolean);
      try {
        const r = await api.organize(ctl.id, { album_ids: [...chosen], tags });
        s.show.album_ids = r.album_ids;
        s.show.tags = r.tags;
        s.albums = albums.filter((a) => r.album_ids.includes(a.id)).map((a) => ({ id: a.id, title: a.title, accent: a.accent }));
        s._changed({ type: "organized" });
        sheet.close();
        toast("Saved.");
      } catch (err) {
        div.querySelector("[data-error]").textContent = err.message;
      }
    }
  });
}

// ---- Interrupt (the producer) ---------------------------------------------------------

export function openInterruptSheet(ctl) {
  if (!ctl.canInterrupt) return;
  const wasPlaying = ctl.holdForInterrupt();
  let sent = false;
  const div = document.createElement("div");
  div.className = "interrupt";
  setHtml(div, html`
    <p class="muted small">The show's paused. Ask the hosts anything, or tell them where to take it: they answer right away, then carry on.</p>
    ${Recorder.supported ? html`
      <button type="button" class="talk-btn" data-talk aria-label="Hold to ask by voice">
        ${icon("mic", 34, 2)}<span data-talk-label>Hold to talk</span></button>` : ""}
    <form class="row gap" data-ask>
      <label class="sr-only" for="int-text">Your question or direction</label>
      <input id="int-text" class="field grow" maxlength="500" placeholder="Why did the serpent talk?" autocomplete="off">
      <button class="btn white" type="submit">Ask</button>
    </form>
    <div class="int-actions">
      <button type="button" class="btn chip-btn" data-kind="deeper">${icon("plus", 16, 2.4)}Go deeper</button>
      <button type="button" class="btn chip-btn" data-kind="move_on">${icon("jumpLive", 16)}Move on</button>
      <button type="button" class="btn chip-btn" data-kind="steer">${icon("restyle", 16)}Steer: take it where I typed</button>
    </div>
    <p class="error" role="alert" data-error></p>`);
  const sheet = openSheet({
    title: "Interrupt the show", body: div,
    onClose: () => { if (!sent) ctl.resumeAfterHold(wasPlaying); },
  });
  const input = div.querySelector("#int-text");
  const error = (msg) => { div.querySelector("[data-error]").textContent = msg || ""; };
  const send = (kind, text = "") => {
    if (ctl.interrupt(kind, text, wasPlaying)) {
      sent = true;
      sheet.close();
    } else {
      error("The show can't be interrupted right now.");
    }
  };
  div.querySelector("[data-ask]").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text) return input.focus();
    send("ask", text);
  });
  div.querySelector(".int-actions").addEventListener("click", (e) => {
    const b = e.target.closest("[data-kind]");
    if (!b) return;
    const text = input.value.trim();
    if (b.dataset.kind === "steer" && !text) {
      error("Type where you want the show to go first, e.g. \"Argue the other side\".");
      return input.focus();
    }
    send(b.dataset.kind, text);
  });

  const talk = div.querySelector("[data-talk]");
  if (!talk) return;
  const label = div.querySelector("[data-talk-label]");
  let rec = null;
  const begin = async (e) => {
    e?.preventDefault?.();
    if (rec) return;
    error("");
    rec = new Recorder();
    try {
      await rec.start();
    } catch {
      rec = null;
      return error("No microphone access. Allow it in the browser, or type instead.");
    }
    talk.classList.add("on");
    label.textContent = "Listening… let go to send";
  };
  const finish = async () => {
    if (!rec) return;
    const r = rec;
    rec = null;
    talk.classList.remove("on");
    const blob = await r.stop();
    if (blob.size < 1200) {
      label.textContent = "Hold to talk";
      return error("Hold the button while you speak.");
    }
    label.textContent = "Got it…";
    talk.disabled = true;
    try {
      const { text } = await ctl.transcribe(blob);
      input.value = text;
      send("ask", text);
    } catch (err) {
      error(err.message);
      label.textContent = "Hold to talk";
      talk.disabled = false;
    }
  };
  talk.addEventListener("pointerdown", begin);
  for (const ev of ["pointerup", "pointerleave", "pointercancel"]) talk.addEventListener(ev, finish);
  talk.addEventListener("keydown", (e) => { if ((e.key === " " || e.key === "Enter") && !e.repeat) begin(e); });
  talk.addEventListener("keyup", (e) => { if (e.key === " " || e.key === "Enter") finish(); });
}

export function downloadTranscript(ctl) {
  saveBlob(new Blob([ctl.store.transcriptMarkdown()], { type: "text/markdown" }), `${slug(ctl.store.show.title)}.md`);
}
