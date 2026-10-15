// UI plumbing: regions that re-render only when their content changes, bottom
// sheets, and click delegation through data-action attributes.
import { html, setHtml } from "./html.js";
import { icon } from "./icons.js";

/** Named [data-r] slots inside a root, each re-rendered only when its key changes. */
export class Regions {
  constructor(root) {
    this.root = root;
    this.keys = new Map();
    this.els = new Map();
  }

  el(name) {
    let el = this.els.get(name);
    if (!el || !el.isConnected) {
      el = this.root.querySelector(`[data-r="${name}"]`);
      this.els.set(name, el);
    }
    return el;
  }

  set(name, key, render) {
    const el = this.el(name);
    if (!el) return;
    if (this.keys.get(name) === key) return;
    this.keys.set(name, key);
    setHtml(el, render());
  }

  reset() {
    this.keys.clear();
  }
}

/** Clicks on [data-action] call handlers[action](el, event). */
export function bindActions(root, handlers) {
  const onClick = (e) => {
    const el = e.target.closest("[data-action]");
    if (!el || !root.contains(el)) return;
    const fn = handlers[el.dataset.action];
    if (!fn) return;
    e.preventDefault();
    fn(el, e);
  };
  root.addEventListener("click", onClick);
  return () => root.removeEventListener("click", onClick);
}

let openCount = 0;

/** A bottom sheet on phones, a centered dialog on desktop. */
export function openSheet({ title, body, className = "", onClose, wide = false }) {
  const overlay = document.createElement("div");
  overlay.className = "sheet-overlay";
  const id = `sheet-${Date.now()}`;
  setHtml(overlay, html`
    <div class="sheet ${className} ${wide ? "wide" : ""}" role="dialog" aria-modal="true" aria-labelledby="${id}">
      <span class="grabber" aria-hidden="true"></span>
      <header class="sheet-head">
        <h2 id="${id}">${title}</h2>
        <button class="icon-btn round" type="button" data-close aria-label="Close">${icon("close", 18, 2.2)}</button>
      </header>
      <div class="sheet-body"></div>
    </div>`);
  const sheet = overlay.querySelector(".sheet");
  const content = overlay.querySelector(".sheet-body");
  if (body instanceof Node) content.append(body);
  else setHtml(content, body);
  const previous = document.activeElement;
  let closed = false;

  const close = () => {
    if (closed) return;
    closed = true;
    document.removeEventListener("keydown", onKey);
    overlay.classList.add("closing");
    setTimeout(() => overlay.remove(), 160);
    if (--openCount === 0) document.body.classList.remove("sheet-open");
    previous?.focus?.();
    onClose?.();
  };
  const onKey = (e) => {
    if (e.key === "Escape") close();
  };
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay || e.target.closest("[data-close]")) close();
  });
  document.addEventListener("keydown", onKey);
  document.body.append(overlay);
  document.body.classList.add("sheet-open");
  openCount++;
  requestAnimationFrame(() => {
    overlay.classList.add("open");
    (sheet.querySelector("[autofocus]") || sheet.querySelector("input, textarea, button:not([data-close])"))?.focus();
  });
  return { el: sheet, body: content, close };
}

export function confirmSheet({ title, message, confirm, danger = false }) {
  return new Promise((resolve) => {
    const div = document.createElement("div");
    div.className = "stack";
    setHtml(div, html`
      <p class="muted">${message}</p>
      <div class="row gap">
        <button class="btn quiet grow" type="button" data-close>Cancel</button>
        <button class="btn ${danger ? "danger" : "primary"} grow" type="button" data-yes>${confirm}</button>
      </div>`);
    let answered = false;
    const sheet = openSheet({ title, body: div, onClose: () => { if (!answered) resolve(false); } });
    div.querySelector("[data-yes]").addEventListener("click", () => {
      answered = true;
      sheet.close();
      resolve(true);
    });
  });
}
