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
from pathlib import Path

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


def _section_bounds(lines: list[str], name: str) -> tuple[int, int] | None:
    """Index of the line after ``name:``, and of that block's end."""
    start = None
    for i, line in enumerate(lines):
        if re.match(rf"^{re.escape(name)}\s*:", line):
            start = i + 1
            break
    if start is None:
        return None

    end = len(lines)
    for i in range(start, len(lines)):
        if _TOP_LEVEL.match(lines[i]):
            end = i
            break
    return start, end


def _campaign_bounds(lines: list[str]) -> tuple[int, int]:
    bounds = _section_bounds(lines, "campaign")
    if bounds is None:
        raise CampaignEditError(
            "This campaign file has no top-level `campaign:` section, so there "
            "is nothing safe to edit. Fix the file by hand."
        )
    return bounds


def _replace_scalar(
    lines: list[str],
    start: int,
    end: int,
    key: str,
    value: str,
    *,
    quote: bool = True,
) -> bool:
    """Rewrite ``  key: ...`` in place. Returns whether it was found."""
    pattern = re.compile(rf"^(\s+){re.escape(key)}\s*:")
    rendered = _scalar(value) if quote else value
    for i in range(start, end):
        match = pattern.match(lines[i])
        if match:
            lines[i] = f"{match.group(1)}{key}: {rendered}"
            return True
    return False


def _remove_scalar(lines: list[str], start: int, end: int, key: str) -> bool:
    """Drop a ``  key: ...`` line. Returns whether one was removed.

    A commented-out ``# key: ...`` is left alone -- it is documentation, not a
    value, and the user may want it back.
    """
    pattern = re.compile(rf"^(\s+){re.escape(key)}\s*:")
    for i in range(start, end):
        if pattern.match(lines[i]):
            del lines[i]
            return True
    return False


def _set_batch_size(lines: list[str], size: int) -> None:
    """Set the one cap that governs every stage.

    ``discovery.max_results`` moves with it. The two are ANDed at runtime
    (``discovery_cap`` takes the smaller), so raising only the batch size would
    silently do nothing -- the worst kind of setting.
    """
    campaign = _section_bounds(lines, "limits")
    if campaign is None:
        raise CampaignEditError(
            "This campaign file has no top-level `limits:` section. "
            "Add one with `batch_size:` under it, or edit the file by hand."
        )
    start, end = campaign
    if not _replace_scalar(lines, start, end, "batch_size", str(size), quote=False):
        raise CampaignEditError("No `batch_size:` line found under `limits:`.")

    discovery = _section_bounds(lines, "discovery")
    if discovery is not None:
        start, end = discovery
        _replace_scalar(lines, start, end, "max_results", str(size), quote=False)


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
    batch_size: int | None = None,
    sender_name: str | None = None,
) -> str:
    """Return ``text`` with the campaign target replaced.

    Raises ``CampaignEditError`` if the result would not parse, so a bad edit
    can never leave the user with a campaign file the pipeline cannot load.
    """
    if batch_size is not None and batch_size < 1:
        raise CampaignEditError("The batch size must be at least 1.")
    if batch_size is not None and batch_size > 200:
        raise CampaignEditError(
            f"A batch of {batch_size} is more than a day's work to review, and "
            "Google only returns about 60 results per area anyway. 200 is the "
            "ceiling here; run the batch twice if you really want more."
        )
    if sender_name is not None and not sender_name.strip():
        raise CampaignEditError(
            "The sender name cannot be empty -- it is who the message is from."
        )
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

    if sender_name is not None:
        # Lives under `outreach:`, not `campaign:`, so it gets its own bounds.
        # Editable from the page because the tracked YAML is not what runs
        # once the dashboard has forked to a .local twin -- changing the name
        # in the file Lyon can see would silently do nothing.
        bounds = _section_bounds(lines, "outreach")
        if bounds is None:
            raise CampaignEditError("No `outreach:` section found in this campaign.")
        o_start, o_end = bounds
        if not _replace_scalar(lines, o_start, o_end, "sender_name", sender_name.strip()):
            raise CampaignEditError("No `sender_name:` line found under `outreach:`.")

    if batch_size is not None:
        _set_batch_size(lines, batch_size)

    result = "\n".join(lines)
    if text.endswith("\n"):
        result += "\n"

    _validate(result)
    return result


def rewrite_research(
    text: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> str:
    """Set the model settings under ``research:``, comments intact.

    ``base_url`` is inserted when absent, because the shipped campaign file
    carries it only as a commented-out hint -- a user switching to a LAN
    Ollama box would otherwise have nowhere for it to go.
    """
    if provider is not None and not provider.strip():
        raise CampaignEditError("Provider cannot be empty.")
    if model is not None and not model.strip():
        raise CampaignEditError("Model cannot be empty -- name the model to use.")

    lines = text.splitlines()
    bounds = _section_bounds(lines, "research")
    if bounds is None:
        raise CampaignEditError(
            "This campaign file has no top-level `research:` section."
        )
    start, end = bounds

    for key, value in (("provider", provider), ("model", model)):
        if value is None:
            continue
        if not _replace_scalar(lines, start, end, key, value.strip()):
            lines.insert(start, f"  {key}: {_scalar(value.strip())}")
            start, end = _section_bounds(lines, "research")  # type: ignore[misc]

    if base_url is not None:
        cleaned = base_url.strip()
        if not _replace_scalar(lines, start, end, "base_url", cleaned):
            if cleaned:
                lines.insert(start, f"  base_url: {_scalar(cleaned)}")

    result = "\n".join(lines)
    if text.endswith("\n"):
        result += "\n"
    _validate(result)
    return result


def rewrite_design_level(text: str, level: str) -> str:
    """Set ``build.design_level``, comments intact.

    Inserted when absent: the shipped campaign file predates the setting, so
    a campaign that has never been edited has nowhere for it to go.
    """
    from .config import DESIGN_LEVELS

    cleaned = level.strip().lower()
    if cleaned not in DESIGN_LEVELS:
        raise CampaignEditError(
            f"design_level must be one of {sorted(DESIGN_LEVELS)}, got {level!r}"
        )

    lines = text.splitlines()
    bounds = _section_bounds(lines, "build")
    if bounds is None:
        raise CampaignEditError(
            "This campaign file has no top-level `build:` section."
        )
    start, end = bounds
    if not _replace_scalar(lines, start, end, "design_level", cleaned):
        lines.insert(start, f"  design_level: {_scalar(cleaned)}")

    result = "\n".join(lines)
    if text.endswith("\n"):
        result += "\n"
    _validate(result)
    return result


def rewrite_services(text: str, intro: str, services: list[str]) -> str:
    """Set ``outreach.services_intro`` and ``outreach.services``, comments intact.

    The list is replaced wholesale rather than merged: it is short, the user
    is editing it as one block on the page, and a merge would leave a line
    they deleted still in the file.
    """
    cleaned = [str(line).strip() for line in services if str(line).strip()]
    if cleaned and not intro.strip():
        raise CampaignEditError(
            "The services list needs a line introducing it, or the bullets "
            "arrive with no explanation."
        )

    lines = text.splitlines()
    bounds = _section_bounds(lines, "outreach")
    if bounds is None:
        raise CampaignEditError(
            "This campaign file has no top-level `outreach:` section."
        )
    start, end = bounds

    # Drop the existing block first -- the key and every item under it -- so a
    # shorter list does not leave the old tail behind.
    kept, i = [], start
    while i < end:
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("services:") or stripped.startswith("services_intro:"):
            i += 1
            # Its items, if any: the indented "- " lines that follow.
            while i < end and lines[i].strip().startswith("- "):
                i += 1
            continue
        kept.append(line)
        i += 1
    lines = lines[:start] + kept + lines[end:]

    # Written even when empty, never omitted. A `*.local.yaml` fork inherits
    # any key it does not carry from the tracked template beside it, so a key
    # simply deleted here would come straight back on the next load -- which
    # would make "clear the list" mean "restore whatever shipped".
    block = [f"  services_intro: {_scalar(intro.strip())}"]
    if cleaned:
        block.append("  services:")
        block += [f"    - {_scalar(line)}" for line in cleaned]
    else:
        block.append("  services: []")
    lines = lines[:start] + block + lines[start:]

    result = "\n".join(lines)
    if text.endswith("\n"):
        result += "\n"
    _validate(result)
    return result


def rewrite_outreach(text: str, *, model: str | None = None) -> str:
    """Set ``outreach.model`` -- the smaller model that writes the message.

    Unlike the research model, blank is meaningful here: it means "use the
    research model", so clearing the field removes the line rather than
    rejecting the edit.
    """
    lines = text.splitlines()
    bounds = _section_bounds(lines, "outreach")
    if bounds is None:
        raise CampaignEditError(
            "This campaign file has no top-level `outreach:` section."
        )
    start, end = bounds

    if model is not None:
        cleaned = model.strip()
        if cleaned:
            if not _replace_scalar(lines, start, end, "model", cleaned):
                lines.insert(start, f"  model: {_scalar(cleaned)}")
                start, end = _section_bounds(lines, "outreach")  # type: ignore[misc]
        else:
            _remove_scalar(lines, start, end, "model")

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


LOCAL_SUFFIX = ".local.yaml"


def local_path_for(path: Path) -> Path:
    """Where this campaign's edits belong: a sibling ``*.local.yaml``.

    Already-local files are returned unchanged, so forking happens once.
    """
    if path.name.endswith(LOCAL_SUFFIX):
        return path
    return path.with_name(path.name.removesuffix(".yaml") + LOCAL_SUFFIX)


def fork_to_local(path: Path) -> Path:
    """Copy a shipped campaign file to its local twin, once.

    The dashboard writes to the campaign file, and git tracks it. That is a
    collision waiting for the next `git pull`: the user's category and areas
    versus whatever shipped, on every update, forever. The fix is to stop
    editing the tracked file at all -- it becomes the starting template, and
    every edit lands in a gitignored copy beside it.

    Returns the path to write. Never overwrites an existing local file.
    """
    local = local_path_for(path)
    if local == path or local.exists():
        return local
    try:
        local.write_text(path.read_text())
    except OSError as exc:
        raise CampaignEditError(
            f"Could not create {local.name}: {exc}"
        ) from exc
    return local
