"""Best-effort Facebook / Instagram lookup.

Expectations, stated plainly: most of these lookups fail. Facebook and
Instagram serve a login wall to anonymous requests, so a `login required`
status is the normal outcome, not a bug. That is exactly why this module never
raises -- a lead with no social data still gets a research brief from its
Google reviews.

Only publicly served pages are read. Nothing behind a login is accessed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import requests

from . import web

log = logging.getLogger("outreach")

_META_RE = re.compile(
    r"<meta[^>]+(?:property|name)=[\"'](og:description|og:title|description)[\"']"
    r"[^>]+content=[\"']([^\"']{0,600})[\"']",
    re.IGNORECASE,
)
_META_REVERSED_RE = re.compile(
    r"<meta[^>]+content=[\"']([^\"']{0,600})[\"']"
    r"[^>]+(?:property|name)=[\"'](og:description|og:title|description)[\"']",
    re.IGNORECASE,
)

_SOCIAL_HOSTS = ("facebook.com", "fb.com", "fb.me", "instagram.com")


@dataclass(frozen=True)
class SocialProfile:
    handle: str = ""
    bio: str = ""
    recent_post: str = ""
    status: str = "not attempted"
    emails: tuple[str, ...] = ()

    @property
    def found_anything(self) -> bool:
        return bool(self.handle or self.bio or self.recent_post or self.emails)


def handle_from_url(url: str) -> str:
    """Pull the profile handle out of a social URL, e.g. '@sunsalon'."""
    if not url:
        return ""
    parsed = urlsplit(url if "://" in url else f"https://{url}")
    host = parsed.netloc.lower().removeprefix("www.").removeprefix("m.")
    if not any(host == h or host.endswith("." + h) for h in _SOCIAL_HOSTS):
        return ""

    segments = [s for s in parsed.path.split("/") if s]
    if not segments:
        return ""
    # facebook.com/profile.php?id=... has no usable handle.
    if segments[0] in ("profile.php", "p", "pages", "people"):
        return ""
    return "@" + segments[0]


def extract_meta_description(html: str) -> str:
    """Read og:description / description, which often survives a login wall."""
    for pattern, content_group in ((_META_RE, 2), (_META_REVERSED_RE, 1)):
        for match in pattern.finditer(html or ""):
            content = match.group(content_group).strip()
            if content and len(content) > 20:
                return content[:600]
    return ""


def lookup(
    url: str,
    *,
    session: requests.Session | None = None,
    timeout: float = 15.0,
) -> SocialProfile:
    """Look up a social profile. Always returns -- never raises."""
    handle = handle_from_url(url)
    if not url:
        return SocialProfile(status="no social url on record")

    try:
        page = web.fetch(url, session=session, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 -- a lookup must never break a run
        log.debug("Social lookup blew up for %s: %s", url, exc)
        return SocialProfile(handle=handle, status=f"lookup failed: {type(exc).__name__}")

    if not page.ok:
        # A login wall still leaves us the handle, which is worth recording.
        return SocialProfile(handle=handle, status=page.status)

    # Meta tags survive login walls far more often than body text does, so
    # they are the primary source here and visible text is the fallback.
    bio = extract_meta_description(page.html)
    recent_post = "" if bio else _first_sentences(page.text)

    return SocialProfile(
        handle=handle,
        bio=bio,
        recent_post=recent_post,
        status="ok" if (bio or recent_post) else "page served but empty",
        emails=page.emails,
    )


def _first_sentences(text: str, *, limit: int = 300) -> str:
    cleaned = (text or "").strip()
    return cleaned[:limit]
