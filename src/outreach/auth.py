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
from google_auth_oauthlib.flow import InstalledAppFlow, WSGITimeoutError

from . import paths

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


# Google issues a refresh token that expires after **seven days** when the
# OAuth consent screen is still in "Testing". Nothing about the error says
# so: it arrives as `invalid_grant`, which reads as a revoked grant or a
# clock problem, and signing in again fixes it -- for another seven days.
# That is a tool that breaks every week with no hint why, so when the cause
# is recognisable the message names it and names the one-time fix.
def _seven_day_note(client_file: Path) -> str:
    """The cure, pointed at the project that actually issued this sign-in.

    The first version said "pick the project this tool uses" -- which is the
    one sentence the reader cannot act on. Six Cloud projects, three of them
    called some variant of the same name, and the obvious one was a
    `gen-lang-client-...` that AI Studio had made for a Gemini key and that
    had no consent screen at all. An hour went on the wrong page, and from
    that side it looks exactly like the advice being wrong.

    So the project is named, with its own link. Only `project_id` is used;
    see `client_project`.
    """
    project = client_project(client_file)
    # The link carries the project because two projects can share a display
    # name -- his do -- so the name in the Console's picker proves nothing
    # and the chip at the top looks identical on the wrong one. And the page
    # has two names depending on which Console a project shows you, so both
    # are given: looking for the wrong one ends up on Enabled APIs, which is
    # a different screen with nothing to press.
    where = (
        f"  1. Open https://console.cloud.google.com/auth/audience"
        f"?project={project}\n"
        f"     (this tool's project is {project} -- use the link, not the\n"
        f"      project picker: two projects can share a display name)\n"
        f"     The page is called Audience, or OAuth consent screen on an\n"
        f"     older Console. It is NOT Enabled APIs & services.\n"
        if project
        else "  1. Open https://console.cloud.google.com/auth/audience\n"
             "     and pick the project that issued this sign-in. The page is\n"
             "     called Audience, or OAuth consent screen on an older\n"
             "     Console. It is NOT Enabled APIs & services.\n"
    )
    return (
        "\nThis usually means the seven-day limit, not anything you did.\n"
        "Google expires the sign-in after a week while the OAuth consent "
        "screen is still set to \"Testing\" -- so it works, then stops, then "
        "works again when you sign in, over and over.\n\n"
        "To stop it happening again, once:\n"
        + where
        + "  2. If it says Publishing status: Testing, press PUBLISH APP\n"
        "  3. Sign in again here\n"
        "An \"unverified app\" warning on the sign-in screen after that is "
        "expected for your own tool -- press Advanced, then continue.\n\n"
        "If that page says \"Google Auth Platform not configured yet\", or "
        "the project is gone, then the sign-in's OAuth client no longer "
        "exists and no amount of publishing will help: make a new one "
        "(APIs & Services > Credentials > Create credentials > OAuth client "
        "ID > Desktop app), save the JSON over\n"
        f"    {client_file}\n"
        "and sign in again.\n"
    )


def _is_expired_grant(exc: Exception | None) -> bool:
    """Whether a refresh failed because the grant itself is finished.

    Matched on the error text because that is where Google puts it -- the
    exception type is the same for a network blip, and treating those alike
    would send someone to the Cloud Console over a dropped connection.
    """
    return exc is not None and "invalid_grant" in str(exc).lower()



def client_project(client_file: Path) -> str:
    """Which Cloud project issued this OAuth client, or "" if unreadable.

    An hour was lost publishing the consent screen of the wrong project --
    a `gen-lang-client-...` one that AI Studio makes for a Gemini key, which
    had no consent screen at all and so had never issued anything. Nothing
    on screen connected the tool to a project, and there is no way to work
    it out by looking.

    Only `project_id` is read. The client id and secret live in the same
    file and are never printed: a project id appears in every Console URL
    and is not a credential, which is exactly why it is the part worth
    showing.
    """
    try:
        import json

        data = json.loads(client_file.read_text())
    except Exception:  # noqa: BLE001 -- absent or malformed is "unknown"
        return ""
    for section in ("installed", "web"):
        found = data.get(section, {})
        if isinstance(found, dict) and found.get("project_id"):
            return str(found["project_id"])
    return str(data.get("project_id", "") or "")



def _client_id_of(path: Path, *, token: bool) -> str:
    """The client id recorded in a token file or an OAuth client file."""
    try:
        import json

        data = json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return ""
    if token:
        return str(data.get("client_id", "") or "")
    for section in ("installed", "web"):
        found = data.get(section, {})
        if isinstance(found, dict) and found.get("client_id"):
            return str(found["client_id"])
    return str(data.get("client_id", "") or "")


def stale_client(client_file: Path, token_file: Path) -> bool:
    """Whether the stored sign-in came from a different OAuth client.

    Replacing a deleted OAuth client leaves the old token on disk, and
    refreshing it fails with `invalid_grant: Bad Request` -- the same words
    an expired grant produces. So the fix that had just been performed
    looked like it had not worked, and the message went on recommending it.

    Both ids are read and neither is printed: what the reader needs is that
    the two disagree, not their values.
    """
    stored = _client_id_of(token_file, token=True)
    current = _client_id_of(client_file, token=False)
    return bool(stored and current and stored != current)


def get_credentials(
    client_file: Path,
    token_file: Path,
    *,
    scopes: list[str] | None = None,
    allow_consent: bool = True,
    consent_timeout: int | None = None,
) -> Credentials:
    """Load cached credentials, refreshing or running consent as needed.

    The token file is written with owner-only permissions and is gitignored.

    ``allow_consent=False`` is for callers with nobody watching a terminal.
    Consent means ``run_local_server``, which opens a browser and then blocks
    until someone completes the sign-in -- forever, if nobody does. That is
    survivable in the CLI, where the user is already looking at the window,
    and not survivable inside a dashboard request thread: the panel simply
    stops answering, with no error to show and a log that looks healthy. Those
    callers get an AuthError naming the one command that fixes it instead.
    """
    scopes = scopes or SCOPES
    creds: Credentials | None = None

    # A server has no browser and nobody watching, so consent is not a
    # preference there -- it is impossible. The caller's `allow_consent` is
    # overruled rather than trusted, because the default is True and every
    # CLI command inherits it: a scheduled run inside a container would
    # otherwise reach `run_local_server`, try to open a browser that is not
    # there, and either fail with an error about a display or block until
    # something kills it. Neither says what is actually wrong.
    consent_possible = allow_consent and not paths.is_hosted()

    if token_file.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_file), scopes)
        except ValueError as exc:
            # Usually means the stored scopes no longer match SCOPES.
            log.warning("Discarding unreadable token file %s: %s", token_file, exc)
            creds = None

    # A sign-in made with a *different* OAuth client cannot be refreshed,
    # and the failure is word for word the one an expired grant gives. The
    # old token is simply finished: discard it and sign in, rather than
    # spending a network call to be told so in misleading terms.
    replaced_client = bool(creds) and stale_client(client_file, token_file)
    if replaced_client:
        log.info("The stored sign-in is from a previous OAuth client; "
                 "it cannot be refreshed and will be replaced.")
        creds = None

    if creds and creds.valid:
        return creds

    refresh_error: Exception | None = None
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _persist(creds, token_file)
            return creds
        except Exception as exc:  # noqa: BLE001 -- fall through to full consent
            refresh_error = exc
            log.warning("Token refresh failed (%s); re-running consent.", exc)

    if not consent_possible:
        detail = f" ({type(refresh_error).__name__}: {refresh_error})" if refresh_error else ""
        if paths.is_hosted():
            # Different fix, different words. Telling someone to run a
            # Windows batch file on a server they reach over ssh wastes the
            # one message that had their attention, and "sign in again" is
            # advice this machine cannot act on at all.
            had_one = " and could not be renewed" if creds else " is missing"
            why = _seven_day_note(client_file) if _is_expired_grant(refresh_error) else ""
            raise AuthError(
                f"The Google sign-in{had_one}{detail}.\n{why}\n"
                "This server cannot sign in: that needs a browser, and there "
                "is none here.\n\n"
                "On the machine you normally use, sign in once so a fresh "
                f"token is written, then copy that file to\n"
                f"    {token_file}\n"
                "on this server and restart it. Nothing else needs to change."
            )
        # Named as a double-click first. `outreach preflight` is the same
        # thing and is kept because the error is also read on other people's
        # machines, but the owner of this tool does not use a terminal, and
        # an instruction he cannot follow is not an instruction.
        if replaced_client:
            raise AuthError(
                "The stored Google sign-in was made with a different OAuth "
                "client, so it cannot be renewed -- this is what you get "
                "straight after replacing one.\n\n"
                "Nothing is wrong with the new client. It just needs signing "
                "in once:\n"
                "Close the dashboard, double-click run.bat and press 9 "
                "(\"Check everything works\"), and finish the Google sign-in "
                "it opens."
            )
        raise AuthError(
            "Your Google sign-in has expired and could not be renewed"
            f"{detail}.\n"
            + (_seven_day_note(client_file) if _is_expired_grant(refresh_error) else "")
            + "\nSigning in again needs a browser, which this process cannot "
            "open on your behalf.\n"
            "Close the dashboard, double-click run.bat and press 9 "
            "(\"Check everything works\") -- or run `outreach preflight` in a "
            "terminal. Finish the Google sign-in it opens, then start "
            "Dashboard.bat again."
        )

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
        creds = flow.run_local_server(port=0, timeout_seconds=consent_timeout)
    except WSGITimeoutError as exc:
        raise AuthError(
            "The Google sign-in was never finished, so nothing was created.\n"
            "A browser tab should have opened asking you to choose a Google "
            "account and allow access.\n"
            "Press the button again, and complete it in that tab."
        ) from exc
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
