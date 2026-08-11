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


# A quota that refills on its own clock, versus one that is gone for the day.
# The distinction decides whether waiting helps, and getting it wrong either
# wastes an hour retrying a dead quota or throws away images that would have
# worked a minute later.
_PER_MINUTE = re.compile(r"per\s*minute", re.IGNORECASE)


class ImageBudget:
    """How the run reacts to Gemini refusing an image.

    Two different refusals wear the same HTTP 429:

    **Per-minute.** Refills on its own. The right answer is to stop asking
    for *this* page and try again on the next one, by which time a minute of
    page generation has passed. Closing the run over this would throw away
    every image for sixty leads because one minute was busy.

    **Per-day, or billing.** Gone until tomorrow, or until the account
    changes. Every later request fails identically, so the run stops asking
    entirely -- otherwise sixty leads spend a hundred and eighty calls
    learning the same thing, against the budget the pages themselves need.

    Neither is persisted: quotas reset, and tomorrow's run should try again.
    """

    def __init__(self, limit: int = 0) -> None:
        # A hard ceiling on images per run. This is the only guardrail that is
        # code rather than a note in the docs, and it exists because Vertex
        # spends real credits: a loop that misbehaves at 3am is Lyon's money,
        # not a free-tier 429. 0 means unlimited, which keeps the Developer
        # API path exactly as it was.
        self.limit = max(0, limit)
        self.used = 0
        self.open = True
        self.reason = ""
        self.skipped_pages = 0
        self._cooling = False

    def spent(self) -> None:
        """One image was generated. Closes the budget at the cap."""
        self.used += 1
        if self.limit and self.used >= self.limit:
            self.close(f"run image cap of {self.limit} reached")

    def start_page(self) -> None:
        """A new page. Whatever the last one hit, a minute-limit has moved on."""
        self._cooling = False

    @property
    def available(self) -> bool:
        return self.open and not self._cooling

    def refused(self, reason: str, *, quota: str) -> None:
        if _PER_MINUTE.search(quota or "") or (not quota and "per minute" in reason.lower()):
            self._cooling = True
            self.skipped_pages += 1
            log.info(
                "  Image quota is per-minute and currently full, so this page "
                "goes out without images. The next page tries again."
            )
            return
        self.close(reason)

    def close(self, reason: str) -> None:
        if self.open:
            self.open = False
            self.reason = reason
            log.warning(
                "  Image generation is out of quota for now, so the rest of "
                "this run builds pages without images. %s",
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
    if budget is not None:
        budget.start_page()
        if not budget.available:
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
                budget.refused(str(exc), quota=getattr(exc, "quota", ""))
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
        if budget is not None:
            budget.spent()
            if not budget.open:
                # The cap closed mid-page. Keep what this page already has --
                # a page with one image beats discarding it over a ceiling.
                return images
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
