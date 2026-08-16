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


class SheetSchemaError(SheetsError):
    """A tab's header row does not match the schema this code reads by.

    Its own class because the fix is specific and always the same: run
    ``outreach init-sheet`` to rewrite the header row. Callers that show
    errors to a human should let this one through verbatim.
    """


def guard_manual_column(tab: Tab, column: str, *, user_edit: bool) -> None:
    """Refuse to write a manual column unless a human just asked for it.

    Manual columns exist so a pipeline re-run can never overwrite what you
    typed. That protects you from *automation*, not from yourself: pressing
    Approve in the dashboard is a person changing their own cell, and refusing
    it would mean the only way to approve a build is to hand-edit the Sheet.

    So ``user_edit=True`` is legitimate for exactly one shape of caller: a
    dashboard action, on a named row, that the user triggered in that moment.
    It is never legitimate inside a stage -- a stage runs unattended, and
    "the user started the run" is not consent to overwrite a specific cell.

    Lives at module level so test fakes can enforce the same rule the real
    client does. A fake that skipped it would let this exact bug ship.
    """
    if not tab.columns[tab.index_of(column)].manual or user_edit:
        return
    raise SheetsError(
        f"{tab.title}.{column} is a manual column; refusing to write. "
        "Only a direct user action may set it, and it must pass user_edit=True."
    )


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

        Reads from row 1 so the header can be checked before any value is
        trusted. Rows are zipped against ``tab.headers`` by position, so a
        Sheet whose header row is narrower or in a different order does not
        fail -- it silently returns the wrong value under the right name. That
        is how a mid-tab column insert once made every drafted message vanish
        from the dashboard while the run log said success, so the mismatch is
        an error now rather than a guess.
        """
        last = column_letter(len(tab.columns) - 1)
        result = _with_retry(
            lambda: self._service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{tab.title}'!A1:{last}",
            )
            .execute(),
            f"read {tab.title}",
        )
        values: list[list[str]] = result.get("values", [])  # type: ignore[union-attr]
        if not values:
            # No header and no data: a tab that exists but was never set up.
            # ensure_tabs writes the header, so let the caller get an empty
            # read rather than an error about a Sheet with nothing in it.
            return {}

        self._check_header(tab, values[0])

        rows: dict[str, tuple[int, dict[str, str]]] = {}
        for offset, raw in enumerate(values[1:]):
            key = (raw[0] if raw else "").strip()
            if not key:
                continue
            # Sheets truncates trailing empty cells; pad so header zip is total.
            padded = list(raw) + [""] * (len(tab.columns) - len(raw))
            rows[key] = (offset + 2, dict(zip(tab.headers, padded)))
        return rows

    @staticmethod
    def _check_header(tab: Tab, header: list[str]) -> None:
        """Raise unless the Sheet's header row matches the schema exactly."""
        live = [h.strip() for h in header]
        want = tab.headers
        if live == want:
            return

        if len(live) < len(want) and live == want[: len(live)]:
            missing = ", ".join(want[len(live):])
            # Widening the header is necessary but rarely sufficient. A header
            # narrower than the schema is also what makes appends land past the
            # end of the table, so by the time this fires there are usually
            # data rows sitting in the wrong columns too -- and init-sheet does
            # not move those. Naming only init-sheet here sent a founder round
            # the loop of "I ran the fix and the rows are still missing".
            raise SheetSchemaError(
                f"The '{tab.title}' tab is missing the column(s) {missing}, so "
                "rows are being read against the wrong columns.\n"
                "Check the data rows before you fix the header: a short header "
                "also makes new rows get appended past the end of the table, "
                "where nothing can see them.\n"
                f"  - '{schema.OUTREACH_LOG.title}': run "
                "`python scripts/migrate_outreach_log.py` (dry run, writes "
                "nothing). It repairs the header and the rows together.\n"
                "  - Otherwise, if the data rows are known to be in the right "
                "columns, `outreach init-sheet` rewrites row 1 on its own and "
                "never touches your data."
            )

        differences = [
            f"column {column_letter(i)}: Sheet has "
            f"{(live[i] if i < len(live) else '(empty)')!r}, "
            f"code expects {(want[i] if i < len(want) else '(nothing)')!r}"
            for i in range(max(len(live), len(want)))
            if (live[i] if i < len(live) else None) != (want[i] if i < len(want) else None)
        ]
        raise SheetSchemaError(
            f"The '{tab.title}' tab's header row does not match the schema, so "
            "every row would be read against the wrong columns:\n  "
            + "\n  ".join(differences)
            + "\nRun `outreach init-sheet` to rewrite row 1. If your data rows "
            "are also in the old order, fix those first -- see "
            "scripts/migrate_outreach_log.py."
        )

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
        self,
        tab: Tab,
        column: str,
        values_by_key: dict[str, object],
        *,
        user_edit: bool = False,
    ) -> int:
        """Write one column for specific rows, keyed by the tab's first column.

        Used when a stage owns exactly one field -- advancing `Stage` on the
        Leads tab, say. Writing the single cell instead of the whole automated
        run avoids round-tripping every other value through a string, which
        would quietly turn a rating of 4.5 into the text "4.5".

        ``user_edit`` is the only way to write a manual column. See
        ``guard_manual_column`` for when that is legitimate.
        """
        if not values_by_key:
            return 0
        guard_manual_column(tab, column, user_edit=user_edit)

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
