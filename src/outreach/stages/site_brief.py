"""Stage 4 -- turn each research brief into a ready-to-paste Lovable prompt.

Run: ``outreach run site-brief``

The prompt is the deliverable. Calling Lovable is optional convenience, and it
is off: ``lovable.enabled`` defaults to false, and even when true a project is
only created for rows where *you* have set ``Approval Status`` to ``Approved``.
That column is manual, so automation cannot approve its own spending.

Resumable like every other stage -- a lead that already has a Site Briefs row
costs nothing on a re-run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .. import schema
from ..config import Config
from ..errors import ErrorLog
from ..models import utc_now
from ..providers.llm import BriefWriter, render_lovable_prompt
from ..providers.lovable import LovableAdapter, LovableSkipped
from ..selection import select_pending
from ..sheets import SheetsClient
from ..stage import Stage, advance

log = logging.getLogger("outreach")


@dataclass
class SiteBriefReport:
    candidates: int = 0
    already_done: int = 0
    not_ready: int = 0
    selected: int = 0
    prompts_written: int = 0
    projects_created: int = 0
    build_skipped: int = 0
    errors: int = 0
    llm_calls: int = 0
    checkpoints: int = 0

    def summary_lines(self) -> list[str]:
        return [
            f"Leads on the Sheet        {self.candidates}",
            f"  already briefed         {self.already_done}  (skipped)",
            f"  not yet researched      {self.not_ready}  (skipped)",
            f"  selected this run       {self.selected}",
            "",
            f"Lovable prompts written   {self.prompts_written}",
            f"Lovable projects created  {self.projects_created}",
            f"  builds not attempted    {self.build_skipped}",
            f"Errors logged             {self.errors}",
            "",
            f"Claude calls              {self.llm_calls}",
            f"Sheet checkpoints         {self.checkpoints}",
        ]


@dataclass
class _Pending:
    rows: list[dict[str, object]] = field(default_factory=list)
    stage_updates: dict[str, object] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.rows)

    def clear(self) -> None:
        self.rows.clear()
        self.stage_updates.clear()


def _flush(sheets: SheetsClient, pending: _Pending, report: SiteBriefReport) -> None:
    if not pending.rows:
        return
    sheets.upsert(schema.SITE_BRIEFS, pending.rows)
    sheets.update_column(schema.LEADS, "Stage", pending.stage_updates)
    report.checkpoints += 1
    log.info("Checkpoint: saved %d site brief(s).", len(pending.rows))
    pending.clear()


def run(
    config: Config,
    sheets: SheetsClient,
    writer: BriefWriter,
    lovable: LovableAdapter,
    *,
    limit: int | None = None,
) -> SiteBriefReport:
    report = SiteBriefReport()
    errors = ErrorLog("site-brief")

    leads = sheets.read_rows(schema.LEADS)
    research = sheets.read_rows(schema.RESEARCH)
    existing = sheets.read_rows(schema.SITE_BRIEFS)
    report.candidates = len(leads)

    batch_size = min(limit or config.batch_size, config.batch_size)
    selected, already, not_ready = select_pending(
        leads,
        set(existing),
        requires=Stage.RESEARCHED,
        target=Stage.SITE_BRIEFED,
        batch_size=batch_size,
    )
    report.already_done = already
    report.not_ready = not_ready
    report.selected = len(selected)

    if not selected:
        log.info("Nothing to brief -- every researched lead already has a prompt.")
        return report

    log.info("Writing %d Lovable prompt(s).", len(selected))
    pending = _Pending()

    for index, lead in enumerate(selected, start=1):
        lead_id = lead.get("Lead ID", "")
        name = lead.get("Business Name", "") or lead_id

        with errors.guard(lead_id=lead_id, business_name=name):
            log.info("[%d/%d] %s", index, len(selected), name)

            brief = research.get(lead_id)
            if brief is None:
                raise ValueError(
                    "Lead is marked Researched but has no Research row -- "
                    "re-run `outreach run research` for it."
                )
            _row_number, research_row = brief

            spec = writer.write_site_spec(
                _spec_prompt(lead, research_row, config)
            )
            prompt = render_lovable_prompt(
                business_name=name,
                category=lead.get("Category", ""),
                location=config.campaign.location,
                spec=spec,
                # Passed straight through rather than via the model, so the
                # phone number on the site is the one Google has, character for
                # character. A hallucinated digit here costs a real customer.
                phone=lead.get("Phone", ""),
                address=lead.get("Address", ""),
                social_handle=research_row.get("Social Handle", ""),
            )

            # No build happens here, ever. A brand-new row is seeded "Pending",
            # and only you can change that to "Approved" -- so building is a
            # separate command (`run site-build`) you invoke afterwards.
            pending.rows.append(
                {
                    "Lead ID": lead_id,
                    "Business Name": name,
                    "Lovable Prompt": prompt,
                    # Manual column, seeded once on insert so the cell isn't
                    # ambiguously blank. Never rewritten afterwards -- so a
                    # later config change cannot retroactively approve a lead
                    # you already declined.
                    "Approval Status": (
                        "Approved" if config.lovable.auto_approve else "Pending"
                    ),
                    "Lovable Project ID": "",
                    "Preview URL": "",
                    "Created At": utc_now(),
                }
            )
            pending.stage_updates[lead_id] = advance(
                lead.get("Stage"), Stage.SITE_BRIEFED
            ).value
            report.prompts_written += 1

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

    if len(errors):
        try:
            sheets.append(schema.ERRORS, errors.rows())
        except Exception as exc:  # noqa: BLE001
            log.error("Could not write Errors tab: %s", exc)
    report.errors = len(errors)

    return report


@dataclass
class BuildReport:
    approved: int = 0
    already_built: int = 0
    built: int = 0
    skipped: int = 0
    errors: int = 0

    def summary_lines(self) -> list[str]:
        return [
            f"Rows marked Approved      {self.approved}",
            f"  already built           {self.already_built}  (skipped)",
            f"Projects created          {self.built}",
            f"Skipped                   {self.skipped}",
            f"Errors logged             {self.errors}",
        ]


def build_approved(
    config: Config,
    sheets: SheetsClient,
    lovable: LovableAdapter,
    *,
    limit: int | None = None,
) -> BuildReport:
    """Stage 4b -- build the sites you approved. Spends Lovable credits.

    Separate from prompt generation on purpose. Prompts are free and safe to
    regenerate; builds cost money, so they only ever happen on rows you have
    personally marked ``Approved`` in the Sheet, in a command you ran
    deliberately.
    """
    report = BuildReport()
    errors = ErrorLog("site-build")

    if not lovable.enabled:
        log.info(
            "lovable.enabled is false -- nothing will be built. "
            "Paste the prompts from the Site Briefs tab into Lovable by hand, "
            "or flip the flag once the connector is wired up."
        )
        return report

    briefs = sheets.read_rows(schema.SITE_BRIEFS)
    batch_size = min(limit or config.batch_size, config.batch_size)

    queue: list[dict[str, str]] = []
    for _lead_id, (_row, row) in briefs.items():
        if not lovable.should_build(row.get("Approval Status", "")):
            continue
        report.approved += 1
        if row.get("Lovable Project ID", "").strip():
            report.already_built += 1
            continue
        if len(queue) < batch_size:
            queue.append(row)

    if not queue:
        log.info("No approved-and-unbuilt rows.")
        return report

    project_ids: dict[str, object] = {}
    preview_urls: dict[str, object] = {}
    stage_updates: dict[str, object] = {}

    for row in queue:
        lead_id = row.get("Lead ID", "")
        name = row.get("Business Name", "") or lead_id

        with errors.guard(lead_id=lead_id, business_name=name):
            try:
                project = lovable.create_project(
                    name=name, prompt=row.get("Lovable Prompt", "")
                )
            except LovableSkipped as exc:
                report.skipped += 1
                log.info("  %s: %s", name, exc)
                continue

            project_ids[lead_id] = project.project_id
            preview_urls[lead_id] = project.preview_url
            stage_updates[lead_id] = Stage.SITE_BUILT.value
            report.built += 1

    if project_ids:
        sheets.update_column(schema.SITE_BRIEFS, "Lovable Project ID", project_ids)
        sheets.update_column(schema.SITE_BRIEFS, "Preview URL", preview_urls)
        sheets.update_column(schema.LEADS, "Stage", stage_updates)

    if len(errors):
        try:
            sheets.append(schema.ERRORS, errors.rows())
        except Exception as exc:  # noqa: BLE001
            log.error("Could not write Errors tab: %s", exc)
    report.errors = len(errors)

    return report


def _spec_prompt(
    lead: dict[str, str], research_row: dict[str, str], config: Config
) -> str:
    from ..providers.llm import build_site_spec_prompt

    return build_site_spec_prompt(
        name=lead.get("Business Name", ""),
        category=lead.get("Category", ""),
        location=config.campaign.location,
        address=lead.get("Address", ""),
        phone=lead.get("Phone", ""),
        tone=research_row.get("Tone", ""),
        services=research_row.get("Services", ""),
        price_tier=research_row.get("Price Tier", ""),
        praise=research_row.get("Customer Praise", ""),
        pain_points=research_row.get("Customer Pain Points", ""),
        angle=research_row.get("Suggested Website Angle", ""),
        social_handle=research_row.get("Social Handle", ""),
        social_bio=research_row.get("Social Bio", ""),
        social_post=research_row.get("Social Recent Post", ""),
    )
