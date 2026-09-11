"""Everything this tool writes, and where a server is allowed to put it.

On Lyon's machine the answer is "beside the code", and that is right for a
tool you start by double-clicking a file in the folder. It is wrong for a
server: a container's copy of the code is read-only and replaced on every
deploy, so anything written into it is either refused or silently thrown
away at the next release.

The two things that would be thrown away are the Google token -- losing it
means signing in again with a browser, which a server cannot do -- and every
key saved from the dashboard.
"""
from __future__ import annotations

import os
import pathlib

import pytest

from outreach import paths


@pytest.fixture
def hosted(tmp_path, monkeypatch):
    """A state directory somewhere else entirely, as a deploy would have."""
    monkeypatch.setenv(paths.ENV_VAR, str(tmp_path))
    return tmp_path


# ----------------------------------------------------------- the default


def test_setting_nothing_reproduces_todays_layout(monkeypatch):
    """Nobody's existing install may move.

    Lyon's .env, credentials and campaign twin are already on disk where
    they are; a default that relocated them would look like the tool losing
    his keys.
    """
    monkeypatch.delenv(paths.ENV_VAR, raising=False)
    here = pathlib.Path.cwd()

    assert paths.state_dir() == here
    assert paths.env_file() == here / ".env"
    assert paths.credentials_dir() == here / "credentials"
    assert paths.is_hosted() is False


def test_the_state_directory_is_read_each_time_not_frozen_at_import(monkeypatch, tmp_path):
    """The dashboard and the tests both change the working directory.

    A module-level constant would freeze whichever ran first.
    """
    monkeypatch.delenv(paths.ENV_VAR, raising=False)
    monkeypatch.chdir(tmp_path)
    assert paths.state_dir() == tmp_path


def test_a_blank_variable_is_not_a_state_directory(monkeypatch):
    """An unset variable and one set to "" mean the same thing.

    Docker passes through empty strings for variables nobody filled in, and
    treating "" as a path would put the token in the filesystem root.
    """
    monkeypatch.setenv(paths.ENV_VAR, "   ")
    assert paths.state_dir() == pathlib.Path.cwd()
    assert paths.is_hosted() is False


# -------------------------------------------------------------- when hosted


def test_every_writable_path_moves_together(hosted):
    assert paths.env_file() == hosted / ".env"
    assert paths.credentials_dir() == hosted / "credentials"
    assert paths.cache_dir() == hosted / ".cache"
    assert paths.campaign_dir() == hosted / "campaigns"
    assert paths.is_hosted() is True


def test_the_google_token_lands_in_the_state_directory(hosted):
    """The one file a deploy must never erase."""
    from outreach.config import load_config

    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    assert config.secrets.token_file == hosted / "credentials" / "token.json"
    assert config.secrets.oauth_client_file == hosted / "credentials" / "oauth_client.json"


def test_an_explicit_token_path_still_wins(hosted, monkeypatch):
    """A host that mounts its secret somewhere specific must be obeyed."""
    from outreach.config import load_config

    monkeypatch.setenv("GOOGLE_TOKEN_FILE", "/run/secrets/token.json")
    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    assert str(config.secrets.token_file) == "/run/secrets/token.json"


def test_saved_settings_do_not_land_in_the_read_only_repo(hosted):
    """The campaign twin is the file every dashboard setting is written to.

    Beside the template it would be inside the image -- refused on a
    read-only layer, or wiped by the next deploy along with every setting.
    """
    from outreach.campaign_edit import local_path_for

    template = pathlib.Path("config/campaigns/penang-my.yaml")
    local = local_path_for(template)

    assert local.parent == hosted / "campaigns"
    assert "config/campaigns" not in str(local)


def test_forking_creates_the_directory_it_needs(hosted):
    """A mounted volume is empty on its first boot."""
    from outreach.campaign_edit import fork_to_local

    local = fork_to_local(pathlib.Path("config/campaigns/penang-my.yaml"))

    assert local.exists()
    assert local.read_text() == pathlib.Path("config/campaigns/penang-my.yaml").read_text()


def test_forking_twice_never_overwrites_what_was_saved(hosted):
    from outreach.campaign_edit import fork_to_local

    template = pathlib.Path("config/campaigns/penang-my.yaml")
    first = fork_to_local(template)
    first.write_text("category: something the user chose\n")

    assert fork_to_local(template) == first
    assert first.read_text() == "category: something the user chose\n"


# ------------------------------------------------- nothing writes to the code


def test_no_module_still_hardcodes_a_write_into_the_working_copy():
    """The seam this whole change exists to close.

    One missed `Path(".env")` would put a key in the image, where it is
    readable by anyone who pulls it and gone at the next deploy -- and
    nothing would fail loudly enough to notice.
    """
    offenders = []
    for source in pathlib.Path("src/outreach").rglob("*.py"):
        for number, line in enumerate(source.read_text().splitlines(), start=1):
            code = line.split("#", 1)[0]
            if 'Path(".env")' in code or 'Path(".cache' in code:
                offenders.append(f"{source}:{number}")
    assert not offenders, f"these write into the code directory: {offenders}"


def test_the_check_above_can_actually_fail():
    """A negative assertion with a broken pattern reports clean for ever."""
    control = 'set_key(Path(".env"), "K", v)  # not a comment'
    code = control.split("#", 1)[0]
    assert 'Path(".env")' in code
