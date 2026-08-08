"""Unit coverage for the Stage 2 providers: web, social, Place Details, LLM."""

import json
import types

import pytest
import responses

from outreach.providers import llm, social, web
from outreach.providers.places import DETAILS_URL, PlacesClient, _parse_details

# ------------------------------------------------------------------- web


@pytest.mark.parametrize(
    "candidate,expected",
    [
        ("hello@sunsalon.com.my", True),
        ("bookings@salon.co", True),
        ("noreply@salon.com", False),
        ("someone@example.com", False),
        ("hash@2x.png", False),
        ("a@sentry.io", False),
        ("a@eu.sentry.io", False),
        ("notanemail", False),
        ("two@@at.com", False),
        ("nolocal@", False),
    ],
)
def test_is_plausible_email(candidate, expected):
    assert web.is_plausible_email(candidate) is expected


def test_mailto_addresses_rank_above_body_text():
    html = """
      <p>Scraped somewhere@random.com in the footer</p>
      <a href="mailto:hello@sunsalon.my?subject=Booking">Email us</a>
    """
    assert web.extract_emails(html)[0] == "hello@sunsalon.my"


def test_email_extraction_deduplicates():
    html = "a@b.com a@b.com <a href='mailto:a@b.com'>x</a>"
    assert web.extract_emails(html) == ["a@b.com"]


def test_strip_html_drops_scripts_and_collapses_whitespace():
    html = "<html><script>var x = 'hidden';</script><p>Real   text</p></html>"
    text = web.strip_html(html)
    assert "hidden" not in text
    assert text == "Real text"


@responses.activate
def test_fetch_reports_login_wall_rather_than_raising():
    responses.add(responses.GET, "https://facebook.com/x", status=403)
    result = web.fetch("https://facebook.com/x")
    assert result.ok is False
    assert result.status == "login required"


@responses.activate
def test_fetch_reports_timeout_rather_than_raising():
    import requests

    responses.add(responses.GET, "https://slow.example", body=requests.Timeout())
    assert web.fetch("https://slow.example").status == "timeout"


@responses.activate
def test_fetch_skips_non_html_content():
    responses.add(
        responses.GET,
        "https://x.example/logo.png",
        body=b"\x89PNG",
        content_type="image/png",
    )
    assert web.fetch("https://x.example/logo.png").status.startswith("not a page")


def test_fetch_handles_empty_url_without_network():
    assert web.fetch("").status == "no url"


# ---------------------------------------------------------------- social


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://facebook.com/sunsalon", "@sunsalon"),
        ("https://www.instagram.com/sunsalon/", "@sunsalon"),
        ("https://m.facebook.com/sunsalon", "@sunsalon"),
        ("https://facebook.com/profile.php?id=123", ""),
        ("https://facebook.com/", ""),
        ("https://sunsalon.com.my", ""),
        ("", ""),
    ],
)
def test_handle_from_url(url, expected):
    assert social.handle_from_url(url) == expected


def test_meta_description_is_read_in_either_attribute_order():
    forward = '<meta property="og:description" content="Family salon in Georgetown since 1998">'
    reversed_ = '<meta content="Family salon in Georgetown since 1998" name="description">'
    for html in (forward, reversed_):
        assert "Georgetown" in social.extract_meta_description(html)


def test_meta_description_ignores_stub_content():
    assert social.extract_meta_description('<meta name="description" content="Home">') == ""


def test_lookup_with_no_url_never_touches_the_network():
    assert social.lookup("").status == "no social url on record"


@responses.activate
def test_lookup_keeps_the_handle_even_behind_a_login_wall():
    responses.add(responses.GET, "https://facebook.com/sunsalon", status=403)
    profile = social.lookup("https://facebook.com/sunsalon")

    assert profile.handle == "@sunsalon"
    assert profile.status == "login required"
    assert profile.bio == ""


@responses.activate
def test_lookup_reads_the_bio_when_the_page_is_public():
    responses.add(
        responses.GET,
        "https://facebook.com/sunsalon",
        body='<meta property="og:description" content="Family salon in Georgetown since 1998">',
        content_type="text/html",
    )
    profile = social.lookup("https://facebook.com/sunsalon")

    assert profile.status == "ok"
    assert "Georgetown" in profile.bio


# -------------------------------------------------------- Place Details


def test_parse_details_extracts_reviews_and_price_label():
    details = _parse_details(
        {
            "id": "abc",
            "displayName": {"text": "Sun Salon"},
            "rating": 4.6,
            "userRatingCount": 31,
            "priceLevel": "PRICE_LEVEL_MODERATE",
            "editorialSummary": {"text": "Neighbourhood hair salon"},
            "regularOpeningHours": {"weekdayDescriptions": ["Monday: 9-6"]},
            "reviews": [
                {
                    "rating": 5,
                    "text": {"text": "Great cut"},
                    "relativePublishTimeDescription": "a month ago",
                    "authorAttribution": {"displayName": "Jane Doe"},
                }
            ],
        }
    )

    assert details.name == "Sun Salon"
    assert details.price_level == "Mid-range"
    assert details.opening_hours == ("Monday: 9-6",)
    assert details.reviews[0].rating == 5
    assert details.reviews[0].when == "a month ago"


def test_parse_details_does_not_capture_review_author_identity():
    details = _parse_details(
        {
            "id": "abc",
            "reviews": [
                {
                    "rating": 5,
                    "text": {"text": "Great cut"},
                    "authorAttribution": {
                        "displayName": "Jane Doe",
                        "photoUri": "https://x/photo.jpg",
                    },
                }
            ],
        }
    )
    review = details.reviews[0]
    assert not hasattr(review, "author")
    assert "Jane" not in repr(review)


def test_parse_details_skips_empty_reviews_and_bad_ratings():
    details = _parse_details(
        {
            "id": "abc",
            "rating": "n/a",
            "reviews": [
                {"rating": 5, "text": {"text": "   "}},
                {"rating": "five", "text": {"text": "Good"}},
            ],
        }
    )
    assert details.rating is None
    assert len(details.reviews) == 1
    assert details.reviews[0].rating == 0


@responses.activate
def test_place_details_sends_bare_field_names_not_places_prefixed():
    url = DETAILS_URL.format(place_id="abc")
    responses.add(responses.GET, url, json={"id": "abc"}, status=200)

    PlacesClient("key").place_details("abc")

    mask = responses.calls[0].request.headers["X-Goog-FieldMask"]
    assert "reviews" in mask
    assert "places." not in mask


def test_place_details_requires_a_place_id():
    from outreach.providers.places import PlacesError

    with pytest.raises(PlacesError, match="place_id"):
        PlacesClient("key").place_details("")


# ------------------------------------------------------------------ LLM


def _response(text=None, stop_reason="end_turn", stop_details=None):
    content = []
    if text is not None:
        content = [types.SimpleNamespace(type="text", text=text)]
    return types.SimpleNamespace(
        content=content, stop_reason=stop_reason, stop_details=stop_details
    )


class FakeClient:
    def __init__(self, response):
        self._response = response
        self.kwargs = None
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.kwargs = kwargs
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


VALID = json.dumps(
    {
        "tone": "warm",
        "services": ["cut", "colour"],
        "price_tier": "mid-range",
        "customer_praise": "Friendly",
        "customer_pain_points": "Booking is hard",
        "suggested_website_angle": "Add online booking.",
    }
)


def test_brief_writer_parses_a_valid_response():
    client = FakeClient(_response(VALID))
    brief = llm.BriefWriter("k", client=client).write("evidence")

    assert brief.services_text == "cut, colour"
    assert brief.price_tier == "mid-range"


def test_brief_writer_requests_structured_output_and_effort():
    client = FakeClient(_response(VALID))
    llm.BriefWriter("k", client=client, effort="high").write("evidence")

    output_config = client.kwargs["output_config"]
    assert output_config["effort"] == "high"
    assert output_config["format"]["type"] == "json_schema"
    assert client.kwargs["model"] == "claude-opus-5"


def test_refusal_is_reported_before_content_is_read():
    client = FakeClient(
        _response(
            text=None,
            stop_reason="refusal",
            stop_details=types.SimpleNamespace(category="cyber"),
        )
    )
    with pytest.raises(llm.LLMError, match="declined"):
        llm.BriefWriter("k", client=client).write("evidence")


def test_truncated_response_is_an_explicit_error():
    client = FakeClient(_response('{"tone": "wa', stop_reason="max_tokens"))
    with pytest.raises(llm.LLMError, match="max_tokens"):
        llm.BriefWriter("k", client=client).write("evidence")


def test_unparseable_json_is_an_explicit_error():
    client = FakeClient(_response("not json at all"))
    with pytest.raises(llm.LLMError, match="unparseable"):
        llm.BriefWriter("k", client=client).write("evidence")


def test_sdk_exceptions_become_llm_errors():
    client = FakeClient(RuntimeError("connection reset"))
    with pytest.raises(llm.LLMError, match="connection reset"):
        llm.BriefWriter("k", client=client).write("evidence")


def test_missing_api_key_fails_fast_with_guidance():
    with pytest.raises(llm.LLMError, match="ANTHROPIC_API_KEY"):
        llm.BriefWriter("")


def test_prompt_includes_reviews_and_states_when_there_are_none():
    with_reviews = llm.build_prompt(
        name="Sun Salon",
        category="Hair salon",
        address="1 Test St",
        rating=4.6,
        review_count=31,
        price_level="Mid-range",
        editorial_summary="",
        opening_hours=(),
        reviews=[(5, "Great cut", "a month ago")],
    )
    assert "[5/5, a month ago] Great cut" in with_reviews

    without = llm.build_prompt(
        name="Sun Salon",
        category="",
        address="",
        rating=None,
        review_count=0,
        price_level="",
        editorial_summary="",
        opening_hours=(),
        reviews=[],
    )
    assert "Reviews: none available." in without


# ------------------------------------------------- model capability guards


@pytest.mark.parametrize(
    "model,expected",
    [
        ("claude-opus-5", True),
        ("claude-sonnet-5", True),
        ("claude-opus-4-8", True),
        ("claude-sonnet-4-6", True),
        ("claude-haiku-4-5", False),
        ("claude-haiku-4-5-20251001", False),
        ("claude-sonnet-4-5", False),
        ("claude-opus-4-1", False),
    ],
)
def test_supports_effort(model, expected):
    assert llm.supports_effort(model) is expected


def test_effort_is_sent_to_models_that_accept_it():
    client = FakeClient(_response(VALID))
    llm.BriefWriter("k", client=client, model="claude-sonnet-5", effort="high").write("e")

    assert client.kwargs["output_config"]["effort"] == "high"


def test_effort_is_omitted_for_models_that_reject_it():
    """Haiku 4.5 400s on output_config.effort -- sending it fails every lead."""
    client = FakeClient(_response(VALID))
    llm.BriefWriter("k", client=client, model="claude-haiku-4-5", effort="medium").write("e")

    output_config = client.kwargs["output_config"]
    assert "effort" not in output_config
    # The structured-output format must survive the omission.
    assert output_config["format"]["type"] == "json_schema"
