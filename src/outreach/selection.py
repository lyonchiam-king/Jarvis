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


def social_rank(lead: dict[str, str]) -> tuple[int, int]:
    """Sort key putting the leads worth working on first.

    A business whose only web presence is a Facebook page is the better lead
    twice over. There is more to research -- a bio, photos, posts in their own
    voice -- so the brief and the demo come out richer. And they have already
    decided they want to be findable online, which is most of the argument
    already made.

    Within each group, more reviews first: reviews are the raw material the
    research brief is built from, and a business with three of them yields a
    thin one.

    Lives here rather than in one stage because the preference has to hold all
    the way down the pipeline. Ranking only the research queue leaves an
    earlier backlog of website-less leads sitting at the front of every later
    stage, which is exactly what it looks like when the setting is ignored.
    """
    has_social = 0 if lead.get("Website Status") == "social_only" else 1
    try:
        reviews = int(lead.get("Review Count") or 0)
    except (TypeError, ValueError):
        reviews = 0
    return (has_social, -reviews)


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
