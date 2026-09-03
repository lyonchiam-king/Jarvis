"""Best-effort page fetching and email extraction.

Nothing here is allowed to raise into a stage. Every lookup either returns a
result or returns a status string explaining why it didn't -- a Facebook page
that requires a login is a normal Tuesday, not an error condition.

Only publicly served pages are read. No login walls are bypassed, no rate
limits are evaded, and nothing behind authentication is touched.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import requests

log = logging.getLogger("outreach")

USER_AGENT = (
    "Mozilla/5.0 (compatible; SME-Outreach-Research/0.1; "
    "+contact via the business owner)"
)

# Deliberately conservative: requires a dot-separated TLD of 2+ letters and
# rejects the image-extension false positives that plague naive patterns.
_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,63}",
)

_MAILTO_RE = re.compile(r"mailto:([^\"'>?\s]+)", re.IGNORECASE)

# Addresses that are never a real business contact.
_JUNK_LOCALPARTS = frozenset(
    {"example", "test", "noreply", "no-reply", "donotreply", "sentry", "wixpress"}
)
_JUNK_DOMAINS = frozenset(
    {
        "example.com",
        "sentry.io",
        "wixpress.com",
        "schema.org",
        "w3.org",
        "googleapis.com",
        "gstatic.com",
        "cloudflare.com",
        "sentry-cdn.com",
    }
)
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico")

_TAG_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class PageResult:
    """Outcome of one fetch. ``status`` is always human-readable."""

    url: str
    status: str
    text: str = ""
    emails: tuple[str, ...] = ()
    # Raw (truncated) body. Kept because meta tags carry the useful signal on
    # social pages, and tag-stripping destroys them.
    html: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def is_plausible_email(candidate: str) -> bool:
    """Filter out the tracking, placeholder, and asset-filename noise."""
    address = candidate.strip().strip(".,;:").lower()
    if address.count("@") != 1:
        return False

    local, _, domain = address.partition("@")
    if not local or not domain or "." not in domain:
        return False
    if local in _JUNK_LOCALPARTS:
        return False
    if domain in _JUNK_DOMAINS:
        return False
    if any(domain.endswith("." + d) for d in _JUNK_DOMAINS):
        return False
    if address.endswith(_IMAGE_SUFFIXES):
        return False
    # "2x@image" style asset references.
    if local.isdigit():
        return False
    return True


def extract_emails(html: str) -> list[str]:
    """Pull candidate addresses from a page, mailto: links first.

    Order matters: an address behind a ``mailto:`` is one the business chose to
    publish, which is a much stronger signal than one scraped from body text.
    """
    found: list[str] = []
    seen: set[str] = set()

    for match in _MAILTO_RE.findall(html or ""):
        address = match.split("?")[0].strip().lower()
        if is_plausible_email(address) and address not in seen:
            seen.add(address)
            found.append(address)

    for match in _EMAIL_RE.findall(html or ""):
        address = match.strip().strip(".,;:").lower()
        if is_plausible_email(address) and address not in seen:
            seen.add(address)
            found.append(address)

    return found


def strip_html(html: str, *, limit: int = 4000) -> str:
    """Crude tag stripping -- enough to give the model readable page text."""
    without_scripts = _TAG_RE.sub(" ", html or "")
    text = _ANY_TAG_RE.sub(" ", without_scripts)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )
    return _WS_RE.sub(" ", text).strip()[:limit]


def fetch(
    url: str,
    *,
    session: requests.Session | None = None,
    timeout: float = 15.0,
    max_bytes: int = 500_000,
) -> PageResult:
    """Fetch a public page. Never raises -- failures come back as a status."""
    if not url or not url.strip():
        return PageResult(url=url, status="no url")

    parsed = urlsplit(url if "://" in url else f"https://{url}")
    if parsed.scheme not in ("http", "https"):
        return PageResult(url=url, status=f"unsupported scheme: {parsed.scheme}")

    client = session or requests.Session()
    try:
        response = client.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
            timeout=timeout,
            allow_redirects=True,
        )
    except requests.Timeout:
        return PageResult(url=url, status="timeout")
    except requests.RequestException as exc:
        return PageResult(url=url, status=f"unreachable: {type(exc).__name__}")

    if response.status_code == 404:
        return PageResult(url=url, status="not found (404)")
    if response.status_code in (401, 403):
        # Standard for Facebook and Instagram without a session.
        return PageResult(url=url, status="login required")
    if response.status_code >= 400:
        return PageResult(url=url, status=f"http {response.status_code}")

    content_type = response.headers.get("Content-Type", "")
    if content_type and "html" not in content_type and "text" not in content_type:
        return PageResult(url=url, status=f"not a page ({content_type.split(';')[0]})")

    body = response.text[:max_bytes]
    return PageResult(
        url=url,
        status="ok",
        text=strip_html(body),
        emails=tuple(extract_emails(body)),
        html=body,
    )
