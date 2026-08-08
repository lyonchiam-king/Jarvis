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

    def write(self, prompt: str) -> ResearchBrief:
        self.call_count += 1
        try:
            response = self._client.messages.create(
                model=self.model,
                # Thinking is on by default on Opus 5 and counts against
                # max_tokens, so the budget below leaves room for both.
                max_tokens=self.max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                output_config={
                    "effort": self.effort,
                    "format": {"type": "json_schema", "schema": BRIEF_SCHEMA},
                },
            )
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001 -- surfaces as a per-lead error
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc

        return _to_brief(_extract_json(response))
