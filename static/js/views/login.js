// /login — Firebase (Google, or email + password), or dev sign-in locally.
import { api } from "../api.js";
import { html, raw, setHtml } from "../html.js";
import { icon, logo } from "../icons.js";
import { ring } from "../ring.js";
import { navigate } from "../router.js";
import { session } from "../session.js";
import { setWorld } from "../theme.js";
import { toast } from "../util.js";

const GOOGLE_G = `<svg width="18" height="18" viewBox="0 0 48 48" aria-hidden="true"><path fill="#FFC107" d="M43.6 20.5H42V20H24v8h11.3C33.7 32.7 29.2 36 24 36c-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 12.9 4 4 12.9 4 24s8.9 20 20 20 20-8.9 20-20c0-1.3-.1-2.4-.4-3.5z"/><path fill="#FF3D00" d="M6.3 14.7l6.6 4.8C14.7 15.1 19 12 24 12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 16.3 4 9.7 8.3 6.3 14.7z"/><path fill="#4CAF50" d="M24 44c5.2 0 9.9-2 13.4-5.2l-6.2-5.2C29.2 35.1 26.7 36 24 36c-5.2 0-9.6-3.3-11.3-8l-6.5 5C9.5 39.6 16.2 44 24 44z"/><path fill="#1976D2" d="M43.6 20.5H42V20H24v8h11.3c-.8 2.2-2.2 4.2-4.1 5.6l6.2 5.2C37 39.2 44 34 44 24c0-1.3-.1-2.4-.4-3.5z"/></svg>`;

export async function loginView(root, { query }) {
  const next = safeNext(query.get("next"));
  if (session.signedIn) {
    navigate(next, { replace: true });
    return null;
  }
  const mode = session.options.auth.mode;
  let creating = query.get("create") === "1";
  setWorld("stage");   // signing in happens on the stage, lit tungsten

  const render = () => setHtml(root, html`
    <main class="login stage" style="--gel:#FFB257">
      <span class="login-ring">${ring({ lit: (i) => i < 22, r: 46, dot: 0.9 })}</span>
      <div class="login-card">
        <a class="login-mark" href="/" data-link aria-label="Impromptu home">${logo()}</a>
        <p class="login-lede">Live podcasts about anything. Call in while they're on air.</p>
        <h1>${creating ? "Create your account" : "Sign in"}</h1>
        <p class="muted">Listen to pods, call in, connect with friends${creating ? ", and create your own with credits" : ""}.</p>
        ${mode === "firebase" ? html`
          <button class="btn white big block" type="button" data-google>${raw(GOOGLE_G)}Continue with Google</button>
          <div class="or"><span>or</span></div>
          <form class="stack" data-email>
            ${creating ? html`
              <label class="field-label" for="l-name">Your name</label>
              <input id="l-name" class="field" autocomplete="name" maxlength="60" required>` : ""}
            <label class="field-label" for="l-email">Email</label>
            <input id="l-email" class="field" type="email" autocomplete="email" required>
            <label class="field-label" for="l-pass">Password</label>
            <input id="l-pass" class="field" type="password" autocomplete="${creating ? "new-password" : "current-password"}" minlength="6" required>
            <p class="error" role="alert" data-error></p>
            <p class="ok" role="status" data-ok></p>
            <button class="btn quiet big block" type="submit">${creating ? "Create account" : "Sign in with email"}</button>
          </form>
          <div class="login-links">
            <button class="btn link" type="button" data-toggle>${creating ? "Already have an account? Sign in" : "New here? Create an account"}</button>
            ${creating ? "" : html`<button class="btn link" type="button" data-reset>Forgot password?</button>`}
          </div>` : html`
          <form class="stack" data-dev>
            <p class="dev-note">${icon("user", 16)}Dev sign-in: any name and email works. Local use only.</p>
            <label class="field-label" for="d-name">Your name</label>
            <input id="d-name" class="field" autocomplete="name" maxlength="60" required>
            <label class="field-label" for="d-email">Email</label>
            <input id="d-email" class="field" type="email" autocomplete="email" required>
            <p class="error" role="alert" data-error></p>
            <button class="btn white big block" type="submit">Sign in</button>
          </form>`}
        <a class="btn link" href="/discover" data-link>Just browsing? Listen to public pods</a>
      </div>
    </main>`);

  const err = (msg) => { const e = root.querySelector("[data-error]"); if (e) e.textContent = msg || ""; };
  const ok = (msg) => { const e = root.querySelector("[data-ok]"); if (e) e.textContent = msg || ""; };

  const finish = async (signIn) => {
    try {
      await signIn();
      await session.load();
      toast(`Welcome, ${session.me?.name || "friend"}.`);
      navigate(next, { replace: true });
    } catch (e) {
      err(e.message);
    }
  };

  const bind = () => {
    const fbConfig = session.options.auth.firebase;
    root.querySelector("[data-google]")?.addEventListener("click", async (e) => {
      e.currentTarget.disabled = true;
      const fb = await import("../firebase.js");
      await finish(async () => {
        try {
          const token = await fb.signInGoogle(fbConfig);
          await api.signInFirebase(token);
        } catch (ex) {
          throw new Error(ex.status ? ex.message : fb.friendly(ex));
        }
      });
      e.currentTarget && (e.currentTarget.disabled = false);
    });
    root.querySelector("[data-email]")?.addEventListener("submit", async (e) => {
      e.preventDefault();
      err("");
      ok("");
      const fb = await import("../firebase.js");
      const email = root.querySelector("#l-email").value.trim();
      const pass = root.querySelector("#l-pass").value;
      const btn = e.currentTarget.querySelector("button[type=submit]");
      btn.disabled = true;
      if (creating) {
        try {
          await fb.createAccount(fbConfig, root.querySelector("#l-name").value.trim(), email, pass);
          creating = false;
          render();
          bind();
          root.querySelector("#l-email").value = email;
          ok("Account created. Check your inbox for a verification link, then sign in.");
        } catch (ex) {
          err(fb.friendly(ex));
          btn.disabled = false;
        }
        return;
      }
      await finish(async () => {
        try {
          const token = await fb.signInEmail(fbConfig, email, pass);
          await api.signInFirebase(token);
        } catch (ex) {
          throw new Error(ex.status ? ex.message : fb.friendly(ex));
        }
      });
      btn.disabled = false;
    });
    root.querySelector("[data-toggle]")?.addEventListener("click", () => {
      creating = !creating;
      render();
      bind();
    });
    root.querySelector("[data-reset]")?.addEventListener("click", async () => {
      const email = root.querySelector("#l-email").value.trim();
      if (!email) return err("Type your email above, then tap Forgot password.");
      const fb = await import("../firebase.js");
      try {
        await fb.resetPassword(fbConfig, email);
        ok("If there's an account for that email, a reset link is on its way.");
      } catch (ex) {
        err(fb.friendly(ex));
      }
    });
    root.querySelector("[data-dev]")?.addEventListener("submit", async (e) => {
      e.preventDefault();
      const name = root.querySelector("#d-name").value.trim();
      const email = root.querySelector("#d-email").value.trim();
      e.currentTarget.querySelector("button").disabled = true;
      await finish(() => api.signInDev(email, name));
      const b = root.querySelector("[data-dev] button");
      if (b) b.disabled = false;
    });
  };

  render();
  bind();
  root.querySelector("input")?.focus();
  return null;
}

// Only ever bounce back to a page on this site.
function safeNext(next) {
  return next && next.startsWith("/") && !next.startsWith("//") ? next : "/";
}
