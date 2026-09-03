"""The Google Sheet layout -- the single source of truth for every tab.

Two rules encoded here:

1. Columns flagged ``manual=True`` belong to the human. The Sheets writer
   computes contiguous runs of automated columns and writes only those ranges,
   so a re-run can never clobber your notes.
2. Every tab is keyed on ``Lead ID`` (the Google ``place_id``) in column A.
   That makes re-runs idempotent and lets tabs be joined with a VLOOKUP.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Column:
    name: str
    manual: bool = False
    note: str = ""


@dataclass(frozen=True)
class Tab:
    title: str
    columns: tuple[Column, ...]

    @property
    def headers(self) -> list[str]:
        return [c.name for c in self.columns]

    def index_of(self, name: str) -> int:
        for i, c in enumerate(self.columns):
            if c.name == name:
                return i
        raise KeyError(f"{self.title} has no column named {name!r}")

    def automated_runs(self) -> list[tuple[int, int]]:
        """Contiguous [start, end) index runs of non-manual columns."""
        runs: list[tuple[int, int]] = []
        start: int | None = None
        for i, col in enumerate(self.columns):
            if col.manual:
                if start is not None:
                    runs.append((start, i))
                    start = None
            elif start is None:
                start = i
        if start is not None:
            runs.append((start, len(self.columns)))
        return runs


LEADS = Tab(
    "Leads",
    (
        Column("Lead ID", note="Google place_id -- stable primary key"),
        Column("Stage"),
        Column("Business Name"),
        Column("Category"),
        Column("Address"),
        Column("Phone"),
        Column("Rating"),
        Column("Review Count"),
        Column("Website Status", note="none | social_only | real"),
        Column("Website URL"),
        Column("Maps URL"),
        Column("Campaign"),
        Column("Country"),
        Column("Discovered At"),
        Column("Last Refreshed"),
        # --- yours below this line; automation never writes these ---
        Column("Priority", manual=True),
        Column("Owner Notes", manual=True),
        Column("Next Action", manual=True),
        # Appended after the manual block, which is allowed -- the rule is
        # append-only, not "automated columns first".
        #
        # The verdict lives here rather than on Research so it can be
        # answered before a lead is researched at all: research decides what
        # is left to do by whether a Lead ID appears on the Research tab, so
        # a stub row written there for an unresearched lead would make that
        # lead skipped for ever.
        Column(
            "WhatsApp Reachable",
            note="yes | no | unknown -- unknown means the check has not run yet",
        ),
        Column("WhatsApp Checked At", note="ISO timestamp; blank until checked"),
    ),
)

RESEARCH = Tab(
    "Research",
    (
        Column("Lead ID"),
        Column("Business Name"),
        Column("Tone"),
        Column("Services"),
        Column("Price Tier"),
        Column("Customer Praise"),
        Column("Customer Pain Points"),
        Column("Suggested Website Angle"),
        Column("Social Handle"),
        Column("Social Bio"),
        Column("Social Recent Post"),
        Column("Social Lookup Status"),
        Column("Email Found"),
        Column("Email Source", note="website | social_page | not found"),
        Column("Research Status"),
        Column("Researched At"),
        # Appended rather than slotted next to "Social Handle" on purpose:
        # rows are read by column position, so inserting a column mid-tab
        # would shift every value after it on sheets that already have data.
        Column(
            "Social Profiles",
            note="platform, handle and full URL -- one profile per line",
        ),
        # Appended, like Social Profiles above and for the same reason.
        # Answered once per lead and then cached: the value is what stops the
        # pipeline spending a site build on someone who cannot be contacted.
        Column(
            "WhatsApp Reachable",
            note="yes | no | unknown -- unknown means the check has not run yet",
        ),
        Column("WhatsApp Checked At", note="ISO timestamp; blank until checked"),
    ),
)

SITE_BRIEFS = Tab(
    "Site Briefs",
    (
        Column("Lead ID"),
        Column("Business Name"),
        Column("Lovable Prompt"),
        Column("Approval Status", manual=True, note="Pending | Approved | Skip"),
        Column("Lovable Project ID"),
        Column("Preview URL"),
        Column("Created At"),
    ),
)

OUTREACH_LOG = Tab(
    "Outreach Log",
    (
        Column("Lead ID"),
        Column("Business Name"),
        Column("Channel", note="Email | WhatsApp"),
        Column("Recipient"),
        Column("Email Source", note="How the address was found, or 'not found'"),
        Column("Subject"),
        Column("Body Preview"),
        Column("Gmail Draft ID"),
        Column("Gmail Draft Link"),
        Column("WhatsApp Message"),
        Column("Drafted At"),
        # --- yours below this line ---
        Column("Status", manual=True, note="Drafted | Sent | Replied | Bounced"),
        Column("Sent At", manual=True),
        Column("Notes", manual=True),
        # Append-only — these two are written by the dashboard when the user
        # presses Send, never by a stage. They sit after the manual columns so
        # the pre-existing 14 columns keep their positions: rows are read by
        # column index, so inserting mid-tab shifts every value in every
        # existing row (see the append-only rule in CLAUDE.md).
        Column("WhatsApp Sent At", note="ISO timestamp set by the dashboard"),
        Column("WhatsApp Status", note="pending | sent | delivered | read | failed"),
    ),
)

ERRORS = Tab(
    "Errors",
    (
        Column("Timestamp"),
        Column("Stage"),
        Column("Lead ID"),
        Column("Business Name"),
        Column("Error Type"),
        Column("Message"),
        Column("Retryable"),
    ),
)

ALL_TABS: tuple[Tab, ...] = (LEADS, RESEARCH, SITE_BRIEFS, OUTREACH_LOG, ERRORS)


def column_letter(index: int) -> str:
    """0-based column index to A1 letter (0 -> A, 26 -> AA)."""
    if index < 0:
        raise ValueError("column index must be non-negative")
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters
