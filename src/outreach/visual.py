"""The look of the infographic -- style, layout, character, palette.

Five things vary across the series and the content does not: the headline,
three problem/solution rows and the sign-off are the same shape every time.
That split is the whole design. The content is evidence (see ``gaps.py``);
this file is craft, so it is template data and campaign settings rather than
anything a model decides per lead -- the same reason the type scale and the
service list are not schema fields.

Two things are deliberately held constant across every style, because a
series that varies everything stops reading as one person's work: the coral
problem card and the mint solution card keep their colours, and the "by Lyon"
ribbon keeps its place. A campaign's palette governs the background and the
accents around them, never the cards themselves.

Rotation is a ring per industry, best pair first. ``next_pair`` returns the
first pair that is not the one the last lead in that industry got, which is
what stops ten salons in a row receiving the same graphic. The ring is
filtered by the campaign's allowed styles and layouts, so cutting those two
lists in the YAML cuts every industry's rotation at once -- one place to
narrow the series down once the good ones are known.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- Axis 1: illustration style -------------------------------------------
# Each value is the phrase handed to the image model. Written as a visual
# description rather than a style name alone: "retro comic" alone gets a
# comic *panel*, speech bubbles and all, which fights the card layout.
STYLES: dict[str, str] = {
    "flat-vector": (
        "clean flat vector illustration, bold simple shapes, no gradients, "
        "crisp edges"
    ),
    "paper-cut": (
        "layered paper-cut illustration, soft drop shadows between layers, "
        "matte textured paper"
    ),
    "claymation": (
        "soft 3D claymation look, rounded modelling-clay forms, gentle studio "
        "lighting"
    ),
    "chalkboard": (
        "hand-drawn chalkboard illustration, chalk texture and hand-lettered "
        "feel on a dark slate ground"
    ),
    "retro-comic": (
        "retro comic illustration with halftone dot shading and bold outlines, "
        "no speech bubbles and no comic panel borders"
    ),
    "watercolour": (
        "soft watercolour storybook illustration with gentle paper texture"
    ),
    "pop-art": (
        "bold pop-art illustration, flat saturated blocks of colour, heavy "
        "outlines"
    ),
}

# --- Axis 2: layout -------------------------------------------------------
# How the three rows are arranged. Each phrase has to place both cards and
# keep the numbering, because the structure is what makes the graphic
# readable at a glance -- the style may change, the two-column
# problem-then-solution reading must not.
LAYOUTS: dict[str, str] = {
    "rows": (
        "horizontal rows, one per step. In each row a coral card labelled "
        "\"THE PROBLEM WE SEE\" sits on the left and a mint-green card "
        "labelled \"HOW THE MARKET SOLVES IT\" on the right, linked by an "
        "arrow pointing right"
    ),
    "road": (
        "a winding road running left to right with one numbered stop per step. "
        "At each stop a coral card labelled \"THE PROBLEM WE SEE\" sits above "
        "the road and a mint-green card labelled \"HOW THE MARKET SOLVES IT\" "
        "below it, linked by an arrow"
    ),
    "flip-cards": (
        "cards side by side, one per step, each split into a coral top half "
        "labelled \"THE PROBLEM WE SEE\" and a mint-green bottom half labelled "
        "\"HOW THE MARKET SOLVES IT\", with a downward arrow between the halves"
    ),
    "sticky-notes": (
        "a whiteboard holding one numbered pair of sticky notes per step. Each "
        "pair has a coral note labelled \"THE PROBLEM WE SEE\" and a mint-green "
        "note labelled \"HOW THE MARKET SOLVES IT\", linked by a marker-pen arrow"
    ),
    "ladder": (
        "ascending steps climbing left to right, numbered in order. On each "
        "step a coral card labelled \"THE PROBLEM WE SEE\" and a mint-green "
        "card labelled \"HOW THE MARKET SOLVES IT\" sit side by side, linked "
        "by an arrow"
    ),
}

# --- Axis 3: the helper character -----------------------------------------
# Matched to the trade, not rotated: an owl reads as tuition and a fox reads
# as a trade, and swapping them every lead would make the graphic feel
# arbitrary rather than made for them.
CHARACTERS: dict[str, str] = {
    "lightbulb": "a small friendly cartoon lightbulb character with a smile",
    "robot": "a small friendly cartoon robot",
    "owl": "a small friendly cartoon owl holding a lightbulb",
    "fox": "a small friendly cartoon fox in work overalls",
    "compass": "a small friendly cartoon compass character",
    "magnifier": "a small friendly cartoon magnifying-glass character",
}

# --- Axis 4: palette ------------------------------------------------------
# The fallback when the lead's own brand colours are not known, which today
# is always: nothing in this pipeline reads a logo. Named per industry so the
# graphic at least belongs to its trade. Governs the ground and the accents;
# the coral and mint cards are fixed.
PALETTES: dict[str, str] = {
    "beauty": "blush pink, warm cream, soft charcoal and brushed brass",
    "tuition": "sage green, cream, warm navy and gold",
    "trades": "deep blue, off-white, slate grey and a single safety orange",
    "fitness": "deep teal, off-white, charcoal and one electric lime accent",
    "clinic": "soft teal, white, warm grey and pale gold",
    "food": "warm terracotta, cream, olive green and charcoal",
    "retail": "mustard yellow, dusty blue, cream and charcoal",
    "neutral": "muted teal, warm cream, soft charcoal and a little ochre",
}


@dataclass(frozen=True)
class Look:
    """One resolved set of choices for one lead."""

    style: str          # key into STYLES
    layout: str         # key into LAYOUTS
    character: str      # key into CHARACTERS
    palette: str        # key into PALETTES

    @property
    def pair(self) -> tuple[str, str]:
        """The part rotation cares about -- see ``next_pair``."""
        return (self.style, self.layout)


@dataclass(frozen=True)
class Industry:
    """A trade, its fixed character and palette, and its rotation ring."""

    key: str
    # Lower-case substrings matched against the lead's Google category. First
    # industry with a hit wins, so narrower trades are listed before broader
    # ones below.
    keywords: tuple[str, ...]
    character: str
    palette: str
    # (style, layout) pairs, best first. Walked in order by ``next_pair``.
    ring: tuple[tuple[str, str], ...]


INDUSTRIES: tuple[Industry, ...] = (
    Industry(
        key="tuition",
        keywords=("tuition", "tutor", "learning", "education", "school", "academy",
                  "language", "music lesson", "driving school"),
        character="owl",
        palette="tuition",
        ring=(("watercolour", "road"), ("paper-cut", "rows"),
              ("flat-vector", "ladder"), ("claymation", "sticky-notes")),
    ),
    Industry(
        key="clinic",
        keywords=("clinic", "dental", "dentist", "doctor", "medical", "physio",
                  "chiropract", "optician", "veterinar", "pharmac"),
        character="compass",
        palette="clinic",
        ring=(("claymation", "rows"), ("flat-vector", "rows"),
              ("paper-cut", "flip-cards"), ("watercolour", "ladder")),
    ),
    Industry(
        key="beauty",
        keywords=("salon", "hair", "nail", "beauty", "spa", "barber", "lash",
                  "massage", "aesthetic", "makeup", "make-up", "brow"),
        character="lightbulb",
        palette="beauty",
        ring=(("paper-cut", "rows"), ("watercolour", "flip-cards"),
              ("flat-vector", "road"), ("claymation", "ladder")),
    ),
    Industry(
        key="fitness",
        keywords=("gym", "fitness", "yoga", "pilates", "martial", "boxing",
                  "crossfit", "personal train", "dance studio", "swim"),
        character="robot",
        palette="fitness",
        ring=(("pop-art", "ladder"), ("flat-vector", "flip-cards"),
              ("retro-comic", "rows"), ("paper-cut", "road")),
    ),
    Industry(
        key="food",
        keywords=("restaurant", "cafe", "coffee", "bakery", "bar", "food",
                  "catering", "kopitiam", "dessert", "juice", "pizza", "noodle"),
        character="magnifier",
        palette="food",
        ring=(("chalkboard", "sticky-notes"), ("paper-cut", "rows"),
              ("watercolour", "road"), ("flat-vector", "flip-cards")),
    ),
    Industry(
        key="trades",
        keywords=("plumb", "electric", "aircon", "air cond", "hvac", "roof",
                  "contractor", "renovat", "repair", "builder", "handyman",
                  "pest", "cleaning", "locksmith", "mechanic", "workshop",
                  "tyre", "car wash", "mover"),
        character="fox",
        palette="trades",
        ring=(("retro-comic", "flip-cards"), ("flat-vector", "rows"),
              ("pop-art", "ladder"), ("paper-cut", "road")),
    ),
    Industry(
        key="retail",
        keywords=("shop", "store", "boutique", "florist", "grocer", "pet",
                  "stationer", "furniture", "optical", "jewel", "tailor",
                  "laundry", "print"),
        character="magnifier",
        palette="retail",
        ring=(("flat-vector", "rows"), ("paper-cut", "flip-cards"),
              ("pop-art", "road"), ("chalkboard", "sticky-notes")),
    ),
)

# Everything that matched nothing above. Not an error: Google's category
# strings are open-ended ("Permanent make-up clinic", "Nasi kandar
# restaurant"), and a trade nobody listed should still get a graphic rather
# than a crash.
DEFAULT_INDUSTRY = Industry(
    key="general",
    keywords=(),
    character="lightbulb",
    palette="neutral",
    ring=(("flat-vector", "rows"), ("paper-cut", "flip-cards"),
          ("watercolour", "road"), ("claymation", "ladder")),
)


def industry_for(category: str, fallback_category: str = "") -> Industry:
    """Which industry this lead's Google category reads as.

    ``fallback_category`` is the campaign's own category, used when the lead
    row has none -- a lead discovered before that column existed, or one
    Google returned without a primary type.
    """
    for text in (category or "", fallback_category or ""):
        needle = text.casefold()
        if not needle:
            continue
        for industry in INDUSTRIES:
            if any(word in needle for word in industry.keywords):
                return industry
    return DEFAULT_INDUSTRY


def next_pair(
    industry: Industry,
    previous: tuple[str, str] | None = None,
    *,
    styles: tuple[str, ...] = (),
    layouts: tuple[str, ...] = (),
) -> tuple[str, str]:
    """The next (style, layout) for this industry, never repeating ``previous``.

    ``styles`` and ``layouts`` are the campaign's allow-lists. Empty means
    "all of them". A ring filtered down to nothing still has to answer, so it
    falls back to the first allowed style with the first allowed layout --
    narrowing the series must never be able to stop it producing a graphic.
    """
    allowed_styles = tuple(s for s in (styles or tuple(STYLES)) if s in STYLES)
    allowed_layouts = tuple(l for l in (layouts or tuple(LAYOUTS)) if l in LAYOUTS)
    if not allowed_styles:
        allowed_styles = tuple(STYLES)
    if not allowed_layouts:
        allowed_layouts = tuple(LAYOUTS)

    ring = [
        pair
        for pair in industry.ring
        if pair[0] in allowed_styles and pair[1] in allowed_layouts
    ]
    if not ring:
        return (allowed_styles[0], allowed_layouts[0])

    for pair in ring:
        if pair != previous:
            return pair
    # Every pair in the ring equals `previous`, which can only happen when the
    # allow-lists have cut it down to one. Repeating is then the honest answer:
    # the alternative is reaching for a style the campaign just excluded.
    return ring[0]


def look_for(
    category: str,
    *,
    fallback_category: str = "",
    previous: tuple[str, str] | None = None,
    styles: tuple[str, ...] = (),
    layouts: tuple[str, ...] = (),
) -> Look:
    """Resolve every axis for one lead."""
    industry = industry_for(category, fallback_category)
    style, layout = next_pair(
        industry, previous, styles=styles, layouts=layouts
    )
    return Look(
        style=style,
        layout=layout,
        character=industry.character,
        palette=industry.palette,
    )


def describe(look: Look) -> dict[str, str]:
    """The four prompt phrases for a resolved look.

    Falls back to the neutral/first entry for a key that is not in a table,
    which is what makes a row written by an older version -- or edited by
    hand in the Sheet -- still render a prompt instead of raising.
    """
    return {
        "style": STYLES.get(look.style, STYLES["flat-vector"]),
        "layout": LAYOUTS.get(look.layout, LAYOUTS["rows"]),
        "character": CHARACTERS.get(look.character, CHARACTERS["lightbulb"]),
        "palette": PALETTES.get(look.palette, PALETTES["neutral"]),
    }


# --- Where the generated image lives --------------------------------------
# The graphic is made outside this pipeline and pasted back in, so unlike a
# built site there is no folder it naturally belongs beside. It goes in the
# state directory, which is the one writable place on every host -- a server's
# copy of the code is read-only and replaced on every deploy, so an image
# written next to the module would be thrown away at the next release.

VISUALS_DIRNAME = "visuals"

# What the browser and the relay will accept, mapped to the extension stored
# on disk. A graphic arrives as whatever the image tool exported, and guessing
# from the bytes is not this module's job -- the dashboard names the type it
# was given and this decides whether that type is allowed at all.
IMAGE_TYPES: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}


def visuals_dir():
    """The directory holding every pasted-back graphic."""
    from .paths import state_dir

    return state_dir() / VISUALS_DIRNAME


def image_rel_path(business_name: str, lead_id: str, content_type: str) -> str:
    """The path recorded in the Sheet: relative to the state directory.

    Relative on purpose. An absolute path baked into the Sheet would break the
    moment OUTREACH_STATE_DIR changed -- which is exactly what happens when a
    local install is moved to a server, the one case where the Sheet is the
    only thing that travels.
    """
    from .slug import site_slug

    suffix = IMAGE_TYPES.get(content_type, ".png")
    return f"{VISUALS_DIRNAME}/{site_slug(business_name, lead_id)}{suffix}"


def resolve_image(stored: str):
    """The image on disk for a stored path, or None.

    The stored value comes off the Sheet, which a person can type into. So it
    is resolved and then checked to be inside the visuals directory: without
    that, a hand-edited cell reading ``../credentials/token.json`` would be
    served by the dashboard and attached to a WhatsApp message. The same rule
    the screenshot route already follows by never accepting a filename off
    the wire at all.
    """
    from pathlib import Path

    stored = (stored or "").strip()
    if not stored:
        return None
    base = visuals_dir()
    try:
        candidate = (base.parent / stored).resolve()
        root = base.resolve()
    except OSError:
        return None
    if candidate != root and root not in candidate.parents:
        return None
    if not candidate.exists() or not candidate.stat().st_size:
        return None
    return candidate


def text_confirmed(row: dict[str, str] | None) -> bool:
    """Whether the words on this graphic have been checked by a person.

    The image tool is outside this pipeline, so nothing here can test that the
    text rendered correctly -- a garbled word on a graphic carrying the
    sender's name is the one failure that cannot be fixed after sending. The
    check goes where the only eye is, and this is the gate that makes it
    binding rather than advisory.
    """
    if not row:
        return False
    return (row.get("Text Confirmed") or "").strip().casefold() in {
        "yes",
        "true",
        "y",
        "confirmed",
    }


def confirmed_image(row: dict[str, str] | None):
    """The graphic for this lead, only if it exists AND its text was checked."""
    if not text_confirmed(row):
        return None
    return resolve_image((row or {}).get("Image File", ""))


def is_skipped(row: dict[str, str] | None) -> bool:
    """Whether this graphic was retired by hand in the Approval Status cell."""
    if not row:
        return False
    return (row.get("Approval Status") or "").strip().casefold() == "skip"


def is_specific(row: dict[str, str] | None) -> bool:
    """Whether the graphic names the business.

    Defaults to False for a blank cell. A row written before this column
    existed, or one whose cell was cleared, must not be described to the
    model as having been made for this business -- claiming it was is the
    lie the owner can spot from the picture itself.
    """
    if not row:
        return False
    return (row.get("Specific") or "").strip().casefold() in {"yes", "true", "y"}


# What the dashboard will accept for one graphic. A 16:9 infographic exports
# well under a megabyte; eight is slack for a lossless export at 4K. The real
# ceiling is the relay, which takes a 25MB JSON body and must carry the
# picture base64-encoded -- a third larger again -- so a file this size still
# sends. Refusing here, with a number, beats the relay answering 413 with an
# HTML error page the client can only report as a transport failure.
MAX_IMAGE_BYTES = 8 * 1024 * 1024


class ImageRejected(Exception):
    """The pasted image is not something that can be sent. Always actionable."""


def decode_data_url(data_url: str) -> tuple[bytes, str]:
    """Bytes and content type from a ``data:image/png;base64,...`` URL.

    This is how the page hands over a pasted or dropped file: the browser
    already has the bytes, so a data URL avoids a multipart form and the
    file-input dance on a phone, where paste is the natural gesture.
    """
    import base64
    import binascii

    text = (data_url or "").strip()
    if not text.startswith("data:"):
        raise ImageRejected("That is not an image. Paste or drop the picture itself.")
    header, _, payload = text.partition(",")
    if not payload:
        raise ImageRejected("The image came through empty. Try pasting it again.")
    content_type = header[len("data:"):].split(";")[0].strip().casefold()
    if content_type not in IMAGE_TYPES:
        raise ImageRejected(
            f"{content_type or 'That file'} cannot be sent on WhatsApp. "
            "Export the graphic as PNG, JPEG or WebP."
        )
    if "base64" not in header:
        raise ImageRejected("The image is not base64 encoded.")
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageRejected(f"The image data is damaged: {exc}") from exc
    check_image_bytes(raw)
    return raw, content_type


def check_image_bytes(raw: bytes) -> None:
    """Refuse an empty or oversized file, naming the number."""
    if not raw:
        raise ImageRejected("The image came through empty. Try pasting it again.")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ImageRejected(
            f"That image is {len(raw) / 1_048_576:.1f}MB, over the "
            f"{MAX_IMAGE_BYTES // 1_048_576}MB limit. Export it smaller -- a "
            "graphic this size will not reach WhatsApp."
        )


def is_fetchable_url(url: str) -> bool:
    """Whether this address is safe for the dashboard to download from.

    The dashboard runs on Lyon's machine, and on a server it runs inside a
    private network. So a URL typed into the page is a request made from
    *there*: an address on the local network, or a cloud metadata endpoint,
    would be fetched with whatever access that machine has and the bytes
    handed straight back. Only public http(s) addresses are allowed, and a
    host that resolves anywhere private is refused.

    Deliberately not a blocklist of strings. "127.0.0.1" is one spelling of
    loopback out of many, and ``localtest.me`` resolves to it from a name
    that looks public -- so the resolved address is what is checked.
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse

    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    if not infos:
        return False
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            return False
    return True


def save_image(business_name: str, lead_id: str, raw: bytes, content_type: str) -> str:
    """Write one graphic into the state directory, returning its stored path.

    Written under a ``.part`` name and moved into place, like the cloudflared
    download: an interruption must not leave a half-file that looks like a
    picture and then fails oddly at send time.
    """
    import os

    check_image_bytes(raw)
    if content_type not in IMAGE_TYPES:
        raise ImageRejected(f"{content_type} cannot be sent on WhatsApp.")
    rel = image_rel_path(business_name, lead_id, content_type)
    target = visuals_dir().parent / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    try:
        part.write_bytes(raw)
        os.replace(part, target)
    except OSError as exc:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        raise ImageRejected(f"Could not save the image: {exc}") from exc
    return rel
