"""Stage 2: selection/resume logic, checkpointing, and per-lead failure."""

from dataclasses import replace

from outreach import schema
from outreach.config import load_config
from outreach.providers.llm import LLMError, ResearchBrief
from outreach.providers.places import PlaceDetails, Review
from outreach.providers.social import SocialProfile
from outreach.stages import research

PENANG = "config/campaigns/penang-my.yaml"


def config():
    return load_config(PENANG, env_file=None)


def lead_row(lead_id, stage="Discovered", name=None, website_status="none"):
    return {
        "Lead ID": lead_id,
        "Stage": stage,
        "Business Name": name or f"Salon {lead_id}",
        "Category": "Hair salon",
        "Address": "1 Test St",
        "Website Status": website_status,
        "Website URL": "",
    }


def leads_index(rows):
    return {r["Lead ID"]: (i + 2, r) for i, r in enumerate(rows)}


# --------------------------------------------------------------- selection


def test_skips_leads_that_already_have_a_research_row():
    leads = leads_index([lead_row("a"), lead_row("b"), lead_row("c")])
    selected, already, not_ready = research.select_leads(
        leads, {"a", "c"}, batch_size=100
    )

    assert [r["Lead ID"] for r in selected] == ["b"]
    assert already == 2
    assert not_ready == 0


def test_skips_leads_already_past_discovered():
    leads = leads_index(
        [
            lead_row("a", stage="Researched"),
            lead_row("b", stage="Email Drafted"),
            lead_row("c", stage="Discovered"),
        ]
    )
    selected, already, _ = research.select_leads(leads, set(), batch_size=100)

    assert [r["Lead ID"] for r in selected] == ["c"]
    assert already == 2


def test_never_touches_a_disqualified_lead():
    leads = leads_index([lead_row("a", stage="Disqualified")])
    selected, _, not_ready = research.select_leads(leads, set(), batch_size=100)

    assert selected == []
    assert not_ready == 1


def test_blank_or_typo_stage_is_treated_as_discovered():
    leads = leads_index(
        [lead_row("a", stage=""), lead_row("b", stage="whoops typed this")]
    )
    selected, _, _ = research.select_leads(leads, set(), batch_size=100)
    assert {r["Lead ID"] for r in selected} == {"a", "b"}


def test_batch_cap_is_enforced():
    leads = leads_index([lead_row(f"p{i}") for i in range(250)])
    selected, _, _ = research.select_leads(leads, set(), batch_size=100)
    assert len(selected) == 100


def test_one_cap_governs_every_stage():
    """The number is the campaign's to choose; that it's shared is not."""
    cfg = config()
    assert cfg.batch_size == cfg.limits.batch_size
    assert cfg.discovery_cap == min(cfg.discovery.max_results, cfg.limits.batch_size)


def test_discovery_cannot_outrun_the_global_cap():
    from dataclasses import replace

    cfg = config()
    cfg = replace(
        cfg,
        limits=replace(cfg.limits, batch_size=25),
        discovery=replace(cfg.discovery, max_results=500),
    )
    assert cfg.discovery_cap == 25


# ------------------------------------------------------------------ fakes


class FakePlaces:
    def __init__(self, details=None, fail_on=()):
        self._details = details or {}
        self._fail_on = set(fail_on)
        self.request_count = 0
        self.asked_for = []

    def place_details(self, place_id):
        self.request_count += 1
        self.asked_for.append(place_id)
        if place_id in self._fail_on:
            raise RuntimeError(f"Places exploded for {place_id}")
        # Default name echoes the id so tests can key on it in the prompt.
        return self._details.get(
            place_id, PlaceDetails(place_id=place_id, name=place_id)
        )


class FakeWriter:
    def __init__(self, fail_on=()):
        self.call_count = 0
        self.prompts = []
        self._fail_on = set(fail_on)

    def write(self, prompt):
        self.call_count += 1
        self.prompts.append(prompt)
        for marker in self._fail_on:
            if marker in prompt:
                raise LLMError("model declined")
        return ResearchBrief(
            tone="warm and local",
            services=("cut", "colour"),
            price_tier="mid-range",
            customer_praise="Friendly staff",
            customer_pain_points="Hard to book",
            suggested_website_angle="Online booking would stop the phone tag.",
        )


class FakeSheets:
    def __init__(self, leads, researched=()):
        self._leads = leads
        self._research = {k: (i + 2, {"Lead ID": k}) for i, k in enumerate(researched)}
        self.upserts = []
        self.column_writes = []
        self.appended = []
        self.spreadsheet_id = "fake"

    def read_rows(self, tab):
        return self._leads if tab is schema.LEADS else self._research

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


# ------------------------------------------------------------------- run


def test_writes_a_brief_and_advances_the_stage():
    leads = leads_index([lead_row("a", name="Sun Salon")])
    sheets = FakeSheets(leads)
    details = {
        "a": PlaceDetails(
            place_id="a",
            name="Sun Salon",
            rating=4.6,
            review_count=31,
            reviews=(Review(rating=5, text="Lovely cut", when="a month ago"),),
        )
    }
    report = research.run(config(), FakePlaces(details), sheets, FakeWriter())

    assert report.briefed == 1
    tab, rows = sheets.upserts[0]
    assert tab is schema.RESEARCH
    assert rows[0]["Suggested Website Angle"].startswith("Online booking")
    assert rows[0]["Research Status"] == "ok"
    assert sheets.column_writes == [(schema.LEADS, "Stage", {"a": "Researched"})]


def test_rerun_does_no_work_when_everything_is_researched():
    leads = leads_index([lead_row("a"), lead_row("b")])
    sheets = FakeSheets(leads, researched=["a", "b"])
    places, writer = FakePlaces(), FakeWriter()

    report = research.run(config(), places, sheets, writer)

    assert report.selected == 0
    assert report.already_researched == 2
    # The expensive calls are the point: neither should have happened.
    assert places.request_count == 0
    assert writer.call_count == 0
    assert sheets.upserts == []


def test_a_failing_lead_is_logged_and_the_rest_still_save():
    leads = leads_index([lead_row("good1"), lead_row("bad"), lead_row("good2")])
    sheets = FakeSheets(leads)
    report = research.run(
        config(), FakePlaces(fail_on={"bad"}), sheets, FakeWriter()
    )

    assert report.briefed == 2
    assert report.errors == 1
    written = {r["Lead ID"] for _tab, rows in sheets.upserts for r in rows}
    assert written == {"good1", "good2"}
    assert sheets.appended and sheets.appended[0][0] is schema.ERRORS


def test_llm_refusal_is_a_per_lead_error_not_a_run_failure():
    leads = leads_index([lead_row("fine"), lead_row("refused")])
    sheets = FakeSheets(leads)
    report = research.run(
        config(), FakePlaces(), sheets, FakeWriter(fail_on={"Business name: refused"})
    )

    assert report.briefed == 1
    assert report.errors == 1


def with_checkpoint_every(n):
    """Config settings are frozen, so build a new one rather than mutating."""
    cfg = config()
    return replace(cfg, research=replace(cfg.research, checkpoint_every=n))


def test_checkpoints_flush_partway_through():
    cfg = with_checkpoint_every(2)

    leads = leads_index([lead_row(f"p{i}") for i in range(5)])
    sheets = FakeSheets(leads)
    report = research.run(cfg, FakePlaces(), sheets, FakeWriter())

    # 5 leads at 2 per checkpoint: two mid-run flushes plus the final one.
    assert report.checkpoints == 3
    assert report.briefed == 5
    assert sum(len(rows) for _tab, rows in sheets.upserts) == 5


def test_a_failed_checkpoint_does_not_lose_the_remaining_run():
    leads = leads_index([lead_row(f"p{i}") for i in range(3)])
    sheets = FakeSheets(leads)

    calls = {"n": 0}
    original = sheets.upsert

    def flaky(tab, rows, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Sheets 503")
        return original(tab, rows, **kwargs)

    sheets.upsert = flaky
    report = research.run(with_checkpoint_every(1), FakePlaces(), sheets, FakeWriter())

    assert report.briefed == 3
    assert report.errors >= 1


def test_limit_flag_cannot_exceed_the_configured_batch_size():
    cfg = config()
    leads = leads_index([lead_row(f"p{i}") for i in range(cfg.batch_size * 3)])
    sheets = FakeSheets(leads)
    report = research.run(cfg, FakePlaces(), sheets, FakeWriter(), limit=500)
    assert report.selected == cfg.batch_size


def test_social_lookup_runs_only_for_social_only_leads(monkeypatch):
    called = []

    def fake_lookup(url, **kwargs):
        called.append(url)
        return SocialProfile(handle="@sun", bio="Salon in GT", status="ok")

    monkeypatch.setattr(research.social_provider, "lookup", fake_lookup)

    leads = leads_index(
        [
            lead_row("plain", website_status="none"),
            lead_row("fb", website_status="social_only"),
        ]
    )
    leads["fb"][1]["Website URL"] = "https://facebook.com/sun"
    sheets = FakeSheets(leads)

    report = research.run(config(), FakePlaces(), sheets, FakeWriter())

    assert called == ["https://facebook.com/sun"]
    assert report.social_found == 1
    rows = {r["Lead ID"]: r for _tab, batch in sheets.upserts for r in batch}
    assert rows["fb"]["Social Handle"] == "@sun"
    assert rows["plain"]["Social Lookup Status"] == "no social profile on record"


def test_email_from_social_page_is_recorded_with_its_source(monkeypatch):
    monkeypatch.setattr(
        research.social_provider,
        "lookup",
        lambda url, **kw: SocialProfile(
            handle="@sun", status="ok", emails=("hello@sunsalon.my",)
        ),
    )
    leads = leads_index([lead_row("fb", website_status="social_only")])
    leads["fb"][1]["Website URL"] = "https://facebook.com/sun"
    sheets = FakeSheets(leads)

    report = research.run(config(), FakePlaces(), sheets, FakeWriter())

    assert report.emails_found == 1
    row = sheets.upserts[0][1][0]
    assert row["Email Found"] == "hello@sunsalon.my"
    assert row["Email Source"] == "social_page"


def test_no_email_is_reported_honestly_rather_than_guessed():
    leads = leads_index([lead_row("a")])
    sheets = FakeSheets(leads)
    research.run(config(), FakePlaces(), sheets, FakeWriter())

    row = sheets.upserts[0][1][0]
    assert row["Email Found"] == ""
    assert row["Email Source"] == "not found"


def test_reviews_are_summarised_but_never_written_to_the_sheet():
    review_text = "The stylist remembered my name after two years"
    details = {
        "a": PlaceDetails(
            place_id="a", name="Sun Salon", reviews=(Review(rating=5, text=review_text),)
        )
    }
    leads = leads_index([lead_row("a")])
    sheets = FakeSheets(leads)
    writer = FakeWriter()

    research.run(config(), FakePlaces(details), sheets, writer)

    # The model sees the raw review...
    assert review_text in writer.prompts[0]
    # ...but nothing written to the Sheet contains it.
    written = " ".join(
        str(v) for _tab, rows in sheets.upserts for r in rows for v in r.values()
    )
    assert review_text not in written


def test_the_research_row_records_every_social_profile():
    from outreach.providers.social import SocialLink, SocialProfile
    from outreach.stages.research import _brief_row

    profile = SocialProfile(
        handle="@espariohair",
        links=(
            SocialLink("Facebook", "@Espariohairsalon",
                       "https://facebook.com/Espariohairsalon-107771601074381"),
            SocialLink("Instagram", "@espariohair",
                       "https://instagram.com/espariohair"),
        ),
    )
    brief = ResearchBrief(
        tone="warm", services=("cut",), price_tier="mid-range",
        customer_praise="", customer_pain_points="", suggested_website_angle="",
    )
    row = _brief_row({"Lead ID": "a"}, brief, profile, "", "not found")

    assert row["Social Profiles"] == (
        "Facebook @Espariohairsalon https://facebook.com/Espariohairsalon-107771601074381\n"
        "Instagram @espariohair https://instagram.com/espariohair"
    )
    assert row["Social Handle"] == "@espariohair", "the old column still works"


# ----------------------------------- socials first: spend the budget on the best


def social_lead(lead_id, *, social=False, reviews=10):
    return {
        **lead_row(lead_id, website_status="social_only" if social else "none"),
        "Review Count": str(reviews),
    }


def test_leads_with_a_social_page_are_researched_first():
    """They have more to work with, so the brief and the demo come out better."""
    leads = leads_index(
        [
            social_lead("plain1"),
            social_lead("fb1", social=True),
            social_lead("plain2"),
            social_lead("fb2", social=True),
        ]
    )
    selected, _, _ = research.select_leads(leads, set(), batch_size=2)

    assert [r["Lead ID"] for r in selected] == ["fb1", "fb2"]


def test_within_a_group_sheet_order_is_kept():
    """The default. A queue you can predict beats a cleverer one you cannot.

    Sorting is stable, so the socials-first split is the only reordering --
    everything else stays in the order you can see on the Sheet.
    """
    leads = leads_index(
        [
            social_lead("thin", social=True, reviews=3),
            social_lead("rich", social=True, reviews=180),
            social_lead("mid", social=True, reviews=40),
        ]
    )
    selected, _, _ = research.select_leads(leads, set(), batch_size=3)

    assert [r["Lead ID"] for r in selected] == ["thin", "rich", "mid"]


def test_socials_still_jump_the_queue_in_sheet_order():
    leads = leads_index(
        [
            social_lead("plain1", reviews=500),
            social_lead("fb", social=True, reviews=2),
            social_lead("plain2", reviews=400),
        ]
    )
    selected, _, _ = research.select_leads(leads, set(), batch_size=3)

    assert [r["Lead ID"] for r in selected] == ["fb", "plain1", "plain2"]


def test_reviews_order_can_be_asked_for_instead():
    """Richer briefs, at the cost of a queue that no longer follows the Sheet."""
    leads = leads_index(
        [
            social_lead("thin", social=True, reviews=3),
            social_lead("rich", social=True, reviews=180),
            social_lead("mid", social=True, reviews=40),
        ]
    )
    selected, _, _ = research.select_leads(
        leads, set(), batch_size=3, order="reviews"
    )

    assert [r["Lead ID"] for r in selected] == ["rich", "mid", "thin"]


def test_the_campaign_ships_in_sheet_order():
    assert config().research.order == "sheet"


def test_an_unknown_order_is_refused_at_load_time(tmp_path):
    from outreach.config import ConfigError, load_config as load

    src = open(PENANG).read().replace(
        "  provider: anthropic", "  order: alphabetical\n  provider: anthropic", 1
    )
    path = tmp_path / "c.yaml"
    path.write_text(src)

    import pytest

    with pytest.raises(ConfigError, match="research.order must be one of"):
        load(path, env_file=None)


def test_leads_without_socials_are_not_dropped_only_deferred():
    leads = leads_index([social_lead("plain"), social_lead("fb", social=True)])
    selected, _, _ = research.select_leads(leads, set(), batch_size=10)

    assert {r["Lead ID"] for r in selected} == {"plain", "fb"}
    assert selected[0]["Lead ID"] == "fb"


def test_sheet_order_can_be_asked_for():
    leads = leads_index([social_lead("plain"), social_lead("fb", social=True)])
    selected, _, _ = research.select_leads(
        leads, set(), batch_size=10, social_first=False
    )
    assert [r["Lead ID"] for r in selected] == ["plain", "fb"]


def test_ordering_never_changes_which_leads_are_eligible():
    """Ranking is about spending the budget well, not about skipping anyone."""
    leads = leads_index(
        [
            social_lead("done", social=True),
            social_lead("fresh"),
            {**social_lead("gone"), "Stage": "Disqualified"},
        ]
    )
    ranked, already_a, not_ready_a = research.select_leads(leads, {"done"}, batch_size=10)
    plain, already_b, not_ready_b = research.select_leads(
        leads, {"done"}, batch_size=10, social_first=False
    )

    assert {r["Lead ID"] for r in ranked} == {r["Lead ID"] for r in plain} == {"fresh"}
    assert (already_a, not_ready_a) == (already_b, not_ready_b) == (1, 1)


def test_a_missing_review_count_does_not_crash_the_sort():
    leads = leads_index(
        [
            {**lead_row("no_count", website_status="social_only")},
            {**lead_row("bad", website_status="social_only"), "Review Count": "n/a"},
        ]
    )
    selected, _, _ = research.select_leads(leads, set(), batch_size=10)
    assert len(selected) == 2


def test_the_run_reports_how_many_of_the_batch_have_socials():
    leads = leads_index(
        [social_lead("fb", social=True), social_lead("plain")]
    )
    sheets = FakeSheets(leads)
    report = research.run(config(), FakePlaces(), sheets, FakeWriter(), limit=1)

    assert report.selected == 1
    assert report.selected_with_social == 1
    assert any("picked first" in line for line in report.summary_lines())


def test_the_campaign_ships_with_socials_first_on():
    assert config().research.social_first is True
