// Choices every new pod has, whichever way it's made (the Create screen, a voice
// memo): who can listen, which albums it goes in, its theme music, and whether it
// opens with your show intro. Markup, plus the New album sheet; each view handles
// the clicks ([data-vis], [data-share], [data-album], [data-new-album], [data-music],
// [data-use-intro]).
import { api } from "./api.js";
import { html, setHtml } from "./html.js";
import { icon } from "./icons.js";
import { session } from "./session.js";
import { openSheet } from "./ui.js";

export const VISIBILITY = [
  ["private", "Private", "lock", "Only you, plus connections you pick."],
  ["connections", "Connections", "people", "All your connections can listen and call in."],
  ["public", "Public", "globe", "Anyone can listen, free. Signed-in listeners can call in."],
];

function radioCard(attr, value, checked, title, desc) {
  return html`
    <button type="button" role="radio" class="radio-card" ${attr}="${value}" aria-checked="${checked}">
      <span class="radio-dot" aria-hidden="true"></span>
      <span class="radio-text"><span class="radio-title">${title}</span><span class="radio-desc">${desc}</span></span>
    </button>`;
}

export function visibilityCards(visibility) {
  return html`${VISIBILITY.map(([id, title, ic, desc]) =>
    radioCard("data-vis", id, visibility === id, html`${icon(ic, 16)} ${title}`, desc))}`;
}

/** For a private pod: which of your connections can hear it too. */
export function shareWith(visibility, connections, sharedWith) {
  if (visibility !== "private") return "";
  return connections.length ? html`
    <p class="field-label share-label">Also share with</p>
    <div class="chips">${connections.map((c) => html`
      <button type="button" class="chip" data-share="${c.uid}" aria-pressed="${sharedWith.includes(c.uid)}">${c.name}</button>`)}</div>`
    : html`<p class="faint small share-label">Want friends to hear it? <a href="/connections" data-link>Add connections</a>.</p>`;
}

export function albumChips(albums, chosen) {
  return html`${albums.map((a) => html`
    <button type="button" class="chip" data-album="${a.id}" aria-pressed="${chosen.includes(a.id)}">${a.title}</button>`)}
    <button type="button" class="chip dashed" data-new-album>${icon("plus", 14, 2.6)}New album</button>`;
}

/** Ask for a name and make a (private) album; onCreated(album) once it exists. */
export function newAlbumSheet(onCreated) {
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
      onCreated(await api.createAlbum({ title: form.querySelector("#album-title").value.trim() }));
      sheet.close();
    } catch (err) {
      form.querySelector("[data-error]").textContent = err.message;
    }
  });
}

/** Theme music. "auto" is your show intro's theme if it has one, else a library pick.
 *  generate: offer original music (memo pods only). mine: music made for you before. */
export function musicCards(choice, { mine = [], generate = false, intro = null } = {}) {
  const music = session.options.music || { library: [], generate: false, credits: 0 };
  const showTheme = intro?.enabled !== false && intro?.music && !["auto", "none"].includes(intro.music);
  const cards = [radioCard("data-music", "auto", choice === "auto",
    showTheme ? "Your show's theme" : "Studio theme",
    showTheme ? "The theme from your show intro."
      : music.library.length ? "Included: a theme from the studio library that suits it."
        : "No studio music is installed yet, so it opens with the hosts' welcome alone.")];
  if (generate && music.generate) {
    cards.push(radioCard("data-music", "generate", choice === "generate", `Original music · +${music.credits} credits`,
      "Made just for this episode, and yours to reuse. It takes a few extra minutes to make."));
  }
  for (const m of mine.slice(0, 3)) {
    cards.push(radioCard("data-music", m.id, choice === m.id, `Your theme: ${m.label}`, "Made for you before. Free to reuse."));
  }
  cards.push(radioCard("data-music", "none", choice === "none", "No music", "Just the hosts."));
  return html`${cards}`;
}

/** Whether the pod opens with your saved show intro, or where to make one.
 *  cohost: worded for a co-hosted call (the co-host welcomes; the theme is added on saving). */
export function introRow(intro, use, { cohost = false } = {}) {
  if (!intro || intro.enabled === false) {
    return cohost
      ? html`<p class="faint small intro-hint">The episode opens with theme music and your co-host's welcome.
        <a href="/intro" data-link>Name your show</a> and they'll welcome listeners to it by name.</p>`
      : html`<p class="faint small intro-hint">Every pod opens with a few seconds of theme music and the hosts' welcome.
        <a href="/intro" data-link>Name your show</a> and they'll welcome listeners to it by name.</p>`;
  }
  return html`
    <div class="switch-row">
      <span id="use-intro-label">Open with your show intro, <strong>${intro.show_name}</strong>
        <a class="faint small" href="/intro" data-link>Edit</a></span>
      <button type="button" role="switch" class="switch" aria-checked="${use}" aria-labelledby="use-intro-label" data-use-intro></button>
    </div>`;
}
