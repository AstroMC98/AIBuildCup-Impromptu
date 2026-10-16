// Thin wrappers over the HTTP API (see app/main.py for the full list).

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

function detail(data) {
  if (!data) return "";
  if (typeof data.detail === "string") return data.detail;
  if (Array.isArray(data.detail)) return data.detail.map((d) => d.msg).join("; ");
  return "";
}

async function call(method, url, { json, form } = {}) {
  const opts = { method, credentials: "same-origin", headers: {} };
  if (form) opts.body = form;
  else if (json !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(json);
  }
  let res;
  try {
    res = await fetch(url, opts);
  } catch {
    throw new ApiError("Can't reach the studio. Check your connection.", 0);
  }
  let data = null;
  try { data = await res.json(); } catch { /* not JSON */ }
  if (!res.ok) throw new ApiError(detail(data) || `Something went wrong (${res.status}).`, res.status);
  return data;
}

const id = encodeURIComponent;

export const api = {
  options: () => call("GET", "/api/options"),
  // accounts
  signInFirebase: (id_token) => call("POST", "/api/auth/firebase", { json: { id_token } }),
  signInDev: (email, name) => call("POST", "/api/auth/dev", { json: { email, name } }),
  signOut: () => call("DELETE", "/api/auth/session"),
  me: () => call("GET", "/api/me"),
  ledger: () => call("GET", "/api/me/ledger"),
  resetCredits: () => call("POST", "/api/me/credits/reset"),
  // personas and styles
  list: (kind) => call("GET", `/api/${kind}`),
  create: (kind, item) => call("POST", `/api/${kind}`, { json: item }),
  update: (kind, itemId, item) => call("PUT", `/api/${kind}/${id(itemId)}`, { json: item }),
  remove: (kind, itemId) => call("DELETE", `/api/${kind}/${id(itemId)}`),
  // connections
  connections: () => call("GET", "/api/connections"),
  connect: (target) => call("POST", "/api/connections", { json: target }),
  accept: (uid) => call("POST", `/api/connections/${id(uid)}/accept`),
  disconnect: (uid) => call("DELETE", `/api/connections/${id(uid)}`),
  userCard: (uid) => call("GET", `/api/users/${id(uid)}`),
  // pods
  extract: (form) => call("POST", "/api/extract", { form }),
  createShow: (body) => call("POST", "/api/shows", { json: body }),
  listShows: (scope = "mine") => call("GET", `/api/shows?scope=${id(scope)}`),
  getShow: (showId) => call("GET", `/api/shows/${id(showId)}`),
  shareShow: (showId, visibility, shared_with = []) =>
    call("PATCH", `/api/shows/${id(showId)}`, { json: { visibility, shared_with } }),
  startDraft: (showId) => call("POST", `/api/shows/${id(showId)}/start`),
  deleteShow: (showId) => call("DELETE", `/api/shows/${id(showId)}`),
  addContext: (showId, form) => call("POST", `/api/shows/${id(showId)}/context`, { form }),
  transcribe: (showId, blob) => {
    const form = new FormData();
    const ext = { "audio/ogg": "ogg", "audio/mp4": "m4a" }[blob.type] || "webm";
    form.append("audio", blob, `question.${ext}`);
    return call("POST", `/api/shows/${id(showId)}/transcribe`, { form });
  },
  audioUrl: (showId, download = false) => `/api/shows/${id(showId)}/audio${download ? "?download=1" : ""}`,
  rawAudioUrl: (showId) => `/api/shows/${id(showId)}/audio?raw=1&download=1`,   // a co-hosted call, unedited
  // co-hosting
  createCohost: (body) => call("POST", "/api/cohost", { json: body }),
  cohostNotes: (notes) => call("POST", "/api/cohost/notes", { json: { notes } }),   // long notes, briefed ahead
  organize: (showId, body) => call("PATCH", `/api/shows/${id(showId)}/organize`, { json: body }),
  // voice memos, your music and your show intro
  createMemo: (form) => call("POST", "/api/memos", { form }),
  produceMemo: (showId, body = {}) => call("POST", `/api/memos/${id(showId)}/produce`, { json: body }),
  memoAudioUrl: (showId) => `/api/memos/${id(showId)}/audio`,
  myMusic: () => call("GET", "/api/me/music"),
  showIntro: () => call("GET", "/api/me/intro"),
  saveShowIntro: (body) => call("PUT", "/api/me/intro", { json: body }),
  deleteShowIntro: () => call("DELETE", "/api/me/intro"),
  showStats: (showId) => call("GET", `/api/shows/${id(showId)}/stats`),
  myStats: () => call("GET", "/api/me/stats"),
  // replay progress; on page exit it goes as a beacon, which survives the unload
  listen: (showId, body, { beacon = false } = {}) => {
    const url = `/api/shows/${id(showId)}/listen`;
    if (beacon && navigator.sendBeacon) {
      return navigator.sendBeacon(url, new Blob([JSON.stringify(body)], { type: "application/json" }));
    }
    return call("POST", url, { json: body }).catch(() => {});
  },
  // albums, creators, subscriptions
  albums: () => call("GET", "/api/albums"),
  createAlbum: (body) => call("POST", "/api/albums", { json: body }),
  album: (albumId) => call("GET", `/api/albums/${id(albumId)}`),
  updateAlbum: (albumId, body) => call("PUT", `/api/albums/${id(albumId)}`, { json: body }),
  deleteAlbum: (albumId) => call("DELETE", `/api/albums/${id(albumId)}`),
  creator: (uid) => call("GET", `/api/creators/${id(uid)}`),
  subscriptions: () => call("GET", "/api/subscriptions"),
  subscribe: (kind, targetId) => call("POST", "/api/subscriptions", { json: { kind, id: targetId } }),
  unsubscribe: (kind, targetId) => call("DELETE", `/api/subscriptions/${id(kind)}/${id(targetId)}`),
  feed: () => call("GET", "/api/feed"),
  feedSeen: () => call("POST", "/api/feed/seen"),
  // personal briefings and the watchlist they're made from
  briefings: () => call("GET", "/api/briefings"),
  makeBriefing: (body) => call("POST", "/api/briefings", { json: body }),
  setSchedule: (body) => call("PUT", "/api/briefings/schedule", { json: body }),
  createBriefingTemplate: (body) => call("POST", "/api/briefing-templates", { json: body }),
  updateBriefingTemplate: (tid, body) => call("PUT", `/api/briefing-templates/${id(tid)}`, { json: body }),
  deleteBriefingTemplate: (tid) => call("DELETE", `/api/briefing-templates/${id(tid)}`),
  watches: () => call("GET", "/api/watches"),
  addWatch: (body) => call("POST", "/api/watches", { json: body }),
  updateWatch: (wid, body) => call("PATCH", `/api/watches/${id(wid)}`, { json: body }),
  removeWatch: (wid) => call("DELETE", `/api/watches/${id(wid)}`),
  checkWatch: (wid) => call("POST", `/api/watches/${id(wid)}/check`),
  // admin
  adminUsers: (q = "") => call("GET", `/api/admin/users?q=${id(q)}`),
  grant: (uid, amount, note) => call("POST", `/api/admin/users/${id(uid)}/credits`, { json: { amount, note } }),
};
