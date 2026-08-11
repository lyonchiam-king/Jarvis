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


# --------------------------------------------- finding the image model, not guessing


class ListSession(FakeSession):
    """A session that answers ListModels as well as generateContent."""

    def __init__(self, models, *replies):
        super().__init__(*replies)
        self._models = models
        self.listed = 0

    def get(self, url, headers, timeout):
        self.listed += 1
        return FakeResponse(
            {
                "models": [
                    {"name": f"models/{n}", "supportedGenerationMethods": ["generateContent"]}
                    for n in self._models
                ]
            }
        )


def test_the_image_model_is_discovered_rather_than_guessed():
    """A model name in a config file is a guess with a shelf life. This one
    was wrong three separate times before it was worth admitting the API
    already knows the answer."""
    session = ListSession(["gemini-3.6-flash", "gemini-2.5-flash-image", "gemini-3.1-flash-image"])
    g = SiteGenerator("key", model="gemini-3.6-flash", max_output_tokens=1000, session=session)

    assert g.image_model == "gemini-3.1-flash-image", "should prefer the newer image model"
    assert session.listed == 1


def test_the_discovered_model_is_looked_up_once_per_run():
    session = ListSession(["gemini-3.1-flash-image"])
    g = SiteGenerator("key", model="m", max_output_tokens=1000, session=session)

    g.image_model, g.image_model, g.image_model

    assert session.listed == 1, "one lookup, not one per page"


def test_an_explicitly_configured_image_model_skips_the_lookup():
    session = ListSession(["gemini-3.1-flash-image"])
    g = SiteGenerator(
        "key", model="m", max_output_tokens=1000,
        image_model="gemini-9-pinned", session=session,
    )

    assert g.image_model == "gemini-9-pinned"
    assert session.listed == 0


def test_a_key_with_no_image_model_says_so_and_names_the_next_step():
    session = ListSession(["gemini-3.6-flash", "text-embedding-004"])
    g = SiteGenerator("key", model="m", max_output_tokens=1000, session=session)

    with pytest.raises(SiteGenError) as err:
        g.image_model

    assert "list-models" in str(err.value)
    assert "billing" in str(err.value)


# ------------------------------------------------------- injected image client


class FakeImageClient:
    """Stands in for `providers.vertex_image.VertexImageClient` -- or anything
    else duck-typed the same way -- without sitegen importing it."""

    def __init__(self, image=b"vertex-bytes", mime="image/png"):
        self._image = image
        self._mime = mime
        self.prompts = []

    def generate_image(self, prompt):
        self.prompts.append(prompt)
        return self._image, self._mime


def test_an_injected_image_client_answers_generate_image_and_is_not_gemini():
    """When a client is handed in, its bytes come back and Gemini's own image
    endpoint is never touched -- the fake session records no posts."""
    client = FakeImageClient(image=b"the-vertex-bytes", mime="image/webp")
    session = FakeSession()  # no replies queued: a post here would raise IndexError
    g = SiteGenerator(
        "key", model="m", max_output_tokens=1000,
        session=session, image_client=client,
    )

    data, mime = g.generate_image("a warm still life")

    assert (data, mime) == (b"the-vertex-bytes", "image/webp")
    assert client.prompts == ["a warm still life"]
    assert session.posts == [], "nothing should have been posted to Gemini"


def test_with_no_image_client_generate_image_still_goes_through_gemini():
    """The default, no-argument path this whole seam must leave untouched:
    behaviour byte-identical to before `image_client` existed."""
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"inlineData": {"mimeType": "image/png", "data": "aGVsbG8="}}
                    ]
                },
                "finishReason": "STOP",
            }
        ]
    }
    session = ListSession(["gemini-3.1-flash-image"], FakeResponse(payload))
    g = SiteGenerator("key", model="m", max_output_tokens=1000, session=session)

    data, mime = g.generate_image("a warm still life")

    assert (data, mime) == (b"hello", "image/png")
    assert session.posts, "the image call should have gone to Gemini"
    assert "generativelanguage.googleapis.com" in session.posts[0]["url"]


def test_the_page_goes_through_vertex_too_when_one_is_wired_in():
    """The free tier rate-limits text separately from images, so a run could
    buy every image and then fail every page -- paying for half a job. That
    is exactly what happened on the first real batch."""
    class Vertex:
        def __init__(self):
            self.text_prompts = []

        def generate_image(self, prompt):
            return b"png", "image/png"

        def generate_text(self, prompt, *, max_output_tokens):
            self.text_prompts.append((prompt, max_output_tokens))
            return VALID_HTML

    vertex = Vertex()
    g = generator(image_client=vertex, max_output_tokens=32000)

    assert g.generate("build me a page") == VALID_HTML
    assert vertex.text_prompts == [("build me a page", 32000)]
    # Nothing may reach the AI Studio endpoint.
    assert g._session.posts == []


def test_a_client_without_generate_text_leaves_the_page_call_alone():
    """The image-only client shape must keep working -- images move without
    dragging the page call with them."""
    class ImagesOnly:
        def generate_image(self, prompt):
            return b"png", "image/png"

    g = generator(candidate_response(VALID_HTML), image_client=ImagesOnly())

    assert g.generate("prompt") == VALID_HTML
    assert len(g._session.posts) == 1, "the page still goes to the Developer API"
