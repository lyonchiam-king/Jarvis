"""Gemini native-API demo site generation.

No network is reachable from this container, so every test here drives
``SiteGenerator`` through an injected fake session, fake clock, and fake
sleep -- nothing sleeps for real and nothing calls Gemini for real.
"""

import pytest

from outreach.errors import is_retryable
from outreach.providers.sitegen import SiteGenError, SiteGenerator

VALID_HTML = "<!DOCTYPE html><html><head></head><body>hi</body></html>"


class FakeResponse:
    def __init__(self, payload=None, status_code=200, text="", not_json=False):
        self._payload = payload
        self.status_code = status_code
        self.text = text or (str(payload) if payload is not None else "")
        self._not_json = not_json

    def json(self):
        if self._not_json:
            raise ValueError("not json")
        return self._payload


def candidate_response(text, finish_reason="STOP", **extra):
    payload = {
        "candidates": [
            {
                "content": {"parts": [{"text": text}]},
                "finishReason": finish_reason,
                **extra,
            }
        ]
    }
    return FakeResponse(payload)


class FakeSession:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.posts = []

    def post(self, url, json, headers, timeout):
        self.posts.append({"url": url, "body": json, "headers": headers, "timeout": timeout})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeClock:
    """Lets a test advance time explicitly instead of trusting real sleep."""

    def __init__(self, start=0.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def generator(*replies, clock=None, sleep=None, **kwargs):
    return SiteGenerator(
        "key",
        model=kwargs.pop("model", "gemini-2.5-flash"),
        max_output_tokens=kwargs.pop("max_output_tokens", 32000),
        session=FakeSession(*replies),
        clock=clock or FakeClock(),
        sleep=sleep if sleep is not None else (lambda s: None),
        **kwargs,
    )


# ------------------------------------------------------------- happy path


def test_generate_returns_the_raw_html():
    g = generator(candidate_response(VALID_HTML))
    assert g.generate("prompt") == VALID_HTML


def test_it_posts_to_the_generatecontent_endpoint_for_the_configured_model():
    g = generator(candidate_response(VALID_HTML), model="gemini-2.5-pro")
    g.generate("prompt")

    post = g._session.posts[0]
    assert post["url"] == (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.5-pro:generateContent"
    )
    assert post["headers"]["x-goog-api-key"] == "key"


def test_the_body_matches_the_native_gemini_shape():
    g = generator(candidate_response(VALID_HTML), max_output_tokens=12345)
    g.generate("write me a page")

    body = g._session.posts[0]["body"]
    assert body["contents"] == [{"parts": [{"text": "write me a page"}]}]
    assert body["generationConfig"]["maxOutputTokens"] == 12345
    assert body["generationConfig"]["temperature"] == 0.7


def test_call_count_increments_per_call():
    g = generator(candidate_response(VALID_HTML), candidate_response(VALID_HTML))
    g.generate("a")
    g.generate("b")
    assert g.call_count == 2


# ------------------------------------------------------------- fence stripping


def test_html_fence_is_stripped():
    fenced = "```html\n" + VALID_HTML + "\n```"
    g = generator(candidate_response(fenced))
    assert g.generate("prompt") == VALID_HTML


def test_bare_fence_is_stripped():
    fenced = "```\n" + VALID_HTML + "\n```"
    g = generator(candidate_response(fenced))
    assert g.generate("prompt") == VALID_HTML


# ------------------------------------------------------------- truncation


def test_max_tokens_raises_instead_of_returning_a_partial_page():
    truncated = "<!DOCTYPE html><html><body>only half the pa"
    g = generator(candidate_response(truncated, finish_reason="MAX_TOKENS"))

    with pytest.raises(SiteGenError, match="cut off mid-page"):
        g.generate("prompt")


def test_max_tokens_error_names_the_setting_to_raise():
    g = generator(candidate_response("partial", finish_reason="MAX_TOKENS"))

    with pytest.raises(SiteGenError, match="build.max_output_tokens"):
        g.generate("prompt")


# ------------------------------------------------------------- safety blocks


def test_safety_finish_reason_names_the_category():
    reply = candidate_response(
        "",
        finish_reason="SAFETY",
        safetyRatings=[
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "blocked": False},
            {"category": "HARM_CATEGORY_HARASSMENT", "blocked": True},
        ],
    )
    g = generator(reply)

    with pytest.raises(SiteGenError, match="HARM_CATEGORY_HARASSMENT"):
        g.generate("prompt")


def test_prompt_feedback_block_reason_raises():
    # "blocked the prompt" is the phrase unique to this path -- a candidates
    # list this empty would also raise via the "no candidates" guard below
    # it, and that fallback message happens to echo the whole feedback dict
    # (so a match on "OTHER" alone would pass even if this guard never ran).
    payload = {"candidates": [], "promptFeedback": {"blockReason": "OTHER"}}
    g = generator(FakeResponse(payload))

    with pytest.raises(SiteGenError, match="blocked the prompt") as exc_info:
        g.generate("prompt")
    assert "OTHER" in str(exc_info.value)


# ------------------------------------------------------------- rate limiting


def test_http_429_raises_a_retryable_error():
    # max_attempts=1 so this test stays about the error, not the retrying.
    g = generator(FakeResponse(status_code=429, text="rate limited"), max_attempts=1)

    with pytest.raises(SiteGenError) as exc_info:
        g.generate("prompt")

    assert is_retryable(exc_info.value)


def test_other_http_errors_are_not_marked_retryable():
    g = generator(FakeResponse(status_code=400, text="bad request"))

    with pytest.raises(SiteGenError) as exc_info:
        g.generate("prompt")

    assert not is_retryable(exc_info.value)


# ------------------------------------------------------------- empty shapes


def test_empty_candidates_list_raises_rather_than_returning_empty_string():
    g = generator(FakeResponse({"candidates": []}))

    with pytest.raises(SiteGenError):
        g.generate("prompt")


def test_a_candidate_with_no_text_part_raises():
    payload = {"candidates": [{"content": {"parts": []}, "finishReason": "STOP"}]}
    g = generator(FakeResponse(payload))

    with pytest.raises(SiteGenError):
        g.generate("prompt")


def test_a_200_with_an_unparseable_body_raises():
    g = generator(FakeResponse(not_json=True))

    with pytest.raises(SiteGenError, match="not JSON"):
        g.generate("prompt")


# ------------------------------------------------------------- pacing


def test_two_back_to_back_calls_sleep_for_the_gap():
    clock = FakeClock(start=100.0)
    slept = []
    g = generator(
        candidate_response(VALID_HTML),
        candidate_response(VALID_HTML),
        clock=clock,
        sleep=slept.append,
        min_interval_seconds=6.5,
    )

    g.generate("a")
    g.generate("b")

    assert slept == [6.5]


def test_a_call_after_a_long_gap_does_not_sleep():
    clock = FakeClock(start=100.0)
    slept = []
    g = generator(
        candidate_response(VALID_HTML),
        candidate_response(VALID_HTML),
        clock=clock,
        sleep=slept.append,
        min_interval_seconds=6.5,
    )

    g.generate("a")
    clock.advance(30.0)
    g.generate("b")

    assert slept == []


# ------------------------------------------------------------- misuse


def test_empty_api_key_names_the_env_var():
    with pytest.raises(SiteGenError, match="GEMINI_API_KEY"):
        SiteGenerator("", model="gemini-2.5-flash", max_output_tokens=1000)


def test_an_unknown_model_name_points_at_list_models():
    """Google's own 404 says only "not found", which reads like the whole
    setup is broken rather than one stale string. Model names move -- the
    error has to say how to find the current one."""
    import pytest

    from outreach.providers.sitegen import SiteGenError, SiteGenerator

    class Resp:
        status_code = 404
        text = '{"error": {"code": 404, "message": "models/x is not found"}}'

        def json(self):
            import json

            return json.loads(self.text)

    class Session:
        def post(self, *a, **k):
            return Resp()

    gen = SiteGenerator(
        "k", model="gemini-9-nope", max_output_tokens=1000,
        min_interval_seconds=0, session=Session(),
    )
    with pytest.raises(SiteGenError) as err:
        gen.generate("build me a page")

    assert "gemini-9-nope" in str(err.value)
    assert "list-models" in str(err.value)


# ------------------------------------------------- Google's own "try again later"


def test_a_busy_model_is_retried_rather_than_failing_the_lead():
    """503 means the model is busy, not that the request was wrong -- Google's
    message literally says "please try again later". Giving up on the first
    one turns a few seconds of waiting into a page that never got written,
    and on a sixty-lead run a brief spike would take out most of the batch.
    """
    slept = []
    g = generator(
        FakeResponse(status_code=503, text="high demand"),
        FakeResponse(status_code=503, text="high demand"),
        candidate_response(VALID_HTML),
        sleep=slept.append,
    )

    out = g.generate("prompt")

    assert out == VALID_HTML
    assert g.call_count == 3, "it should have taken three attempts"
    # Backing off, not hammering: a busy model hit hard stays busy longer.
    assert slept and slept[0] < slept[1]


def test_giving_up_after_the_last_attempt_reports_the_real_reason():
    g = generator(
        *[FakeResponse(status_code=503, text="high demand")] * 3,
        sleep=lambda s: None,
    )

    with pytest.raises(SiteGenError) as err:
        g.generate("prompt")

    assert "busy" in str(err.value).casefold()
    assert is_retryable(err.value), "the lead must come back on the next run"


def test_a_bad_request_is_never_retried():
    """Retrying a 400 wastes three calls to get the same answer."""
    g = generator(*[FakeResponse(status_code=400, text="bad request")] * 3)

    with pytest.raises(SiteGenError):
        g.generate("prompt")

    assert g.call_count == 1


def test_a_quota_error_reports_which_quota_and_waits_as_asked():
    """"Rate limited" cannot tell a per-minute pause from a per-day ceiling,
    and those need opposite reactions. Google puts both in the error details."""
    body = {
        "error": {
            "code": 429,
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [{"quotaId": "GenerateRequestsPerDayPerProject-FreeTier"}],
                },
                {
                    "@type": "type.googleapis.com/google.rpc.RetryInfo",
                    "retryDelay": "27s",
                },
            ],
        }
    }
    g = generator(FakeResponse(body, status_code=429, text="quota"), max_attempts=1)

    with pytest.raises(SiteGenError) as err:
        g.generate("prompt")

    assert "PerDay" in str(err.value)
    assert err.value.retry_after == 27.0


def test_googles_own_retry_delay_is_used_over_the_default_backoff():
    body = {
        "error": {
            "details": [
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "2s"}
            ]
        }
    }
    slept = []
    g = generator(
        FakeResponse(body, status_code=429, text="quota"),
        candidate_response(VALID_HTML),
        sleep=slept.append,
    )

    g.generate("prompt")

    # The first sleep is the retry wait. The 6.5s after it is the ordinary
    # per-minute pacing before the second request, which is unrelated.
    assert slept[0] == 2.0, "should wait what Google asked, not the default 5s"
