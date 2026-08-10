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


# Statuses Google describes as temporary. 429 is included because the free
# tier's per-minute window clears on its own -- waiting is the fix.
_TRANSIENT = frozenset({429, 500, 502, 503, 504})


def _quota_details(response) -> tuple[str, float | None]:
    """The quota that ran out, and the delay Google asked for."""
    try:
        details = ((response.json() or {}).get("error") or {}).get("details") or []
    except Exception:  # noqa: BLE001 -- a 429 with an unreadable body is still a 429
        return "", None

    quota = ""
    delay: float | None = None
    for item in details:
        kind = str(item.get("@type", ""))
        if "QuotaFailure" in kind:
            violations = item.get("violations") or []
            if violations:
                quota = str(
                    violations[0].get("quotaId")
                    or violations[0].get("quotaMetric")
                    or ""
                )
        elif "RetryInfo" in kind:
            raw = str(item.get("retryDelay", "")).rstrip("s")
            try:
                delay = float(raw)
            except ValueError:
                delay = None
    return quota, delay


class SiteGenError(Exception):
    """Gemini could not produce a usable HTML page for this lead."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        # What Google asked us to wait, when it said. Honouring it beats a
        # fixed backoff that is either rude or needlessly slow.
        self.retry_after = retry_after
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
        image_model: str = "gemini-2.5-flash-image",
        min_interval_seconds: float = 6.5,
        max_attempts: int = 3,
        retry_base_seconds: float = 5.0,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not api_key:
            raise SiteGenError(
                "GEMINI_API_KEY is empty. Set it in .env -- the demo builder "
                "needs it to call Gemini."
            )
        self.image_model = image_model
        self.max_attempts = max(1, max_attempts)
        self.retry_base_seconds = retry_base_seconds
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

    def generate_image(self, prompt: str) -> tuple[bytes, str]:
        """One image. Returns ``(bytes, mime_type)``.

        Same transport, same pacing and retries as the page call -- image
        requests count against the same per-minute budget, so they have to
        share the throttle or a page plus its images trips the rate limit on
        its own.
        """
        from ..images import decode_inline

        payload = self._call_with_retry(
            self.image_model,
            {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
            },
        )
        candidates = payload.get("candidates") or []
        if not candidates:
            raise SiteGenError(
                f"Gemini returned no image for {self.image_model}. "
                f"{payload.get('promptFeedback', '')}"
            )
        for part in candidates[0].get("content", {}).get("parts") or []:
            if part.get("inlineData") or part.get("inline_data"):
                return decode_inline(part)
        raise SiteGenError(
            "Gemini replied without an image part -- it answered in text "
            f"instead. Check build.image_model ({self.image_model!r}) is an "
            "image model; `outreach list-models` shows the ones your key has."
        )

    def generate(self, prompt: str) -> str:
        """One page. Retries the failures Google itself calls temporary.

        503 and 500 mean the model is busy, not that the request was wrong --
        Google's own message says "please try again later". Failing the lead
        on the first one turns a five-second wait into a page that never got
        written, and on a sixty-lead run a brief spike would take out most of
        the batch.
        """
        return self._text_from(
            self._call_with_retry(
                self.model,
                {
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "maxOutputTokens": self.max_output_tokens,
                        "temperature": 0.7,
                    },
                },
            )
        )

    def _call_with_retry(self, model: str, body: dict) -> dict:
        last: SiteGenError | None = None
        for attempt in range(self.max_attempts):
            try:
                return self._post(model, body)
            except SiteGenError as exc:
                if exc.status_code not in _TRANSIENT or attempt == self.max_attempts - 1:
                    raise
                last = exc
                # Back off 5s, 15s, 45s. Google asks for "later", and hammering
                # a busy model is how a rate limit becomes a longer one.
                wait = exc.retry_after or self.retry_base_seconds * (3 ** attempt)
                log.info(
                    "  Gemini is busy (HTTP %s). Waiting %.0fs, then retry %d of %d.",
                    exc.status_code, wait, attempt + 2, self.max_attempts,
                )
                self._sleep(wait)
        raise last  # unreachable; the loop either returns or raises

    def _post(self, model: str, body: dict) -> dict:
        """One request. Pacing, status mapping and JSON parsing, no retrying.

        Shared by the page call and the image call: both spend from the same
        per-minute budget, so both must go through the same throttle or a
        page plus its images trips the rate limit on its own.
        """
        self._pace()
        self.call_count += 1

        url = API_URL.format(model=model)
        headers = {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}
        try:
            response = self._session.post(url, json=body, headers=headers, timeout=self.timeout)
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise SiteGenError(f"Could not reach Gemini: {exc}") from exc

        if response.status_code == 429:
            # Google puts the useful part in the error details: which quota
            # ran out, and how long it wants you to wait. "Rate limited" on
            # its own cannot tell a per-minute pause (wait) from a per-day
            # ceiling (come back tomorrow) or a model that needs billing --
            # and those need completely different reactions.
            quota, retry_after = _quota_details(response)
            raise SiteGenError(
                f"Gemini refused the request for {model}: over quota (HTTP 429)."
                + (f" Quota: {quota}." if quota else "")
                + (
                    f" Google asks for {retry_after:.0f}s."
                    if retry_after
                    else ""
                )
                + " A per-minute limit clears by waiting; a per-day one does"
                " not. Image models have their own, much smaller allowance"
                " than text and may need billing enabled on the Google Cloud"
                " project.",
                status_code=429,
                retry_after=retry_after,
            )
        if response.status_code == 404:
            # Model names move. A name that was correct when it was written
            # into a config file will one day 404, and the raw Google error
            # says only "not found" -- which reads like the whole setup is
            # broken rather than one string being stale.
            raise SiteGenError(
                f"Gemini has no model called {model!r}. Model names change; "
                "run `outreach list-models` to see the ones your key can "
                "actually use, then set the one you want under `build:` in "
                "your campaign file.",
                status_code=404,
            )

        if response.status_code in (500, 502, 503, 504):
            raise SiteGenError(
                f"Gemini is busy (HTTP {response.status_code}) for model "
                f"{model}. This is Google's side and usually clears in "
                "a minute or two.",
                status_code=response.status_code,
            )

        if response.status_code != 200:
            raise SiteGenError(
                f"Gemini returned HTTP {response.status_code} for model "
                f"{model}: {(response.text or '')[:300]}",
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

        return payload

    def _text_from(self, payload: dict) -> str:
        """The HTML out of a page reply, or a raise naming what went wrong."""
        feedback = payload.get("promptFeedback") or {}
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
