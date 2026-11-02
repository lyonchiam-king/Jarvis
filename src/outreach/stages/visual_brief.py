"""The infographic track -- turn each research row into an image prompt.

Run: ``outreach run visual-brief``

The deliverable is a prompt. Nothing here generates an image: the graphic is
made by hand in the image tool from this prompt, and pasted back into the
dashboard, which is where ``Image File`` and ``Text Confirmed`` get written.
That is not a limitation worked around -- it is the shape of the tool Lyon
uses, and it means this stage spends at most one small model call per lead.

Additive by construction. It shares research with the website track, writes
to its own tab, and touches the ``Stage`` column not at all: a lead's progress
down the visual track is recorded by its Visual Briefs row existing, exactly
as the WhatsApp verdict is recorded in a column rather than a stage. Adding a
rank to the Stage enum for this would have put two stages at the same position
in a sequence that is compared with ``>=``, and the first casualty would have
been the site-brief stage silently failing to record itself.

What a card may say is decided in ``gaps.py`` from the research row, never by
a model. How it looks is decided in ``visual.py`` from the campaign. The one
model call re-words the chosen cards in this business's own vocabulary, and
falls back to the library wording whenever it cannot -- so a graphic is never
blocked on it and never invented by it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .. import gaps as gaps_lib
from .. import schema, visual
from ..config import Config
from ..errors import ErrorLog
from ..models import utc_now
from ..providers.llm import (
    BriefWriter,
    build_gap_wording_prompt,
    render_infographic_prompt,
)
from ..selection import ORDERS, select_pending
from ..sheets import SheetsClient
from ..stage import Stage
from .reachability import (
    has_email,
    is_contactable,
    is_waiting_on_a_check,
    stored_verdict,
)

log = logging.getLogger("outreach")


# Which group a lead belongs to, by the channel a message would actually use.
#
# This is the split Lyon asked to work from -- "which one have WhatsApp and
# which one doesn't" -- because it is the one that decides how a lead gets
# contacted, and therefore which batch is worth spending an evening in the
# image tool on. The names are the user's words, not the column's.
GROUPS: tuple[str, ...] = ("whatsapp", "email", "unchecked", "none")

GROUP_LABELS: dict[str, str] = {
    "whatsapp": "On WhatsApp",
    "email": "Email only",
    "unchecked": "Not checked yet",
    "none": "No way to contact them",
}


def group_of(lead: dict[str, str], research_row: dict[str, str] | None) -> str:
    """Which group this lead is in, named by the channel that would be used.

    WhatsApp wins where the check said yes, even when there is also an email:
    that is the channel the message goes out on, so grouping it as "email
    only" would put it in a batch Lyon picked for a different reason.

    A lead nobody has checked and that has no email is **unchecked**, not
    unreachable -- missing evidence is not evidence of absence, and the whole
    point of the reachability check is that it answers later.
    """
    if stored_verdict(lead, research_row) == "yes":
        return "whatsapp"
    if has_email(research_row or {}):
        return "email"
    if stored_verdict(lead, research_row) == "no":
        return "none"
    return "unchecked"


@dataclass
class VisualBriefReport:
    candidates: int = 0
    already_done: int = 0
    not_ready: int = 0
    unreachable: int = 0
    waiting_on_check: int = 0
    selected: int = 0
    # Dropped by a group or an explicit pick rather than by the contact gate.
    # Reported separately because "skipped" and "not what you asked for" are
    # different facts, and a run that says neither looks under-delivered.
    filtered_out: int = 0
    prompts_written: int = 0
    # Graphics that will carry the business's own name, and ones that will
    # not. Reported separately because the split is the honesty of the whole
    # track: a generic graphic means the research could not evidence enough
    # to name them, and seeing that number climb is the signal that research
    # is thin rather than that the track is working.
    specific: int = 0
    generic: int = 0
    reworded: int = 0
    wording_failures: int = 0
    wording_reason: str = ""
    errors: int = 0
    llm_calls: int = 0
    checkpoints: int = 0

    def summary_lines(self) -> list[str]:
        lines = [
            f"Leads on the Sheet        {self.candidates}",
            f"  already have a prompt   {self.already_done}  (skipped)",
            f"  not yet researched      {self.not_ready}  (skipped)",
            f"  no way to contact them  {self.unreachable}  (skipped)",
        ]
        if self.waiting_on_check:
            lines.append(
                f"  waiting on a WhatsApp check {self.waiting_on_check}  (held)"
            )
        if self.filtered_out:
            lines.append(
                f"  outside what you picked {self.filtered_out}  (skipped)"
            )
        lines += [
            f"  selected this run       {self.selected}",
            "",
            f"Image prompts written     {self.prompts_written}",
            f"  named for the business  {self.specific}",
            f"  generic (not enough evidence) {self.generic}",
        ]
        if self.reworded:
            lines.append(f"  re-worded for the trade {self.reworded}")
        if self.wording_failures:
            lines.append(
                f"  library wording kept    {self.wording_failures}  "
                f"({self.wording_reason})"
            )
        lines += [
            f"Errors logged             {self.errors}",
            "",
            f"Model calls               {self.llm_calls}",
            f"Sheet checkpoints         {self.checkpoints}",
        ]
        return lines


@dataclass
class _Pending:
    rows: list[dict[str, object]] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.rows)

    def clear(self) -> None:
        self.rows.clear()


def _flush(
    sheets: SheetsClient,
    pending: _Pending,
    report: VisualBriefReport,
    *,
    on_conflict=None,
) -> None:
    if not pending.rows:
        return
    sheets.upsert(schema.VISUAL_BRIEFS, pending.rows, on_conflict=on_conflict)
    report.checkpoints += 1
    log.info("Checkpoint: saved %d image prompt(s).", len(pending.rows))
    pending.clear()


def preserve_image_fields(
    existing_row: dict[str, str], incoming: dict[str, object]
) -> dict[str, object]:
    """``upsert``'s on_conflict when a prompt is rewritten for a lead.

    The image, where it came from, and the confirmation that its text renders
    correctly are the dashboard's to write, and they cost real work -- a
    generation in the image tool and a person reading the words on it. A plain
    upsert would blank all three (they are automated columns) and the graphic
    would silently detach from the lead it was made for.

    ``Created At`` is carried over for the same reason it is in the site
    brief rewrite: it marks when the row first appeared, and there is no
    "rewritten at" column to record the difference.
    """
    return {
        **incoming,
        "Image File": existing_row.get("Image File", ""),
        "Image Source": existing_row.get("Image Source", ""),
        "Text Confirmed": existing_row.get("Text Confirmed", ""),
        "Created At": existing_row.get("Created At", ""),
    }


def last_pairs(
    leads: dict[str, tuple[int, dict[str, str]]],
    existing: dict[str, tuple[int, dict[str, str]]],
    fallback_category: str = "",
) -> dict[str, tuple[str, str]]:
    """The (style, layout) each industry used most recently.

    This is what makes "never the same pair twice in a row" survive across
    runs rather than only within one: without it, every run would restart at
    the top of each industry's ring and ten salons briefed over ten days
    would all get the same graphic.

    Sheet order is insertion order for an append-only tab, so the last row
    seen for an industry is the most recent one written. A row whose style or
    layout cell is blank -- hand-edited, or written before this existed -- is
    ignored rather than treated as a pair, because an empty pair would match
    nothing and silently disable the rotation.
    """
    out: dict[str, tuple[str, str]] = {}
    for lead_id, (_n, row) in existing.items():
        style = (row.get("Style") or "").strip()
        layout = (row.get("Layout") or "").strip()
        if not style or not layout:
            continue
        lead = leads.get(lead_id)
        category = lead[1].get("Category", "") if lead else ""
        industry = visual.industry_for(category, fallback_category)
        out[industry.key] = (style, layout)
    return out


def _cards_for(
    config: Config,
    writer: BriefWriter,
    lead: dict[str, str],
    research_row: dict[str, str] | None,
    report: VisualBriefReport,
) -> tuple[tuple[gaps_lib.Gap, ...], bool]:
    """The cards for one lead, and whether they name the business.

    Returns the library's own wording when the re-wording call is switched
    off or fails. A failure here is never fatal: the fixed lines are always
    true, so the worst case is a slightly blander graphic rather than no
    graphic.
    """
    found = gaps_lib.gaps_for(
        lead,
        research_row,
        max_cards=config.visual.max_cards,
        use_review_pain=config.visual.use_review_pain,
    )
    specific = gaps_lib.is_specific_enough(found)
    if not specific:
        # The generic graphic. Its cards are not claims about this business,
        # so there is nothing to localise and nothing to spend a call on.
        return gaps_lib.GENERIC_GAPS[: config.visual.max_cards], False

    if not config.visual.localise_wording:
        return found, True

    try:
        wording = writer.write_gap_wording(
            build_gap_wording_prompt(
                name=lead.get("Business Name", ""),
                category=lead.get("Category", ""),
                location=config.campaign.location,
                tone=(research_row or {}).get("Tone", ""),
                services=(research_row or {}).get("Services", ""),
                cards=[(g.id, g.problem, g.fix) for g in found],
            )
        )
    except Exception as exc:  # noqa: BLE001 -- deliberately everything
        # Same shape as capture_site_shot: an optional step catches every
        # exception, not only the one its own module raises, and reports the
        # reason in the summary so the sole trace is not a log nobody reads.
        log.warning(
            "Keeping the library wording for %s: %s: %s",
            lead.get("Business Name", "") or lead.get("Lead ID", ""),
            type(exc).__name__,
            exc,
        )
        report.wording_failures += 1
        report.wording_reason = f"{type(exc).__name__}: {exc}"
        return found, True

    # Matched back by id, in the order the evidence chose. A card the model
    # did not return keeps its library wording; one it invented an id for is
    # dropped. This is the step that enforces "never add a card" -- the system
    # prompt asks, and this makes asking unnecessary.
    by_id = {card.id: card for card in wording}
    out = []
    for gap in found:
        card = by_id.get(gap.id)
        if card is None:
            out.append(gap)
            continue
        out.append(gaps_lib.with_wording(gap, card.problem, card.fix))
    report.reworded += 1
    return tuple(out), True


def _row_for(
    config: Config,
    lead: dict[str, str],
    lead_id: str,
    name: str,
    cards: tuple[gaps_lib.Gap, ...],
    specific: bool,
    look: visual.Look,
) -> dict[str, object]:
    prompt = render_infographic_prompt(
        business_name=name,
        cards=[(g.problem, g.fix) for g in cards],
        signature=config.visual.signature,
        aspect=config.visual.aspect,
        specific=specific,
        **visual.describe(look),
    )
    return {
        "Lead ID": lead_id,
        "Business Name": name,
        "Image Prompt": prompt,
        "Gaps Used": gaps_lib.gaps_summary(cards),
        "Style": look.style,
        "Layout": look.layout,
        "Character": look.character,
        "Palette": look.palette,
        "Specific": "yes" if specific else "no",
        # Manual column, seeded once on insert and never rewritten, so a later
        # config change cannot retroactively approve a graphic you declined.
        #
        # Seeded Approved rather than Pending, unlike a site brief: nothing
        # downstream of this spends money on its own -- the image is generated
        # by hand -- and the gate that actually matters is Text Confirmed,
        # which no message is attached without. An extra click with no spend
        # behind it is friction, not a safeguard. Setting Skip here is how you
        # retire a prompt you do not want.
        "Approval Status": "Approved",
        "Image File": "",
        "Image Source": "",
        "Text Confirmed": "",
        "Created At": utc_now(),
    }


def run(
    config: Config,
    sheets: SheetsClient,
    writer: BriefWriter,
    *,
    limit: int | None = None,
    social_first: bool | None = None,
    groups: tuple[str, ...] = (),
    lead_ids: set[str] | None = None,
) -> VisualBriefReport:
    """Write an image prompt per lead, optionally for one group or one pick.

    ``groups`` narrows the run to leads whose contact channel is one of
    ``GROUPS``. Empty means every group, which is what every earlier caller
    gets. ``lead_ids`` narrows it to an explicit set -- the two combine, so a
    pick inside a group is a pick inside a group.

    Both exist because sixty prompts is not a batch anybody works through:
    each one is a visit to the image tool and a picture to read. The point of
    the stage was always a bounded run, and ``--limit`` alone picks *whichever*
    sixty come first rather than the ones worth starting with.
    """
    report = VisualBriefReport()
    errors = ErrorLog("visual-brief")

    leads = sheets.read_rows(schema.LEADS)
    research = sheets.read_rows(schema.RESEARCH)
    existing = sheets.read_rows(schema.VISUAL_BRIEFS)
    report.candidates = len(leads)

    # The same contactability gate the site brief applies, for the same
    # reason: a graphic costs a model call and then Lyon's own time in the
    # image tool, and spending either on a business with no channel at all is
    # the waste the reachability check exists to prevent. An unchecked lead
    # still passes -- a lookup that has not run is not evidence of anything.
    contactable = {}
    waiting = 0
    for lead_id, entry in leads.items():
        row = research.get(lead_id)
        lead = entry[1]
        research_row = row[1] if row else None
        if row is None or is_contactable(research_row, lead):
            contactable[lead_id] = entry
        elif is_waiting_on_a_check(research_row, lead):
            waiting += 1
    report.waiting_on_check = waiting
    report.unreachable = len(leads) - len(contactable) - waiting
    leads_gated = contactable

    # Narrowed before selection, not after, so --limit counts what was asked
    # for. Filtering the selected batch instead would make "write 10 for the
    # WhatsApp group" produce however many of the first 10 leads happened to
    # be in it -- which looks exactly like the filter being ignored.
    if lead_ids is not None:
        leads_gated = {k: v for k, v in leads_gated.items() if k in lead_ids}
    if groups:
        wanted = set(groups)
        leads_gated = {
            lead_id: entry
            for lead_id, entry in leads_gated.items()
            if group_of(
                entry[1],
                (research.get(lead_id) or (0, None))[1],
            )
            in wanted
        }
    report.filtered_out = len(contactable) - len(leads_gated)

    batch_size = min(limit or config.batch_size, config.batch_size)
    prioritise = (
        config.research.social_first if social_first is None else social_first
    )
    selected, already, not_ready = select_pending(
        leads_gated,
        set(existing),
        requires=Stage.RESEARCHED,
        # The same stage it requires: this stage advances nothing, so there is
        # no target to name. See the module docstring.
        target=Stage.RESEARCHED,
        batch_size=batch_size,
        prefer=ORDERS[config.research.order] if prioritise else None,
    )
    report.already_done = already
    report.not_ready = not_ready
    report.selected = len(selected)

    if not selected:
        log.info(
            "Nothing to brief -- every researched lead already has an image "
            "prompt."
        )
        return report

    log.info("Writing %d image prompt(s).", len(selected))
    pending = _Pending()
    previous = last_pairs(leads, existing, config.campaign.category)

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

            cards, specific = _cards_for(
                config, writer, lead, research_row, report
            )
            industry = visual.industry_for(
                lead.get("Category", ""), config.campaign.category
            )
            look = visual.look_for(
                lead.get("Category", ""),
                fallback_category=config.campaign.category,
                previous=previous.get(industry.key),
                styles=config.visual.styles,
                layouts=config.visual.layouts,
            )
            # Recorded before the row is written, so the next lead in this
            # industry rotates off it even if this batch is never flushed.
            previous[industry.key] = look.pair

            pending.rows.append(
                _row_for(config, lead, lead_id, name, cards, specific, look)
            )
            report.prompts_written += 1
            if specific:
                report.specific += 1
            else:
                report.generic += 1

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


def regenerate_one(
    config: Config,
    sheets: SheetsClient,
    writer: BriefWriter,
    lead_id: str,
) -> dict[str, object]:
    """Rewrite one lead's image prompt, keeping its image and confirmation.

    The targeted answer to "the style list changed, so every prompt written
    before it is the old one". A whole-backlog rewrite is the site brief's
    ``--rewrite``; here the per-lead button is what Lyon actually reaches for,
    because the prompt is something he reads before using and re-rolling one
    he is looking at is the normal case.

    The next pair is chosen against whatever this lead already had, so
    pressing it twice moves through the ring rather than returning the same
    graphic.
    """
    leads = sheets.read_rows(schema.LEADS)
    entry = leads.get(lead_id)
    if entry is None:
        raise ValueError(f"No lead on the Sheet with ID {lead_id}")
    lead = entry[1]
    name = lead.get("Business Name", "") or lead_id

    research = sheets.read_rows(schema.RESEARCH)
    brief = research.get(lead_id)
    if brief is None:
        raise ValueError(
            f"{name} has no Research row -- run research for it first."
        )

    existing = sheets.read_rows(schema.VISUAL_BRIEFS).get(lead_id)
    was = existing[1] if existing else {}
    previous_pair = (
        ((was.get("Style") or "").strip(), (was.get("Layout") or "").strip())
        if (was.get("Style") or "").strip() and (was.get("Layout") or "").strip()
        else None
    )

    report = VisualBriefReport()
    cards, specific = _cards_for(config, writer, lead, brief[1], report)
    look = visual.look_for(
        lead.get("Category", ""),
        fallback_category=config.campaign.category,
        previous=previous_pair,
        styles=config.visual.styles,
        layouts=config.visual.layouts,
    )
    row = _row_for(config, lead, lead_id, name, cards, specific, look)
    # Approval Status is manual, so it is dropped rather than re-seeded: a
    # lead already marked Skip must not come back Approved because somebody
    # pressed Regenerate.
    row.pop("Approval Status", None)

    sheets.upsert(
        schema.VISUAL_BRIEFS, [row], on_conflict=preserve_image_fields
    )
    return {
        "lead_id": lead_id,
        "name": name,
        "prompt": row["Image Prompt"],
        "specific": specific,
        "style": look.style,
        "layout": look.layout,
        "cards": [{"problem": g.problem, "fix": g.fix} for g in cards],
        "llm_calls": writer.call_count,
    }
