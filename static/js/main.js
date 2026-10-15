// Boot: learn who we are from the server, then route. The boot ident in index.html shows
// only if this takes longer than 250 ms, and fades as the first screen arrives.
import { html, setHtml } from "./html.js";
import { ring } from "./ring.js";
import { start } from "./router.js";
import { session } from "./session.js";
import { adminView } from "./views/admin.js";
import { albumsView, albumView } from "./views/albums.js";
import { briefingsView } from "./views/briefings.js";
import { cohostCallView } from "./views/cohost-call.js";
import { cohostSetupView } from "./views/cohost-setup.js";
import { connectionsView, inviteView } from "./views/connections.js";
import { createView } from "./views/create.js";
import { creatorView } from "./views/creator.js";
import { introView } from "./views/intro.js";
import { libraryPageView, libraryView, youView } from "./views/library.js";
import { loginView } from "./views/login.js";
import { memoView } from "./views/memo.js";
import { savedView } from "./views/saved.js";
import { showView } from "./views/show.js";
import { showStatsView, statsView } from "./views/stats.js";

function dismissBoot() {
  const boot = document.getElementById("boot");
  if (!boot) return;
  boot.classList.add("gone");
  setTimeout(() => boot.remove(), 320);
}

async function boot() {
  try {
    await session.load();
  } catch {
    document.documentElement.dataset.world = "stage";
    setHtml(document.getElementById("app"), html`
      <div class="splash stage">${ring({ lit: (i) => i % 15 === 0 })}<p class="splash-title">Off air</p>
      <p class="muted">The studio isn't responding. Is the server running?</p></div>`);
    dismissBoot();
    return;
  }
  start([
    [/^\/$/, libraryView],
    [/^\/discover$/, libraryView],
    [/^\/following$/, libraryView],
    [/^\/library$/, libraryPageView],
    [/^\/you$/, youView],
    [/^\/albums$/, albumsView],
    [/^\/briefings$/, briefingsView],
    [/^\/album\/([\w-]+)$/, albumView],
    [/^\/creator\/([\w-]+)$/, creatorView],
    [/^\/stats$/, statsView],
    [/^\/show\/([\w-]+)\/stats$/, showStatsView],
    [/^\/new$/, createView],
    [/^\/memo$/, memoView],
    [/^\/intro$/, introView],
    [/^\/cohost$/, cohostSetupView],
    [/^\/cohost\/([\w-]+)$/, cohostCallView],
    [/^\/login$/, loginView],
    [/^\/personas$/, savedView],
    [/^\/styles$/, savedView],
    [/^\/connections$/, connectionsView],
    [/^\/invite\/([\w-]+)$/, inviteView],
    [/^\/admin$/, adminView],
    [/^\/show\/([\w-]+)$/, showView],
  ]);
  requestAnimationFrame(dismissBoot);
}

boot();
