// The app shell (header, the tab bar on phones and the rail on desktop), sleeves, the
// pod lists, and the house screens: Home, Discover, Library and You.
import { api } from "../api.js";
import { html, setHtml } from "../html.js";
import { icon, VISIBILITY_ICON, VISIBILITY_LABEL, wordmark } from "../icons.js";
import { flipById, slideIndicator, stagger } from "../motion.js";
import { ring } from "../ring.js";
import { navigate } from "../router.js";
import { loginPath, session } from "../session.js";
import { applyAccent, gelFor, initial } from "../theme.js";
import { bindActions } from "../ui.js";
import { fmtAgo, fmtTime, store as local } from "../util.js";

const FILTERS = [["all", "All"], ["live", "Live"], ["finished", "Finished"], ["draft", "Drafts"]];
const SCOPE_LABEL = { mine: "Yours", following: "Following", shared: "Shared", public: "Public" };
const REFRESH_MS = 15000;
const HOUSE_ACCENT = "#B8336A";

// ---- the shell ---------------------------------------------------------------------------

const NAV = [["/", "home", "Home"], ["/discover", "compass", "Discover"], ["/library", "grid", "Library"], ["/you", "user", "You"]];

/** Which tab a path belongs to. */
function section(path) {
  if (path === "/") return "/";
  if (/^\/(discover|following|creator\/)/.test(path)) return "/discover";
  if (/^\/(library|albums|album\/|stats)/.test(path) || /^\/show\/[\w-]+\/stats$/.test(path)) return "/library";
  if (/^\/(show|cohost)\//.test(path)) return "";   // a pod belongs to no tab: it could come from anywhere
  return "/you";
}

/** The header, plus the navigation (tab bar on phones, rail on desktop) for signed-in pages.
 *  nav: false for focused tasks (making a pod), which get a back link instead. */
export function appHeader({ search = true, nav = true } = {}) {
  return html`
    <header class="app-head">
      <a class="brand" href="/" data-link aria-label="Impromptu home">${wordmark()}</a>
      ${session.signedIn && search ? html`
        <form class="head-search" role="search" data-search>
          <label class="sr-only" for="head-search">Search pods</label>
          <span class="head-search-ic" aria-hidden="true">${icon("search", 18)}</span>
          <input id="head-search" type="search" placeholder="Search pods" autocomplete="off">
        </form>` : html`<span class="grow"></span>`}
      <div class="head-actions" data-head-actions>${headActions()}</div>
    </header>
    ${nav && session.signedIn ? appNav() : ""}`;
}

function appNav() {
  const here = section(location.pathname);
  return html`
    <nav class="appnav" aria-label="Main">
      <a class="appnav-mark" href="/" data-link aria-label="Impromptu home">${wordmark()}</a>
      ${NAV.map(([href, ic, label]) => html`
        <a class="appnav-item" href="${href}" data-link ${here === href ? html`aria-current="page"` : ""}>
          <span class="appnav-ic">${icon(ic, 22)}${href === "/you" && session.incoming ? html`<span class="appnav-dot" aria-label="${session.incoming} new"></span>` : ""}</span>
          <span>${label}</span></a>`)}
      <a class="btn white appnav-new" href="/new" data-link><span class="rec-dot"></span>New pod</a>
      <a class="appnav-me" href="/you" data-link>${meAvatar(34)}<span class="col"><strong class="ellipsis">${session.me.name}</strong>
        <span class="faint small">${session.me.credits} credits</span></span></a>
    </nav>`;
}

function meAvatar(size) {
  const me = session.me;
  return me.photo
    ? html`<img class="avatar-img" src="${me.photo}" alt="" referrerpolicy="no-referrer" style="--size:${size}px">`
    : html`<span class="avatar" style="--av:#141720;--size:${size}px">${initial(me.name)}</span>`;
}

function headActions() {
  if (!session.signedIn) {
    return html`
      <a class="btn quiet" href="/discover" data-link>Discover</a>
      <a class="btn white" href="${loginPath()}" data-link>Sign in</a>`;
  }
  const me = session.me;
  return html`
    <a class="credits-chip" href="/you" data-link aria-label="${me.credits} credits"><span class="coin" aria-hidden="true"></span><span>${me.credits}</span></a>
    <a class="account" href="/you" data-link aria-label="You${session.incoming ? `, ${session.incoming} connection requests` : ""}">
      ${meAvatar(36)}${session.incoming ? html`<span class="dot-badge">${session.incoming}</span>` : ""}</a>`;
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
  const offActions = bindActions(root, {});
  return () => { off(); offActions(); };
}

// ---- sleeves: a pod's cover, lit by its gel ------------------------------------------------

/** size: "sm" (list rows), "md" (shelves), "lg" (grids and heroes). */
export function sleeve(s, size = "md") {
  const gel = gelFor(s.accent);
  const live = s.status === "live";
  const making = s.status === "rendering";
  const call = s.kind === "cohost";
  const title = s.title || "Untitled pod";
  const short = size === "sm" ? (call ? "" : [...title.trim()][0]?.toUpperCase() || "") : title;   // a tile this small holds one letter
  return html`
    <span class="sleeve ${size}" style="--gel:${gel}" aria-hidden="true">
      ${size !== "sm" ? ring({ cls: "sleeve-ring", lit: (i) => i % 3 === 0, r: 44, dot: 2 }) : ""}
      ${live && size !== "sm" ? html`<span class="sleeve-live">${call ? "On a call" : "Live"}</span>` : ""}
      ${s.status === "review" ? html`<span class="sleeve-review">Review</span>` : ""}
      ${call && size === "sm" ? html`<span class="sleeve-ic">${icon("mic", 20, 2.2)}</span>` : html`<span class="sleeve-t">${short}</span>`}
      ${making ? html`<span class="sleeve-making">${ring({ lit: (i) => i < Math.round((s.render?.progress || 0) * 60), r: 44, dot: 2.6 })}
        ${size !== "sm" ? html`<b>${Math.round((s.render?.progress || 0) * 100)}%</b>` : ""}</span>` : ""}
    </span>`;
}

// ---- pod lists ---------------------------------------------------------------------------

function hrefOf(s) {
  const call = s.kind === "cohost";
  return call && (s.status === "live" || s.status === "editing") ? `/cohost/${s.id}` : `/show/${s.id}`;
}

function metaOf(s, scope) {
  const live = s.status === "live";
  const call = s.kind === "cohost";
  const memo = s.kind === "memo";
  const by = scope === "following" && s.via ? `${s.via}, `
    : scope !== "mine" && s.owner_name ? `${s.owner_name}, ` : "";
  return live ? `${by}${call ? "on a call now" : "live now"}`
    : s.status === "editing" ? "Editing the episode…"
      : s.status === "draft" ? `Draft, ${s.style_label}`
        : s.status === "rendering" ? `From a voice memo, making it (${Math.round((s.render?.progress || 0) * 100)}%)`
          : s.status === "review" ? "From a voice memo, ready for you to review"
            : s.status === "failed" ? (memo ? "From a voice memo, didn't get made" : "Didn't finish")
              : s.status === "interrupted" ? `${by}interrupted${s.duration_s ? `, ${fmtTime(s.duration_s)}` : ""}`
                : `${by}${s.style_label}${s.duration_s ? `, ${fmtTime(s.duration_s)}` : ""}, ${fmtAgo(s.created_at)}`;
}

export function row(s, active, scope) {
  const hot = s.status === "live" || s.status === "review";
  return html`
    <li data-id="${s.id}">
      <a class="lib-row ${active ? "active" : ""}" href="${hrefOf(s)}" data-link>
        ${sleeve(s, "sm")}
        <span class="lib-text"><span class="lib-title">${s.title || "Untitled pod"}</span>
          <span class="lib-meta ${hot ? "live" : ""}">${metaOf(s, scope)}</span></span>
        ${s.new ? html`<span class="tag new-tag">New</span>` : ""}
        ${scope === "mine" && !["draft", "rendering", "review"].includes(s.status) ? html`
          <span class="vis-ic" title="${VISIBILITY_LABEL[s.visibility]}" aria-label="${VISIBILITY_LABEL[s.visibility]}">${icon(VISIBILITY_ICON[s.visibility] || "lock", 16)}</span>` : ""}
      </a>
    </li>`;
}

function card(s, scope) {
  return html`
    <li data-id="${s.id}">
      <a class="pod-card" href="${hrefOf(s)}" data-link>
        ${sleeve(s, "lg")}
        <span class="pod-title">${s.title || "Untitled pod"}</span>
        <span class="pod-meta ${s.status === "live" || s.status === "review" ? "live" : ""}">${metaOf(s, scope)}</span>
      </a>
    </li>`;
}

/**
 * A filtered, searchable pod list, refreshed while open.
 *   scopes: which lists get a tab (default: all four); scope: force one list, no tabs
 *   layout: "rows" or "grid"; key: where the chosen tab is remembered
 */
export function mountLibraryList(container, {
  activeId = null, compact = false, scope: forced = null,
  scopes = ["mine", "following", "shared", "public"], layout = "rows", key = "gp_scope",
} = {}) {
  const lists = {};
  let scope = forced || (session.signedIn ? local.get(key, scopes[0]) : "public");
  if (!scopes.includes(scope) && !forced) scope = scopes[0];
  if (!session.signedIn) scope = "public";
  let filter = local.get("gp_filter", "all");
  let tag = "";
  let query = "";
  let timer = 0;
  let unseen = 0;
  let markedSeen = false;
  let countedAt = 0;
  let firstPaint = true;

  const render = () => {
    const shows = lists[scope];
    const tabs = session.signedIn && !forced && scopes.length > 1;
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
    const paint = () => setHtml(container, html`
      ${compact ? html`<div class="lib-head"><h2>${SCOPE_LABEL[scope] === "Yours" ? "Your pods" : SCOPE_LABEL[scope]}</h2>
        ${session.signedIn ? html`<a class="round-btn small" href="/new" data-link aria-label="New pod">${icon("plus", 16, 2.4)}</a>` : ""}</div>` : ""}
      ${tabs ? html`<div class="seg-tabs" role="tablist" aria-label="Which pods">
        ${scopes.map((id) => html`<button type="button" role="tab" class="seg-tab" data-scope="${id}" aria-selected="${scope === id}">${SCOPE_LABEL[id]}${
          id === "following" && unseen ? html`<span class="tab-badge" aria-label="${unseen} new">${unseen}</span>` : ""}</button>`)}
      </div>` : ""}
      ${scope === "mine" ? html`<div class="chips" role="group" aria-label="Filter pods">
        ${FILTERS.map(([id, label]) => html`<button type="button" class="chip" data-filter="${id}" aria-pressed="${filter === id}">${label}</button>`)}
        ${tags.map((t) => html`<button type="button" class="chip tag" data-tag="${t}" aria-pressed="${tag === t}">#${t}</button>`)}
      </div>` : ""}
      ${liveNow.length ? html`<a class="live-banner" href="/show/${liveNow[0].id}" data-link>
        <span class="badge live small"><span class="dot"></span>Live</span>
        <span class="ellipsis"><strong>${liveNow[0].title || "A pod"}</strong>, from ${liveNow[0].via}</span></a>` : ""}
      ${!shows ? skeleton(layout)
        : list.length ? html`<ul class="${layout === "grid" ? "pod-grid" : `lib-list ${compact ? "compact" : ""}`}">${list.map((s) =>
          layout === "grid" ? card(s, scope) : row(s, s.id === activeId, scope))}</ul>`
          : html`<p class="empty">${emptyText(scope, shows.length)}</p>`}`);
    // Rows move to their new places instead of flashing (filters, tags, refreshes).
    if (shows && !firstPaint) flipById(container, paint);
    else paint();
    slideIndicator(container.querySelector(".seg-tabs"), `tabs-${key}`);
    if (shows && firstPaint) {
      firstPaint = false;
      stagger(container.querySelectorAll("[data-id]"), { step: 30, rise: 6 });
    }
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
      if (session.signedIn && !forced && scopes.includes("following") && !markedSeen && Date.now() - countedAt > 60000) {
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
      local.set(key, scope);
      firstPaint = true;
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

/** Placeholders shaped like the rows that are coming; they only appear after 300 ms. */
function skeleton(layout) {
  const n = layout === "grid" ? 4 : 5;
  return html`<div class="skeleton ${layout}" aria-label="Loading">${Array.from({ length: n }, () =>
    layout === "grid" ? html`<span class="sk-card"><span class="sk sq"></span><span class="sk ln"></span><span class="sk ln short"></span></span>`
      : html`<span class="sk-row"><span class="sk sq"></span><span class="col grow"><span class="sk ln"></span><span class="sk ln short"></span></span></span>`)}</div>`;
}

function emptyText(scope, total) {
  if (total) return "Nothing matches.";
  if (scope === "shared") return "Nothing shared with you yet. Pods your connections share show up here.";
  if (scope === "public") return "No public pods yet. Be the first to publish one.";
  if (scope === "following") return "Nothing here yet. Subscribe to a creator or an album, and their pods show up here.";
  return "No pods yet. Start one: it takes a few seconds.";
}

// ---- templates -----------------------------------------------------------------------------

export function templateCard(t, { selected = false, as = "button" } = {}) {
  const style = `--tile:${t.accent};--gel:${gelFor(t.accent)}`;
  const inner = html`
    <span class="tpl-top"><span class="tpl-label">${t.label}</span>
      <span class="tpl-meta">${t.hosts.length === 1 ? "1 host" : "2 hosts"}, ${t.minutes} min</span></span>
    <span class="tpl-blurb">${t.blurb}</span>
    <span class="tpl-hosts">${t.hosts.map((h) => html`<span class="tpl-host">${h.name}</span>`)}</span>
    ${selected ? html`<span class="vibe-check" aria-hidden="true">${icon("check", 14, 3)}</span>` : ""}`;
  return as === "link"
    ? html`<a class="tpl" href="/new?template=${t.id}" data-link style="${style}">${inner}</a>`
    : html`<button type="button" class="tpl" data-template="${t.id}" aria-pressed="${selected}" style="${style}">${inner}</button>`;
}

// ---- Home ------------------------------------------------------------------------------------

/** "/": what to make next, what's on air, your latest pods. Signed out: what this is. */
export async function libraryView(root, { url }) {
  if (url.pathname === "/discover" || url.pathname === "/following") return discoverView(root, { url });
  applyAccent(document.documentElement, HOUSE_ACCENT);
  const el = document.createElement("div");
  el.className = "house-page home";
  const templates = session.options.templates || [];
  const me = session.me;
  const first = me ? me.name.split(" ")[0] : "";
  const hour = new Date().getHours();
  const hello = hour < 12 ? `Morning, ${first}.` : hour < 18 ? `Hi ${first}.` : `Evening, ${first}.`;
  setHtml(el, html`
    ${appHeader({ search: false })}
    <main class="home-main">
      ${me ? html`
        <section class="home-hero">
          <h1>${hello} What should today's pod be about?</h1>
          <div data-brief></div>
          ${me.credits > 0 ? html`
            <form class="composer card" data-composer>
              <label class="sr-only" for="compose">What the pod is about</label>
              <textarea id="compose" class="composer-area" rows="3" placeholder="Paste notes, drop in an idea, or type a topic. Add files and links on the next step."></textarea>
              <div class="composer-row">
                <a class="composer-tool rec" href="/memo" data-link aria-label="Make a pod from a voice memo" title="From a voice memo">${icon("mic", 18)}</a>
                <span class="grow"></span>
                <a class="btn quiet" href="/cohost" data-link>${icon("mic", 16, 2.2)}Co-host</a>
                <button class="btn white" type="submit"><span class="rec-dot"></span>Start a pod</button>
              </div>
            </form>`
            : html`<div class="free-note">${icon("coin", 18)}<span><strong>You're on a free account.</strong> Listen, call in, connect with people and build personas for free.
                Making pods uses credits (${session.options.credits_per_minute} a minute); ask an admin for demo credits.</span></div>`}
          ${!session.options.api_key_set ? html`<p class="error">GEMINI_API_KEY isn't set on the server yet, so pods can't start.</p>` : ""}
        </section>` : html`
        <section class="home-hero signed-out">
          <h1>Live podcasts about anything. Call in while they're on air.</h1>
          <p class="muted">Paste notes, drop in a PDF, share a link or just type a topic. The hosts start talking in seconds, and anyone listening can call in.</p>
          <div class="row gap wrap">
            <a class="btn white big" href="${loginPath("/new")}" data-link>Get started</a>
            <a class="btn quiet big" href="/discover" data-link>Listen to public pods</a>
          </div>
        </section>`}
      <section class="shelf" data-live-shelf hidden aria-labelledby="live-h">
        <div class="shelf-head"><h2 id="live-h" class="shelf-h"><span class="lamp red"></span>Live now</h2><a class="shelf-more" href="/discover" data-link>See all</a></div>
        <div class="shelf-row" data-live-row></div>
      </section>
      ${me ? html`
        <section class="shelf" aria-labelledby="mine-h">
          <div class="shelf-head"><h2 id="mine-h" class="shelf-h">Your pods</h2><a class="shelf-more" href="/library" data-link>Library</a></div>
          <ul class="lib-list" data-recent>${skeleton("rows")}</ul>
        </section>` : ""}
      ${templates.length ? html`
        <section class="shelf" aria-labelledby="tpl-h">
          <div class="shelf-head"><h2 id="tpl-h" class="shelf-h">Start from a template</h2></div>
          <div class="tpl-grid">${templates.map((t) => templateCard(t, { as: "link" }))}</div>
        </section>` : ""}
    </main>`);
  root.append(el);
  const offHead = bindHeader(el);

  const form = el.querySelector("[data-composer]");
  form?.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = el.querySelector("#compose").value.trim();
    navigate(text ? `/new?topic=${encodeURIComponent(text)}` : "/new");
  });

  // Live now: public pods on air, and pods on air from people you follow.
  Promise.all([api.listShows("public").catch(() => ({ shows: [] })), me ? api.feed().catch(() => ({ shows: [] })) : { shows: [] }])
    .then(([pub, feed]) => {
      const seen = new Set();
      const live = [...feed.shows, ...pub.shows].filter((s) => s.status === "live" && !seen.has(s.id) && seen.add(s.id)).slice(0, 8);
      if (!live.length || !el.isConnected) return;
      const shelf = el.querySelector("[data-live-shelf]");
      shelf.hidden = false;
      setHtml(el.querySelector("[data-live-row]"), html`${live.map((s) => html`
        <a class="shelf-pod" href="${hrefOf(s)}" data-link>${sleeve(s, "md")}
          <span class="pod-title">${s.title || "A pod"}</span><span class="pod-meta">${s.via || s.owner_name || s.style_label}</span></a>`)}`);
      stagger(shelf.querySelectorAll(".shelf-pod"), { step: 40 });
    });

  if (me) {
    api.listShows("mine").then(({ shows }) => {
      const ul = el.querySelector("[data-recent]");
      if (!ul) return;
      setHtml(ul, shows.length ? html`${shows.slice(0, 4).map((s) => row(s, false, "mine"))}`
        : html`<li class="empty">No pods yet. Start one above: it takes a few seconds.</li>`);
      stagger(ul.querySelectorAll("[data-id]"), { step: 30, rise: 6 });
    }).catch(() => {});
    // A briefing that's ready waits at the top.
    if (api.briefings) {
      api.briefings().then((d) => {
        const b = (d.briefings || []).find((x) => !x.rendering && x.has_audio && x.status === "finished");
        const slot = el.querySelector("[data-brief]");
        if (!b || !slot) return;
        setHtml(slot, html`<a class="brief-ready" href="/show/${b.id}" data-link style="--gel:${gelFor(b.accent || "#2E6F9E")}">
          <span class="brief-play">${icon("play", 22)}</span>
          <span class="col grow"><strong>${b.briefing?.scheduled ? "Your briefing is ready" : "Your latest briefing"}</strong>
            <span class="small">${b.title || "Briefing"}, ${b.duration_s ? fmtTime(b.duration_s) : `${b.minutes} min`}</span></span></a>`);
      }).catch(() => {});
    }
    session.refresh();
  }
  return { unmount() { offHead(); } };
}

// ---- Discover and Library ------------------------------------------------------------------------

async function discoverView(root, { url }) {
  if (url.pathname === "/following") {
    if (!session.signedIn) {
      navigate(loginPath("/following"), { replace: true });
      return null;
    }
    local.set("gp_discover", "following");
  }
  applyAccent(document.documentElement, HOUSE_ACCENT);
  const el = document.createElement("div");
  el.className = "house-page";
  setHtml(el, html`
    ${appHeader()}
    <main class="page wide">
      <div class="page-title"><h1>Discover</h1><p class="muted">Public pods anyone can hear, and new ones from the creators and albums you follow.</p></div>
      <div class="lib-body" data-list></div>
    </main>`);
  root.append(el);
  const lib = mountLibraryList(el.querySelector("[data-list]"), {
    scopes: session.signedIn ? ["public", "following"] : ["public"], key: "gp_discover", layout: "grid",
  });
  const offHead = bindHeader(el, { onSearch: (q) => lib.search(q) });
  return { unmount() { lib.destroy(); offHead(); } };
}

export async function libraryPageView(root) {
  if (!session.signedIn) {
    navigate(loginPath("/library"), { replace: true });
    return null;
  }
  applyAccent(document.documentElement, HOUSE_ACCENT);
  const el = document.createElement("div");
  el.className = "house-page";
  setHtml(el, html`
    ${appHeader()}
    <main class="page wide">
      <div class="page-title row"><div class="col grow"><h1>Library</h1></div>
        <a class="btn chip-btn small-btn" href="/albums" data-link>${icon("bars", 14, 2.4)}Albums</a>
        <a class="btn chip-btn small-btn" href="/stats" data-link>${icon("people", 14, 2.2)}Stats</a></div>
      <div class="lib-body" data-list></div>
    </main>`);
  root.append(el);
  const lib = mountLibraryList(el.querySelector("[data-list]"), { scopes: ["mine", "shared"], key: "gp_library", layout: "grid" });
  const offHead = bindHeader(el, { onSearch: (q) => lib.search(q) });
  return { unmount() { lib.destroy(); offHead(); } };
}

// ---- You -------------------------------------------------------------------------------------------

export async function youView(root) {
  const me = session.me;
  if (!me) {
    navigate(loginPath("/you"), { replace: true });
    return null;
  }
  applyAccent(document.documentElement, HOUSE_ACCENT);
  const el = document.createElement("div");
  el.className = "house-page";
  const perMin = session.options.credits_per_minute;
  setHtml(el, html`
    ${appHeader({ search: false })}
    <main class="page narrow you">
      <div class="you-who">
        ${meAvatar(56)}
        <span class="col grow"><h1>${me.name}</h1><span class="muted small">${me.email}</span></span>
        ${me.admin ? html`<span class="tag">Admin</span>` : ""}
      </div>
      <section class="card credit-card">
        ${ring({ lit: (i) => i < Math.min(60, me.credits), cls: "credit-ring", label: `${me.credits} credits` })}
        <span class="col grow"><strong class="credit-num">${me.credits} credits</strong>
          <span class="muted small">${me.credits ? `About ${Math.round(me.credits / perMin)} minutes of pods, at ${perMin} credit a minute.` : "A free account: listen, call in and connect for free."}</span></span>
      </section>
      <ul class="ledger" data-ledger><li class="faint small">Loading your credit history…</li></ul>
      <nav class="menu" aria-label="Your things">
        <a class="menu-item" href="/briefings" data-link>${icon("sparkle", 20)}Briefings<span class="faint">made just for you</span>${icon("chevronRight", 16)}</a>
        <a class="menu-item" href="/intro" data-link>${icon("mic", 20)}Show intro${icon("chevronRight", 16)}</a>
        <a class="menu-item" href="/memo" data-link>${icon("mic", 20)}Pod from a voice memo${icon("chevronRight", 16)}</a>
        <a class="menu-item" href="/personas" data-link>${icon("user", 20)}Personas${icon("chevronRight", 16)}</a>
        <a class="menu-item" href="/styles" data-link>${icon("restyle", 20)}Styles${icon("chevronRight", 16)}</a>
        <a class="menu-item" href="/connections" data-link>${icon("people", 20)}Connections
          ${session.incoming ? html`<span class="tag new-tag">${session.incoming} new</span>` : ""}${icon("chevronRight", 16)}</a>
        <a class="menu-item" href="/stats" data-link>${icon("bars", 20)}Stats${icon("chevronRight", 16)}</a>
        <a class="menu-item" href="/creator/${me.uid}" data-link>${icon("globe", 20)}Your creator page${icon("chevronRight", 16)}</a>
        ${me.admin ? html`<a class="menu-item" href="/admin" data-link>${icon("shield", 20)}Admin${icon("chevronRight", 16)}</a>` : ""}
        <button class="menu-item" type="button" data-signout>${icon("signOut", 20)}Sign out</button>
      </nav>
    </main>`);
  root.append(el);
  const offHead = bindHeader(el);
  el.querySelector("[data-signout]").addEventListener("click", async () => {
    await session.signOut();
    navigate("/", { replace: true });
  });
  api.ledger().then((r) => {
    const ul = el.querySelector("[data-ledger]");
    if (!ul) return;
    setHtml(ul, r.entries.length ? html`${r.entries.slice(0, 6).map((e) => html`
      <li><span>${ledgerLabel(e)}</span><span class="${e.amount > 0 ? "plus" : "minus"}">${e.amount > 0 ? "+" : ""}${e.amount}</span></li>`)}` : "");
  }).catch(() => setHtml(el.querySelector("[data-ledger]"), ""));
  session.refresh();
  return { unmount() { offHead(); } };
}

/** Kept for callers that still open the account as a sheet: it's a page now. */
export function openAccountSheet() {
  navigate(session.me ? "/you" : loginPath());
}

function ledgerLabel(e) {
  const when = fmtAgo(e.at);
  switch (e.reason) {
    case "pod": return `Pod made, ${when}`;
    case "music": return `Original music, ${when}`;
    case "briefing": return `Briefing, ${when}`;
    case "refund": return `${e.note ? `Refund: ${e.note}` : "Unused minutes back"}, ${when}`;
    case "signup": return `Welcome credits, ${when}`;
    case "grant": return `${e.note || "Credits added"}, ${when}`;
    default: return `${e.note || "Adjustment"}, ${when}`;
  }
}

