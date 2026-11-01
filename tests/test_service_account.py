"""The robot account that reads the Sheet.

A user's OAuth token is issued to a person and carries that person's limits:
a consent screen, an unverified-app warning, and -- on this Workspace -- a
lifetime measured in days. It died three times in three weeks, each time at
the moment Lyon sat down to work.

A service account has none of that. It is an account in its own right, it
refreshes itself from a key on disk, and nothing about it needs publishing,
verifying or signing in. What it cannot do is act *as* a person, so Gmail
drafts stay on the OAuth token -- and a dead OAuth token must then cost the
email drafts and nothing else.

Nothing here reaches Google. What is tested is which credential is chosen,
what is said when it cannot see the Sheet, and that the private key never
leaves the file.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from outreach import auth
from outreach.config import load_config

PENANG = "config/campaigns/penang-my.yaml"

KEY = {
    "type": "service_account",
    "project_id": "sme-outreach-504919",
    "private_key_id": "abc123",
    "private_key": "-----BEGIN PRIVATE KEY-----\nNEVER-PRINT-THIS\n-----END PRIVATE KEY-----\n",
    "client_email": "jarvis-sheets@sme-outreach-504919.iam.gserviceaccount.com",
    "client_id": "109876543210",
    "token_uri": "https://oauth2.googleapis.com/token",
}


@pytest.fixture
def key_file(tmp_path) -> Path:
    path = tmp_path / "service_account.json"
    path.write_text(json.dumps(KEY))
    return path


# ------------------------------------------------- which account is chosen


def test_the_file_being_there_is_the_whole_switch(key_file):
    """No setting to forget, and no second place to look. Drop the key in
    and it is used; take it out and the OAuth token is used exactly as
    before, which is what keeps every existing install working."""
    assert auth.uses_service_account(key_file) is True
    assert auth.uses_service_account(key_file.parent / "absent.json") is False
    assert auth.uses_service_account(None) is False


def test_the_sheet_prefers_the_account_that_cannot_expire(key_file, monkeypatch):
    chosen = {}

    def robot(path, scopes=None):
        chosen["robot"] = path
        return "ROBOT"

    def oauth(*a, **k):
        chosen["oauth"] = True
        return "OAUTH"

    monkeypatch.setattr(auth, "service_account_credentials", robot)
    monkeypatch.setattr(auth, "get_credentials", oauth)

    got = auth.sheets_credentials(Path("client.json"), Path("token.json"), key_file)
    assert got == "ROBOT"
    assert "oauth" not in chosen, "the OAuth token was touched when a key exists"


def test_without_a_key_nothing_changes(monkeypatch, tmp_path):
    """The control. Every install that has no service account must take the
    path it always took."""
    monkeypatch.setattr(
        auth, "service_account_credentials",
        lambda *a, **k: pytest.fail("a service account was built from nothing"),
    )
    monkeypatch.setattr(auth, "get_credentials", lambda *a, **k: "OAUTH")

    got = auth.sheets_credentials(
        Path("client.json"), Path("token.json"), tmp_path / "absent.json"
    )
    assert got == "OAUTH"


# --------------------------------------------------------- what it reveals


def test_the_address_is_readable_and_the_key_is_not(key_file):
    """The address has to be shown -- it is what the Sheet is shared with.
    The private key sits in the same file and is never read by anything but
    Google's own library."""
    email = auth.service_account_email(key_file)
    assert email == KEY["client_email"]
    assert "PRIVATE KEY" not in email
    assert "NEVER-PRINT-THIS" not in email


def test_an_unreadable_key_is_unknown_rather_than_fatal(tmp_path):
    assert auth.service_account_email(tmp_path / "absent.json") == ""
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert auth.service_account_email(broken) == ""


def test_a_malformed_key_says_what_to_replace(tmp_path):
    broken = tmp_path / "service_account.json"
    broken.write_text("this is not the file Google gave you")
    with pytest.raises(auth.ServiceAccountError) as exc:
        auth.service_account_credentials(broken)
    assert "saved whole and unedited" in str(exc.value)


def test_it_never_asks_for_gmail(key_file):
    """A service account cannot write a draft in a person's mailbox without
    domain-wide delegation, so asking for the scope would buy an
    authorisation that still fails at the call -- a worse failure than not
    having it."""
    assert not any("gmail" in scope for scope in auth.SHEETS_SCOPES)
    assert any("spreadsheets" in scope for scope in auth.SHEETS_SCOPES)


# ------------------------------------------- the Sheet nobody shared with it


def test_a_permission_error_becomes_a_share_instruction():
    """The commonest failure right after installing a key, and Google
    reports it as a flat permission error that reads like a broken key."""
    from outreach.sheets import explain

    denied = Exception("The caller does not have permission")
    message = explain(
        denied,
        robot_email=KEY["client_email"],
        sheet_id="17Rp2iMaPKoIHgWEHbdp7brMcGOVSSkql9",
    )
    assert "Press Share" in message
    assert KEY["client_email"] in message
    assert "17Rp2iMaPKoIHgWEHbdp7brMcGOVSSkql9" in message
    assert "Nothing is wrong with the key" in message


def test_the_same_error_is_left_alone_without_a_robot():
    """On an OAuth install a permission error means something else
    entirely, and telling someone to share a Sheet with an account that does
    not exist is an hour wasted."""
    from outreach.sheets import explain

    denied = Exception("The caller does not have permission")
    assert "Press Share" not in explain(denied)


def test_a_disabled_api_is_not_relabelled_as_a_sharing_problem():
    """Two different 403s. Enabling an API and sharing a Sheet are not
    interchangeable repairs."""
    from outreach.sheets import ApiNotEnabled, explain

    message = explain(
        ApiNotEnabled("switched off, enable these three"),
        robot_email=KEY["client_email"],
    )
    assert "switched off" in message
    assert "Press Share" not in message


# -------------------------------------------------- what a dead sign-in costs


def test_a_dead_sign_in_costs_the_emails_and_not_the_run(key_file, monkeypatch, capsys):
    """The whole point of installing a key is that the sign-in keeps dying.
    If that still killed the drafting run, the fix would have bought
    nothing: WhatsApp copy is written into the Sheet and needs no Google
    identity at all."""
    from outreach import cli

    config = load_config(PENANG, env_file=None)
    config = replace(
        config, secrets=replace(config.secrets, service_account_file=key_file)
    )

    monkeypatch.setattr(
        cli, "get_credentials",
        lambda *a, **k: (_ for _ in ()).throw(auth.AuthError("expired")),
        raising=False,
    )
    monkeypatch.setattr(
        auth, "get_credentials",
        lambda *a, **k: (_ for _ in ()).throw(auth.AuthError("expired")),
    )

    gmail = cli._gmail_or_none(config)
    assert gmail is None, "it should carry on without Gmail"

    said = capsys.readouterr().out
    assert "No Gmail drafts this run" in said
    assert "WhatsApp copy is unaffected" in said


def test_without_a_key_a_dead_sign_in_still_stops_the_run(monkeypatch, tmp_path):
    """The control. On an OAuth install the sign-in is also what reads the
    Sheet, so there is nothing left to degrade to and carrying on would
    produce a run that silently did nothing."""
    import click

    from outreach import cli

    config = load_config(PENANG, env_file=None)
    config = replace(
        config,
        secrets=replace(config.secrets, service_account_file=tmp_path / "absent.json"),
    )
    monkeypatch.setattr(
        auth, "get_credentials",
        lambda *a, **k: (_ for _ in ()).throw(auth.AuthError("expired")),
    )
    with pytest.raises(click.ClickException):
        cli._gmail_or_none(config)
