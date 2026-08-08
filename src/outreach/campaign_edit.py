"""Edit a campaign file's target without flattening it.

``yaml.safe_dump`` would round-trip the data and throw away every comment in
the file -- and this project's campaign files are mostly comments explaining
*why* each value is what it is. Those comments are the documentation the user
actually reads, so rewriting the file has to be surgical: change the lines that
hold the values, leave everything else byte-for-byte alone.

Only three things are editable from the dashboard -- category, location, and
areas -- because those are the ones a founder changes weekly. Everything else
stays a deliberate trip to the text editor.
"""

from __future__ import annotations

import json
import re

import yaml


class CampaignEditError(Exception):
    """The edit was rejected. Always says what to fix."""


# A top-level key sits in column 0; anything indented belongs to the block
# above it. That is the whole grammar we need to find the campaign section.
_TOP_LEVEL = re.compile(r"^[A-Za-z_][\w-]*\s*:")
_LIST_ITEM = re.compile(r"^\s+-\s")
_COMMENT_OR_BLANK = re.compile(r"^\s*(#.*)?$")


def _scalar(value: str) -> str:
    """Quote a value so YAML reads it back as the exact string given.

    ``json.dumps`` produces a double-quoted scalar with the right escaping, and
    every JSON string is valid YAML -- which saves hand-rolling quote rules for
    a value a user typed.
    """
    return json.dumps(value, ensure_ascii=False)


def _campaign_bounds(lines: list[str]) -> tuple[int, int]:
    """Index of the line after ``campaign:``, and of the block's end."""
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^campaign\s*:", line):
            start = i + 1
            break
    if start is None:
        raise CampaignEditError(
            "This campaign file has no top-level `campaign:` section, so there "
            "is nothing safe to edit. Fix the file by hand."
        )

    end = len(lines)
    for i in range(start, len(lines)):
        if _TOP_LEVEL.match(lines[i]):
            end = i
            break
    return start, end


def _replace_scalar(lines: list[str], start: int, end: int, key: str, value: str) -> bool:
    """Rewrite ``  key: ...`` in place. Returns whether it was found."""
    pattern = re.compile(rf"^(\s+){re.escape(key)}\s*:")
    for i in range(start, end):
        match = pattern.match(lines[i])
        if match:
            lines[i] = f"{match.group(1)}{key}: {_scalar(value)}"
            return True
    return False


def _replace_areas(lines: list[str], start: int, end: int, areas: list[str]) -> None:
    """Rewrite the ``areas:`` list, keeping the comments that introduce it.

    Comments *above* ``areas:`` survive -- that is where the explanation of why
    several areas are needed lives. Comments interleaved between individual
    list items do not; they belong to entries that are being replaced.
    """
    key = re.compile(r"^(\s+)areas\s*:")
    at = None
    indent = "  "
    for i in range(start, end):
        match = key.match(lines[i])
        if match:
            at, indent = i, match.group(1)
            break

    rendered = [f"{indent}  - {_scalar(a)}" for a in areas]

    if at is None:
        if not areas:
            return
        # No areas block yet: put one after `location:`, where it reads
        # naturally, or at the end of the campaign block as a fallback.
        anchor = end - 1
        for i in range(start, end):
            if re.match(r"^\s+location\s*:", lines[i]):
                anchor = i
                break
        lines[anchor + 1 : anchor + 1] = [f"{indent}areas:", *rendered]
        return

    # Consume the existing list: items, plus blanks and comments *between*
    # items. Stop at the first line that starts something else.
    stop = at + 1
    last_item = at
    while stop < end:
        if _LIST_ITEM.match(lines[stop]):
            last_item = stop
            stop += 1
        elif _COMMENT_OR_BLANK.match(lines[stop]):
            stop += 1
        else:
            break
    # Never swallow trailing blank lines or comments that introduce whatever
    # comes next -- only up to the final list item.
    stop = last_item + 1

    if areas:
        lines[at : stop] = [f"{indent}areas:", *rendered]
    else:
        # Dropping every area is legitimate: discovery falls back to the single
        # `location` query, which is how campaigns worked before areas existed.
        lines[at:stop] = []


def rewrite_campaign(
    text: str,
    *,
    category: str | None = None,
    location: str | None = None,
    areas: list[str] | None = None,
) -> str:
    """Return ``text`` with the campaign target replaced.

    Raises ``CampaignEditError`` if the result would not parse, so a bad edit
    can never leave the user with a campaign file the pipeline cannot load.
    """
    if category is not None and not category.strip():
        raise CampaignEditError("Category cannot be empty -- it is what you search for.")
    if location is not None and not location.strip():
        raise CampaignEditError(
            "Location cannot be empty. It is used in the outreach copy, and it "
            "is the search Google falls back to when no areas are listed."
        )

    lines = text.splitlines()
    start, end = _campaign_bounds(lines)

    if category is not None:
        if not _replace_scalar(lines, start, end, "category", category.strip()):
            raise CampaignEditError("No `category:` line found under `campaign:`.")
    if location is not None:
        if not _replace_scalar(lines, start, end, "location", location.strip()):
            raise CampaignEditError("No `location:` line found under `campaign:`.")
    if areas is not None:
        cleaned = [a.strip() for a in areas if a.strip()]
        if len(set(cleaned)) != len(cleaned):
            seen: set[str] = set()
            dupes = [a for a in cleaned if a in seen or seen.add(a)]  # type: ignore[func-returns-value]
            raise CampaignEditError(
                f"Duplicate area: {dupes[0]!r}. Searching it twice finds nothing "
                "the first search didn't."
            )
        # Bounds are recomputed because a category/location rewrite can only
        # change lines in place -- but an areas insert shifts them, so this
        # must be the last edit.
        start, end = _campaign_bounds(lines)
        _replace_areas(lines, start, end, cleaned)

    result = "\n".join(lines)
    if text.endswith("\n"):
        result += "\n"

    _validate(result)
    return result


def _validate(text: str) -> None:
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise CampaignEditError(f"The edit would produce invalid YAML: {exc}") from exc

    if not isinstance(parsed, dict) or not isinstance(parsed.get("campaign"), dict):
        raise CampaignEditError("The edit would break the `campaign:` section.")

    campaign = parsed["campaign"]
    for required in ("name", "category", "location"):
        if not str(campaign.get(required) or "").strip():
            raise CampaignEditError(f"The edit would leave `campaign.{required}` empty.")
