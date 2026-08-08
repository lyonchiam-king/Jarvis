"""Pipeline stages, ordered.

The ordering matters: stage transitions are forward-only. Re-running discovery
over a lead you have already emailed must not reset it to "Discovered", or you
would email the same business twice.
"""

from __future__ import annotations

from enum import Enum


class Stage(str, Enum):
    DISCOVERED = "Discovered"
    RESEARCHED = "Researched"
    SITE_BRIEFED = "Site Briefed"
    SITE_BUILT = "Site Built"
    EMAIL_DRAFTED = "Email Drafted"
    SENT = "Sent"
    REPLIED = "Replied"
    CLOSED = "Closed"

    # Terminal opt-out. Set by hand when a business asks not to be contacted.
    # Never advanced past by automation.
    DISQUALIFIED = "Disqualified"


# Rank used for forward-only comparison. Disqualified sits above everything so
# no automated stage can climb out of it.
_ORDER: dict[Stage, int] = {
    Stage.DISCOVERED: 0,
    Stage.RESEARCHED: 1,
    Stage.SITE_BRIEFED: 2,
    Stage.SITE_BUILT: 3,
    Stage.EMAIL_DRAFTED: 4,
    Stage.SENT: 5,
    Stage.REPLIED: 6,
    Stage.CLOSED: 7,
    Stage.DISQUALIFIED: 99,
}

# Stages the pipeline sets automatically. Everything else is yours to set by
# hand in the Sheet, and automation will not touch it.
AUTOMATED_STAGES = frozenset(
    {
        Stage.DISCOVERED,
        Stage.RESEARCHED,
        Stage.SITE_BRIEFED,
        Stage.SITE_BUILT,
        Stage.EMAIL_DRAFTED,
    }
)


def rank(stage: Stage) -> int:
    return _ORDER[stage]


def parse(value: str | None) -> Stage | None:
    """Parse a Stage from a Sheet cell, tolerating case and whitespace drift.

    Returns None for blank or unrecognised values -- a human may have typed
    anything into that column, and we would rather treat it as "unknown" than
    crash a run.
    """
    if not value:
        return None
    needle = str(value).strip().casefold()
    for stage in Stage:
        if stage.value.casefold() == needle:
            return stage
    return None


def advance(current: str | Stage | None, target: Stage) -> Stage:
    """Return the stage to write, never moving a lead backwards.

    An unrecognised current value is treated as "no stage yet", so a typo in
    the Sheet self-heals on the next run rather than pinning the lead forever.
    """
    if isinstance(current, Stage):
        current_stage: Stage | None = current
    else:
        current_stage = parse(current)

    if current_stage is None:
        return target
    return current_stage if rank(current_stage) >= rank(target) else target
