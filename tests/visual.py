"""Visual regression and design-contract checks in a real browser (Playwright).

    python -m tests.visual             # capture every screen, compare with the baselines
    python -m tests.visual --update    # re-record the baselines
    python -m tests.visual -k stage    # only the shots whose name contains "stage"

Starts tests/fake_gemini.py and the app (local storage in a temp dir, dev sign-in) and
seeds one account through the API: a persona, a style, an album, a connection and a
request waiting, a draft, a finished pod (played to the end at 20x) and a live pod
(nobody tuned in, so its clock stays at 0:00). Then it captures the first screenful of
each key route at 390x844 (phone) and 1440x900 (desktop), in house light and house
dark, plus the phone in light with reduced motion. Before each shot it waits for the
network, the fonts and any loader to go, waits 400 ms, then finishes every animation
and pauses any audio. Volatile text (clocks, relative times, credits, live captions) is
masked, the browser's clock is pinned to a Monday morning, and audio can't start
without a tap.

A shot fails when more than 0.3% of its pixels differ by more than 24 in any channel:
the capture goes to tests/visual/out/ and the difference, in red, to tests/visual/diff/.
A missing baseline is recorded (NEW).

In the same browser it checks the design contract (docs/plans/design-system.md §8):
  - gel contrast: white and --gel-text on --stage at least 7:1, --on-dim at least 4.5:1,
    for every preset gel, the house gel and 50 seeded random accents
  - no "Loading…" or .spinner left on a settled screen (§6, Phase 3)
  - no horizontal overflow at 390 px
VISUAL_PORT moves both servers (the stub on it, the app on the next port).
Needs requirements-dev.txt and `python -m playwright install chromium`.
"""
import argparse
import asyncio
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import httpx
import numpy as np
import websockets
from PIL import Image
from playwright.sync_api import sync_playwright

from tests.smoke_test import music_library

FAKE_PORT = int(os.environ.get("VISUAL_PORT", "8880"))
APP_PORT = FAKE_PORT + 1
BASE = f"http://127.0.0.1:{APP_PORT}"
WS = f"ws://127.0.0.1:{APP_PORT}"
ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent / "visual"
BASELINE, OUT, DIFF = HERE / "baseline", HERE / "out", HERE / "diff"

CHANNEL_TOLERANCE = 24      # a pixel differs if any channel moved by more than this
MAX_DIFF_RATIO = 0.003      # a shot fails past 0.3% differing pixels
SEED = 20261015             # the random accents for the gel contrast check
# Monday 10:00 UTC: "Morning, Vera." on Home, and every timestamp the server hands out
# is in this clock's future, so relative times all read "just now".
FIXED_NOW = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)

PASSES = [   # name, viewport, phone?, color scheme, reduced motion
    ("phone-light", (390, 844), True, "light", "no-preference"),
    ("phone-dark", (390, 844), True, "dark", "no-preference"),
    ("desktop-light", (1440, 900), False, "light", "no-preference"),
    ("desktop-dark", (1440, 900), False, "dark", "no-preference"),
    ("phone-light-reduced", (390, 844), True, "light", "reduce"),
]
ROUTES = [   # name, path, signed in?
    ("login", "/login", False),
    ("home", "/", True),
    ("discover", "/discover", True),
    ("library", "/library", True),
    ("you", "/you", True),
    ("new", "/new", True),
    ("cohost", "/cohost", True),
    ("memo", "/memo", True),
    ("briefings", "/briefings", True),
    ("personas", "/personas", True),
    ("styles", "/styles", True),
    ("connections", "/connections", True),
    ("albums", "/albums", True),
    ("stats", "/stats", True),
    ("replay", "/show/{finished}", True),   # Now playing on the phone, the studio on desktop
    ("stage", "/show/{live}", True),        # the live stage, waiting for you to tune in
]
MASKS = ", ".join([
    ".clock", ".lm-clock", ".pl-time", "[data-r=where]", "[data-r=live]",     # clocks and positions
    ".cap", "[data-r=transcript] .np-line",                                   # spoken lines
    ".pod-meta", ".lib-meta",                                                 # "4 min ago"
    ".credits-chip", ".appnav-me .faint", ".credit-num", ".credit-ring",      # your balance
    "[data-ledger]", "[data-cost]", "[data-make] .card-head .faint",
    "[data-r=listeners]", "[data-r=hearts]",
    "[data-invite]",                                                          # has the port in it
])

# Headless Chromium lets Web Audio start on its own (--autoplay-policy doesn't stop it).
# A phone doesn't: audio waits for a tap. Without this a pod page would tune in by
# itself, and the live pod's clock would run while it's on screen.
AUDIO_NEEDS_A_TAP_JS = """(() => {
  const Real = window.AudioContext;
  if (Real) window.AudioContext = class extends Real { constructor(...a) { super(...a); this.suspend(); } };
})();"""
# Wait for the boot ident, skeletons, the loaders (components.js loader() and waiting())
# and an empty chapter bar (a pod page before its data or socket snapshot) to go, at most
# 8 s; and for the fonts. The green room's chasing clock stays: that screen is waiting.
SETTLE_JS = """async () => {
  await document.fonts.ready;
  const busy = () => {
    const el = document.querySelector("#boot, .skeleton, .splash .ring.chase, .waiting");
    if (el) return el.id ? `#${el.id}` : `.${[...el.classList].join(".")}`;
    return [...document.querySelectorAll("[data-r=chapters]")].some((b) => !b.childElementCount) ? "an empty chapter bar" : "";
  };
  const t0 = performance.now();
  while (busy() && performance.now() - t0 < 8000) await new Promise((r) => setTimeout(r, 100));
  await document.fonts.ready;
  return { ms: Math.round(performance.now() - t0), busy: busy() };
}"""
FREEZE_JS = """() => {
  for (const a of document.getAnimations()) {
    try { a.finish(); } catch { a.pause(); a.currentTime = 0; }   // infinite ones can't finish
  }
  for (const m of document.querySelectorAll("audio, video")) m.pause();
}"""
PROBE_JS = """() => {
  const text = document.body.innerText;
  const loaders = ["Loading…", "Loading..."].filter((s) => text.includes(s));
  if (document.querySelector(".spinner")) loaders.push(".spinner");
  const de = document.documentElement;
  return { loaders, scrollWidth: de.scrollWidth, clientWidth: de.clientWidth, innerWidth: innerWidth };
}"""
# For each case, a .stage lit the way the app lights one (applyAccent), and the colors
# its tokens resolve to, read back through a 1x1 canvas so oklch()/color() come out sRGB.
GEL_JS = """async (cases) => {
  const { applyAccent, gelFor } = await import("/static/js/theme.js");
  const cv = document.createElement("canvas");
  cv.width = cv.height = 1;
  const cx = cv.getContext("2d", { willReadFrequently: true });
  const rgb = (css) => {
    cx.clearRect(0, 0, 1, 1);
    cx.fillStyle = "#010203";
    cx.fillStyle = css;
    if (cx.fillStyle === "#010203") throw new Error(`the canvas can't paint ${css}`);
    cx.fillRect(0, 0, 1, 1);
    return [...cx.getImageData(0, 0, 1, 1).data];
  };
  const out = [];
  for (const c of cases) {
    const stage = document.createElement("div");
    stage.className = "stage";
    stage.style.transition = "none";
    if (c.accent) applyAccent(stage, c.accent);
    else stage.style.setProperty("--gel", c.gel);
    const probe = document.createElement("span");
    stage.append(probe);
    document.body.append(stage);
    const read = (token) => {
      probe.style.backgroundColor = `var(${token})`;
      const css = getComputedStyle(probe).backgroundColor;
      return { css, rgb: rgb(css) };
    };
    out.push({ ...c, gel: c.accent ? gelFor(c.accent) : c.gel,
               stage: read("--stage"), gelText: read("--gel-text"), onDim: read("--on-dim") });
    stage.remove();
  }
  return out;
}"""


# ---- servers ------------------------------------------------------------------------------

def _start(cmd: list[str], env: dict, log_path: Path) -> subprocess.Popen:
    log = open(log_path, "wb")   # a file, not a pipe: an unread pipe fills up and freezes the server
    proc = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT)
    proc.log = log
    return proc


def _wait_up(url: str, timeout: float = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            httpx.get(url, timeout=1)
            return
        except httpx.HTTPError:
            time.sleep(0.2)
    raise RuntimeError(f"{url} never came up")


# ---- seeding ------------------------------------------------------------------------------

def _request(**over) -> dict:
    return {"source": "Genesis 1-3", "style": "girl gossip", "vibe": "gossip",
            "hosts": [{"name": "Kayla", "voice": "Leda"}, {"name": "Brit", "voice": "Kore"}],
            "minutes": 2, "language": "English", "source_names": ["Genesis 1-3.pdf"], **over}


async def _sign_in(client: httpx.AsyncClient, email: str, name: str) -> dict:
    r = await client.post("/api/auth/dev", json={"email": email, "name": name})
    r.raise_for_status()
    return r.json()["me"]


async def _events(show_id: str, cookie: str, playing: bool, done, timeout: float) -> list[dict]:
    """Join a pod's socket (playing or not) and collect its events until done(events)."""
    events: list[dict] = []
    async with websockets.connect(f"{WS}/ws/show/{show_id}", additional_headers={"Cookie": f"gp_session={cookie}"},
                                  max_size=None) as ws:
        await ws.send(json.dumps({"type": "join"}))
        await ws.send(json.dumps({"type": "player", "playing": playing}))
        deadline = time.time() + timeout
        while not done(events):
            if time.time() > deadline:
                raise AssertionError(f"pod {show_id}: gave up waiting; last events {[e['type'] for e in events[-8:]]}")
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=1)
            except asyncio.TimeoutError:
                continue
            if isinstance(msg, str):
                events.append(json.loads(msg))
    return events


async def seed() -> dict:
    """One account with something on every screen. Pods are made one after another: the
    stub numbers its dialogue across pods, so the order keeps every run's words the same."""
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as me, \
            httpx.AsyncClient(base_url=BASE, timeout=30) as bob, \
            httpx.AsyncClient(base_url=BASE, timeout=30) as carol:
        vera = await _sign_in(me, "visual@example.com", "Vera Visual")
        await _sign_in(bob, "bob@example.com", "Bob Baker")
        await _sign_in(carol, "carol@example.com", "Carol Chen")
        cookie = me.cookies.get("gp_session")

        await me.post("/api/personas", json={"name": "Dr. Maya", "voice": "Kore",
                                             "persona": "A precise neuroscientist who loves mechanisms."})
        await me.post("/api/styles", json={"name": "Cozy science", "prompt": "Warm, nerdy, kind.", "accent": "#1F7F6A"})
        album = (await me.post("/api/albums", json={"title": "Bible gossip", "visibility": "public"})).json()
        await me.post("/api/connections", json={"email": "bob@example.com"})
        await bob.post(f"/api/connections/{vera['uid']}/accept")
        await carol.post("/api/connections", json={"email": "visual@example.com"})   # waiting for Vera

        # A finished pod: played to the end (the clock runs at 20x), then saved.
        r = await me.post("/api/shows", json={"request": _request(), "live": True, "visibility": "public",
                                              "template": "girl-talk", "album_ids": [album["id"]],
                                              "tags": ["Bible", "gossip"]})
        r.raise_for_status()
        finished = r.json()["id"]
        await _events(finished, cookie, True, lambda ev: any(e["type"] == "saved" for e in ev), timeout=180)

        await me.post("/api/shows", json={"request": _request(minutes=5), "live": False})   # a draft

        # A live pod nobody has tuned in to: its clock holds at 0:00, in the intro theme,
        # so the hosts wait to voice the cold open. Settled once it's planned and nothing
        # else has happened for 3 s.
        r = await me.post("/api/shows", json={
            "request": _request(style="late-night radio", vibe="late-night", minutes=5), "live": True,
            "visibility": "public", "template": "late-night"})
        r.raise_for_status()
        live = r.json()["id"]
        quiet: dict = {"n": -1, "since": time.time()}

        def settled(ev: list[dict]) -> bool:
            n = len([e for e in ev if e["type"] not in {"tick", "snapshot"}])
            if n != quiet["n"]:
                quiet.update(n=n, since=time.time())
            planned = any(e["type"] == "plan" or (e["type"] == "snapshot" and e.get("plan")) for e in ev)
            return planned and time.time() - quiet["since"] > 3
        await _events(live, cookie, False, settled, timeout=60)
        return {"cookie": cookie, "finished": finished, "live": live}


# ---- shots ----------------------------------------------------------------------------------

def _diff(actual: Image.Image, expected: Image.Image) -> tuple[float, Image.Image | None]:
    """The share of pixels that differ, and a picture of where (None if they match)."""
    w, h = max(actual.width, expected.width), max(actual.height, expected.height)

    def pad(im: Image.Image) -> np.ndarray:
        canvas = Image.new("RGB", (w, h), (255, 0, 255))
        canvas.paste(im.convert("RGB"), (0, 0))
        return np.asarray(canvas, dtype=np.int16)
    a, b = pad(actual), pad(expected)
    differs = (np.abs(a - b) > CHANNEL_TOLERANCE).any(axis=2)
    ratio = float(differs.mean())
    if ratio <= MAX_DIFF_RATIO:
        return ratio, None
    faded = (b.mean(axis=2) * 0.25 + 255 * 0.75).astype(np.uint8)
    out = np.stack([faded] * 3, axis=2)
    out[differs] = (255, 0, 0)
    return ratio, Image.fromarray(out)


def _shoot(page, url: str) -> tuple[bytes, dict]:
    errors = page.errors = []   # uncaught errors on this screen (see _watch)
    page.goto(url, wait_until="networkidle")
    settle = page.evaluate(SETTLE_JS)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(400)
    page.evaluate(FREEZE_JS)
    probe = {**page.evaluate(PROBE_JS), **settle}
    # The viewport, not the full page: Chromium's full-page capture resizes the viewport
    # for a moment, and a pod page re-mounts its layout on that (show.js listens to a
    # min-width media query), so the shot would sometimes catch it half drawn.
    png = page.screenshot(animations="disabled", caret="hide", mask=[page.locator(MASKS)], mask_color="#FF00FF")
    return png, {**probe, "errors": errors}


def _watch(page):
    """Collect the page's uncaught errors and console errors; _shoot reports them per shot."""
    page.errors = []
    page.on("pageerror", lambda exc: page.errors.append(str(exc).splitlines()[0]))
    page.on("console", lambda m: page.errors.append(m.text) if m.type == "error" else None)
    return page


def _gel_cases() -> list[dict]:
    """Every preset accent in theme.js, the house gel, and 50 random custom accents."""
    src = (ROOT / "static/js/theme.js").read_text(encoding="utf-8")
    block = re.search(r"const GELS = \{(.*?)\};", src, re.S).group(1)
    presets = re.findall(r'"(#[0-9A-Fa-f]{6})"\s*:', block)
    house = re.search(r'HOUSE_GEL = "(#[0-9A-Fa-f]{6})"', src).group(1)
    rng = random.Random(SEED)
    return ([{"label": f"preset {a}", "accent": a} for a in presets] + [{"label": "house", "gel": house}]
            + [{"label": f"custom {a}", "accent": a} for a in (f"#{rng.randrange(1 << 24):06X}" for _ in range(50))])


def _luminance(rgb) -> float:
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in (v / 255 for v in rgb[:3])]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(fg, bg) -> float:
    a, b = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (a + 0.05) / (b + 0.05)


def gel_checks(page) -> list[tuple[bool, str]]:
    """§8: captions (white, --gel-text) at least 7:1 on --stage, --on-dim at least 4.5:1."""
    page.goto("/login", wait_until="networkidle")   # any page: it has the stylesheets
    rows = page.evaluate(GEL_JS, _gel_cases())
    results = []
    for title, fg, need in (("white on --stage", lambda r: (255, 255, 255), 7.0),
                            ("--gel-text on --stage", lambda r: r["gelText"]["rgb"], 7.0),
                            ("--on-dim on --stage", lambda r: r["onDim"]["rgb"], 4.5)):
        scored = sorted(((_contrast(fg(r), r["stage"]["rgb"]), r) for r in rows), key=lambda x: x[0])
        worst, r = scored[0]
        failing = [f"{x['label']} (gel {x['gel']}) {c:.2f}:1" for c, x in scored if c < need]
        note = f"worst {worst:.2f}:1 for {r['label']}, gel {r['gel']}, --stage {r['stage']['css']}"
        results.append((not failing, f"gel contrast: {title} >= {need:g}:1 across {len(rows)} gels ({note})"
                        + (f"; below: {', '.join(failing)}" if failing else "")))
    return results


class Report:
    def __init__(self) -> None:
        self.results: list[tuple[str, str]] = []     # (PASS | FAIL | NEW, text)
        self.loaders: dict[str, list[str]] = {}      # route -> what was left, and in which pass
        self.overflow: list[str] = []
        self.captured: set[str] = set()

    def add(self, status: str, text: str) -> None:
        self.results.append((status, text))

    @property
    def failed(self) -> bool:
        return any(s == "FAIL" for s, _ in self.results)


def _check_shot(report: Report, name: str, png: bytes, update: bool, settle: dict) -> None:
    slow = f", {settle['ms'] / 1000:.1f} s to settle" if settle["ms"] >= 1000 else ""
    if settle["errors"]:
        slow += f", page errors: {' | '.join(dict.fromkeys(settle['errors']))[:300]}"
    if settle["busy"]:   # never recorded: a baseline of a screen still loading is worse than none
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / f"{name}.png").write_bytes(png)
        report.add("FAIL", f"shot {name}: still showing {settle['busy']} after {settle['ms'] / 1000:.0f} s; "
                           f"see tests/visual/out/{name}.png")
        return
    base_png = BASELINE / f"{name}.png"
    if update or not base_png.exists():
        base_png.write_bytes(png)
        report.add("NEW", f"shot {name}{slow and f' ({slow[2:]})'}")
        return
    ratio, diff = _diff(Image.open(BytesIO(png)), Image.open(base_png))
    if diff is None:
        report.add("PASS", f"shot {name} ({ratio:.3%} differ{slow})")
        return
    OUT.mkdir(parents=True, exist_ok=True)
    DIFF.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.png").write_bytes(png)
    diff.save(DIFF / f"{name}.png")
    report.add("FAIL", f"shot {name} ({ratio:.2%} of pixels differ{slow}; see tests/visual/diff/{name}.png)")


def capture(browser, ids: dict, report: Report, update: bool, only: str) -> None:
    """Every route in every pass: a shot, the stray-loader probe and, on phones, overflow."""
    for pname, (w, h), phone, scheme, motion in PASSES:
        contexts = {}
        for signed_in in (False, True):
            ctx = browser.new_context(
                base_url=BASE, viewport={"width": w, "height": h}, device_scale_factor=1,
                is_mobile=phone, has_touch=phone, color_scheme=scheme, reduced_motion=motion,
                locale="en-US", timezone_id="UTC")
            ctx.clock.set_fixed_time(FIXED_NOW)
            ctx.add_init_script(AUDIO_NEEDS_A_TAP_JS)   # so the stage waits at "Tune in"
            if signed_in:
                ctx.add_cookies([{"name": "gp_session", "value": ids["cookie"], "url": BASE}])
            contexts[signed_in] = ctx
        pages = {k: _watch(c.new_page()) for k, c in contexts.items()}
        try:
            for rname, path, signed_in in ROUTES:
                name = f"{rname}-{pname}"
                if only not in name:
                    continue
                report.captured.add(name)
                png, probe = _shoot(pages[signed_in], path.format(**ids))
                if probe["loaders"]:
                    report.loaders.setdefault(f"{path} ({rname})", []).extend(f"{s} [{pname}]" for s in probe["loaders"])
                if phone and (probe["scrollWidth"] > probe["clientWidth"] or probe["innerWidth"] != w):
                    report.overflow.append(f"{path} [{pname}]: scrollWidth {probe['scrollWidth']}, "
                                           f"clientWidth {probe['clientWidth']}, innerWidth {probe['innerWidth']}")
                _check_shot(report, name, png, update, probe)
        finally:
            for c in contexts.values():
                c.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--update", action="store_true", help="re-record the baselines")
    ap.add_argument("-k", default="", help="only the shots whose name contains this")
    args = ap.parse_args()

    started = seeded = time.time()
    data_dir = tempfile.mkdtemp(prefix="podcasts-visual-")
    env = {
        **os.environ, "GEMINI_API_KEY": "fake", "GEMINI_BASE_URL": f"http://127.0.0.1:{FAKE_PORT}",
        "AUTH_MODE": "dev", "STORAGE_BACKEND": "local", "DATA_DIR": data_dir, "ADMIN_EMAILS": "visual@example.com",
        "SIGNUP_CREDITS": "60", "SESSION_SECRET": "visual-test", "FAKE_BEAT_EVERY": "8",
        # the finished pod plays out fast; the live one, with nobody tuned in, never moves or ends
        "CLOCK_SPEED": "20", "LOW_WATER_SECONDS": "3", "LINES_PER_SEGMENT": "4", "IDLE_END_SECONDS": "3600",
        "STINGER_DELAY_SECONDS": "0",
        "MUSIC_DIR": str(music_library(Path(data_dir, "music"))),
    }
    py = sys.executable
    app_log = Path(data_dir, "app.log")
    fake = _start([py, "-m", "uvicorn", "tests.fake_gemini:app", "--port", str(FAKE_PORT)], env, Path(data_dir, "fake.log"))
    srv = _start([py, "-m", "uvicorn", "app.main:app", "--port", str(APP_PORT)], env, app_log)

    for d in (OUT, DIFF):
        shutil.rmtree(d, ignore_errors=True)
    BASELINE.mkdir(parents=True, exist_ok=True)
    report = Report()
    error = None
    try:
        _wait_up(f"http://127.0.0.1:{FAKE_PORT}/docs")
        _wait_up(f"{BASE}/healthz")
        ids = asyncio.run(seed())
        seeded = time.time()
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                capture(browser, ids, report, args.update, args.k)
                if not args.k:
                    ctx = browser.new_context(base_url=BASE, viewport={"width": 390, "height": 844})
                    for ok, text in gel_checks(ctx.new_page()):
                        report.add("PASS" if ok else "FAIL", text)
                    ctx.close()
            finally:
                browser.close()
        if not args.k:
            report.add("FAIL" if report.loaders else "PASS", "no stray loaders on a settled screen" + "".join(
                f"\n    {route}: {', '.join(found)}" for route, found in report.loaders.items()))
            report.add("FAIL" if report.overflow else "PASS", "no horizontal overflow at 390 px" + "".join(
                f"\n    {o}" for o in report.overflow))
        if args.update and not args.k:   # drop baselines for shots that no longer exist
            for old in BASELINE.glob("*.png"):
                if old.stem not in report.captured:
                    old.unlink()
    except Exception as exc:   # report what ran so far, then the failure
        error = exc
    finally:
        for p in (srv, fake):
            p.terminate()
            p.wait(timeout=10)
            p.log.close()

    for status, text in report.results:
        print(f"{status} {text}")
    print(f"{len(report.captured)} shots in {time.time() - started:.0f} s (seeding {seeded - started:.0f} s); "
          f"baselines in tests/visual/baseline/")
    if error:
        print(f"ERROR {type(error).__name__}: {error}")
        print(app_log.read_text(encoding="utf-8", errors="replace")[-4000:])
    if error or report.failed:
        print(f"The app's data and logs are in {data_dir}")
        return 1
    shutil.rmtree(data_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
