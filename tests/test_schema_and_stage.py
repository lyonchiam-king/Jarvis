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


def test_leads_manual_columns_stay_one_untouched_block():
    """Was ...are_trailing, asserting a single automated run.

    Two rules met here and only one can win. Columns are append-only,
    because the Sheet is read by position and inserting one shifts every
    value on every existing row; manual columns being last was only ever an
    efficiency note, since it made a refresh one range write. So the
    WhatsApp verdict was appended after the manual block, the refresh now
    costs two range writes, and what still matters is enforced: the manual
    columns remain a contiguous block that automation never writes.
    """
    runs = schema.LEADS.automated_runs()
    assert runs == [(0, 15), (18, 20)]

    manual = [c.name for c in schema.LEADS.columns if c.manual]
    assert manual == ["Priority", "Owner Notes", "Next Action"]

    # No automated run may overlap the manual block.
    for start, end in runs:
        assert not any(schema.LEADS.columns[i].manual for i in range(start, end))


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


# ------------------------------------------- column order is a migration hazard


def test_column_order_is_append_only():
    """Rows are read by position, so inserting a column mid-tab corrupts data.

    ``read_rows`` zips a row's cells against the *schema's* header list, not
    the header row in the Sheet. On a Sheet that already has data, inserting a
    column anywhere but the end shifts every value after it by one -- silently,
    and only for existing rows.

    So this is a golden list. Appending to the end of a tuple is fine and this
    test will tell you to add the name here. If you need to insert or reorder,
    that is a data migration, not an edit.
    """
    from outreach import schema

    expected = {
        "Leads": [
            "Lead ID", "Stage", "Business Name", "Category", "Address", "Phone",
            "Rating", "Review Count", "Website Status", "Website URL",
            "Maps URL", "Campaign", "Country", "Discovered At", "Last Refreshed",
            "Priority", "Owner Notes", "Next Action",
            "WhatsApp Reachable", "WhatsApp Checked At",
        ],
        "Research": [
            "Lead ID", "Business Name", "Tone", "Services", "Price Tier",
            "Customer Praise", "Customer Pain Points", "Suggested Website Angle",
            "Social Handle", "Social Bio", "Social Recent Post",
            "Social Lookup Status", "Email Found", "Email Source",
            "Research Status", "Researched At", "Social Profiles",
            "WhatsApp Reachable", "WhatsApp Checked At",
        ],
        "Site Briefs": [
            "Lead ID", "Business Name", "Lovable Prompt", "Approval Status",
            "Lovable Project ID", "Preview URL", "Created At",
        ],
        "Outreach Log": [
            "Lead ID", "Business Name", "Channel", "Recipient", "Email Source",
            "Subject", "Body Preview", "Gmail Draft ID", "Gmail Draft Link",
            "WhatsApp Message", "Drafted At", "Status", "Sent At", "Notes",
            "WhatsApp Sent At", "WhatsApp Status",
        ],
        "Errors": [
            "Timestamp", "Stage", "Lead ID", "Business Name", "Error Type",
            "Message", "Retryable",
        ],
    }

    for tab in schema.ALL_TABS:
        assert tab.headers == expected[tab.title], (
            f"{tab.title} column order changed. If you appended a column, add "
            f"its name to the end of this list. If you inserted or reordered "
            f"one, existing Sheets will misread every row after it."
        )


def test_the_new_social_column_is_last_on_research():
    """Proves the append-only rule was followed for the column just added."""
    from outreach import schema

    assert schema.RESEARCH.headers[-1] == "WhatsApp Checked At"


def test_a_row_written_before_a_new_column_existed_still_reads():
    """An existing Sheet has shorter rows; they must pad, not shift."""
    from outreach import schema

    old_row = ["lead-1", "Salon A"] + [""] * 14   # 16 cells, pre-"Social Profiles"
    padded = old_row + [""] * (len(schema.RESEARCH.columns) - len(old_row))
    parsed = dict(zip(schema.RESEARCH.headers, padded))

    assert parsed["Lead ID"] == "lead-1"
    assert parsed["Business Name"] == "Salon A"
    assert parsed["Social Profiles"] == ""
