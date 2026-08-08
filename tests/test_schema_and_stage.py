from outreach import schema
from outreach.schema import Column, Tab, column_letter
from outreach.stage import Stage, advance, parse


def test_column_letter_handles_multi_letter():
    assert column_letter(0) == "A"
    assert column_letter(25) == "Z"
    assert column_letter(26) == "AA"
    assert column_letter(27) == "AB"


def test_automated_runs_splits_around_manual_columns():
    tab = Tab(
        "T",
        (
            Column("a"),
            Column("b"),
            Column("m1", manual=True),
            Column("c"),
            Column("m2", manual=True),
        ),
    )
    assert tab.automated_runs() == [(0, 2), (3, 4)]


def test_automated_runs_when_trailing_columns_are_automated():
    tab = Tab("T", (Column("m", manual=True), Column("a"), Column("b")))
    assert tab.automated_runs() == [(1, 3)]


def test_leads_manual_columns_are_trailing():
    # The upsert writes contiguous runs; keeping manual columns at the end of
    # Leads means a refresh is a single range write.
    assert schema.LEADS.automated_runs() == [(0, 15)]
    manual = [c.name for c in schema.LEADS.columns if c.manual]
    assert manual == ["Priority", "Owner Notes", "Next Action"]


def test_every_tab_is_keyed_on_a_stable_first_column():
    for tab in schema.ALL_TABS:
        assert not tab.columns[0].manual, f"{tab.title} key column must be automated"


def test_stage_never_moves_backwards():
    assert advance("Email Drafted", Stage.DISCOVERED) is Stage.EMAIL_DRAFTED
    assert advance("Discovered", Stage.RESEARCHED) is Stage.RESEARCHED
    assert advance("Replied", Stage.EMAIL_DRAFTED) is Stage.REPLIED


def test_disqualified_is_terminal():
    assert advance("Disqualified", Stage.RESEARCHED) is Stage.DISQUALIFIED


def test_blank_or_unknown_stage_self_heals():
    assert advance("", Stage.DISCOVERED) is Stage.DISCOVERED
    assert advance(None, Stage.DISCOVERED) is Stage.DISCOVERED
    assert advance("typo the user made", Stage.DISCOVERED) is Stage.DISCOVERED


def test_stage_parse_tolerates_human_typing():
    assert parse("  site briefed ") is Stage.SITE_BRIEFED
    assert parse("EMAIL DRAFTED") is Stage.EMAIL_DRAFTED
    assert parse("nonsense") is None
