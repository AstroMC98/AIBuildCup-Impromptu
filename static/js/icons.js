// Line icons from the design board (24x24, round caps). icon("send", 20)
import { raw } from "./html.js";

const P = {
  file: ["M14 3H6v18h12V7z", "M14 3v4h4"],
  addFile: ["M14 3H6v18h12V7z", "M14 3v4h4", "M12 11v6", "M9 14h6"],
  link: ["M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1", "M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"],
  photo: ["M4 5h16v14H4z", "M4 16l5-5 4 4 3-3 4 4"],
  photoAdd: ["M4 5h16v14H4z", "M4 16l5-5 4 4 3-3 4 4", "M15 9h.01"],
  text: ["M5 6V4h14v2", "M12 4v16", "M9 20h6"],
  note: ["M5 4h14v16H5z", "M9 9h6", "M9 13h6", "M9 17h3"],
  close: ["M6 6l12 12", "M18 6L6 18"],
  plus: ["M12 5v14", "M5 12h14"],
  send: ["M4 12l16-8-6 16-2-7z"],
  heart: ["M12 20s-7-4.4-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.6-7 10-7 10z"],
  chat: ["M4 5h16v11H9l-5 4z"],
  restyle: ["M4 7h9", "M17 7h3", "M15 5v4", "M4 17h3", "M11 17h9", "M9 15v4"],
  share: ["M12 4v11", "M7 9l5-5 5 5", "M5 14v6h14v-6"],
  download: ["M12 4v11", "M7 10l5 5 5-5", "M5 20h14"],
  back15: ["M4 12a8 8 0 1 0 2.4-5.7", "M4 4v4h4", "M10 10v5", "M13.5 10H16M13.5 10v2.2h1.3a1.4 1.4 0 0 1 0 2.8h-1.3"],
  back: ["M4 12a8 8 0 1 0 2.4-5.7", "M4 4v4h4"],
  pause: ["M8 5v14", "M16 5v14"],
  play: ["M8 5l11 7-11 7z"],
  jumpLive: ["M5 5l9 7-9 7z", "M18 5v14"],
  search: ["M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14z", "M20 20l-4-4"],
  chevronDown: ["M6 9l6 6 6-6"],
  chevronLeft: ["M15 6l-6 6 6 6"],
  more: ["M5 12h.01", "M12 12h.01", "M19 12h.01"],
  check: ["M5 12l5 5 9-10"],
  bars: ["M6 18v-6", "M12 18V6", "M18 18v-9"],
  volume: ["M4 9v6h4l5 4V5L8 9z", "M17 9a4 4 0 0 1 0 6"],
  mute: ["M4 9v6h4l5 4V5L8 9z", "M17 10l4 4", "M21 10l-4 4"],
  captions: ["M3 6h18v12H3z", "M10 10H8v4h2", "M16 10h-2v4h2"],
  fullscreen: ["M4 9V4h5", "M20 9V4h-5", "M4 15v5h5", "M20 15v5h-5"],
  expand: ["M14 4h6v6", "M10 20H4v-6", "M20 4l-7 7", "M4 20l7-7"],
  logo: ["M6 15v-6", "M10 18V6", "M14 15V9", "M18 13v-2"],
  mic: ["M12 4a3 3 0 0 0-3 3v5a3 3 0 0 0 6 0V7a3 3 0 0 0-3-3z", "M6 11a6 6 0 0 0 12 0", "M12 17v3"],
  micOff: ["M12 4a3 3 0 0 0-3 3v5a3 3 0 0 0 6 0V7a3 3 0 0 0-3-3z", "M6 11a6 6 0 0 0 12 0", "M12 17v3", "M4 4l16 16"],
  phoneOff: ["M2.5 14.5c2.5-2.4 5.8-3.7 9.5-3.7s7 1.3 9.5 3.7l-1.8 3a1 1 0 0 1-1.2.4l-3-1.2a1 1 0 0 1-.6-.9v-2.1a12 12 0 0 0-5.8 0v2.1a1 1 0 0 1-.6.9l-3 1.2a1 1 0 0 1-1.2-.4z"],
  headphones: ["M4 15v-3a8 8 0 0 1 16 0v3", "M4 15h3v5H5a1 1 0 0 1-1-1z", "M20 15h-3v5h2a1 1 0 0 0 1-1z"],
  copy: ["M8 8h12v12H8z", "M4 16V4h12"],
  trash: ["M5 7h14", "M10 11v6", "M14 11v6", "M6 7l1 13h10l1-13", "M9 7V4h6v3"],
  stop: ["M6 6h12v12H6z"],
  signOut: ["M15 4h4v16h-4", "M10 8l-4 4 4 4", "M6 12h11"],
  user: ["M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8z", "M4 20a8 8 0 0 1 16 0"],
  lock: ["M6 11h12v9H6z", "M8 11V8a4 4 0 0 1 8 0v3"],
  people: ["M9 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6z", "M3 19a6 6 0 0 1 12 0", "M16 5.5a3 3 0 0 1 0 5.5", "M17 13.5a6 6 0 0 1 4 5.5"],
  globe: ["M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z", "M3 12h18", "M12 3c2.5 2.6 3.8 5.6 3.8 9s-1.3 6.4-3.8 9c-2.5-2.6-3.8-5.6-3.8-9S9.5 5.6 12 3z"],
  coin: ["M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z", "M14.5 9.5c-.5-1-1.4-1.5-2.5-1.5-1.5 0-2.5.8-2.5 2s1 1.7 2.5 2 2.5.8 2.5 2-1 2-2.5 2c-1.1 0-2-.5-2.5-1.5", "M12 6.5v1.5", "M12 16v1.5"],
  sparkle: ["M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z", "M19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8z"],
  edit: ["M4 20h4L18.5 9.5a2.1 2.1 0 0 0-3-3L5 17v3z", "M14 7l3 3"],
  shield: ["M12 3l7 3v6c0 4.5-3 8-7 9-4-1-7-4.5-7-9V6z"],
};

export const VISIBILITY_ICON = { private: "lock", connections: "people", public: "globe" };
export const VISIBILITY_LABEL = { private: "Private", connections: "Connections", public: "Public" };

const HEAVY = new Set(["more"]);

export function icon(name, size = 24, stroke = 2) {
  const paths = P[name] || [];
  const w = HEAVY.has(name) ? 3 : stroke;
  return raw(
    `<svg aria-hidden="true" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" ` +
      `stroke="currentColor" stroke-width="${w}" stroke-linecap="round" stroke-linejoin="round">` +
      paths.map((d) => `<path d="${d}"></path>`).join("") +
      "</svg>",
  );
}

export const KIND_ICON = { text: "note", file: "file", link: "link", photo: "photo", source: "file" };
