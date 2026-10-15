// Following people and albums: the Subscribe button, album cards, and the
// creator chip shown on pods.
import { api } from "./api.js";
import { html } from "./html.js";
import { icon, VISIBILITY_ICON, VISIBILITY_LABEL } from "./icons.js";
import { navigate } from "./router.js";
import { loginPath, session } from "./session.js";
import { callerColor, initial } from "./theme.js";
import { toast } from "./util.js";

function count(n) {
  return n === 1 ? "1 subscriber" : `${n || 0} subscribers`;
}

export function subscribeButton({ kind, id, subscribed = false, subscribers = 0, small = false }) {
  return html`
    <button type="button" class="btn ${subscribed ? "chip-btn" : "white"} subscribe-btn ${small ? "small-btn" : ""}"
      data-subscribe="${kind}" data-target="${id}" aria-pressed="${subscribed}"
      aria-label="${subscribed ? "Unsubscribe" : "Subscribe"}, ${count(subscribers)}">
      ${subscribed ? icon("check", 16, 2.6) : icon("plus", 16, 2.6)}<span>${subscribed ? "Subscribed" : "Subscribe"}</span>
      <span class="sub-count" aria-hidden="true">${subscribers || 0}</span>
    </button>`;
}

/** Make every [data-subscribe] button under root work. onChange({kind, id, subscribed, subscribers}). */
export function bindSubscribe(root, { onChange } = {}) {
  const onClick = async (e) => {
    const b = e.target.closest("[data-subscribe]");
    if (!b || !root.contains(b)) return;
    e.preventDefault();
    if (!session.signedIn) return navigate(loginPath());
    const kind = b.dataset.subscribe;
    const id = b.dataset.target;
    const on = b.getAttribute("aria-pressed") !== "true";
    b.disabled = true;
    try {
      const r = on ? await api.subscribe(kind, id) : await api.unsubscribe(kind, id);
      const state = { kind, id, subscribed: r.subscribed, subscribers: r.subscribers };
      b.outerHTML = String(subscribeButton({ ...state, small: b.classList.contains("small-btn") }));
      onChange?.(state);
      toast(on ? "Subscribed. Their new pods show up under Following." : "Unsubscribed.");
    } catch (err) {
      b.disabled = false;
      toast(err.message);
    }
  };
  root.addEventListener("click", onClick);
  return () => root.removeEventListener("click", onClick);
}

export function personAvatar(user, size = 36) {
  return user.photo
    ? html`<img class="avatar-img" src="${user.photo}" alt="" referrerpolicy="no-referrer" style="--size:${size}px">`
    : html`<span class="avatar" style="--av:${callerColor(user.name)};--size:${size}px">${initial(user.name)}</span>`;
}

/** "by <creator>" with their Subscribe button (yours: no button). */
export function creatorChip(creator) {
  if (!creator) return "";
  return html`
    <span class="creator-row">
      <a class="creator-chip" href="/creator/${creator.uid}" data-link>${personAvatar(creator, 28)}<span class="col">
        <span class="faint small">${creator.you ? "Your pod" : "Created by"}</span><strong>${creator.name}</strong></span></a>
      ${creator.you ? "" : subscribeButton({ kind: "creator", id: creator.uid, subscribed: creator.subscribed,
        subscribers: creator.subscribers, small: true })}
    </span>`;
}

export function albumCard(a, { mine = false } = {}) {
  return html`
    <a class="album-card" href="/album/${a.id}" data-link style="--tile:${a.accent || "#6A4FB0"}">
      <span class="album-cover" aria-hidden="true">${icon("bars", 26, 2.4)}</span>
      <span class="album-text"><strong>${a.title}</strong>
        <span class="faint small">${a.pods === 1 ? "1 pod" : `${a.pods ?? 0} pods`}${mine ? "" : a.owner_name ? ` · ${a.owner_name}` : ""}</span></span>
      ${mine ? html`<span class="vis-ic" title="${VISIBILITY_LABEL[a.visibility]}">${icon(VISIBILITY_ICON[a.visibility] || "lock", 16)}</span>` : ""}
    </a>`;
}

/** Album chips on a pod ("In: Bible gossip"). */
export function albumChips(albums) {
  if (!albums?.length) return "";
  return html`<span class="album-chips">${icon("bars", 14, 2.4)}<span class="faint small">In</span>${albums.map((a) => html`
    <a class="chip album-chip" href="/album/${a.id}" data-link style="--tile:${a.accent || "#6A4FB0"}">${a.title}</a>`)}</span>`;
}

/** Under a pod's title: its creator (Subscribe), the albums it's in, and Stats for the creator. */
export function creatorBar(store, showId) {
  return html`
    ${creatorChip(store.creator)}
    ${albumChips(store.albums)}
    ${store.producer ? html`<a class="btn chip-btn small-btn" href="/show/${showId}/stats" data-link>${icon("people", 14)}Stats</a>` : ""}`;
}

export function creatorBarKey(store) {
  const c = store.creator || {};
  return `${c.subscribed}|${c.subscribers}|${(store.albums || []).map((a) => a.id).join(",")}|${store.producer}`;
}
