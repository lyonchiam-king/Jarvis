"""The probes that catch a disabled API before a run trips over it."""

import types

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
    ok, detail = sheets_client(exc=SERVICE_DISABLED, monkeypatch=monkeypatch).probe()
    assert ok is False
    assert "Sheets API is not enabled" in detail
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
