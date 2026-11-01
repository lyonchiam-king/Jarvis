"""The in-app handbook: it renders, and nothing leaks through as raw markup."""

import re
from pathlib import Path

from outreach.guide import find_guide, guide_html, render_markdown, slugify

DAILY = Path("DAILY.md")


# ------------------------------------------------------------ the constructs


def test_headings_become_headings_with_anchors():
    out = render_markdown("## Your morning, step by step")
    assert '<h2 id="your-morning-step-by-step">' in out
    assert "Your morning, step by step</h2>" in out


def test_paragraphs_join_wrapped_lines():
    out = render_markdown("A sentence that was\nwrapped across two lines.")
    assert out == "<p>A sentence that was wrapped across two lines.</p>"


def test_bold_and_italic_and_code():
    out = render_markdown("**Approve** the *brief* in `config.yaml`")
    assert "<strong>Approve</strong>" in out
    assert "<em>brief</em>" in out
    assert "<code>config.yaml</code>" in out


def test_links_open_in_a_new_tab():
    out = render_markdown("See [the README](README.md) for setup.")
    assert '<a href="README.md" target="_blank" rel="noopener">the README</a>' in out


def test_bullet_lists():
    out = render_markdown("- Approve\n- Skip\n- Decide later")
    assert out == "<ul><li>Approve</li><li>Skip</li><li>Decide later</li></ul>"


def test_numbered_lists():
    out = render_markdown("1. Copy prompt\n2. Paste into Lovable")
    assert out.startswith("<ol><li>Copy prompt</li>")


def test_a_wrapped_list_item_stays_one_item():
    out = render_markdown("- A bullet that runs on\n  to a second line\n- Another")
    assert out.count("<li>") == 2
    assert "runs on to a second line" in out


def test_tables():
    out = render_markdown("| A | B |\n|---|---|\n| 1 | 2 |")
    assert "<th>A</th><th>B</th>" in out
    assert "<td>1</td><td>2</td>" in out


def test_code_fences_are_preserved_verbatim():
    out = render_markdown('```powershell\ngit pull\n.\\.venv\\Scripts\\Activate.ps1\n```')
    assert "<pre><code>git pull\n.\\.venv\\Scripts\\Activate.ps1</code></pre>" in out


def test_blockquotes():
    out = render_markdown("> **Note:** this matters.")
    assert "<blockquote>" in out and "<strong>Note:</strong>" in out


def test_horizontal_rules():
    assert "<hr>" in render_markdown("Above\n\n---\n\nBelow")


# ------------------------------------------------------------------ safety


def test_html_in_the_source_is_escaped():
    """The guide is ours, but rendering must not be an injection route."""
    out = render_markdown("Watch out for <script>alert(1)</script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_code_spans_are_not_mangled_by_emphasis():
    out = render_markdown("Run `pip install -e .[dev]` and `a*b*c`")
    assert "<code>a*b*c</code>" in out
    assert "<em>" not in out


def test_a_pipe_in_prose_is_not_read_as_a_table():
    out = render_markdown("Use the pipe | character freely in a sentence.")
    assert "<table>" not in out
    assert "<p>" in out


def test_slugs_ignore_markup():
    assert slugify("**Bold** and `code`") == "bold-and-code"


# ------------------------------------------------- against the real handbook


def test_the_real_guide_is_found_and_rendered():
    assert find_guide() is not None
    out = guide_html()
    assert len(out) > 5000
    assert "<h1" in out and "<table>" in out


def test_no_raw_markdown_leaks_into_the_rendered_guide():
    """If DAILY.md grows a construct this renderer can't handle, fail here."""
    out = guide_html()
    # Strip the parts where literal markup is legitimate.
    stripped = re.sub(r"<pre><code>.*?</code></pre>", "", out, flags=re.S)
    stripped = re.sub(r"<code>.*?</code>", "", stripped, flags=re.S)

    leaks = {
        "unrendered heading": re.search(r"(?m)^#{1,6}\s", stripped),
        "unrendered bold": "**" in stripped,
        "unrendered table row": re.search(r"(?m)^\s*\|", stripped),
        "unrendered bullet": re.search(r"(?m)^[-*]\s", stripped),
        "unrendered link": re.search(r"\[[^\]]+\]\([^)]+\)", stripped),
        "unrendered blockquote": re.search(r"(?m)^>\s", stripped),
    }
    found = [name for name, hit in leaks.items() if hit]
    assert not found, f"raw markdown in the rendered guide: {found}"


def test_every_guide_heading_gets_a_unique_anchor():
    """Duplicate ids would make the page's deep links jump to the wrong place."""
    headings = [
        line.lstrip("#").strip()
        for line in DAILY.read_text().splitlines()
        if re.match(r"^#{1,6}\s", line)
    ]
    slugs = [slugify(h) for h in headings]
    duplicates = {s for s in slugs if slugs.count(s) > 1}
    assert not duplicates, f"duplicate anchors: {duplicates}"


def test_the_steps_the_dashboard_links_to_all_exist():
    """The step headings are deep-link targets; renaming one must break a test."""
    out = guide_html()
    for anchor in (
        "getting-it-onto-your-computer",
        "put-it-on-your-desktop-once",
        "1-press-run-this-mornings-batch",
        "2-review-the-briefs-one-at-a-time",
        "3-build-the-ones-you-approved",
    ):
        assert f'id="{anchor}"' in out, f"missing anchor: {anchor}"


def test_the_infographic_steps_help_link_lands_somewhere_real():
    """Every other step heading links into the handbook. A link to an anchor
    that does not exist drops the reader at the top of an 800-line page,
    which is the same as no link at all."""
    from outreach.dashboard import PAGE
    from outreach.guide import slugify

    anchors = {
        slugify(line.lstrip("#").strip())
        for line in open("DAILY.md").read().splitlines()
        if line.startswith("#")
    }
    wanted = "the-second-pitch-a-picture-instead-of-a-page"
    assert wanted in anchors, "the handbook section the Infographic step links to is gone"
    assert f'href="/guide#{wanted}"' in PAGE
