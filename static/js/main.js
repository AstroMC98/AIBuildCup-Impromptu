// Boot: learn who we are from the server, then route.
import { html, setHtml } from "./html.js";
import { start } from "./router.js";
import { session } from "./session.js";
import { adminView } from "./views/admin.js";
import { albumsView, albumView } from "./views/albums.js";
import { connectionsView, inviteView } from "./views/connections.js";
import { createView } from "./views/create.js";
import { creatorView } from "./views/creator.js";
import { libraryView } from "./views/library.js";
import { loginView } from "./views/login.js";
import { savedView } from "./views/saved.js";
import { showView } from "./views/show.js";
import { showStatsView, statsView } from "./views/stats.js";

async function boot() {
  try {
    await session.load();
  } catch {
    setHtml(document.getElementById("app"), html`
      <div class="splash"><p class="splash-title">Off air</p>
      <p class="muted">The studio isn't responding. Is the server running?</p></div>`);
    return;
  }
  start([
    [/^\/$/, libraryView],
    [/^\/discover$/, libraryView],
    [/^\/following$/, libraryView],
    [/^\/albums$/, albumsView],
    [/^\/album\/([\w-]+)$/, albumView],
    [/^\/creator\/([\w-]+)$/, creatorView],
    [/^\/stats$/, statsView],
    [/^\/show\/([\w-]+)\/stats$/, showStatsView],
    [/^\/new$/, createView],
    [/^\/login$/, loginView],
    [/^\/personas$/, savedView],
    [/^\/styles$/, savedView],
    [/^\/connections$/, connectionsView],
    [/^\/invite\/([\w-]+)$/, inviteView],
    [/^\/admin$/, adminView],
    [/^\/show\/([\w-]+)$/, showView],
  ]);
}

boot();
