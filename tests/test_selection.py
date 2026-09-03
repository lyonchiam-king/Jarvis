"""select_pending's idea of "done", and how it heals a missing output row."""

from outreach.selection import select_pending
from outreach.stage import Stage


def leads(*stages):
    return {
        f"lead{i}": (i + 2, {"Lead ID": f"lead{i}", "Stage": stage})
        for i, stage in enumerate(stages)
    }


def test_a_lead_past_the_target_without_an_output_row_is_healed_not_dropped():
    """A half-completed run, or a row someone deleted, used to count as
    done -- so the lead sat past the target with nothing for the next stage
    to read, for ever. It is eligible again now."""
    # Research is target Researched; this lead is already Email Drafted.
    selected, already, _not_ready = select_pending(
        leads("Email Drafted"), set(),
        requires=Stage.DISCOVERED, target=Stage.RESEARCHED, batch_size=10,
    )

    assert [r["Lead ID"] for r in selected] == ["lead0"]
    assert already == 0


def test_a_lead_with_an_output_row_is_still_done():
    """The normal case is untouched: the row on the output tab is what
    makes a lead done, via done_ids."""
    selected, already, _not_ready = select_pending(
        leads("Researched"), {"lead0"},
        requires=Stage.DISCOVERED, target=Stage.RESEARCHED, batch_size=10,
    )

    assert selected == []
    assert already == 1


def test_a_lead_not_yet_researched_is_still_not_ready():
    selected, _already, not_ready = select_pending(
        leads("Discovered"), set(),
        requires=Stage.RESEARCHED, target=Stage.SITE_BRIEFED, batch_size=10,
    )

    assert selected == []
    assert not_ready == 1