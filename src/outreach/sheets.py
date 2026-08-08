"""Google Sheets as the lead database and the dashboard.

The one rule that shapes this module: the Sheet is a *shared* surface. You edit
it by hand while the pipeline writes to it. So writes are surgical -- the
upsert computes contiguous runs of automated columns and touches only those
ranges, leaving every column marked ``manual=True`` in ``schema.py`` untouched
no matter how many times a stage re-runs.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Sequence

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from . import schema
from .schema import Tab, column_letter

log = logging.getLogger("outreach")

_MAX_ATTEMPTS = 5
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class SheetsError(Exception):
    """A Sheets operation failed in a way the run cannot work around."""


def _with_retry(fn: Callable[[], object], what: str) -> object:
    """Retry transient Sheets failures with exponential backoff and jitter.

    Sheets enforces a per-minute write quota; a burst of appends during a large
    discovery run hits 429 routinely. Backing off is expected behaviour here,
    not an error path.
    """
    delay = 1.0
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return fn()
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            if status not in _RETRYABLE_STATUS or attempt == _MAX_ATTEMPTS:
                raise SheetsError(f"{what} failed (HTTP {status}): {exc}") from exc
            sleep_for = delay + random.uniform(0, 0.5)
            log.warning(
                "%s hit HTTP %s (attempt %d/%d); retrying in %.1fs",
                what, status, attempt, _MAX_ATTEMPTS, sleep_for,
            )
            time.sleep(sleep_for)
            delay *= 2
    raise SheetsError(f"{what} exhausted retries")


@dataclass
class UpsertResult:
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    inserted_names: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.inserted + self.updated


class SheetsClient:
    """Thin, purpose-built wrapper over the Sheets v4 API."""

    def __init__(self, credentials, spreadsheet_id: str = "") -> None:
        self._service = build(
            "sheets", "v4", credentials=credentials, cache_discovery=False
        )
        self.spreadsheet_id = spreadsheet_id

    # ---------------------------------------------------------------- create

    def create_spreadsheet(self, title: str) -> str:
        """Create a new spreadsheet and return its ID."""
        body = {"properties": {"title": title}}
        result = _with_retry(
            lambda: self._service.spreadsheets().create(body=body).execute(),
            "create spreadsheet",
        )
        self.spreadsheet_id = result["spreadsheetId"]  # type: ignore[index]
        return self.spreadsheet_id

    @property
    def url(self) -> str:
        return f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}/edit"

    def probe(self) -> tuple[bool, str]:
        """Is the Sheets API switched on for this Cloud project?

        Asks for a spreadsheet that cannot exist. A 400/404 means the API
        answered and is healthy; a 403 SERVICE_DISABLED means it was never
        enabled. Costs nothing and creates nothing -- the point is to fail
        here, in preflight, rather than three commands later mid-run.
        """
        try:
            self._service.spreadsheets().get(
                spreadsheetId="preflight-probe-nonexistent"
            ).execute()
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            detail = str(exc)
            if status == 403 and "SERVICE_DISABLED" in detail:
                return False, (
                    "the Google Sheets API is not enabled on this Cloud project.\n"
                    "       Enable it, wait ~2 minutes, then re-run:\n"
                    "       https://console.cloud.google.com/apis/library/sheets.googleapis.com"
                )
            if status == 403:
                return False, f"Sheets API refused the request: {detail[:200]}"
            if status == 401:
                return False, (
                    "the stored token was rejected. Delete credentials/token.json "
                    "and re-run to sign in again."
                )
            if status in (400, 404):
                return True, "enabled"
            return False, f"unexpected Sheets response (HTTP {status}): {detail[:200]}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

        # A real spreadsheet with that ID would be extraordinary, but the API
        # clearly answered, which is all we were asking.
        return True, "enabled"

    # ------------------------------------------------------------- bootstrap

    def existing_tab_titles(self) -> set[str]:
        meta = _with_retry(
            lambda: self._service.spreadsheets()
            .get(spreadsheetId=self.spreadsheet_id, fields="sheets.properties")
            .execute(),
            "read spreadsheet metadata",
        )
        return {
            s["properties"]["title"] for s in meta.get("sheets", [])  # type: ignore[union-attr]
        }

    def ensure_tabs(self, tabs: Sequence[Tab] = schema.ALL_TABS) -> list[str]:
        """Create any missing tabs and (re)write their header rows.

        Idempotent: safe to run against an existing Sheet. Existing data rows
        are never touched -- only row 1.
        """
        if not self.spreadsheet_id:
            raise SheetsError("No spreadsheet ID set. Run `outreach init-sheet` first.")

        existing = self.existing_tab_titles()
        created: list[str] = []

        add_requests = [
            {"addSheet": {"properties": {"title": tab.title}}}
            for tab in tabs
            if tab.title not in existing
        ]
        if add_requests:
            _with_retry(
                lambda: self._service.spreadsheets()
                .batchUpdate(
                    spreadsheetId=self.spreadsheet_id, body={"requests": add_requests}
                )
                .execute(),
                "create tabs",
            )
            created = [r["addSheet"]["properties"]["title"] for r in add_requests]

        # Headers, for new and existing tabs alike, so a schema addition
        # propagates on the next run.
        data = [
            {
                "range": f"'{tab.title}'!A1:{column_letter(len(tab.columns) - 1)}1",
                "values": [tab.headers],
            }
            for tab in tabs
        ]
        _with_retry(
            lambda: self._service.spreadsheets()
            .values()
            .batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"valueInputOption": "RAW", "data": data},
            )
            .execute(),
            "write headers",
        )

        self._format_headers(tabs)

        # Google always creates a default "Sheet1"; remove it once our own tabs
        # exist so the Sheet opens on Leads.
        self._remove_default_sheet(tabs)
        return created

    def _sheet_ids(self) -> dict[str, int]:
        meta = _with_retry(
            lambda: self._service.spreadsheets()
            .get(spreadsheetId=self.spreadsheet_id, fields="sheets.properties")
            .execute(),
            "read sheet ids",
        )
        return {
            s["properties"]["title"]: s["properties"]["sheetId"]
            for s in meta.get("sheets", [])  # type: ignore[union-attr]
        }

    def _format_headers(self, tabs: Sequence[Tab]) -> None:
        """Bold + freeze row 1 so the dashboard stays readable when scrolled."""
        ids = self._sheet_ids()
        requests: list[dict] = []
        for tab in tabs:
            sheet_id = ids.get(tab.title)
            if sheet_id is None:
                continue
            requests.append(
                {
                    "repeatCell": {
                        "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {
                            "userEnteredFormat": {
                                "textFormat": {"bold": True},
                                "backgroundColor": {"red": 0.93, "green": 0.93, "blue": 0.93},
                            }
                        },
                        "fields": "userEnteredFormat(textFormat,backgroundColor)",
                    }
                }
            )
            requests.append(
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": sheet_id,
                            "gridProperties": {"frozenRowCount": 1},
                        },
                        "fields": "gridProperties.frozenRowCount",
                    }
                }
            )
        if requests:
            _with_retry(
                lambda: self._service.spreadsheets()
                .batchUpdate(
                    spreadsheetId=self.spreadsheet_id, body={"requests": requests}
                )
                .execute(),
                "format headers",
            )

    def _remove_default_sheet(self, tabs: Sequence[Tab]) -> None:
        ids = self._sheet_ids()
        wanted = {t.title for t in tabs}
        default_id = ids.get("Sheet1")
        if default_id is None or "Sheet1" in wanted or len(ids) <= 1:
            return
        try:
            _with_retry(
                lambda: self._service.spreadsheets()
                .batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={"requests": [{"deleteSheet": {"sheetId": default_id}}]},
                )
                .execute(),
                "remove default Sheet1",
            )
        except SheetsError as exc:
            # Cosmetic only -- never fail a run over it.
            log.debug("Could not remove default Sheet1: %s", exc)

    # ------------------------------------------------------------------ read

    def read_rows(self, tab: Tab) -> dict[str, tuple[int, dict[str, str]]]:
        """Read the tab keyed by its first column.

        Returns ``{key: (row_number, {header: value})}`` with 1-based row
        numbers, so callers can address a row directly for an update.
        """
        last = column_letter(len(tab.columns) - 1)
        result = _with_retry(
            lambda: self._service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{tab.title}'!A2:{last}",
            )
            .execute(),
            f"read {tab.title}",
        )
        values: list[list[str]] = result.get("values", [])  # type: ignore[union-attr]

        rows: dict[str, tuple[int, dict[str, str]]] = {}
        for offset, raw in enumerate(values):
            key = (raw[0] if raw else "").strip()
            if not key:
                continue
            # Sheets truncates trailing empty cells; pad so header zip is total.
            padded = list(raw) + [""] * (len(tab.columns) - len(raw))
            rows[key] = (offset + 2, dict(zip(tab.headers, padded)))
        return rows

    # ----------------------------------------------------------------- write

    def upsert(
        self,
        tab: Tab,
        rows: Sequence[dict[str, object]],
        *,
        on_conflict: Callable[[dict[str, str], dict[str, object]], dict[str, object] | None]
        | None = None,
    ) -> UpsertResult:
        """Insert new rows, update existing ones, never touch manual columns.

        ``on_conflict(existing_row, incoming_row)`` lets a caller merge against
        what is already in the Sheet -- returning ``None`` skips the row.
        """
        if not rows:
            return UpsertResult()

        existing = self.read_rows(tab)
        result = UpsertResult()
        updates: list[dict] = []
        appends: list[list[object]] = []
        runs = tab.automated_runs()

        for incoming in rows:
            key = str(incoming.get(tab.columns[0].name, "")).strip()
            if not key:
                result.skipped += 1
                continue

            match = existing.get(key)
            if match is None:
                appends.append(self._row_values(tab, incoming))
                result.inserted += 1
                name_col = "Business Name"
                if name_col in tab.headers:
                    result.inserted_names.append(str(incoming.get(name_col, key)))
                continue

            row_number, existing_row = match
            merged = on_conflict(existing_row, incoming) if on_conflict else incoming
            if merged is None:
                result.skipped += 1
                continue

            for start, end in runs:
                segment = [merged.get(c.name, "") for c in tab.columns[start:end]]
                updates.append(
                    {
                        "range": (
                            f"'{tab.title}'!{column_letter(start)}{row_number}"
                            f":{column_letter(end - 1)}{row_number}"
                        ),
                        "values": [segment],
                    }
                )
            result.updated += 1

        if updates:
            for chunk in _chunks(updates, 100):
                _with_retry(
                    lambda c=chunk: self._service.spreadsheets()
                    .values()
                    .batchUpdate(
                        spreadsheetId=self.spreadsheet_id,
                        body={"valueInputOption": "RAW", "data": c},
                    )
                    .execute(),
                    f"update {tab.title}",
                )

        if appends:
            last = column_letter(len(tab.columns) - 1)
            for chunk in _chunks(appends, 500):
                _with_retry(
                    lambda c=chunk: self._service.spreadsheets()
                    .values()
                    .append(
                        spreadsheetId=self.spreadsheet_id,
                        range=f"'{tab.title}'!A:{last}",
                        valueInputOption="RAW",
                        insertDataOption="INSERT_ROWS",
                        body={"values": c},
                    )
                    .execute(),
                    f"append to {tab.title}",
                )

        return result

    def update_column(
        self, tab: Tab, column: str, values_by_key: dict[str, object]
    ) -> int:
        """Write one column for specific rows, keyed by the tab's first column.

        Used when a stage owns exactly one field -- advancing `Stage` on the
        Leads tab, say. Writing the single cell instead of the whole automated
        run avoids round-tripping every other value through a string, which
        would quietly turn a rating of 4.5 into the text "4.5".
        """
        if not values_by_key:
            return 0
        if tab.columns[tab.index_of(column)].manual:
            raise SheetsError(f"{tab.title}.{column} is a manual column; refusing to write")

        existing = self.read_rows(tab)
        letter = column_letter(tab.index_of(column))
        data = [
            {
                "range": f"'{tab.title}'!{letter}{existing[key][0]}",
                "values": [[value]],
            }
            for key, value in values_by_key.items()
            if key in existing
        ]
        if not data:
            return 0

        for chunk in _chunks(data, 100):
            _with_retry(
                lambda c=chunk: self._service.spreadsheets()
                .values()
                .batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={"valueInputOption": "RAW", "data": c},
                )
                .execute(),
                f"update {tab.title}.{column}",
            )
        return len(data)

    def append(self, tab: Tab, rows: Sequence[dict[str, object]]) -> int:
        """Append rows unconditionally. Used for the append-only Errors tab."""
        if not rows:
            return 0
        values = [self._row_values(tab, r) for r in rows]
        last = column_letter(len(tab.columns) - 1)
        _with_retry(
            lambda: self._service.spreadsheets()
            .values()
            .append(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{tab.title}'!A:{last}",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": values},
            )
            .execute(),
            f"append to {tab.title}",
        )
        return len(values)

    @staticmethod
    def _row_values(tab: Tab, row: dict[str, object]) -> list[object]:
        # Manual columns are written as "" on insert only -- a brand new row has
        # nothing of yours in it yet.
        return [row.get(c.name, "") for c in tab.columns]


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]
