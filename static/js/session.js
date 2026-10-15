// Who's using the app: options from the server, and the signed-in user (or null).
import { api } from "./api.js";

export const session = {
  options: null,
  me: null,          // { uid, name, email, photo, credits, admin } or null
  incoming: 0,       // connection requests waiting for you
  _subs: new Set(),

  async load() {
    this.options = await api.options();
    this.me = this.options.me;
    if (this.me) await this.refresh();
    return this.options;
  },

  /** Re-read the account (credits change when you go live, refunds, grants). */
  async refresh() {
    try {
      const r = await api.me();
      this.me = r.me;
      this.incoming = r.incoming;
    } catch (err) {
      if (err.status === 401) this.me = null;
    }
    this.changed();
    return this.me;
  },

  on(fn) {
    this._subs.add(fn);
    return () => this._subs.delete(fn);
  },

  changed() {
    for (const fn of this._subs) fn();
  },

  get signedIn() { return Boolean(this.me); },
  get admin() { return Boolean(this.me?.admin); },
  get credits() { return this.me?.credits ?? 0; },
  get creator() { return this.signedIn; },

  /** What a pod costs; Research on (the hosts look things up live) costs more per minute. */
  cost(minutes, research = false) {
    const rate = (this.options?.credits_per_minute ?? 1) + (research ? this.options?.research_credits_per_minute ?? 0 : 0);
    return Math.max(1, Math.ceil(minutes * rate));
  },

  preset(id) {
    return this.options?.presets.find((p) => p.id === id) || null;
  },

  template(id) {
    return this.options?.templates.find((t) => t.id === id) || null;
  },

  /** Voices of one gender ("" = all). */
  voices(gender = "") {
    return (this.options?.voices || []).filter((v) => !gender || v.gender === gender);
  },

  voiceGender(name) {
    return this.options?.voices.find((v) => v.name === name)?.gender || "";
  },

  personaPreset(id) {
    return this.options?.persona_presets?.find((p) => p.id === id) || null;
  },

  hostColor(i) {
    return this.options?.host_colors?.[i] || { avatar: "#30284F", label: "#C3BCDA" };
  },

  async signOut() {
    await api.signOut().catch(() => {});
    if (this.options?.auth?.mode === "firebase") {
      const fb = await import("./firebase.js");
      await fb.signOutFirebase(this.options.auth.firebase).catch(() => {});
    }
    this.me = null;
    this.incoming = 0;
    this.changed();
  },
};

export function loginPath(next = location.pathname + location.search) {
  return `/login?next=${encodeURIComponent(next)}`;
}
