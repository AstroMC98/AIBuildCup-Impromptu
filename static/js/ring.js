// The studio clock: 60 second-dots with the quarter marks brighter. It is the app's one
// loader and progress shape: a chase while something has no number yet, a fill for
// seconds or progress, arcs for chapters, one dot per minute for a pod's length.
import { raw } from "./html.js";

/**
 * ring({ lit, chase, r, dot, cls, label })
 *   lit:   (i) => boolean, which dots start lit
 *   chase: an indeterminate comet (pure CSS; still under reduced motion)
 */
export function ring({ lit = null, chase = false, r = 44, dot = 2.4, cls = "", label = "" } = {}) {
  let dots = "";
  for (let i = 0; i < 60; i++) {
    const a = (i / 60) * Math.PI * 2 - Math.PI / 2;
    const q = i % 15 === 0;
    const c = [q ? "q" : "", lit && lit(i) ? "lit" : ""].filter(Boolean).join(" ");
    dots += `<circle${c ? ` class="${c}"` : ""}${chase ? ` style="--i:${i}"` : ""} cx="${(50 + r * Math.cos(a)).toFixed(2)}" cy="${(50 + r * Math.sin(a)).toFixed(2)}" r="${q ? dot * 1.35 : dot}"></circle>`;
  }
  const aria = label ? `role="img" aria-label="${label.replace(/"/g, "&quot;")}"` : 'aria-hidden="true"';
  return raw(`<svg class="ring ${chase ? "chase" : ""} ${cls}" viewBox="0 0 100 100" ${aria}>${dots}</svg>`);
}

/** Light dots 0..n-1 of a ring already in the page (n may be fractional). */
export function fillRing(svg, n) {
  if (!svg) return;
  const k = Math.round(n);
  if (svg._lit === k) return;
  svg._lit = k;
  svg.querySelectorAll("circle").forEach((c, i) => c.classList.toggle("lit", i < k));
}

/** Seconds on the clock: the dots up to the second hand. */
export const secondsLit = (seconds) => Math.floor(seconds % 60) + 1;
