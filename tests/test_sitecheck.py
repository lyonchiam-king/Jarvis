"""Pure checks over generated HTML -- no I/O, so every case here is exactly
the string in, list-of-problems out. Each test mutates exactly one thing away
from a page that otherwise passes every check, so a broken check fails only
its own test, never a neighbour's.
"""

from outreach.sitecheck import (
    DISCLAIMER_TEXT,
    SiteProblem,
    check_site,
    repair_instruction,
    to_confirm_count,
)

PHONE = "+60 4 226 1234"  # deliberately spaced differently from the page copy
HEADLINE = "Great Headline For This Shop"
BUSINESS = "Test Business"


def build_html(
    *,
    doctype="<!DOCTYPE html>",
    title="<title>Test Business</title>",
    head_extra="",
    h1=f"<h1>{HEADLINE}</h1>",
    phone_html="<p>Call +60 4-226 1234</p>",
    body_extra="",
    footer=f"<footer>{DISCLAIMER_TEXT}</footer>",
    noindex='<meta name="robots" content="noindex">',
    closing="</body>\n</html>",
):
    return (
        f"{doctype}\n<html>\n<head>\n{title}\n{noindex}\n{head_extra}\n</head>\n"
        f"<body>\n{h1}\n{phone_html}\n{body_extra}\n{footer}\n{closing}"
    )


def codes(html, **overrides):
    kwargs = dict(phone=PHONE, headline=HEADLINE, business_name=BUSINESS,
                   min_bytes=10, max_bytes=1_000_000)
    kwargs.update(overrides)
    return {p.code for p in check_site(html, **kwargs)}


def test_a_clean_page_has_no_problems():
    assert check_site(
        build_html(), phone=PHONE, headline=HEADLINE, business_name=BUSINESS,
        min_bytes=10, max_bytes=1_000_000,
    ) == []


# ------------------------------------------------------------------- truncated


def test_a_page_missing_its_closing_html_tag_is_truncated():
    html = build_html(closing="")
    assert "truncated" in codes(html)


def test_an_unclosed_tag_is_truncated_even_when_html_closes():
    """The stray </html> at the end would fool a naive "ends with" check --
    the tag stack is what catches a break earlier in the document."""
    html = build_html(body_extra="<div><p>never closed")
    assert "truncated" in codes(html)


def test_void_elements_do_not_trip_the_open_tag_check():
    """br/img/meta/link/input/hr never get a closing tag -- proving this
    passes is proving the void-element list actually took effect, not that
    the check is simply not running."""
    html = build_html(body_extra="<br><img src=\"/a.png\"><hr><input type=\"text\">")
    assert "truncated" not in codes(html)


# --------------------------------------------------------------- byte bounds


def test_too_small_is_flagged():
    html = build_html()
    assert "too_small" in codes(html, min_bytes=len(html.encode("utf-8")) + 1000)


def test_too_large_is_flagged():
    html = build_html()
    assert "too_large" in codes(html, max_bytes=10)


def test_a_page_within_bounds_triggers_neither():
    html = build_html()
    result = codes(html, min_bytes=10, max_bytes=len(html.encode("utf-8")) + 1)
    assert "too_small" not in result
    assert "too_large" not in result


# ----------------------------------------------------------------- structure


def test_missing_doctype_is_flagged():
    html = build_html(doctype="")
    assert "no_doctype" in codes(html)


def test_missing_title_is_flagged():
    html = build_html(title="<title></title>")
    assert "no_title" in codes(html)


def test_zero_h1_is_flagged():
    html = build_html(h1="")
    assert "h1_count" in codes(html)


def test_two_h1_is_flagged():
    html = build_html(h1=f"<h1>{HEADLINE}</h1><h1>Second</h1>")
    assert "h1_count" in codes(html)


def test_exactly_one_h1_is_not_flagged():
    assert "h1_count" not in codes(build_html())


# --------------------------------------------------------------------- phone


def test_missing_phone_is_flagged():
    html = build_html(phone_html="<p>no number here</p>")
    assert "missing_phone" in codes(html)


def test_phone_matches_after_normalising_punctuation_and_spacing():
    """The page spells the number '+60 4-226 1234'; the lead's phone field is
    '+60 4 226 1234'. Naive substring comparison fails this; digit
    normalisation must not."""
    html = build_html(phone_html="<p>Ring +60 4-226 1234 anytime</p>")
    assert "missing_phone" not in codes(html, phone="+60 4 226 1234")


# ------------------------------------------------------------------ headline


def test_missing_headline_is_flagged():
    html = build_html(h1="<h1>Something Else Entirely</h1>")
    assert "missing_headline" in codes(html, headline=HEADLINE)


def test_blank_headline_skips_the_check_instead_of_passing_vacuously():
    html = build_html(h1="<h1>Whatever The Model Wrote</h1>")
    assert "missing_headline" not in codes(html, headline="")


# ------------------------------------------------------------------- images


def test_external_image_src_is_flagged():
    html = build_html(body_extra='<img src="https://cdn.example.com/photo.jpg">')
    assert "external_image" in codes(html)


def test_external_background_image_is_flagged():
    html = build_html(
        head_extra="<style>.hero{background-image:url('https://cdn.example.com/bg.jpg');}</style>"
    )
    assert "external_image" in codes(html)


def test_relative_image_src_is_not_flagged():
    html = build_html(body_extra='<img src="/assets/hero.png">')
    assert "external_image" not in codes(html)


def test_data_uri_image_is_not_flagged():
    html = build_html(body_extra='<img src="data:image/png;base64,AAAA">')
    assert "external_image" not in codes(html)


def test_google_maps_host_is_not_flagged():
    html = build_html(
        body_extra='<img src="https://www.google.com/maps/vt/some-tile.png">'
    )
    assert "external_image" not in codes(html)


def test_google_fonts_host_is_not_flagged():
    html = build_html(head_extra='<link href="https://fonts.gstatic.com/x.woff2">')
    # fonts.gstatic.com is not fetched via <img>, but confirm the allowlist
    # constant itself would not flag it if it were -- covered via script test
    # below for the src path; this asserts no false positive leaks in from
    # an unrelated <link>.
    assert "external_image" not in codes(html)


# ------------------------------------------------------------------ scripts


def test_external_script_src_is_flagged():
    html = build_html(head_extra='<script src="https://cdn.example.com/lib.js"></script>')
    assert "external_script" in codes(html)


def test_relative_script_src_is_not_flagged():
    html = build_html(head_extra='<script src="/app.js"></script>')
    assert "external_script" not in codes(html)


def test_inline_script_with_no_src_is_not_flagged():
    html = build_html(head_extra="<script>console.log('hi')</script>")
    assert "external_script" not in codes(html)


# --------------------------------------------------------------------- forms


def test_form_with_an_action_is_flagged():
    html = build_html(body_extra='<form action="/submit"><input type="text"></form>')
    assert "form_action" in codes(html)


def test_form_with_no_action_is_not_flagged():
    html = build_html(body_extra='<form><input type="text"></form>')
    assert "form_action" not in codes(html)


# --------------------------------------------------------------- placeholder


def test_lorem_ipsum_is_flagged():
    html = build_html(body_extra="<p>Lorem ipsum dolor sit amet</p>")
    assert "placeholder" in codes(html)


def test_example_dot_com_is_flagged():
    html = build_html(body_extra="<p>Email us at owner@example.com</p>")
    assert "placeholder" in codes(html)


def test_your_api_key_is_flagged():
    html = build_html(head_extra="<script>const KEY = 'YOUR_API_KEY';</script>")
    assert "placeholder" in codes(html)


def test_insert_bracket_is_flagged():
    html = build_html(body_extra="<p>Address: [INSERT ADDRESS HERE]</p>")
    assert "placeholder" in codes(html)


def test_leftover_code_fence_is_flagged():
    html = build_html(body_extra="<p>```</p>")
    assert "placeholder" in codes(html)


def test_to_confirm_marker_is_not_a_placeholder():
    """The most likely check to be added by mistake: [TO CONFIRM] is the
    model correctly declining to invent a fact, not a defect."""
    html = build_html(body_extra="<p>Address: [TO CONFIRM]</p>")
    assert "placeholder" not in codes(html)


def test_to_confirm_marker_is_counted_not_failed():
    html = build_html(body_extra="<p>[TO CONFIRM] and [TO CONFIRM] again</p>")
    assert to_confirm_count(html) == 2
    assert check_site(
        html, phone=PHONE, headline=HEADLINE, business_name=BUSINESS,
        min_bytes=10, max_bytes=1_000_000,
    ) == []


def test_a_page_with_no_marker_counts_zero():
    assert to_confirm_count(build_html()) == 0


# --------------------------------------------------------------------- meta


def test_missing_noindex_is_flagged():
    html = build_html(noindex="")
    assert "no_noindex" in codes(html)


def test_noindex_present_is_not_flagged():
    assert "no_noindex" not in codes(build_html())


def test_noindex_is_recognised_regardless_of_attribute_order():
    html = build_html(noindex='<meta content="noindex" name="robots">')
    assert "no_noindex" not in codes(html)


def test_missing_disclaimer_is_flagged():
    html = build_html(footer="<footer>Thanks for visiting</footer>")
    assert "no_disclaimer" in codes(html)


def test_disclaimer_present_is_not_flagged():
    assert "no_disclaimer" not in codes(build_html())


# ------------------------------------------------------------ repair_instruction


def test_repair_instruction_is_empty_for_no_problems():
    assert repair_instruction([]) == ""


def test_repair_instruction_names_each_problem_and_asks_for_the_whole_page():
    problems = [
        SiteProblem("no_doctype", "missing <!DOCTYPE html>"),
        SiteProblem("missing_phone", "'+60 4 226 1234' not found on the page"),
    ]
    message = repair_instruction(problems)
    assert "no_doctype" in message
    assert "missing_phone" in message
    assert "missing <!DOCTYPE html>" in message
    assert "whole" in message.lower() or "complete" in message.lower()
