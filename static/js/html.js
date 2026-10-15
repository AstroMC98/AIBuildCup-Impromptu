// Escape-by-default HTML templating. Guests can type names and call-ins that
// every other listener sees, so nothing they send may ever become markup.
//
//   html`<p>${userText}</p>`   escapes userText
//   html`${raw(svg)}`           trusted markup only (icons we ship)
//   html`${[a, b]}`             arrays are joined; nested html`` stays as markup

const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

export const escapeHtml = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);

class Markup {
  constructor(s) { this.s = s; }
  toString() { return this.s; }
}

export const raw = (s) => new Markup(String(s));

function render(v) {
  if (v === null || v === undefined || v === false) return "";
  if (v instanceof Markup) return v.s;
  if (Array.isArray(v)) return v.map(render).join("");
  return escapeHtml(v);
}

export function html(strings, ...values) {
  let out = strings[0];
  for (let i = 0; i < values.length; i++) out += render(values[i]) + strings[i + 1];
  return new Markup(out);
}

export function setHtml(el, markup) {
  el.innerHTML = String(markup);
}

// The text the TTS engine actually says: no vocal tags, no |asides|.
export function spokenText(t) {
  return t.replace(/<[^>]+>/g, "").replace(/\|[^|]*\|/g, "").replace(/\s+/g, " ").trim();
}

// Captions: vocal tags become quiet cues, |asides| become the other host's interjection.
export function renderSpoken(text, otherHost) {
  return raw(
    escapeHtml(text)
      .replace(/&lt;([a-z][a-z -]*)&gt;/gi, (_, tag) => `<span class="cue">(${tag})</span>`)
      .replace(/\|([^|]+)\|/g, (_, s) => ` <span class="aside h${otherHost}">${s.trim()}</span> `),
  );
}
