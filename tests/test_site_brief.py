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
        """Mirrors SheetsClient.upsert: on_conflict actually merges against
        what's stored, and only automated columns are overwritten -- a manual
        column (or one an on_conflict declined to touch) keeps what was
        already there. A fake that skipped this once let a manual-column bug
        ship past a green suite (see CLAUDE.md); the same risk applies to any
        AUTOMATED column an on_conflict is relied on to preserve, which is
        exactly what the rewrite tests below are checking.
        """
        self.upserts.append((tab, list(rows)))
        from outreach.sheets import UpsertResult

        store = self._tabs.setdefault(tab.title, {})
        automated = {
            tab.columns[i].name for start, end in tab.automated_runs() for i in range(start, end)
        }
        result = UpsertResult()

        for incoming in rows:
            key = str(incoming.get(tab.columns[0].name, "")).strip()
            if not key:
                result.skipped += 1
                continue

            match = store.get(key)
            if match is None:
                row_number = len(store) + 2
                full_row = {c.name: incoming.get(c.name, "") for c in tab.columns}
                store[key] = (row_number, full_row)
                result.inserted += 1
                continue

            row_number, existing_row = match
            merged = on_conflict(existing_row, incoming) if on_conflict else incoming
            if merged is None:
                result.skipped += 1
                continue

            updated_row = dict(existing_row)
            for name in automated:
                updated_row[name] = merged.get(name, "")
            store[key] = (row_number, updated_row)
            result.updated += 1

        return result

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


# ------------------------------------------------------------- rewrite


def existing_brief(
    lead_id,
    *,
    prompt="old prompt, written before the template changed",
    approval="Approved",
    project_id="",
    preview_url="",
    created_at="2024-01-01T00:00:00Z",
):
    return {
        "Lead ID": lead_id,
        "Business Name": f"Salon {lead_id}",
        "Lovable Prompt": prompt,
        "Approval Status": approval,
        "Lovable Project ID": project_id,
        "Preview URL": preview_url,
        "Created At": created_at,
    }


def test_preserve_rewrite_fields_keeps_the_built_link_and_swaps_only_the_prompt():
    """Unit-level: the on_conflict function itself, isolated from the Sheet.

    Broken by commenting out any one of the three preserved keys in
    ``_preserve_rewrite_fields`` -- verified by hand while writing this test,
    each removal turns the corresponding assertion into a KeyError/blank
    mismatch.
    """
    from outreach.stages.site_brief import _preserve_rewrite_fields

    existing = existing_brief(
        "a", project_id="proj_1", preview_url="https://preview.test/a"
    )
    incoming = {"Lead ID": "a", "Business Name": "Salon a", "Lovable Prompt": "new"}

    merged = _preserve_rewrite_fields(existing, incoming)

    assert merged["Lovable Prompt"] == "new"
    assert merged["Lovable Project ID"] == "proj_1"
    assert merged["Preview URL"] == "https://preview.test/a"
    assert merged["Created At"] == "2024-01-01T00:00:00Z"


def test_rewrite_replaces_the_prompt_on_an_existing_row():
    sheets = FakeSheets(
        index([lead("a", stage="Site Briefed")]),
        index([research_row("a")]),
        briefs=index([existing_brief("a")]),
    )
    report = site_brief.run(
        config(), sheets, FakeWriter(), LovableAdapter(), rewrite=True
    )

    assert report.rewritten == 1
    stored = sheets.read_rows(schema.SITE_BRIEFS)["a"][1]
    assert stored["Lovable Prompt"] != "old prompt, written before the template changed"
    assert "Sun Salon" not in stored["Lovable Prompt"]  # uses the lead's own name


def test_rewrite_preserves_project_id_preview_url_and_created_at():
    """The data-loss guard. A plain upsert with no on_conflict blanks every
    AUTOMATED column it doesn't mention -- Lovable Project ID and Preview URL
    are two of exactly those columns. Proven non-vacuous by deleting
    ``on_conflict=_preserve_rewrite_fields`` from the ``_flush`` call inside
    ``_run_rewrite``: with it gone this test fails, because FakeSheets.upsert
    (now mirroring the real client) blanks Preview URL and Created At since
    the incoming row never carries them.
    """
    sheets = FakeSheets(
        index([lead("a", stage="Site Briefed")]),
        index([research_row("a")]),
        briefs=index(
            [
                existing_brief(
                    "a",
                    preview_url="https://preview.test/a",
                    created_at="2024-03-01T00:00:00Z",
                )
            ]
        ),
    )
    site_brief.run(config(), sheets, FakeWriter(), LovableAdapter(), rewrite=True)

    stored = sheets.read_rows(schema.SITE_BRIEFS)["a"][1]
    assert stored["Lovable Project ID"] == ""
    assert stored["Preview URL"] == "https://preview.test/a"
    assert stored["Created At"] == "2024-03-01T00:00:00Z"


def test_rewrite_never_emits_approval_status_in_an_upsert_row_or_column_write():
    sheets = FakeSheets(
        index([lead("a", stage="Site Briefed")]),
        index([research_row("a")]),
        briefs=index([existing_brief("a", approval="Approved")]),
    )
    site_brief.run(config(), sheets, FakeWriter(), LovableAdapter(), rewrite=True)

    incoming_row = sheets.upserts[0][1][0]
    assert "Approval Status" not in incoming_row
    assert all(write[1] != "Approval Status" for write in sheets.column_writes)
    # It's still there, untouched -- just never written by this stage.
    stored = sheets.read_rows(schema.SITE_BRIEFS)["a"][1]
    assert stored["Approval Status"] == "Approved"


def test_rewrite_skips_a_row_whose_lovable_project_id_is_set():
    sheets = FakeSheets(
        index([lead("a", stage="Site Built")]),
        index([research_row("a")]),
        briefs=index([existing_brief("a", project_id="proj_1")]),
    )
    writer = FakeWriter()
    report = site_brief.run(config(), sheets, writer, LovableAdapter(), rewrite=True)

    assert report.selected == 0
    assert report.rewritten == 0
    assert writer.call_count == 0
    assert sheets.upserts == []


def test_rewrite_honours_limit():
    sheets = FakeSheets(
        index(
            [
                lead("a", stage="Site Briefed"),
                lead("b", stage="Site Briefed"),
            ]
        ),
        index([research_row("a"), research_row("b")]),
        briefs=index([existing_brief("a"), existing_brief("b")]),
    )
    report = site_brief.run(
        config(), sheets, FakeWriter(), LovableAdapter(), rewrite=True, limit=1
    )

    assert report.selected == 1
    assert report.rewritten == 1
    written = {r["Lead ID"] for _tab, rows in sheets.upserts for r in rows}
    assert len(written) == 1


def test_a_second_rewrite_over_the_same_rows_is_safe():
    sheets = FakeSheets(
        index([lead("a", stage="Site Briefed")]),
        index([research_row("a")]),
        briefs=index(
            [existing_brief("a", preview_url="", created_at="2024-01-01T00:00:00Z")]
        ),
    )
    first = site_brief.run(
        config(), sheets, FakeWriter(), LovableAdapter(), rewrite=True
    )
    second = site_brief.run(
        config(), sheets, FakeWriter(), LovableAdapter(), rewrite=True
    )

    assert (first.rewritten, first.errors) == (1, 0)
    assert (second.rewritten, second.errors) == (1, 0)
    stored = sheets.read_rows(schema.SITE_BRIEFS)["a"][1]
    assert stored["Created At"] == "2024-01-01T00:00:00Z"
    assert stored["Lovable Project ID"] == ""


def test_rewrite_off_is_byte_identical_to_the_default_run():
    """Guards the ``_flush``/``_Pending`` refactor: passing ``rewrite=False``
    (or leaving it out) must reach the exact same code path as before this
    task, producing the exact row this suite already pinned in
    ``test_writes_a_prompt_and_seeds_approval_as_pending``.
    """
    sheets = FakeSheets(index([lead("a")]), index([research_row("a")]))
    report = site_brief.run(
        config(), sheets, FakeWriter(), LovableAdapter(), rewrite=False
    )

    assert isinstance(report, site_brief.SiteBriefReport)
    assert report.prompts_written == 1
    row = sheets.upserts[0][1][0]
    assert row["Approval Status"] == "Pending"
    assert row["Lovable Project ID"] == ""
    assert row["Preview URL"] == ""
    assert "Created At" in row
    assert sheets.column_writes == [(schema.LEADS, "Stage", {"a": "Site Briefed"})]


def test_rewrite_writes_no_stage_column_update():
    sheets = FakeSheets(
        index([lead("a", stage="Site Briefed")]),
        index([research_row("a")]),
        briefs=index([existing_brief("a")]),
    )
    site_brief.run(config(), sheets, FakeWriter(), LovableAdapter(), rewrite=True)

    assert sheets.column_writes == []


def test_rewrite_excludes_a_disqualified_lead_with_an_existing_brief():
    sheets = FakeSheets(
        index([lead("a", stage="Disqualified")]),
        index([research_row("a")]),
        briefs=index([existing_brief("a")]),
    )
    report = site_brief.run(
        config(), sheets, FakeWriter(), LovableAdapter(), rewrite=True
    )

    assert report.selected == 0
    assert report.rewritten == 0


def test_rewrite_over_an_empty_sheet_logs_nothing_to_rewrite_and_returns_cleanly(
    caplog,
):
    import logging

    caplog.set_level(logging.INFO, logger="outreach")
    sheets = FakeSheets({}, {}, briefs={})

    report = site_brief.run(
        config(), sheets, FakeWriter(), LovableAdapter(), rewrite=True
    )

    assert report.selected == 0
    assert report.rewritten == 0
    assert sheets.upserts == []
    assert "nothing to rewrite" in caplog.text.lower()


# ------------------------------------------------- generate to disk (dry run)


class FakeGenerator:
    """Returns canned HTML, and canned image bytes.

    Mirrors the real SiteGenerator closely enough that a page which never
    asked for images still works: `images=None` means image generation is
    unavailable, which is exactly what a quota-exhausted run looks like.
    """

    def __init__(self, replies, images=b"\x89PNG-fake"):
        self._replies = list(replies)
        self._image = images
        self.call_count = 0
        self.image_calls = 0
        self.prompts = []
        self.image_prompts = []

    def generate(self, prompt):
        self.call_count += 1
        self.prompts.append(prompt)
        return self._replies.pop(0)

    def generate_image(self, prompt):
        from outreach.providers.sitegen import SiteGenError

        self.image_calls += 1
        self.image_prompts.append(prompt)
        if self._image is None:
            raise SiteGenError("no image quota left")
        return self._image, "image/png"


def _good_page(phone="04-262 1188", headline=None):
    # Must carry the spec's own headline: the prompt says "use this copy as
    # written", and check_site enforces it.
    headline = SPEC.hero_headline if headline is None else headline
    return (
        "<!doctype html><html><head><title>Chulia Hair House</title>"
        '<meta name="robots" content="noindex"></head><body>'
        f"<h1>{headline}</h1><p>Call {phone}</p>"
        "<p>Unofficial concept by COAI -- not affiliated.</p>"
        + "<p>content</p>" * 400
        + "</body></html>"
    )


def _tmp():
    import tempfile

    return tempfile.mkdtemp()


def _generate(
    tmp_path, replies, *, approval="Approved", stage="Site Briefed",
    images=b"\x89PNG-fake", images_per_page=None,
):
    from dataclasses import replace as dc_replace

    cfg = config()
    build = dc_replace(cfg.build, output_dir=str(tmp_path))
    if images_per_page is not None:
        build = dc_replace(build, images_per_page=images_per_page)
    cfg = dc_replace(cfg, build=build)
    leads = {
        "a": (2, {
            "Lead ID": "a", "Business Name": "Chulia Hair House", "Stage": stage,
            "Category": "hair salon", "Phone": "04-262 1188",
            "Address": "58 Lebuh Chulia", "Website URL": "", "Maps URL": "",
        })
    }
    sheets = FakeSheets(
        leads,
        {"a": (2, {"Lead ID": "a", "Social Profiles": ""})},
        {"a": (2, {
            "Lead ID": "a", "Business Name": "Chulia Hair House",
            "Approval Status": approval, "Lovable Project ID": "",
        })},
    )
    gen = FakeGenerator(replies, images=images)
    report = site_brief.generate_sites(cfg, sheets, FakeWriter(), gen)
    return report, sheets, gen, tmp_path


def test_a_generated_page_is_written_and_nothing_reaches_the_sheet(tmp_path):
    """The whole point of the dry run: files on disk, Sheet untouched."""
    report, sheets, _gen, root = _generate(tmp_path, [_good_page()])

    assert report.generated == 1
    written = list(root.rglob("index.html"))
    assert len(written) == 1
    assert "Chulia Hair House" in written[0].read_text()
    # Not one Sheet write of any kind, beyond an Errors append that did not happen.
    assert sheets.upserts == []
    assert sheets.column_writes == []


def test_a_page_that_stays_invalid_is_never_written(tmp_path):
    """A truncated page must not reach disk, because a deploy walks the disk.

    One repair is attempted; if it still fails the lead is logged and skipped.
    """
    truncated = "<!doctype html><html><body><h1>Half a p"
    report, sheets, gen, root = _generate(tmp_path, [truncated, truncated])

    assert gen.call_count == 2, "one generate, then one repair"
    assert report.generated == 0
    assert report.rejected == 1
    assert report.errors == 1
    assert list(root.rglob("index.html")) == []
    assert sheets.appended and sheets.appended[0][0] is schema.ERRORS


def test_one_repair_rescues_a_bad_first_reply(tmp_path):
    report, _sheets, gen, root = _generate(
        tmp_path, ["<!doctype html><html><body><h1>cut", _good_page()]
    )

    assert gen.call_count == 2
    assert report.generated == 1
    assert report.repaired == 1
    assert len(list(root.rglob("index.html"))) == 1


def test_a_page_already_on_disk_costs_no_model_call(tmp_path):
    """What makes a re-run free -- and it only works because invalid pages
    never land on disk in the first place."""
    _first, _sheets, _gen, root = _generate(tmp_path, [_good_page()])

    report, _s2, gen2, _r2 = _generate(tmp_path, [])

    assert gen2.call_count == 0
    assert report.already_on_disk == 1
    assert report.generated == 0


def test_a_disqualified_lead_never_gets_a_page(tmp_path):
    """Its brief was approved before it was disqualified. Publishing a public
    site for someone who asked not to be contacted is the worst outcome here."""
    report, _sheets, gen, root = _generate(
        tmp_path, [_good_page()], stage="Disqualified"
    )

    assert gen.call_count == 0
    assert report.generated == 0
    assert list(root.rglob("index.html")) == []


def test_only_approved_rows_are_generated(tmp_path):
    report, _sheets, gen, root = _generate(
        tmp_path, [_good_page()], approval="Pending"
    )

    assert gen.call_count == 0
    assert report.approved == 0
    assert list(root.rglob("index.html")) == []


def test_the_html_target_prompt_is_what_reaches_the_generator(tmp_path):
    """A Lovable-targeted prompt here would ask for React that never builds."""
    _report, _sheets, gen, _root = _generate(tmp_path, [_good_page()])

    prompt = gen.prompts[0]
    assert "Framer Motion" not in prompt
    assert "shadcn" not in prompt
    assert "IMAGERY" not in prompt


# ------------------------------------------------------------- regenerate one


def _regen(notes="", *, approval="Pending"):
    leads = {"a": (2, {
        "Lead ID": "a", "Business Name": "Chulia Hair House", "Stage": "Site Briefed",
        "Category": "hair salon", "Phone": "04-262 1188", "Address": "58 Lebuh Chulia",
        "Website URL": "", "Maps URL": "",
    })}
    sheets = FakeSheets(
        leads,
        {"a": (2, {"Lead ID": "a", "Social Profiles": "", "Tone": "warm"})},
        {"a": (2, {
            "Lead ID": "a", "Business Name": "Chulia Hair House",
            "Approval Status": approval, "Lovable Prompt": "the old prompt",
            "Lovable Project ID": "", "Preview URL": "", "Created At": "2026-01-01",
        })},
    )

    class Spy(FakeWriter):
        def __init__(self):
            super().__init__()
            self.spec_prompt = ""

        def write_site_spec(self, prompt):
            self.spec_prompt = prompt
            return super().write_site_spec(prompt)

    writer = Spy()
    result = site_brief.regenerate_one(config(), sheets, writer, "a", notes=notes)
    return result, sheets, writer


def test_regenerating_writes_only_the_prompt_cell():
    """Approval Status, Created At and the built marker belong to other
    decisions. A rewrite that cleared any of them would lose real work."""
    prompt, sheets, _writer = _regen(approval="Approved")

    assert prompt
    written = [(tab.title, col) for tab, col, _v in sheets.column_writes]
    assert written == [(schema.SITE_BRIEFS.title, "Lovable Prompt")]
    assert all(col != "Approval Status" for _t, col, _v in sheets.column_writes)


def test_my_notes_reach_the_model():
    _prompt, _sheets, writer = _regen("make it far more upmarket")

    assert "make it far more upmarket" in writer.spec_prompt
    assert "WHAT THE FOUNDER WANTS CHANGED" in writer.spec_prompt


def test_a_plain_rewrite_sends_no_notes_section():
    """A leftover instruction from the last lead steering this one is the
    failure mode that makes 'why is every brief dark now' unanswerable."""
    _prompt, _sheets, writer = _regen("")

    assert "WHAT THE FOUNDER WANTS CHANGED" not in writer.spec_prompt


def test_regenerating_a_lead_with_no_research_says_what_to_do():
    leads = {"a": (2, {"Lead ID": "a", "Business Name": "Sun Salon"})}
    sheets = FakeSheets(leads, {}, {})

    with pytest.raises(ValueError, match="run Research for it"):
        site_brief.regenerate_one(config(), sheets, FakeWriter(), "a")


def test_regenerating_an_unknown_lead_is_refused():
    sheets = FakeSheets({}, {}, {})

    with pytest.raises(ValueError, match="No lead with ID"):
        site_brief.regenerate_one(config(), sheets, FakeWriter(), "nope")


# ------------------------------------------------------------------- imagery


def test_images_are_generated_and_written_next_to_the_page():
    """A page of colour and type alone reads as a wireframe. The images sit
    beside index.html and are referenced relatively, which is also the only
    shape the site check allows."""
    report, _sheets, gen, root = _generate(
        __import__("pathlib").Path(_tmp()), [_good_page()], images_per_page=3
    )

    assert gen.image_calls == 3  # this test asks for three explicitly
    folder = next(p for p in root.iterdir() if p.is_dir())
    written = sorted(f.name for f in folder.iterdir())
    assert "index.html" in written
    assert len([f for f in written if f.startswith("image-")]) == 3


def test_the_page_is_told_which_image_files_exist():
    _report, _sheets, gen, _root = _generate(
        __import__("pathlib").Path(_tmp()), [_good_page()], images_per_page=3
    )

    prompt = gen.prompts[0]
    assert "IMAGES YOU HAVE" in prompt
    assert "image-1.png" in prompt
    # It must not be free to invent a filename or reach off-site for one.
    assert "Do not invent an image file" in prompt


def test_no_image_prompt_ever_asks_for_the_real_premises():
    """A generated interior of *their* salon, inside a pitch that says 'I
    looked at your business', is a fabricated depiction of a real place --
    and the owner is the one person certain to know it is not their room."""
    _report, _sheets, gen, _root = _generate(
        __import__("pathlib").Path(_tmp()), [_good_page()], images_per_page=3
    )

    # Assert something was actually asked for first. `generate_for_site`
    # refuses a banned prompt before sending it, so a loop over "prompts that
    # were sent" passes vacuously on an empty list -- which is exactly what a
    # briefs list full of banned wording would produce.
    assert len(gen.image_prompts) == 3, "no image prompts were sent at all"

    banned = ("shopfront", "storefront", "interior of", "the owner", "the staff")
    for prompt in gen.image_prompts:
        low = prompt.casefold()
        for word in banned:
            assert word not in low, f"image prompt asks for {word!r}: {prompt}"
        assert "no people" in low or "not a person" in low


def test_a_page_still_gets_built_when_images_fail():
    """Image quota is a separate, smaller budget. Losing the whole lead over
    decoration is the wrong trade."""
    import pathlib

    root = pathlib.Path(_tmp())
    report, _sheets, gen, _r = _generate(root, [_good_page()], images=None)

    assert report.generated == 1
    folder = next(p for p in root.iterdir() if p.is_dir())
    assert (folder / "index.html").exists()
    assert not [f for f in folder.iterdir() if f.name.startswith("image-")]


def test_running_out_of_image_quota_stops_the_run_asking():
    """Image models have their own, much smaller allowance than text. When it
    runs out every request 429s instantly -- and without a breaker a sixty-lead
    run spends a hundred and eighty calls learning that a hundred and eighty
    times, against the same per-minute budget the pages need."""
    import pathlib

    from outreach.providers.sitegen import SiteGenError

    class OutOfQuota(FakeGenerator):
        quota = "GenerateRequestsPerDayPerProject-FreeTier"

        def generate_image(self, prompt):
            self.image_calls += 1
            raise SiteGenError("over quota", status_code=429, quota=self.quota)

    from dataclasses import replace as dc_replace

    root = pathlib.Path(_tmp())
    cfg = config()
    cfg = dc_replace(cfg, build=dc_replace(cfg.build, output_dir=str(root)))

    leads, sheets = {}, None
    rows = {}
    for key in ("a", "b"):
        rows[key] = (2, {
            "Lead ID": key, "Business Name": f"Salon {key}", "Stage": "Site Briefed",
            "Category": "hair salon", "Phone": "04-262 1188",
            "Address": "1 St", "Website URL": "", "Maps URL": "",
        })
    sheets = FakeSheets(
        rows,
        {k: (2, {"Lead ID": k, "Social Profiles": ""}) for k in rows},
        {k: (2, {"Lead ID": k, "Business Name": f"Salon {k}",
                 "Approval Status": "Approved", "Lovable Project ID": ""}) for k in rows},
    )
    gen = OutOfQuota([_good_page(), _good_page()])

    report = site_brief.generate_sites(cfg, sheets, FakeWriter(), gen)

    assert report.generated == 2, "pages must still be built without images"
    # One refusal on the first lead closes it. Without the breaker this would
    # be 3 per lead across 2 leads = 6.
    assert gen.image_calls == 1, f"asked {gen.image_calls} times after running out"


def test_a_per_minute_image_limit_only_skips_the_page_it_hit():
    """The two refusals wear the same HTTP 429 and need opposite reactions.

    A per-minute limit refills on its own clock, so the next page -- a minute
    of page generation later -- should try again. Treating it like a per-day
    ceiling throws away the images for every remaining lead because one
    minute happened to be busy.
    """
    import pathlib
    from dataclasses import replace as dc_replace

    from outreach.providers.sitegen import SiteGenError

    class Busy(FakeGenerator):
        def __init__(self, replies):
            super().__init__(replies)
            self.fail_next = True

        def generate_image(self, prompt):
            self.image_calls += 1
            if self.fail_next:
                self.fail_next = False
                raise SiteGenError(
                    "over quota",
                    status_code=429,
                    quota="GenerateContentInputTokensPerModelPerMinute-FreeTier",
                )
            return b"\x89PNG", "image/png"

    root = pathlib.Path(_tmp())
    cfg = config()
    cfg = dc_replace(
        cfg, build=dc_replace(cfg.build, output_dir=str(root), images_per_page=3)
    )
    rows = {
        k: (2, {
            "Lead ID": k, "Business Name": f"Salon {k}", "Stage": "Site Briefed",
            "Category": "hair salon", "Phone": "04-262 1188", "Address": "1 St",
            "Website URL": "", "Maps URL": "",
        })
        for k in ("a", "b")
    }
    sheets = FakeSheets(
        rows,
        {k: (2, {"Lead ID": k, "Social Profiles": ""}) for k in rows},
        {k: (2, {"Lead ID": k, "Business Name": f"Salon {k}",
                 "Approval Status": "Approved", "Lovable Project ID": ""}) for k in rows},
    )
    gen = Busy([_good_page(), _good_page()])

    report = site_brief.generate_sites(cfg, sheets, FakeWriter(), gen)

    assert report.generated == 2
    # First page hit the limit and went without. The second tried again and
    # got its three images -- 1 failed call + 3 successful ones.
    assert gen.image_calls == 4, f"expected a retry on the next page, got {gen.image_calls}"
    folders = sorted(p for p in root.iterdir() if p.is_dir())
    counts = sorted(len([f for f in d.iterdir() if f.name.startswith("image-")]) for d in folders)
    assert counts == [0, 3], f"expected one page without images and one with, got {counts}"


def test_the_run_image_cap_stops_spending_but_not_the_run():
    """The only guardrail that is code rather than documentation.

    Vertex spends real credits, so a loop that misbehaves overnight is Lyon's
    money rather than a free-tier 429. Reaching the cap must close the budget
    and let the remaining pages build without images -- failing the run would
    throw away pages that are already paid for.
    """
    import pathlib
    from dataclasses import replace as dc_replace

    root = pathlib.Path(_tmp())
    cfg = config()
    cfg = dc_replace(
        cfg,
        build=dc_replace(
            cfg.build, output_dir=str(root), images_per_page=1, max_images_per_run=2
        ),
    )
    rows = {
        k: (2, {
            "Lead ID": k, "Business Name": f"Salon {k}", "Stage": "Site Briefed",
            "Category": "hair salon", "Phone": "04-262 1188", "Address": "1 St",
            "Website URL": "", "Maps URL": "",
        })
        for k in ("a", "b", "c", "d")
    }
    sheets = FakeSheets(
        rows,
        {k: (2, {"Lead ID": k, "Social Profiles": ""}) for k in rows},
        {k: (2, {"Lead ID": k, "Business Name": f"Salon {k}",
                 "Approval Status": "Approved", "Lovable Project ID": ""}) for k in rows},
    )
    gen = FakeGenerator([_good_page() for _ in rows])

    report = site_brief.generate_sites(cfg, sheets, FakeWriter(), gen)

    assert report.generated == 4, "every page still gets built"
    assert gen.image_calls == 2, f"cap of 2 not honoured -- {gen.image_calls} images bought"
    with_images = sum(
        1 for d in root.iterdir()
        if d.is_dir() and any(f.name.startswith("image-") for f in d.iterdir())
    )
    assert with_images == 2


def test_an_unlimited_cap_is_the_default_behaviour():
    """0 means unlimited, so the free-tier path is unchanged by this."""
    from outreach.images import ImageBudget

    budget = ImageBudget(limit=0)
    for _ in range(500):
        budget.spent()

    assert budget.open, "limit=0 must never close the budget"
