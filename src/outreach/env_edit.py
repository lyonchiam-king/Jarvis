"""Set one key in .env without disturbing the rest of it.

The same discipline as ``campaign_edit``: replace the line that holds the
value, leave every other line -- comments included -- byte for byte. A .env
holding a working Places key, Sheet ID and OAuth paths is not something to
rewrite wholesale because one API key changed.

Values are written but never read back out. The dashboard can tell you a key
is *set*; it cannot show it to you, and it never sends one to the browser.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Conservative on purpose: this only ever writes names the code already knows.
_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


class EnvEditError(Exception):
    """The edit was rejected. Always says what to fix."""


def _line_for(name: str, value: str) -> str:
    # A value containing whitespace or '#' must be quoted or python-dotenv
    # truncates it at the comment marker.
    if value and (value != value.strip() or "#" in value or " " in value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'{name}="{escaped}"'
    return f"{name}={value}"


def set_key(path: Path, name: str, value: str) -> None:
    """Write ``name=value`` into the .env at ``path``, creating it if absent.

    An existing assignment is replaced in place, wherever it sits, so the
    comments explaining it stay attached. A name that appears more than once
    is collapsed to the first occurrence -- python-dotenv takes the last, so
    leaving a stale duplicate below would silently win.
    """
    if not _NAME.match(name or ""):
        raise EnvEditError(f"{name!r} is not a valid environment variable name.")
    if "\n" in value or "\r" in value:
        raise EnvEditError("A key cannot contain a line break.")

    try:
        original = path.read_text() if path.exists() else ""
    except OSError as exc:
        raise EnvEditError(f"Could not read {path}: {exc}") from exc

    lines = original.splitlines()
    assignment = re.compile(rf"^\s*(?:export\s+)?{re.escape(name)}\s*=")

    replaced = False
    kept: list[str] = []
    for line in lines:
        if assignment.match(line):
            if replaced:
                continue  # drop the duplicate rather than let it win
            kept.append(_line_for(name, value))
            replaced = True
        else:
            kept.append(line)

    if not replaced:
        kept.append(_line_for(name, value))

    text = "\n".join(kept)
    if not text.endswith("\n"):
        text += "\n"

    try:
        path.write_text(text)
        # A file of API keys should not be world-readable. Best effort: this
        # is a no-op on Windows, which uses ACLs instead.
        os.chmod(path, 0o600)
    except OSError as exc:
        raise EnvEditError(f"Could not write {path}: {exc}") from exc


def is_set(path: Path, name: str) -> bool:
    """Whether the key has a non-empty value. Never returns the value."""
    if not path.exists():
        return False
    assignment = re.compile(rf"^\s*(?:export\s+)?{re.escape(name)}\s*=\s*(.*)$")
    for line in path.read_text().splitlines():
        match = assignment.match(line)
        if match:
            return bool(match.group(1).strip().strip("\"'"))
    return False
