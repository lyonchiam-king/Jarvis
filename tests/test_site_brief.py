"""Stage 4: prompt generation, and the three gates in front of spending credits."""

from dataclasses import replace

import pytest

from outreach import schema
from outreach.config import load_config
from outreach.providers.llm import SiteSpec, render_lovable_prompt
from outreach.providers.lovable import (
    LovableAdapter,
    LovableSkipped,
    LovableUnavailable,
)
from outreach.stages import site_brief

PENANG = "config/campaigns/penang-my.yaml"


def config(**lovable_overrides):
    """Campaign config with the Lovable settings pinned, not inherited.

    ``auto_approve`` lives in a campaign file the user edits, so any test that
    depends on it states its own value here.
    """
    cfg = load_config(PENANG, env_file=None)
    return replace(
        cfg,
        lovable=replace(cfg.lovable, **{"auto_approve": False, **lovable_overrides}),
    )


SPEC = SiteSpec(
    site_goal="Let people book a cut without calling.",
    pages=("Home", "Services", "Contact"),
    hero_headline="Georgetown's neighbourhood salon since 1998",
    hero_subcopy="Walk in, or book a slot in ten seconds.",
    key_sections=("Booking widget", "Services and prices", "Find us"),
    primary_cta="Book a slot",
    visual_direction="Warm neutrals, generous whitespace, a humanist serif.",
)


# ------------------------------------------------------- prompt rendering


def test_rendered_prompt_contains_every_part_of_the_spec():
    prompt = render_lovable_prompt(
        business_name="Sun Salon",
        category="hair salon",
        location="Georgetown, Penang",
        spec=SPEC,
    )

    assert "Sun Salon" in prompt
    assert "Georgetown, Penang" in prompt
    assert SPEC.hero_headline in prompt
    assert "1. Booking widget" in prompt
    assert "3. Find us" in prompt
    assert SPEC.visual_direction in prompt


def test_rendered_prompt_forbids_invented_details():
    prompt = render_lovable_prompt(
        business_name="Sun Salon", category="hair salon", location="", spec=SPEC
    )
    assert "Do not" in prompt and "invent" in prompt
    assert "[TO CONFIRM]" in prompt


def test_rendering_is_deterministic():
    first = render_lovable_prompt(
        business_name="Sun Salon", category="salon", location="Penang", spec=SPEC
    )
    second = render_lovable_prompt(
        business_name="Sun Salon", category="salon", location="Penang", spec=SPEC
    )
    assert first == second


# ------------------------------------------------------------ the gates


def test_disabled_adapter_never_builds():
    adapter = LovableAdapter(enabled=False)
    assert adapter.should_build("Approved") is False

    with pytest.raises(LovableSkipped, match="enabled is false"):
        adapter.create_project(name="Sun Salon", prompt="build a site")


def test_enabled_adapter_still_needs_your_manual_approval():
    adapter = LovableAdapter(enabled=True)
    assert adapter.should_build("Pending") is False
    assert adapter.should_build("") is False
    assert adapter.should_build("Skip") is False
    assert adapter.should_build("Approved") is True
    assert adapter.should_build("  approved  ") is True


def test_confirmation_is_required_before_spending():
    declined = LovableAdapter(enabled=True, confirm=lambda _msg: False)
    with pytest.raises(LovableSkipped, match="declined"):
        declined.create_project(name="Sun Salon", prompt="p")


def test_non_interactive_run_refuses_rather_than_assuming_yes():
    adapter = LovableAdapter(enabled=True, require_confirmation=True, confirm=None)
    with pytest.raises(LovableSkipped, match="non-interactive"):
        adapter.create_project(name="Sun Salon", prompt="p")


def test_missing_transport_explains_how_to_connect():
    adapter = LovableAdapter(enabled=True, require_confirmation=False)
    with pytest.raises(LovableUnavailable, match="claude.ai"):
        adapter.create_project(name="Sun Salon", prompt="p")


def test_all_gates_passed_creates_a_project():
    class Transport:
        def create_project(self, *, name, prompt):
            return {"id": "proj_1", "preview_url": "https://preview.test/1"}

    adapter = LovableAdapter(
        enabled=True, confirm=lambda _msg: True, transport=Transport()
    )
    project = adapter.create_project(name="Sun Salon", prompt="p")

    assert project.project_id == "proj_1"
    assert project.preview_url == "https://preview.test/1"
    assert adapter.projects_created == 1


# ------------------------------------------------------------------- run


def lead(lead_id, stage="Researched"):
    return {
        "Lead ID": lead_id,
        "Stage": stage,
        "Business Name": f"Salon {lead_id}",
        "Category": "Hair salon",
    }


def research_row(lead_id):
    return {
        "Lead ID": lead_id,
        "Tone": "warm",
        "Services": "cut, colour",
        "Price Tier": "mid-range",
        "Customer Praise": "Friendly",
        "Customer Pain Points": "Booking is hard",
        "Suggested Website Angle": "Online booking",
    }


def index(rows):
    return {r["Lead ID"]: (i + 2, r) for i, r in enumerate(rows)}


class FakeSheets:
    def __init__(self, leads, research, briefs=None):
        self._tabs = {
            schema.LEADS.title: leads,
            schema.RESEARCH.title: research,
            schema.SITE_BRIEFS.title: briefs or {},
        }
        self.upserts = []
        self.column_writes = []
        self.appended = []
        self.spreadsheet_id = "fake"

    def read_rows(self, tab):
        return self._tabs.get(tab.title, {})

    def upsert(self, tab, rows, *, on_conflict=None):
        self.upserts.append((tab, list(rows)))
        from outreach.sheets import UpsertResult

        return UpsertResult(inserted=len(rows))

    def update_column(self, tab, column, values_by_key):
        self.column_writes.append((tab, column, dict(values_by_key)))
        return len(values_by_key)

    def append(self, tab, rows):
        self.appended.append((tab, list(rows)))
        return len(rows)


class FakeWriter:
    def __init__(self):
        self.call_count = 0

    def write_site_spec(self, prompt):
        self.call_count += 1
        return SPEC


def test_writes_a_prompt_and_seeds_approval_as_pending():
    sheets = FakeSheets(index([lead("a")]), index([research_row("a")]))
    report = site_brief.run(config(), sheets, FakeWriter(), LovableAdapter())

    assert report.prompts_written == 1
    row = sheets.upserts[0][1][0]
    assert "Sun Salon" not in row["Lovable Prompt"]  # uses the lead's own name
    assert row["Approval Status"] == "Pending"
    assert row["Lovable Project ID"] == ""
    assert sheets.column_writes == [(schema.LEADS, "Stage", {"a": "Site Briefed"})]


def test_prompt_generation_never_spends_credits():
    class ExplodingAdapter(LovableAdapter):
        def create_project(self, *, name, prompt):
            raise AssertionError("site-brief must never build")

    sheets = FakeSheets(index([lead("a")]), index([research_row("a")]))
    report = site_brief.run(
        config(), sheets, FakeWriter(), ExplodingAdapter(enabled=True)
    )
    assert report.prompts_written == 1


def test_unresearched_lead_is_not_ready():
    sheets = FakeSheets(index([lead("a", stage="Discovered")]), {})
    report = site_brief.run(config(), sheets, FakeWriter(), LovableAdapter())

    assert report.selected == 0
    assert report.not_ready == 1


def test_rerun_skips_leads_that_already_have_a_prompt():
    sheets = FakeSheets(
        index([lead("a")]),
        index([research_row("a")]),
        briefs=index([{"Lead ID": "a"}]),
    )
    writer = FakeWriter()
    report = site_brief.run(config(), sheets, writer, LovableAdapter())

    assert report.selected == 0
    assert writer.call_count == 0


def test_missing_research_row_is_a_per_lead_error():
    sheets = FakeSheets(index([lead("a"), lead("b")]), index([research_row("b")]))
    report = site_brief.run(config(), sheets, FakeWriter(), LovableAdapter())

    assert report.prompts_written == 1
    assert report.errors == 1


# ------------------------------------------------------------ site-build


def brief_row(lead_id, approval="Pending", project_id=""):
    return {
        "Lead ID": lead_id,
        "Business Name": f"Salon {lead_id}",
        "Lovable Prompt": "build a site",
        "Approval Status": approval,
        "Lovable Project ID": project_id,
    }


def test_build_does_nothing_while_lovable_is_disabled():
    sheets = FakeSheets({}, {}, briefs=index([brief_row("a", approval="Approved")]))
    report = site_brief.build_approved(config(), sheets, LovableAdapter(enabled=False))

    assert report.built == 0
    assert sheets.column_writes == []


def test_build_only_touches_rows_you_approved():
    class Transport:
        def __init__(self):
            self.calls = []

        def create_project(self, *, name, prompt):
            self.calls.append(name)
            return {"id": f"proj_{name}", "preview_url": "https://p/x"}

    transport = Transport()
    sheets = FakeSheets(
        {},
        {},
        briefs=index(
            [
                brief_row("yes", approval="Approved"),
                brief_row("no", approval="Pending"),
                brief_row("skip", approval="Skip"),
            ]
        ),
    )
    adapter = LovableAdapter(
        enabled=True, confirm=lambda _m: True, transport=transport
    )
    report = site_brief.build_approved(config(), sheets, adapter)

    assert report.built == 1
    assert transport.calls == ["Salon yes"]
    ids = dict(sheets.column_writes[0][2])
    assert ids == {"yes": "proj_Salon yes"}


def test_build_skips_rows_that_already_have_a_project():
    sheets = FakeSheets(
        {}, {}, briefs=index([brief_row("a", approval="Approved", project_id="proj_1")])
    )
    adapter = LovableAdapter(enabled=True, confirm=lambda _m: True)
    report = site_brief.build_approved(config(), sheets, adapter)

    assert report.approved == 1
    assert report.already_built == 1
    assert report.built == 0


def test_build_advances_the_stage_to_site_built():
    class Transport:
        def create_project(self, *, name, prompt):
            return {"id": "proj_1", "preview_url": "https://p/1"}

    sheets = FakeSheets({}, {}, briefs=index([brief_row("a", approval="Approved")]))
    adapter = LovableAdapter(
        enabled=True, confirm=lambda _m: True, transport=Transport()
    )
    site_brief.build_approved(config(), sheets, adapter)

    stage_write = [w for w in sheets.column_writes if w[0] is schema.LEADS]
    assert stage_write == [(schema.LEADS, "Stage", {"a": "Site Built"})]


# ------------------------------------------- social handle reaches the prompt


def test_social_profile_is_included_in_the_site_spec_prompt():
    from outreach.providers.llm import build_site_spec_prompt

    prompt = build_site_spec_prompt(
        name="Sun Salon", category="hair salon", location="Penang",
        tone="warm", services="cut", price_tier="mid-range",
        praise="Friendly", pain_points="Booking", angle="Online booking",
        social_handle="@sunsalon", social_bio="Family salon since 1998",
        social_post="New colour range in stock",
    )
    assert "@sunsalon" in prompt
    assert "Family salon since 1998" in prompt
    assert "New colour range in stock" in prompt
    assert "Link every one of these prominently" in prompt


def test_prompt_forbids_inventing_a_social_profile_when_none_was_found():
    from outreach.providers.llm import build_site_spec_prompt

    prompt = build_site_spec_prompt(
        name="Sun Salon", category="hair salon", location="Penang",
        tone="warm", services="cut", price_tier="mid-range",
        praise="", pain_points="", angle="",
    )
    assert "do not invent or link one" in prompt


def test_stage_4_passes_the_social_columns_through():
    sheets = FakeSheets(
        index([lead("a")]),
        index([{**research_row("a"), "Social Handle": "@sunsalon",
                "Social Bio": "Cutting hair in GT since 1998"}]),
    )

    class CapturingWriter(FakeWriter):
        def __init__(self):
            super().__init__()
            self.prompt = ""

        def write_site_spec(self, prompt):
            self.prompt = prompt
            return super().write_site_spec(prompt)

    writer = CapturingWriter()
    site_brief.run(config(), sheets, writer, LovableAdapter())

    assert "@sunsalon" in writer.prompt
    assert "Cutting hair in GT since 1998" in writer.prompt


def test_prompt_routes_enquiries_to_a_sheet_the_owner_already_understands():
    """These owners have never had a website -- no admin panel, no login."""
    prompt = render_lovable_prompt(
        business_name="Sun Salon", category="salon", location="Penang", spec=SPEC
    )
    assert "Google Sheets connector" in prompt
    assert "admin panel" in prompt
    assert "WhatsApp" in prompt


def test_auto_approve_seeds_briefs_as_approved():
    sheets = FakeSheets(index([lead("a")]), index([research_row("a")]))
    site_brief.run(config(auto_approve=True), sheets, FakeWriter(), LovableAdapter())

    assert sheets.upserts[0][1][0]["Approval Status"] == "Approved"


def test_auto_approve_off_still_seeds_pending():
    sheets = FakeSheets(index([lead("a")]), index([research_row("a")]))
    site_brief.run(config(auto_approve=False), sheets, FakeWriter(), LovableAdapter())

    assert sheets.upserts[0][1][0]["Approval Status"] == "Pending"


def test_auto_approve_still_does_not_build_anything_here():
    """Approved != built. Stage 4 writes prompts; only `site-build` spends."""

    class ExplodingAdapter(LovableAdapter):
        def create_project(self, *, name, prompt):
            raise AssertionError("site-brief must never build, even auto-approved")

    sheets = FakeSheets(index([lead("a")]), index([research_row("a")]))
    report = site_brief.run(
        config(auto_approve=True), sheets, FakeWriter(), ExplodingAdapter(enabled=True)
    )

    assert report.prompts_written == 1
    assert report.projects_created == 0


# ------------------------------------- the brief reads like a designer wrote it


FULL_SPEC = SiteSpec(
    site_goal="Turn a phone browser into a booked appointment.",
    pages=("Home",),
    hero_headline="Cut, colour and a proper wash on Lebuh Chulia",
    hero_subcopy="Walk in if we're quiet, or pick a time below.",
    key_sections=("Book a time -- three taps, above the fold on mobile.",),
    primary_cta="Book a time",
    visual_direction="Quiet, warm, unmistakably a Chulia shophouse.",
    visitor="Someone at 8pm whose usual barber is shut, on a phone.",
    proof_points=("4.7 from 213 Google reviews",),
    palette="Off-white #FAF7F2, near-black #1C1A17, one teal #12604F accent.",
    typography="Fraunces headings, Inter body, headings 2.2x body.",
    avoid=("No stock photos of models", "No 'unisex salon' phrasing"),
)


def rendered(**kwargs):
    return render_lovable_prompt(
        business_name="Chulia Hair House",
        category="hair salon",
        location="Georgetown",
        spec=FULL_SPEC,
        **kwargs,
    )


def test_the_brief_opens_with_the_visitor_not_the_business():
    prompt = rendered()
    assert "WHO IT IS FOR" in prompt
    assert FULL_SPEC.visitor in prompt
    assert prompt.index("WHO IT IS FOR") < prompt.index("THE ONE JOB")


def test_every_section_is_tied_to_the_one_action():
    """The discipline that separates a booking site from a brochure."""
    prompt = rendered()
    assert 'does not move someone toward "Book a time"' in prompt


def test_design_decisions_are_carried_through_not_summarised_away():
    prompt = rendered()
    assert "#FAF7F2" in prompt and "#12604F" in prompt
    assert "Fraunces" in prompt and "Inter" in prompt


def test_the_do_not_list_survives_into_the_brief():
    prompt = rendered()
    assert "DO NOT" in prompt
    assert "No stock photos of models" in prompt


def test_proof_points_are_fenced_against_embellishment():
    prompt = rendered()
    assert "4.7 from 213 Google reviews" in prompt
    assert "invent nothing further" in prompt


def test_real_contact_details_are_passed_through_verbatim():
    """A hallucinated digit in a phone number costs a real customer."""
    prompt = rendered(
        phone="04-262 1188",
        address="58 Lebuh Chulia, 10200 George Town",
        social_handle="@chuliahairhouse",
    )
    assert "04-262 1188" in prompt
    assert "58 Lebuh Chulia, 10200 George Town" in prompt
    assert "@chuliahairhouse" in prompt
    assert "do not paraphrase" in prompt


def test_sections_absent_from_the_spec_leave_no_empty_headings():
    """A thin spec must not render a brief full of dangling labels."""
    thin = SiteSpec(
        site_goal="Get people to call.",
        pages=(),
        hero_headline="A barber on Chulia",
        hero_subcopy="Open six days.",
        key_sections=("Call us -- the number, big.",),
        primary_cta="Call now",
        visual_direction="",
    )
    prompt = render_lovable_prompt(
        business_name="X", category="barber", location="Penang", spec=thin
    )

    assert "DO NOT" not in prompt
    assert "PROOF" not in prompt
    assert "REAL DETAILS" not in prompt
    assert "- Home (single page)" in prompt, "an empty page list still needs one page"


def test_the_builder_is_told_to_mark_gaps_rather_than_guess():
    assert "[TO CONFIRM]" in rendered()
    assert "rather than guessing" in rendered()


def test_stage_4_hands_the_lead_its_own_phone_and_address():
    sheets = FakeSheets(
        index([{**lead("a"), "Phone": "04-262 1188", "Address": "58 Lebuh Chulia"}]),
        index([{**research_row("a"), "Social Handle": "@chuliahair"}]),
    )
    site_brief.run(config(), sheets, FakeWriter(), LovableAdapter())

    prompt = sheets.upserts[0][1][0]["Lovable Prompt"]
    assert "04-262 1188" in prompt
    assert "58 Lebuh Chulia" in prompt
    assert "@chuliahair" in prompt


def test_the_spec_model_is_asked_for_real_design_decisions():
    """Guards the system prompt against drifting back to 'modern and clean'."""
    from outreach.providers.llm import SITE_SPEC_SCHEMA, SITE_SPEC_SYSTEM

    for field in ("visitor", "palette", "typography", "avoid", "proof_points"):
        assert field in SITE_SPEC_SCHEMA["required"]
    assert "hex" in SITE_SPEC_SYSTEM
    assert "Never invent a fact" in SITE_SPEC_SYSTEM


# ------------------------------- every social profile, with platform and link


def test_the_brief_lists_every_profile_with_its_platform_and_url():
    prompt = render_lovable_prompt(
        business_name="Espario Hair Salon",
        category="hair salon",
        location="George Town",
        spec=FULL_SPEC,
        social_profiles=(
            "Facebook @Espariohairsalon "
            "https://www.facebook.com/Espariohairsalon-107771601074381\n"
            "Instagram @espariohair https://instagram.com/espariohair"
        ),
    )

    assert "SOCIAL PROFILES" in prompt
    assert "Facebook @Espariohairsalon" in prompt
    assert "https://www.facebook.com/Espariohairsalon-107771601074381" in prompt
    assert "Instagram @espariohair" in prompt
    assert "https://instagram.com/espariohair" in prompt


def test_the_builder_is_forbidden_from_inventing_a_profile():
    """A guessed handle sends real customers to a stranger's account."""
    prompt = render_lovable_prompt(
        business_name="X", category="salon", location="Penang", spec=FULL_SPEC,
        social_profiles="Instagram @realsalon https://instagram.com/realsalon",
    )
    assert "do not invent a handle for a platform that is not listed" in prompt
    assert "do not guess a" in prompt


def test_no_socials_means_no_social_section():
    prompt = render_lovable_prompt(
        business_name="X", category="salon", location="Penang", spec=FULL_SPEC
    )
    assert "SOCIAL PROFILES" not in prompt


def test_stage_4_passes_the_whole_profile_list_through():
    sheets = FakeSheets(
        index([lead("a")]),
        index([
            {
                **research_row("a"),
                "Social Handle": "@espariohair",
                "Social Profiles": (
                    "Facebook @Espariohairsalon https://facebook.com/Espariohairsalon-107771601074381\n"
                    "Instagram @espariohair https://instagram.com/espariohair"
                ),
            }
        ]),
    )
    site_brief.run(config(), sheets, FakeWriter(), LovableAdapter())

    prompt = sheets.upserts[0][1][0]["Lovable Prompt"]
    assert "Facebook" in prompt and "Instagram" in prompt
    assert "https://instagram.com/espariohair" in prompt


def test_the_spec_model_is_given_the_platforms_too():
    from outreach.providers.llm import build_site_spec_prompt

    prompt = build_site_spec_prompt(
        name="X", category="salon", location="Penang",
        tone="warm", services="cut", price_tier="mid-range",
        praise="", pain_points="", angle="",
        social_profiles="TikTok @salonpg https://tiktok.com/@salonpg",
    )
    assert "TikTok @salonpg" in prompt
    assert "https://tiktok.com/@salonpg" in prompt
