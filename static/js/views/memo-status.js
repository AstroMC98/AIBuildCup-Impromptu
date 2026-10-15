// A voice-memo pod before it's out (only its owner gets here): being made, waiting
// for the owner to review the outline, or failed. Once it's finished, /show/{id}
// is an ordinary replay.
import { api } from "../api.js";
import { searchSuggestions, webSources } from "../components.js";
import { html, setHtml } from "../html.js";
import { icon } from "../icons.js";
import { lengthChip, musicCards } from "../pod-options.js";
import { fillRing, ring } from "../ring.js";
import { navigate } from "../router.js";
import { session } from "../session.js";
import { applyAccent, setWorld } from "../theme.js";
import { confirmSheet } from "../ui.js";
import { fmtTime, toast } from "../util.js";

const POLL_MS = 3000;
const LENGTHS = [5, 10, 15];
const MIN_BEATS = 3, MAX_BEATS = 10;
const STAGES = [
  ["transcribing", "Listening to your memo"],
  ["researching", "Looking things up"],
  ["outlining", "Planning the episode"],
  ["queued", "Waiting for a free studio"],
  ["voicing", "Recording the hosts"],
  ["scoring", "Adding the music"],
  ["mixing", "Mixing"],
  ["saving", "Saving the episode"],
];
const CONFIDENCE = { mixed: "Sources disagree", thin: "Thin evidence" };

export function memoScreen(root, data) {
  applyAccent(document.documentElement, data.show.accent);
  document.title = `${data.show.title || "Voice memo"} – Impromptu`;
  const el = document.createElement("div");   // listeners go here, not on the router's root
  root.replaceChildren(el);
  const status = data.show.status;
  // Reviewing is paperwork (the house); being made and failing happen on the stage.
  if (status === "review") {
    setWorld("house");
    return reviewScreen(el, data);
  }
  el.className = "memo-stage stage";
  if (status === "failed") return failedScreen(el, data);
  return makingScreen(el, data);
}

const reload = () => navigate(location.pathname, { replace: true });

async function deletePod(id) {
  const ok = await confirmSheet({
    title: "Delete this pod?", message: "It stops being made, and any credits it used come back.", confirm: "Delete", danger: true,
  });
  if (!ok) return;
  await api.deleteShow(id).catch((err) => toast(err.message));
  session.refresh();
  navigate("/", { replace: true });
}

// ---- being made ------------------------------------------------------------------------

/** One lamp per step. failed: the step it stopped at is lit red. */
function stageList(show, { failed = false } = {}) {
  const r = show.render || {};
  let at = STAGES.findIndex(([id]) => id === r.stage);
  if (r.stage === "queued" && !(show.beats || []).length) at = 0;   // just sent: nothing's queued for long
  return html`${STAGES.map(([id, label], i) => {
    if (id === "queued" && r.stage !== "queued") return "";
    const state = i < at ? "done" : i === at ? (failed ? "failed" : "now") : "later";
    return html`<li class="make-stage ${state}">
      <span class="make-mark" aria-hidden="true">${state === "done" ? icon("check", 14, 3) : state === "failed" ? "!" : ""}</span>
      <span>${label}</span></li>`;
  })}`;
}

function makingScreen(root, data) {
  const id = data.show.id;
  setHtml(root, html`
    <div class="page narrow memo-status">
      <a class="back-link" href="/" data-link>${icon("chevronLeft", 20)}Your pods</a>
      <div class="draft-card making">
        <span class="badge live small"><span class="dot"></span>Making your pod</span>
        <div class="make-clock" role="progressbar" aria-label="Progress" aria-valuemin="0" aria-valuemax="100">
          ${ring({ r: 46, dot: 2.1 })}<span class="make-pct" data-pct></span></div>
        <h1 data-title></h1>
        <ol class="make-stages" data-stages></ol>
        <p class="muted small" data-detail aria-live="polite"></p>
        <p class="faint small">You can leave this page. Your pod keeps going, and it's in Your pods when it's ready.</p>
        <button class="btn link danger-text" type="button" data-delete>${icon("trash", 18)}Stop and delete</button>
      </div>
    </div>`);
  const $ = (s) => root.querySelector(s);
  const paint = (show) => {
    const r = show.render || {};
    $("[data-title]").textContent = show.title || "Voice memo";
    const pct = Math.round((r.progress || 0) * 100);
    fillRing($(".make-clock .ring"), Math.max(1, Math.round(pct * 0.6)));   // the hour fills as it's made
    setHtml($("[data-pct]"), html`${pct}<small>%</small>`);
    $(".make-clock").setAttribute("aria-valuenow", String(pct));
    setHtml($("[data-stages]"), stageList(show));
    $("[data-detail]").textContent = r.detail || "";
  };
  paint(data.show);
  $("[data-delete]").addEventListener("click", () => deletePod(id));

  const timer = setInterval(async () => {
    try {
      const fresh = await api.getShow(id);
      if (fresh.show.status !== "rendering") {
        clearInterval(timer);
        if (fresh.show.status === "finished") {
          session.refresh();   // unused minutes come back
          toast("Your pod is ready.");
        }
        reload();
      } else {
        paint(fresh.show);
      }
    } catch (err) {
      if (err.status === 404) {
        clearInterval(timer);
        navigate("/", { replace: true });
      }
    }
  }, POLL_MS);
  return { unmount() { clearInterval(timer); } };
}

// ---- failed -------------------------------------------------------------------------------

function failedScreen(root, data) {
  const s = data.show;
  const memo = data.memo || {};
  const opts = memo.options || {};
  const cost = session.cost(opts.minutes || s.minutes || 15) + (opts.music === "generate" ? memo.credits?.music || 0 : 0);
  setHtml(root, html`
    <div class="page narrow">
      <a class="back-link" href="/" data-link>${icon("chevronLeft", 20)}Your pods</a>
      <div class="draft-card making">
        <span class="badge live small">Didn't get made</span>
        <h1>${s.title || "Voice memo"}</h1>
        ${STAGES.some(([id]) => id === s.render?.stage) ? html`<ol class="make-stages">${stageList(s, { failed: true })}</ol>` : ""}
        <p class="muted">${s.render?.error || "Something went wrong while making this pod."} Any credits it used came back.</p>
        <p class="cost ${session.credits < cost ? "short" : ""}">Trying again uses ${cost} credits · you have ${session.credits}</p>
        <p class="error" role="alert" data-error></p>
        <button class="btn white big block go" type="button" data-again ${session.credits < cost ? "disabled" : ""}>${icon("sparkle", 20)}Try again</button>
        <button class="btn link danger-text" type="button" data-delete>${icon("trash", 18)}Delete</button>
      </div>
    </div>`);
  root.querySelector("[data-again]").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    try {
      await api.produceMemo(s.id, {});
      session.refresh();
      reload();
    } catch (err) {
      root.querySelector("[data-error]").textContent = err.message;
      btn.disabled = false;
    }
  });
  root.querySelector("[data-delete]").addEventListener("click", () => deletePod(s.id));
  return null;
}

// ---- review -------------------------------------------------------------------------------

function reviewScreen(root, data) {
  const s = data.show;
  const memo = data.memo || {};
  const brief = memo.brief || {};
  const research = memo.research || {};
  const opts = memo.options || {};
  const plan = memo.plan || { title: s.title, beats: s.beats || [] };
  const state = {
    title: plan.title || s.title || "",
    beats: (plan.beats || []).map((b) => ({ title: b.title, notes: b.notes || "" })),
    excluded: new Set(memo.excluded_findings || []),
    minutes: opts.minutes || s.minutes || 15,
    music: opts.music || "auto",
    mine: [],
  };

  setHtml(root, html`
    <div class="page memo-review">
      <a class="back-link" href="/" data-link>${icon("chevronLeft", 20)}Your pods</a>
      <div class="page-title col">
        <span class="badge ended">Ready for you to review</span>
        <h1>Check the outline</h1>
        <p class="muted">Nothing's recorded yet. Change anything you like, then press Produce.</p>
      </div>

      <section class="card" aria-labelledby="rv-memo">
        <h2 id="rv-memo" class="card-h">Your memo</h2>
        ${memo.audio_url ? html`<audio class="memo-audio" controls preload="metadata" src="${memo.audio_url}"></audio>` : ""}
        ${brief.thesis ? html`<p><strong>The idea:</strong> ${brief.thesis}</p>` : ""}
        <details class="heard"><summary>What we heard${memo.duration_s ? ` (${fmtTime(memo.duration_s)})` : ""}</summary>
          <p class="muted">${brief.transcript || ""}</p></details>
      </section>

      <section class="card" aria-labelledby="rv-research">
        <h2 id="rv-research" class="card-h">What the research found</h2>
        ${research.ok ? html`
          ${research.summary ? html`<p>${research.summary}</p>` : ""}
          <p class="faint small">Untick anything the hosts shouldn't use.</p>
          <ul class="findings" data-findings></ul>
          ${(research.disagreements || []).length ? html`
            <p class="field-label">Where sources disagree</p>
            <ul class="plain">${research.disagreements.map((d) => html`<li>${d}</li>`)}</ul>` : ""}
          ${webSources(research.sources).length ? html`
            <p class="field-label">Sources</p>
            <ul class="sources">${webSources(research.sources).slice(0, 12).map((src) => html`
              <li><a href="${src.url}" target="_blank" rel="noopener noreferrer">${src.title}</a> <span class="faint small">${src.domain}</span></li>`)}</ul>` : ""}
          ${(research.search_suggestions || []).map(searchSuggestions)}`
          : html`<p class="muted">The research didn't come back. The hosts will stick to your memo and what's well established.</p>`}
      </section>

      <section class="card" aria-labelledby="rv-outline">
        <h2 id="rv-outline" class="card-h">The outline</h2>
        <label class="field-label" for="rv-title">Title</label>
        <input id="rv-title" class="field" maxlength="70" value="${state.title}">
        <p class="field-label">Chapters</p>
        <ol class="beat-edit" data-beats></ol>
        <button class="btn outline" type="button" data-add-beat>${icon("plus", 16, 2.4)}Add a chapter</button>
      </section>

      <h2 class="create-h2">How long?</h2>
      <div class="len-chips" role="group" aria-label="Length" data-lengths></div>
      <h2 class="create-h2">Theme music</h2>
      <div class="radio-cards" role="radiogroup" aria-label="Theme music" data-music></div>

      <p class="error" role="alert" data-error></p>
      <div class="go-bar">
        <p class="cost" data-cost></p>
        <button class="btn white big block go" type="button" data-produce>${icon("sparkle", 20)}Produce</button>
        <button class="btn link danger-text" type="button" data-delete>Delete</button>
      </div>
    </div>`);
  const $ = (q) => root.querySelector(q);

  const renderFindings = () => {
    const list = $("[data-findings]");
    if (!list) return;
    setHtml(list, html`${(research.findings || []).map((f) => html`
      <li><label class="finding">
        <input type="checkbox" data-finding="${f.id}" ${state.excluded.has(f.id) ? "" : "checked"}>
        <span class="col"><span>${f.claim}</span>
          <span class="faint small">${f.outlet}${f.date ? ` · ${f.date}` : ""}${CONFIDENCE[f.confidence] ? html` · <span class="pill done">${CONFIDENCE[f.confidence]}</span>` : ""}</span></span>
      </label></li>`)}`);
  };
  const renderBeats = () => {
    setHtml($("[data-beats]"), html`${state.beats.map((b, i) => html`
      <li class="beat">
        <span class="beat-n">${i + 1}</span>
        <span class="col grow">
          <label class="sr-only" for="beat-t-${i}">Chapter ${i + 1} title</label>
          <input id="beat-t-${i}" class="field" maxlength="80" value="${b.title}" data-beat-title="${i}">
          <label class="sr-only" for="beat-n-${i}">What it covers</label>
          <textarea id="beat-n-${i}" class="field area short" maxlength="600" data-beat-notes="${i}">${b.notes}</textarea>
        </span>
        <span class="col beat-tools">
          <button class="icon-btn small" type="button" data-beat-move="${i}" data-step="-1" aria-label="Move chapter ${i + 1} up" ${i === 0 ? "disabled" : ""}>${icon("chevronUp", 18)}</button>
          <button class="icon-btn small" type="button" data-beat-move="${i}" data-step="1" aria-label="Move chapter ${i + 1} down" ${i === state.beats.length - 1 ? "disabled" : ""}>${icon("chevronDown", 18)}</button>
          <button class="icon-btn small" type="button" data-beat-remove="${i}" aria-label="Remove chapter ${i + 1}" ${state.beats.length <= MIN_BEATS ? "disabled" : ""}>${icon("trash", 16)}</button>
        </span>
      </li>`)}`);
    $("[data-add-beat]").hidden = state.beats.length >= MAX_BEATS;
  };
  const cost = () => session.cost(state.minutes) + (state.music === "generate" ? session.options.music?.credits || 0 : 0);
  const renderCost = () => {
    const c = cost();
    $("[data-cost]").textContent = `Uses ${c} credit${c === 1 ? "" : "s"} · you have ${session.credits}`;
    $("[data-cost]").classList.toggle("short", session.credits < c);
    $("[data-produce]").disabled = session.credits < c;
  };
  const renderLengths = () => {
    setHtml($("[data-lengths]"), html`${LENGTHS.map((m) => html`
      ${lengthChip(m, state.minutes === m)}`)}`);
    renderCost();
  };
  const renderMusic = () => {
    setHtml($("[data-music]"), musicCards(state.music, { mine: state.mine, generate: true, intro: null }));
    renderCost();
  };

  root.addEventListener("input", (e) => {
    const t = e.target;
    if (t.dataset.beatTitle !== undefined) state.beats[Number(t.dataset.beatTitle)].title = t.value;
    if (t.dataset.beatNotes !== undefined) state.beats[Number(t.dataset.beatNotes)].notes = t.value;
    if (t.id === "rv-title") state.title = t.value;
  });
  root.addEventListener("change", (e) => {
    const box = e.target.closest("[data-finding]");
    if (!box) return;
    const fid = Number(box.dataset.finding);
    if (box.checked) state.excluded.delete(fid); else state.excluded.add(fid);
  });
  root.addEventListener("click", async (e) => {
    const t = e.target;
    const move = t.closest("[data-beat-move]");
    if (move) {
      const from = Number(move.dataset.beatMove);
      const [b] = state.beats.splice(from, 1);
      state.beats.splice(from + Number(move.dataset.step), 0, b);
      renderBeats();
    } else if (t.closest("[data-beat-remove]")) {
      state.beats.splice(Number(t.closest("[data-beat-remove]").dataset.beatRemove), 1);
      renderBeats();
    } else if (t.closest("[data-add-beat]")) {
      state.beats.push({ title: "", notes: "" });
      renderBeats();
      root.querySelector(`#beat-t-${state.beats.length - 1}`)?.focus();
    } else if (t.closest("[data-min]")) {
      state.minutes = Number(t.closest("[data-min]").dataset.min);
      renderLengths();
    } else if (t.closest("[data-music]")) {
      state.music = t.closest("[data-music]").dataset.music;
      renderMusic();
    } else if (t.closest("[data-delete]")) {
      deletePod(s.id);
    } else if (t.closest("[data-produce]")) {
      produce(t.closest("[data-produce]"));
    }
  });

  async function produce(btn) {
    const error = $("[data-error]");
    error.textContent = "";
    const beats = state.beats.map((b) => ({ title: b.title.trim(), notes: b.notes.trim() }));
    if (beats.some((b) => !b.title)) return (error.textContent = "Give every chapter a title, or remove it.");
    btn.disabled = true;
    try {
      await api.produceMemo(s.id, {
        title: state.title.trim() || null, beats, excluded_findings: [...state.excluded],
        minutes: state.minutes, music: state.music,
      });
      session.refresh();
      reload();
    } catch (err) {
      error.textContent = err.message;
      btn.disabled = false;
    }
  }

  renderFindings();
  renderBeats();
  renderLengths();
  renderMusic();
  api.myMusic().then((r) => { state.mine = r.music; renderMusic(); }).catch(() => {});
  return null;
}
