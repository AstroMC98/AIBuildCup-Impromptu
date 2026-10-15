// Persona editing, shared by the Personas page and the host editor on /new: who
// they are, their gender and a voice that fits it, what they like and know, and
// private notes that only this host draws on (listeners never see them).
import { api } from "./api.js";
import { html, setHtml } from "./html.js";
import { icon } from "./icons.js";
import { session } from "./session.js";
import { callerColor, initial } from "./theme.js";

export const GENDER_LABEL = { "": "Let the show decide", female: "Female", male: "Male" };
const TRAITS = [
  ["likes", "Likes", "Rainy days, jazz, a good debate"],
  ["specialties", "Specialties", "Marine biology, octopuses, ocean myths"],
  ["skills", "Skills", "Analogies, storytelling, gentle myth-busting"],
];
const NOTES_CHARS = 20000;

function voiceOptions(gender, selected) {
  return html`
    <option value="auto">Auto: ${gender ? `a ${gender} voice ` : ""}picked to fit the style</option>
    ${session.voices(gender).map((v) => html`
      <option value="${v.name}" ${v.name === selected ? "selected" : ""}>${v.name}, ${v.character.toLowerCase()}${gender ? "" : ` (${v.gender})`}</option>`)}`;
}

/** The fields of a persona (or a host). `prefix` keeps ids unique on a page. */
export function personaFields(p = {}, { prefix = "p", required = true, notes = true } = {}) {
  const gender = p.gender || session.voiceGender(p.voice) || "";
  const open = Boolean(p.likes || p.specialties || p.skills || p.context);
  return html`
    <label class="field-label" for="${prefix}-name">Name</label>
    <input id="${prefix}-name" class="field" maxlength="40" ${required ? "required" : ""} value="${p.name || ""}"
      placeholder="${required ? "Dr. Maya Reyes" : "Leave blank to let the show pick"}">
    <div class="field-pair">
      <span class="col"><label class="field-label" for="${prefix}-gender">Gender</label>
        <select id="${prefix}-gender" class="field">${["", "female", "male"].map((g) => html`
          <option value="${g}" ${g === gender ? "selected" : ""}>${GENDER_LABEL[g]}</option>`)}</select></span>
      <span class="col"><label class="field-label" for="${prefix}-voice">Voice</label>
        <select id="${prefix}-voice" class="field">${voiceOptions(gender, p.voice)}</select></span>
    </div>
    <label class="field-label" for="${prefix}-persona">Personality</label>
    <textarea id="${prefix}-persona" class="field area short" maxlength="600" ${required ? "required" : ""}
      placeholder="${required ? "A marine biologist who gets giddy about octopuses, explains with sea analogies, and gently corrects myths."
        : "Who are they? Attitude, quirks, how they play off the other host. Leave blank to let the show write one."}">${p.persona || ""}</textarea>
    <details class="persona-more" ${open ? "open" : ""}>
      <summary>Likes, specialties and skills${notes ? ", plus private notes" : ""}</summary>
      ${TRAITS.map(([key, label, hint]) => html`
        <label class="field-label" for="${prefix}-${key}">${label}</label>
        <input id="${prefix}-${key}" class="field" maxlength="300" value="${p[key] || ""}" placeholder="${hint}">`)}
      ${notes ? html`
        <label class="field-label" for="${prefix}-context">Their own notes
          <span class="faint small">· private: only this host knows them; listeners never see them</span></label>
        <textarea id="${prefix}-context" class="field area" maxlength="${NOTES_CHARS}"
          placeholder="Background, stories, expertise, opinions: anything this host should know and bring up when it fits.">${p.context || ""}</textarea>
        <div class="notes-tools">
          <button type="button" class="btn outline small-btn" data-notes-file>${icon("file", 14)}Add from a file</button>
          <button type="button" class="btn outline small-btn" data-notes-link>${icon("link", 14)}Add from a link</button>
          <input type="file" data-notes-input hidden accept="${(session.options.file_types || []).join(",")}">
        </div>
        <div class="row gap notes-link" data-notes-linkrow hidden>
          <label class="sr-only" for="${prefix}-notes-url">Link to a page or PDF</label>
          <input id="${prefix}-notes-url" class="field" type="url" inputmode="url" placeholder="https://…">
          <button type="button" class="btn quiet" data-notes-fetch>Fetch</button>
        </div>
        <p class="faint small" data-notes-meta aria-live="polite"></p>` : ""}
    </details>`;
}

export function readPersona(form, prefix = "p") {
  const val = (key) => form.querySelector(`#${prefix}-${key}`)?.value.trim() ?? "";
  return {
    name: val("name"), gender: val("gender"), voice: val("voice") || "auto", persona: val("persona"),
    likes: val("likes"), specialties: val("specialties"), skills: val("skills"), context: val("context"),
  };
}

/** Put a persona's values into the fields (casting a saved or premade persona). */
export function fillPersona(form, p, prefix = "p") {
  const gender = p.gender || session.voiceGender(p.voice) || "";
  for (const key of ["name", "persona", "likes", "specialties", "skills", "context"]) {
    const el = form.querySelector(`#${prefix}-${key}`);
    if (el) el.value = p[key] || "";
  }
  form.querySelector(`#${prefix}-gender`).value = gender;
  setHtml(form.querySelector(`#${prefix}-voice`), voiceOptions(gender, p.voice));
  if (p.likes || p.specialties || p.skills || p.context) form.querySelector(".persona-more")?.setAttribute("open", "");
}

/** Keep the voice list to the chosen gender, and pull notes in from files and links. */
export function bindPersonaForm(form, prefix = "p") {
  const gender = form.querySelector(`#${prefix}-gender`);
  const voice = form.querySelector(`#${prefix}-voice`);
  gender.addEventListener("change", () => {
    const keep = session.voiceGender(voice.value) === gender.value ? voice.value : "auto";
    setHtml(voice, voiceOptions(gender.value, keep));
  });
  voice.addEventListener("change", () => {
    const g = session.voiceGender(voice.value);
    if (g && gender.value !== g) {
      gender.value = g;
      setHtml(voice, voiceOptions(g, voice.value));
    }
  });

  const notes = form.querySelector(`#${prefix}-context`);
  if (!notes) return;
  const meta = form.querySelector("[data-notes-meta]");
  const add = async (fd, busy) => {
    meta.textContent = busy;
    try {
      const res = await api.extract(fd);
      const text = res.sources.map((s) => `--- ${s.name} ---\n${s.text}`).join("\n\n");
      notes.value = [notes.value.trim(), text].filter(Boolean).join("\n\n").slice(0, NOTES_CHARS);
      meta.textContent = `Added ${res.sources.map((s) => s.name).join(", ")} to the notes.`;
      return true;
    } catch (err) {
      meta.textContent = err.message;
      return false;
    }
  };
  const file = form.querySelector("[data-notes-input]");
  form.querySelector("[data-notes-file]").addEventListener("click", () => file.click());
  file.addEventListener("change", () => {
    const f = file.files[0];
    file.value = "";
    if (!f) return;
    const fd = new FormData();
    fd.append("files", f);
    add(fd, `Reading ${f.name}…`);
  });
  const row = form.querySelector("[data-notes-linkrow]");
  form.querySelector("[data-notes-link]").addEventListener("click", () => {
    row.hidden = !row.hidden;
    if (!row.hidden) row.querySelector("input").focus();
  });
  const fetchLink = async () => {
    const input = row.querySelector("input");
    if (!input.value.trim()) return;
    const fd = new FormData();
    fd.append("url", input.value.trim());
    if (await add(fd, "Fetching the page…")) {
      input.value = "";
      row.hidden = true;
    }
  };
  row.querySelector("[data-notes-fetch]").addEventListener("click", fetchLink);
  row.querySelector("input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();  // not the whole form
      fetchLink();
    }
  });
}

/** A persona as a card body: avatar, name, personality, gender and voice, notes badge. */
export function personaCard(p, { size = 44 } = {}) {
  const facts = [p.gender ? GENDER_LABEL[p.gender] : "", p.voice && p.voice !== "auto" ? `${p.voice} voice` : "Auto voice"];
  return html`
    <span class="avatar" style="--av:${callerColor(p.name)};--size:${size}px">${initial(p.name)}</span>
    <span class="col grow"><strong>${p.name}</strong><span class="muted small">${p.persona}</span>
      <span class="faint small">${facts.filter(Boolean).join(" · ")}${p.specialties ? ` · ${p.specialties}` : ""}</span>
      ${p.context ? html`<span class="notes-tag">${icon("lock", 12)}Private notes</span>` : ""}</span>`;
}
