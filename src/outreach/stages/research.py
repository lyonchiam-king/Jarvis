"""Stage 2 -- research each discovered lead into a structured brief.

Run: ``outreach run research``

Resumable by construction. A run only picks up leads that are still at
`Discovered` and have no row in the Research tab, and it flushes results to the
Sheet every few leads. Interrupt it, re-run it, and it continues where it
stopped instead of re-buying Place Details and re-spending Claude tokens on
work it already did.

Per lead this costs one Places Details request (the Enterprise SKU, because of
reviews), up to two page fetches, and one Claude call -- which is why the batch
cap matters.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import requests

from .. import schema
from ..config import Config
from ..errors import ErrorLog
from ..models import utc_now
from ..providers import social as social_provider
from ..providers import web
from ..providers.llm import BriefWriter, ResearchBrief, build_prompt
from ..providers.places import PlaceDetails, PlacesClient
from ..sheets import SheetsClient
from ..stage import Stage, advance, parse, rank

log = logging.getLogger("outreach")


@dataclass
class ResearchReport:
    candidates: int = 0
    already_researched: int = 0
    not_ready: int = 0
    selected: int = 0
    briefed: int = 0
    social_found: int = 0
    emails_found: int = 0
    errors: int = 0
    llm_calls: int = 0
    places_requests: int = 0
    checkpoints: int = 0

    def summary_lines(self) -> list[str]:
        return [
            f"Leads on the Sheet        {self.candidates}",
            f"  already researched      {self.already_researched}  (skipped)",
            f"  not yet discovered      {self.not_ready}  (skipped)",
            f"  selected this run       {self.selected}",
            "",
            f"Briefs written            {self.briefed}",
            f"  social data found       {self.social_found}",
            f"  emails found            {self.emails_found}",
            f"Errors logged             {self.errors}",
            "",
            f"Claude calls              {self.llm_calls}",
            f"Places Details requests   {self.places_requests}",
            f"Sheet checkpoints         {self.checkpoints}",
        ]


@dataclass
class _Pending:
    """Results buffered since the last checkpoint."""

    research_rows: list[dict[str, object]] = field(default_factory=list)
    stage_updates: dict[str, object] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.research_rows)

    def clear(self) -> None:
        self.research_rows.clear()
        self.stage_updates.clear()


def select_leads(
    leads: dict[str, tuple[int, dict[str, str]]],
    researched_ids: set[str],
    *,
    batch_size: int,
) -> tuple[list[dict[str, str]], int, int]:
    """Pick the leads this run should process.

    Returns ``(selected, already_researched, not_ready)``. A lead is skipped if
    it already has a Research row, or if its Stage has advanced past
    `Discovered` -- both mean the work is done or no longer applies.
    """
    selected: list[dict[str, str]] = []
    already = 0
    not_ready = 0

    for lead_id, (_row_number, row) in leads.items():
        if lead_id in researched_ids:
            already += 1
            continue

        current = parse(row.get("Stage"))
        if current is None:
            # Blank or unrecognised: treat as freshly discovered so a typo in
            # the Sheet doesn't strand a lead forever.
            current = Stage.DISCOVERED
        if current is Stage.DISQUALIFIED:
            not_ready += 1
            continue
        if rank(current) > rank(Stage.DISCOVERED):
            # Already researched (or further along) but missing its Research
            # row -- someone deleted it, or an older run half-completed.
            already += 1
            continue

        if len(selected) < batch_size:
            selected.append(row)

    return selected, already, not_ready


def _discover_email(
    details: PlaceDetails,
    profile: social_provider.SocialProfile,
    *,
    config: Config,
    session: requests.Session,
) -> tuple[str, str]:
    """Find a contact email. Returns ``(email, source)``.

    Realistically this often comes back empty: businesses without a website
    usually have no published email either. That is the finding, not a failure
    -- it is why the pipeline generates WhatsApp text for every lead.
    """
    if not config.research.email_discovery:
        return "", "disabled"

    # A social page that served us content sometimes carries a contact address.
    if profile.emails:
        return profile.emails[0], "social_page"

    # If Places lists a real site (rare here, but leads get re-checked), read it.
    site = details.website_uri
    if site and "facebook.com" not in site and "instagram.com" not in site:
        page = web.fetch(site, session=session, timeout=config.research.request_timeout)
        if page.ok and page.emails:
            return page.emails[0], "website"

    return "", "not found"


def _brief_row(
    lead: dict[str, str],
    brief: ResearchBrief,
    profile: social_provider.SocialProfile,
    email: str,
    email_source: str,
) -> dict[str, object]:
    return {
        "Lead ID": lead.get("Lead ID", ""),
        "Business Name": lead.get("Business Name", ""),
        "Tone": brief.tone,
        "Services": brief.services_text,
        "Price Tier": brief.price_tier,
        "Customer Praise": brief.customer_praise,
        "Customer Pain Points": brief.customer_pain_points,
        "Suggested Website Angle": brief.suggested_website_angle,
        "Social Handle": profile.handle,
        "Social Bio": profile.bio,
        "Social Recent Post": profile.recent_post,
        "Social Lookup Status": profile.status,
        "Email Found": email,
        "Email Source": email_source,
        "Research Status": "ok",
        "Researched At": utc_now(),
    }


def _flush(
    sheets: SheetsClient,
    pending: _Pending,
    report: ResearchReport,
) -> None:
    """Write buffered results. Called every checkpoint and once at the end."""
    if not pending.research_rows:
        return
    sheets.upsert(schema.RESEARCH, pending.research_rows)
    sheets.update_column(schema.LEADS, "Stage", pending.stage_updates)
    report.checkpoints += 1
    log.info("Checkpoint: saved %d brief(s) to the Sheet.", len(pending.research_rows))
    pending.clear()


def run(
    config: Config,
    places: PlacesClient,
    sheets: SheetsClient,
    writer: BriefWriter,
    *,
    limit: int | None = None,
) -> ResearchReport:
    report = ResearchReport()
    errors = ErrorLog("research")
    session = requests.Session()

    leads = sheets.read_rows(schema.LEADS)
    researched = set(sheets.read_rows(schema.RESEARCH))
    report.candidates = len(leads)

    batch_size = min(limit or config.batch_size, config.batch_size)
    selected, already, not_ready = select_leads(
        leads, researched, batch_size=batch_size
    )
    report.already_researched = already
    report.not_ready = not_ready
    report.selected = len(selected)

    if not selected:
        log.info("Nothing to research -- every lead is done or not yet discovered.")
        return report

    log.info(
        "Researching %d lead(s); %d already done, %d not ready.",
        len(selected), already, not_ready,
    )

    pending = _Pending()

    for index, lead in enumerate(selected, start=1):
        lead_id = lead.get("Lead ID", "")
        name = lead.get("Business Name", "") or lead_id

        with errors.guard(lead_id=lead_id, business_name=name):
            log.info("[%d/%d] %s", index, len(selected), name)

            details = places.place_details(lead_id)

            profile = social_provider.SocialProfile(status="skipped")
            if config.research.social_lookup:
                website = lead.get("Website URL", "") or details.website_uri
                if lead.get("Website Status") == "social_only" and website:
                    profile = social_provider.lookup(
                        website,
                        session=session,
                        timeout=config.research.request_timeout,
                    )
                else:
                    profile = social_provider.SocialProfile(
                        status="no social profile on record"
                    )
            if profile.found_anything:
                report.social_found += 1

            email, email_source = _discover_email(
                details, profile, config=config, session=session
            )
            if email:
                report.emails_found += 1

            prompt = build_prompt(
                name=details.name or name,
                category=details.category or lead.get("Category", ""),
                address=details.address or lead.get("Address", ""),
                rating=details.rating,
                review_count=details.review_count,
                price_level=details.price_level,
                editorial_summary=details.editorial_summary,
                opening_hours=details.opening_hours,
                reviews=[
                    (r.rating, r.text, r.when)
                    for r in details.reviews[: config.research.max_reviews]
                ],
                social_bio=profile.bio,
                social_post=profile.recent_post,
                website_status=lead.get("Website Status", "none"),
            )

            brief = writer.write(prompt)

            pending.research_rows.append(
                _brief_row(lead, brief, profile, email, email_source)
            )
            pending.stage_updates[lead_id] = advance(
                lead.get("Stage"), Stage.RESEARCHED
            ).value
            report.briefed += 1

        # Checkpoint outside the guard: a lead that failed shouldn't stop the
        # successful ones before it from being saved.
        if len(pending) >= config.research.checkpoint_every:
            try:
                _flush(sheets, pending, report)
            except Exception as exc:  # noqa: BLE001
                log.error("Checkpoint write failed: %s", exc)
                errors.record(exc)

    try:
        _flush(sheets, pending, report)
    except Exception as exc:  # noqa: BLE001
        log.error("Final write failed: %s", exc)
        errors.record(exc)

    report.llm_calls = writer.call_count
    report.places_requests = places.request_count

    if len(errors):
        try:
            sheets.append(schema.ERRORS, errors.rows())
        except Exception as exc:  # noqa: BLE001
            log.error("Could not write Errors tab: %s", exc)
    report.errors = len(errors)

    return report
