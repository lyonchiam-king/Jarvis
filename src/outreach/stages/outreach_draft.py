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
from pathlib import Path
from dataclasses import dataclass, field

from .. import compliance, schema
from ..config import Config
from ..errors import ErrorLog
from ..models import utc_now
from ..providers import gmail as gmail_provider
from ..providers.llm import BriefWriter, build_outreach_prompt
from .reachability import can_whatsapp, is_waiting_on_a_check
from ..selection import ORDERS, select_pending
from ..sheets import SheetsClient
from .. import screenshot, visual as visual_lib
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
    held_for_check: int = 0
    screenshots: int = 0
    screenshot_failures: int = 0
    screenshot_reason: str = ""
    # Leads whose graphic has not come back from the image tool yet, or has
    # come back and not been checked. A hold, never a draft: writing one and
    # advancing the Stage would retire the lead for good, so when the image
    # did arrive nothing would ever message them -- the same bug the WhatsApp
    # check's hold exists to prevent.
    held_for_image: int = 0
    # Counted apart from `screenshots`, which says "with a site screenshot" --
    # true of a photographed page and a lie about a graphic. One number
    # covering both would make the summary describe work that did not happen.
    graphics_attached: int = 0
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
            f"Held for the WhatsApp check{self.held_for_check:>5}"
            if self.held_for_check
            else "",
            "",
            f"Messages written          {self.copy_written}",
            f"  Gmail drafts created    {self.drafts_created}",
            f"  WhatsApp only (no email){self.whatsapp_only:>4}",
            f"  with a site screenshot  {self.screenshots:>4}"
            if self.screenshots
            else "",
            f"  no screenshot           {self.screenshot_failures:>4}  "
            f"({self.screenshot_reason})"
            if self.screenshot_failures
            else "",
            f"  waiting on a graphic    {self.held_for_image:>4}  (held)"
            if self.held_for_image
            else "",
            f"  with an infographic     {self.graphics_attached:>4}"
            if self.graphics_attached
            else "",
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



def site_page_for(config, lead: dict[str, str], lead_id: str) -> Path | None:
    """The built page on disk for this lead, if there is one."""
    from ..slug import site_slug

    name = lead.get("Business Name", "") or lead_id
    page = Path(config.build.output_dir) / site_slug(name, lead_id) / "index.html"
    return page if page.exists() else None


def shot_dir_for(config, lead: dict[str, str], lead_id: str) -> Path:
    """Where this lead's picture belongs, whether or not a page is on disk."""
    from ..slug import site_slug

    name = lead.get("Business Name", "") or lead_id
    return Path(config.build.output_dir) / site_slug(name, lead_id)


def capture_site_shot(
    config,
    lead: dict[str, str],
    lead_id: str,
    preview_url: str = "",
) -> Path | None:
    """Photograph the built site so the message can carry a picture of it.

    The page comes from disk when this pipeline generated it, and from the
    Preview URL when it did not -- a site built in Google AI Studio or
    Lovable exists only at a URL, and looking solely on disk meant every
    externally built site quietly got no screenshot at all.

    Never fatal. A screenshot that cannot be taken -- no browser, no page, a
    browser that hung -- must not stop a draft: the message is the
    deliverable and the picture only makes it land better.
    """
    if not getattr(config.build, "screenshot", True):
        return None

    page = site_page_for(config, lead, lead_id)
    source = page if page is not None else (preview_url or "").strip()
    if not source:
        return None

    # Beside the page when there is one, so the picture travels with the
    # site; in the lead's slug directory when the site is only a URL.
    out = (
        screenshot.shot_path_for(page)
        if page is not None
        else shot_dir_for(config, lead, lead_id) / "screenshot.png"
    )
    if out.exists() and out.stat().st_size:
        return out  # already taken; photographing it again buys nothing

    try:
        return screenshot.capture(
            source,
            out,
            browser=getattr(config.build, "screenshot_browser", ""),
            timeout=float(getattr(config.build, "screenshot_timeout", 20.0)),
        ).path
    except Exception as exc:  # noqa: BLE001 -- deliberately everything
        # Every exception, not just ScreenshotError. Catching only the one
        # this module raises left every other failure -- a permissions error
        # writing the file, an OSError from the browser process, anything
        # Windows-specific -- to escape into the per-lead error guard and
        # kill that lead's draft. When it happened for every lead the whole
        # run produced nothing, message included, which is the exact
        # opposite of "the picture is never worth the message".
        log.warning(
            "No screenshot for %s: %s: %s",
            lead.get("Business Name", lead_id),
            type(exc).__name__,
            exc,
        )
        # Handed back so the run summary can say why, once, rather than
        # leaving the only trace in a log Lyon does not read.
        capture_site_shot.last_error = f"{type(exc).__name__}: {exc}"
        return None


def needs_an_image(config) -> bool:
    """Whether this campaign's message is supposed to carry a graphic."""
    return getattr(config.outreach, "track", "website") in ("visual", "both")


def attachment_for(config, lead, lead_id, preview_url="", visual_row=None):
    """The one picture this lead's message carries, and what it is.

    Returns ``(path, kind)`` with kind "visual", "site" or "" -- one image per
    message, because the relay sends one, and because "both tracks" is one
    message by design: the same business messaged twice in a day is the thing
    this pipeline works hardest to avoid.

    The graphic wins over the screenshot whenever there is one. It is the hook
    -- it names the gaps and it is what the message is written about -- and the
    page it would displace is still in the message as a link. A screenshot of
    a site beside a message arguing about three gaps is the pitch
    contradicting itself.

    Never raises. Both branches are optional decoration over a message that
    sends regardless.
    """
    if visual_row is not None:
        try:
            found = visual_lib.confirmed_image(visual_row)
        except Exception as exc:  # noqa: BLE001 -- deliberately everything
            log.warning(
                "Could not resolve the graphic for %s: %s: %s",
                lead.get("Business Name", lead_id), type(exc).__name__, exc,
            )
            found = None
        if found is not None:
            return found, "visual"

    shot = capture_site_shot(config, lead, lead_id, preview_url)
    return (shot, "site") if shot is not None else (None, "")


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
    # Only read when the campaign actually pitches a graphic. A campaign on
    # the original track must not pay a Sheet round trip for a tab it has
    # nothing in.
    visual_rows = (
        sheets.read_rows(schema.VISUAL_BRIEFS) if needs_an_image(config) else {}
    )
    report.candidates = len(leads)

    batch_size = min(limit or config.batch_size, config.batch_size)
    # With require_site on, the demo is the pitch: a lead is not ready to be
    # written to until there is a site for them to look at.
    #
    # On the visual-only track there is no site to wait for -- the artefact is
    # the graphic, which lives on its own tab and not in the Stage column. So
    # the Stage gate drops back to Researched and the wait happens per lead
    # below, as a hold. "both" keeps the site gate, because it does promise a
    # page as well.
    if needs_an_image(config) and config.outreach.track == "visual":
        requires = Stage.RESEARCHED
    else:
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

    # Identical for every lead in the run, so built once out here rather than
    # per business inside the loop.
    services_block = gmail_provider.render_services_block(
        config.outreach.services_intro, config.outreach.services
    )

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

            # A lead with no email and no answer from the reachability check
            # is held, not drafted. The old code let it fall into the
            # Unreachable branch below, which retired it for good: a Draft
            # row was written and the Stage advanced, so when the check
            # later came back yes nothing would ever regenerate the message.
            # Holding costs nothing -- the run just leaves it for the next
            # one, after the check has run.
            if config.outreach.whatsapp_enabled and is_waiting_on_a_check(
                research_row, lead
            ):
                report.held_for_check += 1
                log.info(
                    "  held: the WhatsApp check has not answered yet -- "
                    "run the check, then draft again."
                )
                continue

            preview_url = ""
            brief = site_briefs.get(lead_id)
            if brief is not None:
                preview_url = brief[1].get("Preview URL", "")

            visual_entry = visual_rows.get(lead_id)
            visual_row = visual_entry[1] if visual_entry else None
            graphic = (
                visual_lib.confirmed_image(visual_row)
                if needs_an_image(config)
                else None
            )

            # The graphic is the pitch on this track, so there is nothing to
            # say until it exists. Held rather than drafted: a message
            # written now would describe a picture that is not attached, and
            # writing the row would advance the Stage and retire the lead --
            # so when the image finally arrived, nothing would ever message
            # them. A graphic marked Skip is not a wait either; it is a
            # decision, so the lead falls through to the website pitch.
            if (
                needs_an_image(config)
                and graphic is None
                and not visual_lib.is_skipped(visual_row)
            ):
                report.held_for_image += 1
                log.info(
                    "  held: no confirmed graphic yet -- paste the image into "
                    "the dashboard and tick that the text is right."
                )
                continue

            pitch = "visual" if graphic is not None else "site"
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
            ,
                angle=config.outreach.angle,
                offers_follow_up=config.outreach.offers_follow_up,
                pitch=pitch,
                has_page=bool(preview_url.strip()),
                # A generic graphic may never be described as made for them.
                # The owner can see from the picture that it was not, so the
                # claim fails exactly where it is being read.
                #
                # With no graphic attached there is nothing for this to
                # describe, so it stays at the default every existing caller
                # already passed -- adding a track must not change the prompt
                # the other tracks produce, and that includes by one keyword.
                specific=(
                    visual_lib.is_specific(visual_row)
                    if graphic is not None
                    else True
                ),
            )
            report.copy_written += 1

            # Computed once and used everywhere below. Appending at each use
            # site instead is how one of them gets missed -- the message would
            # then read one way in the log and another on the Sheet.
            whatsapp_text = gmail_provider.with_services(
                copy.whatsapp_message, services_block
            )

            recipient = research_row.get("Email Found", "").strip()
            email_source = research_row.get("Email Source", "not found")

            body = gmail_provider.compose_body(
                body=copy.email_body,
                sender_name=config.outreach.sender_name,
                sender_title=config.outreach.sender_title,
                company=config.outreach.company,
                postal_address=config.outreach.postal_address,
                opt_out_note=config.outreach.opt_out_note,
                services_block=services_block,
            )

            draft_id = ""
            draft_link = ""
            channel = "WhatsApp"
            notes = ""

            # Taken for anyone reachable on WhatsApp, whatever channel the
            # draft is filed under. A lead with an email is filed as Email
            # and still appears in the WhatsApp queue, so gating the picture
            # on "no email" meant most leads never got one.
            if not dry_run and can_whatsapp(
                lead, research_row, config.compliance.country_code
            ):
                capture_site_shot.last_error = ""
                # One picture per message. A confirmed graphic is already on
                # disk, so this does not even start a browser for it -- the
                # old code screenshotted unconditionally, which on the visual
                # track would have spent a browser launch per lead to produce
                # an image nothing then attached.
                attached, kind = attachment_for(
                    config, lead, lead_id, preview_url, visual_row
                )
                if kind == "visual":
                    report.graphics_attached += 1
                elif attached is not None:
                    report.screenshots += 1
                elif capture_site_shot.last_error:
                    report.screenshot_failures += 1
                    report.screenshot_reason = capture_site_shot.last_error

            if recipient and gmail is not None and not dry_run:
                draft = gmail.create_draft(
                    to=recipient, subject=copy.subject, body=body
                )
                draft_id, draft_link = draft.draft_id, draft.link
                channel = "Email"
                report.drafts_created += 1
            elif recipient:
                channel = "Email"
            elif can_whatsapp(lead, research_row, config.compliance.country_code):
                report.whatsapp_only += 1
            else:
                # No email, and the check has answered no -- or WhatsApp is
                # switched off and there was never a check to wait for. That
                # is a dead end, not a wait: mark it done so it is not
                # re-selected, and say why in Notes.
                channel = "Unreachable"
                notes = "Unreachable: no email and not reachable on WhatsApp"

            if dry_run:
                log.info("  subject: %s", copy.subject)
                log.info("  to:      %s", recipient or "(no email -- WhatsApp only)")
                log.info("  whatsapp: %s", whatsapp_text)
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
                    "WhatsApp Message": whatsapp_text,
                    "Drafted At": utc_now(),
                    # Manual columns, seeded once on insert. "Drafted" is the
                    # truth right now; you change it to Sent after you send.
                    "Status": "Drafted",
                    "Sent At": "",
                    "Notes": notes,
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


def regenerate_one(
    config: Config,
    sheets: SheetsClient,
    writer: BriefWriter,
    lead_id: str,
) -> dict[str, str]:
    """Rewrite one lead's outreach message. One model call.

    Exists for the moment you read a draft and it is wrong -- generic, or
    missing the thing that actually sells this business. Re-rolling it costs
    one call and about ten seconds, cheaper than sending a message you did
    not believe.

    Returns the new copy so the dashboard can show it immediately. Only the
    copy columns are rewritten: ``Status``, ``Sent At`` and ``Notes`` are
    manual and left alone, and a WhatsApp-only lead has no Gmail draft to
    refresh (the dashboard's Rewrite button only appears on the WhatsApp
    panel, which lists exactly those leads).
    """
    leads = sheets.read_rows(schema.LEADS)
    research = sheets.read_rows(schema.RESEARCH)
    site_briefs = sheets.read_rows(schema.SITE_BRIEFS)

    services_block = gmail_provider.render_services_block(
        config.outreach.services_intro, config.outreach.services
    )

    lead_entry = leads.get(lead_id)
    if lead_entry is None:
        raise ValueError(f"No lead with ID {lead_id!r} on the Leads tab.")
    _lrow, lead = lead_entry

    research_entry = research.get(lead_id)
    if research_entry is None:
        raise ValueError(
            f"{lead.get('Business Name', lead_id)} has no Research row -- "
            "run Research for it before rewriting its message."
        )
    _rrow, research_row = research_entry

    name = lead.get("Business Name", "") or lead_id

    preview_url = ""
    brief = site_briefs.get(lead_id)
    if brief is not None:
        preview_url = brief[1].get("Preview URL", "")

    # The same four arguments the run passes. Its own call site, with its own
    # test: three render_lovable_prompt call sites once went unwired while
    # every test stayed green, because each one looked like somebody else's
    # job.
    visual_row = None
    if needs_an_image(config):
        entry = sheets.read_rows(schema.VISUAL_BRIEFS).get(lead_id)
        visual_row = entry[1] if entry else None
    graphic = visual_lib.confirmed_image(visual_row) if visual_row else None

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
    ,
        angle=config.outreach.angle,
        offers_follow_up=config.outreach.offers_follow_up,
        pitch="visual" if graphic is not None else "site",
        has_page=bool(preview_url.strip()),
        specific=visual_lib.is_specific(visual_row) if graphic is not None else True,
    )

    body = gmail_provider.compose_body(
        body=copy.email_body,
        sender_name=config.outreach.sender_name,
        sender_title=config.outreach.sender_title,
        company=config.outreach.company,
        postal_address=config.outreach.postal_address,
        opt_out_note=config.outreach.opt_out_note,
        services_block=services_block,
    )

    whatsapp_text = gmail_provider.with_services(
        copy.whatsapp_message, services_block
    )
    sheets.update_column(
        schema.OUTREACH_LOG, "WhatsApp Message", {lead_id: whatsapp_text}
    )
    sheets.update_column(schema.OUTREACH_LOG, "Subject", {lead_id: copy.subject})
    sheets.update_column(
        schema.OUTREACH_LOG, "Body Preview", {lead_id: body[:BODY_PREVIEW_CHARS]}
    )

    return {
        "subject": copy.subject,
        "email_body": copy.email_body,
        "whatsapp_message": whatsapp_text,
    }


# Set on every failure so the run summary can report one reason.
capture_site_shot.last_error = ""
