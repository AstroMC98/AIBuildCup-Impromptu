"""Turn uploaded files and links into plain text the writers can work from.

Listeners holding a share link can submit links too, so fetching is guarded:
only public addresses (checked again on every redirect), a size cap and a timeout.
"""
from __future__ import annotations

import asyncio
import io
import ipaddress
import json
import re
import socket
import zipfile
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx
from pypdf import PdfReader

TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".srt", ".vtt", ".log", ".yaml", ".yml"}
SUPPORTED = sorted(TEXT_EXTENSIONS | {".pdf", ".docx", ".html", ".htm"})
IMAGE_TYPES = ["image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"]
IMAGE_EXTENSIONS = [".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif"]

# Sites like Wikipedia refuse fetchers that don't say who they are and how to reach
# them (403), so the reader names itself and points back at the app that sent it.
BOT_NAME = "GeminiPodcastsBot/1.0"
MAX_FETCH_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5
FETCH_TIMEOUT = 15.0


class SourceError(ValueError):
    pass


class _TextFromHTML(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article", "blockquote"}

    def __init__(self) -> None:
        super().__init__()
        self.out: list[str] = []
        self._skip_depth = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip_depth:
            self.out.append(data)


def _tidy(text: str) -> str:
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def html_to_text(html: str) -> tuple[str, str]:
    parser = _TextFromHTML()
    parser.feed(html)
    return parser.title.strip(), _tidy("".join(parser.out))


def pdf_to_text(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    return _tidy("\n\n".join(page.extract_text() or "" for page in reader.pages))


def docx_to_text(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise SourceError("That .docx file could not be opened.") from exc
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    return _tidy(re.sub(r"<[^>]+>", "", xml))


def file_to_text(filename: str, data: bytes) -> str:
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext == ".pdf":
        text = pdf_to_text(data)
    elif ext == ".docx":
        text = docx_to_text(data)
    elif ext in {".html", ".htm"}:
        text = html_to_text(data.decode("utf-8", errors="ignore"))[1]
    elif ext == ".json":
        try:
            text = json.dumps(json.loads(data), indent=1, ensure_ascii=False)
        except json.JSONDecodeError:
            text = data.decode("utf-8", errors="ignore")
    elif ext in TEXT_EXTENSIONS or not ext:
        text = data.decode("utf-8", errors="ignore")
    else:
        raise SourceError(f"{filename}: {ext} files aren't supported. Use {', '.join(SUPPORTED)}.")
    if not text.strip():
        raise SourceError(f"{filename}: no readable text found (scanned PDFs need OCR first).")
    return text


def sniff_image(data: bytes) -> str | None:
    """The image type from its first bytes. File names and browsers can lie."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in {b"heic", b"heix", b"hevc", b"heim", b"heis", b"hevm", b"hevs"}:
            return "image/heic"
        if brand in {b"mif1", b"msf1"}:
            return "image/heif"
    return None


# ---- links ------------------------------------------------------------------

async def _resolve(host: str, port: int) -> list[str]:
    """Every address a host name resolves to. Tests replace this."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def _check_public(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise SourceError("Links need to start with http:// or https://")
    host = parts.hostname
    try:
        addresses = [host] if _is_ip(host) else await _resolve(host, parts.port or (443 if parts.scheme == "https" else 80))
    except OSError as exc:
        raise SourceError(f"Couldn't find {host}.") from exc
    for addr in addresses:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global:
            raise SourceError("That link points to a private network address, so it can't be fetched.")


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


async def _read_capped(res: httpx.Response) -> bytes:
    declared = res.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_FETCH_BYTES:
        raise SourceError("That page is over 5 MB.")
    body = bytearray()
    async for chunk in res.aiter_bytes():
        body += chunk
        if len(body) > MAX_FETCH_BYTES:
            raise SourceError("That page is over 5 MB.")
    return bytes(body)


def user_agent(contact: str) -> str:
    about = "reads pages listeners share with a live podcast"
    return f"{BOT_NAME} (+{contact}; {about})" if contact else f"{BOT_NAME} ({about})"


async def url_to_text(
    url: str, *, contact: str = "", transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[str, str]:
    """Fetch a page or PDF and return (title, text). Redirects are followed by hand
    so every hop is checked against private addresses. `contact` is this app's own
    address, sent in the User-Agent."""
    current = url.strip()
    headers = {"User-Agent": user_agent(contact)}
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=FETCH_TIMEOUT, transport=transport) as client:
            for _ in range(MAX_REDIRECTS + 1):
                await _check_public(current)
                async with client.stream("GET", current, headers=headers) as res:
                    if res.is_redirect and res.headers.get("location"):
                        current = urljoin(current, res.headers["location"])
                        continue
                    res.raise_for_status()
                    body = await _read_capped(res)
                    ctype = res.headers.get("content-type", "")
                    charset = res.charset_encoding or "utf-8"
                    break
            else:
                raise SourceError("That link redirects too many times.")
    except httpx.HTTPError as exc:
        raise SourceError(f"Couldn't fetch {url}: {exc}") from exc

    if "pdf" in ctype or current.lower().endswith(".pdf") or body.startswith(b"%PDF"):
        return current, pdf_to_text(body)
    text = body.decode(charset, errors="ignore")
    if "html" in ctype or "<html" in text[:500].lower():
        title, extracted = html_to_text(text)
        return title or current, extracted
    return current, _tidy(text)
