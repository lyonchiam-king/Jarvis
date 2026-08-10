"""Google OAuth -- one consent flow covering Sheets, Drive and Gmail.

Why OAuth rather than a service account: a service account cannot create drafts
in your mailbox without Workspace domain-wide delegation, which is far more
setup than a solo founder needs. OAuth authenticates the pipeline *as you*.

On the Gmail scope: there is no Google scope that grants draft creation while
forbidding send. ``gmail.compose`` is the narrowest one available and it does
technically permit sending. The "never auto-send" guarantee is enforced by this
codebase containing no send call at all -- see the assertion in
``stages/outreach_draft.py`` -- not by the scope.
"""

from __future__ import annotations

import logging
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

log = logging.getLogger("outreach")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",  # read/write the Sheet
    "https://www.googleapis.com/auth/drive.file",    # only files this app creates
    "https://www.googleapis.com/auth/gmail.compose",  # create drafts
]

# A second, separate token for Vertex image generation. Do not fold this into
# SCOPES above: re-consenting the existing Sheets/Gmail token with an added
# scope is how you lose it. google-auth 2.56.3 raises RefreshError("Not all
# requested scopes were granted...") the next time that token refreshes if the
# scopes it was issued for don't match SCOPES exactly (see
# google/oauth2/credentials.py, Credentials.refresh) -- and the except clause
# above turns that into a fresh browser consent, which can simply fail if
# there is no browser handy or the app is stuck behind Google's unverified-app
# warning. Two token files means the worst that happens to a Vertex outage is
# no images; folding the scopes together means the worst that happens is Lyon
# loses Sheets and Gmail too.
VERTEX_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


class AuthError(Exception):
    """OAuth could not be completed. Message tells the user what to fix."""


def get_credentials(
    client_file: Path,
    token_file: Path,
    *,
    scopes: list[str] | None = None,
) -> Credentials:
    """Load cached credentials, refreshing or running consent as needed.

    The token file is written with owner-only permissions and is gitignored.
    """
    scopes = scopes or SCOPES
    creds: Credentials | None = None

    if token_file.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_file), scopes)
        except ValueError as exc:
            # Usually means the stored scopes no longer match SCOPES.
            log.warning("Discarding unreadable token file %s: %s", token_file, exc)
            creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _persist(creds, token_file)
            return creds
        except Exception as exc:  # noqa: BLE001 -- fall through to full consent
            log.warning("Token refresh failed (%s); re-running consent.", exc)

    if not client_file.exists():
        raise AuthError(
            f"OAuth client file not found: {client_file}\n\n"
            "Google Cloud Console > APIs & Services > Credentials >\n"
            "  Create credentials > OAuth client ID > Application type: Desktop app\n"
            f"Download the JSON and save it to {client_file}.\n"
            "See README section 'First-time Google Cloud setup'."
        )

    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(client_file), scopes)
        # port=0 picks a free localhost port; the redirect URI is registered
        # automatically for Desktop app clients.
        creds = flow.run_local_server(port=0)
    except Exception as exc:  # noqa: BLE001
        raise AuthError(f"OAuth consent failed: {exc}") from exc

    _persist(creds, token_file)
    return creds


def vertex_credentials(client_file: Path, token_file: Path) -> Credentials:
    """The Vertex-only counterpart to get_credentials -- same client secret
    file (the same OAuth app can request different scopes), a separate token
    file, and the cloud-platform scope instead of SCOPES.
    """
    return get_credentials(client_file, token_file, scopes=VERTEX_SCOPES)


def access_token(creds: Credentials) -> str:
    """A live bearer token string, refreshing first if the cached one has
    expired.

    A single research or build run can outlive the one-hour lifetime of an
    OAuth access token, so every call site needs a token that is guaranteed
    fresh at the moment it is used, not just at process start.
    """
    if not creds.valid:
        creds.refresh(Request())
    return creds.token


def _persist(creds: Credentials, token_file: Path) -> None:
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json())
    try:
        token_file.chmod(0o600)
    except OSError:
        # Non-POSIX filesystem; the file is still gitignored.
        pass
    log.debug("Saved credentials to %s", token_file)
