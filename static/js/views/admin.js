// /admin — grant demo credits. Admins are the emails in ADMIN_EMAILS.
import { api } from "../api.js";
import { waiting } from "../components.js";
import { html, setHtml } from "../html.js";
import { navigate } from "../router.js";
import { loginPath, session } from "../session.js";
import { applyAccent, callerColor, initial } from "../theme.js";
import { openSheet } from "../ui.js";
import { fmtAgo, toast } from "../util.js";
import { appHeader, bindHeader } from "./library.js";

export async function adminView(root) {
  if (!session.signedIn) {
    navigate(loginPath("/admin"), { replace: true });
    return null;
  }
  if (!session.admin) {
    navigate("/", { replace: true });
    return null;
  }
  applyAccent(document.documentElement, "#3A4250");
  let users = [];
  let timer = 0;
  const el = document.createElement("div");
  el.className = "page-wrap";
  setHtml(el, html`
    ${appHeader({ search: false })}
    <main class="page wide">
      <div class="page-title"><h1>Admin</h1>
        <p class="muted">Grant credits for the demo. One credit is about ${1 / session.options.credits_per_minute} minute of pod.</p></div>
      <label class="sr-only" for="a-q">Search users</label>
      <input id="a-q" class="field" type="search" placeholder="Search by name or email">
      <div data-users>${waiting()}</div>
    </main>`);
  root.append(el);
  const offHead = bindHeader(el);

  const render = () => setHtml(el.querySelector("[data-users]"), users.length ? html`
    <ul class="people admin-users">${users.map((u) => html`
      <li class="person">
        <span class="avatar" style="--av:${callerColor(u.name)};--size:40px">${initial(u.name)}</span>
        <span class="col grow"><strong>${u.name}</strong><span class="muted small">${u.email} · seen ${fmtAgo(u.last_seen)}</span></span>
        <span class="credit-pill">${u.credits ?? 0}</span>
        <button class="btn chip-btn" type="button" data-grant="${u.uid}">Credits</button>
      </li>`)}</ul>` : html`<p class="empty">No users match.</p>`);
  const load = async (q = "") => {
    users = (await api.adminUsers(q)).users;
    render();
  };

  el.querySelector("#a-q").addEventListener("input", (e) => {
    clearTimeout(timer);
    timer = setTimeout(() => load(e.target.value).catch((ex) => toast(ex.message)), 250);
  });
  el.addEventListener("click", (e) => {
    const b = e.target.closest("[data-grant]");
    if (!b) return;
    const u = users.find((x) => x.uid === b.dataset.grant);
    const form = document.createElement("form");
    form.className = "stack";
    setHtml(form, html`
      <p class="muted">${u.name} has <strong>${u.credits ?? 0}</strong> credits.</p>
      <div class="chips">${[10, 30, 60, 120].map((n) => html`<button type="button" class="chip" data-amt="${n}">+${n}</button>`)}</div>
      <label class="field-label" for="g-amt">Amount (negative to take back)</label>
      <input id="g-amt" class="field" type="number" step="1" value="30" required>
      <label class="field-label" for="g-note">Note</label>
      <input id="g-note" class="field" maxlength="200" placeholder="Demo credits">
      <p class="error" role="alert" data-error></p>
      <button class="btn white big block" type="submit">Apply</button>`);
    const sheet = openSheet({ title: `Credits for ${u.name}`, body: form });
    form.addEventListener("click", (ev) => {
      const amt = ev.target.closest("[data-amt]");
      if (amt) form.querySelector("#g-amt").value = amt.dataset.amt;
    });
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      try {
        const r = await api.grant(u.uid, Number(form.querySelector("#g-amt").value), form.querySelector("#g-note").value.trim());
        u.credits = r.credits;
        render();
        sheet.close();
        toast(`${u.name} now has ${r.credits} credits.`);
        if (u.uid === session.me.uid) session.refresh();
      } catch (ex) {
        form.querySelector("[data-error]").textContent = ex.message;
      }
    });
  });

  render();
  await load().catch((ex) => toast(ex.message));
  return { unmount() { offHead(); clearTimeout(timer); } };
}
