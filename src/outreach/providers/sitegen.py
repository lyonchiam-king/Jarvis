"""Gemini's native API, used to generate one self-contained HTML demo site per lead.

This deliberately does not go through ``_Writer`` in ``llm.py``. That base
class exists for JSON-schema calls a few hundred tokens long; a 32k-output-
token HTML page is a different shape of call (no schema, no JSON envelope,
much larger budget) and reusing it would mean bending the response parsing
around a case it was never built for. Gemini's own API is also not the
OpenAI-compatible one ``OpenAICompatibleWriter`` speaks -- the endpoint, the
auth header, and the response envelope (``candidates`` / ``finishReason``)
are all Google's own shape.

Sixty of these run overnight with nobody watching, and the site they produce
goes live to Firebase unread. Nothing here may hand back a partial page --
every failure mode below raises rather than returning ``""`` or a half page,
because a half-built page in front of a real business owner is the failure
this module exists to prevent.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

import requests

log = logging.getLogger("outreach")

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class SiteGenError(Exception):
    """Gemini could not produce a usable HTML page for this lead."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        # `errors.is_retryable` reads `status_code` off the exception (falling
        # back to `.response.status_code`) to decide whether the Errors tab
        # marks this lead safe to retry. A 429 here is a pacing problem, not a
        # bad prompt, so it must come back True.
        self.status_code = status_code


class SiteGenerator:
    """One HTML page per call, paced to Gemini's free-tier rate limit."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        max_output_tokens: int,
        min_interval_seconds: float = 6.5,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not api_key:
            raise SiteGenError(
                "GEMINI_API_KEY is empty. Set it in .env -- the demo builder "
                "needs it to call Gemini."
            )
        self.api_key = api_key
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.min_interval_seconds = min_interval_seconds
        self._session = session or requests.Session()
        self._sleep = sleep
        self._clock = clock
        self._last_call: float | None = None
        self.call_count = 0
        # Generation of a full page can genuinely take a while at 32k output
        # tokens; a short timeout would fail runs that were only ever slow,
        # not broken.
        self.timeout = 180.0

    def _pace(self) -> None:
        """Free tier is 10 requests/minute. Sixty leads run unattended overnight,
        so nobody is around to notice a 429 and back off by hand -- the pacing
        has to be built in rather than reactive.
        """
        if self._last_call is not None:
            elapsed = self._clock() - self._last_call
            remaining = self.min_interval_seconds - elapsed
            if remaining > 0:
                self._sleep(remaining)
        self._last_call = self._clock()

    def generate(self, prompt: str) -> str:
        self._pace()
        self.call_count += 1

        url = API_URL.format(model=self.model)
        headers = {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "maxOutputTokens": self.max_output_tokens,
                "temperature": 0.7,
            },
        }
        try:
            response = self._session.post(url, json=body, headers=headers, timeout=self.timeout)
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise SiteGenError(f"Could not reach Gemini: {exc}") from exc

        if response.status_code == 429:
            raise SiteGenError(
                f"Gemini rate-limited this request (HTTP 429) for model "
                f"{self.model}. The free tier allows 10 requests/minute -- "
                "if this keeps happening, raise build.min_interval_seconds.",
                status_code=429,
            )
        if response.status_code != 200:
            raise SiteGenError(
                f"Gemini returned HTTP {response.status_code} for model "
                f"{self.model}: {(response.text or '')[:300]}",
                status_code=response.status_code,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise SiteGenError(f"Gemini's response was not JSON: {exc}") from exc

        feedback = payload.get("promptFeedback") or {}
        block_reason = feedback.get("blockReason")
        if block_reason:
            raise SiteGenError(
                f"Gemini blocked the prompt before generating anything "
                f"(blockReason: {block_reason})."
            )

        candidates = payload.get("candidates") or []
        if not candidates:
            raise SiteGenError(
                f"Gemini returned no candidates for {self.model} "
                f"(promptFeedback: {feedback or 'none'})."
            )

        candidate = candidates[0] or {}
        finish_reason = candidate.get("finishReason")

        # Truncation is the single most likely failure of this call -- 32k
        # output tokens is a lot to ask for and it is easy to undershoot the
        # budget. Returning the partial string here is exactly the bug that
        # would put a half-built page in front of a business owner, so this
        # must raise rather than hand back whatever text did arrive.
        if finish_reason == "MAX_TOKENS":
            raise SiteGenError(
                f"Gemini's reply for {self.model} was cut off mid-page "
                "(finishReason: MAX_TOKENS) before reaching </html>. Raise "
                "build.max_output_tokens and try again."
            )
        if finish_reason == "SAFETY":
            raise SiteGenError(
                "Gemini blocked this reply on safety grounds (category: "
                f"{_safety_category(candidate)})."
            )

        parts = ((candidate.get("content") or {}).get("parts")) or []
        text = ""
        for part in parts:
            candidate_text = part.get("text") if isinstance(part, dict) else None
            if isinstance(candidate_text, str) and candidate_text:
                text = candidate_text
                break

        if not text:
            raise SiteGenError(
                f"Gemini's reply for {self.model} had no text in it "
                f"(finishReason: {finish_reason or 'unknown'})."
            )

        return _strip_fences(text)


def _safety_category(candidate: dict[str, Any]) -> str:
    for rating in candidate.get("safetyRatings") or []:
        if isinstance(rating, dict) and rating.get("blocked"):
            return str(rating.get("category") or "unknown")
    return "unknown"


def _strip_fences(text: str) -> str:
    """Unwrap a ```html ... ``` (or bare ```) fence around the page.

    Models emit these constantly for HTML the same way they do for JSON, and
    a fence left in place makes the file invalid -- it is no longer parseable
    HTML, it is a code block containing HTML. This is deliberately its own
    small stripper rather than a reuse of ``llm._strip_fences``, which hunts
    for the outermost ``{``/``}`` pair; that logic is meaningless for a
    document that starts with ``<!DOCTYPE html>``.
    """
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        first_newline = cleaned.find("\n")
        cleaned = cleaned[first_newline + 1 :] if first_newline != -1 else cleaned[3:]
    cleaned = cleaned.strip()
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return cleaned.strip()
