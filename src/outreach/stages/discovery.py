"""Stage 1 -- find businesses without a website and write them to the Leads tab.

Run: ``outreach run discovery``

Idempotent. The Google ``place_id`` is the primary key, so re-running over the
same area refreshes existing rows in place rather than duplicating them, and a
lead you have already emailed never gets knocked back to "Discovered".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .. import schema
from ..config import Config
from ..errors import ErrorLog
from ..models import Lead, WebsiteStatus, utc_now
from ..providers.places import PlaceResult, PlacesClient, classify_website
from ..sheets import SheetsClient, UpsertResult
from ..stage import Stage, advance

log = logging.getLogger("outreach")


@dataclass
class DiscoveryReport:
    seen: int = 0
    qualified: int = 0
    rejected_has_website: int = 0
    rejected_status: int = 0
    rejected_rating: int = 0
    rejected_reviews: int = 0
    duplicates_in_run: int = 0
    api_requests: int = 0
    upsert: UpsertResult = field(default_factory=UpsertResult)
    errors: int = 0

    def summary_lines(self) -> list[str]:
        return [
            f"Places examined        {self.seen}",
            f"  no website / social  {self.qualified}",
            f"  rejected: has site   {self.rejected_has_website}",
            f"  rejected: status     {self.rejected_status}",
            f"  rejected: rating     {self.rejected_rating}",
            f"  rejected: reviews    {self.rejected_reviews}",
            f"  duplicates in run    {self.duplicates_in_run}",
            "",
            f"Leads inserted         {self.upsert.inserted}",
            f"Leads refreshed        {self.upsert.updated}",
            f"Places API requests    {self.api_requests}",
            f"Errors logged          {self.errors}",
        ]


def website_qualifies(status: WebsiteStatus, treat_social_as_no_website: bool) -> bool:
    """Whether this lead counts as 'no website' for the campaign."""
    if status is WebsiteStatus.NONE:
        return True
    if status is WebsiteStatus.SOCIAL_ONLY:
        return treat_social_as_no_website
    return False


def _to_lead(place: PlaceResult, config: Config, website_status: WebsiteStatus) -> Lead:
    now = utc_now()
    return Lead(
        lead_id=place.place_id,
        name=place.name,
        category=place.category or config.campaign.category,
        address=place.address,
        phone=place.phone,
        rating=place.rating,
        review_count=place.review_count,
        website_status=website_status,
        website_url=place.website_uri,
        maps_url=place.maps_url,
        campaign=config.campaign.name,
        country=config.compliance.country_code,
        discovered_at=now,
        last_refreshed=now,
    )


def _merge_existing(existing: dict[str, str], incoming: dict[str, object]) -> dict[str, object]:
    """Refresh Places fields without rewriting history or regressing the stage.

    ``Discovered At`` is when we first saw the lead and must survive a refresh;
    ``Stage`` may have been advanced by a later stage or by hand.
    """
    merged = dict(incoming)
    merged["Discovered At"] = existing.get("Discovered At") or incoming.get("Discovered At", "")
    merged["Stage"] = advance(existing.get("Stage"), Stage.DISCOVERED).value
    return merged


def run(
    config: Config,
    places: PlacesClient,
    sheets: SheetsClient | None,
    *,
    dry_run: bool = False,
) -> DiscoveryReport:
    """Search, filter, and write. ``sheets`` may be None when ``dry_run``."""
    if not dry_run and sheets is None:
        raise ValueError("sheets client is required unless dry_run=True")

    report = DiscoveryReport()
    errors = ErrorLog("discovery")
    d = config.discovery

    leads: list[Lead] = []
    seen_ids: set[str] = set()
    query = config.campaign.search_query

    log.info("Searching Places for %r", query)

    try:
        for place in places.search_text(
            query,
            page_size=d.page_size,
            language_code=config.campaign.language,
        ):
            report.seen += 1

            with errors.guard(lead_id=place.place_id, business_name=place.name):
                if place.place_id in seen_ids:
                    report.duplicates_in_run += 1
                    continue

                if d.allowed_business_status and place.business_status:
                    if place.business_status not in d.allowed_business_status:
                        report.rejected_status += 1
                        continue

                website_status = classify_website(place.website_uri, d.social_domains)
                if not website_qualifies(website_status, d.treat_social_as_no_website):
                    report.rejected_has_website += 1
                    continue

                if d.min_rating > 0 and (place.rating or 0.0) < d.min_rating:
                    report.rejected_rating += 1
                    continue

                if place.review_count < d.min_review_count:
                    report.rejected_reviews += 1
                    continue

                seen_ids.add(place.place_id)
                leads.append(_to_lead(place, config, website_status))
                report.qualified += 1

            if len(leads) >= d.max_results:
                log.info("Reached max_results=%d; stopping search.", d.max_results)
                break
    except Exception as exc:  # noqa: BLE001 -- a search-level failure, not per-lead
        errors.record(exc)
        log.error("Places search failed: %s", exc)

    report.api_requests = places.request_count

    if dry_run:
        log.info("Dry run -- not writing to the Sheet.")
        report.errors = len(errors)
        for lead in leads:
            log.info(
                "  %-40s %-18s %s",
                lead.name[:40], lead.phone or "(no phone)", lead.website_status.value,
            )
        return report

    if leads:
        rows = [lead.to_row(Stage.DISCOVERED.value) for lead in leads]
        report.upsert = sheets.upsert(schema.LEADS, rows, on_conflict=_merge_existing)

    # Errors are flushed last, and their own failure must not mask the run.
    if len(errors):
        try:
            sheets.append(schema.ERRORS, errors.rows())
        except Exception as exc:  # noqa: BLE001
            log.error("Could not write Errors tab: %s", exc)
    report.errors = len(errors)

    return report
