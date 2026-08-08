"""The guarantee that matters: a re-run must never clobber your manual columns."""

import pytest

from outreach import schema, sheets as sheets_mod
from outreach.sheets import SheetsClient


class _Exec:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeValues:
    def __init__(self, rows):
        self.rows = rows
        self.batch_updates = []
        self.appends = []

    def get(self, spreadsheetId, range):  # noqa: A002 - mirrors the Google client
        return _Exec({"values": self.rows})

    def batchUpdate(self, spreadsheetId, body):  # noqa: N802
        self.batch_updates.extend(body["data"])
        return _Exec({})

    def append(self, spreadsheetId, range, valueInputOption, insertDataOption, body):  # noqa: A002, N803
        self.appends.extend(body["values"])
        return _Exec({})


class FakeSpreadsheets:
    def __init__(self, rows):
        self._values = FakeValues(rows)
        self.batch_requests = []

    def values(self):
        return self._values

    def get(self, spreadsheetId, fields=None):
        return _Exec({"sheets": [{"properties": {"title": t.title, "sheetId": i}}
                                 for i, t in enumerate(schema.ALL_TABS)]})

    def batchUpdate(self, spreadsheetId, body):  # noqa: N802
        self.batch_requests.extend(body["requests"])
        return _Exec({})

    def create(self, body):
        return _Exec({"spreadsheetId": "new-sheet-id"})


class FakeService:
    def __init__(self, rows):
        self._ss = FakeSpreadsheets(rows)

    def spreadsheets(self):
        return self._ss


@pytest.fixture
def client(monkeypatch):
    def make(rows=None):
        service = FakeService(rows or [])
        monkeypatch.setattr(sheets_mod, "build", lambda *a, **k: service)
        c = SheetsClient(credentials=None, spreadsheet_id="sheet-1")
        c._fake = service  # test handle
        return c

    return make


def test_update_writes_only_automated_columns(client):
    # An existing lead where the founder has filled in the three manual columns.
    existing_row = ["place-1", "Discovered", "Salon A"] + [""] * 12 + [
        "High",
        "Owner is a friend of a friend",
        "Call Tuesday",
    ]
    c = client([existing_row])

    c.upsert(
        schema.LEADS,
        [{"Lead ID": "place-1", "Stage": "Discovered", "Business Name": "Salon A v2"}],
    )

    written = c._fake._ss.values().batch_updates
    assert len(written) == 1
    # Columns A..O are the automated run; P, Q, R are the founder's.
    assert written[0]["range"] == "'Leads'!A2:O2"
    assert len(written[0]["values"][0]) == 15


def test_manual_column_values_are_never_sent_in_the_payload(client):
    c = client([["place-1"] + [""] * 14 + ["High", "notes", "next"]])
    c.upsert(
        schema.LEADS,
        [{"Lead ID": "place-1", "Business Name": "X", "Owner Notes": "SHOULD NOT APPEAR"}],
    )
    payload = c._fake._ss.values().batch_updates[0]["values"][0]
    assert "SHOULD NOT APPEAR" not in payload


def test_new_lead_is_appended_full_width(client):
    c = client([])
    result = c.upsert(schema.LEADS, [{"Lead ID": "new-1", "Business Name": "Salon N"}])

    assert result.inserted == 1
    appended = c._fake._ss.values().appends
    assert len(appended) == 1
    assert len(appended[0]) == len(schema.LEADS.columns)
    assert appended[0][0] == "new-1"


def test_on_conflict_can_skip_a_row(client):
    c = client([["place-1"] + [""] * 17])
    result = c.upsert(
        schema.LEADS,
        [{"Lead ID": "place-1"}],
        on_conflict=lambda existing, incoming: None,
    )
    assert result.skipped == 1
    assert c._fake._ss.values().batch_updates == []


def test_rows_missing_a_key_are_skipped_not_written(client):
    c = client([])
    result = c.upsert(schema.LEADS, [{"Lead ID": "", "Business Name": "orphan"}])
    assert result.skipped == 1
    assert c._fake._ss.values().appends == []


def test_read_rows_pads_truncated_trailing_cells(client):
    # Sheets omits trailing empty cells; the header zip must still line up.
    c = client([["place-1", "Discovered", "Salon A"]])
    rows = c.read_rows(schema.LEADS)
    row_number, data = rows["place-1"]

    assert row_number == 2
    assert data["Business Name"] == "Salon A"
    assert data["Next Action"] == ""
    assert len(data) == len(schema.LEADS.columns)


def test_blank_rows_are_ignored(client):
    c = client([["place-1"], [], ["", "junk"], ["place-2"]])
    rows = c.read_rows(schema.LEADS)
    assert set(rows) == {"place-1", "place-2"}


def test_errors_tab_is_append_only(client):
    c = client([])
    n = c.append(
        schema.ERRORS,
        [{"Timestamp": "t", "Stage": "discovery", "Message": "boom", "Retryable": "TRUE"}],
    )
    assert n == 1
    assert c._fake._ss.values().appends[0][0] == "t"
