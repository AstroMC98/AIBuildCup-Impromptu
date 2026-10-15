// /stats (all your pods) and /show/{id}/stats (one pod): how many listened, for how
// long, when people left, replays, and who listened. Names are shown for signed-in
// listeners only, and only to the pod's creator; anonymous listeners are counted.
import { api } from "../api.js";
import { html, raw, setHtml } from "../html.js";
import { icon } from "../icons.js";
import { navigate } from "../router.js";
import { loginPath, session } from "../session.js";
import { applyAccent, callerColor, initial } from "../theme.js";
import { fmtAgo, fmtTime } from "../util.js";
import { appHeader, bindHeader } from "./library.js";

const REFRESH_MS = 10000;

function duration(sec) {
  sec = Math.round(sec || 0);
  if (sec < 60) return `${sec}s`;
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m ${String(sec % 60).padStart(2, "0")}s`;
}

function tile(label, value, sub = "") {
  return html`<div class="stat-tile"><span class="stat-num">${value}</span><span class="stat-label">${label}</span>
    ${sub ? html`<span class="faint small">${sub}</span>` : ""}</div>`;
}

/** Listening over time: a stepped area chart of [seconds into the show, listeners]. */
function timelineChart(points, total) {
  if (!points.length) return html`<p class="empty">No listening yet.</p>`;
  const w = 640, h = 160, pad = 24;
  const xMax = Math.max(total || 0, points[points.length - 1][0], 60);
  const yMax = Math.max(1, ...points.map((p) => p[1]));
  const x = (t) => pad + (t / xMax) * (w - pad * 2);
  const y = (n) => h - pad - (n / yMax) * (h - pad * 2);
  let d = `M${x(points[0][0])},${y(0)}`;
  for (let i = 0; i < points.length; i++) {
    const [t, n] = points[i];
    d += ` L${x(t)},${y(i ? points[i - 1][1] : n)} L${x(t)},${y(n)}`;
  }
  const last = points[points.length - 1];
  d += ` L${x(Math.max(last[0], xMax))},${y(last[1])} L${x(Math.max(last[0], xMax))},${y(0)} Z`;
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => Math.round(f * xMax));
  return html`<svg class="chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="Listeners over the show, up to ${yMax} at once">
    <line x1="${pad}" x2="${w - pad}" y1="${y(0)}" y2="${y(0)}" class="axis"/>
    <line x1="${pad}" x2="${w - pad}" y1="${y(yMax)}" y2="${y(yMax)}" class="grid"/>
    <text x="${pad - 6}" y="${y(yMax) + 4}" class="tick" text-anchor="end">${yMax}</text>
    <path d="${d}" class="area"/>
    ${ticks.map((t) => raw(`<text x="${x(t)}" y="${h - 6}" class="tick" text-anchor="middle">${fmtTime(t)}</text>`))}
  </svg>`;
}

/** When people left: one bar per minute of the show. */
function dropoffChart(dropoff, total) {
  const minutes = Math.max(1, Math.ceil((total || 0) / 60));
  const counts = new Array(minutes).fill(0);
  for (const d of dropoff) if (d.minute < minutes) counts[d.minute] += d.left; else counts[minutes - 1] += d.left;
  const max = Math.max(1, ...counts);
  if (!dropoff.length) return html`<p class="empty">Nobody left early.</p>`;
  return html`<div class="bars" role="img" aria-label="Listeners who left, by minute of the show">
    ${counts.map((n, i) => html`<span class="bar-col" title="${n} left during minute ${i + 1}">
      <span class="bar" style="--h:${(n / max) * 100}%">${n ? html`<span class="bar-n">${n}</span>` : ""}</span>
      <span class="bar-x">${i % Math.ceil(minutes / 8) === 0 ? `${i}m` : ""}</span></span>`)}
  </div>`;
}

function personRow(p) {
  const live = p.live, rep = p.replay;
  const liveText = live ? `${fmtTime(live.join_pos || 0)} to ${live.to_end ? "the end" : fmtTime(live.leave_pos || 0)}, ${duration(live.listened_s)} heard` : "";
  const repText = rep ? `replay: ${duration(rep.listened_s)}${rep.completed ? ", finished it" : rep.leave_pos ? `, got to ${fmtTime(rep.leave_pos)}` : ""}` : "";
  return html`<li class="person">
    <span class="avatar" style="--av:${callerColor(p.name)};--size:34px">${initial(p.name)}</span>
    <span class="col grow"><strong>${p.name}</strong>
      <span class="muted small">${[live ? `Live ${liveText}` : "", repText].filter(Boolean).join(" · ")}</span></span>
    ${live && live.to_end ? html`<span class="pill next">${live.here_now ? "Here now" : "Stayed"}</span>` : ""}
  </li>`;
}

// ---- one pod ---------------------------------------------------------------------------------

export async function showStatsView(root, { params: [showId] }) {
  if (!session.signedIn) {
    navigate(loginPath(), { replace: true });
    return null;
  }
  const el = document.createElement("div");
  el.className = "page-wrap";
  root.append(el);
  setHtml(el, html`${appHeader({ search: false })}<main class="page wide" data-body><p class="empty">Loading…</p></main>`);
  const offHead = bindHeader(el);
  let timer = 0;

  const draw = (data) => {
    const { show, stats: st, live } = data;
    applyAccent(document.documentElement, show.accent || "#B8336A");
    const L = st.live, R = st.replays;
    setHtml(el.querySelector("[data-body]"), html`
      <a class="back-link" href="/show/${show.id}" data-link>${icon("chevronLeft", 18, 2.4)}Back to the pod</a>
      <div class="page-title row">
        <div class="col grow"><h1>${show.title || "Untitled pod"}</h1>
          <p class="muted">${live ? html`<span class="badge live small"><span class="dot"></span>Live</span> ${L.now} listening now · updates every few seconds`
            : `Stats · ${fmtAgo(show.created_at)}${st.duration_s ? ` · ${fmtTime(st.duration_s)} long` : ""}`}</p></div>
      </div>
      <div class="stat-grid">
        ${tile("Listeners", L.unique, `${L.signed_in} signed in, ${L.anonymous} anonymous`)}
        ${tile("Peak at once", L.peak)}
        ${tile("Average listen", duration(L.avg_listen_s), `${duration(L.listened_s)} in all`)}
        ${tile("Stayed to the end", L.stayed_to_end)}
        ${tile("Replays", R.plays, R.plays ? `${Math.round(R.avg_completion * 100)}% average, ${R.completed} finished` : "")}
        ${tile("Call-ins", st.engagement.comments)}
        ${tile("Reactions", st.engagement.hearts)}
        ${tile("Your subscribers", st.engagement.subscribers)}
      </div>
      <section class="card"><h2 class="card-h">Listening over the show</h2>${timelineChart(L.timeline, st.duration_s)}</section>
      <section class="card"><h2 class="card-h">When people left <span class="faint">live listeners who dropped off, by minute</span></h2>
        ${dropoffChart(L.dropoff, st.duration_s || (L.timeline.at(-1)?.[0] ?? 0))}</section>
      <section class="card"><h2 class="card-h">Who listened <span class="faint">signed-in listeners; only you can see this</span></h2>
        ${st.people.length ? html`<ul class="people">${st.people.map(personRow)}</ul>` : html`<p class="empty">No signed-in listeners yet.</p>`}
        ${st.anonymous ? html`<p class="faint small">Plus ${st.anonymous} anonymous ${st.anonymous === 1 ? "listener" : "listeners"}: counted, never named.</p>` : ""}
      </section>`);
    clearTimeout(timer);
    if (live) timer = setTimeout(load, REFRESH_MS);
  };

  const load = async () => {
    try {
      draw(await api.showStats(showId));
    } catch (err) {
      setHtml(el.querySelector("[data-body]"), html`<div class="splash-card"><h1>No stats here</h1><p class="muted">${err.message}</p>
        <a class="btn white" href="/show/${showId}" data-link>Back to the pod</a></div>`);
    }
  };
  await load();
  return { unmount() { clearTimeout(timer); offHead(); } };
}

// ---- all pods ------------------------------------------------------------------------------------

export async function statsView(root) {
  if (!session.signedIn) {
    navigate(loginPath("/stats"), { replace: true });
    return null;
  }
  applyAccent(document.documentElement, "#B8336A");
  const el = document.createElement("div");
  el.className = "page-wrap";
  root.append(el);
  setHtml(el, html`${appHeader({ search: false })}<main class="page wide" data-body><p class="empty">Loading…</p></main>`);
  const offHead = bindHeader(el);
  const data = await api.myStats().catch((err) => ({ error: err.message }));
  if (data.error) {
    setHtml(el.querySelector("[data-body]"), html`<p class="empty">${data.error}</p>`);
    return { unmount() { offHead(); } };
  }
  const t = data.totals, subs = data.subscribers;
  setHtml(el.querySelector("[data-body]"), html`
    <div class="page-title row"><div class="col grow"><h1>Stats</h1>
      <p class="muted">How your pods are doing. Who listened is visible to you only, and only for signed-in listeners.</p></div>
      <a class="btn chip-btn" href="/creator/${session.me.uid}" data-link>Your creator page</a></div>
    <div class="stat-grid">
      ${tile("Subscribers", subs.creator, "to you")}
      ${tile("Pods", t.pods)}
      ${tile("Listeners", t.listeners, "live, across pods")}
      ${tile("Listening time", duration(t.listened_s), "live and replays")}
      ${tile("Replays", t.replays)}
    </div>
    ${subs.albums.length ? html`<section class="card"><h2 class="card-h">Album subscribers</h2>
      <ul class="people">${subs.albums.map((a) => html`<li class="person"><a class="grow" href="/album/${a.id}" data-link><strong>${a.title}</strong></a>
        <span class="muted">${a.subscribers}</span></li>`)}</ul></section>` : ""}
    <section class="card"><h2 class="card-h">Your pods</h2>
      ${data.pods.length ? html`<div class="stats-table" role="table" aria-label="Your pods">
        <div class="st-row st-head" role="row"><span role="columnheader">Pod</span><span role="columnheader">Listeners</span>
          <span role="columnheader">Peak</span><span role="columnheader">Avg listen</span><span role="columnheader">Replays</span></div>
        ${data.pods.map((p) => html`<a class="st-row" role="row" href="/show/${p.id}/stats" data-link>
          <span role="cell" class="st-title"><span class="lib-cover" style="--tile:${p.accent || "#30284F"}" aria-hidden="true">${initial(p.title)}</span>
            <span class="col"><strong class="ellipsis">${p.title || "Untitled pod"}</strong>
              <span class="faint small">${p.status === "live" ? "Live now" : fmtAgo(p.created_at)}</span></span></span>
          <span role="cell">${p.listeners}</span><span role="cell">${p.peak}</span>
          <span role="cell">${duration(p.avg_listen_s)}</span><span role="cell">${p.replays}</span></a>`)}
      </div>` : html`<p class="empty">No pods yet.</p>`}
    </section>`);
  return { unmount() { offHead(); } };
}
