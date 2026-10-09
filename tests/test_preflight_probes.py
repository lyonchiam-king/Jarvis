"""The probes that catch a disabled API before a run trips over it."""

import json
import types
from dataclasses import replace

import pytest
from googleapiclient.errors import HttpError

from outreach.providers.gmail import GmailClient
from outreach.sheets import SheetsClient


class _Resp:
    def __init__(self, status):
        self.status = status
        self.reason = "err"


def _raiser(exc):
    class _Exec:
        def execute(self):
            raise exc

    return _Exec()


class FakeSheetsService:
    def __init__(self, exc=None, result=None):
        self._exc, self._result = exc, result

    def spreadsheets(self):
        return self

    def get(self, spreadsheetId, fields=None):  # noqa: N803
        if self._exc:
            return _raiser(self._exc)
        return types.SimpleNamespace(execute=lambda: self._result or {})


class FakeGmailService:
    def __init__(self, exc=None):
        self._exc = exc

    def users(self):
        return self

    def drafts(self):
        return self

    def list(self, userId, maxResults):  # noqa: N803
        if self._exc:
            return _raiser(self._exc)
        return types.SimpleNamespace(execute=lambda: {"drafts": []})


def sheets_client(exc=None, result=None, monkeypatch=None):
    import outreach.sheets as mod

    monkeypatch.setattr(mod, "build", lambda *a, **k: FakeSheetsService(exc, result))
    return SheetsClient(credentials=None, spreadsheet_id="")


SERVICE_DISABLED = HttpError(
    _Resp(403), b'{"error":{"message":"...SERVICE_DISABLED..."}}'
)


def test_sheets_probe_names_the_disabled_api(monkeypatch):
    """The wording moved to `api_disabled_message`, which names all three
    APIs rather than only the one that happened to fail first. What this
    still holds is the property: the probe says an API is off, and offers
    somewhere to switch it on."""
    ok, detail = sheets_client(exc=SERVICE_DISABLED, monkeypatch=monkeypatch).probe()
    assert ok is False
    assert "Google Sheets API" in detail
    assert "console.cloud.google.com" in detail


def test_sheets_probe_treats_a_404_as_healthy(monkeypatch):
    ok, detail = sheets_client(
        exc=HttpError(_Resp(404), b"not found"), monkeypatch=monkeypatch
    ).probe()
    assert ok is True
    assert detail == "enabled"


def test_sheets_probe_flags_a_dead_token(monkeypatch):
    ok, detail = sheets_client(
        exc=HttpError(_Resp(401), b"unauthorized"), monkeypatch=monkeypatch
    ).probe()
    assert ok is False
    assert "token.json" in detail


def test_gmail_probe_names_the_disabled_api():
    ok, detail = GmailClient(None, service=FakeGmailService(SERVICE_DISABLED)).probe()
    assert ok is False
    assert "Gmail API is not enabled" in detail


def test_gmail_probe_flags_a_missing_scope():
    ok, detail = GmailClient(
        None, service=FakeGmailService(HttpError(_Resp(403), b"insufficient scope"))
    ).probe()
    assert ok is False
    assert "token.json" in detail


def test_gmail_probe_passes_when_healthy():
    ok, detail = GmailClient(None, service=FakeGmailService()).probe()
    assert ok is True
    assert "compose" in detail


def test_probes_never_create_or_send_anything():
    """Both probes must be read-only -- no create, no send."""
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "src" / "outreach"
    for path, func in ((src / "sheets.py", "probe"), (src / "providers/gmail.py", "probe")):
        tree = ast.parse(path.read_text())
        node = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == func
        )
        called = {
            n.func.attr for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        assert not called & {"create", "send", "update", "append", "batchUpdate"}, (
            f"{path.name}.probe() performs a write"
        )


# ----------------------------------------- which project the sign-in is from

# An hour went on publishing the consent screen of a `gen-lang-client-...`
# project -- the one AI Studio makes for a Gemini key. It had no consent
# screen at all, so it had never issued the sign-in this tool uses. Nothing
# on screen connected the tool to a project, and there is no way to work it
# out by looking at either end.


def test_the_project_is_read_from_the_client_file(tmp_path):
    from outreach.auth import client_project

    client = tmp_path / "oauth_client.json"
    client.write_text(json.dumps({"installed": {
        "client_id": "123.apps.googleusercontent.com",
        "project_id": "sme-outreach-live",
        "client_secret": "DO-NOT-PRINT-ME",
    }}))
    assert client_project(client) == "sme-outreach-live"


def test_a_web_client_is_read_too(tmp_path):
    from outreach.auth import client_project

    client = tmp_path / "oauth_client.json"
    client.write_text(json.dumps({"web": {"project_id": "other-project"}}))
    assert client_project(client) == "other-project"


def test_an_unreadable_client_file_is_unknown_rather_than_fatal(tmp_path):
    """Preflight exists to report problems, so it may not become one."""
    from outreach.auth import client_project

    missing = tmp_path / "nope.json"
    assert client_project(missing) == ""

    broken = tmp_path / "broken.json"
    broken.write_text("{not json at all")
    assert client_project(broken) == ""


def test_preflight_names_the_project_and_never_the_secret(tmp_path, monkeypatch):
    """The project id is in every Console URL and is not a credential. The
    client id and secret sit in the same file and are never printed --
    secrets are write-only, and that does not bend for a diagnostic."""
    from click.testing import CliRunner

    from outreach import cli as cli_mod

    client = tmp_path / "oauth_client.json"
    client.write_text(json.dumps({"installed": {
        "client_id": "123-abc.apps.googleusercontent.com",
        "project_id": "sme-outreach-live",
        "client_secret": "GOCSPX-never-show-this",
    }}))

    from outreach.config import load_config

    base = load_config("config/campaigns/penang-my.yaml", env_file=None)
    config = replace(base, secrets=replace(
        base.secrets,
        oauth_client_file=client,
        token_file=tmp_path / "token.json",
        places_api_key="",
        sheet_id="s",
    ))

    monkeypatch.setattr(cli_mod, "load_config", lambda *a, **k: config)
    monkeypatch.setattr(
        cli_mod, "_credentials",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no network here")),
    )

    result = CliRunner().invoke(cli_mod.main, ["preflight"])
    out = result.output

    assert "sme-outreach-live" in out
    assert "console.cloud.google.com/auth/audience?project=sme-outreach-live" in out
    assert "GOCSPX-never-show-this" not in out
    assert "123-abc.apps.googleusercontent.com" not in out


def test_the_probe_names_the_same_three_apis_as_the_run(monkeypatch):
    """Preflight is where someone checks whether enabling worked. A second,
    shorter description naming only Sheets would have them switch on one
    API, see preflight pass, and hit Drive on the next command."""
    from googleapiclient.errors import HttpError

    from outreach.sheets import SheetsClient

    disabled = (
        "returned \"Google Sheets API has not been used in project "
        "506060726614 before or it is disabled.\" Details: "
        "\"[{'reason': 'SERVICE_DISABLED'}]\""
    )

    class _Resp:
        status = 403
        reason = "Forbidden"

    class _Req:
        def execute(self):
            raise HttpError(_Resp(), disabled.encode())

    class _Sheets:
        def get(self, **kwargs):
            return _Req()

    class _Service:
        def spreadsheets(self):
            return _Sheets()

    client = SheetsClient.__new__(SheetsClient)
    client._service = _Service()

    ok, detail = client.probe()
    assert ok is False
    assert "506060726614" in detail
    for host in ("sheets.googleapis.com", "drive.googleapis.com", "gmail.googleapis.com"):
        assert f"apis/library/{host}" in detail
