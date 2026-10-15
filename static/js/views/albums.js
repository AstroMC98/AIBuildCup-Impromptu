// /albums (yours) and /album/{id}: playlists of pods. A pod can be in several
// albums; each album is private, for connections, or public, and anyone who can
// see an album sees only the pods in it they're allowed to hear.
import { api } from "../api.js";
import { html, setHtml } from "../html.js";
import { icon, VISIBILITY_ICON, VISIBILITY_LABEL } from "../icons.js";
import { navigate } from "../router.js";
import { loginPath, session } from "../session.js";
import { albumCard, bindSubscribe, personAvatar, subscribeButton } from "../social.js";
import { applyAccent } from "../theme.js";
import { confirmSheet, openSheet } from "../ui.js";
import { toast } from "../util.js";
import { appHeader, bindHeader, row, sleeve } from "./library.js";

const ACCENTS = ["#6A4FB0", "#B8336A", "#3B3F9E", "#B5521C", "#1F7F6A", "#80620F", "#2E6F9E", "#3A4250"];
const VIS = [
  ["private", "Only you. Add it to a pod's sharing to let others in."],
  ["connections", "Your connections can see it and subscribe."],
  ["public", "Anyone can see it and subscribe."],
];

function albumForm(a = {}) {
  const accent = a.accent || ACCENTS[0];
  return html`
    <label class="field-label" for="al-title">Name</label>
    <input id="al-title" class="field" maxlength="80" required value="${a.title || ""}" placeholder="Bible stories, gossip edition">
    <label class="field-label" for="al-desc">About</label>
    <textarea id="al-desc" class="field area short" maxlength="1000" placeholder="What ties these pods together.">${a.description || ""}</textarea>
    <p class="field-label">Who can see it</p>
    <div class="radio-cards" role="radiogroup" aria-label="Who can see it">${VIS.map(([id, desc]) => html`
      <button type="button" role="radio" class="radio-card" data-vis="${id}" aria-checked="${(a.visibility || "private") === id}">
        <span class="radio-dot" aria-hidden="true"></span>
        <span class="radio-text"><span class="radio-title">${icon(VISIBILITY_ICON[id], 16)} ${VISIBILITY_LABEL[id]}</span>
          <span class="radio-desc">${desc}</span></span></button>`)}</div>
    <p class="faint small">Pods keep their own sharing: people only see the pods in here they could hear anyway.</p>
    <p class="field-label">Color</p>
    <div class="swatches" role="radiogroup" aria-label="Color">${ACCENTS.map((c) => html`
      <button type="button" role="radio" class="swatch" data-accent="${c}" aria-checked="${accent === c}" style="--tile:${c}" aria-label="${c}"></button>`)}</div>`;
}

function readAlbum(form) {
  return {
    title: form.querySelector("#al-title").value.trim(),
    description: form.querySelector("#al-desc").value.trim(),
    visibility: form.querySelector("[data-vis][aria-checked=true]")?.dataset.vis || "private",
    accent: form.querySelector("[data-accent][aria-checked=true]")?.dataset.accent || ACCENTS[0],
  };
}

/** New or edit; resolves with the saved album, or null. */
function editAlbum(album = null) {
  return new Promise((resolve) => {
    const form = document.createElement("form");
    form.className = "stack";
    setHtml(form, html`${albumForm(album || {})}<p class="error" role="alert" data-error></p>
      <button class="btn white big block" type="submit">${album ? "Save" : "Create album"}</button>`);
    form.addEventListener("click", (e) => {
      const pick = e.target.closest("[data-vis], [data-accent]");
      if (!pick) return;
      const attr = pick.dataset.vis ? "data-vis" : "data-accent";
      for (const b of form.querySelectorAll(`[${attr}]`)) b.setAttribute("aria-checked", String(b === pick));
    });
    let saved = null;
    const sheet = openSheet({ title: album ? "Edit album" : "New album", body: form, onClose: () => resolve(saved) });
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        saved = album ? await api.updateAlbum(album.id, readAlbum(form)) : await api.createAlbum(readAlbum(form));
        sheet.close();
      } catch (err) {
        form.querySelector("[data-error]").textContent = err.message;
      }
    });
  });
}

// ---- /albums --------------------------------------------------------------------------------

export async function albumsView(root) {
  if (!session.signedIn) {
    navigate(loginPath("/albums"), { replace: true });
    return null;
  }
  applyAccent(document.documentElement, "#6A4FB0");
  const el = document.createElement("div");
  el.className = "page-wrap";
  setHtml(el, html`
    ${appHeader({ search: false })}
    <main class="page wide">
      <div class="page-title row">
        <div class="col grow"><h1>Albums</h1>
          <p class="muted">Group your pods into albums, like playlists or series. Make one public and people can subscribe to it.</p></div>
        <button class="btn white" type="button" data-new>${icon("plus", 16, 2.6)}New album</button>
      </div>
      <div data-list><p class="empty">Loading…</p></div>
    </main>`);
  root.append(el);
  const offHead = bindHeader(el);
  let albums = [];
  const render = () => setHtml(el.querySelector("[data-list]"), albums.length
    ? html`<div class="album-grid">${albums.map((a) => albumCard(a, { mine: true }))}</div>`
    : html`<p class="empty">No albums yet. Make one, then add pods from the album page or when you create a pod.</p>`);
  el.querySelector("[data-new]").addEventListener("click", async () => {
    const a = await editAlbum();
    if (a) navigate(`/album/${a.id}`);
  });
  albums = (await api.albums().catch(() => ({ albums: [] }))).albums;
  render();
  return { unmount() { offHead(); } };
}

// ---- /album/{id} ----------------------------------------------------------------------------

export async function albumView(root, { params: [albumId] }) {
  const el = document.createElement("div");
  el.className = "page-wrap";
  root.append(el);
  let data;
  try {
    data = await api.album(albumId);
  } catch (err) {
    if (err.status === 401 || (err.status === 404 && !session.signedIn)) {
      navigate(loginPath(), { replace: true });
      return null;
    }
    setHtml(el, html`${appHeader({ search: false })}<main class="page"><div class="splash-card"><h1>Album not found</h1>
      <p class="muted">${err.message}</p><a class="btn white" href="/" data-link>Back to your pods</a></div></main>`);
    const off = bindHeader(el);
    return { unmount() { off(); } };
  }

  const render = () => {
    const a = data.album;
    applyAccent(document.documentElement, a.accent || "#6A4FB0");
    setHtml(el, html`
      ${appHeader({ search: false })}
      <main class="page wide">
        <header class="hero" style="--tile:${a.accent || "#6A4FB0"}">
          <span class="album-cover big" aria-hidden="true">${icon("bars", 44, 2.2)}</span>
          <div class="col grow hero-text">
            <span class="eyebrow">${icon(VISIBILITY_ICON[a.visibility] || "lock", 14)} ${VISIBILITY_LABEL[a.visibility]} album</span>
            <h1>${a.title}</h1>
            ${a.description ? html`<p class="muted">${a.description}</p>` : ""}
            <a class="creator-chip" href="/creator/${data.owner.uid}" data-link>${personAvatar(data.owner, 24)}<strong>${data.owner.name}</strong></a>
            <span class="faint small">${a.pods === 1 ? "1 pod" : `${a.pods} pods`}</span>
          </div>
          <div class="row gap wrap hero-actions">
            ${data.mine ? html`
              <span class="faint small">${data.subscribers === 1 ? "1 subscriber" : `${data.subscribers} subscribers`}</span>
              <a class="btn white" href="/new?album=${a.id}" data-link>${icon("plus", 16, 2.6)}New pod here</a>
              <button class="btn chip-btn" type="button" data-add>Add pods</button>
              <button class="btn chip-btn" type="button" data-edit>${icon("edit", 16)}Edit</button>
              <button class="icon-btn" type="button" data-delete aria-label="Delete album">${icon("trash", 20)}</button>`
              : subscribeButton({ kind: "album", id: a.id, subscribed: data.subscribed, subscribers: data.subscribers })}
          </div>
        </header>
        ${data.shows.length ? html`<ul class="lib-list album-pods">${data.shows.map((s) => row(s, false, data.mine ? "mine" : "album"))}</ul>`
          : html`<p class="empty">${data.mine ? "No pods in this album yet. Add some, or start a new pod here." : "No pods here that you can listen to yet."}</p>`}
      </main>`);
  };
  render();
  const offHead = bindHeader(el);
  const offSub = bindSubscribe(el, { onChange: (s) => Object.assign(data, { subscribed: s.subscribed, subscribers: s.subscribers }) });

  el.addEventListener("click", async (e) => {
    if (e.target.closest("[data-edit]")) {
      const saved = await editAlbum(data.album);
      if (saved) {
        data.album = { ...data.album, ...saved, pods: data.album.pods };
        render();
      }
    } else if (e.target.closest("[data-delete]")) {
      const ok = await confirmSheet({
        title: `Delete ${data.album.title}?`, message: "The pods in it stay; they're just not grouped any more.",
        confirm: "Delete album", danger: true,
      });
      if (!ok) return;
      await api.deleteAlbum(albumId).catch((err) => toast(err.message));
      navigate("/albums");
    } else if (e.target.closest("[data-add]")) {
      await pickPods();
      data = await api.album(albumId);
      render();
    }
  });

  async function pickPods() {
    const shows = (await api.listShows("mine")).shows;
    const div = document.createElement("div");
    div.className = "stack";
    const draw = () => setHtml(div, html`
      <p class="muted small">Tap a pod to put it in this album or take it out. A pod can be in more than one album.</p>
      <ul class="pick-list">${shows.map((s) => {
        const inside = (s.album_ids || []).includes(albumId);
        return html`<li><button type="button" class="pick-row" data-pick="${s.id}" aria-pressed="${inside}">
          ${sleeve(s, "sm")}
          <span class="ellipsis grow">${s.title || "Untitled pod"}</span>
          <span class="pick-check" aria-hidden="true">${inside ? icon("check", 16, 3) : icon("plus", 16, 2.6)}</span></button></li>`;
      })}</ul>`);
    draw();
    div.addEventListener("click", async (e) => {
      const b = e.target.closest("[data-pick]");
      if (!b) return;
      const s = shows.find((x) => x.id === b.dataset.pick);
      const ids = new Set(s.album_ids || []);
      if (ids.has(albumId)) ids.delete(albumId); else ids.add(albumId);
      try {
        s.album_ids = (await api.organize(s.id, { album_ids: [...ids] })).album_ids;
        draw();
      } catch (err) {
        toast(err.message);
      }
    });
    await new Promise((resolve) => openSheet({ title: "Pods in this album", body: div, onClose: resolve }));
  }

  return { unmount() { offHead(); offSub(); } };
}
