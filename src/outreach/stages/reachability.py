"""Find out who can actually be contacted -- before a site is built for them.

The pipeline used to discover reachability last: research, brief, generate a
site, draft the message, and only then notice the number was a landline with
no email behind it. Every one of those cost a site build and the tokens that
went with it, spent on a lead that was never contactable.

So the question moves forward. This stage runs between research and the site
brief and answers it once per lead, cheaply:

  * the numbering plan says mobile  -> reachable, no lookup needed
  * the numbering plan says landline -> not on WhatsApp, no lookup needed
  * the plan cannot say (US, Canada, and any country without a rule)
                                     -> ask WhatsApp whether it is registered

Only that third case costs a lookup, which is why a Malaysian campaign
carries on making almost none and a US one is answered properly instead of
being written off. The answer is cached in the Sheet and never asked twice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .. import phone as phone_lib, schema
from ..stage import Stage

log = logging.getLogger("outreach")

YES, NO, UNKNOWN = "yes", "no", "unknown"


@dataclass
class ReachabilityReport:
    checked: int = 0
    from_plan: int = 0
    from_lookup: int = 0
    reachable: int = 0
    unreachable: int = 0
    still_unknown: int = 0
    already_done: int = 0
    skipped_no_relay: int = 0
    errors: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [
            f"Checked {self.checked} lead(s): "
            f"{self.from_lookup} asked on WhatsApp, "
            f"{self.from_plan} had no usable number.",
            f"  reachable on WhatsApp: {self.reachable}",
            f"  not on WhatsApp:       {self.unreachable}",
        ]
        if self.still_unknown:
            out.append(
                f"  still unknown:         {self.still_unknown} "
                "(lookup failed -- they stay in the queue and are retried)"
            )
        if self.already_done:
            out.append(f"  already answered:      {self.already_done}")
        if self.skipped_no_relay:
            out.append(
                f"{self.skipped_no_relay} lead(s) need a WhatsApp lookup but the "
                "relay is not connected. Pair it in the dashboard and re-run -- "
                "nothing was written for them."
            )
        return out


def check_reachability(
    config,
    sheets,
    client=None,
    *,
    limit: int = 0,
    recheck: bool = False,
) -> ReachabilityReport:
    """Answer 'can we reach this lead on WhatsApp?' for researched leads.

    ``recheck`` ignores the cached verdict and asks again. Needed once,
    because early verdicts were decided by the numbering plan rather than by
    WhatsApp, and every "no" it produced for a landline is untrustworthy --
    UK businesses running WhatsApp Business on an 020 number were written
    off without ever being asked.
    """
    report = ReachabilityReport()

    home_country = getattr(getattr(config, "compliance", None), "country_code", "") or ""

    leads = sheets.read_rows(schema.LEADS)
    research = sheets.read_rows(schema.RESEARCH)

    pending: list[tuple[str, str, str]] = []  # lead_id, phone, country
    for lead_id, (_row, lead) in leads.items():
        if (lead.get("Stage") or "").strip() != Stage.RESEARCHED.value:
            continue
        row = research.get(lead_id)
        if row is None:
            continue
        # Cached: a lead answered once is never asked again. `unknown` is not
        # an answer -- it is what a failed lookup leaves behind, and it stays
        # in the queue on purpose.
        if not recheck and (
            row[1].get("WhatsApp Reachable") or ""
        ).strip().lower() in {YES, NO}:
            report.already_done += 1
            continue
        pending.append(
            (lead_id, lead.get("Phone", ""), lead.get("Country", "") or home_country)
        )

    if limit > 0:
        pending = pending[:limit]

    if not pending:
        log.info("Nothing to check -- every researched lead already has an answer.")
        return report

    verdicts: dict[str, str] = {}
    needs_lookup: dict[str, str] = {}  # lead_id -> E.164 digits

    for lead_id, raw_phone, country in pending:
        e164 = phone_lib.normalise(raw_phone, country)
        if not e164 or e164.startswith("0"):
            # No usable number at all -- nothing to dial and nothing to ask
            # about. The one verdict the numbering plan is still allowed to
            # reach on its own, because it is about the number existing, not
            # about who is on WhatsApp.
            verdicts[lead_id] = NO
            report.from_plan += 1
        else:
            # Everything else is asked, whatever the plan thinks. The plan is
            # wrong in both directions: WhatsApp Business verifies landlines
            # by voice call, so a fixed line is not a no -- which is what
            # wrote off every UK business with an 020 number without ever
            # asking -- and plenty of mobiles have no WhatsApp account, so a
            # mobile is not a yes either.
            needs_lookup[lead_id] = e164

    if needs_lookup:
        client = client or _default_client(config)
        if client is None or not client.is_connected():
            report.skipped_no_relay = len(needs_lookup)
            log.warning(
                "%d lead(s) need a WhatsApp lookup but the relay is not "
                "connected -- leaving them unanswered.",
                len(needs_lookup),
            )
        else:
            by_number = {number: lead_id for lead_id, number in needs_lookup.items()}
            try:
                results = client.check_numbers(list(by_number))
            except Exception as exc:  # noqa: BLE001 -- one bad batch is not a verdict
                log.warning("WhatsApp lookup failed: %s", exc)
                report.errors.append(str(exc))
                report.skipped_no_relay = len(needs_lookup)
                results = {}
            for number, registered in results.items():
                lead_id = by_number.get(number)
                if lead_id is None:
                    continue
                report.from_lookup += 1
                if registered is True:
                    verdicts[lead_id] = YES
                elif registered is False:
                    verdicts[lead_id] = NO
                else:
                    # None means the lookup itself failed. Recorded as
                    # unknown so the next run tries again rather than
                    # retiring a lead over one bad request.
                    verdicts[lead_id] = UNKNOWN

    if not verdicts:
        return report

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sheets.update_column(schema.RESEARCH, "WhatsApp Reachable", verdicts)
    sheets.update_column(
        schema.RESEARCH,
        "WhatsApp Checked At",
        {lead_id: now for lead_id in verdicts},
    )

    report.checked = len(verdicts)
    report.reachable = sum(1 for v in verdicts.values() if v == YES)
    report.unreachable = sum(1 for v in verdicts.values() if v == NO)
    report.still_unknown = sum(1 for v in verdicts.values() if v == UNKNOWN)
    return report


def _default_client(config):
    """The relay client, or None when WhatsApp is switched off entirely.

    The settings are flat on `outreach` -- `whatsapp_enabled`,
    `whatsapp_relay_port` -- not a nested `whatsapp` object. This once read
    the nested shape through getattr, found nothing, and returned None on a
    campaign where WhatsApp was switched on: no lookup ever ran, every US
    lead stayed unchecked, and sites were built for people who could not be
    messaged. Read the real attributes, and let a missing one raise.
    """
    if not config.outreach.whatsapp_enabled:
        return None
    from ..providers.whatsapp import WhatsAppClient

    return WhatsAppClient(port=config.outreach.whatsapp_relay_port)


def has_email(research_row: dict[str, str]) -> bool:
    email = (research_row.get("Email Found") or "").strip().lower()
    return bool(email) and email != "not found"


def is_contactable(research_row: dict[str, str]) -> bool:
    """Whether building this lead a site is worth paying for.

    Needs a *confirmed* channel: reachable on WhatsApp, or holding an email.
    A lead whose WhatsApp check has not run is NOT contactable yet -- it is
    unknown, and building on unknown is exactly the cost this gate exists to
    avoid. Those leads hold, and the moment the check runs they either
    qualify or they do not.

    This used to let unchecked leads through, on the reasoning that a lookup
    which has not run is not evidence. True, but the conclusion was wrong:
    the safe thing to do with no evidence is wait, not spend. Five US leads
    went all the way to a built site and only then turned out to be
    unmessageable.
    """
    reachable = (research_row.get("WhatsApp Reachable") or "").strip().lower()
    if reachable == YES:
        return True
    # An email is a confirmed channel in its own right, so it qualifies a
    # lead whether or not WhatsApp ever answers for it.
    return has_email(research_row)


def is_waiting_on_a_check(research_row: dict[str, str]) -> bool:
    """Held back only because the WhatsApp check has not answered yet.

    Distinguished from a real "no" so the report can tell Lyon to connect
    the relay, rather than leaving him to wonder where his leads went.
    """
    reachable = (research_row.get("WhatsApp Reachable") or "").strip().lower()
    return reachable in {"", UNKNOWN} and not has_email(research_row)


def can_whatsapp(
    lead: dict[str, str],
    research_row: dict[str, str] | None = None,
    default_country: str = "",
) -> bool:
    """Can this lead receive a WhatsApp message?

    The cached verdict wins when there is one: it came from WhatsApp itself
    and is the only thing that can answer for a US or Canadian number. The
    numbering-plan guess is the fallback for a lead the check has not reached
    yet, and it is a guess -- in an ambiguous country it says "no" where the
    honest answer is "not yet asked", so the check is what should decide.
    """
    # The cached verdict, and nothing else. It came from WhatsApp itself.
    # There used to be a numbering-plan fallback here for unchecked leads,
    # and it was wrong in both directions -- it read a UK landline as
    # unreachable when WhatsApp Business runs on fixed lines every day. An
    # unchecked lead is now simply not yet confirmed, and the build gate
    # holds it rather than guessing about it.
    verdict = ((research_row or {}).get("WhatsApp Reachable") or "").strip().lower()
    return verdict == YES
