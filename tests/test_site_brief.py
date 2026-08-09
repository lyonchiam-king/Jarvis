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
    depends on it states its own value here. ``research.social_lookup`` is
    pinned too: the Leads-tab fallback in stage 4 is gated on that flag, so a
    campaign edit to it must not silently change what these tests assert.
    """
    cfg = load_config(PENANG, env_file=None)
    return replace(
        cfg,
        lovable=replace(cfg.lovable, **{"auto_approve": False, **lovable_overrides}),
        research=replace(cfg.research, social_lookup=True),
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

    def update_column(self, tab, column, values_by_key, *, user_edit=False):
        from outreach.sheets import guard_manual_column

        guard_manual_column(tab, column, user_edit=user_edit)
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
    )
    assert "04-262 1188" in prompt
    assert "58 Lebuh Chulia, 10200 George Town" in prompt
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
        index(
            [
                {
                    **research_row("a"),
                    "Social Handle": "@chuliahair",
                    "Social Profiles": (
                        "Instagram @chuliahair https://instagram.com/chuliahair"
                    ),
                }
            ]
        ),
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


def test_a_bare_handle_with_no_url_never_reaches_the_builder():
    """The bug: a handle with no known URL used to render 'SOCIAL PROFILES /
    - @sun -- link it prominently' with nothing to link, which is an
    invitation for the builder to guess a URL -- and a guessed URL can send
    a real customer to a stranger's profile. Lyon's rule is that this can
    never happen, so a bare handle renders nothing rather than unlinked
    text with an instruction to link it."""
    prompt = render_lovable_prompt(
        business_name="X", category="salon", location="Penang", spec=FULL_SPEC,
        social_handle="@sun",
    )
    assert "SOCIAL PROFILES" not in prompt
    assert "@sun" not in prompt


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


# ------------------------------------------- motion, interaction, and sources


MOTION_SPEC = replace(
    FULL_SPEC,
    signature_moment=(
        "Drag the before/after slider on a real customer's cut -- it follows "
        "the thumb, and snaps to whichever side you release nearest."
    ),
    motion_language=(
        "Sections rise 16px and fade in over 420ms on a cubic-bezier(.16,1,.3,1) "
        "ease-out, siblings staggered 60ms. Nothing bounces; this is a salon, "
        "not a toy."
    ),
    micro_interactions=(
        "Tap the WhatsApp button -> it depresses 2px and the icon nudges right",
        "Focus a form field -> the label rises and the underline draws in 180ms",
        "Gallery image enters -> settles from 1.02 scale like a laid photograph",
    ),
)


def motion_prompt(**kwargs):
    return render_lovable_prompt(
        business_name="Chulia Hair House",
        category="hair salon",
        location="Georgetown",
        spec=MOTION_SPEC,
        **kwargs,
    )


def test_the_signature_moment_reaches_the_builder():
    prompt = motion_prompt()
    assert "MOTION AND INTERACTION" in prompt
    assert "before/after slider" in prompt


def test_motion_is_specified_in_numbers_not_adjectives():
    prompt = motion_prompt()
    assert "420ms" in prompt
    assert "cubic-bezier" in prompt
    assert "staggered 60ms" in prompt


def test_micro_interactions_are_listed_as_trigger_and_response():
    prompt = motion_prompt()
    assert "Tap the WhatsApp button -> it depresses 2px" in prompt


def test_motion_is_named_in_the_stack_lovable_actually_has():
    """Framer Motion ships with Lovable; Three.js would be fought or faked."""
    prompt = motion_prompt()
    assert "Framer Motion is already available" in prompt
    assert "transform and opacity only" in prompt


def test_the_builder_is_given_the_apis_not_adjectives():
    """A builder told 'smooth animations' produces a default fade; a builder
    given whileInView, AnimatePresence and layoutId produces an effect."""
    prompt = motion_prompt()
    assert "whileInView" in prompt
    assert "AnimatePresence" in prompt
    assert "layoutId" in prompt
    assert "useReducedMotion" in prompt


def test_the_techniques_that_break_a_phone_are_ruled_out():
    """Award-gallery moves that cost a booking on 4G."""
    prompt = motion_prompt()
    for banned in ("scroll-jacking", "cursor", "WebGL", "Three.js"):
        assert banned in prompt, f"{banned} is not ruled out"
    assert "prefers-reduced-motion" in prompt


def test_content_must_survive_the_animation_never_running():
    prompt = motion_prompt()
    assert "readable" in prompt and "even if the animation never runs" in prompt


def test_the_performance_budget_outranks_the_visual_ideas():
    """Rewritten (see task brief): the old wording -- 'it outranks every
    visual idea above' -- read literally as a veto over the whole motion
    section above it, which is exactly backwards. LCP is set by the hero, so
    the budget is scoped to the hero and the first tap; below the fold is
    then explicitly free to move within the transform/opacity rule.

    The permissive half is not the whole rescope, though: the hero itself
    still may not animate, and when an effect and the budget collide the
    effect loses -- that tiebreak is what the rescope would otherwise be
    silently missing, so it is asserted here too, against the rendered
    prompt (not just the spec-writing system prompt)."""
    prompt = motion_prompt()
    assert "2.5s" in prompt
    assert "governs the hero and the first tap" in prompt
    assert "Below the fold, animate" in prompt
    assert "freely inside the transform/opacity rule" in prompt
    # The restrictive half: no entrance animation on the hero, and the
    # tiebreak restored from the pre-rewrite wording, adapted to the scope.
    assert "no entrance animation and no load gate" in prompt
    assert (
        "If an effect cannot fit inside the hero-and-first-tap budget, "
        "drop the effect -- not the budget." in prompt
    )


def test_a_thin_spec_still_gets_the_motion_floor():
    """The bug this guards: a spec with no signature_moment, no
    motion_language and micro_interactions=() used to render no motion
    section at all, so a thin spec shipped a static page. The floor is fixed
    text, not derived from spec values, so it survives even here."""
    prompt = render_lovable_prompt(
        business_name="X", category="salon", location="Penang", spec=FULL_SPEC
    )
    assert "MOTION AND INTERACTION" in prompt
    assert "BUILD ALL OF THESE" in prompt
    assert "Signature moment:" not in prompt, "nothing to report, so it reports nothing"


def test_motion_floor_never_demands_sections_the_spec_does_not_have():
    """The bug: floor items 4 (showcase modal) and 5 (mobile bar) rendered
    unconditionally, telling the builder to BUILD a showcase card and a
    mobile bar that appear nowhere else in the prompt when the spec has
    neither -- contradicting 'if a section does not move someone toward the
    CTA, it does not belong on the page'. FULL_SPEC has no showcase_items and
    no mobile_bar, so neither may appear in the floor, and the numbering
    must not skip 4 and 5."""
    prompt = render_lovable_prompt(
        business_name="X", category="salon", location="Penang", spec=FULL_SPEC
    )
    floor = prompt[prompt.index("BUILD ALL OF THESE"):prompt.index("The hero itself")]

    assert "showcase card" not in floor
    assert "mobile bar" not in floor
    assert "1. The signature moment" in floor
    assert "2. Sections below the fold" in floor
    assert "3. The primary CTA answers the tap" in floor
    assert "4." not in floor
    assert "5." not in floor


def test_motion_floor_includes_showcase_and_mobile_bar_when_the_spec_has_them():
    """Items 1-3 are the floor; 4 and 5 are numbered on without a gap when
    the spec actually has a showcase and a mobile bar to build."""
    prompt = rich()
    floor = prompt[prompt.index("BUILD ALL OF THESE"):prompt.index("The hero itself")]

    assert "4. A showcase card expands into its modal" in floor
    assert "5. The mobile bar enters with its own animation" in floor


# ------------------------------------------------ sources the builder studies


def test_the_google_business_profile_is_given_as_a_source():
    prompt = motion_prompt(maps_url="https://maps.google.com/?cid=123")

    assert "STUDY THESE FIRST" in prompt
    assert "https://maps.google.com/?cid=123" in prompt
    assert "Read the recent reviews" in prompt


def test_socials_are_sources_as_well_as_links_to_place():
    prompt = motion_prompt(
        maps_url="https://maps.google.com/?cid=123",
        social_profiles="Instagram @chuliahair https://instagram.com/chuliahair",
    )
    sources = prompt[prompt.index("STUDY THESE FIRST"):prompt.index("WHO IT IS FOR")]

    assert "instagram.com/chuliahair" in sources, "studied for tone and photos"
    assert "https://instagram.com/chuliahair" in prompt[prompt.index("SOCIAL PROFILES"):]


def test_the_builder_is_told_to_say_so_rather_than_invent_what_it_could_not_open():
    prompt = motion_prompt(maps_url="https://maps.google.com/?cid=123")
    assert "do not invent what you would have found there" in prompt


def test_sources_come_before_the_design_brief():
    """Study first, then design -- the order is the instruction."""
    prompt = motion_prompt(maps_url="https://maps.google.com/?cid=1")
    assert prompt.index("STUDY THESE FIRST") < prompt.index("THE ONE JOB")


def test_no_sources_means_no_study_section():
    assert "STUDY THESE FIRST" not in motion_prompt()


# ------------------------------------------------------------- imagery


def test_imagery_guidance_appears_only_when_there_are_sources():
    assert "IMAGERY" not in motion_prompt()
    assert "IMAGERY" in motion_prompt(maps_url="https://maps.google.com/?cid=123")


def test_imagery_names_all_three_source_types_and_the_sequence():
    """Lyon: pull all three, then select, then decide reuse or recreate per
    photo -- not merely read the pages for tone."""
    prompt = motion_prompt(
        maps_url="https://maps.google.com/?cid=123",
        social_profiles=(
            "Instagram @chuliahair https://instagram.com/chuliahair\n"
            "Facebook @chuliahair https://facebook.com/chuliahair"
        ),
    )
    imagery = prompt[prompt.index("IMAGERY"):prompt.index("WHO IT IS FOR")]

    assert "Google Business Profile photos" in imagery
    assert "Instagram" in imagery
    assert "Facebook" in imagery
    assert "Pull every photo" in imagery
    assert "Then select." in imagery
    assert "use it as-is" in imagery
    assert "recreate the same subject and framing" in imagery


def test_imagery_states_the_judgement_call_with_a_concrete_comparison():
    prompt = motion_prompt(maps_url="https://maps.google.com/?cid=123")
    assert "Penang shoplot" in prompt
    assert "2019" in prompt
    assert "hero image must come from the real premises" in prompt


def test_imagery_never_substitutes_stock_for_either_case():
    prompt = motion_prompt(maps_url="https://maps.google.com/?cid=123")
    assert "Never substitute stock photography" in prompt


def test_imagery_admits_when_a_login_walled_platform_will_not_open():
    """Instagram and Facebook are login-walled and often can't be fetched;
    Google Business photos usually can. The prompt must say so plainly
    rather than paper over it -- a site built on invented photography is
    worse than one that admits it used a placeholder."""
    prompt = motion_prompt(maps_url="https://maps.google.com/?cid=123")
    assert "login-walled" in prompt
    assert "say which one plainly" in prompt
    assert "never invent a photo you could not see" in prompt


def test_showcase_cards_point_at_imagery_instead_of_repeating_it():
    prompt = rich(maps_url="https://maps.google.com/?cid=123")
    assert "Follow IMAGERY above for the photo" in prompt


def test_showcase_cards_never_reference_imagery_when_there_are_no_sources():
    """The bug: with no maps_url and no social_profiles, IMAGERY never
    renders (see test_imagery_guidance_appears_only_when_there_are_sources),
    but the showcase-card paragraph pointed at it anyway -- and the rule that
    sentence replaced ('a solid tinted block from the palette') was gone, so
    the one case with no photo source at all had no image instruction."""
    prompt = rich()  # no maps_url, no social_profiles -- blank of both

    assert "IMAGERY" not in prompt
    assert "Follow IMAGERY" not in prompt
    assert "solid tinted block from the palette" in prompt
    assert "never a stock photo" in prompt


def test_stage_4_passes_the_maps_url_through():
    sheets = FakeSheets(
        index([{**lead("a"), "Maps URL": "https://maps.google.com/?cid=99"}]),
        index([research_row("a")]),
    )
    site_brief.run(config(), sheets, FakeWriter(), LovableAdapter())

    assert "https://maps.google.com/?cid=99" in sheets.upserts[0][1][0]["Lovable Prompt"]


def test_the_model_is_told_to_design_motion_for_this_business():
    from outreach.providers.llm import SITE_SPEC_SCHEMA, SITE_SPEC_SYSTEM

    for field in ("signature_moment", "motion_language", "micro_interactions"):
        assert field in SITE_SPEC_SCHEMA["required"]
    assert "milliseconds" in SITE_SPEC_SYSTEM
    assert "signature moment" in SITE_SPEC_SYSTEM
    assert "portfolios whose job is to be" in SITE_SPEC_SYSTEM


def test_the_spec_model_is_told_the_photos_are_its_raw_material():
    """Task 6: two paragraphs telling the model that visual_direction and
    avoid should be written against what is actually photographable for THIS
    business, and that the hero still does not animate even as the ambition
    of signature_moment and motion_language goes up."""
    from outreach.providers.llm import SITE_SPEC_SYSTEM

    assert "image library" in SITE_SPEC_SYSTEM
    assert "actually photographable for THIS business" in SITE_SPEC_SYSTEM
    assert "Raise the ambition" in SITE_SPEC_SYSTEM
    assert "the hero itself does not animate" in SITE_SPEC_SYSTEM


def test_the_required_field_count_has_not_moved():
    """Ratchet, not a red-then-green test: it passes today and is meant to
    keep passing. SITE_SPEC_SCHEMA["required"] is deliberately capped at 19
    (modelcheck.py calls it out to Lyon as 'the demanding one') because a
    20th required field costs every weaker non-Anthropic model, not just the
    prompt template. This guards against that cap quietly moving."""
    from outreach.providers.llm import SITE_SPEC_SCHEMA

    assert len(SITE_SPEC_SCHEMA["required"]) == 19


def test_the_prompt_stays_pasteable():
    """Lyon pastes FULL_SPEC-sized prompts into Lovable's box by hand. The
    ceiling is the richest realistic prompt (every optional field filled,
    both social sources present) measured after Tasks 4-6, plus modest
    headroom -- so the next person who wants to add another block has to
    make a trade instead of silently pushing the prompt past what's
    actually pasteable."""
    prompt = rich(
        maps_url="https://maps.google.com/?cid=123",
        social_profiles=(
            "Instagram @chuliahair https://instagram.com/chuliahair\n"
            "Facebook @chuliahair https://facebook.com/chuliahair"
        ),
        phone="04-262 1188",
        address="58 Lebuh Chulia, 10200 George Town",
    )
    # Measured at 9150 chars post Tasks 4-6 (IMAGERY, the motion floor, and
    # the scoped performance-budget bullet). 9500 leaves ~350 chars of
    # headroom -- enough to fix a typo, not enough to add a section for free.
    assert len(prompt) < 9500


# ------------------------------- a build order, not a description of a website


RICH_SPEC = replace(
    MOTION_SPEC,
    highlights=("Walk-ins Welcome", "Scalp Check First", "Closed Tuesdays"),
    showcase_items=(
        "Scalp-first cut and treatment -- 45 min, Consultation included",
        "Colour correction -- Half day, Bond repair",
        "Student trim -- Under RM30, No appointment",
    ),
    interactive_feature=(
        "A three-question picker titled 'What's going on with your hair?' -- "
        "tabs for Concern, Length, When. Each answer updates a live summary "
        "card naming the treatment it points to, and the button beneath it "
        "opens WhatsApp with that treatment already written into the message."
    ),
    mobile_bar=("Call -> tel: link", "WhatsApp -> click-to-chat with the salon"),
)


def rich(**kwargs):
    return render_lovable_prompt(
        business_name="Chulia Hair House", category="hair salon",
        location="Georgetown", spec=RICH_SPEC, **kwargs,
    )


def test_the_highlights_strip_is_specified():
    prompt = rich()
    assert "HIGHLIGHTS STRIP" in prompt
    assert "Walk-ins Welcome" in prompt


def test_offerings_become_numbered_cards_with_tags():
    """Enumerated items produce a page; 'showcase the services' produces a stub."""
    prompt = rich()
    assert "1. Scalp-first cut and treatment -- 45 min, Consultation included" in prompt
    assert "3. Student trim" in prompt
    assert "opening a modal" in prompt


def test_cards_prefer_real_photos_over_stock():
    prompt = rich()
    assert "never a stock photo" in prompt


def test_the_interactive_piece_is_briefed_and_tied_to_the_cta():
    prompt = rich()
    assert "THE INTERACTIVE PIECE" in prompt
    assert "three-question picker" in prompt
    assert 'handing them to "Book a time"' in prompt


def test_the_interactive_piece_must_hold_state_and_work_by_thumb():
    prompt = rich()
    assert "work with a thumb" in prompt
    assert "hold its state" in prompt


def test_the_mobile_bar_is_pinned_and_does_not_cover_content():
    prompt = rich()
    assert "FLOATING BAR ON MOBILE" in prompt
    assert "Call -> tel: link" in prompt
    assert "pad the page bottom by its height" in prompt


def test_the_build_order_reads_top_to_bottom():
    """A builder works down the page; the brief must be in that order."""
    prompt = rich(maps_url="https://maps.google.com/?cid=1", phone="04-262 1188")
    order = [
        "STUDY THESE FIRST",
        "WHO IT IS FOR",
        "THE ONE JOB",
        "HERO",
        "HOME PAGE, IN SCROLL ORDER",
        "HIGHLIGHTS STRIP",
        "WHAT THEY OFFER",
        "THE INTERACTIVE PIECE",
        "ART DIRECTION",
        "MOTION AND INTERACTION",
        "FLOATING BAR ON MOBILE",
        "REAL DETAILS",
        "BUILD RULES",
    ]
    positions = [prompt.index(section) for section in order]
    assert positions == sorted(positions), "the brief is out of build order"


def test_a_thin_spec_still_renders_without_empty_headings():
    prompt = render_lovable_prompt(
        business_name="X", category="salon", location="Penang", spec=FULL_SPEC
    )
    for absent in (
        "HIGHLIGHTS STRIP", "WHAT THEY OFFER", "THE INTERACTIVE PIECE",
        "FLOATING BAR ON MOBILE",
    ):
        assert absent not in prompt


def test_the_model_must_supply_the_structural_fields():
    from outreach.providers.llm import SITE_SPEC_SCHEMA, SITE_SPEC_SYSTEM

    for field in ("highlights", "showcase_items", "interactive_feature", "mobile_bar"):
        assert field in SITE_SPEC_SCHEMA["required"]
    assert "Name the pattern" in SITE_SPEC_SYSTEM
    assert "mirrors how they sell" in SITE_SPEC_SYSTEM


def test_sections_are_asked_for_as_named_ui_patterns():
    from outreach.providers.llm import SITE_SPEC_SCHEMA

    described = SITE_SPEC_SCHEMA["properties"]["key_sections"]["description"]
    assert "UI pattern" in described
    for pattern in ("sticky nav", "card grid", "tabbed builder", "review ticker"):
        assert pattern in described


# ---------------------------- the preference has to hold past the research step


def ranked_lead(lead_id, *, social=False, reviews=10):
    return {
        **lead(lead_id),
        "Website Status": "social_only" if social else "none",
        "Review Count": str(reviews),
    }


def test_briefs_are_written_for_the_social_leads_first():
    """The bug: research ranked its queue, site-brief did not, so an older
    website-less lead jumped straight to the front of the brief step."""
    sheets = FakeSheets(
        index(
            [
                ranked_lead("old_no_social"),
                ranked_lead("fb", social=True),
            ]
        ),
        index([research_row("old_no_social"), research_row("fb")]),
    )
    site_brief.run(config(), sheets, FakeWriter(), LovableAdapter(), limit=1)

    written = [r["Lead ID"] for _tab, rows in sheets.upserts for r in rows]
    assert written == ["fb"]


def test_sheet_order_can_still_be_asked_for():
    sheets = FakeSheets(
        index([ranked_lead("plain"), ranked_lead("fb", social=True)]),
        index([research_row("plain"), research_row("fb")]),
    )
    site_brief.run(
        config(), sheets, FakeWriter(), LovableAdapter(), limit=1, social_first=False
    )

    written = [r["Lead ID"] for _tab, rows in sheets.upserts for r in rows]
    assert written == ["plain"]


def test_ranking_does_not_change_who_is_eligible():
    sheets = FakeSheets(
        index([ranked_lead("plain"), ranked_lead("fb", social=True)]),
        index([research_row("plain"), research_row("fb")]),
    )
    report = site_brief.run(config(), sheets, FakeWriter(), LovableAdapter())

    written = {r["Lead ID"] for _tab, rows in sheets.upserts for r in rows}
    assert written == {"plain", "fb"}
    assert report.prompts_written == 2


# --------------------------- Leads-tab fallback when Social Profiles is blank


def test_blank_social_profiles_falls_back_to_the_leads_tab_website_url():
    """Rows written before the Social Profiles column existed read back "".

    The lead's own Website URL is a Facebook link the discovery pass already
    has on hand -- reading it costs nothing extra, and it beats sending
    Lovable a prompt with no social section at all.
    """
    sheets = FakeSheets(
        index([{**lead("a"), "Website URL": "https://facebook.com/sun"}]),
        index([{**research_row("a"), "Social Profiles": ""}]),
    )
    site_brief.run(config(), sheets, FakeWriter(), LovableAdapter())

    prompt = sheets.upserts[0][1][0]["Lovable Prompt"]
    assert "Facebook @sun https://facebook.com/sun" in prompt
    # Render-only: the derived value must never reach the Research tab, or a
    # future real lookup would find the cell already (wrongly) non-blank.
    assert [tab for tab, _rows in sheets.upserts] == [schema.SITE_BRIEFS]


def test_a_real_research_lookup_is_not_overridden_by_the_leads_tab():
    """The Research row's own value is strictly better and must win outright."""
    sheets = FakeSheets(
        index([{**lead("a"), "Website URL": "https://facebook.com/wrongsalon"}]),
        index(
            [
                {
                    **research_row("a"),
                    "Social Profiles": "Instagram @sun https://instagram.com/sun",
                }
            ]
        ),
    )
    site_brief.run(config(), sheets, FakeWriter(), LovableAdapter())

    prompt = sheets.upserts[0][1][0]["Lovable Prompt"]
    assert "Instagram @sun https://instagram.com/sun" in prompt
    assert "wrongsalon" not in prompt
