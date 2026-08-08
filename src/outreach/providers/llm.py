"""Turn raw signals about a business into a structured research brief.

Uses Claude Opus 5 with structured outputs, so the response is guaranteed to
match the schema rather than being parsed out of prose.

Compliance note: review text is passed to the model here and the *summary* is
what gets written to the Sheet. Raw review text is never persisted, which keeps
the pipeline inside Google's caching terms.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("outreach")

BRIEF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tone": {
            "type": "string",
            "description": "How this business presents itself, in 3-8 words.",
        },
        "services": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Specific services or products, from the evidence only.",
        },
        "price_tier": {
            "type": "string",
            "enum": ["budget", "mid-range", "premium", "unknown"],
        },
        "customer_praise": {
            "type": "string",
            "description": "What customers consistently compliment. One or two sentences.",
        },
        "customer_pain_points": {
            "type": "string",
            "description": (
                "Recurring complaints or friction, especially anything a website "
                "would fix. 'None evident' if the reviews are uniformly positive."
            ),
        },
        "suggested_website_angle": {
            "type": "string",
            "description": (
                "One concrete pitch angle for this specific business, naming the "
                "problem a site would solve. Two sentences at most."
            ),
        },
    },
    "required": [
        "tone",
        "services",
        "price_tier",
        "customer_praise",
        "customer_pain_points",
        "suggested_website_angle",
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You brief a founder who sells simple, well-built websites to small businesses \
that don't have one yet. He reads these briefs before writing a first outreach \
message, so they need to be specific enough to quote back to the owner.

Ground every field in the evidence you are given. If the evidence doesn't \
support a claim, say so plainly rather than inventing something plausible -- a \
brief that says "not evident from reviews" is useful, a fabricated detail is \
worse than useless because he may repeat it to the owner.

For the website angle: name the specific friction a site would remove for this \
business. "They get booking questions in the reviews and have no way to take \
bookings online" is useful. "A website would improve their online presence" is \
not.

Write for a reader, not a template. No marketing language, no filler.
"""


SITE_SPEC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "site_goal": {
            "type": "string",
            "description": "The one job this site must do for this business.",
        },
        "pages": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Page names. Keep it small -- 3 to 5 for an SME.",
        },
        "hero_headline": {"type": "string"},
        "hero_subcopy": {"type": "string"},
        "key_sections": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Sections on the home page, in order, each named and described.",
        },
        "primary_cta": {
            "type": "string",
            "description": "The single action the site pushes visitors toward.",
        },
        "visual_direction": {
            "type": "string",
            "description": (
                "Concrete direction: palette, type, mood. Fit the business, not "
                "a generic template."
            ),
        },
    },
    "required": [
        "site_goal",
        "pages",
        "hero_headline",
        "hero_subcopy",
        "key_sections",
        "primary_cta",
        "visual_direction",
    ],
    "additionalProperties": False,
}

SITE_SPEC_SYSTEM = """\
You spec small, sharp websites for local businesses that currently have none. \
The spec you produce is handed to a site builder, so it must be concrete: real \
headlines, named sections, a specific visual direction.

Design for the business you are given, not for a template. A barber shop and a \
tuition centre should not come out looking like the same site. Use the \
research brief -- if customers complain about booking, the site's job is \
booking; if they praise a specific person by name, the site should feature \
them.

Never invent facts about the business. If you don't know their prices, don't \
write a pricing page. Keep the site small -- three to five pages is right for \
an SME, and a single strong page often beats five weak ones.
"""

OUTREACH_COPY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "subject": {
            "type": "string",
            "description": "Email subject. Specific to this business, under 60 characters.",
        },
        "email_body": {
            "type": "string",
            "description": (
                "The email itself, plain text, no signature block -- the "
                "signature and compliance footer are appended separately. "
                "Under 150 words."
            ),
        },
        "whatsapp_message": {
            "type": "string",
            "description": (
                "A much shorter WhatsApp opener for the same business. Under 60 "
                "words, conversational, no subject line, no formal signoff."
            ),
        },
    },
    "required": ["subject", "email_body", "whatsapp_message"],
    "additionalProperties": False,
}

OUTREACH_COPY_SYSTEM = """\
You write first-contact messages from a founder to a small business owner who \
has never heard of him. Professional but warm -- the register of a competent \
neighbour, not a marketing department.

The whole message earns its keep in the first sentence, and that sentence must \
prove you actually looked at their business. Reference something specific and \
true from the research brief. Generic openers ("I hope this email finds you \
well", "I came across your business") get deleted unread.

Rules:
- Say the specific thing you noticed, then the specific thing you'd do about it.
- One ask, and make it small -- a reply, not a meeting.
- No hype, no superlatives, no "revolutionise", no exclamation marks.
- Never claim you are an existing customer, never invent a mutual connection, \
never imply a prior relationship. Everything you say must be true from the \
brief alone.
- The WhatsApp version is not the email pasted shorter -- it is a message a \
person would actually send on their phone.
"""


# Models that reject `output_config.effort` with a 400. Effort arrived with the
# 4.6 generation; the older small models predate it. Sending it to one of these
# fails every single lead, so the parameter is omitted rather than gambled.
_MODELS_WITHOUT_EFFORT = (
    "claude-haiku-4-5",
    "claude-haiku-3",
    "claude-sonnet-4-5",
    "claude-sonnet-3",
    "claude-opus-4-1",
    "claude-opus-4-0",
)


def supports_effort(model: str) -> bool:
    return not any(model.startswith(prefix) for prefix in _MODELS_WITHOUT_EFFORT)


class LLMError(Exception):
    """The model could not produce a brief for this lead."""


@dataclass(frozen=True)
class ResearchBrief:
    tone: str
    services: tuple[str, ...]
    price_tier: str
    customer_praise: str
    customer_pain_points: str
    suggested_website_angle: str

    @property
    def services_text(self) -> str:
        return ", ".join(self.services)


@dataclass(frozen=True)
class SiteSpec:
    site_goal: str
    pages: tuple[str, ...]
    hero_headline: str
    hero_subcopy: str
    key_sections: tuple[str, ...]
    primary_cta: str
    visual_direction: str


@dataclass(frozen=True)
class OutreachCopy:
    subject: str
    email_body: str
    whatsapp_message: str


def render_lovable_prompt(
    *,
    business_name: str,
    category: str,
    location: str,
    spec: SiteSpec,
) -> str:
    """Turn the structured spec into the text you paste into Lovable.

    Deterministic on purpose: the model decides *what* the site should be, this
    function decides how that is worded. Same spec in, same prompt out, which
    makes it reviewable and testable.
    """
    pages = "\n".join(f"- {p}" for p in spec.pages)
    sections = "\n".join(f"{i}. {s}" for i, s in enumerate(spec.key_sections, start=1))
    where = f" in {location}" if location else ""

    return f"""\
Build a small marketing website for {business_name}, a {category or 'local business'}{where}.

GOAL
{spec.site_goal}

PAGES
{pages}

HOME PAGE HERO
Headline: {spec.hero_headline}
Subcopy: {spec.hero_subcopy}
Primary call to action: {spec.primary_cta}

HOME PAGE SECTIONS (in order)
{sections}

VISUAL DIRECTION
{spec.visual_direction}

CONSTRAINTS
- Mobile first. Most visitors will arrive on a phone.
- Fast and light. No heavy frameworks, no carousels, no stock-photo filler.
- Every claim on the site must be one the business can stand behind. Do not
  invent prices, awards, testimonials, staff names, or years in business.
- Use placeholder text clearly marked [TO CONFIRM] wherever a real detail is
  needed but not supplied.
- Make the phone number and location impossible to miss.
"""


def build_site_spec_prompt(
    *,
    name: str,
    category: str,
    location: str,
    tone: str,
    services: str,
    price_tier: str,
    praise: str,
    pain_points: str,
    angle: str,
    social_handle: str = "",
    social_bio: str = "",
    social_post: str = "",
    phone: str = "",
    address: str = "",
) -> str:
    lines = [
        f"Business: {name}",
        f"Category: {category or 'unknown'}",
        f"Location: {location or 'unknown'}",
    ]
    if address:
        lines.append(f"Address: {address}")
    if phone:
        lines.append(f"Phone: {phone}")

    lines += [
        "",
        "Research brief:",
        f"- Tone: {tone or 'unknown'}",
        f"- Services: {services or 'unknown'}",
        f"- Price tier: {price_tier or 'unknown'}",
        f"- Customers praise: {praise or 'nothing recorded'}",
        f"- Customer pain points: {pain_points or 'none evident'}",
        f"- Suggested angle: {angle or 'none recorded'}",
    ]

    # The social profile matters twice over: it's the business's own voice
    # (better evidence of tone than anything inferred from reviews), and the
    # site should link to it rather than compete with it.
    if social_handle or social_bio or social_post:
        lines += ["", "Their own social presence:"]
        if social_handle:
            lines.append(f"- Handle: {social_handle}")
        if social_bio:
            lines.append(f"- Bio: {social_bio}")
        if social_post:
            lines.append(f"- Recent post: {social_post}")
        lines.append(
            "- Link this profile prominently on the site. It is where their "
            "existing customers already follow them, and their photo feed is "
            "content the site would otherwise lack."
        )
    else:
        lines += ["", "No social profile found -- do not invent or link one."]

    return "\n".join(lines)


def build_outreach_prompt(
    *,
    name: str,
    category: str,
    location: str,
    tone: str,
    services: str,
    praise: str,
    pain_points: str,
    angle: str,
    sender_name: str,
    sender_title: str,
    company: str,
    preview_url: str = "",
) -> str:
    lines = [
        f"Business: {name}",
        f"Category: {category or 'unknown'}",
        f"Location: {location or 'unknown'}",
        "",
        "Research brief:",
        f"- Tone: {tone or 'unknown'}",
        f"- Services: {services or 'unknown'}",
        f"- Customers praise: {praise or 'nothing recorded'}",
        f"- Customer pain points: {pain_points or 'none evident'}",
        f"- Website angle: {angle or 'none recorded'}",
        "",
        "Sender:",
        f"- Name: {sender_name or 'the sender'}",
        f"- Title: {sender_title or ''}",
        f"- Company: {company or ''}",
    ]
    if preview_url:
        lines += [
            "",
            f"A working preview site has already been built: {preview_url}",
            "Lead with that -- it is the strongest thing in the message.",
        ]
    else:
        lines += [
            "",
            "No site has been built yet. Do not imply one exists or offer a link.",
        ]
    return "\n".join(lines)


def build_prompt(
    *,
    name: str,
    category: str,
    address: str,
    rating: float | None,
    review_count: int,
    price_level: str,
    editorial_summary: str,
    opening_hours: tuple[str, ...] | list[str],
    reviews: list[tuple[int, str, str]],
    social_bio: str = "",
    social_post: str = "",
    website_status: str = "none",
) -> str:
    """Assemble the evidence block. Deterministic, so it is easy to test."""
    lines = [
        f"Business name: {name}",
        f"Category: {category or 'unknown'}",
        f"Address: {address or 'unknown'}",
        f"Google rating: {rating if rating is not None else 'none'} "
        f"from {review_count} reviews",
        f"Google price level: {price_level or 'not stated'}",
        f"Existing web presence: {website_status}",
    ]
    if opening_hours:
        lines.append("Opening hours: " + "; ".join(opening_hours))
    if editorial_summary:
        lines.append(f"Google summary: {editorial_summary}")
    if social_bio:
        lines.append(f"Social bio: {social_bio}")
    if social_post:
        lines.append(f"Recent social text: {social_post}")

    if reviews:
        lines.append("")
        lines.append("Reviews:")
        for stars, text, when in reviews:
            when_part = f", {when}" if when else ""
            lines.append(f"- [{stars}/5{when_part}] {text}")
    else:
        lines.append("")
        lines.append("Reviews: none available.")

    return "\n".join(lines)


def _extract_json(response) -> dict:
    """Pull the structured payload out of the response, checking stop_reason.

    Claude Opus 5 can decline a request outright -- checking ``stop_reason``
    before touching ``content`` is what stops that becoming an IndexError.
    """
    stop_reason = getattr(response, "stop_reason", None)
    if stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None) if details else None
        raise LLMError(f"Model declined this request (category: {category or 'unknown'})")
    if stop_reason == "max_tokens":
        raise LLMError("Response hit max_tokens before completing the brief")

    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            try:
                return json.loads(block.text)
            except json.JSONDecodeError as exc:
                raise LLMError(f"Model returned unparseable JSON: {exc}") from exc
    raise LLMError(f"No text block in response (stop_reason={stop_reason})")


def _to_brief(payload: dict) -> ResearchBrief:
    services = payload.get("services") or []
    if isinstance(services, str):
        services = [services]
    return ResearchBrief(
        tone=str(payload.get("tone", "")).strip(),
        services=tuple(str(s).strip() for s in services if str(s).strip()),
        price_tier=str(payload.get("price_tier", "unknown")).strip(),
        customer_praise=str(payload.get("customer_praise", "")).strip(),
        customer_pain_points=str(payload.get("customer_pain_points", "")).strip(),
        suggested_website_angle=str(payload.get("suggested_website_angle", "")).strip(),
    )


class BriefWriter:
    """Wraps the Anthropic client so stages don't import the SDK directly."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "claude-opus-5",
        effort: str = "medium",
        max_tokens: int = 6000,
        client: Any | None = None,
    ) -> None:
        if client is None:
            if not api_key:
                raise LLMError(
                    "ANTHROPIC_API_KEY is empty. Set it in .env -- Stage 2 needs it "
                    "to write research briefs."
                )
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover -- declared dependency
                raise LLMError("The `anthropic` package is not installed") from exc
            client = anthropic.Anthropic(api_key=api_key)

        self._client = client
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.call_count = 0
        self._warned_effort = False

    def _output_config(self, schema: dict[str, Any]) -> dict[str, Any]:
        config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
        if supports_effort(self.model):
            config["effort"] = self.effort
        elif not self._warned_effort:
            log.info(
                "%s does not support the effort parameter -- ignoring "
                "research.effort=%s for this model.",
                self.model, self.effort,
            )
            self._warned_effort = True
        return config

    def _call(self, system: str, prompt: str, schema: dict[str, Any]) -> dict:
        self.call_count += 1
        try:
            response = self._client.messages.create(
                model=self.model,
                # Thinking is on by default on Opus 5 and counts against
                # max_tokens, so the budget below leaves room for both.
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config=self._output_config(schema),
            )
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001 -- surfaces as a per-lead error
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc

        return _extract_json(response)

    def write(self, prompt: str) -> ResearchBrief:
        """Stage 2 -- the research brief."""
        return _to_brief(self._call(SYSTEM_PROMPT, prompt, BRIEF_SCHEMA))

    def write_site_spec(self, prompt: str) -> SiteSpec:
        """Stage 4 -- the structured site spec behind the Lovable prompt."""
        payload = self._call(SITE_SPEC_SYSTEM, prompt, SITE_SPEC_SCHEMA)
        return SiteSpec(
            site_goal=str(payload.get("site_goal", "")).strip(),
            pages=_as_tuple(payload.get("pages")),
            hero_headline=str(payload.get("hero_headline", "")).strip(),
            hero_subcopy=str(payload.get("hero_subcopy", "")).strip(),
            key_sections=_as_tuple(payload.get("key_sections")),
            primary_cta=str(payload.get("primary_cta", "")).strip(),
            visual_direction=str(payload.get("visual_direction", "")).strip(),
        )

    def write_outreach_copy(self, prompt: str) -> OutreachCopy:
        """Stage 5 -- the email and the WhatsApp opener."""
        payload = self._call(OUTREACH_COPY_SYSTEM, prompt, OUTREACH_COPY_SCHEMA)
        return OutreachCopy(
            subject=str(payload.get("subject", "")).strip(),
            email_body=str(payload.get("email_body", "")).strip(),
            whatsapp_message=str(payload.get("whatsapp_message", "")).strip(),
        )


def _as_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    return tuple(str(v).strip() for v in (value or []) if str(v).strip())
