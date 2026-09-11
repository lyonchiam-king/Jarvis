"""Vertex AI's image endpoint -- the IMAGE half of demo-site generation, moved
off the Gemini Developer API so it can be paid for with Google Cloud trial
credits (Google excludes AI Studio usage from those credits). The page call
stays exactly where it was, in ``sitegen.SiteGenerator.generate``; only the
picture moves.

This is a drop-in duck type for ``SiteGenerator.generate_image``: same
``(bytes, mime_type)`` return, same ``SiteGenError`` with ``.status_code`` and
``.quota`` set on the way out, because ``images.generate_for_site`` and
``ImageBudget`` already know how to read both off whichever generator they
were handed and must not need to learn a second shape.

``token_provider`` is a zero-argument callable rather than a captured bearer
string because an OAuth access token lives for one hour and a sixty-lead
overnight run outlives that; the caller is expected to pass something like
``lambda: auth.access_token(creds)`` so every request fetches (and, inside
``access_token``, refreshes if needed) a live token at the moment it is used.

Reuses ``SiteGenError`` and ``_quota_details`` from ``sitegen`` rather than
inventing a second exception type or a second QuotaFailure/RetryInfo parser --
Vertex returns the same ``google.rpc`` error-detail shapes the Developer API
does, so the parsing is identical even though the transport (OAuth bearer
token vs. an API key, a Cloud project path vs. a bare model name) is not.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

import requests

from ..images import decode_inline
from .sitegen import SiteGenError, _quota_details, _safety_category, _strip_fences

log = logging.getLogger("outreach")

# v1beta1, not v1. The publisher catalogue lists Gemini 3.x image models
# under v1beta1, and asking v1 for one returns 404 "no such model" -- which
# reads as a wrong name and sends you hunting through regions instead. The
# version is a constructor argument so it can be tried both ways.
_GLOBAL_URL = (
    "https://aiplatform.googleapis.com/{version}/projects/{project}/locations/global/"
    "publishers/google/models/{model}:generateContent"
)
_REGIONAL_URL = (
    "https://{region}-aiplatform.googleapis.com/{version}/projects/{project}/locations/"
    "{region}/publishers/google/models/{model}:generateContent"
)
# ListPublisherModels -- unlike the Developer API's per-key ListModels, this
# is scoped to the publisher ("google"), not to a project, so it needs no
# project in the path.
_LIST_URL = "https://aiplatform.googleapis.com/v1beta1/publishers/google/models"

# Same set sitegen retries: a rate limit that clears on its own, and server
# errors Google's own message calls temporary.
_TRANSIENT = frozenset({429, 500, 502, 503, 504})


def _strip_model_prefix(name: str) -> str:
    """Lyon will eventually paste a full ``publishers/google/models/x`` path
    out of Google's docs. 404ing on that instead of stripping it ourselves
    would be a bad first experience with a brand-new provider."""
    return name.removeprefix("publishers/google/models/").removeprefix("models/")


class VertexImageClient:
    """One image per call. No pacing by default -- Vertex images do not share
    the Developer API's ten-per-minute page budget, so a fixed sleep here
    would only make runs slower for nothing. A 429 still backs off, honouring
    whatever ``retry_after`` Google asks for.
    """

    def __init__(
        self,
        token_provider: Callable[[], str],
        *,
        project: str,
        region: str,
        model: str,
        image_size: str = "",
        api_version: str = "v1beta1",
        min_interval_seconds: float = 2.0,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        max_attempts: int = 3,
        timeout: float = 180.0,
    ) -> None:
        self._token_provider = token_provider
        # Vertex has its own per-minute limits, separate from AI Studio's.
        # I shipped this with no pacing on the assumption that credits meant
        # no rate limit; the first real batch proved otherwise on the second
        # lead. Two seconds is enough to space a spec, an image and a page
        # without making a sixty-lead run noticeably longer.
        self.min_interval_seconds = min_interval_seconds
        self._clock = clock
        self._last_call = 0.0
        self.api_version = api_version
        self.project = project
        self.region = region
        self.model = _strip_model_prefix(model)
        self.image_size = image_size
        self._session = session or requests.Session()
        self._sleep = sleep
        self.max_attempts = max(1, max_attempts)
        self.timeout = timeout
        self.call_count = 0

    @property
    def endpoint(self) -> str:
        """The exact URL, so ``outreach vertex-check`` can print what it is
        about to call rather than leaving Lyon to guess at a 403."""
        if self.region == "global":
            return _GLOBAL_URL.format(
                version=self.api_version, project=self.project, model=self.model
            )
        return _REGIONAL_URL.format(
            version=self.api_version,
            project=self.project,
            region=self.region,
            model=self.model,
        )

    def generate_image(self, prompt: str) -> tuple[bytes, str]:
        payload = self._call_with_retry(self._image_body(prompt))
        candidates = payload.get("candidates") or []
        if not candidates:
            raise SiteGenError(
                f"Vertex returned no image for {self.model}. "
                f"{payload.get('promptFeedback', '')}"
            )

        candidate = candidates[0] or {}
        finish_reason = candidate.get("finishReason")
        if finish_reason in ("SAFETY", "PROHIBITED_CONTENT"):
            raise SiteGenError(
                "Vertex blocked this image on safety grounds (finishReason: "
                f"{finish_reason}, category: {_safety_category(candidate)})."
            )

        for part in (candidate.get("content") or {}).get("parts") or []:
            if part.get("inlineData") or part.get("inline_data"):
                return decode_inline(part)

        raise SiteGenError(
            "Vertex replied without an image part -- it answered in text "
            f"instead. Check build.vertex_image_model ({self.model!r}) is an "
            "image model; `outreach vertex-models` shows the ones your "
            "project has."
        )

    def generate_text(self, prompt: str, *, max_output_tokens: int) -> str:
        """A text reply from the same endpoint, for the HTML page call.

        Vertex serves the text models too, so routing pages here as well
        takes the whole run off AI Studio's free tier -- which is the thing
        that was rate-limiting a batch after the images had already been
        paid for.
        """
        payload = self._call_with_retry(
            {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "maxOutputTokens": max_output_tokens,
                    "temperature": 0.7,
                },
            }
        )
        candidates = payload.get("candidates") or []
        if not candidates:
            raise SiteGenError(
                f"Vertex returned no candidates for {self.model} "
                f"({payload.get('promptFeedback', 'no feedback')})."
            )
        candidate = candidates[0] or {}
        if candidate.get("finishReason") == "MAX_TOKENS":
            raise SiteGenError(
                "Vertex cut the page off mid-document (MAX_TOKENS). Raise "
                "build.max_output_tokens."
            )
        for part in candidate.get("content", {}).get("parts") or []:
            if part.get("text"):
                return _strip_fences(part["text"])
        raise SiteGenError(
            f"Vertex replied without any text for {self.model}."
        )

    def list_models(self) -> list[str]:
        headers = {
            "Authorization": f"Bearer {self._token_provider()}",
            "x-goog-user-project": self.project,
        }
        try:
            response = self._session.get(
                _LIST_URL,
                headers=headers,
                params={"parent": "publishers/google"},
                timeout=self.timeout,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise SiteGenError(f"Could not reach Vertex: {exc}") from exc

        error = self._common_error(response)
        if error is not None:
            raise error
        if response.status_code != 200:
            raise SiteGenError(
                f"Could not list Vertex models (HTTP {response.status_code}). "
                "Set build.vertex_image_model in your campaign file to skip "
                "the lookup.",
                status_code=response.status_code,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise SiteGenError(f"Vertex's response was not JSON: {exc}") from exc

        models = payload.get("publisherModels") or payload.get("models") or []
        return [_strip_model_prefix(str(m.get("name", ""))) for m in models]

    # ------------------------------------------------------------------ retry

    def _call_with_retry(self, body: dict) -> dict:
        last: SiteGenError | None = None
        for attempt in range(self.max_attempts):
            try:
                return self._post(body)
            except SiteGenError as exc:
                if exc.status_code not in _TRANSIENT or attempt == self.max_attempts - 1:
                    raise
                last = exc
                # Same 5s/15s/45s backoff shape as sitegen -- hammering a busy
                # model is how a rate limit becomes a longer one.
                wait = exc.retry_after or 5.0 * (3 ** attempt)
                log.info(
                    "  Vertex is busy (HTTP %s). Waiting %.0fs, then retry %d of %d.",
                    exc.status_code, wait, attempt + 2, self.max_attempts,
                )
                self._sleep(wait)
        raise last  # unreachable; the loop either returns or raises

    def _image_body(self, prompt: str) -> dict:
        generation_config: dict = {"responseModalities": ["TEXT", "IMAGE"]}
        if self.image_size:
            # Only sent when set -- an imageConfig block on a model that does
            # not recognise the field is a 400, not a silent ignore.
            generation_config["imageConfig"] = {"imageSize": self.image_size}
        return {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": generation_config,
        }

    def _pace(self) -> None:
        if self.min_interval_seconds <= 0 or not self._last_call:
            self._last_call = self._clock()
            return
        remaining = self.min_interval_seconds - (self._clock() - self._last_call)
        if remaining > 0:
            self._sleep(remaining)
        self._last_call = self._clock()

    def _post(self, body: dict) -> dict:
        self._pace()
        self.call_count += 1

        headers = {
            "Authorization": f"Bearer {self._token_provider()}",
            "Content-Type": "application/json",
            # Not optional: user OAuth credentials need an explicit quota
            # project or Google returns a 403 asking for exactly this header.
            "x-goog-user-project": self.project,
        }
        try:
            response = self._session.post(
                self.endpoint, json=body, headers=headers, timeout=self.timeout
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise SiteGenError(f"Could not reach Vertex: {exc}") from exc

        error = self._common_error(response)
        if error is not None:
            raise error

        if response.status_code == 404:
            raise SiteGenError(
                f"Vertex has no model called {self.model!r} in region "
                f"{self.region!r}. Run `outreach vertex-models`.",
                status_code=404,
            )
        if response.status_code != 200:
            raise SiteGenError(
                f"Vertex returned HTTP {response.status_code} for model "
                f"{self.model}: {(response.text or '')[:300]}",
                status_code=response.status_code,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise SiteGenError(f"Vertex's response was not JSON: {exc}") from exc

        feedback = payload.get("promptFeedback") or {}
        block_reason = feedback.get("blockReason")
        if block_reason:
            raise SiteGenError(
                "Vertex blocked the prompt before generating anything "
                f"(blockReason: {block_reason})."
            )

        return payload

    def _common_error(self, response) -> SiteGenError | None:
        """The status codes that mean the same thing regardless of which
        endpoint was called -- quota, project setup, and credentials. Shared
        between the image call and ``list_models`` because Vertex evaluates
        these before it even looks at which request this is.
        """
        status = response.status_code
        if status == 200:
            return None
        text = response.text or ""

        if status == 429:
            quota, retry_after = _quota_details(response)
            return SiteGenError(
                f"Vertex refused the request for {self.project}: over quota "
                "(HTTP 429)."
                + (f" Quota: {quota}." if quota else "")
                + (f" Google asks for {retry_after:.0f}s." if retry_after else "")
                + " A per-minute limit clears by waiting; a per-day one does"
                " not.",
                status_code=429,
                retry_after=retry_after,
                quota=quota,
            )

        if status == 403 and (
            "SERVICE_DISABLED" in text or "has not been used in project" in text
        ):
            return SiteGenError(
                f"The Vertex AI API is not switched on for project "
                f"{self.project} yet, or it was switched on in the last few "
                "minutes and has not propagated. Console -> APIs & Services "
                "-> Library -> search `aiplatform.googleapis.com` -> Enable, "
                "then wait two minutes and run `outreach vertex-check` "
                "again.",
                status_code=403,
            )

        if status == 403 and "billing" in text.casefold():
            return SiteGenError(
                f"Vertex refused the request for project {self.project}: "
                "billing does not look enabled on this project. Console -> "
                "Billing -> link a billing account to this project, then run "
                "`outreach vertex-check` again.",
                status_code=403,
            )

        if status == 401 or (
            status == 403 and ("scope" in text.casefold() or "credential" in text.casefold())
        ):
            return SiteGenError(
                "Delete `credentials/vertex_token.json` and run `outreach "
                "vertex-check` to sign in again. Your Sheets and Gmail login "
                "is a different file and is not affected.",
                status_code=status,
            )

        if status in (500, 502, 503, 504):
            return SiteGenError(
                f"Vertex is busy (HTTP {status}) for project {self.project}. "
                "This is Google's side and usually clears in a minute or "
                "two.",
                status_code=status,
            )

        return None
