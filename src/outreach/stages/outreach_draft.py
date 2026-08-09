"""Stage 5 -- write the outreach and put it in your Gmail drafts.

Run: ``outreach run outreach-draft``

**Nothing is sent.** Emails land in your Gmail drafts folder and you press send.
That is enforced by there being no send call in this codebase -- see
``providers/gmail.py`` and the source-level test that checks it.

WhatsApp text is generated for every lead regardless of whether an email
address was found, because for businesses with no website the phone number is
usually the only reliable contact channel.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .. import compliance, schema
from ..config import Config
from ..errors import ErrorLog
from ..models import utc_now
from ..providers import gmail as gmail_provider
from ..providers.llm import BriefWriter, build_outreach_prompt
from ..selection import ORDERS, select_pending
from ..sheets import SheetsClient
from ..stage import Stage, advance

log = logging.getLogger("outreach")

BODY_PREVIEW_CHARS = 180


class MissingPostalAddress(Exception):
    """A commercial email needs a postal address in most jurisdictions."""


@dataclass
class OutreachReport:
    candidates: int = 0
    already_done: int = 0
    not_ready: int = 0
    selected: int = 0
    copy_written: int = 0
    drafts_created: int = 0
    whatsapp_only: int = 0
    errors: int = 0
    llm_calls: int = 0
    checkpoints: int = 0

    def summary_lines(self) -> list[str]:
        return [
            f"Leads on the Sheet        {self.candidates}",
            f"  already drafted         {self.already_done}  (skipped)",
            f"  not ready yet           {self.not_ready}  (skipped)",
            f"  selected this run       {self.selected}",
            "",
            f"Messages written          {self.copy_written}",
            f"  Gmail drafts created    {self.drafts_created}",
            f"  WhatsApp only (no email){self.whatsapp_only:>4}",
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


def _flush(sheets: SheetsClient, pending: _Pending, report: OutreachReport) -> None:
    if not pending.rows:
        return
    sheets.upsert(schema.OUTREACH_LOG, pending.rows)
    sheets.update_column(schema.LEADS, "Stage", pending.stage_updates)
    report.checkpoints += 1
    log.info("Checkpoint: saved %d message(s).", len(pending.rows))
    pending.clear()


def preflight(config: Config) -> None:
    """Refuse to draft anything until the campaign is legally shaped.

    Raises ``ComplianceBlocked`` for a flagged jurisdiction that hasn't been
    acknowledged, and ``MissingPostalAddress`` if there is no address to put in
    the footer -- required by CAN-SPAM, GDPR/ePrivacy, and Australia's Spam
    Act, and cheap to satisfy.
    """
    compliance.check_outreach_allowed(
        config.compliance.country_code, config.compliance.acknowledged
    )
    if not config.outreach.postal_address.strip():
        raise MissingPostalAddress(
            "outreach.postal_address is empty.\n\n"
            "A commercial email must carry a real postal address in most of the "
            "jurisdictions this pipeline targets (CAN-SPAM, GDPR/ePrivacy, "
            "Australia's Spam Act). It goes in the footer of every draft.\n\n"
            "Add it to your campaign YAML:\n"
            "  outreach:\n"
            '    postal_address: "Your Company, 12 Some Road, 10450 Penang, Malaysia"'
        )


def run(
    config: Config,
    sheets: SheetsClient,
    writer: BriefWriter,
    gmail: gmail_provider.GmailClient | None,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    social_first: bool | None = None,
) -> OutreachReport:
    preflight(config)

    report = OutreachReport()
    errors = ErrorLog("outreach-draft")

    leads = sheets.read_rows(schema.LEADS)
    research = sheets.read_rows(schema.RESEARCH)
    site_briefs = sheets.read_rows(schema.SITE_BRIEFS)
    existing = sheets.read_rows(schema.OUTREACH_LOG)
    report.candidates = len(leads)

    batch_size = min(limit or config.batch_size, config.batch_size)
    # With require_site on, the demo is the pitch: a lead is not ready to be
    # written to until there is a site for them to look at.
    requires = Stage.SITE_BUILT if config.outreach.require_site else Stage.RESEARCHED
    prioritise = (
        config.research.social_first if social_first is None else social_first
    )
    selected, already, not_ready = select_pending(
        leads,
        set(existing),
        requires=requires,
        target=Stage.EMAIL_DRAFTED,
        batch_size=batch_size,
        prefer=ORDERS[config.research.order] if prioritise else None,
    )
    report.already_done = already
    report.not_ready = not_ready
    report.selected = len(selected)

    if not selected:
        if config.outreach.require_site and not_ready:
            log.info(
                "Nothing to draft. %d lead(s) are waiting on a demo site -- "
                "build those first (outreach.require_site is on).",
                not_ready,
            )
        else:
            log.info("Nothing to draft -- every eligible lead already has a message.")
        return report

    log.info("Writing outreach for %d lead(s).", len(selected))
    pending = _Pending()

    for index, lead in enumerate(selected, start=1):
        lead_id = lead.get("Lead ID", "")
        name = lead.get("Business Name", "") or lead_id

        with errors.guard(lead_id=lead_id, business_name=name):
            log.info("[%d/%d] %s", index, len(selected), name)

            found = research.get(lead_id)
            if found is None:
                raise ValueError(
                    "Lead is marked Researched but has no Research row -- "
                    "re-run `outreach run research` for it."
                )
            _row, research_row = found

            preview_url = ""
            brief = site_briefs.get(lead_id)
            if brief is not None:
                preview_url = brief[1].get("Preview URL", "")

            copy = writer.write_outreach_copy(
                build_outreach_prompt(
                    name=name,
                    category=lead.get("Category", ""),
                    location=config.campaign.location,
                    tone=research_row.get("Tone", ""),
                    services=research_row.get("Services", ""),
                    praise=research_row.get("Customer Praise", ""),
                    pain_points=research_row.get("Customer Pain Points", ""),
                    angle=research_row.get("Suggested Website Angle", ""),
                    sender_name=config.outreach.sender_name,
                    sender_title=config.outreach.sender_title,
                    company=config.outreach.company,
                    preview_url=preview_url,
                )
            )
            report.copy_written += 1

            recipient = research_row.get("Email Found", "").strip()
            email_source = research_row.get("Email Source", "not found")

            body = gmail_provider.compose_body(
                body=copy.email_body,
                sender_name=config.outreach.sender_name,
                sender_title=config.outreach.sender_title,
                company=config.outreach.company,
                postal_address=config.outreach.postal_address,
                opt_out_note=config.outreach.opt_out_note,
            )

            draft_id = ""
            draft_link = ""
            channel = "WhatsApp"

            if recipient and gmail is not None and not dry_run:
                draft = gmail.create_draft(
                    to=recipient, subject=copy.subject, body=body
                )
                draft_id, draft_link = draft.draft_id, draft.link
                channel = "Email"
                report.drafts_created += 1
            elif recipient:
                channel = "Email"
            else:
                report.whatsapp_only += 1

            if dry_run:
                log.info("  subject: %s", copy.subject)
                log.info("  to:      %s", recipient or "(no email -- WhatsApp only)")
                log.info("  whatsapp: %s", copy.whatsapp_message)
                continue

            pending.rows.append(
                {
                    "Lead ID": lead_id,
                    "Business Name": name,
                    "Channel": channel,
                    "Recipient": recipient,
                    "Email Source": email_source,
                    "Subject": copy.subject,
                    "Body Preview": body[:BODY_PREVIEW_CHARS],
                    "Gmail Draft ID": draft_id,
                    "Gmail Draft Link": draft_link,
                    "WhatsApp Message": copy.whatsapp_message,
                    "Drafted At": utc_now(),
                    # Manual columns, seeded once on insert. "Drafted" is the
                    # truth right now; you change it to Sent after you send.
                    "Status": "Drafted",
                    "Sent At": "",
                    "Notes": "",
                }
            )
            pending.stage_updates[lead_id] = advance(
                lead.get("Stage"), Stage.EMAIL_DRAFTED
            ).value

        if len(pending) >= config.research.checkpoint_every:
            try:
                _flush(sheets, pending, report)
            except Exception as exc:  # noqa: BLE001
                log.error("Checkpoint write failed: %s", exc)
                errors.record(exc)

    if not dry_run:
        try:
            _flush(sheets, pending, report)
        except Exception as exc:  # noqa: BLE001
            log.error("Final write failed: %s", exc)
            errors.record(exc)

    report.llm_calls = writer.call_count

    if len(errors) and not dry_run:
        try:
            sheets.append(schema.ERRORS, errors.rows())
        except Exception as exc:  # noqa: BLE001
            log.error("Could not write Errors tab: %s", exc)
    report.errors = len(errors)

    return report
