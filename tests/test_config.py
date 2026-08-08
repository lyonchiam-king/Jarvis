import pytest

from outreach.config import ConfigError, load_config

PENANG = "config/campaigns/penang-my.yaml"
UK = "config/campaigns/example-manchester-uk.yaml"


def test_loads_penang_campaign(monkeypatch):
    monkeypatch.setenv("PLACES_API_KEY", "test-key")
    monkeypatch.setenv("SHEET_ID", "sheet-123")
    config = load_config(PENANG, env_file=None)

    assert config.campaign.name == "penang-hair-salons"
    assert config.campaign.search_query == "hair salon in Georgetown, Penang, Malaysia"
    assert config.compliance.country_code == "MY"
    assert config.secrets.places_api_key == "test-key"
    assert config.secrets.sheet_id == "sheet-123"
    assert "facebook.com" in config.discovery.social_domains


def test_region_swap_needs_no_code_change():
    uk = load_config(UK, env_file=None)
    assert uk.compliance.country_code == "GB"
    assert uk.campaign.search_query == "barber shop in Manchester, United Kingdom"


def test_secrets_never_come_from_yaml(monkeypatch):
    monkeypatch.delenv("PLACES_API_KEY", raising=False)
    config = load_config(PENANG, env_file=None)
    assert config.secrets.places_api_key == ""


def test_rejects_page_size_above_places_cap(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(
        "campaign:\n"
        "  name: x\n  category: y\n  location: z\n"
        "discovery:\n  page_size: 50\n"
        "compliance:\n  country_code: MY\n"
    )
    with pytest.raises(ConfigError, match="page_size"):
        load_config(path, env_file=None)


def test_country_code_is_required(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("campaign:\n  name: x\n  category: y\n  location: z\n")
    with pytest.raises(ConfigError, match="country_code"):
        load_config(path, env_file=None)


def test_missing_campaign_field_names_the_file(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("campaign:\n  name: x\ncompliance:\n  country_code: MY\n")
    with pytest.raises(ConfigError, match="category"):
        load_config(path, env_file=None)
