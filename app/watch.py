"""The sites and links a listener watches, for their briefings.

Three kinds of watch:
  site    a homepage, section or blog. Its RSS or Atom feed (named in the page's
          <link rel="alternate">, or at a common path) lists new posts. A site with
          no feed counts a headline link that wasn't on the page last time as new.
  page    one page: a pricing page, a status page, an agenda. We keep its text
          paragraph by paragraph and report what was added and what was removed.
  search  words to search the news for. Gemini with Grounding with Google Search
          finds recent stories (Fetcher.searcher); a story whose headline we haven't
          seen is new. Google asks apps to show the Search Suggestions that come with
          grounded results, so the latest are kept on the watch (and the briefing).
Pasting a feed's own address makes a site whose feed we already know.

Fetching goes through the guard in sources.py (public addresses only, checked
again on every redirect, a 5 MB cap) and names the app in the User-Agent. It
honors robots.txt, sends If-None-Match / If-Modified-Since so an unchanged page
costs a 304, and spaces out requests to the same host. What comes back is
untrusted text: briefings fence it as material to report on, never instructions.

A watch (users/{uid}/watches/{id}):
  id, url, label, kind (site | page | search), feed_url, query, suggestions, title, created_at,
  etag, last_modified, seen [hash of each item or link already counted],
  last_checked, last_changed, failures, paused, status (ok | blocked | failing), note,
  pending [updates waiting for the next briefing]
A page's text for comparing (users/{uid}/watch_pages/{same id}): id, paras [{h: hash, t: text}], at
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Awaitable, Callable
from urllib.parse import quote_plus, urljoin, urlsplit
from urllib.robotparser import RobotFileParser
from xml.etree.ElementTree import ParseError

import httpx
from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring as parse_xml

from .sources import (
    BOT_NAME, FETCH_TIMEOUT, MAX_REDIRECTS, SourceError, _check_public, _read_capped, html_to_text, pdf_to_text,
    user_agent,
)

MAX_WATCHES = 20          # per listener
MAX_SEARCHES = 5          # of those, searches: each check is a grounded Gemini call
QUERY_CHARS = 200
MAX_PENDING = 8           # updates kept per watch until a briefing uses them
UPDATE_DAYS = 7           # older updates are dropped unheard
FIRST_ITEMS = 3           # a newly added site's latest posts count as news once
NEW_PER_CHECK = 5
SEEN_KEEP = 400
EXCERPT_CHARS = 700
PARA_CHARS = 400
PAGE_PARAS = 400
PAUSE_AFTER = 5           # failed checks in a row
RECHECK_SECONDS = 15 * 60  # a watch checked this recently isn't fetched again for a briefing
HOST_GAP = 1.0            # seconds between requests to one host
ROBOTS_TTL = 6 * 3600
FEED_TYPES = {"application/rss+xml", "application/atom+xml", "application/feed+json", "application/rdf+xml"}
FEED_PATHS = ("/feed", "/rss.xml", "/atom.xml", "/feed.xml", "/index.xml", "/rss")
ROBOT_TOKEN = BOT_NAME.split("/")[0]

ATOM = "{http://www.w3.org/2005/Atom}"
RSS1 = "{http://purl.org/rss/1.0/}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"

# Lines that change on every visit without anything happening: relative times,
# clocks, copyright years, view counters.
VOLATILE = re.compile(
    r"^(?:(?:last )?updated\b.*|.*\b\d+\s+(?:seconds?|minutes?|mins?|hours?|hrs?)\s+ago\b.*|"
    r"\d{1,2}:\d{2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?(?:\s+[A-Z]{2,4})?|"
    r"(?:©|\(c\)|copyright)\s*\d{4}.*|[\d,.]+\s+(?:views?|reads?|comments?|shares?))$",
    re.IGNORECASE,
)


class Blocked(SourceError):
    """The site's robots.txt asks bots like ours to stay out."""


# (query, days back) -> (stories [title, source, url, published, summary], Search Suggestions HTML)
Searcher = Callable[[str, int], Awaitable[tuple[list[dict], str]]]


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:12]


def _headline_key(title: str) -> str:
    """The same story from a second search reads the same once case and punctuation go."""
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def search_watch(query: str) -> dict:
    """What a new search watch is (the counterpart of Fetcher.detect for links)."""
    query = re.sub(r"\s+", " ", query).strip()[:QUERY_CHARS]
    if len(query) < 2:
        raise SourceError("Type a few words to search for.")
    return {"kind": "search", "url": f"https://www.google.com/search?q={quote_plus(query)}", "feed_url": None,
            "query": query, "title": query, "found": f"Searching the news for “{query}”"}


def _clip(text: str, n: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def host_of(url: str) -> str:
    host = urlsplit(url).hostname or url
    return host[4:] if host.startswith("www.") else host


@dataclass
class Fetched:
    url: str            # after redirects
    status: int
    headers: httpx.Headers
    body: bytes
    encoding: str = "utf-8"

    @property
    def text(self) -> str:
        return self.body.decode(self.encoding, errors="ignore")

    @property
    def is_html(self) -> bool:
        ctype = self.headers.get("content-type", "")
        return "html" in ctype or "<html" in self.body[:1000].decode("ascii", "ignore").lower()


# ---- reading feeds and pages ---------------------------------------------------------------

def parse_feed(body: bytes) -> tuple[str, list[dict]] | None:
    """(feed title, items newest first) for RSS 2.0, RSS 1.0 / RDF and Atom, else None.
    Each item: id, title, link, summary (plain text)."""
    head = body[:4096].lower()
    if not any(tag in head for tag in (b"<rss", b"<feed", b"<rdf")):
        return None
    try:
        root = parse_xml(body)   # defusedxml: no entity expansion, no external references
    except (ParseError, DefusedXmlException):
        return None

    def text(el, *names: str) -> str:
        for name in names:
            found = el.find(name)
            if found is not None and (found.text or "").strip():
                return found.text.strip()
        return ""

    def item(id_: str, title: str, link: str, summary: str) -> dict:
        plain = html_to_text(summary)[1] if "<" in summary else summary
        return {"id": id_ or link or title, "title": _clip(title, 200), "link": link,
                "summary": _clip(plain, EXCERPT_CHARS)}

    items: list[dict] = []
    if root.tag == "rss":
        channel = root.find("channel")
        if channel is None:
            return None
        title = text(channel, "title")
        for it in channel.findall("item"):
            items.append(item(text(it, "guid"), text(it, "title"), text(it, "link"),
                              text(it, "description", CONTENT + "encoded")))
    elif root.tag == ATOM + "feed":
        title = text(root, ATOM + "title")
        for entry in root.findall(ATOM + "entry"):
            link = ""
            for ln in entry.findall(ATOM + "link"):
                if ln.get("rel", "alternate") == "alternate" and ln.get("href"):
                    link = ln.get("href", "")
                    break
            items.append(item(text(entry, ATOM + "id"), text(entry, ATOM + "title"), link,
                              text(entry, ATOM + "summary", ATOM + "content")))
    elif root.tag.endswith("RDF"):
        channel = root.find(RSS1 + "channel")
        title = text(channel, RSS1 + "title") if channel is not None else ""
        for it in root.findall(RSS1 + "item"):
            items.append(item(it.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about", ""),
                              text(it, RSS1 + "title"), text(it, RSS1 + "link"), text(it, RSS1 + "description")))
    else:
        return None
    return _clip(title, 120), [i for i in items if i["title"] or i["link"]]


class _PageLinks(HTMLParser):
    """Feed links in the head, and headline links in the content (not menus or footers)."""
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "aside"}

    def __init__(self) -> None:
        super().__init__()
        self.feeds: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._skip = 0
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "link" and "alternate" in (a.get("rel") or "").lower() and (a.get("type") or "").lower() in FEED_TYPES:
            if a.get("href"):
                self.feeds.append(a["href"])
        elif tag in self.SKIP:
            self._skip += 1
        elif tag == "a" and not self._skip and a.get("href"):
            self._href, self._text = a["href"], []

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "a" and self._href is not None:
            self.links.append((self._href, re.sub(r"\s+", " ", "".join(self._text)).strip()))
            self._href = None

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)


def page_links(html: str, base: str) -> tuple[list[str], list[tuple[str, str]]]:
    """(feed urls, headline links) on a page. Headlines are same-site links with
    enough text to be a title, in page order, without repeats."""
    parser = _PageLinks()
    parser.feed(html)
    site = host_of(base)
    out: dict[str, str] = {}
    for href, label in parser.links:
        url = urljoin(base, href).split("#", 1)[0]
        if urlsplit(url).scheme in {"http", "https"} and host_of(url) == site and len(label) >= 25 and url not in out:
            out[url] = _clip(label, 200)
    return [urljoin(base, f) for f in parser.feeds], list(out.items())


def _feed_belongs(page: str, feed: str) -> bool:
    """A site's home page owns any feed it names; a deeper page (/blog/) only one under
    its own path (/blog/feed). /downloads/ naming the site-wide news feed is a page to
    watch, not that feed."""
    path = urlsplit(page).path.rstrip("/")
    if not path:
        return True
    feed_path = urlsplit(feed).path
    return host_of(feed) == host_of(page) and feed_path.startswith(path + "/")


def paragraphs(text: str) -> list[str]:
    """A page's text as comparable paragraphs, minus lines that change on every visit."""
    out: list[str] = []
    for raw in re.split(r"\n+", text):
        para = re.sub(r"\s+", " ", raw).strip()
        if para and not VOLATILE.match(para):
            out.append(para[:PARA_CHARS])
    return out[:PAGE_PARAS]


# ---- fetching ------------------------------------------------------------------------------

class Fetcher:
    """One per process, so robots.txt answers and per-host spacing are shared by every check."""

    def __init__(self, *, contact: str = "", transport: httpx.AsyncBaseTransport | None = None,
                 host_gap: float = HOST_GAP, robots_ttl: float = ROBOTS_TTL, searcher: Searcher | None = None):
        self.contact = contact            # the app's own address, for the User-Agent
        self.transport = transport
        self.searcher = searcher          # news search for search watches (Gemini + Google Search)
        self.host_gap = host_gap
        self.robots_ttl = robots_ttl
        self._robots: dict[str, tuple[RobotFileParser, float]] = {}
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def _space(self, host: str) -> None:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            wait = self._last.get(host, 0.0) + self.host_gap - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last[host] = time.monotonic()

    async def _raw(self, url: str, headers: dict) -> Fetched:
        current = url.strip()
        sent = {"User-Agent": user_agent(self.contact), **headers}
        try:
            async with httpx.AsyncClient(follow_redirects=False, timeout=FETCH_TIMEOUT, transport=self.transport) as client:
                for _ in range(MAX_REDIRECTS + 1):
                    await _check_public(current)
                    await self._space(urlsplit(current).hostname or "")
                    async with client.stream("GET", current, headers=sent) as res:
                        if res.is_redirect and res.headers.get("location"):
                            current = urljoin(current, res.headers["location"])
                            continue
                        body = await _read_capped(res) if res.status_code == 200 else b""
                        return Fetched(current, res.status_code, res.headers, body, res.charset_encoding or "utf-8")
        except httpx.HTTPError as exc:
            raise SourceError(f"Couldn't reach {host_of(url)} ({type(exc).__name__}).") from exc
        raise SourceError("That link redirects too many times.")

    async def _allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        cached = self._robots.get(origin)
        if not cached or time.monotonic() - cached[1] > self.robots_ttl:
            rules = RobotFileParser()
            try:
                got = await self._raw(origin + "/robots.txt", {})
            except SourceError:
                return True   # unreachable: the page fetch itself will tell us
            if got.status == 200:
                rules.parse(got.text.splitlines())
            elif 400 <= got.status < 500:
                rules.allow_all = True      # no robots.txt: no rules
            else:
                return True                 # a server error: try again next time
            cached = (rules, time.monotonic())
            self._robots[origin] = cached
        return cached[0].can_fetch(ROBOT_TOKEN, url)

    async def get(self, url: str, *, etag: str | None = None, modified: str | None = None) -> Fetched:
        """A page, or a 304 when it hasn't changed since the validators we hold."""
        if not await self._allowed(url):
            raise Blocked(f"{host_of(url)} asks bots not to read that page (robots.txt).")
        headers = {"Accept": "text/html,application/xhtml+xml,application/rss+xml,application/atom+xml,application/xml;q=0.9,*/*;q=0.5"}
        if etag:
            headers["If-None-Match"] = etag
        if modified:
            headers["If-Modified-Since"] = modified
        got = await self._raw(url, headers)
        if got.status in (200, 304):
            return got
        reason = {401: "needs a sign-in", 403: "turned us away", 404: "isn't there any more", 410: "is gone",
                  429: "asked us to slow down"}.get(got.status, "had an error")
        raise SourceError(f"{host_of(got.url)} {reason} ({got.status}).")

    # ---- adding a watch ----------------------------------------------------------------------

    async def detect(self, url: str, mode: str = "auto") -> dict:
        """What a new watch is: a site with a feed, a site without one, or a page.
        `mode` is what the listener picked: auto, site (new posts) or page (changes)."""
        got = await self.get(url)
        feed = parse_feed(got.body)
        if feed is not None:
            return {"kind": "site", "url": got.url, "feed_url": got.url, "title": feed[0] or host_of(got.url),
                    "found": f"Feed found: {len(feed[1])} posts"}
        if got.is_html:
            title, _ = html_to_text(got.text)
            title = _clip(title, 120) or host_of(got.url)
            if mode != "page":
                feeds, links = page_links(got.text, got.url)
                if mode == "auto":   # a deep link is a page to watch, unless the feed is its own
                    feeds = [f for f in feeds if _feed_belongs(got.url, f)]
                for candidate in await self._feed_candidates(got.url, feeds):
                    try:
                        sub = await self.get(candidate)
                    except SourceError:
                        continue
                    found = parse_feed(sub.body)
                    if found is not None:
                        return {"kind": "site", "url": got.url, "feed_url": sub.url, "title": title,
                                "found": f"Feed found: {len(found[1])} posts"}
                if mode == "site":
                    return {"kind": "site", "url": got.url, "feed_url": None, "title": title,
                            "found": f"No feed, so we'll watch its {len(links)} headline links"}
            return {"kind": "page", "url": got.url, "feed_url": None, "title": title,
                    "found": "Watching this page for changes"}
        if mode == "site":
            raise SourceError("That isn't a web page or a feed, so there are no posts to watch.")
        title = got.url.rsplit("/", 1)[-1] or host_of(got.url)
        return {"kind": "page", "url": got.url, "feed_url": None, "title": _clip(title, 120),
                "found": "Watching this file for changes"}

    async def _feed_candidates(self, url: str, feeds: list[str]) -> list[str]:
        if feeds:
            return feeds[:2]
        parts = urlsplit(url)
        if parts.path in ("", "/"):   # a site's home page: try where feeds usually live
            return [f"{parts.scheme}://{parts.netloc}{p}" for p in FEED_PATHS[:3]]
        return []

    # ---- checking ----------------------------------------------------------------------------

    async def check(self, watch: dict, page: dict | None = None) -> tuple[dict, dict | None, list[dict]]:
        """Fetch one watch. Returns (the watch, its page text for next time, new updates).
        Failures are recorded on the watch, not raised; it pauses after PAUSE_AFTER in a row."""
        now = time.time()
        w = {**watch, "last_checked": now}
        updates: list[dict] = []
        try:
            if w["kind"] == "page":
                updates, page = await self._check_page(w, page)
            elif w["kind"] == "search":
                updates = await self._check_search(w, watch.get("last_checked"))
            elif w.get("feed_url"):
                updates = await self._check_feed(w)
            else:
                updates = await self._check_links(w)
            w.update(failures=0, status="ok", note="")
        except Blocked as exc:
            w.update(failures=int(w.get("failures", 0)) + 1, status="blocked", note=str(exc))
        except SourceError as exc:
            w.update(failures=int(w.get("failures", 0)) + 1, status="failing", note=str(exc))
        if w["status"] != "ok" and w["failures"] >= PAUSE_AFTER:
            w.update(paused=True, note=f"Paused after {PAUSE_AFTER} failed checks. {w['note']}")
        # Items come newest first; stamping each a millisecond earlier keeps that order by time.
        stamp = [{"site": host_of(w["url"]), **u, "watch_id": w["id"],   # a search names each story's outlet
                  "label": w.get("label") or w.get("title") or host_of(w["url"]), "found_at": now - i / 1000}
                 for i, u in enumerate(updates)]
        if stamp:
            w["last_changed"] = now
        keep = [u for u in w.get("pending", []) + stamp if now - u.get("found_at", now) < UPDATE_DAYS * 86400]
        keep.sort(key=lambda u: u.get("found_at", 0))
        w["pending"] = keep[-MAX_PENDING:]
        return w, page, stamp

    def _validators(self, w: dict, got: Fetched) -> None:
        w["etag"] = got.headers.get("etag")
        w["last_modified"] = got.headers.get("last-modified")

    async def _check_feed(self, w: dict) -> list[dict]:
        got = await self.get(w["feed_url"], etag=w.get("etag"), modified=w.get("last_modified"))
        if got.status == 304:
            return []
        self._validators(w, got)
        feed = parse_feed(got.body)
        if feed is None:
            raise SourceError("The site's feed didn't come back as a feed this time.")
        return self._new(w, [(i["id"], i) for i in feed[1]],
                         lambda i: {"kind": "new", "title": i["title"], "url": i["link"] or w["url"],
                                    "excerpt": i["summary"]})

    async def _check_links(self, w: dict) -> list[dict]:
        got = await self.get(w["url"], etag=w.get("etag"), modified=w.get("last_modified"))
        if got.status == 304:
            return []
        self._validators(w, got)
        _, links = page_links(got.text, got.url)
        return self._new(w, [(url, (url, label)) for url, label in links],
                         lambda pair: {"kind": "new", "title": pair[1], "url": pair[0], "excerpt": ""})

    async def _check_search(self, w: dict, previous: float | None) -> list[dict]:
        if self.searcher is None:
            raise SourceError("News search isn't available right now.")
        days = 3 if not previous else min(7, max(1, math.ceil((time.time() - float(previous)) / 86400)))
        try:
            items, suggestions = await self.searcher(w.get("query") or w.get("title") or "", days)
        except SourceError:
            raise
        except Exception as exc:   # the model or the search failed: try again next time
            raise SourceError(f"The news search didn't work this time ({type(exc).__name__}).") from exc
        if suggestions:
            w["suggestions"] = suggestions[:20_000]
        return self._new(w, [(_headline_key(i["title"]), i) for i in items],
                         lambda i: {"kind": "new", "via": "search", "title": i["title"], "url": i.get("url") or "",
                                    "site": i.get("source") or "", "excerpt": i.get("summary") or "",
                                    "published": i.get("published") or ""})

    def _new(self, w: dict, keyed: list[tuple[str, object]], make) -> list[dict]:
        """Items not counted before. A watch's first check takes its latest few as news."""
        seen = list(w.get("seen") or [])
        known = set(seen)
        first = not seen and not w.get("last_changed")
        keys = [_hash(k) for k, _ in keyed]
        fresh = [v for h, (_, v) in zip(keys, keyed) if h not in known]
        picked = fresh[:FIRST_ITEMS] if first else fresh[:NEW_PER_CHECK]
        current = set(keys)
        w["seen"] = (keys + [h for h in seen if h not in current])[:SEEN_KEEP]
        return [make(v) for v in picked]

    async def _check_page(self, w: dict, page: dict | None) -> tuple[list[dict], dict | None]:
        got = await self.get(w["url"], etag=w.get("etag"), modified=w.get("last_modified"))
        if got.status == 304:
            return [], page
        self._validators(w, got)
        if got.body.startswith(b"%PDF") or "pdf" in got.headers.get("content-type", ""):
            title, text = w.get("title", ""), pdf_to_text(got.body)
        else:
            title, text = html_to_text(got.text) if got.is_html else ("", got.text)
        # {h, t} pairs, not [hash, text]: Firestore rejects arrays of arrays.
        new_page = {"id": w["id"], "paras": [{"h": _hash(p), "t": p} for p in paragraphs(text)], "at": time.time()}
        if not page or not page.get("paras"):
            return [], new_page            # the first look: nothing to compare with yet
        old = {x["h"] for x in page["paras"]}
        now_hashes = {x["h"] for x in new_page["paras"]}
        added = [x["t"] for x in new_page["paras"] if x["h"] not in old]
        removed = [x["t"] for x in page["paras"] if x["h"] not in now_hashes]
        if not added and not removed:
            return [], new_page
        return [{
            "kind": "changed", "title": _clip(title, 200) or w.get("title") or host_of(w["url"]), "url": w["url"],
            "excerpt": _clip(" / ".join(added), EXCERPT_CHARS),
            "removed": _clip(" / ".join(removed), EXCERPT_CHARS // 2),
        }], new_page


async def check_all(fetcher: Fetcher, watches: list[dict], pages: dict[str, dict], *,
                    force: bool = False, budget: float = 30.0,
                    on_checked: Callable[[dict, dict | None, list[dict]], Awaitable[None]] | None = None,
                    ) -> list[tuple[dict, dict | None, list[dict]]]:
    """Check every active watch not checked in the last RECHECK_SECONDS, a few at a time,
    giving up on whatever is still running after `budget` seconds. on_checked runs as each
    one finishes (so it can be saved, and a waiting page can show it), not at the end."""
    now = time.time()
    due = [w for w in watches if not w.get("paused")
           and (force or now - float(w.get("last_checked") or 0) >= RECHECK_SECONDS)]
    gate = asyncio.Semaphore(5)

    async def one(w: dict):
        async with gate:
            result = await fetcher.check(w, pages.get(w["id"]))
        if on_checked is not None:
            await on_checked(*result)
        return result

    tasks = [asyncio.create_task(one(w)) for w in due]
    if not tasks:
        return []
    done, pending = await asyncio.wait(tasks, timeout=budget)
    for t in pending:
        t.cancel()
    return [t.result() for t in done if not t.exception()]
