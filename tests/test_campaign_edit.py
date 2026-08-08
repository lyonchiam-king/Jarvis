"""Editing the campaign target from the dashboard, without wrecking the file."""

from pathlib import Path

import pytest
import yaml

from outreach.campaign_edit import CampaignEditError, rewrite_campaign
from outreach.config import load_config

PENANG = Path("config/campaigns/penang-my.yaml")

SIMPLE = """\
# Why this campaign exists.
campaign:
  name: test
  # The thing we search for.
  category: "hair salon"
  location: "Georgetown, Penang, Malaysia"
  language: en
  # Areas are a finite well -- see the README.
  areas:
    - "Georgetown, Penang, Malaysia"
    - "Bayan Lepas, Penang, Malaysia"

limits:
  batch_size: 60

compliance:
  country_code: MY
"""


def parsed(text):
    return yaml.safe_load(text)["campaign"]


# ------------------------------------------------------------- the edits


def test_category_is_replaced():
    out = rewrite_campaign(SIMPLE, category="barber shop")
    assert parsed(out)["category"] == "barber shop"


def test_areas_are_replaced_wholesale():
    out = rewrite_campaign(SIMPLE, areas=["Ipoh, Perak", "Taiping, Perak"])
    assert parsed(out)["areas"] == ["Ipoh, Perak", "Taiping, Perak"]


def test_location_is_replaced():
    out = rewrite_campaign(SIMPLE, location="Ipoh, Perak, Malaysia")
    assert parsed(out)["location"] == "Ipoh, Perak, Malaysia"


def test_everything_can_change_at_once():
    out = rewrite_campaign(
        SIMPLE, category="cafe", location="Bangsar, KL", areas=["Bangsar, KL"]
    )
    c = parsed(out)
    assert (c["category"], c["location"], c["areas"]) == ("cafe", "Bangsar, KL", ["Bangsar, KL"])


# ------------------------------------------------- the comments must survive


def test_comments_survive_an_edit():
    """The comments are the documentation the user actually reads."""
    out = rewrite_campaign(SIMPLE, category="cafe", areas=["Ipoh"])

    assert "# Why this campaign exists." in out
    assert "# The thing we search for." in out
    assert "# Areas are a finite well -- see the README." in out


def test_untouched_sections_are_byte_identical():
    out = rewrite_campaign(SIMPLE, category="cafe")
    assert "limits:\n  batch_size: 60" in out
    assert "compliance:\n  country_code: MY" in out


def test_the_real_campaign_file_survives_a_round_trip():
    """The file that actually ships, with all eighteen areas and its prose."""
    original = PENANG.read_text()
    out = rewrite_campaign(original, category="barber shop")

    assert out.count("#") == original.count("#"), "a comment went missing"
    after = yaml.safe_load(out)
    before = yaml.safe_load(original)
    before["campaign"]["category"] = "barber shop"
    assert after == before, "only the category should have changed"


def test_rewriting_with_no_changes_is_a_no_op():
    assert rewrite_campaign(SIMPLE) == SIMPLE


def test_the_result_still_loads_as_a_config(tmp_path):
    out = rewrite_campaign(PENANG.read_text(), category="cafe", areas=["Ipoh", "Taiping"])
    path = tmp_path / "edited.yaml"
    path.write_text(out)

    cfg = load_config(path, env_file=None)
    assert cfg.campaign.category == "cafe"
    assert cfg.campaign.areas == ("Ipoh", "Taiping")
    assert cfg.campaign.search_queries == ("cafe in Ipoh", "cafe in Taiping")


# --------------------------------------------------------------- refusals


def test_an_empty_category_is_refused():
    with pytest.raises(CampaignEditError, match="Category cannot be empty"):
        rewrite_campaign(SIMPLE, category="   ")


def test_an_empty_location_is_refused():
    with pytest.raises(CampaignEditError, match="Location cannot be empty"):
        rewrite_campaign(SIMPLE, location="")


def test_duplicate_areas_are_refused():
    with pytest.raises(CampaignEditError, match="Duplicate area"):
        rewrite_campaign(SIMPLE, areas=["Ipoh", "Taiping", "Ipoh"])


def test_a_file_with_no_campaign_section_is_refused():
    with pytest.raises(CampaignEditError, match="no top-level"):
        rewrite_campaign("limits:\n  batch_size: 60\n", category="cafe")


# ------------------------------------------------------------ awkward input


def test_a_value_with_quotes_survives():
    out = rewrite_campaign(SIMPLE, category='hair "salon" & spa')
    assert parsed(out)["category"] == 'hair "salon" & spa'


def test_a_value_with_a_colon_survives():
    """Unquoted, this would parse as a nested mapping and corrupt the file."""
    out = rewrite_campaign(SIMPLE, location="Penang: the island")
    assert parsed(out)["location"] == "Penang: the island"


def test_blank_lines_in_the_area_list_are_dropped():
    out = rewrite_campaign(SIMPLE, areas=["Ipoh", "  ", "", "Taiping"])
    assert parsed(out)["areas"] == ["Ipoh", "Taiping"]


def test_clearing_every_area_falls_back_to_the_single_location(tmp_path):
    out = rewrite_campaign(SIMPLE, areas=[])
    assert "areas" not in parsed(out)

    path = tmp_path / "c.yaml"
    path.write_text(out + "\ndiscovery:\n  max_results: 60\n")
    cfg = load_config(path, env_file=None)
    assert cfg.campaign.search_queries == ("hair salon in Georgetown, Penang, Malaysia",)


def test_areas_can_be_added_to_a_file_that_has_none():
    no_areas = SIMPLE.replace(
        '  # Areas are a finite well -- see the README.\n'
        '  areas:\n'
        '    - "Georgetown, Penang, Malaysia"\n'
        '    - "Bayan Lepas, Penang, Malaysia"\n',
        "",
    )
    assert "areas" not in yaml.safe_load(no_areas)["campaign"]

    out = rewrite_campaign(no_areas, areas=["Ipoh", "Taiping"])
    assert parsed(out)["areas"] == ["Ipoh", "Taiping"]
    assert parsed(out)["language"] == "en", "the rest of the block is intact"


def test_the_next_section_is_never_swallowed():
    out = rewrite_campaign(SIMPLE, areas=["Ipoh"])
    assert yaml.safe_load(out)["limits"]["batch_size"] == 60
    assert yaml.safe_load(out)["compliance"]["country_code"] == "MY"
