"""The visual-brief stage, and what the draft does with its output.

Two things are load-bearing here and both are about a lead not being lost:
the stage must never touch the Stage column (a visual brief is not a step in
that sequence), and a lead whose graphic has not come back must be HELD
rather than drafted -- a drafted row advances the Stage and retires it, so
when the image finally arrived nothing would ever message them.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from outreach import schema, visual
from outreach.config import load_config
from outreach.providers.llm import GapCard, OutreachCopy
from outreach.stages import outreach_draft, visual_brief

PENANG = "config/campaigns/penang-my.yaml"


def cfg(track="visual", **visual_over):
    base = load_config(PENANG, env_file=None)
    v = dict(signature="by Lyon, Business Growth Consultant")
    v.update(visual_over)
    return replace(
        base,
        outreach=replace(base.outreach, track=track, postal_address="1 Road, Penang"),
        visual=replace(base.visual, **v),
    )


def lead(lead_id, *, name=None, category="Nail salon", stage="Researched",
         website="none", reviews="42", rating="4.8", phone="012-3456789"):
    return {
        "Lead ID": lead_id,
        "Stage": stage,
        "Business Name": name or f"Salon {lead_id}",
        "Category": category,
        "Phone": phone,
        "Website Status": website,
        "Rating": rating,
        "Review Count": reviews,
    }


def research(lead_id, *, pain="Hard to get through on the phone", email="",
             whatsapp="yes"):
    return {
        "Lead ID": lead_id,
        "Business Name": f"Salon {lead_id}",
        "Tone": "warm",
        "Services": "manicure, gel",
        "Customer Praise": "Friendly",
        "Customer Pain Points": pain,
        "Suggested Website Angle": "Online booking",
        "Email Found": email,
        "Email Source": "social_page" if email else "not found",
        "WhatsApp Reachable": whatsapp,
    }


def index(rows):
    return {r["Lead ID"]: (i + 2, r) for i, r in enumerate(rows)}


class FakeSheets:
    """Honours ``on_conflict``, because that is the whole point of the
    rewrite path -- a fake more permissive than the real client is how the
    manual-column bug once shipped past a green suite."""

    def __init__(self, leads, research_rows, visuals=None, outreach=None,
                 site_briefs=None):
        self._tabs = {
            schema.LEADS.title: leads,
            schema.RESEARCH.title: research_rows,
            schema.VISUAL_BRIEFS.title: visuals or {},
            schema.OUTREACH_LOG.title: outreach or {},
            schema.SITE_BRIEFS.title: site_briefs or {},
        }
        self.upserts = []
        self.column_writes = []
        self.appended = []
        self.spreadsheet_id = "fake"

    def read_rows(self, tab):
        return self._tabs.get(tab.title, {})

    def upsert(self, tab, rows, *, on_conflict=None):
        existing = self._tabs.setdefault(tab.title, {})
        written = []
        for row in rows:
            key = row.get("Lead ID")
            if key in existing and on_conflict is not None:
                row = on_conflict(existing[key][1], row)
            merged = {**(existing.get(key, (0, {}))[1]), **row}
            existing[key] = (len(existing) + 2, merged)
            written.append(row)
        self.upserts.append((tab, written))
        from outreach.sheets import UpsertResult

        return UpsertResult(inserted=len(written))

    def update_column(self, tab, column, values_by_key, *, user_edit=False):
        from outreach.sheets import guard_manual_column

        guard_manual_column(tab, column, user_edit=user_edit)
        self.column_writes.append((tab, column, dict(values_by_key)))
        return len(values_by_key)

    def append(self, tab, rows):
        self.appended.append((tab, list(rows)))
        return len(rows)


class FakeWriter:
    """Re-words every card by prefixing it, so a localised card is visible in
    the output and an invented one is too."""

    def __init__(self, *, fail=False, extra_card=False, wrong_ids=False,
                 drop_all=False):
        self.call_count = 0
        self.prompts = []
        self.timeouts = []
        self.fail = fail
        self.extra_card = extra_card
        self.wrong_ids = wrong_ids
        self.drop_all = drop_all

    def write_gap_wording(self, prompt, *, timeout=None):
        self.call_count += 1
        self.prompts.append(prompt)
        self.timeouts.append(timeout)
        if self.fail:
            raise RuntimeError("the model fell over")
        if self.drop_all:
            return ()
        ids = []
        for line in prompt.splitlines():
            if line.strip().startswith("- id:"):
                ids.append(line.split(":", 1)[1].strip())
        if self.wrong_ids:
            ids = [f"invented_{i}" for i in ids]
        out = [GapCard(id=i, problem=f"SALON {i}", fix=f"FIX {i}") for i in ids]
        if self.extra_card:
            out.append(GapCard(id="made_up", problem="Made up", fix="Nonsense"))
        return tuple(out)


def rows_of(sheets):
    """Every Visual Briefs row written, in order."""
    return [r for tab, rows in sheets.upserts if tab is schema.VISUAL_BRIEFS
            for r in rows]


# ------------------------------------------------------------------ the run


def test_a_prompt_is_written_per_researched_lead():
    sheets = FakeSheets(index([lead("a"), lead("b")]),
                        index([research("a"), research("b")]))
    report = visual_brief.run(cfg(), sheets, FakeWriter())
    assert report.prompts_written == 2
    assert report.errors == 0
    written = rows_of(sheets)
    assert {r["Lead ID"] for r in written} == {"a", "b"}
    for row in written:
        assert row["Image Prompt"].strip()
        assert row["Gaps Used"].strip()
        assert set(row) == set(schema.VISUAL_BRIEFS.headers)


def test_the_stage_column_is_never_touched():
    """A visual brief is not a step in the Stage sequence. Writing one would
    put two stages at the same rank, and the first casualty would be the site
    brief silently failing to record itself."""
    sheets = FakeSheets(index([lead("a")]), index([research("a")]))
    visual_brief.run(cfg(), sheets, FakeWriter())
    assert sheets.column_writes == []


def test_a_lead_that_already_has_a_prompt_costs_nothing():
    existing = index([{"Lead ID": "a", "Style": "paper-cut", "Layout": "rows"}])
    sheets = FakeSheets(index([lead("a")]), index([research("a")]), visuals=existing)
    writer = FakeWriter()
    report = visual_brief.run(cfg(), sheets, writer)
    assert report.already_done == 1
    assert report.prompts_written == 0
    assert writer.call_count == 0


def test_an_unresearched_lead_is_not_briefed():
    sheets = FakeSheets(index([lead("a", stage="Discovered")]), {})
    report = visual_brief.run(cfg(), sheets, FakeWriter())
    assert report.not_ready == 1
    assert report.prompts_written == 0


def test_a_lead_with_no_channel_at_all_is_skipped():
    """A graphic costs a model call and then Lyon's own time in the image
    tool. Spending either on a business nobody can contact is the waste the
    reachability check exists to prevent."""
    sheets = FakeSheets(
        index([lead("a")]),
        index([research("a", whatsapp="no", email="")]),
    )
    report = visual_brief.run(cfg(), sheets, FakeWriter())
    assert report.unreachable == 1
    assert report.prompts_written == 0


def test_an_unchecked_lead_is_still_briefed_rather_than_dropped():
    sheets = FakeSheets(index([lead("a")]),
                        index([research("a", whatsapp="unknown")]))
    report = visual_brief.run(cfg(), sheets, FakeWriter())
    assert report.unreachable == 0
    assert report.waiting_on_check + report.prompts_written >= 1


def test_the_rotation_moves_on_between_leads_in_one_run():
    """Ten salons in one batch must not all get the same graphic."""
    leads = [lead(k) for k in "abcd"]
    sheets = FakeSheets(index(leads), index([research(k) for k in "abcd"]))
    visual_brief.run(cfg(), sheets, FakeWriter())
    pairs = [(r["Style"], r["Layout"]) for r in rows_of(sheets)]
    assert len(pairs) == 4
    for earlier, later in zip(pairs, pairs[1:]):
        assert earlier != later, pairs


def test_the_rotation_remembers_the_last_run():
    """Without this every run restarts at the top of the ring and ten salons
    briefed over ten days all get the same graphic."""
    industry = visual.industry_for("Nail salon")
    first = visual.next_pair(industry)
    already = index([{**lead("a"), "Style": first[0], "Layout": first[1]}])
    sheets = FakeSheets(index([lead("a"), lead("b")]),
                        index([research("a"), research("b")]),
                        visuals=already)
    visual_brief.run(cfg(), sheets, FakeWriter())
    written = rows_of(sheets)
    assert len(written) == 1 and written[0]["Lead ID"] == "b"
    assert (written[0]["Style"], written[0]["Layout"]) != first


def test_a_blank_style_cell_does_not_disable_the_rotation():
    already = index([{**lead("a"), "Style": "", "Layout": ""}])
    pairs = visual_brief.last_pairs(index([lead("a")]), already, "hair salon")
    assert pairs == {}


def test_the_campaigns_allow_list_narrows_the_series():
    leads = [lead(k) for k in "abc"]
    sheets = FakeSheets(index(leads), index([research(k) for k in "abc"]))
    visual_brief.run(
        cfg(styles=("paper-cut",), layouts=("rows", "ladder")),
        sheets, FakeWriter(),
    )
    for row in rows_of(sheets):
        assert row["Style"] == "paper-cut"
        assert row["Layout"] in ("rows", "ladder")


# -------------------------------------------------------- what a card says


def test_a_thin_lead_gets_the_generic_graphic_rather_than_invented_claims():
    thin = lead("a", website="real", reviews="0", rating="")
    sheets = FakeSheets(index([thin]),
                        index([research("a", pain="none evident",
                                        email="hi@x.com")]))
    report = visual_brief.run(cfg(), sheets, FakeWriter())
    assert report.generic == 1 and report.specific == 0
    row = rows_of(sheets)[0]
    assert row["Specific"] == "no"
    assert "Salon a" not in row["Image Prompt"]
    assert "for your business" in row["Image Prompt"]


def test_a_well_researched_lead_gets_its_own_name_on_the_graphic():
    sheets = FakeSheets(index([lead("a", name="Your Nail Crush")]),
                        index([research("a")]))
    report = visual_brief.run(cfg(), sheets, FakeWriter())
    assert report.specific == 1 and report.generic == 0
    row = rows_of(sheets)[0]
    assert row["Specific"] == "yes"
    assert "Your Nail Crush" in row["Image Prompt"]


def test_the_model_cannot_add_a_card():
    """The system prompt asks; this makes asking unnecessary."""
    sheets = FakeSheets(index([lead("a")]), index([research("a")]))
    visual_brief.run(cfg(), sheets, FakeWriter(extra_card=True))
    prompt = rows_of(sheets)[0]["Image Prompt"]
    assert "Made up" not in prompt
    assert '4: "' not in prompt


def test_a_card_the_model_renamed_keeps_the_library_wording():
    """An id it invented cannot be matched back to its evidence, so the card
    it was meant to replace must survive unchanged rather than vanish."""
    sheets = FakeSheets(index([lead("a")]), index([research("a")]))
    visual_brief.run(cfg(), sheets, FakeWriter(wrong_ids=True))
    prompt = rows_of(sheets)[0]["Image Prompt"]
    assert "SALON " not in prompt
    assert "The same question keeps coming up" in prompt


def test_a_model_that_returns_nothing_leaves_three_true_cards():
    sheets = FakeSheets(index([lead("a")]), index([research("a")]))
    visual_brief.run(cfg(), sheets, FakeWriter(drop_all=True))
    prompt = rows_of(sheets)[0]["Image Prompt"]
    assert '3: "' in prompt


def test_a_failed_wording_call_is_reported_not_fatal():
    """An optional step catches every exception and says why in the summary,
    because the only other trace is a log nobody reads."""
    sheets = FakeSheets(index([lead("a")]), index([research("a")]))
    report = visual_brief.run(cfg(), sheets, FakeWriter(fail=True))
    assert report.prompts_written == 1
    assert report.errors == 0
    assert report.wording_failures == 1
    assert "RuntimeError" in report.wording_reason
    assert "the model fell over" in report.wording_reason
    assert "\n".join(report.summary_lines()).count("standard wording kept") == 1


def test_switching_off_localisation_spends_no_model_call():
    sheets = FakeSheets(index([lead("a")]), index([research("a")]))
    writer = FakeWriter()
    visual_brief.run(cfg(localise_wording=False), sheets, writer)
    assert writer.call_count == 0
    assert "The same question keeps coming up" in rows_of(sheets)[0]["Image Prompt"]


def test_the_generic_graphic_spends_no_model_call():
    """Its cards are not claims about this business, so there is nothing to
    localise and nothing to spend on."""
    thin = lead("a", website="real", reviews="0", rating="")
    sheets = FakeSheets(index([thin]),
                        index([research("a", pain="", email="hi@x.com")]))
    writer = FakeWriter()
    visual_brief.run(cfg(), sheets, writer)
    assert writer.call_count == 0


# ------------------------------------------------------------- regenerate


def test_regenerating_keeps_the_image_and_the_confirmation(tmp_path, monkeypatch):
    """The image cost a generation in the image tool and the tick cost a
    person reading the words on it."""
    monkeypatch.setenv("OUTREACH_STATE_DIR", str(tmp_path))
    before = index([{
        "Lead ID": "a", "Business Name": "Salon a", "Style": "paper-cut",
        "Layout": "rows", "Image File": "visuals/salon-a.png",
        "Image Source": "upload", "Text Confirmed": "yes",
        "Created At": "2026-01-01T00:00:00Z", "Approval Status": "Approved",
    }])
    sheets = FakeSheets(index([lead("a")]), index([research("a")]), visuals=before)
    out = visual_brief.regenerate_one(cfg(), sheets, FakeWriter(), "a")
    row = sheets.read_rows(schema.VISUAL_BRIEFS)["a"][1]
    assert row["Image File"] == "visuals/salon-a.png"
    assert row["Text Confirmed"] == "yes"
    assert row["Created At"] == "2026-01-01T00:00:00Z"
    assert out["prompt"].strip()


def test_regenerating_rotates_off_the_pair_it_had():
    """Pressing it twice must move through the ring, not return the same
    graphic."""
    before = index([{"Lead ID": "a", "Style": "paper-cut", "Layout": "rows"}])
    sheets = FakeSheets(index([lead("a")]), index([research("a")]), visuals=before)
    out = visual_brief.regenerate_one(cfg(), sheets, FakeWriter(), "a")
    assert (out["style"], out["layout"]) != ("paper-cut", "rows")


def test_regenerating_never_un_skips_a_prompt_you_declined():
    before = index([{"Lead ID": "a", "Style": "paper-cut", "Layout": "rows",
                     "Approval Status": "Skip"}])
    sheets = FakeSheets(index([lead("a")]), index([research("a")]), visuals=before)
    visual_brief.regenerate_one(cfg(), sheets, FakeWriter(), "a")
    assert sheets.read_rows(schema.VISUAL_BRIEFS)["a"][1]["Approval Status"] == "Skip"


def test_regenerating_a_lead_with_no_research_says_so():
    sheets = FakeSheets(index([lead("a")]), {})
    with pytest.raises(ValueError, match="no Research row"):
        visual_brief.regenerate_one(cfg(), sheets, FakeWriter(), "a")


def test_preserve_image_fields_leaves_the_prompt_new():
    out = visual_brief.preserve_image_fields(
        {"Image File": "visuals/x.png", "Text Confirmed": "yes",
         "Image Source": "url", "Created At": "then"},
        {"Image Prompt": "NEW", "Image File": "", "Text Confirmed": "",
         "Image Source": "", "Created At": "now"},
    )
    assert out["Image Prompt"] == "NEW"
    assert out["Image File"] == "visuals/x.png"
    assert out["Text Confirmed"] == "yes"
    assert out["Created At"] == "then"


# ----------------------------------------- what the draft does with all this


class CopyWriter:
    def __init__(self):
        self.call_count = 0
        self.pitches = []
        self.has_pages = []
        self.specifics = []

    def write_outreach_copy(self, prompt, *, angle="no-website",
                            offers_follow_up=False, pitch="site",
                            has_page=False, specific=True):
        self.call_count += 1
        self.pitches.append(pitch)
        self.has_pages.append(has_page)
        self.specifics.append(specific)
        return OutreachCopy(
            subject="A thought",
            email_body="Body.",
            whatsapp_message="Hi, Lyon here.",
        )


class NoGmail:
    def create_draft(self, **_):  # pragma: no cover -- never reached
        raise AssertionError("no email lead should reach Gmail")


def confirmed(tmp_path, lead_id="a", specific="yes"):
    """A Visual Briefs row whose image really is on disk and really is ticked."""
    visual.visuals_dir().mkdir(parents=True, exist_ok=True)
    (visual.visuals_dir() / f"{lead_id}.png").write_bytes(b"pretend png")
    return index([{
        "Lead ID": lead_id,
        "Business Name": f"Salon {lead_id}",
        "Image File": f"visuals/{lead_id}.png",
        "Text Confirmed": "yes",
        "Specific": specific,
        "Approval Status": "Approved",
    }])


def test_a_lead_whose_graphic_has_not_come_back_is_held_not_drafted():
    """A drafted row advances the Stage and retires the lead, so when the
    image finally arrived nothing would ever message them."""
    sheets = FakeSheets(index([lead("a")]), index([research("a")]))
    writer = CopyWriter()
    report = outreach_draft.run(cfg("visual"), sheets, writer, None)
    assert report.held_for_image == 1
    assert report.copy_written == 0
    assert writer.call_count == 0
    assert sheets.upserts == []
    assert sheets.column_writes == []
    assert "waiting on a graphic" in "\n".join(report.summary_lines())


def test_an_image_on_disk_that_nobody_checked_is_still_a_hold(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTREACH_STATE_DIR", str(tmp_path))
    visual.visuals_dir().mkdir(parents=True)
    (visual.visuals_dir() / "a.png").write_bytes(b"png")
    unticked = index([{"Lead ID": "a", "Image File": "visuals/a.png",
                       "Text Confirmed": "", "Specific": "yes"}])
    sheets = FakeSheets(index([lead("a")]), index([research("a")]), visuals=unticked)
    report = outreach_draft.run(cfg("visual"), sheets, CopyWriter(), None)
    assert report.held_for_image == 1


def test_a_confirmed_graphic_releases_the_lead_and_pitches_the_picture(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OUTREACH_STATE_DIR", str(tmp_path))
    sheets = FakeSheets(index([lead("a")]), index([research("a")]),
                        visuals=confirmed(tmp_path))
    writer = CopyWriter()
    report = outreach_draft.run(cfg("visual"), sheets, writer, None)
    assert report.held_for_image == 0
    assert report.copy_written == 1
    assert writer.pitches == ["visual"]
    assert writer.specifics == [True]
    assert writer.has_pages == [False]


def test_a_generic_graphic_is_never_described_as_made_for_them(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OUTREACH_STATE_DIR", str(tmp_path))
    sheets = FakeSheets(index([lead("a")]), index([research("a")]),
                        visuals=confirmed(tmp_path, specific="no"))
    writer = CopyWriter()
    outreach_draft.run(cfg("visual"), sheets, writer, None)
    assert writer.specifics == [False]


def test_both_tracks_send_one_message_carrying_the_page_link(
    tmp_path, monkeypatch
):
    """Messaging the same business twice in a day is the thing this pipeline
    works hardest to avoid."""
    monkeypatch.setenv("OUTREACH_STATE_DIR", str(tmp_path))
    briefs = index([{"Lead ID": "a", "Preview URL": "https://example.com/a/"}])
    sheets = FakeSheets(
        index([lead("a", stage="Site Built")]),
        index([research("a")]),
        visuals=confirmed(tmp_path),
        site_briefs=briefs,
    )
    writer = CopyWriter()
    report = outreach_draft.run(cfg("both"), sheets, writer, None)
    assert report.copy_written == 1
    assert writer.pitches == ["visual"]
    assert writer.has_pages == [True]
    rows = [r for tab, rows in sheets.upserts if tab is schema.OUTREACH_LOG
            for r in rows]
    assert len(rows) == 1, "one message, not two"


def test_a_graphic_marked_skip_is_a_decision_not_a_wait(tmp_path, monkeypatch):
    """Holding forever on a prompt you deliberately retired would strand the
    lead; falling through to the page pitch is the honest answer."""
    monkeypatch.setenv("OUTREACH_STATE_DIR", str(tmp_path))
    skipped = index([{"Lead ID": "a", "Approval Status": "Skip",
                      "Image File": "", "Text Confirmed": ""}])
    sheets = FakeSheets(index([lead("a")]), index([research("a")]),
                        visuals=skipped)
    writer = CopyWriter()
    report = outreach_draft.run(cfg("visual"), sheets, writer, None)
    assert report.held_for_image == 0
    assert writer.pitches == ["site"]


def test_the_original_track_never_reads_the_visual_tab():
    """A campaign on the original track must not pay a Sheet round trip for a
    tab it has nothing in."""
    sheets = FakeSheets(index([lead("a", stage="Site Built")]),
                        index([research("a")]))
    seen = []
    plain = sheets.read_rows

    def spy(tab):
        seen.append(tab.title)
        return plain(tab)

    sheets.read_rows = spy
    writer = CopyWriter()
    outreach_draft.run(cfg("website"), sheets, writer, None)
    assert schema.VISUAL_BRIEFS.title not in seen
    assert writer.pitches == ["site"]
    assert writer.specifics == [True]


def test_the_graphic_is_the_attachment_when_there_is_one(tmp_path, monkeypatch):
    """A screenshot of a site beside a message arguing about three gaps is the
    pitch contradicting itself."""
    monkeypatch.setenv("OUTREACH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        outreach_draft, "capture_site_shot",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not screenshot when a graphic exists")
        ),
    )
    row = confirmed(tmp_path)["a"][1]
    path, kind = outreach_draft.attachment_for(
        cfg("visual"), lead("a"), "a", "https://example.com/a/", row
    )
    assert kind == "visual"
    assert path is not None and path.name == "a.png"


def test_the_screenshot_is_the_attachment_when_there_is_no_graphic(monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(
        outreach_draft, "capture_site_shot", lambda *a, **k: Path("/tmp/shot.png")
    )
    path, kind = outreach_draft.attachment_for(
        cfg("website"), lead("a"), "a", "https://example.com/a/", None
    )
    assert kind == "site" and path == Path("/tmp/shot.png")


def test_no_picture_at_all_is_an_empty_kind_not_a_crash(monkeypatch):
    monkeypatch.setattr(outreach_draft, "capture_site_shot", lambda *a, **k: None)
    path, kind = outreach_draft.attachment_for(cfg("website"), lead("a"), "a")
    assert path is None and kind == ""


def test_a_broken_image_path_cannot_kill_the_draft(monkeypatch):
    """The picture is never worth the message."""
    from pathlib import Path

    def explode(_row):
        raise OSError("the disk went away")

    monkeypatch.setattr(outreach_draft.visual_lib, "confirmed_image", explode)
    monkeypatch.setattr(
        outreach_draft, "capture_site_shot", lambda *a, **k: Path("/tmp/shot.png")
    )
    path, kind = outreach_draft.attachment_for(
        cfg("visual"), lead("a"), "a", "", {"Image File": "visuals/a.png"}
    )
    assert kind == "site"


def test_an_attached_graphic_is_not_reported_as_a_site_screenshot(
    tmp_path, monkeypatch
):
    """"with a site screenshot" is true of a photographed page and a lie
    about a graphic. One number covering both makes the summary describe work
    that did not happen."""
    monkeypatch.setenv("OUTREACH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(outreach_draft, "capture_site_shot", lambda *a, **k: None)
    sheets = FakeSheets(index([lead("a")]), index([research("a")]),
                        visuals=confirmed(tmp_path))
    report = outreach_draft.run(cfg("visual"), sheets, CopyWriter(), None)
    assert report.graphics_attached == 1
    assert report.screenshots == 0
    summary = "\n".join(report.summary_lines())
    assert "with an infographic" in summary
    assert "with a site screenshot" not in summary


# ------------------------------------------------ picking who a run is for
#
# Sixty prompts is not a batch anybody works through: each one is a visit to
# the image tool and a picture to read. `--limit` alone bounds the cost but
# picks whichever leads come first, which is not the same as picking the ones
# worth starting with.


def test_a_lead_on_whatsapp_is_grouped_by_the_channel_that_will_be_used():
    """WhatsApp wins over an email it also has: that is where the message
    goes, so calling it "email only" would put it in a batch picked for a
    different reason."""
    assert visual_brief.group_of(
        {"WhatsApp Reachable": "yes"}, {"Email Found": "hi@x.com"}
    ) == "whatsapp"


def test_a_lead_with_only_an_email_is_its_own_group():
    assert visual_brief.group_of(
        {"WhatsApp Reachable": "no"}, {"Email Found": "hi@x.com"}
    ) == "email"


def test_nobody_has_asked_yet_is_not_the_same_as_no_way_to_reach_them():
    """Missing evidence is not evidence of absence -- the whole point of the
    reachability check being able to answer later."""
    assert visual_brief.group_of({}, {"Email Found": ""}) == "unchecked"
    assert visual_brief.group_of(
        {"WhatsApp Reachable": "no"}, {"Email Found": ""}
    ) == "none"


def test_running_for_one_group_writes_for_that_group_only():
    sheets = FakeSheets(
        index([lead("w"), lead("e"), lead("u")]),
        index([
            research("w", whatsapp="yes"),
            research("e", whatsapp="no", email="hi@x.com"),
            research("u", whatsapp="unknown"),
        ]),
    )
    report = visual_brief.run(cfg(), sheets, FakeWriter(), groups=("whatsapp",))
    assert [r["Lead ID"] for r in rows_of(sheets)] == ["w"]
    assert report.filtered_out == 1  # the email lead; the unchecked one is held
    assert "outside what you picked" in "\n".join(report.summary_lines())


def test_the_limit_counts_what_was_asked_for_not_what_came_first():
    """Filtering the selected batch instead of the candidates would make
    "write 2 for the WhatsApp group" produce however many of the first two
    leads happened to be in it -- which looks exactly like the filter being
    ignored."""
    leads = [lead("e1"), lead("e2"), lead("w1"), lead("w2")]
    sheets = FakeSheets(
        index(leads),
        index([
            research("e1", whatsapp="no", email="a@x.com"),
            research("e2", whatsapp="no", email="b@x.com"),
            research("w1", whatsapp="yes"),
            research("w2", whatsapp="yes"),
        ]),
    )
    visual_brief.run(cfg(), sheets, FakeWriter(), limit=2, groups=("whatsapp",))
    assert sorted(r["Lead ID"] for r in rows_of(sheets)) == ["w1", "w2"]


def test_an_explicit_pick_writes_for_exactly_those():
    sheets = FakeSheets(
        index([lead(k) for k in "abc"]),
        index([research(k) for k in "abc"]),
    )
    visual_brief.run(cfg(), sheets, FakeWriter(), lead_ids={"a", "c"})
    assert sorted(r["Lead ID"] for r in rows_of(sheets)) == ["a", "c"]


def test_a_pick_and_a_group_narrow_together():
    sheets = FakeSheets(
        index([lead("w"), lead("e")]),
        index([
            research("w", whatsapp="yes"),
            research("e", whatsapp="no", email="hi@x.com"),
        ]),
    )
    visual_brief.run(
        cfg(), sheets, FakeWriter(), groups=("whatsapp",), lead_ids={"w", "e"}
    )
    assert [r["Lead ID"] for r in rows_of(sheets)] == ["w"]


def test_no_filter_at_all_behaves_exactly_as_before():
    """Every earlier caller passes neither, and must keep getting every
    eligible lead."""
    sheets = FakeSheets(
        index([lead("a"), lead("b")]), index([research("a"), research("b")])
    )
    report = visual_brief.run(cfg(), sheets, FakeWriter())
    assert report.prompts_written == 2
    assert report.filtered_out == 0
    assert "outside what you picked" not in "\n".join(report.summary_lines())


# ---------------------------------------- when the model stops answering
#
# Seen on a real run: ollama.com read-timed-out on the first lead and the
# fallback did its job -- but at 180 seconds a lead, ten leads would have been
# half an hour of waiting to produce exactly what not asking produces
# instantly.


class SlowWriter:
    """Fails every re-wording call, and records the timeout it was given."""

    def __init__(self, *, fail_after=0):
        self.call_count = 0
        self.timeouts = []
        self.fail_after = fail_after

    def write_gap_wording(self, prompt, *, timeout=None):
        self.call_count += 1
        self.timeouts.append(timeout)
        if self.call_count > self.fail_after:
            raise RuntimeError("Read timed out. (read timeout=180.0)")
        return ()


def test_the_rewording_call_gets_its_own_short_budget():
    """The writer's own default is sized for a whole site spec from a slow
    local model. This call returns a handful of short lines."""
    sheets = FakeSheets(index([lead("a")]), index([research("a")]))
    writer = SlowWriter(fail_after=5)
    visual_brief.run(cfg(), sheets, writer)
    assert writer.timeouts == [visual_brief.WORDING_TIMEOUT]
    assert visual_brief.WORDING_TIMEOUT <= 60, "a short answer does not need a minute"


def test_a_run_stops_asking_after_two_failures_in_a_row():
    leads = [lead(k) for k in "abcdefgh"]
    sheets = FakeSheets(index(leads), index([research(k) for k in "abcdefgh"]))
    writer = SlowWriter()
    report = visual_brief.run(cfg(), sheets, writer)

    assert report.prompts_written == 8, "every prompt must still be written"
    assert report.errors == 0
    assert writer.call_count == visual_brief.WORDING_GIVE_UP_AFTER, (
        "it went on paying the timeout after the endpoint was plainly down"
    )
    assert report.wording_given_up is True
    summary = "\n".join(report.summary_lines())
    assert "stopped answering" in summary
    assert "still true" in summary


def test_a_single_blip_does_not_stop_the_rest_of_the_run():
    """One failure is a blip. Giving up on fifty-eight leads because two
    scattered ones failed would lose a feature that was mostly working."""
    leads = [lead(k) for k in "abcd"]
    sheets = FakeSheets(index(leads), index([research(k) for k in "abcd"]))

    class Flaky(SlowWriter):
        def write_gap_wording(self, prompt, *, timeout=None):
            self.call_count += 1
            self.timeouts.append(timeout)
            if self.call_count == 2:
                raise RuntimeError("one blip")
            return ()

    writer = Flaky()
    report = visual_brief.run(cfg(), sheets, writer)
    assert writer.call_count == 4, "a single failure stopped the run asking"
    assert report.wording_given_up is False


def test_giving_up_is_reported_apart_from_the_failures_that_caused_it():
    """"It failed twice" and "it stopped trying" are different facts, and
    only the second explains the other leads never being attempted."""
    report = visual_brief.VisualBriefReport(
        prompts_written=10, wording_failures=2,
        wording_reason="RuntimeError: Read timed out", wording_given_up=True,
    )
    summary = "\n".join(report.summary_lines())
    assert "standard wording kept   2" in summary
    assert "Nothing needs redoing" in summary


def test_a_healthy_run_says_nothing_about_any_of_this():
    """A control: the explanation must not appear when nothing went wrong."""
    sheets = FakeSheets(index([lead("a")]), index([research("a")]))
    report = visual_brief.run(cfg(), sheets, FakeWriter())
    summary = "\n".join(report.summary_lines())
    assert "stopped answering" not in summary
    assert "standard wording kept" not in summary


# ------------------------------------------- a business already messaged
#
# Reported from a real run: "this is already been sent... we are only looking
# at those that has not been sent yet", beside a picker counting 104 on
# WhatsApp. A business contacted months ago with the website pitch was being
# counted as fresh work.


def sent_row(lead_id, when="2026-08-28T17:48:30Z", status="sent"):
    return {
        "Lead ID": lead_id,
        "WhatsApp Sent At": when,
        "WhatsApp Status": status,
        "WhatsApp Message": "The old website pitch.",
    }


def test_already_messaged_wins_over_the_channel():
    """Mixing them into "On WhatsApp" is what made 104 look like 104 new
    conversations."""
    lead_row = {"WhatsApp Reachable": "yes"}
    assert visual_brief.group_of(lead_row, {}, None) == "whatsapp"
    assert visual_brief.group_of(lead_row, {}, sent_row("a")) == "messaged"


def test_a_drafted_but_unsent_lead_is_not_already_messaged():
    """Drafted is not sent. A message sitting in the queue is still fresh
    work, and hiding it would strand it."""
    drafted = {"Lead ID": "a", "WhatsApp Message": "ready", "WhatsApp Status": "",
               "WhatsApp Sent At": ""}
    assert visual_brief.group_of({"WhatsApp Reachable": "yes"}, {}, drafted) == "whatsapp"


def test_unconfirmed_counts_as_messaged():
    """`unconfirmed` means the message left -- the send queue already treats
    it that way, and the two must not disagree about who was contacted."""
    row = {"Lead ID": "a", "WhatsApp Status": "unconfirmed", "WhatsApp Sent At": ""}
    assert visual_brief.already_messaged(row) is True


def test_the_messaged_group_can_still_be_run_for():
    """A second approach with something new to say is a real campaign. It is
    just never what "On WhatsApp" means."""
    sheets = FakeSheets(
        index([lead("fresh"), lead("old")]),
        index([research("fresh"), research("old")]),
    )
    sheets._tabs[schema.OUTREACH_LOG.title] = index([sent_row("old")])
    visual_brief.run(cfg(), sheets, FakeWriter(), groups=("messaged",))
    assert [r["Lead ID"] for r in rows_of(sheets)] == ["old"]


def test_asking_for_whatsapp_excludes_the_ones_already_messaged():
    sheets = FakeSheets(
        index([lead("fresh"), lead("old")]),
        index([research("fresh"), research("old")]),
    )
    sheets._tabs[schema.OUTREACH_LOG.title] = index([sent_row("old")])
    visual_brief.run(cfg(), sheets, FakeWriter(), groups=("whatsapp",))
    assert [r["Lead ID"] for r in rows_of(sheets)] == ["fresh"]


def test_an_unfiltered_run_pays_no_round_trip_for_a_tab_it_does_not_need():
    sheets = FakeSheets(index([lead("a")]), index([research("a")]))
    seen = []
    plain = sheets.read_rows
    sheets.read_rows = lambda tab: (seen.append(tab.title), plain(tab))[1]
    visual_brief.run(cfg(), sheets, FakeWriter())
    assert schema.OUTREACH_LOG.title not in seen


# -------------------------------------------- what the message is built from


def test_the_message_is_given_the_points_the_picture_makes():
    """The visual beats have always said to land on the first point the
    picture makes. Nothing ever gave the model those points, so every message
    was written blind to the three statements it was about."""
    from outreach.stages.outreach_draft import picture_points

    row = {"Image Prompt": (
        'Top headline, large: "3 quick wins for X".\n'
        '1: "The same question keeps coming up" -> "Answer it before they ask"\n'
        '2: "Everything lives in the feed" -> "A page that does not scroll away"\n'
        'Bottom ribbon in small text: "by Lyon".\n'
    )}
    points = picture_points(row)
    assert "The same question keeps coming up -- Answer it before they ask" in points
    # The headline and the ribbon are on the picture but are not points being
    # made; handing them over is how a message opens on a job title.
    assert "by Lyon" not in points
    assert "3 quick wins" not in points


def test_the_visual_track_never_offers_a_website(tmp_path, monkeypatch):
    """Its leads may carry a Preview URL from website work done months ago.
    That is not this message's subject, and raising it reads as a second
    attempt at the same sale."""
    from outreach.stages.outreach_draft import offers_a_page

    assert offers_a_page(cfg("visual"), "https://example.com/a/") is False
    assert offers_a_page(cfg("both"), "https://example.com/a/") is True
    assert offers_a_page(cfg("website"), "https://example.com/a/") is True
    assert offers_a_page(cfg("both"), "") is False


def test_a_visual_draft_passes_the_points_and_no_page(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTREACH_STATE_DIR", str(tmp_path))
    visuals = confirmed(tmp_path)
    visuals["a"][1]["Image Prompt"] = (
        '1: "Calls go unanswered" -> "Let them book online"\n'
    )
    briefs = index([{"Lead ID": "a", "Preview URL": "https://example.com/a/"}])
    sheets = FakeSheets(index([lead("a")]), index([research("a")]),
                        visuals=visuals, site_briefs=briefs)

    seen = {}

    class Spy(CopyWriter):
        def write_outreach_copy(self, prompt, **kw):
            seen["prompt"] = prompt
            seen.update(kw)
            return super().write_outreach_copy(prompt, **kw)

    outreach_draft.run(cfg("visual"), sheets, Spy(), None)
    assert "Calls go unanswered -- Let them book online" in seen["prompt"]
    assert seen["has_page"] is False
    assert "https://example.com/a/" not in seen["prompt"]
