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

    def search_text(self, query, **kwargs):
        yield from self._results


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
    return load_config(PENANG, env_file=None)


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


def test_rerun_refreshes_rather_than_duplicating():
    sheets = FakeSheets()
    discovery.run(config(), FakePlaces([place("a", "Salon A")]), sheets)
    report = discovery.run(config(), FakePlaces([place("a", "Salon A Renamed")]), sheets)

    assert report.upsert.inserted == 0
    assert report.upsert.updated == 1
    assert sheets.existing["a"]["Business Name"] == "Salon A Renamed"


def test_rerun_does_not_regress_an_advanced_lead():
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
    discovery.run(config(), FakePlaces([place("a", "Salon A")]), sheets)

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
