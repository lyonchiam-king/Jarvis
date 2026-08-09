"""The safety net for unattended demo generation.

Sixty of these run overnight and nobody reads the HTML before it goes live on
Firebase. Nothing here talks to the network -- every check is a pure function
over the string Gemini handed back, so it runs in milliseconds against every
site and never becomes the bottleneck the pipeline waits on.

Two checks matter more than the rest. ``truncated`` catches the single most
likely Gemini failure (see ``providers/sitegen.py``'s MAX_TOKENS handling --
this is the check that would also catch a truncation that somehow made it
past that guard). ``external_image`` is the one that keeps the pipeline
inside Google's terms: this design is deliberately photo-free, and the model
will still try to reach for an ``<img src="https://...">`` or a guessed Maps
photo URL unless something stops it -- a guessed image URL 404s before the
prospect even clicks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlsplit

# Void elements never get a closing tag, so a parser that pushed them onto an
# "open tags" stack would flag every well-formed page as having unclosed
# elements. https://html.spec.whatwg.org/multipage/syntax.html#void-elements
_VOID_ELEMENTS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
     "source", "track", "wbr"}
)

# The only hosts a self-contained demo may reach: Google Fonts (CSS + the
# actual font files) and Google's own domain, which covers the Maps embed.
# Everything else -- any photo, any script, any stock-image CDN -- is a
# guessed URL that will 404 before a prospect clicks it, or a caching-terms
# violation if it happens to resolve. See the module docstring.
_ALLOWED_HOSTS = frozenset({"fonts.googleapis.com", "fonts.gstatic.com", "www.google.com"})

# The prompt tells the model to write this exact marker where a real detail
# (a price, an address, a fact it wasn't given) is missing, and calls that the
# correct outcome rather than a fabrication. It must never be treated as a
# defect -- see `to_confirm_count`.
TO_CONFIRM_MARKER = "[TO CONFIRM]"

DISCLAIMER_TEXT = "Unofficial concept by COAI"

_IMG_SRC_RE = re.compile(r"<img\b[^>]*?\bsrc=[\"']([^\"']+)[\"']", re.IGNORECASE)
_BG_IMAGE_RE = re.compile(
    r"background-image\s*:\s*url\(\s*[\"']?([^\"')]+)[\"']?\s*\)", re.IGNORECASE
)
_SCRIPT_SRC_RE = re.compile(r"<script\b[^>]*?\bsrc=[\"']([^\"']+)[\"']", re.IGNORECASE)
_FORM_ACTION_RE = re.compile(r"<form\b[^>]*?\baction=[\"']([^\"']*)[\"']", re.IGNORECASE)
_H1_RE = re.compile(r"<h1\b", re.IGNORECASE)
_TITLE_RE = re.compile(r"<title\b[^>]*>\s*([^<\s][^<]*)", re.IGNORECASE)
_DOCTYPE_RE = re.compile(r"<!doctype\s+html", re.IGNORECASE)
_ROBOTS_NOINDEX_RE = re.compile(
    r'<meta\b(?=[^>]*name=["\']robots["\'])(?=[^>]*content=["\'][^"\']*noindex)',
    re.IGNORECASE,
)

_PLACEHOLDER_PATTERNS = (
    "lorem ipsum",
    "example.com",
    "your_api_key",
    "[insert",
    "```",
)


@dataclass(frozen=True)
class SiteProblem:
    code: str
    detail: str


class _OpenTagStack(HTMLParser):
    """Tracks unclosed tags so a page cut off mid-element is caught even when
    it happens not to trail off after ``</html>`` -- a stray truncation that
    lands mid-attribute or mid-tag can otherwise look like ordinary trailing
    whitespace to a naive string check.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() not in _VOID_ELEMENTS:
            self.stack.append(tag.lower())

    def handle_startendtag(self, tag: str, attrs) -> None:
        pass  # self-closing (<br/>) -- never pushed, nothing to pop either

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i] == tag:
                del self.stack[i]
                return


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")


def _host(url: str) -> str | None:
    """The URL's host, or ``None`` if the reference is one of the allowed
    "no host to check" shapes: relative, or a ``data:`` URI.
    """
    url = url.strip()
    if not url or url.startswith("data:"):
        return None
    if url.startswith("//"):
        url = "http:" + url
    parsed = urlsplit(url)
    if not parsed.netloc:
        return None  # relative -- same origin as the page itself
    return parsed.netloc.lower()


def _is_truncated(html: str) -> bool:
    stripped = html.strip()
    if not stripped.lower().endswith("</html>"):
        return True
    parser = _OpenTagStack()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 -- a parser that chokes is itself a bad page
        return True
    return bool(parser.stack)


def to_confirm_count(html: str) -> int:
    """How many `[TO CONFIRM]` markers the model left. Not a problem -- see
    `TO_CONFIRM_MARKER` -- just a number worth surfacing on the dashboard so
    Lyon can see how thin the evidence for a given site was.
    """
    return html.count(TO_CONFIRM_MARKER)


def check_site(
    html: str,
    *,
    phone: str,
    headline: str,
    business_name: str,
    min_bytes: int,
    max_bytes: int,
) -> list[SiteProblem]:
    problems: list[SiteProblem] = []

    if _is_truncated(html):
        problems.append(
            SiteProblem("truncated", "page does not end with a closed </html>")
        )

    size = len(html.encode("utf-8"))
    if size < min_bytes:
        problems.append(
            SiteProblem("too_small", f"{size} bytes, below the {min_bytes}-byte floor")
        )
    elif size > max_bytes:
        problems.append(
            SiteProblem("too_large", f"{size} bytes, above the {max_bytes}-byte ceiling")
        )

    if not _DOCTYPE_RE.search(html):
        problems.append(SiteProblem("no_doctype", "missing <!DOCTYPE html>"))

    if not _TITLE_RE.search(html):
        problems.append(SiteProblem("no_title", "missing a non-empty <title>"))

    h1_count = len(_H1_RE.findall(html))
    if h1_count != 1:
        problems.append(SiteProblem("h1_count", f"found {h1_count} <h1> elements, want exactly 1"))

    # Normalise both sides to digits before comparing -- "+60 4-226 1234" and
    # "+60 4 226 1234" are the same number and a naive substring check on the
    # raw strings fails it. An empty `phone` normalises to "", which is
    # trivially a substring of anything, so nothing to guard there: there is
    # simply no number to have lost.
    phone_digits = _digits(phone)
    if phone_digits and phone_digits not in _digits(html):
        problems.append(SiteProblem("missing_phone", f"{phone!r} not found on the page"))

    # A blank headline is a thin spec, not a page defect -- checking it would
    # pass vacuously (an empty string is a substring of everything) and give
    # false confidence that the hero copy is present. Skip outright instead.
    if headline.strip() and headline not in html:
        problems.append(SiteProblem("missing_headline", f"{headline!r} not found verbatim"))

    for pattern, label in (
        (_IMG_SRC_RE, "img"),
        (_BG_IMAGE_RE, "background-image"),
    ):
        for url in pattern.findall(html):
            host = _host(url)
            if host and host not in _ALLOWED_HOSTS:
                problems.append(SiteProblem("external_image", f"{label} points at {host}: {url}"))

    for url in _SCRIPT_SRC_RE.findall(html):
        host = _host(url)
        if host and host not in _ALLOWED_HOSTS:
            problems.append(SiteProblem("external_script", f"script src points at {host}: {url}"))

    for action in _FORM_ACTION_RE.findall(html):
        problems.append(SiteProblem("form_action", f"<form action={action!r}> -- no backend exists"))

    lowered = html.lower()
    for needle in _PLACEHOLDER_PATTERNS:
        if needle in lowered:
            problems.append(SiteProblem("placeholder", f"contains placeholder text: {needle!r}"))

    if not _ROBOTS_NOINDEX_RE.search(html):
        problems.append(
            SiteProblem("no_noindex", "missing <meta name=\"robots\" content=\"noindex\">")
        )

    if DISCLAIMER_TEXT not in html:
        problems.append(SiteProblem("no_disclaimer", f"missing the COAI footer line ({DISCLAIMER_TEXT!r})"))

    return problems


def repair_instruction(problems: list[SiteProblem]) -> str:
    """One corrective message to feed back to Gemini, in the same spirit as
    the JSON-schema repair in `providers/llm.py`: name exactly what is wrong,
    then ask for the whole document again rather than a patch -- a diff
    invites the model to leave the rest of a truncated or malformed page as
    it was.
    """
    if not problems:
        return ""
    lines = ["That page is not acceptable as-is. Specifically:"]
    lines += [f"- {p.code}: {p.detail}" for p in problems]
    lines += [
        "",
        "Reply with the complete, corrected HTML document again -- the whole "
        "page, not a diff or a snippet -- with every issue above fixed.",
    ]
    return "\n".join(lines)
