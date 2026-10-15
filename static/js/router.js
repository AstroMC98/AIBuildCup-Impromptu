// History-API routing. Views are async functions (root, route) -> { unmount }.
// pushState (not page loads) keeps the AudioContext unlocked by "Go live" alive
// on the way from /new to /show/{id}.

let routes = [];
let current = null;
let token = 0;

export function navigate(path, { replace = false } = {}) {
  if (replace) history.replaceState({}, "", path);
  else history.pushState({}, "", path);
  dispatch();
}

export function start(table) {
  routes = table;
  addEventListener("popstate", dispatch);
  document.addEventListener("click", (e) => {
    const a = e.target.closest("a[data-link]");
    if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || a.target) return;
    e.preventDefault();
    navigate(a.getAttribute("href"));
  });
  dispatch();
}

async function dispatch() {
  const mine = ++token;
  const url = new URL(location.href);
  const root = document.getElementById("app");
  for (const [pattern, view] of routes) {
    const m = url.pathname.match(pattern);
    if (!m) continue;
    try { current?.unmount?.(); } catch (err) { console.error(err); }
    current = null;
    root.replaceChildren();
    window.scrollTo(0, 0);
    const mounted = await view(root, { params: m.slice(1), query: url.searchParams, url });
    if (mine !== token) mounted?.unmount?.();  // navigated away while loading
    else current = mounted;
    return;
  }
  navigate("/", { replace: true });
}
