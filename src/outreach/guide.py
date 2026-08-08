"""Render the handbook inside the app.

The guide is ``DAILY.md`` -- one file, read both on GitHub and in the browser.
A second hand-written HTML copy would drift out of date within a week, and the
stale one would be the one the user actually reads.

That means turning Markdown into HTML with no dependency to do it. This is not
a general Markdown implementation and does not try to be: it covers exactly the
constructs ``DAILY.md`` uses, and ``tests/test_guide.py`` asserts that no raw
markup escapes into the output for the real file. Add a construct to the guide
that isn't handled here and that test fails.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

GUIDE_FILE = "DAILY.md"

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_TABLE_DIVIDER = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")
_ORDERED = re.compile(r"^(\d+)\.\s+(.*)$")
_UNORDERED = re.compile(r"^[-*]\s+(.*)$")
_RULE = re.compile(r"^-{3,}$")

_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])")
_CODE = re.compile(r"`([^`]+)`")


def slugify(text: str) -> str:
    """A stable anchor id, so the page can link straight to a section."""
    plain = _CODE.sub(r"\1", _BOLD.sub(r"\1", text))
    plain = _LINK.sub(r"\1", plain)
    slug = re.sub(r"[^\w\s-]", "", plain).strip().lower()
    return re.sub(r"[\s_]+", "-", slug)


def _inline(text: str) -> str:
    """Escape, then apply inline markup.

    Code spans are lifted out first and put back last, so backticked text
    containing ``*`` or ``_`` is never mangled into emphasis.
    """
    spans: list[str] = []

    def stash(match: re.Match[str]) -> str:
        spans.append(html.escape(match.group(1)))
        return f"\x00{len(spans) - 1}\x00"

    text = _CODE.sub(stash, text)
    text = html.escape(text)
    text = _LINK.sub(
        lambda m: f'<a href="{html.escape(m.group(2), quote=True)}" '
        f'target="_blank" rel="noopener">{m.group(1)}</a>',
        text,
    )
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)
    text = text.replace("&amp;mdash;", "&mdash;")

    for i, span in enumerate(spans):
        text = text.replace(f"\x00{i}\x00", f"<code>{span}</code>")
    return text


def _table(rows: list[str]) -> str:
    def cells(line: str) -> list[str]:
        stripped = line.strip()
        if stripped.startswith("|"):
            stripped = stripped[1:]
        if stripped.endswith("|"):
            stripped = stripped[:-1]
        return [c.strip() for c in stripped.split("|")]

    head, body = cells(rows[0]), rows[2:]
    out = ["<table><thead><tr>"]
    out += [f"<th>{_inline(c)}</th>" for c in head]
    out.append("</tr></thead><tbody>")
    for line in body:
        out.append("<tr>")
        out += [f"<td>{_inline(c)}</td>" for c in cells(line)]
        out.append("</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def render_markdown(text: str) -> str:
    """Turn the guide's Markdown into HTML."""
    lines = text.splitlines()
    out: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]

        if line.startswith("```"):
            i += 1
            block: list[str] = []
            while i < len(lines) and not lines[i].startswith("```"):
                block.append(lines[i])
                i += 1
            i += 1
            out.append(f"<pre><code>{html.escape(chr(10).join(block))}</code></pre>")
            continue

        if not line.strip():
            i += 1
            continue

        if _RULE.match(line.strip()):
            out.append("<hr>")
            i += 1
            continue

        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            body = heading.group(2)
            out.append(
                f'<h{level} id="{slugify(body)}">{_inline(body)}</h{level}>'
            )
            i += 1
            continue

        # A table needs its divider on the second line, otherwise a stray
        # pipe in prose would swallow the paragraph.
        if (
            line.lstrip().startswith("|")
            and i + 1 < len(lines)
            and _TABLE_DIVIDER.match(lines[i + 1])
        ):
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(lines[i])
                i += 1
            out.append(_table(rows))
            continue

        if line.lstrip().startswith(">"):
            block = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                block.append(lines[i].lstrip()[1:].lstrip())
                i += 1
            out.append(f"<blockquote>{render_markdown(chr(10).join(block))}</blockquote>")
            continue

        if _UNORDERED.match(line.strip()) or _ORDERED.match(line.strip()):
            ordered = bool(_ORDERED.match(line.strip()))
            items: list[str] = []
            while i < len(lines):
                stripped = lines[i].strip()
                match = _ORDERED.match(stripped) if ordered else _UNORDERED.match(stripped)
                if match:
                    items.append(match.group(2) if ordered else match.group(1))
                    i += 1
                elif stripped and lines[i].startswith(("  ", "\t")) and items:
                    # A wrapped continuation line belongs to the item above.
                    items[-1] += " " + stripped
                    i += 1
                else:
                    break
            tag = "ol" if ordered else "ul"
            body = "".join(f"<li>{_inline(item)}</li>" for item in items)
            out.append(f"<{tag}>{body}</{tag}>")
            continue

        paragraph: list[str] = []
        while i < len(lines) and lines[i].strip():
            stripped = lines[i].strip()
            if (
                stripped.startswith(("#", ">", "```", "|"))
                or _RULE.match(stripped)
                or _UNORDERED.match(stripped)
                or _ORDERED.match(stripped)
            ):
                break
            paragraph.append(stripped)
            i += 1
        if paragraph:
            out.append(f"<p>{_inline(' '.join(paragraph))}</p>")
        else:
            i += 1

    return "".join(out)


def find_guide(start: Path | None = None) -> Path | None:
    """Locate DAILY.md next to the installed package or up the tree."""
    here = (start or Path(__file__).resolve()).parent
    for candidate in [here, *here.parents]:
        path = candidate / GUIDE_FILE
        if path.is_file():
            return path
    return None


def guide_html() -> str:
    """The handbook as HTML, or an honest explanation of why it's missing."""
    path = find_guide()
    if path is None:
        return (
            "<p>The guide file <code>DAILY.md</code> could not be found next to "
            "this installation. It is also readable on GitHub, in the project "
            "folder, or in any text editor.</p>"
        )
    try:
        return render_markdown(path.read_text(encoding="utf-8"))
    except OSError as exc:  # pragma: no cover -- unreadable file
        return f"<p>Could not read {html.escape(str(path))}: {html.escape(str(exc))}</p>"
