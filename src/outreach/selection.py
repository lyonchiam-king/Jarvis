"""Which leads a stage should process on this run.

Every stage asks the same three questions, so they ask them in one place:

* Has this lead already been through me? (a row on my output tab)
* Has it already moved past me? (its Stage outranks my target)
* Is its upstream work done? (its Stage reaches my requirement)

Getting this right is what makes re-running a stage free. A lead that is
already done costs no API calls on the next run.
"""

from __future__ import annotations

from typing import Callable

from .stage import Stage, parse, rank


def has_social(lead: dict[str, str]) -> bool:
    """Whether this lead's only web presence is a social page."""
    return lead.get("Website Status") == "social_only"


def social_rank(lead: dict[str, str]) -> tuple[int, int]:
    """Socials first, then the order they already sit in.

    A business whose only web presence is a Facebook page is the better lead
    twice over. There is more to research -- a bio, photos, posts in their own
    voice -- so the brief and the demo come out richer. And they have already
    decided they want to be findable online, which is most of the argument
    already made.

    Beyond that one distinction it does not reorder anything. Sheet order is
    Google's own relevance order, it is the order you can see, and a queue you
    can predict is worth more than a marginally better-sorted one. Sorting is
    stable, so returning a constant second element preserves it.

    Lives here rather than in one stage because the preference has to hold all
    the way down the pipeline. Ranking only the research queue leaves an
    earlier backlog of website-less leads sitting at the front of every later
    stage, which is exactly what it looks like when the setting is ignored.
    """
    return (0 if has_social(lead) else 1, 0)


def social_then_reviews_rank(lead: dict[str, str]) -> tuple[int, int]:
    """Socials first, then most-reviewed first.

    Reviews are the raw material a brief is built from, so a business with two
    hundred of them yields a richer one than a business with four. The cost is
    predictability: the queue no longer follows the Sheet, which reads as
    random unless you happen to be looking at the review column.
    """
    try:
        reviews = int(lead.get("Review Count") or 0)
    except (TypeError, ValueError):
        reviews = 0
    return (0 if has_social(lead) else 1, -reviews)


# The orders a campaign may ask for, by the name used in `research.order`.
ORDERS = {
    "sheet": social_rank,
    "reviews": social_then_reviews_rank,
}


def select_pending(
    leads: dict[str, tuple[int, dict[str, str]]],
    done_ids: set[str],
    *,
    requires: Stage,
    target: Stage,
    batch_size: int,
    prefer: Callable[[dict[str, str]], object] | None = None,
) -> tuple[list[dict[str, str]], int, int]:
    """Return ``(selected, already_done, not_ready)``.

    ``requires`` is the stage a lead must have reached for this work to make
    sense; ``target`` is the stage this run will advance it to.

    ``prefer`` sorts the eligible leads before the batch cap is applied, so a
    bounded run spends its budget on the best candidates rather than on
    whichever happens to sit highest in the Sheet. The sort is stable, so leads
    of equal rank keep Sheet order.
    """
    eligible: list[dict[str, str]] = []
    already = 0
    not_ready = 0

    for lead_id, (_row_number, row) in leads.items():
        if lead_id in done_ids:
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

        if rank(current) >= rank(target):
            # Past this stage already, but missing its output row -- someone
            # deleted it, or an older run half-completed.
            already += 1
            continue

        if rank(current) < rank(requires):
            # Upstream stage hasn't run yet.
            not_ready += 1
            continue

        eligible.append(row)

    if prefer is not None:
        eligible.sort(key=prefer)

    return eligible[:batch_size], already, not_ready


def select_rewrite(
    leads: dict[str, tuple[int, dict[str, str]]],
    briefs: dict[str, tuple[int, dict[str, str]]],
    *,
    batch_size: int,
    prefer: Callable[[dict[str, str]], object] | None = None,
) -> tuple[list[dict[str, str]], int]:
    """Return ``(selected, already_built)`` for a prompt rewrite.

    The mirror image of ``select_pending``: a normal run skips any lead that
    already has a Site Briefs row, but a rewrite specifically wants those --
    it exists to push an improved prompt template out to leads select_pending
    would now consider "done". The one thing it must still refuse is a row
    Lovable has already built from: overwriting that prompt teaches nothing
    and the built site's link (Lovable Project ID, Preview URL) would have to
    survive the rewrite untouched regardless, so there's no reason to spend
    the model call on it.

    A Disqualified lead is excluded the same as it would be from any other
    stage, even if it still has a Site Briefs row from before it was marked.
    """
    eligible: list[dict[str, str]] = []
    already_built = 0

    for lead_id, (_row_number, brief_row) in briefs.items():
        if (brief_row.get("Lovable Project ID") or "").strip():
            already_built += 1
            continue

        lead_match = leads.get(lead_id)
        if lead_match is None:
            continue
        _lead_row_number, lead = lead_match
        if parse(lead.get("Stage")) is Stage.DISQUALIFIED:
            continue

        eligible.append(lead)

    if prefer is not None:
        eligible.sort(key=prefer)

    return eligible[:batch_size], already_built
