"""The infographic track: what a card may claim, how it looks, and the gates.

The riskiest thing in this track is not a crash -- it is a graphic that makes
a checkable claim about a real business and gets it wrong, sent under Lyon's
name. Most of what is asserted here is therefore about what must NOT appear:
a card with no evidence, a business name on a generic graphic, a message
describing a picture nobody attached.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from outreach import gaps, schema, visual
from outreach.config import ConfigError, load_config
from outreach.providers.llm import (
    GapCard,
    build_gap_wording_prompt,
    outreach_copy_system,
    render_infographic_prompt,
)

PENANG = "config/campaigns/penang-my.yaml"


# ------------------------------------------------------------------ gaps.py


def a_lead(**extra):
    row = {
        "Lead ID": "L1",
        "Business Name": "Your Nail Crush",
        "Category": "Nail salon",
        "Website Status": "none",
        "Rating": "4.8",
        "Review Count": "42",
    }
    row.update(extra)
    return row


def a_research(**extra):
    row = {
        "Lead ID": "L1",
        "Tone": "warm",
        "Services": "manicure, gel",
        "Customer Pain Points": "Hard to get through on the phone",
        "Email Found": "",
        "Email Source": "not found",
    }
    row.update(extra)
    return row


def test_a_gap_with_no_evidence_is_never_offered():
    """The whole point of the module. A business whose research says nothing
    gets nothing -- not three cards invented to fill the format."""
    bare = {"Lead ID": "L1", "Business Name": "X", "Website Status": "real"}
    found = gaps.gaps_for(bare, None)
    assert found == ()
    assert not gaps.is_specific_enough(found)


def test_no_website_and_social_only_are_never_both_chosen():
    """Two readings of one column. Showing both says the same thing twice."""
    for status in ("none", "social_only"):
        found = gaps.gaps_for(a_lead(**{"Website Status": status}), a_research())
        ids = [g.id for g in found]
        assert not ({"no_website", "social_only"} <= set(ids))


def test_the_review_card_is_dropped_when_the_reviews_showed_nothing():
    """Without this the literal words "none evident" become a problem card."""
    for blank in ("", "none evident", "None", "nothing recorded", "n/a"):
        found = gaps.gaps_for(a_lead(), a_research(**{"Customer Pain Points": blank}))
        assert "review_pain" not in [g.id for g in found], blank


def test_the_review_card_can_be_switched_off_for_a_campaign():
    found = gaps.gaps_for(a_lead(), a_research(), use_review_pain=False)
    assert "review_pain" not in [g.id for g in found]


def test_no_email_needs_research_to_have_actually_looked():
    """A blank Email Source means nobody searched. Missing evidence is not
    evidence of absence, and "the phone is the only way in" is checkable."""
    never_looked = a_research(**{"Email Source": ""})
    assert "no_email" not in [g.id for g in gaps.gaps_for(a_lead(), never_looked)]

    looked = a_research(**{"Email Source": "not found"})
    # Pushed past the card cap by stronger gaps, so ask for enough room.
    ids = [g.id for g in gaps.gaps_for(a_lead(), looked, max_cards=9)]
    assert "no_email" in ids


def test_an_email_that_was_found_is_not_a_missing_email():
    found = a_research(**{"Email Found": "hi@x.com", "Email Source": "website"})
    assert "no_email" not in [g.id for g in gaps.gaps_for(a_lead(), found, max_cards=9)]


def test_review_volume_gives_one_card_not_two():
    strong = gaps.gaps_for(a_lead(**{"Rating": "4.9", "Review Count": "80"}),
                           a_research(), max_cards=9)
    assert "strong_but_invisible" in [g.id for g in strong]
    assert "few_reviews" not in [g.id for g in strong]

    thin = gaps.gaps_for(a_lead(**{"Rating": "4.9", "Review Count": "3"}),
                         a_research(), max_cards=9)
    assert "few_reviews" in [g.id for g in thin]
    assert "strong_but_invisible" not in [g.id for g in thin]


def test_a_business_with_no_reviews_gets_no_review_volume_card():
    none = gaps.gaps_for(a_lead(**{"Review Count": "0", "Rating": ""}),
                         a_research(), max_cards=9)
    ids = [g.id for g in none]
    assert "few_reviews" not in ids and "strong_but_invisible" not in ids


def test_the_card_cap_is_a_cap_and_not_a_target():
    """Three is the format. Two evidenced gaps produce two cards."""
    rich = gaps.gaps_for(a_lead(), a_research())
    assert len(rich) == 3

    two = gaps.gaps_for(
        a_lead(**{"Website Status": "real", "Review Count": "40", "Rating": "4.9"}),
        a_research(),
        max_cards=3,
    )
    assert len(two) == 2, [g.id for g in two]
    assert gaps.is_specific_enough(two)


def test_one_evidenced_gap_is_not_enough_to_name_the_business():
    only_one = gaps.gaps_for(
        {"Lead ID": "L1", "Business Name": "X", "Website Status": "none"},
        {"Email Source": "", "Customer Pain Points": ""},
    )
    assert len(only_one) == 1
    assert not gaps.is_specific_enough(only_one)


def test_every_card_carries_its_evidence_into_the_sheet():
    """Months later, a claim on a graphic is defensible only if the row still
    says what justified it."""
    found = gaps.gaps_for(a_lead(), a_research())
    summary = gaps.gaps_summary(found)
    for gap in found:
        assert gap.id in summary
        assert gap.evidence in summary
    assert "Hard to get through on the phone" in summary


def test_rewording_keeps_the_id_and_the_evidence():
    gap = gaps.gaps_for(a_lead(), a_research())[0]
    out = gaps.with_wording(gap, "Calls go unanswered", "Let them book online")
    assert out.id == gap.id
    assert out.evidence == gap.evidence
    assert out.problem == "Calls go unanswered"


def test_blank_rewording_falls_back_to_the_library_text():
    """A model that returns nothing useful must leave a true card, not an
    empty one."""
    gap = gaps.gaps_for(a_lead(), a_research())[0]
    out = gaps.with_wording(gap, "   ", "")
    assert out.problem == gap.problem and out.fix == gap.fix


def test_the_generic_cards_say_they_are_not_about_this_business():
    for gap in gaps.GENERIC_GAPS:
        assert "not a claim about this business" in gap.evidence


def test_no_library_card_blames_the_owner():
    """A graphic telling an owner what their customers complain about is an
    insult with the sender's name under it."""
    banned = ("complain", "bad review", "rude", "you failed", "poor", "your fault")
    for gap in gaps.GENERIC_GAPS + tuple(
        g for g in gaps.gaps_for(a_lead(), a_research(), max_cards=9)
    ):
        text = f"{gap.problem} {gap.fix}".casefold()
        for word in banned:
            assert word not in text, (gap.id, word)


# ---------------------------------------------------------------- visual.py


def test_the_industry_comes_from_the_category():
    assert visual.industry_for("Nail salon").key == "beauty"
    assert visual.industry_for("Tuition centre").key == "tuition"
    assert visual.industry_for("Air conditioning repair service").key == "trades"
    assert visual.industry_for("Dental clinic").key == "clinic"
    assert visual.industry_for("Nasi kandar restaurant").key == "food"


def test_an_unknown_category_still_gets_a_look():
    """Google's category strings are open-ended. A trade nobody listed must
    get a graphic, not a crash."""
    look = visual.look_for("Widget reticulation consultancy")
    assert look.style in visual.STYLES
    assert look.layout in visual.LAYOUTS
    assert look.character in visual.CHARACTERS


def test_the_campaign_category_is_the_fallback():
    assert visual.industry_for("", "hair salon").key == "beauty"


def test_the_pair_never_repeats_the_previous_one():
    """Ten salons briefed on ten days must not all get the same graphic."""
    industry = visual.industry_for("Nail salon")
    first = visual.next_pair(industry)
    second = visual.next_pair(industry, previous=first)
    assert second != first
    assert visual.next_pair(industry, previous=second) != second


def test_cutting_the_style_list_cuts_every_rotation():
    """His one ask: narrow the series down in one place."""
    industry = visual.industry_for("Nail salon")
    for _ in range(4):
        style, layout = visual.next_pair(
            industry, previous=None, styles=("paper-cut",), layouts=("rows", "ladder")
        )
        assert style == "paper-cut"
        assert layout in ("rows", "ladder")


def test_an_allow_list_that_empties_the_ring_still_answers():
    """Narrowing the series must never stop it producing a graphic."""
    industry = visual.industry_for("Nail salon")
    style, layout = visual.next_pair(
        industry, styles=("chalkboard",), layouts=("sticky-notes",)
    )
    assert (style, layout) == ("chalkboard", "sticky-notes")


def test_an_unknown_name_in_the_allow_list_is_ignored_not_crashed():
    industry = visual.industry_for("Nail salon")
    style, layout = visual.next_pair(industry, styles=("nope",), layouts=("nope",))
    assert style in visual.STYLES and layout in visual.LAYOUTS


def test_every_industry_ring_names_real_styles_and_layouts():
    """A typo in a ring would silently drop that pair from the rotation."""
    rings = [i for i in visual.INDUSTRIES] + [visual.DEFAULT_INDUSTRY]
    for industry in rings:
        assert industry.character in visual.CHARACTERS, industry.key
        assert industry.palette in visual.PALETTES, industry.key
        assert len(industry.ring) >= 2, f"{industry.key} cannot rotate"
        for style, layout in industry.ring:
            assert style in visual.STYLES, (industry.key, style)
            assert layout in visual.LAYOUTS, (industry.key, layout)


def test_every_layout_places_both_cards():
    """The style may change; the two-column problem-then-solution reading is
    what makes the graphic legible and must not."""
    for key, phrase in visual.LAYOUTS.items():
        assert "THE PROBLEM WE SEE" in phrase, key
        assert "HOW THE MARKET SOLVES IT" in phrase, key
        assert "coral" in phrase and "mint-green" in phrase, key


def test_no_layout_hardcodes_a_card_count():
    """A two-card graphic under a layout that said "three stops" is the prompt
    contradicting itself, and the model resolves that by inventing a card."""
    for key, phrase in visual.LAYOUTS.items():
        for number in ("three", "3", "two", "2"):
            assert number not in phrase.casefold().split(), (key, number)


# --------------------------------------------------- where the image lives


def test_a_hand_edited_path_cannot_escape_the_visuals_directory(tmp_path, monkeypatch):
    """The stored path comes off the Sheet, which a person can type into."""
    monkeypatch.setenv("OUTREACH_STATE_DIR", str(tmp_path))
    (tmp_path / "credentials").mkdir()
    (tmp_path / "credentials" / "token.json").write_text("secret")
    assert visual.resolve_image("../credentials/token.json") is None
    assert visual.resolve_image("visuals/../credentials/token.json") is None
    assert visual.resolve_image("/etc/passwd") is None


def test_a_real_image_resolves(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTREACH_STATE_DIR", str(tmp_path))
    visual.visuals_dir().mkdir(parents=True)
    (visual.visuals_dir() / "x.png").write_bytes(b"not really a png")
    assert visual.resolve_image("visuals/x.png") is not None


def test_an_empty_file_is_not_an_image(tmp_path, monkeypatch):
    """A zero exit code is not proof; bytes on disk are."""
    monkeypatch.setenv("OUTREACH_STATE_DIR", str(tmp_path))
    visual.visuals_dir().mkdir(parents=True)
    (visual.visuals_dir() / "empty.png").write_bytes(b"")
    assert visual.resolve_image("visuals/empty.png") is None


def test_the_stored_path_is_relative_so_the_sheet_survives_a_move():
    rel = visual.image_rel_path("Your Nail Crush", "ChIJabc", "image/png")
    assert rel.startswith("visuals/")
    assert not rel.startswith("/")
    assert rel.endswith(".png")


def test_an_unchecked_image_is_not_attachable(tmp_path, monkeypatch):
    """Nothing in this container can test that the text on the graphic
    rendered correctly, so the tick is the gate."""
    monkeypatch.setenv("OUTREACH_STATE_DIR", str(tmp_path))
    visual.visuals_dir().mkdir(parents=True)
    (visual.visuals_dir() / "x.png").write_bytes(b"bytes")
    row = {"Image File": "visuals/x.png", "Text Confirmed": ""}
    assert visual.confirmed_image(row) is None
    assert visual.confirmed_image({**row, "Text Confirmed": "yes"}) is not None


def test_a_blank_specific_cell_reads_as_not_specific():
    """A row written before the column existed must not let the message claim
    the graphic was made for this business."""
    assert visual.is_specific(None) is False
    assert visual.is_specific({}) is False
    assert visual.is_specific({"Specific": "no"}) is False
    assert visual.is_specific({"Specific": "yes"}) is True


# ------------------------------------------------------------- the prompt


THREE = [
    ("Calls go unanswered", "Let them book online"),
    ("Prices are not visible", "A clear price list"),
    ("First-timers feel unsure", "Say what to expect"),
]


def render(**over):
    args = dict(
        business_name="Your Nail Crush",
        cards=THREE,
        signature="by Lyon, Business Growth Consultant",
        **visual.describe(visual.look_for("Nail salon")),
    )
    args.update(over)
    return render_infographic_prompt(**args)


def test_the_prompt_is_deterministic():
    assert render() == render()


def test_the_card_count_drives_the_headline():
    assert '"3 quick wins for Your Nail Crush"' in render()
    assert '"2 quick wins for Your Nail Crush"' in render(cards=THREE[:2])
    assert '"1 quick win for Your Nail Crush"' in render(cards=THREE[:1])


def test_a_generic_graphic_carries_no_business_name_anywhere():
    """The owner can see from the picture that it was not made for them, so
    the claim fails exactly where it is being read."""
    out = render(specific=False)
    assert "Your Nail Crush" not in out
    assert "for your business" in out


def test_every_card_reaches_the_prompt_verbatim():
    out = render()
    for index, (problem, fix) in enumerate(THREE, start=1):
        assert f'{index}: "{problem}" -> "{fix}"' in out


def test_the_cards_keep_their_colours_whatever_the_palette_says():
    """A style phrase naming its own colours would otherwise recolour the
    cards and break the series' one constant."""
    out = render()
    assert "Keep the problem cards coral and the solution cards mint green" in out


def test_the_signature_ribbon_is_rendered_verbatim():
    out = render(signature="by Someone Else")
    assert 'Bottom ribbon in small text: "by Someone Else".' in out


def test_no_signature_means_no_ribbon_rather_than_an_empty_one():
    assert "Bottom ribbon" not in render(signature="")


def test_the_prompt_forbids_a_logo_and_a_fabricated_rating():
    """A model asked for a business graphic will draw a logo for it, and an
    invented logo on a pitch to the business that owns the real one is the
    same error as an invented price."""
    out = render()
    assert "No logos of any kind" in out
    assert "no star ratings" in out
    assert "Text exactly as written above" in out


def test_a_prompt_with_no_cards_is_refused():
    with pytest.raises(ValueError, match="at least one card"):
        render(cards=[])


def test_the_wording_prompt_never_shows_the_model_the_complaint():
    """A model handed the complaint puts the complaint on the card."""
    out = build_gap_wording_prompt(
        name="Your Nail Crush",
        category="Nail salon",
        location="Penang",
        tone="warm",
        services="manicure",
        cards=[("review_pain", "The same question keeps coming up",
                "Answer it before they have to ask")],
    )
    assert "Hard to get through on the phone" not in out
    assert "review_pain" in out


# ------------------------------------------------- the message's own prompt


def test_the_website_prompt_is_untouched_by_the_new_track():
    """Adding a track must not change a word of the prompt the other tracks
    already produce -- the same promise the conversion angle made."""
    plain = outreach_copy_system()
    assert outreach_copy_system(pitch="site") == plain
    assert "1. HOOK" in plain
    # None of the visual-only text may leak into it.
    assert "attached picture" not in plain
    assert "NOT specific to this business" not in plain


def test_the_visual_prompt_puts_the_picture_at_the_centre():
    out = outreach_copy_system(pitch="visual")
    assert "attached picture" in out
    assert "Website: [the link]" not in out


def test_both_tracks_produce_one_message_carrying_the_page_link():
    out = outreach_copy_system(pitch="visual", has_page=True)
    assert "Website: [the link]" in out
    assert "the picture is the hook" in out.casefold()


def test_a_generic_graphic_forbids_claiming_it_was_made_for_them():
    out = outreach_copy_system(pitch="visual", specific=False)
    assert "NOT specific to this business" in out
    assert "never claim the picture was made for them" in out


def test_a_specific_graphic_does_not_carry_the_generic_warning():
    out = outreach_copy_system(pitch="visual", specific=True)
    assert "NOT specific to this business" not in out


# ------------------------------------------------------------------ config


def test_the_live_campaign_still_loads_on_the_original_track():
    cfg = load_config(PENANG, env_file=None)
    assert cfg.outreach.track == "website"


def campaign_with(tmp_path, track, *, extra=""):
    """The live campaign with its own `track:` line replaced.

    Replaced, never prepended: a second `outreach.track` key is last-wins
    YAML, so the config loaded would not be the config the test thought it
    wrote. The `visual:` block is likewise edited rather than appended, since
    the file now ships one.
    """
    src = open(PENANG).read()
    assert "\n  track: website\n" in src, "the campaign file no longer names a track"
    src = src.replace("\n  track: website\n", f"\n  track: {track}\n", 1)
    if extra:
        assert "\nvisual:\n" in src, "the campaign file no longer has a visual block"
        src = src.replace("\nvisual:\n", f"\nvisual:\n{extra}", 1)
    path = tmp_path / "c.yaml"
    path.write_text(src)
    return path


def test_an_unknown_track_fails_at_load_naming_the_fix(tmp_path):
    with pytest.raises(ConfigError, match="outreach.track must be one of"):
        load_config(campaign_with(tmp_path, "graphics"), env_file=None)


def test_a_graphics_campaign_must_say_who_the_graphics_are_from(tmp_path):
    with pytest.raises(ConfigError, match="visual.signature"):
        load_config(campaign_with(tmp_path, "visual"), env_file=None)


def test_a_graphics_campaign_loads_once_it_has_a_signature(tmp_path):
    path = campaign_with(
        tmp_path, "both",
        extra='  signature: "by Lyon, Business Growth Consultant"\n',
    )
    cfg = load_config(path, env_file=None)
    assert cfg.outreach.track == "both"
    assert cfg.visual.signature == "by Lyon, Business Growth Consultant"


def test_a_typo_in_the_style_list_fails_at_load(tmp_path):
    """Dropped silently, it looks exactly like the rotation ignoring the
    setting -- the drift discovery.social_domains already fails loudly over."""
    path = campaign_with(
        tmp_path, "visual",
        extra='  signature: "by Lyon"\n  styles:\n    - paper-cut\n    - watercolor\n',
    )
    with pytest.raises(ConfigError, match="visual.styles lists 'watercolor'"):
        load_config(path, env_file=None)


def test_a_guessed_image_tool_url_is_refused(tmp_path):
    path = campaign_with(
        tmp_path, "visual",
        extra='  signature: "by Lyon"\n  image_tool_url: "labs.google"\n',
    )
    with pytest.raises(ConfigError, match="image_tool_url must be a full"):
        load_config(path, env_file=None)
