// /creator/{uid}: a creator's page. Their albums and pods you're allowed to see,
// how many people subscribe, and the Subscribe button.
import { api } from "../api.js";
import { waiting } from "../components.js";
import { html, setHtml } from "../html.js";
import { session } from "../session.js";
import { albumCard, bindSubscribe, personAvatar, subscribeButton } from "../social.js";
import { applyAccent } from "../theme.js";
import { appHeader, bindHeader, row } from "./library.js";

export async function creatorView(root, { params: [uid] }) {
  applyAccent(document.documentElement, "#B8336A");
  const el = document.createElement("div");
  el.className = "page-wrap";
  setHtml(el, html`${appHeader({ search: false })}<main class="page">${waiting()}</main>`);
  root.append(el);
  let data;
  try {
    data = await api.creator(uid);
  } catch (err) {
    setHtml(el, html`${appHeader({ search: false })}<main class="page"><div class="splash-card"><h1>Creator not found</h1>
      <p class="muted">${err.message}</p><a class="btn white" href="/discover" data-link>Discover pods</a></div></main>`);
    const off = bindHeader(el);
    return { unmount() { off(); } };
  }
  const u = data.user;
  setHtml(el, html`
    ${appHeader({ search: false })}
    <main class="page wide">
      <header class="hero creator-hero">
        ${personAvatar(u, 88)}
        <div class="col grow hero-text">
          <span class="eyebrow">Creator</span>
          <h1>${u.name}</h1>
          <span class="faint small"><span data-subcount>${data.subscribers === 1 ? "1 subscriber" : `${data.subscribers} subscribers`}</span>
            · ${data.shows.length === 1 ? "1 pod" : `${data.shows.length} pods`}</span>
        </div>
        <div class="row gap hero-actions">
          ${data.you ? html`<a class="btn chip-btn" href="/stats" data-link>Your stats</a>`
            : subscribeButton({ kind: "creator", id: u.uid, subscribed: data.subscribed, subscribers: data.subscribers })}
        </div>
      </header>
      ${data.albums.length ? html`
        <h2 class="section-h">Albums</h2>
        <div class="album-grid">${data.albums.map((a) => albumCard(a))}</div>` : ""}
      <h2 class="section-h">Pods</h2>
      ${data.shows.length ? html`<ul class="lib-list">${data.shows.map((s) => row(s, false, "creator"))}</ul>`
        : html`<p class="empty">${data.you ? "Pods you share show up here." : session.signedIn
          ? "No pods here that you can listen to yet. Subscribe and new ones show up under Following."
          : "No public pods yet."}</p>`}
    </main>`);
  const offHead = bindHeader(el);
  const offSub = bindSubscribe(el, {
    onChange: (s) => {
      el.querySelector("[data-subcount]").textContent = s.subscribers === 1 ? "1 subscriber" : `${s.subscribers} subscribers`;
    },
  });
  return { unmount() { offHead(); offSub(); } };
}
