"""Repair an Outreach Log written under the pre-f5eee8c column order.

Commit 67101e3 inserted ``WhatsApp Sent At`` and ``WhatsApp Status`` in the
middle of the tab; f5eee8c moved them to the end again but only in the code --
rows already on the Sheet kept the old positions, and the Sheet's header row
was never widened past the original 14 columns. Two things broke as a result:

*Rotated rows.* A row written under the old order puts its send timestamp in
column L, its send status in M and its ``Drafted`` marker in N. The current
code reads those three as ``Status``, ``Sent At`` and ``Notes``, so a sent row
looks like it has a Status of "2026-08-15T07:37:54Z" -- not "Drafted" -- and
the send queue drops it.

*Rows appended past the table.* ``upsert`` appends into ``A:P`` (16 columns)
while the Sheet's header table was only 14 wide, so Google placed whole rows
starting at column P. Their Lead ID is not in column A, and ``read_rows`` keys
on column A -- so the dashboard never saw them at all.

This script rewrites the header, moves both kinds of row back into the current
schema order, and clears the debris to the right. It prints what it would do
and changes nothing unless ``--apply`` is passed.

    python scripts/migrate_outreach_log.py           # dry run
    python scripts/migrate_outreach_log.py --apply
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from outreach import schema  # noqa: E402
from outreach.config import load_config  # noqa: E402
from outreach.dashboard import _sheets_client  # noqa: E402
from outreach.schema import column_letter  # noqa: E402

TAB = schema.OUTREACH_LOG
WIDTH = len(TAB.columns)  # 16

# Column order as it stood between 67101e3 and f5eee8c. Only the last five
# differ from the current schema, but spelling the whole thing out makes the
# remap below readable and lets the assertion catch a schema change.
OLD_ORDER = (
    "Lead ID",
    "Business Name",
    "Channel",
    "Recipient",
    "Email Source",
    "Subject",
    "Body Preview",
    "Gmail Draft ID",
    "Gmail Draft Link",
    "WhatsApp Message",
    "Drafted At",
    "WhatsApp Sent At",
    "WhatsApp Status",
    "Status",
    "Sent At",
    "Notes",
)

STATUS_WORDS = {"drafted", "sent", "replied", "bounced"}
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:")


def _cell(row: list[str], i: int) -> str:
    return row[i].strip() if i < len(row) else ""


def _looks_like_lead_id(value: str) -> bool:
    # Google place_ids are opaque, but they are long and have no spaces --
    # enough to tell one from a business name or a timestamp.
    return len(value) > 12 and " " not in value


def find_shift(row: list[str]) -> int | None:
    """How many columns right of A this row actually starts.

    Not hardcoded: the append range widened as the schema grew, so rows landed
    at different offsets depending on when they were written. The Lead ID is
    the anchor -- it is the only cell whose shape is unmistakable.
    """
    for i, value in enumerate(row):
        if _looks_like_lead_id(value.strip()):
            return i
    return None


def body_layout(body: list[str]) -> str:
    """Whether a row's cells are in the old (67101e3) or current order.

    The discriminator is where the ``Status`` word sits. Old order put the two
    WhatsApp columns before the manual block, pushing Status to index 13;
    current order has it at index 11. A sent row also carries an ISO timestamp
    at index 11 under the old order, which is the giveaway on rows whose
    Status cell was cleared by hand.
    """
    if _cell(body, 13).casefold() in STATUS_WORDS:
        return "old16"
    if ISO.match(_cell(body, 11)):
        return "old16"
    return "current"


def classify(row: list[str]) -> tuple[str, int]:
    """Return ``(layout, shift)`` for one raw row."""
    if not any(c.strip() for c in row):
        return "blank", 0

    shift = find_shift(row)
    if shift is None:
        return "unknown", 0

    return body_layout(row[shift:]), shift


def to_current(row: list[str], kind: str, shift: int) -> list[str]:
    """Return the row in current-schema order, padded to the tab width."""
    body = row[shift:]
    padded = list(body) + [""] * (WIDTH - len(body))
    body = padded[:WIDTH]

    if kind == "current":
        return body

    # Old order -> current order, by name, so the mapping cannot drift out of
    # sync with the schema.
    return [body[OLD_ORDER.index(col.name)] for col in TAB.columns]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write the changes")
    args = ap.parse_args()

    assert set(OLD_ORDER) == set(TAB.headers), (
        "OLD_ORDER and the current schema must contain the same columns; "
        "the schema changed and this migration needs revisiting."
    )

    config = load_config()
    sheets = _sheets_client(config)
    svc = sheets._service.spreadsheets()

    got = svc.values().get(
        spreadsheetId=config.secrets.sheet_id,
        range=f"'{TAB.title}'!A1:BZ",
    ).execute()
    values: list[list[str]] = got.get("values", [])
    if not values:
        print("Outreach Log is empty -- nothing to do.")
        return 0

    widest = max(len(r) for r in values)
    print(f"Read {len(values)} rows, widest {widest} columns "
          f"(schema wants {WIDTH}).\n")

    header, *data = values
    new_rows: list[list[str]] = [list(TAB.headers)]
    counts = {"current": 0, "old16": 0, "blank": 0, "unknown": 0}
    shifted = 0
    problems: list[str] = []

    if [h.strip() for h in header[:WIDTH]] != list(TAB.headers):
        print(f"Header row will be rewritten to the {WIDTH} schema columns.")
        print(f"  was: {[h for h in header]}")
        print(f"  now: {TAB.headers}\n")

    for n, raw in enumerate(data, start=2):
        kind, shift = classify(raw)
        counts[kind] += 1
        if kind == "blank":
            continue
        if kind == "unknown":
            problems.append(
                f"  row {n}: no Lead ID anywhere in the row -- first cells "
                f"{[c[:20] for c in raw[:3]]}"
            )
            new_rows.append((list(raw) + [""] * WIDTH)[:WIDTH])
            continue
        if shift:
            shifted += 1

        fixed = to_current(raw, kind, shift)
        new_rows.append(fixed)
        name = fixed[TAB.index_of("Business Name")][:34]
        status = fixed[TAB.index_of("Status")]
        wa_at = fixed[TAB.index_of("WhatsApp Sent At")][:20]
        wa_st = fixed[TAB.index_of("WhatsApp Status")]
        notes = fixed[TAB.index_of("Notes")][:24]
        print(f"  row {n:>3} [{kind:>7} +{shift:>2}] {name:<34} "
              f"Status={status!r:<11} WA={wa_st!r:<9} at={wa_at!r:<22} "
              f"notes={notes!r}")

    print()
    print(f"  already current order : {counts['current']}")
    print(f"  rotated (old16 order) : {counts['old16']}")
    print(f"  blank (dropped)       : {counts['blank']}")
    print(f"  unrecognised          : {counts['unknown']}")
    print(f"  ...of which shifted right of column A: {shifted}")

    if problems:
        print("\nRows this script could not classify -- copied through "
              "untouched, check them by hand:")
        print("\n".join(problems))

    # Every surviving row must have a Lead ID in column A, or read_rows will
    # skip it exactly the way it skipped the shifted rows.
    missing_key = [
        i + 1 for i, r in enumerate(new_rows[1:], start=1) if not r[0].strip()
    ]
    if missing_key:
        print(f"\nABORT: {len(missing_key)} row(s) would still have no Lead ID "
              f"in column A: {missing_key}")
        return 1

    last_col = column_letter(WIDTH - 1)
    if not args.apply:
        print(f"\nDry run -- nothing written. {len(new_rows) - 1} data rows "
              f"would be rewritten into A1:{last_col}{len(new_rows)}, and "
              f"columns {column_letter(WIDTH)}: cleared.")
        print("Re-run with --apply to write.")
        return 0

    # This rewrites every cell of the tab, so keep the original grid on disk
    # first. .cache/ is gitignored; the file is the only way back if the remap
    # turns out to be wrong on a row nobody checked.
    backup = Path(".cache") / f"outreach-log-{int(time.time())}.json"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(json.dumps(values, ensure_ascii=False, indent=1), "utf-8")
    print(f"\nBacked up the original grid to {backup.resolve()}")

    # Clear the debris to the right first, so a shorter grid cannot leave
    # orphaned cells behind that a later append would trip over again.
    if widest > WIDTH:
        svc.values().clear(
            spreadsheetId=config.secrets.sheet_id,
            range=f"'{TAB.title}'!{column_letter(WIDTH)}1:BZ",
            body={},
        ).execute()
        print(f"Cleared {column_letter(WIDTH)}1:BZ")

    svc.values().update(
        spreadsheetId=config.secrets.sheet_id,
        range=f"'{TAB.title}'!A1:{last_col}{len(new_rows)}",
        valueInputOption="RAW",
        body={"values": new_rows},
    ).execute()
    print(f"Wrote A1:{last_col}{len(new_rows)} -- {len(new_rows) - 1} data rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
