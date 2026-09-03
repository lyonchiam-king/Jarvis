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
        token = _token(tmp_path, refresh_token=None)
        client = tmp_path / "client.json"
        client.write_text("{}", encoding="utf-8")

        class FakeFlow:
            @staticmethod
            def from_client_secrets_file(path, scopes):
                return FakeFlow()

            def run_local_server(self, port=0):
                from google.oauth2.credentials import Credentials
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
