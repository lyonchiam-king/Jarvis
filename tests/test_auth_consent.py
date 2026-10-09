"""An expired sign-in must fail the dashboard, not hang it.

`get_credentials` falls back to `InstalledAppFlow.run_local_server`, which
opens a browser and blocks until somebody completes the sign-in. In the CLI
that is fine -- the user is looking at the window. Inside a dashboard request
thread it is not: the panel stops answering with no error to show, and the
server log looks perfectly healthy. That is the failure shape this module
pins shut.
"""

import json

import pytest

from outreach import auth


def _token(tmp_path, *, expired=True, refresh_token="r-1"):
    token = tmp_path / "token.json"
    token.write_text(json.dumps({
        "token": "at-1",
        "refresh_token": refresh_token,
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "cid",
        "client_secret": "secret",
        "scopes": auth.SCOPES,
        # In the past, so google-auth reports the credentials as expired.
        "expiry": "2020-01-01T00:00:00Z" if expired else "2999-01-01T00:00:00Z",
    }), encoding="utf-8")
    return token


class TestNonInteractiveCredentials:
    def test_a_failed_refresh_raises_instead_of_opening_a_browser(
        self, tmp_path, monkeypatch
    ):
        token = _token(tmp_path)

        def boom(self, request):
            raise RuntimeError("invalid_grant: Token has been expired or revoked.")

        monkeypatch.setattr(
            "google.oauth2.credentials.Credentials.refresh", boom
        )

        def must_not_run(*a, **k):
            raise AssertionError(
                "consent flow started -- this would block the request forever"
            )

        monkeypatch.setattr(
            auth.InstalledAppFlow, "from_client_secrets_file", must_not_run
        )

        with pytest.raises(auth.AuthError) as exc:
            auth.get_credentials(
                tmp_path / "client.json", token, allow_consent=False
            )

        message = str(exc.value)
        # Says what broke, why it cannot fix itself, and the one command that can.
        assert "expired" in message
        assert "browser" in message
        assert "preflight" in message
        # Carries the underlying reason, so an invalid_grant is distinguishable
        # from a network failure without reading the server log.
        assert "invalid_grant" in message

    def test_a_working_refresh_still_succeeds_without_consent(
        self, tmp_path, monkeypatch
    ):
        token = _token(tmp_path)
        refreshed = []

        def ok(self, request):
            self.token = "at-2"
            self.expiry = None  # google-auth treats None as "does not expire"
            refreshed.append(True)

        monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", ok)

        creds = auth.get_credentials(
            tmp_path / "client.json", token, allow_consent=False
        )

        assert refreshed, "an expired token should still be refreshed"
        assert creds.token == "at-2"
        # And the renewed token is persisted, so the next process starts valid.
        assert json.loads(token.read_text(encoding="utf-8"))["token"] == "at-2"

    def test_a_valid_token_needs_neither_refresh_nor_consent(
        self, tmp_path, monkeypatch
    ):
        token = _token(tmp_path, expired=False)
        monkeypatch.setattr(
            auth.InstalledAppFlow, "from_client_secrets_file",
            lambda *a, **k: pytest.fail("consent must not run for a valid token"),
        )

        creds = auth.get_credentials(
            tmp_path / "client.json", token, allow_consent=False
        )
        assert creds.token == "at-1"

    def test_the_cli_default_still_allows_consent(self, tmp_path, monkeypatch):
        """The terminal path is unchanged -- somebody is watching it."""
        waited = []
        token = _token(tmp_path, refresh_token=None)
        client = tmp_path / "client.json"
        client.write_text("{}", encoding="utf-8")

        class FakeFlow:
            @staticmethod
            def from_client_secrets_file(path, scopes):
                return FakeFlow()

            # Mirrors the real signature: a fake that refused the keyword
            # the production call passes would fail for the wrong reason.
            def run_local_server(self, port=0, timeout_seconds=None):
                from google.oauth2.credentials import Credentials
                waited.append(timeout_seconds)
                return Credentials(
                    token="consented",
                    refresh_token="r-2",
                    token_uri="https://oauth2.googleapis.com/token",
                    client_id="cid",
                    client_secret="secret",
                    scopes=auth.SCOPES,
                )

        monkeypatch.setattr(auth, "InstalledAppFlow", FakeFlow)

        creds = auth.get_credentials(client, token)
        assert creds.token == "consented"
        # Unbounded in the terminal on purpose: somebody is sitting there,
        # and cutting their sign-in short mid-flow helps nobody.
        assert waited == [None]

    def test_a_bounded_consent_that_times_out_says_what_to_do(
        self, tmp_path, monkeypatch
    ):
        """The dashboard's first-run button has to allow consent -- there is
        no token yet -- so it bounds the wait instead. What comes back has to
        be readable by someone who just closed the wrong tab, not a
        WSGITimeoutError."""
        token = _token(tmp_path, refresh_token=None)
        client = tmp_path / "client.json"
        client.write_text("{}", encoding="utf-8")

        class TimingOutFlow:
            @staticmethod
            def from_client_secrets_file(path, scopes):
                return TimingOutFlow()

            def run_local_server(self, port=0, timeout_seconds=None):
                assert timeout_seconds == 5, "the bound must reach the flow"
                raise auth.WSGITimeoutError(
                    "Timed out waiting for response from authorization server"
                )

        monkeypatch.setattr(auth, "InstalledAppFlow", TimingOutFlow)

        with pytest.raises(auth.AuthError) as exc:
            auth.get_credentials(client, token, consent_timeout=5)

        message = str(exc.value)
        assert "never finished" in message
        assert "Press the button again" in message
        # Not the library's wording, which reads like a server fault.
        assert "WSGITimeout" not in message


# ------------------------------------------- why the sign-in keeps expiring

# Reported a month after it was set up: "it says the Google link is broken."
# The refresh failed with invalid_grant, which the old message reported
# faithfully and explained not at all -- so the fix on offer was to sign in
# again, which works for exactly seven more days. Google expires the refresh
# token after a week while the consent screen is in "Testing", and that is
# the whole of it. A tool that breaks weekly and blames nothing is a tool
# nobody trusts.


def _refresh_fails_with(monkeypatch, tmp_path, message: str):
    """A stored token whose refresh raises `message`, consent forbidden."""
    from outreach import auth

    token = tmp_path / "token.json"
    token.write_text(json.dumps({
        "token": "old", "refresh_token": "r", "client_id": "c",
        "client_secret": "s", "scopes": list(auth.SCOPES),
        "expiry": "2020-01-01T00:00:00Z",
    }))

    def boom(*a, **k):
        raise RuntimeError(message)

    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", boom)
    monkeypatch.setattr(
        auth.InstalledAppFlow, "from_client_secrets_file",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("consent started")),
    )
    with pytest.raises(auth.AuthError) as exc:
        auth.get_credentials(tmp_path / "client.json", token, allow_consent=False)
    return str(exc.value)


def test_an_expired_grant_names_the_seven_day_limit(monkeypatch, tmp_path):
    message = _refresh_fails_with(
        monkeypatch, tmp_path, "invalid_grant: Token has been expired or revoked."
    )
    assert "seven-day" in message or "seven day" in message
    assert "Testing" in message
    # And the one-time fix, as somewhere to go rather than something to know.
    assert "PUBLISH APP" in message
    assert "console.cloud.google.com" in message


def test_the_fix_is_one_he_can_perform(monkeypatch, tmp_path):
    """He drives this from run.bat and does not use a terminal. The old text
    offered only `outreach preflight`, which is the right command and the
    wrong instruction."""
    message = _refresh_fails_with(
        monkeypatch, tmp_path, "invalid_grant: Token has been expired or revoked."
    )
    assert "run.bat" in message
    assert "press 9" in message


def test_a_network_failure_is_not_blamed_on_the_consent_screen(monkeypatch, tmp_path):
    """The control, and the reason this is matched on the error text rather
    than the exception type. Sending someone into the Cloud Console over a
    dropped connection costs them an hour and fixes nothing."""
    message = _refresh_fails_with(
        monkeypatch, tmp_path, "Connection aborted: name resolution failed"
    )
    assert "Testing" not in message
    assert "PUBLISH APP" not in message
    # It still says what broke and what to do about it.
    assert "expired" in message
    assert "run.bat" in message


def test_the_hosted_refusal_explains_it_too(monkeypatch, tmp_path):
    """A server hits this on the same seven-day clock, and the person reading
    that log is the one who can publish the app."""
    from outreach import auth, paths

    monkeypatch.setenv(paths.ENV_VAR, str(tmp_path / "state"))
    message = _refresh_fails_with(
        monkeypatch, tmp_path, "invalid_grant: Token has been expired or revoked."
    )
    assert "Testing" in message
    assert "PUBLISH APP" in message
    # Still the hosted instruction, not the Windows one.
    assert "copy that file to" in message
    assert "run.bat" not in message


def test_the_cure_names_the_project_rather_than_describing_it(monkeypatch, tmp_path):
    """"Pick the project this tool uses" is the one sentence the reader
    cannot act on. Six projects, three sharing a name, and the obvious one
    was an AI Studio project with no consent screen at all -- an hour on the
    wrong page, which looks exactly like the advice being wrong.
    """
    from outreach import auth

    client = tmp_path / "client.json"
    client.write_text(json.dumps({"installed": {
        "project_id": "sme-outreach-504919",
        "client_secret": "GOCSPX-never-show-this",
    }}))

    token = tmp_path / "token.json"
    token.write_text(json.dumps({
        "token": "old", "refresh_token": "r", "client_id": "c",
        "client_secret": "s", "scopes": list(auth.SCOPES),
        "expiry": "2020-01-01T00:00:00Z",
    }))

    def boom(*a, **k):
        raise RuntimeError("invalid_grant: Bad Request")

    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", boom)
    monkeypatch.setattr(
        auth.InstalledAppFlow, "from_client_secrets_file",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("consent started")),
    )
    with pytest.raises(auth.AuthError) as exc:
        auth.get_credentials(client, token, allow_consent=False)

    message = str(exc.value)
    # A link that lands on the right page, not an instruction to find it.
    assert "audience?project=sme-outreach-504919" in message
    # And the secret sharing that file is still never printed.
    assert "GOCSPX-never-show-this" not in message


def test_a_missing_consent_screen_is_a_different_repair(monkeypatch, tmp_path):
    """Publishing cannot fix a client that no longer exists. Every project
    showing "not configured yet" is the signature of exactly that, and
    without this the reader keeps pressing a button that was never there.
    """
    message = _refresh_fails_with(
        monkeypatch, tmp_path, "invalid_grant: Bad Request"
    )
    assert "not configured yet" in message
    assert "Desktop app" in message


# --------------------------------------- a sign-in from a replaced client

# Straight after replacing a deleted OAuth client, the old token on disk
# fails to refresh with `invalid_grant: Bad Request` -- word for word what
# an expired grant gives. So the repair that had just been carried out
# looked like it had not worked, and the message went on recommending it.


def _two_clients(tmp_path, same: bool):
    from outreach import auth

    client = tmp_path / "client.json"
    client.write_text(json.dumps({"installed": {
        "client_id": "new-506060726614.apps.googleusercontent.com",
        "project_id": "sme-outreach-504919",
        "client_secret": "GOCSPX-never-show-this",
    }}))
    token = tmp_path / "token.json"
    token.write_text(json.dumps({
        "token": "old",
        "refresh_token": "r",
        "client_id": ("new-506060726614.apps.googleusercontent.com" if same
                      else "old-111111111111.apps.googleusercontent.com"),
        "client_secret": "s",
        "scopes": list(auth.SCOPES),
        "expiry": "2020-01-01T00:00:00Z",
    }))
    return client, token


def test_a_token_from_the_previous_client_is_spotted(tmp_path):
    from outreach.auth import stale_client

    client, token = _two_clients(tmp_path, same=False)
    assert stale_client(client, token) is True


def test_a_matching_client_is_not_called_stale(tmp_path):
    """The control. Without it, anything at all would look replaced and
    every sign-in would be thrown away on every run."""
    from outreach.auth import stale_client

    client, token = _two_clients(tmp_path, same=True)
    assert stale_client(client, token) is False


def test_a_replaced_client_is_never_refreshed(tmp_path, monkeypatch):
    """Refreshing it cannot work, and the error it returns is the misleading
    one. Don't spend the call to be told so in the wrong words."""
    from outreach import auth

    client, token = _two_clients(tmp_path, same=False)

    def must_not_refresh(*a, **k):
        raise AssertionError("refreshed a token from a replaced client")

    monkeypatch.setattr(
        "google.oauth2.credentials.Credentials.refresh", must_not_refresh
    )
    ran = {"consent": False}

    class _Flow:
        @staticmethod
        def from_client_secrets_file(*a, **k):
            ran["consent"] = True

            class _F:
                @staticmethod
                def run_local_server(**kw):
                    return auth.Credentials(
                        token="fresh", refresh_token="r2",
                        client_id="c", client_secret="s",
                        scopes=list(auth.SCOPES),
                    )
            return _F()

    monkeypatch.setattr(auth, "InstalledAppFlow", _Flow)
    creds = auth.get_credentials(client, token, allow_consent=True)
    assert ran["consent"], "it should have gone straight to signing in"
    assert creds.token == "fresh"


def test_the_refusal_says_it_is_a_replaced_client_not_an_expiry(tmp_path, monkeypatch):
    """Where consent cannot run, the words have to change too -- telling
    someone to publish a consent screen they have just rebuilt sends them
    round the same hour again."""
    from outreach import auth

    client, token = _two_clients(tmp_path, same=False)
    monkeypatch.setattr(
        auth.InstalledAppFlow, "from_client_secrets_file",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("consent started")),
    )
    with pytest.raises(auth.AuthError) as exc:
        auth.get_credentials(client, token, allow_consent=False)

    message = str(exc.value)
    assert "different OAuth client" in message
    assert "run.bat" in message
    # Not the seven-day story, which would be the wrong hour to send again.
    assert "PUBLISH APP" not in message
    # And neither client id, nor the secret beside one of them.
    assert "GOCSPX-never-show-this" not in message
    assert "506060726614" not in message
