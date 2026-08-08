"""Google Places API (New) -- Text Search.

Uses ``places.googleapis.com/v1`` rather than the legacy Maps Places endpoints,
which Google deprecated for new customers in March 2025.

Two things about this API drive the design:

* **Field masks are mandatory and billing-relevant.** You must name every field
  you want, and the set you name selects the pricing SKU. ``websiteUri`` and
  ``rating`` push the request into the Pro tier -- but ``websiteUri`` is the
  entire point of this pipeline, so that cost is unavoidable. We ask for
  nothing beyond what we use.
* **Pages are capped at 20 results.** Anything larger is paginated with
  ``pageToken``.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Iterator
from urllib.parse import urlsplit

import requests

from ..models import WebsiteStatus

log = logging.getLogger("outreach")

SEARCH_TEXT_URL = "https://places.googleapis.com/v1/places:searchText"

# Exactly what discovery consumes -- nothing speculative.
SEARCH_FIELDS = (
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.nationalPhoneNumber",
    "places.internationalPhoneNumber",
    "places.websiteUri",
    "places.rating",
    "places.userRatingCount",
    "places.primaryTypeDisplayName",
    "places.primaryType",
    "places.googleMapsUri",
    "places.businessStatus",
    "nextPageToken",
)

_MAX_ATTEMPTS = 4
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class PlacesError(Exception):
    """A Places API call failed."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class PlaceResult:
    """A single Places record, normalised out of the API's nested shape."""

    place_id: str
    name: str
    address: str
    phone: str
    website_uri: str
    rating: float | None
    review_count: int
    category: str
    maps_url: str
    business_status: str


def classify_website(
    website_uri: str,
    social_domains: tuple[str, ...] | list[str],
) -> WebsiteStatus:
    """Decide whether a business genuinely has a website.

    Google puts whatever URL the owner supplied into ``websiteUri``, and for
    small businesses that is very often a Facebook page or a link-in-bio.
    Those are not websites, and the owners behind them are the warmest leads in
    the set -- they have already decided they want to be findable online.
    """
    if not website_uri or not website_uri.strip():
        return WebsiteStatus.NONE

    host = urlsplit(website_uri.strip()).netloc.lower()
    if not host:
        # Something like "facebook.com/foo" with no scheme.
        host = website_uri.strip().lower().split("/")[0]
    host = host.split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]

    if not host:
        return WebsiteStatus.NONE

    for domain in social_domains:
        d = domain.lower().strip()
        if not d:
            continue
        if host == d or host.endswith("." + d):
            return WebsiteStatus.SOCIAL_ONLY

    return WebsiteStatus.REAL


def _parse_place(raw: dict) -> PlaceResult | None:
    place_id = raw.get("id") or ""
    if not place_id:
        return None

    display = raw.get("displayName") or {}
    primary = raw.get("primaryTypeDisplayName") or {}

    rating = raw.get("rating")
    try:
        rating_value = float(rating) if rating is not None else None
    except (TypeError, ValueError):
        rating_value = None

    try:
        review_count = int(raw.get("userRatingCount") or 0)
    except (TypeError, ValueError):
        review_count = 0

    return PlaceResult(
        place_id=place_id,
        name=display.get("text") or "",
        address=raw.get("formattedAddress") or "",
        phone=raw.get("nationalPhoneNumber") or raw.get("internationalPhoneNumber") or "",
        website_uri=raw.get("websiteUri") or "",
        rating=rating_value,
        review_count=review_count,
        category=primary.get("text") or raw.get("primaryType") or "",
        maps_url=raw.get("googleMapsUri") or "",
        business_status=raw.get("businessStatus") or "",
    )


class PlacesClient:
    def __init__(
        self,
        api_key: str,
        *,
        session: requests.Session | None = None,
        timeout: float = 20.0,
    ) -> None:
        if not api_key:
            raise PlacesError(
                "PLACES_API_KEY is empty. Set it in .env -- see README "
                "'First-time Google Cloud setup'."
            )
        self.api_key = api_key
        self.session = session or requests.Session()
        self.timeout = timeout
        self.request_count = 0

    def _post(self, body: dict) -> dict:
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": ",".join(SEARCH_FIELDS),
        }
        delay = 1.0
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = self.session.post(
                    SEARCH_TEXT_URL, json=body, headers=headers, timeout=self.timeout
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt == _MAX_ATTEMPTS:
                    raise PlacesError(f"Places API unreachable: {exc}") from exc
                time.sleep(delay + random.uniform(0, 0.5))
                delay *= 2
                continue

            self.request_count += 1

            if response.status_code == 200:
                return response.json()

            if response.status_code in _RETRYABLE_STATUS and attempt < _MAX_ATTEMPTS:
                sleep_for = delay + random.uniform(0, 0.5)
                log.warning(
                    "Places API HTTP %s (attempt %d/%d); retrying in %.1fs",
                    response.status_code, attempt, _MAX_ATTEMPTS, sleep_for,
                )
                time.sleep(sleep_for)
                delay *= 2
                continue

            raise PlacesError(
                _explain_error(response.status_code, response.text),
                status_code=response.status_code,
            )

        raise PlacesError("Places API exhausted retries")

    def search_text(
        self,
        query: str,
        *,
        page_size: int = 20,
        language_code: str = "en",
        max_pages: int = 10,
    ) -> Iterator[PlaceResult]:
        """Yield places matching a free-text query, following pagination.

        ``max_pages`` is a hard stop so a bad config cannot run up a bill.
        """
        page_token: str | None = None
        for page in range(max_pages):
            body: dict[str, object] = {
                "textQuery": query,
                "pageSize": max(1, min(page_size, 20)),
                "languageCode": language_code,
            }
            if page_token:
                body["pageToken"] = page_token

            payload = self._post(body)
            places = payload.get("places") or []
            log.debug("Places page %d returned %d results", page + 1, len(places))

            for raw in places:
                parsed = _parse_place(raw)
                if parsed is not None:
                    yield parsed

            page_token = payload.get("nextPageToken")
            if not page_token:
                return


def _explain_error(status: int, text: str) -> str:
    """Turn a Places HTTP error into something the user can act on."""
    snippet = (text or "")[:400]
    if status == 400:
        return (
            "Places API rejected the request (HTTP 400). Usually a malformed "
            f"field mask or an empty text query.\nResponse: {snippet}"
        )
    if status == 403:
        return (
            "Places API returned 403. The three usual causes:\n"
            "  1. 'Places API (New)' is not enabled on the Cloud project\n"
            "  2. No active billing account is linked\n"
            "  3. The API key has an application or API restriction blocking this call\n"
            f"Response: {snippet}"
        )
    if status == 429:
        return f"Places API quota exceeded (HTTP 429).\nResponse: {snippet}"
    return f"Places API error (HTTP {status}): {snippet}"
