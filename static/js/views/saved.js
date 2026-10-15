// /personas and /styles: the speakers and styles you keep for your pods.
import { api } from "../api.js";
import { html, setHtml } from "../html.js";
import { icon } from "../icons.js";
import { bindPersonaForm, personaCard, personaFields, readPersona } from "../personas.js";
import { navigate } from "../router.js";
import { loginPath, session } from "../session.js";
import { applyAccent } from "../theme.js";
import { confirmSheet, openSheet } from "../ui.js";
import { toast } from "../util.js";
import { appHeader, bindHeader } from "./library.js";

const ACCENTS = ["#B8336A", "#3B3F9E", "#B5521C", "#3A4250", "#1F7F6A", "#80620F", "#6A4FB0", "#2E6F9E", "#2F6BD0", "#9C4A5A"];

const KINDS = {
  personas: {
    title: "Personas",
    lede: "Speakers you cast as hosts: who they are, a voice that fits, what they love and know, and private notes only they draw on. Reuse them in any pod.",
    empty: "No personas yet. Make one, or start from a premade persona below.",
    noun: "persona",
    card: (p) => personaCard(p),
    form: (p) => personaFields(p),
    bind: (f) => bindPersonaForm(f),
    read: (f) => readPersona(f),
  },
  styles: {
    title: "Styles",
    lede: "Your own vibes: how the pod should sound. They show up next to the presets when you create a pod.",
    empty: "No styles yet. Describe one, like “Shakespeare, but a cooking show”.",
    noun: "style",
    card: (s) => html`
      <span class="style-swatch" style="--tile:${s.accent}" aria-hidden="true"></span>
      <span class="col grow"><strong>${s.name}</strong><span class="muted small">${s.prompt}</span></span>`,
    form: (s) => html`
      <label class="field-label" for="f-name">Name</label>
      <input id="f-name" class="field" maxlength="40" required value="${s.name || ""}" placeholder="Cozy science">
      <label class="field-label" for="f-prompt">How it should sound</label>
      <textarea id="f-prompt" class="field area" maxlength="2000" required placeholder="Warm and nerdy, like two friends explaining science over tea. Lots of analogies, no jargon.">${s.prompt || ""}</textarea>
      <p class="field-label">Color</p>
      <div class="swatches" role="radiogroup" aria-label="Color">${ACCENTS.map((a) => html`
        <button type="button" role="radio" class="swatch" data-accent="${a}" aria-checked="${(s.accent || ACCENTS[0]) === a}" style="--tile:${a}" aria-label="${a}"></button>`)}</div>`,
    read: (f) => ({ name: f.querySelector("#f-name").value.trim(), prompt: f.querySelector("#f-prompt").value.trim(),
                    accent: f.querySelector(".swatch[aria-checked=true]")?.dataset.accent || ACCENTS[0] }),
  },
};

export async function savedView(root, { url }) {
  const kind = url.pathname === "/styles" ? "styles" : "personas";
  if (!session.signedIn) {
    navigate(loginPath(url.pathname), { replace: true });
    return null;
  }
  const k = KINDS[kind];
  applyAccent(document.documentElement, "#6A4FB0");
  let items = null;
  const el = document.createElement("div");
  el.className = "page-wrap";
  setHtml(el, html`
    ${appHeader({ search: false })}
    <main class="page wide">
      <div class="page-title row">
        <div class="col grow"><h1>${k.title}</h1><p class="muted">${k.lede}</p></div>
        <button class="btn white" type="button" data-new>${icon("plus", 16, 2.6)}New ${k.noun}</button>
      </div>
      <div data-list></div>
      ${kind === "personas" ? html`
        <h2 class="section-h">Premade personas</h2>
        <p class="muted small">Cast them as they are when you create a pod, or copy one here and make it your own.</p>
        <div class="saved-grid" data-presets></div>` : ""}
    </main>`);
  root.append(el);
  const offHead = bindHeader(el);

  const renderList = () => setHtml(el.querySelector("[data-list]"), items === null ? html`<p class="empty">Loading…</p>`
    : items.length ? html`<div class="saved-grid">${items.map((it) => html`
        <div class="saved-card">${k.card(it)}
          <span class="row saved-actions">
            <button class="icon-btn small" type="button" data-edit="${it.id}" aria-label="Edit ${it.name}">${icon("edit", 18)}</button>
            <button class="icon-btn small" type="button" data-delete="${it.id}" aria-label="Delete ${it.name}">${icon("trash", 18)}</button>
          </span></div>`)}</div>`
      : html`<p class="empty">${k.empty}</p>`);

  if (kind === "personas") {
    const presets = session.options.persona_presets || [];
    setHtml(el.querySelector("[data-presets]"), html`${presets.map((p) => html`
      <div class="saved-card">${personaCard(p)}
        <span class="row saved-actions"><button class="btn chip-btn small-btn" type="button" data-copy="${p.id}">Customize</button></span></div>`)}`);
    el.querySelector("[data-presets]").addEventListener("click", (e) => {
      const b = e.target.closest("[data-copy]");
      if (!b) return;
      const { id, ...preset } = session.personaPreset(b.dataset.copy);
      edit({ ...preset, fromPreset: true });  // a new persona, prefilled
    });
  }

  const edit = (item = {}) => {
    const form = document.createElement("form");
    form.className = "stack";
    setHtml(form, html`${k.form(item)}<p class="error" role="alert" data-error></p>
      <button class="btn white big block" type="submit">${item.id ? "Save" : `Add ${k.noun}`}</button>`);
    k.bind?.(form);
    form.addEventListener("click", (e) => {
      const sw = e.target.closest("[data-accent]");
      if (sw) for (const b of form.querySelectorAll("[data-accent]")) b.setAttribute("aria-checked", String(b === sw));
    });
    const sheet = openSheet({
      title: item.id ? `Edit ${k.noun}` : item.fromPreset ? `Customize ${item.name}` : `New ${k.noun}`, body: form,
    });
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const body = k.read(form);
        if (item.id) {
          const saved = await api.update(kind, item.id, body);
          items = items.map((x) => (x.id === item.id ? saved : x));
        } else {
          items.push(await api.create(kind, body));
        }
        renderList();
        sheet.close();
      } catch (err) {
        form.querySelector("[data-error]").textContent = err.message;
      }
    });
  };

  el.addEventListener("click", async (e) => {
    if (e.target.closest("[data-new]")) return edit();
    const ed = e.target.closest("[data-edit]");
    if (ed) return edit(items.find((x) => x.id === ed.dataset.edit));
    const del = e.target.closest("[data-delete]");
    if (del) {
      const it = items.find((x) => x.id === del.dataset.delete);
      if (!(await confirmSheet({ title: `Delete ${it.name}?`, message: "Pods that already used it aren't affected.", confirm: "Delete", danger: true }))) return;
      await api.remove(kind, it.id).catch((err) => toast(err.message));
      items = items.filter((x) => x.id !== it.id);
      renderList();
    }
  });

  renderList();
  items = (await api.list(kind).catch(() => ({ items: [] }))).items;
  renderList();
  return { unmount() { offHead(); } };
}
