"""Create or repair the campaign spreadsheet.

Run: ``outreach init-sheet``

Safe to run repeatedly. Against an existing Sheet it adds any missing tab and
refreshes header rows, leaving your data alone.
"""

from __future__ import annotations

import logging

from .. import schema
from ..config import Config
from ..sheets import SheetsClient

log = logging.getLogger("outreach")


def run(config: Config, sheets: SheetsClient, *, title: str | None = None) -> tuple[str, list[str]]:
    """Return ``(spreadsheet_id, newly_created_tab_titles)``."""
    created_spreadsheet = False

    if not sheets.spreadsheet_id:
        sheet_title = title or f"SME Outreach -- {config.campaign.name}"
        log.info("Creating spreadsheet %r", sheet_title)
        sheets.create_spreadsheet(sheet_title)
        created_spreadsheet = True

    new_tabs = sheets.ensure_tabs(schema.ALL_TABS)

    if created_spreadsheet:
        log.info("")
        log.info("Created new spreadsheet. Add this to your .env:")
        log.info("  SHEET_ID=%s", sheets.spreadsheet_id)

    return sheets.spreadsheet_id, new_tabs
