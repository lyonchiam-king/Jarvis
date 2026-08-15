"""Editing the campaign target from the dashboard, without wrecking the file."""

from pathlib import Path

import pytest
import yaml

from outreach.campaign_edit import CampaignEditError, rewrite_campaign, rewrite_outreach
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


# ------------------------------------------------------- the batch-size cap


WITH_DISCOVERY = SIMPLE + """
discovery:
  max_results: 60
  page_size: 20
"""


def test_batch_size_is_replaced():
    out = rewrite_campaign(WITH_DISCOVERY, batch_size=25)
    assert yaml.safe_load(out)["limits"]["batch_size"] == 25


def test_discovery_max_results_moves_with_it():
    """The two are ANDed at runtime, so raising one alone would do nothing."""
    out = rewrite_campaign(WITH_DISCOVERY, batch_size=120)
    loaded = yaml.safe_load(out)

    assert loaded["limits"]["batch_size"] == 120
    assert loaded["discovery"]["max_results"] == 120


def test_a_raised_cap_actually_raises_the_effective_cap(tmp_path):
    out = rewrite_campaign(PENANG.read_text(), batch_size=120)
    path = tmp_path / "c.yaml"
    path.write_text(out)

    cfg = load_config(path, env_file=None)
    assert cfg.batch_size == 120
    assert cfg.discovery_cap == 120, "discovery would otherwise still stop at 60"


def test_batch_size_is_written_unquoted_so_it_stays_a_number():
    out = rewrite_campaign(WITH_DISCOVERY, batch_size=25)
    assert "batch_size: 25" in out
    assert '"25"' not in out


def test_other_discovery_settings_are_untouched():
    out = rewrite_campaign(WITH_DISCOVERY, batch_size=25)
    assert yaml.safe_load(out)["discovery"]["page_size"] == 20


def test_zero_and_negative_batch_sizes_are_refused():
    for bad in (0, -5):
        with pytest.raises(CampaignEditError, match="at least 1"):
            rewrite_campaign(WITH_DISCOVERY, batch_size=bad)


def test_an_absurd_batch_size_is_refused():
    with pytest.raises(CampaignEditError, match="ceiling"):
        rewrite_campaign(WITH_DISCOVERY, batch_size=5000)


def test_a_file_without_a_limits_section_says_so():
    no_limits = SIMPLE.replace("limits:\n  batch_size: 60\n", "")
    with pytest.raises(CampaignEditError, match="no top-level `limits:`"):
        rewrite_campaign(no_limits, batch_size=25)


def test_batch_size_and_target_can_change_together():
    out = rewrite_campaign(
        WITH_DISCOVERY, category="cafe", areas=["Ipoh"], batch_size=30
    )
    loaded = yaml.safe_load(out)

    assert loaded["campaign"]["category"] == "cafe"
    assert loaded["campaign"]["areas"] == ["Ipoh"]
    assert loaded["limits"]["batch_size"] == 30


# ------------------------------------------------------- the outreach model


OUTREACH = """\
campaign:
  name: test
  category: "hair salon"
  location: "Georgetown, Penang, Malaysia"

outreach:
  sender_name: "Yiu Ming"
  require_site: true

compliance:
  country_code: MY
"""


def test_outreach_model_is_set():
    out = rewrite_outreach(OUTREACH, model="qwen3:cloud")
    assert yaml.safe_load(out)["outreach"]["model"] == "qwen3:cloud"


def test_outreach_model_is_added_when_absent():
    out = rewrite_outreach(OUTREACH, model="qwen3:cloud")
    assert 'model: "qwen3:cloud"' in out


def test_clearing_the_outreach_model_removes_the_line():
    with_model = rewrite_outreach(OUTREACH, model="qwen3:cloud")
    out = rewrite_outreach(with_model, model="")
    assert "model" not in yaml.safe_load(out)["outreach"]


def test_clearing_an_absent_outreach_model_is_a_no_op():
    out = rewrite_outreach(OUTREACH, model="")
    assert yaml.safe_load(out)["outreach"] == yaml.safe_load(OUTREACH)["outreach"]


def test_the_outreach_model_round_trips_through_the_real_file(tmp_path):
    out = rewrite_outreach(PENANG.read_text(), model="qwen3:cloud")
    path = tmp_path / "c.yaml"
    path.write_text(out)

    cfg = load_config(path, env_file=None)
    assert cfg.outreach.model == "qwen3:cloud"


def test_a_file_with_no_outreach_section_says_so():
    with pytest.raises(CampaignEditError, match="no top-level `outreach:`"):
        rewrite_outreach(SIMPLE, model="qwen3:cloud")


# ------------------- settings live beside the template, not inside it


def test_a_shipped_file_forks_to_a_local_twin(tmp_path):
    from outreach.campaign_edit import fork_to_local

    path = tmp_path / "penang-my.yaml"
    path.write_text(SIMPLE)

    local = fork_to_local(path)

    assert local.name == "penang-my.local.yaml"
    assert local.read_text() == SIMPLE
    assert path.read_text() == SIMPLE, "the template is left alone"


def test_forking_twice_never_clobbers_your_settings(tmp_path):
    """The second Save must edit your file, not reset it to the template."""
    from outreach.campaign_edit import fork_to_local

    path = tmp_path / "penang-my.yaml"
    path.write_text(SIMPLE)
    local = fork_to_local(path)
    local.write_text(SIMPLE.replace("hair salon", "barber shop"))

    again = fork_to_local(path)

    assert again == local
    assert "barber shop" in again.read_text()


def test_an_already_local_file_is_returned_as_is(tmp_path):
    from outreach.campaign_edit import fork_to_local

    path = tmp_path / "penang-my.local.yaml"
    path.write_text(SIMPLE)
    assert fork_to_local(path) == path


def test_the_local_name_is_derived_predictably():
    from outreach.campaign_edit import local_path_for

    assert local_path_for(Path("a/b/kl-cafes.yaml")).name == "kl-cafes.local.yaml"
    assert local_path_for(Path("a/b/kl.local.yaml")).name == "kl.local.yaml"


def test_local_campaign_files_are_gitignored():
    """Otherwise the next `git pull` collides with the user's own settings."""
    ignored = Path(".gitignore").read_text()
    assert "config/campaigns/*.local.yaml" in ignored


def test_the_shipped_templates_are_still_tracked():
    """They are the starting point, and they are worth versioning."""
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files", "config/campaigns/"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "penang-my.yaml" in tracked
    assert ".local.yaml" not in tracked
