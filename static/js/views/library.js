// The app header, the account menu, the pod lists (yours, the creators and albums
// you follow, shared with you, Discover), and the home screen.
import { api } from "../api.js";
import { html, setHtml } from "../html.js";
import { icon, VISIBILITY_ICON, VISIBILITY_LABEL } from "../icons.js";
import { navigate } from "../router.js";
import { loginPath, session } from "../session.js";
import { applyAccent, callerColor, initial } from "../theme.js";
import { bindActions, openSheet } from "../ui.js";
import { fmtAgo, fmtTime, store as local } from "../util.js";

const FILTERS = [["all", "All"], ["live", "Live"], ["finished", "Finished"], ["draft", "Drafts"]];
const SCOPES = [["mine", "Yours"], ["following", "Following"], ["shared", "Shared"], ["public", "Discover"]];
const REFRESH_MS = 15000;

// ---- header and account menu ------------------------------------------------------

export function appHeader({ search = true } = {}) {
  return html`
    <header class="app-head">
      <a class="brand" href="/" data-link aria-label="Gemini Podcasts"><span class="brand-mark" aria-hidden="true">${icon("logo", 18, 2.4)}</span><span class="brand-text">Gemini Podcasts</span></a>
      ${session.signedIn && search ? html`
        <form class="head-search" role="search" data-search>
          <label class="sr-only" for="head-search">Search pods</label>
          <span class="head-search-ic" aria-hidden="true">${icon("search", 18)}</span>
          <input id="head-search" type="search" placeholder="Search pods" autocomplete="off">
        </form>` : html`<span class="grow"></span>`}
      <div class="row gap head-actions" data-head-actions>${headActions()}</div>
    </header>`;
}

function headActions() {
  if (!session.signedIn) {
    return html`
      <a class="btn quiet" href="/discover" data-link>Discover</a>
      <a class="btn white" href="${loginPath()}" data-link>Sign in</a>`;
  }
  const me = session.me;
  return html`
    <button class="credits-chip" type="button" data-action="account" aria-label="${me.credits} credits">
      ${icon("coin", 16)}<span>${me.credits}</span><span class="wide-only">credits</span></button>
    <a class="btn white new-pod" href="/new" data-link aria-label="New pod">${icon("plus", 16, 2.6)}<span class="wide-only">New pod</span></a>
    <button class="account" type="button" data-action="account" aria-label="Your account${session.incoming ? `, ${session.incoming} connection requests` : ""}">
      ${me.photo ? html`<img src="${me.photo}" alt="" referrerpolicy="no-referrer">` : initial(me.name)}
      ${session.incoming ? html`<span class="dot-badge">${session.incoming}</span>` : ""}
    </button>`;
}

export function bindHeader(root, { onSearch } = {}) {
  const input = root.querySelector("[data-search] input");
  if (input) {
    root.querySelector("[data-search]").addEventListener("submit", (e) => e.preventDefault());
    input.addEventListener("input", () => onSearch?.(input.value));
  }
  const off = session.on(() => {
    const slot = root.querySelector("[data-head-actions]");
    if (slot) setHtml(slot, headActions());
  });
  const offActions = bindActions(root, { account: () => openAccountSheet() });
  return () => { off(); offActions(); };
}

export async function openAccountSheet() {
  const me = session.me;
  if (!me) return navigate(loginPath());
  const div = document.createElement("div");
  div.className = "account-sheet";
  const render = (entries = null) => setHtml(div, html`
    <div class="row gap acct-who">
      <span class="avatar" style="--av:${callerColor(me.name)};--size:48px">${initial(me.name)}</span>
      <span class="col"><strong>${me.name}</strong><span class="muted small">${me.email}</span></span>
      ${me.admin ? html`<span class="tag">Admin</span>` : ""}
    </div>
    <div class="credit-card">
      <span class="credit-num">${me.credits}</span>
      <span class="col"><strong>credits</strong>
        <span class="muted small">${me.credits ? `About ${me.credits} minutes of pods.` : "Free account: listen, call in and connect for free."}
          Creating a pod uses ${session.options.credits_per_minute} credit per minute.</span></span>
    </div>
    ${entries === null ? html`<p class="faint small">Loading credit history…</p>` : entries.length ? html`
      <ul class="ledger">${entries.slice(0, 6).map((e) => html`
        <li><span>${ledgerLabel(e)}</span><span class="${e.amount > 0 ? "plus" : "minus"}">${e.amount > 0 ? "+" : ""}${e.amount}</span></li>`)}
      </ul>` : ""}
    <nav class="menu">
      <a class="menu-item" href="/" data-link>${icon("mic", 20)}Your pods</a>
      <a class="menu-item" href="/following" data-link>${icon("heart", 20)}Following</a>
      <a class="menu-item" href="/albums" data-link>${icon("bars", 20)}Albums</a>
      <a class="menu-item" href="/stats" data-link>${icon("people", 20)}Stats</a>
      <a class="menu-item" href="/creator/${me.uid}" data-link>${icon("user", 20)}Your creator page</a>
      <a class="menu-item" href="/discover" data-link>${icon("globe", 20)}Discover</a>
      <a class="menu-item" href="/connections" data-link>${icon("people", 20)}Connections
        ${session.incoming ? html`<span class="pill pink">${session.incoming} new</span>` : ""}</a>
      <a class="menu-item" href="/personas" data-link>${icon("user", 20)}Personas</a>
      <a class="menu-item" href="/styles" data-link>${icon("restyle", 20)}Styles</a>
      <a class="menu-item" href="/intro" data-link>${icon("sparkle", 20)}Show intro</a>
      <a class="menu-item" href="/memo" data-link>${icon("mic", 20)}Pod from a voice memo</a>
      ${me.admin ? html`<a class="menu-item" href="/admin" data-link>${icon("shield", 20)}Admin</a>` : ""}
      <button class="menu-item" type="button" data-signout>${icon("signOut", 20)}Sign out</button>
    </nav>`);
  render();
  const sheet = openSheet({ title: "Your account", body: div });
  div.addEventListener("click", async (e) => {
    if (e.target.closest("a[data-link]")) sheet.close();
    if (e.target.closest("[data-signout]")) {
      sheet.close();
      await session.signOut();
      navigate("/", { replace: true });
    }
  });
  api.ledger().then((r) => render(r.entries)).catch(() => render([]));
}

function ledgerLabel(e) {
  const when = fmtAgo(e.at);
  switch (e.reason) {
    case "pod": return `Pod made · ${when}`;
    case "music": return `Original music · ${when}`;
    case "refund": return `Refund: ${e.note || "Unused minutes"} · ${when}`;
    case "signup": return `Welcome credits · ${when}`;
    case "grant": return `${e.note || "Credits added"} · ${when}`;
    default: return `${e.note || "Adjustment"} · ${when}`;
  }
}

// ---- pod lists -------------------------------------------------------------------------

/** Yours / Shared / Discover lists, filtered and searchable, refreshed while open. */
export function mountLibraryList(container, { activeId = null, compact = false, scope: forced = null } = {}) {
  const lists = {};
  let scope = forced || (session.signedIn ? local.get("gp_scope", "mine") : "public");
  if (!session.signedIn) scope = "public";
  let filter = local.get("gp_filter", "all");
  let tag = "";
  let query = "";
  let timer = 0;
  let unseen = 0;
  let markedSeen = false;
  let countedAt = 0;

  const render = () => {
    const shows = lists[scope];
    const tabs = session.signedIn && !forced;
    const q = query.trim().toLowerCase();
    const tags = scope === "mine" ? [...new Set((shows || []).flatMap((s) => s.tags || []))].sort() : [];
    if (tag && !tags.includes(tag)) tag = "";
    const list = (shows || []).filter((s) =>
      (scope !== "mine" || filter === "all" || s.status === filter
        || (filter === "finished" && ["interrupted", "failed"].includes(s.status)))
      && (!tag || (s.tags || []).includes(tag))
      && (!q || `${s.title} ${s.style_label} ${s.hosts.join(" ")} ${s.owner_name} ${(s.tags || []).join(" ")} ${s.via || ""}`
        .toLowerCase().includes(q)));
    const liveNow = scope === "following" ? (shows || []).filter((s) => s.status === "live") : [];
    setHtml(container, html`
      <div class="lib-head">
        <h2>${{ public: "Discover", shared: "Shared with you", following: "Following" }[scope] || "Your pods"}</h2>
        <span class="row gap-s">
          ${scope === "mine" ? html`<a class="btn chip-btn small-btn" href="/albums" data-link>${icon("bars", 14, 2.4)}Albums</a>` : ""}
          ${session.signedIn ? html`<a class="round-btn small" href="/new" data-link aria-label="New pod">${icon("plus", 16, 2.4)}</a>` : ""}
        </span>
      </div>
      ${tabs ? html`<div class="seg-tabs" role="tablist" aria-label="Which pods">
        ${SCOPES.map(([id, label]) => html`<button type="button" role="tab" class="seg-tab" data-scope="${id}" aria-selected="${scope === id}">${label}${
          id === "following" && unseen ? html`<span class="tab-badge" aria-label="${unseen} new">${unseen}</span>` : ""}</button>`)}
      </div>` : ""}
      ${scope === "mine" ? html`<div class="chips" role="group" aria-label="Filter pods">
        ${FILTERS.map(([id, label]) => html`<button type="button" class="chip" data-filter="${id}" aria-pressed="${filter === id}">${label}</button>`)}
      </div>
      ${tags.length ? html`<div class="chips tag-chips" role="group" aria-label="Tags">
        ${tags.map((t) => html`<button type="button" class="chip tag" data-tag="${t}" aria-pressed="${tag === t}">#${t}</button>`)}
      </div>` : ""}` : ""}
      ${liveNow.length ? html`<a class="live-banner" href="/show/${liveNow[0].id}" data-link>
        <span class="badge live small"><span class="dot"></span>Live now</span>
        <span class="ellipsis"><strong>${liveNow[0].title || "A pod"}</strong> from ${liveNow[0].via}</span></a>` : ""}
      ${!shows ? html`<p class="empty">Loading…</p>`
        : list.length ? html`<ul class="lib-list ${compact ? "compact" : ""}">${list.map((s) => row(s, s.id === activeId, scope))}</ul>`
          : html`<p class="empty">${emptyText(scope, shows.length)}</p>`}`);
  };

  const load = async () => {
    try {
      if (scope === "following") {
        const feed = await api.feed();
        lists.following = feed.shows;
        unseen = feed.unseen;
        render();
        if (!markedSeen && feed.unseen) {  // opening the tab counts as seeing them; badges stay for this visit
          markedSeen = true;
          unseen = 0;
          api.feedSeen().catch(() => {});
        }
        return;
      }
      lists[scope] = (await api.listShows(scope)).shows;
      if (session.signedIn && !forced && !markedSeen && Date.now() - countedAt > 60000) {
        countedAt = Date.now();  // the Following badge: at most once a minute
        api.feed().then((f) => { unseen = f.unseen; render(); }).catch(() => {});
      }
      render();
    } catch (err) {
      if (err.status === 401) { scope = "public"; return load(); }
      if (!lists[scope]) setHtml(container, html`<p class="empty">${err.message}</p>`);
    }
  };

  container.addEventListener("click", (e) => {
    const f = e.target.closest("[data-filter]");
    const s = e.target.closest("[data-scope]");
    const t = e.target.closest("[data-tag]");
    if (t) {
      tag = tag === t.dataset.tag ? "" : t.dataset.tag;
      render();
    } else if (f) {
      filter = f.dataset.filter;
      local.set("gp_filter", filter);
      render();
    } else if (s) {
      scope = s.dataset.scope;
      local.set("gp_scope", scope);
      render();
      load();
    }
  });

  render();
  load();
  timer = setInterval(load, REFRESH_MS);
  return {
    search(q) { query = q; render(); },
    refresh: load,
    destroy() { clearInterval(timer); },
  };
}

function emptyText(scope, total) {
  if (total) return "Nothing matches.";
  if (scope === "shared") return "Nothing shared with you yet. Pods your connections share show up here.";
  if (scope === "public") return "No public pods yet. Be the first to publish one.";
  if (scope === "following") return "Nothing here yet. Subscribe to a creator or an album, and their pods show up here.";
  return "No pods yet. Start one: it takes a few seconds.";
}

export function row(s, active, scope) {
  const live = s.status === "live";
  const by = scope === "following" && s.via ? `${s.via} · `
    : scope !== "mine" && s.owner_name ? `by ${s.owner_name} · ` : "";
  const memo = s.kind === "memo";
  const meta = live ? `${by}Live now`
    : s.status === "draft" ? `Draft, ${s.style_label}`
      : s.status === "rendering" ? `From a voice memo · making it, ${Math.round((s.render?.progress || 0) * 100)}%`
      : s.status === "review" ? "From a voice memo · ready for you to review"
      : s.status === "failed" ? (memo ? "From a voice memo · didn't get made" : "Didn't finish")
        : s.status === "interrupted" ? `${by}Interrupted${s.duration_s ? `, ${fmtTime(s.duration_s)}` : ""}`
          : `${by}${s.style_label}${s.duration_s ? `, ${fmtTime(s.duration_s)}` : ""} · ${fmtAgo(s.created_at)}`;
  return html`
    <li>
      <a class="lib-row ${active ? "active" : ""}" href="/show/${s.id}" data-link>
        <span class="lib-cover" style="--tile:${s.accent || "#30284F"}" aria-hidden="true">${initial(s.title)}</span>
        <span class="lib-text"><span class="lib-title">${s.title || "Untitled pod"}</span>
          <span class="lib-meta ${live || s.status === "review" ? "live" : ""}">${meta}</span></span>
        ${s.new ? html`<span class="pill pink new-pill">New</span>` : ""}
        ${scope === "mine" && !["draft", "rendering", "review"].includes(s.status) ? html`
          <span class="vis-ic" title="${VISIBILITY_LABEL[s.visibility]}" aria-label="${VISIBILITY_LABEL[s.visibility]}">${icon(VISIBILITY_ICON[s.visibility] || "lock", 16)}</span>` : ""}
      </a>
    </li>`;
}

// ---- templates -----------------------------------------------------------------------------

export function templateCard(t, { selected = false, as = "button" } = {}) {
  const inner = html`
    <span class="tpl-top"><span class="tpl-label">${t.label}</span>
      <span class="tpl-meta">${t.hosts.length === 1 ? "1 host" : "2 hosts"} · ${t.minutes} min</span></span>
    <span class="tpl-blurb">${t.blurb}</span>
    <span class="tpl-hosts">${t.hosts.map((h) => html`<span class="tpl-host">${h.name}</span>`)}</span>
    ${selected ? html`<span class="vibe-check" aria-hidden="true">${icon("check", 14, 3)}</span>` : ""}`;
  return as === "link"
    ? html`<a class="tpl" href="/new?template=${t.id}" data-link style="--tile:${t.accent}">${inner}</a>`
    : html`<button type="button" class="tpl" data-template="${t.id}" aria-pressed="${selected}" style="--tile:${t.accent}">${inner}</button>`;
}

// ---- home -------------------------------------------------------------------------------------

/** "/" (and "/discover", which opens on public pods). */
export async function libraryView(root, { url }) {
  const discover = url.pathname === "/discover";
  if (url.pathname === "/following") {
    if (!session.signedIn) {
      navigate(loginPath("/following"), { replace: true });
      return null;
    }
    local.set("gp_scope", "following");
  }
  applyAccent(document.documentElement, "#B8336A");
  const el = document.createElement("div");
  el.className = "home";
  const templates = session.options.templates || [];
  const me = session.me;
  setHtml(el, html`
    ${appHeader()}
    <div class="home-body">
      <aside class="lib-pane" data-lib></aside>
      <main class="home-stage">
        <div class="welcome">
          <span class="welcome-mark" aria-hidden="true">${icon("mic", 40, 1.8)}</span>
          <h1>${me ? `Hi ${me.name.split(" ")[0]}. What should today's pod be about?` : "Turn anything into a live pod you can call in to."}</h1>
          <p class="muted">Paste notes, drop a PDF, share a link, or just type a topic. Pick a vibe or a template.
            The hosts start talking in seconds, and you (and your connections) can call in.</p>
          ${!me ? html`
            <div class="row gap wrap">
              <a class="btn white big" href="${loginPath("/new")}" data-link>Get started</a>
              <a class="btn quiet big" href="/discover" data-link>Listen to public pods</a>
            </div>`
            : me.credits > 0 ? html`
              <div class="row gap wrap">
                <a class="btn white big go" href="/new" data-link><span class="rec-dot"></span>Start a new pod</a>
                <a class="btn quiet big" href="/memo" data-link>${icon("mic", 20)}From a voice memo</a>
              </div>`
              : html`<div class="free-note">${icon("coin", 18)}<span><strong>Free account.</strong> Listen, call in, connect, and build personas and styles for free.
                  Creating pods uses credits (${session.options.credits_per_minute} per minute); ask an admin for demo credits.</span></div>`}
          ${me && !session.options.api_key_set ? html`<p class="error">GEMINI_API_KEY isn't set on the server yet, so pods can't start.</p>` : ""}
        </div>
        ${templates.length ? html`
          <section class="tpl-section" aria-labelledby="tpl-h">
            <h2 id="tpl-h" class="section-h">Start from a template</h2>
            <div class="tpl-grid">${templates.map((t) => templateCard(t, { as: "link" }))}</div>
          </section>` : ""}
      </main>
    </div>`);
  root.append(el);
  const lib = mountLibraryList(el.querySelector("[data-lib]"), { scope: discover ? "public" : null });
  const offHead = bindHeader(el, { onSearch: (q) => lib.search(q) });
  if (me) session.refresh();
  return {
    unmount() {
      lib.destroy();
      offHead();
    },
  };
}
