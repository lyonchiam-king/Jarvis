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

# Host -> the platform's name as a person would say it. A bare handle is not
# enough for a site builder: "@espariohair" could be linked to any of these,
# and guessing wrong sends the salon's customers to a stranger's profile.
PLATFORMS: dict[str, str] = {
    "facebook.com": "Facebook",
    "fb.com": "Facebook",
    "fb.me": "Facebook",
    "instagram.com": "Instagram",
    "tiktok.com": "TikTok",
    "x.com": "X",
    "twitter.com": "X",
    "linkedin.com": "LinkedIn",
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "threads.net": "Threads",
    "threads.com": "Threads",
    "pinterest.com": "Pinterest",
    "linktr.ee": "Linktree",
    "beacons.ai": "Beacons",
    "wa.me": "WhatsApp",
    "api.whatsapp.com": "WhatsApp",
    "t.me": "Telegram",
    "shopee.com.my": "Shopee",
    "lazada.com.my": "Lazada",
    "linktree.com": "Linktree",
    "carrd.co": "Carrd",
    "business.site": "Google Business Site",
}

# Path segments that are the platform's own furniture, not a business profile.
_NOT_A_PROFILE = frozenset(
    {
        "profile.php", "p", "pages", "people", "sharer", "sharer.php", "share",
        "dialog", "plugins", "tr", "login", "signup", "explore", "reel",
        "reels", "watch", "hashtag", "search", "policies", "help", "about",
        "privacy", "legal", "settings", "home", "intent", "embed", "channel",
        "playlist", "results", "user",
    }
)

# Facebook appends a numeric page id to pages with no vanity URL:
# "Espariohairsalon-107771601074381". The digits are noise to a reader, so
# they are trimmed from the handle -- never from the URL, which needs them.
_FB_NUMERIC_SUFFIX = re.compile(r"-\d{6,}$")

_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


@dataclass(frozen=True)
class SocialLink:
    """One profile, in the three parts a site builder needs to link it."""

    platform: str
    handle: str
    url: str

    def display(self) -> str:
        """One line for the Sheet and the prompt: platform, handle, link."""
        parts = [self.platform]
        if self.handle:
            parts.append(self.handle)
        parts.append(self.url)
        return " ".join(parts)


@dataclass(frozen=True)
class SocialProfile:
    handle: str = ""
    bio: str = ""
    recent_post: str = ""
    status: str = "not attempted"
    emails: tuple[str, ...] = ()
    # Every profile found: the one on record, plus any the page links to.
    links: tuple[SocialLink, ...] = ()

    @property
    def found_anything(self) -> bool:
        return bool(
            self.handle or self.bio or self.recent_post or self.emails or self.links
        )

    @property
    def links_text(self) -> str:
        """All profiles, one per line, for a Sheet cell or a prompt block."""
        return "\n".join(link.display() for link in self.links)


def platform_for(url: str) -> str:
    """The platform's display name, or '' if this isn't a social URL."""
    if not url or not url.strip():
        return ""
    parsed = urlsplit(url.strip() if "://" in url else f"https://{url.strip()}")
    host = parsed.netloc.lower().removeprefix("www.").removeprefix("m.")
    host = host.split(":")[0]
    for domain, name in PLATFORMS.items():
        if host == domain or host.endswith("." + domain):
            return name
    return ""


def classify_social_url(url: str) -> SocialLink | None:
    """Turn a URL into platform + handle + canonical link, or None.

    Keeps the URL intact even when no handle can be read: a Facebook page
    reachable only as ``profile.php?id=123`` is still a real profile worth
    linking, and dropping it because it has no pretty name loses the lead's
    only web presence.
    """
    platform = platform_for(url)
    if not platform:
        return None

    cleaned = url.strip()
    parsed = urlsplit(cleaned if "://" in cleaned else f"https://{cleaned}")
    segments = [s for s in parsed.path.split("/") if s]

    handle = ""
    if segments and segments[0].lower() not in _NOT_A_PROFILE:
        first = segments[0].lstrip("@")
        if platform == "Facebook":
            first = _FB_NUMERIC_SUFFIX.sub("", first)
        if first:
            handle = "@" + first

    # Rebuild without query strings and tracking, but keep profile.php's id --
    # it is the only thing identifying that page.
    path = parsed.path.rstrip("/")
    query = f"?{parsed.query}" if segments and segments[0] == "profile.php" else ""
    host = parsed.netloc.lower().removeprefix("m.")
    return SocialLink(
        platform=platform,
        handle=handle,
        url=f"https://{host}{path}{query}",
    )


def extract_social_links(html: str, *, known: str = "") -> tuple[SocialLink, ...]:
    """Every distinct social profile linked from a page.

    Businesses cross-link their own profiles -- a Facebook page's header
    usually carries its Instagram -- so the page we already fetched is the
    cheapest place to find the rest of them.
    """
    found: dict[tuple[str, str], SocialLink] = {}

    first = classify_social_url(known) if known else None
    if first is not None:
        found[(first.platform, first.url.casefold())] = first

    for href in _HREF_RE.findall(html or ""):
        candidate = href.strip()
        if candidate.startswith("//"):
            candidate = "https:" + candidate
        if not candidate.lower().startswith(("http://", "https://")):
            continue
        link = classify_social_url(candidate)
        # A platform link with no handle is furniture (a share button, a login
        # prompt) unless it is the profile we already knew about.
        if link is None or not link.handle:
            continue
        found.setdefault((link.platform, link.url.casefold()), link)

    return tuple(found.values())


def handle_from_url(url: str) -> str:
    """Pull the profile handle out of a social URL, e.g. '@sunsalon'."""
    link = classify_social_url(url)
    if link is None or link.platform not in ("Facebook", "Instagram"):
        return ""
    return link.handle


def profiles_text(*urls: str) -> str:
    """Format raw URLs as ``SocialProfile.links_text`` would, with no lookup.

    Used where a real lookup never ran -- most often the Leads-tab fallback
    for a Research row whose ``Social Profiles`` cell predates that column,
    or was written before ``PLATFORMS`` learned a domain. A URL that is not a
    social link (a genuine website, an empty cell) classifies to nothing
    rather than being echoed as a competitor's link. Each argument may itself
    be several lines -- a hand-edited Sheet cell -- so blank and
    whitespace-only lines are dropped rather than raising.
    """
    found: dict[tuple[str, str], SocialLink] = {}
    for block in urls:
        for line in (block or "").splitlines():
            candidate = line.strip()
            if not candidate:
                continue
            link = classify_social_url(candidate)
            if link is None:
                continue
            found.setdefault((link.platform, link.url.casefold()), link)
    return "\n".join(link.display() for link in found.values())


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
    known = classify_social_url(url)
    known_links = (known,) if known is not None else ()

    if not url:
        return SocialProfile(status="no social url on record")

    try:
        page = web.fetch(url, session=session, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 -- a lookup must never break a run
        log.debug("Social lookup blew up for %s: %s", url, exc)
        return SocialProfile(
            handle=handle,
            links=known_links,
            status=f"lookup failed: {type(exc).__name__}",
        )

    if not page.ok:
        # A login wall still leaves us the profile we started from, which is
        # the single most useful thing to put on the site.
        return SocialProfile(handle=handle, links=known_links, status=page.status)

    # Meta tags survive login walls far more often than body text does, so
    # they are the primary source here and visible text is the fallback.
    bio = extract_meta_description(page.html)
    recent_post = "" if bio else _first_sentences(page.text)
    links = extract_social_links(page.html, known=url) or known_links

    return SocialProfile(
        handle=handle,
        bio=bio,
        recent_post=recent_post,
        status="ok" if (bio or recent_post) else "page served but empty",
        emails=page.emails,
        links=links,
    )


def _first_sentences(text: str, *, limit: int = 300) -> str:
    cleaned = (text or "").strip()
    return cleaned[:limit]
