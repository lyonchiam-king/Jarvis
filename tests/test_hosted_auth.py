"""Signing in to Google from a machine with no browser.

Consent means `run_local_server`: it opens a browser and blocks until
somebody finishes. On a server there is no browser and nobody watching, so
reaching that code is not a slow path -- it is a request that fails with an
error about a display, or hangs until something kills it. Neither says what
is actually wrong, and what is actually wrong has a one-line fix.

A token that already exists refreshes over HTTP with no browser at all, which
is why seeding one by hand is the whole of the setup.
"""
from __future__ import annotations

import pathlib

import pytest

from outreach import paths
from outreach.auth import AuthError, get_credentials


@pytest.fixture
def hosted(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.ENV_VAR, str(tmp_path))
    (tmp_path / "credentials").mkdir()
    return tmp_path


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.delenv(paths.ENV_VAR, raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "credentials").mkdir()
    return tmp_path


def no_browser_please(monkeypatch):
    """Make reaching the consent flow an unmistakable failure.

    Asserting on an error message alone would pass if the code opened a
    browser first and *then* complained.
    """
    import outreach.auth as auth

    class Forbidden(AssertionError):
        pass

    class Boom:
        @staticmethod
        def from_client_secrets_file(*a, **k):
            raise Forbidden("a browser sign-in was attempted")

    monkeypatch.setattr(auth, "InstalledAppFlow", Boom)
    return Forbidden


# ------------------------------------------------------------ when hosted


def test_a_server_never_reaches_the_browser_sign_in(hosted, monkeypatch):
    """The caller's allow_consent is overruled, not trusted.

    It defaults to True and every CLI command inherits it, so a scheduled
    run inside a container would otherwise walk straight into it.
    """
    no_browser_please(monkeypatch)

    with pytest.raises(AuthError) as raised:
        get_credentials(
            hosted / "credentials" / "oauth_client.json",
            hosted / "credentials" / "token.json",
            allow_consent=True,
        )
    assert "needs a browser, and there is none here" in str(raised.value)


def test_the_refusal_names_the_file_to_copy_and_where(hosted, monkeypatch):
    """A fix nobody can follow is not a fix.

    Telling someone to run a Windows batch file on a server they reach over
    ssh spends the one message that had their attention.
    """
    no_browser_please(monkeypatch)

    with pytest.raises(AuthError) as raised:
        get_credentials(
            hosted / "credentials" / "oauth_client.json",
            hosted / "credentials" / "token.json",
        )

    message = str(raised.value)
    assert str(hosted / "credentials" / "token.json") in message
    assert "Dashboard.bat" not in message, "local instructions on a server"
    assert "preflight" not in message


def test_a_missing_token_reads_differently_from_an_expired_one(hosted, monkeypatch):
    """Two different situations with two different causes.

    "Could not be renewed" sends someone hunting for a revoked grant when
    the truth is that nobody ever put a token on the machine.
    """
    no_browser_please(monkeypatch)

    with pytest.raises(AuthError) as raised:
        get_credentials(
            hosted / "credentials" / "oauth_client.json",
            hosted / "credentials" / "token.json",
        )
    assert "is missing" in str(raised.value)


def test_a_seeded_token_is_used_without_any_sign_in(hosted, monkeypatch):
    """The whole point of seeding one by hand.

    If this needed consent, hosting would be impossible rather than merely
    fiddly to set up.
    """
    no_browser_please(monkeypatch)
    import outreach.auth as auth

    token = hosted / "credentials" / "token.json"
    token.write_text("{}")

    class Valid:
        valid = True
        expired = False
        refresh_token = "r"

    monkeypatch.setattr(
        auth.Credentials, "from_authorized_user_file",
        staticmethod(lambda *a, **k: Valid()),
    )

    creds = get_credentials(hosted / "credentials" / "oauth_client.json", token)
    assert isinstance(creds, Valid)


def test_an_expired_token_refreshes_over_http_with_no_browser(hosted, monkeypatch):
    """Refresh is an HTTP call. A server does that perfectly well."""
    no_browser_please(monkeypatch)
    import outreach.auth as auth

    token = hosted / "credentials" / "token.json"
    token.write_text("{}")
    refreshed = []

    class Expired:
        valid = False
        expired = True
        refresh_token = "r"

        def refresh(self, request):
            refreshed.append(True)
            self.valid, self.expired = True, False

        def to_json(self):
            return "{}"

    monkeypatch.setattr(
        auth.Credentials, "from_authorized_user_file",
        staticmethod(lambda *a, **k: Expired()),
    )

    get_credentials(hosted / "credentials" / "oauth_client.json", token)
    assert refreshed == [True]


# ------------------------------------------------------------- when local


def test_a_local_run_still_opens_a_browser(local, monkeypatch):
    """Nothing about Lyon's machine may change.

    He signs in by pressing a button and finishing it in the tab that opens.
    """
    no_browser_please(monkeypatch)
    # The client file has to exist, or the code refuses earlier for a
    # different and perfectly correct reason.
    (local / "credentials" / "oauth_client.json").write_text("{}")

    # The sentinel is wrapped by the code's own catch-all, which is right --
    # a failed consent should read as a failed consent. Reaching that wrapper
    # at all is the proof the browser path was taken.
    with pytest.raises(AuthError) as raised:
        get_credentials(
            local / "credentials" / "oauth_client.json",
            local / "credentials" / "token.json",
            allow_consent=True,
        )
    assert "OAuth consent failed" in str(raised.value)
    assert "a browser sign-in was attempted" in str(raised.value)


def test_a_local_caller_can_still_refuse_consent_for_itself(local, monkeypatch):
    """The dashboard passes allow_consent=False from a request thread, where
    a blocking browser prompt makes the panel stop answering with no error."""
    no_browser_please(monkeypatch)

    with pytest.raises(AuthError) as raised:
        get_credentials(
            local / "credentials" / "oauth_client.json",
            local / "credentials" / "token.json",
            allow_consent=False,
        )
    assert "Dashboard.bat" in str(raised.value), "the local fix, for a local user"
