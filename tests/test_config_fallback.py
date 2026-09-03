"""A dead CAMPAIGN_CONFIG must not take the whole tool down.

Saving any setting from the dashboard writes CAMPAIGN_CONFIG into .env,
pointing at a *.local.yaml twin. If that file is later deleted, moved, or
never finished being written, every command loads config first and so every
command died -- drafting, research, sending, all of it, with an error about
config that gives no hint the dashboard caused it.
"""

import pytest

from outreach.config import ConfigError, load_config

TRACKED = "config/campaigns/penang-my.yaml"


def test_a_missing_target_falls_back_to_the_tracked_campaign(monkeypatch):
    monkeypatch.setenv("CAMPAIGN_CONFIG", "/no/such/campaign.local.yaml")

    config = load_config(env_file=None)

    assert config.campaign.name, "the pipeline must still be usable"


def test_the_fallback_says_what_happened(monkeypatch, caplog):
    """Silence here would mean settings quietly stop applying, which is
    worse than the crash: the tool works, just not as configured."""
    monkeypatch.setenv("CAMPAIGN_CONFIG", "/no/such/campaign.local.yaml")

    with caplog.at_level("WARNING"):
        load_config(env_file=None)

    message = caplog.text
    assert "/no/such/campaign.local.yaml" in message
    assert "not being applied" in message, "it must say settings are inactive"
    assert ".env" in message, "and where to fix it"


def test_an_explicit_config_path_is_never_second_guessed(monkeypatch):
    """--config is a direct instruction. Silently loading something else
    would be worse than failing, because the run would look successful."""
    monkeypatch.setenv("CAMPAIGN_CONFIG", TRACKED)

    with pytest.raises(ConfigError, match="not found"):
        load_config("/no/such/explicit.yaml", env_file=None)


def test_a_working_campaign_config_is_still_used(monkeypatch):
    """The fallback must not fire when the file is fine."""
    monkeypatch.setenv("CAMPAIGN_CONFIG", TRACKED)

    assert load_config(env_file=None).campaign.name == "penang-hair-salons"
