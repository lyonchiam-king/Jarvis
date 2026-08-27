"""Stage 4 -- turn each research brief into a ready-to-paste Lovable prompt.

Run: ``outreach run site-brief``

The prompt is the deliverable. Calling Lovable is optional convenience, and it
is off: ``lovable.enabled`` defaults to false, and even when true a project is
only created for rows where *you* have set ``Approval Status`` to ``Approved``.
That column is manual, so automation cannot approve its own spending.

Resumable like every other stage -- a lead that already has a Site Briefs row
costs nothing on a re-run.

Run: ``outreach run site-brief --rewrite`` for the exception to that: when the
prompt template itself changes, every existing brief was written against the
old one and a normal run will never touch it again (that's the resumability
above, working as designed). ``--rewrite`` targets exactly that backlog --
briefs Lovable hasn't built from yet -- and spends a model call per lead
because the whole point is a new prompt, non-deterministic like any other
model output, so a prompt you already read and approved will come back
different.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from .. import schema
from ..config import Config
from ..errors import ErrorLog
from ..models import utc_now
from ..providers import social
from ..providers.llm import BriefWriter, render_lovable_prompt
from ..providers.lovable import LovableAdapter, LovableSkipped
from ..selection import ORDERS, select_pending, select_rewrite
from ..sheets import SheetsClient
from ..stage import Stage, advance
from .reachability import is_contactable

log = logging.getLogger("outreach")


@dataclass
class SiteBriefReport:
    candidates: int = 0
    already_done: int = 0
    not_ready: int = 0
    unreachable: int = 0
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
            f"  no way to contact them  {self.unreachable}  (skipped)",
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
class RewriteReport:
    """Report for `run(..., rewrite=True)` -- a different queue, so a
    different shape than SiteBriefReport rather than a poor fit reused."""

    candidates: int = 0
    already_built: int = 0
    selected: int = 0
    rewritten: int = 0
    errors: int = 0
    llm_calls: int = 0
    checkpoints: int = 0

    def summary_lines(self) -> list[str]:
        return [
            f"Existing briefs           {self.candidates}",
            f"  already built           {self.already_built}  (skipped)",
            f"  selected this run       {self.selected}",
            "",
            f"Prompts rewritten         {self.rewritten}",
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


def _flush(
    sheets: SheetsClient,
    pending: _Pending,
    report: SiteBriefReport | RewriteReport,
    *,
    on_conflict: Callable[[dict[str, str], dict[str, object]], dict[str, object] | None]
    | None = None,
    write_stage: bool = True,
) -> None:
    if not pending.rows:
        return
    sheets.upsert(schema.SITE_BRIEFS, pending.rows, on_conflict=on_conflict)
    # Rewrite mode never advances Stage: every row it touches is already at or
    # past Site Briefed, so `advance` would be a no-op that still cost a round
    # trip to the Sheet.
    if write_stage:
        sheets.update_column(schema.LEADS, "Stage", pending.stage_updates)
    report.checkpoints += 1
    log.info("Checkpoint: saved %d site brief(s).", len(pending.rows))
    pending.clear()


def _preserve_rewrite_fields(
    existing_row: dict[str, str], incoming: dict[str, object]
) -> dict[str, object]:
    """`upsert`'s on_conflict for a rewrite: only the prompt and name are new.

    `Lovable Project ID` and `Preview URL` are AUTOMATED columns, so a plain
    upsert over an existing row would blank them and sever the link to a site
    Lovable already built -- the exact bug this exists to prevent.
    `Created At` is preserved too: it marks when the row was first written,
    not when it was last rewritten, and there is no "rewritten at" column to
    record the difference (the schema is append-only; adding one is a
    decision for later, not a side effect of this change).
    """
    return {
        **incoming,
        "Lovable Project ID": existing_row.get("Lovable Project ID", ""),
        "Preview URL": existing_row.get("Preview URL", ""),
        "Created At": existing_row.get("Created At", ""),
    }


def run(
    config: Config,
    sheets: SheetsClient,
    writer: BriefWriter,
    lovable: LovableAdapter,
    *,
    limit: int | None = None,
    social_first: bool | None = None,
    rewrite: bool = False,
) -> SiteBriefReport | RewriteReport:
    if rewrite:
        return _run_rewrite(config, sheets, writer, limit=limit, social_first=social_first)

    report = SiteBriefReport()
    errors = ErrorLog("site-brief")

    leads = sheets.read_rows(schema.LEADS)
    research = sheets.read_rows(schema.RESEARCH)
    existing = sheets.read_rows(schema.SITE_BRIEFS)
    report.candidates = len(leads)

    # The contactability gate. Building a site is the most expensive thing
    # this pipeline does, and it used to happen before anyone knew whether
    # the lead could be reached at all -- so a landline with no email cost a
    # full generation and its tokens for a message that could never be sent.
    # A lead is dropped here only when it has no channel whatsoever: not on
    # WhatsApp AND no email. Unchecked leads pass through, because a lookup
    # that has not run yet is not evidence of anything.
    contactable = {}
    for lead_id, entry in leads.items():
        row = research.get(lead_id)
        if row is None or is_contactable(row[1]):
            contactable[lead_id] = entry
    report.unreachable = len(leads) - len(contactable)
    leads = contactable

    batch_size = min(limit or config.batch_size, config.batch_size)
    # Same preference as research. Ranking only the research queue would leave
    # leads researched before that setting existed sitting at the front here.
    prioritise = (
        config.research.social_first if social_first is None else social_first
    )
    selected, already, not_ready = select_pending(
        leads,
        set(existing),
        requires=Stage.RESEARCHED,
        target=Stage.SITE_BRIEFED,
        batch_size=batch_size,
        prefer=ORDERS[config.research.order] if prioritise else None,
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

            # The Research row's own lookup wins outright when it has one --
            # it may carry profiles the lead's current Website URL doesn't
            # (a page found by crawling, not just the one column). This is
            # strictly a rendering fallback: nothing here is written back to
            # the Research tab, so a blank cell stays visible and eligible
            # for a real backfill later instead of being permanently masked.
            # Gated on research.social_lookup: that flag is the user's one
            # switch for "don't put social links in front of the builder",
            # and a fallback that ignored it would keep doing the thing they
            # just turned off.
            social_profiles = research_row.get("Social Profiles", "").strip()
            if not social_profiles and config.research.social_lookup:
                social_profiles = social.profiles_text(lead.get("Website URL", ""))

            spec = writer.write_site_spec(
                _spec_prompt(lead, research_row, config, social_profiles)
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
                social_profiles=social_profiles,
                # The builder is told to open these and work from what the
                # business actually looks like, rather than from adjectives.
                maps_url=lead.get("Maps URL", ""),
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


def _run_rewrite(
    config: Config,
    sheets: SheetsClient,
    writer: BriefWriter,
    *,
    limit: int | None,
    social_first: bool | None,
) -> RewriteReport:
    """Push a prompt-template improvement out to leads select_pending would
    now treat as done -- see the module for why a normal run cannot do this.
    """
    report = RewriteReport()
    errors = ErrorLog("site-brief-rewrite")

    leads = sheets.read_rows(schema.LEADS)
    research = sheets.read_rows(schema.RESEARCH)
    briefs = sheets.read_rows(schema.SITE_BRIEFS)
    report.candidates = len(briefs)

    batch_size = min(limit or config.batch_size, config.batch_size)
    prioritise = (
        config.research.social_first if social_first is None else social_first
    )
    selected, already_built = select_rewrite(
        leads,
        briefs,
        batch_size=batch_size,
        prefer=ORDERS[config.research.order] if prioritise else None,
    )
    report.already_built = already_built
    report.selected = len(selected)

    if not selected:
        log.info("Nothing to rewrite -- no existing brief is both unbuilt and eligible.")
        return report

    # Selection cannot tell a lead this run already rewrote from one it
    # hasn't reached yet -- there is no "rewritten at" marker, and inventing
    # one would need a schema column (columns are append-only, so that's a
    # real decision, not a side effect of this change). So an interrupted
    # rewrite that's re-run repeats any lead it already got to in this batch,
    # bounded by --limit same as everything else.
    log.info(
        "Rewriting %d Lovable prompt(s). Re-running repeats any lead already "
        "rewritten in this batch -- bound it with --limit if that matters.",
        len(selected),
    )
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

            social_profiles = research_row.get("Social Profiles", "").strip()
            if not social_profiles and config.research.social_lookup:
                social_profiles = social.profiles_text(lead.get("Website URL", ""))

            spec = writer.write_site_spec(
                _spec_prompt(lead, research_row, config, social_profiles)
            )
            prompt = render_lovable_prompt(
                business_name=name,
                category=lead.get("Category", ""),
                location=config.campaign.location,
                spec=spec,
                phone=lead.get("Phone", ""),
                address=lead.get("Address", ""),
                social_handle=research_row.get("Social Handle", ""),
                social_profiles=social_profiles,
                maps_url=lead.get("Maps URL", ""),
            )

            # Only the two fields that are actually new. Everything else --
            # Approval Status (manual; never a stage's to write), Lovable
            # Project ID, Preview URL, Created At -- is left for on_conflict
            # to carry over from the row already in the Sheet.
            pending.rows.append(
                {
                    "Lead ID": lead_id,
                    "Business Name": name,
                    "Lovable Prompt": prompt,
                }
            )
            report.rewritten += 1

        if len(pending) >= config.research.checkpoint_every:
            try:
                _flush(
                    sheets, pending, report,
                    on_conflict=_preserve_rewrite_fields, write_stage=False,
                )
            except Exception as exc:  # noqa: BLE001
                log.error("Checkpoint write failed: %s", exc)
                errors.record(exc)

    try:
        _flush(
            sheets, pending, report,
            on_conflict=_preserve_rewrite_fields, write_stage=False,
        )
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


@dataclass
class GenerateReport:
    """What one `--dry-run` generation pass did.

    Deliberately separate from BuildReport: that one counts Lovable projects,
    and reusing it would print "Projects created" for files on disk.
    """

    approved: int = 0
    already_on_disk: int = 0
    generated: int = 0
    repaired: int = 0
    rejected: int = 0
    errors: int = 0
    model_calls: int = 0
    paths: list[str] = field(default_factory=list)

    def summary_lines(self) -> list[str]:
        out = [
            f"Rows marked Approved      {self.approved}",
            f"  already generated       {self.already_on_disk}  (skipped, free)",
            f"Pages written             {self.generated}",
            f"  needed one repair       {self.repaired}",
            f"Pages refused             {self.rejected}  (nothing written)",
            f"Errors logged             {self.errors}",
            "",
            f"Gemini calls              {self.model_calls}",
        ]
        if self.paths:
            out += ["", "Open these in your browser:"]
            out += [f"  {path}" for path in self.paths]
        return out


def generate_sites(
    config: Config,
    sheets: SheetsClient,
    writer: BriefWriter,
    generator,
    *,
    limit: int | None = None,
) -> GenerateReport:
    """Generate demo pages to disk. Writes nothing to the Sheet, publishes nothing.

    This is the step that exists so a page can be looked at before anyone
    outside sees it. Nothing here touches Google or Firebase, so a bad batch
    costs Gemini calls and nothing else.

    Two rules carry the safety:

    A page is written only after it passes `check_site`. That is what lets the
    next run trust a file on disk and skip it for free -- if half-valid pages
    could land there, the cache would be a liability rather than a saving.

    A page that still fails after one repair is logged and skipped. It is not
    written, so it cannot be published later by a deploy that walks the
    directory.
    """
    from pathlib import Path

    from ..images import (
        ImageBudget,
        generate_for_site,
        load_existing,
        manifest_lines,
    )
    from ..providers.sitegen import SiteGenError
    from ..sitecheck import check_site, repair_instruction
    from ..slug import site_slug

    report = GenerateReport()
    errors = ErrorLog("site-generate")

    leads = sheets.read_rows(schema.LEADS)
    research = sheets.read_rows(schema.RESEARCH)
    briefs = sheets.read_rows(schema.SITE_BRIEFS)

    # Resolved from the working directory. Dashboard.bat and run.bat both
    # `cd /d "%~dp0"` before running anything, so on Windows this is the repo
    # root by construction.
    root = Path(config.build.output_dir)

    queue: list[tuple[str, dict[str, str]]] = []
    for lead_id, (_row, brief_row) in briefs.items():
        approval = (brief_row.get("Approval Status") or "").strip().casefold()
        if approval != "approved":
            continue
        report.approved += 1
        # A lead disqualified after its brief was approved must not get a
        # public page. build_approved has never checked this; here it would
        # mean publishing a site for someone who asked not to be contacted.
        lead_entry = leads.get(lead_id)
        if lead_entry is not None:
            _lrow, lead = lead_entry
            if (lead.get("Stage") or "").strip() == Stage.DISQUALIFIED.value:
                continue
        if (brief_row.get("Lovable Project ID") or "").strip():
            continue
        if len(queue) < min(limit or config.batch_size, config.batch_size):
            queue.append((lead_id, brief_row))

    if not queue:
        log.info("Nothing approved and unbuilt to generate.")
        return report

    log.info("Generating %d page(s) into %s/", len(queue), root)
    # Shared across the whole run: one quota refusal is enough to know.
    image_budget = ImageBudget(limit=config.build.max_images_per_run)

    for index, (lead_id, brief_row) in enumerate(queue, start=1):
        name = brief_row.get("Business Name", "") or lead_id
        lead_entry = leads.get(lead_id)
        lead = lead_entry[1] if lead_entry else {}
        research_entry = research.get(lead_id)
        research_row = research_entry[1] if research_entry else {}

        with errors.guard(lead_id=lead_id, business_name=name):
            slug = site_slug(name, lead_id)
            path = root / slug / "index.html"

            if path.exists():
                report.already_on_disk += 1
                report.paths.append(str(path.resolve()))
                log.info("[%d/%d] %s -- already on disk", index, len(queue), name)
                continue

            log.info("[%d/%d] %s", index, len(queue), name)

            social_profiles = (
                research_row.get("Social Profiles", "").strip()
                or (
                    social.profiles_text(lead.get("Website URL", ""))
                    if config.research.social_lookup
                    else ""
                )
            )
            spec = writer.write_site_spec(
                _spec_prompt(lead, research_row, config, social_profiles)
            )
            # Images first: the page has to be told which files exist before
            # it can reference them, and a page written against images that
            # were never generated would ship broken <img> tags.
            # Anything a previous attempt already paid for.
            images = load_existing(
                path.parent,
                spec,
                business_name=name,
                category=lead.get("Category", ""),
                count=config.build.images_per_page,
            )
            if images:
                log.info("  %d image(s) already on disk -- not re-bought.", len(images))
            images = images or generate_for_site(
                generator,
                spec,
                business_name=name,
                category=lead.get("Category", ""),
                count=config.build.images_per_page,
                budget=image_budget,
            )
            if images:
                # Written now, not after the page validates. Images are paid
                # for the moment they are generated, and the page call after
                # this one can fail on a rate limit -- which would throw away
                # something already bought. On disk they are also picked up
                # free by the next run.
                path.parent.mkdir(parents=True, exist_ok=True)
                for image in images:
                    # Empty data means it came off disk already -- see
                    # images.load_existing. Nothing to rewrite.
                    if image.data:
                        (path.parent / image.filename).write_bytes(image.data)
                log.info("  %d image(s) generated and saved.", len(images))

            prompt = render_lovable_prompt(
                business_name=name,
                category=lead.get("Category", ""),
                location=config.campaign.location,
                spec=spec,
                phone=lead.get("Phone", ""),
                address=lead.get("Address", ""),
                social_handle=research_row.get("Social Handle", ""),
                social_profiles=social_profiles,
                maps_url=lead.get("Maps URL", ""),
                target="html",
                images=manifest_lines(images),
            )

            html = generator.generate(prompt)
            problems = check_site(
                html,
                phone=lead.get("Phone", ""),
                headline=spec.hero_headline,
                business_name=name,
                min_bytes=config.build.min_bytes,
                max_bytes=config.build.max_bytes,
            )

            if problems:
                log.info(
                    "  %d problem(s): %s -- asking for one repair",
                    len(problems),
                    ", ".join(p.code for p in problems),
                )
                html = generator.generate(
                    prompt + "\n\n" + repair_instruction(problems)
                )
                problems = check_site(
                    html,
                    phone=lead.get("Phone", ""),
                    headline=spec.hero_headline,
                    business_name=name,
                    min_bytes=config.build.min_bytes,
                    max_bytes=config.build.max_bytes,
                )
                if not problems:
                    report.repaired += 1

            if problems:
                report.rejected += 1
                raise SiteGenError(
                    f"page still invalid after one repair: "
                    f"{', '.join(p.code for p in problems)}. Nothing was written."
                )

            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(html, encoding="utf-8")
            report.generated += 1
            report.paths.append(str(path.resolve()))

    report.model_calls = getattr(generator, "call_count", 0)

    if len(errors):
        try:
            sheets.append(schema.ERRORS, errors.rows())
        except Exception as exc:  # noqa: BLE001
            log.error("Could not write Errors tab: %s", exc)
    report.errors = len(errors)

    return report


def regenerate_one(
    config: Config,
    sheets: SheetsClient,
    writer: BriefWriter,
    lead_id: str,
    *,
    notes: str = "",
) -> str:
    """Rewrite one brief and return the new prompt. One model call.

    Exists for the moment you read a brief and it is wrong -- flat, or aimed
    at the wrong customer, or missing the thing that actually sells this
    business. Re-rolling it costs one call and about ten seconds, which is
    cheaper than building a demo from a brief you did not believe.

    `notes` are yours, passed to the model alongside the research and not
    stored anywhere. Approval Status is never touched: rewriting is not
    deciding, and a rewrite that silently un-approved a row would lose work.
    """
    leads = sheets.read_rows(schema.LEADS)
    research = sheets.read_rows(schema.RESEARCH)

    lead_entry = leads.get(lead_id)
    if lead_entry is None:
        raise ValueError(f"No lead with ID {lead_id!r} on the Leads tab.")
    _lrow, lead = lead_entry

    research_entry = research.get(lead_id)
    if research_entry is None:
        raise ValueError(
            f"{lead.get('Business Name', lead_id)} has no Research row -- "
            "run Research for it before rewriting its brief."
        )
    _rrow, research_row = research_entry

    name = lead.get("Business Name", "") or lead_id
    social_profiles = (
        research_row.get("Social Profiles", "").strip()
        or (
            social.profiles_text(lead.get("Website URL", ""))
            if config.research.social_lookup
            else ""
        )
    )

    spec_prompt = _spec_prompt(lead, research_row, config, social_profiles)
    if notes:
        spec_prompt += (
            "\n\nWHAT THE FOUNDER WANTS CHANGED\n"
            f"{notes}\n"
            "This is a direct instruction from the person who will show this "
            "site to the owner. Follow it, and keep everything else grounded "
            "in the evidence above."
        )

    spec = writer.write_site_spec(spec_prompt)
    prompt = render_lovable_prompt(
        business_name=name,
        category=lead.get("Category", ""),
        location=config.campaign.location,
        spec=spec,
        phone=lead.get("Phone", ""),
        address=lead.get("Address", ""),
        social_handle=research_row.get("Social Handle", ""),
        social_profiles=social_profiles,
        maps_url=lead.get("Maps URL", ""),
        target="html" if config.build.target == "firebase" else "lovable",
    )

    # Only the prompt cell. Approval Status, Created At, Preview URL and the
    # built marker all belong to other decisions and are left alone.
    sheets.update_column(schema.SITE_BRIEFS, "Lovable Prompt", {lead_id: prompt})
    return prompt


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
    lead: dict[str, str],
    research_row: dict[str, str],
    config: Config,
    social_profiles: str,
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
        social_profiles=social_profiles,
    )
