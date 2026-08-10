"""Generated imagery for a demo site.

A page built only from colour and type reads as a wireframe, however good the
copy is. Gemini can make images, so it makes them here -- written next to the
page as ordinary files and referenced relatively, which is also the only kind
of image the site check allows.

What is generated, and what is deliberately not:

**Mood and texture.** Warm light on a bistro table, the grain of a chopping
board, an abstract wash in the palette. This is what stock photography does
for every other site, made to fit this business instead of bought off a shelf.

**Never the premises.** No generated interior, shopfront, staff, food they
actually serve, or work they actually did. A photorealistic image posing as
*their* place, inside a pitch that says "I looked at your business", is a
fabricated depiction of a real one -- and the owner is the one person
guaranteed to know it is not their room. That would cost the reply the whole
demo exists to earn. ``_forbidden`` enforces it on every prompt.

The images are honest placeholders and the outreach copy says so, which is
also the ask that gets a reply: send me your photos and I will drop them in.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from dataclasses import dataclass

from .providers.sitegen import SiteGenError, SiteGenerator

log = logging.getLogger("outreach")

# Extensions by what the API says it sent. Kept narrow on purpose: an unknown
# mime type means the reply was not an image, which is a failure, not a file
# to write with a guessed suffix.
_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}

# Subjects that would make an image a claim about this specific business
# rather than decoration. Checked against the prompt we send, not the reply --
# it is cheaper to refuse to ask than to judge what came back.
_FORBIDDEN = re.compile(
    r"\b("
    r"shopfront|storefront|store front|facade|"
    r"interior of|inside the (shop|salon|restaurant|cafe|café)|"
    r"the (owner|staff|team|chef|barber|stylist)s?\b|"
    r"their (shop|salon|premises|kitchen|dining room|team|staff)"
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GeneratedImage:
    """One image, plus the alt text the page should use for it."""

    filename: str
    data: bytes
    alt: str
    purpose: str

    def line(self) -> str:
        """How this image is described to the model writing the HTML."""
        return f"- {self.filename} -- {self.purpose}. alt text: \"{self.alt}\""


def _forbidden(prompt: str) -> str | None:
    """The banned subject in this prompt, if any."""
    found = _FORBIDDEN.search(prompt or "")
    return found.group(0) if found else None


def image_briefs(spec, business_name: str, category: str, *, count: int) -> list[tuple[str, str, str]]:
    """Turn a site spec into ``(purpose, prompt, alt)`` triples.

    Built from `visual_direction` and `palette` because those are the fields
    the spec model is strongest at, and because they are decisions about this
    business that were made from its actual reviews.
    """
    look = (getattr(spec, "visual_direction", "") or "").strip()
    palette = (getattr(spec, "palette", "") or "").strip()
    style = (
        f"Photographic, shallow depth of field, natural light. "
        f"Colour palette: {palette}. Mood: {look}"
    ).strip()

    briefs: list[tuple[str, str, str]] = [
        (
            "the hero background, sits behind the headline so keep the centre uncluttered",
            f"An atmospheric, abstract background image evoking a {category}. "
            f"Texture and light only -- no building, no room, no people, no signage, "
            f"no text. Generous empty space in the middle for a headline to sit over. "
            f"{style}",
            f"Atmospheric background evoking a {category}",
        ),
        (
            "a texture band between sections, decorative only",
            f"A close, tactile macro texture associated with a {category} -- "
            f"a material or surface, not a place and not a person. No text, no logos. "
            f"{style}",
            "Texture detail",
        ),
        (
            "an accent image beside the story section",
            f"A quiet still-life of a single object associated with a {category}, "
            f"on a plain surface. One object, no room around it, no people, no text. "
            f"{style}",
            "Still life detail",
        ),
    ]
    return briefs[: max(0, count)]


class ImageBudget:
    """Stops a run asking for images the account cannot currently make.

    Image models have their own, much smaller allowance than text. When that
    runs out every request 429s instantly, and without this a sixty-lead run
    spends a hundred and eighty calls discovering the same thing a hundred
    and eighty times -- against the same per-minute budget the pages need.

    One quota refusal closes it for the rest of the run. It is deliberately
    not persisted: quotas reset, and a run tomorrow should try again.
    """

    def __init__(self) -> None:
        self.open = True
        self.reason = ""

    def close(self, reason: str) -> None:
        if self.open:
            self.open = False
            self.reason = reason
            log.warning(
                "  Image generation is out of quota, so the rest of this run "
                "builds pages without images. %s",
                reason,
            )


def generate_for_site(
    generator: SiteGenerator,
    spec,
    *,
    business_name: str,
    category: str,
    count: int,
    budget: "ImageBudget | None" = None,
) -> list[GeneratedImage]:
    """Generate this page's images. Never raises -- a page without images is
    still a page, and losing the whole lead over decoration is the wrong trade.
    """
    if budget is not None and not budget.open:
        return []

    images: list[GeneratedImage] = []
    for index, (purpose, prompt, alt) in enumerate(
        image_briefs(spec, business_name, category, count=count), start=1
    ):
        banned = _forbidden(prompt)
        if banned:
            # Belt and braces: the briefs above are ours, so this only fires
            # if someone edits one into a claim about the real business.
            log.warning("  Skipping image %d: prompt names %r.", index, banned)
            continue
        try:
            data, mime = generator.generate_image(prompt)
        except SiteGenError as exc:
            if exc.status_code == 429 and budget is not None:
                # Not "busy" -- out of allowance. Every later request in this
                # run will fail the same way, so stop asking.
                budget.close(str(exc))
                return images
            log.info("  Image %d not generated (%s). The page still gets built.", index, exc)
            continue

        ext = _EXTENSIONS.get(mime)
        if ext is None:
            log.info("  Image %d came back as %s, which is not an image. Skipped.", index, mime)
            continue

        images.append(
            GeneratedImage(
                filename=f"image-{index}.{ext}", data=data, alt=alt, purpose=purpose
            )
        )
    return images


def decode_inline(part: dict) -> tuple[bytes, str]:
    """Pull the bytes out of one ``inlineData`` part."""
    inline = part.get("inlineData") or part.get("inline_data") or {}
    mime = str(inline.get("mimeType") or inline.get("mime_type") or "").lower()
    raw = inline.get("data") or ""
    if not raw:
        raise SiteGenError("Gemini returned an image part with no data in it.")
    try:
        return base64.b64decode(raw, validate=True), mime
    except (binascii.Error, ValueError) as exc:
        raise SiteGenError(f"Gemini's image data was not valid base64: {exc}") from exc


def manifest_lines(images: list[GeneratedImage]) -> str:
    """The block handed to the model writing the HTML."""
    if not images:
        return ""
    return "\n".join(image.line() for image in images)
