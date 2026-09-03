import pytest
import responses

from outreach.models import WebsiteStatus
from outreach.providers.places import (
    SEARCH_TEXT_URL,
    PlacesClient,
    PlacesError,
    classify_website,
)

SOCIAL = ("facebook.com", "instagram.com", "linktr.ee", "business.site")


@pytest.mark.parametrize(
    "url,expected",
    [
        ("", WebsiteStatus.NONE),
        ("   ", WebsiteStatus.NONE),
        ("https://sunsalon.com.my", WebsiteStatus.REAL),
        ("http://www.sunsalon.com.my/book", WebsiteStatus.REAL),
        ("https://www.facebook.com/sunsalon", WebsiteStatus.SOCIAL_ONLY),
        ("https://facebook.com/sunsalon", WebsiteStatus.SOCIAL_ONLY),
        ("https://m.facebook.com/sunsalon", WebsiteStatus.SOCIAL_ONLY),
        ("https://instagram.com/sunsalon", WebsiteStatus.SOCIAL_ONLY),
        ("https://linktr.ee/sunsalon", WebsiteStatus.SOCIAL_ONLY),
        ("https://sunsalon.business.site", WebsiteStatus.SOCIAL_ONLY),
    ],
)
def test_classify_website(url, expected):
    assert classify_website(url, SOCIAL) is expected


def test_classify_website_is_not_fooled_by_lookalike_domain():
    # notfacebook.com must not match facebook.com
    assert classify_website("https://notfacebook.com/x", SOCIAL) is WebsiteStatus.REAL


def test_classify_website_handles_missing_scheme():
    assert classify_website("facebook.com/sunsalon", SOCIAL) is WebsiteStatus.SOCIAL_ONLY


def _place(pid, **kw):
    base = {
        "id": pid,
        "displayName": {"text": f"Salon {pid}"},
        "formattedAddress": "1 Test St",
        "nationalPhoneNumber": "04-226 1234",
        "rating": 4.5,
        "userRatingCount": 12,
        "primaryTypeDisplayName": {"text": "Hair salon"},
        "googleMapsUri": "https://maps.google.com/?cid=1",
        "businessStatus": "OPERATIONAL",
    }
    base.update(kw)
    return base


@responses.activate
def test_search_text_follows_pagination():
    responses.add(
        responses.POST,
        SEARCH_TEXT_URL,
        json={"places": [_place("a"), _place("b")], "nextPageToken": "TOKEN"},
        status=200,
    )
    responses.add(
        responses.POST, SEARCH_TEXT_URL, json={"places": [_place("c")]}, status=200
    )

    client = PlacesClient("key")
    results = list(client.search_text("hair salon in Penang"))

    assert [r.place_id for r in results] == ["a", "b", "c"]
    assert client.request_count == 2
    # Second request must carry the page token.
    import json as _json

    assert _json.loads(responses.calls[1].request.body)["pageToken"] == "TOKEN"


@responses.activate
def test_search_text_sends_field_mask_and_caps_page_size():
    responses.add(responses.POST, SEARCH_TEXT_URL, json={"places": []}, status=200)
    client = PlacesClient("key")
    list(client.search_text("q", page_size=99))

    import json as _json

    request = responses.calls[0].request
    assert "places.websiteUri" in request.headers["X-Goog-FieldMask"]
    assert request.headers["X-Goog-Api-Key"] == "key"
    assert _json.loads(request.body)["pageSize"] == 20


@responses.activate
def test_place_without_id_is_dropped_not_crashed():
    responses.add(
        responses.POST,
        SEARCH_TEXT_URL,
        json={"places": [{"displayName": {"text": "ghost"}}, _place("a")]},
        status=200,
    )
    results = list(PlacesClient("key").search_text("q"))
    assert [r.place_id for r in results] == ["a"]


@responses.activate
def test_malformed_rating_does_not_crash():
    responses.add(
        responses.POST,
        SEARCH_TEXT_URL,
        json={"places": [_place("a", rating="n/a", userRatingCount=None)]},
        status=200,
    )
    (result,) = list(PlacesClient("key").search_text("q"))
    assert result.rating is None
    assert result.review_count == 0


@responses.activate
def test_403_explains_the_three_usual_causes():
    responses.add(responses.POST, SEARCH_TEXT_URL, json={"error": "denied"}, status=403)
    with pytest.raises(PlacesError) as exc:
        list(PlacesClient("key").search_text("q"))
    message = str(exc.value)
    assert "billing" in message.lower()
    assert "Places API (New)" in message


@responses.activate
def test_500_is_retried_then_succeeds():
    responses.add(responses.POST, SEARCH_TEXT_URL, json={}, status=503)
    responses.add(
        responses.POST, SEARCH_TEXT_URL, json={"places": [_place("a")]}, status=200
    )
    client = PlacesClient("key")
    results = list(client.search_text("q"))
    assert len(results) == 1


def test_empty_api_key_fails_fast_with_guidance():
    with pytest.raises(PlacesError, match="PLACES_API_KEY"):
        PlacesClient("")
