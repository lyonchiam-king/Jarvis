"""The Vertex OAuth token is deliberately separate from the Sheets/Gmail one.

See the comment above auth.VERTEX_SCOPES: folding cloud-platform into SCOPES
would make the *next* refresh of Lyon's working Sheets/Gmail token demand a
scope it was never consented to, which google-auth treats as a RefreshError
and this codebase turns into a fresh browser consent -- one that can simply
fail. The guard test below exists so nobody "tidies up" the two scope lists
into one.
"""

from __future__ import annotations

from outreach import auth


def test_vertex_scopes_is_cloud_platform_only():
    assert auth.VERTEX_SCOPES == ["https://www.googleapis.com/auth/cloud-platform"]


def test_cloud_platform_scope_is_never_added_to_the_sheets_gmail_scopes():
    """The one test standing between a future edit and re-consenting every
    user's working Sheets/Gmail token for an image feature."""
    assert "https://www.googleapis.com/auth/cloud-platform" not in auth.SCOPES


def test_vertex_credentials_requests_vertex_scopes_not_the_shared_ones(monkeypatch, tmp_path):
    captured = {}

    def fake_get_credentials(client_file, token_file, *, scopes=None):
        captured["client_file"] = client_file
        captured["token_file"] = token_file
        captured["scopes"] = scopes
        return "fake-creds"

    monkeypatch.setattr(auth, "get_credentials", fake_get_credentials)

    client_file = tmp_path / "oauth_client.json"
    token_file = tmp_path / "vertex_token.json"
    result = auth.vertex_credentials(client_file, token_file)

    assert result == "fake-creds"
    assert captured["client_file"] == client_file
    assert captured["token_file"] == token_file
    assert captured["scopes"] == auth.VERTEX_SCOPES


class _StubCredentials:
    """A stand-in for google.oauth2.credentials.Credentials with no network
    and no real Google object -- just the attributes access_token reads."""

    def __init__(self, *, valid, token, expired=True):
        self.valid = valid
        self.token = token
        self.expired = expired
        self.refresh_calls = 0

    def refresh(self, request):
        self.refresh_calls += 1
        self.valid = True
        self.token = f"{self.token}-refreshed"


def test_access_token_returns_the_live_token_without_refreshing_when_valid():
    creds = _StubCredentials(valid=True, token="abc123")
    assert auth.access_token(creds) == "abc123"
    assert creds.refresh_calls == 0


def test_access_token_refreshes_an_expired_token_before_returning_it():
    creds = _StubCredentials(valid=False, token="stale")
    result = auth.access_token(creds)
    assert creds.refresh_calls == 1
    assert result == "stale-refreshed"


def test_access_token_returns_a_live_token_on_every_call():
    """A 60-page run can outlive a one-hour token, so this must not cache a
    token past its validity -- each call re-checks creds.valid."""
    creds = _StubCredentials(valid=True, token="first")
    assert auth.access_token(creds) == "first"

    creds.valid = False
    creds.token = "second"
    assert auth.access_token(creds) == "second-refreshed"
