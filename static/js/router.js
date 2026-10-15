// History-API routing. Views are async functions (root, route) -> { unmount }.
// pushState (not page loads) keeps the AudioContext unlocked by "Go live" alive
// on the way from /new to /show/{id}.
//
// Every route change is a view transition (motion.css): into an on-air screen the stage
// floods out from where you tapped ("house lights down"), back out it shrinks away, and
// other pages slide forward or back. The swap waits at most 250 ms for the new view, so
// a slow fetch lands on that view's own loader instead of freezing the old page.
import { setWorld } from "./theme.js";
import { reduced } from "./motion.js";

let routes = [];
let current = null;
let token = 0;
let lastPath = null;
let popping = false;

const STAGE = [/^\/show\/[\w-]+$/, /^\/cohost\/[\w-]+$/];
const isStage = (path) => STAGE.some((re) => re.test(path || ""));

export function navigate(path, { replace = false } = {}) {
  if (replace) history.replaceState({}, "", path);
  else history.pushState({}, "", path);
  dispatch();
}

export function start(table) {
  routes = table;
  addEventListener("popstate", () => { popping = true; dispatch(); });
  // Remember where people tap, so the stage can flood out from that point.
  addEventListener("pointerdown", (e) => {
    const s = document.documentElement.style;
    s.setProperty("--vt-x", `${e.clientX}px`);
    s.setProperty("--vt-y", `${e.clientY}px`);
  }, { capture: true, passive: true });
  document.addEventListener("click", (e) => {
    const a = e.target.closest("a[data-link]");
    if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || a.target) return;
    e.preventDefault();
    navigate(a.getAttribute("href"));
  });
  dispatch();
}

function kindOf(from, to, back) {
  if (isStage(to) && !isStage(from)) return "to-stage";
  if (isStage(from) && !isStage(to)) return "to-house";
  return back ? "back" : "forward";
}

async function dispatch() {
  const mine = ++token;
  const url = new URL(location.href);
  const root = document.getElementById("app");
  const back = popping;
  popping = false;
  const match = routes.find(([pattern]) => pattern.test(url.pathname));
  if (!match) return navigate("/", { replace: true });
  const [pattern, view] = match;
  const params = url.pathname.match(pattern).slice(1);

  let pending = null;
  const swap = async () => {
    try { current?.unmount?.(); } catch (err) { console.error(err); }
    current = null;
    root.replaceChildren();
    window.scrollTo(0, 0);
    setWorld(isStage(url.pathname) ? "stage" : "house");
    pending = view(root, { params, query: url.searchParams, url });
    await Promise.race([pending, new Promise((r) => setTimeout(r, 250))]);
  };

  const from = lastPath;
  lastPath = url.pathname;
  const html = document.documentElement;
  if (from !== null && from !== url.pathname && document.startViewTransition && !reduced()) {
    html.dataset.vt = kindOf(from, url.pathname, back);
    const vt = document.startViewTransition(swap);
    vt.finished.finally(() => { if (mine === token) delete html.dataset.vt; });
    await vt.updateCallbackDone.catch(() => {});
  } else {
    await swap();
  }
  const mounted = await pending;
  if (mine !== token) mounted?.unmount?.();  // navigated away while loading
  else current = mounted;
}
