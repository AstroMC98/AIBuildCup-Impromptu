// /intro — your show intro. Every pod opens with a few seconds of theme music and
// the hosts' welcome; give your show a name (and a line of your own, and a theme)
// and every pod you make welcomes listeners to it.
import { api } from "../api.js";
import { html, setHtml } from "../html.js";
import { navigate } from "../router.js";
import { loginPath, session } from "../session.js";
import { applyAccent } from "../theme.js";
import { confirmSheet } from "../ui.js";
import { toast } from "../util.js";
import { appHeader, bindHeader } from "./library.js";

export async function introView(root) {
  if (!session.signedIn) {
    navigate(loginPath("/intro"), { replace: true });
    return null;
  }
  applyAccent(document.documentElement, "#6A4FB0");
  const [{ intro }, { music: mine }] = await Promise.all([
    api.showIntro().catch(() => ({ intro: null })),
    api.myMusic().catch(() => ({ music: [] })),
  ]);
  const state = {
    saved: Boolean(intro),
    music: intro?.music || "auto",
    enabled: intro?.enabled ?? true,
  };

  const el = document.createElement("div");
  el.className = "page-wrap";
  setHtml(el, html`
    ${appHeader({ search: false })}
    <main class="page">
      <div class="page-title"><div class="col">
        <h1>Your show intro</h1>
        <p class="muted">Every pod opens with a few seconds of theme music and the hosts' welcome. Name your show, and in
          every pod you make they'll welcome listeners to it.</p>
      </div></div>
      <form class="card stack intro-form" data-form>
        <label class="field-label" for="in-name">Show name</label>
        <input id="in-name" class="field" maxlength="60" required placeholder="Walk &amp; Talk" value="${intro?.show_name || ""}">
        <label class="field-label" for="in-line">Your intro line <span class="faint small">optional, said in your words</span></label>
        <input id="in-line" class="field" maxlength="200" placeholder="the show where big ideas start on a walk" value="${intro?.intro_line || ""}">
        <p class="intro-preview" data-preview aria-live="polite"></p>
        <p class="field-label">Theme music</p>
        <div class="radio-cards" role="radiogroup" aria-label="Theme music" data-music></div>
        <div class="switch-row">
          <span id="in-on" class="col"><span>Use it on new pods</span>
            <span class="muted small">You can still switch it off for any one pod.</span></span>
          <button type="button" role="switch" class="switch" aria-labelledby="in-on" data-enabled></button>
        </div>
        <p class="error" role="alert" data-error></p>
        <button class="btn white big block" type="submit">Save</button>
        ${state.saved ? html`<button class="btn link danger-text" type="button" data-remove>Remove my show intro</button>` : ""}
      </form>
    </main>`);
  root.append(el);
  const offHead = bindHeader(el);
  const $ = (s) => el.querySelector(s);

  const card = (value, title, desc) => html`
    <button type="button" role="radio" class="radio-card" data-music="${value}" aria-checked="${state.music === value}">
      <span class="radio-dot" aria-hidden="true"></span>
      <span class="radio-text"><span class="radio-title">${title}</span><span class="radio-desc">${desc}</span></span>
    </button>`;
  const renderMusic = () => setHtml($("[data-music]"), html`
    ${card("auto", "Matched to each pod", "A theme from the studio library that suits each pod's vibe.")}
    ${(session.options.music?.library || []).map((p) => card(p.id, p.label, "From the studio library, on every pod."))}
    ${mine.map((m) => card(m.id, m.label, "Made for you. On every pod."))}
    ${card("none", "No music", "Just the hosts' welcome.")}`);
  const renderPreview = () => {
    const name = $("#in-name").value.trim();
    const line = $("#in-line").value.trim();
    $("[data-preview]").textContent = name
      ? `The hosts open with something like: “Welcome to ${name}${line ? `, ${line}` : ""}.”`
      : "";
  };
  const renderEnabled = () => $("[data-enabled]").setAttribute("aria-checked", String(state.enabled));

  el.addEventListener("input", renderPreview);
  el.addEventListener("click", async (e) => {
    const m = e.target.closest("[data-music]");
    if (m) {
      state.music = m.dataset.music;
      renderMusic();
    } else if (e.target.closest("[data-enabled]")) {
      state.enabled = !state.enabled;
      renderEnabled();
    } else if (e.target.closest("[data-remove]")) {
      const ok = await confirmSheet({ title: "Remove your show intro?", message: "Pods you make next get the automatic intro. Pods already made keep theirs.", confirm: "Remove", danger: true });
      if (!ok) return;
      await api.deleteShowIntro().catch((err) => toast(err.message));
      toast("Removed. New pods get the automatic intro.");
      navigate("/intro", { replace: true });
    }
  });
  $("[data-form]").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("[data-error]").textContent = "";
    try {
      await api.saveShowIntro({
        show_name: $("#in-name").value.trim(), intro_line: $("#in-line").value.trim(),
        music: state.music, enabled: state.enabled,
      });
      toast(state.enabled ? "Saved. Your next pods open with it." : "Saved. It's off for now.");
      if (!state.saved) navigate("/intro", { replace: true });
    } catch (err) {
      $("[data-error]").textContent = err.message;
    }
  });

  renderMusic();
  renderPreview();
  renderEnabled();
  return { unmount() { offHead(); } };
}
