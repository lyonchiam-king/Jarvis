"""Stage 1 filtering and write behaviour, with Places and Sheets faked out."""

from outreach import schema
from outreach.config import load_config
from outreach.providers.places import PlaceResult
from outreach.sheets import UpsertResult
from outreach.stages import discovery

PENANG = "config/campaigns/penang-my.yaml"


class FakePlaces:
    def __init__(self, results):
        self._results = results
        self.request_count = 1
        self.queries = []

    def search_text(self, query, **kwargs):
        self.queries.append(query)
        yield from self._results


class FakePlacesByArea(FakePlaces):
    """Different results per search query, the way real areas behave."""

    def __init__(self, by_query):
        super().__init__([])
        self._by_query = by_query

    def search_text(self, query, **kwargs):
        self.queries.append(query)
        yield from self._by_query.get(query, [])


class FakeSheets:
    """Records what discovery tried to write."""

    def __init__(self, existing=None):
        self.existing = existing or {}
        self.upserted = []
        self.appended = []
        self.spreadsheet_id = "fake"

    def upsert(self, tab, rows, *, on_conflict=None):
        self.upserted.append((tab, rows))
        result = UpsertResult()
        for row in rows:
            key = row["Lead ID"]
            if key in self.existing:
                merged = on_conflict(self.existing[key], row) if on_conflict else row
                if merged is None:
                    result.skipped += 1
                    continue
                self.existing[key] = {**self.existing[key], **{k: str(v) for k, v in merged.items()}}
                result.updated += 1
            else:
                self.existing[key] = {k: str(v) for k, v in row.items()}
                result.inserted += 1
                result.inserted_names.append(str(row.get("Business Name", "")))
        return result

    def append(self, tab, rows):
        self.appended.append((tab, rows))
        return len(rows)

    def read_rows(self, tab):
        if tab is not schema.LEADS:
            return {}
        return {k: (i + 2, v) for i, (k, v) in enumerate(self.existing.items())}


def place(pid, name="Salon", website="", status="OPERATIONAL", rating=4.5, reviews=10):
    return PlaceResult(
        place_id=pid,
        name=name,
        address="1 Test St, Georgetown",
        phone="04-226 1234",
        website_uri=website,
        rating=rating,
        review_count=reviews,
        category="Hair salon",
        maps_url="https://maps.google.com/?cid=1",
        business_status=status,
    )


def config():
    """One search area, pinned.

    The campaign file lists eighteen areas and is meant to keep growing. A
    filtering test that inherited them would search all eighteen and drown in
    duplicates, so the base helper searches the single fallback location and
    ``cfg_with(areas=...)`` opts in.
    """
    from dataclasses import replace

    cfg = load_config(PENANG, env_file=None)
    return replace(cfg, campaign=replace(cfg.campaign, areas=()))


def test_keeps_no_website_and_social_only_drops_real_sites():
    places = FakePlaces(
        [
            place("no-site", "No Site Salon", website=""),
            place("fb-only", "Facebook Salon", website="https://facebook.com/x"),
            place("real", "Real Site Salon", website="https://realsalon.com.my"),
        ]
    )
    sheets = FakeSheets()
    report = discovery.run(config(), places, sheets)

    assert report.qualified == 2
    assert report.rejected_has_website == 1
    written = {r["Lead ID"] for r in sheets.upserted[0][1]}
    assert written == {"no-site", "fb-only"}


def test_social_only_can_be_excluded_by_config(tmp_path):
    cfg = config()
    cfg = type(cfg)(
        campaign=cfg.campaign,
        discovery=type(cfg.discovery)(
            **{**cfg.discovery.__dict__, "treat_social_as_no_website": False}
        ),
        outreach=cfg.outreach,
        lovable=cfg.lovable,
        compliance=cfg.compliance,
        secrets=cfg.secrets,
    )
    places = FakePlaces([place("fb-only", website="https://facebook.com/x")])
    sheets = FakeSheets()
    report = discovery.run(cfg, places, sheets)

    assert report.qualified == 0
    assert report.rejected_has_website == 1


def test_filters_closed_businesses_and_low_review_counts():
    places = FakePlaces(
        [
            place("closed", status="CLOSED_PERMANENTLY"),
            place("thin", reviews=1),          # min_review_count is 3
            place("good", reviews=9),
        ]
    )
    sheets = FakeSheets()
    report = discovery.run(config(), places, sheets)

    assert report.rejected_status == 1
    assert report.rejected_reviews == 1
    assert report.qualified == 1


def test_duplicate_place_ids_within_one_run_are_collapsed():
    places = FakePlaces([place("same"), place("same"), place("other")])
    sheets = FakeSheets()
    report = discovery.run(config(), places, sheets)

    assert report.duplicates_in_run == 1
    assert report.qualified == 2


def test_refresh_updates_in_place_rather_than_duplicating():
    sheets = FakeSheets()
    discovery.run(config(), FakePlaces([place("a", "Salon A")]), sheets)
    report = discovery.run(
        config(), FakePlaces([place("a", "Salon A Renamed")]), sheets, refresh=True
    )

    assert report.upsert.inserted == 0
    assert report.upsert.updated == 1
    assert sheets.existing["a"]["Business Name"] == "Salon A Renamed"


def test_refresh_does_not_regress_an_advanced_lead():
    sheets = FakeSheets(
        existing={
            "a": {
                "Lead ID": "a",
                "Stage": "Email Drafted",
                "Business Name": "Salon A",
                "Discovered At": "2026-01-01 00:00:00Z",
            }
        }
    )
    discovery.run(config(), FakePlaces([place("a", "Salon A")]), sheets, refresh=True)

    assert sheets.existing["a"]["Stage"] == "Email Drafted"
    # First-seen date survives a refresh.
    assert sheets.existing["a"]["Discovered At"] == "2026-01-01 00:00:00Z"


def test_max_results_caps_the_run():
    cfg = config()
    many = [place(f"p{i}") for i in range(200)]
    sheets = FakeSheets()
    report = discovery.run(cfg, FakePlaces(many), sheets)

    assert report.qualified == cfg.discovery.max_results


def test_a_failing_lead_is_logged_and_the_run_continues():
    bad = place("bad")
    good = place("good")

    class BrokenPlaces(FakePlaces):
        def search_text(self, query, **kwargs):
            yield bad
            yield good

    # Force a failure on the first lead only.
    original = discovery.classify_website
    calls = {"n": 0}

    def flaky(url, domains):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("social lookup exploded")
        return original(url, domains)

    discovery.classify_website = flaky
    try:
        sheets = FakeSheets()
        report = discovery.run(config(), BrokenPlaces([]), sheets)
    finally:
        discovery.classify_website = original

    assert report.errors == 1
    assert report.qualified == 1
    assert sheets.appended and sheets.appended[0][0] is schema.ERRORS


def test_dry_run_writes_nothing():
    sheets = FakeSheets()
    report = discovery.run(config(), FakePlaces([place("a")]), None, dry_run=True)

    assert report.qualified == 1
    assert sheets.upserted == []


# ------------------------------------- "another sixty tomorrow, no repeats"


def cfg_with(*, areas=(), cap=None):
    """Campaign config with the search areas and batch cap pinned."""
    from dataclasses import replace

    c = config()
    c = replace(c, campaign=replace(c.campaign, areas=tuple(areas)))
    if cap is not None:
        c = replace(
            c,
            limits=replace(c.limits, batch_size=cap),
            discovery=replace(c.discovery, max_results=cap),
        )
    return c


def test_a_second_run_skips_everything_it_already_found():
    """The core daily promise: today's batch is never yesterday's batch."""
    sheets = FakeSheets()
    day_one = discovery.run(cfg_with(cap=2), FakePlaces([place("a"), place("b")]), sheets)
    assert day_one.qualified == 2

    day_two = discovery.run(
        cfg_with(cap=2), FakePlaces([place("a"), place("b")]), sheets
    )

    assert day_two.qualified == 0
    assert day_two.already_known == 2
    assert day_two.upsert.inserted == 0
    assert day_two.upsert.updated == 0, "a known lead is not even rewritten"


def test_known_leads_do_not_use_up_the_batch():
    """Otherwise a run would return 2 new leads instead of the 3 you asked for."""
    sheets = FakeSheets()
    discovery.run(cfg_with(cap=3), FakePlaces([place("a"), place("b")]), sheets)

    report = discovery.run(
        cfg_with(cap=3),
        FakePlaces([place("a"), place("b"), place("c"), place("d"), place("e")]),
        sheets,
    )

    assert report.already_known == 2
    assert report.qualified == 3
    assert {r["Lead ID"] for r in sheets.upserted[-1][1]} == {"c", "d", "e"}


def test_a_lead_you_already_contacted_is_never_touched_again():
    sheets = FakeSheets(
        existing={"a": {"Lead ID": "a", "Stage": "Sent", "Business Name": "Salon A"}}
    )
    report = discovery.run(cfg_with(cap=5), FakePlaces([place("a")]), sheets)

    assert report.already_known == 1
    assert sheets.upserted == [], "nothing written at all"
    assert sheets.existing["a"]["Stage"] == "Sent"


def test_areas_are_walked_in_order_until_the_batch_is_full():
    """One Google query is a finite well, so a campaign lists several areas."""
    cfg = cfg_with(areas=["Georgetown", "Bayan Lepas", "Butterworth"], cap=3)
    places = FakePlacesByArea(
        {
            "hair salon in Georgetown": [place("g1"), place("g2")],
            "hair salon in Bayan Lepas": [place("b1"), place("b2")],
            "hair salon in Butterworth": [place("t1")],
        }
    )
    report = discovery.run(cfg, places, FakeSheets())

    assert report.qualified == 3
    assert report.areas_searched == 2, "stops as soon as the cap is full"
    assert places.queries == ["hair salon in Georgetown", "hair salon in Bayan Lepas"]


def test_the_next_day_moves_on_to_the_next_area():
    cfg = cfg_with(areas=["Georgetown", "Bayan Lepas"], cap=2)
    by_area = {
        "hair salon in Georgetown": [place("g1"), place("g2")],
        "hair salon in Bayan Lepas": [place("b1"), place("b2")],
    }
    sheets = FakeSheets()

    discovery.run(cfg, FakePlacesByArea(by_area), sheets)
    day_two = discovery.run(cfg, FakePlacesByArea(by_area), sheets)

    assert day_two.qualified == 2
    assert {r["Lead ID"] for r in sheets.upserted[-1][1]} == {"b1", "b2"}
    assert set(sheets.existing) == {"g1", "g2", "b1", "b2"}


def test_running_out_of_areas_says_so_instead_of_going_quiet():
    cfg = cfg_with(areas=["Georgetown"], cap=10)
    sheets = FakeSheets()
    discovery.run(cfg, FakePlacesByArea({"hair salon in Georgetown": [place("g1")]}), sheets)

    report = discovery.run(
        cfg, FakePlacesByArea({"hair salon in Georgetown": [place("g1")]}), sheets
    )

    assert report.qualified == 0
    assert report.exhausted is True
    advice = " ".join(report.summary_lines())
    assert "add" in advice and "areas" in advice


def test_a_full_batch_is_not_reported_as_exhausted():
    cfg = cfg_with(areas=["Georgetown"], cap=2)
    report = discovery.run(
        cfg,
        FakePlacesByArea({"hair salon in Georgetown": [place("g1"), place("g2")]}),
        FakeSheets(),
    )

    assert report.qualified == 2
    assert report.exhausted is False


def test_no_areas_configured_still_searches_the_single_location():
    cfg = cfg_with(cap=5)
    places = FakePlaces([place("a")])
    discovery.run(cfg, places, FakeSheets())

    assert places.queries == ["hair salon in Georgetown, Penang, Malaysia"]


def test_an_unreadable_leads_tab_does_not_stop_the_run():
    """Re-reporting a lead is survivable; refusing to run is not."""

    class Unreadable(FakeSheets):
        def read_rows(self, tab):
            raise RuntimeError("Sheets hiccup")

    report = discovery.run(cfg_with(cap=5), FakePlaces([place("a")]), Unreadable())
    assert report.qualified == 1


def test_one_bad_area_does_not_abort_the_others():
    class HalfBroken(FakePlacesByArea):
        def search_text(self, query, **kwargs):
            self.queries.append(query)
            if query.endswith("Bayan Lepas"):
                raise RuntimeError("Places said no")
            yield from self._by_query.get(query, [])

    cfg = cfg_with(areas=["Bayan Lepas", "Georgetown"], cap=5)
    places = HalfBroken({"hair salon in Georgetown": [place("g1")]})
    report = discovery.run(cfg, places, FakeSheets())

    assert report.qualified == 1
    assert report.errors == 1
