"""Stage 1 -- find businesses without a website and write them to the Leads tab.

Run: ``outreach run discovery``

Idempotent, and non-repeating. The Google ``place_id`` is the primary key, so
re-running can never duplicate a row or knock a lead you have already emailed
back to "Discovered".

Beyond that, a run **hunts for businesses you have never seen**: place_ids
already on the Leads tab are skipped and do not count toward the batch cap. So
"60 today, 60 more tomorrow" means 60 genuinely new businesses each time,
rather than the same 60 re-reported.

That only works if there are new businesses to find. Google caps a single text
query at roughly 60 results however you paginate, so one query is a finite
well. ``campaign.areas`` lists further places to search, and a run walks them
in order until the cap is full -- see ``search_queries`` in config.
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
from .. import phone as phone_lib
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
    already_known: int = 0
    areas_searched: int = 0
    exhausted: bool = False
    api_requests: int = 0
    upsert: UpsertResult = field(default_factory=UpsertResult)
    errors: int = 0

    def summary_lines(self) -> list[str]:
        lines = [
            f"Areas searched         {self.areas_searched}",
            f"Places examined        {self.seen}",
            f"  already on the Sheet {self.already_known}  (skipped)",
            f"  new, no website      {self.qualified}",
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
        if self.exhausted:
            lines += [
                "",
                "Every area in this campaign is used up -- Google has no more",
                "results to give for these searches. To keep finding leads, add",
                "areas or change the category in your campaign file.",
            ]
        return lines


def website_qualifies(
    status: WebsiteStatus,
    treat_social_as_no_website: bool,
    angle: str = "no-website",
) -> bool:
    """Whether this lead is one the campaign wants.

    The no-website angle keeps only businesses with nothing to lose: the demo
    site is the entire pitch, and someone who already has a site is being
    asked to replace one rather than to get one.

    The conversion angle argues about enquiries rather than about having a
    website at all, which is an argument that works whatever they have -- so
    it keeps everyone. Nothing is double-messaged as a result: discovery
    skips every place_id already on the Leads tab, so a business contacted
    under one angle is never picked up again by the other.
    """
    if angle == "conversion":
        return True
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
        # The lead's real country, read off its international phone number,
        # falling back to the campaign's setting only when Google gave no
        # number. Stamping the campaign country on every lead is what made a
        # UK number dial as Malaysian.
        # Three sources, most trustworthy first. Google's own ISO code for
        # this address settles it; the phone's international prefix covers a
        # listing without address components; the campaign setting is the
        # last resort and applies only when Google gave neither. That order
        # is the whole fix -- the campaign setting used to be the ONLY
        # source, so a Penang config copied for a UK run stamped MY on every
        # British lead and dialled them as Malaysian.
        country=(
            place.country
            or phone_lib.country_from_number(place.phone)
            or config.compliance.country_code
        ),
        discovered_at=now,
        last_refreshed=now,
    )


def _merge_existing(existing: dict[str, str], incoming: dict[str, object]) -> dict[str, object]:
    """Refresh Places fields without rewriting history or regressing the stage.

    ``Discovered At`` is when we first saw the lead and must survive a refresh;
    ``Stage`` may have been advanced by a later stage or by hand. The WhatsApp
    verdict columns must survive too: a fresh row carries neither, and the
    refresh upsert writes the whole automated run -- so without carrying them
    over, every refresh blanked the verdicts of every existing lead, and the
    reachability answer someone had already paid a lookup for was thrown away
    (research re-ran the check, or the draft held the lead for a check that
    would never come).
    """
    merged = dict(incoming)
    merged["Discovered At"] = existing.get("Discovered At") or incoming.get("Discovered At", "")
    merged["Stage"] = advance(existing.get("Stage"), Stage.DISCOVERED).value
    merged["WhatsApp Reachable"] = existing.get("WhatsApp Reachable", "")
    merged["WhatsApp Checked At"] = existing.get("WhatsApp Checked At", "")
    return merged


def run(
    config: Config,
    places: PlacesClient,
    sheets: SheetsClient | None,
    *,
    dry_run: bool = False,
    refresh: bool = False,
    limit: int | None = None,
) -> DiscoveryReport:
    """Search, filter, and write. ``sheets`` may be None when ``dry_run``.

    ``limit`` finds fewer than the campaign's cap on this one run -- useful for
    a first look, or a small day. It can only ever lower the cap, never raise
    it above ``limits.batch_size``.

    With ``refresh`` the run re-reads businesses already on the Sheet and
    updates their Places fields, instead of skipping them to hunt for new ones.
    Useful every few months when phone numbers and ratings have drifted; it
    finds nothing new, so it is not the daily mode.
    """
    if not dry_run and sheets is None:
        raise ValueError("sheets client is required unless dry_run=True")

    report = DiscoveryReport()
    errors = ErrorLog("discovery")
    d = config.discovery

    # Everything we have ever seen. Skipping these is what makes a second run
    # return a different 60 rather than the same 60.
    known_ids: set[str] = set()
    if sheets is not None and not refresh:
        try:
            known_ids = set(sheets.read_rows(schema.LEADS))
        except Exception as exc:  # noqa: BLE001
            # Better to risk re-reporting a lead than to abort the run: the
            # upsert is keyed on place_id, so nothing can duplicate either way.
            log.warning("Could not read existing leads (%s); searching without them.", exc)

    leads: list[Lead] = []
    seen_ids: set[str] = set()
    queries = config.campaign.search_queries
    cap = min(limit, config.discovery_cap) if limit else config.discovery_cap

    if known_ids:
        log.info("Skipping %d business(es) already on the Sheet.", len(known_ids))

    for query in queries:
        if len(leads) >= cap:
            break

        report.areas_searched += 1
        log.info("Searching Places for %r", query)
        found_here = 0

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

                    # The whole point of a daily run: never work the same
                    # business twice. Known leads do not consume the cap.
                    if place.place_id in known_ids:
                        report.already_known += 1
                        continue

                    if d.allowed_business_status and place.business_status:
                        if place.business_status not in d.allowed_business_status:
                            report.rejected_status += 1
                            continue

                    website_status = classify_website(place.website_uri, d.social_domains)
                    if not website_qualifies(
                        website_status,
                        d.treat_social_as_no_website,
                        config.outreach.angle,
                    ):
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
                    found_here += 1

                if len(leads) >= cap:
                    log.info("Reached batch cap of %d; stopping search.", cap)
                    break
        except Exception as exc:  # noqa: BLE001 -- search-level, not per-lead
            errors.record(exc)
            log.error("Places search failed for %r: %s", query, exc)
            continue

        if found_here == 0:
            log.info("  nothing new in this area.")

    # Walked every area and still short of the cap: this campaign's searches
    # have no more to give, and saying so is more useful than a quiet zero.
    report.exhausted = len(leads) < cap and report.areas_searched >= len(queries)

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
